"""
Wave Riding runner + three-track comparison (wave-riding-baseline-v0). SHADOW.

Provides:
  * `WaveRunner` — streaming engine+adapter driver (one frozen observation at a
    time), used by the server shadow hook and tests; optional durable store.
  * `build_tracks` — three INDEPENDENT simulated tracks from the SAME seed
    (source observation, contract, initial timestamp, initial quote, simulated
    initial fill, quote-quality decision):
        Baseline A : one simulated contract under the STANDARD ratchet rules
        Baseline B : one simulated contract held to the WR terminal timestamp
        Wave Riding: one simulated contract + momentum adds + WR reversal exit
    None of the tracks touches the live Guard/Platform.

`simulate_standard_ratchet` mirrors `scalp_server.Guard.on_price` (profit % off
entry, laddered giveback tolerance, grace + confirm_ticks, whole-position exit).
It is a pure re-implementation for simulation ONLY — it must stay in sync with
the live Guard rules and never calls or mutates the live Guard.
"""
import feature_engine as fe
import wave_riding as wr
from wave_config import BASELINE_VERSION
from wave_order_adapter import ShadowOrderAdapter

# Representative standard ladder for Baseline A (mirrors the ratchet test fixture;
# in a live comparison this would come from the user's actual guard config).
DEFAULT_LADDER = [{"at": 0, "tol": 15}, {"at": 15, "tol": 6}, {"at": 30, "tol": 2.5}]

# Baseline-policy provenance. The simulated Baseline A re-implements the live
# Guard RULES; these stamps let a parity test detect drift without refactoring
# the live Guard. `LIVE_GUARD_REFERENCE_VERSION` mirrors Guard.EXECUTION_MODE.
SIMULATED_STANDARD_POLICY_VERSION = "wave-riding-baseline-v0"
LIVE_GUARD_REFERENCE_VERSION = "WHOLE_POSITION_RATCHET"
_STD_CONFIRM_TICKS = 2
_STD_GRACE_SECONDS = 60


def standard_policy_hash(ladder=None, confirm_ticks=_STD_CONFIRM_TICKS,
                         grace_seconds=_STD_GRACE_SECONDS):
    return fe.canonical_hash({"ladder": ladder or DEFAULT_LADDER,
                              "confirm_ticks": confirm_ticks,
                              "grace_seconds": grace_seconds,
                              "price_basis": "executable_bid",
                              "exit": "whole_position"})


# ── streaming runner (engine + adapter, synchronous shadow fills) ───────────

class WaveRunner:
    def __init__(self, config, adapter=None):
        self.cfg = config
        self.engine = wr.WaveEngine(config)
        self.adapter = adapter or ShadowOrderAdapter()

    def seed(self, position_id, side, underlying_symbol, contract_symbol,
             initial_obs, source=None):
        pos = wr.new_position(position_id, side, underlying_symbol, contract_symbol,
                              self.cfg, source=source)
        pos["state"] = wr.INITIAL_ORDER_PENDING
        fill = self.adapter.fill_add(initial_obs, self.cfg.initial_contracts, self.cfg)
        if not fill.get("filled"):
            return pos, fill                      # stays pending; no simulated position
        pos = self.engine.seed_initial_fill(pos, fill, initial_obs)
        return pos, fill

    def step(self, pos, obs, store=None):
        dec = self.engine.observe(pos, obs)
        pos = dec["position"]
        if store:
            store.append_observation(dec["audit_payload"])
        act = dec["intended_action"]
        if act == wr.ACT_SUBMIT_ADD:
            oi = dec["order_intent"]
            if store and store.order_seen(oi["idempotency_key"]):
                pos = self.engine.reject_add(pos, "duplicate", obs)   # already filled
            else:
                fill = self.adapter.fill_add(obs, oi["qty"], self.cfg)
                if fill.get("filled"):
                    if store:
                        store.append_order({**oi, **fill})
                    pos = self.engine.apply_add_fill(pos, fill, obs)
                else:
                    pos = self.engine.reject_add(pos, fill.get("reason"), obs)
        elif act == wr.ACT_SUBMIT_EXIT:
            oi = dec["order_intent"]
            fill = self.adapter.fill_exit(obs, oi["qty"], self.cfg)
            if fill.get("filled"):
                if store:
                    store.append_exit({**oi, **fill})
                pos = self.engine.apply_exit_fill(pos, fill)
            else:
                pos = dict(pos)
                pos["state"] = wr.MONITORING_WAVE     # retry exit on next valid tick
        if store:
            store.save_position(pos)
        return pos, dec


# ── Wave Riding batch track ─────────────────────────────────────────────────

def run_wave_track(config, observations, source=None):
    """Run the WR engine over a frozen, ordered observation list (no look-ahead:
    each observation is exposed sequentially). Liquidates any remainder at the
    terminal observation so the track has a complete end-to-end result."""
    runner = WaveRunner(config)
    obs0 = observations[0]
    pos, seed_fill = runner.seed("wr-track", obs0.get("side", "CALL"),
                                 obs0.get("underlying_symbol", "U"),
                                 obs0.get("contract_symbol", "C"), obs0, source=source)
    if not seed_fill.get("filled"):
        return {"track": "wave_riding", "error": "initial_fill_unavailable"}
    adds, exit_rec = [], None
    peak_val = pos["peak_executable_value_usd"] or 0.0
    max_cost = pos["total_cost_basis_usd"]
    max_qty = pos["open_quantity"]
    entry_cost_per = seed_fill["price"]
    for obs in observations[1:]:
        prev_adds = pos["filled_adds"]
        pos, dec = runner.step(pos, obs)
        peak_val = max(peak_val, pos.get("peak_executable_value_usd") or 0.0)
        max_cost = max(max_cost, pos["total_cost_basis_usd"])
        max_qty = max(max_qty, pos["open_quantity"])
        if pos["filled_adds"] > prev_adds:
            adds.append({"ts": obs["ts"], "add_no": pos["filled_adds"],
                         "fill_price": pos["last_add_option_fill_price"],
                         "anchor": pos["last_wave_anchor_underlying_price"],
                         "avg_cost": pos["weighted_average_option_cost"]})
        if pos["state"] in (wr.CLOSED, wr.PARTIALLY_LIQUIDATED):
            exit_rec = {"ts": obs["ts"], "reason": dec["order_intent"]["exit_reason"]
                        if dec.get("order_intent") else "reversal",
                        "realized_pnl_usd": pos["realized_pnl_usd"]}
            break
    # terminal liquidation of any remainder (end-to-end result)
    if pos["open_quantity"] > 0:
        term = observations[-1]
        fill = ShadowOrderAdapter().fill_exit(term, pos["open_quantity"], config)
        if fill.get("filled"):
            pos = wr.WaveEngine(config).apply_exit_fill(pos, fill)
            exit_rec = exit_rec or {"ts": term["ts"], "reason": "TERMINAL",
                                    "realized_pnl_usd": pos["realized_pnl_usd"]}
    final_val = pos.get("current_executable_value_usd") or 0.0
    return {
        "track": "wave_riding", "version": BASELINE_VERSION,
        "entry_cost_per_contract": entry_cost_per,
        "num_adds": pos["filled_adds"],
        "peak_contract_quantity": max_qty,
        "avg_cost_after_adds": pos["weighted_average_option_cost"],
        "max_capital_committed_usd": round(max_cost, 2),
        "peak_executable_value_usd": round(peak_val, 2),
        "net_pnl_usd": pos.get("realized_pnl_usd"),
        "profit_retained_from_peak_pct": (round(final_val / peak_val, 4) if peak_val else None),
        "exit": exit_rec, "adds": adds, "final_state": pos["state"],
    }


# ── Baseline A: standard ratchet (pure sim of Guard rules) ──────────────────

def simulate_standard_ratchet(observations, entry, ladder=None, confirm_ticks=2,
                              grace_seconds=60.0, slippage=0.02, stall_seconds=0.0,
                              stall_min_profit=20.0):
    """Pure re-implementation of scalp_server.Guard.on_price (profit % off entry on
    the executable bid, laddered giveback, grace, confirm_ticks, and the
    time-based stall exit). MUST stay in lockstep with the live Guard — the
    parity test catches drift. Never calls or mutates the live Guard."""
    ladder = sorted(ladder or DEFAULT_LADDER, key=lambda r: r["at"])
    peak = 0.0
    breach = 0
    opened = observations[0]["now"]
    peak_time = opened
    mfe = mae = 0.0
    exit_px = exit_ts = None
    reason = "standard_ratchet"
    for obs in observations:
        bid = obs.get("option_bid")
        if bid is None or not obs.get("market_open"):
            continue
        profit = (bid - entry) / entry * 100.0
        mfe, mae = max(mfe, profit), min(mae, profit)
        if profit > peak:
            peak, breach, peak_time = profit, 0, obs["now"]
            continue
        if obs["now"] - opened < grace_seconds:
            continue
        tol = ladder[0]["tol"]
        for r in ladder:
            if peak >= r["at"]:
                tol = r["tol"]
        if profit <= peak - tol:
            breach += 1
            if breach >= confirm_ticks:
                exit_px, exit_ts, reason = max(0.0, bid - slippage), obs["ts"], "standard_ratchet"
                break
            continue
        breach = 0
        if stall_seconds and peak >= stall_min_profit and obs["now"] - peak_time >= stall_seconds:
            exit_px, exit_ts, reason = max(0.0, bid - slippage), obs["ts"], "stall"
            break
    if exit_px is None:                       # never breached → terminal exit
        term = observations[-1]
        exit_px = max(0.0, (term.get("option_bid") or entry) - slippage)
        exit_ts, reason = term["ts"], "terminal"
    net = (exit_px - entry) * 100.0
    return {"track": "baseline_a_standard_ratchet", "version": BASELINE_VERSION,
            "entry_cost_per_contract": entry, "peak_pct": round(peak, 3),
            "mfe_pct": round(mfe, 3), "mae_pct": round(mae, 3),
            "num_adds": 0, "peak_contract_quantity": 1,
            "max_capital_committed_usd": round(entry * 100.0, 2),
            "exit": {"ts": exit_ts, "reason": reason, "exit_price": round(exit_px, 6)},
            "net_pnl_usd": round(net, 2)}


# ── Baseline B: buy and hold to WR terminal timestamp ──────────────────────

def run_baseline_b(observations, entry, slippage=0.02):
    term = observations[-1]
    exit_px = max(0.0, (term.get("option_bid") or entry) - slippage)
    net = (exit_px - entry) * 100.0
    mfe = mae = 0.0
    for obs in observations:
        bid = obs.get("option_bid")
        if bid is None:
            continue
        profit = (bid - entry) / entry * 100.0
        mfe, mae = max(mfe, profit), min(mae, profit)
    return {"track": "baseline_b_hold", "version": BASELINE_VERSION,
            "entry_cost_per_contract": entry, "num_adds": 0,
            "peak_contract_quantity": 1, "mfe_pct": round(mfe, 3), "mae_pct": round(mae, 3),
            "max_capital_committed_usd": round(entry * 100.0, 2),
            "exit": {"ts": term["ts"], "reason": "hold_to_terminal", "exit_price": round(exit_px, 6)},
            "net_pnl_usd": round(net, 2)}


# ── three independent tracks from the SAME seed ─────────────────────────────

def build_tracks(config, observations, ladder=None):
    """Same seed for all three tracks: same source observation, contract, initial
    timestamp, initial quote, simulated initial fill, and quote-quality decision.
    The shared initial fill is computed ONCE and reused; tracks never
    independently reconstruct their initial conditions."""
    if not observations:
        return {"error": "no_observations"}
    obs0 = observations[0]
    seed_fill = ShadowOrderAdapter().fill_add(obs0, config.initial_contracts, config)
    seed = {"contract_symbol": obs0.get("contract_symbol"),
            "side": obs0.get("side"), "initial_ts": obs0.get("ts"),
            "initial_quote": {"bid": obs0.get("option_bid"), "ask": obs0.get("option_ask")},
            "quote_quality": obs0.get("quote_quality"),
            "initial_fill": seed_fill}
    if not seed_fill.get("filled"):
        return {"seed": seed, "error": "initial_fill_unavailable_same_seed"}
    entry = seed_fill["price"]
    a = simulate_standard_ratchet(observations, entry, ladder=ladder,
                                  slippage=config.slippage_allowance)
    b = run_baseline_b(observations, entry, slippage=config.slippage_allowance)
    w = run_wave_track(config, observations)
    comparison = {t["track"]: {
        "net_pnl_usd": t.get("net_pnl_usd"),
        "num_adds": t.get("num_adds"),
        "peak_contract_quantity": t.get("peak_contract_quantity"),
        "max_capital_committed_usd": t.get("max_capital_committed_usd"),
        "exit_ts": (t.get("exit") or {}).get("ts"),
    } for t in (a, b, w)}
    return {"seed": seed, "baseline_a": a, "baseline_b": b, "wave_riding": w,
            "comparison": comparison,
            "simulated_standard_policy_version": SIMULATED_STANDARD_POLICY_VERSION,
            "simulated_standard_policy_hash": standard_policy_hash(ladder),
            "live_guard_reference_version": LIVE_GUARD_REFERENCE_VERSION,
            "note": ("Shadow, non-qualifying, experimental. Judge WR against "
                     "incremental capital, drawdown, slippage, and tail loss — "
                     "not gross profit alone.")}
