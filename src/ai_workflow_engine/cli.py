"""Command-line entry point: `awe validate|run`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .pipeline import (
    PipelineError,
    execution_layers,
    load_pipeline,
    referenced_variables,
    required_variables,
)
from . import __version__
from .runner import (
    DEFAULT_MAX_CONCURRENT_STEPS,
    GatewayCheckError,
    DEFAULT_MAX_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    PipelineRunError,
    run_pipeline,
)


def _parse_var(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--var must be KEY=VALUE, got {raw!r}")
    key, _, value = raw.partition("=")
    if not key:
        raise argparse.ArgumentTypeError(f"--var must be KEY=VALUE, got {raw!r}")
    return key, value


def _non_negative_int(raw: str) -> int:
    """argparse type for a count that may be zero (meaning "no limit") but
    never negative -- asyncio.Semaphore(-1) raises a ValueError that would
    otherwise reach the user as a traceback."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 (no limit) or more, got {value}")
    return value


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
    var_refs = referenced_variables(pipeline)
    if var_refs:
        print(f"  variables referenced: {', '.join(sorted(var_refs))}")


def _cmd_run(args: argparse.Namespace) -> None:
    try:
        pipeline = load_pipeline(args.file)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    variables = dict(args.var or [])

    # Checked before anything is submitted: without this, a variable only a
    # later layer uses fails that layer's template after the earlier layers
    # have already run -- jobs submitted, time and quota spent.
    missing = sorted(required_variables(pipeline) - variables.keys())
    if missing:
        flags = " ".join(f"--var {name}=..." for name in missing)
        print(f"error: pipeline needs variable(s) not supplied: {', '.join(missing)} (pass {flags})", file=sys.stderr)
        raise SystemExit(1)

    async def _go() -> dict:
        return await run_pipeline(
            pipeline,
            args.gateway_url,
            variables=variables,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            max_poll_interval=args.max_poll_interval,
            max_concurrent_steps=args.max_concurrent_steps or None,
            check_capabilities=True,
        )

    try:
        results = asyncio.run(_go())
    except GatewayCheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
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
    parser = argparse.ArgumentParser(
        prog="awe",
        description=(
            "Run a chain of ai-job-gateway jobs described in a YAML file.\n"
            "The file is a DAG: it is checked in full (typos, cycles, undeclared\n"
            "step references) before any job is submitted."
        ),
        epilog=(
            "examples:\n"
            "  awe validate examples/mock-chain.yaml\n"
            "  awe run examples/mock-chain.yaml --gateway-url http://127.0.0.1:8000 --var prompt='a cat'\n"
            "\n"
            "'run' needs an ai-job-gateway server; 'validate' needs nothing.\n"
            "Exit code: 0 ok, 1 the pipeline or a step failed, 2 bad command line."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"awe {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="{validate,run}")

    validate_parser = subparsers.add_parser(
        "validate",
        help="check a pipeline file and print its execution layers (runs nothing)",
        description="Check a pipeline file: YAML, DAG, depends_on and template references. Runs nothing, needs no gateway.",
    )
    validate_parser.add_argument("file", help="path to the pipeline YAML file")
    validate_parser.set_defaults(func=_cmd_validate)

    run_parser = subparsers.add_parser(
        "run",
        help="run a pipeline against an ai-job-gateway server",
        description=(
            "Run a pipeline: submit each step to the gateway, layer by layer, and print every step's "
            "{status, result, error} as JSON on stdout. Before the first job it checks that the "
            "gateway is reachable and offers every capability the pipeline uses."
        ),
    )
    run_parser.add_argument("file", help="path to the pipeline YAML file")
    run_parser.add_argument(
        "--gateway-url",
        required=True,
        metavar="URL",
        help="base URL of the ai-job-gateway server, e.g. http://127.0.0.1:8000",
    )
    run_parser.add_argument(
        "--var",
        action="append",
        type=_parse_var,
        metavar="KEY=VALUE",
        help="value for {{ vars.KEY }} in the pipeline; repeat for several",
    )
    run_parser.add_argument(
        "--timeout", type=float, default=60.0, help="per-step timeout in seconds (default: %(default)s)"
    )
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="first wait between polls of a job, in seconds (default: %(default)s)",
    )
    run_parser.add_argument(
        "--max-poll-interval",
        type=float,
        default=DEFAULT_MAX_POLL_INTERVAL,
        help=(
            "ceiling the poll interval backs off to for a job that keeps "
            "reporting 'processing' (default: %(default)s)"
        ),
    )
    run_parser.add_argument(
        "--max-concurrent-steps",
        type=_non_negative_int,
        default=DEFAULT_MAX_CONCURRENT_STEPS,
        help=(
            "how many of a layer's steps may be in flight at the gateway at "
            "once; 0 submits a whole layer at once (default: %(default)s)"
        ),
    )
    run_parser.set_defaults(func=_cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
