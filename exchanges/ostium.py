"""
Ostium - real bid/ask via /PricePublish/latest-price (one request per asset).
Ostium has no historical API (confirmed by direct probing) - this module is
forward/live-only, which is exactly what the front-test needs to start
building a real historical record for Ostium for the first time.
"""
import asyncio
import aiohttp

URL = "https://metadata-backend.ostium.io/PricePublish/latest-price"

# Ostium crypto asset symbols differ from the other exchanges (Ostium uses "<COIN>USD")
COIN_TO_OSTIUM_ASSET = {
    "BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "BNB": "BNBUSD",
    "ADA": "ADAUSD", "XRP": "XRPUSD", "TRX": "TRXUSD", "LINK": "LINKUSD",
    "HYPE": "HYPEUSD",
}


async def _fetch_one(session: aiohttp.ClientSession, coin: str, asset: str):
    try:
        async with session.get(
            URL, params={"asset": asset}, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            d = await resp.json()
        if d.get("isMarketOpen") is False:
            return coin, None
        bid, ask = float(d["bid"]), float(d["ask"])
        return coin, (bid, ask)
    except Exception:
        return coin, None


async def fetch_book_tickers(session: aiohttp.ClientSession, coins: list[str]) -> dict[str, tuple[float, float]]:
    targets = [(c, COIN_TO_OSTIUM_ASSET[c]) for c in coins if c in COIN_TO_OSTIUM_ASSET]
    results = await asyncio.gather(*[_fetch_one(session, c, a) for c, a in targets])
    return {coin: val for coin, val in results if val is not None}
