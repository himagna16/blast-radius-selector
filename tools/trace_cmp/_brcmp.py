"""Recorder for the comparison map (used only while tracing the pinned commit's test suite).

Every instrumented comparison `a OP b` calls one of the functions below with its site number.
Lines "<site> <code>" are appended to $BRC_OUT the first time a process sees them:
  0  the site was evaluated
  1  the partner operator (is <-> ==, is not <-> !=, < <-> <=, > <-> >=) gave another result
  2  operands for which evaluating the partner operator is not known to be free of side effects
"""
import enum
import os

_FD = os.open(os.environ.get("BRC_OUT", "/tmp/brc.flags"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
_state = bytearray(1 << 16)     # per site: bit 0 evaluated, bit 1 differs, bit 2 opaque (as written out)


def _flag(site, code):
    bit = 1 << code
    if not _state[site] & bit:
        _state[site] |= bit
        os.write(_FD, b"%d %d\n" % (site, code))


_EQS = {object.__eq__, str.__eq__, int.__eq__, float.__eq__, bytes.__eq__, complex.__eq__, type(None).__eq__,
        getattr(enum.Enum, "__eq__")}
_NES = {object.__ne__, str.__ne__, int.__ne__, float.__ne__, bytes.__ne__, complex.__ne__, type(None).__ne__,
        getattr(enum.Enum, "__ne__")}
_CONT = {tuple: (tuple.__eq__, tuple.__ne__), list: (list.__eq__, list.__ne__), frozenset: (frozenset.__eq__, frozenset.__ne__),
         set: (set.__eq__, set.__ne__), dict: (dict.__eq__, dict.__ne__)}
_ORD = {int: (int.__lt__, int.__le__, int.__gt__, int.__ge__), float: (float.__lt__, float.__le__, float.__gt__, float.__ge__),
        str: (str.__lt__, str.__le__, str.__gt__, str.__ge__), bytes: (bytes.__lt__, bytes.__le__, bytes.__gt__, bytes.__ge__)}
_kind = {}
_okind = {}


def _is_expr_eq(f):
    c = getattr(f, "__code__", None)
    return c is not None and c.co_filename.replace("\\", "/").endswith("sqlglot/expressions/core.py") and c.co_name in ("__eq__", "__ne__")


def _tkind(t):
    """0: equality may run arbitrary code; 1: built-in equality on a scalar or identity; 2: built-in
    container (its elements decide); 3: sqlglot expression (identity or type test first, then hashes)."""
    k = _kind.get(t)
    if k is None:
        eq, ne = t.__eq__, t.__ne__
        k = 0
        for base, (ceq, cne) in _CONT.items():
            if issubclass(t, base) and eq is ceq and ne is cne:
                k = 2
        if not k:
            if eq in _EQS and ne in _NES:
                k = 1
            elif _is_expr_eq(eq) and _is_expr_eq(ne):
                k = 3
        _kind[t] = k
    return k


def _flat(c):
    for x in (c if not isinstance(c, dict) else list(c.keys()) + list(c.values())):
        if _tkind(type(x)) != 1:
            return False
    return True


def _pure_eq(a, b):
    ka, kb = _tkind(type(a)), _tkind(type(b))
    if not ka or not kb:
        return False
    if ka == 3 or kb == 3:
        return a is b or type(a) is not type(b)
    if ka == 2 and kb == 2:
        return len(a) <= 64 and len(b) <= 64 and _flat(a) and _flat(b)
    return True


def _okind_of(t):
    k = _okind.get(t)
    if k is None:
        k = 0
        for base, fs in _ORD.items():
            if issubclass(t, base) and (t.__lt__, t.__le__, t.__gt__, t.__ge__) == fs:
                k = 1
        if issubclass(t, (tuple, list)) and t.__lt__ in (tuple.__lt__, list.__lt__) and t.__le__ in (tuple.__le__, list.__le__) \
                and t.__gt__ in (tuple.__gt__, list.__gt__) and t.__ge__ in (tuple.__ge__, list.__ge__):
            k = 2
        _okind[t] = k
    return k


def _pure_ord(a, b):
    ka, kb = _okind_of(type(a)), _okind_of(type(b))
    if not ka or not kb:
        return False
    if ka == 2 or kb == 2:
        if ka != kb:
            return False
        for c in (a, b):
            if len(c) > 64:
                return False
            for x in c:
                if _okind_of(type(x)) != 1:
                    return False
    return True


def is_(s, a, b):
    r = a is b
    st = _state[s]
    if st > 1:
        return r
    if not st:
        _flag(s, 0)
    if _pure_eq(a, b):
        try:
            if (a == b) is not r:
                _flag(s, 1)
        except BaseException:
            _flag(s, 1)
    else:
        _flag(s, 2)
    return r


def isnot(s, a, b):
    r = a is not b
    st = _state[s]
    if st > 1:
        return r
    if not st:
        _flag(s, 0)
    if _pure_eq(a, b):
        try:
            if (a != b) is not r:
                _flag(s, 1)
        except BaseException:
            _flag(s, 1)
    else:
        _flag(s, 2)
    return r


def eq(s, a, b):
    _flag(s, 0)
    pure = _pure_eq(a, b)
    if not pure:
        _flag(s, 2)
    try:
        r = a == b
    except BaseException:
        _flag(s, 1)
        raise
    if pure and (a is b) is not r:
        _flag(s, 1)
    return r


def ne(s, a, b):
    _flag(s, 0)
    pure = _pure_eq(a, b)
    if not pure:
        _flag(s, 2)
    try:
        r = a != b
    except BaseException:
        _flag(s, 1)
        raise
    if pure and (a is not b) is not r:
        _flag(s, 1)
    return r


def _ordered(s, a, b, r, alt):
    if _pure_ord(a, b):
        try:
            if alt(a, b) is not r:
                _flag(s, 1)
        except BaseException:
            _flag(s, 1)
    else:
        _flag(s, 2)
    return r


def lt(s, a, b):
    st = _state[s]
    if st > 1:
        return a < b
    if not st:
        _flag(s, 0)
    try:
        r = a < b
    except BaseException:
        _flag(s, 1)
        raise
    return _ordered(s, a, b, r, lambda x, y: x <= y)


def le(s, a, b):
    st = _state[s]
    if st > 1:
        return a <= b
    if not st:
        _flag(s, 0)
    try:
        r = a <= b
    except BaseException:
        _flag(s, 1)
        raise
    return _ordered(s, a, b, r, lambda x, y: x < y)


def gt(s, a, b):
    st = _state[s]
    if st > 1:
        return a > b
    if not st:
        _flag(s, 0)
    try:
        r = a > b
    except BaseException:
        _flag(s, 1)
        raise
    return _ordered(s, a, b, r, lambda x, y: x >= y)


def ge(s, a, b):
    st = _state[s]
    if st > 1:
        return a >= b
    if not st:
        _flag(s, 0)
    try:
        r = a >= b
    except BaseException:
        _flag(s, 1)
        raise
    return _ordered(s, a, b, r, lambda x, y: x > y)
