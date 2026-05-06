"""Tests for the Safelinks sanitizer.

The fixture URL is the literal example Alain pasted from a forwarded
NAM12 message; if Microsoft changes the wrapper shape these tests are
the canary.
"""

from __future__ import annotations

import pytest

from app.links import sanitize_url, unwrap_safelink


SAMPLE_SAFELINK = (
    "https://nam12.safelinks.protection.outlook.com/?"
    "url=https%3A%2F%2Fwww.mmi.mit.edu%2Fforum"
    "&data=05%7C02%7Csubscribers%40example.edu"
    "%7Ce61d11d526d9424eea0108de9d0b3038"
    "%7C2ff601167431425db5af077d7791bda4%7C0%7C0%7C639120868778743361"
    "%7CUnknown%7CTWFpbGZsb3d8eyJFbXB0eU1hcGkiOnRydWV9"
    "&sdata=hJFUMxamc5dytT5JYvQqtExzz9YIDnNAxxgrPar5bTc%3D"
    "&reserved=0"
)


def test_unwrap_safelink_extracts_inner_url():
    assert unwrap_safelink(SAMPLE_SAFELINK) == "https://www.mmi.mit.edu/forum"


def test_sanitize_url_is_idempotent():
    once = sanitize_url(SAMPLE_SAFELINK)
    twice = sanitize_url(once)
    assert once == twice == "https://www.mmi.mit.edu/forum"


@pytest.mark.parametrize(
    "host",
    [
        "nam01.safelinks.protection.outlook.com",
        "nam12.safelinks.protection.outlook.com",
        "eur03.safelinks.protection.outlook.com",
        "apac.safelinks.protection.outlook.com",
        "safelinks.protection.outlook.com",
    ],
)
def test_sanitize_url_handles_regional_hosts(host: str):
    wrapped = f"https://{host}/?url=https%3A%2F%2Fexample.com%2Fa&data=x"
    assert sanitize_url(wrapped) == "https://example.com/a"


def test_sanitize_url_passes_through_plain_urls():
    plain = "https://www.mmi.mit.edu/forum?ref=abc"
    assert sanitize_url(plain) == plain


def test_sanitize_url_handles_none_and_non_strings():
    assert sanitize_url(None) is None
    assert sanitize_url(42) == 42  # type: ignore[arg-type]


def test_sanitize_url_unwraps_nested_safelinks():
    inner = "https://www.mmi.mit.edu/forum"
    once = (
        "https://nam12.safelinks.protection.outlook.com/?url="
        + inner.replace(":", "%3A").replace("/", "%2F")
    )
    twice = (
        "https://nam01.safelinks.protection.outlook.com/?url="
        + once.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F").replace("=", "%3D")
    )
    assert sanitize_url(twice) == inner


def test_sanitize_url_leaves_safelinks_without_url_param_alone():
    weird = "https://nam12.safelinks.protection.outlook.com/?data=foo"
    assert sanitize_url(weird) == weird


def test_sanitize_url_rejects_non_http_inner():
    """A safelink wrapping ``javascript:`` or ``file:`` must not be unwrapped."""
    bad = "https://nam12.safelinks.protection.outlook.com/?url=javascript%3Aalert(1)"
    assert sanitize_url(bad) == bad
