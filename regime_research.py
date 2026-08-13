"""
REGIME RESEARCH — does the HMM discover anything a simpler rule doesn't?

This is the gate the reviewer asked for before any signal integration. It
answers three separate questions, in order, and refuses to skip ahead:

  1. Permutation-null test. Run the walk-forward once to collect, per fold,
     the HMM's inferred directional state and the genuinely-future next-h-bar
     return. Then shuffle the returns against the state codes many times to
     build a null distribution of the effect size. The observed effect is
     reported against that null's median / 5th / 95th percentiles, plus an
     empirical permutation p (fraction of |null| >= |observed|). This is a
     LABEL-permutation test: it makes no independence assumption about the
     overlapping walk-forward windows, it only asks whether the state→return
     association is stronger than chance re-pairings of the same data.

  2. Baseline comparison. The real question is not "does the HMM predict?" but
     "does the HMM beat cheap observable rules?" — persistence, rolling-return
     sign, regression-slope sign, and a linear probe. Each is computed from
     data available up to t only, on the same folds, at the same horizon. An
     HMM effect of 0.08 is worthless if last-bar sign gets 0.10 more stably.

  3. Multi-horizon, one pre-designated primary. Effects are measured at
     h ∈ {1,3,6,12,30} bars. PRIMARY_HORIZON is fixed at 6 (≈1 min) BEFORE
     looking at results, to prevent horizon-shopping. The others are secondary
     diagnostics only.

Verdict is gated on MIN_VALIDATION_BARS. Below it, this reports classification
diagnostics and abstains from any predictive claim. Nothing here touches
position sizing or reweights another signal — that stays off the table until
a verdict of "beats_null_and_baselines" holds across multiple market days.
"""

import numpy as np

import regime_model as rm

# v3 = cumulative forward-log-return targets, finite-sample p, session-aware
#      moving-block controlling null, pre-registered single primary baseline.
# v3 targets are NOT comparable to v2; the aggregator refuses to blend versions.
RESEARCH_VERSION = "regime-research-v3"
TARGET_DEFINITION = "forward_cumulative_log_return"

HORIZONS = [1, 3, 6, 12, 30]       # bars (~10s, 30s, 1m, 2m, 5m)
PRIMARY_HORIZON = 6                # pre-designated, before seeing any result
N_PERMUTATIONS = 2000
BASELINE_LOOKBACK = 6              # bars used by the observable-rule baselines
PRIMARY_BASELINE = "rolling_return_sign"   # pre-registered; the gate compares against THIS one
MIN_FOLDS = 50
MIN_DIRECTIONAL_N = 30
PASS_NULL_PCTL = 95               # observed must exceed this pctl of the null
STRIDE = 15
TRAIN_WINDOW = rm.MIN_FIT_BARS

# The metric is signed directional correlation: state code (+1 up / -1 down)
# vs future return. Higher is better; a GOOD model has POSITIVE correlation.
# Two gates that must not be conflated (this was a real bug in v1):
#   beats_baselines  — hmm_score > best signed baseline + MIN_INCREMENTAL_EDGE
#   positive_edge    — hmm_score >= MIN_ABSOLUTE_EDGE (i.e. actually predictive)
# hmm_score = -0.09 beats a baseline of -0.40 NUMERICALLY, but has NO positive
# predictive value. A pass requires both, plus a time-aware-null exceedance.
METRIC = "signed_directional_correlation"
HIGHER_IS_BETTER = True
MIN_ABSOLUTE_EDGE = 0.05          # provisional — retune once real data exists
MIN_INCREMENTAL_EDGE = 0.02       # HMM must beat the best baseline by at least this

# Time-aware null parameters. An IID shuffle destroys autocorrelation and makes
# the null too narrow (over-optimistic significance). Circular-shift and block
# permutation preserve the target's internal time-series dependence and only
# break its contemporaneous alignment with the HMM output.
CIRCULAR_MIN_SHIFT = 30           # excludes tiny shifts that leave series near-aligned
BLOCK_LEN_FOLDS = 5               # contiguous block length (in folds) for block null


# ─── directional baselines (each computed from data up to t only) ─────────

def _baseline_predictions(window_returns):
    """Signed directional call in {-1,0,+1} from the returns available up to t.
    window_returns is the return series for the training window ending at t."""
    r = window_returns
    out = {}
    # persistence: sign of the most recent bar return
    out["persistence"] = float(np.sign(r[-1]))
    # rolling-return sign over the lookback
    look = r[-BASELINE_LOOKBACK:] if len(r) >= BASELINE_LOOKBACK else r
    out["rolling_return_sign"] = float(np.sign(look.sum()))
    # regression-slope sign over the lookback
    if len(look) >= 2:
        t = np.arange(len(look))
        slope = np.polyfit(t, np.cumsum(look), 1)[0]
        out["regression_slope_sign"] = float(np.sign(slope))
    else:
        out["regression_slope_sign"] = 0.0
    return out


def _linear_probe(X_train, next_returns_train, x_now):
    """Least-squares probe: fit next-bar return on features over the train
    window, predict at t, return signed direction. A cheap 'can a linear model
    on the same features do it' baseline."""
    if len(X_train) < 10:
        return 0.0
    A = np.column_stack([X_train, np.ones(len(X_train))])
    try:
        coef, *_ = np.linalg.lstsq(A, next_returns_train, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0
    pred = float(np.append(x_now, 1.0) @ coef)
    return float(np.sign(pred))


# ─── walk-forward record collection ───────────────────────────────────────

def forward_log_return(close, index, horizon):
    """Cumulative forward log return from bar `index` to `index+horizon`:
    log(P[index+horizon] / P[index]). This is the return over the WHOLE horizon,
    not the isolated final bar — the v2 bug this v3 fixes. NaN-guarded."""
    target_index = index + horizon
    if index < 0 or target_index >= len(close):
        return float("nan")
    start_price = float(close[index])
    end_price = float(close[target_index])
    if start_price <= 0 or end_price <= 0:
        return float("nan")
    return float(np.log(end_price / start_price))


def _reconstruct_close(returns):
    """Rebuild a positive price path from the bar simple-return column so
    forward_log_return can take true price ratios. close[0]=1; a gap-return of
    0 just carries price forward, matching build_bars' gap handling."""
    close = np.empty(len(returns) + 1)
    close[0] = 1.0
    for i, r in enumerate(returns):
        close[i + 1] = close[i] * (1.0 + float(r))
    return close[1:]   # align index with bar index


def _session_date_of(ts):
    """ET calendar date for a bar timestamp (unix seconds), for session-aware
    nulls that must never join blocks across market dates."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc
    try:
        return datetime.fromtimestamp(float(ts), tz=et).date().isoformat()
    except Exception:
        return "unknown"


def _collect_records(symbol, train_window, stride):
    bar_times, X = rm.build_bars(symbol)
    n = len(X)
    max_h = max(HORIZONS)
    last_checkpoint = n - max_h - 1
    if last_checkpoint < train_window:
        return n, []

    close = _reconstruct_close(X[:, 0])
    records = []
    for t in range(train_window, last_checkpoint + 1, stride):
        window = X[max(0, t - train_window + 1): t + 1]
        model = rm.fit_hmm(window, n_restarts=3, seed=t)
        if model is None:
            continue
        labels = rm.label_states(model["means"], model["variances"])
        log_pi = np.log(np.maximum(model["pi"], 1e-300))
        log_A = np.log(np.maximum(model["A"], 1e-300))
        log_B = rm._log_emissions(window, model["means"], model["variances"])
        log_alpha = rm._forward(log_pi, log_A, log_B)
        filtered = np.exp(log_alpha[-1] - rm._logsumexp(log_alpha[-1]))
        state_label = labels.get(int(np.argmax(filtered)), "unknown")
        hmm_code = {"trending_up": 1, "trending_down": -1}.get(state_label, 0)

        # baselines from the same window (returns are feature column 0)
        win_returns = window[:, 0]
        base = _baseline_predictions(win_returns)
        # linear probe: train on window pairs (X[i] -> return[i+1])
        if len(window) >= 12:
            probe = _linear_probe(window[:-1], window[1:, 0], window[-1])
        else:
            probe = 0.0
        base["linear_probe"] = probe

        # cumulative forward log return over each horizon (v3), genuinely future
        future = {h: forward_log_return(close, t, h) for h in HORIZONS}
        session = _session_date_of(bar_times[t]) if t < len(bar_times) else "unknown"
        records.append({"t": t, "hmm_code": hmm_code, "baselines": base,
                        "future": future, "session": session})
    return n, records


# ─── effect size + null distributions (iid + time-aware) ──────────────────

def _score(codes, rets):
    """Signed directional correlation. Higher = better; positive = predictive."""
    codes, rets = np.asarray(codes, float), np.asarray(rets, float)
    if len(codes) < MIN_DIRECTIONAL_N or np.std(codes) == 0 or np.std(rets) == 0:
        return None
    return float(np.corrcoef(codes, rets)[0, 1])


def _corr(codes, rets):
    return float(np.corrcoef(codes, rets)[0, 1])


def _finite_p(null, observed):
    """+1 finite-sample correction: p = (1 + #{null >= obs}) / (n_perm + 1).
    A finite permutation sample can never literally establish p = 0.0."""
    return (1 + int(np.sum(np.asarray(null) >= observed))) / (len(null) + 1)


def _estimate_block_len(rets, default=BLOCK_LEN_FOLDS):
    """Block length from the target's own autocorrelation: first lag where the
    ACF falls below 1/e, capped. Longer than the dependence horizon is the goal."""
    rets = np.asarray(rets, float)
    n = len(rets)
    if n < 8:
        return max(1, min(default, n // 2))
    r = rets - rets.mean()
    denom = float(np.sum(r * r))
    if denom == 0:
        return default
    bl = default
    for lag in range(1, min(n // 2, 50)):
        ac = float(np.sum(r[:-lag] * r[lag:]) / denom)
        if ac < 0.3679:
            bl = max(2, lag)
            break
    return int(min(bl, max(2, n // 5)))


def _session_groups(sessions, n):
    """Contiguous index groups sharing a session (market-date) label."""
    if not sessions or len(sessions) != n:
        return [list(range(n))]
    groups, start = [], 0
    for i in range(1, n):
        if sessions[i] != sessions[i - 1]:
            groups.append(list(range(start, i)))
            start = i
    groups.append(list(range(start, n)))
    return groups


def _iid_shuffle(rets, rng, sessions=None, block_len=None):
    """Destroys ALL time-series structure — over-narrow null, DIAGNOSTIC only."""
    return rng.permutation(rets)


def _circular_shift(rets, rng, sessions=None, block_len=None):
    """Whole-series circular shift. Preserves autocorrelation but can create an
    artificial join across the series ends (and across sessions) — kept as a
    DIAGNOSTIC, not the controlling null."""
    n = len(rets)
    min_shift = min(CIRCULAR_MIN_SHIFT, max(3, n // 10))
    if n <= 2 * min_shift:
        return None
    shift = int(rng.integers(min_shift, n - min_shift))
    return np.roll(rets, shift)


def _session_block_shuffle(rets, rng, sessions=None, block_len=None):
    """Session-aware moving-block bootstrap — the CONTROLLING null. Resamples
    contiguous blocks WITHIN each market date (never joining blocks across
    sessions), preserving intraday dependence while breaking alignment with the
    state codes. This avoids the cross-session artificial joins the plain
    circular shift can create over overnight gaps / regime boundaries."""
    n = len(rets)
    block_len = block_len or BLOCK_LEN_FOLDS
    out = np.empty(n)
    for g in _session_groups(sessions, n):
        lo, hi = g[0], g[-1] + 1
        seg = rets[lo:hi]
        gl = len(seg)
        L = max(1, min(block_len, gl))
        filled = []
        while len(filled) < gl:
            start = int(rng.integers(0, gl))
            filled.extend(seg[(start + k) % gl] for k in range(L))
        out[lo:hi] = np.asarray(filled[:gl])
    return out


def _null_summary(codes, rets, shuffle_fn, n_perm, rng, sessions=None, block_len=None):
    """One-sided (higher-is-better) null test with a given shuffle strategy and
    the +1 finite-sample p-value."""
    codes, rets = np.asarray(codes, float), np.asarray(rets, float)
    observed = _score(codes, rets)
    if observed is None:
        return None
    null = []
    for _ in range(n_perm):
        shuffled = shuffle_fn(rets, rng, sessions, block_len)
        if shuffled is None:
            return None
        null.append(_corr(codes, shuffled))
    null = np.asarray(null)
    p95 = float(np.percentile(null, PASS_NULL_PCTL))
    return {
        "observed_effect": round(observed, 4),
        "null_median": round(float(np.median(null)), 4),
        "null_p05": round(float(np.percentile(null, 5)), 4),
        "null_p95": round(p95, 4),
        "one_sided_p": round(_finite_p(null, observed), 4),   # +1 corrected, never 0.0
        "exceeds_null_p95": bool(observed > p95),
        "n": len(codes),
    }


# ─── top-level report ─────────────────────────────────────────────────────

def research_report(symbol="SPY", train_window=TRAIN_WINDOW, stride=STRIDE,
                    n_perm=N_PERMUTATIONS, seed=0):
    n_bars, records = _collect_records(symbol, train_window, stride)
    status = rm._tier_status(n_bars)

    if len(records) < MIN_FOLDS:
        return {
            "available": False, "status": status, "n_bars": n_bars,
            "folds": len(records), "min_folds": MIN_FOLDS,
            "reason": (f"Only {len(records)} walk-forward folds from {n_bars} bars — need "
                       f"{MIN_FOLDS}. Grows on its own as the tick log fills; no shortcut."),
        }

    hmm_codes_all = np.asarray([r["hmm_code"] for r in records], float)
    baseline_names = list(records[0]["baselines"].keys())
    sessions_all = [r["session"] for r in records]

    per_horizon = {}
    for h in HORIZONS:
        rets_raw = np.asarray([r["future"][h] for r in records], float)
        mask = np.isfinite(rets_raw)                 # drop any NaN target (edge/gap)
        codes_h = hmm_codes_all[mask]
        rets_h = rets_raw[mask]
        sessions_h = [sessions_all[i] for i in range(len(records)) if mask[i]]
        block_len = _estimate_block_len(rets_h)
        hmm_score = _score(codes_h, rets_h)

        # nulls (independent RNG streams). Session-aware BLOCK is controlling;
        # circular + iid are diagnostics only.
        block = _null_summary(codes_h, rets_h, _session_block_shuffle, n_perm,
                              np.random.default_rng(seed), sessions_h, block_len)
        circ = _null_summary(codes_h, rets_h, _circular_shift, n_perm,
                             np.random.default_rng(seed + 1), sessions_h, block_len)
        iid = _null_summary(codes_h, rets_h, _iid_shuffle, n_perm,
                            np.random.default_rng(seed + 2), sessions_h, block_len)

        baselines = {}
        for name in baseline_names:
            codes_b = np.asarray([r["baselines"][name] for r in records], float)[mask]
            eff = _score(codes_b, rets_h)
            baselines[name] = None if eff is None else round(eff, 4)
        primary_baseline_score = baselines.get(PRIMARY_BASELINE)
        others = [v for v in baselines.values() if v is not None]
        best_baseline_score = max(others) if others else None   # secondary diagnostic only

        # gate against the PRE-REGISTERED primary baseline (not best-of-all)
        beats_primary_numerically = (hmm_score is not None and primary_baseline_score is not None
                                     and hmm_score > primary_baseline_score)
        beats_primary = (hmm_score is not None and primary_baseline_score is not None
                         and hmm_score > primary_baseline_score + MIN_INCREMENTAL_EDGE)
        positive_edge = (hmm_score is not None and hmm_score >= MIN_ABSOLUTE_EDGE)
        time_aware_significant = bool(block and block["exceeds_null_p95"])

        per_horizon[h] = {
            "metric": METRIC, "higher_is_better": HIGHER_IS_BETTER,
            "target_definition": TARGET_DEFINITION,
            "n_used": int(mask.sum()), "block_len_folds": block_len,
            "hmm_score": None if hmm_score is None else round(hmm_score, 4),
            "primary_baseline": PRIMARY_BASELINE,
            "primary_baseline_score": primary_baseline_score,
            "best_of_all_baselines_score": best_baseline_score,   # diagnostic, not the gate
            "baseline_scores": baselines,
            "hmm_beats_primary_baseline_numerically": bool(beats_primary_numerically),
            "hmm_beats_primary_baseline_with_margin": bool(beats_primary),
            "hmm_has_positive_predictive_value": bool(positive_edge),
            "time_aware_significant": time_aware_significant,
            "block_permutation_p": None if block is None else block["one_sided_p"],
            "circular_shift_p": None if circ is None else circ["one_sided_p"],
            "iid_permutation_p": None if iid is None else iid["one_sided_p"],
            "null_session_block": block, "null_circular_shift": circ, "null_iid": iid,
        }

    # verdict on the PRE-DESIGNATED primary horizon only
    prim = per_horizon[PRIMARY_HORIZON]
    gates = {
        "time_aware_significant": prim["time_aware_significant"],
        "beats_primary_baseline_with_margin": prim["hmm_beats_primary_baseline_with_margin"],
        "has_positive_predictive_value": prim["hmm_has_positive_predictive_value"],
    }
    if status != "validation_ready":
        verdict = "insufficient_data_for_verdict"
    elif prim["hmm_score"] is None or prim["null_session_block"] is None:
        verdict = "insufficient_directional_samples"
    elif all(gates.values()):
        verdict = "beats_null_and_baselines"
    elif gates["time_aware_significant"] and gates["has_positive_predictive_value"]:
        verdict = "predictive_but_not_beyond_baseline"
    elif gates["time_aware_significant"]:
        verdict = "significant_but_no_positive_edge"
    else:
        verdict = "no_edge_beyond_chance"

    return {
        "available": True, "status": status, "n_bars": n_bars, "folds": len(records),
        "research_version": RESEARCH_VERSION,
        "metric": METRIC, "higher_is_better": HIGHER_IS_BETTER,
        "target_definition": TARGET_DEFINITION,
        "primary_baseline": PRIMARY_BASELINE,
        "edge_thresholds": {"min_absolute_edge": MIN_ABSOLUTE_EDGE,
                            "min_incremental_edge": MIN_INCREMENTAL_EDGE},
        "null_controlling_verdict": "session_aware_block (moving-block bootstrap within market "
                                    "dates); circular_shift and iid are diagnostics only",
        "primary_horizon_bars": PRIMARY_HORIZON,
        "primary_horizon_seconds": PRIMARY_HORIZON * rm.BAR_SECONDS,
        "verdict": verdict, "gates": gates,
        "primary_horizon_result": prim,
        "secondary_horizons": {h: per_horizon[h] for h in HORIZONS if h != PRIMARY_HORIZON},
        "notes": (
            "v3: targets are cumulative forward log returns over each horizon (not the isolated "
            "final bar); the controlling null is a session-aware moving-block bootstrap; the gate "
            "compares against the pre-registered primary baseline (rolling_return_sign), not the "
            "best of all. Verdict is on the primary horizon only; secondary horizons are "
            "diagnostics. 'beats_null_and_baselines' at one point in time is necessary but NOT "
            "sufficient to wire this into sizing."),
        "acceptance_gate_remaining": [
            "holds across multiple market days",
            "survives realistic spread/slippage/impact",
            "states non-collapsed and economically persistent",
            "timestamp/timing-quality audit passes",
            "only then: signal weighting / sizing",
        ],
    }
