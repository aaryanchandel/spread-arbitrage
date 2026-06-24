"""Hyperliquid - real top-of-book bid/ask via l2Book (one request per coin)."""
import asyncio
import aiohttp

URL = "https://api.hyperliquid.xyz/info"


async def _fetch_one(session: aiohttp.ClientSession, coin: str):
    try:
        async with session.post(
            URL, json={"type": "l2Book", "coin": coin},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            d = await resp.json()
        levels = d.get("levels", [])
        if len(levels) < 2 or not levels[0] or not levels[1]:
            return coin, None
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
        return coin, (bid, ask)
    except Exception:
        return coin, None


async def fetch_book_tickers(session: aiohttp.ClientSession, coins: list[str]) -> dict[str, tuple[float, float]]:
    results = await asyncio.gather(*[_fetch_one(session, c) for c in coins])
    return {coin: val for coin, val in results if val is not None}


async def fetch_depth(session: aiohttp.ClientSession, coin: str) -> dict | None:
    """Full order-book depth (every level, price+size) for one coin - used to
    estimate a realistic average fill price for a given notional size instead
    of assuming the whole order fills at the single best price."""
    try:
        async with session.post(
            URL, json={"type": "l2Book", "coin": coin},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            d = await resp.json()
        levels = d.get("levels", [])
        if len(levels) < 2:
            return None
        bids = [(float(l["px"]), float(l["sz"])) for l in levels[0]]
        asks = [(float(l["px"]), float(l["sz"])) for l in levels[1]]
        if not bids or not asks:
            return None
        return {"bids": bids, "asks": asks}
    except Exception:
        return None
