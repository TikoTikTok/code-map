"""Shared fixtures: script paths, a subprocess runner, and a private cache folder per test session."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "code-map" / "scripts"
FIXTURE = ROOT / "tests" / "fixture_repo"
LANGS = ROOT / "tests" / "fixture_langs"
EXPECTED = ROOT / "tests" / "expected"


@pytest.fixture(scope="session", autouse=True)
def private_cache(tmp_path_factory):
    """Never touch the user's real %LOCALAPPDATA%/code-map cache from the tests."""
    d = tmp_path_factory.mktemp("code-map-cache")
    old = os.environ.get("CODE_MAP_CACHE_DIR")
    os.environ["CODE_MAP_CACHE_DIR"] = str(d)
    yield d
    if old is None:
        os.environ.pop("CODE_MAP_CACHE_DIR", None)
    else:
        os.environ["CODE_MAP_CACHE_DIR"] = old


def run(script: str, *args: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True,
                          encoding="utf-8", env=full_env, cwd=str(cwd or ROOT))


def expected(name: str) -> str:
    return (EXPECTED / name).read_text(encoding="utf-8")


def fixture_args(*rest: str) -> list[str]:
    return [str(FIXTURE), *rest]
