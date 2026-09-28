"""The version lives in two places; the release workflow only checks one."""

from __future__ import annotations

import tomllib
from pathlib import Path

import ai_workflow_engine


def test_dunder_version_matches_pyproject():
    # yayinla.yml compares the tag against pyproject.toml. __version__ is a
    # second, hand-maintained copy that nothing else compares, so a bump
    # that forgets it would ship a wheel reporting the old version.
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert ai_workflow_engine.__version__ == pyproject["project"]["version"]
