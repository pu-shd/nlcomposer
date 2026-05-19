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
    <meta property="article:published_time" content="2026-04-27T08:30:00Z">
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
    assert md["date_text"] == "Apr 27"
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
    assert "date_text" not in md
    assert "pull_quote" not in md
    assert md["publication_logo_url"] == "https://example.com/favicon.ico"


JSONLD_HTML = """
<!doctype html>
<html><head>
  <title>JSON-LD page</title>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@graph": [
      {"@type": "WebSite", "name": "Example"},
      {
        "@type": "NewsArticle",
        "headline": "JSON-LD headline",
        "author": [{"@type": "Person", "name": "Jane Reporter"}],
        "datePublished": "2026-05-03T13:14:00-04:00"
      }
    ]
  }
  </script>
</head><body></body></html>
"""


def test_parse_jsonld_supplies_byline_and_date():
    md = parse_article_metadata(JSONLD_HTML, "https://example.com/jsonld")
    assert md["byline"] == "Jane Reporter"
    assert md["date_text"] == "May 3"


TIME_TAG_HTML = """
<!doctype html>
<html><head>
  <title>Time-tag page</title>
  <meta name="author" content="A. Writer">
</head><body>
  <article>
    <time datetime="2026-01-09">January 9, 2026</time>
    <p>...</p>
  </article>
</body></html>
"""


def test_parse_time_tag_used_when_no_meta_date():
    md = parse_article_metadata(TIME_TAG_HTML, "https://example.com/time")
    assert md["byline"] == "A. Writer"
    assert md["date_text"] == "Jan 9"


URL_BYLINE_HTML = """
<!doctype html>
<html><head>
  <title>Profile-URL byline</title>
  <meta property="article:author" content="https://example.com/authors/jane">
  <meta name="byl" content="Jane Reporter">
</head><body></body></html>
"""


def test_parse_drops_url_byline_in_favor_of_text():
    md = parse_article_metadata(URL_BYLINE_HTML, "https://example.com/u")
    # The article:author is a profile URL — must not become the byline.
    assert md["byline"] == "Jane Reporter"


BAD_DATE_HTML = """
<!doctype html>
<html><head>
  <title>Bad date</title>
  <meta property="article:published_time" content="not a date at all">
</head><body></body></html>
"""


def test_parse_unparseable_date_omitted_silently():
    md = parse_article_metadata(BAD_DATE_HTML, "https://example.com/x")
    assert "date_text" not in md


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


def test_endpoint_returns_502_with_actionable_detail_on_upstream_5xx(monkeypatch):
    """5xx from the upstream should carry the status code AND tell the
    user to try again later — bare ``upstream returned 503`` is unhelpful
    in the article-card status line where the SPA renders it verbatim."""
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article", params={"url": "https://example.com/down"}
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "503" in detail
    assert "try again later" in detail.lower() or "down or misbehaving" in detail.lower()


def test_endpoint_explains_403_with_anti_bot_guidance(monkeypatch):
    """403 from a Cloudflare-fronted site (Substack et al.) — the editor
    needs to know to enter the article fields by hand rather than retry
    the same URL endlessly."""
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article",
        params={"url": "https://pandaperspectives.substack.com/p/x"},
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "refused" in detail.lower() or "anti-bot" in detail.lower()
    assert "by hand" in detail.lower()


def test_endpoint_returns_502_on_network_failure(monkeypatch):
    """DNS / connection errors get a clear non-technical reason string."""
    def fake_fetch_html(url, *, timeout=8.0, user_agent=None, client=None):
        raise httpx.ConnectError("dns lookup failed")

    monkeypatch.setattr("app.fetch._fetch_html", fake_fetch_html)
    res = client.get(
        "/api/fetch/article", params={"url": "https://example.invalid/x"}
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "could not reach" in detail.lower() or "connection" in detail.lower()

def test_fetch_youtube_falls_back_to_scraping_on_api_failure(monkeypatch):
    """If the YouTube API is unconfigured (RuntimeError), it falls back to scraping."""
    from app.podcast import YoutubeMetadata
    def fake_fetch_video_details(video_id):
        raise RuntimeError("YouTube API is not configured")
        
    def fake_fetch_youtube_metadata(url):
        return {
            "title": "Scraped Title",
            "summary": "Scraped Summary",
            "youtube_video_url": url,
            "timecodes": []
        }
        
    monkeypatch.setattr("app.controllers.fetch.fetch_video_details", fake_fetch_video_details)
    monkeypatch.setattr("app.controllers.fetch.fetch_youtube_metadata", fake_fetch_youtube_metadata)
    
    res = client.get("/api/fetch/youtube", params={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert res.status_code == 200
    assert res.json()["title"] == "Scraped Title"
