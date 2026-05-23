from django.urls import path

from . import views

app_name = "docgen"

urlpatterns = [
    path("", views.index, name="index"),
    path("verify/<str:token>/", views.verify_token, name="verify-token"),
]
