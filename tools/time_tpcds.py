"""Per-query cost of test_execute_tpcds on the pinned commit -> data/tpc_cost.json.

usage (inside the image, cwd = the sqlglot checkout):  python time_tpcds.py OUT.json [repeats]
"exec": CPU seconds of sqlglot's executor for the query (optimize included);
"duck": CPU seconds of the DuckDB run and the frame comparison. Median over the repeats."""
import json
import pickle
import statistics
import sys
import time

sys.path.insert(0, "/app")
sys.path.insert(0, ".")
import brstate

brstate.preload()
import tests.helpers as H
import tests.test_executor as TE

P = time.process_time
cls = TE.TestExecutor
cls.setUpClass()
case = cls("test_execute_tpcds")
TE.initializer(H.TPCDS_SCHEMA, cls.tpcds_tables)
ex, du = {}, {}
for rep in range(int(sys.argv[2]) if len(sys.argv) > 2 else 3):
    cls.cache.clear()
    for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
        if not meta.get("execute"):
            continue
        e = pickle.loads(pickle.dumps(TE.parse_one(sql)))
        t0 = P()
        tab = TE.mp_execute(e, meta)
        t1 = P()
        case.subtestHelper(i, pickle.loads(pickle.dumps(tab)), tpch=False)
        t2 = P()
        ex.setdefault(i, []).append(t1 - t0)
        du.setdefault(i, []).append(t2 - t1)
json.dump({"exec": {str(i): round(statistics.median(v), 4) for i, v in sorted(ex.items())},
           "duck": {str(i): round(statistics.median(v), 4) for i, v in sorted(du.items())}}, open(sys.argv[1], "w"))
