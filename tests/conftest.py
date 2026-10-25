"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / "src", ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from nostalgia.catalog import Catalog  # noqa: E402
from nostalgia.config import load_config  # noqa: E402
from nostalgia.paths import Paths  # noqa: E402
from nostalgia.pipeline import scan  # noqa: E402

from fixtures import FixtureLibrary, build_library  # noqa: E402


@pytest.fixture()
def library(tmp_path: Path) -> FixtureLibrary:
    return build_library(tmp_path)


@pytest.fixture()
def paths(library: FixtureLibrary) -> Paths:
    resolved = Paths(library.root)
    resolved.ensure_runtime_dirs()
    return resolved


@pytest.fixture()
def config(library: FixtureLibrary):
    return load_config(library.config)


@pytest.fixture()
def catalog(config, paths: Paths):
    with Catalog(paths.catalog) as instance:
        scan(config, instance, paths)
        yield instance
