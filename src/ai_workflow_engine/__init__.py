"""ai-workflow-engine: a small DAG orchestrator that chains
ai-job-gateway-compatible jobs (generate -> upscale -> lip-sync, ...)
defined as a YAML pipeline.

Public surface::

    from ai_workflow_engine import Pipeline, load_pipeline, run_pipeline
"""

from __future__ import annotations

from .pipeline import Pipeline, PipelineError, Step, load_pipeline, parse_pipeline, referenced_variables
from .runner import PipelineRunError, StepResult, run_pipeline

__all__ = [
    "Pipeline",
    "PipelineError",
    "Step",
    "load_pipeline",
    "parse_pipeline",
    "referenced_variables",
    "PipelineRunError",
    "StepResult",
    "run_pipeline",
]

__version__ = "0.1.0"
