"""
Jenkins client.

Jenkins is the build engine: it clones the pinned commit, builds the image
with the Dockerfile we generated, pushes it, and reports the digest back to
``/internal/builds/<build_id>/result/``.

Two things deliberately do *not* travel through here:

* **Registry credentials.** They live in Jenkins' own credential store. This
  service never holds a Docker Hub token, so it cannot leak one.
* **The callback token.** Also a Jenkins credential, injected into the job at
  runtime rather than passed as a build parameter (parameters are visible in
  the Jenkins UI and in build history).

The Jenkins URL is internal infrastructure and is never exposed to end users;
only an opaque ``log_reference`` is stored on the build row.
"""
from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.urls import reverse

from builds.errors import BuildError, ErrorCode

logger = logging.getLogger(__name__)

CRUMB_PATH = "/crumbIssuer/api/json"


def _require_setting(name: str) -> str:
    value = str(getattr(settings, name, "") or "").strip()
    if not value:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            f"{name} is not configured",
            retryable=False,
        )
    return value


def callback_url(build_id: str) -> str:
    """Absolute URL Jenkins should POST the result to."""
    base = str(settings.BUILD_CALLBACK_BASE_URL or "").rstrip("/") + "/"
    path = reverse("build-result-callback", kwargs={"build_id": str(build_id)})
    return urljoin(base, path.lstrip("/"))


def _crumb(session: requests.Session, base_url: str, timeout: int) -> dict[str, str]:
    """Fetch a CSRF crumb when the controller requires one."""
    try:
        response = session.get(f"{base_url}{CRUMB_PATH}", timeout=timeout)
    except requests.RequestException as exc:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE, f"Jenkins is unreachable: {exc.__class__.__name__}"
        ) from exc

    if response.status_code == 404:
        # Crumb issuer disabled: nothing to send.
        return {}
    if response.status_code >= 500:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            f"Jenkins crumb issuer returned {response.status_code}",
        )
    if response.status_code in (401, 403):
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            "Jenkins rejected the service credentials",
            retryable=False,
        )

    try:
        payload = response.json()
        return {str(payload["crumbRequestField"]): str(payload["crumb"])}
    except (ValueError, KeyError):
        logger.warning("Jenkins crumb response was not understood; continuing without a crumb")
        return {}


def trigger(
    *,
    build_id: str,
    repository_url: str,
    commit_sha: str,
    dockerfile: str,
    image_repository: str,
    image_tag: str,
) -> str:
    """
    Start the ``build-image`` job and return a reference for ``log_reference``.

    Raises ``BuildError(EXECUTOR_UNAVAILABLE)`` — retryable for transport and
    5xx failures, non-retryable for a rejected credential or misconfiguration.
    """
    base_url = _require_setting("JENKINS_URL").rstrip("/")
    user = _require_setting("JENKINS_USER")
    token = _require_setting("JENKINS_API_TOKEN")
    job = _require_setting("JENKINS_JOB_NAME")
    timeout = int(settings.JENKINS_TIMEOUT_SECONDS)

    parameters = {
        "BUILD_ID": str(build_id),
        "REPO_URL": repository_url,
        "COMMIT_SHA": commit_sha,
        # Base64 keeps newlines and quoting intact across the parameter
        # boundary; the Jenkinsfile decodes it to a file.
        "DOCKERFILE_B64": base64.b64encode(dockerfile.encode("utf-8")).decode("ascii"),
        "IMAGE_REPOSITORY": image_repository,
        "IMAGE_TAG": image_tag,
        "CALLBACK_URL": callback_url(build_id),
    }

    session = requests.Session()
    session.auth = (user, token)
    try:
        headers = _crumb(session, base_url, timeout)
        try:
            response = session.post(
                f"{base_url}/job/{job}/buildWithParameters",
                data=parameters,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise BuildError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"Jenkins is unreachable: {exc.__class__.__name__}",
            ) from exc

        if response.status_code in (401, 403):
            raise BuildError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                "Jenkins rejected the service credentials",
                retryable=False,
            )
        if response.status_code == 404:
            raise BuildError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"Jenkins job {job!r} does not exist",
                retryable=False,
            )
        if response.status_code >= 500:
            raise BuildError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"Jenkins returned {response.status_code}",
            )
        if response.status_code not in (200, 201, 302):
            raise BuildError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"unexpected Jenkins response {response.status_code}",
                retryable=False,
            )

        # 201 Created carries the queue item URL; fall back to the job name.
        reference = response.headers.get("Location") or f"{job}#queued"
        logger.info("build_id=%s jenkins job triggered ref=%s", build_id, reference)
        return reference
    finally:
        session.close()


def fetch_console_log(log_reference: str, *, max_bytes: int = 200_000) -> str:
    """
    Fetch a build's console output on demand.

    Logs are never copied into this service's database; ``log_reference`` is
    stored and the text is read from Jenkins only when somebody asks.
    """
    if not log_reference:
        return ""

    base_url = _require_setting("JENKINS_URL").rstrip("/")
    user = _require_setting("JENKINS_USER")
    token = _require_setting("JENKINS_API_TOKEN")
    timeout = int(settings.JENKINS_TIMEOUT_SECONDS)

    url = log_reference if log_reference.startswith(base_url) else None
    if url is None:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            "log reference does not point at the configured Jenkins",
            retryable=False,
        )

    try:
        response = requests.get(
            urljoin(url.rstrip("/") + "/", "consoleText"),
            auth=(user, token),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE, f"Jenkins is unreachable: {exc.__class__.__name__}"
        ) from exc

    if response.status_code != 200:
        raise BuildError(
            ErrorCode.EXECUTOR_UNAVAILABLE, f"Jenkins returned {response.status_code}"
        )
    return response.text[:max_bytes]
