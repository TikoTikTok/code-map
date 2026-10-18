#!/usr/bin/env python3
"""deps.py - what one file imports and who depends on it (fan-out / fan-in), for change-impact questions.

    python deps.py <repo> <file> [--json]

Three sections: the file's imports (resolved to repository files where
possible, external otherwise), the files that import it, and the files that
reference symbols it defines without importing it (PHP autoload, Go packages,
globals, C headers included transitively).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codemap_core import LANG_SHORT, Imp, add_common_args, lang_family, open_repo, setup_stdout  # noqa: E402


def imp_text(imp: Imp) -> str:
    names = ", ".join(imp.names)
    if imp.kind in ("from", "use", "reexport", "import", "require", "dynamic", "source", "mod", "include",
                    "sysinclude"):
        return f"{imp.kind} {imp.module}" + (f" [{names}]" if names else "")
    return f"{imp.module}" + (f" [{names}]" if names else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(ap)
    ap.add_argument("file", help="path inside the repo (absolute, relative, or a unique suffix)")
    args = ap.parse_args(argv)
    setup_stdout()
    repo = open_repo(args)
    infos = repo.load_all()
    rel = repo.find_file(args.file)
    if rel is None or rel not in infos:
        print(f"error: not a code file in the repo: {args.file}", file=sys.stderr)
        return 2
    fi = infos[rel]

    imports = []
    for imp in fi.imports:
        tgt = repo.resolve_import(rel, imp)
        imports.append({"line": imp.line, "module": imp.module, "names": imp.names, "kind": imp.kind,
                        "resolved": tgt if tgt in infos else None,
                        "external": tgt is None})
    importers = []
    for p in sorted(infos):
        if p == rel:
            continue
        for imp in infos[p].imports:
            if repo.resolve_import(p, imp) == rel:
                importers.append({"path": p, "line": imp.line, "module": imp.module, "names": imp.names,
                                  "kind": imp.kind})
    importer_paths = {x["path"] for x in importers}
    defined = sorted(n for n in fi.defined_names()
                     if len(n) >= 3 and any(s.name == n and s.kind != "method" for s in fi.symbols))
    users = []
    for p in sorted(infos):
        if p == rel or p in importer_paths or lang_family(infos[p].lang) != lang_family(fi.lang):
            continue
        own = infos[p].defined_names()
        shared = [(n, infos[p].refs[n][0]) for n in defined if n in infos[p].refs and n not in own]
        if shared:
            users.append({"path": p, "symbols": [{"name": n, "count": c} for n, c in shared]})

    if args.json:
        data = {"path": rel, "lang": fi.lang, "lines": fi.lines, "imports": imports, "imported_by": importers,
                "references_symbols": users}
        sys.stdout.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        return 0

    out = [f"# deps {rel} ({LANG_SHORT.get(fi.lang, fi.lang)}, {fi.lines} lines): "
           f"{len(imports)} imports, {len(importers)} importers, {len(users)} other users"]
    out.append(f"## imports ({len(imports)})")
    if not imports:
        out.append("  (none)")
    width = max([len(x["resolved"] or x["module"]) for x in imports] + [1])
    for x in imports:
        target = x["resolved"] or x["module"]
        tag = "" if x["resolved"] else "  (external)"
        names = ", ".join(x["names"])
        out.append(f"  {x['line']:>4}  {target:<{width}}  {x['kind']}" + (f" [{names}]" if names else "") + tag)
    out.append(f"## imported by ({len(importers)})")
    if not importers:
        out.append("  (none)")
    width = max([len(x["path"]) for x in importers] + [1])
    for x in importers:
        names = ", ".join(x["names"])
        out.append(f"  {x['line']:>4}  {x['path']:<{width}}  {x['kind']} {x['module']}" + (f" [{names}]" if names else ""))
    out.append(f"## references symbols defined here without importing ({len(users)})")
    if not users:
        out.append("  (none)")
    width = max([len(x["path"]) for x in users] + [1])
    for x in users:
        syms = ", ".join(f"{s['name']} x{s['count']}" for s in x["symbols"])
        out.append(f"  {x['path']:<{width}}  {syms}")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
