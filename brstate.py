"""Fingerprint what importing sqlglot leaves behind: every module global and class attribute.

Comparing a patched tree's fingerprints with the pinned commit's shows exactly which module or
class attributes a module-level / class-level edit changed (including derived ones), or that it
changed nothing at all.  Function bodies are deliberately excluded (line coverage handles them);
a function contributes its identity, signature defaults and closure.
"""
import enum
import functools
import hashlib
import logging
import re
import sys
import types

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
SKIP_ATTRS = {"__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__", "__static_attributes__",
              "__doc__", "__annotations__", "__annotate__", "__annotate_func__", "__annotations_cache__",
              "__builtins__", "__cached__", "__file__", "__loader__", "__spec__", "__path__", "__package__",
              "__name__", "__orig_bases__", "__parameters__", "__abstractmethods__", "_abc_impl",
              "__slots__", "__match_args__", "__type_params__", "__conditional_annotations__"}


def keyrepr(k):
    """Stable text for a dict key / set member, or None when it has none."""
    t = type(k)
    # dict and set lookups go by value: an int-valued or str-valued enum member is the same key
    # as the plain int or str
    if isinstance(k, str):
        return "s:" + str.__str__(k)
    if isinstance(k, int):
        return "i:%d" % int(k)
    if isinstance(k, enum.Enum):
        return "e:%s.%s.%s" % (type(k).__module__, type(k).__qualname__, k.name)
    if isinstance(k, type):
        return "c:%s.%s" % (k.__module__, k.__qualname__)
    if t is tuple:
        parts = [keyrepr(x) for x in k]
        return None if any(p is None for p in parts) else "t:(" + ",".join(parts) + ")"
    if k is None:
        return "n:"
    return None


def entries(fpr, v):
    """{key repr: fingerprint} of a class-level dict / set, or None when a key has no stable repr."""
    out = {}
    if isinstance(v, dict):
        for k, x in dict.items(v):
            r = keyrepr(k)
            if r is None:
                return None
            out[r] = fpr.fp(x)
    elif isinstance(v, (set, frozenset)):
        for k in (set.__iter__(v) if isinstance(v, set) else frozenset.__iter__(v)):
            r = keyrepr(k)
            if r is None:
                return None
            out[r] = "1"
    else:
        return None
    return out


_PRIM = frozenset((type(None), bool, int, float, complex, str, bytes))
_FUNC_T = types.FunctionType
_BUILTIN_T = (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
              types.MethodWrapperType, types.GetSetDescriptorType, types.MemberDescriptorType)
_sha1 = hashlib.sha1


class Fingerprinter:
    """fp(o) -> stable text for o. The traversal (order, memo, cycle and depth cut-offs) is the
    one of the plain recursive version kept below as SlowFingerprinter; fp is a closure over local
    state and exact builtin container types skip the isinstance chain, which only saves time."""

    def __init__(self, with_code=False, sort_all=False):
        self.memo = memo = {}
        self.stack = stack = set()
        self.keep = keep = []
        self.with_code = with_code
        self.sort_all = sort_all    # ignore every dict's order (for attributes whose order is unstable)
        _fp = self._fp
        memo_get = memo.get
        stack_add, stack_discard = stack.add, stack.discard
        keep_append = keep.append
        prim = _PRIM

        def fp(o, depth=0):
            t = type(o)
            if t in prim:
                return repr(o)
            i = id(o)
            got = memo_get(i)
            if got is not None:
                return got
            if i in stack or depth > 40:
                return "<cycle>"
            stack_add(i)
            try:
                r = _fp(o, t, depth + 1)
            except Exception as e:      # be conservative: unknown objects compare by type
                r = "<%s:%s>" % (t.__name__, type(e).__name__)
            stack_discard(i)
            if len(r) > 60:
                r = _sha1(r.encode("utf-8", "surrogatepass")).hexdigest()[:20]
            memo[i] = r
            keep_append(o)              # ids stay unique while we hold the object
            return r
        self.fp = fp

    def code(self, c):
        consts = tuple(self.code(k) if isinstance(k, types.CodeType) else repr(k) for k in c.co_consts)
        return (c.co_code, consts, c.co_names, c.co_varnames, c.co_argcount, c.co_kwonlyargcount, c.co_flags)

    def _fp(self, o, t, d):
        f = self.fp
        # exact builtin types first: for these the isinstance chain below ends in the same branch.
        # fp of a None/bool/number/str/bytes is its repr and touches no state, so it is inlined.
        if t is tuple or t is list:
            prim = _PRIM
            parts = [repr(x) if type(x) in prim else f(x, d) for x in o]
            if self.sort_all:
                parts.sort()    # a sequence built from a set changes its order from run to run
            return t.__name__ + "(" + ",".join(parts) + ")"
        if t is dict:
            prim = _PRIM
            items = [(repr(k) if type(k) in prim else f(k, d)) + ":" + (repr(v) if type(v) in prim else f(v, d))
                     for k, v in o.items()]
            if self.sort_all or not all(type(k) is str for k in o):
                items.sort()
            return "{" + ",".join(items) + "}"
        if t is _FUNC_T:
            return self._func(o, d)
        if isinstance(o, type):
            return "C:%s.%s" % (o.__module__, o.__qualname__)
        if isinstance(o, types.ModuleType):
            return "M:" + o.__name__
        if isinstance(o, enum.Enum):
            return "E:%s.%s.%s" % (t.__module__, t.__qualname__, o.name)
        if t in (tuple, list):
            parts = [f(x, d) for x in o]
            if self.sort_all:
                parts.sort()
            return t.__name__ + "(" + ",".join(parts) + ")"
        if t in (set, frozenset):
            return t.__name__ + "{" + ",".join(sorted(f(x, d) for x in o)) + "}"
        if isinstance(o, dict):
            items = [f(k, d) + ":" + f(v, d) for k, v in o.items()]
            if self.sort_all or not all(type(k) is str for k in o):
                items.sort()    # built by iterating sets of classes: the order differs from run to run
            return "{" + ",".join(items) + "}"
        if isinstance(o, (types.FunctionType,)):
            return self._func(o, d)
        if isinstance(o, _BUILTIN_T):
            return "B:%s" % getattr(o, "__qualname__", repr(o))
        if isinstance(o, types.MethodType):
            return "BM:" + f(o.__func__, d) + "@" + f(o.__self__, d)
        if isinstance(o, (staticmethod, classmethod)):
            return t.__name__ + ":" + f(o.__func__, d)
        if isinstance(o, property):
            return "P:" + f(o.fget, d) + f(o.fset, d) + f(o.fdel, d)
        if isinstance(o, functools.partial):
            return "partial:" + f(o.func, d) + f(o.args, d) + f(o.keywords, d)
        if isinstance(o, logging.Logger):
            return "L:" + o.name
        if isinstance(o, re.Pattern):
            return "re:%r:%d" % (o.pattern, o.flags)
        if isinstance(o, (types.MappingProxyType,)):
            return "mp" + f(dict(o), d)
        mod = getattr(t, "__module__", "")
        if mod.startswith(("sqlglot", "tests")):
            parts = []
            dd = getattr(o, "__dict__", None)
            if isinstance(dd, dict):
                parts += ["%s=%s" % (k, f(v, d)) for k, v in dd.items()]
            for klass in t.__mro__:
                for s in getattr(klass, "__slots__", ()) or ():
                    if isinstance(s, str) and s not in ("__dict__", "__weakref__"):
                        try:
                            parts.append("%s=%s" % (s, f(getattr(o, s), d)))
                        except AttributeError:
                            pass
            if not parts:
                return "O:%s.%s:%s" % (mod, t.__qualname__, _ADDR.sub("", repr(o)))
            return "O:%s.%s(%s)" % (mod, t.__qualname__, ",".join(parts))
        return "R:%s:%s" % (t.__name__, _ADDR.sub("", repr(o)))

    def _func(self, o, d):
        f = self.fp
        cells = ()
        if o.__closure__:
            cells = tuple(self._cell(c, d) for c in o.__closure__)
        body = self.code(o.__code__) if (self.with_code or o.__name__ == "<lambda>") else ()
        return "F:%s.%s|%s|%s|%s|%s" % (o.__module__, o.__qualname__, f(o.__defaults__, d), f(o.__kwdefaults__, d), cells,
                                     hashlib.sha1(repr(body).encode()).hexdigest()[:12] if body else "")

    def _cell(self, c, d):
        try:
            return self.fp(c.cell_contents, d)
        except ValueError:
            return "<empty>"


class SlowFingerprinter:
    """The reference version (kept for checking that Fingerprinter gives the same text)."""
    def __init__(self, with_code=False, sort_all=False):
        self.memo = {}
        self.stack = set()
        self.with_code = with_code
        self.sort_all = sort_all    # ignore every dict's order (for attributes whose order is unstable)

    def code(self, c):
        consts = tuple(self.code(k) if isinstance(k, types.CodeType) else repr(k) for k in c.co_consts)
        return (c.co_code, consts, c.co_names, c.co_varnames, c.co_argcount, c.co_kwonlyargcount, c.co_flags)

    def fp(self, o, depth=0):
        t = type(o)
        if o is None or t in (bool, int, float, complex, str, bytes):
            return repr(o)
        i = id(o)
        got = self.memo.get(i)
        if got is not None:
            return got
        if i in self.stack or depth > 40:
            return "<cycle>"
        self.stack.add(i)
        try:
            r = self._fp(o, t, depth + 1)
        except Exception as e:      # be conservative: unknown objects compare by type
            r = "<%s:%s>" % (t.__name__, type(e).__name__)
        self.stack.discard(i)
        if len(r) > 60:
            r = hashlib.sha1(r.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        self.memo[i] = r
        self.keep = getattr(self, "keep", [])
        self.keep.append(o)         # ids stay unique while we hold the object
        return r

    def _fp(self, o, t, d):
        f = self.fp
        if isinstance(o, type):
            return "C:%s.%s" % (o.__module__, o.__qualname__)
        if isinstance(o, types.ModuleType):
            return "M:" + o.__name__
        if isinstance(o, enum.Enum):
            return "E:%s.%s.%s" % (t.__module__, t.__qualname__, o.name)
        if t in (tuple, list):
            parts = [f(x, d) for x in o]
            if self.sort_all:
                parts.sort()
            return t.__name__ + "(" + ",".join(parts) + ")"
        if t in (set, frozenset):
            return t.__name__ + "{" + ",".join(sorted(f(x, d) for x in o)) + "}"
        if isinstance(o, dict):
            items = [f(k, d) + ":" + f(v, d) for k, v in o.items()]
            if self.sort_all or not all(type(k) is str for k in o):
                items.sort()    # built by iterating sets of classes: the order differs from run to run
            return "{" + ",".join(items) + "}"
        if isinstance(o, (types.FunctionType,)):
            cells = ()
            if o.__closure__:
                cells = tuple(self._cell(c, d) for c in o.__closure__)
            body = self.code(o.__code__) if (self.with_code or o.__name__ == "<lambda>") else ()
            return "F:%s.%s|%s|%s|%s|%s" % (o.__module__, o.__qualname__, f(o.__defaults__, d), f(o.__kwdefaults__, d), cells,
                                         hashlib.sha1(repr(body).encode()).hexdigest()[:12] if body else "")
        if isinstance(o, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                          types.MethodWrapperType, types.GetSetDescriptorType, types.MemberDescriptorType)):
            return "B:%s" % getattr(o, "__qualname__", repr(o))
        if isinstance(o, types.MethodType):
            return "BM:" + f(o.__func__, d) + "@" + f(o.__self__, d)
        if isinstance(o, (staticmethod, classmethod)):
            return t.__name__ + ":" + f(o.__func__, d)
        if isinstance(o, property):
            return "P:" + f(o.fget, d) + f(o.fset, d) + f(o.fdel, d)
        if isinstance(o, functools.partial):
            return "partial:" + f(o.func, d) + f(o.args, d) + f(o.keywords, d)
        if isinstance(o, logging.Logger):
            return "L:" + o.name
        if isinstance(o, re.Pattern):
            return "re:%r:%d" % (o.pattern, o.flags)
        if isinstance(o, (types.MappingProxyType,)):
            return "mp" + f(dict(o), d)
        mod = getattr(t, "__module__", "")
        if mod.startswith(("sqlglot", "tests")):
            parts = []
            dd = getattr(o, "__dict__", None)
            if isinstance(dd, dict):
                parts += ["%s=%s" % (k, f(v, d)) for k, v in dd.items()]
            for klass in t.__mro__:
                for s in getattr(klass, "__slots__", ()) or ():
                    if isinstance(s, str) and s not in ("__dict__", "__weakref__"):
                        try:
                            parts.append("%s=%s" % (s, f(getattr(o, s), d)))
                        except AttributeError:
                            pass
            if not parts:
                return "O:%s.%s:%s" % (mod, t.__qualname__, _ADDR.sub("", repr(o)))
            return "O:%s.%s(%s)" % (mod, t.__qualname__, ",".join(parts))
        return "R:%s:%s" % (t.__name__, _ADDR.sub("", repr(o)))

    def _cell(self, c, d):
        try:
            return self.fp(c.cell_contents, d)
        except ValueError:
            return "<empty>"


def snapshot(prefix="sqlglot", with_code=False, sort_all=False, only=None, modules=None, items=None):
    """{(module, class qualname or '', attribute): fingerprint} for every loaded module under prefix.
    With `items` (a dict), also fills it with the per-key fingerprints of class-level dicts and sets."""
    fpr = Fingerprinter(with_code, sort_all)
    out = {}
    if only is not None:
        for name, q, k in only:
            try:
                o = sys.modules[name]
                for part in (q.split(".") if q else ()):
                    o = vars(o)[part]
                out[(name, q, k)] = fpr.fp(o.__bases__ if k == "__bases__" else vars(o)[k])
            except Exception:
                pass
        return out
    for name, mod in sorted(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        if modules is not None and name not in modules:
            continue
        classes = []
        for k, v in list(vars(mod).items()):
            if k in SKIP_ATTRS:
                continue
            out[(name, "", k)] = fpr.fp(v)
            if items is not None and isinstance(v, (dict, set, frozenset)):
                e = entries(fpr, v)
                if e is not None:
                    items[(name, "", k)] = e
            if isinstance(v, type) and v.__module__ == name:
                classes.append(v)
        seen = set()
        while classes:
            c = classes.pop()
            if c in seen:
                continue
            seen.add(c)
            q = c.__qualname__
            out[(name, q, "__bases__")] = fpr.fp(c.__bases__)
            for k, v in list(vars(c).items()):
                if k in SKIP_ATTRS:
                    continue
                out[(name, q, k)] = fpr.fp(v)
                if items is not None and isinstance(v, (dict, set, frozenset)):
                    e = entries(fpr, v)
                    if e is not None:
                        items[(name, q, k)] = e
                if isinstance(v, type) and v.__module__ == name and v.__qualname__.startswith(q + "."):
                    classes.append(v)
    return out


def preload():
    """What a test process has loaded before sqlglot (sqlglot looks at sys.modules for unittest)."""
    import unittest, unittest.mock, decimal, datetime, typing, enum, functools, collections, dataclasses  # noqa
    import concurrent.futures, multiprocessing, importlib, math, numbers, copy, itertools, textwrap, threading  # noqa
    try:
        import numpy, pandas, duckdb  # noqa
        from pandas.testing import assert_frame_equal  # noqa
    except Exception:
        return
    # the test process is pytest's: code that asks whether pytest is loaded must get the same answer.
    # A stand-in keeps the real import (and its cost) for whoever actually uses the module.
    if "pytest" not in sys.modules:
        import types

        class _Lazy(types.ModuleType):
            def __getattr__(self, name):
                if name.startswith("__"):
                    raise AttributeError(name)
                import importlib
                if sys.modules.get("pytest") is self:
                    del sys.modules["pytest"]
                return getattr(importlib.import_module("pytest"), name)

        sys.modules["pytest"] = _Lazy("pytest")
    # DuckDB starts one thread per visible core for every connection; on a big host that alone can
    # exhaust a small process limit. Two threads give the same results on this data.
    if not getattr(duckdb.connect, "_br", False):
        real_connect = duckdb.connect

        def connect(*a, **k):
            if "config" not in k and len(a) < 3:
                k["config"] = {"threads": 2}
            return real_connect(*a, **k)

        connect._br = True
        duckdb.connect = connect


def eager_footprint():
    """What a bare import of sqlglot has loaded: its modules and the dialects registered so far."""
    import sqlglot
    mods = sorted(m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot."))
    try:
        reg = sorted(str(k) for k in sqlglot.Dialect._classes)
    except Exception:
        reg = None
    return mods, reg


def load_all():
    """Import every sqlglot module (dialects load lazily otherwise)."""
    import importlib
    import os
    import sqlglot
    root = os.path.dirname(sqlglot.__file__)
    failed = []
    for dp, dn, fn in os.walk(root, followlinks=True):   # scratch trees are mostly symlinks
        dn[:] = sorted(d for d in dn if d != "__pycache__")   # same import order on every filesystem
        for f in sorted(fn):
            if f.endswith(".py") and f != "__main__.py":
                rel = os.path.relpath(os.path.join(dp, f), os.path.dirname(root))[:-3].replace(os.sep, ".")
                if rel.endswith(".__init__"):
                    rel = rel[:-9]
                try:
                    importlib.import_module(rel)
                except BaseException as e:
                    failed.append((rel, type(e).__name__))
    return failed


def build(runs=8):
    """Pinned-commit state. Several fresh processes: attributes whose fingerprint varies from run to
    run are compared order-insensitively, and ignored when even that is unstable."""
    import pickle
    import subprocess
    # like the selector's children: test libraries first, and a different heap layout every run
    code = ("import sys, pickle, random, brstate; random.seed(%d); brstate.preload(); "
            "junk = [bytearray(random.randrange(1, 5000)) for _ in range(random.randrange(10, 20000))]; "
            "junk2 = [object() for _ in range(random.randrange(10, 50000))]; del junk[::2]; "
            "brstate.load_all(); "
            "items = {}; a = brstate.snapshot(items=items); "
            "sys.stdout.buffer.write(pickle.dumps((a, brstate.snapshot(sort_all=True), items)))")
    snaps = [pickle.loads(subprocess.run([sys.executable, "-c", code % i], capture_output=True, check=True).stdout)
             for i in range(runs)]
    first, first_sorted, items = snaps[0]
    unstable = {k for o, _, _ in snaps for k in set(o) | set(first) if o.get(k) != first.get(k)}
    volatile = {k for _, so, _ in snaps for k in unstable if so.get(k) != first_sorted.get(k)}
    # per-key fingerprints, kept only where every run agrees
    items = {k: v for k, v in items.items() if all(it.get(k) == v for _, _, it in snaps)}
    return {"fp": first, "sorted": {k: first_sorted.get(k) for k in unstable - volatile}, "volatile": volatile,
            "items": items}


def diff(pinned, modules=None):
    """Import everything (or just `modules`) from the tree on sys.path, compare with the pinned state.
    -> (modules that failed to import, changed (module, class, attribute) keys)."""
    if modules is None:
        failed = load_all()
        now = snapshot()
        keys = set(pinned["fp"]) | set(now)
    else:
        import importlib
        failed = []
        for name in modules:
            try:
                importlib.import_module(name)
            except BaseException as e:
                failed.append((name, type(e).__name__))
        mods = set(modules)
        now = snapshot(modules=mods)
        keys = {k for k in pinned["fp"] if k[0] in mods} | set(now)
    fp, srt, vol = pinned["fp"], pinned["sorted"], pinned["volatile"]
    changed = [k for k in keys if fp.get(k) != now.get(k) and k not in vol]
    recheck = [k for k in changed if k in srt]
    if recheck:
        again = snapshot(sort_all=True, only=recheck)
        same = {k for k in recheck if again.get(k) == srt[k]}
        changed = [k for k in changed if k not in same]
    # which keys of a changed dict / set differ (None: unknown, treat the whole container as changed)
    detail = {}
    pitems = pinned.get("items", {})
    if changed and pitems:
        fpr = Fingerprinter()
        for name, q, k in changed:
            was = pitems.get((name, q, k))
            if was is None:
                continue
            try:
                o = sys.modules[name]
                for part in (q.split(".") if q else ()):
                    o = vars(o)[part]
                cur = entries(fpr, vars(o)[k])
            except Exception:
                cur = None
            if cur is not None:
                detail[(name, q, k)] = sorted(x for x in set(was) | set(cur) if was.get(x) != cur.get(x))
    return failed, sorted(changed), detail


if __name__ == "__main__":
    import json
    import pickle
    if sys.argv[1] == "build":
        st = build(int(sys.argv[3]) if len(sys.argv) > 3 else 8)
        pickle.dump(st, open(sys.argv[2], "wb"))
        import subprocess
        code = "import sys, pickle, brstate; brstate.preload(); sys.stdout.buffer.write(pickle.dumps(brstate.eager_footprint()))"
        eager = pickle.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, check=True).stdout)
        pickle.dump(eager, open(sys.argv[2] + ".eager", "wb"))
        print("state: %d attributes, %d order-unstable, %d volatile" % (len(st["fp"]), len(st["sorted"]), len(st["volatile"])), file=sys.stderr)
    else:
        preload()
        try:
            import sqlglot  # noqa: F401
        except BaseException:
            print(json.dumps({"import_failed": True}))
            sys.exit(0)
        failed, changed, detail = diff(pickle.load(open(sys.argv[2], "rb")))
        print(json.dumps({"failed": failed, "changed": changed, "detail": [[list(k), v] for k, v in detail.items()]}))
