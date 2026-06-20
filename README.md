# Crypto Spread Arb — Live Paper-Trading Front-Test

Continuous, real-market paper trading of the cross-exchange spread/reversal
arbitrage strategy backtested over the prior 90 days (Hyperliquid, Pacifica,
Binance) — now extended live to include **Ostium**, which has no historical
API and could only be added going forward.

Detection logic mirrors the backtest (mid-price spread crossing zero =
convergence; flipping past the opposite threshold = reversal). Execution is
**not** simulated at mid-price — every simulated fill uses the exchange's
actual top-of-book bid or ask at that moment, so the P&L you see here already
includes the bid-ask cost that the original backtest could only approximate
with a flat slippage buffer. This is the realistic "does it actually work"
check before risking real capital.

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

- `/status` → `total_return_pct` is the live, bid-ask-aware return since start. Compare this against the original backtest's idealized mid-price numbers — a meaningful gap between the two is exactly the "does it actually work in the real market" signal you're looking for.
- Let it run **at least 1-2 weeks** before drawing conclusions — the backtest showed trade frequencies as low as 2-3/month for some pairs (SUI, AAVE), so short windows will look noisy or empty for those specifically. The high-frequency pairs (CRV, XPL, NEAR) should produce trades within hours.
- Compare `/trades` net_pnl_usd distribution against the backtest's "100% historical win rate" claim — that claim was flagged as an artifact of only counting trades that converged within the data window; the live front-test will show you the real win rate, including any trades that never converge.

## Known limitations (carried over from the backtest)

- Ostium fee schedule is a placeholder (`config.py` → `TAKER_FEE["ost"]`) — not publicly confirmed, update if you find their real schedule.
- No actual liquidation simulation — leverage is used only to size notional for P&L, not to model an actual margin call mid-trade. Treat any single position's notional as a proxy, not a guarantee you'd survive that leverage live.
- Polling-based, not websocket — fine for a multi-second-resolution front-test, not for low-latency execution if this graduates to real capital.
