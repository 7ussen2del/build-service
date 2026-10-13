"""Celery application for the Build Service."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("build_service")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
