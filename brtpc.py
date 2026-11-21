"""Staged checks for the two TPC executor tests, the dearest tests in the suite.

test_execute_tpcds runs every TPC-DS query through four stages:
    A  parse the query, pickle it to a worker           (cheap)
    B  optimize it                                       (cheap)
    C  plan it and run it on the pure-Python executor    (nearly all of the 21 s)
    D  transpile to DuckDB, run there, compare frames
A stage's result can only differ from the pinned commit's when its code or its input differs.
So A, B and the transpile of D are re-run under the patch and their outputs compared with
fingerprints taken from the pinned commit; stage C (and the DuckDB comparison) is only run for
real on the queries where the patch reaches stage-C code or an input fingerprint changed.
Stage C itself has two halves: sqlglot's executor produces a table, and DuckDB's answer to the
transpiled query is compared with it. Only the first half runs sqlglot code, so the table the
pinned commit produces is fingerprinted too: a query whose table and transpiled SQL both match
the pinned ones compares exactly as it did there, and the DuckDB half is skipped for it.

test_optimized_tpch compares DuckDB results of transpiled TPC-H queries: with the transpiled
strings unchanged, nothing it does can change.

The fingerprints are a map of the pinned commit (built in the Dockerfile), like the coverage map.
"""
import hashlib
import json
import pickle
import time

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCH_OPT = "tests/test_executor.py::TestExecutor::test_optimized_tpch"
FX_DS = "optimizer/tpc-ds/tpc-ds.sql"
FX_H = "optimizer/tpc-h/tpc-h.sql"


class _Short(BaseException):
    """Leaves sqlglot.executor.execute right after the optimizer returned."""


def _h(s):
    if isinstance(s, str):
        s = s.encode("utf-8", "surrogatepass")
    return hashlib.sha1(s).hexdigest()[:20]


def _hexpr(e):
    return _h(json.dumps(e.dump(), sort_keys=True, default=repr))


def _htable(t):
    """Fingerprint of an executor result: column names, and every cell with its type."""
    return _h(repr((tuple(t.columns), [tuple((type(c).__name__, repr(c)) for c in r) for r in t.rows])))


class Phases:
    """Phase switch used while tracing / timing; a no-op when checking."""

    def __init__(self, tracer=None):
        self.tracer = tracer
        self.cpu = {}
        self._cur = None
        self._t = time.process_time()

    def set(self, name):
        now = time.process_time()
        if self._cur is not None:
            self.cpu[self._cur] = self.cpu.get(self._cur, 0.0) + now - self._t
        self._cur = name
        if self.tracer is not None:
            self.tracer.set_phase(name)
        self._t = time.process_time()


def _mods():
    import tests.helpers as H
    import tests.test_executor as TE
    import sqlglot.executor as X
    from sqlglot.executor.table import Table
    return H, TE, X, Table


def _pairs(H, fx):
    loader = getattr(H.load_sql_fixture_pairs, "__wrapped__", H.load_sql_fixture_pairs)
    return list(loader(fx))


def _dummy_tables(H, Table):
    return {name: Table(columns=cols, rows=[]) for name, cols in H.TPCDS_SCHEMA.items()}


def _stage_b(TE, X, expr, meta):
    """Run mp_execute up to the end of optimize; return the optimized expression's fingerprint."""
    box = {}
    orig = X.optimize

    def opt(*a, **k):
        r = orig(*a, **k)
        box["h"] = _hexpr(r)
        raise _Short()

    X.optimize = opt
    try:
        TE.mp_execute(expr, meta)
    except _Short:
        pass
    finally:
        X.optimize = orig
    return box.get("h")


def result_hashes():
    """Run on the pinned commit: fingerprint of the table each executed TPC-DS query produces."""
    H, TE, X, Table = _mods()
    cls = TE.TestExecutor
    cls.setUpClass()
    out = {}
    try:
        TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
        for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
            if meta.get("execute"):
                try:
                    expr = pickle.loads(pickle.dumps(TE.parse_one(sql)))
                    out[i] = _htable(pickle.loads(pickle.dumps(TE.mp_execute(expr, meta))))
                except Exception:
                    out[i] = None
    finally:
        cls.tearDownClass()
    return out


def fingerprints(results=()):
    """Run on the pinned commit. `results`: result_hashes() of other processes; a query's result
    fingerprint is kept only when every process agrees on it."""
    H, TE, X, Table = _mods()
    TE.initializer(H.TPCDS_SCHEMA, _dummy_tables(H, Table))
    ds = {}
    for i, (meta, sql, _) in enumerate(_pairs(H, FX_DS)):
        expr = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        if meta.get("execute"):
            ds[i] = (_hexpr(expr), _stage_b(TE, X, expr, meta), _h(TE.transpile(sql, write="duckdb")[0]))
        else:
            ds[i] = (_hexpr(expr), None, None)
    tpch = {}
    for i, (_, sql, optimized) in enumerate(_pairs(H, FX_H)):
        tpch[i] = (_h(TE.transpile(sql, write="duckdb")[0]), _h(TE.transpile(optimized, write="duckdb")[0]))
    res = {}
    if results:
        first = results[0]
        res = {i: h for i, h in first.items() if h is not None and all(r.get(i) == h for r in results)}
    return {"ds": ds, "h": tpch, "res": res}


def build(path, seeds=("0", "1", "2")):
    """Write the fingerprint file. The result fingerprints are taken in separate processes with
    different string-hash seeds, so one that depends on set order is never trusted."""
    import os
    import subprocess
    import sys
    import tempfile
    results = []
    for seed in seeds:
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            tmp = f.name
        try:
            code = "import pickle, brtpc; pickle.dump(brtpc.result_hashes(), open(%r, 'wb'))" % tmp
            subprocess.run([sys.executable, "-c", code], check=True, env=dict(os.environ, PYTHONHASHSEED=seed),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            results.append(pickle.load(open(tmp, "rb")))
        finally:
            os.unlink(tmp)
    fp = fingerprints(results)
    pickle.dump(fp, open(path, "wb"))
    return fp


STATS = {"same": 0, "compared": 0, "todo": 0, "work": 0.0}


def check_tpcds(fp, need_b, need_c, cost=None, allow=None, duck=None, deadline=None):
    import signal
    try:
        return _check_tpcds(fp, need_b, need_c, cost, allow, duck, deadline)
    finally:
        if deadline is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _check_tpcds(fp, need_b, need_c, cost, allow, duck, deadline):
    """-> ('pass' | 'fail' | 'giveup', None), or ('real', None) when the fixture no longer matches the map.

    First every query's cheap stages are compared with the pinned fingerprints, which settles
    which queries need stage C re-run and which of those will need DuckDB as well (their
    transpiled SQL changed); the first such query is run as soon as it is found. If the rest of
    that work (`cost`: executor seconds per query, `duck`: DuckDB
    seconds per query) adds up to more than `allow`, checking is dearer than running the test and
    the answer is 'giveup'. Otherwise the queries are run right here, cheapest first, on the real
    tables. A table that differs from the pinned commit's goes through the test's own helper
    (DuckDB and the frame comparison); the first failure ends it.

    With `deadline` (time.time() value) every step runs under the caller's SIGALRM timer, a few
    seconds at most each, so a change that makes one query loop costs seconds, not the budget."""
    import signal

    def arm(seconds):
        if deadline is not None:
            left = deadline - time.time()
            signal.setitimer(signal.ITIMER_REAL, max(0.05, min(seconds, left)))

    H, TE, X, Table = _mods()
    pairs = _pairs(H, FX_DS)
    if len(pairs) != len(fp["ds"]):
        return "real", None
    dummy = _dummy_tables(H, Table)
    TE.initializer(H.TPCDS_SCHEMA, dummy)
    exprs = {}
    arm(6.0)
    for i, (meta, sql, _) in enumerate(pairs):
        try:
            exprs[i] = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        except Exception:
            return "fail", None          # the test submits every query before it looks at any result
    box = {}
    res = fp.get("res") or {}
    cost = cost or {}
    duck = duck or {}
    same_sql = {}

    def real(i):
        cls = TE.TestExecutor
        if "case" not in box:
            arm(10.0)
            cls.setUpClass()
            box["case"] = cls("test_execute_tpcds")
        TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
        try:
            arm(max(2.0, 20.0 * cost.get(i, 0.0)))
            table = pickle.loads(pickle.dumps(TE.mp_execute(exprs[i], pairs[i][0])))
            if same_sql.get(i) and res.get(i) is not None and _htable(table) == res[i]:
                STATS["same"] += 1
                return                   # same table, same DuckDB query: compares as on the pinned commit
            STATS["compared"] += 1
            arm(max(3.0, 20.0 * duck.get(i, 0.0)))
            box["case"].subtestHelper(i, table, tpch=False)
        finally:
            TE.initializer(H.TPCDS_SCHEMA, dummy)

    order = sorted((i for i in exprs if pairs[i][0].get("execute")), key=lambda i: (cost.get(i, 0.0), i))
    todo = []
    for i in order:
        meta, sql, _ = pairs[i]
        h1, h2, h3 = fp["ds"][i][:3]
        need_real = i in need_c
        arm(2.0)
        try:
            if not need_real and (i in need_b or _hexpr(exprs[i]) != h1):
                need_real = _stage_b(TE, X, exprs[i], meta) != h2
            same_sql[i] = _h(TE.transpile(sql, write="duckdb")[0]) == h3
        except Exception:
            need_real = True
        if need_real or not same_sql.get(i):
            if not box.get("probed"):
                # the first query that needs it is run at once: a change that breaks the stage
                # usually breaks this one, and then the rest of the comparing is saved
                box["probed"] = True
                try:
                    real(i)
                except Exception:
                    return "fail", None
            else:
                todo.append(i)
    work = sum(cost.get(i, 0.0) + (0.0 if same_sql.get(i) and res.get(i) is not None else duck.get(i, 0.0)) for i in todo)
    STATS["todo"] = len(todo)
    STATS["work"] = work
    if allow is not None and work > allow:
        return "giveup", None
    for i in todo:
        try:
            real(i)
        except Exception:
            return "fail", None
    return "pass", None


def check_tpch(fp):
    H, TE, X, Table = _mods()
    pairs = _pairs(H, FX_H)
    if len(pairs) != len(fp["h"]):
        return "real", None
    real = set()
    for i, (_, sql, optimized) in enumerate(pairs):
        try:
            if (_h(TE.transpile(sql, write="duckdb")[0]), _h(TE.transpile(optimized, write="duckdb")[0])) != fp["h"][i]:
                real.add(i)
        except Exception:
            real.add(i)
    return ("real", real) if real else ("pass", None)


MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"


def check_max_nodes(width=200):
    """test_max_nodes with 200 columns instead of 10000 and its limits scaled alike.

    The test parses `SELECT x=x=...=x, ...` (100 `=` a column) and expects the node limit to
    trip: at exactly one node past a limit of columns*100, and at a limit twenty times smaller.
    Every column is parsed by the same code in the same state, so the scaled copy walks the same
    paths and sits on the same boundaries. (An approximation all the same: it was adopted after
    agreeing with the real test on every one of several hundred mutants of the code it runs.)"""
    import tests.test_parser as TP
    parse_one, exp, ParseError = TP.parse_one, TP.exp, TP.ParseError
    try:
        if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
            return "fail", None
        cols = [f"col{i}" for i in range(500)]
        if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select):
            return "fail", None
        depth = 100
        expr = "x" + "=x" * depth
        sql = f"SELECT {','.join([expr for _ in range(width)])}"
        a, b = width * depth, width * depth // 20
        try:
            parse_one(sql, max_nodes=a)
            return "fail", None
        except ParseError as e:
            if "Maximum number of AST nodes" not in str(e):
                return "fail", None
        try:
            parse_one(sql, max_nodes=b)
            return "fail", None
        except ParseError as e:
            if "(%d)" % b not in str(e):
                return "fail", None
        try:
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
            return "fail", None
        except ParseError:
            pass
        if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
            return "fail", None
    except Exception:
        return "fail", None
    return "pass", None


# ---------------------------------------------------------------- build-time tracing / timing

def trace_tpcds_query(i, ph):
    """Run query i of test_execute_tpcds for real, switching phases: S class fixture, A parse and
    transpile (always re-run by the check), B optimize, C everything else."""
    H, TE, X, Table = _mods()
    cls = TE.TestExecutor
    ph.set("S")
    cls.setUpClass()
    try:
        case = cls("test_execute_tpcds")
        meta, sql, _ = cls.tpcds_sqls[i]
        ph.set("A")
        expr = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
        if meta.get("execute"):
            ph.set("B")
            orig = X.optimize

            def opt(*a, **k):
                r = orig(*a, **k)
                ph.set("C")
                return r

            X.optimize = opt
            try:
                table = pickle.loads(pickle.dumps(TE.mp_execute(expr, meta)))
            finally:
                X.optimize = orig
            orig_tr = TE.transpile

            def tr(*a, **k):
                ph.set("A")
                try:
                    return orig_tr(*a, **k)
                finally:
                    ph.set("C")

            TE.transpile = tr
            try:
                case.subtestHelper(i, table, tpch=False)
            finally:
                TE.transpile = orig_tr
        ph.set("S")
    finally:
        cls.tearDownClass()
    ph.set(None)


def trace_tpch_opt(ph):
    H, TE, X, Table = _mods()
    cls = TE.TestExecutor
    ph.set("S")
    cls.setUpClass()
    try:
        case = cls("test_optimized_tpch")
        orig_tr = TE.transpile

        def tr(*a, **k):
            ph.set("A")
            try:
                return orig_tr(*a, **k)
            finally:
                ph.set("C")

        TE.transpile = tr
        ph.set("C")
        try:
            case.test_optimized_tpch()
        finally:
            TE.transpile = orig_tr
        ph.set("S")
    finally:
        cls.tearDownClass()
    ph.set(None)
