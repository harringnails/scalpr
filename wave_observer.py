"""
wave_observer.py — live SHADOW observation lifecycle (wave-riding-shadow-observer-v0).

Turns real-time synchronized observations into sequential, once-only, durable
input to the pure Wave Riding engine. FULLY SIMULATED: never submits, cancels, or
modifies a broker order; never mutates Guard / live positions / Standard mode.

Guarantees:
  * start creates a position only from a valid, SYNCHRONIZED decision-time quote
    with a ready ATR, persists the immutable shared seed + hash BEFORE marking the
    simulation active, and shares that one seed across all three tracks;
  * a durable monotonically-increasing sequence per position — an observation is
    applied only if newer than the last canonical processed one; repeats /
    out-of-order pairs are auditable no-ops (DUPLICATE_OBSERVATION /
    OUT_OF_ORDER_OBSERVATION); the observation identity is persisted BEFORE any
    simulated fill or durable transition;
  * unsynchronized/stale/invalid pairs are blocked by the engine gate;
  * restart reloads active positions, never manufactures missed observations,
    requires a fresh synchronized quote, and cannot double-fill;
  * manual stop uses the latest valid synchronized bid or returns
    STOP_PENDING_VALID_QUOTE; abandon is a distinct non-performance terminal.
"""
import feature_engine as fe
import wave_riding as wr
import wave_store as wst
import wave_baselines as wb
from wave_order_adapter import ShadowOrderAdapter
from wave_quotes import OBSERVER_VERSION

_BAD_Q = ("crossed", "missing", "unusable", "stale")


def observation_id(pid, obs):
    return fe.canonical_hash([pid, obs.get("underlying_quote_timestamp"),
                              obs.get("option_quote_timestamp"),
                              obs.get("observation_sequence"), OBSERVER_VERSION])


def build_seed(pid, underlying, contract, direction, obs, fill, cfg, source=None):
    src = source or {}
    seed = {
        "shadow_position_id": pid, "observer_version": OBSERVER_VERSION,
        "strategy_version": wr.WAVE_RIDING_VERSION,
        "contract_symbol": contract, "underlying_symbol": underlying, "direction": direction,
        "activation_timestamp": obs["ts"],
        "underlying_quote": {"price": obs["underlying_price"],
                             "ts": obs["underlying_quote_timestamp"],
                             "source": obs["sources"]["underlying_quote"]},
        "option_quote": {"bid": obs["option_bid"], "ask": obs["option_ask"],
                         "mid": obs["option_mid"], "ts": obs["option_quote_timestamp"],
                         "source": obs["sources"]["option_quote"]},
        "field_sources": obs["sources"],
        "field_timestamps": {"underlying": obs["underlying_quote_timestamp"],
                             "option": obs["option_quote_timestamp"]},
        "quote_quality": obs["quote_quality"],
        "synchronization_status": obs["synchronization_status"],
        "timestamp_skew_seconds": obs["timestamp_skew_seconds"],
        "simulated_initial_fill": fill,
        "frozen_atr": {"atr_value": obs["atr_value"], "atr_quality": obs["atr_quality"],
                       "atr_audit": obs.get("atr_audit")},
        "config_versions": {"strategy": wr.WAVE_RIDING_VERSION,
                            "atr": cfg.intraday_atr_version,
                            "shadow_fill": cfg.shadow_fill_version,
                            "baseline": cfg.baseline_version, "observer": OBSERVER_VERSION},
        "source_link": {k: src.get(k) for k in
                        ("source_decision_id", "source_position_id",
                         "source_contract_symbol", "source_activation_timestamp",
                         "research_cohort_id", "research_scope_version")},
    }
    seed["seed_hash"] = fe.canonical_hash({k: v for k, v in seed.items() if k != "seed_hash"})
    return seed


class ShadowObserver:
    def __init__(self, config, adapter=None, store=wst):
        self.cfg = config.validate()
        self.engine = wr.WaveEngine(config)
        self.adapter = adapter or ShadowOrderAdapter()
        self.store = store

    # ── start ────────────────────────────────────────────────────────────────
    def start(self, shadow_position_id, underlying_symbol, contract_symbol,
              direction, initial_obs, source=None):
        blockers = []
        if not initial_obs.get("market_open"):
            blockers.append(wr.R_MARKET_CLOSED)
        if initial_obs.get("unsynchronized"):
            blockers.append(wr.R_UNSYNCHRONIZED)
        if initial_obs.get("quote_quality") in _BAD_Q:
            blockers.append("BAD_OPTION_QUOTE")
        if not initial_obs.get("atr_value") or initial_obs.get("atr_quality") == "WARMUP_BLOCKED":
            blockers.append(wr.R_ATR_WARMUP)
        if blockers:
            return {"created": False, "blocking_reason": blockers}

        fill = self.adapter.fill_add(initial_obs, self.cfg.initial_contracts, self.cfg)
        if not fill.get("filled"):
            return {"created": False, "blocking_reason": ["INITIAL_FILL_UNAVAILABLE",
                                                          fill.get("reason")]}

        pos = wr.new_position(shadow_position_id, direction, underlying_symbol,
                              contract_symbol, self.cfg, source=source)
        pos["state"] = wr.INITIAL_ORDER_PENDING
        pos = self.engine.seed_initial_fill(pos, fill, initial_obs)
        pos["observer_version"] = OBSERVER_VERSION
        pos["observer_paused"] = False
        pos["last_processed_sequence"] = initial_obs.get("observation_sequence", 0)
        oid = observation_id(shadow_position_id, initial_obs)
        pos["last_processed_observation_id"] = oid
        pos["seed_hash"] = None

        seed = build_seed(shadow_position_id, underlying_symbol, contract_symbol,
                          direction, initial_obs, fill, self.cfg, source)
        pos["seed_hash"] = seed["seed_hash"]

        # persist seed BEFORE marking active; observation begins only after this
        if self.store.save_seed(seed) is None and seed.get("seed_hash") is None:
            return {"created": False, "blocking_reason": ["SEED_PERSIST_FAILED"]}
        self.store.append_obs_stream(shadow_position_id, {**initial_obs, "observation_id": oid})
        self.store.save_position(pos)
        self.store.register_active(shadow_position_id)
        return {"created": True, "position_id": shadow_position_id,
                "seed_hash": seed["seed_hash"], "state": pos["state"]}

    # ── observe (dedup + identity-before-fill + once-only) ────────────────────
    def observe(self, pos, obs):
        pid = pos["position_id"]
        seq = obs.get("observation_sequence")
        oid = observation_id(pid, obs)
        last = pos.get("last_processed_sequence", -1)

        if seq is not None and seq <= last:
            seen = self.store.observation_id_seen(pid, oid)
            code = "DUPLICATE_OBSERVATION" if seen else "OUT_OF_ORDER_OBSERVATION"
            return {"applied": False, "code": code, "sequence": seq, "position": pos}
        if pos.get("observer_paused"):
            return {"applied": False, "code": "PAUSED", "position": pos}

        # persist observation identity BEFORE any simulated fill / durable transition
        self.store.append_obs_stream(pid, {**obs, "observation_id": oid})

        dec = self.engine.observe(pos, obs)
        pos = dec["position"]
        act = dec["intended_action"]
        if act == wr.ACT_SUBMIT_ADD:
            oi = dec["order_intent"]
            if self.store.order_seen(oi["idempotency_key"]):
                pos = self.engine.reject_add(pos, "duplicate", obs)
            else:
                fill = self.adapter.fill_add(obs, oi["qty"], self.cfg)
                if fill.get("filled"):
                    self.store.append_order({**oi, **fill})
                    pos = self.engine.apply_add_fill(pos, fill, obs)
                else:
                    pos = self.engine.reject_add(pos, fill.get("reason"), obs)
        elif act == wr.ACT_SUBMIT_EXIT:
            oi = dec["order_intent"]
            fill = self.adapter.fill_exit(obs, oi["qty"], self.cfg)
            if fill.get("filled"):
                self.store.append_exit({**oi, **fill})
                pos = self.engine.apply_exit_fill(pos, fill)
                if pos["state"] == wr.CLOSED:
                    self.store.unregister_active(pid)
            else:
                pos = dict(pos)
                pos["state"] = wr.MONITORING_WAVE          # retry exit on next valid tick

        pos["last_processed_sequence"] = seq
        pos["last_processed_observation_id"] = oid
        pos["observer_version"] = OBSERVER_VERSION
        self.store.append_observation(dec["audit_payload"])
        self.store.save_position(pos)
        return {"applied": True, "decision": dec, "position": pos}

    # ── pause / resume ────────────────────────────────────────────────────────
    def pause(self, pos):
        pos = dict(pos); pos["observer_paused"] = True
        self.store.save_position(pos)
        return pos

    def resume(self, pos):
        pos = dict(pos)
        pos["observer_paused"] = False
        pos["awaiting_fresh_confirmation"] = True    # require a fresh synchronized quote
        pos["confirm_started_at"] = None
        self.store.save_position(pos)
        return pos

    # ── stop (MANUAL_SHADOW_STOP) / abandon ──────────────────────────────────
    def stop(self, pos, obs):
        ok = (not obs.get("unsynchronized")
              and obs.get("quote_quality") not in _BAD_Q
              and (obs.get("option_bid") or 0) > 0
              and obs.get("market_open"))
        if not ok:
            return {"stopped": False, "status": "STOP_PENDING_VALID_QUOTE", "position": pos}
        fill = self.adapter.fill_exit(obs, pos["open_quantity"], self.cfg)
        if not fill.get("filled"):
            return {"stopped": False, "status": "STOP_PENDING_VALID_QUOTE", "position": pos}
        self.store.append_exit({"exit_reason": "MANUAL_SHADOW_STOP", "position_id": pos["position_id"], **fill})
        pos = self.engine.apply_exit_fill(pos, fill)
        pos["exit_reason"] = "MANUAL_SHADOW_STOP"
        self.store.unregister_active(pos["position_id"])
        self.store.save_position(pos)
        return {"stopped": True, "status": "MANUAL_SHADOW_STOP", "position": pos}

    def abandon(self, pos):
        """Administrative close WITHOUT a terminal fill. Distinct from a completed
        simulated exit; excluded from normal performance results."""
        pos = dict(pos)
        pos["terminal_status"] = "ABANDONED_WITHOUT_TERMINAL_FILL"
        pos["abandoned"] = True
        self.store.unregister_active(pos["position_id"])
        self.store.save_position(pos)
        return pos

    # ── restart recovery ──────────────────────────────────────────────────────
    def reload(self, position_id):
        """Reload one active sim position after restart. Never manufactures the
        missed observations; requires a fresh synchronized quote before resuming;
        cannot double-fill (last_processed_sequence gates re-delivery)."""
        pos = self.store.reconstruct(position_id)
        if pos is None:
            return None
        pos["awaiting_fresh_confirmation"] = True
        pos["confirm_started_at"] = None
        # last_processed_sequence persists in the record → dedupe holds across restart
        self.store.save_position(pos)
        return pos

    def reload_all(self):
        return [self.reload(pid) for pid in self.store.list_active()]

    # ── finalize: three tracks from the SAME stored seed/observation stream ───
    def finalize(self, position_id, ladder=None):
        pos = self.store.load_position(position_id)
        seed = self.store.load_seed(position_id)
        stream = self.store.read_obs_stream(position_id)
        if pos and pos.get("abandoned"):
            return {"position_id": position_id, "abandoned": True,
                    "terminal_status": "ABANDONED_WITHOUT_TERMINAL_FILL",
                    "note": "excluded from normal performance results"}
        if not stream:
            return {"position_id": position_id, "error": "no_observations"}
        tracks = wb.build_tracks(self.cfg, stream, ladder=ladder)
        tracks["seed_hash"] = (seed or {}).get("seed_hash")
        tracks["shadow_position_id"] = position_id
        return tracks
