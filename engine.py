"""
Paper-trading spread-arb engine - v2.

ENTRY: requires an actually-crossed order book, not a mid-price difference.
We only open a position when one exchange's BID is genuinely above the
other's ASK by more than the round-trip taker fee cost - i.e. you could buy
low on one book and sell high on the other, with the books themselves
proving it, not an inferred mid-price gap. No slippage buffer is added on
top of fees: crossing the books already prices in the real execution cost,
and the brief says not to double up on costs we aren't actually facing.

EXIT (primary): take profit as soon as unwinding the position right now -
selling the long leg at its current bid, buying back the short leg at its
current ask, net of the full round-trip fee - would be break-even or better.
This is also bid/ask-driven, not mid-price-driven.

EXIT (safety nets, wide by design): a 95th-percentile stop-loss and a
95th-percentile max-hold, both sized from the 90-day backtest's own
historical distributions (risk_params.json). These exist only to bound the
tail case where a position never reaches a profitable unwind - they are not
the primary exit logic.
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
    """Real taker fees only, both legs, open + close. No synthetic slippage padding."""
    fee_a = config.TAKER_FEE.get(a, 0.0005)
    fee_b = config.TAKER_FEE.get(b, 0.0005)
    return (fee_a + fee_b) * 2 * 100  # to %


def safe_leverage(coin: str, a: str, b: str) -> float:
    levs = config.SYMBOL_LEVERAGE.get(coin, {})
    return min(levs.get(a, 3), levs.get(b, 3))


class PaperEngine:
    def __init__(self):
        self.state = {}   # (coin, a, b) -> dict(direction, pos_id, long_exch, short_exch, entry_mid_spread, opened_at)
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
        st = self.state.get(key)

        if st is None:
            self._maybe_open(coin, a, b, book_a, book_b)
        else:
            self._maybe_close(key, coin, a, b, book_a, book_b)

    # ── ENTRY: real crossed-book arbitrage only ─────────────────────────────
    def _maybe_open(self, coin, a, b, book_a, book_b):
        bid_a, ask_a = book_a
        bid_b, ask_b = book_b
        mid = (self._mid(book_a) + self._mid(book_b)) / 2
        if mid <= 0:
            return

        rt_cost = round_trip_cost_pct(a, b)

        # buy A at its ask, sell B at its bid - profitable only if B's bid clears A's ask + costs
        edge_long_a_short_b = (bid_b - ask_a) / mid * 100
        # buy B at its ask, sell A at its bid
        edge_long_b_short_a = (bid_a - ask_b) / mid * 100

        if edge_long_a_short_b > rt_cost and edge_long_a_short_b >= edge_long_b_short_a:
            self._open(coin, a, b, "long_a", long_exch=a, short_exch=b,
                       entry_long_px=ask_a, entry_short_px=bid_b, mid=mid, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_a_short_b)
        elif edge_long_b_short_a > rt_cost:
            self._open(coin, a, b, "long_b", long_exch=b, short_exch=a,
                       entry_long_px=ask_b, entry_short_px=bid_a, mid=mid, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_b_short_a)

    def _open(self, coin, a, b, direction, long_exch, short_exch, entry_long_px, entry_short_px, mid, rt_cost, crossed_edge_pct):
        key = (coin, a, b)
        mid_a = self._mid(self.books[a][coin])
        mid_b = self._mid(self.books[b][coin])
        entry_mid_spread_pct = (mid_a - mid_b) / mid * 100

        lev = safe_leverage(coin, a, b)
        notional = (self.margin_per_pair / 2) * lev
        pair_label = f"{a.upper()}-{b.upper()}"

        pos_id = db.open_position(
            symbol=coin, pair=pair_label, direction=f"long_{long_exch}_short_{short_exch}",
            entry_long_px=entry_long_px, entry_short_px=entry_short_px,
            entry_mid_spread_pct=entry_mid_spread_pct, notional_usd=notional, leverage=lev, kind="arb",
        )
        self.state[key] = {
            "direction": direction, "pos_id": pos_id,
            "long_exch": long_exch, "short_exch": short_exch,
            "entry_long_px": entry_long_px, "entry_short_px": entry_short_px,
            "notional_usd": notional, "entry_mid_spread_pct": entry_mid_spread_pct,
            "opened_at": time.time(),
        }
        stop_loss_pct, max_hold_h = config.get_risk_params(coin, a, b)
        net_edge_after_fees = crossed_edge_pct - rt_cost
        log.info(f"OPEN  {coin:10} {pair_label:10} long_{long_exch}/short_{short_exch} "
                  f"crossed_edge={crossed_edge_pct:+.4f}% net_after_fees={net_edge_after_fees:+.4f}% "
                  f"notional=${notional:.0f} lev={lev}x stop@{stop_loss_pct:.3f}% max_hold={max_hold_h:.1f}h")

    # ── EXIT: take profit on real unwind P&L, else stop-loss / max-hold ────
    def _maybe_close(self, key, coin, a, b, book_a, book_b):
        st = self.state[key]
        long_exch, short_exch = st["long_exch"], st["short_exch"]
        long_book = book_a if long_exch == a else book_b
        short_book = book_a if short_exch == a else book_b

        exit_long_px = long_book[0]    # sell long position at bid
        exit_short_px = short_book[1]  # buy back short at ask

        rt_cost = round_trip_cost_pct(a, b)
        fee_usd = st["notional_usd"] * rt_cost / 100

        long_pnl_pct = (exit_long_px - st["entry_long_px"]) / st["entry_long_px"]
        short_pnl_pct = (st["entry_short_px"] - exit_short_px) / st["entry_short_px"]
        projected_net_pnl = (long_pnl_pct + short_pnl_pct) * st["notional_usd"] - fee_usd

        mid = (self._mid(book_a) + self._mid(book_b)) / 2
        current_mid_spread_pct = (self._mid(book_a) - self._mid(book_b)) / mid * 100 if mid > 0 else 0
        stop_loss_pct, max_hold_h = config.get_risk_params(coin, a, b)
        hold_hours = (time.time() - st["opened_at"]) / 3600

        reason = None
        if projected_net_pnl >= 0:
            reason = "profit_take"
        elif hold_hours >= max_hold_h:
            reason = "max_hold"
        elif st["direction"] == "long_a" and current_mid_spread_pct <= -stop_loss_pct:
            reason = "stop_loss"
        elif st["direction"] == "long_b" and current_mid_spread_pct >= stop_loss_pct:
            reason = "stop_loss"

        if reason is None:
            return

        net_pnl = db.close_position(st["pos_id"], exit_long_px, exit_short_px, current_mid_spread_pct, fee_usd, exit_reason=reason)
        del self.state[key]
        log.info(f"CLOSE {coin:10} {a.upper()}-{b.upper():10} reason={reason:12} "
                 f"hold={hold_hours:.2f}h net_pnl=${net_pnl:+.2f}")

    def snapshot_equity(self, note=""):
        eq = self.current_equity()
        open_n = len(self.state)
        db.record_equity_snapshot(eq, open_n, note)
        return eq, open_n
