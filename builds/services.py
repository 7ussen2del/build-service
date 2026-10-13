"""Build lifecycle services: the guarded status-change helper and the build pipeline."""
import base64
import logging
import os
import shutil
import tempfile

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import git, jenkins, naming
from . import templating
from .models import Build, BuildStatus

logger = logging.getLogger(__name__)

# Allowed state transitions. SUCCESS and FAILED are final.
_ALLOWED_TRANSITIONS = {
    BuildStatus.QUEUED: {BuildStatus.BUILDING, BuildStatus.FAILED},
    BuildStatus.BUILDING: {BuildStatus.SUCCESS, BuildStatus.FAILED},
    BuildStatus.SUCCESS: set(),
    BuildStatus.FAILED: set(),
}

# Result fields that may be set atomically alongside a status change.
_SETTABLE_FIELDS = {
    "commit_sha",
    "image_repository",
    "image_tag",
    "image_digest",
    "error_code",
    "error_message",
}


class InvalidTransition(Exception):
    """Raised when a status change is not allowed by the state machine."""


@transaction.atomic
def transition(build_id, to_status, **fields):
    """Move a build to ``to_status``, locking the row and rejecting invalid moves.

    The row is locked with ``select_for_update``, so only one worker can perform a
    given transition: a second concurrent ``QUEUED -> BUILDING`` attempt observes
    the row already in ``BUILDING`` and is rejected.
    """
    build = Build.objects.select_for_update().get(build_id=build_id)

    if to_status not in _ALLOWED_TRANSITIONS[build.status]:
        raise InvalidTransition(f"{build.status} -> {to_status}")

    unknown = set(fields) - _SETTABLE_FIELDS
    if unknown:
        raise ValueError(f"Cannot set fields via transition: {sorted(unknown)}")

    build.status = to_status
    if to_status == BuildStatus.BUILDING and build.started_at is None:
        build.started_at = timezone.now()
    if to_status in (BuildStatus.SUCCESS, BuildStatus.FAILED):
        build.finished_at = timezone.now()
    for key, value in fields.items():
        setattr(build, key, value)

    build.save()
    return build


def _fail(build_id, code, message):
    try:
        transition(build_id, BuildStatus.FAILED, error_code=code, error_message=message[:2000])
    except InvalidTransition:
        logger.warning("Could not fail build %s (already terminal)", build_id)


def run_pipeline(build_id):
    """Resolve the commit, clone, check paths, render, and trigger Jenkins (README 5-6).

    On success the build stays ``BUILDING``; Jenkins reports the result later via the
    callback. Each failure maps to its error code (README section 9).
    """
    try:
        build = transition(build_id, BuildStatus.BUILDING)
    except InvalidTransition:
        logger.info("Build %s is not QUEUED; skipping pipeline", build_id)
        return
    except Build.DoesNotExist:
        logger.warning("Build %s not found; skipping pipeline", build_id)
        return

    workspace = None
    try:
        sha = git.resolve_commit(
            build.repository_url, build.branch, settings.GIT_TIMEOUT_SECONDS
        )
        repository = naming.image_repository(
            settings.DOCKERHUB_NAMESPACE, settings.PLATFORM_NAME, build.owner_id, build.name
        )
        tag = naming.image_tag(sha)
        # Repository and tag are fixed at build time; the result callback reads them
        # from the record (README section 7) and supplies only the digest.
        Build.objects.filter(build_id=build_id).update(
            commit_sha=sha, image_repository=repository, image_tag=tag
        )

        os.makedirs(settings.BUILD_WORKSPACE_ROOT, exist_ok=True)
        workspace = tempfile.mkdtemp(
            prefix=f"{build_id}-", dir=settings.BUILD_WORKSPACE_ROOT
        )
        repo_dir = os.path.join(workspace, "repo")
        git.shallow_clone(
            build.repository_url, sha, repo_dir, settings.GIT_TIMEOUT_SECONDS
        )

        git.resolve_in_repo(repo_dir, build.source_directory)
        if build.requirements_file:
            git.resolve_in_repo(repo_dir, build.requirements_file)

        dockerfile = templating.render_dockerfile(
            build.application_type,
            source_directory=build.source_directory,
            run_command=build.run_command,
            port=build.port,
            requirements_file=build.requirements_file,
        )

        callback_url = (
            settings.BUILD_CALLBACK_BASE_URL.rstrip("/")
            + f"/internal/builds/{build_id}/result/"
        )
        jenkins.trigger_build(
            base_url=settings.JENKINS_URL,
            user=settings.JENKINS_USER,
            token=settings.JENKINS_API_TOKEN,
            timeout=settings.GIT_TIMEOUT_SECONDS,
            build_id=str(build_id),
            repo_url=build.repository_url,
            commit_sha=sha,
            dockerfile_b64=base64.b64encode(dockerfile.encode()).decode(),
            image_repository=repository,
            image_tag=tag,
            callback_url=callback_url,
        )
    except git.PathError as exc:
        _fail(build_id, "INVALID_PAYLOAD", str(exc))
    except git.GitError as exc:
        _fail(build_id, "GIT_ERROR", str(exc))
    except templating.TemplateError as exc:
        _fail(build_id, "TEMPLATE_ERROR", str(exc))
    except jenkins.JenkinsError as exc:
        _fail(build_id, "EXECUTOR_UNAVAILABLE", str(exc))
    except Exception:
        logger.exception("Unexpected error building %s", build_id)
        _fail(build_id, "INTERNAL_ERROR", "An unexpected error occurred.")
    finally:
        if workspace:
            shutil.rmtree(workspace, ignore_errors=True)
