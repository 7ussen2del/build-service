"""Write-side operations on builds.

All writes go through this module (plan, P1 item 6). It holds two slices:

* **Intake** (Phase 2) -- ``create_build_from_request``: exactly one Build per
  ``request_id``, QUEUED or FAILED, dispatched once after commit.
* **State machine** (Phase 3) -- ``transition`` and its shortcuts: the single
  place where ``Build.status`` ever changes. Nothing else in the service may
  assign to ``status``; the model deliberately enforces no transition rules of
  its own, so bypassing this module bypasses the state machine entirely.
"""

from __future__ import annotations

import logging

from celery import current_app
from django.db import transaction
from django.utils import timezone

from builds.models import TERMINAL_STATUSES, Build, BuildStatus
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


# --------------------------------------------------------------------------- #
# Intake (Phase 2)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# State machine (Phase 3)
# --------------------------------------------------------------------------- #
# The only legal status changes. A status whose entry is empty is terminal.
#
# Keys and members are the *values* (`.value`) rather than the TextChoices
# members themselves: a member hashes by its enum name, while the database
# hands back a plain string, so an enum-keyed dict would quietly miss.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    BuildStatus.QUEUED.value: frozenset(
        {BuildStatus.PREPARING.value, BuildStatus.FAILED.value}
    ),
    BuildStatus.PREPARING.value: frozenset(
        {BuildStatus.BUILDING.value, BuildStatus.FAILED.value}
    ),
    BuildStatus.BUILDING.value: frozenset(
        {BuildStatus.PUSHING.value, BuildStatus.FAILED.value}
    ),
    BuildStatus.PUSHING.value: frozenset(
        {BuildStatus.SUCCESS.value, BuildStatus.FAILED.value}
    ),
    BuildStatus.SUCCESS.value: frozenset(),
    BuildStatus.FAILED.value: frozenset(),
}

TERMINAL_VALUES: frozenset[str] = frozenset(
    status.value for status in TERMINAL_STATUSES
)

# Facts about *what was built*. Once the pipeline has resolved one, it is the
# permanent record of this build and may never be rewritten -- a replayed or
# late executor callback must not be able to repoint a build at a different
# commit or image.
WRITE_ONCE_FIELDS: frozenset[str] = frozenset(
    {
        "commit_sha",
        "template_name",
        "template_version",
        "rendered_dockerfile_hash",
        "image_repository",
        "image_tag",
        "image_digest",
        "error_code",
        "error_message",
    }
)

# Bookkeeping about *how* the build is being run. Allowed to change as the job
# progresses (a handle is issued, then a log URL becomes known).
_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {"executor_name", "executor_handle", "log_reference"}
)

# Fail closed: a transition may write these columns and nothing else. Identity,
# ownership and the request snapshot are immutable by omission, and status and
# the timestamps are managed by `transition` itself.
WRITABLE_FIELDS: frozenset[str] = WRITE_ONCE_FIELDS | _MUTABLE_FIELDS

# The four states other services are allowed to see (README section 6). The
# internal stages collapse into one, so adding a stage later changes no
# external contract.
EXTERNAL_STATUSES: dict[str, str] = {
    BuildStatus.QUEUED.value: "queued",
    BuildStatus.PREPARING.value: "in_progress",
    BuildStatus.BUILDING.value: "in_progress",
    BuildStatus.PUSHING.value: "in_progress",
    BuildStatus.SUCCESS.value: "success",
    BuildStatus.FAILED.value: "failed",
}


class BuildStateError(Exception):
    """Base class for a refusal coming from the state machine."""


class InvalidTransition(BuildStateError):
    """The requested status change is not permitted."""

    def __init__(
        self,
        message: str,
        *,
        build_id=None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.build_id = build_id
        self.from_status = from_status
        self.to_status = to_status


class ImmutableFieldError(BuildStateError):
    """An attempt to rewrite a field that is already set."""

    def __init__(self, build_id, field: str, stored, attempted) -> None:
        super().__init__(
            f"Build {build_id}: {field} is already {_short(stored)} and cannot "
            f"be changed to {_short(attempted)}."
        )
        self.build_id = build_id
        self.field = field


def _short(value: object, limit: int = 80) -> str:
    """A repr bounded in length, so no oversized value lands in a log line."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _is_unset(value: object) -> bool:
    """The model uses both conventions for "not yet known": NULL and ""."""
    return value is None or value == ""


def _reject_unwritable(fields: dict) -> None:
    unknown = sorted(set(fields) - WRITABLE_FIELDS)
    if unknown:
        # A programming error, not a runtime condition: a typo in a field name
        # must never be silently dropped.
        raise ValueError(
            f"transition() cannot write {unknown}; writable fields are "
            f"{sorted(WRITABLE_FIELDS)}."
        )


def _log_replayed_result(build: Build, fields: dict) -> None:
    conflicts = {
        field: (getattr(build, field), value)
        for field, value in fields.items()
        if getattr(build, field) != value
    }
    if not conflicts:
        logger.info(
            "Build %s is already %s; identical result replayed, nothing to do.",
            build.build_id, build.status,
        )
        return
    # Not an error -- the build stays as it is -- but worth an operator's
    # attention, because two runs reported different facts for one build.
    logger.warning(
        "Build %s is already %s; ignoring a replayed result with differing "
        "fields: %s",
        build.build_id,
        build.status,
        ", ".join(
            f"{field} stored={_short(stored)} received={_short(received)}"
            for field, (stored, received) in sorted(conflicts.items())
        ),
    )


def transition(build_id, to_status: str, **fields) -> Build:
    """Move one build to ``to_status``, atomically, and record ``fields``.

    The row is locked with ``select_for_update`` for the whole check-then-write,
    so two workers racing on the same build are serialised by the database
    rather than by hope: the loser re-reads the winner's status and is refused.

    * A transition outside :data:`ALLOWED_TRANSITIONS` raises
      :class:`InvalidTransition`.
    * ``started_at`` is stamped on first entry to PREPARING, ``finished_at`` on
      any terminal state.
    * A field in :data:`WRITE_ONCE_FIELDS` that already holds a different value
      raises :class:`ImmutableFieldError`.
    * Re-reporting the result a terminal build already has is a successful
      no-op (Celery and executor callbacks are both at-least-once). Reporting a
      *different* terminal result is refused and logged as a warning.

    Raises ``Build.DoesNotExist`` if there is no such build.
    """
    target = str(to_status)
    _reject_unwritable(fields)
    if target not in ALLOWED_TRANSITIONS:
        raise InvalidTransition(
            f"{target!r} is not a known build status.",
            build_id=build_id,
            to_status=target,
        )

    with transaction.atomic():
        build = Build.objects.select_for_update().get(build_id=build_id)
        source = str(build.status)

        if build.is_terminal:
            if target == source:
                _log_replayed_result(build, fields)
                return build
            # A successful build must never be flipped to failed by a late
            # notification error, and a failed one must never claim success.
            logger.warning(
                "Refusing to move build %s out of terminal %s into %s.",
                build.build_id, source, target,
            )
            raise InvalidTransition(
                f"Build {build.build_id} is terminal in {source}; it cannot "
                f"become {target}.",
                build_id=build.build_id,
                from_status=source,
                to_status=target,
            )

        if target not in ALLOWED_TRANSITIONS[source]:
            raise InvalidTransition(
                f"Build {build.build_id}: {source} -> {target} is not an "
                f"allowed transition.",
                build_id=build.build_id,
                from_status=source,
                to_status=target,
            )

        updates: dict[str, object] = {"status": target}
        for field, value in fields.items():
            stored = getattr(build, field)
            if (
                field in WRITE_ONCE_FIELDS
                and not _is_unset(stored)
                and stored != value
            ):
                raise ImmutableFieldError(build.build_id, field, stored, value)
            updates[field] = value

        now = timezone.now()
        if target == BuildStatus.PREPARING.value and build.started_at is None:
            updates["started_at"] = now
        if target in TERMINAL_VALUES:
            updates["finished_at"] = now

        for field, value in updates.items():
            setattr(build, field, value)
        # updated_at carries auto_now, which only fires for a field named in
        # update_fields -- and the Phase 10 reconciler sweeps on updated_at.
        build.save(update_fields=[*updates, "updated_at"])

    logger.info("Build %s: %s -> %s.", build.build_id, source, target)
    return build


def claim_for_preparation(build_id) -> bool:
    """Take ownership of a QUEUED build, returning True only to the winner.

    This is the pipeline's guard against running one build twice. The broker is
    at-least-once, so the same ``build_id`` can legitimately arrive at two
    workers; both call this, the database serialises them, and the loser gets
    False and exits having done nothing.
    """
    try:
        transition(build_id, BuildStatus.PREPARING)
    except Build.DoesNotExist:
        # Nothing to run and nothing that will appear later: a retry would only
        # loop. Loud, because intake should have committed the row first.
        logger.error("Cannot claim build %s: no such build.", build_id)
        return False
    except InvalidTransition:
        logger.info(
            "Build %s is not QUEUED (already claimed or finished); standing down.",
            build_id,
        )
        return False
    return True


def mark_failed(build_id, error_code: str, error_message: str = "") -> Build:
    """Shortcut for the failure path: any active state -> FAILED."""
    return transition(
        build_id,
        BuildStatus.FAILED,
        error_code=error_code,
        error_message=error_message,
    )


def external_status(build: Build) -> str:
    """Collapse the six internal states into the four other services see."""
    return EXTERNAL_STATUSES[str(build.status)]
