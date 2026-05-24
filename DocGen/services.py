# pyright: reportAttributeAccessIssue=false

import base64
from html.parser import HTMLParser
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse, unquote

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
_RICH_TEXT_ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "ol", "ul", "li", "sub", "sup", "blockquote"}
_RICH_TEXT_VOID_TAGS = {"br"}


class _RichTextSanitizer(HTMLParser):
    """Minimal allow-list HTML sanitizer for document body content."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._open_tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        _ = attrs
        normalized = str(tag or "").lower()
        if normalized not in _RICH_TEXT_ALLOWED_TAGS:
            return
        self._parts.append(f"<{normalized}>")
        if normalized not in _RICH_TEXT_VOID_TAGS:
            self._open_tags.append(normalized)

    def handle_startendtag(self, tag, attrs):
        _ = attrs
        normalized = str(tag or "").lower()
        if normalized not in _RICH_TEXT_ALLOWED_TAGS:
            return
        if normalized in _RICH_TEXT_VOID_TAGS:
            self._parts.append(f"<{normalized}/>")
            return
        self._parts.append(f"<{normalized}></{normalized}>")

    def handle_endtag(self, tag):
        normalized = str(tag or "").lower()
        if normalized not in _RICH_TEXT_ALLOWED_TAGS or normalized in _RICH_TEXT_VOID_TAGS:
            return
        if normalized in self._open_tags:
            while self._open_tags:
                open_tag = self._open_tags.pop()
                self._parts.append(f"</{open_tag}>")
                if open_tag == normalized:
                    break

    def handle_data(self, data):
        self._parts.append(escape(data or ""))

    def handle_entityref(self, name):
        self._parts.append(f"&{name};")

    def handle_charref(self, name):
        self._parts.append(f"&#{name};")

    def get_html(self) -> str:
        while self._open_tags:
            self._parts.append(f"</{self._open_tags.pop()}>")
        return "".join(self._parts)


def sanitize_rich_text_html(raw_html: str) -> str:
    parser = _RichTextSanitizer()
    parser.feed(str(raw_html or ""))
    parser.close()
    return parser.get_html()


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

    originator_username = document.originator.username if document.originator else ""
    originator_full_name = (
        document.originator.get_full_name() or originator_username
        if document.originator
        else ""
    )
    context = {
        "reference_number": document.reference_number or "",
        "date": now_local.strftime("%d %b %Y"),
        "title": document.title or "",
        "subject": document.subject or "",
        "document_type": str(document.document_type or ""),
        "status": str(document.status or ""),
        "originator": originator_username,
        "originator_name": originator_full_name,
        "submitted_date": submitted_at,
        "approval_date": approved_at,
    }

    # M1: Resolve SIGNATURE placeholders — inject approver name and date from the last APPROVE stage.
    # This populates "signature_name", "signature_designation", and "signature_date" tokens that
    # templates can reference as {{signature_name}}, {{signature_designation}}, etc.
    signature_name = ""
    signature_designation = ""
    signature_date = ""
    try:
        last_approve_stage = (
            document.workflow_stages
            .filter(required_action__in=["APPROVE", "APPROVE_WITH_COMMENTS"], acted_at__isnull=False)
            .order_by("-acted_at")
            .first()
        )
        if last_approve_stage:
            if last_approve_stage.actor_type == "USER":
                from django.contrib.auth import get_user_model
                _User = get_user_model()
                try:
                    actor_user = _User.objects.get(username=last_approve_stage.actor_value)
                    signature_name = actor_user.get_full_name() or actor_user.username
                    signature_designation = str(
                        getattr(getattr(actor_user, "profile", None), "designation", "") or ""
                    )
                except _User.DoesNotExist:
                    signature_name = last_approve_stage.actor_value
            else:
                stage_meta = last_approve_stage.metadata or {}
                signature_name = str(stage_meta.get("actor_display_name") or last_approve_stage.actor_value or "")
            if last_approve_stage.acted_at:
                signature_date = timezone.localtime(last_approve_stage.acted_at).strftime("%d %b %Y")
    except Exception:
        pass

    context["signature_name"] = signature_name
    context["signature_designation"] = signature_designation
    context["signature_date"] = signature_date

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

    # D6: Prefer TemplateAsset LOGO (active) over settings-based logos.
    _template_asset_logo: str = ""
    try:
        from .models import TemplateAsset, TemplateAssetType  # local import to avoid circular
        asset_qs = (
            TemplateAsset.objects
            .filter(revision=document.template_revision, asset_type=TemplateAssetType.LOGO, is_active=True)
            .order_by("label")
            .first()
        )
        if asset_qs and asset_qs.file:
            try:
                _template_asset_logo = asset_qs.file.path
            except (ValueError, AttributeError):
                pass
    except Exception:
        pass
    if _template_asset_logo:
        left_logo_src = Path(_template_asset_logo).resolve().as_uri()

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

    def _local_path_from_uri(uri: str) -> Path | None:
        raw = str(uri or "").strip()
        if not raw:
            return None
        lower_raw = raw.lower()
        if lower_raw.startswith("file://"):
            parsed = urlparse(raw)
            path_str = unquote(parsed.path or "")
            if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
                path_str = path_str[1:]
            candidate = Path(path_str)
            return candidate if candidate.exists() else None
        candidate = Path(raw)
        if candidate.is_absolute() and candidate.exists():
            return candidate
        return None

    def _logo_img_style(src: str, max_w_px: int = 190, max_h_px: int = 64) -> str:
        """Return a PDF-safe logo style that keeps image aspect ratio (no skew)."""
        fallback = (
            f"display:inline-block;width:auto;height:{max_h_px}px;"
            f"max-width:{max_w_px}px;"
        )
        local_path = _local_path_from_uri(src)
        if local_path is None:
            return fallback
        try:
            from PIL import Image  # type: ignore[import]

            with Image.open(local_path) as image:
                width_px, height_px = image.size
            if width_px <= 0 or height_px <= 0:
                return fallback
            scale = min(max_w_px / float(width_px), max_h_px / float(height_px), 1.0)
            render_w = max(1, int(round(width_px * scale)))
            render_h = max(1, int(round(height_px * scale)))
            return (
                "display:inline-block;"
                f"width:{render_w}px;"
                f"height:{render_h}px;"
            )
        except Exception:
            return fallback

    def _resolve_pdf_font(font_key: str) -> str:
        key = str(font_key or "helvetica").lower()
        if key == "times":
            return "Times-Roman"
        if key == "courier":
            return "Courier"
        return "Helvetica"

    def _default_font_size(block_type: str) -> float:
        if block_type == "letterhead":
            return 13.0
        if block_type == "reference_line":
            return 10.0
        if block_type == "contacts_block":
            return 10.0
        if block_type == "footer":
            return 9.0
        return 11.0

    def _resolve_font_size(block_type: str, raw_size) -> float:
        try:
            parsed = float(raw_size)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
        return _default_font_size(block_type)

    def _block_style(block_type: str, align: str, font_key: str, font_size, split_mode: bool = False) -> str:
        style = "margin-bottom:10px;white-space:normal;"
        style += f"font-family:{_resolve_pdf_font(font_key)};"
        style += f"font-size:{_resolve_font_size(block_type, font_size)}pt;"
        if not split_mode and align in {"left", "center", "right"}:
            style += f"text-align:{align};"
        if block_type == "letterhead":
            style += "font-weight:700;line-height:1.35;border-bottom:1px solid #222;padding-bottom:8px;margin-bottom:16px;"
        elif block_type == "reference_line":
            style += "color:#333;margin-bottom:8px;"
        elif block_type == "subject_line":
            style += "font-weight:700;text-decoration:underline;margin-top:10px;margin-bottom:12px;"
        elif block_type == "salutation":
            style += "margin-top:8px;margin-bottom:10px;"
        elif block_type == "body":
            style += "line-height:1.65;"
            if not split_mode:
                style += "text-align:justify;"
        elif block_type == "contacts_block":
            style += "white-space:nowrap;"
        elif block_type == "closing":
            style += "margin-top:14px;margin-bottom:8px;"
        elif block_type == "signature_block":
            style += "margin-top:26px;line-height:1.4;"
        elif block_type == "footer":
            style += "color:#555;border-top:1px solid #ccc;padding-top:6px;margin-top:20px;"
        return style

    def _render_table_content(raw_content: str) -> str:
        """Render a JSON array (or array-of-arrays) as an HTML table."""
        try:
            data = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return escape(raw_content)
        if not isinstance(data, list) or not data:
            return escape(raw_content)

        rows_html: list[str] = []
        header_row = data[0]
        if isinstance(header_row, dict):
            headers = list(header_row.keys())
            header_cells = "".join(
                f"<th style='border:1px solid #aaa;padding:4px 8px;background:#e8e8e8;font-weight:bold;'>{escape(str(h))}</th>"
                for h in headers
            )
            rows_html.append(f"<tr>{header_cells}</tr>")
            for row in data:
                if isinstance(row, dict):
                    cells = "".join(
                        f"<td style='border:1px solid #aaa;padding:4px 8px;'>{escape(str(row.get(h, '')))}</td>"
                        for h in headers
                    )
                    rows_html.append(f"<tr>{cells}</tr>")
        elif isinstance(header_row, list):
            # First row treated as headers.
            header_cells = "".join(
                f"<th style='border:1px solid #aaa;padding:4px 8px;background:#e8e8e8;font-weight:bold;'>{escape(str(h))}</th>"
                for h in header_row
            )
            rows_html.append(f"<tr>{header_cells}</tr>")
            for row in data[1:]:
                cells = "".join(
                    f"<td style='border:1px solid #aaa;padding:4px 8px;'>{escape(str(cell))}</td>"
                    for cell in (row if isinstance(row, list) else [row])
                )
                rows_html.append(f"<tr>{cells}</tr>")
        else:
            # Flat list — single column.
            for item in data:
                rows_html.append(
                    f"<tr><td style='border:1px solid #aaa;padding:4px 8px;'>{escape(str(item))}</td></tr>"
                )

        return (
            "<table style='width:100%;border-collapse:collapse;margin-bottom:10px;'>"
            + "".join(rows_html)
            + "</table>"
        )

    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "custom")
        align = str(block.get("align") or "left")
        font_key = str(block.get("font_family") or "helvetica")
        font_size = block.get("font_size")
        content = _render_template_tokens(str(block.get("content") or ""), render_context)

        # D2: Evaluate conditional visibility — skip block if condition is not met.
        conditional_cfg = block.get("conditional")
        if isinstance(conditional_cfg, dict):
            depends_on = str(conditional_cfg.get("depends_on_field") or "").strip()
            show_if = str(conditional_cfg.get("show_if_value") or "").strip()
            if depends_on:
                actual_value = str(render_context.get(depends_on) or "").strip()
                if actual_value != show_if:
                    continue  # Condition false — skip this block entirely.

        # D3: Render table-type blocks from JSON array content.
        if block_type == "table":
            table_html = _render_table_content(content)
            style = _block_style(block_type, align, font_key, font_size, split_mode=False)
            parts.append(f"<div style='{style}'>{table_html}</div>")
            continue

        if block_type == "letterhead":
            content_html = escape(content).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            left_logo_style = _logo_img_style(left_logo_src)
            right_logo_style = _logo_img_style(right_logo_src)
            left_img_html = (
                f"<img src='{escape(left_logo_src)}' style='{left_logo_style}' />"
                if left_logo_src
                else ""
            )
            right_img_html = (
                f"<img src='{escape(right_logo_src)}' style='{right_logo_style}' />"
                if right_logo_src
                else ""
            )
            text_align = align if align in {"left", "center", "right"} else "left"
            if right_img_html:
                row_html = (
                    "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                    "<tr>"
                    f"<td style='width:20%;text-align:left;vertical-align:top;'>{left_img_html}</td>"
                    f"<td style='width:60%;text-align:{text_align};vertical-align:top;'>{content_html}</td>"
                    f"<td style='width:20%;text-align:right;vertical-align:top;'>{right_img_html}</td>"
                    "</tr>"
                    "</table>"
                )
            else:
                if text_align == "center":
                    row_html = (
                        "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                        "<tr>"
                        f"<td style='width:20%;text-align:left;vertical-align:top;'>{left_img_html}</td>"
                        f"<td style='width:60%;text-align:center;vertical-align:top;'>{content_html}</td>"
                        "<td style='width:20%;text-align:right;vertical-align:top;'></td>"
                        "</tr>"
                        "</table>"
                    )
                else:
                    row_html = (
                        "<table style='width:100%;border-collapse:collapse;table-layout:fixed;'>"
                        "<tr>"
                        f"<td style='width:20%;text-align:left;vertical-align:top;'>{left_img_html}</td>"
                        f"<td style='width:80%;text-align:{text_align};vertical-align:top;'>{content_html}</td>"
                        "</tr>"
                        "</table>"
                    )
            style = _block_style(block_type, align, font_key, font_size, split_mode=False)
            parts.append(f"<div style='{style}'>{row_html}</div>")
            continue

        if block_type == "body":
            content_html = sanitize_rich_text_html(content)
            style = _block_style(block_type, align, font_key, font_size, split_mode=False)
            parts.append(f"<div style='{style}'>{content_html}</div>")
            continue

        if "||" in content:
            left_raw, right_raw = content.split("||", 1)
            left_html = escape(left_raw.strip()).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            right_html = escape(right_raw.strip()).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            style = _block_style(block_type, align, font_key, font_size, split_mode=True)
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
        style = _block_style(block_type, align, font_key, font_size, split_mode=False)

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


def _build_qr_html(document: Document) -> str:
    """Return the QR code HTML block for the document, or empty string if no token exists."""
    try:
        qr_token = document.qr_token  # OneToOne; raises RelatedObjectDoesNotExist if absent
        verify_path = f"/compass/docgen/verify/{qr_token.token}/"
        verify_url = getattr(settings, "DOCGEN_PUBLIC_BASE_URL", "").rstrip("/") + verify_path
        qr_data_uri = _generate_qr_data_uri(verify_url)
        if qr_data_uri:
            return (
                "<div style='margin-top:24px;text-align:center;'>"
                f"<img src='{qr_data_uri}' width='100' height='100' />"
                "<br/><span style='font-size:8pt;color:#555;'>"
                f"Scan to verify: {escape(verify_url)}"
                "</span></div>"
            )
        return f"<p style='font-size:9pt;color:#555;'>Verify: {escape(verify_url)}</p>"
    except Exception:
        return ""


def _build_approval_trail_html(document: Document) -> str:
    """Return an HTML block showing the completed workflow trail for the document."""
    acted_stages = list(
        document.workflow_stages
        .exclude(acted_at=None)
        .order_by("stage_order", "id")
        .select_related("acted_by")
    )
    if not acted_stages:
        return ""

    rows = []
    for stage in acted_stages:
        actor_display = ""
        if stage.acted_by:
            full_name = stage.acted_by.get_full_name()
            actor_display = full_name if full_name else stage.acted_by.username
        acted_at_display = (
            timezone.localtime(stage.acted_at).strftime("%d %b %Y %H:%M") if stage.acted_at else ""
        )
        rows.append(
            "<tr>"
            f"<td style='padding:4px 6px;border:1px solid #ccc;'>{escape(stage.title)}</td>"
            f"<td style='padding:4px 6px;border:1px solid #ccc;'>{escape(actor_display)}</td>"
            f"<td style='padding:4px 6px;border:1px solid #ccc;'>{escape(stage.required_action)}</td>"
            f"<td style='padding:4px 6px;border:1px solid #ccc;'>{escape(stage.comments)}</td>"
            f"<td style='padding:4px 6px;border:1px solid #ccc;'>{escape(acted_at_display)}</td>"
            "</tr>"
        )

    return (
        "<div style='margin-top:24px;'>"
        "<div style='font-weight:700;font-size:10pt;margin-bottom:6px;border-bottom:1px solid #555;padding-bottom:4px;'>"
        "Approval Trail"
        "</div>"
        "<table style='width:100%;border-collapse:collapse;font-size:9pt;'>"
        "<thead>"
        "<tr style='background:#f3f3f3;'>"
        "<th style='padding:4px 6px;border:1px solid #ccc;text-align:left;'>Stage</th>"
        "<th style='padding:4px 6px;border:1px solid #ccc;text-align:left;'>Actor</th>"
        "<th style='padding:4px 6px;border:1px solid #ccc;text-align:left;'>Action</th>"
        "<th style='padding:4px 6px;border:1px solid #ccc;text-align:left;'>Comments</th>"
        "<th style='padding:4px 6px;border:1px solid #ccc;text-align:left;'>Date</th>"
        "</tr>"
        "</thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody>"
        "</table>"
        "</div>"
    )


_CLASSIFICATION_WATERMARKS: dict[str, str] = {
    "CONFIDENTIAL": "CONFIDENTIAL",
    "confidential": "CONFIDENTIAL",
    "RESTRICTED": "RESTRICTED",
    "restricted": "RESTRICTED",
    "SECRET": "SECRET",
    "secret": "SECRET",
    "FOR OFFICIAL USE ONLY": "FOR OFFICIAL USE ONLY",
    "for official use only": "FOR OFFICIAL USE ONLY",
    "FOUO": "FOR OFFICIAL USE ONLY",
    "fouo": "FOR OFFICIAL USE ONLY",
}


def _build_classification_watermark_style(document: Document, is_draft: bool = False) -> str:
    """Return a CSS <style> block with an appropriate watermark for the document.

    Draft preview always shows 'DRAFT'. Finalized documents show a classification
    watermark when classification is set to a recognized sensitive value.
    """
    if is_draft:
        watermark_text = "DRAFT"
        color = "rgba(200,0,0,0.12)"
    else:
        raw_classification = str(document.classification or "").strip()
        watermark_text = _CLASSIFICATION_WATERMARKS.get(raw_classification, "")
        if not watermark_text:
            return ""
        color = "rgba(0,0,180,0.08)"

    escaped_text = watermark_text.replace("'", "\\'")
    return (
        "<style>"
        "body::before {"
        f"  content: '{escaped_text}';"
        "  position: fixed;"
        "  top: 40%;"
        "  left: 5%;"
        "  font-size: 60pt;"
        "  color: " + color + ";"
        "  transform: rotate(-35deg);"
        "  z-index: -1;"
        "  pointer-events: none;"
        "}"
        "</style>"
    )


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

    approval_trail_html = _build_approval_trail_html(document)
    qr_html = _build_qr_html(document)
    classification_watermark = _build_classification_watermark_style(document, is_draft=include_diagnostics)

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
        {classification_watermark}
    </head>
    <body>
        {diagnostics_html}
        {layout_body}
        {approval_trail_html}
        {qr_html}
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
        {classification_watermark}
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

        {approval_trail_html}
        {qr_html}

        <p class=\"small\">Generated by DocGen on {escape(timezone.now().isoformat())}</p>
    </body>
</html>
""".strip()


def _render_html_to_pdf_bytes(html: str) -> bytes:
    def _pisa_link_callback(uri: str, rel: str) -> str:
        _ = rel
        raw_uri = str(uri or "").strip()
        if not raw_uri:
            return raw_uri

        lower_uri = raw_uri.lower()
        if lower_uri.startswith(("http://", "https://", "data:")):
            return raw_uri

        if lower_uri.startswith("file://"):
            parsed = urlparse(raw_uri)
            file_path = unquote(parsed.path or "")
            if file_path.startswith("/") and len(file_path) > 2 and file_path[2] == ":":
                file_path = file_path[1:]
            if file_path and os.path.exists(file_path):
                return file_path

        static_url = str(getattr(settings, "STATIC_URL", "/static/") or "/static/")
        media_url = str(getattr(settings, "MEDIA_URL", "/media/") or "/media/")
        if not static_url.startswith("/"):
            static_url = "/" + static_url
        if not media_url.startswith("/"):
            media_url = "/" + media_url

        if raw_uri.startswith(static_url):
            rel_path = raw_uri[len(static_url):].lstrip("/")
            found = finders.find(rel_path)
            if found and os.path.exists(found):
                return found
            fallback = Path(settings.BASE_DIR) / "static" / rel_path
            if fallback.exists():
                return str(fallback)

        if raw_uri.startswith(media_url):
            rel_path = raw_uri[len(media_url):].lstrip("/")
            media_root = getattr(settings, "MEDIA_ROOT", "")
            if media_root:
                candidate = Path(media_root) / rel_path
                if candidate.exists():
                    return str(candidate)

        if os.path.isabs(raw_uri) and os.path.exists(raw_uri):
            return raw_uri

        base_candidate = Path(settings.BASE_DIR) / raw_uri.lstrip("/")
        if base_candidate.exists():
            return str(base_candidate)

        return raw_uri

    output = BytesIO()
    render_result = pisa.CreatePDF(
        src=html,
        dest=output,
        encoding="utf-8",
        link_callback=_pisa_link_callback,
    )
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
    """Same as _build_pdf_html but passes include_diagnostics=True so the DRAFT watermark is applied."""
    return _build_pdf_html(document, include_diagnostics=True)


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
