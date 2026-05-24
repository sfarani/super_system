"""
DocGen UI Views — server-rendered Django template views.

URL namespace: docgen:ui-*
Base prefix:   /compass/docgen/ui/
"""
from __future__ import annotations

import csv
import json
from io import StringIO

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .services import resolve_registry_template_tokens
from .models import (
    Document,
    DocumentAttachment,
    DocumentComment,
    DocumentPDF,
    DocumentStatus,
    DocumentType,
    DocumentWorkflowStage,
    PlaceholderType,
    Template,
    TemplateCategory,
    TemplateRevision,
    TemplateTokenAuditAction,
    TemplateTokenAuditLog,
    TemplateTokenDefinition,
    TemplateTokenValue,
    TemplatePlaceholder,
    TemplateStatus,
    TemplateWorkflowStage,
    TokenScope,
    WorkflowActionType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_choices():
    return [s.value for s in DocumentStatus]


def _type_choices():
    return [t.value for t in DocumentType]


def _optional_logo_options() -> list[dict[str, str]]:
    """Return configured right-side letterhead logos for the builder dropdown."""
    raw = getattr(settings, "DOCGEN_OPTIONAL_LOGOS", {})
    options: list[dict[str, str]] = [{"key": "", "label": "None", "url": ""}]

    if not isinstance(raw, dict):
        return options

    for key, entry in raw.items():
        key_str = str(key or "").strip()
        if not key_str:
            continue

        if isinstance(entry, dict):
            label = str(entry.get("label") or key_str).strip()
            url = str(entry.get("url") or "").strip()
        else:
            label = key_str
            url = str(entry or "").strip()

        if not url:
            continue

        options.append({"key": key_str, "label": label, "url": url})

    return options


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    from django.db.models import Count

    # Summary stats
    total = Document.objects.count()
    pending = Document.objects.filter(
        status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL]
    ).count()
    finalized = Document.objects.filter(status=DocumentStatus.FINALIZED).count()

    # SLA stats
    sla_qs = DocumentWorkflowStage.objects.filter(due_at__isnull=False)
    sla_total = sla_qs.count()
    now = timezone.now()
    sla_overdue = sla_qs.filter(due_at__lt=now, acted_at__isnull=True).count()
    sla_escalated = sla_qs.filter(escalation_level__gt=0).count()
    breach_rate = round(sla_overdue / sla_total * 100, 1) if sla_total else 0.0

    # Pending workflow stages (most recent 20)
    pending_stages = (
        DocumentWorkflowStage.objects
        .filter(status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL])
        .select_related("document")
        .order_by("-created_at")[:20]
    )
    pending_stage_data = [
        {
            "document_id": s.document_id,
            "document_ref": s.document.reference_number or f"#{s.document_id}",
            "title": s.title,
            "actor_value": s.actor_value,
            "status": s.status,
        }
        for s in pending_stages
    ]

    # Recent documents
    recent_docs = list(
        Document.objects
        .select_related("originator")
        .values("id", "title", "subject", "reference_number", "status", "document_type",
                "originator__username", "created_at")
        .order_by("-created_at")[:10]
    )

    # Status breakdown with percentage
    by_status = list(
        Document.objects.values("status").annotate(count=Count("id")).order_by("-count")
    )
    for item in by_status:
        item["pct"] = round(item["count"] / total * 100, 1) if total else 0

    return render(request, "docgen/dashboard.html", {
        "stats": {
            "total": total,
            "pending": pending,
            "finalized": finalized,
        },
        "sla": {
            "total_stages_with_sla": sla_total,
            "overdue": sla_overdue,
            "escalated": sla_escalated,
            "breach_rate_percent": breach_rate,
        },
        "pending_stages": pending_stage_data,
        "recent_docs": recent_docs,
        "status_breakdown": by_status,
    })


# ---------------------------------------------------------------------------
# Document List
# ---------------------------------------------------------------------------

@login_required
def document_list(request):
    filter_q = request.GET.get("q", "").strip()
    filter_status = request.GET.get("status", "").strip()
    filter_document_type = request.GET.get("document_type", "").strip()

    qs = (
        Document.objects
        .select_related("originator")
        .values("id", "title", "subject", "reference_number", "status", "document_type",
                "originator__username", "created_at")
    )

    if filter_q:
        qs = qs.filter(
            Q(reference_number__icontains=filter_q)
            | Q(title__icontains=filter_q)
            | Q(subject__icontains=filter_q)
        )
    if filter_status:
        qs = qs.filter(status=filter_status)
    if filter_document_type:
        qs = qs.filter(document_type=filter_document_type)

    documents = list(qs.order_by("-created_at")[:200])

    return render(request, "docgen/document_list.html", {
        "documents": documents,
        "filter_q": filter_q,
        "filter_status": filter_status,
        "filter_document_type": filter_document_type,
        "status_choices": _status_choices(),
        "type_choices": _type_choices(),
    })


# ---------------------------------------------------------------------------
# Document Create
# ---------------------------------------------------------------------------

@login_required
def document_create(request):
    # Gather all published templates with their latest published revision id
    templates_qs = (
        Template.objects
        .filter(status=TemplateStatus.PUBLISHED)
        .select_related("category")
        .prefetch_related(
            Prefetch(
                "revisions",
                queryset=TemplateRevision.objects.filter(is_published=True).order_by("-version"),
                to_attr="published_revisions",
            )
        )
        .order_by("title")
    )

    templates_data = []
    for tpl in templates_qs:
        latest_rev = tpl.published_revisions[0] if tpl.published_revisions else None
        templates_data.append({
            "id": tpl.id,
            "title": tpl.title,
            "code": tpl.code,
            "description": tpl.description,
            "category": tpl.category.name if tpl.category else None,
            "latest_published_revision_id": latest_rev.id if latest_rev else None,
        })

    return render(request, "docgen/document_create.html", {
        "templates": templates_data,
        "type_choices": _type_choices(),
        "selected_revision_id": None,
        "template_id": None,
        "placeholders_json": "[]",
    })


# ---------------------------------------------------------------------------
# Document Detail
# ---------------------------------------------------------------------------

@login_required
def document_detail_ui(request, document_id: int):
    document = get_object_or_404(
        Document.objects.select_related("originator", "template_revision__template"),
        pk=document_id,
    )

    # Placeholders from the template revision
    placeholders = list(
        TemplatePlaceholder.objects
        .filter(revision=document.template_revision)
        .order_by("display_order")
    )

    # Current field values keyed by placeholder name
    fields_map = {
        f.placeholder_name: f.value_text or ""
        for f in document.fields.all()
    }
    fields_json = json.dumps(fields_map)
    metadata = document.metadata if isinstance(document.metadata, dict) else {}
    template_tokens_map = metadata.get("template_tokens") if isinstance(metadata.get("template_tokens"), dict) else {}
    template_tokens_json = json.dumps(template_tokens_map)
    inherited_tokens_map, inherited_sources_map = resolve_registry_template_tokens(document)
    inherited_tokens_json = json.dumps(inherited_tokens_map)
    inherited_token_sources_json = json.dumps(inherited_sources_map)

    # Workflow stages
    workflow_stages = list(
        document.workflow_stages
        .select_related("acted_by")
        .order_by("stage_order", "id")
    )
    wf_data = [
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
            "comments": s.comments,
            "due_at": s.due_at,
            "escalated_at": s.escalated_at,
        }
        for s in workflow_stages
    ]

    # Timeline events from metadata
    timeline_events = []
    if isinstance(document.metadata, dict):
        for ev in document.metadata.get("events", []):
            timeline_events.append(ev)

    # Comments
    comments = list(
        document.comments.select_related("author").order_by("created_at").values(
            "id", "body", "author__username", "stage_id", "parent_id",
            "is_internal", "created_at", "updated_at",
        )
    )
    for c in comments:
        c["author"] = c.pop("author__username")
        c["created_at"] = c["created_at"].isoformat() if c["created_at"] else None
        c["updated_at"] = c["updated_at"].isoformat() if c["updated_at"] else None
    comments_json = json.dumps(comments)

    # Attachments
    attachments = list(
        document.attachments.select_related("uploaded_by").values(
            "id", "filename", "file_size", "uploaded_by__username", "created_at",
        )
    )
    for a in attachments:
        # Keep `original_name` for existing frontend bindings.
        a["original_name"] = a.get("filename")
        a["uploaded_by"] = a.pop("uploaded_by__username")
        a["created_at"] = a["created_at"].isoformat() if a["created_at"] else None
    attachments_json = json.dumps(attachments)

    # PDF list
    pdf_list = list(
        document.pdf_versions.select_related("generated_by").order_by("-version")
    )

    # Available workflow actions based on status
    status = document.status
    available_actions = []
    if status in (DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL):
        available_actions = [
            a.value for a in WorkflowActionType
            if a.value not in ("REJECT",)  # reject shown separately
        ]
        available_actions.append("reject")

    # Determine which tabs to show
    tabs = [
        ("details", "Details"),
        ("workflow", "Workflow"),
        ("timeline", "Timeline"),
        ("comments", "Comments"),
        ("attachments", "Attachments"),
    ]
    if pdf_list:
        tabs.append(("pdfs", "PDFs"))

    return render(request, "docgen/document_detail.html", {
        "document": document,
        "placeholders": placeholders,
        "fields_json": fields_json,
        "template_tokens_json": template_tokens_json,
        "inherited_tokens_json": inherited_tokens_json,
        "inherited_token_sources_json": inherited_token_sources_json,
        "workflow_stages": wf_data,
        "timeline_events": timeline_events,
        "comments_json": comments_json,
        "attachments_json": attachments_json,
        "pdf_list": pdf_list,
        "available_actions": available_actions,
        "tabs": tabs,
    })


# ---------------------------------------------------------------------------
# Template List
# ---------------------------------------------------------------------------

@login_required
def template_list(request):
    templates_qs = (
        Template.objects
        .select_related("category")
        .annotate(revision_count=Count("revisions"))
        .order_by("-created_at")
    )

    templates_data = []
    for tpl in templates_qs:
        templates_data.append({
            "id": tpl.id,
            "title": tpl.title,
            "code": tpl.code,
            "description": tpl.description,
            "status": tpl.status,
            "category": tpl.category.name if tpl.category else None,
            "revision_count": tpl.revision_count,
        })

    categories = list(TemplateCategory.objects.order_by("name").values("id", "name"))

    return render(request, "docgen/template_list.html", {
        "templates": templates_data,
        "categories": categories,
    })


# ---------------------------------------------------------------------------
# Central Token Registry (global/department/user values)
# ---------------------------------------------------------------------------

@login_required
def token_registry(request):
    can_manage_group_tokens = (
        request.user.is_superuser
        or request.user.is_staff
        or request.user.has_perm(f"{Template._meta.app_label}.admin")
    )
    groups = list(request.user.groups.order_by("name"))

    selected_group = None
    selected_group_id = request.GET.get("group_id") or request.POST.get("group_id")
    if can_manage_group_tokens and groups:
        if selected_group_id:
            try:
                selected_group = next(g for g in groups if g.id == int(selected_group_id))
            except (StopIteration, ValueError, TypeError):
                selected_group = groups[0]
        else:
            selected_group = groups[0]

    definitions = list(TemplateTokenDefinition.objects.filter(is_active=True).order_by("key"))
    def_ids = [d.id for d in definitions]
    definitions_by_key = {d.key: d for d in definitions}

    def _apply_token_value(*, definition, scope, value, actor, source, group=None, user=None):
        lookup = {
            "definition": definition,
            "scope": scope,
        }
        if scope == TokenScope.GROUP:
            lookup["group"] = group
        if scope == TokenScope.USER:
            lookup["user"] = user

        existing = TemplateTokenValue.objects.filter(**lookup).first()
        normalized = (value or "").strip()
        old_value = existing.value if existing else ""

        if not normalized:
            if existing is not None:
                existing.delete()
                TemplateTokenAuditLog.objects.create(
                    definition=definition,
                    scope=scope,
                    group=group if scope == TokenScope.GROUP else None,
                    user=user if scope == TokenScope.USER else None,
                    actor=actor,
                    action=TemplateTokenAuditAction.DELETE,
                    old_value=old_value,
                    new_value="",
                    source=source,
                )
            return

        if existing is None:
            TemplateTokenValue.objects.create(value=normalized, **lookup)
            TemplateTokenAuditLog.objects.create(
                definition=definition,
                scope=scope,
                group=group if scope == TokenScope.GROUP else None,
                user=user if scope == TokenScope.USER else None,
                actor=actor,
                action=TemplateTokenAuditAction.CREATE,
                old_value="",
                new_value=normalized,
                source=source,
            )
            return

        if old_value != normalized:
            existing.value = normalized
            existing.save(update_fields=["value", "updated_at"])
            TemplateTokenAuditLog.objects.create(
                definition=definition,
                scope=scope,
                group=group if scope == TokenScope.GROUP else None,
                user=user if scope == TokenScope.USER else None,
                actor=actor,
                action=TemplateTokenAuditAction.UPDATE,
                old_value=old_value,
                new_value=normalized,
                source=source,
            )

    if request.method == "GET" and request.GET.get("export") == "csv":
        rows = []
        global_values = TemplateTokenValue.objects.filter(definition_id__in=def_ids, scope=TokenScope.GLOBAL)
        for row in global_values.select_related("definition"):
            rows.append({
                "key": row.definition.key,
                "scope": "GLOBAL",
                "target": "",
                "value": row.value,
            })

        if selected_group is not None:
            group_values = TemplateTokenValue.objects.filter(
                definition_id__in=def_ids,
                scope=TokenScope.GROUP,
                group=selected_group,
            )
            for row in group_values.select_related("definition", "group"):
                rows.append({
                    "key": row.definition.key,
                    "scope": "GROUP",
                    "target": row.group.name if row.group else "",
                    "value": row.value,
                })

        user_values = TemplateTokenValue.objects.filter(
            definition_id__in=def_ids,
            scope=TokenScope.USER,
            user=request.user,
        )
        for row in user_values.select_related("definition", "user"):
            rows.append({
                "key": row.definition.key,
                "scope": "USER",
                "target": row.user.username if row.user else "",
                "value": row.value,
            })

        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=["key", "scope", "target", "value"])
        writer.writeheader()
        writer.writerows(rows)
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="docgen_token_registry.csv"'
        return response

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "import_csv":
            upload = request.FILES.get("csv_file")
            if upload is None:
                messages.error(request, "Please choose a CSV file to import.")
                return redirect(reverse("docgen:ui-token-registry"))

            try:
                decoded = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                messages.error(request, "CSV must be UTF-8 encoded.")
                return redirect(reverse("docgen:ui-token-registry"))

            reader = csv.DictReader(StringIO(decoded))
            required_cols = {"key", "scope", "target", "value"}
            if not reader.fieldnames or not required_cols.issubset(set(reader.fieldnames)):
                messages.error(request, "CSV headers must include: key,scope,target,value")
                return redirect(reverse("docgen:ui-token-registry"))

            applied = 0
            errors: list[str] = []
            for idx, row in enumerate(reader, start=2):
                key = (row.get("key") or "").strip()
                raw_scope = (row.get("scope") or "").strip().upper()
                target = (row.get("target") or "").strip()
                value = (row.get("value") or "").strip()
                scope = "GROUP" if raw_scope == "DEPARTMENT" else raw_scope

                if not key or scope not in {"GLOBAL", "GROUP", "USER"}:
                    errors.append(f"Row {idx}: invalid key/scope.")
                    continue
                definition = definitions_by_key.get(key)
                if definition is None:
                    errors.append(f"Row {idx}: unknown token key '{key}'.")
                    continue

                if scope == "GLOBAL":
                    if not can_manage_group_tokens:
                        errors.append(f"Row {idx}: not allowed to import GLOBAL values.")
                        continue
                    _apply_token_value(
                        definition=definition,
                        scope=TokenScope.GLOBAL,
                        value=value,
                        actor=request.user,
                        source="ui_csv_import",
                    )
                    applied += 1
                    continue

                if scope == "GROUP":
                    if not can_manage_group_tokens or selected_group is None:
                        errors.append(f"Row {idx}: no department scope selected.")
                        continue
                    if target and selected_group and target != selected_group.name:
                        errors.append(f"Row {idx}: target '{target}' does not match selected department '{selected_group.name}'.")
                        continue
                    _apply_token_value(
                        definition=definition,
                        scope=TokenScope.GROUP,
                        group=selected_group,
                        value=value,
                        actor=request.user,
                        source="ui_csv_import",
                    )
                    applied += 1
                    continue

                if scope == "USER":
                    if target and target != request.user.username:
                        errors.append(f"Row {idx}: USER target must be '{request.user.username}' or blank.")
                        continue
                    _apply_token_value(
                        definition=definition,
                        scope=TokenScope.USER,
                        user=request.user,
                        value=value,
                        actor=request.user,
                        source="ui_csv_import",
                    )
                    applied += 1

            if applied:
                messages.success(request, f"Imported {applied} token rows.")
            if errors:
                messages.error(request, "Import issues: " + " ".join(errors[:5]))
            query = ""
            if selected_group is not None:
                query = f"?group_id={selected_group.id}"
            return redirect(reverse("docgen:ui-token-registry") + query)

        # Save user-scoped token values.
        for definition in definitions:
            field_name = f"user_{definition.id}"
            value = (request.POST.get(field_name) or "").strip()
            _apply_token_value(
                definition=definition,
                scope=TokenScope.USER,
                user=request.user,
                value=value,
                actor=request.user,
                source="ui_form",
            )

        # Save selected group-scoped token values (if allowed).
        if can_manage_group_tokens and selected_group is not None:
            for definition in definitions:
                field_name = f"group_{definition.id}"
                value = (request.POST.get(field_name) or "").strip()
                _apply_token_value(
                    definition=definition,
                    scope=TokenScope.GROUP,
                    group=selected_group,
                    value=value,
                    actor=request.user,
                    source="ui_form",
                )

        messages.success(request, "Token values saved successfully.")

        redirect_url = reverse("docgen:ui-token-registry")
        if selected_group is not None:
            return redirect(f"{redirect_url}?saved=1&group_id={selected_group.id}")
        return redirect(f"{redirect_url}?saved=1")

    global_values = {
        row["definition_id"]: row["value"]
        for row in TemplateTokenValue.objects.filter(
            definition_id__in=def_ids,
            scope=TokenScope.GLOBAL,
        ).values("definition_id", "value")
    }
    user_values = {
        row["definition_id"]: row["value"]
        for row in TemplateTokenValue.objects.filter(
            definition_id__in=def_ids,
            scope=TokenScope.USER,
            user=request.user,
        ).values("definition_id", "value")
    }
    group_values = {}
    if selected_group is not None:
        group_values = {
            row["definition_id"]: row["value"]
            for row in TemplateTokenValue.objects.filter(
                definition_id__in=def_ids,
                scope=TokenScope.GROUP,
                group=selected_group,
            ).values("definition_id", "value")
        }

    token_rows = [
        {
            "id": definition.id,
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "global_value": global_values.get(definition.id, ""),
            "group_value": group_values.get(definition.id, ""),
            "user_value": user_values.get(definition.id, ""),
        }
        for definition in definitions
    ]

    recent_audit_logs = list(
        TemplateTokenAuditLog.objects.select_related("definition", "group", "user", "actor")[:30]
    )

    return render(request, "docgen/token_registry.html", {
        "token_rows": token_rows,
        "groups": groups,
        "selected_group": selected_group,
        "can_manage_group_tokens": can_manage_group_tokens,
        "saved": request.GET.get("saved") == "1",
        "recent_audit_logs": recent_audit_logs,
    })


@login_required
def token_registry_sample_csv(request):
    """Download a prefilled CSV template for token registry import."""
    groups = list(request.user.groups.order_by("name"))
    selected_group_id = request.GET.get("group_id")
    selected_group = None
    if groups:
        if selected_group_id:
            try:
                selected_group = next(g for g in groups if g.id == int(selected_group_id))
            except (StopIteration, ValueError, TypeError):
                selected_group = groups[0]
        else:
            selected_group = groups[0]

    definitions = list(TemplateTokenDefinition.objects.filter(is_active=True).order_by("key"))

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["key", "scope", "target", "value"])
    writer.writeheader()

    # Prefill rows to reduce mistakes. Keep value empty so user fills it in.
    for definition in definitions:
        writer.writerow({
            "key": definition.key,
            "scope": "USER",
            "target": request.user.username,
            "value": "",
        })
        if selected_group is not None:
            writer.writerow({
                "key": definition.key,
                "scope": "GROUP",
                "target": selected_group.name,
                "value": "",
            })

    # If no token definitions exist yet, include one instructional row.
    if not definitions:
        writer.writerow({
            "key": "department",
            "scope": "GROUP",
            "target": selected_group.name if selected_group is not None else "Your Department",
            "value": "",
        })

    response = HttpResponse(output.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="docgen_token_registry_sample.csv"'
    return response


# ---------------------------------------------------------------------------
# Template Detail (manage placeholders + workflow stages)
# ---------------------------------------------------------------------------

@login_required
def template_detail(request, template_id: int):
    template = get_object_or_404(
        Template.objects.select_related("category"),
        pk=template_id,
    )

    # Get all revisions; pick the latest published one as "current"
    revisions = list(
        template.revisions.order_by("-version")
    )
    current_revision = next(
        (r for r in revisions if r.is_published),
        revisions[0] if revisions else None,
    )

    if current_revision is None:
        return render(request, "docgen/template_detail.html", {
            "template": template,
            "current_revision": None,
            "placeholders": [],
            "stages": [],
            "revisions": revisions,
            "field_type_choices": [ft.value for ft in PlaceholderType],
            "action_choices": [a.value for a in WorkflowActionType],
            "optional_logo_options": _optional_logo_options(),
        })

    placeholders = list(
        TemplatePlaceholder.objects
        .filter(revision=current_revision)
        .order_by("display_order")
    )
    stages = list(
        TemplateWorkflowStage.objects
        .filter(revision=current_revision)
        .order_by("stage_order")
    )

    return render(request, "docgen/template_detail.html", {
        "template": template,
        "current_revision": current_revision,
        "placeholders": placeholders,
        "stages": stages,
        "revisions": revisions,
        "field_type_choices": [ft.value for ft in PlaceholderType],
        "action_choices": [a.value for a in WorkflowActionType],
        "optional_logo_options": _optional_logo_options(),
    })


# ---------------------------------------------------------------------------
# QR Verification page (public — no login required)
# ---------------------------------------------------------------------------

def verify_page(request, token: str):
    """
    Public verification page rendered from QR scan.

    Reuses the existing verify_token JSON API logic but renders HTML for browsers.
    If the client explicitly requests JSON (Accept: application/json), delegates
    to the original JSON view. This keeps the URL canonical for both uses.
    """
    from django.core import signing
    from .models import DocumentQRToken, DocumentStatus as DS

    accept = request.META.get("HTTP_ACCEPT", "")
    if "application/json" in accept and "text/html" not in accept:
        from .views import verify_token as _json_view
        return _json_view(request, token)

    result: dict = {}
    try:
        payload = signing.loads(token, salt="docgen-verify")
        qr_id = payload.get("qr_id")
        qr = DocumentQRToken.objects.select_related("document").get(pk=qr_id)

        doc = qr.document
        base = {
            "reference_number": doc.reference_number,
            "document_type": doc.document_type,
            "finalized_at": doc.finalized_at.strftime("%d %b %Y %H:%M") if doc.finalized_at else None,
        }

        if qr.is_revoked:
            result = {"valid": False, "status": "revoked", "revoked_reason": qr.revoked_reason, **base}
        elif doc.status in (DS.FINALIZED, DS.ARCHIVED):
            if doc.superseded_by_id:
                superseding = Document.objects.filter(supersedes=doc).first()
                result = {
                    "valid": False,
                    "status": "superseded",
                    "superseded_by": superseding.reference_number if superseding else None,
                    **base,
                }
            else:
                result = {"valid": True, "status": "valid", **base}
        else:
            result = {"valid": False, "status": "revoked", "revoked_reason": "document_not_finalized", **base}

    except signing.BadSignature:
        result = {"valid": False, "status": "tampered"}
    except DocumentQRToken.DoesNotExist:
        result = {"valid": False, "status": "not_found"}

    return render(request, "docgen/verify.html", {
        "result": result,
        "verified_at": timezone.now().strftime("%d %b %Y %H:%M UTC"),
    })
