"""Trace (or just time) every test and fixture slice, each in its own fresh process.
usage: trace_iso.py ROOT TESTS.json SLICE_META.json.gz OUT.pkl.gz [--workers N] [--notrace]
SLICE_META: output of trace_slices.py (only its 'meta' {test: {fixture: count}} is used)."""
import sys, os, json, gzip, pickle, time, resource
sys.path.insert(0, "/entry"); sys.path.insert(0, "/entry/tools")
root, tj, smeta, outp = sys.argv[1:5]
W = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
TRACE = "--notrace" not in sys.argv
os.environ.setdefault("OMP_NUM_THREADS", "1"); os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import unittest, unittest.mock, decimal, datetime, logging, typing, enum, functools, collections, dataclasses  # noqa
import concurrent.futures, multiprocessing, importlib  # noqa
import numpy, pandas, duckdb  # noqa
from pandas.testing import assert_frame_equal  # noqa
import brrun, brtrace, brtpc
tests = [t["id"] for t in json.load(open(tj))["tests"]]
meta = json.load(gzip.open(smeta, "rt"))["meta"]
items = [(t, None, t) for t in tests]
for t in sorted(meta):
    items.append((t, {}, t + "##"))
    for rel, n in meta[t].items():
        for i in range(n):
            items.append((t, {rel: {i}}, "%s#%s#%d" % (t, rel, i)))
# staged TPC tests: one process per TPC-DS query, yielding phase records
N_DS = meta[brtpc.TPCDS][brtpc.FX_DS]
PH = {}   # key -> index of the phase item
def add(key):
    PH[key] = len(items); items.append((None, None, key))
add(brtpc.TPCDS + "#@S#0"); add(brtpc.TPCDS + "#@A#0")
for i in range(N_DS):
    add("%s#@B#%d" % (brtpc.TPCDS, i)); add("%s#@C#%d" % (brtpc.TPCDS, i))
add(brtpc.TPCH_OPT + "#@S#0"); add(brtpc.TPCH_OPT + "#@A#0"); add(brtpc.TPCH_OPT + "#@C#0")
JOBS = [("item", j) for j in range(len(items)) if items[j][0] is not None] + [("ds", i) for i in range(N_DS)] + [("tpch", 0)]
print("items", len(items), "jobs", len(JOBS), "trace", TRACE, file=sys.stderr, flush=True)

def one(job):
    """Runs in a fresh fork. Returns [(item index, record)]."""
    os.chdir(root); sys.path.insert(0, root); os.environ["PYTHONPATH"] = root
    dn = os.open(os.devnull, os.O_WRONLY); os.dup2(dn, 1); os.dup2(dn, 2)
    tr = brtrace.Tracer(root, lines="--filesonly" not in sys.argv) if TRACE else None
    kind, arg = job
    if kind == "item":
        info = {}
        def rep(key, st, cpu):
            info["st"] = st; info["cpu"] = cpu
        t0 = time.process_time()
        brrun.run_tests([items[arg]], tracer=tr, report=rep)
        info["total"] = time.process_time() - t0
        if tr:
            info.update(tr.record())
        return [(arg, info)]
    ph = brtpc.Phases(tr)
    if tr:
        tr.hook_dialects()
    brrun.import_test_module("tests.test_executor")
    import tests.test_executor as TE
    TE.ProcessPoolExecutor = brrun.LazyPool
    if kind == "ds":
        brtpc.trace_tpcds_query(arg, ph)
        tid, names = brtpc.TPCDS, {"S": "#@S#0", "A": "#@A#0", "B": "#@B#%d" % arg, "C": "#@C#%d" % arg}
    else:
        brtpc.trace_tpch_opt(ph)
        tid, names = brtpc.TPCH_OPT, {"S": "#@S#0", "A": "#@A#0", "C": "#@C#0"}
    rec = tr.record() if tr else {}
    out = []
    for phase, suffix in names.items():
        r = {"st": "pass", "cpu": ph.cpu.get(phase, 0.0), "total": ph.cpu.get(phase, 0.0)}
        if tr:
            p = rec.get("phases", {}).get(phase, {"rl": set(), "il": set()})
            r.update({"rl": p["rl"], "il": p["il"], "classes": rec["classes"], "mro": rec["mro"], "files": rec["files"]})
        out.append((PH[tid + suffix], r))
    return out

def worker(idx, wfd):
    out = os.fdopen(wfd, "wb")
    for j in range(idx, len(JOBS), W):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                data = pickle.dumps(one(JOBS[j]), 5)
            except BaseException as e:
                import traceback
                data = pickle.dumps([(-1, {"st": "crash:%r %s" % (e, traceback.format_exc()[-600:]), "job": JOBS[j]})], 5)
            with os.fdopen(w, "wb") as f:
                f.write(data)
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            data = f.read()
        os.waitpid(pid, 0)
        if not data:
            data = pickle.dumps([(-1, {"st": "died", "job": JOBS[j]})], 5)
        out.write(len(data).to_bytes(4, "little") + data)
        out.flush()
    out.close()
    os._exit(0)

pipes = []
for i in range(W):
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(r)
        worker(i, w)
    os.close(w)
    pipes.append(os.fdopen(r, "rb"))
import select
rbits, ibits, cbits, fbits = {}, {}, {}, {}
mro = {}
cost = [0.0] * len(items); total = [0.0] * len(items); status = [None] * len(items)
done = 0
live = list(pipes)
t0 = time.time()
while live:
    rd, _, _ = select.select(live, [], [])
    for p in rd:
        hdr = p.read(4)
        if len(hdr) < 4:
            live.remove(p); continue
        n = int.from_bytes(hdr, "little")
        for j, rec in pickle.loads(p.read(n)):
            if j < 0:
                print("JOB PROBLEM", rec, file=sys.stderr, flush=True)
                continue
            bit = 1 << j
            status[j] = rec["st"]
            if items[j][2].endswith("#@S#0"):     # the class fixture, seen once per query: keep the largest
                cost[j] = max(cost[j], rec.get("cpu", 0.0)); total[j] = cost[j]
            elif items[j][2].endswith("#@A#0"):   # always re-run for every query: add up
                cost[j] += rec.get("cpu", 0.0); total[j] = cost[j]
            else:
                cost[j] = rec.get("cpu", 0.0); total[j] = rec.get("total", 0.0)
            for k in rec.get("rl", ()):
                rbits[k] = rbits.get(k, 0) | bit
            for k in rec.get("il", ()):
                ibits[k] = ibits.get(k, 0) | bit
            for k in rec.get("classes", ()):
                cbits[k] = cbits.get(k, 0) | bit
                for b_ in rec["mro"].get(k, ()):
                    cbits[b_] = cbits.get(b_, 0) | bit
            mro.update(rec.get("mro", {}))
            for k in rec.get("files", ()):
                fbits[k] = fbits.get(k, 0) | bit
        done += 1
        if done % 500 == 0:
            print(done, "/", len(JOBS), round(time.time() - t0), "s", file=sys.stderr, flush=True)
bad = [(items[j][2], status[j]) for j in range(len(items)) if status[j] != "pass"]
print("non-pass:", len(bad), bad[:15], file=sys.stderr)
def nest(d):
    o = {}
    for (f, ln), b in d.items():
        o.setdefault(f, {})[ln] = b
    return o
res = {"keys": [it[2] for it in items], "n_tests": len(tests), "status": status, "cost": cost, "total": total}
if "--fingerprints" in sys.argv:
    pass
if TRACE:
    res.update({"rl": nest(rbits), "il": nest(ibits), "classes": cbits, "files": fbits, "mro": mro})
with gzip.open(outp, "wb", compresslevel=6) as f:
    pickle.dump(res, f, 5)
print("saved", outp, "sum cost", round(sum(cost[:len(tests)]), 2), "wall", round(time.time() - t0), file=sys.stderr)
