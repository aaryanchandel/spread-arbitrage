"""
Binance - real top-of-book bid/ask.

Tries USDM futures first (matches the original 90-day backtest). Binance
futures API geo-blocks many cloud-hosting regions (HTTP 451) - including,
in practice, Railway's default region - returning an error dict instead of
the expected list. When that happens we fall back to the spot bookTicker
endpoint, which is far less commonly blocked. Spot vs perp introduces a
small basis difference (funding-driven), but it's a far better signal than
no Binance leg at all.
"""
import logging

import aiohttp

FUTURES_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
SPOT_URL = "https://api.binance.com/api/v3/ticker/bookTicker"

log = logging.getLogger("exchanges.binance")

_state = {"using_spot_fallback": False, "warned": False}


async def _get_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
        return resp.status, data


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Binance symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}
    """
    url = SPOT_URL if _state["using_spot_fallback"] else FUTURES_URL
    try:
        status, data = await _get_json(session, url)
        if not isinstance(data, list):
            if url == FUTURES_URL:
                if not _state["warned"]:
                    log.warning(f"Binance futures API unreachable (status={status}, body={data}) - "
                                f"likely geo-blocked from this host. Falling back to spot bookTicker.")
                    _state["warned"] = True
                _state["using_spot_fallback"] = True
                status, data = await _get_json(session, SPOT_URL)
                if not isinstance(data, list):
                    log.warning(f"Binance spot fallback also failed (status={status}, body={data})")
                    return {}
            else:
                log.warning(f"Binance spot API unreachable (status={status}, body={data})")
                return {}
    except Exception as e:
        log.warning(f"Binance fetch error: {e}")
        return {}

    by_symbol = {row["symbol"]: row for row in data}
    out = {}
    for coin, full_symbol in symbols_map.items():
        row = by_symbol.get(full_symbol)
        if row:
            out[coin] = (float(row["bidPrice"]), float(row["askPrice"]))
    return out
