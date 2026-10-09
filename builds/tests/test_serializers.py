"""Phase 2 validation rules (serializers), independent of the database."""

from __future__ import annotations

import uuid

import pytest

from builds.serializers import BuildRequestSerializer, EnvelopeSerializer


def _snapshot(**overrides) -> dict:
    data = {
        "name": "mates",
        "repository_url": "https://github.com/omar-mn/mates",
        "branch": "main",
        "application_type": "django",
        "source_directory": "src",
        "requirements_file": "requirements.txt",
        "port": 8000,
        "run_command": "bash ./run.sh",
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
def test_envelope_accepts_valid_identity():
    serializer = EnvelopeSerializer(
        data={"request_id": str(uuid.uuid4()), "application_id": 42, "owner_id": 17}
    )
    assert serializer.is_valid(), serializer.errors


@pytest.mark.parametrize(
    "data",
    [
        {"application_id": 42, "owner_id": 17},  # no request_id
        {"request_id": "not-a-uuid", "application_id": 42, "owner_id": 17},
        {"request_id": str(uuid.uuid4()), "owner_id": 17},  # no application_id
        {"request_id": str(uuid.uuid4()), "application_id": 42},  # no owner_id
        {"request_id": str(uuid.uuid4()), "application_id": 0, "owner_id": 17},  # < 1
    ],
)
def test_envelope_rejects_bad_identity(data):
    assert not EnvelopeSerializer(data=data).is_valid()


# --------------------------------------------------------------------------- #
# Snapshot: happy path
# --------------------------------------------------------------------------- #
def test_snapshot_accepts_valid_payload():
    serializer = BuildRequestSerializer(data=_snapshot())
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["application_type"] == "django"
    assert serializer.validated_data["port"] == 8000


# --------------------------------------------------------------------------- #
# repository_url
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x/y",  # not https
        "ssh://git@github.com/x/y",  # ssh
        "git://github.com/x/y",  # git
        "file:///etc/passwd",  # file
        "https://user:pass@github.com/x/y",  # userinfo
        "https:///no-host",  # empty host
    ],
)
def test_repository_url_rejected(url):
    assert not BuildRequestSerializer(data=_snapshot(repository_url=url)).is_valid()


@pytest.mark.parametrize(
    "url", ["https://github.com/a/b", "https://gitlab.com/group/sub/repo"]
)
def test_repository_url_accepted(url):
    assert BuildRequestSerializer(data=_snapshot(repository_url=url)).is_valid()


# --------------------------------------------------------------------------- #
# branch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "branch", ["-x", "a..b", "feat/../x", "has space", "tab\tbad", "bad~char"]
)
def test_branch_rejected(branch):
    assert not BuildRequestSerializer(data=_snapshot(branch=branch)).is_valid()


@pytest.mark.parametrize(
    "branch", ["main", "feature/login", "release-1.2.3", "v2_final"]
)
def test_branch_accepted(branch):
    assert BuildRequestSerializer(data=_snapshot(branch=branch)).is_valid(), branch


# --------------------------------------------------------------------------- #
# application_type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("app_type", ["django", "flask", "node", "react"])
def test_application_type_accepted(app_type):
    assert BuildRequestSerializer(data=_snapshot(application_type=app_type)).is_valid()


def test_application_type_rejected():
    assert not BuildRequestSerializer(data=_snapshot(application_type="python")).is_valid()


# --------------------------------------------------------------------------- #
# port
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("port", [0, -1, 65536, 70000, "abc", 80.5])
def test_port_rejected(port):
    assert not BuildRequestSerializer(data=_snapshot(port=port)).is_valid()


@pytest.mark.parametrize("port", [1, 80, 8000, 65535])
def test_port_accepted(port):
    assert BuildRequestSerializer(data=_snapshot(port=port)).is_valid()


# --------------------------------------------------------------------------- #
# name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["MyApp", "-app", "app-", "my_app", "app!", ""])
def test_name_rejected(name):
    assert not BuildRequestSerializer(data=_snapshot(name=name)).is_valid()


@pytest.mark.parametrize("name", ["mates", "my-app", "app123", "a"])
def test_name_accepted(name):
    assert BuildRequestSerializer(data=_snapshot(name=name)).is_valid(), name


# --------------------------------------------------------------------------- #
# paths: source_directory / requirements_file
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", ["/abs", "../escape", "a/../b", "a\\b", "nul\x00byte", "line\nbreak"]
)
def test_source_directory_rejected(path):
    assert not BuildRequestSerializer(data=_snapshot(source_directory=path)).is_valid()


@pytest.mark.parametrize("path", ["", "src", "src/app", "a/b/c"])
def test_source_directory_accepted(path):
    assert BuildRequestSerializer(data=_snapshot(source_directory=path)).is_valid(), path


def test_requirements_file_allows_null_and_blank():
    assert BuildRequestSerializer(data=_snapshot(requirements_file=None)).is_valid()
    assert BuildRequestSerializer(data=_snapshot(requirements_file="")).is_valid()


@pytest.mark.parametrize("path", ["/abs", "../x", "a/../b"])
def test_requirements_file_rejected(path):
    assert not BuildRequestSerializer(data=_snapshot(requirements_file=path)).is_valid()


# --------------------------------------------------------------------------- #
# run_command
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ["", "   ", "has\nnewline", "nul\x00byte"])
def test_run_command_rejected(cmd):
    assert not BuildRequestSerializer(data=_snapshot(run_command=cmd)).is_valid()


def test_run_command_accepted():
    assert BuildRequestSerializer(data=_snapshot(run_command="gunicorn app.wsgi")).is_valid()
