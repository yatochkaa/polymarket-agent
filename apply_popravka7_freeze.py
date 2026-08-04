#!/usr/bin/env python3
# apply_popravka7_freeze.py
# Second (recording) commit bookkeeping for the Popravka 7 freeze.
# PS 5.1 SAFE: run as a real .py file. Do NOT use '<<' heredoc in PowerShell.
# Idempotent: re-running does not duplicate marks.
import io, os, sys, argparse, tempfile, shutil

CANCEL_MARK = "> \u041e\u0422\u041c\u0415\u041d\u0415\u041d\u0410 2026-08-02 \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u043e\u0439 7. \u041f\u0440\u0438\u0447\u0438\u043d\u0430: \u0438\u0437\u043c\u0435\u0440\u044f\u043b\u0430 \u0440\u0435\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u0439 PnL, \u0430 \u043d\u0435 CLV (\u0432\u0435\u043b\u0438\u0447\u0438\u043d\u0430 \u043a\u043e\u0434\u0438\u0440\u0443\u0435\u0442 \u0438\u0441\u0445\u043e\u0434)."
CANCEL_TAG = "\u041e\u0422\u041c\u0415\u041d\u0415\u041d\u0410 2026-08-02 \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u043e\u0439 7"
PLACEHOLDER = "<\u0432\u043f\u0438\u0441\u044b\u0432\u0430\u0435\u0442\u0441\u044f \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u043c \u043a\u043e\u043c\u043c\u0438\u0442\u043e\u043c>"
HDR_PREFIX = "\u0425\u044d\u0448 \u043a\u043e\u043c\u043c\u0438\u0442\u0430:"
POPRAVKI = "\u041f\u041e\u041f\u0420\u0410\u0412\u041a\u0418"
P7_TAG = "\u041f\u043e\u043f\u0440\u0430\u0432\u043a\u0430 7 (2026-08-02)"

def _p7_line(h):
    return "- \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u0430 7 (2026-08-02): \u043f\u0440\u0435\u0434\u043c\u0430\u0442\u0447\u0435\u0432\u0430\u044f \u0437\u0430\u043a\u0440\u044b\u0432\u0430\u044e\u0449\u0430\u044f \u043b\u0438\u043d\u0438\u044f \u043a\u0430\u043a \u0438\u0437\u043c\u0435\u0440\u044f\u0435\u043c\u0430\u044f \u0432\u0435\u043b\u0438\u0447\u0438\u043d\u0430; \u0441\u043c. POPRAVKA7.md. \u0425\u044d\u0448 \u0437\u0430\u043c\u043e\u0440\u043e\u0437\u043a\u0438: %s." % h

def _p5_line():
    return "- \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u0430 5 (2026-08-01): \u041e\u0422\u041c\u0415\u041d\u0415\u041d\u0410 2026-08-02 \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u043e\u0439 7 (\u0438\u0437\u043c\u0435\u0440\u044f\u043b\u0430 \u0440\u0435\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u0439 PnL, \u0430 \u043d\u0435 CLV)."

def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()

def write(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

def mark_popravka5(root):
    p = os.path.join(root, "POPRAVKA5.md")
    if not os.path.exists(p):
        return ("POPRAVKA5.md", "MISSING", None)
    lines = read(p).split("\n")
    if any(CANCEL_TAG in ln for ln in lines):
        return ("POPRAVKA5.md", "ALREADY", None)
    idx = 1
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            idx = i + 1
            break
    lines.insert(idx, CANCEL_MARK)
    write(p, "\n".join(lines))
    lo = max(0, idx - 1)
    return ("POPRAVKA5.md", "MARKED", "\n".join(lines[lo:idx + 2]))

def fill_hash_popravka7(root, h):
    p = os.path.join(root, "POPRAVKA7.md")
    if not os.path.exists(p):
        return ("POPRAVKA7.md", "MISSING", None)
    lines = read(p).split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(HDR_PREFIX):
            if h in ln:
                return ("POPRAVKA7.md", "ALREADY", ln)
            if PLACEHOLDER in ln:
                lines[i] = ln.replace(PLACEHOLDER, h)
                write(p, "\n".join(lines))
                return ("POPRAVKA7.md", "HASH_SET", lines[i])
            return ("POPRAVKA7.md", "HEADER_UNEXPECTED", ln)
    return ("POPRAVKA7.md", "NO_HEADER", None)

def update_prereg(root, h):
    p = os.path.join(root, "PREREGISTRATION.md")
    if not os.path.exists(p):
        return ("PREREGISTRATION.md", "MISSING", None)
    lines = read(p).split("\n")
    if any(P7_TAG in ln for ln in lines):
        return ("PREREGISTRATION.md", "ALREADY", None)
    anchor = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if POPRAVKI in s.upper() and (s.startswith("#") or len(s) <= 24):
            anchor = i
            break
    if anchor is None:
        return ("PREREGISTRATION.md", "NO_ANCHOR", None)
    lines.insert(anchor + 1, _p5_line())
    lines.insert(anchor + 1, _p7_line(h))
    write(p, "\n".join(lines))
    return ("PREREGISTRATION.md", "UPDATED", "\n".join(lines[anchor:anchor + 4]))

def selftest():
    d = tempfile.mkdtemp()
    try:
        write(os.path.join(d, "POPRAVKA5.md"), "# \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u0430 5\n\n\u0442\u0435\u043a\u0441\u0442\n")
        write(os.path.join(d, "POPRAVKA7.md"),
              "# \u041f\u041e\u041f\u0420\u0410\u0412\u041a\u0410 7\n\u0414\u0430\u0442\u0430: 2026-08-02\n" + HDR_PREFIX + " " + PLACEHOLDER + "   (x)\n\n\u0442\u0435\u043b\u043e \u00ab" + HDR_PREFIX + " " + PLACEHOLDER + "\u00bb.\n")
        write(os.path.join(d, "PREREGISTRATION.md"), "# x\n\n## " + POPRAVKI + "\n- \u041f\u043e\u043f\u0440\u0430\u0432\u043a\u0430 2\n")
        h = "7b65c2a"
        assert mark_popravka5(d)[1] == "MARKED"
        assert mark_popravka5(d)[1] == "ALREADY"
        t5 = read(os.path.join(d, "POPRAVKA5.md")).split("\n")
        assert t5[0].startswith("# ") and t5[1] == CANCEL_MARK, t5
        assert fill_hash_popravka7(d, h)[1] == "HASH_SET"
        assert fill_hash_popravka7(d, h)[1] == "ALREADY"
        t7 = read(os.path.join(d, "POPRAVKA7.md"))
        assert (HDR_PREFIX + " " + h + "   (x)") in t7, "header must carry hash"
        assert ("\u00ab" + HDR_PREFIX + " " + PLACEHOLDER + "\u00bb") in t7, "body quote must be preserved"
        assert update_prereg(d, h)[1] == "UPDATED"
        assert update_prereg(d, h)[1] == "ALREADY"
        tp = read(os.path.join(d, "PREREGISTRATION.md"))
        assert P7_TAG in tp and ("\u0425\u044d\u0448 \u0437\u0430\u043c\u043e\u0440\u043e\u0437\u043a\u0438: " + h) in tp, tp
        assert CANCEL_TAG in tp
        lp = tp.split("\n")
        ai = next(i for i, l in enumerate(lp) if POPRAVKI in l.upper())
        assert P7_TAG in lp[ai + 1] and CANCEL_TAG in lp[ai + 2], lp[ai:ai + 3]
        print("SELFTEST OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["apply", "selftest"])
    ap.add_argument("--hash", default=None)
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    if a.mode == "selftest":
        selftest(); return
    if not a.hash:
        print("ERROR: --hash required (short freeze hash, e.g. 7b65c2a)"); sys.exit(2)
    results = [mark_popravka5(a.root), fill_hash_popravka7(a.root, a.hash), update_prereg(a.root, a.hash)]
    print("=== apply_popravka7_freeze (hash=%s) ===" % a.hash)
    for name, status, ctx in results:
        print("%-20s %s" % (name, status))
        if ctx:
            print("  --- context ---")
            for ln in ctx.split("\n"):
                print("  | " + ln)
    if any(n == "PREREGISTRATION.md" and s == "NO_ANCHOR" for n, s, _ in results):
        p = os.path.join(a.root, "PREREGISTRATION.md")
        if os.path.exists(p):
            lines = read(p).split("\n")
            print("  PREREG anchor not found; lines 90-106 for MANUAL placement:")
            for i in range(89, min(106, len(lines))):
                print("  %4d| %s" % (i + 1, lines[i]))
    ok = all(s in ("MARKED", "ALREADY", "HASH_SET", "UPDATED") for _, s, _ in results)
    print("=== %s ===" % ("OK: verify contexts above, then git add the 3 files" if ok else "REVIEW: some steps need manual action"))

if __name__ == "__main__":
    main()
