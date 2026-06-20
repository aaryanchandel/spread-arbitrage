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

    by_symbol = {}
    for t in trades:
        s = by_symbol.setdefault(t["symbol"], {"n_trades": 0, "net_pnl_usd": 0.0})
        s["n_trades"] += 1
        s["net_pnl_usd"] += t["net_pnl_usd"]
    for s in by_symbol.values():
        s["net_pnl_usd"] = round(s["net_pnl_usd"], 2)

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
        "by_exit_reason": by_exit_reason,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
    }
