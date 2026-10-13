"""Internal result-callback endpoint (README section 7).

Internal only: this must not be exposed through the public gateway. The Bearer
token is compared in constant time; repository and tag come from the Build record,
not the request; an already-final build is a 200 no-op.
"""
import re

from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import tasks
from .models import Build, BuildStatus
from .services import InvalidTransition, transition

_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def _check_token(request):
    """None = missing/malformed (401); False = wrong (403); True = ok."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    expected = settings.INTERNAL_SERVICE_TOKEN
    if not expected or not header.startswith("Bearer "):
        return None
    return constant_time_compare(header[len("Bearer ") :], expected)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def build_result(request, build_id):
    token = _check_token(request)
    if token is None:
        return Response({"detail": "Missing or malformed token."}, status=401)
    if not token:
        return Response({"detail": "Invalid token."}, status=403)

    data = request.data if isinstance(request.data, dict) else {}
    result = data.get("status")
    if result not in ("success", "failed"):
        return Response({"detail": "Invalid status."}, status=400)

    try:
        build = Build.objects.get(build_id=build_id)
    except Build.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)

    if build.status in (BuildStatus.SUCCESS, BuildStatus.FAILED):
        return Response({"detail": "Already final; no change."}, status=200)

    if result == "success":
        digest = data.get("image_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            return Response({"detail": "Invalid image_digest."}, status=400)
        try:
            transition(build_id, BuildStatus.SUCCESS, image_digest=digest)
        except InvalidTransition:
            return Response({"detail": "Conflict."}, status=409)
        tasks.notify_application.delay(str(build_id))
        tasks.notify_deployment.delay(str(build_id))
    else:
        try:
            transition(
                build_id,
                BuildStatus.FAILED,
                error_code=str(data.get("error_code") or "BUILD_FAILED")[:50],
                error_message=str(data.get("error_message") or "")[:2000],
            )
        except InvalidTransition:
            return Response({"detail": "Conflict."}, status=409)
        tasks.notify_application.delay(str(build_id))

    return Response({"detail": "Recorded."}, status=200)
