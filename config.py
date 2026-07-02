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
POLL_INTERVAL_SECS = float(os.environ.get("POLL_INTERVAL_SECS", "0.5"))

# ── fee schedule (standard, non-VIP tier) ───────────────────────────────────
# Only real, confirmed costs - no synthetic slippage padding. Entry always
# pays taker fees - it requires an actually-crossed order book (see engine.py)
# that may vanish in seconds, so there's no time to rest a maker order there.
TAKER_FEE = {
    "hl": 0.00035, "pac": 0.00020, "ost": 0.00010, "aster": 0.00035,
}

# Maker fees - used only on the EXIT leg, where there's no fleeting-opportunity
# pressure: closing a position that's already profitable can afford to rest a
# limit order for a bit and capture the lower maker rate instead of crossing
# the spread with a taker order again. HL's maker rate is its published
# standard-tier rate. Pacifica and Ostium don't publicly document a separate
# maker rate - estimated here as half of their taker fee; verify before relying
# on this for real capital.
MAKER_FEE = {
    "hl": 0.00010, "pac": 0.00010, "ost": 0.00005, "aster": 0.00010,
}

# How long to rest maker exit orders before giving up. NOTE: this also adds
# real risk, not just a fee-timing tradeoff - the spread itself can decay
# back to unprofitable while waiting, even with both legs still hedged
# against the underlying. Kept short by default for exactly that reason; if
# it times out AND is no longer profitable, the position is NOT force-closed
# at a loss - see engine.py's EXIT-ABORT path.
MAKER_EXIT_TIMEOUT_SECS = int(os.environ.get("MAKER_EXIT_TIMEOUT_SECS", "20"))

# ── risk parameters (99.99th-percentile, data-driven from the 90-day backtest) ─
# Extremely wide by design - these only fire on true tail events. The primary
# exit is still taking profit as soon as unwinding is net-positive; these exist
# only to bound the case where a position simply never gets there.
RISK_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "risk_params.json")
try:
    with open(RISK_PARAMS_PATH) as f:
        _RISK_PARAMS = json.load(f)
except FileNotFoundError:
    _RISK_PARAMS = {}

# Conservative defaults for any coin x pair combo with no backtested history
# (mainly Ostium pairs, since Ostium has no historical API at all).
DEFAULT_STOP_LOSS_SPREAD_PCT = 1.00
DEFAULT_MAX_HOLD_HOURS = 96.0
DEFAULT_MEAN_SPREAD_PCT = 0.0
DEFAULT_STD_SPREAD_PCT = 0.15

# Extra buffer on top of the 99.99th-percentile stop-loss - widen it further so
# it only fires on something genuinely outside the historical record, not the
# historical extreme itself.
STOP_LOSS_BUFFER_MULT = float(os.environ.get("STOP_LOSS_BUFFER_MULT", "1.05"))


def get_risk_params(coin: str, a: str, b: str) -> tuple[float, float]:
    """Returns (stop_loss_spread_pct, max_hold_hours) - wide, 99.99th-percentile sized
    (stop-loss further padded by STOP_LOSS_BUFFER_MULT)."""
    rp = _RISK_PARAMS.get(f"{coin}|{a}|{b}") or _RISK_PARAMS.get(f"{coin}|{b}|{a}")
    if rp is None:
        return DEFAULT_STOP_LOSS_SPREAD_PCT * STOP_LOSS_BUFFER_MULT, DEFAULT_MAX_HOLD_HOURS
    return rp["p9999_abs_spread_pct"] * STOP_LOSS_BUFFER_MULT, rp["p9999_hold_hours"]


def get_baseline_spread_stats(coin: str, a: str, b: str) -> tuple[float, float]:
    """Returns (historical_mean_spread_pct, historical_std_spread_pct) - the z-score
    baseline used when there isn't yet enough live data to compute one ourselves."""
    rp = _RISK_PARAMS.get(f"{coin}|{a}|{b}")
    if rp is not None:
        return rp["mean_spread_pct"], rp["std_spread_pct"]
    rp_flip = _RISK_PARAMS.get(f"{coin}|{b}|{a}")
    if rp_flip is not None:
        return -rp_flip["mean_spread_pct"], rp_flip["std_spread_pct"]
    return DEFAULT_MEAN_SPREAD_PCT, DEFAULT_STD_SPREAD_PCT


# ── z-score entry filter ─────────────────────────────────────────────────────
# On top of the crossed-book + fee requirement, also require the current
# mid-spread to be a statistically unusual dislocation relative to its own
# recent behavior - not just noise that happens to clear the fee threshold.
# Fewer trades, each with a real statistical edge behind it, not just a
# margin-of-fees edge. Live rolling stats are used once enough data has
# accumulated; until then it falls back to the 90-day historical baseline
# above, so there's no cold-start gap after every redeploy.
Z_ENTRY_THRESHOLD = float(os.environ.get("Z_ENTRY_THRESHOLD", "4.5"))
Z_ROLLING_WINDOW = int(os.environ.get("Z_ROLLING_WINDOW", "7200"))   # ~1h at 0.5s polling
Z_MIN_LIVE_OBS = int(os.environ.get("Z_MIN_LIVE_OBS", "600"))        # ~5min at 0.5s polling

# Exit side of the z-score: hold the position until the spread has actually
# reverted to (near) its mean - |z| at or below this - instead of exiting at
# the first tick that covers fees. Entering at z>=Z_ENTRY_THRESHOLD and
# exiting near z=0 captures the full average dislocation, which is the most
# mean-reversion can statistically pay. Order-book profitability (after ALL
# fees, at the position's real size) remains the hard floor - z reverting
# while the book can't fill a profitable unwind still does NOT exit. Risk
# exits (stop_loss / max_hold) ignore this entirely.
Z_EXIT_THRESHOLD = float(os.environ.get("Z_EXIT_THRESHOLD", "0.5"))

# ── adaptive per-symbol cooldown ─────────────────────────────────────────────
# If a coin loses LOSS_STREAK_THRESHOLD trades in a row (its own most recent
# closed trades, across any exchange-pair), new entries on that coin are
# paused for LOSS_STREAK_COOLDOWN_HOURS. This is adaptive, not a static
# blacklist: a single win immediately resets the streak to zero, and even
# without a win the cooldown itself expires and the coin gets a fresh chance
# - if it keeps losing it gets re-excluded automatically, if conditions
# improve it trades again automatically. No manual list to maintain.
LOSS_STREAK_THRESHOLD = int(os.environ.get("LOSS_STREAK_THRESHOLD", "2"))
LOSS_STREAK_COOLDOWN_HOURS = float(os.environ.get("LOSS_STREAK_COOLDOWN_HOURS", "24"))

# ── symbol universe + leverage (from backtest, p99 4h move sized) ──────────
# coin -> {exchange -> max_safe_leverage}
# Aster supports far higher exchange-side max leverage than any of these
# coins' volatility-sized safe leverage below, so it inherits the same
# vol-derived number as hl/pac rather than a separate exchange cap
# (unlike Ostium, whose own leverage limits are the actual binding constraint
# for some coins - e.g. BTC's 10x).
# Bybit, OKX, and Binance were tried and dropped (see README "Removed
# exchanges") - Bybit/OKX never crossed against anything in the pool, just
# added redundant liquidity at Binance-identical prices; Binance was removed
# separately due to its IP-whitelisting requirement being impractical to
# satisfy reliably from Railway's dynamic egress IPs (static IP is a
# Pro-plan-only feature).
SYMBOL_LEVERAGE = {
    "BTC":      {"hl": 20, "pac": 20, "ost": 10, "aster": 20},
    "ETH":      {"hl": 20, "pac": 20, "ost": 10, "aster": 20},
    "SOL":      {"hl": 15, "pac": 15, "ost": 8, "aster": 15},
    "MEGA":     {"hl": 2.4, "pac": 2.4, "aster": 2.4},
    "CRV":      {"hl": 4.6, "pac": 4.6, "aster": 4.6},
    "XPL":      {"hl": 2.7, "pac": 2.7, "aster": 2.7},
    "NEAR":     {"hl": 3.1, "pac": 3.1, "aster": 3.1},
    "LIT":      {"hl": 2.8, "pac": 2.8, "aster": 2.8},
    "MON":      {"hl": 3.0, "pac": 3.0, "aster": 3.0},
    "ZRO":      {"hl": 3.5, "pac": 3.5, "aster": 3.5},
    "JUP":      {"hl": 3.9, "pac": 3.9, "aster": 3.9},
    "WLD":      {"hl": 2.1, "pac": 2.1, "aster": 2.1},
    "FARTCOIN": {"hl": 3.3, "pac": 3.3, "aster": 3.3},
    "AAVE":     {"hl": 5, "pac": 5, "aster": 5},
    "ZEC":      {"hl": 5, "pac": 5, "aster": 5},
    "ADA":      {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
    "XRP":      {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
    "BNB":      {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
    "TRX":      {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
    "LINK":     {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
    "HYPE":     {"hl": 5, "pac": 5, "ost": 5, "aster": 5},
}

# Full exchange-native symbol per coin
ASTER_SYMBOL = {coin: f"{coin}USDT" for coin in SYMBOL_LEVERAGE}

# Which exchanges to track for each coin (Ostium only for its known crypto set)
EXCHANGES_PER_COIN = {
    coin: (["hl", "pac", "aster"] + (["ost"] if "ost" in levs else []))
    for coin, levs in SYMBOL_LEVERAGE.items()
}

ALL_COINS = list(SYMBOL_LEVERAGE.keys())

# de-duplicated pairs to monitor, diversified basket
N_CONCURRENT_PAIRS = int(os.environ.get("N_CONCURRENT_PAIRS", "10"))

# ── LIVE TRADING (real money - defaults OFF) ────────────────────────────────
# Master switch. Defaults false so every deploy is paper-only unless someone
# explicitly flips this in Railway's Variables tab.
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"

# Comma-separated exchange names with a working broker (see brokers/__init__.py)
# AND credentials configured. A pair only trades live if BOTH its legs are in
# this set - everything else stays paper, regardless of LIVE_TRADING, since
# the strategy is inherently two-legged and one real leg with no hedge is a
# directional bet, not arbitrage. e.g. LIVE_EXCHANGES=aster enables nothing
# yet (needs a second exchange); LIVE_EXCHANGES=aster,hl enables only
# HL-Aster pairs once both brokers exist and are credentialed.
LIVE_EXCHANGES = {e.strip() for e in os.environ.get("LIVE_EXCHANGES", "").split(",") if e.strip()}

# Hard cap on real notional committed per exchange leg - enforced in code at
# order time, not just a suggestion. Matches the "$20 per exchange" live
# experiment; raise deliberately once that's proven out, don't rely on this
# alone for sizing (margin_per_pair / leverage still apply on top).
PER_EXCHANGE_CAPITAL_USD = float(os.environ.get("PER_EXCHANGE_CAPITAL_USD", "20"))

# Instant kill switch - set true in Railway to stop all NEW live entries
# without redeploying or touching code. Existing open live positions are
# unaffected (still managed by the normal stop-loss/max-hold/exit logic) -
# this only blocks opening new ones.
KILL_SWITCH = os.environ.get("KILL_SWITCH", "false").lower() == "true"

# If one leg of a live spread fills and the other doesn't, retry the missing
# leg for this many seconds before giving up and flattening the filled leg
# at market (accepting the small loss rather than running unhedged).
LEG_FILL_RETRY_SECS = float(os.environ.get("LEG_FILL_RETRY_SECS", "5"))

# Per-exchange circuit breaker: after this many CONSECUTIVE failed live-open
# attempts involving the same exchange (margin exhausted, broken delegation,
# any leg failure), block NEW live entries on that exchange for the cooldown.
# Failure modes like these don't fix themselves between ticks - retrying every
# crossed-book tick just burns flatten fees on the leg that DID fill.
EXCHANGE_FAIL_STREAK = int(os.environ.get("EXCHANGE_FAIL_STREAK", "3"))
EXCHANGE_FAIL_COOLDOWN_MINS = float(os.environ.get("EXCHANGE_FAIL_COOLDOWN_MINS", "30"))
