"""
Celery tasks — the only entry points for long-running work.

Nothing here does real work itself: each task is a thin, logged wrapper around
:mod:`builds.services`, so the same logic is callable from tests and from the
reconciler without a broker.

Queues (routed in settings):
    builds.start_build          -> build
    builds.run_build_pipeline   -> build_exec
    builds.reconcile_stuck_builds -> build_exec
    builds.notify_application   -> build_notify
    builds.notify_deployment    -> build_notify
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from celery import shared_task
from django.db import transaction

from builds import services
from builds.models import Build
from builds.serializers import BuildEnvelopeSerializer, BuildSnapshotSerializer
from builds.services import IntakeResult

logger = logging.getLogger(__name__)

NOTIFY_MAX_RETRIES = 5
NOTIFY_MAX_BACKOFF_SECONDS = 60


# ---------------------------------------------------------------------------
# Phase 2 — intake
# ---------------------------------------------------------------------------
@shared_task(name="builds.start_build", bind=True)
def start_build(self, payload: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """
    Entry point called by the Application Service.

    Accepts the snapshot either as a single dict argument or as keyword
    arguments, because the exact call shape is still being agreed with the
    Application Service team (see "Decisions still open").
    """
    data: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
    if not data and kwargs:
        data = dict(kwargs)

    envelope = BuildEnvelopeSerializer(data=data)
    if not envelope.is_valid():
        # No usable identity: there is no row to write and nobody to notify.
        # Log the failing field *names* only — values are untrusted input.
        logger.error(
            "start_build discarded: invalid envelope fields=%s task_id=%s",
            sorted(envelope.errors),
            self.request.id,
        )
        return IntakeResult(accepted=False, reason="invalid_envelope").as_dict()

    snapshot = BuildSnapshotSerializer(data=data)
    if not snapshot.is_valid():
        build, created = services.create_invalid_payload_build(
            envelope.validated_data, data, snapshot.errors
        )
        logger.warning(
            "build_id=%s rejected INVALID_PAYLOAD fields=%s",
            build.build_id,
            sorted(snapshot.errors),
        )
        if created:
            transaction.on_commit(lambda: services.dispatch_result_notifications(build))
        return IntakeResult(
            accepted=False, build=build, created=created, reason="invalid_payload"
        ).as_dict()

    build, created = services.create_queued_build(
        envelope.validated_data, snapshot.validated_data
    )
    if created:
        logger.info(
            "build_id=%s application_id=%s accepted, dispatching pipeline",
            build.build_id,
            build.application_id,
        )
        transaction.on_commit(lambda: services.dispatch_pipeline(build))

    return IntakeResult(
        accepted=True,
        build=build,
        created=created,
        reason="" if created else "duplicate",
    ).as_dict()


# ---------------------------------------------------------------------------
# Phase 6 — pipeline
# ---------------------------------------------------------------------------
@shared_task(name="builds.run_build_pipeline", bind=True)
def run_build_pipeline(self, build_id: str) -> dict[str, Any]:
    """Drive one build from QUEUED to "Jenkins triggered" (or FAILED)."""
    return services.execute_build_pipeline(build_id)


# ---------------------------------------------------------------------------
# Phase 9 — notifications
#
# A notification must never change a build's outcome: once a build is SUCCESS,
# no amount of downstream failure may rewrite it. Retries are bounded and
# exhaustion is logged, not raised.
# ---------------------------------------------------------------------------
def _retry_or_give_up(task: Any, exc: Exception, build_id: str, label: str) -> dict[str, Any]:
    attempts = task.request.retries + 1
    if task.request.retries >= NOTIFY_MAX_RETRIES:
        logger.error(
            "build_id=%s %s permanently failed after %s attempts: %s",
            build_id,
            label,
            attempts,
            exc,
        )
        return {"sent": False, "reason": "retries_exhausted"}

    countdown = min(2**task.request.retries, NOTIFY_MAX_BACKOFF_SECONDS)
    logger.warning(
        "build_id=%s %s attempt %s failed (%s); retrying in %ss",
        build_id,
        label,
        attempts,
        exc,
        countdown,
    )
    raise task.retry(exc=exc, countdown=countdown)


@shared_task(name="builds.notify_application", bind=True, max_retries=NOTIFY_MAX_RETRIES)
def notify_application(self, build_id: str) -> dict[str, Any]:
    """Report a terminal build (SUCCESS *or* FAILED) to the Application Service."""
    try:
        return services.send_application_notification(build_id)
    except Build.DoesNotExist:
        logger.error("build_id=%s notify_application: unknown build", build_id)
        return {"sent": False, "reason": "unknown_build"}
    except Exception as exc:  # noqa: BLE001 - bounded retry, then log and stop
        return _retry_or_give_up(self, exc, build_id, "notify_application")


@shared_task(name="builds.notify_deployment", bind=True, max_retries=NOTIFY_MAX_RETRIES)
def notify_deployment(self, build_id: str) -> dict[str, Any]:
    """Hand a successful image to the Deployment Service. Never called for failures."""
    try:
        return services.send_deployment_notification(build_id)
    except Build.DoesNotExist:
        logger.error("build_id=%s notify_deployment: unknown build", build_id)
        return {"sent": False, "reason": "unknown_build"}
    except Exception as exc:  # noqa: BLE001 - bounded retry, then log and stop
        return _retry_or_give_up(self, exc, build_id, "notify_deployment")


# ---------------------------------------------------------------------------
# Phase 10 — reconciler
# ---------------------------------------------------------------------------
@shared_task(name="builds.reconcile_stuck_builds", bind=True)
def reconcile_stuck_builds(self) -> dict[str, Any]:
    """Periodic sweep: time out stranded builds, re-dispatch stale QUEUED ones."""
    return services.reconcile_stuck_builds()
