"""
DocGen UI Views — server-rendered Django template views.

URL namespace: docgen:ui-*
Base prefix:   /compass/docgen/ui/
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

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
    TemplatePlaceholder,
    TemplateStatus,
    TemplateWorkflowStage,
    WorkflowActionType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_choices():
    return [s.value for s in DocumentStatus]


def _type_choices():
    return [t.value for t in DocumentType]


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
                queryset=TemplateRevision.objects.filter(status="PUBLISHED").order_by("-version"),
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
        .filter(template_revision=document.template_revision)
        .order_by("display_order")
    )

    # Current field values keyed by placeholder name
    fields_map = {
        f.placeholder_name: f.value_text or ""
        for f in document.fields.all()
    }
    fields_json = json.dumps(fields_map)

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
            "id", "original_name", "file_size", "uploaded_by__username", "created_at",
        )
    )
    for a in attachments:
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
        })

    placeholders = list(
        TemplatePlaceholder.objects
        .filter(template_revision=current_revision)
        .order_by("display_order")
    )
    stages = list(
        TemplateWorkflowStage.objects
        .filter(template_revision=current_revision)
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
