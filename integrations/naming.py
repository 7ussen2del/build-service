"""
Image naming.

Docker Hub repositories allow exactly two path components
(``namespace/repository``), so the platform, the owner and the application are
folded into the second one::

    <DOCKERHUB_NAMESPACE>/<PLATFORM_NAME>-<owner_id>-<name>:<short_sha>

The tag is always the short commit SHA — never only ``latest`` — so every
image can be traced back to the exact commit it was built from.
"""
from __future__ import annotations

import re

from django.conf import settings

from builds.errors import BuildError, ErrorCode

#: One Docker path component: lowercase alphanumerics, single separators.
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_REPOSITORY_LENGTH = 255
SHORT_SHA_LENGTH = 7


def image_repository(owner_id: int, name: str) -> str:
    """Build the full Docker Hub repository path for an application."""
    namespace = str(settings.DOCKERHUB_NAMESPACE or "").strip().lower()
    platform = str(settings.PLATFORM_NAME or "").strip().lower()

    if not namespace or not platform:
        raise BuildError(
            ErrorCode.INTERNAL_ERROR,
            "DOCKERHUB_NAMESPACE and PLATFORM_NAME must both be configured",
        )
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
        raise BuildError(ErrorCode.INTERNAL_ERROR, f"invalid owner_id: {owner_id!r}")

    application = str(name or "").strip().lower()
    if not application:
        raise BuildError(ErrorCode.INTERNAL_ERROR, "application name is empty")

    path = f"{platform}-{owner_id}-{application}"
    for component in (namespace, path):
        if not COMPONENT_RE.match(component):
            raise BuildError(
                ErrorCode.INTERNAL_ERROR,
                f"{component!r} is not a valid Docker repository component",
            )

    repository = f"{namespace}/{path}"
    if len(repository) > MAX_REPOSITORY_LENGTH:
        raise BuildError(
            ErrorCode.INTERNAL_ERROR, f"repository name is too long: {len(repository)} chars"
        )
    return repository


def image_tag(commit_sha: str) -> str:
    """The short commit SHA used as the image tag."""
    sha = str(commit_sha or "").strip().lower()
    if not COMMIT_SHA_RE.match(sha):
        raise BuildError(
            ErrorCode.INTERNAL_ERROR, "commit sha must be 40 lowercase hex characters"
        )

    tag = sha[:SHORT_SHA_LENGTH]
    if not TAG_RE.match(tag):  # pragma: no cover - unreachable for hex input
        raise BuildError(ErrorCode.INTERNAL_ERROR, f"invalid image tag: {tag!r}")
    return tag


def image_reference(repository: str, tag: str) -> str:
    return f"{repository}:{tag}"
