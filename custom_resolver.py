# -*- coding: utf-8 -*-
"""
custom_resolver.py — generic JSON/text webhook resolver client (curl_cffi + FlareSolverr edition).

All outgoing HTTP goes through `curl_cffi.requests.Session(impersonate=...)`, so the TLS/HTTP2
handshake matches a real browser instead of the python-requests / OpenSSL fingerprint.

Optionally, an automated Cloudflare challenge solver (FlareSolverr, local or remote) supplies a
clearance session (cf_clearance / __cf_bm / ... cookies + the exact User-Agent of the solver's browser).
It is cached process-wide and injected into every thread's curl_cffi session. Without FLARESOLVERR_URL
the module behaves exactly like the plain direct curl_cffi client.

Flow:
    text -> extract_target_url() -> call_resolver() -> stream_file()
                                    |                  (download into BytesIO)
                                    +- Step 0a POST {FLARESOLVERR_URL}  {"cmd": "request.get", "url": ENDPOINT/}
                                    |          (only if FLARESOLVERR_URL is set; the result -- cookies + userAgent --
                                    |           is cached for FLARESOLVERR_SESSION_TTL and shared by all threads;
                                    |           cookies go into the thread's cookie jar, the UA overrides the session's)
                                    +- Step 0  GET  {ENDPOINT}/   (silent browser-style pre-warm: edge cookies,
                                    |          TLS session, cache state; failures are ignored)
                                    +- Step 1  GET  {ENDPOINT}{HANDSHAKE_PATH}?q=<urlencoded target>
                                    |          -> metadata (JSON object/array, or plain text)
                                    |          (HTTP 403 while a clearance is active: the clearance is invalidated,
                                    |           FlareSolverr is asked for a fresh one ONCE and the handshake is retried;
                                    |           HTTP 403 after that: retried with the next profile in
                                    |           RESOLVER_RETRY_PROFILES, on a fresh pre-warmed session)
                                    +- Step 2  POST {ENDPOINT}{EXCHANGE_PATH}  json=<step-1 payload>
                                               -> response body is the final direct media URL

The per-thread session (and therefore its cookies, clearance and impersonation profile) is shared by
pre-warm, Step 1, Step 2 and stream_file().

Requires:  pip install -U "curl_cffi>=0.7"

Environment variables (all optional):
    RESOLVER_ENDPOINT              Base URL of the resolver.                 default: https://example.internal
    RESOLVER_HANDSHAKE_PATH        Step-1 path.                              default: /api/9d1dd0c0
    RESOLVER_EXCHANGE_PATH         Step-2 path.                              default: /api/79bf4d06
    RESOLVER_IMPERSONATE           curl_cffi browser profile.                default: safari17_2_ios
                                   (falls back to "chrome" if the installed curl_cffi doesn't know it)
    RESOLVER_RETRY_PROFILES        Comma-separated profiles tried, in order, when the Step-1 handshake
                                   returns HTTP 403 with the current profile.   default: chrome124,chrome120
                                   A profile that works becomes the process-wide default.
    RESOLVER_PREWARM_TTL           Seconds a thread's pre-warmed session is considered fresh. 0 = pre-warm
                                   before every call_resolver().             default: 300
    RESOLVER_USER_AGENT            Override the User-Agent. Leave unset when impersonating: curl_cffi then
                                   sends the UA that matches the TLS fingerprint (a mismatch is itself a signal).
                                   Ignored while a FlareSolverr clearance is active (cf_clearance is bound
                                   to the solver's User-Agent).
    RESOLVER_CONNECT_TIMEOUT       Connect timeout, seconds.                 default: 10
    RESOLVER_READ_TIMEOUT          Read timeout of each resolver call.       default: 30
    RESOLVER_DOWNLOAD_TIMEOUT      Stall timeout while downloading.          default: 30
    RESOLVER_DOWNLOAD_DEADLINE     Hard wall-clock cap for a whole download. default: 120
    RESOLVER_MAX_BYTES             Max download size in bytes.               default: 45 MiB
    RESOLVER_ALLOW_PRIVATE_DOWNLOADS=1   Allow media URLs that resolve to private/loopback addresses.

    FLARESOLVERR_URL               FlareSolverr API URL, e.g. http://127.0.0.1:8191/v1 (a bare host:port gets
                                   "/v1" appended). Unset/empty = FlareSolverr disabled (direct curl_cffi flow).
    FLARESOLVERR_TIMEOUT           Seconds FlareSolverr may spend solving a challenge (sent as maxTimeout,
                                   in ms).                                   default: 60
    FLARESOLVERR_SESSION_TTL       Seconds a harvested clearance session is reused before FlareSolverr is
                                   called again (capped by the cf_clearance cookie's own expiry). 0 = never
                                   cache.                                    default: 1500 (25 min)
    FLARESOLVERR_FAILURE_COOLDOWN  After a failed FlareSolverr call, seconds during which it is not retried
                                   (requests proceed on the direct flow).    default: 30

Operational note: cf_clearance is bound to the client IP and User-Agent. FlareSolverr must therefore reach
the resolver from the same public IP (or proxy) as this process.
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import (
        ConnectionError as CurlConnectionError,
        RequestException,
        Timeout as CurlTimeout,
    )
except ImportError as _imp_err:  # pragma: no cover
    raise ImportError(
        'custom_resolver requires curl_cffi>=0.7 (pip install -U "curl_cffi>=0.7")'
    ) from _imp_err

logger = logging.getLogger("custom_resolver")

# --------------------------------------------------------------------------- config

RESOLVER_ENDPOINT = os.getenv("RESOLVER_ENDPOINT", "https://example.internal")
_PLACEHOLDER_ENDPOINT = "https://example.internal"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except ValueError:
        return default


def _env_path(name: str, default: str) -> str:
    p = (os.getenv(name, "") or default).strip()
    return p if p.startswith("/") else "/" + p


def _env_list(name: str, default: str) -> tuple:
    """Comma/whitespace separated env var -> ordered, de-duplicated tuple of non-empty strings."""
    raw = os.getenv(name, "")
    if not raw.strip():
        raw = default
    out: list = []
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return tuple(out)


HANDSHAKE_PATH = _env_path("RESOLVER_HANDSHAKE_PATH", "/api/9d1dd0c0")
EXCHANGE_PATH = _env_path("RESOLVER_EXCHANGE_PATH", "/api/79bf4d06")

IMPERSONATE = (os.getenv("RESOLVER_IMPERSONATE", "") or "safari17_2_ios").strip()
_FALLBACK_PROFILE = "chrome"
# Profiles tried (in order) when the handshake answers 403 with the current profile.
RETRY_PROFILES = _env_list("RESOLVER_RETRY_PROFILES", "chrome124,chrome120")
USER_AGENT = (os.getenv("RESOLVER_USER_AGENT", "") or "").strip()  # empty = let curl_cffi decide

CONNECT_TIMEOUT = _env_float("RESOLVER_CONNECT_TIMEOUT", 10.0)
READ_TIMEOUT = _env_float("RESOLVER_READ_TIMEOUT", 30.0)
DOWNLOAD_TIMEOUT = _env_float("RESOLVER_DOWNLOAD_TIMEOUT", 30.0)
DOWNLOAD_DEADLINE = _env_float("RESOLVER_DOWNLOAD_DEADLINE", 120.0)
MAX_DOWNLOAD_BYTES = _env_int("RESOLVER_MAX_BYTES", 45 * 1024 * 1024)
ALLOW_PRIVATE_DOWNLOADS = (os.getenv("RESOLVER_ALLOW_PRIVATE_DOWNLOADS", "0").strip() == "1")
PREWARM_TTL = _env_float("RESOLVER_PREWARM_TTL", 300.0)

# --- FlareSolverr (optional Cloudflare challenge solver) --------------------------------------------
FLARESOLVERR_URL = (os.getenv("FLARESOLVERR_URL", "") or "").strip()         # empty = disabled
FLARESOLVERR_TIMEOUT = _env_float("FLARESOLVERR_TIMEOUT", 60.0)
if FLARESOLVERR_TIMEOUT <= 0:
    FLARESOLVERR_TIMEOUT = 60.0
FLARESOLVERR_SESSION_TTL = max(0.0, _env_float("FLARESOLVERR_SESSION_TTL", 25 * 60.0))
FLARESOLVERR_FAILURE_COOLDOWN = max(0.0, _env_float("FLARESOLVERR_FAILURE_COOLDOWN", 30.0))
_FLARESOLVERR_HTTP_MARGIN = 15.0   # HTTP read timeout = solver budget + this (the solver needs time to answer)
_CLEARANCE_EXPIRY_MARGIN = 30.0    # stop trusting a cf_clearance cookie this many seconds before it expires

_MAX_REDIRECTS = 5
_CHUNK = 64 * 1024
_MAX_MEDIA_URL_LEN = 4096
_MAX_META_LEN = 200

_REDIRECT_CODES = (301, 302, 303, 307, 308)
_PREWARM_MAX_REDIRECTS = 3
_PREWARM_READ_TIMEOUT = 15.0     # the pre-warm must never hold up a request for long
_PROFILE_RETRY_DELAY = 0.3       # short pause before re-handshaking with another profile

_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)]}>،؛؟…"
_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_CONTENT_TYPE_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
}
_KNOWN_EXTS = {"mp3", "m4a", "aac", "ogg", "flac", "wav", "webm", "opus"}


# --------------------------------------------------------------------------- errors / types


class ResolverError(Exception):
    """Base class. `user_message` is safe to show to end users."""

    user_message = "Resolver error."

    def __init__(self, detail: str = "", user_message: str | None = None):
        super().__init__(detail or self.user_message)
        if user_message:
            self.user_message = user_message


class ResolverNotConfigured(ResolverError):
    user_message = "Resolver endpoint is not configured."


class ResolverTimeout(ResolverError):
    user_message = "The resolver took too long to respond."


class ResolverBadResponse(ResolverError):
    user_message = "The resolver returned an unusable response."


class DownloadError(ResolverError):
    user_message = "Downloading the file failed."


class DownloadTooLarge(DownloadError):
    user_message = "The file is too large to send."


class _ProfileRejected(ResolverError):
    """Internal: the installed curl_cffi does not know an explicitly requested impersonation profile."""


class _FlareSolverrError(ResolverError):
    """Internal: FlareSolverr is unreachable / failed to solve. Never escapes: the direct flow takes over."""

    user_message = "The challenge solver is unavailable."


@dataclass(frozen=True)
class ResolverResult:
    title: str
    artist: str
    download_url: str


# --------------------------------------------------------------------------- impersonating http session
#
# curl_cffi sessions are kept per thread (the bot resolves in a thread pool). A session reused within a
# thread also keeps the cookies set during the pre-warm / step-1 handshake for the step-2 exchange and
# the download.
#
# Profile selection:
#   * `_active_profile` is the process-wide default (RESOLVER_IMPERSONATE, or whatever last worked).
#   * `_tls.pinned_profile` pins the profile for the current thread while a call_resolver() -> stream_file()
#     sequence is in flight, so a concurrent profile switch in another thread can never swap the session
#     (and drop its cookies) halfway through Step 1 -> Step 2 -> download.
#
# FlareSolverr clearance (see the FlareSolverr section below):
#   * `_tls.clearance`         the clearance this thread is using (None = plain direct flow).
#   * `_tls.clearance_applied` the clearance already injected into the CURRENT session object, so cookies /
#                              User-Agent are injected once per session, and again whenever a profile switch
#                              builds a new session.

_tls = threading.local()
_profile_lock = threading.Lock()
_active_profile = IMPERSONATE


def _effective_profile() -> str:
    return getattr(_tls, "pinned_profile", None) or _active_profile


def _get_session(profile: str | None = None):
    profile = profile or _effective_profile()
    if getattr(_tls, "profile", None) != profile or getattr(_tls, "session", None) is None:
        old = getattr(_tls, "session", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        _tls.session = curl_requests.Session(impersonate=profile)
        _tls.profile = profile
        _tls.warmed_at = None          # a brand-new session has no edge cookies yet
        _tls.clearance_applied = None  # ... and no clearance cookies / User-Agent either
    clearance = getattr(_tls, "clearance", None)
    if clearance is not None and getattr(_tls, "clearance_applied", None) is not clearance:
        _inject_clearance(_tls.session, clearance)
        _tls.clearance_applied = clearance
    return _tls.session


def _reset_thread_session() -> None:
    """Drop this thread's session (cookie jar, headers, pre-warm state); the next _get_session() builds a clean one."""
    old = getattr(_tls, "session", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    _tls.session = None
    _tls.profile = None
    _tls.warmed_at = None
    _tls.clearance_applied = None


def _is_impersonation_error(exc: BaseException) -> bool:
    return "impersonat" in str(exc).lower()


def _short(url: str, n: int = 80) -> str:
    return url if len(url) <= n else url[:n] + "…"


def _send(method: str, url: str, *, fail_cls: type, fail_msg: str | None, timeout,
          headers: dict, stream: bool = False, json_body=None, profile: str | None = None):
    """
    One impersonated request, redirects NOT followed (callers handle/validate them).
    Maps transport failures to ResolverTimeout / `fail_cls`.

    `profile=None` (default): uses the thread's effective profile. If that profile is unknown to the
    installed curl_cffi, switches once (process-wide) to the "chrome" profile and retries.
    `profile="<name>"`: forces that profile for this thread's session and never touches the process-wide
    default; an unknown profile raises the internal `_ProfileRejected` so the caller can skip it.
    """
    global _active_profile
    for attempt in (0, 1):
        kwargs = {
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": False,
            "stream": stream,
        }
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            session = _get_session(profile)
            return session.request(method, url, **kwargs)
        except CurlTimeout as e:
            raise ResolverTimeout(f"timeout on {method} {_short(url)}") from e
        except CurlConnectionError as e:
            if "timed out" in str(e).lower():
                raise ResolverTimeout(f"timeout on {method} {_short(url)}") from e
            raise fail_cls(f"connection error on {method} {_short(url)}: {e}", fail_msg) from e
        except RequestException as e:
            if _is_impersonation_error(e):
                if profile is not None:
                    raise _ProfileRejected(f"impersonation profile {profile!r} rejected: {e}") from e
                if attempt == 0:
                    with _profile_lock:
                        if _active_profile != _FALLBACK_PROFILE:
                            logger.warning("[resolver] impersonation profile %r rejected (%s); falling back to %r",
                                           _active_profile, e, _FALLBACK_PROFILE)
                            _active_profile = _FALLBACK_PROFILE
                    _tls.pinned_profile = None      # follow the (new) process-wide profile
                    continue
            raise fail_cls(f"request failed on {method} {_short(url)}: {e}", fail_msg) from e
    raise fail_cls(f"request failed on {method} {_short(url)}", fail_msg)


def is_configured() -> bool:
    """True once RESOLVER_ENDPOINT points at something other than the placeholder."""
    ep = (RESOLVER_ENDPOINT or "").strip().rstrip("/")
    return bool(ep) and ep != _PLACEHOLDER_ENDPOINT


def _endpoint_base() -> str:
    """RESOLVER_ENDPOINT without a trailing slash (what API paths are appended to)."""
    return RESOLVER_ENDPOINT.rstrip("/")


def _endpoint_origin() -> str:
    p = urlparse(RESOLVER_ENDPOINT)
    if not p.scheme or not p.netloc:
        return RESOLVER_ENDPOINT.rstrip("/")
    return f"{p.scheme}://{p.netloc}"


def _effective_user_agent() -> str:
    """
    User-Agent to send explicitly ("" = let the impersonation profile decide). While a FlareSolverr clearance
    is active its User-Agent always wins: cf_clearance is bound to the browser identity that solved the challenge.
    """
    clearance = getattr(_tls, "clearance", None)
    if clearance is not None:
        return clearance.user_agent
    return USER_AGENT


def _sync_user_agent(headers: dict) -> None:
    """Re-align an already built headers dict (mutated in place) with the current effective User-Agent."""
    ua = _effective_user_agent()
    if ua:
        headers["User-Agent"] = ua
    else:
        headers.pop("User-Agent", None)


def _build_headers() -> dict:
    """
    Headers for the same-origin XHR/fetch-style API calls (Step 1 and Step 2). Mirrors what a browser
    sends for `fetch()` from a page on the resolver's own origin. The User-Agent (and the client-hint
    headers of Chromium profiles) come from the curl_cffi impersonation profile unless overridden.
    """
    h = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": _endpoint_origin(),
        "Referer": _endpoint_base() + "/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Priority": "u=1, i",
    }
    ua = _effective_user_agent()
    if ua:
        h["User-Agent"] = ua
    return h


def _build_navigation_headers() -> dict:
    """Headers for a top-level HTML page load (the silent root pre-warm GET)."""
    h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
    }
    ua = _effective_user_agent()
    if ua:
        h["User-Agent"] = ua
    return h


def _origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def _prewarm_session(*, force: bool = False, profile: str | None = None) -> None:
    """
    Silent browser-style `GET {RESOLVER_ENDPOINT}/` on the current thread's session so it picks up the
    edge cookies, TLS session tickets and cache state a real visitor would have before calling the API.

    Never raises and never blocks for long: any failure (timeout, 403, odd status, redirect off-origin)
    is logged at DEBUG and ignored — the real request that follows is what decides success. Same-origin
    redirects are followed (max 3) because they may carry Set-Cookie. A session that was pre-warmed less
    than RESOLVER_PREWARM_TTL seconds ago is not warmed again unless `force=True`.
    """
    try:
        _get_session(profile)
        if not force and PREWARM_TTL > 0:
            warmed_at = getattr(_tls, "warmed_at", None)
            if warmed_at is not None and (time.monotonic() - warmed_at) < PREWARM_TTL:
                return

        origin = _endpoint_origin()
        url = _endpoint_base() + "/"
        headers = _build_navigation_headers()
        timeout = (CONNECT_TIMEOUT, min(READ_TIMEOUT, _PREWARM_READ_TIMEOUT))

        for _ in range(_PREWARM_MAX_REDIRECTS + 1):
            resp = _send("GET", url, fail_cls=ResolverError, fail_msg=None,
                         timeout=timeout, headers=headers, profile=profile)
            code = resp.status_code
            loc = resp.headers.get("Location") if code in _REDIRECT_CODES else None
            _close_quietly(resp)
            logger.debug("[resolver] pre-warm GET %s -> HTTP %s (profile %s)", _short(url), code,
                         getattr(_tls, "profile", "?"))
            if not loc:
                break
            nxt = urljoin(url, loc)
            if _origin_of(nxt) != origin.lower():
                break
            url = nxt

        _tls.warmed_at = time.monotonic()
    except Exception as e:  # pre-warm is strictly best-effort
        logger.debug("[resolver] pre-warm skipped: %s", e)


def _promote_profile(profile: str) -> None:
    """A retry profile got past the 403: pin it for this thread and make it the process-wide default."""
    global _active_profile
    _tls.pinned_profile = profile
    with _profile_lock:
        if _active_profile != profile:
            logger.info("[resolver] impersonation profile %r now default (was %r)", profile, _active_profile)
            _active_profile = profile


def _handshake_with_profile_retry(url: str, *, headers: dict, timeout, fail_msg: str):
    """
    Step-1 GET.
      1. HTTP 403 while a FlareSolverr clearance is active -> the clearance is considered stale: it is
         invalidated, a fresh one is fetched from FlareSolverr (once) and the handshake is repeated.
      2. Still HTTP 403 (Cloudflare edge block) -> re-handshake with each profile in RETRY_PROFILES that
         hasn't been tried yet — each on a fresh session that is pre-warmed first (a fresh session only
         carries the clearance, never cookies issued to the blocked fingerprint).

    Returns the first non-403 response (a retry profile is then pinned/promoted), or the last 403 response
    if everything was blocked / unavailable — the caller's `_raise_for_status` turns that into the
    usual "rejected the request" ResolverError.

    `headers` is mutated in place if the clearance (and with it the User-Agent) changes, so Step 2 keeps
    using the identity that passed the handshake.
    """
    resp = _send("GET", url, fail_cls=ResolverError, fail_msg=fail_msg, timeout=timeout, headers=headers)
    if resp.status_code == 403:
        resp = _refresh_clearance_after_403(resp, url, headers=headers, timeout=timeout, fail_msg=fail_msg)
    if resp.status_code != 403:
        return resp

    current = _effective_profile()
    tried = {current}
    for candidate in RETRY_PROFILES:
        if candidate in tried:
            continue
        tried.add(candidate)
        logger.warning("[resolver] handshake HTTP 403 with profile %r; retrying with %r", current, candidate)
        time.sleep(_PROFILE_RETRY_DELAY)
        try:
            _prewarm_session(force=True, profile=candidate)
            retry = _send("GET", url, fail_cls=ResolverError, fail_msg=fail_msg, timeout=timeout,
                          headers=headers, profile=candidate)
        except _ProfileRejected as e:
            logger.warning("[resolver] retry profile %r unavailable (%s); skipping", candidate, e)
            continue
        _close_quietly(resp)
        resp = retry
        if resp.status_code != 403:
            _promote_profile(candidate)
            break
    return resp


# --------------------------------------------------------------------------- FlareSolverr clearance
#
# FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) drives a real Chromium that passes the
# Cloudflare challenge. We only call it for the resolver's root page, harvest the resulting cookies
# (cf_clearance, __cf_bm, ...) and the exact User-Agent, and then keep talking to the resolver with
# curl_cffi — the solver is never in the data path, so a clearance is reused for FLARESOLVERR_SESSION_TTL.
#
#   _fs_cache         the one process-wide clearance (None = nothing cached)
#   _fs_state_lock    short lock guarding _fs_cache / _fs_failed_until
#   _fs_fetch_lock    single-flight: while one thread is solving (up to FLARESOLVERR_TIMEOUT seconds) the
#                     others wait for its result instead of launching their own browser challenges
#   _fs_failed_until  monotonic deadline of the failure cooldown (no hammering of a dead solver)


@dataclass(frozen=True)
class _ClearanceCookie:
    name: str
    value: str
    domain: str
    path: str
    secure: bool


@dataclass(frozen=True, eq=False)
class _Clearance:
    """
    One FlareSolverr solve: cookies + the User-Agent of the browser that obtained them. Immutable and
    compared by identity — `a is b` means "the same solve", which is how stale entries are recognised.
    """
    cookies: tuple                  # tuple[_ClearanceCookie, ...]
    user_agent: str
    obtained_at: float              # time.monotonic()
    expires_at: float               # time.monotonic() deadline


_fs_cache: _Clearance | None = None
_fs_failed_until = 0.0
_fs_state_lock = threading.Lock()
_fs_fetch_lock = threading.Lock()


def _flaresolverr_endpoint() -> str:
    """FLARESOLVERR_URL, validated; a bare scheme://host:port gets the standard "/v1" path appended."""
    url = FLARESOLVERR_URL.strip()
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        raise _FlareSolverrError(f"FLARESOLVERR_URL is not a valid http(s) URL: {_short(url)!r}")
    if p.path in ("", "/"):
        url = url.rstrip("/") + "/v1"
    return url


def _cookie_domain_matches(domain: str, host: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    h = (host or "").lower()
    return bool(d) and (h == d or h.endswith("." + d))


def _parse_flaresolverr_response(data, *, host: str) -> _Clearance:
    """FlareSolverr JSON reply -> _Clearance. Raises _FlareSolverrError if the solve did not succeed."""
    if not isinstance(data, dict):
        raise _FlareSolverrError("unexpected response from FlareSolverr (not a JSON object)")
    status = str(data.get("status", "")).lower()
    if status != "ok":
        raise _FlareSolverrError(
            f"FlareSolverr reported status {status or '?'!r}: {_clean_meta(data.get('message')) or 'no message'}")
    solution = data.get("solution")
    if not isinstance(solution, dict):
        raise _FlareSolverrError("FlareSolverr response has no 'solution'")

    user_agent = str(solution.get("userAgent") or "").strip()
    if not user_agent or _CTRL_RE.search(user_agent):
        raise _FlareSolverrError("FlareSolverr solution has no usable 'userAgent'")

    raw_cookies = solution.get("cookies")
    if not isinstance(raw_cookies, list):
        raw_cookies = []

    cookies: list = []
    cf_expiry_epoch: float | None = None
    for c in raw_cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        value = c.get("value")
        if not name or value is None:
            continue
        value = str(value)
        if _CTRL_RE.search(name) or _CTRL_RE.search(value):
            continue
        domain = str(c.get("domain") or "").strip().lower() or host
        if not _cookie_domain_matches(domain, host):
            continue                                  # a cookie the browser would never send to the resolver
        path = str(c.get("path") or "/").strip() or "/"
        cookies.append(_ClearanceCookie(name=name, value=value, domain=domain, path=path,
                                        secure=bool(c.get("secure"))))
        exp = c.get("expires")
        if (name == "cf_clearance" and isinstance(exp, (int, float)) and not isinstance(exp, bool)
                and exp > 0):
            cf_expiry_epoch = float(exp) if cf_expiry_epoch is None else min(cf_expiry_epoch, float(exp))

    now = time.monotonic()
    expires_at = now + FLARESOLVERR_SESSION_TTL
    if cf_expiry_epoch is not None:      # never outlive the clearance cookie itself
        remaining = cf_expiry_epoch - time.time() - _CLEARANCE_EXPIRY_MARGIN
        expires_at = min(expires_at, now + max(0.0, remaining))

    return _Clearance(cookies=tuple(cookies), user_agent=user_agent, obtained_at=now, expires_at=expires_at)


def _request_flaresolverr_solution() -> _Clearance:
    """One uncached FlareSolverr round trip: POST request.get for the resolver's root page."""
    endpoint = _flaresolverr_endpoint()
    host = (urlparse(_endpoint_base()).hostname or "").lower()
    payload = {
        "cmd": "request.get",
        "url": f"{_endpoint_base()}/",
        "maxTimeout": int(FLARESOLVERR_TIMEOUT * 1000),
    }
    started = time.monotonic()
    try:
        resp = curl_requests.post(
            endpoint,
            json=payload,
            timeout=(CONNECT_TIMEOUT, FLARESOLVERR_TIMEOUT + _FLARESOLVERR_HTTP_MARGIN),
            allow_redirects=False,
        )
    except CurlTimeout as e:
        raise _FlareSolverrError(f"timeout waiting for FlareSolverr at {_short(endpoint)}") from e
    except CurlConnectionError as e:
        raise _FlareSolverrError(f"cannot connect to FlareSolverr at {_short(endpoint)}: {e}") from e
    except RequestException as e:
        raise _FlareSolverrError(f"FlareSolverr request failed: {e}") from e

    try:
        data = resp.json()           # FlareSolverr also answers errors (HTTP 500) with a JSON body
    except ValueError:
        data = None
    if data is None:
        raise _FlareSolverrError(f"FlareSolverr answered HTTP {resp.status_code} with a non-JSON body")

    clearance = _parse_flaresolverr_response(data, host=host)
    logger.info("[resolver] FlareSolverr solved %s in %.1fs: %d cookie(s) [%s], cf_clearance=%s, reuse for %.0fs",
                host or "?", time.monotonic() - started, len(clearance.cookies),
                ", ".join(c.name for c in clearance.cookies) or "-",
                "yes" if any(c.name == "cf_clearance" for c in clearance.cookies) else "no",
                max(0.0, clearance.expires_at - time.monotonic()))
    return clearance


def _fs_cache_lookup() -> _Clearance | None:
    """The cached clearance if it is still valid (an expired one is dropped), else None."""
    global _fs_cache
    with _fs_state_lock:
        cached = _fs_cache
        if cached is None:
            return None
        if time.monotonic() >= cached.expires_at:
            _fs_cache = None
            return None
        return cached


def _invalidate_flaresolverr_session(stale: _Clearance | None = None) -> None:
    """
    Drop the cached clearance. With `stale`, only if the cache still holds that very solve — if another
    thread has already replaced it with a fresh one, that fresh clearance is left alone.
    """
    global _fs_cache
    with _fs_state_lock:
        if _fs_cache is not None and (stale is None or _fs_cache is stale):
            _fs_cache = None


def _get_flaresolverr_session() -> _Clearance | None:
    """
    Cached Cloudflare clearance (cookies + userAgent) for RESOLVER_ENDPOINT, or None.

    * FLARESOLVERR_URL unset/empty -> None immediately (plain direct curl_cffi flow).
    * Cache hit (younger than FLARESOLVERR_SESSION_TTL and its cf_clearance cookie) -> returned as is.
    * Cache miss -> exactly one thread POSTs
          {"cmd": "request.get", "url": "<RESOLVER_ENDPOINT>/", "maxTimeout": <FLARESOLVERR_TIMEOUT in ms>}
      to FLARESOLVERR_URL while concurrent callers wait for its result.
    * FlareSolverr unreachable / unable to solve -> None, and no new attempt for
      FLARESOLVERR_FAILURE_COOLDOWN seconds. This function never raises: the caller simply continues
      without a clearance.
    """
    global _fs_cache, _fs_failed_until
    if not FLARESOLVERR_URL:
        return None

    cached = _fs_cache_lookup()
    if cached is not None:
        return cached

    with _fs_fetch_lock:
        cached = _fs_cache_lookup()               # another thread may have finished while we waited
        if cached is not None:
            return cached
        with _fs_state_lock:
            if time.monotonic() < _fs_failed_until:
                return None
        try:
            fresh = _request_flaresolverr_solution()
        except _FlareSolverrError as e:
            logger.warning("[resolver] FlareSolverr unavailable (%s); continuing without a clearance for %.0fs",
                           e, FLARESOLVERR_FAILURE_COOLDOWN)
            fresh = None
        except Exception:                          # never let the optional solver break the direct flow
            logger.exception("[resolver] unexpected FlareSolverr failure; continuing without a clearance")
            fresh = None
        with _fs_state_lock:
            if fresh is None:
                _fs_failed_until = time.monotonic() + FLARESOLVERR_FAILURE_COOLDOWN
            else:
                _fs_cache = fresh
                _fs_failed_until = 0.0
        return fresh


def _inject_clearance(session, clearance: _Clearance) -> None:
    """Put the harvested cookies into `session`'s cookie jar and make its User-Agent the solver's."""
    for ck in clearance.cookies:
        try:
            try:
                session.cookies.set(ck.name, ck.value, domain=ck.domain, path=ck.path, secure=ck.secure)
            except TypeError:                      # older curl_cffi: set() has no `secure` argument
                session.cookies.set(ck.name, ck.value, domain=ck.domain, path=ck.path)
        except Exception as e:
            logger.debug("[resolver] could not inject cookie %r: %s", ck.name, e)
    try:
        session.headers["User-Agent"] = clearance.user_agent
    except Exception as e:
        logger.debug("[resolver] could not override session User-Agent: %s", e)


def _chrome_profile_for_ua(user_agent: str) -> str | None:
    """Newest plain `chromeNNN` curl_cffi profile that is not newer than the Chrome in `user_agent`."""
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    if not m:
        return None
    major = int(m.group(1))
    try:
        names = list(curl_requests.BrowserType.__members__)
    except Exception:
        return None
    best: tuple | None = None
    for name in names:
        pm = re.fullmatch(r"chrome(\d+)", name)
        if pm and int(pm.group(1)) <= major and (best is None or int(pm.group(1)) > best[0]):
            best = (int(pm.group(1)), name)
    return best[1] if best else None


def _clearance_profile(clearance: _Clearance) -> str:
    """
    TLS profile to use with a clearance. FlareSolverr solves with Chromium, so the TLS/HTTP2 fingerprint
    should be Chromium-like too: a Chrome profile configured by the operator is kept; anything else
    (e.g. the Safari default) is replaced by the Chrome profile closest to the solver's Chrome version.
    """
    base = _active_profile
    if base.lower().startswith("chrome"):
        return base
    return _chrome_profile_for_ua(clearance.user_agent) or _FALLBACK_PROFILE


def _bind_clearance(clearance: _Clearance | None) -> None:
    """Make this thread use `clearance` (None = plain flow). A change throws the old session (and jar) away."""
    if getattr(_tls, "clearance", None) is clearance:
        return
    _tls.clearance = clearance
    _reset_thread_session()


def _use_clearance(clearance: _Clearance | None) -> None:
    """Bind `clearance` to this thread and pin the matching TLS profile (None -> process-wide profile)."""
    _bind_clearance(clearance)
    _tls.pinned_profile = _clearance_profile(clearance) if clearance is not None else None


def _refresh_clearance_after_403(resp, url: str, *, headers: dict, timeout, fail_msg: str):
    """
    Step-1 answered HTTP 403. If a clearance is active it is evidently stale (expired, IP/UA drift, revoked):
    invalidate it, get a fresh solve from FlareSolverr — once — and repeat the handshake on a clean session.
    Returns the new response, or `resp` unchanged when there is no clearance / the solver cannot help (in
    which case the thread falls back to the plain flow and the profile retries take over).
    """
    stale = getattr(_tls, "clearance", None)
    if stale is None:
        return resp

    logger.warning("[resolver] handshake HTTP 403 despite an active FlareSolverr clearance; "
                   "invalidating it and re-solving once")
    _invalidate_flaresolverr_session(stale)
    fresh = _get_flaresolverr_session()
    if fresh is None or fresh is stale:
        _use_clearance(None)
        _sync_user_agent(headers)
        return resp

    _use_clearance(fresh)
    _sync_user_agent(headers)
    _prewarm_session(force=True)
    _close_quietly(resp)
    return _send("GET", url, fail_cls=ResolverError, fail_msg=fail_msg, timeout=timeout, headers=headers)


# --------------------------------------------------------------------------- 1) extract


def extract_target_url(text: str) -> str | None:
    """Return the first http(s) link found in `text`, or None."""
    if not text:
        return None
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0)
    # strip trailing punctuation that belongs to the sentence, not the URL
    while url and url[-1] in _TRAILING_PUNCT:
        # keep a closing ")" if the URL contains a matching "(" (e.g. wiki links)
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return None
    return url


# --------------------------------------------------------------------------- 2) resolve (two steps)


def _raise_for_status(resp, step: str) -> None:
    code = resp.status_code
    if code == 429:
        raise ResolverError(f"{step}: rate limited (429)", "The resolver is busy right now. Try again shortly.")
    if code in (401, 403):
        raise ResolverError(f"{step}: HTTP {code} (blocked / anti-bot challenge?)",
                            "The resolver rejected the request.")
    if code >= 400:
        raise ResolverError(f"{step}: HTTP {code}", "The resolver could not process this link.")
    if 300 <= code < 400:
        raise ResolverBadResponse(f"{step}: unexpected redirect ({code})")


def _clean_meta(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    s = _CTRL_RE.sub(" ", str(value))
    return _WS_RE.sub(" ", s).strip()[:_MAX_META_LEN]


def _pick(meta: dict, keys: tuple) -> str:
    for k in keys:
        v = _clean_meta(meta.get(k))
        if v:
            return v
    return ""


def _meta_dict(obj) -> dict:
    """The dict that holds title/artist: the object itself, or a nested data/metadata/meta object."""
    if not isinstance(obj, dict):
        return {}
    if any(k in obj for k in ("title", "name", "artist", "author", "performer")):
        return obj
    for k in ("data", "metadata", "meta"):
        inner = obj.get(k)
        if isinstance(inner, dict):
            return inner
    return obj


def _parse_handshake(resp) -> tuple[str, str, object]:
    """
    Step-1 response -> (title, artist, payload for step 2).
      * JSON object/array : payload is the parsed JSON, passed through unchanged.
      * anything else     : treated as opaque text; payload is {"data": "<text>"}.
    """
    text = (resp.text or "").strip()
    if not text:
        raise ResolverBadResponse("handshake: empty response body")

    parsed = None
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None

    if isinstance(parsed, (dict, list)):
        meta = _meta_dict(parsed)
        title = _pick(meta, ("title", "name"))
        artist = _pick(meta, ("artist", "author", "performer"))
        payload: object = parsed
    else:
        if isinstance(parsed, str):          # a bare JSON string, e.g. "abc123"
            text = parsed.strip()
        title, artist = "", ""
        payload = {"data": text}

    return title or "Unknown title", artist or "Unknown artist", payload


def _parse_media_url(resp) -> str:
    """Step-2 response body -> validated direct media URL."""
    body = (resp.text or "").strip()
    if body[:1] == '"':                      # JSON-encoded string
        try:
            decoded = json.loads(body)
            if isinstance(decoded, str):
                body = decoded.strip()
        except ValueError:
            pass
    if not body:
        raise ResolverBadResponse("exchange: empty response body",
                                  "The resolver found nothing to download for this link.")
    if body[:1] in "{[":
        raise ResolverBadResponse("exchange: got JSON, expected a plain media URL")
    if len(body) > _MAX_MEDIA_URL_LEN or re.search(r"\s", body):
        raise ResolverBadResponse("exchange: response is not a single URL")
    p = urlparse(body)
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        raise ResolverBadResponse("exchange: response is not an http(s) URL")
    return body


def call_resolver(target_url: str) -> ResolverResult:
    """
    Two-step resolution (preceded by an optional FlareSolverr clearance and a silent session pre-warm):
      0a. FlareSolverr (only if FLARESOLVERR_URL is set): cached cookies + User-Agent injected into the session
      0.  GET  {ENDPOINT}/                                           -> edge cookies / TLS session (ignored)
      1.  GET  {ENDPOINT}{HANDSHAKE_PATH}?q=<urlencoded target_url>  -> metadata
          (HTTP 403 with an active clearance -> re-solve once and retry; then the profiles in
           RESOLVER_RETRY_PROFILES)
      2.  POST {ENDPOINT}{EXCHANGE_PATH}  (json = step-1 payload)    -> direct media URL (text)
    All steps, and a following stream_file() in the same thread, share one session, its cookies and
    its browser identity.
    """
    if not is_configured():
        raise ResolverNotConfigured("RESOLVER_ENDPOINT is not set")
    if not target_url:
        raise ResolverBadResponse("empty target url", "No link to resolve.")

    base = _endpoint_base()
    timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)
    unreachable = "Could not reach the resolver."

    # start from the process-wide profile; pinned again once Step 1 has succeeded
    _tls.pinned_profile = None

    # ---- step 0a: Cloudflare clearance from FlareSolverr (cached; None when disabled / unavailable, in which
    #      case everything below is the plain direct curl_cffi flow)
    _use_clearance(_get_flaresolverr_session())

    headers = _build_headers()       # built after the clearance: it decides the User-Agent

    # ---- step 0: silent pre-warm of the browser session (root GET, HTML navigation headers)
    _prewarm_session()

    # ---- step 1: handshake & metadata (re-handshakes with another profile on HTTP 403)
    handshake_url = f"{base}{HANDSHAKE_PATH}?q={quote(target_url, safe='')}"
    r1 = _handshake_with_profile_retry(handshake_url, headers=headers, timeout=timeout, fail_msg=unreachable)
    _raise_for_status(r1, "handshake")
    title, artist, payload = _parse_handshake(r1)

    # keep Step 2 and the download on exactly the session/profile that passed the handshake
    _tls.pinned_profile = getattr(_tls, "profile", None) or _effective_profile()

    # ---- step 2: token exchange
    r2 = _send("POST", f"{base}{EXCHANGE_PATH}", fail_cls=ResolverError, fail_msg=unreachable,
               timeout=timeout, headers=headers, json_body=payload)
    _raise_for_status(r2, "exchange")
    media_url = _parse_media_url(r2)

    return ResolverResult(title=title, artist=artist, download_url=media_url)


# --------------------------------------------------------------------------- 3) download


def _host_is_public(host: str) -> bool:
    """False if `host` resolves to any private/loopback/link-local/reserved address."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def _check_download_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        raise DownloadError(f"bad download url scheme/host: {url[:80]}")
    if not ALLOW_PRIVATE_DOWNLOADS and not _host_is_public(p.hostname):
        raise DownloadError(f"download host is not public: {p.hostname}")


def _guess_ext(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ct]
    tail = urlparse(url).path.rsplit(".", 1)
    if len(tail) == 2 and tail[1].lower() in _KNOWN_EXTS:
        return tail[1].lower()
    return "mp3"


def _close_quietly(resp) -> None:
    if resp is None:
        return
    try:
        resp.close()
    except Exception:
        pass


def stream_file(download_url: str) -> io.BytesIO:
    """
    Download `download_url` through the impersonated session into an in-memory BytesIO (rewound to 0).
    The returned buffer has a `.name` attribute ("audio.<ext>") so Telegram sees a proper filename.
    Enforces: http(s) only, public hosts only (unless allowed), manual redirect handling with
    re-validation of every hop, a size cap (Content-Length and running total), a stall timeout, and a
    wall-clock deadline.
    """
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    url = download_url
    headers = {
        "Referer": _endpoint_origin() + "/",
        "Accept": "*/*",
    }
    ua = _effective_user_agent()
    if ua:
        headers["User-Agent"] = ua

    resp = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            _check_download_url(url)
            resp = _send("GET", url, fail_cls=DownloadError, fail_msg=None,
                         timeout=(CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT), headers=headers, stream=True)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                _close_quietly(resp)
                resp = None
                if not loc:
                    raise DownloadError("redirect without Location header")
                url = urljoin(url, loc)
                continue
            break
        else:
            raise DownloadError("too many redirects")

        if resp.status_code >= 400:
            raise DownloadError(f"HTTP {resp.status_code} while downloading")

        declared = resp.headers.get("Content-Length")
        if declared and str(declared).isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise DownloadTooLarge(f"declared size {declared} exceeds cap")

        buf = io.BytesIO()
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise DownloadTooLarge(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")
                if time.monotonic() > deadline:
                    raise ResolverTimeout("download deadline exceeded")
                buf.write(chunk)
        except ResolverError:
            raise
        except CurlTimeout as e:
            raise ResolverTimeout("timeout while downloading") from e
        except CurlConnectionError as e:
            if "timed out" in str(e).lower():
                raise ResolverTimeout("timeout while downloading") from e
            raise DownloadError(f"stream error: {e}") from e
        except Exception as e:  # curl errors surfaced mid-stream
            raise DownloadError(f"stream error: {e}") from e

        if total == 0:
            raise DownloadError("downloaded file is empty")

        buf.seek(0)
        buf.name = f"audio.{_guess_ext(resp.headers.get('Content-Type', ''), url)}"
        return buf
    finally:
        _close_quietly(resp)
