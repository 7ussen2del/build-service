"""
Dockerfile rendering.

The threat model here is simple: ``run_command`` and the two path fields come
from outside this service, and a Dockerfile is executable text. Three things
keep that safe:

1. Every variable is validated by *kind* before rendering — paths must resolve
   inside the clone, ``port`` must be an integer in range, and ``run_command``
   is converted to ``json.dumps(shlex.split(cmd))``.
2. ``CMD`` is always exec form (a JSON array), so Docker never hands the value
   to a shell, and JSON escaping keeps a newline from becoming a new
   instruction.
3. Jinja renders in a sandbox with ``StrictUndefined``: a template typo fails
   the build instead of silently producing an empty instruction.

The rendered text is never stored in the database. ``template_name`` and
``template_version`` are, so any image can be traced back to the template that
produced it.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from django.conf import settings
from jinja2 import FileSystemLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from builds.errors import BuildError, ErrorCode
from integrations.paths import ensure_dir_exists, ensure_file_exists, relative_posix

logger = logging.getLogger(__name__)

DOCKERFILE_TEMPLATE_NAME = "Dockerfile.j2"
TEMPLATE_SPEC_NAME = "template.yaml"

_APPLICATION_TYPE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")


class VariableKind(StrEnum):
    REPO_FILE = "repo_file"
    REPO_DIR = "repo_dir"
    PORT = "port"
    EXEC_COMMAND = "exec_command"


@dataclass(frozen=True)
class TemplateVariable:
    name: str
    kind: VariableKind
    required: bool = True


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    version: str
    base_image: str
    variables: tuple[TemplateVariable, ...]
    directory: Path


class RenderedDockerfile(NamedTuple):
    """``(dockerfile_text, template_name, template_version)``."""

    dockerfile: str
    template_name: str
    template_version: str


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------
def templates_root() -> Path:
    return Path(settings.BUILD_TEMPLATES_ROOT)


def load_template_spec(application_type: str) -> TemplateSpec:
    """Load and validate ``templates/<application_type>/template.yaml``."""
    if not _APPLICATION_TYPE_RE.match(str(application_type or "")):
        # The value is a validated choice upstream, but it is also a path
        # component here, so it is re-checked before touching the filesystem.
        raise BuildError(
            ErrorCode.TEMPLATE_NOT_FOUND,
            f"unsupported application type: {application_type!r}",
        )

    directory = templates_root() / str(application_type)
    spec_path = directory / TEMPLATE_SPEC_NAME
    dockerfile_path = directory / DOCKERFILE_TEMPLATE_NAME

    if not (directory.is_dir() and spec_path.is_file() and dockerfile_path.is_file()):
        raise BuildError(
            ErrorCode.TEMPLATE_NOT_FOUND,
            f"no template for application type {application_type!r}",
        )

    try:
        raw = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED, f"{TEMPLATE_SPEC_NAME} is unreadable: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED, f"{TEMPLATE_SPEC_NAME} must be a mapping"
        )

    name = str(raw.get("name") or "")
    version = str(raw.get("version") or "")
    base_image = str(raw.get("base_image") or "")

    if name != str(application_type):
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED,
            f"{TEMPLATE_SPEC_NAME} name {name!r} does not match its directory",
        )
    if not _VERSION_RE.match(version):
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED, f"invalid template version: {version!r}"
        )
    if not base_image:
        raise BuildError(ErrorCode.TEMPLATE_RENDER_FAILED, "base_image is required")

    declared = raw.get("variables") or {}
    if not isinstance(declared, dict) or not declared:
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED, "template declares no variables"
        )

    variables: list[TemplateVariable] = []
    for variable_name, options in declared.items():
        options = options or {}
        if not isinstance(options, dict):
            raise BuildError(
                ErrorCode.TEMPLATE_RENDER_FAILED,
                f"variable {variable_name!r} must be a mapping",
            )
        try:
            kind = VariableKind(str(options.get("kind")))
        except ValueError as exc:
            raise BuildError(
                ErrorCode.TEMPLATE_RENDER_FAILED,
                f"variable {variable_name!r} has an unknown kind: {options.get('kind')!r}",
            ) from exc
        variables.append(
            TemplateVariable(
                name=str(variable_name),
                kind=kind,
                required=bool(options.get("required", True)),
            )
        )

    return TemplateSpec(
        name=name,
        version=version,
        base_image=base_image,
        variables=tuple(variables),
        directory=directory,
    )


def available_templates() -> dict[str, str]:
    """``{template_name: version}`` for every template on disk."""
    found: dict[str, str] = {}
    root = templates_root()
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            spec = load_template_spec(child.name)
        except BuildError:
            continue
        found[spec.name] = spec.version
    return found


# ---------------------------------------------------------------------------
# Variable validation, by kind
# ---------------------------------------------------------------------------
def validate_port(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildError(ErrorCode.INVALID_PAYLOAD, f"port must be an integer, got {value!r}")
    if not 1 <= value <= 65535:
        raise BuildError(ErrorCode.INVALID_PAYLOAD, f"port {value} is out of range")
    return str(value)


def validate_exec_command(value: Any) -> str:
    """Turn a command string into an exec-form JSON array."""
    if not isinstance(value, str) or not value.strip():
        raise BuildError(ErrorCode.INVALID_PAYLOAD, "run_command must be a non-empty string")
    if any(char in value for char in ("\x00", "\n", "\r")):
        raise BuildError(ErrorCode.INVALID_PAYLOAD, "run_command must not contain NUL or newlines")

    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise BuildError(ErrorCode.INVALID_PAYLOAD, f"run_command is not parseable: {exc}") from exc
    if not parts:
        raise BuildError(ErrorCode.INVALID_PAYLOAD, "run_command must contain at least one token")

    # json.dumps escapes quotes, backslashes and control characters, so the
    # result is always exactly one line of valid JSON.
    return json.dumps(parts)


def validate_repo_file(value: Any, repository_root: Path | str) -> str:
    if not isinstance(value, str):
        raise BuildError(ErrorCode.INVALID_PATH, "path must be a string")
    return relative_posix(repository_root, ensure_file_exists(repository_root, value))


def validate_repo_dir(value: Any, repository_root: Path | str) -> str:
    if not isinstance(value, str):
        raise BuildError(ErrorCode.INVALID_PATH, "path must be a string")
    return relative_posix(repository_root, ensure_dir_exists(repository_root, value))


def build_render_context(
    spec: TemplateSpec, values: Any, repository_root: Path | str
) -> dict[str, str]:
    """
    Validate every declared variable and return the render context.

    ``values`` is anything with the snapshot attributes — a ``Build`` row in
    production, a simple namespace in tests.
    """
    context: dict[str, str] = {"base_image": spec.base_image}

    for variable in spec.variables:
        raw = getattr(values, variable.name, None)

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if variable.required:
                raise BuildError(
                    ErrorCode.INVALID_PAYLOAD,
                    f"{variable.name} is required by the {spec.name} template",
                )
            continue

        match variable.kind:
            case VariableKind.PORT:
                context[variable.name] = validate_port(raw)
            case VariableKind.EXEC_COMMAND:
                context[variable.name] = validate_exec_command(raw)
            case VariableKind.REPO_FILE:
                context[variable.name] = validate_repo_file(raw, repository_root)
            case VariableKind.REPO_DIR:
                context[variable.name] = validate_repo_dir(raw, repository_root)

    return context


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _environment(directory: Path) -> SandboxedEnvironment:
    return SandboxedEnvironment(
        loader=FileSystemLoader(str(directory)),
        undefined=StrictUndefined,
        # A Dockerfile is not markup: HTML-escaping would corrupt it.
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_dockerfile(values: Any, repository_root: Path | str) -> RenderedDockerfile:
    """
    Render the Dockerfile for ``values.application_type``.

    Returns the text plus the template identity to record on the build row.
    """
    spec = load_template_spec(getattr(values, "application_type", None))
    context = build_render_context(spec, values, repository_root)

    try:
        template = _environment(spec.directory).get_template(DOCKERFILE_TEMPLATE_NAME)
        text = template.render(**context)
    except TemplateError as exc:
        raise BuildError(
            ErrorCode.TEMPLATE_RENDER_FAILED,
            f"could not render the {spec.name} template: {exc}",
        ) from exc

    if "\x00" in text:  # pragma: no cover - defensive
        raise BuildError(ErrorCode.TEMPLATE_RENDER_FAILED, "rendered Dockerfile contains NUL")

    logger.info("rendered template=%s version=%s", spec.name, spec.version)
    return RenderedDockerfile(text, spec.name, spec.version)
