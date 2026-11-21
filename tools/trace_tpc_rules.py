"""Which lines each optimizer rule executes for each TPC-DS query -> data/tpc_rules.json.gz.

usage (inside the image):  python trace_tpc_rules.py /app/repo OUT.json.gz [--workers N]

test_execute_tpcds optimizes every query before running it. The stage-B trace of trace_iso.py
covers that as a whole; here the same real call is traced rule by rule (phase "R<k>" for
optimizer.RULES[k]; "B0" is everything else between entering mp_execute and optimize returning)."""
import gzip
import inspect
import json
import os
import pickle
import sys
from multiprocessing import get_context

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
root = os.path.realpath(sys.argv[1])
W = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
os.environ.setdefault("OMP_NUM_THREADS", "1")
import brstate  # noqa: E402

brstate.preload()
import brrun  # noqa: E402
import brtpc  # noqa: E402
import brtrace  # noqa: E402

_S = object()


def wrap(rule, k, ph):
    """Same parameter names as the rule (the driver passes keyword arguments by name)."""
    args = inspect.getfullargspec(rule).args
    src = "def w(%s):\n    kw = {n: v for n, v in (%s) if v is not _S}\n    ph.set('R%d')\n    try:\n        return rule(%s, **kw)\n    finally:\n        ph.set('B0')\n" % (
        ", ".join([args[0]] + ["%s=_S" % a for a in args[1:]]),
        "".join("(%r, %s), " % (a, a) for a in args[1:]), k, args[0])
    ns = {"_S": _S, "ph": ph, "rule": rule}
    exec(src, ns)
    return ns["w"]


def one(i):
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root
    tr = brtrace.Tracer(root + "/", lines=True)
    ph = brtpc.Phases(tr)
    tr.hook_dialects()
    ph.set("pre")
    brrun.import_test_module("tests.test_executor")
    H, TE, X, Table = brtpc._mods()
    from sqlglot.optimizer import optimizer as O
    TE.initializer(H.TPCDS_SCHEMA, brtpc._dummy_tables(H, Table))
    meta, sql, _ = brtpc._pairs(H, brtpc.FX_DS)[i]
    if not meta.get("execute"):
        return i, None, None
    expr = pickle.loads(pickle.dumps(TE.parse_one(sql)))
    orig = X.optimize

    def opt(*a, **k):
        r = orig(*a, rules=[wrap(rule, n, ph) for n, rule in enumerate(O.RULES)], **k)
        ph.set("post")
        raise brtpc._Short()

    X.optimize = opt
    ph.set("B0")
    try:
        TE.mp_execute(expr, meta)
    except brtpc._Short:
        pass
    ph.set(None)
    rec = tr.record()
    out = {}
    for name, d in rec.get("phases", {}).items():
        if name == "B0" or name.startswith("R"):
            out[name] = sorted(set(d["rl"]) | set(d["il"]))
    return i, out, [r.__name__ for r in O.RULES]


if __name__ == "__main__":
    n = 99
    cover = {}
    rules = None
    with get_context("fork").Pool(W, maxtasksperchild=1) as pool:   # a fresh process per query
        for i, out, names in pool.imap(one, range(n), chunksize=1):
            if out is None:
                continue
            rules = rules or names
            for phase, lines in out.items():
                m = cover.setdefault(phase, {})
                for f, ln in lines:
                    m.setdefault(f, {}).setdefault(str(ln), []).append(i)
    json.dump({"rules": rules, "cover": cover}, gzip.open(sys.argv[2], "wt"), separators=(",", ":"))
    print({k: sum(len(v) for v in m.values()) for k, m in sorted(cover.items())}, file=sys.stderr)
