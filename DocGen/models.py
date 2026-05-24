# pyright: reportAttributeAccessIssue=false

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone


class TimeStampedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		abstract = True


class TemplateStatus(models.TextChoices):
	DRAFT = "DRAFT", "Draft"
	PUBLISHED = "PUBLISHED", "Published"
	RETIRED = "RETIRED", "Retired"


class PlaceholderType(models.TextChoices):
	SHORT_TEXT = "SHORT_TEXT", "Short Text"
	LONG_TEXT = "LONG_TEXT", "Long Text"
	DATE = "DATE", "Date"
	NUMBER = "NUMBER", "Number"
	CURRENCY = "CURRENCY", "Currency"
	DROPDOWN = "DROPDOWN", "Dropdown"
	USER_LOOKUP = "USER_LOOKUP", "User Lookup"
	DEPARTMENT_LOOKUP = "DEPARTMENT_LOOKUP", "Department Lookup"
	TABLE = "TABLE", "Table"
	SIGNATURE = "SIGNATURE", "Signature"
	REFERENCE_NUMBER = "REFERENCE_NUMBER", "Reference Number"
	ATTACHMENT_LINK = "ATTACHMENT_LINK", "Attachment Link"
	CONDITIONAL_BLOCK = "CONDITIONAL_BLOCK", "Conditional Block"


class WorkflowStageMode(models.TextChoices):
	SEQUENTIAL = "SEQUENTIAL", "Sequential"
	PARALLEL_ALL = "PARALLEL_ALL", "Parallel (All Must Act)"
	PARALLEL_ANY = "PARALLEL_ANY", "Parallel (Any One Can Act)"


class WorkflowActorType(models.TextChoices):
	USER = "USER", "Named User"
	ROLE = "ROLE", "Role"
	POSITION = "POSITION", "Position"
	DYNAMIC = "DYNAMIC", "Dynamic"


class WorkflowActionType(models.TextChoices):
	REVIEW = "REVIEW", "Review"
	ENDORSE = "ENDORSE", "Endorse"
	APPROVE = "APPROVE", "Approve"
	APPROVE_WITH_COMMENTS = "APPROVE_WITH_COMMENTS", "Approve With Comments"
	RETURN = "RETURN", "Return for Correction"
	REJECT = "REJECT", "Reject"
	DELEGATE = "DELEGATE", "Delegate"
	REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION", "Request Clarification"


class DocumentStatus(models.TextChoices):
	DRAFT = "DRAFT", "Draft"
	UNDER_REVIEW = "UNDER_REVIEW", "Under Review"
	UNDER_APPROVAL = "UNDER_APPROVAL", "Under Approval"
	APPROVED = "APPROVED", "Approved"
	FINALIZED = "FINALIZED", "Finalized"
	ARCHIVED = "ARCHIVED", "Archived"
	RETURNED = "RETURNED", "Returned"
	REJECTED = "REJECTED", "Rejected"
	WITHDRAWN = "WITHDRAWN", "Withdrawn"


class DocumentType(models.TextChoices):
	OFFICIAL_LETTER = "OFFICIAL_LETTER", "Official Letter"
	MEMORANDUM = "MEMORANDUM", "Memorandum"
	OFFICE_ORDER = "OFFICE_ORDER", "Office Order"
	CIRCULAR = "CIRCULAR", "Circular / Notice"
	ENDORSEMENT = "ENDORSEMENT", "Endorsement"
	CERTIFICATE = "CERTIFICATE", "Certificate"
	MEETING_RECORD = "MEETING_RECORD", "Meeting Agenda / Minutes"
	SHOW_CAUSE = "SHOW_CAUSE", "Show Cause / Explanation Letter"


class TemplateCategory(TimeStampedModel):
	name = models.CharField(max_length=120, unique=True)
	slug = models.SlugField(max_length=140, unique=True)
	description = models.TextField(blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return self.name


class TokenScope(models.TextChoices):
	GLOBAL = "GLOBAL", "Global"
	GROUP = "GROUP", "Department (Group)"
	USER = "USER", "User"


class TemplateTokenDefinition(TimeStampedModel):
	key = models.CharField(max_length=100, unique=True)
	label = models.CharField(max_length=180)
	description = models.TextField(blank=True)
	is_active = models.BooleanField(default=True)
	allow_document_override = models.BooleanField(default=True)

	class Meta:
		ordering = ["key"]

	def __str__(self):
		return self.key


class TemplateTokenValue(TimeStampedModel):
	definition = models.ForeignKey(
		TemplateTokenDefinition,
		on_delete=models.CASCADE,
		related_name="values",
	)
	scope = models.CharField(max_length=16, choices=TokenScope.choices)
	group = models.ForeignKey(
		"auth.Group",
		on_delete=models.CASCADE,
		null=True,
		blank=True,
		related_name="docgen_template_token_values",
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		null=True,
		blank=True,
		related_name="docgen_template_token_values",
	)
	value = models.TextField(blank=True)

	class Meta:
		ordering = ["definition__key", "scope", "group__name", "user__username"]
		constraints = [
			models.UniqueConstraint(
				fields=["definition", "scope"],
				condition=models.Q(scope=TokenScope.GLOBAL),
				name="uniq_docgen_token_value_global",
			),
			models.UniqueConstraint(
				fields=["definition", "scope", "group"],
				condition=models.Q(scope=TokenScope.GROUP),
				name="uniq_docgen_token_value_group",
			),
			models.UniqueConstraint(
				fields=["definition", "scope", "user"],
				condition=models.Q(scope=TokenScope.USER),
				name="uniq_docgen_token_value_user",
			),
		]

	def clean(self):
		super().clean()
		if self.scope == TokenScope.GLOBAL and (self.group_id or self.user_id):
			raise ValidationError("Global token values cannot target group or user.")
		if self.scope == TokenScope.GROUP and (not self.group_id or self.user_id):
			raise ValidationError("Group token values require group and must not target user.")
		if self.scope == TokenScope.USER and (not self.user_id or self.group_id):
			raise ValidationError("User token values require user and must not target group.")

	def __str__(self):
		target = "global"
		if self.scope == TokenScope.GROUP and self.group_id:
			target = f"group:{self.group.name}"
		elif self.scope == TokenScope.USER and self.user_id:
			target = f"user:{self.user}"
		return f"{self.definition.key} [{target}]"


class TemplateTokenAuditAction(models.TextChoices):
	CREATE = "CREATE", "Create"
	UPDATE = "UPDATE", "Update"
	DELETE = "DELETE", "Delete"


class TemplateTokenAuditLog(TimeStampedModel):
	definition = models.ForeignKey(
		TemplateTokenDefinition,
		on_delete=models.CASCADE,
		related_name="audit_logs",
	)
	scope = models.CharField(max_length=16, choices=TokenScope.choices)
	group = models.ForeignKey(
		"auth.Group",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_template_token_audit_logs",
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_template_token_audit_logs",
	)
	actor = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_template_token_audit_actions",
	)
	action = models.CharField(max_length=16, choices=TemplateTokenAuditAction.choices)
	old_value = models.TextField(blank=True)
	new_value = models.TextField(blank=True)
	source = models.CharField(max_length=40, default="ui")

	class Meta:
		ordering = ["-created_at", "-id"]

	def __str__(self):
		target = "global"
		if self.scope == TokenScope.GROUP and self.group_id:
			target = f"group:{self.group.name}"
		elif self.scope == TokenScope.USER and self.user_id:
			target = f"user:{self.user}"
		return f"{self.definition.key} {self.action} [{target}]"


class Template(TimeStampedModel):
	category = models.ForeignKey(
		TemplateCategory,
		on_delete=models.PROTECT,
		related_name="templates",
	)
	title = models.CharField(max_length=255)
	code = models.CharField(max_length=80, unique=True)
	description = models.TextField(blank=True)
	status = models.CharField(
		max_length=20,
		choices=TemplateStatus.choices,
		default=TemplateStatus.DRAFT,
	)
	published_at = models.DateTimeField(null=True, blank=True)
	is_locked = models.BooleanField(default=False)

	class Meta:
		ordering = ["title"]
		permissions = [
			("template_author", "Can author DocGen templates"),
			("template_publisher", "Can publish and retire DocGen templates"),
			("admin", "Can administer DocGen operations"),
			("viewer", "Can view finalized DocGen documents"),
		]

	def __str__(self):
		return f"{self.code} - {self.title}"

	def publish_revision(self, revision: "TemplateRevision") -> None:
		if revision.template_id != self.id:
			raise ValidationError("Revision does not belong to this template.")

		with transaction.atomic():
			self.revisions.filter(is_published=True).update(is_published=False)
			revision.is_published = True
			revision.save(update_fields=["is_published", "updated_at"])
			self.status = TemplateStatus.PUBLISHED
			self.is_locked = True
			self.published_at = timezone.now()
			self.save(update_fields=["status", "is_locked", "published_at", "updated_at"])

	def create_next_revision(self, created_by=None) -> "TemplateRevision":
		latest = self.revisions.order_by("-version").first()
		next_version = 1 if latest is None else latest.version + 1
		layout_schema = {} if latest is None else latest.layout_schema
		new_revision = TemplateRevision.objects.create(
			template=self,
			version=next_version,
			layout_schema=layout_schema,
			created_by=created_by,
		)
		if latest is not None:
			placeholders = [
				TemplatePlaceholder(
					revision=new_revision,
					name=placeholder.name,
					label=placeholder.label,
					field_type=placeholder.field_type,
					is_required=placeholder.is_required,
					display_order=placeholder.display_order,
					config=placeholder.config,
				)
				for placeholder in latest.placeholders.all()
			]
			stages = [
				TemplateWorkflowStage(
					revision=new_revision,
					stage_order=stage.stage_order,
					title=stage.title,
					mode=stage.mode,
					actor_type=stage.actor_type,
					actor_value=stage.actor_value,
					required_action=stage.required_action,
					sla_hours=stage.sla_hours,
					is_mandatory=stage.is_mandatory,
				)
				for stage in latest.workflow_stages.all()
			]
			TemplatePlaceholder.objects.bulk_create(placeholders)
			TemplateWorkflowStage.objects.bulk_create(stages)
		return new_revision


class TemplateRevision(TimeStampedModel):
	template = models.ForeignKey(
		Template,
		on_delete=models.CASCADE,
		related_name="revisions",
	)
	version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
	layout_schema = models.JSONField(default=dict, blank=True)
	is_published = models.BooleanField(default=False)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_created_template_revisions",
	)

	class Meta:
		ordering = ["template", "-version"]
		constraints = [
			models.UniqueConstraint(
				fields=["template", "version"],
				name="uniq_docgen_template_revision_version",
			)
		]

	def __str__(self):
		return f"{self.template.code} v{self.version}"

	def clean(self):
		super().clean()
		if not self.pk:
			return

		original = TemplateRevision.objects.get(pk=self.pk)
		if not original.is_published:
			return

		immutable_changed = (
			self.template_id != original.template_id
			or self.version != original.version
			or self.layout_schema != original.layout_schema
			or self.created_by_id != original.created_by_id
		)
		if immutable_changed:
			raise ValidationError("Published template revisions are immutable.")

	def save(self, *args, **kwargs):
		self.full_clean()
		return super().save(*args, **kwargs)


class TemplatePlaceholder(TimeStampedModel):
	revision = models.ForeignKey(
		TemplateRevision,
		on_delete=models.CASCADE,
		related_name="placeholders",
	)
	name = models.CharField(max_length=100)
	label = models.CharField(max_length=180)
	field_type = models.CharField(max_length=40, choices=PlaceholderType.choices)
	is_required = models.BooleanField(default=False)
	display_order = models.PositiveIntegerField(default=1)
	config = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ["display_order", "name"]
		constraints = [
			models.UniqueConstraint(
				fields=["revision", "name"],
				name="uniq_docgen_placeholder_name_per_revision",
			)
		]

	def __str__(self):
		return f"{self.revision} :: {self.name}"


class TemplateWorkflowStage(TimeStampedModel):
	revision = models.ForeignKey(
		TemplateRevision,
		on_delete=models.CASCADE,
		related_name="workflow_stages",
	)
	stage_order = models.PositiveIntegerField(default=1)
	title = models.CharField(max_length=160)
	mode = models.CharField(
		max_length=20,
		choices=WorkflowStageMode.choices,
		default=WorkflowStageMode.SEQUENTIAL,
	)
	actor_type = models.CharField(max_length=20, choices=WorkflowActorType.choices)
	actor_value = models.CharField(max_length=255)
	required_action = models.CharField(
		max_length=30,
		choices=WorkflowActionType.choices,
		default=WorkflowActionType.REVIEW,
	)
	sla_hours = models.PositiveIntegerField(default=48)
	is_mandatory = models.BooleanField(default=True)
	# M2: Optional branch condition evaluated when choosing the next stage after this one.
	# Schema: {"field": "<placeholder_name>", "operator": "eq|neq|in|nin", "value": <any>}
	branch_condition = models.JSONField(null=True, blank=True)

	class Meta:
		ordering = ["stage_order", "id"]
		constraints = [
			models.UniqueConstraint(
				fields=["revision", "stage_order"],
				name="uniq_docgen_workflow_stage_order",
			)
		]

	def __str__(self):
		return f"{self.revision} stage {self.stage_order}: {self.title}"


class TemplateAssetType(models.TextChoices):
	LOGO = "LOGO", "Logo"
	WATERMARK = "WATERMARK", "Watermark"
	LETTERHEAD = "LETTERHEAD", "Letterhead"


class TemplateAsset(TimeStampedModel):
	"""Images (logos, watermarks, letterheads) attached to a template revision."""
	revision = models.ForeignKey(
		TemplateRevision,
		on_delete=models.CASCADE,
		related_name="assets",
	)
	asset_type = models.CharField(max_length=20, choices=TemplateAssetType.choices)
	file = models.FileField(upload_to="docgen/template_assets/")
	label = models.CharField(max_length=180, blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ["asset_type", "label"]

	def __str__(self):
		return f"{self.revision} — {self.asset_type}: {self.label or self.file.name}"


class ReferenceNumberSequence(models.Model):
	year = models.PositiveIntegerField()
	doc_type_code = models.CharField(max_length=20)
	current_value = models.PositiveIntegerField(default=0)

	class Meta:
		constraints = [
			models.UniqueConstraint(
				fields=["year", "doc_type_code"],
				name="uniq_docgen_ref_sequence_year_type",
			)
		]

	def __str__(self):
		return f"{self.doc_type_code}/{self.year} -> {self.current_value}"

	@classmethod
	def next_value(cls, year: int, doc_type_code: str) -> int:
		with transaction.atomic():
			sequence, _ = cls.objects.select_for_update().get_or_create(
				year=year,
				doc_type_code=doc_type_code,
				defaults={"current_value": 0},
			)
			sequence.current_value += 1
			sequence.save(update_fields=["current_value"])
			return sequence.current_value


class Document(TimeStampedModel):
	template_revision = models.ForeignKey(
		TemplateRevision,
		on_delete=models.PROTECT,
		related_name="documents",
	)
	document_type = models.CharField(max_length=30, choices=DocumentType.choices)
	title = models.CharField(max_length=255)
	subject = models.CharField(max_length=255, blank=True)
	originator = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_originated_documents",
	)
	status = models.CharField(
		max_length=20,
		choices=DocumentStatus.choices,
		default=DocumentStatus.DRAFT,
	)
	classification = models.CharField(max_length=40, blank=True)
	reference_number = models.CharField(max_length=120, unique=True, null=True, blank=True)
	metadata = models.JSONField(default=dict, blank=True)
	submitted_at = models.DateTimeField(null=True, blank=True)
	approved_at = models.DateTimeField(null=True, blank=True)
	finalized_at = models.DateTimeField(null=True, blank=True)
	archived_at = models.DateTimeField(null=True, blank=True)
	supersedes = models.ForeignKey(
		"self",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="superseded_by_documents",
	)

	class Meta:
		ordering = ["-created_at"]
		permissions = [
			("originator", "Can create and submit DocGen documents"),
			("reviewer", "Can review DocGen workflow stages"),
			("approver", "Can approve and finalize DocGen documents"),
		]

	def __str__(self):
		return self.reference_number or f"Draft #{self.pk}"

	@staticmethod
	def get_type_code(document_type: str) -> str:
		code_map: dict[str, str] = {
			DocumentType.OFFICIAL_LETTER: "OL",
			DocumentType.MEMORANDUM: "MEM",
			DocumentType.OFFICE_ORDER: "OO",
			DocumentType.CIRCULAR: "CIR",
			DocumentType.ENDORSEMENT: "END",
			DocumentType.CERTIFICATE: "CER",
			DocumentType.MEETING_RECORD: "MM",
			DocumentType.SHOW_CAUSE: "SC",
		}
		return code_map.get(str(document_type), "DOC")

	def assign_reference_number(self, org_code: str = "") -> str:
		if self.reference_number:
			return self.reference_number

		if not org_code:
			from django.conf import settings as _settings
			org_code = str(getattr(_settings, "DOCGEN_ORG_CODE", "HQ")).strip() or "HQ"

		year = timezone.now().year
		type_code = self.get_type_code(self.document_type)
		sequence = ReferenceNumberSequence.next_value(year=year, doc_type_code=type_code)
		self.reference_number = f"COMPASS/{org_code}/{type_code}/{year}/{sequence:05d}"
		self.submitted_at = self.submitted_at or timezone.now()
		self.save(update_fields=["reference_number", "submitted_at", "updated_at"])
		return self.reference_number


class DocumentField(TimeStampedModel):
	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="fields",
	)
	placeholder_name = models.CharField(max_length=100)
	value_text = models.TextField(blank=True)
	value_json = models.JSONField(default=dict, blank=True)

	class Meta:
		constraints = [
			models.UniqueConstraint(
				fields=["document", "placeholder_name"],
				name="uniq_docgen_document_placeholder_value",
			)
		]

	def __str__(self):
		return f"{self.document_id}:{self.placeholder_name}"


class DocumentRevision(TimeStampedModel):
	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="revisions",
	)
	version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
	snapshot = models.JSONField(default=dict, blank=True)
	created_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_created_document_revisions",
	)

	class Meta:
		ordering = ["document", "-version"]
		constraints = [
			models.UniqueConstraint(
				fields=["document", "version"],
				name="uniq_docgen_document_revision_version",
			)
		]

	def __str__(self):
		return f"{self.document} rev {self.version}"


class DocumentWorkflowStage(TimeStampedModel):
	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="workflow_stages",
	)
	stage_order = models.PositiveIntegerField(default=1)
	title = models.CharField(max_length=160)
	execution_mode = models.CharField(
		max_length=20,
		choices=WorkflowStageMode.choices,
		default=WorkflowStageMode.SEQUENTIAL,
	)
	actor_type = models.CharField(max_length=20, choices=WorkflowActorType.choices)
	actor_value = models.CharField(max_length=255)
	required_action = models.CharField(max_length=30, choices=WorkflowActionType.choices)
	status = models.CharField(max_length=20, choices=DocumentStatus.choices, default=DocumentStatus.UNDER_REVIEW)
	acted_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_workflow_actions",
	)
	acted_at = models.DateTimeField(null=True, blank=True)
	due_at = models.DateTimeField(null=True, blank=True)
	reminder_sent_at = models.DateTimeField(null=True, blank=True)
	escalated_at = models.DateTimeField(null=True, blank=True)
	escalation_level = models.PositiveIntegerField(default=0)
	comments = models.TextField(blank=True)

	class Meta:
		ordering = ["stage_order", "id"]

	def __str__(self):
		return f"Doc {self.document_id} stage {self.stage_order}"


class DocumentQRToken(TimeStampedModel):
	document = models.OneToOneField(
		Document,
		on_delete=models.CASCADE,
		related_name="qr_token",
	)
	token = models.CharField(max_length=255, unique=True)
	signed_payload = models.TextField(blank=True)
	is_revoked = models.BooleanField(default=False)
	revoked_reason = models.TextField(blank=True)
	revoked_at = models.DateTimeField(null=True, blank=True)

	def __str__(self):
		return f"QR<{self.document_id}>"


class DocumentPDF(TimeStampedModel):
	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="pdf_versions",
	)
	version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
	file = models.FileField(upload_to="docgen/pdfs/%Y/%m/")
	sha256_hash = models.CharField(max_length=64)
	is_active = models.BooleanField(default=True)
	generated_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_generated_pdfs",
	)

	class Meta:
		ordering = ["document", "-version"]
		constraints = [
			models.UniqueConstraint(
				fields=["document", "version"],
				name="uniq_docgen_pdf_version",
			)
		]

	def __str__(self):
		return f"PDF<{self.document_id}> v{self.version}"


class RetentionPolicy(TimeStampedModel):
	name = models.CharField(max_length=80, unique=True, default="default")
	document_type = models.CharField(
		max_length=30,
		choices=DocumentType.choices,
		null=True,
		blank=True,
		help_text="If set, this policy applies only to the specified document type. Leave blank for a global policy.",
	)
	archive_retention_days = models.PositiveIntegerField(
		default=365,
		validators=[MinValueValidator(1)],
	)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ["-is_active", "name"]

	def __str__(self):
		type_label = f" [{self.document_type}]" if self.document_type else " [global]"
		return f"{self.name}{type_label} ({self.archive_retention_days} days)"

	@classmethod
	def get_active_days(cls, document_type: str = "") -> int | None:
		"""Return retention days. Looks up doc-type-specific policy first, then global."""
		if document_type:
			specific = cls.objects.filter(is_active=True, document_type=document_type).order_by("id").first()
			if specific is not None:
				return specific.archive_retention_days
		global_policy = cls.objects.filter(is_active=True, document_type__isnull=True).order_by("id").first()
		if global_policy is None:
			return None
		return global_policy.archive_retention_days


class DocumentComment(TimeStampedModel):
	"""Threaded, optionally stage-scoped comment on a document."""

	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="comments",
	)
	stage = models.ForeignKey(
		DocumentWorkflowStage,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="stage_comments",
	)
	author = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_comments",
	)
	parent = models.ForeignKey(
		"self",
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="replies",
	)
	body = models.TextField()
	is_internal = models.BooleanField(default=False)

	class Meta:
		ordering = ["created_at"]

	def __str__(self):
		return f"Comment<{self.id}> on Doc {self.document_id}"


class DocumentAttachment(TimeStampedModel):
	"""File attachment associated with a document."""

	document = models.ForeignKey(
		Document,
		on_delete=models.CASCADE,
		related_name="attachments",
	)
	uploaded_by = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name="docgen_attachments",
	)
	file = models.FileField(upload_to="docgen/attachments/%Y/%m/")
	filename = models.CharField(max_length=255)
	file_size = models.PositiveIntegerField(default=0)
	mime_type = models.CharField(max_length=120, blank=True)
	description = models.TextField(blank=True)

	class Meta:
		ordering = ["created_at"]

	def __str__(self):
		return f"Attachment<{self.id}> '{self.filename}' on Doc {self.document_id}"
