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


def _hfast(e):
    """Fingerprint of an expression for the rule-by-rule comparison (same content as _hexpr)."""
    return _h(repr(e.dump()))


def _opt_args(TE, X, expr, meta):
    """The arguments mp_execute hands to optimize for a query -> (args, kwargs), or (None, None)."""
    box = {}
    orig = X.optimize

    def opt(*a, **k):
        box["a"], box["k"] = a, k
        raise _Short()

    X.optimize = opt
    try:
        TE.mp_execute(expr, meta)
    except _Short:
        pass
    finally:
        X.optimize = orig
    return box.get("a"), box.get("k")


def rule_chain(TE, X, pairs, exprs, ds):
    """Run on the pinned commit: optimize, one rule at a time.

    For every executed query the optimizer's rules are applied one by one, each from a pickled
    copy of the previous rule's output; the inputs and a fingerprint of every output are kept.
    A query is only kept when the last fingerprint equals the one the real, uninterrupted call
    produces, and a step only when it gives the same output again when replayed on its own.
    -> (meta for the fingerprint file, {rule index: {query: pickled input}})"""
    from sqlglot.optimizer import optimizer as O
    rules = list(O.RULES)
    order = [i for i in sorted(exprs) if pairs[i][0].get("execute")]
    if not order:
        return None, {}
    a0, kw = _opt_args(TE, X, exprs[order[0]], pairs[order[0]][0])
    if a0 is None:
        return None, {}
    fps, blobs, cost = {}, {k: {} for k in range(len(rules))}, [0.0] * len(rules)
    for i in order:
        try:
            cur = pickle.dumps(exprs[i])
            f, ins = [], []
            for k, rule in enumerate(rules):
                ins.append(cur)
                t0 = time.process_time()
                out = X.optimize(pickle.loads(cur), *a0[1:], rules=[rule], **kw)
                f.append(_hfast(out))
                cost[k] += time.process_time() - t0
                cur = pickle.dumps(out)
            last = _hexpr(out)
        except Exception:
            continue
        if last == ds[i][1]:
            fps[i] = f
            for k in range(len(rules)):
                blobs[k][i] = ins[k]
    bad = set()
    for k in reversed(range(len(rules))):       # each step again, on its own and in another order
        for i in reversed(sorted(fps)):
            try:
                out = X.optimize(pickle.loads(blobs[k][i]), *a0[1:], rules=[rules[k]], **kw)
                if _hfast(out) != fps[i][k]:
                    bad.add((k, i))
            except Exception:
                bad.add((k, i))
    n = max(1, len(fps))
    return {"names": [r.__name__ for r in rules], "fps": fps, "bad": bad, "cost": [c / n for c in cost]}, blobs


def fingerprints(results=(), chain_path=None):
    """Run on the pinned commit. `results`: result_hashes() of other processes; a query's result
    fingerprint is kept only when every process agrees on it."""
    H, TE, X, Table = _mods()
    TE.initializer(H.TPCDS_SCHEMA, _dummy_tables(H, Table))
    ds = {}
    pairs = _pairs(H, FX_DS)
    exprs = {}
    for i, (meta, sql, _) in enumerate(pairs):
        expr = exprs[i] = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        if meta.get("execute"):
            ds[i] = (_hexpr(expr), _stage_b(TE, X, expr, meta), _h(TE.transpile(sql, write="duckdb")[0]))
        else:
            ds[i] = (_hexpr(expr), None, None)
    rules = None
    if chain_path:
        try:
            rules, blobs = rule_chain(TE, X, pairs, exprs, ds)
            for k, blob in blobs.items():
                pickle.dump(blob, open("%s.r%d" % (chain_path, k), "wb"))
        except Exception:
            rules = None
    tpch = {}
    for i, (_, sql, optimized) in enumerate(_pairs(H, FX_H)):
        tpch[i] = (_h(TE.transpile(sql, write="duckdb")[0]), _h(TE.transpile(optimized, write="duckdb")[0]))
    res = {}
    if results:
        first = results[0]
        res = {i: h for i, h in first.items() if h is not None and all(r.get(i) == h for r in results)}
    return {"ds": ds, "h": tpch, "res": res, "rules": rules}


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
    fp = fingerprints(results, chain_path=path)
    try:
        fp["fast_tables"] = bool(tables_match(path + ".tables"))
    except Exception:
        fp["fast_tables"] = False
    pickle.dump(fp, open(path, "wb"))
    return fp


STATS = {"same": 0, "compared": 0, "todo": 0, "work": 0.0}


def check_tpcds(fp, need_b, need_c, cost=None, allow=None, duck=None, a_same=False, rule_hit=None, partial=None,
                deadline=None, chain_path=None):
    import signal
    try:
        return _check_tpcds(fp, set(need_b), need_c, cost, allow, duck, deadline, a_same, rule_hit, chain_path, partial)
    finally:
        if deadline is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _check_tpcds(fp, need_b, need_c, cost, allow, duck, deadline, a_same=False, rule_hit=None, chain_path=None,
                 partial=None):
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
    seconds at most each, so a change that makes one query loop costs seconds, not the budget.

    `a_same`: the change reaches nothing that parsing or transpiling the queries runs, so only
    the queries looked at below are parsed at all. `rule_hit` ({rule index: queries}): for these
    queries the change reaches the optimizer only inside those rules; each such rule is re-run
    from the pinned commit's input for it and its output compared (see rule_chain). A query whose
    rule output differs goes on to the ordinary whole-optimizer comparison.

    `partial` ((seconds, queries in order) or None): what to do when the work exceeds `allow`.
    Instead of giving up, the listed queries (the ones found most telling) are run first, up to
    that many seconds of executor time. If every table comes out exactly as on the pinned commit
    the test is taken to pass. This is a judgement, not a proof: the queries left out could
    still differ. Any table that differs at all ends it with 'giveup'."""
    import signal

    if partial and (not a_same or need_b or rule_hit):
        partial = None                   # earlier stages are reached: every query counts

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

    def parsed(i):
        if i not in exprs:
            exprs[i] = pickle.loads(pickle.dumps(TE.parse_one(pairs[i][1])))
        return exprs[i]

    if not a_same:
        for i in range(len(pairs)):
            try:
                parsed(i)
            except Exception:
                return "fail", None      # the test submits every query before it looks at any result
    box = {}
    executed = [i for i in range(len(pairs)) if pairs[i][0].get("execute")]
    res = fp.get("res") or {}
    cost = cost or {}
    duck = duck or {}
    same_sql = {}

    def real(i):
        cls = TE.TestExecutor
        if "case" not in box:
            arm(10.0)
            if fp.get("fast_tables"):
                _fast_setup(TE, H, Table, chain_path and chain_path + ".tables")    # only the tables the queries below use get read
            else:
                cls.setUpClass()
            box["case"] = cls("test_execute_tpcds")
        TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
        try:
            arm(max(2.0, 20.0 * cost.get(i, 0.0)))
            table = pickle.loads(pickle.dumps(TE.mp_execute(parsed(i), pairs[i][0])))
            if same_sql.get(i) and res.get(i) is not None and _htable(table) == res[i]:
                STATS["same"] += 1
                return                   # same table, same DuckDB query: compares as on the pinned commit
            STATS["compared"] += 1
            if fp.get("fast_tables") and cls.tpcds_conn is None:
                arm(10.0)
                _fast_conn(TE, H)            # DuckDB is only needed from the first table that differs
            arm(max(3.0, 20.0 * duck.get(i, 0.0)))
            box["case"].subtestHelper(i, table, tpch=False)
        finally:
            TE.initializer(H.TPCDS_SCHEMA, dummy)

    order = sorted(executed, key=lambda i: (cost.get(i, 0.0), i))
    if partial:
        rank = {q: n for n, q in enumerate(partial[1])}
        order.sort(key=lambda i: (rank.get(i, len(rank)), cost.get(i, 0.0), i))
    rules = _RuleSteps(fp, chain_path, rule_hit, TE, X, pairs, parsed, order, arm) if rule_hit else None
    todo = []
    for i in order:
        by_rule = rules is not None and rules.has(i)
        if a_same and i not in need_b and i not in need_c and not by_rule:
            continue                     # nothing the change reaches runs for this query before stage C
        meta, sql, _ = pairs[i]
        h1, h2, h3 = fp["ds"][i][:3]
        need_real = i in need_c
        arm(2.0)
        try:
            if not need_real:
                # the pinned inputs of the rules are only this query's inputs if it still parses the same
                whole = i in need_b or not (a_same or _hexpr(parsed(i)) == h1)
                if not whole and by_rule:
                    whole = not rules.same(i)
                if whole:
                    need_real = _stage_b(TE, X, parsed(i), meta) != h2
            same_sql[i] = True if a_same else _h(TE.transpile(sql, write="duckdb")[0]) == h3
        except Exception:
            need_real = True
        if need_real or not same_sql.get(i):
            if i not in need_c or not same_sql.get(i):
                box["upstream"] = True   # an earlier stage gives another result for this query
            if not box.get("probed"):
                # the first query that needs it is run at once: a change that breaks the stage
                # usually breaks this one, and then the rest of the comparing is saved
                box["probed"] = True
                box["probed_i"] = i
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
        # only for what the judgement was measured on: execution-stage code reached by the
        # change, with every query still parsing, optimizing and transpiling as before
        if not partial or STATS["compared"] or box.get("upstream"):
            return "giveup", None
        spent = cost.get(box.get("probed_i"), 0.0)
        for i in todo:
            if spent + cost.get(i, 0.0) > partial[0]:
                break
            spent += cost.get(i, 0.0)
            before = STATS["same"]
            try:
                real(i)
            except Exception:
                return "fail", None
            if STATS["same"] == before:
                return "giveup", None    # a table differs (the comparison passed): not the same behaviour
        STATS["partial"] = spent
        return "pass", None
    for i in todo:
        try:
            real(i)
        except Exception:
            return "fail", None
    return "pass", None


class _LazyTables(dict):
    """TPC-DS tables read on first use, the way the test class's setUpClass reads them all.

    With `rows` (see tables_match) a table's rows come from the copy kept at build time, which
    was checked equal, cell for cell and type for type, to what setUpClass reads; it is only used
    while the fixture file is the one it was taken from."""

    def __init__(self, TE, H, Table, rows=None):
        super().__init__()
        self.TE, self.H, self.Table, self.rows = TE, H, Table, rows or {}

    def __missing__(self, name):
        kept = self.rows.get(name)
        if kept is not None and _stat(self.TE, name) == kept[0]:
            t = self.Table(columns=self.H.TPCDS_SCHEMA[name], rows=pickle.loads(kept[1]))
        else:
            t = _load_table(self.TE, self.Table, name, self.H.TPCDS_SCHEMA[name])
        self[name] = t
        return t


def _stat(TE, table):
    import os
    st = os.stat("%s%s.csv.gz" % (TE.DIR_TPCDS, table))
    return st.st_size, st.st_mtime_ns


def _load_table(TE, Table, table, columns):
    """One table of the TPC-DS fixture, read exactly as TestExecutor.setUpClass reads it."""
    import ast
    import csv
    reader = csv.reader(TE.open_file("%s%s.csv.gz" % (TE.DIR_TPCDS, table)), delimiter="|")
    rows = []
    ctypes = []
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return Table(columns=columns, rows=rows)


def _fast_setup(TE, H, Table, rows_path=None):
    """What setUpClass provides for the TPC-DS half of the class, with the tables read on demand
    and the DuckDB connection made by _fast_conn when a comparison first needs it."""
    cls = TE.TestExecutor
    cls.tpcds_conn = None
    cls.cache = {}
    cls.tpcds_sqls = list(_pairs(H, FX_DS))
    rows = None
    if rows_path:
        try:
            rows = pickle.load(open(rows_path, "rb"))
        except Exception:
            rows = None
    cls.tpcds_tables = _LazyTables(TE, H, Table, rows)


def _fast_conn(TE, H):
    """The DuckDB connection setUpClass makes for the TPC-DS half of the class."""
    import duckdb
    cls = TE.TestExecutor
    conn = duckdb.connect()
    for table, columns in H.TPCDS_SCHEMA.items():
        conn.execute(
            f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{TE.DIR_TPCDS}{table}.csv.gz', delim='|', header=True, columns={columns})
                """
        )
    cls.tpcds_conn = conn


def _same_rows(ra_list, rb_list):
    if len(ra_list) != len(rb_list):
        return False
    for ra, rb in zip(ra_list, rb_list):
        if type(ra) is not type(rb) or len(ra) != len(rb) or any(type(x) is not type(y) or x != y for x, y in zip(ra, rb)):
            return False
    return True


def tables_match(rows_path=None):
    """Run on the pinned commit: does _load_table give exactly the tables setUpClass builds?
    With `rows_path`, also keep a pickled copy of every table's rows there, checked to load back
    equal (cell for cell, type for type) to what setUpClass reads."""
    H, TE, X, Table = _mods()
    cls = TE.TestExecutor
    cls.setUpClass()
    kept = {}
    try:
        for name, columns in H.TPCDS_SCHEMA.items():
            a, b = cls.tpcds_tables[name], _load_table(TE, Table, name, columns)
            if tuple(a.columns) != tuple(b.columns) or not _same_rows(a.rows, b.rows):
                return False
            blob = pickle.dumps(list(a.rows), protocol=pickle.HIGHEST_PROTOCOL)
            c = Table(columns=columns, rows=pickle.loads(blob))
            if tuple(c.columns) == tuple(a.columns) and _same_rows(a.rows, c.rows) and type(c.rows) is type(a.rows):
                kept[name] = (_stat(TE, name), blob)
        ok = list(cls.tpcds_sqls) == list(_pairs(H, FX_DS))
        if ok and rows_path:
            pickle.dump(kept, open(rows_path, "wb"))
        return ok
    finally:
        cls.tearDownClass()


class _RuleSteps:
    """Re-runs single optimizer rules from their pinned inputs (see rule_chain)."""

    def __init__(self, fp, chain_path, rule_hit, TE, X, pairs, parsed, order, arm):
        self.R = fp.get("rules")
        self.path, self.TE, self.X, self.pairs, self.parsed, self.order, self.arm = chain_path, TE, X, pairs, parsed, order, arm
        self.hit = {}
        for k, qs in rule_hit.items():
            for i in qs:
                self.hit.setdefault(i, []).append(k)
        self.ready = None
        self.blobs = {}

    def has(self, i):
        return i in self.hit

    def _prepare(self):
        self.ready = False
        if not self.R or not self.path or not self.order:
            return
        from sqlglot.optimizer import optimizer as O
        self.rules = list(O.RULES)
        if [r.__name__ for r in self.rules] != self.R["names"]:
            return
        i0 = self.order[0]
        self.a0, self.kw = _opt_args(self.TE, self.X, self.parsed(i0), self.pairs[i0][0])
        self.ready = self.a0 is not None

    def same(self, i):
        """True when every rule the change reaches for query i still gives the pinned output."""
        try:
            if self.ready is None:
                self._prepare()
            if not self.ready:
                return False
            R = self.R
            for k in sorted(self.hit[i]):
                if not 0 <= k < len(self.rules) or (k, i) in R["bad"] or i not in R["fps"]:
                    return False
                if k not in self.blobs:
                    self.blobs[k] = pickle.load(open("%s.r%d" % (self.path, k), "rb"))
                if i not in self.blobs[k]:
                    return False
                self.arm(2.0)
                out = self.X.optimize(pickle.loads(self.blobs[k][i]), *self.a0[1:], rules=[self.rules[k]], **self.kw)
                STATS["steps"] = STATS.get("steps", 0) + 1
                if _hfast(out) != R["fps"][i][k]:
                    return False
            return True
        except Exception:
            return False


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
            # B is the optimizer call alone. What mp_execute and execute do before it (looking up
            # the tables, building the schema) feeds the execution stage directly, so it is C.
            ph.set("C")
            orig = X.optimize

            def opt(*a, **k):
                ph.set("B")
                try:
                    return orig(*a, **k)
                finally:
                    ph.set("C")

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
