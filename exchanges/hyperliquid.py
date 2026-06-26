"""Hyperliquid - real-time order book via a persistent WebSocket
(wss://api.hyperliquid.xyz/ws), replacing the previous per-coin REST poll.

l2Book pushes a full top-of-book snapshot on every book change (not a diff),
so each message simply REPLACES the cached state for that coin - no
snapshot+diff sequence-number reconciliation needed, unlike Binance-style
raw diff streams. fetch_book_tickers/fetch_depth now just read this
in-memory cache (instant, no network round-trip at decision time) instead
of making a REST call - this matters most for the live entry/exit depth
checks, which used to add real latency to the trading decision itself.

If the WS dies and hasn't reconnected yet, cached entries older than
STALE_AFTER_SECS are treated as missing rather than served stale - the
engine already handles a coin having no book data gracefully (skips it
that tick), so silently serving outdated prices is strictly worse than that.
"""
import asyncio
import logging
import time

import aiohttp
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger("exchanges.hyperliquid")

WS_URL = "wss://api.hyperliquid.xyz/ws"
STALE_AFTER_SECS = 15

_book_cache: dict[str, dict] = {}  # coin -> {"bids": [(px,sz),...], "asks": [...], "ts": float}
_ws_task: asyncio.Task | None = None


def _parse_levels(levels) -> dict:
    bids = [(float(l["px"]), float(l["sz"])) for l in levels[0]]
    asks = [(float(l["px"]), float(l["sz"])) for l in levels[1]]
    return {"bids": bids, "asks": asks, "ts": time.time()}


async def _run_ws(coins: list[str]):
    """Long-running background task - connects, subscribes to l2Book for
    every coin, and reconnects with backoff on any disconnect/error. Never
    raises - a dead feed just means fetch_book_tickers/fetch_depth return
    nothing for affected coins (treated as stale) until reconnection succeeds."""
    backoff = 1
    connector = aiohttp.TCPConnector(resolver=ThreadedResolver())
    while True:
        try:
            async with aiohttp.ClientSession(connector=connector, connector_owner=False) as session:
                async with session.ws_connect(WS_URL, heartbeat=30) as ws:
                    for coin in coins:
                        await ws.send_json({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}})
                    log.info(f"HL WS connected, subscribed to {len(coins)} coins")
                    backoff = 1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            d = msg.json()
                        except Exception:
                            continue
                        if d.get("channel") != "l2Book":
                            continue
                        data = d.get("data", {})
                        levels = data.get("levels", [])
                        coin = data.get("coin")
                        if coin and len(levels) >= 2 and levels[0] and levels[1]:
                            _book_cache[coin] = _parse_levels(levels)
        except Exception as e:
            log.warning(f"HL WS disconnected/error: {e} - reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def start_ws(coins: list[str]):
    """Call once at app startup. Idempotent - returns the existing task if
    already running."""
    global _ws_task
    if _ws_task is None:
        _ws_task = asyncio.create_task(_run_ws(coins))
    return _ws_task


def _fresh(coin: str) -> dict | None:
    entry = _book_cache.get(coin)
    if entry is None or time.time() - entry["ts"] > STALE_AFTER_SECS:
        return None
    return entry


async def fetch_book_tickers(session, coins: list[str]) -> dict[str, tuple[float, float]]:
    """Reads the live WS cache - kept async with an unused session param to
    match the existing poll_loop interface, but makes no network call."""
    out = {}
    for coin in coins:
        entry = _fresh(coin)
        if entry and entry["bids"] and entry["asks"]:
            out[coin] = (entry["bids"][0][0], entry["asks"][0][0])
    return out


async def fetch_depth(session, coin: str) -> dict | None:
    return _fresh(coin)
