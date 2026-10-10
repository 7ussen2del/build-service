"""Phase 4 acceptance: git resolution, cloning, error classification."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from builds.errors import BuildError, ErrorCode
from integrations import git
from tests.conftest import commit_all, init_repo, requires_git, run_git

pytestmark = requires_git


# ---------------------------------------------------------------------------
# resolve_commit
# ---------------------------------------------------------------------------
def test_resolve_commit_returns_the_exact_sha(local_repo: Path, local_repo_head: str) -> None:
    sha = git.resolve_commit(local_repo.as_uri(), "main")

    assert sha == local_repo_head
    assert len(sha) == 40
    assert git.COMMIT_SHA_RE.match(sha)


def test_resolve_commit_missing_branch(local_repo: Path) -> None:
    with pytest.raises(BuildError) as exc_info:
        git.resolve_commit(local_repo.as_uri(), "no-such-branch")

    assert exc_info.value.error_code == ErrorCode.BRANCH_NOT_FOUND
    assert exc_info.value.retryable is False


def test_resolve_commit_missing_repository(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as exc_info:
        git.resolve_commit((tmp_path / "nothing-here").as_uri(), "main")

    assert exc_info.value.error_code == ErrorCode.REPO_NOT_FOUND


def test_resolve_commit_picks_the_requested_branch(local_repo: Path) -> None:
    run_git(["checkout", "-b", "feature/x"], cwd=local_repo)
    (local_repo / "extra.txt").write_text("more\n", encoding="utf-8")
    feature_head = commit_all(local_repo, "feature commit")
    run_git(["checkout", "main"], cwd=local_repo)

    assert git.resolve_commit(local_repo.as_uri(), "feature/x") == feature_head
    assert git.resolve_commit(local_repo.as_uri(), "main") != feature_head


# ---------------------------------------------------------------------------
# Input guards
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "git://github.com/a/b",
        "git@github.com:a/b.git",
        "ssh://git@github.com/a/b",
        "--upload-pack=touch /tmp/pwned",
        "https://exa mple.com/a/b",
        "",
    ],
)
def test_unsupported_urls_are_refused_before_git_runs(url: str) -> None:
    with patch.object(git, "_run_git") as runner:
        with pytest.raises(BuildError) as exc_info:
            git.resolve_commit(url, "main")

    assert exc_info.value.error_code == ErrorCode.REPO_NOT_FOUND
    runner.assert_not_called()


@pytest.mark.parametrize("branch", ["--upload-pack=x", "-x", "a..b", "has space", ""])
def test_unsafe_refs_are_refused_before_git_runs(branch: str) -> None:
    with patch.object(git, "_run_git") as runner:
        with pytest.raises(BuildError) as exc_info:
            git.resolve_commit("https://example.com/a/b", branch)

    assert exc_info.value.error_code == ErrorCode.BRANCH_NOT_FOUND
    runner.assert_not_called()


def test_file_urls_require_the_test_only_setting(local_repo: Path, settings) -> None:
    settings.GIT_ALLOW_FILE_URLS = False

    with pytest.raises(BuildError) as exc_info:
        git.resolve_commit(local_repo.as_uri(), "main")

    assert exc_info.value.error_code == ErrorCode.REPO_NOT_FOUND


# ---------------------------------------------------------------------------
# shallow_clone
# ---------------------------------------------------------------------------
def test_shallow_clone_checks_out_the_expected_commit(
    local_repo: Path, local_repo_head: str, tmp_path: Path
) -> None:
    destination = tmp_path / "workspace" / "repo"

    head = git.shallow_clone(
        local_repo.as_uri(), "main", destination, expected_sha=local_repo_head
    )

    assert head == local_repo_head
    assert (destination / "requirements.txt").is_file()
    assert (destination / "src" / "manage.py").is_file()


def test_shallow_clone_is_shallow(local_repo: Path, tmp_path: Path) -> None:
    (local_repo / "second.txt").write_text("2\n", encoding="utf-8")
    commit_all(local_repo, "second commit")
    destination = tmp_path / "workspace" / "repo"

    git.shallow_clone(local_repo.as_uri(), "main", destination)

    log = run_git(["rev-list", "--count", "HEAD"], cwd=destination).stdout.strip()
    assert log == "1"


def test_shallow_clone_detects_a_moved_branch(local_repo: Path, tmp_path: Path) -> None:
    stale_sha = "0" * 40

    with pytest.raises(BuildError) as exc_info:
        git.shallow_clone(
            local_repo.as_uri(), "main", tmp_path / "repo", expected_sha=stale_sha
        )

    assert exc_info.value.error_code == ErrorCode.COMMIT_MISMATCH
    assert exc_info.value.retryable is False


def test_shallow_clone_missing_branch(local_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(BuildError) as exc_info:
        git.shallow_clone(local_repo.as_uri(), "nope", tmp_path / "repo")

    assert exc_info.value.error_code in {
        ErrorCode.REPO_NOT_FOUND,
        ErrorCode.BRANCH_NOT_FOUND,
    }


# ---------------------------------------------------------------------------
# Error classification and safety of the subprocess call
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stderr", "expected", "retryable"),
    [
        (
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            ErrorCode.GIT_AUTH_FAILED,
            False,
        ),
        ("remote: Repository not found.", ErrorCode.REPO_NOT_FOUND, False),
        (
            "fatal: unable to access 'https://x/': Could not resolve host: x",
            ErrorCode.GIT_NETWORK_ERROR,
            True,
        ),
        (
            "fatal: unable to access 'https://x/': Failed to connect to x port 443",
            ErrorCode.GIT_NETWORK_ERROR,
            True,
        ),
        ("fatal: something nobody predicted", ErrorCode.REPO_NOT_FOUND, False),
    ],
)
def test_stderr_classification(stderr: str, expected: ErrorCode, retryable: bool) -> None:
    completed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr=stderr)

    with patch.object(git, "_run_git", return_value=completed):
        with pytest.raises(BuildError) as exc_info:
            git.resolve_commit("https://example.com/a/b", "main")

    assert exc_info.value.error_code == expected
    assert exc_info.value.retryable is retryable


def test_timeout_is_a_retryable_network_error() -> None:
    with patch(
        "integrations.git.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
    ):
        with pytest.raises(BuildError) as exc_info:
            git.resolve_commit("https://example.com/a/b", "main")

    assert exc_info.value.error_code == ErrorCode.GIT_NETWORK_ERROR
    assert exc_info.value.retryable is True


def test_git_is_invoked_without_a_shell_and_with_a_timeout(local_repo: Path) -> None:
    with patch("integrations.git.subprocess.run", wraps=subprocess.run) as runner:
        git.resolve_commit(local_repo.as_uri(), "main")

    kwargs = runner.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["timeout"] > 0
    assert isinstance(runner.call_args.args[0], list)


def test_git_environment_cannot_prompt_or_inherit_credentials(monkeypatch) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/leak")
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret")

    env = git._git_env()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GITHUB_TOKEN" not in env
    assert not any("secret" in value for value in env.values())


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------
def test_workspace_is_removed_on_success(settings, tmp_path: Path) -> None:
    settings.BUILD_WORKSPACE_ROOT = str(tmp_path / "builds")

    with git.build_workspace("abc-123") as repo:
        repo.mkdir(parents=True)
        (repo / "file.txt").write_text("x", encoding="utf-8")
        assert repo.exists()

    assert not (tmp_path / "builds" / "abc-123").exists()


def test_workspace_is_removed_on_failure(settings, tmp_path: Path) -> None:
    settings.BUILD_WORKSPACE_ROOT = str(tmp_path / "builds")

    with pytest.raises(RuntimeError):
        with git.build_workspace("abc-456") as repo:
            repo.mkdir(parents=True)
            raise RuntimeError("boom")

    assert not (tmp_path / "builds" / "abc-456").exists()


def test_workspace_removes_a_cloned_repository(
    settings, tmp_path: Path, local_repo: Path
) -> None:
    # git leaves read-only files in .git on Windows; cleanup must still work.
    settings.BUILD_WORKSPACE_ROOT = str(tmp_path / "builds")

    with git.build_workspace("abc-789") as repo:
        git.shallow_clone(local_repo.as_uri(), "main", repo)
        assert (repo / ".git").exists()

    assert not (tmp_path / "builds" / "abc-789").exists()
