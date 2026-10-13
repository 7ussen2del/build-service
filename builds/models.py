"""The Build model: the single table owned by the Build Service (README section 4)."""
import uuid

from django.db import models


class BuildStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    BUILDING = "BUILDING", "Building"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class ApplicationType(models.TextChoices):
    DJANGO = "django", "Django"
    FLASK = "flask", "Flask"
    NODE = "node", "Node.js"
    REACT = "react", "React"


class Build(models.Model):
    # Identity
    build_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_id = models.UUIDField(unique=True)
    application_id = models.BigIntegerField(db_index=True)
    owner_id = models.BigIntegerField(db_index=True)

    status = models.CharField(
        max_length=20, choices=BuildStatus.choices, default=BuildStatus.QUEUED
    )

    # Request snapshot (port and requirements_file nullable)
    name = models.CharField(max_length=100)
    repository_url = models.URLField(max_length=500)
    branch = models.CharField(max_length=200)
    application_type = models.CharField(max_length=20, choices=ApplicationType.choices)
    source_directory = models.CharField(max_length=500)
    requirements_file = models.CharField(max_length=500, null=True, blank=True)
    port = models.IntegerField(null=True, blank=True)
    run_command = models.CharField(max_length=1000)

    # Exact commit, set once resolved
    commit_sha = models.CharField(max_length=40, null=True, blank=True)

    # Image info, set on success
    image_repository = models.CharField(max_length=255, null=True, blank=True)
    image_tag = models.CharField(max_length=100, null=True, blank=True)
    image_digest = models.CharField(max_length=100, null=True, blank=True)

    # Failure info
    error_code = models.CharField(max_length=50, null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    # Timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Build {self.build_id} ({self.status})"
