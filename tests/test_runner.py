from __future__ import annotations

import json
import time

import httpx
import pytest

from ai_workflow_engine.pipeline import parse_pipeline
from ai_workflow_engine.runner import PipelineRunError, run_pipeline


def _fake_gateway(*, capability_results=None, capability_delays=None, capability_failures=None):
    """A minimal in-memory ai-job-gateway-compatible mock server: every
    capability's job is 'ready' on the very first poll (no real async
    background work), optionally after a configured delay/failure per
    capability, and echoes back the params it received alongside a
    configured or default result payload.
    """
    capability_results = capability_results or {}
    capability_delays = capability_delays or {}
    capability_failures = capability_failures or {}
    jobs: dict[str, dict] = {}
    job_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            capability = request.url.path.removeprefix("/v1/")
            job_counter["n"] += 1
            job_id = f"job-{job_counter['n']}"
            params = json.loads(request.content)
            jobs[job_id] = {
                "capability": capability,
                "params": params,
                "created": time.monotonic(),
            }
            return httpx.Response(202, json={"id": job_id, "polling_url": f"/v1/jobs/{job_id}"})

        job_id = request.url.path.rsplit("/", 1)[-1]
        job = jobs[job_id]
        capability = job["capability"]
        delay = capability_delays.get(capability, 0)
        if time.monotonic() - job["created"] < delay:
            return httpx.Response(200, json={"status": "processing"})
        if capability in capability_failures:
            return httpx.Response(200, json={"status": "error", "error": capability_failures[capability]})
        result = capability_results.get(capability, {"echoed": job["params"]})
        return httpx.Response(200, json={"status": "ready", "result": result})

    return handler


def _pipeline(steps):
    return parse_pipeline({"name": "test", "steps": steps})


@pytest.mark.asyncio
async def test_single_step_pipeline_returns_result():
    pipeline = _pipeline([{"name": "a", "capability": "echo", "params": {"x": 1}}])
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_gateway()))
    results = await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)
    assert results["a"].status == "ready"
    assert results["a"].result == {"echoed": {"x": 1}}


@pytest.mark.asyncio
async def test_later_step_can_reference_earlier_steps_result():
    pipeline = _pipeline(
        [
            {"name": "generate", "capability": "gen", "params": {"prompt": "a cat"}},
            {
                "name": "upscale",
                "capability": "up",
                "params": {"source": "{{ steps.generate.result.image_url }}"},
                "depends_on": ["generate"],
            },
        ]
    )
    handler = _fake_gateway(capability_results={"gen": {"image_url": "http://x/1.png"}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)
    assert results["generate"].result == {"image_url": "http://x/1.png"}
    assert results["upscale"].result == {"echoed": {"source": "http://x/1.png"}}


@pytest.mark.asyncio
async def test_top_level_variables_are_available_to_templates():
    pipeline = _pipeline([{"name": "a", "capability": "echo", "params": {"prompt": "{{ vars.subject }}"}}])
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_gateway()))
    results = await run_pipeline(
        pipeline, "http://gw.test", variables={"subject": "a red sneaker"}, http_client=client, poll_interval=0
    )
    assert results["a"].result == {"echoed": {"prompt": "a red sneaker"}}


@pytest.mark.asyncio
async def test_independent_steps_in_same_layer_run_concurrently():
    pipeline = _pipeline(
        [
            {"name": "a", "capability": "slow-a", "params": {}},
            {"name": "b", "capability": "slow-b", "params": {}},
        ]
    )
    handler = _fake_gateway(capability_delays={"slow-a": 0.2, "slow-b": 0.2})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    start = time.monotonic()
    results = await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0.02)
    elapsed = time.monotonic() - start

    assert results["a"].status == "ready"
    assert results["b"].status == "ready"
    # sequential would take >= 0.4s; concurrent should comfortably finish well under that
    assert elapsed < 0.35


@pytest.mark.asyncio
async def test_step_failure_raises_pipeline_run_error_and_stops_later_layers():
    pipeline = _pipeline(
        [
            {"name": "a", "capability": "boom", "params": {}},
            {"name": "b", "capability": "echo", "params": {}, "depends_on": ["a"]},
        ]
    )
    handler = _fake_gateway(capability_failures={"boom": "provider exploded"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(PipelineRunError, match="provider exploded") as exc_info:
        await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)

    assert exc_info.value.step_name == "a"
    assert "b" not in exc_info.value.partial_results


@pytest.mark.asyncio
async def test_one_failure_in_a_layer_does_not_hide_the_others_result_that_layer():
    pipeline = _pipeline(
        [
            {"name": "ok", "capability": "echo", "params": {}},
            {"name": "bad", "capability": "boom", "params": {}},
        ]
    )
    handler = _fake_gateway(capability_failures={"boom": "nope"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(PipelineRunError) as exc_info:
        await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)

    partial = exc_info.value.partial_results
    assert partial["ok"].status == "ready"
    assert partial["bad"].status == "error"


@pytest.mark.asyncio
async def test_undefined_template_reference_fails_that_step_without_crashing_the_run():
    pipeline = _pipeline([{"name": "a", "capability": "echo", "params": {"x": "{{ vars.missing }}"}}])
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_gateway()))

    with pytest.raises(PipelineRunError, match="template error"):
        await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)
