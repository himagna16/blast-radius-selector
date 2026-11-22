"""usage: instrument.py REPO SITES_JSON
Rewrites every sqlglot/**/*.py under REPO so that each single-operator comparison with
is / is not / == / != / < / <= / > / >= goes through _brcmp (same value, evaluation order and line
numbers), and writes the list of sites: [path, line, column, operator] with positions in the
ORIGINAL file."""
import ast, json, os, shutil, sys

# `==` and `!=` are not traced: the map never uses them (see merge.py)
FAM = {ast.Is: "is_", ast.IsNot: "isnot", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}


def rewrite(src, rel, sites):
    tree = ast.parse(src)
    data = src.encode("utf-8")
    starts = [0]
    for ln in data.split(b"\n")[:-1]:
        starts.append(starts[-1] + len(ln) + 1)

    def span(n):
        return starts[n.lineno - 1] + n.col_offset, starts[n.end_lineno - 1] + n.end_col_offset

    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) or (hasattr(ast, "TemplateStr") and isinstance(n, ast.TemplateStr)):
            for m in ast.walk(n):
                skip.add(id(m))
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in FAM and id(n) not in skip]
    nodes.sort(key=lambda n: (span(n)[0], -span(n)[1]))

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
            ls, le = span(n.left)
            rs, re_ = span(n.comparators[0])
            L = render(ls, le, [m for m in inner if ls <= span(m)[0] and span(m)[1] <= le])
            R = render(rs, re_, [m for m in inner if rs <= span(m)[0] and span(m)[1] <= re_])
            sid = len(sites)
            sites.append([rel, n.lineno, n.col_offset, type(n.ops[0]).__name__])
            text = b"_brcmp.%s(%d, (%s), (%s)" % (FAM[type(n.ops[0])].encode(), sid, L, R)
            text += b"\n" * (data[ns:ne].count(b"\n") - text.count(b"\n")) + b")"
            out.append(data[pos:ns])
            out.append(text)
            pos = ne
            i = j
        out.append(data[pos:end])
        return b"".join(out)

    new = render(0, len(data), nodes)
    # import of the recorder, without moving any line when that is possible
    body = tree.body
    k = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        k = 1
    while k < len(body) and isinstance(body[k], ast.ImportFrom) and body[k].module == "__future__":
        k += 1
    lines = new.split(b"\n")
    if k > 0:
        ln = body[k - 1].end_lineno - 1          # same line numbers in `new` as in the original
        lines[ln] = lines[ln] + b"; import _brcmp"
    elif body and not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.While, ast.Try, ast.With)) \
            and body[0].col_offset == 0:
        ln = body[0].lineno - 1
        lines[ln] = b"import _brcmp; " + lines[ln]
    else:
        lines.insert(0, b"import _brcmp")
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
            assert new.count(b"\n") == src.encode().count(b"\n") or not src.strip() or b"import _brcmp\n" == new[:15], rel
            open(p, "wb").write(new)
            n += 1
    for dp, dn, fn in os.walk(repo):
        if "__pycache__" in dn:
            shutil.rmtree(os.path.join(dp, "__pycache__"))
            dn.remove("__pycache__")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_brcmp.py"), os.path.join(repo, "_brcmp.py"))
    json.dump(sites, open(sites_path, "w"))
    print("instrumented %d files, %d sites" % (n, len(sites)))
