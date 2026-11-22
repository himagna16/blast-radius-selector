"""Patch -> candidate tests, using the prebuilt map (lines / class usage / files)."""
import ast
import re

import applypatch


def _stmt_children(node):
    for name in ("body", "orelse", "finalbody", "handlers", "cases"):
        for ch in getattr(node, name, ()) or ():
            if isinstance(ch, ast.stmt):
                yield ch
            elif isinstance(ch, (ast.ExceptHandler, ast.match_case)):
                yield ch


def _own_lines(node):
    lines = set(range(node.lineno, node.end_lineno + 1)) if hasattr(node, "lineno") else set()
    if isinstance(node, ast.match_case):
        lines = set(range(node.pattern.lineno, node.body[-1].end_lineno + 1))
    for ch in _stmt_children(node):
        if isinstance(ch, ast.match_case):
            a, b = ch.pattern.lineno, ch.body[-1].end_lineno
            hdr = set(range(a, ch.body[0].lineno))
            lines -= set(range(a, b + 1)) - hdr
        elif isinstance(ch, ast.ExceptHandler):
            hdr = set(range(ch.lineno, ch.body[0].lineno)) if ch.body else set()
            lines -= set(range(ch.lineno, ch.end_lineno + 1)) - hdr
        else:
            a = ch.lineno
            if getattr(ch, "decorator_list", None):
                a = min(a, ch.decorator_list[0].lineno)
            lines -= set(range(a, ch.end_lineno + 1))
    return lines


class Site:
    """Where one changed position lives."""
    __slots__ = ("kind", "lines", "func_range", "body_range", "classes", "target", "sig", "docstring", "lam",
                 "assign", "stmt_type", "node", "in_string", "fresh")

    def __init__(self):
        self.kind = "none"      # 'func' | 'import' | 'none'
        self.lines = set()      # lines whose execution means "this change was reached"
        self.func_range = None  # (first, last) of enclosing def / lambda
        self.classes = []       # enclosing class chain e.g. ['Snowflake', 'Tokenizer']
        self.target = []        # names bound by an import-level statement
        self.sig = False        # change sits in a def/lambda signature or decorator
        self.docstring = False
        self.lam = False
        self.body_range = None  # body lines of the def whose signature changed
        self.assign = None      # names/attributes assigned by the innermost statement (None: not an assignment)
        self.stmt_type = None
        self.node = None        # innermost statement (or the def/lambda whose signature holds the position)
        self.in_string = False  # the position sits inside a string literal
        self.fresh = False      # the enclosing def exists only in the patched file

    def __repr__(self):
        return "Site(%s lines=%s cls=%s tgt=%s sig=%s doc=%s)" % (
            self.kind, sorted(self.lines)[:6], ".".join(self.classes), self.target, self.sig, self.docstring)


def assigned_names(stmt):
    """Names / attribute names bound anywhere inside a statement; None when it binds nothing."""
    out = []
    for n in ast.walk(stmt):
        tg = []
        if isinstance(n, ast.Assign):
            tg = n.targets
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            tg = [n.target]
        elif isinstance(n, ast.NamedExpr):
            tg = [n.target]
        for t in tg:
            for x in ast.walk(t):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                    out.append(x.id)
                elif isinstance(x, ast.Attribute) and isinstance(x.ctx, ast.Store):
                    out.append(x.attr)
                elif isinstance(x, ast.Subscript) and isinstance(x.ctx, ast.Store):
                    v = x.value
                    while isinstance(v, ast.Subscript):
                        v = v.value
                    if isinstance(v, ast.Name):
                        out.append(v.id)
                    elif isinstance(v, ast.Attribute):
                        out.append(v.attr)
    return out or None


def _contains(n, line, col):
    lo = getattr(n, "lineno", None)
    if lo is None:
        return False
    a = (lo, n.col_offset)
    if getattr(n, "decorator_list", None):
        d = n.decorator_list[0]
        a = min(a, (d.lineno, 0))
    return a <= (line, col) < (n.end_lineno, n.end_col_offset)


def locate(tree, line, col):
    site = Site()
    path = [tree]
    node = tree
    while True:
        nxt = None
        for ch in ast.iter_child_nodes(node):
            if _contains(ch, line, col):
                nxt = ch
                break
        if nxt is None:
            break
        path.append(nxt)
        node = nxt
    scope = None
    stmt = None
    classes = []
    sig_node = None
    for i, n in enumerate(path):
        nx = path[i + 1] if i + 1 < len(path) else None
        if isinstance(n, ast.stmt):
            stmt = n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if nx is not None and any(nx is b for b in n.body):
                scope, stmt, sig_node = n, None, None
            elif nx is not None or (line, col) < (n.body[0].lineno, 0):
                sig_node = n
            # else: blank/comment line inside the body
        elif isinstance(n, ast.Lambda):
            if nx is n.body or (nx is None and (line, col) >= (n.body.lineno, n.body.col_offset)):
                scope, sig_node = n, None
            else:
                sig_node = n
        elif isinstance(n, ast.ClassDef):
            if nx is not None and any(nx is b for b in n.body):
                classes.append(n.name)
                stmt = None
    site.classes = classes
    leaf = path[-1]
    site.in_string = isinstance(leaf, ast.Constant) and isinstance(leaf.value, (str, bytes)) or isinstance(leaf, ast.JoinedStr)
    site.node = stmt if stmt is not None else sig_node
    if stmt is None and scope is None and sig_node is None:
        return site
    if stmt is not None:
        site.stmt_type = type(stmt).__name__
        if isinstance(stmt, (ast.Return, ast.Yield, ast.Raise, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            site.assign = None
        else:
            site.assign = assigned_names(stmt)
    if sig_node is not None and not isinstance(sig_node, ast.Lambda) and sig_node.body:
        site.body_range = (sig_node.body[0].lineno, sig_node.end_lineno)
    if isinstance(scope, ast.Lambda):
        site.kind, site.lam = "func", True
        site.lines = set(range(scope.body.lineno, scope.body.end_lineno + 1))
        site.func_range = (scope.lineno, scope.end_lineno)
        if sig_node is not None:
            site.lines |= set(range(sig_node.lineno, sig_node.end_lineno + 1))
        return site
    if scope is not None:
        site.kind = "func"
        site.func_range = (scope.body[0].lineno, scope.end_lineno)   # the body: `def` itself runs on import
        if sig_node is not None:
            site.lines = set(range(sig_node.lineno, sig_node.end_lineno + 1))
        elif stmt is not None:
            site.lines = _own_lines(stmt)
            b0 = scope.body[0]
            if stmt is b0 and isinstance(b0, ast.Expr) and isinstance(b0.value, ast.Constant) and isinstance(b0.value.value, str):
                site.docstring = True
        return site
    site.kind = "import"
    if sig_node is not None:
        site.sig = True
        site.func_range = (sig_node.lineno, sig_node.end_lineno)
        site.lines = set(range(sig_node.lineno, sig_node.end_lineno + 1))
        if not isinstance(sig_node, ast.Lambda):
            site.target = [sig_node.name]
            return site
    if stmt is not None:
        if isinstance(stmt, ast.ClassDef):
            site.lines |= {line}
            site.target = [stmt.name]
        else:
            site.lines |= _own_lines(stmt)
            tg = []
            if isinstance(stmt, ast.Assign):
                tg = stmt.targets
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                tg = [stmt.target]
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                site.target = [(a.asname or a.name).split(".")[0] for a in stmt.names]
            for t in tg:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        site.target.append(n.id)
                    elif isinstance(n, ast.Attribute):
                        site.target.append(n.attr)
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                site.docstring = True
    return site


def positions(fp):
    """Changed positions as (old, new) lists of (line, col, text, partner). col is a utf8 byte offset;
    partner is the position on the other side when a removed line was replaced by an added one."""
    old, new = [], []

    def flush(dels, adds):
        if dels and len(dels) == len(adds):
            for (ol, ot), (nl, nt) in zip(dels, adds):
                k = 0
                m = min(len(ot), len(nt))
                while k < m and ot[k] == nt[k]:
                    k += 1
                if k == len(ot.rstrip("\r\n")) and k == len(nt.rstrip("\r\n")):
                    continue
                o = (ol, len(ot[:k].encode()), ot)
                n = (nl, len(nt[:k].encode()), nt)
                old.append(o + (n,))
                new.append(n + (o,))
        else:
            for ol, ot in dels:
                if ot.strip():
                    old.append((ol, len(ot) - len(ot.lstrip()), ot, None))
            for nl, nt in adds:
                if nt.strip():
                    new.append((nl, len(nt) - len(nt.lstrip()), nt, None))

    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol else os_ + 1
        n = ns if nl else ns + 1
        dels, adds = [], []
        for tag, txt in body:
            if tag == " ":
                flush(dels, adds)
                dels, adds = [], []
                o += 1
                n += 1
            elif tag == "-":
                dels.append((o, txt))
                o += 1
            else:
                adds.append((n, txt))
                n += 1
        flush(dels, adds)
    return old, new


def line_maps(fp):
    """new line -> old line. Unchanged lines map exactly; a run of N removed lines replaced by N
    added lines maps pairwise; other added lines map to None."""
    segs = []      # (new_start, new_end_excl, delta) with old = new + delta
    pairs = {}
    delta = 0
    prev_new = 1
    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol else os_ + 1
        n = ns if nl else ns + 1
        segs.append((prev_new, n, delta))
        dels, adds = [], []

        def flush():
            if dels and len(dels) == len(adds):
                for a_, b_ in zip(dels, adds):
                    pairs[b_] = a_
            del dels[:], adds[:]

        for tag, _ in body:
            if tag == " ":
                flush()
                segs.append((n, n + 1, o - n))
                o += 1
                n += 1
            elif tag == "-":
                dels.append(o)
                o += 1
            else:
                adds.append(n)
                n += 1
        flush()
        delta = o - n
        prev_new = n
    segs.append((prev_new, 1 << 30, delta))

    def new2old(line):
        if line in pairs:
            return pairs[line]
        for a, b, d in segs:
            if a <= line < b:
                return line + d
        return None
    return new2old


_OLD_TREES = {}


def _same(a, b):
    return a is not None and b is not None and type(a) is type(b) and ast.dump(a) == ast.dump(b)


def analyze_py(old_src, new_src, fp, path=None):
    """('noop', None) | ('syntax', err) | ('sites', [Site]) with Site.lines/func_range in OLD numbering.

    Only the statements holding a changed position are compared, so an edit that leaves the syntax
    tree alone (a comment, spacing, quoting) is recognised without comparing whole modules."""
    try:
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError) as e:
        return "syntax", str(e)
    got = _OLD_TREES.get(path) if path else None
    if got is None or got[0] != old_src:
        got = (old_src, ast.parse(old_src))
        if path:
            _OLD_TREES[path] = got
    old_tree = got[1]
    oldp, newp = positions(fp)
    new2old = line_maps(fp)
    shape_ok = [type(n) for n in old_tree.body] == [type(n) for n in new_tree.body]
    sites = []
    cache = {}

    def loc(tree, pos):
        k = (id(tree), pos[0], pos[1])
        if k not in cache:
            cache[k] = locate(tree, pos[0], pos[1])
        return cache[k]

    def lone_noop(site, text):
        # a whole line added or removed that is only a comment, outside any string literal
        return shape_ok and text.lstrip().startswith("#") and not site.in_string

    for ln, col, text, partner in oldp:
        s = loc(old_tree, (ln, col))
        if partner is not None:
            if shape_ok and _same(s.node, loc(new_tree, partner).node):
                continue
        elif lone_noop(s, text):
            continue
        if s.kind != "none":
            sites.append(s)
    for ln, col, text, partner in newp:
        s = loc(new_tree, (ln, col))
        if partner is not None:
            if shape_ok and _same(loc(old_tree, partner).node, s.node):
                continue
        elif lone_noop(s, text):
            continue
        if s.kind == "none":
            continue
        s.lines = {o for o in (new2old(x) for x in s.lines) if o is not None}
        rng = s.func_range
        if rng and all(new2old(x) is None for x in range(rng[0], rng[1] + 1)):
            s.fresh = True
        for attr in ("func_range", "body_range"):
            rng = getattr(s, attr)
            if rng:
                a, b = rng
                oa = next((new2old(x) for x in range(a, b + 1) if new2old(x) is not None), None)
                ob = next((new2old(x) for x in range(b, a - 1, -1) if new2old(x) is not None), None)
                setattr(s, attr, (oa, ob) if oa is not None and ob is not None else None)
        sites.append(s)
    if not sites:
        if shape_ok or ast.dump(old_tree) == ast.dump(new_tree):
            return "noop", None
        return "sites", [loc(old_tree, (p[0], p[1])) for p in oldp] + [loc(new_tree, (p[0], p[1])) for p in newp]
    return "sites", sites


# ---------------------------------------------------------------- operator swaps that change no value
_PARTNER = {"Is": "Eq", "Eq": "Is", "IsNot": "NotEq", "NotEq": "IsNot", "Lt": "LtE", "LtE": "Lt", "Gt": "GtE", "GtE": "Gt"}
_OPTOK = re.compile(r"\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>")


def swapped_ops(old_src, new_src, fp, path=None):
    """[(line, column, old operator)] when the patch does nothing but replace comparison operators
    by their partners (is <-> ==, is not <-> !=, < <-> <=, > <-> >=) in single-operator comparisons,
    else None. Positions are those of the comparison in the old file."""
    # the changed lines are taken from the two texts themselves, never from the hunk headers (a
    # hunk still applies when its header is off, by its context)
    old_lines, new_lines = old_src.split("\n"), new_src.split("\n")
    if len(old_lines) != len(new_lines) or not fp.hunks:
        return None
    lines = set()
    for n, (a, b) in enumerate(zip(old_lines, new_lines), 1):
        if a != b:
            if _OPTOK.sub("\0", a) != _OPTOK.sub("\0", b):
                return None
            lines.add(n)
    if not lines:
        return None
    try:
        got = _OLD_TREES.get(path) if path else None
        old_tree = got[1] if got is not None and got[0] == old_src else ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    out = []

    def touched(n):
        a = getattr(n, "lineno", None)
        if a is None:
            return True
        b = getattr(n, "end_lineno", a) or a
        for d in getattr(n, "decorator_list", ()):      # decorators sit above the line of their def
            a = min(a, d.lineno)
        return any(a <= ln <= b for ln in lines)

    def same(x, y):
        if type(x) is not type(y):
            return False
        if getattr(x, "lineno", None) != getattr(y, "lineno", None):
            return False
        if not touched(x):
            return getattr(x, "end_lineno", None) == getattr(y, "end_lineno", None)   # the lines are the same text
        if isinstance(x, ast.Compare) and len(x.ops) == 1 and len(y.ops) == 1 and type(x.ops[0]) is not type(y.ops[0]):
            if _PARTNER.get(type(x.ops[0]).__name__) != type(y.ops[0]).__name__:
                return False
            out.append((x.lineno, x.col_offset, type(x.ops[0]).__name__))
            return same(x.left, y.left) and same(x.comparators[0], y.comparators[0])
        for f in x._fields:
            u, v = getattr(x, f, None), getattr(y, f, None)
            if isinstance(u, list):
                if not isinstance(v, list) or len(u) != len(v):
                    return False
                for p, q in zip(u, v):
                    if isinstance(p, ast.AST):
                        if not isinstance(q, ast.AST) or not same(p, q):
                            return False
                    elif type(p) is not type(q) or repr(p) != repr(q):
                        return False
            elif isinstance(u, ast.AST):
                if not isinstance(v, ast.AST) or not same(u, v):
                    return False
            elif type(u) is not type(v) or repr(u) != repr(v):
                return False
        return True

    try:
        if not same(old_tree, new_tree) or not out:
            return None
    except RecursionError:
        return None
    return out
