#!/usr/bin/env python3
"""sym.py - where a symbol is defined and where it is used.

    python sym.py <repo> <name> [--kind function|class|method|any] [--max-refs 40] [--json]

Every definition (file, lines, signature, enclosing class, first docstring
line), then the call sites and references grouped by file with one line of
context each, then ready-to-paste `sed -n 'a,bp' file` commands for the
definition bodies. <name> may be qualified: Class.method or Class::method.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codemap_core import (add_common_args, open_repo, quote_posix, ref_lines, setup_stdout)  # noqa: E402

KINDS = {
    "function": {"function"},
    "class": {"class", "struct", "interface", "trait", "enum", "union", "impl"},
    "method": {"method"},
    "any": None,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("name", help="symbol name, optionally qualified (Class.method)")
    ap.add_argument("--kind", choices=sorted(KINDS), default="any")
    ap.add_argument("--max-refs", type=int, default=40, help="reference lines to print before summarizing")
    args = ap.parse_args(argv)
    setup_stdout()
    repo = open_repo(args)
    infos = repo.load_all()
    query = args.name.replace("::", ".")
    parent, _, name = query.rpartition(".")
    allowed = KINDS[args.kind]

    defs = []
    for p in sorted(infos):
        for s in infos[p].symbols:
            if s.name != name:
                continue
            if parent and s.parent != parent:
                continue
            if allowed is not None and s.kind not in allowed:
                continue
            defs.append((p, s))

    refs: dict[str, list[tuple[int, str, str]]] = {}
    for p in sorted(infos):
        fi = infos[p]
        if name not in fi.refs:
            continue
        text = repo.read_text(p)
        if text is None:
            continue
        lines = text.split("\n")
        hits = []
        for ln, is_call in ref_lines(text, fi.lang, name):
            ctx = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
            if len(ctx) > 100:
                ctx = ctx[:97].rstrip() + "..."
            hits.append((ln, "call" if is_call else "ref", ctx))
        if hits:
            refs[p] = sorted(set(hits))

    n_refs = sum(len(v) for v in refs.values())
    if args.json:
        data = {"name": query,
                "definitions": [dict(path=p, **s.to_dict()) for p, s in defs],
                "references": {p: [{"line": ln, "kind": k, "text": t} for ln, k, t in v] for p, v in refs.items()},
                "sed": [f"sed -n '{s.line},{s.end}p' {quote_posix(p)}" for p, s in defs]}
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return 0 if defs or refs else 1

    out = [f"# sym {query}: {len(defs)} definition{'s' if len(defs) != 1 else ''}, "
           f"{n_refs} reference{'s' if n_refs != 1 else ''} in {len(refs)} file{'s' if len(refs) != 1 else ''}"]
    out.append("## definitions")
    if not defs:
        out.append("  (none)")
    for p, s in defs:
        out.append(f"{p}:{s.line}-{s.end} {s.kind} {s.qual}")
        out.append(f"  {s.sig}")
        if s.doc:
            out.append(f"  doc: {s.doc}")
    out.append(f"## references ({n_refs} in {len(refs)} files)")
    if not refs:
        out.append("  (none)")
    printed = 0
    overflow: list[str] = []
    for p in sorted(refs):
        hits = refs[p]
        if printed >= args.max_refs:
            overflow.append(f"{p} ({len(hits)})")
            continue
        out.append(p)
        for ln, kind, ctx in hits:
            if printed >= args.max_refs:
                left = len(hits) - (hits.index((ln, kind, ctx)))
                out.append(f"  ... +{left} more in this file")
                break
            out.append(f"  {ln} {kind:<4} {ctx}")
            printed += 1
    if overflow:
        out.append(f"  ... more files: {', '.join(overflow)}")
    if defs:
        out.append("## bodies")
        for p, s in defs:
            out.append(f"sed -n '{s.line},{s.end}p' {quote_posix(p)}")
    sys.stdout.write("\n".join(out) + "\n")
    return 0 if defs or refs else 1


if __name__ == "__main__":
    sys.exit(main())
