"""SQLite persistence for paper positions, closed trades, and equity snapshots."""
import sqlite3
import os
import json
import time

DB_PATH = os.environ.get("DB_PATH", "paper.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, pair TEXT, direction TEXT,
            entry_time REAL, entry_long_px REAL, entry_short_px REAL,
            entry_mid_spread_pct REAL, notional_usd REAL, leverage REAL,
            kind TEXT, status TEXT DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, pair TEXT, direction TEXT, kind TEXT,
            entry_time REAL, exit_time REAL,
            entry_long_px REAL, entry_short_px REAL,
            exit_long_px REAL, exit_short_px REAL,
            entry_mid_spread_pct REAL, exit_mid_spread_pct REAL,
            notional_usd REAL, leverage REAL,
            gross_pnl_usd REAL, fee_usd REAL, net_pnl_usd REAL,
            hold_hours REAL
        );
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, equity_usd REAL, open_positions INTEGER, note TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def open_position(symbol, pair, direction, entry_long_px, entry_short_px,
                   entry_mid_spread_pct, notional_usd, leverage, kind):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO positions
           (symbol, pair, direction, entry_time, entry_long_px, entry_short_px,
            entry_mid_spread_pct, notional_usd, leverage, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, pair, direction, time.time(), entry_long_px, entry_short_px,
         entry_mid_spread_pct, notional_usd, leverage, kind),
    )
    conn.commit()
    pos_id = cur.lastrowid
    conn.close()
    return pos_id


def get_open_positions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_position(pos_id, exit_long_px, exit_short_px, exit_mid_spread_pct, fee_usd):
    conn = get_conn()
    pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    if pos is None:
        conn.close()
        return None
    pos = dict(pos)

    # long leg pnl% = (exit_long - entry_long)/entry_long ; short leg pnl% = (entry_short - exit_short)/entry_short
    long_pnl_pct = (exit_long_px - pos["entry_long_px"]) / pos["entry_long_px"]
    short_pnl_pct = (pos["entry_short_px"] - exit_short_px) / pos["entry_short_px"]
    gross_pnl_usd = (long_pnl_pct + short_pnl_pct) * pos["notional_usd"]
    net_pnl_usd = gross_pnl_usd - fee_usd
    hold_hours = (time.time() - pos["entry_time"]) / 3600

    conn.execute(
        """INSERT INTO trades
           (symbol, pair, direction, kind, entry_time, exit_time,
            entry_long_px, entry_short_px, exit_long_px, exit_short_px,
            entry_mid_spread_pct, exit_mid_spread_pct, notional_usd, leverage,
            gross_pnl_usd, fee_usd, net_pnl_usd, hold_hours)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pos["symbol"], pos["pair"], pos["direction"], pos["kind"],
         pos["entry_time"], time.time(),
         pos["entry_long_px"], pos["entry_short_px"], exit_long_px, exit_short_px,
         pos["entry_mid_spread_pct"], exit_mid_spread_pct, pos["notional_usd"], pos["leverage"],
         gross_pnl_usd, fee_usd, net_pnl_usd, hold_hours),
    )
    conn.execute("UPDATE positions SET status='closed' WHERE id=?", (pos_id,))
    conn.commit()
    conn.close()
    return net_pnl_usd


def record_equity_snapshot(equity_usd, open_positions, note=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO equity_snapshots (ts, equity_usd, open_positions, note) VALUES (?, ?, ?, ?)",
        (time.time(), equity_usd, open_positions, note),
    )
    conn.commit()
    conn.close()


def get_all_trades():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY exit_time DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_equity_curve(limit=2000):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


def get_realized_pnl_total():
    conn = get_conn()
    row = conn.execute("SELECT COALESCE(SUM(net_pnl_usd), 0) AS total FROM trades").fetchone()
    conn.close()
    return row["total"]
