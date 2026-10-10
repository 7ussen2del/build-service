"""Phase 0 acceptance: settings read from the environment correctly."""
from __future__ import annotations

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.settings import database_from_url, env_bool, env_int, env_list


def test_postgres_url_is_parsed() -> None:
    parsed = database_from_url("postgresql://u:p%40ss@db.internal:6543/buildsvc")

    assert parsed["ENGINE"] == "django.db.backends.postgresql"
    assert parsed["NAME"] == "buildsvc"
    assert parsed["USER"] == "u"
    assert parsed["PASSWORD"] == "p@ss"
    assert parsed["HOST"] == "db.internal"
    assert parsed["PORT"] == "6543"


def test_sqlite_url_is_parsed() -> None:
    parsed = database_from_url("sqlite:///local.sqlite3")

    assert parsed["ENGINE"] == "django.db.backends.sqlite3"
    assert str(parsed["NAME"]).endswith("local.sqlite3")


def test_unsupported_scheme_is_rejected() -> None:
    with pytest.raises(ImproperlyConfigured):
        database_from_url("mysql://u:p@localhost/db")


def test_env_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_BOOL", "TRUE")
    monkeypatch.setenv("X_INT", "42")
    monkeypatch.setenv("X_LIST", "a, b ,,c")

    assert env_bool("X_BOOL") is True
    assert env_int("X_INT", 0) == 42
    assert env_list("X_LIST", []) == ["a", "b", "c"]

    monkeypatch.setenv("X_INT", "not-a-number")
    with pytest.raises(ImproperlyConfigured):
        env_int("X_INT", 0)


def test_build_settings_are_present() -> None:
    for name in (
        "BUILD_WORKSPACE_ROOT",
        "BUILD_TEMPLATES_ROOT",
        "INTERNAL_SERVICE_TOKEN",
        "BUILD_CALLBACK_BASE_URL",
        "JENKINS_URL",
        "DOCKERHUB_NAMESPACE",
        "PLATFORM_NAME",
        "GIT_TIMEOUT_SECONDS",
    ):
        assert getattr(settings, name) not in (None, "")


def test_jinja_template_dir_is_not_a_django_template_dir() -> None:
    # templates/ holds Dockerfile.j2 files for the build pipeline; Django must
    # not try to load them as HTML templates.
    assert settings.TEMPLATES[0]["DIRS"] == []
