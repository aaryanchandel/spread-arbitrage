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
  .live-banner { background: #2a0e0e; border: 1px solid #7a1f1f; color: #f87171; border-radius: 6px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px; display: none; font-weight: 600; }
  .live-badge { background: #7a1f1f; color: #fff; border-radius: 4px; padding: 1px 6px; font-size: 10px; font-weight: 700; letter-spacing: .03em; margin-left: 6px; vertical-align: middle; }
</style>
</head>
<body>
  <h1>Cross-Exchange Spread Arb &mdash; Live Front-Test</h1>
  <div class="sub" id="subtitle">loading...</div>
  <div class="live-banner" id="live_banner" style="display:none"></div>
  <div class="banner" id="cooldown_banner" style="display:none"></div>

  <div class="grid" id="kpis"></div>

  <h2>Open Positions</h2>
  <table id="open_table"><thead><tr><th>Symbol</th><th>Pair</th><th>Direction</th><th>Entry Time</th><th>Long Px</th><th>Short Px</th><th>Entry Spread</th><th>Notional</th><th>Leverage</th><th>Unrealized PnL</th></tr></thead><tbody></tbody></table>

  <h2>Recent Closed Trades</h2>
  <table id="trades_table"><thead><tr><th>Symbol</th><th>Pair</th><th>Entry Time</th><th>Exit Time</th><th>Long Px</th><th>Short Px</th><th>Entry Spread</th><th>Exit Reason</th><th>Hold (h)</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <h2>P&amp;L by Exchange-Pair</h2>
  <table id="pair_table"><thead><tr><th>Pair</th><th>Trades</th><th>Win Rate</th><th>Avg Win</th><th>Avg Loss</th><th>Profit Factor</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <h2>P&amp;L by Exchange</h2>
  <p style="color:#666;font-size:12px;margin:-4px 0 8px">Credits both legs of every pair trade to each exchange involved - shows which exchanges tend to show up in the most/least profitable arbs, not a true per-leg PnL split.</p>
  <table id="exchange_table"><thead><tr><th>Exchange</th><th>Trades</th><th>Win Rate</th><th>Avg Win</th><th>Avg Loss</th><th>Profit Factor</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <h2>P&amp;L by Symbol</h2>
  <table id="symbol_table"><thead><tr><th>Symbol</th><th>Trades</th><th>Net PnL</th></tr></thead><tbody></tbody></table>

  <div class="refresh">Auto-refreshes every 15s &middot; <a href="/report" style="color:#666">raw JSON report</a></div>

<script>
function fmtUsd(v) { return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); }
function cls(v) { return v >= 0 ? 'pos' : 'neg'; }
function fmtPx(v) {
  if (v == null) return '-';
  const decimals = v >= 100 ? 2 : v >= 1 ? 4 : 6;
  return v.toFixed(decimals);
}
function fmtPct(v) { return v == null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(4) + '%'; }
function fmtTime(unixSecs) {
  if (unixSecs == null) return '-';
  return new Date(unixSecs * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}

async function refresh() {
  const r = await fetch('/report'); const d = await r.json();

  document.getElementById('subtitle').textContent =
    `Running ${d.days_running} days  |  ${d.total_trades} trades  |  win rate ${d.win_rate_pct ?? '-'}%`;

  const st = await (await fetch('/status')).json();

  document.getElementById('kpis').innerHTML = `
    <div class="card"><div class="label">Equity (realized)</div><div class="value">$${d.equity_usd.toFixed(2)}</div></div>
    <div class="card"><div class="label">Net Realized PnL</div><div class="value ${cls(d.realized_pnl_usd)}">${fmtUsd(d.realized_pnl_usd)}</div></div>
    <div class="card"><div class="label">Total Return</div><div class="value ${cls(d.total_return_pct)}">${d.total_return_pct.toFixed(2)}%</div></div>
    <div class="card"><div class="label">Unrealized PnL</div><div class="value ${cls(st.unrealized_pnl_usd)}">${fmtUsd(st.unrealized_pnl_usd)}</div></div>
    <div class="card"><div class="label">Equity incl. Unrealized</div><div class="value">$${st.equity_with_unrealized_usd.toFixed(2)}</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="value neg">${d.max_drawdown_pct.toFixed(2)}%</div></div>
    <div class="card"><div class="label">Worst Day</div><div class="value ${cls(d.worst_day?.pnl_usd ?? 0)}">${d.worst_day ? fmtUsd(d.worst_day.pnl_usd) : '-'}</div></div>
    <div class="card"><div class="label">Worst Week</div><div class="value ${cls(d.worst_week?.pnl_usd ?? 0)}">${d.worst_week ? fmtUsd(d.worst_week.pnl_usd) : '-'}</div></div>
    <div class="card" style="border-color:#7a1f1f"><div class="label">Live Realized PnL</div><div class="value ${cls(d.live_realized_pnl_usd)}">${fmtUsd(d.live_realized_pnl_usd)}</div></div>
    <div class="card" style="border-color:#7a1f1f"><div class="label">Live Open Positions</div><div class="value">${d.live_open_positions_count}</div></div>
  `;

  const liveBanner = document.getElementById('live_banner');
  if (st.live_trading_enabled) {
    liveBanner.style.display = 'block';
    const readyTxt = st.live_exchanges_ready.length ? st.live_exchanges_ready.join(', ') : 'NONE';
    liveBanner.textContent = `LIVE TRADING ACTIVE - real money, $${st.per_exchange_capital_usd}/exchange cap. ` +
      `Credentialed exchanges: ${readyTxt}.` +
      (st.kill_switch ? ' KILL_SWITCH IS ON - no new entries.' : '');
  } else {
    liveBanner.style.display = 'none';
  }

  const cdBanner = document.getElementById('cooldown_banner');
  if (st.coins_in_cooldown && st.coins_in_cooldown.length > 0) {
    cdBanner.style.display = 'block';
    cdBanner.textContent = 'In cooldown (repeated losses, new entries paused): ' +
      st.coins_in_cooldown.map(c => `${c.symbol} (${c.loss_streak} losses, ${c.hours_remaining}h left)`).join(', ');
  } else {
    cdBanner.style.display = 'none';
  }
  const liveBadge = isLive => isLive ? '<span class="live-badge">LIVE</span>' : '';

  document.querySelector('#open_table tbody').innerHTML = st.open_positions.map(p => `
    <tr><td>${p.symbol}${liveBadge(p.is_live)}</td><td>${p.pair}</td><td>${p.direction}</td><td>${fmtTime(p.entry_time)}</td>
    <td>${fmtPx(p.entry_long_px)}</td><td>${fmtPx(p.entry_short_px)}</td><td>${fmtPct(p.entry_mid_spread_pct)}</td>
    <td>$${p.notional_usd.toFixed(0)}</td><td>${p.leverage}x</td>
    <td class="${cls(p.unrealized_pnl_usd ?? 0)}">${p.unrealized_pnl_usd != null ? fmtUsd(p.unrealized_pnl_usd) : '-'}
    ${p.exiting ? ' <span style="color:#f0c674;font-size:11px">(exiting, maker)</span>' : ''}</td></tr>
  `).join('') || '<tr><td colspan="10" style="color:#666">none open</td></tr>';

  document.querySelector('#trades_table tbody').innerHTML = st.recent_trades.map(t => `
    <tr><td>${t.symbol}${liveBadge(t.is_live)}</td><td>${t.pair}</td>
    <td>${fmtTime(t.entry_time)}</td><td>${fmtTime(t.exit_time)}</td>
    <td>${fmtPx(t.entry_long_px)}</td><td>${fmtPx(t.entry_short_px)}</td><td>${fmtPct(t.entry_mid_spread_pct)}</td>
    <td>${t.exit_reason ?? '-'}</td><td>${t.hold_hours.toFixed(2)}</td>
    <td class="${cls(t.net_pnl_usd)}">${fmtUsd(t.net_pnl_usd)}</td></tr>
  `).join('') || '<tr><td colspan="10" style="color:#666">no closed trades yet</td></tr>';

  document.querySelector('#symbol_table tbody').innerHTML = Object.entries(d.by_symbol).map(([sym, v]) => `
    <tr><td>${sym}</td><td>${v.n_trades}</td><td class="${cls(v.net_pnl_usd)}">${fmtUsd(v.net_pnl_usd)}</td></tr>
  `).join('') || '<tr><td colspan="3" style="color:#666">no closed trades yet</td></tr>';

  const riskRewardRow = (key, v) => `
    <tr><td>${key}</td><td>${v.n_trades}</td><td>${v.win_rate_pct ?? '-'}%</td>
    <td class="pos">${fmtUsd(v.avg_win_usd)}</td><td class="neg">${fmtUsd(v.avg_loss_usd)}</td>
    <td>${v.profit_factor ?? '-'}</td><td class="${cls(v.net_pnl_usd)}">${fmtUsd(v.net_pnl_usd)}</td></tr>
  `;
  const byNetPnlDesc = obj => Object.entries(obj).sort((x, y) => y[1].net_pnl_usd - x[1].net_pnl_usd);

  document.querySelector('#pair_table tbody').innerHTML = byNetPnlDesc(d.by_pair).map(([k, v]) => riskRewardRow(k, v)).join('')
    || '<tr><td colspan="7" style="color:#666">no closed trades yet</td></tr>';

  document.querySelector('#exchange_table tbody').innerHTML = byNetPnlDesc(d.by_exchange).map(([k, v]) => riskRewardRow(k, v)).join('')
    || '<tr><td colspan="7" style="color:#666">no closed trades yet</td></tr>';
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
