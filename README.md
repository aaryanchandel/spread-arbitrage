# Crypto Spread Arb — Live Paper-Trading Front-Test

Continuous, real-market paper trading of the cross-exchange spread/reversal
arbitrage strategy backtested over the prior 90 days (Hyperliquid, Pacifica,
Binance) — now extended live to include **Ostium** and **Aster**, neither of
which had historical data available so they could only be added going
forward. The pool is 5 exchanges, 21 coins, up to `C(5,2)=10` exchange-pairs
per coin.

**Removed exchanges:** Bybit and OKX were added, live-spot-checked across
several major pairs (SOL, ZEC, ETH, JUP, XPL, BTC, NEAR), and then removed -
neither ever appeared on either side of a profitable crossing against
anything else in the pool. Both price within ~0.01% of Binance at all times
(expected - they're all deep, efficient CEX-grade venues on the same
underlying asset), so they added monitored pairs and poll load without
adding edge. The actual crossings came from venues with genuinely different
liquidity/pricing profiles - Ostium (oracle-fed) and Pacifica (thin books) -
not from adding more identically-priced centralized exchanges. Their adapter
code is gone too (`exchanges/bybit.py`, `exchanges/okx.py` removed) rather
than kept around disabled.

**v3 strategy (current):** entry and exit are both decided on real bid/ask,
not mid-price.

- **Entry** requires BOTH:
  1. An actually-crossed order book - one exchange's bid must sit above the
     other's ask by more than the exact round-trip taker fee cost (no
     slippage buffer added - crossing the books already prices in the real
     execution cost). This is a true, immediately-capturable arbitrage, not
     an inferred mid-price gap.
  2. A statistically unusual dislocation - the current mid-spread's z-score
     against its own recent distribution must exceed `Z_ENTRY_THRESHOLD`
     (default 2.5). This is the accuracy filter: a lot of crossed-book
     moments are real but unremarkable, just barely clearing fees -
     requiring a z-score on top means we only trade the ones that are also
     larger than this pair's normal day-to-day noise. Fewer trades, each
     backed by a statistical edge, not just a margin-of-fees edge. The
     baseline is a live rolling window (`Z_ROLLING_WINDOW`, default 360
     ticks ≈ 1h) once `Z_MIN_LIVE_OBS` (default 30) observations have
     accumulated; before that, it falls back to the 90-day historical
     mean/std baked into `risk_params.json`, so there's no cold-start gap
     right after a redeploy.
- **Exit (primary)** takes profit the instant unwinding the position right
  now - sell the long leg at its current bid, buy back the short leg at its
  current ask, net of the full round-trip *taker* fee - would be break-even
  or better. Also bid/ask-driven.
- **Exit fee optimization (maker-first):** once that condition is met, the
  position doesn't close immediately at taker rates - it rests a limit
  order at the best achievable maker price on each leg (current ask for the
  long leg, current bid for the short leg) for up to
  `MAKER_EXIT_TIMEOUT_SECS` (default 20s), to capture the lower maker fee
  (`config.MAKER_FEE`) instead. **This is a real bet on the spread, not a
  free fee-timing option** - both legs staying hedged against the underlying
  does not mean the spread itself can't decay back to unprofitable while
  you wait. At timeout: if it's still profitable at current taker prices,
  it falls back to a guaranteed taker close (`profit_take_taker_fallback`).
  If it's *not* still profitable (the maker didn't fill and the opportunity
  decayed), the attempt is abandoned (`EXIT-ABORT` in the logs) and the
  position keeps holding - it does **not** force-close at a loss just
  because a timer expired. Risk controls (stop-loss/max-hold) are still
  active throughout and will catch a true reversal. Entry never attempts a
  maker order - a crossed book may vanish in seconds, so there's no time to
  rest an order on the way in.
- **Exit (safety nets)**: a 99.99th-percentile stop-loss (further padded by
  `STOP_LOSS_BUFFER_MULT`, default 1.05x) and a 99.99th-percentile max-hold,
  both sized from the 90-day backtest's own historical distributions
  (`risk_params.json`). These are deliberately extremely wide - essentially
  the historical max, padded another 5% - so they only fire on something
  genuinely outside the historical record, never as a routine exit path.
- **Adaptive per-symbol cooldown**: if a coin loses `LOSS_STREAK_THRESHOLD`
  (default 2) trades in a row - across any exchange-pair it trades on - new
  entries on that coin pause for `LOSS_STREAK_COOLDOWN_HOURS` (default 24).
  This is adaptive, not a static blacklist: a single win immediately resets
  the streak to zero, and even without a win the cooldown expires on its own
  - if the coin keeps losing it gets re-excluded automatically next time,
  if conditions improve it trades again automatically. No manual list to
  maintain. Visible on the dashboard as an amber banner and in `/status` →
  `coins_in_cooldown`.

This replaced an earlier v1 that entered/exited on mid-price spread crossing
zero - which looked good in the original OHLC backtest but lost money once
real bid-ask costs were included live (every leg pays the spread crossing
it twice: once on entry, once on exit). v2 added crossed-book entry and
maker-first exits; v3 added the z-score accuracy filter and fixed a bug
where the maker-exit timeout could force-close at a loss; this revision
added the stop-loss buffer and the adaptive cooldown.

**Tuning knobs** (env vars, see `.env.example`): `Z_ENTRY_THRESHOLD` (lower
= more trades, less selective; higher = fewer, more confident),
`Z_ROLLING_WINDOW`, `Z_MIN_LIVE_OBS`, `MAKER_EXIT_TIMEOUT_SECS`,
`STOP_LOSS_BUFFER_MULT`, `LOSS_STREAK_THRESHOLD`, `LOSS_STREAK_COOLDOWN_HOURS`.

## What it does

- Polls real bid/ask every `POLL_INTERVAL_SECS` (default 10s) from:
  - Binance USDM perps (`/fapi/v1/ticker/bookTicker`)
  - Hyperliquid (`l2Book` per coin)
  - Pacifica (`/book` per symbol)
  - Ostium (`/PricePublish/latest-price` per asset — crypto pairs only: BTC, ETH, SOL, BNB, ADA, XRP, TRX, LINK, HYPE)
  - Aster (asterdex) USDT perps (`/fapi/v1/ticker/bookTicker` — Binance-API-compatible, one request, all symbols)
- Runs the same convergence/reversal state machine as the backtest, per coin x exchange-pair.
- Opens/closes **paper** positions (no real money, no real orders) using real bid/ask fills.
- Persists every position, closed trade, and periodic equity snapshot to SQLite.
- Exposes a status API so you can check progress without touching the server.

## Endpoints (once deployed)

- `GET /health` — liveness check
- `GET /status` — current equity, open positions, recent trades
- `GET /trades` — full closed-trade log
- `GET /equity_curve` — equity snapshots over time

## Run locally

```bash
pip install -r requirements.txt
python main.py
# then in another terminal:
curl http://localhost:8000/status
```

## Deploy — GitHub + Railway

This environment doesn't have `gh` or the Railway CLI authenticated, so push
and deploy manually (5 minutes):

1. **Create the GitHub repo** (from this folder):
   ```bash
   git init   # already done if you got this file from the assistant
   git add .
   git commit -m "Initial paper-trading front-test"
   gh repo create crypto-arb-fronttest --private --source=. --push
   ```
   (No `gh`? Create an empty repo on github.com, then:
   `git remote add origin <your-repo-url> && git push -u origin main`)

2. **Deploy on Railway:**
   - railway.app → New Project → Deploy from GitHub repo → select this repo.
   - Railway auto-detects Python via `railway.json` / `Procfile` (nixpacks builder, `python main.py` start command).
   - Add a **Volume** mounted at `/data` (Railway dashboard → your service → Settings → Volumes) so the SQLite DB survives restarts/redeploys.
   - Set environment variable `DB_PATH=/data/paper.db`.
   - Optionally override `PAPER_CAPITAL_USD`, `DEPLOY_FRACTION`, `POLL_INTERVAL_SECS`, `N_CONCURRENT_PAIRS` (see `.env.example`).
   - Deploy. Railway gives you a public URL — hit `<url>/status` any time to see if it's working.

3. **Without a volume**, the DB resets on every redeploy/restart — fine for a quick smoke test, not for tracking a multi-week front-test. Add the volume before leaving it running unattended.

## Binance geo-block (HL-BN / PAC-BN pairs paused)

Binance's futures API (`fapi.binance.com`) geo-blocks many cloud-hosting regions. If you see `Binance FUTURES API unreachable` in the logs, that's this, not a bug — and the dashboard will show a banner when it's active.

**Do not enable `ALLOW_SPOT_FALLBACK`** as the fix. Binance spot and Binance perp differ by the funding-driven basis, which for this strategy's actual edge coins (CRV, JUP, MON, etc.) currently runs 0.04-0.14% — a meaningful fraction of the ~0.17-0.21% entry threshold itself. Substituting spot prices doesn't just lose precision, it measures a *different thing* than the cross-exchange mispricing the strategy is built to capture, which defeats the entire point of running a live front-test for an honest signal.

**Real fix:** change Railway's deployment region (Settings → the block is geography-based, not Railway-specific - Binance restricts futures access from certain jurisdictions, commonly including US-hosted infrastructure). Until that's done, HL-PAC and Ostium pairs keep trading normally on clean, comparable data — you're running a 3-exchange front-test instead of 4, not a broken one.

## Reading the results

- `/status` → `total_return_pct` is the live, bid-ask-aware return since start.
- `/report` → `by_exit_reason` shows the split between `profit_take`, `stop_loss`, and `max_hold` closes - in v2, most closes should be `profit_take` by construction (you only enter when the books already prove a profitable trade, and you exit the moment unwinding is break-even or better). A high proportion of `stop_loss`/`max_hold` exits is a signal something about market conditions or the risk-param sizing needs revisiting.
- Let it run **at least 1-2 weeks** before drawing conclusions — true crossed-book opportunities are rarer than the old mid-price-gap signal, so expect fewer total trades than the original backtest, even if each one is more reliably profitable.

## Known limitations

- Ostium fee schedule is a placeholder (`config.py` → `TAKER_FEE["ost"]`) — not publicly confirmed, update if you find their real schedule. Pacifica and Ostium's maker fees (`config.MAKER_FEE`) are estimated as half their taker rate, since neither publicly documents a separate maker rate - HL and Binance's maker rates are their real published standard-tier numbers. Aster's fee was taken from its publicly listed standard tier as of June 2026 (0.035%/0.01% taker/maker) - verify if it changes.
- Aster has no historical backtest behind it (same situation as Ostium) — its pairs use the generic default stop-loss/max-hold/z-score baseline until enough live history accumulates.
- The maker-exit fill simulation is a simplification: it assumes a full fill the instant the market touches your resting price, with no partial fills and no queue position ahead of you. Real maker fills can be slower or smaller than this model assumes, especially on thinner books (Pacifica, Ostium).
- No actual liquidation simulation — leverage is used only to size notional for P&L, not to model an actual margin call mid-trade. Treat any single position's notional as a proxy, not a guarantee you'd survive that leverage live.
- Polling-based, not websocket — by the time you fetch both books and react, the crossed-book opportunity may have already closed on its own; this is real execution latency the bid/ask entry condition can't fully eliminate, only bound (10s poll interval, fast-moving books can revert faster than that).
- `risk_params.json` (stop-loss/max-hold sizing) is derived from mid-price OHLC history, since that's what the original backtest had - it's a reasonable data-driven proxy for "how unusual is this," not a perfect bid/ask-based measure. Pairs not in the original 35-symbol backtest (mostly anything involving Ostium) fall back to a generic wide default (0.50% stop, 72h max-hold) - tighten these once you've collected enough live history to compute real ones.
