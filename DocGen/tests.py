from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.core.exceptions import ValidationError
import json

from .models import (
	Document,
	DocumentQRToken,
	DocumentType,
	Template,
	TemplateCategory,
	TemplateRevision,
	TemplateWorkflowStage,
)


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
