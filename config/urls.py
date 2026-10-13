"""Root URL configuration.

The build-result endpoint is internal only (README section 7) and must not be
exposed through the public gateway.
"""
from django.urls import include, path

urlpatterns = [
    path("", include("builds.urls")),
]
