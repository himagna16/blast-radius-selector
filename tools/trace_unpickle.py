"""Lines sqlglot executes while a pool result is unpickled in TestOptimizer (where a shared pool can break).
usage: trace_unpickle.py ROOT TESTS.json OUT.json"""
import sys, os, json
sys.path.insert(0, "/entry"); sys.path.insert(0, "/entry/tools")
root, tj, outp = sys.argv[1:4]
os.chdir(root); sys.path.insert(0, root); os.environ["PYTHONPATH"] = root
import unittest, numpy, pandas, duckdb  # noqa
import brrun, brtrace
tests = [t["id"] for t in json.load(open(tj))["tests"] if "::TestOptimizer::" in t["id"]]
tr = brtrace.Tracer(root)
brrun.PHASE_HOOK = tr.set_phase
res = brrun.run_tests(tests, tracer=tr)
rec = tr.record()
u = rec["phases"].get("U", {"rl": set(), "il": set()})
lines = {}
for f, ln in u["rl"] | u["il"]:
    lines.setdefault(f, set()).add(ln)
json.dump({"tests/test_optimizer.py::TestOptimizer": {f: sorted(v) for f, v in sorted(lines.items())}}, open(outp, "w"))
print("non-pass", [k for k, v in res.items() if v != "pass"][:5], "files", {f: len(v) for f, v in lines.items()}, file=sys.stderr)
