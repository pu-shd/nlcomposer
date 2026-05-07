"""Tests for the AppSettings model + /api/settings endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app
from app.settings import AppSettings, load_settings, save_settings


@pytest.fixture
def client() -> TestClient:
    # Use the lifespan ctx manager so the DB is initialized on startup.
    with TestClient(app) as c:
        yield c


# ───────── Model defaults ────────────────────────────────────────────


def test_settings_model_has_sane_defaults():
    s = AppSettings()
    assert s.from_name
    assert "@" in s.from_email
    assert "@" in s.to_email
    # Behavior toggles default to the values used by the UI.
    assert s.preview_debounce_ms == 400
    assert s.auto_archive_on_download is True
    # Initial publication date is opt-in; without it the composer can't
    # derive the printed "Nth year" and falls back to a placeholder.
    assert s.initial_publication_date is None


def test_settings_model_accepts_initial_publication_date():
    from datetime import date

    s = AppSettings(initial_publication_date=date(2012, 9, 17))
    assert s.initial_publication_date == date(2012, 9, 17)
    # JSON round-trip preserves it as an ISO string the JS can parse.
    data = s.model_dump(mode="json")
    assert data["initial_publication_date"] == "2012-09-17"


def test_settings_validates_safelink_in_unsubscribe_url():
    """SanitizedHttpUrl on the model field must unwrap Microsoft Safelinks."""
    wrapped = (
        "https://nam12.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fexample.com%2Funsubscribe&data=foo"
    )
    s = AppSettings(unsubscribe_url=wrapped)
    assert str(s.unsubscribe_url).rstrip("/") == "https://example.com/unsubscribe"


def test_settings_rejects_out_of_range_debounce():
    with pytest.raises(ValueError):
        AppSettings(preview_debounce_ms=-1)
    with pytest.raises(ValueError):
        AppSettings(preview_debounce_ms=10_000)


# ───────── DB round-trip ─────────────────────────────────────────────


def test_load_returns_defaults_when_no_row():
    conn = connect()
    init_db(conn)
    try:
        s = load_settings(conn)
    finally:
        conn.close()
    assert isinstance(s, AppSettings)
    assert s.from_name == AppSettings().from_name


def test_save_then_load_round_trip():
    conn = connect()
    init_db(conn)
    try:
        save_settings(
            conn,
            AppSettings(
                from_name="Test User",
                from_email="t@example.com",
                to_email="dest@example.com",
                preview_debounce_ms=750,
                auto_archive_on_download=False,
            ),
        )
        conn.commit()
        loaded = load_settings(conn)
    finally:
        conn.close()
    assert loaded.from_name == "Test User"
    assert loaded.from_email == "t@example.com"
    assert loaded.preview_debounce_ms == 750
    assert loaded.auto_archive_on_download is False


def test_save_is_upsert_not_append():
    """A second save must overwrite the first row, never insert id=2."""
    conn = connect()
    init_db(conn)
    try:
        save_settings(conn, AppSettings(from_name="First"))
        save_settings(conn, AppSettings(from_name="Second"))
        conn.commit()
        rows = conn.execute("SELECT id FROM settings").fetchall()
        loaded = load_settings(conn)
    finally:
        conn.close()
    assert [r["id"] for r in rows] == [1]
    assert loaded.from_name == "Second"


# ───────── HTTP endpoints ────────────────────────────────────────────


def test_get_settings_returns_defaults_when_unset(client: TestClient):
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["from_name"] == AppSettings().from_name
    assert body["preview_debounce_ms"] == 400
    assert body["auto_archive_on_download"] is True


def test_put_settings_persists_and_round_trips(client: TestClient):
    payload = {
        "from_name": "Alain K.",
        "from_email": "alain@example.com",
        "to_email": "list@example.com",
        "reply_to": None,
        "unsubscribe_url": "https://example.com/unsub",
        "masthead_logo_url": None,
        "default_intro_html": "<p>Greetings.</p>",
        "default_footer_html": "<p>Cheers.</p>",
        "preview_debounce_ms": 250,
        "auto_archive_on_download": False,
    }
    res = client.put("/api/settings", json=payload)
    assert res.status_code == 200, res.text
    saved = res.json()
    assert saved["from_name"] == "Alain K."
    assert saved["preview_debounce_ms"] == 250
    assert saved["auto_archive_on_download"] is False

    again = client.get("/api/settings")
    assert again.status_code == 200
    fetched = again.json()
    assert fetched["from_name"] == "Alain K."
    assert fetched["default_intro_html"] == "<p>Greetings.</p>"
    assert str(fetched["unsubscribe_url"]).startswith("https://example.com/unsub")


def test_put_settings_rejects_invalid_payload(client: TestClient):
    res = client.put("/api/settings", json={"preview_debounce_ms": -5})
    assert res.status_code == 422


def test_put_settings_unwraps_safelink_in_url_field(client: TestClient):
    wrapped = (
        "https://nam12.safelinks.protection.outlook.com/?url="
        "https%3A%2F%2Fexample.com%2Fnewsletter&data=x"
    )
    res = client.put(
        "/api/settings",
        json={"unsubscribe_url": wrapped},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "safelinks" not in str(body["unsubscribe_url"])
    assert str(body["unsubscribe_url"]).rstrip("/") == "https://example.com/newsletter"


# ───────── UI wiring (smoke checks) ──────────────────────────────────


def test_index_includes_settings_button_and_dialog(client: TestClient):
    body = client.get("/").text
    assert 'id="open-settings"' in body
    assert 'id="settings-dialog"' in body
    # The dialog form must expose every editable field by name so the
    # JS can read+write them with form.elements[name].
    for name in (
        "from_name", "from_email", "to_email", "reply_to",
        "unsubscribe_url", "masthead_logo_url",
        "initial_publication_date",
        "default_intro_html", "default_footer_html",
        "preview_debounce_ms", "auto_archive_on_download",
    ):
        assert f'name="{name}"' in body, f"settings dialog missing field {name}"


def test_settings_endpoint_round_trips_initial_publication_date(client: TestClient):
    res = client.put(
        "/api/settings",
        json={"initial_publication_date": "2012-09-17"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["initial_publication_date"] == "2012-09-17"
    again = client.get("/api/settings").json()
    assert again["initial_publication_date"] == "2012-09-17"


def test_composer_js_calls_settings_endpoint(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/settings" in js
    # Both halves of the contract must be wired:
    assert "loadSettings" in js
    assert "saveSettings" in js
