import json

from conftest import LANGS, expected, fixture_args, run


def test_deps_ts_golden():
    r = run("deps.py", *fixture_args("web/src/api.ts", "--no-cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected("deps_api.txt")


def test_deps_php_golden_and_suffix_lookup():
    r = run("deps.py", *fixture_args("php/src/User.php", "--no-cache"))
    assert r.stdout == expected("deps_user_php.txt")
    by_suffix = run("deps.py", *fixture_args("User.php", "--no-cache"))
    assert by_suffix.stdout == r.stdout


def test_deps_python_json():
    r = run("deps.py", *fixture_args("app/util.py", "--no-cache", "--json"))
    data = json.loads(r.stdout)
    assert data["imports"] == []
    assert data["imported_by"] == [{"path": "app/main.py", "line": 4, "module": "app.util",
                                    "names": ["Greeter", "helper"], "kind": "from"}]
    assert data["references_symbols"] == []


def test_deps_resolves_rust_mod_c_include_and_bash_source():
    rs = run("deps.py", str(LANGS), "rs/src/util.rs", "--no-cache")
    assert "rs/src/main.rs  mod util [util]" in rs.stdout
    c = run("deps.py", str(LANGS), "c/util.h", "--no-cache")
    assert "c/main.c  include util.h" in c.stdout and "c/util.c  include util.h" in c.stdout
    sh = run("deps.py", str(LANGS), "sh/lib.sh", "--no-cache")
    assert "sh/run.sh  source ./lib.sh" in sh.stdout
    cpp = run("deps.py", str(LANGS), "cpp/shape.hpp", "--no-cache")
    assert "cpp/shape.cpp  include shape.hpp" in cpp.stdout
    assert "string  sysinclude  (external)" in cpp.stdout


def test_deps_unknown_file():
    r = run("deps.py", *fixture_args("nope/missing.py", "--no-cache"))
    assert r.returncode == 2
