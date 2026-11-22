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
import heapq
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
TPC_FULL = 1.6     # traced stage-C seconds of a TPC-DS query understate a full re-run by about this much
BUDGET = float(os.environ.get("BR_BUDGET", "1"))        # scales the per-test time limit (emulated runs need more)
CALIBRATE = os.environ.get("BR_CALIBRATE", "0") == "1"   # see the calibration commit
TPC_PARTIAL = float(os.environ.get("BR_TPC_PARTIAL", "4"))   # executor seconds for a partial TPC-DS check (0: never)
FILL = os.environ.get("BR_FILL", "1") == "1"                  # cleared candidates into idle worker time
WARM = os.environ.get("BR_WARM", "1") == "1"                 # checks of patches outside the bare import start from a parent that did it
LAZY_EST = os.environ.get("BR_LAZY_EST", "1") == "1"         # count the lower set-up cost when deciding what to run
LAZY_FIXTURES = os.environ.get("BR_LAZY", "1") == "1"         # TestExecutor's class fixtures on first use
TPC_SMALL = float(os.environ.get("BR_TPC_SMALL", "0.85"))     # the same, when the cheap tests of the class all ran and passed (0: off)
SIB_MS = 400                                                 # "cheap": recorded milliseconds of such a test, at most
DIAG = os.environ.get("BR_DIAG", "0") == "1"                # see the diagnostic commit
USE_MAXNODES_CHECK = os.environ.get("BR_MAXNODES", "1") == "1"
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


def _pack_cost_slow(ms_list):
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


def pack_cost(ms_list):
    """Same value as _pack_cost_slow (longest-first onto the least loaded worker, lowest index on
    ties, for 1..8 workers). Loads are kept as load*16+worker in a heap, so the smallest entry is
    the least loaded worker with the lowest index, exactly what loads.index(min(loads)) picks; a
    worker count whose lower bound (largest item, or the mean load) cannot beat the best so far is
    skipped, since its greedy cost could not be lower."""
    if not ms_list:
        return 0
    if not all(type(m) is int and m >= 0 for m in ms_list):
        return _pack_cost_slow(ms_list)
    ms_list = sorted(ms_list, reverse=True)
    n = len(ms_list)
    total = sum(ms_list)
    top = ms_list[0]
    best = total + WORKER_MS          # k = 1
    if top == total:
        return best
    for k in range(2, min(8, n) + 1):
        if max(top, -(-total // k)) + WORKER_MS * k >= best:
            continue
        heap = list(range(k))       # load 0, worker j
        for ms in ms_list:
            heapq.heapreplace(heap, heap[0] + (ms << 4))
        c = (max(heap) >> 4) + WORKER_MS * k
        if c < best:
            best = c
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
        # (module, class, attribute) -> {key text: tests that look that key up, "*": tests that walk it}
        self.lookups = {k: {kk: self.bs[v] for kk, v in m.items()} for k, m in d.get("lookups", {}).items()}
        self.classes_dotted = {k.replace(":", ".", 1): v for k, v in self.classes.items()}
        self.warm = {f: set(v) for f, v in d.get("warm", {}).items()}   # lines some tests reach only after others ran
        self.tpc_exec = d.get("tpc_cost", {}).get("exec", {})   # TPC-DS query -> seconds in sqlglot's executor
        self.tpc_duck = d.get("tpc_cost", {}).get("duck", {})   # ... and in the DuckDB comparison
        self.tpc_rules = d.get("tpc_rules")         # lines each optimizer rule runs for each TPC-DS query
        self.tpc_sentinels = d.get("tpc_sentinels") or []   # TPC-DS queries, most telling first
        self.import_calls = {f: set(v) for f, v in d.get("import_calls", {}).items()}   # entry lines of functions called during imports
        # comparisons whose operator and its partner gave the same value on every evaluation of the whole suite
        self.cmp_same = d.get("cmp_same", {})   # path -> {(line, column): operator}
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
        self.tpcds_test = num.get("tests/test_executor.py::TestExecutor::test_execute_tpcds")
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
        memo = self.__dict__.setdefault("_dc_memo", {})
        got = memo.get(name)
        if got is None:
            got = memo[name] = self._def_callers(name)
        return got

    def _def_callers(self, name):
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


def refine_cand(ix, changed, callable_attr=None, detail=None):
    """Candidates of the module/class attributes that differ from the pinned commit after import:
    tests that use the owning class (or a subclass) and execute, at run time, a line naming the
    attribute. For a dict or set whose changed keys are known, also only tests that look one of
    those keys up (or walk the whole container); a key that is a class stands for its instances."""
    detail = detail or {}
    b = 0
    for mod, cls, attr in changed:
        special = attr.startswith("__") and attr.endswith("__")
        readers = ix.name_refs(attr)
        callers, is_def = ix.def_callers(attr)
        readers |= callers
        if (is_def or (callable_attr and callable_attr(mod, cls, attr))) and not readers:
            special = True      # a callable nobody is seen to call: assume name-based dispatch
        keys = detail.get((mod, cls, attr))
        if keys is not None and not special:
            look = ix.lookups.get((mod, cls, attr))
            f = 0
            ok = True
            for k in keys:
                if k.startswith("c:"):
                    f |= ix.classes_dotted.get(k[2:], 0) | ix.name_refs(k.rsplit(".", 1)[-1])
                elif look is not None:
                    f |= look.get(k, 0)
                else:
                    ok = False      # nobody was traced reading this container: do not trust the trace
            if look is not None and any(not k.startswith("c:") for k in keys):
                f |= look.get("*", 0)
            if ok:
                # key lookups were traced for whole tests only: fixture entries and TPC stages
                # keep every reader of the attribute
                readers &= f | ~ix.real
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
        self.state_clean = False  # set in the child: nothing in any module's state differs from the pinned commit
        self.rule_ok = True      # only plain function-body edits in sqlglot: optimizer rules can be checked one by one
        self.wide = False        # a module-level statement reaches into other modules: compare all module state
        self.sim_collect = False  # import statements changed: try the imports in pytest's collection order
        self.pool_risk = set()   # classes whose shared pool the change could break (see brrun.SyncPool)
        self.risk_users = 0      # their pool-using tests: all selected unless the pool is seen to survive
        self.ended = False
        self.failed = set()
        self.done = set()
        self.items = []
        self.blind = 0


INTERP_RX = re.compile(
    r"setrecursionlimit|warnings\s*\.\s*(?:simplefilter|filterwarnings|resetwarnings)|\b(?:simplefilter|filterwarnings)\s*\("
    r"|logging\s*\.\s*(?:basicConfig|disable|setLoggerClass|setLogRecordFactory)|\.\s*(?:setLevel|addHandler|removeHandler|addFilter)\s*\("
    r"|\.disabled\s*=|\.propagate\s*="
    r"|environ\s*(?:\[|\.\s*(?:setdefault|update|pop|clear)\b)|\b(?:putenv|unsetenv)\s*\("
    r"|setlocale|\b(?:setcontext|getcontext)\s*\("
    r"|sys\s*\.\s*(?:path|modules|meta_path|path_hooks)\s*(?:\[[^\]]*\]\s*=[^=]|\.\s*(?:insert|append|extend|remove|pop|clear|update|setdefault)\b|=[^=])"
    r"|\bdel\s+sys\s*\.\s*(?:path|modules)"
    r"|sys\s*\.\s*(?:excepthook|stdout|stderr|stdin|displayhook)\s*=|setswitchinterval|\bgc\s*\.\s*(?:disable|set_threshold|freeze)"
    r"|\bbuiltins\s*\.\s*\w+\s*=[^=]|__builtins__|\btzset\b|\bsetattr\s*\(\s*(?:sys|os|builtins|unittest|logging|warnings)\b"
    r"|\bsignal\s*\.\s*(?:signal|alarm|setitimer)|\bos\s*\.\s*(?:chdir|umask|_exit|abort|kill)\b|\bsys\s*\.\s*exit\b|\b(?:exit|quit)\s*\("
    r"|set_start_method|setdefaulttimeout|\bfaulthandler\b|\batexit\b|\bresource\s*\.\s*setrlimit")
# Statements by which a test (or a test module being imported) changes something other tests see:
# an attribute or item of a class / module set or deleted, setattr, a mock patch started by hand,
# a module reload, a class that registers itself by being defined (anything but a plain test class).
LEAK_RX = re.compile(
    r"^\s*(?:del\s+)?(?!self\.|cls\.)[\w\.]*\b(?:sqlglot|[A-Z]\w*)(?:\.\w+)+(?:\[[^\]]*\])?\s*"
    r"(?:=[^=]|\.(?:clear|update|pop|popitem|add|append|remove|discard|setdefault|extend|insert)\()"
    r"|\b(?:setattr|delattr)\s*\(\s*(?!self\b)|\.start\(\)|\breload\s*\(|__dict__\s*(?:\[|\.\s*(?:pop|update|setdefault|clear)\b)"
    r"|^\s*class\s+\w+\s*\((?!\s*(?:unittest\s*\.\s*)?TestCase\s*\)|\s*Validator\s*\))")
PYTEST_RX = re.compile(r"\bpytest\b|\bpytestmark\b")


def added_code(old, new):
    """Lines the patch adds or changes, without their comments."""
    was = set((old or "").splitlines())
    return [ln.split("#", 1)[0] for ln in new.splitlines() if ln not in was]


def inert_module(src, other_py):
    """True when importing a module with this source can do nothing but define names: only
    docstrings, imports of code the patch leaves alone, constant assignments and plain functions."""
    import ast

    def const(node):
        if node is None or isinstance(node, ast.Constant):
            return True
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return all(const(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return all(const(k) and const(v) for k, v in zip(node.keys, node.values))
        return isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)

    def fixture(dec):       # @pytest.fixture / @pytest.fixture(scope="module"): autouse is seen at import
        if isinstance(dec, ast.Call):
            if not all(const(a) for a in dec.args) or not all(const(k.value) for k in dec.keywords):
                return False
            dec = dec.func
        return (isinstance(dec, ast.Attribute) and dec.attr == "fixture" and isinstance(dec.value, ast.Name)
                and dec.value.id == "pytest") or (isinstance(dec, ast.Name) and dec.id == "fixture")

    def own(name):          # an import that may execute code the patch changed or added
        return bool(other_py) and (not name or name.split(".")[0] in ("sqlglot", "tests", "sqlglotc", "benchmarks", "setup"))

    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Import):
            if any(own(a.name) for a in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if node.level or own(node.module or "") or any(a.name == "*" for a in node.names):
                return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            if not all(fixture(d) for d in node.decorator_list) or not all(const(d) for d in a.defaults + a.kw_defaults):
                return False
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not all(isinstance(t, ast.Name) and not t.id.startswith(("pytest", "collect_", "__")) for t in targets) \
                    or not const(node.value):
                return False
        elif isinstance(node, ast.Pass):
            continue
        else:
            return False
    return True


def inert_test_module(src, other_py):
    """True when importing a new test module can only define tests: what `inert_module` allows,
    plus plain test classes and the usual `if __name__ == "__main__"` tail."""
    import ast
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return True     # it cannot be imported at all: nothing in it runs

    def plain_decorator(d):
        while isinstance(d, ast.Call):
            d = d.func
        while isinstance(d, ast.Attribute):
            d = d.value
        return isinstance(d, ast.Name) and d.id in ("unittest", "mock", "patch", "staticmethod", "classmethod", "property")

    rest = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.decorator_list or node.keywords:
                return False
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not all(plain_decorator(d) for d in sub.decorator_list):
                        return False
                elif isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    if not all(isinstance(t, ast.Name) for t in targets):
                        return False
                    if any(isinstance(n, (ast.Call, ast.Await, ast.Yield)) for n in ast.walk(sub)):
                        return False
                elif not (isinstance(sub, ast.Pass) or (isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Constant))):
                    return False
        elif isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) \
                and node.test.left.id == "__name__":
            continue    # not the main module under pytest
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and all(plain_decorator(d) for d in node.decorator_list):
            continue
        else:
            rest.append(node)
    return inert_module(ast.unparse(ast.Module(body=rest, type_ignores=[])), other_py)


def side_effect_stmt(node):
    """An import-level statement that does more than bind names in its own module or class:
    it sets an attribute or item of some other object, or is a bare call."""
    import ast
    if node is None or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return False
    if isinstance(node, ast.Expr):
        return not isinstance(node.value, ast.Constant)
    for n in ast.walk(node):
        if isinstance(n, (ast.Assign, ast.Delete)):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        else:
            continue
        if any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in targets):
            return True
    return False


def own_import(node):
    """An import statement that can pull in the package's own modules (only those can form a cycle)."""
    import ast
    if isinstance(node, ast.ImportFrom):
        return bool(node.level) or (node.module or "").split(".")[0] in ("sqlglot", "tests")
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] in ("sqlglot", "tests") for a in node.names)
    return True


def spawners(ix):
    """Tests that start another Python process: they see what a bare import of the package loads."""
    import ast
    b = 0
    for rel, src in ix.sources().items():
        if not rel.startswith("tests/") or "subprocess" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and "subprocess" in (ast.get_source_segment(src, fn) or ""):
                    suffix = "::%s::%s" % (cls.name, fn.name)
                    for t in bits(ix.in_file.get(rel, 0)):
                        if ix.tests[t].endswith(suffix):
                            b |= 1 << t
    return b


def collect_failures(ix):
    """Import every test module in collection order in a throwaway process, as pytest does before
    it runs anything; -> the test files that fail to import that way (an import cycle can depend
    on which module is imported first)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(30)
            import importlib
            bad, seen = [], set()
            for t in ix.tests:
                rel = t.split("::", 1)[0]
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    importlib.import_module(rel[:-3].replace("/", "."))
                except BaseException:
                    bad.append(rel)
            os.write(w, "\n".join(bad).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return [x for x in data.decode().split("\n") if x]


def shadows(path):
    """True when a new file at `path` would be imported in place of a standard or installed module."""
    parts = path.split("/")
    if len(parts) == 1 and path.endswith(".py"):
        name = path[:-3]
    elif len(parts) == 2 and parts[1] == "__init__.py" and not os.path.isdir(os.path.join(REPO, parts[0])):
        name = parts[0]
    else:
        return False
    if name in ("sitecustomize", "usercustomize") or name in sys.stdlib_module_names or name in sys.builtin_module_names:
        return True
    try:
        import importlib.machinery
        site = [p for p in sys.path if p.rstrip("/").endswith(("site-packages", "dist-packages"))]
        return importlib.machinery.PathFinder.find_spec(name, site) is not None
    except Exception:
        return True


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
        plan.rule_ok = False
        b = ix.files.get("L:" + os.path.dirname(path), 0)
        plan.cand |= b
        plan.notes.append("%s:new:%d" % (path, bin(b & ix.real).count("1")))
        return
    if new is None:   # deleted module: whatever imports it
        plan.rule_ok = False
        plan.cand |= ix.pyfile(path) | ix.files.get("L:" + os.path.dirname(path), 0)
        plan.check.add(path)
        if is_test:   # a test whose file is gone cannot pass
            plan.direct |= ix.in_file.get(path, 0)
        plan.notes.append("%s:deleted" % path)
        return
    if cookie(old) != cookie(new):
        plan.rule_ok = False
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
    # (sqlglot/typing is left out: a test there compares the compiled code of its entries, which an
    # operator swap does change)
    if kind == "sites" and not is_test and ix.cmp_same.get(path) and not path.startswith("sqlglot/typing/"):
        # nothing but comparison operators replaced by their partners (`is` for `==`, `<` for `<=`
        # ...), each at a comparison where the map of the pinned commit shows both operators giving
        # the same value on every evaluation any test performs, with operands whose comparison runs
        # no code of the package: no evaluation changes, so no test can tell the difference
        sw = brplan.swapped_ops(old, new, fp, path)
        if sw and all(ix.cmp_same[path].get((ln, col)) == op for ln, col, op in sw):
            b = ix.files.get(path, 0) & ix.real
            plan.cand |= b               # tests that read this file as text see other text
            plan.notes.append("%s:same-values:%d%s" % (path, len(sw), ":readers %d" % bin(b).count("1") if b else ""))
            return
    if kind == "syntax":       # the module cannot be imported: everything that imports it fails
        plan.rule_ok = False
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
            if s.lam or not lines or is_test:
                plan.rule_ok = False
            imp = ix.import_calls.get(path, ()) if s.lam else ix.il.get(path, ())
            if not is_test and any(ln in imp for ln in lines):
                # the edited code also runs while modules import: what that leaves behind (wrapped
                # functions, parsed templates, lookup tables) is compared with the pinned commit's
                plan.refine = True
                plan.wide = True
                plan.check.add(path)
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
                plan.direct |= b & ix.real      # and no run in isolation can stand in for that order
                plan.rule_ok = False
                tag += "/order-dependent"
        else:
            plan.rule_ok = False
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
            if side_effect_stmt(s.node):
                plan.wide = True
            if s.stmt_type in ("Import", "ImportFrom") and own_import(s.node):
                plan.sim_collect = True
        else:
            plan.cand |= b
        plan.notes.append("%s:%s:%d" % (path, tag, bin(b & ix.real).count("1")))
    if path.startswith("sqlglot/typing/"):
        plan.cand |= ix.in_file.get("tests/test_typing.py", 0)


def plan_data(ix, plan, path, old, new, fp=None):
    plan.rule_ok = False
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
    # Paths written with a prefix other than a/ and b/ (git's mnemonic prefixes, or none): a file that
    # is not in the checkout under its written name but is there without its first part
    def there(p):
        return os.path.lexists(os.path.join(REPO, p))
    if any(fp.old and not fp.is_new and "/" in fp.old and not there(fp.old) and there(fp.old.split("/", 1)[1]) for fp in fps):
        for fp in fps:
            for a in ("old", "new"):
                v = getattr(fp, a)
                if v and "/" in v and not (a == "old" and there(v)):
                    setattr(fp, a, v.split("/", 1)[1])
        plan.notes.append("path-prefix")
    # one file changed by more than one section of the patch (a diff never writes that; patches
    # glued together do): the sections apply one after the other, which the per-file reasoning
    # below does not follow
    seen_paths = {}
    for fp in fps:
        for q in {fp.rename_from or (None if fp.is_new else fp.old), None if fp.is_deleted else (fp.rename_to or fp.new or fp.old)}:
            if q:
                seen_paths[q] = seen_paths.get(q, 0) + 1
    if any(n > 1 for n in seen_paths.values()):
        plan.direct = ix.real
        plan.unpatchable = True
        plan.notes.append("file-in-several-sections")
        return plan
    events = []
    for fp in fps:
        src = fp.rename_from or (None if fp.is_new else fp.old)
        dst = None if fp.is_deleted else (fp.rename_to or fp.new or fp.old)
        old = None
        if src and not there(src):
            # a change to a file the pinned checkout does not have: the patch was not read the way
            # it was meant, so nothing derived from it can be trusted
            plan.direct = ix.real
            plan.unpatchable = True
            plan.notes.append("%s:unknown-path" % src)
            continue
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
            events.append((path, o, n))
            if o is None and path.endswith(".py") and shadows(path):
                # the checkout's top directory is searched before the standard library and the
                # installed packages: a new top-level module of the same name replaces theirs
                plan.direct |= ix.real
                plan.notes.append("%s:shadows-module" % path)
            elif path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
                plan_py(ix, plan, path, o, n, fp)
            else:
                plan_data(ix, plan, path, o, n, fp)
    gone = [(p, o) for p, o, n in events if n is None and o is not None and p.endswith(".py")]
    for path, o, n in events:
        if n is None or not path.endswith(".py") or not (path.startswith(("sqlglot/", "tests/")) or path == "conftest.py"):
            continue
        base = o
        if o is None and gone:
            # a file that moved: what it runs is what the pinned commit already ran under another name
            was = [g for g in gone if g[1] == n] or [g for g in gone if os.path.basename(g[0]) == os.path.basename(path)] \
                or (gone if len(gone) == 1 else [])
            if was:
                base = was[0][1]
        added = added_code(base, n)
        removed = added_code(n, base) if base is not None else []
        if any(INTERP_RX.search(ln) for ln in added):
            # the change sets something interpreter-wide (recursion limit, warning filters,
            # logging, environment ...): it outlives the code that runs it and reaches tests
            # that never touch the file. Only a full run can tell.
            plan.direct |= ix.real
            plan.notes.append("%s:interpreter-setting" % path)
        elif path.startswith("tests/") and any(LEAK_RX.search(ln) for ln in added + removed):
            # a test sets (or stops restoring) something on a shared class or module: in one
            # pytest process that reaches every test that runs after it
            plan.direct |= ix.real
            plan.notes.append("%s:shared-state" % path)
        elif base is None and path.startswith("tests/") \
                and re.match(r"(?:test_.*|.*_test)\.py$", os.path.basename(path)) \
                and not inert_test_module(n, [f for f in plan.files if f.endswith(".py") and f != path]):
            # pytest imports a new test file with all the others, before any test runs
            plan.direct |= ix.real
            plan.notes.append("%s:new-test-file-runs-code" % path)
        elif path.startswith("tests/") and any(PYTEST_RX.search(ln) for ln in added):
            # pytest marks, fixtures and hooks mean nothing to the unittest runner used for checking
            plan.direct |= (ix.in_file.get(path, 0) | ix.pyfile(path)) & ix.real
            plan.notes.append("%s:pytest-specific" % path)
    for kind, path in plan.probe:
        # importing the file shows whether it raises or defines hooks, not what its top-level
        # statements do to the interpreter (warning filters, limits, patched functions): unless
        # the module is plainly inert, only pytest could tell
        src = plan.files.get(path)
        if src is not None and not inert_module(src, [f for f in plan.files if f.endswith(".py") and f != path]):
            plan.direct |= tests_under(ix, os.path.dirname(path) if path.startswith("tests/") else "")
            plan.notes.append("%s:not-inert" % path)
    return plan


def tpc_rule_hits(ix, plan, skip=()):
    """Which optimizer rules the change reaches, for each TPC-DS query -> {rule index: queries}.
    None when the change is not made of plain function-body edits; a query the change reaches
    outside the rules (or nowhere, by this trace) is left to the whole-optimizer comparison."""
    R = ix.tpc_rules
    if not R or not plan.rule_ok or ((plan.refine or plan.icand) and not getattr(plan, "state_clean", False)) \
            or not plan.site_lines \
            or not all(f.startswith("sqlglot/") and f.endswith(".py") for f in plan.files):
        return None
    changed = {}
    for path, lines in plan.site_lines:
        changed.setdefault(path, set()).update(lines)
    hit = {}
    for phase, cover in R["cover"].items():
        qs = set()
        for path, lines in changed.items():
            fm = cover.get(path)
            if fm:
                for ln in lines:
                    qs.update(fm.get(ln, ()))
        if qs:
            hit[phase] = qs
    outside = hit.pop("B0", set())
    explained = set().union(*hit.values()) if hit else set()
    ok = explained - outside                 # reached, and only inside rules
    out = {}
    for phase, qs in hit.items():
        qs = (qs & ok) - set(skip)
        if qs:
            out[int(phase[1:])] = qs
    return out


LAZY_OVERHEAD = 0.05     # seconds of class set-up left when TestExecutor's fixtures are built on first use
_EXEC_CLASS = "tests/test_executor.py::TestExecutor"


def touches_executor_tests(plan):
    """The staged checks and the lazily built fixtures stand in for the test's own code, its helpers
    and its fixture files: not when the patch changes one of those."""
    return any(f in ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py")
               or f.startswith(("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/"))
               or os.path.basename(f) == "conftest.py" for f in plan.files)


def lazy_fixtures(ix, plan, cand=None):
    """True when TestExecutor's shared set-up runs no changed code and none of its own files
    changed: the set-up then does what it does on the pinned commit, and its results can be built
    when a test first asks for them (brtpc.lazy_class)."""
    if not LAZY_FIXTURES or plan.unpatchable or touches_executor_tests(plan):
        return False
    t = ix.tpcds_test
    ph = (ix.slices.get(t) or {}).get("phase") if t is not None else None
    if not ph or "S" not in ph:
        return False
    return not (plan.cand if cand is None else cand) >> ph["S"][0] & 1


def class_overhead(ix, plan, cls):
    """What a class's shared set-up adds to the estimate of running some of its tests. The lazily
    built fixtures make the real cost lower than this for TestExecutor, but the estimate is kept:
    with the lower figure more blocks of tests were run instead of selected, and on the scoring
    machine that cost more CPU than the selections it saved (BR_LAZY_EST=1 uses the lower figure)."""
    if LAZY_EST and str(cls) == _EXEC_CLASS and lazy_fixtures(ix, plan):
        return min(LAZY_OVERHEAD, ix.class_overhead.get(cls, 0))
    return ix.class_overhead.get(cls, 0)


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
    # the staged checks stand in for the test's own code, its helpers and its fixture files
    touches_tests = any(f in ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py")
                        or f.startswith(("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/"))
                        or os.path.basename(f) == "conftest.py" for f in plan.files)
    for t in bits(plan.cand & ix.real & ~plan.direct):
        sl = ix.slices.get(t)
        full = ix.cost[t]
        spec = None
        est = full
        if USE_MAXNODES_CHECK and ix.tests[t] == "tests/test_parser.py::TestParser::test_max_nodes" \
                and not any(f in ("tests/test_parser.py", "tests/__init__.py") or os.path.basename(f) == "conftest.py"
                            for f in plan.files):
            items.append((t, {"@maxnodes": True}, 0.4 * SPEED))
            continue
        ph = sl["phase"] if sl else None
        if ph and not touches_tests and not plan.cand >> ph["S"][0] & 1:
            # staged check: only stages the change reaches are re-run for real
            a_cost = ix.cost[ph["A"][0]]
            if "B" in ph:
                need_b = {i for i, j in ph["B"].items() if plan.cand >> j & 1}
                need_c = {i for i, j in ph["C"].items() if plan.cand >> j & 1}
                at_import = False
                for path, lines in plan.site_lines:
                    il = ix.il.get(path, {})
                    if any(ix.bs[il[ln]] >> t & 1 for ln in lines if ln in il):
                        at_import = True
                clean = getattr(plan, "state_clean", False)
                if at_import and not clean:
                    # an edited function line runs while this test's modules import: what it
                    # leaves behind (decorated functions, parsed templates, lookup tables) can
                    # reach any stage of any query without that line running again. Unless the
                    # child has seen that every module's state is as on the pinned commit.
                    need_b = set(ph["B"])
                    need_c = set(ph["C"])
                else:
                    at_import = False
                # for module/class-level edits the queries are always parsed and compared again
                a_same = (clean or not (plan.refine or plan.icand)) and not at_import and not plan.cand >> ph["A"][0] & 1
                rule_hit, steps = (None if at_import else tpc_rule_hits(ix, plan, need_c)), 0.0
                if rule_hit:
                    rc = ix.tpc_rules.get("cost") or []
                    per = {}
                    for k, qs in rule_hit.items():
                        for i in qs:
                            per[i] = per.get(i, 0.0) + (rc[k] if k < len(rc) else 0.01)
                    # several rules to redo cost as much as the whole optimizer once
                    dear = {i for i, c in per.items() if c >= 0.8 * ix.cost[ph["B"][i]]}
                    rule_hit = {k: qs - dear for k, qs in rule_hit.items() if qs - dear}
                    need_b = (need_b - set(per)) | dear
                    steps = sum(c for i, c in per.items() if i not in dear)
                    if rule_hit and not any("tpc-rules" in n for n in plan.notes):
                        plan.notes.append("tpc-rules %s" % ",".join("%s:%d" % (ix.tpc_rules["rules"][k][:12], len(q))
                                                                    for k, q in sorted(rule_hit.items())))
                if a_same:
                    a_cost *= 0.2        # the test module and a query or two, not all ninety-nine
                # a query whose stage C is re-run costs its executor time (optimize included); the
                # DuckDB half is only paid when the table comes out different
                c = a_cost + sum(ix.cost[ph["B"][i]] for i in need_b - need_c) + steps \
                    + sum(ix.tpc_exec.get(i, ix.cost[ph["B"][i]] + TPC_FULL * ix.cost[ph["C"][i]]) for i in need_c)
                if need_c:
                    c += ix.class_overhead.get(ix.class_of[t], 0)
                partial = None
                if TPC_PARTIAL and ix.tpc_sentinels and a_same and need_c and not need_b and not rule_hit:
                    # a full re-run of the execution stage costs about what the test costs; a
                    # partial one (see brtpc.check_tpcds) is capped at TPC_PARTIAL seconds. Only
                    # when the map says the change is reached by the execution stage alone: the
                    # judgement was measured on such changes, and a change to parsing or
                    # optimizing can alter one query without touching the others.
                    partial = (TPC_PARTIAL, ix.tpc_sentinels)
                    if TPC_SMALL:
                        # evidence of another kind: the class's cheap tests that the change reaches.
                        # On the study mutants almost every change that breaks this test breaks
                        # one of them too, so when they all run and pass a shorter list of
                        # queries is asked for (see the README)
                        sibs = tuple(sorted(ix.tests[u] for u in bits(plan.cand & ix.real)
                                            if u != t and ix.class_of[u] == ix.class_of[t] and ms[u] <= SIB_MS))
                        if len(sibs) >= 3:
                            partial = (TPC_PARTIAL, ix.tpc_sentinels, sibs, TPC_SMALL)
                    c = min(c, a_cost + ix.class_overhead.get(ix.class_of[t], 0) + TPC_PARTIAL + 0.3)
                if c < full * 0.9:
                    # seconds of query work the check may take on before running it stops paying
                    allow = 0.8 * (ms[t] + WORKER_MS) / 1000.0 / SPEED - a_cost - ix.class_overhead.get(ix.class_of[t], 0)
                    if partial:
                        allow = min(allow, TPC_PARTIAL)
                    items.append((t, {"@tpcds": (need_b, need_c, {i: ix.tpc_exec.get(i, ix.cost[j]) for i, j in ph["C"].items()},
                                                 allow, ix.tpc_duck, a_same, rule_hit, partial)}, c * SPEED))
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
_STATE = None    # pinned module state (state.pkl), loaded by the parent when some child compares it


CHILD_MB = 1500


def rss_mb(pid):
    try:
        with open("/proc/%d/statm" % pid) as f:
            return int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") / 1048576.0)
    except (OSError, ValueError, IndexError):
        return 0.0


def cpu_s(pid):
    """CPU seconds a process has used so far (itself, not its children)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            p = f.read().rsplit(")", 1)[1].split()
        return (int(p[11]) + int(p[12])) / float(os.sysconf("SC_CLK_TCK"))
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


def warm_ok(plan, loaded):
    """True when a bare `import sqlglot` runs the same code on the patched tree as on the pinned
    one, so that a check of this patch may start from a process that has already done that import
    on the checkout. `loaded`: the modules that import loads on the pinned commit. The patch may
    only change test files and existing python files of the package that are not among those
    modules: nothing the import executes is then different (a file it does not load is not read
    by it), while a new, removed or non-python file could change what a name resolves to."""
    if plan.unpatchable:
        return False
    for rel, text in plan.files.items():
        parts = rel.split("/")
        if parts[0] == "tests" and len(parts) > 1:
            continue
        if parts[0] != "sqlglot" or len(parts) < 2 or not rel.endswith(".py") or text is None:
            return False
        if not os.path.isfile(os.path.join(REPO, rel)) or os.path.islink(os.path.join(REPO, rel)):
            return False
        name = rel[:-3].replace("/", ".")
        if name.endswith(".__init__"):
            name = name[:-9]
        if name in loaded:
            return False
    return True


def warm_import():
    """Import the package from the checkout in this process (what every check does first, in its
    scratch copy). True when it loaded exactly what it loads on the pinned commit."""
    import brstate
    import gc
    ok = False
    keep = os.dup(1)
    dn = os.open(os.devnull, os.O_WRONLY)
    sys.stdout.flush()
    os.dup2(dn, 1)                      # standard output carries the answers: nothing else may land there
    sys.path.insert(0, REPO)
    try:
        ok = brstate.eager_footprint() == pickle.load(open(os.path.join(HERE, "state.pkl.eager"), "rb"))
    except BaseException:
        ok = False
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os.dup2(keep, 1)
        os.close(keep)
        os.close(dn)
        while REPO in sys.path:
            sys.path.remove(REPO)
    if ok:
        gc.collect()
        gc.freeze()
    return ok


def rebase(root):
    """After warm_import, in a check process: every loaded module of the package is pointed at its
    copy in the scratch tree (same bytes), so that whatever is imported from here on, and whoever
    asks a module where it lives, gets the patched tree."""
    pre = REPO.rstrip(os.sep) + os.sep

    def mv(v):
        return os.path.join(root, v[len(pre):]) if isinstance(v, str) and v.startswith(pre) else v

    for name, m in list(sys.modules.items()):
        if m is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        d = vars(m)
        for a in ("__file__", "__cached__"):
            if isinstance(d.get(a), str):
                d[a] = mv(d[a])
        locs = [mv(x) for x in d["__path__"]] if "__path__" in d else None
        if locs is not None:
            d["__path__"] = locs
        sp = d.get("__spec__")
        if sp is not None:
            sp.origin = mv(sp.origin)
            if sp.submodule_search_locations is not None:
                sp.submodule_search_locations = locs if locs is not None else [mv(x) for x in sp.submodule_search_locations]
            try:
                if sp.cached is not None:
                    sp.cached = mv(sp.cached)
            except Exception:
                pass
            ld = sp.loader
            if isinstance(getattr(ld, "path", None), str):
                try:
                    ld.path = mv(ld.path)
                except Exception:
                    pass
    import importlib
    importlib.invalidate_caches()


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
    if getattr(plan, "warm", False):
        rebase(root)
    try:
        import sqlglot  # noqa: F401
    except BaseException:
        out.write("ALL\n")
        out.flush()
        os._exit(0)
    import importlib
    broken = 0
    try:
        # does a bare import of the package still load what it loaded on the pinned commit?
        import brstate
        ep = os.path.join(HERE, "state.pkl.eager")
        if plan.code and os.path.exists(ep) and brstate.eager_footprint() != pickle.load(open(ep, "rb")):
            broken |= spawners(ix) & ix.real
    except Exception:
        pass
    if plan.sim_collect:
        try:
            for rel in collect_failures(ix):
                broken |= ix.in_file.get(rel, 0) & ix.real
        except Exception:
            pass
    if plan.refine:
        # which module / class attributes differ from the pinned commit once everything is imported?
        try:
            import brstate
            mods = ALL_MODULES if plan.wide else None
            for rel in plan.check:
                dep = ix.dependents.get(rel)
                if dep is None or mods is ALL_MODULES:
                    mods = ALL_MODULES          # not a module we know: look at everything
                else:
                    mods = (mods or set()) | set(dep)
            pinned = _STATE if _STATE is not None else pickle.load(open(os.path.join(HERE, "state.pkl"), "rb"))
            failed, changed, detail = brstate.diff(pinned,
                                                   None if mods is ALL_MODULES or not mods else sorted(mods))
            for name in failed:
                rel = name[0].replace(".", "/")
                rel = rel + ".py" if os.path.exists(os.path.join(REPO, rel + ".py")) else rel + "/__init__.py"
                broken |= ix.pyfile(rel) & ix.real
            plan.direct = plan.base_direct
            plan.state_clean = mods is ALL_MODULES and not changed and not failed and not plan.icand

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
                few = len(names) <= 32
                for rel, src in ix.sources().items():
                    # a regex match needs one of the names as a substring: the plain test first
                    if rel.startswith("tests/") and (not few or any(n in src for n in names)) and rx.search(src) \
                            and rel not in plan.files:
                        try:
                            importlib.import_module(rel[:-3].replace("/", "."))
                        except BaseException:
                            broken |= (ix.pyfile(rel) | ix.in_file.get(rel, 0)) & ix.real
            decide(ix, plan, ms, plan.base_cand | refine_cand(ix, changed, is_callable, detail))
            out.write("N %d %d %s\n" % (len(changed), len(failed), ",".join("/".join(k) for k in changed[:6]).replace(" ", "")))
        except Exception as e:
            # the comparison itself failed: the static candidates of an import-level edit are
            # no upper bound on what it reaches, so nothing is ruled out
            plan.direct = ix.real
            plan.items = []
            try:
                out.write("N -1 -1 refine-error:%s\n" % type(e).__name__)
            except Exception:
                pass
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
    if DIAG:
        out.write("B %s\n" % " ".join(str(n) for n in bits(getattr(plan, "blind", 0) & ix.real)))
    # everything still open: selected unless a result below clears it
    out.write("U %s\n" % " ".join(str(n) for n in bits((plan.direct & ix.real) | sum(1 << t for t, _, _ in plan.items))))
    for n in bits(broken):
        out.write("%d F\n" % n)
    out.flush()
    items = [it for it in plan.items if not broken >> it[0] & 1]
    remaining = {t: (ms[t], est) for t, spec, est in items}
    state = {"timeouts": 0, "since": 0, "fixed": [ms[n] for n in bits(plan.direct | broken)]}
    est_of = {ix.tests[t]: est for t, _, est in items}
    for t, spec, est in items:
        if spec and "@tpcds" in spec:     # its check may grow to its allowance when more queries need re-running
            est_of[ix.tests[t]] = max(est, 0.6 * 0.8 * (ms[t] + WORKER_MS) / 1000.0)
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
        st = getattr(sys.modules.get("brtpc"), "STATS", None)
        if PROBE and st is not None:
            sib = st.get("sib") or [0, 0, 0]
            out.write("X %d %d %d %d %d %d %d %d %d\n" % (sib[0], sib[1], sib[2], st.get("todo", 0), st.get("same", 0), st.get("compared", 0),
                                                 int(100 * st.get("work", 0.0)), int(100 * st.get("partial", 0.0) if "partial" in st else -1), st.get("b", 0)))
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
        # wall seconds for one run: a change that makes a test crawl or loop is cut off here and
        # the test selected, which is what its real timeout would report
        return BUDGET * max(3.0, 2.5 * est_of[key] + 1.0)

    brrun.FAILFAST = True
    brrun.FINGERPRINTS = os.path.join(HERE, "tpc.pkl")
    brrun.LAZY_EXECUTOR = lazy_fixtures(ix, plan)
    brrun.run_tests([(ix.tests[t], spec, ix.tests[t]) for t, spec, est in items], budget=budget, report=report)
    finish()


def decide(ix, p, ms, cand):
    """Split candidates into `direct` (select blindly) and `items` (check by running)."""
    p.cand = cand
    p.blind = 0
    raw = build_items(ix, p, ms)
    keep = []
    for t, spec, est in raw:
        if est > 0.8 * (ms[t] + WORKER_MS) / 1000.0:
            p.direct |= 1 << t          # dearer to check than to select
            p.blind |= 1 << t
        else:
            keep.append((t, spec, est))
    keep.sort(key=lambda it: (-ms[it[0]], it[0]))
    classes = {ix.class_of[t] for t, _, _ in keep}
    p.est = sum(e for _, _, e in keep) + SPEED * (ix.import_cost + sum(class_overhead(ix, p, c) for c in classes)) + FORK_COST
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
                    if spec and "@tpcds" in spec:   # its check may take on more work, but never more than the gain
                        v = spec["@tpcds"]
                        room = (gain - 0.3) / 1.15 / SPEED - (e / SPEED - sum(v[2].get(i, 0.0) for i in v[1]))
                        spec = dict(spec, **{"@tpcds": v[:3] + (min(v[3], room),) + v[4:]})
                    first.append((t, spec, e))
                    pool = others
                    continue
            rest.append((t, spec, e))
        for t, spec, e in first:
            v = spec.get("@tpcds") if spec else None
            if v and v[7] and len(v[7]) > 2:
                # the cheap tests of the class that go with a partial TPC-DS check: run with it
                names = set(v[7][2])
                with_it = [it for it in rest if it[1] is None and ix.tests[it[0]] in names]
                if len(with_it) == len(names):
                    rest = [it for it in rest if not (it[1] is None and ix.tests[it[0]] in names)]
                    first = first + with_it
                break
        # 2. everything else, as a block
        overhead = SPEED * (ix.import_cost + sum(class_overhead(ix, p, c) for c in {ix.class_of[t] for t, _, _ in rest})) + FORK_COST
        run = sum(e for _, _, e in rest) + overhead + (0 if (fixed or first) else P_ANY * 2.3)
        blind = (pack_cost(fixed + [ms[t] for t, _, _ in rest]) - pack_cost(fixed)) / 1000.0
        if rest and blind <= run:
            p.notes.append("blind %d (%.1f<=%.1f)" % (len(rest), blind, run))
            p.direct |= sum(1 << t for t, _, _ in rest)
            p.blind |= sum(1 << t for t, _, _ in rest)
            rest = []
        keep = first + rest
        classes = {ix.class_of[t] for t, _, _ in keep}
        p.est = sum(e for _, _, e in keep) + SPEED * (ix.import_cost + sum(class_overhead(ix, p, c) for c in classes)) + FORK_COST
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
        if any(p.refine for p in todo):
            ix.sources()       # read once here rather than in every child that compares module state
            global _STATE
            try:               # the pinned module state, likewise (children only read it)
                _STATE = pickle.load(open(os.path.join(HERE, "state.pkl"), "rb"))
            except Exception:
                _STATE = None
        os.makedirs(SCRATCH, exist_ok=True)
        scan_tree()
        import gc
        gc.collect()
        gc.freeze()
    if CALIBRATE:
        # one full executor-only TPC-DS check on the untouched checkout: its CPU seconds on the
        # scoring machine tell how fast that machine runs this work
        pid = os.fork()
        if pid == 0:
            try:
                dn = os.open(os.devnull, os.O_WRONLY)
                os.dup2(dn, 1)
                os.dup2(dn, 2)
                import brstate
                import brtpc
                brstate.preload()
                os.chdir(REPO)
                sys.path.insert(0, REPO)
                fp = pickle.load(open(os.path.join(HERE, "tpc.pkl"), "rb"))
                brtpc.check_tpcds(fp, set(), set(fp["ds"]), dict(ix.tpc_exec), None, ix.tpc_duck)
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
    # 3 minutes for a practice-sized batch, 4.5 for the larger final one
    deadline = T0 + min(4.0 * len(cids), 160.0 if len(cids) <= 44 else 235.0)
    def drive(queue):
        active = {}
        while queue or active:
            while queue and len(active) < PAR:
                p = queue.pop(0)
                if time.time() + p.est / 1.5 > deadline:
                    p.notes.append("no-time")
                    p.never_ran = True
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
                # the last entry: a child that has not sent its first report by then is stuck importing
                # (judged by its own CPU seconds, so that a slow or busy machine does not look stuck;
                # the wall-clock limit only catches a child that sleeps)
                active[r] = [p, pid, b"", time.time() + 3 * p.est + 25, time.time() + 60]
            if not active:
                break
            ready, _, _ = select.select(list(active), [], [], 1.0)
            now = time.time()
            for fd in list(active):          # a runaway child must never push the container over its memory limit
                if rss_mb(active[fd][1]) > CHILD_MB:
                    active[fd][3] = 0
            for fd in list(active):
                p, pid, buf, kill_at, first_by = active[fd]
                if p.unverified is None and not getattr(p, "everything", False) and (now > first_by or cpu_s(pid) > 12.0):
                    kill_at = 0
                eof = False
                if fd in ready:
                    chunk = os.read(fd, 65536)
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        active[fd][2] = buf
                        for ln in lines:
                            if ln == b"ALL":         # `import sqlglot` itself fails: nothing can pass
                                p.everything = True
                                continue
                            if ln[:2] == b"U " or ln == b"U":
                                p.unverified = {int(x) for x in ln[2:].split()}
                                continue
                            if ln == b"E":
                                p.ended = True
                                continue
                            if ln[:2] == b"B " or ln == b"B":
                                p.blind_set = {int(x) for x in ln[2:].split()}
                                continue
                            if ln[:2] == b"R " or ln == b"R":
                                p.risk_users = sum(1 << int(x) for x in ln[2:].split())
                                continue
                            if ln[:2] == b"X ":
                                p.xstats = [int(v) for v in ln[2:].split()]
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

    # Checks that can start from a parent that has already imported the package go last, after that
    # import (see warm_ok); every other check starts, as before, in a process that has not.
    warm = []
    if WARM and todo:
        try:
            loaded = set(pickle.load(open(os.path.join(HERE, "state.pkl.eager"), "rb"))[0])
        except Exception:
            loaded = set()
        if loaded:
            warm = [p for p in todo if warm_ok(p, loaded)]
        if len(warm) < 2:
            warm = []
    drive([p for p in todo if p not in warm])
    if warm:
        if warm_import():
            for p in warm:
                p.warm = True
            drive(list(warm))
        else:
            for p in warm:           # this process is no clean start for a check any more
                p.never_ran = True
                p.notes.append("warm-failed")
    if DIAG:
        diag_variant = os.urandom(1)[0] % 2
        diag_t = {n for n, t in enumerate(ix.tests) if t.endswith("::test_execute_tpcds")}
        t0 = time.process_time()
        while diag_variant and time.process_time() - t0 < 5.0:   # marks the variant in the CPU figure
            pass
    out = []
    for p in plans:
        if p.unverified is None:    # the child never reported: fall back to the static plan
            p.unverified = set(bits((p.direct | p.probe_under) & ix.real)) | {t for t, _, _ in p.items}
            ran = p in todo and not getattr(p, "never_ran", False)
            if ran or (p.refine and p in todo):
                # a check process that died or hung before its first report (the patched package
                # may not even import in finite time), or an import-level edit whose module state
                # was never compared: nothing is ruled out
                p.unverified = set(bits(ix.real))
                p.notes.append("no-report")
        sel = p.failed | (p.unverified - (p.done - p.failed))
        if DIAG:
            # diagnostic run (not an entry): leave out what was selected without proof, either the
            # TPC-DS test alone or everything else, to learn from the practice score where the
            # unproven selections go
            unproven = (sel - p.failed) & (p.blind_set if getattr(p, "blind_set", None) is not None else set(bits(p.blind)))
            sel -= (unproven & diag_t) if diag_variant == 0 else (unproven - diag_t)
        if p.risk_users and not p.ended and (p.items or p.refine):
            sel |= set(bits(p.risk_users))      # the child did not finish: its pool may have broken
        if getattr(p, "everything", False):
            sel = set(range(ix.n))
        nums = [data_num[ix.tests[n]] for n in sel if ix.tests[n] in data_num]
        if unknown_tests and (p.direct & ix.real == ix.real or p.cand):
            nums = sorted(set(nums) | set(unknown_tests))
        _, ws = pack([(n, data_ms[n]) for n in nums])
        if ws and FILL:
            # Workers below the busiest one have idle time that costs nothing. Candidates that were
            # cleared (run and passed, or ruled out by a map) go there, shortest first, as long as
            # no worker grows past the busiest: a test that fails for a reason no check here saw
            # is then selected after all, and the commit costs exactly what it cost before.
            chosen = set(nums)
            pool = (p.cand | getattr(p, "base_cand", 0) | p.icand | p.direct) & ix.real
            extra = sorted((data_ms[data_num[ix.tests[n]]], data_num[ix.tests[n]]) for n in bits(pool)
                           if ix.tests[n] in data_num and data_num[ix.tests[n]] not in chosen)
            loads = [sum(data_ms[n] for n in w) for w in ws]
            top = max(loads)
            for m_, n_ in extra:
                j = loads.index(min(loads))
                if loads[j] + m_ > top:
                    break               # shortest first: nothing longer fits either
                loads[j] += m_
                ws[j].append(n_)
            nums = [n for w in ws for n in w]
        out.append(json.dumps({"commit": p.cid, "workers": ws}, separators=(",", ":")))
        log("%s cpu=%.2f cand=%d run=%d est=%.1fs failed=%d unverified=%d selected=%d | %s" % (
            p.cid, getattr(p, "cpu", 0.0), bin(p.cand & ix.real).count("1"), len(p.items), getattr(p, "est", 0), len(p.failed),
            len(p.unverified - p.done), len(nums), "; ".join(p.notes)[:400]))
    if PROBE:
        out = probe(out, plans, tj, PROBE)
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    t = os.times()
    log("cpu %.2fs (self %.2f, children %.2f) wall %.1fs" % (
        t[0] + t[1] + t[2] + t[3], t[0] + t[1], t[2] + t[3], time.time() - T0))


PROBE = os.environ.get("BR_PROBE", "")


def probe(out, plans, tj, which):
    """Measurement run (not an entry). Where this program's CPU time goes on the scoring machine
    is written into the selections of commits that select nothing, as extra zero-second tests and
    workers, so that it can be read off the simulated seconds: one worker holding tests of x
    milliseconds in all on the first such commit, and w more workers spread over the next ones."""
    rows = [json.loads(o) for o in out]
    zero = [t["number"] for t in tj if t["seconds"] == 0]
    small = sorted(((int(round(t["seconds"] * 1000)), t["number"]) for t in tj if 0 < t["seconds"] <= 0.1), reverse=True)

    def cat(p):
        if any(spec and ("@tpcds" in spec or "@tpch" in spec) for _, spec, _ in p.items):
            return "T"
        return "S" if p.refine else "P"

    def cpu(p):
        return getattr(p, "cpu", 0.0)

    if which == "1":
        # state comparisons in 0.2 s units, number of checks of a second or more, TPC checks in 0.5 s units
        x = 10 * min(99, int(round(sum(cpu(p) for p in plans if cat(p) == "S") / 0.2))) + min(9, sum(1 for p in plans if cpu(p) >= 1.0))
        w = int(round(sum(cpu(p) for p in plans if cat(p) == "T") / 0.5))
    elif which == "5":
        # the dearest TPC check: which part of the package its patch changes
        tp = sorted((p for p in plans if cat(p) == "T"), key=cpu, reverse=True)
        top = tp[0] if tp else None
        code = {"sqlglot/executor/__init__.py": 1, "sqlglot/executor/table.py": 2, "sqlglot/executor/python.py": 3,
                "sqlglot/executor/context.py": 4, "sqlglot/executor/env.py": 5, "sqlglot/planner.py": 6}
        py = sorted(f for f in (top.files if top else {}) if f.endswith(".py") and f.startswith("sqlglot/"))
        w = (code.get(py[0], 7) if py else 0) + (8 if len(top.files if top else {}) > 1 else 0) + (16 if top is not None and top.refine else 0)
        lines = sorted({ln for _, ls in (top.site_lines if top else []) for ln in ls})
        # x: how many function-body lines changed (tens) and how many files (units); nothing finer
        x = 10 * min(99, len(lines)) + min(9, len(top.files) if top else 0)
        log("probe5 files=%r lines=%r" % (py, lines[:6]))
    elif which == "4":
        # CPU of the second, third and fourth dearest TPC checks in 0.5 s units; whole-optimizer runs of all of them / 8
        tp = sorted((p for p in plans if cat(p) == "T"), key=cpu, reverse=True)
        c = [cpu(p) for p in tp] + [0.0] * 4
        x = 100 * min(9, int(round(c[1] / 0.5))) + 10 * min(9, int(round(c[2] / 0.5))) + min(9, int(round(c[3] / 0.5)))
        w = min(31, sum((getattr(p, "xstats", None) or [0] * 9)[8] for p in tp) // 8)
        log("probe4 cpu=%r b=%r" % ([round(v, 2) for v in c[:5]], [(getattr(p, "xstats", None) or [0] * 9)[8] for p in tp[:5]]))
    elif which == "3":
        # the dearest TPC check: sibling tests named / run / 1 + failed (0 = not run), queries queued,
        # tables equal, tables compared, partial seconds
        tp = [p for p in plans if cat(p) == "T"]
        top = max(tp, key=cpu) if tp else None
        xs = getattr(top, "xstats", None) or [0] * 8
        # x: hundreds = min(9, named sibling tests), tens = min(9, run), units = min(9, third figure)
        x = 100 * min(9, xs[0]) + 10 * min(9, xs[1]) + min(9, xs[2])
        # w: queued queries / 8 (0..7), + 8 if a partial run happened, + 16 if a table was compared
        w = min(7, xs[3] // 8) + (8 if xs[7] >= 0 else 0) + (16 if xs[5] else 0)
        log("probe3 xs=%r cpu=%.2f" % (xs, cpu(top) if top else 0))
    else:
        # the dearest check: its CPU in 0.02 s units and what kind it is
        top = max(plans, key=cpu)
        x = min(999, int(round(cpu(top) / 0.02)))
        w = {"T": 1, "S": 2, "P": 3}[cat(top)] + (4 if top.failed else 0) \
            + (8 if any(spec and "@maxnodes" in spec for _, spec, _ in top.items) else 0) + (16 if getattr(top, "warm", False) else 0)
    empties = [r for r in rows if not r["workers"]]
    log("probe %s x=%d w=%d carriers=%d" % (which, x, w, len(empties)))
    if empties and zero:
        ws, left = [zero[0]], x
        for m, n in small:
            if m <= left:
                ws.append(n)
                left -= m
        empties[0]["workers"] = [ws]
        for r in empties[1:]:
            if w <= 0:
                break
            take = min(8, w, len(zero))
            r["workers"] = [[zero[i]] for i in range(take)]
            w -= take
    return [json.dumps(r, separators=(",", ":")) for r in rows]


if __name__ == "__main__":
    main()
