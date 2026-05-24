# pyright: reportAttributeAccessIssue=false

try:
    from celery import shared_task
except Exception:  # pragma: no cover
    def shared_task(*_args, **_kwargs):
        def _decorator(func):
            return func
        return _decorator

from .services import process_sla_events


@shared_task(name="docgen.process_sla_events")
def process_sla_events_task():
    return process_sla_events()


@shared_task(name="docgen.generate_pdf_artifact")
def generate_pdf_artifact_task(document_id: int, generated_by_id: int | None = None):
    """Asynchronously generate a PDF artifact for a finalized document."""
    from django.contrib.auth import get_user_model
    from .models import Document
    from .services import generate_pdf_artifact

    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        return {"error": f"Document {document_id} not found"}

    generated_by = None
    if generated_by_id is not None:
        User = get_user_model()
        try:
            generated_by = User.objects.get(pk=generated_by_id)
        except User.DoesNotExist:
            pass

    pdf = generate_pdf_artifact(document=document, generated_by=generated_by)
    return {"pdf_id": pdf.id, "version": pdf.version, "sha256": pdf.sha256_hash}
