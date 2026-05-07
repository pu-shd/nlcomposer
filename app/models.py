"""Data models for a SmartDrivingCars newsletter issue.

The shape mirrors the patterns observed in the historical issue corpus
(`smartdrivingcar/_newsletters/<slug>/index.md`). Each section is optional
so partial drafts validate, but a fully-populated issue covers every block
seen in production newsletters.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Annotated, Optional

from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, Field

from .links import sanitize_url

# Any URL field on a NewsletterIssue runs through ``sanitize_url`` before
# pydantic does its own URL parsing — so a Microsoft Safelink pasted into
# the form (or arriving via the JSON API) gets unwrapped at the boundary
# and the rest of the app only ever sees the real target.
SanitizedHttpUrl = Annotated[AnyHttpUrl, BeforeValidator(sanitize_url)]


class Article(BaseModel):
    """One linked article in the body of the newsletter.

    Reflects the canonical pattern:
        ### [Headline](url)
        Byline, Date, "pull quote..." [Read more](url) Hmmmm... Alain comment
    """

    headline: str
    url: SanitizedHttpUrl
    byline: Optional[str] = None
    date_text: Optional[str] = Field(
        default=None,
        description='Free-form date as printed in the byline, e.g. "Apr 27"',
    )
    pull_quote: Optional[str] = None
    read_more_label: str = "Read more"
    read_more_url: Optional[SanitizedHttpUrl] = Field(
        default=None,
        description="Defaults to `url` when omitted.",
    )
    publication_logo_url: Optional[SanitizedHttpUrl] = Field(
        default=None,
        description="Small logo/icon shown next to the headline (favicon by default).",
    )
    alain_comment: Optional[str] = Field(
        default=None,
        description='Optional "Hmmmm... Alain" commentary appended after Read more.',
    )

    def effective_read_more_url(self) -> AnyHttpUrl:
        return self.read_more_url or self.url


class PodcastTimecode(BaseModel):
    """One bullet in the podcast timecode list."""

    timecode: str = Field(description='As displayed, e.g. "0:50" or "17:35"')
    label: str
    url: SanitizedHttpUrl = Field(
        description="Deep link into the YouTube video at the timecode.",
    )


class PodcastEpisode(BaseModel):
    """A podcast/zoomcast episode block."""

    episode_number: Optional[int] = None
    title: str
    zoomcast_url: Optional[SanitizedHttpUrl] = None
    podcast_url: Optional[SanitizedHttpUrl] = None
    youtube_video_url: Optional[SanitizedHttpUrl] = None
    guest: Optional[str] = None
    summary: Optional[str] = None
    timecodes: list[PodcastTimecode] = Field(default_factory=list)


class StaticBlock(BaseModel):
    """A repeated/static block — sponsor blurb, book promo, conference promo, etc.

    Stored as raw HTML so Alain can hand-tune the rendering once and reuse it
    across issues. The composer UI offers a "carry forward from last issue"
    button to populate these.
    """

    title: str
    html: str
    enabled: bool = True


class HighlightRef(BaseModel):
    """Reference to an article that should be repeated in the bottom highlights.

    Bottom highlights are rendered without icons or commentary, possibly in a
    different order than the main body.
    """

    article_index: int = Field(
        description="Zero-based index into NewsletterIssue.articles.",
    )
    override_label: Optional[str] = Field(
        default=None,
        description="If set, replaces the headline text in the highlights list.",
    )


class NewsletterIssue(BaseModel):
    """One full newsletter issue ready for rendering."""

    # Branding (mirrored from AppSettings.newsletter_name at build time
    # so renders are self-contained and round-trip safely without the
    # template needing access to the settings store).
    newsletter_name: str = "MyNewsletter"

    # Header / frontmatter
    issue_year: int = Field(description='e.g. 14 for "14th year"')
    issue_number: int = Field(description='e.g. 9 for "9th edition"')
    issue_title: str = Field(description="Short slug-like name for the edition")
    issue_date: _date
    display_date: Optional[str] = Field(
        default=None,
        description='Printed date string, e.g. "Sunday, May 3, 2026". Auto-generated if omitted.',
    )
    masthead_logo_url: Optional[SanitizedHttpUrl] = None
    masthead_logo_cid: Optional[str] = Field(
        default=None,
        description="Content-ID for an inline-attached logo image (set by the eml builder).",
    )
    intro_html: Optional[str] = Field(
        default=None,
        description="Optional intro paragraph(s) above the first article. Raw HTML.",
    )

    # Email envelope (defaults are sensible for the SmartDrivingCars list)
    from_name: str = "SmartDrivingCars"
    from_email: str = "newsletter@smartdrivingcar.com"
    to_email: str = "subscriber@example.com"
    reply_to: Optional[str] = None
    subject: Optional[str] = Field(
        default=None,
        description="Auto-built from issue_year/number/title when omitted.",
    )

    # Body sections — every one optional so partial drafts validate.
    articles: list[Article] = Field(default_factory=list)
    podcast: Optional[PodcastEpisode] = None
    static_blocks: list[StaticBlock] = Field(default_factory=list)
    highlights: list[HighlightRef] = Field(default_factory=list)

    footer_html: Optional[str] = None
    unsubscribe_url: Optional[SanitizedHttpUrl] = None

    # Render-time toggle propagated from AppSettings.include_read_more_link
    # so the same payload renders consistently whether served via /api/preview
    # or /api/eml. Default True preserves the historical behavior; the
    # composer client injects the configured value at build time.
    include_read_more_link: bool = True

    def effective_subject(self) -> str:
        if self.subject:
            return self.subject
        return (
            f"{self.newsletter_name} {self.issue_year}.{self.issue_number:02d} — "
            f"{self.issue_title}"
        )

    def effective_display_date(self) -> str:
        if self.display_date:
            return self.display_date
        return self.issue_date.strftime("%A, %B %-d, %Y")

    def edition_line(self) -> str:
        suffix = _ordinal_suffix(self.issue_number)
        year_suffix = _ordinal_suffix(self.issue_year)
        return (
            f"{self.issue_number}{suffix} edition of the "
            f"{self.issue_year}{year_suffix} year of {self.newsletter_name} eLetter"
        )

    def header_slug(self) -> str:
        """The middle line of the masthead header — e.g. ``14.9-BeijingAutoShow-5.03.26``.

        Format intentionally differs from the .eml filename: issue number and
        month are unpadded, day and 2-digit year are zero-padded, mirroring
        the historical newsletter style.
        """
        d = self.issue_date
        return (
            f"{self.issue_year}.{self.issue_number}-{self.issue_title}-"
            f"{d.month}.{d.day:02d}.{d.year % 100:02d}"
        )


def _ordinal_suffix(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def compute_issue_year(issue_date: _date, initial_publication_date: _date) -> int:
    """Return the 1-based "Nth year" for ``issue_date``.

    The newsletter rolls over to a new "year of publication" each time the
    issue date crosses the anniversary of ``initial_publication_date``.
    Year 1 starts the day the newsletter launched, year 2 begins the day
    after the first anniversary, etc. Issues dated before the launch fall
    back to 1 (the field can never be 0 in the printed masthead).

    The composer UI computes this client-side from the configured
    ``AppSettings.initial_publication_date`` and the form's issue date,
    replacing the legacy "Year" input — so it lives here in the model
    layer where both client (mirrored in JS) and server-side tests can
    exercise the same arithmetic.
    """
    years = issue_date.year - initial_publication_date.year
    if (issue_date.month, issue_date.day) < (
        initial_publication_date.month,
        initial_publication_date.day,
    ):
        years -= 1
    return max(1, years + 1)
