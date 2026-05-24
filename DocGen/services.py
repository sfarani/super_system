# pyright: reportAttributeAccessIssue=false

import base64
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import OperationalError, ProgrammingError
from django.utils import timezone
from django.utils.html import escape
from django.utils.module_loading import import_string
from datetime import timedelta
from xhtml2pdf import pisa

from .adapters import LocalActorResolutionAdapter
from .models import (
    Document,
    DocumentPDF,
    DocumentStatus,
    DocumentWorkflowStage,
    TemplateTokenDefinition,
    TemplateTokenValue,
    TokenScope,
)
from .notifications import LocalNotificationAdapter


_TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_BRACKET_TOKEN_RE = re.compile(r"\[(\w+)\]")


def _coerce_text_value(value_text, value_json) -> str:
    if value_text not in [None, ""]:
        return str(value_text)
    if value_json in [None, "", {}]:
        return ""
    return json.dumps(value_json, ensure_ascii=True)


def resolve_registry_template_tokens(document: Document) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve centralized template tokens for a document originator.

    Precedence: GLOBAL < GROUP < USER
    Returns: (resolved_values, source_labels)
    """
    try:
        definitions = list(
            TemplateTokenDefinition.objects.filter(is_active=True).values("id", "key")
        )
    except (ProgrammingError, OperationalError):
        return {}, {}
    if not definitions:
        return {}, {}

    def_by_id = {row["id"]: row["key"] for row in definitions}
    def_ids = list(def_by_id.keys())

    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}

    global_rows = (
        TemplateTokenValue.objects
        .filter(definition_id__in=def_ids, scope=TokenScope.GLOBAL)
        .values("definition_id", "value")
    )
    for row in global_rows:
        key = def_by_id.get(row["definition_id"])
        if key:
            resolved[key] = row["value"]
            sources[key] = "Global"

    originator = document.originator
    if originator is not None:
        group_ids = list(originator.groups.values_list("id", flat=True))
        if group_ids:
            group_rows = (
                TemplateTokenValue.objects
                .filter(definition_id__in=def_ids, scope=TokenScope.GROUP, group_id__in=group_ids)
                .values("definition_id", "value", "group__name")
                .order_by("group__name", "group_id", "-updated_at")
            )
            for row in group_rows:
                key = def_by_id.get(row["definition_id"])
                if key:
                    resolved[key] = row["value"]
                    sources[key] = f"Department: {row['group__name'] or 'Group'}"

        user_rows = (
            TemplateTokenValue.objects
            .filter(definition_id__in=def_ids, scope=TokenScope.USER, user=originator)
            .values("definition_id", "value")
        )
        for row in user_rows:
            key = def_by_id.get(row["definition_id"])
            if key:
                resolved[key] = row["value"]
                sources[key] = "User"

    return resolved, sources


def _build_render_context(document: Document, field_map: dict[str, str]) -> dict[str, str]:
    now_local = timezone.localtime(timezone.now())
    submitted_at = timezone.localtime(document.submitted_at).strftime("%d %b %Y") if document.submitted_at else ""
    approved_at = timezone.localtime(document.approved_at).strftime("%d %b %Y") if document.approved_at else ""
    metadata = document.metadata if isinstance(document.metadata, dict) else {}
    custom_tokens = metadata.get("template_tokens") if isinstance(metadata.get("template_tokens"), dict) else {}
    registry_tokens, _ = resolve_registry_template_tokens(document)

    context = {
        "reference_number": document.reference_number or "",
        "date": now_local.strftime("%d %b %Y"),
        "title": document.title or "",
        "subject": document.subject or "",
        "document_type": str(document.document_type or ""),
        "status": str(document.status or ""),
        "originator": document.originator.username if document.originator else "",
        "submitted_date": submitted_at,
        "approval_date": approved_at,
    }
    context.update({str(k): "" if v is None else str(v) for k, v in registry_tokens.items() if str(k).strip()})
    context.update({str(k): "" if v is None else str(v) for k, v in custom_tokens.items() if str(k).strip()})
    context.update(field_map)
    return context


def _extract_layout_tokens(layout_schema: dict) -> set[str]:
    blocks = layout_schema.get("blocks") if isinstance(layout_schema, dict) else None
    if not isinstance(blocks, list):
        return set()

    tokens: set[str] = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = str(block.get("content") or "")
        tokens.update(match.group(1) for match in _TOKEN_RE.finditer(content))
        tokens.update(match.group(1) for match in _BRACKET_TOKEN_RE.finditer(content))
    return tokens


def _find_missing_required_placeholders(document: Document, field_map: dict[str, str]) -> list[str]:
    required_names = list(
        document.template_revision.placeholders.filter(is_required=True).values_list("name", flat=True)
    )
    return sorted(name for name in required_names if not str(field_map.get(name, "")).strip())


def _build_preview_diagnostics_html(missing_required: list[str], unresolved_tokens: list[str]) -> str:
    if not missing_required and not unresolved_tokens:
        return ""

    details: list[str] = []
    if missing_required:
        details.append(
            "<div><strong>Missing required fields:</strong> "
            + escape(", ".join(missing_required))
            + "</div>"
        )
    if unresolved_tokens:
        details.append(
            "<div><strong>Unresolved tokens in layout:</strong> "
            + escape(", ".join(unresolved_tokens))
            + "</div>"
        )

    return (
        "<div style='border:1px solid #f59e0b;background:#fffbeb;color:#92400e;"
        "padding:8px 10px;margin-bottom:12px;font-size:9pt;line-height:1.4;'>"
        "<div style='font-weight:700;margin-bottom:4px;'>Preview Warnings</div>"
        + "".join(details)
        + "</div>"
    )


def _render_template_tokens(raw: str, render_context: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in render_context:
            return render_context[token]
        return match.group(0)

    rendered = _TOKEN_RE.sub(_replace, raw or "")
    # Support builder defaults like [reference_number] and [date].
    return _BRACKET_TOKEN_RE.sub(_replace, rendered)


def _build_layout_blocks_html(document: Document, render_context: dict[str, str]) -> str:
    layout = document.template_revision.layout_schema or {}
    blocks = layout.get("blocks") if isinstance(layout, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return ""

    page = layout.get("page") if isinstance(layout, dict) else {}
    if not isinstance(page, dict):
        page = {}

    def _resolve_logo_src(raw_url: str) -> str:
        value = _render_template_tokens(str(raw_url or ""), render_context).strip()
        if not value or "{{" in value or "}}" in value:
            return ""
        if value.startswith("/static/"):
            rel_path = value[len("/static/"):]
            found_path = finders.find(rel_path)
            if found_path:
                return Path(found_path).resolve().as_uri()
            local_path = Path(settings.BASE_DIR) / value.lstrip("/")
            if local_path.exists():
                return local_path.resolve().as_uri()
        return value

    left_logo_src = _resolve_logo_src("/static/DocGen/pnra_logo.png")

    raw_optional = getattr(settings, "DOCGEN_OPTIONAL_LOGOS", {})
    selected_right_logo_url = ""
    selected_key = str(page.get("logo_right_key") or "").strip()

    if selected_key and isinstance(raw_optional, dict):
        entry = raw_optional.get(selected_key)
        if isinstance(entry, dict):
            selected_right_logo_url = str(entry.get("url") or "").strip()
        elif entry is not None:
            selected_right_logo_url = str(entry).strip()

    # Backward compatibility for already-saved layouts.
    if not selected_right_logo_url:
        selected_right_logo_url = str(page.get("logo_right_url") or "").strip()

    right_logo_src = _resolve_logo_src(selected_right_logo_url)

    def _block_style(block_type: str, align: str, split_mode: bool = False) -> str:
        style = "margin-bottom:10px;white-space:normal;"
        if not split_mode and align in {"left", "center", "right"}:
            style += f"text-align:{align};"
        if block_type == "letterhead":
            style += "font-weight:700;font-size:13pt;line-height:1.35;border-bottom:1px solid #222;padding-bottom:8px;margin-bottom:16px;"
        elif block_type == "reference_line":
            style += "font-size:10pt;color:#333;margin-bottom:8px;"
        elif block_type == "subject_line":
            style += "font-weight:700;text-decoration:underline;margin-top:10px;margin-bottom:12px;"
        elif block_type == "salutation":
            style += "margin-top:8px;margin-bottom:10px;"
        elif block_type == "body":
            style += "line-height:1.65;"
            if not split_mode:
                style += "text-align:justify;"
        elif block_type == "contacts_block":
            style += "font-size:10pt;white-space:nowrap;"
        elif block_type == "closing":
            style += "margin-top:14px;margin-bottom:8px;"
        elif block_type == "signature_block":
            style += "margin-top:26px;line-height:1.4;"
        elif block_type == "footer":
            style += "font-size:9pt;color:#555;border-top:1px solid #ccc;padding-top:6px;margin-top:20px;"
        return style

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "custom")
        align = str(block.get("align") or "left")
        content = _render_template_tokens(str(block.get("content") or ""), render_context)

        if block_type == "letterhead":
            content_html = escape(content).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            left_img_html = (
                f"<img src='{escape(left_logo_src)}' style='height:48px;max-width:140px;' />"
                if left_logo_src
                else ""
            )
            right_img_html = (
                f"<img src='{escape(right_logo_src)}' style='height:48px;max-width:140px;' />"
                if right_logo_src
                else ""
            )
            if right_img_html:
                row_html = (
                    "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                    "<tr>"
                    f"<td style='width:20%;text-align:left;vertical-align:top;'>{left_img_html}</td>"
                    f"<td style='width:60%;text-align:center;vertical-align:top;'>{content_html}</td>"
                    f"<td style='width:20%;text-align:right;vertical-align:top;'>{right_img_html}</td>"
                    "</tr>"
                    "</table>"
                )
            else:
                row_html = (
                    "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                    "<tr>"
                    f"<td style='width:20%;text-align:left;vertical-align:top;'>{left_img_html}</td>"
                    f"<td style='width:80%;text-align:center;vertical-align:top;'>{content_html}</td>"
                    "</tr>"
                    "</table>"
                )
            style = _block_style(block_type, align, split_mode=False)
            parts.append(f"<div style='{style}'>{row_html}</div>")
            continue

        if "||" in content:
            left_raw, right_raw = content.split("||", 1)
            left_html = escape(left_raw.strip()).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            right_html = escape(right_raw.strip()).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            style = _block_style(block_type, align, split_mode=True)
            parts.append(
                f"<div style='{style}'>"
                "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                "<tr>"
                f"<td style='width:50%;text-align:left;vertical-align:top;'>{left_html}</td>"
                f"<td style='width:50%;text-align:right;vertical-align:top;'>{right_html}</td>"
                "</tr>"
                "</table>"
                "</div>"
            )
            continue

        content_html = escape(content).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        style = _block_style(block_type, align, split_mode=False)

        parts.append(f"<div style='{style}'>{content_html}</div>")

    return "".join(parts)


def _resolve_page_setup(document: Document) -> tuple[str, str, int, int, int, int]:
    layout = document.template_revision.layout_schema or {}
    page = layout.get("page") if isinstance(layout, dict) else {}
    if not isinstance(page, dict):
        page = {}

    def _int_or(default: int, value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    paper_size = str(page.get("paper_size") or "A4")
    orientation = str(page.get("orientation") or "portrait")
    margin_top = _int_or(20, page.get("margin_top"))
    margin_bottom = _int_or(20, page.get("margin_bottom"))
    margin_left = _int_or(25, page.get("margin_left"))
    margin_right = _int_or(25, page.get("margin_right"))
    if orientation not in {"portrait", "landscape"}:
        orientation = "portrait"
    return paper_size, orientation, margin_top, margin_right, margin_bottom, margin_left


def _generate_qr_data_uri(data: str) -> str | None:
    """Return a base64 PNG data URI for a QR code, or None if qrcode is not installed."""
    try:
        import qrcode  # type: ignore[import]
        from PIL import Image  # type: ignore[import]
    except ImportError:
        return None
    img = qrcode.make(data)
    buf = BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_pdf_html(document: Document, include_diagnostics: bool = False) -> str:
    field_map = {
        field.placeholder_name: _coerce_text_value(field.value_text, field.value_json)
        for field in document.fields.order_by("placeholder_name")
    }
    render_context = _build_render_context(document, field_map)

    layout_body = _build_layout_blocks_html(document, render_context)
    paper_size, orientation, margin_top, margin_right, margin_bottom, margin_left = _resolve_page_setup(document)
    diagnostics_html = ""

    if include_diagnostics:
        layout_schema = document.template_revision.layout_schema or {}
        layout_tokens = _extract_layout_tokens(layout_schema if isinstance(layout_schema, dict) else {})
        unresolved_tokens = sorted(token for token in layout_tokens if token not in render_context)
        missing_required = _find_missing_required_placeholders(document, field_map)
        diagnostics_html = _build_preview_diagnostics_html(missing_required, unresolved_tokens)

    if layout_body:
        return f"""
<!DOCTYPE html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <style>
            @page {{ size: {escape(paper_size)} {escape(orientation)}; margin: {margin_top}mm {margin_right}mm {margin_bottom}mm {margin_left}mm; }}
            body {{ margin: 0; font-family: Helvetica, Arial, sans-serif; font-size: 11pt; color: #111; line-height: 1.5; }}
        </style>
    </head>
    <body>
        {diagnostics_html}
        {layout_body}
    </body>
</html>
""".strip()

    fields = [
        {
            "placeholder_name": field.placeholder_name,
            "value_text": field.value_text,
            "value_json": field.value_json,
        }
        for field in document.fields.order_by("placeholder_name")
    ]
    rows = []
    for field in fields:
        value_text = field.get("value_text")
        value_json = field.get("value_json")
        rendered_value = value_text if value_text not in [None, ""] else json.dumps(value_json, ensure_ascii=True)
        rows.append(
            "<tr>"
            f"<td>{escape(field.get('placeholder_name') or '')}</td>"
            f"<td>{escape(rendered_value)}</td>"
            "</tr>"
        )

    events = (document.metadata or {}).get("events", [])
    event_items = [
        "<li>"
        f"{escape(event.get('timestamp', ''))}"
        f" - {escape(event.get('action', ''))}"
        f" ({escape(event.get('actor', 'system') or 'system')})"
        "</li>"
        for event in events[-10:]
    ]

    finalized_at = document.finalized_at.isoformat() if document.finalized_at else ""

    # QR code block — embed image if token exists and qrcode library is available
    qr_html = ""
    try:
        qr_token = document.qr_token  # OneToOne; raises if absent
        verify_path = f"/compass/docgen/verify/{qr_token.token}/"
        verify_url = getattr(settings, "DOCGEN_PUBLIC_BASE_URL", "").rstrip("/") + verify_path
        qr_data_uri = _generate_qr_data_uri(verify_url)
        if qr_data_uri:
            qr_html = (
                "<div style='margin-top:24px;text-align:center;'>"
                f"<img src='{qr_data_uri}' width='100' height='100' />"
                "<br/><span style='font-size:8pt;color:#555;'>"
                f"Scan to verify: {escape(verify_url)}"
                "</span></div>"
            )
        else:
            qr_html = f"<p class='small'>Verify: {escape(verify_url)}</p>"
    except Exception:
        pass

    return f"""
<!DOCTYPE html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <style>
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; color: #111; }}
            h1 {{ font-size: 18pt; margin-bottom: 4px; }}
            h2 {{ font-size: 12pt; margin-top: 18px; margin-bottom: 6px; }}
            .meta {{ margin: 2px 0; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th, td {{ border: 1px solid #bbb; padding: 6px; text-align: left; vertical-align: top; }}
            th {{ background: #f3f3f3; }}
            .small {{ color: #666; font-size: 9pt; }}
        </style>
    </head>
    <body>
        <h1>{escape(document.title)}</h1>
        <div class=\"meta\"><strong>Reference:</strong> {escape(document.reference_number or '')}</div>
        <div class=\"meta\"><strong>Type:</strong> {escape(document.document_type)}</div>
        <div class=\"meta\"><strong>Subject:</strong> {escape(document.subject or '')}</div>
        <div class=\"meta\"><strong>Status:</strong> {escape(document.status)}</div>
        <div class=\"meta\"><strong>Finalized At:</strong> {escape(finalized_at)}</div>

        <h2>Field Values</h2>
        <table>
            <thead>
                <tr><th>Placeholder</th><th>Value</th></tr>
            </thead>
            <tbody>
                {''.join(rows) if rows else '<tr><td colspan="2">No fields captured.</td></tr>'}
            </tbody>
        </table>

        <h2>Recent Workflow Events</h2>
        <ul>
            {''.join(event_items) if event_items else '<li>No events recorded.</li>'}
        </ul>

        {qr_html}

        <p class=\"small\">Generated by DocGen on {escape(timezone.now().isoformat())}</p>
    </body>
</html>
""".strip()


def _render_html_to_pdf_bytes(html: str) -> bytes:
    output = BytesIO()
    render_result = pisa.CreatePDF(src=html, dest=output, encoding="utf-8")
    if render_result.err:
        raise ValidationError("PDF rendering failed.")
    return output.getvalue()


def generate_pdf_artifact(document: Document, generated_by=None) -> DocumentPDF:
    html = _build_pdf_html(document)
    content = _render_html_to_pdf_bytes(html)
    sha256_hash = hashlib.sha256(content).hexdigest()

    latest = document.pdf_versions.order_by("-version").first()
    next_version = 1 if latest is None else latest.version + 1

    document.pdf_versions.filter(is_active=True).update(is_active=False)
    pdf = DocumentPDF(
        document=document,
        version=next_version,
        sha256_hash=sha256_hash,
        is_active=True,
        generated_by=generated_by,
    )
    timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
    filename = f"{document.reference_number or document.pk}_v{next_version}_{timestamp}.pdf"
    pdf.file.save(filename, ContentFile(content), save=False)
    pdf.save()
    return pdf


def generate_pdf_artifact_stub(document: Document, generated_by=None) -> DocumentPDF:
    # Backward-compatible alias for callers not yet updated.
    return generate_pdf_artifact(document=document, generated_by=generated_by)


def generate_pdf_preview(document: Document) -> bytes:
    """Return a DRAFT-watermarked PDF as raw bytes without saving any artifact."""
    html = _build_pdf_preview_html(document)
    return _render_html_to_pdf_bytes(html)


def _build_pdf_preview_html(document: Document) -> str:
    """Same as _build_pdf_html but injects a DRAFT watermark style."""
    base = _build_pdf_html(document, include_diagnostics=True)
    watermark_style = (
        "<style>"
        "body::before {"
        "  content: 'DRAFT';"
        "  position: fixed;"
        "  top: 40%;"
        "  left: 10%;"
        "  font-size: 90pt;"
        "  color: rgba(200,0,0,0.12);"
        "  transform: rotate(-35deg);"
        "  z-index: -1;"
        "  pointer-events: none;"
        "}"
        "</style>"
    )
    return base.replace("</head>", watermark_style + "</head>", 1)


def _get_actor_resolution_adapter():
    adapter_path = getattr(
        settings,
        "DOCGEN_ACTOR_RESOLUTION_ADAPTER",
        "DocGen.adapters.LocalActorResolutionAdapter",
    )
    try:
        adapter_cls = import_string(adapter_path)
        adapter = adapter_cls()
    except Exception:
        adapter = LocalActorResolutionAdapter()

    if not hasattr(adapter, "resolve"):
        return LocalActorResolutionAdapter()
    return adapter


def resolve_workflow_actor(actor_type: str, actor_value: str, document: Document) -> str:
    adapter = _get_actor_resolution_adapter()
    resolved = adapter.resolve(actor_type=actor_type, actor_value=actor_value, document=document)
    if isinstance(resolved, str) and resolved:
        return resolved
    return LocalActorResolutionAdapter().resolve(actor_type=actor_type, actor_value=actor_value, document=document)


def _get_notification_adapter():
    adapter_path = getattr(
        settings,
        "DOCGEN_NOTIFICATION_ADAPTER",
        "DocGen.notifications.LocalNotificationAdapter",
    )
    try:
        adapter_cls = import_string(adapter_path)
        adapter = adapter_cls()
    except Exception:
        adapter = LocalNotificationAdapter()

    if not hasattr(adapter, "publish"):
        return LocalNotificationAdapter()
    return adapter


def emit_notification_event(event_type: str, payload: dict) -> bool:
    adapter = _get_notification_adapter()
    try:
        return bool(adapter.publish(event_type=event_type, payload=payload))
    except Exception:
        return False


def _append_system_event(document: Document, action: str, comment: str, now=None):
    current_time = now or timezone.now()
    metadata = document.metadata or {}
    events = metadata.get("events", [])
    events.append(
        {
            "action": action,
            "actor": None,
            "comment": comment,
            "timestamp": current_time.isoformat(),
        }
    )
    metadata["events"] = events
    document.metadata = metadata


def process_sla_events(now=None) -> dict[str, int]:
    current_time = now or timezone.now()
    reminder_minutes = int(getattr(settings, "DOCGEN_SLA_REMINDER_MINUTES_BEFORE_DUE", 60))
    escalation_minutes = int(getattr(settings, "DOCGEN_SLA_ESCALATION_MINUTES_AFTER_DUE", 120))

    reminder_cutoff_delta = timedelta(minutes=max(0, reminder_minutes))
    escalation_cutoff_delta = timedelta(minutes=max(0, escalation_minutes))

    active_stages = DocumentWorkflowStage.objects.select_related("document").filter(
        status__in=[DocumentStatus.UNDER_REVIEW, DocumentStatus.UNDER_APPROVAL],
        due_at__isnull=False,
    )

    reminders_sent = 0
    escalations_sent = 0
    notifications_sent = 0
    notification_failures = 0

    for stage in active_stages:
        due_at = stage.due_at
        if due_at is None:
            continue

        document_updated = False
        stage_updated_fields = []

        if stage.reminder_sent_at is None:
            reminder_start = due_at - reminder_cutoff_delta
            if reminder_start <= current_time < due_at:
                reminder_payload = {
                    "document_id": stage.document_id,
                    "reference_number": stage.document.reference_number,
                    "stage_id": stage.id,
                    "stage_order": stage.stage_order,
                    "required_action": stage.required_action,
                    "actor_value": stage.actor_value,
                    "due_at": due_at.isoformat(),
                }
                _append_system_event(
                    stage.document,
                    action="sla_reminder",
                    comment=f"Stage {stage.id} due at {due_at.isoformat()}",
                    now=current_time,
                )
                if emit_notification_event("docgen.stage.sla_reminder", reminder_payload):
                    notifications_sent += 1
                else:
                    notification_failures += 1
                stage.reminder_sent_at = current_time
                stage_updated_fields.extend(["reminder_sent_at", "updated_at"])
                reminders_sent += 1
                document_updated = True

        if stage.escalated_at is None and current_time >= (due_at + escalation_cutoff_delta):
            escalation_payload = {
                "document_id": stage.document_id,
                "reference_number": stage.document.reference_number,
                "stage_id": stage.id,
                "stage_order": stage.stage_order,
                "required_action": stage.required_action,
                "actor_value": stage.actor_value,
                "due_at": due_at.isoformat(),
                "overdue_minutes": int((current_time - due_at).total_seconds() // 60),
            }
            _append_system_event(
                stage.document,
                action="sla_escalation",
                comment=f"Stage {stage.id} overdue since {due_at.isoformat()}",
                now=current_time,
            )
            if emit_notification_event("docgen.stage.sla_escalation", escalation_payload):
                notifications_sent += 1
            else:
                notification_failures += 1
            stage.escalated_at = current_time
            stage.escalation_level = (stage.escalation_level or 0) + 1
            if "updated_at" not in stage_updated_fields:
                stage_updated_fields.append("updated_at")
            stage_updated_fields.extend(["escalated_at", "escalation_level"])
            escalations_sent += 1
            document_updated = True

        if document_updated:
            with transaction.atomic():
                stage.document.save(update_fields=["metadata", "updated_at"])
                stage.save(update_fields=list(dict.fromkeys(stage_updated_fields)))

    return {
        "checked": active_stages.count(),
        "reminders_sent": reminders_sent,
        "escalations_sent": escalations_sent,
        "notifications_sent": notifications_sent,
        "notification_failures": notification_failures,
    }
