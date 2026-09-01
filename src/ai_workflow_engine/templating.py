"""Render a step's params against a context of already-finished steps'
results, plus caller-supplied top-level variables.

Deliberately minimal: every string value in ``params`` is rendered as a
Jinja2 template with ``StrictUndefined`` (a reference to an unfinished or
misspelled step/variable is a loud render-time error, not a silently empty
string). Non-string values (numbers, booleans, nested dicts/lists) pass
through structurally - only strings actually get template-rendered.

``_ENV`` is a ``jinja2.sandbox.SandboxedEnvironment``, not a plain
``Environment`` - a template string can still reach ``__class__``/``__mro__``/
``__globals__`` off any object in ``steps``/``vars`` (the standard Jinja2 SSTI
gadget chain) under a plain ``Environment``, which is RCE-capable. Sandboxing
was verified free: every legitimate usage here (``vars.x`` / dict/attribute
access on ``steps.name.result``, ``StrictUndefined`` error behavior) renders
identically under ``SandboxedEnvironment`` - this codebase never used macros,
custom filters, or anything else Sandboxed would actually block, so this
costs nothing today and closes the SSTI path before a pipeline file could
ever plausibly come from a less-trusted source than "operator-authored,
trusted like code" (a shared marketplace, an uploaded file, a webhook).
"""

from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

_ENV = SandboxedEnvironment(undefined=StrictUndefined)


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
