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
