"""Phase 0 acceptance: Celery is wired the way the plan requires."""
from __future__ import annotations

from config.celery import app as celery_app


def test_early_ack_and_single_prefetch() -> None:
    # Early ack + prefetch 1: redelivery is handled by request_id idempotency,
    # not by holding messages unacked for the length of a build.
    assert celery_app.conf.task_acks_late is False
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_queue_routing() -> None:
    routes = celery_app.conf.task_routes

    assert routes["builds.start_build"]["queue"] == "build"
    assert routes["builds.run_build_pipeline"]["queue"] == "build_exec"
    assert routes["builds.notify_application"]["queue"] == "build_notify"
    assert routes["builds.notify_deployment"]["queue"] == "build_notify"


def test_json_only_serialization() -> None:
    assert celery_app.conf.task_serializer == "json"
    assert list(celery_app.conf.accept_content) == ["json"]
