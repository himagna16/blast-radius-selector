# Same base image the commits were labeled in (its Python and SQLite decide which tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV BR_PROBE=8 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 SKIP_INTEGRATION=0 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# The base image ships the standard library without compiled caches, and some installed modules
# have none either: compile them once here instead of in every run (same bytecode, same behaviour).
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13 > /dev/null || true
# sqlglot at the pinned commit, straight from its own repository.
RUN python -c "import urllib.request,tarfile,io,os; \
sha='eb11aec4be8cb2122afe713a4816662c3f0fb2fa'; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/%s.tar.gz'%sha).read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app',filter='data'); \
os.rename('/app/sqlglot-'+sha,'/app/repo')" \
 && python -m compileall -q /app/repo/sqlglot /app/repo/tests
COPY applypatch.py brplan.py brpt.py brrun.py brstate.py brtpc.py build_index.py selector.py /app/
COPY data /app/data
# Maps of the pinned commit, built here from the traced data and from the checkout itself:
#   index.pkl  which tests execute which lines / use which classes / read which files
#   tpc.pkl    fingerprints of intermediate and final results of the two TPC executor tests
#   state.pkl  fingerprints of every module global and class attribute after import
RUN python /app/build_index.py /app/data/iso.pkl.gz /app/data/cost_iso.pkl.gz /app/index.pkl /app/repo \
 && cd /app/repo \
 && PYTHONPATH=/app/repo:/app python -c "import brtpc; fp = brtpc.build('/app/tpc.pkl'); print('tpc result fingerprints', len(fp['res'])); open('/app/tpc.pkl.mn', 'w').write('%.4f' % brtpc.max_nodes_pace())" 2>/dev/null \
 && PYTHONPATH=/app/repo:/app python /app/brstate.py build /app/state.pkl 8 2>&1 | tail -1 \
 && rm -rf /app/data && python -m compileall -q /app/*.py && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
