#!/usr/bin/env python3
"""slice.py - exact line ranges of a file, numbered, with the enclosing scope headers on top.

    python slice.py <file>:<a>-<b>[,<c>-<d>] [<file2>:<e>-<f> ...] [--repo DIR] [-C N] [--json]
    python slice.py <file>                      # no ranges: outline of the file

Prints only the requested lines (prefixed with their numbers and `|`) under the
class / function lines that enclose them (prefixed with `>`), so a model gets
context without the whole file. Paths are printed as given, forward slashes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codemap_core import (LANG_SHORT, detect_lang, enclosing, normalize_text, outline_text, setup_stdout,  # noqa: E402
                          sym_line)

SPEC_RE = re.compile(r"^(?P<file>.+?)(?::(?P<ranges>\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*))?$")


def parse_spec(spec: str):
    m = SPEC_RE.match(spec)
    if not m:
        raise ValueError(spec)
    file = m.group("file")
    ranges = []
    if m.group("ranges"):
        for part in m.group("ranges").split(","):
            a, _, b = part.partition("-")
            a = int(a)
            b = int(b) if b else a
            if b < a:
                a, b = b, a
            ranges.append((a, b))
    return file, ranges


def merge(ranges: list[tuple[int, int]], n: int, ctx: int) -> list[tuple[int, int]]:
    rs = sorted((max(1, a - ctx), min(n, b + ctx)) for a, b in ranges)
    out: list[tuple[int, int]] = []
    for a, b in rs:
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("specs", nargs="+", help="file:a-b[,c-d] (or just file for an outline)")
    ap.add_argument("--repo", help="base folder for relative paths (default: current folder)")
    ap.add_argument("-C", "--context", type=int, default=0, help="extra lines around each range")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)
    setup_stdout()
    base = Path(args.repo) if args.repo else Path.cwd()
    out: list[str] = []
    data = []
    rc = 0
    for spec in args.specs:
        try:
            file, ranges = parse_spec(spec)
        except ValueError:
            print(f"error: bad spec: {spec}", file=sys.stderr)
            rc = 2
            continue
        shown = file.replace("\\", "/")
        p = Path(file)
        if not p.is_absolute():
            p = base / p
        try:
            raw = p.read_bytes()
        except OSError:
            print(f"error: cannot read {file}", file=sys.stderr)
            rc = 2
            continue
        text = normalize_text(raw)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lang = detect_lang(p) or "text"
        info = outline_text(shown, text, lang) if lang != "text" else None
        symbols = info.symbols if info else []
        n = len(lines)
        head = f"== {shown} ({LANG_SHORT.get(lang, lang)}, {n} lines)"
        if not ranges:
            out.append(head)
            if not symbols:
                out.append("  (no symbols found)")
            for s in symbols:
                out.append(sym_line(s))
            data.append({"file": shown, "lang": lang, "lines": n, "symbols": [s.to_dict() for s in symbols]})
            continue
        rs = merge(ranges, n, args.context)
        out.append(head)
        width = len(str(n))
        printed_scope: set[int] = set()
        chunks = []
        for idx, (a, b) in enumerate(rs):
            if idx > 0:
                out.append(f"{'':>{width}}   ...")
            scope = [s for s in enclosing(symbols, a) if s.line < a]
            scope_lines = []
            for s in scope:
                if s.line not in printed_scope:
                    out.append(f"{s.line:>{width}} > {lines[s.line - 1].rstrip()}")
                    printed_scope.add(s.line)
                scope_lines.append(s.line)
            body = []
            for ln in range(a, b + 1):
                out.append(f"{ln:>{width}} | {lines[ln - 1].rstrip()}")
                body.append(lines[ln - 1].rstrip())
            chunks.append({"start": a, "end": b, "scope": scope_lines, "lines": body})
        data.append({"file": shown, "lang": lang, "lines": n, "chunks": chunks})
    if args.json:
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    elif out:
        sys.stdout.write("\n".join(out) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
