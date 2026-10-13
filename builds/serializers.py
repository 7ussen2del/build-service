"""Validation of the incoming build request (README section 3).

Envelope fields (request_id, application_id, owner_id) are validated separately
in tasks.py, because a bad envelope cannot produce a record at all.
"""
import re
from urllib.parse import urlsplit

from rest_framework import serializers

from .models import ApplicationType

_BRANCH_RE = re.compile(r"^[A-Za-z0-9/._-]+$")
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def _check_relative_path(value):
    if value.startswith("/") or value.startswith("\\"):
        raise serializers.ValidationError("Must be a relative path (no leading '/').")
    if ".." in re.split(r"[\\/]", value):
        raise serializers.ValidationError("Must not contain '..'.")
    return value


class BuildRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    repository_url = serializers.CharField(max_length=500)
    branch = serializers.CharField(max_length=200)
    application_type = serializers.ChoiceField(choices=ApplicationType.values)
    source_directory = serializers.CharField(max_length=500)
    requirements_file = serializers.CharField(
        max_length=500, required=False, allow_null=True
    )
    port = serializers.IntegerField(
        min_value=1, max_value=65535, required=False, allow_null=True
    )
    run_command = serializers.CharField(max_length=1000)

    def validate_name(self, value):
        if not _NAME_RE.match(value):
            raise serializers.ValidationError(
                "Lowercase letters, digits and '-' only."
            )
        return value

    def validate_repository_url(self, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https":
            raise serializers.ValidationError("Must use https.")
        if not parsed.hostname:
            raise serializers.ValidationError("Must include a host.")
        if "@" in parsed.netloc:
            raise serializers.ValidationError("Must not embed credentials.")
        return value

    def validate_branch(self, value):
        if value.startswith("-"):
            raise serializers.ValidationError("Must not start with '-'.")
        if ".." in value:
            raise serializers.ValidationError("Must not contain '..'.")
        if not _BRANCH_RE.match(value):
            raise serializers.ValidationError("Contains invalid characters.")
        return value

    def validate_source_directory(self, value):
        return _check_relative_path(value)

    def validate_requirements_file(self, value):
        if value in (None, ""):
            return value
        return _check_relative_path(value)

    def validate_run_command(self, value):
        if any(c in value for c in ("\x00", "\n", "\r")):
            raise serializers.ValidationError("Must not contain NUL or newline.")
        return value
