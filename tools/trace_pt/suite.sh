#!/bin/sh
# usage (in the lab image): suite.sh SEED XDIST ; mounts: /src (sqlglot ro), /tools (lab/tools ro), /pt (lab/exp_pt/tools ro), /out (rw), /tests.json
SEED=$1; N=${2:-0}
mkdir -p /tmp/w && cd /src && tar cf - --exclude=.git --exclude=sqlglot-integration-tests --exclude=posts --exclude=pdoc . | (cd /tmp/w && tar xf -)
cd /tmp/w
python /pt/instrument_pt.py /tmp/w /out/sites.suite$SEED.json || exit 1
XD=""; [ "$N" != "0" ] && XD="-n $N"
rm -rf /out/suite$SEED; mkdir -p /out/suite$SEED
BRPT_TESTS=/tests.json BRPT_DIR=/out/suite$SEED BRPT_TAG=s$SEED PYTHONHASHSEED=$SEED BR_OUT=/tmp/res.json PYTHONPATH=/tmp/w:/tools:/pt \
  timeout 7200 python -m pytest tests -p brplugin -p brptplugin -p no:cacheprovider -q --timeout=3600 $XD > /tmp/pytest.log 2>&1
echo "rc=$?" >> /tmp/pytest.log
# records of processes started without BRPT_DIR fall back to /tmp/brpt
[ -d /tmp/brpt ] && cp -r /tmp/brpt /out/suite$SEED/fallback
python - "$SEED" <<'PY'
import json,glob,sys
failed=set(); cf=[]; seen=[]
for f in glob.glob('/tmp/res.json*'):
    d=json.load(open(f)); failed|=set(d['failed']); cf+=d['collect_failed']; seen+=d['seen']
json.dump({"failed":sorted(failed),"collect_failed":sorted(set(cf)),"n_seen":len(seen),"tail":open('/tmp/pytest.log').read()[-1500:]}, open('/out/result.suite%s.json'%sys.argv[1],'w'))
PY
