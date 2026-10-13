"""Git operations: commit resolution, pinned shallow clone, path containment (README section 5).

Only public repositories are supported. Every git command uses an argument list
(never shell=True), a timeout, and GIT_TERMINAL_PROMPT=0 so nothing blocks waiting
for credentials.
"""
import os
import subprocess
from pathlib import Path


class GitError(Exception):
    """Repo/branch not found, private repo, or a git network/timeout problem."""


class PathError(Exception):
    """A requested path is missing or escapes the repository root."""


def _run_git(args, timeout, cwd=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitError(f"git could not be run: {exc}") from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout


def _parse_commit(stdout, branch):
    wanted = f"refs/heads/{branch}"
    first = None
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if first is None:
            first = sha
        if ref == wanted:
            return sha
    if first is not None:
        return first
    raise GitError(f"branch '{branch}' not found")


def resolve_commit(repository_url, branch, timeout):
    """Resolve the exact commit SHA that ``branch`` currently points to."""
    stdout = _run_git(["ls-remote", repository_url, branch], timeout=timeout)
    return _parse_commit(stdout, branch)


def shallow_clone(repository_url, commit_sha, dest, timeout):
    """Shallow-clone ``repository_url`` pinned to exactly ``commit_sha`` into ``dest``."""
    os.makedirs(dest, exist_ok=True)
    _run_git(["init", "--quiet", dest], timeout=timeout)
    _run_git(["-C", dest, "remote", "add", "origin", repository_url], timeout=timeout)
    _run_git(
        ["-C", dest, "fetch", "--depth", "1", "origin", commit_sha], timeout=timeout
    )
    _run_git(["-C", dest, "checkout", "--quiet", commit_sha], timeout=timeout)


def resolve_in_repo(repo_root, relative_path):
    """Resolve ``relative_path`` inside ``repo_root``; reject escapes, require existence.

    ``Path.resolve`` follows symlinks and collapses ``..``, so a symlink or a path
    that points outside the repository is rejected here.
    """
    root = Path(repo_root).resolve()
    target = (root / relative_path).resolve()
    if root != target and root not in target.parents:
        raise PathError(f"path '{relative_path}' escapes the repository")
    if not target.exists():
        raise PathError(f"path '{relative_path}' does not exist")
    return target
