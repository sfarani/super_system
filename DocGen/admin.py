from django.contrib import admin
from .models import (
	Document,
	DocumentField,
	DocumentPDF,
	DocumentQRToken,
	DocumentRevision,
	DocumentWorkflowStage,
	ReferenceNumberSequence,
	Template,
	TemplateCategory,
	TemplatePlaceholder,
	TemplateRevision,
	TemplateWorkflowStage,
)


@admin.register(TemplateCategory)
class TemplateCategoryAdmin(admin.ModelAdmin):
	list_display = ("name", "slug", "is_active", "created_at")
	search_fields = ("name", "slug")
	list_filter = ("is_active",)


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
	list_display = ("code", "title", "status", "is_locked", "published_at")
	search_fields = ("code", "title")
	list_filter = ("status", "is_locked", "category")


@admin.register(TemplateRevision)
class TemplateRevisionAdmin(admin.ModelAdmin):
	list_display = ("template", "version", "is_published", "created_at")
	list_filter = ("is_published",)


@admin.register(TemplatePlaceholder)
class TemplatePlaceholderAdmin(admin.ModelAdmin):
	list_display = ("revision", "name", "field_type", "is_required", "display_order")
	list_filter = ("field_type", "is_required")
	search_fields = ("name", "label")


@admin.register(TemplateWorkflowStage)
class TemplateWorkflowStageAdmin(admin.ModelAdmin):
	list_display = ("revision", "stage_order", "title", "mode", "actor_type", "required_action")
	list_filter = ("mode", "actor_type", "required_action")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
	list_display = (
		"id",
		"title",
		"document_type",
		"status",
		"reference_number",
		"originator",
		"created_at",
	)
	list_filter = ("document_type", "status", "classification")
	search_fields = ("title", "reference_number", "subject")


@admin.register(DocumentField)
class DocumentFieldAdmin(admin.ModelAdmin):
	list_display = ("document", "placeholder_name", "updated_at")
	search_fields = ("placeholder_name",)


@admin.register(DocumentRevision)
class DocumentRevisionAdmin(admin.ModelAdmin):
	list_display = ("document", "version", "created_by", "created_at")


@admin.register(DocumentWorkflowStage)
class DocumentWorkflowStageAdmin(admin.ModelAdmin):
	list_display = ("document", "stage_order", "title", "required_action", "status", "acted_by", "acted_at")
	list_filter = ("required_action", "status", "actor_type")


@admin.register(DocumentQRToken)
class DocumentQRTokenAdmin(admin.ModelAdmin):
	list_display = ("document", "token", "is_revoked", "revoked_at", "created_at")
	list_filter = ("is_revoked",)
	search_fields = ("token", "document__reference_number")


@admin.register(DocumentPDF)
class DocumentPDFAdmin(admin.ModelAdmin):
	list_display = ("document", "version", "is_active", "sha256_hash", "created_at")
	list_filter = ("is_active",)


@admin.register(ReferenceNumberSequence)
class ReferenceNumberSequenceAdmin(admin.ModelAdmin):
	list_display = ("doc_type_code", "year", "current_value")
	list_filter = ("year", "doc_type_code")
