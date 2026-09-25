"""Record setups the live rules REFUSED, so they can be scored later without risking money.

The retest rule blocks a real category of trade. Measured over 38 decided setups:

    RETESTED  n=33   61% at the zone  ->  15% entered at market
    NO RETEST n= 5  100% at the zone  ->  80% entered at market

87% of setups retest, and entering those early is a disaster. The 13% that never come
back do run — but scoring them at the zone edge is circular ("price never returned" IS
the winning move) and they are unfillable there anyway. The open question is whether
anything visible AT ARM TIME separates the two, and n=5 cannot answer it.

So: keep the live rule strict, and write down every setup it refuses. A few weeks of
this is a real sample instead of five. Nothing here can place an order or change a rule
— it only appends.

WHAT IT RECORDS, deliberately more than "market entry": a no-retest setup does not have
to be taken at market, which is the worst possible fill and the one that scored 15%.
Three candidate entries are stored so the scorer can compare them fairly later:

    market   the live price at arm time      (worst fill, no waiting)
    part25   25% of the way back into the zone  (a shallow pullback that never reaches
    part50   50% of the way back                 the zone still fills these)

All three share the same stop, so whichever fills gets a different R and a different
win rate. That comparison is the whole point of collecting this.
"""
import os

from bot import trade_ledger

DEFAULT_NAME = "shadow_no_retest"


def shadow_path(base_dir=None):
    return trade_ledger.ledger_path(DEFAULT_NAME, base_dir)


def candidate_entries(price, zone_lo, zone_hi, is_long):
    """{name: price} for the fills a no-retest setup could plausibly get.

    For a LONG the zone sits below price, so coming back INTO it means falling toward
    zone_hi. part25/part50 are partial retracements from the live price toward the near
    edge — a shallow dip that never reaches the zone still fills them.
    """
    try:
        px = float(price); lo = float(zone_lo); hi = float(zone_hi)
    except (TypeError, ValueError):
        return {}
    if px <= 0 or lo > hi:
        return {}
    near = hi if is_long else lo          # the edge price would touch first coming back
    return {
        "market": px,
        "part25": px + (near - px) * 0.25,
        "part50": px + (near - px) * 0.50,
    }


def record_refusal(symbol, side, price, zone_lo, zone_hi, stop, target, ts,
                   reason="no_retest", path=None, extra=None):
    """Append one refused setup. Returns True when it is on disk.

    Fail-soft on purpose: this is observation, never a trading decision, so it must not
    be able to raise into the entry path it is observing.
    """
    try:
        is_long = str(side).upper() == "LONG"
        row = {
            "ts": ts, "symbol": symbol, "side": side, "reason": reason,
            "price": price, "zone_lo": zone_lo, "zone_hi": zone_hi,
            "stop": stop, "target": target,
            "entries": candidate_entries(price, zone_lo, zone_hi, is_long),
        }
        if extra:
            row.update(extra)
        return trade_ledger.append_row(path or shadow_path(), row)
    except Exception:
        return False


def load(path=None):
    return trade_ledger.read_rows(path or shadow_path())
