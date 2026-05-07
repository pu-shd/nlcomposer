"""Unit tests for the Media Library data layer."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import connect, init_db
from app.media_store import (
    MediaItem,
    create_upload,
    delete_media,
    get_media,
    get_media_by_url,
    get_media_bytes,
    list_media,
    mark_failed,
    mark_ready,
    sniff_image_kind,
    update_alt_text,
    upsert_by_url,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-but-non-empty"
JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIFfake"


@pytest.fixture
def conn():
    c = connect()
    init_db(c)
    yield c
    c.close()


def test_media_table_exists_after_init_db(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='media'"
    ).fetchall()
    assert len(rows) == 1


def test_upsert_by_url_creates_pending_row(conn):
    item = upsert_by_url(conn, url="https://example.com/logo.png", kind="logo")
    assert isinstance(item, MediaItem)
    assert item.id > 0
    assert item.kind == "logo"
    assert str(item.url) == "https://example.com/logo.png"
    assert item.status == "pending"
    assert item.alt_text == ""
    assert item.has_bytes is False
    assert item.error is None


def test_upsert_by_url_is_idempotent(conn):
    a = upsert_by_url(conn, url="https://example.com/x.png", kind="logo")
    b = upsert_by_url(conn, url="https://example.com/x.png", kind="logo")
    assert a.id == b.id


def test_upsert_does_not_change_existing_kind(conn):
    a = upsert_by_url(conn, url="https://example.com/y.png", kind="image")
    b = upsert_by_url(conn, url="https://example.com/y.png", kind="logo")
    assert a.id == b.id
    assert b.kind == "image"  # first reference wins


def test_get_media_by_url_returns_none_for_missing(conn):
    assert get_media_by_url(conn, "https://nope.example.com/x.png") is None


def test_create_upload_persists_bytes_and_starts_ready(conn):
    item = create_upload(
        conn,
        kind="logo",
        filename="alain.png",
        content_type="image/png",
        content_bytes=PNG_BYTES,
    )
    assert item.status == "ready"
    assert item.url is None
    assert item.filename == "alain.png"
    assert item.content_type == "image/png"
    assert item.has_bytes is True
    assert item.alt_text == ""


def test_create_upload_rejects_non_png_jpeg(conn):
    with pytest.raises(ValueError, match="content_type"):
        create_upload(
            conn,
            kind="logo",
            filename="x.gif",
            content_type="image/gif",
            content_bytes=b"GIF89a",
        )


def test_create_upload_rejects_empty_bytes(conn):
    with pytest.raises(ValueError, match="non-empty"):
        create_upload(
            conn,
            kind="logo",
            filename="x.png",
            content_type="image/png",
            content_bytes=b"",
        )


def test_create_upload_accepts_jpeg(conn):
    item = create_upload(
        conn,
        kind="image",
        filename="banner.jpg",
        content_type="image/jpeg",
        content_bytes=JPG_BYTES,
    )
    assert item.content_type == "image/jpeg"
    assert item.kind == "image"


def test_update_alt_text_sets_description(conn):
    item = upsert_by_url(conn, url="https://example.com/z.png", kind="logo")
    updated = update_alt_text(conn, item.id, "Sponsor logo for Acme Corp.")
    assert updated is not None
    assert updated.alt_text == "Sponsor logo for Acme Corp."
    assert updated.is_usable() is True


def test_unusable_when_alt_text_empty(conn):
    item = upsert_by_url(conn, url="https://example.com/q.png", kind="logo")
    assert item.is_usable() is False


def test_update_alt_text_returns_none_for_missing(conn):
    assert update_alt_text(conn, 9999, "hi") is None


def test_mark_ready_caches_bytes_and_clears_error(conn):
    item = upsert_by_url(conn, url="https://example.com/p.png", kind="image")
    mark_failed(conn, item.id, error="timeout")
    ready = mark_ready(
        conn, item.id, content_type="image/png", content_bytes=PNG_BYTES
    )
    assert ready is not None
    assert ready.status == "ready"
    assert ready.content_type == "image/png"
    assert ready.has_bytes is True
    assert ready.error is None


def test_mark_ready_rejects_non_png_jpeg(conn):
    item = upsert_by_url(conn, url="https://example.com/r.png", kind="logo")
    with pytest.raises(ValueError):
        mark_ready(
            conn, item.id, content_type="image/webp", content_bytes=b"webp"
        )


def test_mark_failed_records_error(conn):
    item = upsert_by_url(conn, url="https://example.com/f.png", kind="image")
    failed = mark_failed(conn, item.id, error="404 Not Found")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "404 Not Found"
    assert failed.has_bytes is False


def test_mark_failed_truncates_long_error(conn):
    item = upsert_by_url(conn, url="https://example.com/l.png", kind="image")
    failed = mark_failed(conn, item.id, error="x" * 1000)
    assert failed is not None
    assert len(failed.error or "") == 500


def test_get_media_bytes_returns_none_when_not_cached(conn):
    item = upsert_by_url(conn, url="https://example.com/n.png", kind="image")
    assert get_media_bytes(conn, item.id) is None


def test_get_media_bytes_returns_payload_when_cached(conn):
    item = create_upload(
        conn,
        kind="logo",
        filename="a.png",
        content_type="image/png",
        content_bytes=PNG_BYTES,
    )
    payload = get_media_bytes(conn, item.id)
    assert payload is not None
    body, ctype = payload
    assert body == PNG_BYTES
    assert ctype == "image/png"


def test_list_media_orders_by_updated_at_desc(conn):
    a = upsert_by_url(conn, url="https://example.com/a.png", kind="logo")
    b = upsert_by_url(conn, url="https://example.com/b.png", kind="image")
    update_alt_text(conn, a.id, "touch a so it sorts first")
    items = list_media(conn)
    assert [i.id for i in items[:2]] == [a.id, b.id]


def test_delete_media_removes_row(conn):
    item = upsert_by_url(conn, url="https://example.com/d.png", kind="logo")
    assert delete_media(conn, item.id) is True
    assert get_media(conn, item.id) is None
    assert delete_media(conn, item.id) is False


def test_url_uniqueness_enforced_at_db_level(conn):
    upsert_by_url(conn, url="https://example.com/u.png", kind="logo")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO media (kind, url, status, alt_text, created_at, updated_at) "
            "VALUES ('logo', 'https://example.com/u.png', 'pending', '', '', '')"
        )


def test_media_kind_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO media (kind, status, alt_text, created_at, updated_at) "
            "VALUES ('banner', 'pending', '', '', '')"
        )


def test_media_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO media (kind, status, alt_text, created_at, updated_at) "
            "VALUES ('logo', 'archived', '', '', '')"
        )


# ───────── Magic-byte sniffer ────────────────────────────────────────


def test_sniff_image_kind_recognizes_png():
    assert sniff_image_kind(PNG_BYTES) == "image/png"


def test_sniff_image_kind_recognizes_jpeg():
    assert sniff_image_kind(JPG_BYTES) == "image/jpeg"


def test_sniff_image_kind_rejects_gif_signature():
    assert sniff_image_kind(b"GIF89a" + b"\x00" * 10) is None


def test_sniff_image_kind_rejects_html_payload():
    assert sniff_image_kind(b"<!doctype html><html>...") is None


def test_sniff_image_kind_handles_empty_payload():
    assert sniff_image_kind(b"") is None


def test_sniff_image_kind_only_reads_head():
    """Sniff must be cheap — reading the first few bytes is enough."""
    payload = PNG_BYTES + b"\x00" * 10_000_000  # 10 MiB tail
    # Doesn't blow up + still classifies correctly.
    assert sniff_image_kind(payload[:8]) == "image/png"
