"""
Live chart server — candlestick chart with entry / SL / TP boxes in your browser.
Run:  python3 chart_server.py
Then: open  http://localhost:8888
"""

import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request

CRYPTO_STATE   = "crypto_state.json"
TEST_STATE     = "test_state.json"
STRATEGY_STATE = "strategy_state.json"
PORT           = 8888

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
    <option value="test">test_bot  (EMA 9/21)</option>
    <option value="smc">binance_bot  (SMC)</option>
    <option value="strat">tradingbot  (SMC/Alpaca)</option>
  </select>
  <select id="symSel"></select>
  <span id="bal"></span>
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
};
const STOCK_SYMS = ['SPY','QQQ','AAPL','NVDA','TSLA','GOOGL','META','MSFT'];
const ALL_SYMS = {
  test:  ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD'],
  smc:   ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD'],
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
}

function drawZones(times, entry, sl, tp, isLong, entryTime){
  if(!entry || !times.length){ greenZone.setData([]); redZone.setData([]); return; }

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
  if(entryTime){
    // Truncate microseconds to milliseconds — Python isoformat() emits 6-digit
    // fractions (.104002) which some JS engines parse as NaN, breaking findIndex.
    const cleanTime = entryTime.replace(/(\.\d{3})\d+/, '$1');
    const entryTs = Math.floor(new Date(cleanTime).getTime() / 1000);
    startIdx = times.findIndex(t => t >= entryTs);
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
  // Extend the zone 40 candles past the last real candle so the box always has
  // visual width even when a trade just opened (same look as TradingView position tool).
  const interval = times.length > 1 ? times[times.length-1] - times[times.length-2] : 300;
  const lastT    = times[times.length - 1];
  const future   = Array.from({length: 40}, (_, i) => lastT + interval * (i + 1));
  const zoneTimes = [...times.slice(startIdx), ...future];
  greenZone.setData(zoneTimes.map(t=>({ time:t, value: tp || entry })));
  redZone.setData(  zoneTimes.map(t=>({ time:t, value: sl || entry })));
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

  const bal=data?.balance, start=data?.start_balance;
  const pnl=bal&&start?(bal-start):null;
  document.getElementById('bal').textContent = bal
    ? `  Balance: $${bal.toLocaleString(undefined,{minimumFractionDigits:2})}${pnl!=null?`  |  P&L: ${pnl>=0?'+':''}$${pnl.toFixed(2)}`:''}`
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
        chart.timeScale().scrollToRealTime();
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
  const lastTime = lastCandleTimes.length ? lastCandleTimes[lastCandleTimes.length-1] : null;
  if(pos){
    const isLong = pos.side==='LONG';
    drawLines(pos.entry_price, pos.stop_loss, pos.take_profit);
    drawZones(lastCandleTimes, pos.entry_price, pos.stop_loss, pos.take_profit, isLong,
              pos.entry_time || sm?.entry_time);
    clearAnalysis();   // position lines take over; hide the pending-zone overlay
    drawTrend(sm?.trendline, lastTime);   // keep the structural trendline visible
  } else {
    clearLines(); drawZones([],null,null,null,true);
    drawAnalysis(sm);                      // show what the bot is watching
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
                       f"?granularity={gran}&limit=200")
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
    print("  (keep binance_bot.py + test_bot.py running too)")
    print("=" * 55)
    ChartServer(("", PORT), Handler).serve_forever()
