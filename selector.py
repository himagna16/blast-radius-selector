#!/usr/bin/env python
"""Blast Radius selector: pick the tests a patch can break.

Static step.  Every changed position is located in the syntax tree.  A map traced from the
pinned commit (each test run alone in a fresh process) says which tests execute a line, at run
time or while importing, which classes a test instantiates and which files it opens.  That
gives the candidates: the only tests the change can reach.

Dynamic step.  When checking is cheaper than selecting blindly, the patch is applied to a
scratch tree and the candidates are run in a forked child; only the ones that really fail are
selected.  Fixture-driven tests are run on just the fixture entries the change reaches.
"""
import json
import os
import pickle
import re
import select
import shutil
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import applypatch  # noqa: E402
import brplan  # noqa: E402

REPO = os.environ.get("BR_REPO", "/app/repo")
DATA = os.environ.get("BR_DATA", "/data")
INDEX = os.environ.get("BR_INDEX", os.path.join(HERE, "index.pkl"))
SCRATCH = os.environ.get("BR_SCRATCH", "/tmp/br")
SPEED = float(os.environ.get("BR_SPEED", "2.5"))     # scorer CPU seconds per traced-machine second
FORK_COST = float(os.environ.get("BR_FORK", "0.15"))  # fixed CPU cost of one verification child
P_ANY = float(os.environ.get("BR_PANY", "0.5"))       # prior that a code change breaks something
PAR = int(os.environ.get("BR_PAR", "2"))
NO_RUN = os.environ.get("BR_NORUN") == "1"
WORKER_MS = 2000
GENERIC_CLASS_NAMES = {"Parser", "Generator", "Tokenizer", "Dialect", "JSONPathTokenizer"}
STMT_FIXTURES = ("identity.sql", "partial.sql")
ALL_MODULES = object()
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bits(b):
    out = []
    while b:
        low = b & -b
        out.append(low.bit_length() - 1)
        b ^= low
    return out


def pack(items):
    """items: [(number, ms)] -> (cost_ms, workers) minimising busiest load + 2 s per worker."""
    if not items:
        return 0, []
    items = sorted(items, key=lambda x: -x[1])
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, ms in items:
            j = loads.index(min(loads))
            loads[j] += ms
            ws[j].append(n)
        c = max(loads) + WORKER_MS * k
        if best is None or c < best[0]:
            best = (c, ws)
    return best


def pack_cost(ms_list):
    if not ms_list:
        return 0
    ms_list = sorted(ms_list, reverse=True)
    best = None
    for k in range(1, min(8, len(ms_list)) + 1):
        loads = [0] * k
        for ms in ms_list:
            j = loads.index(min(loads))
            loads[j] += ms
        c = max(loads) + WORKER_MS * k
        if best is None or c < best:
            best = c
        if loads and max(loads) == ms_list[0]:
            break   # more workers cannot lower the busiest load any further
    return best


class Index:
    def __init__(self):
        d = pickle.load(open(INDEX, "rb"))
        self.keys = d["keys"]
        self.n = d["n_tests"]
        self.tests = self.keys[:self.n]
        self.real = (1 << self.n) - 1
        self.bs = d["bits"]            # table of bitsets over items; the maps below hold positions
        self.rl, self.il = d["rl"], d["il"]
        self.classes = {k: self.bs[v] for k, v in d["classes"].items()}
        self.files = {k: self.bs[v] for k, v in d["files"].items()}
        self.cost = d["cost"]
        self.total = d["total"]
        self.warm = {f: set(v) for f, v in d.get("warm", {}).items()}   # lines some tests reach only after others ran
        self.readers = d.get("readers", {})         # identifier -> tests executing a line that mentions it
        self.dependents = d.get("dependents", {})   # sqlglot file -> modules importing it, directly or not
        self.file_of = [t.split("::")[0] for t in self.tests]
        self.class_of = [t.rsplit("::", 1)[0] for t in self.tests]
        self.in_file = {}
        for i, f in enumerate(self.file_of):
            self.in_file[f] = self.in_file.get(f, 0) | (1 << i)
        # slices: test idx -> {"base": idx, "files": {fixture: {entry: idx}}, "mask": bits}
        self.slices = {}
        num = {t: i for i, t in enumerate(self.tests)}
        for j in range(self.n, len(self.keys)):
            tid, fx, ent = self.keys[j].split("#")
            s = self.slices.setdefault(num[tid], {"base": None, "files": {}, "mask": 0, "phase": {}})
            if fx.startswith("@"):
                s["phase"].setdefault(fx[1:], {})[int(ent)] = j     # staged checks (brtpc)
                continue
            s["mask"] |= 1 << j
            if fx == "":
                s["base"] = j
            else:
                s["files"].setdefault(fx, {})[int(ent)] = j
        # per-class fixed cost of running anything from that class (imports + class fixtures)
        ov = {}
        for i in range(self.n):
            ov.setdefault(self.class_of[i], []).append(max(0.0, self.total[i] - self.cost[i]))
        self.class_overhead = {c: sorted(v)[len(v) // 2] for c, v in ov.items()}
        base_ov = min(self.class_overhead.values()) if self.class_overhead else 0.0
        self.class_overhead = {c: max(0.0, v - base_ov) for c, v in self.class_overhead.items()}
        self.import_cost = base_ov
        self._pyfile = {}
        self._src = None
        self._trees = {}
        # test classes whose tests share one process pool: {class: (lines run while unpickling results, users)}
        self.pools = {}
        for cls, lines in d.get("pool_unpickle", {}).items():
            f = cls.split("::")[0]
            try:
                src = open(os.path.join(REPO, f), encoding="utf-8").read().split("\n")
            except OSError:
                continue
            at = [i for i, line in enumerate(src, 1) if ".pool.submit(" in line]
            users = self.cover(f, at, runtime_only=True) & self.in_file.get(f, 0)
            self.pools[cls] = ({k: set(v) for k, v in lines.items()}, users)

    def cover(self, path, lines, runtime_only=False):
        b = 0
        bs = self.bs
        r = self.rl.get(path)
        i = None if runtime_only else self.il.get(path)
        for ln in lines:
            if r:
                b |= bs[r.get(ln, 0)]
            if i:
                b |= bs[i.get(ln, 0)]
        return b

    def pyfile(self, path):
        """Everything that executes any line of the file (so: everything that imports it)."""
        b = self._pyfile.get(path)
        if b is None:
            b = 0
            for m in (self.rl.get(path), self.il.get(path)):
                if m:
                    for v in set(m.values()):
                        b |= self.bs[v]
            self._pyfile[path] = b
        return b

    def sources(self):
        if self._src is None:
            self._src = {}
            for base in ("sqlglot", "tests"):
                for dp, dn, fn in os.walk(os.path.join(REPO, base)):
                    if "fixtures" in dp or "__pycache__" in dp:
                        continue
                    for f in fn:
                        if f.endswith(".py"):
                            p = os.path.join(dp, f)
                            try:
                                self._src[os.path.relpath(p, REPO)] = open(p, encoding="utf-8").read()
                            except (OSError, UnicodeDecodeError):
                                pass
        return self._src

    def tree(self, rel):
        t = self._trees.get(rel)
        if t is None:
            import ast
            t = self._trees[rel] = ast.parse(self.sources()[rel])
        return t

    def closure(self, names, skip=None):
        """Follow a module/class-level name to the code that reads it.

        Returns (run-time readers bitset, bounded, modules that touch the names while importing).
        A mention on a line that runs during imports must be an assignment; its targets then
        count as derived names and are followed too.  Anything else makes the result unbounded.
        """
        names = set(names)
        readers = 0
        bounded = True
        seen = set()
        mods = set()
        for _ in range(5):
            rx = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in sorted(names)))
            added = False
            for rel, src in self.sources().items():
                if not rx.search(src):
                    continue
                rmap = self.rl.get(rel, {})
                imap = self.il.get(rel, {})
                for i, line in enumerate(src.split("\n"), 1):
                    if not rx.search(line):
                        continue
                    readers |= self.bs[rmap.get(i, 0)]
                    # a line that only ever runs while modules import feeds module state
                    if i in imap and i not in rmap and (rel, i) not in seen:
                        seen.add((rel, i))
                        mods.add(rel)
                        if skip and (rel, i) in skip:
                            continue
                        for m in rx.finditer(line):
                            col = len(line[:m.start()].encode())
                            try:
                                site = brplan.locate(self.tree(rel), i, col)
                            except SyntaxError:
                                site = None
                            if site is None or site.kind == "none" or not site.assign:
                                bounded = False
                            else:
                                new = set(site.assign) - names
                                if new:
                                    names |= new
                                    added = True
            if not added:
                break
            if len(names) > 16:
                bounded = False
                break
        else:
            bounded = False
        return readers, bounded, mods

    def name_refs(self, name):
        return self.bs[self.readers.get(name, 0)]

    def def_callers(self, name):
        """Tests that run the body of any function or method called `name`, anywhere in the library.
        -> (bitset, whether such a def exists). Methods are often reached by name-based dispatch,
        which no line mentioning the name shows."""
        rx = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+%s\b" % re.escape(name), re.M)
        b = 0
        found = False
        for rel, src in self.sources().items():
            if not rel.startswith("sqlglot/") or ("def " + name) not in src:
                continue
            lines = src.split("\n")
            for m in rx.finditer(src):
                found = True
                indent = len(m.group(1).expandtabs(8))
                start = src.count("\n", 0, m.start()) + 1
                body = []
                for i in range(start + 1, len(lines) + 1):
                    text = lines[i - 1]
                    if text.strip() and len(text) - len(text.lstrip()) <= indent and not text.lstrip().startswith((")", "#")):
                        break
                    body.append(i)
                b |= self.cover(rel, body, runtime_only=True)
        return b, found


def refine_cand(ix, changed, callable_attr=None):
    """Candidates of the module/class attributes that differ from the pinned commit after import:
    tests that use the owning class (or a subclass) and execute, at run time, a line naming the attribute."""
    groups = {}
    for mod, cls, attr in changed:
        groups.setdefault((mod, cls), set()).add(attr)
    b = 0
    cache = {}
    for (mod, cls), attrs in groups.items():
        special = any(a.startswith("__") and a.endswith("__") for a in attrs)
        key = tuple(sorted(attrs))
        if key not in cache:
            r = 0
            for a in key:
                got = ix.name_refs(a)
                callers, is_def = ix.def_callers(a)
                got |= callers
                if (is_def or (callable_attr and callable_attr(mod, cls, a))) and not got:
                    special = True      # a callable nobody is seen to call: assume name-based dispatch
                r |= got
            cache[key] = (r, special)
        readers, sp = cache[key]
        special = special or sp
        if cls:
            parts = cls.split(".")
            users = 0
            for k in range(1, len(parts) + 1):
                users |= ix.classes.get(mod + ":" + ".".join(parts[:k]), 0)
            if parts[-1] not in GENERIC_CLASS_NAMES and len(parts[-1]) > 3:
                users |= ix.name_refs(parts[-1])
            b |= users if special else users & readers
        else:
            b |= readers
    return b


# ---------------------------------------------------------------- fixtures

def _filter(s):
    return "\n".join(line for line in s.splitlines() if line and not line.startswith("--"))


def _extract_meta(sql):
    lines = sql.split("\n")
    i = 0
    while lines[i].startswith("#"):
        key, val = lines[i].split(":", maxsplit=1)
        i += 1
    return "\n".join(lines[i:])


def fixture_entries(rel, text):
    """Entries as tests/helpers.py yields them; raises what the loader would raise."""
    if rel in STMT_FIXTURES or rel.endswith("__invalid.sql"):
        out, lines = [], []
        for line in _filter(text).splitlines():
            lines.append(line)
            if not line.startswith("#"):
                out.append("\n".join(lines))
                lines = []
        return out
    st = _filter(text).split(";")
    out = []
    for i in range(0, len(st), 2):
        if i + 1 < len(st):
            sql = st[i].strip()
            _extract_meta(sql)
            out.append((sql, st[i + 1].strip()))
    return out


# ---------------------------------------------------------------- planning

CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
PYTEST_INI = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg", "pyproject.toml")
PYTEST_SECTION = re.compile(r"^\[(pytest|tool:pytest|tool\.pytest[^\]]*)\]", re.M)


def cookie(src):
    """Declared source encoding, normalised; utf-8 when none is declared."""
    import codecs
    for line in src.split("\n", 2)[:2]:
        m = CODING.match(line)
        if m:
            try:
                return codecs.lookup(m.group(1)).name
            except LookupError:
                return "?" + m.group(1).lower()
    return "utf-8"


def pytest_config(text):
    """The pytest sections of a config file, as text ('' when it has none)."""
    out, keep = [], False
    for line in (text or "").splitlines():
        if line.startswith("["):
            keep = bool(PYTEST_SECTION.match(line))
        if keep and line.strip():
            out.append(line.strip())
    return "\n".join(out)


def tests_under(ix, directory):
    pre = directory + "/" if directory else ""
    b = 0
    for f, m in ix.in_file.items():
        if f.startswith(pre):
            b |= m
    return b


class Plan:
    def __init__(self, cid):
        self.cid = cid
        self.direct = 0       # select without running
        self.cand = 0         # candidates (tests and slices)
        self.files = {}       # relpath -> new text | None (deleted)
        self.notes = []
        self.unpatchable = False
        self.code = False     # touches python code (as opposed to data files only)
        self.check = set()    # sqlglot modules to import first: an import failure fails their importers
        self.icand = 0        # static candidates of module/class-level edits (fallback for `refine`)
        self.refine = False   # module/class-level edit in sqlglot: compare module state after import
        self.unverified = None
        self.site_lines = []  # (path, lines) of function-level changes
        self.probe = []       # (kind, relpath): conftest.py / test package __init__ to import in the child
        self.probe_under = 0  # tests those files can affect (selected when the child cannot tell)
        self.pool_risk = set()   # classes whose shared pool the change could break (see brrun.SyncPool)
        self.risk_users = 0      # their pool-using tests: all selected unless the pool is seen to survive
        self.ended = False
        self.failed = set()
        self.done = set()
        self.items = []


def plan_py(ix, plan, path, old, new, fp):
    mod = path[:-3].replace("/", ".")
    is_test = path.startswith("tests/")
    plan.code = True
    base_name = os.path.basename(path)
    if is_test and new is not None and base_name in ("conftest.py", "__init__.py"):
        # pytest loads conftest.py itself, and a test package's __init__ runs before any of its
        # tests import: the child imports the file and sees whether it breaks or hooks into the run
        if old is None or brplan.analyze_py(old, new, fp, path)[0] != "noop":
            plan.probe.append(("conftest" if base_name == "conftest.py" else "pkginit", path))
            plan.probe_under |= tests_under(ix, os.path.dirname(path))
            plan.notes.append("%s:probe" % path)
        return
    if old is None:   # new module: only directory listings can notice it
        b = ix.files.get("L:" + os.path.dirname(path), 0)
        plan.cand |= b
        plan.notes.append("%s:new:%d" % (path, bin(b & ix.real).count("1")))
        return
    if new is None:   # deleted module: whatever imports it
        plan.cand |= ix.pyfile(path) | ix.files.get("L:" + os.path.dirname(path), 0)
        plan.check.add(path)
        if is_test:   # a test whose file is gone cannot pass
            plan.direct |= ix.in_file.get(path, 0)
        plan.notes.append("%s:deleted" % path)
        return
    if cookie(old) != cookie(new):
        # the encoding declaration changed: the bytes on disk are decoded differently
        try:
            compile(new.encode("utf-8", "surrogateescape"), path, "exec", dont_inherit=True)
            bad = False
        except (SyntaxError, ValueError, LookupError):
            bad = True
        b = (ix.pyfile(path) | ix.in_file.get(path, 0)) & ix.real
        if bad:
            plan.direct |= b
            plan.notes.append("%s:bad-encoding:%d" % (path, bin(b).count("1")))
            return
        if not new.isascii():
            plan.cand |= b
            plan.check.add(path)
            plan.notes.append("%s:encoding:%d" % (path, bin(b).count("1")))
    kind, sites = brplan.analyze_py(old, new, fp, path)
    if kind == "noop":
        plan.notes.append("%s:noop" % path)
        return
    if kind == "syntax":       # the module cannot be imported: everything that imports it fails
        b = (ix.pyfile(path) | ix.in_file.get(path, 0)) & ix.real
        plan.direct |= b
        plan.notes.append("%s:syntax:%d" % (path, bin(b).count("1")))
        return
    defs = set()
    for s in sites:
        if s.kind == "import" and not s.sig:
            defs |= {(path, ln) for ln in s.lines}
    for s in sites:
        tag = s.kind
        if s.docstring:
            plan.notes.append("%s:%s/doc" % (path, tag))
            continue
        if s.kind == "func":
            lines = s.lines
            if not lines and s.func_range:
                lines = range(s.func_range[0], s.func_range[1] + 1)
            if not lines and s.fresh:
                continue        # body of a function the patch adds: its `def` line is handled as a new name
            if is_test and not lines:
                continue        # statement added to a test: the test itself is found through its other lines
            plan.site_lines.append((path, set(lines)))
            if s.lam and lines:
                # the lambda's first line also holds the statement that defines it (runs on import);
                # only run-time execution of that line means the lambda itself was called
                first = min(lines)
                b = ix.cover(path, [first], runtime_only=True) | ix.cover(path, [ln for ln in lines if ln != first])
                tag += "/lambda"
            else:
                b = ix.cover(path, lines) if lines else ix.pyfile(path)
            if any(ln in ix.il.get(path, ()) and ln not in ix.rl.get(path, ()) for ln in lines):
                plan.check.add(path)    # runs only while importing
            for cls, (ulines, users) in ix.pools.items():
                if any(ln in ulines.get(path, ()) for ln in lines):
                    plan.pool_risk.add(cls)     # runs while a pool result is unpickled
            if s.func_range and any(ln in ix.warm.get(path, ()) for ln in lines):
                # reached only in the wake of other tests: who gets here depends on the run order,
                # so take everything that enters the function at all
                b |= ix.cover(path, range(s.func_range[0], s.func_range[1] + 1))
                tag += "/order-dependent"
        else:
            base = ix.cover(path, s.lines) if s.lines else ix.pyfile(path)
            if not base:
                base = ix.pyfile(path) if not s.lines else 0
            plan.check.add(path)
            b = base
            if is_test and s.sig and s.fresh and s.target and s.target[0].startswith("test") \
                    and not re.search(r"\bdef %s\b" % re.escape(s.target[0]), old):
                plan.notes.append("%s:new-test" % path)     # a test nobody is asked about
                continue
            if is_test and s.sig and s.target and s.target[0].startswith("test") and s.classes:
                # the `def` line (name, decorators, signature) of one test method: only that test
                suffix = "::%s::%s" % (s.classes[-1], s.target[0])
                b = 0
                for t in bits(ix.in_file.get(path, 0)):
                    if ix.tests[t].endswith(suffix):
                        b |= 1 << t
                tag += "/test-def"
            elif is_test and s.sig and s.body_range and s.target and s.target[0] not in (
                    "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule",
                    "asyncSetUp", "asyncTearDown", "run", "__init__"):
                # a helper's `def` line: whoever calls it
                b = ix.cover(path, range(s.body_range[0], s.body_range[1] + 1))
                tag += "/helper-def"
            elif is_test:
                b = base | ix.in_file.get(path, 0)
                tag += "/testfile"
                if s.assign and not s.classes:
                    readers, bounded, _ = ix.closure(s.assign, skip=defs)
                    if bounded:
                        b &= readers
                        tag += "/readers"
            elif s.sig:
                if s.body_range:
                    b = ix.cover(path, range(s.body_range[0], s.body_range[1] + 1))
                    tag += "/sig"
                    plan.cand |= b      # callers of the function, whatever the module state says
            else:
                tag += "/%s" % s.stmt_type      # the child compares module state and narrows this
        if s.kind == "import" and not is_test:
            plan.icand |= b
            plan.refine = True
        else:
            plan.cand |= b
        plan.notes.append("%s:%s:%d" % (path, tag, bin(b & ix.real).count("1")))
    if path.startswith("sqlglot/typing/"):
        plan.cand |= ix.in_file.get("tests/test_typing.py", 0)


def plan_data(ix, plan, path, old, new, fp=None):
    readers = ix.files.get(path, 0)
    base_name = os.path.basename(path)
    if base_name == "conftest.py" and new is not None:
        plan.probe.append(("conftest", path))
        plan.probe_under |= tests_under(ix, os.path.dirname(path) if path.startswith("tests/") else "")
        plan.notes.append("%s:probe" % path)
        return
    if base_name in PYTEST_INI and os.path.dirname(path) in ("", "tests"):
        if pytest_config(old) != pytest_config(new) or (base_name.lstrip(".").startswith("pytest.") and (old or "") != (new or "")):
            plan.direct |= ix.real       # pytest itself is configured differently: nothing here can tell how
            plan.notes.append("%s:pytest-config" % path)
            return
    if old is None or new is None:
        readers |= ix.files.get("L:" + os.path.dirname(path), 0)
        plan.cand |= readers & ix.real
        plan.notes.append("%s:data-add/del:%d" % (path, bin(readers & ix.real).count("1")))
        return
    if not readers:
        plan.notes.append("%s:unread" % path)
        return
    if path.startswith("tests/fixtures/") and path.endswith(".sql"):
        rel = path[len("tests/fixtures/"):]
        if _filter(old) == _filter(new):
            plan.notes.append("%s:fixture-equivalent" % path)
            return
        try:
            a, b = fixture_entries(rel, old), fixture_entries(rel, new)
        except Exception:
            a = b = None
        if a is not None and len(a) == len(b):
            changed = {i for i in range(len(a)) if a[i] != b[i]}
            # an edit confined to `#` / `--` lines may well be harmless: worth checking by running
            meta_only = fp is not None and all(
                (not t.strip()) or t.lstrip().startswith(("#", "--"))
                for h in fp.hunks for tag, t in h[4] if tag != " ")
            sel = 0
            for t in bits(readers & ix.real):
                sl = ix.slices.get(t)
                if sl and rel in sl["files"]:
                    ent = sl["files"][rel]
                    hit = [ent[i] for i in changed if i in ent]
                    if hit:
                        sel |= 1 << t
                        for j in hit:
                            sel |= 1 << j
                elif ix.class_of[t] == "tests/test_executor.py::TestExecutor":
                    continue     # only loads the file in its class fixture; the entries still load
                else:
                    sel |= 1 << t
            if meta_only:
                plan.cand |= sel
            else:
                plan.fixture_sel = getattr(plan, "fixture_sel", 0) | sel
            plan.notes.append("%s:fixture%s:%d entries:%d tests" % (path, "/meta" if meta_only else "", len(changed), bin(sel & ix.real).count("1")))
            return
        plan.fixture_sel = getattr(plan, "fixture_sel", 0) | (readers & ix.real)
        plan.notes.append("%s:fixture-reshaped:%d" % (path, bin(readers & ix.real).count("1")))
        return
    plan.cand |= readers & ix.real       # cheap to check by running the readers against the new file
    plan.notes.append("%s:data:%d" % (path, bin(readers & ix.real).count("1")))


def make_plan(ix, cid, text):
    plan = Plan(cid)
    try:
        fps = applypatch.parse(text)
    except Exception as e:
        plan.direct = ix.real
        plan.notes.append("parse-error %r" % e)
        return plan
    if sum(len(fp.hunks) for fp in fps) != len(re.findall(r"^@@ -\d", text, re.M)) or (not fps and text.strip()):
        # hunks the parser could not attribute to a file: nothing here can be trusted
        plan.direct = ix.real
        plan.unpatchable = True
        plan.notes.append("unparsed-hunks")
        return plan
    for fp in fps:
        src = fp.rename_from or (None if fp.is_new else fp.old)
        dst = None if fp.is_deleted else (fp.rename_to or fp.new or fp.old)
        old = None
        changed_lines = [t for h in fp.hunks for tag, t in h[4] if tag != " "]
        if (changed_lines and all(t.startswith("Subproject commit ") for t in changed_lines)) \
                or (src and os.path.isdir(os.path.join(REPO, src))):
            plan.notes.append("%s:submodule-pointer" % (dst or src))   # not a file of this checkout
            continue
        if src:
            try:
                old = open(os.path.join(REPO, src), encoding="utf-8", newline="").read()
            except (OSError, UnicodeDecodeError):
                old = None
        if fp.binary or (src and old is None):
            for p in (src, dst):
                if p:
                    plan.direct |= (ix.files.get(p, 0) | ix.pyfile(p) | ix.in_file.get(p, 0)) & ix.real
            plan.notes.append("%s:binary/unreadable" % (dst or src))
            plan.unpatchable = True
            continue
        try:
            new = None if fp.is_deleted else (applypatch.apply_text(old, fp) if fp.hunks else (old or ""))
        except Exception:
            for p in (src, dst):
                if p:
                    plan.direct |= (ix.files.get(p, 0) | ix.pyfile(p) | ix.in_file.get(p, 0)) & ix.real
            plan.notes.append("%s:does-not-apply" % (dst or src))
            plan.unpatchable = True
            continue
        todo = []
        if src and src != dst:
            plan.files[src] = None
            todo.append((src, old, None))
            if dst:
                todo.append((dst, None, new))
        else:
            todo.append((dst or src, old, new))
        if dst:
            plan.files[dst] = new
        for path, o, n in todo:
            if o == n:
                continue
            if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
                plan_py(ix, plan, path, o, n, fp)
            else:
                plan_data(ix, plan, path, o, n, fp)
    return plan


def build_items(ix, plan, ms):
    """Turn candidate bits into runnable items: (test idx, slice spec | None, est seconds)."""
    fx = getattr(plan, "fixture_sel", 0)
    if fx:
        if plan.code:
            plan.cand |= fx          # the code change may alter what the edited entries do
        else:
            # fixture edits nearly always break their reader: select cheap readers outright,
            # check only the expensive ones, on just the edited entries
            for t in bits(fx & ix.real):
                if ms[t] >= 5000 and t in ix.slices:
                    plan.cand |= (1 << t) | (fx & ix.slices[t]["mask"])
                else:
                    plan.direct |= 1 << t
    items = []
    touches_tests = any(f.startswith("tests/") for f in plan.files)
    for t in bits(plan.cand & ix.real & ~plan.direct):
        sl = ix.slices.get(t)
        full = ix.cost[t]
        spec = None
        est = full
        ph = sl["phase"] if sl else None
        if ph and not touches_tests and not plan.cand >> ph["S"][0] & 1:
            # staged check: only stages the change reaches are re-run for real
            a_cost = ix.cost[ph["A"][0]]
            if "B" in ph:
                need_b = {i for i, j in ph["B"].items() if plan.cand >> j & 1}
                need_c = {i for i, j in ph["C"].items() if plan.cand >> j & 1}
                c = a_cost + sum(ix.cost[ph["B"][i]] for i in need_b | need_c) + sum(ix.cost[ph["C"][i]] for i in need_c)
                if need_c:
                    c += ix.class_overhead.get(ix.class_of[t], 0)
                if c < full * 0.9:
                    items.append((t, {"@tpcds": (need_b, need_c, {i: ix.cost[j] for i, j in ph["C"].items()})}, c * SPEED))
                    continue
            elif not plan.cand >> ph["C"][0] & 1:
                items.append((t, {"@tpch": True}, a_cost * SPEED))
                continue
        if sl and sl["base"] is not None:
            hit = plan.cand & sl["mask"]
            if hit:
                base_cost = ix.cost[sl["base"]]
                c = base_cost
                spec = {}
                for f, ent in sl["files"].items():
                    for i, j in ent.items():
                        if hit >> j & 1:
                            spec.setdefault(f, set()).add(i)
                            c += max(0.0, ix.cost[j] - base_cost)
                if c < full * 0.9:
                    est = c
                else:
                    spec = None
        items.append((t, spec, est * SPEED))
    return items


# ---------------------------------------------------------------- verification child

_TREE = None


CHILD_MB = 1500


def rss_mb(pid):
    try:
        with open("/proc/%d/statm" % pid) as f:
            return int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") / 1048576.0)
    except (OSError, ValueError, IndexError):
        return 0.0


def scan_tree():
    """Read the checkout once: directories, python files (kept in memory) and everything else."""
    global _TREE
    if _TREE is not None:
        return _TREE
    tree = []
    for dp, dn, fn in os.walk(REPO):
        dn[:] = sorted(d for d in dn if d not in (".git", "__pycache__"))
        rel = os.path.relpath(dp, REPO)
        rel = "" if rel == "." else rel
        pys, others = [], []
        for f in sorted(fn):
            p = os.path.join(dp, f)
            if f.endswith(".py") and not os.path.islink(p):
                st = os.stat(p)
                with open(p, "rb") as fh:
                    pys.append((f, fh.read(), st.st_mtime_ns))
            else:
                others.append(f)
        tree.append((rel, pys, others, os.path.isdir(os.path.join(dp, "__pycache__"))))
    _TREE = tree
    return tree


def materialize(dst, files):
    """Scratch copy of the repo. Real directories and real copies of every python file (tests
    resolve their own path, symlinks would lead them back to the unpatched checkout); modification
    times are kept so the compiled caches, shared by symlink, stay valid. Data files are symlinks."""
    for rel, pys, others, has_cache in scan_tree():
        d = os.path.join(dst, rel) if rel else dst
        os.makedirs(d, exist_ok=True)
        sd = os.path.join(REPO, rel) if rel else REPO
        for f, data, mtime in pys:
            r = rel + "/" + f if rel else f
            if r in files:
                continue
            p = os.path.join(d, f)
            with open(p, "wb") as fh:
                fh.write(data)
            os.utime(p, ns=(mtime, mtime))
        for f in others:
            r = rel + "/" + f if rel else f
            if r not in files:
                os.symlink(os.path.join(sd, f), os.path.join(d, f))
        if has_cache:
            os.symlink(os.path.join(sd, "__pycache__"), os.path.join(d, "__pycache__"))
    for rel, text in files.items():
        if text is None:
            continue
        p = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
        os.utime(p, (1, 1))


def stop_rule(fixed_ms, remaining, anchored):
    """True when selecting the remaining candidates blindly is no dearer than checking them."""
    run = sum(e for _, e in remaining)
    blind = (pack_cost(fixed_ms + [m for m, _ in remaining]) - pack_cost(fixed_ms)) / 1000.0
    expect_fail = 0.0 if (anchored or fixed_ms) else P_ANY * 2.3
    return blind <= run + expect_fail, blind, run


def child_main(ix, plan, ms, wfd):
    import brrun
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    out = os.fdopen(wfd, "w", buffering=1)
    root = os.path.join(SCRATCH, plan.cid)
    materialize(root, plan.files)
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root
    try:
        import sqlglot  # noqa: F401
    except BaseException:
        out.write("ALL\n")
        out.flush()
        os._exit(0)
    import importlib
    broken = 0
    if plan.refine:
        # which module / class attributes differ from the pinned commit once everything is imported?
        try:
            import brstate
            mods = None
            for rel in plan.check:
                dep = ix.dependents.get(rel)
                if dep is None or mods is ALL_MODULES:
                    mods = ALL_MODULES          # not a module we know: look at everything
                else:
                    mods = (mods or set()) | set(dep)
            failed, changed = brstate.diff(pickle.load(open(os.path.join(HERE, "state.pkl"), "rb")),
                                           None if mods is ALL_MODULES or not mods else sorted(mods))
            for name in failed:
                rel = name[0].replace(".", "/")
                rel = rel + ".py" if os.path.exists(os.path.join(REPO, rel + ".py")) else rel + "/__init__.py"
                broken |= ix.pyfile(rel) & ix.real
            plan.direct = plan.base_direct
            def is_callable(mod, cls, attr):
                try:
                    o = sys.modules[mod]
                    for part in (cls.split(".") if cls else ()):
                        o = vars(o)[part]
                    v = vars(o)[attr]
                    return callable(v) or isinstance(v, (staticmethod, classmethod, property))
                except Exception:
                    return True         # gone (or unreadable): it may have been a method

            # a test file that mentions a changed or removed name may no longer import
            names = {k[2] for k in changed} | {k[1].split(".")[0] for k in changed if k[1]}
            names = {n for n in names if n and not (n.startswith("__") and n.endswith("__"))}
            if names:
                rx = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in sorted(names)))
                for rel, src in ix.sources().items():
                    if rel.startswith("tests/") and rx.search(src) and rel not in plan.files:
                        try:
                            importlib.import_module(rel[:-3].replace("/", "."))
                        except BaseException:
                            broken |= (ix.pyfile(rel) | ix.in_file.get(rel, 0)) & ix.real
            decide(ix, plan, ms, plan.base_cand | refine_cand(ix, changed, is_callable))
            out.write("N %d %d %s\n" % (len(changed), len(failed), ",".join("/".join(k) for k in changed[:6]).replace(" ", "")))
        except Exception:
            pass                        # keep the static plan
    else:
        for rel in sorted(plan.check):
            if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
                continue
            name = rel[:-3].replace("/", ".")
            if name.endswith(".__init__"):
                name = name[:-9]
            try:
                importlib.import_module(name)
            except BaseException:
                broken |= ix.pyfile(rel) & ix.real
    for kind, rel in plan.probe:
        under = tests_under(ix, os.path.dirname(rel) if rel.startswith("tests/") else "")
        name = rel[:-3].replace("/", ".")
        if name.endswith(".__init__"):
            name = name[:-9]
        try:
            mod = importlib.import_module(name)
        except BaseException:
            broken |= under              # nothing under it can be collected
            continue
        if kind == "conftest":
            for k, v in list(vars(mod).items()):
                marker = getattr(v, "_fixture_function_marker", None) or getattr(v, "_pytestfixturefunction", None)
                if k.startswith("pytest_") or getattr(marker, "autouse", False):
                    plan.direct |= under     # it takes part in every test: only pytest could tell how
                    break
    # everything still open: selected unless a result below clears it
    out.write("U %s\n" % " ".join(str(n) for n in bits((plan.direct & ix.real) | sum(1 << t for t, _, _ in plan.items))))
    for n in bits(broken):
        out.write("%d F\n" % n)
    out.flush()
    items = [it for it in plan.items if not broken >> it[0] & 1]
    remaining = {t: (ms[t], est) for t, spec, est in items}
    state = {"timeouts": 0, "since": 0, "fixed": [ms[n] for n in bits(plan.direct | broken)]}
    est_of = {ix.tests[t]: est for t, _, est in items}
    num = {ix.tests[t]: t for t, _, _ in items}

    if plan.risk_users:
        out.write("R %s\n" % " ".join(str(n) for n in bits(plan.risk_users)))

    def finish():
        users = 0
        if brrun.BROKEN_POOLS:                  # a shared pool broke: every test using one may fail
            for _, u in ix.pools.values():
                users |= u
        elif plan.risk_users and any(plan.risk_users >> n & 1 for n in remaining):
            users = plan.risk_users             # stopped before the pool-using candidates were all run
        for n in bits(users):
            out.write("%d F\n" % n)
        out.write("E\n")
        out.flush()
        os._exit(0)

    def report(key, st, cpu):
        n = num[key]
        remaining.pop(n, None)
        if st == "pass":
            out.write("%d P\n" % n)
            state["since"] += 1
            recheck = state["since"] >= 8
        else:
            out.write("%d F\n" % n)
            state["fixed"].append(ms[n])
            recheck = True
            if st == "fail:timeout":
                state["timeouts"] += 1
                if state["timeouts"] >= 3:
                    finish()
        if recheck and remaining:
            state["since"] = 0
            if stop_rule(state["fixed"], list(remaining.values()), True)[0]:
                finish()

    def budget(key):
        return max(6.0, 8.0 * est_of[key])

    brrun.FAILFAST = True
    brrun.FINGERPRINTS = os.path.join(HERE, "tpc.pkl")
    brrun.run_tests([(ix.tests[t], spec, ix.tests[t]) for t, spec, est in items], budget=budget, report=report)
    finish()


def decide(ix, p, ms, cand):
    """Split candidates into `direct` (select blindly) and `items` (check by running)."""
    p.cand = cand
    raw = build_items(ix, p, ms)
    keep = []
    for t, spec, est in raw:
        if est > 0.8 * (ms[t] + WORKER_MS) / 1000.0:
            p.direct |= 1 << t          # dearer to check than to select
        else:
            keep.append((t, spec, est))
    keep.sort(key=lambda it: (-ms[it[0]], it[0]))
    classes = {ix.class_of[t] for t, _, _ in keep}
    p.est = sum(e for _, _, e in keep) + SPEED * (ix.import_cost + sum(ix.class_overhead.get(c, 0) for c in classes)) + FORK_COST
    fixed = [ms[n] for n in bits(p.direct)]
    if keep and (p.unpatchable or NO_RUN):
        p.direct |= sum(1 << t for t, _, _ in keep)
        keep = []
    if keep:
        # 1. the few long tests: check one when that is cheaper than what selecting it adds
        pool = [ms[t] for t, _, _ in keep]
        first, rest = [], []
        for n, (t, spec, e) in enumerate(keep):
            if n < 10 and ms[t] >= 1500:
                others = list(pool)
                others.remove(ms[t])
                gain = (pack_cost(fixed + pool) - pack_cost(fixed + others)) / 1000.0
                if gain > e * 1.15 + 0.3:
                    first.append((t, spec, e))
                    pool = others
                    continue
            rest.append((t, spec, e))
        # 2. everything else, as a block
        overhead = SPEED * (ix.import_cost + sum(ix.class_overhead.get(c, 0) for c in {ix.class_of[t] for t, _, _ in rest})) + FORK_COST
        run = sum(e for _, _, e in rest) + overhead + (0 if (fixed or first) else P_ANY * 2.3)
        blind = (pack_cost(fixed + [ms[t] for t, _, _ in rest]) - pack_cost(fixed)) / 1000.0
        if rest and blind <= run:
            p.notes.append("blind %d (%.1f<=%.1f)" % (len(rest), blind, run))
            p.direct |= sum(1 << t for t, _, _ in rest)
            rest = []
        keep = first + rest
        classes = {ix.class_of[t] for t, _, _ in keep}
        p.est = sum(e for _, _, e in keep) + SPEED * (ix.import_cost + sum(ix.class_overhead.get(c, 0) for c in classes)) + FORK_COST
    p.items = keep
    # A change that can make unpickling a pool result fail breaks the class's shared pool, and then
    # every later test using that pool fails whatever it covers. Unless the candidates that use the
    # pool are all going to be run (the stand-in pool breaks the same way), select every user.
    p.risk_users = 0
    for cls, (ulines, users) in ix.pools.items():
        risky = cls in p.pool_risk or (p.refine and (cand & ~getattr(p, "base_cand", 0)) & users)
        if risky and cand & users:
            p.risk_users |= users
            if p.direct & cand & users:
                p.direct |= users
                p.notes.append("pool-risk: all %d users" % bin(users).count("1"))


def prepare(ix, cid, text, ms):
    """Plan one commit. With `refine` set the child redoes `decide` on what the module state shows."""
    try:
        p = make_plan(ix, cid, text)
        p.base_direct = p.direct
        p.base_cand = p.cand
        decide(ix, p, ms, p.cand | p.icand)
    except Exception as e:
        p = Plan(cid)
        p.direct = p.base_direct = ix.real
        p.base_cand = 0
        p.est = 0.0
        p.notes.append("plan-crash %r" % (e,))
    if (p.refine or p.probe) and not (p.unpatchable or NO_RUN):
        p.est = max(p.est, 1.0) if p.items else 1.0
    return p


# ---------------------------------------------------------------- main

def main():
    tj = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    data_num = {t["id"]: t["number"] for t in tj}
    data_ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tj}
    ix = Index()
    known = set(ix.tests)
    unknown_tests = [t["number"] for t in tj if t["id"] not in known]
    ms = [data_ms.get(data_num.get(t, -1), 0) for t in ix.tests]
    cdir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    plans = []
    for cid in cids:
        text = open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="surrogateescape", newline="").read()
        plans.append(prepare(ix, cid, text, ms))
    log("planned %d commits: cpu %.2fs" % (len(plans), sum(os.times()[:2])))
    todo = sorted((p for p in plans if p.items or ((p.refine or p.probe) and not (p.unpatchable or NO_RUN))), key=lambda p: -p.est)
    if todo:
        import brrun  # noqa
        import brstate
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        brstate.preload()      # test libraries, loaded once here and inherited by every child
        os.makedirs(SCRATCH, exist_ok=True)
        scan_tree()
        import gc
        gc.collect()
        gc.freeze()
    deadline = T0 + min(160.0, 4.0 * len(cids))
    active = {}
    queue = list(todo)
    while queue or active:
        while queue and len(active) < PAR:
            p = queue.pop(0)
            if time.time() + p.est / 1.5 > deadline:
                p.notes.append("no-time")
                continue  # stays unverified: every candidate is selected
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    os.setpgid(0, 0)
                    child_main(ix, p, ms, w)
                finally:
                    os._exit(1)
            os.close(w)
            active[r] = [p, pid, b"", time.time() + 3 * p.est + 25]
        if not active:
            break
        ready, _, _ = select.select(list(active), [], [], 1.0)
        now = time.time()
        for fd in list(active):          # a runaway child must never push the container over its memory limit
            if rss_mb(active[fd][1]) > CHILD_MB:
                active[fd][3] = 0
        for fd in list(active):
            p, pid, buf, kill_at = active[fd]
            eof = False
            if fd in ready:
                chunk = os.read(fd, 65536)
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    active[fd][2] = buf
                    for ln in lines:
                        if ln == b"ALL":
                            p.direct = ix.real
                            continue
                        if ln[:2] == b"U " or ln == b"U":
                            p.unverified = {int(x) for x in ln[2:].split()}
                            continue
                        if ln == b"E":
                            p.ended = True
                            continue
                        if ln[:2] == b"R " or ln == b"R":
                            p.risk_users = sum(1 << int(x) for x in ln[2:].split())
                            continue
                        if ln[:2] == b"N ":
                            p.notes.append("state:%s" % ln[2:].decode())
                            continue
                        try:
                            n, st = ln.split()
                            n = int(n)
                        except ValueError:
                            continue
                        p.done.add(n)
                        if st != b"P":
                            p.failed.add(n)
                else:
                    eof = True
            if eof or now > kill_at or now > deadline + 8:
                if not eof:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    ru = os.wait4(pid, 0)[2]
                    p.cpu = ru.ru_utime + ru.ru_stime
                except OSError:
                    pass
                os.close(fd)
                del active[fd]
                shutil.rmtree(os.path.join(SCRATCH, p.cid), ignore_errors=True)
    out = []
    for p in plans:
        if p.unverified is None:    # the child never reported: fall back to the static plan
            p.unverified = set(bits((p.direct | p.probe_under) & ix.real)) | {t for t, _, _ in p.items}
        sel = p.failed | (p.unverified - (p.done - p.failed))
        if p.risk_users and not p.ended and (p.items or p.refine):
            sel |= set(bits(p.risk_users))      # the child did not finish: its pool may have broken
        if p.direct & ix.real == ix.real:
            sel = set(range(ix.n))
        nums = [data_num[ix.tests[n]] for n in sel if ix.tests[n] in data_num]
        if unknown_tests and (p.direct & ix.real == ix.real or p.cand):
            nums = sorted(set(nums) | set(unknown_tests))
        _, ws = pack([(n, data_ms[n]) for n in nums])
        out.append(json.dumps({"commit": p.cid, "workers": ws}, separators=(",", ":")))
        log("%s cpu=%.2f cand=%d run=%d est=%.1fs failed=%d unverified=%d selected=%d | %s" % (
            p.cid, getattr(p, "cpu", 0.0), bin(p.cand & ix.real).count("1"), len(p.items), getattr(p, "est", 0), len(p.failed),
            len(p.unverified - p.done), len(nums), "; ".join(p.notes)[:400]))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    t = os.times()
    log("cpu %.2fs (self %.2f, children %.2f) wall %.1fs" % (
        t[0] + t[1] + t[2] + t[3], t[0] + t[1], t[2] + t[3], time.time() - T0))


if __name__ == "__main__":
    main()
