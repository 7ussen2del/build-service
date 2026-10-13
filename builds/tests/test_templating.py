"""Phase 3 tests: Dockerfile templating safety (README section 5)."""
import pytest
from jinja2 import UndefinedError

from builds import templating
from builds.templating import TemplateError, render_dockerfile


def _render(**overrides):
    kwargs = dict(
        source_directory="src",
        run_command="bash ./run.sh",
        port=8000,
        requirements_file="requirements.txt",
    )
    application_type = overrides.pop("application_type", "django")
    kwargs.update(overrides)
    return render_dockerfile(application_type, **kwargs)


def test_known_good_django_render():
    out = _render()
    assert "FROM python:3.12-slim" in out
    assert "COPY requirements.txt ." in out
    assert "RUN pip install --no-cache-dir -r requirements.txt" in out
    assert "COPY src ." in out
    assert "EXPOSE 8000" in out
    assert 'CMD ["bash", "./run.sh"]' in out


def test_run_command_shell_metachars_stay_inert():
    out = _render(run_command="; rm -rf /")
    assert 'CMD [";", "rm", "-rf", "/"]' in out
    # No shell line that would actually run the deletion.
    assert "RUN rm -rf" not in out


def test_run_command_substitution_stays_literal():
    out = _render(run_command="$(reboot)")
    assert 'CMD ["$(reboot)"]' in out


def test_unknown_type_raises_template_error():
    with pytest.raises(TemplateError):
        _render(application_type="ruby")


def test_unbalanced_quotes_raise_template_error():
    with pytest.raises(TemplateError):
        _render(run_command='echo "unterminated')


def test_environment_is_strict_undefined():
    env = templating._environment()
    with pytest.raises(UndefinedError):
        env.from_string("{{ missing_variable }}").render()


def test_environment_autoescape_is_off():
    assert templating._environment().autoescape is False


@pytest.mark.parametrize(
    "app_type,base_image,marker",
    [
        ("flask", "python:3.12-slim", "pip install --no-cache-dir -r requirements.txt"),
        ("node", "node:20-slim", "npm install"),
        ("react", "node:20-slim", "npm run build"),
    ],
)
def test_additional_types_select_their_template(app_type, base_image, marker):
    out = _render(application_type=app_type)
    assert f"FROM {base_image}" in out
    assert marker in out
    assert 'CMD ["bash", "./run.sh"]' in out
