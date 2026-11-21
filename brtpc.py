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


def fingerprints():
    """Run on the pinned commit."""
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
    return {"ds": ds, "h": tpch}


def check_tpcds(fp, need_b, need_c, cost=None):
    """-> ('pass' | 'fail', None), or ('real', None) when the fixture no longer matches the map.

    Queries whose stage C must be re-run are run for real right here, cheapest first, against the
    real tables and DuckDB, exactly as the test's own helper does; the first failure ends it."""
    H, TE, X, Table = _mods()
    pairs = _pairs(H, FX_DS)
    if len(pairs) != len(fp["ds"]):
        return "real", None
    dummy = _dummy_tables(H, Table)
    TE.initializer(H.TPCDS_SCHEMA, dummy)
    exprs = {}
    for i, (meta, sql, _) in enumerate(pairs):
        try:
            exprs[i] = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        except Exception:
            return "fail", None          # the test submits every query before it looks at any result
    box = {}

    def real(i):
        cls = TE.TestExecutor
        if "case" not in box:
            cls.setUpClass()
            box["case"] = cls("test_execute_tpcds")
        TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
        try:
            table = pickle.loads(pickle.dumps(TE.mp_execute(exprs[i], pairs[i][0])))
            box["case"].subtestHelper(i, table, tpch=False)
        finally:
            TE.initializer(H.TPCDS_SCHEMA, dummy)

    cost = cost or {}
    order = sorted((i for i in exprs if pairs[i][0].get("execute")), key=lambda i: (cost.get(i, 0.0), i))
    for i in order:
        meta, sql, _ = pairs[i]
        need_real = i in need_c
        if not need_real:
            h1, h2, h3 = fp["ds"][i]
            try:
                if i in need_b or _hexpr(exprs[i]) != h1:
                    need_real = _stage_b(TE, X, exprs[i], meta) != h2
                if not need_real:
                    need_real = _h(TE.transpile(sql, write="duckdb")[0]) != h3
            except Exception:
                need_real = True
        if need_real:
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
