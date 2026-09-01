# Scalpr Read-only Status Monitor

`scalpr_status.py` renders existing evidence into one self-contained
`scalpr_status.html`. It does not import the server, collector, broker, Guard,
order, or admission code. Source JSON, JSONL, and log files are opened in read
mode only. The renderer atomically replaces only the requested HTML output.

## Authority labels

- Reversal: `INFERENTIAL · PROSPECTIVE ACCRUAL`. This describes the frozen A2
  evidence track but does not claim an edge before the gate is reached.
- Pin, iron condor, and Databento: `EXPLORATORY · NON-INFERENTIAL`.
- Collector and liveness: `OPERATIONS · NON-SIGNAL`.

No card can authorize a trade. Missing or malformed sources render as `—` or
`not yet`; values are never inferred or fabricated.

## Render

From a merged operative checkout:

```zsh
cd "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7"
"/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/.venv/bin/python" \
  scalpr_status.py render \
  --root "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7" \
  --pin-root "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-flashalpha-pin-scanner" \
  --databento-root "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7-a2-relabel/a2_exploratory_databento" \
  --output "/Users/natalieharrington/Documents/Scalpr Trading/Scalpr7/scalpr_status.html"
```

The same command is safe to append as the final successful step of the nightly
job. It has no network calls, exits successfully when study files are absent,
and never rewrites a ledger. Explicit source roots are recommended for the
nightly job; interactive use also discovers the established sibling worktrees.

The generated HTML is intentionally gitignored. It contains all CSS inline and
has no JavaScript, external assets, or runtime server dependency.
