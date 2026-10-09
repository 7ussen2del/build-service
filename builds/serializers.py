"""Validation for incoming build requests (Phase 2).

Two serializers, matching the plan's two-level validation:

* ``EnvelopeSerializer`` -- the three routing/identity fields. If these are bad
  there is no row to create and nobody to notify, so the task discards the
  message.
* ``BuildRequestSerializer`` -- the snapshot itself. If these are bad the
  service still records a FAILED build (``INVALID_PAYLOAD``) for the audit
  trail, so this layer only decides valid-vs-invalid; it never trusts the
  values enough to let the model or DB guards be skipped.

Nothing here touches the database. Re-validation is mandatory even though the
Application Service is "trusted" (plan, hard rules).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from rest_framework import serializers

from builds.models import ApplicationType

# Upper bounds: generous for real inputs, tight enough that a multi-kilobyte
# value can never reach the database or, later, a Dockerfile template.
MAX_URL_LENGTH = 500
MAX_BRANCH_LENGTH = 255
MAX_NAME_LENGTH = 100
MAX_PATH_LENGTH = 255
MAX_RUN_COMMAND_LENGTH = 500

# A branch name: an alnum, then alnum plus / . _ -. The leading "-" and ".."
# rules are checked separately so the error message is specific.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# An image-name component: lowercase alnum with inner hyphens only.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Hook for a reserved-port policy. v1 has none (plan: "exclude reserved ports
# if a policy exists"), so the set is empty and the check is a no-op.
RESERVED_PORTS: frozenset[int] = frozenset()


def _reject_control_chars(value: str, field: str) -> None:
    if "\x00" in value:
        raise serializers.ValidationError(f"{field} must not contain a NUL byte.")
    if "\n" in value or "\r" in value:
        raise serializers.ValidationError(f"{field} must not contain a newline.")


def _validate_relative_path(value: str, field: str) -> str:
    _reject_control_chars(value, field)
    if value.startswith("/"):
        raise serializers.ValidationError(
            f"{field} must be a relative path, not absolute."
        )
    # These paths are joined onto a POSIX checkout later; a backslash is a
    # traversal vector on some tooling, so forbid it here too.
    if "\\" in value:
        raise serializers.ValidationError(f"{field} must use forward slashes only.")
    if ".." in value.split("/"):
        raise serializers.ValidationError(f"{field} must not contain '..'.")
    return value


class EnvelopeSerializer(serializers.Serializer):
    """The identity fields that must be present before any work can happen."""

    request_id = serializers.UUIDField()
    application_id = serializers.IntegerField(min_value=1)
    owner_id = serializers.IntegerField(min_value=1)


class BuildRequestSerializer(serializers.Serializer):
    """The build snapshot. Field names mirror the ``Build`` model columns."""

    name = serializers.CharField(max_length=MAX_NAME_LENGTH)
    repository_url = serializers.CharField(max_length=MAX_URL_LENGTH)
    branch = serializers.CharField(max_length=MAX_BRANCH_LENGTH)
    application_type = serializers.ChoiceField(choices=ApplicationType.choices)
    port = serializers.IntegerField(min_value=1, max_value=65535)
    run_command = serializers.CharField(max_length=MAX_RUN_COMMAND_LENGTH)
    source_directory = serializers.CharField(
        max_length=MAX_PATH_LENGTH, required=False, allow_blank=True, default=""
    )
    requirements_file = serializers.CharField(
        max_length=MAX_PATH_LENGTH,
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None,
    )

    def validate_name(self, value: str) -> str:
        if not _NAME_RE.match(value):
            raise serializers.ValidationError(
                "name must be lowercase letters, digits and hyphens, and may not "
                "start or end with a hyphen (it becomes part of the image name)."
            )
        return value

    def validate_repository_url(self, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https":
            raise serializers.ValidationError(
                "repository_url must use https (ssh, git and file URLs are rejected)."
            )
        if not parts.hostname:
            raise serializers.ValidationError("repository_url has no host.")
        if parts.username or parts.password:
            raise serializers.ValidationError(
                "repository_url must not embed credentials (user:pass@)."
            )
        return value

    def validate_branch(self, value: str) -> str:
        if value.startswith("-"):
            raise serializers.ValidationError("branch must not start with '-'.")
        if ".." in value:
            raise serializers.ValidationError("branch must not contain '..'.")
        if not _BRANCH_RE.match(value):
            raise serializers.ValidationError(
                "branch may contain only letters, digits and / . _ -."
            )
        return value

    def validate_port(self, value: int) -> int:
        if value in RESERVED_PORTS:
            raise serializers.ValidationError(f"port {value} is reserved.")
        return value

    def validate_run_command(self, value: str) -> str:
        _reject_control_chars(value, "run_command")
        if not value.strip():
            raise serializers.ValidationError("run_command must not be empty.")
        return value

    def validate_source_directory(self, value: str) -> str:
        if value == "":
            return value
        return _validate_relative_path(value, "source_directory")

    def validate_requirements_file(self, value):
        if value in (None, ""):
            return value
        return _validate_relative_path(value, "requirements_file")
