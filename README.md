# Blast Radius selector

Entry for the RWX "Blast Radius" track: given a patch against sqlglot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, print the tests that can fail and how to split
them over workers.

## How it works

**1. A map of the pinned commit** (`data/`, built once with `tools/trace_iso.py`).
Every test is run alone in a fresh process under `sys.monitoring`. For each test the map
records the lines it executes at run time, the lines it executes while modules import, the
classes it instantiates and the files it opens. Fixture-driven tests are also traced one
fixture entry at a time, and the two TPC executor tests one stage at a time. One-line lambdas
that already run while modules import leave no run-time trace (`tools/trace_import_calls.py`);
those lines count as run by every test that imports them.

**2. Static step** (`brplan.py`, `selector.py`). Each changed position of a patch is located
in the syntax tree:

- inside a function body: candidates are the tests that execute the enclosing statement;
- at module or class level: candidates are the tests that import the module, use the class
  (or a subclass) and execute a line that names the changed attribute;
- in a fixture or other data file: the tests that read it, narrowed to the edited entries;
- comment/whitespace-only edits (same syntax tree) and unread files: nothing.

Some changes reach tests through pytest or the interpreter rather than through code a test
runs, and the checking step below cannot see those. They are answered conservatively: a pytest
configuration change, a `conftest.py` or test-package `__init__.py` that does more than define
names, a new top-level module that shadows a standard or installed one, and a statement that
sets something interpreter-wide (recursion limit, warning filters, logging, environment)
select every test they can reach. So does test code that sets (or stops restoring) something on
a shared class or module, starts a mock patch by hand, reloads a module or defines a class that
registers itself, and a new test file whose import does more than define tests: pytest runs all
of that in one process with every other test. A test file that starts using pytest marks or
fixtures has its tests selected outright, and code reached only in the wake of other tests is
never checked in isolation.

Three things are compared with the pinned commit before any test is run: what a bare
`import sqlglot` loads (tests that start a fresh interpreter depend on it), whether every test
module still imports when they are imported in pytest's collection order (import statements
changed), and, for a module-level statement that reaches into another module, the state of
every module rather than just the importers of the changed one.

**3. Dynamic step** (`brrun.py`, `brstate.py`, `brtpc.py`). When checking is cheaper than
selecting blindly, the patch is applied to a scratch tree in `/tmp` and the candidates are run
in a forked child with a small in-process `unittest` runner; only the tests that really fail are
selected. Fixture-driven tests run on just the entries the change reaches. For module/class-level
edits the child first compares every module global and class attribute with the pinned commit's
(`state.pkl`), so a change that leaves module state untouched selects nothing. The two TPC
executor tests are checked stage by stage against fingerprints of the pinned commit's intermediate
results (`tpc.pkl`), so the 21-second execution stage only runs where its code or inputs changed.
The optimizer stage is itself checked rule by rule where the change is a plain function-body
edit: the lines each optimizer rule runs for each query are traced (`tools/trace_tpc_rules.py`),
the pinned commit's input and output of every rule are kept in the image, and only the rules the
change reaches are re-run from those inputs. A rule whose output differs sends the query to the
whole-optimizer comparison.
Where the execution stage does run, only the tables its query uses are read, and the table
sqlglot's executor produces is compared with the pinned commit's first: an identical table for an identical DuckDB query compares exactly as it did there, so the
DuckDB half is skipped. The check adds up the work before starting and gives up (selecting the
test) when running it would cost more than the test itself. Every run has a time limit; a test
that hits it is selected, which is what its real timeout would report.

**4. Packing.** Selected tests are split longest-first over 1 to 8 workers, whichever number
minimises busiest worker + 2 s per worker.

Nothing here maps a patch, or a kind of edit, to an answer: the maps describe the pinned commit
only (coverage, imports, fixtures, intermediate results), and every decision about a patch comes
from where it lands in that map or from running the tests against it.

## Files

| | |
| --- | --- |
| `selector.py` | entry point: planning, the run-or-select policy, worker packing |
| `brplan.py` | locates a changed position in the syntax tree |
| `applypatch.py` | unified-diff parser/applier (stdlib only) |
| `brrun.py` | in-process test runner; fixture slicing; inline stand-ins for process pools |
| `brstate.py` | module-state fingerprints |
| `brtpc.py` | staged checks for the TPC executor tests |
| `build_index.py` | turns `data/` into the index the selector loads (run in the Dockerfile) |
| `tools/` | the tracer and the scripts that produced `data/` |

## Try it

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
```
