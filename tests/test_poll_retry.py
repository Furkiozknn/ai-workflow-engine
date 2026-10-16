"""A transient poll failure is retried a bounded number of times instead of
failing the whole run, and a malformed submission response is reported."""

from __future__ import annotations

import httpx
import pytest

from ai_workflow_engine import runner as runner_module
from ai_workflow_engine.pipeline import parse_pipeline
from ai_workflow_engine.runner import PipelineRunError, run_pipeline


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Keep the real retry count, drop the real waits."""
    count = len(runner_module.poll_retry_delays())
    monkeypatch.setattr(runner_module, "poll_retry_delays", lambda: [0.0] * count)
    return count


def _pipeline():
    return parse_pipeline({"name": "t", "steps": [{"name": "a", "capability": "echo", "params": {"x": 1}}]})


def _gateway(poll_answers):
    """POST accepts; GETs answer from ``poll_answers`` in order (an exception
    instance is raised), then 'ready' forever."""
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "j1", "polling_url": "/v1/jobs/j1"})
        index = gets["n"]
        gets["n"] += 1
        if index < len(poll_answers):
            item = poll_answers[index]
            if isinstance(item, Exception):
                raise item
            return item
        return httpx.Response(200, json={"status": "ready", "result": {"ok": True}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), gets


async def test_one_dropped_connection_no_longer_fails_the_run():
    client, gets = _gateway([httpx.ConnectError("connection reset")])
    results = await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0)
    assert results["a"].status == "ready"
    assert gets["n"] == 2


async def test_a_502_then_a_504_are_retried():
    client, gets = _gateway([httpx.Response(502), httpx.Response(504)])
    results = await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0)
    assert results["a"].result == {"ok": True}
    assert gets["n"] == 3


async def test_retries_are_bounded_and_then_the_step_fails(_fast_retries):
    client, gets = _gateway([httpx.ConnectError("gateway gone")] * (_fast_retries + 1))
    with pytest.raises(PipelineRunError, match="gateway gone"):
        await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0)
    assert gets["n"] == _fast_retries + 1


async def test_a_500_is_not_retried():
    client, gets = _gateway([httpx.Response(500, text="bug")])
    with pytest.raises(PipelineRunError, match="500"):
        await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0)
    assert gets["n"] == 1


async def test_no_retry_sleeps_past_the_step_deadline(monkeypatch):
    # With real waits and a deadline shorter than the first one, the
    # failure is reported at once instead of after the backoff.
    monkeypatch.setattr(runner_module, "poll_retry_delays", lambda: [30.0, 30.0])
    client, gets = _gateway([httpx.Response(503)])
    with pytest.raises(PipelineRunError, match="503"):
        await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0, timeout=1.0)
    assert gets["n"] == 1


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(202, json={"id": "j1"}), "missing 'polling_url'"),
        (httpx.Response(202, text="<html>not json</html>"), "not a JSON object"),
    ],
)
async def test_a_malformed_submission_response_names_the_problem(response, expected):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(PipelineRunError, match=expected):
        await run_pipeline(_pipeline(), "http://gw.test", http_client=client, poll_interval=0)
