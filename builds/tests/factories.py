"""
Shared factory for Build rows in tests.

Keeps every test from repeating the full snapshot.
"""
from __future__ import annotations

import uuid
from typing import Any

from builds.models import ApplicationType, Build, BuildStatus

VALID_PAYLOAD: dict[str, Any] = {
    "request_id": "11111111-1111-1111-1111-111111111111",
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


def payload(**overrides: Any) -> dict[str, Any]:
    """A valid intake payload with a fresh request_id, plus any overrides."""
    data = dict(VALID_PAYLOAD)
    data["request_id"] = str(uuid.uuid4())
    data.update(overrides)
    return data


def make_build(**overrides: Any) -> Build:
    """Create a Build row directly, bypassing intake validation."""
    fields: dict[str, Any] = {
        "request_id": uuid.uuid4(),
        "application_id": 42,
        "owner_id": 17,
        "status": BuildStatus.QUEUED,
        "name": "mates",
        "repository_url": "https://github.com/omar-mn/mates",
        "branch": "main",
        "application_type": ApplicationType.DJANGO,
        "source_directory": "src",
        "requirements_file": "requirements.txt",
        "port": 8000,
        "run_command": "bash ./run.sh",
    }
    fields.update(overrides)
    return Build.objects.create(**fields)
