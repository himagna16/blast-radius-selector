"""In-process test runner for sqlglot's unittest suite (no pytest).

run_tests(root, test_ids, ...) must be called in a process that has not imported
sqlglot/tests yet (normally a forked child). Returns {test_id: status}.
"""
import os
import pickle
import signal
import sys
import time
import unittest
from concurrent.futures import Future


BROKEN_POOLS = []     # one entry per pool that broke during this process
PHASE_HOOK = None     # build-time tracing: called with "U" while a result is unpickled, None after


class SyncPool:
    """Stands in for ProcessPoolExecutor(spawn): runs the work inline and keeps the pickle round
    trips. Like the real pool it BREAKS when a result cannot be unpickled in the parent (or the
    arguments in the worker): that call and every later submit fail with BrokenProcessPool. The
    optimizer tests share one pool per class, so a break fails every later test that uses it."""

    def __init__(self, *a, initializer=None, initargs=(), **kw):
        self._broken = False
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def _break(self):
        from concurrent.futures.process import BrokenProcessPool
        if not self._broken:
            self._broken = True
            BROKEN_POOLS.append(self)
        return BrokenProcessPool("A process in the process pool was terminated abruptly")

    def submit(self, fn, *args, **kwargs):
        if self._broken:
            raise self._break()
        fut = Future()
        try:
            blob = pickle.dumps((args, kwargs))
        except _Timeout:
            raise
        except Exception as e:          # the feeder thread reports it on this future only
            fut.set_exception(e)
            return fut
        try:
            args, kwargs = pickle.loads(blob)
        except _Timeout:
            raise
        except Exception:               # the worker dies reading its task
            fut.set_exception(self._break())
            return fut
        try:
            blob = pickle.dumps(fn(*args, **kwargs))
        except _Timeout:
            raise
        except BaseException as e:  # noqa
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            fut.set_exception(e)        # raised by the work, or while pickling its result: sent back
            return fut
        if PHASE_HOOK:
            PHASE_HOOK("U")
        try:
            fut.set_result(pickle.loads(blob))
        except _Timeout:
            raise
        except Exception:               # the parent cannot read the result
            fut.set_exception(self._break())
        finally:
            if PHASE_HOOK:
                PHASE_HOOK(None)
        return fut

    def shutdown(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Timeout(BaseException):
    pass


class _Abort(Exception):
    """Raised after the first failed subtest when only pass/fail matters."""


FAILFAST = False
FINGERPRINTS = "/app/tpc.pkl"


class _LazyFuture:
    def __init__(self, fn, args, kwargs):
        self._call = (fn, args, kwargs)
        self._done = False
        self._res = self._exc = None

    def result(self, timeout=None):
        if not self._done:
            fn, args, kwargs = self._call
            try:
                args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
                self._res = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
            except _Timeout:
                raise
            except Exception as e:
                self._exc = e
            self._done = True
        if self._exc is not None:
            raise self._exc
        return self._res


class LazyPool:
    """For the executor tests: work happens at .result(), so a failing query stops the test early."""

    def __init__(self, *a, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        return _LazyFuture(fn, args, kwargs)

    def shutdown(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# Slicing: run a fixture-driven test on a subset of its fixture entries.
_cur_slices = None      # None = everything; else {fixture filename: set(indices)}
_rec = None             # when a dict: {(filename): count} of entries seen by the current test
ATTR_SLICES = {         # tests whose fixtures are loaded once per class
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds_sqls", "optimizer/tpc-ds/tpc-ds.sql"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch_sqls", "optimizer/tpc-h/tpc-h.sql"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch_sqls", "optimizer/tpc-h/tpc-h.sql"),
}


def _wrap_loader(orig):
    def loader(filename):
        sel = None if _cur_slices is None else _cur_slices.get(filename, ())
        n = 0
        for i, item in enumerate(orig(filename)):
            n = i + 1
            if sel is None or i in sel:
                yield item
        if _rec is not None:
            _rec[filename] = max(_rec.get(filename, 0), n)
    loader._br = True
    loader.__wrapped__ = orig
    return loader


def _alarm(signum, frame):
    raise _Timeout()


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = {}  # test id -> reason
        self.class_errors = []
        self.started = []

    def _id(self, test):
        return getattr(test, "_br_id", None)

    def _mark(self, test, why):
        tc = getattr(test, "test_case", test)  # _SubTest -> parent
        i = self._id(tc)
        if i is None:
            self.class_errors.append((str(test), why))
        else:
            self.bad.setdefault(i, why)

    def startTest(self, test):
        super().startTest(test)
        self.started.append(self._id(test))

    def addError(self, test, err):
        self._mark(test, "error:" + err[0].__name__)

    def addFailure(self, test, err):
        self._mark(test, "fail")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, "subfail")
            if FAILFAST and not issubclass(err[0], _Abort):
                raise _Abort()

    def addUnexpectedSuccess(self, test):
        self._mark(test, "unexpected-success")


def split_id(tid):
    path, cls, meth = tid.split("::")
    return path[:-3].replace("/", "."), cls, meth


def import_test_module(modname):
    import importlib
    mod = importlib.import_module(modname)
    if hasattr(mod, "ProcessPoolExecutor") and mod.ProcessPoolExecutor not in (SyncPool, LazyPool):
        mod.ProcessPoolExecutor = LazyPool if modname == "tests.test_executor" else SyncPool
    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        f = getattr(mod, name, None)
        if f is not None and not getattr(f, "_br", False):
            setattr(mod, name, _wrap_loader(f))
    return mod


def run_tests(items, budget=None, tracer=None, report=None):
    """items: test ids, or (test_id, slice_spec_or_None, key) tuples (key names the run; default the id).
    Runs grouped by class so class fixtures are built once. budget: fn(key)->seconds or None.
    report(key, status, cpu_seconds) is called as each run finishes.
    Returns {key: 'pass' | 'fail:<why>'}."""
    global _cur_slices
    out = {}
    groups = {}
    signal.signal(signal.SIGALRM, _alarm)

    def tell(key, st, cpu=0.0):
        out[key] = st
        if report:
            report(key, st, cpu)

    staged = []
    for it in items:
        if isinstance(it, str):
            it = (it, None, it)
        tid, spec, key = it
        if spec and ("@tpcds" in spec or "@tpch" in spec or "@maxnodes" in spec):
            staged.append(it)
            continue
        m, c, _ = split_id(tid)
        groups.setdefault((m, c), []).append((tid, spec, key))
    for tid, spec, key in staged:
        # cheap staged check first; fall back to the real test on whatever it could not clear
        t0 = time.process_time()
        verdict, real = "real", None
        b = budget(key) if budget else 0
        try:
            import brtpc
            fx = None
            if b:
                signal.setitimer(signal.ITIMER_REAL, b)
            if "@maxnodes" in spec:
                import_test_module("tests.test_parser")
                verdict, real = brtpc.check_max_nodes()
            else:
                import_test_module("tests.test_executor")
                fp = pickle.load(open(FINGERPRINTS, "rb"))
            if "@maxnodes" in spec:
                pass
            elif "@tpcds" in spec:
                verdict, real = brtpc.check_tpcds(fp, *spec["@tpcds"], deadline=(time.time() + b) if b else None,
                                                  chain_path=FINGERPRINTS)
                fx = brtpc.FX_DS
            else:
                verdict, real = brtpc.check_tpch(fp)
                fx = brtpc.FX_H
        except _Timeout:
            verdict = "timeout"
        except Exception:
            verdict, real = "real", None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if verdict in ("pass", "fail", "timeout", "giveup"):
            tell(key, "pass" if verdict == "pass" else "fail:" + verdict, time.process_time() - t0)
        else:
            m, c, _ = split_id(tid)
            groups.setdefault((m, c), []).append((tid, None if real is None else {fx: real}, key))
    for (modname, clsname), runs in groups.items():
        try:
            mod = import_test_module(modname)
            cls = getattr(mod, clsname)
        except BaseException as e:  # import failure = every test in the file fails
            if isinstance(e, (KeyboardInterrupt, SystemExit, _Timeout)):
                raise
            for tid, spec, key in runs:
                tell(key, "fail:import:" + type(e).__name__)
            continue
        cases = []
        for tid, spec, key in runs:
            try:
                case = cls(tid.split("::")[2])
            except Exception:
                tell(key, "fail:nomethod")
                continue
            case._br_id = key
            case._br_tid = tid
            case._br_spec = spec
            cases.append(case)
        res = _Result()

        def make(case):
            real = case.run

            def run(result=None):
                global _cur_slices
                key, tid, spec = case._br_id, case._br_tid, case._br_spec
                if spec is not None:
                    if tid in ATTR_SLICES:
                        attr, fname = ATTR_SLICES[tid]
                        full = getattr(type(case), attr, None)
                        if full is not None:
                            setattr(case, attr, [full[i] for i in sorted(spec.get(fname, ())) if i < len(full)])
                    else:
                        _cur_slices = spec
                if tracer:
                    tracer.start(key)
                t0 = time.process_time()
                b = budget(key) if budget else 0
                if b:
                    signal.setitimer(signal.ITIMER_REAL, b)
                try:
                    r = real(result)
                except _Timeout:
                    res.bad[key] = "timeout"
                    r = None
                finally:
                    if b:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    if tracer:
                        tracer.stop(key)
                    _cur_slices = None
                tell(key, ("fail:" + res.bad[key]) if key in res.bad else "pass", time.process_time() - t0)
                return r
            return run

        for case in cases:
            case.run = make(case)
        suite = unittest.TestSuite(cases)
        if tracer:
            tracer.start_class(modname, clsname)
        try:
            suite.run(res)
        except _Timeout:
            pass
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            _cur_slices = None
            if tracer:
                tracer.stop_class(modname, clsname)
        for case in cases:
            key = case._br_id
            if res.class_errors and out.get(key, "pass") == "pass":
                # setUpClass / tearDownClass / module fixture error: every test of the class fails
                tell(key, "fail:class:" + res.class_errors[0][1])
            elif key in res.bad and out.get(key) == "pass":   # late failure (cleanup)
                tell(key, "fail:" + res.bad[key])
            elif key not in out:
                tell(key, "fail:notrun")
    return out
