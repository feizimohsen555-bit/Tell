# -*- coding: utf-8 -*-
"""
custom_resolver.py — generic JSON-webhook resolver client.

Flow:
    text  ->  extract_target_url()  ->  call_resolver()  ->  stream_file()
                                         (POST /api/fetch)    (download into BytesIO)

Environment variables (all optional):
    RESOLVER_ENDPOINT              Base URL of the resolver.      default: https://example.internal
    RESOLVER_USER_AGENT            User-Agent header sent to the resolver.
    RESOLVER_CONNECT_TIMEOUT       Connect timeout, seconds.      default: 10
    RESOLVER_READ_TIMEOUT          Read timeout of the API call.  default: 30
    RESOLVER_DOWNLOAD_TIMEOUT      Read timeout per chunk while downloading.   default: 30
    RESOLVER_DOWNLOAD_DEADLINE     Hard wall-clock cap for a whole download.   default: 120
    RESOLVER_MAX_BYTES             Max download size in bytes.    default: 45 MiB (Telegram bot upload limit is 50 MB)
    RESOLVER_ALLOW_PRIVATE_DOWNLOADS=1   Allow download URLs that resolve to private/loopback addresses.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("custom_resolver")

# --------------------------------------------------------------------------- config

RESOLVER_ENDPOINT = os.getenv("RESOLVER_ENDPOINT", "https://example.internal")
_PLACEHOLDER_ENDPOINT = "https://example.internal"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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


USER_AGENT = os.getenv("RESOLVER_USER_AGENT", DEFAULT_USER_AGENT)
CONNECT_TIMEOUT = _env_float("RESOLVER_CONNECT_TIMEOUT", 10.0)
READ_TIMEOUT = _env_float("RESOLVER_READ_TIMEOUT", 30.0)
DOWNLOAD_TIMEOUT = _env_float("RESOLVER_DOWNLOAD_TIMEOUT", 30.0)
DOWNLOAD_DEADLINE = _env_float("RESOLVER_DOWNLOAD_DEADLINE", 120.0)
MAX_DOWNLOAD_BYTES = _env_int("RESOLVER_MAX_BYTES", 45 * 1024 * 1024)
ALLOW_PRIVATE_DOWNLOADS = (os.getenv("RESOLVER_ALLOW_PRIVATE_DOWNLOADS", "0").strip() == "1")

_MAX_REDIRECTS = 5
_CHUNK = 64 * 1024

_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)]}>،؛؟…"

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


@dataclass(frozen=True)
class ResolverResult:
    title: str
    artist: str
    download_url: str


# --------------------------------------------------------------------------- http session

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16))
_session.mount("http://", HTTPAdapter(pool_connections=2, pool_maxsize=8))


def is_configured() -> bool:
    """True once RESOLVER_ENDPOINT points at something other than the placeholder."""
    ep = (RESOLVER_ENDPOINT or "").strip().rstrip("/")
    return bool(ep) and ep != _PLACEHOLDER_ENDPOINT


def _endpoint_origin() -> str:
    p = urlparse(RESOLVER_ENDPOINT)
    if not p.scheme or not p.netloc:
        return RESOLVER_ENDPOINT.rstrip("/")
    return f"{p.scheme}://{p.netloc}"


def _build_headers() -> dict:
    origin = _endpoint_origin()
    return {
        "User-Agent": USER_AGENT,
        "Origin": origin,
        "Referer": origin + "/",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


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


# --------------------------------------------------------------------------- 2) resolve


def call_resolver(target_url: str) -> ResolverResult:
    """POST {"url": target_url} to {RESOLVER_ENDPOINT}/api/fetch and parse title/artist/download_url."""
    if not is_configured():
        raise ResolverNotConfigured("RESOLVER_ENDPOINT is not set")
    if not target_url:
        raise ResolverBadResponse("empty target url", "No link to resolve.")

    api = f"{RESOLVER_ENDPOINT.rstrip('/')}/api/fetch"
    try:
        resp = _session.post(
            api,
            json={"url": target_url},
            headers=_build_headers(),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=False,
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        if isinstance(e, requests.Timeout) or "timed out" in str(e).lower():
            raise ResolverTimeout(f"timeout calling {api}") from e
        raise ResolverError(f"connection error: {e}", "Could not reach the resolver.") from e
    except requests.RequestException as e:
        raise ResolverError(f"request failed: {e}", "Could not reach the resolver.") from e

    if resp.status_code == 429:
        raise ResolverError("rate limited (429)", "The resolver is busy right now. Try again shortly.")
    if resp.status_code >= 400:
        raise ResolverError(
            f"HTTP {resp.status_code} from resolver",
            "The resolver could not process this link.",
        )
    if 300 <= resp.status_code < 400:
        raise ResolverBadResponse(f"unexpected redirect ({resp.status_code}) from resolver")

    try:
        data = resp.json()
    except ValueError as e:
        raise ResolverBadResponse("response is not valid JSON") from e

    if not isinstance(data, dict):
        raise ResolverBadResponse("JSON root is not an object")

    download_url = str(data.get("download_url") or "").strip()
    if not download_url:
        raise ResolverBadResponse("missing download_url", "The resolver found nothing to download for this link.")
    if urlparse(download_url).scheme.lower() not in ("http", "https"):
        raise ResolverBadResponse("download_url is not http(s)")

    title = str(data.get("title") or "").strip() or "Unknown title"
    artist = str(data.get("artist") or "").strip() or "Unknown artist"
    return ResolverResult(title=title, artist=artist, download_url=download_url)


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


def stream_file(download_url: str) -> io.BytesIO:
    """
    Download `download_url` into an in-memory BytesIO (rewound to 0).
    The returned buffer has a `.name` attribute ("audio.<ext>") so Telegram sees a proper filename.
    Enforces: http(s) only, public hosts only (unless allowed), manual redirect handling with
    re-validation, a size cap, and a wall-clock deadline.
    """
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    url = download_url
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": _endpoint_origin() + "/",
        "Accept": "*/*",
    }

    resp = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            _check_download_url(url)
            try:
                resp = _session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT),
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                if isinstance(e, requests.Timeout) or "timed out" in str(e).lower():
                    raise ResolverTimeout("timeout starting download") from e
                raise DownloadError(f"connection error: {e}") from e
            except requests.RequestException as e:
                raise DownloadError(f"request failed: {e}") from e

            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                resp.close()
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
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
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
        except (requests.Timeout, requests.ConnectionError) as e:
            raise ResolverTimeout("timeout while downloading") from e
        except requests.RequestException as e:
            raise DownloadError(f"stream error: {e}") from e

        if total == 0:
            raise DownloadError("downloaded file is empty")

        buf.seek(0)
        buf.name = f"audio.{_guess_ext(resp.headers.get('Content-Type', ''), url)}"
        return buf
    finally:
        if resp is not None:
            resp.close()
