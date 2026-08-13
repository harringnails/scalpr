"""
Wave Riding v0 — PURE direction-normalized engine + explicit state machine.

Anti-martingale pyramiding: adds only to a profitable, directionally-confirmed
position; NEVER averages down; caps additions/contracts/cost/risk; exits the
whole accumulated position on a spread-adjusted reversal. SHADOW ONLY — this
module is pure (no I/O, no broker, no files, no UI). Side effects live in the
order adapter and the store.

Contract: given a frozen `observation` + current `position` + `config`, the
engine returns a Decision:
    { next_state, intended_action, reason_codes, updated_metrics, audit_payload,
      order_intent, position }
`order_intent` (when action is SUBMIT_ADD / SUBMIT_EXIT) is handed to an adapter;
the adapter's fill is then applied via `apply_add_fill` / `apply_exit_fill`
(also pure). The engine never inspects future bars/quotes/outcomes.

Direction normalization (one engine for calls and puts):
    direction_sign = +1 for CALL, -1 for PUT
    directional_move = (U_now - last_wave_anchor_underlying) * direction_sign
A positive directional_move means the underlying moved the intended way for
either side. The option's executable BID separately confirms the position
benefits.
"""
import feature_engine as fe
from wave_config import WAVE_RIDING_VERSION

# ── states (§18) ────────────────────────────────────────────────────────────
DISABLED = "DISABLED"
ARMED = "ARMED"
INITIAL_ORDER_PENDING = "INITIAL_ORDER_PENDING"
INITIAL_POSITION_OPEN = "INITIAL_POSITION_OPEN"
MONITORING_WAVE = "MONITORING_WAVE"
ADD_CONFIRMING = "ADD_CONFIRMING"
ADD_PENDING = "ADD_PENDING"
ADD_PARTIALLY_FILLED = "ADD_PARTIALLY_FILLED"
ADD_FILLED = "ADD_FILLED"
COOLDOWN = "COOLDOWN"
MAX_POSITION_REACHED = "MAX_POSITION_REACHED"
REVERSAL_TRIGGERED = "REVERSAL_TRIGGERED"
LIQUIDATING = "LIQUIDATING"
PARTIALLY_LIQUIDATED = "PARTIALLY_LIQUIDATED"
CLOSED = "CLOSED"
SUSPENDED = "SUSPENDED"
ERROR = "ERROR"

# ── actions ─────────────────────────────────────────────────────────────────
ACT_NONE = "NONE"
ACT_SUBMIT_ADD = "SUBMIT_ADD"
ACT_SUBMIT_EXIT = "SUBMIT_EXIT"

# ── reason codes ────────────────────────────────────────────────────────────
R_MARKET_CLOSED = "MARKET_CLOSED"
R_OPEN_WINDOW = "IN_OPENING_WINDOW"
R_CLOSE_WINDOW = "IN_CLOSING_WINDOW"
R_QUOTE_BAD = "QUOTE_INVALID"          # crossed/missing/unusable/stale
R_QUOTE_OLD = "QUOTE_TOO_OLD"
R_SPREAD_WIDE = "SPREAD_TOO_WIDE"
R_ATR_WARMUP = "ADD_BLOCKED_ATR_WARMUP"
R_NOT_PROFITABLE = "POSITION_NOT_PROFITABLE"
R_UNDERLYING_SHORT = "UNDERLYING_MOVE_INSUFFICIENT"
R_TREND_MISALIGNED = "TREND_MISALIGNED"
R_OPTION_NOT_CONFIRMED = "OPTION_VALUE_NOT_CONFIRMED"
R_COOLDOWN = "COOLDOWN_ACTIVE"
R_MAX_ADDS = "MAX_ADDS_REACHED"
R_MAX_CONTRACTS = "MAX_CONTRACTS_REACHED"
R_MAX_COST = "MAX_COST_REACHED"
R_MAX_INCR_COST = "MAX_INCREMENTAL_COST_REACHED"
R_MAX_RISK = "MAX_RISK_REACHED"
R_PROFIT_FUNDED = "PROFIT_FUNDED_GATE_FAILED"
R_HARD_VETO = "HARD_VETO_ACTIVE"
R_ADD_PENDING = "ADD_ALREADY_PENDING"
R_UNSYNCHRONIZED = "OBSERVATION_UNSYNCHRONIZED"   # underlying/option quote skew too large
R_ELIGIBLE = "ALL_ADD_CONDITIONS_MET"

# exit reason codes (precedence order)
X_EMERGENCY = "EMERGENCY_KILL_SWITCH"
X_HARD_STOP = "HARD_MAX_LOSS_STOP"
X_DATA_SAFETY = "MARKET_DATA_SAFETY_EXIT"
X_REVERSAL = "WAVE_RIDING_REVERSAL"


# ── position record (§19 + referential source_* + internal timers) ──────────

def new_position(position_id, side, underlying_symbol, contract_symbol,
                 config, source=None):
    """Create an ARMED, fully-simulated Wave Riding position. `source` carries
    REFERENTIAL-ONLY linkage to any real trade — never real-position state."""
    side = side.upper()
    assert side in ("CALL", "PUT")
    src = source or {}
    return {
        "position_id": position_id,
        "strategy_mode": "WAVE_RIDING",
        "strategy_version": WAVE_RIDING_VERSION,
        "simulated": True,
        "direction": side,
        "direction_sign": 1 if side == "CALL" else -1,
        "underlying_symbol": underlying_symbol,
        "contract_symbol": contract_symbol,
        "state": ARMED,
        "wave_sequence": 0,
        "initial_quantity": config.initial_contracts,
        "open_quantity": 0,
        "filled_adds": 0,
        "max_adds": config.max_adds,
        "max_total_contracts": config.max_total_contracts,
        "initial_underlying_price": None,
        "last_wave_anchor_underlying_price": None,
        "initial_option_fill_price": None,
        "last_add_option_fill_price": None,
        "last_add_exec_bid": None,
        "weighted_average_option_cost": None,
        "total_cost_basis_usd": 0.0,
        "current_executable_value_usd": None,
        "peak_executable_value_usd": None,
        "unrealized_pnl_usd": None,
        "unrealized_pnl_pct": None,
        "effective_giveback_pct": None,
        "frozen_wave_atr": None,
        "frozen_atr_quality": None,
        "realized_pnl_usd": None,
        "last_add_timestamp": None,
        "cooldown_until": None,          # epoch seconds
        "confirm_started_at": None,      # epoch seconds
        "peak_pending_value": None,
        "peak_pending_count": 0,
        "pending_order_key": None,
        "awaiting_fresh_confirmation": False,   # set on restart
        "last_valid_quote_timestamp": None,
        # referential-only link to a real decision/position (never mutated here)
        "source_decision_id": src.get("source_decision_id"),
        "source_position_id": src.get("source_position_id"),
        "source_contract_symbol": src.get("source_contract_symbol"),
        "source_activation_timestamp": src.get("source_activation_timestamp"),
        "research_cohort_id": src.get("research_cohort_id"),
        "research_scope_version": src.get("research_scope_version"),
        "created_at": None,
        "updated_at": None,
    }


# ── idempotency ─────────────────────────────────────────────────────────────

def idempotency_key(pos, intended_qty, signal_ts):
    return fe.canonical_hash([pos["position_id"], pos["wave_sequence"],
                              pos["contract_symbol"], intended_qty, signal_ts,
                              pos["strategy_version"]])


# ── engine ──────────────────────────────────────────────────────────────────

class WaveEngine:
    def __init__(self, config):
        self.cfg = config.validate()

    # -- helpers --
    def _quote_valid(self, obs):
        """Executable-quote validity gate; returns (ok, reason|None)."""
        if not obs.get("market_open"):
            return False, R_MARKET_CLOSED
        q = obs.get("quote_quality")
        if q in ("crossed", "missing", "unusable", "stale"):
            return False, R_QUOTE_BAD
        bid, ask = obs.get("option_bid"), obs.get("option_ask")
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return False, R_QUOTE_BAD
        age = obs.get("quote_age_sec")
        if age is not None and age > self.cfg.max_quote_age_seconds:
            return False, R_QUOTE_OLD
        sp = obs.get("spread_pct")
        if sp is not None and sp > self.cfg.max_spread_pct:
            return False, R_SPREAD_WIDE
        return True, None

    def _exec_value(self, pos, obs):
        bid = obs.get("option_bid")
        if bid is None or pos["open_quantity"] <= 0:
            return None
        return bid * pos["open_quantity"] * 100.0

    def _effective_giveback(self, obs):
        cfg = self.cfg
        sp = obs.get("spread_pct") or 0.0
        # volatility_noise_floor stubbed/disabled — not fabricated
        return max(cfg.configured_giveback_pct, cfg.spread_noise_multiplier * sp)

    # -- seeding the fully-simulated initial position --
    def seed_initial_fill(self, pos, fill, obs):
        """INITIAL_ORDER_PENDING → INITIAL_POSITION_OPEN → MONITORING_WAVE.
        `fill` = {price, ts, qty}; freezes the first wave ATR from obs."""
        p = dict(pos)
        qty = fill["qty"]
        p["open_quantity"] = qty
        p["initial_underlying_price"] = obs["underlying_price"]
        p["last_wave_anchor_underlying_price"] = obs["underlying_price"]
        p["initial_option_fill_price"] = fill["price"]
        p["last_add_option_fill_price"] = fill["price"]
        p["last_add_exec_bid"] = obs.get("option_bid")
        p["total_cost_basis_usd"] = fill["price"] * qty * 100.0
        p["weighted_average_option_cost"] = fill["price"]
        exec_v = (obs.get("option_bid") or 0.0) * qty * 100.0
        p["current_executable_value_usd"] = exec_v
        p["peak_executable_value_usd"] = exec_v
        p["frozen_wave_atr"] = obs.get("atr_value")
        p["frozen_atr_quality"] = obs.get("atr_quality")
        p["last_add_timestamp"] = fill["ts"]
        p["last_valid_quote_timestamp"] = obs.get("ts")
        p["wave_sequence"] = 1
        p["state"] = MONITORING_WAVE
        p["created_at"] = p["created_at"] or fill["ts"]
        p["updated_at"] = fill["ts"]
        return p

    # -- the add-condition gate (§5, 17 conditions) --
    def evaluate_add(self, pos, obs):
        """Returns (eligible: bool, reasons: list, metrics: dict). `reasons` are
        blocking codes when not eligible; [R_ELIGIBLE] when eligible."""
        cfg = self.cfg
        reasons = []
        sign = pos["direction_sign"]
        u = obs["underlying_price"]
        anchor = pos["last_wave_anchor_underlying_price"]
        directional_move = (u - anchor) * sign
        atr = pos.get("frozen_wave_atr")
        required_move = (cfg.add_trigger_atr_fraction * atr) if atr else None
        bid = obs.get("option_bid")
        last_bid = pos.get("last_add_exec_bid")
        add_qty = cfg.add_contracts_per_wave
        proj_fill = (obs.get("option_ask") or 0.0) + cfg.slippage_allowance
        proj_incr_cost = proj_fill * add_qty * 100.0
        proj_total_cost = pos["total_cost_basis_usd"] + proj_incr_cost
        exec_v = self._exec_value(pos, obs)
        metrics = {
            "directional_move": round(directional_move, 6),
            "required_directional_move": (round(required_move, 6) if required_move is not None else None),
            "frozen_wave_atr": atr,
            "atr_quality": obs.get("atr_quality"),
            "projected_fill_price": round(proj_fill, 6),
            "projected_incremental_cost_usd": round(proj_incr_cost, 2),
            "projected_total_cost_usd": round(proj_total_cost, 2),
            "current_executable_value_usd": (round(exec_v, 2) if exec_v is not None else None),
        }

        # unsynchronized underlying/option pair → block add (a higher-priority
        # simulated emergency is handled separately in _exit_reason)
        if obs.get("unsynchronized"):
            reasons.append(R_UNSYNCHRONIZED)
        # 8/9 quote validity + spread (also covers freshness)
        ok, qr = self._quote_valid(obs)
        if not ok:
            reasons.append(qr)
        # 15 hard veto / event / emergency
        if obs.get("hard_veto"):
            reasons.append(R_HARD_VETO)
        # 17 market session windows
        if not obs.get("market_open"):
            reasons.append(R_MARKET_CLOSED)
        if obs.get("in_open_window"):
            reasons.append(R_OPEN_WINDOW)
        if obs.get("in_close_window"):
            reasons.append(R_CLOSE_WINDOW)
        # ATR warm-up (15/required-evidence)
        if atr is None or obs.get("atr_quality") == "WARMUP_BLOCKED":
            reasons.append(R_ATR_WARMUP)
        # 4 profitability (executable bid) — NEVER average down
        if cfg.require_position_profitable and exec_v is not None:
            if exec_v <= pos["total_cost_basis_usd"]:
                reasons.append(R_NOT_PROFITABLE)
        # 5 underlying moved required distance from the FROZEN goalpost
        if required_move is None or directional_move < required_move:
            reasons.append(R_UNDERLYING_SHORT)
        # 6 trend aligned
        if cfg.require_vwap_alignment:
            if obs.get("trend_ok") is False or obs.get("above_vwap_side_ok") is False:
                reasons.append(R_TREND_MISALIGNED)
        # 7 option executable value confirms
        if bid is None or last_bid is None or bid <= last_bid:
            reasons.append(R_OPTION_NOT_CONFIRMED)
        else:
            gain_pct = (bid - last_bid) / last_bid * 100.0
            if gain_pct < cfg.minimum_option_gain_for_add_pct:
                reasons.append(R_OPTION_NOT_CONFIRMED)
        # 11 cooldown
        if pos.get("cooldown_until") and obs["now"] < pos["cooldown_until"]:
            reasons.append(R_COOLDOWN)
        # 12 max adds
        if pos["filled_adds"] >= cfg.max_adds:
            reasons.append(R_MAX_ADDS)
        # 13 max total contracts
        if pos["open_quantity"] + add_qty > cfg.max_total_contracts:
            reasons.append(R_MAX_CONTRACTS)
        # 14 cost / incremental / risk caps
        if proj_total_cost > cfg.max_total_cost_usd:
            reasons.append(R_MAX_COST)
        if proj_incr_cost > cfg.max_incremental_cost_usd:
            reasons.append(R_MAX_INCR_COST)
        if proj_total_cost > cfg.max_position_risk_usd:
            reasons.append(R_MAX_RISK)
        # profit-funded gate
        if cfg.require_profit_funded_add and exec_v is not None:
            unreal = exec_v - pos["total_cost_basis_usd"]
            if unreal < proj_incr_cost * cfg.required_profit_coverage_ratio:
                reasons.append(R_PROFIT_FUNDED)
        # 16 no add pending
        if pos.get("pending_order_key"):
            reasons.append(R_ADD_PENDING)

        eligible = not reasons
        return eligible, (reasons if reasons else [R_ELIGIBLE]), metrics

    # -- exit precedence (§15) --
    def _exit_reason(self, pos, obs):
        """Highest-priority exit reason, or None. Emergency > hard stop > data
        safety > reversal. (Stall/time omitted in v0.)"""
        if obs.get("emergency_exit"):
            return X_EMERGENCY
        exec_v = self._exec_value(pos, obs)
        ok, _ = self._quote_valid(obs)
        # hard max-loss stop (simulated, off cost basis)
        if exec_v is not None and ok:
            loss = pos["total_cost_basis_usd"] - exec_v
            if loss >= self.cfg.max_position_risk_usd:
                return X_HARD_STOP
        if obs.get("data_safety_exit"):
            return X_DATA_SAFETY
        # ordinary reversal is BLOCKED on an unsynchronized pair (emergency/hard
        # stop above still fire) — the two quotes don't describe the same instant
        if obs.get("unsynchronized"):
            return None
        # reversal (only on a valid quote and an established peak)
        if ok and exec_v is not None and pos.get("peak_executable_value_usd"):
            peak = pos["peak_executable_value_usd"]
            if peak > 0:
                giveback = (peak - exec_v) / peak * 100.0
                if giveback >= self._effective_giveback(obs):
                    return X_REVERSAL
        return None

    def _update_peak(self, pos, obs):
        """Peak on executable value; only advances on a VALID quote; optional
        N-observation confirmation so one anomalous bid can't set a false peak.
        An unsynchronized pair never advances the peak (the option bid can't be
        trusted against the underlying at that instant)."""
        if obs.get("unsynchronized"):
            return pos
        ok, _ = self._quote_valid(obs)
        exec_v = self._exec_value(pos, obs)
        if not ok or exec_v is None:
            return pos
        p = dict(pos)
        p["current_executable_value_usd"] = round(exec_v, 2)
        p["unrealized_pnl_usd"] = round(exec_v - p["total_cost_basis_usd"], 2)
        p["last_valid_quote_timestamp"] = obs.get("ts")
        cur_peak = p.get("peak_executable_value_usd") or 0.0
        if exec_v > cur_peak:
            n = self.cfg.peak_confirmation_observations
            if n <= 1:
                p["peak_executable_value_usd"] = round(exec_v, 2)
                p["peak_pending_value"], p["peak_pending_count"] = None, 0
            else:
                if p.get("peak_pending_value") is not None and exec_v >= p["peak_pending_value"]:
                    p["peak_pending_count"] += 1
                else:
                    p["peak_pending_value"], p["peak_pending_count"] = round(exec_v, 2), 1
                if p["peak_pending_count"] >= n:
                    p["peak_executable_value_usd"] = p["peak_pending_value"]
                    p["peak_pending_value"], p["peak_pending_count"] = None, 0
        return p

    # -- the main step --
    def observe(self, pos, obs):
        """Advance the state machine by one frozen observation. Returns a Decision
        dict including the new position. Pure — no I/O, no future data."""
        cfg = self.cfg
        pos = self._update_peak(pos, obs)
        state = pos["state"]
        action, order_intent, reasons = ACT_NONE, None, []
        eff_gb = self._effective_giveback(obs)
        pos["effective_giveback_pct"] = round(eff_gb, 4)
        pos["updated_at"] = obs.get("ts")

        # terminal / inactive states: nothing to do
        if state in (DISABLED, ARMED, INITIAL_ORDER_PENDING, CLOSED, ERROR):
            return self._decision(pos, state, ACT_NONE, ["INACTIVE_STATE"], {}, None)
        if state == SUSPENDED:
            # only exits still evaluated; adds blocked until rearm
            xr = self._exit_reason(pos, obs)
            if xr:
                return self._trigger_exit(pos, obs, xr)
            return self._decision(pos, SUSPENDED, ACT_NONE, ["SUSPENDED"], {}, None)

        # exits have priority in every active state
        xr = self._exit_reason(pos, obs)
        if xr:
            return self._trigger_exit(pos, obs, xr)

        # cooldown
        if state == COOLDOWN:
            if pos.get("cooldown_until") and obs["now"] >= pos["cooldown_until"]:
                pos["state"] = MONITORING_WAVE
                state = MONITORING_WAVE
            else:
                return self._decision(pos, COOLDOWN, ACT_NONE, [R_COOLDOWN], {}, None)

        if state == MAX_POSITION_REACHED:
            return self._decision(pos, MAX_POSITION_REACHED, ACT_NONE,
                                  [R_MAX_CONTRACTS], {}, None)

        # after restart we require a fresh confirmation interval before adding
        if pos.get("awaiting_fresh_confirmation"):
            pos["awaiting_fresh_confirmation"] = False
            pos["confirm_started_at"] = None
            pos["state"] = MONITORING_WAVE
            return self._decision(pos, MONITORING_WAVE, ACT_NONE,
                                  ["AWAIT_FRESH_CONFIRMATION"], {}, None)

        # monitoring / confirming
        eligible, add_reasons, metrics = self.evaluate_add(pos, obs)

        if state == MONITORING_WAVE:
            if eligible:
                pos["state"] = ADD_CONFIRMING
                pos["confirm_started_at"] = obs["now"]
                return self._decision(pos, ADD_CONFIRMING, ACT_NONE,
                                      ["CONFIRMATION_STARTED"], metrics, None)
            return self._decision(pos, MONITORING_WAVE, ACT_NONE, add_reasons, metrics, None)

        if state == ADD_CONFIRMING:
            if not eligible:
                pos["state"] = MONITORING_WAVE
                pos["confirm_started_at"] = None      # RESET timer on any failure
                return self._decision(pos, MONITORING_WAVE, ACT_NONE,
                                      add_reasons + ["CONFIRMATION_RESET"], metrics, None)
            elapsed = obs["now"] - (pos.get("confirm_started_at") or obs["now"])
            if elapsed >= cfg.confirmation_seconds:
                # confirmation complete + risk authorized → submit add
                add_qty = cfg.add_contracts_per_wave
                key = idempotency_key(pos, add_qty, obs["ts"])
                pos["state"] = ADD_PENDING
                pos["pending_order_key"] = key
                order_intent = {
                    "idempotency_key": key, "action": "ADD", "qty": add_qty,
                    "price_basis": "ask_plus_slippage",
                    "signal_ts": obs["ts"], "wave_sequence": pos["wave_sequence"],
                    "contract_symbol": pos["contract_symbol"],
                    "position_id": pos["position_id"],
                    "strategy_version": pos["strategy_version"],
                }
                return self._decision(pos, ADD_PENDING, ACT_SUBMIT_ADD,
                                      [R_ELIGIBLE, "CONFIRMATION_COMPLETE"], metrics,
                                      order_intent)
            return self._decision(pos, ADD_CONFIRMING, ACT_NONE,
                                  ["CONFIRMING", f"elapsed={round(elapsed,3)}s"], metrics, None)

        if state == ADD_PENDING:
            # awaiting fill resolution (runner resolves synchronously in shadow)
            return self._decision(pos, ADD_PENDING, ACT_NONE, [R_ADD_PENDING], {}, None)

        return self._decision(pos, state, ACT_NONE, ["NOOP"], {}, None)

    # -- fills (pure state updates applied by the runner after the adapter) --
    def apply_add_fill(self, pos, fill, obs):
        """ADD_PENDING → ADD_FILLED → COOLDOWN. Updates anchor, qty, cost, count,
        re-freezes ATR, rebases peak, starts cooldown, bumps wave_sequence."""
        p = dict(pos)
        qty = fill["qty"]
        p["open_quantity"] += qty
        p["filled_adds"] += 1
        p["total_cost_basis_usd"] = round(p["total_cost_basis_usd"] + fill["price"] * qty * 100.0, 4)
        p["weighted_average_option_cost"] = round(p["total_cost_basis_usd"] / (p["open_quantity"] * 100.0), 6)
        p["last_add_option_fill_price"] = fill["price"]
        p["last_add_exec_bid"] = obs.get("option_bid")
        p["last_wave_anchor_underlying_price"] = obs["underlying_price"]   # anchor = fill underlying
        p["last_add_timestamp"] = fill["ts"]
        p["frozen_wave_atr"] = obs.get("atr_value")                       # re-freeze for next wave
        p["frozen_atr_quality"] = obs.get("atr_quality")
        exec_v = (obs.get("option_bid") or 0.0) * p["open_quantity"] * 100.0
        p["current_executable_value_usd"] = round(exec_v, 2)
        p["peak_executable_value_usd"] = round(exec_v, 2)                  # rebase peak to new size
        p["peak_pending_value"], p["peak_pending_count"] = None, 0
        p["cooldown_until"] = obs["now"] + self.cfg.add_cooldown_seconds
        p["wave_sequence"] += 1
        p["pending_order_key"] = None
        p["updated_at"] = fill["ts"]
        # cap check → MAX_POSITION_REACHED (still manages exit)
        if (p["filled_adds"] >= self.cfg.max_adds
                or p["open_quantity"] >= self.cfg.max_total_contracts):
            p["state"] = MAX_POSITION_REACHED
        else:
            p["state"] = COOLDOWN
        return p

    def reject_add(self, pos, reason, obs):
        """Add order rejected/timed out: DO NOT mutate qty/anchor/count. Return to
        MONITORING_WAVE (or SUSPENDED on a data reason)."""
        p = dict(pos)
        p["pending_order_key"] = None
        p["confirm_started_at"] = None
        p["state"] = SUSPENDED if reason == "data" else MONITORING_WAVE
        p["updated_at"] = obs.get("ts")
        return p

    def apply_exit_fill(self, pos, fill):
        """LIQUIDATING → PARTIALLY_LIQUIDATED / CLOSED using broker-confirmed qty."""
        p = dict(pos)
        sold = fill["qty"]
        p["open_quantity"] = max(0, p["open_quantity"] - sold)
        # realized P&L on the sold portion (exec/sim price vs weighted avg cost)
        realized = (fill["price"] - (p["weighted_average_option_cost"] or fill["price"])) * sold * 100.0
        p["realized_pnl_usd"] = round((p.get("realized_pnl_usd") or 0.0) + realized, 2)
        p["last_add_timestamp"] = fill["ts"]
        p["updated_at"] = fill["ts"]
        p["state"] = CLOSED if p["open_quantity"] == 0 else PARTIALLY_LIQUIDATED
        return p

    # -- decision builders --
    def _trigger_exit(self, pos, obs, xr):
        p = dict(pos)
        p["pending_order_key"] = None          # cancel any pending add first
        p["state"] = REVERSAL_TRIGGERED
        qty = p["open_quantity"]
        key = idempotency_key(p, -qty, obs["ts"])   # negative qty distinguishes exits
        order_intent = {
            "idempotency_key": key, "action": "EXIT", "qty": qty,
            "price_basis": "bid_minus_slippage", "signal_ts": obs["ts"],
            "wave_sequence": p["wave_sequence"], "contract_symbol": p["contract_symbol"],
            "position_id": p["position_id"], "strategy_version": p["strategy_version"],
            "exit_reason": xr,
        }
        peak = p.get("peak_executable_value_usd")
        cur = self._exec_value(p, obs)
        metrics = {
            "exit_reason": xr, "peak_executable_value_usd": peak,
            "current_executable_value_usd": (round(cur, 2) if cur is not None else None),
            "configured_giveback_pct": self.cfg.configured_giveback_pct,
            "effective_giveback_pct": round(self._effective_giveback(obs), 4),
            "spread_pct_at_exit": obs.get("spread_pct"),
            "qty_submitted": qty,
        }
        return self._decision(p, REVERSAL_TRIGGERED, ACT_SUBMIT_EXIT, [xr], metrics, order_intent)

    def _decision(self, pos, next_state, action, reasons, metrics, order_intent):
        pos = dict(pos)
        pos["state"] = next_state
        audit = {
            "strategy_version": pos["strategy_version"],
            "position_id": pos["position_id"],
            "ts": pos.get("updated_at"),
            "state": next_state,
            "action": action,
            "reason_codes": reasons,
            "direction": pos["direction"],
            "open_quantity": pos["open_quantity"],
            "wave_sequence": pos["wave_sequence"],
            "underlying_price": None,
            "metrics": metrics,
        }
        return {"next_state": next_state, "intended_action": action,
                "reason_codes": reasons, "updated_metrics": metrics,
                "audit_payload": audit, "order_intent": order_intent,
                "position": pos}
