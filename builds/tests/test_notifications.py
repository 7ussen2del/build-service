"""Phase 5 tests: notification payloads and dispatch (README section 8)."""
from datetime import datetime, timezone as dt_timezone
import uuid
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.utils import timezone

from builds import tasks
from builds.models import Build, BuildStatus

DIGEST = "sha256:" + "a" * 64


def _build(status, **extra):
    now = timezone.now()
    data = dict(
        request_id=uuid.uuid4(),
        application_id=42,
        owner_id=17,
        name="mates",
        repository_url="https://github.com/omar-mn/mates",
        branch="main",
        application_type="django",
        source_directory="src",
        requirements_file="requirements.txt",
        port=8000,
        run_command="bash ./run.sh",
        status=status,
        commit_sha="a" * 40,
        started_at=now,
        finished_at=now,
    )
    data.update(extra)
    return Build.objects.create(**data)


@pytest.mark.django_db
def test_application_payload_success_shape():
    started = datetime(2026, 1, 1, 10, 0, 0, tzinfo=dt_timezone.utc)
    finished = datetime(2026, 1, 1, 10, 5, 0, tzinfo=dt_timezone.utc)
    build = _build(
        BuildStatus.SUCCESS,
        image_repository="acme/p-17-mates",
        image_tag="a" * 7,
        image_digest=DIGEST,
        started_at=started,
        finished_at=finished,
    )
    payload = tasks._application_payload(build)
    assert set(payload.keys()) == {
        "build_id",
        "application_id",
        "started_at",
        "finished_at",
        "status",
        "image_repository",
        "image_tag",
        "image_digest",
        "commit_sha",
    }
    assert payload["status"] == "success"
    assert payload["image_repository"] == "acme/p-17-mates"
    assert payload["image_tag"] == "a" * 7
    assert payload["image_digest"] == DIGEST
    assert payload["started_at"] == "2026-01-01T10:00:00+00:00"
    assert payload["finished_at"] == "2026-01-01T10:05:00+00:00"
    assert "error_code" not in payload
    assert "error_message" not in payload


@pytest.mark.django_db
def test_application_payload_failure_shape():
    build = _build(
        BuildStatus.FAILED,
        error_code="BUILD_FAILED",
        error_message="boom",
        commit_sha=None,
    )
    payload = tasks._application_payload(build)
    assert set(payload.keys()) == {
        "build_id",
        "application_id",
        "started_at",
        "finished_at",
        "status",
        "error_message",
        "commit_sha",
    }
    assert payload["status"] == "failed"
    assert payload["error_message"] == "[BUILD_FAILED] boom"
    assert payload["commit_sha"] is None
    assert "error_code" not in payload
    assert "image_repository" not in payload
    assert "image_tag" not in payload
    assert "image_digest" not in payload


@pytest.mark.django_db
def test_deployment_payload_shape_and_fresh_request_id():
    build = _build(
        BuildStatus.SUCCESS,
        image_repository="acme/p-17-mates",
        image_tag="a" * 7,
        image_digest=DIGEST,
    )
    payload = tasks._deployment_payload(build)
    assert payload["build_id"] == str(build.build_id)
    assert payload["owner_id"] == 17
    assert payload["application_type"] == "django"
    assert payload["port"] == 8000
    assert payload["image_digest"] == DIGEST
    assert uuid.UUID(payload["request_id"])
    # fresh per attempt
    assert payload["request_id"] != tasks._deployment_payload(build)["request_id"]


@pytest.mark.django_db
def test_notify_application_sends_result_task(monkeypatch):
    build = _build(
        BuildStatus.SUCCESS,
        image_repository="acme/p-17-mates",
        image_tag="a" * 7,
        image_digest=DIGEST,
    )
    monkeypatch.setattr(settings, "APP_SERVICE_URL", "http://application-service:3000")
    monkeypatch.setattr(settings, "APP_SERVICE_TOKEN", "secret-token")
    resp = MagicMock()
    post = MagicMock(return_value=resp)
    monkeypatch.setattr(tasks.requests, "post", post)

    tasks.notify_application(str(build.build_id))

    post.assert_called_once()
    assert (
        post.call_args.args[0]
        == f"http://application-service:3000/internal/builds/{build.request_id}/result/"
    )
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert post.call_args.kwargs["timeout"] == 10
    assert post.call_args.kwargs["json"] == tasks._application_payload(build)
    resp.raise_for_status.assert_called_once()


@pytest.mark.django_db
def test_notify_application_started_posts_started_event(monkeypatch):
    started = datetime(2026, 1, 1, 10, 0, 0, tzinfo=dt_timezone.utc)
    build = _build(BuildStatus.BUILDING, started_at=started, finished_at=None)
    monkeypatch.setattr(settings, "APP_SERVICE_URL", "http://application-service:3000")
    monkeypatch.setattr(settings, "APP_SERVICE_TOKEN", "secret-token")
    resp = MagicMock()
    post = MagicMock(return_value=resp)
    monkeypatch.setattr(tasks.requests, "post", post)

    tasks.notify_application_started(str(build.build_id))

    post.assert_called_once()
    assert (
        post.call_args.args[0]
        == f"http://application-service:3000/internal/builds/{build.request_id}/started/"
    )
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}
    assert post.call_args.kwargs["timeout"] == 10
    assert post.call_args.kwargs["json"] == {
        "build_id": str(build.build_id),
        "application_id": 42,
        "started_at": "2026-01-01T10:00:00+00:00",
    }
    resp.raise_for_status.assert_called_once()


@pytest.mark.django_db
def test_notify_deployment_sends_on_success(monkeypatch):
    build = _build(
        BuildStatus.SUCCESS,
        image_repository="acme/p-17-mates",
        image_tag="a" * 7,
        image_digest=DIGEST,
    )
    fake = MagicMock()
    monkeypatch.setattr(tasks, "current_app", fake)
    tasks.notify_deployment(str(build.build_id))
    fake.send_task.assert_called_once()
    assert fake.send_task.call_args.args[0] == "deployments.deploy_image"
    assert fake.send_task.call_args.kwargs["queue"] == "deployment"


@pytest.mark.django_db
def test_notify_deployment_skips_non_success(monkeypatch):
    build = _build(BuildStatus.FAILED, error_code="BUILD_FAILED")
    fake = MagicMock()
    monkeypatch.setattr(tasks, "current_app", fake)
    tasks.notify_deployment(str(build.build_id))
    fake.send_task.assert_not_called()
