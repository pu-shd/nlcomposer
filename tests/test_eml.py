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
    # Header
    assert "SmartDrivingCars" in html
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


def test_filename_is_filesystem_safe(sample_issue):
    fname = build_eml_filename(sample_issue)
    assert fname.endswith(".eml")
    assert "14.09" in fname
    assert "BeijingAutoShow" in fname
    assert "2026-05-03" in fname
    assert " " not in fname
    assert "/" not in fname
