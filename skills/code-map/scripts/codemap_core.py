#!/usr/bin/env python3
"""codemap_core.py - shared engine for the code-map scripts.

Not a CLI. map.py, sym.py, q.py, slice.py and deps.py import it from the same
folder. It owns: the repository walk (what counts as code, what is skipped),
language detection, the per-file outline (symbols, imports, identifier
references) through one of three engines, the on-disk cache, import resolution,
the file reference graph and its PageRank, and the text helpers every script
shares. Everything it returns is sorted, so the same input gives the same bytes.

Engines, in order of preference:
  ts   tree-sitter via tree-sitter-language-pack (all supported languages)
  ast  Python's stdlib ast (only .py, when tree-sitter is missing)
  rx   a regex outline (every other language, when tree-sitter is missing)
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CORE_VERSION = 1
MAX_TEXT = 2_000_000
INSTALL_HINT = "python -m pip install tree-sitter-language-pack ast-grep-py"

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "third_party",
             "thirdparty", "dist", "build", ".venv", "venv", "__pycache__", "target", ".next",
             ".nuxt", "coverage", "htmlcov", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".gradle", "Pods", ".terraform", "site-packages", ".yarn", ".pnpm",
             ".eggs", ".cache", ".parcel-cache", ".turbo"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "Cargo.lock",
             "poetry.lock", "uv.lock", "Pipfile.lock", "composer.lock", "go.sum", "Gemfile.lock",
             "flake.lock", "packages.lock.json"}
GENERATED_NAME = ("*.min.js", "*.min.mjs", "*.min.css", "*.bundle.js", "*_pb2.py", "*_pb2_grpc.py",
                  "*.pb.go", "*_generated.*", "*.generated.*", "*.g.cs", "*.g.dart", "*.freezed.dart")
GENERATED_MARK = re.compile(r"@generated|do not edit|code generated|auto-?generated|automatically generated",
                            re.I)

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".php": "php", ".phtml": "php",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".inl": "cpp",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
}
SHEBANG_LANG = (("python", "python"), ("node", "javascript"), ("bash", "bash"), ("sh", "bash"),
                ("zsh", "bash"), ("php", "php"))
LANG_SHORT = {"python": "py", "typescript": "ts", "tsx": "tsx", "javascript": "js", "php": "php",
              "go": "go", "rust": "rs", "c": "c", "cpp": "cpp", "bash": "sh"}
LANG_FAMILY = {"typescript": "js", "tsx": "js", "javascript": "js", "cpp": "c"}


def lang_family(lang: str) -> str:
    """Languages whose symbols can refer to each other (ts/tsx/js, c/cpp); everything else is its own."""
    return LANG_FAMILY.get(lang, lang)

CONTAINER_KINDS = {"class", "struct", "interface", "trait", "impl", "enum", "namespace", "union", "module"}
KIND_WEIGHT = {"class": 1.0, "struct": 1.0, "interface": 0.9, "trait": 0.9, "impl": 0.8, "enum": 0.7,
               "namespace": 0.6, "module": 0.6, "union": 0.7, "function": 1.0, "method": 0.6,
               "type": 0.5, "macro": 0.5, "var": 0.3, "const": 0.3}


# ---------------------------------------------------------------- data model

@dataclass
class Sym:
    kind: str
    name: str
    line: int
    end: int
    sig: str
    parent: str = ""
    doc: str = ""

    @property
    def qual(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "name": self.name, "line": self.line, "end": self.end, "sig": self.sig}
        if self.parent:
            d["parent"] = self.parent
        if self.doc:
            d["doc"] = self.doc
        return d

    @staticmethod
    def from_dict(d: dict) -> "Sym":
        return Sym(d["kind"], d["name"], d["line"], d["end"], d["sig"], d.get("parent", ""), d.get("doc", ""))


@dataclass
class Imp:
    module: str
    names: list[str]
    line: int
    kind: str = "import"

    def to_dict(self) -> dict:
        return {"module": self.module, "names": self.names, "line": self.line, "kind": self.kind}

    @staticmethod
    def from_dict(d: dict) -> "Imp":
        return Imp(d["module"], list(d.get("names", [])), d["line"], d.get("kind", "import"))


@dataclass
class FileInfo:
    path: str
    lang: str
    lines: int
    engine: str
    symbols: list[Sym] = field(default_factory=list)
    imports: list[Imp] = field(default_factory=list)
    refs: dict[str, list[int]] = field(default_factory=dict)  # name -> [total, calls]

    def to_dict(self) -> dict:
        return {"path": self.path, "lang": self.lang, "lines": self.lines, "engine": self.engine,
                "symbols": [s.to_dict() for s in self.symbols],
                "imports": [i.to_dict() for i in self.imports],
                "refs": {k: self.refs[k] for k in sorted(self.refs)}}

    @staticmethod
    def from_dict(d: dict) -> "FileInfo":
        return FileInfo(d["path"], d["lang"], d["lines"], d["engine"],
                        [Sym.from_dict(s) for s in d.get("symbols", [])],
                        [Imp.from_dict(i) for i in d.get("imports", [])],
                        {k: list(v) for k, v in d.get("refs", {}).items()})

    def defined_names(self) -> set[str]:
        return {s.name for s in self.symbols}


# ---------------------------------------------------------------- text helpers

def is_text(raw: bytes) -> bool:
    if b"\x00" in raw[:8000]:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def normalize_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def clean_sig(s: str, limit: int = 100) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    s = re.sub(r"\s*(\{|:|;|=>\s*\{?)\s*$", "", s).rstrip()
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def clean_doc(s: str, limit: int = 80) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def line_from_col(lines: list[str], row: int, col_bytes: int) -> str:
    """Text of line `row` (0-based) from UTF-8 byte column `col_bytes` to the end."""
    if row < 0 or row >= len(lines):
        return ""
    return lines[row].encode("utf-8")[col_bytes:].decode("utf-8", errors="replace")


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def quote_posix(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- engine selection

_ENGINE_FORCED = os.environ.get("CODE_MAP_ENGINE", "").strip().lower()
_hint_printed = False


def _load_ts():
    if _ENGINE_FORCED in ("fallback", "ast", "rx", "regex", "stdlib"):
        return None
    try:
        import tree_sitter_language_pack  # noqa: F401
    except Exception:
        return None
    try:
        import codemap_ts  # same folder; sys.path is set by the scripts
        return codemap_ts if codemap_ts.AVAILABLE else None
    except Exception:
        return None


TS = None  # set at the bottom of this module, once every name codemap_ts imports exists


def engine_id() -> str:
    return TS.ENGINE_ID if TS else "stdlib"


def hint_once():
    global _hint_printed
    if TS is None and not _hint_printed:
        _hint_printed = True
        print(f"code-map: tree-sitter not installed, using the stdlib fallback ({INSTALL_HINT})",
              file=sys.stderr)


def outline_text(rel: str, text: str, lang: str) -> FileInfo:
    """Outline one file's text with the best available engine."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        nlines = len(lines) - 1
    else:
        nlines = len(lines)
    if TS is not None and lang in TS.LANGS:
        info = TS.outline(text, lang)
        engine = "ts"
    else:
        hint_once()
        import codemap_fallback
        info, engine = codemap_fallback.outline(text, lang)
    info.path = rel
    info.lang = lang
    info.lines = nlines
    info.engine = engine
    info.symbols.sort(key=lambda s: (s.line, s.end, s.kind, s.name))
    info.imports.sort(key=lambda i: (i.line, i.module, i.kind))
    return info


def ref_lines(text: str, lang: str, name: str) -> list[tuple[int, bool]]:
    """Every line (1-based) on which `name` is referenced, with whether it is a call."""
    if TS is not None and lang in TS.LANGS:
        return TS.ref_lines(text, lang, name)
    import codemap_fallback
    return codemap_fallback.ref_lines(text, lang, name)


# ---------------------------------------------------------------- cache

def cache_dir() -> Path:
    env = os.environ.get("CODE_MAP_CACHE_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "code-map" / "cache"
    return Path.home() / ".cache" / "code-map" / "cache"


class Cache:
    def __init__(self, root: Path, enabled: bool):
        self.enabled = enabled
        self.path = cache_dir() / (hashlib.sha1(root.as_posix().encode("utf-8")).hexdigest() + ".json")
        self.entries: dict[str, dict] = {}
        self.dirty = False
        if enabled:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("v") == CORE_VERSION and data.get("engine") == engine_id():
                    self.entries = data.get("files", {})
            except Exception:
                self.entries = {}

    def get(self, abs_path: str, size: int, mtime_ns: int) -> FileInfo | None:
        if not self.enabled:
            return None
        e = self.entries.get(abs_path)
        if e and e.get("size") == size and e.get("mtime") == mtime_ns:
            try:
                return FileInfo.from_dict(e["info"])
            except Exception:
                return None
        return None

    def put(self, abs_path: str, size: int, mtime_ns: int, info: FileInfo):
        if not self.enabled:
            return
        self.entries[abs_path] = {"size": size, "mtime": mtime_ns, "info": info.to_dict()}
        self.dirty = True

    def save(self):
        if not (self.enabled and self.dirty):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": CORE_VERSION, "engine": engine_id(), "files": self.entries},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- repository

def detect_lang(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in LANG_BY_EXT:
        return LANG_BY_EXT[ext]
    if ext == "" or ext in (".txt",):
        try:
            with open(path, "rb") as fh:
                head = fh.read(160)
        except OSError:
            return None
        if head.startswith(b"#!"):
            first = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
            for key, lang in SHEBANG_LANG:
                if re.search(r"\b" + key + r"\d*(\.exe)?\b", first):
                    return lang
    return None


def _match_glob(rel: str, pat: str) -> bool:
    if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pat):
        return True
    return any(fnmatch.fnmatch(part, pat) for part in rel.split("/")[:-1])


class Repo:
    def __init__(self, root: str | os.PathLike, include=(), exclude=(), use_cache: bool = True):
        self.root = Path(root).resolve()
        self.include = list(include or ())
        self.exclude = list(exclude or ())
        self.cache = Cache(self.root, use_cache)
        self.files: list[str] = []  # rel posix paths of code files, sorted
        self.langs: dict[str, str] = {}
        self._abs: dict[str, Path] = {}
        self._info: dict[str, FileInfo] = {}
        self._by_base: dict[str, list[str]] = {}
        self._dirs: set[str] = set()
        self._walked = False

    # ----- walk
    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    def walk(self) -> list[str]:
        if self._walked:
            return self.files
        self._walked = True
        found: list[str] = []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            for fn in sorted(fns):
                p = Path(dp) / fn
                try:
                    rel = p.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if fn in LOCKFILES or any(fnmatch.fnmatch(fn, g) for g in GENERATED_NAME):
                    continue
                if self.exclude and any(_match_glob(rel, g) for g in self.exclude):
                    continue
                if self.include and not any(_match_glob(rel, g) for g in self.include):
                    continue
                lang = detect_lang(p)
                if lang is None:
                    continue
                found.append(rel)
                self.langs[rel] = lang
                self._abs[rel] = p
        self.files = sorted(found)
        for rel in self.files:
            self._by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
            d = rel.rsplit("/", 1)[0] if "/" in rel else ""
            self._dirs.add(d)
        return self.files

    def abs(self, rel: str) -> Path:
        return self._abs.get(rel) or (self.root / rel)

    def read_text(self, rel: str) -> str | None:
        p = self.abs(rel)
        try:
            if p.is_symlink() or p.stat().st_size > MAX_TEXT:
                return None
            raw = p.read_bytes()
        except OSError:
            return None
        if not is_text(raw):
            return None
        return normalize_text(raw)

    # ----- outlines
    def info(self, rel: str) -> FileInfo | None:
        """Outline of one walked file (cached). None for binary, generated or unreadable files."""
        if rel in self._info:
            return self._info[rel]
        p = self.abs(rel)
        try:
            st = p.stat()
        except OSError:
            return None
        hit = self.cache.get(p.as_posix(), st.st_size, st.st_mtime_ns)
        if hit is not None:
            hit.path = rel
            self._info[rel] = hit
            return hit
        text = self.read_text(rel)
        if text is None:
            return None
        head = "\n".join(text.split("\n", 6)[:6])
        if GENERATED_MARK.search(head):
            return None
        if text.count("\n") < 10 and len(text) > 6000:
            return None  # minified single-line bundle
        lang = self.langs.get(rel) or detect_lang(p)
        if lang is None:
            return None
        info = outline_text(rel, text, lang)
        self.cache.put(p.as_posix(), st.st_size, st.st_mtime_ns, info)
        self._info[rel] = info
        return info

    def load_all(self) -> dict[str, FileInfo]:
        self.walk()
        out: dict[str, FileInfo] = {}
        for rel in self.files:
            fi = self.info(rel)
            if fi is not None:
                out[rel] = fi
        self.cache.save()
        return out

    def find_file(self, needle: str) -> str | None:
        """Resolve a user-supplied path (absolute, relative to root or to cwd, or a suffix) to a rel path."""
        self.walk()
        cands = []
        n = needle.replace("\\", "/")
        p = Path(needle)
        try:
            if p.is_absolute() and p.exists():
                cands.append(self.rel(p))
        except (ValueError, OSError):
            pass
        for base in (self.root, Path.cwd()):
            q = base / needle
            try:
                if q.exists():
                    cands.append(self.rel(q))
            except (ValueError, OSError):
                pass
        for c in cands:
            if c in self.langs:
                return c
        if cands:
            return cands[0]
        if n in self.langs:
            return n
        hits = sorted(r for r in self.files if r == n or r.endswith("/" + n))
        if hits:
            return hits[0]
        base = n.rsplit("/", 1)[-1]
        hits = sorted(self._by_base.get(base, []))
        return hits[0] if hits else None

    # ----- import resolution
    def _by_suffix(self, cand: str) -> str | None:
        base = cand.rsplit("/", 1)[-1]
        hits = [r for r in self._by_base.get(base, []) if r == cand or r.endswith("/" + cand)]
        if not hits:
            return None
        return sorted(hits, key=lambda r: (len(r), r))[0]

    def _exists_rel(self, rel: str) -> str | None:
        rel = os.path.normpath(rel).replace("\\", "/")
        if rel.startswith("../") or rel == "..":
            return None
        if rel in self.langs:
            return rel
        try:
            if (self.root / rel).is_file():
                return rel
        except OSError:
            pass
        return None

    def resolve_import(self, src: str, imp: Imp) -> str | None:
        lang = self.langs.get(src, "")
        srcdir = src.rsplit("/", 1)[0] if "/" in src else ""
        mod = imp.module
        if lang == "python":
            return self._resolve_py(srcdir, mod, imp.names)
        if lang in ("javascript", "typescript", "tsx"):
            return self._resolve_js(srcdir, mod)
        if lang == "php":
            return self._resolve_php(srcdir, mod, imp.kind)
        if lang == "go":
            return self._resolve_go(mod)
        if lang == "rust":
            return self._resolve_rust(src, srcdir, mod, imp.kind)
        if lang in ("c", "cpp"):
            return self._resolve_c(srcdir, mod, imp.kind)
        if lang == "bash":
            return self._resolve_bash(srcdir, mod)
        return None

    def _resolve_py(self, srcdir: str, mod: str, names: list[str]) -> str | None:
        m = re.match(r"^(\.*)(.*)$", mod)
        dots, rest = m.group(1), m.group(2)
        parts = [p for p in rest.split(".") if p]
        if dots:
            base = srcdir
            for _ in range(len(dots) - 1):
                base = base.rsplit("/", 1)[0] if "/" in base else ""
            for tail in (parts, parts[:-1] if parts else None):
                if tail is None:
                    continue
                stem = "/".join([base] + tail) if base else "/".join(tail)
                for cand in ((stem + ".py") if tail else None, (stem + "/__init__.py") if stem else None):
                    if cand:
                        hit = self._exists_rel(cand)
                        if hit:
                            return hit
            for n in names:
                stem = "/".join([x for x in [base] + parts + [n] if x])
                hit = self._exists_rel(stem + ".py") or self._exists_rel(stem + "/__init__.py")
                if hit:
                    return hit
            return None
        if not parts:
            return None
        stem = "/".join(parts)
        for cand in (stem + ".py", stem + "/__init__.py"):
            hit = self._by_suffix(cand)
            if hit:
                return hit
        for n in names:
            hit = self._by_suffix(stem + "/" + n + ".py")
            if hit:
                return hit
        if len(parts) > 1:
            hit = self._by_suffix("/".join(parts[:-1]) + ".py")
            if hit:
                return hit
        return None

    _JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")

    def _resolve_js(self, srcdir: str, mod: str) -> str | None:
        if mod.startswith("@/") or mod.startswith("~/"):
            bases = ["src/" + mod[2:], mod[2:]]
        elif mod.startswith("./") or mod.startswith("../") or mod in (".", ".."):
            bases = [(srcdir + "/" + mod) if srcdir else mod]
        else:
            return None
        for b in bases:
            b = os.path.normpath(b).replace("\\", "/")
            probes = [b] + [b + e for e in self._JS_EXT] + [b + "/index" + e for e in self._JS_EXT]
            stem, dot, ext = b.rpartition(".")
            if dot and ext in ("js", "jsx", "mjs", "cjs"):
                probes += [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
            for cand in probes:
                hit = self._exists_rel(cand)
                if hit and (hit in self.langs or cand == b):
                    return hit
        return None

    def _resolve_php(self, srcdir: str, mod: str, kind: str) -> str | None:
        if kind == "require":
            rel = mod
            if rel.startswith("./") or rel.startswith("../"):
                cand = (srcdir + "/" + rel) if srcdir else rel
                hit = self._exists_rel(cand)
                if hit:
                    return hit
            for cand in ((srcdir + "/" + rel) if srcdir else rel, rel):
                hit = self._exists_rel(cand)
                if hit:
                    return hit
            return self._by_suffix(rel.lstrip("./"))
        parts = [p for p in mod.replace("\\", "/").split("/") if p]
        if not parts:
            return None
        for n in (3, 2, 1):
            if len(parts) >= n:
                hit = self._by_suffix("/".join(parts[-n:]) + ".php")
                if hit:
                    return hit
        return None

    def _resolve_go(self, mod: str) -> str | None:
        parts = [p for p in mod.split("/") if p]
        for n in (3, 2, 1):
            if len(parts) >= n:
                suffix = "/".join(parts[-n:])
                dirs = sorted(d for d in self._dirs if d == suffix or d.endswith("/" + suffix))
                if dirs:
                    d = dirs[0]
                    files = sorted(r for r in self.files if (r.rsplit("/", 1)[0] if "/" in r else "") == d
                                   and r.endswith(".go"))
                    return files[0] if files else None
        return None

    def _resolve_rust(self, src: str, srcdir: str, mod: str, kind: str) -> str | None:
        base = src.rsplit("/", 1)[-1]
        in_mod_root = base in ("mod.rs", "lib.rs", "main.rs")
        here = srcdir if in_mod_root else (srcdir + "/" + base[:-3] if srcdir else base[:-3])
        if kind == "mod":
            for cand in ((here + "/" + mod + ".rs") if here else mod + ".rs", (here + "/" + mod + "/mod.rs") if here else mod + "/mod.rs",
                         (srcdir + "/" + mod + ".rs") if srcdir else mod + ".rs"):
                hit = self._exists_rel(cand)
                if hit:
                    return hit
            return None
        parts = [p for p in re.split(r"::", mod.split("{")[0].strip().rstrip(":")) if p]
        if not parts:
            return None
        if parts[0] == "crate":
            root = srcdir
            while root and not any(self._exists_rel(root + "/" + f) for f in ("lib.rs", "main.rs")):
                root = root.rsplit("/", 1)[0] if "/" in root else ""
            parts = parts[1:]
        elif parts[0] == "super":
            root = srcdir.rsplit("/", 1)[0] if ("/" in srcdir and not in_mod_root) else srcdir
            if not in_mod_root:
                root = srcdir
            else:
                root = srcdir.rsplit("/", 1)[0] if "/" in srcdir else ""
            parts = parts[1:]
        elif parts[0] == "self":
            root = here
            parts = parts[1:]
        else:
            return None
        while parts:
            stem = "/".join([root] + parts) if root else "/".join(parts)
            for cand in (stem + ".rs", stem + "/mod.rs"):
                hit = self._exists_rel(cand)
                if hit:
                    return hit
            parts = parts[:-1]
        return None

    def _resolve_c(self, srcdir: str, mod: str, kind: str) -> str | None:
        if kind == "include":
            cand = (srcdir + "/" + mod) if srcdir else mod
            hit = self._exists_rel(cand)
            if hit:
                return hit
        return self._by_suffix(mod)

    def _resolve_bash(self, srcdir: str, mod: str) -> str | None:
        mod = re.sub(r'^\$\{?(BASH_SOURCE\[0\]|0|__dir|DIR|SCRIPT_DIR)\}?/', "", mod)
        mod = re.sub(r'^"?\$\(dirname[^)]*\)"?/', "", mod)
        cand = (srcdir + "/" + mod) if srcdir else mod
        hit = self._exists_rel(cand)
        if hit:
            return hit
        return self._by_suffix(mod.rsplit("/", 1)[-1])

    # ----- graph
    def graph(self, infos: dict[str, FileInfo]):
        """Weighted file graph: import edges plus symbol-reference edges. Returns (edges, rank)."""
        files = sorted(infos)
        definers: dict[tuple[str, str], list[str]] = {}  # (family, name) -> defining files
        for rel in files:
            fam = lang_family(infos[rel].lang)
            for name in sorted(infos[rel].defined_names()):
                definers.setdefault((fam, name), []).append(rel)
        edges: dict[tuple[str, str], float] = {}

        def add(a: str, b: str, w: float):
            if a != b:
                edges[(a, b)] = edges.get((a, b), 0.0) + w

        for rel in files:
            fi = infos[rel]
            fam = lang_family(fi.lang)
            for imp in fi.imports:
                tgt = self.resolve_import(rel, imp)
                if tgt and tgt in infos:
                    add(rel, tgt, 3.0)
            for name in sorted(fi.refs):
                defs = definers.get((fam, name))
                if not defs:
                    continue
                total = fi.refs[name][0]
                w = math.sqrt(total)
                if len(name) < 4:
                    w *= 0.3
                if name.startswith("_"):
                    w *= 0.2
                w /= math.sqrt(len(defs))
                for d in defs:
                    add(rel, d, w)
        rank = pagerank(files, edges)
        return edges, rank


def pagerank(nodes: list[str], edges: dict[tuple[str, str], float], damping: float = 0.85,
             iters: int = 40) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    out_w: dict[str, float] = {x: 0.0 for x in nodes}
    for (a, _b), w in edges.items():
        out_w[a] += w
    pr = {x: 1.0 / n for x in nodes}
    keys = sorted(edges)
    for _ in range(iters):
        dangling = sum(pr[x] for x in nodes if out_w[x] == 0.0)
        base = (1.0 - damping) / n + damping * dangling / n
        new = {x: base for x in nodes}
        for (a, b) in keys:
            new[b] += damping * pr[a] * edges[(a, b)] / out_w[a]
        pr = new
    return {x: round(pr[x], 9) for x in nodes}


# ---------------------------------------------------------------- shared rendering

def sym_line(sym: Sym, indent: int = 2) -> str:
    extra = 2 if sym.parent else 0
    return f"{' ' * (indent + extra)}{sym.line}-{sym.end} {sym.sig}"


def file_header(fi: FileInfo) -> str:
    return f"{fi.path} ({LANG_SHORT.get(fi.lang, fi.lang)}, {fi.lines} lines)"


def enclosing(symbols: list[Sym], line: int) -> list[Sym]:
    """Symbols whose range contains `line`, outermost first."""
    hits = [s for s in symbols if s.line <= line <= s.end]
    hits.sort(key=lambda s: (s.line, -s.end))
    return hits


def setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:
        pass


def add_common_args(ap, repo: bool = True):
    if repo:
        ap.add_argument("repo", help="repository root")
        ap.add_argument("--include", action="append", default=[], metavar="GLOB",
                        help="only files matching this glob (repeatable)")
        ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip files matching this glob (repeatable)")
        ap.add_argument("--no-cache", action="store_true", help="ignore and do not write the outline cache")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")


def open_repo(args) -> Repo:
    root = Path(args.repo)
    if not root.is_dir():
        print(f"error: not a folder: {root}", file=sys.stderr)
        sys.exit(2)
    return Repo(root, include=args.include, exclude=args.exclude, use_cache=not args.no_cache)


TS = _load_ts()
