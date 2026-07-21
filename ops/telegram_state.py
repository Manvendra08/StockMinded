"""Persistent Telegram channel state (last processed message id per channel).

State is stored in the shared journal DB so there is a single SQLite file.
This module is additive — it only touches a dedicated ``telegram_state`` table
and never reads/writes existing tables.
"""

from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_state (
    channel TEXT PRIMARY KEY,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class TelegramState:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def get_last_msg_id(self, channel: str) -> int:
        cur = self.conn.execute(
            "SELECT last_msg_id FROM telegram_state WHERE channel = ?", (channel,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def set_last_msg_id(self, channel: str, msg_id: int) -> None:
        self.conn.execute(
            """INSERT INTO telegram_state(channel, last_msg_id, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(channel) DO UPDATE SET
                 last_msg_id = excluded.last_msg_id,
                 updated_at = datetime('now')""",
            (channel, int(msg_id)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
