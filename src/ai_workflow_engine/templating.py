"""Render a step's params against a context of already-finished steps'
results, plus caller-supplied top-level variables.

Deliberately minimal: every string value in ``params`` is rendered as a
Jinja2 template with ``StrictUndefined`` (a reference to an unfinished or
misspelled step/variable is a loud render-time error, not a silently empty
string). Non-string values (numbers, booleans, nested dicts/lists) pass
through structurally - only strings actually get template-rendered.

Security note: ``_ENV`` is a ``jinja2.sandbox.SandboxedEnvironment``, not a
plain ``Environment`` - see this repo's README Security section. A pipeline
file can come from somewhere other than the operator's own keyboard (a
shared library, a downloaded example, a pull request), and a plain
``Environment`` lets a malicious ``params`` string escape string
interpolation entirely and reach arbitrary Python objects (the classic
``{{ ''.__class__.__mro__[1].__subclasses__() }}`` pattern) -- real code
execution, not a theoretical risk. ``SandboxedEnvironment`` blocks that
while leaving ordinary ``{{ vars.x }}`` / ``{{ steps.a.result.x }}``
interpolation (the only thing a legitimate pipeline template does) working
exactly as before.
"""

from __future__ import annotations

from typing import Any

from jinja2 import StrictUndefined, nodes
from jinja2.exceptions import SecurityError
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
        except SecurityError as exc:
            raise TemplateRenderError(
                f"unsafe operation: {exc} (pipeline templates render in a sandboxed Jinja2 "
                "environment; only variable interpolation and safe filters are permitted, "
                "see README Security section)"
            ) from exc
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


def _find_root_attr_refs(value: Any, root_name: str) -> set[str]:
    """Best-effort static scan of a params tree for every
    ``<root_name>.<name>...`` reference (e.g. every ``steps.generate...`` or
    every ``vars.prompt``), without resolving or executing anything. Used
    for validation/introspection ahead of an actual render: catching a
    misspelled or undeclared step/variable reference before a real pipeline
    run gets partway through and fails on it.

    Handles the dotted-attribute form used throughout this project's
    pipelines (``steps.generate.result.output``); a dynamic/subscript form
    (``steps[var].result``) isn't recognized statically and simply won't
    show up here - the runtime StrictUndefined check still catches a real
    problem in that case, this is purely an early-warning layer on top.
    """
    refs: set[str] = set()

    def _scan_string(source: str) -> None:
        try:
            ast = _ENV.parse(source)
        except Exception:  # noqa: BLE001 - a syntax error is reported at render time, not here
            return
        for getattr_node in ast.find_all(nodes.Getattr):
            chain: list[str] = []
            cur: Any = getattr_node
            while isinstance(cur, nodes.Getattr):
                chain.append(cur.attr)
                cur = cur.node
            if isinstance(cur, nodes.Name) and cur.name == root_name and chain:
                refs.add(chain[-1])

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            _scan_string(v)
        elif isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
        elif isinstance(v, list):
            for vv in v:
                _walk(vv)

    _walk(value)
    return refs


def find_step_references(params: dict[str, Any]) -> set[str]:
    """Every step name referenced anywhere in ``params`` via ``steps.<name>...``."""
    return _find_root_attr_refs(params, "steps")


def find_var_references(params: dict[str, Any]) -> set[str]:
    """Every variable name referenced anywhere in ``params`` via ``vars.<name>...``."""
    return _find_root_attr_refs(params, "vars")
