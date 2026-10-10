"""
Serializers for everything that crosses a service boundary.

The Application Service is trusted to *address* us, not to send us valid data:
every field is re-validated here before it reaches the database, git, or a
template. Validation is split in two levels on purpose:

* :class:`BuildEnvelopeSerializer` — identity. If this fails we have no row to
  write and nobody to notify, so the message is logged and discarded.
* :class:`BuildSnapshotSerializer` — the build description. If this fails we
  can still record a FAILED build and report it back.
"""
from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from typing import Any
from urllib.parse import urlparse

from rest_framework import serializers

from builds.errors import CALLBACK_ERROR_CODES, ErrorCode
from builds.models import ApplicationType, BuildStatus

MAX_NAME_LENGTH = 100
MAX_URL_LENGTH = 500
MAX_BRANCH_LENGTH = 255
MAX_PATH_LENGTH = 500
MAX_RUN_COMMAND_LENGTH = 4000

NAME_RE = re.compile(r"^[a-z0-9-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9/._-]+$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_CONTROL_CHARS = {"\x00", "\n", "\r"}
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _reject_control_chars(value: str, field: str) -> None:
    if any(char in value for char in _CONTROL_CHARS):
        raise serializers.ValidationError(f"{field} must not contain NUL or newlines.")


def validate_relative_repo_path(value: str, field: str) -> str:
    """
    Accept only a repo-relative path.

    Rejects absolute paths (POSIX *and* Windows), drive letters, UNC paths,
    backslashes, and any ``..`` segment. This is the first of two gates; the
    second (:func:`integrations.paths.resolve_inside`) re-checks the resolved
    real path against the clone root to catch symlink escapes.
    """
    _reject_control_chars(value, field)

    if value != value.strip():
        raise serializers.ValidationError(f"{field} must not have surrounding whitespace.")
    if not value:
        raise serializers.ValidationError(f"{field} must not be empty.")
    if "\\" in value:
        raise serializers.ValidationError(f"{field} must use forward slashes.")
    if _WINDOWS_DRIVE_RE.match(value):
        raise serializers.ValidationError(f"{field} must be relative, not a drive path.")
    if value.startswith("/") or posixpath.isabs(value) or ntpath.isabs(value):
        raise serializers.ValidationError(f"{field} must be relative, not absolute.")
    if any(segment == ".." for segment in value.split("/")):
        raise serializers.ValidationError(f"{field} must not contain '..'.")
    return value


class BuildEnvelopeSerializer(serializers.Serializer):
    """Identity of the request. Unknown extra keys are ignored here."""

    request_id = serializers.UUIDField()
    application_id = serializers.IntegerField(min_value=1)
    owner_id = serializers.IntegerField(min_value=1)


class BuildSnapshotSerializer(serializers.Serializer):
    """The build description. Every field is re-validated from scratch."""

    name = serializers.CharField(max_length=MAX_NAME_LENGTH)
    repository_url = serializers.CharField(max_length=MAX_URL_LENGTH)
    branch = serializers.CharField(max_length=MAX_BRANCH_LENGTH)
    application_type = serializers.ChoiceField(choices=ApplicationType.values)
    source_directory = serializers.CharField(max_length=MAX_PATH_LENGTH)
    requirements_file = serializers.CharField(
        max_length=MAX_PATH_LENGTH, required=False, allow_null=True
    )
    port = serializers.IntegerField(min_value=1, max_value=65535)
    run_command = serializers.CharField(max_length=MAX_RUN_COMMAND_LENGTH)

    def validate_name(self, value: str) -> str:
        # Also becomes part of the Docker Hub repository name.
        if not NAME_RE.match(value):
            raise serializers.ValidationError(
                "name must match [a-z0-9-]+ (lowercase letters, digits, hyphens)."
            )
        return value

    def validate_repository_url(self, value: str) -> str:
        _reject_control_chars(value, "repository_url")
        if value != value.strip():
            raise serializers.ValidationError("repository_url must not have surrounding whitespace.")

        parsed = urlparse(value)
        if parsed.scheme != "https":
            # v1 supports public https clones only; no ssh, no git://, no file://.
            raise serializers.ValidationError("repository_url must use https.")
        if not parsed.hostname:
            raise serializers.ValidationError("repository_url must include a host.")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            # Credentials in a URL would end up in the DB and in git's argv.
            raise serializers.ValidationError("repository_url must not contain credentials.")
        return value

    def validate_branch(self, value: str) -> str:
        _reject_control_chars(value, "branch")
        if not BRANCH_RE.match(value):
            raise serializers.ValidationError("branch contains unsupported characters.")
        if value.startswith("-"):
            # Would otherwise be parsed by git as an option.
            raise serializers.ValidationError("branch must not start with '-'.")
        if ".." in value:
            raise serializers.ValidationError("branch must not contain '..'.")
        if value.startswith("/") or value.endswith("/") or "//" in value:
            raise serializers.ValidationError("branch has an invalid '/' placement.")
        if value.endswith(".lock"):
            raise serializers.ValidationError("branch must not end with '.lock'.")
        return value

    def validate_source_directory(self, value: str) -> str:
        return validate_relative_repo_path(value, "source_directory")

    def validate_requirements_file(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return validate_relative_repo_path(value, "requirements_file")

    def validate_run_command(self, value: str) -> str:
        _reject_control_chars(value, "run_command")
        if not value.strip():
            raise serializers.ValidationError("run_command must not be empty.")
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise serializers.ValidationError(f"run_command is not parseable: {exc}") from exc
        if not parts:
            raise serializers.ValidationError("run_command must contain at least one token.")
        return value


class BuildResultCallbackSerializer(serializers.Serializer):
    """
    Body of ``POST /internal/builds/<build_id>/result/``.

    Note what is *absent*: the image repository and tag. Those are read from
    the build row, never taken from the callback, so a compromised build agent
    cannot point a build at someone else's image.
    """

    status = serializers.ChoiceField(choices=["success", "failed"])
    image_digest = serializers.CharField(required=False, allow_null=True, max_length=80)
    error_code = serializers.CharField(required=False, allow_null=True, max_length=50)
    error_message = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=4000
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        status = attrs["status"]

        if status == "success":
            digest = (attrs.get("image_digest") or "").strip()
            if not SHA256_DIGEST_RE.match(digest):
                raise serializers.ValidationError(
                    {"image_digest": "must match ^sha256:[a-f0-9]{64}$ on success."}
                )
            attrs["image_digest"] = digest
            attrs["error_code"] = ""
            attrs["error_message"] = ""
            attrs["target_status"] = BuildStatus.SUCCESS
            return attrs

        code = (attrs.get("error_code") or "").strip()
        if code not in {value.value for value in CALLBACK_ERROR_CODES}:
            allowed = ", ".join(sorted(value.value for value in CALLBACK_ERROR_CODES))
            raise serializers.ValidationError(
                {"error_code": f"must be one of: {allowed}."}
            )
        attrs["error_code"] = ErrorCode(code)
        attrs["error_message"] = (attrs.get("error_message") or "").strip()
        attrs["image_digest"] = None
        attrs["target_status"] = BuildStatus.FAILED
        return attrs
