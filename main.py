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
from exchanges import binance, hyperliquid, pacifica, ostium

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")

db.init_db()
engine = PaperEngine()

EQUITY_SNAPSHOT_EVERY_N_TICKS = 30  # ~5 min at 10s polling


async def poll_loop():
    tick = 0
    async with aiohttp.ClientSession() as session:
        while True:
            t0 = time.time()
            try:
                hl_coins = [c for c, e in config.EXCHANGES_PER_COIN.items() if "hl" in e]
                pac_coins = [c for c, e in config.EXCHANGES_PER_COIN.items() if "pac" in e]
                bn_map = {c: config.BINANCE_SYMBOL[c] for c, e in config.EXCHANGES_PER_COIN.items() if "bn" in e}
                ost_coins = [c for c, e in config.EXCHANGES_PER_COIN.items() if "ost" in e]

                results = await asyncio.gather(
                    hyperliquid.fetch_book_tickers(session, hl_coins),
                    pacifica.fetch_book_tickers(session, pac_coins),
                    binance.fetch_book_tickers(session, bn_map),
                    ostium.fetch_book_tickers(session, ost_coins),
                    return_exceptions=True,
                )
                names = ["hl", "pac", "bn", "ost"]
                for name, res in zip(names, results):
                    if isinstance(res, Exception):
                        log.warning(f"{name} fetch failed: {res}")
                        continue
                    engine.update_books(name, res)

                engine.tick()

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
    return JSONResponse({
        "capital_usd": config.PAPER_CAPITAL_USD,
        "equity_usd": round(eq, 2),
        "total_return_pct": round((eq / config.PAPER_CAPITAL_USD - 1) * 100, 3),
        "realized_pnl_usd": round(realized, 2),
        "binance_futures_blocked": binance._state["futures_blocked"],
        "binance_spot_fallback_enabled": binance.ALLOW_SPOT_FALLBACK,
        "open_positions_count": len(open_positions),
        "open_positions": open_positions,
        "closed_trades_count": len(trades),
        "recent_trades": trades[:20],
    })


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
