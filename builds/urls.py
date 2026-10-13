from django.urls import path

from . import views

urlpatterns = [
    path(
        "internal/builds/<uuid:build_id>/result/",
        views.build_result,
        name="build-result",
    ),
]
