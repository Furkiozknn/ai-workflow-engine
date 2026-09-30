"""ai-workflow-engine: a small DAG orchestrator that chains
ai-job-gateway-compatible jobs (generate -> upscale -> lip-sync, ...)
defined as a YAML pipeline.

Public surface::

    import asyncio
    from ai_workflow_engine import load_pipeline, run_pipeline

    pipeline = load_pipeline("examples/mock-chain.yaml")   # raises PipelineError on a bad file
    results = asyncio.run(
        run_pipeline(pipeline, "http://127.0.0.1:8000", variables={"prompt": "a cat"})
    )                                                       # raises PipelineRunError if a step fails
    print(results["publish"].result)

``results`` maps step name to :class:`StepResult` (``status``, ``result``,
``error``). The command line is ``awe validate|run`` (see ``awe --help``).
"""

from __future__ import annotations

from .pipeline import Pipeline, PipelineError, Step, load_pipeline, parse_pipeline, referenced_variables
from .runner import GatewayCheckError, PipelineRunError, StepResult, check_gateway, run_pipeline

__all__ = [
    "Pipeline",
    "PipelineError",
    "Step",
    "load_pipeline",
    "parse_pipeline",
    "referenced_variables",
    "GatewayCheckError",
    "PipelineRunError",
    "check_gateway",
    "StepResult",
    "run_pipeline",
]

__version__ = "0.1.0"
