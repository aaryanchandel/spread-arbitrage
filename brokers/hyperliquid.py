"""
Hyperliquid live broker - real signed order placement via the official
hyperliquid-python-sdk (wallet-signed, not a REST API key/secret).

LIVE TRADING: every function below can move real money. Reads:
- HL_API_WALLET_PRIVATE_KEY: a dedicated, trade-only "API wallet" created
  at app.hyperliquid.xyz/API - NOT your main wallet's key. An API wallet
  can place/cancel orders on your behalf but cannot withdraw funds.
- HL_ACCOUNT_ADDRESS: your main account address that API wallet trades on
  behalf of.

The SDK is synchronous (uses `requests` internally) - every call here runs
in a thread via asyncio.to_thread so it doesn't block the event loop the
rest of the bot's book-polling depends on.
"""
import asyncio
import logging
import math
import os

log = logging.getLogger("brokers.hyperliquid")

API_WALLET_PRIVATE_KEY = os.environ.get("HL_API_WALLET_PRIVATE_KEY", "").strip()
ACCOUNT_ADDRESS = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
is_configured = bool(API_WALLET_PRIVATE_KEY and ACCOUNT_ADDRESS)

_exchange = None
_info = None
_sz_decimals_cache: dict[str, int] = {}


class BrokerError(Exception):
    pass


def _client():
    global _exchange, _info
    if _exchange is None:
        if not API_WALLET_PRIVATE_KEY or not ACCOUNT_ADDRESS:
            raise BrokerError("HL_API_WALLET_PRIVATE_KEY/HL_ACCOUNT_ADDRESS not set - refusing to trade")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        wallet = Account.from_key(API_WALLET_PRIVATE_KEY)
        _info = Info(constants.MAINNET_API_URL, skip_ws=True)
        _exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=ACCOUNT_ADDRESS)
    return _exchange, _info


def _user_state():
    _, info = _client()
    return info.user_state(ACCOUNT_ADDRESS)


async def get_margin_summary(session=None) -> dict:
    """Read-only - account value / margin used. Safe to call any time to verify connectivity."""
    state = await asyncio.to_thread(_user_state)
    return state.get("marginSummary", {})


async def get_position(session, coin: str) -> dict | None:
    """Read-only. Returns {"qty", "side", "entry_price"} or None if flat."""
    def _fetch():
        state = _user_state()
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            if p.get("coin") == coin:
                qty = float(p.get("szi", 0))
                if abs(qty) > 1e-12:
                    return {"qty": qty, "side": "long" if qty > 0 else "short",
                            "entry_price": float(p.get("entryPx", 0))}
        return None
    return await asyncio.to_thread(_fetch)


async def get_all_positions(session=None) -> dict:
    """Read-only - every currently open position on this account, keyed by
    coin. Used at startup to find real positions the bot isn't tracking
    (e.g. left over from a crash mid-trade) so they can be flattened."""
    def _fetch():
        state = _user_state()
        out = {}
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            qty = float(p.get("szi", 0))
            if abs(qty) > 1e-12:
                out[p["coin"]] = {"qty": qty, "side": "long" if qty > 0 else "short",
                                   "entry_price": float(p.get("entryPx", 0))}
        return out
    return await asyncio.to_thread(_fetch)


async def set_leverage(session, coin: str, leverage: int) -> None:
    """Sets ISOLATED leverage for this coin before opening a position - isolated
    (not cross) so a liquidation on one position can't bleed margin away from
    other unrelated live positions on the same account."""
    def _do():
        exchange, _ = _client()
        exchange.update_leverage(int(leverage), coin, is_cross=False)
    await asyncio.to_thread(_do)


def _sz_decimals(coin: str) -> int:
    """HL rejects order sizes with more decimal places than an asset's
    szDecimals allows (raises 'float_to_wire causes rounding') - fetched
    once from info.meta() and cached, never hardcoded per-coin."""
    if not _sz_decimals_cache:
        _, info = _client()
        meta = info.meta()
        for a in meta.get("universe", []):
            _sz_decimals_cache[a["name"]] = a["szDecimals"]
    if coin not in _sz_decimals_cache:
        raise BrokerError(f"No HL meta entry for coin '{coin}' - can't determine size precision")
    return _sz_decimals_cache[coin]


def _extract_fill(result: dict, ref_price: float) -> tuple[float, float, object]:
    statuses = result.get("response", {}).get("data", {}).get("statuses", [{}])
    filled = statuses[0].get("filled", {}) if statuses else {}
    avg_price = float(filled.get("avgPx", ref_price)) if filled else ref_price
    filled_qty = float(filled.get("totalSz", 0)) if filled else 0.0
    oid = filled.get("oid")
    return avg_price, filled_qty, oid


async def place_market_order(session, coin: str, side: str, notional_usd: float, ref_price: float) -> dict:
    """LIVE - places a real market order (aggressive IOC limit under the hood, via
    the SDK's market_open). side: 'BUY' or 'SELL'."""
    raw_sz = notional_usd / ref_price
    is_buy = side == "BUY"

    def _do():
        exchange, _ = _client()
        decimals = _sz_decimals(coin)
        mult = 10 ** decimals
        sz = math.floor(raw_sz * mult) / mult
        if sz <= 0:
            raise BrokerError(f"{coin}: order size rounds to 0 at szDecimals={decimals} "
                               f"(notional=${notional_usd}, ref_price={ref_price})")
        return sz, exchange.market_open(coin, is_buy, sz)

    sz, result = await asyncio.to_thread(_do)
    avg_price, filled_qty, oid = _extract_fill(result, ref_price)
    if filled_qty <= 0:
        raise BrokerError(f"HL order for {coin} did not report a fill: {result}")
    log.info(f"LIVE ORDER {coin} {side} sz={sz:.6f} avgPx={avg_price} oid={oid}")
    return {"order_id": oid, "filled_qty": filled_qty, "avg_price": avg_price, "status": "FILLED"}


async def close_position(session, coin: str) -> dict | None:
    """LIVE - market-closes whatever position currently exists in coin."""
    pos = await get_position(session, coin)
    if pos is None:
        return None

    def _do():
        exchange, _ = _client()
        return exchange.market_close(coin)

    result = await asyncio.to_thread(_do)
    avg_price, filled_qty, oid = _extract_fill(result, pos["entry_price"])
    log.info(f"LIVE CLOSE {coin} oid={oid} avgPx={avg_price}")
    return {"order_id": oid, "filled_qty": filled_qty or abs(pos["qty"]), "avg_price": avg_price, "status": "FILLED"}
