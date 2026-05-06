"""Scrape YouTube metadata so the composer can pre-fill a PodcastEpisode.

Given a YouTube watch URL we fetch the oEmbed endpoint for the episode
title and the watch page HTML for the full description, then parse
``MM:SS`` / ``H:MM:SS`` lines from the description into deep-link
timecode bullets pointing back into the source video at each offset.

The HTTP layer and the parsers live in separate functions so tests can
exercise the parsers with fixture strings and stub the network call
without touching the public Internet — same pattern as :mod:`app.fetch`.
"""

from __future__ import annotations

import json
import re
from typing import Optional, TypedDict
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .links import sanitize_url

_DEFAULT_TIMEOUT = 8.0
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; sdc-composer/0.1; "
    "+https://smartdrivingcar.com/)"
)
_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"


class PodcastTimecodeDict(TypedDict):
    timecode: str
    label: str
    url: str


class YoutubeMetadata(TypedDict, total=False):
    title: str
    summary: str
    youtube_video_url: str
    timecodes: list[PodcastTimecodeDict]


def extract_video_id(url: str) -> Optional[str]:
    """Pull the 11-ish-char video ID out of any common YouTube URL shape.

    Supports ``watch?v=…``, ``youtu.be/…``, ``shorts/…``, ``embed/…``,
    ``v/…``, and ``live/…``. Returns ``None`` if no ID is recognizable.
    """

    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return None
    host = (parsed.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        path_id = parsed.path.lstrip("/").split("/", 1)[0]
        return path_id or None
    if "youtube.com" not in host and "youtube-nocookie.com" not in host:
        return None
    if parsed.path == "/watch":
        qs = parse_qs(parsed.query)
        v = qs.get("v")
        if v and v[0]:
            return v[0]
    parts = parsed.path.lstrip("/").split("/", 2)
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "v", "live"}:
        return parts[1] or None
    return None


def validate_youtube_url(url: str) -> str:
    """Reject anything that doesn't yield a YouTube video ID.

    Also blocks file/ftp/loopback URLs the same way :func:`app.fetch.validate_fetch_url`
    does so the endpoint cannot be turned into an SSRF probe by accident.
    """

    cleaned = sanitize_url(url.strip()) or ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are supported")
    if not parsed.netloc:
        raise ValueError("URL is missing a host")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(
        ".localhost"
    ):
        raise ValueError("URL host is not allowed")
    if not extract_video_id(cleaned):
        raise ValueError("URL is not a recognizable YouTube video URL")
    return cleaned


# ───────── Timecode parsing ───────────────────────────────────────────────

# Anchor at start-of-line so we don't collide with timecodes embedded mid-
# sentence ("at 3:30 they discuss…"). Allow common bullet/dash prefixes.
# Matches H:MM:SS or M:SS / MM:SS, then a separator, then a non-empty label.
_TIMECODE_LINE = re.compile(
    r"""
    ^[ \t]*[\-•*–>]*[ \t]*
    (?P<tc>(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>\d{2}))
    [ \t\-:–—]+
    (?P<label>\S[^\n]*?)[ \t]*$
    """,
    re.MULTILINE | re.VERBOSE,
)


def _timecode_to_seconds(tc: str) -> int:
    parts = [int(p) for p in tc.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        return 0
    return h * 3600 + m * 60 + s


def _strip_label(label: str) -> str:
    return label.strip().strip("–—-:  ").strip()


def parse_timecodes(description: str, video_id: str) -> list[PodcastTimecodeDict]:
    """Extract ``MM:SS`` / ``H:MM:SS`` timecode lines from a YT description.

    Each match becomes a dict with ``timecode`` (as printed), ``label`` (text
    after the timecode, with bullets/dashes trimmed), and ``url`` (a deep
    link into the video at that offset). Order is preserved.
    """

    out: list[PodcastTimecodeDict] = []
    seen: set[tuple[str, str]] = set()
    for match in _TIMECODE_LINE.finditer(description):
        tc = match.group("tc")
        label = _strip_label(match.group("label"))
        if not label:
            continue
        # Drop adjacent-line dupes (some descriptions repeat a timecode).
        key = (tc, label.lower())
        if key in seen:
            continue
        seen.add(key)
        seconds = _timecode_to_seconds(tc)
        out.append(
            {
                "timecode": tc,
                "label": label,
                "url": (
                    f"https://www.youtube.com/watch?v={video_id}&t={seconds}s"
                ),
            }
        )
    return out


def strip_timecode_lines(description: str) -> str:
    """Return the description with timecode-only lines removed.

    Useful for populating the podcast ``summary`` so the timecodes don't
    show up twice in the rendered email (once as bullets, once embedded).
    Leading/trailing blank lines and runs of >2 blank lines collapse.
    """

    kept: list[str] = []
    for line in description.splitlines():
        if _TIMECODE_LINE.match(line):
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ───────── oEmbed + watch-page parsers ────────────────────────────────────


def parse_oembed(payload: dict) -> dict:
    """Pull the title (and only the title) from a YouTube oEmbed response."""

    out: dict = {}
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        out["title"] = title.strip()
    return out


# YouTube embeds the full description inside a JSON blob assigned to
# ``ytInitialPlayerResponse``. The ``shortDescription`` field carries the
# user-facing description with newlines escaped as ``\n``. We pull it out
# with a forgiving regex (it may appear before *or* after other fields).
_DESC_RX = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')


def parse_watch_page_description(html: str) -> Optional[str]:
    """Extract ``videoDetails.shortDescription`` from a YouTube watch page.

    Falls back to the ``og:description`` meta tag (truncated by YouTube,
    but better than nothing) if the JSON blob isn't present — happens with
    consent-wall variants.
    """

    match = _DESC_RX.search(html)
    if match is not None:
        raw = match.group(1)
        try:
            return json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            return raw.encode("utf-8").decode("unicode_escape", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:description"})
    if tag and tag.get("content"):
        content = tag["content"]
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


# ───────── Network ────────────────────────────────────────────────────────


def _fetch(
    url: str,
    *,
    client: Optional[httpx.Client],
    timeout: float,
    user_agent: str,
) -> tuple[str, str]:
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if client is not None:
        r = client.get(url, headers=headers, follow_redirects=True, timeout=timeout)
        r.raise_for_status()
        return r.text, str(r.url)
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


def fetch_youtube_metadata(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
) -> YoutubeMetadata:
    """High-level helper: validate, fetch oEmbed + watch page, parse.

    Returns a partial dict with whatever could be populated. The watch
    page is required (it carries the description); the oEmbed call is
    nice-to-have for the title and is allowed to fail silently.
    """

    safe = validate_youtube_url(url)
    video_id = extract_video_id(safe)
    if video_id is None:  # pragma: no cover — validate guarantees this
        raise ValueError("URL is not a recognizable YouTube video URL")

    out: YoutubeMetadata = {
        "youtube_video_url": f"https://www.youtube.com/watch?v={video_id}",
    }

    oembed_url = f"{_OEMBED_ENDPOINT}?url={safe}&format=json"
    try:
        oembed_text, _ = _fetch(
            oembed_url,
            client=client,
            timeout=timeout,
            user_agent=_DEFAULT_USER_AGENT,
        )
        try:
            payload = json.loads(oembed_text)
            parsed = parse_oembed(payload)
            if "title" in parsed:
                out["title"] = parsed["title"]
        except json.JSONDecodeError:
            pass
    except httpx.HTTPError:
        # Title is a nice-to-have; the description fetch is what matters.
        pass

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    html, _ = _fetch(
        watch_url,
        client=client,
        timeout=timeout,
        user_agent=_DEFAULT_USER_AGENT,
    )
    description = parse_watch_page_description(html)
    if description:
        out["summary"] = strip_timecode_lines(description)
        out["timecodes"] = parse_timecodes(description, video_id)
    else:
        out["timecodes"] = []

    return out
