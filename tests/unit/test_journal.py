"""Tests for ops/journal.py."""
import json
import sqlite3
import pytest
from ops.journal import Journal


@pytest.fixture
def journal(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    return Journal(db_path)


class TestJournal:
    def test_tables_created_on_init(self, journal):
        tables = {row[0] for row in journal.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "regime_snapshots" in tables
        assert "flow_snapshots" in tables
        assert "trades" in tables

    def test_log_regime_inserts_row(self, journal):
        payload = {"regime": "TREND_UP", "trend_score": 3, "vix": 12.5}
        journal.log_regime(payload)
        rows = journal.conn.execute("SELECT regime, payload FROM regime_snapshots").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "TREND_UP"
        stored = json.loads(rows[0][1])
        assert stored["trend_score"] == 3

    def test_log_flow_inserts_row(self, journal):
        payload = {"smart_money_bias": "LONG", "pcr_oi": 1.3}
        journal.log_flow(payload)
        rows = journal.conn.execute("SELECT payload FROM flow_snapshots").fetchall()
        assert len(rows) == 1
        stored = json.loads(rows[0][0])
        assert stored["smart_money_bias"] == "LONG"

    def test_open_trade_returns_id(self, journal):
        trade_id = journal.open_trade(
            symbol="RELIANCE",
            structure="primary",
            side="long",
            qty=100,
            entry=2500.0,
            stop=2475.0,
            target=2560.0,
            risk_rupees=2500.0,
            regime="TREND_UP",
        )
        assert isinstance(trade_id, int)
        assert trade_id > 0

    def test_close_trade_sets_exit_and_pnl(self, journal):
        trade_id = journal.open_trade(
            symbol="INFY", structure="secondary", side="long",
            qty=50, entry=1800.0, stop=1780.0, target=1840.0,
            risk_rupees=1000.0, regime="TREND_UP",
        )
        journal.close_trade(trade_id, exit_price=1830.0, pnl_rupees=1500.0)
        row = journal.conn.execute(
            "SELECT exit_price, pnl_rupees, closed_at FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        assert row[0] == 1830.0
        assert row[1] == 1500.0
        assert row[2] is not None

    def test_multiple_regime_logs(self, journal):
        for regime in ["TREND_UP", "RANGE_LOW_VOL", "VOL_EXPANSION"]:
            journal.log_regime({"regime": regime})
        count = journal.conn.execute("SELECT COUNT(*) FROM regime_snapshots").fetchone()[0]
        assert count == 3

    def test_idempotent_schema_creation(self, tmp_path):
        db_path = str(tmp_path / "idem.sqlite")
        j1 = Journal(db_path)
        j2 = Journal(db_path)  # should not raise
        j2.log_regime({"regime": "TREND_DOWN"})
        rows = j1.conn.execute("SELECT * FROM regime_snapshots").fetchall()
        assert len(rows) >= 0  # no error
