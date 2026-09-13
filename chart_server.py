"""
Live chart server — candlestick chart with entry / SL / TP boxes in your browser.
Run:  python3 chart_server.py
Then: open  http://localhost:8888
"""

import json, os, re, time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
from jinja2 import Template

CRYPTO_STATE   = "crypto_state.json"
TEST_STATE     = "test_state.json"
STRATEGY_STATE = "strategy_state.json"
PORT           = 8888

_eq_cache = {"t": 0.0, "v": None}
def _fee_summary(account):
    """{'totalFees': float|None, 'feeRate': float|None} from the bot's state file.

    Returns None values rather than 0.0 when the key is absent, so the UI can show "—"
    for "never recorded" instead of a misleading $0.00 — the stock bot does not model
    fees at all, and crypto only started on 2026-08-22.
    """
    f = CRYPTO_STATE if account == "crypto" else STRATEGY_STATE
    try:
        with open(f) as fh:
            d = json.load(fh)
        return {"totalFees": d.get("total_fees"), "feeRate": d.get("fee_rate")}
    except Exception:
        return {"totalFees": None, "feeRate": None}


def _live_equity(account):
    """LIVE mark-to-market account equity for the journal's Portfolio Value KPI — the
    Robinhood-style number that moves with the market, NOT the once-a-day Macro snapshot.
    crypto: the bot's own state file (rewritten every loop, has the true `equity`).
    stock: Alpaca /v2/account (cached ~10s so polling can't hammer it). None on failure."""
    try:
        if account == "crypto":
            if not os.path.exists(CRYPTO_STATE):
                return None
            with open(CRYPTO_STATE) as fh:
                d = json.load(fh)
            return d.get("equity", d.get("balance"))
        # stock — live equity from Alpaca, cached
        if time.time() - _eq_cache["t"] < 10 and _eq_cache["v"] is not None:
            return _eq_cache["v"]
        from config import API_KEY, API_SECRET, BASE_URL
        req = urllib.request.Request(
            f"{BASE_URL}/v2/account",
            headers={"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        v = float(data["equity"]) if data.get("equity") else None
        _eq_cache.update(t=time.time(), v=v)
        return v
    except Exception:
        return None


# ── Intraday equity history ───────────────────────────────────────────────────
# The journal's equity chart was built purely from DAILY Macro-tab snapshots with
# "today" upserted as a single point — so there was no intraday curve for a live graph
# to draw, and polling faster couldn't invent one. A background sampler records equity
# on a fixed cadence so the chart has a real per-minute shape to render and tween along.
#
# Why sample here rather than poll harder from the browser: the underlying sources only
# move so fast (crypto_state.json is rewritten by the bot loop; Alpaca equity is cached
# ~10s above), so a 1s browser poll would return the same number ~10x and draw a
# staircase. Sampling server-side means history accumulates even with NO browser open,
# which is the part that actually matters — close the tab for an hour and the curve is
# still there when you come back.
EQUITY_HISTORY_FILE = "equity_history.json"
_EQ_SAMPLE_SECS     = 5           # cadence; finer than this just duplicates points
_EQ_MAX_POINTS      = 17280       # 24h at 5s — bounded so the file can't grow forever
_eq_history         = {"crypto": [], "stock": []}   # account -> [[epoch_secs, equity], ...]


def _load_equity_history():
    """Restore today's samples across a chart_server restart. Points older than 24h are
    dropped on load so a stale file can't resurrect yesterday's curve."""
    try:
        if not os.path.exists(EQUITY_HISTORY_FILE):
            return
        with open(EQUITY_HISTORY_FILE) as fh:
            saved = json.load(fh)
        cutoff = time.time() - 86400
        for acct in ("crypto", "stock"):
            pts = [p for p in saved.get(acct, []) if isinstance(p, list) and len(p) == 2
                   and p[0] >= cutoff]
            _eq_history[acct] = pts[-_EQ_MAX_POINTS:]
    except Exception:
        pass


def _save_equity_history():
    try:
        tmp = EQUITY_HISTORY_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_eq_history, fh)
        os.replace(tmp, EQUITY_HISTORY_FILE)   # atomic: a crash mid-write can't corrupt it
    except Exception:
        pass


def _equity_sampler():
    """Daemon loop: append one equity sample per account per tick. Fail-soft — a bad
    read just skips that tick rather than killing the thread and silently ending history."""
    last_save = 0.0
    while True:
        try:
            now = time.time()
            for acct in ("crypto", "stock"):
                v = _live_equity(acct)
                if v is None:
                    continue
                hist = _eq_history[acct]
                # Skip a duplicate timestamp/value pair — the sources update slower than
                # the sample rate, and repeated identical points just bloat the payload.
                if hist and abs(hist[-1][1] - float(v)) < 1e-9 and now - hist[-1][0] < 60:
                    continue
                hist.append([now, float(v)])
                if len(hist) > _EQ_MAX_POINTS:
                    del hist[:len(hist) - _EQ_MAX_POINTS]
            if now - last_save > 30:
                _save_equity_history()
                last_save = now
        except Exception:
            pass
        time.sleep(_EQ_SAMPLE_SECS)


def _bucket_secs_for(window_secs):
    """Interval the curve STEPS by, Robinhood-style.

    Robinhood does not slide its line forward continuously — it advances one point per
    interval, so a 5-minute chart gains a point at :00, :05, :10 and sits still between
    them. We sample equity every 5s, which is the right cadence for capturing the value
    but the wrong cadence for drawing: plotting every raw sample makes the curve creep
    forward every few seconds and turns a quiet hour into 720 near-identical points."""
    if window_secs <= 3600:    return 60     # 1H -> 1-minute steps  (60 points)
    if window_secs <= 21600:   return 300    # 6H -> 5-minute steps  (72 points)
    return 900                                # 1D -> 15-minute steps (96 points)


def _equity_series(account, window_secs=86400):
    """[{t, v}] bucketed to the window's interval, plus the baseline the UI measures against.

    Baseline = the FIRST point in the window, mirroring how Robinhood anchors its
    green/red change to the start of the selected period rather than to zero.

    Buckets are floored against the epoch, so their edges land on real wall-clock marks
    (:00, :05, :10 … and exact hours) rather than on whatever second the sampler happened
    to start — which is what "when does the day start?" comes down to. Every US/EU offset
    is a whole or half hour, so epoch-aligned edges are also local-clock-aligned edges."""
    bucket = _bucket_secs_for(window_secs)
    pts = [p for p in _eq_history.get(account, []) if p[0] >= time.time() - window_secs]
    if not pts:
        v = _live_equity(account)
        if v is None:
            return {"points": [], "baseline": None, "last": None, "bucket": bucket}
        pts = [[time.time(), float(v)]]

    # Keep each bucket's LAST sample — a bucket's "close", exactly like a candle. The
    # newest bucket is still forming, so its value keeps moving until the interval ends.
    closes = {}
    for t, v in pts:
        closes[int(t // bucket) * bucket] = v
    points = [{"t": k, "v": closes[k]} for k in sorted(closes)]
    return {
        "points":   points,
        "baseline": points[0]["v"],
        "last":     points[-1]["v"],
        "bucket":   bucket,
    }


# ── HTML page ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
<meta charset="UTF-8">
<title>Debbie-La Live Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#131722;color:#d1d4dc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  #header{padding:10px 16px;background:#1e222d;display:flex;align-items:center;gap:12px;border-bottom:1px solid #2a2e39;flex-shrink:0}
  #header h1{font-size:15px;font-weight:700;color:#ffffff;letter-spacing:.5px}
  select{background:#2a2e39;color:#d1d4dc;border:1px solid #363a45;padding:5px 10px;border-radius:4px;font-size:13px;cursor:pointer;outline:none}
  select:hover{border-color:#4c5261}
  #bal{font-size:13px;color:#787b86;margin-left:4px}
  #journal-link{margin-left:auto;background:#2a2e39;color:#d1d4dc;border:1px solid #363a45;padding:5px 12px;border-radius:4px;font-size:13px;text-decoration:none;display:flex;align-items:center;gap:6px;transition:border-color .15s,color .15s}
  #journal-link:hover{border-color:#22d3ee;color:#fff}
  #info{display:flex;gap:0;background:#1a1d27;border-bottom:1px solid #2a2e39;flex-shrink:0;overflow-x:auto}
  .icard{padding:10px 18px;border-right:1px solid #2a2e39;min-width:120px}
  .ilabel{font-size:10px;color:#787b86;text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px}
  .ival{font-size:14px;font-weight:600;white-space:nowrap}
  .g{color:#26A655}.r{color:#FF5350}.w{color:#ffffff}.b{color:#2196f3}.y{color:#ffb74d}
  #chart-wrap{flex:1;position:relative}
  #chart{width:100%;height:100%}
  #no-pos{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
  #no-pos span{background:#1e222d;padding:16px 28px;border-radius:8px;color:#787b86;font-size:14px;border:1px solid #2a2e39}
  #pulse{position:fixed;bottom:10px;right:14px;font-size:11px;color:#363a45;transition:color .3s}
  #pulse.active{color:#26A655}
</style>
</head>
<body>

<div id="header">
  <h1>⚡ DEBBIE-LA LIVE CHART</h1>
  <select id="botSel">
    <option value="test">test_bot  (1H/1M Sweep)</option>
    <option value="smc">binance_bot  (SMC)</option>
    <option value="strat">tradingbot  (SMC/Alpaca)</option>
  </select>
  <select id="symSel"></select>
  <span id="bal"></span>
  <a id="journal-link" href="/journal" target="_blank" rel="noopener">📓 Journal</a>
</div>

<div id="info">
  <div class="icard"><div class="ilabel">Side</div><div class="ival" id="i-side">—</div></div>
  <div class="icard"><div class="ilabel">Qty</div><div class="ival w" id="i-qty">—</div></div>
  <div class="icard"><div class="ilabel">Invested</div><div class="ival y" id="i-invested">—</div></div>
  <div class="icard"><div class="ilabel">Entry</div><div class="ival b" id="i-entry">—</div></div>
  <div class="icard"><div class="ilabel">Price</div><div class="ival w" id="i-price">—</div></div>
  <div class="icard"><div class="ilabel">Stop Loss</div><div class="ival r" id="i-sl">—</div></div>
  <div class="icard"><div class="ilabel">Take Profit</div><div class="ival g" id="i-tp">—</div></div>
  <div class="icard"><div class="ilabel">→ SL</div><div class="ival r" id="i-dsl">—</div></div>
  <div class="icard"><div class="ilabel">→ TP</div><div class="ival g" id="i-dtp">—</div></div>
  <div class="icard"><div class="ilabel">R : R</div><div class="ival w" id="i-rr">—</div></div>
  <div class="icard"><div class="ilabel">Unrealised P&L</div><div class="ival" id="i-pnl">—</div></div>
  <div class="icard"><div class="ilabel">Max Risk</div><div class="ival r" id="i-risk">—</div></div>
  <div class="icard"><div class="ilabel">Max Reward</div><div class="ival g" id="i-rew">—</div></div>
</div>

<div id="chart-wrap">
  <div id="chart"></div>
  <div id="no-pos"><span id="no-pos-msg">Select a bot and symbol above</span></div>
</div>
<div id="pulse">● live</div>

<script>
// Maps internal symbol name → Coinbase product id
const PAIRS = {
  'BTC/USD':'BTC-USD','ETH/USD':'ETH-USD','SOL/USD':'SOL-USD',
  'DOGE/USD':'DOGE-USD','XRP/USD':'XRP-USD','AVAX/USD':'AVAX-USD',
  'POL/USD':'POL-USD','ADA/USD':'ADA-USD',
  'LINK/USD':'LINK-USD','LTC/USD':'LTC-USD',
  'HYPE/USD':'HYPE-USD','INJ/USD':'INJ-USD','SEI/USD':'SEI-USD',
  'DRIFT/USD':'DRIFT-USD','ASTER/USD':'ASTER-USD',
};
const STOCK_SYMS = ['SPY','QQQ','AAPL','NVDA','TSLA','GOOGL','META','MSFT'];
const ALL_SYMS = {
  test:  ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD'],
  // smc mirrors binance_bot.py DEFAULT_SYMBOLS (13 as of 2026-08-20). test_bot still
  // trades only the 8 above — do not add the new coins there until test_bot.py:75 does.
  smc:   ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD',
          'HYPE/USD','INJ/USD','SEI/USD','DRIFT/USD','ASTER/USD'],
  strat: STOCK_SYMS,
};

// ── Chart setup ───────────────────────────────────────────────────────────────
const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background:{color:'#131722'}, textColor:'#d1d4dc' },
  grid:   { vertLines:{color:'#1e222d'}, horzLines:{color:'#1e222d'} },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor:'#2a2e39' },
  timeScale: { borderColor:'#2a2e39', timeVisible:true, secondsVisible:false },
  handleScale: true, handleScroll: true,
});

const candles = chart.addCandlestickSeries({
  upColor:'#0ced00', downColor:'#ff3c38',
  borderUpColor:'#0cee00', borderDownColor:'#ff3e3b',
  wickUpColor:'#0acb00', wickDownColor:'#ff4845',
});

// Pick price precision from magnitude so sub-dollar coins (POL $0.08, DOGE $0.08)
// don't collapse to a $0.01 grid. Default minMove=0.01 makes their whole range
// smaller than one tick → candles render as flat dashes and the axis reads "0.08".
function priceFmtFor(p){
  p = Math.abs(p || 0);
  if(p >= 1000) return {precision:2, minMove:0.01};
  if(p >= 1)    return {precision:3, minMove:0.001};
  if(p >= 0.1)  return {precision:4, minMove:0.0001};
  if(p >= 0.01) return {precision:5, minMove:0.00001};
  return {precision:7, minMove:0.0000001};
}

// Profit zone — BaselineSeries clips the fill exactly between data value and baseline.
// For LONG: data=TP (above baseline=entry) → topFill is #008000, bounded entry→TP.
// For SHORT: data=TP (below baseline=entry) → bottomFill is #008000, bounded TP→entry.
const greenZone = chart.addBaselineSeries({
  lineWidth:0,
  topLineColor:'rgba(0,0,0,0)', bottomLineColor:'rgba(0,0,0,0)',
  topFillColor1:'rgba(38,166,154,0.20)', topFillColor2:'rgba(38,166,154,0.05)',
  bottomFillColor1:'rgba(0,0,0,0)',      bottomFillColor2:'rgba(0,0,0,0)',
  baseValue:{ type:'price', price:0 },
  priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
});
const redZone = chart.addBaselineSeries({
  lineWidth:0,
  topLineColor:'rgba(0,0,0,0)', bottomLineColor:'rgba(0,0,0,0)',
  topFillColor1:'rgba(0,0,0,0)',             topFillColor2:'rgba(0,0,0,0)',
  bottomFillColor1:'rgba(239,83,80,0.05)', bottomFillColor2:'rgba(239,83,80,0.20)',
  baseValue:{ type:'price', price:0 },
  priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
});

// Diagonal trendline (ascending support / descending resistance)
const trendLine = chart.addLineSeries({
  color:'#ffb300', lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dashed,
  priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
});
function drawTrend(tl, lastTime){
  if(!tl || !tl.t1 || !tl.t2 || tl.t2<=tl.t1){ trendLine.setData([]); return; }
  // extend the line from the first anchor through to the latest candle using its slope
  const slope = (tl.p2 - tl.p1) / (tl.t2 - tl.t1);
  const endT  = (lastTime && lastTime > tl.t2) ? lastTime : tl.t2;
  const endP  = tl.p1 + slope * (endT - tl.t1);
  trendLine.setData([{ time: tl.t1, value: tl.p1 }, { time: endT, value: endP }]);
}

let pLines = [];
function clearLines(){ pLines.forEach(l=>{try{candles.removePriceLine(l)}catch(e){}}); pLines=[]; }

function drawLines(entry, sl, tp){
  clearLines();
  if(!entry) return;
  pLines.push(candles.createPriceLine({price:entry, color:'#2196f3', lineWidth:2,
    lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'ENTRY'}));
  if(sl) pLines.push(candles.createPriceLine({price:sl, color:'#ef5350', lineWidth:2,
    lineStyle:LightweightCharts.LineStyle.Solid, axisLabelVisible:true, title:'STOP'}));
  if(tp) pLines.push(candles.createPriceLine({price:tp, color:'#26a69a', lineWidth:2,
    lineStyle:LightweightCharts.LineStyle.Solid, axisLabelVisible:true, title:'TARGET'}));
}

// ── Bot's pending analysis: draw what it's WATCHING when it has no position yet ──
// Reads the per-symbol state machine straight from the state file: the entry zone
// (AMD / FVG / supply / demand), the swept liquidity level, and the EQL/EQH pools.
let aLines = [];
function clearAnalysis(){ aLines.forEach(l=>{try{candles.removePriceLine(l)}catch(e){}}); aLines=[]; }

function drawAnalysis(sm){
  clearAnalysis();
  if(!sm) return;
  const st = sm.state;
  if((st==='ENTRY_WAIT'||st==='SWEEP_HUNT') && sm.fvg_low && sm.fvg_high){
    const bearish = sm.bias==='BEARISH';
    const col  = bearish ? '#ff7043' : '#66bb6a';      // supply=orange, demand=green
    const kind = sm.amd_phase ? 'AMD' : 'FVG';
    const side = bearish ? 'SHORT' : 'LONG';
    aLines.push(candles.createPriceLine({price:sm.fvg_high, color:col, lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
      title:`${kind} zone ▲ ${side}`}));
    aLines.push(candles.createPriceLine({price:sm.fvg_low, color:col, lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
      title:`${kind} zone ▼`}));
  }
  if(sm.sweep_low){
    aLines.push(candles.createPriceLine({price:sm.sweep_low, color:'#ab47bc', lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'swept'}));
  }
  // EQL / EQH liquidity pools (present only if the bot wrote them to state)
  if(sm.eql_level) aLines.push(candles.createPriceLine({price:sm.eql_level, color:'#42a5f5',
    lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
    title:`EQL ${sm.eql_touch||''}`.trim()}));
  if(sm.eqh_level) aLines.push(candles.createPriceLine({price:sm.eqh_level, color:'#42a5f5',
    lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
    title:`EQH ${sm.eqh_touch||''}`.trim()}));

  // ── SMC context markings — what the bot is structurally "seeing" right now ─────
  // Distinct colors so they're separable at a glance from the zone/pool lines above:
  //   BOS = yellow, CHoCH = pink, displacement = cyan band, OTE = purple band,
  //   inducement = red (the trap level that tends to get swept before the zone tap).
  if(sm.bos_level) aLines.push(candles.createPriceLine({price:sm.bos_level, color:'#ffd54f',
    lineWidth:2, lineStyle:LightweightCharts.LineStyle.Solid, axisLabelVisible:true,
    title:`BOS ${sm.bos_dir==='bullish'?'▲':'▼'}`}));

  if(sm.choch_level) aLines.push(candles.createPriceLine({price:sm.choch_level, color:'#f06292',
    lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true,
    title:`CHoCH ${sm.choch_dir==='bullish'?'▲':'▼'}`}));

  // Displacement candle range — the momentum bar that left the imbalance
  if(sm.disp_high && sm.disp_low){
    aLines.push(candles.createPriceLine({price:sm.disp_high, color:'#26c6da', lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true, title:'DISP ▲'}));
    aLines.push(candles.createPriceLine({price:sm.disp_low, color:'#26c6da', lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true, title:'DISP ▼'}));
  }

  // Fib Optimal Trade Entry band (38.2%-61.8% of the recent swing)
  if(sm.ote_high && sm.ote_low){
    aLines.push(candles.createPriceLine({price:sm.ote_high, color:'#ba68c8', lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.LargeDashed, axisLabelVisible:true, title:'OTE 0.382'}));
    aLines.push(candles.createPriceLine({price:sm.ote_low, color:'#ba68c8', lineWidth:1,
      lineStyle:LightweightCharts.LineStyle.LargeDashed, axisLabelVisible:true, title:'OTE 0.618'}));
  }

  // Inducement — the minor pool likely swept BEFORE price delivers into our zone
  if(sm.inducement) aLines.push(candles.createPriceLine({price:sm.inducement, color:'#ef5350',
    lineWidth:2, lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
    title:'IDM (inducement)'}));
}

// ── test_bot's 1H liquidity pools: the two swing high/low levels it's watching
// for a sweep+reversal. Only meaningful while flat — cleared once a position opens.
let poolLines = [];
function clearPools(){ poolLines.forEach(l=>{try{candles.removePriceLine(l)}catch(e){}}); poolLines=[]; }

function drawPools(pool){
  clearPools();
  if(!pool || pool.state==='IN_TRADE') return;
  if(pool.pool_high) poolLines.push(candles.createPriceLine({price:pool.pool_high, color:'#ff7043',
    lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
    title:'1H HIGH'}));
  if(pool.pool_low) poolLines.push(candles.createPriceLine({price:pool.pool_low, color:'#42a5f5',
    lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dotted, axisLabelVisible:true,
    title:'1H LOW'}));
}

function drawZones(times, entry, sl, tp, isLong, entryTime){
  if(!entry || !times.length){ greenZone.setData([]); redZone.setData([]); candles.setMarkers([]); return; }

  // Baseline is always the entry price — this is the boundary both zones hinge on.
  greenZone.applyOptions({ baseValue:{ type:'price', price: entry } });
  redZone.applyOptions({   baseValue:{ type:'price', price: entry } });

  if(isLong){
    greenZone.applyOptions({
      topFillColor1:'rgba(38,166,154,0.20)', topFillColor2:'rgba(38,166,154,0.05)',
      bottomFillColor1:'rgba(0,0,0,0)',      bottomFillColor2:'rgba(0,0,0,0)',
    });
    redZone.applyOptions({
      topFillColor1:'rgba(0,0,0,0)',           topFillColor2:'rgba(0,0,0,0)',
      bottomFillColor1:'rgba(239,83,80,0.05)', bottomFillColor2:'rgba(239,83,80,0.20)',
    });
  } else {
    greenZone.applyOptions({
      topFillColor1:'rgba(0,0,0,0)',           topFillColor2:'rgba(0,0,0,0)',
      bottomFillColor1:'rgba(38,166,154,0.05)',bottomFillColor2:'rgba(38,166,154,0.20)',
    });
    redZone.applyOptions({
      topFillColor1:'rgba(239,83,80,0.20)', topFillColor2:'rgba(239,83,80,0.05)',
      bottomFillColor1:'rgba(0,0,0,0)',     bottomFillColor2:'rgba(0,0,0,0)',
    });
  }

  // Start the zone at the exact candle where the trade was entered.
  // 1. Prefer the ISO entry_time from the state file (exact match).
  // 2. Fallback: scan candles for the first bar whose wick touched entry price
  //    (high >= entry for LONG, low <= entry for SHORT) — works for old trades
  //    that pre-date the entry_time field.
  let startIdx = 0;
  const _interval = times.length > 1 ? times[times.length-1] - times[times.length-2] : 300;
  if(entryTime){
    // Truncate microseconds to milliseconds — Python isoformat() emits 6-digit
    // fractions (.104002) which some JS engines parse as NaN, breaking findIndex.
    const cleanTime = entryTime.replace(/(\.\d{3})\d+/, '$1');
    const entryTs = Math.floor(new Date(cleanTime).getTime() / 1000);
    // Put the marker on the candle that CONTAINS the entry (open ≤ entry < next open),
    // not the first candle at/after it — `t >= entryTs` landed one candle late whenever
    // the fill happened mid-candle (entry 21:26 → marked on the 21:30 bar, not 21:25).
    startIdx = times.findIndex(t => t + _interval > entryTs && t <= entryTs);
    if(startIdx < 0) startIdx = times.findIndex(t => t >= entryTs);   // fallback: nearest after
    if(startIdx < 0) startIdx = Math.max(0, times.length - 60);
  } else if(lastCandles.length && entry){
    // Scan oldest→newest: first candle whose wick reached entry price = entry candle
    const idx = isLong
      ? lastCandles.findIndex(c => c.high >= entry)
      : lastCandles.findIndex(c => c.low  <= entry);
    startIdx = idx >= 0 ? idx : Math.max(0, times.length - 60);
  } else {
    startIdx = Math.max(0, times.length - 60);
  }
  // Extend the zone a few candles past the last real candle so the box always has
  // visual width even when a trade just opened (same look as TradingView position tool).
  // Kept small (12) so fitContent() doesn't leave a big empty gap on the right that
  // would push the real candles left and re-fatten them.
  const interval = times.length > 1 ? times[times.length-1] - times[times.length-2] : 300;
  const lastT    = times[times.length - 1];
  const future   = Array.from({length: 12}, (_, i) => lastT + interval * (i + 1));
  const zoneTimes = [...times.slice(startIdx), ...future];
  greenZone.setData(zoneTimes.map(t=>({ time:t, value: tp || entry })));
  redZone.setData(  zoneTimes.map(t=>({ time:t, value: sl || entry })));

  // Entry marker — an arrow ON the exact candle the trade was entered, so you can
  // see WHERE it fired, not just the horizontal entry line. Up-arrow below the bar
  // for a long (buy), down-arrow above the bar for a short (sell).
  const entryT = times[startIdx];
  if(entryT != null){
    // ONE marker: the arrow shape IS the arrow. The old text 'ENTRY ▲' also carried a
    // triangle glyph, so you saw the arrow shape PLUS a ▲ — that's the "two arrows".
    candles.setMarkers([{
      time: entryT,
      position: isLong ? 'belowBar' : 'aboveBar',
      color: '#2196f3',
      shape: isLong ? 'arrowUp' : 'arrowDown',
      text: 'ENTRY',
    }]);
  } else {
    candles.setMarkers([]);
  }
}

new ResizeObserver(()=>{
  chart.applyOptions({width:chartEl.clientWidth, height:chartEl.clientHeight});
}).observe(chartEl);

// ── State ─────────────────────────────────────────────────────────────────────
let lastCandleTimes  = [];
let lastCandles      = [];     // full OHLCV — used to locate entry candle when entry_time is null
let chartLoadedSym   = null;   // which symbol is currently fully loaded
let lastCandleCount  = 0;

function fmt(n, dec=2){ return n!=null ? '$'+n.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec}) : '—'; }
function pct(a,b){ return b ? ((a-b)/b*100) : 0; }

function updateInfo(pos, curPrice){
  const noPos = document.getElementById('no-pos');
  if(!pos){
    noPos.style.display='flex';
    document.getElementById('no-pos-msg').textContent='No open position for this symbol';
    ['i-side','i-qty','i-invested','i-entry','i-price','i-sl','i-tp','i-dsl','i-dtp','i-rr','i-pnl','i-risk','i-rew']
      .forEach(id=>{ document.getElementById(id).innerHTML='—'; });
    return;
  }
  noPos.style.display='none';

  const isLong = pos.side==='LONG';
  const {entry_price:entry, stop_loss:sl, take_profit:tp, unrealized_pnl:upnl, qty} = pos;
  const risk   = sl && entry ? Math.abs(entry-sl)*Math.abs(qty) : null;
  const reward = tp && entry ? Math.abs(tp-entry)*Math.abs(qty) : null;
  const rr     = risk&&reward ? (reward/risk).toFixed(1) : null;
  const dSL    = sl&&curPrice ? pct(sl,curPrice) : null;
  const dTP    = tp&&curPrice ? pct(tp,curPrice) : null;
  const mv     = curPrice&&entry ? pct(curPrice,entry) : null;

  const set = (id,html)=>{ document.getElementById(id).innerHTML=html; };
  const absQty = qty!=null ? Math.abs(+qty) : null;
  const lev     = pos.leverage || 1;
  const margin  = pos.margin  != null ? pos.margin  : (absQty && entry ? absQty*entry/lev : null);
  const control = absQty && entry ? absQty*entry : null;
  set('i-side',  isLong?'<span class="g">▲ LONG</span>':'<span class="r">▼ SHORT</span>');
  set('i-qty',   absQty!=null ? absQty.toLocaleString(undefined,{maximumFractionDigits:4}) : '—');
  // INVESTED = margin posted (what you actually put in), not full position value
  set('i-invested', margin!=null
    ? `$${margin.toFixed(2)}<br><span style="font-size:10px;color:#787b86">${lev}x → $${control?.toFixed(2)} controlled</span>`
    : '—');
  set('i-entry', fmt(entry,4));
  set('i-price', curPrice ? `${fmt(curPrice,4)}<br><span style="font-size:11px;color:${mv>=0?'#26a69a':'#ef5350'}">${mv>=0?'+':''}${mv?.toFixed(2)}%</span>` : '—');
  set('i-sl',    fmt(sl,4));
  set('i-tp',    fmt(tp,4));
  set('i-dsl',   dSL!=null ? `<span class="r">${dSL.toFixed(2)}%</span>` : '—');
  set('i-dtp',   dTP!=null ? `<span class="g">${dTP>=0?'+':''}${dTP.toFixed(2)}%</span>` : '—');
  set('i-rr',    rr ? `1 : ${rr}` : '—');
  const pnlClr  = upnl>=0?'g':'r';
  set('i-pnl',   `<span class="${pnlClr}">${upnl>=0?'+':''}$${upnl?.toFixed(2)}</span>`);
  set('i-risk',  risk  ? `-$${risk.toFixed(2)}`  : '—');
  set('i-rew',   reward? `+$${reward.toFixed(2)}`: '—');
}

// ── Data fetching ─────────────────────────────────────────────────────────────
async function fetchCandles(sym){
  const bot = document.getElementById('botSel').value;
  try{
    if(bot === 'strat'){
      const r = await fetch(`/api/yfcandles?sym=${sym}`);
      const j = await r.json();
      if(!Array.isArray(j)||!j.length) return [];
      return j; // already {time,open,high,low,close}
    }
    const pair = PAIRS[sym]; if(!pair) return [];
    const r = await fetch(`/api/candles?sym=${pair}&gran=300`);
    const j = await r.json();
    if(!Array.isArray(j)||!j.length) return [];
    // Coinbase: [time, low, high, open, close, volume] — time in seconds, newest-first
    return j.slice().reverse().map(c=>({
      time:+c[0], open:+c[3], high:+c[2], low:+c[1], close:+c[4]
    }));
  }catch(e){ return []; }
}

async function fetchState(){
  try{ const r=await fetch('/api/state'); return r.json(); }catch(e){ return null; }
}

function updateSymSel(state){
  const bot   = document.getElementById('botSel').value;
  const data  = bot==='test' ? state?.test : bot==='strat' ? state?.strat : state?.smc;
  const pos   = data?.positions||{};
  const syms  = ALL_SYMS[bot]||[];
  const sorted= [...syms].sort((a,b)=>(pos[b]?1:0)-(pos[a]?1:0));
  const sel   = document.getElementById('symSel');
  const prev  = sel.value;
  sel.innerHTML = sorted.map(s=>{
    const base=s.split('/')[0];
    return `<option value="${s}">${base}${pos[s]?' ●':''}</option>`;
  }).join('');
  if(prev&&sorted.includes(prev)) sel.value=prev;

  // Prefer equity (cash + open position value) over raw cash balance — a large open
  // position deducts its cost from cash, so raw balance reads like a huge loss when
  // the money is merely deployed. Older state files without equity fall back to balance.
  const bal=data?.equity ?? data?.balance, start=data?.start_balance;
  const pnl=bal&&start?(bal-start):null;
  const pnlPct=pnl!=null&&start?(pnl/start*100):null;
  document.getElementById('bal').textContent = bal
    ? `  Equity: $${bal.toLocaleString(undefined,{minimumFractionDigits:2})}${pnl!=null?`  |  P&L: ${pnl>=0?'+':''}$${pnl.toFixed(2)}${pnlPct!=null?` (${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%)`:''}`:''}`
    : '';
}

// ── Main refresh ──────────────────────────────────────────────────────────────
async function refresh(){
  const bot = document.getElementById('botSel').value;
  const sym = document.getElementById('symSel').value;
  if(!sym) return;

  const [state, cdata] = await Promise.all([fetchState(), fetchCandles(sym)]);

  if(cdata.length){
    lastCandleTimes = cdata.map(c=>c.time);
    lastCandles     = cdata;
    const symChanged = sym !== chartLoadedSym;
    if(symChanged || cdata.length !== lastCandleCount){
      candles.setData(cdata);
      if(symChanged){
        // Match price precision to the coin's magnitude (fixes $0.08 coins rendering
        // as flat dashes on a $0.01 grid with an unreadable "0.08 / 0.08" axis).
        const lastClose = cdata[cdata.length - 1].close;
        candles.applyOptions({ priceFormat: { type:'price', ...priceFmtFor(lastClose) } });
        // fitContent (not scrollToRealTime) so ALL fetched bars fill the width — the
        // default view zoomed into ~60 bars, making each candle look massive/"zoomed in".
        // Fitting the full ~300-bar window shrinks candles to a normal width with real
        // macro context (the whole move, not just the last hour).
        chart.timeScale().fitContent();
        // Force price axis to re-fit — prevents BTC's $67k scale carrying over to SOL's $75
        chart.priceScale('right').applyOptions({ autoScale: true });
      }
      chartLoadedSym  = sym;
      lastCandleCount = cdata.length;
    } else {
      candles.update(cdata[cdata.length - 1]);
    }
  }

  updateSymSel(state);

  const botData = bot==='test' ? state?.test : bot==='strat' ? state?.strat : state?.smc;
  const pos     = botData?.positions?.[sym]||null;
  const cur     = cdata.length ? cdata[cdata.length-1].close : pos?.current_price;

  const sm       = botData?.state_machine?.[sym] || null;
  const pool     = bot==='test' ? botData?.pools?.[sym] || null : null;
  const lastTime = lastCandleTimes.length ? lastCandleTimes[lastCandleTimes.length-1] : null;
  if(pos){
    const isLong = pos.side==='LONG';
    drawLines(pos.entry_price, pos.stop_loss, pos.take_profit);
    drawZones(lastCandleTimes, pos.entry_price, pos.stop_loss, pos.take_profit, isLong,
              pos.entry_time || sm?.entry_time);
    clearAnalysis();   // position lines take over; hide the pending-zone overlay
    clearPools();
    drawTrend(sm?.trendline, lastTime);   // keep the structural trendline visible
  } else {
    clearLines(); drawZones([],null,null,null,true);
    drawAnalysis(sm);                      // show what the bot is watching (SMC bot)
    drawPools(pool);                       // show the 1H liquidity pools (test bot)
    drawTrend(sm?.trendline, lastTime);    // + the diagonal trendline
  }
  updateInfo(pos, cur);

  // Pulse indicator
  const p=document.getElementById('pulse');
  p.classList.add('active');
  setTimeout(()=>p.classList.remove('active'),500);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.getElementById('botSel').addEventListener('change', ()=>{ updateSymSel(null); refresh(); });
document.getElementById('symSel').addEventListener('change', refresh);

// Initial symbol load
fetchState().then(s=>{ updateSymSel(s); refresh(); });
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def strategy_to_chart(raw):
    """Convert strategy_state.json (POSITION_OPEN entries) to the crypto-state shape the JS expects."""
    if not raw:
        return {"positions": {}, "state_machine": {}}
    positions, sm = {}, {}
    for sym, d in raw.items():
        sm[sym] = {"state": d.get("state"), "bias": d.get("bias")}
        if d.get("state") == "POSITION_OPEN":
            ep  = d.get("entry_price") or 0
            mg  = d.get("margin")
            lev = d.get("leverage", 4)
            qty = round(mg * lev / ep, 4) if (mg and ep) else 0
            is_long = d.get("bias") == "BULLISH"
            positions[sym] = {
                "side":           "LONG" if is_long else "SHORT",
                "qty":            qty if is_long else -qty,
                "entry_price":    ep,
                "stop_loss":      d.get("stop_loss"),
                "take_profit":    d.get("take_profit"),
                "entry_time":     d.get("entry_time"),
                "margin":         mg,
                "leverage":       lev,
                "unrealized_pnl": None,
            }
    return {"positions": positions, "state_machine": sm}


# ── Journal: reads Macro/Ledger tabs from Google Sheets, feeds templates/journal.html ──
# Sheets access is imported lazily (inside _fetch_journal_data), not at module load —
# this file has never depended on Sheets/gspread being installed or reachable, and the
# live chart (its main job) must keep working even if the journal's data source is down.

# Every shape the Macro tabs actually contain. The two tabs do NOT agree with each other:
# Crypto Macro carries Sheets' auto long format, Stock Macro carries the abbreviated one.
# Missing the abbreviated form silently dropped EVERY stock row (the caller skips
# unparseable cells), leaving the Quant Desk stock equity curve flat with "1d total"
# despite 26 daily rows existing. Skipping one corrupt cell is right; skipping an entire
# tab is not — so cover the variants rather than assume one writer.
_DATE_FORMATS = (
    "%Y-%m-%d",                        # what log_daily_snapshot actually writes
    "%A, %B %d, %Y at %I:%M:%S %p",    # Sheets auto long-format — real Crypto Macro cells
    "%a, %b %d, %Y",                   # abbreviated — real Stock Macro cells
    "%A, %B %d, %Y",                   # long day, no time component
    "%B %d, %Y",
    "%b %d, %Y",
    "%m/%d/%Y",                        # US locale numeric
)

def _parse_sheet_date(raw):
    """Normalize a Macro-tab Date cell to 'YYYY-MM-DD'. Returns None if unparseable
    (caller skips the row rather than guessing)."""
    if not raw:
        return None
    raw = raw.replace(" ", " ").strip()   # narrow no-break space before AM/PM
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _num(s):
    """'$1,234.56' / '-$12.34' / 1234.5 -> float. None if unparseable."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


_JOURNAL_CACHE = {}   # (account, kind) -> last successfully-parsed data


def _fetch_journal_data(account):
    """account: 'crypto' or 'stock'. Returns (macro_data, ledger_data) in the exact
    shapes journal.html's JS expects:
      macro_data:  [{time: 'YYYY-MM-DD', value: <portfolio $>}, ...] sorted ascending
      ledger_data: [{date, ticker, side, pnl, chartUrl, reason, ...}, ...]
    Never raises. A transient Sheets/network blip ('No route to host', a dropped
    connection) is retried briefly; if it still fails, the last successfully-fetched
    copy of that tab is served instead of an empty list — so a momentary hiccup shows
    slightly-stale data, not a misleading empty journal. Only if a tab was NEVER
    fetched does it fall back to empty."""
    macro_tab  = "Crypto Macro"  if account == "crypto" else "Stock Macro"
    ledger_tab = "Crypto Ledger" if account == "crypto" else "Stock Ledger"

    def _retry(fn, attempts=2, delay=3):
        last = None
        for i in range(1, attempts + 1):
            try:
                return fn()
            except Exception as e:
                last = e
                if i < attempts:
                    time.sleep(delay)
        raise last

    def _cached(kind, fn):
        """Retry fn; cache & return on success; on failure serve last-good (or None)."""
        key = (account, kind)
        try:
            data = _retry(fn)
            _JOURNAL_CACHE[key] = data
            return data
        except Exception as e:
            if key in _JOURNAL_CACHE:
                print(f"[JOURNAL] {kind} read failed ({e}) — serving last cached copy")
                return _JOURNAL_CACHE[key]
            print(f"[JOURNAL] {kind} read failed ({e}) — no cache yet, returning empty")
            return None

    try:
        import sheets_logger as sl
        from config import GOOGLE_SHEET_URL
        sh = _retry(lambda: sl._get_spreadsheet(sl.get_sheet_client(), GOOGLE_SHEET_URL))
    except Exception as e:
        # Total connection failure — serve last-good for both tabs if we ever fetched them.
        print(f"[JOURNAL] Sheets connection failed ({e}) — serving cached data if available")
        return (_JOURNAL_CACHE.get((account, "macro"), []),
                _JOURNAL_CACHE.get((account, "ledger"), []))

    def _read_macro():
        rows = sh.worksheet(macro_tab).get_all_values()[1:]
        # Dedupe same-day snapshots: restarts before the once-per-day persistence fix
        # produced multiple rows for one date, and lightweight-charts requires strictly
        # ascending, unique time points — keep the LAST (most recent) value per day.
        by_date = {}
        for r in rows:
            if len(r) < 2:
                continue
            d = _parse_sheet_date(r[0])
            v = _num(r[1])
            if d and v is not None:
                by_date[d] = v
        return [{"time": d, "value": v} for d, v in sorted(by_date.items())]

    def _read_ledger():
        ws = sh.worksheet(ledger_tab)
        rows    = ws.get_all_values()[1:]
        # Second fetch, FORMULA render, only to pull the raw chart URL out of the
        # =HYPERLINK(url, IMAGE(url)) formula text in the Chart column — the default
        # render option returns the rendered image, not the URL string.
        formula = ws.get_all_values(value_render_option="FORMULA")[1:]
        out = []
        for r, f in zip(rows, formula):
            if len(r) < 14:
                continue
            exit_date = r[1][:10] if r[1] and len(r[1]) >= 10 else None   # ISO -> 'YYYY-MM-DD'
            pnl = _num(r[12])
            if not exit_date or pnl is None:
                continue
            chart_url = None
            if len(f) > 14:
                m = re.search(r'IMAGE\("([^"]+)"\)', f[14])
                if m:
                    chart_url = m.group(1)
            out.append({
                "date": exit_date, "ticker": r[2], "side": r[3],
                "pnl": pnl, "chartUrl": chart_url, "reason": r[13],
                # Full trade detail (LEDGER_HEADER order — see sheets_logger.py)
                "entryTime": r[0] or None, "exitTime": r[1] or None,
                "entry": _num(r[4]), "stopLoss": _num(r[5]), "takeProfit": _num(r[6]),
                "exit": _num(r[7]), "size": _num(r[8]), "margin": _num(r[9]),
                "notional": _num(r[10]), "leverage": _num(r[11]),
                # Appended 2026-09-04, AFTER Chart, so every index above is unchanged and
                # the ~177 pre-existing rows keep parsing. Both are None on those older
                # rows — which is the honest reading: their P&L is GROSS, and nothing
                # recorded why they closed. Don't render a missing value as 0 or "STOP".
                "fees": _num(r[15]) if len(r) > 15 else None,
                "exitReason": (r[16] or None) if len(r) > 16 else None,
            })
        return out

    macro_data  = _cached("macro", _read_macro) or []
    ledger_data = _cached("ledger", _read_ledger) or []
    return macro_data, ledger_data


_JOURNAL_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "templates", "journal.html")

def render_journal_page(account):
    """Fetch + render the full journal.html response body (bytes) for one account."""
    macro_data, ledger_data = _fetch_journal_data(account)
    with open(_JOURNAL_TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = Template(f.read())
    return tpl.render(macro_data=macro_data, ledger_data=ledger_data, account=account).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/favicon.ico", "/bar-chart-emoji.jpg"):
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bar-chart-emoji.jpg")
            if os.path.exists(fpath):
                body = open(fpath, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
            return
        if path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path.startswith("/api/candles"):
            params  = parse_qs(urlparse(self.path).query)
            product = params.get("sym",  ["BTC-USD"])[0]   # Coinbase product id e.g. BTC-USD
            gran    = params.get("gran", ["300"])[0]        # granularity in seconds (300 = 5m)
            try:
                url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
                       f"?granularity={gran}&limit=300")   # more macro context (was 200)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    body = resp.read()
            except Exception:
                body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/yfcandles":
            qs  = parse_qs(urlparse(self.path).query)
            sym = (qs.get("sym", [""])[0]).upper()
            try:
                url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                       f"?interval=5m&range=1d")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
                r   = data["chart"]["result"][0]
                ts  = r["timestamp"]
                q   = r["indicators"]["quote"][0]
                body = json.dumps([
                    {"time": ts[i], "open": q["open"][i], "high": q["high"][i],
                     "low": q["low"][i], "close": q["close"][i]}
                    for i in range(len(ts))
                    if None not in (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
                ]).encode()
            except Exception:
                body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/equity_series":
            # Intraday equity curve + the baseline the UI anchors its $/% change to.
            # `window` in seconds so the client can ask for 1H / 1D / etc.
            q = parse_qs(urlparse(self.path).query)
            account = q.get("account", ["crypto"])[0]
            try:
                window = max(60, min(86400, int(q.get("window", ["86400"])[0])))
            except (TypeError, ValueError):
                window = 86400
            body = json.dumps(_equity_series(account, window)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/botstatus":
            # "Online" = the bot PROCESS is actually running (ground truth via pgrep),
            # not merely that a state file looks fresh — the stock bot only rewrites its
            # state on position events, so file-freshness would falsely read OFFLINE
            # whenever it's idle-but-running. State-file age is still returned as extra
            # context (how long since the last write).
            import subprocess
            account = parse_qs(urlparse(self.path).query).get("account", ["crypto"])[0]
            patterns = ["binance_bot.py", "test_bot.py"] if account == "crypto" else ["tradingbot.py"]
            def _running(pat):
                try:
                    return subprocess.run(["pgrep", "-f", pat], capture_output=True,
                                          timeout=3).returncode == 0
                except Exception:
                    return False
            online = any(_running(p) for p in patterns)
            files = [CRYPTO_STATE, TEST_STATE] if account == "crypto" else [STRATEGY_STATE]
            newest = max((os.path.getmtime(f) for f in files if os.path.exists(f)), default=None)
            body = json.dumps({
                "online": online,
                "lastUpdate": (datetime.fromtimestamp(newest, timezone.utc).isoformat().replace("+00:00", "Z")) if newest else None,
                "ageSeconds": round(time.time() - newest) if newest else None,
                "equity": _live_equity(account),   # live Portfolio Value (moves with the market)
                # Cumulative trading costs the PaperTrader has charged. Counts from the
                # first run of the fee model (2026-08-22) — it is NOT lifetime, because
                # nothing charged fees before then. Surfaced so the cost of trading is
                # visible next to Net P&L rather than buried in crypto_state.json.
                **_fee_summary(account),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/state":
            def load(f):
                if not os.path.exists(f): return None
                try:
                    with open(f) as fh: return json.load(fh)
                except Exception: return None
            body = json.dumps({
                "smc":   load(CRYPTO_STATE),
                "test":  load(TEST_STATE),
                "strat": strategy_to_chart(load(STRATEGY_STATE)),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/journal":
            account = parse_qs(urlparse(self.path).query).get("account", ["crypto"])[0]
            if account not in ("crypto", "stock"):
                account = "crypto"
            try:
                body = render_journal_page(account)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = f"Journal failed to load: {e}".encode()
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


class ChartServer(HTTPServer):
    def handle_error(self, request, client_address):
        # Browser tab closed/refreshed mid-response — harmless, don't spam the console.
        import sys
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    print("=" * 55)
    print("  DEBBIE-LA LIVE CHART SERVER")
    print("=" * 55)
    print(f"  Open your browser → http://localhost:{PORT}")
    print(f"  Trade journal      → http://localhost:{PORT}/journal")
    print("  (keep binance_bot.py + test_bot.py running too)")

    # Intraday equity sampler: restore today's curve, then keep recording in the
    # background so history builds whether or not a browser tab is open. Daemon so
    # Ctrl-C still exits immediately.
    import threading
    _load_equity_history()
    threading.Thread(target=_equity_sampler, daemon=True, name="equity-sampler").start()
    _pts = sum(len(v) for v in _eq_history.values())
    print(f"  Equity sampler     → every {_EQ_SAMPLE_SECS}s "
          f"({_pts} point(s) restored from {EQUITY_HISTORY_FILE})")
    print("=" * 55)
    ChartServer(("", PORT), Handler).serve_forever()
