# Bar / Quote-Quality Gate — Validation Checklist

> **SUPERSEDED by `FEED_QUALIFICATION_GATE_v1_SPEC.md`.** This manual checklist wrongly assumed a live feed-quality logger existed (the real `/api/feed/quality` is a historical REST comparison and cannot bank a session), and it contained two errors: it referred to a weekend day as a session, and it called disk "fine" when it had dropped near the critical floor. Keep for context only; use the built Feed Qualification Gate instead.

**Why this gate exists:** the cohort's entire direction axis (ATR extension, momentum, confirmation, level proximity) is computed from **completed 5-minute RTH bars**, and every outcome rests on **OPRA option quotes**. If bars or quotes are unhealthy, the study is built on sand. This is the last real blocker before a cohort lock (disk is now fine).

**What "passing" means (honest scope):** the formal gate needs **≥5 clean live sessions** plus a separate premarket pass and quote-quality evidence. **Tomorrow is ONE of those sessions** — you can't fully clear the gate in a day, but you can (a) confirm the checks pass for one session, and (b) catch the specific failures the audit flagged so Codex can fix them. Roles: **you observe and confirm; Codex runs the `feed_quality` validator and fixes any failing check.**

---

## Pre-open (before 09:30 ET)

- [ ] Server + collector up, `feed_quality` module logging (ask Codex if unsure which panel/endpoint).
- [ ] **Both feeds streaming** — SIP and IEX (the restart runs `--sip`). Cross-feed validation needs both.
- [ ] **Premarket bar pass** — confirm premarket bars are building correctly and the premarket check reports **PASS** (this was one of the audit's failing checks). The direction level-set uses `premarket_low`/`premarket_high`, so this matters.
- [ ] Underlying (SPY) quotes fresh in premarket; note any staleness.

## Intraday (during RTH — spot-check a few times, don't babysit)

- [ ] **5-minute RTH bar completeness** — every completed bucket present, no missing or duplicate bars, no zero/`NaN` bars. (This is what ATR/extension/momentum read.)
- [ ] **Option quote quality** — OPRA bid/ask coming back **FRESH** (≤5s), two-sided, spreads sane; watch how often quotes land as `stale`/`crossed`/`locked`/`missing`.
- [ ] **Cross-feed agreement** — SIP vs IEX within tolerance; note any divergence flags from the `feed_quality` validator.
- [ ] **The practical operator signal → direction-axis `UNAVAILABLE` rate.** In the Entry Intelligence decisions, how often does the **direction axis** come back `UNAVAILABLE`/`STALE` *because of bar/quote problems* vs producing a real evaluation? Mostly-real = bars/quotes healthy. Mostly-`UNAVAILABLE` = the gate is not ready, regardless of what a summary says.
- [ ] Note any **429 / rate-limit** bursts that degrade quote freshness.

## Post-close (the verdict)

- [ ] Have Codex run the **`feed_quality` session report** and read three results explicitly:
  - [ ] **RTH bar-quality check: PASS/FAIL** (and why if fail)
  - [ ] **Quote-quality check: PASS/FAIL**
  - [ ] **Premarket pass: PASS/FAIL**
- [ ] Session coverage numbers: fraction of decision minutes with **FRESH bars + FRESH quotes** vs degraded. (You want this high and stable.)
- [ ] Log today as **one clean parallel session** toward the ≥5 needed — or, if anything failed, log it as a *diagnostic* session and capture the specifics.
- [ ] **Session verdict:** all three checks PASS + coverage healthy → one session banked. Any FAIL → not banked; hand the specifics to Codex.

## If a check FAILS — hand to Codex (don't lock)

Capture and pass along: which check failed, the timestamps/bars/quotes involved, and the `feed_quality` validator output. Common culprits to name: missing/late RTH bars (bar_builder watermark/half-open), premarket bar assembly, SIP/IEX divergence, or quote staleness from rate limits. **A failure tomorrow is a win** — it's exactly what the dry run is for, caught before anything's frozen.

## Gate is GREEN (ready for Stage 3 lock) only when, across sessions:
- [ ] ≥5 clean live sessions with RTH bar-quality **PASS**
- [ ] Quote-quality **PASS** with captured evidence
- [ ] Separate premarket pass **PASS**
- [ ] Then — and only then — the bar/quote-quality blocker is cleared and the pre-open cohort lock becomes eligible (still your separate, deliberate approval).

---

**Reminder:** none of this blocks tomorrow's capture or your own manual trading — it's the validation that gates the *lock*, not the dry run. Keep treating any captured numbers as plumbing tests, not signal.
