"""Process-local sliding-window rate limiter for the upload endpoint.

The composer is single-process FastAPI / uvicorn — there is no external
Redis or memcached, and we don't need cluster-wide accuracy. A dict of
``client_id -> deque[float]`` is enough to:

* Reject runaway upload loops from a single client (deliberate or
  accidental).
* Stay correct under concurrent requests (`threading.Lock` around the
  dict mutation; the per-window timestamps live in plain `deque`s).

The window is a rolling N seconds (60s by default) — old timestamps are
trimmed lazily on each acquire(). The data structure self-cleans for
clients that go quiet, since their entries get popped as their window
slides off without anything taking their place.

Why not slowapi or fastapi-limiter? They're great, but require either a
Redis dependency or middleware setup that fights the rest of the app's
plain-uvicorn deployment story. A 30-line in-process limiter is a better
fit for the laptop-scale target.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single :meth:`SlidingWindowLimiter.check` call.

    ``allowed=True``  → caller may proceed; ``hits`` is the new count
                       in the current window after admission.
    ``allowed=False`` → caller exceeded the limit; ``retry_after`` is a
                       small positive float telling the client (and
                       Retry-After header) how many seconds until the
                       oldest hit ages out and capacity reopens.
    """

    allowed: bool
    hits: int
    limit: int
    retry_after: float = 0.0


class SlidingWindowLimiter:
    """Per-key rolling-window counter.

    ``window_seconds`` is the length of the rolling window; ``capacity``
    is the maximum number of admissions per key per window. Setting
    ``capacity=0`` disables admission gating entirely (every check
    returns ``allowed=True``) — useful for tests and for users who want
    to opt out via :class:`AppSettings.media_upload_rate_per_minute=0`.
    """

    def __init__(self, *, window_seconds: float = 60.0, capacity: int = 20) -> None:
        self.window_seconds = float(window_seconds)
        self.capacity = int(capacity)
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self, key: str, *, now: Optional[float] = None
    ) -> RateLimitDecision:
        """Record a hit for ``key`` if capacity remains; otherwise reject."""
        if self.capacity <= 0:
            return RateLimitDecision(allowed=True, hits=0, limit=0)
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window_seconds
        with self._lock:
            bucket = self._buckets[key]
            # Trim timestamps that have aged out of the window.
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.capacity:
                # Oldest hit determines when the next admission opens.
                oldest = bucket[0]
                retry_after = max(0.0, oldest + self.window_seconds - ts)
                return RateLimitDecision(
                    allowed=False,
                    hits=len(bucket),
                    limit=self.capacity,
                    retry_after=retry_after,
                )
            bucket.append(ts)
            return RateLimitDecision(
                allowed=True,
                hits=len(bucket),
                limit=self.capacity,
            )

    def reset(self, key: Optional[str] = None) -> None:
        """Drop counters for one key or (if ``key is None``) all keys.

        Used by the test suite to keep cases independent and by an
        operator wanting to clear a stuck client without restarting.
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# Process-wide limiter for the media-upload endpoint. Capacity is read
# from settings on every check() call (via configure_for) so a settings
# change takes effect on the next request without restarting the server.
upload_limiter = SlidingWindowLimiter(window_seconds=60.0, capacity=20)


def configure_for(*, capacity: int) -> None:
    """Update the upload limiter's capacity in place.

    Called from :mod:`app.controllers.media` before each upload check
    so :class:`AppSettings.media_upload_rate_per_minute` changes apply
    immediately.
    """
    upload_limiter.capacity = max(0, int(capacity))
