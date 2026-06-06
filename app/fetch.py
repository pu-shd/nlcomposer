"""Scrape article metadata so the composer can pre-fill an Article entry.

Given an article URL we fetch the page and extract the fields a newsletter
editor would otherwise type by hand: headline (og:title / <title>), byline
(meta author / article:author), pull-quote excerpt (og:description / meta
description), and a small publication logo (link rel=icon, falling back to
``/favicon.ico``). The HTTP fetch and the HTML parsing live in separate
functions so tests can exercise the parser with fixture HTML and stub the
network call without touching the public Internet.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from datetime import date, datetime
from typing import Any, Iterable, Optional, TypedDict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .links import sanitize_url

_DEFAULT_TIMEOUT = 8.0
# Many publisher CDNs (Substack, Cloudflare-fronted news sites) bounce
# any User-Agent that openly self-identifies as automation — they 403
# the request before it ever hits the page. Send a current desktop-
# Chrome UA + the browser-ish headers a real navigation carries so the
# fetcher reaches the same HTML a user pasting into a browser would.
# We are scraping public OpenGraph/JSON-LD that the page is *designed*
# to share, not bypassing any access control, so this stays well inside
# the intended use of those tags.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class ArticleMetadata(TypedDict, total=False):
    """Subset of Article fields the fetcher can populate."""

    headline: str
    byline: str
    date_text: str
    pull_quote: str
    publication_logo_url: str
    read_more_url: str


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _host_resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0] if sockaddr else ""
        if not _is_public_ip(ip):
            return False
    return True


def validate_fetch_url(url: str) -> str:
    """Validate a URL is safe(ish) to fetch from this local tool.

    Rejects non-http(s) schemes, missing hosts, loopback/private targets,
    and hosts that fail DNS resolution to public IPs.
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
    if not _host_resolves_public(host):
        raise ValueError("URL host is not allowed")
    return cleaned


def _meta_content(soup: BeautifulSoup, **attrs: str) -> Optional[str]:
    tag = soup.find("meta", attrs=attrs)
    if tag is None:
        return None
    content = tag.get("content")
    if not content:
        return None
    text = content.strip()
    return text or None


def _first(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v:
            return v
    return None


def _walk_jsonld(payload: Any) -> Iterable[dict]:
    """Yield every dict inside a JSON-LD payload (handles ``@graph`` and lists)."""

    if isinstance(payload, dict):
        yield payload
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _walk_jsonld(item)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_jsonld(item)


def _jsonld_author_name(author: Any) -> Optional[str]:
    """Pull a printable name out of a JSON-LD ``author`` field."""

    if isinstance(author, str):
        return author.strip() or None
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    if isinstance(author, list):
        names: list[str] = []
        for item in author:
            n = _jsonld_author_name(item)
            if n:
                names.append(n)
        if names:
            return ", ".join(names)
    return None


def _parse_jsonld_meta(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    """Scan ``<script type="application/ld+json">`` blocks for author + date."""

    out: dict[str, Optional[str]] = {"author": None, "date": None}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        for entry in _walk_jsonld(payload):
            if not isinstance(entry, dict):
                continue
            if not out["author"] and "author" in entry:
                out["author"] = _jsonld_author_name(entry["author"])
            if not out["date"]:
                d = entry.get("datePublished") or entry.get("dateCreated")
                if isinstance(d, str) and d.strip():
                    out["date"] = d.strip()
            if out["author"] and out["date"]:
                return out
    return out


def _time_datetime(soup: BeautifulSoup) -> Optional[str]:
    """Return the ``datetime`` attr from the first ``<time datetime=…>`` tag."""

    tag = soup.find("time", attrs={"datetime": True})
    if tag is None:
        return None
    val = tag.get("datetime")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _clean_byline(value: Optional[str]) -> Optional[str]:
    """Drop URL-shaped values (``article:author`` often holds a profile URL)."""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        return None
    return text


def _format_short_date(raw: Optional[str]) -> Optional[str]:
    """Render an ISO-8601 datetime / date into the ``"Apr 27"`` style we print.

    The byline format Alain uses in the corpus is short month + day, no year
    (the issue date already supplies the year). We accept both
    ``2026-04-27T10:00:00Z`` and ``2026-04-27`` shapes.
    """

    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # ``Z`` suffix → +00:00 so ``fromisoformat`` accepts it (Py ≥ 3.11 also
    # parses ``Z`` natively, but normalizing is harmless and broader).
    norm = text.replace("Z", "+00:00")
    parsed: Optional[date] = None
    try:
        parsed = datetime.fromisoformat(norm).date()
    except ValueError:
        try:
            parsed = date.fromisoformat(text[:10])
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    return f"{parsed.strftime('%b')} {parsed.day}"


def _find_icon_href(soup: BeautifulSoup) -> Optional[str]:
    """Pick the best link[rel*=icon] href, preferring apple-touch-icon."""

    def _has_rel(tag, needle: str) -> bool:
        rel = tag.get("rel")
        if not rel:
            return False
        if isinstance(rel, list):
            joined = " ".join(rel).lower()
        else:
            joined = str(rel).lower()
        return needle in joined

    for needle in ("apple-touch-icon", "icon"):
        for tag in soup.find_all("link"):
            if _has_rel(tag, needle) and tag.get("href"):
                return tag["href"].strip()
    return None


def parse_article_metadata(html: str, base_url: str) -> ArticleMetadata:
    """Pure parser — extract Article-fillable metadata from HTML."""

    soup = BeautifulSoup(html, "html.parser")
    out: ArticleMetadata = {}
    jsonld = _parse_jsonld_meta(soup)

    title = _first(
        _meta_content(soup, property="og:title"),
        _meta_content(soup, **{"name": "twitter:title"}),
        soup.title.string.strip() if soup.title and soup.title.string else None,
    )
    if title:
        out["headline"] = title

    byline = _clean_byline(_first(
        _meta_content(soup, **{"name": "author"}),
        _clean_byline(_meta_content(soup, property="article:author")),
        _meta_content(soup, **{"name": "byl"}),
        _meta_content(soup, **{"name": "sailthru.author"}),
        _meta_content(soup, **{"name": "parsely-author"}),
        _meta_content(soup, **{"name": "twitter:creator"}),
        _meta_content(soup, **{"name": "DC.creator"}),
        jsonld.get("author"),
    ))
    if byline:
        out["byline"] = byline

    date_raw = _first(
        _meta_content(soup, property="article:published_time"),
        _meta_content(soup, property="og:article:published_time"),
        _meta_content(soup, **{"itemprop": "datePublished"}),
        _meta_content(soup, **{"name": "parsely-pub-date"}),
        _meta_content(soup, **{"name": "date"}),
        _meta_content(soup, **{"name": "pubdate"}),
        _meta_content(soup, **{"name": "publishdate"}),
        _meta_content(soup, **{"name": "DC.date.issued"}),
        _meta_content(soup, **{"name": "sailthru.date"}),
        jsonld.get("date"),
        _time_datetime(soup),
    )
    formatted_date = _format_short_date(date_raw)
    if formatted_date:
        out["date_text"] = formatted_date

    excerpt = _first(
        _meta_content(soup, property="og:description"),
        _meta_content(soup, **{"name": "description"}),
        _meta_content(soup, **{"name": "twitter:description"}),
    )
    if excerpt:
        out["pull_quote"] = excerpt

    icon_href = _find_icon_href(soup)
    if icon_href:
        out["publication_logo_url"] = urljoin(base_url, icon_href)
    else:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            out["publication_logo_url"] = (
                f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
            )

    out["read_more_url"] = base_url
    return out


def _fetch_html(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    user_agent: str = _DEFAULT_USER_AGENT,
    client: Optional[httpx.Client] = None,
) -> tuple[str, str]:
    """GET ``url`` and return ``(html, final_url)``.

    ``final_url`` is the URL after redirects, used as the base for resolving
    relative icon paths in :func:`parse_article_metadata`.
    """

    headers = {"User-Agent": user_agent, **_DEFAULT_HEADERS}
    if client is not None:
        r = client.get(url, headers=headers, follow_redirects=True, timeout=timeout)
        r.raise_for_status()
        return r.text, str(r.url)

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text, str(r.url)


def fetch_article_metadata(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
) -> ArticleMetadata:
    """High-level helper: validate, fetch, parse."""

    safe_url = validate_fetch_url(url)
    html, final_url = _fetch_html(safe_url, timeout=timeout, client=client)
    return parse_article_metadata(html, final_url)
