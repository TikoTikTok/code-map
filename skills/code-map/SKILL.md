---
name: code-map
description: Deterministic, token-cheap view of a codebase so the model reads outlines and exact line ranges instead of whole files. Five stdlib-friendly Python scripts over tree-sitter and ast-grep give a ranked repo map, every definition and call site of a symbol, structural pattern queries, scoped line slices, and per-file dependency fan-in / fan-out, with byte-identical output for identical input. Use for "map this repo", "where is X defined", "who calls X", "what imports this file", "outline this file", "find all places that do <pattern>", "what breaks if I change this file", or before reading any source file over ~150 lines. Not a refactoring tool; it only reads.
---

# code-map

Five scripts, one shared engine. They parse with tree-sitter (ten languages) and
query with ast-grep, cache every outline, and print sorted, timestamp-free text
that a model can read in one glance. When the packages are missing they still
run: Python through the stdlib `ast`, everything else through a regex outline,
with a one-line install hint on stderr.

Languages: Python, TypeScript / JavaScript (incl. .tsx, .mjs, .cjs), PHP, Go, Rust, C, C++, Bash.

## Token discipline

The point of this skill is to spend tokens on judgment, not on scrolling.

1. **Never Read a file over ~150 lines without a map or a symbol lookup first.** `slice.py <file>` alone gives the outline; `map.py --focus <file>` gives it in repo context.
2. **map, then sym, then slice.** Map to find the files that matter, `sym` to locate the definition and its callers, `slice` to read exactly those lines with their enclosing class / function headers. Read a whole file only when it is under ~150 lines or the slice shows you need all of it.
3. **One query per question.** "Where is X defined" is one `sym.py`; "who imports this" is one `deps.py`; "every place that does `await $X.json()`" is one `q.py`. Do not run the map again to answer a question the map already answered.
4. **Trust the ranges.** Symbol lines are `start-end` from the parser. Paste the `sed -n 'a,bp' file` commands `sym.py` prints instead of guessing a window.
5. **Delegate repo-wide sweeps.** Over ~300 code files, send an Explore subagent one narrow question ("which file defines the websocket handler and what does it import?") with the `map.py` header pasted in, and keep only its conclusion.
6. **Never paste whole files into answers.** Quote the slice, cite `file:line`.
7. **Budget is advisory, cache is free.** The default map is ~1500 tokens; raise `--budget` once if the repo is large rather than running several maps. Outlines are cached under `%LOCALAPPDATA%/code-map/cache` (or `~/.cache/code-map`), keyed by absolute path, size and mtime, so a second run on a big repo is instant. `--no-cache` bypasses it.

## When to use which script

| Question | Command | Reads back |
|---|---|---|
| What is in this repo, what matters | `map.py <repo> [--budget N] [--focus path-or-symbol]` | ranked files with symbols and line ranges |
| Where is X defined, who calls it | `sym.py <repo> X [--kind function\|class\|method\|any]` | definitions, references with context, `sed` commands |
| Every place that does a pattern | `q.py <repo> '<ast-grep pattern>' [--lang L] [--count]` | `file:line: match` lines |
| Read these lines, with context | `slice.py file:a-b[,c-d] ...` | numbered lines under their scope headers |
| What imports this, what does it import | `deps.py <repo> <file>` | fan-out, fan-in, other users of its symbols |
| Outline one file | `slice.py <file>` (no ranges) | symbol list |

All scripts take `--json`; repo scripts take `--include GLOB`, `--exclude GLOB`, `--no-cache`. Paths in the output are relative to the repo root, forward slashes.

## Install

```bash
python -m pip install tree-sitter-language-pack ast-grep-py
```

Without them every script still runs (stdlib fallback, approximate for non-Python code). `sg` / `ast-grep` on PATH is used by `q.py` when `ast-grep-py` is missing.

## Procedure

### 1. Map

```bash
python <skill>/scripts/map.py <repo>
```

Read the header (file and symbol counts, budget used), then the files in rank order. Rank is PageRank over imports plus symbol references, so entry points and shared modules come first; tests and scripts come last. A `... +N more symbols` line means the budget cut that file; `--focus <path>` shows all of it. The `# not shown` line lists what the budget dropped.

### 2. Locate

```bash
python <skill>/scripts/sym.py <repo> handleRequest
python <skill>/scripts/sym.py <repo> Router.dispatch --kind method
```

Definitions first (file:start-end, kind, qualified name, signature, first docstring line), then references grouped by file with one line of context each, tagged `call` or `ref`, then the `sed -n` commands for the bodies. Two definitions of the same name in different languages are both listed; pick by path.

### 3. Read exactly that

```bash
python <skill>/scripts/slice.py src/router.ts:120-160
python <skill>/scripts/slice.py src/router.ts:120-160,300-310 src/bus.ts:10-40 -C 2
```

Lines marked `>` are the enclosing class / function headers (printed once), lines marked `|` are the requested range. `-C N` widens each range.

### 4. Impact and patterns

```bash
python <skill>/scripts/deps.py <repo> src/router.ts
python <skill>/scripts/q.py <repo> 'fetch($URL, $$$)' --lang ts
python <skill>/scripts/q.py <repo> 'subprocess.run($$$ARGS)' --count
python <skill>/scripts/q.py <repo> --rule rules.yml
```

`deps.py` answers "what breaks if I change this": imports (resolved to repo files or marked external), importers, and files that use its symbols without importing it (PHP autoload, Go packages, C headers). `q.py` takes any ast-grep pattern: `$A` one node, `$$$ARGS` many. Go, C, C++ and Rust expression patterns are wrapped in a function body automatically; pass `--selector KIND` to pick the node kind if the automatic choice is wrong.

## Output rules the scripts keep

- Files sorted by rank then path; symbols by line; references by file then line. No timestamps, no absolute paths, no engine name in stdout (it goes to stderr).
- The same input gives byte-identical output across runs, with and without the cache.
- Vendored, generated, minified and lock files are skipped: `node_modules`, `vendor`, `dist`, `build`, `.venv`, `target`, `*.min.js`, `*_pb2.py`, `*.pb.go`, files whose head says `@generated` / `DO NOT EDIT`, and every common lockfile.

## Files

- `scripts/map.py` — ranked repo map within a token budget; `--focus` forces files or symbols in.
- `scripts/sym.py` — definitions, references, `sed` commands for one symbol.
- `scripts/q.py` — ast-grep pattern or rule file over the repo; `--count` for totals.
- `scripts/slice.py` — exact line ranges with scope headers; outline when no range is given.
- `scripts/deps.py` — imports, importers and symbol users of one file.
- `scripts/codemap_core.py` — walk, language detection, cache, import resolution, reference graph, PageRank.
- `scripts/codemap_ts.py` — tree-sitter outlines and reference counting per language.
- `scripts/codemap_fallback.py` — stdlib `ast` for Python and a regex outline for the rest.
