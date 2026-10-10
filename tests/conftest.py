"""
Shared fixtures for the integration tests.

`local_repo` builds a throwaway git repository on disk. Tests clone it over a
``file://`` URL, which the test settings allow and production does not.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

GIT = shutil.which("git")

requires_git = pytest.mark.skipif(GIT is None, reason="git executable not available")


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git for *fixture setup* with a deterministic identity."""
    assert GIT is not None
    return subprocess.run(
        [
            GIT,
            "-c",
            "user.name=Build Service Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        run_git(["init", f"--initial-branch={branch}"], cwd=path)
    except subprocess.CalledProcessError:  # pragma: no cover - older git
        run_git(["init"], cwd=path)
        run_git(["checkout", "-b", branch], cwd=path)


def commit_all(path: Path, message: str = "initial commit") -> str:
    run_git(["add", "-A"], cwd=path)
    run_git(["commit", "-m", message], cwd=path)
    return run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


@pytest.fixture()
def local_repo(tmp_path: Path) -> Path:
    """A one-commit repository on branch 'main' with a django-ish layout."""
    repo = tmp_path / "origin"
    init_repo(repo)

    (repo / "requirements.txt").write_text("django==5.1\n", encoding="utf-8")
    source = repo / "src"
    source.mkdir()
    (source / "manage.py").write_text("print('hi')\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/bin/sh\npython src/manage.py runserver\n", encoding="utf-8")

    commit_all(repo)
    return repo


@pytest.fixture()
def local_repo_head(local_repo: Path) -> str:
    return run_git(["rev-parse", "HEAD"], cwd=local_repo).stdout.strip()
