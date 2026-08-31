from __future__ import annotations

import pytest

from ai_workflow_engine.pipeline import PipelineError, execution_layers, parse_pipeline, parse_pipeline_str


def _minimal(steps):
    return {"name": "test-pipeline", "steps": steps}


def test_parses_minimal_pipeline():
    pipeline = parse_pipeline(_minimal([{"name": "a", "capability": "echo", "params": {"x": 1}}]))
    assert pipeline.name == "test-pipeline"
    assert len(pipeline.steps) == 1
    assert pipeline.steps[0].depends_on == []


def test_missing_name_rejected():
    with pytest.raises(PipelineError, match="name"):
        parse_pipeline({"steps": [{"name": "a", "capability": "echo"}]})


def test_missing_steps_rejected():
    with pytest.raises(PipelineError, match="steps"):
        parse_pipeline({"name": "p"})


def test_empty_steps_rejected():
    with pytest.raises(PipelineError, match="steps"):
        parse_pipeline({"name": "p", "steps": []})


def test_step_missing_capability_rejected():
    with pytest.raises(PipelineError, match="capability"):
        parse_pipeline(_minimal([{"name": "a"}]))


def test_duplicate_step_name_rejected():
    with pytest.raises(PipelineError, match="duplicate"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "a", "capability": "echo"},
                    {"name": "a", "capability": "echo"},
                ]
            )
        )


def test_depends_on_unknown_step_rejected():
    with pytest.raises(PipelineError, match="unknown step"):
        parse_pipeline(_minimal([{"name": "a", "capability": "echo", "depends_on": ["nope"]}]))


def test_self_dependency_rejected():
    with pytest.raises(PipelineError, match="itself"):
        parse_pipeline(_minimal([{"name": "a", "capability": "echo", "depends_on": ["a"]}]))


def test_direct_cycle_rejected():
    with pytest.raises(PipelineError, match="cycle"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "a", "capability": "echo", "depends_on": ["b"]},
                    {"name": "b", "capability": "echo", "depends_on": ["a"]},
                ]
            )
        )


def test_longer_cycle_rejected():
    with pytest.raises(PipelineError, match="cycle"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "a", "capability": "echo", "depends_on": ["c"]},
                    {"name": "b", "capability": "echo", "depends_on": ["a"]},
                    {"name": "c", "capability": "echo", "depends_on": ["b"]},
                ]
            )
        )


def test_invalid_yaml_raises_pipeline_error():
    with pytest.raises(PipelineError, match="invalid YAML"):
        parse_pipeline_str("name: [unterminated")


def test_non_mapping_top_level_raises():
    with pytest.raises(PipelineError, match="mapping"):
        parse_pipeline_str("- just\n- a\n- list\n")


def test_execution_layers_linear_chain():
    pipeline = parse_pipeline(
        _minimal(
            [
                {"name": "a", "capability": "echo"},
                {"name": "b", "capability": "echo", "depends_on": ["a"]},
                {"name": "c", "capability": "echo", "depends_on": ["b"]},
            ]
        )
    )
    assert execution_layers(pipeline) == [["a"], ["b"], ["c"]]


def test_execution_layers_parallel_branches_share_a_layer():
    pipeline = parse_pipeline(
        _minimal(
            [
                {"name": "generate", "capability": "img"},
                {"name": "upscale", "capability": "up", "depends_on": ["generate"]},
                {"name": "caption", "capability": "cap", "depends_on": ["generate"]},
                {"name": "merge", "capability": "merge", "depends_on": ["upscale", "caption"]},
            ]
        )
    )
    layers = execution_layers(pipeline)
    assert layers[0] == ["generate"]
    assert set(layers[1]) == {"caption", "upscale"}
    assert layers[2] == ["merge"]


def test_execution_layers_independent_steps_are_all_layer_zero():
    pipeline = parse_pipeline(
        _minimal(
            [
                {"name": "a", "capability": "echo"},
                {"name": "b", "capability": "echo"},
            ]
        )
    )
    assert execution_layers(pipeline) == [["a", "b"]]
