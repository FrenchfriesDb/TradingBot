"""What happened in one trading session — the questions we actually care about.

WHY (2026-10-03). Three changes shipped in 48h with ZERO live trading history: the 5-minute
loop (15M -> 5M), the 1R target, and the crypto 1.4x tap gate. Each was measured, none has
been observed. This turns "watch it Monday" into something that does not depend on anyone
remembering to look.

    python3 tools/session_report.py              # today
    python3 tools/session_report.py 2026-10-05   # a specific date

Answers, in order of what would change a decision:
  1. Did the loop actually run at 5m, or did it fall behind? (the cadence change is worth
     +0.05R -> +0.19R ONLY if the bot keeps up)
  2. Did the iteration budget fire? (passes overrunning = the precondition failing)
  3. Did the chase refusals drop? ("the move left without us" was 12 of 30 on the old loop;
     the 1-minute reconstruction says they should mostly vanish at 5m)
  4. Did anything trade, and at what R?
  5. Did any flatten get REJECTED? (the GOOGL livelock, fixed 856c754 — if this ever
     prints again the fix did not hold)

CAVEAT — THE TWO LOGS KEEP DIFFERENT CLOCKS. stock_bot stamps every line in LOCAL time;
binance_bot writes an undated stream under "  YYYY-MM-DD HH:MM UTC" block headers. So the
same calendar date is NOT the same window for both, and a crypto session spans two local
dates. Read the crypto numbers as "the UTC day", not "the trading day".
"""
import collections
import datetime as dt
import os
import re
import sys

LOGS = os.path.join(os.path.expanduser("~"), "Library", "Logs", "debbiela")


def section(t):
    print(f"\n  {t}\n  " + "-" * max(44, len(t)))


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    print(f"\n  SESSION REPORT — {day}")
    print("  " + "=" * 56)

    for bot, expect_iters in (("stock_bot", 78), ("binance_bot", None)):
        path = os.path.join(LOGS, f"{bot}.log")
        try:
            raw = open(path, encoding="utf-8", errors="replace").read().splitlines(True)
            # Two log shapes. stock_bot prefixes every line with the date. binance_bot
            # writes an undated stream under periodic "  YYYY-MM-DD HH:MM UTC" headers, so
            # a per-line date filter finds nothing and reports a healthy bot as silent —
            # which is exactly what the first version of this tool did.
            if any(day in l[:30] for l in raw[:5000]) and bot == "stock_bot":
                lines = [l for l in raw if day in l[:30]]
            else:
                lines, keep = [], False
                hdr = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T]")
                for l in raw:
                    m = hdr.match(l)
                    if m:
                        keep = (m.group(1) == day)
                    if keep:
                        lines.append(l)
                if not lines:                      # fall back to a plain per-line match
                    lines = [l for l in raw if day in l[:30]]
        except OSError as e:
            print(f"\n  {bot}: log unreadable ({e})")
            continue
        if not lines:
            print(f"\n  {bot}: NOTHING LOGGED on {day}")
            continue
        blob = "".join(lines)

        section(f"{bot} — {len(lines):,} lines")

        # 1. cadence
        took = [float(m) for m in re.findall(r"Took ([0-9.]+)s", blob)]
        if took:
            took_s = sorted(took)
            print(f"    iterations completed   {len(took)}"
                  + (f"  of ~{expect_iters} possible at 5m" if expect_iters else ""))
            print(f"    iteration time         median {took_s[len(took_s)//2]:.0f}s   "
                  f"p90 {took_s[int(len(took_s)*0.9)]:.0f}s   max {took_s[-1]:.0f}s")
            over = sum(1 for t in took if t > 300)
            if over:
                print(f"    ** {over} iteration(s) exceeded the 5-minute interval **")

        # 2. the budget guard
        budget = len(re.findall(r"Iteration budget", blob))
        print(f"    budget deferrals       {budget}"
              + ("   <-- passes are OVERRUNNING; cadence precondition failing" if budget else ""))

        # 3. refusals
        ref = collections.Counter(re.findall(r"🚫 ([A-Za-z][A-Za-z ]{3,40})", blob))
        if ref:
            print("    refusals:")
            for k, n in ref.most_common(8):
                tag = ""
                if "Tap not actionable" in k:
                    tag = "   <-- the chase guard; should be RARE at 5m"
                print(f"      {n:>4}  {k.strip()}{tag}")

        # 4. trades
        entries = len(re.findall(r"ENTRY|Submitting|BUY order|🎯 ENTRY", blob))
        fills = re.findall(r"filled|FILLED", blob)
        print(f"    entry-ish lines        {entries}     fill mentions {len(fills)}")

        # 5. the livelock canary
        rejected = len(re.findall(r"close REJECTED", blob))
        naked = len(re.findall(r"Re-attached protection", blob))
        print(f"    flatten REJECTED       {rejected}"
              + ("   <-- the GOOGL livelock shape is BACK" if rejected else ""))
        print(f"    protection re-attached {naked}"
              + ("   <-- positions going naked" if naked > 2 else ""))

    section("verdict checklist")
    print("    [ ] stock iterations near the 5m expectation (not 16-of-26 like 10-02)")
    print("    [ ] zero 'Iteration budget' deferrals")
    print("    [ ] 'Tap not actionable' much rarer than the old 12-per-session")
    print("    [ ] any entries at all — and their R, from the ledger")
    print("    [ ] zero 'close REJECTED'")
    print()


if __name__ == "__main__":
    main()
