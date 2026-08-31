"""Execute a Pipeline against an ai-job-gateway-compatible server.

Steps run in dependency layers (see ``pipeline.execution_layers``): every
step within one layer runs concurrently via ``asyncio.gather``, since by
construction none of them depend on each other. A step's rendered result
becomes available to every later layer's Jinja2 templates as
``steps.<name>.result``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .gateway_poll import (
    GatewayHTTPError,
    classify_poll_body,
    expired_detail,
    is_expired_poll_response,
    parse_submission,
    resolve_polling_url,
    submit_url,
)
from .pipeline import Pipeline, execution_layers
from .templating import TemplateRenderError, render_params


class PipelineRunError(Exception):
    def __init__(self, step_name: str, message: str) -> None:
        self.step_name = step_name
        self.message = message
        super().__init__(f"step {step_name!r} failed: {message}")


@dataclass
class StepResult:
    name: str
    status: str  # "ready" or "error"
    result: Optional[Any] = None
    error: Optional[str] = None


async def _run_step(
    gateway_url: str,
    capability: str,
    rendered_params: dict[str, Any],
    *,
    http_client: httpx.AsyncClient,
    timeout: float,
    poll_interval: float,
) -> Any:
    """Submit one job and poll it to completion. Returns the job's result,
    or raises PipelineRunError-friendly exceptions (caller attaches the
    step name)."""
    response = await http_client.post(submit_url(gateway_url, capability), json=rendered_params)
    body_json = response.json() if response.status_code < 400 else None
    try:
        _job_id, polling_url = parse_submission(response.status_code, body_json, response.text)
    except GatewayHTTPError as exc:
        raise RuntimeError(f"submission rejected ({exc.status_code}): {exc.body_text}") from exc

    deadline = time.monotonic() + timeout
    while True:
        poll_response = await http_client.get(resolve_polling_url(gateway_url, polling_url))
        if is_expired_poll_response(poll_response.status_code):
            raise RuntimeError(expired_detail(poll_response.json()))
        poll_response.raise_for_status()
        outcome = classify_poll_body(poll_response.json())
        if outcome.ready:
            return outcome.result
        if outcome.terminal:
            raise RuntimeError(outcome.error_message)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"did not finish within {timeout}s (last status: {outcome.status!r})")
        await asyncio.sleep(poll_interval)


async def run_pipeline(
    pipeline: Pipeline,
    gateway_url: str,
    *,
    variables: Optional[dict[str, Any]] = None,
    http_client: Optional[httpx.AsyncClient] = None,
    timeout: float = 60.0,
    poll_interval: float = 0.3,
) -> dict[str, StepResult]:
    """Run every step of ``pipeline`` in dependency order. Returns a dict of
    every step's StepResult, keyed by step name - including steps that
    never ran because an earlier layer failed (status "skipped" is not
    used; instead PipelineRunError is raised as soon as a layer contains a
    failure, with the successful results from prior layers preserved on
    the exception's ``partial_results`` attribute for inspection).
    """
    client = http_client or httpx.AsyncClient()
    owns_client = http_client is None
    variables = variables or {}
    base_url = gateway_url.rstrip("/")

    results: dict[str, StepResult] = {}
    steps_context: dict[str, Any] = {}

    try:
        for layer in execution_layers(pipeline):

            async def run_one(step_name: str) -> StepResult:
                step = pipeline.step(step_name)
                try:
                    rendered = render_params(step.params, steps=steps_context, variables=variables)
                except TemplateRenderError as exc:
                    return StepResult(name=step_name, status="error", error=f"template error: {exc}")
                try:
                    result = await _run_step(
                        base_url,
                        step.capability,
                        rendered,
                        http_client=client,
                        timeout=timeout,
                        poll_interval=poll_interval,
                    )
                except Exception as exc:  # noqa: BLE001 - deliberately broad, see model-comparison-harness's identical choice
                    return StepResult(name=step_name, status="error", error=str(exc))
                return StepResult(name=step_name, status="ready", result=result)

            layer_results = await asyncio.gather(*(run_one(name) for name in layer))
            for step_result in layer_results:
                results[step_result.name] = step_result
                steps_context[step_result.name] = {"status": step_result.status, "result": step_result.result}

            failed = [r for r in layer_results if r.status == "error"]
            if failed:
                first = failed[0]
                error = PipelineRunError(first.name, first.error or "unknown error")
                error.partial_results = results  # type: ignore[attr-defined]
                raise error

        return results
    finally:
        if owns_client:
            await client.aclose()
