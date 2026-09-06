"""Execute a Pipeline against an ai-job-gateway-compatible server.

Steps run in dependency layers (see ``pipeline.execution_layers``): every
step within one layer runs concurrently via ``asyncio.gather``, since by
construction none of them depend on each other. A step's rendered result
becomes available to every later layer's Jinja2 templates as
``steps.<name>.result``.

Two things bound what that concurrency costs the gateway on the other end:

* **How many steps are in flight** is capped by ``max_concurrent_steps``.
  Without it a layer's width *is* the burst: a 40-step fan-out submitted 40
  jobs in the same tick, measured on the mock transport.
* **How often each in-flight step asks** backs off. A poll that keeps
  answering "processing" is not worth repeating at the same rate forever;
  see ``_next_poll_interval``.
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


#: First wait between polls of a submitted job. Deliberately short: most
#: of the gateway's local capabilities finish almost immediately, and a
#: pipeline that spends its first second sleeping feels broken.
DEFAULT_POLL_INTERVAL = 0.3

#: Ceiling the interval grows to. A job that has already run for a minute
#: is not going to be meaningfully later for waiting five seconds more,
#: and the alternative is another 16 pointless round trips.
DEFAULT_MAX_POLL_INTERVAL = 5.0

#: A job younger than this keeps being polled at the base interval, so a
#: fast step still returns fast. Backoff only starts once the job has
#: proven it is not one of those.
POLL_BACKOFF_AFTER_SECONDS = 1.0

#: Growth per poll once backoff starts. 1.5 rather than 2.0: reaching the
#: ceiling in ~7 polls instead of ~4 keeps the interval closer to the
#: actual completion time of a mid-length job.
POLL_BACKOFF_FACTOR = 1.5

#: How many of a layer's steps may be in flight at the gateway at once.
#: Matches ai-job-gateway's own DEFAULT_WEBHOOK_CONCURRENCY, and sits
#: inside httpx's 20-connection keepalive pool, so a wide layer reuses
#: connections instead of churning a new one per step. ``None`` or 0
#: restores the old unbounded behaviour.
DEFAULT_MAX_CONCURRENT_STEPS = 10


def _next_poll_interval(interval: float, elapsed: float, *, max_interval: float) -> float:
    """The wait before the next poll of a job that has been running for
    ``elapsed`` seconds and was last polled ``interval`` seconds ago.

    Flat while the job is young, geometric to a ceiling after that. Note
    that a zero interval stays zero -- growth is multiplicative on purpose,
    so a caller that asks not to wait (the test suite, mostly) never starts
    waiting halfway through a run.
    """
    if elapsed < POLL_BACKOFF_AFTER_SECONDS:
        return interval
    return min(max_interval, interval * POLL_BACKOFF_FACTOR)


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
    max_poll_interval: float,
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

    started = time.monotonic()
    deadline = started + timeout
    interval = poll_interval
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
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeError(f"did not finish within {timeout}s (last status: {outcome.status!r})")
        interval = _next_poll_interval(interval, now - started, max_interval=max_poll_interval)
        # Never sleep past the deadline: the run should report a timeout at
        # the timeout, not one backoff step later.
        await asyncio.sleep(min(interval, deadline - now))


async def _run_step_bounded(slots: Optional[asyncio.Semaphore], *args: Any, **kwargs: Any) -> Any:
    """``_run_step`` behind an optional concurrency gate."""
    if slots is None:
        return await _run_step(*args, **kwargs)
    async with slots:
        return await _run_step(*args, **kwargs)


async def run_pipeline(
    pipeline: Pipeline,
    gateway_url: str,
    *,
    variables: Optional[dict[str, Any]] = None,
    http_client: Optional[httpx.AsyncClient] = None,
    timeout: float = 60.0,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_poll_interval: float = DEFAULT_MAX_POLL_INTERVAL,
    max_concurrent_steps: Optional[int] = DEFAULT_MAX_CONCURRENT_STEPS,
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
    slots = asyncio.Semaphore(max_concurrent_steps) if max_concurrent_steps else None
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
                    # Unlike ai-job-gateway's webhook semaphore -- which is
                    # released across the retry sleep so a backing-off
                    # delivery cannot starve the others -- this slot is held
                    # for the whole step, submission through polling. The
                    # slot *is* the in-flight-job budget; handing it back
                    # while the job is still running at the gateway would
                    # cap nothing.
                    result = await _run_step_bounded(
                        slots,
                        base_url,
                        step.capability,
                        rendered,
                        http_client=client,
                        timeout=timeout,
                        poll_interval=poll_interval,
                        max_poll_interval=max_poll_interval,
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
