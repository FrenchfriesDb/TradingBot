"""
Renders a candlestick PNG for one completed trade — attached to each Google Sheets
ledger row via github_chart_uploader (Drive hosting was ruled out: service accounts
have zero storage quota and can't accept ownership transfers).

Primary path: the EXACT same TradingView lightweight-charts engine and styling as
the live chart_server.py dashboard, rendered in headless Chrome (playwright,
channel="chrome" — uses the installed browser, no download) and screenshotted:
same candle colors, ENTRY/STOP/TARGET axis labels, green profit / red risk zones
from the entry candle onward, and an entry marker arrow.

Fallback: a matplotlib/mplfinance approximation if playwright or Chrome is
unavailable. Never raises — returns None on total failure so a render error can
never block a trade close or crash the trading loop.
"""

import json
import os
import shutil
import tempfile

_VENDOR_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "vendor", "lightweight-charts.standalone.production.js")

_TV_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;padding:0;background:#131722;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
  #wrap{{position:relative;width:1180px;height:560px}}
  #chart{{position:absolute;inset:0}}
  #badge{{position:absolute;top:10px;left:12px;z-index:10;color:#d1d4dc;font-size:15px;font-weight:700;
         background:rgba(30,34,45,.85);padding:6px 12px;border-radius:6px;border:1px solid #2a2e39}}
  #badge span{{color:#787b86;font-weight:400;font-size:12px;margin-left:8px}}
</style></head><body>
<div id="wrap"><div id="badge">{badge}<span>{badge_sub}</span></div><div id="chart"></div></div>
<script>{lib_js}</script>
<script>
const DATA   = {candles_json};
const ENTRY  = {entry};
const SL     = {sl};
const TP     = {tp};
const E_TIME = {entry_ts};   // snapped to a candle time, or null
const IS_LONG = {is_long};

const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
  width: 1180, height: 560,
  layout: {{ background:{{color:'#131722'}}, textColor:'#d1d4dc' }},
  grid:   {{ vertLines:{{color:'#1e222d'}}, horzLines:{{color:'#1e222d'}} }},
  rightPriceScale: {{ borderColor:'#2a2e39' }},
  timeScale: {{ borderColor:'#2a2e39', timeVisible:true, secondsVisible:false }},
}});

function priceFmtFor(p){{
  p = Math.abs(p || 0);
  if(p >= 1000) return {{precision:2, minMove:0.01}};
  if(p >= 1)    return {{precision:3, minMove:0.001}};
  if(p >= 0.1)  return {{precision:4, minMove:0.0001}};
  if(p >= 0.01) return {{precision:5, minMove:0.00001}};
  return {{precision:7, minMove:0.0000001}};
}}
const fmt = priceFmtFor(ENTRY);

// green profit zone / red risk zone between entry and TP/SL, from entry onward —
// fills symmetric on both sides of the baseline so LONG and SHORT both render
const zoneTimes = E_TIME ? DATA.filter(c => c.time >= E_TIME) : DATA;
const greenZone = chart.addBaselineSeries({{
  lineWidth:0, topLineColor:'rgba(0,0,0,0)', bottomLineColor:'rgba(0,0,0,0)',
  topFillColor1:'rgba(38,166,154,0.20)', topFillColor2:'rgba(38,166,154,0.05)',
  bottomFillColor1:'rgba(38,166,154,0.05)', bottomFillColor2:'rgba(38,166,154,0.20)',
  baseValue:{{type:'price', price:ENTRY}},
  priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
}});
greenZone.setData(zoneTimes.map(c => ({{time:c.time, value:TP}})));
const redZone = chart.addBaselineSeries({{
  lineWidth:0, topLineColor:'rgba(0,0,0,0)', bottomLineColor:'rgba(0,0,0,0)',
  topFillColor1:'rgba(239,83,80,0.20)', topFillColor2:'rgba(239,83,80,0.05)',
  bottomFillColor1:'rgba(239,83,80,0.05)', bottomFillColor2:'rgba(239,83,80,0.20)',
  baseValue:{{type:'price', price:ENTRY}},
  priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
}});
redZone.setData(zoneTimes.map(c => ({{time:c.time, value:SL}})));

const candles = chart.addCandlestickSeries({{
  upColor:'#0ced00', downColor:'#ff3c38',
  borderUpColor:'#0cee00', borderDownColor:'#ff3e3b',
  wickUpColor:'#0acb00', wickDownColor:'#ff4845',
  priceFormat: {{type:'price', precision:fmt.precision, minMove:fmt.minMove}},
}});
candles.setData(DATA);

candles.createPriceLine({{price:ENTRY, color:'#2196f3', lineWidth:2,
  lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'ENTRY'}});
candles.createPriceLine({{price:SL, color:'#ef5350', lineWidth:2,
  lineStyle:LightweightCharts.LineStyle.Solid, axisLabelVisible:true, title:'STOP'}});
candles.createPriceLine({{price:TP, color:'#26a69a', lineWidth:2,
  lineStyle:LightweightCharts.LineStyle.Solid, axisLabelVisible:true, title:'TARGET'}});

if (E_TIME) {{
  candles.setMarkers([{{time:E_TIME,
    position: IS_LONG ? 'belowBar' : 'aboveBar',
    color:'#2196f3', shape: IS_LONG ? 'arrowUp' : 'arrowDown', text:'ENTRY'}}]);
}}

chart.timeScale().fitContent();
requestAnimationFrame(() => requestAnimationFrame(() => {{ window.__done = true; }}));
</script></body></html>"""


def _render_tv(df, ticker, side, entry_price, stop_loss, take_profit,
                leverage, timeframe, entry_time):
    """Dashboard-identical render via lightweight-charts in headless Chrome.
    Returns a temp PNG path or None (missing playwright/Chrome/vendor lib, or
    any render error) — caller falls back to matplotlib."""
    try:
        from playwright.sync_api import sync_playwright

        with open(_VENDOR_LIB, encoding="utf-8") as f:
            lib_js = f.read()

        cols = {c.lower(): c for c in df.columns}
        candle_rows = []
        for ts, row in df.iterrows():
            t = int(ts.timestamp()) if ts.tzinfo else int(ts.value // 10**9)
            candle_rows.append({"time": t, "open": float(row[cols["open"]]),
                                 "high": float(row[cols["high"]]),
                                 "low": float(row[cols["low"]]),
                                 "close": float(row[cols["close"]])})

        entry_ts = "null"
        if entry_time is not None:
            e = int(entry_time.timestamp())
            snapped = [c["time"] for c in candle_rows if c["time"] <= e]
            if snapped:
                entry_ts = str(snapped[-1])

        lev_tag = f"{leverage}x" if leverage and leverage > 1 else ""
        html = _TV_HTML.format(
            badge=f"{ticker} {side} {lev_tag}".strip(),
            badge_sub=timeframe or "",
            lib_js=lib_js,
            candles_json=json.dumps(candle_rows),
            entry=entry_price, sl=stop_loss, tp=take_profit,
            entry_ts=entry_ts,
            is_long="true" if side == "LONG" else "false",
        )

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 560},
                                     device_scale_factor=2)
            page.set_content(html, wait_until="load")
            page.wait_for_function("window.__done === true", timeout=10000)
            page.screenshot(path=tmp.name)
            browser.close()
        return tmp.name
    except Exception as e:
        print(f"[CHART] TV render unavailable for {ticker} ({e}) — falling back to matplotlib")
        return None

_TVWEB_OVERLAY_JS = """(args) => {
  const out = [];
  try {
    const raw = window.chartWidget.model().model();
    const s = raw.mainSeries();
    const ps = s.priceScale();
    const ts = raw.timeScale();

    // widen the visible window so the whole trade (entry -> now) is on screen
    if (args.entryTs) {
      try {
        const idx = ts.timePointToIndex(args.entryTs);
        const r = ts.visibleBarsStrictRange();
        if (idx != null && isFinite(idx) && r && idx < r.firstBar()) {
          const span = r.lastBar() - idx;
          ts.zoomToBarsRange(idx - Math.max(10, span * 0.15), r.lastBar() + Math.max(5, span * 0.05));
          out.push('zoomed to include entry');
        }
      } catch (e) { out.push('zoom skip: ' + e.message); }
    }
    return out.join('; ');
  } catch (e) { return 'zoom fatal: ' + e.message; }
}"""

_TVWEB_DRAW_JS = """(args) => {
  const out = [];
  const raw = window.chartWidget.model().model();
  const s = raw.mainSeries();
  const ps = s.priceScale();
  const ts = raw.timeScale();

  let pane = null, best = 0;
  for (const c of document.querySelectorAll('canvas')) {
    const r = c.getBoundingClientRect();
    if (r.width * r.height > best) { best = r.width * r.height; pane = r; }
  }
  if (!pane) return 'no pane canvas';

  let p2y = null;
  try {
    let fv = (typeof s.firstValue === 'function') ? s.firstValue() : null;
    if (fv && typeof fv === 'object' && 'value' in fv) fv = fv.value;
    if (isFinite(ps.priceToCoordinate(args.entry, fv))) p2y = (p) => ps.priceToCoordinate(p, fv);
  } catch (e) {}
  if (!p2y) { try { if (isFinite(ps.priceToCoordinate(args.entry))) p2y = (p) => ps.priceToCoordinate(p); } catch (e) {} }
  if (!p2y) {
    const pr = ps.priceRange();
    const mn = pr.minValue ? pr.minValue() : pr.min, mx = pr.maxValue ? pr.maxValue() : pr.max;
    p2y = (p) => pane.height * (1 - (p - mn) / (mx - mn));
  }

  const fmt = (p) => p >= 1 ? p.toFixed(2) : p.toFixed(4);
  const specs = [
    {price: args.tp,    color: '#26a69a', dashed: false, label: 'TARGET'},
    {price: args.entry, color: '#2196f3', dashed: true,  label: 'ENTRY'},
    {price: args.sl,    color: '#ef5350', dashed: false, label: 'STOP'},
  ];
  for (const sp of specs) sp.y = pane.top + p2y(sp.price);

  // de-overlap the pills (labels only — lines stay at their true price)
  const sorted = [...specs].sort((a, b) => a.y - b.y);
  sorted.forEach(sp => { sp.pillY = sp.y; });
  for (let i = 1; i < sorted.length; i++)
    if (sorted[i].pillY - sorted[i - 1].pillY < 20) sorted[i].pillY = sorted[i - 1].pillY + 20;

  for (const sp of specs) {
    const inView = sp.y > pane.top + 2 && sp.y < pane.bottom - 2;
    if (inView) {
      const line = document.createElement('div');
      line.style.cssText = `position:fixed;left:${pane.left}px;width:${pane.width}px;top:${sp.y}px;` +
        `border-top:2px ${sp.dashed ? 'dashed' : 'solid'} ${sp.color};z-index:9999;pointer-events:none;`;
      document.body.appendChild(line);
    }
    const py = Math.min(Math.max(sp.pillY, pane.top + 10), pane.bottom - 10);
    const pill = document.createElement('div');
    pill.textContent = `${sp.label} ${fmt(sp.price)}`;
    pill.style.cssText = `position:fixed;right:${window.innerWidth - pane.right + 6}px;top:${py}px;` +
      `transform:translateY(-50%);background:${sp.color};color:#fff;` +
      `font:700 11px -apple-system,BlinkMacSystemFont,sans-serif;padding:2px 7px;` +
      `border-radius:4px;z-index:10000;pointer-events:none;`;
    document.body.appendChild(pill);
    out.push(`${sp.label} y=${sp.y.toFixed(0)}${inView ? '' : ' (offscreen)'}`);
  }

  if (args.entryTs) {
    try {
      const idx = ts.timePointToIndex(args.entryTs);
      if (idx != null && isFinite(idx)) {
        const x = pane.left + ts.indexToCoordinate(idx);
        if (x > pane.left && x < pane.right) {
          const v = document.createElement('div');
          v.style.cssText = `position:fixed;left:${x}px;top:${pane.top}px;height:${pane.height}px;` +
            `border-left:1px dotted #2196f3;z-index:9998;pointer-events:none;`;
          document.body.appendChild(v);
          out.push('entry vline x=' + x.toFixed(0));
        }
      }
    } catch (e) { out.push('vline err: ' + e.message); }
  }

  const badge = document.createElement('div');
  badge.textContent = args.badge;
  badge.style.cssText = `position:fixed;top:${pane.top + 30}px;left:${pane.left + 8}px;` +
    `background:rgba(33,150,243,.9);color:#fff;font:700 12px -apple-system,sans-serif;` +
    `padding:3px 9px;border-radius:4px;z-index:10000;pointer-events:none;`;
  document.body.appendChild(badge);
  return out.join('; ');
}"""


def render_stock_tradingview(ticker, side, entry_price, stop_loss, take_profit,
                              leverage=1, entry_time=None, exit_time=None):
    """STOCK BOT ONLY (crypto keeps the lightweight-charts renderer): screenshots
    a real TradingView chart (widgetembed page, real market data, their candles/
    axes/legend) with ENTRY/STOP/TARGET lines and an entry-time marker overlaid
    using TradingView's own read-only coordinate APIs — no account, no drawings
    saved anywhere. Interval auto-picks from trade duration so the whole trade
    fits. Returns a temp PNG path, or None on any failure (network, headless
    Chrome missing, TradingView internals changed) so callers can fall back to
    render_trade_chart(). Runs playwright in a dedicated thread because lumibot's
    strategy thread may host an asyncio loop, which the sync API refuses."""
    import threading

    result = [None]

    def _work():
        try:
            from datetime import timezone as _tz
            from playwright.sync_api import sync_playwright

            dur_h = 6.0
            if entry_time is not None and exit_time is not None:
                dur_h = max(0.1, (exit_time - entry_time).total_seconds() / 3600)
            if dur_h <= 48:
                interval, tf_label = "15", "15m"
            elif dur_h <= 240:
                interval, tf_label = "60", "1h"
            else:
                interval, tf_label = "D", "1D"

            entry_ts = None
            if entry_time is not None:
                et = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=_tz.utc)
                entry_ts = int(et.timestamp())

            url = ("https://s.tradingview.com/widgetembed/?symbol=" + ticker +
                   f"&interval={interval}&theme=dark&style=1&timezone=America%2FNew_York"
                   "&locale=en&hide_side_toolbar=1&hide_top_toolbar=1"
                   "&allow_symbol_change=0&hideideas=1&saveimage=0")

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            lev_tag = f" {leverage}x" if leverage and leverage > 1 else ""
            args = {"entry": float(entry_price), "sl": float(stop_loss),
                    "tp": float(take_profit), "entryTs": entry_ts,
                    "badge": f"{side}{lev_tag} · {tf_label}"}

            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                # 1600x900 @2x -> 3200x1800 PNG: finer candles/text, closer to a
                # full-window TradingView look than the default embed size
                page = browser.new_page(viewport={"width": 1600, "height": 900},
                                         device_scale_factor=2)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function(
                    "() => { try { return window.chartWidget.model().model()"
                    ".timeScale().visibleBarsStrictRange() !== null; } catch(e) { return false; } }",
                    timeout=25000)
                page.wait_for_timeout(2500)          # let candles/volume paint
                page.evaluate(_TVWEB_OVERLAY_JS, args)
                page.wait_for_timeout(900)           # zoom animation settle
                diag = page.evaluate(_TVWEB_DRAW_JS, args)
                print(f"[CHART] TradingView overlay ({ticker}): {diag}")
                page.wait_for_timeout(400)
                page.screenshot(path=tmp.name)
                browser.close()
            result[0] = tmp.name
        except Exception as e:
            print(f"[CHART] TradingView snapshot failed for {ticker} ({e}) — using fallback renderer")

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=90)
    return result[0]


_TV_BG    = "#131722"   # TradingView dark chart background
_TV_GRID  = "#1e222d"
_TV_TEXT  = "#d1d4dc"
_TV_UP    = "#26a69a"
_TV_DOWN  = "#ef5350"
_TV_ENTRY = "#2962ff"
_MAX_BARS = 150         # readability cap — more than this turns candles to mush


def _to_index_tz(ts, index):
    """Convert an aware/naive datetime to the df index's timezone convention so
    mplfinance vlines land in the right spot (ccxt frames are naive-UTC, lumibot
    frames are usually tz-aware ET)."""
    if ts is None:
        return None
    try:
        idx_tz = getattr(index, "tz", None)
        if idx_tz is not None:
            return ts.astimezone(idx_tz) if ts.tzinfo else ts.tz_localize(idx_tz)
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    except Exception:
        return None


def render_trade_chart(df, ticker, side, entry_price, stop_loss, take_profit,
                        leverage=1, timeframe="", entry_time=None):
    """df: OHLCV DataFrame with a DatetimeIndex and open/high/low/close columns
    (any case). Returns a path to a temp PNG, or None if df is empty/invalid and
    every renderer fails. Tries the dashboard-identical lightweight-charts render
    first, falls back to matplotlib. All heavy imports are lazy so a machine
    missing them can still run the bots — trades just log without a chart."""
    if df is None or len(df) == 0:
        return None
    cols = {c.lower(): c for c in df.columns}
    if not all(c in cols for c in ["open", "high", "low", "close"]):
        return None
    path = _render_tv(df, ticker, side, entry_price, stop_loss, take_profit,
                       leverage, timeframe, entry_time)
    if path:
        return path
    return _render_mpl(df, ticker, side, entry_price, stop_loss, take_profit,
                        leverage, timeframe, entry_time)


def _render_mpl(df, ticker, side, entry_price, stop_loss, take_profit,
                 leverage=1, timeframe="", entry_time=None):
    """Matplotlib approximation of the dashboard look — fallback only."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import mplfinance as mpf

        cols = {c.lower(): c for c in df.columns}
        required = ["open", "high", "low", "close"]
        if not all(c in cols for c in required):
            return None
        plot_df = df.rename(columns={cols[c]: c.capitalize() for c in required})[
            [c.capitalize() for c in required]
        ]
        if len(plot_df) > _MAX_BARS:
            plot_df = plot_df.iloc[-_MAX_BARS:]

        mc = mpf.make_marketcolors(up=_TV_UP, down=_TV_DOWN, edge="inherit", wick="inherit")
        style = mpf.make_mpf_style(
            marketcolors=mc, facecolor=_TV_BG, figcolor=_TV_BG,
            edgecolor=_TV_GRID, gridcolor=_TV_GRID, gridstyle="-",
            rc={"axes.labelcolor": _TV_TEXT, "xtick.color": _TV_TEXT,
                "ytick.color": _TV_TEXT, "text.color": _TV_TEXT, "font.size": 9},
        )

        hlines = dict(hlines=[take_profit, entry_price, stop_loss],
                      colors=[_TV_UP, _TV_ENTRY, _TV_DOWN],
                      linestyle="--", linewidths=1.1, alpha=0.9)

        # vertical dotted marker at entry time, only if it falls inside the window
        vlines = None
        entry_ts = _to_index_tz(entry_time, plot_df.index)
        if entry_ts is not None and plot_df.index[0] <= entry_ts <= plot_df.index[-1]:
            vlines = dict(vlines=[entry_ts], colors=["#787b86"], linestyle=":", linewidths=1.0)

        # make sure SL/TP/entry are always in view even when price never came near
        lo = min(float(plot_df["Low"].min()), stop_loss, take_profit, entry_price)
        hi = max(float(plot_df["High"].max()), stop_loss, take_profit, entry_price)
        pad = (hi - lo) * 0.07 or max(hi, 1) * 0.01

        kwargs = dict(type="candle", style=style, hlines=hlines, volume=False,
                      ylim=(lo - pad, hi + pad), figsize=(11, 5.5), returnfig=True,
                      datetime_format="%m-%d %H:%M", xrotation=0)
        if vlines:
            kwargs["vlines"] = vlines
        fig, axlist = mpf.plot(plot_df, **kwargs)
        ax = axlist[0]

        lev_tag = f"  {leverage}x" if leverage and leverage > 1 else ""
        tf_tag  = f"   ·   {timeframe}" if timeframe else ""
        ax.set_title(f"{ticker}  {side}{lev_tag}{tf_tag}", color=_TV_TEXT,
                     fontsize=13, fontweight="bold", loc="left", pad=12)

        # right-edge price tags, TradingView-style colored pills — nudged apart
        # vertically when two levels sit close (e.g. SL trailed to break-even
        # right under entry) so no pill ever hides another
        span = (hi + pad) - (lo - pad)
        min_gap = span * 0.055
        tags = sorted([(take_profit, _TV_UP, "TP"), (entry_price, _TV_ENTRY, "ENTRY"),
                       (stop_loss, _TV_DOWN, "SL")], key=lambda t: t[0])
        ys = [t[0] for t in tags]
        for i in range(1, len(ys)):
            if ys[i] - ys[i - 1] < min_gap:
                ys[i] = ys[i - 1] + min_gap
        overshoot = ys[-1] - (hi + pad - min_gap * 0.5)
        if overshoot > 0:
            ys = [y - overshoot for y in ys]
        for (price, color, label), y in zip(tags, ys):
            ax.text(1.006, y, f" {label} {price:g} ",
                    transform=ax.get_yaxis_transform(), color="white",
                    fontsize=8.5, fontweight="bold", va="center", ha="left",
                    bbox=dict(boxstyle="round,pad=0.28", fc=color, ec="none"))

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        fig.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor=_TV_BG)
        plt.close(fig)
        return tmp.name
    except Exception as e:
        print(f"[CHART] Render failed for {ticker}: {e}")
        return None


def save_chart_locally(temp_path, filename, charts_dir="charts"):
    """Moves a temp chart PNG (from render_trade_chart) into a permanent local
    folder. Returns the relative path on success, None on any failure — never
    raises, so a disk error can't block the ledger row from being written."""
    if not temp_path:
        return None
    try:
        os.makedirs(charts_dir, exist_ok=True)
        dest = os.path.join(charts_dir, filename)
        shutil.move(temp_path, dest)
        return dest
    except Exception as e:
        print(f"[CHART] Local save failed: {e}")
        return None
