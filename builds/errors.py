"""
Error taxonomy shared by the whole service.

Lives in ``builds`` (not ``integrations``) because both the Django layer and
the integration modules need it, and the dependency must point one way:
``integrations`` -> ``builds.errors``, never the reverse.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Every value that may ever land in ``Build.error_code``."""

    INVALID_PAYLOAD = "INVALID_PAYLOAD"

    REPO_NOT_FOUND = "REPO_NOT_FOUND"
    BRANCH_NOT_FOUND = "BRANCH_NOT_FOUND"
    GIT_AUTH_FAILED = "GIT_AUTH_FAILED"
    GIT_NETWORK_ERROR = "GIT_NETWORK_ERROR"
    #: Branch moved between `ls-remote` and `clone` (addition to the plan's
    #: table; see RUNNING.md). Non-retryable: the pipeline re-resolves on the
    #: next request rather than chasing a moving branch.
    COMMIT_MISMATCH = "COMMIT_MISMATCH"

    INVALID_PATH = "INVALID_PATH"

    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
    TEMPLATE_RENDER_FAILED = "TEMPLATE_RENDER_FAILED"

    DOCKER_BUILD_FAILED = "DOCKER_BUILD_FAILED"
    REGISTRY_AUTH_FAILED = "REGISTRY_AUTH_FAILED"
    REGISTRY_PUSH_FAILED = "REGISTRY_PUSH_FAILED"

    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    EXECUTOR_TIMEOUT = "EXECUTOR_TIMEOUT"
    WORKER_CRASHED = "WORKER_CRASHED"

    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Codes whose cause may clear on its own, retried a bounded number of times.
RETRYABLE_CODES = frozenset(
    {ErrorCode.GIT_NETWORK_ERROR, ErrorCode.EXECUTOR_UNAVAILABLE}
)

#: Codes the build engine (Jenkins) is allowed to report back on a callback.
#: Anything else in a callback body is rejected as a bad request.
CALLBACK_ERROR_CODES = frozenset(
    {
        ErrorCode.DOCKER_BUILD_FAILED,
        ErrorCode.REGISTRY_AUTH_FAILED,
        ErrorCode.REGISTRY_PUSH_FAILED,
        ErrorCode.EXECUTOR_TIMEOUT,
        ErrorCode.INTERNAL_ERROR,
    }
)


class BuildError(Exception):
    """A failure with a stable, reportable error code."""

    def __init__(
        self,
        error_code: ErrorCode | str,
        message: str = "",
        *,
        retryable: bool | None = None,
    ) -> None:
        self.error_code = ErrorCode(error_code)
        self.message = message or str(error_code)
        # Default retryability comes from the taxonomy; callers may override.
        self.retryable = (
            self.error_code in RETRYABLE_CODES if retryable is None else retryable
        )
        super().__init__(f"{self.error_code}: {self.message}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BuildError(error_code={self.error_code!s}, "
            f"message={self.message!r}, retryable={self.retryable})"
        )
