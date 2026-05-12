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
    from datetime import date

    s = AppSettings()
    # Behavior toggles default to the values used by the UI.
    assert s.preview_debounce_ms == 400
    assert s.auto_archive_on_download is True
    # Read-more link is a render-toggle that defaults on so existing
    # newsletters keep their familiar layout; users can opt out.
    assert s.include_read_more_link is True
    # Confirm-before-delete defaults on so users don't accidentally drop
    # work; the confirm-delete dialog itself offers an opt-out.
    assert s.confirm_before_delete is True
    # Embed-all-media defaults on so a fresh install renders Article
    # Logos and Static-block images from cached bytes; the renderer
    # falls back to the source URL when bytes are missing.
    assert s.embed_all_media is True
    # Media-upload gating defaults — kept in sync with CLAUDE.md so a
    # change to the published contract surfaces as a test failure.
    assert s.media_upload_max_bytes == 5 * 1024 * 1024
    assert s.media_upload_allowed_types == ["image/png", "image/jpeg"]
    assert s.media_upload_rate_per_minute == 20
    assert s.media_url_imports_enabled is True
    # Newsletter brand and email envelope ship empty so a fresh install
    # doesn't print a placeholder identity — the user fills them in
    # Settings before the first preview matters.
    assert s.newsletter_name == ""
    assert s.from_name == ""
    assert s.from_email == ""
    assert s.to_email == ""
    # Inception Date defaults to the historical SmartDrivingCars launch
    # so a brand-new install renders the right "Nth year" without first
    # making the user open Settings.
    assert s.initial_publication_date == date(2013, 1, 1)


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


def test_save_then_load_round_trips_masthead_logo_media_id():
    """AppSettings.masthead_logo_media_id persists through SQLite.

    Covers the new "uploaded masthead logo" path — settings save the
    Media Library id, and load_settings returns it intact (as int, not
    string) so buildIssue() and the eml controller can dereference it.
    """
    conn = connect()
    init_db(conn)
    try:
        save_settings(
            conn,
            AppSettings(masthead_logo_media_id=42),
        )
        conn.commit()
        loaded = load_settings(conn)
    finally:
        conn.close()
    assert loaded.masthead_logo_media_id == 42
    # Default is None when not set
    assert AppSettings().masthead_logo_media_id is None


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


def test_put_settings_self_heals_when_settings_table_is_missing(client: TestClient):
    """If something has nuked the ``settings`` table (most commonly a
    half-finished ``deploy.sh wipe-data`` where the new container hasn't
    re-run init_db yet), the next PUT should re-bootstrap the schema and
    succeed — instead of returning a bare 500 the user can't act on.
    """
    # Simulate the missing-schema state by dropping the settings table
    # via a direct connection. The test client opens its own per-request
    # connection, so we have to mirror it.
    from app.db import connect
    raw = connect()
    try:
        raw.execute("DROP TABLE IF EXISTS settings")
        raw.commit()
    finally:
        raw.close()

    res = client.put(
        "/api/settings",
        json={"from_name": "After Self-Heal", "preview_debounce_ms": 333},
    )
    assert res.status_code == 200, res.text
    assert res.json()["from_name"] == "After Self-Heal"

    again = client.get("/api/settings")
    assert again.status_code == 200
    assert again.json()["from_name"] == "After Self-Heal"


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
        "newsletter_name",
        "from_name", "from_email", "to_email", "reply_to",
        "unsubscribe_url", "masthead_logo_url",
        "initial_publication_date",
        "default_intro_html", "default_footer_html",
        "preview_debounce_ms", "auto_archive_on_download",
        "include_read_more_link", "confirm_before_delete",
        "embed_all_media",
    ):
        assert f'name="{name}"' in body, f"settings dialog missing field {name}"


def test_settings_dialog_drops_cancel_and_starts_save_disabled(client: TestClient):
    """Cancel is gone; Save renders disabled and is gated on dirty
    state by the JS. Discard and a dirty-warning paragraph live in
    the dialog markup but start hidden."""
    body = client.get("/").text
    dialog_start = body.index('id="settings-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    # Cancel button is removed from the markup entirely.
    assert 'id="settings-cancel"' not in dialog_block
    # Save renders with the disabled attribute (turned on dynamically).
    assert 'id="settings-save"' in dialog_block
    assert 'disabled>' in dialog_block.split('id="settings-save"', 1)[1].split('>', 1)[0] + '>' \
        or 'disabled' in dialog_block.split('id="settings-save"', 1)[1].split('</button>', 1)[0]
    # Discard button is present-but-hidden until dirty.
    assert 'id="settings-discard"' in dialog_block
    # Dirty warning paragraph is present-but-hidden by default.
    assert 'id="settings-dirty-warning"' in dialog_block
    # The old "Defaults applied to every new draft. Edit once…" hint is gone.
    assert "Defaults applied to every new draft" not in dialog_block


def test_settings_dialog_renames_from_and_to_labels(client: TestClient):
    """The visible labels read just "From" and "To" — the long
    "From email" / "To email" wording is gone — and From-name is
    hidden (still in DOM so settings round-trip preserves any
    legacy value)."""
    body = client.get("/").text
    dialog_start = body.index('id="settings-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    assert 'class="lbl">From<' in dialog_block
    assert 'class="lbl">To<' in dialog_block
    assert "From email" not in dialog_block
    assert "To email" not in dialog_block
    # From-name is hidden — its <label> wrapper carries the hidden attr.
    assert 'name="from_name"' in dialog_block
    fn_start = dialog_block.rindex("<label", 0, dialog_block.index('name="from_name"'))
    fn_label_open = dialog_block[fn_start : dialog_block.index(">", fn_start) + 1]
    assert "hidden" in fn_label_open


def test_inception_date_immediately_follows_newsletter_name(client: TestClient):
    body = client.get("/").text
    dialog_start = body.index('id="settings-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    nn_idx = dialog_block.index('name="newsletter_name"')
    ipd_idx = dialog_block.index('name="initial_publication_date"')
    fn_idx = dialog_block.index('name="from_name"')
    # newsletter_name → initial_publication_date → from_name (hidden).
    assert nn_idx < ipd_idx < fn_idx, (
        "Inception Date must sit between Newsletter name and the "
        "(hidden) From name in the Settings DOM order."
    )


def test_composer_js_tracks_settings_dirty_state(client: TestClient):
    js = client.get("/static/composer.js").text
    # Save is enabled only when refreshSettingsDirty returns true.
    assert "refreshSettingsDirty" in js
    # Discard rolls back the form to currentSettings.
    assert "discardSettingsChanges" in js
    # Close-with-unsaved guard surfaces an inline warning.
    assert "tryCloseSettingsDialog" in js
    # Esc on the native <dialog> is intercepted via the cancel event.
    assert "settingsDialog.addEventListener('cancel'" in js \
        or 'settingsDialog.addEventListener("cancel"' in js


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


def test_embed_all_media_round_trips_through_http(client: TestClient):
    """The renderer reads ``embed_all_media`` to decide between cid:
    attachments and bare source URLs; the toggle has to survive a PUT.
    """
    res = client.put("/api/settings", json={"embed_all_media": False})
    assert res.status_code == 200, res.text
    assert res.json()["embed_all_media"] is False

    again = client.get("/api/settings").json()
    assert again["embed_all_media"] is False


def test_embed_all_media_persists_in_db():
    conn = connect()
    init_db(conn)
    try:
        save_settings(conn, AppSettings(embed_all_media=False))
        conn.commit()
        loaded = load_settings(conn)
    finally:
        conn.close()
    assert loaded.embed_all_media is False


def test_composer_js_includes_embed_all_media_field(client: TestClient):
    """SETTINGS_FIELDS must list embed_all_media so fillSettingsForm
    and collectSettings round-trip the new checkbox.
    """
    js = client.get("/static/composer.js").text
    assert "embed_all_media" in js
