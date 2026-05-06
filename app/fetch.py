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

from typing import Optional, TypedDict
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

_DEFAULT_TIMEOUT = 8.0
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; sdc-composer/0.1; "
    "+https://smartdrivingcar.com/)"
)


class ArticleMetadata(TypedDict, total=False):
    """Subset of Article fields the fetcher can populate."""

    headline: str
    byline: str
    pull_quote: str
    publication_logo_url: str
    read_more_url: str


def validate_fetch_url(url: str) -> str:
    """Validate a URL is safe(ish) to fetch from this local tool.

    Rejects non-http(s) schemes, missing hosts, and obvious loopback
    targets. This is not a hardened SSRF filter — the composer is intended
    to run on a developer's laptop — but it stops the most embarrassing
    accidents (``file://``, ``http://localhost``).
    """

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http(s) URLs are supported")
    if not parsed.netloc:
        raise ValueError("URL is missing a host")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(
        ".localhost"
    ):
        raise ValueError("URL host is not allowed")
    return url.strip()


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

    title = _first(
        _meta_content(soup, property="og:title"),
        _meta_content(soup, **{"name": "twitter:title"}),
        soup.title.string.strip() if soup.title and soup.title.string else None,
    )
    if title:
        out["headline"] = title

    byline = _first(
        _meta_content(soup, **{"name": "author"}),
        _meta_content(soup, property="article:author"),
        _meta_content(soup, **{"name": "byl"}),
        _meta_content(soup, **{"name": "sailthru.author"}),
    )
    if byline:
        out["byline"] = byline

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

    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
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
