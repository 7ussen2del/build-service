"""
Service layer: every database write to ``Build`` goes through this module.

Phase 2 (this section) covers intake — turning an untrusted payload into
exactly one row, idempotently. Later sections own state transitions and the
pipeline itself.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from celery import current_app
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from builds.errors import BuildError, ErrorCode
from builds.models import Build, BuildStatus
from builds.serializers import (
    MAX_BRANCH_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PATH_LENGTH,
    MAX_RUN_COMMAND_LENGTH,
    MAX_URL_LENGTH,
)
from builds.templating import render_dockerfile
from integrations import git, jenkins, naming

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = (
    "name",
    "repository_url",
    "branch",
    "application_type",
    "source_directory",
    "requirements_file",
    "port",
    "run_command",
)

MAX_ERROR_MESSAGE_LENGTH = 2000

# Postgres IntegerField bounds; a wildly out-of-range port must not raise on
# the very INSERT that is meant to record it as invalid.
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


@dataclass(frozen=True)
class IntakeResult:
    """Outcome of one ``builds.start_build`` message."""

    accepted: bool
    build: Build | None = None
    created: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe summary for the Celery result / logs."""
        return {
            "accepted": self.accepted,
            "created": self.created,
            "reason": self.reason,
            "build_id": str(self.build.build_id) if self.build else None,
            "status": self.build.status if self.build else None,
        }


# ---------------------------------------------------------------------------
# Coercion helpers, used only for recording an *invalid* payload
# ---------------------------------------------------------------------------
def _coerce_text(value: Any, max_length: int) -> str:
    """Best-effort text for a NOT NULL column on a rejected payload."""
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).replace("\x00", "")[:max_length]


def _coerce_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _INT32_MIN <= value <= _INT32_MAX else None
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.lstrip("-").isdigit():
            number = int(candidate)
            return number if _INT32_MIN <= number <= _INT32_MAX else None
    return None


def _truncate_message(message: str) -> str:
    return message[:MAX_ERROR_MESSAGE_LENGTH]


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------
def create_queued_build(
    envelope: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> tuple[Build, bool]:
    """
    Create the QUEUED row for a valid payload.

    ``request_id`` is the idempotency key: a duplicate message returns the
    existing row with ``created=False`` so the caller knows not to dispatch
    the pipeline a second time.
    """
    request_id = envelope["request_id"]
    try:
        with transaction.atomic():
            build = Build.objects.create(
                request_id=request_id,
                application_id=envelope["application_id"],
                owner_id=envelope["owner_id"],
                status=BuildStatus.QUEUED,
                **{field: snapshot.get(field) for field in SNAPSHOT_FIELDS},
            )
    except IntegrityError:
        existing = Build.objects.filter(request_id=request_id).first()
        if existing is None:
            raise
        logger.info(
            "build_id=%s request_id=%s duplicate start_build ignored",
            existing.build_id,
            request_id,
        )
        return existing, False
    return build, True


def create_invalid_payload_build(
    envelope: Mapping[str, Any], payload: Mapping[str, Any], errors: Any
) -> tuple[Build, bool]:
    """
    Record a FAILED row for a payload we could identify but not accept.

    The envelope was valid, so there *is* someone to report back to. Snapshot
    values are coerced, never trusted: they exist on the row for debugging.
    """
    request_id = envelope["request_id"]
    message = _truncate_message(f"Invalid build payload: {json.dumps(errors, default=str)}")

    try:
        with transaction.atomic():
            build = Build.objects.create(
                request_id=request_id,
                application_id=envelope["application_id"],
                owner_id=envelope["owner_id"],
                status=BuildStatus.FAILED,
                error_code=ErrorCode.INVALID_PAYLOAD,
                error_message=message,
                finished_at=timezone.now(),
                name=_coerce_text(payload.get("name"), MAX_NAME_LENGTH),
                repository_url=_coerce_text(payload.get("repository_url"), MAX_URL_LENGTH),
                branch=_coerce_text(payload.get("branch"), MAX_BRANCH_LENGTH),
                application_type=_coerce_text(payload.get("application_type"), 20),
                source_directory=_coerce_text(payload.get("source_directory"), MAX_PATH_LENGTH),
                requirements_file=_coerce_text(payload.get("requirements_file"), MAX_PATH_LENGTH)
                or None,
                port=_coerce_port(payload.get("port")),
                run_command=_coerce_text(payload.get("run_command"), MAX_RUN_COMMAND_LENGTH),
            )
    except IntegrityError:
        existing = Build.objects.filter(request_id=request_id).first()
        if existing is None:
            raise
        return existing, False
    return build, True


# ---------------------------------------------------------------------------
# Phase 3 — the state machine
#
# Every status change in the service goes through `transition` or
# `claim_for_preparation`. Nothing else may assign Build.status.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    BuildStatus.QUEUED: frozenset({BuildStatus.PREPARING, BuildStatus.FAILED}),
    BuildStatus.PREPARING: frozenset({BuildStatus.BUILDING, BuildStatus.FAILED}),
    BuildStatus.BUILDING: frozenset({BuildStatus.SUCCESS, BuildStatus.FAILED}),
    # Terminal: nothing leaves these.
    BuildStatus.SUCCESS: frozenset(),
    BuildStatus.FAILED: frozenset(),
}

#: Facts about what was built. Once recorded they are never rewritten, so a
#: late or hostile message cannot repoint a build at a different artifact.
WRITE_ONCE_FIELDS = ("commit_sha", "image_repository", "image_tag", "image_digest")

_MODEL_FIELD_NAMES = frozenset(field.name for field in Build._meta.fields)


class InvalidTransition(Exception):
    """Raised when a caller asks for a state change the machine forbids."""

    def __init__(self, build_id: Any, from_status: str, to_status: str) -> None:
        self.build_id = str(build_id)
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"build {build_id}: {from_status} -> {to_status} is not allowed"
        )


class ImmutableFieldError(Exception):
    """Raised on an attempt to rewrite an already-recorded build fact."""

    def __init__(self, build_id: Any, field: str, current: Any, incoming: Any) -> None:
        self.build_id = str(build_id)
        self.field = field
        super().__init__(
            f"build {build_id}: {field} is already set to {current!r}; "
            f"refusing to overwrite with {incoming!r}"
        )


def _apply_fields(build: Build, fields: Mapping[str, Any]) -> list[str]:
    """Assign non-status fields in memory, returning the names that changed."""
    changed: list[str] = []
    for name, value in fields.items():
        if name == "status":
            raise ValueError("status must be changed via transition(), not as a field")
        if name not in _MODEL_FIELD_NAMES:
            raise ValueError(f"unknown Build field: {name}")

        if name in WRITE_ONCE_FIELDS:
            current = getattr(build, name)
            if current:
                if current != value:
                    raise ImmutableFieldError(build.build_id, name, current, value)
                continue  # identical rewrite: nothing to do

        if getattr(build, name) != value:
            setattr(build, name, value)
            changed.append(name)
    return changed


def _save_changed(build: Build, changed: list[str]) -> None:
    if not changed:
        return
    # dict.fromkeys de-duplicates while preserving order.
    build.save(update_fields=[*dict.fromkeys([*changed, "updated_at"])])


def transition(build_id: Any, to_status: str, **fields: Any) -> Build:
    """
    Move a build to ``to_status``, optionally recording fields in the same lock.

    Re-applying a status a build already holds is a no-op: for terminal states
    the row is left exactly as the first result left it, so a duplicate
    callback can never rewrite the original outcome.
    """
    with transaction.atomic():
        build = Build.objects.select_for_update().get(build_id=build_id)

        if build.status == to_status:
            if build.is_terminal:
                logger.info(
                    "build_id=%s already %s; ignoring duplicate result",
                    build.build_id,
                    build.status,
                )
                return build
            _save_changed(build, _apply_fields(build, fields))
            return build

        if to_status not in ALLOWED_TRANSITIONS[build.status]:
            raise InvalidTransition(build.build_id, build.status, to_status)

        changed = _apply_fields(build, fields)
        build.status = to_status
        changed.append("status")

        now = timezone.now()
        if to_status == BuildStatus.PREPARING and build.started_at is None:
            build.started_at = now
            changed.append("started_at")
        if to_status in BuildStatus.terminal() and build.finished_at is None:
            build.finished_at = now
            changed.append("finished_at")

        _save_changed(build, changed)
        logger.info("build_id=%s -> %s", build.build_id, to_status)
        return build


def record_fields(build_id: Any, **fields: Any) -> Build:
    """Record build facts without touching the status."""
    with transaction.atomic():
        build = Build.objects.select_for_update().get(build_id=build_id)
        _save_changed(build, _apply_fields(build, fields))
        return build


def claim_for_preparation(build_id: Any) -> bool:
    """
    Atomically take ownership of a QUEUED build.

    A single conditional UPDATE is the whole guard: if two workers receive the
    same message, exactly one sees ``rowcount == 1`` and runs the pipeline.
    """
    now = timezone.now()
    claimed = Build.objects.filter(
        build_id=build_id, status=BuildStatus.QUEUED
    ).update(status=BuildStatus.PREPARING, started_at=now, updated_at=now)

    if claimed:
        logger.info("build_id=%s claimed for preparation", build_id)
    else:
        logger.info("build_id=%s not claimable (already claimed or terminal)", build_id)
    return claimed == 1


def mark_failed(
    build_id: Any, error_code: ErrorCode | str, error_message: str = ""
) -> Build:
    """
    Record a terminal failure.

    A build that already reached a terminal state is left alone — in
    particular, nothing may turn a SUCCESS into a FAILED.
    """
    try:
        return transition(
            build_id,
            BuildStatus.FAILED,
            error_code=str(ErrorCode(error_code)),
            error_message=_truncate_message(error_message),
        )
    except InvalidTransition:
        build = Build.objects.get(build_id=build_id)
        logger.warning(
            "build_id=%s refusing to mark FAILED: already terminal (%s)",
            build.build_id,
            build.status,
        )
        return build


# ---------------------------------------------------------------------------
# Phase 6 — pipeline orchestration
#
# This is the only place that drives a build forward. It does not wait for the
# image to be built: it hands the job to Jenkins and exits, and the result
# arrives later as a callback.
# ---------------------------------------------------------------------------
def _retryable_delay(attempt: int) -> float:
    return float(settings.BUILD_RETRY_BASE_DELAY_SECONDS) * (2 ** (attempt - 1))


def with_retries(
    operation: Callable[[], Any],
    *,
    build_id: Any,
    label: str,
    attempts: int | None = None,
) -> Any:
    """
    Run one pipeline step, retrying only errors marked retryable.

    Everything else fails immediately: re-running a clone against a branch
    that does not exist will never start working.
    """
    total = attempts or int(settings.BUILD_RETRY_ATTEMPTS)
    for attempt in range(1, total + 1):
        try:
            return operation()
        except BuildError as exc:
            if not exc.retryable or attempt == total:
                raise
            delay = _retryable_delay(attempt)
            logger.warning(
                "build_id=%s %s attempt %s/%s failed (%s); retrying in %.1fs",
                build_id,
                label,
                attempt,
                total,
                exc.error_code,
                delay,
            )
            if delay > 0:
                time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _prepare_and_trigger(build: Build) -> None:
    """QUEUED work: resolve, validate, render, name, then trigger Jenkins."""
    build_id = build.build_id

    # 1. Pin the exact commit. Everything downstream refers to this SHA.
    commit_sha = with_retries(
        lambda: git.resolve_commit(build.repository_url, build.branch),
        build_id=build_id,
        label="resolve_commit",
    )
    record_fields(build_id, commit_sha=commit_sha)

    # 2. Clone it and render the Dockerfile from validated variables. The
    #    workspace exists only for this block.
    with git.build_workspace(build_id) as repository_root:
        with_retries(
            lambda: git.shallow_clone(
                build.repository_url,
                build.branch,
                repository_root,
                expected_sha=commit_sha,
            ),
            build_id=build_id,
            label="shallow_clone",
        )
        rendered = render_dockerfile(build, repository_root)

    record_fields(
        build_id,
        template_name=rendered.template_name,
        template_version=rendered.template_version,
    )

    # 3. Name the artifact before anything can produce it.
    repository = naming.image_repository(build.owner_id, build.name)
    tag = naming.image_tag(commit_sha)
    record_fields(build_id, image_repository=repository, image_tag=tag)

    # 4. Hand off. The build is BUILDING from the moment Jenkins is asked.
    transition(build_id, BuildStatus.BUILDING)
    reference = with_retries(
        lambda: jenkins.trigger(
            build_id=str(build_id),
            repository_url=build.repository_url,
            commit_sha=commit_sha,
            dockerfile=rendered.dockerfile,
            image_repository=repository,
            image_tag=tag,
        ),
        build_id=build_id,
        label="jenkins_trigger",
    )
    if reference:
        record_fields(build_id, log_reference=reference[:500])


def execute_build_pipeline(build_id: Any) -> dict[str, Any]:
    """
    Take one build from QUEUED to BUILDING, or to FAILED with a reason.

    Safe to run twice: the claim guard means only one caller proceeds.
    """
    if not claim_for_preparation(build_id):
        return {"build_id": str(build_id), "executed": False, "reason": "not_claimable"}

    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        logger.error("build_id=%s vanished after being claimed", build_id)
        return {"build_id": str(build_id), "executed": False, "reason": "unknown_build"}

    try:
        _prepare_and_trigger(build)
    except BuildError as exc:
        logger.warning("build_id=%s failed: %s", build_id, exc)
        failed = mark_failed(build_id, exc.error_code, exc.message)
        transaction.on_commit(lambda: dispatch_result_notifications(failed))
        return {
            "build_id": str(build_id),
            "executed": True,
            "status": failed.status,
            "error_code": failed.error_code,
        }
    except Exception:
        # Stack trace to the log only: error_message is reported to other
        # services and must not leak internals.
        logger.exception("build_id=%s unexpected error during preparation", build_id)
        failed = mark_failed(
            build_id,
            ErrorCode.INTERNAL_ERROR,
            "unexpected error during build preparation",
        )
        transaction.on_commit(lambda: dispatch_result_notifications(failed))
        return {
            "build_id": str(build_id),
            "executed": True,
            "status": failed.status,
            "error_code": failed.error_code,
        }

    return {"build_id": str(build_id), "executed": True, "status": BuildStatus.BUILDING}


# ---------------------------------------------------------------------------
# Phase 9 — outward notifications
#
# These functions build and send payloads. They never write to Build: a
# downstream service being down is not a build failure.
# ---------------------------------------------------------------------------
def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value else None


def build_application_payload(build: Build) -> dict[str, Any]:
    """Result contract for the Application Service (sent for every outcome)."""
    succeeded = build.status == BuildStatus.SUCCESS
    return {
        "build_id": str(build.build_id),
        "application_id": build.application_id,
        "status": "success" if succeeded else "failed",
        # Image fields are null on failure; the keys stay present so the
        # consumer sees one stable schema.
        "image_repository": build.image_repository if succeeded else None,
        "image_tag": build.image_tag if succeeded else None,
        "image_digest": build.image_digest if succeeded else None,
        "commit_sha": build.commit_sha,
        "template_name": build.template_name or None,
        "template_version": build.template_version or None,
        "started_at": _isoformat(build.started_at),
        "finished_at": _isoformat(build.finished_at),
        "error_code": build.error_code or None,
        "error_message": build.error_message or None,
    }


def build_deployment_payload(build: Build) -> dict[str, Any]:
    """
    Deployment request contract (successes only).

    ``build_id`` is stable across attempts and is the Deployment Service's
    dedup key; ``request_id`` is fresh per attempt so a retry is visibly a
    retry rather than a new build.
    """
    return {
        "request_id": str(uuid.uuid4()),
        "build_id": str(build.build_id),
        "application_id": build.application_id,
        "owner_id": build.owner_id,
        "image_repository": build.image_repository,
        "image_tag": build.image_tag,
        "image_digest": build.image_digest,
        "commit_sha": build.commit_sha,
        "application_type": build.application_type,
        "port": build.port,
    }


def send_application_notification(build_id: Any) -> dict[str, Any]:
    """Send the build result to the Application Service."""
    build = Build.objects.get(build_id=build_id)
    if not build.is_terminal:
        logger.error(
            "build_id=%s notify_application called while still %s", build_id, build.status
        )
        return {"sent": False, "reason": "not_terminal"}

    payload = build_application_payload(build)
    current_app.send_task(
        settings.APPLICATION_RESULT_TASK_NAME,
        args=[payload],
        queue=settings.APPLICATION_RESULT_QUEUE,
    )
    logger.info(
        "build_id=%s result reported to the application service status=%s",
        build_id,
        build.status,
    )
    return {"sent": True, "status": build.status}


def send_deployment_notification(build_id: Any) -> dict[str, Any]:
    """Hand a successful image to the Deployment Service."""
    build = Build.objects.get(build_id=build_id)
    if build.status != BuildStatus.SUCCESS:
        # Failed builds are never deployed.
        logger.info(
            "build_id=%s not SUCCESS (%s); skipping deployment notification",
            build_id,
            build.status,
        )
        return {"sent": False, "reason": "not_successful"}

    payload = build_deployment_payload(build)
    current_app.send_task(
        settings.DEPLOYMENT_TASK_NAME,
        args=[payload],
        queue=settings.DEPLOYMENT_QUEUE,
    )
    logger.info(
        "build_id=%s image handed to the deployment service request_id=%s",
        build_id,
        payload["request_id"],
    )
    return {"sent": True, "request_id": payload["request_id"]}


# ---------------------------------------------------------------------------
# Phase 10 — reconciliation of stranded builds
# ---------------------------------------------------------------------------
def _fail_stranded(
    queryset, error_code: ErrorCode, message: str
) -> list[str]:
    failed: list[str] = []
    for build in queryset:
        updated = mark_failed(build.build_id, error_code, message)
        if updated.status == BuildStatus.FAILED and updated.error_code == error_code:
            failed.append(str(build.build_id))
            transaction.on_commit(
                lambda bound=updated: dispatch_result_notifications(bound)
            )
    return failed


def reconcile_stuck_builds() -> dict[str, Any]:
    """
    Periodic sweep for builds nothing will ever finish.

    * PREPARING too long -> the worker died mid-preparation.
    * BUILDING too long  -> Jenkins never called back.
    * QUEUED too long    -> the dispatch was lost; re-dispatch (the claim
      guard makes a double dispatch harmless).

    Also deletes workspaces left behind by a crashed worker.
    """
    now = timezone.now()

    crashed = _fail_stranded(
        Build.objects.filter(
            status=BuildStatus.PREPARING,
            updated_at__lt=now - timedelta(seconds=settings.BUILD_PREPARING_TIMEOUT_SECONDS),
        ),
        ErrorCode.WORKER_CRASHED,
        "preparation did not finish within the allowed time",
    )

    timed_out = _fail_stranded(
        Build.objects.filter(
            status=BuildStatus.BUILDING,
            updated_at__lt=now - timedelta(seconds=settings.BUILD_BUILDING_TIMEOUT_SECONDS),
        ),
        ErrorCode.EXECUTOR_TIMEOUT,
        "the build engine did not report a result within the allowed time",
    )

    redispatched: list[str] = []
    stale_queued = Build.objects.filter(
        status=BuildStatus.QUEUED,
        created_at__lt=now - timedelta(seconds=settings.BUILD_QUEUED_REDISPATCH_SECONDS),
    )
    for build in stale_queued:
        logger.warning("build_id=%s still QUEUED; re-dispatching pipeline", build.build_id)
        dispatch_pipeline(build)
        redispatched.append(str(build.build_id))

    removed_workspaces = git.purge_stale_workspaces(
        int(settings.BUILD_WORKSPACE_MAX_AGE_SECONDS)
    )

    summary = {
        "worker_crashed": crashed,
        "executor_timeout": timed_out,
        "redispatched": redispatched,
        "workspaces_removed": removed_workspaces,
    }
    if any(summary.values()):
        logger.info("reconciler: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Dispatch helpers
#
# Imported lazily: builds.tasks imports this module, so a module-level import
# of the tasks would be circular.
# ---------------------------------------------------------------------------
def dispatch_pipeline(build: Build) -> None:
    """Queue the pipeline for a freshly created QUEUED build."""
    from builds.tasks import run_build_pipeline

    run_build_pipeline.apply_async(
        args=[str(build.build_id)], queue=settings.BUILD_QUEUE_EXEC
    )


def dispatch_result_notifications(build: Build) -> None:
    """
    Report a terminal build outward.

    The Application Service hears about every terminal build; the Deployment
    Service only ever hears about successes.
    """
    from builds.tasks import notify_application, notify_deployment

    notify_application.apply_async(
        args=[str(build.build_id)], queue=settings.BUILD_QUEUE_NOTIFY
    )
    if build.status == BuildStatus.SUCCESS:
        notify_deployment.apply_async(
            args=[str(build.build_id)], queue=settings.BUILD_QUEUE_NOTIFY
        )
