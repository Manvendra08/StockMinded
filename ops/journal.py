"""SQLite journal: trades + regime snapshots."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    """Canonical UTC timestamp used for EVERY Python-side write in this module.

    M7 FIX: centralising timestamp generation guarantees one consistent format
    (tz-aware UTC ISO-8601, e.g. ``2026-07-22T10:30:00.123456+00:00``) across all
    tables. Previously each call site repeated
    ``datetime.now(timezone.utc).isoformat()``, and that drift-prone duplication
    risked mixing formats. The range queries below additionally normalise BOTH
    sides via SQLite's ``datetime()`` so any residual format difference (naive vs
    aware, ``T`` vs space separator, fractional seconds, or the ``datetime('now')``
    schema default) can never cause a query to silently match nothing or everything.
    """
    return datetime.now(timezone.utc).isoformat()


# Schema version for migrations
SCHEMA_VERSION = 7

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

CREATE TABLE IF NOT EXISTS fundamentals_cache (
    symbol TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investment_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    scan_ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence TEXT NOT NULL,
    rationale TEXT,
    key_risks TEXT,
    entry_zone TEXT,
    stop_loss TEXT,
    target TEXT,
    telegram_msg_id INTEGER,
    telegram_channel TEXT,
    fundamentals_json TEXT,
    regime_at_scan TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_investment_scan ON investment_verdicts(scan_id);
CREATE INDEX IF NOT EXISTS idx_investment_symbol ON investment_verdicts(symbol);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    channel TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    date_str TEXT,
    text TEXT NOT NULL,
    platform TEXT DEFAULT 'telegram',
    UNIQUE(channel, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_channel ON raw_messages(channel);
CREATE INDEX IF NOT EXISTS idx_raw_messages_fetched ON raw_messages(fetched_at);

CREATE TABLE IF NOT EXISTS sahi_classifications (
    url TEXT PRIMARY KEY,
    title TEXT,
    symbol TEXT,
    company_name TEXT,
    news_event TEXT,
    event_type TEXT DEFAULT 'general',
    sentiment TEXT DEFAULT 'NEUTRAL',
    verdict TEXT,
    confidence REAL,
    classified_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sahi_classifications_symbol ON sahi_classifications(symbol);
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
    
    # Migration from v3 to v4: Add news/event columns to investment_verdicts.
    if current_version < 4:
        for col_sql in (
            "ALTER TABLE investment_verdicts ADD COLUMN news_event TEXT",
            "ALTER TABLE investment_verdicts ADD COLUMN event_type TEXT DEFAULT 'general'",
            "ALTER TABLE investment_verdicts ADD COLUMN sentiment_direction TEXT DEFAULT 'NEUTRAL'",
            "ALTER TABLE investment_verdicts ADD COLUMN company_name TEXT",
        ):
            try:
                cursor.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists

    # Migration from v5 to v6: Add source_platform to investment_verdicts.
    if current_version < 6:
        try:
            cursor.execute("ALTER TABLE investment_verdicts ADD COLUMN source_platform TEXT DEFAULT 'telegram'")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migration from v6 to v7: Add sahi_classifications table for persistent
    # per-article news classification (stable card verdict badges).
    if current_version < 7:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sahi_classifications (
                url TEXT PRIMARY KEY,
                title TEXT,
                symbol TEXT,
                company_name TEXT,
                news_event TEXT,
                event_type TEXT DEFAULT 'general',
                sentiment TEXT DEFAULT 'NEUTRAL',
                verdict TEXT,
                confidence REAL,
                classified_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sahi_classifications_symbol ON sahi_classifications(symbol)"
        )

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
                _utc_now_iso(),
                payload.get("regime"),
                json.dumps(payload),
            ),
        )
        self.conn.commit()

    def log_flow(self, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO flow_snapshots(ts, payload) VALUES (?,?)",
            (_utc_now_iso(), json.dumps(payload, default=str)),
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
                kw.get("opened_at", _utc_now_iso()),
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
            (_utc_now_iso(), exit_price, pnl_rupees, trade_id),
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
                _utc_now_iso(),
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
            # M7 FIX: normalise BOTH sides with SQLite's datetime() so the
            # comparison is robust to format drift (tz-aware vs naive, 'T' vs
            # space separator, fractional seconds). A plain string compare here
            # silently broke whenever the stored `ts` and the caller-supplied
            # `since_date` used different ISO flavours.
            query += " WHERE datetime(ts) >= datetime(?)"
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
        threshold_iso = threshold.isoformat()

        # M7 FIX: normalise BOTH sides with SQLite's datetime() so pruning is
        # robust to format drift. A raw `ts < ?` string compare could silently
        # delete nothing (or everything) if the stored `ts` format ever differed
        # from the threshold's ISO flavour. datetime() parses both to a canonical
        # 'YYYY-MM-DD HH:MM:SS' UTC form first; unparseable rows yield NULL and
        # are safely left untouched (fail-closed for deletion).
        cur = self.conn.execute(
            "DELETE FROM skipped_trades WHERE datetime(ts) < datetime(?)",
            (threshold_iso,),
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
                    _utc_now_iso(),
                    loss_root_cause,
                    json.dumps(timing_at_exit or {}, default=str),
                    notes,
                ),
            )
        self.conn.commit()

    def save_investment_verdicts(self, scan_id: str, scan_ts: str, verdicts: list[dict]) -> int:
        """Insert a batch of verdicts for one pipeline scan.

        ``verdicts`` items are dicts with keys:
            symbol, verdict, confidence, rationale, key_risks, entry_zone,
            stop_loss, target, telegram_msg_id, telegram_channel,
            fundamentals_json, regime_at_scan,
            news_event, event_type, sentiment_direction, company_name,
            source_platform
        The UNIQUE(scan_id, symbol) constraint prevents duplicate rows on re-run.
        Returns the number of rows written.
        """
        written = 0
        for v in verdicts:
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO investment_verdicts(
                        scan_id, scan_ts, symbol, verdict, confidence, rationale,
                        key_risks, entry_zone, stop_loss, target, telegram_msg_id,
                        telegram_channel, fundamentals_json, regime_at_scan,
                        news_event, event_type, sentiment_direction, company_name,
                        source_platform)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        scan_id,
                        scan_ts,
                        v.get("symbol"),
                        v.get("verdict"),
                        v.get("confidence"),
                        v.get("rationale"),
                        v.get("key_risks"),
                        v.get("entry_zone"),
                        v.get("stop_loss"),
                        v.get("target"),
                        v.get("telegram_msg_id"),
                        v.get("telegram_channel"),
                        v.get("fundamentals_json"),
                        v.get("regime_at_scan"),
                        v.get("news_event"),
                        v.get("event_type"),
                        v.get("sentiment_direction"),
                        v.get("company_name"),
                        v.get("source_platform", "telegram"),
                    ),
                )
                written += 1
            except sqlite3.OperationalError:
                pass
        self.conn.commit()
        return written

    def has_recent_verdict(
        self,
        symbol: str,
        news_event: str = "",
        msg_id: int | None = None,
        max_age_hours: int = 24,
        source_platform: str = "",
    ) -> bool:
        """Check if a verdict for this symbol & news event/msg_id exists within max_age_hours.

        Only skips re-processing when there is a strong match:
        - For Telegram: match by msg_id (exact) OR news_event prefix AND same source_platform.
        - For Sahi: match only by news_event prefix AND source_platform='sahi'.
        - Never block cross-platform processing (Telegram verdict does not block Sahi re-run).
        """
        if not symbol:
            return False

        # Exact msg_id match — only for Telegram messages with a real msg_id
        if msg_id and msg_id > 0 and source_platform in ("telegram", ""):
            cur = self.conn.execute(
                """SELECT 1 FROM investment_verdicts
                   WHERE symbol = ? AND telegram_msg_id = ?
                     AND datetime(created_at) >= datetime('now', ?) LIMIT 1""",
                (symbol, msg_id, f"-{max_age_hours} hours"),
            )
            if cur.fetchone():
                return True

        # News event prefix match — only skip if same source_platform AND meaningful news_event
        if news_event and source_platform:
            prefix = news_event[:35].strip().lower()
            if len(prefix) >= 10:  # require at least 10 chars to avoid false positives
                cur = self.conn.execute(
                    """SELECT news_event FROM investment_verdicts
                        WHERE symbol = ? AND source_platform = ?
                          AND datetime(created_at) >= datetime('now', ?)""",
                    (symbol, source_platform, f"-{max_age_hours} hours"),
                )
                for row in cur.fetchall():
                    existing = (row[0] or "").strip().lower()
                    if existing and (prefix in existing or existing[:35] == prefix):
                        return True
        return False

    def deduplicate_investment_verdicts(self) -> int:
        """Delete true duplicate rows - same symbol + news_event AND same scan_id.

        IMPORTANT: Only deduplicates within the same scan_id. Historical rows from
        earlier scans are preserved so the Verdicts table shows history.
        Rows with NULL/empty news_event are never deleted (they may be from different
        Telegram messages for the same symbol).
        """
        cur = self.conn.execute(
            """DELETE FROM investment_verdicts
               WHERE id NOT IN (
                   SELECT MAX(id)
                   FROM investment_verdicts
                   WHERE news_event IS NOT NULL AND TRIM(news_event) != ''
                   GROUP BY scan_id, symbol, source_platform, LOWER(SUBSTR(TRIM(news_event), 1, 35))
               )
               AND news_event IS NOT NULL AND TRIM(news_event) != ''
               AND id NOT IN (
                   -- Also keep the latest row per scan_id + source_platform even if news_event matches
                   SELECT MAX(id) FROM investment_verdicts GROUP BY scan_id, symbol, source_platform
               )"""
        )
        deleted = cur.rowcount
        self.conn.commit()
        return deleted

    def get_investment_scans(self, limit: int = 50) -> list[dict]:
        """Return recent scans, each with its verdict rows.

        Response shape (matches plan §5.2):
            [{scan_id, scan_ts, verdicts: [{...}, ...]}, ...]
        """
        cur = self.conn.execute(
            """SELECT scan_id, scan_ts FROM investment_verdicts
               GROUP BY scan_id ORDER BY MAX(created_at) DESC LIMIT ?""",
            (limit,),
        )
        scans = []
        for scan_id, scan_ts in cur.fetchall():
            vcur = self.conn.execute(
                """SELECT id, scan_id, scan_ts, symbol, verdict, confidence,
                          rationale, key_risks, entry_zone, stop_loss, target,
                          telegram_msg_id, telegram_channel, regime_at_scan,
                          news_event, event_type, sentiment_direction, company_name,
                          source_platform
                   FROM investment_verdicts WHERE scan_id = ? ORDER BY symbol""",
                (scan_id,),
            )
            cols = [d[0] for d in vcur.description]
            verdicts = [dict(zip(cols, row)) for row in vcur.fetchall()]
            scans.append({"scan_id": scan_id, "scan_ts": scan_ts, "verdicts": verdicts})
        return scans

    def get_investment_scan(self, scan_id: str) -> dict | None:
        cur = self.conn.execute(
            """SELECT id, scan_id, scan_ts, symbol, verdict, confidence,
                      rationale, key_risks, entry_zone, stop_loss, target,
                      telegram_msg_id, telegram_channel, regime_at_scan,
                      news_event, event_type, sentiment_direction, company_name,
                      source_platform
               FROM investment_verdicts WHERE scan_id = ? ORDER BY symbol""",
            (scan_id,),
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            return None
        verdicts = [dict(zip(cols, row)) for row in rows]
        return {
            "scan_id": scan_id,
            "scan_ts": verdicts[0]["scan_ts"],
            "verdicts": verdicts,
        }

    def get_investment_summary(self) -> dict:
        """Summary cards: total scans, total verdicts, counts by verdict."""
        total_scans = self.conn.execute(
            "SELECT COUNT(DISTINCT scan_id) FROM investment_verdicts"
        ).fetchone()[0]
        total_verdicts = self.conn.execute(
            "SELECT COUNT(*) FROM investment_verdicts"
        ).fetchone()[0]
        by_verdict = {"BUY": 0, "SELL": 0, "AVOID": 0}
        cur = self.conn.execute(
            "SELECT verdict, COUNT(*) FROM investment_verdicts GROUP BY verdict"
        )
        for verdict, count in cur.fetchall():
            if verdict in by_verdict:
                by_verdict[verdict] = count
        return {
            "total_scans": total_scans,
            "total_verdicts": total_verdicts,
            "by_verdict": by_verdict,
        }

    def prune_investment_scans(self, keep: int) -> int:
        """Keep only the most recent ``keep`` scans; delete older ones."""
        cur = self.conn.execute(
            """SELECT scan_id FROM investment_verdicts
               GROUP BY scan_id ORDER BY MAX(created_at) DESC LIMIT -1 OFFSET ?""",
            (keep,),
        )
        old_ids = [r[0] for r in cur.fetchall()]
        if not old_ids:
            return 0
        deleted = 0
        for sid in old_ids:
            deleted += self.conn.execute(
                "DELETE FROM investment_verdicts WHERE scan_id = ?", (sid,)
            ).rowcount
        self.conn.commit()
        return deleted

    def prune_investment_verdicts(self, max_age_hours: int = 24) -> int:
        """Delete verdicts older than ``max_age_hours`` (all verdict values)."""
        deleted = self.conn.execute(
            """DELETE FROM investment_verdicts
               WHERE datetime(created_at) < datetime('now', ?)""",
            (f"-{max_age_hours} hours",),
        ).rowcount
        self.conn.commit()
        return deleted

    def clear_all_investment_verdicts(self) -> int:
        """Delete all investment verdicts from the database to start fresh."""
        deleted = self.conn.execute("DELETE FROM investment_verdicts").rowcount
        self.conn.commit()
        return deleted

    def cache_fundamentals(self, symbol: str, payload: dict, fetched_at: str) -> None:
        self.conn.execute(
            """INSERT INTO fundamentals_cache(symbol, fetched_at, payload_json)
               VALUES (?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 fetched_at = excluded.fetched_at,
                 payload_json = excluded.payload_json""",
            (symbol, fetched_at, json.dumps(payload, default=str)),
        )
        self.conn.commit()

    def get_cached_fundamentals(self, symbol: str, max_age_hours: int = 24) -> dict | None:
        cur = self.conn.execute(
            "SELECT payload_json, fetched_at FROM fundamentals_cache WHERE symbol = ?",
            (symbol,),
        )
        row = cur.fetchone()
        if not row:
            return None
        payload_json, fetched_at = row
        try:
            from datetime import datetime, timedelta, timezone

            fetched_dt = datetime.fromisoformat(fetched_at)
            if datetime.now(timezone.utc) - fetched_dt > timedelta(hours=max_age_hours):
                return None
        except Exception:
            pass
        try:
            return json.loads(payload_json)
        except Exception:
            return None

    def log_raw_messages(self, messages: list, platform_map: dict | None = None) -> None:
        """Log raw messages/tweets fetched from Telegram/X channels.

        Only messages that pass the is_stock_specific() heuristic are stored.
        Macro/political/non-company messages are dropped at ingestion time to
        keep the raw_messages table and the Live Source Feed uncluttered.
        """
        from signals.telegram_parser import is_stock_specific
        for m in messages:
            text = getattr(m, "text", "") or ""
            if not is_stock_specific(text):
                continue  # Drop non-company-specific messages
            pl = (platform_map or {}).get(getattr(m, "channel", ""), "telegram")
            self.conn.execute(
                """INSERT INTO raw_messages(fetched_at, channel, msg_id, date_str, text, platform)
                   VALUES (datetime('now'), ?, ?, ?, ?, ?)
                   ON CONFLICT(channel, msg_id) DO UPDATE SET
                     fetched_at = datetime('now'),
                     text = excluded.text,
                     date_str = excluded.date_str,
                     platform = excluded.platform""",
                (
                    getattr(m, "channel", ""),
                    getattr(m, "msg_id", 0),
                    str(getattr(m, "date", "") or ""),
                    text,
                    pl,
                ),
            )
        self.conn.commit()

    def get_raw_messages(self, limit: int = 50, channel: str | None = None) -> list[dict]:
        """Fetch raw messages/tweets ordered by newest first.

        When channel is None, uses a windowed query (ROW_NUMBER per channel) to ensure
        all active channels get fair representation instead of high-volume channels
        starving low-volume signal channels.
        """
        if channel:
            query = "SELECT id, fetched_at, channel, msg_id, date_str, text, platform FROM raw_messages WHERE channel = ? ORDER BY id DESC LIMIT ?"
            params = [channel, limit]
        else:
            query = """
            WITH Ranked AS (
                SELECT id, fetched_at, channel, msg_id, date_str, text, platform,
                       ROW_NUMBER() OVER (PARTITION BY channel ORDER BY id DESC) as rn
                FROM raw_messages
            )
            SELECT id, fetched_at, channel, msg_id, date_str, text, platform
            FROM Ranked
            WHERE rn <= ?
            ORDER BY id DESC
            LIMIT ?
            """
            per_ch = max(10, limit // 2)
            params = [per_ch, limit]

        cur = self.conn.execute(query, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Post-filter: remove non-stock-specific messages that were stored before
        # the ingestion-level filter was added (backward-compatible cleanup).
        try:
            from signals.telegram_parser import is_stock_specific
            rows = [r for r in rows if is_stock_specific(r.get("text", ""))]
        except Exception:
            pass  # If import fails, return all rows unfiltered

        return rows

    def save_sahi_classifications(self, rows: list[dict]) -> int:
        """Upsert per-article sahi.com classifications (keyed by article URL).

        Each row may contain: url, title, symbol, company_name, news_event,
        event_type, sentiment, verdict, confidence. Persisting the
        classification per article is what keeps the Investment dashboard card
        badges stable across reloads (they are no longer recomputed client-side
        from transient data).
        """
        written = 0
        for r in rows:
            url = r.get("url")
            if not url:
                continue
            self.conn.execute(
                """INSERT INTO sahi_classifications(
                       url, title, symbol, company_name, news_event,
                       event_type, sentiment, verdict, confidence, classified_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                       title = excluded.title,
                       symbol = excluded.symbol,
                       company_name = excluded.company_name,
                       news_event = excluded.news_event,
                       event_type = excluded.event_type,
                       sentiment = excluded.sentiment,
                       verdict = excluded.verdict,
                       confidence = excluded.confidence,
                       classified_at = excluded.classified_at""",
                (
                    url,
                    r.get("title"),
                    r.get("symbol"),
                    r.get("company_name"),
                    r.get("news_event"),
                    r.get("event_type", "general"),
                    r.get("sentiment", "NEUTRAL"),
                    r.get("verdict"),
                    r.get("confidence"),
                    _utc_now_iso(),
                ),
            )
            written += 1
        self.conn.commit()
        return written

    def get_sahi_classifications(self, urls: list[str] | None = None) -> dict[str, dict]:
        """Return stored sahi.com classifications keyed by article URL.

        When ``urls`` is provided only those articles are returned; otherwise
        every stored classification is returned.
        """
        query = (
            "SELECT url, title, symbol, company_name, news_event, event_type, "
            "sentiment, verdict, confidence, classified_at FROM sahi_classifications"
        )
        params: list = []
        if urls:
            placeholders = ",".join("?" for _ in urls)
            query += f" WHERE url IN ({placeholders})"
            params.extend(urls)
        cur = self.conn.execute(query, params)
        cols = [d[0] for d in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}

    def get_latest_verdicts_by_symbol(self) -> dict[str, dict]:
        """Return the most recent investment verdict for each symbol.

        Used to enrich sahi.com card badges with the actual BUY/SELL/AVOID
        verdict produced by the LLM fusion pass (from investment_verdicts),
        rather than only the raw news sentiment.
        """
        cur = self.conn.execute(
            """SELECT symbol, verdict, confidence, rationale, news_event,
                      company_name, scan_ts
               FROM investment_verdicts v
               WHERE scan_ts = (
                   SELECT MAX(scan_ts) FROM investment_verdicts
                   WHERE symbol = v.symbol
               )"""
        )
        cols = [d[0] for d in cur.description]
        out: dict[str, dict] = {}
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            sym = d.get("symbol")
            if sym:
                out[sym] = d
        return out

    def close(self) -> None:
        self.conn.close()
