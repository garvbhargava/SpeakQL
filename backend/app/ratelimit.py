"""Rate limits (Backend Plan §7.4).

One module, and **every limit is a configuration value rather than a
literal** — so tightening one during a demo is an environment change, not a
deployment.

Three paths are limited, and they are the three that cost something real:

    ask      every question runs the local model and may escalate to a billed one
    upload   disk, and an index rebuild
    export   a second full read of the result

Sign-in has two more (§7.3), keyed by address and by client IP rather than by
account, because the person asking for a code does not have an account yet:

    code per address   10 an hour -- bounds mail-bombing one stranger
    code per IP        30 an hour -- bounds mail-bombing many of them

Guessing a code is bounded separately, by the lockout in auth/otp.py.

A refused request still writes its `query_log` row. Rate limiting is an
outcome, not an absence of one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass, field
from enum import Enum


class Limited(Enum):
    ASK = "ask"
    UPLOAD = "upload"
    EXPORT = "export"
    CODE_PER_ADDRESS = "sign-in codes for this address"
    CODE_PER_IP = "sign-in codes from this network"


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
                 export_per_hour: int, codes_per_address_hour: int = 10,
                 codes_per_ip_hour: int = 30) -> None:
        self._limits = {
            Limited.ASK: (ask_per_hour, 3600, "hour"),
            Limited.UPLOAD: (upload_per_day, 86400, "day"),
            Limited.EXPORT: (export_per_hour, 3600, "hour"),
            Limited.CODE_PER_ADDRESS: (codes_per_address_hour, 3600, "hour"),
            Limited.CODE_PER_IP: (codes_per_ip_hour, 3600, "hour"),
        }
        self._buckets: dict[tuple[Hashable, Limited], _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, person_id: Hashable, what: Limited) -> Decision:
        """Test and consume in one step, under a lock.

        Separating "check" from "consume" invites the race where two requests
        both see the last slot. `person_id` is whatever identifies the caller
        for this limit: a person, or for sign-in an address or an IP.
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

    def peek(self, person_id: Hashable, what: Limited) -> int:
        """How many remain, without consuming one."""
        limit, window, _ = self._limits[what]
        now = time.time()
        with self._lock:
            bucket = self._buckets.get((person_id, what))
            if bucket is None:
                return limit
            live = [t for t in bucket.hits if t > now - window]
            return max(0, limit - len(live))

    def reset(self, person_id: Hashable | None = None) -> None:
        """Tests, and the demo reset button."""
        with self._lock:
            if person_id is None:
                self._buckets.clear()
            else:
                for key in [k for k in self._buckets if k[0] == person_id]:
                    del self._buckets[key]
