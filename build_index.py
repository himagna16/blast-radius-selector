"""Merge the traced map and the untraced timings into the index the selector loads.
usage: build_index.py iso.pkl.gz cost_iso.pkl.gz out.pkl [repo]"""
import ast, gzip, json, os, pickle, re, sys

iso = pickle.load(gzip.open(sys.argv[1], "rb"))
cost = pickle.load(gzip.open(sys.argv[2], "rb"))
assert iso["keys"] == cost["keys"], "trace and timing runs disagree on the item list"
table = [0]
ids = {0: 0}


def share(d):
    """Bitsets are stored once in a table; the maps hold table positions."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = share(v)
        else:
            i = ids.get(v)
            if i is None:
                i = ids[v] = len(table)
                table.append(v)
            out[k] = i
    return out


out = {
    "keys": iso["keys"], "n_tests": iso["n_tests"],
    "rl": share(iso["rl"]), "il": share(iso["il"]), "classes": share(iso["classes"]), "files": share(iso["files"]),
    "cost": cost["cost"], "total": cost["total"],
}

# From the checkout itself: which tests execute, at run time, a line that mentions a given
# identifier; and which sqlglot modules import (directly or not) a given module.
repo = sys.argv[4] if len(sys.argv) > 4 else "/app/repo"
ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
readers = {}
imports = {}
for base in ("sqlglot", "tests"):
    for dp, dn, fn in os.walk(os.path.join(repo, base)):
        dn[:] = sorted(d for d in dn if d not in ("__pycache__", "fixtures"))
        for f in sorted(fn):
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dp, f), repo)
            src = open(os.path.join(dp, f), encoding="utf-8").read()
            rmap = iso["rl"].get(rel, {})
            for i, line in enumerate(src.split("\n"), 1):
                b = rmap.get(i)
                if b:
                    for name in set(ident.findall(line)):
                        readers[name] = readers.get(name, 0) | b
            if base == "sqlglot":
                mod = rel[:-3].replace(os.sep, ".")
                pkg = mod.rsplit(".", 1)[0]
                if mod.endswith(".__init__"):
                    mod = pkg = mod[:-9]
                deps = set()
                tree = ast.parse(src)
                skip = set()        # imports under `if TYPE_CHECKING:` never run
                for node in ast.walk(tree):
                    if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
                        for sub in node.body:
                            skip |= {id(x) for x in ast.walk(sub)}
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        skip |= {id(x) for x in ast.walk(node)} - {id(node)}   # runs when called, not on import
                for node in ast.walk(tree):
                    if id(node) in skip:
                        continue
                    if isinstance(node, ast.Import):
                        deps |= {a.name for a in node.names}
                    elif isinstance(node, ast.ImportFrom):
                        m = node.module or ""
                        if node.level:
                            up = pkg.split(".")
                            up = up[:len(up) - node.level + 1]
                            m = ".".join(up + ([m] if m else []))
                        deps.add(m)
                        deps |= {m + "." + a.name for a in node.names}
                imports[mod] = (rel, {d for d in deps if d.startswith("sqlglot")})
known = set(imports)
# a module also depends on its parent packages
for mod, (rel, deps) in imports.items():
    parts = mod.split(".")
    deps |= {".".join(parts[:k]) for k in range(1, len(parts))}
    deps &= known
dependents = {}
for mod, (rel, deps) in imports.items():
    for d in deps:
        dependents.setdefault(d, set()).add(mod)
closure = {}
for mod, (rel, _) in imports.items():
    seen, todo = {mod}, [mod]
    while todo:
        for m in dependents.get(todo.pop(), ()):
            if m not in seen:
                seen.add(m)
                todo.append(m)
    closure[rel] = sorted(seen)
# lines executed while a pool result is unpickled, per test class that shares one pool (see brrun.SyncPool)
pu = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "pool_unpickle.json")
out["pool_unpickle"] = json.load(open(pu)) if os.path.exists(pu) else {}
out["readers"] = share(readers)
out["dependents"] = closure
out["bits"] = table
pickle.dump(out, open(sys.argv[3], "wb"), protocol=5)
print("index: %d items, %d files with run-time lines, %d classes, %d names, %d distinct bitsets" % (
    len(out["keys"]), len(out["rl"]), len(out["classes"]), len(readers), len(table)))
