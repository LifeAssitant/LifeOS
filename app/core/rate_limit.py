"""Simple in-process rate limiter for hosted Gemini chats."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from uuid import UUID

from fastapi import HTTPException


class RateLimiter:
    def __init__(self, max_calls: int = 30, window_seconds: int = 60) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] > self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                raise HTTPException(
                    status_code=429,
                    detail="Too many AI requests. Slow down a moment.",
                )
            bucket.append(now)


hosted_chat_limiter = RateLimiter(max_calls=20, window_seconds=60)


def enforce_hosted_chat_limit(user_id: UUID) -> None:
    hosted_chat_limiter.check(f"chat:{user_id}")
