import json
import os
import shutil

from conftest import FIXTURE, expected, fixture_args, run


def test_map_text_matches_golden():
    r = run("map.py", *fixture_args("--no-cache"))
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected("map.txt")


def test_map_is_byte_identical_with_and_without_cache(private_cache):
    for f in os.listdir(private_cache):
        os.remove(os.path.join(private_cache, f))
    cold = run("map.py", *fixture_args())
    warm = run("map.py", *fixture_args())
    none = run("map.py", *fixture_args("--no-cache"))
    assert cold.returncode == warm.returncode == none.returncode == 0
    assert cold.stdout == warm.stdout == none.stdout == expected("map.txt")
    assert os.listdir(private_cache), "a cache file should have been written"


def test_map_skips_vendored_generated_and_lockfiles():
    r = run("map.py", *fixture_args("--no-cache", "--budget", "100000"))
    for banned in ("junkFunction", "vendoredFunction", "generatedFunction", "package-lock", "node_modules",
                   "vendor/"):
        assert banned not in r.stdout, banned


def test_map_budget_cuts_and_focus_forces():
    small = run("map.py", *fixture_args("--no-cache", "--budget", "60"))
    assert small.returncode == 0
    assert "... +" in small.stdout and "# not shown" in small.stdout
    header = small.stdout.splitlines()[0]
    assert "budget 60 tok" in header
    focused = run("map.py", *fixture_args("--no-cache", "--budget", "60", "--focus", "view.tsx", "--focus", "greet"))
    assert "web/src/view.tsx (tsx, 6 lines)" in focused.stdout
    assert "export function UserCard" in focused.stdout
    assert "18-20 def greet(self)" in focused.stdout
    assert "12-20 class Greeter" in focused.stdout  # the container comes along with the member


def test_map_include_exclude():
    only_py = run("map.py", *fixture_args("--no-cache", "--include", "*.py"))
    assert "app/util.py" in only_py.stdout and "web/src" not in only_py.stdout
    no_php = run("map.py", *fixture_args("--no-cache", "--exclude", "php"))
    assert "php/" not in no_php.stdout and "app/util.py" in no_php.stdout


def test_map_json():
    r = run("map.py", *fixture_args("--no-cache", "--json"))
    data = json.loads(r.stdout)
    assert data["budget_tokens"] == 1500
    paths = [f["path"] for f in data["files"]]
    assert paths == sorted(paths, key=lambda p: [f["rank"] for f in data["files"] if f["path"] == p][0])
    util = next(f for f in data["files"] if f["path"] == "app/util.py")
    greet = next(s for s in util["symbols"] if s["name"] == "greet")
    assert greet == {"kind": "method", "name": "greet", "line": 18, "end": 20, "sig": "def greet(self)",
                     "parent": "Greeter", "doc": "Return the greeting."}


def test_map_on_a_copy_gives_the_same_bytes(tmp_path):
    """Absolute location must not leak into the output."""
    copy = tmp_path / "fixture_repo"
    shutil.copytree(FIXTURE, copy)
    r = run("map.py", str(copy), "--no-cache")
    assert r.stdout == expected("map.txt")
