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


class Fingerprinter:
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
            return t.__name__ + "(" + ",".join(f(x, d) for x in o) + ")"
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


def snapshot(prefix="sqlglot", with_code=False, sort_all=False, only=None, modules=None):
    """{(module, class qualname or '', attribute): fingerprint} for every loaded module under prefix."""
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
            "sys.stdout.buffer.write(pickle.dumps((brstate.snapshot(), brstate.snapshot(sort_all=True))))")
    snaps = [pickle.loads(subprocess.run([sys.executable, "-c", code % i], capture_output=True, check=True).stdout)
             for i in range(runs)]
    first, first_sorted = snaps[0]
    unstable = {k for o, _ in snaps for k in set(o) | set(first) if o.get(k) != first.get(k)}
    volatile = {k for _, so in snaps for k in unstable if so.get(k) != first_sorted.get(k)}
    return {"fp": first, "sorted": {k: first_sorted.get(k) for k in unstable - volatile}, "volatile": volatile}


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
    return failed, sorted(changed)


if __name__ == "__main__":
    import json
    import pickle
    if sys.argv[1] == "build":
        st = build(int(sys.argv[3]) if len(sys.argv) > 3 else 8)
        pickle.dump(st, open(sys.argv[2], "wb"))
        print("state: %d attributes, %d order-unstable, %d volatile" % (len(st["fp"]), len(st["sorted"]), len(st["volatile"])), file=sys.stderr)
    else:
        preload()
        try:
            import sqlglot  # noqa: F401
        except BaseException:
            print(json.dumps({"import_failed": True}))
            sys.exit(0)
        failed, changed = diff(pickle.load(open(sys.argv[2], "rb")))
        print(json.dumps({"failed": failed, "changed": changed}))
