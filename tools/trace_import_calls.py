"""Functions that run while modules are being imported -> data/import_calls.json.

usage (inside the image):  python trace_import_calls.py /app/repo OUT.json

brtrace.py switches PY_START off for a function after its first call. CPython then stops
reporting LINE for the line the function starts on, so for a one-line lambda (or one-line def,
or generator expression) first called during an import, later run-time calls leave no trace.
This lists the entry lines of every function called during any import (all sqlglot modules and
all test modules); build_index.py treats those lines as run by every test that imports them."""
import dis
import importlib
import json
import os
import sys

root = os.path.realpath(sys.argv[1]) + "/"
sys.path.insert(0, "/app")
os.chdir(root)
sys.path.insert(0, root)
import brstate

brstate.preload()
mon = sys.monitoring
E = mon.events
TOOL = 4
mon.use_tool_id(TOOL, "imp")
depth = [0]
seen = {}
done = set()
SETUP = ("RESUME", "COPY_FREE_VARS", "MAKE_CELL", "RETURN_GENERATOR", "POP_TOP", "NOP")


def rel(fn):
    rp = os.path.realpath(fn) if not fn.startswith("<") else fn
    return rp[len(root):] if rp.startswith(root) and rp.endswith(".py") else None


def start(code, off):
    f = rel(code.co_filename)
    if f is None:
        return mon.DISABLE
    if code.co_name == "<module>":
        depth[0] += 1
        return None
    if depth[0] and code.co_flags & 1 and code not in done:
        done.add(code)
        entry = next((ln for _, _, ln in code.co_lines() if ln is not None), code.co_firstlineno)
        # only a function with real work on its entry line loses anything (a multi-line def does not)
        if any(i.positions and i.positions.lineno == entry and i.opname not in SETUP for i in dis.get_instructions(code)):
            seen.setdefault(f, set()).add(entry)
    return None


def leave(code, *a):
    if code.co_name == "<module>" and rel(code.co_filename) is not None:
        depth[0] -= 1


mon.register_callback(TOOL, E.PY_START, start)
mon.register_callback(TOOL, E.PY_RETURN, leave)
mon.register_callback(TOOL, E.PY_UNWIND, leave)
mon.set_events(TOOL, E.PY_START | E.PY_RETURN | E.PY_UNWIND)
import sqlglot  # noqa: E402

brstate.load_all()
for dp, dn, fn in os.walk("tests"):
    dn[:] = sorted(d for d in dn if d not in ("__pycache__", "fixtures"))
    for f in sorted(fn):
        if f.endswith(".py") and f not in ("gen_fixtures.py", "leakcheck.py"):
            try:
                importlib.import_module(os.path.join(dp, f)[:-3].replace(os.sep, "."))
            except BaseException:
                pass
mon.set_events(TOOL, 0)
json.dump({f: sorted(v) for f, v in sorted(seen.items())}, open(sys.argv[2], "w"))
print(sum(len(v) for v in seen.values()), "entry lines in", len(seen), "files", file=sys.stderr)
