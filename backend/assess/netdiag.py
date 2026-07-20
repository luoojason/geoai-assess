"""Plain-language classification of data-source network failures, plus a
process-wide registry the step runners pop to surface per-layer fetch errors
in the wizard (fetchers deliberately never raise — see osm_layers)."""
from __future__ import annotations

import threading

_errors: dict[str, str] = {}
_lock = threading.Lock()


def friendly_net_reason(exc: Exception) -> str:
    """One sentence a non-developer can act on, from a raw network exception."""
    s = str(exc)
    name = type(exc).__name__
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
    with _lock:
        _errors[layer_fn] = friendly_net_reason(exc)


def take_fetch_errors() -> dict[str, str]:
    """Pop every recorded failure (keyed by fetcher function name)."""
    with _lock:
        out = dict(_errors)
        _errors.clear()
        return out
