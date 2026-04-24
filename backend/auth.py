"""
Authentication and rate-limiting middleware helpers — v0.9.0.

HTTP Basic Auth
---------------
Enabled only when ``settings.demo_password`` is set (not None).
When enabled, every request must include a valid Authorization header.
Username is ignored; only the password is checked via secrets.compare_digest
(constant-time, prevents timing attacks).

Rate Limiter
------------
Token-bucket style: each IP address is allowed at most
``settings.rate_limit_per_minute`` requests per 60-second rolling window.
Uses an OrderedDict as an LRU cache capped at ``settings.rate_limit_lru_max``
to bound memory on high-traffic hosts.  The oldest entry is evicted when the
cap is reached.

IP resolution
-------------
When ``settings.trust_proxy_headers`` is True the leftmost non-empty value in
X-Forwarded-For is used as the client IP (RunPod / Nginx proxy aware).
Otherwise request.client.host is used directly.

X-Requested-With CSRF guard
----------------------------
``require_geoai_header`` enforces that the custom header
``X-Requested-With: geoai`` is present, blocking cross-origin form posts and
other CSRF vectors that cannot set custom headers.
"""

from __future__ import annotations

import collections
import secrets
import time
from typing import Deque

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from backend.config import settings

_security = HTTPBasic(auto_error=False)


# ---------------------------------------------------------------------------
# IP resolution
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Return the real client IP, proxy-aware."""
    if settings.trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # Leftmost entry is the original client.
            return xff.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


# ---------------------------------------------------------------------------
# HTTP Basic Auth
# ---------------------------------------------------------------------------


def require_basic_auth(request: Request) -> None:
    """
    Raise HTTP 401 if demo_password is set and the request lacks valid creds.

    When demo_password is None (default), this is a no-op — the endpoint is
    open.  Set DEMO_PASSWORD in the environment to require auth.
    """
    if settings.demo_password is None:
        return

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic realm=\"GeoAssess\""},
        )

    import base64
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
        _username, _sep, password = decoded.partition(":")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Authorization header",
            headers={"WWW-Authenticate": "Basic realm=\"GeoAssess\""},
        )

    if not secrets.compare_digest(password.encode(), settings.demo_password.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"GeoAssess\""},
        )


# ---------------------------------------------------------------------------
# X-Requested-With CSRF guard
# ---------------------------------------------------------------------------


def require_geoai_header(request: Request) -> None:
    """
    Raise HTTP 403 if the custom CSRF header is absent.

    Browsers cannot set arbitrary headers on cross-origin requests without a
    CORS preflight, so requiring ``X-Requested-With: geoai`` blocks naive CSRF.
    Same-origin fetch() and XMLHttpRequest both include it after the frontend
    sets it explicitly.
    """
    value = request.headers.get("x-requested-with", "")
    if value.strip().lower() != "geoai":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid X-Requested-With header",
        )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """
    Per-IP sliding-window rate limiter backed by an LRU OrderedDict.

    Args:
        max_per_minute: Maximum requests allowed per 60-second window.
        lru_max: Maximum number of IPs to track before evicting the oldest.
    """

    def __init__(self, max_per_minute: int, lru_max: int) -> None:
        self._max = max_per_minute
        self._lru_max = lru_max
        # ip -> deque of timestamps (float, epoch seconds)
        self._windows: collections.OrderedDict[str, Deque[float]] = (
            collections.OrderedDict()
        )

    def is_allowed(self, ip: str) -> bool:
        """Return True if the request is within the rate limit for *ip*."""
        now = time.monotonic()
        cutoff = now - 60.0

        if ip in self._windows:
            # Move to end (LRU: most recently used)
            self._windows.move_to_end(ip)
        else:
            if len(self._windows) >= self._lru_max:
                # Evict the least-recently-used entry
                self._windows.popitem(last=False)
            self._windows[ip] = collections.deque()

        window = self._windows[ip]
        # Purge timestamps older than the rolling window
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self._max:
            return False

        window.append(now)
        return True

    def clear(self) -> None:
        """Remove all tracked IPs. Used by tests to reset state between runs."""
        self._windows.clear()


# Module-level singleton — instantiated once from settings so all requests
# share a single bucket store.  Tests reset it via limiter.clear() or by
# monkeypatching ``backend.app.rate_limiter``.
rate_limiter = RateLimiter(
    max_per_minute=settings.rate_limit_per_minute,
    lru_max=settings.rate_limit_lru_max,
)


def require_rate_limit(request: Request) -> None:
    """Raise HTTP 429 if the client IP has exceeded the rate limit."""
    ip = _client_ip(request)
    if not rate_limiter.is_allowed(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded — please wait before retrying",
            headers={"Retry-After": "60"},
        )
