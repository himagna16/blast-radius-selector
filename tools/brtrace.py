"""Build-time tracer (sys.monitoring + audit hook) for ONE isolated test run per process.

Records, for the run: lines executed at run time (rl), lines executed while a module was being
imported or in module/class bodies (il), classes instantiated or handed out by the dialect
registry, and files opened / directories listed under the repo root.
"""
import os
import sys

mon = sys.monitoring
TOOL = 4
E = mon.events


class Tracer:
    def __init__(self, root, lines=True):
        self.root = os.path.realpath(root) + os.sep
        self.fcache = {}
        self.rl = set()      # (relpath, line)
        self.il = set()
        self.classes = set()
        self.mro = {}
        self.files = set()
        self.depth = 0
        self._hooked = False
        self.phases = {}
        self.lines = lines
        if lines:
            mon.use_tool_id(TOOL, "br")
            mon.register_callback(TOOL, E.LINE, self._line)
            mon.register_callback(TOOL, E.PY_START, self._start)
            mon.register_callback(TOOL, E.PY_RETURN, self._ret)
            mon.register_callback(TOOL, E.PY_UNWIND, self._unwind)
            mon.set_events(TOOL, E.LINE | E.PY_START | E.PY_RETURN | E.PY_UNWIND)
        sys.addaudithook(self._audit)

    def _rel(self, fn):
        try:
            return self.fcache[fn]
        except KeyError:
            f = None
            if fn.startswith(self.root) or not fn.startswith(("/", "<")):
                rp = os.path.realpath(fn)
                if rp.startswith(self.root) and rp.endswith(".py"):
                    f = rp[len(self.root):]
            self.fcache[fn] = f
            return f

    def _line(self, code, line):
        f = self._rel(code.co_filename)
        if f is None:
            return mon.DISABLE
        if self.depth or not (code.co_flags & 1):
            self.il.add((f, line))
            return None
        self.rl.add((f, line))
        return mon.DISABLE

    def _start(self, code, off):
        name = code.co_name
        if name == "<module>":
            if self._rel(code.co_filename) is not None:
                self.depth += 1
            return None
        if name == "__init__":
            if self._rel(code.co_filename) is None:
                return mon.DISABLE
            try:
                obj = sys._getframe(1).f_locals.get("self")
            except Exception:
                obj = None
            if obj is not None:
                self.use_class(type(obj))
            return None
        return mon.DISABLE

    def _ret(self, code, off, val):
        if code.co_name == "<module>":
            if self._rel(code.co_filename) is not None:
                self.depth -= 1
            return None
        return mon.DISABLE

    def _unwind(self, code, off, exc):
        if code.co_name == "<module>" and self._rel(code.co_filename) is not None:
            self.depth -= 1

    def use_class(self, c):
        if not isinstance(c, type):
            return
        k = c.__module__ + ":" + c.__qualname__
        if k not in self.classes:
            self.classes.add(k)
            self.mro[k] = [b.__module__ + ":" + b.__qualname__ for b in c.__mro__[1:-1]]
            for attr in ("parser_class", "generator_class", "tokenizer_class", "jsonpath_tokenizer_class"):
                comp = c.__dict__.get(attr) if attr in getattr(c, "__dict__", {}) else getattr(c, attr, None)
                if isinstance(comp, type):
                    self.use_class(comp)

    def _audit(self, ev, args):
        if ev == "open" or ev == "os.listdir" or ev == "os.scandir":
            p = args[0]
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
            if not isinstance(p, str):
                return
            if ev == "open" and len(args) > 1 and isinstance(args[1], str) and ("w" in args[1] or "a" in args[1] or "x" in args[1]):
                return
            if ev != "open":
                # the import system lists every directory it searches; only listings made by the
                # tests or the library themselves say "this code looks at what files exist"
                try:
                    if sys._getframe(1).f_code.co_filename.startswith("<frozen importlib"):
                        return
                except Exception:
                    pass
            rp = os.path.realpath(p)
            if rp.startswith(self.root) or rp + os.sep == self.root:
                rel = rp[len(self.root):]
                if "__pycache__" in rel or rel.endswith(".pyc"):
                    return
                self.files.add(("L:" if ev != "open" else "") + rel)

    def hook_dialects(self):
        if self._hooked:
            return
        self._hooked = True
        try:
            from sqlglot.dialects import dialect as d
        except Exception:
            return
        meta = type(d.Dialect)
        tr = self
        for name in ("__getitem__", "get"):
            orig = meta.__dict__.get(name)
            if orig is None:
                continue
            f = getattr(orig, "__func__", orig)

            def wrap(cls, *a, __o=f, **k):
                r = __o(cls, *a, **k)
                tr.use_class(r)
                return r
            setattr(meta, name, classmethod(wrap) if isinstance(orig, classmethod) else wrap)

    def set_phase(self, name):
        """Route lines to a named bucket from now on (None: the default bucket)."""
        if not self.lines:
            return
        if name is None:
            name = "_"
        self.rl, self.il = self.phases.setdefault(name, (set(), set()))
        mon.restart_events()

    # runner hooks
    def start_class(self, mod, cls):
        self.hook_dialects()

    def stop_class(self, mod, cls):
        pass

    def start(self, key):
        self.hook_dialects()

    def stop(self, key):
        pass

    def record(self):
        if self.lines:
            mon.set_events(TOOL, 0)
        rec = {"rl": self.rl, "il": self.il, "classes": self.classes, "mro": self.mro, "files": self.files}
        if self.phases:
            rec["phases"] = {k: {"rl": v[0], "il": v[1]} for k, v in self.phases.items()}
        return rec
