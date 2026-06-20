"""
Binance USDM futures - real top-of-book bid/ask.

Binance futures API geo-blocks many cloud-hosting regions (HTTP 451-style
block) - confirmed happening from Railway's default region, returning an
error dict instead of the expected list.

We deliberately do NOT fall back to Binance spot prices by default. Spot vs
perp differs by the funding-driven basis, which for this strategy's actual
edge coins (CRV, JUP, MON, etc.) currently sits at 0.04-0.14% - a meaningful
fraction of the ~0.17-0.21% entry threshold itself. Silently substituting
spot would contaminate the exact cross-exchange signal this front-test
exists to validate honestly. If futures is unreachable, the Binance leg is
simply skipped (HL-PAC and Ostium pairs keep trading on clean, comparable
data) until real futures access is restored - e.g. by changing Railway's
deployment region, since the block is geography-based, not Railway-specific.

Set ALLOW_SPOT_FALLBACK=true if you explicitly want the degraded spot-proxy
behavior anyway (e.g. for a quick connectivity check, not for real results).
"""
import logging
import os

import aiohttp

FUTURES_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
SPOT_URL = "https://api.binance.com/api/v3/ticker/bookTicker"

ALLOW_SPOT_FALLBACK = os.environ.get("ALLOW_SPOT_FALLBACK", "false").lower() == "true"

log = logging.getLogger("exchanges.binance")

_state = {"futures_blocked": False, "warned": False}


async def _get_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
        return resp.status, data


async def fetch_book_tickers(session: aiohttp.ClientSession, symbols_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    """
    symbols_map: {coin -> full Binance symbol, e.g. 'BTC' -> 'BTCUSDT'}
    Returns {coin -> (bid, ask)}. Empty dict if futures is blocked and spot
    fallback isn't explicitly enabled - callers must treat that as "no data
    this cycle", not an error.
    """
    try:
        status, data = await _get_json(session, FUTURES_URL)
        if isinstance(data, list):
            _state["futures_blocked"] = False
        else:
            if not _state["warned"]:
                log.warning(
                    f"Binance FUTURES API unreachable (status={status}, body={data}). "
                    f"This is almost always a geo-block on this host's region, not a bug. "
                    f"BN-leg pairs are paused (HL-PAC / Ostium pairs keep trading normally) "
                    f"until this is fixed - try a different Railway deployment region. "
                    f"Set ALLOW_SPOT_FALLBACK=true to use spot prices instead (degrades signal quality)."
                )
                _state["warned"] = True
            _state["futures_blocked"] = True
            if not ALLOW_SPOT_FALLBACK:
                return {}
            status, data = await _get_json(session, SPOT_URL)
            if not isinstance(data, list):
                log.warning(f"Binance spot fallback also failed (status={status}, body={data})")
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
