"""UI smoke tests for the Media Library dialog wiring.

These mirror the drafts/archive UI smoke tests — they verify that the
composer page exposes the menu trigger, dialog markup, and JS hooks
that allow the SPA to open the dialog, list items, register a URL,
edit alt-text, and delete entries through the existing /api/media
controller.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_index_includes_media_button_and_dialog(client: TestClient):
    body = client.get("/").text
    assert 'id="open-media"' in body
    assert 'id="media-dialog"' in body
    assert 'id="media-list"' in body
    assert 'id="media-create-form"' in body
    assert 'id="media-status"' in body


def test_media_button_lives_inside_main_menu(client: TestClient):
    body = client.get("/").text
    menu_start = body.index('id="main-menu"')
    menu_end = body.index("</div>", menu_start)
    menu_block = body[menu_start:menu_end]
    assert 'id="open-media"' in menu_block
    assert "Media Library" in menu_block


def test_media_create_form_offers_kind_and_url(client: TestClient):
    body = client.get("/").text
    form_start = body.index('id="media-create-form"')
    # Pick up enough of the form for the assertions below.
    form_end = body.index("</form>", form_start)
    form_block = body[form_start:form_end]
    assert 'name="kind"' in form_block
    assert 'value="logo"' in form_block
    assert 'value="image"' in form_block
    assert 'name="url"' in form_block
    assert 'type="url"' in form_block


def test_media_dialog_describes_image_description_label(client: TestClient):
    body = client.get("/").text
    # The dialog hint references the required Image description so the
    # user understands the alt-text gate before adding items.
    dialog_start = body.index('id="media-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    assert "Image description" in dialog_block


def test_composer_js_wires_media_endpoints(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/media" in js
    assert "openMediaDialog" in js
    assert "refreshMediaList" in js
    assert "saveMediaAlt" in js
    assert "deleteMediaItem" in js


def test_composer_js_renders_alt_text_editor_for_each_row(client: TestClient):
    js = client.get("/static/composer.js").text
    # The list renderer must surface an alt-text input + Save button per
    # row so the user can mark a pending item usable.
    assert 'data-action="save-alt"' in js
    assert 'data-field="alt_text"' in js
    # Image description label is required wording.
    assert "Image description" in js


def test_composer_js_renders_status_badge_and_placeholder(client: TestClient):
    js = client.get("/static/composer.js").text
    # Status badges + placeholder thumbnail classes must appear so
    # pending/failed items render distinctly without bytes cached.
    assert "media-badge" in js
    assert "media-thumb-pending" in js
    assert "media-thumb-failed" in js


def test_create_then_list_then_alt_then_delete_through_ui_endpoints(
    client: TestClient,
):
    """End-to-end exercise of the endpoints the dialog talks to.

    Mirrors what the dialog does: POST a URL → see it in GET list with a
    pending status → PUT the alt text → DELETE. The controller already
    has direct unit coverage; this check pins the flow the JS depends on.
    """
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/logo.png"},
    )
    assert res.status_code == 201, res.text
    item = res.json()
    assert item["status"] == "pending"
    assert item["alt_text"] == ""

    res = client.get("/api/media")
    assert res.status_code == 200
    listed = res.json()
    assert any(m["id"] == item["id"] for m in listed)

    res = client.put(
        f"/api/media/{item['id']}",
        json={"alt_text": "Sponsor logo"},
    )
    assert res.status_code == 200
    assert res.json()["alt_text"] == "Sponsor logo"

    res = client.delete(f"/api/media/{item['id']}")
    assert res.status_code == 204
