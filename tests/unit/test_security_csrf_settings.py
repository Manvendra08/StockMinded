import pytest
import json
from dashboard.server import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


MUTATING_ROUTES = [
    "/api/refresh",
    "/api/send-alerts",
    "/api/test-telegram",
    "/api/paper/auto-enter",
    "/api/paper/check",
    "/api/paper/close/1",
    "/api/options/close/1",
    "/api/options/auto-enter",
    "/api/intelligence/refresh_weights",
    "/api/investment/scan/run",
]


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_mutating_routes_reject_get(client, route):
    """C3 Fix Verification: State-changing endpoints must return 405 on GET."""
    response = client.get(route)
    assert response.status_code == 405, f"Route {route} allowed GET! Expected 405 Method Not Allowed."


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_mutating_routes_require_csrf_header(client, route):
    """C3 Fix Verification: State-changing POST endpoints without CSRF header return 403."""
    response = client.post(route, json={})
    assert response.status_code == 403, f"Route {route} allowed POST without CSRF header! Expected 403 Forbidden."
    data = response.get_json()
    assert "CSRF verification failed" in data.get("error", "")


def test_settings_validation_unknown_key(client):
    """C4 Fix Verification: Settings update with unknown key returns 400 Bad Request."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    payload = {"unknown_malicious_key": "hacked"}
    response = client.post("/api/paper/settings", json=payload, headers=headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "Unknown settings key" in data.get("error", "")


def test_settings_validation_out_of_range_daily_stop(client):
    """C4 Fix Verification: Out of range daily stop pct (>0.20) returns 400 Bad Request."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    payload = {"rg_daily_stop_pct": 0.99}  # 99% daily stop loss is invalid
    response = client.post("/api/paper/settings", json=payload, headers=headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "rg_daily_stop_pct" in data.get("error", "")


def test_settings_validation_out_of_range_sl_pct(client):
    """C4 Fix Verification: Out of range sl_pct (>50.0) returns 400 Bad Request."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    payload = {"sl_pct": 100.0}
    response = client.post("/api/paper/settings", json=payload, headers=headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "sl_pct" in data.get("error", "")


def test_settings_validation_invalid_enum(client):
    """C4 Fix Verification: Invalid min_confidence enum value returns 400 Bad Request."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    payload = {"min_confidence": "EXTREME"}
    response = client.post("/api/paper/settings", json=payload, headers=headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "min_confidence" in data.get("error", "")


def test_settings_validation_valid_payload(client):
    """C4 Fix Verification: Valid settings payload succeeds with 200 OK."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    payload = {
        "sl_pct": 2.5,
        "tgt_pct": 5.0,
        "rg_daily_stop_pct": 0.03,
        "min_confidence": "HIGH"
    }
    response = client.post("/api/paper/settings", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data.get("sl_pct") == 2.5
    assert data.get("min_confidence") == "HIGH"
