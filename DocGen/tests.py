from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test.utils import override_settings
from datetime import timedelta
import json
from unittest.mock import patch

from .adapters import CompassActorResolutionAdapter
from .models import (
	Document,
	DocumentQRToken,
	DocumentType,
	DocumentWorkflowStage,
	RetentionPolicy,
	Template,
	TemplateCategory,
	TemplateRevision,
	TemplateWorkflowStage,
)


class CustomActorResolutionAdapter:
	def resolve(self, actor_type: str, actor_value: str, document) -> str:
		return f"custom:{actor_type.lower()}:{actor_value}"


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
		revision = TemplateRevision.objects.create(
			template=template,
			version=1,
			is_published=True,
		)
		self.document = Document.objects.create(
			template_revision=revision,
			document_type=DocumentType.MEMORANDUM,
			title="Memo Doc",
			reference_number="COMPASS/HQ/MEM/2026/00001",
			finalized_at=timezone.now(),
		)

	def test_verify_endpoint_returns_valid_document(self):
		token = DocumentQRToken.objects.create(document=self.document, token="abc123")
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": token.token}))

		self.assertEqual(response.status_code, 200)
		data = response.json()
		self.assertTrue(data["valid"])
		self.assertEqual(data["reference_number"], self.document.reference_number)

	def test_verify_endpoint_returns_not_found_for_unknown_token(self):
		response = self.client.get(reverse("docgen:verify-token", kwargs={"token": "not-found"}))
		self.assertEqual(response.status_code, 404)


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
