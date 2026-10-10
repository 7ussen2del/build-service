"""Project-level views (health probe only)."""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse


def health(_request: HttpRequest) -> JsonResponse:
    """Liveness probe. Deliberately does not touch the DB or the broker."""
    return JsonResponse({"status": "ok", "service": "build-service"})
