# pyright: reportAttributeAccessIssue=false

import json
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError
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
	TemplatePlaceholder,
	TemplateRevision,
	TemplateStatus,
	TemplateWorkflowStage,
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


def _append_document_event(document: Document, action: str, actor, comment: str = ""):
	metadata = document.metadata or {}
	events = metadata.get("events", [])
	events.append(
		{
			"action": action,
			"actor": actor.username if actor else None,
			"comment": comment,
			"timestamp": timezone.now().isoformat(),
		}
	)
	metadata["events"] = events
	document.metadata = metadata


def _ensure_revision_editable(revision: TemplateRevision):
	if revision.is_published:
		return JsonResponse(
			{"error": "Published revisions are immutable. Clone a new draft revision first."},
			status=400,
		)
	return None


def _route_status_for_stage(required_action: str) -> str:
	if required_action in {"APPROVE", "APPROVE_WITH_COMMENTS"}:
		return DocumentStatus.UNDER_APPROVAL
	return DocumentStatus.UNDER_REVIEW


def _get_active_stage(document: Document):
	return (
		document.workflow_stages.filter(status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL])
		.order_by("stage_order", "id")
		.first()
	)


def _advance_to_next_stage(document: Document, current_stage: DocumentWorkflowStage):
	next_stage = (
		document.workflow_stages.filter(stage_order__gt=current_stage.stage_order)
		.order_by("stage_order", "id")
		.first()
	)
	if next_stage is None:
		document.status = DocumentStatus.APPROVED
		document.approved_at = timezone.now()
		document.save(update_fields=["status", "approved_at", "updated_at"])
		return None

	next_stage.status = _route_status_for_stage(next_stage.required_action)
	next_stage.save(update_fields=["status", "updated_at"])
	document.status = next_stage.status
	document.save(update_fields=["status", "updated_at"])
	return next_stage


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

	route_status = DocumentStatus.UNDER_REVIEW
	document.submitted_at = timezone.now()
	_append_document_event(
		document=document,
		action="submit",
		actor=request.user if request.user.is_authenticated else None,
	)
	document.save(update_fields=["submitted_at", "metadata", "updated_at"])

	document.workflow_stages.all().delete()
	template_stages = list(document.template_revision.workflow_stages.order_by("stage_order", "id"))
	first_stage_order = template_stages[0].stage_order if template_stages else None
	for stage in template_stages:
		stage_status = DocumentStatus.DRAFT
		if stage.stage_order == first_stage_order:
			stage_status = _route_status_for_stage(stage.required_action)
			route_status = stage_status
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

	document.status = route_status
	document.save(update_fields=["status", "updated_at"])

	_snapshot_document(document, actor=request.user if request.user.is_authenticated else None)

	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"reference_number": document.reference_number,
			"workflow_stages": document.workflow_stages.count(),
		},
	)


@require_http_methods(["POST"])
def document_action(request, document_id: int, action: str):
	document = get_object_or_404(Document, pk=document_id)
	action = action.lower()
	allowed_actions = {
		"review",
		"endorse",
		"approve",
		"approve_with_comments",
		"return",
		"reject",
		"delegate",
	}
	if action not in allowed_actions:
		return JsonResponse({"error": "Unsupported action."}, status=400)

	allowed_statuses = {
		"review": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"endorse": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"approve": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"approve_with_comments": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"return": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"reject": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"delegate": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
	}
	if document.status not in allowed_statuses[action]:
		return JsonResponse(
			{"error": f"Cannot {action} document in status {document.status}."},
			status=400,
		)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	comment = payload.get("comment", "")
	actor = request.user if request.user.is_authenticated else None
	now = timezone.now()
	active_stage = _get_active_stage(document)
	if active_stage is None:
		return JsonResponse({"error": "No active workflow stage found."}, status=400)

	if action in {"delegate"}:
		delegate_to = payload.get("delegate_to")
		if not delegate_to:
			return JsonResponse({"error": "delegate_to is required for delegate action."}, status=400)
		active_stage.actor_value = delegate_to
		active_stage.comments = comment
		active_stage.acted_by = actor
		active_stage.acted_at = now
		active_stage.save(update_fields=["actor_value", "comments", "acted_by", "acted_at", "updated_at"])
		_append_document_event(document=document, action=action, actor=actor, comment=comment)
		document.save(update_fields=["metadata", "updated_at"])
		_snapshot_document(document, actor=actor)
		return JsonResponse(
			{
				"document_id": document.id,
				"action": action,
				"status": document.status,
				"delegated_to": delegate_to,
			}
		)

	action_required_map = {
		"review": {"REVIEW"},
		"endorse": {"REVIEW", "ENDORSE"},
		"approve": {"APPROVE", "APPROVE_WITH_COMMENTS"},
		"approve_with_comments": {"APPROVE", "APPROVE_WITH_COMMENTS"},
	}
	if action in action_required_map and active_stage.required_action not in action_required_map[action]:
		return JsonResponse(
			{
				"error": f"Action {action} is not valid for stage requiring {active_stage.required_action}."
			},
			status=400,
		)

	if action in {"return", "reject"}:
		terminal_status = DocumentStatus.RETURNED if action == "return" else DocumentStatus.REJECTED
		document.status = terminal_status
		active_stage.status = terminal_status
		active_stage.acted_by = actor
		active_stage.acted_at = now
		active_stage.comments = comment
		active_stage.save(update_fields=["status", "acted_by", "acted_at", "comments", "updated_at"])
		_append_document_event(document=document, action=action, actor=actor, comment=comment)
		document.save(update_fields=["status", "metadata", "updated_at"])
		_snapshot_document(document, actor=actor)
		return JsonResponse(
			{
				"document_id": document.id,
				"action": action,
				"status": document.status,
				"approved_at": document.approved_at,
			}
		)

	active_stage.status = DocumentStatus.APPROVED
	active_stage.acted_by = actor
	active_stage.acted_at = now
	active_stage.comments = comment
	active_stage.save(update_fields=["status", "acted_by", "acted_at", "comments", "updated_at"])
	next_stage = _advance_to_next_stage(document, active_stage)
	_append_document_event(document=document, action=action, actor=actor, comment=comment)
	document.save(update_fields=["metadata", "updated_at"])

	_snapshot_document(document, actor=actor)
	return JsonResponse(
		{
			"document_id": document.id,
			"action": action,
			"status": document.status,
			"approved_at": document.approved_at,
			"next_stage": next_stage.stage_order if next_stage else None,
		}
	)


@require_GET
def document_timeline(request, document_id: int):
	document = get_object_or_404(Document, pk=document_id)
	metadata = document.metadata or {}
	events = metadata.get("events", [])
	revisions = [
		{
			"version": revision.version,
			"created_at": revision.created_at,
			"status": revision.snapshot.get("status"),
		}
		for revision in document.revisions.order_by("version")
	]
	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"reference_number": document.reference_number,
			"events": events,
			"revisions": revisions,
		}
	)


@require_http_methods(["GET", "POST"])
def template_revision_placeholders(request, revision_id: int):
	revision = get_object_or_404(TemplateRevision, pk=revision_id)

	if request.method == "GET":
		results = [
			{
				"id": placeholder.id,
				"name": placeholder.name,
				"label": placeholder.label,
				"field_type": placeholder.field_type,
				"is_required": placeholder.is_required,
				"display_order": placeholder.display_order,
				"config": placeholder.config,
			}
			for placeholder in revision.placeholders.order_by("display_order", "id")
		]
		return JsonResponse({"count": len(results), "results": results})

	read_only_error = _ensure_revision_editable(revision)
	if read_only_error is not None:
		return read_only_error

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	required_fields = ["name", "label", "field_type"]
	if any(not payload.get(field_name) for field_name in required_fields):
		return JsonResponse({"error": "name, label, and field_type are required."}, status=400)

	try:
		placeholder = TemplatePlaceholder.objects.create(
			revision=revision,
			name=payload["name"],
			label=payload["label"],
			field_type=payload["field_type"],
			is_required=payload.get("is_required", False),
			display_order=payload.get("display_order", 1),
			config=payload.get("config", {}),
		)
	except (ValidationError, IntegrityError) as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": placeholder.id,
			"revision_id": revision.id,
			"name": placeholder.name,
		},
		status=201,
	)


@require_http_methods(["PATCH", "DELETE"])
def template_placeholder_detail(request, placeholder_id: int):
	placeholder = get_object_or_404(TemplatePlaceholder.objects.select_related("revision"), pk=placeholder_id)
	read_only_error = _ensure_revision_editable(placeholder.revision)
	if read_only_error is not None:
		return read_only_error

	if request.method == "DELETE":
		placeholder.delete()
		return JsonResponse({}, status=204)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	for field_name in ["label", "field_type", "is_required", "display_order", "config"]:
		if field_name in payload:
			setattr(placeholder, field_name, payload[field_name])

	try:
		placeholder.save()
	except (ValidationError, IntegrityError) as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": placeholder.id,
			"name": placeholder.name,
			"label": placeholder.label,
			"field_type": placeholder.field_type,
		}
	)


@require_http_methods(["GET", "POST"])
def template_revision_workflow_stages(request, revision_id: int):
	revision = get_object_or_404(TemplateRevision, pk=revision_id)

	if request.method == "GET":
		results = [
			{
				"id": stage.id,
				"stage_order": stage.stage_order,
				"title": stage.title,
				"mode": stage.mode,
				"actor_type": stage.actor_type,
				"actor_value": stage.actor_value,
				"required_action": stage.required_action,
				"sla_hours": stage.sla_hours,
				"is_mandatory": stage.is_mandatory,
			}
			for stage in revision.workflow_stages.order_by("stage_order", "id")
		]
		return JsonResponse({"count": len(results), "results": results})

	read_only_error = _ensure_revision_editable(revision)
	if read_only_error is not None:
		return read_only_error

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	required_fields = ["stage_order", "title", "actor_type", "actor_value", "required_action"]
	if any(payload.get(field_name) in [None, ""] for field_name in required_fields):
		return JsonResponse(
			{"error": "stage_order, title, actor_type, actor_value, and required_action are required."},
			status=400,
		)

	try:
		stage = TemplateWorkflowStage.objects.create(
			revision=revision,
			stage_order=payload["stage_order"],
			title=payload["title"],
			mode=payload.get("mode", "SEQUENTIAL"),
			actor_type=payload["actor_type"],
			actor_value=payload["actor_value"],
			required_action=payload["required_action"],
			sla_hours=payload.get("sla_hours", 48),
			is_mandatory=payload.get("is_mandatory", True),
		)
	except (ValidationError, IntegrityError) as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": stage.id,
			"revision_id": revision.id,
			"stage_order": stage.stage_order,
		},
		status=201,
	)


@require_http_methods(["PATCH", "DELETE"])
def template_workflow_stage_detail(request, stage_id: int):
	stage = get_object_or_404(TemplateWorkflowStage.objects.select_related("revision"), pk=stage_id)
	read_only_error = _ensure_revision_editable(stage.revision)
	if read_only_error is not None:
		return read_only_error

	if request.method == "DELETE":
		stage.delete()
		return JsonResponse({}, status=204)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	for field_name in [
		"stage_order",
		"title",
		"mode",
		"actor_type",
		"actor_value",
		"required_action",
		"sla_hours",
		"is_mandatory",
	]:
		if field_name in payload:
			setattr(stage, field_name, payload[field_name])

	try:
		stage.save()
	except (ValidationError, IntegrityError) as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": stage.id,
			"stage_order": stage.stage_order,
			"title": stage.title,
			"required_action": stage.required_action,
		}
	)
