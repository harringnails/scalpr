# Options-Flow Evidence Ranking — Design Return (`flow-evidence-v0`)

**Design only — no code yet. Stops for approval.**

A read-only, per-ticker ranking of *options-flow evidence* with 🟢/🟠 stars, built
ONLY from data we actually have. It answers "is there aligned directional flow
here, and how good is the data?" — **evidence, not a probability, not a buy
signal, not a calibrated score.** GREEN DATA ≠ GREEN TRADE.

Non-negotiables (unchanged): does NOT touch the live Guard, orders, Standard
mode, or the frozen Wave/Incubation cohorts. Nothing fabricated — unavailable
inputs (L2 order flow, internals, news) are shown as unavailable.

---

## 1. What it is / is not

- **Is:** a ranked list of tickers you've workup'd, each with a directional
  read (bullish / bearish / mixed), a data-quality **🟢 green / 🟠 amber /
  insufficient** tier, and the component evidence behind it.
- **Is not:** a probability, an edge claim, a trade recommendation, or an input
  to any live/automated decision. It's decision-support you read.

## 2. Data sources (verified against the code)

**Available now — from the UW workup you already pull (per-contract snapshot):**
`ask_pct` (aggressive-buy %), `net_aggressor` (ask−bid volume), `sweep_vol`,
`oi_chg` (OI build), `oi_since_prev` (cross-session OI), `multileg_pct` (to
discount spread noise), `volume`, `iv_rank`, `max_pain`, per-contract greeks.

**Available but currently discarded — recover via an additive un-discard
(flagged for approval, §6):** UW `gex-levels` (dealer gamma / gamma walls) and
`darkpool` (off-exchange levels). workup already fetches both; they're just not
persisted.

**Explicitly UNAVAILABLE — shown as such, never fabricated:** equity Level II /
order book / absorption / iceberg (needs a paid depth feed), market internals
(TICK/ADD/TRIN/A-D, VIX/DXY), news/NLP, true volume profile (needs full-volume
trades). Vanna/charm (UW doesn't provide; would need modeling).

**Freshness:** the flow is a per-workup-run snapshot, not a live stream — the
ranking is only as fresh as each ticker's last workup pull. Staleness downgrades
the tier (§4).

## 3. Component evidence (per ticker, each with its own data-status)

Aggregated across the in-band contracts (delta 0.15–0.70) and split call vs put:

| Signal | Reads | Direction |
|---|---|---|
| Aggressive buying | `ask_pct` weighted by volume, calls vs puts | high call ask% = bullish; high put ask% = bearish |
| Net aggressor | Σ `net_aggressor` calls vs puts | positive on calls = bullish; on puts = bearish |
| Sweeps | Σ `sweep_vol` calls vs puts (urgency) | side with more sweeps |
| OI build | Σ `oi_chg` / `oi_since_prev` calls vs puts (new positioning) | side building OI |
| Put/call skew | OI or premium ratio | tilt |
| IV rank | `iv_rank` (context only, not directional) | — |
| Dealer GEX / gamma walls | `gex-levels` (once un-discarded) | pin vs accelerate context |
| Dark-pool levels | `darkpool` (once un-discarded) | large off-exchange interest |

Each signal reports `available / degraded / unavailable`. Nothing is inferred
from a missing signal.

## 4. Green / amber / star logic (data quality × agreement)

The tier fuses **data quality** and **directional agreement** — never a
probability:

- **🟢 GREEN** — the workup is fresh, ≥ N in-band contracts with real quotes, and
  the *available* directional signals **agree** (e.g., call ask% high + call OI
  building + net aggressor positive all point bullish). Strong, aligned,
  well-sourced flow evidence in one direction.
- **🟠 AMBER** — data present but signals **conflict/mixed**, OR the workup is
  moderately stale, OR only a partial signal set is available.
- **INSUFFICIENT** (no star) — too few live signals / stale-or-missing workup.
  Shown honestly rather than guessed.

Rank order: GREEN (by agreement strength) → AMBER → INSUFFICIENT. Direction
(BULLISH/BEARISH/MIXED) shown beside each. A prominent header states the star is
**data-quality + agreement, not probability of profit.**

## 5. Modules & boundaries

- `flow_evidence.py` (new, pure) — `score_ticker(workup_payload) -> evidence` and
  `rank(payloads) -> ranked list`. Deterministic, no I/O, no probability. Version
  `flow-evidence-v0`; carries `is_edge_claim: false`.
- Reuse: `feature_engine.in_band_contracts` (delta band), `assess_quote_quality`
  (freshness/quality), `canonical_hash`; the workup `runs/` cache as input; the
  Wave picker's 🟢★ star pattern for the UI.
- `wave_server` (or a small `flow_server`) helper: read `runs/` for all cached
  tickers, call `flow_evidence.rank`. Read-only.
- Endpoint: `GET /api/flow/ranking` (read-only). Dashboard: a "FLOW EVIDENCE"
  ranked panel (🟢/🟠 + direction + component chips), advisory only.
- Tests: `test_flow_evidence.py` — agreement→green, conflict→amber, stale/sparse→
  insufficient, unavailable inputs never fabricated, no probability field, no
  scalp_server import / no broker path.

## 6. The one change that needs your OK — un-discard GEX + dark pool

To include dealer gamma and dark-pool levels, `workup_api.do_workup` must persist
the `gex-levels` and `darkpool` blocks it already fetches (currently dropped).
This is **additive** (new keys in the run payload); it does **not** rename or
change any existing field, and `feature_engine` reads only specific keys so it
is **unaffected** — meaning **no frozen cohort (Wave/Incubation/intel) input
changes.** Still, it touches the workup payload, so I'm flagging it explicitly.
If you'd rather not touch workup at all right now, v0 ships **without** GEX/dark
pool (aggressor/sweeps/OI/skew/IV only), and we add them later.

## 7. Honest limitations (stated on the panel)

- Snapshot, not live-streaming flow — as fresh as the last workup pull.
- Your #1 data (equity L2 order flow) is absent — this ranks **options** flow
  only; it cannot see equity absorption/iceberg/depth.
- No calibration: it does not claim any win probability. It surfaces who's active
  in the options and whether the signals agree — the *evidence* an eventual
  calibrated model would consume, once the missing feeds and forward labels exist.

## 8. Return for approval

1. Approve the **evidence-only, green/amber, no-probability** framing.
2. Approve (or decline) the **GEX/dark-pool un-discard** in §6.
3. Confirm the **green rule** (fresh + ≥N contracts + available signals agree)
   and the **rank order** (green→amber→insufficient).

On approval I'll build `flow_evidence.py` + tests + the read-only endpoint and
panel, and — if approved — the additive GEX/dark-pool un-discard. It stays out of
live trades and the frozen cohorts.
