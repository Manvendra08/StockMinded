"""Tests for skipped trades API endpoint."""
from unittest.mock import MagicMock, patch

from dashboard import server


def test_skipped_trades_summary_counts():
    client = server.app.test_client()
    rows = [
        {"skip_reason": "NOT_DIRECTIONAL", "risk_gate": "paper_equity_only"},
        {"skip_reason": "RISK_GATE", "risk_gate": "daily_stop"},
        {"skip_reason": "RISK_GATE", "risk_gate": "daily_stop"},
    ]
    mock_journal = MagicMock()
    mock_journal.get_skipped_trades.return_value = rows

    with patch("dashboard.server.load_config", return_value={"paths": {"journal_db": "dummy"}}):
        with patch("dashboard.server.Journal", return_value=mock_journal):
            resp = client.get("/api/paper/skipped?date=2026-04-29&limit=50")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["summary"]["total"] == 3
    assert data["summary"]["by_reason"]["RISK_GATE"] == 2
    assert data["summary"]["by_gate"]["daily_stop"] == 2


def test_skipped_trades_invalid_date():
    client = server.app.test_client()
    mock_journal = MagicMock()
    mock_journal.get_skipped_trades.return_value = []

    with patch("dashboard.server.load_config", return_value={"paths": {"journal_db": "dummy"}}):
        with patch("dashboard.server.Journal", return_value=mock_journal):
            resp = client.get("/api/paper/skipped?date=2026-99-99")

    assert resp.status_code == 400
