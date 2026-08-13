#!/usr/bin/env python3
"""
workup_api.py — Unusual Whales pre-trade workup panel for Scalpr Trade.

Install: drop next to scalp_server.py along with workup.py, then add two
lines to scalp_server.py after `app = FastAPI(...)`:

    from workup_api import router as workup_router
    app.include_router(workup_router)

Browse to http://localhost:8420/workup

v2 changes:
  - Budget is a first-class input. Every contract shows cost (mid x 100) and
    max contracts affordable. A tight spread on an unaffordable contract is
    not a finding.
  - REACH gate: how many liquid, in-band contracts actually fit the budget.
  - atm-chains now queries the nearest expiry overall, not the nearest inside
    the DTE window — a thin far expiry produced a 3-contract ATM read.
  - Earnings date is shown as unverified, because the API exposes no
    confirmed/estimated flag.

This panel reports evidence. It does not score, rank, or recommend trades.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import workup as W

router = APIRouter()

RUNS = Path(__file__).parent / "runs"
RUNS.mkdir(exist_ok=True)

# Retention for the per-ticker per-day run cache. 30 days keeps enough history
# for cross-day OI comparison without unbounded growth. The natural ceiling is
# the API's 90-day lookback — a run older than that can't be meaningfully
# compared against fresh data, so there's no reason to keep it. One-line
# configurable; raise toward 90 only if you want longer OI/seasonality history.
RUN_RETENTION_DAYS = 30

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def run_path(ticker: str, day: str | None = None) -> Path:
    return RUNS / f"{ticker.upper()}_{day or date.today().isoformat()}.json"


def prune_runs(retention_days: int = RUN_RETENTION_DAYS) -> int:
    """Delete run-cache files older than the retention window. Dates are parsed
    from the filename (TICKER_YYYY-MM-DD.json), not mtime, so a touched file
    isn't mistaken for fresh. Malformed names are skipped, never deleted."""
    removed = 0
    for p in RUNS.glob("*.json"):
        try:
            day = datetime.strptime(p.stem.rsplit("_", 1)[-1], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue   # unexpected name — leave it alone
        if (date.today() - day).days > retention_days:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# prune once at import so a long-running server doesn't accumulate forever
prune_runs()


def _last(v):
    if isinstance(v, list) and v:
        return v[-1]
    return v if isinstance(v, dict) else None


def _cached_spot(ticker: str) -> tuple[float | None, str | None]:
    """Return the most recent cached spot for a ticker, if any.

    This is a fallback for fresh spot lookups. It lets the workup panel still
    build a chain when the live spot request is temporarily unavailable, while
    making the fallback explicit in the returned payload.
    """
    t = ticker.upper().strip()
    candidates = sorted(
        (p for p in RUNS.glob(f"{t}_*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in candidates:
        try:
            payload = json.loads(p.read_text())
        except Exception:
            continue
        spot = W.f(payload.get("spot"))
        if spot:
            return spot, p.name
    return None, None


def do_workup(ticker: str, dte_lo: int, dte_hi: int, budget: float) -> dict:
    key = os.environ.get("UW_API_KEY")
    if not key:
        raise RuntimeError("UW_API_KEY is not set in the server's environment")

    prune_runs()   # also prune on each run, so a rarely-restarted server stays bounded
    uw = W.UW(key)
    t = ticker.upper()
    today = date.today()

    state, st = uw.get(f"stock/{t}/stock-state")
    spot = None
    spot_source = "live"
    if isinstance(state, dict):
        spot = W.f(state.get("close") or state.get("last")
                   or state.get("price"))
    elif isinstance(state, list) and state:
        spot = W.f(state[0].get("close"))
    if not spot:
        cached_spot, cached_from = _cached_spot(t)
        if cached_spot:
            spot = cached_spot
            spot_source = f"cached:{cached_from}"
        else:
            raise RuntimeError(
                f"could not resolve spot for {t} ({st}); no cached workup spot available"
            )

    ctx = {}
    for name, path in [
        ("iv-rank", f"stock/{t}/iv-rank"),
        ("realized", f"stock/{t}/volatility/realized"),
        ("term-structure", f"stock/{t}/volatility/term-structure"),
        ("variance-risk-premium",
         f"stock/{t}/volatility/variance-risk-premium"),
        ("max-pain", f"stock/{t}/max-pain"),
        ("gex-levels", f"stock/{t}/gex-levels"),
        ("darkpool", f"darkpool/{t}"),
        ("expiry-breakdown", f"stock/{t}/expiry-breakdown"),
        ("short-interest", f"shorts/{t}/interest-float/v2"),
    ]:
        data, _ = uw.get(path)
        ctx[name] = data

    all_exp = sorted(
        x for x in (W.d(e.get("expires"))
                    for e in (ctx.get("expiry-breakdown") or [])
                    if isinstance(e, dict))
        if x and x >= today
    )
    expiries = [e.isoformat() for e in all_exp
                if dte_lo <= (e - today).days <= dte_hi]
    if not expiries:
        raise RuntimeError(f"no expiries for {t} in DTE {dte_lo}-{dte_hi}")

    # nearest expiry overall — a thin in-window expiry gives a useless read
    atm_rows, _ = W.fetch_atm(uw, t, all_exp[0].isoformat())
    atm = W.atm_summary(atm_rows or [])

    rows = W.fetch_all_contracts(uw, t, expiries, verbose=False)
    contracts = W.build_contracts(rows, spot, today, dte_lo, dte_hi, 0.043)

    for c in contracts:
        c["cost"] = round(c["mid"] * 100, 2)
        c["max_contracts"] = int(budget // c["cost"]) if c["cost"] else 0
        c["friction"] = round(c["spread_abs"] * 100, 2)

    payload = {
        "ticker": t, "spot": spot, "budget": budget,
        "spot_source": spot_source,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "dte": [dte_lo, dte_hi],
        "atm": atm,
        "atm_expiry": all_exp[0].isoformat(),
        "iv_rank": _last(ctx.get("iv-rank")),
        "max_pain": W.nearest_max_pain(ctx.get("max-pain"), today),
        "expiry_breakdown": ctx.get("expiry-breakdown"),
        "contracts": contracts,
        "gates": gates(atm, ctx, contracts, today, budget),
        # ADDITIVE (flow-evidence-v0): persist dealer-gamma + dark-pool blocks UW
        # already returns (previously discarded). New keys only — no existing
        # field renamed/changed; feature_engine reads specific keys, so frozen
        # cohorts are unaffected.
        "gex_levels": ctx.get("gex-levels"),
        "darkpool": ctx.get("darkpool"),
    }
    run_path(t).write_text(json.dumps(payload, indent=2, default=str))
    return payload


def gates(atm, ctx, contracts, today, budget):
    liq = [c for c in contracts if W.tradeable(c)]
    band = [c for c in liq if 0.15 <= abs(c.get("delta") or 0) <= 0.70]
    reach = [c for c in band if c.get("max_contracts", 0) >= 1]
    g = []

    ed = W.d(atm.get("next_earnings_date"))
    if not ed:
        g.append({"key": "EVENT", "state": "unknown", "value": "no date",
                  "note": "earnings unresolved — check the IR page"})
    else:
        days = (ed - today).days
        inside = any(days <= c["dte"] for c in contracts) if contracts else False
        g.append({
            "key": "EVENT", "state": "warn" if inside else "clear",
            "value": f"{ed.isoformat()} · {days}d",
            "note": ("lands inside the window — unverified, no "
                     "confirmed/estimated flag in the API" if inside
                     else "falls after the window — unverified"),
        })

    rank = W.f((_last(ctx.get("iv-rank")) or {}).get("iv_rank_1y"))
    if rank is None:
        g.append({"key": "VOL", "state": "unknown", "value": "n/a",
                  "note": "IV rank unavailable"})
    else:
        g.append({
            "key": "VOL",
            "state": "warn" if rank >= 70 else (
                "clear" if rank <= 35 else "mid"),
            "value": f"IVR {rank:.0f}",
            "note": ("rich against its own year" if rank >= 70 else
                     "cheap against its own year" if rank <= 35 else
                     "mid-range against its own year"),
        })

    if not liq:
        g.append({"key": "EXIT", "state": "warn", "value": "0",
                  "note": "nothing clears the spread, OI and volume floors"})
    else:
        med = sorted(c["spread_pct"] for c in liq)[len(liq) // 2]
        g.append({
            "key": "EXIT",
            "state": "clear" if med <= 6 else ("mid" if med <= 12 else "warn"),
            "value": f"{len(liq)} · {med:.1f}%",
            "note": "liquid contracts, median spread as % of mid",
        })

    g.append({
        "key": "BAND",
        "state": "clear" if len(band) >= 8 else (
            "mid" if len(band) >= 3 else "warn"),
        "value": str(len(band)),
        "note": "delta 0.15–0.70 with real quotes",
    })

    g.append({
        "key": "REACH",
        "state": "clear" if len(reach) >= 5 else (
            "mid" if len(reach) >= 1 else "warn"),
        "value": f"{len(reach)} / {len(band)}",
        "note": (f"fit inside ${budget:,.0f}" if reach
                 else f"nothing in band fits ${budget:,.0f}"),
    })
    return g


def _worker(t, lo, hi, budget):
    try:
        do_workup(t, lo, hi, budget)
        with _LOCK:
            _JOBS[t] = {"state": "done", "error": None}
    except Exception as e:
        traceback.print_exc()
        with _LOCK:
            _JOBS[t] = {"state": "error", "error": str(e)[:300]}


@router.post("/api/workup/{ticker}")
def start(ticker: str, dte_lo: int = 7, dte_hi: int = 60,
          budget: float = 2000):
    t = ticker.upper().strip()
    if not t.isalpha() or len(t) > 6:
        raise HTTPException(400, "bad symbol")
    with _LOCK:
        if _JOBS.get(t, {}).get("state") == "running":
            return {"state": "running"}
        _JOBS[t] = {"state": "running", "error": None}
    threading.Thread(target=_worker, args=(t, dte_lo, dte_hi, budget),
                     daemon=True).start()
    return {"state": "running"}


@router.get("/api/workup/{ticker}")
def fetch(ticker: str):
    t = ticker.upper().strip()
    job = _JOBS.get(t, {})
    p = run_path(t)
    payload = json.loads(p.read_text()) if p.exists() else None
    if payload and "spot_source" not in payload:
        payload["spot_source"] = "legacy"

    prior = [x for x in sorted(RUNS.glob(f"{t}_*.json")) if x != p]
    if payload and prior:
        prev = json.loads(prior[-1].read_text())
        payload["prev_as_of"] = prev.get("as_of")
        pmap = {c["symbol"]: c for c in prev.get("contracts", [])}
        for c in payload.get("contracts", []):
            old = pmap.get(c["symbol"])
            c["oi_since_prev"] = c["oi"] - old["oi"] if old else None

    return JSONResponse({"state": job.get("state", "idle"),
                         "error": job.get("error"), "data": payload})


# ── Scalpr Intelligence (scalpr-intel-v0) — research layer, NON-QUALIFYING ──
# The workup PAGE above stays evidence-only (it does not score or recommend).
# These endpoints are the SEPARATE feature/label/score research layer. Nothing
# here is a trade approval; formal_cohort_eligible is permanently False and no
# ML is involved. Unwired inputs are surfaced as explicit nulls, not values.

def _intel_data_client():
    """Best-effort Alpaca data client for intraday context; None → the feature
    engine degrades those groups rather than fabricating them."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        k = os.environ.get("ALPACA_API_KEY")
        s = os.environ.get("ALPACA_SECRET_KEY")
        if k and s:
            return StockHistoricalDataClient(k, s)
    except Exception:
        pass
    return None


@router.get("/api/intel/features/{ticker}")
def intel_features(ticker: str):
    import feature_engine as fe
    t = ticker.upper().strip()
    if not t.isalpha() or len(t) > 6:
        raise HTTPException(400, "bad symbol")
    p = run_path(t)
    payload = json.loads(p.read_text()) if p.exists() else None
    fr = fe.build_feature_record(_intel_data_client(), t, workup_payload=payload)
    return JSONResponse(fr)


@router.get("/api/intel/score/{ticker}")
def intel_score(ticker: str):
    import feature_engine as fe
    t = ticker.upper().strip()
    if not t.isalpha() or len(t) > 6:
        raise HTTPException(400, "bad symbol")
    p = run_path(t)
    payload = json.loads(p.read_text()) if p.exists() else None
    fr = fe.build_feature_record(_intel_data_client(), t, workup_payload=payload)
    sc = fe.score_record(fr)
    return JSONResponse({"score": sc, "feature_record": fr})


@router.post("/api/intel/label/{ticker}")
def intel_label(ticker: str):
    """Label every in-band contract's target-before-stop from today's underlying
    path (delta-gamma proxy, clearly flagged). Append-only. Best run after the
    close; before then, forward bars are partial and labels mark themselves
    unavailable rather than leaking future data."""
    import feature_engine as fe
    import entry_policy as ep
    t = ticker.upper().strip()
    if not t.isalpha() or len(t) > 6:
        raise HTTPException(400, "bad symbol")
    p = run_path(t)
    payload = json.loads(p.read_text()) if p.exists() else None
    if not payload:
        raise HTTPException(404, "no workup run for ticker today")
    dc = _intel_data_client()
    bars, _ = (ep._session_bars(dc, t) if dc else ([], None))
    fr = fe.build_feature_record(dc, t, workup_payload=payload)
    labels = fe.label_in_band(fr, payload.get("contracts") or [], bars)
    n = fe.persist_contract_labels(t, date.today().isoformat(), labels)
    return JSONResponse({
        "labeled": n,
        "available": sum(1 for l in labels if l.get("available")),
        "in_band": len(labels),
        "feature_record_timestamp": fr["timestamp"],
        "label_basis": "delta_gamma_proxy",
        "formal_cohort_eligible": False,
    })


@router.get("/workup", response_class=HTMLResponse)
def page():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workup — Scalpr</title>
<style>
:root{--ink:#0F1620;--panel:#16202C;--line:#243243;--text:#C9D6E4;
--dim:#6E8199;--faint:#465569;--clear:#4FB286;--mid:#D9A441;
--warn:#C4634F;--unknown:#5B6B80;
--mono:ui-monospace,"SF Mono",Menlo,monospace;
--sans:-apple-system,"Helvetica Neue",sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
font:13px/1.5 var(--mono);-webkit-font-smoothing:antialiased}
header{display:flex;gap:9px;align-items:center;flex-wrap:wrap;
padding:13px 18px;border-bottom:1px solid var(--line);position:sticky;
top:0;background:var(--ink);z-index:5}
h1{font:600 11px/1 var(--sans);letter-spacing:.22em;text-transform:uppercase;
color:var(--dim);margin:0 12px 0 0}
label{font:11px var(--sans);color:var(--faint);letter-spacing:.08em}
input,button,select{background:var(--panel);color:var(--text);
border:1px solid var(--line);border-radius:3px;padding:7px 9px;
font:13px var(--mono)}
input{width:92px;text-transform:uppercase}
input.n{width:76px;text-transform:none}
button{cursor:pointer;border-color:var(--faint)}
button:hover{border-color:var(--dim)}
button:disabled{opacity:.4;cursor:default}
button:focus-visible,input:focus-visible,select:focus-visible{
outline:2px solid var(--clear);outline-offset:1px}
main{padding:18px;max-width:1240px}
.meta{color:var(--dim);font-size:12px;margin-bottom:15px}
.meta b{color:var(--text)}
.gates{display:flex;gap:2px;margin-bottom:22px;flex-wrap:wrap}
.gate{flex:1;min-width:142px;background:var(--panel);
border-top:3px solid var(--g);padding:11px 12px 12px}
.gate .k{font:600 10px/1 var(--sans);letter-spacing:.18em;color:var(--dim)}
.gate .v{font-size:16px;margin:7px 0 5px;color:var(--g)}
.gate .n{font-size:11px;color:var(--dim);line-height:1.4}
.clear{--g:var(--clear)}.mid{--g:var(--mid)}
.warn{--g:var(--warn)}.unknown{--g:var(--unknown)}
h2{font:600 10px/1 var(--sans);letter-spacing:.18em;text-transform:uppercase;
color:var(--dim);margin:26px 0 8px;padding-bottom:7px;
border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:right;color:var(--faint);font-weight:400;padding:5px 7px;
font-size:11px}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:5px 7px;border-top:1px solid var(--line)}
tbody tr:hover{background:#1B2735}
tr.over td{color:var(--faint)}
.up{color:var(--clear)}.down{color:var(--warn)}.dim{color:var(--dim)}
.note{color:var(--faint);font-size:11px;margin-top:9px;max-width:66ch;
line-height:1.55}
.empty{color:var(--dim);padding:26px 0}
@media(prefers-reduced-motion:no-preference){
.pulse{animation:p 1.1s ease-in-out infinite}
@keyframes p{50%{opacity:.35}}}
</style></head><body>
<header>
<h1>Workup</h1>
<input id="t" value="GOOGL" aria-label="ticker">
<select id="dte" aria-label="DTE range">
<option value="7,60">DTE 7–60</option>
<option value="14,45">DTE 14–45</option>
<option value="30,120">DTE 30–120</option>
</select>
<label for="b">budget $</label><input class="n" id="b" value="2000">
<button id="load">Load cached</button>
<button id="run">Pull fresh</button>
<span id="st" class="meta" style="margin:0"></span>
</header>
<main><div id="body"><p class="empty">Enter a symbol and load a run.</p></div>
</main>
<script>
const $=s=>document.querySelector(s);let poll=null;
const n=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const m=v=>v==null?'—':Number(v).toLocaleString(undefined,
  {maximumFractionDigits:0});

function render(p){
 if(!p){$('#body').innerHTML='<p class="empty">No run on disk for today. '+
  'Pull fresh to build one.</p>';return;}
 const g=(p.gates||[]).map(x=>`<div class="gate ${x.state}">
  <div class="k">${x.key}</div><div class="v">${x.value}</div>
  <div class="n">${x.note}</div></div>`).join('');

 const liq=(p.contracts||[]).filter(c=>c.spread_pct<=10&&c.oi>=100
  &&c.volume>=10&&c.iv>0.01&&c.iv<3);
 const band=liq.filter(c=>Math.abs(c.delta||0)>=0.15
  &&Math.abs(c.delta||0)<=0.70)
  .sort((a,b)=>(b.max_contracts>0)-(a.max_contracts>0)
   ||a.spread_pct-b.spread_pct).slice(0,18);
 const open=liq.filter(c=>c.oi_chg>0&&(c.multileg_pct||0)<25)
  .sort((a,b)=>b.oi_chg-a.oi_chg).slice(0,14);

 const brows=band.map(c=>`<tr class="${c.max_contracts?'':'over'}">
  <td>${c.expiry}</td><td>${n(c.strike,1)}</td><td>${c.type}</td>
  <td>${c.dte}</td><td>${m(c.cost)}</td>
  <td>${c.max_contracts||'—'}</td><td>${m(c.friction)}</td>
  <td>${n(c.spread_pct,2)}</td><td>${n(c.iv,3)}</td>
  <td class="${c.delta>0?'up':'down'}">${n(c.delta,3)}</td>
  <td>${n(c.theta,3)}</td><td>${m(c.oi)}</td>
  <td class="${c.oi_since_prev==null?'dim':(c.oi_since_prev>0?'up':(c.oi_since_prev<0?'down':''))}">${c.oi_since_prev==null?'—':(c.oi_since_prev>0?'+':'')+m(c.oi_since_prev)}</td>
  <td>${m(c.volume)}</td></tr>`
 ).join('');

 const orows=open.map(c=>`<tr>
  <td>${c.expiry}</td><td>${n(c.strike,1)}</td><td>${c.type}</td>
  <td class="up">+${m(c.oi_chg)}</td><td>${n(c.ask_pct,1)}</td>
  <td>${n(c.multileg_pct,1)}</td><td>${n(c.vol_oi,2)}</td>
  <td class="${c.delta>0?'up':'down'}">${n(c.delta,3)}</td>
  <td>${m(c.premium)}</td></tr>`).join('');

 const mp=p.max_pain?`max pain ${p.max_pain.max_pain} `+
  `(${p.max_pain.expiry})`:'max pain n/a';

 $('#body').innerHTML=`
 <div class="meta"><b>${p.ticker}</b> · spot <b>${n(p.spot,2)}</b> ·
  DTE ${p.dte[0]}–${p.dte[1]} · budget $${m(p.budget)} · ${mp} ·
  ${p.contracts.length} contracts · pulled ${p.as_of}</div>
 <div class="gates">${g}</div>

 <h2>In band · delta 0.15–0.70 · greyed rows exceed budget</h2>
 <table><thead><tr><th>expiry</th><th>strike</th><th>t</th><th>dte</th>
 <th>cost</th><th>max</th><th>friction</th><th>spr%</th><th>iv</th>
 <th>delta</th><th>theta</th><th>oi</th><th>oiΔd</th><th>vol</th></tr></thead>
 <tbody>${brows||'<tr><td colspan=14 class="dim">none</td></tr>'}</tbody>
 </table>
 <p class="note">Cost is mid × 100. Friction is the bid-ask spread in dollars
  per contract — what a round trip costs before the underlying moves at all.
  A 2% spread on a $40,000 contract is $800. Percent alone hides that.
  oiΔd is open-interest change since the previous dated run for this ticker
  (${p.prev_as_of?'vs '+p.prev_as_of:'—'}) — cross-session position build/unwind,
  not intraday. Shows “—” until a second run on a later date exists.</p>

 <h2>OI increases · single-leg only</h2>
 <table><thead><tr><th>expiry</th><th>strike</th><th>t</th><th>oi chg</th>
 <th>ask%</th><th>ml%</th><th>vol/oi</th><th>delta</th><th>premium</th>
 </tr></thead>
 <tbody>${orows||'<tr><td colspan=9 class="dim">none</td></tr>'}</tbody>
 </table>
 <p class="note">Rows above 25% multileg are excluded — spread legs read as
  directional buying when they aren't. High ask% means lifted at the offer.
  vol/oi above 1 means today's volume exceeded standing open interest.
  None of this reveals intent: a bought put hedges a long book as readily as
  it bets against one.</p>`;
}

async function load(){
 const t=$('#t').value.trim().toUpperCase();if(!t)return;
 const r=await fetch('/api/workup/'+t),j=await r.json();
 if(j.state==='running'){
  $('#st').innerHTML='<span class="pulse">pulling '+t+'…</span>';
  if(!poll)poll=setInterval(load,3000);
  if(j.data)render(j.data);return;}
 if(poll){clearInterval(poll);poll=null;}
 $('#st').textContent=j.error?('error: '+j.error):'';
 $('#run').disabled=false;render(j.data);
}
$('#run').onclick=async()=>{
 const t=$('#t').value.trim().toUpperCase();
 const [lo,hi]=$('#dte').value.split(',');
 const b=parseFloat($('#b').value)||2000;
 $('#run').disabled=true;
 $('#st').innerHTML='<span class="pulse">pulling '+t+'…</span>';
 await fetch(`/api/workup/${t}?dte_lo=${lo}&dte_hi=${hi}&budget=${b}`,
  {method:'POST'});
 if(!poll)poll=setInterval(load,3000);
};
$('#load').onclick=load;
$('#t').addEventListener('keydown',e=>{if(e.key==='Enter')load();});
load();
</script></body></html>
"""
