"""Pacifica - real top-of-book bid/ask via /book (one request per symbol)."""
import asyncio
import aiohttp

URL = "https://api.pacifica.fi/api/v1/book"


async def _fetch_one(session: aiohttp.ClientSession, symbol: str):
    try:
        async with session.get(
            URL, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            d = await resp.json()
        levels = d.get("data", {}).get("l", [])
        if len(levels) < 2 or not levels[0] or not levels[1]:
            return symbol, None
        bid = float(levels[0][0]["p"])
        ask = float(levels[1][0]["p"])
        return symbol, (bid, ask)
    except Exception:
        return symbol, None


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols: list[str]) -> dict[str, tuple[float, float]]:
    results = await asyncio.gather(*[_fetch_one(session, s) for s in symbols])
    return {symbol: val for symbol, val in results if val is not None}


async def fetch_depth(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """Full order-book depth (every level, price+amount) for one symbol - used
    to estimate a realistic average fill price for a given notional size
    instead of assuming the whole order fills at the single best price."""
    try:
        async with session.get(
            URL, params={"symbol": symbol}, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            d = await resp.json()
        levels = d.get("data", {}).get("l", [])
        if len(levels) < 2:
            return None
        bids = [(float(l["p"]), float(l["a"])) for l in levels[0]]
        asks = [(float(l["p"]), float(l["a"])) for l in levels[1]]
        if not bids or not asks:
            return None
        return {"bids": bids, "asks": asks}
    except Exception:
        return None
