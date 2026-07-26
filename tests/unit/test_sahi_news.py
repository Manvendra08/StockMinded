"""Unit tests for the sahi.com investment news pipeline (no external calls).

Covers the fixes for:
  * 'Fetch & Classify Now does not cover all cards' — batched LLM extraction.
  * 'Bot is not fetching all news from the past 1 hour' — recency window +
    pagination in fetch_sahi_headlines.
  * 'verdict on the card is not persistent' — per-article classification stored
    in the journal.
"""
import json

import pytest

import data.sahi_news as sahi_news
from data.sahi_news import (
    SahiExtractedTicker,
    SahiHeadline,
    _age_text_to_minutes,
    extract_tickers_from_headlines,
    match_tickers_to_headlines,
)
from ops.journal import Journal


# ── Age parsing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("just now", 0),
        ("5 min ago", 5),
        ("12 minutes ago", 12),
        ("1 hour ago", 60),
        ("3 hours ago", 180),
        ("2 days ago", 2880),
        ("", None),
        ("unknown", None),
    ],
)
def test_age_text_to_minutes(text, expected):
    assert _age_text_to_minutes(text) == expected


# ── Recency window + pagination ──────────────────────────────────────────────
class _StubResponse:
    status_code = 200
    text = "<html></html>"
    encoding = "utf-8"


class _StubSession:
    def get(self, url, timeout=20):
        return _StubResponse()

    def close(self):
        pass


def _mk(url, age):
    return SahiHeadline(title=f"title {url}", summary="", article_url=url,
                        age_text="", age_minutes=age)


def test_fetch_window_filters_old_and_paginates(monkeypatch):
    # Page 1: mix of fresh + one old; Page 2: all old (should stop the walk).
    pages = [
        [_mk("u1", 5), _mk("u2", 20), _mk("u3", 40), _mk("u4", 70)],
        [_mk("u5", 80), _mk("u6", 90)],
        [_mk("u7", 5)],  # never reached because page 2 is all out-of-window
    ]
    calls = {"n": 0}

    def fake_parse(html):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else []

    monkeypatch.setattr(sahi_news, "_parse_listing_html", fake_parse)
    monkeypatch.setattr(sahi_news, "_fetch_article_content", lambda h: None)

    out = sahi_news.fetch_sahi_headlines(
        limit=60, session=_StubSession(), min_interval=0,
        window_minutes=60, max_pages=4,
    )
    assert [h.article_url for h in out] == ["u1", "u2", "u3"]


def test_fetch_no_window_keeps_top_limit(monkeypatch):
    pages = [
        [_mk("u1", 5), _mk("u2", 200)],
        [_mk("u3", 300)],
    ]
    calls = {"n": 0}

    def fake_parse(html):
        idx = calls["n"]
        calls["n"] += 1
        return pages[idx] if idx < len(pages) else []

    monkeypatch.setattr(sahi_news, "_parse_listing_html", fake_parse)
    monkeypatch.setattr(sahi_news, "_fetch_article_content", lambda h: None)

    out = sahi_news.fetch_sahi_headlines(
        limit=2, session=_StubSession(), min_interval=0,
        window_minutes=None, max_pages=4,
    )
    assert [h.article_url for h in out] == ["u1", "u2"]


def test_fetch_keeps_unknown_age(monkeypatch):
    pages = [[_mk("uX", None), _mk("uY", None)]]
    monkeypatch.setattr(sahi_news, "_parse_listing_html", lambda html: pages[0])
    monkeypatch.setattr(sahi_news, "_fetch_article_content", lambda h: None)
    out = sahi_news.fetch_sahi_headlines(
        limit=60, session=_StubSession(), min_interval=0,
        window_minutes=60, max_pages=2,
    )
    assert [h.article_url for h in out] == ["uX", "uY"]


# ── Batched extraction covers every headline ─────────────────────────────────
def test_extraction_covers_all_headlines():
    headlines = [
        SahiHeadline(title=f"Headline {i} SYM{i}", summary=f"sum {i}",
                     content="x" * 1500, article_url=f"url{i}")
        for i in range(1, 21)
    ]
    seen_batches = {"count": 0, "max_block_len": 0}

    def fake_llm(prompt, system_prompt=None, json_mode=True, max_tokens=2048):
        import re
        seen_batches["count"] += 1
        # The block between the markers is what the model actually sees.
        block = prompt.split("<<<HEADLINES>>>")[1].split("<<<END_HEADLINES>>>")[0]
        seen_batches["max_block_len"] = max(seen_batches["max_block_len"], len(block))
        idxs = [int(m) for m in re.findall(r"\[(\d+)\] ", prompt)]
        mentions = [
            {"headline_index": idx, "symbol": f"SYM{i}", "company_name": f"Headline {i}",
             "news_event": f"event {i}", "event_type": "order",
             "sentiment": "POSITIVE", "confidence": 0.9}
            for idx, i in enumerate(idxs, 1)
        ]
        return json.dumps({"mentions": mentions})

    out = extract_tickers_from_headlines(
        headlines, call_llm=fake_llm, universe=None,
        min_confidence=0.4, batch_size=6,
    )
    symbols = {t.symbol for t in out}
    # Every one of the 20 headlines must have been seen and classified.
    assert symbols == {f"SYM{i}" for i in range(1, 21)}
    # 20 headlines / batch_size 6 -> 4 batches.
    assert seen_batches["count"] == 4


def test_extraction_dedupes_by_symbol_highest_confidence():
    headlines = [SahiHeadline(title="RELIANCE A", article_url="u1"),
                 SahiHeadline(title="RELIANCE B", article_url="u2")]
    responses = iter([
        json.dumps({"mentions": [{"headline_index": 1, "symbol": "RELIANCE", "confidence": 0.6,
                                  "news_event": "e1", "sentiment": "POSITIVE"}]}),
        json.dumps({"mentions": [{"headline_index": 1, "symbol": "RELIANCE", "confidence": 0.95,
                                  "news_event": "e2", "sentiment": "POSITIVE"}]}),
    ])

    def fake_llm(*a, **k):
        return next(responses)

    out = extract_tickers_from_headlines(
        headlines, call_llm=fake_llm, batch_size=1,
    )
    assert len(out) == 1
    assert out[0].symbol == "RELIANCE"
    assert out[0].confidence == 0.95


# ── Headline → ticker mapping (persistent badges) ────────────────────────────
def test_match_tickers_to_headlines():
    headlines = [
        SahiHeadline(title="RELIANCE bags order", article_url="u1"),
        SahiHeadline(title="Quarterly results", summary="TCS reports", article_url="u2"),
        SahiHeadline(title="Generic market news", article_url="u3"),
    ]
    extracted = [
        SahiExtractedTicker("RELIANCE", 0.9, "order", company_name="Reliance Industries"),
        SahiExtractedTicker("TCS", 0.8, "earnings", company_name="Tata Consultancy"),
    ]
    mapping = match_tickers_to_headlines(headlines, extracted)
    assert mapping["u1"].symbol == "RELIANCE"
    assert mapping["u2"].symbol == "TCS"
    assert "u3" not in mapping


# ── Journal classification persistence ───────────────────────────────────────
def test_sahi_classifications_roundtrip(tmp_path):
    j = Journal(str(tmp_path / "inv.sqlite"))
    rows = [
        {"url": "u1", "title": "RELIANCE order", "symbol": "RELIANCE",
         "company_name": "Reliance", "news_event": "bags order",
         "event_type": "order", "sentiment": "POSITIVE", "verdict": "BUY",
         "confidence": 0.9},
    ]
    assert j.save_sahi_classifications(rows) == 1
    stored = j.get_sahi_classifications(["u1"])
    assert stored["u1"]["symbol"] == "RELIANCE"
    assert stored["u1"]["sentiment"] == "POSITIVE"

    # Upsert updates in place (keyed by URL).
    rows[0]["sentiment"] = "NEGATIVE"
    rows[0]["verdict"] = "SELL"
    j.save_sahi_classifications(rows)
    stored = j.get_sahi_classifications(["u1"])
    assert stored["u1"]["sentiment"] == "NEGATIVE"
    assert stored["u1"]["verdict"] == "SELL"
    j.close()


def test_latest_verdicts_by_symbol(tmp_path):
    j = Journal(str(tmp_path / "inv.sqlite"))
    j.save_investment_verdicts(
        "s1", "2026-01-01T00:00:00+00:00",
        [{"symbol": "RELIANCE", "verdict": "AVOID", "confidence": "LOW",
          "rationale": "old", "news_event": "old event", "company_name": "Reliance"}],
    )
    j.save_investment_verdicts(
        "s2", "2026-01-02T00:00:00+00:00",
        [{"symbol": "RELIANCE", "verdict": "BUY", "confidence": "HIGH",
          "rationale": "new", "news_event": "new event", "company_name": "Reliance"}],
    )
    latest = j.get_latest_verdicts_by_symbol()
    assert latest["RELIANCE"]["verdict"] == "BUY"
    assert latest["RELIANCE"]["news_event"] == "new event"
    j.close()
