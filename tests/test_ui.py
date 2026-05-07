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


@pytest.fixture
def client() -> TestClient:
    """Use the lifespan context manager so init_db runs on startup."""
    with TestClient(app) as c:
        yield c


def test_index_serves_composer_page(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    # Page must contain the form skeleton with all top-level sections so
    # Alain can drive the whole pipeline through the UI without curl.
    assert "<title>SDC Composer</title>" in body
    for legend in (
        "Issue Details",
        "Articles",
        "Podcast",
        "Static blocks",
        "Bottom highlights",
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


def test_preview_endpoint_rejects_invalid_issue(client: TestClient):
    """The UI relies on a 422 to surface validation errors when required
    fields are missing — confirm that round trip works."""
    res = client.post("/api/preview/html", json={})
    assert res.status_code == 422
