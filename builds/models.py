"""Domain models for the builds app.

The service owns a single aggregate -- the :class:`Build` -- recording one
attempt to turn a snapshot of an application's repository into a container
image. A row is created the moment a request is accepted and then only moves
forward through the status machine:

    QUEUED -> PREPARING -> BUILDING -> PUSHING -> SUCCESS
                                   \\-> FAILED  (reachable from any active state)

Two rules shape every field below:

* ``application_id`` and ``owner_id`` are plain integers. They name rows owned
  by *other* services and must never become foreign keys across a service
  boundary.
* Nothing here enforces immutability or legal transitions -- that is the
  service layer's job (Phase 3). The model stays a plain record so that a build
  which fails on a malformed payload can still be persisted for the audit
  trail (hence ``port`` and friends being nullable).
"""

from __future__ import annotations

import uuid

from django.db import models


class BuildStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    PREPARING = "PREPARING", "Preparing"
    BUILDING = "BUILDING", "Building"
    PUSHING = "PUSHING", "Pushing"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class ApplicationType(models.TextChoices):
    DJANGO = "django", "Django"
    FLASK = "flask", "Flask"
    NODE = "node", "Node.js"
    REACT = "react", "React"


class NotificationStatus(models.TextChoices):
    """Delivery state of a downstream callback (App service / Deploy service)."""

    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    NOT_REQUIRED = "NOT_REQUIRED", "Not required"


# A build in one of these states will never change again. The reconciler
# (Phase 10) treats every other status as potentially stuck.
TERMINAL_STATUSES = frozenset({BuildStatus.SUCCESS, BuildStatus.FAILED})


class Build(models.Model):
    # --- Identity -----------------------------------------------------------
    # build_id is the public handle other services quote back to us; it is not
    # the primary key, so the internal integer id never leaves this service.
    build_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # request_id is the caller's idempotency key: a retried request carries the
    # same value, and the unique constraint collapses the retry onto one row.
    request_id = models.UUIDField(unique=True, editable=False)

    # --- Ownership (plain integers, never cross-service foreign keys) --------
    application_id = models.BigIntegerField(db_index=True)
    owner_id = models.BigIntegerField(db_index=True)

    # --- State machine ------------------------------------------------------
    status = models.CharField(
        max_length=16,
        choices=BuildStatus.choices,
        default=BuildStatus.QUEUED,
        db_index=True,
    )

    # --- Source snapshot ----------------------------------------------------
    # Captured verbatim at intake so a later edit to the application cannot
    # retroactively change what this build actually built.
    name = models.CharField(max_length=255)
    # A plain CharField on purpose: real URL validation happens in the Phase 2
    # serializer, never at the database level.
    repository_url = models.CharField(max_length=500)
    branch = models.CharField(max_length=255)
    # Full 40-char git SHA. Null until the pipeline resolves the branch; the
    # image tag later uses only the first N characters of it.
    commit_sha = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    application_type = models.CharField(
        max_length=16, choices=ApplicationType.choices, db_index=True
    )
    source_directory = models.CharField(max_length=500, blank=True, default="")
    # Null for node/react, which have no Python requirements file.
    requirements_file = models.CharField(max_length=255, null=True, blank=True)
    run_command = models.CharField(max_length=500, blank=True, default="")
    # Nullable so a payload carrying an out-of-range port can still be recorded
    # as a FAILED build rather than lost; the valid range is enforced in Meta.
    port = models.PositiveIntegerField(null=True, blank=True)

    # --- Template resolution (filled by the pipeline, Phase 5) --------------
    template_name = models.CharField(max_length=100, blank=True, default="")
    template_version = models.CharField(max_length=20, blank=True, default="")
    rendered_dockerfile_hash = models.CharField(max_length=64, null=True, blank=True)

    # --- Image coordinates (filled while PUSHING / on SUCCESS) --------------
    image_repository = models.CharField(max_length=255, blank=True, default="")
    image_tag = models.CharField(max_length=255, blank=True, default="")
    # Set exactly once when the push resolves; the service refuses to change it.
    image_digest = models.CharField(max_length=255, blank=True, default="")

    # --- Executor (the Jenkins job that runs the build) ---------------------
    executor_name = models.CharField(max_length=100, blank=True, default="")
    # Opaque pointer back to the job: queue id, build number, URL, ...
    executor_handle = models.JSONField(null=True, blank=True)
    # Where the full build log lives (e.g. a Jenkins URL) -- never the log, and
    # never anything rendered from a secret.
    log_reference = models.CharField(max_length=500, null=True, blank=True)

    # --- Failure reporting --------------------------------------------------
    error_code = models.CharField(max_length=100, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    # --- Downstream notifications -------------------------------------------
    app_notification_status = models.CharField(
        max_length=16,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )
    app_notification_attempts = models.PositiveIntegerField(default=0)
    app_notified_at = models.DateTimeField(null=True, blank=True)
    deployment_notification_status = models.CharField(
        max_length=16,
        choices=NotificationStatus.choices,
        default=NotificationStatus.PENDING,
    )
    deployment_notification_attempts = models.PositiveIntegerField(default=0)
    deployment_notified_at = models.DateTimeField(null=True, blank=True)

    # --- Timestamps ---------------------------------------------------------
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Indexed: the reconciler sweeps for non-terminal rows untouched for too
    # long, which is exactly a filter on (status, updated_at).
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The hot read path: "every build for application X in status Y".
            models.Index(
                fields=["application_id", "status"], name="build_app_status_idx"
            ),
        ]
        constraints = [
            # A stored port is a real TCP port or it is absent. Enforced in the
            # database so no write path -- service, admin or shell -- can slip a
            # nonsense port past it. ``check=`` keeps this valid on Django 5.0;
            # it becomes ``condition=`` when the project moves to Django 6.
            models.CheckConstraint(
                check=models.Q(port__isnull=True)
                | models.Q(port__range=(1, 65535)),
                name="build_port_in_valid_range",
            ),
        ]

    def __str__(self) -> str:
        return f"Build {self.build_id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        """True once the build has reached SUCCESS or FAILED and is frozen."""
        return self.status in TERMINAL_STATUSES
