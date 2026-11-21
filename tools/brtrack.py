"""Build-time: record which keys each test looks up in the class-level dicts and sets of sqlglot.

After everything is imported, every dict / set / frozenset held directly by a sqlglot class is
swapped for a subclass that reports reads. A read of one key reports that key (hit or miss);
anything that walks the container (iteration, copying, comparison, set algebra) reports "*".
"""
import enum
import sys

LOG = set()      # (module, class qualname, attribute, key repr | "*")
ACTIVE = [False]


from brstate import keyrepr  # noqa: E402  (same key text as the state fingerprints)


def _one(self, k):
    if ACTIVE[0]:
        r = keyrepr(k)
        LOG.add(self._br + (r if r is not None else "*",))


def _all(self):
    if ACTIVE[0]:
        LOG.add(self._br + ("*",))


class TDict(dict):
    __slots__ = ("_br",)

    def __getitem__(self, k):
        _one(self, k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        _one(self, k)
        return dict.get(self, k, d)

    def __contains__(self, k):
        _one(self, k)
        return dict.__contains__(self, k)

    def __iter__(self):
        _all(self)
        return dict.__iter__(self)

    def keys(self):
        _all(self)
        return dict.keys(self)

    def values(self):
        _all(self)
        return dict.values(self)

    def items(self):
        _all(self)
        return dict.items(self)

    def copy(self):
        _all(self)
        return dict.copy(self)

    def __len__(self):
        _all(self)
        return dict.__len__(self)

    def __eq__(self, o):
        _all(self)
        return dict.__eq__(self, o)

    def __ne__(self, o):
        _all(self)
        return dict.__ne__(self, o)

    def __or__(self, o):
        _all(self)
        return dict.__or__(self, o)

    def __ror__(self, o):
        _all(self)
        return dict.__ror__(self, o)

    def __reversed__(self):
        _all(self)
        return dict.__reversed__(self)

    def __reduce_ex__(self, p):
        _all(self)
        return (dict, (dict(dict.items(self)),))

    __hash__ = None


def _mkset(base):
    class TSet(base):
        __slots__ = ("_br",)

        def __contains__(self, k):
            _one(self, k)
            return base.__contains__(self, k)

        def __iter__(self):
            _all(self)
            return base.__iter__(self)

        def __len__(self):
            _all(self)
            return base.__len__(self)

    def wrap(name):
        orig = getattr(base, name)

        def f(self, *a, **k):
            _all(self)
            r = orig(self, *a, **k)
            return r
        f.__name__ = name
        return f

    for name in ("__eq__", "__ne__", "__le__", "__lt__", "__ge__", "__gt__", "__and__", "__rand__", "__or__", "__ror__",
                 "__sub__", "__rsub__", "__xor__", "__rxor__", "union", "intersection", "difference",
                 "symmetric_difference", "issubset", "issuperset", "isdisjoint", "copy"):
        setattr(TSet, name, wrap(name))
    if base is frozenset:
        TSet.__hash__ = lambda self: base.__hash__(self)
    else:
        TSet.__hash__ = None
    return TSet


TSetM = _mkset(set)
TFrozen = _mkset(frozenset)


def install(prefix="sqlglot"):
    """Swap the containers. Returns how many were swapped."""
    made = {}       # id(original) -> tracking copy
    keep = []
    owners = []
    for name, mod in sorted(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
        seen = set()
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            for k, v in list(vars(c).items()):
                if isinstance(v, type) and v.__module__ == name and v.__qualname__.startswith(c.__qualname__ + "."):
                    stack.append(v)
                    continue
                t = type(v)
                if t is dict:
                    new = TDict(v)
                elif t is set:
                    new = TSetM(v)
                elif t is frozenset:
                    new = TFrozen(v)
                else:
                    continue
                if id(v) in made:
                    continue
                new._br = (name, c.__qualname__, k)
                made[id(v)] = new
                keep.append(v)
                owners.append((c, k))
    # every class attribute and module global that is one of those objects now points at the copy
    n = 0
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        for k, v in list(vars(mod).items()):
            if id(v) in made:
                setattr(mod, k, made[id(v)])
                n += 1
            if isinstance(v, type) and v.__module__ == name:
                stack = [v]
                seen = set()
                while stack:
                    c = stack.pop()
                    if c in seen:
                        continue
                    seen.add(c)
                    for ck, cv in list(vars(c).items()):
                        if isinstance(cv, type) and cv.__module__ == name and cv.__qualname__.startswith(c.__qualname__ + "."):
                            stack.append(cv)
                        elif id(cv) in made:
                            try:
                                setattr(c, ck, made[id(cv)])
                                n += 1
                            except (AttributeError, TypeError):
                                pass
    install.keep = keep
    return n
