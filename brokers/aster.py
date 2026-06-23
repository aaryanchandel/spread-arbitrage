"""
Aster live broker - real signed order placement (Binance-compatible v1 HMAC API).

LIVE TRADING: every function below can move real money. Reads ASTER_API_KEY /
ASTER_API_SECRET from the environment - never hardcode these, never log them.
Raises immediately if they're unset rather than silently no-op'ing, so a
missing credential fails loud instead of looking like a hung order.
"""
import hashlib
import hmac
import logging
import os
import time

import aiohttp

log = logging.getLogger("brokers.aster")

BASE_URL = "https://fapi.asterdex.com"
API_KEY = os.environ.get("ASTER_API_KEY", "").strip()
API_SECRET = os.environ.get("ASTER_API_SECRET", "").strip()
is_configured = bool(API_KEY and API_SECRET)

_symbol_precision_cache: dict[str, int] = {}


class BrokerError(Exception):
    pass


def _sign(params: dict) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = "&".join(f"{k}={v}" for k, v in params.items())
    params["signature"] = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return params


async def _request(session: aiohttp.ClientSession, method: str, path: str,
                    params: dict | None = None, signed: bool = False) -> dict:
    if not API_KEY or not API_SECRET:
        raise BrokerError("ASTER_API_KEY/ASTER_API_SECRET not set - refusing to call a live trading endpoint")
    params = dict(params or {})
    if signed:
        params = _sign(params)
    headers = {"X-MBX-APIKEY": API_KEY}
    async with session.request(method, f"{BASE_URL}{path}", params=params, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise BrokerError(f"Aster API error (status={resp.status}): {data}")
        return data


async def _symbol_precision(session: aiohttp.ClientSession, symbol: str) -> int:
    if symbol not in _symbol_precision_cache:
        data = await _request(session, "GET", "/fapi/v1/exchangeInfo")
        for s in data.get("symbols", []):
            _symbol_precision_cache[s["symbol"]] = s["quantityPrecision"]
    return _symbol_precision_cache.get(symbol, 3)


async def get_balance_usdt(session: aiohttp.ClientSession) -> float:
    """Available USDT balance - read-only, safe to call any time to verify connectivity."""
    data = await _request(session, "GET", "/fapi/v2/balance", signed=True)
    for asset in data:
        if asset["asset"] == "USDT":
            return float(asset["availableBalance"])
    return 0.0


async def get_position(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """Read-only. Returns {"qty", "side", "entry_price"} or None if flat."""
    data = await _request(session, "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
    for pos in data:
        qty = float(pos["positionAmt"])
        if abs(qty) > 1e-12:
            return {"qty": qty, "side": "long" if qty > 0 else "short",
                    "entry_price": float(pos["entryPrice"])}
    return None


async def get_all_positions(session: aiohttp.ClientSession) -> dict:
    """Read-only - every currently open position on this account, keyed by
    symbol. Used at startup to find real positions the bot isn't tracking
    (e.g. left over from a crash mid-trade) so they can be flattened."""
    data = await _request(session, "GET", "/fapi/v2/positionRisk", signed=True)
    out = {}
    for pos in data:
        qty = float(pos["positionAmt"])
        if abs(qty) > 1e-12:
            out[pos["symbol"]] = {"qty": qty, "side": "long" if qty > 0 else "short",
                                   "entry_price": float(pos["entryPrice"])}
    return out


async def set_leverage(session: aiohttp.ClientSession, symbol: str, leverage: int) -> None:
    """Sets account leverage for this symbol before opening a position - Aster
    requires an integer leverage, not a fraction."""
    await _request(session, "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)


async def place_market_order(session: aiohttp.ClientSession, symbol: str, side: str,
                              notional_usd: float, ref_price: float) -> dict:
    """LIVE - places a real market order. side: 'BUY' or 'SELL'. Sizes quantity
    from notional_usd / ref_price, rounded to the symbol's exchange-required precision."""
    precision = await _symbol_precision(session, symbol)
    qty = round(notional_usd / ref_price, precision)
    if qty <= 0:
        raise BrokerError(f"Computed order quantity is zero for {symbol} "
                           f"(notional=${notional_usd}, ref_price={ref_price})")
    data = await _request(session, "POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
    }, signed=True)
    avg_price = float(data.get("avgPrice") or 0) or ref_price
    log.info(f"LIVE ORDER {symbol} {side} qty={qty} orderId={data.get('orderId')} avgPrice={avg_price}")
    return {
        "order_id": data.get("orderId"),
        "filled_qty": float(data.get("executedQty", 0)),
        "avg_price": avg_price,
        "status": data.get("status"),
    }


async def close_position(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """LIVE - market-closes whatever position currently exists in symbol (reduceOnly).
    Returns None if already flat."""
    pos = await get_position(session, symbol)
    if pos is None:
        return None
    side = "SELL" if pos["qty"] > 0 else "BUY"
    data = await _request(session, "POST", "/fapi/v1/order", {
        "symbol": symbol, "side": side, "type": "MARKET",
        "quantity": abs(pos["qty"]), "reduceOnly": "true",
    }, signed=True)
    log.info(f"LIVE CLOSE {symbol} {side} qty={abs(pos['qty'])} orderId={data.get('orderId')}")
    return {
        "order_id": data.get("orderId"),
        "filled_qty": float(data.get("executedQty", 0)),
        "avg_price": float(data.get("avgPrice") or 0),
        "status": data.get("status"),
    }
