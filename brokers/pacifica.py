"""
Pacifica live broker - real signed order placement via agent-wallet message
signing (Solana ed25519), built against Pacifica's documented signing scheme
and python-sdk examples (github.com/pacifica-fi/python-sdk).

LIVE TRADING: every function below can move real money. Reads:
- PACIFICA_AGENT_PRIVATE_KEY: base58 private key of a dedicated AGENT WALLET
  (create one at app.pacifica.fi/apikey) - NOT your main wallet's key. An
  agent wallet signs on your account's behalf without holding withdrawal
  rights.
- PACIFICA_ACCOUNT_ADDRESS: your main Pacifica account's public address,
  the one the agent wallet trades on behalf of.

KNOWN LIMITATION: the exact response field names for order fills
(average_filled_price, filled_amount, etc.) and position objects are this
module's best-effort inference from partial public docs - Pacifica's full
response schema wasn't independently confirmed against a live call while
building this. Run a small real order via the dashboard/logs and check
`raw=...` in the log line against this code before trusting it for size.
"""
import json
import logging
import math
import os
import time
import uuid

import aiohttp

log = logging.getLogger("brokers.pacifica")

BASE_URL = "https://api.pacifica.fi/api/v1"
AGENT_PRIVATE_KEY = os.environ.get("PACIFICA_AGENT_PRIVATE_KEY", "").strip()
ACCOUNT_ADDRESS = os.environ.get("PACIFICA_ACCOUNT_ADDRESS", "").strip()
is_configured = bool(AGENT_PRIVATE_KEY and ACCOUNT_ADDRESS)

_keypair = None
_market_info_cache: dict[str, dict] = {}


class BrokerError(Exception):
    pass


def _decimals_of(value_str: str) -> int:
    return len(value_str.split(".")[1]) if "." in value_str else 0


async def _market_info(session: aiohttp.ClientSession, symbol: str) -> dict:
    """Fetches and caches per-symbol lot_size/min_order_size/tick_size from the
    public /info endpoint - queried dynamically, never hardcoded, since a wrong
    guessed lot size either gets every order rejected or (worse) silently
    rounds size wrong."""
    if not _market_info_cache:
        async with session.get(f"{BASE_URL}/info", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
        if not data.get("success"):
            raise BrokerError(f"Pacifica /info error: {data}")
        for m in data.get("data", []):
            _market_info_cache[m["symbol"]] = m
    if symbol not in _market_info_cache:
        raise BrokerError(f"No Pacifica market info for symbol '{symbol}'")
    return _market_info_cache[symbol]


def _round_to_lot(amount: float, lot_size_str: str) -> float:
    lot_size = float(lot_size_str)
    decimals = _decimals_of(lot_size_str)
    rounded = math.floor(amount / lot_size) * lot_size
    return round(rounded, decimals)


def _get_keypair():
    global _keypair
    if _keypair is None:
        if not AGENT_PRIVATE_KEY or not ACCOUNT_ADDRESS:
            raise BrokerError("PACIFICA_AGENT_PRIVATE_KEY/PACIFICA_ACCOUNT_ADDRESS not set - refusing to trade")
        from solders.keypair import Keypair
        _keypair = Keypair.from_base58_string(AGENT_PRIVATE_KEY)
    return _keypair


def _sort_json_keys(value):
    if isinstance(value, dict):
        return {k: _sort_json_keys(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json_keys(v) for v in value]
    return value


def _sign(order_type: str, payload: dict) -> dict:
    """Returns the full signed request body (header fields + payload),
    per Pacifica's documented signing scheme: sign a compact, key-sorted
    JSON of {header fields, "data": payload} with the agent wallet key."""
    import base58
    keypair = _get_keypair()
    header = {"type": order_type, "timestamp": int(time.time() * 1000), "expiry_window": 5000}
    message = json.dumps(_sort_json_keys({**header, "data": payload}), separators=(",", ":"))
    signature = base58.b58encode(bytes(keypair.sign_message(message.encode("utf-8")))).decode("ascii")
    return {
        "account": ACCOUNT_ADDRESS,
        "agent_wallet": str(keypair.pubkey()),
        "signature": signature,
        "timestamp": header["timestamp"],
        "expiry_window": header["expiry_window"],
        **payload,
    }


async def get_position(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """Read-only, public endpoint (no signing needed) - confirmed reachable at
    /positions?account=<address> during development."""
    async with session.get(f"{BASE_URL}/positions", params={"account": ACCOUNT_ADDRESS},
                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    if not data.get("success"):
        raise BrokerError(f"Pacifica positions error: {data}")
    for pos in data.get("data", []):
        if pos.get("symbol") == symbol:
            qty = float(pos.get("amount", 0) or 0)
            if abs(qty) > 1e-12:
                side = pos.get("side", "")
                return {"qty": qty, "side": "long" if side in ("bid", "long") else "short",
                        "entry_price": float(pos.get("entry_price", 0) or 0)}
    return None


async def place_market_order(session: aiohttp.ClientSession, symbol: str, side: str,
                              notional_usd: float, ref_price: float) -> dict:
    """LIVE - places a real market order. side: 'BUY' or 'SELL' (mapped to Pacifica's bid/ask)."""
    info = await _market_info(session, symbol)
    raw_amount = notional_usd / ref_price
    amount = _round_to_lot(raw_amount, info["lot_size"])
    min_order_size = float(info.get("min_order_size", 0) or 0)
    if amount <= 0:
        raise BrokerError(f"{symbol}: order amount rounds to 0 with lot_size={info['lot_size']} "
                           f"(notional=${notional_usd}, ref_price={ref_price}) - notional too small for this lot size")
    # min_order_size is a USD notional floor (observed identically as "10" across
    # wildly different-priced symbols, e.g. BTC and MEGA) - NOT a base-asset-unit
    # count, so it must be compared against notional, not the raw rounded amount.
    rounded_notional = amount * ref_price
    if min_order_size > 0 and rounded_notional < min_order_size:
        raise BrokerError(f"{symbol}: rounded order notional ${rounded_notional:.2f} is below "
                           f"Pacifica's min_order_size=${min_order_size}")

    decimals = _decimals_of(info["lot_size"])
    payload = {
        "symbol": symbol, "reduce_only": False, "amount": f"{amount:.{decimals}f}",
        "side": "bid" if side == "BUY" else "ask",
        "slippage_percent": "0.5", "client_order_id": str(uuid.uuid4()),
    }
    request = _sign("create_market_order", payload)
    async with session.post(f"{BASE_URL}/orders/create_market", json=request,
                             timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    if not data.get("success"):
        raise BrokerError(f"Pacifica order error: {data}")
    order = data.get("data", {}) or {}
    avg_price = float(order.get("average_filled_price") or ref_price)
    filled_qty = float(order.get("filled_amount") or amount)
    log.info(f"LIVE ORDER {symbol} {side} amount={amount} avgPrice={avg_price} raw={order}")
    return {"order_id": order.get("order_id"), "filled_qty": filled_qty,
            "avg_price": avg_price, "status": order.get("status", "unknown")}


async def close_position(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """LIVE - market-closes whatever position currently exists in symbol (reduce_only)."""
    pos = await get_position(session, symbol)
    if pos is None:
        return None
    info = await _market_info(session, symbol)
    decimals = _decimals_of(info["lot_size"])
    amount = _round_to_lot(abs(pos["qty"]), info["lot_size"])
    side = "ask" if pos["qty"] > 0 else "bid"
    payload = {
        "symbol": symbol, "reduce_only": True, "amount": f"{amount:.{decimals}f}",
        "side": side, "slippage_percent": "0.5", "client_order_id": str(uuid.uuid4()),
    }
    request = _sign("create_market_order", payload)
    async with session.post(f"{BASE_URL}/orders/create_market", json=request,
                             timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    if not data.get("success"):
        raise BrokerError(f"Pacifica close error: {data}")
    order = data.get("data", {}) or {}
    log.info(f"LIVE CLOSE {symbol} side={side} amount={amount} raw={order}")
    return {"order_id": order.get("order_id"),
            "filled_qty": float(order.get("filled_amount") or amount),
            "avg_price": float(order.get("average_filled_price") or 0), "status": order.get("status", "unknown")}
