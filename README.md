# Crypto Spread Arb — Live Paper-Trading Front-Test

## Live trading (real money - off by default)

There's an optional live-trading path alongside paper mode, gated by
`LIVE_TRADING` (defaults to **false** - every deploy is paper-only unless
this is explicitly set). All 4 brokers now exist:

| Exchange | Auth model | Confidence |
|---|---|---|
| Aster (`brokers/aster.py`) | HMAC-SHA256 signed REST (Binance-compatible) | High - well-documented, simple REST |
| Hyperliquid (`brokers/hyperliquid.py`) | Wallet-signed via official Python SDK (`market_open`/`market_close`) | High - official SDK, documented method signatures |
| Pacifica (`brokers/pacifica.py`) | Agent-wallet message signing (Solana ed25519) | **Medium** - signing scheme confirmed from their own SDK source, but exact response field names (order fill price/qty) are inferred from partial docs, not independently verified against a live response |
| Ostium (`brokers/ostium.py`) | On-chain (Arbitrum) via official SDK, asset codes resolved dynamically via `get_pairs()` (never hardcoded - a wrong hardcoded code would silently trade the wrong instrument) | **Medium** - SDK surface confirmed, but on-chain fill price is approximated as the reference book price, not parsed from transaction receipts |

**Before flipping `LIVE_TRADING=true` for Pacifica or Ostium specifically**:
do one real test order manually (smallest size the exchange allows) and
check the `raw=...` data in the logs against what the broker code expects -
the "Medium confidence" exchanges above are the ones where a live test is
worth doing deliberately, not just trusting the code.

**A pair only trades live if BOTH its legs have a broker AND a working
credential AND are listed in `LIVE_EXCHANGES`** - the engine checks each
broker's `is_configured` flag (true only if its required env vars are
actually set), not just whether the module exists. Startup logs print
exactly which exchanges are both built and credentialed
(`Exchanges with a broker AND valid credentials: [...]`) so you can verify
before any real order goes out. Live trading activates automatically, pair
by pair, the moment two exchanges are both ready - no code changes needed.

**Safety rails (all in `config.py`, env-var controlled):**
- `LIVE_TRADING=false` by default - the master switch.
- `PER_EXCHANGE_CAPITAL_USD` (default `20`) - hard notional cap per leg,
  enforced at order time, not just a suggestion.
- `KILL_SWITCH=true` halts all new live entries instantly (existing open
  live positions still get managed by the normal stop-loss/max-hold/exit
  logic - this only blocks new ones).
- `LEG_FILL_RETRY_SECS` (default `5`) - if one leg fills and the other
  doesn't, retries the missing leg for this long, then **flattens the
  filled leg and aborts** rather than leaving a real, unhedged position
  open. Verified with deterministic tests (fake brokers, no real network
  calls): both-legs-fill, leg-mismatch-retry-then-flatten, and
  `LIVE_TRADING=false` never calling a broker even if everything else is
  configured.
- Live positions skip the maker-exit fee optimization for now - always a
  taker close (`profit_take_live_taker`) the instant it's profitable. Maker
  resting on real exits is real extra order-execution risk on a brand-new
  path; add it later once live taker closes are proven out, not on day one.
- Real fills (not the paper book mark) are what get recorded as
  entry/exit price for live positions - `positions`/`trades` tables have an
  `is_live` column so live and paper trades are never conflated in
  reporting.

**Setting it up:**
1. Set each exchange's credentials as Railway environment variables - paste
   real secret values directly into Railway's Variables tab, never into a
   chat or a committed file. Required per exchange:
   - Aster: `ASTER_API_KEY`, `ASTER_API_SECRET`
   - Hyperliquid: `HL_API_WALLET_PRIVATE_KEY` (a dedicated trade-only API
     wallet from app.hyperliquid.xyz/API - **not** your main wallet's key,
     it should not be able to withdraw), `HL_ACCOUNT_ADDRESS`
   - Pacifica: `PACIFICA_AGENT_PRIVATE_KEY` (a dedicated agent wallet from
     app.pacifica.fi/apikey, same non-withdrawal principle), `PACIFICA_ACCOUNT_ADDRESS`
   - Ostium: `OSTIUM_WALLET_PRIVATE_KEY` (needs real ETH on Arbitrum for
     gas, separate from USDC collateral), `OSTIUM_RPC_URL` (an Arbitrum
     RPC endpoint, e.g. from Alchemy), optionally `OSTIUM_LEVERAGE`
     (default `2`)
2. Add the exchanges you want active to `LIVE_EXCHANGES` (comma-separated,
   e.g. `LIVE_EXCHANGES=aster,hl,pac,ost`).
3. Set `LIVE_TRADING=true`.
4. Redeploy. Check the logs for `LIVE TRADING ENABLED` on startup - it
   prints exactly which exchanges have both a broker and a *valid
   credential* (not just that the module exists), so you can confirm
   before any real order goes out.

Continuous, real-market paper trading of the cross-exchange spread/reversal
arbitrage strategy backtested over the prior 90 days (Hyperliquid, Pacifica,
Binance) — now extended live to include **Ostium** and **Aster**, neither of
which had historical data available so they could only be added going
forward. The pool is **4 exchanges** (Hyperliquid, Pacifica, Ostium, Aster),
21 coins, up to `C(4,2)=6` exchange-pairs per coin.

**Removed exchanges:**
- **Bybit and OKX** were added, live-spot-checked across several major pairs
  (SOL, ZEC, ETH, JUP, XPL, BTC, NEAR), and then removed - neither ever
  appeared on either side of a profitable crossing against anything else in
  the pool. Both price within ~0.01% of Binance at all times (expected -
  they're all deep, efficient CEX-grade venues on the same underlying
  asset), so they added monitored pairs and poll load without adding edge.
- **Binance** was removed separately, for an operational reason rather than
  a profitability one: its API requires either an unrestricted (no
  IP-whitelist) key or a whitelisted static IP, and Railway's static
  outbound IP is a Pro-plan-only feature with IPs that can still change if
  the service is ever moved to a different region. Rather than run on an
  unrestricted Binance key or take on that fragility, it was dropped from
  the live pool. `exchanges/binance.py`/`bybit.py`/`okx.py` are deleted
  rather than kept around disabled - if Binance comes back later (e.g. a
  Pro-plan static IP, or a fixed-IP proxy), it's a small, self-contained
  re-add, not an unwind of dead code.

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

## Reading the results

- `/status` → `total_return_pct` is the live, bid-ask-aware return since start.
- `/report` → `by_exit_reason` shows the split between `profit_take`, `stop_loss`, and `max_hold` closes - in v2, most closes should be `profit_take` by construction (you only enter when the books already prove a profitable trade, and you exit the moment unwinding is break-even or better). A high proportion of `stop_loss`/`max_hold` exits is a signal something about market conditions or the risk-param sizing needs revisiting.
- Let it run **at least 1-2 weeks** before drawing conclusions — true crossed-book opportunities are rarer than the old mid-price-gap signal, so expect fewer total trades than the original backtest, even if each one is more reliably profitable.

## Known limitations

- Ostium fee schedule is a placeholder (`config.py` → `TAKER_FEE["ost"]`) — not publicly confirmed, update if you find their real schedule. Pacifica and Ostium's maker fees (`config.MAKER_FEE`) are estimated as half their taker rate, since neither publicly documents a separate maker rate - HL's maker rate is its real published standard-tier number. Aster's fee was taken from its publicly listed standard tier as of June 2026 (0.035%/0.01% taker/maker) - verify if it changes.
- Aster has no historical backtest behind it (same situation as Ostium) — its pairs use the generic default stop-loss/max-hold/z-score baseline until enough live history accumulates.
- The maker-exit fill simulation is a simplification: it assumes a full fill the instant the market touches your resting price, with no partial fills and no queue position ahead of you. Real maker fills can be slower or smaller than this model assumes, especially on thinner books (Pacifica, Ostium).
- No actual liquidation simulation — leverage is used only to size notional for P&L, not to model an actual margin call mid-trade. Treat any single position's notional as a proxy, not a guarantee you'd survive that leverage live.
- Polling-based, not websocket — by the time you fetch both books and react, the crossed-book opportunity may have already closed on its own; this is real execution latency the bid/ask entry condition can't fully eliminate, only bound (10s poll interval, fast-moving books can revert faster than that).
- `risk_params.json` (stop-loss/max-hold sizing) is derived from mid-price OHLC history, since that's what the original backtest had - it's a reasonable data-driven proxy for "how unusual is this," not a perfect bid/ask-based measure. Pairs not in the original 35-symbol backtest (mostly anything involving Ostium) fall back to a generic wide default (0.50% stop, 72h max-hold) - tighten these once you've collected enough live history to compute real ones.
