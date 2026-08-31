"""Command-line entry point: `awe validate|run`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .pipeline import PipelineError, execution_layers, load_pipeline
from .runner import PipelineRunError, run_pipeline


def _parse_var(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--var must be KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition("=")
    if not key:
        raise argparse.ArgumentTypeError(f"--var must be KEY=VALUE, got {raw!r}")
    return key, value


def _cmd_validate(args: argparse.Namespace) -> None:
    try:
        pipeline = load_pipeline(args.file)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    layers = execution_layers(pipeline)
    print(f"OK: {pipeline.name!r} - {len(pipeline.steps)} step(s) in {len(layers)} layer(s)")
    for i, layer in enumerate(layers):
        print(f"  layer {i}: {', '.join(layer)}")


def _cmd_run(args: argparse.Namespace) -> None:
    try:
        pipeline = load_pipeline(args.file)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    variables = dict(args.var or [])

    async def _go() -> dict:
        return await run_pipeline(
            pipeline,
            args.gateway_url,
            variables=variables,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )

    try:
        results = asyncio.run(_go())
    except PipelineRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        partial = getattr(exc, "partial_results", {})
        for name, step_result in partial.items():
            status_marker = "OK" if step_result.status == "ready" else "FAIL"
            print(f"  [{status_marker}] {name}", file=sys.stderr)
        raise SystemExit(1)

    output = {
        name: {"status": r.status, "result": r.result, "error": r.error} for name, r in results.items()
    }
    print(json.dumps(output, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="awe", description="ai-workflow-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a pipeline file's structure and DAG")
    validate_parser.add_argument("file")
    validate_parser.set_defaults(func=_cmd_validate)

    run_parser = subparsers.add_parser("run", help="run a pipeline against a gateway server")
    run_parser.add_argument("file")
    run_parser.add_argument("--gateway-url", required=True)
    run_parser.add_argument("--var", action="append", type=_parse_var, metavar="KEY=VALUE")
    run_parser.add_argument("--timeout", type=float, default=60.0, help="per-step timeout in seconds")
    run_parser.add_argument("--poll-interval", type=float, default=0.3)
    run_parser.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
