"""
URLs for the builds app.

``internal/`` is kept separate from anything a gateway might expose publicly:
the only authentication on it is the shared service token.
"""
from __future__ import annotations

from django.urls import path

from builds.views import BuildResultCallbackView

internal_urlpatterns = [
    path(
        "builds/<uuid:build_id>/result/",
        BuildResultCallbackView.as_view(),
        name="build-result-callback",
    ),
]

urlpatterns: list = []
