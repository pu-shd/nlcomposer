"""HTTP-level tests for the Media Library REST controller."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rate_limit import upload_limiter


PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-but-non-empty-bytes"
JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIFcontent"


@pytest.fixture(autouse=True)
def _reset_upload_limiter():
    """Tests share the process-wide upload limiter; clear its bucket
    between cases so an earlier test's hits don't trip later asserts."""
    upload_limiter.reset()
    yield
    upload_limiter.reset()


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


def test_content_on_demand_fetch_success(client: TestClient, monkeypatch):
    """Missing bytes are fetched synchronously if a URL is available."""
    # 1. Insert a pending row
    item = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/ondemand.png"},
    ).json()

    # 2. Mock httpx.Client.get surgically.
    # We can't mock the whole class because TestClient uses it.
    # We'll mock the 'send' method or just the 'get' on a per-instance basis
    # if we can, but monkeypatching the class is easier if we filter.
    import httpx
    original_get = httpx.Client.get

    def mock_get(self, url, *args, **kwargs):
        if str(url) == "https://example.com/ondemand.png":
            class MockResponse:
                def __init__(self):
                    self.content = PNG_BYTES
                    self.status_code = 200
                def raise_for_status(self):
                    pass
            return MockResponse()
        return original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", mock_get)

    # 3. GET /content should trigger the fetch and return the bytes
    res = client.get(f"/api/media/{item['id']}/content")
    assert res.status_code == 200
    assert res.content == PNG_BYTES
    assert res.headers["content-type"] == "image/png"

    # 4. Verify the DB row is now 'ready'
    updated = client.get(f"/api/media/{item['id']}").json()
    assert updated["status"] == "ready"
    assert updated["has_bytes"] is True


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


# ───────── Configurable upload gating ─────────────────────────────────


def _put_settings(client: TestClient, **overrides) -> None:
    """Helper: PUT current settings with overrides applied. Re-fetches
    so we don't drop unrelated fields like newsletter_name."""
    current = client.get("/api/settings").json()
    current.update(overrides)
    res = client.put("/api/settings", json=current)
    assert res.status_code == 200, res.text


def test_upload_rejects_payload_above_configured_max_bytes(client: TestClient):
    _put_settings(client, media_upload_max_bytes=1024)
    big = PNG_BYTES + b"\x00" * 2048
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("big.png", io.BytesIO(big), "image/png")},
    )
    assert res.status_code == 413
    assert "exceeds" in res.json()["detail"]


def test_upload_rejects_when_magic_bytes_disagree_with_content_type(
    client: TestClient,
):
    """A caller claiming image/png but sending HTML must not slip through."""
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={
            "file": (
                "spoof.png",
                io.BytesIO(b"<html><body>not an image</body></html>"),
                "image/png",
            ),
        },
    )
    assert res.status_code == 415
    assert "match" in res.json()["detail"].lower()


def test_upload_rejects_jpg_bytes_declared_as_png(client: TestClient):
    """Even if the bytes ARE a real image, declared MIME mismatching the
    sniffed format is rejected — keeps the persisted content_type honest."""
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("x.png", io.BytesIO(JPG_BYTES), "image/png")},
    )
    assert res.status_code == 415
    assert "mismatch" in res.json()["detail"].lower()


def test_upload_rate_limit_returns_429_with_retry_after(client: TestClient):
    _put_settings(client, media_upload_rate_per_minute=2)
    # First two uploads succeed.
    for i in range(2):
        res = client.post(
            "/api/media/upload",
            data={"kind": "logo"},
            files={"file": (f"a{i}.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert res.status_code == 201, res.text
    # Third trips the limiter.
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("c.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert res.status_code == 429
    assert "Retry-After" in res.headers
    assert int(res.headers["Retry-After"]) >= 1


def test_upload_rate_limit_disabled_when_zero(client: TestClient):
    _put_settings(client, media_upload_rate_per_minute=0)
    # Hit far more than any nonzero default — none should 429.
    for i in range(5):
        res = client.post(
            "/api/media/upload",
            data={"kind": "logo"},
            files={"file": (f"x{i}.png", io.BytesIO(PNG_BYTES), "image/png")},
        )
        assert res.status_code == 201


def test_upload_respects_configured_allowed_types(client: TestClient):
    """Removing image/jpeg from the allowlist makes JPEG uploads fail."""
    _put_settings(client, media_upload_allowed_types=["image/png"])
    res = client.post(
        "/api/media/upload",
        data={"kind": "image"},
        files={"file": ("banner.jpg", io.BytesIO(JPG_BYTES), "image/jpeg")},
    )
    assert res.status_code == 415


def test_url_imports_disabled_returns_403(client: TestClient):
    _put_settings(client, media_url_imports_enabled=False)
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/x.png"},
    )
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"].lower()


def test_url_imports_enabled_default_works(client: TestClient):
    """Default-enabled case: POST /api/media still 201s without setup."""
    res = client.post(
        "/api/media",
        json={"kind": "logo", "url": "https://example.com/default.png"},
    )
    assert res.status_code == 201


def test_upload_persists_sniffed_type_not_declared_one(client: TestClient):
    """When declared and sniffed agree, the persisted content_type comes
    from the sniff — so the row's metadata reflects ground truth."""
    res = client.post(
        "/api/media/upload",
        data={"kind": "logo"},
        files={"file": ("ok.png", io.BytesIO(PNG_BYTES), "image/png")},
    )
    assert res.status_code == 201
    assert res.json()["content_type"] == "image/png"
