from django.urls import path

from . import views

app_name = "docgen"

urlpatterns = [
    path("", views.index, name="index"),
    path("verify/<str:token>/", views.verify_token, name="verify-token"),
    path("templates/", views.template_collection, name="template-collection"),
    path("templates/<int:template_id>/revisions/clone/", views.template_clone_revision, name="template-clone-revision"),
    path("templates/<int:template_id>/publish/", views.template_publish, name="template-publish"),
    path("templates/<int:template_id>/retire/", views.template_retire, name="template-retire"),
    path("template-revisions/<int:revision_id>/placeholders/", views.template_revision_placeholders, name="template-revision-placeholders"),
    path("template-placeholders/<int:placeholder_id>/", views.template_placeholder_detail, name="template-placeholder-detail"),
    path("template-revisions/<int:revision_id>/workflow-stages/", views.template_revision_workflow_stages, name="template-revision-workflow-stages"),
    path("template-workflow-stages/<int:stage_id>/", views.template_workflow_stage_detail, name="template-workflow-stage-detail"),
    path("documents/", views.document_collection, name="document-collection"),
    path("documents/<int:document_id>/fields/", views.document_set_fields, name="document-set-fields"),
    path("documents/<int:document_id>/submit/", views.document_submit, name="document-submit"),
    path("documents/<int:document_id>/actions/<str:action>/", views.document_action, name="document-action"),
    path("documents/<int:document_id>/timeline/", views.document_timeline, name="document-timeline"),
    path("documents/<int:document_id>/finalize/", views.document_finalize, name="document-finalize"),
    path("documents/<int:document_id>/archive/", views.document_archive, name="document-archive"),
    path("documents/archive/", views.document_archive_collection, name="document-archive-collection"),
]
