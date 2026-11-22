"""Per-test recorder for operator swaps (used only while tracing the pinned commit's tests).

Instrumented code calls, for every single-operator comparison `a OP b`, the function named after
OP with the site number, and for every two-operand `and`/`or` (not inside another BoolOp)
`bx` / `band` / `bor`. For each test and site the recorder keeps one byte:

  comparison (op k in OPS order): bit k = evaluated; bit j (j != k) = in this test, operator j
      gave another value than op k at least once, or could not be evaluated without running
      package code / side effects, or the identity of the operands is not stable across runs
  and/or: bit 0 = evaluated; bit 1 = swapping the operator could change something

Attribution: BRPT_TEST=n (isolated run: the whole process is test n, children too);
otherwise the process that called own() sets the current test with set_test(n) (ALL between
tests) and publishes it in a 4-byte file BRPT_CUR that other processes (pool workers,
subprocesses, forks) read at every evaluation. Records: uint32 triples (test, site, mask) in
$BRPT_DIR/rec.$BRPT_TAG.<pid>; masks of one (test, site) are OR-ed by the merger.
"""
import array
import atexit
import enum
import mmap
import os
import struct
import types

ALL = 0xFFFFFFFF
OPS = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot")
_N = 1 << 17
_st = bytearray(_N)
_touched = []
_buf = array.array("I")
_fd = None
_DIR = os.environ.get("BRPT_DIR", "/tmp/brpt")
_TAG = os.environ.get("BRPT_TAG", "x")
_mm = None
_cur = ALL
_mode = 1           # 0 owner (main test process), 1 child (reads BRPT_CUR), 2 fixed (isolated run)
if os.environ.get("BRPT_TEST"):
    _mode = 2
    _cur = int(os.environ["BRPT_TEST"])


def _out():
    global _fd
    if _fd is None:
        os.makedirs(_DIR, exist_ok=True)
        _fd = os.open(os.path.join(_DIR, "rec.%s.%d" % (_TAG, os.getpid())), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    return _fd


def _emit():
    """Moves the masks of the current test into the buffer (and resets them)."""
    st, b, c = _st, _buf, _cur
    for s in _touched:
        b.append(c); b.append(s); b.append(st[s]); st[s] = 0
    _touched.clear()


def flush():
    _emit()
    if _buf:
        os.write(_out(), _buf.tobytes())
        del _buf[:]


def _put(s, m):
    os.write(_out(), struct.pack("<III", _cur, s, m))


def _switch(t):
    global _cur
    _emit()
    _cur = t
    if len(_buf) > 300000:
        flush()


def _rd():
    global _mm
    if _mm is None:
        p = os.environ.get("BRPT_CUR")
        if not p:
            return ALL
        try:
            f = open(p, "rb")
            _mm = mmap.mmap(f.fileno(), 4, access=mmap.ACCESS_READ)
            f.close()
        except OSError:
            return ALL
    return struct.unpack_from("<I", _mm, 0)[0]


def _sync():
    t = _rd()
    if t != _cur:
        _switch(t)


# --- owner API (pytest plugin) ---
_wmm = None


def own(path):
    """Called in the main test process: it publishes the current test in `path`."""
    global _mode, _wmm
    with open(path, "wb") as f:
        f.write(struct.pack("<I", ALL))
    f = open(path, "r+b")
    _wmm = mmap.mmap(f.fileno(), 4)
    _mode = 0
    os.environ["BRPT_CUR"] = path


def set_test(t):
    if _mode == 0:
        _switch(t)
        _wmm[0:4] = struct.pack("<I", t)
        if len(_buf) > 100000:
            flush()


def _after_fork():
    global _fd, _buf, _mode, _wmm
    _fd = None
    _buf = array.array("I")
    _touched.clear()
    _st[:] = bytes(_N)          # the parent's masks belong to the parent's records
    if _mode == 0:
        _mode = 1
        _wmm = None


os.register_at_fork(after_in_child=_after_fork)
atexit.register(flush)


def _set(s, bits):
    if _busy:
        return
    if _mode == 1:
        _sync()
    m = _st[s]
    n = m | bits
    if n != m:
        if not m:
            _touched.append(s)
        _st[s] = n
        if _mode == 1:          # processes that may end with os._exit: write at once
            _put(s, n)


# --- purity of operand comparisons (from lab/exp_cmp/v2/_brcmp.py, extended) ---
_EQS = {object.__eq__, str.__eq__, int.__eq__, float.__eq__, bytes.__eq__, complex.__eq__, type(None).__eq__,
        getattr(enum.Enum, "__eq__"), type.__eq__}
_NES = {object.__ne__, str.__ne__, int.__ne__, float.__ne__, bytes.__ne__, complex.__ne__, type(None).__ne__,
        getattr(enum.Enum, "__ne__"), type.__ne__}
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
        try:
            eq, ne = t.__eq__, t.__ne__
        except Exception:
            _kind[t] = 0
            return 0
        k = 0
        if isinstance(t, type) and issubclass(t, type):   # classes compared: metaclass equality
            k = 1 if (eq in _EQS and ne in _NES) else 0
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
    if ka == 2 or kb == 2:      # container vs scalar: the scalar's (built-in) equality says False
        return True
    return True


def _okind_of(t):
    k = _okind.get(t)
    if k is None:
        k = 0
        try:
            for base, fs in _ORD.items():
                if issubclass(t, base) and (t.__lt__, t.__le__, t.__gt__, t.__ge__) == fs:
                    k = 1
            if issubclass(t, (tuple, list)) and t.__lt__ in (tuple.__lt__, list.__lt__) and t.__le__ in (tuple.__le__, list.__le__) \
                    and t.__gt__ in (tuple.__gt__, list.__gt__) and t.__ge__ in (tuple.__ge__, list.__ge__):
                k = 2
        except Exception:
            k = 0
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


_SINGLE_T = (type, enum.Enum, types.ModuleType, types.FunctionType, types.BuiltinFunctionType)


def _single(x):
    return x is None or x is True or x is False or x is Ellipsis or x is NotImplemented or isinstance(x, _SINGLE_T)


def _idstable(a, b):
    """Whether `a is b` gives the same answer in every run: one side is a singleton, or the two
    are different objects with different values (equal values could be one cached/interned
    object in another run; one object could be two equal ones)."""
    if _single(a) or _single(b):
        return True
    if a is b:
        return False
    try:
        return _pure_eq(a, b) and not (a == b)
    except BaseException:
        return False


def _alt(j, a, b):
    """Value of `a OPS[j] b` when it is known to run no package code; None otherwise."""
    try:
        if j >= 6:
            if not _idstable(a, b):
                return None
            return (a is b) if j == 6 else (a is not b)
        if j <= 1:
            if not _pure_eq(a, b):
                return None
            return (a == b) if j == 0 else (a != b)
        if not _pure_ord(a, b):
            return None
        if j == 2:
            return a < b
        if j == 3:
            return a <= b
        if j == 4:
            return a > b
        return a >= b
    except BaseException:
        return None


_busy = False


def _chk(s, k, a, b, r):
    global _busy
    if _busy:           # comparisons run by the recorder itself are not the program's
        return
    if _mode == 1:
        _sync()
    m = _st[s]
    new = m | (1 << k)
    need = 0xFF & ~m & ~(1 << k)
    if need:
        _busy = True
        try:
            if type(r) is not bool or (k >= 6 and not _idstable(a, b)):
                new |= need
            else:
                for j in range(8):
                    if need >> j & 1:
                        v = _alt(j, a, b)
                        if v is None or v is not r:
                            new |= 1 << j
        finally:
            _busy = False
    m = _st[s]
    new |= m
    if new != m:
        if not m:
            _touched.append(s)
        _st[s] = new
        if _mode == 1:
            _put(s, new)


def eq(s, a, b):
    try:
        r = a == b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 0, a, b, r)
    return r


def ne(s, a, b):
    try:
        r = a != b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 1, a, b, r)
    return r


def lt(s, a, b):
    try:
        r = a < b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 2, a, b, r)
    return r


def le(s, a, b):
    try:
        r = a <= b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 3, a, b, r)
    return r


def gt(s, a, b):
    try:
        r = a > b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 4, a, b, r)
    return r


def ge(s, a, b):
    try:
        r = a >= b
    except BaseException:
        _set(s, 0xFF)
        raise
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 5, a, b, r)
    return r


def is_(s, a, b):
    r = a is b
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 6, a, b, r)
    return r


def isnot(s, a, b):
    r = a is not b
    if _mode == 1 or _st[s] != 0xFF:
        _chk(s, 7, a, b, r)
    return r


# --- and / or ---
def bx(s, a):
    """Second operand not simple (calls, subscripts, ...): every evaluation can be affected."""
    if _mode == 1 or _st[s] != 3:
        _set(s, 3)
    return a


_PB = {}


def _purebool(v):
    t = type(v)
    k = _PB.get(t)
    if k is None:
        k = True
        for n in ("__bool__", "__len__"):
            f = getattr(t, n, None)
            if f is not None and not isinstance(f, (types.WrapperDescriptorType, types.BuiltinFunctionType,
                                                     types.MethodDescriptorType, types.MethodWrapperType)):
                k = False
        _PB[t] = k
    return k


_DATA_OK = (types.MemberDescriptorType, types.GetSetDescriptorType)
_NONDATA_OK = (types.FunctionType, staticmethod, classmethod, types.BuiltinFunctionType, types.MethodDescriptorType,
               types.WrapperDescriptorType, types.ClassMethodDescriptorType)


def _plain(o, name):
    """Whether getattr(o, name) is a plain lookup (no package code can run)."""
    try:
        t = type(o)
        if isinstance(o, types.ModuleType):
            return t is types.ModuleType and name in o.__dict__
        if isinstance(o, type):
            if t.__getattribute__ is not type.__getattribute__:
                return False
            for k in t.__mro__:
                if k is not type and k is not object and name in k.__dict__:
                    return False
            for k in o.__mro__:
                if name in k.__dict__:
                    v = k.__dict__[name]
                    g = getattr(type(v), "__get__", None)
                    return g is None or isinstance(v, _NONDATA_OK) or isinstance(v, (property,) + _DATA_OK)
            return False
        if t.__getattribute__ is not object.__getattribute__:
            return False
        d = None
        for k in t.__mro__:
            if name in k.__dict__:
                d = k.__dict__[name]
                break
        else:
            k = None
        if k is not None:
            dt = type(d)
            if hasattr(dt, "__set__") or hasattr(dt, "__delete__"):
                return isinstance(d, _DATA_OK)
        try:
            od = object.__getattribute__(o, "__dict__")
        except AttributeError:
            od = None
        if isinstance(od, dict) and name in od:
            return True
        if k is None:
            return False
        return not hasattr(type(d), "__get__") or isinstance(d, _NONDATA_OK)
    except BaseException:
        return False


def _walk(s, f, chain):
    try:
        v = f()
        plain = True
        for n in chain:
            if plain and not _plain(v, n):
                plain = False
            v = getattr(v, n)
        return v, plain
    except BaseException:
        _set(s, 3)
        raise


def band(s, a, f, chain, ctx):
    if not a:                    # `or` would evaluate the second operand
        _set(s, 3)
        return a
    v, plain = _walk(s, f, chain)
    if not plain:
        _set(s, 3)
    elif ctx:                    # truth only: `or` says True
        _set(s, 1 if (_purebool(v) and v) else 3)
    else:                        # value: `or` returns a
        _set(s, 1 if (v is a and _single(v)) else 3)
    return v


def bor(s, a, f, chain, ctx):
    if a:                        # `and` would evaluate the second operand
        _set(s, 3)
        return a
    v, plain = _walk(s, f, chain)
    if not plain:
        _set(s, 3)
    elif ctx:                    # truth only: `and` says False
        _set(s, 1 if (_purebool(v) and not v) else 3)
    else:
        _set(s, 1 if (v is a and _single(v)) else 3)
    return v
