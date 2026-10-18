#!/usr/bin/env python3
"""codemap_fallback.py - stdlib engines for code-map.

Used when tree-sitter-language-pack is not installed: Python files go through
the stdlib `ast` module (exact), every other language through a regex outline
(good enough for maps and slices, honest about being approximate: the cache and
the --json output label it "rx").
"""
from __future__ import annotations

import ast
import re

from codemap_core import FileInfo, Imp, Sym, clean_doc, clean_sig, line_from_col


def outline(text: str, lang: str) -> tuple[FileInfo, str]:
    if lang == "python":
        try:
            return _py_outline(text), "ast"
        except (SyntaxError, ValueError, RecursionError):
            pass
    return _rx_outline(text, lang), "rx"


def ref_lines(text: str, lang: str, name: str) -> list[tuple[int, bool]]:
    if lang == "python":
        try:
            return _py_ref_lines(text, name)
        except (SyntaxError, ValueError, RecursionError):
            pass
    return _rx_ref_lines(text, lang, name)


# ---------------------------------------------------------------- python via ast

def _py_doc(node) -> str:
    d = ast.get_docstring(node, clean=False)
    if not d:
        return ""
    for ln in d.split("\n"):
        if ln.strip():
            return clean_doc(ln)
    return ""


def _py_outline(text: str) -> FileInfo:
    tree = ast.parse(text)
    lines = text.split("\n")
    syms: list[Sym] = []
    imps: list[Imp] = []

    def sig(node, limit: int = 100) -> str:
        text = line_from_col(lines, node.lineno - 1, node.col_offset)
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and body[0].lineno == node.lineno:
            text = lines[node.lineno - 1].encode("utf-8")[node.col_offset: body[0].col_offset].decode("utf-8", "replace")
        return clean_sig(text, limit)

    def imports_in(body):
        for node in body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    imps.append(Imp(a.name, [], node.lineno))
            elif isinstance(node, ast.ImportFrom):
                mod = "." * (node.level or 0) + (node.module or "")
                imps.append(Imp(mod, [a.name for a in node.names], node.lineno, "from"))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            syms.append(Sym("function", node.name, node.lineno, node.end_lineno, sig(node), "", _py_doc(node)))
        elif isinstance(node, ast.ClassDef):
            syms.append(Sym("class", node.name, node.lineno, node.end_lineno, sig(node), "", _py_doc(node)))
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    syms.append(Sym("method", m.name, m.lineno, m.end_lineno, sig(m), node.name, _py_doc(m)))
                elif isinstance(m, ast.ClassDef):
                    syms.append(Sym("class", m.name, m.lineno, m.end_lineno, sig(m), node.name, _py_doc(m)))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    syms.append(Sym("var", t.id, node.lineno, node.end_lineno, sig(node, 60)))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            syms.append(Sym("var", node.target.id, node.lineno, node.end_lineno, sig(node, 60)))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports_in([node])
        elif isinstance(node, ast.If):
            imports_in(node.body)
            imports_in(node.orelse)
        elif isinstance(node, ast.Try):
            imports_in(node.body)
            for h in node.handlers:
                imports_in(h.body)
            imports_in(node.orelse)
    refs: dict[str, list[int]] = {}

    def bump(name: str, call: bool = False):
        if not name:
            return
        e = refs.get(name)
        if e is None:
            e = refs[name] = [0, 0]
        e[0] += 1
        if call:
            e[1] += 1

    call_sites: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_sites.add(id(node.func))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            bump(node.id, id(node) in call_sites)
        elif isinstance(node, ast.Attribute):
            bump(node.attr, id(node) in call_sites)
        elif isinstance(node, ast.arg):
            bump(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            bump(node.arg)
        elif isinstance(node, ast.alias):
            for part in node.name.split("."):
                bump(part)
            if node.asname:
                bump(node.asname)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bump(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                bump(n)
    return FileInfo("", "python", 0, "ast", syms, imps, refs)


def _py_ref_lines(text: str, name: str) -> list[tuple[int, bool]]:
    tree = ast.parse(text)
    call_sites: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_sites.add(id(node.func))
    out: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            out.append((node.lineno, id(node) in call_sites))
        elif isinstance(node, ast.Attribute) and node.attr == name:
            out.append((node.end_lineno or node.lineno, id(node) in call_sites))
        elif isinstance(node, ast.arg) and node.arg == name:
            out.append((node.lineno, False))
        elif isinstance(node, ast.keyword) and node.arg == name:
            out.append((node.lineno, False))
        elif isinstance(node, ast.alias) and (name in node.name.split(".") or node.asname == name):
            out.append((node.lineno, False))
    return sorted(set(out))


# ---------------------------------------------------------------- regex outline

_STR = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\\n])*`')
_WORD = re.compile(r"[A-Za-z_$][\w$]*")

_KW_COMMON = {"if", "else", "for", "while", "do", "return", "break", "continue", "switch", "case", "default",
              "new", "delete", "this", "null", "true", "false", "in", "of", "typeof", "instanceof", "void",
              "try", "catch", "finally", "throw", "throws", "class", "struct", "enum", "interface", "function",
              "fn", "func", "def", "let", "const", "var", "static", "public", "private", "protected", "import",
              "export", "from", "as", "use", "mod", "pub", "impl", "trait", "type", "package", "go", "defer",
              "chan", "select", "range", "map", "nil", "mut", "ref", "self", "Self", "super", "crate", "match",
              "loop", "where", "async", "await", "yield", "with", "not", "and", "or", "is", "elif", "lambda",
              "pass", "None", "True", "False", "sizeof", "unsigned", "signed", "int", "char", "long", "short",
              "float", "double", "bool", "string", "namespace", "using", "template", "typename", "virtual",
              "override", "extends", "implements", "abstract", "final", "readonly", "declare", "then", "fi",
              "esac", "done", "local", "echo", "exit", "extern", "inline", "operator", "goto", "require",
              "include", "require_once", "include_once", "echo", "print", "unset", "isset", "empty", "array",
              "foreach", "endforeach", "endif", "endwhile", "declare", "global", "yield", "u8", "u16", "u32",
              "u64", "i8", "i16", "i32", "i64", "usize", "isize", "f32", "f64", "str", "String", "Vec", "Box",
              "Option", "Some", "Ok", "Err", "Result"}

_COMMENT = {
    "python": ("#",), "bash": ("#",), "php": ("//", "#"),
    "javascript": ("//",), "typescript": ("//",), "tsx": ("//",),
    "go": ("//",), "rust": ("//",), "c": ("//",), "cpp": ("//",),
}


def _blank_strings(line: str) -> str:
    return _STR.sub(lambda m: " " * len(m.group(0)), line)


def _code_only(line: str, lang: str) -> str:
    """The line with string contents and trailing comments replaced by spaces (columns preserved)."""
    s = _blank_strings(line)
    for marker in _COMMENT.get(lang, ("//",)):
        i = s.find(marker)
        if i >= 0:
            s = s[:i] + " " * (len(s) - i)
    i = s.find("/*")
    if i >= 0 and lang != "python" and lang != "bash":
        j = s.find("*/", i + 2)
        end = len(s) if j < 0 else j + 2
        s = s[:i] + " " * (end - i) + s[end:]
    return s


def _brace_end(lines: list[str], start: int, lang: str, lookahead: int = 3) -> int:
    """0-based index of the line that closes the block opened on/after `start`; `start` if none opens.

    Depth is checked at the end of each line, so braces that open and close inside the definition
    line itself (destructured parameters, one-line bodies, `= { a: 1 };`) do not end the block early."""
    depth = 0
    opened = False
    for i in range(start, len(lines)):
        s = _code_only(lines[i], lang)
        if not opened and i - start >= lookahead:
            return start
        if not opened and ";" in s and "{" not in s:
            return start
        for ch in s:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}" and opened:
                depth -= 1
        if opened and depth <= 0:
            return i
    return len(lines) - 1 if opened else start


def _indent_end(lines: list[str], start: int) -> int:
    base = len(lines[start]) - len(lines[start].lstrip())
    last = start
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip()) <= base:
            break
        last = i
    return last


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def _cut_body(sig: str) -> str:
    """Drop the body that starts on the signature line: the `{` after the last `)` (or the first `{`)."""
    close = sig.rfind(")")
    i = sig.find("{", close + 1 if close >= 0 else 0)
    if i < 0:
        i = sig.find("{")
    return sig[:i].rstrip() if i >= 0 else sig


_RX_TOP = {
    "python": [
        ("function", re.compile(r"^(?:async\s+)?def\s+(?P<name>\w+)")),
        ("class", re.compile(r"^class\s+(?P<name>\w+)")),
        ("var", re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?::[^=]+)?=[^=]")),
    ],
    "js": [
        ("function", re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*(?P<name>[\w$]+)")),
        ("class", re.compile(r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(?P<name>[\w$]+)")),
        ("interface", re.compile(r"^(?:export\s+)?interface\s+(?P<name>\w+)")),
        ("type", re.compile(r"^(?:export\s+)?type\s+(?P<name>\w+)\s*(?:<[^=]*)?=")),
        ("enum", re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>\w+)")),
        ("namespace", re.compile(r"^(?:export\s+)?(?:declare\s+)?namespace\s+(?P<name>[\w.]+)")),
        ("function", re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[\w$]+)\s*(?::[^=]+?)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::\s*[^=]+?)?=>")),
        ("function", re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[\w$]+)\s*(?::[^=]+?)?=\s*(?:async\s+)?function\b")),
        ("var", re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>[\w$]+)\s*(?::[^=]+?)?=(?!.*\brequire\s*\()(?!.*\bimport\s*\()")),
        ("function", re.compile(r"^(?:module\.)?exports\.(?P<name>[\w$]+)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)")),
    ],
    "php": [
        ("namespace", re.compile(r"^namespace\s+(?P<name>[\w\\]+)")),
        ("class", re.compile(r"^(?:(?:abstract|final|readonly)\s+)*class\s+(?P<name>\w+)")),
        ("interface", re.compile(r"^interface\s+(?P<name>\w+)")),
        ("trait", re.compile(r"^trait\s+(?P<name>\w+)")),
        ("enum", re.compile(r"^enum\s+(?P<name>\w+)")),
        ("function", re.compile(r"^function\s+&?(?P<name>\w+)\s*\(")),
        ("const", re.compile(r"^const\s+(?P<name>\w+)\s*=")),
    ],
    "go": [
        ("method", re.compile(r"^func\s+\((?:\w+\s+)?\*?(?P<parent>\w+)[^)]*\)\s+(?P<name>\w+)\s*\(")),
        ("function", re.compile(r"^func\s+(?P<name>\w+)\s*[(\[]")),
        ("struct", re.compile(r"^type\s+(?P<name>\w+)(?:\[[^\]]*\])?\s+struct\b")),
        ("interface", re.compile(r"^type\s+(?P<name>\w+)(?:\[[^\]]*\])?\s+interface\b")),
        ("type", re.compile(r"^type\s+(?P<name>\w+)\s+\S")),
        ("var", re.compile(r"^var\s+(?P<name>\w+)")),
        ("const", re.compile(r"^const\s+(?P<name>\w+)")),
    ],
    "rust": [
        ("function", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern\s+\"\w+\")\s+)*fn\s+(?P<name>\w+)")),
        ("struct", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?struct\s+(?P<name>\w+)")),
        ("enum", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?enum\s+(?P<name>\w+)")),
        ("union", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?union\s+(?P<name>\w+)")),
        ("trait", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+(?P<name>\w+)")),
        ("impl", re.compile(r"^(?:unsafe\s+)?impl(?:<[^>]*>)?\s+(?:(?P<trait>[\w:]+(?:<[^>]*>)?)\s+for\s+)?(?P<name>[\w:]+)")),
        ("module", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>\w+)")),
        ("const", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(?:mut\s+)?(?P<name>\w+)\s*:")),
        ("type", re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?type\s+(?P<name>\w+)")),
        ("macro", re.compile(r"^macro_rules!\s+(?P<name>\w+)")),
    ],
    "c": [
        ("macro", re.compile(r"^#\s*define\s+(?P<name>\w+)")),
        ("namespace", re.compile(r"^namespace\s+(?P<name>\w+)\s*\{")),
        ("struct", re.compile(r"^(?:typedef\s+)?struct\s+(?P<name>\w+)\s*(?:final\s*)?(?::[^{]*)?\{")),
        ("class", re.compile(r"^(?:typedef\s+)?class\s+(?P<name>\w+)\s*(?:final\s*)?(?::[^{]*)?\{")),
        ("union", re.compile(r"^(?:typedef\s+)?union\s+(?P<name>\w+)\s*\{")),
        ("enum", re.compile(r"^(?:typedef\s+)?enum(?:\s+class|\s+struct)?\s+(?P<name>\w+)\s*(?::[^{]*)?\{")),
        ("type", re.compile(r"^typedef\s+(?!struct\s*\{|union\s*\{|enum\s*\{).*?\b(?P<name>\w+)\s*(?:\[[^\]]*\])?\s*;")),
        ("type", re.compile(r"^using\s+(?P<name>\w+)\s*=")),
        ("function", re.compile(r"^(?!(?:if|for|while|switch|return|else|do|case|sizeof|typedef|static_assert|using|namespace|struct|class|enum|union|template|#)\b)(?:[\w:<>,~*&\[\]\s]+?[\s*&])?(?P<name>~?[\w:]+|operator\S*)\s*\((?P<rest>.*)$")),
    ],
    "bash": [
        ("function", re.compile(r"^function\s+(?P<name>[\w-]+)")),
        ("function", re.compile(r"^(?P<name>[\w-]+)\s*\(\)")),
        ("var", re.compile(r"^(?:export\s+|declare\s+(?:-\w+\s+)*|readonly\s+)?(?P<name>[A-Za-z_]\w*)=")),
    ],
}
_RX_MEMBER = {
    "js": re.compile(r"^(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set|declare)\s+)*\*?\s*(?P<name>[\w$#]+)\s*(?:<[^>]*>)?\s*\((?P<rest>.*)$"),
    "php": re.compile(r"^(?:(?:public|private|protected|static|abstract|final)\s+)*function\s+&?(?P<name>\w+)\s*\("),
    "rust": re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern\s+\"\w+\")\s+)*fn\s+(?P<name>\w+)"),
    "c": re.compile(r"^(?!(?:if|for|while|switch|return|else|do|case|sizeof|typedef|static_assert|using|namespace|struct|class|enum|union|#|public|private|protected)\b)(?:[\w:<>,~*&\[\]\s]+?[\s*&])?(?P<name>~?[\w:]+|operator\S*)\s*\((?P<rest>.*)$"),
    "python": re.compile(r"^(?:async\s+)?def\s+(?P<name>\w+)"),
}
_RX_IMPORT = {
    "python": [
        (re.compile(r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)"), "import"),
        (re.compile(r"^\s*from\s+([.\w]+)\s+import\s+\(?([^)#]+)"), "from"),
    ],
    "js": [
        (re.compile(r"^\s*import\s+(?:type\s+)?(.*?)\s+from\s+['\"]([^'\"]+)['\"]"), "import"),
        (re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]"), "import-bare"),
        (re.compile(r"^\s*export\s+(.*?)\s+from\s+['\"]([^'\"]+)['\"]"), "reexport"),
        (re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"), "require"),
        (re.compile(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"), "dynamic"),
    ],
    "php": [
        (re.compile(r"^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;"), "use"),
        (re.compile(r"\b(?:require|include)(?:_once)?\b\s*\(?\s*(.+?)\s*\)?\s*;"), "require"),
    ],
    "go": [(re.compile(r"^\s*(?:import\s+)?(?:(\w+)\s+)?\"([^\"]+)\"\s*$"), "import")],
    "rust": [(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);"), "use"),
             (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;"), "mod")],
    "c": [(re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]"), "include")],
    "bash": [(re.compile(r"^\s*(?:source|\.)\s+(\S+)"), "source")],
}


def _family(lang: str) -> str:
    if lang in ("javascript", "typescript", "tsx"):
        return "js"
    if lang == "cpp":
        return "c"
    return lang


def _doc_above(lines: list[str], i: int) -> str:
    """First line of the comment block directly above line index i."""
    j = i - 1
    block: list[str] = []
    while j >= 0:
        s = lines[j].strip()
        if not s:
            break
        if s.startswith(("///", "//!", "//", "#")) and not s.startswith("#!") and not s.startswith("#["):
            block.append(s)
            j -= 1
            continue
        if s.endswith("*/"):
            k = j
            while k >= 0 and "/*" not in lines[k]:
                k -= 1
            if k < 0:
                break
            body = "\n".join(lines[k: j + 1]).strip()
            body = re.sub(r"^/\*+!?", "", body)
            body = re.sub(r"\*+/$", "", body)
            for ln in body.split("\n"):
                ln = re.sub(r"^\s*\*+\s?", "", ln).strip()
                if ln:
                    return clean_doc(ln)
            return ""
        break
    if not block:
        return ""
    t = re.sub(r"^(///?!?|//|#)\s?", "", block[-1])
    return clean_doc(t)


def _rx_outline(text: str, lang: str) -> FileInfo:
    fam = _family(lang)
    lines = text.split("\n")
    syms: list[Sym] = []
    imps: list[Imp] = []
    defpos: set[tuple[int, int]] = set()
    rules = _RX_TOP.get(fam, [])
    member_rx = _RX_MEMBER.get(fam)
    i = 0
    n = len(lines)
    in_go_block: str | None = None
    while i < n:
        raw = lines[i]
        s = raw.strip()
        code = _code_only(raw, lang).strip()
        if not code:
            i += 1
            continue
        if fam == "go":
            if re.match(r"^(type|var|const|import)\s*\($", code):
                in_go_block = code.split()[0]
                i += 1
                continue
            if in_go_block and code == ")":
                in_go_block = None
                i += 1
                continue
            if in_go_block in ("type", "var", "const") and _indent(raw) > 0:
                m = re.match(r"^(?P<name>\w+)(?:\[[^\]]*\])?\s+(?P<what>struct\b|interface\b|\S+)?", code)
                if m and in_go_block == "type":
                    kind = {"struct": "struct", "interface": "interface"}.get((m.group("what") or "").rstrip(), "type")
                    end = _brace_end(lines, i, lang) if kind in ("struct", "interface") else i
                    syms.append(Sym(kind, m.group("name"), i + 1, end + 1, clean_sig(s), "", _doc_above(lines, i)))
                    i = max(i, end) + 1 if end > i else i + 1
                    continue
                if m and in_go_block in ("var", "const"):
                    syms.append(Sym(in_go_block, m.group("name"), i + 1, i + 1, clean_sig(s)))
                i += 1
                continue
        matched = False
        for kind, rx in rules:
            if _indent(raw) > 0 and fam != "c":
                break
            m = rx.match(code)
            if not m:
                continue
            if fam == "c" and kind == "function":
                joined = " ".join(_code_only(x, lang).strip() for x in lines[i: i + 3])
                if ";" in joined.split("{")[0]:
                    continue
                if "{" not in joined:
                    continue
            name = m.group("name")
            parent = m.groupdict().get("parent") or ""
            if fam == "c" and "::" in name and not name.startswith("operator"):
                parent, _, name = name.rpartition("::")
                if kind == "function":
                    kind = "method"
            if fam == "rust" and kind == "impl":
                trait = m.groupdict().get("trait")
                label = f"{trait} for {name}" if trait else name
            else:
                label = name
            if fam == "python":
                end = _indent_end(lines, i)
            elif kind in ("var", "const", "type", "macro") and not (fam == "js" and "{" in code and "}" not in code):
                end = i
                while fam == "c" and kind == "macro" and lines[end].rstrip().endswith("\\") and end + 1 < n:
                    end += 1
            else:
                end = _brace_end(lines, i, lang)
            if kind not in ("var", "const", "macro") and "{" in code:
                s = _cut_body(s)
            elif fam == "js" and kind == "function" and "=>" in code and not code.startswith(("function", "export function", "export default function", "export async function", "async function")):
                s = s[: s.index("=>")].rstrip()
            if fam == "python" and kind == "class":
                doc = _py_rx_doc(lines, i)
            elif fam == "python" and kind == "function":
                doc = _py_rx_doc(lines, i)
            else:
                doc = _doc_above(lines, i) if kind not in ("var", "const", "macro") else ""
            sym = Sym(kind, name, i + 1, end + 1, clean_sig(s, 60 if kind in ("var", "const") else 100), parent, doc)
            if fam == "rust" and kind == "impl":
                sym.sig = clean_sig(s)
                sym.name = name.split("<")[0].rsplit("::", 1)[-1]
                label = sym.name
            syms.append(sym)
            defpos.add((i, _code_only(raw, lang).find(name, m.start("name") if fam != "c" else 0)))
            container = kind in ("class", "struct", "interface", "trait", "impl", "union", "enum", "namespace", "module")
            if container and end > i and member_rx is not None and kind not in ("enum", "union", "namespace", "module"):
                _rx_members(lines, i, end, lang, fam, member_rx, sym, syms, defpos)
            if container and kind in ("namespace", "module"):
                pass  # members of namespaces are scanned as top-level lines below
            elif end > i and kind not in ("namespace", "module"):
                i = end
            matched = True
            break
        if not matched or fam == "c":
            for rx, kind in _RX_IMPORT.get(fam, []):
                m = rx.search(code if kind not in ("require",) else _blank_strings(raw)) if False else rx.search(raw)
                if not m:
                    continue
                _rx_import(imps, fam, kind, m, i + 1, raw)
        i += 1
    refs: dict[str, list[int]] = {}
    for row, raw in enumerate(lines):
        code = _code_only(raw, lang)
        for m in _WORD.finditer(code):
            w = m.group(0)
            if w in _KW_COMMON or (row, m.start()) in defpos:
                continue
            e = refs.get(w)
            if e is None:
                e = refs[w] = [0, 0]
            e[0] += 1
            if _rx_is_call(code, m, fam):
                e[1] += 1
    return FileInfo("", lang, 0, "rx", syms, imps, refs)


def _rx_is_call(code: str, m, fam: str) -> bool:
    if fam == "bash":
        return code[: m.start()].strip() in ("", "|", "&&", "||", ";", "{", "(", "$(") and not code[m.end():].startswith("=")
    rest = code[m.end():]
    return bool(re.match(r"\s*\(", rest)) or (fam == "rust" and rest.startswith("!("))


def _py_rx_doc(lines: list[str], i: int) -> str:
    j = i + 1
    while j < len(lines) and lines[j].strip().endswith(":") is False and not lines[j].strip():
        j += 1
    if j < len(lines):
        s = lines[j].strip()
        m = re.match(r"^[rRbBuUfF]{0,2}(\"\"\"|'''|\"|')(.*)$", s, re.S)
        if m:
            inner = m.group(2)
            q = m.group(1)
            if inner.endswith(q):
                inner = inner[: -len(q)]
            if inner.strip():
                return clean_doc(inner)
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines):
                return clean_doc(lines[k].strip().rstrip("\"'"))
    return ""


def _rx_members(lines, start, end, lang, fam, member_rx, container: Sym, syms, defpos):
    body = range(start + 1, end)
    indents = [_indent(lines[k]) for k in body
               if lines[k].strip() and _code_only(lines[k], lang).strip() not in ("", "{", "}", "{}", "};")
               and not re.match(r"^\s*(public|private|protected)\s*:\s*$", lines[k])]
    if not indents:
        return
    member_indent = min(indents)
    k = start + 1
    while k < end:
        raw = lines[k]
        code = _code_only(raw, lang).strip()
        if not code or _indent(raw) != member_indent:
            k += 1
            continue
        m = member_rx.match(code)
        if m and not re.match(r"^(if|for|while|switch|return|else|do|case|new|throw|await|const|let|var|import|export|function|class|try|catch|yield|delete|typeof|super|this)\b", code):
            name = m.group("name")
            if fam == "c":
                if "::" in name:
                    name = name.rsplit("::", 1)[-1]
                joined = " ".join(_code_only(x, lang).strip() for x in lines[k: k + 3])
                if "{" not in joined.split(";")[0] and ";" not in joined:
                    k += 1
                    continue
            if fam == "python":
                mend = _indent_end(lines, k)
            else:
                mend = _brace_end(lines, k, lang)
            kind = "method"
            doc = _py_rx_doc(lines, k) if fam == "python" else _doc_above(lines, k)
            msig = raw.strip()
            if fam != "python" and "{" in code:
                msig = _cut_body(msig)
            syms.append(Sym(kind, name, k + 1, mend + 1, clean_sig(msig), container.name, doc))
            defpos.add((k, _code_only(raw, lang).find(name)))
            k = max(mend, k) + 1
            continue
        k += 1


def _rx_import(imps: list[Imp], fam: str, kind: str, m, line: int, raw: str):
    if fam == "python":
        if kind == "import":
            for mod in m.group(1).split(","):
                imps.append(Imp(mod.strip().split(" as ")[0], [], line))
        else:
            names = [x.strip().split(" as ")[0] for x in m.group(2).split(",") if x.strip()]
            imps.append(Imp(m.group(1), names, line, "from"))
    elif fam == "js":
        if kind == "import":
            clause, mod = m.group(1), m.group(2)
            names = [x.strip().split(" as ")[0] for x in re.sub(r"[{}]", ",", clause).split(",") if x.strip()]
            names = [n.replace("* as ", "").replace("type ", "") for n in names]
            imps.append(Imp(mod, names, line))
        elif kind == "import-bare":
            imps.append(Imp(m.group(1), [], line))
        elif kind == "reexport":
            clause, mod = m.group(1), m.group(2)
            names = [x.strip().split(" as ")[0] for x in re.sub(r"[{}]", ",", clause).split(",") if x.strip()] or ["*"]
            imps.append(Imp(mod, names, line, "reexport"))
        else:
            names: list[str] = []
            dm = re.match(r"^\s*(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=", raw)
            if dm:
                names = sorted(set(re.findall(r"[\w$]+", dm.group(1)))) if dm.group(1).startswith("{") else [dm.group(1)]
            imps.append(Imp(m.group(1), names, line, kind))
    elif fam == "php":
        if kind == "use":
            full = m.group(1)
            imps.append(Imp(full, [full.rsplit("\\", 1)[-1]], line, "use"))
        else:
            arg = m.group(1)
            parts = re.findall(r"['\"]([^'\"]+)['\"]", arg)
            mod = "".join(parts).strip()
            if mod:
                if "__DIR__" in arg or "dirname(__FILE__)" in arg:
                    mod = "./" + mod.lstrip("/")
                imps.append(Imp(mod, [], line, "require"))
    elif fam == "go":
        alias, path = m.group(1), m.group(2)
        imps.append(Imp(path, [alias or path.rsplit("/", 1)[-1]], line))
    elif fam == "rust":
        if kind == "mod":
            imps.append(Imp(m.group(1), [m.group(1)], line, "mod"))
        else:
            text = m.group(1).strip()
            names = []
            if "{" in text and "}" in text:
                for part in text[text.index("{") + 1: text.rindex("}")].split(","):
                    part = part.strip().split(" as ")[0].strip()
                    if part and part != "self":
                        names.append(part.rsplit("::", 1)[-1])
            else:
                names.append(text.split(" as ")[0].rsplit("::", 1)[-1])
            imps.append(Imp(text, names, line, "use"))
    elif fam == "c":
        imps.append(Imp(m.group(2), [], line, "sysinclude" if m.group(1) == "<" else "include"))
    elif fam == "bash":
        imps.append(Imp(m.group(1).strip("\"'"), [], line, "source"))


def _rx_ref_lines(text: str, lang: str, name: str) -> list[tuple[int, bool]]:
    fam = _family(lang)
    info = _rx_outline(text, lang)
    deflines = {(s.line - 1) for s in info.symbols if s.name == name}
    out: list[tuple[int, bool]] = []
    rx = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    for row, raw in enumerate(text.split("\n")):
        code = _code_only(raw, lang)
        for m in rx.finditer(code):
            if row in deflines and _is_def_line(code, name, fam):
                continue
            out.append((row + 1, _rx_is_call(code, m, fam)))
    return sorted(set(out))


def _is_def_line(code: str, name: str, fam: str) -> bool:
    return bool(re.search(r"\b(def|class|function|fn|func|struct|enum|trait|interface|impl|mod|type|namespace)\b[^=]*\b"
                          + re.escape(name) + r"\b", code))
