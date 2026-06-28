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
import math
import statistics
import time
from collections import deque

import brokers
import config
import db
from exchanges import aster as aster_market
from exchanges import hyperliquid as hl_market
from exchanges import pacifica as pac_market

log = logging.getLogger("engine")

PAIRS_PER_COIN = {
    coin: list(itertools.combinations(exchs, 2))
    for coin, exchs in config.EXCHANGES_PER_COIN.items()
}

# Exchanges with a real walkable order book (depth + quantity per level) - used
# to estimate a realistic average fill price for an actual notional size
# instead of assuming the whole order fills at the single best price. Ostium
# has no entry here deliberately: it's an oracle-priced on-chain venue with no
# order-book concept, so its quoted bid/ask already IS the executable price.
DEPTH_MODULES = {"hl": hl_market, "pac": pac_market, "aster": aster_market}


def _broker_symbol(exch: str, coin: str) -> str:
    if exch == "aster":
        return config.ASTER_SYMBOL[coin]
    if exch in ("hl", "pac", "ost"):
        return coin  # HL/Pacifica/Ostium address coins directly, no exchange-specific suffix
    raise NotImplementedError(f"No live symbol mapping implemented for exchange '{exch}'")


def vwap_for_notional(levels: list[tuple[float, float]], notional_usd: float) -> float | None:
    """Walks order-book levels (best price first, each (price, base_qty)),
    accumulating until notional_usd worth has been filled, and returns the
    volume-weighted average price for that fill. Returns None if the provided
    levels don't have enough cumulative depth to fill the target size - a
    real signal that this size is too big for the currently visible book, not
    just a rounding artifact."""
    remaining_quote = notional_usd
    quote_spent = 0.0
    base_filled = 0.0
    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue
        level_quote = price * qty
        if level_quote <= remaining_quote:
            quote_spent += level_quote
            base_filled += qty
            remaining_quote -= level_quote
        else:
            base_take = remaining_quote / price
            quote_spent += remaining_quote
            base_filled += base_take
            remaining_quote = 0.0
            break
    if remaining_quote > 1e-9 or base_filled <= 0:
        return None
    return quote_spent / base_filled


async def _vwap_price(session, exch: str, symbol: str, side: str, notional_usd: float, fallback_px: float) -> float | None:
    """Realistic average fill price for notional_usd on this exchange/symbol,
    from live order-book depth. side: 'buy' walks asks, 'sell' walks bids.
    Returns fallback_px unmodified (trusted as-is) when the exchange has no
    order-book depth concept (Ostium) or the depth fetch itself fails/hiccups
    - never blocks a decision on a transient data issue. Returns None only
    when depth WAS fetched but is genuinely insufficient to fill this size -
    a real "too big for this book right now" signal the caller should act on."""
    module = DEPTH_MODULES.get(exch)
    if module is None:
        return fallback_px
    try:
        depth = await module.fetch_depth(session, symbol)
    except Exception as e:
        log.warning(f"DEPTH-FETCH-FAILED {exch} {symbol}: {e} - using top-of-book, not blocking on a data hiccup")
        return fallback_px
    if depth is None:
        return fallback_px
    levels = depth["asks"] if side == "buy" else depth["bids"]
    return vwap_for_notional(levels, notional_usd)


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
        self._rehydrate_state()
        if config.LIVE_TRADING:
            live_ready = sorted(
                e for e in config.LIVE_EXCHANGES
                if e in brokers.BROKERS and getattr(brokers.BROKERS[e], "is_configured", False)
            )
            log.warning(f"LIVE TRADING ENABLED - real orders, real money. "
                        f"Exchanges with a broker AND valid credentials: {live_ready or 'NONE'}. "
                        f"PER_EXCHANGE_CAPITAL_USD=${config.PER_EXCHANGE_CAPITAL_USD}. "
                        f"KILL_SWITCH={config.KILL_SWITCH}")
        else:
            log.info("LIVE_TRADING is false - paper mode only, no real orders will be placed.")

    def _live_eligible(self, a: str, b: str) -> bool:
        """A pair only trades live if BOTH legs have a working, CREDENTIALED broker -
        a half-built or uncredentialed broker on one leg can't hedge, it's just a
        directional bet."""
        if not config.LIVE_TRADING or config.KILL_SWITCH:
            return False

        def _ready(exch):
            return (exch in config.LIVE_EXCHANGES and exch in brokers.BROKERS
                    and getattr(brokers.BROKERS[exch], "is_configured", False))

        return _ready(a) and _ready(b)

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

    def _rehydrate_state(self):
        """self.state is in-memory only and starts empty on every process
        restart (every redeploy). Without this, any position still open from
        before a restart - including a real, real-money live one - becomes
        invisible to _evaluate: no more stop-loss/max-hold/profit-take checks
        ever run on it, and the engine could even open a SECOND position on
        the same coin/pair since it no longer knows the slot is occupied.
        Rebuilds self.state from the DB's still-open rows so monitoring
        resumes exactly where it left off."""
        for row in db.get_open_positions():
            pair_parts = row["pair"].split("-")
            if len(pair_parts) != 2:
                log.error(f"REHYDRATE-SKIP pos_id={row['id']} {row['symbol']} - unparseable pair '{row['pair']}'")
                continue
            pair_set = {p.lower() for p in pair_parts}
            key = next((k for k in self.active_keys
                        if k[0] == row["symbol"] and {k[1], k[2]} == pair_set), None)
            if key is None:
                log.error(f"REHYDRATE-SKIP pos_id={row['id']} {row['symbol']} {row['pair']} is_live={bool(row['is_live'])} - "
                          f"no matching active coin/exchange-pair (config changed?) - THIS POSITION IS UNMONITORED, "
                          f"check it manually on-exchange")
                continue
            coin, a, b = key
            direction_raw = row["direction"]  # "long_<exch>_short_<exch>"
            try:
                long_exch = direction_raw.split("long_")[1].split("_short_")[0]
                short_exch = direction_raw.split("_short_")[1]
            except IndexError:
                log.error(f"REHYDRATE-SKIP pos_id={row['id']} {row['symbol']} - unparseable direction '{direction_raw}'")
                continue
            self.state[key] = {
                "direction": "long_a" if long_exch == a else "long_b",
                "pos_id": row["id"], "long_exch": long_exch, "short_exch": short_exch,
                "entry_long_px": row["entry_long_px"], "entry_short_px": row["entry_short_px"],
                "notional_usd": row["notional_usd"], "entry_mid_spread_pct": row["entry_mid_spread_pct"],
                "opened_at": row["entry_time"], "exiting": False, "is_live": bool(row["is_live"]),
            }
            tag = "LIVE" if row["is_live"] else "PAPER"
            log.warning(f"REHYDRATED[{tag}] pos_id={row['id']} {row['symbol']} {row['pair']} "
                        f"long_{long_exch}/short_{short_exch} - resuming monitoring after restart")

    def update_books(self, exch: str, book: dict):
        self.books[exch] = book

    def current_equity(self) -> float:
        realized = db.get_realized_pnl_total()
        return config.PAPER_CAPITAL_USD + realized

    async def reconcile_orphans(self, session):
        """LIVE safety net, run once at startup before the poll loop begins: any
        REAL position on a live-configured exchange with no matching tracked
        hedge (e.g. left over from a crash mid-open, or one leg the health
        check already flattened just before the process restarted) is
        auto-flattened immediately - no residual exposure should ever survive
        a restart, hedged or not."""
        if not config.LIVE_TRADING:
            return
        tracked: dict[str, set[str]] = {}
        for (coin, a, b), st in self.state.items():
            tracked.setdefault(st["long_exch"], set()).add(_broker_symbol(st["long_exch"], coin))
            tracked.setdefault(st["short_exch"], set()).add(_broker_symbol(st["short_exch"], coin))

        for exch_name, broker in brokers.BROKERS.items():
            if exch_name not in config.LIVE_EXCHANGES or not getattr(broker, "is_configured", False):
                continue
            try:
                real_positions = await broker.get_all_positions(session)
            except Exception as e:
                log.error(f"RECONCILE-FAILED {exch_name}: {e}")
                continue
            known = tracked.get(exch_name, set())
            for symbol, pos in real_positions.items():
                if symbol in known:
                    continue
                log.error(f"ORPHAN-POSITION {exch_name} {symbol} {pos} - no tracked hedge found for this "
                          f"position (crash leftover?) - auto-flattening to guarantee zero residual "
                          f"exposure after restart")
                try:
                    await broker.close_position(session, symbol)
                except Exception as e:
                    log.error(f"ORPHAN-FLATTEN-FAILED {exch_name} {symbol} - MANUAL INTERVENTION NEEDED: {e}")

    async def _check_live_health(self, session):
        """LIVE safety net, run every tick: detects when a leg's real position
        vanished outside the bot's own close logic (liquidation, exchange-side
        stop-out, ADL) by polling each broker directly - self.state alone can't
        tell "still open" apart from "got liquidated 30 seconds ago". The
        instant this is caught, flattens the surviving leg immediately,
        accepting whatever PnL exists right now without waiting for a better
        price - running one leg naked is the one risk this strategy can't
        absorb, and every second spent waiting only extends that exposure."""
        for key in list(self.state.keys()):
            st = self.state.get(key)
            if st is None or not st.get("is_live") or st.get("exiting"):
                continue
            coin, a, b = key
            long_exch, short_exch = st["long_exch"], st["short_exch"]
            long_symbol = _broker_symbol(long_exch, coin)
            short_symbol = _broker_symbol(short_exch, coin)
            try:
                long_pos, short_pos = await asyncio.gather(
                    brokers.BROKERS[long_exch].get_position(session, long_symbol),
                    brokers.BROKERS[short_exch].get_position(session, short_symbol),
                )
            except Exception as e:
                log.warning(f"HEALTH-CHECK-FAILED {coin} {long_exch}/{short_exch}: {e}")
                continue

            long_gone, short_gone = long_pos is None, short_pos is None
            if not long_gone and not short_gone:
                continue  # hedge intact, both legs alive

            if long_gone and short_gone:
                log.warning(f"LEGS-ALREADY-FLAT {coin} {long_exch}/{short_exch} - both closed outside the "
                            f"bot's tracking, cleaning up record (pos_id={st['pos_id']})")
                db.close_position(st["pos_id"], st["entry_long_px"], st["entry_short_px"],
                                   st["entry_mid_spread_pct"], 0.0, exit_reason="external_close_both_legs")
                del self.state[key]
                continue

            gone_exch = long_exch if long_gone else short_exch
            survivor_exch = short_exch if long_gone else long_exch
            survivor_symbol = short_symbol if long_gone else long_symbol
            log.error(f"LEG-LIQUIDATED {coin} {gone_exch} leg vanished unexpectedly (liquidation/stop-out) - "
                      f"flattening surviving {survivor_exch} leg NOW, pos_id={st['pos_id']}")
            try:
                survivor_close = await brokers.BROKERS[survivor_exch].close_position(session, survivor_symbol)
            except Exception as e:
                log.error(f"LIVE-FLATTEN-FAILED {coin} {survivor_exch} after leg liquidation - "
                          f"MANUAL INTERVENTION NEEDED, this leg may still be open: {e}")
                continue

            # The liquidated leg's real exit price was never reported to us (it
            # closed outside any order we placed) - approximate with the current
            # book mid rather than entry price, which would wrongly imply 0%
            # PnL on that leg. This is a bookkeeping approximation only; the
            # real-money outcome already happened regardless of how it's recorded.
            gone_book = self.books.get(gone_exch, {}).get(coin)
            gone_exit_px = self._mid(gone_book) if gone_book else (
                st["entry_long_px"] if long_gone else st["entry_short_px"])
            survivor_exit_px = survivor_close["avg_price"] if survivor_close else (
                st["entry_short_px"] if long_gone else st["entry_long_px"])
            exit_long_px, exit_short_px = (gone_exit_px, survivor_exit_px) if long_gone else (survivor_exit_px, gone_exit_px)

            fee_usd = st["notional_usd"] * config.TAKER_FEE.get(survivor_exch, 0.0005)
            mid = (exit_long_px + exit_short_px) / 2
            exit_spread_pct = ((exit_long_px - exit_short_px) / mid * 100) if mid else 0.0
            net_pnl = db.close_position(st["pos_id"], exit_long_px, exit_short_px, exit_spread_pct, fee_usd,
                                         exit_reason="leg_liquidated")
            del self.state[key]
            log.error(f"CLOSE[LIVE-LIQUIDATION] {coin:10} {a.upper()}-{b.upper():10} "
                      f"liquidated_leg={gone_exch} survivor={survivor_exch} net_pnl=${net_pnl:+.2f} "
                      f"(liquidated leg's exit price is approximated from current book, not an exact fill)")

    async def tick(self, session=None):
        if config.LIVE_TRADING:
            await self._check_live_health(session)
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
            ok, real_long_px, real_short_px = await self._depth_check_open(
                session, coin, a, b, long_exch=a, short_exch=b,
                long_fallback_px=ask_a, short_fallback_px=bid_b, rt_cost=rt_cost, mid=mid)
            if not ok:
                return
            await self._open(session, coin, a, b, "long_a", long_exch=a, short_exch=b,
                       entry_long_px=real_long_px, entry_short_px=real_short_px, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_a_short_b, z=z, z_source=z_source)
        elif edge_long_b_short_a > rt_cost:
            ok, real_long_px, real_short_px = await self._depth_check_open(
                session, coin, a, b, long_exch=b, short_exch=a,
                long_fallback_px=ask_b, short_fallback_px=bid_a, rt_cost=rt_cost, mid=mid)
            if not ok:
                return
            await self._open(session, coin, a, b, "long_b", long_exch=b, short_exch=a,
                       entry_long_px=real_long_px, entry_short_px=real_short_px, rt_cost=rt_cost,
                       crossed_edge_pct=edge_long_b_short_a, z=z, z_source=z_source)

    async def _depth_check_open(self, session, coin, a, b, long_exch, short_exch,
                                 long_fallback_px, short_fallback_px, rt_cost, mid):
        """Applies to EVERY pair, live or paper: re-verifies the crossed-book
        edge survives realistic order-book depth for the actual notional about
        to be committed, instead of trusting mid/top-of-book alone - the order
        book is where the order actually gets executed. A thin top-of-book
        price can show a juicy edge that vanishes (or flips to a loss) the
        moment a real market order has to walk through worse levels to fill
        its full size - exactly the "looked profitable, closed at a loss"
        pattern. Sized to whichever notional this pair will actually trade at
        (live capital x leverage, or the paper-equivalent margin x leverage),
        so the depth requirement always matches the real order size. Returns
        (ok, realistic_long_px, realistic_short_px)."""
        lev = max(1, math.floor(safe_leverage(coin, a, b)))
        if self._live_eligible(a, b):
            notional = config.PER_EXCHANGE_CAPITAL_USD * lev
        else:
            notional = (self.margin_per_pair / 2) * lev
        long_symbol = _broker_symbol(long_exch, coin)
        short_symbol = _broker_symbol(short_exch, coin)

        long_px = await _vwap_price(session, long_exch, long_symbol, "buy", notional, long_fallback_px)
        if long_px is None:
            log.info(f"DEPTH-SKIP {coin} {long_exch} insufficient book depth for ${notional:.0f} notional - skipping entry")
            return False, None, None
        short_px = await _vwap_price(session, short_exch, short_symbol, "sell", notional, short_fallback_px)
        if short_px is None:
            log.info(f"DEPTH-SKIP {coin} {short_exch} insufficient book depth for ${notional:.0f} notional - skipping entry")
            return False, None, None

        realistic_edge_pct = (short_px - long_px) / mid * 100
        if realistic_edge_pct <= rt_cost:
            log.info(f"DEPTH-SKIP {coin} {long_exch}/{short_exch} edge vanishes under realistic depth-adjusted "
                      f"fill for ${notional:.0f}: depth-adjusted edge={realistic_edge_pct:+.4f}% <= cost {rt_cost:.4f}%")
            return False, None, None
        return True, long_px, short_px

    async def _live_execute_open(self, session, coin, long_exch, short_exch, ref_long_px, ref_short_px, leverage: int):
        """LIVE - places real orders on both legs. Long leg first; if the short leg
        can't be filled within LEG_FILL_RETRY_SECS, flattens the long leg and aborts
        rather than leaving a real, unhedged directional position open. Sets
        ISOLATED leverage on each leg before opening so PER_EXCHANGE_CAPITAL_USD
        commands leverage x that notional instead of trading 1x flat.

        Both legs are sized from ONE shared target base-asset quantity
        (anchored to the long leg's price), not from independently dividing
        the same notional by each leg's own (different) price - that would
        let the two legs drift to different sizes any time the two
        exchanges' prices differ, which they always do by construction (that
        gap IS the spread being traded). After both legs fill, a hard check
        verifies the actual filled quantities are equal within a tight
        tolerance - delta-neutral by construction AND verified after the
        fact, never just assumed."""
        notional = config.PER_EXCHANGE_CAPITAL_USD * leverage
        target_qty = notional / ref_long_px
        long_broker, short_broker = brokers.BROKERS[long_exch], brokers.BROKERS[short_exch]
        long_symbol = _broker_symbol(long_exch, coin)
        short_symbol = _broker_symbol(short_exch, coin)

        try:
            await long_broker.set_leverage(session, long_symbol, leverage)
            long_fill = await long_broker.place_market_order(session, long_symbol, "BUY", target_qty, ref_long_px)
        except Exception as e:
            log.error(f"LIVE-OPEN-ABORT {coin} long leg on {long_exch} failed before any fill: {e}")
            return None

        deadline = time.time() + config.LEG_FILL_RETRY_SECS
        short_fill, last_err = None, None
        while time.time() < deadline:
            try:
                await short_broker.set_leverage(session, short_symbol, leverage)
                short_fill = await short_broker.place_market_order(session, short_symbol, "SELL", target_qty, ref_short_px)
                break
            except Exception as e:
                last_err = e
                await asyncio.sleep(1)

        if short_fill is None:
            log.error(f"LIVE-OPEN-ABORT {coin} short leg on {short_exch} never filled ({last_err}) - "
                      f"flattening long leg on {long_exch} to avoid running unhedged")
            try:
                await long_broker.close_position(session, long_symbol)
                # Open + close both incurred a real taker fee on long_exch even
                # though no tracked position was ever opened (db.open_position
                # was never called) - record it so this real cost is visible
                # in PnL reporting, not silently absorbed.
                fee_usd = 2 * config.TAKER_FEE.get(long_exch, 0.0005) * notional
                db.record_aborted_attempt(coin, f"{long_exch.upper()}-{short_exch.upper()}",
                                           notional, fee_usd, "aborted_leg_never_filled")
            except Exception as e:
                log.error(f"LIVE-FLATTEN-FAILED {coin} on {long_exch} - MANUAL INTERVENTION NEEDED: {e}")
            return None

        # HARD CHECK: both legs reported a fill, but verify they're ACTUALLY
        # delta-neutral - independent per-exchange lot-size rounding or a
        # partial fill can still leave a residual mismatch even with a shared
        # target qty. Any residual beyond tolerance is treated as a hedge
        # failure: flatten BOTH legs immediately rather than carry any
        # directional exposure, however small. The tolerance is NOT a guessed
        # flat number - it's calibrated to each leg's REAL lot-size
        # granularity (e.g. Aster rounds BTC to 0.001 = ~$60 at recent prices;
        # treating that normal quantization as a "hedge failure" would abort
        # nearly every BTC trade through Aster even when working perfectly).
        # Anything beyond ~1.5 lots of slack is a genuine mismatch, not rounding.
        long_lot = getattr(long_broker, "get_lot_size", lambda s: 0.0)(long_symbol)
        short_lot = getattr(short_broker, "get_lot_size", lambda s: 0.0)(short_symbol)
        coarsest_lot = max(long_lot, short_lot)
        qty_tolerance = coarsest_lot * 1.5 if coarsest_lot > 0 else target_qty * 0.02
        qty_mismatch = abs(long_fill["filled_qty"] - short_fill["filled_qty"])
        avg_px = (ref_long_px + ref_short_px) / 2
        residual_usd = qty_mismatch * avg_px
        tolerance_usd = max(0.50, qty_tolerance * avg_px)
        if qty_mismatch > qty_tolerance:
            log.error(f"DELTA-MISMATCH {coin} {long_exch}/{short_exch} long_qty={long_fill['filled_qty']} "
                      f"short_qty={short_fill['filled_qty']} residual=${residual_usd:.2f} > "
                      f"tolerance=${tolerance_usd:.2f} - NOT delta-neutral, flattening BOTH legs")
            results = await asyncio.gather(
                long_broker.close_position(session, long_symbol),
                short_broker.close_position(session, short_symbol),
                return_exceptions=True,
            )
            for exch, res in zip((long_exch, short_exch), results):
                if isinstance(res, Exception):
                    log.error(f"LIVE-FLATTEN-FAILED {coin} on {exch} after delta mismatch - "
                              f"MANUAL INTERVENTION NEEDED: {res}")
            # Both legs opened AND closed - four real taker fees even though no
            # tracked position was ever opened. Record it so this real cost is
            # visible in PnL reporting, not silently absorbed.
            fee_usd = 2 * (config.TAKER_FEE.get(long_exch, 0.0005) + config.TAKER_FEE.get(short_exch, 0.0005)) * notional
            db.record_aborted_attempt(coin, f"{long_exch.upper()}-{short_exch.upper()}",
                                       notional, fee_usd, "aborted_delta_mismatch")
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
            # safe_leverage's per-coin caps (from the 90-day liquidation/safety
            # backtest) can be fractional (e.g. 2.8x) - exchanges require an
            # integer, and flooring (never rounding up) keeps it within the cap.
            lev_int = max(1, math.floor(lev))
            live_result = await self._live_execute_open(session, coin, long_exch, short_exch,
                                                          entry_long_px, entry_short_px, lev_int)
            if live_result is None:
                return  # aborted (leg mismatch or broker error) - nothing recorded, no half-open position
            long_fill, short_fill = live_result
            entry_long_px, entry_short_px = long_fill["avg_price"], short_fill["avg_price"]
            notional = config.PER_EXCHANGE_CAPITAL_USD * lev_int
            lev = lev_int
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

    async def _depth_check_close(self, session, coin, long_exch, short_exch, notional,
                                  entry_long_px, entry_short_px, fallback_long_px, fallback_short_px, a, b):
        """LIVE exits only: re-verifies the projected profit survives realistic
        depth-adjusted fill prices for the position's actual notional before
        force-closing - top-of-book alone can show a profitable exit that
        turns into a loss once a real market order walks through thinner
        levels. Only gates the discretionary profit-take close; stop_loss/
        max_hold always fire unconditionally regardless of this check."""
        long_symbol = _broker_symbol(long_exch, coin)
        short_symbol = _broker_symbol(short_exch, coin)
        # closing long = selling (walk bids); closing short = buying back (walk asks)
        exit_long_px = await _vwap_price(session, long_exch, long_symbol, "sell", notional, fallback_long_px)
        if exit_long_px is None:
            log.info(f"DEPTH-EXIT-SKIP {coin} {long_exch} insufficient depth to safely close ${notional:.0f} now")
            return False
        exit_short_px = await _vwap_price(session, short_exch, short_symbol, "buy", notional, fallback_short_px)
        if exit_short_px is None:
            log.info(f"DEPTH-EXIT-SKIP {coin} {short_exch} insufficient depth to safely close ${notional:.0f} now")
            return False

        rt_cost = round_trip_cost_pct(a, b, maker_exit=False)
        fee_usd = notional * rt_cost / 100
        long_pnl_pct = (exit_long_px - entry_long_px) / entry_long_px
        short_pnl_pct = (entry_short_px - exit_short_px) / entry_short_px
        realistic_net_pnl = (long_pnl_pct + short_pnl_pct) * notional - fee_usd
        if realistic_net_pnl < 0:
            log.info(f"DEPTH-EXIT-SKIP {coin} {long_exch}/{short_exch} top-of-book showed profit but "
                      f"depth-adjusted exit is projected net=${realistic_net_pnl:+.2f} - waiting for a better moment")
            return False
        return True

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
                if await self._depth_check_close(session, coin, long_exch=st["long_exch"], short_exch=st["short_exch"],
                                                  notional=st["notional_usd"], entry_long_px=st["entry_long_px"],
                                                  entry_short_px=st["entry_short_px"],
                                                  fallback_long_px=m["exit_long_px"], fallback_short_px=m["exit_short_px"],
                                                  a=a, b=b):
                    await self._force_close(session, key, coin, a, b, m, reason="profit_take_live_taker")
                # else: top-of-book showed profit but realistic depth-adjusted fill
                # doesn't - skip this tick and re-check next tick rather than forcing
                # into a loss. Risk controls above (stop_loss/max_hold) are NOT gated
                # this way - those must always fire regardless of liquidity.
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
            # Concurrent, not sequential - minimizes the window where one leg
            # is already flat and the other isn't, and means a slow/failed
            # close on one leg never delays the other from even starting.
            long_close, short_close = await asyncio.gather(
                brokers.BROKERS[st["long_exch"]].close_position(session, long_symbol),
                brokers.BROKERS[st["short_exch"]].close_position(session, short_symbol),
                return_exceptions=True,
            )
            long_failed = isinstance(long_close, Exception)
            short_failed = isinstance(short_close, Exception)
            if long_failed or short_failed:
                if long_failed != short_failed:
                    # one leg closed, the other didn't - now genuinely unhedged.
                    # _check_live_health() will catch this on the very next
                    # tick (it polls each leg's real position independently)
                    # and flatten the survivor, but flag it loudly now too.
                    ok_exch = st["short_exch"] if long_failed else st["long_exch"]
                    failed_exch = st["long_exch"] if long_failed else st["short_exch"]
                    log.error(f"PARTIAL-CLOSE {coin} {a.upper()}-{b.upper()} - {ok_exch} closed but "
                              f"{failed_exch} failed: {long_close if long_failed else short_close} - "
                              f"NOW UNHEDGED, relying on next health check to flatten {ok_exch}")
                else:
                    log.error(f"LIVE-CLOSE-FAILED {coin} {a.upper()}-{b.upper()} - MANUAL INTERVENTION NEEDED, "
                              f"position may still be open on the exchange: long={long_close} short={short_close}")
                return  # don't mark closed in DB unless we're sure both legs are actually closed live
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
