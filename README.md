# Crypto Spread Arb — Live Paper-Trading Front-Test

Continuous, real-market paper trading of the cross-exchange spread/reversal
arbitrage strategy backtested over the prior 90 days (Hyperliquid, Pacifica,
Binance) — now extended live to include **Ostium**, which has no historical
API and could only be added going forward.

**v2 strategy (current):** entry and exit are both decided on real bid/ask,
not mid-price.

- **Entry** requires an actually-crossed order book: one exchange's bid must
  sit above the other's ask by more than the exact round-trip taker fee cost
  (no slippage buffer added - crossing the books already prices in the real
  execution cost). This is a true, immediately-capturable arbitrage, not an
  inferred mid-price gap.
- **Exit (primary)** takes profit the instant unwinding the position right
  now - sell the long leg at its current bid, buy back the short leg at its
  current ask, net of the full round-trip *taker* fee - would be break-even
  or better. Also bid/ask-driven.
- **Exit fee optimization (maker-first):** once that condition is met, the
  position doesn't close immediately at taker rates - it rests a limit
  order at the best achievable maker price on each leg (current ask for the
  long leg, current bid for the short leg) for up to
  `MAKER_EXIT_TIMEOUT_SECS` (default 60s), to capture the lower maker fee
  (`config.MAKER_FEE`) instead. Both legs stay open and hedged against each
  other the whole time, so waiting adds fee-timing risk, not directional
  risk. If it hasn't filled by the timeout, it falls back to a guaranteed
  taker close at whatever the market is then - exit_reason will read
  `profit_take_maker` or `profit_take_taker_fallback` depending on which
  happened. Entry never does this - a crossed book may vanish in seconds,
  so there's no time to rest an order on the way in.
- **Exit (safety nets)**: a 95th-percentile stop-loss and a 95th-percentile
  max-hold, both sized from the 90-day backtest's own historical
  distributions (`risk_params.json`). These are wide by design - they only
  exist to bound the tail case where a position never reaches a profitable
  unwind, they are not the primary exit mechanism.

This replaced an earlier v1 that entered/exited on mid-price spread crossing
zero - which looked good in the original OHLC backtest but lost money once
real bid-ask costs were included live (every leg pays the spread crossing
it twice: once on entry, once on exit). v2 only enters when the books
themselves already prove a profitable trade exists.

## What it does

- Polls real bid/ask every `POLL_INTERVAL_SECS` (default 10s) from:
  - Binance USDM perps (`/fapi/v1/ticker/bookTicker`)
  - Hyperliquid (`l2Book` per coin)
  - Pacifica (`/book` per symbol)
  - Ostium (`/PricePublish/latest-price` per asset — crypto pairs only: BTC, ETH, SOL, BNB, ADA, XRP, TRX, LINK, HYPE)
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

- Ostium fee schedule is a placeholder (`config.py` → `TAKER_FEE["ost"]`) — not publicly confirmed, update if you find their real schedule. Pacifica and Ostium's maker fees (`config.MAKER_FEE`) are estimated as half their taker rate, since neither publicly documents a separate maker rate - HL and Binance's maker rates are their real published standard-tier numbers.
- The maker-exit fill simulation is a simplification: it assumes a full fill the instant the market touches your resting price, with no partial fills and no queue position ahead of you. Real maker fills can be slower or smaller than this model assumes, especially on thinner books (Pacifica, Ostium).
- No actual liquidation simulation — leverage is used only to size notional for P&L, not to model an actual margin call mid-trade. Treat any single position's notional as a proxy, not a guarantee you'd survive that leverage live.
- Polling-based, not websocket — by the time you fetch both books and react, the crossed-book opportunity may have already closed on its own; this is real execution latency the bid/ask entry condition can't fully eliminate, only bound (10s poll interval, fast-moving books can revert faster than that).
- `risk_params.json` (stop-loss/max-hold sizing) is derived from mid-price OHLC history, since that's what the original backtest had - it's a reasonable data-driven proxy for "how unusual is this," not a perfect bid/ask-based measure. Pairs not in the original 35-symbol backtest (mostly anything involving Ostium) fall back to a generic wide default (0.50% stop, 72h max-hold) - tighten these once you've collected enough live history to compute real ones.
