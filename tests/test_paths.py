"""Phase 4 acceptance: path containment, including symlink escapes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from builds.errors import BuildError, ErrorCode
from integrations.paths import (
    ensure_dir_exists,
    ensure_file_exists,
    relative_posix,
    resolve_inside,
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "app").mkdir(parents=True)
    (root / "requirements.txt").write_text("django\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("token\n", encoding="utf-8")
    return root


def symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks not permitted on this platform: {exc}")


# ---------------------------------------------------------------------------
# Accepted paths
# ---------------------------------------------------------------------------
def test_nested_path_resolves(repo: Path) -> None:
    assert resolve_inside(repo, "src/main.py") == (repo / "src" / "main.py").resolve()


def test_dot_resolves_to_the_root(repo: Path) -> None:
    assert resolve_inside(repo, ".") == repo.resolve()


def test_relative_posix_roundtrip(repo: Path) -> None:
    resolved = resolve_inside(repo, "src/app")

    assert relative_posix(repo, resolved) == "src/app"
    assert relative_posix(repo, resolve_inside(repo, ".")) == "."


# ---------------------------------------------------------------------------
# Rejected paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("/etc/passwd", id="absolute-posix"),
        pytest.param("C:/Windows/System32", id="windows-drive"),
        pytest.param("\\\\server\\share", id="unc"),
        pytest.param("src\\main.py", id="backslash"),
        pytest.param("../outside/secret.txt", id="traversal"),
        pytest.param("src/../../outside/secret.txt", id="traversal-nested"),
        pytest.param("..", id="traversal-bare"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("src/main.py\x00.txt", id="nul-byte"),
        pytest.param("src\nRUN echo pwned", id="newline"),
    ],
)
def test_rejected_paths(repo: Path, candidate: str) -> None:
    with pytest.raises(BuildError) as exc_info:
        resolve_inside(repo, candidate)

    assert exc_info.value.error_code == ErrorCode.INVALID_PATH
    assert exc_info.value.retryable is False


def test_non_string_is_rejected(repo: Path) -> None:
    with pytest.raises(BuildError) as exc_info:
        resolve_inside(repo, None)  # type: ignore[arg-type]

    assert exc_info.value.error_code == ErrorCode.INVALID_PATH


# ---------------------------------------------------------------------------
# Symlink escapes: the reason a textual check is not enough
# ---------------------------------------------------------------------------
def test_symlinked_file_pointing_outside_is_rejected(repo: Path, tmp_path: Path) -> None:
    symlink_or_skip(repo / "escape.txt", tmp_path / "outside" / "secret.txt")

    with pytest.raises(BuildError) as exc_info:
        resolve_inside(repo, "escape.txt")

    assert exc_info.value.error_code == ErrorCode.INVALID_PATH


def test_symlinked_directory_pointing_outside_is_rejected(repo: Path, tmp_path: Path) -> None:
    symlink_or_skip(repo / "escape", tmp_path / "outside", target_is_directory=True)

    with pytest.raises(BuildError) as exc_info:
        resolve_inside(repo, "escape/secret.txt")

    assert exc_info.value.error_code == ErrorCode.INVALID_PATH


def test_symlink_staying_inside_is_allowed(repo: Path) -> None:
    symlink_or_skip(repo / "alias.py", repo / "src" / "main.py")

    assert resolve_inside(repo, "alias.py") == (repo / "src" / "main.py").resolve()


# ---------------------------------------------------------------------------
# Existence helpers
# ---------------------------------------------------------------------------
def test_ensure_file_exists(repo: Path) -> None:
    assert ensure_file_exists(repo, "requirements.txt").is_file()

    with pytest.raises(BuildError) as missing:
        ensure_file_exists(repo, "nope.txt")
    assert missing.value.error_code == ErrorCode.INVALID_PATH

    with pytest.raises(BuildError) as wrong_type:
        ensure_file_exists(repo, "src")
    assert "not a file" in wrong_type.value.message


def test_ensure_dir_exists(repo: Path) -> None:
    assert ensure_dir_exists(repo, "src").is_dir()

    with pytest.raises(BuildError) as wrong_type:
        ensure_dir_exists(repo, "requirements.txt")
    assert "not a directory" in wrong_type.value.message


def test_missing_root_is_reported(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as exc_info:
        resolve_inside(tmp_path / "does-not-exist", "x")

    assert exc_info.value.error_code == ErrorCode.INVALID_PATH
