"""Render a step's params against a context of already-finished steps'
results, plus caller-supplied top-level variables.

Deliberately minimal: every string value in ``params`` is rendered as a
Jinja2 template with ``StrictUndefined`` (a reference to an unfinished or
misspelled step/variable is a loud render-time error, not a silently empty
string). Non-string values (numbers, booleans, nested dicts/lists) pass
through structurally - only strings actually get template-rendered.

Trust note (same open question already logged for prompt-template-manager's
``renderer.py`` in research/lab/BACKLOG.md, P2/conditional): ``_ENV`` is a
plain ``jinja2.Environment``, not a ``SandboxedEnvironment`` - it runs the
*full* Jinja2 language (loops, filters, macros, attribute access), not just
``{{ var }}`` substitution. That is fine under today's assumption that a
pipeline YAML file is operator-authored and trusted the same as code (it can
already invoke arbitrary gateway capabilities). It stops being fine the
moment a pipeline file can come from a less-trusted source (a shared
marketplace, an uploaded file, a webhook) - Jinja2 SSTI payloads in a
``params`` string (e.g. reaching ``__class__``/``__mro__``/``__globals__``
off any object in ``steps``/``vars``) can read process state or worse. If
that assumption ever changes, switch ``_ENV`` to
``jinja2.sandbox.SandboxedEnvironment`` rather than trying to sanitize
template strings.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, StrictUndefined

_ENV = Environment(undefined=StrictUndefined)


class TemplateRenderError(Exception):
    pass


def render_params(params: dict[str, Any], *, steps: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    context = {"steps": steps, "vars": variables}
    return {key: _render_value(value, context) for key, value in params.items()}


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return _ENV.from_string(value).render(**context)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, mirrors runner.run_one's
            # identical choice: a bad template (undefined reference, syntax error like a
            # missing '}}', a filter raising on the wrong type, ...) must become a clean
            # per-step error the caller turns into StepResult(status="error"), never a raw
            # jinja2/Python exception that escapes run_pipeline as something other than
            # PipelineRunError and crashes the whole run.
            raise TemplateRenderError(str(exc)) from exc
    if isinstance(value, dict):
        return {k: _render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v, context) for v in value]
    return value
