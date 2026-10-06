"""Read-only queries over :class:`~builds.models.Build`.

Every read the rest of the service needs is a named function here, so query
shapes live in one place and the service and view layers never assemble ORM
filters inline. Writes do not belong here -- they go through ``services.py``
(Phase 3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import QuerySet

from builds.models import Build


def get_build_by_build_id(build_id: uuid.UUID | str) -> Build | None:
    """Return the build with this public id, or ``None`` if there is none."""
    return Build.objects.filter(build_id=build_id).first()


def get_build_by_request_id(request_id: uuid.UUID | str) -> Build | None:
    """Return the build created for this idempotency key, or ``None``.

    Intake calls this first so a retried request resolves to the existing row
    instead of starting a second build.
    """
    return Build.objects.filter(request_id=request_id).first()


def list_builds_for_application(application_id: int) -> QuerySet[Build]:
    """All builds for one application, newest first (``Meta.ordering``)."""
    return Build.objects.filter(application_id=application_id)


def list_stuck_builds(status: str, older_than: datetime) -> QuerySet[Build]:
    """Builds sitting in ``status`` and untouched since ``older_than``.

    ``updated_at`` (``auto_now``) is the "last progress" signal: a build the
    pipeline is still advancing keeps bumping it, so a row whose ``updated_at``
    predates ``older_than`` has genuinely stalled. The reconciler (Phase 10)
    uses this to find builds to fail or retry. Oldest first, so the build that
    has been stuck longest is handled first.
    """
    return Build.objects.filter(
        status=status, updated_at__lt=older_than
    ).order_by("updated_at")
