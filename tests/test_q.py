import importlib.util
import json
import sys

import pytest

from conftest import LANGS, SCRIPTS, expected, fixture_args, run

HAVE_AG = importlib.util.find_spec("ast_grep_py") is not None


@pytest.mark.skipif(not HAVE_AG, reason="ast-grep-py not installed")
def test_q_pattern_golden_and_count():
    r = run("q.py", *fixture_args("helper($A)", "--no-cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected("q_helper.txt")
    c = run("q.py", *fixture_args("helper($A)", "--count"))
    assert c.stdout == "app/main.py\t1\napp/util.py\t1\nphp/index.php\t1\n# 3 matches in 3 files\n"


@pytest.mark.skipif(not HAVE_AG, reason="ast-grep-py not installed")
def test_q_lang_filter_php_and_ts_json():
    php = run("q.py", *fixture_args("$X->name()", "--lang", "php"))
    assert php.stdout == "php/index.php:12: $u->name()\n# 1 matches in 1 files\n"
    ts = run("q.py", *fixture_args("toName($A)", "--lang", "ts", "--json"))
    assert json.loads(ts.stdout) == [{"file": "web/src/index.ts", "line": 11, "col": 12, "end_line": 11,
                                      "text": "toName(u)"}]


@pytest.mark.skipif(not HAVE_AG, reason="ast-grep-py not installed")
def test_q_wraps_go_c_cpp_rust_expressions():
    r = run("q.py", str(LANGS), "helper($A)")
    assert r.stdout == ("c/main.c:6: helper(it.id)\n"
                        "rs/src/main.rs:19: helper(self.x)\n"
                        "svc/main.go:24: helper(1)\n"
                        "# 3 matches in 3 files\n")
    go = run("q.py", str(LANGS), "fmt.Println($$$A)", "--lang", "go")
    assert go.stdout.startswith("svc/main.go:24: fmt.Println(s.Addr, helper(1))\n")
    cpp = run("q.py", str(LANGS), "geo::twice($A)", "--lang", "cpp")
    assert cpp.stdout.startswith("cpp/shape.cpp:10: geo::twice(c.area())\n")
    rust = run("q.py", str(LANGS), "println!($$$A)", "--lang", "rust")
    assert rust.stdout.startswith('rs/src/main.rs:44: println!("{}", p.area())\n')
    whole = run("q.py", str(LANGS), "func $F($$$) { $$$ }", "--lang", "go", "--count")
    assert whole.stdout == "svc/main.go\t1\n# 1 matches in 1 files\n"  # only main(): no receiver, no return type


RULE_YAML = """# calls of helper inside a function, as an ast-grep rule file
id: calls-helper
language: python
message: "helper called"
rule:
  any:
    - pattern: helper($A)
    - pattern: helper($A, $B)
  inside:
    kind: function_definition
    stopBy: end
"""


@pytest.mark.skipif(not HAVE_AG, reason="ast-grep-py not installed")
def test_q_rule_file(tmp_path):
    rule = tmp_path / "rule.yml"
    rule.write_text(RULE_YAML, encoding="utf-8")
    r = run("q.py", *fixture_args("--rule", str(rule)))
    assert r.stdout == "app/main.py:10: helper(1)\napp/util.py:20: helper(CONST)\n# 2 matches in 2 files\n", r.stderr


def test_simple_yaml_parser_matches_pyyaml():
    """The built-in parser (used when PyYAML is absent) must read a typical rule file like PyYAML does."""
    spec = importlib.util.spec_from_file_location("codemap_q_yaml_test", SCRIPTS / "q.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    want = {"id": "calls-helper", "language": "python", "message": "helper called",
            "rule": {"any": [{"pattern": "helper($A)"}, {"pattern": "helper($A, $B)"}],
                     "inside": {"kind": "function_definition", "stopBy": "end"}}}
    assert mod.parse_simple_yaml(RULE_YAML) == want
    try:
        import yaml  # type: ignore
        assert yaml.safe_load(RULE_YAML) == want
    except ImportError:
        pass
    more = "rule:\n  kind: call\n  has:\n    field: function\n    regex: '^h'\nconstraints:\n  A:\n    regex: ^\\d+$\nutils:\n  n: 1\n  flag: true\n  none: ~\n  list:\n    - a\n    - 2\n"
    assert mod.parse_simple_yaml(more) == {"rule": {"kind": "call", "has": {"field": "function", "regex": "^h"}},
                                           "constraints": {"A": {"regex": "^\\d+$"}},
                                           "utils": {"n": 1, "flag": True, "none": None, "list": ["a", 2]}}


def test_q_degrades_to_regex_without_ast_grep(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ast_grep_py", None)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("codemap_q_test", SCRIPTS / "q.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    rc = mod.main(fixture_args("helper($A)", "--no-cache"))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "approximating the pattern with a regex" in err
    lines = out.splitlines()
    # a regex cannot tell the definition `def helper(x):` from a call, so only check the calls are found
    for want in ("app/main.py:10: helper(1)", "app/util.py:20: helper(CONST)", "php/index.php:13: helper(1)"):
        assert want in lines, want
