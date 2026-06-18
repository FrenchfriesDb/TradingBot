"""
Live chart server — candlestick chart with entry / SL / TP boxes in your browser.
Run:  python3 chart_server.py
Then: open  http://localhost:8888
"""

import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request

CRYPTO_STATE = "crypto_state.json"
TEST_STATE   = "test_state.json"
PORT         = 8888

# ── HTML page ─────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
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
  .g{color:#26a69a}.r{color:#ef5350}.w{color:#ffffff}.b{color:#2196f3}.y{color:#ffb74d}
  #chart-wrap{flex:1;position:relative}
  #chart{width:100%;height:100%}
  #no-pos{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
  #no-pos span{background:#1e222d;padding:16px 28px;border-radius:8px;color:#787b86;font-size:14px;border:1px solid #2a2e39}
  #pulse{position:fixed;bottom:10px;right:14px;font-size:11px;color:#363a45;transition:color .3s}
  #pulse.active{color:#26a69a}
</style>
</head>
<body>

<div id="header">
  <h1>⚡ DEBBIE-LA LIVE CHART</h1>
  <select id="botSel">
    <option value="test">test_bot  (EMA 9/21)</option>
    <option value="smc">binance_bot  (SMC)</option>
  </select>
  <select id="symSel"></select>
  <span id="bal"></span>
</div>

<div id="info">
  <div class="icard"><div class="ilabel">Side</div><div class="ival" id="i-side">—</div></div>
  <div class="icard"><div class="ilabel">Qty</div><div class="ival w" id="i-qty">—</div></div>
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
const ALL_SYMS = {
  test: ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD'],
  smc:  ['BTC/USD','ETH/USD','SOL/USD','DOGE/USD','XRP/USD','AVAX/USD','POL/USD','ADA/USD'],
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

function drawZones(times, entry, sl, tp, isLong){
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

  // Data values determine where the fill ends — always TP for #008000, SL for #ff0000.
  // BaselineSeries clips the fill precisely between (data value) and (baseline=entry).
  // Only draw over the last 60 candles (~5h of 5m bars) so zones don't
  // bleed all the way back through chart history.
  const zoneTimes = times.slice(-60);
  greenZone.setData(zoneTimes.map(t=>({ time:t, value: tp || entry })));
  redZone.setData(  zoneTimes.map(t=>({ time:t, value: sl || entry })));
}

new ResizeObserver(()=>{
  chart.applyOptions({width:chartEl.clientWidth, height:chartEl.clientHeight});
}).observe(chartEl);

// ── State ─────────────────────────────────────────────────────────────────────
let lastCandleTimes  = [];
let chartLoadedSym   = null;   // which symbol is currently fully loaded
let lastCandleCount  = 0;

function fmt(n, dec=2){ return n!=null ? '$'+n.toLocaleString(undefined,{minimumFractionDigits:dec,maximumFractionDigits:dec}) : '—'; }
function pct(a,b){ return b ? ((a-b)/b*100) : 0; }

function updateInfo(pos, curPrice){
  const noPos = document.getElementById('no-pos');
  if(!pos){ noPos.style.display='flex'; document.getElementById('no-pos-msg').textContent='No open position for this symbol'; return; }
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
  set('i-side',  isLong?'<span class="g">▲ LONG</span>':'<span class="r">▼ SHORT</span>');
  set('i-qty',   qty!=null ? Math.abs(+qty).toLocaleString(undefined,{maximumFractionDigits:4}) : '—');
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
  const pair = PAIRS[sym]; if(!pair) return [];
  try{
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
  const data  = bot==='test' ? state?.test : state?.smc;
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
    const symChanged = sym !== chartLoadedSym;
    if(symChanged || cdata.length !== lastCandleCount){
      candles.setData(cdata);
      if(symChanged){
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

  const botData = bot==='test' ? state?.test : state?.smc;
  const pos     = botData?.positions?.[sym]||null;
  const cur     = cdata.length ? cdata[cdata.length-1].close : pos?.current_price;

  if(pos){
    const isLong = pos.side==='LONG';
    drawLines(pos.entry_price, pos.stop_loss, pos.take_profit);
    drawZones(lastCandleTimes, pos.entry_price, pos.stop_loss, pos.take_profit, isLong);
  } else {
    clearLines(); drawZones([],null,null,null,true);
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = urlparse(self.path).path
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

        elif path == "/api/state":
            def load(f):
                if not os.path.exists(f): return None
                try:
                    with open(f) as fh: return json.load(fh)
                except Exception: return None
            body = json.dumps({"smc": load(CRYPTO_STATE), "test": load(TEST_STATE)}).encode()
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
