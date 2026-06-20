"""Binance USDM perpetuals - real top-of-book bid/ask."""
import aiohttp

URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Binance symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}
    One request fetches ALL symbols, then we filter to what we track.
    """
    async with session.get(URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    by_symbol = {row["symbol"]: row for row in data}
    out = {}
    for coin, full_symbol in symbols_map.items():
        row = by_symbol.get(full_symbol)
        if row:
            out[coin] = (float(row["bidPrice"]), float(row["askPrice"]))
    return out
