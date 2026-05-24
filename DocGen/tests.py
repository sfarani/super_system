from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core import signing
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test.utils import override_settings
from datetime import timedelta
import json
from unittest.mock import patch

from .adapters import CompassActorResolutionAdapter
from .notifications import HttpNotificationAdapter
from .services import process_sla_events, sanitize_rich_text_html
from .models import (
	Document,
	DocumentAttachment,
	DocumentComment,
	DocumentQRToken,
	DocumentType,
	DocumentStatus,
	DocumentWorkflowStage,
	RetentionPolicy,
	Template,
	TemplateCategory,
	TemplateRevision,
	TemplateTokenDefinition,
	TemplateWorkflowStage,
)


class CustomActorResolutionAdapter:
	def resolve(self, actor_type: str, actor_value: str, document) -> str:
		return f"custom:{actor_type.lower()}:{actor_value}"


class CapturingNotificationAdapter:
	events = []

	def publish(self, event_type: str, payload: dict) -> bool:
		self.__class__.events.append((event_type, payload))
		return True


class FailingNotificationAdapter:
	def publish(self, event_type: str, payload: dict) -> bool:
		_ = (event_type, payload)
		return False


class DocGenModelTests(TestCase):
	def setUp(self):
		category = TemplateCategory.objects.create(name="Office", slug="office")
		template = Template.objects.create(
			category=category,
			title="Office Order Template",
			code="OO_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)

	def test_reference_number_increments_for_same_type_year(self):
		doc1 = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.OFFICE_ORDER,
			title="Office Order One",
		)
		doc2 = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.OFFICE_ORDER,
			title="Office Order Two",
		)

		ref1 = doc1.assign_reference_number(org_code="HQ")
		ref2 = doc2.assign_reference_number(org_code="HQ")

		year = timezone.now().year
		self.assertEqual(ref1, f"COMPASS/HQ/OO/{year}/00001")
		self.assertEqual(ref2, f"COMPASS/HQ/OO/{year}/00002")


class DocGenViewTests(TestCase):
	def setUp(self):
		category = TemplateCategory.objects.create(name="Memo", slug="memo")
		template = Template.objects.create(
			category=category,
			title="Memo Template",
			code="MEMO_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		self.document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Memo Doc",
			reference_number="COMPASS/HQ/MEM/2026/00001",
			finalized_at=timezone.now(),
		)

	def test_verify_endpoint_returns_valid_document(self):
		token = DocumentQRToken.objects.create(
			document=self.document,
			token="abc123",
			signed_payload=signing.dumps({"reference_number": self.document.reference_number}, salt="docgen-verify"),
		)
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": token.token}))

		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertTrue(data["valid"])
		self.assertEqual(data["reference_number"], self.document.reference_number)

	def test_verify_endpoint_returns_not_found_for_unknown_token(self):
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": "not-found"}))
		self.assertEqual(response.status_code, 404)

	def test_rich_text_sanitizer_removes_disallowed_tags(self):
		raw = "<p>Hello <strong>World</strong></p><script>alert(1)</script><img src='x' onerror='x'>"
		sanitized = sanitize_rich_text_html(raw)

		self.assertIn("<p>Hello <strong>World</strong></p>", sanitized)
		self.assertNotIn("<script", sanitized)
		self.assertNotIn("<img", sanitized)

	def test_verify_endpoint_returns_revoked_status(self):
		token = DocumentQRToken.objects.create(
			document=self.document,
			token="revoked-token",
			signed_payload=signing.dumps({"reference_number": self.document.reference_number}, salt="docgen-verify"),
			is_revoked=True,
			revoked_reason="Invalidated by admin",
		)
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": token.token}))
		self.assertEqual(response.status_code, 200)
		self.assertFalse(response.json()["valid"])
		self.assertEqual(response.json()["status"], "revoked")

	def test_verify_endpoint_detects_tampered_token_payload(self):
		token = DocumentQRToken.objects.create(
			document=self.document,
			token="tampered-token",
			signed_payload="definitely-not-a-valid-signature",
		)
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": token.token}))
		self.assertEqual(response.status_code, 400)
		self.assertEqual(response.json()["status"], "tampered")

	def test_verify_endpoint_returns_superseded_status(self):
		token = DocumentQRToken.objects.create(
			document=self.document,
			token="superseded-token",
			signed_payload="e30=",
		)
		token.signed_payload = signing.dumps({"reference_number": self.document.reference_number}, salt="docgen-verify")
		token.save(update_fields=["signed_payload", "updated_at"])

		superseding = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Superseding Memo",
			reference_number="COMPASS/HQ/MEM/2026/00002",
			status=DocumentStatus.FINALIZED,
			finalized_at=timezone.now(),
			supersedes=self.document,
		)
		self.assertEqual(superseding.supersedes_id, self.document.id)

		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": token.token}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertFalse(data["valid"])
		self.assertEqual(data["status"], "superseded")
		self.assertEqual(data["superseded_by"], superseding.reference_number)


class TemplateLifecycleTests(TestCase):
	def setUp(self):
		self.category = TemplateCategory.objects.create(name="General", slug="general")
		self.template = Template.objects.create(
			category=self.category,
			title="General Template",
			code="GEN_TEMPLATE",
		)

	def test_publish_revision_marks_template_published(self):
		revision = TemplateRevision.objects.create(
			template=self.template,
			version=1,
			layout_schema={"blocks": []},
		)

		self.template.publish_revision(revision)
		self.template.refresh_from_db()
		revision.refresh_from_db()

		self.assertEqual(self.template.status, "PUBLISHED")
		self.assertTrue(self.template.is_locked)
		self.assertTrue(revision.is_published)

	def test_published_revision_is_immutable(self):
		revision = TemplateRevision.objects.create(
			template=self.template,
			version=1,
			layout_schema={"blocks": [{"type": "body"}]},
			is_published=True,
		)

		revision.layout_schema = {"blocks": [{"type": "header"}]}
		with self.assertRaises(ValidationError):
			revision.save()


class TemplateApiTests(TestCase):
	def setUp(self):
		self.category = TemplateCategory.objects.create(name="API Category", slug="api-category")

	def test_create_template_via_api(self):
		payload = {
			"category_id": self.category.id,
			"title": "API Template",
			"code": "API_TEMPLATE",
			"layout_schema": {"blocks": [{"type": "body"}]},
		}
		response = self.client.post(
			reverse("docgen:template-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)

		self.assertEqual(response.status_code, 201)
		self.assertEqual(Template.objects.count(), 1)
		template = Template.objects.get(code="API_TEMPLATE")
		self.assertEqual(template.revisions.count(), 1)

	def test_list_templates_via_api(self):
		template = Template.objects.create(
			category=self.category,
			title="List Template",
			code="LIST_TEMPLATE",
		)
		TemplateRevision.objects.create(template=template, version=1)

		response = self.client.get(reverse("docgen:template-collection"))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["code"], "LIST_TEMPLATE")

	def test_clone_revision_via_api(self):
		template = Template.objects.create(
			category=self.category,
			title="Clone Template",
			code="CLONE_TEMPLATE",
		)
		TemplateRevision.objects.create(template=template, version=1, layout_schema={"blocks": []})

		response = self.client.post(reverse("docgen:template-clone-revision", kwargs={"template_id": template.id}))
		self.assertEqual(response.status_code, 201)
		self.assertEqual(template.revisions.count(), 2)

	def test_publish_template_via_api(self):
		template = Template.objects.create(
			category=self.category,
			title="Publish Template",
			code="PUBLISH_TEMPLATE",
		)
		rev1 = TemplateRevision.objects.create(template=template, version=1)
		rev2 = TemplateRevision.objects.create(template=template, version=2)

		response = self.client.post(
			reverse("docgen:template-publish", kwargs={"template_id": template.id}),
			data=json.dumps({"revision_id": rev2.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		template.refresh_from_db()
		rev1.refresh_from_db()
		rev2.refresh_from_db()
		self.assertEqual(template.status, "PUBLISHED")
		self.assertFalse(rev1.is_published)
		self.assertTrue(rev2.is_published)

	def test_retire_template_via_api(self):
		template = Template.objects.create(
			category=self.category,
			title="Retire Template",
			code="RETIRE_TEMPLATE",
		)
		TemplateRevision.objects.create(template=template, version=1)

		response = self.client.post(reverse("docgen:template-retire", kwargs={"template_id": template.id}))
		self.assertEqual(response.status_code, 200)
		template.refresh_from_db()
		self.assertEqual(template.status, "RETIRED")
		self.assertTrue(template.is_locked)

	@override_settings(DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.CapturingNotificationAdapter")
	def test_template_lifecycle_emits_notification_events(self):
		CapturingNotificationAdapter.events = []
		create_payload = {
			"category_id": self.category.id,
			"title": "Notified Template",
			"code": "NOTIFIED_TEMPLATE",
			"layout_schema": {"blocks": [{"type": "body"}]},
		}
		create_response = self.client.post(
			reverse("docgen:template-collection"),
			data=json.dumps(create_payload),
			content_type="application/json",
		)
		self.assertEqual(create_response.status_code, 201)
		template_id = create_response.json()["id"]

		clone_response = self.client.post(reverse("docgen:template-clone-revision", kwargs={"template_id": template_id}))
		self.assertEqual(clone_response.status_code, 201)

		publish_response = self.client.post(reverse("docgen:template-publish", kwargs={"template_id": template_id}))
		self.assertEqual(publish_response.status_code, 200)

		retire_response = self.client.post(reverse("docgen:template-retire", kwargs={"template_id": template_id}))
		self.assertEqual(retire_response.status_code, 200)

		event_types = [event_type for event_type, _payload in CapturingNotificationAdapter.events]
		self.assertIn("docgen.template.create", event_types)
		self.assertIn("docgen.template.clone_revision", event_types)
		self.assertIn("docgen.template.publish", event_types)
		self.assertIn("docgen.template.retire", event_types)


class DocumentApiTests(TestCase):
	def setUp(self):
		self.category = TemplateCategory.objects.create(name="Docs", slug="docs")
		template = Template.objects.create(
			category=self.category,
			title="Document Template",
			code="DOC_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=1,
			title="Initial Review",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			sla_hours=24,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=2,
			title="Final Approval",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="approver",
			required_action="APPROVE",
			sla_hours=24,
		)

	def test_create_document_via_api(self):
		payload = {
			"template_revision_id": self.revision.id,
			"document_type": "MEMORANDUM",
			"title": "API Document",
			"subject": "Subject",
		}
		response = self.client.post(
			reverse("docgen:document-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		self.assertEqual(Document.objects.count(), 1)

	@override_settings(DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.CapturingNotificationAdapter")
	def test_document_create_and_set_fields_emit_notifications(self):
		CapturingNotificationAdapter.events = []
		payload = {
			"template_revision_id": self.revision.id,
			"document_type": "MEMORANDUM",
			"title": "Notified API Document",
			"subject": "Subject",
		}
		create_response = self.client.post(
			reverse("docgen:document-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(create_response.status_code, 201)
		document_id = create_response.json()["id"]

		fields_payload = {
			"fields": [
				{
					"placeholder_name": "recipient",
					"value_text": "Director",
					"value_json": {},
				}
			]
		}
		fields_response = self.client.post(
			reverse("docgen:document-set-fields", kwargs={"document_id": document_id}),
			data=json.dumps(fields_payload),
			content_type="application/json",
		)
		self.assertEqual(fields_response.status_code, 200)

		event_types = [event_type for event_type, _payload in CapturingNotificationAdapter.events]
		self.assertIn("docgen.document.create", event_types)
		self.assertIn("docgen.document.set_fields", event_types)

	def test_set_document_fields_via_api(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Field Document",
		)
		payload = {
			"fields": [
				{
					"placeholder_name": "recipient",
					"value_text": "Director",
					"value_json": {},
				}
			]
		}
		response = self.client.post(
			reverse("docgen:document-set-fields", kwargs={"document_id": document.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.fields.count(), 1)

	def test_set_body_text_field_sanitizes_html_and_keeps_delta(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Body Field Document",
		)
		payload = {
			"fields": [
				{
					"placeholder_name": "body_text",
					"value_text": "<p>Intro <strong>text</strong></p><script>alert(1)</script>",
					"value_json": {"ops": [{"insert": "Intro text\n"}]},
				}
			]
		}
		response = self.client.post(
			reverse("docgen:document-set-fields", kwargs={"document_id": document.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)

		stored = document.fields.get(placeholder_name="body_text")
		self.assertIn("<strong>text</strong>", stored.value_text)
		self.assertNotIn("<script", stored.value_text)
		self.assertEqual(stored.value_json, {"ops": [{"insert": "Intro text\n"}]})

	def test_set_template_tokens_allows_unknown_layout_keys(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Unknown Token Document",
		)
		# Keep at least one registry key present to ensure mixed payload path is covered.
		TemplateTokenDefinition.objects.create(
			key="department",
			label="Department",
			description="Department name",
			allow_document_override=True,
		)
		payload = {
			"fields": [],
			"template_tokens": {
				"City": "Islamabad",
				"department": "Admin Wing",
			},
		}
		response = self.client.post(
			reverse("docgen:document-set-fields", kwargs={"document_id": document.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		tokens = (document.metadata or {}).get("template_tokens", {})
		self.assertEqual(tokens.get("City"), "Islamabad")
		self.assertEqual(tokens.get("department"), "Admin Wing")

	def test_submit_document_via_api_assigns_reference_and_workflow(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Submit Document",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_REVIEW")
		self.assertTrue(document.reference_number)
		self.assertEqual(document.workflow_stages.count(), 2)
		self.assertEqual(document.revisions.count(), 1)

	def test_submit_non_draft_document_is_blocked(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Approved Document",
			status="APPROVED",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 400)

	def _create_submitted_document(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Action Document",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		return document

	def test_approve_action_transitions_document(self):
		document = self._create_submitted_document()
		review_response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		self.assertEqual(review_response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_APPROVAL")

		approve_response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "approve"}),
			data=json.dumps({"comment": "Looks good"}),
			content_type="application/json",
		)
		self.assertEqual(approve_response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "APPROVED")
		self.assertIsNotNone(document.approved_at)
		self.assertEqual(document.revisions.count(), 3)

	def test_return_action_transitions_document(self):
		document = self._create_submitted_document()
		response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "return"}),
			data=json.dumps({"comment": "Need changes"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "RETURNED")
		self.assertEqual(document.revisions.count(), 2)

	def test_reject_action_transitions_document(self):
		document = self._create_submitted_document()
		response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "reject"}),
			data=json.dumps({"comment": "Not acceptable"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "REJECTED")
		self.assertEqual(document.revisions.count(), 2)

	def test_action_blocked_for_draft_document(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Draft Action",
			status="DRAFT",
		)
		response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "approve"})
		)
		self.assertEqual(response.status_code, 400)

	def test_delegate_action_updates_active_stage_actor(self):
		document = self._create_submitted_document()
		response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "delegate"}),
			data=json.dumps({"delegate_to": "senior_reviewer", "comment": "Please handle"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		active_stage = document.workflow_stages.order_by("stage_order", "id").first()
		self.assertEqual(active_stage.actor_value, "senior_reviewer")

	def test_timeline_endpoint_returns_events_and_revisions(self):
		document = self._create_submitted_document()
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		response = self.client.get(reverse("docgen:document-timeline", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertGreaterEqual(len(data["events"]), 2)
		self.assertGreaterEqual(len(data["revisions"]), 2)

	def test_submit_routes_directly_to_under_approval_when_first_stage_is_approval(self):
		template = Template.objects.create(
			category=self.category,
			title="Approval First Template",
			code="APPROVAL_FIRST_TEMPLATE",
		)
		revision = TemplateRevision.objects.create(template=template, version=1, is_published=True)
		revision.workflow_stages.create(
			stage_order=1,
			title="Approver Stage",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="approver",
			required_action="APPROVE",
		)
		document = Document.objects.create(
			template_revision=revision,
			document_type=DocumentType.MEMORANDUM,
			title="Approval Route Document",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_APPROVAL")

	def test_request_clarification_keeps_document_under_review(self):
		document = self._create_submitted_document()
		response = self.client.post(
			reverse(
				"docgen:document-action",
				kwargs={"document_id": document.id, "action": "request_clarification"},
			),
			data=json.dumps({"comment": "Need clarification on paragraph 2"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_REVIEW")
		self.assertEqual(document.revisions.count(), 2)

	def test_finalize_approved_document_sets_finalized_and_qr(self):
		document = self._create_submitted_document()
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "approve"}),
			data=json.dumps({"comment": "Approved"}),
			content_type="application/json",
		)
		finalize_response = self.client.post(
			reverse("docgen:document-finalize", kwargs={"document_id": document.id})
		)
		self.assertEqual(finalize_response.status_code, 200)
		response_data = finalize_response.json()
		document.refresh_from_db()
		self.assertEqual(document.status, "FINALIZED")
		self.assertIsNotNone(document.finalized_at)
		self.assertTrue(hasattr(document, "qr_token"))
		self.assertEqual(document.pdf_versions.count(), 1)
		pdf = document.pdf_versions.first()
		self.assertTrue(pdf.is_active)
		self.assertEqual(len(pdf.sha256_hash), 64)
		with pdf.file.open("rb") as generated_pdf:
			header = generated_pdf.read(5)
		self.assertEqual(header, b"%PDF-")
		self.assertEqual(response_data["pdf_version"], 1)
		self.assertEqual(response_data["pdf_sha256"], pdf.sha256_hash)

	def test_finalize_rejected_document_is_blocked(self):
		document = self._create_submitted_document()
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "reject"}),
			data=json.dumps({"comment": "Rejected"}),
			content_type="application/json",
		)
		finalize_response = self.client.post(
			reverse("docgen:document-finalize", kwargs={"document_id": document.id})
		)
		self.assertEqual(finalize_response.status_code, 400)

	def _create_finalized_document(self):
		document = self._create_submitted_document()
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "approve"}),
			data=json.dumps({"comment": "Approved"}),
			content_type="application/json",
		)
		finalize_response = self.client.post(
			reverse("docgen:document-finalize", kwargs={"document_id": document.id})
		)
		self.assertEqual(finalize_response.status_code, 200)
		document.refresh_from_db()
		return document

	def test_archive_blocked_until_retention_window(self):
		document = self._create_finalized_document()
		archive_response = self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": document.id})
		)
		self.assertEqual(archive_response.status_code, 400)

	def test_archive_with_force_succeeds(self):
		document = self._create_finalized_document()
		archive_response = self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": document.id}),
			data=json.dumps({"force": True}),
			content_type="application/json",
		)
		self.assertEqual(archive_response.status_code, 200)
		self.assertIsNotNone(archive_response.json().get("archived_at"))
		document.refresh_from_db()
		self.assertEqual(document.status, "ARCHIVED")
		self.assertIsNotNone(document.archived_at)

	def test_archive_after_retention_window_succeeds(self):
		document = self._create_finalized_document()
		document.finalized_at = timezone.now() - timedelta(days=370)
		document.save(update_fields=["finalized_at", "updated_at"])

		archive_response = self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": document.id})
		)
		self.assertEqual(archive_response.status_code, 200, archive_response.content.decode())
		document.refresh_from_db()
		self.assertEqual(document.status, "ARCHIVED")

	def test_archive_uses_active_retention_policy(self):
		RetentionPolicy.objects.create(name="strict", archive_retention_days=30, is_active=True)
		document = self._create_finalized_document()
		document.finalized_at = timezone.now() - timedelta(days=31)
		document.save(update_fields=["finalized_at", "updated_at"])

		archive_response = self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": document.id})
		)
		self.assertEqual(archive_response.status_code, 200)
		self.assertEqual(archive_response.json()["retention_days"], 30)

	def test_archive_collection_lists_archived_documents(self):
		archived_document = self._create_finalized_document()
		archive_response = self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": archived_document.id}),
			data=json.dumps({"force": True}),
			content_type="application/json",
		)
		self.assertEqual(archive_response.status_code, 200)

		response = self.client.get(reverse("docgen:document-archive-collection"))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["id"], archived_document.id)

	def test_archive_collection_supports_search_filter(self):
		first_document = self._create_finalized_document()
		self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": first_document.id}),
			data=json.dumps({"force": True}),
			content_type="application/json",
		)

		second_document = self._create_submitted_document()
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": second_document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": second_document.id, "action": "approve"}),
			data=json.dumps({"comment": "Approved"}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-finalize", kwargs={"document_id": second_document.id})
		)
		second_document.refresh_from_db()
		self.client.post(
			reverse("docgen:document-archive", kwargs={"document_id": second_document.id}),
			data=json.dumps({"force": True}),
			content_type="application/json",
		)

		response = self.client.get(
			reverse("docgen:document-archive-collection"),
			{"q": first_document.reference_number},
		)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["id"], first_document.id)

	def test_submit_resolves_role_actor_value(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Actor Resolution",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		first_stage = document.workflow_stages.order_by("stage_order", "id").first()
		self.assertTrue(first_stage.actor_value.startswith("role:"))

	def test_parallel_all_requires_all_stage_actions(self):
		document = self._create_submitted_document()
		first_stage = document.workflow_stages.filter(stage_order=1).first()
		first_stage.execution_mode = "PARALLEL_ALL"
		first_stage.actor_value = "role:reviewer_a"
		first_stage.save(update_fields=["execution_mode", "actor_value", "updated_at"])
		DocumentWorkflowStage.objects.create(
			document=document,
			stage_order=1,
			title="Parallel Reviewer B",
			execution_mode="PARALLEL_ALL",
			actor_type="ROLE",
			actor_value="role:reviewer_b",
			required_action="REVIEW",
			status="UNDER_REVIEW",
		)

		active_stages = list(
			document.workflow_stages.filter(status="UNDER_REVIEW", stage_order=1).order_by("id")
		)
		first_review = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"stage_id": active_stages[0].id, "comment": "A done"}),
			content_type="application/json",
		)
		self.assertEqual(first_review.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_REVIEW")

		second_review = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"stage_id": active_stages[1].id, "comment": "B done"}),
			content_type="application/json",
		)
		self.assertEqual(second_review.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_APPROVAL")

	def test_parallel_any_advances_on_first_action(self):
		document = self._create_submitted_document()
		first_stage = document.workflow_stages.filter(stage_order=1).first()
		first_stage.execution_mode = "PARALLEL_ANY"
		first_stage.actor_value = "role:reviewer_a"
		first_stage.save(update_fields=["execution_mode", "actor_value", "updated_at"])
		DocumentWorkflowStage.objects.create(
			document=document,
			stage_order=1,
			title="Parallel Reviewer B",
			execution_mode="PARALLEL_ANY",
			actor_type="ROLE",
			actor_value="role:reviewer_b",
			required_action="REVIEW",
			status="UNDER_REVIEW",
		)

		first_stage = document.workflow_stages.filter(stage_order=1).order_by("id").first()
		review_response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"stage_id": first_stage.id, "comment": "Any one done"}),
			content_type="application/json",
		)
		self.assertEqual(review_response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_APPROVAL")
		approved_in_first_order = document.workflow_stages.filter(stage_order=1, status="APPROVED").count()
		self.assertEqual(approved_in_first_order, 2)

	def test_next_stage_order_hook_skips_to_requested_stage(self):
		template = Template.objects.create(
			category=self.category,
			title="Branch Hook Template",
			code="BRANCH_HOOK_TEMPLATE",
		)
		revision = TemplateRevision.objects.create(template=template, version=1, is_published=True)
		revision.workflow_stages.create(
			stage_order=1,
			title="Review 1",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
		)
		revision.workflow_stages.create(
			stage_order=2,
			title="Review 2",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer2",
			required_action="REVIEW",
		)
		revision.workflow_stages.create(
			stage_order=3,
			title="Approval",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="approver",
			required_action="APPROVE",
		)
		document = Document.objects.create(
			template_revision=revision,
			document_type=DocumentType.MEMORANDUM,
			title="Branch Doc",
		)
		self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))

		review_response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "skip middle", "next_stage_order": 3}),
			content_type="application/json",
		)
		self.assertEqual(review_response.status_code, 200)
		document.refresh_from_db()
		self.assertEqual(document.status, "UNDER_APPROVAL")
		stage2 = document.workflow_stages.filter(stage_order=2).first()
		self.assertEqual(stage2.status, "DRAFT")

	@override_settings(DOCGEN_ACTOR_RESOLUTION_ADAPTER="DocGen.tests.CustomActorResolutionAdapter")
	def test_submit_uses_configured_actor_resolution_adapter(self):
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Configured Adapter Resolution",
		)
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		self.assertEqual(response.status_code, 200)
		first_stage = document.workflow_stages.order_by("stage_order", "id").first()
		self.assertEqual(first_stage.actor_value, "custom:role:reviewer")

	def test_compass_adapter_extracts_dynamic_manager_actor(self):
		originator = get_user_model().objects.create_user(username="originator_user", password="testpass123")
		document = Document.objects.create(
			template_revision=self.revision,
			document_type=DocumentType.MEMORANDUM,
			title="Dynamic Manager Resolution",
			originator=originator,
		)
		adapter = CompassActorResolutionAdapter()
		with patch.object(adapter, "_request_json", return_value={"data": {"username": "manager_user"}}):
			resolved = adapter.resolve(
				actor_type="DYNAMIC",
				actor_value="N+1_OF_ORIGINATOR",
				document=document,
			)
		self.assertEqual(resolved, "manager_user")

	@override_settings(DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.CapturingNotificationAdapter")
	def test_submit_and_review_emit_document_notification_events(self):
		CapturingNotificationAdapter.events = []
		document = self._create_submitted_document()

		review_response = self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "Reviewed"}),
			content_type="application/json",
		)
		self.assertEqual(review_response.status_code, 200)

		event_types = [event_type for event_type, _payload in CapturingNotificationAdapter.events]
		self.assertIn("docgen.document.submit", event_types)
		self.assertIn("docgen.document.review", event_types)


class DocGenRbacTests(TestCase):
	@override_settings(DOCGEN_ENFORCE_RBAC=True)
	def test_unauthenticated_template_create_forbidden_when_rbac_enabled(self):
		category = TemplateCategory.objects.create(name="RBAC Category", slug="rbac-category")
		payload = {
			"category_id": category.id,
			"title": "Blocked Template",
			"code": "BLOCKED_TEMPLATE",
		}
		response = self.client.post(
			reverse("docgen:template-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 403)

	@override_settings(DOCGEN_ENFORCE_RBAC=True)
	def test_authenticated_template_create_forbidden_without_permission(self):
		category = TemplateCategory.objects.create(name="RBAC Category 2", slug="rbac-category-2")
		user = get_user_model().objects.create_user(username="rbac_no_perm", password="testpass123")
		self.client.force_login(user)
		payload = {
			"category_id": category.id,
			"title": "Blocked Template 2",
			"code": "BLOCKED_TEMPLATE_2",
		}
		response = self.client.post(
			reverse("docgen:template-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 403)

	@override_settings(DOCGEN_ENFORCE_RBAC=True)
	def test_authenticated_template_create_allowed_with_permission(self):
		category = TemplateCategory.objects.create(name="RBAC Category 3", slug="rbac-category-3")
		user = get_user_model().objects.create_user(username="rbac_author", password="testpass123")
		permission = Permission.objects.get(codename="template_author")
		user.user_permissions.add(permission)
		self.client.force_login(user)

		payload = {
			"category_id": category.id,
			"title": "Allowed Template",
			"code": "ALLOWED_TEMPLATE",
		}
		response = self.client.post(
			reverse("docgen:template-collection"),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)


class TemplateStructureApiTests(TestCase):
	def setUp(self):
		category = TemplateCategory.objects.create(name="Structure", slug="structure")
		template = Template.objects.create(
			category=category,
			title="Structure Template",
			code="STRUCT_TEMPLATE",
		)
		self.draft_revision = TemplateRevision.objects.create(template=template, version=1, is_published=False)
		self.published_revision = TemplateRevision.objects.create(template=template, version=2, is_published=True)

	def test_create_placeholder_on_draft_revision(self):
		payload = {
			"name": "recipient",
			"label": "Recipient",
			"field_type": "SHORT_TEXT",
		}
		response = self.client.post(
			reverse("docgen:template-revision-placeholders", kwargs={"revision_id": self.draft_revision.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		self.assertEqual(self.draft_revision.placeholders.count(), 1)

	def test_create_placeholder_blocked_on_published_revision(self):
		payload = {
			"name": "recipient",
			"label": "Recipient",
			"field_type": "SHORT_TEXT",
		}
		response = self.client.post(
			reverse("docgen:template-revision-placeholders", kwargs={"revision_id": self.published_revision.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	def test_update_and_delete_placeholder(self):
		placeholder = self.draft_revision.placeholders.create(
			name="subject",
			label="Subject",
			field_type="SHORT_TEXT",
		)
		update_response = self.client.patch(
			reverse("docgen:template-placeholder-detail", kwargs={"placeholder_id": placeholder.id}),
			data=json.dumps({"label": "Document Subject"}),
			content_type="application/json",
		)
		self.assertEqual(update_response.status_code, 200)
		placeholder.refresh_from_db()
		self.assertEqual(placeholder.label, "Document Subject")

		delete_response = self.client.delete(
			reverse("docgen:template-placeholder-detail", kwargs={"placeholder_id": placeholder.id})
		)
		self.assertEqual(delete_response.status_code, 204)
		self.assertEqual(self.draft_revision.placeholders.count(), 0)

	def test_create_workflow_stage_on_draft_revision(self):
		payload = {
			"stage_order": 1,
			"title": "Initial Review",
			"actor_type": "ROLE",
			"actor_value": "reviewer",
			"required_action": "REVIEW",
		}
		response = self.client.post(
			reverse("docgen:template-revision-workflow-stages", kwargs={"revision_id": self.draft_revision.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		self.assertEqual(self.draft_revision.workflow_stages.count(), 1)

	def test_create_workflow_stage_blocked_on_published_revision(self):
		payload = {
			"stage_order": 1,
			"title": "Initial Review",
			"actor_type": "ROLE",
			"actor_value": "reviewer",
			"required_action": "REVIEW",
		}
		response = self.client.post(
			reverse("docgen:template-revision-workflow-stages", kwargs={"revision_id": self.published_revision.id}),
			data=json.dumps(payload),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	def test_update_and_delete_workflow_stage(self):
		stage = self.draft_revision.workflow_stages.create(
			stage_order=1,
			title="Initial Review",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
		)
		update_response = self.client.patch(
			reverse("docgen:template-workflow-stage-detail", kwargs={"stage_id": stage.id}),
			data=json.dumps({"title": "Desk Review", "required_action": "ENDORSE"}),
			content_type="application/json",
		)
		self.assertEqual(update_response.status_code, 200)
		stage.refresh_from_db()
		self.assertEqual(stage.title, "Desk Review")
		self.assertEqual(stage.required_action, "ENDORSE")

		delete_response = self.client.delete(
			reverse("docgen:template-workflow-stage-detail", kwargs={"stage_id": stage.id})
		)
		self.assertEqual(delete_response.status_code, 204)
		self.assertEqual(self.draft_revision.workflow_stages.count(), 0)


class DocGenSlaProcessingTests(TestCase):
	def setUp(self):
		CapturingNotificationAdapter.events = []
		category = TemplateCategory.objects.create(name="SLA", slug="sla")
		template = Template.objects.create(
			category=category,
			title="SLA Template",
			code="SLA_TEMPLATE",
		)
		revision = TemplateRevision.objects.create(template=template, version=1, is_published=True)
		self.document = Document.objects.create(
			template_revision=revision,
			document_type=DocumentType.MEMORANDUM,
			title="SLA Document",
			status=DocumentStatus.UNDER_REVIEW,
		)

	@override_settings(
		DOCGEN_SLA_REMINDER_MINUTES_BEFORE_DUE=60,
		DOCGEN_SLA_ESCALATION_MINUTES_AFTER_DUE=120,
		DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.CapturingNotificationAdapter",
	)
	def test_sla_reminder_sent_once_before_due(self):
		now = timezone.now()
		stage = DocumentWorkflowStage.objects.create(
			document=self.document,
			stage_order=1,
			title="Review Stage",
			execution_mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			status=DocumentStatus.UNDER_REVIEW,
			due_at=now + timedelta(minutes=30),
		)

		first_result = process_sla_events(now=now)
		second_result = process_sla_events(now=now)

		stage.refresh_from_db()
		self.document.refresh_from_db()
		events = (self.document.metadata or {}).get("events", [])
		self.assertEqual(first_result["reminders_sent"], 1)
		self.assertEqual(second_result["reminders_sent"], 0)
		self.assertEqual(first_result["notifications_sent"], 1)
		self.assertEqual(first_result["notification_failures"], 0)
		self.assertIsNotNone(stage.reminder_sent_at)
		self.assertEqual(len([event for event in events if event.get("action") == "sla_reminder"]), 1)
		self.assertEqual(len(CapturingNotificationAdapter.events), 1)
		self.assertEqual(CapturingNotificationAdapter.events[0][0], "docgen.stage.sla_reminder")

	@override_settings(
		DOCGEN_SLA_REMINDER_MINUTES_BEFORE_DUE=60,
		DOCGEN_SLA_ESCALATION_MINUTES_AFTER_DUE=15,
		DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.CapturingNotificationAdapter",
	)
	def test_sla_escalation_sent_once_after_due_threshold(self):
		now = timezone.now()
		stage = DocumentWorkflowStage.objects.create(
			document=self.document,
			stage_order=1,
			title="Approval Stage",
			execution_mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="approver",
			required_action="APPROVE",
			status=DocumentStatus.UNDER_APPROVAL,
			due_at=now - timedelta(minutes=20),
		)

		first_result = process_sla_events(now=now)
		second_result = process_sla_events(now=now)

		stage.refresh_from_db()
		self.document.refresh_from_db()
		events = (self.document.metadata or {}).get("events", [])
		self.assertEqual(first_result["escalations_sent"], 1)
		self.assertEqual(second_result["escalations_sent"], 0)
		self.assertEqual(first_result["notifications_sent"], 1)
		self.assertEqual(first_result["notification_failures"], 0)
		self.assertIsNotNone(stage.escalated_at)
		self.assertEqual(stage.escalation_level, 1)
		self.assertEqual(len([event for event in events if event.get("action") == "sla_escalation"]), 1)
		self.assertEqual(len(CapturingNotificationAdapter.events), 1)
		self.assertEqual(CapturingNotificationAdapter.events[0][0], "docgen.stage.sla_escalation")

	@override_settings(
		DOCGEN_SLA_REMINDER_MINUTES_BEFORE_DUE=60,
		DOCGEN_SLA_ESCALATION_MINUTES_AFTER_DUE=120,
		DOCGEN_NOTIFICATION_ADAPTER="DocGen.tests.FailingNotificationAdapter",
	)
	def test_sla_notification_failure_does_not_block_processing(self):
		now = timezone.now()
		stage = DocumentWorkflowStage.objects.create(
			document=self.document,
			stage_order=1,
			title="Review Stage",
			execution_mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			status=DocumentStatus.UNDER_REVIEW,
			due_at=now + timedelta(minutes=30),
		)

		result = process_sla_events(now=now)
		stage.refresh_from_db()
		self.assertEqual(result["reminders_sent"], 1)
		self.assertEqual(result["notifications_sent"], 0)
		self.assertEqual(result["notification_failures"], 1)
		self.assertIsNotNone(stage.reminder_sent_at)


class MockHttpResponse:
	def __init__(self, body: bytes, status: int = 200):
		self._body = body
		self.status = status

	def read(self):
		return self._body

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc, tb):
		_ = (exc_type, exc, tb)
		return False


class DocGenExternalAdapterIntegrationTests(TestCase):
	def setUp(self):
		category = TemplateCategory.objects.create(name="Adapters", slug="adapters")
		template = Template.objects.create(
			category=category,
			title="Adapters Template",
			code="ADAPTERS_TEMPLATE",
		)
		revision = TemplateRevision.objects.create(template=template, version=1, is_published=True)
		originator = get_user_model().objects.create_user(username="adapter_originator", password="testpass123")
		self.document = Document.objects.create(
			template_revision=revision,
			document_type=DocumentType.MEMORANDUM,
			title="Adapter Doc",
			originator=originator,
		)

	@override_settings(
		DOCGEN_COMPASS_ORGCHART_ROLE_URL="https://compass.local/org/role",
		DOCGEN_COMPASS_API_TIMEOUT_SECONDS=4,
		DOCGEN_COMPASS_API_TOKEN="token-123",
	)
	@patch("DocGen.adapters.urlopen")
	def test_compass_adapter_role_lookup_calls_external_api(self, mocked_urlopen):
		mocked_urlopen.return_value = MockHttpResponse(body=b'{"data": {"username": "resolved_reviewer"}}')
		adapter = CompassActorResolutionAdapter()

		resolved = adapter.resolve(actor_type="ROLE", actor_value="reviewer", document=self.document)

		self.assertEqual(resolved, "resolved_reviewer")
		self.assertTrue(mocked_urlopen.called)
		request = mocked_urlopen.call_args[0][0]
		self.assertIn("role=reviewer", request.full_url)
		self.assertIn(f"originator_id={self.document.originator_id}", request.full_url)
		self.assertEqual(request.headers.get("Authorization"), "Bearer token-123")

	@override_settings(
		DOCGEN_COMPASS_ORGCHART_MANAGER_URL="https://compass.local/org/manager",
	)
	@patch("DocGen.adapters.urlopen")
	def test_compass_adapter_falls_back_when_external_fails(self, mocked_urlopen):
		mocked_urlopen.side_effect = RuntimeError("service unavailable")
		adapter = CompassActorResolutionAdapter()

		resolved = adapter.resolve(
			actor_type="DYNAMIC",
			actor_value="N+1_OF_ORIGINATOR",
			document=self.document,
		)

		self.assertEqual(resolved, f"dynamic:n+1:{self.document.originator_id}")

	@override_settings(
		DOCGEN_NOTIFICATION_HTTP_ENDPOINT="https://compass.local/events",
		DOCGEN_NOTIFICATION_TIMEOUT_SECONDS=5,
		DOCGEN_NOTIFICATION_API_TOKEN="notify-token",
	)
	@patch("DocGen.notifications.urlopen")
	def test_http_notification_adapter_publishes_event_payload(self, mocked_urlopen):
		mocked_urlopen.return_value = MockHttpResponse(body=b"{}", status=202)
		adapter = HttpNotificationAdapter()
		payload = {"document_id": 123, "status": "UNDER_REVIEW"}

		ok = adapter.publish(event_type="docgen.document.submit", payload=payload)

		self.assertTrue(ok)
		request = mocked_urlopen.call_args[0][0]
		self.assertEqual(request.get_method(), "POST")
		self.assertEqual(request.headers.get("Authorization"), "Bearer notify-token")
		body = json.loads(request.data.decode("utf-8"))
		self.assertEqual(body["event_type"], "docgen.document.submit")
		self.assertEqual(body["payload"], payload)

	@override_settings(DOCGEN_NOTIFICATION_HTTP_ENDPOINT="https://compass.local/events")
	@patch("DocGen.notifications.urlopen")
	def test_http_notification_adapter_returns_false_on_error(self, mocked_urlopen):
		mocked_urlopen.side_effect = RuntimeError("network error")
		adapter = HttpNotificationAdapter()

		ok = adapter.publish(event_type="docgen.stage.sla_escalation", payload={"stage_id": 99})

		self.assertFalse(ok)


class DocumentDetailApiTests(TestCase):
	"""Tests for Phase 7 endpoints: detail, update, withdraw, workflow, PDF, QR, reports."""

	def setUp(self):
		self.category = TemplateCategory.objects.create(name="Detail", slug="detail")
		template = Template.objects.create(
			category=self.category,
			title="Detail Template",
			code="DETAIL_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=1,
			title="Review",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			sla_hours=24,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=2,
			title="Approve",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="approver",
			required_action="APPROVE",
			sla_hours=24,
		)

	def _create_draft(self):
		response = self.client.post(
			reverse("docgen:document-collection"),
			data=json.dumps({
				"template_revision_id": self.revision.id,
				"document_type": "MEMORANDUM",
				"title": "Detail Doc",
				"subject": "Testing detail",
			}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		return Document.objects.get(pk=response.json()["id"])

	def _submit(self, document):
		self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document.id}))
		document.refresh_from_db()
		return document

	def _approve_all(self, document):
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "review"}),
			data=json.dumps({"comment": "ok"}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": document.id, "action": "approve"}),
			data=json.dumps({"comment": "approved"}),
			content_type="application/json",
		)
		document.refresh_from_db()
		return document

	# --- GET /documents/{id}/ ---

	def test_document_detail_returns_full_record(self):
		doc = self._create_draft()
		response = self.client.get(reverse("docgen:document-detail", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["id"], doc.id)
		self.assertEqual(data["title"], "Detail Doc")
		self.assertIn("fields", data)
		self.assertIn("workflow_stages", data)

	def test_document_detail_returns_404_for_unknown(self):
		response = self.client.get(reverse("docgen:document-detail", kwargs={"document_id": 99999}))
		self.assertEqual(response.status_code, 404)

	# --- PATCH /documents/{id}/ ---

	def test_update_draft_document_title(self):
		doc = self._create_draft()
		response = self.client.patch(
			reverse("docgen:document-detail", kwargs={"document_id": doc.id}),
			data=json.dumps({"title": "Updated Title", "subject": "New Subject"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		doc.refresh_from_db()
		self.assertEqual(doc.title, "Updated Title")
		self.assertEqual(doc.subject, "New Subject")

	def test_update_approved_document_is_blocked(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		response = self.client.patch(
			reverse("docgen:document-detail", kwargs={"document_id": doc.id}),
			data=json.dumps({"title": "Attempt Update"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	# --- POST /documents/{id}/withdraw/ ---

	def test_withdraw_draft_document(self):
		doc = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-withdraw", kwargs={"document_id": doc.id}),
			data=json.dumps({"comment": "No longer needed"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		doc.refresh_from_db()
		self.assertEqual(doc.status, "WITHDRAWN")

	def test_withdraw_under_review_document(self):
		doc = self._submit(self._create_draft())
		self.assertEqual(doc.status, "UNDER_REVIEW")
		response = self.client.post(
			reverse("docgen:document-withdraw", kwargs={"document_id": doc.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		doc.refresh_from_db()
		self.assertEqual(doc.status, "WITHDRAWN")

	def test_withdraw_finalized_document_is_blocked(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc.id}))
		doc.refresh_from_db()
		self.assertEqual(doc.status, "FINALIZED")
		response = self.client.post(reverse("docgen:document-withdraw", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 400)

	# --- GET /documents/{id}/workflow/ ---

	def test_document_workflow_lists_stages(self):
		doc = self._submit(self._create_draft())
		response = self.client.get(reverse("docgen:document-workflow", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["document_id"], doc.id)
		self.assertGreaterEqual(data["count"], 1)
		self.assertIn("stages", data)
		first_stage = data["stages"][0]
		self.assertIn("actor_value", first_stage)
		self.assertIn("due_at", first_stage)

	# --- GET /documents/{id}/pdf/ ---

	def test_document_pdf_list_empty_before_finalization(self):
		doc = self._create_draft()
		response = self.client.get(reverse("docgen:document-pdf-list", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["count"], 0)

	def test_document_pdf_list_after_finalization(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc.id}))
		doc.refresh_from_db()
		response = self.client.get(reverse("docgen:document-pdf-list", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["pdfs"][0]["version"], 1)
		self.assertTrue(data["pdfs"][0]["is_active"])

	# --- GET /documents/{id}/qr/ ---

	def test_document_qr_detail_none_before_finalization(self):
		doc = self._create_draft()
		response = self.client.get(reverse("docgen:document-qr-detail", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		self.assertIsNone(response.json()["qr_token"])

	def test_document_qr_detail_after_finalization(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc.id}))
		doc.refresh_from_db()
		response = self.client.get(reverse("docgen:document-qr-detail", kwargs={"document_id": doc.id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertIsNotNone(data["token"])
		self.assertFalse(data["is_revoked"])
		self.assertIn("verify_url", data)

	# --- POST /documents/{id}/qr/revoke/ ---

	def test_qr_revoke_marks_token_revoked(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc.id}))
		doc.refresh_from_db()
		response = self.client.post(
			reverse("docgen:document-qr-revoke", kwargs={"document_id": doc.id}),
			data=json.dumps({"reason": "Document retracted"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertTrue(data["is_revoked"])
		self.assertEqual(data["revoked_reason"], "Document retracted")
		doc.qr_token.refresh_from_db()
		self.assertTrue(doc.qr_token.is_revoked)

	def test_qr_revoke_no_token_returns_404(self):
		doc = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-qr-revoke", kwargs={"document_id": doc.id}),
		)
		self.assertEqual(response.status_code, 404)

	# --- POST /documents/{id}/supersede/ ---

	def _create_finalized(self):
		doc = self._create_draft()
		self._submit(doc)
		self._approve_all(doc)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc.id}))
		doc.refresh_from_db()
		return doc

	def test_supersede_links_documents(self):
		original = self._create_finalized()
		newer = self._create_finalized()
		response = self.client.post(
			reverse("docgen:document-supersede", kwargs={"document_id": original.id}),
			data=json.dumps({"superseding_document_id": newer.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["document_id"], original.id)
		newer.refresh_from_db()
		self.assertEqual(newer.supersedes_id, original.id)

	def test_supersede_draft_is_blocked(self):
		original = self._create_finalized()
		draft_superseding = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-supersede", kwargs={"document_id": original.id}),
			data=json.dumps({"superseding_document_id": draft_superseding.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	def test_supersede_self_is_blocked(self):
		original = self._create_finalized()
		response = self.client.post(
			reverse("docgen:document-supersede", kwargs={"document_id": original.id}),
			data=json.dumps({"superseding_document_id": original.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	# --- GET /documents/ with filters ---

	def test_document_list_filter_by_status(self):
		self._create_draft()
		self._create_draft()
		response = self.client.get(
			reverse("docgen:document-collection"),
			{"status": "DRAFT"},
		)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 2)
		for result in data["results"]:
			self.assertEqual(result["status"], "DRAFT")

	def test_document_list_search_by_title(self):
		self._create_draft()
		other = Document.objects.create(
			template_revision=self.revision,
			document_type="MEMORANDUM",
			title="Unique XYZ Document",
		)
		response = self.client.get(
			reverse("docgen:document-collection"),
			{"q": "XYZ"},
		)
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["id"], other.id)

	# --- Reports ---

	def test_report_summary_returns_counts(self):
		self._create_draft()
		self._create_draft()
		response = self.client.get(reverse("docgen:report-summary"))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertIn("total", data)
		self.assertIn("by_status", data)
		self.assertIn("by_document_type", data)
		self.assertGreaterEqual(data["total"], 2)

	def test_report_sla_returns_stats(self):
		doc = self._submit(self._create_draft())
		response = self.client.get(reverse("docgen:report-sla"))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertIn("total_stages_with_sla", data)
		self.assertIn("overdue", data)
		self.assertIn("breach_rate_percent", data)
		self.assertGreaterEqual(data["total_stages_with_sla"], 1)

	def test_report_pending_returns_by_actor(self):
		doc = self._submit(self._create_draft())
		response = self.client.get(reverse("docgen:report-pending"))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertIn("total_pending", data)
		self.assertIn("by_actor", data)
		self.assertGreaterEqual(data["total_pending"], 1)


class DocumentCommentsAndPreviewTests(TestCase):
	"""Tests for Phase 8: document comments and draft PDF preview."""

	def setUp(self):
		self.category = TemplateCategory.objects.create(name="Comments", slug="comments")
		template = Template.objects.create(
			category=self.category,
			title="Comments Template",
			code="COMMENTS_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=1,
			title="Review",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			sla_hours=24,
		)

	def _create_draft(self):
		response = self.client.post(
			reverse("docgen:document-collection"),
			data=json.dumps({
				"template_revision_id": self.revision.id,
				"document_type": "MEMORANDUM",
				"title": "Comments Doc",
			}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		return response.json()["id"]

	def _submit(self, document_id):
		response = self.client.post(reverse("docgen:document-submit", kwargs={"document_id": document_id}))
		self.assertEqual(response.status_code, 200)
		return document_id

	# --- Comments: empty list ---

	def test_comments_list_empty_on_new_document(self):
		doc_id = self._create_draft()
		response = self.client.get(reverse("docgen:document-comments", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 0)
		self.assertEqual(data["results"], [])

	# --- Comments: add and retrieve ---

	def test_add_comment_to_document(self):
		doc_id = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Please review section 2."}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		data = response.json()
		self.assertEqual(data["body"], "Please review section 2.")
		self.assertIsNone(data["stage_id"])
		self.assertIsNone(data["parent_id"])

	def test_comment_list_after_add(self):
		doc_id = self._create_draft()
		self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "First comment."}),
			content_type="application/json",
		)
		response = self.client.get(reverse("docgen:document-comments", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["body"], "First comment.")

	def test_add_comment_requires_body(self):
		doc_id = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": ""}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 400)

	# --- Comments: stage-scoped ---

	def test_stage_scoped_comment(self):
		doc_id = self._submit(self._create_draft())
		doc = Document.objects.get(pk=doc_id)
		stage = doc.workflow_stages.first()
		response = self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Stage note.", "stage_id": stage.id}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		data = response.json()
		self.assertEqual(data["stage_id"], stage.id)

	def test_filter_comments_by_stage_id(self):
		doc_id = self._submit(self._create_draft())
		doc = Document.objects.get(pk=doc_id)
		stage = doc.workflow_stages.first()
		# Add one stage-scoped and one document-level comment
		self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Stage comment.", "stage_id": stage.id}),
			content_type="application/json",
		)
		self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Doc comment."}),
			content_type="application/json",
		)
		response = self.client.get(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			{"stage_id": stage.id},
		)
		data = response.json()
		self.assertEqual(data["count"], 1)
		self.assertEqual(data["results"][0]["body"], "Stage comment.")

	# --- Comments: threaded replies ---

	def test_threaded_reply(self):
		doc_id = self._create_draft()
		parent_resp = self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Parent comment."}),
			content_type="application/json",
		)
		parent_id = parent_resp.json()["id"]
		reply_resp = self.client.post(
			reverse("docgen:document-comments", kwargs={"document_id": doc_id}),
			data=json.dumps({"body": "Reply.", "parent_id": parent_id}),
			content_type="application/json",
		)
		self.assertEqual(reply_resp.status_code, 201)
		self.assertEqual(reply_resp.json()["parent_id"], parent_id)

	# --- Comments: 404 for unknown document ---

	def test_comments_404_for_unknown_document(self):
		response = self.client.get(reverse("docgen:document-comments", kwargs={"document_id": 99999}))
		self.assertEqual(response.status_code, 404)

	# --- Draft PDF preview ---

	def test_preview_returns_pdf_content_type(self):
		doc_id = self._create_draft()
		response = self.client.get(reverse("docgen:document-preview", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response["Content-Type"], "application/pdf")

	def test_preview_content_disposition_contains_draft(self):
		doc_id = self._create_draft()
		response = self.client.get(reverse("docgen:document-preview", kwargs={"document_id": doc_id}))
		self.assertIn("DRAFT", response["Content-Disposition"])

	def test_preview_returns_non_empty_bytes(self):
		doc_id = self._create_draft()
		response = self.client.get(reverse("docgen:document-preview", kwargs={"document_id": doc_id}))
		self.assertGreater(len(response.content), 100)

	def test_preview_404_for_unknown_document(self):
		response = self.client.get(reverse("docgen:document-preview", kwargs={"document_id": 99999}))
		self.assertEqual(response.status_code, 404)

	def test_preview_works_on_submitted_document(self):
		doc_id = self._submit(self._create_draft())
		response = self.client.get(reverse("docgen:document-preview", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response["Content-Type"], "application/pdf")


class DocumentAttachmentsAndPdfRegenTests(TestCase):
	"""Tests for Phase 9: document attachments and on-demand PDF regeneration."""

	def setUp(self):
		self.category = TemplateCategory.objects.create(name="Attach", slug="attach")
		template = Template.objects.create(
			category=self.category,
			title="Attach Template",
			code="ATTACH_TEMPLATE",
		)
		self.revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		TemplateWorkflowStage.objects.create(
			revision=self.revision,
			stage_order=1,
			title="Review",
			mode="SEQUENTIAL",
			actor_type="ROLE",
			actor_value="reviewer",
			required_action="REVIEW",
			sla_hours=24,
		)

	def _create_draft(self):
		response = self.client.post(
			reverse("docgen:document-collection"),
			data=json.dumps({
				"template_revision_id": self.revision.id,
				"document_type": "MEMORANDUM",
				"title": "Attach Doc",
			}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 201)
		return response.json()["id"]

	def _finalize(self, doc_id):
		"""Submit → approve → finalize a document."""
		self.client.post(reverse("docgen:document-submit", kwargs={"document_id": doc_id}))
		self.client.post(
			reverse("docgen:document-action", kwargs={"document_id": doc_id, "action": "review"}),
			data=json.dumps({}),
			content_type="application/json",
		)
		self.client.post(reverse("docgen:document-finalize", kwargs={"document_id": doc_id}))
		return doc_id

	# --- Attachments: empty list ---

	def test_attachment_list_empty(self):
		doc_id = self._create_draft()
		response = self.client.get(reverse("docgen:document-attachments", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["count"], 0)

	# --- Attachments: upload ---

	def test_upload_attachment(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		doc_id = self._create_draft()
		f = SimpleUploadedFile("report.txt", b"file content here", content_type="text/plain")
		response = self.client.post(
			reverse("docgen:document-attachments", kwargs={"document_id": doc_id}),
			data={"file": f, "description": "Supporting report"},
		)
		self.assertEqual(response.status_code, 201)
		data = response.json()
		self.assertEqual(data["filename"], "report.txt")
		self.assertEqual(data["mime_type"], "text/plain")

	def test_attachment_appears_in_list(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		doc_id = self._create_draft()
		f = SimpleUploadedFile("notes.txt", b"notes", content_type="text/plain")
		self.client.post(
			reverse("docgen:document-attachments", kwargs={"document_id": doc_id}),
			data={"file": f},
		)
		response = self.client.get(reverse("docgen:document-attachments", kwargs={"document_id": doc_id}))
		self.assertEqual(response.json()["count"], 1)
		self.assertEqual(response.json()["results"][0]["filename"], "notes.txt")

	def test_upload_requires_file(self):
		doc_id = self._create_draft()
		response = self.client.post(
			reverse("docgen:document-attachments", kwargs={"document_id": doc_id}),
			data={},
		)
		self.assertEqual(response.status_code, 400)

	# --- Attachments: delete ---

	def test_delete_attachment(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		doc_id = self._create_draft()
		f = SimpleUploadedFile("delete_me.txt", b"data", content_type="text/plain")
		upload_resp = self.client.post(
			reverse("docgen:document-attachments", kwargs={"document_id": doc_id}),
			data={"file": f},
		)
		attachment_id = upload_resp.json()["id"]
		del_resp = self.client.delete(
			reverse("docgen:document-attachment-detail", kwargs={"document_id": doc_id, "attachment_id": attachment_id})
		)
		self.assertEqual(del_resp.status_code, 204)
		self.assertFalse(DocumentAttachment.objects.filter(pk=attachment_id).exists())

	def test_delete_attachment_404_wrong_document(self):
		from django.core.files.uploadedfile import SimpleUploadedFile
		doc1_id = self._create_draft()
		doc2_id = self._create_draft()
		f = SimpleUploadedFile("file.txt", b"x", content_type="text/plain")
		upload_resp = self.client.post(
			reverse("docgen:document-attachments", kwargs={"document_id": doc1_id}),
			data={"file": f},
		)
		attachment_id = upload_resp.json()["id"]
		# Try to delete attachment from wrong document
		del_resp = self.client.delete(
			reverse("docgen:document-attachment-detail", kwargs={"document_id": doc2_id, "attachment_id": attachment_id})
		)
		self.assertEqual(del_resp.status_code, 404)

	# --- On-demand PDF regeneration ---

	def test_pdf_regen_on_finalized_document(self):
		doc_id = self._finalize(self._create_draft())
		response = self.client.post(reverse("docgen:document-pdf-generate", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 201)
		data = response.json()
		self.assertIn("pdf_version", data)
		self.assertGreater(data["pdf_version"], 1)  # v1 was from finalize, this is v2

	def test_pdf_regen_blocked_on_draft(self):
		doc_id = self._create_draft()
		response = self.client.post(reverse("docgen:document-pdf-generate", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 400)

	def test_pdf_regen_creates_new_version(self):
		doc_id = self._finalize(self._create_draft())
		# First regen
		self.client.post(reverse("docgen:document-pdf-generate", kwargs={"document_id": doc_id}))
		# Second regen
		response = self.client.post(reverse("docgen:document-pdf-generate", kwargs={"document_id": doc_id}))
		self.assertEqual(response.status_code, 201)
		self.assertGreaterEqual(response.json()["pdf_version"], 3)

	# --- QR image in finalized PDF ---

	def test_finalized_pdf_contains_qr_data(self):
		"""PDF bytes for a finalized doc should be non-empty (QR image embedded)."""
		doc_id = self._finalize(self._create_draft())
		doc = Document.objects.get(pk=doc_id)
		active_pdf = doc.pdf_versions.filter(is_active=True).first()
		self.assertIsNotNone(active_pdf)
		active_pdf.file.open("rb")
		content = active_pdf.file.read()
		active_pdf.file.close()
		self.assertGreater(len(content), 500)  # non-trivial PDF with QR
