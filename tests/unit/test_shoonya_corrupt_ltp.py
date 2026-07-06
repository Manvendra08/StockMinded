from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from data import shoonya_fetcher


@pytest.mark.unit
def test_fetch_option_chain_rejects_corrupt_ltp(monkeypatch) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"

    # Mock login and basic resolution calls
    monkeypatch.setattr(fetcher, "login", lambda force=False: True)
    monkeypatch.setattr(
        fetcher,
        "_search_scrip",
        lambda exchange, searchtext: {
            "stat": "Ok",
            "values": [{"tsym": "NIFTY26JUL24FUT", "token": "1111", "instname": "FUTIDX"}],
        },
    )

    # Underlying quotes mock: underlying price = 24116.80
    quotes_database = {
        "1111": {"stat": "Ok", "lp": "24116.80"},
        "PE_TOKEN": {"stat": "Ok", "lp": "24027.80", "strprc": "25600.00"},  # Corrupt Put LTP (spot leaked)
        "CE_TOKEN": {"stat": "Ok", "lp": "5.00", "strprc": "25600.00"},      # Valid Call LTP (OTM)
        "VALID_PE_TOKEN": {"stat": "Ok", "lp": "1500.00", "strprc": "25600.00"} # Valid Put LTP (ITM)
    }

    monkeypatch.setattr(
        fetcher,
        "_get_quotes",
        lambda exchange, token: quotes_database.get(token, {"stat": "Not_Ok"}),
    )

    # Option chain response: returns two option contracts
    monkeypatch.setattr(
        fetcher,
        "_get_option_chain",
        lambda exchange, tsym, strike, count=30: {
            "stat": "Ok",
            "values": [
                {
                    "tsym": "NIFTY26JUL2425600PE",
                    "token": "PE_TOKEN",
                    "optt": "PE",
                    "strprc": "25600.00",
                    "expiry": "26-JUL-2024",
                },
                {
                    "tsym": "NIFTY26JUL2425600CE",
                    "token": "CE_TOKEN",
                    "optt": "CE",
                    "strprc": "25600.00",
                    "expiry": "26-JUL-2024",
                },
            ],
        },
    )

    result = fetcher.fetch_option_chain("NIFTY")
    assert result is not None
    assert result["underlying_price"] == 24116.80

    # The PE_TOKEN ltp is 24027.80, which exceeds max_allowed (1483.2 + 3617.52 = 5100.72) and is corrupt.
    # It must be rejected and set to 0.0.
    # The CE_TOKEN ltp is 5.00, which is valid and must be retained.
    strikes = {s["option_type"]: s["ltp"] for s in result["strikes"]}
    assert strikes["PE"] == 0.0
    assert strikes["CE"] == 5.00


@pytest.mark.unit
def test_fetch_option_chain_accepts_valid_itm_put(monkeypatch) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"

    monkeypatch.setattr(fetcher, "login", lambda force=False: True)
    monkeypatch.setattr(
        fetcher,
        "_search_scrip",
        lambda exchange, searchtext: {
            "stat": "Ok",
            "values": [{"tsym": "NIFTY26JUL24FUT", "token": "1111", "instname": "FUTIDX"}],
        },
    )

    # Underlying price is 24116.80
    # A Put option (PE) at 25600 has intrinsic value = 1483.20.
    # LTP is 1500.00, which is a perfectly valid ITM price (intrinsic + 16.80 time value).
    # Since 1500.00 is <= 1483.20 + 3617.52 (5100.72), it must be accepted.
    quotes_database = {
        "1111": {"stat": "Ok", "lp": "24116.80"},
        "PE_TOKEN": {"stat": "Ok", "lp": "1500.00", "strprc": "25600.00"}
    }

    monkeypatch.setattr(
        fetcher,
        "_get_quotes",
        lambda exchange, token: quotes_database.get(token, {"stat": "Not_Ok"}),
    )

    monkeypatch.setattr(
        fetcher,
        "_get_option_chain",
        lambda exchange, tsym, strike, count=30: {
            "stat": "Ok",
            "values": [
                {
                    "tsym": "NIFTY26JUL2425600PE",
                    "token": "PE_TOKEN",
                    "optt": "PE",
                    "strprc": "25600.00",
                    "expiry": "26-JUL-2024",
                }
            ],
        },
    )

    result = fetcher.fetch_option_chain("NIFTY")
    assert result is not None
    assert result["strikes"][0]["ltp"] == 1500.00
