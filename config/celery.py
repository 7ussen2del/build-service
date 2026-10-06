"""Celery application for the Build Service.

Queues are declared in ``config.settings`` (``CELERY_TASK_ROUTES``) so the
layout is configuration rather than code:

    builds.start_build      -> build        intake: creates the Build row
    builds.pipeline.*       -> build_exec   clone, render, build, push
    builds.notifications.*  -> build_notify downstream callbacks

The intake queue is consumed by a separate worker from the pipeline queue, so
a long-running build can never delay the acceptance of a new request.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("build_service")

# Read every CELERY_* key from Django settings, so there is exactly one place
# where broker, routing and worker behaviour are configured.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Pick up @shared_task definitions from every app in INSTALLED_APPS.
app.autodiscover_tasks()


if __name__ == "__main__":  # pragma: no cover
    app.start()
