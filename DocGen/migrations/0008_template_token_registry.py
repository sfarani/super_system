from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("DocGen", "0007_documentattachment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.CreateModel(
            name="TemplateTokenDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("key", models.CharField(max_length=100, unique=True)),
                ("label", models.CharField(max_length=180)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("allow_document_override", models.BooleanField(default=True)),
            ],
            options={"ordering": ["key"]},
        ),
        migrations.CreateModel(
            name="TemplateTokenValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("GLOBAL", "Global"),
                            ("GROUP", "Department (Group)"),
                            ("USER", "User"),
                        ],
                        max_length=16,
                    ),
                ),
                ("value", models.TextField(blank=True)),
                (
                    "definition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="values",
                        to="DocGen.templatetokendefinition",
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="docgen_template_token_values",
                        to="auth.group",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="docgen_template_token_values",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["definition__key", "scope", "group__name", "user__username"]},
        ),
        migrations.AddConstraint(
            model_name="templatetokenvalue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("scope", "GLOBAL")),
                fields=("definition", "scope"),
                name="uniq_docgen_token_value_global",
            ),
        ),
        migrations.AddConstraint(
            model_name="templatetokenvalue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("scope", "GROUP")),
                fields=("definition", "scope", "group"),
                name="uniq_docgen_token_value_group",
            ),
        ),
        migrations.AddConstraint(
            model_name="templatetokenvalue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("scope", "USER")),
                fields=("definition", "scope", "user"),
                name="uniq_docgen_token_value_user",
            ),
        ),
    ]
