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
    path("documents/", views.document_collection, name="document-collection"),
    path("documents/<int:document_id>/fields/", views.document_set_fields, name="document-set-fields"),
    path("documents/<int:document_id>/submit/", views.document_submit, name="document-submit"),
]
