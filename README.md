# Build Service

A Django-based internal microservice that turns application source code into a deployable Docker image. It receives build requests asynchronously from the Application Service, orchestrates the full pipeline — commit resolution, Dockerfile rendering, Jenkins build, image push — and reports results back to downstream services.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture Overview](#2-architecture-overview)
3. [Key Features](#3-key-features)
4. [Technologies & Tools](#4-technologies--tools)
5. [Project Structure](#5-project-structure)
6. [Core Components](#6-core-components)
7. [Build Lifecycle](#7-build-lifecycle)
8. [Templates System](#8-templates-system)
9. [API Documentation](#9-api-documentation)
10. [Celery & Background Tasks](#10-celery--background-tasks)
11. [Database Design](#11-database-design)
12. [Environment Variables](#12-environment-variables)
13. [Installation & Setup](#13-installation--setup)
14. [Running the Project](#14-running-the-project)
15. [Testing](#15-testing)
16. [Jenkins Integration](#16-jenkins-integration)
17. [Error Handling](#17-error-handling)
18. [Security](#18-security)
19. [Current Limitations](#19-current-limitations)

---

## 1. Project Overview

The Build Service is one of four microservices in an internal Platform-as-a-Service (PaaS) deployment platform alongside Auth, Application, and Deployment services.

**Single responsibility:** convert source code into an immutable container image in a registry, then notify the appropriate downstream services of the outcome.

### What it does

1. Receives a build request from the Application Service as a Celery task on the `build` queue.
2. Validates the request payload and records a `Build` row in its own PostgreSQL database.
3. Resolves the exact commit SHA of the target branch.
4. Clones the repository at that commit, validates that declared paths exist.
5. Renders a Dockerfile from a Jinja2 template appropriate for the application type.
6. Triggers a Jenkins pipeline to build the Docker image and push it to DockerHub.
7. Receives the result via a secure internal HTTP callback.
8. Notifies the Application Service of every result, and the Deployment Service on success only.

### What it does not do

- It does not expose public user-facing APIs (no JWT authentication is implemented).
- It does not perform auth, application CRUD, Kubernetes management, or gateway routing.
- It never reads or writes another service's database; `application_id` and `owner_id` are plain integers.

---

## 2. Architecture Overview

```
Application Service
    │
    │  Celery: send_task("builds.start_build", payload)
    │  Queue: "build"
    ▼
┌─────────────────────────────────────────────────────────────┐
│                      Build Service                          │
│                                                             │
│  [Celery Worker]                                            │
│       │  start_build task                                   │
│       │  → Validate payload                                 │
│       │  → Create Build(QUEUED) in PostgreSQL               │
│       │  → Dispatch run_build_pipeline.delay()              │
│       │                                                     │
│       │  run_build_pipeline task                            │
│       │  → Transition Build: QUEUED → BUILDING              │
│       │  → git ls-remote  (resolve commit SHA)              │
│       │  → git shallow clone + path validation              │
│       │  → Jinja2 Dockerfile rendering                      │
│       │  → Encode Dockerfile as base64                      │
│       │  → POST to Jenkins /job/build-image/buildWithParams │
│       │  → Workspace cleanup                                │
│       ▼                                                     │
│  [Jenkins Pipeline]                                         │
│       │  → git checkout pinned commit                       │
│       │  → Decode and write Dockerfile                      │
│       │  → docker build                                     │
│       │  → docker push → DockerHub                          │
│       │  → Inspect image digest                             │
│       │  → POST /internal/builds/{id}/result/ (callback)   │
│       ▼                                                     │
│  [Django REST endpoint]                                     │
│       │  POST /internal/builds/{id}/result/                 │
│       │  → Authenticate Bearer token                        │
│       │  → Transition Build: BUILDING → SUCCESS/FAILED      │
│       │  → Dispatch notify_application.delay()              │
│       │  → Dispatch notify_deployment.delay() (on success)  │
│       ▼                                                     │
│  [Celery Notification Tasks]                                │
│       → send_task("applications.build_result", ...)         │
│       → send_task("deployments.deploy_image", ...)          │
└─────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
 Application Service           Deployment Service
```

### Infrastructure components

| Component | Technology | Purpose |
|---|---|---|
| Web server | Django 5.1 + Gunicorn (dev: `runserver`) | Hosts the internal callback endpoint |
| Task broker | Redis 7 | Celery message transport |
| Database | PostgreSQL 16 | Owns the `builds_build` table |
| Task executor | Celery 5.4 | Async build intake and notifications |
| Build engine | Jenkins (Declarative Pipeline) | Runs `docker build` + `docker push` |
| Container registry | DockerHub | Stores built images |

---

## 3. Key Features

### Asynchronous build intake
Build requests arrive as Celery tasks named `builds.start_build` on the `build` queue. The intake task returns quickly; long-running work is handed to a separate pipeline task.

### Two-level payload validation
- **Envelope** (`request_id`, `application_id`, `owner_id`): if any are missing or malformed, the message is discarded with no database record created.
- **Build fields** (`repository_url`, `branch`, `name`, `port`, `paths`, `run_command`): if invalid, a `Build` row is created immediately with `status=FAILED` and `error_code=INVALID_PAYLOAD`, allowing the Application Service to be notified.

### Idempotent request handling
`request_id` is the idempotency key. If the same `request_id` is received twice (e.g., Celery redelivery), the second delivery is detected and silently dropped — no duplicate build is created or dispatched.

### Enforced state machine
Build status transitions are controlled by a single `transition()` service function that uses `SELECT FOR UPDATE` row-level locking. Invalid transitions (e.g., jumping from `QUEUED` directly to `SUCCESS`) raise `InvalidTransition`. Terminal states (`SUCCESS`, `FAILED`) are immutable.

### Git commit pinning
The service resolves the exact 40-character commit SHA via `git ls-remote` before cloning. The clone is pinned to that SHA with `--depth 1`, ensuring the build is reproducible and not affected by branch movement during execution.

### Secure path validation
Declared `source_directory` and `requirements_file` paths are validated using `Path.resolve()`, which follows symlinks. Any path that escapes the repository root — including `..` traversal and symlinks pointing outside — raises a `PathError` and fails the build.

### Jinja2 Dockerfile rendering with injection protection
Dockerfiles are rendered from sandboxed Jinja2 templates (`SandboxedEnvironment`, `StrictUndefined`). The `run_command` field is always converted to a JSON exec-form array via `shlex.split()` + `json.dumps()`, which prevents shell injection regardless of the value provided.

### Jenkins orchestration
The service triggers a Jenkins declarative pipeline by POSTing parameters to `/job/build-image/buildWithParameters`. The rendered Dockerfile is transmitted as a base64-encoded parameter. Jenkins credentials (registry token, callback token) are stored in Jenkins itself and never passed as job parameters.

### Secure internal callback
Jenkins reports the build result to `POST /internal/builds/{build_id}/result/`. The endpoint requires a `Bearer` token compared with constant-time comparison (`django.utils.crypto.constant_time_compare`) to prevent timing attacks. Already-finished builds are treated as a 200 no-op.

### Downstream notifications with retry
Two Celery tasks notify other services after a result is recorded:
- `notify_application`: always dispatched (success or failure) → sends `applications.build_result`
- `notify_deployment`: dispatched on success only → sends `deployments.deploy_image`

Both tasks use `autoretry_for=(Exception,)` with exponential backoff (`retry_backoff=True`, `max_retries=5`, `retry_jitter=True`), bounded at 600 seconds maximum delay.

### Image naming convention
Images are named to satisfy DockerHub's two-path-level constraint:
```
{DOCKERHUB_NAMESPACE}/{PLATFORM_NAME}-{owner_id}-{app_name}:{commit_sha[:7]}
```
Example: `acme/autodeploy-17-mates:a82f91c`

### Django Admin
The `Build` model is registered in Django Admin with list display, filters, and read-only protection on immutable fields.

---

## 4. Technologies & Tools

### Application stack

| Technology | Version | Role |
|---|---|---|
| Python | 3.12 | Runtime |
| Django | 5.1.4 | Web framework, ORM, admin |
| Django REST Framework | 3.15.2 | API serializers and views |
| Celery | 5.4.0 | Async task execution |
| Redis (client) | 5.2.1 | Celery broker client |
| psycopg | 3.2.3 (`binary`) | PostgreSQL adapter |
| dj-database-url | 2.3.0 | Database URL parsing |
| Jinja2 | 3.1.4 | Dockerfile template rendering |
| requests | 2.32.3 | Jenkins HTTP API calls |
| WhiteNoise | 6.9.0 | Static file serving |

### Testing

| Technology | Version | Role |
|---|---|---|
| pytest | 8.3.4 | Test runner |
| pytest-django | 4.9.0 | Django integration for pytest |

### Infrastructure

| Component | Version | Role |
|---|---|---|
| PostgreSQL | 16 | Primary database |
| Redis | 7 | Celery broker |
| Docker | — | Container runtime |
| Docker Compose | — | Local orchestration |
| Jenkins | LTS | Build engine |
| DockerHub | — | Container registry |

---

## 5. Project Structure

```
Build-Service-recovered/
│
├── config/                        # Django project package
│   ├── __init__.py
│   ├── celery.py                  # Celery application definition
│   ├── settings.py                # All settings sourced from environment variables
│   ├── urls.py                    # Root URL config (admin + builds)
│   └── wsgi.py                    # WSGI entry point
│
├── builds/                        # Core Django application
│   ├── __init__.py
│   ├── admin.py                   # Django Admin registration for Build model
│   ├── apps.py                    # App config
│   ├── models.py                  # Build model, BuildStatus, ApplicationType enums
│   ├── serializers.py             # BuildRequestSerializer with all validation rules
│   ├── services.py                # State machine (transition), pipeline orchestration (run_pipeline)
│   ├── tasks.py                   # Celery tasks: start_build, run_build_pipeline, notify_*
│   ├── templating.py              # Jinja2 rendering engine and TemplateError
│   ├── urls.py                    # URL: /internal/builds/<uuid>/result/
│   ├── views.py                   # build_result view (callback endpoint)
│   ├── migrations/
│   │   └── 0001_initial.py        # Initial database schema migration
│   └── tests/
│       ├── __init__.py
│       ├── test_admin.py          # Admin registration and access control tests
│       ├── test_git.py            # Git integration unit tests
│       ├── test_intake.py         # Intake task, validation, idempotency tests
│       ├── test_jenkins.py        # Jenkins trigger unit tests
│       ├── test_naming.py         # Image naming tests
│       ├── test_notifications.py  # Notification payload and dispatch tests
│       ├── test_pipeline.py       # Pipeline orchestration integration tests
│       ├── test_result.py         # Callback endpoint tests
│       ├── test_state_machine.py  # State machine transition tests
│       └── test_templating.py     # Dockerfile rendering and injection tests
│
├── integrations/                  # External service clients
│   ├── __init__.py
│   ├── git.py                     # Git operations: resolve_commit, shallow_clone, resolve_in_repo
│   ├── jenkins.py                 # Jenkins HTTP trigger: trigger_build()
│   └── naming.py                  # Image naming: image_repository(), image_tag()
│
├── templates/                     # Jinja2 Dockerfile templates (one per app type)
│   ├── django/
│   │   └── Dockerfile.j2
│   ├── flask/
│   │   └── Dockerfile.j2
│   ├── node/
│   │   └── Dockerfile.j2
│   └── react/
│       └── Dockerfile.j2
│
├── jenkins/
│   ├── Dockerfile                 # Jenkins controller image
│   └── Jenkinsfile                # Declarative pipeline: checkout, build, push, callback
│
├── staticfiles/                   # Collected static files (WhiteNoise)
│
├── manage.py                      # Django management CLI
├── Dockerfile                     # Build Service application image (python:3.12-slim)
├── docker-compose.yml             # Full local stack definition
├── requirements.txt               # Python dependencies
├── pytest.ini                     # pytest configuration
├── .env.example                   # Environment variable template
├── .gitignore
└── db.sqlite3                     # SQLite fallback (dev only, not used in Docker)
```

---

## 6. Core Components

### `builds/models.py` — Data Model

Defines the `BuildStatus` and `ApplicationType` enums, and the `Build` model (the only table this service owns).

**`BuildStatus`** (TextChoices):
- `QUEUED` — received and waiting to run
- `BUILDING` — pipeline is executing (cloning, rendering, Jenkins triggered)
- `SUCCESS` — image built and pushed, digest stored
- `FAILED` — terminal failure at any stage

**`ApplicationType`** (TextChoices):
- `django`, `flask`, `node`, `react`

---

### `builds/serializers.py` — Input Validation

`BuildRequestSerializer` validates all build request fields beyond the envelope. Each field has specific rules:

| Field | Validation rules |
|---|---|
| `name` | Lowercase letters, digits, and `-` only (`^[a-z0-9-]+$`) |
| `repository_url` | `https` scheme only; non-empty hostname; no `user:pass@` credentials |
| `branch` | Letters, digits, `/._-`; must not start with `-`; must not contain `..` |
| `application_type` | Must be one of `django`, `flask`, `node`, `react` |
| `source_directory` | Relative path; no leading `/`; no `..` segments |
| `requirements_file` | Optional; same relative-path rules as above when provided |
| `port` | Integer between 1 and 65535 (optional) |
| `run_command` | Non-empty; must not contain NUL (`\x00`), `\n`, or `\r` |

---

### `builds/services.py` — State Machine & Pipeline

#### `transition(build_id, to_status, **fields)`

The single gateway for all `Build` status changes.

```python
# Allowed transitions (enforced strictly)
_ALLOWED_TRANSITIONS = {
    BuildStatus.QUEUED:    {BuildStatus.BUILDING, BuildStatus.FAILED},
    BuildStatus.BUILDING:  {BuildStatus.SUCCESS,  BuildStatus.FAILED},
    BuildStatus.SUCCESS:   set(),   # terminal
    BuildStatus.FAILED:    set(),   # terminal
}

# Fields that may be set alongside a status change
_SETTABLE_FIELDS = {
    "commit_sha", "image_repository", "image_tag",
    "image_digest", "error_code", "error_message",
}
```

Behaviour:
- Runs inside `transaction.atomic()` with `select_for_update()` — concurrent workers serialise on the row lock.
- Sets `started_at` automatically on transition to `BUILDING`.
- Sets `finished_at` automatically on transition to `SUCCESS` or `FAILED`.
- Raises `InvalidTransition` for disallowed moves.
- Raises `ValueError` for unrecognised field names.

#### `run_pipeline(build_id)`

Orchestrates the full build pipeline synchronously (called from a Celery task):

1. Attempts `QUEUED → BUILDING` transition — exits silently if already past `QUEUED`.
2. Resolves the commit SHA via `git ls-remote`.
3. Computes `image_repository` and `image_tag`; updates the `Build` row.
4. Creates a temporary workspace and performs a shallow clone.
5. Validates `source_directory` and `requirements_file` paths inside the clone.
6. Renders the Dockerfile for the application type.
7. Base64-encodes the Dockerfile and constructs the callback URL.
8. Triggers the Jenkins `build-image` job via HTTP.
9. Deletes the workspace in a `finally` block (always cleaned up).
10. On any failure, calls `_fail()` to transition to `FAILED` with the appropriate error code.

---

### `builds/tasks.py` — Celery Tasks

#### `start_build(payload)` — `@shared_task(name="builds.start_build")`

Entry point from the Application Service.

```python
# Called by the Application Service as:
app.send_task("builds.start_build", args=[payload], queue="build")
```

Flow:
1. Validates envelope fields; discards the message (returns `None`) if invalid.
2. Checks `Build.objects.filter(request_id=...).exists()` for idempotency.
3. Validates full payload with `BuildRequestSerializer`.
4. Creates `Build(QUEUED)` on valid payload, or `Build(FAILED, error_code=INVALID_PAYLOAD)` on invalid.
5. Dispatches `run_build_pipeline.delay(str(build.build_id))`.

#### `run_build_pipeline(build_id)` — `@shared_task(name="builds.run_build_pipeline")`

Delegates to `services.run_pipeline(build_id)`.

#### `notify_application(build_id)` — `@shared_task(name="builds.notify_application")`

Sends the build result to the Application Service:
```python
current_app.send_task("applications.build_result", kwargs=payload)
```

Payload on success:
```json
{
  "build_id": "uuid",
  "application_id": 42,
  "status": "success",
  "image_repository": "acme/autodeploy-17-mates",
  "image_tag": "a82f91c",
  "image_digest": "sha256:<64 hex chars>",
  "commit_sha": "<40 hex chars>"
}
```

Payload on failure (adds `error_code` and `error_message`; image fields are `null`):
```json
{
  "build_id": "uuid",
  "application_id": 42,
  "status": "failed",
  "image_repository": null,
  "image_tag": null,
  "image_digest": null,
  "commit_sha": null,
  "error_code": "GIT_ERROR",
  "error_message": "branch 'missing-branch' not found"
}
```

#### `notify_deployment(build_id)` — `@shared_task(name="builds.notify_deployment")`

Sends deployment data to the Deployment Service on success only:
```python
current_app.send_task("deployments.deploy_image", kwargs=payload)
```

Payload:
```json
{
  "request_id": "<fresh uuid per attempt>",
  "build_id": "<stable build uuid>",
  "application_id": 42,
  "owner_id": 17,
  "image_repository": "acme/autodeploy-17-mates",
  "image_tag": "a82f91c",
  "image_digest": "sha256:<64 hex chars>",
  "commit_sha": "<40 hex chars>",
  "application_type": "django",
  "port": 8000
}
```

> **Note:** `request_id` is regenerated on every delivery attempt. `build_id` is constant and serves as the Deployment Service's deduplication key.

**Retry configuration** (applies to both notification tasks):
```python
autoretry_for=(Exception,)
retry_backoff=True
retry_backoff_max=600  # max 10-minute delay between retries
max_retries=5
retry_jitter=True
```

---

### `builds/templating.py` — Dockerfile Rendering

Provides `render_dockerfile(application_type, *, source_directory, run_command, port, requirements_file)`.

Base images per type:
```python
BASE_IMAGES = {
    "django": "python:3.12-slim",
    "flask":  "python:3.12-slim",
    "node":   "node:20-slim",
    "react":  "node:20-slim",
}
```

The rendering environment:
```python
SandboxedEnvironment(
    loader=FileSystemLoader(<BASE_DIR>/templates),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)
```

`run_command` is always converted to a JSON exec array before being injected into the template:
```python
command = json.dumps(shlex.split(run_command))
# "bash ./run.sh"  →  '["bash", "./run.sh"]'
# "; rm -rf /"     →  '[";", "rm", "-rf", "/"]'
# "$(reboot)"      →  '["$(reboot)"]'
```

---

### `integrations/git.py` — Git Operations

All git commands use `subprocess.run([...], shell=False)` with `GIT_TERMINAL_PROMPT=0` in the environment and an explicit timeout. No credentials are ever passed.

| Function | Description |
|---|---|
| `resolve_commit(repo_url, branch, timeout)` | Runs `git ls-remote` and parses the SHA for the target branch |
| `shallow_clone(repo_url, commit_sha, dest, timeout)` | Initialises a repo, adds the remote, fetches `--depth 1` by SHA, checks out |
| `resolve_in_repo(repo_root, relative_path)` | Uses `Path.resolve()` to follow symlinks; rejects paths outside repo root or non-existent paths |
| `_run_git(args, timeout, cwd)` | Internal runner; raises `GitError` on non-zero exit or timeout |

Exceptions:
- `GitError` — repo/branch not found, network/timeout issue, git command failure
- `PathError` — path escapes repository or does not exist

---

### `integrations/jenkins.py` — Jenkins Trigger

Provides `trigger_build(*, base_url, user, token, timeout, build_id, repo_url, commit_sha, dockerfile_b64, image_repository, image_tag, callback_url)`.

- Posts to `{base_url}/job/build-image/buildWithParameters`.
- Passes credentials only via HTTP Basic Auth (`auth=(user, token)`), never in the POST body.
- Raises `JenkinsError` on connection failure or HTTP 4xx/5xx response.

---

### `integrations/naming.py` — Image Naming

```python
def image_repository(namespace, platform, owner_id, name):
    return f"{namespace}/{platform}-{owner_id}-{name}"
    # e.g. "acme/autodeploy-17-mates"

def image_tag(commit_sha):
    return commit_sha[:7]
    # e.g. "a82f91c"
```

---

### `builds/views.py` — Callback Endpoint

`build_result(request, build_id)` — `POST /internal/builds/<uuid>/result/`

Authentication:
```python
header = request.META.get("HTTP_AUTHORIZATION", "")
# Must be: "Bearer <INTERNAL_SERVICE_TOKEN>"
# Compared with: constant_time_compare(header[7:], settings.INTERNAL_SERVICE_TOKEN)
```

Logic:
- Returns `401` if the `Authorization` header is absent or malformed.
- Returns `403` if the token value is wrong.
- Returns `400` if `status` is not `success` or `failed`.
- Returns `400` if `status=success` but `image_digest` does not match `^sha256:[a-f0-9]{64}$`.
- Returns `404` if the `build_id` is unknown.
- Returns `200` with `{"detail": "Already final; no change."}` if the build is already in a terminal state (idempotency).
- Returns `409` if `transition()` raises `InvalidTransition` (concurrent result delivery).
- On success: transitions to `SUCCESS`, stores digest, dispatches both notification tasks.
- On failure: transitions to `FAILED`, stores error info, dispatches only `notify_application`.

---

## 7. Build Lifecycle

```
Application Service publishes:
  send_task("builds.start_build", args=[payload], queue="build")

Step 1 — Intake (start_build task)
  ├── Validate envelope (request_id, application_id, owner_id)
  │   └── Bad envelope → discard message, no DB record
  ├── Check request_id uniqueness
  │   └── Duplicate → return None (idempotent no-op)
  ├── Validate all build fields (BuildRequestSerializer)
  │   ├── Invalid → Create Build(FAILED, error_code=INVALID_PAYLOAD)
  │   │             → Dispatch notify_application
  │   └── Valid   → Create Build(QUEUED)
  │                 → Dispatch run_build_pipeline.delay(build_id)
  └── Task returns

Step 2 — Pipeline (run_build_pipeline task → services.run_pipeline)
  ├── Transition: QUEUED → BUILDING
  │   └── If not QUEUED (concurrent dispatch) → exit silently
  ├── git ls-remote <repo_url> <branch>  →  commit_sha (40 chars)
  ├── Compute image_repository and image_tag
  │   ├── image_repository = "{namespace}/{platform}-{owner_id}-{name}"
  │   └── image_tag        = commit_sha[:7]
  ├── UPDATE build SET commit_sha, image_repository, image_tag
  ├── tempfile.mkdtemp() → workspace
  ├── git shallow clone (pinned to commit_sha)
  ├── Validate source_directory exists in clone (Path.resolve)
  ├── Validate requirements_file exists in clone (if provided)
  ├── Render Dockerfile via Jinja2 template for application_type
  ├── base64.b64encode(dockerfile.encode())
  ├── Construct callback_url = {BUILD_CALLBACK_BASE_URL}/internal/builds/{build_id}/result/
  ├── POST to Jenkins /job/build-image/buildWithParameters
  │   ├── On connection/HTTP error → FAILED (error_code=EXECUTOR_UNAVAILABLE)
  │   └── On success  → Jenkins job queued
  └── shutil.rmtree(workspace)   ← always, in finally block

Step 3 — Jenkins Pipeline (Jenkinsfile)
  ├── Stage: Checkout commit
  │   └── git init, remote add, fetch --depth 1 <SHA>, checkout <SHA>
  ├── Stage: Write Dockerfile
  │   └── echo "$DOCKERFILE_B64" | base64 -d > Dockerfile
  ├── Stage: Build
  │   └── docker build -t {IMAGE_REPOSITORY}:{IMAGE_TAG} -f Dockerfile .
  ├── Stage: Push
  │   ├── docker login (from Jenkins credentials: "dockerhub")
  │   └── docker push {IMAGE_REPOSITORY}:{IMAGE_TAG}
  ├── Stage: Report success
  │   ├── docker inspect → image digest
  │   └── POST /internal/builds/{BUILD_ID}/result/
  │         body: {"status":"success","image_digest":"sha256:..."}
  │         header: Authorization: Bearer {CALLBACK_TOKEN}
  └── post.failure:
        POST /internal/builds/{BUILD_ID}/result/
          body: {"status":"failed","error_code":"BUILD_FAILED","error_message":"..."}

Step 4 — Result callback (build_result view)
  ├── Authenticate Bearer token
  ├── Validate digest format (success) or error info (failure)
  ├── Transition: BUILDING → SUCCESS (store image_digest)
  │          or: BUILDING → FAILED  (store error_code, error_message)
  ├── Dispatch notify_application.delay(build_id)   ← always
  └── Dispatch notify_deployment.delay(build_id)    ← success only

Step 5 — Notifications
  ├── notify_application → send_task("applications.build_result", ...)
  └── notify_deployment  → send_task("deployments.deploy_image", ...)
       (with autoretry, exponential backoff, max 5 attempts)
```

---

## 8. Templates System

Jinja2 Dockerfile templates live in `templates/<application_type>/Dockerfile.j2`. The rendering engine (`builds/templating.py`) selects the template by `application_type` and passes a context dictionary.

### Template context variables

| Variable | Type | Source |
|---|---|---|
| `base_image` | string | Hardcoded per type in `BASE_IMAGES` dict |
| `source_directory` | string | From build request |
| `requirements_file` | string or `None` | From build request |
| `port` | int or `None` | From build request |
| `run_command` | string (JSON array) | Derived: `json.dumps(shlex.split(original))` |

### Django template (`templates/django/Dockerfile.j2`)

```dockerfile
FROM {{ base_image }}
WORKDIR /app
COPY {{ requirements_file }} .
RUN pip install --no-cache-dir -r requirements.txt
COPY {{ source_directory }} .
EXPOSE {{ port }}
CMD {{ run_command }}
```

Rendered example for `run_command="bash ./run.sh"`, `port=8000`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src .
EXPOSE 8000
CMD ["bash", "./run.sh"]
```

### Flask template (`templates/flask/Dockerfile.j2`)

Identical structure to the Django template, using the same `python:3.12-slim` base image.

### Node.js template (`templates/node/Dockerfile.j2`)

```dockerfile
FROM {{ base_image }}
WORKDIR /app
COPY {{ source_directory }} .
RUN npm install
EXPOSE {{ port }}
CMD {{ run_command }}
```

Base image: `node:20-slim`

### React template (`templates/react/Dockerfile.j2`)

```dockerfile
FROM {{ base_image }}
WORKDIR /app
COPY {{ source_directory }} .
RUN npm install
RUN npm run build
EXPOSE {{ port }}
CMD {{ run_command }}
```

Base image: `node:20-slim` (single-stage build)

### Injection safety

The `run_command` field is always parsed through `shlex.split()` and re-serialised as a JSON array before template rendering. This renders all shell metacharacters inert:

| `run_command` input | Rendered `CMD` |
|---|---|
| `bash ./run.sh` | `CMD ["bash", "./run.sh"]` |
| `; rm -rf /` | `CMD [";", "rm", "-rf", "/"]` |
| `$(reboot)` | `CMD ["$(reboot)"]` |
| `echo "unterminated` | Raises `TemplateError` (shlex parse error) |

---

## 9. API Documentation

The Build Service exposes **one** HTTP endpoint. There are no public user-facing endpoints.

---

### `POST /internal/builds/{build_id}/result/`

Records the outcome of a Jenkins build. This endpoint is for internal use only and must not be exposed through the public API gateway.

**URL parameters:**

| Parameter | Type | Description |
|---|---|---|
| `build_id` | UUID | The `build_id` of the build to update |

**Authentication:**

```
Authorization: Bearer <INTERNAL_SERVICE_TOKEN>
```

The token is compared with constant-time comparison. Missing or malformed headers return `401`. An incorrect token value returns `403`.

**Request body — success:**

```json
{
  "status": "success",
  "image_digest": "sha256:a3f1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
}
```

**Request body — failure:**

```json
{
  "status": "failed",
  "error_code": "BUILD_FAILED",
  "error_message": "docker build exited with code 1"
}
```

**Response codes:**

| Code | Condition |
|---|---|
| `200` | Result recorded successfully |
| `200` | Build already in terminal state (no-op, idempotent) |
| `400` | `status` is not `success` or `failed` |
| `400` | `status=success` but `image_digest` is missing or does not match `^sha256:[a-f0-9]{64}$` |
| `401` | `Authorization` header missing or not `Bearer <token>` format |
| `403` | Token value is incorrect |
| `404` | `build_id` not found |
| `409` | Concurrent result delivery conflict (`InvalidTransition`) |

**Success response body:**
```json
{"detail": "Recorded."}
```

**Already-final response body:**
```json
{"detail": "Already final; no change."}
```

---

### Django Admin

Available at `/admin/` (requires superuser login).

The `Build` model is registered with:
- **List display:** `build_id`, `name`, `status`, `application_type`, `branch`, `created_at`
- **Filters:** `status`, `application_type`
- **Search:** `build_id`, `name`, `repository_url`
- **Read-only fields:** `build_id`, `request_id`, `commit_sha`, `image_repository`, `image_tag`, `image_digest`, `error_code`, `error_message`, `started_at`, `finished_at`, `created_at`

---

## 10. Celery & Background Tasks

### Celery application (`config/celery.py`)

```python
app = Celery("build_service")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

Tasks are auto-discovered from all installed apps.

### Queue configuration (`config/settings.py`)

```python
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TASK_DEFAULT_QUEUE = "build"
```

All tasks run on the `build` queue by default. There is no queue-level routing between intake and pipeline tasks.

### Task registry

| Task name | Registered as | Triggered by | Queue |
|---|---|---|---|
| `builds.start_build` | `@shared_task` | Application Service via `send_task` | `build` |
| `builds.run_build_pipeline` | `@shared_task` | `start_build` via `.delay()` | `build` (default) |
| `builds.notify_application` | `@shared_task` | `build_result` view via `.delay()` | `build` (default) |
| `builds.notify_deployment` | `@shared_task` | `build_result` view via `.delay()` | `build` (default) |

### Starting the Celery worker

```bash
celery -A config worker -Q build -l info
```

As defined in `docker-compose.yml`:
```yaml
celery:
  build: .
  command: celery -A config worker -Q build -l info
  env_file: .env
  depends_on:
    - postgres
    - redis
```

### Notification retry policy

```python
_NOTIFY_OPTS = dict(
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
    retry_jitter=True,
)
```

This applies to both `notify_application` and `notify_deployment`. Failed notifications are retried with exponential backoff up to a maximum of 5 attempts, with jitter to avoid thundering-herd. The build's own status is never modified by notification retries or failures.

---

## 11. Database Design

The Build Service owns one table: `builds_build`. It never reads or writes other services' databases.

### `Build` model

```
builds_build
├── id                 BigAutoField (PK, auto-increment)
├── build_id           UUID (unique, non-editable, default=uuid4)   ← public stable identifier
├── request_id         UUID (unique)                                 ← idempotency key
├── application_id     BigInteger (db_index)                        ← not a ForeignKey
├── owner_id           BigInteger (db_index)                        ← not a ForeignKey
├── status             CharField(20, choices=BuildStatus, default=QUEUED)
├── name               CharField(100)
├── repository_url     URLField(500)
├── branch             CharField(200)
├── application_type   CharField(20, choices=ApplicationType)
├── source_directory   CharField(500)
├── requirements_file  CharField(500, null, blank)
├── port               IntegerField(null, blank)
├── run_command        CharField(1000)
├── commit_sha         CharField(40, null, blank)                   ← set once on pipeline start
├── image_repository   CharField(255, null, blank)                  ← set once on pipeline start
├── image_tag          CharField(100, null, blank)                  ← set once on pipeline start
├── image_digest       CharField(100, null, blank)                  ← set on SUCCESS by Jenkins callback
├── error_code         CharField(50, null, blank)                   ← set on FAILED
├── error_message      TextField(null, blank)                       ← set on FAILED
├── started_at         DateTimeField(null, blank)                   ← set on BUILDING transition
├── finished_at        DateTimeField(null, blank)                   ← set on SUCCESS/FAILED transition
└── created_at         DateTimeField(auto_now_add)
```

### Key constraints

| Constraint | Column(s) | Enforcement |
|---|---|---|
| Primary key | `id` | DB |
| Unique | `build_id` | DB |
| Unique | `request_id` | DB (idempotency) |
| Index | `application_id` | DB (`db_index=True`) |
| Index | `owner_id` | DB (`db_index=True`) |
| Row lock | — | `select_for_update()` in `transition()` |

### Status transitions

```
QUEUED ──────────────────────► BUILDING ──────────────────────► SUCCESS
  │                               │                                  │
  └──────────────────────────────► FAILED ◄─────────────────────────┘
                                (terminal)
```

Both `SUCCESS` and `FAILED` are terminal — no further transitions are allowed.

### Field lifecycle

| Field | Set at | Never overwritten after |
|---|---|---|
| `commit_sha` | Pipeline start | Set once |
| `image_repository` | Pipeline start | Set once |
| `image_tag` | Pipeline start | Set once |
| `image_digest` | SUCCESS callback | Set once |
| `error_code` | FAILED transition | Set once |
| `error_message` | FAILED transition | Set once |
| `started_at` | First BUILDING entry | Set once |
| `finished_at` | Any terminal transition | Set once |

---

## 12. Environment Variables

All environment variables are defined in `config/settings.py` and templated in `.env.example`. Copy `.env.example` to `.env` and fill in the required values before running.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ Yes | `dev-insecure-secret-key` | Django secret key. **Must be changed in production.** |
| `DATABASE_URL` | ✅ Yes | `sqlite:///db.sqlite3` | Full database URL, e.g. `postgres://build:build@postgres:5432/build_service` |
| `DEBUG` | No | `false` | Set to `true` for development. Never `true` in production. |
| `ALLOWED_HOSTS` | No | `*` | Comma-separated list of allowed hostnames |
| `CELERY_BROKER_URL` | ✅ Yes | `redis://localhost:6379/0` | Redis URL used as the Celery broker |
| `BUILD_WORKSPACE_ROOT` | No | `/tmp/builds` | Directory where temporary git clones are created |
| `GIT_TIMEOUT_SECONDS` | No | `60` | Timeout (seconds) applied to every git subprocess call |
| `JENKINS_URL` | ✅ Yes | `""` | Base URL of the Jenkins instance, e.g. `http://jenkins:8080` |
| `JENKINS_USER` | ✅ Yes | `""` | Jenkins username for API authentication |
| `JENKINS_API_TOKEN` | ✅ Yes | `""` | Jenkins API token for the above user |
| `BUILD_CALLBACK_BASE_URL` | ✅ Yes | `""` | Base URL this service is reachable at by Jenkins, e.g. `http://web:8000` |
| `INTERNAL_SERVICE_TOKEN` | ✅ Yes | `""` | Bearer token Jenkins must present to the callback endpoint |
| `DOCKERHUB_NAMESPACE` | ✅ Yes | `""` | DockerHub account/namespace, e.g. `acme` |
| `PLATFORM_NAME` | ✅ Yes | `""` | Platform identifier used in image naming, e.g. `autodeploy` |

> **Note:** Variables marked with ✅ are required for the service to function. Those with defaults may be left unset in development but must be explicitly configured for production.

---

## 13. Installation & Setup

### Prerequisites

- **Docker Desktop** (or Docker Engine + Docker Compose v2) — required for the full stack
- **Git** — installed on the Docker host (the Dockerfile installs it in the application container)
- A **DockerHub** account with push access to your namespace
- A **Jenkins** instance (the included `jenkins/Dockerfile` can be used to build a local one)

### 1. Clone the repository

```bash
git clone <repository-url>
cd Build-Service-recovered
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set all required values:

```env
SECRET_KEY=your-long-random-secret-key
DATABASE_URL=postgres://build:build@postgres:5432/build_service
CELERY_BROKER_URL=redis://redis:6379/0

BUILD_WORKSPACE_ROOT=/tmp/builds
GIT_TIMEOUT_SECONDS=60

JENKINS_URL=http://jenkins:8080
JENKINS_USER=admin
JENKINS_API_TOKEN=your-jenkins-api-token

BUILD_CALLBACK_BASE_URL=http://web:8000
INTERNAL_SERVICE_TOKEN=your-random-callback-secret

DOCKERHUB_NAMESPACE=your-dockerhub-username
PLATFORM_NAME=myplatform
```

### 3. Jenkins credentials setup

In Jenkins, create two credentials:
- **ID: `dockerhub`** — Username/Password with your DockerHub credentials
- **ID: `build-callback-token`** — Secret Text with the same value as `INTERNAL_SERVICE_TOKEN`

Create a Pipeline job named **`build-image`** using the `jenkins/Jenkinsfile` from this repository.

---

## 14. Running the Project

### With Docker Compose (recommended)

```bash
# Build and start all services
docker compose up --build

# Run in background
docker compose up --build -d

# Apply database migrations (first run only)
docker compose exec web python manage.py migrate

# Create a Django superuser (for admin access)
docker compose exec web python manage.py createsuperuser

# View logs
docker compose logs -f web
docker compose logs -f celery

# Stop all services
docker compose down
```

### Services and ports

| Service | Port | Description |
|---|---|---|
| `web` | `8000` | Django application server |
| `celery` | — | Celery worker (no exposed port) |
| `postgres` | `5432` | PostgreSQL database |
| `redis` | `6379` | Redis broker |
| `jenkins` | `8081` | Jenkins controller UI |

**Django Admin:** http://localhost:8000/admin/
**Jenkins:** http://localhost:8081/

### Local development (without Docker)

Requires Python 3.12, PostgreSQL, and Redis running locally.

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with local DB and Redis URLs

# Apply migrations
python manage.py migrate

# Start the web server
python manage.py runserver

# Start the Celery worker (separate terminal)
celery -A config worker -Q build -l info
```

### Collecting static files

```bash
python manage.py collectstatic --noinput
```

---

## 15. Testing

### Configuration (`pytest.ini`)

```ini
[pytest]
DJANGO_SETTINGS_MODULE = config.settings
python_files = tests.py test_*.py *_tests.py
```

### Running tests

```bash
# With Docker Compose (requires running postgres)
docker compose exec web pytest

# With verbose output
docker compose exec web pytest -v

# Run a specific test file
docker compose exec web pytest builds/tests/test_state_machine.py -v

# Run a specific test
docker compose exec web pytest builds/tests/test_intake.py::test_valid_payload_queues_build_and_dispatches -v

# Locally (with DATABASE_URL pointing to a test DB)
pytest
pytest -v
```

### Test structure

| Test file | Phase | What is covered |
|---|---|---|
| `test_state_machine.py` | P0/P1 | Project config, state transitions, concurrency, immutability |
| `test_intake.py` | P2 | `start_build` task, validation, idempotency |
| `test_git.py` | P3 | `resolve_commit`, `shallow_clone`, `resolve_in_repo`, symlink escape |
| `test_templating.py` | P3 | Dockerfile rendering, injection protection, all 4 app types |
| `test_pipeline.py` | P4 | Full pipeline with mocked git/Jenkins/templating |
| `test_result.py` | P5 | Callback endpoint auth, success/failure, idempotency |
| `test_notifications.py` | P5 | Notification payload shapes, dispatch, skip-on-failure |
| `test_jenkins.py` | P4 | Jenkins HTTP trigger, parameter safety, error handling |
| `test_naming.py` | P4 | Image repository and tag naming |
| `test_admin.py` | — | Admin registration, access control |

### Notable test coverage

**Idempotency:**
```bash
pytest builds/tests/test_intake.py::test_duplicate_delivery_is_noop -v
```

**Concurrency (two workers racing on the same build):**
```bash
pytest builds/tests/test_state_machine.py::test_only_one_worker_can_start_build -v
```

**Injection protection:**
```bash
pytest builds/tests/test_templating.py::test_run_command_shell_metachars_stay_inert -v
pytest builds/tests/test_templating.py::test_run_command_substitution_stays_literal -v
```

**Symlink escape:**
```bash
pytest builds/tests/test_git.py::test_resolve_in_repo_symlink_escape -v
```

**All validation rules:**
```bash
pytest builds/tests/test_intake.py::test_invalid_field_records_failed -v
```

---

## 16. Jenkins Integration

### Overview

Jenkins acts as the isolated build executor. It has access to the Docker daemon on its host and to DockerHub credentials. The Build Service never performs a `docker build` or `docker push` itself.

### Communication flow

```
Build Service                    Jenkins
─────────────────                ────────────────────────────────────────
POST /job/build-image/         →  Receives: BUILD_ID, REPO_URL, COMMIT_SHA,
  buildWithParameters               DOCKERFILE_B64, IMAGE_REPOSITORY,
  auth: (user, api_token)           IMAGE_TAG, CALLBACK_URL
                                 Credentials in Jenkins only:
                                   "dockerhub" (registry login)
                                   "build-callback-token" (callback auth)
                               ↓
                                 Stage: Checkout commit
                                   git init, fetch --depth 1 <SHA>, checkout
                               ↓
                                 Stage: Write Dockerfile
                                   echo "$DOCKERFILE_B64" | base64 -d > Dockerfile
                               ↓
                                 Stage: Build
                                   docker build -t <REPO>:<TAG> -f Dockerfile .
                               ↓
                                 Stage: Push
                                   docker login (from "dockerhub" credential)
                                   docker push <REPO>:<TAG>
                               ↓
                                 Stage: Report success
                                   docker inspect → digest
                                   curl POST <CALLBACK_URL>
                                     Authorization: Bearer <CALLBACK_TOKEN>
                                     {"status":"success","image_digest":"sha256:..."}

POST /internal/builds/{id}/result/ ← (from Jenkins callback)
```

### Jenkinsfile (`jenkins/Jenkinsfile`)

The pipeline is a declarative Jenkins pipeline with these stages:

1. **Checkout commit** — clones and pins to the exact SHA
2. **Write Dockerfile** — decodes the base64 parameter
3. **Build** — `docker build`
4. **Push** — `docker login` + `docker push`
5. **Report success** — inspects the digest and POSTs to the callback URL

**Post-build actions:**
- `post.failure` — POSTs `{"status":"failed","error_code":"BUILD_FAILED"}` to the callback URL
- `post.always` — `docker logout` and `cleanWs()` (workspace cleanup)

### Security in the Jenkinsfile

- Registry credentials and callback token come from Jenkins's credentials store, not from job parameters.
- All `sh` steps use **single-quoted strings** to prevent Groovy from interpolating variables into shell strings, which would leak values into the process list and build log.
- `docker logout` always runs in `post.always`.

### Jenkins controller image (`jenkins/Dockerfile`)

A custom Jenkins image is provided for the local Docker Compose stack. It is built and started as the `jenkins` service on port `8081`.

```yaml
# docker-compose.yml excerpt
jenkins:
  build:
    context: .
    dockerfile: jenkins/Dockerfile
  ports:
    - "8081:8080"
  volumes:
    - jenkins_home:/var/jenkins_home
    - /var/run/docker.sock:/var/run/docker.sock
```

> **Warning:** Mounting the Docker socket (`/var/run/docker.sock`) gives the Jenkins container root-equivalent access to the host's Docker daemon. This setup is intended for local development and testing only, not for production deployment.

---

## 17. Error Handling

### Error codes used in the current implementation

| `error_code` | Set when | Transition |
|---|---|---|
| `INVALID_PAYLOAD` | Serializer validation fails (bad URL, branch, type, paths, etc.) | `QUEUED → FAILED` (before pipeline) |
| `INVALID_PAYLOAD` | `source_directory` or `requirements_file` does not exist in the clone | Pipeline → `FAILED` |
| `GIT_ERROR` | `git ls-remote` or `shallow_clone` fails (timeout, network, bad branch, etc.) | Pipeline → `FAILED` |
| `TEMPLATE_ERROR` | No template for the application type, or Jinja2 rendering fails | Pipeline → `FAILED` |
| `EXECUTOR_UNAVAILABLE` | Jenkins is unreachable or returns HTTP 4xx/5xx | Pipeline → `FAILED` |
| `INTERNAL_ERROR` | Unexpected uncaught exception in the pipeline | Pipeline → `FAILED` |
| `BUILD_FAILED` | `docker build` or `docker push` failed inside Jenkins | Callback → `FAILED` |

### Error propagation

- **Envelope errors** (bad `request_id`, `application_id`, `owner_id`): message is discarded; no DB record; no notification.
- **Payload validation errors**: a `Build(FAILED)` row is created immediately; `notify_application` is dispatched.
- **Pipeline errors**: `Build` transitions to `FAILED` via `_fail()`; `notify_application` is dispatched from the callback view (after Jenkins reports failure) or directly from the pipeline on executor errors.
- **Notification failures**: retried up to 5 times with backoff; the build's own status is never affected.

### Concurrency protection

The `transition()` function acquires a row-level lock with `select_for_update()` inside `transaction.atomic()`. If two workers attempt the same transition simultaneously, one raises `InvalidTransition` and exits safely. The callback endpoint returns `409` in this case.

---

## 18. Security

### Implemented security measures

**Bearer token authentication (callback endpoint)**
The `POST /internal/builds/{id}/result/` endpoint requires a `Bearer` token. It is compared using `django.utils.crypto.constant_time_compare`, which prevents timing-based token enumeration.

**No credentials in git operations**
All `git` subprocesses set `GIT_TERMINAL_PROMPT=0` in the environment, preventing git from blocking on credential prompts. Only public repositories are supported. No credentials are passed on the command line or stored anywhere.

**Argument-list subprocess calls (no `shell=True`)**
Every `subprocess.run()` call in `integrations/git.py` uses an argument list, not a shell string. Shell injection via repository URLs or branch names is not possible.

**Exec-form `CMD` in Dockerfiles (injection prevention)**
`run_command` is always parsed with `shlex.split()` and re-encoded as a JSON array. It is impossible to inject additional Dockerfile instructions or shell commands through the `run_command` field.

**Path traversal and symlink protection**
`resolve_in_repo()` uses `Path.resolve()` (which follows symlinks) to compute the canonical path, then checks that it is a descendant of the repository root. Both `..` traversal and symlinks pointing outside the repo are rejected.

**Credentials only in Jenkins Credential Store**
DockerHub credentials and the callback token are stored in Jenkins's credentials store (IDs `dockerhub` and `build-callback-token`). They are never passed as job parameters, logged, or embedded in Dockerfiles or image layers.

**Single-quoted shell strings in Jenkinsfile**
All `sh` steps in the Jenkinsfile use single-quoted strings, preventing Groovy variable interpolation, which would leak secret values into the process list and build log.

**No cross-service database access**
`application_id` and `owner_id` are stored as plain `BigIntegerField` columns with no `ForeignKey` to any external database.

**Django security middleware enabled**
`SecurityMiddleware`, `XFrameOptionsMiddleware`, `CsrfViewMiddleware`, and `AuthenticationMiddleware` are all active in the middleware stack.

**WhiteNoise for static files**
Static files are served by WhiteNoise middleware, eliminating the need to expose a separate file server.

### Not yet implemented

- The application container does **not** run as a non-root user.
- There is no structured (JSON) logging.
- There are no Prometheus metrics.
- There is no HTTPS enforcement at this layer (expected to be handled by a reverse proxy in production).
- Read endpoints with JWT authentication are not implemented.

---

## 19. Current Limitations

The following features are described in the design plan but are **not implemented** in the current codebase.

### Model and state machine

- **Missing `PREPARING` and `PUSHING` states.** The implemented state machine has 4 states (`QUEUED`, `BUILDING`, `SUCCESS`, `FAILED`). The design plan specifies 6 states, with `PREPARING` for the git/render phase and `PUSHING` for the Docker push phase. These intermediate states would allow finer-grained progress reporting.
- **No `updated_at` field.** The `Build` model has no `updated_at` timestamp.
- **No `template_name` / `template_version` fields.** The model does not record which template version was used for a build.
- **No `log_reference` field.** No reference to the Jenkins build log URL is stored.
- **No `rendered_dockerfile_hash` field.** The hash of the rendered Dockerfile is not stored.
- **No `executor_name` / `executor_handle` fields.** The Jenkins job number and queue URL are not persisted.
- **No notification tracking fields.** There is no `app_notification_status`, `deployment_notification_status`, attempt counter, or `notified_at` timestamp on the `Build` model.
- **No `selectors.py`.** Selector functions (`get_build_by_build_id`, `list_builds_for_application`, `list_stuck_builds`) do not exist.

### Infrastructure and workers

- **Single Celery worker.** The `docker-compose.yml` runs one Celery service consuming the `build` queue. The design plan calls for separate `worker-intake` (fast) and `worker-exec` (long-running) workers.
- **No Celery beat service.** There is no periodic task scheduler, so stuck builds are never automatically recovered.
- **No task queue routing.** All tasks use the same `build` queue. The design plan specifies `build_exec` for pipeline tasks and `build_notify` for notification tasks.
- **`task_acks_late` and `worker_prefetch_multiplier` not configured.**

### Reconciler

- **No stuck-build reconciler.** A build stuck in `QUEUED` or `BUILDING` (e.g., due to a worker crash) will remain in that state permanently. The design plan specifies a Celery beat task to detect and handle these.
- **No `sweep_old_workspaces`.** Old workspace directories from crashed workers are not cleaned up automatically.

### Executor and registry abstractions

- **No `BuildExecutor` interface (Phase 6 of design plan).** Jenkins is called directly from `services.py` with no abstraction. It is not possible to swap the build engine without modifying service code.
- **No `ContainerRegistry` interface (Phase 7).** There is no registry adapter. The service trusts the digest reported by Jenkins without independent verification against the DockerHub API.
- **No `FakeExecutor` for testing.** Pipeline tests require mocking Jenkins at the function level rather than via a swappable executor interface.

### Templates

- **No versioned template layout.** Templates are at `templates/<type>/Dockerfile.j2`, not `templates/<type>/v1/Dockerfile.j2`. There is no mechanism to introduce a `v2` template without breaking the existing path.
- **No `template.yaml` metadata files.** There is no machine-readable schema for required variables per template type.
- **`ACTIVE_TEMPLATE_VERSIONS` setting is absent.** Template selection is implicit (by filename), not driven by configuration.
- **Node template uses `npm install` instead of `npm ci`.** `npm ci` is preferred for reproducible builds.
- **React template is single-stage.** The design plan recommended a multi-stage build (build stage with Node, serve stage with nginx or `serve`).

### Git error granularity

- **Generic `GIT_ERROR` for all git failures.** The design plan defines distinct codes `REPO_NOT_FOUND`, `BRANCH_NOT_FOUND`, `GIT_AUTH_FAILED`, and `GIT_NETWORK_ERROR` (the only retryable one). The current code raises a single `GitError` for all these cases, losing the ability to distinguish transient from permanent failures.
- **No `MAX_REPO_SIZE_MB` enforcement.** Repository size is not checked after cloning.
- **No `REPO_TOO_LARGE` error code.**

### Pipeline

- **`run_build_pipeline.delay()` is not wrapped in `transaction.on_commit()`.** If the database transaction creating the `Build` row rolls back after the task is dispatched, the Celery worker will receive a `build_id` that does not exist.
- **No retry logic for transient pipeline errors** (e.g., temporary Jenkins unavailability).
- **`notify_application` is not dispatched from pipeline failure path.** If the pipeline fails before Jenkins is triggered, `notify_application` is dispatched by the Jenkins callback only. If Jenkins is never triggered (e.g., network error to Jenkins), the Application Service is not notified until after the callback view is called — which will never happen.

### APIs and authentication

- **No public read endpoints.** There is no `GET /builds/{id}/`, no `GET /builds/?application_id=`, and no `GET /builds/{id}/logs/` endpoint.
- **No JWT authentication.** `PyJWT` is not installed. There is no `builds/authentication.py`.
- **No `/health/` endpoint.** There is no health check endpoint for load balancers or container orchestrators.

### Observability and operations

- **No structured logging.** Log output is plain text. There is no `build_id` or `stage` field in log records.
- **No Prometheus metrics.** `prometheus-client` is not installed. There is no `/metrics/` endpoint.
- **Non-root container.** The `Dockerfile` does not add or switch to a non-root user.

### Jenkins integration completeness

- **No CSRF crumb handling.** Some Jenkins configurations require a CSRF crumb with API requests. The current `trigger_build()` does not fetch or include a crumb.
- **No `Location` header resolution.** The `POST` to Jenkins returns a `Location` header pointing to the queue item. The current implementation does not follow this to retrieve the build number.
- **No `get_status()` or `get_logs_reference()` on Jenkins.** There is no way to poll Jenkins for build status from the Build Service side.
- **No progress callback before push** (`PUSHING` state). Jenkins does not send an intermediate callback before the push stage.
- **No `retry(3)` on `docker push` in the Jenkinsfile.**
