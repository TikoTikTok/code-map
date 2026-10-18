#!/usr/bin/env python3
"""map.py - a token-budgeted repository map: the files that matter most, with their symbols and line ranges.

    python map.py <repo> [--budget 1500] [--focus path-or-symbol ...] [--include GLOB] [--exclude GLOB] [--json]

Files are ranked by PageRank over a reference graph (imports plus symbol
references). Symbols are then picked by file rank, kind and how many other
files mention them, until the budget (tokens = chars / 4) is spent. --focus
forces a file (every symbol) or a symbol (its definitions) into the map.
Output is sorted and stable: same input, same bytes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codemap_core import (CONTAINER_KINDS, KIND_WEIGHT, FileInfo, Sym, _match_glob, add_common_args,  # noqa: E402
                          file_header, lang_family, open_repo, setup_stdout, sym_line, tokens)


def focus_matches(fi: FileInfo, focus: list[str]) -> tuple[bool, set[int]]:
    """(whole file forced, indexes of forced symbols)."""
    whole = False
    syms: set[int] = set()
    for f in focus:
        f = f.replace("\\", "/")
        low = f.lower()
        if _match_glob(fi.path, f) or fi.path.lower().endswith(low) or low in fi.path.lower():
            whole = True
        for i, s in enumerate(fi.symbols):
            if s.name == f or s.qual == f or s.qual.replace(".", "::") == f:
                syms.add(i)
    return whole, syms


def build(repo, infos: dict[str, FileInfo], rank: dict[str, float], budget_tokens: int, focus: list[str]):
    budget = budget_tokens * 4
    files = sorted(infos, key=lambda p: (-rank.get(p, 0.0), p))
    refs_by_name: dict[tuple[str, str], set[str]] = {}  # (family, name) -> files mentioning it
    for p, fi in infos.items():
        fam = lang_family(fi.lang)
        for n in fi.refs:
            refs_by_name.setdefault((fam, n), set()).add(p)
    # index of the container symbol for each member, by (file, idx)
    container: dict[tuple[str, int], int] = {}
    for p, fi in infos.items():
        open_by_name: dict[str, int] = {}
        for i, s in enumerate(fi.symbols):
            if s.parent:
                c = open_by_name.get(s.parent)
                if c is not None and fi.symbols[c].line <= s.line <= fi.symbols[c].end:
                    container[(p, i)] = c
            if s.kind in CONTAINER_KINDS:
                open_by_name[s.name] = i
    definers: dict[tuple[str, str], int] = {}
    for fi in infos.values():
        fam = lang_family(fi.lang)
        for n in fi.defined_names():
            definers[(fam, n)] = definers.get((fam, n), 0) + 1
    cands = []
    forced: set[tuple[str, int]] = set()
    for p in files:
        fi = infos[p]
        fam = lang_family(fi.lang)
        whole, fsyms = focus_matches(fi, focus) if focus else (False, set())
        scored = []
        for i, s in enumerate(fi.symbols):
            ext = len(refs_by_name.get((fam, s.name), set()) - {p})
            score = rank.get(p, 0.0) * KIND_WEIGHT.get(s.kind, 0.5) * (1.0 + math.log2(1 + ext))
            if len(s.name) <= 2:
                score *= 0.2  # `$`, `n`, `h`: too common to be a landmark
            elif len(s.name) == 3:
                score *= 0.6
            if s.end == s.line and s.kind not in CONTAINER_KINDS:
                score *= 0.7  # one-liners
            score /= math.sqrt(definers.get((fam, s.name), 1))  # names defined in many files are ambiguous
            if whole or i in fsyms:
                forced.add((p, i))
            scored.append((-score, s.line, i))
        scored.sort()
        for ordinal, (neg, line, i) in enumerate(scored):
            # later symbols of the same file give way to the first symbols of other files
            cands.append((neg / (1.0 + 0.12 * ordinal), p, line, i))
    cands.sort()
    chosen: set[tuple[str, int]] = set()
    files_in: set[str] = set()
    size = 0

    def cost(p: str, i: int) -> int:
        fi = infos[p]
        c = len(sym_line(fi.symbols[i])) + 1
        if p not in files_in:
            c += len(file_header(fi)) + 1 + 24  # header plus the "... +N more" reserve
        ci = container.get((p, i))
        if ci is not None and (p, ci) not in chosen:
            c += len(sym_line(fi.symbols[ci])) + 1
        return c

    def take(p: str, i: int):
        nonlocal size
        size += cost(p, i)
        ci = container.get((p, i))
        if ci is not None:
            chosen.add((p, ci))
        chosen.add((p, i))
        files_in.add(p)

    for key in cands:
        p, i = key[1], key[3]
        if (p, i) in forced and (p, i) not in chosen:
            take(p, i)
    skips = 0
    for key in cands:
        p, i = key[1], key[3]
        if (p, i) in chosen:
            continue
        if size + cost(p, i) <= budget:
            take(p, i)
        else:
            skips += 1
            if skips > 400:
                break
    out_lines: list[str] = []
    shown_files = []
    shown_syms = 0
    for pos, p in enumerate(files, 1):
        if p not in files_in:
            continue
        fi = infos[p]
        idxs = sorted(i for (q, i) in chosen if q == p)
        out_lines.append(file_header(fi))
        shown = []
        for i in idxs:
            out_lines.append(sym_line(fi.symbols[i]))
            shown.append(fi.symbols[i])
        hidden = len(fi.symbols) - len(idxs)
        if hidden > 0:
            out_lines.append(f"  ... +{hidden} more symbols")
        shown_syms += len(idxs)
        shown_files.append({"path": p, "lang": fi.lang, "lines": fi.lines, "rank": pos,
                            "score": round(rank.get(p, 0.0), 6), "symbols": [s.to_dict() for s in shown],
                            "hidden": hidden})
    not_shown = [p for p in files if p not in files_in]
    body = "\n".join(out_lines)
    used = tokens(body)
    total_syms = sum(len(fi.symbols) for fi in infos.values())
    head = (f"# code-map {repo.root.name}: {len(infos)} code files ({len(shown_files)} shown), "
            f"{total_syms} symbols ({shown_syms} shown); budget {budget_tokens} tok, used {used} tok")
    tail = ""
    if not_shown:
        names = ", ".join(not_shown[:8])
        more = f" and {len(not_shown) - 8} more" if len(not_shown) > 8 else ""
        tail = f"# not shown ({len(not_shown)} files, by rank): {names}{more}"
    text = head + "\n" + body + ("\n" + tail if tail else "") + "\n"
    data = {"root": repo.root.name, "budget_tokens": budget_tokens, "used_tokens": used,
            "files": shown_files, "not_shown": not_shown}
    return text, data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("--budget", type=int, default=1500, help="token budget for the map body (chars/4)")
    ap.add_argument("--focus", action="append", default=[], metavar="PATH|SYMBOL",
                    help="always include this file (all symbols) or symbol (repeatable)")
    args = ap.parse_args(argv)
    setup_stdout()
    repo = open_repo(args)
    infos = repo.load_all()
    if not infos:
        print("# code-map: no code files found", file=sys.stderr)
        return 1
    _edges, rank = repo.graph(infos)
    text, data = build(repo, infos, rank, args.budget, args.focus)
    if args.json:
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
