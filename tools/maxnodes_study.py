"""In container: for each id, truth (pytest on test_max_nodes alone) and scaled proxy. usage: PATCH_DIR OUT ids..."""
import sys, os, subprocess, json, re
sys.path.insert(0, "/tools")
pd, outp = sys.argv[1], sys.argv[2]
proxy_src = open("/tools/proxy_maxnodes.py").read()
code = re.search(r"code = r'''(.*?)'''", proxy_src, re.S).group(1)
out = json.load(open(outp)) if os.path.exists(outp) else {}
for k in sys.argv[3:]:
    if k in out: continue
    subprocess.run("rm -rf /tmp/w && mkdir /tmp/w && cd /src && tar cf - --exclude=.git --exclude=__pycache__ --exclude=fixtures sqlglot tests | (cd /tmp/w && tar xf -)", shell=True)
    subprocess.run([sys.executable, "/tools/applypatch.py", "/tmp/w", f"{pd}/{k}.patch"], capture_output=True)
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd="/tmp/w", capture_output=True, text=True, timeout=120)
        px = (p.stdout.strip().splitlines() or ["FAIL noout"])[-1]
    except subprocess.TimeoutExpired:
        px = "FAIL timeout"
    try:
        t = subprocess.run([sys.executable, "-m", "pytest", "tests/test_parser.py::TestParser::test_max_nodes", "-q", "-p", "no:cacheprovider", "--timeout=600"],
                           cwd="/tmp/w", capture_output=True, text=True, timeout=900, env=dict(os.environ, PYTHONPATH="/tmp/w"))
        truth = "PASS" if t.returncode == 0 else "FAIL " + (t.stdout.strip().splitlines() or [""])[-1][:70]
    except subprocess.TimeoutExpired:
        truth = "FAIL timeout"
    out[k] = [truth, px]
    json.dump(out, open(outp, "w"))
