"""Merge the traced map and the untraced timings into the index the selector loads.
usage: build_index.py iso.pkl.gz cost_iso.pkl.gz out.pkl"""
import gzip, pickle, sys

iso = pickle.load(gzip.open(sys.argv[1], "rb"))
cost = pickle.load(gzip.open(sys.argv[2], "rb"))
assert iso["keys"] == cost["keys"], "trace and timing runs disagree on the item list"
table = [0]
ids = {0: 0}


def share(d):
    """Bitsets are stored once in a table; the maps hold table positions."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = share(v)
        else:
            i = ids.get(v)
            if i is None:
                i = ids[v] = len(table)
                table.append(v)
            out[k] = i
    return out


out = {
    "keys": iso["keys"], "n_tests": iso["n_tests"],
    "rl": share(iso["rl"]), "il": share(iso["il"]), "classes": share(iso["classes"]), "files": share(iso["files"]),
    "bits": table, "cost": cost["cost"], "total": cost["total"],
}
pickle.dump(out, open(sys.argv[3], "wb"), protocol=5)
print("index: %d items, %d files with run-time lines, %d classes, %d distinct bitsets" % (
    len(out["keys"]), len(out["rl"]), len(out["classes"]), len(table)))
