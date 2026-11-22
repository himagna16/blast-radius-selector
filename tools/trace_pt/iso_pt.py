"""Run every test alone in a fresh forked process (as entry/tools/trace_iso.py does, with
entry/brrun.run_tests: process pools run in-process) with BRPT_TEST set, so everything that
process evaluates (module imports, class set-up, pool tasks, subprocesses) is attributed to it.
usage: iso_pt.py ROOT TESTS.json OUTDIR [--workers N] [--only i,j,...]
Writes OUTDIR/rec.iso.<pid> (via _brpt) and OUTDIR/status.json {number: status}."""
import sys, os, json, time
sys.path.insert(0, "/entry")
root, tj, outdir = sys.argv[1:4]
W = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
os.environ.setdefault("OMP_NUM_THREADS", "1"); os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import unittest, unittest.mock, decimal, datetime, logging, typing, enum, functools, collections, dataclasses  # noqa
import concurrent.futures, multiprocessing, importlib  # noqa
import numpy, pandas, duckdb  # noqa
from pandas.testing import assert_frame_equal  # noqa
import brrun
assert "sqlglot" not in sys.modules and "_brpt" not in sys.modules
tests = json.load(open(tj))["tests"]
todo = [t for t in tests]
if "--only" in sys.argv:
    keep = {int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")}
    todo = [t for t in tests if t["number"] in keep]
os.makedirs(outdir, exist_ok=True)
TIMEOUT = 1500


def one(t):
    os.chdir(root); sys.path.insert(0, root); os.environ["PYTHONPATH"] = root
    os.environ["BRPT_TEST"] = str(t["number"]); os.environ["BRPT_DIR"] = outdir; os.environ["BRPT_TAG"] = "iso"
    dn = os.open(os.devnull, os.O_WRONLY); os.dup2(dn, 1); os.dup2(dn, 2)
    signal_alarm(TIMEOUT)
    st = brrun.run_tests([t["id"]])
    import _brpt
    _brpt.flush()
    return st.get(t["id"], "missing")


def signal_alarm(n):
    import signal
    signal.alarm(n)


def worker(idx, wfd):
    out = os.fdopen(wfd, "w")
    for j in range(idx, len(todo), W):
        r, w = os.pipe()
        t0 = time.time()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                s = one(todo[j])
            except BaseException as e:
                s = "crash:%r" % (e,)
                try:
                    import _brpt
                    _brpt.flush()
                except BaseException:
                    pass
            os.write(w, s.encode()[:500])
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            data = f.read().decode() or "died"
        os.waitpid(pid, 0)
        out.write(json.dumps([todo[j]["number"], data, round(time.time() - t0, 2)]) + "\n")
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
    pipes.append(os.fdopen(r, "r"))
import select
status = {}
t0 = time.time()
live = list(pipes)
while live:
    rd, _, _ = select.select(live, [], [])
    for p in rd:
        line = p.readline()
        if not line:
            live.remove(p)
            continue
        n, s, dt = json.loads(line)
        status[n] = [s, dt]
        if len(status) % 100 == 0:
            print(len(status), "/", len(todo), round(time.time() - t0), "s", file=sys.stderr, flush=True)
json.dump(status, open(os.path.join(outdir, "status.json"), "w"))
bad = {n: s for n, s in status.items() if s[0] != "pass"}
print("done", len(status), "non-pass", len(bad), list(bad.items())[:20], "wall", round(time.time() - t0), file=sys.stderr)
