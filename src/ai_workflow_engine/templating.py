"""Render a step's params against a context of already-finished steps'
results, plus caller-supplied top-level variables.

Deliberately minimal: every string value in ``params`` is rendered as a
Jinja2 template with ``StrictUndefined`` (a reference to an unfinished or
misspelled step/variable is a loud render-time error, not a silently empty
string). Non-string values (numbers, booleans, nested dicts/lists) pass
through structurally - only strings actually get template-rendered.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, StrictUndefined, UndefinedError

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
        except UndefinedError as exc:
            raise TemplateRenderError(str(exc)) from exc
    if isinstance(value, dict):
        return {k: _render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v, context) for v in value]
    return value
