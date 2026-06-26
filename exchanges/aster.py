"""Aster (asterdex) USDT perpetuals - real-time order book via a persistent
combined WebSocket stream (wss://fstream.asterdex.com/stream), replacing the
previous REST poll. API is Binance-compatible (same field names), unlike
Binance itself it hasn't shown any geo-block behavior in testing.

Uses the Partial Book Depth stream (<symbol>@depth20@100ms), NOT the plain
diff stream (<symbol>@depth) - the partial stream pushes the full top-20
snapshot on every update, so each message simply REPLACES the cached state
for that symbol. The plain diff stream requires fetching a REST snapshot
first and reconciling incremental updates by sequence number (U/u/pu) -
unnecessary complexity avoided by using the snapshot-style stream instead.
"""
import asyncio
import logging
import time

import aiohttp
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger("exchanges.aster")

WS_BASE = "wss://fstream.asterdex.com/stream"
STALE_AFTER_SECS = 15

_book_cache: dict[str, dict] = {}  # full Aster symbol (e.g. 'BTCUSDT') -> {"bids","asks","ts"}
_ws_task: asyncio.Task | None = None


async def _run_ws(symbols: list[str]):
    """Long-running background task - connects to a single combined stream
    covering every symbol, and reconnects with backoff on any disconnect/
    error. Never raises - a dead feed just means fetch_book_tickers/
    fetch_depth return nothing for affected symbols until reconnection succeeds."""
    streams = "/".join(f"{s.lower()}@depth20@100ms" for s in symbols)
    url = f"{WS_BASE}?streams={streams}"
    backoff = 1
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    while True:
        try:
            async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    log.info(f"Aster WS connected, subscribed to {len(symbols)} symbols")
                    backoff = 1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = msg.json()
                        except Exception:
                            continue
                        data = payload.get("data", payload)  # combined stream wraps in {"stream":...,"data":...}
                        symbol = data.get("s")
                        bids = data.get("b", [])
                        asks = data.get("a", [])
                        if symbol and bids and asks:
                            _book_cache[symbol] = {
                                "bids": [(float(p), float(q)) for p, q in bids],
                                "asks": [(float(p), float(q)) for p, q in asks],
                                "ts": time.time(),
                            }
        except Exception as e:
            log.warning(f"Aster WS disconnected/error: {e} - reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def start_ws(full_symbols: list[str]):
    """Call once at app startup with the full list of Aster symbols (e.g.
    ['BTCUSDT', 'ETHUSDT', ...]). Idempotent."""
    global _ws_task
    if _ws_task is None:
        _ws_task = asyncio.create_task(_run_ws(full_symbols))
    return _ws_task


def _fresh(symbol: str) -> dict | None:
    entry = _book_cache.get(symbol)
    if entry is None or time.time() - entry["ts"] > STALE_AFTER_SECS:
        return None
    return entry


async def fetch_book_tickers(session, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Aster symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}. Reads the live WS cache - kept async with an
    unused session param to match the existing poll_loop interface, but makes
    no network call.
    """
    out = {}
    for coin, full_symbol in symbols_map.items():
        entry = _fresh(full_symbol)
        if entry and entry["bids"] and entry["asks"]:
            out[coin] = (entry["bids"][0][0], entry["asks"][0][0])
    return out


async def fetch_depth(session, symbol: str) -> dict | None:
    """Full order-book depth (top 20 levels, price+qty) for one full Aster
    symbol (e.g. 'BTCUSDT') - used to estimate a realistic average fill price
    for a given notional size instead of assuming the whole order fills at
    the single best price."""
    return _fresh(symbol)
