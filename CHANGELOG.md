# Changelog

This project follows [Semantic Versioning](https://semver.org/). The version
in `pyproject.toml`, `ai_workflow_engine.__version__` and the git tag must
agree; the release workflow and `tests/test_version.py` check that.

## 0.1.0 — not yet tagged

First release. Everything below is on `main`; no tag or PyPI upload exists yet.

### Pipelines
- A pipeline is a YAML file: named steps, each one `ai-job-gateway` job
  (`capability` + `params`), with explicit `depends_on`.
- Validated at load time: unknown or self dependencies, cycles (reported as
  the path that closes them), duplicate names, a `capability` that is not a
  plain URL-safe token, YAML anchors/aliases, and a `steps.<name>` template
  reference whose step is missing from `depends_on`.
- Pipeline files are read as UTF-8 regardless of locale. A directory, an
  unreadable file, a non-UTF-8 file or pathologically nested YAML is a
  `PipelineError`, not a traceback. The cycle check no longer hits Python's
  recursion limit on long dependency chains.

### Running
- Steps run in dependency layers; a layer's steps run concurrently, capped
  by `--max-concurrent-steps` (default 10), with poll backoff up to
  `--max-poll-interval`.
- Params are rendered with a sandboxed Jinja2 environment and
  `StrictUndefined`.
- `awe run` refuses to start when a `vars.<name>` the pipeline needs was not
  passed with `--var` (names guarded by `| default(...)` / `is defined` are
  optional), instead of failing a later layer after earlier jobs already ran.
- A failing layer stops the run with `PipelineRunError`, keeping earlier
  results on `partial_results`.

### Command line
- `awe run` asks the gateway what it offers before submitting anything: an
  unreachable gateway, or a capability it does not have, is one `error:` line
  and exit 1 with no job sent. A gateway without `/v1/capabilities` skips the
  check. Library: `run_pipeline(..., check_capabilities=True)`, off by default.
- `--help` has a description, examples, exit codes and text for every
  argument; `awe --version`. A `depends_on` typo suggests the real step name;
  `awe validate <directory>` says it is a directory.
- `examples/mock-chain.yaml`: a pipeline for the gateway's built-in
  `mock-generate`/`echo`, no key needed.

### Project
- CI: tests on Python 3.11, 3.12 and 3.13 with a locked install, a package
  build + `twine check`, contract tests against the real `ai-job-gateway`,
  and a check that the vendored `gateway_poll.py` matches its canonical copy.
- Release workflow publishes to PyPI via Trusted Publishing on a `v*` tag.
