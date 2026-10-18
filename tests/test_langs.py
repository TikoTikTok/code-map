"""Every language the skill promises works end to end through the tree-sitter engine."""
import importlib
import importlib.util
import sys

import pytest

from conftest import LANGS, SCRIPTS, run

HAVE_TS = importlib.util.find_spec("tree_sitter_language_pack") is not None
pytestmark = pytest.mark.skipif(not HAVE_TS, reason="tree-sitter-language-pack not installed")


@pytest.fixture
def core(monkeypatch):
    for name in ("codemap_core", "codemap_ts", "codemap_fallback"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delenv("CODE_MAP_ENGINE", raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    mod = importlib.import_module("codemap_core")
    assert mod.TS is not None
    return mod


def outline(core, rel):
    text = (LANGS / rel).read_text(encoding="utf-8")
    info = core.outline_text(rel, text, core.detect_lang(LANGS / rel))
    assert info.engine == "ts"
    return info


def test_go(core):
    info = outline(core, "svc/main.go")
    got = {(s.kind, s.name, s.parent, s.line, s.end) for s in info.symbols}
    assert {("struct", "Server", "", 9, 11), ("interface", "Handler", "", 14, 16), ("const", "Version", "", 18, 18),
            ("var", "debug", "", 20, 20), ("method", "Start", "Server", 23, 26), ("function", "helper", "", 28, 30),
            ("function", "main", "", 32, 35)} == got
    assert next(s.doc for s in info.symbols if s.name == "Start") == "Start runs the server."
    assert [(i.module, i.names) for i in info.imports] == [("fmt", ["fmt"]), ("os", ["os"])]
    assert info.refs["helper"] == [1, 1] and info.refs["Println"] == [1, 1]


def test_rust(core):
    info = outline(core, "rs/src/main.rs")
    got = {(s.kind, s.name, s.parent) for s in info.symbols}
    assert {("module", "util", ""), ("struct", "Point", ""), ("impl", "Point", ""), ("method", "new", "Point"),
            ("method", "sum", "Point"), ("trait", "Shape", ""), ("method", "area", "Shape"),
            ("method", "area", "Point"), ("enum", "Mode", ""), ("const", "LIMIT", ""),
            ("function", "main", "")} == got
    assert next(s.doc for s in info.symbols if s.name == "Point") == "A point on the grid."
    assert next(s.sig for s in info.symbols if s.kind == "impl" and s.line == 27) == "impl Shape for Point"
    mods = [(i.kind, i.module, i.names) for i in info.imports]
    assert ("mod", "util", ["util"]) in mods and ("use", "util::helper", ["helper"]) in mods
    assert info.refs["helper"] == [2, 1]  # one `use`, one call


def test_c_and_cpp(core):
    h = outline(core, "c/util.h")
    assert {(s.kind, s.name, s.line, s.end) for s in h.symbols} >= {
        ("macro", "MAX_ITEMS", 4, 4), ("struct", "item", 6, 9), ("type", "item_t", 11, 11), ("enum", "color", 13, 13)}
    c = outline(core, "c/util.c")
    assert [(s.kind, s.name, s.line, s.end, s.doc) for s in c.symbols] == [
        ("function", "helper", 4, 6, "Add one."), ("function", "hidden", 8, 11, "")]
    assert [(i.kind, i.module) for i in c.imports] == [("include", "util.h")]
    hpp = outline(core, "cpp/shape.hpp")
    got = {(s.kind, s.name, s.parent) for s in hpp.symbols}
    assert {("namespace", "geo", ""), ("class", "Shape", ""), ("method", "~Shape", "Shape"), ("method", "area", "Shape"),
            ("method", "name", "Shape"), ("class", "Circle", ""), ("method", "Circle", "Circle"),
            ("method", "area", "Circle"), ("function", "twice", "")} == got
    twice = next(s for s in hpp.symbols if s.name == "twice")
    assert (twice.line, twice.end, twice.sig) == (22, 23, "template <typename T> T twice(T v)")
    cpp = outline(core, "cpp/shape.cpp")
    assert [(s.kind, s.name, s.parent, s.line, s.end) for s in cpp.symbols] == [
        ("method", "area", "geo::Circle", 4, 6), ("function", "main", "", 8, 12)]


def test_bash(core):
    lib = outline(core, "sh/lib.sh")
    assert [(s.kind, s.name, s.line, s.end, s.doc) for s in lib.symbols] == [
        ("var", "VERSION", 4, 4, ""), ("function", "helper", 7, 9, "Print the argument with a prefix."),
        ("function", "cleanup", 11, 13, "")]
    runsh = outline(core, "sh/run.sh")
    assert [(i.kind, i.module) for i in runsh.imports] == [("source", "./lib.sh")]
    assert runsh.refs["helper"] == [1, 1] and runsh.refs["cleanup"] == [1, 1] and runsh.refs["main"] == [1, 1]


def test_sym_across_languages():
    r = run("sym.py", str(LANGS), "helper", "--no-cache")
    assert r.returncode == 0
    defs = [ln for ln in r.stdout.splitlines() if ln.startswith(("c/", "svc/", "rs/", "sh/")) and " function " in ln]
    assert defs == ["c/util.c:4-6 function helper", "rs/src/util.rs:2-4 function helper",
                    "sh/lib.sh:7-9 function helper", "svc/main.go:28-30 function helper"]
    assert "  6 call printf(\"%d %s\\n\", helper(it.id), argv[0]);" in r.stdout
    assert "  8 call helper \"$1\"" in r.stdout
