"""URL routing for the Build Service.

The public surface is intentionally tiny: a health probe for the platform and
the read APIs added in Phase 12. Build submission does not happen over HTTP --
it arrives as a Celery task from the Application Service.
"""

from django.contrib import admin
from django.urls import path

from config.views import health

urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    # Phase 12: builds.urls -- build status and log-reference read APIs.
]
