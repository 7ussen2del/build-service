"""Phase 0 acceptance: the health probe answers 200 without a DB."""
from __future__ import annotations

from django.test import Client


def test_health_returns_200(client: Client) -> None:
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "build-service"}
