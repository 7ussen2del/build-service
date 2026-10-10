"""
Celery application for the Build Service.

Importing this module is what makes ``@shared_task`` registrations visible to
the worker, so it is re-exported from ``config/__init__.py``.

Run the single worker that consumes every queue with::

    celery -A config worker -l info -Q build,build_exec,build_notify
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("build_service")

# All Celery settings live in Django settings under the CELERY_ namespace.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Picks up tasks.py in every installed Django app (builds/tasks.py).
app.autodiscover_tasks()


@app.task(name="builds.debug_ping")
def debug_ping() -> str:
    """Trivial task used to smoke-test broker connectivity."""
    return "pong"
