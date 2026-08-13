"""
REGIME MODEL — a 4-state Gaussian hidden Markov model over SPY's tick log.

Scope, deliberately: this module infers a probability distribution over a
small set of latent market "states" from recent price/volatility/order-flow
behavior. It does NOT feed position sizing or reweight any existing signal.
That's intentional — the build order is regime probabilities first, then
walk-forward validation (see validate_regime.py... actually see the
validate_* functions below) to check whether the regime read has any
genuine out-of-sample relationship to what happens next, and only after
that would it make sense to wire it into anything that risks money.

No claim is made here that this resembles what any specific fund actually
runs internally — HMMs are a standard, well-documented statistical tool
(originally from speech recognition) for inferring a hidden discrete state
from a continuous observation sequence. Using one is defensible on its own
terms, not because of who else may or may not have used one.

States (assigned post-hoc by ranking fitted means/variances, not fixed
labels baked into training):
  1. trending_up      — highest mean bar return
  2. trending_down     — lowest mean bar return
  3. low_vol_chop      — of the remaining two, lower volatility
  4. high_vol_disorder — of the remaining two, higher volatility

Math, in plain terms:
  Emission:  log N(x | mu_k, var_k) for diagonal covariance, summed over dims
  Forward:   log_alpha[t,k]  = log_B[t,k] + logsumexp_j(log_alpha[t-1,j] + log_A[j,k])
  Backward:  log_beta[t,k]   = logsumexp_j(log_A[k,j] + log_B[t+1,j] + log_beta[t+1,j])
  Posterior: gamma[t,k]      = exp(log_alpha[t,k] + log_beta[t,k] - loglik)   [smoothed, training only]
  Filtered:  P(S_t | X_1:t)  = softmax(log_alpha[t,:])                        [live inference — no future leak]
  M-step:    standard Baum-Welch re-estimation of pi, A, means, vars from gamma/xi
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

TICK_LOG = Path("tick_log.csv")
BAR_SECONDS = 10
MODEL_VERSION = "hmm-v1"

# Tiered bar thresholds. These encode the distinction between three different
# questions that "the model ran" tends to blur together:
#   MIN_FIT_BARS        — the optimizer is allowed to run at all
#   MIN_REPORTABLE_BARS — an estimate may be shown, flagged experimental
#   MIN_VALIDATION_BARS — predictive conclusions may be evaluated
# "Fit succeeded" is deliberately NOT allowed to read as "trustworthy".
MIN_FIT_BARS = 300           # ~50 min at 10s/bar — enough to run, not to trust
MIN_REPORTABLE_BARS = 1800   # ~5 hrs — estimate stable enough to display as experimental
MIN_VALIDATION_BARS = 5000   # ~14 hrs — enough to power out-of-sample predictive claims
MIN_BARS = MIN_FIT_BARS      # back-compat alias

# Classification-quality gates — a 4-state model can technically fit while one
# state swallows everything or another is nearly empty. That's not a 4-regime model.
MIN_STATE_OCCUPANCY = 0.05
MAX_STATE_OCCUPANCY = 0.80

N_STATES = 4
N_RESTARTS = 5
MAX_EM_ITERS = 100
CONVERGE_TOL = 1e-4
VAR_FLOOR = 1e-10
STATE_NAMES = ["trending_up", "trending_down", "low_vol_chop", "high_vol_disorder"]


# ─── features ──────────────────────────────────────────────────────────────

def _load_ticks(symbol):
    if not TICK_LOG.exists():
        return []
    out = []
    with TICK_LOG.open(newline="") as f:
        for r in csv.DictReader(f):
            if r.get("symbol") != symbol.upper():
                continue
            try:
                ts = datetime.fromisoformat(r["utc_time"]).timestamp()
                mid = float(r["mid"])
                bs, aS = float(r.get("bid_size") or 0), float(r.get("ask_size") or 0)
            except (KeyError, ValueError):
                continue
            out.append((ts, mid, bs, aS))
    out.sort(key=lambda x: x[0])
    return out


def _load_events(symbol):
    """Tick rows -> MarketEvents carrying BOTH provider and receipt time. Rows
    predating provider_ts capture fall back to receipt time and are counted as
    unaudited so timing quality is never falsely claimed for them."""
    import bar_builder
    if not TICK_LOG.exists():
        return [], 0
    events, audited = [], 0
    with TICK_LOG.open(newline="") as f:
        for r in csv.DictReader(f):
            if r.get("symbol") != symbol.upper():
                continue
            try:
                recv = datetime.fromisoformat(r["utc_time"]).timestamp()
                bid = float(r.get("bid") or 0)
                ask = float(r.get("ask") or 0)
                bs, asz = float(r.get("bid_size") or 0), float(r.get("ask_size") or 0)
            except (KeyError, ValueError):
                continue
            pts = r.get("provider_ts") or ""
            if pts:
                try:
                    prov = datetime.fromisoformat(pts).timestamp()
                    audited += 1
                except ValueError:
                    prov = recv
            else:
                prov = recv
            events.append(bar_builder.MarketEvent(
                int(prov * 1e9), int(recv * 1e9), "quote",
                bid=bid or None, ask=ask or None, bid_size=bs, ask_size=asz))
    return events, audited


def timing_report(symbol="SPY"):
    """Timing-quality audit of the tick stream feeding the regime features.
    Returns audited=False/status='unaudited' for legacy data (can't verify),
    or the full bar_builder quality report once provider timestamps are flowing."""
    import bar_builder
    events, audited = _load_events(symbol)
    n = len(events)
    if n == 0:
        return {"audited": False, "status": "no_data"}
    frac = audited / n
    if frac < 0.5:
        return {"audited": False, "status": "unaudited", "audited_fraction": round(frac, 3),
                "note": ("Most tick rows predate provider-timestamp capture, so feed timing "
                         "cannot be verified. New rows are audited going forward; this clears "
                         "itself as fresh ticks accumulate.")}
    _, quality = bar_builder.build_bars_audited(events)
    return {"audited": True, "status": "audited", "audited_fraction": round(frac, 3),
            "timing_quality": quality}


def build_bars(symbol="SPY", bar_seconds=BAR_SECONDS):
    """Aggregate raw ticks into fixed-length bars: [return, realized_vol, quote_size_imbalance].
    Returns (bar_times, feature_matrix) — bar_times is the bar's closing timestamp,
    for lining up with genuinely-future data in the walk-forward validator."""
    ticks = _load_ticks(symbol)
    if len(ticks) < 2:
        return [], np.zeros((0, 3))

    t0 = ticks[0][0]
    buckets = {}
    for ts, mid, bs, aS in ticks:
        idx = int((ts - t0) // bar_seconds)
        buckets.setdefault(idx, []).append((ts, mid, bs, aS))

    bar_times, rows = [], []
    idxs = sorted(buckets)
    prev_close = None
    for idx in idxs:
        pts = buckets[idx]
        mids = [p[1] for p in pts]
        open_, close = mids[0], mids[-1]
        ret = (close / prev_close - 1) if prev_close else 0.0
        # intra-bar realized vol from tick-to-tick returns within the bucket
        tick_rets = [mids[i] / mids[i - 1] - 1 for i in range(1, len(mids))] if len(mids) > 1 else [0.0]
        vol = float(np.std(tick_rets)) if len(tick_rets) > 1 else 0.0
        flows = [(bs - aS) / (bs + aS) for _, _, bs, aS in pts if (bs + aS) > 0]
        flow = float(np.mean(flows)) if flows else 0.0
        rows.append([ret, vol, flow])
        bar_times.append(pts[-1][0])
        prev_close = close

    # drop the first bar (return is always 0 — no prior close to compare against)
    return bar_times[1:], np.array(rows[1:], dtype=float)


# ─── Gaussian HMM (diagonal covariance), Baum-Welch in log space ──────────

def _logsumexp(a, axis=None):
    a_max = np.max(a, axis=axis, keepdims=True)
    a_max_safe = np.where(np.isfinite(a_max), a_max, 0)
    out = np.log(np.sum(np.exp(a - a_max_safe), axis=axis, keepdims=True)) + a_max_safe
    return np.squeeze(out, axis=axis) if axis is not None else out.reshape(())


def _log_emissions(X, means, variances):
    T, D = X.shape
    K = means.shape[0]
    log_b = np.zeros((T, K))
    for k in range(K):
        var_k = np.maximum(variances[k], VAR_FLOOR)
        diff2 = (X - means[k]) ** 2
        log_b[:, k] = -0.5 * np.sum(np.log(2 * np.pi * var_k) + diff2 / var_k, axis=1)
    return log_b


def _forward(log_pi, log_A, log_B):
    T, K = log_B.shape
    log_alpha = np.zeros((T, K))
    log_alpha[0] = log_pi + log_B[0]
    for t in range(1, T):
        log_alpha[t] = log_B[t] + _logsumexp(log_alpha[t - 1][:, None] + log_A, axis=0)
    return log_alpha


def _backward(log_A, log_B):
    T, K = log_B.shape
    log_beta = np.zeros((T, K))
    for t in range(T - 2, -1, -1):
        log_beta[t] = _logsumexp(log_A + log_B[t + 1][None, :] + log_beta[t + 1][None, :], axis=1)
    return log_beta


def _fit_once(X, n_states, max_iters, tol, rng):
    T, D = X.shape
    K = n_states
    # init: random data points as means, overall variance, mildly sticky transitions
    idx = rng.choice(T, size=K, replace=False)
    means = X[idx].copy()
    variances = np.tile(np.var(X, axis=0) + 1e-6, (K, 1))
    pi = np.full(K, 1.0 / K)
    A = np.full((K, K), 0.1 / (K - 1) if K > 1 else 1.0)
    np.fill_diagonal(A, 0.9)

    prev_ll = -np.inf
    for iteration in range(max_iters):
        log_pi, log_A = np.log(np.maximum(pi, 1e-300)), np.log(np.maximum(A, 1e-300))
        log_B = _log_emissions(X, means, variances)
        log_alpha = _forward(log_pi, log_A, log_B)
        log_beta = _backward(log_A, log_B)
        ll = float(_logsumexp(log_alpha[-1]))

        log_gamma = log_alpha + log_beta - ll
        gamma = np.exp(log_gamma)

        # xi for transition re-estimation
        xi_sum = np.zeros((K, K))
        for t in range(T - 1):
            log_xi_t = (log_alpha[t][:, None] + log_A + log_B[t + 1][None, :]
                        + log_beta[t + 1][None, :] - ll)
            xi_sum += np.exp(log_xi_t)

        pi = gamma[0] / gamma[0].sum()
        denom = xi_sum.sum(axis=1, keepdims=True)
        A = xi_sum / np.where(denom > 0, denom, 1)
        weight = gamma.sum(axis=0)
        means = (gamma.T @ X) / np.where(weight[:, None] > 0, weight[:, None], 1)
        variances = np.zeros((K, D))
        for k in range(K):
            diff2 = (X - means[k]) ** 2
            w = weight[k] if weight[k] > 0 else 1
            variances[k] = (gamma[:, k][:, None] * diff2).sum(axis=0) / w
        variances = np.maximum(variances, VAR_FLOOR)

        if abs(ll - prev_ll) < tol:
            prev_ll = ll
            break
        prev_ll = ll

    return {"pi": pi, "A": A, "means": means, "variances": variances,
            "loglik": prev_ll, "iterations": iteration + 1}


def _robust_scaler(X_train):
    """Median / (MAD·1.4826) per feature, computed on the TRAINING window only.
    The three features (return, vol, quote-size imbalance) live on very
    different scales; without this, initialization and variance flooring can
    destabilize EM or let one feature dominate."""
    center = np.median(X_train, axis=0)
    mad = np.median(np.abs(X_train - center), axis=0) * 1.4826
    scale = np.maximum(mad, 1e-8)
    return center, scale


def fit_hmm(X, n_states=N_STATES, n_restarts=N_RESTARTS, max_iters=MAX_EM_ITERS,
            tol=CONVERGE_TOL, seed=0):
    """Fit in robustly-scaled space (better-conditioned EM), then un-scale the
    parameters back to DATA units so all downstream code (emissions, filtering,
    labeling, diagnostics, reporting) is unchanged. The scaler is computed only
    from X (the training window) — never from future/validation rows — so this
    introduces no look-ahead leakage in the walk-forward."""
    center, scale = _robust_scaler(X)
    Xs = (X - center) / scale
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(n_restarts):
        try:
            result = _fit_once(Xs, n_states, max_iters, tol, rng)
        except (np.linalg.LinAlgError, FloatingPointError):
            continue
        if best is None or result["loglik"] > best["loglik"]:
            best = result
    if best is None:
        return None
    # un-scale parameters to data units: mu = mu'·s + c ; var = var'·s²
    best["means"] = best["means"] * scale + center
    best["variances"] = np.maximum(best["variances"] * (scale ** 2), VAR_FLOOR)
    # report loglik in data units (Jacobian: -T·Σ log s per the change of variables)
    best["loglik"] = best["loglik"] - len(X) * float(np.sum(np.log(scale)))
    best["scaler"] = {"center": center.tolist(), "scale": scale.tolist()}
    return best


def label_states(means, variances):
    """Post-hoc labeling by ranking fitted parameters — not fixed at training
    time, so the labels describe whatever the data actually produced."""
    order_by_return = np.argsort(-means[:, 0])   # descending
    up, down = order_by_return[0], order_by_return[-1]
    middle = [i for i in order_by_return if i not in (up, down)]
    middle_sorted = sorted(middle, key=lambda i: variances[i, 1])
    labels = {}
    labels[up] = "trending_up"
    labels[down] = "trending_down"
    if len(middle_sorted) >= 1:
        labels[middle_sorted[0]] = "low_vol_chop"
    if len(middle_sorted) >= 2:
        labels[middle_sorted[-1]] = "high_vol_disorder"
    return labels


# ─── fit diagnostics / quality gates ──────────────────────────────────────

def _posteriors(X, model):
    """Smoothed gamma (for describing the fit), filtered posteriors (for live
    inference), and the log-likelihood. Filtered is what live reads use; gamma
    is only ever used to describe the historical fit, never to predict."""
    log_pi = np.log(np.maximum(model["pi"], 1e-300))
    log_A = np.log(np.maximum(model["A"], 1e-300))
    log_B = _log_emissions(X, model["means"], model["variances"])
    log_alpha = _forward(log_pi, log_A, log_B)
    log_beta = _backward(log_A, log_B)
    ll = float(_logsumexp(log_alpha[-1]))
    gamma = np.exp(log_alpha + log_beta - ll)
    filtered = np.exp(log_alpha - _logsumexp(log_alpha, axis=1)[:, None])
    return gamma, filtered, ll


def _separation_matrix(means, variances):
    """Standardized distance between state mean-returns (dim 0):
    D_ij = |mu_i - mu_j| / sqrt(0.5(var_i + var_j)). Small D = the optimizer
    split two states that aren't economically distinguishable."""
    K = means.shape[0]
    D = np.zeros((K, K))
    for i in range(K):
        for j in range(K):
            if i == j:
                continue
            pooled = np.sqrt(0.5 * (variances[i, 0] + variances[j, 0]))
            D[i, j] = abs(means[i, 0] - means[j, 0]) / pooled if pooled > 0 else 0.0
    return D


def fit_diagnostics(X, model):
    """Occupancy, separation, expected durations, label confidence — the
    Stage-1 'is this even a coherent 4-regime classification' checks, kept
    separate from any predictive claim."""
    gamma, filtered, ll = _posteriors(X, model)
    occupancy = gamma.mean(axis=0)
    labels = label_states(model["means"], model["variances"])
    D = _separation_matrix(model["means"], model["variances"])
    # per-state label confidence: how separated this state's return-mean is from
    # its NEAREST neighbour, squashed to (0,1). Low = ambiguous economic label.
    K = model["means"].shape[0]
    nearest_sep = [min((D[k, j] for j in range(K) if j != k), default=0.0) for k in range(K)]
    label_conf = [round(float(d / (1 + d)), 3) for d in nearest_sep]
    durations = [round(float(1.0 / (1.0 - min(model["A"][k, k], 1 - 1e-9))), 1) for k in range(K)]
    # mean of PER-BAR posterior entropies (not entropy of the average, which is
    # uniform whenever occupancy is balanced even if each bar is decisive).
    # High = the model rarely commits to a state bar-to-bar → states poorly
    # separated or data uninformative.
    per_bar_ent = -np.sum(filtered * np.log(np.maximum(filtered, 1e-12)), axis=1) / np.log(K)
    ent = float(per_bar_ent.mean())

    occ_by_label = {labels.get(k, f"state_{k}"): round(float(occupancy[k]), 3) for k in range(K)}
    min_occ, max_occ = float(occupancy.min()), float(occupancy.max())
    collapsed = bool(min_occ < MIN_STATE_OCCUPANCY or max_occ > MAX_STATE_OCCUPANCY)
    min_separation = float(np.min(D[D > 0])) if np.any(D > 0) else 0.0

    return {
        "state_occupancy": occ_by_label,
        "min_occupancy": round(min_occ, 3), "max_occupancy": round(max_occ, 3),
        "collapsed_or_dominated": collapsed,
        "min_pairwise_separation": round(min_separation, 3),
        "expected_durations_bars": {labels.get(k, f"state_{k}"): durations[k] for k in range(K)},
        "label_confidence": {labels.get(k, f"state_{k}"): label_conf[k] for k in range(K)},
        "posterior_entropy_normalized": round(ent, 3),
    }


def _tier_status(n_bars):
    if n_bars < MIN_FIT_BARS:
        return "insufficient"
    if n_bars < MIN_REPORTABLE_BARS:
        return "fit_only"          # ran, but estimate not yet stable — do not interpret
    if n_bars < MIN_VALIDATION_BARS:
        return "experimental"      # estimate displayable, predictive claims not yet powered
    return "validation_ready"      # enough data to actually evaluate predictive value


# ─── live inference (filtered, no future leak) ────────────────────────────

_cache = {}
_cache_ttl = 60  # seconds — refitting on every dashboard poll would be wasteful


def regime_read(symbol="SPY", force=False):
    import time as _time
    hit = _cache.get(symbol)
    if hit and not force and _time.time() - hit["fitted_at"] < _cache_ttl:
        model = hit["model"]
        bar_times, X = build_bars(symbol)
    else:
        bar_times, X = build_bars(symbol)
        if len(X) < MIN_BARS:
            return {"available": False, "n_bars": len(X), "min_bars": MIN_BARS,
                    "reason": (f"Only {len(X)} {BAR_SECONDS}s bars of {symbol.upper()} tick "
                               f"history so far — need at least {MIN_BARS} (~{MIN_BARS*BAR_SECONDS//60} "
                               f"min of continuous logging during market hours) before a regime "
                               f"model is fit on anything but noise.")}
        model = fit_hmm(X)
        if model is None:
            return {"available": False, "n_bars": len(X), "min_bars": MIN_BARS,
                    "reason": "HMM fit failed to converge on any restart — try again shortly."}
        _cache[symbol] = {"model": model, "fitted_at": _time.time()}

    if len(X) < MIN_BARS:
        return {"available": False, "n_bars": len(X), "min_bars": MIN_BARS,
                "reason": f"Only {len(X)} bars so far — need at least {MIN_BARS}."}

    labels = label_states(model["means"], model["variances"])
    log_pi, log_A = np.log(np.maximum(model["pi"], 1e-300)), np.log(np.maximum(model["A"], 1e-300))
    log_B = _log_emissions(X, model["means"], model["variances"])
    log_alpha = _forward(log_pi, log_A, log_B)
    filtered = np.exp(log_alpha[-1] - _logsumexp(log_alpha[-1]))   # P(S_t | X_1:t), last bar only

    probs = {labels.get(k, f"state_{k}"): round(float(filtered[k]), 4) for k in range(len(filtered))}
    best_k = int(np.argmax(filtered))
    diag = fit_diagnostics(X, model)
    status = _tier_status(len(X))
    label_conf_best = diag["label_confidence"].get(labels.get(best_k, f"state_{best_k}"), 0.0)

    interpretation = {
        "fit_only": "Model ran but the estimate is not yet stable — do not interpret the regime.",
        "experimental": "Estimate is displayable but predictive claims are not yet powered.",
        "validation_ready": "Enough history to evaluate predictive value — run /api/regime/research.",
    }.get(status, "")

    return {
        "available": True, "status": status, "interpretation": interpretation,
        "n_bars": len(X), "bar_seconds": BAR_SECONDS,
        "state": labels.get(best_k, f"state_{best_k}"), "state_probs": probs,
        "label_confidence_for_current_state": label_conf_best,
        "state_means": {labels.get(k, f"state_{k}"): {"return": round(float(model["means"][k, 0]), 6),
                         "vol": round(float(model["means"][k, 1]), 6),
                         "quote_size_imbalance": round(float(model["means"][k, 2]), 4)}
                         for k in range(len(model["means"]))},
        "diagnostics": diag,
        "metadata": {
            "model_version": MODEL_VERSION,
            "fit_end_time": datetime.now(timezone.utc).isoformat(),
            "bars_used": len(X), "n_states": N_STATES,
            "converged": model["iterations"] < MAX_EM_ITERS,
            "em_iterations": model["iterations"],
            "final_log_likelihood": round(model["loglik"], 2),
            "features": ["bar_return", "realized_vol", "quote_size_imbalance"],
            "covariance": "diagonal", "converge_tol": CONVERGE_TOL,
            "n_restarts": N_RESTARTS, "seed_base": 0,
        },
        "disclaimer": ("Filtered state probabilities from a 4-state Gaussian HMM fit on this "
                       "account's own recent tick history — a statistical description of the "
                       "current regime, not a validated predictor. 'Fit succeeded' is not "
                       "'regime is trustworthy'. See /api/regime/research before trusting this "
                       "for anything beyond a read, and note the status field above."),
    }


# ─── walk-forward, out-of-sample validation ───────────────────────────────
#
# The point of this section: decide honestly whether the regime read has any
# real relationship to what happens next, BEFORE it's ever wired into signal
# weighting or position sizing. Every checkpoint fits only on bars up to and
# including t, reads the filtered state at t, and checks it against bar t+1's
# return — data the fit never saw. Nothing here is smoothed with hindsight.

DIRECTIONAL_STATES = {"trending_up": 1, "trending_down": -1}
MIN_FOLDS = 50            # walk-forward checkpoints needed before reporting anything
MIN_PER_STATE = 10        # a given state needs this many checkpoints before ITS bucket is trusted
MIN_DIRECTIONAL_N = 30    # a correlation coefficient needs a much bigger floor than a mean —
                          # 10 points is enough to produce a convincing-looking spurious r


def validate_regime(symbol="SPY", train_window=MIN_BARS, stride=10, min_folds=MIN_FOLDS):
    bar_times, X = build_bars(symbol)
    n = len(X)
    last_checkpoint = n - 2  # need bar t and a genuinely-future bar t+1
    possible_folds = max(0, (last_checkpoint - train_window) // stride + 1) if last_checkpoint >= train_window else 0

    if possible_folds < min_folds:
        return {
            "available": False, "possible_folds": possible_folds, "min_folds": min_folds,
            "n_bars": n,
            "reason": (f"Only {possible_folds} walk-forward checkpoints possible with "
                       f"{n} bars so far — need at least {min_folds}. This grows on its own "
                       f"as the tick log accumulates; no shortcut around needing the data."),
        }

    checkpoints = range(train_window, last_checkpoint + 1, stride)
    by_state = {name: [] for name in STATE_NAMES}
    unconditional = []
    folds_run = 0

    for t in checkpoints:
        window = X[max(0, t - train_window + 1): t + 1]
        model = fit_hmm(window, n_restarts=3, seed=t)   # fewer restarts than live — cost-bounded
        if model is None:
            continue
        labels = label_states(model["means"], model["variances"])
        log_pi, log_A = np.log(np.maximum(model["pi"], 1e-300)), np.log(np.maximum(model["A"], 1e-300))
        log_B = _log_emissions(window, model["means"], model["variances"])
        log_alpha = _forward(log_pi, log_A, log_B)
        filtered = np.exp(log_alpha[-1] - _logsumexp(log_alpha[-1]))
        state_label = labels.get(int(np.argmax(filtered)), "unknown")
        next_return = float(X[t + 1, 0])   # genuinely out-of-sample — bar t+1, never seen by the fit above
        by_state.setdefault(state_label, []).append(next_return)
        unconditional.append(next_return)
        folds_run += 1

    if folds_run < min_folds:
        return {
            "available": False, "possible_folds": folds_run, "min_folds": min_folds, "n_bars": n,
            "reason": f"Only {folds_run} checkpoints actually completed a fit — need {min_folds}.",
        }

    overall_mean = float(np.mean(unconditional)) if unconditional else 0.0
    state_stats = {}
    for name, rets in by_state.items():
        if len(rets) < MIN_PER_STATE:
            state_stats[name] = {"n": len(rets), "available": False}
            continue
        state_stats[name] = {
            "n": len(rets), "available": True,
            "mean_next_bar_return": round(float(np.mean(rets)), 6),
            "vs_unconditional": round(float(np.mean(rets) - overall_mean), 6),
        }

    # simple directional effect size: correlate {+1,-1,0}-coded state against actual next return.
    # NOT a significance test — checkpoints overlap and market data is autocorrelated, so a naive
    # p-value here would be false precision. Reported as effect size + sample size only.
    coded, rets = [], []
    for name, ret_list in by_state.items():
        code = DIRECTIONAL_STATES.get(name)
        if code is None:
            continue
        coded.extend([code] * len(ret_list))
        rets.extend(ret_list)
    directional_corr = None
    if len(coded) >= MIN_DIRECTIONAL_N and np.std(coded) > 0 and np.std(rets) > 0:
        directional_corr = round(float(np.corrcoef(coded, rets)[0, 1]), 3)

    return {
        "available": True, "status": _tier_status(n), "folds": folds_run, "n_bars": n,
        "metadata": {"model_version": MODEL_VERSION, "n_states": N_STATES,
                     "features": ["bar_return", "realized_vol", "quote_size_imbalance"],
                     "train_window_bars": train_window, "stride_bars": stride,
                     "computed_at": datetime.now(timezone.utc).isoformat()},
        "unconditional_mean_next_bar_return": round(overall_mean, 6),
        "by_state": state_stats,
        "directional_effect_size": directional_corr,
        "directional_effect_size_note": (None if directional_corr is not None else
            f"Only {len(coded)} trending-state checkpoints so far — need {MIN_DIRECTIONAL_N} "
            f"before a correlation here means anything rather than noise."),
        "directional_n": len(coded),
        "caveat": ("Effect size only, not a significance test — walk-forward checkpoints use "
                   "overlapping windows and market returns are autocorrelated, so a naive p-value "
                   "would overstate confidence. A near-zero effect size here means: don't wire this "
                   "into sizing yet, regardless of how the live regime read looks."),
    }
