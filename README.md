# ai-workflow-engine

A small DAG orchestrator that chains [`ai-job-gateway`](https://github.com/Furkiozknn/ai-job-gateway)-compatible jobs (generate → upscale → lip-sync, ...) defined as a YAML pipeline. The generalization of ComfyUI's "the graph is a durable, shareable artifact" lesson (see the research in [`Furkiozknn/Furkiozknn`](https://github.com/Furkiozknn/Furkiozknn)'s architecture doc), minus the visual node editor — a pipeline here is a plain YAML file, git-diffable like code.

Part of the same small ecosystem as [`ai-job-gateway`](https://github.com/Furkiozknn/ai-job-gateway), [`prompt-template-manager`](https://github.com/Furkiozknn/prompt-template-manager), [`model-comparison-harness`](https://github.com/Furkiozknn/model-comparison-harness), and [`asset-provenance-toolkit`](https://github.com/Furkiozknn/asset-provenance-toolkit) — coupled only through documented HTTP contracts, never through a shared Python dependency ([ADR-006](https://github.com/Furkiozknn/Furkiozknn/blob/claude/ai-creative-platform-research-fwh2vt/research/lab/DECISIONS.md)). Vendors the same [`gateway_poll.py`](https://github.com/Furkiozknn/Furkiozknn/blob/claude/ai-creative-platform-research-fwh2vt/research/lab/shared/gateway_poll.py) module as the other three ([ADR-008](https://github.com/Furkiozknn/Furkiozknn/blob/claude/ai-creative-platform-research-fwh2vt/research/lab/DECISIONS.md)).

## Why

A single `ai-job-gateway` job is one model call. A real creative pipeline is usually several, chained: generate an image, then upscale it, then run lip-sync on the result. Wiring that by hand each time (submit, poll, copy the URL out of the result, submit the next job) is exactly the kind of repetitive plumbing this tool exists to remove — while staying just a plain YAML file, not a hosted visual builder.

## What it does

- Parses a **pipeline**: a named list of **steps**, each one job submission (`capability` + `params`).
- Validates the pipeline is a genuine DAG at load time: unknown `depends_on` references and cycles are rejected before anything runs, not discovered mid-execution.
- Groups steps into **execution layers**: everything in one layer has no dependency on anything else in that layer, so the whole layer runs **concurrently**. Independent steps never wait on each other just because they happen to be listed one after another in the file.
- Lets a later step's params reference an **earlier step's result** via Jinja2: `{{ steps.generate.result.image_url }}`. A typo'd or not-yet-finished step reference is a loud render-time error (`StrictUndefined`), not a silently empty string.
- Stops the pipeline (raising `PipelineRunError`) as soon as any step in a layer fails, while still surfacing every result already produced by earlier/same-layer steps (`partial_results`) — so you can see exactly how far it got.

## Install

```bash
uv sync --group dev
```

## Pipeline file format

```yaml
name: generate-and-upscale
steps:
  - name: generate
    capability: image-generate
    params:
      prompt: "{{ vars.prompt }}"
      seed: 42

  - name: upscale
    capability: image-upscale
    params:
      source_url: "{{ steps.generate.result.image_url }}"
      scale: 2
    depends_on: [generate]
```

- `steps[].capability` — the `ai-job-gateway` capability to submit the job to (`POST /v1/{capability}`).
- `steps[].params` — the job's params. Any string value is rendered as a Jinja2 template against `vars.*` (CLI `--var KEY=VALUE`) and `steps.<name>.result.*` (any already-finished step in an earlier layer). Non-string values pass through unchanged.
- `steps[].depends_on` — explicit list of step names this step must wait for. **Not inferred from template references** — if a step's params reference `steps.generate...`, that step must also list `generate` in its own `depends_on`. This keeps the DAG's shape visible just by reading the YAML, without needing to parse every template string to know the execution order.

## CLI usage

```bash
# Validate a pipeline's structure and see its execution layers, without running anything
awe validate examples/generate-and-upscale.yaml

# Run it against a live ai-job-gateway server
awe run examples/generate-and-upscale.yaml \
    --gateway-url http://localhost:8000 \
    --var prompt="a red sneaker on a white background"
```

`awe run` prints every step's final `{status, result, error}` as JSON on success. On failure it prints which steps succeeded/failed to stderr and exits non-zero.

## Library usage

```python
import asyncio
from ai_workflow_engine import load_pipeline, run_pipeline

pipeline = load_pipeline("pipeline.yaml")
results = asyncio.run(run_pipeline(pipeline, "http://localhost:8000", variables={"prompt": "a cat"}))
print(results["upscale"].result)
```

## Testing

```bash
uv run pytest -v
```

## Roadmap / known v1 limitations

- No retry policy per step — a failed step fails the whole run. Retries are a natural v2 addition once real (non-mock) providers surface which failures are worth retrying automatically.
- `depends_on` is explicit, not inferred from template references — see above. Auto-inference was considered and deliberately deferred: it's a nicer authoring experience but adds a real risk of a template edit silently changing execution order in a way the YAML doesn't show.
- No persistence — a pipeline run's state lives only in the process that ran `awe run`. There's no resume-from-where-it-failed yet; re-running re-executes every step from scratch.

## License

MIT
