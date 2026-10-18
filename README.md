# code-map

[![tests](https://github.com/TikoTikTok/code-map/actions/workflows/test.yml/badge.svg)](https://github.com/TikoTikTok/code-map/actions/workflows/test.yml)

A Claude Code skill that gives the model a deterministic, token-cheap view of a
codebase, so it reads outlines and exact line ranges instead of whole files.

Five scripts over one engine. tree-sitter outlines every file (symbols with
start-end lines, imports, identifier references), ast-grep answers structural
queries, a reference graph ranks the files, and a cache keyed by path, size
and mtime makes the second run on a big repo instant. The output is sorted and
timestamp-free: the same input gives byte-identical bytes, with or without the
cache, and the test suite checks that.

It exists to spend tokens on judgment instead of scrolling. A 740-line file is
11k tokens to read; the map of its whole repository is 1.5k, the symbol lookup
300, and the slice of the one method that matters 250.

## What a run looks like

```
map.py <repo>        →  ranked files, each with its symbols and line ranges, inside a token budget
   model picks the file and symbol that matter
sym.py <repo> name   →  every definition (file:a-b, kind, signature, docstring) + references with one line of context
   model pastes the sed command sym.py printed, or
slice.py file:a-b    →  exactly those lines, numbered, under their class / function headers
deps.py <repo> file  →  imports, importers, and files that use its symbols: what breaks if this changes
q.py <repo> pattern  →  ast-grep structural search: every place that does `await $X.json()`
```

## Install

```bash
python -m pip install tree-sitter-language-pack ast-grep-py
```

Versions this was built and tested against (Python 3.14.2, Windows 11):

| package | version |
|---|---|
| tree-sitter-language-pack | 1.22.1 |
| tree-sitter | 0.26.0 |
| ast-grep-py | 0.51.0 |
| pytest (tests only) | 9.1.1 |

Without the packages every script still runs: Python goes through the stdlib
`ast` module (exact), other languages through a regex outline (approximate),
and a one-line install hint goes to stderr. `q.py` uses the `sg` / `ast-grep`
CLI if it is on PATH, and otherwise approximates the pattern with a regex.

As a Claude Code plugin from this repo:

```bash
claude plugin marketplace add TikoTikTok/code-map
claude plugin install code-map@code-map
```

Or copy the skill folder into your user skills (what this machine does):

```bash
cp -R code-map/skills/code-map ~/.claude/skills/
```

Then, in any session, the skill triggers on "map this repo", "where is X
defined", "who calls X", "what imports this file", "outline this file", "find
all places that do <pattern>".

## Use the scripts directly

```bash
S=skills/code-map/scripts
python $S/map.py <repo> [--budget 1500] [--focus path-or-symbol] [--include GLOB] [--exclude GLOB]
python $S/sym.py <repo> <name> [--kind function|class|method|any] [--max-refs 40]
python $S/q.py <repo> '<ast-grep pattern>' [--lang ts] [--count] [--selector KIND]
python $S/q.py <repo> --rule rules.yml
python $S/slice.py <file>:<a>-<b>[,<c>-<d>] [<file2>:<e>-<f>] [-C 2] [--repo DIR]
python $S/slice.py <file>                      # outline only
python $S/deps.py <repo> <file>
```

Every script takes `--json`; the repo scripts take `--no-cache`. Paths in the
output are relative to the repo root with forward slashes. The cache lives in
`%LOCALAPPDATA%/code-map/cache` (or `~/.cache/code-map/cache`, or
`$CODE_MAP_CACHE_DIR`), one JSON file per repository.

Languages: Python, TypeScript / JavaScript (.ts .tsx .js .mjs .cjs .jsx), PHP,
Go, Rust, C, C++, Bash. Skipped: `node_modules`, `vendor`, `dist`, `build`,
`.venv`, `target` and friends, lockfiles, `*.min.js`, `*_pb2.py`, `*.pb.go`,
anything whose first lines say `@generated` or `DO NOT EDIT`, binaries.

## Validation on real code

### flow-trace (Python, 7 code files)

```
$ python skills/code-map/scripts/map.py C:/Users/tiko/proj/flow-trace --budget 600
# code-map flow-trace: 7 code files (5 shown), 100 symbols (47 shown); budget 600 tok, used 598 tok
skills/flow-trace/scripts/repo_scan.py (py, 625 lines)
  120-127 def is_text(raw: bytes) -> bool
  130-132 def short(s: str, n: int = 120) -> str
  135-141 def run_git(root: Path, *args: str) -> str
  144-590 class Scan
    145-150 def __init__(self, root: Path, max_hits: int)
    153-157 def walk(self)
    159-160 def rel(self, p: Path) -> str
    162-177 def kind(self, p: Path) -> str
    179-193 def load_text(self, p: Path) -> list[str] | None
    196-230 def inventory(self)
    232-242 def autorun(self)
    244-367 def entry_points(self)
    369-406 def indicators(self)
    408-431 def hidden_chars(self)
    433-457 def binaries(self)
    526-590 def markdown(self) -> str
  593-621 def main(argv=None) -> int
  ... +18 more symbols
skills/flow-trace/scripts/build_trace.py (py, 408 lines)
  28-38 LANG =
  43-44 class BuildError(Exception)
  47-53 def lang_for(rel: str) -> str
  ...
```

```
$ python skills/code-map/scripts/sym.py C:/Users/tiko/proj/flow-trace load_text
# sym load_text: 1 definition, 9 references in 1 file
## definitions
skills/flow-trace/scripts/repo_scan.py:179-193 method Scan.load_text
  def load_text(self, p: Path) -> list[str] | None
## references (9 in 1 files)
skills/flow-trace/scripts/repo_scan.py
  308 call lines = self.load_text(p) or []
  322 call lines = self.load_text(p) or []
  ...
## bodies
sed -n '179,193p' 'skills/flow-trace/scripts/repo_scan.py'
```

```
$ python skills/code-map/scripts/slice.py --repo C:/Users/tiko/proj/flow-trace skills/flow-trace/scripts/repo_scan.py:179-193
== skills/flow-trace/scripts/repo_scan.py (py, 625 lines)
144 > class Scan:
179 |     def load_text(self, p: Path) -> list[str] | None:
180 |         r = self.rel(p)
181 |         if r in self.text:
182 |             return self.text[r]
...
```

### lets (JavaScript / TypeScript, 59 code files, 1783 symbols)

```
$ python skills/code-map/scripts/map.py C:/Users/tiko/proj/lets
# code-map lets: 59 code files (25 shown), 1783 symbols (105 shown); budget 1500 tok, used 1492 tok
lets/engine/src/native.mjs (js, 741 lines)
  133-133 const clean = (s, max = MAX_TEXT)
  141-141 const button = (key, label, extra = {})
  154-177 export function svgProblem(source)
  184-235 export function sanitizeTree(node, { prefix = 'u.', counter = { nodes: 0 }, depth = 0 } = {})
  244-260 export function dumpTree(node, indent = 0)
  264-644 class NativeWindow
    367-369 push(type, data, extra = {})
    467-473 stay()
    477-493 action(act)
    625-630 profile()
    632-643 destroy(reason)
  648-741 export class NativeRuntime
    668-686 open(unique, { windowId = null, args = {}, viewport = null } = {})
    690-692 list()
    720-725 action(id, act)
  ... +61 more symbols
lets-unique-mors/ui/mors.js (js, 1074 lines)
  ...
```

```
$ python skills/code-map/scripts/sym.py C:/Users/tiko/proj/lets NativeWindow
# sym NativeWindow: 2 definitions, 7 references in 3 files
## definitions
lets/engine/src/native.mjs:264-644 class NativeWindow
  class NativeWindow
lets/mods/lets-native/types/index.d.ts:13-24 type NativeWindow
  export type NativeWindow =
  doc: A native window this session draws as a pane. The host token is never kept here.
## references (7 in 3 files)
lets/engine/src/native.mjs
  677 call const w = new NativeWindow(this, { id, u, args: args && typeof args === 'object' ? args : {}, per...
lets/mods/lets-native/hooks/register.tsx
  20 ref  import type { Feed, FeedEvent, HostInfo, HostRef, NativeState, NativeWindow, Tree, TreeNode } fro...
  ...
```

Timing on lets (59 files, ~30k lines): 0.98 s without the cache, 0.24 s warm.
The cache file is 536 KB.

### Before / after token estimate

Tokens are chars / 4, the same estimate the scripts use for their budget.
"Before" is what the model reads when it opens the files a question needs;
"after" is map + sym + the slice that answers it.

| question | whole-file reads | map + sym + slice |
|---|---|---|
| flow-trace: "what does `load_text` do and who calls it" | `repo_scan.py` 8 371 tok (+ `build_trace.py` 4 159 if it looks there) | map 1 349 + sym 201 + slice 179-193 ≈ 200 → **≈ 1 750** |
| lets: "where is `NativeWindow` defined, who constructs it" | `native.mjs` 11 413 tok (+ `register.tsx`) | map 1 583 + sym 277 + slice 264-330 ≈ 700 → **≈ 2 560** |

Roughly 5x fewer tokens per question on both repos, and the saving grows with
file size because the map and symbol lookup cost the same whatever the file.

## Layout

```
.claude-plugin/              plugin + marketplace manifests
skills/code-map/
  SKILL.md                   trigger phrases, token discipline, when-to-use table, procedure
  scripts/map.py             ranked repo map within a token budget
  scripts/sym.py             definitions, references, sed commands for one symbol
  scripts/q.py               ast-grep pattern / rule file over the repo
  scripts/slice.py           exact line ranges with scope headers; outline when no range
  scripts/deps.py            imports, importers, symbol users of one file
  scripts/codemap_core.py    walk, language detection, cache, import resolution, graph, PageRank
  scripts/codemap_ts.py      tree-sitter outlines + reference counting per language
  scripts/codemap_fallback.py  stdlib ast (Python) and regex outlines (the rest)
tests/
  fixture_repo/              Python + TypeScript + PHP with known symbols, calls, imports, plus junk that must be skipped
  fixture_langs/             Go, Rust, C, C++, Bash
  expected/                  golden outputs the tests compare byte for byte
  test_*.py                  pytest: goldens, determinism with/without cache, fallback engine, every language
```

## Tests

```bash
python -m pytest tests -q
```

The suite runs every script against the fixture repo and compares the exact
text, runs the map cold, warm and without cache and asserts identical bytes,
monkeypatches `tree_sitter_language_pack` away to exercise the stdlib
fallback, and checks each promised language through both engines.

CI (`.github/workflows/test.yml`) runs it on every push and pull request:
Ubuntu, Windows and macOS on Python 3.12 and 3.14 with the engines installed,
one job with nothing but pytest installed so the stdlib fallback is tested for
real, and one job that maps this repository twice and compares the bytes.

## How the map is ranked

Each code file is a node. Edges: resolved imports (weight 3) and symbol
references (a file that mentions `Router` points at the file that defines
`Router`, weighted by the square root of the mention count, discounted for
short or underscore names and split among several definers). PageRank with
damping 0.85 over 40 iterations ranks the files; symbols within a file are
scored by kind (classes and functions first, methods, then types, then
variables), by how many other files mention the name, and with a small penalty
for each extra symbol from the same file so other files get their first
symbols in. Everything is sorted before it is iterated, which is what keeps
the output byte-identical.

## License

MIT.
