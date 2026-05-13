"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    Article,
    HighlightRef,
    NewsletterIssue,
    PodcastEpisode,
    PodcastTimecode,
    StaticBlock,
)


@pytest.fixture
def client() -> TestClient:
    """A FastAPI TestClient bound to the app's lifespan.

    Hoisted here so the five test files that used to define an
    identical fixture no longer have to. The ``with TestClient(app)``
    context ensures init_db + seed_default_templates run on entry and
    the DB connection is closed on exit. Pairs with the autouse
    ``_isolate_data_dir`` fixture so each test gets a fresh data dir.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point every test at a per-test ``SDC_DATA_DIR``.

    Without this, importing ``app.main`` triggers the FastAPI lifespan
    which calls ``init_db`` against the developer's working tree.
    """
    monkeypatch.setenv("SDC_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def sample_issue() -> NewsletterIssue:
    """An issue patterned after `14.9-BeijingAutoShow-5.03.26` with stripped-down content.

    Carries an explicit ``newsletter_name="SmartDrivingCars"`` so the
    rendered HTML/text (and existing assertions like
    ``"SmartDrivingCars Newsletter" in text``) reflect the historical
    branding rather than the model's generic ``"MyNewsletter"`` default.
    """
    return NewsletterIssue(
        newsletter_name="SmartDrivingCars",
        issue_year=14,
        issue_number=9,
        issue_title="BeijingAutoShow",
        issue_date=date(2026, 5, 3),
        from_name="Newsletter Editor",
        from_email="editor@example.edu",
        to_email="subscribers@example.edu",
        articles=[
            Article(
                headline=(
                    "Beijing Auto Show 2026: A Field Report from China's "
                    "Operational-Excellence Era"
                ),
                url="https://pandaperspectives.substack.com/p/beijing-auto-show-2026-a-field-report",
                byline="P. Perspective",
                date_text="April 27",
                pull_quote=(
                    "We spent yesterday at the Beijing Auto Show. Sore feet, "
                    "a dead phone, and the cleanest single-day read on the "
                    "Chinese auto industry we have had in years."
                ),
                publication_logo_url=(
                    "https://substackcdn.com/icons/substack/icon.png"
                ),
                alain_comment=(
                    "Another excellent session. He can own the 1,000-3,000 "
                    "mile truck long-haul market."
                ),
            ),
            Article(
                headline="Why Are We Still Driving?",
                url="https://www.nytimes.com/2026/04/30/opinion/waymo-self-driving-cars-andrew-miller.html",
                byline="A. Miller",
                date_text="Apr 30",
                pull_quote="Waymo is here. Why aren't we using it more?",
            ),
        ],
        podcast=PodcastEpisode(
            episode_number=412,
            title=(
                "What is it like at the 2026 Auto Show in Beijing? On episode "
                "412 of Smart Driving Cars."
            ),
            zoomcast_url="https://youtu.be/OVyjLVA5524",
            podcast_url="https://open.spotify.com/episode/572BBiIYJFnR2GanmrgQSm",
            youtube_video_url="https://www.youtube.com/watch?v=OVyjLVA5524",
            guest="Camille Kamga",
            summary=(
                "On episode 412 of Smart Driving Cars, we get a look at the "
                "show floor, courtesy of CCNY Professor Camille Kamga."
            ),
            timecodes=[
                PodcastTimecode(
                    timecode="0:00",
                    label="open",
                    url="https://www.youtube.com/watch?v=OVyjLVA5524",
                ),
                PodcastTimecode(
                    timecode="0:50",
                    label="Guest Professor Camille Kamga at the Beijing Auto Show",
                    url="https://www.youtube.com/watch?v=OVyjLVA5524&t=50s",
                ),
                PodcastTimecode(
                    timecode="17:35",
                    label="Tesla still selling well overall in China despite competition",
                    url="https://www.youtube.com/watch?v=OVyjLVA5524&t=1055s",
                ),
            ],
        ),
        static_blocks=[
            StaticBlock(
                title="The Real Case for Driverless Mobility",
                html=(
                    '<p>Narrated by Fred Fishkin, available now on '
                    '<a href="https://www.audible.com/">Audible</a>.</p>'
                ),
            ),
            StaticBlock(
                title="2026 Automated Transportation Symposium (ATS)",
                html=(
                    '<p>July 27-30, 2026, San Diego, CA. '
                    '<a href="https://www.cvent.com/c/abstracts/739f4041-3744-44c3-a97e-9f688c5e544f">Call for papers</a>.</p>'
                ),
            ),
        ],
        highlights=[
            HighlightRef(article_index=0),
            HighlightRef(article_index=1, override_label="NYT: Why Are We Still Driving?"),
        ],
        unsubscribe_url="https://smartdrivingcar.com/unsubscribe",
    )
