"""Root URL configuration.

The build-result endpoint is internal only (README section 7) and must not be
exposed through the public gateway.
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("builds.urls")),
]
