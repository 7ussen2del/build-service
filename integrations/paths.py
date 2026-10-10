"""
Path containment.

Payload-supplied paths are checked twice: textually at intake
(:func:`builds.serializers.validate_relative_repo_path`) and again here,
against the *real* filesystem, after the repository is on disk. The second
check is what catches a symlink inside the repository pointing outside it —
something no amount of string validation can see.
"""
from __future__ import annotations

import ntpath
import posixpath
import re
from pathlib import Path

from builds.errors import BuildError, ErrorCode

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_FORBIDDEN_CHARS = ("\x00", "\n", "\r")


def _invalid(message: str) -> BuildError:
    return BuildError(ErrorCode.INVALID_PATH, message)


def resolve_inside(root: Path | str, relative: str) -> Path:
    """
    Resolve ``relative`` under ``root`` and prove the result stays inside.

    Raises :class:`BuildError` with ``INVALID_PATH`` for absolute paths, drive
    letters, backslashes, ``..`` segments, and anything whose resolved real
    path lands outside ``root``.
    """
    root_path = Path(root)
    try:
        real_root = root_path.resolve(strict=True)
    except OSError as exc:
        raise _invalid(f"repository root is unusable: {exc}") from exc
    if not real_root.is_dir():
        raise _invalid("repository root is not a directory")

    if not isinstance(relative, str) or not relative.strip():
        raise _invalid("path must be a non-empty string")
    if any(char in relative for char in _FORBIDDEN_CHARS):
        raise _invalid("path must not contain NUL or newlines")
    if "\\" in relative:
        raise _invalid("path must use forward slashes")
    if _WINDOWS_DRIVE_RE.match(relative):
        raise _invalid("path must be relative, not a drive path")
    if relative.startswith("/") or posixpath.isabs(relative) or ntpath.isabs(relative):
        raise _invalid("path must be relative, not absolute")
    if any(segment == ".." for segment in relative.split("/")):
        raise _invalid("path must not contain '..'")

    try:
        resolved = (real_root / relative).resolve()
    except OSError as exc:
        raise _invalid(f"path could not be resolved: {exc}") from exc

    if resolved != real_root and not resolved.is_relative_to(real_root):
        # Textually clean, really outside: a symlink escape.
        raise _invalid(f"path {relative!r} escapes the repository root")
    return resolved


def ensure_file_exists(root: Path | str, relative: str) -> Path:
    resolved = resolve_inside(root, relative)
    if not resolved.is_file():
        raise _invalid(f"{relative!r} is not a file in the repository")
    return resolved


def ensure_dir_exists(root: Path | str, relative: str) -> Path:
    resolved = resolve_inside(root, relative)
    if not resolved.is_dir():
        raise _invalid(f"{relative!r} is not a directory in the repository")
    return resolved


def relative_posix(root: Path | str, resolved: Path) -> str:
    """
    Express a resolved path as a repo-relative POSIX path.

    This — never the host absolute path — is what goes into a Dockerfile, so
    the instruction stays relative to the build context.
    """
    real_root = Path(root).resolve()
    relative = resolved.relative_to(real_root).as_posix()
    return relative or "."
