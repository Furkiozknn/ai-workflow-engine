"""Pipeline definition: parse a YAML file into a validated DAG of steps.

A pipeline is a list of named steps, each submitting one job to an
ai-job-gateway-compatible server. A step's params can reference an
earlier step's result via Jinja2 (``{{ steps.generate.result.output }}``)
-- the dependency that reference implies must also be declared explicitly
in that step's ``depends_on``, so the DAG's shape is always visible by
reading the YAML, not inferred by scanning template strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# gateway_poll.submit_url builds the request as f"{base_url}/v1/{capability}"
# with no encoding or escaping - an unrestricted capability string is a
# path/query injection into that request. Confirmed exploitable:
# "../admin/delete-all" escapes the /v1/ namespace entirely via dot-segment
# normalization, and "images?admin=true" injects arbitrary query params.
# Restricting to this shape closes that off before it ever reaches submit_url.
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class PipelineError(Exception):
    pass


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
        data = yaml.safe_load(text)
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
        if not _CAPABILITY_RE.match(capability):
            raise PipelineError(
                f"step {step_name!r}: 'capability' {capability!r} must match {_CAPABILITY_RE.pattern} "
                "(it becomes a URL path segment - no '/', '?', '.', or whitespace allowed)"
            )

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

    pipeline = Pipeline(name=name, steps=steps)
    _check_acyclic(pipeline)
    return pipeline


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
