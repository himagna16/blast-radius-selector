"""Merge the traced map and the untraced timings into the index the selector loads.
usage: build_index.py iso.pkl.gz cost_iso.pkl.gz out.pkl [repo]"""
import ast, gzip, json, os, pickle, re, sys

iso = pickle.load(gzip.open(sys.argv[1], "rb"))
cost = pickle.load(gzip.open(sys.argv[2], "rb"))
assert iso["keys"] == cost["keys"], "trace and timing runs disagree on the item list"
# Dependencies that only show when tests share a process (traced in suite order): a test that runs
# after another one cleared some state executes lines it never reaches alone.
wp = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "warm.json")
warm = json.load(open(wp)) if os.path.exists(wp) else {}
tnum = {k: i for i, k in enumerate(iso["keys"][:iso["n_tests"]])}
for f, lines in warm.items():
    for ln, tests in lines.items():
        m = iso["rl"].setdefault(f, {})
        b = m.get(int(ln), 0)
        for t in tests:
            if t in tnum:
                b |= 1 << tnum[t]
        m[int(ln)] = b

# One-line lambdas / defs / generator expressions that also run while modules import: the tracer
# cannot see their later run-time calls (tools/trace_import_calls.py explains why), so every item
# that executed the line during an import is taken to run it.
ip = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "import_calls.json")
import_calls = json.load(open(ip)) if os.path.exists(ip) else {}
if import_calls:
    for f, lines in import_calls.items():
        for ln in lines:
            b = iso["il"].get(f, {}).get(ln, 0)
            if b:
                m = iso["rl"].setdefault(f, {})
                m[ln] = m.get(ln, 0) | b

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
# which keys of class-level dicts / sets each test looks up (tools/trace_keys.py)
tc = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "tpc_cost.json")
# measured seconds per TPC-DS query: sqlglot's executor ("exec") and the DuckDB comparison ("duck")
out["tpc_cost"] = {k: {int(i): v for i, v in m.items()} for k, m in json.load(open(tc)).items()} if os.path.exists(tc) else {}
ts = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "tpc_sentinels.json")
if os.path.exists(ts):
    out["tpc_sentinels"] = json.load(open(ts))["order"]
tr = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "tpc_rules.json.gz")
# which lines each optimizer rule executes for each TPC-DS query (tools/trace_tpc_rules.py)
if os.path.exists(tr):
    d = json.load(gzip.open(tr, "rt"))
    out["tpc_rules"] = {"rules": d["rules"], "cost": d.get("cost"),
                        "cover": {ph: {f: {int(ln): tuple(q) for ln, q in m.items()} for f, m in fm.items()}
                                  for ph, fm in d["cover"].items()}}
kp = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "keys.pkl.gz")
lookups = {}
if os.path.exists(kp):
    kd = pickle.load(gzip.open(kp, "rb"))
    remap = [tnum.get(t) for t in kd["tests"]]
    assert all(r == i for i, r in enumerate(remap)), "key trace made for another test list"
    lookups = kd["lookups"]
# comparisons at which an operator and its partner agree on every evaluation (tools/trace_cmp)
cp = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "cmp_same.json")
cmp_same = {}
if os.path.exists(cp):
    for f, rows in json.load(open(cp))["same"].items():
        cmp_same[f] = {(ln, col): op for ln, col, op in rows}
out["cmp_same"] = cmp_same
# per test: at which comparisons / two-operand and-or an operator replacement can show (tools/trace_pt).
# Kept in a file of its own (read only for a patch that is nothing but such replacements); the index
# gets the files it covers.
pp = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "pt_map.json.gz")
out["pt_files"] = ()
if os.path.exists(pp):
    d = json.load(gzip.open(pp, "rt"))
    pos = [tnum.get(i) for i in d["ids"]]
    assert all(x is not None for x in pos) and len(pos) == iso["n_tests"], "operator map made for another test list"
    pt = {}
    for f, rows in d["sites"].items():
        m = pt[f] = {}
        for ln, col, kind, op, alts in rows:
            a = m[(ln, col, kind, op)] = {}
            for alt, v in alts.items():
                nums = [k for k in range(len(pos)) if int(v, 16) >> k & 1] if isinstance(v, str) else v
                a[alt] = sum(1 << pos[k] for k in nums)
    pickle.dump(pt, open(os.path.splitext(sys.argv[3])[0] + ".pt.pkl", "wb"), protocol=5)
    out["pt_files"] = tuple(sorted(pt))
# places where the package leaves shared state behind when an exception interrupts them: the lines
# of the window, a pattern for test code that can newly trigger it, and the tests that fail while
# the state is left behind (measured on the pinned commit with the state forced)
lp = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "leaks.json")
leaks = []
if os.path.exists(lp):
    for row in json.load(open(lp))["leaks"]:
        v = 0
        for t in row["victims"]:
            if t in tnum:
                v |= 1 << tnum[t]
        leaks.append((row["path"], tuple(row["lines"]), row.get("added_test_text"), v, tuple(row.get("watch") or ()), frozenset(row.get("at_rest") or ())))
out["leaks"] = leaks
out["import_calls"] = import_calls
out["lookups"] = share(lookups)
out["warm"] = {f: sorted(int(x) for x in lines) for f, lines in warm.items()}
out["readers"] = share(readers)
out["dependents"] = closure
out["bits"] = table
pickle.dump(out, open(sys.argv[3], "wb"), protocol=5)
print("index: %d items, %d files with run-time lines, %d classes, %d names, %d distinct bitsets" % (
    len(out["keys"]), len(out["rl"]), len(out["classes"]), len(readers), len(table)))
