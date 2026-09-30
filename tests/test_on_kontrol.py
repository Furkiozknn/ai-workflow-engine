"""Pre-flight gateway check, `--help`/`--version`, and the shipped examples."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_workflow_engine import GatewayCheckError, check_gateway, load_pipeline, run_pipeline
from ai_workflow_engine.cli import main
from ai_workflow_engine.pipeline import execution_layers, parse_pipeline

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _pipeline(*capabilities: str):
    return parse_pipeline(
        {
            "name": "t",
            "steps": [{"name": f"s{i}", "capability": c, "params": {}} for i, c in enumerate(capabilities)],
        }
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_unknown_capability_is_reported_before_any_job_is_submitted():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"echo": "echo", "mock-generate": "mock"})

    with pytest.raises(GatewayCheckError) as exc_info:
        await run_pipeline(
            _pipeline("echo", "nope"), "http://gw.test", http_client=_client(handler), check_capabilities=True
        )
    message = str(exc_info.value)
    assert "'nope'" in message
    assert "echo, mock-generate" in message
    assert "/v1/capabilities" in message
    assert seen == [("GET", "/v1/capabilities")]  # nothing was POSTed


async def test_unreachable_gateway_says_where_and_what_to_start():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    with pytest.raises(GatewayCheckError) as exc_info:
        await check_gateway(_pipeline("echo"), "http://127.0.0.1:1", _client(handler))
    message = str(exc_info.value)
    assert "http://127.0.0.1:1" in message
    assert "ai-job-gateway serve" in message


async def test_gateway_without_a_capabilities_endpoint_is_not_an_error():
    for response in (
        httpx.Response(404, json={"detail": "not found"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["echo"]),
    ):
        await check_gateway(_pipeline("anything"), "http://gw.test", _client(lambda r, resp=response: resp))


async def test_check_is_off_by_default_so_library_callers_keep_one_request_per_job():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(202, json={"id": "j1", "polling_url": "/v1/jobs/j1"})
        return httpx.Response(200, json={"status": "ready", "result": {}})

    await run_pipeline(_pipeline("echo"), "http://gw.test", http_client=_client(handler), poll_interval=0)
    assert "/v1/capabilities" not in paths


def test_help_has_examples_and_documents_every_argument(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["awe", "run", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8000" in out
    assert "{{ vars.KEY }}" in out
    monkeypatch.setattr("sys.argv", ["awe", "--help"])
    with pytest.raises(SystemExit):
        main()
    out = capsys.readouterr().out
    assert "examples/mock-chain.yaml" in out
    assert "Exit code" in out


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["awe", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("awe ")


def test_run_turns_a_gateway_check_failure_into_one_error_line(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_text("name: p\nsteps:\n  - name: a\n    capability: echo\n    params: {}\n")

    async def failing(*args, **kwargs):
        raise GatewayCheckError("cannot reach the gateway at http://gw.test")

    monkeypatch.setattr("ai_workflow_engine.cli.run_pipeline", failing)
    monkeypatch.setattr("sys.argv", ["awe", "run", str(pipeline_file), "--gateway-url", "http://gw.test"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: cannot reach the gateway")
    assert "Traceback" not in err


def test_validating_a_directory_names_the_real_problem(tmp_path: Path):
    from ai_workflow_engine import PipelineError

    with pytest.raises(PipelineError, match="it is a directory"):
        load_pipeline(tmp_path)


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.yaml")))
def test_every_shipped_example_is_a_valid_dag(name: str):
    pipeline = load_pipeline(EXAMPLES / name)
    assert execution_layers(pipeline)


def test_mock_chain_example_runs_two_steps_side_by_side_then_joins_them():
    assert execution_layers(load_pipeline(EXAMPLES / "mock-chain.yaml")) == [["draft", "note"], ["publish"]]


def test_a_typo_in_depends_on_suggests_the_real_step_name():
    from ai_workflow_engine import PipelineError

    with pytest.raises(PipelineError, match=r"unknown step 'dratf' \(did you mean 'draft'\?\)"):
        parse_pipeline(
            {
                "name": "t",
                "steps": [
                    {"name": "draft", "capability": "echo", "params": {}},
                    {"name": "publish", "capability": "echo", "params": {}, "depends_on": ["dratf"]},
                ],
            }
        )
