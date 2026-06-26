"""
Entrypoint - runs the paper-trading engine as a background loop alongside a
small FastAPI status server (so it satisfies Railway's "web service" port
requirement and gives you a URL to check whether it's actually working).
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse, HTMLResponse

import config
import db
import report
from dashboard import HTML as DASHBOARD_HTML
from engine import PaperEngine
from exchanges import hyperliquid, pacifica, ostium, aster
import brokers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")

# The Ostium SDK's underlying GraphQL client (gql) logs its full schema
# introspection query/response (megabytes of text) at INFO level on first
# connection - silence it specifically so it doesn't bury real log lines
# (OPEN[LIVE], LIVE-CLOSE-FAILED, etc.) under noise.
logging.getLogger("gql").setLevel(logging.WARNING)
logging.getLogger("gql.transport").setLevel(logging.WARNING)
logging.getLogger("gql.transport.aiohttp").setLevel(logging.WARNING)
logging.getLogger("gql.transport.requests").setLevel(logging.WARNING)

db.init_db()
engine = PaperEngine()

EQUITY_SNAPSHOT_EVERY_N_TICKS = 30  # ~5 min at 10s polling

# Computed once - static for the life of the process. HL/Pacifica/Aster now
# stream these via persistent WebSockets (see start_ws() in lifespan below)
# instead of REST-polling every tick; Ostium has no order-book concept (it's
# oracle-priced) so it stays on its REST poll inside poll_loop.
HL_COINS = [c for c, e in config.EXCHANGES_PER_COIN.items() if "hl" in e]
PAC_COINS = [c for c, e in config.EXCHANGES_PER_COIN.items() if "pac" in e]
OST_COINS = [c for c, e in config.EXCHANGES_PER_COIN.items() if "ost" in e]
ASTER_MAP = {c: config.ASTER_SYMBOL[c] for c, e in config.EXCHANGES_PER_COIN.items() if "aster" in e}


async def poll_loop():
    tick = 0
    async with aiohttp.ClientSession() as session:
        while True:
            t0 = time.time()
            try:
                results = await asyncio.gather(
                    hyperliquid.fetch_book_tickers(session, HL_COINS),
                    pacifica.fetch_book_tickers(session, PAC_COINS),
                    ostium.fetch_book_tickers(session, OST_COINS),
                    aster.fetch_book_tickers(session, ASTER_MAP),
                    return_exceptions=True,
                )
                names = ["hl", "pac", "ost", "aster"]
                for name, res in zip(names, results):
                    if isinstance(res, Exception):
                        log.warning(f"{name} fetch failed: {res}")
                        continue
                    engine.update_books(name, res)

                await engine.tick(session)

                tick += 1
                if tick % EQUITY_SNAPSHOT_EVERY_N_TICKS == 0:
                    eq, open_n = engine.snapshot_equity()
                    log.info(f"Equity snapshot: ${eq:.2f}  open_positions={open_n}")
            except Exception as e:
                log.exception(f"poll_loop error: {e}")

            elapsed = time.time() - t0
            await asyncio.sleep(max(0.5, config.POLL_INTERVAL_SECS - elapsed))


@asynccontextmanager
async def lifespan(app: FastAPI):
    hyperliquid.start_ws(HL_COINS)
    pacifica.start_ws(PAC_COINS)
    aster.start_ws(list(ASTER_MAP.values()))
    log.info("Started persistent WS feeds: HL/Pacifica/Aster (Ostium stays on REST poll - oracle-priced, no order book)")
    if config.LIVE_TRADING:
        async with aiohttp.ClientSession() as session:
            await engine.reconcile_orphans(session)
    task = asyncio.create_task(poll_loop())
    log.info("Paper-trading poll loop started")
    yield
    task.cancel()


app = FastAPI(title="Crypto Spread Arb Front-Test", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    eq = engine.current_equity()
    open_positions = db.get_open_positions()
    trades = db.get_all_trades()
    realized = db.get_realized_pnl_total()
    unrealized_total, unrealized_by_pos = engine.get_unrealized_pnl()
    unrealized_map = {p["pos_id"]: p for p in unrealized_by_pos}
    for pos in open_positions:
        u = unrealized_map.get(pos["id"])
        pos["unrealized_pnl_usd"] = u["unrealized_pnl_usd"] if u else None
        pos["hold_hours"] = u["hold_hours"] if u else None
        pos["exiting"] = u["exiting"] if u else False
        pos["is_live"] = bool(pos.get("is_live"))
    for t in trades:
        t["is_live"] = bool(t.get("is_live"))
    equity_with_unrealized = eq + unrealized_total
    live_ready = sorted(
        e for e in config.LIVE_EXCHANGES
        if e in brokers.BROKERS and getattr(brokers.BROKERS[e], "is_configured", False)
    )

    # Per-exchange margin/notional currently committed by REAL open positions only -
    # at-a-glance view of where leveraged exposure is concentrated right now.
    # margin_usd is the actual capital tied up (notional / leverage); notional_usd
    # is the leveraged position size - the gap between them IS the leverage in use.
    live_exposure_by_exchange: dict[str, dict] = {}
    for pos in open_positions:
        if not pos["is_live"]:
            continue
        parts = pos["direction"].split("_")  # "long_<exch>_short_<exch>"
        if len(parts) != 4:
            continue
        long_exch, short_exch = parts[1], parts[3]
        margin_usd = pos["notional_usd"] / pos["leverage"] if pos["leverage"] else pos["notional_usd"]
        for exch in (long_exch, short_exch):
            slot = live_exposure_by_exchange.setdefault(exch, {"notional_usd": 0.0, "margin_usd": 0.0, "positions": 0})
            slot["notional_usd"] += pos["notional_usd"]
            slot["margin_usd"] += margin_usd
            slot["positions"] += 1
    for slot in live_exposure_by_exchange.values():
        slot["notional_usd"] = round(slot["notional_usd"], 2)
        slot["margin_usd"] = round(slot["margin_usd"], 2)
    return JSONResponse({
        "capital_usd": config.PAPER_CAPITAL_USD,
        "equity_usd": round(eq, 2),
        "total_return_pct": round((eq / config.PAPER_CAPITAL_USD - 1) * 100, 3),
        "realized_pnl_usd": round(realized, 2),
        "unrealized_pnl_usd": unrealized_total,
        "equity_with_unrealized_usd": round(equity_with_unrealized, 2),
        "total_return_with_unrealized_pct": round((equity_with_unrealized / config.PAPER_CAPITAL_USD - 1) * 100, 3),
        "coins_in_cooldown": engine.get_cooldown_status(),
        "open_positions_count": len(open_positions),
        "open_positions": open_positions,
        "closed_trades_count": len(trades),
        "recent_trades": trades[:20],
        "live_trading_enabled": config.LIVE_TRADING,
        "live_exchanges_ready": live_ready,
        "kill_switch": config.KILL_SWITCH,
        "live": db.get_live_summary(),
        "per_exchange_capital_usd": config.PER_EXCHANGE_CAPITAL_USD,
        "live_exposure_by_exchange": live_exposure_by_exchange,
    })


@app.post("/admin/reset-metrics-temp")
async def admin_reset_metrics_temp(token: str):
    """TEMPORARY - one-time dashboard reset, to be removed in the very next
    commit after use. Wipes closed positions/trades/equity history; leaves
    any currently open position untouched so nothing real gets orphaned."""
    if token != "203G4ZrOOpl5ew4uZ5KNUrebSB7yWPG7":
        return JSONResponse({"error": "invalid token"}, status_code=403)
    db.reset_all_metrics()
    return JSONResponse({"status": "reset complete"})


@app.get("/trades")
async def trades():
    return JSONResponse({"trades": db.get_all_trades()})


@app.get("/equity_curve")
async def equity_curve():
    return JSONResponse({"equity_curve": db.get_equity_curve()})


@app.get("/report")
async def get_report():
    return JSONResponse(report.build_report())


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/")
async def root():
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
