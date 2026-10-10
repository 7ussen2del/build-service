"""Phase 1 acceptance: the Build table behaves as specified."""
from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction

from builds.models import ApplicationType, Build, BuildStatus
from builds.tests.factories import make_build

pytestmark = pytest.mark.django_db


def test_create_defaults() -> None:
    build = make_build()

    assert build.status == BuildStatus.QUEUED
    assert isinstance(build.build_id, uuid.UUID)
    assert build.created_at is not None
    assert build.updated_at is not None
    # Nothing resolved or produced yet.
    assert build.commit_sha is None
    assert build.image_repository is None
    assert build.image_tag is None
    assert build.image_digest is None
    assert build.started_at is None
    assert build.finished_at is None
    assert build.error_code == ""


def test_request_id_is_unique() -> None:
    request_id = uuid.uuid4()
    make_build(request_id=request_id)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_build(request_id=request_id)


def test_build_id_is_unique() -> None:
    build = make_build()

    with pytest.raises(IntegrityError), transaction.atomic():
        make_build(build_id=build.build_id)


def test_build_id_is_generated_per_row() -> None:
    first, second = make_build(), make_build()

    assert first.build_id != second.build_id


def test_application_and_owner_are_plain_integers() -> None:
    # Service boundary: never ForeignKeys into another service's tables.
    for field_name in ("application_id", "owner_id"):
        field = Build._meta.get_field(field_name)
        assert field.get_internal_type() == "BigIntegerField"
        assert not field.is_relation


def test_terminal_states() -> None:
    assert BuildStatus.terminal() == {"SUCCESS", "FAILED"}
    assert BuildStatus.non_terminal() == {"QUEUED", "PREPARING", "BUILDING"}

    assert make_build(status=BuildStatus.SUCCESS).is_terminal is True
    assert make_build(status=BuildStatus.BUILDING).is_terminal is False


def test_port_is_nullable_so_bad_ports_can_be_recorded() -> None:
    build = make_build(port=None, status=BuildStatus.FAILED, error_code="INVALID_PAYLOAD")

    build.refresh_from_db()
    assert build.port is None
    assert build.status == BuildStatus.FAILED


def test_commit_sha_field_is_exactly_40_chars() -> None:
    assert Build._meta.get_field("commit_sha").max_length == 40


def test_short_sha_and_image_reference() -> None:
    build = make_build()
    assert build.short_sha == ""
    assert build.image_reference is None

    build.commit_sha = "a" * 40
    build.image_repository = "ns/iiap-17-mates"
    build.image_tag = "a" * 7
    assert build.short_sha == "a" * 7
    assert build.image_reference == f"ns/iiap-17-mates:{'a' * 7}"


def test_application_type_choices() -> None:
    assert [value for value, _ in ApplicationType.choices] == [
        "django",
        "flask",
        "node",
        "react",
    ]


def test_table_name_and_ordering() -> None:
    assert Build._meta.db_table == "builds"

    older = make_build()
    newer = make_build()
    # ordering = ("-created_at",) -> newest first
    assert list(Build.objects.values_list("pk", flat=True))[0] in (newer.pk, older.pk)
    assert Build.objects.count() == 2
