"""Initial Build table."""
from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Build",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("build_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("request_id", models.UUIDField(unique=True)),
                ("application_id", models.BigIntegerField(db_index=True)),
                ("owner_id", models.BigIntegerField(db_index=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("PREPARING", "Preparing"),
                            ("BUILDING", "Building"),
                            ("SUCCESS", "Success"),
                            ("FAILED", "Failed"),
                        ],
                        db_index=True,
                        default="QUEUED",
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(max_length=100)),
                ("repository_url", models.CharField(max_length=500)),
                ("branch", models.CharField(max_length=255)),
                (
                    "application_type",
                    models.CharField(
                        choices=[
                            ("django", "Django"),
                            ("flask", "Flask"),
                            ("node", "Node"),
                            ("react", "React"),
                        ],
                        max_length=20,
                    ),
                ),
                ("source_directory", models.CharField(max_length=500)),
                ("requirements_file", models.CharField(blank=True, max_length=500, null=True)),
                ("port", models.IntegerField(blank=True, null=True)),
                ("run_command", models.TextField(blank=True, default="")),
                ("commit_sha", models.CharField(blank=True, db_index=True, max_length=40, null=True)),
                ("template_name", models.CharField(blank=True, default="", max_length=50)),
                ("template_version", models.CharField(blank=True, default="", max_length=20)),
                ("image_repository", models.CharField(blank=True, max_length=255, null=True)),
                ("image_tag", models.CharField(blank=True, max_length=128, null=True)),
                ("image_digest", models.CharField(blank=True, max_length=80, null=True)),
                ("error_code", models.CharField(blank=True, default="", max_length=50)),
                ("error_message", models.TextField(blank=True, default="")),
                ("log_reference", models.CharField(blank=True, max_length=500, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "builds",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="build",
            index=models.Index(
                fields=["status", "created_at"], name="build_status_created_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="build",
            index=models.Index(
                fields=["application_id", "-created_at"], name="build_app_created_idx"
            ),
        ),
    ]
