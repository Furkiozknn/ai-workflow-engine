from __future__ import annotations

import pytest

from ai_workflow_engine.templating import TemplateRenderError, render_params


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


def test_jinja2_ssti_gadget_chain_is_blocked_by_sandboxing():
    # The standard Jinja2 SSTI payload: reach __class__/__mro__/__subclasses__
    # off any object in scope to walk to arbitrary Python classes. Under a
    # plain jinja2.Environment this executes; SandboxedEnvironment must
    # refuse attribute access on dunder names instead.
    payload = "{{ vars.animal.__class__.__mro__[1].__subclasses__() }}"
    with pytest.raises(TemplateRenderError):
        render_params({"x": payload}, steps={}, variables={"animal": "cat"})


def test_malformed_template_syntax_raises_template_render_error():
    # A bad Jinja2 syntax error (unclosed '{{') used to escape as a raw
    # jinja2.TemplateSyntaxError instead of the documented TemplateRenderError -
    # see templating._render_value's broad except.
    with pytest.raises(TemplateRenderError):
        render_params({"x": "{{ vars.missing"}, steps={}, variables={})
