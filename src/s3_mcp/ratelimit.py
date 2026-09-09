"""Per-caller rate limiting.

The API gateway already throttles the endpoint as a whole, but a stage-wide
limit is shared: one client stuck in a retry loop can consume the entire budget
and every other customer gets 429s it did not cause. This limits each caller
separately, so a single misbehaving client degrades only itself.

It also protects what the gateway cannot see. Every authenticated call reaches
the portal's MySQL database and most of them download an object from S3; those
are the resources worth defending, and they are behind the gateway, not in
front of it.

Two tiers, because the interesting abuse happens before anyone is identified:

  by IP       applied to every request, including the unauthenticated discovery
              documents and any request whose token turns out to be bad. This is
              the only limit that can apply to an attacker who has no account.
  by subject  applied once the token is verified. Generous — a real analytics
              conversation fires a burst of tool calls — but bounded.

Honest about its scope: the buckets live in the process, so with several Lambda
instances warm the effective ceiling is (instances x limit). That is deliberate.
A shared store would mean a DynamoDB round trip on every request to defend
against a case the gateway throttle and the function's reserved concurrency
already bound; the aggregate is capped either way. Move `_Buckets` behind
DynamoDB if you ever need the limit to be exact rather than sufficient.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .settings import get_settings


@dataclass
class _Bucket:
    """A token bucket: `tokens` refills at `rate` up to `capacity`.

    Chosen over a fixed window because a window lets a caller spend its whole
    allowance in the last instant of one window and again in the first instant
    of the next, which is twice the intended rate at the worst moment."""

    tokens: float
    updated: float


class _Buckets:
    """Bounded map of key -> bucket.

    Bounded matters: the key is a client IP, so an attacker choosing keys is an
    attacker choosing how much memory we allocate. When the map is full the
    idlest entries are dropped, which for a flood of one-shot IPs means the
    entries dropped are exactly the ones that will not be seen again."""

    def __init__(self, capacity: float, rate: float, max_keys: int = 10_000) -> None:
        self._capacity = capacity
        self._rate = rate
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Spend one token. Returns 0 if allowed, else seconds until one frees."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict(now)
                bucket = _Bucket(tokens=self._capacity, updated=now)
                self._buckets[key] = bucket

            bucket.tokens = min(
                self._capacity, bucket.tokens + (now - bucket.updated) * self._rate
            )
            bucket.updated = now

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return 0.0
            return (1 - bucket.tokens) / self._rate

    def _evict(self, now: float) -> None:
        # Drop everything that has had time to refill completely: a full bucket
        # is indistinguishable from one that has never been used, so forgetting
        # it changes no decision.
        full_after = self._capacity / self._rate
        for key in [k for k, b in self._buckets.items() if now - b.updated > full_after]:
            del self._buckets[key]
        if len(self._buckets) >= self._max_keys:
            # Still full — evict the least recently seen quarter.
            oldest = sorted(self._buckets.items(), key=lambda kv: kv[1].updated)
            for key, _ in oldest[: self._max_keys // 4]:
                del self._buckets[key]


@dataclass
class _Limiters:
    anonymous: _Buckets
    identified: _Buckets


_limiters: _Limiters | None = None
_init_lock = threading.Lock()


def _get() -> _Limiters:
    global _limiters
    if _limiters is None:
        with _init_lock:
            if _limiters is None:
                s = get_settings()
                _limiters = _Limiters(
                    anonymous=_Buckets(
                        capacity=float(s.rate_limit_anon_burst),
                        rate=s.rate_limit_anon_per_minute / 60.0,
                    ),
                    identified=_Buckets(
                        capacity=float(s.rate_limit_user_burst),
                        rate=s.rate_limit_user_per_minute / 60.0,
                    ),
                )
    return _limiters


def check_address(ip: str) -> float:
    """Rate limit by source address. 0 means allowed."""
    if not get_settings().rate_limit_enabled or not ip:
        return 0.0
    return _get().anonymous.take(ip)


def check_subject(subject: str) -> float:
    """Rate limit by authenticated subject. 0 means allowed."""
    if not get_settings().rate_limit_enabled or not subject:
        return 0.0
    return _get().identified.take(subject)


def reset() -> None:
    """Drop all state. Tests only."""
    global _limiters
    _limiters = None
