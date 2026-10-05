"""SQLite 儲存：每日收盤價與每日資產快照。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    date   TEXT NOT NULL,
    symbol TEXT NOT NULL,
    close  REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS snapshots (
    date              TEXT PRIMARY KEY,
    gross_assets      REAL NOT NULL,
    liabilities       REAL NOT NULL,
    net_equity        REAL NOT NULL,
    asset_leverage    REAL,
    equity_leverage   REAL,
    bond_ratio        REAL,
    min_maintenance   REAL,
    pledge_pnl        REAL,
    verdict           TEXT,
    detail            TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def save_prices(self, day: date, prices: dict[str, float], source: str) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices (date, symbol, close, source) VALUES (?, ?, ?, ?)",
            [(day.isoformat(), s, p, source) for s, p in prices.items()],
        )
        self.conn.commit()

    def last_price(self, symbol: str, on_or_before: date) -> tuple[date, float] | None:
        row = self.conn.execute(
            "SELECT date, close FROM prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol, on_or_before.isoformat()),
        ).fetchone()
        return (date.fromisoformat(row[0]), row[1]) if row else None

    def price_history(self, symbol: str, on_or_before: date, limit: int) -> list[float]:
        """回傳最近 limit 筆收盤價，由舊到新。"""
        rows = self.conn.execute(
            "SELECT close FROM prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT ?",
            (symbol, on_or_before.isoformat(), limit),
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def save_snapshot(self, day: date, summary: dict, verdict: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO snapshots
               (date, gross_assets, liabilities, net_equity, asset_leverage, equity_leverage,
                bond_ratio, min_maintenance, pledge_pnl, verdict, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                day.isoformat(),
                summary["gross_assets"],
                summary["liabilities"],
                summary["net_equity"],
                summary["asset_leverage"],
                summary["equity_leverage"],
                summary["bond_ratio"],
                summary["min_maintenance"],
                summary["pledge_pnl"],
                verdict,
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def snapshots(self, limit: int = 30) -> list[tuple]:
        rows = self.conn.execute(
            """SELECT date, gross_assets, liabilities, net_equity, asset_leverage, equity_leverage,
                      bond_ratio, min_maintenance, pledge_pnl, verdict
               FROM snapshots ORDER BY date DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return list(reversed(rows))
