"""Phase 0 acceptance: the health probe reports process and database state."""

from __future__ import annotations

import pytest
from django.db.utils import OperationalError

from config import views as health_views


@pytest.mark.django_db
def test_health_returns_ok_when_database_is_reachable(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


class _UnreachableDatabase:
    """Stand-in for the connection proxy while PostgreSQL is down."""

    def cursor(self):
        raise OperationalError("could not connect to server")


@pytest.mark.django_db
def test_health_returns_503_when_database_is_unreachable(client, monkeypatch):
    monkeypatch.setattr(health_views, "connection", _UnreachableDatabase())

    response = client.get("/health/")

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "database": "unreachable"}
