"""Write-side operations on builds.

All writes go through this module (plan, P1 item 6). The Phase 2 slice is the
single entry point intake needs: create exactly one Build per ``request_id``,
decide QUEUED vs FAILED, and -- only for a freshly created, valid build --
dispatch the pipeline once the row is committed.
"""

from __future__ import annotations

import logging

from celery import current_app
from django.db import transaction
from django.utils import timezone

from builds.models import Build, BuildStatus
from builds.serializers import BuildRequestSerializer

logger = logging.getLogger("builds")

INVALID_PAYLOAD = "INVALID_PAYLOAD"

# The pipeline is dispatched by task name rather than by importing the task,
# so intake never imports the pipeline module (which, from Phase 8, imports
# this one -- a cycle). The queue matches the routing in config/settings.
PIPELINE_TASK_NAME = "builds.pipeline.run_build"
PIPELINE_QUEUE = "build_exec"

# Column -> max length, used only to make a rejected payload storable.
_SNAPSHOT_TEXT_LIMITS = {
    "name": 255,
    "repository_url": 500,
    "branch": 255,
    "application_type": 16,
    "source_directory": 500,
    "run_command": 500,
}


def _dispatch_pipeline(build_id: str) -> None:
    current_app.send_task(PIPELINE_TASK_NAME, args=[build_id], queue=PIPELINE_QUEUE)


def _coerce_text(value: object, max_length: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:max_length]


def _safe_snapshot(payload: dict) -> dict:
    """A best-effort snapshot of a rejected payload, guaranteed to be storable.

    A bad payload must still be recorded (plan, gap #1: ``port`` is nullable for
    exactly this reason), so each value is coerced to something its column will
    accept: text is stringified and truncated, and a port that is not a valid
    1-65535 integer is dropped to NULL.
    """
    snapshot: dict = {
        field: _coerce_text(payload.get(field), limit)
        for field, limit in _SNAPSHOT_TEXT_LIMITS.items()
    }
    snapshot["requirements_file"] = _coerce_text(payload.get("requirements_file"), 255) or None

    port = payload.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        snapshot["port"] = port
    else:
        snapshot["port"] = None
    return snapshot


def _format_errors(errors) -> str:
    parts = []
    for field, messages in errors.items():
        if isinstance(messages, (list, tuple)):
            joined = " ".join(str(m) for m in messages)
        else:
            joined = str(messages)
        parts.append(f"{field}: {joined}")
    return "; ".join(parts)[:1000]


def create_build_from_request(
    *, request_id, application_id: int, owner_id: int, payload: dict
) -> Build:
    """Create (idempotently) the Build for one request and start it if valid.

    The envelope (request_id/application_id/owner_id) has already been validated
    by the caller. This validates the rest of the snapshot:

    * valid   -> Build(QUEUED); on first creation only, dispatch the pipeline
      after commit.
    * invalid -> Build(FAILED, INVALID_PAYLOAD) carrying a best-effort raw
      snapshot; no dispatch.

    Idempotency is keyed on ``request_id``: a redelivery of the same request
    returns the existing row and dispatches nothing (covers Celery redelivery).
    """
    serializer = BuildRequestSerializer(data=payload)
    is_valid = serializer.is_valid()

    if is_valid:
        defaults = {
            "application_id": application_id,
            "owner_id": owner_id,
            "status": BuildStatus.QUEUED,
            **serializer.validated_data,
        }
    else:
        defaults = {
            "application_id": application_id,
            "owner_id": owner_id,
            "status": BuildStatus.FAILED,
            "error_code": INVALID_PAYLOAD,
            "error_message": _format_errors(serializer.errors),
            "finished_at": timezone.now(),
            **_safe_snapshot(payload),
        }

    with transaction.atomic():
        build, created = Build.objects.get_or_create(
            request_id=request_id, defaults=defaults
        )
        if created and is_valid:
            # on_commit, so the pipeline worker can never load a build_id before
            # the row it needs is durably committed.
            build_id = str(build.build_id)
            transaction.on_commit(lambda: _dispatch_pipeline(build_id))

    if not created:
        logger.info(
            "Duplicate request %s -> existing build %s (%s); no dispatch.",
            request_id, build.build_id, build.status,
        )
    elif not is_valid:
        # Phase 11 sends the Application Service notification; the PENDING
        # notification status left on the row is what it will pick up.
        logger.info(
            "Recorded FAILED build %s for request %s: INVALID_PAYLOAD.",
            build.build_id, request_id,
        )

    return build
