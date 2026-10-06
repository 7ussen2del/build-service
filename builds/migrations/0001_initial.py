"""Initial schema for the builds app: the Build aggregate.

Hand-authored to mirror ``builds.models.Build`` field-for-field. It is
equivalent to the output of ``makemigrations builds``; run
``python manage.py makemigrations builds --check --dry-run`` to confirm the
model and this migration are in sync.
"""

from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

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
                (
                    "build_id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, unique=True
                    ),
                ),
                ("request_id", models.UUIDField(editable=False, unique=True)),
                ("application_id", models.BigIntegerField(db_index=True)),
                ("owner_id", models.BigIntegerField(db_index=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("PREPARING", "Preparing"),
                            ("BUILDING", "Building"),
                            ("PUSHING", "Pushing"),
                            ("SUCCESS", "Success"),
                            ("FAILED", "Failed"),
                        ],
                        db_index=True,
                        default="QUEUED",
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("repository_url", models.CharField(max_length=500)),
                ("branch", models.CharField(max_length=255)),
                (
                    "commit_sha",
                    models.CharField(
                        blank=True, db_index=True, max_length=40, null=True
                    ),
                ),
                (
                    "application_type",
                    models.CharField(
                        choices=[
                            ("django", "Django"),
                            ("flask", "Flask"),
                            ("node", "Node.js"),
                            ("react", "React"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "source_directory",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                (
                    "requirements_file",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "run_command",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                ("port", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "template_name",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                (
                    "template_version",
                    models.CharField(blank=True, default="", max_length=20),
                ),
                (
                    "rendered_dockerfile_hash",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                (
                    "image_repository",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "image_tag",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "image_digest",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "executor_name",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                ("executor_handle", models.JSONField(blank=True, null=True)),
                (
                    "log_reference",
                    models.CharField(blank=True, max_length=500, null=True),
                ),
                (
                    "error_code",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                (
                    "app_notification_status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("SENT", "Sent"),
                            ("FAILED", "Failed"),
                            ("NOT_REQUIRED", "Not required"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                (
                    "app_notification_attempts",
                    models.PositiveIntegerField(default=0),
                ),
                ("app_notified_at", models.DateTimeField(blank=True, null=True)),
                (
                    "deployment_notification_status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("SENT", "Sent"),
                            ("FAILED", "Failed"),
                            ("NOT_REQUIRED", "Not required"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                (
                    "deployment_notification_attempts",
                    models.PositiveIntegerField(default=0),
                ),
                (
                    "deployment_notified_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True, db_index=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="build",
            index=models.Index(
                fields=["application_id", "status"], name="build_app_status_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="build",
            constraint=models.CheckConstraint(
                check=models.Q(port__isnull=True)
                | models.Q(port__range=(1, 65535)),
                name="build_port_in_valid_range",
            ),
        ),
    ]
