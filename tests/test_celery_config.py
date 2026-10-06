"""Phase 0 acceptance: Celery queue routing and worker behaviour are wired."""

from __future__ import annotations

import pytest

from config.celery import app


@pytest.mark.parametrize(
    ("task_name", "expected_queue"),
    [
        # Intake runs alone on its own queue and worker.
        ("builds.start_build", "build"),
        # The pipeline steps share the work queue.
        ("builds.pipeline.resolve_commit", "build_exec"),
        ("builds.pipeline.run_build", "build_exec"),
        # Notifications never share a queue with a build.
        ("builds.notifications.notify_application", "build_notify"),
        ("builds.notifications.notify_deployment", "build_notify"),
    ],
)
def test_task_routes_send_tasks_to_the_expected_queue(task_name, expected_queue):
    route = app.amqp.router.route({}, task_name)

    assert route["queue"] is not None, f"{task_name} matched no route"
    assert route["queue"].name == expected_queue


def test_intake_and_pipeline_never_share_a_queue():
    intake = app.amqp.router.route({}, "builds.start_build")["queue"].name
    pipeline = app.amqp.router.route({}, "builds.pipeline.run_build")["queue"].name

    assert intake != pipeline


def test_worker_prefetch_is_one_message_at_a_time():
    assert app.conf.worker_prefetch_multiplier == 1


def test_tasks_are_acked_early_as_the_readme_requires():
    assert app.conf.task_acks_late is False


def test_visibility_timeout_outlasts_the_longest_build():
    # Redis redelivers an unacked message after visibility_timeout. If that is
    # shorter than a real image build, the broker starts a duplicate build.
    visibility_timeout = app.conf.broker_transport_options["visibility_timeout"]

    assert visibility_timeout >= 60 * 60


def test_celery_results_are_not_used():
    # Build state lives in PostgreSQL; nothing may depend on a broker key.
    assert app.conf.task_ignore_result is True


def test_always_eager_is_off_by_default():
    # Guards against a config regression that would run builds inside the API
    # process instead of on a worker.
    assert app.conf.task_always_eager is False
