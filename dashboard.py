"""Single-page, no-build HTML dashboard - fetches /report and /status client-side."""

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Spread Arb Front-Test</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #0b0e14; color: #e6e6e6; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 28px; }
  .card { background: #161a23; border-radius: 8px; padding: 14px 16px; border: 1px solid #232838; }
  .card .label { color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 4px; }
  .pos { color: #4ade80; } .neg { color: #f87171; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 28px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #232838; }
  th { color: #888; font-weight: 500; text-transform: uppercase; font-size: 11px; }
  h2 { font-size: 15px; color: #ccc; margin: 28px 0 10px; }
  .refresh { color: #666; font-size: 12px; }
  .banner { background: #2a1f0e; border: 1px solid #6b4a12; color: #f0c674; border-radius: 6px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px; display: none; }
</style>
</head>
<body>
  <h1>Cross-Exchange Spread Arb &mdash; Live Front-Test</h1>
  <div class="sub" id="subtitle">loading...</div>
  <div class="banner" id="bn_banner">Binance futures API is geo-blocked from this host - BN-leg pairs (HL-BN, PAC-BN) are paused. HL-PAC and Ostium pairs keep trading normally on clean data. See README for the fix (change Railway region).</div>

  <div class="grid" id="kpis"></div>

  <h2>Open Positions</h2>
  <table id="open_table"><thead><tr><th>Symbol</th><th>Pair</th><th>Direction</th><th>Notional</th><th>Leverage</th><th>Kind</th></tr></thead><tbody></tbody></table>

  <h2>Recent Closed Trades</h2>
  <table id="trades_table"><thead><tr><th>Symbol</th><th>Pair</th><th>Exit Reason</th><th>Hold (h)</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <h2>P&amp;L by Symbol</h2>
  <table id="symbol_table"><thead><tr><th>Symbol</th><th>Trades</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <div class="refresh">Auto-refreshes every 15s &middot; <a href="/report" style="color:#666">raw JSON report</a></div>

<script>
function fmtUsd(v) { return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); }
function cls(v) { return v >= 0 ? 'pos' : 'neg'; }

async function refresh() {
  const r = await fetch('/report'); const d = await r.json();

  document.getElementById('subtitle').textContent =
    `Running ${d.days_running} days  |  ${d.total_trades} trades  |  win rate ${d.win_rate_pct ?? '-'}%`;

  document.getElementById('kpis').innerHTML = `
    <div class="card"><div class="label">Equity</div><div class="value">$${d.equity_usd.toFixed(2)}</div></div>
    <div class="card"><div class="label">Total Return</div><div class="value ${cls(d.total_return_pct)}">${d.total_return_pct.toFixed(2)}%</div></div>
    <div class="card"><div class="label">Realized PnL</div><div class="value ${cls(d.realized_pnl_usd)}">${fmtUsd(d.realized_pnl_usd)}</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="value neg">${d.max_drawdown_pct.toFixed(2)}%</div></div>
    <div class="card"><div class="label">Worst Day</div><div class="value ${cls(d.worst_day?.pnl_usd ?? 0)}">${d.worst_day ? fmtUsd(d.worst_day.pnl_usd) : '-'}</div></div>
    <div class="card"><div class="label">Worst Week</div><div class="value ${cls(d.worst_week?.pnl_usd ?? 0)}">${d.worst_week ? fmtUsd(d.worst_week.pnl_usd) : '-'}</div></div>
  `;

  const st = await (await fetch('/status')).json();
  document.getElementById('bn_banner').style.display = st.binance_futures_blocked ? 'block' : 'none';
  document.querySelector('#open_table tbody').innerHTML = st.open_positions.map(p => `
    <tr><td>${p.symbol}</td><td>${p.pair}</td><td>${p.direction}</td><td>$${p.notional_usd.toFixed(0)}</td><td>${p.leverage}x</td><td>${p.kind}</td></tr>
  `).join('') || '<tr><td colspan="6" style="color:#666">none open</td></tr>';

  document.querySelector('#trades_table tbody').innerHTML = st.recent_trades.map(t => `
    <tr><td>${t.symbol}</td><td>${t.pair}</td><td>${t.exit_reason ?? '-'}</td><td>${t.hold_hours.toFixed(2)}</td>
    <td class="${cls(t.net_pnl_usd)}">${fmtUsd(t.net_pnl_usd)}</td></tr>
  `).join('') || '<tr><td colspan="5" style="color:#666">no closed trades yet</td></tr>';

  document.querySelector('#symbol_table tbody').innerHTML = Object.entries(d.by_symbol).map(([sym, v]) => `
    <tr><td>${sym}</td><td>${v.n_trades}</td><td class="${cls(v.net_pnl_usd)}">${fmtUsd(v.net_pnl_usd)}</td></tr>
  `).join('') || '<tr><td colspan="3" style="color:#666">no closed trades yet</td></tr>';
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
