from django.urls import path

from . import views
from . import ui_views

app_name = "docgen"

urlpatterns = [
    path("", views.index, name="index"),
    # JSON API — used by the test suite and API consumers
    path("verify/<str:token>/", views.verify_token, name="verify-token"),

    # Template management
    path("templates/", views.template_collection, name="template-collection"),
    path("templates/<int:template_id>/revisions/clone/", views.template_clone_revision, name="template-clone-revision"),
    path("templates/<int:template_id>/publish/", views.template_publish, name="template-publish"),
    path("templates/<int:template_id>/retire/", views.template_retire, name="template-retire"),

    # Template structure (placeholders + workflow stages)
    path("template-revisions/<int:revision_id>/placeholders/", views.template_revision_placeholders, name="template-revision-placeholders"),
    path("template-placeholders/<int:placeholder_id>/", views.template_placeholder_detail, name="template-placeholder-detail"),
    path("template-revisions/<int:revision_id>/workflow-stages/", views.template_revision_workflow_stages, name="template-revision-workflow-stages"),
    path("template-workflow-stages/<int:stage_id>/", views.template_workflow_stage_detail, name="template-workflow-stage-detail"),
    path("template-revisions/<int:revision_id>/layout/", views.template_revision_layout, name="template-revision-layout"),

    # Document collection (list + create)
    path("documents/", views.document_collection, name="document-collection"),

    # Archive collection — must appear before <int:document_id> patterns
    path("documents/archive/", views.document_archive_collection, name="document-archive-collection"),

    # Document detail, update, withdraw
    path("documents/<int:document_id>/", views.document_detail, name="document-detail"),
    path("documents/<int:document_id>/withdraw/", views.document_withdraw, name="document-withdraw"),

    # Document data + lifecycle
    path("documents/<int:document_id>/fields/", views.document_set_fields, name="document-set-fields"),
    path("documents/<int:document_id>/submit/", views.document_submit, name="document-submit"),
    path("documents/<int:document_id>/actions/<str:action>/", views.document_action, name="document-action"),
    path("documents/<int:document_id>/timeline/", views.document_timeline, name="document-timeline"),
    path("documents/<int:document_id>/finalize/", views.document_finalize, name="document-finalize"),
    path("documents/<int:document_id>/archive/", views.document_archive, name="document-archive"),

    # Document workflow, PDF, QR
    path("documents/<int:document_id>/workflow/", views.document_workflow, name="document-workflow"),
    path("documents/<int:document_id>/pdf/", views.document_pdf_list, name="document-pdf-list"),
    path("documents/<int:document_id>/pdf/<int:pdf_id>/download/", views.document_pdf_download, name="document-pdf-download"),
    path("documents/<int:document_id>/preview/", views.document_preview, name="document-preview"),
    path("documents/<int:document_id>/qr/", views.document_qr_detail, name="document-qr-detail"),
    path("documents/<int:document_id>/qr/revoke/", views.document_qr_revoke, name="document-qr-revoke"),
    path("documents/<int:document_id>/supersede/", views.document_supersede, name="document-supersede"),

    # Document comments
    path("documents/<int:document_id>/comments/", views.document_comments, name="document-comments"),

    # Document attachments
    path("documents/<int:document_id>/attachments/", views.document_attachments, name="document-attachments"),
    path("documents/<int:document_id>/attachments/<int:attachment_id>/", views.document_attachment_detail, name="document-attachment-detail"),

    # On-demand PDF re-generation
    path("documents/<int:document_id>/pdf/generate/", views.document_pdf_generate, name="document-pdf-generate"),

    # Reporting
    path("reports/summary/", views.report_summary, name="report-summary"),
    path("reports/sla/", views.report_sla, name="report-sla"),
    path("reports/pending/", views.report_pending, name="report-pending"),

    # -----------------------------------------------------------------------
    # UI (server-rendered HTML pages)
    # -----------------------------------------------------------------------
    path("ui/", ui_views.dashboard, name="ui-dashboard"),
    path("ui/documents/", ui_views.document_list, name="ui-document-list"),
    path("ui/documents/new/", ui_views.document_create, name="ui-document-create"),
    path("ui/documents/<int:document_id>/", ui_views.document_detail_ui, name="ui-document-detail"),
    path("ui/templates/", ui_views.template_list, name="ui-template-list"),
    path("ui/templates/<int:template_id>/", ui_views.template_detail, name="ui-template-detail"),
    path("ui/tokens/", ui_views.token_registry, name="ui-token-registry"),
    path("ui/tokens/sample-csv/", ui_views.token_registry_sample_csv, name="ui-token-registry-sample-csv"),
    # Public QR verification page (renders HTML for browser scans)
    path("ui/verify/<str:token>/", ui_views.verify_page, name="ui-verify-page"),
]
