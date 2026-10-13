"""Phase 5 tests: the internal result callback endpoint (README section 7)."""
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from builds import tasks
from builds.models import Build, BuildStatus

TOKEN = "secret-token"
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def ctx(db, monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", TOKEN)
    app_delay, dep_delay = MagicMock(), MagicMock()
    monkeypatch.setattr(tasks.notify_application, "delay", app_delay)
    monkeypatch.setattr(tasks.notify_deployment, "delay", dep_delay)
    build = Build.objects.create(
        request_id=uuid.uuid4(),
        application_id=42,
        owner_id=17,
        name="mates",
        repository_url="https://github.com/omar-mn/mates",
        branch="main",
        application_type="django",
        source_directory="src",
        requirements_file="requirements.txt",
        port=8000,
        run_command="bash ./run.sh",
        status=BuildStatus.BUILDING,
        commit_sha="a" * 40,
        image_repository="acme/autodeploy-17-mates",
        image_tag="a" * 7,
    )
    return SimpleNamespace(
        client=APIClient(), build=build, app=app_delay, dep=dep_delay
    )


def _url(build):
    return f"/internal/builds/{build.build_id}/result/"


def _post(ctx, body, token=TOKEN, header=None):
    kwargs = {"format": "json"}
    if header is not None:
        kwargs["HTTP_AUTHORIZATION"] = header
    elif token is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return ctx.client.post(_url(ctx.build), body, **kwargs)


def test_missing_token_is_401(ctx):
    r = _post(ctx, {"status": "success", "image_digest": DIGEST}, token=None)
    assert r.status_code == 401


def test_malformed_token_is_401(ctx):
    r = _post(ctx, {"status": "success", "image_digest": DIGEST}, header="Token x")
    assert r.status_code == 401


def test_wrong_token_is_403(ctx):
    r = _post(ctx, {"status": "success", "image_digest": DIGEST}, token="nope")
    assert r.status_code == 403


def test_success_records_digest_and_notifies_both(ctx):
    r = _post(ctx, {"status": "success", "image_digest": DIGEST})
    assert r.status_code == 200
    ctx.build.refresh_from_db()
    assert ctx.build.status == BuildStatus.SUCCESS
    assert ctx.build.image_digest == DIGEST
    assert ctx.build.image_repository == "acme/autodeploy-17-mates"  # from the record
    ctx.app.assert_called_once()
    ctx.dep.assert_called_once()


def test_bad_digest_is_400_and_unchanged(ctx):
    r = _post(ctx, {"status": "success", "image_digest": "sha256:bad"})
    assert r.status_code == 400
    ctx.build.refresh_from_db()
    assert ctx.build.status == BuildStatus.BUILDING
    ctx.app.assert_not_called()


def test_failure_records_and_notifies_app_only(ctx):
    r = _post(
        ctx,
        {"status": "failed", "error_code": "BUILD_FAILED", "error_message": "boom"},
    )
    assert r.status_code == 200
    ctx.build.refresh_from_db()
    assert ctx.build.status == BuildStatus.FAILED
    assert ctx.build.error_code == "BUILD_FAILED"
    ctx.app.assert_called_once()
    ctx.dep.assert_not_called()


def test_already_final_build_is_noop(ctx):
    Build.objects.filter(pk=ctx.build.pk).update(
        status=BuildStatus.SUCCESS, image_digest="sha256:" + "b" * 64
    )
    r = _post(ctx, {"status": "failed", "error_code": "X"})
    assert r.status_code == 200
    ctx.build.refresh_from_db()
    assert ctx.build.status == BuildStatus.SUCCESS
    assert ctx.build.image_digest == "sha256:" + "b" * 64
    ctx.app.assert_not_called()
    ctx.dep.assert_not_called()


def test_unknown_build_is_404(ctx):
    url = f"/internal/builds/{uuid.uuid4()}/result/"
    r = ctx.client.post(
        url,
        {"status": "success", "image_digest": DIGEST},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
    )
    assert r.status_code == 404
