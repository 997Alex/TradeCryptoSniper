#!/usr/bin/env python3
"""Reconstruct every trade from the logs and test entry filters against it.

Two questions, one parser:

  1. APPROACH SHAPE -- how did the bought side's price arrive at the band?
     Rising into it, decaying into it, or flat? Then: what would a filter on
     that shape have cost in volume and in PnL?

  2. POST-ENTRY DRAWDOWN -- once held, did the position signal it was going
     wrong while there was still time to act?

Both are answered purely from `logs/*.log`; nothing here touches the bot or its
state. Run it against a finished log, or a live one -- open positions are simply
reported as UNKNOWN.

    python3 tools/approach.py                        # current run
    python3 tools/approach.py logs/*.log             # every archived run too

The economics that judge every filter: a win pays about +$0.77 and a loss costs
-$5.11, so ONE LOSS NEEDS ~6.7 WINS TO REPAY. A filter that removes one loss but
also removes seven winners is net negative. Volume is not free.
"""
import re
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIN_USD, LOSS_USD = 0.77, 5.11
WINDOW = 300

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
PRICES = re.compile(r"([A-Z]+)>Y(\d+) N(\d+)")
REMAIN = re.compile(r"t-(\d+)s")
ENTER = re.compile(r"▶ ENTER (\w+) (YES|NO) at (\d+)¢")
RESULT = re.compile(r"(✓|✗) (\S+) (YES|NO) (WON|LOST)")

# Resolution lines name the coin in full; every other line uses the ticker.
NAME = {"Solana": "SOL", "Dogecoin": "DOGE", "Ethereum": "ETH",
        "Bitcoin": "BTC", "Ripple": "XRP", "XRP": "XRP"}


def _secs(ts: str) -> int:
    h, m, s = ts[11:].split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _close_of(t: int) -> int:
    """End of the 5-minute window containing t."""
    return (t // WINDOW + 1) * WINDOW


def parse(paths):
    """samples[(coin, side)] -> [(abs_time, price_cents, seconds_remaining)]"""
    samples = defaultdict(list)
    entries, results = [], []
    for p in paths:
        for ln in ANSI.sub("", Path(p).read_text()).splitlines():
            m = TS.match(ln)
            if not m:
                continue
            t = _secs(m.group(1))
            if ">Y" in ln:
                rm = REMAIN.search(ln)
                rs = int(rm.group(1)) if rm else None
                for coin, y, n in PRICES.findall(ln):
                    samples[(coin, "YES")].append((t, int(y), rs))
                    samples[(coin, "NO")].append((t, int(n), rs))
            e = ENTER.search(ln)
            if e:
                entries.append((t, e.group(1), e.group(2), int(e.group(3)), m.group(1)[11:]))
            r = RESULT.search(ln)
            if r:
                results.append((t, NAME.get(r.group(2), r.group(2)), r.group(3), r.group(4)))
    return samples, entries, results


def attach_outcomes(entries, results):
    """Anchor each result to the WINDOW its position was opened in.

    Pairing results to entries FIFO per (coin, side) is WRONG here: not every
    settlement prints a ✓/✗ line, because two code paths resolve positions and
    only `_resolution_monitor` logs. One missing line then shifts every later
    outcome for that coin by one -- which in practice put a winning trade's
    result onto a losing trade. `data/bucket_stats.json` is authoritative for
    the aggregate count; these log lines are a subset of it.

    A window holds at most one position per (coin, side), and settlement is
    observed 82-263s after the window closes. The acceptance span here is
    deliberately wider than that (900s) because nothing in the bot bounds the
    latency -- `check_resolutions` polls a remote API. But a 900s span covers
    three windows, so it can admit more than one candidate. When it does, this
    REFUSES to pick: guessing is what produced the mis-assignment above.
    Ambiguous and unmatched entries alike stay UNKNOWN and are excluded from
    every statistic rather than being silently attributed.
    """
    out, taken = {}, set()
    for t_r, coin, side, res in results:
        eligible = [i for i, e in enumerate(entries)
                    if i not in taken and e[1] == coin and e[2] == side
                    and _close_of(e[0]) <= t_r <= _close_of(e[0]) + 900]
        if len(eligible) == 1:
            out[eligible[0]] = res
            taken.add(eligible[0])
    return out


def wilson(w: int, n: int, z: float = 1.96):
    """95% interval on a proportion. NOT the normal approximation, which
    collapses near p=1 -- exactly where this strategy lives."""
    p = w / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, c - h) * 100, min(1.0, c + h) * 100


def build(samples, entries, outcome, lookback=30):
    """One row per entry: how the price arrived, and how far it fell after."""
    rows = []
    for i, (t, coin, side, price, clock) in enumerate(entries):
        prior = [pr for (ts_, pr, _) in samples[(coin, side)] if t - lookback <= ts_ < t]
        # Post-entry quotes of the side we hold, bounded to THIS window and to
        # the part of it where an exit would still have been possible. Without
        # the upper bound the scan walks into the next round, where the same
        # coin/side is a different market and routinely quotes 0¢.
        close = _close_of(t)
        after = [pr for (ts_, pr, rs) in samples[(coin, side)]
                 if t < ts_ <= close and rs is not None and rs >= 15]
        rows.append({
            "time": clock, "coin": coin, "side": side, "entry": price,
            "peak_before": max(prior) if prior else None,
            "min_after": min(after) if after else None,
            "outcome": outcome.get(i, "UNKNOWN"),
        })
    return rows


def _pnl(rows):
    return sum(WIN_USD if r["outcome"] == "WON" else -LOSS_USD for r in rows)


def report_approach(rows):
    print("\n=== 1. APPROACH SHAPE ===")
    print("how the bought side's price arrived at the band, vs outcome\n")
    print(f"{'time':>8} {'coin':<5} {'side':<4} {'entry':>5} {'peak30s':>8} {'drop':>5}  outcome")
    for r in rows:
        pk = f"{r['peak_before']}¢" if r["peak_before"] is not None else "-"
        dr = f"{r['peak_before'] - r['entry']:+d}" if r["peak_before"] is not None else "-"
        flag = "  <<<" if r["outcome"] == "LOST" else ""
        print(f"{r['time']:>8} {r['coin']:<5} {r['side']:<4} {r['entry']:>4}¢ "
              f"{pk:>8} {dr:>5}  {r['outcome']}{flag}")

    done = [r for r in rows if r["outcome"] in ("WON", "LOST") and r["peak_before"] is not None]
    if not done:
        return
    print("\nwin rate by shape:")
    buckets = defaultdict(lambda: [0, 0])
    for r in done:
        drop = r["peak_before"] - r["entry"]
        k = "decayed into band" if drop > 0 else "flat" if drop == 0 else "rose into band"
        buckets[k][0 if r["outcome"] == "WON" else 1] += 1
    for k, (w, l) in sorted(buckets.items()):
        print(f"  {k:<20} W{w}/L{l}  n={w+l:<3} wr={100*w/(w+l):.0f}%")

    print(f"\ncost of a decay filter  (baseline: {len(done)} trades, ${_pnl(done):+.2f})")
    for d in (1, 3, 5, 8):
        kept = [r for r in done if r["peak_before"] - r["entry"] <= d]
        if not kept:
            continue
        kw = sum(1 for r in kept if r["outcome"] == "WON")
        print(f"  block drop >{d}¢:  keeps {len(kept):>2}/{len(done)} "
              f"({100*len(kept)/len(done):>3.0f}% of volume)  W{kw}/L{len(kept)-kw}  "
              f"${_pnl(kept):+.2f}")


def report_drawdown(rows):
    print("\n\n=== 2. POST-ENTRY DRAWDOWN ===")
    print("lowest quote of the held side while >=15s remained\n")
    done = [r for r in rows if r["outcome"] in ("WON", "LOST")]
    for r in sorted(done, key=lambda r: (r["outcome"], r["min_after"] if r["min_after"] is not None else 999)):
        lo = f"{r['min_after']}¢" if r["min_after"] is not None else "no sample"
        print(f"{r['time']:>8} {r['coin']:<5} {r['side']:<4} {r['entry']:>4}¢ "
              f"{lo:>11}  {r['outcome']}")
    if not done:
        return

    print("\ncounterfactual stop-loss: exit if the held side is quoted <= X with >=15s left")
    print("(hypothetical -- no exit path exists in the bot; the quote is a mid, not a bid)\n")
    for x in (60, 70, 75, 80):
        for haircut in (0, 5, 10):
            pnl = 0.0
            fired = []
            for r in done:
                if r["min_after"] is not None and r["min_after"] <= x:
                    fired.append(r)
                    shares = 5.0 / (r["entry"] / 100.0)
                    pnl += shares * (max(0, r["min_after"] - haircut) - r["entry"]) / 100.0
                else:
                    pnl += WIN_USD if r["outcome"] == "WON" else -LOSS_USD
            fl = sum(1 for r in fired if r["outcome"] == "LOST")
            print(f"  X={x}¢ slip={haircut}¢:  fires {len(fired):>2}/{len(done)} "
                  f"({fl} losers, {len(fired)-fl} winners)  "
                  f"${_pnl(done):+.2f} -> ${pnl:+.2f}")


def main():
    paths = sys.argv[1:] or [ROOT / "logs" / "crypto_bot.log"]
    samples, entries, results = parse(paths)
    rows = build(samples, entries, attach_outcomes(entries, results))

    report_approach(rows)
    report_drawdown(rows)

    done = [r for r in rows if r["outcome"] in ("WON", "LOST")]
    w = sum(1 for r in done if r["outcome"] == "WON")
    print(f"\n\n=== TOTALS ===")
    print(f"  entries={len(rows)}  resolved={len(done)}  unresolved/unlogged={len(rows)-len(done)}")
    if done:
        lo, hi = wilson(w, len(done))
        print(f"  W{w}/L{len(done)-w}  wr={100*w/len(done):.1f}%  wilson95=[{lo:.1f}%, {hi:.1f}%]")
        print(f"  breakeven is ~87-89%: judge on the LOWER bound, not the point estimate")


if __name__ == "__main__":
    main()
