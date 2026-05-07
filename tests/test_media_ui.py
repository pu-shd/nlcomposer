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


def test_media_dialog_shares_chrome_with_drafts_archive(client: TestClient):
    """The Media dialog must wear the same shell as Drafts/Archive —
    .dialog-body wrapper, .dialog-header h2, .templates-list, and
    .dialog-actions footer — so the four dialogs feel like a set
    rather than three Templates/Drafts/Archive + a one-off Media."""
    body = client.get("/").text
    dialog_start = body.index('id="media-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    for hook in (
        'class="dialog-body"',
        'class="dialog-header"',
        'class="templates-list',     # may include extra classes
        'class="dialog-actions"',
    ):
        assert hook in dialog_block, f"media dialog missing shared {hook!r}"

    # And the shared CSS group selectors must reference #media-dialog
    # alongside the other three (so a future global tweak touches all
    # four at once).
    css = client.get("/static/composer.css").text
    for group in (
        "#templates-dialog,\n#drafts-dialog,\n#archive-dialog,\n#media-dialog",
        "#templates-dialog .dialog-body,\n#drafts-dialog .dialog-body,\n#archive-dialog .dialog-body,\n#media-dialog .dialog-body",
        "#templates-dialog .templates-list,\n#drafts-dialog .templates-list,\n#archive-dialog .templates-list,\n#media-dialog .templates-list",
    ):
        assert group in css, f"composer.css missing unified group selector:\n{group}"


def test_media_dialog_offers_upload_tab_and_form(client: TestClient):
    """Upload is the second supported flow alongside Add-by-URL — the
    dialog needs a tab strip, a hidden Upload form pre-targeting
    /api/media/upload (multipart), and the same Kind/file inputs."""
    body = client.get("/").text
    dialog_start = body.index('id="media-dialog"')
    dialog_end = body.index("</dialog>", dialog_start)
    dialog_block = body[dialog_start:dialog_end]
    assert 'class="media-create-tabs"' in dialog_block
    assert 'data-media-tab="url"' in dialog_block
    assert 'data-media-tab="upload"' in dialog_block
    assert 'id="media-upload-form"' in dialog_block
    # The upload form's file input must accept image/png and image/jpeg.
    assert 'accept="image/png,image/jpeg"' in dialog_block
    assert 'name="kind"' in dialog_block
    assert 'name="file"' in dialog_block


def test_composer_js_wires_media_endpoints(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/media" in js
    assert "openMediaDialog" in js
    assert "refreshMediaList" in js
    assert "saveMediaAlt" in js
    assert "deleteMediaItem" in js


def test_article_template_includes_logo_affordances(client: TestClient):
    """Each article card carries Fetch / Upload / Library buttons next
    to the Logo URL field, plus an inline status line and a hidden
    file input for the upload flow."""
    body = client.get("/").text
    tpl_start = body.index('id="article-template"')
    tpl_end = body.index("</template>", tpl_start)
    tpl = body[tpl_start:tpl_end]
    for hook in (
        'data-field="publication_logo_url"',
        'data-action="register-logo"',
        'data-action="upload-logo"',
        'data-action="pick-logo"',
        'data-role="logo-upload"',
        'data-role="logo-status"',
        'accept="image/png,image/jpeg"',
    ):
        assert hook in tpl, f"article logo affordance missing: {hook}"


def test_composer_js_wires_logo_actions_and_picker(client: TestClient):
    js = client.get("/static/composer.js").text
    # Each article card binds the three logo actions in addArticle.
    assert "wireArticleLogoActions" in js
    # Fetch action POSTs to /api/media (URL register, kind=logo).
    assert '"kind": "logo"' in js or "'kind': 'logo'" in js or 'kind: "logo"' in js or "kind: 'logo'" in js
    # Upload action POSTs multipart to /api/media/upload.
    assert "/api/media/upload" in js
    # Library action opens the picker filtered to kind=logo.
    assert "openLibraryPicker" in js


def test_media_picker_dialog_present(client: TestClient):
    body = client.get("/").text
    assert 'id="media-picker-dialog"' in body
    assert 'id="media-picker-list"' in body
    assert 'id="media-picker-close"' in body


def test_static_template_carries_richtext_toolbar(client: TestClient):
    """Static block now wraps its <textarea> with a toolbar offering
    bold / italic / heading / link / image. The Image button reuses
    the library picker (kind=image)."""
    body = client.get("/").text
    tpl_start = body.index('id="static-template"')
    tpl_end = body.index("</template>", tpl_start)
    tpl = body[tpl_start:tpl_end]
    assert 'class="static-toolbar"' in tpl
    for action in ("bold", "italic", "heading", "link", "image"):
        assert f'data-rt-action="{action}"' in tpl, f"static toolbar missing {action}"
    # The textarea is still the source of truth.
    assert 'data-field="html"' in tpl


def test_composer_js_wires_static_richtext_toolbar(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "wireStaticRichtext" in js
    # Image action calls openLibraryPicker filtered to kind=image.
    assert "kind: 'image'" in js or 'kind: "image"' in js
    # Toolbar emits a synthetic input event so the existing schedulePreview
    # listener picks up the change without us coupling to it directly.
    assert "new Event('input'" in js or 'new Event("input"' in js


def test_composer_js_wires_upload_form_and_tab_switch(client: TestClient):
    """Upload submission goes through /api/media/upload as multipart
    (no Content-Type: application/json), and the tab handler toggles
    each form's hidden state. Rate-limit 429s surface a Retry-After."""
    js = client.get("/static/composer.js").text
    assert "/api/media/upload" in js
    assert "selectMediaTab" in js
    assert "mediaUploadForm" in js
    # Soft check — tab buttons drive form visibility.
    assert "media-tab" in js
    # Rate-limit handling threads Retry-After into the user-visible status.
    assert "Retry-After" in js


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
