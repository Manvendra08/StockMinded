from __future__ import annotations

from typing import Any

from data import shoonya_fetcher


def test_is_session_expired_response_detects_invalid_session_key() -> None:
    response = {
        "stat": "Not_Ok",
        "emsg": "Session Expired :  Invalid Session Key",
    }

    assert shoonya_fetcher._is_session_expired_response(response) is True


def test_api_call_refreshes_expired_token_once(monkeypatch) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"
    fetcher.access_token = "expired-token"
    monkeypatch.setattr(fetcher, "_load_cached_token", lambda: None)

    calls: list[tuple[str, dict[str, Any], str | None]] = []
    responses: list[dict[str, Any]] = [
        {"stat": "Not_Ok", "emsg": "Session Expired :  Invalid Session Key"},
        {"stat": "Ok", "values": [{"token": "123"}]},
    ]

    def fake_post_jdata(
        url: str, payload: dict[str, Any], access_token: str | None = None
    ) -> dict[str, Any]:
        calls.append((url, dict(payload), access_token))
        return responses.pop(0)

    def fake_login(force: bool = False) -> bool:
        assert force is True
        fetcher.access_token = "fresh-token"
        return True

    monkeypatch.setattr(shoonya_fetcher, "_post_jdata", fake_post_jdata)
    monkeypatch.setattr(fetcher, "login", fake_login)

    result = fetcher._api_call("SearchScrip", {"exch": "NFO", "stext": "NIFTY"})

    assert result == {"stat": "Ok", "values": [{"token": "123"}]}
    assert len(calls) == 2
    assert calls[0][2] == "expired-token"
    assert calls[1][2] == "fresh-token"
    assert calls[1][1]["uid"] == "USER1"


def test_api_call_clears_token_and_sets_cooldown_when_refresh_fails(monkeypatch) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"
    fetcher.access_token = "expired-token"

    monkeypatch.setattr(shoonya_fetcher, "_SHOONYA_LOGIN_FAILURE_TS", 0.0)
    monkeypatch.setattr(fetcher, "_clear_cached_token", lambda: setattr(fetcher, "access_token", None))
    monkeypatch.setattr(fetcher, "login", lambda force=False: False)
    monkeypatch.setattr(
        shoonya_fetcher,
        "_post_jdata",
        lambda url, payload, access_token=None: {
            "stat": "Not_Ok",
            "emsg": "Session Expired :  Invalid Session Key",
        },
    )

    result = fetcher._api_call("SearchScrip", {"exch": "NFO", "stext": "NIFTY"})

    assert result == {
        "stat": "Not_Ok",
        "emsg": "Session Expired :  Invalid Session Key",
    }
    assert fetcher.access_token is None
    assert shoonya_fetcher._SHOONYA_LOGIN_FAILURE_TS > 0


def test_fetch_quote_returns_none_when_ltp_missing(monkeypatch) -> None:
    fetcher = shoonya_fetcher.ShoonyaFetcher()
    fetcher.user_id = "USER1"

    monkeypatch.setattr(fetcher, "login", lambda force=False: True)
    monkeypatch.setattr(
        fetcher,
        "_search_scrip",
        lambda exchange, searchtext: {
            "stat": "Ok",
            "values": [{"tsym": "RELIANCE-EQ", "token": "2885"}],
        },
    )
    monkeypatch.setattr(
        fetcher,
        "_get_quotes",
        lambda exchange, token: {"stat": "Ok", "c": "2800.00"},
    )

    assert fetcher.fetch_quote("RELIANCE") is None


def test_validate_optional_float_handles_none() -> None:
    from scratch.validate_shoonya import _fmt_optional_float

    assert _fmt_optional_float(None) == "N/A"
    assert _fmt_optional_float("") == "N/A"
    assert _fmt_optional_float("123.456") == "123.46"