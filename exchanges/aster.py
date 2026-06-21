"""Aster (asterdex) USDT perpetuals - real top-of-book bid/ask.
API is Binance-compatible (same field names), unlike Binance itself it
hasn't shown any geo-block behavior in testing."""
import aiohttp

URL = "https://fapi.asterdex.com/fapi/v1/ticker/bookTicker"


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Aster symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}
    """
    try:
        async with session.get(URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
        if not isinstance(data, list):
            return {}
    except Exception:
        return {}
    by_symbol = {row["symbol"]: row for row in data}
    out = {}
    for coin, full_symbol in symbols_map.items():
        row = by_symbol.get(full_symbol)
        if row:
            out[coin] = (float(row["bidPrice"]), float(row["askPrice"]))
    return out
