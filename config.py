"""
Configuration - symbol universe, fee schedule, leverage map, capital allocation.
Defaults are taken directly from the 90-day backtest (arb_90d_leverage_sized.csv)
so the front-test is an apples-to-apples real-market continuation of it.
"""
import json
import os

# ── capital / sizing ────────────────────────────────────────────────────────
PAPER_CAPITAL_USD = float(os.environ.get("PAPER_CAPITAL_USD", "1000"))
DEPLOY_FRACTION = float(os.environ.get("DEPLOY_FRACTION", "0.60"))   # % of capital posted as margin
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "10"))

# ── fee schedule (standard, non-VIP tier) ───────────────────────────────────
# Only real, confirmed costs - no synthetic slippage padding. Entry always
# pays taker fees - it requires an actually-crossed order book (see engine.py)
# that may vanish in seconds, so there's no time to rest a maker order there.
TAKER_FEE = {"hl": 0.00035, "pac": 0.00020, "bn": 0.00040, "ost": 0.00010}

# Maker fees - used only on the EXIT leg, where there's no fleeting-opportunity
# pressure: closing a position that's already profitable can afford to rest a
# limit order for a bit and capture the lower maker rate instead of crossing
# the spread with a taker order again. HL and BN maker rates are their published
# standard-tier rates. Pacifica and Ostium don't publicly document a separate
# maker rate - estimated here as half of their taker fee; verify before relying
# on this for real capital.
MAKER_FEE = {"hl": 0.00010, "pac": 0.00010, "bn": 0.00020, "ost": 0.00005}

# How long to rest maker exit orders before giving up and force-closing at
# market (taker) instead. Both legs stay open and hedged against each other
# the whole time, so this delay adds fee-timing risk, not directional risk.
MAKER_EXIT_TIMEOUT_SECS = int(os.environ.get("MAKER_EXIT_TIMEOUT_SECS", "60"))

# ── risk parameters (95th-percentile, data-driven from the 90-day backtest) ─
# Wide by design - these are tail safety nets, not the primary exit mechanism
# (the primary exit is taking profit as soon as unwinding is net-positive).
RISK_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "risk_params.json")
try:
    with open(RISK_PARAMS_PATH) as f:
        _RISK_PARAMS = json.load(f)
except FileNotFoundError:
    _RISK_PARAMS = {}

# Conservative defaults for any coin x pair combo with no backtested history
# (mainly Ostium pairs, since Ostium has no historical API at all).
DEFAULT_STOP_LOSS_SPREAD_PCT = 0.50
DEFAULT_MAX_HOLD_HOURS = 72.0


def get_risk_params(coin: str, a: str, b: str) -> tuple[float, float]:
    """Returns (stop_loss_spread_pct, max_hold_hours) - wide, 95th-percentile sized."""
    key = f"{coin}|{a}|{b}"
    alt_key = f"{coin}|{b}|{a}"
    rp = _RISK_PARAMS.get(key) or _RISK_PARAMS.get(alt_key)
    if rp is None:
        return DEFAULT_STOP_LOSS_SPREAD_PCT, DEFAULT_MAX_HOLD_HOURS
    return rp["p95_abs_spread_pct"], rp["p95_hold_hours"]

# ── symbol universe + leverage (from backtest, p99 4h move sized) ──────────
# coin -> {exchange -> max_safe_leverage}
SYMBOL_LEVERAGE = {
    "BTC":      {"hl": 20, "pac": 20, "bn": 20, "ost": 10},
    "ETH":      {"hl": 20, "pac": 20, "bn": 20, "ost": 10},
    "SOL":      {"hl": 15, "pac": 15, "bn": 15, "ost": 8},
    "MEGA":     {"hl": 2.4, "pac": 2.4, "bn": 2.4},
    "CRV":      {"hl": 4.6, "pac": 4.6, "bn": 4.6},
    "XPL":      {"hl": 2.7, "pac": 2.7, "bn": 2.7},
    "NEAR":     {"hl": 3.1, "pac": 3.1, "bn": 3.1},
    "LIT":      {"hl": 2.8, "pac": 2.8, "bn": 2.8},
    "MON":      {"hl": 3.0, "pac": 3.0, "bn": 3.0},
    "ZRO":      {"hl": 3.5, "pac": 3.5, "bn": 3.5},
    "JUP":      {"hl": 3.9, "pac": 3.9, "bn": 3.9},
    "WLD":      {"hl": 2.1, "pac": 2.1, "bn": 2.1},
    "FARTCOIN": {"hl": 3.3, "pac": 3.3, "bn": 3.3},
    "AAVE":     {"hl": 5, "pac": 5, "bn": 5},
    "ZEC":      {"hl": 5, "pac": 5, "bn": 5},
    "ADA":      {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
    "XRP":      {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
    "BNB":      {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
    "TRX":      {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
    "LINK":     {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
    "HYPE":     {"hl": 5, "pac": 5, "bn": 5, "ost": 5},
}

# Binance full symbol per coin (USDT perp)
BINANCE_SYMBOL = {coin: f"{coin}USDT" for coin in SYMBOL_LEVERAGE}

# Which exchanges to track for each coin (Ostium only for its known crypto set)
EXCHANGES_PER_COIN = {
    coin: (["hl", "pac", "bn"] + (["ost"] if "ost" in levs else []))
    for coin, levs in SYMBOL_LEVERAGE.items()
}

ALL_COINS = list(SYMBOL_LEVERAGE.keys())

# de-duplicated pairs to monitor, diversified basket
N_CONCURRENT_PAIRS = int(os.environ.get("N_CONCURRENT_PAIRS", "10"))
