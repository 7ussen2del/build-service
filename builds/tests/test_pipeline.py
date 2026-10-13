"""Phase 4 tests: the build pipeline orchestration (README sections 5-6, 9)."""
import base64
import uuid
from unittest.mock import MagicMock

import pytest
from django.conf import settings

from builds import templating
from builds.models import Build, BuildStatus
from builds.tasks import run_build_pipeline
from integrations import git, jenkins


@pytest.fixture
def build(db):
    return Build.objects.create(
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
        status=BuildStatus.QUEUED,
    )


@pytest.fixture
def happy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DOCKERHUB_NAMESPACE", "acme")
    monkeypatch.setattr(settings, "PLATFORM_NAME", "autodeploy")
    monkeypatch.setattr(settings, "BUILD_CALLBACK_BASE_URL", "https://cb")
    monkeypatch.setattr(settings, "JENKINS_URL", "https://jenkins")
    monkeypatch.setattr(git, "resolve_commit", MagicMock(return_value="a" * 40))
    monkeypatch.setattr(git, "shallow_clone", MagicMock())
    monkeypatch.setattr(git, "resolve_in_repo", MagicMock())
    monkeypatch.setattr(
        templating, "render_dockerfile", MagicMock(return_value="FROM x\n")
    )
    trigger = MagicMock()
    monkeypatch.setattr(jenkins, "trigger_build", trigger)
    return trigger


def test_pipeline_happy_path_triggers_jenkins(build, happy):
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.BUILDING
    assert build.commit_sha == "a" * 40
    happy.assert_called_once()
    kwargs = happy.call_args.kwargs
    assert kwargs["image_repository"] == "acme/autodeploy-17-mates"
    assert kwargs["image_tag"] == "a" * 7
    assert kwargs["callback_url"] == f"https://cb/internal/builds/{build.build_id}/result/"
    assert base64.b64decode(kwargs["dockerfile_b64"]).decode() == "FROM x\n"


def test_pipeline_git_error_maps_to_git_error(build, happy, monkeypatch):
    monkeypatch.setattr(
        git, "resolve_commit", MagicMock(side_effect=git.GitError("no repo"))
    )
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "GIT_ERROR"


def test_pipeline_path_error_maps_to_invalid_payload(build, happy, monkeypatch):
    monkeypatch.setattr(
        git, "resolve_in_repo", MagicMock(side_effect=git.PathError("missing"))
    )
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "INVALID_PAYLOAD"


def test_pipeline_template_error_maps_to_template_error(build, happy, monkeypatch):
    monkeypatch.setattr(
        templating,
        "render_dockerfile",
        MagicMock(side_effect=templating.TemplateError("bad")),
    )
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "TEMPLATE_ERROR"


def test_pipeline_jenkins_error_maps_to_executor_unavailable(build, happy, monkeypatch):
    monkeypatch.setattr(
        jenkins, "trigger_build", MagicMock(side_effect=jenkins.JenkinsError("down"))
    )
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.FAILED
    assert build.error_code == "EXECUTOR_UNAVAILABLE"


def test_pipeline_second_dispatch_is_noop(build, happy):
    run_build_pipeline(str(build.build_id))
    run_build_pipeline(str(build.build_id))
    build.refresh_from_db()
    assert build.status == BuildStatus.BUILDING
    assert happy.call_count == 1


def test_pipeline_cleans_up_workspace(build, happy, tmp_path):
    run_build_pipeline(str(build.build_id))
    assert list(tmp_path.iterdir()) == []
