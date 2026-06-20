"""
Configuration - symbol universe, fee schedule, leverage map, capital allocation.
Defaults are taken directly from the 90-day backtest (arb_90d_leverage_sized.csv)
so the front-test is an apples-to-apples real-market continuation of it.
"""
import os

# ── capital / sizing ────────────────────────────────────────────────────────
PAPER_CAPITAL_USD = float(os.environ.get("PAPER_CAPITAL_USD", "1000"))
DEPLOY_FRACTION = float(os.environ.get("DEPLOY_FRACTION", "0.60"))   # % of capital posted as margin
POLL_INTERVAL_SECS = int(os.environ.get("POLL_INTERVAL_SECS", "10"))

# ── fee schedule (taker, standard tier) ─────────────────────────────────────
TAKER_FEE = {"hl": 0.00035, "pac": 0.00020, "bn": 0.00040, "ost": 0.00010}
# Ostium fee unconfirmed publicly - placeholder, override via env if you find their real schedule
ENTRY_MULT = 1.5  # require executable edge to exceed round-trip cost by this multiple before entering

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
