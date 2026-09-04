# Signal-Threshold Calibration Report v0

**Status:** EXPLORATORY / NON-INFERENTIAL / IN-SAMPLE KNOB-SETTING ONLY.
This report does not freeze parameters, count toward N, test edge, or authorize a trade.

## Proposed Values

| Knob | Meaning | Proposed | Reasoned default | Basis |
|---|---|---:|---:|---|
| `A` | acceptance band | **0.20 pts** | 0.20 pts | fallback |
| `W` | hold window | **900 s** | 900 s | fallback |
| `G` | back-below grace | **120 s** | 120 s | fallback |
| `S` | sweep depth | **0.25 pts** | 0.25 pts | calibrated |
| `R` | reclaim window | **425 s** | 90 s | calibrated |
| `V` | proxy-VWAP slope window | **2 min** | 8 min | calibrated |

## Calibration Boundary

- Completed sessions only, through **2026-09-03**; current/partial sessions excluded.
- Tick source: `/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/tick_log.csv`; 10 complete RTH sessions, 120948 clean included quotes.
- Included-row SHA-256: `1fe359d7bf2975f96b4a2571a9c441e5381f99abbac96e3c196c498d565b18ab` (hashes only rows inside the frozen cutoff).
- Quotes were positive, two-sided, non-crossed and sampled to their last observed midpoint in each 5-second bucket. No interpolation or future quote was used.
- Calibration convention only: a long-arm down-sweep crosses the prior trailing 5-minute low; reclaim must occur within 10 minutes. These conventions characterize distributions and do not silently freeze a detector.
- Fixed design left untouched: N=150, p<=0.01, four chronological folds with >=3/4 sign consistency, dense 5/15/30/60-minute outcomes, <=5-second freshness, and no volume marker.

## Distribution Summaries

- `S` sweep depth in the completed tick sessions: n=1007, p25=0.02 pts, median=0.05 pts, p75=0.12 pts, p90=0.24 pts. Proposal uses the 75th percentile of `databento_spy_p75` rounded to $0.05 to separate deeper events from routine local-low jitter.
- `R` time to reclaim among sweeps at/above proposed `S`: n=98, p25=126.90 s, median=208.33 s, p75=386.20 s, p90=525.13 s. Proposal uses the 80th percentile rounded to 5 seconds.
- `A` post-reclaim dip below the reclaimed level among 29 events that survived the default 15-minute/120-second hold screen: n=29, p25=0.02 pts, median=0.06 pts, p75=0.10 pts, p90=0.25 pts. Proposal uses the 75th percentile rounded to $0.05; failed reclaims are not allowed to widen the acceptance band.
- `W` hold duration before cumulative back-below time exceeds the default grace: n=98, p25=179.52 s, median=444.72 s, p75=900.00 s, p90=900.00 s.
- `G` cumulative back-below time during the default hold assessment: n=98, p25=83.06 s, median=286.06 s, p75=630.26 s, p90=791.97 s.
- `V` quote-mid proxy slope sign persistence three minutes forward:
  -  2 min: 91.5% sign persistence across 3520 eligible minute observations
  -  4 min: 92.3% sign persistence across 3500 eligible minute observations
  -  6 min: 93.3% sign persistence across 3480 eligible minute observations
  -  8 min: 94.1% sign persistence across 3460 eligible minute observations
  - 10 min: 94.6% sign persistence across 3440 eligible minute observations
  - 12 min: 95.1% sign persistence across 3420 eligible minute observations
  - 15 min: 95.5% sign persistence across 3390 eligible minute observations

## Knob Decisions

- `A` — **FALLBACK**: Only 29 sweep/reclaim events survived the default hold/grace screen, below the 30-event floor; the 0.20-point reasoned default is retained instead of claiming precision from a thin tail.
- `W` — **FALLBACK**: Persistence/failed-hold separation needs at least 20 independent sessions; current data is too thin, so the 900-second reasoned default is retained.
- `G` — **FALLBACK**: Back-below grace is strongly coupled to the hold rule and needs at least 20 independent sessions; the 120-second default is retained.
- `S` — **CALIBRATED**: Calibrated from the 75th percentile of the long historical SPY distribution-shape archive; clean tick-level sessions remain the sub-minute cross-check.
- `R` — **CALIBRATED**: Calibrated from point-in-time reclaim durations for events meeting the proposed sweep-depth cutoff.
- `V` — **CALIBRATED**: Calibrated only when the shortest candidate window reaches 80% three-minute sign persistence with at least 200 observations.

## Databento Distribution-Shape Check

The optional archive contained `SPY` one-minute bars across 859 sessions. Reclaimed local-low excursion depths: n=31807, p25=0.06 pts, median=0.13 pts, p75=0.27 pts, p90=0.49 pts.
The archive basis is SPY, so the SPX-to-SPY approximately 10x point-price rescaling factor is **1.0 (no rescaling needed)**. Databento is used only for non-inferential distribution shape, not sub-minute `R`/`G` calibration and never toward study N.

## Freeze Status

**NOT FROZEN.** No preregistration file was edited. Operator approval and a separate freeze commit are required before prospective accrual can begin.
Historical and pre-lock observations remain in-sample and cannot count toward either study's N.
