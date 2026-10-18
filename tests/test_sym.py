import json

from conftest import expected, fixture_args, run


def test_sym_helper_golden():
    r = run("sym.py", *fixture_args("helper", "--no-cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected("sym_helper.txt")


def test_sym_qualified_method_golden():
    r = run("sym.py", *fixture_args("Greeter.greet", "--no-cache"))
    assert r.returncode == 0
    assert r.stdout == expected("sym_greet.txt")
    assert run("sym.py", *fixture_args("Greeter::greet", "--no-cache")).stdout == r.stdout


def test_sym_kind_filter_and_sed_commands():
    r = run("sym.py", *fixture_args("User", "--kind", "class", "--no-cache"))
    assert r.stdout == expected("sym_user_class.txt")
    assert "sed -n '5,19p' 'php/src/User.php'" in r.stdout
    assert "sed -n '2,5p' 'web/src/types.ts'" in r.stdout
    methods_only = run("sym.py", *fixture_args("User", "--kind", "method", "--no-cache"))
    assert "## definitions\n  (none)" in methods_only.stdout


def test_sym_json_and_unknown_symbol():
    r = run("sym.py", *fixture_args("toName", "--no-cache", "--json"))
    data = json.loads(r.stdout)
    assert [d["path"] for d in data["definitions"]] == ["web/src/api.ts"]
    assert data["references"]["web/src/index.ts"][1] == {"line": 11, "kind": "call", "text": "return toName(u);"}
    assert data["sed"] == ["sed -n '11,11p' 'web/src/api.ts'"]
    missing = run("sym.py", *fixture_args("noSuchSymbolAnywhere", "--no-cache"))
    assert missing.returncode == 1
    assert "0 definitions, 0 references" in missing.stdout
