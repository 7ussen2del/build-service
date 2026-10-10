"""Config package: exposes the Celery app so tasks register on Django start."""
from __future__ import annotations

from config.celery import app as celery_app

__all__ = ("celery_app",)
