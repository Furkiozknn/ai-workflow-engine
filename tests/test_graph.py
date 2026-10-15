from __future__ import annotations

from pathlib import Path

import pytest

from ai_workflow_engine.cli import main
from ai_workflow_engine.graph import render_graph, to_dot, to_mermaid
from ai_workflow_engine.pipeline import load_pipeline, parse_pipeline

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _pipeline(steps, name="g"):
    return parse_pipeline({"name": name, "steps": steps})


DIAMOND = [
    {"name": "a", "capability": "gen", "params": {"p": "{{ vars.prompt }}"}},
    {"name": "b", "capability": "up", "params": {}, "depends_on": ["a"]},
    {"name": "c", "capability": "inspect", "params": {}, "depends_on": ["a"]},
    {"name": "d", "capability": "merge", "params": {}, "depends_on": ["b", "c"]},
]


def test_mermaid_has_one_node_per_step_and_one_edge_per_dependency():
    text = to_mermaid(_pipeline(DIAMOND))
    lines = text.splitlines()
    assert lines[0] == "flowchart LR"
    nodes = [l for l in lines if '["' in l]
    edges = [l.strip() for l in lines if "-->" in l]
    assert len(nodes) == 4
    # Ids follow execution order: a=s0, b=s1, c=s2, d=s3.
    assert sorted(edges) == ["s0 --> s1", "s0 --> s2", "s1 --> s3", "s2 --> s3"]
    assert 's0["a<br/>(gen)"]' in text
    assert "%% layer 1: b, c" in text


def test_mermaid_mock_chain_example_matches_its_layers():
    text = to_mermaid(load_pipeline(EXAMPLES / "mock-chain.yaml"))
    assert "%% layer 0: draft, note" in text
    assert "%% layer 1: publish" in text
    assert "s0 --> s2" in text and "s1 --> s2" in text


def test_mermaid_escapes_label_text():
    steps = [{"name": 'we"ird <b>#1 & co', "capability": "echo", "params": {}}]
    text = to_mermaid(_pipeline(steps))
    assert 's0["we#quot;ird #lt;b#gt;#35;1 #amp; co<br/>(echo)"]' in text
    # Nothing in a label can close the quote or open a tag.
    label_line = next(l for l in text.splitlines() if '["' in l)
    inner = label_line.split('["', 1)[1].rsplit('"]', 1)[0]
    assert '"' not in inner and "<b>" not in inner


def test_dot_pins_each_layer_to_one_rank_and_escapes_names():
    text = to_dot(_pipeline(DIAMOND, name='say "hi"'))
    assert text.startswith('digraph "say \\"hi\\"" {')
    assert '{ rank=same; "b"; "c"; }' in text
    assert '"a" -> "b";' in text and '"c" -> "d";' in text
    assert '"a" [label="a\\ngen"];' in text
    assert text.rstrip().endswith("}")
    # Single-step layers get no rank group.
    assert text.count("rank=same") == 1


def test_show_vars_lists_each_steps_variables():
    mermaid = to_mermaid(_pipeline(DIAMOND), show_vars=True)
    dot = to_dot(_pipeline(DIAMOND), show_vars=True)
    assert "a<br/>(gen)<br/>vars: prompt" in mermaid
    assert '"a" [label="a\\ngen\\nvars: prompt"];' in dot
    assert "vars:" not in to_mermaid(_pipeline(DIAMOND))


def test_render_graph_rejects_an_unknown_format():
    with pytest.raises(ValueError):
        render_graph(_pipeline(DIAMOND), "svg")


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["awe", *argv])
    main()


def test_cli_graph_prints_mermaid_by_default(monkeypatch, capsys):
    _run(monkeypatch, ["graph", str(EXAMPLES / "mock-chain.yaml")])
    out = capsys.readouterr().out
    assert out.startswith("flowchart LR\n")


def test_cli_graph_writes_dot_to_a_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "dag.dot"
    _run(
        monkeypatch,
        ["graph", str(EXAMPLES / "local-media-chain.yaml"), "--format", "dot", "-o", str(target)],
    )
    assert capsys.readouterr().out == ""
    text = target.read_text(encoding="utf-8")
    assert '"resize" -> "optimize";' in text
    assert '{ rank=same; "inspect"; "resize"; }' in text


def test_cli_graph_refuses_an_invalid_pipeline(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nsteps:\n  - name: a\n    capability: echo\n    depends_on: [b]\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, ["graph", str(bad)])
    assert exc_info.value.code == 1
    assert "unknown step" in capsys.readouterr().err
