"""
Django settings for the Build Service.

Every value comes from the environment with a development-friendly default so
that ``manage.py`` and ``pytest`` work on a fresh clone. Production values are
injected via the environment (see ``.env.example``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def env_str(name: str, default: str = "") -> str:
    """Return an environment string, treating "" as unset."""
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from exc


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a number, got {raw!r}") from exc


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def database_from_url(url: str) -> dict[str, object]:
    """Translate a DATABASE_URL into a Django DATABASES entry."""
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+", 1)[0]

    if scheme == "sqlite":
        # sqlite:///relative.db  or  sqlite:////abs/path.db  or sqlite://:memory:
        path = parsed.path or ""
        if parsed.netloc == ":memory:" or path in {"", "/:memory:", ":memory:"}:
            name: str | Path = ":memory:"
        else:
            # On Windows "sqlite:///C:/x.db" parses to path "/C:/x.db".
            if re.match(r"^/[A-Za-z]:", path):
                path = path[1:]
            name = Path(path) if Path(path).is_absolute() else BASE_DIR / path.lstrip("/")
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(name)}

    if scheme not in {"postgres", "postgresql", "psql", "pgsql"}:
        raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme: {scheme!r}")

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")) or "postgres",
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port) if parsed.port else "",
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 0),
    }


# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------
SECRET_KEY = env_str("SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DEBUG", False)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1", "web"])
if DEBUG:
    ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "builds",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# NOTE: BASE_DIR / "templates" is deliberately NOT a Django template dir.
# That directory holds Jinja2 *Dockerfile* templates rendered by the build
# pipeline, never HTML rendered by Django.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": database_from_url(
        env_str(
            "DATABASE_URL",
            "postgresql://build:build@localhost:5432/build_service",
        )
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # Internal endpoints authenticate with a shared token checked in the view;
    # nothing in this service relies on DRF's session/user auth.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env_str("CELERY_RESULT_BACKEND", "")  # not used; fire-and-forget
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_ENABLE_UTC = True

# Early ack: the intake task is idempotent via request_id, so a redelivery
# storm is worse than a lost message we can safely re-request.
CELERY_TASK_ACKS_LATE = False
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

CELERY_TASK_DEFAULT_QUEUE = "build"

# Queue names used across the service.
BUILD_QUEUE_INTAKE = "build"
BUILD_QUEUE_EXEC = "build_exec"
BUILD_QUEUE_NOTIFY = "build_notify"

CELERY_TASK_ROUTES = {
    # Incoming from the Application Service.
    "builds.start_build": {"queue": BUILD_QUEUE_INTAKE},
    # Long-running pipeline work.
    "builds.run_build_pipeline": {"queue": BUILD_QUEUE_EXEC},
    "builds.reconcile_stuck_builds": {"queue": BUILD_QUEUE_EXEC},
    # Outbound result reporting.
    "builds.notify_application": {"queue": BUILD_QUEUE_NOTIFY},
    "builds.notify_deployment": {"queue": BUILD_QUEUE_NOTIFY},
}

# --- Outbound contracts -----------------------------------------------------
# OPEN DECISION: the Application Service task name and queue are not agreed
# yet. Both are configurable so neither side has to redeploy to change it.
APPLICATION_RESULT_TASK_NAME = env_str(
    "APPLICATION_RESULT_TASK_NAME", "applications.record_build_result"
)
APPLICATION_RESULT_QUEUE = env_str("APPLICATION_RESULT_QUEUE", "application")

DEPLOYMENT_TASK_NAME = env_str("DEPLOYMENT_TASK_NAME", "deployments.deploy_image")
DEPLOYMENT_QUEUE = env_str("DEPLOYMENT_QUEUE", "deploy")

# --- Reconciler (Phase 10) --------------------------------------------------
BUILD_PREPARING_TIMEOUT_SECONDS = env_int("BUILD_PREPARING_TIMEOUT_SECONDS", 600)
BUILD_BUILDING_TIMEOUT_SECONDS = env_int("BUILD_BUILDING_TIMEOUT_SECONDS", 3600)
BUILD_QUEUED_REDISPATCH_SECONDS = env_int("BUILD_QUEUED_REDISPATCH_SECONDS", 300)
BUILD_WORKSPACE_MAX_AGE_SECONDS = env_int("BUILD_WORKSPACE_MAX_AGE_SECONDS", 86400)
BUILD_RECONCILE_INTERVAL_SECONDS = env_int("BUILD_RECONCILE_INTERVAL_SECONDS", 180)

CELERY_BEAT_SCHEDULE = {
    "reconcile-stuck-builds": {
        "task": "builds.reconcile_stuck_builds",
        "schedule": float(BUILD_RECONCILE_INTERVAL_SECONDS),
        "options": {"queue": BUILD_QUEUE_EXEC, "expires": BUILD_RECONCILE_INTERVAL_SECONDS},
    },
}


# ---------------------------------------------------------------------------
# Build Service specific
# ---------------------------------------------------------------------------
BUILD_WORKSPACE_ROOT = env_str("BUILD_WORKSPACE_ROOT", "/tmp/builds")
BUILD_TEMPLATES_ROOT = str(BASE_DIR / "templates")

INTERNAL_SERVICE_TOKEN = env_str("INTERNAL_SERVICE_TOKEN", "dev-internal-token")
BUILD_CALLBACK_BASE_URL = env_str("BUILD_CALLBACK_BASE_URL", "http://web:8000")

JENKINS_URL = env_str("JENKINS_URL", "http://jenkins:8080")
JENKINS_USER = env_str("JENKINS_USER", "")
JENKINS_API_TOKEN = env_str("JENKINS_API_TOKEN", "")
JENKINS_JOB_NAME = env_str("JENKINS_JOB_NAME", "build-image")
JENKINS_TIMEOUT_SECONDS = env_int("JENKINS_TIMEOUT_SECONDS", 30)

DOCKERHUB_NAMESPACE = env_str("DOCKERHUB_NAMESPACE", "example")
PLATFORM_NAME = env_str("PLATFORM_NAME", "iiap")

GIT_TIMEOUT_SECONDS = env_int("GIT_TIMEOUT_SECONDS", 120)

# file:// clones are needed by the test suite (it builds throwaway local
# repositories). Production must keep this off: v1 clones public https only.
GIT_ALLOW_FILE_URLS = env_bool("GIT_ALLOW_FILE_URLS", False)

# Bounded in-step retries for retryable failures (network, executor down).
BUILD_RETRY_ATTEMPTS = env_int("BUILD_RETRY_ATTEMPTS", 3)
BUILD_RETRY_BASE_DELAY_SECONDS = env_float("BUILD_RETRY_BASE_DELAY_SECONDS", 2.0)

# --- Optional read-only status endpoint (Phase 10, item 2) ------------------
# Off by default: the Application Service already owns build status for end
# users. Enable only if it needs this service to answer directly. JWTs are
# verified locally against a public key — the Auth Service is never called.
BUILD_STATUS_API_ENABLED = env_bool("BUILD_STATUS_API_ENABLED", False)
JWT_PUBLIC_KEY = env_str("JWT_PUBLIC_KEY", "").replace("\\n", "\n")
JWT_ALGORITHM = env_str("JWT_ALGORITHM", "RS256")
JWT_ISSUER = env_str("JWT_ISSUER", "")
JWT_AUDIENCE = env_str("JWT_AUDIENCE", "")
JWT_OWNER_CLAIM = env_str("JWT_OWNER_CLAIM", "sub")


# ---------------------------------------------------------------------------
# Logging: plain lines, build_id included by the caller in each message.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
        },
    },
    "root": {"handlers": ["console"], "level": env_str("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
