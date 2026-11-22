#!/bin/sh
# usage (from the project root): lab/exp_pt/tools/run.sh suite SEED XDIST | iso WORKERS [--only ...]
BR=$(cd "$(dirname "$0")/../../.." && pwd); cd $BR
K=$1; shift
M="-v $BR/sqlglot:/src:ro -v $BR/lab/tools:/tools:ro -v $BR/lab/exp_pt/tools:/pt:ro -v $BR/entry:/entry:ro -v $BR/lab/exp_pt/out:/out -v $BR/kit/sample/tests.json:/tests.json:ro"
docker run --rm --platform linux/arm64 --cpus 4 --memory ${MEM:-3500m} $M --tmpfs /tmp:rw,size=900m br-lab:arm64 /pt/$K.sh "$@"
