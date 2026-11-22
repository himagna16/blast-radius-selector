#!/bin/sh
# usage (in the lab image): iso.sh WORKERS [--only ...] ; mounts: /src (sqlglot ro), /entry (ro), /pt (ro), /out (rw), /tests.json
W=$1; shift
mkdir -p /tmp/w && cd /src && tar cf - --exclude=.git --exclude=sqlglot-integration-tests --exclude=posts --exclude=pdoc . | (cd /tmp/w && tar xf -)
cd /tmp/w
python /pt/instrument_pt.py /tmp/w /out/sites.iso.json || exit 1
rm -rf /out/iso; mkdir -p /out/iso
PYTHONHASHSEED=0 python /pt/iso_pt.py /tmp/w /tests.json /out/iso --workers $W "$@"
[ -d /tmp/brpt ] && cp -r /tmp/brpt /out/iso/fallback
exit 0
