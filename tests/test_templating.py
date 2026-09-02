from __future__ import annotations

import pytest

from ai_workflow_engine.templating import (
    TemplateRenderError,
    find_step_references,
    find_var_references,
    render_params,
)


def test_plain_values_pass_through_unrendered():
    result = render_params({"width": 1024, "ok": True}, steps={}, variables={})
    assert result == {"width": 1024, "ok": True}


def test_string_template_substitutes_from_vars():
    result = render_params({"prompt": "a {{ vars.animal }}"}, steps={}, variables={"animal": "cat"})
    assert result == {"prompt": "a cat"}


def test_string_template_substitutes_from_steps():
    steps = {"generate": {"status": "ready", "result": {"output": "http://example.test/img.png"}}}
    result = render_params({"image_url": "{{ steps.generate.result.output }}"}, steps=steps, variables={})
    assert result == {"image_url": "http://example.test/img.png"}


def test_nested_dict_and_list_values_are_rendered_recursively():
    steps = {"a": {"result": {"x": "42"}}}
    params = {"nested": {"list": ["{{ steps.a.result.x }}", "literal"]}}
    result = render_params(params, steps=steps, variables={})
    assert result == {"nested": {"list": ["42", "literal"]}}


def test_reference_to_unfinished_step_raises_template_render_error():
    with pytest.raises(TemplateRenderError):
        render_params({"x": "{{ steps.nope.result.output }}"}, steps={}, variables={})


def test_reference_to_unknown_variable_raises_template_render_error():
    with pytest.raises(TemplateRenderError):
        render_params({"x": "{{ vars.nope }}"}, steps={}, variables={})


def test_malformed_template_syntax_raises_template_render_error():
    # A bad Jinja2 syntax error (unclosed '{{') used to escape as a raw
    # jinja2.TemplateSyntaxError instead of the documented TemplateRenderError -
    # see templating._render_value's broad except.
    with pytest.raises(TemplateRenderError):
        render_params({"x": "{{ vars.missing"}, steps={}, variables={})


def test_sandboxed_environment_blocks_unsafe_attribute_access():
    steps = {"a": {"result": {"x": "hi"}}}
    with pytest.raises(TemplateRenderError, match="unsafe operation"):
        render_params({"x": "{{ steps.a.result.x.__class__.__mro__ }}"}, steps=steps, variables={})


def test_find_step_references_extracts_step_names():
    params = {
        "source": "{{ steps.generate.result.output }}",
        "other": "{{ steps.caption.result.text }} and {{ steps.generate.result.output }}",
        "literal": "no refs here",
    }
    assert find_step_references(params) == {"generate", "caption"}


def test_find_step_references_empty_when_no_step_refs():
    assert find_step_references({"prompt": "{{ vars.subject }}"}) == set()


def test_find_var_references_extracts_var_names():
    params = {"prompt": "a {{ vars.subject }} on {{ vars.background }}"}
    assert find_var_references(params) == {"subject", "background"}


def test_find_references_walk_nested_dicts_and_lists():
    params = {"outer": {"list": ["{{ steps.a.result.x }}", "{{ vars.y }}"]}}
    assert find_step_references(params) == {"a"}
    assert find_var_references(params) == {"y"}
