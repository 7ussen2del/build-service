"""Phase 2 acceptance: validation, idempotency, dispatch."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from builds.errors import ErrorCode
from builds.models import Build, BuildStatus
from builds.tasks import start_build
from builds.tests.factories import payload

pytestmark = pytest.mark.django_db


@pytest.fixture()
def dispatch_pipeline():
    with patch("builds.services.dispatch_pipeline") as mocked:
        yield mocked


@pytest.fixture()
def dispatch_notifications():
    with patch("builds.services.dispatch_result_notifications") as mocked:
        yield mocked


def run_intake(data, on_commit_callbacks):
    """Run the intake task and flush transaction.on_commit callbacks."""
    with on_commit_callbacks(execute=True):
        return start_build.apply(args=[data]).get()


# ---------------------------------------------------------------------------
# Happy path + idempotency
# ---------------------------------------------------------------------------
def test_valid_payload_creates_one_row_and_one_dispatch(
    dispatch_pipeline, django_capture_on_commit_callbacks
) -> None:
    data = payload()

    result = run_intake(data, django_capture_on_commit_callbacks)

    assert result["accepted"] is True
    assert result["created"] is True
    assert Build.objects.count() == 1

    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert build.application_id == 42
    assert build.owner_id == 17
    assert build.name == "mates"
    assert build.port == 8000
    assert str(build.request_id) == data["request_id"]
    assert dispatch_pipeline.call_count == 1


def test_same_request_id_twice_creates_one_row_and_one_dispatch(
    dispatch_pipeline, django_capture_on_commit_callbacks
) -> None:
    data = payload()

    first = run_intake(data, django_capture_on_commit_callbacks)
    second = run_intake(data, django_capture_on_commit_callbacks)

    assert first["created"] is True
    assert second["created"] is False
    assert second["accepted"] is True
    assert second["reason"] == "duplicate"
    assert Build.objects.count() == 1
    assert dispatch_pipeline.call_count == 1


def test_requirements_file_may_be_omitted(
    dispatch_pipeline, django_capture_on_commit_callbacks
) -> None:
    data = payload(application_type="node")
    data.pop("requirements_file")

    run_intake(data, django_capture_on_commit_callbacks)

    assert Build.objects.get().requirements_file is None


# ---------------------------------------------------------------------------
# Envelope failures: log and discard, no row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"request_id": None}, id="request_id-missing"),
        pytest.param({"request_id": "not-a-uuid"}, id="request_id-malformed"),
        pytest.param({"application_id": None}, id="application_id-missing"),
        pytest.param({"application_id": 0}, id="application_id-zero"),
        pytest.param({"application_id": "abc"}, id="application_id-not-int"),
        pytest.param({"owner_id": -1}, id="owner_id-negative"),
    ],
)
def test_invalid_envelope_is_discarded(
    mutation, dispatch_pipeline, dispatch_notifications, django_capture_on_commit_callbacks
) -> None:
    data = payload(**mutation)
    if mutation.get("request_id", "sentinel") is None:
        data.pop("request_id")

    result = run_intake(data, django_capture_on_commit_callbacks)

    assert result["accepted"] is False
    assert result["reason"] == "invalid_envelope"
    assert Build.objects.count() == 0
    assert dispatch_pipeline.call_count == 0
    assert dispatch_notifications.call_count == 0


def test_empty_payload_does_not_crash(django_capture_on_commit_callbacks) -> None:
    result = run_intake({}, django_capture_on_commit_callbacks)

    assert result["accepted"] is False
    assert Build.objects.count() == 0


# ---------------------------------------------------------------------------
# Snapshot failures: FAILED row + notification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"repository_url": "http://github.com/a/b"}, id="url-not-https"),
        pytest.param({"repository_url": "git@github.com:a/b.git"}, id="url-ssh"),
        pytest.param({"repository_url": "https://user:pw@github.com/a/b"}, id="url-credentials"),
        pytest.param({"repository_url": "https:///no-host"}, id="url-no-host"),
        pytest.param({"branch": "-dash"}, id="branch-leading-dash"),
        pytest.param({"branch": "feature/../etc"}, id="branch-dotdot"),
        pytest.param({"branch": "has space"}, id="branch-space"),
        pytest.param({"branch": "trailing/"}, id="branch-trailing-slash"),
        pytest.param({"application_type": "rails"}, id="type-unknown"),
        pytest.param({"port": 0}, id="port-zero"),
        pytest.param({"port": 70000}, id="port-too-large"),
        pytest.param({"port": "eight"}, id="port-not-int"),
        pytest.param({"name": "Mates"}, id="name-uppercase"),
        pytest.param({"name": "my_app"}, id="name-underscore"),
        pytest.param({"source_directory": "/etc"}, id="dir-absolute"),
        pytest.param({"source_directory": "../etc"}, id="dir-traversal"),
        pytest.param({"source_directory": "C:/Windows"}, id="dir-windows-drive"),
        pytest.param({"source_directory": "src\\app"}, id="dir-backslash"),
        pytest.param({"requirements_file": "../../requirements.txt"}, id="req-traversal"),
        pytest.param({"run_command": ""}, id="run-empty"),
        pytest.param({"run_command": "   "}, id="run-whitespace"),
        pytest.param({"run_command": "echo 'unbalanced"}, id="run-unbalanced-quote"),
    ],
)
def test_invalid_snapshot_records_failed_build_and_notifies(
    mutation, dispatch_pipeline, dispatch_notifications, django_capture_on_commit_callbacks
) -> None:
    data = payload(**mutation)

    result = run_intake(data, django_capture_on_commit_callbacks)

    assert result["accepted"] is False
    assert result["reason"] == "invalid_payload"

    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == ErrorCode.INVALID_PAYLOAD
    assert build.error_message
    assert build.finished_at is not None

    # The Application Service is told; the pipeline is never started.
    assert dispatch_notifications.call_count == 1
    assert dispatch_pipeline.call_count == 0


def test_newline_injection_in_run_command_is_rejected(
    dispatch_notifications, django_capture_on_commit_callbacks
) -> None:
    data = payload(run_command="python app.py\nRUN echo pwned")

    run_intake(data, django_capture_on_commit_callbacks)

    assert Build.objects.get().error_code == ErrorCode.INVALID_PAYLOAD


def test_out_of_range_port_is_still_recorded(
    dispatch_notifications, django_capture_on_commit_callbacks
) -> None:
    # port is nullable precisely so that an absurd value can be recorded.
    data = payload(port=10**12)

    run_intake(data, django_capture_on_commit_callbacks)

    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.port is None


def test_duplicate_invalid_payload_notifies_once(
    dispatch_notifications, django_capture_on_commit_callbacks
) -> None:
    data = payload(port=0)

    run_intake(data, django_capture_on_commit_callbacks)
    run_intake(data, django_capture_on_commit_callbacks)

    assert Build.objects.count() == 1
    assert dispatch_notifications.call_count == 1


def test_payload_accepted_as_kwargs(
    dispatch_pipeline, django_capture_on_commit_callbacks
) -> None:
    data = payload()

    with django_capture_on_commit_callbacks(execute=True):
        result = start_build.apply(kwargs=data).get()

    assert result["accepted"] is True
    assert Build.objects.count() == 1


def test_task_is_registered_under_the_agreed_name() -> None:
    assert start_build.name == "builds.start_build"
