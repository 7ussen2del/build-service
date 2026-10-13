"""Dockerfile generation: template selection and safe Jinja rendering (README section 5).

Templates live in ``templates/<type>/Dockerfile.j2``. Rendering is sandboxed, uses
StrictUndefined so missing variables error loudly, and turns ``run_command`` into an
exec-form JSON array so it never goes through a shell.
"""
import json
import shlex

from django.conf import settings
from jinja2 import FileSystemLoader, StrictUndefined
from jinja2 import TemplateError as JinjaTemplateError
from jinja2.sandbox import SandboxedEnvironment

# Base image per application type, passed to the template as `base_image`.
BASE_IMAGES = {
    "django": "python:3.12-slim",
    "flask": "python:3.12-slim",
    "node": "node:20-slim",
    "react": "node:20-slim",
}


class TemplateError(Exception):
    """No template for the type, or rendering failed."""


def _environment():
    return SandboxedEnvironment(
        loader=FileSystemLoader(str(settings.BASE_DIR / "templates")),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_dockerfile(
    application_type, *, source_directory, run_command, port=None, requirements_file=None
):
    """Render the Dockerfile for ``application_type`` and return it as a string."""
    base_image = BASE_IMAGES.get(application_type)
    if base_image is None:
        raise TemplateError(f"no template for application type '{application_type}'")

    try:
        command = json.dumps(shlex.split(run_command))
    except ValueError as exc:
        raise TemplateError(f"invalid run_command: {exc}") from exc

    context = {
        "base_image": base_image,
        "source_directory": source_directory,
        "requirements_file": requirements_file,
        "port": port,
        "run_command": command,
    }
    env = _environment()
    try:
        template = env.get_template(f"{application_type}/Dockerfile.j2")
        return template.render(context)
    except JinjaTemplateError as exc:
        raise TemplateError(str(exc)) from exc
