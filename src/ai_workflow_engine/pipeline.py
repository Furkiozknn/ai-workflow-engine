"""Pipeline definition: parse a YAML file into a validated DAG of steps.

A pipeline is a list of named steps, each submitting one job to an
ai-job-gateway-compatible server. A step's params can reference an
earlier step's result via Jinja2 (``{{ steps.generate.result.output }}``)
-- the dependency that reference implies must also be declared explicitly
in that step's ``depends_on``, so the DAG's shape is always visible by
reading the YAML, not inferred by scanning template strings.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .templating import find_step_references, find_var_references


class PipelineError(Exception):
    pass


class _NoAliasSafeLoader(yaml.SafeLoader):
    """A SafeLoader that refuses YAML anchors/aliases (``&anchor`` / ``*alias``).

    An alias makes the *parsed* object graph share references instead of
    duplicating them, so a nested-anchor file (the "billion laughs" pattern,
    applied to YAML instead of XML) parses in milliseconds -- but this
    project has no de-duplicating walk anywhere downstream: Jinja2
    rendering and ``json.dumps`` (for `awe run`'s output) both walk a
    params tree at full depth with no awareness that two branches are the
    same object, so they reproduce the full exponential blowup the instant
    they touch it. A handful of nested anchors (a few hundred bytes of
    YAML) is enough to make either one hang or exhaust memory. Pipeline
    files have no legitimate need for anchor reuse, so this closes the
    hole outright instead of trying to bound the resulting size after the
    fact (which would already be too late for the reference-sharing case).
    """

    def compose_node(self, parent, index):  # noqa: D102 - see class docstring
        if self.check_event(yaml.events.AliasEvent):
            event = self.get_event()
            raise PipelineError(
                f"pipeline YAML uses a YAML anchor/alias reference (*{event.anchor}) "
                f"at line {event.start_mark.line + 1}; anchors/aliases are not supported "
                "in pipeline files (a small file using them can expand into an "
                "exponentially large structure once rendered or printed) -- write "
                "the value out in full instead"
            )
        return super().compose_node(parent, index)


@dataclass
class Step:
    name: str
    capability: str
    params: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Pipeline:
    name: str
    steps: list[Step]

    def step_names(self) -> set[str]:
        return {step.name for step in self.steps}

    def step(self, name: str) -> Step:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)


def load_pipeline(path: str | Path) -> Pipeline:
    """Load and validate a pipeline from a YAML file."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError as exc:
        raise PipelineError(f"no such file: {path}") from exc
    return parse_pipeline_str(text)


def parse_pipeline_str(text: str) -> Pipeline:
    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)
    except yaml.YAMLError as exc:
        raise PipelineError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError("pipeline file must contain a YAML mapping at the top level")
    return parse_pipeline(data)


def parse_pipeline(data: dict[str, Any]) -> Pipeline:
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise PipelineError("pipeline must have a non-empty string 'name'")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PipelineError("pipeline must have a non-empty 'steps' list")

    steps: list[Step] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise PipelineError(f"steps[{i}] must be a mapping")

        step_name = raw.get("name")
        if not isinstance(step_name, str) or not step_name:
            raise PipelineError(f"steps[{i}] must have a non-empty string 'name'")
        if step_name in seen_names:
            raise PipelineError(f"duplicate step name: {step_name!r}")
        seen_names.add(step_name)

        capability = raw.get("capability")
        if not isinstance(capability, str) or not capability:
            raise PipelineError(f"step {step_name!r} must have a non-empty string 'capability'")

        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise PipelineError(f"step {step_name!r}: 'params' must be a mapping")

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise PipelineError(f"step {step_name!r}: 'depends_on' must be a list of step names")

        steps.append(Step(name=step_name, capability=capability, params=params, depends_on=list(depends_on)))

    known = {s.name for s in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep not in known:
                raise PipelineError(f"step {step.name!r} depends on unknown step {dep!r}")
            if dep == step.name:
                raise PipelineError(f"step {step.name!r} cannot depend on itself")

    _check_step_references(steps, known)

    pipeline = Pipeline(name=name, steps=steps)
    _check_acyclic(pipeline)
    return pipeline


def _describe_unknown_step(name: str, known: list[str]) -> str:
    suggestions = difflib.get_close_matches(name, known, n=1)
    if suggestions:
        return f"{name!r} (did you mean {suggestions[0]!r}?)"
    return repr(name)


def _check_step_references(steps: list[Step], known: set[str]) -> None:
    """Enforce, at load time, the invariant this project's design already
    depends on but never used to check: a step referencing
    ``steps.<name>...`` in its params must list ``<name>`` in its own
    ``depends_on``. Without this, a forgotten ``depends_on`` entry doesn't
    fail loudly and consistently -- it either works by luck (the
    referenced step happens to land in an earlier layer anyway because of
    *other* declared deps) or fails with a StrictUndefined error midway
    through a real run, wasting whatever the run already did. Catching it
    here turns a flaky, order-dependent runtime bug into an immediate,
    actionable load-time error.
    """
    known_list = sorted(known)
    for step in steps:
        referenced = find_step_references(step.params)

        if step.name in referenced:
            raise PipelineError(
                f"step {step.name!r} references itself via 'steps.{step.name}...', "
                "which can never be defined (a step cannot depend on its own result)"
            )

        unknown_refs = referenced - known
        if unknown_refs:
            described = ", ".join(_describe_unknown_step(r, known_list) for r in sorted(unknown_refs))
            raise PipelineError(
                f"step {step.name!r} references unknown step(s) via 'steps.<name>...': {described}"
            )

        missing_deps = sorted(referenced - set(step.depends_on))
        if missing_deps:
            refs_text = ", ".join(f"steps.{dep}..." for dep in missing_deps)
            raise PipelineError(
                f"step {step.name!r} references {refs_text} in its params but does not list "
                f"{missing_deps} in depends_on -- add them so this step is guaranteed to run "
                "after they finish, instead of the order being accidental"
            )


def referenced_variables(pipeline: Pipeline) -> set[str]:
    """Every ``vars.<name>`` referenced anywhere in the pipeline's steps --
    the ``--var`` flags a caller needs to supply for `awe run` to have a
    chance of succeeding. Best-effort static scan (see
    ``templating.find_var_references``); exposed mainly for `awe validate`
    to show upfront, before anyone actually tries to run the pipeline.
    """
    refs: set[str] = set()
    for step in pipeline.steps:
        refs |= find_var_references(step.params)
    return refs


def _check_acyclic(pipeline: Pipeline) -> None:
    """Raise PipelineError if the depends_on graph has a cycle.

    Plain DFS with a recursion-stack set - the graph is expected to be
    small (a handful to a few dozen steps), so this doesn't need to be
    more clever than that.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {step.name: WHITE for step in pipeline.steps}

    def visit(name: str, path: list[str]) -> None:
        color[name] = GRAY
        for dep in pipeline.step(name).depends_on:
            if color[dep] == GRAY:
                cycle = " -> ".join(path + [dep])
                raise PipelineError(f"cycle detected in pipeline dependencies: {cycle}")
            if color[dep] == WHITE:
                visit(dep, path + [dep])
        color[name] = BLACK

    for step in pipeline.steps:
        if color[step.name] == WHITE:
            visit(step.name, [step.name])


def execution_layers(pipeline: Pipeline) -> list[list[str]]:
    """Group step names into ordered layers: every step in layer N depends
    only on steps in layers 0..N-1, so all steps within one layer can run
    concurrently. Assumes the pipeline has already been validated acyclic.
    """
    remaining = {step.name: set(step.depends_on) for step in pipeline.steps}
    layers: list[list[str]] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise PipelineError("cycle detected in pipeline dependencies")
        layers.append(ready)
        for name in ready:
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return layers
