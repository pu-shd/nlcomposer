"""HTTP-level tests for the Media Library REST controller."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-but-non-empty-bytes"
JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIFcontent"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _upload_logo(client: TestClient, filename: str = "alain.png") -> dict:
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": (filename, io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_list_empty_returns_200(client: TestClient):
    res = client.get("/api/media")
    assert res.status_code == 200
    assert res.json() == []


def test_create_by_url_inserts_pending_row(client: TestClient):
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/sponsor.png"},
    )
    assert res.status_code == 201, res.text
    item = res.json()
    assert item["kind"] == "logo"
    assert item["url"].rstrip("/") == "https://example.com/sponsor.png"
    assert item["status"] == "pending"
    assert item["alt_text"] == ""
    assert item["has_bytes"] is False


def test_create_by_url_is_idempotent(client: TestClient):
    a = client.post(
        "/api/media",
        json={"kind": "image", "url": "https://example.com/a.png"},
    ).json()
    b = client.post(
        "/api/media",
        json={"kind": "image", "url": "https://example.com/a.png"},
    ).json()
    assert a["id"] == b["id"]
    # First reference wins on kind even if a later post differs.
    c = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/a.png"},
    ).json()
    assert c["kind"] == "image"


def test_create_by_url_unwraps_safelinks(client: TestClient):
    """Sanity check that SanitizedHttpUrl runs at the model boundary."""
    safelink = (
        "https://nam04.safelinks.protection.outlook.com/?url=https%3A%2F%2F"
        "example.com%2Fsafe.png&data=foo"
    )
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": safelink},
    )
    assert res.status_code == 201, res.text
    assert "safelinks" not in res.json()["url"]
    assert "example.com/safe.png" in res.json()["url"]


def test_create_by_url_rejects_non_http_url(client: TestClient):
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "ftp://example.com/x.png"},
    )
    assert res.status_code == 422


def test_upload_accepts_png(client: TestClient):
    item = _upload_logo(client)
    assert item["status"] == "ready"
    assert item["url"] is None
    assert item["filename"] == "alain.png"
    assert item["content_type"] == "image/png"
    assert item["has_bytes"] is True


def test_upload_accepts_jpeg(client: TestClient):
    res = client.post(
        "/api/media/upload",
        data={"kind": "image"},
        files={"file": ("banner.jpg", io.BytesIO(JPG_BYTES), "image/jpeg")},
    )
    assert res.status_code == 201, res.text
    item = res.json()
    assert item["content_type"] == "image/jpeg"
    assert item["kind"] == "image"


def test_upload_rejects_non_png_jpeg(client: TestClient):
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("x.gif", io.BytesIO(b"GIF89a"), "image/gif")},
    )
    assert res.status_code == 415


def test_upload_rejects_empty_body(client: TestClient):
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("empty.png", io.BytesIO(b""), "image/png")},
    )
    assert res.status_code == 400


def test_get_one_returns_item(client: TestClient):
    item = _upload_logo(client)
    res = client.get(f"/api/media/{item['id']}")
    assert res.status_code == 200
    assert res.json()["id"] == item["id"]


def test_get_one_404_for_missing(client: TestClient):
    res = client.get("/api/media/9999")
    assert res.status_code == 404


def test_put_alt_text_marks_usable(client: TestClient):
    item = _upload_logo(client)
    res = client.put(
        f"/api/media/{item['id']}",
        json={"alt_text": "Sponsor logo for Acme."},
    )
    assert res.status_code == 200
    assert res.json()["alt_text"] == "Sponsor logo for Acme."


def test_put_alt_text_404_for_missing(client: TestClient):
    res = client.put("/api/media/9999", json={"alt_text": "hi"})
    assert res.status_code == 404


def test_delete_removes_item(client: TestClient):
    item = _upload_logo(client)
    res = client.delete(f"/api/media/{item['id']}")
    assert res.status_code == 204
    assert client.get(f"/api/media/{item['id']}").status_code == 404


def test_delete_404_for_missing(client: TestClient):
    res = client.delete("/api/media/9999")
    assert res.status_code == 404


def test_content_streams_uploaded_bytes(client: TestClient):
    item = _upload_logo(client)
    res = client.get(f"/api/media/{item['id']}/content")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/png")
    assert res.content == PNG_BYTES
    assert "max-age" in res.headers.get("cache-control", "")


def test_content_404_when_pending(client: TestClient):
    """URL-sourced rows have no bytes until the background fetcher runs."""
    item = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/pending.png"},
    ).json()
    res = client.get(f"/api/media/{item['id']}/content")
    assert res.status_code == 404


def test_content_404_for_missing_id(client: TestClient):
    res = client.get("/api/media/9999/content")
    assert res.status_code == 404


def test_list_returns_recently_updated_first(client: TestClient):
    a = client.post(
        "/api/media", json={"kind": "logo", "url": "https://example.com/a.png"}
    ).json()
    b = client.post(
        "/api/media", json={"kind": "image", "url": "https://example.com/b.png"}
    ).json()
    # Touch a so it sorts first.
    client.put(f"/api/media/{a['id']}", json={"alt_text": "touch"})
    items = client.get("/api/media").json()
    assert [i["id"] for i in items[:2]] == [a["id"], b["id"]]
