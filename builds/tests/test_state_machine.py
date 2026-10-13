"""Phase 0/1 tests: project configuration and the state machine (README section 4)."""
import uuid

import pytest
from django.conf import settings

from builds.models import ApplicationType, Build, BuildStatus
from builds.services import InvalidTransition, transition


def test_project_configuration():
    assert settings.CELERY_TASK_DEFAULT_QUEUE == "build"
    assert "builds" in settings.INSTALLED_APPS


def _make_build(**overrides):
    data = dict(
        request_id=uuid.uuid4(),
        application_id=42,
        owner_id=17,
        name="mates",
        repository_url="https://github.com/omar-mn/mates",
        branch="main",
        application_type=ApplicationType.DJANGO,
        source_directory="src",
        requirements_file="requirements.txt",
        port=8000,
        run_command="bash ./run.sh",
    )
    data.update(overrides)
    return Build.objects.create(**data)


@pytest.mark.django_db
def test_queued_to_building_sets_started_at():
    build = _make_build()
    assert build.status == BuildStatus.QUEUED
    updated = transition(build.build_id, BuildStatus.BUILDING)
    assert updated.status == BuildStatus.BUILDING
    assert updated.started_at is not None


@pytest.mark.django_db
def test_building_to_success_sets_finished_at_and_image_fields():
    build = _make_build()
    transition(build.build_id, BuildStatus.BUILDING)
    updated = transition(
        build.build_id,
        BuildStatus.SUCCESS,
        image_repository="ns/django-17-mates",
        image_tag="a82f91c",
        image_digest="sha256:" + "a" * 64,
    )
    assert updated.status == BuildStatus.SUCCESS
    assert updated.finished_at is not None
    assert updated.image_tag == "a82f91c"


@pytest.mark.django_db
def test_queued_to_failed_records_error():
    build = _make_build()
    updated = transition(
        build.build_id, BuildStatus.FAILED, error_code="INVALID_PAYLOAD"
    )
    assert updated.status == BuildStatus.FAILED
    assert updated.finished_at is not None
    assert updated.error_code == "INVALID_PAYLOAD"


@pytest.mark.django_db
def test_building_to_failed_allowed():
    build = _make_build()
    transition(build.build_id, BuildStatus.BUILDING)
    updated = transition(build.build_id, BuildStatus.FAILED, error_code="BUILD_FAILED")
    assert updated.status == BuildStatus.FAILED


@pytest.mark.django_db
@pytest.mark.parametrize("bad_target", [BuildStatus.SUCCESS, BuildStatus.QUEUED])
def test_queued_cannot_skip_or_stay(bad_target):
    build = _make_build()
    with pytest.raises(InvalidTransition):
        transition(build.build_id, bad_target)


@pytest.mark.django_db
def test_success_is_final():
    build = _make_build()
    transition(build.build_id, BuildStatus.BUILDING)
    transition(build.build_id, BuildStatus.SUCCESS)
    for target in (BuildStatus.BUILDING, BuildStatus.FAILED, BuildStatus.SUCCESS):
        with pytest.raises(InvalidTransition):
            transition(build.build_id, target)


@pytest.mark.django_db
def test_failed_is_final():
    build = _make_build()
    transition(build.build_id, BuildStatus.FAILED)
    for target in (BuildStatus.BUILDING, BuildStatus.SUCCESS, BuildStatus.QUEUED):
        with pytest.raises(InvalidTransition):
            transition(build.build_id, target)


@pytest.mark.django_db
def test_only_one_worker_can_start_build():
    """QUEUED->BUILDING happens once; a second start attempt is rejected.

    select_for_update in transition() serializes concurrent workers on a
    transactional DB, so the second worker observes BUILDING and is rejected.
    """
    build = _make_build()
    transition(build.build_id, BuildStatus.BUILDING)
    with pytest.raises(InvalidTransition):
        transition(build.build_id, BuildStatus.BUILDING)


@pytest.mark.django_db
def test_unknown_field_is_rejected():
    build = _make_build()
    with pytest.raises(ValueError):
        transition(build.build_id, BuildStatus.BUILDING, bogus="x")
