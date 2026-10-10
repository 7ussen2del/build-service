"""
Build model: the single table owned by this service.

Service boundary: ``application_id`` and ``owner_id`` are plain integers that
mirror rows in *other* services' databases. They are deliberately NOT
ForeignKeys — this service never joins against, or queries, another service.
Everything else on the row is a per-build snapshot, authoritative only for the
build it belongs to.
"""
from __future__ import annotations

import uuid

from django.db import models


class BuildStatus(models.TextChoices):
    """Lifecycle states. SUCCESS and FAILED are terminal."""

    QUEUED = "QUEUED", "Queued"
    PREPARING = "PREPARING", "Preparing"
    BUILDING = "BUILDING", "Building"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"

    @classmethod
    def terminal(cls) -> frozenset[str]:
        return frozenset({cls.SUCCESS.value, cls.FAILED.value})

    @classmethod
    def non_terminal(cls) -> frozenset[str]:
        return frozenset({cls.QUEUED.value, cls.PREPARING.value, cls.BUILDING.value})


class ApplicationType(models.TextChoices):
    DJANGO = "django", "Django"
    FLASK = "flask", "Flask"
    NODE = "node", "Node"
    REACT = "react", "React"


class Build(models.Model):
    # --- Identity -------------------------------------------------------
    #: Stable public identifier, safe to hand to other services.
    build_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    #: Idempotency key supplied by the Application Service.
    request_id = models.UUIDField(unique=True)
    application_id = models.BigIntegerField(db_index=True)
    owner_id = models.BigIntegerField(db_index=True)

    status = models.CharField(
        max_length=16,
        choices=BuildStatus.choices,
        default=BuildStatus.QUEUED,
        db_index=True,
    )

    # --- Snapshot from the request --------------------------------------
    name = models.CharField(max_length=100)
    repository_url = models.CharField(max_length=500)
    branch = models.CharField(max_length=255)
    application_type = models.CharField(max_length=20, choices=ApplicationType.choices)
    source_directory = models.CharField(max_length=500)
    requirements_file = models.CharField(max_length=500, null=True, blank=True)
    #: Nullable so an out-of-range port can still be recorded on a FAILED row.
    port = models.IntegerField(null=True, blank=True)
    run_command = models.TextField(blank=True, default="")

    # --- Resolved during preparation ------------------------------------
    commit_sha = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    template_name = models.CharField(max_length=50, blank=True, default="")
    template_version = models.CharField(max_length=20, blank=True, default="")

    # --- Result ----------------------------------------------------------
    image_repository = models.CharField(max_length=255, null=True, blank=True)
    image_tag = models.CharField(max_length=128, null=True, blank=True)
    image_digest = models.CharField(max_length=80, null=True, blank=True)
    error_code = models.CharField(max_length=50, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    # --- Tracking --------------------------------------------------------
    #: Reference to the build engine's own log (Jenkins queue/build URL).
    #: Logs are never copied into this DB; they are fetched on demand.
    log_reference = models.CharField(max_length=500, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "builds"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["status", "created_at"], name="build_status_created_idx"),
            models.Index(fields=["application_id", "-created_at"], name="build_app_created_idx"),
        ]

    def __str__(self) -> str:
        return f"Build {self.build_id} [{self.status}]"

    # --- Convenience -----------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in BuildStatus.terminal()

    @property
    def short_sha(self) -> str:
        """First 7 characters of the resolved commit, or "" when unresolved."""
        return (self.commit_sha or "")[:7]

    @property
    def image_reference(self) -> str | None:
        """``repository:tag``, or None before the image name is computed."""
        if not self.image_repository or not self.image_tag:
            return None
        return f"{self.image_repository}:{self.image_tag}"
