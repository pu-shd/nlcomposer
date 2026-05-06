"""Tests for the composer UI page and its served static assets.

These tests do not exercise the JS in a browser — that is done via the
manual smoke-test against a live uvicorn instance (see Makefile). They
do verify that the HTML page is served, references the expected static
assets, and that those assets are reachable through the FastAPI app.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_index_serves_composer_page():
    res = client.get("/")
    assert res.status_code == 200
    body = res.text
    # Page must contain the form skeleton with all top-level sections so
    # Alain can drive the whole pipeline through the UI without curl.
    assert "<title>SDC Composer</title>" in body
    for legend in (
        "Issue header",
        "From / To / Subject",
        "Articles",
        "Podcast",
        "Static blocks",
        "Bottom highlights",
        "Footer",
    ):
        assert legend in body, f"composer page missing section: {legend}"
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


def test_static_assets_are_served():
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


def test_preview_endpoint_renders_for_minimal_issue():
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


def test_preview_endpoint_round_trips_full_issue(sample_issue):
    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/preview/html", json=payload)
    assert res.status_code == 200, res.text
    body = res.text
    assert "Beijing Auto Show 2026" in body
    assert "Camille Kamga" in body
    assert "Highlights" in body


def test_eml_endpoint_returns_attachment_for_full_issue(sample_issue):
    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("message/rfc822")
    disp = res.headers["content-disposition"]
    assert "attachment" in disp
    assert "BeijingAutoShow" in disp
    assert b"Subject:" in res.content
    assert b"multipart/alternative" in res.content


def test_preview_endpoint_rejects_invalid_issue():
    """The UI relies on a 422 to surface validation errors when required
    fields are missing — confirm that round trip works."""
    res = client.post("/api/preview/html", json={})
    assert res.status_code == 422
