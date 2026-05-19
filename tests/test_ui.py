"""Tests for the composer UI page and its served static assets.

These tests do not exercise the JS in a browser — that is done via the
manual smoke-test against a live uvicorn instance (see Makefile). They
do verify that the HTML page is served, references the expected static
assets, and that those assets are reachable through the FastAPI app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_index_serves_composer_page(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    # Page must contain the form skeleton with all top-level sections so
    # Alain can drive the whole pipeline through the UI without curl.
    assert "<title>Newsletter Composer</title>" in body
    for legend in (
        "Issue Details",
        "Articles",
        "Podcast",
        "Static blocks",
        "Footer",
    ):
        assert legend in body, f"composer page missing section: {legend}"
    # The old "From / To / Subject" section has been retired — its
    # fields are now app-wide-only (in Settings) and Subject moved into
    # the Issue Details section.
    assert "From / To / Subject" not in body
    # Inside the main composer <form>, the email envelope and masthead
    # logo must NOT be collected per-issue — they live only in the
    # Settings dialog (which is outside the composer form).
    form_start = body.index('<form id="composer"')
    form_end = body.index("</form>", form_start)
    form_body = body[form_start:form_end]
    for name in (
        "from_name", "from_email", "to_email", "reply_to",
        "masthead_logo_url",
    ):
        assert f'name="{name}"' not in form_body, (
            f"composer form should not collect {name} per-issue anymore"
        )
    # And it must reference the JS + CSS the UI depends on.
    assert "/static/composer.js" in body
    assert "/static/composer.css" in body
    # Repeating-section templates must be present so the JS can clone them.
    for tpl_id in (
        "article-template",
        "podcast-template",
        "timecode-template",
        "static-template",
        "highlight-template",
    ):
        assert f'id="{tpl_id}"' in body


def test_static_assets_are_served(client: TestClient):
    css = client.get("/static/composer.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert ".form-pane" in css.text

    js = client.get("/static/composer.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "buildIssue" in js.text
    assert "/api/preview/html" in js.text
    assert "/api/eml" in js.text
    # Article fetch automation must be wired through the UI:
    assert "/api/fetch/article" in js.text
    assert "fetchArticleInto" in js.text


def test_article_template_includes_fetch_button(client: TestClient):
    body = client.get("/").text
    # Each cloned article card carries a Fetch button so the user can
    # auto-populate headline/byline/excerpt/favicon from the URL.
    assert 'data-action="fetch-article"' in body
    assert 'data-role="fetch-status"' in body


def test_article_template_uses_collapsible_card_with_drag_handle(client: TestClient):
    """Article cards are <details> with a prominent header bar, a drag
    handle, and a live headline preview — so the user can collapse,
    reorder, and scan the article list at a glance.
    """
    body = client.get("/").text

    # The cloned card must be a collapsible <details> (open by default)
    # with the prominent header bar as its <summary>.
    article_tpl_start = body.index('id="article-template"')
    article_tpl_end = body.index("</template>", article_tpl_start)
    tpl = body[article_tpl_start:article_tpl_end]

    assert "<details" in tpl, "article card should be a <details> element"
    assert "article-card" in tpl, "article card needs the .article-card class"
    assert "<summary" in tpl, "article card needs a <summary> header bar"
    assert "card-header" in tpl, "summary should carry the .card-header class"
    assert "drag-handle" in tpl, "article card needs a drag handle for reordering"
    assert "card-headline-preview" in tpl, (
        "header bar needs a live headline preview slot"
    )
    # Remove button must still exist inside the new header bar.
    assert "btn-remove" in tpl


def test_issue_details_renames_and_hides_legacy_fields(client: TestClient):
    """The Issue Details section uses friendly labels and hides the
    fields we don't want the user to think about anymore. Display date
    and Subject are kept in the markup (so the JS can still surface
    them later) but rendered hidden so they don't clutter the form.
    """
    body = client.get("/").text
    # Friendlier labels.
    assert "Issue Number" in body
    assert "Issue Title" in body
    assert "Intro Message" in body
    # Old wording is gone.
    assert "Title slug" not in body
    assert "Intro HTML (optional)" not in body
    # Display date and Subject labels are present but hidden via the
    # ``hidden`` boolean attribute on the wrapping <label class="span-2">.
    assert 'hidden>Display date' in body
    assert 'hidden>Subject (auto-generated' in body


def test_required_fields_have_inline_error_slots(client: TestClient):
    """Issue Number and Issue Title each carry a hidden <small> slot
    that the JS toggles on when the value goes blank, so the user gets
    inline messaging instead of a silent form."""
    body = client.get("/").text
    assert 'data-error-for="issue_number"' in body
    assert 'data-error-for="issue_title"' in body


def test_composer_js_gates_download_on_required_fields(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "validateRequired" in js
    # The Download button is the gated entry point — must be referenced
    # by the validator wiring so it can flip disabled.
    assert "download-eml" in js
    assert "REQUIRED_FIELDS" in js


def test_composer_js_short_circuits_preview_for_missing_required_fields(client: TestClient):
    """When Issue Number / Issue Title are missing, the preview pane
    must render a friendly placeholder instead of forwarding to the
    server (which would respond 422 with a raw pydantic dump).
    """
    js = client.get("/static/composer.js").text
    assert "renderRequiredFieldsPlaceholder" in js
    # The placeholder must be rendered BEFORE the preview POST, so the
    # validation gate appears earlier in doPreview than the fetch call.
    placeholder_idx = js.index("renderRequiredFieldsPlaceholder()")
    fetch_idx = js.index("fetch('/api/preview/html'")
    assert placeholder_idx < fetch_idx, (
        "validation placeholder must short-circuit before the preview POST"
    )


def test_topbar_uses_hamburger_menu_for_drafts_and_archive(client: TestClient):
    """Drafts + Archive moved out of the main action cluster into a
    dropdown reachable from a hamburger icon to the left of the title.
    Templates is hidden but the trigger remains in DOM so its handler
    keeps wiring (no JS changes needed to re-enable later).
    """
    body = client.get("/").text
    # Hamburger trigger + dropdown markup.
    assert 'id="open-menu"' in body
    assert 'id="main-menu"' in body
    assert 'class="menu-trigger"' in body
    # Drafts and Archive triggers live INSIDE the dropdown menu, not
    # in the topbar's own .actions cluster.
    menu_start = body.index('id="main-menu"')
    menu_end = body.index('</div>', menu_start)
    menu_block = body[menu_start:menu_end]
    assert 'id="open-drafts"' in menu_block
    assert 'id="open-archive"' in menu_block
    # Templates is present but hidden (no longer surfaced in the UI).
    assert 'id="open-templates"' in body
    assert 'id="open-templates" class="btn ghost" aria-haspopup="dialog" hidden' in body


def test_topbar_renames_download_and_replaces_reset_with_compose_new(client: TestClient):
    body = client.get("/").text
    # "Download" → "Download & Archive": clicking the button writes
    # the issue to the archive table in the same request, so the new
    # label makes the side effect explicit at the click target.
    assert ">Download &amp; Archive<" in body, (
        "topbar Download button should read 'Download & Archive' so the "
        "side-effect of archiving is visible at click time"
    )
    assert "Download .eml" not in body
    # Reset is gone; a Compose-new button takes its place and opens
    # a custom dialog (so we don't lose work to a confirm()).
    assert 'id="reset-form"' not in body
    assert 'id="compose-new"' in body
    assert 'id="compose-new-dialog"' in body
    # Compose-new carries a visible "Compose New" text label, not just
    # an icon — paired with the file-plus SVG via .btn-with-icon.
    assert ">Compose New</span>" in body
    # Order in the topbar action cluster: Compose New BEFORE Download
    # (Download is the rightmost action, primary CTA).
    compose_idx = body.index('id="compose-new"')
    download_idx = body.index('id="download-eml"')
    assert compose_idx < download_idx, (
        "Compose New must precede Download in the topbar"
    )
    # The Compose-new dialog must offer the Save-as-Draft escape hatch.
    for el in (
        'id="compose-new-save"',
        'id="compose-new-discard"',
        'id="compose-new-cancel"',
        'name="draft_name"',
    ):
        assert el in body, f"compose-new dialog missing element: {el}"


def test_compose_new_js_saves_via_drafts_endpoint(client: TestClient):
    js = client.get("/static/composer.js").text
    # Save & start new must POST to /api/drafts (re-using the existing
    # drafts API), not invent a new endpoint.
    assert "openComposeNewDialog" in js
    assert "/api/drafts" in js
    # Discard branch reloads the page; nothing should silently nuke
    # the form without the dialog interaction.
    assert "startFresh" in js


def test_compose_new_dialog_uses_settings_chrome(client: TestClient):
    """The compose-new dialog shares the Settings dialog's chrome (rounded
    panel, header bar with X in the upper-right) so it doesn't read like
    a stray browser-default <dialog>."""
    css = client.get("/static/composer.css").text
    # The dialog selectors group settings + compose-new + confirm-delete
    # so a single rule sheet drives all three. Spot-check a few rules.
    assert "#compose-new-dialog" in css
    assert "#confirm-delete-dialog" in css
    # The X close button uses the dialog-close modifier so it hugs the
    # upper-right corner (no left margin, larger glyph).
    body = client.get("/").text
    assert 'class="btn-remove dialog-close" id="compose-new-close"' in body
    assert 'class="btn-remove dialog-close" id="confirm-delete-close"' in body


def test_confirm_delete_dialog_present(client: TestClient):
    """A custom confirm-delete dialog replaces window.confirm() with
    a Settings-aligned dialog that includes a 'don't ask again' toggle.
    """
    body = client.get("/").text
    assert 'id="confirm-delete-dialog"' in body
    assert 'id="confirm-delete-message"' in body
    assert 'id="confirm-delete-skip"' in body
    assert 'id="confirm-delete-confirm"' in body
    assert 'id="confirm-delete-cancel"' in body


def test_lists_use_trash_icon_for_delete(client: TestClient):
    """Templates / drafts / archive lists render delete actions as a
    trash-icon button rather than a plain Delete text button."""
    js = client.get("/static/composer.js").text
    # The shared trash SVG constant is reused by every list renderer.
    assert "TRASH_ICON_SVG" in js
    # No remaining text-only Delete buttons in the list renderers — the
    # icon class is what gets emitted now.
    assert 'btn-icon-danger' in js
    # And no callers still go through window.confirm().
    assert "confirm(`Delete" not in js
    assert "confirmDelete(" in js


def test_confirm_delete_skipped_when_setting_off(client: TestClient):
    """If AppSettings.confirm_before_delete is false, confirmDelete()
    must short-circuit to true so the call site deletes immediately
    without showing the dialog."""
    js = client.get("/static/composer.js").text
    # The branch reads currentSettings.confirm_before_delete and resolves
    # true without opening the dialog when it's been turned off.
    assert "confirm_before_delete" in js
    assert "Promise.resolve(true)" in js


def test_layout_starts_with_preview_pane_collapsed(client: TestClient):
    """The form pane should claim full width on first load — surfacing
    a usable preview only when the editor explicitly asks for it. The
    toggle button mirrors the collapsed state so screen readers see
    aria-pressed=true / "Show preview" up front."""
    body = client.get("/").text
    # The <main class="layout"> ships with preview-hidden engaged.
    assert 'class="layout preview-hidden"' in body
    # And the toggle reflects "currently hidden".
    toggle_idx = body.index('id="toggle-preview"')
    toggle_block = body[toggle_idx : body.index('>', toggle_idx) + 1]
    assert 'aria-pressed="true"' in toggle_block
    assert 'aria-label="Show preview"' in toggle_block


def test_layout_includes_resizer(client: TestClient):
    """A vertical splitter sits between the composer form and the
    preview pane so Alain can retune the column widths or yank the
    splitter all the way over to collapse the preview."""
    body = client.get("/").text
    assert 'class="layout-resizer"' in body
    assert 'role="separator"' in body
    js = client.get("/static/composer.js").text
    # Pointer-based drag wiring + the snap-collapse path must be present.
    assert "layout-resizer" in js
    assert "pointerdown" in js
    assert "preview-hidden" in js


def test_form_replaces_year_input_with_computed_display(client: TestClient):
    """The Year input has been replaced by a read-only computed display
    backed by AppSettings.initial_publication_date — Alain stops typing
    a year that's auto-derivable from the issue date.
    """
    body = client.get("/").text
    # Old required input must be gone (no name="issue_year" on a form input).
    assert 'name="issue_year"' not in body
    # The replacement: an <output> element with a stable id that the JS
    # writes the computed "Nth year" string into.
    assert 'id="issue-year-display"' in body


def test_composer_js_computes_issue_year_from_settings(client: TestClient):
    js = client.get("/static/composer.js").text
    # Year is computed client-side, mirroring app/models.compute_issue_year.
    assert "computeIssueYear" in js
    # The setting that drives the math must be wired into the dialog's
    # field list so it round-trips on save.
    assert "initial_publication_date" in js


def test_composer_js_handles_article_reordering(client: TestClient):
    """Drag-and-drop wiring + stable per-card ids must be present so
    reordering articles does not break highlight references. Highlight
    selects bind to a card's id (not its index) and only convert to a
    numeric ``article_index`` at buildIssue() time.
    """
    js = client.get("/static/composer.js").text

    # HTML5 drag-and-drop wiring for the article reorder UX.
    for hook in ("dragstart", "dragover", "dragend"):
        assert hook in js, f"composer.js missing drag-and-drop hook: {hook}"

    # Articles must carry stable ids so highlight refs survive reorder.
    assert "data-article-id" in js or "articleId" in js, (
        "composer.js should assign a stable id per article card"
    )
    # Highlights must look up by id, not just by numeric index.
    assert "idToOutIdx" in js or "articleId" in js, (
        "buildIssue should resolve highlights via the article id map"
    )


def test_preview_endpoint_renders_for_minimal_issue(client: TestClient):
    """The UI's first auto-render fires before the user types anything;
    the preview endpoint must still cope with a minimal valid issue."""
    payload = {
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "BeijingAutoShow",
        "issue_date": "2026-05-03",
    }
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    assert "9th edition of the 14th year" in res.text


def test_preview_endpoint_round_trips_full_issue(sample_issue, client: TestClient):
    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    body = res.text
    assert "Beijing Auto Show 2026" in body
    assert "Camille Kamga" in body
    assert "Highlights" in body


def test_preview_endpoint_round_trips_reordered_articles_with_highlights(
    sample_issue, client: TestClient,
):
    """Simulates what the client POSTs after the user drags an article
    into a new position: highlights still point to their original
    referenced article, but the index has been remapped on save.

    Protects against regression of the id↔index translation in
    composer.js's buildIssue().
    """
    payload = sample_issue.model_dump(mode="json")
    # Pre-condition: fixture has a highlight pointing at the first article
    # ("Beijing Auto Show…") and one at the second ("Why Are We Still…").
    assert payload["articles"][0]["headline"].startswith("Beijing Auto Show")
    assert payload["highlights"][0]["article_index"] == 0
    assert payload["highlights"][1]["article_index"] == 1

    # Reverse article order; remap highlights (the JS does this when
    # serializing — the model only ever sees post-remap indices).
    payload["articles"] = list(reversed(payload["articles"]))
    n = len(payload["articles"])
    for h in payload["highlights"]:
        h["article_index"] = (n - 1) - h["article_index"]

    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    body = res.text
    # Both articles still render and the override label still attaches
    # to the right article (the second highlight ref).
    assert "Beijing Auto Show 2026" in body
    assert "Why Are We Still Driving?" in body
    assert "NYT: Why Are We Still Driving?" in body


def test_eml_endpoint_returns_attachment_for_full_issue(sample_issue, client: TestClient):
    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("message/rfc822")
    disp = res.headers["content-disposition"]
    assert "attachment" in disp
    assert "BeijingAutoShow" in disp
    assert b"Subject:" in res.content
    assert b"multipart/alternative" in res.content


def test_eml_endpoint_inlines_masthead_logo_from_media_id(sample_issue, client: TestClient):
    """Uploading a masthead and pointing the issue at it embeds the bytes
    inline (cid:) in the resulting .eml — the recipient's mail client
    never tries to dereference /api/media/{id}/content.

    Regression net for the upload path that the Settings dialog wires up:
    fail-stop if anyone reverts the eml controller to ignore
    ``masthead_logo_media_id`` (in which case the .eml would carry the
    relative URL and silently break in every mailbox on the planet).
    """
    import io
    from email import message_from_bytes, policy

    # 1. Upload a fake PNG through the same /api/media/upload endpoint the
    #    Settings UI uses. PNG_BYTES is the same minimal signature-prefix
    #    payload the media_controller tests rely on.
    png_bytes = b"\x89PNG\r\n\x1a\nfake-but-non-empty-bytes"
    upload_res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("masthead.png", io.BytesIO(png_bytes), "image/png")},
    )
    assert upload_res.status_code == 201, upload_res.text
    media_id = upload_res.json()["id"]

    # 2. POST an issue whose masthead points at the upload id (and has NO
    #    URL fallback). The eml controller must resolve the id to inline
    #    bytes; the template must render cid: not /api/media/...
    payload = sample_issue.model_dump(mode="json")
    payload["masthead_logo_media_id"] = media_id
    payload.pop("masthead_logo_url", None)
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200, res.text

    msg = message_from_bytes(res.content, policy=policy.default)
    image_parts = [p for p in msg.walk() if p.get_content_type().startswith("image/")]
    assert len(image_parts) == 1
    cid = image_parts[0].get("Content-ID", "").strip("<>")
    assert cid

    # 3. HTML alternative references cid:, not the /api/media URL.
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert any(f"cid:{cid}" in p.get_content() for p in html_parts)
    assert all(f"/api/media/{media_id}/content" not in p.get_content() for p in html_parts)


def test_eml_falls_back_to_url_when_media_id_has_no_cached_bytes(
    sample_issue, client: TestClient
):
    """A URL-sourced Media Library item is *pending* until the background
    fetcher caches its bytes. The eml controller must NOT attach a 0-byte
    image — it should skip the cid path and let the template fall through
    to the URL fallback (which exists precisely for this case)."""
    # Register a URL-sourced item — status is "pending", no bytes cached.
    reg_res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/pending-logo.png"},
    )
    assert reg_res.status_code == 201, reg_res.text
    media_id = reg_res.json()["id"]
    assert reg_res.json()["status"] == "pending"

    payload = sample_issue.model_dump(mode="json")
    payload["masthead_logo_media_id"] = media_id
    payload["masthead_logo_url"] = "https://example.com/fallback-logo.png"
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200, res.text

    from email import message_from_bytes, policy
    msg = message_from_bytes(res.content, policy=policy.default)
    image_parts = [p for p in msg.walk() if p.get_content_type().startswith("image/")]
    assert image_parts == [], "no inline image — bytes were not cached"
    # The URL fallback wins; template renders the regular <img src=URL>.
    html = next(
        p for p in msg.walk() if p.get_content_type() == "text/html"
    ).get_content()
    assert "https://example.com/fallback-logo.png" in html
    assert f"/api/media/{media_id}/content" not in html


def test_preview_round_trips_article_with_media_id_and_no_url(
    sample_issue, client: TestClient
):
    """The article-logo upload flow now stores the Media Library id in a
    hidden companion field instead of stuffing /api/media/{id}/content
    into the URL input — that relative URL failed Pydantic's AnyHttpUrl
    validator on every preview/eml round-trip and broke uploaded article
    logos.

    Regression net: an article with media_id set and url empty must
    serialize cleanly through /api/preview/html and render via the
    /api/media endpoint.
    """
    payload = sample_issue.model_dump(mode="json")
    # The fixture sets publication_logo_url on the first article; switch
    # it to an upload-shaped reference (media_id, no url) to simulate the
    # post-upload state the JS would POST.
    payload["articles"][0].pop("publication_logo_url", None)
    payload["articles"][0]["publication_logo_media_id"] = 17  # numeric int — the simpler validator path

    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    assert 'src="/api/media/17/content"' in res.text


def test_preview_accepts_string_media_id_for_lax_int_coercion(
    sample_issue, client: TestClient
):
    """JSON from a <input type='hidden'> serializes as a string. Pydantic's
    default lax mode coerces "17" → 17 for int fields — confirm that path
    works so we don't have to special-case the hidden input on the JS side."""
    payload = sample_issue.model_dump(mode="json")
    payload["articles"][0].pop("publication_logo_url", None)
    payload["articles"][0]["publication_logo_media_id"] = "17"

    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    assert 'src="/api/media/17/content"' in res.text


def test_preview_endpoint_rejects_invalid_issue(client: TestClient):
    """The UI relies on a 422 to surface validation errors when required
    fields are missing — confirm that round trip works."""
    res = client.post("/api/preview/html", json={})
    assert res.status_code == 422


# ───────── FAB + sortable form sections ─────────────────────────────


def test_form_pane_has_floating_add_section_fab(client: TestClient):
    """A "+" floating action button hovers in the lower-right of the
    form-pane and opens a menu with three add-section options."""
    body = client.get("/").text
    # The FAB lives inside .form-pane (so the grid-stack alignment anchors
    # to the form-pane viewport, not the global viewport).
    fab_idx = body.index('id="add-section-fab"')
    pane_start = body.index('class="form-pane"')
    pane_end = body.index("</section>", pane_start)
    assert pane_start < fab_idx < pane_end, (
        "Add-section FAB must live inside the .form-pane so it anchors "
        "to the form-pane corner, not the global page."
    )
    # Menu contains exactly the three sortable section types.
    assert 'id="add-section-menu"' in body
    for t in ("articles", "podcast", "static_blocks"):
        assert f'data-add-type="{t}"' in body, (
            f"Add-section menu missing option for {t}"
        )


def test_sortable_sections_wrapper_present(client: TestClient):
    """Articles / Podcast / Static-blocks / Highlights <details> live in a single
    wrapper that hosts drag-reorder and is bracketed by the fixed
    Issue Details (above) and Footer (below)."""
    body = client.get("/").text
    assert 'id="sortable-sections"' in body
    # Must NOT contain a sortable wrapper around the fixed sections.
    sortable_idx = body.index('id="sortable-sections"')
    issue_details_idx = body.index("Issue Details")
    footer_idx = body.index(">Footer<")
    assert issue_details_idx < sortable_idx < footer_idx, (
        "Sortable region must sit between Issue Details and Footer "
        "so the fixed sections never get sorted."
    )


def test_section_templates_present_for_each_sortable_type(client: TestClient):
    body = client.get("/").text
    for tpl_id in (
        "articles-section-template",
        "podcast-section-template",
        "static-blocks-section-template",
    ):
        assert f'id="{tpl_id}"' in body, (
            f"Composer markup missing section template: {tpl_id}"
        )
    # Each template carries a section-drag-handle (visible only when 2+
    # sortable sections exist) and a remove button.
    for tpl_id in (
        "articles-section-template",
        "podcast-section-template",
        "static-blocks-section-template",
    ):
        tpl_start = body.index(f'id="{tpl_id}"')
        tpl_end = body.index("</template>", tpl_start)
        tpl = body[tpl_start:tpl_end]
        assert "section-drag-handle" in tpl, (
            f"{tpl_id} should expose a section-drag-handle for sorting"
        )
        assert "section-remove" in tpl, (
            f"{tpl_id} should expose a section-remove (×) button"
        )
        assert 'data-section-type=' in tpl, (
            f"{tpl_id} should advertise its section type via data-section-type"
        )


def test_composer_js_wires_sortable_section_drag(client: TestClient):
    """JS provides section-level drag wiring distinct from the existing
    article-card drag, scoped to #sortable-sections so the fixed
    Issue Details / Footer can never be reordered."""
    js = client.get("/static/composer.js").text
    # Section-level drag helpers + state are present.
    for needle in (
        "addSection",
        "wireSectionDragHandle",
        "draggingSectionEl",
        "sortable-section",
        "updateSectionDragVisibility",
    ):
        assert needle in js, f"composer.js missing section-drag hook: {needle}"
    # The section dragover listener is bound to #sortable-sections, not
    # the whole document (which would let the user drag a section into
    # the fixed regions).
    assert "sortableSections.addEventListener('dragover'" in js, (
        "section dragover must be scoped to #sortable-sections so fixed "
        "sections can't be reordered"
    )


def test_composer_js_emits_form_sections_metadata(client: TestClient):
    """`buildIssue()` records the sortable section structure so drafts
    round-trip the user's chosen order/grouping."""
    js = client.get("/static/composer.js").text
    # The metadata key.
    assert "form_sections" in js
    # buildIssue walks sortable sections in DOM order (not the legacy
    # singleton containers).
    assert "sortableSectionsAll" in js
    # loadIssueIntoForm restores from form_sections when present.
    assert "issue.form_sections" in js


def test_preview_endpoint_ignores_form_sections_extra(client: TestClient):
    """The form_sections metadata is a UI-only hint — it must flow to
    /api/preview/html without tripping pydantic's validator (extras
    are silently ignored on NewsletterIssue)."""
    payload = {
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "BeijingAutoShow",
        "issue_date": "2026-05-03",
        "form_sections": [
            {"type": "articles", "id": "s-1", "count": 0},
            {"type": "podcast", "id": "s-2", "enabled": False},
            {"type": "static_blocks", "id": "s-3", "count": 0},
        ],
    }
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text


def test_compose_form_does_not_render_sortable_handle_for_fixed_sections(client: TestClient):
    """Issue Details / Footer must NOT carry the
    sortable-section class — only items inside #sortable-sections do."""
    body = client.get("/").text
    # Locate the fixed section <details> by their summary labels and
    # verify they don't carry the sortable-section class.
    for label in ("Issue Details", ">Footer<"):
        idx = body.index(label)
        # Walk backwards to the nearest <details ...>
        det_start = body.rfind("<details", 0, idx)
        det_open_end = body.index(">", det_start)
        det_tag = body[det_start:det_open_end]
        assert "sortable-section" not in det_tag, (
            f"Fixed section near {label!r} should not be sortable-section"
        )


def test_sortable_section_templates_carry_full_drag_handle_affordance(client: TestClient):
    """The drag-handle on each sortable section template must carry the
    full accessibility + iconography contract the CSS + JS rely on:

      - element class ``section-drag-handle`` (CSS visibility + cursor)
      - ``aria-label`` so screen readers announce the affordance
      - ``title`` so hovering reveals the affordance on desktop
      - an inline ``<svg>`` grip icon (CSS sizes it to 14×18)

    Regression net for the moment someone removes ``draggable="false"``
    on the handle (it must default to false so clicks on the handle
    don't *also* start a drag of the parent <summary>; mousedown flips
    it to true at runtime — see wireSectionDragHandle).
    """
    body = client.get("/").text
    for tpl_id in (
        "articles-section-template",
        "podcast-section-template",
        "static-blocks-section-template",
    ):
        tpl_start = body.index(f'id="{tpl_id}"')
        tpl_end = body.index("</template>", tpl_start)
        tpl = body[tpl_start:tpl_end]
        # Drag-handle element + a11y wiring
        assert 'class="section-drag-handle"' in tpl, (
            f"{tpl_id}: handle must be exactly the class CSS keys off of"
        )
        assert 'aria-label="Drag to reorder section"' in tpl, (
            f"{tpl_id}: drag handle must announce itself to assistive tech"
        )
        assert 'title="Drag to reorder section"' in tpl, (
            f"{tpl_id}: drag handle needs a hover tooltip"
        )
        # Inline grip icon — sized by CSS rule
        # `#composer .sortable-section .section-drag-handle svg`
        # (matches the dotted-grip style of the article-card handles).
        handle_idx = tpl.index('class="section-drag-handle"')
        handle_close = tpl.index("</span>", handle_idx)
        handle = tpl[handle_idx:handle_close]
        assert "<svg" in handle, (
            f"{tpl_id}: drag handle must inline an SVG grip icon"
        )
        # The handle element itself must NOT be marked
        # ``draggable="true"`` — wireSectionDragHandle flips the
        # *parent* <details> section's `.draggable` on mousedown, so a
        # draggable handle would race the section drag and produce a
        # confused state. (Default for <span> is non-draggable, so no
        # explicit ``draggable="false"`` is required; just absence of
        # the true form.)
        assert 'draggable="true"' not in handle, (
            f"{tpl_id}: section-drag-handle must not be draggable=\"true\" — "
            "wireSectionDragHandle drives the section drag, not the handle"
        )


def test_section_reorder_round_trips_through_draft_save_and_load(client: TestClient):
    """Simulate a user dragging the Podcast section to sit above the
    Articles section, then back-and-forth through the draft store. The
    DOM-position invariant is encoded in ``form_sections`` — the
    composer.js section walker preserves DOM order in that array and
    loadIssueIntoForm rehydrates from it, so the surviving order in the
    saved draft is the authoritative test of "the drag reorder stuck."
    """
    # Two saves of the same set of sections, with the form_sections
    # array describing two different orders. /api/drafts round-trips
    # the JSON payload verbatim — what we POST is what we get back.
    base = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [
            {"headline": "A", "url": "https://example.com/a"},
        ],
    }
    payload_order_a = {
        **base,
        "form_sections": [
            {"type": "articles", "id": "s-1", "count": 1},
            {"type": "podcast", "id": "s-2", "enabled": True},
            {"type": "static_blocks", "id": "s-3", "count": 0},
        ],
    }
    payload_order_b = {
        **base,
        "form_sections": [
            # Podcast first, then static_blocks, then articles.
            {"type": "podcast", "id": "s-2", "enabled": True},
            {"type": "static_blocks", "id": "s-3", "count": 0},
            {"type": "articles", "id": "s-1", "count": 1},
        ],
    }

    # Save order A.
    a = client.post(
        "/api/drafts", json={"name": "Order A", "payload": payload_order_a}
    )
    assert a.status_code == 201, a.text
    a_id = a.json()["id"]
    # Save order B.
    b = client.post(
        "/api/drafts", json={"name": "Order B", "payload": payload_order_b}
    )
    assert b.status_code == 201, b.text
    b_id = b.json()["id"]

    # Reload each and confirm the section order survived the round-trip
    # — independently per draft, so the two saves don't bleed into one
    # another.
    a_got = client.get(f"/api/drafts/{a_id}").json()
    b_got = client.get(f"/api/drafts/{b_id}").json()
    a_order = [s["type"] for s in a_got["payload"]["form_sections"]]
    b_order = [s["type"] for s in b_got["payload"]["form_sections"]]
    assert a_order == ["articles", "podcast", "static_blocks"], a_order
    assert b_order == ["podcast", "static_blocks", "articles"], b_order


def test_section_reorder_preserves_article_ordering_within_articles_section(client: TestClient):
    """Within an Articles section, articles still appear in
    NewsletterIssue.articles slice order — the section can move
    around relative to other section types but the internal article
    sequence is the model's flat list."""
    payload = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [
            {"headline": "First", "url": "https://example.com/first"},
            {"headline": "Second", "url": "https://example.com/second"},
        ],
        "form_sections": [
            {"type": "podcast", "id": "s-2", "enabled": True},
            {"type": "articles", "id": "s-1", "count": 2},
        ],
    }
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    html = res.text
    first_idx = html.index("First")
    second_idx = html.index("Second")
    assert first_idx < second_idx, (
        "articles inside an Articles section render in articles-list "
        "order even when the section itself moves around"
    )


def test_section_reorder_drives_render_order_of_section_types(client: TestClient):
    """Dragging Podcast above Articles makes Podcast render first in
    the .eml — form_sections is the render plan, not just metadata.
    With no form_sections the renderer falls back to the legacy
    fixed order (articles → podcast → static blocks)."""
    payload_dragged = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [{"headline": "ArticleA", "url": "https://example.com/a"}],
        "podcast": {
            "episode_number": 1,
            "title": "PodcastA",
        },
        "static_blocks": [{"title": "StaticA", "html": "<p>x</p>"}],
        # Editor dragged: static → podcast → articles
        "form_sections": [
            {"type": "static_blocks", "count": 1},
            {"type": "podcast", "enabled": True},
            {"type": "articles", "count": 1},
        ],
    }
    html = client.post("/api/preview/html", json=payload_dragged).text
    static_idx = html.index("StaticA")
    podcast_idx = html.index("PodcastA")
    article_idx = html.index("ArticleA")
    assert static_idx < podcast_idx < article_idx, (
        "form_sections is the render plan — static then podcast then "
        "articles, exactly the dragged order"
    )

    # Legacy fallback: no form_sections → original fixed order.
    payload_legacy = {**payload_dragged}
    payload_legacy.pop("form_sections")
    html2 = client.post("/api/preview/html", json=payload_legacy).text
    a2 = html2.index("ArticleA")
    p2 = html2.index("PodcastA")
    s2 = html2.index("StaticA")
    assert a2 < p2 < s2, (
        "no form_sections → renderer falls back to legacy article → "
        "podcast → static_blocks ordering"
    )


def test_multiple_articles_sections_render_contiguous_slices(client: TestClient):
    """Two Articles sections with a Static block between them: each
    section's slice of NewsletterIssue.articles must render back-to-
    back, with the Static block landing in the middle per
    form_sections."""
    payload = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [
            {"headline": "ArticleA", "url": "https://example.com/a"},
            {"headline": "ArticleB", "url": "https://example.com/b"},
        ],
        "static_blocks": [{"title": "StaticMid", "html": "<p>x</p>"}],
        "form_sections": [
            {"type": "articles", "count": 1},
            {"type": "static_blocks", "count": 1},
            {"type": "articles", "count": 1},
        ],
    }
    html = client.post("/api/preview/html", json=payload).text
    a_idx = html.index("ArticleA")
    s_idx = html.index("StaticMid")
    b_idx = html.index("ArticleB")
    assert a_idx < s_idx < b_idx, (
        "form_sections must slice the flat articles list — first article "
        "before the static block, second article after"
    )


# ───────── Highlights overhaul: template + JS wiring smoke checks ────


def test_highlight_template_carries_search_and_hidden_cache_fields(client: TestClient):
    """The highlight row template must expose every hidden cache field
    the new search-driven picker populates, plus the per-row drag
    handle. Without these the SPA can't round-trip an archive-sourced
    highlight through preview/eml."""
    body = client.get("/").text
    tpl_start = body.index('id="highlight-template"')
    tpl_end = body.index("</template>", tpl_start)
    tpl = body[tpl_start:tpl_end]

    # Search input + dropdown + meta hint
    assert 'data-role="highlight-search"' in tpl
    assert 'data-role="highlight-suggestions"' in tpl
    assert 'data-role="highlight-meta"' in tpl

    # Drag handle (per-row, separate from the article card's)
    assert "highlight-drag-handle" in tpl

    # Hidden reference fields — both flavors must round-trip.
    for field in (
        "article_index",
        "source_archive_id",
        "source_article_index",
        "cached_headline",
        "cached_url",
        "cached_issue_date",
        "cached_issue_year",
        "cached_issue_number",
        "cached_issue_title",
    ):
        assert f'data-field="{field}"' in tpl, (
            f"highlight template missing hidden field for {field}"
        )


def test_composer_js_wires_archive_candidates_endpoint(client: TestClient):
    """The JS must reach /api/archive/highlight-candidates with a `q=`
    and drive the drop-down via wireHighlightRow / wireHighlightDrag."""
    js = client.get("/static/composer.js").text
    assert "/api/archive/highlight-candidates" in js
    assert "wireHighlightRow" in js
    assert "wireHighlightDrag" in js
    # The picker must explicitly support both flavors of HighlightRef.
    assert "source_archive_id" in js
    assert "cached_headline" in js


def test_highlight_row_styles_are_present(client: TestClient):
    """CSS sanity: the new row chrome + suggestions dropdown have to be
    styled so the form looks right after the rewrite."""
    css = client.get("/static/composer.css").text
    assert "highlight-row" in css
    assert "highlight-drag-handle" in css
    assert "highlight-suggestions" in css


def test_preview_endpoint_accepts_new_highlight_shape(client: TestClient):
    """Round-trip an archive-sourced highlight through /api/preview/html
    so the renderer's grouping + display_url + display_label all run
    against a realistic payload."""
    payload = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [
            {"headline": "Local A", "url": "https://example.com/a"},
        ],
        "highlights": [
            {
                "source_archive_id": 1,
                "source_article_index": 0,
                "cached_headline": "Featured From Last Month",
                "cached_url": "https://example.com/feat",
                "cached_issue_date": "2026-04-05",
                "cached_issue_title": "AprilEdition",
            },
            {"article_index": 0},
        ],
    }
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    html = res.text
    assert "Featured From Last Month" in html
    assert "This issue" in html
    assert "AprilEdition" in html


def test_preview_endpoint_rejects_highlight_with_no_reference(client: TestClient):
    """The model validator should reject a highlights row that points
    at nothing — protects against the SPA silently saving a row before
    the user picked a candidate."""
    payload = {
        "newsletter_name": "Test",
        "issue_year": 14,
        "issue_number": 9,
        "issue_title": "Demo",
        "issue_date": "2026-05-03",
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": [{"headline": "x", "url": "https://example.com/x"}],
        "highlights": [{"override_label": "no reference"}],
    }
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 422


# ───────── Autosave loop ─────────────────────────────────────────────


def test_settings_form_exposes_autosave_interval_input(client: TestClient):
    """The Settings dialog must surface the autosave interval so the
    user can tune it (or disable it via 0) without editing JSON. The
    range matches the AppSettings field validator (0..600)."""
    body = client.get("/").text
    assert 'name="autosave_interval_seconds"' in body
    # Min/max attributes mirror the pydantic field bounds so the
    # browser's number-input doesn't accept values the server will
    # 422 on.
    autosave_idx = body.index('name="autosave_interval_seconds"')
    # Walk back to the opening <input ...>
    input_start = body.rfind("<input", 0, autosave_idx)
    input_close = body.index(">", autosave_idx)
    input_tag = body[input_start : input_close + 1]
    assert 'min="0"' in input_tag
    assert 'max="600"' in input_tag


def test_composer_js_carries_autosave_helpers(client: TestClient):
    """The JS scheduler must expose the named hooks the rest of the
    SPA piggybacks on (loadDraft, manual save) — without them the
    autosave loop forks a new "Autosave — Untitled" draft every time
    the user reloads a previously-saved one."""
    js = client.get("/static/composer.js").text
    for needle in (
        "restartAutosave",
        "stampAutosaveSaved",
        "resetAutosaveTarget",
        "_autosaveTick",
        "/api/drafts",
        "navigator.sendBeacon",
    ):
        assert needle in js, f"composer.js missing autosave hook: {needle}"


def test_composer_js_autosave_pauses_while_a_dialog_is_open(client: TestClient):
    """The autosave loop must skip its tick while ANY native <dialog>
    is open. Saving mid-modal would either capture a transient form
    state the user didn't choose to commit (Compose-new confirmation,
    Save-draft naming) or fight the user for the "Saving…" status pill
    while they're focused on an error inside the dialog.

    Detection is centralized in ``_isAnyDialogOpen`` and the tick
    short-circuits via ``dialog[open]`` query. This test asserts both
    pieces are wired together so a future refactor that splits the
    helper into per-dialog state doesn't accidentally drop the pause.
    """
    js = client.get("/static/composer.js").text
    assert "_isAnyDialogOpen" in js
    assert "dialog[open]" in js
    # The tick body explicitly reads the pause helper before doing any
    # buildIssue/serialize work.
    tick_idx = js.index("const _autosaveTick =")
    tick_end = js.index("};", tick_idx)
    tick_body = js[tick_idx:tick_end]
    assert "_isAnyDialogOpen()" in tick_body, (
        "_autosaveTick must check the dialog-pause helper at the top of "
        "the tick — without that, the loop saves through modals"
    )


def test_index_renders_autosave_status_pill(client: TestClient):
    """The autosave-status pill lives in the topbar so the user can
    glance at it and know whether the loop is healthy."""
    body = client.get("/").text
    assert 'id="autosave-status"' in body


def test_drafts_put_round_trips_payload_for_autosave(client: TestClient):
    """The autosave loop relies on PUT /api/drafts/{id} accepting a
    `{payload}` body and returning the updated draft. This regression
    net guards the endpoint contract — a future refactor that
    accidentally requires `{name, payload}` would break autosave's
    PUT path silently from the SPA's perspective."""
    # Seed a draft.
    created = client.post(
        "/api/drafts",
        json={"name": "Autosave — Untitled", "payload": {"newsletter_name": "x"}},
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    # PUT a fresh payload — name omitted, just like the autosave loop.
    updated = client.put(
        f"/api/drafts/{draft_id}",
        json={"payload": {"newsletter_name": "x", "issue_title": "Updated"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["payload"]["issue_title"] == "Updated"

    # GET reflects the update.
    got = client.get(f"/api/drafts/{draft_id}").json()
    assert got["payload"]["issue_title"] == "Updated"


# ───────── Upload buttons trigger the OS file picker ─────────────────


def test_masthead_upload_uses_visually_hidden_file_input(client: TestClient):
    """The Settings masthead Upload button opens the OS picker via a
    programmatic .click() on the file input. That only works reliably
    when the input is still in the layout tree — `hidden` (display:none)
    breaks the call on some browser+OS combos. Assert we use the
    .visually-hidden-file pattern instead so programmatic clicks always
    open the picker."""
    body = client.get("/").text
    upload_start = body.index('id="masthead-upload-file"')
    # Walk back to the opening <input ...> so we read attrs on this tag only.
    input_start = body.rfind("<input", 0, upload_start)
    input_close = body.index(">", upload_start)
    input_tag = body[input_start : input_close + 1]
    assert 'class="visually-hidden-file"' in input_tag, (
        "masthead file input must use visually-hidden-file (not the "
        "`hidden` attribute) so programmatic .click() opens the picker"
    )
    # `hidden` attribute should NOT be on this input.
    assert " hidden" not in input_tag, (
        "masthead file input must not carry the `hidden` HTML attribute "
        "— it triggers display:none which breaks input.click()"
    )

    css = client.get("/static/composer.css").text
    assert ".visually-hidden-file" in css, (
        "composer.css must define the .visually-hidden-file helper"
    )
    # The helper must keep the input in the layout tree (position
    # off-screen rather than display:none) — otherwise we're back to
    # the same .click() issue.
    helper_idx = css.index(".visually-hidden-file")
    helper_end = css.index("}", helper_idx)
    helper_body = css[helper_idx:helper_end]
    assert "position:" in helper_body and "absolute" in helper_body, (
        "visually-hidden-file must position the input off-screen, not "
        "remove it from the layout tree"
    )
    assert "display: none" not in helper_body, (
        "visually-hidden-file must NOT use display:none (would break "
        "input.click())"
    )


def test_logos_and_media_upload_tab_uses_button_then_picker_flow(client: TestClient):
    """The Upload tab in Logos & Media should match the Settings
    masthead pattern: a visible Upload… button + a hidden file input.
    Clicking the button programmatically clicks the input, which opens
    the OS picker, and the picker's `change` event auto-POSTs. No
    separate submit step.
    """
    body = client.get("/").text
    # Upload tab form is present.
    upload_form_start = body.index('id="media-upload-form"')
    upload_form_end = body.index("</form>", upload_form_start)
    form = body[upload_form_start:upload_form_end]

    # Visible trigger button (type=button so it doesn't submit).
    assert 'id="media-upload-trigger"' in form, (
        "Logos & Media upload form must expose a media-upload-trigger button"
    )
    trigger_idx = form.index('id="media-upload-trigger"')
    btn_start = form.rfind("<button", 0, trigger_idx)
    btn_close = form.index(">", trigger_idx)
    btn_tag = form[btn_start : btn_close + 1]
    assert 'type="button"' in btn_tag, (
        "media-upload-trigger must be type=button (not submit) so the "
        "click opens the picker rather than submitting the form"
    )

    # File input is the visually-hidden flavor, not display:none.
    assert 'id="media-upload-file"' in form
    upload_input_idx = form.index('id="media-upload-file"')
    input_start = form.rfind("<input", 0, upload_input_idx)
    input_close = form.index(">", upload_input_idx)
    input_tag = form[input_start : input_close + 1]
    assert 'class="visually-hidden-file"' in input_tag, (
        "Logos & Media file input must use visually-hidden-file so "
        "programmatic .click() opens the picker"
    )

    # JS wires the trigger to the file input + auto-runs the upload
    # on the file picker's `change` event.
    js = client.get("/static/composer.js").text
    assert "mediaUploadTrigger" in js
    assert "mediaUploadFileInput" in js
    assert "_runMediaUpload" in js
    # The trigger's click handler clears + clicks the hidden input —
    # both calls must be present so the upload retriggers on the
    # picker's change event.
    trigger_idx = js.index("mediaUploadTrigger.addEventListener('click'")
    block_end = js.index("});", trigger_idx)
    trigger_block = js[trigger_idx:block_end]
    assert "mediaUploadFileInput.click()" in trigger_block, (
        "media-upload-trigger click handler must call .click() on the "
        "hidden file input — that's what opens the OS picker"
    )


# ───────── WYSIWYG toolbars + picker URL fetch ───────────────────────


def test_basic_richtext_toolbars_wrap_intro_pull_quote_and_comment(client: TestClient):
    """The composer surfaces a Quill editor for every textarea whose content
    should accept inline HTML — Intro Message in Issue Details, plus Pull
    quote and Comment inside each article card."""
    body = client.get("/").text

    # Intro Message inside Issue Details
    intro_idx = body.index('name="intro_html"')
    intro_wrap_open = body.rfind('data-quill-field="intro_html"', 0, intro_idx)
    assert intro_wrap_open >= 0

    # Pull quote + Comment inside the article template
    article_tpl_start = body.index('id="article-template"')
    article_tpl_end = body.index('</template>', article_tpl_start)
    article_tpl = body[article_tpl_start:article_tpl_end]
    # Each textarea sits in its own data-quill-field.
    assert 'data-quill-field="pull_quote"' in article_tpl
    assert 'data-quill-field="alain_comment"' in article_tpl

    # JS exposes the generalized helper and binds it on init and per-card.
    js = client.get("/static/composer.js").text
    assert "wireQuillEditor" in js
    # Wired from the form-level init (Intro Message lives in the static
    # form, not the article template).
    assert "form.querySelectorAll('[data-quill-field]'" in js
    # Wired per article card so cloned Pull-quote + Comment scopes get
    # picked up too.
    assert "node.querySelectorAll('[data-quill-field]'" in js


def test_basic_richtext_toolbar_css_helpers_are_styled(client: TestClient):
    """Quill's toolbar and container get custom styling to match our UI."""
    css = client.get("/static/composer.css").text
    assert '.ql-toolbar' in css
    assert '.ql-container' in css


def test_media_picker_dialog_carries_url_fetch_form(client: TestClient):
    """The Pick-an-image dialog accepts a new asset by URL in addition
    to picking from the existing library list. The URL form requires
    both URL and Image description — alt_text is otherwise the
    static-block image-embed flow's hard gate on insertion."""
    body = client.get("/").text
    picker_idx = body.index('id="media-picker-dialog"')
    picker_end = body.index('</dialog>', picker_idx)
    picker = body[picker_idx:picker_end]

    assert 'id="media-picker-url-form"' in picker, (
        "Pick-an-image dialog must expose the URL-fetch form"
    )
    # Both required inputs are present.
    assert 'name="url"' in picker
    assert 'name="alt_text"' in picker
    assert 'id="media-picker-url-status"' in picker, (
        "URL-fetch form needs a status slot for the user to see fetch errors"
    )

    js = client.get("/static/composer.js").text
    # The URL-fetch submit hits the same /api/media surface the
    # Logos & Media dialog uses for URL imports, then PUTs the
    # description so the picker's onPick callback sees a non-empty
    # alt_text and isn't blocked downstream.
    assert "mediaPickerUrlForm" in js
    assert "mediaPickerUrlForm.addEventListener('submit'" in js
    # Carries the picker's current `kind` through the URL register.
    assert "_pickerKind" in js


def test_image_embed_picker_via_static_block_can_accept_url_fetch(client: TestClient):
    """Sanity: the static-block image-embed toolbar opens the same
    picker that the URL fetch form is bound to, so a user clicking
    Image inside a static block can now paste a URL and immediately
    embed without bouncing to Logos & Media first."""
    js = client.get("/static/composer.js").text
    # The static-block Image action still calls openLibraryPicker
    # with kind=image — that picker now carries the URL form.
    assert "openLibraryPicker({" in js
    # The actual handler is now inside wireQuillEditor.
    assert "toolbar.addHandler('image'" in js or 'toolbar.addHandler("image"' in js
    assert "openLibraryPicker" in js


# ───────── Inline masthead Image-description editor ──────────────────


def test_settings_renders_inline_masthead_alt_text_editor(client: TestClient):
    """The Settings dialog must expose an inline Image-description
    editor for the uploaded masthead logo so the operator never has to
    detour through Logos & Media to satisfy the alt-text gate the
    server enforces at Save time."""
    body = client.get("/").text
    # The row is hidden by default (no media id selected yet) — but
    # the markup must be present so the JS can show it after upload.
    assert 'data-role="masthead-alt-row"' in body
    assert 'data-role="masthead-alt-text"' in body
    assert 'id="masthead-alt-input"' in body
    assert 'id="masthead-alt-status"' in body
    # Server PUTs alt_text through this input — confirm the JS wiring
    # is present so a future refactor doesn't drop the surface. The
    # masthead path uses a shared bindAltTextEditor helper that's
    # also driving the Article-card flow.
    js = client.get("/static/composer.js").text
    assert "bindAltTextEditor" in js, (
        "composer.js must expose a shared alt-text editor helper — "
        "previously two near-identical persistAlt/showAltEditor blocks "
        "lived in the file"
    )
    assert "mastheadAltEditor" in js, (
        "Settings masthead must bind the shared editor via "
        "bindAltTextEditor — not roll its own"
    )
    assert "syncMastheadAltEditor" in js
    # The upload success path must surface the editor instead of
    # printing the old "set it in Logos & Media first" error.
    assert "Fill the Image description below before Save" in js
    # The shared editor body PUTs /api/media/{id} — guard the
    # contract end-to-end.
    helper_idx = js.index("const bindAltTextEditor =")
    # The arrow function's closing brace is on its own line so we can
    # locate the helper body by walking forward to the next blank-line
    # boundary in the IIFE.
    helper_end = js.index("\n  };", helper_idx)
    helper_body = js[helper_idx:helper_end]
    assert "/api/media/" in helper_body
    assert "method: 'PUT'" in helper_body


def test_legacy_masthead_alt_text_error_string_is_gone(client: TestClient):
    """The old "Set its Image description in Logos & Media first" line
    pushed the user out of Settings unnecessarily. With the inline
    editor, that wording is replaced by an in-place hint pointing at
    the new input."""
    js = client.get("/static/composer.js").text
    assert "Set its Image description in Logos & Media first" not in js
