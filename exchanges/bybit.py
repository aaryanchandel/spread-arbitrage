"""Bybit USDT perpetuals - real top-of-book bid/ask."""
import aiohttp

URL = "https://api.bybit.com/v5/market/tickers"


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Bybit symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}
    One request fetches ALL linear perps, then we filter to what we track.
    """
    try:
        async with session.get(URL, params={"category": "linear"},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            d = await resp.json()
        rows = d.get("result", {}).get("list", [])
    except Exception:
        return {}
    by_symbol = {row["symbol"]: row for row in rows}
    out = {}
    for coin, full_symbol in symbols_map.items():
        row = by_symbol.get(full_symbol)
        if row and row.get("bid1Price") and row.get("ask1Price"):
            out[coin] = (float(row["bid1Price"]), float(row["ask1Price"]))
    return out
