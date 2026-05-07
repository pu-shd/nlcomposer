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


@pytest.fixture
def client() -> TestClient:
    """Use the lifespan context manager so init_db runs on startup."""
    with TestClient(app) as c:
        yield c


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
    # Turn off auto-archive
    conn = connect()
    init_db(conn)
    try:
        save_settings(conn, AppSettings(auto_archive_on_download=False))
        conn.commit()
    finally:
        conn.close()

    payload = sample_issue.model_dump(mode="json")
    res = client.post("/api/eml", json=payload)
    assert res.status_code == 200

    listing = client.get("/api/archive").json()
    assert listing == []


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
