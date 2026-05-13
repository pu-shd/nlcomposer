"""Data models for a SmartDrivingCars newsletter issue.

The shape mirrors the patterns observed in the historical issue corpus
(`smartdrivingcar/_newsletters/<slug>/index.md`). Each section is optional
so partial drafts validate, but a fully-populated issue covers every block
seen in production newsletters.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Annotated, Optional

from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, Field, model_validator

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
    publication_logo_media_id: Optional[int] = Field(
        default=None,
        description=(
            "Media Library id for an uploaded article logo. Preferred over "
            "``publication_logo_url`` when set — the in-browser preview "
            "renders it as ``/api/media/{id}/content``. (Mail-client "
            "inline embedding of per-article logos follows the planned "
            "``embed_all_media`` work; until then, uploaded article logos "
            "render in the preview but not in the recipient's inbox — "
            "use a public URL via the Logo URL field for now.)"
        ),
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

    Bottom highlights are rendered without icons or commentary, optionally
    grouped under the issue date the article was originally featured in.
    Two flavors of reference are supported:

    * **In-issue** — ``article_index`` points at one of the current
      issue's ``articles``. This is the original behavior and what every
      pre-archive-sourcing draft contains.
    * **Archive-sourced** — ``source_archive_id`` + ``source_article_index``
      reference an article from a previously-published issue stored in the
      ``archive`` table. The candidate-search endpoint resolves the
      reference once on pick and the JS round-trips the metadata via the
      ``cached_*`` fields, so the renderer never queries the archive at
      render time. The cached fields also let the template group
      highlights by source-issue date without any extra DB hop.

    Exactly one referencing kind must be set; the validator below enforces
    that. ``override_label`` continues to apply to either flavor.
    """

    # In-issue reference — historical shape; still the default for the
    # most common case where the editor recaps articles from THIS issue.
    article_index: Optional[int] = Field(
        default=None,
        description="Zero-based index into NewsletterIssue.articles.",
    )

    # Archive-sourced reference. Both ids must be set together; the JS
    # populates source_archive_id + source_article_index at pick time
    # alongside the cached_* fields below.
    source_archive_id: Optional[int] = Field(
        default=None,
        description="archive.id of the source issue (cross-issue highlight).",
    )
    source_article_index: Optional[int] = Field(
        default=None,
        description="Zero-based index into archive.payload.articles.",
    )

    # Renderable snapshot — populated by the SPA when an archive
    # candidate is picked, so the template needs no DB access to render
    # cross-issue highlights and groupings survive a draft save/load.
    cached_headline: Optional[str] = Field(
        default=None,
        description="Headline text for archive-sourced refs.",
    )
    cached_url: Optional[str] = Field(
        default=None,
        description="Article URL for archive-sourced refs.",
    )
    cached_issue_date: Optional[_date] = Field(
        default=None,
        description="Date of the source issue (used as the group heading).",
    )
    cached_issue_year: Optional[int] = Field(
        default=None,
        description="`issue_year` of the source issue.",
    )
    cached_issue_number: Optional[int] = Field(
        default=None,
        description="`issue_number` of the source issue.",
    )
    cached_issue_title: Optional[str] = Field(
        default=None,
        description="`issue_title` of the source issue (for display next to date).",
    )

    override_label: Optional[str] = Field(
        default=None,
        description="If set, replaces the headline text in the highlights list.",
    )

    @model_validator(mode="after")
    def _exactly_one_referencing_kind(self) -> "HighlightRef":
        """Reject refs that point nowhere — or point in two directions at once.

        Draft loading is the realistic culprit: an old draft that only
        sets ``article_index`` is still valid. A new draft that sets
        ``source_archive_id`` + ``source_article_index`` is valid. A row
        with neither (e.g. a half-filled form) is rejected so the server
        responds 422 with a clear pointer instead of rendering a blank
        list item; a row with both is ambiguous so it's rejected too.
        """
        has_in_issue = self.article_index is not None
        has_archive = (
            self.source_archive_id is not None
            and self.source_article_index is not None
        )
        partial_archive = (
            (self.source_archive_id is None) != (self.source_article_index is None)
        )
        if partial_archive:
            raise ValueError(
                "source_archive_id and source_article_index must be set together"
            )
        if has_in_issue and has_archive:
            raise ValueError(
                "highlight cannot reference both an in-issue article_index "
                "and a source_archive_id — pick one"
            )
        if not has_in_issue and not has_archive:
            raise ValueError(
                "highlight needs either article_index or source_archive_id+"
                "source_article_index"
            )
        return self

    def display_url(self, articles: "list[Article]") -> Optional[str]:
        """Return the URL to link the highlight to.

        Cross-issue refs lean on ``cached_url`` (captured at pick time);
        in-issue refs resolve through the current ``articles`` list so
        the URL stays in sync if the editor edits the article between
        adding the highlight and exporting the issue.
        """
        if self.article_index is not None and 0 <= self.article_index < len(articles):
            return str(articles[self.article_index].url)
        return self.cached_url

    def display_label(self, articles: "list[Article]") -> Optional[str]:
        """Return the visible link text, honoring ``override_label``."""
        if self.override_label:
            return self.override_label
        if self.article_index is not None and 0 <= self.article_index < len(articles):
            return articles[self.article_index].headline
        return self.cached_headline


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
    masthead_logo_media_id: Optional[int] = Field(
        default=None,
        description=(
            "Media Library id for an uploaded masthead logo. Preferred over "
            "``masthead_logo_url`` when set — the eml builder resolves it to "
            "inline bytes (cid:) and the in-browser preview renders it as "
            "``/api/media/{id}/content``. ``url`` remains as a fallback if "
            "both are present and the media item is not yet ready."
        ),
    )
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

    def highlights_grouped(self) -> list[dict]:
        """Group bottom highlights by source issue date for the template.

        Returns a list of ``{label, date, refs}`` dicts ordered by date
        descending (newest issue first) so the current week sits at the
        top of the rendered Highlights block. ``label`` is the heading
        text — ``"This issue"`` for refs into the current articles,
        otherwise the source issue's formatted date plus its title.
        Inside each group, refs preserve the order the editor arranged
        them with the drag handle. URL-keyed dedup keeps the first
        occurrence of each article so a recap that appears in multiple
        archive editions only renders once.
        """
        in_issue: list[HighlightRef] = []
        # ``(year, month, day, title)`` sort key so older years sink
        # toward the bottom of the rendered block; in-issue group is
        # synthesized separately and always emitted first.
        by_archive: dict[tuple, dict] = {}
        for h in self.highlights:
            if h.article_index is not None:
                in_issue.append(h)
                continue
            d = h.cached_issue_date
            if d is None:
                # Defensive: an archive-sourced ref with no cached date
                # is malformed — render it under a synthetic "Archive"
                # bucket rather than dropping it silently.
                key = (0, 0, 0, "")
                label = "Archive"
            else:
                key = (d.year, d.month, d.day, h.cached_issue_title or "")
                label = self._format_group_label(h)
            bucket = by_archive.setdefault(
                key,
                {"label": label, "date": d, "refs": []},
            )
            bucket["refs"].append(h)

        # Dedup by URL inside each group so a recap that appears in
        # multiple archives only renders once. We dedup per-group rather
        # than globally because a user may intentionally feature the
        # same canonical article under two different source dates.
        for bucket in by_archive.values():
            seen: set[str] = set()
            deduped: list[HighlightRef] = []
            for ref in bucket["refs"]:
                url = (ref.cached_url or "").strip()
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                deduped.append(ref)
            bucket["refs"] = deduped

        groups: list[dict] = []
        if in_issue:
            # Same per-group URL dedup for the current-issue bucket. We
            # resolve URLs through `display_url` so an override doesn't
            # accidentally hide a duplicate.
            seen = set()
            deduped = []
            for ref in in_issue:
                url = (ref.display_url(self.articles) or "").strip()
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                deduped.append(ref)
            groups.append(
                {
                    "label": "This issue",
                    "date": self.issue_date,
                    "refs": deduped,
                }
            )
        # Sort archive groups newest → oldest.
        for key in sorted(by_archive.keys(), reverse=True):
            groups.append(by_archive[key])
        return groups

    def _format_group_label(self, h: "HighlightRef") -> str:
        """Heading text for an archive-sourced group: date + edition."""
        d = h.cached_issue_date
        if d is None:
            return h.cached_issue_title or "Archive"
        # Match the human-friendly format used by ``effective_display_date``.
        base = d.strftime("%B %-d, %Y")
        if h.cached_issue_title:
            return f"{base} · {h.cached_issue_title}"
        return base


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
