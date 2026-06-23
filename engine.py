"""
Paper-trading spread-arb engine - v3.

ENTRY: requires BOTH of these, not just one:
  1. An actually-crossed order book - one exchange's BID genuinely above the
     other's ASK by more than the round-trip taker fee cost. The books
     themselves prove the arb exists, not an inferred mid-price gap.
  2. A statistically unusual dislocation: the current mid-spread's z-score
     against its own recent distribution must exceed Z_ENTRY_THRESHOLD. This
     filters out crossings that are real but unremarkable noise (just barely
     clearing fees) from ones that represent a genuine, larger-than-normal
     dislocation - fewer trades, each with a real statistical edge behind it,
     not just a margin-of-fees edge. The rolling baseline is live once enough
     observations accumulate (Z_MIN_LIVE_OBS), falling back to the 90-day
     historical baseline (risk_params.json) until then - no cold-start gap
     after every redeploy.
Entry always pays taker fees - a crossed book may vanish in seconds, so
there's no time to rest a maker order on the way in.

EXIT (primary): the instant unwinding the position right now - at a
guaranteed taker fill - would be break-even or better, the position enters
a maker-exit attempt: it rests a limit order at the best achievable maker
price on each leg (the current ask for the long leg, the current bid for
the short leg) for up to MAKER_EXIT_TIMEOUT_SECS, hoping the market comes
to it and fills at the lower maker fee. If it doesn't fill in time AND the
position is still profitable at current taker prices, it falls back to a
guaranteed taker close. If it doesn't fill AND the opportunity has decayed
back to unprofitable while waiting, the attempt is abandoned and the
position keeps holding rather than being force-closed at a loss - waiting
for a maker fill is a real bet on the spread, not a free fee-timing option;
both legs staying hedged against the underlying does NOT mean the spread
itself can't move against you while you wait.

EXIT (safety nets, wide by design): a 99.99th-percentile stop-loss and a
99.99th-percentile max-hold, both sized from the 90-day backtest's own
historical distributions (risk_params.json) - these are tail-event bounds,
not a normal exit path. They always force an immediate taker close (abort
any pending maker attempt) - certainty matters more than the fee saving
once risk controls are triggered.
"""
import asyncio
import itertools
import logging
import statistics
import time
from collections import deque

import brokers
import config
import db

log = logging.getLogger("engine")

PAIRS_PER_COIN = {
    coin: list(itertools.combinations(exchs, 2))
    for coin, exchs in config.EXCHANGES_PER_COIN.items()
}


def _broker_symbol(exch: str, coin: str) -> str:
    if exch == "aster":
        return config.ASTER_SYMBOL[coin]
    raise NotImplementedError(f"No live symbol mapping implemented for exchange '{exch}'")


def entry_fee_pct(a: str, b: str) -> float:
    """Taker fee, both legs, paid at open. Always taker - crossed-book entries can't wait."""
    return (config.TAKER_FEE.get(a, 0.0005) + config.TAKER_FEE.get(b, 0.0005)) * 100


def exit_fee_pct(a: str, b: str, maker: bool) -> float:
    fees = config.MAKER_FEE if maker else config.TAKER_FEE
    default = 0.0002 if maker else 0.0005
    return (fees.get(a, default) + fees.get(b, default)) * 100


def round_trip_cost_pct(a: str, b: str, maker_exit: bool = False) -> float:
    """Real fees only, no synthetic slippage padding. Entry leg is always taker."""
    return entry_fee_pct(a, b) + exit_fee_pct(a, b, maker=maker_exit)


def safe_leverage(coin: str, a: str, b: str) -> float:
    levs = config.SYMBOL_LEVERAGE.get(coin, {})
    return min(levs.get(a, 3), levs.get(b, 3))


class PaperEngine:
    def __init__(self):
        self.state = {}   # (coin, a, b) -> dict(...)
        self.books = {}   # exch -> {coin: (bid, ask)}
        self.spread_history = {}  # (coin, a, b) -> deque[float] of recent mid-spread % observations
        self.cooldown_logged = set()  # coins we've already logged entering cooldown (avoid log spam)
        self.margin_per_pair = (config.PAPER_CAPITAL_USD * config.DEPLOY_FRACTION) / config.N_CONCURRENT_PAIRS
        self.active_keys = self._select_active_pairs()
        log.info(f"Tracking {len(self.active_keys)} coin x exchange-pair combinations, "
                 f"z-score entry threshold={config.Z_ENTRY_THRESHOLD}")
        if config.LIVE_TRADING:
            live_ready = config.LIVE_EXCHANGES & set(brokers.BROKERS.keys())
            log.warning(f"LIVE TRADING ENABLED - real orders, real money. "
                        f"Exchanges with a broker AND credentialed: {sorted(live_ready) or 'NONE'}. "
                        f"PER_EXCHANGE_CAPITAL_USD=${config.PER_EXCHANGE_CAPITAL_USD}. "
                        f"KILL_SWITCH={config.KILL_SWITCH}")
        else:
            log.info("LIVE_TRADING is false - paper mode only, no real orders will be placed.")

    def _live_eligible(self, a: str, b: str) -> bool:
        """A pair only trades live if BOTH legs have a working, credentialed broker -
        a half-built broker on one leg can't hedge, it's just a directional bet."""
        if not config.LIVE_TRADING or config.KILL_SWITCH:
            return False
        return (a in config.LIVE_EXCHANGES and b in config.LIVE_EXCHANGES
                and a in brokers.BROKERS and b in brokers.BROKERS)

    def _in_cooldown(self, coin: str) -> bool:
        """True if this coin just lost LOSS_STREAK_THRESHOLD+ in a row and is still
        within its cooldown window. Adaptive: resets on any win, expires on its own."""
        streak, last_exit_time = db.get_loss_streak(coin)
        if streak < config.LOSS_STREAK_THRESHOLD or last_exit_time is None:
            self.cooldown_logged.discard(coin)
            return False
        elapsed_hours = (time.time() - last_exit_time) / 3600
        if elapsed_hours >= config.LOSS_STREAK_COOLDOWN_HOURS:
            self.cooldown_logged.discard(coin)
            return False
        if coin not in self.cooldown_logged:
            log.info(f"COOLDOWN {coin:10} {streak} losses in a row - pausing new entries for "
                     f"{config.LOSS_STREAK_COOLDOWN_HOURS - elapsed_hours:.1f} more hours")
            self.cooldown_logged.add(coin)
        return True

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

    async def tick(self, session=None):
        for coin, a, b in self.active_keys:
            book_a = self.books.get(a, {}).get(coin)
            book_b = self.books.get(b, {}).get(coin)
            if not book_a or not book_b:
                continue
            await self._evaluate(session, coin, a, b, book_a, book_b)

    def _mid(self, book):
        return (book[0] + book[1]) / 2

    async def _evaluate(self, session, coin, a, b, book_a, book_b):
        key = (coin, a, b)

        mid_a, mid_b = self._mid(book_a), self._mid(book_b)
        mid = (mid_a + mid_b) / 2
        if mid > 0:
            spread_pct = (mid_a - mid_b) / mid * 100
            self.spread_history.setdefault(key, deque(maxlen=config.Z_ROLLING_WINDOW)).append(spread_pct)

        st = self.state.get(key)
        if st is None:
            await self._maybe_open(session, key, coin, a, b, book_a, book_b)
        else:
            await self._maybe_close(session, key, coin, a, b, book_a, book_b)

    def _zscore(self, key, coin, a, b, current_spread_pct):
        """Live rolling z-score once enough observations exist, else the 90-day historical baseline."""
        hist = self.spread_history.get(key)
        if hist is not None and len(hist) >= config.Z_MIN_LIVE_OBS:
            mean = statistics.mean(hist)
            std = statistics.pstdev(hist)
            source = "live"
        else:
            mean, std = config.get_baseline_spread_stats(coin, a, b)
            source = "hist"
        if std <= 0:
            return 0.0, source
        return (current_spread_pct - mean) / std, source

    # ── ENTRY: crossed-book arbitrage AND a statistically unusual dislocation ──
    async def _maybe_open(self, session, key, coin, a, b, book_a, book_b):
        bid_a, ask_a = book_a
        bid_b, ask_b = book_b
        mid_a, mid_b = self._mid(book_a), self._mid(book_b)
        mid = (mid_a + mid_b) / 2
        if mid <= 0:
            return

        current_spread_pct = (mid_a - mid_b) / mid * 100
        z, z_source = self._zscore(key, coin, a, b, current_spread_pct)
        if abs(z) < config.Z_ENTRY_THRESHOLD:
            return  # crossing might exist, but it's not statistically unusual - skip for accuracy

        if self._in_cooldown(coin):
            return  # this coin just lost repeatedly - sit out until it earns back eligibility

        rt_cost = round_trip_cost_pct(a, b, maker_exit=False)

        # buy A at its ask, sell B at its bid - profitable only if B's bid clears A's ask + costs
        edge_long_a_short_b = (bid_b - ask_a) / mid * 100
        # buy B at its ask, sell A at its bid
        edge_long_b_short_a = (bid_a - ask_b) / mid * 100

        if edge_long_a_short_b > rt_cost and edge_long_a_short_b >= edge_long_b_short_a:
            await self._open(session, coin, a, b, "long_a", long_exch=a, short_exch=b,
                       entry_long_px=ask_a, entry_short_px=bid_b, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_a_short_b, z=z, z_source=z_source)
        elif edge_long_b_short_a > rt_cost:
            await self._open(session, coin, a, b, "long_b", long_exch=b, short_exch=a,
                       entry_long_px=ask_b, entry_short_px=bid_a, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_b_short_a, z=z, z_source=z_source)

    async def _live_execute_open(self, session, coin, long_exch, short_exch, ref_long_px, ref_short_px):
        """LIVE - places real orders on both legs. Long leg first; if the short leg
        can't be filled within LEG_FILL_RETRY_SECS, flattens the long leg and aborts
        rather than leaving a real, unhedged directional position open."""
        notional = config.PER_EXCHANGE_CAPITAL_USD
        long_broker, short_broker = brokers.BROKERS[long_exch], brokers.BROKERS[short_exch]
        long_symbol = _broker_symbol(long_exch, coin)
        short_symbol = _broker_symbol(short_exch, coin)

        try:
            long_fill = await long_broker.place_market_order(session, long_symbol, "BUY", notional, ref_long_px)
        except Exception as e:
            log.error(f"LIVE-OPEN-ABORT {coin} long leg on {long_exch} failed before any fill: {e}")
            return None

        deadline = time.time() + config.LEG_FILL_RETRY_SECS
        short_fill, last_err = None, None
        while time.time() < deadline:
            try:
                short_fill = await short_broker.place_market_order(session, short_symbol, "SELL", notional, ref_short_px)
                break
            except Exception as e:
                last_err = e
                await asyncio.sleep(1)

        if short_fill is None:
            log.error(f"LIVE-OPEN-ABORT {coin} short leg on {short_exch} never filled ({last_err}) - "
                      f"flattening long leg on {long_exch} to avoid running unhedged")
            try:
                await long_broker.close_position(session, long_symbol)
            except Exception as e:
                log.error(f"LIVE-FLATTEN-FAILED {coin} on {long_exch} - MANUAL INTERVENTION NEEDED: {e}")
            return None

        return long_fill, short_fill

    async def _open(self, session, coin, a, b, direction, long_exch, short_exch, entry_long_px, entry_short_px, rt_cost, crossed_edge_pct, z, z_source):
        key = (coin, a, b)
        mid_a = self._mid(self.books[a][coin])
        mid_b = self._mid(self.books[b][coin])
        mid = (mid_a + mid_b) / 2
        entry_mid_spread_pct = (mid_a - mid_b) / mid * 100

        lev = safe_leverage(coin, a, b)
        notional = (self.margin_per_pair / 2) * lev
        pair_label = f"{a.upper()}-{b.upper()}"
        is_live = False

        if self._live_eligible(a, b):
            live_result = await self._live_execute_open(session, coin, long_exch, short_exch, entry_long_px, entry_short_px)
            if live_result is None:
                return  # aborted (leg mismatch or broker error) - nothing recorded, no half-open position
            long_fill, short_fill = live_result
            entry_long_px, entry_short_px = long_fill["avg_price"], short_fill["avg_price"]
            notional = config.PER_EXCHANGE_CAPITAL_USD
            is_live = True

        pos_id = db.open_position(
            symbol=coin, pair=pair_label, direction=f"long_{long_exch}_short_{short_exch}",
            entry_long_px=entry_long_px, entry_short_px=entry_short_px,
            entry_mid_spread_pct=entry_mid_spread_pct, notional_usd=notional, leverage=lev, kind="arb",
            is_live=is_live,
        )
        self.state[key] = {
            "direction": direction, "pos_id": pos_id,
            "long_exch": long_exch, "short_exch": short_exch,
            "entry_long_px": entry_long_px, "entry_short_px": entry_short_px,
            "notional_usd": notional, "entry_mid_spread_pct": entry_mid_spread_pct,
            "opened_at": time.time(), "exiting": False, "is_live": is_live,
        }
        stop_loss_pct, max_hold_h = config.get_risk_params(coin, a, b)
        net_edge_after_fees = crossed_edge_pct - rt_cost
        tag = "LIVE" if is_live else "PAPER"
        log.info(f"OPEN[{tag}] {coin:10} {pair_label:10} long_{long_exch}/short_{short_exch} "
                  f"crossed_edge={crossed_edge_pct:+.4f}% net_after_fees={net_edge_after_fees:+.4f}% "
                  f"z={z:+.2f}({z_source}) notional=${notional:.0f} lev={lev}x "
                  f"stop@{stop_loss_pct:.3f}% max_hold={max_hold_h:.1f}h")

    def _mark_to_market(self, key, coin, a, b, book_a, book_b):
        """Taker-guaranteed mark-to-market - the conservative baseline used to decide whether
        it's worth starting an exit attempt at all, and as the fallback if maker doesn't fill."""
        st = self.state[key]
        long_exch, short_exch = st["long_exch"], st["short_exch"]
        long_book = book_a if long_exch == a else book_b
        short_book = book_a if short_exch == a else book_b

        exit_long_px = long_book[0]    # sell long position at bid
        exit_short_px = short_book[1]  # buy back short at ask

        rt_cost = round_trip_cost_pct(a, b, maker_exit=False)
        fee_usd = st["notional_usd"] * rt_cost / 100

        long_pnl_pct = (exit_long_px - st["entry_long_px"]) / st["entry_long_px"]
        short_pnl_pct = (st["entry_short_px"] - exit_short_px) / st["entry_short_px"]
        projected_net_pnl = (long_pnl_pct + short_pnl_pct) * st["notional_usd"] - fee_usd

        mid = (self._mid(book_a) + self._mid(book_b)) / 2
        current_mid_spread_pct = (self._mid(book_a) - self._mid(book_b)) / mid * 100 if mid > 0 else 0
        hold_hours = (time.time() - st["opened_at"]) / 3600

        return {
            "projected_net_pnl": projected_net_pnl, "exit_long_px": exit_long_px,
            "exit_short_px": exit_short_px, "fee_usd": fee_usd,
            "current_mid_spread_pct": current_mid_spread_pct, "hold_hours": hold_hours,
        }

    # ── EXIT ─────────────────────────────────────────────────────────────────
    async def _maybe_close(self, session, key, coin, a, b, book_a, book_b):
        st = self.state[key]
        m = self._mark_to_market(key, coin, a, b, book_a, book_b)
        stop_loss_pct, max_hold_h = config.get_risk_params(coin, a, b)

        # risk controls always win, always taker, abort any pending maker attempt
        if m["hold_hours"] >= max_hold_h:
            await self._force_close(session, key, coin, a, b, m, reason="max_hold")
            return
        if (st["direction"] == "long_a" and m["current_mid_spread_pct"] <= -stop_loss_pct) or \
           (st["direction"] == "long_b" and m["current_mid_spread_pct"] >= stop_loss_pct):
            await self._force_close(session, key, coin, a, b, m, reason="stop_loss")
            return

        if st.get("is_live"):
            # Live positions skip the maker-exit fee optimization for now - it's an
            # unverified extra layer of real-order risk on top of a brand-new live
            # path. Always taker-close the instant it's profitable; maker-exit can
            # be added once live taker closes are proven out.
            if m["projected_net_pnl"] >= 0:
                await self._force_close(session, key, coin, a, b, m, reason="profit_take_live_taker")
            return

        if st["exiting"]:
            await self._progress_maker_exit(session, key, coin, a, b, book_a, book_b, m)
            return

        if m["projected_net_pnl"] >= 0:
            self._start_maker_exit(key, coin, a, b, book_a, book_b)

    async def _force_close(self, session, key, coin, a, b, m, reason):
        st = self.state[key]
        exit_long_px, exit_short_px = m["exit_long_px"], m["exit_short_px"]

        if st.get("is_live"):
            long_symbol = _broker_symbol(st["long_exch"], coin)
            short_symbol = _broker_symbol(st["short_exch"], coin)
            try:
                long_close = await brokers.BROKERS[st["long_exch"]].close_position(session, long_symbol)
                short_close = await brokers.BROKERS[st["short_exch"]].close_position(session, short_symbol)
            except Exception as e:
                log.error(f"LIVE-CLOSE-FAILED {coin} {a.upper()}-{b.upper()} - MANUAL INTERVENTION NEEDED, "
                          f"position may still be open on the exchange: {e}")
                return  # don't mark closed in DB unless we're sure it's actually closed live
            if long_close:
                exit_long_px = long_close["avg_price"]
            if short_close:
                exit_short_px = short_close["avg_price"]

        net_pnl = db.close_position(st["pos_id"], exit_long_px, exit_short_px,
                                     m["current_mid_spread_pct"], m["fee_usd"], exit_reason=reason)
        del self.state[key]
        tag = "LIVE" if st.get("is_live") else "PAPER"
        log.info(f"CLOSE[{tag}] {coin:10} {a.upper()}-{b.upper():10} reason={reason:24} "
                 f"hold={m['hold_hours']:.2f}h net_pnl=${net_pnl:+.2f}")

    def _start_maker_exit(self, key, coin, a, b, book_a, book_b):
        st = self.state[key]
        long_exch, short_exch = st["long_exch"], st["short_exch"]
        long_book = book_a if long_exch == a else book_b
        short_book = book_a if short_exch == a else book_b

        st["exiting"] = True
        st["maker_long_px"] = long_book[1]    # rest sell-to-close at current ask (doesn't cross the bid - maker)
        st["maker_short_px"] = short_book[0]  # rest buy-to-close at current bid (doesn't cross the ask - maker)
        st["maker_started_at"] = time.time()
        log.info(f"EXIT~ {coin:10} {a.upper()}-{b.upper():10} attempting maker @ "
                 f"long={st['maker_long_px']} short={st['maker_short_px']} "
                 f"(timeout {config.MAKER_EXIT_TIMEOUT_SECS}s, falls back to taker)")

    async def _progress_maker_exit(self, session, key, coin, a, b, book_a, book_b, m):
        st = self.state[key]
        long_exch, short_exch = st["long_exch"], st["short_exch"]
        long_book = book_a if long_exch == a else book_b
        short_book = book_a if short_exch == a else book_b

        # filled if the market has moved through our resting price
        long_filled = long_book[0] >= st["maker_long_px"]
        short_filled = short_book[1] <= st["maker_short_px"]
        elapsed = time.time() - st["maker_started_at"]

        if long_filled and short_filled:
            rt_cost = round_trip_cost_pct(a, b, maker_exit=True)
            fee_usd = st["notional_usd"] * rt_cost / 100
            net_pnl = db.close_position(st["pos_id"], st["maker_long_px"], st["maker_short_px"],
                                         m["current_mid_spread_pct"], fee_usd, exit_reason="profit_take_maker")
            del self.state[key]
            saved = m["fee_usd"] - fee_usd
            log.info(f"CLOSE {coin:10} {a.upper()}-{b.upper():10} reason=profit_take_maker     "
                     f"hold={m['hold_hours']:.2f}h net_pnl=${net_pnl:+.2f} (fee saved=${saved:.3f} vs taker)")
        elif elapsed >= config.MAKER_EXIT_TIMEOUT_SECS:
            if m["projected_net_pnl"] >= 0:
                # still profitable at current taker prices - lock it in even without the maker fill
                await self._force_close(session, key, coin, a, b, m, reason="profit_take_taker_fallback")
            else:
                # maker didn't fill AND the opportunity decayed while we waited - don't force a
                # loss just because a timer expired. Abandon the attempt and keep holding; risk
                # controls (stop-loss/max-hold) are still active and will catch a true reversal.
                st["exiting"] = False
                log.info(f"EXIT-ABORT {coin:10} {a.upper()}-{b.upper():10} maker never filled and "
                         f"spread decayed (would now be ${m['projected_net_pnl']:+.2f}) - resuming hold, not forcing a loss")
        # else: keep resting and waiting for the market to come to us

    def get_cooldown_status(self):
        """Which coins are currently excluded for repeated losses, and for how much longer."""
        out = []
        for coin in config.ALL_COINS:
            streak, last_exit_time = db.get_loss_streak(coin)
            if streak < config.LOSS_STREAK_THRESHOLD or last_exit_time is None:
                continue
            elapsed_hours = (time.time() - last_exit_time) / 3600
            remaining = config.LOSS_STREAK_COOLDOWN_HOURS - elapsed_hours
            if remaining > 0:
                out.append({"symbol": coin, "loss_streak": streak, "hours_remaining": round(remaining, 1)})
        return out

    def get_unrealized_pnl(self):
        """Mark every open position to market right now (taker-guaranteed baseline). Returns (total_usd, per_position list)."""
        total = 0.0
        per_position = []
        for key, st in list(self.state.items()):
            coin, a, b = key
            book_a = self.books.get(a, {}).get(coin)
            book_b = self.books.get(b, {}).get(coin)
            if not book_a or not book_b:
                continue
            m = self._mark_to_market(key, coin, a, b, book_a, book_b)
            total += m["projected_net_pnl"]
            per_position.append({
                "pos_id": st["pos_id"], "symbol": coin, "pair": f"{a.upper()}-{b.upper()}",
                "unrealized_pnl_usd": round(m["projected_net_pnl"], 2),
                "hold_hours": round(m["hold_hours"], 2),
                "exiting": st["exiting"],
            })
        return round(total, 2), per_position

    def snapshot_equity(self, note=""):
        eq = self.current_equity()
        open_n = len(self.state)
        db.record_equity_snapshot(eq, open_n, note)
        return eq, open_n
