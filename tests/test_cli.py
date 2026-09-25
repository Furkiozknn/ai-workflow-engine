from __future__ import annotations

from pathlib import Path

import pytest

from ai_workflow_engine.cli import main
from ai_workflow_engine.runner import (
    DEFAULT_MAX_CONCURRENT_STEPS,
    DEFAULT_MAX_POLL_INTERVAL,
)

PIPELINE_YAML = """
name: cli-test
steps:
  - name: a
    capability: echo
    params:
      x: 1
  - name: b
    capability: echo
    params:
      x: 2
    depends_on: [a]
"""


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["awe", *argv])
    main()


def test_validate_ok(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)

    _run(monkeypatch, ["validate", str(pipeline_file)])
    out = capsys.readouterr().out
    assert "OK:" in out
    assert "layer 0: a" in out
    assert "layer 1: b" in out


def test_validate_bad_pipeline_exits_nonzero(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text("name: bad\nsteps: []\n")

    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["validate", str(pipeline_file)])
    assert exc_info.value.code == 1
    assert "error:" in capsys.readouterr().err


def test_run_missing_file_exits_nonzero(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["run", "/no/such/file.yaml", "--gateway-url", "http://gw.test"])
    assert exc_info.value.code == 1
    assert "error:" in capsys.readouterr().err


def test_validate_shows_referenced_variables(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(
        """
name: with-vars
steps:
  - name: a
    capability: echo
    params:
      prompt: "{{ vars.subject }}"
"""
    )

    _run(monkeypatch, ["validate", str(pipeline_file)])
    out = capsys.readouterr().out
    assert "variables referenced: subject" in out


def test_validate_omits_variables_line_when_none_referenced(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)

    _run(monkeypatch, ["validate", str(pipeline_file)])
    out = capsys.readouterr().out
    assert "variables referenced" not in out


def test_validate_reports_missing_depends_on_for_step_reference(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(
        """
name: bad
steps:
  - name: generate
    capability: gen
    params:
      prompt: hi
  - name: upscale
    capability: up
    params:
      source: "{{ steps.generate.result.output }}"
"""
    )

    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["validate", str(pipeline_file)])
    assert exc_info.value.code == 1
    assert "does not list" in capsys.readouterr().err


def test_var_flag_parsing_rejects_missing_equals(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)

    with pytest.raises(SystemExit):
        _run(monkeypatch, ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--var", "no-equals-sign"])


def _capture_run_pipeline_kwargs(monkeypatch, tmp_path: Path, extra_argv):
    """Run `awe run` with run_pipeline stubbed out, and return the kwargs it
    was called with. The flags are only worth having if they reach the
    runner; parsing them into a Namespace nobody reads is the failure mode
    this guards."""
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)
    seen = {}

    async def fake_run_pipeline(pipeline, gateway_url, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr("ai_workflow_engine.cli.run_pipeline", fake_run_pipeline)
    _run(monkeypatch, ["run", str(pipeline_file), "--gateway-url", "http://gw.test", *extra_argv])
    return seen


def test_run_passes_pacing_flags_through_to_the_runner(tmp_path: Path, monkeypatch, capsys):
    seen = _capture_run_pipeline_kwargs(
        monkeypatch,
        tmp_path,
        ["--poll-interval", "0.7", "--max-poll-interval", "12", "--max-concurrent-steps", "3"],
    )
    assert seen["poll_interval"] == 0.7
    assert seen["max_poll_interval"] == 12.0
    assert seen["max_concurrent_steps"] == 3


def test_run_defaults_to_the_bounded_pacing(tmp_path: Path, monkeypatch, capsys):
    seen = _capture_run_pipeline_kwargs(monkeypatch, tmp_path, [])
    assert seen["max_concurrent_steps"] == DEFAULT_MAX_CONCURRENT_STEPS
    assert seen["max_poll_interval"] == DEFAULT_MAX_POLL_INTERVAL


def test_max_concurrent_steps_zero_means_unbounded(tmp_path: Path, monkeypatch, capsys):
    # argparse gives 0; the runner's opt-out is None. The CLI has to make
    # that translation or --max-concurrent-steps 0 would cap the layer at
    # zero steps and hang.
    seen = _capture_run_pipeline_kwargs(monkeypatch, tmp_path, ["--max-concurrent-steps", "0"])
    assert seen["max_concurrent_steps"] is None


def test_negative_max_concurrent_steps_is_rejected_by_the_parser(tmp_path: Path, monkeypatch, capsys):
    # asyncio.Semaphore(-1) raises "initial value must be >= 0"; the user
    # should see an argparse message, not that traceback.
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)
    with pytest.raises(SystemExit):
        _run(
            monkeypatch,
            ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--max-concurrent-steps", "-1"],
        )
    assert "0 (no limit) or more" in capsys.readouterr().err


def test_run_with_a_missing_var_fails_before_submitting_anything(tmp_path: Path, monkeypatch, capsys):
    # Layer 0 needs no variable, layer 1 does. Without a pre-flight check
    # the run submitted (and paid for) layer 0, then died on layer 1's
    # template with "'dict object' has no attribute 'image'".
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(
        """
name: late-var
steps:
  - name: generate
    capability: gen
    params:
      prompt: fixed
  - name: caption
    capability: cap
    params:
      source: "{{ steps.generate.result.output }}"
      lang: "{{ vars.lang }}"
      tone: "{{ vars.tone | default('neutral') }}"
    depends_on: [generate]
"""
    )
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(True)
        return {}

    monkeypatch.setattr("ai_workflow_engine.cli.run_pipeline", fake_run_pipeline)
    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["run", str(pipeline_file), "--gateway-url", "http://gw.test"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--var lang=" in err
    assert "tone" not in err
    assert called == []


def test_run_with_every_required_var_supplied_proceeds(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(
        'name: v\nsteps:\n  - name: a\n    capability: echo\n    params: {p: "{{ vars.lang }}"}\n'
    )
    seen = {}

    async def fake_run_pipeline(pipeline, gateway_url, **kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr("ai_workflow_engine.cli.run_pipeline", fake_run_pipeline)
    _run(monkeypatch, ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--var", "lang=tr"])
    assert seen["variables"] == {"lang": "tr"}


def test_validate_a_directory_is_an_error_not_a_traceback(tmp_path: Path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["validate", str(tmp_path)])
    assert exc_info.value.code == 1
    assert "error:" in capsys.readouterr().err
