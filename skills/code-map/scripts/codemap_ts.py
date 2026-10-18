#!/usr/bin/env python3
"""codemap_ts.py - tree-sitter engine for code-map: outline, imports and identifier references.

Imported by codemap_core when tree-sitter-language-pack is installed. One
function per language builds the symbol list from the parse tree; one generic
cursor walk counts identifier references and tells calls from plain mentions.
"""
from __future__ import annotations

import re

from codemap_core import FileInfo, Imp, Sym, clean_doc, clean_sig, line_from_col

try:
    import tree_sitter_language_pack as _tslp
    from importlib.metadata import version as _version
    AVAILABLE = True
    ENGINE_ID = "ts-" + _version("tree-sitter-language-pack")
except Exception:  # pragma: no cover - exercised only when the package is missing
    _tslp = None
    AVAILABLE = False
    ENGINE_ID = "none"

LANGS = {"python", "typescript", "tsx", "javascript", "php", "go", "rust", "c", "cpp", "bash"}
JS_LANGS = {"javascript", "typescript", "tsx"}

_parsers: dict = {}


def _parser(lang: str):
    p = _parsers.get(lang)
    if p is None:
        p = _tslp.get_parser(lang)
        _parsers[lang] = p
    return p


def _walk(root):
    cur = root.walk()
    while True:
        yield cur.node
        if cur.goto_first_child():
            continue
        if cur.goto_next_sibling():
            continue
        while True:
            if not cur.goto_parent():
                return
            if cur.goto_next_sibling():
                break


# ---------------------------------------------------------------- reference tables

_JS_IDENTS = {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier",
              "shorthand_property_identifier_pattern", "private_property_identifier"}
IDENTS = {
    "python": {"identifier"},
    "javascript": _JS_IDENTS, "typescript": _JS_IDENTS, "tsx": _JS_IDENTS,
    "php": {"name"},
    "go": {"identifier", "field_identifier", "type_identifier", "package_identifier"},
    "rust": {"identifier", "field_identifier", "type_identifier"},
    "c": {"identifier", "field_identifier", "type_identifier"},
    "cpp": {"identifier", "field_identifier", "type_identifier", "namespace_identifier"},
    "bash": {"variable_name", "word"},
}
_JS_CALLS = {"call_expression": "function", "new_expression": "constructor"}
CALLS = {
    "python": {"call": "function"},
    "javascript": _JS_CALLS, "typescript": _JS_CALLS, "tsx": _JS_CALLS,
    "php": {"function_call_expression": "function", "member_call_expression": "name",
            "scoped_call_expression": "name", "nullsafe_member_call_expression": "name",
            "object_creation_expression": None},
    "go": {"call_expression": "function"},
    "rust": {"call_expression": "function", "macro_invocation": "macro"},
    "c": {"call_expression": "function"},
    "cpp": {"call_expression": "function", "new_expression": "type"},
    "bash": {"command_name": None},
}
_JS_CHAIN = {"member_expression": "property"}
CHAIN = {
    "python": {"attribute": "attribute"},
    "javascript": _JS_CHAIN, "typescript": _JS_CHAIN, "tsx": _JS_CHAIN,
    "php": {"qualified_name": None},
    "go": {"selector_expression": "field"},
    "rust": {"field_expression": "field", "scoped_identifier": "name", "generic_function": "function"},
    "c": {"field_expression": "field"},
    "cpp": {"field_expression": "field", "qualified_identifier": "name", "template_function": "name"},
    "bash": {},
}


def _is_ident(lang: str, node) -> bool:
    if node.type not in IDENTS[lang]:
        return False
    p = node.parent
    if lang == "php" and p is not None and p.type in ("variable_name", "namespace_name"):
        return False
    if lang == "bash" and node.type == "word" and (p is None or p.type != "command_name"):
        return False
    return True


def _is_call(lang: str, node) -> bool:
    chain = CHAIN[lang]
    calls = CALLS[lang]
    cur = node
    p = node.parent
    while p is not None and p.type in chain:
        f = chain[p.type]
        if f:
            last = p.child_by_field_name(f)
        else:
            nc = p.named_children
            last = nc[-1] if nc else None
        if last is None or last != cur:
            return False
        cur = p
        p = p.parent
    if p is None or p.type not in calls:
        return False
    f = calls[p.type]
    if f is None:
        nc = p.named_children
        return bool(nc) and nc[0] == cur
    callee = p.child_by_field_name(f)
    return callee is not None and callee == cur


# ---------------------------------------------------------------- outline context

class _Ctx:
    def __init__(self, text: str, lang: str):
        self.lang = lang
        self.lines = text.split("\n")
        self.src = text.encode("utf-8")
        self.syms: list[Sym] = []
        self.imports: list[Imp] = []
        self.defpos: set[tuple[int, int]] = set()

    def txt(self, node) -> str:
        return node.text.decode("utf-8", errors="replace") if node is not None else ""

    def sig(self, node, cut=None, limit: int = 100) -> str:
        """The definition's first line from the node's start column, stopped at `cut` (the body) when
        that starts on the same line, so one-line functions show their head and not their body."""
        row, col = node.start_point
        text = line_from_col(self.lines, row, col)
        if cut is not None and cut.start_point[0] == row and cut.start_point[1] > col:
            text = self.lines[row].encode("utf-8")[col: cut.start_point[1]].decode("utf-8", errors="replace")
        return clean_sig(text, limit)

    def add(self, kind: str, name, node, outer=None, parent: str = "", doc: str = "", cut=None,
            prefix=None) -> Sym:
        """Record a symbol. `outer` is the wrapping node (export / decorator / template) whose first line
        gives the signature when it starts on the same line; `prefix` is a node whose first line is
        prepended instead (C++ templates); `cut` is the body node the signature stops at."""
        name_node = None
        if not isinstance(name, str):
            name_node = name
            name = self.txt(name_node)
        if name_node is not None:
            self.defpos.add((name_node.start_point[0], name_node.start_point[1]))
        first = outer if outer is not None else node
        if first.start_point[0] != node.start_point[0]:
            first = node
        if cut is None:
            cut = node.child_by_field_name("body")
        limit = 60 if kind in ("var", "const") else 100
        sig = self.sig(first, cut, limit)
        start = node.start_point[0] + 1
        if prefix is not None and prefix.start_point[0] < node.start_point[0]:
            sig = clean_sig(self.sig(prefix, None, limit) + " " + sig, limit)
            start = prefix.start_point[0] + 1
        end_row, end_col = node.end_point
        if end_col == 0 and end_row > node.start_point[0]:
            end_row -= 1  # the node swallowed the trailing newline (preprocessor lines)
        s = Sym(kind, name, start, end_row + 1, sig, parent, doc)
        self.syms.append(s)
        return s

    def imp(self, module: str, names: list[str], node, kind: str = "import"):
        if module:
            self.imports.append(Imp(module, names, node.start_point[0] + 1, kind))

    def doc_before(self, node) -> str:
        """First line of the comment block that ends right above `node`."""
        prev = node.prev_sibling
        expect = node.start_point[0] - 1
        collected: list[str] = []
        while prev is not None and prev.type in ("comment", "line_comment", "block_comment") \
                and prev.end_point[0] >= expect and prev.end_point[0] <= node.start_point[0]:
            t = self.txt(prev)
            collected.append(t)
            if t.startswith("/*") or t.startswith("#[") or t.startswith("#!"):
                break
            expect = prev.start_point[0] - 1
            prev = prev.prev_sibling
        if not collected:
            return ""
        return _comment_first_line(collected[-1])

    def py_doc(self, node) -> str:
        body = node.child_by_field_name("body")
        if body is None or not body.named_children:
            return ""
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_children:
            first = first.named_children[0]
        if first.type != "string":
            return ""
        return _docstring_first_line(self.txt(first))


def _comment_first_line(t: str) -> str:
    t = t.strip()
    if t.startswith("/*"):
        t = re.sub(r"^/\*+!?", "", t)
        t = re.sub(r"\*+/$", "", t)
        for ln in t.split("\n"):
            ln = re.sub(r"^\s*\*+\s?", "", ln).strip()
            if ln:
                return clean_doc(ln)
        return ""
    t = re.sub(r"^(///?!?|//|#!?|--)\s?", "", t)
    return clean_doc(t)


def _docstring_first_line(t: str) -> str:
    m = re.match(r"^[rRbBuUfF]{0,2}(\"\"\"|'''|\"|')(.*)$", t, re.S)
    if not m:
        return ""
    q, inner = m.group(1), m.group(2)
    if inner.endswith(q):
        inner = inner[: -len(q)]
    for ln in inner.split("\n"):
        if ln.strip():
            return clean_doc(ln)
    return ""


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        return s[1:-1]
    return s


# ---------------------------------------------------------------- python

def _py(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        outer = ch
        t = ch.type
        if t == "decorated_definition":
            inner = ch.child_by_field_name("definition")
            if inner is None:
                continue
            ch, t = inner, inner.type
        if t == "function_definition":
            kind = "method" if parent is not None and parent.kind == "class" else "function"
            ctx.add(kind, ch.child_by_field_name("name"), ch, outer, parent.name if parent else "", ctx.py_doc(ch))
        elif t == "class_definition":
            s = ctx.add("class", ch.child_by_field_name("name"), ch, outer, parent.name if parent else "",
                        ctx.py_doc(ch))
            body = ch.child_by_field_name("body")
            if body is not None:
                _py(ctx, body, s)
        elif t in ("expression_statement", "assignment") and parent is None and ch.named_children:
            a = ch.named_children[0] if t == "expression_statement" else ch
            if a.type == "assignment":
                left = a.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    ctx.add("var", left, ch, outer)
        elif t == "import_statement":
            for n in ch.named_children:
                if n.type == "dotted_name":
                    ctx.imp(ctx.txt(n), [], ch)
                elif n.type == "aliased_import":
                    ctx.imp(ctx.txt(n.child_by_field_name("name")), [], ch)
        elif t == "import_from_statement":
            mod = ch.child_by_field_name("module_name")
            names = []
            for n in ch.named_children:
                if n == mod:
                    continue
                if n.type == "dotted_name":
                    names.append(ctx.txt(n))
                elif n.type == "aliased_import":
                    names.append(ctx.txt(n.child_by_field_name("name")))
                elif n.type == "wildcard_import":
                    names.append("*")
            ctx.imp(ctx.txt(mod), names, ch, "from")
        elif t in ("if_statement", "try_statement") and parent is None:
            for blk in ch.named_children:
                if blk.type == "block":
                    _py_imports_only(ctx, blk)
                elif blk.type in ("else_clause", "elif_clause", "except_clause"):
                    for b2 in blk.named_children:
                        if b2.type == "block":
                            _py_imports_only(ctx, b2)


def _py_imports_only(ctx: _Ctx, block):
    for ch in block.children:
        if ch.type in ("import_statement", "import_from_statement"):
            _py(ctx, _Single(ch), None)


class _Single:
    """Wraps one node so a loop over `.children` sees just it."""

    def __init__(self, node):
        self.children = [node]


# ---------------------------------------------------------------- javascript / typescript

_JS_FN_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_JS_CLASS = {"class_declaration", "abstract_class_declaration", "class"}


def _js(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        outer = ch
        t = ch.type
        if t == "export_statement":
            src = ch.child_by_field_name("source")
            if src is not None:
                names = []
                for n in _walk(ch):
                    if n.type == "export_specifier":
                        names.append(ctx.txt(n.child_by_field_name("name")))
                if not names:
                    names = ["*"]
                ctx.imp(_strip_quotes(ctx.txt(src)), names, ch, "reexport")
                continue
            decl = ch.child_by_field_name("declaration")
            if decl is None:
                decl = next((n for n in ch.named_children if n.type in _JS_CLASS | {"function_declaration",
                             "generator_function_declaration", "arrow_function", "function_expression",
                             "function"}), None)
            if decl is None:
                continue
            ch, t = decl, decl.type
        if t == "ambient_declaration":
            inner = next((n for n in ch.named_children), None)
            if inner is None:
                continue
            ch, t = inner, inner.type
        if t in ("function_declaration", "generator_function_declaration"):
            ctx.add("function", ch.child_by_field_name("name") or "default", ch, outer,
                    parent.name if parent else "", ctx.doc_before(outer))
        elif t in ("arrow_function", "function_expression", "function") and outer.type == "export_statement":
            ctx.add("function", ch.child_by_field_name("name") or "default", ch, outer, "", ctx.doc_before(outer))
        elif t in _JS_CLASS:
            s = ctx.add("class", ch.child_by_field_name("name") or "default", ch, outer,
                        parent.name if parent else "", ctx.doc_before(outer))
            body = ch.child_by_field_name("body")
            if body is not None:
                _js_members(ctx, body, s)
        elif t == "interface_declaration":
            ctx.add("interface", ch.child_by_field_name("name"), ch, outer, parent.name if parent else "",
                    ctx.doc_before(outer))
        elif t == "type_alias_declaration":
            ctx.add("type", ch.child_by_field_name("name"), ch, outer, parent.name if parent else "",
                    ctx.doc_before(outer))
        elif t == "enum_declaration":
            ctx.add("enum", ch.child_by_field_name("name"), ch, outer, parent.name if parent else "",
                    ctx.doc_before(outer))
        elif t in ("lexical_declaration", "variable_declaration"):
            first = True
            for d in ch.named_children:
                if d.type != "variable_declarator":
                    continue
                name = d.child_by_field_name("name")
                if name is None or name.type != "identifier":
                    continue
                value = d.child_by_field_name("value")
                if value is not None and _is_require(ctx, value):
                    first = False
                    continue
                kind = "function" if value is not None and value.type in _JS_FN_VALUES else "var"
                cut = value.child_by_field_name("body") if kind == "function" else None
                ctx.add(kind, name, d, outer if first else None, parent.name if parent else "",
                        ctx.doc_before(outer) if first else "", cut=cut)
                first = False
        elif t == "expression_statement" and ch.named_children:
            inner = ch.named_children[0]
            if inner.type == "internal_module":
                s = ctx.add("namespace", inner.child_by_field_name("name"), inner, outer,
                            parent.name if parent else "", ctx.doc_before(outer))
                body = inner.child_by_field_name("body")
                if body is not None:
                    _js(ctx, body, s)
            elif inner.type == "assignment_expression":
                left = inner.child_by_field_name("left")
                right = inner.child_by_field_name("right")
                lt = ctx.txt(left)
                m = re.match(r"^(?:module\.)?exports\.([A-Za-z_$][\w$]*)$", lt)
                if m and left is not None:
                    kind = "function" if right is not None and right.type in _JS_FN_VALUES else "var"
                    prop = left.child_by_field_name("property")
                    cut = right.child_by_field_name("body") if kind == "function" else None
                    ctx.add(kind, prop if prop is not None else m.group(1), ch, outer, "", ctx.doc_before(outer),
                            cut=cut)
        elif t == "import_statement":
            src = ch.child_by_field_name("source")
            names = []
            for n in _walk(ch):
                if n.type == "import_specifier":
                    names.append(ctx.txt(n.child_by_field_name("name")))
                elif n.type == "identifier" and n.parent is not None and n.parent.type in ("import_clause",
                                                                                            "namespace_import"):
                    names.append(ctx.txt(n))
            ctx.imp(_strip_quotes(ctx.txt(src)), names, ch)


def _is_require(ctx: _Ctx, value) -> bool:
    if value.type == "await_expression" and value.named_children:
        value = value.named_children[0]
    if value.type != "call_expression":
        return False
    fn = value.child_by_field_name("function")
    return fn is not None and (ctx.txt(fn) == "require" or fn.type == "import")


def _js_members(ctx: _Ctx, body, cls: Sym):
    for ch in body.children:
        t = ch.type
        if t in ("method_definition", "abstract_method_signature", "method_signature"):
            name = ch.child_by_field_name("name")
            if name is not None:
                ctx.add("method", name, ch, None, cls.name, ctx.doc_before(ch))
        elif t == "public_field_definition":
            value = ch.child_by_field_name("value")
            name = ch.child_by_field_name("name")
            if name is not None and value is not None and value.type in _JS_FN_VALUES:
                ctx.add("method", name, ch, None, cls.name, ctx.doc_before(ch), cut=value.child_by_field_name("body"))


def _js_calls(ctx: _Ctx, node):
    """require('x') and import('x') anywhere in the file."""
    if node.type != "call_expression":
        return
    fn = node.child_by_field_name("function")
    if fn is None:
        return
    is_req = ctx.txt(fn) == "require"
    if not is_req and fn.type != "import":
        return
    args = node.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return
    a = args.named_children[0]
    if a.type not in ("string", "template_string"):
        return
    mod = _strip_quotes(ctx.txt(a))
    names: list[str] = []
    p = node.parent
    if p is not None and p.type == "variable_declarator":
        nm = p.child_by_field_name("name")
        if nm is not None:
            if nm.type == "identifier":
                names = [ctx.txt(nm)]
            else:
                names = sorted({ctx.txt(n) for n in _walk(nm)
                                if n.type in ("shorthand_property_identifier_pattern", "identifier")})
    ctx.imp(mod, names, node, "require" if is_req else "dynamic")


# ---------------------------------------------------------------- php

_PHP_CONTAINERS = {"class_declaration": "class", "interface_declaration": "interface",
                   "trait_declaration": "trait", "enum_declaration": "enum"}


def _php(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        t = ch.type
        if t == "namespace_definition":
            name = ch.child_by_field_name("name")
            s = ctx.add("namespace", name if name is not None else "(global)", ch, None, "", "")
            body = ch.child_by_field_name("body")
            if body is not None:
                _php(ctx, body, None)
        elif t in _PHP_CONTAINERS:
            s = ctx.add(_PHP_CONTAINERS[t], ch.child_by_field_name("name"), ch, None,
                        parent.name if parent else "", ctx.doc_before(ch))
            body = ch.child_by_field_name("body")
            if body is not None:
                _php_members(ctx, body, s)
        elif t == "function_definition":
            ctx.add("function", ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
        elif t == "const_declaration":
            for el in ch.named_children:
                if el.type == "const_element" and el.named_children:
                    ctx.add("const", el.named_children[0], ch, None, "", "")
        elif t == "namespace_use_declaration":
            for cl in ch.named_children:
                if cl.type != "namespace_use_clause":
                    continue
                qn = next((n for n in cl.named_children if n.type in ("qualified_name", "name")), None)
                if qn is None:
                    continue
                full = ctx.txt(qn)
                ctx.imp(full, [full.rsplit("\\", 1)[-1]], ch, "use")
        elif t == "expression_statement" and ch.named_children:
            _php_require(ctx, ch.named_children[0])


def _php_members(ctx: _Ctx, body, cls: Sym):
    for ch in body.children:
        if ch.type == "method_declaration":
            ctx.add("method", ch.child_by_field_name("name"), ch, None, cls.name, ctx.doc_before(ch))


def _php_require(ctx: _Ctx, node):
    if node.type not in ("require_expression", "require_once_expression", "include_expression",
                         "include_once_expression"):
        return
    parts = []
    for n in _walk(node):
        if n.type in ("string_content", "string_fragment"):
            parts.append(ctx.txt(n))
    mod = "".join(parts).strip()
    if not mod:
        return
    whole = ctx.txt(node)
    if "__DIR__" in whole or "dirname(__FILE__)" in whole:
        mod = "./" + mod.lstrip("/")
    ctx.imp(mod, [], node, "require")


# ---------------------------------------------------------------- go

def _go(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        t = ch.type
        if t == "function_declaration":
            ctx.add("function", ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
        elif t == "method_declaration":
            recv = ch.child_by_field_name("receiver")
            rname = ""
            if recv is not None:
                ti = next((n for n in _walk(recv) if n.type == "type_identifier"), None)
                rname = ctx.txt(ti)
            ctx.add("method", ch.child_by_field_name("name"), ch, None, rname, ctx.doc_before(ch))
        elif t == "type_declaration":
            specs = [n for n in ch.named_children if n.type in ("type_spec", "type_alias")]
            for spec in specs:
                inner = spec.child_by_field_name("type")
                kind = {"struct_type": "struct", "interface_type": "interface"}.get(inner.type if inner else "", "type")
                ctx.add(kind, spec.child_by_field_name("name"), spec, ch if len(specs) == 1 else None, "",
                        ctx.doc_before(ch) if len(specs) == 1 else ctx.doc_before(spec))
        elif t in ("var_declaration", "const_declaration"):
            kind = "var" if t == "var_declaration" else "const"
            specs = [n for n in ch.named_children if n.type in ("var_spec", "const_spec")]
            for spec in specs:
                for nm in spec.children_by_field_name("name"):
                    ctx.add(kind, nm, spec, ch if len(specs) == 1 else None, "", "")
        elif t == "import_declaration":
            for n in _walk(ch):
                if n.type == "import_spec":
                    path = _strip_quotes(ctx.txt(n.child_by_field_name("path")))
                    alias = n.child_by_field_name("name")
                    ctx.imp(path, [ctx.txt(alias) if alias is not None else path.rsplit("/", 1)[-1]], n)


# ---------------------------------------------------------------- rust

def _rust(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        t = ch.type
        pname = parent.name if parent else ""
        if t == "function_item":
            ctx.add("method" if parent is not None else "function", ch.child_by_field_name("name"), ch, None,
                    pname, ctx.doc_before(ch))
        elif t == "function_signature_item":
            ctx.add("method", ch.child_by_field_name("name"), ch, None, pname, ctx.doc_before(ch))
        elif t in ("struct_item", "enum_item", "union_item", "type_item", "const_item", "static_item",
                   "macro_definition"):
            kind = {"struct_item": "struct", "enum_item": "enum", "union_item": "union", "type_item": "type",
                    "const_item": "const", "static_item": "const", "macro_definition": "macro"}[t]
            ctx.add(kind, ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
        elif t == "trait_item":
            s = ctx.add("trait", ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
            body = ch.child_by_field_name("body")
            if body is not None:
                _rust(ctx, body, s)
        elif t == "impl_item":
            typ = ch.child_by_field_name("type")
            trait = ch.child_by_field_name("trait")
            ti = next((n for n in _walk(typ) if n.type == "type_identifier"), None) if typ is not None else None
            tname = ctx.txt(ti) if ti is not None else ctx.txt(typ)
            label = f"{ctx.txt(trait)} for {ctx.txt(typ)}" if trait is not None else ctx.txt(typ)
            s = ctx.add("impl", label, ch, None, "", ctx.doc_before(ch))
            s.name = tname
            body = ch.child_by_field_name("body")
            if body is not None:
                _rust(ctx, body, Sym("impl", tname, s.line, s.end, s.sig))
        elif t == "mod_item":
            s = ctx.add("module", ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
            body = ch.child_by_field_name("body")
            if body is not None:
                _rust(ctx, body, None)
            else:
                ctx.imp(s.name, [s.name], ch, "mod")
        elif t == "use_declaration":
            arg = ch.child_by_field_name("argument")
            text = ctx.txt(arg)
            names = []
            if "{" in text:
                inner = text[text.index("{") + 1: text.rindex("}")] if "}" in text else ""
                for part in inner.split(","):
                    part = part.strip().split(" as ")[0].strip()
                    if part and part != "self":
                        names.append(part.rsplit("::", 1)[-1])
            else:
                names.append(text.split(" as ")[0].rsplit("::", 1)[-1])
            ctx.imp(text, names, ch, "use")


# ---------------------------------------------------------------- c / c++

def _c_declname(decl):
    node = decl
    while node is not None and node.type != "function_declarator":
        nxt = node.child_by_field_name("declarator")
        if nxt is None:
            nxt = next((c for c in node.named_children if c.type.endswith("declarator")), None)
        node = nxt
    if node is None:
        return None, None
    name = node.child_by_field_name("declarator")
    while name is not None and name.type in ("parenthesized_declarator", "pointer_declarator"):
        name = name.child_by_field_name("declarator") or (name.named_children[0] if name.named_children else None)
    return node, name


def _c_split_qualified(ctx: _Ctx, name):
    """(name_node, name_text, scope_text) for identifier / field_identifier / qualified_identifier."""
    scope = ""
    while name is not None and name.type == "qualified_identifier":
        sc = name.child_by_field_name("scope")
        if sc is not None:
            scope = (scope + "::" + ctx.txt(sc)) if scope else ctx.txt(sc)
        name = name.child_by_field_name("name")
    return name, ctx.txt(name), scope


_C_CONTAINERS = {"struct_specifier": "struct", "class_specifier": "class", "union_specifier": "union"}


def _c(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        outer = ch
        prefix = None
        t = ch.type
        if t == "template_declaration":
            inner = next((n for n in ch.named_children if n.type in ("function_definition", "class_specifier",
                          "struct_specifier", "declaration", "alias_declaration")), None)
            if inner is None:
                continue
            prefix = ch
            ch, t = inner, inner.type
        if t == "function_definition":
            _fd, name = _c_declname(ch.child_by_field_name("declarator"))
            if name is None:
                continue
            name, text, scope = _c_split_qualified(ctx, name)
            if name is None:
                continue
            pname = parent.name if parent else scope
            kind = "method" if pname else "function"
            ctx.add(kind, name, ch, outer, pname, ctx.doc_before(outer), prefix=prefix)
        elif t == "field_declaration" and parent is not None:
            _fd, name = _c_declname(ch.child_by_field_name("declarator"))
            if name is None:
                continue
            name, text, scope = _c_split_qualified(ctx, name)
            if name is not None:
                ctx.add("method", name, ch, outer, parent.name, ctx.doc_before(outer))
        elif t in _C_CONTAINERS:
            name = ch.child_by_field_name("name")
            body = ch.child_by_field_name("body")
            if name is None or body is None:
                continue
            name, text, scope = _c_split_qualified(ctx, name)
            s = ctx.add(_C_CONTAINERS[t], name, ch, outer, parent.name if parent else "", ctx.doc_before(outer),
                        prefix=prefix)
            _c(ctx, body, s)
        elif t == "enum_specifier":
            name = ch.child_by_field_name("name")
            if name is not None and ch.child_by_field_name("body") is not None:
                ctx.add("enum", name, ch, outer, parent.name if parent else "", ctx.doc_before(outer))
        elif t == "type_definition":
            decl = ch.child_by_field_name("declarator")
            if decl is not None and decl.type == "type_identifier":
                ctx.add("type", decl, ch, outer, "", ctx.doc_before(outer))
            inner = next((n for n in ch.named_children if n.type in _C_CONTAINERS or n.type == "enum_specifier"), None)
            if inner is not None and inner.child_by_field_name("name") is not None and inner.child_by_field_name("body") is not None:
                _c(ctx, _Single(inner), parent)
        elif t == "alias_declaration":
            ctx.add("type", ch.child_by_field_name("name"), ch, outer, "", ctx.doc_before(outer))
        elif t == "namespace_definition":
            name = ch.child_by_field_name("name")
            s = ctx.add("namespace", name if name is not None else "(anonymous)", ch, outer, "", ctx.doc_before(outer))
            body = ch.child_by_field_name("body")
            if body is not None:
                _c(ctx, body, None)
        elif t in ("preproc_function_def", "preproc_def"):
            ctx.add("macro", ch.child_by_field_name("name"), ch, outer, "", "")
        elif t in ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "linkage_specification",
                   "declaration_list"):
            body = ch.child_by_field_name("body") if t == "linkage_specification" else ch
            if body is not None:
                _c(ctx, body, parent)
        elif t == "preproc_include":
            path = ch.child_by_field_name("path")
            if path is None:
                continue
            if path.type == "system_lib_string":
                ctx.imp(ctx.txt(path).strip("<>"), [], ch, "sysinclude")
            else:
                ctx.imp(_strip_quotes(ctx.txt(path)), [], ch, "include")


# ---------------------------------------------------------------- bash

def _bash(ctx: _Ctx, node, parent: Sym | None):
    for ch in node.children:
        t = ch.type
        if t == "function_definition":
            ctx.add("function", ch.child_by_field_name("name"), ch, None, "", ctx.doc_before(ch))
        elif t == "variable_assignment":
            ctx.add("var", ch.child_by_field_name("name"), ch, None, "", "")
        elif t == "declaration_command":
            for va in ch.named_children:
                if va.type == "variable_assignment":
                    ctx.add("var", va.child_by_field_name("name"), ch, None, "", "")
        elif t == "command":
            name = ch.child_by_field_name("name")
            if name is not None and ctx.txt(name) in ("source", "."):
                arg = next((n for n in ch.named_children if n != name), None)
                if arg is not None:
                    ctx.imp(_strip_quotes(ctx.txt(arg)), [], ch, "source")
        elif t in ("if_statement", "compound_statement", "subshell"):
            pass


_OUTLINERS = {"python": _py, "javascript": _js, "typescript": _js, "tsx": _js, "php": _php, "go": _go,
              "rust": _rust, "c": _c, "cpp": _c, "bash": _bash}


# ---------------------------------------------------------------- public API

def outline(text: str, lang: str) -> FileInfo:
    ctx = _Ctx(text, lang)
    tree = _parser(lang).parse(ctx.src)
    root = tree.root_node
    _OUTLINERS[lang](ctx, root, None)
    refs: dict[str, list[int]] = {}
    js = lang in JS_LANGS
    for n in _walk(root):
        if js:
            _js_calls(ctx, n)
        if not _is_ident(lang, n):
            continue
        sp = n.start_point
        if (sp[0], sp[1]) in ctx.defpos:
            continue
        name = n.text.decode("utf-8", errors="replace")
        if not name:
            continue
        e = refs.get(name)
        if e is None:
            e = refs[name] = [0, 0]
        e[0] += 1
        if _is_call(lang, n):
            e[1] += 1
    return FileInfo("", lang, 0, "ts", ctx.syms, ctx.imports, refs)


def ref_lines(text: str, lang: str, name: str) -> list[tuple[int, bool]]:
    ctx = _Ctx(text, lang)
    tree = _parser(lang).parse(ctx.src)
    root = tree.root_node
    _OUTLINERS[lang](ctx, root, None)
    want = name.encode("utf-8")
    out: list[tuple[int, bool]] = []
    for n in _walk(root):
        if n.text != want or not _is_ident(lang, n):
            continue
        sp = n.start_point
        if (sp[0], sp[1]) in ctx.defpos:
            continue
        out.append((sp[0] + 1, _is_call(lang, n)))
    return out
