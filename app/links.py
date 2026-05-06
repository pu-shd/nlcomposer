"""Sanitize wrapped URLs before they enter a NewsletterIssue.

Subscribers' corporate mail gateways often rewrap shared links as Microsoft
Safelinks::

    https://nam12.safelinks.protection.outlook.com/?url=<original>&data=...&sdata=...

Pasting that into the newsletter would leak the original recipient's tenant
ID, tie the click to a per-mailbox tracker, and break once the SafeLinks
token expires. :func:`sanitize_url` extracts the wrapped target and
recurses (a few hops max) in case the wrapper is nested.

Pure stdlib — no extra dependency. Idempotent and a no-op on non-safelink
URLs so it can sit on every URL boundary in the app without surprise.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlparse

_SAFELINK_HOST_SUFFIX = ".safelinks.protection.outlook.com"
_MAX_HOPS = 5


def _is_safelink_host(host: str) -> bool:
    host = (host or "").lower()
    return host.endswith(_SAFELINK_HOST_SUFFIX) or host == _SAFELINK_HOST_SUFFIX[1:]


def unwrap_safelink(url: str) -> str:
    """One-hop unwrap. Returns ``url`` unchanged when not a Safelink."""

    parsed = urlparse(url)
    if not _is_safelink_host(parsed.hostname or ""):
        return url
    qs = parse_qs(parsed.query)
    # Microsoft uses lowercase ``url=`` in the wild; tolerate uppercase too.
    inner_list = qs.get("url") or qs.get("URL")
    if not inner_list:
        return url
    inner = inner_list[0]
    p2 = urlparse(inner)
    if p2.scheme not in ("http", "https") or not p2.netloc:
        return url
    return inner


def sanitize_url(url: Optional[str]) -> Optional[str]:
    """Public entry point. Idempotent; recurses through nested wrappers."""

    if not isinstance(url, str):
        return url
    cleaned = url.strip()
    for _ in range(_MAX_HOPS):
        nxt = unwrap_safelink(cleaned)
        if nxt == cleaned:
            return nxt
        cleaned = nxt
    return cleaned
