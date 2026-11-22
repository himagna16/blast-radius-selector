"""pytest plugin (-p brptplugin): tells _brpt which test is running in this process.
Only the call phase of a test is attributed to it; collection, imports, set-up and tear-down
(setUpClass, fixtures) and anything between tests are recorded as ALL (count for every test)."""
import json
import os

_num = {}


def pytest_configure(config):
    global _num
    _num = {t["id"]: t["number"] for t in json.load(open(os.environ["BRPT_TESTS"]))["tests"]}
    import _brpt
    _brpt.own("/tmp/brpt_cur.%d" % os.getpid())


def _set(t):
    import _brpt
    _brpt.set_test(t)


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    n = _num.get(item.nodeid)
    _set(n if n is not None else 0xFFFFFFFF)
    try:
        yield
    finally:
        _set(0xFFFFFFFF)


def pytest_unconfigure(config):
    import _brpt
    _brpt.set_test(0xFFFFFFFF)
    _brpt.flush()
