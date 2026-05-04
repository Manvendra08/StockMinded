"""SQLite journal: trades + regime snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    regime TEXT,
    payload JSON
);

CREATE TABLE IF NOT EXISTS flow_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    payload JSON
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT,
    structure TEXT,
    side TEXT,
    qty INTEGER,
    entry REAL,
    stop REAL,
    target REAL,
    exit_price REAL,
    risk_rupees REAL,
    pnl_rupees REAL,
    regime TEXT,
    notes TEXT,
    planned_risk REAL,
    entry_rule TEXT,
    trail_rule TEXT,
    source_regime TEXT,
    skip_reason TEXT
);

CREATE TABLE IF NOT EXISTS skipped_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT,
    direction TEXT,
    alert_confidence TEXT,
    skip_reason TEXT,
    regime TEXT,
    flow_bias TEXT,
    risk_gate TEXT,
    notes TEXT
);
"""


class Journal:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def log_regime(self, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO regime_snapshots(ts, regime, payload) VALUES (?,?,?)",
            (datetime.utcnow().isoformat(), payload.get("regime"), json.dumps(payload)),
        )
        self.conn.commit()

    def log_flow(self, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO flow_snapshots(ts, payload) VALUES (?,?)",
            (datetime.utcnow().isoformat(), json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def open_trade(self, **kw) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades(opened_at, symbol, structure, side, qty, entry, stop, target, risk_rupees, regime, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kw.get("opened_at", datetime.utcnow().isoformat()),
                kw["symbol"], kw["structure"], kw["side"], kw["qty"],
                kw.get("entry"), kw.get("stop"), kw.get("target"),
                kw.get("risk_rupees"), kw.get("regime"), kw.get("notes", ""),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, pnl_rupees: float) -> None:
        self.conn.execute(
            "UPDATE trades SET closed_at=?, exit_price=?, pnl_rupees=? WHERE id=?",
            (datetime.utcnow().isoformat(), exit_price, pnl_rupees, trade_id),
        )
        self.conn.commit()

    def log_skipped_trade(self, symbol: str, direction: str, alert_confidence: str,
                         skip_reason: str, regime: str, flow_bias: str,
                         risk_gate: str, notes: str = "") -> None:
        """Log a trade that was skipped with reason for learning."""
        self.conn.execute(
            """INSERT INTO skipped_trades(ts, symbol, direction, alert_confidence,
               skip_reason, regime, flow_bias, risk_gate, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                datetime.utcnow().isoformat(),
                symbol, direction, alert_confidence,
                skip_reason, regime, flow_bias, risk_gate, notes,
            ),
        )
        self.conn.commit()

    def get_skipped_trades(self, limit: int = 50, since_date: str | None = None) -> list[dict]:
        """Retrieve skipped trades for analysis."""
        query = "SELECT * FROM skipped_trades"
        params = []
        if since_date:
            query += " WHERE ts >= ?"
            params.append(since_date)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        
        cur = self.conn.execute(query, params)
        rows = cur.fetchall()
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in rows]
