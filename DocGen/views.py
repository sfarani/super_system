# pyright: reportAttributeAccessIssue=false

import json
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .models import (
	Document,
	DocumentField,
	DocumentQRToken,
	DocumentRevision,
	DocumentStatus,
	DocumentWorkflowStage,
	Template,
	TemplateCategory,
	TemplateRevision,
	TemplateStatus,
)


def _parse_json_request(request):
	if not request.body:
		return {}
	try:
		return json.loads(request.body.decode("utf-8"))
	except (json.JSONDecodeError, UnicodeDecodeError):
		raise ValidationError("Invalid JSON payload.")


def _snapshot_document(document: Document, actor=None):
	latest = document.revisions.order_by("-version").first()
	next_version = 1 if latest is None else latest.version + 1
	field_data = [
		{
			"placeholder_name": field.placeholder_name,
			"value_text": field.value_text,
			"value_json": field.value_json,
		}
		for field in document.fields.order_by("placeholder_name")
	]
	snapshot = {
		"document_id": document.id,
		"status": document.status,
		"reference_number": document.reference_number,
		"document_type": document.document_type,
		"title": document.title,
		"subject": document.subject,
		"fields": field_data,
	}
	DocumentRevision.objects.create(
		document=document,
		version=next_version,
		snapshot=snapshot,
		created_by=actor,
	)


@require_GET
def index(request):
	return JsonResponse(
		{
			"module": "DocGen",
			"status": "ok",
			"message": "DocGen module is active.",
		}
	)


@require_GET
def verify_token(request, token: str):
	try:
		qr = DocumentQRToken.objects.select_related("document").get(token=token)
	except DocumentQRToken.DoesNotExist:
		return JsonResponse({"valid": False, "reason": "token_not_found"}, status=404)

	status = "revoked" if qr.is_revoked else "valid"
	payload = {
		"valid": not qr.is_revoked,
		"status": status,
		"reference_number": qr.document.reference_number,
		"document_type": qr.document.document_type,
		"finalized_at": qr.document.finalized_at,
	}
	if qr.is_revoked:
		payload["revoked_reason"] = qr.revoked_reason
	return JsonResponse(payload)


@require_http_methods(["GET", "POST"])
def template_collection(request):
	if request.method == "GET":
		status_filter = request.GET.get("status")
		queryset = Template.objects.select_related("category").all()
		if status_filter:
			queryset = queryset.filter(status=status_filter)

		results = []
		for template in queryset:
			latest_revision = template.revisions.order_by("-version").first()
			results.append(
				{
					"id": template.id,
					"code": template.code,
					"title": template.title,
					"status": template.status,
					"category": template.category.name,
					"latest_revision": latest_revision.version if latest_revision else None,
				}
			)
		return JsonResponse({"count": len(results), "results": results})

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	category_id = payload.get("category_id")
	title = payload.get("title")
	code = payload.get("code")
	description = payload.get("description", "")
	layout_schema = payload.get("layout_schema", {})

	if not category_id or not title or not code:
		return JsonResponse(
			{"error": "category_id, title, and code are required."},
			status=400,
		)

	category = get_object_or_404(TemplateCategory, pk=category_id)

	try:
		template = Template.objects.create(
			category=category,
			title=title,
			code=code,
			description=description,
		)
		revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			layout_schema=layout_schema,
		)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": template.id,
			"code": template.code,
			"title": template.title,
			"status": template.status,
			"revision": revision.version,
		},
		status=201,
	)


@require_http_methods(["POST"])
def template_clone_revision(request, template_id: int):
	template = get_object_or_404(Template, pk=template_id)
	new_revision = template.create_next_revision(created_by=request.user if request.user.is_authenticated else None)
	return JsonResponse(
		{
			"template_id": template.id,
			"new_revision": new_revision.version,
			"status": template.status,
		},
		status=201,
	)


@require_http_methods(["POST"])
def template_publish(request, template_id: int):
	template = get_object_or_404(Template, pk=template_id)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	revision_id = payload.get("revision_id")
	if revision_id:
		revision = get_object_or_404(TemplateRevision, pk=revision_id, template=template)
	else:
		revision = template.revisions.order_by("-version").first()
		if revision is None:
			return JsonResponse({"error": "Template has no revisions to publish."}, status=400)

	try:
		template.publish_revision(revision)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"template_id": template.id,
			"published_revision": revision.version,
			"status": template.status,
		},
	)


@require_http_methods(["POST"])
def template_retire(request, template_id: int):
	template = get_object_or_404(Template, pk=template_id)
	template.status = TemplateStatus.RETIRED
	template.is_locked = True
	template.save(update_fields=["status", "is_locked", "updated_at"])
	return JsonResponse(
		{
			"template_id": template.id,
			"status": template.status,
		},
	)


@require_http_methods(["GET", "POST"])
def document_collection(request):
	if request.method == "GET":
		documents = Document.objects.select_related("template_revision", "originator").all()[:100]
		results = [
			{
				"id": document.id,
				"title": document.title,
				"document_type": document.document_type,
				"status": document.status,
				"reference_number": document.reference_number,
				"template_revision": document.template_revision.version,
			}
			for document in documents
		]
		return JsonResponse({"count": len(results), "results": results})

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	template_revision_id = payload.get("template_revision_id")
	document_type = payload.get("document_type")
	title = payload.get("title")
	subject = payload.get("subject", "")

	if not template_revision_id or not document_type or not title:
		return JsonResponse(
			{"error": "template_revision_id, document_type, and title are required."},
			status=400,
		)

	template_revision = get_object_or_404(TemplateRevision, pk=template_revision_id)
	document = Document.objects.create(
		template_revision=template_revision,
		document_type=document_type,
		title=title,
		subject=subject,
		originator=request.user if request.user.is_authenticated else None,
	)

	return JsonResponse(
		{
			"id": document.id,
			"status": document.status,
			"title": document.title,
		},
		status=201,
	)


@require_http_methods(["POST"])
def document_set_fields(request, document_id: int):
	document = get_object_or_404(Document, pk=document_id)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	fields = payload.get("fields", [])
	if not isinstance(fields, list):
		return JsonResponse({"error": "fields must be a list."}, status=400)

	updated = 0
	for item in fields:
		placeholder_name = item.get("placeholder_name")
		if not placeholder_name:
			continue
		value_text = item.get("value_text", "")
		value_json = item.get("value_json", {})
		DocumentField.objects.update_or_create(
			document=document,
			placeholder_name=placeholder_name,
			defaults={"value_text": value_text, "value_json": value_json},
		)
		updated += 1

	return JsonResponse({"document_id": document.id, "updated_fields": updated})


@require_http_methods(["POST"])
def document_submit(request, document_id: int):
	document = get_object_or_404(Document.objects.select_related("template_revision"), pk=document_id)
	if document.status not in [DocumentStatus.DRAFT, DocumentStatus.RETURNED]:
		return JsonResponse({"error": "Only draft or returned documents can be submitted."}, status=400)

	if not document.reference_number:
		document.assign_reference_number(org_code="HQ")

	document.status = DocumentStatus.UNDER_REVIEW
	document.submitted_at = timezone.now()
	document.save(update_fields=["status", "submitted_at", "updated_at"])

	document.workflow_stages.all().delete()
	template_stages = list(document.template_revision.workflow_stages.order_by("stage_order", "id"))
	first_stage_order = template_stages[0].stage_order if template_stages else None
	for stage in template_stages:
		stage_status = (
			DocumentStatus.UNDER_REVIEW if stage.stage_order == first_stage_order else DocumentStatus.DRAFT
		)
		DocumentWorkflowStage.objects.create(
			document=document,
			stage_order=stage.stage_order,
			title=stage.title,
			actor_type=stage.actor_type,
			actor_value=stage.actor_value,
			required_action=stage.required_action,
			status=stage_status,
			due_at=timezone.now() + timedelta(hours=stage.sla_hours),
		)

	_snapshot_document(document, actor=request.user if request.user.is_authenticated else None)

	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"reference_number": document.reference_number,
			"workflow_stages": document.workflow_stages.count(),
		},
	)
