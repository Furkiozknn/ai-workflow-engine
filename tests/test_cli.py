from __future__ import annotations

from pathlib import Path

import pytest

from ai_workflow_engine.cli import main

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


def test_var_flag_parsing_rejects_missing_equals(tmp_path: Path, monkeypatch, capsys):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(PIPELINE_YAML)

    with pytest.raises(SystemExit):
        _run(monkeypatch, ["run", str(pipeline_file), "--gateway-url", "http://gw.test", "--var", "no-equals-sign"])
