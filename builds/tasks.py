"""Celery tasks for the builds app.

Phase 2 wires intake:

* ``start_build`` (task name ``builds.start_build``) is the entry point the
  Application Service calls with ``send_task`` on the ``build`` queue. It
  validates the envelope and hands off to the service.
* ``run_build_pipeline`` (task name ``builds.pipeline.run_build``) is the
  pipeline entry point on the ``build_exec`` queue. It is a stub here; Phase 8
  implements the real claim -> clone -> render -> execute walk. It exists now
  so intake has a dispatch target and the exec worker has something to consume.
"""

from __future__ import annotations

import logging

from celery import shared_task

from builds.serializers import EnvelopeSerializer
from builds.services import create_build_from_request

logger = logging.getLogger("builds")


@shared_task(name="builds.start_build")
def start_build(payload: dict) -> None:
    if not isinstance(payload, dict):
        logger.error(
            "Discarding build request: payload is not an object (got %s).",
            type(payload).__name__,
        )
        return

    envelope = EnvelopeSerializer(data=payload)
    if not envelope.is_valid():
        # A missing/invalid request_id, application_id or owner_id means there
        # is no row to create and nobody to notify -- drop the message.
        logger.error(
            "Discarding build request with invalid envelope: %s", envelope.errors
        )
        return

    data = envelope.validated_data
    create_build_from_request(
        request_id=data["request_id"],
        application_id=data["application_id"],
        owner_id=data["owner_id"],
        payload=payload,
    )


@shared_task(name="builds.pipeline.run_build")
def run_build_pipeline(build_id: str) -> None:
    # Phase 8 fills this in. Kept intentionally inert so an early dispatch or a
    # redelivery is a harmless no-op until the real pipeline (and its claim
    # guard) exists.
    logger.info(
        "run_build_pipeline received build_id=%s (pipeline arrives in Phase 8).",
        build_id,
    )
