"""Read-mostly admin for operational inspection of builds."""
from __future__ import annotations

from django.contrib import admin

from builds.models import Build


@admin.register(Build)
class BuildAdmin(admin.ModelAdmin):
    list_display = (
        "build_id",
        "status",
        "application_id",
        "owner_id",
        "name",
        "application_type",
        "short_sha",
        "image_tag",
        "created_at",
    )
    list_filter = ("status", "application_type", "created_at")
    search_fields = ("build_id", "request_id", "name", "commit_sha", "image_digest")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    # Builds are written by the pipeline only; the admin is for inspection.
    readonly_fields = tuple(
        field.name for field in Build._meta.fields
    ) + ("short_sha", "image_reference")

    def has_add_permission(self, request) -> bool:  # noqa: ARG002
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: ARG002
        return False
