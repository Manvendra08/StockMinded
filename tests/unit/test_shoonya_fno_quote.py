from __future__ import annotations

import pytest

from data import shoonya_fetcher


@pytest.mark.unit
def test_fetch_fno_quote_uses_close_as_prev_close_not_price_precision(
    monkeypatch,
) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"

    monkeypatch.setattr(fetcher, "login", lambda force=False: True)
    monkeypatch.setattr(
        fetcher,
        "_search_scrip",
        lambda exchange, searchtext: {
            "stat": "Ok",
            "values": [
                {
                    "instname": "FUTSTK",
                    "tsym": "ASHOKLEY25JUL26FUT",
                    "token": "12345",
                }
            ],
        },
    )
    monkeypatch.setattr(
        fetcher,
        "_get_quotes",
        lambda exchange, token: {
            "stat": "Ok",
            "lp": "164.85",
            "c": "161.91",
            "pp": "2",  # price precision (not previous close)
            "o": "162.00",
            "h": "165.10",
            "l": "161.20",
            "v": "12345",
            "oi": "67890",
        },
    )

    out = fetcher.fetch_fno_quote("ASHOKLEY")

    assert out is not None
    assert out["prev_close"] == 161.91
    assert out["change_pct"] == pytest.approx(1.82, abs=0.01)
