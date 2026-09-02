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


@pytest.mark.parametrize(
    "capability",
    [
        "../admin/delete-all",  # escapes the /v1/ namespace via dot-segment normalization
        "images?admin=true",  # injects a query parameter
        "images/../../secret",
        "with space",
        "trailing/slash/",
        "",
    ],
)
def test_capability_rejects_url_path_injection(capability):
    # capability becomes a URL path segment (POST /v1/{capability}) with no
    # encoding - anything but a plain token must be rejected before it ever
    # reaches the HTTP layer.
    with pytest.raises(PipelineError, match="capability"):
        parse_pipeline(_minimal([{"name": "a", "capability": capability}]))


@pytest.mark.parametrize("capability", ["echo", "image-generate", "image_upscale", "a1", "ABC"])
def test_capability_accepts_plain_tokens(capability):
    pipeline = parse_pipeline(_minimal([{"name": "a", "capability": capability}]))
    assert pipeline.steps[0].capability == capability


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


def test_step_reference_without_matching_depends_on_is_rejected():
    with pytest.raises(PipelineError, match="does not list"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "generate", "capability": "gen", "params": {"prompt": "hi"}},
                    {
                        "name": "upscale",
                        "capability": "up",
                        "params": {"source": "{{ steps.generate.result.output }}"},
                        # missing depends_on: [generate]
                    },
                ]
            )
        )


def test_step_reference_with_matching_depends_on_is_accepted():
    pipeline = parse_pipeline(
        _minimal(
            [
                {"name": "generate", "capability": "gen", "params": {"prompt": "hi"}},
                {
                    "name": "upscale",
                    "capability": "up",
                    "params": {"source": "{{ steps.generate.result.output }}"},
                    "depends_on": ["generate"],
                },
            ]
        )
    )
    assert pipeline.step("upscale").depends_on == ["generate"]


def test_step_reference_to_unknown_step_is_rejected_with_suggestion():
    with pytest.raises(PipelineError, match=r"did you mean 'generate'"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "generate", "capability": "gen", "params": {"prompt": "hi"}},
                    {
                        "name": "upscale",
                        "capability": "up",
                        "params": {"source": "{{ steps.generat.result.output }}"},
                        "depends_on": ["generate"],
                    },
                ]
            )
        )


def test_step_referencing_itself_is_rejected():
    with pytest.raises(PipelineError, match="references itself"):
        parse_pipeline(
            _minimal(
                [
                    {"name": "a", "capability": "echo", "params": {"x": "{{ steps.a.result.y }}"}},
                ]
            )
        )


def test_yaml_anchor_alias_is_rejected():
    src = """
name: bomb
steps:
  - name: a
    capability: echo
    params: &p
      x: [1, 2, 3]
  - name: b
    capability: echo
    params: *p
"""
    with pytest.raises(PipelineError, match="anchor/alias"):
        parse_pipeline_str(src)
