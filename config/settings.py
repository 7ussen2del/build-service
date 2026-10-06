"""Django settings for the Build Service.

Every value is read from the environment: no secret is ever committed here.
``.env.example`` lists every variable together with its default.

The service owns its PostgreSQL database exclusively. No other service's data
is reachable from this process -- ``application_id`` / ``owner_id`` are plain
integers on our own tables, never foreign keys across a service boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from kombu import Queue

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def env_str(name: str, default: str | None = None) -> str:
    try:
        return os.environ[name]
    except KeyError:
        if default is None:
            raise ImproperlyConfigured(
                f"Required environment variable {name} is not set."
            ) from None
        return default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ImproperlyConfigured(
            f"{name} must be an integer, got {raw!r}."
        ) from None


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_map(name: str, default: dict[str, str]) -> dict[str, str]:
    """Parse ``KEY=value,KEY=value`` into a dict."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return dict(default)
    parsed: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ImproperlyConfigured(
                f"{name} entries must look like 'django=v1'; got {item!r}."
            )
        parsed[key.strip()] = value.strip()
    return parsed


DEBUG = env_bool("DEBUG", default=True)


def secret(name: str, dev_default: str) -> str:
    """Read a secret, falling back to a loud dev placeholder only while DEBUG.

    Outside DEBUG a missing secret is a hard startup failure: the service must
    never run in production with a value that is committed to the repository.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    if not DEBUG:
        raise ImproperlyConfigured(
            f"{name} must be set when DEBUG is false; secrets never live in code."
        )
    return dev_default


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY = secret("SECRET_KEY", "dev-insecure-secret-key")
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])
INTERNAL_SERVICE_TOKEN = secret("INTERNAL_SERVICE_TOKEN", "dev-internal-token")

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

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Dockerfile templates are deliberately NOT Django templates. They live
        # under TEMPLATES_ROOT and are rendered in Phase 5 through an explicit,
        # validated Jinja2 environment. Keeping them out of DIRS avoids a
        # collision between "Django templates" and "build templates".
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --------------------------------------------------------------------------- #
# Database -- owned exclusively by this service
# --------------------------------------------------------------------------- #
def database_from_url(url: str) -> dict[str, object]:
    """Translate a ``postgres://`` URL into a Django DATABASES entry."""
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured(
            f"DATABASE_URL must be a postgres:// URL, got scheme {parsed.scheme!r}."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ImproperlyConfigured(f"DATABASE_URL has an invalid port: {exc}") from exc

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(port or ""),
        "CONN_MAX_AGE": 60,
        # Fail fast instead of hanging a worker on an unreachable database.
        "OPTIONS": {"connect_timeout": 5},
    }


DATABASES = {
    "default": database_from_url(
        env_str("DATABASE_URL", "postgres://build:build@localhost:5432/build_service")
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


# --------------------------------------------------------------------------- #
# REST framework -- read APIs only (Phase 12)
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    # Fail closed: a view is private unless it says otherwise. Phase 12 adds the
    # JWT authentication class issued by the Auth service.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer"]
        if not DEBUG
        else [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
    ),
}


# --------------------------------------------------------------------------- #
# Build pipeline configuration
# --------------------------------------------------------------------------- #
# Ephemeral per-build workspaces, wiped after each build. Never inside the
# project tree, and never inside the image.
BUILD_WORKSPACE_ROOT = Path(env_str("BUILD_WORKSPACE_ROOT", "/tmp/builds"))

# Root of the Dockerfile templates: <TEMPLATES_ROOT>/<type>/<version>/...
# Layout is <type>/v1/{Dockerfile.j2,template.yaml}; the active version per
# application type is pinned below, not hardcoded in the pipeline.
TEMPLATES_ROOT = Path(env_str("TEMPLATES_ROOT", str(BASE_DIR / "build_templates")))
ACTIVE_TEMPLATE_VERSIONS = env_map(
    "ACTIVE_TEMPLATE_VERSIONS",
    {"django": "v1", "flask": "v1", "node": "v1", "react": "v1"},
)

# Guard rails applied to every incoming snapshot before any work starts.
MAX_REPO_SIZE_MB = env_int("MAX_REPO_SIZE_MB", 100)
GIT_TIMEOUT_SECONDS = env_int("GIT_TIMEOUT_SECONDS", 60)


# --------------------------------------------------------------------------- #
# Celery
#
# Queue layout is configuration, not code (see config/celery.py):
#   build        -> intake only: creates the Build row, nothing slow
#   build_exec   -> the pipeline itself (clone, render, build, push)
#   build_notify -> downstream notifications
# --------------------------------------------------------------------------- #
CELERY_BROKER_URL = env_str("CELERY_BROKER_URL", "redis://localhost:6379/0")

# Build state lives in PostgreSQL, so Celery results are deliberately unused:
# nothing may depend on a broker key surviving a Redis restart.
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND") or None
CELERY_TASK_IGNORE_RESULT = True

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# A task with no explicit route is a bug. Send it to the general work queue
# rather than to `build`, which must stay responsive for new requests.
CELERY_TASK_DEFAULT_QUEUE = "build_exec"

# Declared explicitly so the topology exists even before the first task is
# routed, and so a typo in a route name fails loudly instead of silently
# creating a queue nobody consumes.
CELERY_TASK_QUEUES = (
    Queue("build"),
    Queue("build_exec"),
    Queue("build_notify"),
)

CELERY_TASK_ROUTES = {
    "builds.start_build": {"queue": "build"},
    "builds.pipeline.*": {"queue": "build_exec"},
    "builds.notifications.*": {"queue": "build_notify"},
}

# One unacked message at a time: a slow build must not hoard the queue.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Early ack, as the service README requires. The trade-off is that a worker
# killed mid-task loses that task entirely -- the Phase 10 reconciler is what
# recovers builds left in a non-terminal state.
CELERY_TASK_ACKS_LATE = False

# Must exceed the longest normal task (a full image build) or Redis will
# redeliver a task that is still running, starting a duplicate build.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 2 * 60 * 60}

CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Test convenience only; must stay False in every deployed environment.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", default=False)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        # Build logs stay in Jenkins; the service logs references, never
        # credentials or rendered secrets.
        "builds": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
