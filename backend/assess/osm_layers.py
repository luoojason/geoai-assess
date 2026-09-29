"""OSM data fetchers for the assessment pipeline.

Each function accepts a bbox as (west, south, east, north) in EPSG:4326
(lon_min, lat_min, lon_max, lat_max) and returns a GeoDataFrame in the
requested projected CRS. Returns an empty GeoDataFrame (never raises) when
no features are found for the area.
"""
from __future__ import annotations

import logging
import math
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import geopandas as gpd
import osmnx as ox
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError

from backend.assess.netdiag import record_fetch_error

logger = logging.getLogger(__name__)

# Prof. Shi's field test (2026-07-20): overpass-api.de was unreachable from his
# network while other hosts worked. On the first network failure we switch the
# whole process to the mirror and retry — sticky, so later fetchers start there.
_OVERPASS_PRIMARY = "https://overpass-api.de/api"
_OVERPASS_MIRROR = "https://overpass.kumi.systems/api"
_OVERPASS_MIRROR2 = "https://overpass.private.coffee/api"
# The two servers BEHIND overpass-api.de, addressed directly. Measured 2026-09-16:
#
#   overpass-api.de          -> 162.55.144.139, 65.109.112.52
#   z.overpass-api.de        -> 162.55.144.139
#   lz4.overpass-api.de      -> 65.109.112.52
#   overpass.kumi.systems    -> 193.219.97.30
#   overpass.private.coffee  -> 193.219.97.30
#
# So the old three-endpoint rotation was really TWO machines, and its two
# "mirrors" were one box under two names: when that box is sick, failing over
# from one name to the other is failing over to itself. That morning both names
# read-timed-out at 40 s while z. and lz4. answered the same query in about a
# second, and Prof. Shi's step 3 had lost every layer an hour earlier.
#
# The dead-address filter below only handles a backend that refuses the TCP
# handshake. A backend that ACCEPTS and then 504s is invisible to it, and the
# DNS name hands every request to a coin flip between the two — naming them
# individually is what lets the rotation step off the one that is 504ing.
_OVERPASS_Z = "https://z.overpass-api.de/api"
_OVERPASS_LZ4 = "https://lz4.overpass-api.de/api"

# Tried in order, rotating on failure. Three, not two, because on 2026-08-07 a
# real query measured 21.7 s on the primary, 504 on kumi, and 4.2 s on
# private.coffee — the endpoint we had as our only fallback was the sick one, so
# a two-host rotation had nowhere good to go.
#
# Endpoints are added only if they serve GLOBAL data. overpass.osm.ch answered
# the same probe in 0.7 s with ZERO ways: it is a Switzerland-only extract, so
# adding it would turn "wrong server" into "this county has no buildings" —
# silently, which is the exact failure class this file exists to prevent.
#
# The same-cluster servers come before the other box: they are distinct
# machines, and on 2026-09-16 they were the ones answering.
_OVERPASS_ENDPOINTS = [_OVERPASS_PRIMARY, _OVERPASS_Z, _OVERPASS_LZ4,
                       _OVERPASS_MIRROR2, _OVERPASS_MIRROR]

# --- addresses that will not accept a connection ----------------------------
# An endpoint hostname publishes several addresses and they are not equally
# usable. Measured 2026-08-11, five TCP handshakes each:
#
#   overpass-api.de -> 162.55.144.139   connected 5/5 in 0.1 s
#   overpass-api.de ->  65.109.112.52   timed out  5/5 at 8.0 s
#
# DNS round-robins between them, so about half of all connections hung for the
# full connect timeout and were counted as network failures — whatever the
# query, the tile size, or the server's load. That is the whole of the
# "accepted a one-node probe seconds earlier, then refused to complete the TCP
# handshake for the real query" report above: it was never the query, it was
# which address that particular connection happened to get. The probe cannot
# see it either, because the probe opens ONE connection and may well get the
# good one. Pinned to the reachable address, the whole-county buildings query
# answered in 1.8 s (51,461 elements) and landuse in 2.3 s (69,863); unpinned,
# the same queries failed 14 times out of 14 minutes earlier.
#
# The verdict EXPIRES, and that matters more than it looks. Half an hour later
# the same measurement put 162.55.144.139 in the dead column and 65.109.112.52
# back in service — the roles had swapped. So this is not a permanently broken
# route: it is a backend refusing connections from a client that has spent its
# allowance, and it moves as load moves. A verdict cached for the life of the
# process would eventually pin the app to an address that has since died, which
# is the very failure this code exists to prevent.
#
# requests/urllib3 resolve through socket.getaddrinfo, so that is the only seam
# that can steer them without reimplementing the transport.
_ADDR_PROBE_TIMEOUT_S = float(os.environ.get("OSM_ADDR_PROBE_TIMEOUT_S", "3"))
_ADDR_RECHECK_S = float(os.environ.get("OSM_ADDR_RECHECK_S", "300"))

_dead_addresses: set[str] = set()
_addr_checked: dict[str, float] = {}      # host -> when its verdict goes stale
_addr_lock = threading.Lock()
_real_getaddrinfo = socket.getaddrinfo


def _overpass_hostnames() -> set[str]:
    """Just the hostnames of our own endpoints — nothing else may be filtered."""
    from urllib.parse import urlsplit

    return {urlsplit(e).hostname for e in _OVERPASS_ENDPOINTS}


def _address_is_reachable(ip: str, port: int = 443,
                          timeout: float | None = None) -> bool:
    """Can we complete a TCP handshake with this address? No TLS, no query — a
    host that is merely busy still completes a handshake, so this separates
    'unreachable' from 'slow', which is exactly the distinction the fetchers
    could not make."""
    s = socket.socket()
    s.settimeout(_ADDR_PROBE_TIMEOUT_S if timeout is None else timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _unreachable_addresses(host: str, resolve=None, reachable=None) -> set[str]:
    """Which of `host`'s addresses never answer. Injectable for tests.

    Fails OPEN: if NOTHING answers the host is simply down, and dropping every
    address would turn a transient outage into a permanent one for the life of
    the process. Only a host with both working and dead addresses is filtered —
    which is the case this exists for.
    """
    resolve = resolve or (lambda h: sorted({
        info[4][0] for info in _real_getaddrinfo(h, 443, socket.AF_INET)
    }))
    reachable = reachable or _address_is_reachable
    try:
        addresses = list(resolve(host))
    except Exception:
        return set()
    if len(addresses) < 2:
        return set()          # nothing to choose between
    dead = {ip for ip in addresses if not reachable(ip)}
    return set() if len(dead) == len(addresses) else dead


def _overpass_getaddrinfo(host, port, *args, **kwargs):
    """socket.getaddrinfo, minus the addresses we have proven do not connect.

    Process-wide, so it is inert for every hostname that is not one of ours,
    and it fails open if the filter would leave nothing.
    """
    infos = _real_getaddrinfo(host, port, *args, **kwargs)
    if host not in _overpass_hostnames() or not _dead_addresses:
        return infos
    usable = [i for i in infos if i[4][0] not in _dead_addresses]
    return usable or infos


def _resolved_addresses(host: str) -> set[str]:
    try:
        return {info[4][0] for info in _real_getaddrinfo(host, 443, socket.AF_INET)}
    except Exception:
        return set()


def _learn_dead_addresses(host: str | None) -> None:
    """Re-check a host's addresses whenever its verdict has gone stale.

    Re-checking, rather than deciding once, is what keeps this from becoming the
    bug it fixes: the refusing backend changes over time (measured 2026-08-11 —
    see above), so a permanent verdict would eventually route every request to
    an address that has since stopped answering.
    """
    if not host:
        return
    now = time.time()
    with _addr_lock:
        if _addr_checked.get(host, 0.0) > now:
            return
        _addr_checked[host] = now + _ADDR_RECHECK_S
    dead = _unreachable_addresses(host)
    with _addr_lock:
        # Retire this host's previous verdict before recording the new one, so a
        # backend that has started taking traffic again is not shut out forever.
        _dead_addresses.difference_update(_resolved_addresses(host))
        _dead_addresses.update(dead)
    if dead:
        logger.warning(
            f"Overpass: {host} publishes {len(dead)} address(es) that do not accept "
            f"a connection ({', '.join(sorted(dead))}) — skipping them. Left in the "
            "rotation, each one costs a full connect timeout on roughly every other "
            "request, which reads as a server outage rather than a dead route."
        )
    if socket.getaddrinfo is not _overpass_getaddrinfo:
        socket.getaddrinfo = _overpass_getaddrinfo


# --- step budget ------------------------------------------------------------
# The step abandons a layer's future when its wall-clock budget expires, but
# abandoning a future does not stop the thread behind it: hcas._fetch_pass
# deliberately does not join its workers (a hung socket would put the block back
# exactly where it was removed from). Nothing below used to know the budget
# existed, so an abandoned layer kept quartering and retrying indefinitely.
#
# Measured 2026-08-11: a server reporting no run in progress was still holding
# 14 Overpass sockets and opening more. Overpass allows ~2 slots per IP, so
# those zombies consumed the app's entire allowance and every RE-run started
# starved — which is why the stall reproduced every time and never got further,
# defeating the "each attempt gets further" retry design.
#
# The deadline is thread-local: layers run concurrently and each worker sets its
# own, so one layer's budget can never leak into another thread.
_budget = threading.local()


class BudgetExpired(RuntimeError):
    """The step's wall-clock budget ran out while this fetch was still running.

    Nothing is waiting for the result any more, so the work stops here rather
    than continuing to spend the process's Overpass allowance on it.
    """


def set_deadline(when: float | None) -> None:
    """Bound the current thread's fetching to a `time.time()` deadline."""
    _budget.deadline = when


def clear_deadline() -> None:
    set_deadline(None)


def current_deadline() -> float | None:
    return getattr(_budget, "deadline", None)


def _budget_expired() -> bool:
    deadline = current_deadline()
    return deadline is not None and time.time() >= deadline


def _stop_if_expired() -> None:
    if _budget_expired():
        raise BudgetExpired(
            "the step's time budget ran out before this area could be fetched"
        )


# --- known outage -----------------------------------------------------------
# Set when the pre-flight probe finds that NO endpoint answers. The retry and
# splitting machinery exists to get around ONE sick server; when every server is
# refusing, all it can do is multiply the wait before reporting the same
# failure. Process-wide, like the rest of the probe's findings, and re-decided
# whenever the probe runs again.
_outage_suspected_flag = False


def _outage_suspected() -> bool:
    return _outage_suspected_flag


def _next_endpoint(used: str) -> str:
    """The next endpoint to try, skipping any the probe found out of date.

    Rotation on failure has to honour the freshness gate too: without this, the
    probe could correctly refuse a stale mirror and the very first timeout would
    hand the run straight back to it.
    """
    usable = [e for e in _OVERPASS_ENDPOINTS if e not in _stale_endpoints] or _OVERPASS_ENDPOINTS
    # ...and skip hosts we are resting (refused us, or asked us to wait) while
    # any other host is available. `used` stays in the list as a position.
    awake = [e for e in usable if e == used or not _host_blocked(_host(e))]
    if any(e != used for e in awake):
        usable = awake
    try:
        i = usable.index(used)
    except ValueError:
        # `used` was itself excluded (or unknown) — start at the top of the
        # usable list rather than rotating relative to a host we will not reuse.
        return usable[0]
    return usable[(i + 1) % len(usable)]
_failover_lock = threading.Lock()
_failed_over = False
# --- request budget ---------------------------------------------------------
# osmnx defaults to a 180 s socket timeout and osmnx>=2 retries, so ONE hung
# layer could hold a step for many minutes with nothing the user could do. Since
# 2026-07-27 the study area is the whole county (Prof. Shi's request), which made
# every Overpass query 10-60x larger than the old 5 km disc and turned that
# latent risk into his 2026-08-07 report: "after the data are fetched, the
# process cannot move on". Bound it here, and again per layer in hcas.py.
OSM_TIMEOUT_S = int(os.environ.get("OSM_TIMEOUT_S", "90"))

# overpass.kumi.systems answers a generic client with HTTP 429 and the body
# "Please include a meaningful User-Agent string with your requests to avoid
# rate-limiting" (verified 2026-08-07), so the failover mirror was rejecting us
# on arrival. Identify the app.
_USER_AGENT = "HCAS/1.0 (Healthy Community Assessment System; +https://github.com/luoojason)"

# Connect and read are bounded SEPARATELY. A single scalar made both 90 s, and
# the failure seen repeatedly on 2026-08-07 was a *connect* timeout: the host
# accepted a one-node probe seconds earlier, then refused to complete the TCP
# handshake for the real query. Waiting 90 s to learn a server will not talk to
# us is 80 s of nothing — and it is paid per layer, before the rotation can move
# on. A server that is up completes a handshake in well under 10 s.
#
# requests takes (connect, read) as a tuple and osmnx passes requests_timeout
# straight through; the tuple would otherwise land in the Overpass query header
# as "[timeout:(10, 90)]", so the settings string is pinned to a literal (the
# default template's {maxsize} is empty unless overpass_memory is set).
OSM_CONNECT_TIMEOUT_S = int(os.environ.get("OSM_CONNECT_TIMEOUT_S", "10"))

ox.settings.requests_timeout = (OSM_CONNECT_TIMEOUT_S, OSM_TIMEOUT_S)
ox.settings.overpass_settings = f"[out:json][timeout:{OSM_TIMEOUT_S}]"
ox.settings.http_user_agent = _USER_AGENT

# osmnx's own rate limiter is OFF, and this is the single most important line in
# the file. With it on (its default) osmnx calls /api/status before EVERY query
# and sleeps for the pause it computes — but it parses that status by hard-coded
# line index (`response_text.split("\\n")[4]`), and only overpass-api.de's format
# puts the slot count there. Measured 2026-08-08:
#
#   overpass-api.de     -> pause 0.0 s   (line 4 is "2 slots available now.")
#   overpass.kumi...    -> the pause probe itself BLOCKS (line 4 is the header of
#                          the running-queries table), falling into a 5 s
#                          recursion / 60 s default
#
# Per query. With two workers over ~36 tiles x 8 layers that is hours of the
# CLIENT sleeping, indistinguishable from a server timeout — and it is exactly
# what looked like a two-day Overpass outage. Raw requests to the same hosts in
# the same minute answered in 0.8-15 s, which is how the contradiction showed up.
# With this off, a transit fetch that had never completed returned in 6.1 s.
#
# Politeness is NOT given up with it: the polite client below replaces osmnx's
# request function, reads /status with a parser that searches lines instead of
# indexing them, waits when the server says so, caps our own in-flight requests,
# and backs off on refusals. See "polite Overpass client".
ox.settings.overpass_rate_limit = False


# --- polite Overpass client -------------------------------------------------
# Measured 2026-09-29 on Railway: one step-3 run (7 layers, two areas) made every
# overpass-api.de backend refuse TCP from the app's IP for 10+ minutes. The
# client had no way to hear "wait": osmnx's status check is off (above), every
# refusal was retried instantly, and nothing counted the requests in flight
# across layers, areas and abandoned threads. This section is the fix, and it is
# plain politeness, not evasion: the same identified client, sending less, later.
#
#  1. Slot check. Before a query to the overpass-api.de family we GET /status
#     (short timeout, cached a few seconds) and parse it by searching lines:
#     "N slots available now." -> go; "Slot available after: ..., in N seconds."
#     -> wait N s if that fits the layer's budget, else call the host busy and
#     rotate. A status call that fails changes nothing (never hangs a query).
#     kumi and private.coffee serve a different format and are not asked.
#  2. Refused hosts cool down. A connection refused in under a second, or a
#     TCP handshake that times out, is the "this IP is refused" signal; two in a
#     row on one host stop us sending to it for OSM_HOST_COOLDOWN_S instead of
#     hammering the refusal.
#  3. One in-flight cap per server family, counted at the HTTP request, so a
#     project area, a base area, a prefetch and an abandoned layer all share it.
#  4. 429/504 are raised to the tile loop (which backs off) instead of osmnx's
#     unbounded "sleep 55 s and recurse", which ignored the step budget.
_STATUS_HOSTS = {"overpass-api.de", "z.overpass-api.de", "lz4.overpass-api.de"}
_FAMILY_KEY = "overpass-api.de"
OSM_OVERPASS_MAX_INFLIGHT = int(os.environ.get("OSM_OVERPASS_MAX_INFLIGHT", "2"))
OSM_STATUS_TIMEOUT_S = float(os.environ.get("OSM_STATUS_TIMEOUT_S", "4"))
OSM_STATUS_CACHE_S = float(os.environ.get("OSM_STATUS_CACHE_S", "5"))
OSM_STATUS_MAX_WAIT_S = float(os.environ.get("OSM_STATUS_MAX_WAIT_S", "90"))
OSM_HOST_COOLDOWN_S = float(os.environ.get("OSM_HOST_COOLDOWN_S", "300"))
OSM_FAST_REFUSAL_S = float(os.environ.get("OSM_FAST_REFUSAL_S", "1.0"))
OSM_SLOT_WAIT_MAX_S = float(os.environ.get("OSM_SLOT_WAIT_MAX_S", "180"))


class OverpassBusy(Exception):
    """We chose not to send: the server asked us to wait longer than the layer
    can afford, or we are resting a host that refused us. Counts as a refusal
    (retried with backoff, never split) and carries a plain reason for the UI."""

    def __init__(self, plain_reason: str):
        super().__init__(plain_reason)
        self.plain_reason = plain_reason


_ASKED_TO_WAIT = (
    "the OpenStreetMap server asked us to wait before sending more requests (its "
    "fair-use limit), and the wait did not fit in this step's time. Try again in "
    "a few minutes."
)
_RESTING_HOSTS = (
    "the OpenStreetMap servers were refusing connections from this server, so the "
    "app paused its requests to let their limit reset. Try again in a few minutes."
)
_OWN_CAP_FULL = (
    "the app's fair-use share of the OpenStreetMap server stayed in use by its "
    "other downloads for this step's whole time. Try again in a few minutes."
)

_SLOTS_NOW_RE = re.compile(r"^\s*(\d+)\s+slots?\s+available\s+now", re.I | re.M)
_SLOT_AFTER_RE = re.compile(
    r"^\s*Slot available after:.*?\bin\s+(-?\d+)\s+seconds?", re.I | re.M)
_RATE_LIMIT_RE = re.compile(r"^\s*Rate limit:\s*(\d+)", re.I | re.M)


def parse_overpass_status(text) -> dict | None:
    """{'rate_limit', 'available', 'wait_s'} from an overpass-api.de /status body,
    or None when the body is not in that format (garbage, kumi's layout, HTML).

    Searched line by line, never by index — osmnx's `split("\\n")[4]` is what
    hung on another server's layout. A body can list free slots AND occupied
    ones; `wait_s` is the soonest occupied slot and only matters at 0 free.
    """
    if not isinstance(text, str):
        return None
    now = _SLOTS_NOW_RE.search(text)
    afters = [max(0, int(m)) for m in _SLOT_AFTER_RE.findall(text)]
    if now is None and not afters:
        return None
    rl = _RATE_LIMIT_RE.search(text)
    return {
        "rate_limit": int(rl.group(1)) if rl else None,
        "available": int(now.group(1)) if now else 0,
        "wait_s": float(min(afters)) if afters else None,
    }


_polite_lock = threading.Condition()
_status_cache: dict[str, dict] = {}        # endpoint -> parsed status + "until"
_inflight: dict[str, int] = {}              # gate key -> requests on the wire
_rate_limits: dict[str, int] = {}           # endpoint -> last "Rate limit: N"
_host_blocked_until: dict[str, float] = {}  # host -> resting until (time.time())
_host_block_reason: dict[str, str] = {}
_fast_refusals: dict[str, int] = {}         # host -> consecutive fast refusals
_polite_stats: dict[str, float] = {}


def _stat(key: str, n: float = 1) -> None:
    _polite_stats[key] = _polite_stats.get(key, 0) + n


def polite_stats() -> dict:
    """Counters for measurement: status calls, waits, cooldowns, refusals."""
    return dict(_polite_stats)


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _gate_key(host: str) -> str:
    return _FAMILY_KEY if host in _STATUS_HOSTS else host


def _gate_limit(key: str) -> int:
    limit = max(1, OSM_OVERPASS_MAX_INFLIGHT)
    if key == _FAMILY_KEY:
        reported = [n for n in _rate_limits.values() if n > 0]   # 0 = unlimited
        if reported:
            limit = min(limit, min(reported))
    return limit


def _host_blocked(host: str) -> bool:
    return _host_blocked_until.get(host, 0.0) > time.time()


def _block_host(host: str, seconds: float, reason: str) -> None:
    with _polite_lock:
        until = time.time() + seconds
        if until > _host_blocked_until.get(host, 0.0):
            _host_blocked_until[host] = until
            _host_block_reason[host] = reason


def _remaining_budget() -> float:
    deadline = current_deadline()
    return float("inf") if deadline is None else deadline - time.time()


def _acquire_gate(key: str) -> None:
    """Wait for one of our own in-flight places for this server family."""
    give_up = time.time() + max(0.0, min(_remaining_budget(), OSM_SLOT_WAIT_MAX_S))
    with _polite_lock:
        while _inflight.get(key, 0) >= _gate_limit(key):
            left = give_up - time.time()
            if left <= 0:
                raise OverpassBusy(_OWN_CAP_FULL)
            _polite_lock.wait(timeout=min(left, 1.0))
        _inflight[key] = _inflight.get(key, 0) + 1


def _release_gate(key: str) -> None:
    with _polite_lock:
        _inflight[key] = max(0, _inflight.get(key, 0) - 1)
        _polite_lock.notify_all()


def _fetch_status_text(url: str) -> str | None:
    """GET <endpoint>/status. Any failure is None: it must never block a query."""
    import requests

    _stat("status_calls")
    try:
        r = requests.get(f"{url.rstrip('/')}/status", timeout=OSM_STATUS_TIMEOUT_S,
                         headers={"User-Agent": _USER_AGENT})
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def _status_for(url: str) -> dict | None:
    """The endpoint's status, from a few-seconds cache so parallel workers share
    one call. A cached "N free" is consumed as workers use it, so two workers
    cannot both spend the same last slot."""
    with _polite_lock:
        entry = _status_cache.get(url)
        if entry and entry["until"] > time.time() and entry["available"] > 0:
            entry["available"] -= 1
            return dict(entry, available=entry["available"] + 1)
    status = parse_overpass_status(_fetch_status_text(url))
    if status is None:
        return None
    with _polite_lock:
        if status["rate_limit"] is not None:
            _rate_limits[url] = status["rate_limit"]
        entry = dict(status, until=time.time() + OSM_STATUS_CACHE_S)
        if entry["available"] > 0:
            entry["available"] -= 1        # this caller takes one
        _status_cache[url] = entry
    return status


def _wait_for_server_slot(url: str) -> None:
    """Honour the server's own "wait N seconds" before sending a query."""
    if _host(url) not in _STATUS_HOSTS:
        return
    for attempt in range(3):              # wait, recheck, at most twice
        status = _status_for(url)
        if status is None or status["available"] > 0 or status["wait_s"] is None:
            return
        pause = status["wait_s"] + 1.0
        if attempt == 2 or pause > min(OSM_STATUS_MAX_WAIT_S, _remaining_budget() - 5.0):
            _block_host(_host(url), status["wait_s"], _ASKED_TO_WAIT)
            _stat("asked_to_wait_rotations")
            raise OverpassBusy(_ASKED_TO_WAIT)
        logger.info(f"Overpass {_host(url)} asked us to wait {pause:.0f} s — waiting")
        _stat("waits")
        _stat("wait_s", pause)
        time.sleep(pause)
        with _polite_lock:
            _status_cache.pop(url, None)


def _note_fast_refusal(host: str) -> None:
    with _polite_lock:
        _fast_refusals[host] = _fast_refusals.get(host, 0) + 1
        strikes = _fast_refusals[host]
    _stat("fast_refusals")
    if strikes >= 2:
        _block_host(host, OSM_HOST_COOLDOWN_S, _RESTING_HOSTS)
        with _polite_lock:
            _fast_refusals[host] = 0
        _stat("cooldowns")
        logger.warning(
            f"Overpass {host} refused two connections in a row — resting it "
            f"for {OSM_HOST_COOLDOWN_S:.0f} s"
        )


def _polite_overpass_request(data):
    """Drop-in for osmnx._overpass._overpass_request (osmnx 2.x).

    Same cache and parsing as osmnx; different manners. It deliberately does
    not call osmnx's _config_dns, which pins the host to one gethostbyname()
    answer and rebinds socket.getaddrinfo process-wide — bypassing the
    dead-address filter above every time it ran.
    """
    import requests
    from osmnx import _http

    url = ox.settings.overpass_url.rstrip("/")
    host = _host(url)
    full = url + "/interpreter"
    prepared = str(requests.Request("GET", full, params=data).prepare().url)
    cached = _http._retrieve_from_cache(prepared)
    if isinstance(cached, dict):
        return cached
    if _host_blocked(host):
        raise OverpassBusy(_host_block_reason.get(host, _RESTING_HOSTS))
    key = _gate_key(host)
    _acquire_gate(key)
    try:
        _wait_for_server_slot(url)
        _stop_if_expired()
        _stat("requests")
        t0 = time.monotonic()
        try:
            response = requests.post(full, data=data, timeout=ox.settings.requests_timeout,
                                     headers=_http._get_http_headers(),
                                     **ox.settings.requests_kwargs)
        except requests.exceptions.ConnectionError as exc:
            # Two shapes of "this IP is refused": a reset in under a second, or
            # a handshake that never completes. The second is what the live run
            # on 2026-09-29 actually saw (every family host: connect timeout at
            # 10 s, never a reset), so counting only fast resets rested nothing.
            # A ReadTimeout is NOT counted — the server accepted us.
            if isinstance(exc, requests.exceptions.ConnectTimeout) or (
                    not isinstance(exc, requests.exceptions.Timeout)
                    and time.monotonic() - t0 < OSM_FAST_REFUSAL_S):
                _note_fast_refusal(host)
            raise
    finally:
        _release_gate(key)
    with _polite_lock:
        _fast_refusals[host] = 0          # it answered: the IP is not refused
    if response.status_code in (429, 504):
        _stat("http_refusals")
        with _polite_lock:
            _status_cache.pop(url, None)  # the next attempt reads a fresh status
        raise ResponseStatusCodeError(
            f"{host!r} responded: {response.status_code} {response.reason}")
    response_json = _http._parse_response(response)
    if not isinstance(response_json, dict):
        raise InsufficientResponseError("Overpass API did not return a dict of results.")
    _http._save_to_cache(prepared, response_json, response.ok)
    return response_json


from osmnx import _overpass as _ox_overpass  # noqa: E402

_ox_overpass._overpass_request = _polite_overpass_request


# 12 s, not 6: measured on a flaky link 2026-08-07, overpass-api.de answered a
# one-node probe in 8.0 s while the mirror timed out at 10.4 s — and ten minutes
# earlier the two had swapped places. A probe tight enough to misjudge a working
# endpoint is worse than no probe, since it would send every query to a mirror
# that is not necessarily better.
_PROBE_TIMEOUT_S = float(os.environ.get("OSM_PROBE_TIMEOUT_S", "12"))
_probed = False
# When the probe's verdict goes stale. It used to be decided ONCE per process
# and never again — `_probed` was never reset outside the tests — so a single
# bad minute became a permanent state. Found 2026-09-16 in Prof. Shi's own run:
# the first step 3 of the day probed during an outage, set the outage flag, and
# from then on every layer of every later run got one attempt, no rotation, and
# failed, while the servers had long since recovered. A verdict of "all down" is
# rechecked after three minutes; a working endpoint is trusted for ten. Not one
# minute: a failing probe is five requests and ~60 s of timeouts, and measured
# the same afternoon a one-minute recheck kept a rate-limited address probing
# almost continuously, which is how an allowance stays spent.
_PROBE_TTL_OK_S = float(os.environ.get("OSM_PROBE_TTL_S", "600"))
_PROBE_TTL_OUTAGE_S = float(os.environ.get("OSM_PROBE_TTL_OUTAGE_S", "180"))
_probe_expires_at = 0.0

# --- data freshness ---------------------------------------------------------
# An Overpass mirror can be fast and WRONG. Measured 2026-08-11 against
# overpass.private.coffee, one of our own rotation entries:
#
#   attempt 1  504 after 37 s
#   attempt 2  read timeout after 45 s
#   attempt 3  200 in 1.6 s   -> timestamp_osm_base 2026-05-06  (three months old)
#   attempt 4  504 after 36 s
#
# and an earlier call the same hour returned 2026-07-07, so it is a cluster
# whose backends sit at different vintages. The probe below picks whichever
# endpoint answers FASTEST, and a stale backend answers a one-node probe in
# 1.6 s — so it wins the probe it should lose, and a run can take May features
# for one tile and August features for the next inside a single layer, with
# nothing on screen saying so.
#
# That is the same failure the osm.ch note above describes ("wrong server"
# reading as "this county has no buildings"), on a host we had already adopted.
# Every Overpass response carries osm3s.timestamp_osm_base, so the age is
# knowable before we commit a run to a host — check it and refuse.
#
# Unknown age is ALLOWED: a response we cannot parse for a timestamp is not
# evidence of staleness, and refusing on it would reject working endpoints on a
# format change. Only a KNOWN-too-old endpoint is rejected.
OSM_MAX_DATA_AGE_DAYS = float(os.environ.get("OSM_MAX_DATA_AGE_DAYS", "7"))

# Endpoints the probe found reachable but out of date, and the age it saw.
# Rotation skips these; the step surfaces the warning if we are forced onto one.
_stale_endpoints: dict[str, float] = {}
_stale_warning: str | None = None


def _data_age_days(resp) -> float | None:
    """Age of the endpoint's OSM data in days, or None if it cannot be read."""
    from datetime import datetime, timezone

    try:
        ts = resp.json()["osm3s"]["timestamp_osm_base"]
        base = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - base).total_seconds() / 86400.0
    except Exception:
        return None


def stale_data_warning() -> str | None:
    """Plain-language warning when the run is using out-of-date OSM data, else
    None. The step runners put this on the artifact so a number computed from
    three-month-old features can never look like a current measurement."""
    return _stale_warning


def _probe_overpass_once() -> None:
    """Settle on a working Overpass endpoint before the first real query.

    Discovering a dead endpoint through the real query costs a full
    `requests_timeout` first — 90 s of connect timeout on a link that cannot
    reach the host at all (measured 2026-08-07). Probing each endpoint in turn
    costs at most _PROBE_TIMEOUT_S each and lands the whole run on a host that is
    answering *today*: on 2026-08-07 the primary took 21.7 s and the mirror
    returned 504, while the third endpoint answered the same query in 4.2 s.

    The probe is a tiny REAL query against /interpreter, not /status: /status
    answered inside the probe window on a link where /interpreter still timed
    out, so the cheap endpoint proves nothing about the one that matters.
    """
    global _probed, _failed_over, _stale_warning, _outage_suspected_flag, _probe_expires_at
    with _failover_lock:
        if _probed and time.time() < _probe_expires_at:
            return
        _probed = True
        # Claimed up front so concurrent layers do not all probe at once; the
        # real expiry is set when the verdict is in.
        _probe_expires_at = time.time() + _PROBE_TIMEOUT_S * (len(_OVERPASS_ENDPOINTS) + 1)
    _outage_suspected_flag = False    # this probe decides it afresh
    import requests

    start = ox.settings.overpass_url
    order = [start] + [e for e in _OVERPASS_ENDPOINTS if e != start]
    reasons = []
    stale: list[tuple[str, float]] = []   # reachable, but serving old data

    def _select(url: str) -> None:
        global _failed_over
        with _failover_lock:
            ox.settings.overpass_url = url
            _failed_over = url != _OVERPASS_PRIMARY

    for url in order:
        try:
            # Before spending a probe on this host, drop any address of its that
            # cannot complete a handshake. Otherwise the probe may land on the
            # good address, adopt the host, and leave every real query playing
            # coin-flips with a black hole (measured 2026-08-11 — see the dead
            # A-record note above).
            from urllib.parse import urlsplit

            _learn_dead_addresses(urlsplit(url).hostname)
            r = requests.post(
                f"{url}/interpreter",
                data={"data": "[out:json][timeout:5];node(0,0,0.001,0.001);out 1;"},
                headers={"User-Agent": _USER_AGENT},
                timeout=_PROBE_TIMEOUT_S,
            )
            if r.status_code >= 500:
                reasons.append(f"{url} HTTP {r.status_code}")
                continue
            age = _data_age_days(r)
            if age is not None and age > OSM_MAX_DATA_AGE_DAYS:
                # Fast is not the same as correct. Skip it and keep looking.
                _stale_endpoints[url] = age
                stale.append((url, age))
                reasons.append(f"{url} data {age:.0f} days old")
                logger.warning(
                    f"Overpass: skipping {url} — its data is {age:.0f} days old "
                    f"(limit {OSM_MAX_DATA_AGE_DAYS:.0f}); a fast mirror serving stale "
                    "features would look exactly like a current measurement"
                )
                continue
            if url != start:
                logger.warning(
                    f"Overpass: starting on {url} — earlier endpoints unavailable "
                    f"({'; '.join(reasons)})"
                )
            _select(url)
            # A current endpoint answered, so any earlier "forced onto old data"
            # verdict no longer describes this run.
            _stale_endpoints.pop(url, None)
            _stale_warning = None
            _probe_expires_at = time.time() + _PROBE_TTL_OK_S
            return
        except Exception as exc:
            reasons.append(f"{url} {type(exc).__name__}")

    if stale:
        # Nothing current answered, but something answered. Use the freshest of
        # the stale hosts rather than returning nothing — and SAY SO, loudly
        # enough that the number reaches the report wearing its age.
        url, age = min(stale, key=lambda p: p[1])
        _select(url)
        _stale_warning = (
            f"The only OpenStreetMap server reachable on this run ({url}) is serving "
            f"data about {age:.0f} days old, so the map features below are that old. "
            "Census boundaries and health outcomes are unaffected. Re-run later for "
            "current data."
        )
        logger.error(f"Overpass: all current endpoints unreachable; {_stale_warning}")
        _probe_expires_at = time.time() + _PROBE_TTL_OUTAGE_S
        return
    # Every endpoint failed its probe. Leave the URL where it started and let the
    # real call report the failure — an outage is the fetchers' story to tell,
    # with a plain-language reason, not something to swallow here.
    #
    # But do not let each layer re-discover the outage the expensive way. Each
    # one would otherwise pay failover across every endpoint, then its retries,
    # then quarter itself and repeat the lot — measured 2026-08-11 during a real
    # outage, that spent the ENTIRE 900 s budget to return nothing, when the
    # probe had already said so at 40 s. One attempt per layer is enough to
    # report the failure honestly, and still enough to recover if the probe was
    # wrong, since the attempt is a real query against a real endpoint.
    _outage_suspected_flag = True
    _probe_expires_at = time.time() + _PROBE_TTL_OUTAGE_S
    logger.warning(
        f"Overpass: no endpoint answered a probe ({'; '.join(reasons)}) — "
        "treating this as an outage and giving each layer a single attempt "
        "rather than a full retry cascade; checking again in "
        f"{_PROBE_TTL_OUTAGE_S:.0f} s"
    )


def _with_overpass_failover(call):
    """Run an osmnx call; on a network failure, fail over to the Overpass
    mirror once and retry. InsufficientResponseError (a genuinely-empty query
    result) passes straight through — that is not a network problem.

    Retries are decided by which endpoint THIS call used, not by whether some
    other thread has already switched. Step 3 fetches layers concurrently, so
    the old check ("has anyone failed over yet?") let the first failure switch
    the URL and every sibling already in flight against the primary then
    re-raised without ever trying the mirror — three layers died on the primary
    in the same millisecond in the 2026-08-07 reproduction.
    """
    global _failed_over
    used = ox.settings.overpass_url
    try:
        return call()
    except InsufficientResponseError:
        raise
    except BudgetExpired:
        raise                     # not a network problem; nothing to fail over to
    except Exception as exc:
        # The retry below costs another full connect+read timeout. If the step
        # has already given up on this layer there is nobody to hand it to, and
        # spending it holds an Overpass slot the NEXT attempt needs.
        if _budget_expired():
            raise
        # Same reasoning when the probe already found every endpoint refusing:
        # rotating to the next one is rotating to another host we just watched
        # fail, at the cost of a second full timeout.
        if _outage_suspected():
            raise
        # Rotate to the next endpoint and give this call ONE more attempt there.
        # Rotating (rather than switching one way to a designated mirror) is what
        # keeps a run off whichever host is sick today: measured 2026-08-07 with
        # the same query, primary 21.7 s / kumi 504 / private.coffee 4.2 s — and
        # ten minutes earlier kumi had been the fast one. No host is reliably best.
        nxt = _next_endpoint(used)
        with _failover_lock:
            ox.settings.overpass_url = nxt
            _failed_over = nxt != _OVERPASS_PRIMARY
        # Endpoint SELECTION drops a host's black-holed addresses, but selection
        # runs once and these retries run for the rest of the step's budget — so
        # without this, every rotation re-played the coin flip against a dead A
        # record and never learned. Measured 2026-09-01: a 900 s step spent
        # entirely here, the verdict never refreshed, and the connect timeouts
        # surfaced to the user as "DNS lookup failed - check the system DNS
        # settings". The verdict is cached for _ADDR_RECHECK_S, so this is a
        # dict lookup on all but the first rotation per host per 5 minutes.
        #
        # `used` FIRST, and it is the one that matters. The host we are leaving
        # is the one that just produced evidence, and the rotation will come
        # back round to it — learning only about `nxt` fixed nothing, because
        # the dead address belonged to the host that failed (measured
        # 2026-09-02: twenty minutes of rotating off overpass-api.de on
        # ConnectTimeout without ever re-probing it).
        # Not after OverpassBusy: nothing went on the wire, so there is no
        # evidence about addresses, and probing a host we are resting would
        # send it the very connections we chose to withhold.
        if not isinstance(exc, OverpassBusy):
            _learn_dead_addresses(urlsplit(used).hostname)
            _learn_dead_addresses(urlsplit(nxt).hostname)
        logger.warning(f"Overpass {used} failed ({exc!r}) — retrying on {nxt}")
        try:
            return call()
        except InsufficientResponseError:
            raise
        except Exception as exc2:
            # Two timeouts is all one call pays; a third would just repeat a wait
            # already spent. Move the endpoint on so the NEXT call starts fresh.
            after = _next_endpoint(nxt)
            with _failover_lock:
                ox.settings.overpass_url = after
                _failed_over = after != _OVERPASS_PRIMARY
            # Same reasoning as the rotation above: the next call inherits this
            # endpoint, so it inherits its dead addresses unless we look now.
            if not isinstance(exc2, OverpassBusy):
                _learn_dead_addresses(urlsplit(after).hostname)
            logger.warning(
                f"Overpass {nxt} failed too ({exc2!r}) — next request tries {after}"
            )
            raise


# --- adaptive tiling -------------------------------------------------------
# A whole-county Overpass query is often too big to answer at all. Measured
# 2026-08-07 against overpass.kumi.systems for Grand Isle County VT (one of the
# smallest counties in the country, 212 km2 of land):
#
#   whole county bbox, buildings : FAILED after 103 s (truncated response)
#   same area as 4 quadrants     : 2.0 s + 1.8 s + 5.6 s + 6.9 s -> 6,086 buildings
#
# So the query is not slow, it is too large; splitting it makes the same data
# arrive in a sixth of the time we used to spend failing. Prof. Shi's county-wide
# study area (his 2026-07-27 request) is what pushed every layer over that edge,
# and no timeout tuning can fix it — a bounded wait just fails faster.
#
# Split adaptively rather than on a fixed grid: a rural county answers in one
# request, and only the tiles that actually fail are subdivided, so a dense urban
# county pays for its density and nothing else does.
OSM_TILE_MAX_DEPTH = int(os.environ.get("OSM_TILE_MAX_DEPTH", "3"))  # <= 64 tiles per start tile

# ...except a county is NOT reliably too big, and pre-splitting one was what
# stopped step 3 finishing at all. Corrected 2026-08-11 against Adams County,
# Ohio (39001), same layer, same hour, osmnx cache bypassed:
#
#   whole county, 1 request   :  19.3 s  ->  43,783 elements,  0/1 failed
#   0.25 deg,     4 requests  :  88.0 s  ->  29,517 elements,  3/4 failed
#   0.20 deg,     9 requests  : 168.5 s  ->  41,372 elements,  5/9 failed
#
# More requests was slower, LESS complete, and the failures included HTTP 429:
# issuing them back to back made the app rate-limit itself. The reason is that
# per-request cost barely depends on area — a query returning ZERO elements
# still cost 12-19 s, and shrinking a tile to 1/36 of its area still cost
# 19-57 s. That fixed floor means splitting can only ever add to the total.
#
# The 103 s whole-county failure recorded above was real, but it was measured
# against overpass.kumi.systems — the host that 504'd on everything that day —
# and generalised into policy for every layer on every endpoint. On a healthy
# connection the whole-county query for the two heaviest layers answered in
# 1.8 s (buildings, 51,461 elements) and 2.3 s (landuse, 69,863).
#
# So the cap is now a safety valve for areas far larger than a county, not the
# normal path: 1.0° is ~85 x 111 km at 40°N, which puts essentially every US
# county in a single request and still pre-splits something Alaska-sized. Areas
# that genuinely are too big are found the way this function was always meant to
# find them — adaptively, by failing once and quartering.
OSM_TILE_MAX_DEG = float(os.environ.get("OSM_TILE_MAX_DEG", "1.0"))

# Quartering is the answer to "too big for this server". It is the WRONG answer
# to a refused connection, a 429 or a gateway 504 — there it turns one failed
# request into four and makes the rate limiting worse. Every failure actually
# measured on 2026-08-11 was of that second kind (whole-county queries answered
# in 1.8-19.3 s, so almost nothing at county scale is genuinely oversized), so
# retry the same area first and only split when retrying has not helped.
OSM_TILE_RETRIES = int(os.environ.get("OSM_TILE_RETRIES", "1"))

# ...and one retry was not enough, because the retry ran out and the split
# happened anyway. Measured 2026-08-12 (Delaware County OH), the identical
# whole-county buildings query against overpass-api.de inside one minute:
#
#   [timeout:90]  -> 504 after 20.2 s          [timeout:180] -> 429 after 19.9 s
#   [timeout:90]  -> 200 in   23.9 s (23.3 MB) [timeout:300] -> 200 in  12.9 s
#
# The area is answerable — 26,532 building elements, and roads 47,869 in 14.1 s
# — and roughly a third of attempts are turned away regardless, at about 20 s
# each. So a refusal is a coin flip, and the right response to one is to flip
# again, not to send four requests where one was just declined.
OSM_REFUSAL_RETRIES = int(os.environ.get("OSM_REFUSAL_RETRIES", "3"))

# Gateway and rate-limit codes. 504 is included deliberately: it reads like
# "your query was too slow", but the measurement above shows the SAME query
# returning 504 and then 200 at the same declared timeout, so here it is load.
_REFUSAL_STATUS = {429, 502, 503, 504}


def _is_refusal(exc: BaseException) -> bool:
    """Did the server decline to do the work, rather than fail at doing it?

    Splitting an area answers the question "is this too big for this server?".
    It is the wrong answer to a refusal, where it turns one turned-away request
    into four and makes the rate limiting worse — the note above OSM_TILE_RETRIES
    has said so since 2026-08-11, but the code still fell through to the split
    once the retry was spent. The 2026-08-12 reproduction logged the result:

        Overpass query still failing ... after 2 attempts (ConnectTimeout(...))
        — splitting into 4 (depth 1)

    A connection that never completed cannot have measured anything about the
    area behind it. These are the failures that carry no size information:

      * the connection never completed  (ConnectTimeout, ConnectionError)
      * the server dropped a query it had accepted  (ReadTimeout)
      * it answered with a rate limit or a gateway error  (429/502/503/504)

    A truncated or unparseable body is deliberately NOT here. That is what a
    genuinely oversize query looked like when one was actually measured (Grand
    Isle VT, 2026-08-07: whole county failed after 103 s with a truncated
    response, the same area as four quadrants returned 6,086 buildings in 16 s),
    and that case must still be allowed to split.
    """
    import requests

    if isinstance(exc, OverpassBusy):
        return True          # we held back on the server's word: same as a refusal
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout)):
        return True          # covers ConnectTimeout and ReadTimeout, both subclasses
    if isinstance(exc, ResponseStatusCodeError):
        # osmnx puts the status line in the message (_http._parse_response).
        found = re.search(r"responded:\s*(\d{3})", str(exc))
        return bool(found) and int(found.group(1)) in _REFUSAL_STATUS
    return False


# Backoff between attempts at the same area: roughly 5 s, 15 s, 40 s, then
# capped, each with +/-25% jitter so parallel layers do not retry in lockstep.
# It was 2/4/8 s — measured 2026-09-29 on Railway, retrying that fast after
# refusals is how every overpass-api.de backend came to refuse the app's IP for
# 10+ minutes. _pause_for_retry still clamps every pause to the step budget.
_BACKOFF_STEPS_S = (5.0, 15.0, 40.0)
OSM_BACKOFF_MAX_S = float(os.environ.get("OSM_BACKOFF_MAX_S", "60"))


def _retry_pause(attempt: int) -> float:
    """Backoff before retry number `attempt` (1-based) of the same area."""
    import random

    i = max(1, attempt) - 1
    base = _BACKOFF_STEPS_S[i] if i < len(_BACKOFF_STEPS_S) else OSM_BACKOFF_MAX_S
    return min(base, OSM_BACKOFF_MAX_S) * random.uniform(0.75, 1.25)


def _pause_for_retry(attempt: int) -> None:
    """Sleep the backoff, but never past the step's deadline."""
    pause = _retry_pause(attempt)
    deadline = current_deadline()
    if deadline is not None:
        pause = min(pause, max(deadline - time.time(), 0.0))
    if pause > 0:
        time.sleep(pause)


def _start_tiles(bbox):
    """Split a bbox into pieces no larger than OSM_TILE_MAX_DEG on either axis."""
    west, south, east, north = bbox
    nx = max(1, math.ceil((east - west) / OSM_TILE_MAX_DEG))
    ny = max(1, math.ceil((north - south) / OSM_TILE_MAX_DEG))
    if nx == 1 and ny == 1:
        return [tuple(bbox)]
    dx, dy = (east - west) / nx, (north - south) / ny
    return [
        (west + i * dx, south + j * dy, west + (i + 1) * dx, south + (j + 1) * dy)
        for i in range(nx)
        for j in range(ny)
    ]


def _tiled(bbox, call_one, depth: int = 0):
    """Run `call_one(bbox)`; if it keeps failing, quarter the bbox and try each
    part. Returns a list of results (never raises unless depth is exhausted).

    The area is RETRIED before it is split. Splitting answers "too big for this
    server"; it is the wrong answer to a refused connection or a 429, where it
    turns one failed request into four — and those were the failures actually
    measured (2026-08-11), not oversized queries.

    InsufficientResponseError means the tile is genuinely empty — that is an
    answer, not a failure, so it is neither retried nor split.

    Every loop is deadline-aware: once the step's budget is gone this returns
    what already arrived instead of continuing to fetch for nobody.
    """
    if depth == 0:
        _probe_overpass_once()
        tiles = _start_tiles(bbox)
        if len(tiles) > 1:
            logger.info(f"Overpass: {bbox} is large — starting at {len(tiles)} tiles")
            out, failures = [], []
            for i, t in enumerate(tiles):
                if _budget_expired():
                    # Stop walking tiles nobody is waiting for, and keep what
                    # already arrived — a partial layer beats a zombie thread.
                    logger.warning(
                        f"Overpass: budget expired after {i} of {len(tiles)} start "
                        "tiles; abandoning the rest"
                    )
                    break
                try:
                    out.extend(_tiled(t, call_one, depth))
                except Exception as exc:
                    failures.append(exc)
            if failures and not out:
                raise failures[0]
            if failures:
                logger.warning(
                    f"{len(failures)} of {len(tiles)} start tiles failed; keeping the rest"
                )
            return out
    west, south, east, north = bbox
    _stop_if_expired()
    # Try this area a few times before deciding it is too big. A connection
    # refused, a 429 or a gateway 504 says nothing about the size of the query,
    # and splitting on one multiplies the requests that caused it.
    last_failure = None
    attempt = 0
    attempts = 1 if _outage_suspected() else 1 + OSM_TILE_RETRIES
    while attempt < attempts:
        if attempt:
            _pause_for_retry(attempt)
            _stop_if_expired()
        attempt += 1
        try:
            return [call_one(bbox)]
        except InsufficientResponseError:
            return []
        except BudgetExpired:
            raise
        except Exception as exc:
            last_failure = exc
            _stop_if_expired()
            # A refusal buys more attempts at the SAME area. Refusals are
            # intermittent (see OSM_REFUSAL_RETRIES) so another flip of the coin
            # is both cheaper and likelier to work than four requests to a
            # server that just turned this one away.
            if _is_refusal(exc) and not _outage_suspected():
                attempts = max(attempts,
                               1 + OSM_TILE_RETRIES + OSM_REFUSAL_RETRIES)
    if depth >= OSM_TILE_MAX_DEPTH:
        raise last_failure
    if _is_refusal(last_failure):
        # Every attempt was declined, so nothing here measured the area. Four
        # sub-requests would be four more of exactly what is being refused —
        # and quartering to depth 3 turns one refusal into as many as 85.
        logger.warning(
            f"Overpass refused {bbox} on all {attempt} attempts "
            f"({last_failure!r}) — not splitting: a refusal says nothing about "
            "the size of the area, and splitting multiplies the refused requests"
        )
        raise last_failure
    if _outage_suspected():
        # Splitting asks "is this area too big?" of servers that are not
        # answering anything. Report the outage instead of quadrupling it.
        raise last_failure
    # One failure fans out to as many as 85 sub-requests (1 + 4 + 16 + 64).
    # If the budget died during the attempts, none of that may happen.
    _stop_if_expired()
    mid_x, mid_y = (west + east) / 2, (south + north) / 2
    quads = [
        (west, south, mid_x, mid_y), (mid_x, south, east, mid_y),
        (west, mid_y, mid_x, north), (mid_x, mid_y, east, north),
    ]
    logger.info(
        f"Overpass query still failing for {bbox} after {1 + OSM_TILE_RETRIES} "
        f"attempts ({last_failure!r}) — splitting into 4 (depth {depth + 1})"
    )
    out = []
    failures = []
    for q in quads:
        if _budget_expired():
            break                 # keep what arrived; stop spending on this layer
        try:
            out.extend(_tiled(q, call_one, depth + 1))
        except Exception as exc:  # keep the tiles that did answer
            failures.append(exc)
    if failures and not out:
        raise failures[0]
    if failures:
        logger.warning(
            f"{len(failures)} of 4 sub-tiles failed under {bbox}; keeping the rest"
        )
    return out


def _combine(parts, crs_hint=None):
    """Concatenate tile results and drop features seen in more than one tile
    (anything crossing a tile edge is returned by both)."""
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    import pandas as pd

    combined = pd.concat(parts)
    combined = combined[~combined.index.duplicated(keep="first")]
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=parts[0].crs or crs_hint)


def _features(bbox, tags):
    parts = _tiled(
        bbox,
        lambda b: _with_overpass_failover(lambda: ox.features_from_bbox(bbox=b, tags=tags)),
    )
    out = _combine(parts)
    return out if out is not None else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def _graph(bbox, network_type):
    """Edges only — the callers all want `graph_to_gdfs(...)[1]`, and edges are
    what can be merged across tiles (a graph cannot be stitched meaningfully at a
    tile boundary, but its edge geometries can)."""
    def one(b):
        graph = _with_overpass_failover(
            lambda: ox.graph_from_bbox(bbox=b, network_type=network_type)
        )
        return ox.graph_to_gdfs(graph)[1]

    out = _combine(_tiled(bbox, one))
    return out if out is not None else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


_BUILDING_TAGS = {"building": True}

_GREENSPACE_TAGS = {
    "leisure": ["park", "garden", "nature_reserve", "recreation_ground"],
    "landuse": ["grass", "park", "recreation_ground", "forest", "meadow", "village_green"],
}

_POLYGON_GEOM_TYPES = {"Polygon", "MultiPolygon"}


def fetch_buildings(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch building footprints within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    """
    try:
        gdf = _features(bbox, _BUILDING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_buildings", exc)
        logger.warning(f"fetch_buildings failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_greenspace(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch parks and green areas within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained (excludes point amenity tags).
    """
    try:
        gdf = _features(bbox, _GREENSPACE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_greenspace", exc)
        logger.warning(f"fetch_greenspace failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_road_network(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
    network_type: str = "drive",
) -> gpd.GeoDataFrame:
    """Fetch road network edges within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Returns the edges GeoDataFrame from the osmnx graph.
    """
    try:
        edges = _graph(bbox, network_type)  # already the edges GeoDataFrame
        if edges.empty:
            return _empty_gdf(crs)
        return edges.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_road_network", exc)
        logger.warning(f"fetch_road_network failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_PARKING_TAGS = {"amenity": "parking", "parking": True}

_SIDEWALK_TAGS = {
    "highway": ["footway", "path", "pedestrian", "sidewalk"],
}

_TRANSIT_TAGS = {
    "public_transport": ["stop_position", "platform"],
    "highway": "bus_stop",
    "railway": ["station", "halt", "tram_stop"],
}

_LINE_GEOM_TYPES = {"LineString", "MultiLineString"}
_POINT_GEOM_TYPES = {"Point", "MultiPoint"}


def fetch_parking(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch parking areas within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained.
    """
    try:
        gdf = _features(bbox, _PARKING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_parking", exc)
        logger.warning(f"fetch_parking failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_sidewalks(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch sidewalk and pedestrian path edges within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only LineString geometries are retained.
    """
    try:
        gdf = _features(bbox, _SIDEWALK_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_LINE_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_sidewalks", exc)
        logger.warning(f"fetch_sidewalks failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_transit_stops(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch transit stops within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only Point geometries are retained.
    """
    try:
        gdf = _features(bbox, _TRANSIT_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_transit_stops", exc)
        logger.warning(f"fetch_transit_stops failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_LANDUSE_TAGS = {"landuse": True}


def fetch_landuse(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch all landuse polygons within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained; the 'landuse' tag column is kept.
    """
    try:
        gdf = _features(bbox, _LANDUSE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_landuse", exc)
        logger.warning(f"fetch_landuse failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_FACILITY_TAGS = {
    "amenity": [
        "hospital", "clinic", "doctors", "pharmacy",
        "school", "kindergarten", "library", "community_centre",
    ],
    "shop": ["supermarket", "greengrocer", "convenience"],
}


def fetch_facilities(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch community facilities (health, education, food, civic) within bbox.

    bbox: (west, south, east, north) in EPSG:4326.
    Polygon footprints are reduced to centroids so the layer is uniformly
    point-typed. (ponytail: centroid ~ entrance at neighborhood scale)
    """
    try:
        gdf = _features(bbox, _FACILITY_TAGS)
        if gdf.empty:
            return _empty_gdf(crs)
        gdf = gdf.to_crs(crs)
        poly = gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)
        gdf.loc[poly, "geometry"] = gdf.loc[poly].geometry.centroid
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_facilities", exc)
        logger.warning(f"fetch_facilities failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def _empty_gdf(crs: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=crs)
