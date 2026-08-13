# Ratchet Exit — Options-Price Noise Sensitivity Assessment

*Evidence-based review of the current `WHOLE_POSITION_RATCHET` exit. Documents
behavior only — **no thresholds are changed** by this assessment. Findings are
demonstrated by `test_ratchet_exit.py` (the false-peak replay).*

## What the exit actually does (recap)

The ladder rungs are **profit-activation + allowed-giveback tiers**, not staged
scale-out levels. `Guard.tolerance()` returns the giveback tolerance for the
current peak; `Guard.on_price()` tracks the peak and fires a single exit when the
price gives back more than the tolerance (after `confirm_ticks`). `Platform.sell()`
then sells `qty=guard.qty` — 100% of the remaining position — in one order.

## The six questions, answered from the code

### 1. Which price establishes and evaluates the peak?
- **Stocks:** the **bid** (`px = bid_price or ask_price`) — the price you'd
  actually sell into. Good.
- **Options:** the **bid/ask MID** (`mid = (bid+ask)/2`, fallback bid or ask) —
  `scalp_server._poll_loop`. Peak, profit, and giveback are all measured against
  the mid.

Both `peak` and the current profit are computed from this price in
`Guard.on_price()`: `profit = (price - entry) / entry * 100`.

### 2. Is the exit based on an executable bid or on last trade?
Neither the peak nor the exit uses the **last trade**. Stocks use bid (executable).
**Options use the mid, which is NOT executable** — a market sell fills near the
bid. So for options the recorded peak/profit systematically **overstates** what
you could actually sell for by roughly half the spread. (This is the same issue
recorded as Known-limitation #3 in `SCALPR_OVERVIEW.md`.)

### 3. Can an anomalous tick establish a false peak?
**Yes — and this is the sharpest vulnerability.** The peak updates on the *first*
higher read with **no confirmation**:

```python
if profit > self.peak:
    self.peak, self.peak_time = profit, time.time()
```

and the peak **only ratchets up, never down**. So a single anomalous quote — on
options, a momentary wide spread that inflates the mid — permanently sets a false
high peak, which then **tightens the giveback tolerance** (higher peak → later
rung → smaller tol). Replay in `test_ratchet_exit.py`:
a true peak of +10% (tol 15%) tolerates a pullback to +5% with no exit; but one
anomalous +30% mid tick locks in a false peak, tightens tol to 2.5%, and the same
+5% read is now 25% below the false peak → **premature exit**. The exit banks a
real ~+5% instead of continuing to hold, purely because of one bad tick.

### 4. Can spread widening trigger an unintended exit?
**Yes, on two paths.** (a) A widening where the ask spikes raises the mid → false
peak (as above). (b) A widening where the bid drops lowers the mid → the mid can
fall below `peak − tol` even if fair value didn't move → a **false-breach exit**.
Because the exit is measured on the mid, spread dynamics alone can move it across
the threshold without any real price move.

### 5. Must a breach persist / be confirmed?
**Partially — asymmetrically.** The *exit* side is protected: a breach must
persist for `confirm_ticks` (default 2) consecutive reads before firing, and a
`grace_seconds` window (default 60s) blocks any exit right after entry. But the
*peak* side has **no** confirmation — one tick sets it. So false **exits** from a
single bad low tick are largely filtered, while false **peaks** from a single bad
high tick are not. The vulnerability lives entirely on the unprotected peak side.

### 6. Should giveback thresholds vary by liquidity / volatility / delta / DTE?
Currently they are **fully static** — user-set rungs, identical regardless of the
contract's liquidity, implied/realized volatility, delta, or time to expiration.
A tolerance that's sensible for a liquid 30-DTE contract may be far too tight for
a wide-spread 0DTE contract (where normal mid noise is a large % of premium),
making premature exits more likely exactly where they hurt most.

## Summary of findings (no changes made)

| Concern | Current behavior | Risk |
|---|---|---|
| Peak/eval price (options) | bid/ask **mid** | overstates executable P&L; spread-sensitive |
| Executable basis | mid, not bid; exit fills at market ≈ bid | recorded peak > sellable value |
| Anomalous tick → false peak | peak set on 1 unconfirmed tick, ratchets up only | **premature exits**; persistent |
| Spread widening | moves the mid across thresholds | false peaks and false breaches |
| Breach confirmation | exit confirmed (`confirm_ticks`+grace); **peak not** | asymmetric protection |
| Threshold adaptivity | fully static | too tight for wide-spread / 0DTE |

## Candidate mitigations (documented, NOT implemented, NOT decided)

Listed for a future decision only — none is applied here, and thresholds are
unchanged:

1. **Guard options off the bid** (like stocks), keeping mid as display only —
   removes the executable-vs-mid gap and much of the spread sensitivity.
2. **Confirm the peak** the way exits are confirmed (require N ticks, or a small
   dwell, before ratcheting the peak up) — directly kills the single-tick false
   peak.
3. **Outlier guard on peak establishment** — reject a new peak whose jump vs the
   prior read exceeds a sanity bound, or whose spread exceeds a width limit.
4. **Spread-quality gate** — ignore ticks (for both peak and exit) when the
   spread is abnormally wide, since the mid is then unreliable.
5. **Liquidity/vol/DTE-aware tolerance** — scale the giveback with spread width,
   realized vol, or time to expiration instead of a fixed %.

Recommended sequencing if pursued: (1) and (2) are the highest-leverage and
lowest-risk (they address the demonstrated false-peak failure directly); (5) is a
larger design change best deferred until there is forward evidence. **Any change
here alters live exit behavior and should be versioned and tested against replays
before adoption.**
