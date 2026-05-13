"""End-to-end tests for the .eml builder.

The goal here is *fidelity*: the message must round-trip through Python's
``email`` parser and yield the structure mail clients expect — multipart
alternative with both text and html bodies, sensible headers, and (when
provided) an inline-related image referenced by ``cid:``.
"""

from __future__ import annotations

import email
from email import policy
from email.message import EmailMessage

from app.eml import build_eml, build_eml_filename, render_html, render_text


def test_render_html_contains_required_blocks(sample_issue):
    html = render_html(sample_issue)
    # Header — three stacked lines below the masthead
    assert "SmartDrivingCars" in html
    assert "Sunday, May 3, 2026" in html
    assert "14.9-BeijingAutoShow-5.03.26" in html
    assert "9th edition of the 14th year" in html
    # Articles
    assert "Beijing Auto Show 2026" in html
    assert "https://pandaperspectives.substack.com/p/beijing-auto-show-2026-a-field-report" in html
    assert "Hmmmm" in html  # Alain commentary survived
    assert "P. Perspective" in html
    # Podcast
    assert "ZoomCast" in html
    assert "Camille Kamga" in html
    assert "0:50" in html
    # Static block
    assert "Real Case for Driverless Mobility" in html
    # Highlights repeat
    assert "Highlights" in html
    assert "NYT: Why Are We Still Driving?" in html
    # Footer
    assert "unsubscribe" in html.lower()


def test_render_omits_read_more_when_toggle_off(sample_issue):
    """When NewsletterIssue.include_read_more_link is False (driven by
    AppSettings.include_read_more_link), the per-article 'Read more'
    anchor must disappear from both HTML and plain-text renders. The
    article's own headline link still points at the source URL — the
    setting only suppresses the duplicate trailing link.
    """
    on_html = render_html(sample_issue)
    on_text = render_text(sample_issue)
    # Default (on) — link is present in both renders.
    assert ">Read more</a>" in on_html
    assert "Read more: https://" in on_text

    off = sample_issue.model_copy(update={"include_read_more_link": False})
    off_html = render_html(off)
    off_text = render_text(off)
    assert ">Read more</a>" not in off_html
    assert "Read more: https://" not in off_text
    # Headline anchor still leads to the article URL (not removed).
    assert (
        "https://pandaperspectives.substack.com/p/beijing-auto-show-2026-a-field-report"
        in off_html
    )


def test_render_text_strips_html_but_keeps_links(sample_issue):
    text = render_text(sample_issue)
    assert "SmartDrivingCars Newsletter" in text
    assert "Beijing Auto Show 2026" in text
    assert "https://pandaperspectives.substack.com/p/beijing-auto-show-2026-a-field-report" in text
    assert "Hmmmm" in text
    # No HTML tags should leak through
    assert "<a " not in text
    assert "<p>" not in text


def test_build_eml_round_trips_as_multipart_alternative(sample_issue):
    raw = build_eml(sample_issue)
    assert isinstance(raw, bytes)

    msg = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)

    # Top-level headers
    assert "SmartDrivingCars" in msg["Subject"]
    assert "BeijingAutoShow" in msg["Subject"]
    assert msg["From"] is not None and "editor@example.edu" in msg["From"]
    assert msg["To"] == "subscribers@example.edu"

    # Multipart structure
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.iter_parts())
    types = sorted(p.get_content_type() for p in parts)
    assert types == ["text/html", "text/plain"]

    # Bodies survived encoding
    html_part = next(p for p in parts if p.get_content_type() == "text/html")
    text_part = next(p for p in parts if p.get_content_type() == "text/plain")
    assert "Beijing Auto Show 2026" in html_part.get_content()
    assert "Beijing Auto Show 2026" in text_part.get_content()


def test_build_eml_with_inline_logo_attaches_related_image(sample_issue):
    fake_png = (
        b"\x89PNG\r\n\x1a\n"  # PNG header
        b"\x00\x00\x00\rIHDR"  # minimal chunk
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n\x2d\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    raw = build_eml(sample_issue, masthead_logo_bytes=fake_png)
    msg = email.message_from_bytes(raw, policy=policy.default)

    # Walk and look for the image part.
    image_parts = [p for p in msg.walk() if p.get_content_type().startswith("image/")]
    assert len(image_parts) == 1, (
        "expected exactly one inline image part, got "
        f"{[p.get_content_type() for p in image_parts]}"
    )
    image = image_parts[0]
    cid = image.get("Content-ID", "").strip("<>")
    assert cid, "image part must carry a Content-ID for cid: references to resolve"

    # The HTML alternative should reference the cid.
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert html_parts, "html alternative missing after inline-image attach"
    assert any(f"cid:{cid}" in p.get_content() for p in html_parts), (
        "HTML body did not reference the inline image's Content-ID"
    )


def test_render_html_uses_media_endpoint_when_only_media_id_is_set(sample_issue):
    """Preview path: masthead_logo_media_id renders as /api/media/{id}/content.

    The in-browser preview re-renders on every keystroke and shouldn't
    need to do any byte resolution. The eml controller swaps this for an
    inline cid: at .eml-build time — that's covered by the dedicated
    /api/eml test in test_main.py.
    """
    issue = sample_issue.model_copy(update={"masthead_logo_media_id": 7})
    html = render_html(issue)
    assert 'src="/api/media/7/content"' in html
    # The plain URL fallback should not appear when media_id is set.
    assert "cid:" not in html


def test_render_html_prefers_cid_over_media_id_and_url(sample_issue):
    """When build_eml has set the cid, the template ignores media_id/url.

    The cid path is what survives in the recipient's mail client — the
    /api/media URL would 404 once the message leaves our domain — so the
    template must always prefer cid when build_eml has populated it.
    """
    issue = sample_issue.model_copy(
        update={
            "masthead_logo_cid": "masthead-test-cid",
            "masthead_logo_media_id": 7,
            "masthead_logo_url": "https://example.com/logo.png",
        }
    )
    html = render_html(issue)
    assert 'src="cid:masthead-test-cid"' in html
    assert "/api/media/7/content" not in html
    assert "https://example.com/logo.png" not in html


def test_render_html_falls_back_to_url_when_no_cid_and_no_media_id(sample_issue):
    issue = sample_issue.model_copy(
        update={"masthead_logo_url": "https://example.com/logo.png"}
    )
    html = render_html(issue)
    assert 'src="https://example.com/logo.png"' in html
    assert "/api/media/" not in html


def test_filename_is_filesystem_safe(sample_issue):
    fname = build_eml_filename(sample_issue)
    assert fname.endswith(".eml")
    assert "14.09" in fname
    assert "BeijingAutoShow" in fname
    assert "2026-05-03" in fname
    assert " " not in fname
    assert "/" not in fname


# ───────── Bottom highlights grouping (mixed sources) ────────────────


def test_render_html_groups_mixed_highlights_with_subheadings(sample_issue):
    """When highlights pull from both the current issue and the archive,
    the rendered HTML should group them under per-source headings so the
    reader can tell at a glance which week each link is from."""
    from datetime import date

    issue = sample_issue.model_copy(
        update={
            "highlights": [
                # Current issue ref by index
                __import__("app.models", fromlist=["HighlightRef"]).HighlightRef(
                    article_index=0
                ),
                # Archive ref with cached metadata
                __import__("app.models", fromlist=["HighlightRef"]).HighlightRef(
                    source_archive_id=42,
                    source_article_index=1,
                    cached_headline="From last month",
                    cached_url="https://example.com/last-month",
                    cached_issue_date=date(2026, 4, 5),
                    cached_issue_title="LastMonthIssue",
                ),
            ]
        }
    )
    html = render_html(issue)
    # Outer block
    assert ">Highlights<" in html
    # Per-source headings appear when more than one group is present
    assert "This issue" in html
    assert "LastMonthIssue" in html
    # The archive ref's URL is rendered straight from cached_url so the
    # mail client can fetch the historical article.
    assert "https://example.com/last-month" in html


def test_render_html_omits_subheadings_when_only_current_issue(sample_issue):
    """A regular all-current-issue draft keeps the simple one-list look
    — no per-group sub-headings cluttering the bottom block."""
    from app.models import HighlightRef
    issue = sample_issue.model_copy(
        update={"highlights": [HighlightRef(article_index=0)]}
    )
    html = render_html(issue)
    assert ">Highlights<" in html
    # No "This issue" sub-heading when there's only one group.
    assert ">This issue<" not in html
