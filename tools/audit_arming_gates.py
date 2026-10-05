#!/usr/bin/env python3
"""Which gates does each arming path actually apply?

WHY THIS EXISTS. On 2026-10-05 the operator looked at one AERO chart and said "if it was
an AMD, it needs a strong breakout". They were right: both AMD branches armed on
manipulation alone — sweep + daily trend + ATR + depth + "a zone exists" — and never asked
whether price displaced out of the sweep. That gap shipped 2026-07-01 and survived three
months, a funnel instrumentation pass, a stale-zone sweep, a chase sweep, a target sweep and
a displacement sweep. Every one of those measured the strategy's OUTPUT. None asked whether
each arming path applies the gates the strategy claims to have.

The recurring defect in this repo is not a wrong number. It is a rule that exists on one
path and not the others — the R:R gates, the stop fills, the fee legs, the HTF frame, the
banners, the chart symbols, the shadow ledger. Nine instances in a single session. A tool
that prints the matrix finds the tenth before an operator does.

This is a LINT, not a judge: it reports which gate markers appear in each arming path's
guard context. A blank is a question to answer, not proof of a bug.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: gate name -> regex that evidences it
GATES = {
    "displacement": r"has_displacement|detect_displacement|amd_distribution_confirmed",
    "daily-trend":  r"daily_trend\s*==|dtrend\s*==|get_daily_trend",
    "ATR floor":    r"atr_pct\s*>=|atr_gate_for|atr_min",
    "sweep-depth":  r"sweep_depth|>=\s*0\.003",
    "zone-found":   r"find_demand_zone|find_supply_zone|detect_displacement_fvg|f\d\b|found",
}

LOOKBACK = 70   # lines of guard context above the arming call


def audit(path, label):
    src = path.read_text().splitlines()
    # Skip comments and the definition. The first version matched a COMMENT that merely
    # mentioned arm_zone() and reported it as an ungated arming path — a lint that cries
    # wolf is a lint that gets ignored, which is how the real gap survived three months.
    def _is_call(line):
        code = line.split("#", 1)[0]
        if "arm_zone(" in code and "def arm_zone" not in code:
            return True
        # the stock bot has no arm_zone(); it arms by setting the zone directly
        return bool(re.search(r"self\.fvg_low\[\w+\]\s*=\s*(?!None)", code))
    sites = [i for i, l in enumerate(src) if _is_call(l)]
    print(f"\n{'='*86}\n  {label} — {len(sites)} arming sites\n{'='*86}")
    print(f"  {'line':>6}  {'what arms it':<30} " + " ".join(f"{g[:12]:>13}" for g in GATES))
    print("  " + "-" * 84)
    gaps = []
    for i in sites:
        ctx = "\n".join(src[max(0, i - LOOKBACK):i + 1])
        # name the path from the nearest print/comment above it
        name = "?"
        for j in range(i, max(0, i - LOOKBACK), -1):
            m = re.search(r"(AMD \(PRIORITY\)[^\"']*|STEP \d[^\"']*|trend-follow|wedge|breaker|CHoCH|chase)",
                          src[j], re.I)
            if m:
                name = m.group(1)[:30].strip()
                break
        marks = []
        for g, pat in GATES.items():
            hit = bool(re.search(pat, ctx))
            marks.append("      ✓      " if hit else "      ·      ")
            if not hit and g == "displacement":
                gaps.append((i + 1, name))
        print(f"  {i+1:>6}  {name:<30} " + "".join(marks))
    return gaps


def main():
    gaps = []
    gaps += audit(ROOT / "binance_bot.py", "binance_bot.py (crypto)")
    # The stock bot has arming paths too. Auditing only the crypto file is how the AMD
    # distribution gate got fixed on one bot and not the other — a tool built to find
    # "a rule on one path and not the others" that itself only looked at one file.
    gaps += audit(ROOT / "bot" / "strategy.py", "bot/strategy.py (stocks)")
    print("\n  ✓ = a marker for that gate appears in the arming path's guard context")
    print("  · = NO marker found. Not proof of a bug — a question to answer.")
    if gaps:
        print(f"\n  ⚠️  {len(gaps)} arming path(s) with NO displacement evidence:")
        for ln, nm in gaps:
            print(f"      line {ln}: {nm}")
        print("\n  A path that arms without displacement is taking a setup on a level alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
