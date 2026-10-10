"""
Git access.

Rules that hold for every call in this module:

* ``shell=False`` with an argument list — never a shell string.
* An explicit timeout, so a hung network call cannot pin a worker forever.
* A scrubbed environment: ``GIT_TERMINAL_PROMPT=0`` plus no system/global git
  config and no credential helpers, so git can never prompt or silently pick
  up someone's stored token.
* No credentials anywhere. v1 clones public https repositories only.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from builds.errors import BuildError, ErrorCode

logger = logging.getLogger(__name__)

COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9/._-]+$")

MAX_STDERR_CHARS = 500
HEAD_TIMEOUT_SECONDS = 30

# stderr fingerprints, checked in this order. Auth comes first because a
# private repository reports "not found" to an unauthenticated client only
# *after* it has failed to get credentials.
_AUTH_SIGNS = (
    "could not read username",
    "could not read password",
    "authentication failed",
    "terminal prompts disabled",
    "invalid username or password",
    "permission denied",
    "access denied",
    "askpass",
)
_NOT_FOUND_SIGNS = (
    "repository not found",
    "not found",
    "does not appear to be a git repository",
    "does not exist",
    "no such file or directory",
    "not a git repository",
)
_NETWORK_SIGNS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection refused",
    "connection reset",
    "operation timed out",
    "network is unreachable",
    "temporary failure in name resolution",
    "early eof",
    "rpc failed",
    "unexpected disconnect",
    "ssl",
    "tls",
    "gnutls",
)


# ---------------------------------------------------------------------------
# Input guards (defence in depth: intake validated these already)
# ---------------------------------------------------------------------------
def _assert_safe_url(repository_url: str) -> None:
    if not isinstance(repository_url, str) or not repository_url.strip():
        raise BuildError(ErrorCode.REPO_NOT_FOUND, "repository url is empty")
    if any(char in repository_url for char in ("\x00", "\n", "\r", " ", "\t")):
        raise BuildError(ErrorCode.REPO_NOT_FOUND, "repository url contains illegal characters")
    if repository_url.startswith("-"):
        # Would be parsed by git as an option rather than a URL.
        raise BuildError(ErrorCode.REPO_NOT_FOUND, "repository url must not start with '-'")

    if repository_url.startswith("https://"):
        return
    if repository_url.startswith("file://") and getattr(settings, "GIT_ALLOW_FILE_URLS", False):
        # Enabled only by the test suite, never in a deployed configuration.
        return
    raise BuildError(ErrorCode.REPO_NOT_FOUND, "unsupported repository url scheme")


def _assert_safe_ref(branch: str) -> None:
    if not isinstance(branch, str) or not branch.strip():
        raise BuildError(ErrorCode.BRANCH_NOT_FOUND, "branch is empty")
    if not BRANCH_RE.match(branch) or branch.startswith("-") or ".." in branch:
        raise BuildError(ErrorCode.BRANCH_NOT_FOUND, f"branch {branch!r} is not a valid ref")


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------
def _git_env() -> dict[str, str]:
    """A minimal environment: git must never prompt or read user config."""
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        "GCM_INTERACTIVE": "never",
        # Stable, parseable English error strings for classification.
        "LC_ALL": "C",
        "LANG": "C",
    }
    # Pass through only what git needs to run at all. Notably absent:
    # anything that could carry a credential.
    for name in (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SystemRoot",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    ):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _sanitized(stderr: str) -> str:
    return " ".join((stderr or "").split())[:MAX_STDERR_CHARS]


def _classify(stderr: str, *, fallback: ErrorCode = ErrorCode.REPO_NOT_FOUND) -> BuildError:
    text = (stderr or "").lower()
    detail = _sanitized(stderr)

    if any(sign in text for sign in _AUTH_SIGNS):
        return BuildError(ErrorCode.GIT_AUTH_FAILED, f"git authentication failed: {detail}")
    if any(sign in text for sign in _NOT_FOUND_SIGNS):
        return BuildError(ErrorCode.REPO_NOT_FOUND, f"repository or ref missing: {detail}")
    if any(sign in text for sign in _NETWORK_SIGNS):
        return BuildError(ErrorCode.GIT_NETWORK_ERROR, f"git network error: {detail}")
    return BuildError(fallback, f"git failed: {detail}")


def _run_git(
    args: Sequence[str],
    *,
    timeout: int | None = None,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_timeout = timeout or settings.GIT_TIMEOUT_SECONDS
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user-controlled argv[0]
            ["git", *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            env=_git_env(),
            cwd=str(cwd) if cwd is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            ErrorCode.GIT_NETWORK_ERROR,
            f"git {args[0]} timed out after {effective_timeout}s",
        ) from exc
    except FileNotFoundError as exc:
        raise BuildError(ErrorCode.INTERNAL_ERROR, "git executable not found") from exc


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
def _force_rmtree(path: Path) -> None:
    """Remove a tree, clearing the read-only bits git leaves on Windows."""
    if not path.exists():
        return

    def handle(func, target, _exc):  # type: ignore[no-untyped-def]
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError as err:
            logger.warning("workspace cleanup: could not remove %s (%s)", target, err)

    try:
        shutil.rmtree(path, onexc=handle)
    except TypeError:  # pragma: no cover - Python < 3.12
        shutil.rmtree(path, onerror=lambda f, t, e: handle(f, t, e[1]))


@contextmanager
def build_workspace(build_id: object) -> Iterator[Path]:
    """
    Yield ``<BUILD_WORKSPACE_ROOT>/<build_id>/repo`` and always delete it.

    The directory itself is deliberately *not* created: ``git clone`` wants to
    create its own destination.
    """
    root = Path(settings.BUILD_WORKSPACE_ROOT) / str(build_id)
    repo = root / "repo"
    try:
        root.mkdir(parents=True, exist_ok=True)
        yield repo
    finally:
        _force_rmtree(root)
        logger.info("build_id=%s workspace removed", build_id)


def purge_stale_workspaces(max_age_seconds: int) -> int:
    """
    Delete workspaces a crashed worker never cleaned up.

    Returns the number of directories removed.
    """
    root = Path(settings.BUILD_WORKSPACE_ROOT)
    if not root.is_dir() or max_age_seconds <= 0:
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:  # pragma: no cover - vanished mid-scan
            continue
        logger.info("removing stale workspace %s", child.name)
        _force_rmtree(child)
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def resolve_commit(repository_url: str, branch: str) -> str:
    """
    Resolve a branch to the exact 40-character commit SHA it points at now.

    Pinning the SHA here is what makes the rest of the build reproducible: the
    clone, the image tag, and the recorded result all refer to this commit even
    if the branch moves a second later.
    """
    _assert_safe_url(repository_url)
    _assert_safe_ref(branch)

    result = _run_git(["ls-remote", "--heads", repository_url, f"refs/heads/{branch}"])
    if result.returncode != 0:
        raise _classify(result.stderr)

    output = (result.stdout or "").strip()
    if not output:
        # git succeeded and reported no matching ref.
        raise BuildError(
            ErrorCode.BRANCH_NOT_FOUND, f"branch {branch!r} does not exist in the repository"
        )

    sha = output.split()[0].strip().lower()
    if not COMMIT_SHA_RE.match(sha):
        raise BuildError(
            ErrorCode.REPO_NOT_FOUND, "could not parse a commit sha from ls-remote output"
        )
    return sha


def head_commit(repository_path: Path | str) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=repository_path, timeout=HEAD_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise BuildError(
            ErrorCode.INTERNAL_ERROR, f"could not read HEAD: {_sanitized(result.stderr)}"
        )
    sha = (result.stdout or "").strip().lower()
    if not COMMIT_SHA_RE.match(sha):
        raise BuildError(ErrorCode.INTERNAL_ERROR, "could not parse HEAD commit sha")
    return sha


def shallow_clone(
    repository_url: str,
    branch: str,
    destination: Path | str,
    *,
    expected_sha: str | None = None,
) -> str:
    """
    Shallow-clone a single branch and return the cloned HEAD commit.

    When ``expected_sha`` is given and HEAD differs, the branch moved between
    resolution and clone: that is reported as ``COMMIT_MISMATCH`` rather than
    silently building a different commit than the one recorded on the row.
    """
    _assert_safe_url(repository_url)
    _assert_safe_ref(branch)

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    # A retried clone would otherwise fail on "destination path already
    # exists" after a partial attempt.
    _force_rmtree(destination_path)

    result = _run_git(
        [
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            branch,
            repository_url,
            str(destination_path),
        ]
    )
    if result.returncode != 0:
        raise _classify(result.stderr)

    head = head_commit(destination_path)
    if expected_sha and head != expected_sha.lower():
        raise BuildError(
            ErrorCode.COMMIT_MISMATCH,
            f"branch {branch!r} moved during preparation: "
            f"expected {expected_sha}, cloned {head}",
        )
    return head
