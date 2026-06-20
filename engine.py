"""
Paper-trading spread-arb engine.

Detection uses MID-price spread (consistent with the 90-day backtest, so
results are comparable). Execution - entries and exits - uses REAL bid/ask:
  - Opening the long leg fills at that exchange's ASK.
  - Opening the short leg fills at that exchange's BID.
  - Closing the long leg fills at that exchange's BID.
  - Closing the short leg (buying back) fills at that exchange's ASK.
This is what makes the front-test "real market", not the idealized mid-price
backtest - bid/ask spread is itself a cost the strategy has to clear.
"""
import itertools
import logging
import time

import config
import db

log = logging.getLogger("engine")

PAIRS_PER_COIN = {
    coin: list(itertools.combinations(exchs, 2))
    for coin, exchs in config.EXCHANGES_PER_COIN.items()
}


def round_trip_cost_pct(a: str, b: str) -> float:
    fee_a = config.TAKER_FEE.get(a, 0.0005)
    fee_b = config.TAKER_FEE.get(b, 0.0005)
    return (fee_a + fee_b) * 2 * 100  # open+close both legs, to %


def safe_leverage(coin: str, a: str, b: str) -> float:
    levs = config.SYMBOL_LEVERAGE.get(coin, {})
    return min(levs.get(a, 3), levs.get(b, 3))


class PaperEngine:
    def __init__(self):
        self.state = {}   # (coin, a, b) -> dict(direction, pos_id, entry_mid_spread, is_reversal)
        self.books = {}   # exch -> {coin: (bid, ask)}
        self.margin_per_pair = (config.PAPER_CAPITAL_USD * config.DEPLOY_FRACTION) / config.N_CONCURRENT_PAIRS
        self.active_keys = self._select_active_pairs()
        log.info(f"Tracking {len(self.active_keys)} coin x exchange-pair combinations")

    def _select_active_pairs(self):
        keys = []
        for coin, pairs in PAIRS_PER_COIN.items():
            for a, b in pairs:
                keys.append((coin, a, b))
        return keys

    def update_books(self, exch: str, book: dict):
        self.books[exch] = book

    def current_equity(self) -> float:
        realized = db.get_realized_pnl_total()
        return config.PAPER_CAPITAL_USD + realized

    def tick(self):
        for coin, a, b in self.active_keys:
            book_a = self.books.get(a, {}).get(coin)
            book_b = self.books.get(b, {}).get(coin)
            if not book_a or not book_b:
                continue
            self._evaluate(coin, a, b, book_a, book_b)

    def _mid(self, book):
        return (book[0] + book[1]) / 2

    def _evaluate(self, coin, a, b, book_a, book_b):
        key = (coin, a, b)
        mid_a, mid_b = self._mid(book_a), self._mid(book_b)
        mid_avg = (mid_a + mid_b) / 2
        if mid_avg <= 0:
            return
        spread_pct = (mid_a - mid_b) / mid_avg * 100  # positive => a richer than b

        rt_cost = round_trip_cost_pct(a, b)
        threshold = rt_cost * config.ENTRY_MULT
        st = self.state.get(key)

        if st is None:
            if abs(spread_pct) > threshold:
                self._open(coin, a, b, book_a, book_b, spread_pct, rt_cost, is_reversal=False)
        else:
            direction = st["direction"]  # 'long_a' or 'long_b'
            converged = (direction == "long_a" and spread_pct >= 0) or (direction == "long_b" and spread_pct <= 0)
            if converged:
                self._close(key, coin, a, b, book_a, book_b, spread_pct, rt_cost)
                # check immediate reversal continuation
                if abs(spread_pct) > threshold:
                    new_dir_is_a_cheap = spread_pct < 0  # a cheaper than b now -> long a
                    if (new_dir_is_a_cheap and direction == "long_b") or (not new_dir_is_a_cheap and direction == "long_a"):
                        self._open(coin, a, b, book_a, book_b, spread_pct, rt_cost, is_reversal=True)

    def _open(self, coin, a, b, book_a, book_b, spread_pct, rt_cost, is_reversal):
        key = (coin, a, b)
        # spread_pct > 0 means a is richer -> short a, long b. spread_pct < 0 -> long a, short b.
        if spread_pct > 0:
            direction = "long_b"
            long_exch, short_exch = b, a
            entry_long_px = book_b[1]   # buy at ask
            entry_short_px = book_a[0]  # sell at bid
        else:
            direction = "long_a"
            long_exch, short_exch = a, b
            entry_long_px = book_a[1]
            entry_short_px = book_b[0]

        lev = safe_leverage(coin, a, b)
        notional = (self.margin_per_pair / 2) * lev
        pair_label = f"{a.upper()}-{b.upper()}"
        kind = "reversal" if is_reversal else "convergence"

        pos_id = db.open_position(
            symbol=coin, pair=pair_label, direction=f"long_{long_exch}_short_{short_exch}",
            entry_long_px=entry_long_px, entry_short_px=entry_short_px,
            entry_mid_spread_pct=spread_pct, notional_usd=notional, leverage=lev, kind=kind,
        )
        self.state[key] = {"direction": direction, "pos_id": pos_id, "long_exch": long_exch, "short_exch": short_exch}
        log.info(f"OPEN  {coin:10} {pair_label:10} {direction:8} spread={spread_pct:+.4f}% "
                  f"notional=${notional:.0f} lev={lev}x kind={kind}")

    def _close(self, key, coin, a, b, book_a, book_b, spread_pct, rt_cost):
        st = self.state.pop(key)
        long_exch, short_exch = st["long_exch"], st["short_exch"]
        long_book = book_a if long_exch == a else book_b
        short_book = book_a if short_exch == a else book_b
        exit_long_px = long_book[0]    # sell long position at bid
        exit_short_px = short_book[1]  # buy back short at ask

        pos = next((p for p in db.get_open_positions() if p["id"] == st["pos_id"]), None)
        fee_usd = (pos["notional_usd"] * rt_cost / 100) if pos else 0.0

        net_pnl = db.close_position(st["pos_id"], exit_long_px, exit_short_px, spread_pct, fee_usd)
        log.info(f"CLOSE {coin:10} {a.upper()}-{b.upper():10} spread={spread_pct:+.4f}% "
                  f"net_pnl=${net_pnl:+.2f}" if net_pnl is not None else "CLOSE (no matching position)")

    def snapshot_equity(self, note=""):
        eq = self.current_equity()
        open_n = len(self.state)
        db.record_equity_snapshot(eq, open_n, note)
        return eq, open_n
