"""Unit tests for app.rate_limit.SlidingWindowLimiter.

These exercise the limiter directly (no HTTP) so the rolling-window
math is tested in isolation from the FastAPI plumbing in test_media_controller.
Inject ``now`` to keep tests deterministic — no real sleeps.
"""

from __future__ import annotations

from app.rate_limit import SlidingWindowLimiter


def test_admits_under_capacity():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=3)
    for i in range(3):
        d = lim.check("alice", now=100.0 + i)
        assert d.allowed
        assert d.hits == i + 1


def test_rejects_at_capacity():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=2)
    lim.check("alice", now=100.0)
    lim.check("alice", now=101.0)
    d = lim.check("alice", now=102.0)
    assert not d.allowed
    assert d.hits == 2
    assert d.limit == 2
    # Retry-after points to the oldest hit's expiry: 100 + 60 - 102 = 58s.
    assert d.retry_after == 58.0


def test_window_slides_forward():
    lim = SlidingWindowLimiter(window_seconds=10, capacity=2)
    lim.check("alice", now=100.0)
    lim.check("alice", now=101.0)
    # 10.5s after the first hit — only the first ages out (cutoff is
    # ``now - window``; trim is inclusive on the cutoff edge).
    d = lim.check("alice", now=110.5)
    assert d.allowed
    assert d.hits == 2  # 101.0 + 110.5 in the bucket


def test_keys_are_isolated():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=1)
    a = lim.check("alice", now=100.0)
    b = lim.check("bob", now=100.0)
    assert a.allowed and b.allowed
    # Neither blocks the other.


def test_capacity_zero_means_unlimited():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=0)
    for i in range(50):
        d = lim.check("alice", now=100.0 + i)
        assert d.allowed
        # Counters aren't tracked when disabled.
        assert d.hits == 0
        assert d.limit == 0


def test_reset_clears_one_key():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=1)
    lim.check("alice", now=100.0)
    assert not lim.check("alice", now=100.5).allowed
    lim.reset("alice")
    assert lim.check("alice", now=101.0).allowed


def test_reset_with_no_key_clears_all():
    lim = SlidingWindowLimiter(window_seconds=60, capacity=1)
    lim.check("alice", now=100.0)
    lim.check("bob", now=100.0)
    lim.reset()
    assert lim.check("alice", now=100.0).allowed
    assert lim.check("bob", now=100.0).allowed
