"""Phase 5 tests: notification payloads and dispatch (README section 8)."""
import uuid
from unittest.mock import MagicMock

import pytest

from builds import tasks
from builds.models import Build, BuildStatus

DIGEST = "sha256:" + "a" * 64


def _build(status, **extra):
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
    )
    data.update(extra)
    return Build.objects.create(**data)


@pytest.mark.django_db
def test_application_payload_success_shape():
    build = _build(
        BuildStatus.SUCCESS,
        image_repository="acme/p-17-mates",
        image_tag="a" * 7,
        image_digest=DIGEST,
    )
    payload = tasks._application_payload(build)
    assert payload["status"] == "success"
    assert payload["image_repository"] == "acme/p-17-mates"
    assert payload["image_digest"] == DIGEST
    assert "error_code" not in payload


@pytest.mark.django_db
def test_application_payload_failure_shape():
    build = _build(BuildStatus.FAILED, error_code="BUILD_FAILED", error_message="boom")
    payload = tasks._application_payload(build)
    assert payload["status"] == "failed"
    assert payload["image_repository"] is None
    assert payload["image_tag"] is None
    assert payload["image_digest"] is None
    assert payload["error_code"] == "BUILD_FAILED"
    assert payload["error_message"] == "boom"


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
    fake = MagicMock()
    monkeypatch.setattr(tasks, "current_app", fake)
    tasks.notify_application(str(build.build_id))
    fake.send_task.assert_called_once()
    assert fake.send_task.call_args.args[0] == "applications.build_result"


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


@pytest.mark.django_db
def test_notify_deployment_skips_non_success(monkeypatch):
    build = _build(BuildStatus.FAILED, error_code="BUILD_FAILED")
    fake = MagicMock()
    monkeypatch.setattr(tasks, "current_app", fake)
    tasks.notify_deployment(str(build.build_id))
    fake.send_task.assert_not_called()
