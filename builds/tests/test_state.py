"""Phase 3 acceptance: the state machine is the only way status changes."""
from __future__ import annotations

import threading

import pytest
from django.conf import settings
from django.db import connection

from builds.errors import ErrorCode
from builds.models import Build, BuildStatus
from builds.services import (
    ImmutableFieldError,
    InvalidTransition,
    claim_for_preparation,
    mark_failed,
    record_fields,
    transition,
)
from builds.tests.factories import make_build

pytestmark = pytest.mark.django_db

SHA = "a" * 40

needs_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="requires Postgres row locking (set DATABASE_URL to a Postgres URL)",
)


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "target"),
    [
        (BuildStatus.QUEUED, BuildStatus.PREPARING),
        (BuildStatus.QUEUED, BuildStatus.FAILED),
        (BuildStatus.PREPARING, BuildStatus.BUILDING),
        (BuildStatus.PREPARING, BuildStatus.FAILED),
        (BuildStatus.BUILDING, BuildStatus.SUCCESS),
        (BuildStatus.BUILDING, BuildStatus.FAILED),
    ],
)
def test_allowed_transitions(start, target) -> None:
    build = make_build(status=start)

    updated = transition(build.build_id, target)

    assert updated.status == target


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (BuildStatus.QUEUED, BuildStatus.BUILDING),
        (BuildStatus.QUEUED, BuildStatus.SUCCESS),
        (BuildStatus.PREPARING, BuildStatus.QUEUED),
        (BuildStatus.PREPARING, BuildStatus.SUCCESS),
        (BuildStatus.BUILDING, BuildStatus.QUEUED),
        (BuildStatus.BUILDING, BuildStatus.PREPARING),
        (BuildStatus.SUCCESS, BuildStatus.FAILED),
        (BuildStatus.SUCCESS, BuildStatus.BUILDING),
        (BuildStatus.FAILED, BuildStatus.SUCCESS),
        (BuildStatus.FAILED, BuildStatus.PREPARING),
    ],
)
def test_forbidden_transitions(start, target) -> None:
    build = make_build(status=start)

    with pytest.raises(InvalidTransition):
        transition(build.build_id, target)

    build.refresh_from_db()
    assert build.status == start


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def test_started_at_is_set_entering_preparing() -> None:
    build = make_build()
    assert build.started_at is None

    updated = transition(build.build_id, BuildStatus.PREPARING)

    assert updated.started_at is not None
    assert updated.finished_at is None


def test_finished_at_is_set_on_terminal_states() -> None:
    build = make_build(status=BuildStatus.BUILDING)

    updated = transition(build.build_id, BuildStatus.SUCCESS, image_digest=f"sha256:{'b' * 64}")

    assert updated.finished_at is not None


def test_started_at_is_not_overwritten() -> None:
    build = make_build()
    first = transition(build.build_id, BuildStatus.PREPARING).started_at

    # A second PREPARING is a no-op, not a restart.
    again = transition(build.build_id, BuildStatus.PREPARING)

    assert again.started_at == first


# ---------------------------------------------------------------------------
# Idempotency of terminal results
# ---------------------------------------------------------------------------
def test_same_terminal_result_twice_is_a_noop() -> None:
    build = make_build(status=BuildStatus.BUILDING)
    first = transition(build.build_id, BuildStatus.SUCCESS, image_digest=f"sha256:{'c' * 64}")

    second = transition(build.build_id, BuildStatus.SUCCESS, image_digest=f"sha256:{'c' * 64}")

    assert second.finished_at == first.finished_at
    assert second.image_digest == first.image_digest


def test_duplicate_failure_keeps_the_original_error() -> None:
    build = make_build(status=BuildStatus.PREPARING)
    mark_failed(build.build_id, ErrorCode.BRANCH_NOT_FOUND, "no such branch")

    mark_failed(build.build_id, ErrorCode.INTERNAL_ERROR, "something else")

    build.refresh_from_db()
    assert build.error_code == ErrorCode.BRANCH_NOT_FOUND
    assert build.error_message == "no such branch"


def test_mark_failed_never_overturns_success() -> None:
    build = make_build(status=BuildStatus.BUILDING)
    transition(build.build_id, BuildStatus.SUCCESS, image_digest=f"sha256:{'d' * 64}")

    result = mark_failed(build.build_id, ErrorCode.INTERNAL_ERROR, "late failure")

    assert result.status == BuildStatus.SUCCESS
    build.refresh_from_db()
    assert build.status == BuildStatus.SUCCESS
    assert build.error_code == ""


def test_mark_failed_truncates_long_messages() -> None:
    build = make_build()

    mark_failed(build.build_id, ErrorCode.INTERNAL_ERROR, "x" * 5000)

    build.refresh_from_db()
    assert len(build.error_message) == 2000


# ---------------------------------------------------------------------------
# Write-once build facts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("commit_sha", SHA, "b" * 40),
        ("image_repository", "ns/iiap-17-mates", "ns/evil"),
        ("image_tag", "aaaaaaa", "bbbbbbb"),
        ("image_digest", f"sha256:{'a' * 64}", f"sha256:{'b' * 64}"),
    ],
)
def test_build_facts_cannot_be_overwritten(field, first, second) -> None:
    build = make_build(**{field: first})

    with pytest.raises(ImmutableFieldError):
        record_fields(build.build_id, **{field: second})

    build.refresh_from_db()
    assert getattr(build, field) == first


def test_identical_rewrite_of_a_build_fact_is_allowed() -> None:
    build = make_build(commit_sha=SHA)

    record_fields(build.build_id, commit_sha=SHA)

    build.refresh_from_db()
    assert build.commit_sha == SHA


def test_record_fields_does_not_change_status() -> None:
    build = make_build(status=BuildStatus.PREPARING)

    updated = record_fields(build.build_id, commit_sha=SHA, template_name="django")

    assert updated.status == BuildStatus.PREPARING
    assert updated.commit_sha == SHA
    assert updated.template_name == "django"


def test_status_cannot_be_smuggled_in_as_a_field() -> None:
    build = make_build()

    with pytest.raises(ValueError, match="status"):
        record_fields(build.build_id, status=BuildStatus.SUCCESS)


def test_unknown_field_is_rejected() -> None:
    build = make_build()

    with pytest.raises(ValueError, match="unknown Build field"):
        record_fields(build.build_id, nonexistent="x")


# ---------------------------------------------------------------------------
# The claim guard
# ---------------------------------------------------------------------------
def test_claim_moves_queued_to_preparing() -> None:
    build = make_build()

    assert claim_for_preparation(build.build_id) is True

    build.refresh_from_db()
    assert build.status == BuildStatus.PREPARING
    assert build.started_at is not None


def test_second_claim_loses() -> None:
    build = make_build()

    assert claim_for_preparation(build.build_id) is True
    assert claim_for_preparation(build.build_id) is False


@pytest.mark.parametrize(
    "status",
    [BuildStatus.PREPARING, BuildStatus.BUILDING, BuildStatus.SUCCESS, BuildStatus.FAILED],
)
def test_claim_only_works_from_queued(status) -> None:
    build = make_build(status=status)

    assert claim_for_preparation(build.build_id) is False


def test_claim_of_unknown_build_returns_false() -> None:
    import uuid

    assert claim_for_preparation(uuid.uuid4()) is False


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_have_exactly_one_winner() -> None:
    build = make_build()
    results: list[bool] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait(timeout=5)
        try:
            outcome = claim_for_preparation(build.build_id)
        finally:
            connection.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == [False, True]
    build.refresh_from_db()
    assert build.status == BuildStatus.PREPARING
