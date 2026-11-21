"""Scaled-down test_max_nodes, run on patched trees. usage (in container): proxy_maxnodes.py PATCH_DIR ids..."""
import sys, os, subprocess, json
code = r'''
import sys
sys.path.insert(0, ".")
def proxy(width=200):
    from sqlglot import parse_one, exp
    from sqlglot.errors import ParseError
    assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    assert isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
    depth = 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join([expr for _ in range(width)])}"
    a, b = width * depth, width * depth // 20
    try:
        parse_one(sql, max_nodes=a); raise AssertionError("no raise a")
    except ParseError as e:
        assert "Maximum number of AST nodes" in str(e), str(e)[:80]
    try:
        parse_one(sql, max_nodes=b); raise AssertionError("no raise b")
    except ParseError as e:
        assert "(%d)" % b in str(e), str(e)[:80]
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5); raise AssertionError("no raise c")
    except ParseError:
        pass
    assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
try:
    proxy(); print("PASS")
except BaseException as e:
    print("FAIL", type(e).__name__, str(e)[:60].replace("\n", " "))
'''
pd = sys.argv[1]; out = {}
for k in sys.argv[2:]:
    subprocess.run("rm -rf /tmp/w && mkdir /tmp/w && cd /src && tar cf - --exclude=.git --exclude=__pycache__ sqlglot | (cd /tmp/w && tar xf -)", shell=True)
    r = subprocess.run([sys.executable, "/tools/applypatch.py", "/tmp/w", f"{pd}/{k}.patch"], capture_output=True, text=True)
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd="/tmp/w", capture_output=True, text=True, timeout=60)
        out[k] = (p.stdout.strip().splitlines() or ["FAIL noout " + p.stderr[-80:]])[-1]
    except subprocess.TimeoutExpired:
        out[k] = "FAIL timeout"
print(json.dumps(out))
