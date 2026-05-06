from __future__ import annotations

from datetime import date

from app.models import Article, NewsletterIssue, _ordinal_suffix


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
    issue = NewsletterIssue(
        issue_year=14, issue_number=9, issue_title="x", issue_date=date(2026, 5, 3)
    )
    assert issue.edition_line() == "9th edition of the 14th year of SmartDrivingCars eLetter"


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
