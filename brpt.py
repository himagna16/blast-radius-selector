"""Per-test operator-swap map: recogniser and lookup (pure functions, stdlib only).

The map (pt_map.json.gz, built by tools/merge_pt.py from traces of the pinned commit) says, for
every single-operator comparison and every two-operand and/or in sqlglot/, and for every
alternative operator, which tests can see a difference when the operator is replaced: in such a
test the alternative gave another value at least once, or could not be evaluated without
running package code, or the test consumed state computed there by another test (see README).
A patch that does nothing but such replacements can only fail tests in the union of those sets.
"""
import ast
import gzip
import json
import re

OPS = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot")
_TOK = re.compile(r"\bis\s+not\b|\bis\b|==|!=|<=|>=|<|>|\band\b|\bor\b")


def load(path):
    """-> {path: {(line, col, kind, op): {alt: int bitset of test numbers}}}"""
    d = json.load(gzip.open(path, "rt"))
    out = {}
    for p, rows in d["sites"].items():
        m = out.setdefault(p, {})
        for ln, col, kind, op, alts in rows:
            m[(ln, col, kind, op)] = {a: (int(v, 16) if isinstance(v, str) else sum(1 << t for t in v)) for a, v in alts.items()}
    return out


def edits(old_src, new_src, fp=None, trees=None):
    """[(line, col, kind, old op, new op)] when the new text differs from the old one only by
    comparison operators of single-operator comparisons replaced by other comparison operators
    and/or two-operand `and`/`or` swapped, the rest of both syntax trees identical; else None.
    kind "cmp" (new op one of OPS) or "bool" (op "And"/"Or", new op "swap"). Positions are those
    of the old file. Changed lines are taken from the two texts, never from hunk headers."""
    if fp is not None and not getattr(fp, "hunks", True):
        return None
    old_lines, new_lines = old_src.split("\n"), new_src.split("\n")
    if len(old_lines) != len(new_lines):
        return None
    lines = set()
    for n, (a, b) in enumerate(zip(old_lines, new_lines), 1):
        if a != b:
            if _TOK.sub("\0", a) != _TOK.sub("\0", b):
                return None
            lines.add(n)
    if not lines:
        return None
    try:
        old_tree, new_tree = trees() if trees else (ast.parse(old_src), ast.parse(new_src))
    except (SyntaxError, ValueError, RecursionError):
        return None
    out = []

    def touched(n):
        a = getattr(n, "lineno", None)
        if a is None:
            return True
        b = getattr(n, "end_lineno", a) or a
        for d in getattr(n, "decorator_list", ()):
            a = min(a, d.lineno)
        return any(a <= ln <= b for ln in lines)

    def same(x, y, parent_bool=False):
        if type(x) is not type(y):
            return False
        if getattr(x, "lineno", None) != getattr(y, "lineno", None):
            return False
        if not touched(x):
            return getattr(x, "end_lineno", None) == getattr(y, "end_lineno", None)   # the lines are the same text
        if isinstance(x, ast.Compare):
            if len(x.ops) != len(y.ops):
                return False
            if len(x.ops) == 1 and type(x.ops[0]) is not type(y.ops[0]):
                a, b = type(x.ops[0]).__name__, type(y.ops[0]).__name__
                if a not in OPS or b not in OPS:
                    return False
                out.append((x.lineno, x.col_offset, "cmp", a, b))
                return same(x.left, y.left) and same(x.comparators[0], y.comparators[0])
        if isinstance(x, ast.BoolOp) and type(x.op) is not type(y.op):
            if len(x.values) != 2 or len(y.values) != 2 or parent_bool:
                return False
            out.append((x.lineno, x.col_offset, "bool", type(x.op).__name__, "swap"))
            return same(x.values[0], y.values[0], True) and same(x.values[1], y.values[1], True)
        pb = isinstance(x, ast.BoolOp)
        for f in x._fields:
            u, v = getattr(x, f, None), getattr(y, f, None)
            if isinstance(u, list):
                if not isinstance(v, list) or len(u) != len(v):
                    return False
                for p, q in zip(u, v):
                    if isinstance(p, ast.AST):
                        if not isinstance(q, ast.AST) or not same(p, q, pb):
                            return False
                    elif type(p) is not type(q) or repr(p) != repr(q):
                        return False
            elif isinstance(u, ast.AST):
                if not isinstance(v, ast.AST) or not same(u, v, pb):
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


def swapped_sites(old_src, new_src, fp, path, ptmap, trees=None):
    """[((path, line, col, kind, op), alt)] when the patch to `path` is nothing but operator
    replacements at sites of the map, else None."""
    if not path.startswith("sqlglot/") or not path.endswith(".py") or path.startswith("sqlglot/typing/"):
        return None
    sites = ptmap.get(path)
    if not sites:
        return None
    ed = edits(old_src, new_src, fp, trees)
    if ed is None:
        return None
    out = []
    for ln, col, kind, op, new in ed:
        alts = sites.get((ln, col, kind, op))
        if alts is None or new not in alts:
            return None
        out.append(((path, ln, col, kind, op), new))
    return out


def affected(ptmap, found):
    """Union of the affected tests (int bitset) of [(site, alt)] from swapped_sites."""
    b = 0
    for (path, ln, col, kind, op), alt in found:
        b |= ptmap[path][(ln, col, kind, op)][alt]
    return b
