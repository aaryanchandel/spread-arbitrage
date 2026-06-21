"""OKX USDT-margined perpetual swaps - real top-of-book bid/ask."""
import aiohttp

URL = "https://www.okx.com/api/v5/market/tickers"


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full OKX instId, e.g. 'BTC' -> 'BTC-USDT-SWAP'}
    Returns {coin -> (bid, ask)}
    One request fetches ALL SWAP instruments, then we filter to what we track.
    """
    try:
        async with session.get(URL, params={"instType": "SWAP"},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            d = await resp.json()
        rows = d.get("data", [])
    except Exception:
        return {}
    by_inst = {row["instId"]: row for row in rows}
    out = {}
    for coin, inst_id in symbols_map.items():
        row = by_inst.get(inst_id)
        if row and row.get("bidPx") and row.get("askPx"):
            out[coin] = (float(row["bidPx"]), float(row["askPx"]))
    return out
