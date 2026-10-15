"""Builds the standalone HTML dashboard for the index-mode backtest,
embedding the backtest's own result JSON directly into the page (no
fetch needed, so it works opened straight from disk via file://).

Run scripts/backtest_index_mode.py FIRST -- this reads the
scripts/backtest_index_mode_result.json file that script writes."""
import json
import os

RESULT_PATH = "scripts/backtest_index_mode_result.json"
if not os.path.exists(RESULT_PATH):
    raise SystemExit(
        f"\n{RESULT_PATH} not found.\n"
        f"Run the backtest first:  python -m scripts.backtest_index_mode\n"
        f"Then run this script again.\n"
    )

with open(RESULT_PATH, encoding="utf-8") as f:
    full_result = json.load(f)

# The dashboard only needs settings/summary/trades (not the raw
# ~49k-point equity_curve -- the trade log's capital_after values
# already give the dashboard everything it needs to draw the equity
# and drawdown charts at trade-close resolution).
dashboard_data = {
    "settings": full_result["settings"],
    "summary": full_result["summary"],
    "trades": full_result["trades"],
}
data_json = json.dumps(dashboard_data, separators=(",", ":"))

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>NIFTY50 Index-Mode Backtest</title>
<style>
  :root {
    --bg: #0a0e14; --card: #12161f; --card-inset: #171c27; --border: #232936;
    --text: #e6e9ef; --text-muted: #8892a4; --accent: #4ade80; --danger: #f87171;
    --warn: #fbbf24; --blue: #60a5fa;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 20px 16px 60px;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .subtitle { color: var(--text-muted); font-size: 13px; margin-bottom: 18px; }
  .warning-box {
    background: rgba(251,191,36,0.08); border: 1px solid var(--warn); border-radius: 6px;
    padding: 12px 14px; font-size: 12.5px; line-height: 1.6; color: #fde68a; margin-bottom: 20px;
  }
  .warning-box b { color: var(--warn); }
  .stat-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 10px; margin-bottom: 20px;
  }
  .stat-tile {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 12px 14px;
  }
  .stat-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  .stat-value { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .stat-value.pos { color: var(--accent); }
  .stat-value.neg { color: var(--danger); }
  .stat-sub { font-size: 11px; color: var(--text-muted); margin-top: 4px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; margin-bottom: 20px;
  }
  .card h2 { font-size: 14px; margin: 0 0 12px; color: var(--text); }
  .chart-row { display: grid; grid-template-columns: 1fr; gap: 20px; margin-bottom: 20px; }
  @media (min-width: 860px) { .chart-row { grid-template-columns: 1fr 1fr; } }
  svg { width: 100%; height: auto; display: block; }
  .axis-label { fill: var(--text-muted); font-size: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th {
    position: sticky; top: 0; background: var(--card-inset); text-align: left;
    padding: 7px 8px; color: var(--text-muted); font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.03em; border-bottom: 1px solid var(--border);
    cursor: pointer; user-select: none;
  }
  thead th:hover { color: var(--text); }
  tbody td { padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tbody tr:hover { background: var(--card-inset); }
  .pnl-pos { color: var(--accent); }
  .pnl-neg { color: var(--danger); }
  .table-scroll { max-height: 560px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 10.5px; font-weight: 600; }
  .badge-call { background: rgba(74,222,128,0.15); color: var(--accent); }
  .badge-put { background: rgba(248,113,113,0.15); color: var(--danger); }
  .reason-tag { font-size: 10.5px; color: var(--text-muted); }
  .settings-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; font-size: 12px;
  }
  .settings-grid div { background: var(--card-inset); border-radius: 5px; padding: 8px 10px; }
  .settings-grid .k { color: var(--text-muted); font-size: 10.5px; text-transform: uppercase; }
  .settings-grid .v { font-size: 14px; font-weight: 600; margin-top: 2px; }
  .footer-note { font-size: 11px; color: var(--text-muted); margin-top: 8px; }
  .filter-row { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
  .filter-row button {
    background: var(--card-inset); color: var(--text); border: 1px solid var(--border);
    padding: 5px 12px; border-radius: 5px; font-size: 12px; cursor: pointer;
  }
  .filter-row button.active { background: var(--accent); color: #0a0e14; border-color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <h1>NIFTY50 Index-Mode Backtest</h1>
  <div class="subtitle" id="subtitleText"></div>

  <div class="warning-box" id="warningBox"></div>

  <div class="stat-grid" id="statGrid"></div>

  <div class="card">
    <h2>Live NIFTY 50 settings used (exact match, 2026-09-29)</h2>
    <div class="settings-grid" id="settingsGrid"></div>
  </div>

  <div class="chart-row">
    <div class="card">
      <h2>Equity Curve</h2>
      <svg id="equityChart" viewBox="0 0 560 260" preserveAspectRatio="none"></svg>
    </div>
    <div class="card">
      <h2>Drawdown</h2>
      <svg id="ddChart" viewBox="0 0 560 260" preserveAspectRatio="none"></svg>
    </div>
  </div>

  <div class="card">
    <h2>Exit Reason Breakdown</h2>
    <svg id="reasonChart" viewBox="0 0 560 80" preserveAspectRatio="none" style="height:70px;"></svg>
  </div>

  <div class="card">
    <h2>Full Trade Log (<span id="tradeCount"></span> trades)</h2>
    <div class="filter-row" id="filterRow">
      <button data-filter="all" class="active">All</button>
      <button data-filter="win">Wins</button>
      <button data-filter="loss">Losses</button>
      <button data-filter="long_call">Calls</button>
      <button data-filter="long_put">Puts</button>
    </div>
    <div class="table-scroll">
      <table id="tradeTable">
        <thead>
          <tr>
            <th data-key="idx">#</th>
            <th data-key="entry_time">Entry Time</th>
            <th data-key="exit_time">Exit Time</th>
            <th data-key="direction">Dir</th>
            <th data-key="entry_price">Entry</th>
            <th data-key="exit_price">Exit</th>
            <th data-key="lots">Lots</th>
            <th data-key="qty">Qty</th>
            <th data-key="hold_minutes">Hold (min)</th>
            <th data-key="peak_profit_pct">Peak %</th>
            <th data-key="exit_reason">Exit Reason</th>
            <th data-key="pnl_pct">P&amp;L %</th>
            <th data-key="pnl">P&amp;L (₹)</th>
            <th data-key="capital_after">Capital After</th>
          </tr>
        </thead>
        <tbody id="tradeTbody"></tbody>
      </table>
    </div>
    <div class="footer-note">Click column headers to sort. Use the filter buttons to isolate wins/losses/direction.</div>
  </div>
</div>

<script id="backtest-data" type="application/json">__DATA_JSON__</script>
<script>
const DATA = JSON.parse(document.getElementById('backtest-data').textContent);
const s = DATA.summary;
const trades = DATA.trades;
const settings = DATA.settings;
// Derived, not hardcoded -- so this dashboard is correct whether it's
// built from the Rs 30,000 options-style run or the Rs 20,00,000
// futures-lot run (or any other capital figure in the future).
const INITIAL_CAPITAL = Math.round((s.final_capital - s.total_pnl) * 100) / 100;
const initialCapitalStr = '₹' + INITIAL_CAPITAL.toLocaleString('en-IN');

document.getElementById('subtitleText').textContent =
  `${s.first_trade} → ${s.last_trade}  •  ${s.trade_count} trades  •  Initial capital ${initialCapitalStr}`;

// ---- Warning/methodology box (built from real data, not hardcoded) ----
(function() {
  const reasons = s.exit_reason_breakdown || {};
  const sessionEndCount = reasons.session_end_exit || 0;
  const sessionEndPct = s.trade_count > 0 ? Math.round(sessionEndCount / s.trade_count * 100) : 0;
  const lotSize = trades.length > 0 && trades[0].lots ? Math.round(trades[0].qty / trades[0].lots) : 65;
  const firstDate = s.first_trade.split(' ')[0];
  const lastDate = s.last_trade.split(' ')[0];

  document.getElementById('warningBox').innerHTML = `
    <b>What this actually tests:</b> the same signal stack (Hurst regime + Z-score momentum + chart structure confirmation)
    and the same risk engine (Hard SL, Time Stop, TSL ratchet) as your live options-buying system &mdash; but every entry/exit
    price here is the raw NIFTY50 <b>index</b> price, not an option premium, traded in real NSE futures lots of
    <b>${lotSize}</b> at full notional value (no margin/leverage modeled &mdash; a real margined futures account could likely
    hold more lots per rupee of capital than this backtest allows).<br/><br/>
    <b>SL/TSL % applied literally, unscaled</b> &mdash; per explicit instruction. A ${settings.hard_sl_pct}% Hard SL means the
    index itself must move ${settings.hard_sl_pct}% intraday, which is rare, so <b>${sessionEndPct}% of all trades here
    (${sessionEndCount}/${s.trade_count}) exit via forced session-end close (3:28&nbsp;PM)</b>, not SL/TSL. This is the real,
    honest behavior of your exact live settings applied to raw index price &mdash; not a bug.<br/><br/>
    <b>Also not modeled:</b> VWAP (Leg&nbsp;3) &mdash; not wired into any backtest, same gap as the live system's own backtest
    engine. OFI and PCR are excluded (no historical depth/chain snapshots retained). No brokerage, slippage, or transaction
    costs, and no real futures margin/SPAN rules. Data window: ${firstDate} to ${lastDate} &mdash; the full NIFTY50 candle
    history currently stored.
  `;
})();

// ---- Stat tiles ----
function fmtPct(v, digits=2) { return v === null || v === undefined ? '—' : v.toFixed(digits) + '%'; }
function fmtRupee(v) { return v === null || v === undefined ? '—' : (v >= 0 ? '+' : '') + '₹' + v.toLocaleString('en-IN', {maximumFractionDigits:2}); }
function posNeg(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : ''); }

const tiles = [
  { label: 'CAGR', value: fmtPct(s.cagr_pct), cls: posNeg(s.cagr_pct), sub: `over ${s.years_elapsed} yrs` },
  { label: 'Sharpe Ratio', value: s.sharpe_ratio !== null ? s.sharpe_ratio.toFixed(3) : '—', cls: posNeg(s.sharpe_ratio), sub: 'annualized, per-trade' },
  { label: 'Max Drawdown', value: fmtPct(s.max_drawdown_pct), cls: 'neg', sub: fmtRupee(-s.max_drawdown_rupees) },
  { label: 'Profit Factor', value: s.profit_factor !== null ? s.profit_factor.toFixed(3) : '—', cls: posNeg((s.profit_factor||0) - 1), sub: 'gross profit / gross loss' },
  { label: 'Expectancy / Trade', value: fmtRupee(s.expectancy_rupees_per_trade), cls: posNeg(s.expectancy_rupees_per_trade), sub: `win rate ${s.win_rate_pct}%` },
  { label: 'Total Trades', value: s.trade_count, cls: '', sub: `${s.win_count}W / ${s.loss_count}L` },
  { label: 'Total Return', value: fmtPct(s.total_return_pct), cls: posNeg(s.total_return_pct), sub: fmtRupee(s.total_pnl) },
  { label: 'Final Capital', value: '₹' + s.final_capital.toLocaleString('en-IN'), cls: posNeg(s.total_pnl), sub: `from ${initialCapitalStr}` },
];
document.getElementById('statGrid').innerHTML = tiles.map(t => `
  <div class="stat-tile">
    <div class="stat-label">${t.label}</div>
    <div class="stat-value ${t.cls}">${t.value}</div>
    <div class="stat-sub">${t.sub}</div>
  </div>
`).join('');

// ---- Settings grid ----
const settingsLabels = {
  hard_sl_pct: 'Hard SL %', time_stop_minutes: 'Time Stop (min)',
  tsl_activation_pct: 'TSL Activation %', tsl_base_trail_pct: 'TSL Base Trail %',
  tsl_k: 'TSL Tighten Rate (k)', tsl_floor_pct: 'TSL Floor %',
  risk_per_trade_pct: 'Risk/Trade %', min_confidence: 'Min Confidence',
  require_chart_confirmation: 'Chart Confirmation',
};
document.getElementById('settingsGrid').innerHTML = Object.entries(settingsLabels).map(([k, label]) => `
  <div><div class="k">${label}</div><div class="v">${typeof settings[k] === 'boolean' ? (settings[k] ? 'ON' : 'OFF') : settings[k]}</div></div>
`).join('');

// ---- Equity curve + drawdown (SVG, no external libs) ----
function buildLineChart(svgId, points, opts) {
  const svg = document.getElementById(svgId);
  const W = 560, H = 260, padL = 46, padR = 10, padT = 10, padB = 24;
  const xs = points.map((_, i) => i);
  const ys = points.map(p => p);
  const minY = opts.minYZero ? Math.min(0, ...ys) : Math.min(...ys);
  const maxY = Math.max(...ys);
  const yRange = (maxY - minY) || 1;
  const xScale = i => padL + (i / Math.max(1, xs.length - 1)) * (W - padL - padR);
  const yScale = v => padT + (1 - (v - minY) / yRange) * (H - padT - padB);

  let path = points.map((v, i) => `${i === 0 ? 'M' : 'L'} ${xScale(i).toFixed(1)} ${yScale(v).toFixed(1)}`).join(' ');
  let fillPath = path + ` L ${xScale(points.length - 1).toFixed(1)} ${yScale(minY).toFixed(1)} L ${xScale(0).toFixed(1)} ${yScale(minY).toFixed(1)} Z`;

  const zeroY = yScale(opts.zeroLine !== undefined ? opts.zeroLine : minY);
  const gridLines = [];
  const steps = 4;
  for (let i = 0; i <= steps; i++) {
    const val = minY + (yRange * i / steps);
    const y = yScale(val);
    gridLines.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W-padR}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-width="1"/>`);
    gridLines.push(`<text x="${padL - 6}" y="${(y+3).toFixed(1)}" text-anchor="end" class="axis-label">${opts.yFmt ? opts.yFmt(val) : val.toFixed(0)}</text>`);
  }

  svg.innerHTML = `
    <defs>
      <linearGradient id="${svgId}-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${opts.color}" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="${opts.color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${gridLines.join('')}
    <line x1="${padL}" y1="${zeroY.toFixed(1)}" x2="${W-padR}" y2="${zeroY.toFixed(1)}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3,3"/>
    <path d="${fillPath}" fill="url(#${svgId}-grad)" stroke="none"/>
    <path d="${path}" fill="none" stroke="${opts.color}" stroke-width="1.6"/>
  `;
}

const capitalSeries = [INITIAL_CAPITAL, ...trades.map(t => t.capital_after)];
buildLineChart('equityChart', capitalSeries, { color: '#4ade80', yFmt: v => '₹' + Math.round(v).toLocaleString('en-IN'), minYZero: false, zeroLine: INITIAL_CAPITAL });

let peak = INITIAL_CAPITAL, dd = [];
for (const v of capitalSeries) { peak = Math.max(peak, v); dd.push(-((peak - v) / peak * 100)); }
buildLineChart('ddChart', dd, { color: '#f87171', yFmt: v => v.toFixed(1) + '%', minYZero: true, zeroLine: 0 });

// ---- Exit reason bar ----
(function() {
  const reasons = s.exit_reason_breakdown;
  const svg = document.getElementById('reasonChart');
  const entries = Object.entries(reasons);
  const total = entries.reduce((a, [,v]) => a + v, 0);
  const colors = { session_end_exit: '#60a5fa', time_stop: '#fbbf24', hard_sl_hit: '#f87171', tsl_hit: '#4ade80' };
  let x = 10, W = 540, H = 70;
  const bars = entries.map(([k, v]) => {
    const w = (v / total) * W;
    const rect = `<rect x="${x.toFixed(1)}" y="20" width="${w.toFixed(1)}" height="24" fill="${colors[k] || '#8892a4'}" rx="3"/>`;
    const label = `<text x="${(x + w/2).toFixed(1)}" y="60" text-anchor="middle" class="axis-label">${k.replace('_',' ')} (${v})</text>`;
    x += w;
    return rect + label;
  }).join('');
  svg.innerHTML = bars;
})();

// ---- Trade table ----
let currentFilter = 'all';
let sortKey = 'idx';
let sortDir = 1;

function getFilteredSorted() {
  let rows = trades.map((t, i) => ({...t, idx: i + 1}));
  if (currentFilter === 'win') rows = rows.filter(t => t.pnl > 0);
  else if (currentFilter === 'loss') rows = rows.filter(t => t.pnl <= 0);
  else if (currentFilter === 'long_call' || currentFilter === 'long_put') rows = rows.filter(t => t.direction === currentFilter);
  rows.sort((a, b) => (a[sortKey] > b[sortKey] ? 1 : a[sortKey] < b[sortKey] ? -1 : 0) * sortDir);
  return rows;
}

function renderTable() {
  const rows = getFilteredSorted();
  document.getElementById('tradeCount').textContent = rows.length;
  document.getElementById('tradeTbody').innerHTML = rows.map(t => `
    <tr>
      <td>${t.idx}</td>
      <td>${t.entry_time}</td>
      <td>${t.exit_time}</td>
      <td><span class="badge ${t.direction === 'long_call' ? 'badge-call' : 'badge-put'}">${t.direction === 'long_call' ? 'CALL' : 'PUT'}</span></td>
      <td>${t.entry_price.toLocaleString('en-IN')}</td>
      <td>${t.exit_price.toLocaleString('en-IN')}</td>
      <td>${t.lots !== undefined ? t.lots : '—'}</td>
      <td>${t.qty}</td>
      <td>${t.hold_minutes}</td>
      <td>${t.peak_profit_pct}%</td>
      <td class="reason-tag">${t.exit_reason.replace('_',' ')}</td>
      <td class="${t.pnl_pct >= 0 ? 'pnl-pos' : 'pnl-neg'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct}%</td>
      <td class="${t.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${fmtRupee(t.pnl)}</td>
      <td>₹${t.capital_after.toLocaleString('en-IN')}</td>
    </tr>
  `).join('');
}

document.querySelectorAll('#filterRow button').forEach(btn => {
  btn.addEventListener('click', () => {
    currentFilter = btn.dataset.filter;
    document.querySelectorAll('#filterRow button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderTable();
  });
});

document.querySelectorAll('#tradeTable thead th').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = 1; }
    renderTable();
  });
});

renderTable();
</script>
</body>
</html>
"""

html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
with open("scripts/nifty_index_backtest_dashboard.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Wrote scripts/nifty_index_backtest_dashboard.html", len(html), "bytes")
