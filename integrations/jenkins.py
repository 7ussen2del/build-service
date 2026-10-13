"""Trigger the Jenkins ``build-image`` job (README section 6).

Registry credentials and the callback token live in Jenkins credentials; they are
never sent as job parameters or logged here.
"""
import requests


class JenkinsError(Exception):
    """Jenkins could not be reached, or refused to start the job."""


def trigger_build(
    *,
    base_url,
    user,
    token,
    timeout,
    build_id,
    repo_url,
    commit_sha,
    dockerfile_b64,
    image_repository,
    image_tag,
    callback_url,
):
    url = f"{base_url.rstrip('/')}/job/build-image/buildWithParameters"
    params = {
        "BUILD_ID": build_id,
        "REPO_URL": repo_url,
        "COMMIT_SHA": commit_sha,
        "DOCKERFILE_B64": dockerfile_b64,
        "IMAGE_REPOSITORY": image_repository,
        "IMAGE_TAG": image_tag,
        "CALLBACK_URL": callback_url,
    }
    try:
        response = requests.post(url, data=params, auth=(user, token), timeout=timeout)
    except requests.RequestException as exc:
        raise JenkinsError(f"could not reach Jenkins: {exc}") from exc
    if response.status_code >= 400:
        raise JenkinsError(f"Jenkins returned HTTP {response.status_code}")
