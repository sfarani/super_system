# pyright: reportAttributeAccessIssue=false

import json
import uuid
from datetime import timedelta

from django.conf import settings as django_settings
from django.core import signing
from django.core.signing import BadSignature
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.utils import OperationalError, ProgrammingError
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .models import (
	Document,
	DocumentAttachment,
	DocumentComment,
	DocumentField,
	DocumentPDF,
	DocumentQRToken,
	DocumentRevision,
	DocumentStatus,
	DocumentWorkflowStage,
	RetentionPolicy,
	Template,
	TemplateCategory,
	TemplatePlaceholder,
	TemplateRevision,
	TemplateTokenDefinition,
	TemplateStatus,
	TemplateWorkflowStage,
)
from .services import (
	emit_notification_event,
	generate_pdf_artifact,
	generate_pdf_preview,
	resolve_workflow_actor,
	sanitize_rich_text_html,
)


_DOCGEN_PERMISSION_PREFIX = f"{Template._meta.app_label}."
_DOCGEN_ROLE_PERMISSIONS = {
	"docgen.originator": f"{_DOCGEN_PERMISSION_PREFIX}originator",
	"docgen.reviewer": f"{_DOCGEN_PERMISSION_PREFIX}reviewer",
	"docgen.approver": f"{_DOCGEN_PERMISSION_PREFIX}approver",
	"docgen.template_author": f"{_DOCGEN_PERMISSION_PREFIX}template_author",
	"docgen.template_publisher": f"{_DOCGEN_PERMISSION_PREFIX}template_publisher",
	"docgen.admin": f"{_DOCGEN_PERMISSION_PREFIX}admin",
	"docgen.viewer": f"{_DOCGEN_PERMISSION_PREFIX}viewer",
}


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
	event_timestamp = timezone.now().isoformat()
	metadata = document.metadata or {}
	events = metadata.get("events", [])
	events.append(
		{
			"action": action,
			"actor": actor.username if actor else None,
			"comment": comment,
			"timestamp": event_timestamp,
		}
	)
	metadata["events"] = events
	document.metadata = metadata
	emit_notification_event(
		event_type=f"docgen.document.{action}",
		payload={
			"document_id": document.id,
			"reference_number": document.reference_number,
			"document_type": document.document_type,
			"status": document.status,
			"action": action,
			"actor": actor.username if actor else None,
			"comment": comment,
			"timestamp": event_timestamp,
		},
	)


def _emit_template_event(action: str, template: Template, actor=None, extra_payload=None):
	payload = {
		"template_id": template.id,
		"code": template.code,
		"title": template.title,
		"status": template.status,
		"actor": actor.username if actor else None,
		"timestamp": timezone.now().isoformat(),
	}
	if extra_payload:
		payload.update(extra_payload)
	emit_notification_event(event_type=f"docgen.template.{action}", payload=payload)


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


def _is_docgen_rbac_enforced() -> bool:
	return bool(getattr(django_settings, "DOCGEN_ENFORCE_RBAC", False))


def _has_docgen_role(user, allowed_roles) -> bool:
	if not _is_docgen_rbac_enforced():
		return True
	if not user.is_authenticated:
		return False
	if user.is_superuser:
		return True
	for role in allowed_roles:
		permission = _DOCGEN_ROLE_PERMISSIONS.get(role)
		if permission and user.has_perm(permission):
			return True
	return False


def _authorize_docgen_action(request, allowed_roles):
	if _has_docgen_role(request.user, allowed_roles):
		return None
	return JsonResponse({"error": "Forbidden by DocGen role policy."}, status=403)


def _archive_retention_days(document_type: str = "") -> int:
	policy_days = RetentionPolicy.get_active_days(document_type=document_type)
	if policy_days is not None:
		return int(policy_days)
	return int(getattr(django_settings, "DOCGEN_DEFAULT_ARCHIVE_DAYS", 365))


def _can_edit_document_workflow(document: Document) -> bool:
	if document.status in [DocumentStatus.DRAFT, DocumentStatus.RETURNED]:
		return True
	if document.status in [DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL]:
		return not document.workflow_stages.filter(
			status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL]
		).exists()
	return False


def _coerce_positive_int(value, default: int, max_value: int) -> int:
	try:
		parsed = int(value)
	except (TypeError, ValueError):
		return default
	return max(1, min(parsed, max_value))


def _issue_or_get_qr_token(document: Document, pdf_sha256: str = "") -> DocumentQRToken:
	qr = getattr(document, "qr_token", None)

	payload = {
		"reference_number": document.reference_number,
		"document_type": document.document_type,
		"finalized_at": (document.finalized_at or timezone.now()).isoformat(),
	}
	if pdf_sha256:
		payload["pdf_sha256"] = pdf_sha256
	signed_payload = signing.dumps(payload, salt="docgen-verify")

	if qr is not None:
		# Update existing token's signed payload to include pdf hash if newly provided.
		if pdf_sha256 and "pdf_sha256" not in (qr.signed_payload or ""):
			qr.signed_payload = signed_payload
			qr.save(update_fields=["signed_payload", "updated_at"])
		return qr

	token = uuid.uuid4().hex
	return DocumentQRToken.objects.create(
		document=document,
		token=token,
		signed_payload=signed_payload,
	)


def _get_active_stage(document: Document):
	return (
		document.workflow_stages.filter(status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL])
		.order_by("stage_order", "id")
		.first()
	)


def _get_active_stage_queryset(document: Document):
	active = document.workflow_stages.filter(status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL])
	first = active.order_by("stage_order", "id").first()
	if first is None:
		return document.workflow_stages.none()
	return active.filter(stage_order=first.stage_order).order_by("id")


def _activate_stage_order(document: Document, stage_order: int):
	stages = list(document.workflow_stages.filter(stage_order=stage_order).order_by("id"))
	if not stages:
		return []

	route_status = DocumentStatus.UNDER_REVIEW
	if any(stage.required_action in {"APPROVE", "APPROVE_WITH_COMMENTS"} for stage in stages):
		route_status = DocumentStatus.UNDER_APPROVAL

	for stage in stages:
		stage.status = route_status
		stage.save(update_fields=["status", "updated_at"])

	document.status = route_status
	document.save(update_fields=["status", "updated_at"])
	return stages


def _resolve_next_stage_order(document: Document, current_order: int, payload=None):
	payload = payload or {}
	payload_target = payload.get("next_stage_order")
	if payload_target is not None:
		try:
			next_stage_order = int(payload_target)
		except (TypeError, ValueError):
			next_stage_order = None
		if next_stage_order and next_stage_order > current_order:
			if document.workflow_stages.filter(stage_order=next_stage_order).exists():
				return next_stage_order

	branch_rules = (document.metadata or {}).get("branch_rules", {})
	branch_target = branch_rules.get(str(current_order))
	if branch_target is not None:
		try:
			next_stage_order = int(branch_target)
		except (TypeError, ValueError):
			next_stage_order = None
		if next_stage_order and next_stage_order > current_order:
			if document.workflow_stages.filter(stage_order=next_stage_order).exists():
				return next_stage_order

	# M2: Evaluate branch_condition on the TemplateWorkflowStage definition.
	# Try each candidate next stage in order; pick the first whose branch_condition matches.
	doc_fields = document.field_values or {}
	candidates = (
		document.workflow_stages.filter(stage_order__gt=current_order)
		.select_related("template_stage")
		.order_by("stage_order", "id")
	)
	for candidate in candidates:
		template_stage = getattr(candidate, "template_stage", None)
		branch_cond = getattr(template_stage, "branch_condition", None) if template_stage else None
		if not branch_cond or not isinstance(branch_cond, dict):
			return candidate.stage_order  # no condition = unconditionally next
		field_name = str(branch_cond.get("field") or "").strip()
		operator = str(branch_cond.get("operator") or "eq").strip()
		expected = branch_cond.get("value")
		if not field_name:
			return candidate.stage_order
		actual = doc_fields.get(field_name)
		match = False
		if operator == "eq":
			match = str(actual) == str(expected)
		elif operator == "neq":
			match = str(actual) != str(expected)
		elif operator == "in":
			match = str(actual) in [str(v) for v in (expected if isinstance(expected, list) else [expected])]
		elif operator == "nin":
			match = str(actual) not in [str(v) for v in (expected if isinstance(expected, list) else [expected])]
		else:
			match = str(actual) == str(expected)
		if match:
			return candidate.stage_order

	# No branch condition matched — fall back to plain sequential next.
	next_stage = (
		document.workflow_stages.filter(stage_order__gt=current_order)
		.order_by("stage_order", "id")
		.first()
	)
	return None if next_stage is None else next_stage.stage_order


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
		qr = DocumentQRToken.objects.select_related("document__originator").get(token=token)
	except DocumentQRToken.DoesNotExist:
		return JsonResponse({"valid": False, "reason": "token_not_found"}, status=404)

	try:
		signing.loads(qr.signed_payload, salt="docgen-verify")
	except BadSignature:
		return JsonResponse(
			{
				"valid": False,
				"status": "tampered",
				"reason": "token_tampered",
			},
			status=400,
		)

	doc = qr.document
	superseded_by = doc.superseded_by_documents.order_by("-created_at").first()
	if superseded_by is not None:
		return JsonResponse(
			{
				"valid": False,
				"status": "superseded",
				"reason": "document_superseded",
				"reference_number": doc.reference_number,
				"document_type": doc.document_type,
				"finalized_at": doc.finalized_at,
				"superseded_by": superseded_by.reference_number,
			}
		)

	# Resolve final approver (last acted APPROVE/APPROVE_WITH_COMMENTS stage)
	final_approver_stage = (
		doc.workflow_stages
		.filter(required_action__in=["APPROVE", "APPROVE_WITH_COMMENTS"])
		.exclude(acted_at=None)
		.order_by("-stage_order", "-acted_at")
		.select_related("acted_by")
		.first()
	)
	final_approver = None
	if final_approver_stage and final_approver_stage.acted_by:
		actor = final_approver_stage.acted_by
		full_name = actor.get_full_name()
		final_approver = full_name if full_name else actor.username

	originator_display = None
	if doc.originator:
		full_name = doc.originator.get_full_name()
		originator_display = full_name if full_name else doc.originator.username

	status = "revoked" if qr.is_revoked else "valid"
	payload = {
		"valid": not qr.is_revoked,
		"status": status,
		"title": doc.title,
		"reference_number": doc.reference_number,
		"document_type": doc.document_type,
		"originator": originator_display,
		"approved_at": doc.approved_at,
		"finalized_at": doc.finalized_at,
		"final_approver": final_approver,
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
		auth_error = _authorize_docgen_action(
			request,
			{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
		)
		if auth_error is not None:
			return auth_error

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

	_emit_template_event(
		action="create",
		template=template,
		actor=request.user if request.user.is_authenticated else None,
		extra_payload={"revision": revision.version},
	)

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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	template = get_object_or_404(Template, pk=template_id)
	new_revision = template.create_next_revision(created_by=request.user if request.user.is_authenticated else None)
	_emit_template_event(
		action="clone_revision",
		template=template,
		actor=request.user if request.user.is_authenticated else None,
		extra_payload={"new_revision": new_revision.version},
	)
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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	template = get_object_or_404(Template, pk=template_id)

	payload = {}
	if "application/json" in (request.content_type or ""):
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

	_emit_template_event(
		action="publish",
		template=template,
		actor=request.user if request.user.is_authenticated else None,
		extra_payload={"published_revision": revision.version},
	)

	return JsonResponse(
		{
			"template_id": template.id,
			"published_revision": revision.version,
			"status": template.status,
		},
	)


@require_http_methods(["POST"])
def template_retire(request, template_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	template = get_object_or_404(Template, pk=template_id)
	template.status = TemplateStatus.RETIRED
	template.is_locked = True
	template.save(update_fields=["status", "is_locked", "updated_at"])
	_emit_template_event(
		action="retire",
		template=template,
		actor=request.user if request.user.is_authenticated else None,
	)
	return JsonResponse(
		{
			"template_id": template.id,
			"status": template.status,
		},
	)


@require_http_methods(["POST"])
def document_set_fields(request, document_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.originator", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)

	payload = {}
	if "application/json" in (request.content_type or ""):
		try:
			payload = _parse_json_request(request)
		except ValidationError as error:
			return JsonResponse({"error": str(error)}, status=400)

	fields = payload.get("fields", [])
	if not isinstance(fields, list):
		return JsonResponse({"error": "fields must be a list."}, status=400)
	template_tokens_payload = payload.get("template_tokens", None)
	if template_tokens_payload is not None and not isinstance(template_tokens_payload, dict):
		return JsonResponse({"error": "template_tokens must be a JSON object."}, status=400)

	updated = 0
	updated_names = []
	for item in fields:
		placeholder_name = item.get("placeholder_name")
		if not placeholder_name:
			continue
		value_text = "" if item.get("value_text") is None else str(item.get("value_text", ""))
		value_json = item.get("value_json", {})

		# `body_text` is treated as rich text. Store a sanitized HTML projection in value_text,
		# while retaining the editor payload (e.g., Quill Delta) in value_json.
		if str(placeholder_name).strip().lower() == "body_text":
			value_text = sanitize_rich_text_html(value_text)
			if value_json in [None, ""]:
				value_json = {}
		DocumentField.objects.update_or_create(
			document=document,
			placeholder_name=placeholder_name,
			defaults={"value_text": value_text, "value_json": value_json},
		)
		updated += 1
		updated_names.append(placeholder_name)

	updated_token_count = None
	if template_tokens_payload is not None:
		requested_keys = [str(k).strip() for k in template_tokens_payload.keys() if str(k).strip()]
		try:
			definitions = {
				row["key"]: row
				for row in TemplateTokenDefinition.objects.filter(is_active=True, key__in=requested_keys).values("key", "allow_document_override")
			}
			# Allow ad-hoc layout tokens that are not present in central registry definitions.
			# Restrict only those keys that are explicitly registered as non-overridable.
			non_overridable = sorted(
				key
				for key in requested_keys
				if key in definitions and not definitions[key]["allow_document_override"]
			)
			if non_overridable:
				return JsonResponse(
					{"error": f"Token keys not allowed for document override: {', '.join(non_overridable)}"},
					status=400,
				)
		except (ProgrammingError, OperationalError):
			# Registry tables may not exist before migrations are applied.
			pass

		normalized_tokens = {}
		for key, value in template_tokens_payload.items():
			if not isinstance(key, str):
				continue
			normalized_key = key.strip()
			if not normalized_key:
				continue
			normalized_tokens[normalized_key] = "" if value is None else str(value)

		metadata = document.metadata if isinstance(document.metadata, dict) else {}
		metadata["template_tokens"] = normalized_tokens
		document.metadata = metadata
		document.save(update_fields=["metadata", "updated_at"])
		updated_token_count = len(normalized_tokens)

	emit_notification_event(
		event_type="docgen.document.set_fields",
		payload={
			"document_id": document.id,
			"reference_number": document.reference_number,
			"status": document.status,
			"updated_fields": updated,
			"placeholder_names": updated_names,
			"template_tokens_updated": updated_token_count,
			"actor": request.user.username if request.user.is_authenticated else None,
			"timestamp": timezone.now().isoformat(),
		},
	)

	response_payload = {"document_id": document.id, "updated_fields": updated}
	if updated_token_count is not None:
		response_payload["template_tokens_updated"] = updated_token_count
	return JsonResponse(response_payload)


@require_http_methods(["POST"])
def document_submit(request, document_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.originator", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document.objects.select_related("template_revision"), pk=document_id)
	if document.status not in [DocumentStatus.DRAFT, DocumentStatus.RETURNED]:
		return JsonResponse({"error": "Only draft or returned documents can be submitted."}, status=400)

	configured_document_stages = list(document.workflow_stages.order_by("stage_order", "id"))
	template_stages = list(document.template_revision.workflow_stages.order_by("stage_order", "id"))
	if not configured_document_stages and not template_stages:
		return JsonResponse(
			{
				"error": "Cannot submit document because no workflow stages are configured. Add document workflow stages or configure the template revision workflow first.",
			},
			status=400,
		)

	if not document.reference_number:
		document.assign_reference_number()

	route_status = DocumentStatus.UNDER_REVIEW
	document.submitted_at = timezone.now()
	_append_document_event(
		document=document,
		action="submit",
		actor=request.user if request.user.is_authenticated else None,
	)
	document.save(update_fields=["submitted_at", "metadata", "updated_at"])

	if configured_document_stages:
		first_stage_order = configured_document_stages[0].stage_order
		now = timezone.now()
		for stage in configured_document_stages:
			stage_status = DocumentStatus.DRAFT
			if stage.stage_order == first_stage_order:
				stage_status = _route_status_for_stage(stage.required_action)
				route_status = stage_status
			resolved_actor_value = resolve_workflow_actor(
				actor_type=stage.actor_type,
				actor_value=stage.actor_value,
				document=document,
			)
			stage.actor_value = resolved_actor_value
			stage.status = stage_status
			stage.acted_by = None
			stage.acted_at = None
			stage.reminder_sent_at = None
			stage.escalated_at = None
			stage.escalation_level = 0
			stage.comments = ""
			stage.due_at = now + timedelta(hours=48)
			stage.save(
				update_fields=[
					"actor_value",
					"status",
					"acted_by",
					"acted_at",
					"reminder_sent_at",
					"escalated_at",
					"escalation_level",
					"comments",
					"due_at",
					"updated_at",
				]
			)
	else:
		document.workflow_stages.all().delete()
		first_stage_order = template_stages[0].stage_order if template_stages else None
		for stage in template_stages:
			stage_status = DocumentStatus.DRAFT
			if stage.stage_order == first_stage_order:
				stage_status = _route_status_for_stage(stage.required_action)
				route_status = stage_status
			resolved_actor_value = resolve_workflow_actor(
				actor_type=stage.actor_type,
				actor_value=stage.actor_value,
				document=document,
			)
			DocumentWorkflowStage.objects.create(
				document=document,
				stage_order=stage.stage_order,
				title=stage.title,
				execution_mode=stage.mode,
				actor_type=stage.actor_type,
				actor_value=resolved_actor_value,
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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.reviewer", "docgen.approver", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	action = action.lower()
	allowed_actions = {
		"review",
		"endorse",
		"approve",
		"approve_with_comments",
		"request_clarification",
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
		"request_clarification": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"return": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"reject": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
		"delegate": {DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL},
	}
	if document.status not in allowed_statuses[action]:
		return JsonResponse(
			{"error": f"Cannot {action} document in status {document.status}."},
			status=400,
		)

	payload = {}
	if "application/json" in (request.content_type or ""):
		try:
			payload = _parse_json_request(request)
		except ValidationError as error:
			return JsonResponse({"error": str(error)}, status=400)

	comment = payload.get("comment", "")
	actor = request.user if request.user.is_authenticated else None
	now = timezone.now()
	active_stages = _get_active_stage_queryset(document)
	if not active_stages.exists():
		return JsonResponse({"error": "No active workflow stage found."}, status=400)

	stage_id = payload.get("stage_id")
	if stage_id is not None:
		target_stage = active_stages.filter(pk=stage_id).first()
		if target_stage is None:
			return JsonResponse({"error": "stage_id is not active for this document."}, status=400)
	else:
		target_stage = active_stages.first()

	if action == "delegate":
		delegate_to = payload.get("delegate_to")
		if not delegate_to:
			return JsonResponse({"error": "delegate_to is required for delegate action."}, status=400)
		target_stage.actor_value = delegate_to
		target_stage.comments = comment
		target_stage.acted_by = actor
		target_stage.acted_at = now
		target_stage.save(update_fields=["actor_value", "comments", "acted_by", "acted_at", "updated_at"])
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

	if action == "request_clarification":
		target_stage.status = DocumentStatus.UNDER_REVIEW
		target_stage.acted_by = actor
		target_stage.acted_at = now
		target_stage.comments = comment
		target_stage.save(update_fields=["status", "acted_by", "acted_at", "comments", "updated_at"])
		document.status = DocumentStatus.UNDER_REVIEW
		_append_document_event(document=document, action=action, actor=actor, comment=comment)
		document.save(update_fields=["status", "metadata", "updated_at"])
		_snapshot_document(document, actor=actor)
		return JsonResponse(
			{
				"document_id": document.id,
				"action": action,
				"status": document.status,
			}
		)

	action_required_map = {
		"review": {"REVIEW"},
		"endorse": {"REVIEW", "ENDORSE"},
		"approve": {"APPROVE", "APPROVE_WITH_COMMENTS"},
		"approve_with_comments": {"APPROVE", "APPROVE_WITH_COMMENTS"},
	}
	if action in action_required_map and target_stage.required_action not in action_required_map[action]:
		return JsonResponse(
			{
				"error": f"Action {action} is not valid for stage requiring {target_stage.required_action}."
			},
			status=400,
		)

	if action in {"return", "reject"}:
		terminal_status = DocumentStatus.RETURNED if action == "return" else DocumentStatus.REJECTED
		document.status = terminal_status
		active_stages.update(status=terminal_status, acted_by=actor, acted_at=now, comments=comment)
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

	target_stage.status = DocumentStatus.APPROVED
	target_stage.acted_by = actor
	target_stage.acted_at = now
	target_stage.comments = comment
	target_stage.save(update_fields=["status", "acted_by", "acted_at", "comments", "updated_at"])

	current_order_stages = document.workflow_stages.filter(stage_order=target_stage.stage_order).order_by("id")
	group_mode = current_order_stages.first().execution_mode if current_order_stages.exists() else "SEQUENTIAL"

	advance_ready = True
	if group_mode == "PARALLEL_ALL":
		advance_ready = not current_order_stages.exclude(status=DocumentStatus.APPROVED).exists()
	elif group_mode == "PARALLEL_ANY":
		current_order_stages.exclude(pk=target_stage.pk).exclude(status=DocumentStatus.APPROVED).update(
			status=DocumentStatus.APPROVED,
			acted_by=actor,
			acted_at=now,
			comments="Auto-closed by PARALLEL_ANY",
		)
		advance_ready = True

	next_stage = None
	if advance_ready:
		next_order = _resolve_next_stage_order(document, target_stage.stage_order, payload=payload)
		if next_order is None:
			document.status = DocumentStatus.APPROVED
			document.approved_at = now
			document.save(update_fields=["status", "approved_at", "updated_at"])
		else:
			activated = _activate_stage_order(document, next_order)
			next_stage = activated[0] if activated else None

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


@require_http_methods(["POST"])
def document_finalize(request, document_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.approver", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	if document.status != DocumentStatus.APPROVED:
		return JsonResponse(
			{"error": "Only approved documents can be finalized."},
			status=400,
		)

	now = timezone.now()
	document.status = DocumentStatus.FINALIZED
	document.finalized_at = now
	_append_document_event(
		document=document,
		action="finalize",
		actor=request.user if request.user.is_authenticated else None,
	)
	document.save(update_fields=["status", "finalized_at", "metadata", "updated_at"])
	# Issue QR token first (without hash); PDF generated next.
	# If Celery is available, generate the PDF asynchronously and update the token later.
	# If not, fall back to synchronous generation so the hash is available immediately.
	_use_async = bool(getattr(django_settings, "DOCGEN_ASYNC_PDF_GENERATION", False))
	actor_id = request.user.id if request.user.is_authenticated else None

	if _use_async:
		qr = _issue_or_get_qr_token(document)
		_snapshot_document(document, actor=request.user if request.user.is_authenticated else None)
		from .tasks import generate_pdf_artifact_task  # noqa: PLC0415
		generate_pdf_artifact_task.delay(document_id=document.id, generated_by_id=actor_id)
		verify_url = reverse("docgen:verify-token", kwargs={"token": qr.token})
		return JsonResponse(
			{
				"document_id": document.id,
				"status": document.status,
				"finalized_at": document.finalized_at,
				"qr_token": qr.token,
				"verify_url": verify_url,
				"pdf_version": None,
				"pdf_sha256": None,
				"pdf_generation": "queued",
			}
		)

	# Synchronous path (default): generate PDF then embed hash in QR token.
	pdf = generate_pdf_artifact(
		document=document,
		generated_by=request.user if request.user.is_authenticated else None,
	)
	qr = _issue_or_get_qr_token(document, pdf_sha256=pdf.sha256_hash)
	_snapshot_document(document, actor=request.user if request.user.is_authenticated else None)

	verify_url = reverse("docgen:verify-token", kwargs={"token": qr.token})
	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"finalized_at": document.finalized_at,
			"qr_token": qr.token,
			"verify_url": verify_url,
			"pdf_version": pdf.version,
			"pdf_sha256": pdf.sha256_hash,
		}
	)


@require_http_methods(["POST"])
def document_archive(request, document_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	if document.status == DocumentStatus.ARCHIVED:
		return JsonResponse(
			{
				"document_id": document.id,
				"status": document.status,
				"retention_days": _archive_retention_days(document_type=document.document_type),
				"forced": False,
				"archived_at": document.archived_at,
			}
		)
	if document.status != DocumentStatus.FINALIZED:
		return JsonResponse({"error": "Only finalized documents can be archived."}, status=400)

	payload = {}
	if "application/json" in (request.content_type or ""):
		try:
			payload = _parse_json_request(request)
		except ValidationError as error:
			return JsonResponse({"error": str(error)}, status=400)

	force = bool(payload.get("force", False))
	retention_days = _archive_retention_days(document_type=document.document_type)
	if not force:
		if document.finalized_at is None:
			return JsonResponse({"error": "Document missing finalized timestamp."}, status=400)
		eligible_on = document.finalized_at + timedelta(days=retention_days)
		if timezone.now() < eligible_on:
			return JsonResponse(
				{
					"error": "Retention window not reached.",
					"eligible_on": eligible_on,
					"retention_days": retention_days,
				},
				status=400,
			)

	document.status = DocumentStatus.ARCHIVED
	document.archived_at = timezone.now()
	_append_document_event(
		document=document,
		action="archive",
		actor=request.user if request.user.is_authenticated else None,
		comment="forced" if force else "",
	)
	document.save(update_fields=["status", "archived_at", "metadata", "updated_at"])
	_snapshot_document(document, actor=request.user if request.user.is_authenticated else None)

	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"retention_days": retention_days,
			"forced": force,
			"archived_at": document.archived_at,
		}
	)


@require_GET
def document_archive_collection(request):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	queryset = Document.objects.filter(status=DocumentStatus.ARCHIVED).select_related("template_revision")

	search = (request.GET.get("q") or "").strip()
	if search:
		queryset = queryset.filter(Q(reference_number__icontains=search) | Q(title__icontains=search))

	document_type = request.GET.get("document_type")
	if document_type:
		queryset = queryset.filter(document_type=document_type)

	archived_after = request.GET.get("archived_after")
	if archived_after:
		parsed_after = parse_datetime(archived_after)
		if parsed_after is None:
			return JsonResponse({"error": "archived_after must be a valid ISO datetime."}, status=400)
		queryset = queryset.filter(archived_at__gte=parsed_after)

	archived_before = request.GET.get("archived_before")
	if archived_before:
		parsed_before = parse_datetime(archived_before)
		if parsed_before is None:
			return JsonResponse({"error": "archived_before must be a valid ISO datetime."}, status=400)
		queryset = queryset.filter(archived_at__lte=parsed_before)

	limit = _coerce_positive_int(request.GET.get("limit"), default=50, max_value=200)
	documents = list(queryset.order_by("-archived_at", "-id")[:limit])

	results = [
		{
			"id": document.id,
			"title": document.title,
			"reference_number": document.reference_number,
			"document_type": document.document_type,
			"status": document.status,
			"archived_at": document.archived_at,
			"finalized_at": document.finalized_at,
			"template_revision": document.template_revision.version,
		}
		for document in documents
	]

	return JsonResponse(
		{
			"count": len(results),
			"results": results,
			"retention_days": _archive_retention_days(),
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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

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
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.template_author", "docgen.template_publisher", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

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


@require_http_methods(["GET", "PATCH"])
def template_revision_layout(request, revision_id: int):
	"""GET or PATCH the layout_schema of a draft template revision."""
	revision = get_object_or_404(TemplateRevision, pk=revision_id)

	if request.method == "GET":
		return JsonResponse({"revision_id": revision.id, "layout_schema": revision.layout_schema})

	# PATCH — only allowed on unpublished revisions
	read_only_error = _ensure_revision_editable(revision)
	if read_only_error is not None:
		return read_only_error

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	layout_schema = payload.get("layout_schema")
	if not isinstance(layout_schema, dict):
		return JsonResponse({"error": "layout_schema must be a JSON object."}, status=400)

	revision.layout_schema = layout_schema
	# bypass full_clean immutability check — revision is not published
	TemplateRevision.objects.filter(pk=revision.pk).update(layout_schema=layout_schema)
	return JsonResponse({"revision_id": revision.id, "layout_schema": revision.layout_schema})


# ---------------------------------------------------------------------------
# Document detail / update / withdraw
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "PATCH"])
def document_detail(request, document_id: int):
	document = get_object_or_404(Document.objects.select_related("template_revision", "originator"), pk=document_id)

	if request.method == "GET":
		fields = [
			{
				"placeholder_name": f.placeholder_name,
				"value_text": f.value_text,
				"value_json": f.value_json,
			}
			for f in document.fields.order_by("placeholder_name")
		]
		stages = [
			{
				"id": s.id,
				"stage_order": s.stage_order,
				"title": s.title,
				"execution_mode": s.execution_mode,
				"actor_type": s.actor_type,
				"actor_value": s.actor_value,
				"required_action": s.required_action,
				"status": s.status,
				"acted_at": s.acted_at,
				"due_at": s.due_at,
				"comments": s.comments,
			}
			for s in document.workflow_stages.order_by("stage_order", "id")
		]
		qr_token = None
		if hasattr(document, "qr_token"):
			qr_token = document.qr_token.token
		return JsonResponse(
			{
				"id": document.id,
				"title": document.title,
				"subject": document.subject,
				"document_type": document.document_type,
				"status": document.status,
				"classification": document.classification,
				"reference_number": document.reference_number,
				"originator": document.originator.username if document.originator else None,
				"template_revision": document.template_revision.version,
				"submitted_at": document.submitted_at,
				"approved_at": document.approved_at,
				"finalized_at": document.finalized_at,
				"archived_at": document.archived_at,
				"created_at": document.created_at,
				"updated_at": document.updated_at,
				"fields": fields,
				"workflow_stages": stages,
				"qr_token": qr_token,
			}
		)

	# PATCH — update draft fields (title / subject / classification)
	auth_error = _authorize_docgen_action(request, {"docgen.originator", "docgen.admin"})
	if auth_error is not None:
		return auth_error

	if document.status not in {DocumentStatus.DRAFT, DocumentStatus.RETURNED}:
		return JsonResponse(
			{"error": "Only DRAFT or RETURNED documents can be updated."},
			status=400,
		)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	editable = ["title", "subject", "classification"]
	updated = []
	for field_name in editable:
		if field_name in payload:
			setattr(document, field_name, payload[field_name])
			updated.append(field_name)

	if updated:
		document.save(update_fields=[*updated, "updated_at"])

	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
			"updated_fields": updated,
		}
	)


@require_http_methods(["POST"])
def document_withdraw(request, document_id: int):
	auth_error = _authorize_docgen_action(request, {"docgen.originator", "docgen.admin"})
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)

	withdrawable = {
		DocumentStatus.DRAFT,
		DocumentStatus.UNDER_REVIEW,
		DocumentStatus.UNDER_APPROVAL,
		DocumentStatus.RETURNED,
	}
	if document.status not in withdrawable:
		return JsonResponse(
			{"error": f"Cannot withdraw a document in status {document.status}."},
			status=400,
		)

	payload = {}
	if "application/json" in (request.content_type or ""):
		try:
			payload = _parse_json_request(request)
		except ValidationError as error:
			return JsonResponse({"error": str(error)}, status=400)

	comment = payload.get("comment", "")
	actor = request.user if request.user.is_authenticated else None

	document.workflow_stages.filter(
		status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL]
	).update(status=DocumentStatus.WITHDRAWN)

	document.status = DocumentStatus.WITHDRAWN
	_append_document_event(document=document, action="withdraw", actor=actor, comment=comment)
	document.save(update_fields=["status", "metadata", "updated_at"])
	_snapshot_document(document, actor=actor)

	return JsonResponse(
		{
			"document_id": document.id,
			"status": document.status,
		}
	)


# ---------------------------------------------------------------------------
# Document workflow stages list + create
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def document_workflow(request, document_id: int):
	document = get_object_or_404(Document, pk=document_id)

	if request.method == "POST":
		auth_error = _authorize_docgen_action(
			request,
			{"docgen.originator", "docgen.admin"},
		)
		if auth_error is not None:
			return auth_error

		if not _can_edit_document_workflow(document):
			return JsonResponse(
				{"error": "Document workflow can be edited only in DRAFT/RETURNED, or during recovery when no active workflow stage exists."},
				status=400,
			)

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

		if payload.get("replace_existing") is True:
			document.workflow_stages.all().delete()

		mode = payload.get("execution_mode", payload.get("mode", "SEQUENTIAL"))
		try:
			stage = DocumentWorkflowStage(
				document=document,
				stage_order=payload.get("stage_order"),
				title=payload.get("title"),
				execution_mode=mode,
				actor_type=payload.get("actor_type"),
				actor_value=payload.get("actor_value"),
				required_action=payload.get("required_action"),
				status=DocumentStatus.DRAFT,
			)
			stage.full_clean()
			stage.save()
		except (ValidationError, IntegrityError) as error:
			return JsonResponse({"error": str(error)}, status=400)

		return JsonResponse(
			{
				"id": stage.id,
				"document_id": document.id,
				"stage_order": stage.stage_order,
				"title": stage.title,
				"execution_mode": stage.execution_mode,
				"actor_type": stage.actor_type,
				"actor_value": stage.actor_value,
				"required_action": stage.required_action,
				"status": stage.status,
			},
			status=201,
		)

	stages = [
		{
			"id": s.id,
			"stage_order": s.stage_order,
			"title": s.title,
			"execution_mode": s.execution_mode,
			"actor_type": s.actor_type,
			"actor_value": s.actor_value,
			"required_action": s.required_action,
			"status": s.status,
			"acted_by": s.acted_by.username if s.acted_by else None,
			"acted_at": s.acted_at,
			"due_at": s.due_at,
			"reminder_sent_at": s.reminder_sent_at,
			"escalated_at": s.escalated_at,
			"escalation_level": s.escalation_level,
			"comments": s.comments,
		}
		for s in document.workflow_stages.select_related("acted_by").order_by("stage_order", "id")
	]
	return JsonResponse(
		{
			"document_id": document.id,
			"document_status": document.status,
			"count": len(stages),
			"stages": stages,
		}
	)


@require_http_methods(["PATCH", "DELETE"])
def document_workflow_stage_detail(request, document_id: int, stage_id: int):
	auth_error = _authorize_docgen_action(
		request,
		{"docgen.originator", "docgen.admin"},
	)
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	stage = get_object_or_404(DocumentWorkflowStage, pk=stage_id, document=document)

	if not _can_edit_document_workflow(document):
		return JsonResponse(
			{"error": "Document workflow can be edited only in DRAFT/RETURNED, or during recovery when no active workflow stage exists."},
			status=400,
		)

	if request.method == "DELETE":
		stage.delete()
		return JsonResponse({}, status=204)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	for field_name in ["stage_order", "title", "execution_mode", "actor_type", "actor_value", "required_action"]:
		if field_name in payload:
			setattr(stage, field_name, payload[field_name])

	if "mode" in payload:
		stage.execution_mode = payload["mode"]

	try:
		stage.full_clean()
		stage.save()
	except (ValidationError, IntegrityError) as error:
		return JsonResponse({"error": str(error)}, status=400)

	return JsonResponse(
		{
			"id": stage.id,
			"document_id": document.id,
			"stage_order": stage.stage_order,
			"title": stage.title,
			"execution_mode": stage.execution_mode,
			"actor_type": stage.actor_type,
			"actor_value": stage.actor_value,
			"required_action": stage.required_action,
			"status": stage.status,
		}
	)


# ---------------------------------------------------------------------------
# Document PDF list + download
# ---------------------------------------------------------------------------

@require_GET
def document_pdf_download(request, document_id: int, pdf_id: int):
	"""Download a specific finalized PDF artifact."""
	document = get_object_or_404(Document, pk=document_id)
	pdf = get_object_or_404(DocumentPDF, pk=pdf_id, document=document)

	if not pdf.file:
		return JsonResponse({"error": "PDF file not stored on disk."}, status=404)

	import os

	try:
		with pdf.file.open("rb") as fh:
			data = fh.read()
	except OSError:
		return JsonResponse({"error": "PDF file could not be read."}, status=404)

	file_name = (
		f"{document.reference_number or document.id}_v{pdf.version}.pdf"
		.replace("/", "-")
		.replace(" ", "_")
	)
	response = HttpResponse(data, content_type="application/pdf")
	response["Content-Disposition"] = f'attachment; filename="{file_name}"'
	response["Content-Length"] = len(data)
	return response


@require_GET
def document_pdf_list(request, document_id: int):
	document = get_object_or_404(Document, pk=document_id)
	pdfs = [
		{
			"id": p.id,
			"version": p.version,
			"sha256_hash": p.sha256_hash,
			"is_active": p.is_active,
			"generated_by": p.generated_by.username if p.generated_by else None,
			"created_at": p.created_at,
			"file_name": p.file.name if p.file else None,
		}
		for p in document.pdf_versions.select_related("generated_by").order_by("-version")
	]
	return JsonResponse(
		{
			"document_id": document.id,
			"count": len(pdfs),
			"pdfs": pdfs,
		}
	)


# ---------------------------------------------------------------------------
# Document QR info + revocation
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def document_qr_detail(request, document_id: int):
	document = get_object_or_404(Document, pk=document_id)
	try:
		qr = document.qr_token
	except DocumentQRToken.DoesNotExist:
		return JsonResponse({"document_id": document.id, "qr_token": None})

	verify_url = reverse("docgen:verify-token", kwargs={"token": qr.token})
	return JsonResponse(
		{
			"document_id": document.id,
			"token": qr.token,
			"is_revoked": qr.is_revoked,
			"revoked_reason": qr.revoked_reason,
			"revoked_at": qr.revoked_at,
			"verify_url": verify_url,
			"created_at": qr.created_at,
		}
	)


@require_http_methods(["POST"])
def document_qr_revoke(request, document_id: int):
	auth_error = _authorize_docgen_action(request, {"docgen.admin"})
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	try:
		qr = document.qr_token
	except DocumentQRToken.DoesNotExist:
		return JsonResponse({"error": "No QR token found for this document."}, status=404)

	if qr.is_revoked:
		return JsonResponse(
			{
				"document_id": document.id,
				"token": qr.token,
				"is_revoked": True,
				"revoked_reason": qr.revoked_reason,
			}
		)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	reason = payload.get("reason", "")
	qr.is_revoked = True
	qr.revoked_reason = reason
	qr.revoked_at = timezone.now()
	qr.save(update_fields=["is_revoked", "revoked_reason", "revoked_at", "updated_at"])

	_append_document_event(
		document=document,
		action="qr_revoke",
		actor=request.user if request.user.is_authenticated else None,
		comment=reason,
	)
	document.save(update_fields=["metadata", "updated_at"])

	return JsonResponse(
		{
			"document_id": document.id,
			"token": qr.token,
			"is_revoked": True,
			"revoked_reason": qr.revoked_reason,
			"revoked_at": qr.revoked_at,
		}
	)


# ---------------------------------------------------------------------------
# Document supersession
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
def document_supersede(request, document_id: int):
	"""Mark document_id as superseded by a newer finalized document."""
	auth_error = _authorize_docgen_action(request, {"docgen.admin"})
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	if document.status not in {DocumentStatus.FINALIZED, DocumentStatus.ARCHIVED}:
		return JsonResponse(
			{"error": "Only FINALIZED or ARCHIVED documents can be superseded."},
			status=400,
		)

	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	superseding_id = payload.get("superseding_document_id")
	if not superseding_id:
		return JsonResponse({"error": "superseding_document_id is required."}, status=400)

	superseding = get_object_or_404(Document, pk=superseding_id)
	if superseding.status != DocumentStatus.FINALIZED:
		return JsonResponse(
			{"error": "Superseding document must be FINALIZED."},
			status=400,
		)
	if superseding.pk == document.pk:
		return JsonResponse({"error": "A document cannot supersede itself."}, status=400)

	superseding.supersedes = document
	superseding.save(update_fields=["supersedes", "updated_at"])

	actor = request.user if request.user.is_authenticated else None
	_append_document_event(
		document=document,
		action="superseded",
		actor=actor,
		comment=f"Superseded by {superseding.reference_number or superseding.id}",
	)
	document.save(update_fields=["metadata", "updated_at"])

	return JsonResponse(
		{
			"document_id": document.id,
			"superseded_by": superseding.reference_number,
			"superseding_document_id": superseding.id,
		}
	)


# ---------------------------------------------------------------------------
# Improved document list with search / filter
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def document_collection(request):
	if request.method == "GET":
		queryset = Document.objects.select_related("template_revision", "originator").all()

		search = (request.GET.get("q") or "").strip()
		if search:
			queryset = queryset.filter(
				Q(reference_number__icontains=search) | Q(title__icontains=search) | Q(subject__icontains=search)
			)

		status_filter = request.GET.get("status")
		if status_filter:
			queryset = queryset.filter(status=status_filter)

		document_type_filter = request.GET.get("document_type")
		if document_type_filter:
			queryset = queryset.filter(document_type=document_type_filter)

		classification_filter = request.GET.get("classification")
		if classification_filter:
			queryset = queryset.filter(classification=classification_filter)

		originator_filter = request.GET.get("originator")
		if originator_filter:
			queryset = queryset.filter(originator__username=originator_filter)

		originator_id_filter = request.GET.get("originator_id")
		if originator_id_filter:
			try:
				queryset = queryset.filter(originator_id=int(originator_id_filter))
			except (ValueError, TypeError):
				return JsonResponse({"error": "originator_id must be an integer."}, status=400)

		# Support both submitted_after/submitted_before and from_date/to_date aliases.
		submitted_after_raw = request.GET.get("submitted_after") or request.GET.get("from_date")
		if submitted_after_raw:
			parsed = parse_datetime(submitted_after_raw)
			if parsed is None:
				return JsonResponse({"error": "submitted_after/from_date must be a valid ISO datetime."}, status=400)
			queryset = queryset.filter(submitted_at__gte=parsed)

		submitted_before_raw = request.GET.get("submitted_before") or request.GET.get("to_date")
		if submitted_before_raw:
			parsed = parse_datetime(submitted_before_raw)
			if parsed is None:
				return JsonResponse({"error": "submitted_before/to_date must be a valid ISO datetime."}, status=400)
			queryset = queryset.filter(submitted_at__lte=parsed)

		limit = _coerce_positive_int(request.GET.get("limit"), default=50, max_value=200)
		documents = list(queryset.order_by("-created_at")[:limit])

		results = [
			{
				"id": doc.id,
				"title": doc.title,
				"subject": doc.subject,
				"document_type": doc.document_type,
				"classification": doc.classification,
				"status": doc.status,
				"reference_number": doc.reference_number,
				"originator": doc.originator.username if doc.originator else None,
				"originator_name": (
					doc.originator.get_full_name() or doc.originator.username
					if doc.originator else None
				),
				"template_revision": doc.template_revision.version,
				"submitted_at": doc.submitted_at,
				"created_at": doc.created_at,
			}
			for doc in documents
		]
		return JsonResponse({"count": len(results), "results": results})

	# POST — create document
	try:
		auth_error = _authorize_docgen_action(
			request,
			{"docgen.originator", "docgen.admin"},
		)
		if auth_error is not None:
			return auth_error

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
	emit_notification_event(
		event_type="docgen.document.create",
		payload={
			"document_id": document.id,
			"document_type": document.document_type,
			"status": document.status,
			"title": document.title,
			"subject": document.subject,
			"template_revision": document.template_revision.version,
			"actor": request.user.username if request.user.is_authenticated else None,
			"timestamp": timezone.now().isoformat(),
		},
	)

	return JsonResponse(
		{
			"id": document.id,
			"status": document.status,
			"title": document.title,
		},
		status=201,
	)


# ---------------------------------------------------------------------------
# Reporting / dashboard APIs
# ---------------------------------------------------------------------------

@require_GET
def report_summary(request):
	"""Document volume grouped by status and document type."""
	from django.db.models import Count

	by_status = {
		row["status"]: row["count"]
		for row in Document.objects.values("status").annotate(count=Count("id"))
	}
	by_type = {
		row["document_type"]: row["count"]
		for row in Document.objects.values("document_type").annotate(count=Count("id"))
	}
	total = Document.objects.count()
	return JsonResponse(
		{
			"total": total,
			"by_status": by_status,
			"by_document_type": by_type,
		}
	)


@require_GET
def report_sla(request):
	"""SLA compliance stats across workflow stages."""
	qs = DocumentWorkflowStage.objects.filter(due_at__isnull=False)
	total = qs.count()
	now = timezone.now()
	overdue = qs.filter(due_at__lt=now, acted_at__isnull=True).count()
	escalated = qs.filter(escalation_level__gt=0).count()

	# breach rate
	breach_rate = round(overdue / total * 100, 1) if total else 0.0

	return JsonResponse(
		{
			"total_stages_with_sla": total,
			"overdue": overdue,
			"escalated": escalated,
			"breach_rate_percent": breach_rate,
		}
	)


@require_GET
def report_pending(request):
	"""Pending actions grouped by actor_value — useful for a dashboard inbox widget."""
	from django.db.models import Count

	pending_statuses = [DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL]
	rows = (
		DocumentWorkflowStage.objects.filter(status__in=pending_statuses)
		.values("actor_value", "actor_type")
		.annotate(pending_count=Count("id"))
		.order_by("-pending_count")
	)
	results = [
		{
			"actor_value": row["actor_value"],
			"actor_type": row["actor_type"],
			"pending_count": row["pending_count"],
		}
		for row in rows
	]
	total_pending = sum(r["pending_count"] for r in results)
	return JsonResponse(
		{
			"total_pending": total_pending,
			"by_actor": results,
		}
	)


# ---------------------------------------------------------------------------
# Document Comments
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def document_comments(request, document_id: int):
	"""List or create comments on a document."""
	document = get_object_or_404(Document, pk=document_id)

	if request.method == "GET":
		stage_id = request.GET.get("stage_id")
		qs = document.comments.select_related("author", "stage", "parent").order_by("created_at")
		if stage_id is not None:
			qs = qs.filter(stage_id=stage_id)

		def _serialize(c):
			return {
				"id": c.id,
				"body": c.body,
				"author": c.author.username if c.author else None,
				"stage_id": c.stage_id,
				"parent_id": c.parent_id,
				"is_internal": c.is_internal,
				"created_at": c.created_at,
				"updated_at": c.updated_at,
			}

		results = [_serialize(c) for c in qs]
		return JsonResponse({"document_id": document.id, "count": len(results), "results": results})

	# POST — add a new comment
	try:
		payload = _parse_json_request(request)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=400)

	body = (payload.get("body") or "").strip()
	if not body:
		return JsonResponse({"error": "body is required."}, status=400)

	stage_id = payload.get("stage_id")
	parent_id = payload.get("parent_id")
	is_internal = bool(payload.get("is_internal", False))

	stage = None
	if stage_id is not None:
		stage = get_object_or_404(DocumentWorkflowStage, pk=stage_id, document=document)

	parent = None
	if parent_id is not None:
		parent = get_object_or_404(DocumentComment, pk=parent_id, document=document)

	comment = DocumentComment.objects.create(
		document=document,
		stage=stage,
		author=request.user if request.user.is_authenticated else None,
		parent=parent,
		body=body,
		is_internal=is_internal,
	)

	return JsonResponse(
		{
			"id": comment.id,
			"document_id": document.id,
			"body": comment.body,
			"author": comment.author.username if comment.author else None,
			"stage_id": comment.stage_id,
			"parent_id": comment.parent_id,
			"is_internal": comment.is_internal,
			"created_at": comment.created_at,
		},
		status=201,
	)


# ---------------------------------------------------------------------------
# Document draft PDF preview
# ---------------------------------------------------------------------------

@require_GET
def document_preview(request, document_id: int):
	"""Generate and stream a DRAFT-watermarked PDF for any document status."""
	document = get_object_or_404(Document, pk=document_id)
	try:
		pdf_bytes = generate_pdf_preview(document)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=500)

	safe_ref = (document.reference_number or str(document.pk)).replace("/", "_")
	filename = f"DRAFT_{safe_ref}.pdf"
	response = HttpResponse(pdf_bytes, content_type="application/pdf")
	response["Content-Disposition"] = f'inline; filename="{filename}"'
	return response


# ---------------------------------------------------------------------------
# Document Attachments
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
def document_attachments(request, document_id: int):
	"""List or upload attachments for a document."""
	document = get_object_or_404(Document, pk=document_id)

	if request.method == "GET":
		qs = document.attachments.select_related("uploaded_by").order_by("created_at")
		results = [
			{
				"id": a.id,
				"filename": a.filename,
				"file_size": a.file_size,
				"mime_type": a.mime_type,
				"description": a.description,
				"uploaded_by": a.uploaded_by.username if a.uploaded_by else None,
				"created_at": a.created_at,
			}
			for a in qs
		]
		return JsonResponse({"document_id": document.id, "count": len(results), "results": results})

	# POST — upload a file
	uploaded_file = request.FILES.get("file")
	if not uploaded_file:
		return JsonResponse({"error": "file is required as a multipart upload."}, status=400)

	description = request.POST.get("description", "")
	mime_type = uploaded_file.content_type or ""
	filename = uploaded_file.name or "attachment"

	attachment = DocumentAttachment(
		document=document,
		uploaded_by=request.user if request.user.is_authenticated else None,
		filename=filename,
		file_size=uploaded_file.size,
		mime_type=mime_type,
		description=description,
	)
	attachment.file.save(filename, uploaded_file, save=False)
	attachment.save()

	return JsonResponse(
		{
			"id": attachment.id,
			"document_id": document.id,
			"filename": attachment.filename,
			"file_size": attachment.file_size,
			"mime_type": attachment.mime_type,
		},
		status=201,
	)


@require_http_methods(["DELETE"])
def document_attachment_detail(request, document_id: int, attachment_id: int):
	"""Delete a single attachment."""
	document = get_object_or_404(Document, pk=document_id)
	attachment = get_object_or_404(DocumentAttachment, pk=attachment_id, document=document)
	attachment.file.delete(save=False)
	attachment.delete()
	return JsonResponse({}, status=204)


# ---------------------------------------------------------------------------
# On-demand PDF re-generation
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
def document_pdf_generate(request, document_id: int):
	"""Admin-only: regenerate the PDF for any finalized document."""
	auth_error = _authorize_docgen_action(request, {"docgen.admin"})
	if auth_error is not None:
		return auth_error

	document = get_object_or_404(Document, pk=document_id)
	if document.status not in {DocumentStatus.FINALIZED, DocumentStatus.ARCHIVED}:
		return JsonResponse(
			{"error": "Only FINALIZED or ARCHIVED documents can have their PDF regenerated."},
			status=400,
		)

	try:
		pdf = generate_pdf_artifact(
			document=document,
			generated_by=request.user if request.user.is_authenticated else None,
		)
	except ValidationError as error:
		return JsonResponse({"error": str(error)}, status=500)

	return JsonResponse(
		{
			"document_id": document.id,
			"pdf_version": pdf.version,
			"pdf_sha256": pdf.sha256_hash,
			"is_active": pdf.is_active,
		},
		status=201,
	)


# ---------------------------------------------------------------------------
# Dashboard widget endpoints
# ---------------------------------------------------------------------------

@require_GET
def widget_pending_approvals(request):
	"""Return pending approval stages for the authenticated user (or global count for admins)."""
	if not request.user.is_authenticated:
		return JsonResponse({"error": "Authentication required."}, status=401)

	active_stages = DocumentWorkflowStage.objects.select_related("document").filter(
		status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL],
		document__status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL],
	)

	is_admin = _has_docgen_role(request.user, {"docgen.admin"})
	if not is_admin:
		# Filter to stages where this user is the assigned actor.
		username = request.user.username
		active_stages = active_stages.filter(
			Q(actor_type="USER", actor_value=username)
		)

	limit = _coerce_positive_int(request.GET.get("limit"), default=20, max_value=100)
	stages = list(active_stages.order_by("due_at", "id")[:limit])

	results = [
		{
			"stage_id": stage.id,
			"document_id": stage.document_id,
			"reference_number": stage.document.reference_number,
			"document_title": stage.document.title,
			"document_type": stage.document.document_type,
			"stage_title": stage.title,
			"required_action": stage.required_action,
			"actor_value": stage.actor_value,
			"due_at": stage.due_at,
			"status": stage.status,
		}
		for stage in stages
	]
	return JsonResponse({"count": len(results), "results": results})


@require_GET
def widget_recent_documents(request):
	"""Return recently created/updated documents for the authenticated user."""
	if not request.user.is_authenticated:
		return JsonResponse({"error": "Authentication required."}, status=401)

	queryset = Document.objects.select_related("originator").all()

	is_admin = _has_docgen_role(request.user, {"docgen.admin"})
	if not is_admin:
		# Show documents originated by this user or involving them in a workflow stage.
		queryset = queryset.filter(
			Q(originator=request.user) |
			Q(workflow_stages__actor_type="USER", workflow_stages__actor_value=request.user.username)
		).distinct()

	limit = _coerce_positive_int(request.GET.get("limit"), default=10, max_value=50)
	documents = list(queryset.order_by("-updated_at")[:limit])

	results = [
		{
			"id": doc.id,
			"title": doc.title,
			"document_type": doc.document_type,
			"status": doc.status,
			"reference_number": doc.reference_number,
			"originator": doc.originator.username if doc.originator else None,
			"updated_at": doc.updated_at,
		}
		for doc in documents
	]
	return JsonResponse({"count": len(results), "results": results})

