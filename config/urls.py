"""Root URL configuration."""
from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

from builds.urls import internal_urlpatterns
from config.views import health

urlpatterns = [
    path("health/", health, name="health"),
    # Service-to-service only: token authenticated, never routed publicly.
    # Deliberately un-namespaced so `reverse("build-result-callback")` works
    # from the Jenkins client without hard-coding the prefix.
    path("internal/", include(internal_urlpatterns)),
    path("admin/", admin.site.urls),
]
