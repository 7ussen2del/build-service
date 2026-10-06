"""Phase 1 acceptance tests for the Build data model and its selectors.

The database schema itself is under test too: pytest-django applies every
migration to build the test database, so a broken ``0001_initial`` fails these
tests before a single assertion runs.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from builds import selectors
from builds.models import ApplicationType, Build, BuildStatus, NotificationStatus

pytestmark = pytest.mark.django_db


def make_build(**overrides) -> Build:
    """Create a Build with valid defaults, overridable per field.

    Only the fields that have no model default are supplied here; everything
    else (status, notification state, timestamps, ...) is left to the model so
    the tests actually exercise those defaults.
    """
    kwargs = {
        "request_id": uuid.uuid4(),
        "application_id": 1,
        "owner_id": 1,
        "name": "demo",
        "repository_url": "https://example.com/acme/demo.git",
        "branch": "main",
        "application_type": ApplicationType.DJANGO,
    }
    kwargs.update(overrides)
    return Build.objects.create(**kwargs)


# --------------------------------------------------------------------------- #
# Creation and defaults
# --------------------------------------------------------------------------- #
def test_create_build_persists_with_a_generated_build_id():
    build = make_build()

    assert build.pk is not None
    assert isinstance(build.build_id, uuid.UUID)
    assert Build.objects.count() == 1


def test_status_defaults_to_queued():
    assert make_build().status == BuildStatus.QUEUED


def test_notification_state_defaults_to_pending_and_zero():
    build = make_build()

    assert build.app_notification_status == NotificationStatus.PENDING
    assert build.deployment_notification_status == NotificationStatus.PENDING
    assert build.app_notification_attempts == 0
    assert build.deployment_notification_attempts == 0
    assert build.app_notified_at is None
    assert build.deployment_notified_at is None


def test_optional_snapshot_fields_start_null_or_empty():
    build = make_build()

    # Nullable fields are None until the pipeline fills them.
    assert build.commit_sha is None
    assert build.port is None
    assert build.requirements_file is None
    assert build.executor_handle is None
    assert build.log_reference is None
    assert build.rendered_dockerfile_hash is None
    assert build.started_at is None
    assert build.finished_at is None
    # Non-null text fields default to the empty string, not None.
    assert build.image_digest == ""
    assert build.error_code == ""
    assert build.error_message == ""


def test_timestamps_are_populated_on_create():
    build = make_build()

    assert build.created_at is not None
    assert build.updated_at is not None


def test_str_includes_build_id_and_status():
    build = make_build()
    assert str(build.build_id) in str(build)
    assert build.status in str(build)


# --------------------------------------------------------------------------- #
# Uniqueness constraints
# --------------------------------------------------------------------------- #
def test_request_id_is_unique():
    shared = uuid.uuid4()
    make_build(request_id=shared)

    with pytest.raises(IntegrityError):
        # Wrapped so the aborted statement stays contained in a savepoint and
        # the surrounding test transaction remains usable.
        with transaction.atomic():
            make_build(request_id=shared)


def test_build_id_is_unique():
    shared = uuid.uuid4()
    make_build(build_id=shared)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_build(build_id=shared)


# --------------------------------------------------------------------------- #
# Port check constraint
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_port", [0, 65536, 70000])
def test_port_outside_1_to_65535_is_rejected(bad_port):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            make_build(port=bad_port)


@pytest.mark.parametrize("good_port", [1, 8000, 65535])
def test_port_within_range_is_accepted(good_port):
    assert make_build(port=good_port).port == good_port


def test_null_port_is_accepted():
    assert make_build(port=None).port is None


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #
def test_get_build_by_build_id_round_trips():
    build = make_build()
    assert selectors.get_build_by_build_id(build.build_id) == build


def test_get_build_by_build_id_returns_none_when_missing():
    assert selectors.get_build_by_build_id(uuid.uuid4()) is None


def test_get_build_by_request_id_round_trips():
    build = make_build()
    assert selectors.get_build_by_request_id(build.request_id) == build


def test_list_builds_for_application_filters_by_application():
    mine = make_build(application_id=42)
    make_build(application_id=99)

    assert list(selectors.list_builds_for_application(42)) == [mine]


def test_list_builds_for_application_orders_newest_first():
    now = timezone.now()
    older = make_build(application_id=7)
    newer = make_build(application_id=7)
    # auto_now_add is set on insert, so force distinct timestamps directly
    # rather than relying on sub-millisecond create ordering.
    Build.objects.filter(pk=older.pk).update(created_at=now - timedelta(minutes=5))
    Build.objects.filter(pk=newer.pk).update(created_at=now)

    result = list(selectors.list_builds_for_application(7))

    assert [b.pk for b in result] == [newer.pk, older.pk]


def test_list_stuck_builds_matches_status_and_age_only():
    now = timezone.now()
    stuck = make_build(status=BuildStatus.BUILDING)
    fresh = make_build(status=BuildStatus.BUILDING)
    terminal = make_build(status=BuildStatus.SUCCESS)

    # Backdate only the stuck row past the cutoff. .update() bypasses auto_now.
    Build.objects.filter(pk=stuck.pk).update(updated_at=now - timedelta(hours=2))
    cutoff = now - timedelta(hours=1)

    result = list(selectors.list_stuck_builds(BuildStatus.BUILDING, cutoff))

    assert stuck in result
    assert fresh not in result  # updated too recently
    assert terminal not in result  # wrong status


# --------------------------------------------------------------------------- #
# is_terminal helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (BuildStatus.QUEUED, False),
        (BuildStatus.PREPARING, False),
        (BuildStatus.BUILDING, False),
        (BuildStatus.PUSHING, False),
        (BuildStatus.SUCCESS, True),
        (BuildStatus.FAILED, True),
    ],
)
def test_is_terminal(status, expected):
    assert make_build(status=status).is_terminal is expected
