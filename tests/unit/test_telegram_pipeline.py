"""Unit tests for the dual-engine Telegram pipeline (no external calls)."""
import json

import pytest

from ops.journal import Journal
from ops.telegram_state import TelegramState
from signals.telegram_parser import parse_message, ExtractedTicker
from signals.telegram_fusion import apply_hard_filters, run_fusion, Verdict


# ── TelegramState ────────────────────────────────────────────────────────────
class TestTelegramState:
    def test_roundtrip(self, tmp_path):
        st = TelegramState(str(tmp_path / "tg.sqlite"))
        assert st.get_last_msg_id("ch1") == 0
        st.set_last_msg_id("ch1", 42)
        assert st.get_last_msg_id("ch1") == 42
        st.set_last_msg_id("ch1", 100)
        assert st.get_last_msg_id("ch1") == 100
        st.close()


# ── Parser ───────────────────────────────────────────────────────────────────
def _fake_llm_tickers():
    return {
        "mentions": [
            {"symbol": "Reliance.NS", "confidence": 0.9, "company_name": "Reliance Industries", "news_event": "breakout above 2500"},
            {"symbol": "NIFTY", "confidence": 0.8, "company_name": "Nifty Index", "news_event": "index call"},
            {"symbol": "TATAMOTORS", "confidence": 0.3, "company_name": "Tata Motors", "news_event": "weak"},
        ]
    }


def test_parser_normalizes_and_filters(monkeypatch):
    monkeypatch.setattr(
        "data.ai_scraper.call_llm",
        lambda *a, **k: _fake_llm_tickers(),
    )
    out = parse_message(
        "buy reliance", universe={"RELIANCE", "TATAMOTORS"}, min_confidence=0.5
    )
    symbols = {t.symbol for t in out}
    assert "RELIANCE" in symbols
    assert "NIFTY" not in symbols  # indices filtered by universe
    assert "TATAMOTORS" not in symbols  # low confidence dropped


def test_parser_empty_response():
    out = parse_message("", universe={"RELIANCE"})
    assert out == []


# ── Fusion hard filters ──────────────────────────────────────────────────────
class TestHardFilters:
    GOOD = {
        "debt_to_equity": 0.8,
        "roce_pct": 22,
        "promoter_pledge_pct": 5,
        "sales_growth_3y_pct": 18,
        "profit_growth_3y_pct": 20,
    }
    FILTERS = {
        "max_debt_to_equity": 1.5,
        "min_roce_pct": 15,
        "max_promoter_pledge_pct": 20,
        "min_sales_growth_3y_pct": 10,
        "min_profit_growth_3y_pct": 10,
    }

    def test_pass(self):
        ok, reason = apply_hard_filters(self.GOOD, self.FILTERS)
        assert ok is True

    def test_reject_high_debt(self):
        f = dict(self.GOOD, debt_to_equity=2.0)
        ok, reason = apply_hard_filters(f, self.FILTERS)
        assert ok is False and "debt" in reason.lower()

    def test_reject_low_roce(self):
        f = dict(self.GOOD, roce_pct=10)
        ok, reason = apply_hard_filters(f, self.FILTERS)
        assert ok is False and "capital" in reason.lower()

    def test_missing_data_skipped(self):
        f = dict(self.GOOD, promoter_pledge_pct=None)
        ok, reason = apply_hard_filters(f, self.FILTERS)
        assert ok is True


# ── Fusion end-to-end (mocked LLM) ───────────────────────────────────────────
def _fake_fusion_llm(*a, **k):
    return json.dumps({
        "symbol": "RELIANCE",
        "verdict": "BUY",
        "confidence": "HIGH",
        "rationale": "Strong breakout with clean fundamentals",
        "key_risks": "Market correction",
        "entry_zone": "2500-2520",
        "stop_loss": "2440",
        "target": "2700",
    })


def test_run_fusion_filters_then_fuses(monkeypatch):
    monkeypatch.setattr(
        "data.ai_scraper.call_llm", lambda *a, **k: _fake_fusion_llm()
    )
    extracted = [
        ExtractedTicker("RELIANCE", 0.9, "breakout"),
        ExtractedTicker("WEAKCO", 0.8, "avoid"),
    ]

    def fund_fn(sym):
        if sym == "RELIANCE":
            return {"debt_to_equity": 0.8, "roce_pct": 22, "promoter_pledge_pct": 5,
                    "sales_growth_3y_pct": 18, "profit_growth_3y_pct": 20}
        return {"debt_to_equity": 3.0, "roce_pct": 5, "promoter_pledge_pct": 40,
                "sales_growth_3y_pct": 2, "profit_growth_3y_pct": 1}

    verdicts = run_fusion(
        extracted=extracted,
        fundamentals_fn=fund_fn,
        filters=TestHardFilters.FILTERS,
        regime="TREND_UP",
        call_llm=None,
    )
    by_sym = {v.symbol: v for v in verdicts}
    assert by_sym["RELIANCE"].verdict == "BUY"
    assert by_sym["RELIANCE"].confidence == "HIGH"
    assert by_sym["WEAKCO"].verdict == "AVOID"  # hard filter rejection


# ── Journal investment CRUD ──────────────────────────────────────────────────
class TestInvestmentJournal:
    def test_save_and_read_scans(self, tmp_path):
        j = Journal(str(tmp_path / "inv.sqlite"))
        j.save_investment_verdicts(
            "scan1", "2026-01-01T00:00:00+00:00",
            [
                {"symbol": "RELIANCE", "verdict": "BUY", "confidence": "HIGH",
                 "rationale": "r", "key_risks": "k", "entry_zone": "1", "stop_loss": "2",
                 "target": "3", "telegram_msg_id": 5, "telegram_channel": "ch",
                 "fundamentals_json": "{}", "regime_at_scan": "TREND_UP"},
                {"symbol": "INFY", "verdict": "AVOID", "confidence": "LOW",
                 "rationale": "r", "key_risks": "", "entry_zone": "", "stop_loss": "",
                 "target": "", "telegram_msg_id": 6, "telegram_channel": "ch",
                 "fundamentals_json": "{}", "regime_at_scan": "TREND_UP"},
            ],
        )
        scans = j.get_investment_scans(limit=10)
        assert len(scans) == 1
        assert len(scans[0]["verdicts"]) == 2
        detail = j.get_investment_scan("scan1")
        assert detail["scan_id"] == "scan1"
        summary = j.get_investment_summary()
        assert summary["total_scans"] == 1
        assert summary["by_verdict"]["BUY"] == 1
        assert summary["by_verdict"]["AVOID"] == 1

    def test_dedup_on_rerun(self, tmp_path):
        j = Journal(str(tmp_path / "inv.sqlite"))
        v = {"symbol": "RELIANCE", "verdict": "BUY", "confidence": "HIGH",
             "rationale": "r", "key_risks": "", "entry_zone": "", "stop_loss": "",
             "target": "", "telegram_msg_id": 1, "telegram_channel": "c",
             "fundamentals_json": "{}", "regime_at_scan": "X"}
        j.save_investment_verdicts("s1", "t", [v])
        j.save_investment_verdicts("s1", "t", [v])  # same scan_id+symbol -> ignored
        assert len(j.get_investment_scans(10)[0]["verdicts"]) == 1

    def test_prune_keeps_most_recent(self, tmp_path):
        j = Journal(str(tmp_path / "inv.sqlite"))
        for i in range(3):
            j.save_investment_verdicts(
                f"s{i}", f"2026-01-0{i+1}T00:00:00+00:00",
                [{"symbol": "X", "verdict": "BUY", "confidence": "LOW", "rationale": "",
                  "key_risks": "", "entry_zone": "", "stop_loss": "", "target": "",
                  "telegram_msg_id": 0, "telegram_channel": "", "fundamentals_json": "{}",
                  "regime_at_scan": ""}],
            )
        deleted = j.prune_investment_scans(keep=1)
        assert deleted == 2
        assert len(j.get_investment_scans(10)) == 1

    def test_fundamentals_cache(self, tmp_path):
        from datetime import datetime, timezone
        j = Journal(str(tmp_path / "inv.sqlite"))
        now_iso = datetime.now(timezone.utc).isoformat()
        j.cache_fundamentals("RELIANCE", {"roce_pct": 22}, now_iso)
        cached = j.get_cached_fundamentals("RELIANCE", max_age_hours=24)
        assert cached == {"roce_pct": 22}
