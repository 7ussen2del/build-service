"""Phase 3 tests: git commit resolution, pinned clone, path containment (README section 5)."""
from unittest.mock import MagicMock

import pytest

from integrations import git
from integrations.git import GitError, PathError

SHA = "a" * 40
OTHER = "b" * 40


def test_parse_commit_matches_branch_ref():
    out = f"{OTHER}\trefs/heads/dev\n{SHA}\trefs/heads/main\n"
    assert git._parse_commit(out, "main") == SHA


def test_parse_commit_branch_not_found():
    with pytest.raises(GitError):
        git._parse_commit("", "main")


def test_resolve_commit_uses_ls_remote(monkeypatch):
    captured = {}

    def fake(args, timeout, cwd=None):
        captured["args"] = args
        return f"{SHA}\trefs/heads/main\n"

    monkeypatch.setattr(git, "_run_git", fake)
    assert git.resolve_commit("https://host/repo", "main", 30) == SHA
    assert captured["args"] == ["ls-remote", "https://host/repo", "main"]


def test_run_git_sets_no_prompt_timeout_and_no_shell(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"], calls["kwargs"] = cmd, kwargs
        result = MagicMock(returncode=0, stdout="ok", stderr="")
        return result

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    assert git._run_git(["ls-remote", "u", "b"], timeout=30) == "ok"
    assert calls["cmd"][0] == "git"
    assert calls["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls["kwargs"]["timeout"] == 30
    assert calls["kwargs"].get("shell", False) is False


def test_run_git_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        git.subprocess,
        "run",
        lambda cmd, **k: MagicMock(returncode=128, stdout="", stderr="fatal: nope"),
    )
    with pytest.raises(GitError):
        git._run_git(["ls-remote", "u", "b"], timeout=30)


def test_run_git_raises_on_timeout(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise git.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(git.subprocess, "run", fake_run)
    with pytest.raises(GitError):
        git._run_git(["ls-remote", "u", "b"], timeout=30)


def test_shallow_clone_pins_to_sha(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        git, "_run_git", lambda args, timeout, cwd=None: calls.append(args) or ""
    )
    git.shallow_clone("https://host/repo", SHA, str(tmp_path / "repo"), 30)
    flat = [" ".join(a) for a in calls]
    assert any(f"fetch --depth 1 origin {SHA}" in f for f in flat)
    assert any("checkout" in f and SHA in f for f in flat)


def test_resolve_in_repo_ok(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    assert git.resolve_in_repo(tmp_path, "src") == (tmp_path / "src").resolve()
    assert (
        git.resolve_in_repo(tmp_path, "src/app.py")
        == (tmp_path / "src" / "app.py").resolve()
    )


def test_resolve_in_repo_missing(tmp_path):
    with pytest.raises(PathError):
        git.resolve_in_repo(tmp_path, "nope.txt")


def test_resolve_in_repo_dotdot_escape(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "secret").write_text("s")
    with pytest.raises(PathError):
        git.resolve_in_repo(tmp_path / "repo", "../secret")


def test_resolve_in_repo_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    link = repo / "link"
    try:
        link.symlink_to(outside / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    with pytest.raises(PathError):
        git.resolve_in_repo(repo, "link")
