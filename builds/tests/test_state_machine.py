"""Phase 3 acceptance tests for the build state machine.

Three things are under test: the transition table (every allowed move, every
forbidden one), the guards that keep a finished build finished, and the
concurrency guarantee that one build can only be claimed once.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.db import connection

from builds.models import ApplicationType, Build, BuildStatus
from builds.services import (
    ALLOWED_TRANSITIONS,
    ImmutableFieldError,
    InvalidTransition,
    claim_for_preparation,
    external_status,
    mark_failed,
    transition,
)

pytestmark = pytest.mark.django_db

ALL_STATUSES = [status.value for status in BuildStatus]
TERMINAL = {BuildStatus.SUCCESS.value, BuildStatus.FAILED.value}

# Derived from the table itself, so adding a state to the state machine without
# updating these tests fails loudly rather than going untested.
ALLOWED_PAIRS = [
    (source, target)
    for source, targets in ALLOWED_TRANSITIONS.items()
    for target in sorted(targets)
]
# Everything else -- except a terminal state repeating its own result, which is
# the documented idempotent no-op rather than a forbidden transition.
FORBIDDEN_PAIRS = [
    (source, target)
    for source in ALL_STATUSES
    for target in ALL_STATUSES
    if target not in ALLOWED_TRANSITIONS[source]
    and not (source in TERMINAL and target == source)
]


def make_build(**overrides) -> Build:
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


def make_build_in(status: str, **overrides) -> Build:
    """A build forced into ``status`` without going through the state machine.

    ``QuerySet.update`` bypasses ``save()`` on purpose: the fixture must be able
    to set up any state, including ones ``transition`` would refuse to reach.
    """
    build = make_build(**overrides)
    Build.objects.filter(pk=build.pk).update(status=status)
    build.refresh_from_db()
    return build


# --------------------------------------------------------------------------- #
# The transition table
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("source", "target"), ALLOWED_PAIRS)
def test_allowed_transition_is_applied(source, target):
    build = make_build_in(source)

    returned = transition(build.build_id, target)

    assert returned.status == target
    build.refresh_from_db()
    assert build.status == target


@pytest.mark.parametrize(("source", "target"), FORBIDDEN_PAIRS)
def test_forbidden_transition_is_refused(source, target):
    build = make_build_in(source)

    with pytest.raises(InvalidTransition):
        transition(build.build_id, target)

    build.refresh_from_db()
    assert build.status == source


def test_unknown_target_status_is_refused():
    build = make_build()

    with pytest.raises(InvalidTransition):
        transition(build.build_id, "CACHING")

    build.refresh_from_db()
    assert build.status == BuildStatus.QUEUED


def test_missing_build_raises_does_not_exist():
    with pytest.raises(Build.DoesNotExist):
        transition(uuid.uuid4(), BuildStatus.PREPARING)


def test_table_covers_every_status():
    assert set(ALLOWED_TRANSITIONS) == set(ALL_STATUSES)
    assert all(not ALLOWED_TRANSITIONS[status] for status in TERMINAL)


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
def test_started_at_is_stamped_on_entering_preparing():
    build = make_build()
    assert build.started_at is None

    transition(build.build_id, BuildStatus.PREPARING)

    build.refresh_from_db()
    assert build.started_at is not None
    assert build.finished_at is None


def test_started_at_is_not_overwritten_by_later_stages():
    build = make_build()
    first = transition(build.build_id, BuildStatus.PREPARING).started_at

    transition(build.build_id, BuildStatus.BUILDING)

    build.refresh_from_db()
    assert build.started_at == first


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (BuildStatus.PUSHING.value, BuildStatus.SUCCESS.value),
        (BuildStatus.QUEUED.value, BuildStatus.FAILED.value),
        (BuildStatus.BUILDING.value, BuildStatus.FAILED.value),
    ],
)
def test_finished_at_is_stamped_on_any_terminal_state(source, target):
    build = make_build_in(source)
    assert build.finished_at is None

    transition(build.build_id, target)

    build.refresh_from_db()
    assert build.finished_at is not None


def test_updated_at_is_bumped_so_the_reconciler_sees_progress():
    build = make_build()
    before = build.updated_at

    transition(build.build_id, BuildStatus.PREPARING)

    build.refresh_from_db()
    assert build.updated_at > before


# --------------------------------------------------------------------------- #
# Write-once fields
# --------------------------------------------------------------------------- #
def test_write_once_field_is_recorded():
    build = make_build_in(BuildStatus.PREPARING.value)
    sha = "a" * 40

    transition(build.build_id, BuildStatus.BUILDING, commit_sha=sha)

    build.refresh_from_db()
    assert build.commit_sha == sha


def test_rewriting_a_set_field_is_refused():
    build = make_build_in(BuildStatus.PREPARING.value, commit_sha="a" * 40)

    with pytest.raises(ImmutableFieldError):
        transition(build.build_id, BuildStatus.BUILDING, commit_sha="b" * 40)

    build.refresh_from_db()
    assert build.commit_sha == "a" * 40
    # The refusal rolled the whole transition back, status included.
    assert build.status == BuildStatus.PREPARING


def test_rewriting_a_set_field_with_the_same_value_is_allowed():
    sha = "a" * 40
    build = make_build_in(BuildStatus.PREPARING.value, commit_sha=sha)

    transition(build.build_id, BuildStatus.BUILDING, commit_sha=sha)

    build.refresh_from_db()
    assert build.status == BuildStatus.BUILDING
    assert build.commit_sha == sha


def test_mutable_executor_fields_may_change():
    build = make_build_in(BuildStatus.PREPARING.value, log_reference="first")

    transition(build.build_id, BuildStatus.BUILDING, log_reference="second")

    build.refresh_from_db()
    assert build.log_reference == "second"


def test_unwritable_field_is_a_programming_error():
    build = make_build()

    # Identity, ownership and the request snapshot are not the state machine's
    # to change -- and a typo must not be silently dropped either.
    for field, value in [
        ("application_id", 99),
        ("request_id", uuid.uuid4()),
        ("branch", "other"),
        ("statuss", BuildStatus.FAILED),
    ]:
        with pytest.raises(ValueError):
            transition(build.build_id, BuildStatus.PREPARING, **{field: value})

    build.refresh_from_db()
    assert build.status == BuildStatus.QUEUED


# --------------------------------------------------------------------------- #
# Terminal idempotency
# --------------------------------------------------------------------------- #
def test_repeating_the_same_terminal_result_is_a_no_op():
    build = make_build_in(BuildStatus.PUSHING.value)
    transition(build.build_id, BuildStatus.SUCCESS, image_digest="sha256:abc")
    build.refresh_from_db()
    finished_at = build.finished_at

    # An at-least-once executor callback, delivered twice.
    returned = transition(build.build_id, BuildStatus.SUCCESS, image_digest="sha256:abc")

    assert returned.status == BuildStatus.SUCCESS
    build.refresh_from_db()
    assert build.finished_at == finished_at
    assert build.image_digest == "sha256:abc"


def test_a_differing_terminal_result_is_ignored_not_applied():
    build = make_build_in(BuildStatus.BUILDING.value)
    mark_failed(build.build_id, "DOCKER_BUILD_FAILED", "exit 1")

    # Same verdict, different detail: a no-op, and the first result stands.
    mark_failed(build.build_id, "SOMETHING_ELSE", "other")

    build.refresh_from_db()
    assert build.error_code == "DOCKER_BUILD_FAILED"
    assert build.error_message == "exit 1"


def test_a_successful_build_can_never_be_failed_afterwards():
    build = make_build_in(BuildStatus.PUSHING.value)
    transition(build.build_id, BuildStatus.SUCCESS)

    # A failed downstream notification must not undo a successful build.
    with pytest.raises(InvalidTransition):
        mark_failed(build.build_id, "NOTIFY_FAILED", "deployment service down")

    build.refresh_from_db()
    assert build.status == BuildStatus.SUCCESS
    assert build.error_code == ""


def test_a_failed_build_can_never_claim_success_afterwards():
    build = make_build_in(BuildStatus.BUILDING.value)
    mark_failed(build.build_id, "DOCKER_BUILD_FAILED")

    with pytest.raises(InvalidTransition):
        transition(build.build_id, BuildStatus.SUCCESS)

    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED


# --------------------------------------------------------------------------- #
# mark_failed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        BuildStatus.QUEUED.value,
        BuildStatus.PREPARING.value,
        BuildStatus.BUILDING.value,
        BuildStatus.PUSHING.value,
    ],
)
def test_mark_failed_works_from_every_active_state(source):
    build = make_build_in(source)

    mark_failed(build.build_id, "WORKER_CRASHED", "no heartbeat")

    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "WORKER_CRASHED"
    assert build.error_message == "no heartbeat"
    assert build.finished_at is not None


# --------------------------------------------------------------------------- #
# Pipeline claim guard
# --------------------------------------------------------------------------- #
def test_claim_succeeds_once_then_refuses():
    build = make_build()

    assert claim_for_preparation(build.build_id) is True
    assert claim_for_preparation(build.build_id) is False

    build.refresh_from_db()
    assert build.status == BuildStatus.PREPARING


@pytest.mark.parametrize(
    "source",
    [
        BuildStatus.BUILDING.value,
        BuildStatus.SUCCESS.value,
        BuildStatus.FAILED.value,
    ],
)
def test_claim_refuses_a_build_that_is_not_queued(source):
    build = make_build_in(source)

    assert claim_for_preparation(build.build_id) is False

    build.refresh_from_db()
    assert build.status == source


def test_claim_of_an_unknown_build_returns_false():
    assert claim_for_preparation(uuid.uuid4()) is False


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_claims_and_only_one_wins():
    """The real guarantee: two workers, one row, exactly one winner.

    ``transaction=True`` is required -- each thread needs its own connection and
    must see the other's committed work, which the default test transaction
    would hide.
    """
    build = make_build()
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def claim() -> None:
        try:
            barrier.wait(timeout=10)
            outcomes.append(claim_for_preparation(build.build_id))
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a claiming thread never finished"

    assert not errors, errors
    assert sorted(outcomes) == [False, True]

    build.refresh_from_db()
    assert build.status == BuildStatus.PREPARING
    assert build.started_at is not None


# --------------------------------------------------------------------------- #
# External status mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (BuildStatus.QUEUED.value, "queued"),
        (BuildStatus.PREPARING.value, "in_progress"),
        (BuildStatus.BUILDING.value, "in_progress"),
        (BuildStatus.PUSHING.value, "in_progress"),
        (BuildStatus.SUCCESS.value, "success"),
        (BuildStatus.FAILED.value, "failed"),
    ],
)
def test_external_status_collapses_the_internal_stages(status, expected):
    assert external_status(Build(status=status)) == expected


def test_external_status_never_leaks_an_internal_stage_name():
    exposed = {external_status(Build(status=status)) for status in ALL_STATUSES}
    assert exposed == {"queued", "in_progress", "success", "failed"}
