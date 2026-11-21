"""Trace fixture-driven tests one fixture entry at a time.
usage: trace_slices.py ROOT MAP.json.gz COSTS.json OUT.json.gz [min_cost]"""
import sys, os, json, gzip, time
sys.path.insert(0, "/entry"); sys.path.insert(0, "/entry/tools")
root, mapp, costp, outp = sys.argv[1:5]
MIN = float(sys.argv[5]) if len(sys.argv) > 5 else 0.1
os.chdir(root); sys.path.insert(0, root); os.environ["PYTHONPATH"] = root
import brrun, brtrace
m = json.load(gzip.open(mapp, "rt"))
costs = json.load(open(costp))["dur"]
targets = []
for t, rec in m["tests"].items():
    fx = [f for f in rec["files"] if f.startswith("tests/fixtures/") and f.endswith(".sql")]
    if t in brrun.ATTR_SLICES:
        fx = ["tests/fixtures/" + brrun.ATTR_SLICES[t][1]]
    if fx and (costs.get(t, 0) >= MIN or t in brrun.ATTR_SLICES):
        targets.append((t, fx))
print("sliceable tests:", len(targets), file=sys.stderr)
tracer = brtrace.Tracer(root)
out_cost = {}
meta = {}
import tests.helpers as H
bad = []
items = []
for t, fx in sorted(targets):
    counts = {}
    for f in fx:
        rel = f[len("tests/fixtures/"):]
        loader = H.load_sql_fixtures if (rel in ("identity.sql", "partial.sql") or rel.endswith("__invalid.sql")) else H.load_sql_fixture_pairs
        counts[rel] = sum(1 for _ in loader(rel))
    items.append((t, {}, t + "##"))
    for rel, n in counts.items():
        for i in range(n):
            items.append((t, {rel: {i}}, "%s#%s#%d" % (t, rel, i)))
    meta[t] = counts
def rep(key, st, cpu):
    out_cost[key] = round(cpu, 5)
    if st != "pass":
        bad.append((key, st))
brrun.run_tests(items, tracer=tracer, report=rep)
for t, fx in sorted(targets):
    print(t.split("::")[-1], meta[t], "base %.3f" % out_cost[t + "##"], "sum %.2f" % sum(v for k, v in out_cost.items() if k.startswith(t + "#")), "full(traced) %.2f" % costs.get(t, 0), file=sys.stderr, flush=True)
print("non-passing slices:", bad[:20], len(bad), file=sys.stderr)
tracer.save(outp + ".tmp")
d = json.load(gzip.open(outp + ".tmp", "rt"))
json.dump({"files": d["files"], "slices": {k: v for k, v in d["tests"].items() if "#" in k}, "cost": out_cost, "meta": meta, "mro": d["mro"]}, gzip.open(outp, "wt"))
os.remove(outp + ".tmp")
