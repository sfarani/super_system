from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .models import DocumentQRToken


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
