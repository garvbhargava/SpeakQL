"""Rate limits (Backend Plan §7.4).

One module, and **every limit is a configuration value rather than a
literal** — so tightening one during a demo is an environment change, not a
deployment.

Three paths are limited, and they are the three that cost something real:

    ask      every question runs the local model and may escalate to a billed one
    upload   disk, and an index rebuild
    export   a second full read of the result

Login is limited separately, by the OTP lockout in auth/otp.py, because the
control there is per-address rather than per-account: an attacker guessing
codes does not have an account yet.

A refused request still writes its `query_log` row. Rate limiting is an
outcome, not an absence of one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class Limited(Enum):
    ASK = "ask"
    UPLOAD = "upload"
    EXPORT = "export"


@dataclass
class Decision:
    allowed: bool
    remaining: int
    retry_after_seconds: int = 0
    limit: int = 0
    window: str = ""

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        minutes = max(1, round(self.retry_after_seconds / 60))
        return (
            f"You have used all {self.limit} of this {self.window}. "
            f"Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
        )


@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)


class RateLimiter:
    """A sliding window, in process.

    In process is the right call for a single-container deployment and the
    wrong one for several — the note is here rather than in a comment further
    away because it is the first thing to change when this is scaled, and the
    fix is to move these buckets into Postgres or Redis without touching any
    call site.
    """

    def __init__(self, *, ask_per_hour: int, upload_per_day: int,
                 export_per_hour: int) -> None:
        self._limits = {
            Limited.ASK: (ask_per_hour, 3600, "hour"),
            Limited.UPLOAD: (upload_per_day, 86400, "day"),
            Limited.EXPORT: (export_per_hour, 3600, "hour"),
        }
        self._buckets: dict[tuple[int, Limited], _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, person_id: int, what: Limited) -> Decision:
        """Test and consume in one step, under a lock.

        Separating "check" from "consume" invites the race where two requests
        both see the last slot.
        """
        limit, window, label = self._limits[what]
        now = time.time()
        key = (person_id, what)

        with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket())
            cutoff = now - window
            bucket.hits = [t for t in bucket.hits if t > cutoff]

            if len(bucket.hits) >= limit:
                oldest = min(bucket.hits)
                return Decision(
                    allowed=False,
                    remaining=0,
                    retry_after_seconds=int(oldest + window - now) + 1,
                    limit=limit,
                    window=label,
                )

            bucket.hits.append(now)
            return Decision(
                allowed=True,
                remaining=limit - len(bucket.hits),
                limit=limit,
                window=label,
            )

    def peek(self, person_id: int, what: Limited) -> int:
        """How many remain, without consuming one."""
        limit, window, _ = self._limits[what]
        now = time.time()
        with self._lock:
            bucket = self._buckets.get((person_id, what))
            if bucket is None:
                return limit
            live = [t for t in bucket.hits if t > now - window]
            return max(0, limit - len(live))

    def reset(self, person_id: int | None = None) -> None:
        """Tests, and the demo reset button."""
        with self._lock:
            if person_id is None:
                self._buckets.clear()
            else:
                for key in [k for k in self._buckets if k[0] == person_id]:
                    del self._buckets[key]
