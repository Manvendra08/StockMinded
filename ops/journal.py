"""SQLite journal: trades + regime snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Schema version for migrations
SCHEMA_VERSION = 3

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO schema_version (version) VALUES (0);

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
    skip_reason TEXT,
    entry_quality TEXT,
    loss_root_cause TEXT,
    timing_snapshot JSON,
    event_risk_mode INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS trade_exit_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER UNIQUE NOT NULL,
    ts TEXT NOT NULL,
    loss_root_cause TEXT,
    timing_at_exit JSON,
    notes TEXT,
    FOREIGN KEY(trade_id) REFERENCES trades(id)
);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply schema migrations to bring database up to SCHEMA_VERSION."""
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cursor.fetchone()
    current_version = row[0] if row else 0
    
    if current_version >= SCHEMA_VERSION:
        return
    
    # Migration from v1 to v2: Add new columns to trades table.
    # Use "IF NOT EXISTS" semantics via try/except because SCHEMA may
    # already define these columns on a fresh database.
    if current_version < 2:
        for col_sql in (
            "ALTER TABLE trades ADD COLUMN source_regime TEXT",
            "ALTER TABLE trades ADD COLUMN skip_reason TEXT",
            "ALTER TABLE trades ADD COLUMN entry_quality TEXT",
            "ALTER TABLE trades ADD COLUMN loss_root_cause TEXT",
            "ALTER TABLE trades ADD COLUMN timing_snapshot JSON",
            "ALTER TABLE trades ADD COLUMN event_risk_mode INTEGER DEFAULT 0",
        ):
            try:
                cursor.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists
    
    # Migration from v2 to v3: Add new tables
    if current_version < 3:
        cursor.execute("""
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
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_exit_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER UNIQUE NOT NULL,
                ts TEXT NOT NULL,
                loss_root_cause TEXT,
                timing_at_exit JSON,
                notes TEXT,
                FOREIGN KEY(trade_id) REFERENCES trades(id)
            )
        """)
    
    cursor.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
        (SCHEMA_VERSION,)
    )
    conn.commit()


class Journal:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        _migrate_schema(self.conn)
        self.conn.commit()

    def log_regime(self, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO regime_snapshots(ts, regime, payload) VALUES (?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                payload.get("regime"),
                json.dumps(payload),
            ),
        )
        self.conn.commit()

    def log_flow(self, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO flow_snapshots(ts, payload) VALUES (?,?)",
            (datetime.now(timezone.utc).isoformat(), json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def open_trade(self, **kw) -> int:
        # BUG-27 FIX: Validate required kwargs upfront instead of letting
        # KeyError propagate from deep inside the execute call.
        _required = ("symbol", "structure", "side", "qty")
        missing = [k for k in _required if k not in kw]
        if missing:
            raise ValueError(
                f"open_trade() missing required kwargs: {', '.join(missing)}"
            )
        cur = self.conn.execute(
            """INSERT INTO trades(opened_at, symbol, structure, side, qty, entry, stop, target, risk_rupees, regime, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kw.get("opened_at", datetime.now(timezone.utc).isoformat()),
                kw["symbol"],
                kw["structure"],
                kw["side"],
                kw["qty"],
                kw.get("entry"),
                kw.get("stop"),
                kw.get("target"),
                kw.get("risk_rupees"),
                kw.get("regime"),
                kw.get("notes", ""),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_price: float, pnl_rupees: float) -> None:
        self.conn.execute(
            "UPDATE trades SET closed_at=?, exit_price=?, pnl_rupees=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), exit_price, pnl_rupees, trade_id),
        )
        self.conn.commit()

    def log_skipped_trade(
        self,
        symbol: str,
        direction: str,
        alert_confidence: str,
        skip_reason: str,
        regime: str,
        flow_bias: str,
        risk_gate: str,
        notes: str = "",
    ) -> None:
        """Log a trade that was skipped with reason for learning."""
        self.conn.execute(
            """INSERT INTO skipped_trades(ts, symbol, direction, alert_confidence,
               skip_reason, regime, flow_bias, risk_gate, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol,
                direction,
                alert_confidence,
                skip_reason,
                regime,
                flow_bias,
                risk_gate,
                notes,
            ),
        )
        self.conn.commit()

    def get_skipped_trades(
        self, limit: int = 50, since_date: str | None = None
    ) -> list[dict]:
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

    def has_skipped_today(
        self,
        symbol: str,
        skip_reason: str,
        risk_gate: str,
    ) -> bool:
        """Check if a skip log entry already exists today for the same
        symbol + skip_reason + risk_gate combo.
        Prevents redundant logging across repeated engine cycles.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = self.conn.execute(
            """SELECT 1 FROM skipped_trades
               WHERE date(ts) = ?
                 AND symbol = ?
                 AND skip_reason = ?
                 AND risk_gate = ?
               LIMIT 1""",
            (today, symbol, skip_reason, risk_gate),
        )
        return cur.fetchone() is not None

    def clear_skipped_trades(self, older_than_days: int) -> int:
        """Clear skipped trades older than N days.

        Normalizes timestamps to a consistent UTC ISO format to avoid string
        comparison mismatches between tz-aware and tz-naive values.
        """
        from datetime import timedelta

        threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)

        # Store `ts` using datetime.now(timezone.utc).isoformat() elsewhere in this module.
        threshold_iso = threshold.isoformat()

        cur = self.conn.execute(
            "DELETE FROM skipped_trades WHERE ts < ?", (threshold_iso,)
        )
        self.conn.commit()
        return cur.rowcount

    def log_entry_quality(
        self,
        symbol: str,
        direction: str,
        entry_quality: str,
        timing_snapshot: dict | None,
        event_risk_mode: bool = False,
        trade_id: int | None = None,
    ) -> None:
        """Log entry quality and timing snapshot for a trade.

        Args:
            symbol: Stock ticker
            direction: LONG or SHORT
            entry_quality: GOOD | LATE | CHASING | REVERSAL
            timing_snapshot: Dict with VWAP, RSI, breadth, etc.
            event_risk_mode: Whether trade entered during event risk
            trade_id: ID to update if known
        """
        if trade_id:
            self.conn.execute(
                """UPDATE trades SET entry_quality=?, timing_snapshot=?, event_risk_mode=?
                   WHERE id=?""",
                (
                    entry_quality,
                    json.dumps(timing_snapshot or {}, default=str),
                    1 if event_risk_mode else 0,
                    trade_id,
                ),
            )
            self.conn.commit()

    def log_loss_root_cause(
        self,
        trade_id: int,
        loss_root_cause: str,
        timing_at_exit: dict | None = None,
        notes: str = "",
    ) -> None:
        """Assign root cause post-trade (during exit or backtest).

        Args:
            trade_id: Trade ID
            loss_root_cause: LATE_ENTRY | MARKET_REVERSAL | SENTIMENT_FLIP | OVEREXTENDED | NORMAL
            timing_at_exit: Timing snapshot at exit
            notes: Additional notes
        """
        # First try to update existing record
        existing = self.conn.execute(
            "SELECT id FROM trade_exit_analysis WHERE trade_id=?", (trade_id,)
        ).fetchone()

        if existing:
            # Update
            self.conn.execute(
                """UPDATE trade_exit_analysis SET loss_root_cause=?, timing_at_exit=?, notes=?
                   WHERE trade_id=?""",
                (
                    loss_root_cause,
                    json.dumps(timing_at_exit or {}, default=str),
                    notes,
                    trade_id,
                ),
            )
        else:
            # Insert
            self.conn.execute(
                """INSERT INTO trade_exit_analysis(trade_id, ts, loss_root_cause, timing_at_exit, notes)
                   VALUES (?,?,?,?,?)""",
                (
                    trade_id,
                    datetime.now(timezone.utc).isoformat(),
                    loss_root_cause,
                    json.dumps(timing_at_exit or {}, default=str),
                    notes,
                ),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
