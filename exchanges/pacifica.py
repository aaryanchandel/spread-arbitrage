"""Pacifica - real-time order book via a persistent WebSocket
(wss://ws.pacifica.fi/ws), replacing the previous REST poll.

The "book" channel pushes the full state for the subscribed aggregation
level on every update (not a diff), so each message simply REPLACES the
cached state for that symbol. agg_level=1 (finest) matches what the REST
/book endpoint returns by default.
"""
import asyncio
import logging
import time

import aiohttp
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger("exchanges.pacifica")

WS_URL = "wss://ws.pacifica.fi/ws"
STALE_AFTER_SECS = 15

_book_cache: dict[str, dict] = {}  # symbol -> {"bids","asks","ts"}
_ws_task: asyncio.Task | None = None


async def _run_ws(symbols: list[str]):
    """Long-running background task - connects, subscribes to the book
    channel for every symbol, and reconnects with backoff on any disconnect/
    error. Never raises - a dead feed just means fetch_book_tickers/
    fetch_depth return nothing for affected symbols until reconnection succeeds."""
    backoff = 1
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    while True:
        try:
            async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
                async with session.ws_connect(WS_URL, heartbeat=25) as ws:
                    for symbol in symbols:
                        await ws.send_json({"method": "subscribe",
                                             "params": {"source": "book", "symbol": symbol, "agg_level": 1}})
                    log.info(f"Pacifica WS connected, subscribed to {len(symbols)} symbols")
                    backoff = 1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = msg.json()
                        except Exception:
                            continue
                        if d.get("channel") != "book":
                            continue
                        data = d.get("data", {})
                        levels = data.get("l", [])
                        symbol = data.get("s")
                        if symbol and len(levels) >= 2 and levels[0] and levels[1]:
                            _book_cache[symbol] = {
                                "bids": [(float(l["p"]), float(l["a"])) for l in levels[0]],
                                "asks": [(float(l["p"]), float(l["a"])) for l in levels[1]],
                                "ts": time.time(),
                            }
        except Exception as e:
            log.warning(f"Pacifica WS disconnected/error: {e} - reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def start_ws(symbols: list[str]):
    """Call once at app startup. Idempotent."""
    global _ws_task
    if _ws_task is None:
        _ws_task = asyncio.create_task(_run_ws(symbols))
    return _ws_task


def _fresh(symbol: str) -> dict | None:
    entry = _book_cache.get(symbol)
    if entry is None or time.time() - entry["ts"] > STALE_AFTER_SECS:
        return None
    return entry


async def fetch_book_tickers(session, symbols: list[str]) -> dict[str, tuple[float, float]]:
    """Reads the live WS cache - kept async with an unused session param to
    match the existing poll_loop interface, but makes no network call."""
    out = {}
    for symbol in symbols:
        entry = _fresh(symbol)
        if entry and entry["bids"] and entry["asks"]:
            out[symbol] = (entry["bids"][0][0], entry["asks"][0][0])
    return out


async def fetch_depth(session, symbol: str) -> dict | None:
    """Full order-book depth (every level, price+amount) for one symbol - used
    to estimate a realistic average fill price for a given notional size
    instead of assuming the whole order fills at the single best price."""
    return _fresh(symbol)
