"""Tests for the archive store + /api/archive endpoints + auto-archive on /api/eml."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.archive_store import (
    ArchiveCreate,
    create_archive,
    delete_archive,
    get_archive,
    get_archive_eml,
    list_archive,
)
from app.db import connect, init_db
from app.main import app
from app.settings import AppSettings, save_settings


SAMPLE_EML_BYTES = (
    b"From: alice@example.com\r\nTo: bob@example.com\r\n"
    b'Subject: SmartDrivingCars 14.09 \xe2\x80\x94 BeijingAutoShow\r\n\r\n'
    b"hello world\r\n"
)


# ───────── Store-level CRUD ──────────────────────────────────────────


def test_create_and_get_archive():
    conn = connect()
    init_db(conn)
    try:
        a = create_archive(
            conn,
            ArchiveCreate(
                slug="14.9-BeijingAutoShow-5.03.26",
                subject="SmartDrivingCars 14.09 — BeijingAutoShow",
                filename="14.09-BeijingAutoShow-2026-05-03.eml",
                payload={"issue_year": 14, "issue_number": 9},
                eml_bytes=SAMPLE_EML_BYTES,
            ),
        )
        conn.commit()
        loaded = get_archive(conn, a.id)
        eml = get_archive_eml(conn, a.id)
    finally:
        conn.close()
    assert loaded is not None
    assert loaded.slug == "14.9-BeijingAutoShow-5.03.26"
    assert loaded.subject == "SmartDrivingCars 14.09 — BeijingAutoShow"
    assert loaded.size_bytes == len(SAMPLE_EML_BYTES)
    assert loaded.payload == {"issue_year": 14, "issue_number": 9}
    assert eml is not None
    assert eml[0] == "14.09-BeijingAutoShow-2026-05-03.eml"
    assert eml[1] == SAMPLE_EML_BYTES


def test_list_orders_by_created_desc():
    conn = connect()
    init_db(conn)
    try:
        a = create_archive(
            conn,
            ArchiveCreate(
                slug="A", filename="a.eml", payload={"a": 1},
                eml_bytes=b"a",
            ),
        )
        time.sleep(0.005)
        b = create_archive(
            conn,
            ArchiveCreate(
                slug="B", filename="b.eml", payload={"b": 1},
                eml_bytes=b"bb",
            ),
        )
        conn.commit()
        rows = list_archive(conn)
    finally:
        conn.close()
    # Most-recent first
    assert [r.id for r in rows] == [b.id, a.id]
    assert rows[0].size_bytes == 2
    assert rows[1].size_bytes == 1


def test_list_empty():
    conn = connect()
    init_db(conn)
    try:
        rows = list_archive(conn)
    finally:
        conn.close()
    assert rows == []


def test_delete_archive():
    conn = connect()
    init_db(conn)
    try:
        a = create_archive(
            conn,
            ArchiveCreate(
                slug="x", filename="x.eml", payload={}, eml_bytes=b"x",
            ),
        )
        conn.commit()
        assert delete_archive(conn, a.id) is True
        assert get_archive(conn, a.id) is None
    finally:
        conn.close()


def test_delete_unknown_returns_false():
    conn = connect()
    init_db(conn)
    try:
        assert delete_archive(conn, 99999) is False
    finally:
        conn.close()


# ───────── HTTP endpoints ────────────────────────────────────────────


def test_list_initially_empty(client: TestClient):
    res = client.get("/api/archive")
    assert res.status_code == 200
    assert res.json() == []


def test_get_unknown_returns_404(client: TestClient):
    res = client.get("/api/archive/99999")
    assert res.status_code == 404


def test_download_unknown_returns_404(client: TestClient):
    res = client.get("/api/archive/99999/download")
    assert res.status_code == 404


def test_delete_unknown_returns_404(client: TestClient):
    res = client.delete("/api/archive/99999")
    assert res.status_code == 404


# ───────── Auto-archive integration ──────────────────────────────────


def test_eml_download_auto_archives_when_enabled(sample_issue, client: TestClient):
    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("message/rfc822")

    listing = client.get("/api/archive").json()
    assert len(listing) == 1
    item = listing[0]
    assert item["slug"] == sample_issue.header_slug()
    assert item["subject"] == sample_issue.effective_subject()
    assert item["filename"].endswith(".eml")
    assert item["size_bytes"] > 100  # the rendered eml is non-trivial

    # The detail endpoint returns the full payload, but not the bytes
    detail = client.get(f"/api/archive/{item['id']}").json()
    assert detail["payload"]["issue_title"] == sample_issue.issue_title
    assert "eml_bytes" not in detail

    # The download endpoint streams back rfc822 bytes that match what /api/eml returned
    re_download = client.get(f"/api/archive/{item['id']}/download")
    assert re_download.status_code == 200
    assert re_download.headers["content-type"].startswith("message/rfc822")
    assert re_download.content == res.content


def test_eml_download_does_not_archive_when_disabled(sample_issue, client: TestClient):
    settings_payload = client.get("/api/settings").json()
    settings_payload["auto_archive_on_download"] = False
    put_res = client.put("/api/settings", json=settings_payload)
    assert put_res.status_code == 200

    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200

    listing = client.get("/api/archive").json()
    assert len(listing) == 0





def test_archive_delete_returns_204(sample_issue, client: TestClient):
    client.post("/api/eml", json=sample_issue.model_dump(mode="json"))
    [item] = client.get("/api/archive").json()
    res = client.delete(f"/api/archive/{item['id']}")
    assert res.status_code == 204
    assert client.get(f"/api/archive/{item['id']}").status_code == 404


def test_multiple_downloads_create_multiple_entries(sample_issue, client: TestClient):
    payload = sample_issue.model_dump(mode="json")
    client.post("/api/eml", json=payload)
    client.post("/api/eml", json=payload)
    listing = client.get("/api/archive").json()
    assert len(listing) == 2


# ───────── UI wiring (smoke checks) ──────────────────────────────────


def test_index_includes_archive_button_and_dialog(client: TestClient):
    body = client.get("/").text
    assert 'id="open-archive"' in body
    assert 'id="archive-dialog"' in body


def test_composer_js_calls_archive_endpoint(client: TestClient):
    js = client.get("/static/composer.js").text
    assert "/api/archive" in js
    assert "openArchiveDialog" in js


# ───────── /api/archive/highlight-candidates ─────────────────────────


def _seed_archive_with_issue(client, *, slug, articles, issue_year=14, issue_number=9,
                              issue_title="DemoIssue", issue_date="2026-04-01"):
    """Helper: POST an issue to /api/eml so the archive table gets a row
    with the supplied articles. This is the natural seeding path — the
    composer never inserts archive rows directly.

    Returns the freshly-created archive id (from the list endpoint).
    """
    payload = {
        "newsletter_name": "Test",
        "issue_year": issue_year,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "issue_date": issue_date,
        "from_name": "x",
        "from_email": "x@example.com",
        "to_email": "y@example.com",
        "articles": articles,
    }
    # /api/eml writes the archive row when auto_archive_on_download=True
    # (the default).
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200, res.text
    lst = client.get("/api/archive").json()
    return lst[0]["id"]


def test_highlight_candidates_empty_archive_returns_empty_list(client: TestClient):
    res = client.get("/api/archive/highlight-candidates")
    assert res.status_code == 200
    assert res.json() == []


def test_highlight_candidates_surfaces_articles_from_archive(client: TestClient):
    _seed_archive_with_issue(
        client,
        slug="14.9-DemoIssue-4.01.26",
        articles=[
            {"headline": "Beijing Auto Show", "url": "https://example.com/beijing"},
            {"headline": "Why Are We Still Driving?", "url": "https://example.com/why"},
        ],
    )
    res = client.get("/api/archive/highlight-candidates")
    assert res.status_code == 200
    items = res.json()
    headlines = {c["headline"] for c in items}
    assert "Beijing Auto Show" in headlines
    assert "Why Are We Still Driving?" in headlines
    # Issue-level metadata propagates onto every candidate so the SPA
    # can populate cached_* fields without a second fetch.
    for c in items:
        assert c["issue_title"] == "DemoIssue"
        assert c["issue_year"] == 14
        assert c["issue_number"] == 9
        assert c["issue_date"] == "2026-04-01"


def test_highlight_candidates_filters_by_query_case_insensitive(client: TestClient):
    _seed_archive_with_issue(
        client,
        slug="14.9-DemoIssue-4.01.26",
        articles=[
            {"headline": "Beijing Auto Show", "url": "https://example.com/beijing"},
            {"headline": "Why Are We Still Driving?", "url": "https://example.com/why"},
        ],
    )
    res = client.get("/api/archive/highlight-candidates?q=BEIJING")
    items = res.json()
    assert len(items) == 1
    assert items[0]["headline"] == "Beijing Auto Show"


def test_highlight_candidates_dedups_by_url_keeping_newest(client: TestClient):
    """The same article URL surfacing in TWO archived editions should
    appear once — the newer occurrence wins so the cached metadata
    points at the most recent feature."""
    # Older edition first — its archive row gets created earlier.
    _seed_archive_with_issue(
        client,
        slug="14.8-Older-3.25.26",
        issue_number=8,
        issue_title="Older",
        issue_date="2026-03-25",
        articles=[
            {"headline": "Recurring article", "url": "https://example.com/recur"},
        ],
    )
    # Tiny sleep so the second created_at is strictly newer.
    time.sleep(0.01)
    _seed_archive_with_issue(
        client,
        slug="14.9-Newer-4.01.26",
        issue_number=9,
        issue_title="Newer",
        issue_date="2026-04-01",
        articles=[
            {"headline": "Recurring article (reposted)", "url": "https://example.com/recur"},
        ],
    )
    items = client.get("/api/archive/highlight-candidates").json()
    by_url = [c for c in items if c["url"] == "https://example.com/recur"]
    assert len(by_url) == 1, f"expected 1 deduped, got: {by_url}"
    # Newest archive entry wins; the candidate carries that one's metadata.
    assert by_url[0]["issue_title"] == "Newer"


def test_highlight_candidates_respects_days_window(client: TestClient):
    """`days=0`-ish requests should not surface arbitrarily old archives.
    Use the smallest legal window (1 day) and seed with a 'today' issue
    so it lands inside the window — the endpoint clamps days ≥ 1."""
    _seed_archive_with_issue(
        client,
        slug="recent",
        articles=[{"headline": "Recent", "url": "https://example.com/recent"}],
    )
    res = client.get("/api/archive/highlight-candidates?days=1")
    assert res.status_code == 200
    assert any(c["headline"] == "Recent" for c in res.json())


def test_highlight_candidates_route_does_not_collide_with_parametric_archive(
    client: TestClient,
):
    """Regression for the FastAPI route-order bug: /highlight-candidates
    must be matched before /{archive_id} or Starlette 422s on the int
    parse before falling through. The presence of this test guards the
    declaration order in app/controllers/archive.py."""
    res = client.get("/api/archive/highlight-candidates")
    assert res.status_code == 200
    # Sanity: the parametric route still 404s normally on a real int that
    # doesn't exist (would 422 if the literal route were placed wrong and
    # int parsing already happened).
    res2 = client.get("/api/archive/9999")
    assert res2.status_code == 404
