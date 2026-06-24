"""Aster (asterdex) USDT perpetuals - real top-of-book bid/ask.
API is Binance-compatible (same field names), unlike Binance itself it
hasn't shown any geo-block behavior in testing."""
import aiohttp

URL = "https://fapi.asterdex.com/fapi/v1/ticker/bookTicker"
DEPTH_URL = "https://fapi.asterdex.com/fapi/v1/depth"


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


async def fetch_depth(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """Full order-book depth (top 20 levels, price+qty) for one full Aster
    symbol (e.g. 'BTCUSDT') - used to estimate a realistic average fill price
    for a given notional size instead of assuming the whole order fills at
    the single best price. bookTicker (used for the regular poll) has no
    quantity field at all, so this is a separate per-symbol endpoint."""
    try:
        async with session.get(DEPTH_URL, params={"symbol": symbol, "limit": 20},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            d = await resp.json()
        bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
        if not bids or not asks:
            return None
        return {"bids": bids, "asks": asks}
    except Exception:
        return None
