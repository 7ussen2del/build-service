"""Views that belong to the service itself rather than to a domain app."""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, JsonResponse


def health(request: HttpRequest) -> JsonResponse:
    """Probe used by Docker and the platform.

    Reports healthy only when the process is up *and* the database answers,
    because a worker that cannot reach PostgreSQL can accept no build.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any database error means "not healthy"
        return JsonResponse(
            {"status": "unhealthy", "database": "unreachable"}, status=503
        )
    return JsonResponse({"status": "ok", "database": "ok"})
