from django.contrib import admin

from .models import Build


@admin.register(Build)
class BuildAdmin(admin.ModelAdmin):
    list_display = (
        "build_id",
        "name",
        "status",
        "application_type",
        "branch",
        "created_at",
    )
    list_filter = (
        "status",
        "application_type",
    )
    search_fields = (
        "build_id",
        "name",
        "repository_url",
    )
    readonly_fields = (
        "build_id",
        "request_id",
        "commit_sha",
        "image_repository",
        "image_tag",
        "image_digest",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
    )
