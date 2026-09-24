#!/usr/bin/env python3
"""jac_ast_edit — AST-native surgical editor for Jac, backed by tree-sitter.

Protocol (JSON over stdin/stdout):
    jac_ast_edit.py symbols <file>
    jac_ast_edit.py edit    <file>     # body: {"operations": [ ... ]}

Edits by symbol, not string: {action, target, name} locates a symbol through
the tree-sitter parse tree, computes byte spans, and splices text. Batches are
atomic — all ops resolve against the ORIGINAL tree, splices apply in one pass,
and the result is re-parsed; if the new source has more ERROR nodes than the
original, the batch is rejected and nothing is written.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from tree_sitter import Node, Tree

from tree_sitter_jac import new_parser

# --------------------------------------------------------------------------
# helpers


class EditError(Exception):
    def __init__(self, code: str, message: str, suggestions=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


def _named(n: Node) -> Node:
    """Unwrap container nodes (element / archetype_member wrappers)."""
    while n and n.type in ("element", "archetype_member") and n.named_child_count:
        n = n.named_children[0]
    return n


def _field(n: Node | None, name: str) -> Node | None:
    return n.child_by_field_name(name) if n else None


def _node_text(n: Node) -> str:
    return n.text.decode("utf-8", "replace")


def _indent_of(src: bytes, pos: int) -> str:
    """Whitespace prefix of the line containing byte offset pos."""
    line_start = src.rfind(b"\n", 0, pos) + 1
    prefix = src[line_start:pos]
    return prefix.decode("utf-8", "replace") if prefix.strip() == b"" else ""


def _edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein-ish distance — small, good enough for did-you-mean."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        curr = [i]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _closest_symbol_name(needle: str, qualified_names: list[str]) -> str | None:
    """Case-insensitive fuzzy match; strips dot prefixes so 'greet' matches
    'Card.greet'. Returns None when nothing is within threshold."""
    if not needle or not qualified_names:
        return None
    # Match the full needle and its last dotted segment ("Card.labl" → "labl").
    targets = [needle.lower()]
    last = needle.split(".")[-1].lower()
    if last != targets[0]:
        targets.append(last)
    threshold = max(2, (len(needle) * 2 + 4) // 5)  # ceil(0.4 * len)
    best: tuple[str, int] | None = None
    for name in qualified_names:
        bare = name.split(".")[-1]
        d = min(_edit_distance(t, bare.lower()) for t in targets)
        if d <= threshold and (best is None or d < best[1]):
            best = (name, d)
    return best[0] if best else None


def _count_errors(root: Node) -> int:
    count = 0
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "ERROR" or n.is_missing:
            count += 1
        stack.extend(n.children)
    return count


# --------------------------------------------------------------------------
# symbol index


def _kind_of_archetype(n: Node) -> str:
    k = _field(n, "kind")
    return _node_text(k) if k else "archetype"


def _test_label(n: Node) -> str | None:
    """test foo { } — grammar currently leaves the name as a bare token/ERROR.
    Recover it: first non-'test' child before '{' that has word text."""
    for ch in n.children:
        if ch.type == "{" or ch.type == "block_body":
            break
        if ch.type == "test":
            continue
        txt = _node_text(ch).strip()
        if txt and txt != "test":
            return txt
    return None


class Symbol:
    __slots__ = ("kind", "name", "qualified", "node", "parent", "index")

    def __init__(self, kind, name, qualified, node, parent=None, index=0):
        self.kind = kind
        self.name = name
        self.qualified = qualified
        self.node = node
        self.parent = parent
        self.index = index


def _index_symbols(root: Node) -> list[Symbol]:
    symbols: list[Symbol] = []
    counters: dict[str, int] = {}

    def add(kind, name, node, parent=None):
        qualified = f"{parent}.{name}" if parent and name else (name or "")
        key = (parent or "") + "\x00" + kind + "\x00" + (name or "")
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        symbols.append(Symbol(kind, name, qualified, node, parent, idx))

    def walk_members(owner: Node, owner_name: str):
        for member in owner.named_children:
            m = _named(member)
            if m.type == "ability":
                nm = _field(m, "name")
                add("ability", _node_text(nm) if nm else None, m, owner_name)
                add("method", _node_text(nm) if nm else None, m, owner_name)
            elif m.type == "has_statement":
                for hv in m.named_children:
                    if hv.type == "has_var":
                        nm = _field(hv, "name")
                        add("has", _node_text(nm) if nm else None, hv, owner_name)
                        add("property", _node_text(nm) if nm else None, hv, owner_name)
            elif m.type == "test":
                label = _test_label(m)
                add("test", label, m, owner_name)
            elif m.type == "archetype_member":
                walk_members(m, owner_name)  # nested wrapper forms

    def walk_enum(enu: Node, ename: str):
        for ch in enu.named_children:
            if ch.type == "enum_member":
                nm = _node_text(ch).split("=")[0].strip()
                add("member", nm, ch, ename)

    for top in root.named_children:
        real = _named(top)
        t = real.type
        if t == "archetype":
            kind = _kind_of_archetype(real)
            nm = _field(real, "name")
            name = _node_text(nm) if nm else None
            add(kind, name, real)
            add("archetype", name, real)
            for ch in real.named_children:
                if ch.type in ("archetype_member", "accessor_block"):
                    walk_members(ch, name)
        elif t == "ability":
            nm = _field(real, "name")
            name = _node_text(nm) if nm else None
            add("function", name, real)
            add("ability", name, real)
        elif t == "enum":
            nm = _field(real, "name")
            name = _node_text(nm) if nm else None
            add("enum", name, real)
            walk_enum(real, name)
        elif t == "global_var":
            nm = _field(real, "name")
            add("glob", _node_text(nm) if nm else None, real)
        elif t == "impl":
            tgt = _field(real, "target")
            add("impl", _node_text(tgt) if tgt else None, real)
        elif t == "test":
            add("test", _test_label(real), real)
        elif t == "type_alias":
            nm = _field(real, "name")
            add("type", _node_text(nm) if nm else None, real)
        elif t == "import_statement":
            add("import", None, real)
        elif t == "module_code":
            add("code", None, real)
    return symbols


def _resolve(symbols: list[Symbol], target: str | None, name: str | None,
             index: int | None, require=True) -> Symbol:
    target = (target or "").lower()
    name = name or None
    idx = index or 0
    cands = [s for s in symbols if (not target or s.kind == target)]
    if name:
        cands = [s for s in cands if s.name == name or s.qualified == name]
    if idx >= len(cands):
        if require:
            pool = ", ".join(sorted({f"{s.kind}:{s.qualified or s.kind}"
                                     for s in symbols})) or "<empty file>"
            hints = [f"available: {pool}"]
            if name:
                names = [s.qualified or s.name or "" for s in symbols]
                close = _closest_symbol_name(name, names)
                if close:
                    hints.insert(0, f'Did you mean: "{close}"?')
            raise EditError("symbol_not_found",
                            f"no symbol target={target!r} name={name!r} index={idx}",
                            hints)
        return None
    return cands[idx]


def _import_info(stmt: Node) -> dict:
    """(path, items, is_from) for an import_statement node."""
    path_txt = None
    items: list[str] = []
    is_from = False
    for c in stmt.children:
        if c.type == "import_path":
            path_txt = _node_text(c)
        elif c.type == "import_items":
            is_from = True
            items = [_node_text(ic) for ic in c.named_children]
    return {"path": path_txt, "items": items, "is_from": is_from}


# --------------------------------------------------------------------------
# span computation


def _body_span(n: Node) -> tuple[int, int, int, int, int]:
    """(inner_start, inner_end, close_brace_indent_pos, block_start, block_end)
    for a node with a block_body."""
    block = next((c for c in n.named_children if c.type == "block_body"), None)
    if block is None:
        return None
    src = n.tree.root_node.text if False else None  # unused; spans absolute
    ob = block.start_byte
    ce = block.end_byte
    # block_body includes braces per grammar? verify: children start '{' end '}'
    inner_start, inner_end = ob, ce
    kids = block.children
    if kids and _node_text(kids[0]) == "{":
        inner_start = kids[0].end_byte
    if kids and _node_text(kids[-1]) == "}":
        inner_end = kids[-1].start_byte
    return inner_start, inner_end, 0, ob, ce


def _detect_indent(src: bytes) -> str:
    for line in src.split(b"\n"):
        stripped = line.lstrip(b" \t")
        if stripped != line and stripped:
            ind = line[: len(line) - len(stripped)].decode()
            return ind
    return "    "


def _trim_removal(src: bytes, start: int, end: int) -> tuple[int, int]:
    """Expand a removal to full line(s) and eat resulting blank lines."""
    ls = src.rfind(b"\n", 0, start) + 1
    le = src.find(b"\n", end)
    le = len(src) if le == -1 else le + 1
    if src[ls:end].strip() == b"":
        pass  # whole-line node already
    if le < len(src) and src[le:].lstrip(b" \t").startswith(b"\n") is False:
        pass
    # eat following blank lines
    while le < len(src) and src[le] in (32, 9):
        nxt = src.find(b"\n", le)
        nxt = len(src) if nxt == -1 else nxt
        if src[le:nxt].strip() == b"" and nxt > le:
            le = nxt + 1
        else:
            break
    # if previous line is blank and next line is blank or EOF, eat one blank above
    prev_end = ls - 1  # at the '\n' ending previous line
    above_ls = src.rfind(b"\n", 0, prev_end) + 1
    if above_ls <= prev_end and src[above_ls:prev_end].strip() == b"":
        below = src[le:le + 80]
        if below.strip() == b"" or le >= len(src) or below.lstrip().startswith(b"\n"):
            ls = above_ls
    return ls, le


def _indent_block(code: str, indent: str) -> str:
    """Prefix the first line with indent; continuation lines are taken as
    already written at final indentation (ts-morph-style relative text)."""
    lines = code.rstrip("\n").split("\n")
    if not lines or not lines[0].strip():
        return "\n".join(lines)
    lines[0] = indent + lines[0]
    return "\n".join(lines)


def _renest(code: str, base: str) -> str:
    """Accept both relative and absolute-style bodies: if the minimum
    indentation of non-empty continuation lines exceeds the base indent,
    shift left so the deepest nesting starts at base + one level."""
    base_n = len(base.replace("\t", "    "))
    lines = code.rstrip("\n").split("\n")
    cont = [ln for ln in lines[1:] if ln.strip()]
    if not cont or base_n == 0:
        return code
    min_n = min(len(ln) - len(ln.lstrip(" ").replace("\t", "    ")[:0] + ln.lstrip()) for ln in cont) if False else min(
        (lambda s: len(s) - len(s.lstrip()))(ln.replace("\t", "    ")) for ln in cont)
    if min_n > base_n:
        shift = min_n - base_n
        out = [lines[0]]
        for ln in lines[1:]:
            if ln.strip() == "":
                out.append("")
            else:
                cur = ln.replace("\t", "    ")
                cur_n = len(cur) - len(cur.lstrip())
                cut = min(shift, cur_n)
                out.append(cur[cut:])
        return "\n".join(out)
    return code


# --------------------------------------------------------------------------
# operations


class Engine:
    def __init__(self, src: bytes, tree: Tree):
        self.src = src
        self.tree = tree
        self.symbols = _index_symbols(tree.root_node)
        self.splices: list[tuple[int, int, bytes, str]] = []  # start, end, repl, label

    def splice(self, start: int, end: int, repl: str | bytes, label: str):
        self.splices.append((start, end, repl.encode() if isinstance(repl, str) else repl, label))

    # -- locate helpers ----------------------------------------------------

    def must(self, target, name, index) -> Symbol:
        return _resolve(self.symbols, target, name, index)

    # -- micro ops ---------------------------------------------------------

    def op_rename(self, op):
        s = self.must(op.get("target"), op.get("name"), op.get("index"))
        nm = _field(s.node, "name")
        if nm is None:
            raise EditError("no_name_node", f"symbol {s.qualified or s.kind} has no name node; use replace")
        self.splice(nm.start_byte, nm.end_byte, op["value"], f"rename {s.qualified}")

    def op_remove(self, op):
        s = self.must(op.get("target"), op.get("name"), op.get("index"))
        ls, le = _trim_removal(self.src, s.node.start_byte, s.node.end_byte)
        self.splice(ls, le, "", f"remove {s.qualified or s.kind}")

    def op_set_initializer(self, op):
        s = self.must(op.get("target") or "has", op.get("name"), op.get("index"))
        node = s.node
        eq = next((c for c in node.children if c.type == "="), None)
        if eq:
            # value expression runs from after '=' to end (before ';')
            semi = next((c for c in node.children if c.type == ";"), None)
            vstart = eq.end_byte
            vend = semi.start_byte if semi else node.end_byte
            while vend > vstart and chr(self.src[vend - 1]).isspace():
                vend -= 1
            while vstart < vend and chr(self.src[vstart]).isspace():
                vstart += 1
            self.splice(vstart, vend, op["value"], f"set_initializer {s.qualified}")
        else:
            semi = next((c for c in node.children if c.type == ";"), None)
            pos = semi.start_byte if semi else node.end_byte
            self.splice(pos, pos, f" = {op['value']}", f"add_initializer {s.qualified}")

    def op_set_return_type(self, op):
        s = self.must(op.get("target") or "function", op.get("name"), op.get("index"))
        sig = next((c for c in s.node.named_children if c.type == "func_signature"), None)
        rt = _field(sig, "return_type") if sig else None
        val = op["value"]
        if rt is not None:
            self.splice(rt.start_byte, rt.end_byte, val, f"set_return_type {s.qualified}")
            return
        if sig is None:
            raise EditError("no_signature", f"{s.qualified} has no func_signature")
        arrow = next((c for c in sig.children if c.type == "->"), None)
        if arrow:
            self.splice(arrow.end_byte, arrow.end_byte, f" {val}", f"set_return_type {s.qualified}")
        else:
            self.splice(sig.end_byte, sig.end_byte, f" -> {val}", f"set_return_type {s.qualified}")

    # -- body ops ----------------------------------------------------------

    def _body_or_fail(self, s: Symbol):
        span = _body_span(s.node)
        if span is None:
            # bodyless declaration: can f() -> str;  → convert ';' to block
            semi = next((c for c in s.node.children if c.type == ";"), None)
            if semi is None:
                raise EditError("no_body", f"{s.qualified or s.kind} has no block body")
            ind = _detect_indent(self.src)
            return ("nobody", semi.start_byte, semi.end_byte, ind)
        inner_start, inner_end, _, ob, ce = span
        # base indent = indent of the closing-brace line, else detect + 1 level
        ind = _indent_of(self.src, ce) or _detect_indent(self.src) + "    "
        return ("body", inner_start, inner_end, ind)

    def op_set_body(self, op):
        s = self.must(op.get("target") or "function", op.get("name"), op.get("index"))
        mode, a, b, ind = self._body_or_fail(s)
        code = op["newCode"]
        if mode == "nobody":
            repl = " {\n" + (_indent_block(code, ind) if code.strip() else "") + ("\n" + _indent_of(self.src, s.node.start_byte) + "}" if code.strip() else "}")
            self.splice(a, b, repl, f"set_body {s.qualified}")
            return
        code = _renest(code, ind)
        inner = ("\n" + _indent_block(code, ind) + "\n" + _indent_of(self.src, a)) if code.strip() else ""
        self.splice(a, b, inner, f"set_body {s.qualified}")

    def op_replace_in_body(self, op):
        s = self.must(op.get("target") or "function", op.get("name"), op.get("index"))
        mode, a, b, _ = self._body_or_fail(s)
        if mode == "nobody":
            raise EditError("no_body", f"{s.qualified} has no block body")
        body = self.src[a:b].decode("utf-8", "replace")
        anchor = op["value"]
        hits = body.count(anchor)
        if hits == 0:
            raise EditError("anchor_not_found", f"anchor {anchor[:40]!r} not in {s.qualified} body")
        if hits > 1:
            raise EditError("ambiguous_anchor", f"anchor {anchor[:40]!r} appears {hits}x in {s.qualified} body")
        start = a + body.index(anchor)
        if op.get("valueEnd"):
            endtxt = op["valueEnd"]
            endrel = body.find(endtxt, body.index(anchor))
            if endrel == -1:
                raise EditError("anchor_not_found", f"valueEnd {endtxt[:40]!r} not after value anchor")
            end = a + endrel + len(endtxt)
        else:
            end = start + len(anchor)
        self.splice(start, end, op["newCode"], f"replace_in_body {s.qualified}")

    def op_add_statement(self, op):
        s = self.must(op.get("target") or "function", op.get("name"), op.get("index"))
        mode, a, b, ind = self._body_or_fail(s)
        if mode == "nobody":
            raise EditError("no_body", f"{s.qualified} has no block body")
        ins = "\n" + _indent_block(_renest(op["newCode"].strip("\n"), ind), ind)
        self.splice(b, b, ins, f"add_statement {s.qualified}")

    # -- structural ops ----------------------------------------------------

    def _insert_in_owner(self, s: Symbol, text: str, label, before_kinds=None):
        """Insert a member into a braced body. Default: at the line before the
        closing brace. before_kinds: at the line before the first member whose
        unwrapped type matches (has statements land on top, methods last)."""
        node = s.node
        block = next((c for c in node.named_children if c.type == "block_body"), None)
        owner = block if block is not None else node
        close = None
        for c in owner.children:
            if _node_text(c) == "}":
                close = c
        if close is None:
            raise EditError("no_body", f"{s.qualified or s.kind} has no braced body")
        fallback = _detect_indent(self.src) + "    "
        text = _renest(text, fallback).rstrip()
        if before_kinds:
            for ch in owner.named_children:
                first = _named(ch)
                if first.type in before_kinds:
                    line_start = self.src.rfind(b"\n", 0, first.start_byte) + 1
                    ind = _indent_of(self.src, first.start_byte) or fallback
                    self.splice(line_start, line_start, ind + text + "\n\n", label)
                    return
        close_ind = _indent_of(self.src, close.start_byte) or fallback
        close_line_start = self.src.rfind(b"\n", 0, close.start_byte) + 1
        self.splice(close_line_start, close_line_start,
                    close_ind + text + "\n\n" + close_ind, label)

    def op_add_method(self, op):
        s = self.must(op.get("target") or "archetype", op.get("name"), op.get("index"))
        self._insert_in_owner(s, op["newCode"], f"add_method {s.qualified}",
                              before_kinds=("ability", "test"))

    def op_add_property(self, op):
        s = self.must(op.get("target") or "archetype", op.get("name"), op.get("index"))
        text = op["newCode"].strip()
        if not text.startswith("has ") and not text.startswith("static has"):
            text = "has " + text
        if not text.rstrip().endswith(";"):
            text = text.rstrip() + ";"
        self._insert_in_owner(s, text, f"add_property {s.qualified}",
                              before_kinds=("has_statement",))

    def op_replace(self, op):
        s = self.must(op.get("target"), op.get("name"), op.get("index"))
        self.splice(s.node.start_byte, s.node.end_byte, op["newCode"], f"replace {s.qualified or s.kind}")

    def op_add_function(self, op):
        # module-level def appended at end of file
        code = op["newCode"].rstrip() + "\n"
        self.splice(len(self.src), len(self.src), ("\n" if self.src and not self.src.endswith(b"\n\n") else "") + code, "add_function")

    # -- structural micro ops (Empryo harvest) -----------------------------

    def op_set_type(self, op):
        s = self.must(op.get("target"), op.get("name"), op.get("index"))
        val = op["value"].strip()
        ta = next((c for c in s.node.named_children
                   if c.type == "type_annotation"), None)
        if ta is not None:
            self.splice(ta.start_byte, ta.end_byte, val, f"set_type {s.qualified}")
        else:
            nm = s.node.named_children[0]  # identifier
            self.splice(nm.end_byte, nm.end_byte, f": {val}",
                        f"set_type {s.qualified}")

    def _param_list_of(self, s: Symbol) -> Node:
        sig = next((c for c in s.node.named_children
                    if c.type == "func_signature"), None)
        if sig is None:
            raise EditError("no_parameters",
                            f"{s.qualified or s.kind} has no parameter list")
        pl = next((c for c in sig.named_children
                   if c.type == "parameter_list"), None)
        if pl is None:
            raise EditError("no_parameters",
                            f"{s.qualified or s.kind} has no parameter list")
        return pl

    def op_add_parameter(self, op):
        s = self.must(op.get("target") or "ability", op.get("name"),
                      op.get("index"))
        pl = self._param_list_of(s)
        val = op["value"].strip()
        params = [c for c in pl.named_children if c.type == "param"]
        if params:
            last = params[-1]
            self.splice(last.end_byte, last.end_byte, ", " + val,
                        f"add_parameter {s.qualified}")
        else:
            cb = pl.children[-1]  # ')'
            self.splice(cb.start_byte, cb.start_byte, val,
                        f"add_parameter {s.qualified}")

    def op_remove_parameter(self, op):
        s = self.must(op.get("target") or "ability", op.get("name"),
                      op.get("index"))
        pl = self._param_list_of(s)
        want = (op.get("value") or "").strip()
        params = [c for c in pl.named_children if c.type == "param"]
        tgt = next((pm for pm in params
                    if pm.named_children
                    and _node_text(pm.named_children[0]) == want), None)
        if tgt is None:
            names = [_node_text(pm.named_children[0]) for pm in params
                     if pm.named_children]
            raise EditError(
                "parameter_not_found",
                f"no parameter named {want!r} in {s.qualified or s.kind}",
                [f"parameters: {', '.join(names)}" if names else "<none>"])
        sibs = pl.children
        i = sibs.index(tgt)
        start, end = tgt.start_byte, tgt.end_byte
        if i + 1 < len(sibs) and _node_text(sibs[i + 1]) == ",":
            end = sibs[i + 1].end_byte
        elif i - 1 >= 0 and _node_text(sibs[i - 1]) == ",":
            start = sibs[i - 1].start_byte
        self.splice(start, end, "", f"remove_parameter {s.qualified}.{want}")

    def op_set_extends(self, op):
        s = self.must(op.get("target") or "archetype", op.get("name"),
                      op.get("index"))
        kids = s.node.children
        ob_i = next((i for i, c in enumerate(kids)
                     if _node_text(c) == "("), -1)
        val = (op.get("value") or "").strip()
        if ob_i >= 0:
            cb_i = next(i for i in range(ob_i + 1, len(kids))
                        if _node_text(kids[i]) == ")")
            self.splice(kids[ob_i].end_byte, kids[cb_i].start_byte, val,
                        f"set_extends {s.qualified}")
        elif val:
            nm = _field(s.node, "name")
            self.splice(nm.end_byte, nm.end_byte, f"({val})",
                        f"set_extends {s.qualified}")

    # -- import ops ---------------------------------------------------------

    def op_add_named_import(self, op):
        module = (op.get("value") or "").strip()
        sym = (op.get("newCode") or "").strip()
        for s in (x for x in self.symbols
                  if x.kind == "import" and x.node.type == "import_statement"):
            info = _import_info(s.node)
            if info["path"] != module or not info["is_from"]:
                continue
            bare = sym.split(" as ")[0].strip()
            if any(it.split(" as ")[0].strip() == bare
                   for it in info["items"]):
                return  # idempotent — already imported
            items_node = next(c for c in s.node.children
                              if c.type == "import_items")
            named = items_node.named_children
            if named:
                last = named[-1]
                self.splice(last.end_byte, last.end_byte, ", " + sym,
                            f"add_named_import {module}.{bare}")
            else:
                cb = next(c for c in items_node.children
                          if _node_text(c) == "}")
                self.splice(cb.start_byte, cb.start_byte, sym,
                            f"add_named_import {module}.{bare}")
            return
        self.op_add_import({"value": f"import from {module} {{ {sym} }};"})

    def op_remove_import(self, op):
        module = (op.get("value") or "").strip()
        sym = (op.get("newCode") or "").strip() or None
        for s in (x for x in self.symbols
                  if x.kind == "import" and x.node.type == "import_statement"):
            info = _import_info(s.node)
            if info["path"] != module:
                continue
            if sym and info["is_from"]:
                items_node = next(c for c in s.node.children
                                  if c.type == "import_items")
                named = items_node.named_children
                ident = next((c for c in named
                              if _node_text(c).split(" as ")[0].strip() == sym),
                             None)
                if ident is None:
                    raise EditError(
                        "symbol_not_found",
                        f"{module!r} does not import {sym!r}",
                        [f"items: {', '.join(_node_text(c) for c in named)}"])
                if len(named) > 1:
                    sibs = items_node.children
                    i = sibs.index(ident)
                    start, end = ident.start_byte, ident.end_byte
                    if i + 1 < len(sibs) and _node_text(sibs[i + 1]) == ",":
                        end = sibs[i + 1].end_byte
                    elif i - 1 >= 0 and _node_text(sibs[i - 1]) == ",":
                        start = sibs[i - 1].start_byte
                    self.splice(start, end, "",
                                f"remove_import {module}.{sym}")
                    return
                # last item → remove the whole statement
            ls, le = _trim_removal(self.src, s.node.start_byte, s.node.end_byte)
            self.splice(ls, le, "", f"remove_import {module}")
            return
        raise EditError("symbol_not_found", f"no import of {module!r}",
                        ["add one with add_import first"])

    def op_organize_imports(self, op):
        imports = [s for s in self.symbols
                   if s.kind == "import" and s.node.type == "import_statement"]
        if len(imports) <= 1:
            return
        plains: dict[str, str] = {}
        froms: dict[str, list[str]] = {}
        for s in imports:
            info = _import_info(s.node)
            if info["is_from"]:
                lst = froms.setdefault(info["path"], [])
                for it in info["items"]:
                    if it not in lst:
                        lst.append(it)
            else:
                plains[info["path"]] = f"import {info['path']};"
        lines = [plains[k] for k in sorted(plains)]
        lines += [f"import from {k} {{ {', '.join(froms[k])} }};"
                  for k in sorted(froms)]
        first = imports[0].node
        self.splice(first.start_byte, first.end_byte, "\n".join(lines),
                    "organize_imports")
        for s in imports[1:]:
            ls, le = _trim_removal(self.src, s.node.start_byte, s.node.end_byte)
            self.splice(ls, le, "", "organize_imports")

    # -- module-level declaration creators ----------------------------------

    def _append_module(self, code: str, label: str):
        pre = ""
        if self.src and not self.src.endswith(b"\n\n"):
            pre = "\n" if self.src.endswith(b"\n") else "\n\n"
        self.splice(len(self.src), len(self.src), pre + code, label)

    def _after_imports_pos(self) -> int:
        imports = [s for s in self.symbols if s.kind == "import"]
        return imports[-1].node.end_byte if imports else 0

    def _wrapped(self, head: str, body: str) -> str:
        inner = ("\n" + _indent_block(_renest(body.strip("\n"), ""), "    ")
                 + "\n") if body.strip() else ""
        return f"{head} {{{inner}}}\n"

    def op_add_enum(self, op):
        name = (op.get("value") or "").strip()
        code = self._wrapped(f"enum {name}", op.get("newCode") or "")
        self._append_module(code, f"add_enum {name}")

    def op_add_archetype(self, op):
        kind = (op.get("target") or "obj").lower()
        if kind == "archetype":
            kind = "obj"
        if kind not in ("obj", "node", "edge", "walker", "class"):
            raise EditError(
                "bad_target",
                f"add_archetype target must be obj|node|edge|walker|class, "
                f"got {kind!r}")
        name = (op.get("value") or "").strip()
        code = self._wrapped(f"{kind} {name}", op.get("newCode") or "")
        self._append_module(code, f"add_archetype {kind} {name}")

    def op_add_glob(self, op):
        text = (op.get("newCode") or op.get("value") or "").strip()
        if not text.startswith("glob "):
            text = "glob " + text
        if not text.rstrip().endswith(";"):
            text = text.rstrip() + ";"
        pos = self._after_imports_pos()
        pre = "" if pos == 0 else "\n"
        self.splice(pos, pos, pre + text + "\n", "add_glob")

    def op_add_type_alias(self, op):
        name = (op.get("value") or "").strip()
        texp = (op.get("newCode") or "").strip().rstrip(";")
        pos = self._after_imports_pos()
        pre = "" if pos == 0 else "\n"
        self.splice(pos, pos, pre + f"type {name} = {texp};\n",
                    f"add_type_alias {name}")

    def op_add_impl(self, op):
        code = op["newCode"].strip("\n") + "\n"
        self._append_module(code, "add_impl")

    def op_add_test(self, op):
        name = (op.get("value") or "").strip()
        if not name.startswith(('"', "'")):
            name = f'"{name}"'
        code = self._wrapped(f"test {name}", op.get("newCode") or "")
        self._append_module(code, f"add_test {name}")

    # -- file ops ----------------------------------------------------------

    def op_insert_text(self, op):
        anchor = op.get("value")
        code = op["newCode"]
        if anchor == "after-imports":
            imports = [s for s in self.symbols if s.kind == "import"]
            if imports:
                last = imports[-1].node
                self.splice(last.end_byte, last.end_byte, "\n" + code, "insert_text after-imports")
                return
            pos = 0
            self.splice(0, 0, code + "\n", "insert_text after-imports")
            return
        idx = op.get("index")
        if idx is None or idx == -1:
            pos = len(self.src)
            pre = "\n" if self.src and not self.src.endswith(b"\n") else ""
            self.splice(pos, pos, pre + code + "\n", "insert_text end")
        elif idx == 0:
            self.splice(0, 0, code + "\n", "insert_text start")
        else:
            self.splice(idx, idx, code, "insert_text")

    def op_add_import(self, op):
        # Idempotent: an identical import line is a no-op (Empryo-style merge).
        line = op["value"].rstrip() + "\n"
        norm = " ".join(line.split())
        for existing in self.src.decode("utf-8", "replace").splitlines():
            if " ".join(existing.split()) == norm.rstrip("\n"):
                return  # already present — merge semantics
        imports = [s for s in self.symbols if s.kind == "import"]
        if imports:
            last = imports[-1].node
            self.splice(last.end_byte, last.end_byte, "\n" + line.rstrip("\n"), "add_import")
        else:
            self.splice(0, 0, line, "add_import")


OPS = {
    "rename": Engine.op_rename,
    "remove": Engine.op_remove,
    "set_initializer": Engine.op_set_initializer,
    "set_return_type": Engine.op_set_return_type,
    "set_body": Engine.op_set_body,
    "replace_in_body": Engine.op_replace_in_body,
    "add_statement": Engine.op_add_statement,
    "add_method": Engine.op_add_method,
    "add_property": Engine.op_add_property,
    "add_member": None,  # routed below
    "replace": Engine.op_replace,
    "add_function": Engine.op_add_function,
    "insert_text": Engine.op_insert_text,
    "add_import": Engine.op_add_import,
    "set_type": Engine.op_set_type,
    "add_parameter": Engine.op_add_parameter,
    "remove_parameter": Engine.op_remove_parameter,
    "set_extends": Engine.op_set_extends,
    "add_named_import": Engine.op_add_named_import,
    "remove_import": Engine.op_remove_import,
    "organize_imports": Engine.op_organize_imports,
    "add_enum": Engine.op_add_enum,
    "add_archetype": Engine.op_add_archetype,
    "add_glob": Engine.op_add_glob,
    "add_type_alias": Engine.op_add_type_alias,
    "add_impl": Engine.op_add_impl,
    "add_test": Engine.op_add_test,
}


# --------------------------------------------------------------------------
# entry points


def cmd_symbols(path: Path) -> dict:
    src = path.read_bytes()
    tree = new_parser().parse(src)
    out = []
    for s in _index_symbols(tree.root_node):
        n = s.node
        out.append({
            "kind": s.kind,
            "name": s.name,
            "qualified": s.qualified or None,
            "parent": s.parent,
            "index": s.index,
            "startLine": n.start_point[0] + 1,
            "endLine": n.end_point[0] + 1,
            "startCol": n.start_point[1],
            "detail": _node_text(n).split("\n")[0][:100],
        })
    return {"symbols": out, "hasErrors": tree.root_node.has_error}


def apply_batch(path: Path, operations: list[dict], dry_run=False) -> dict:
    """Apply ops sequentially: each op re-parses the CURRENT source, so spans
    stay valid even when an earlier op nests inside a later one's region.
    Atomicity is preserved at the write level: nothing is written until every
    op has applied and the final source passes the re-parse check."""
    src = path.read_bytes()
    base_errors = _count_errors(new_parser().parse(src).root_node)
    cur = src
    results = []
    for i, op in enumerate(operations):
        action = op.get("action")
        if action == "add_member":
            code = op.get("newCode", "").strip()
            action = "add_property" if code.startswith("has") else "add_method"
        if action == "create_file":
            continue  # handled by caller (extension writes file)
        fn = OPS.get(action)
        if fn is None or not callable(fn):
            raise EditError("unknown_action", f"unknown action {action!r}",
                            [f"known: {', '.join(sorted(k for k in OPS))}"])
        tree = new_parser().parse(cur)
        eng = Engine(cur, tree)
        try:
            fn(eng, op)
            results.append({"op": i, "action": action, "ok": True})
        except EditError as e:
            e.message = f"op[{i}] {action}: {e.message}"
            raise
        for a, b, repl, _label in sorted(eng.splices, key=lambda t: -t[0]):
            cur = cur[:a] + repl + cur[b:]
    newtree = new_parser().parse(cur)
    new_errors = _count_errors(newtree.root_node)
    if new_errors > base_errors:
        old_ex = src.decode("utf-8", "replace")
        new_ex = cur.decode("utf-8", "replace")
        import difflib
        diff = "\n".join(list(difflib.unified_diff(
            old_ex.split("\n"), new_ex.split("\n"), lineterm=""))[2:40])
        raise EditError(
            "syntax_regressed",
            f"batch rejected: parse errors {base_errors} -> {new_errors}; nothing written",
            [diff[:2000]],
        )
    if not dry_run and cur != src:
        path.write_bytes(cur)
    return {
        "ok": True,
        "changed": cur != src,
        "applied": results,
        "bytes": {"before": len(src), "after": len(cur)},
        "lines": {"before": src.count(b"\n") + 1, "after": cur.count(b"\n") + 1},
        "errors": {"before": base_errors, "after": new_errors},
    }


def main(argv):
    if len(argv) < 2:
        print(json.dumps({"error": {"code": "usage", "message": "usage: jac_ast_edit.py symbols|edit <file>"}}))
        return 2
    cmd, filearg = argv[0], argv[1]
    path = Path(filearg)
    if cmd == "create":
        payload = json.loads(sys.stdin.read() or "{}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not payload.get("overwrite"):
            return _out({"error": {"code": "exists", "message": f"{path} exists; pass overwrite:true"}})
        path.write_text(payload.get("content", ""))
        return _out({"ok": True, "created": str(path)})
    if not path.exists():
        return _out({"error": {"code": "no_file", "message": f"{path} not found"}})
    if cmd == "symbols":
        try:
            return _out(cmd_symbols(path))
        except Exception as e:  # noqa: BLE001 — surfaced as JSON
            return _out({"error": {"code": "symbols_failed", "message": str(e)}}, 0)
    if cmd == "edit":
        payload = json.loads(sys.stdin.read() or "{}")
        ops = payload.get("operations")
        if not ops:
            return _out({"error": {"code": "no_ops", "message": "body must be {\"operations\": [...]}"}}, 0)
        try:
            return _out(apply_batch(path, ops, dry_run=payload.get("dryRun", False)))
        except EditError as e:
            return _out({"error": {"code": e.code, "message": e.message,
                                   "suggestions": e.suggestions}}, 0)
        except json.JSONDecodeError as e:
            return _out({"error": {"code": "bad_json", "message": str(e)}}, 0)
    return _out({"error": {"code": "usage", "message": f"unknown command {cmd!r}"}})


def _out(obj, ok_exit=0) -> int:
    print(json.dumps(obj, indent=1))
    return ok_exit if "error" not in obj else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
