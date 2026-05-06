"""Tests for the YouTube/podcast metadata fetcher.

The HTTP layer is mocked with ``httpx.MockTransport`` so the suite never
talks to YouTube. The parsers are exercised against fixture strings
covering: a typical SDC ZoomCast description with a numbered timecode
list, a description with H:MM:SS timecodes, and a description with no
timecodes at all.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.podcast import (
    extract_video_id,
    fetch_youtube_metadata,
    parse_oembed,
    parse_timecodes,
    parse_watch_page_description,
    strip_timecode_lines,
    validate_youtube_url,
)


client = TestClient(app)


SDC_DESCRIPTION = (
    "Smart Driving Cars episode 412 with Newsletter Editor and the panel.\n"
    "We talk Beijing Auto Show takeaways, Waymo growth, and the FSD chart.\n"
    "\n"
    "0:00 Intro and hellos\n"
    "0:50 - Beijing Auto Show field report\n"
    "5:32 — Pony.ai's IPO\n"
    "17:35: Waymo ride volume\n"
    "1:02:14 Closing thoughts\n"
    "\n"
    "Subscribe at smartdrivingcar.com.\n"
)


def _watch_html(short_description: str) -> str:
    """Build a tiny fake watch page that embeds shortDescription as JSON."""

    encoded = json.dumps(short_description)[1:-1]  # strip surrounding quotes
    return (
        "<!doctype html><html><head>"
        '<meta property="og:description" content="(truncated)"></head>'
        f'<body><script>var x = {{"videoDetails":{{"shortDescription":"{encoded}"}}}};</script>'
        "</body></html>"
    )


# ───────── extract_video_id ──────────────────────────────────────────────


def test_extract_video_id_watch():
    assert extract_video_id("https://www.youtube.com/watch?v=abc123XYZ_-") == "abc123XYZ_-"


def test_extract_video_id_short_url():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"


def test_extract_video_id_shorts_path():
    assert extract_video_id("https://www.youtube.com/shorts/abcDEF12345") == "abcDEF12345"


def test_extract_video_id_embed_path():
    assert extract_video_id("https://www.youtube.com/embed/zzzz1111aaa") == "zzzz1111aaa"


def test_extract_video_id_returns_none_for_non_youtube():
    assert extract_video_id("https://example.com/watch?v=abc") is None
    assert extract_video_id("not a url") is None


# ───────── validate_youtube_url ──────────────────────────────────────────


def test_validate_youtube_url_accepts_watch():
    assert (
        validate_youtube_url("  https://www.youtube.com/watch?v=abc12345678  ")
        == "https://www.youtube.com/watch?v=abc12345678"
    )


def test_validate_youtube_url_rejects_non_youtube():
    with pytest.raises(ValueError):
        validate_youtube_url("https://example.com/watch?v=abc")


def test_validate_youtube_url_rejects_loopback():
    with pytest.raises(ValueError):
        validate_youtube_url("http://localhost/watch?v=abc")


def test_validate_youtube_url_rejects_non_http():
    with pytest.raises(ValueError):
        validate_youtube_url("file:///etc/passwd")


# ───────── parse_timecodes ───────────────────────────────────────────────


def test_parse_timecodes_extracts_mmss_and_hmmss():
    out = parse_timecodes(SDC_DESCRIPTION, "abc12345678")
    timecodes = [t["timecode"] for t in out]
    labels = [t["label"] for t in out]
    assert "0:00" in timecodes
    assert "0:50" in timecodes
    assert "5:32" in timecodes
    assert "17:35" in timecodes
    assert "1:02:14" in timecodes
    # Label cleanup: trailing dashes/colons trimmed
    assert "Beijing Auto Show field report" in labels
    assert "Pony.ai's IPO" in labels
    assert "Waymo ride volume" in labels


def test_parse_timecodes_builds_correct_url_with_offset_seconds():
    out = parse_timecodes(SDC_DESCRIPTION, "abc12345678")
    by_tc = {t["timecode"]: t["url"] for t in out}
    # 0:50 -> 50s
    assert by_tc["0:50"].endswith("&t=50s")
    # 5:32 -> 332s
    assert by_tc["5:32"].endswith("&t=332s")
    # 17:35 -> 1055s
    assert by_tc["17:35"].endswith("&t=1055s")
    # 1:02:14 -> 3734s
    assert by_tc["1:02:14"].endswith("&t=3734s")
    assert "https://www.youtube.com/watch?v=abc12345678" in by_tc["0:50"]


def test_parse_timecodes_skips_lines_without_label():
    desc = "0:50\n1:23 actual chapter\n"
    out = parse_timecodes(desc, "vid")
    assert len(out) == 1
    assert out[0]["label"] == "actual chapter"


def test_parse_timecodes_ignores_inline_times():
    # Mid-sentence times like "at 3:30 they discuss" must NOT match because
    # the regex anchors at start-of-line (after whitespace/bullets only).
    desc = "We talked at length and at 3:30 they discuss the chart.\n"
    out = parse_timecodes(desc, "vid")
    assert out == []


def test_parse_timecodes_dedupes_repeated_lines():
    desc = "0:50 Intro\n0:50 Intro\n1:23 Next\n"
    out = parse_timecodes(desc, "vid")
    timecodes = [t["timecode"] for t in out]
    assert timecodes == ["0:50", "1:23"]


# ───────── strip_timecode_lines ──────────────────────────────────────────


def test_strip_timecode_lines_removes_chapter_block():
    cleaned = strip_timecode_lines(SDC_DESCRIPTION)
    assert "0:50" not in cleaned
    assert "17:35" not in cleaned
    assert "Smart Driving Cars episode 412" in cleaned
    assert "Subscribe at smartdrivingcar.com" in cleaned


# ───────── parse_oembed ──────────────────────────────────────────────────


def test_parse_oembed_picks_title():
    payload = {
        "title": "SDC Episode 412: Beijing & Waymo",
        "author_name": "SmartDrivingCars",
        "type": "video",
    }
    assert parse_oembed(payload) == {"title": "SDC Episode 412: Beijing & Waymo"}


def test_parse_oembed_handles_missing_title():
    assert parse_oembed({}) == {}
    assert parse_oembed({"title": "  "}) == {}


# ───────── parse_watch_page_description ──────────────────────────────────


def test_parse_watch_page_description_pulls_from_initial_payload():
    html = _watch_html(SDC_DESCRIPTION)
    desc = parse_watch_page_description(html)
    assert desc is not None
    assert "Beijing Auto Show field report" in desc
    assert "0:50" in desc


def test_parse_watch_page_description_falls_back_to_og():
    html = (
        "<!doctype html><html><head>"
        '<meta property="og:description" content="Truncated SDC description">'
        "</head><body></body></html>"
    )
    assert parse_watch_page_description(html) == "Truncated SDC description"


def test_parse_watch_page_description_returns_none_when_absent():
    assert parse_watch_page_description("<html><body>nope</body></html>") is None


# ───────── fetch_youtube_metadata (MockTransport) ────────────────────────


def test_fetch_youtube_metadata_uses_injected_client():
    expected_video_id = "abc12345678"

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        assert host == "www.youtube.com"
        if request.url.path == "/oembed":
            payload = {
                "title": "SDC Episode 412",
                "author_name": "SmartDrivingCars",
            }
            return httpx.Response(200, text=json.dumps(payload))
        if request.url.path == "/watch":
            assert request.url.params.get("v") == expected_video_id
            return httpx.Response(200, text=_watch_html(SDC_DESCRIPTION))
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as c:
        meta = fetch_youtube_metadata(
            f"https://www.youtube.com/watch?v={expected_video_id}", client=c
        )

    assert meta["title"] == "SDC Episode 412"
    assert "Beijing Auto Show field report" in [t["label"] for t in meta["timecodes"]]
    assert meta["youtube_video_url"] == (
        f"https://www.youtube.com/watch?v={expected_video_id}"
    )
    # Summary excludes the timecode block but keeps the prose.
    assert "0:50" not in meta["summary"]
    assert "Smart Driving Cars episode 412" in meta["summary"]


def test_fetch_youtube_metadata_survives_oembed_failure():
    expected_video_id = "abc12345678"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oembed":
            return httpx.Response(503, text="busy")
        if request.url.path == "/watch":
            return httpx.Response(200, text=_watch_html(SDC_DESCRIPTION))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as c:
        meta = fetch_youtube_metadata(
            f"https://www.youtube.com/watch?v={expected_video_id}", client=c
        )

    # Title is gone, but description-derived fields are still populated.
    assert "title" not in meta
    assert len(meta["timecodes"]) >= 4


# ───────── /api/fetch/youtube endpoint ──────────────────────────────────


def test_endpoint_returns_youtube_metadata(monkeypatch):
    def fake_fetch(url, *, client=None, timeout=8.0, user_agent=None):
        if "/oembed" in url:
            return json.dumps({"title": "SDC Episode 412"}), url
        return _watch_html(SDC_DESCRIPTION), url

    monkeypatch.setattr("app.podcast._fetch", fake_fetch)
    res = client.get(
        "/api/fetch/youtube",
        params={"url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["title"] == "SDC Episode 412"
    assert data["youtube_video_url"] == "https://www.youtube.com/watch?v=abc12345678"
    assert data["timecodes"]
    assert data["timecodes"][0]["timecode"] in {"0:00", "0:50"}


def test_endpoint_rejects_non_youtube_400():
    res = client.get(
        "/api/fetch/youtube", params={"url": "https://example.com/watch?v=abc"}
    )
    assert res.status_code == 400
    assert "YouTube" in res.json()["detail"]


def test_endpoint_rejects_loopback_400():
    res = client.get(
        "/api/fetch/youtube", params={"url": "http://localhost/watch?v=abc"}
    )
    assert res.status_code == 400


def test_endpoint_502_on_upstream_failure(monkeypatch):
    def fake_fetch(url, *, client=None, timeout=8.0, user_agent=None):
        request = httpx.Request("GET", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr("app.podcast._fetch", fake_fetch)
    res = client.get(
        "/api/fetch/youtube",
        params={"url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    assert res.status_code == 502
    assert "503" in res.json()["detail"]


def test_endpoint_502_on_network_error(monkeypatch):
    def fake_fetch(url, *, client=None, timeout=8.0, user_agent=None):
        raise httpx.ConnectError("dns lookup failed")

    monkeypatch.setattr("app.podcast._fetch", fake_fetch)
    res = client.get(
        "/api/fetch/youtube",
        params={"url": "https://www.youtube.com/watch?v=abc12345678"},
    )
    assert res.status_code == 502
    assert "fetch failed" in res.json()["detail"]
