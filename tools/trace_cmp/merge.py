"""usage: merge.py OUT.json FLAGS...  -> the comparisons at which an operator and its partner
agreed on every evaluation of every traced run of the suite (or that no run evaluated at all), with
operands whose comparison is known to run no code of the package."""
import collections, json, os, sys
out, flag_files = sys.argv[1], sys.argv[2:]
# flags after --extra come from runs that did not finish cleanly: what they saw can only take a
# comparison OUT of the map, so they are used without the completeness check
extra_files = []
if "--extra" in flag_files:
    k = flag_files.index("--extra")
    flag_files, extra_files = flag_files[:k], flag_files[k + 1:]
# each run has its own numbering of the comparisons (sites.<run>.json next to flags.<run>)
runs = []
for f in flag_files:
    runs.append((f, [tuple(x) for x in json.load(open(os.path.join(os.path.dirname(f), "sites." + os.path.basename(f).split(".", 1)[1] + ".json")))]))
OPS = ("Is", "IsNot", "Lt", "LtE", "Gt", "GtE")
sites = sorted({s for _, ss in runs for s in ss if s[3] in OPS})
for f, ss in runs:
    assert set(sites) <= set(ss), ("a run that did not instrument every site", f)
n_complete = len(runs)
for f in extra_files:
    runs.append((f, [tuple(x) for x in json.load(open(os.path.join(os.path.dirname(f), "sites." + os.path.basename(f).split(".", 1)[1] + ".json")))]))
# every run must have finished cleanly: a comparison only an unfinished test reaches would look harmless
import os
assert len(flag_files) >= 2, "two complete runs at least"
for f in flag_files:
    r = json.load(open(os.path.join(os.path.dirname(f), "result." + os.path.basename(f).split(".", 1)[1] + ".json")))
    # pytest's own last line: every test passed (one is skipped in this image), and pytest exited 0
    import re
    m = re.search(r"\n(?:=+ )?(\d+) passed, 1 skipped, \d+ subtests passed in [^\n]*\nrc=0\s*$", r["tail"])
    assert m and int(m.group(1)) == 1620 and not r["failed"] and not r["collect_failed"] and "Killed" not in r["tail"] \
        and " failed" not in r["tail"].rsplit("\n", 3)[-2], (f, r["n_seen"], r["failed"][:5], r["tail"][-200:])
flags = collections.defaultdict(set)      # site (path, line, column, operator) -> codes seen in any run
for f, ss in runs:
    for line in open(f, "rb"):
        p = line.split()
        if len(p) == 2:
            flags[ss[int(p[0])]].add(int(p[1]))
# the coverage map of the same commit (per-test line coverage): a comparison no traced run evaluated
# is only trusted to be dead for the suite when no test executes its line either
import pickle
cov = pickle.load(open(os.environ["BR_INDEX"], "rb")) if os.environ.get("BR_INDEX") else None


def covered(path, ln):
    for m in ("rl", "il"):
        j = cov[m].get(path, {}).get(ln)
        if j and cov["bits"][j]:
            return True
    return False


same = collections.defaultdict(list)
stat = collections.Counter()
for path, ln, col, op in sites:
    fl = flags.get((path, ln, col, op), set())
    kind = "never" if not fl else ("same" if fl == {0} else ("differs" if 1 in fl else "opaque"))
    if kind == "never" and (cov is None or covered(path, ln)):
        kind = "never-but-covered"
    stat[(op, kind)] += 1
    # `==` -> `is` is left out: whether two equal values are one object is the interpreter's business
    if kind in ("never", "same") and op in ("Is", "IsNot", "Lt", "LtE", "Gt", "GtE"):
        same[path].append([ln, col, op])
json.dump({"runs": len(flag_files), "same": dict(sorted(same.items()))}, open(out, "w"), separators=(",", ":"))
for k, v in sorted(stat.items()):
    print(k, v)
print("sites", len(sites), "kept", sum(len(v) for v in same.values()))
