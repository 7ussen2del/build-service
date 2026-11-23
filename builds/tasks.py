"""Celery tasks for the Build Service.

``builds.start_build`` is the entry point from the Application Service (README
section 3), ``builds.run_build_pipeline`` runs the build (README sections 5-6),
and the ``notify_*`` tasks report the result to the other services (README
section 8).
"""
from datetime import timezone as dt_timezone
import logging
import uuid

import requests
from celery import current_app, shared_task
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from . import services
from .models import ApplicationType, Build, BuildStatus
from .serializers import BuildRequestSerializer

logger = logging.getLogger(__name__)


def _as_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_envelope(payload):
    """Return (request_id, application_id, owner_id) or None if the envelope is bad."""
    try:
        request_id = uuid.UUID(str(payload["request_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    application_id = _as_int(payload.get("application_id"))
    owner_id = _as_int(payload.get("owner_id"))
    if application_id is None or owner_id is None:
        return None
    return request_id, application_id, owner_id


def _format_errors(errors):
    parts = []
    for field, messages in errors.items():
        if isinstance(messages, (list, tuple)):
            messages = "; ".join(str(m) for m in messages)
        parts.append(f"{field}: {messages}")
    return " | ".join(parts)[:2000]


def _safe_snapshot(payload):
    """Best-effort snapshot for an INVALID_PAYLOAD record (never raises)."""

    def text(key, limit):
        value = payload.get(key)
        return str(value)[:limit] if value is not None else ""

    port = _as_int(payload.get("port"))
    if port is not None and not (1 <= port <= 65535):
        port = None
    requirements_file = payload.get("requirements_file")
    return dict(
        name=text("name", 100),
        repository_url=text("repository_url", 500),
        branch=text("branch", 200),
        application_type=text("application_type", 20),
        source_directory=text("source_directory", 500),
        requirements_file=(
            str(requirements_file)[:500] if requirements_file not in (None, "") else None
        ),
        port=port,
        run_command=text("run_command", 1000),
    )


@shared_task(name="builds.start_build")
def start_build(payload):
    if not isinstance(payload, dict):
        logger.warning("Discarding build request: payload is not a JSON object")
        return None

    envelope = _parse_envelope(payload)
    if envelope is None:
        logger.warning("Discarding build request: missing or invalid envelope fields")
        return None
    request_id, application_id, owner_id = envelope

    if Build.objects.filter(request_id=request_id).exists():
        logger.info("Duplicate delivery for request_id=%s ignored", request_id)
        return None

    serializer = BuildRequestSerializer(data=payload)
    valid = serializer.is_valid()
    errors = serializer.errors

    if valid:
        data = dict(serializer.validated_data)
        if not (data.get("source_directory") or "").strip():
            data["source_directory"] = "."
        if not (data.get("run_command") or "").strip():
            if data["application_type"] in (ApplicationType.NODE, ApplicationType.REACT):
                data["run_command"] = "npm start"
            else:
                valid = False
                errors = {"run_command": ["run_command is required for django/flask"]}

    try:
        if valid:
            build = Build.objects.create(
                request_id=request_id,
                application_id=application_id,
                owner_id=owner_id,
                status=BuildStatus.QUEUED,
                **data,
            )
        else:
            now = timezone.now()
            build = Build.objects.create(
                request_id=request_id,
                application_id=application_id,
                owner_id=owner_id,
                status=BuildStatus.FAILED,
                error_code="INVALID_PAYLOAD",
                error_message=_format_errors(errors),
                started_at=now,
                finished_at=now,
                **_safe_snapshot(payload),
            )
    except IntegrityError:
        logger.info("Concurrent duplicate for request_id=%s ignored", request_id)
        return None

    if valid:
        run_build_pipeline.delay(str(build.build_id))
        logger.info("Queued build %s (request_id=%s)", build.build_id, request_id)
    else:
        notify_application.delay(str(build.build_id))
        logger.info(
            "Recorded INVALID_PAYLOAD build %s (request_id=%s)", build.build_id, request_id
        )
    return str(build.build_id)


@shared_task(name="builds.run_build_pipeline")
def run_build_pipeline(build_id):
    """Resolve commit, render Dockerfile, trigger Jenkins (README sections 5-6)."""
    result = services.run_pipeline(build_id)
    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        return result
    if build.status == BuildStatus.BUILDING:
        notify_application_started.delay(str(build_id))
    elif build.status == BuildStatus.FAILED:
        notify_application.delay(str(build_id))
    return result


# Target task name on the shared broker (README section 8).
DEPLOYMENT_DEPLOY_TASK = "deployments.deploy_image"

_NOTIFY_OPTS = dict(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
    retry_jitter=True,
)


def _iso_utc(dt):
    return dt.astimezone(dt_timezone.utc).isoformat()


def _format_failure_message(build):
    code = (build.error_code or "").strip()
    message = (build.error_message or "").strip()
    if code and message:
        return f"[{code}] {message}"
    if code:
        return f"[{code}]"
    return message or "Build failed"


def _application_payload(build):
    finished_at = build.finished_at or timezone.now()
    started_at = build.started_at or finished_at
    if finished_at < started_at:
        finished_at = started_at
    payload = {
        "build_id": str(build.build_id),
        "application_id": build.application_id,
        "started_at": _iso_utc(started_at),
        "finished_at": _iso_utc(finished_at),
        "commit_sha": build.commit_sha,
    }
    if build.status == BuildStatus.SUCCESS:
        payload["status"] = "success"
        payload["image_repository"] = build.image_repository
        payload["image_tag"] = build.image_tag
        payload["image_digest"] = build.image_digest
    else:
        payload["status"] = "failed"
        payload["error_message"] = _format_failure_message(build)
    return payload


def _deployment_payload(build):
    return {
        "request_id": str(uuid.uuid4()),  # fresh per attempt
        "build_id": str(build.build_id),  # stable dedup key
        "application_id": build.application_id,
        "owner_id": build.owner_id,
        "image_repository": build.image_repository,
        "image_tag": build.image_tag,
        "image_digest": build.image_digest,
        "commit_sha": build.commit_sha,
        "application_type": build.application_type,
        "port": build.port,
    }


@shared_task(name="builds.notify_application_started", **_NOTIFY_OPTS)
def notify_application_started(build_id):
    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        logger.warning("notify_application_started: build %s not found", build_id)
        return
    started_at = build.started_at or timezone.now()
    payload = {
        "build_id": str(build.build_id),
        "application_id": build.application_id,
        "started_at": _iso_utc(started_at),
    }
    url = f"{settings.APP_SERVICE_URL.rstrip('/')}/internal/builds/{build.request_id}/started/"
    resp = requests.post(
        url,
        json=payload,
        timeout=10,
        headers={"Authorization": f"Bearer {settings.APP_SERVICE_TOKEN}"},
    )
    resp.raise_for_status()


@shared_task(name="builds.notify_application", **_NOTIFY_OPTS)
def notify_application(build_id):
    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        logger.warning("notify_application: build %s not found", build_id)
        return
    url = f"{settings.APP_SERVICE_URL.rstrip('/')}/internal/builds/{build.request_id}/result/"
    resp = requests.post(
        url,
        json=_application_payload(build),
        timeout=10,
        headers={"Authorization": f"Bearer {settings.APP_SERVICE_TOKEN}"},
    )
    resp.raise_for_status()


@shared_task(name="builds.notify_deployment", **_NOTIFY_OPTS)
def notify_deployment(build_id):
    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        logger.warning("notify_deployment: build %s not found", build_id)
        return
    if build.status != BuildStatus.SUCCESS:
        logger.info("notify_deployment: build %s is not SUCCESS; skipping", build_id)
        return
    current_app.send_task(
        DEPLOYMENT_DEPLOY_TASK,
        kwargs=_deployment_payload(build),
        queue=settings.DEPLOYMENT_QUEUE,
    )
