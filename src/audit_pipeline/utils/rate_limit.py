"""Rate limiters and retry-with-backoff decorators.

Three building blocks:

  RateLimiter      — sliding-window throttle, blocks until a slot frees up
  retry_with_backoff — decorator that retries on transient failures
  resilient_request   — combined GET/POST helper for HTTP APIs

The pipeline talks to three external surfaces (Anthropic, GitHub, Solana
RPC), each with its own preferred cadence. Use one RateLimiter per surface.
"""

from __future__ import annotations

import functools
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import requests


class RateLimiter:
    """Sliding-window rate limiter (thread-safe).

    >>> rl = RateLimiter(max_calls=50, per_seconds=60)  # 50 req/min
    >>> rl.acquire()  # blocks if needed, returns when a slot is free
    """

    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls = int(max_calls)
        self.window = float(per_seconds)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and (now - self._calls[0]) > self.window:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.01
            time.sleep(max(wait, 0.01))


def retry_with_backoff(
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    retry_on: Iterable[type[BaseException]] = (Exception,),
    retry_on_predicate: Callable[[BaseException], bool] | None = None,
) -> Callable:
    """Decorator: retry with exponential backoff + jitter.

    >>> @retry_with_backoff(max_attempts=3)
    ... def fetch(url): ...
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except tuple(retry_on) as e:
                    if retry_on_predicate and not retry_on_predicate(e):
                        raise
                    last = e
                    if attempt == max_attempts:
                        break
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay = delay * (0.5 + random.random())  # jitter
                    time.sleep(delay)
            assert last is not None
            raise last
        return wrapper
    return deco


def is_transient_http_error(e: BaseException) -> bool:
    """Predicate for retry_with_backoff to retry only on transient errors."""
    if isinstance(e, requests.exceptions.Timeout):
        return True
    if isinstance(e, requests.exceptions.ConnectionError):
        return True
    if isinstance(e, requests.exceptions.HTTPError):
        status = getattr(e.response, "status_code", None)
        if status in (429, 502, 503, 504):
            return True
    return False


_DEFAULT_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(name: str, max_calls: int, per_seconds: float) -> RateLimiter:
    """Singleton-per-name rate limiter (so all callers share a budget)."""
    if name not in _DEFAULT_LIMITERS:
        _DEFAULT_LIMITERS[name] = RateLimiter(max_calls, per_seconds)
    return _DEFAULT_LIMITERS[name]


@retry_with_backoff(
    max_attempts=4,
    retry_on=(requests.exceptions.RequestException,),
    retry_on_predicate=is_transient_http_error,
)
def resilient_get(url: str, **kwargs: Any) -> requests.Response:
    """requests.get with built-in retry on transient failures."""
    r = requests.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    r.raise_for_status()
    return r


@retry_with_backoff(
    max_attempts=4,
    retry_on=(requests.exceptions.RequestException,),
    retry_on_predicate=is_transient_http_error,
)
def resilient_post(url: str, **kwargs: Any) -> requests.Response:
    """requests.post with built-in retry on transient failures."""
    r = requests.post(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    r.raise_for_status()
    return r
