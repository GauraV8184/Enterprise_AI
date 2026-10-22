"""Per-caller request budgets.

The platform spends real money on every model call and writes real rows on every
upload, so a single authenticated caller should not be able to turn one token
into an unbounded bill or an unbounded queue. Two buckets do that: a generous
``standard`` budget for reads, metadata and deletes, and a much smaller ``model`` budget
for endpoints that call an LLM or an embedding model.

**Counted per identity, not per IP.** Behind a proxy every request may arrive
from the same address, and an IP is not what the quota protects; the verified
user id is. Development-stub identities are counted too, so the limit behaves the
same way before authentication is wired up.

**This is a single-process limiter, and that is stated rather than implied.**
The counters live in the process that serves the request. With N worker
processes the effective budget is N times the configured value, and a restart
forgets the window. That is acceptable for the current single-instance
deployment and is the reason Phase 9 introduces Redis; claiming otherwise would
overstate what this code guarantees.

**A rejected request costs nothing.** The check happens before the handler runs,
so a 429 never reaches a model, a database write, or an audit insert.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from fastapi import Request

from app.core.errors import AppError
from app.security.identity import CurrentUserDep

STANDARD_TIER = "standard"
MODEL_TIER = "model"
WINDOW_SECONDS = 60

_SWEEP_INTERVAL_SECONDS = 60.0


class RateLimitedError(AppError):
    """The caller exhausted one of their budgets."""

    status_code = 429
    code = "rate_limited"
    public_message = "Too many requests. Retry shortly."

    def __init__(self, *, tier: str, limit: int, retry_after: int) -> None:
        super().__init__(
            f"Rate limit exceeded: at most {limit} {tier} requests per "
            f"{WINDOW_SECONDS} seconds. Retry in {retry_after} second(s).",
            details={
                "tier": tier,
                "limit": limit,
                "window_seconds": WINDOW_SECONDS,
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )


class RateLimiter:
    """Sliding-window counters keyed by ``(identity, tier)``.

    A sliding window is used instead of a fixed minute so a caller cannot send
    the full budget at 12:00:59 and again at 12:01:00. The state is one deque of
    monotonic timestamps per active identity and tier; entries are pruned on
    every check, and empty buckets are swept once a window so a long-lived
    process does not accumulate dead keys.

    ``threading.Lock`` is not paranoia: FastAPI runs synchronous dependencies in
    a worker thread, so two requests for the same identity can check and append
    concurrently. The lock makes ``check`` atomic.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        standard_per_minute: int,
        model_per_minute: int,
    ) -> None:
        self._enabled = enabled
        self._limits = {
            STANDARD_TIER: standard_per_minute,
            MODEL_TIER: model_per_minute,
        }
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, *, identity: str, tier: str) -> None:
        """Record one request, or raise :class:`RateLimitedError`."""
        if not self._enabled:
            return

        limit = self._limits[tier]
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        key = (identity, tier)

        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = max(1, math.ceil(hits[0] + WINDOW_SECONDS - now))
                raise RateLimitedError(tier=tier, limit=limit, retry_after=retry_after)

            hits.append(now)
            self._sweep(now)

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - WINDOW_SECONDS
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def limits(self) -> dict[str, int]:
        return dict(self._limits)


def get_rate_limiter(request: Request) -> RateLimiter:
    """The limiter built once in ``create_app``."""
    return request.app.state.rate_limiter


def rate_limit(tier: str):
    """Build a dependency that charges one request to ``tier``."""

    def dependency(request: Request, user: CurrentUserDep) -> None:
        get_rate_limiter(request).check(identity=user.user_id, tier=tier)

    return dependency


standard_rate_limit = rate_limit(STANDARD_TIER)
model_rate_limit = rate_limit(MODEL_TIER)
