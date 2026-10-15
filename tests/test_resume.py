"""--state / --resume: record a run, then re-run only what failed or changed."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from ai_workflow_engine.cli import main
from ai_workflow_engine.pipeline import parse_pipeline
from ai_workflow_engine.runner import (
    STATE_VERSION,
    PipelineRunError,
    StateFileError,
    load_state,
    run_pipeline,
    step_fingerprint,
)


class FakeGateway:
    """Every job is ready on the first poll; ``failing`` capabilities error.
    ``submitted`` counts POSTs per capability."""

    def __init__(self, *, failing: set[str] | None = None, results: dict | None = None):
        self.failing = set(failing or ())
        self.results = results or {}
        self.submitted: Counter[str] = Counter()
        self.jobs: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/capabilities":
            return httpx.Response(404)
        if request.method == "POST":
            capability = request.url.path.removeprefix("/v1/")
            self.submitted[capability] += 1
            job_id = f"job-{sum(self.submitted.values())}"
            self.jobs[job_id] = {"capability": capability, "params": json.loads(request.content)}
            return httpx.Response(202, json={"id": job_id, "polling_url": f"/v1/jobs/{job_id}"})
        job = self.jobs[request.url.path.rsplit("/", 1)[-1]]
        if job["capability"] in self.failing:
            return httpx.Response(200, json={"status": "error", "error": "boom"})
        result = self.results.get(job["capability"], {"echoed": job["params"]})
        return httpx.Response(200, json={"status": "ready", "result": result})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


CHAIN = {
    "name": "chain",
    "steps": [
        {"name": "gen", "capability": "gen", "params": {"prompt": "{{ vars.prompt }}"}},
        {"name": "side", "capability": "side", "params": {"x": 1}},
        {
            "name": "up",
            "capability": "up",
            "params": {"src": "{{ steps.gen.result.url }}"},
            "depends_on": ["gen"],
        },
    ],
}


def _pipeline(data=CHAIN):
    return parse_pipeline(data)


async def _run(gw: FakeGateway, state: Path, *, resume: bool, prompt: str = "a cat"):
    return await run_pipeline(
        _pipeline(),
        "http://gw.test",
        variables={"prompt": prompt},
        http_client=gw.client(),
        poll_interval=0,
        state_path=state,
        resume=resume,
    )


async def test_a_failed_run_resumes_from_the_failed_step(tmp_path):
    state = tmp_path / "run.json"
    first = FakeGateway(failing={"up"}, results={"gen": {"url": "u1"}})
    with pytest.raises(PipelineRunError):
        await _run(first, state, resume=False)
    recorded = json.loads(state.read_text(encoding="utf-8"))
    assert recorded["awe_state"] == STATE_VERSION
    assert recorded["pipeline"] == "chain"
    assert recorded["steps"]["gen"]["status"] == "ready"
    assert recorded["steps"]["up"]["status"] == "error"

    second = FakeGateway(results={"gen": {"url": "u2"}})
    results = await _run(second, state, resume=True)

    assert second.submitted == Counter({"up": 1})  # gen and side were not resubmitted
    assert results["gen"].reused and results["side"].reused
    assert not results["up"].reused
    # The reused upstream result is what the re-run step rendered from.
    assert results["up"].result == {"echoed": {"src": "u1"}}
    assert json.loads(state.read_text(encoding="utf-8"))["steps"]["up"]["status"] == "ready"


async def test_a_changed_variable_reruns_the_step_and_everything_it_feeds(tmp_path):
    state = tmp_path / "run.json"
    gw = FakeGateway(results={"gen": {"url": "u-cat"}})
    await _run(gw, state, resume=False, prompt="a cat")

    gw2 = FakeGateway(results={"gen": {"url": "u-dog"}})
    results = await _run(gw2, state, resume=True, prompt="a dog")

    # gen's params changed, so it ran; its new result changed up's params,
    # so up ran too; side did not change and was reused.
    assert gw2.submitted == Counter({"gen": 1, "up": 1})
    assert results["side"].reused
    assert results["up"].result == {"echoed": {"src": "u-dog"}}


async def test_an_upstream_rerun_with_the_same_result_lets_downstream_be_reused(tmp_path):
    state = tmp_path / "run.json"
    await _run(FakeGateway(results={"gen": {"url": "same"}}), state, resume=False, prompt="a")
    gw = FakeGateway(results={"gen": {"url": "same"}})
    results = await _run(gw, state, resume=True, prompt="b")
    # gen's params changed so it ran, but it produced the same url, so up
    # would submit exactly what it submitted before: reused.
    assert gw.submitted == Counter({"gen": 1})
    assert results["up"].reused and not results["gen"].reused


async def test_without_resume_the_state_file_is_rewritten_and_nothing_is_reused(tmp_path):
    state = tmp_path / "run.json"
    await _run(FakeGateway(results={"gen": {"url": "u"}}), state, resume=False)
    gw = FakeGateway(results={"gen": {"url": "u"}})
    results = await _run(gw, state, resume=False)
    assert gw.submitted == Counter({"gen": 1, "side": 1, "up": 1})
    assert not any(r.reused for r in results.values())


async def test_resume_with_no_state_file_yet_is_a_fresh_run(tmp_path):
    state = tmp_path / "new.json"
    gw = FakeGateway(results={"gen": {"url": "u"}})
    await _run(gw, state, resume=True)
    assert sum(gw.submitted.values()) == 3
    assert set(json.loads(state.read_text(encoding="utf-8"))["steps"]) == {"gen", "side", "up"}


async def test_a_state_file_for_another_pipeline_is_refused_before_any_job(tmp_path):
    state = tmp_path / "run.json"
    state.write_text(
        json.dumps({"awe_state": STATE_VERSION, "pipeline": "other", "steps": {}}), encoding="utf-8"
    )
    gw = FakeGateway()
    with pytest.raises(StateFileError, match="written for pipeline 'other'"):
        await _run(gw, state, resume=True)
    assert not gw.submitted


@pytest.mark.parametrize(
    "content, message",
    [
        ("not json", "not valid JSON"),
        (json.dumps({"awe_state": 99, "pipeline": "chain", "steps": {}}), "not an awe state file"),
        (json.dumps({"awe_state": STATE_VERSION, "pipeline": "chain"}), "no 'steps'"),
    ],
)
def test_unusable_state_files_raise_state_file_error(tmp_path, content, message):
    state = tmp_path / "run.json"
    state.write_text(content, encoding="utf-8")
    with pytest.raises(StateFileError, match=message):
        load_state(state, _pipeline())


def test_steps_no_longer_in_the_pipeline_are_dropped_on_load(tmp_path):
    state = tmp_path / "run.json"
    state.write_text(
        json.dumps(
            {
                "awe_state": STATE_VERSION,
                "pipeline": "chain",
                "steps": {"gen": {"status": "ready"}, "removed": {"status": "ready"}},
            }
        ),
        encoding="utf-8",
    )
    assert set(load_state(state, _pipeline())) == {"gen"}


def test_fingerprint_ignores_key_order_and_covers_capability():
    a = step_fingerprint("gen", {"a": 1, "b": [1, 2]})
    assert a == step_fingerprint("gen", {"b": [1, 2], "a": 1})
    assert a != step_fingerprint("other", {"a": 1, "b": [1, 2]})
    assert a != step_fingerprint("gen", {"a": 2, "b": [1, 2]})


async def test_resume_without_a_state_path_is_a_value_error():
    with pytest.raises(ValueError):
        await run_pipeline(_pipeline(), "http://gw.test", resume=True)


async def test_an_unwritable_state_path_fails_before_any_job(tmp_path):
    gw = FakeGateway()
    with pytest.raises(StateFileError, match="cannot write state file"):
        await _run(gw, tmp_path / "missing-dir" / "run.json", resume=False)
    assert not gw.submitted


# --- CLI ---------------------------------------------------------------------

PIPELINE_YAML = """
name: cli-resume
steps:
  - name: a
    capability: ok
    params: {x: 1}
  - name: b
    capability: flaky
    params: {y: "{{ steps.a.result.echoed.x }}"}
    depends_on: [a]
"""


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _cli(monkeypatch, argv, gw: FakeGateway):
    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(gw.handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr("ai_workflow_engine.runner.httpx.AsyncClient", patched)
    monkeypatch.setattr("sys.argv", ["awe", *argv])
    main()


def test_cli_state_and_resume_round_trip(tmp_path, monkeypatch, capsys):
    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_text(PIPELINE_YAML, encoding="utf-8")
    state = tmp_path / "run.json"
    base = ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--poll-interval", "0"]

    with pytest.raises(SystemExit) as exc_info:
        _cli(monkeypatch, [*base, "--state", str(state)], FakeGateway(failing={"flaky"}))
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "[OK] a" in err and "[FAIL] b" in err
    assert f"--state {state} --resume" in err

    gw = FakeGateway()
    _cli(monkeypatch, [*base, "--state", str(state), "--resume"], gw)
    captured = capsys.readouterr()
    assert gw.submitted == Counter({"flaky": 1})
    assert "reused 1 step(s)" in captured.err and ": a" in captured.err
    output = json.loads(captured.out)
    assert output["b"]["status"] == "ready"
    assert set(output["a"]) == {"status", "result", "error"}  # stdout shape unchanged


def test_cli_resume_without_state_is_a_usage_error(tmp_path, monkeypatch, capsys):
    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_text(PIPELINE_YAML, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["awe", "run", str(pipeline_file), "--gateway-url", "http://gw.test", "--resume"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "--resume needs --state" in capsys.readouterr().err


def test_cli_reports_a_state_file_for_another_pipeline(tmp_path, monkeypatch, capsys):
    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_text(PIPELINE_YAML, encoding="utf-8")
    state = tmp_path / "run.json"
    state.write_text(json.dumps({"awe_state": STATE_VERSION, "pipeline": "x", "steps": {}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        _cli(
            monkeypatch,
            ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--state", str(state), "--resume"],
            FakeGateway(),
        )
    assert exc_info.value.code == 1
    assert "written for pipeline 'x'" in capsys.readouterr().err
