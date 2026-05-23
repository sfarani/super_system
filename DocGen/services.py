# pyright: reportAttributeAccessIssue=false

import hashlib
import json

from django.core.files.base import ContentFile
from django.utils import timezone

from .models import Document, DocumentPDF


def _build_pdf_stub_content(document: Document) -> bytes:
    fields = [
        {
            "placeholder_name": field.placeholder_name,
            "value_text": field.value_text,
            "value_json": field.value_json,
        }
        for field in document.fields.order_by("placeholder_name")
    ]
    payload = {
        "reference_number": document.reference_number,
        "document_type": document.document_type,
        "title": document.title,
        "subject": document.subject,
        "status": document.status,
        "finalized_at": document.finalized_at.isoformat() if document.finalized_at else None,
        "fields": fields,
        "metadata": document.metadata or {},
    }
    # Stub payload used until full HTML->PDF pipeline is connected.
    return json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")


def generate_pdf_artifact_stub(document: Document, generated_by=None) -> DocumentPDF:
    content = _build_pdf_stub_content(document)
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


def resolve_workflow_actor(actor_type: str, actor_value: str, document: Document) -> str:
    # Hook for future COMPASS integrations (directory/org chart/position mapping).
    if actor_type == "USER":
        return actor_value
    if actor_type == "ROLE":
        return f"role:{actor_value}"
    if actor_type == "POSITION":
        return f"position:{actor_value}"
    if actor_type == "DYNAMIC":
        if actor_value == "N+1_OF_ORIGINATOR" and document.originator:
            return f"dynamic:n+1:{document.originator_id}"
        return f"dynamic:{actor_value}"
    return actor_value
