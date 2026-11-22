"""usage: merge_pt.py OUT.json.gz OUTDIR [--suites 0,1] [--index entry/data/iso.pkl.gz] [--tests kit/sample/tests.json]
Builds the per-test operator-swap map from the isolated run (OUTDIR/iso) and the suite runs
(OUTDIR/suite<seed>). A test is affected for (site, alternative) when
  1. either run says so for that test (suite records made outside any test count for all tests);
  2. it is a cache consumer of the site: it evaluates the site when run alone but not in some
     suite run (so there it used a result computed earlier), and any test anywhere saw the
     alternative differ at the site;
  3. the site is in a def that is cached or writes module/class state (instrument_pt.stateful),
     some test of a suite run saw the alternative differ there, and the test executes a line of
     that def (coverage map) or evaluates the site.
Every suite run must have passed completely; isolated tests that did not pass are taken as
affected for every site of every def they execute (coverage map)."""
import array, collections, glob, gzip, json, os, pickle, re, sys

OPS = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot")
ALL = 0xFFFFFFFF
out_path, od = sys.argv[1], sys.argv[2]
opt = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
seeds = [x for x in opt("--suites", "0,1").split(",") if x and x != "none"]
BR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
tj = json.load(open(opt("--tests", os.path.join(BR, "kit/sample/tests.json"))))["tests"]
NT = len(tj)
FULL = (1 << NT) - 1
sites = json.load(open(os.path.join(od, "sites.iso.json")))
for s in seeds:
    assert json.load(open(os.path.join(od, "sites.suite%s.json" % s))) == sites, "different instrumentation"
    r = json.load(open(os.path.join(od, "result.suite%s.json" % s)))
    m = re.search(r"\n(?:=+ )?(\d+) passed, 1 skipped, \d+ subtests passed in [^\n]*\nrc=0\s*$", r["tail"])
    assert m and int(m.group(1)) == 1620 and not r["failed"] and not r["collect_failed"], ("suite run not clean", s, r["tail"][-300:])
NS = len(sites)


def load(d):
    """-> (per site: {mask: test bitset}, per site: mask seen outside any test)"""
    acc = collections.defaultdict(int)
    allm = [0] * NS
    n = 0
    for f in glob.glob(os.path.join(d, "rec.*")) + glob.glob(os.path.join(d, "fallback", "rec.*")):
        a = array.array("I")
        a.frombytes(open(f, "rb").read()[: (os.path.getsize(f) // 12) * 12])
        fb = "/fallback/" in f
        for i in range(0, len(a), 3):
            t, s, m = a[i], a[i + 1], a[i + 2]
            if t == ALL or fb:
                allm[s] |= m
            else:
                acc[(s, m)] |= 1 << t
        n += len(a) // 3
    per = [dict() for _ in range(NS)]
    for (s, m), b in acc.items():
        per[s][m] = per[s].get(m, 0) | b
    return per, allm, n


def bitsof(per_s, bit):
    b = 0
    for m, t in per_s.items():
        if m >> bit & 1:
            b |= t
    return b


iso, iso_all, n_iso = load(os.path.join(od, "iso"))
assert not any(iso_all), "isolated records outside a test"
suites = []
for s in seeds:
    suites.append(load(os.path.join(od, "suite%s" % s)))
print("records iso", n_iso, "suites", [x[2] for x in suites], file=sys.stderr)
status = json.load(open(os.path.join(od, "iso", "status.json")))
bad_iso = {int(n) for n, v in status.items() if v[0] != "pass"}
missing = set(range(NT)) - {int(n) for n in status}
bad_iso |= missing
print("isolated non-pass", sorted(bad_iso)[:30], len(bad_iso), file=sys.stderr)

# coverage map of the pinned commit: tests that execute each line
cov = pickle.load(gzip.open(opt("--index", os.path.join(BR, "entry/data/iso.pkl.gz"))))
keys = cov["keys"]
first = {k: j for j, k in enumerate(keys[:cov["n_tests"]])}
num = {t["id"]: t["number"] for t in tj}
key2test = {}
for j, k in enumerate(keys):
    base = k.split("#", 1)[0] if j >= cov["n_tests"] else k
    key2test[j] = num.get(base)
_tc = {}


def tests_of(b):
    r = _tc.get(b)
    if r is None:
        r = 0
        j = 0
        x = b
        while x:
            if x & 1 and key2test.get(j) is not None:
                r |= 1 << key2test[j]
            x >>= 1
            j += 1
        _tc[b] = r
    return r


def lines_tests(path, a, b):
    r = 0
    for m in ("rl", "il"):
        d = cov[m].get(path, {})
        for ln in range(a, b + 1):
            if ln in d:
                r |= d[ln]
    return tests_of(r)


bad_bits = 0
for t in bad_iso:
    bad_bits |= 1 << t

res = collections.defaultdict(list)
stat = collections.Counter()
popc = lambda x: bin(x).count("1")
for s, (path, ln, col, kind, op, f0, f1, stf, form) in enumerate(sites):
    own = OPS.index(op) if kind == "cmp" else 0
    alts = [j for j in range(8) if j != own] if kind == "cmp" else [1]
    e_iso = bitsof(iso[s], own)
    e_su = [(FULL if allm[s] >> own & 1 else bitsof(per[s], own)) for per, allm, _ in suites]
    e_any = e_iso
    for e in e_su:
        e_any |= e
    consumers = 0
    for e in e_su:
        consumers |= e_iso & ~e
    ftests = lines_tests(path, f0, f1) if f0 else 0
    if bad_bits:
        # an isolated test that did not pass may have stopped before reaching the site
        lt = ftests if f0 else lines_tests(path, ln, ln)
        e_badfill = lt & bad_bits
    else:
        e_badfill = 0
    row = {}
    for j in alts:
        a = bitsof(iso[s], j)
        su_diff = 0
        for per, allm, _ in suites:
            if allm[s] >> j & 1:
                a = FULL
                su_diff = FULL
            else:
                x = bitsof(per[s], j)
                a |= x
                su_diff |= x
        r1 = a
        if a:
            a |= consumers
        r2 = a
        if stf and su_diff and "--no-rule3" not in sys.argv:
            a |= ftests | e_any
        if (e_any or ftests) and e_badfill:
            a |= e_badfill
        a &= FULL
        stat[("rule2", kind)] += popc(r2 & ~r1)
        stat[("rule3", kind)] += popc(a & ~r2)
        stat[("aff", kind)] += popc(a)
        stat[("eval", kind)] += popc(e_any)
        key = OPS[j] if kind == "cmp" else "swap"
        n = popc(a)
        row[key] = sorted(t for t in range(NT) if a >> t & 1) if n <= 40 else "%x" % a
    res[path].append([ln, col, kind, op, row])
    stat[("sites", kind)] += 1
    stat[("never", kind)] += (e_any == 0)
for k in sorted(stat):
    print(k, stat[k], file=sys.stderr)
with gzip.open(out_path, "wt", compresslevel=9) as f:
    json.dump({"tests": NT, "runs": {"iso": 1, "suites": seeds}, "sites": dict(sorted(res.items()))}, f, separators=(",", ":"))
print("wrote", out_path, os.path.getsize(out_path), file=sys.stderr)
