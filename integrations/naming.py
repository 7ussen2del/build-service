"""Image repository and tag naming (README section 6).

Docker Hub allows only two path levels, so owner and app name are folded into the
repository name.
"""


def image_repository(namespace, platform, owner_id, name):
    return f"{namespace}/{platform}-{owner_id}-{name}"


def image_tag(commit_sha):
    return commit_sha[:7]
