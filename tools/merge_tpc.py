"""Replace the staged TPC items of a full trace with a fresh `trace_iso.py --only-tpc` run.
usage: merge_tpc.py FULL.pkl.gz TPC_ONLY.pkl.gz OUT.pkl.gz"""
import gzip, pickle, sys
full = pickle.load(gzip.open(sys.argv[1], "rb"))
part = pickle.load(gzip.open(sys.argv[2], "rb"))
assert full["keys"] == part["keys"], "item lists differ"
mask = 0
for j, k in enumerate(full["keys"]):
    if "#@" in k:
        mask |= 1 << j
assert all(part["status"][j] == "pass" for j in range(len(part["keys"])) if mask >> j & 1), "a staged item did not trace"
for name in ("rl", "il"):
    for f, lines in full[name].items():
        for ln in list(lines):
            lines[ln] &= ~mask
    for f, lines in part[name].items():
        m = full[name].setdefault(f, {})
        for ln, b in lines.items():
            m[ln] = m.get(ln, 0) | (b & mask)
    for f in list(full[name]):
        full[name][f] = {ln: b for ln, b in full[name][f].items() if b}
for name in ("classes", "files"):
    for k in list(full[name]):
        full[name][k] &= ~mask
    for k, b in part[name].items():
        full[name][k] = full[name].get(k, 0) | (b & mask)
full["mro"].update(part.get("mro", {}))
pickle.dump(full, gzip.open(sys.argv[3], "wb", compresslevel=6), 5)
print("merged", bin(mask).count("1"), "staged items")
