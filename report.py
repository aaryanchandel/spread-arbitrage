"""Performance reporting - daily/weekly/monthly P&L and drawdown from real closed trades."""
from datetime import datetime, timezone, timedelta

import config
import db


def _bucket_pnl(trades: list[dict], seconds_per_bucket: float, n_buckets: int) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    buckets = [{"start": now - (i + 1) * seconds_per_bucket, "end": now - i * seconds_per_bucket, "pnl_usd": 0.0, "n_trades": 0}
               for i in range(n_buckets)][::-1]
    for t in trades:
        et = t["exit_time"]
        for b in buckets:
            if b["start"] <= et < b["end"]:
                b["pnl_usd"] += t["net_pnl_usd"]
                b["n_trades"] += 1
                break
    for b in buckets:
        b["start_iso"] = datetime.fromtimestamp(b["start"], tz=timezone.utc).isoformat()
        b["end_iso"] = datetime.fromtimestamp(b["end"], tz=timezone.utc).isoformat()
        b["pnl_usd"] = round(b["pnl_usd"], 2)
    return buckets


def _group_pnl(trades: list[dict], key_fn) -> dict:
    """Groups trades by one or more keys per trade (key_fn returns a list of keys a
    trade counts toward - e.g. a pair's two exchange legs). Includes risk-reward
    stats: win_rate, avg_win/avg_loss, and profit_factor (gross win $ / gross loss $,
    >1 means winners outweigh losers in dollar terms, not just count)."""
    groups = {}
    for t in trades:
        for key in key_fn(t):
            groups.setdefault(key, []).append(t["net_pnl_usd"])
    out = {}
    for key, pnls in groups.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        out[key] = {
            "n_trades": len(pnls),
            "net_pnl_usd": round(sum(pnls), 2),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
            "avg_win_usd": round(gross_win / len(wins), 3) if wins else 0.0,
            "avg_loss_usd": round(-gross_loss / len(losses), 3) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        }
    return out


def build_report() -> dict:
    trades = db.get_all_trades()
    equity_curve = db.get_equity_curve()
    capital = config.PAPER_CAPITAL_USD
    realized = db.get_realized_pnl_total()
    equity = capital + realized

    daily = _bucket_pnl(trades, 86400, 30)
    weekly = _bucket_pnl(trades, 86400 * 7, 12)
    monthly = _bucket_pnl(trades, 86400 * 30, 6)

    # drawdown from equity snapshot history
    max_dd_pct = 0.0
    peak = capital
    for snap in equity_curve:
        peak = max(peak, snap["equity_usd"])
        dd = (snap["equity_usd"] - peak) / peak * 100 if peak > 0 else 0
        max_dd_pct = min(max_dd_pct, dd)

    worst_day = min(daily, key=lambda b: b["pnl_usd"]) if daily else None
    worst_week = min(weekly, key=lambda b: b["pnl_usd"]) if weekly else None
    worst_month = min(monthly, key=lambda b: b["pnl_usd"]) if monthly else None

    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else None

    by_exit_reason = {}
    for t in trades:
        r = t.get("exit_reason") or "unknown"
        s = by_exit_reason.setdefault(r, {"n_trades": 0, "net_pnl_usd": 0.0})
        s["n_trades"] += 1
        s["net_pnl_usd"] += t["net_pnl_usd"]
    for s in by_exit_reason.values():
        s["net_pnl_usd"] = round(s["net_pnl_usd"], 2)

    by_symbol = _group_pnl(trades, lambda t: [t["symbol"]])
    by_pair = _group_pnl(trades, lambda t: [t["pair"]])
    # credits both legs of the pair on every trade (e.g. "HL-BYBIT" -> "HL" and "BYBIT") -
    # answers "which exchanges tend to show up in the most/least profitable arbs",
    # not a true per-leg PnL split (the net PnL is for the spread trade as a whole).
    by_exchange = _group_pnl(trades, lambda t: t["pair"].split("-"))

    first_trade_ts = min((t["entry_time"] for t in trades), default=None)
    days_running = (datetime.now(timezone.utc).timestamp() - first_trade_ts) / 86400 if first_trade_ts else 0

    return {
        "capital_usd": capital,
        "equity_usd": round(equity, 2),
        "total_return_pct": round((equity / capital - 1) * 100, 3),
        "realized_pnl_usd": round(realized, 2),
        "max_drawdown_pct": round(max_dd_pct, 3),
        "days_running": round(days_running, 1),
        "total_trades": len(trades),
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "wins": len(wins),
        "losses": len(losses),
        "worst_day": worst_day,
        "worst_week": worst_week,
        "worst_month": worst_month,
        "by_symbol": by_symbol,
        "by_pair": by_pair,
        "by_exchange": by_exchange,
        "by_exit_reason": by_exit_reason,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }
