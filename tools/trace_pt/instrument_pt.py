"""usage: instrument_pt.py REPO SITES_JSON
Rewrites every sqlglot/**/*.py under REPO (in place: run it on a scratch copy) so that
- each single-operator comparison with == != < <= > >= is `is not` calls _brpt.<op>(site, L, R)
- each `and`/`or` with exactly two operands whose parent is not a BoolOp calls _brpt.band/bor
  (second operand a name / constant / attribute chain: evaluated lazily through a lambda, the
  recorder walks the attributes itself) or _brpt.bx (any other second operand)
keeping evaluation order, values and (as far as possible) line numbers. Writes the sites:
[path, line, column, kind ("cmp"|"bool"), operator, func_first, func_last, stateful, form]
with positions in the ORIGINAL file. func_* = lines of the innermost enclosing def (0 0 at
module/class level); stateful = that def is cached or writes module/class state."""
import ast, json, os, shutil, sys

CMP = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge", ast.Is: "is_", ast.IsNot: "isnot"}
MUT = {"setdefault", "append", "add", "update", "extend", "insert", "pop", "popitem", "clear", "remove", "discard", "__setitem__"}
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def module_names(tree):
    out = set()
    for st in tree.body:
        for n in ast.walk(st) if isinstance(st, (ast.If, ast.Try)) else [st]:
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                ts = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in ts:
                    for m in ast.walk(t):
                        if isinstance(m, ast.Name):
                            out.add(m.id)
    return out


def class_names(tree):
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def stateful(fn, gnames, cnames):
    for d in fn.decorator_list:
        src = ast.dump(d).lower()
        if "cache" in src or "memo" in src or "lru" in src:
            return True
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return True
        tg = []
        if isinstance(n, ast.Assign):
            tg = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            tg = [n.target]
        elif isinstance(n, ast.Delete):
            tg = n.targets
        for t in tg:
            for m in ast.walk(t):
                if isinstance(m, (ast.Subscript, ast.Attribute)):
                    b = m.value
                    while isinstance(b, (ast.Subscript, ast.Attribute)):
                        b = b.value
                    if isinstance(b, ast.Name) and (b.id == "cls" or b.id in cnames or (b.id in gnames and b.id not in params)):
                        return True
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in ("setattr", "delattr"):
                return True
            if isinstance(f, ast.Attribute) and f.attr in MUT:
                b = f.value
                while isinstance(b, (ast.Subscript, ast.Attribute)):
                    b = b.value
                if isinstance(b, ast.Name) and (b.id == "cls" or b.id in cnames or (b.id in gnames and b.id not in params)):
                    return True
    return False


def simple_chain(n):
    """(base source, attribute names) for Name / Constant / attribute chains on them, else None."""
    chain = []
    while isinstance(n, ast.Attribute):
        if n.attr.startswith("__") and not n.attr.endswith("__"):
            return None
        chain.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        if n.id.startswith("__") and not n.id.endswith("__"):
            return None
        return n.id, tuple(reversed(chain))
    if isinstance(n, ast.Constant) and not chain and isinstance(n.value, (int, float, str, bytes, bool, type(None))):
        return repr(n.value), ()
    return None


def rewrite(src, rel, sites):
    tree = ast.parse(src)
    data = src.encode("utf-8")
    starts = [0]
    for ln in data.split(b"\n")[:-1]:
        starts.append(starts[-1] + len(ln) + 1)

    def span(n):
        return starts[n.lineno - 1] + n.col_offset, starts[n.end_lineno - 1] + n.end_col_offset

    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[id(c)] = n
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) or (hasattr(ast, "TemplateStr") and isinstance(n, ast.TemplateStr)):
            for m in ast.walk(n):
                skip.add(id(m))
    gnames, cnames = module_names(tree), class_names(tree)
    fcache = {}

    def enclosing(n):
        p = parent.get(id(n))
        while p is not None and not isinstance(p, FUNCS):
            p = parent.get(id(p))
        if p is None:
            return 0, 0, False
        if id(p) not in fcache:
            fcache[id(p)] = (p.lineno, p.end_lineno, stateful(p, gnames, cnames))
        return fcache[id(p)]

    def lambda_ok(n):
        """A lambda placed at n sees the same names as n."""
        c, p = n, parent.get(id(n))
        while p is not None:
            if isinstance(p, ast.ClassDef):
                return False
            if isinstance(p, FUNCS):
                return any(c is s for s in p.body)
            if isinstance(p, ast.Lambda):
                return c is p.body
            if isinstance(p, COMPS) and c is p.generators[0].iter:
                pass        # the first iterable is evaluated in the enclosing scope: keep climbing
            c, p = p, parent.get(id(p))
        return True

    def truth_ctx(n):
        p = parent.get(id(n))
        if isinstance(p, (ast.If, ast.While, ast.IfExp, ast.Assert)) and p.test is n:
            return 1
        if isinstance(p, ast.comprehension) and any(x is n for x in p.ifs):
            return 1
        if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
            return 1
        return 0

    nodes = []
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in CMP:
            nodes.append(n)
        elif isinstance(n, ast.BoolOp) and len(n.values) == 2 and not isinstance(parent.get(id(n)), ast.BoolOp):
            nodes.append(n)
    nodes.sort(key=lambda n: (span(n)[0], -span(n)[1]))

    def nl(a, b):
        return b"\n" * data[a:b].count(b"\n")

    def render(start, end, cands):
        out, pos, i = [], start, 0
        while i < len(cands):
            n = cands[i]
            ns, ne = span(n)
            inner = []
            j = i + 1
            while j < len(cands) and span(cands[j])[0] < ne:
                inner.append(cands[j])
                j += 1

            def hole(c):
                cs, ce = span(c)
                return render(cs, ce, [m for m in inner if cs <= span(m)[0] and span(m)[1] <= ce])
            sid = len(sites)
            f0, f1, stf = enclosing(n)
            if isinstance(n, ast.Compare):
                L, R = n.left, n.comparators[0]
                sites.append([rel, n.lineno, n.col_offset, "cmp", type(n.ops[0]).__name__, f0, f1, stf, ""])
                text = b"_brpt.%s(%d, (%s)," % (CMP[type(n.ops[0])].encode(), sid, hole(L)) + nl(span(L)[1], span(R)[0]) \
                    + b" (%s)" % hole(R) + nl(span(R)[1], ne) + b")"
            else:
                A, B = n.values
                opn = "And" if isinstance(n.op, ast.And) else "Or"
                sc = simple_chain(B)
                lazy = sc is not None and data[span(B)[0]:span(B)[1]].count(b"\n") == 0 and lambda_ok(n)
                sites.append([rel, n.lineno, n.col_offset, "bool", opn, f0, f1, stf, "lazy" if lazy else "any"])
                if lazy:
                    text = b"_brpt.%s(%d, (%s)," % (b"band" if opn == "And" else b"bor", sid, hole(A)) + nl(span(A)[1], ne) \
                        + b" lambda: (%s), %s, %d)" % (sc[0].encode(), repr(sc[1]).encode(), truth_ctx(n))
                else:
                    text = b"(_brpt.bx(%d, (%s))" % (sid, hole(A)) + nl(span(A)[1], span(B)[0]) \
                        + (b" and (" if opn == "And" else b" or (") + hole(B) + b")" + nl(span(B)[1], ne) + b")"
            out.append(data[pos:ns])
            out.append(text)
            pos = ne
            i = j
        out.append(data[pos:end])
        return b"".join(out)

    new = render(0, len(data), nodes)
    body = tree.body
    k = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        k = 1
    while k < len(body) and isinstance(body[k], ast.ImportFrom) and body[k].module == "__future__":
        k += 1
    lines = new.split(b"\n")
    if k > 0:
        ln = body[k - 1].end_lineno - 1
        lines[ln] = lines[ln] + b"; import _brpt"
    elif body and not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.While, ast.Try, ast.With)) \
            and body[0].col_offset == 0 and body[0].lineno == body[0].end_lineno:
        ln = body[0].lineno - 1
        lines[ln] = b"import _brpt; " + lines[ln]
    else:
        lines.insert(0, b"import _brpt")
    new = b"\n".join(lines)
    compile(new, rel, "exec")
    return new


if __name__ == "__main__":
    repo, sites_path = sys.argv[1], sys.argv[2]
    sites = []
    n = 0
    for dp, dn, fn in os.walk(os.path.join(repo, "sqlglot")):
        dn[:] = sorted(d for d in dn if d != "__pycache__")
        for f in sorted(fn):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, repo)
            src = open(p, encoding="utf-8").read()
            new = rewrite(src, rel, sites)
            open(p, "wb").write(new)
            n += 1
    for dp, dn, fn in os.walk(repo):
        if "__pycache__" in dn:
            shutil.rmtree(os.path.join(dp, "__pycache__"))
            dn.remove("__pycache__")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_brpt.py"), os.path.join(repo, "_brpt.py"))
    json.dump(sites, open(sites_path, "w"))
    print("instrumented %d files, %d sites (%d cmp, %d bool)" % (n, len(sites), sum(s[3] == "cmp" for s in sites), sum(s[3] == "bool" for s in sites)))
