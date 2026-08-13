"""
JOURNAL MODEL — learns a directional lean from your own trade history.

What this is: a naive-Bayes-style scorer over the entry_* signal columns in
scalp_journal.csv. For each of the 8 precheck signals, it learns "when this
signal read bullish/bearish/neutral/caution at entry, how often did the
trade end up a winner historically?" and combines those per-signal win rates
into one lean via log-odds.

What this deliberately is NOT: a calibrated probability, a backtest, or
anything that should size a position. It is trained on paper trades from one
account, over whatever window of market regimes happened to occur, with no
walk-forward validation and no out-of-sample test. Same spirit as
precheck.py's own checklist — evidence, not a forecast.

The honesty mechanism is the abstain gate: below MIN_TRADES closed, labeled
trades, score_setup() refuses to produce a lean at all and says how many
more trades are needed. A number computed from 16 trades is not more
trustworthy than no number — it's just a number that LOOKS trustworthy,
which is worse. Precision comes with the sample, not before it.
"""

import csv
import math
from pathlib import Path

from precheck import SIGNAL_NAMES, SIGNAL_KEYS, flatten_signals

JOURNAL = Path("scalp_journal.csv")
EXCLUDED_SCOPE_CLASS = "manual_out_of_envelope"

MIN_TRADES = 30          # closed, signal-tagged trades before any lean is shown
MIN_PER_DIRECTION = 5    # a signal/direction pair needs this many observations
                          # before it's allowed to move the score at all
ALPHA = 1.0               # Laplace smoothing pseudo-count


def _load_labeled_trades(mode=None):
    """Rows that have entry-signal data (i.e. logged after this feature
    shipped) and a usable win/loss label. mode='paper'/'live' filters; None
    keeps both."""
    if not JOURNAL.exists():
        return []
    with JOURNAL.open(newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        if r.get("scope_class") == EXCLUDED_SCOPE_CLASS:
            continue
        if mode and r.get("mode") != mode:
            continue
        if not r.get("entry_verdict"):   # blank = predates signal tracking
            continue
        try:
            realized = float(r.get("realized_pct", ""))
        except ValueError:
            continue
        r["_win"] = realized > 0
        out.append(r)
    return out


def _counts(rows):
    """{signal_key: {direction: {'win': n, 'loss': n}}}"""
    counts = {k: {} for k in SIGNAL_KEYS}
    for r in rows:
        for key in SIGNAL_KEYS:
            direction = r.get(f"entry_{key}", "")
            if not direction:
                continue
            bucket = counts[key].setdefault(direction, {"win": 0, "loss": 0})
            bucket["win" if r["_win"] else "loss"] += 1
    return counts


def _log_odds_contribution(bucket, total_wins, total_losses, n_categories):
    """Smoothed log(P(direction|win)) - log(P(direction|loss)) for one signal."""
    p_win = (bucket["win"] + ALPHA) / (total_wins + ALPHA * n_categories)
    p_loss = (bucket["loss"] + ALPHA) / (total_losses + ALPHA * n_categories)
    return math.log(p_win) - math.log(p_loss)


def score_setup(current_precheck_result, mode="paper"):
    """Score a live setup (a run_precheck() dict) against trade history.

    Returns either:
      {"available": False, "n": <trades so far>, "min": MIN_TRADES,
       "reason": "..."}
    or:
      {"available": True, "n": <trades used>, "wins": .., "losses": ..,
       "lean": "favorable"|"unfavorable"|"even",
       "historical_win_rate": <overall win rate, for context>,
       "log_odds": <raw score>, "signals_used": [...],
       "disclaimer": "..."}
    """
    rows = _load_labeled_trades(mode)
    n = len(rows)
    if n < MIN_TRADES:
        return {
            "available": False, "n": n, "min": MIN_TRADES,
            "reason": (f"Only {n} labeled {mode} trade{'s' if n != 1 else ''} in the journal "
                       f"— need at least {MIN_TRADES} before a historical lean means anything. "
                       f"Keep trading; this fills in on its own."),
        }

    wins = [r for r in rows if r["_win"]]
    losses = [r for r in rows if not r["_win"]]
    counts = _counts(rows)
    current = flatten_signals(current_precheck_result)

    log_odds = 0.0
    used = []
    for name, key in zip(SIGNAL_NAMES, SIGNAL_KEYS):
        direction = current.get(key)
        if not direction or direction == "missing":
            continue
        bucket = counts[key].get(direction)
        if not bucket or bucket["win"] + bucket["loss"] < MIN_PER_DIRECTION:
            continue  # not enough history for THIS specific reading, skip it
        n_categories = max(len(counts[key]), 1)
        contribution = _log_odds_contribution(bucket, len(wins), len(losses), n_categories)
        log_odds += contribution
        used.append({
            "signal": name, "direction": direction,
            "historical_win_rate": round(bucket["win"] / (bucket["win"] + bucket["loss"]) * 100, 1),
            "n": bucket["win"] + bucket["loss"],
        })

    if not used:
        return {
            "available": False, "n": n, "min": MIN_TRADES,
            "reason": ("Enough trades overall, but none of today's specific signal readings "
                       "have enough history yet — different market conditions than what's "
                       "been traded so far."),
        }

    lean = "favorable" if log_odds > 0.4 else "unfavorable" if log_odds < -0.4 else "even"
    return {
        "available": True, "n": n, "wins": len(wins), "losses": len(losses),
        "historical_win_rate": round(len(wins) / n * 100, 1),
        "lean": lean, "log_odds": round(log_odds, 3),
        "signals_used": used,
        "disclaimer": ("Learned from this account's own paper trades only — not a "
                       "calibrated probability, not a backtest, no out-of-sample check. "
                       "A small, biased sample dressed up as a lean. Weight accordingly."),
    }
