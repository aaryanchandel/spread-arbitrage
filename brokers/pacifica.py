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
import os
import time
import uuid

import aiohttp

log = logging.getLogger("brokers.pacifica")

BASE_URL = "https://api.pacifica.fi/api/v1"
AGENT_PRIVATE_KEY = os.environ.get("PACIFICA_AGENT_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.environ.get("PACIFICA_ACCOUNT_ADDRESS", "")
is_configured = bool(AGENT_PRIVATE_KEY and ACCOUNT_ADDRESS)

_keypair = None


class BrokerError(Exception):
    pass


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
    amount = notional_usd / ref_price
    payload = {
        "symbol": symbol, "reduce_only": False, "amount": f"{amount:.6f}",
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
    log.info(f"LIVE ORDER {symbol} {side} amount={amount:.6f} avgPrice={avg_price} raw={order}")
    return {"order_id": order.get("order_id"), "filled_qty": filled_qty,
            "avg_price": avg_price, "status": order.get("status", "unknown")}


async def close_position(session: aiohttp.ClientSession, symbol: str) -> dict | None:
    """LIVE - market-closes whatever position currently exists in symbol (reduce_only)."""
    pos = await get_position(session, symbol)
    if pos is None:
        return None
    side = "ask" if pos["qty"] > 0 else "bid"
    payload = {
        "symbol": symbol, "reduce_only": True, "amount": f"{abs(pos['qty']):.6f}",
        "side": side, "slippage_percent": "0.5", "client_order_id": str(uuid.uuid4()),
    }
    request = _sign("create_market_order", payload)
    async with session.post(f"{BASE_URL}/orders/create_market", json=request,
                             timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()
    if not data.get("success"):
        raise BrokerError(f"Pacifica close error: {data}")
    order = data.get("data", {}) or {}
    log.info(f"LIVE CLOSE {symbol} side={side} amount={abs(pos['qty']):.6f} raw={order}")
    return {"order_id": order.get("order_id"),
            "filled_qty": float(order.get("filled_amount") or abs(pos["qty"])),
            "avg_price": float(order.get("average_filled_price") or 0), "status": order.get("status", "unknown")}
