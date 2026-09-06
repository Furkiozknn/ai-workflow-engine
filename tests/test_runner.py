from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from ai_workflow_engine.pipeline import parse_pipeline
from ai_workflow_engine.runner import (
    DEFAULT_MAX_CONCURRENT_STEPS,
    POLL_BACKOFF_AFTER_SECONDS,
    PipelineRunError,
    _next_poll_interval,
    run_pipeline,
)


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


@pytest.mark.asyncio
async def test_malformed_template_syntax_fails_that_step_without_crashing_the_run():
    # Regression: a step referencing "{{ vars.missing" (no closing braces) used to
    # raise a raw jinja2.TemplateSyntaxError that escaped run_pipeline uncaught -
    # instead of the documented PipelineRunError - crashing the whole run in a way
    # `awe run`'s CLI error handling (which only catches PipelineRunError) could not
    # turn into a clean "error:" message.
    pipeline = _pipeline([{"name": "a", "capability": "echo", "params": {"x": "{{ vars.missing"}}])
    client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_gateway()))

    with pytest.raises(PipelineRunError, match="template error"):
        await run_pipeline(pipeline, "http://gw.test", http_client=client, poll_interval=0)


# --------------------------------------------------------------------------
# What a layer costs the gateway: how many steps at once, and how hard each
# one asks. Both were unbounded before; the numbers in these tests are the
# ones measured against the mock transport.
# --------------------------------------------------------------------------


def _counting_gateway(*, ticks=1, duration=None):
    """A gateway mock that records how many requests are in flight at once.

    Each job answers "processing" for its first ``ticks - 1`` polls (or for
    ``duration`` seconds, if given) and "ready" after that. The ``await
    asyncio.sleep(0)`` is what makes the measurement meaningful: without a
    suspension point every handler would run to completion before the next
    one started, and the peak would always read 1.
    """
    state = {"inflight": 0, "peak": 0, "submissions": 0, "polls": 0}
    jobs: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            await asyncio.sleep(0)
            if request.method == "POST":
                state["submissions"] += 1
                job_id = f"job-{state['submissions']}"
                jobs[job_id] = {"polls": 0, "created": time.monotonic()}
                return httpx.Response(202, json={"id": job_id, "polling_url": f"/v1/jobs/{job_id}"})
            state["polls"] += 1
            job = jobs[request.url.path.rsplit("/", 1)[-1]]
            job["polls"] += 1
            if duration is not None:
                done = time.monotonic() - job["created"] >= duration
            else:
                done = job["polls"] >= ticks
            if not done:
                return httpx.Response(200, json={"status": "processing"})
            return httpx.Response(200, json={"status": "ready", "result": {}})
        finally:
            state["inflight"] -= 1

    return handler, state


def _wide(width):
    return _pipeline([{"name": f"s{i}", "capability": "gen", "params": {}} for i in range(width)])


@pytest.mark.asyncio
async def test_a_wide_layer_does_not_submit_every_step_at_once():
    # A 40-step layer has no internal dependencies, so asyncio.gather starts
    # all 40 in the same tick. Measured before the cap existed: peak 40.
    handler, state = _counting_gateway(ticks=5)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    results = await run_pipeline(
        _wide(40), "http://gw.test", http_client=client, poll_interval=0, max_concurrent_steps=10
    )

    assert len(results) == 40 and all(r.status == "ready" for r in results.values())
    assert state["submissions"] == 40, "every step still runs - the cap paces them, it does not drop any"
    assert state["peak"] <= 10, f"peak concurrency was {state['peak']}"


@pytest.mark.asyncio
async def test_the_fan_out_cap_is_on_by_default():
    # Guards the default itself: a caller who passes nothing must still get
    # a bounded burst, or the cap only protects the people who already knew
    # to ask for it.
    handler, state = _counting_gateway(ticks=3)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await run_pipeline(_wide(40), "http://gw.test", http_client=client, poll_interval=0)

    assert DEFAULT_MAX_CONCURRENT_STEPS < 40, "this test needs a layer wider than the default"
    assert state["peak"] <= DEFAULT_MAX_CONCURRENT_STEPS


@pytest.mark.asyncio
async def test_the_cap_can_be_turned_off_for_the_old_unbounded_behaviour():
    handler, state = _counting_gateway(ticks=3)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await run_pipeline(
        _wide(40), "http://gw.test", http_client=client, poll_interval=0, max_concurrent_steps=None
    )

    assert state["peak"] > DEFAULT_MAX_CONCURRENT_STEPS, "opting out must actually opt out"


@pytest.mark.asyncio
async def test_the_cap_does_not_serialise_a_layer_that_fits_inside_it():
    # The cap paces a layer; it must not turn one into a queue. Two slow
    # steps under a cap of 10 should still overlap.
    handler = _fake_gateway(capability_delays={"slow-a": 0.2, "slow-b": 0.2})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pipeline = _pipeline(
        [
            {"name": "a", "capability": "slow-a", "params": {}},
            {"name": "b", "capability": "slow-b", "params": {}},
        ]
    )

    start = time.monotonic()
    results = await run_pipeline(
        pipeline, "http://gw.test", http_client=client, poll_interval=0.02, max_concurrent_steps=10
    )
    elapsed = time.monotonic() - start

    assert results["a"].status == "ready" and results["b"].status == "ready"
    assert elapsed < 0.35, "serialised would be >= 0.4s"


def test_poll_interval_stays_flat_while_a_job_is_young_then_grows_to_a_ceiling():
    below = POLL_BACKOFF_AFTER_SECONDS - 0.01
    assert _next_poll_interval(0.3, below, max_interval=5.0) == 0.3

    above = POLL_BACKOFF_AFTER_SECONDS
    assert _next_poll_interval(0.3, above, max_interval=5.0) == pytest.approx(0.45)

    # ...and never past the ceiling, however long the job runs.
    assert _next_poll_interval(4.0, 600.0, max_interval=5.0) == 5.0
    assert _next_poll_interval(5.0, 600.0, max_interval=5.0) == 5.0


def test_a_zero_poll_interval_never_starts_growing():
    # Growth is multiplicative on purpose. A caller asking not to wait -
    # most of this suite - must not quietly start waiting mid-run.
    assert _next_poll_interval(0.0, 600.0, max_interval=5.0) == 0.0


@pytest.mark.asyncio
async def test_a_slow_job_is_polled_fewer_times_than_a_flat_interval_would_poll_it():
    # Measured on this mock at a 0.3s base: a 3s job costs 11 polls flat
    # and 8 with backoff. The saving grows with the job -- 30s goes from
    # 101 polls to 15 -- because the interval is at its ceiling by then.
    handler, state = _counting_gateway(duration=3.0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    results = await run_pipeline(
        _wide(1), "http://gw.test", http_client=client, poll_interval=0.3, timeout=30
    )

    assert results["s0"].status == "ready"
    flat_would_be = 3.0 / 0.3
    assert state["polls"] < flat_would_be, f"{state['polls']} polls is no better than flat"


@pytest.mark.asyncio
async def test_a_job_that_finishes_before_the_backoff_threshold_is_polled_at_full_rate():
    # The other half of the trade: backoff must not tax a fast step. This
    # job ends inside POLL_BACKOFF_AFTER_SECONDS, so every poll should still
    # be one base interval apart.
    duration, interval = 0.8, 0.1
    # Absolute, not derived from the constant: a test whose numbers move
    # with the thing under test cannot fail when that thing changes. This
    # assert states the precondition instead, and is the first thing to go
    # red if the threshold is ever lowered under it.
    assert POLL_BACKOFF_AFTER_SECONDS > duration
    handler, state = _counting_gateway(duration=duration)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await run_pipeline(
        _wide(1), "http://gw.test", http_client=client, poll_interval=interval, timeout=30
    )

    # Flat spacing gives 0.8 / 0.1 = 8 polls; allow one for scheduling
    # slop. Backing off from the first poll would land on ~5.
    assert state["polls"] >= 7


@pytest.mark.asyncio
async def test_a_long_poll_interval_does_not_push_the_timeout_out():
    # Pre-existing bug, fixed alongside the backoff: the sleep between polls
    # was unconditional, so `--timeout 0.4 --poll-interval 5` reported its
    # timeout five seconds late. The last sleep is now clipped to whatever
    # is left of the deadline.
    handler, _ = _counting_gateway(ticks=10_000)  # never becomes ready
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    start = time.monotonic()
    with pytest.raises(PipelineRunError, match="did not finish within"):
        await run_pipeline(
            _wide(1), "http://gw.test", http_client=client, poll_interval=5.0, timeout=0.4
        )
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"timed out {elapsed:.1f}s after a 0.4s deadline"
