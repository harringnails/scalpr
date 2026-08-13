"""
SCALPR INTELLIGENCE — FEATURE ENGINE (`scalpr-intel-v0`).

Phase 1 of the Scalpr Intelligence layer: *instrument and label*. This module
turns the existing human-readable evidence (premarket scorecard, HMM regime,
entry-policy intraday context, workup options chain) into ONE machine-readable,
decision-time-frozen feature record per (ticker, timestamp), plus per-contract
target-before-stop labels, plus a TRANSPARENT rules-based score. It trains no
model and places no order. It is the honest foundation the review asked for:
label first, prove the labels, only then consider ML.

────────────────────────────────────────────────────────────────────────────
DISCIPLINE (identical spirit to entry-policy-exploratory-v0 and the snapshot
work):
  * `formal_cohort_eligible` is permanently False in everything this module
    emits. No feature record or label here counts toward any qualified cohort.
  * No ML. The score is a deterministic, auditable RULE score with three
    SEPARATE outputs (direction / trade-quality / executability). There is no
    single blended number and no probability — a rule firing is evidence, not a
    forecast (§5, §12 of the design).
  * NOTHING is fabricated. Any input not yet wired to real data (term-structure
    / skew, VIX & breadth regime, side-classified flow) is emitted as an
    explicit `null` with a per-group `data_status` of "unavailable". A consumer
    can always tell a real value from a missing one.
  * NO FUTURE-DATA LEAK. Labels use only bars strictly after the decision
    minute; the decision is fixed at its decision-time quote.

────────────────────────────────────────────────────────────────────────────
SCHEMA VERSIONING POLICY (per the operator's clarification):
  "Frozen" means SEMANTICALLY STABLE + VERSIONED, not sealed against growth.
    * Within a SCHEMA_VERSION the MEANING of every existing field is immutable.
    * New fields (e.g. once skew / breadth / side-classified flow are wired) are
      ADDED under a bumped SCHEMA_VERSION. Existing fields keep their meaning, so
      old records stay readable and comparable. Additions never rewrite history.
  This is what makes the stub-now / fill-later path additive.

LABEL BASIS (honest limitation, upgradeable under a version bump):
  There is no realized forward option-price feed yet, so per-contract
  target-before-stop is computed from the realized *underlying* bar path
  translated to option terms by a first-order delta+gamma proxy. Every label
  carries `label_basis: "delta_gamma_proxy"` and `realized_option_path: false`
  and lists what the proxy ignores (IV change, theta, real bid/ask at exit).
  When a contract-price logger exists, realized labels supersede these under a
  new label version — proxy labels are never silently treated as realized.
"""

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "scalpr-intel-v0"
# The contract universe frozen inside a snapshot is scoped to this version too;
# tracked explicitly so a future change to the universe rule (e.g. delta band)
# forces a new snapshot identity rather than colliding with an old one.
CONTRACT_UNIVERSE_VERSION = "scalpr-intel-universe-v0"
RULES_SCORE_VERSION = "scalpr-intel-v0"
FORMAL_COHORT_ELIGIBLE = False               # permanent for this exploratory layer

FEATURES_LOG = "scalpr_intel_features_v0.jsonl"
CONTRACT_LABELS_LOG = "scalpr_intel_contract_labels_v0.jsonl"
# Immutable decision-time snapshots (feature record + frozen contract universe).
# The close-time labeler READS these and never regenerates them.
FEATURE_SNAPSHOTS_LOG = "scalpr_intel_feature_snapshots_v0.jsonl"

# Fields frozen for each candidate contract at decision time (the label universe).
FROZEN_QUOTE_FIELDS = ("symbol", "expiry", "strike", "type", "dte", "bid", "ask",
                       "mid", "spread_pct", "iv", "delta", "gamma", "theta", "oi",
                       "volume", "oi_since_prev")

# Quote-quality thresholds (decision-time). Two-tier staleness:
#   * age > STALE_QUOTE_WARN_SEC (15 min)  → minor-stale WARNING, still labelable
#     (quality stays "ok", warning="quote_stale").
#   * age > STALE_QUOTE_UNLABELABLE_SEC (60 min) → MATERIALLY stale: the decision
#     quote is too old to support a credible proxy label, so the contract's label
#     becomes UNLABELABLE_STALE (quality="stale"). The contract is STILL frozen in
#     the research universe — only its label is withheld.
# Structural problems (crossed/missing/unusable) make the contract UNLABELABLE
# regardless of age. Upstream build_contracts already drops crossed/missing
# quotes; this is defense-in-depth and the place stale detection lives.
STALE_QUOTE_WARN_SEC = 900                    # 15 min → warning
STALE_QUOTE_UNLABELABLE_SEC = 3600            # 60 min → materially stale (UNLABELABLE_STALE)
STALE_QUOTE_SEC = STALE_QUOTE_WARN_SEC        # backward-compatible alias
MAX_USABLE_SPREAD_PCT = 100.0                 # spread wider than premium ⇒ unusable


def canonical_hash(obj):
    """Stable SHA-256 over a JSON-canonicalized object (sorted keys). Used for
    feature/contract/label snapshot hashing and correction bookkeeping."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _atomic_append(path, obj):
    """Crash-safe append of one JSON record to a JSONL log.

    Uses a single O_APPEND write syscall (atomic at the kernel level for a
    regular file on a local FS) followed by fsync, so a process death either
    writes the whole line or nothing — a reader never sees a half-record as
    valid. Readers additionally skip any malformed trailing line (see
    `_iter_jsonl`). Whole-file artifacts (session snapshots) use temp+fsync+
    rename in session_snapshot.py; append logs use this. Returns True on success.
    """
    body = json.dumps(obj, separators=(",", ":"))
    # If a previous write was crash-truncated (no trailing newline), prepend one
    # so this record starts on a fresh line — the corrupt partial stays isolated
    # to its own (skipped) line and never swallows the next valid record.
    prefix = ""
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as rf:
                rf.seek(-1, os.SEEK_END)
                if rf.read(1) != b"\n":
                    prefix = "\n"
    except OSError:
        pass
    line = (prefix + body + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _iter_jsonl(path):
    """Yield parsed JSON objects from a JSONL file, skipping any malformed line
    (e.g. a crash-truncated trailing record or a corrupt line). Never raises on
    a bad line — corruption is isolated, valid records still read."""
    p = Path(path)
    if not p.exists():
        return
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def assess_quote_quality(contract, decision_ts=None, quote_as_of=None):
    """Classify the decision-time quote. Returns (quality, warning, age_sec).

    quality ∈ {ok, crossed, missing, locked, unusable, stale}.
      * crossed / missing / unusable → UNLABELABLE (structural).
      * stale (age > STALE_QUOTE_UNLABELABLE_SEC) → UNLABELABLE_STALE (materially
        too old for a credible proxy).
      * locked → labelable, warning "quote_locked".
      * ok + warning "quote_stale" (age > STALE_QUOTE_WARN_SEC) → minor stale,
        still labelable.
    `quote_as_of` is the workup pull time; age is measured against the decision
    timestamp (the workup pull is the freshest quote source we have — a stale
    pull means stale quotes). Material staleness outranks locked."""
    bid, ask, mid = contract.get("bid"), contract.get("ask"), contract.get("mid")
    spread_pct = contract.get("spread_pct")
    age_sec = None
    if decision_ts and quote_as_of:
        try:
            d = datetime.fromisoformat(str(decision_ts).replace("Z", "+00:00"))
            q = datetime.fromisoformat(str(quote_as_of).replace("Z", "+00:00"))
            age_sec = (d - q).total_seconds()
        except Exception:
            age_sec = None
    if bid is None or ask is None or mid is None or mid <= 0:
        return "missing", "quote_missing", age_sec
    if ask < bid:
        return "crossed", "quote_crossed", age_sec
    if spread_pct is not None and spread_pct > MAX_USABLE_SPREAD_PCT:
        return "unusable", "spread_exceeds_premium", age_sec
    if age_sec is not None and age_sec > STALE_QUOTE_UNLABELABLE_SEC:
        return "stale", "quote_materially_stale", age_sec      # → UNLABELABLE_STALE
    if bid == ask:
        return "locked", "quote_locked", age_sec
    if age_sec is not None and age_sec > STALE_QUOTE_WARN_SEC:
        return "ok", "quote_stale", age_sec                    # minor stale warning
    return "ok", None, age_sec


def quote_bucket(quality, warning):
    """Map a quote-quality assessment to a reporting bucket, so label outcomes
    can be tallied separately by quote quality."""
    if quality in ("crossed", "missing", "unusable"):
        return "unusable_unlabelable"
    if quality == "stale":
        return "stale_unlabelable"
    if quality == "locked":
        return "locked"
    if warning:                       # ok-but-warned (minor stale)
        return "warning_quality"
    return "high_quality"

# In-band definition — matches workup / entry-policy (delta magnitude 0.15–0.70).
BAND_DELTA_LO = 0.15
BAND_DELTA_HI = 0.70

# Placeholder label thresholds (option-premium terms). NOT tuned from any result;
# they exist so the labeler produces a target-before-stop label at all. Re-set
# when a realized-fill label version is frozen.
LABEL_TARGET_PCT = 0.25                       # +25% of entry mid
LABEL_STOP_PCT = 0.20                         # -20% of entry mid
LABEL_HORIZON_MIN = 60                        # look-forward window for the label
LABEL_COST_BPS = 1.0                          # placeholder round-trip, labeled


# ── data-status helper ──────────────────────────────────────────────────────

def _status(available, degraded=False):
    if available:
        return "degraded" if degraded else "available"
    return "unavailable"


def _g(d, *path, default=None):
    """Safe nested get."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


# ── the feature record (§1) ─────────────────────────────────────────────────

def build_feature_record(data_client, symbol="SPY", workup_payload=None):
    """One decision-time-frozen, machine-readable feature vector for `symbol`.

    Composes what already exists and marks everything else `unavailable`. Never
    raises on a missing source — a dead source degrades that GROUP's data_status,
    it does not fake a number. `workup_payload` is an optional runs/{T}_{d}.json
    dict (the options chain); if absent, the options/volatility/liquidity groups
    report unavailable rather than inventing values.
    """
    key = symbol.upper().strip()
    as_of = datetime.now(timezone.utc).isoformat()

    # --- premarket scorecard: regime-ish background, confidence, event, vol ---
    sc = None
    try:
        import premarket as pm
        sc = pm.run_premarket(data_client, key)
    except Exception:
        sc = None
    data_conf = _g(sc or {}, "data_confidence", "score", default=None)
    background = _g(sc or {}, "background", default=None)

    # --- HMM regime state ---
    reg = {"available": False}
    try:
        import regime_model as rm
        reg = rm.regime_read(key)
    except Exception:
        reg = {"available": False}

    # --- entry-policy intraday context (VWAP, opening range, session bars) ---
    ic = None
    try:
        import entry_policy as ep
        ic = ep._intraday_context(data_client, key)
    except Exception:
        ic = None

    spot = _g(workup_payload or {}, "spot", default=(ic or {}).get("last"))

    # ---- GROUP: market_regime (partial: HMM available; VIX/breadth NOT wired) --
    market_regime = {
        "data_status": _status(bool(reg.get("available")) or background is not None,
                               degraded=True),
        "hmm_state": reg.get("state") if reg.get("available") else None,
        "hmm_available": bool(reg.get("available")),
        "premarket_background": background,
        # NOT WIRED — explicit stubs (design §1/§4). Never fabricated.
        "spy_trend": None,
        "qqq_trend": None,
        "sector_relative_strength": None,
        "vix_percentile": None,
        "breadth_score": None,
        "_unwired": ["spy_trend", "qqq_trend", "sector_relative_strength",
                     "vix_percentile", "breadth_score"],
    }

    # ---- GROUP: underlying (available from session bars, some IEX-degraded) ----
    underlying = {
        "data_status": _status(ic is not None and ic.get("last") is not None,
                               degraded=True),  # IEX feed → degraded
        "last": (ic or {}).get("last"),
        "vwap": (ic or {}).get("vwap"),
        "above_vwap": (ic or {}).get("above_vwap"),
        "vwap_slope_pos": (ic or {}).get("vwap_slope_pos"),
        "opening_range_complete": (ic or {}).get("or_complete"),
        "or_high": (ic or {}).get("or_high"),
        "or_low": (ic or {}).get("or_low"),
        "or_broken_up": (ic or {}).get("or_broken_up"),
        "or_broken_dn": (ic or {}).get("or_broken_dn"),
        "held_above": (ic or {}).get("held_above"),
        "held_below": (ic or {}).get("held_below"),
        "session_minute": (ic or {}).get("minutes_elapsed"),
        # NOT reliably available on IEX — explicit stubs, not zeros.
        "rsi_5m": None,
        "relative_volume": None,
        "distance_from_vwap_atr": None,
        "_unwired": ["rsi_5m", "relative_volume", "distance_from_vwap_atr"],
    }

    # ---- options chain groups from the workup payload -------------------------
    contracts = (workup_payload or {}).get("contracts") or []
    band = in_band_contracts(contracts)
    has_chain = bool(contracts)

    # GROUP: options_flow (aggregates available; side-classified flow NOT wired)
    tot_oi = sum(int(c.get("oi") or 0) for c in contracts)
    tot_vol = sum(int(c.get("volume") or 0) for c in contracts)
    call_oi = sum(int(c.get("oi") or 0) for c in contracts if c.get("type") == "C")
    put_oi = sum(int(c.get("oi") or 0) for c in contracts if c.get("type") == "P")
    oi_moves = [c.get("oi_since_prev") for c in contracts
                if c.get("oi_since_prev") is not None]
    options_flow = {
        "data_status": _status(has_chain, degraded=True),
        "total_oi": tot_oi if has_chain else None,
        "total_volume": tot_vol if has_chain else None,
        "put_call_oi_ratio": round(put_oi / call_oi, 4) if call_oi else None,
        "contracts_with_oi_since_prev": len(oi_moves) if has_chain else None,
        "net_oi_since_prev": sum(oi_moves) if oi_moves else None,
        # side-classified / sweep flow requires trade-level data — NOT wired.
        "call_premium_bought": None,
        "put_premium_bought": None,
        "net_directional_delta": None,
        "sweep_persistence": None,
        "opening_call_buy_volume": None,
        "opening_put_buy_volume": None,
        "multi_expiry_confirmation": None,
        "_unwired": ["call_premium_bought", "put_premium_bought",
                     "net_directional_delta", "sweep_persistence",
                     "opening_call_buy_volume", "opening_put_buy_volume",
                     "multi_expiry_confirmation"],
    }

    # GROUP: volatility (iv_rank available; term-structure/skew fetched-then-
    # discarded upstream → still-blocked payload check → explicit stubs)
    # The UW iv-rank payload is a nested dict {iv_rank_1y, volatility}; normalize
    # to numbers. Accept a bare scalar too (forward-compatible).
    ivr_raw = _g(workup_payload or {}, "iv_rank")
    iv_rank_val = realized_vol = None
    if isinstance(ivr_raw, dict):
        try:
            iv_rank_val = float(ivr_raw.get("iv_rank_1y"))
        except (TypeError, ValueError):
            iv_rank_val = None
        try:
            realized_vol = float(ivr_raw.get("volatility"))
        except (TypeError, ValueError):
            realized_vol = None
    elif isinstance(ivr_raw, (int, float)):
        iv_rank_val = float(ivr_raw)
    volatility = {
        "data_status": _status(iv_rank_val is not None, degraded=True),
        "iv_rank": iv_rank_val,
        "realized_vol": realized_vol,
        "atm_iv": _g(workup_payload or {}, "atm", "iv") if workup_payload else None,
        # discarded upstream today — unavailable until the payload check settles.
        "iv_percentile": None,
        "term_structure_slope": None,
        "put_call_skew": None,
        "realized_implied_spread": None,
        "expected_move": None,
        "_unwired": ["iv_percentile", "term_structure_slope", "put_call_skew",
                     "realized_implied_spread", "expected_move"],
    }

    # GROUP: events (earnings if the chain carried it)
    earnings = _g(workup_payload or {}, "atm", "earnings") if workup_payload else None
    events = {
        "data_status": _status(earnings is not None),
        "earnings_date": earnings,
        # NOT wired — macro calendar, analyst revisions, filings.
        "earnings_days": None,
        "macro_event_hours": None,
        "_unwired": ["earnings_days", "macro_event_hours"],
    }

    # GROUP: liquidity (available directly from the chain)
    spreads = sorted(c.get("spread_pct") for c in band
                     if c.get("spread_pct") is not None)
    ois = sorted(int(c.get("oi") or 0) for c in band)
    liquidity = {
        "data_status": _status(bool(band)),
        "qualified_contracts": len(band) if has_chain else None,
        "median_spread_pct": (spreads[len(spreads) // 2] if spreads else None),
        "median_open_interest": (ois[len(ois) // 2] if ois else None),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,   # permanently False
        "ticker": key,
        "timestamp": as_of,
        "spot_price": spot,
        "feed_status": "degraded_iex",
        "market_regime": market_regime,
        "underlying": underlying,
        "options_flow": options_flow,
        "volatility": volatility,
        "events": events,
        "liquidity": liquidity,
        "schema_policy": ("Field meanings are immutable within this schema_version; "
                          "new fields arrive under a bumped version. 'unavailable' "
                          "means not-yet-wired, never zero."),
        "disclaimer": ("scalpr-intel-v0 feature record. Non-qualifying research; no "
                       "ML; no orders. Unwired inputs are explicit nulls, not values."),
    }


# ── in-band contract selection (design answer: ALL in-band, delta 0.15–0.70) ─

def in_band_contracts(contracts):
    out = []
    for c in contracts or []:
        d = c.get("delta")
        if d is None:
            continue
        if BAND_DELTA_LO <= abs(d) <= BAND_DELTA_HI:
            out.append(c)
    return out


# ── separated rules score (§5 / §12): direction | quality | executability ───

def score_record(fr):
    """Deterministic, auditable RULE score. Emits THREE SEPARATE scores that are
    never blended: a correct direction can still be a poor trade and a poor
    execution. No probability is produced. Each contributor is listed as
    evidence with its sign, and blocking conditions are surfaced as vetoes.
    """
    ur = fr.get("underlying", {})
    mr = fr.get("market_regime", {})
    liq = fr.get("liquidity", {})
    of = fr.get("options_flow", {})

    evidence, vetoes = [], []

    # ---- DIRECTION score (structure + regime background) --------------------
    dir_pts = 0.0
    bg = mr.get("premarket_background")
    if bg in ("favorable", "leaning_bullish"):
        dir_pts += 0.4; evidence.append(("direction", "premarket_background_bullish", +0.4))
    elif bg in ("unfavorable", "leaning_bearish"):
        dir_pts -= 0.4; evidence.append(("direction", "premarket_background_bearish", -0.4))
    if ur.get("above_vwap") is True:
        dir_pts += 0.2; evidence.append(("direction", "above_vwap", +0.2))
    elif ur.get("above_vwap") is False:
        dir_pts -= 0.2; evidence.append(("direction", "below_vwap", -0.2))
    if ur.get("vwap_slope_pos") is True:
        dir_pts += 0.15; evidence.append(("direction", "vwap_slope_up", +0.15))
    elif ur.get("vwap_slope_pos") is False:
        dir_pts -= 0.15; evidence.append(("direction", "vwap_slope_down", -0.15))
    if ur.get("or_broken_up") is True:
        dir_pts += 0.15; evidence.append(("direction", "opening_range_break_up", +0.15))
    elif ur.get("or_broken_dn") is True:
        dir_pts -= 0.15; evidence.append(("direction", "opening_range_break_dn", -0.15))
    # HMM disorder is a hard directional veto (mirrors entry_policy)
    if mr.get("hmm_state") == "high_vol_disorder":
        vetoes.append("unstable_regime")

    direction = ("LONG" if dir_pts >= 0.4 else "SHORT" if dir_pts <= -0.4
                 else "NEUTRAL")

    # ---- TRADE-QUALITY score (independent of direction) ---------------------
    # Whether the *setup* is clean: regime clarity, flow agreement if available,
    # data completeness. Deliberately does NOT include execution cost.
    quality_pts = 0.0
    if mr.get("hmm_available"):
        quality_pts += 0.2; evidence.append(("quality", "regime_state_available", +0.2))
    else:
        evidence.append(("quality", "regime_unavailable", 0.0))
    if ur.get("opening_range_complete") is True:
        quality_pts += 0.2; evidence.append(("quality", "opening_range_formed", +0.2))
    # flow agreement only counts if flow data is actually wired (it isn't yet)
    if of.get("data_status") == "available" and of.get("net_directional_delta") is not None:
        # placeholder for when side-classified flow lands
        quality_pts += 0.2; evidence.append(("quality", "flow_agreement_measurable", +0.2))
    else:
        evidence.append(("quality", "flow_intent_unavailable", 0.0))
    trade_quality = round(max(0.0, min(1.0, quality_pts)), 3)

    # ---- EXECUTABILITY score (spread / liquidity / affordability) -----------
    exec_pts = 0.0
    msp = liq.get("median_spread_pct")
    if msp is not None:
        if msp <= 5:
            exec_pts += 0.4; evidence.append(("executability", f"median_spread_{msp}pct_ok", +0.4))
        elif msp <= 10:
            exec_pts += 0.2; evidence.append(("executability", f"median_spread_{msp}pct_marginal", +0.2))
        else:
            vetoes.append("spread_too_wide")
            evidence.append(("executability", f"median_spread_{msp}pct_wide", -0.4))
    moi = liq.get("median_open_interest")
    if moi is not None and moi >= 100:
        exec_pts += 0.3; evidence.append(("executability", f"median_oi_{moi}_ok", +0.3))
    elif moi is not None:
        vetoes.append("thin_open_interest")
    qc = liq.get("qualified_contracts")
    if qc is not None and qc >= 3:
        exec_pts += 0.3; evidence.append(("executability", f"{qc}_in_band_contracts", +0.3))
    elif qc == 0:
        vetoes.append("no_qualified_contracts")
    executability = round(max(0.0, min(1.0, exec_pts)), 3)

    # ---- decision: prediction stays separate from policy (§12) ---------------
    # This is NOT a trade approval. It is a research read. NO_TRADE dominates on
    # any veto or when direction is neutral.
    if vetoes:
        decision = "NO_TRADE"
    elif direction == "NEUTRAL":
        decision = "NEUTRAL"
    else:
        decision = direction

    return {
        "schema_version": SCHEMA_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "ticker": fr.get("ticker"),
        "timestamp": fr.get("timestamp"),
        "decision": decision,
        # three SEPARATE scores — never combined into one number
        "direction": direction,
        "direction_score": round(dir_pts, 3),
        "trade_quality_score": trade_quality,
        "executability_score": executability,
        "vetoes": vetoes,
        "evidence": [{"layer": l, "reason": r, "weight": w} for (l, r, w) in evidence],
        "scores_are_independent": True,
        "note": ("Rules-based research read, no probability, no order. A directional "
                 "read, the setup quality, and the executability are reported "
                 "separately on purpose (§5/§12): a right direction can still be an "
                 "un-tradeable or low-quality setup."),
    }


# ── per-contract target-before-stop labels (ALL in-band contracts) ──────────

def _proxy_option_price(entry_mid, delta, gamma, du):
    """Delta-gamma proxy for an option's per-share price after an underlying move.

    FORMULA (second-order Taylor expansion of option price in the underlying):
        P(du) = max(0, P0 + Δ·du + ½·Γ·du²)

    UNITS: `entry_mid` (P0), the result, Δ and Γ are all per-SHARE (Δ in
    $/$, Γ in $/$²), and `du` is the underlying move in dollars
    (U_t − U0). Percentage returns are computed as (P − P0)/P0, so the ×100
    option contract multiplier cancels and never enters the proxy — returns are
    per-premium, not per-contract-dollar. Absolute $ P&L (×100×qty) is
    deliberately NOT produced here.

    SIGNING: Δ and Γ are frozen at decision time (from the workup greeks) and are
    NEVER recomputed later — the label reads the frozen quote. A call has Δ>0, a
    put Δ<0; Γ≥0 for both. Callers pass +du for an up-move and −du for a
    down-move; the caller (label scan) chooses the underlying extreme (high vs
    low) that is favorable/adverse for the option's sign, so puts gain on
    down-moves correctly.

    CLAMP: the result is floored at 0 — a proxy can never return a negative
    (impossible) option value even for a large adverse `du`.

    This is a LABELED PROXY: it ignores IV change, theta decay, and the real
    bid/ask at exit. Realized fills supersede it under a later label version."""
    g = gamma or 0.0
    p = entry_mid + (delta or 0.0) * du + 0.5 * g * du * du
    return max(0.0, p)


def label_contract(contract, session_minute, session_bars):
    """Target-before-stop label for ONE contract, from the realized UNDERLYING
    bar path translated to option terms by the delta+gamma proxy. Uses ONLY bars
    strictly after `session_minute` (no future-data leak). Returns a label dict
    with `label_basis` and `realized_option_path: false` so a proxy label is
    never mistaken for a realized one."""
    entry_mid = contract.get("mid")
    delta = contract.get("delta")
    gamma = contract.get("gamma")
    is_call = contract.get("type") == "C"
    frozen_quote = {k: contract.get(k) for k in
                    ("symbol", "expiry", "strike", "type", "dte", "bid", "ask",
                     "mid", "spread_pct", "iv", "delta", "gamma", "theta", "oi",
                     "volume", "oi_since_prev")}

    base = {
        "symbol": contract.get("symbol"),
        "label_version": SCHEMA_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "label_basis": "delta_gamma_proxy",
        "realized_option_path": False,
        "proxy_ignores": ["iv_change", "theta_decay", "real_bid_ask_at_exit",
                          "real_fills"],
        "frozen_quote": frozen_quote,
    }
    if entry_mid is None or entry_mid <= 0 or delta is None or session_minute is None:
        base.update({"available": False, "reason": "insufficient_contract_or_context"})
        return base
    # need the underlying's decision-time price to translate moves
    u0 = _underlying_at(session_bars, session_minute)
    future = [b for b in session_bars if b["t"] > session_minute
              and b["t"] <= session_minute + LABEL_HORIZON_MIN]
    if u0 is None or not future:
        base.update({"available": False, "reason": "no_forward_underlying_bars"})
        return base

    target = entry_mid * (1 + LABEL_TARGET_PCT)
    stop = entry_mid * (1 - LABEL_STOP_PCT)
    first_hit, mins, exit_px = None, None, None
    mfe = mae = 0.0
    for b in future:
        # option price at bar extremes via proxy (call: high underlying → high
        # option; put: low underlying → high option)
        du_hi = b["high"] - u0
        du_lo = b["low"] - u0
        p_hi = _proxy_option_price(entry_mid, delta, gamma, du_hi if is_call else du_lo)
        p_lo = _proxy_option_price(entry_mid, delta, gamma, du_lo if is_call else du_hi)
        mfe = max(mfe, (p_hi - entry_mid) / entry_mid)
        mae = min(mae, (p_lo - entry_mid) / entry_mid)
        hit_tgt = p_hi >= target
        hit_stop = p_lo <= stop
        if hit_tgt and hit_stop:
            first_hit, mins = "ambiguous_same_bar", b["t"] - session_minute; break
        if hit_stop:
            first_hit, mins, exit_px = "stop", b["t"] - session_minute, stop; break
        if hit_tgt:
            first_hit, mins, exit_px = "target", b["t"] - session_minute, target; break
    if exit_px is None:
        # no hit — mark to last proxy close
        b = future[-1]
        du = b["close"] - u0
        exit_px = _proxy_option_price(entry_mid, delta, gamma,
                                      du if is_call else -du)
    gross = exit_px - entry_mid
    costs = LABEL_COST_BPS / 1e4 * entry_mid
    base.update({
        "available": True,
        "target_pct": LABEL_TARGET_PCT, "stop_pct": LABEL_STOP_PCT,
        "horizon_min": LABEL_HORIZON_MIN,
        "target_before_stop": first_hit == "target",
        "stop_before_target": first_hit == "stop",
        "first_hit": first_hit,               # target | stop | ambiguous_same_bar | None
        "minutes_to_first_hit": mins,
        "mfe_pct": round(mfe, 6), "mae_pct": round(mae, 6),
        "gross_return_pct": round(gross / entry_mid, 6),
        "net_return_pct": round((gross - costs) / entry_mid, 6),
        "exit_reason": first_hit or "horizon_end",
        "costs_note": "PLACEHOLDER cost model; realized bid/ask+fees under a later label version",
    })
    return base


def _underlying_at(bars, minute):
    chosen = None
    for b in bars:
        if b["t"] <= minute:
            chosen = b
        else:
            break
    return chosen["close"] if chosen else None


def label_in_band(feature_record, contracts, session_bars):
    """Label EVERY in-band contract (delta 0.15–0.70) for the decision minute in
    `feature_record`. Append-only; returns the list of label dicts."""
    sm = _g(feature_record, "underlying", "session_minute")
    band = in_band_contracts(contracts)
    labels = [label_contract(c, sm, session_bars) for c in band]
    return labels


# ── append-only stores (immutable, non-qualifying; crash-safe writes) ────────

# Dedup-event log: one line each time a same-minute re-fire is prevented. Lets
# the validation report count duplicates-prevented without tracking in memory.
SNAPSHOT_DEDUP_LOG = "scalpr_intel_snapshot_dedup_v0.jsonl"


def persist_feature_record(fr, path=FEATURES_LOG):
    _atomic_append(path, fr)
    return fr.get("timestamp")


def persist_contract_labels(ticker, market_date, labels, path=CONTRACT_LABELS_LOG):
    rec = {"ticker": ticker, "market_date": str(market_date),
           "recorded_at": datetime.now(timezone.utc).isoformat(),
           "label_version": SCHEMA_VERSION,
           "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
           "labels": labels}
    _atomic_append(path, rec)
    return len(labels)


# ── decision-time immutable snapshot (frozen contract universe + hashes) ─────

def frozen_contract_universe(contracts, quote_as_of=None, decision_ts=None):
    """The COMPLETE in-band (delta 0.15–0.70) contract set frozen at decision
    time — every labelable contract in the configured band, NOT a top-N,
    affordable, or policy-approved subset. Each entry carries its full frozen
    quote, a `contract_snapshot_hash`, and a decision-time quote-quality
    assessment (crossed/locked/missing/stale/unusable + age) so the labeler can
    reject or warn on bad quotes. Never recomputed later."""
    universe = []
    for c in in_band_contracts(contracts):
        fq = {k: c.get(k) for k in FROZEN_QUOTE_FIELDS}
        quality, warning, age_sec = assess_quote_quality(c, decision_ts, quote_as_of)
        universe.append({
            **fq,
            "contract_snapshot_hash": canonical_hash(fq),
            "quote_source_as_of": quote_as_of,
            "quote_age_sec": age_sec,
            "quote_quality": quality,
            "quote_quality_warning": warning,
            "quote_bucket": quote_bucket(quality, warning),
        })
    return universe


def _decision_id(ticker, market_date, iso_ts):
    """Version-scoped, minute-granular id. Identity is
    (ticker, session_date, decision-minute, schema, universe, rules) so a
    same-minute re-fire de-dupes but a version change never collides with an old
    snapshot. Deterministic: one snapshot per ticker per minute per version set."""
    try:
        hhmm = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).strftime("%H%M")
    except Exception:
        hhmm = "0000"
    return (f"{ticker.upper()}:{market_date}:{hhmm}"
            f":{SCHEMA_VERSION}:{CONTRACT_UNIVERSE_VERSION}:{RULES_SCORE_VERSION}")


def persist_feature_snapshot(fr, contracts, market_date, quote_as_of=None,
                             path=FEATURE_SNAPSHOTS_LOG):
    """Write ONE immutable decision-time snapshot: the full feature record plus
    the COMPLETE frozen in-band contract universe, each hashed, crash-safely
    (atomic append + fsync). Idempotent by the version-scoped decision_id — a
    re-fire in the same minute is a no-op (logged to SNAPSHOT_DEDUP_LOG) and the
    original is never overwritten. Returns the decision_id, or None on dedup."""
    ticker = fr.get("ticker")
    decision_ts = fr.get("timestamp", "")
    decision_id = _decision_id(ticker, market_date, decision_ts)
    for existing in _iter_jsonl(path):               # tolerant scan (skips corrupt)
        if existing.get("decision_id") == decision_id:
            _atomic_append(SNAPSHOT_DEDUP_LOG,
                           {"decision_id": decision_id, "ticker": ticker,
                            "market_date": str(market_date),
                            "prevented_at": datetime.now(timezone.utc).isoformat()})
            return None                              # already frozen — immutable
    universe = frozen_contract_universe(contracts, quote_as_of=quote_as_of,
                                        decision_ts=decision_ts)
    underlying_at_decision = (_g(fr, "underlying", "last")
                              if _g(fr, "underlying", "last") is not None
                              else fr.get("spot_price"))
    snap = {
        "decision_id": decision_id,
        "schema_version": SCHEMA_VERSION,
        "contract_universe_version": CONTRACT_UNIVERSE_VERSION,
        "rules_score_version": RULES_SCORE_VERSION,
        "formal_cohort_eligible": FORMAL_COHORT_ELIGIBLE,
        "ticker": ticker,
        "market_date": str(market_date),
        "decision_minute_utc": (datetime.fromisoformat(str(decision_ts).replace("Z", "+00:00"))
                                .replace(second=0, microsecond=0).isoformat()
                                if decision_ts else None),
        "decision_timestamp": decision_ts,
        "session_minute": _g(fr, "underlying", "session_minute"),
        "underlying_at_decision": underlying_at_decision,
        "quote_source_as_of": quote_as_of,
        "label_horizon_min": LABEL_HORIZON_MIN,
        "label_target_pct": LABEL_TARGET_PCT,
        "label_stop_pct": LABEL_STOP_PCT,
        "feature_snapshot_hash": canonical_hash(fr),
        "feature_record": fr,                         # frozen, immutable
        "contract_universe": universe,                # COMPLETE in-band set
    }
    return decision_id if _atomic_append(path, snap) else None


def read_feature_snapshots(path=FEATURE_SNAPSHOTS_LOG, ticker=None, market_date=None):
    """Read snapshots, skipping any corrupt/incomplete line and any record
    missing its identity/hash (an incomplete artifact is ignored, not trusted)."""
    out = []
    for r in _iter_jsonl(path):
        if not r.get("decision_id") or not r.get("feature_snapshot_hash"):
            continue                                  # incomplete/temp artifact
        if ticker and r.get("ticker") != ticker.upper():
            continue
        if market_date and str(r.get("market_date")) != str(market_date):
            continue
        out.append(r)
    return out
