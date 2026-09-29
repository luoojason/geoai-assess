"""Plain-language classification of data-source network failures, plus a
process-wide registry the step runners pop to surface per-layer fetch errors
in the wizard (fetchers deliberately never raise — see osm_layers)."""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager

_errors: dict[str, str] = {}
_lock = threading.Lock()

# When set, record_fetch_error writes here instead of the process-wide registry.
# The HCAS step runners drain the global registry between sequential steps, but
# /compare runs two assessments at once — a shared registry would let one area's
# failure be popped by the other, and the robbed area would then claim a clean
# run. A contextvar keeps each concurrent assessment's failures its own
# (asyncio Tasks get their own context copy, and asyncio.to_thread carries the
# context into the worker thread).
_scope: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "netdiag_scope", default=None
)


def friendly_net_reason(exc: Exception) -> str:
    """One sentence a non-developer can act on, from a raw network exception."""
    # Failures the app chose on the server's behalf (osm_layers.OverpassBusy:
    # "asked us to wait", resting a refusing host) already carry plain words.
    plain = getattr(exc, "plain_reason", None)
    if isinstance(plain, str) and plain:
        return plain
    s = str(exc)
    name = type(exc).__name__
    if name == "BudgetExpired":
        return (
            "the OpenStreetMap servers did not deliver this layer within the "
            "step's time (they were busy or turning requests away). Try again in "
            "a few minutes."
        )
    if "responded: 429" in s:
        return (
            "the OpenStreetMap server asked us to slow down (too many requests); "
            "the app backed off, but it did not clear in this step's time. Try "
            "again in a few minutes."
        )
    if "getaddrinfo failed" in s or "Failed to resolve" in s or "NameResolution" in s:
        return (
            "DNS lookup failed — this computer could not translate the server's "
            "name into an address. Check the system DNS settings (a leftover "
            "VPN/proxy DNS is a common cause)."
        )
    if "10065" in s or "unreachable" in s.lower():
        return (
            "server unreachable — the network refused to route the connection. "
            "Check internet, VPN, or firewall settings."
        )
    if "Timeout" in name or "timed out" in s.lower():
        return "the server did not respond in time (connection timed out)."
    if "Failed to establish a new connection" in s or "Connection refused" in s:
        return (
            "could not connect to the server — check internet, VPN, or proxy "
            "settings."
        )
    return f"request failed ({name})."


def record_fetch_error(layer_fn: str, exc: Exception) -> None:
    reason = friendly_net_reason(exc)
    sink = _scope.get()
    if sink is not None:
        sink[layer_fn] = reason
        return
    with _lock:
        _errors[layer_fn] = reason


@contextmanager
def collect_fetch_errors():
    """Route record_fetch_error into a private dict for the duration of the
    block, so concurrent runs cannot steal each other's failures.

    Yields the dict; it is populated in place as fetchers fail, so read it
    *after* the block (or after the fetches complete) to see everything.
    """
    sink: dict[str, str] = {}
    token = _scope.set(sink)
    try:
        yield sink
    finally:
        _scope.reset(token)


def take_fetch_errors() -> dict[str, str]:
    """Pop every recorded failure (keyed by fetcher function name)."""
    with _lock:
        out = dict(_errors)
        _errors.clear()
        return out


def pop_fetch_error(layer_fn: str) -> str | None:
    """Pop one fetcher's failure, or None if it succeeded.

    Lets a step runner classify each layer the moment ITS fetch returns instead
    of waiting for the whole batch. Prof. Shi (2026-08-07) saw layers reported
    as "done · 0 features" while they had in fact failed, because the error flag
    was only applied after every layer finished — so a stalled run looked like a
    finished one.
    """
    sink = _scope.get()
    if sink is not None:  # inside collect_fetch_errors(), same rule as recording
        return sink.pop(layer_fn, None)
    with _lock:
        return _errors.pop(layer_fn, None)
