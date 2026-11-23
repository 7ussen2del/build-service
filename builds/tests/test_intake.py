"""Phase 2 tests: intake, validation, idempotency (README section 3)."""
import uuid
from unittest.mock import MagicMock

import pytest

from builds.models import Build, BuildStatus
from builds.tasks import notify_application, run_build_pipeline, start_build


def _valid_payload(**overrides):
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


def _patch_delay(monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(run_build_pipeline, "delay", delay)
    return delay


def _patch_notify(monkeypatch):
    delay = MagicMock()
    monkeypatch.setattr(notify_application, "delay", delay)
    return delay


@pytest.mark.django_db
def test_valid_payload_queues_build_and_dispatches(monkeypatch):
    delay = _patch_delay(monkeypatch)
    result = start_build(_valid_payload())
    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert str(build.build_id) == result
    assert build.application_id == 42
    assert build.commit_sha is None
    delay.assert_called_once_with(str(build.build_id))


@pytest.mark.django_db
def test_optional_fields_may_be_absent(monkeypatch):
    _patch_delay(monkeypatch)
    payload = _valid_payload()
    del payload["port"]
    del payload["requirements_file"]
    start_build(payload)
    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert build.port is None
    assert build.requirements_file is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("request_id"),
        lambda p: p.__setitem__("request_id", "not-a-uuid"),
        lambda p: p.pop("application_id"),
        lambda p: p.__setitem__("application_id", "abc"),
        lambda p: p.pop("owner_id"),
        lambda p: p.__setitem__("owner_id", None),
    ],
)
def test_bad_envelope_is_discarded(monkeypatch, mutate):
    delay = _patch_delay(monkeypatch)
    payload = _valid_payload()
    mutate(payload)
    result = start_build(payload)
    assert result is None
    assert Build.objects.count() == 0
    delay.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_url", "http://github.com/x/y"),
        ("repository_url", "https://user:pass@github.com/x/y"),
        ("repository_url", "https:///no-host"),
        ("branch", "-badstart"),
        ("branch", "has/../dots"),
        ("branch", "bad branch!"),
        ("application_type", "ruby"),
        ("port", 0),
        ("port", 70000),
        ("name", "UpperCase"),
        ("name", "bad_name"),
        ("source_directory", "../escape"),
        ("source_directory", "/absolute"),
        ("run_command", "echo hi\nrm -rf /"),
        ("run_command", ""),
    ],
)
def test_invalid_field_records_failed(monkeypatch, field, value):
    delay = _patch_delay(monkeypatch)
    notify = _patch_notify(monkeypatch)
    start_build(_valid_payload(**{field: value}))
    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "INVALID_PAYLOAD"
    assert build.error_message
    assert build.started_at is not None
    assert build.finished_at == build.started_at
    delay.assert_not_called()
    notify.assert_called_once_with(str(build.build_id))


@pytest.mark.django_db
@pytest.mark.parametrize("source_directory", [None, "", "__omit__"])
def test_empty_source_directory_defaults_to_dot(monkeypatch, source_directory):
    delay = _patch_delay(monkeypatch)
    payload = _valid_payload()
    if source_directory == "__omit__":
        payload.pop("source_directory")
    else:
        payload["source_directory"] = source_directory
    start_build(payload)
    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert build.source_directory == "."
    delay.assert_called_once_with(str(build.build_id))


@pytest.mark.django_db
@pytest.mark.parametrize("app_type", ["node", "react"])
@pytest.mark.parametrize("run_command", [None, "", "__omit__"])
def test_empty_run_command_defaults_for_node_and_react(monkeypatch, app_type, run_command):
    delay = _patch_delay(monkeypatch)
    payload = _valid_payload(
        application_type=app_type,
        source_directory=None,
        requirements_file=None,
    )
    if run_command == "__omit__":
        payload.pop("run_command")
    else:
        payload["run_command"] = run_command
    start_build(payload)
    build = Build.objects.get()
    assert build.status == BuildStatus.QUEUED
    assert build.source_directory == "."
    assert build.run_command == "npm start"
    assert build.requirements_file is None
    delay.assert_called_once_with(str(build.build_id))


@pytest.mark.django_db
@pytest.mark.parametrize("app_type", ["django", "flask"])
@pytest.mark.parametrize("run_command", [None, "", "__omit__"])
def test_empty_run_command_fails_for_django_and_flask(monkeypatch, app_type, run_command):
    delay = _patch_delay(monkeypatch)
    notify = _patch_notify(monkeypatch)
    payload = _valid_payload(application_type=app_type, source_directory=None)
    if run_command == "__omit__":
        payload.pop("run_command")
    else:
        payload["run_command"] = run_command
    start_build(payload)
    build = Build.objects.get()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "INVALID_PAYLOAD"
    assert "run_command is required for django/flask" in build.error_message
    assert build.source_directory == ""
    assert build.run_command == ""
    assert build.started_at is not None
    assert build.finished_at == build.started_at
    delay.assert_not_called()
    notify.assert_called_once_with(str(build.build_id))


@pytest.mark.django_db
def test_duplicate_delivery_is_noop(monkeypatch):
    delay = _patch_delay(monkeypatch)
    payload = _valid_payload()
    first = start_build(payload)
    second = start_build(payload)
    assert Build.objects.count() == 1
    assert second is None
    delay.assert_called_once_with(first)


@pytest.mark.django_db
def test_duplicate_invalid_delivery_is_noop(monkeypatch):
    _patch_delay(monkeypatch)
    notify = _patch_notify(monkeypatch)
    payload = _valid_payload(application_type="ruby")
    start_build(payload)
    start_build(payload)
    assert Build.objects.count() == 1
    assert Build.objects.get().status == BuildStatus.FAILED
    notify.assert_called_once()
