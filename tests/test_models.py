from __future__ import annotations

from datetime import date

from app.models import Article, NewsletterIssue, _ordinal_suffix, compute_issue_year


def test_ordinal_suffix_handles_teens_and_units():
    assert _ordinal_suffix(1) == "st"
    assert _ordinal_suffix(2) == "nd"
    assert _ordinal_suffix(3) == "rd"
    assert _ordinal_suffix(4) == "th"
    assert _ordinal_suffix(11) == "th"  # teens are all "th"
    assert _ordinal_suffix(12) == "th"
    assert _ordinal_suffix(21) == "st"
    assert _ordinal_suffix(22) == "nd"


def test_edition_line_matches_corpus_pattern():
    # Model's default newsletter_name kicks in when not specified.
    issue = NewsletterIssue(
        issue_year=14, issue_number=9, issue_title="x", issue_date=date(2026, 5, 3)
    )
    assert issue.edition_line() == "9th edition of the 14th year of MyNewsletter eLetter"


def test_edition_line_uses_configured_newsletter_name():
    """When newsletter_name is set (mirrored from AppSettings at build
    time), it surfaces in the masthead "Nth year of …" line so the
    composer can serve any periodical, not just SmartDrivingCars."""
    issue = NewsletterIssue(
        newsletter_name="SmartDrivingCars",
        issue_year=14,
        issue_number=9,
        issue_title="x",
        issue_date=date(2026, 5, 3),
    )
    assert issue.edition_line() == "9th edition of the 14th year of SmartDrivingCars eLetter"


def test_effective_subject_uses_configured_newsletter_name():
    issue = NewsletterIssue(
        newsletter_name="MyNewsletter",
        issue_year=14, issue_number=9, issue_title="BeijingAutoShow",
        issue_date=date(2026, 5, 3),
    )
    assert issue.effective_subject().startswith("MyNewsletter ")


def test_effective_subject_falls_back_to_year_number_title():
    issue = NewsletterIssue(
        issue_year=14, issue_number=9, issue_title="BeijingAutoShow", issue_date=date(2026, 5, 3)
    )
    subject = issue.effective_subject()
    assert "14.09" in subject
    assert "BeijingAutoShow" in subject


def test_article_read_more_falls_back_to_url():
    a = Article(headline="x", url="https://example.com/a")
    assert str(a.effective_read_more_url()) == "https://example.com/a"


def test_header_slug_matches_corpus_format():
    issue = NewsletterIssue(
        issue_year=14,
        issue_number=9,
        issue_title="BeijingAutoShow",
        issue_date=date(2026, 5, 3),
    )
    # year and issue number are unpadded; month is unpadded; day and 2-digit
    # year are zero-padded — see the historical newsletter examples.
    assert issue.header_slug() == "14.9-BeijingAutoShow-5.03.26"


def test_header_slug_handles_two_digit_month_and_pre_2000_year():
    issue = NewsletterIssue(
        issue_year=14,
        issue_number=12,
        issue_title="YearEnd",
        issue_date=date(2026, 12, 7),
    )
    assert issue.header_slug() == "14.12-YearEnd-12.07.26"


# ───────── compute_issue_year ────────────────────────────────────────


def test_compute_issue_year_matches_historical_14th_year_example():
    """The 14.9-BeijingAutoShow issue (2026-05-03) is in the 14th year.
    A launch around mid-September 2012 places that issue inside year 14
    (the anniversary at 2025-09-... has passed; 2026-09-... has not).
    """
    assert compute_issue_year(date(2026, 5, 3), date(2012, 9, 17)) == 14


def test_compute_issue_year_increments_on_anniversary():
    init = date(2012, 9, 17)
    # Day before anniversary in year 14 → still year 14.
    assert compute_issue_year(date(2026, 9, 16), init) == 14
    # On the anniversary → rolls into year 15.
    assert compute_issue_year(date(2026, 9, 17), init) == 15
    # A week into the new year → year 15.
    assert compute_issue_year(date(2026, 9, 24), init) == 15


def test_compute_issue_year_first_day_is_year_one():
    init = date(2020, 6, 1)
    assert compute_issue_year(date(2020, 6, 1), init) == 1
    assert compute_issue_year(date(2020, 12, 31), init) == 1
    assert compute_issue_year(date(2021, 5, 31), init) == 1
    assert compute_issue_year(date(2021, 6, 1), init) == 2


def test_compute_issue_year_clamps_pre_launch_to_one():
    """Issues dated before the launch can't sensibly be year 0 or
    negative — the masthead always prints at least '1st year'.
    """
    init = date(2020, 6, 1)
    assert compute_issue_year(date(2019, 1, 1), init) == 1
    assert compute_issue_year(date(2020, 5, 31), init) == 1


# ───────── HighlightRef validator + grouping ───────────────────────────


def _minimal_issue(highlights):
    """Fixture-light helper — minimal NewsletterIssue carrying just the
    fields the highlights tests need so we don't lean on conftest's
    sample_issue (which would force a per-test path)."""
    return NewsletterIssue(
        newsletter_name="Test",
        issue_year=1,
        issue_number=1,
        issue_title="Demo",
        issue_date=date(2026, 5, 3),
        articles=[
            Article(headline="Local A", url="https://example.com/a"),
            Article(headline="Local B", url="https://example.com/b"),
        ],
        highlights=highlights,
    )


def test_highlight_ref_accepts_in_issue_only():
    from app.models import HighlightRef

    h = HighlightRef(article_index=0)
    assert h.article_index == 0
    assert h.source_archive_id is None


def test_highlight_ref_accepts_archive_only():
    from app.models import HighlightRef

    h = HighlightRef(
        source_archive_id=5,
        source_article_index=2,
        cached_headline="From Archive",
        cached_url="https://example.com/archived",
        cached_issue_date=date(2026, 1, 1),
    )
    assert h.source_archive_id == 5
    assert h.article_index is None


def test_highlight_ref_rejects_partial_archive_ref():
    """source_archive_id and source_article_index must travel together."""
    import pytest
    from pydantic import ValidationError
    from app.models import HighlightRef

    with pytest.raises(ValidationError):
        HighlightRef(source_archive_id=5)  # missing source_article_index
    with pytest.raises(ValidationError):
        HighlightRef(source_article_index=2)  # missing source_archive_id


def test_highlight_ref_rejects_both_flavors_set():
    """An in-issue ref AND an archive ref on the same row is ambiguous."""
    import pytest
    from pydantic import ValidationError
    from app.models import HighlightRef

    with pytest.raises(ValidationError):
        HighlightRef(
            article_index=0,
            source_archive_id=5,
            source_article_index=2,
        )


def test_highlight_ref_rejects_empty_reference():
    """A row with no reference at all is malformed — should never reach
    the renderer. Reject upfront so the SPA sees a clear 422."""
    import pytest
    from pydantic import ValidationError
    from app.models import HighlightRef

    with pytest.raises(ValidationError):
        HighlightRef()


def test_highlights_grouped_separates_in_issue_from_archive():
    from app.models import HighlightRef

    issue = _minimal_issue(
        [
            HighlightRef(article_index=0),
            HighlightRef(
                source_archive_id=5,
                source_article_index=1,
                cached_headline="Older recap",
                cached_url="https://example.com/older",
                cached_issue_date=date(2026, 1, 1),
                cached_issue_title="JanIssue",
            ),
        ]
    )
    groups = issue.highlights_grouped()
    assert len(groups) == 2
    # Current-issue group first
    assert groups[0]["label"] == "This issue"
    assert len(groups[0]["refs"]) == 1
    assert groups[0]["refs"][0].article_index == 0
    # Archive group second, with the formatted date heading
    assert "January" in groups[1]["label"] or "Jan" in groups[1]["label"]
    assert "JanIssue" in groups[1]["label"]
    assert groups[1]["refs"][0].cached_url == "https://example.com/older"


def test_highlights_grouped_orders_archive_groups_newest_first():
    from app.models import HighlightRef

    issue = _minimal_issue(
        [
            HighlightRef(
                source_archive_id=1,
                source_article_index=0,
                cached_headline="Older",
                cached_url="https://example.com/older",
                cached_issue_date=date(2025, 1, 1),
            ),
            HighlightRef(
                source_archive_id=2,
                source_article_index=0,
                cached_headline="Newer",
                cached_url="https://example.com/newer",
                cached_issue_date=date(2026, 1, 1),
            ),
        ]
    )
    groups = issue.highlights_grouped()
    # No in-issue refs → no "This issue" bucket at the top; archive
    # groups sort newest-first.
    assert len(groups) == 2
    assert groups[0]["date"] == date(2026, 1, 1)
    assert groups[1]["date"] == date(2025, 1, 1)


def test_highlights_grouped_dedups_by_url_within_group():
    """A canonical article should never render twice in a single
    sub-group of the bottom highlights — dedup at grouping time."""
    from app.models import HighlightRef

    issue = _minimal_issue(
        [
            HighlightRef(
                source_archive_id=1,
                source_article_index=0,
                cached_headline="Same article",
                cached_url="https://example.com/canonical",
                cached_issue_date=date(2026, 1, 1),
            ),
            HighlightRef(
                source_archive_id=1,
                source_article_index=2,
                cached_headline="Same article, duped",
                cached_url="https://example.com/canonical",
                cached_issue_date=date(2026, 1, 1),
            ),
        ]
    )
    groups = issue.highlights_grouped()
    assert len(groups) == 1
    assert len(groups[0]["refs"]) == 1


def test_highlights_grouped_dedups_current_issue_by_url():
    """A user that double-clicked Add and picked the same article twice
    should see a single entry."""
    from app.models import HighlightRef

    issue = _minimal_issue(
        [HighlightRef(article_index=0), HighlightRef(article_index=0)]
    )
    groups = issue.highlights_grouped()
    assert len(groups) == 1
    assert len(groups[0]["refs"]) == 1


def test_highlight_ref_display_label_honors_override():
    from app.models import HighlightRef

    issue = _minimal_issue(
        [HighlightRef(article_index=0, override_label="Custom label")]
    )
    label = issue.highlights[0].display_label(issue.articles)
    assert label == "Custom label"


def test_highlight_ref_display_url_resolves_from_current_articles():
    """In-issue refs read the live URL from articles so a post-add edit
    to the article's URL is picked up at render time."""
    from app.models import HighlightRef

    issue = _minimal_issue([HighlightRef(article_index=1)])
    url = issue.highlights[0].display_url(issue.articles)
    assert url == "https://example.com/b"
