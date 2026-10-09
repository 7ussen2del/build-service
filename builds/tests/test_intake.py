"""Phase 2 acceptance tests: intake, validation routing, idempotency, dispatch.

The pipeline dispatch is patched out (``no_dispatch``) so no real Celery
message is sent, and ``django_capture_on_commit_callbacks`` is used to run the
``transaction.on_commit`` hooks that would otherwise never fire inside the
test's rolled-back transaction.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from builds import tasks
from builds.models import Build, BuildStatus
from builds.services import create_build_from_request

pytestmark = pytest.mark.django_db


def valid_payload(**overrides) -> dict:
    payload = {
        "request_id": str(uuid.uuid4()),
        "application_id": 42,
        "owner_id": 17,
        "name": "mates",
        "repository_url": "https://github.com/omar-mn/mates",
        "branch": "main",
        "application_type": "django",
        "source_directory": "src",
        "requirements_file": "requirements.txt",
        "port": 8000,
        "run_command": "bash ./run.sh",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def no_dispatch():
    """Replace the pipeline dispatch so tests assert on it instead of sending."""
    with patch("builds.services._dispatch_pipeline") as mock:
        yield mock


# --------------------------------------------------------------------------- #
# Acceptance criteria
# --------------------------------------------------------------------------- #
def test_valid_payload_creates_queued_and_dispatches_once(
    no_dispatch, django_capture_on_commit_callbacks
):
    payload = valid_payload()

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        tasks.start_build(payload)

    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert build.application_id == 42
    assert build.owner_id == 17
    assert build.name == "mates"
    assert build.port == 8000
    assert build.application_type == "django"
    assert len(callbacks) == 1
    no_dispatch.assert_called_once_with(str(build.build_id))


def test_same_payload_twice_creates_one_row_and_one_dispatch(
    no_dispatch, django_capture_on_commit_callbacks
):
    payload = valid_payload()

    with django_capture_on_commit_callbacks(execute=True):
        tasks.start_build(payload)
        tasks.start_build(payload)  # Celery redelivery: identical request_id

    assert Build.objects.count() == 1
    no_dispatch.assert_called_once()


def test_bad_url_creates_failed_build_with_invalid_payload(
    no_dispatch, django_capture_on_commit_callbacks
):
    payload = valid_payload(repository_url="http://insecure.example.com/repo.git")

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        tasks.start_build(payload)

    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "INVALID_PAYLOAD"
    assert "repository_url" in build.error_message
    assert build.finished_at is not None
    # The raw value is still recorded for the audit trail.
    assert build.repository_url == "http://insecure.example.com/repo.git"
    # A FAILED build neither dispatches the pipeline nor registers a hook.
    assert callbacks == []
    no_dispatch.assert_not_called()


def test_missing_request_id_creates_no_row_and_does_not_raise(no_dispatch):
    payload = valid_payload()
    del payload["request_id"]

    tasks.start_build(payload)  # must not raise

    assert Build.objects.count() == 0
    no_dispatch.assert_not_called()


# --------------------------------------------------------------------------- #
# Robustness around the envelope
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_payload", [None, "garbage", 123, ["list"]])
def test_non_dict_payload_is_discarded(bad_payload, no_dispatch):
    tasks.start_build(bad_payload)

    assert Build.objects.count() == 0
    no_dispatch.assert_not_called()


def test_invalid_envelope_values_are_discarded(no_dispatch):
    tasks.start_build({"request_id": "not-a-uuid", "owner_id": 1})

    assert Build.objects.count() == 0
    no_dispatch.assert_not_called()


# --------------------------------------------------------------------------- #
# Snapshot recording details
# --------------------------------------------------------------------------- #
def test_bad_port_is_recorded_as_null_on_failed_build(no_dispatch):
    # Gap #1: an unstorable port must not stop the FAILED row being written.
    payload = valid_payload(port=70000)

    tasks.start_build(payload)

    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "INVALID_PAYLOAD"
    assert build.port is None


def test_create_build_from_request_is_idempotent(no_dispatch):
    request_id = uuid.uuid4()
    payload = valid_payload(request_id=str(request_id))

    first = create_build_from_request(
        request_id=request_id, application_id=42, owner_id=17, payload=payload
    )
    second = create_build_from_request(
        request_id=request_id, application_id=42, owner_id=17, payload=payload
    )

    assert first.pk == second.pk
    assert Build.objects.count() == 1
