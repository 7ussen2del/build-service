"""
Pytest bootstrap.

Tests must never depend on a developer's local environment, so the test run
pins its own settings before Django is configured.
"""
from __future__ import annotations

import os

# A file-backed SQLite DB keeps `pytest` runnable without Postgres. Set
# DATABASE_URL explicitly (e.g. in CI) to test against Postgres instead.
os.environ.setdefault("DATABASE_URL", "sqlite:///test_db.sqlite3")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")
os.environ.setdefault("DOCKERHUB_NAMESPACE", "testns")
os.environ.setdefault("PLATFORM_NAME", "iiap")

# The git tests clone throwaway repositories from disk.
os.environ.setdefault("GIT_ALLOW_FILE_URLS", "1")
# Keep git operations snappy in tests; real builds use the 120s default.
os.environ.setdefault("GIT_TIMEOUT_SECONDS", "60")
# No sleeping between retries while testing retry behaviour.
os.environ.setdefault("BUILD_RETRY_BASE_DELAY_SECONDS", "0")
