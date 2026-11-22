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
fixtures, or defines one of the set-up hooks pytest calls by name even for unittest classes
(`setup_method`, `setup_module` and the like), has its tests selected outright, and code reached only in the wake of other tests is
never checked in isolation, nor is any other line of a function that has such code (one test
empties the dialect registry; what the loader then does with a changed line no run of one test
alone shows, even though that line is also reached alone).

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
Two things reach the execution stage without their lines running in it, and are treated as
reaching it: what `mp_execute` and `execute` do before calling the optimizer (looking up the
tables, building the schema), and function lines that run while modules import, whose effect
is whatever state they leave behind. For the latter the state of every module is compared with
the pinned commit's first; if nothing differs, only the run-time trace counts.
Where the execution stage does run, only the tables its query uses are read, and the table
sqlglot's executor produces is compared with the pinned commit's first: an identical table for an identical DuckDB query compares exactly as it did there, so the
DuckDB half is skipped. When the table does differ and the query DuckDB would be sent is still
the pinned commit's, character for character, DuckDB is not started either: its answers to the
fixture's queries were recorded when the image was built (asked twice on separate connections,
kept only where both frames are identical), and the test's own comparison is run against the
recorded frame (`BR_DUCK_FRAMES=0` asks DuckDB instead). The check adds up the work before starting and gives up (selecting the
test) when running it would cost more than the test itself. Every run has a time limit; a test
that hits it is selected, which is what its real timeout would report.

**Starting from an imported package.** Every check process begins by importing the package from
its scratch copy of the patched tree. A bare `import sqlglot` loads 36 modules on the pinned commit
(the list is kept in the image). When a patch changes only test files and existing python files of
the package that are not among those modules, that import executes exactly the code it executes on
the pinned commit, so such checks are started last, from the parent after it has done the import
once on the checkout; each child then points the loaded modules at their copies in its scratch tree
(same bytes), and everything imported from there on comes from the patched tree. A patch that adds,
removes or renames a file, or touches anything else, is checked from a process that has imported
nothing, as before (`BR_WARM=0` does that for every patch).

**One judgement call.** When a change reaches execution-stage code that every TPC-DS query
runs, proving that `test_execute_tpcds` still passes means re-running all 57 queries, which
costs as much as running the test. Instead the queries are run most-telling-first for a fixed
budget (2 seconds of executor time: 12 of the 57 with the default), and if every table is
identical to the pinned commit's the test is left out. This is not a proof. It was measured on
632 one-line mutants of the executor and planner, 219 of which break the test.
`data/tpc_sentinels.json` holds the order of the queries, a property of the test's queries and
data, not of any patch: at each step the query that shows, per second of its own run time, the
most breaking mutants that no earlier query shows. The 12 queries show every breaking mutant of
the study; when the list is built on four fifths of them and tried on the rest, about 2% of the
breaking ones get through (mostly mutants that change one query only). It only applies when the
map says the change is reached by the execution stage alone
(not by parsing, optimizing or transpiling any query, and not while modules import): a change
to an earlier stage can alter one query and leave the others alone, so there every query counts.
Any table that differs at all ends it with the test selected. `BR_TPC_PARTIAL=0` turns it off.

The same judgement is helped by a second kind of evidence. The class has 56 cheap tests of the
executor besides the TPC ones; those that the change reaches are run first. On the study mutants,
210 of the 219 changes that break `test_execute_tpcds` break one of these too. So when at least
three such tests were all run and all passed, the list of queries is cut to its first 7
(`BR_TPC_SMALL`, executor seconds); if any of them fails, or fewer than three exist, nothing
changes. With that evidence the short list is also what is run when the queries the change
reaches would have fitted the longer allowance. Like the longer list this is a judgement, not a
proof: on the study mutants no breaking change passes both the cheap tests and the 7 queries
(5 pass the 7 queries alone). Their own verdicts
count as usual, so the ones that pass are no longer selected unverified.

**Class fixtures on first use.** `TestExecutor.setUpClass` reads every TPC-H and TPC-DS table and
opens two DuckDB connections, most of a second, and only the three TPC tests look at any of it.
When the patch changes none of the code that set-up runs (the map has it as its own item) and
none of the test's own files, the set-up does what it does on the pinned commit, so the check
process gives the class the same attributes built on first use instead: table dicts that read a
table when it is asked for (and all of them when the dict is looked at as a whole), connections
that are opened when first used, and the two query lists. The image build checks on the pinned
commit that the lazily read tables equal the eagerly read ones, cell for cell and type for type,
and that every test of the class passes when run alone on this class state; otherwise the
feature stays off (`BR_LAZY=0` turns it off by hand).

**Operator swaps that change no value.** `data/cmp_same.json` is a map of the pinned commit, made
like the coverage map: the whole test suite was run with every single-operator comparison in
`sqlglot/` written with `is`, `is not`, `<`, `<=`, `>` or `>=` also evaluating its partner operator
(`==`, `!=`, `<=`, `<`, `>=`, `>`). The partner is only evaluated when the operands compare with
built-in code (None, numbers, strings, enum members, objects that define no `__eq__`, short tuples
of those, or a sqlglot expression against a value of another type, where `Expression.__eq__`
answers from the types alone); any other operands mark the comparison as unknown. A comparison is
in the map when both operators gave the same value on every evaluation of every run (ten runs
with different hash seeds, serial and with 2 to 4 parallel workers, each required to finish with
every test seen and none failing), or when no run evaluated it and the coverage map agrees that
no test executes its line. A patch that does nothing but replace such operators by their
partners, each at a mapped comparison, changes no value that any test computes, so no test is
selected for it (tests that read the file as text stay candidates). The changed lines are taken
from the old and new file texts, not from the hunk headers; the rest of the two syntax trees must
be identical, and one swapped comparison outside the map sends the whole patch down the normal
path. `==` to `is` is never taken from the map: whether two equal
values are one object is the interpreter's business. The tracer is in `tools/trace_cmp/`.

**Operator replacements, test by test.** `data/pt_map.json.gz` is a second map of the same kind,
kept per test (tracer and merge in `tools/trace_pt/`). Every single-operator comparison and every
two-operand `and`/`or` in `sqlglot/` was traced with each alternative operator evaluated next to
the real one: once with every test run alone in its own process, and twice with the whole suite
under pytest (two hash seeds; evaluations outside a test's call phase count for every test). A
test is *affected* by a replacement when the alternative gave another value in it at least once,
when that could not be told without running package code (then it counts as different), when a
change between identity and equality would not be stable from run to run, or when the test
evaluates the place alone but not in the suite (it then consumed a result another test computed).
For a function that keeps state (caches, globals, class attributes) every test running a line of
it is affected as soon as one differs. A patch to a file that does nothing but replace such
operators inside function bodies, each at a place of the map, can only change the outcome of
affected tests, so only those (and the tests that read the file as text) stay candidates of that
file; the rest of the pipeline runs as usual on them. The tests left out this way are still put
into idle worker time where there is any. A place is not looked up at all when its function (or
its statement, outside any function) deals in values that change from one run of the suite to the
next: calls to `hash`, `id`, clocks or random numbers, sets built there, names that say they hold
a hash or an id, and the comparison methods themselves. For such values the traced runs show one
ordering out of several possible ones (found by a patch that turned `hash(a) == hash(b)` into
`<=`: one test failed under three hash seeds out of ten). Checked on 112 labeled commits of this kind and 56
fresh ones labeled with pytest: every failing test was among the affected ones (`BR_PT=0` turns
the map off).

The scaled copy of `test_max_nodes` (200 columns for 10,000) also has to keep the pinned commit's
pace: its CPU time is measured against a fixed piece of plain Python work, at build on the pinned
commit and again in every check, and a copy that runs more than about twice as slow selects the
real test, which would do fifty times as much of the same work under a time limit. A check process
counts as stuck in its imports by its own CPU seconds, not by the clock, so a busy machine does
not look like a hang.

A patch that changes one file in more than one of its sections (a diff never writes that, glued
patches do) selects every test: the sections apply one after the other, which the per-file
reasoning does not follow. A check process that dies, hangs or never gets to run before its first
report (the patched package may not even import in finite time) also selects every test, and so
does a failed comparison of module state for an import-level edit, whose static candidates are no
upper bound on what it reaches.

A fixture whose entries were added, removed or moved has its readers selected as they are: the
map's entry numbers no longer name the file's entries, so a run on "the entries the change
reaches" would not be a run of the new ones. A file that stops being UTF-8 selects whoever opens
it (a python file: whoever imports it), before any thought about which entries changed. A patch
of several files written without `diff --git` lines (plain `diff -u` output) is split at its file
headers, and a patch that does not apply to the checkout was not read the way it was meant, so
every test is selected.

A changed line that itself works with hash values or object addresses, or that looks at the
process it runs in (environment variables, arguments, loaded modules, the output streams), is not
judged by running tests here: the suite runs under another hash seed and inside pytest, so every
test that executes the line is selected. A git `copy` leaves its source in place.

A new file can change what an import resolves to with no existing line changed. A new compiled
module, byte-code file, `.pth` file or package-metadata directory anywhere, a new package that
shadows a module of the same name, a new `sqlglot/_version.py` (imported at start-up when it
exists), and any binary content in the package or the tests outside the fixture directories select every
test. A new module
in a directory whose package imports by a name taken from data (the dialect loader's and the
optimizer package's fall-back import) makes the tests that reach that import candidates, and a
new directory counts as an entry of its parent's listing. A pytest configuration file that pytest
could no longer parse selects every test (pytest stops before collecting). Lines of a patch end
at a line feed only, as for git: form feeds and Unicode line separators are characters of a line.

A patch whose paths are written with a prefix other than `a/` and `b/` is read by dropping the
first path component when that names a file of the checkout; a patch that changes a file the
checkout does not have was not read the way it was meant, and every test is selected.

**State left behind by an interrupted call.** One place in the package (`data/leaks.json`)
registers an entry in a class dict shared by most parsers, parses, and removes the entry again,
with nothing to remove it when the parse raises. A test that raises in there passes or fails on
its own account, but the entry stays, and tests that run later in the same process fail for it:
no run of one test alone shows that. The check process therefore looks at that dict after every
test it runs. When the entry was left behind, the tests that fail with the entry present are
selected (three; measured by running the whole suite on the pinned commit with the entry kept).
The same when the patch changes those lines themselves, or adds test code or fixture text of the
kind that goes through them. An entry left in another class's copy of the dict, for which nothing
was measured, selects every test. Because the check stops a test at its first failing subtest
while pytest goes on to the next ones, a failed test of a file the patch changes is run once more
to its end before the dict is looked at. Found by an adversarial review of this selector (a test edit
that parses a broken `CONNECT BY` inside `assertRaises`).

**4. Packing.** Selected tests are split longest-first over 1 to 8 workers, whichever number
minimises busiest worker + 2 s per worker.

Workers below the busiest one have idle time that costs nothing, so candidates that a check
cleared are put back there, shortest first, while no worker grows past the busiest: the commit
costs exactly the same, and a test that fails for a reason no check here saw is selected anyway.

**A line for every commit, whatever happens.** A run with a missing or malformed line is no run
at all. The work is therefore done by a child of the container's first process, and its answer
passes through that process: each line is checked against the scorer's rules (known commit, not
repeated, whole test numbers that exist, none twice, at most 8 workers), and a commit left without
a valid line (the child crashed, was killed for memory, or sat still past the time a run may take)
gets a line with every test in it. Planning one commit has a time limit of its own with the same
fallback for that commit, and so has putting its answer together. `BR_FAULT=crash|hang|half`
makes the child fail on purpose, to try this.

Nothing here maps a patch, or a kind of edit, to an answer: the maps describe the pinned commit
only (coverage, imports, fixtures, intermediate results), and every decision about a patch comes
from where it lands in that map or from running the tests against it.

## Files

| | |
| --- | --- |
| `selector.py` | entry point: planning, the run-or-select policy, worker packing |
| `brplan.py` | locates a changed position in the syntax tree |
| `applypatch.py` | unified-diff parser/applier (stdlib only) |
| `brpt.py` | recognises operator replacements and looks them up in the per-test map |
| `brrun.py` | in-process test runner; fixture slicing; inline stand-ins for process pools |
| `brstate.py` | module-state fingerprints |
| `brtpc.py` | staged checks for the TPC executor tests |
| `build_index.py` | turns `data/` into the index the selector loads (run in the Dockerfile) |
| `tools/` | the tracer and the scripts that produced `data/` |

## Try it

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
```
