#!/bin/sh
# usage: trace.sh SEED XDIST ; mounts: /src (repo ro), /tools (ro), /exp (ro), /out (rw)
SEED=$1; N=${2:-0}
mkdir -p /tmp/w && cd /src && tar cf - --exclude=.git --exclude=sqlglot-integration-tests --exclude=posts --exclude=pdoc . | (cd /tmp/w && tar xf -)
cd /tmp/w
python /exp/instrument.py /tmp/w /out/sites.$SEED.json || exit 1
XD=""; [ "$N" != "0" ] && XD="-n $N"
rm -f /out/flags.$SEED
BRC_OUT=/out/flags.$SEED PYTHONHASHSEED=$SEED BR_OUT=/tmp/res.json PYTHONPATH=/tmp/w:/tools timeout 7200 python -m pytest tests -p brplugin -p no:cacheprovider -q --timeout=3600 $XD > /tmp/pytest.log 2>&1
echo "rc=$?" >> /tmp/pytest.log
# flags written by processes that were started without the environment (they fall back to this file)
[ -f /tmp/brc.flags ] && { wc -l < /tmp/brc.flags > /out/fallback.$SEED; cat /tmp/brc.flags >> /out/flags.$SEED; }
python - "$SEED" <<'PY'
import json,glob,sys
failed=set(); cf=[]; seen=[]
for f in glob.glob('/tmp/res.json*'):
    d=json.load(open(f)); failed|=set(d['failed']); cf+=d['collect_failed']; seen+=d['seen']
json.dump({"failed":sorted(failed),"collect_failed":sorted(set(cf)),"n_seen":len(seen),"tail":open('/tmp/pytest.log').read()[-1500:]}, open('/out/result.%s.json'%sys.argv[1],'w'))
PY
