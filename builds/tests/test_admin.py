import uuid

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from builds.admin import BuildAdmin
from builds.models import ApplicationType, Build, BuildStatus


@pytest.fixture
def sample_build(db):
    return Build.objects.create(
        request_id=uuid.uuid4(),
        application_id=1,
        owner_id=1,
        name="test-app",
        repository_url="https://github.com/example/repo",
        branch="main",
        application_type=ApplicationType.DJANGO,
        source_directory="src",
        run_command="python manage.py runserver",
        status=BuildStatus.QUEUED,
    )


@pytest.fixture
def staff_user(db):
    return User.objects.create_superuser(
        username="staff_admin",
        email="admin@example.com",
        password="password123",
    )


@pytest.mark.django_db
def test_build_model_admin_registration():
    """Verify Build model is registered with expected list display, filters, and search fields."""
    assert admin.site.is_registered(Build)
    model_admin = admin.site._registry[Build]
    assert isinstance(model_admin, BuildAdmin)

    assert model_admin.list_display == (
        "build_id",
        "name",
        "status",
        "application_type",
        "branch",
        "created_at",
    )
    assert model_admin.list_filter == ("status", "application_type")
    assert model_admin.search_fields == ("build_id", "name", "repository_url")
    assert "build_id" in model_admin.readonly_fields
    assert "request_id" in model_admin.readonly_fields
    assert "created_at" in model_admin.readonly_fields


@pytest.mark.django_db
def test_admin_access_control(sample_build, staff_user):
    """Verify admin requires staff authentication and renders build list and detail pages."""
    from django.test import RequestFactory

    factory = RequestFactory()
    model_admin = admin.site._registry[Build]

    # Anonymous user
    request = factory.get(reverse("admin:builds_build_changelist"))
    from django.contrib.auth.models import AnonymousUser
    request.user = AnonymousUser()
    response = admin.site.admin_view(model_admin.changelist_view)(request)
    assert response.status_code == 302
    assert "/admin/login/" in response.url

    # Staff user changelist
    request = factory.get(reverse("admin:builds_build_changelist"))
    request.user = staff_user
    response = model_admin.changelist_view(request)
    assert response.status_code == 200

    # Staff user changeform
    request = factory.get(reverse("admin:builds_build_change", args=[sample_build.pk]))
    request.user = staff_user
    response = model_admin.changeform_view(request, object_id=str(sample_build.pk))
    assert response.status_code == 200
