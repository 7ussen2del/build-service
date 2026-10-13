"""Phase 4 tests: Jenkins trigger (README section 6)."""
from unittest.mock import MagicMock

import pytest

from integrations import jenkins
from integrations.jenkins import JenkinsError


def _params(**overrides):
    params = dict(
        base_url="https://jenkins",
        user="SECRET-USER",
        token="SECRET-TOKEN",
        timeout=30,
        build_id="b1",
        repo_url="https://host/repo",
        commit_sha="a" * 40,
        dockerfile_b64="RlJPTSB4Cg==",
        image_repository="acme/autodeploy-17-mates",
        image_tag="a82f91c",
        callback_url="https://cb/internal/builds/b1/result/",
    )
    params.update(overrides)
    return params


def test_trigger_posts_six_params_with_credentials_only_in_auth(monkeypatch):
    captured = {}

    def fake_post(url, data=None, auth=None, timeout=None):
        captured.update(url=url, data=data, auth=auth, timeout=timeout)
        return MagicMock(status_code=201)

    monkeypatch.setattr(jenkins.requests, "post", fake_post)
    jenkins.trigger_build(**_params())

    assert captured["url"] == "https://jenkins/job/build-image/buildWithParameters"
    assert set(captured["data"]) == {
        "BUILD_ID",
        "REPO_URL",
        "COMMIT_SHA",
        "DOCKERFILE_B64",
        "IMAGE_REPOSITORY",
        "IMAGE_TAG",
        "CALLBACK_URL",
    }
    assert captured["auth"] == ("SECRET-USER", "SECRET-TOKEN")
    assert captured["timeout"] == 30
    for value in captured["data"].values():
        assert "SECRET-TOKEN" not in str(value)
        assert "SECRET-USER" not in str(value)


def test_trigger_raises_on_connection_failure(monkeypatch):
    def fake_post(*args, **kwargs):
        raise jenkins.requests.RequestException("boom")

    monkeypatch.setattr(jenkins.requests, "post", fake_post)
    with pytest.raises(JenkinsError):
        jenkins.trigger_build(**_params())


def test_trigger_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        jenkins.requests, "post", lambda *a, **k: MagicMock(status_code=500)
    )
    with pytest.raises(JenkinsError):
        jenkins.trigger_build(**_params())
