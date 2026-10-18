import json

from conftest import FIXTURE, expected, run


def test_slice_ranges_with_scope_headers_golden():
    r = run("slice.py", "--repo", str(FIXTURE), "app/util.py:18-20,4-4", "web/src/index.ts:9-11")
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected("slice_ranges.txt")


def test_slice_outline_mode_golden():
    r = run("slice.py", "--repo", str(FIXTURE), "php/src/User.php")
    assert r.stdout == expected("slice_outline_user_php.txt")


def test_slice_context_and_single_line():
    r = run("slice.py", "--repo", str(FIXTURE), "app/util.py:8", "-C", "1")
    assert r.stdout.splitlines()[1:] == [" 7 | def helper(x):", ' 8 |     """Add one to x."""', " 9 |     return x + 1"]


def test_slice_json_and_bad_spec():
    r = run("slice.py", "--repo", str(FIXTURE), "app/util.py:18-19", "--json")
    data = json.loads(r.stdout)
    assert data[0]["chunks"][0] == {"start": 18, "end": 19, "scope": [12],
                                    "lines": ["    def greet(self):", '        """Return the greeting."""']}
    bad = run("slice.py", "--repo", str(FIXTURE), "app/util.py:x-y")
    assert bad.returncode == 2
