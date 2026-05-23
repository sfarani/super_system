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
