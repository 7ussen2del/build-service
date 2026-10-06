"""Admin registration for :class:`~builds.models.Build`.

The admin is a read-only inspector. Builds are created and advanced only by the
service layer, so letting an operator edit a row by hand -- bypassing the state
machine and the immutability rules (Phase 3) -- would be a foot-gun. Every
field is visible, none is editable, and rows cannot be added or deleted here.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from builds.models import Build


@admin.register(Build)
class BuildAdmin(admin.ModelAdmin):
    list_display = (
        "build_id",
        "application_id",
        "status",
        "application_type",
        "image_tag",
        "created_at",
    )
    list_filter = (
        "status",
        "application_type",
        "app_notification_status",
        "deployment_notification_status",
    )
    search_fields = (
        "build_id",
        "request_id",
        "commit_sha",
        "repository_url",
        "image_tag",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def get_readonly_fields(self, request: HttpRequest, obj: Build | None = None):
        # Derived from the model so the read-only set never drifts as fields
        # are added.
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Build | None = None
    ) -> bool:
        return False
