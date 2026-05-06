"""Tests for the article-metadata fetcher.

The HTTP layer is mocked with ``httpx.MockTransport`` so the suite never
touches the public Internet. The HTML parser is exercised against fixture
strings that mirror real-world variants we expect to see (Substack-style
og:tags, NYT-style meta tags, sites with no icon link at all).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.fetch import (
    fetch_article_metadata,
    parse_article_metadata,
    validate_fetch_url,
)
from app.main import app


client = TestClient(app)


SUBSTACK_HTML = """
<!doctype html>
<html>
  <head>
    <title>Beijing Auto Show 2026 — Panda Perspectives</title>
    <meta property="og:title" content="Beijing Auto Show 2026: A Field Report">
    <meta property="og:description" content="Sore feet, a dead phone, and the cleanest single-day read on the Chinese auto industry we have had in years.">
    <meta name="author" content="P. Perspective">
    <link rel="apple-touch-icon" href="/favicon-180.png">
    <link rel="icon" href="/favicon.ico">
  </head>
  <body><h1>Article body</h1></body>
</html>
"""

NYT_HTML = """
<!doctype html>
<html>
  <head>
    <title>Why Are We Still Driving? - The New York Times</title>
    <meta property="og:title" content="Why Are We Still Driving?">
    <meta name="description" content="Waymo is here. Why aren't we using it more?">
    <meta name="byl" content="By Andrew Miller">
    <link rel="shortcut icon" href="https://www.nytimes.com/vi-assets/static-assets/favicon.ico">
  </head>
  <body></body>
</html>
"""

BARE_HTML = """
<!doctype html>
<html><head><title>Plain Page</title></head><body></body></html>
"""


def test_parse_substack_style():
    md = parse_article_metadata(
        SUBSTACK_HTML, "https://pandaperspectives.substack.com/p/beijing"
    )
    assert md["headline"] == "Beijing Auto Show 2026: A Field Report"
    assert md["byline"] == "P. Perspective"
    assert "Sore feet" in md["pull_quote"]
    # apple-touch-icon wins over icon, and relative path resolves:
    assert (
        md["publication_logo_url"]
        == "https://pandaperspectives.substack.com/favicon-180.png"
    )
    assert (
        md["read_more_url"]
        == "https://pandaperspectives.substack.com/p/beijing"
    )


def test_parse_nyt_style_uses_meta_byl_and_absolute_icon():
    md = parse_article_metadata(NYT_HTML, "https://www.nytimes.com/some/article")
    assert md["headline"] == "Why Are We Still Driving?"
    assert md["byline"] == "By Andrew Miller"
    assert "Waymo is here" in md["pull_quote"]
    assert (
        md["publication_logo_url"]
        == "https://www.nytimes.com/vi-assets/static-assets/favicon.ico"
    )


def test_parse_bare_page_falls_back_to_title_and_default_favicon():
    md = parse_article_metadata(BARE_HTML, "https://example.com/posts/1")
    assert md["headline"] == "Plain Page"
    assert "byline" not in md
    assert "pull_quote" not in md
    assert md["publication_logo_url"] == "https://example.com/favicon.ico"


def test_validate_fetch_url_rejects_non_http():
    with pytest.raises(ValueError):
        validate_fetch_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        validate_fetch_url("ftp://example.com/a")


def test_validate_fetch_url_rejects_loopback():
    with pytest.raises(ValueError):
        validate_fetch_url("http://localhost:8000/foo")
    with pytest.raises(ValueError):
        validate_fetch_url("http://127.0.0.1/")


def test_validate_fetch_url_strips_whitespace():
    assert (
        validate_fetch_url("  https://example.com/a  ")
        == "https://example.com/a"
    )


def test_fetch_article_metadata_uses_injected_client():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "pandaperspectives.substack.com"
        return httpx.Response(200, text=SUBSTACK_HTML)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as c:
        md = fetch_article_metadata(
            "https://pandaperspectives.substack.com/p/beijing", client=c
        )
    assert md["headline"] == "Beijing Auto Show 2026: A Field Report"
    assert md["byline"] == "P. Perspective"


def test_endpoint_returns_metadata(monkeypatch):
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        assert url == "https://pandaperspectives.substack.com/p/beijing"
        return SUBSTACK_HTML, url

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article",
        params={"url": "https://pandaperspectives.substack.com/p/beijing"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["headline"] == "Beijing Auto Show 2026: A Field Report"
    assert data["byline"] == "P. Perspective"
    assert "publication_logo_url" in data
    assert (
        data["read_more_url"]
        == "https://pandaperspectives.substack.com/p/beijing"
    )


def test_endpoint_rejects_loopback_with_400():
    res = client.get(
        "/api/fetch/article", params={"url": "http://localhost/foo"}
    )
    assert res.status_code == 400
    assert "not allowed" in res.json()["detail"]


def test_endpoint_rejects_non_http_with_400():
    res = client.get(
        "/api/fetch/article", params={"url": "file:///etc/hosts"}
    )
    assert res.status_code == 400


def test_endpoint_returns_502_on_upstream_failure(monkeypatch):
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article", params={"url": "https://example.com/down"}
    )
    assert res.status_code == 502
    assert "503" in res.json()["detail"]


def test_endpoint_returns_502_on_network_failure(monkeypatch):
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        raise httpx.ConnectError("dns lookup failed")

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article", params={"url": "https://example.invalid/x"}
    )
    assert res.status_code == 502
    assert "fetch failed" in res.json()["detail"]
