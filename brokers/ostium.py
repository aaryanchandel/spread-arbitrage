"""
Ostium live broker - real on-chain perpetuals on Arbitrum via the official
ostium-python-sdk (wraps web3.py contract calls).

LIVE TRADING: every function below sends a real on-chain transaction. Reads:
- OSTIUM_WALLET_PRIVATE_KEY: a real wallet private key. Needs gas (ETH on
  Arbitrum) sitting in it, separate from the USDC used as trading collateral.
- OSTIUM_RPC_URL: an Arbitrum RPC endpoint (e.g. from Alchemy).

Asset codes are NOT hardcoded - SDK's get_pairs() is queried once and cached,
matching coin -> pair id by symbol, specifically to avoid the real-money risk
of a wrong hardcoded asset_type silently trading the wrong instrument.

KNOWN LIMITATIONS:
- On-chain confirmation latency (seconds) is a real characteristic of this
  exchange, not a bug - trade-execution calls are synchronous (send tx, wait
  for receipt) and run via asyncio.to_thread so they don't block the rest of
  the bot's polling.
- Fixed at OSTIUM_LEVERAGE (default 2x) for this build, sized so
  collateral = notional_usd / OSTIUM_LEVERAGE - tune deliberately once proven.
- avg_price on open/close is approximated as the reference book price passed
  in, not parsed from on-chain event logs - good enough for PnL bookkeeping,
  not for verifying execution quality.
"""
import asyncio
import logging
import os

log = logging.getLogger("brokers.ostium")

WALLET_PRIVATE_KEY = os.environ.get("OSTIUM_WALLET_PRIVATE_KEY", "")
RPC_URL = os.environ.get("OSTIUM_RPC_URL", "")
OSTIUM_LEVERAGE = float(os.environ.get("OSTIUM_LEVERAGE", "2"))
is_configured = bool(WALLET_PRIVATE_KEY and RPC_URL)

_sdk = None
_pair_id_cache: dict[str, int] = {}


class BrokerError(Exception):
    pass


def _client():
    global _sdk
    if _sdk is None:
        if not WALLET_PRIVATE_KEY or not RPC_URL:
            raise BrokerError("OSTIUM_WALLET_PRIVATE_KEY/OSTIUM_RPC_URL not set - refusing to trade")
        from ostium_python_sdk import NetworkConfig, OstiumSDK
        _sdk = OstiumSDK(NetworkConfig.mainnet(), WALLET_PRIVATE_KEY, RPC_URL)
    return _sdk


def _wallet_address() -> str:
    from eth_account import Account
    return Account.from_key(WALLET_PRIVATE_KEY).address


async def _resolve_pair_id(coin: str) -> int:
    if coin in _pair_id_cache:
        return _pair_id_cache[coin]
    sdk = _client()
    pairs = await sdk.subgraph.get_pairs()
    for p in pairs:
        _pair_id_cache[p["from"]] = int(p["id"])
    if coin not in _pair_id_cache:
        raise BrokerError(f"No Ostium pair found for coin '{coin}' (checked {len(pairs)} pairs)")
    return _pair_id_cache[coin]


async def get_position(session, coin: str) -> dict | None:
    """Read-only - queries Ostium's subgraph for this wallet's open trades."""
    sdk = _client()
    pair_id = await _resolve_pair_id(coin)
    trades = await sdk.subgraph.get_open_trades(_wallet_address())
    for t in trades:
        if int(t["pair"]["id"]) == pair_id:
            collateral = float(t.get("collateral", 0) or 0)
            leverage = float(t.get("leverage", 1) or 1)
            qty = collateral * leverage
            side = "long" if t.get("buy") else "short"
            return {"qty": qty if side == "long" else -qty, "side": side,
                    "entry_price": float(t.get("openPrice", 0) or 0), "_raw": t}
    return None


async def place_market_order(session, coin: str, side: str, notional_usd: float, ref_price: float) -> dict:
    """LIVE - opens a real on-chain position. side: 'BUY' (long) or 'SELL' (short)."""
    pair_id = await _resolve_pair_id(coin)
    collateral = notional_usd / OSTIUM_LEVERAGE

    def _do():
        sdk = _client()
        trade_params = {
            "collateral": collateral,
            "leverage": OSTIUM_LEVERAGE,
            "asset_type": pair_id,
            "direction": side == "BUY",
            "order_type": "MARKET",
        }
        return sdk.ostium.perform_trade(trade_params, at_price=ref_price)

    receipt = await asyncio.to_thread(_do)
    tx_hash = receipt.get("transactionHash")
    tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    log.info(f"LIVE ORDER {coin} {side} notional=${notional_usd} collateral=${collateral:.2f} "
             f"leverage={OSTIUM_LEVERAGE}x pair_id={pair_id} tx={tx_hash}")
    return {"order_id": tx_hash, "filled_qty": notional_usd / ref_price,
            "avg_price": ref_price, "status": "FILLED"}


async def close_position(session, coin: str) -> dict | None:
    """LIVE - closes the real on-chain position in coin, if any."""
    pos = await get_position(session, coin)
    if pos is None:
        return None
    trade = pos["_raw"]

    def _do():
        sdk = _client()
        return sdk.ostium.close_trade(trade["pair"]["id"], trade["index"])

    receipt = await asyncio.to_thread(_do)
    tx_hash = receipt.get("transactionHash")
    tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    log.info(f"LIVE CLOSE {coin} tx={tx_hash}")
    return {"order_id": tx_hash, "filled_qty": abs(pos["qty"]),
            "avg_price": pos["entry_price"], "status": "FILLED"}
