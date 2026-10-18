"""The scripts must run with no third-party package: Python through stdlib ast, the rest through regex."""
import importlib
import importlib.util
import sys

import pytest

from conftest import FIXTURE, LANGS, SCRIPTS, expected, fixture_args, run

CORE_MODULES = ("codemap_core", "codemap_ts", "codemap_fallback")


@pytest.fixture
def no_tree_sitter(monkeypatch):
    """Make `import tree_sitter_language_pack` fail and load the engine fresh under that condition."""
    for name in CORE_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)
    monkeypatch.delenv("CODE_MAP_ENGINE", raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    core = importlib.import_module("codemap_core")
    assert core.TS is None, "tree-sitter must be unavailable in this fixture"
    yield core
    for name in CORE_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"codemap_{name}_test", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fallback_engine_is_stdlib(no_tree_sitter):
    core = no_tree_sitter
    py = core.outline_text("x.py", "def f(a):\n    '''Doc.'''\n    return g(a)\n", "python")
    assert py.engine == "ast"
    assert [(s.kind, s.name, s.line, s.end, s.doc) for s in py.symbols] == [("function", "f", 1, 3, "Doc.")]
    assert py.refs == {"a": [2, 0], "g": [1, 1]}
    ts = core.outline_text("x.ts", "export class A {\n  run() { return 1; }\n}\n", "typescript")
    assert ts.engine == "rx"
    assert [(s.kind, s.name, s.line, s.end, s.parent) for s in ts.symbols] == [("class", "A", 1, 3, ""),
                                                                               ("method", "run", 2, 2, "A")]


def test_fallback_map_in_process_matches_golden(no_tree_sitter, capsys):
    mod = load_script("map")
    rc = mod.main(fixture_args("--no-cache"))
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == expected("map_fallback.txt")
    assert "tree-sitter not installed" in err


def test_fallback_subprocess_prints_hint_and_matches_golden():
    env = {"CODE_MAP_ENGINE": "fallback"}
    r = run("map.py", *fixture_args("--no-cache"), env=env)
    assert r.returncode == 0
    assert r.stdout == expected("map_fallback.txt")
    assert "python -m pip install tree-sitter-language-pack ast-grep-py" in r.stderr
    s = run("sym.py", *fixture_args("helper", "--no-cache"), env=env)
    assert s.stdout == expected("sym_helper_fallback.txt")
    d = run("deps.py", *fixture_args("web/src/api.ts", "--no-cache"), env=env)
    assert d.stdout == expected("deps_api.txt")  # import resolution does not depend on the engine
    sl = run("slice.py", "--repo", str(FIXTURE), "app/util.py:18-20,4-4", "web/src/index.ts:9-11", env=env)
    assert sl.stdout == expected("slice_ranges.txt")


def test_fallback_outlines_every_language(no_tree_sitter):
    core = no_tree_sitter
    want = {
        "svc/main.go": {("struct", "Server"), ("interface", "Handler"), ("method", "Start"), ("function", "helper"),
                        ("function", "main"), ("const", "Version"), ("var", "debug")},
        "rs/src/main.rs": {("struct", "Point"), ("impl", "Point"), ("method", "new"), ("method", "sum"),
                           ("trait", "Shape"), ("enum", "Mode"), ("const", "LIMIT"), ("function", "main")},
        "c/util.c": {("function", "helper"), ("function", "hidden")},
        "c/util.h": {("macro", "MAX_ITEMS"), ("struct", "item"), ("type", "item_t"), ("enum", "color")},
        "cpp/shape.hpp": {("namespace", "geo"), ("class", "Shape"), ("class", "Circle"), ("method", "area"),
                          ("method", "name")},
        "sh/lib.sh": {("function", "helper"), ("function", "cleanup"), ("var", "VERSION")},
    }
    for rel, syms in want.items():
        text = (LANGS / rel).read_text(encoding="utf-8")
        info = core.outline_text(rel, text, core.detect_lang(LANGS / rel))
        assert info.engine == "rx"
        got = {(s.kind, s.name) for s in info.symbols}
        assert syms <= got, (rel, syms - got)
