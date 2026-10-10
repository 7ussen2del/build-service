"""
Internal HTTP surface.

Only the build engine calls these endpoints, authenticated with a shared
internal token. There is no user-facing write API here: builds are started
over Celery, not over HTTP.
"""
from __future__ import annotations

import hmac
import logging

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from builds import services
from builds.models import Build, BuildStatus
from builds.serializers import BuildResultCallbackSerializer

logger = logging.getLogger(__name__)

BEARER_PREFIX = "Bearer "


def _token_is_valid(request: Request) -> tuple[bool, str]:
    """Constant-time comparison of the internal service token."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith(BEARER_PREFIX):
        return False, "missing"

    presented = header[len(BEARER_PREFIX) :].strip()
    expected = str(settings.INTERNAL_SERVICE_TOKEN or "")
    if not expected:  # pragma: no cover - misconfiguration guard
        logger.error("INTERNAL_SERVICE_TOKEN is not configured; rejecting callback")
        return False, "unconfigured"

    if not hmac.compare_digest(presented, expected):
        return False, "wrong"
    return True, ""


class BuildResultCallbackView(APIView):
    """
    ``POST /internal/builds/<build_id>/result/``

    Idempotent by design: the same result twice is a 200 no-op, a conflicting
    result is a 409, and neither can rewrite a build that already finished.
    """

    # The caller is a machine holding a shared token, not a Django user.
    authentication_classes: list = []
    permission_classes: list = []

    def post(self, request: Request, build_id: str) -> Response:
        valid, reason = _token_is_valid(request)
        if not valid:
            logger.warning("build_id=%s callback rejected: token %s", build_id, reason)
            http_status = (
                status.HTTP_401_UNAUTHORIZED
                if reason in ("missing", "unconfigured")
                else status.HTTP_403_FORBIDDEN
            )
            return Response({"detail": "Invalid internal token."}, status=http_status)

        try:
            build = Build.objects.get(build_id=build_id)
        except (Build.DoesNotExist, ValueError, TypeError):
            # Unknown ids are 404 whether malformed or simply absent.
            return Response({"detail": "Build not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = BuildResultCallbackSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(
                "build_id=%s callback rejected: invalid body fields=%s",
                build_id,
                sorted(serializer.errors),
            )
            return Response(
                {"detail": "Invalid callback body.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = serializer.validated_data
        target = result["target_status"]

        # Already terminal: either the identical result (no-op) or a conflict.
        if build.is_terminal:
            if self._is_same_result(build, result):
                logger.info("build_id=%s duplicate callback ignored", build_id)
                return Response(
                    {"detail": "Already recorded.", "status": build.status},
                    status=status.HTTP_200_OK,
                )
            logger.warning(
                "build_id=%s conflicting callback: %s already recorded, got %s",
                build_id,
                build.status,
                target,
            )
            return Response(
                {
                    "detail": "Build already has a different terminal result.",
                    "status": build.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            if target == BuildStatus.SUCCESS:
                # Repository and tag come from the row, never from the body.
                updated = services.transition(
                    build.build_id,
                    BuildStatus.SUCCESS,
                    image_digest=result["image_digest"],
                    error_code="",
                    error_message="",
                )
            else:
                updated = services.mark_failed(
                    build.build_id, result["error_code"], result["error_message"]
                )
        except services.InvalidTransition:
            # e.g. a result for a build that never reached BUILDING.
            logger.warning(
                "build_id=%s callback rejected: cannot go %s -> %s",
                build_id,
                build.status,
                target,
            )
            return Response(
                {"detail": f"Cannot apply {target} to a {build.status} build."},
                status=status.HTTP_409_CONFLICT,
            )
        except services.ImmutableFieldError:
            logger.warning("build_id=%s callback tried to rewrite a recorded fact", build_id)
            return Response(
                {"detail": "Build artifact facts are already recorded."},
                status=status.HTTP_409_CONFLICT,
            )

        transaction.on_commit(lambda: services.dispatch_result_notifications(updated))
        logger.info("build_id=%s callback recorded status=%s", build_id, updated.status)
        return Response(
            {"detail": "Recorded.", "status": updated.status}, status=status.HTTP_200_OK
        )

    @staticmethod
    def _is_same_result(build: Build, result: dict) -> bool:
        if result["target_status"] != build.status:
            return False
        if build.status == BuildStatus.SUCCESS:
            return build.image_digest == result["image_digest"]
        # A repeated failure is the same result regardless of its code: the
        # first recorded reason is the one that stands.
        return True
