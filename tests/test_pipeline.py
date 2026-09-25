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


# --- load_pipeline: every way a file can fail to load is a PipelineError ---


def test_load_pipeline_reads_utf8_regardless_of_locale(tmp_path):
    # Path.read_text() without an encoding uses the locale's: cp1252 on a
    # stock Windows install, ASCII under LC_ALL=C. A prompt with a single
    # non-ASCII letter then failed to load (or loaded as mojibake) depending
    # on the machine, not the file. YAML files are UTF-8 by spec. The locale
    # encoding is fixed at interpreter start-up, hence the subprocess.
    import os
    import subprocess
    import sys

    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_bytes(
        'name: u\nsteps:\n  - name: a\n    capability: echo\n    params: {prompt: "kırmızı ayakkabı"}\n'.encode()
    )
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    code = (
        "import sys; from ai_workflow_engine.pipeline import load_pipeline; "
        "sys.stdout.buffer.write(load_pipeline(sys.argv[1]).steps[0].params['prompt'].encode())"
    )
    proc = subprocess.run([sys.executable, "-c", code, str(pipeline_file)], env=env, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert proc.stdout.decode() == "kırmızı ayakkabı"


def test_load_pipeline_directory_is_a_pipeline_error(tmp_path):
    from ai_workflow_engine.pipeline import load_pipeline

    with pytest.raises(PipelineError, match="cannot read"):
        load_pipeline(tmp_path)


def test_load_pipeline_non_utf8_file_is_a_pipeline_error(tmp_path):
    from ai_workflow_engine.pipeline import load_pipeline

    pipeline_file = tmp_path / "p.yaml"
    pipeline_file.write_bytes(b"\xff\xfe\x00not yaml")
    with pytest.raises(PipelineError, match="UTF-8"):
        load_pipeline(pipeline_file)


def test_pathologically_nested_yaml_is_a_pipeline_error():
    # PyYAML's composer recurses once per nesting level; a few kilobytes of
    # '[' used to escape as a bare RecursionError traceback.
    text = "name: d\nsteps:\n  - name: a\n    capability: x\n    params: {k: " + "[" * 5000 + "]" * 5000 + "}\n"
    with pytest.raises(PipelineError, match="nested too deeply"):
        parse_pipeline_str(text)


def test_long_dependency_chain_listed_in_reverse_does_not_hit_the_recursion_limit():
    # The cycle check was a recursive DFS: a chain longer than Python's
    # recursion limit, listed last-step-first, crashed with RecursionError
    # instead of validating.
    n = 3000
    steps = [
        {"name": f"s{i}", "capability": "x", **({"depends_on": [f"s{i - 1}"]} if i else {})}
        for i in reversed(range(n))
    ]
    pipeline = parse_pipeline(_minimal(steps))
    assert len(execution_layers(pipeline)) == n


def test_long_cycle_is_still_reported_with_its_path():
    n = 3000
    steps = [{"name": f"s{i}", "capability": "x", "depends_on": [f"s{(i + 1) % n}"]} for i in range(n)]
    with pytest.raises(PipelineError, match=r"cycle detected in pipeline dependencies: s0 -> s1 -> .* -> s0$"):
        parse_pipeline(_minimal(steps))
