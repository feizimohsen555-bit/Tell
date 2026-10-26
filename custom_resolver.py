# -*- coding: utf-8 -*-
"""
custom_resolver.py — generic JSON/text webhook resolver client (curl_cffi edition).

All outgoing HTTP goes through `curl_cffi.requests.Session(impersonate=...)`, so the TLS/HTTP2
handshake matches a real browser instead of the python-requests / OpenSSL fingerprint.

Flow:
    text -> extract_target_url() -> call_resolver() -> stream_file()
                                    |                  (download into BytesIO)
                                    +- Step 0  GET  {ENDPOINT}/   (silent browser-style pre-warm: edge cookies,
                                    |          TLS session, cache state; failures are ignored)
                                    +- Step 1  GET  {ENDPOINT}{HANDSHAKE_PATH}?q=<urlencoded target>
                                    |          -> metadata (JSON object/array, or plain text)
                                    |          (on HTTP 403 the handshake is retried with the next profile
                                    |           in RESOLVER_RETRY_PROFILES, on a fresh pre-warmed session;
                                    |           if every profile is still blocked and FLARESOLVERR_URL is set,
                                    |           FlareSolverr solves the challenge in a real browser and its
                                    |           cookies + User-Agent are loaded into the session)
                                    +- Step 2  POST {ENDPOINT}{EXCHANGE_PATH}  json=<step-1 payload>
                                               -> response body is the final direct media URL

The per-thread session (and therefore its cookies and impersonation profile) is shared by pre-warm,
Step 1, Step 2 and stream_file().

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
    RESOLVER_CONNECT_TIMEOUT       Connect timeout, seconds.                 default: 10
    RESOLVER_READ_TIMEOUT          Read timeout of each resolver call.       default: 30
    RESOLVER_DOWNLOAD_TIMEOUT      Stall timeout while downloading.          default: 30
    RESOLVER_DOWNLOAD_DEADLINE     Hard wall-clock cap for a whole download. default: 120
    RESOLVER_MAX_BYTES             Max download size in bytes.               default: 45 MiB
    RESOLVER_ALLOW_PRIVATE_DOWNLOADS=1   Allow media URLs that resolve to private/loopback addresses.

FlareSolverr fallback (optional; used only when the handshake is still HTTP 403 after all profiles):
    FLARESOLVERR_URL               e.g. http://flaresolverr:8191/v1   (empty = fallback disabled)
    FLARESOLVERR_TIMEOUT           Max seconds FlareSolverr may spend on one challenge.   default: 60
    FLARESOLVERR_TTL               Seconds a solved clearance (cookies + UA) is reused process-wide,
                                   so FlareSolverr is not hit on every request.            default: 1500
    FLARESOLVERR_PROFILE           curl_cffi profile used together with the clearance.      default: chrome
"""

from __future__ import annotations

import html
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

FLARESOLVERR_URL = (os.getenv("FLARESOLVERR_URL", "") or "").strip()
FLARESOLVERR_TIMEOUT = _env_float("FLARESOLVERR_TIMEOUT", 60.0)
FLARESOLVERR_TTL = _env_float("FLARESOLVERR_TTL", 1500.0)
FLARESOLVERR_PROFILE = (os.getenv("FLARESOLVERR_PROFILE", "") or "chrome").strip()

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
    return _tls.session


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


def _current_ua() -> str:
    """User-Agent override: the one FlareSolverr's browser used (cf_clearance is bound to it), else RESOLVER_USER_AGENT."""
    return getattr(_tls, "ua_override", None) or USER_AGENT


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
    ua = _current_ua()
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
    ua = _current_ua()
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
    Step-1 GET. If it answers HTTP 403 (Cloudflare edge block), re-handshake with each profile in
    RETRY_PROFILES that hasn't been tried yet — each on a fresh session that is pre-warmed first, since
    cookies issued to the blocked fingerprint must not leak into the next attempt.

    Returns the first non-403 response (that profile is then pinned/promoted), or the last 403 response
    if every profile was blocked / unavailable — the caller's `_raise_for_status` turns that into the
    usual "rejected the request" ResolverError.
    """
    resp = _send("GET", url, fail_cls=ResolverError, fail_msg=fail_msg, timeout=timeout, headers=headers)
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


# --------------------------------------------------------------------------- FlareSolverr fallback
#
# When Cloudflare still answers 403 to every curl_cffi profile, FlareSolverr (a headless Chrome) opens the
# handshake URL, passes the challenge and returns the cookies (cf_clearance, ...) plus the User-Agent it used.
# Those are loaded into the thread's curl_cffi session, and the normal Step 1 / Step 2 / download flow
# continues on it. The clearance is cached process-wide for FLARESOLVERR_TTL seconds.

_fs_lock = threading.Lock()
_fs_cache: dict = {}          # {"cookies": [...], "ua": str, "at": time.monotonic()}


class _TextResponse:
    """Minimal stand-in for a curl_cffi response, built from a body FlareSolverr fetched."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.headers: dict = {}

    def json(self):
        return json.loads(self.text)

    def close(self) -> None:
        pass


def flaresolverr_configured() -> bool:
    return bool(FLARESOLVERR_URL)


def _fs_endpoint() -> str:
    u = FLARESOLVERR_URL.rstrip("/")
    return u if urlparse(u).path.rstrip("/").endswith("/v1") else u + "/v1"


def _fs_cache_fresh() -> bool:
    at = _fs_cache.get("at")
    return at is not None and (time.monotonic() - at) < FLARESOLVERR_TTL


def _fs_body_text(rendered: str) -> str:
    """Browser-rendered JSON/text (`<pre>{...}</pre>`) -> the raw text."""
    s = (rendered or "").strip()
    if s[:1] in ("{", "["):
        return s
    m = re.search(r"<pre[^>]*>(.*?)</pre>", s, re.IGNORECASE | re.DOTALL)
    s = m.group(1) if m else re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s).strip()


def _fs_solve(url: str) -> dict:
    """Ask FlareSolverr to open `url` in a real browser. Returns its `solution` dict or raises ResolverError."""
    payload = {"cmd": "request.get", "url": url, "maxTimeout": int(FLARESOLVERR_TIMEOUT * 1000)}
    unavailable = "The anti-bot solver is unavailable."
    try:
        r = curl_requests.post(_fs_endpoint(), json=payload,
                               timeout=(CONNECT_TIMEOUT, FLARESOLVERR_TIMEOUT + 15))
    except CurlTimeout as e:
        raise ResolverTimeout("FlareSolverr timed out") from e
    except Exception as e:
        raise ResolverError(f"FlareSolverr unreachable: {e}", unavailable) from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.status_code != 200 or data.get("status") != "ok":
        msg = str(data.get("message") or "")[:150]
        raise ResolverError(f"FlareSolverr failed: HTTP {r.status_code} {msg}",
                            "The anti-bot solver could not pass the challenge.")
    sol = data.get("solution")
    if not isinstance(sol, dict):
        raise ResolverError("FlareSolverr returned no solution", unavailable)
    return sol


def _fs_apply_cache() -> bool:
    """Load the cached clearance (cookies + UA) into this thread's session. True if something was applied."""
    if not _fs_cache_fresh():
        return False
    _tls.pinned_profile = FLARESOLVERR_PROFILE
    session = _get_session(FLARESOLVERR_PROFILE)
    for c in _fs_cache.get("cookies") or []:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        try:
            session.cookies.set(name, value, domain=c.get("domain") or "", path=c.get("path") or "/")
        except Exception as e:
            logger.debug("[resolver] could not set FlareSolverr cookie %r: %s", name, e)
    _tls.ua_override = _fs_cache.get("ua") or None
    return True


def _handshake_via_flaresolverr(url: str, *, stale_stamp, timeout, fail_msg: str):
    """
    Step-1 fallback after HTTP 403. Gets a clearance from FlareSolverr (unless another thread just refreshed
    it), loads it into this thread's session and repeats the handshake with curl_cffi. If curl_cffi is still
    blocked but the browser itself fetched a JSON body, that body is used as the handshake response.
    Returns (response, headers) — the headers must be reused for Step 2.
    """
    solution = None
    with _fs_lock:
        if not (_fs_cache_fresh() and _fs_cache.get("at") != stale_stamp):
            logger.warning("[resolver] handshake blocked (HTTP 403); asking FlareSolverr")
            solution = _fs_solve(url)
            _fs_cache.update(cookies=solution.get("cookies") or [],
                             ua=str(solution.get("userAgent") or ""),
                             at=time.monotonic())
    _fs_apply_cache()
    headers = _build_headers()
    resp = _send("GET", url, fail_cls=ResolverError, fail_msg=fail_msg, timeout=timeout, headers=headers)

    if resp.status_code == 403 and solution is not None and solution.get("status") == 200:
        body = _fs_body_text(solution.get("response"))
        if body[:1] in ("{", "["):
            logger.warning("[resolver] still 403 with FlareSolverr cookies; using the browser-fetched handshake body")
            _close_quietly(resp)
            resp = _TextResponse(200, body)
    return resp, headers


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
    Two-step resolution (preceded by a silent session pre-warm):
      0. GET  {ENDPOINT}/                                            -> edge cookies / TLS session (ignored)
      1. GET  {ENDPOINT}{HANDSHAKE_PATH}?q=<urlencoded target_url>   -> metadata
         (HTTP 403 -> retried with the profiles in RESOLVER_RETRY_PROFILES,
          then via FlareSolverr if FLARESOLVERR_URL is set)
      2. POST {ENDPOINT}{EXCHANGE_PATH}  (json = step-1 payload)     -> direct media URL (text)
    All steps, and a following stream_file() in the same thread, share one session and its cookies.
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
    _tls.ua_override = None

    # a still-fresh FlareSolverr clearance (cookies + UA) is loaded up front, so most calls never need it again
    use_fs = flaresolverr_configured()
    fs_stamp = _fs_cache.get("at") if (use_fs and _fs_apply_cache()) else None
    headers = _build_headers()

    # ---- step 0: silent pre-warm of the browser session (root GET, HTML navigation headers)
    _prewarm_session()

    # ---- step 1: handshake & metadata (re-handshakes with another profile on HTTP 403,
    #              then falls back to FlareSolverr if that is configured)
    handshake_url = f"{base}{HANDSHAKE_PATH}?q={quote(target_url, safe='')}"
    if fs_stamp is not None:
        r1 = _send("GET", handshake_url, fail_cls=ResolverError, fail_msg=unreachable,
                   timeout=timeout, headers=headers)
    else:
        r1 = _handshake_with_profile_retry(handshake_url, headers=headers, timeout=timeout, fail_msg=unreachable)
    if r1.status_code == 403 and use_fs:
        _close_quietly(r1)
        r1, headers = _handshake_via_flaresolverr(handshake_url, stale_stamp=fs_stamp,
                                                  timeout=timeout, fail_msg=unreachable)
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
    ua = _current_ua()
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
