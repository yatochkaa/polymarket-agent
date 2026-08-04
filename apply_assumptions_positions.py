#!/usr/bin/env python3
# apply_assumptions_positions.py
# Idempotently append the 2026-08-02 data-api/positions assumption to ASSUMPTIONS.md.
# Modes:
#   apply --root .
#   selftest
# Content is verbatim from the user; the script adds/derives NO numbers.
import io, os, sys, tempfile, shutil

MARKER = "## 2026-08-02 \u2014 data-api/positions: \u0442\u043e\u043b\u044c\u043a\u043e \u043e\u0442\u043a\u0440\u044b\u0442\u044b\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438"

BLOCK = "\n".join([
    MARKER,
    "",
    "- data-api/positions \u043e\u0442\u0434\u0430\u0451\u0442 \u0422\u041e\u041b\u042c\u041a\u041e \u043e\u0442\u043a\u0440\u044b\u0442\u044b\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438.",
    "- \u041f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043e \u043d\u0430 \u043a\u043e\u0448\u0435\u043b\u044c\u043a\u0435 0x03dc85f8...: 307 \u0440\u044b\u043d\u043a\u043e\u0432 \u0432 /trades, 8 \u0432 /positions, \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u044e\u0442 299, \u043e\u0431\u0440\u0430\u0442\u043d\u044b\u0445 \u0441\u043b\u0443\u0447\u0430\u0435\u0432 0.",
    "- \u041f\u043e\u043b\u044f \u043f\u0440\u0438\u0431\u044b\u043b\u0438 (cashPnl, realizedPnl, avgPrice, size) \u0434\u0430\u044e\u0442\u0441\u044f \u043f\u043e \u043a\u0430\u0436\u0434\u043e\u043c\u0443 \u0440\u044b\u043d\u043a\u0443, \u043d\u043e \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u043e\u0442\u043a\u0440\u044b\u0442\u044b\u0445.",
    "- \u0421\u043b\u0435\u0434\u0441\u0442\u0432\u0438\u0435: \u0424\u0438\u043b\u044c\u0442\u0440 4 \u0447\u0435\u0440\u0435\u0437 /positions \u043d\u0435\u0432\u043e\u0437\u043c\u043e\u0436\u0435\u043d; \u0441\u043a\u0430\u043d \u0446\u0435\u043f\u043e\u0447\u043a\u0438 \u0432 \u043f\u043b\u0430\u043d\u0435 \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f.",
    "- \u041f\u043e\u0441\u0442\u0440\u0430\u043d\u0438\u0447\u043d\u043e\u0441\u0442\u0438 \u043d\u0435\u0442: limit \u0434\u043e 1000 \u043e\u0442\u0434\u0430\u0451\u0442 \u0442\u043e\u0442 \u0436\u0435 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442, offset \u0437\u0430 \u043f\u0440\u0435\u0434\u0435\u043b\u043e\u043c \u0434\u0430\u0451\u0442 \u043f\u0443\u0441\u0442\u043e.",
    "- \u041f\u043e\u0431\u043e\u0447\u043d\u043e: /positions \u043e\u0442\u0434\u0430\u0451\u0442 oppositeAsset \u2014 \u0433\u043e\u0442\u043e\u0432\u043e\u0435 \u0441\u043e\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0438\u0435 \u0442\u043e\u043a\u0435\u043d\u0430 \u0438 \u043a\u043e\u043c\u043f\u043b\u0435\u043c\u0435\u043d\u0442\u0430.",
    "- \u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a: probes/glm/probe2_run.log, probes/glm/probe2_summary.json",
    "",
])

def read(p):
    return io.open(p, encoding="utf-8").read()

def write(p, s):
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)

def apply(root):
    p = os.path.join(root, "ASSUMPTIONS.md")
    if not os.path.exists(p):
        print("ASSUMPTIONS.md   NOT_FOUND (aborting; check --root or create the file)")
        return 2
    s = read(p)
    if MARKER in s:
        print("ASSUMPTIONS.md   SKIP (marker already present)")
    else:
        if not s.endswith("\n"):
            s += "\n"
        if not s.endswith("\n\n"):
            s += "\n"
        s += BLOCK
        write(p, s)
        print("ASSUMPTIONS.md   APPENDED")
    tail = read(p).split("\n")[-12:]
    print("--- context (tail) ---")
    for ln in tail:
        print("| " + ln)
    return 0

def selftest():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "ASSUMPTIONS.md")
        write(p, "# ASSUMPTIONS\n\n- prior assumption\n")
        apply(d)
        c1 = read(p)
        assert c1.count(MARKER) == 1, "marker should appear once after first apply"
        assert "oppositeAsset" in c1
        assert "prior assumption" in c1, "must preserve prior content"
        apply(d)
        c2 = read(p)
        assert c2 == c1, "second apply must be a no-op"
        assert c2.count(MARKER) == 1, "marker must stay unique"
        rc = apply(os.path.join(d, "nope"))
        assert rc == 2, "missing file must return 2"
        print("SELFTEST OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main(argv):
    if not argv:
        print("usage: apply_assumptions_positions.py [apply --root . | selftest]")
        return 1
    mode = argv[0]
    if mode == "selftest":
        selftest()
        return 0
    if mode == "apply":
        root = "."
        if "--root" in argv:
            root = argv[argv.index("--root") + 1]
        return apply(root)
    print("unknown mode: " + mode)
    return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
