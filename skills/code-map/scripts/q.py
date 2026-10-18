#!/usr/bin/env python3
"""q.py - structural code search: an ast-grep pattern (or rule file) over the repo, one line per match.

    python q.py <repo> '<pattern>' [--lang LANG] [--count] [--selector KIND] [--json]
    python q.py <repo> --rule rules.yml [--lang LANG] [--count]

Patterns are ast-grep patterns: `$A` matches one node, `$$$ARGS` matches many
(`fetch($URL, $$$)`, `await $X.json()`, `def $F(self, $$$): $$$`). Go, C, C++
and Rust expression patterns are wrapped in a function body automatically;
pass --selector KIND to choose which node kind of the wrapped pattern to match.
--count prints per-file totals only. Without ast-grep-py (or the `sg` CLI) the
pattern degrades to a regex and says so on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codemap_core import INSTALL_HINT, add_common_args, open_repo, setup_stdout  # noqa: E402

AG_LANGS = {"python", "typescript", "tsx", "javascript", "php", "go", "rust", "c", "cpp", "bash"}
LANG_ALIASES = {"py": "python", "ts": "typescript", "js": "javascript", "sh": "bash", "rs": "rust",
                "c++": "cpp", "cc": "cpp", "golang": "go"}
# languages whose grammars reject a bare expression at top level, with a wrapper and selectors to try
WRAP = {
    "go": ("package p\nfunc _w() {\n", "\n}\n"),
    "c": ("void _w(void) {\n", "\n}\n"),
    "cpp": ("void _w() {\n", "\n}\n"),
    "rust": ("fn _w() {\n", "\n}\n"),
}
SELECTORS = ["call_expression", "macro_invocation", "assignment_expression", "short_var_declaration",
             "declaration", "let_declaration", "return_statement", "if_statement", "for_statement",
             "binary_expression", "field_expression", "selector_expression", "expression_statement"]


def trim(text: str, limit: int = 120) -> str:
    first = text.strip().split("\n", 1)[0]
    first = re.sub(r"\s+", " ", first)
    if len(text.strip().split("\n")) > 1:
        first += " ..."
    return first if len(first) <= limit else first[: limit - 3] + "..."


def _yaml_scalar(s: str):
    s = s.strip()
    if not s or s in ("~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        return inner.replace("\\\\", "\\").replace('\\"', '"') if s[0] == '"' else inner.replace("''", "'")
    if s[0] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def parse_simple_yaml(text: str):
    """The YAML subset ast-grep rule files use: nested mappings, lists of scalars or mappings, scalars,
    comments. Anchors, multi-line scalars and flow collections spanning lines are not supported; install
    PyYAML for those."""
    lines = []
    for raw in text.split("\n"):
        stripped = raw.split("#", 1)[0] if not re.search(r"[\"'].*#", raw) else raw
        if stripped.strip() == "" or stripped.lstrip().startswith("#"):
            continue
        if stripped.strip() == "---":
            continue
        lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))

    def block(i: int, indent: int):
        """Parse the block whose first line is at index i with the given indent; return (value, next_i)."""
        if i >= len(lines):
            return None, i
        if lines[i][1].startswith("- "):
            return seq(i, indent)
        return mapping(i, indent)

    def mapping(i: int, indent: int):
        out: dict = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
            ln = lines[i][1]
            m = re.match(r"^([^:\s][^:]*?)\s*:(?:\s+(.*))?$", ln)
            if not m:
                raise ValueError(f"cannot parse line: {ln}")
            key, val = m.group(1).strip().strip("\"'"), m.group(2)
            if val is not None and val != "":
                out[key] = _yaml_scalar(val)
                i += 1
            else:
                i += 1
                if i < len(lines) and lines[i][0] > indent:
                    out[key], i = block(i, lines[i][0])
                elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
                    out[key], i = seq(i, indent)
                else:
                    out[key] = None
        return out, i

    def seq(i: int, indent: int):
        out: list = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            item = lines[i][1][2:].strip()
            if re.match(r"^[^:\s][^:]*?\s*:(\s+.*)?$", item) and not item.startswith(("\"", "'", "[", "{")):
                # a mapping that starts on the dash line: re-read it as an indented mapping
                lines[i] = (indent + 2, item)
                val, i = mapping(i, indent + 2)
                out.append(val)
            else:
                out.append(_yaml_scalar(item))
                i += 1
        return out, i

    value, i = block(0, lines[0][0] if lines else 0)
    if i < len(lines):
        raise ValueError(f"cannot parse line: {lines[i][1]}")
    return value


def load_rule(path: str) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(raw)
    except ImportError:
        if raw.lstrip().startswith("{"):
            doc = json.loads(raw)
        else:
            try:
                doc = parse_simple_yaml(raw)
            except ValueError as e:
                raise SystemExit(f"error: {e} (install PyYAML for full YAML support)")
    if not isinstance(doc, dict):
        raise SystemExit("error: rule file must be a mapping with a `rule` key")
    cfg = {k: v for k, v in doc.items() if k in ("rule", "constraints", "utils", "transform")}
    if "rule" not in cfg:
        cfg = {"rule": doc}
    return cfg, doc.get("language")


def _has_error(node) -> bool:
    if node.kind() == "ERROR":
        return True
    return any(_has_error(c) for c in node.children())


def pattern_configs(ag, lang: str, pattern: str, selector: str | None) -> list[dict]:
    """Candidate rule configs for `pattern` in `lang`, plain first.

    Go, C, C++ and Rust grammars reject a bare expression at top level (`helper($A)` parses as a
    declaration or an error), so a second candidate wraps the pattern in a function body (with a
    statement terminator first, which C needs) and selects the first node kind that builds a matcher.
    run_ag runs every candidate and keeps, per language, the one with the most matches, plain on a
    tie: a whole top-level construct (`func $F($$$) { $$$ }`) matches plainly and never wrapped, an
    expression matches wrapped and never plainly. --selector KIND forces one wrapped candidate."""
    plain = {"rule": {"pattern": pattern}}
    if lang not in WRAP:
        return [plain]
    pre, suf = WRAP[lang]
    bodies = [pattern + ";", pattern] if not pattern.rstrip().endswith((";", "}")) else [pattern]
    for sel in ([selector] if selector else SELECTORS):  # most specific selector first, statement last
        for body in bodies:
            context = pre + body + suf
            cfg = {"rule": {"pattern": {"context": context, "selector": sel}}}
            try:
                ag.SgRoot(context, lang).root().find_all(cfg)  # raises when the selector finds no node
                return [cfg] if selector else [plain, cfg]
            except Exception:
                continue
    return [plain]


def run_ag(repo, files, lang_of, cfgs_for_lang):
    import ast_grep_py as ag
    cfgs: dict[str, list[dict]] = {}
    buckets: dict[str, list[list]] = {}  # lang -> one result list per candidate config
    for rel in files:
        lang = lang_of[rel]
        if lang not in AG_LANGS:
            continue
        text = repo.read_text(rel)
        if text is None:
            continue
        if lang not in cfgs:
            cfgs[lang] = cfgs_for_lang(ag, lang)
            buckets[lang] = [[] for _ in cfgs[lang]]
        if not cfgs[lang]:
            continue
        root = ag.SgRoot(text, lang).root()
        for idx, cfg in enumerate(cfgs[lang]):
            if cfg is None:
                continue
            try:
                matches = root.find_all(cfg)
            except Exception as e:  # pattern invalid for this grammar
                print(f"q: skipping {lang} ({str(e).splitlines()[0]})", file=sys.stderr)
                cfgs[lang][idx] = None
                continue
            for m in matches:
                r = m.range()
                buckets[lang][idx].append((rel, r.start.line + 1, r.start.column + 1, r.end.line + 1, m.text()))
    results = []
    for lang in sorted(buckets):
        best = buckets[lang][0] if buckets[lang] else []
        for b in buckets[lang][1:]:
            if len(b) > len(best):
                best = b
        results.extend(best)
    return results


def run_sg_cli(sg: str, repo, pattern: str | None, rule: str | None, lang: str | None):
    cmd = [sg]
    if rule:
        cmd += ["scan", "--rule", rule, "--json=compact"]
    else:
        cmd += ["run", "--pattern", pattern, "--json=compact"]
        if lang:
            cmd += ["--lang", lang]
    cmd.append(str(repo.root))
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    results = []
    try:
        items = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        print(out.stderr.strip(), file=sys.stderr)
        return results
    for it in items:
        try:
            rel = Path(it["file"]).resolve().relative_to(repo.root).as_posix()
        except ValueError:
            rel = it["file"].replace("\\", "/")
        if rel not in repo.langs:
            continue
        rng = it["range"]
        results.append((rel, rng["start"]["line"] + 1, rng["start"]["column"] + 1, rng["end"]["line"] + 1,
                        it["text"]))
    return results


def pattern_to_regex(pattern: str) -> re.Pattern:
    parts = re.split(r"(\$\$\$\w*|\$\w+)", pattern)
    rx = ""
    for part in parts:
        if part.startswith("$$$"):
            rx += r".*?"
        elif part.startswith("$"):
            rx += r"[^\s,;(){}]+?(?:\([^)]*\))?"
        else:
            rx += r"\s*".join(re.escape(tok) for tok in re.split(r"\s+", part) if tok)
            if part.strip() != part and part.strip():
                rx = rx
    return re.compile(rx)


def run_regex(repo, files, pattern: str):
    rx = pattern_to_regex(pattern)
    results = []
    for rel in files:
        text = repo.read_text(rel)
        if text is None:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            m = rx.search(line)
            if m:
                results.append((rel, i, m.start() + 1, i, m.group(0)))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("pattern", nargs="?", help="ast-grep pattern (omit with --rule)")
    ap.add_argument("--lang", help="only files of this language (python, typescript, tsx, javascript, php, go, rust, c, cpp, bash)")
    ap.add_argument("--rule", help="ast-grep rule file (YAML, or JSON) instead of a pattern")
    ap.add_argument("--count", action="store_true", help="per-file match counts only")
    ap.add_argument("--selector", help="node kind to select from a wrapped Go/C/C++/Rust pattern")
    args = ap.parse_args(argv)
    setup_stdout()
    if not args.pattern and not args.rule:
        ap.error("a pattern or --rule is required")
    repo = open_repo(args)
    files = repo.walk()
    lang = LANG_ALIASES.get((args.lang or "").lower(), (args.lang or "").lower()) or None
    rule_cfg, rule_lang = (load_rule(args.rule) if args.rule else (None, None))
    if rule_lang and not lang:
        lang = LANG_ALIASES.get(str(rule_lang).lower(), str(rule_lang).lower())
    if lang:
        files = [f for f in files if repo.langs[f] == lang]

    results = None
    try:
        import ast_grep_py as ag  # noqa: F401
        have_ag = True
    except ImportError:
        have_ag = False
    if have_ag:
        if rule_cfg:
            results = run_ag(repo, files, repo.langs, lambda _ag, _l: [rule_cfg])
        else:
            results = run_ag(repo, files, repo.langs,
                             lambda _ag, l: pattern_configs(_ag, l, args.pattern, args.selector))
    else:
        sg = shutil.which("sg") or shutil.which("ast-grep")
        if sg and (args.rule or args.pattern):
            print(f"q: ast-grep-py not installed, using {sg}", file=sys.stderr)
            results = run_sg_cli(sg, repo, args.pattern, args.rule, lang)
        elif args.pattern:
            print(f"q: ast-grep-py not installed, approximating the pattern with a regex ({INSTALL_HINT})",
                  file=sys.stderr)
            results = run_regex(repo, files, args.pattern)
        else:
            print(f"error: --rule needs ast-grep-py or the sg CLI ({INSTALL_HINT})", file=sys.stderr)
            return 2
    results.sort()
    if args.count:
        counts: dict[str, int] = {}
        for r in results:
            counts[r[0]] = counts.get(r[0], 0) + 1
        if args.json:
            sys.stdout.write(json.dumps(counts, indent=1) + "\n")
        else:
            for rel in sorted(counts):
                sys.stdout.write(f"{rel}\t{counts[rel]}\n")
            sys.stdout.write(f"# {len(results)} matches in {len(counts)} files\n")
        return 0 if results else 1
    if args.json:
        data = [{"file": r[0], "line": r[1], "col": r[2], "end_line": r[3], "text": r[4]} for r in results]
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    else:
        for r in results:
            sys.stdout.write(f"{r[0]}:{r[1]}: {trim(r[4])}\n")
        sys.stdout.write(f"# {len(results)} matches in {len({r[0] for r in results})} files\n")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
