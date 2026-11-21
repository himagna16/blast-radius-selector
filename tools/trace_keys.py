"""Which keys does each test look up in sqlglot's class-level dicts and sets?  (see brtrack.py)
usage: trace_keys.py ROOT TESTS.json OUT.pkl.gz [--workers N]"""
import sys, os, json, gzip, pickle, time
sys.path.insert(0, "/entry"); sys.path.insert(0, "/entry/tools")
root, tj, outp = sys.argv[1:4]
W = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 3
import brstate
brstate.preload()
import brrun, brtrack
tests = [t["id"] for t in json.load(open(tj))["tests"]]

def one(tid):
    os.chdir(root); sys.path.insert(0, root); os.environ["PYTHONPATH"] = root
    dn = os.open(os.devnull, os.O_WRONLY); os.dup2(dn, 1); os.dup2(dn, 2)
    brstate.load_all()
    n = brtrack.install()
    brtrack.ACTIVE[0] = True
    res = brrun.run_tests([tid])
    brtrack.ACTIVE[0] = False
    return {"st": res[tid], "log": brtrack.LOG, "n": n}

def worker(idx, wfd):
    out = os.fdopen(wfd, "wb")
    for j in range(idx, len(tests), W):
        r, w = os.pipe(); pid = os.fork()
        if pid == 0:
            os.close(r)
            try: data = pickle.dumps(one(tests[j]), 5)
            except BaseException as e:
                import traceback
                data = pickle.dumps({"st": "crash %r %s" % (e, traceback.format_exc()[-400:]), "log": set(), "n": 0}, 5)
            with os.fdopen(w, "wb") as f: f.write(data)
            os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as f: data = f.read()
        os.waitpid(pid, 0)
        if not data: data = pickle.dumps({"st": "died", "log": set(), "n": 0}, 5)
        out.write(len(data).to_bytes(4, "little") + j.to_bytes(4, "little") + data); out.flush()
    out.close(); os._exit(0)

pipes = []
for i in range(W):
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(r); worker(i, w)
    os.close(w); pipes.append(os.fdopen(r, "rb"))
import select
look = {}; status = {}; live = list(pipes); done = 0; t0 = time.time(); swapped = 0
while live:
    rd, _, _ = select.select(live, [], [])
    for p in rd:
        hdr = p.read(8)
        if len(hdr) < 8: live.remove(p); continue
        n = int.from_bytes(hdr[:4], "little"); j = int.from_bytes(hdr[4:], "little")
        rec = pickle.loads(p.read(n)); status[tests[j]] = rec["st"]; swapped = max(swapped, rec["n"])
        bit = 1 << j
        for mod, cls, attr, key in rec["log"]:
            d = look.setdefault((mod, cls, attr), {})
            d[key] = d.get(key, 0) | bit
        done += 1
        if done % 300 == 0: print(done, round(time.time() - t0), file=sys.stderr, flush=True)
bad = [(k, v) for k, v in status.items() if v != "pass"]
print("swapped containers", swapped, "| non-pass", len(bad), bad[:12], file=sys.stderr)
print("tracked attributes", len(look), "keys", sum(len(v) for v in look.values()), file=sys.stderr)
pickle.dump({"tests": tests, "lookups": look, "nonpass": [k for k, _ in bad]}, gzip.open(outp, "wb", compresslevel=6), 5)
print("saved", file=sys.stderr)
