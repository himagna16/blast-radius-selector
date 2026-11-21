"""Minimal unified-diff (git style) parser/applier. Pure stdlib."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old", "new", "hunks", "is_new", "is_deleted", "binary", "rename_from", "rename_to")

    def __init__(self):
        self.old = self.new = None
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines[(tag, text_with_nl)])
        self.is_new = self.is_deleted = self.binary = False
        self.rename_from = self.rename_to = None

    @property
    def path(self):
        return self.new if self.new not in (None, "/dev/null") else self.old


def _strip(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return p
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(text):
    lines = text.splitlines(keepends=True)
    out, cur, i, n = [], None, 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch()
            out.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln.rstrip("\n"))
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            i += 1
            continue
        if cur is None:
            if ln.startswith("--- "):
                cur = FilePatch()
                out.append(cur)
            else:
                i += 1
                continue
        if ln.startswith("new file mode"):
            cur.is_new = True
        elif ln.startswith("deleted file mode"):
            cur.is_deleted = True
        elif ln.startswith("rename from "):
            cur.rename_from = ln[12:].rstrip("\n")
        elif ln.startswith("rename to "):
            cur.rename_to = ln[10:].rstrip("\n")
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            cur.binary = True
        elif ln.startswith("--- "):
            p = _strip(ln[4:].split("\t")[0])
            if p == "/dev/null":
                cur.is_new = True
            else:
                cur.old = p
        elif ln.startswith("+++ "):
            p = _strip(ln[4:].split("\t")[0])
            if p == "/dev/null":
                cur.is_deleted = True
            else:
                cur.new = p
        else:
            m = HUNK.match(ln)
            if m:
                os_, ol = int(m.group(1)), int(m.group(2) or 1)
                ns, nl = int(m.group(3)), int(m.group(4) or 1)
                body, co, cn = [], 0, 0
                i += 1
                while i < n and (co < ol or cn < nl):
                    h = lines[i]
                    t = h[:1]
                    if t == "\\":
                        if body:
                            tag, txt = body[-1]
                            body[-1] = (tag, txt.rstrip("\n").rstrip("\r"))
                        i += 1
                        continue
                    if t in ("\n", "\r", ""):
                        t, h = " ", " " + h
                    if t == " ":
                        co += 1
                        cn += 1
                    elif t == "-":
                        co += 1
                    elif t == "+":
                        cn += 1
                    else:
                        break
                    body.append((t, h[1:]))
                    i += 1
                if i < n and lines[i].startswith("\\"):
                    if body:
                        tag, txt = body[-1]
                        body[-1] = (tag, txt.rstrip("\n").rstrip("\r"))
                    i += 1
                cur.hunks.append((os_, ol, ns, nl, body))
                continue
        i += 1
    return out


def apply_text(old_text, fp):
    """Return new text for one FilePatch (None if deleted). Raises ValueError on mismatch."""
    if fp.is_deleted:
        return None
    src = old_text.splitlines(keepends=True) if old_text else []
    res, pos = [], 0
    for os_, ol, ns, nl, body in fp.hunks:
        start = os_ - 1 if ol else os_
        want = [t for tag, t in body if tag != "+"]
        if src[start:start + len(want)] != want:
            # tolerate drift / trailing-newline differences: search nearby
            found = None
            norm = [w.rstrip("\r\n") for w in want]
            for d in range(0, max(len(src), 1) + 1):
                for s in (start - d, start + d):
                    if s < pos or s < 0 or s + len(want) > len(src):
                        continue
                    if [x.rstrip("\r\n") for x in src[s:s + len(want)]] == norm:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                raise ValueError("hunk does not apply: %s @%d" % (fp.path, os_))
            start = found
        res.extend(src[pos:start])
        for tag, t in body:
            if tag != "-":
                res.append(t)
        pos = start + len(want)
    res.extend(src[pos:])
    return "".join(res)


def changed_lines(fp):
    """(old_lines_removed_or_adjacent, new_lines_added) as 1-based line numbers.
    For pure insertions, old side reports the line before/after the insertion point."""
    old_del, new_add, old_anchor = [], [], []
    for os_, ol, ns, nl, body in fp.hunks:
        o = os_ if ol else os_ + 1
        nn = ns if nl else ns + 1
        for tag, _ in body:
            if tag == " ":
                o += 1
                nn += 1
            elif tag == "-":
                old_del.append(o)
                o += 1
            else:
                new_add.append(nn)
                old_anchor.append(o)  # old line that will follow the inserted line
                nn += 1
    return old_del, new_add, old_anchor


def apply_to_tree(root, text):
    import os
    done = []
    for fp in parse(text):
        if fp.binary:
            done.append((fp.path, "binary"))
            continue
        src_path = fp.rename_from or fp.old
        old = None
        if not fp.is_new and src_path:
            with open(os.path.join(root, src_path), newline="") as f:
                old = f.read()
        new = apply_text(old, fp) if (fp.hunks or fp.is_new or fp.is_deleted) else old
        if fp.is_deleted or (fp.rename_from and fp.rename_to and fp.rename_from != fp.rename_to):
            try:
                os.remove(os.path.join(root, src_path))
            except OSError:
                pass
        if new is not None:
            dst = os.path.join(root, fp.rename_to or fp.new or fp.old)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", newline="") as f:
                f.write(new)
        done.append((fp.path, "ok"))
    return done


if __name__ == "__main__":
    import sys
    print(apply_to_tree(sys.argv[1], open(sys.argv[2], newline="").read()))
