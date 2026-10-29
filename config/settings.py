"""Django settings for the Build Service.

All configuration comes from environment variables (README section 11).
"""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-key")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

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
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get(
            "DATABASE_URL", "sqlite:///" + str(BASE_DIR / "db.sqlite3")
        ),
        conn_max_age=600,
    )
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Celery / broker
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TASK_DEFAULT_QUEUE = "build"

# Build Service configuration (README section 11)
BUILD_WORKSPACE_ROOT = os.environ.get("BUILD_WORKSPACE_ROOT", "/tmp/builds")
GIT_TIMEOUT_SECONDS = int(os.environ.get("GIT_TIMEOUT_SECONDS", "60"))
JENKINS_URL = os.environ.get("JENKINS_URL", "")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_API_TOKEN = os.environ.get("JENKINS_API_TOKEN", "")
BUILD_CALLBACK_BASE_URL = os.environ.get("BUILD_CALLBACK_BASE_URL", "")
INTERNAL_SERVICE_TOKEN = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
DOCKERHUB_NAMESPACE = os.environ.get("DOCKERHUB_NAMESPACE", "")
PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "")
