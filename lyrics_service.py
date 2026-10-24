#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lyrics Service for Nokhod Bot — پیدا کردن متن آهنگ بدون وابستگی به lyricsify / کلودفلر

چرا؟  lyricsify.com پشت «چالش مدیریت‌شدهٔ کلودفلر» است (لاگ: server=cloudflare cf-mitigated=challenge).
چالش جاوااسکریپت اجرا می‌کند و هیچ کتابخانهٔ HTTP (requests / curl_cffi) از آن رد نمی‌شود؛ پروکسی و UA هم کمکی نمی‌کند.
این ماژول به‌جایش از APIهای JSON بدون چالش می‌خواند:
    ۱) LRCLIB      https://lrclib.net/api/search?q=...         (رایگان، بدون کلید؛ متن ساده + متن زمان‌دار)
    ۲) lyrics.ovh  https://api.lyrics.ovh/suggest|v1/...       (رایگان؛ فالبک)

کنار bot.py بگذار (مثل pinterest_link.py):

    from lyrics_service import find_lyrics, LyricsUnavailable
    try:
        res = find_lyrics("هایده ساقی", variants_fn=my_llm_fn)   # variants_fn اختیاری: str -> [str, ...]
    except LyricsUnavailable:
        ...                                                       # همهٔ منبع‌ها در دسترس نبودند (شبکه/بلاک)
    # res = {"song","artist","lyrics","source","score","more_url"}  یا None (پیدا نشد)

متغیرهای محیطی (همه اختیاری):
    LYRICS_PROVIDERS    ترتیب منبع‌ها، جداشده با کاما          پیش‌فرض: lrclib,ovh
    LYRICS_TIMEOUT_SEC  سقف انتظار هر درخواست                   پیش‌فرض: 8
    LYRICS_BUDGET_SEC   سقف زمان کل یک جستجو                    پیش‌فرض: 22
    LYRICS_PROXY        پروکسی (فقط اگر سرور به این سایت‌ها دسترسی ندارد؛ AI_PROXY عمداً خوانده نمی‌شود)
    LRCLIB_BASE_URL     پیش‌فرض https://lrclib.net
    LYRICS_OVH_BASE_URL پیش‌فرض https://api.lyrics.ovh
"""

import os
import re
import time
import html as _html
import difflib
import logging
import threading
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger("LyricsService")


# ----------------------------------------------------------------------------- تنظیمات
def _env_float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


LYRICS_PROXY = (os.environ.get("LYRICS_PROXY") or "").strip()
_TIMEOUT = _env_float("LYRICS_TIMEOUT_SEC", 8.0)
_BUDGET = _env_float("LYRICS_BUDGET_SEC", 22.0)
_LRCLIB_BASE = (os.environ.get("LRCLIB_BASE_URL") or "https://lrclib.net").strip().rstrip("/")
_OVH_BASE = (os.environ.get("LYRICS_OVH_BASE_URL") or "https://api.lyrics.ovh").strip().rstrip("/")
_PROVIDER_ORDER = [p.strip().lower() for p in (os.environ.get("LYRICS_PROVIDERS") or "lrclib,ovh").split(",") if p.strip()]

# LRCLIB از کلاینت‌ها می‌خواهد خودشان را با User-Agent معرفی کنند
_UA = "NokhodBot/1.0 (Telegram bot; lyrics lookup)"

_ACCEPT_SCORE = 0.25      # کمتر از این ← «پیدا نشد» (نتیجهٔ بی‌ربط نشان نمی‌دهیم)
_GOOD_SCORE = 0.55        # بیشتر از این ← بدون امتحان‌کردن عبارت‌های جایگزین قبول می‌شود
_MAX_VARIANTS = 3


class LyricsUnavailable(Exception):
    """هیچ‌کدام از منبع‌ها جواب ندادند (خطای شبکه / بلاک / سرور خراب) — با «پیدا نشد» فرق دارد."""


# ----------------------------------------------------------------------------- کش TTL کوچک
class _TTL:
    def __init__(self, ttl: float, maxsize: int = 300):
        self.ttl, self.maxsize = ttl, maxsize
        self._d: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            hit = self._d.get(key)
            if not hit:
                return None
            if hit[0] < time.time():
                self._d.pop(key, None)
                return None
            return hit[1]

    def set(self, key: str, value: Any):
        with self._lock:
            if len(self._d) >= self.maxsize:
                now = time.time()
                for k in [k for k, (exp, _) in self._d.items() if exp < now] or [next(iter(self._d))]:
                    self._d.pop(k, None)
            self._d[key] = (time.time() + self.ttl, value)


_CACHE = _TTL(24 * 3600)


# ----------------------------------------------------------------------------- نرمال‌سازی و تمیزکاری
_DIAC_RE = re.compile("[ً-ْٰـ]")


def _fa_light(s: str) -> str:
    """نرمال‌سازی سبک برای ارسال به API: ی/ک عربی ← فارسی، اعراب/کشیده حذف، فاصله‌ها یکی."""
    s = _DIAC_RE.sub("", s or "").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", s.replace("‌", " ")).strip()


def _norm(s: str) -> str:
    """نرمال‌سازی برای مقایسه (بدون علامت‌ها، حروف کوچک)."""
    t = _fa_light(s).lower().replace("ۀ", "ه").replace("أ", "ا")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_TS_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_META_RE = re.compile(r"^\[(?:ar|ti|al|au|by|length|offset|re|ve|id|la|lang):.*\]$", re.I)
_OVH_HEAD_RE = re.compile(r"^(?:paroles de la chanson|lyrics for|lyrics of)\b.*$", re.I)


def _clean_lyrics(text: Any) -> str:
    """حذف زمان‌بندی LRC ([01:23.45])، تگ‌های متادیتا و خط‌های خالی اضافه."""
    if not isinstance(text, str):
        return ""
    lines: List[str] = []
    for ln in _html.unescape(text).replace("\r", "").split("\n"):
        ln = _TS_RE.sub("", ln).strip()
        if _META_RE.match(ln):
            continue
        lines.append(re.sub(r"[ \t]+", " ", ln))
    while lines and _OVH_HEAD_RE.match(lines[0]):       # سرخط تبلیغاتی lyrics.ovh
        lines.pop(0)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _n_lines(text: str) -> int:
    return sum(1 for x in (text or "").split("\n") if x.strip())


# ----------------------------------------------------------------------------- شباهت نتیجه به درخواست
def _similarity(query_norm: str, artist: str, song: str) -> float:
    cand = _norm(f"{artist} {song}")
    if not query_norm or not cand:
        return 0.0
    qs, cs = set(query_norm.split()), set(cand.split())
    overlap = len(qs & cs) / max(1, len(qs))
    partial = sum(1 for t in qs if len(t) >= 2 and t in cand) / max(1, len(qs))
    seq = difflib.SequenceMatcher(None, query_norm, cand).ratio()
    return max(overlap, partial * 0.9, seq)


# ----------------------------------------------------------------------------- HTTP
_session = requests.Session()
_session.headers.update({"User-Agent": _UA, "Accept": "application/json"})
if LYRICS_PROXY:
    _session.proxies.update({"http": LYRICS_PROXY, "https": LYRICS_PROXY})


class _Ctx:
    """وضعیت یک جستجو: ددلاین کل + اینکه آیا حداقل یک منبع «جواب داد» (۲۰۰ یا ۴۰۴)."""

    def __init__(self, budget: float):
        self.deadline = time.time() + budget
        self.answered = False

    def left(self) -> float:
        return self.deadline - time.time()

    def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        """(status, json|None). خطای شبکه/وقت تمام ← (0, None). یک بار هم روی 429/5xx دوباره تلاش می‌کند."""
        for attempt in (0, 1):
            left = self.left()
            if left <= 0.5:
                return 0, None
            try:
                r = _session.get(url, params=params, timeout=(min(4.0, left), min(_TIMEOUT, left)))
            except requests.RequestException as e:
                logger.warning(f"[LYRICS] خطای شبکه {url[:60]}: {type(e).__name__}: {str(e)[:120]}")
                return 0, None
            st = r.status_code
            if st in (429, 502, 503, 504) and attempt == 0:
                time.sleep(0.6)
                continue
            if st in (200, 404):
                self.answered = True
            elif st in (403, 401):
                logger.warning(f"[LYRICS] وضعیت {st} برای {url[:60]} (بلاک؟) server={r.headers.get('server')}")
            else:
                logger.warning(f"[LYRICS] وضعیت {st} برای {url[:60]}")
            try:
                return st, r.json()
            except ValueError:
                return st, None
        return 0, None


# ----------------------------------------------------------------------------- منبع ۱: LRCLIB
def _p_lrclib(ctx: _Ctx, q: str) -> List[Dict[str, Any]]:
    st, data = ctx.get(f"{_LRCLIB_BASE}/api/search", {"q": _fa_light(q)})
    if st != 200 or not isinstance(data, list):
        return []
    out = []
    for it in data[:15]:
        if not isinstance(it, dict) or it.get("instrumental"):
            continue
        lyrics = _clean_lyrics(it.get("plainLyrics")) or _clean_lyrics(it.get("syncedLyrics"))
        if _n_lines(lyrics) < 2:
            continue
        out.append({"song": str(it.get("trackName") or it.get("name") or "").strip(),
                    "artist": str(it.get("artistName") or "").strip(),
                    "lyrics": lyrics, "source": "LRCLIB"})
    return out


# ----------------------------------------------------------------------------- منبع ۲: lyrics.ovh
def _p_ovh(ctx: _Ctx, q: str) -> List[Dict[str, Any]]:
    st, data = ctx.get(f"{_OVH_BASE}/suggest/{urllib.parse.quote(_fa_light(q), safe='')}")
    items = data.get("data") if st == 200 and isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    qn = _norm(q)
    picks = []
    for it in items[:10]:
        try:
            title, artist = str(it["title"]).strip(), str(it["artist"]["name"]).strip()
        except (KeyError, TypeError):
            continue
        picks.append((_similarity(qn, artist, title), artist, title))
    picks.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, artist, title in picks:
        if len(out) >= 2 or len(seen) >= 3 or ctx.left() < 2:
            break
        if (artist, title) in seen:
            continue
        seen.add((artist, title))
        st, d = ctx.get(f"{_OVH_BASE}/v1/{urllib.parse.quote(artist, safe='')}/{urllib.parse.quote(title, safe='')}")
        lyrics = _clean_lyrics(d.get("lyrics")) if st == 200 and isinstance(d, dict) else ""
        if _n_lines(lyrics) >= 2:
            out.append({"song": title, "artist": artist, "lyrics": lyrics, "source": "lyrics.ovh"})
    return out


_PROVIDERS: Dict[str, Callable[[_Ctx, str], List[Dict[str, Any]]]] = {"lrclib": _p_lrclib, "ovh": _p_ovh}


# ----------------------------------------------------------------------------- متد عمومی
def _more_url(artist: str, song: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(f"{artist} {song} lyrics".strip())


def _search_round(ctx: _Ctx, queries: List[str], all_norms: List[str]) -> Optional[Dict[str, Any]]:
    """همهٔ منبع‌ها × عبارت‌ها؛ بهترین کاندید (بر اساس شباهت به هر عبارت) یا None. به محض نتیجهٔ خوب برمی‌گردد."""
    best: Optional[Dict[str, Any]] = None
    for name in _PROVIDER_ORDER:
        fn = _PROVIDERS.get(name)
        if not fn:
            continue
        for q in queries:
            if ctx.left() <= 0.5:
                return best
            try:
                cands = fn(ctx, q)
            except Exception as e:                      # هیچ خطایی نباید ربات را بیندازد
                logger.warning(f"[LYRICS] خطای غیرمنتظره در {name}: {type(e).__name__}: {e}")
                continue
            for c in cands:
                c["score"] = max(_similarity(n, c["artist"], c["song"]) for n in all_norms)
                key = (round(c["score"], 2), _n_lines(c["lyrics"]))
                if best is None or key > (round(best["score"], 2), _n_lines(best["lyrics"])):
                    best = c
            if best and best["score"] >= _GOOD_SCORE:
                return best
    return best


def find_lyrics(query: str, variants: Optional[Iterable[str]] = None,
                variants_fn: Optional[Callable[[str], Iterable[str]]] = None,
                budget: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    query        : عبارت کاربر («هایده ساقی»، «Coldplay Yellow»، …)
    variants     : عبارت‌های جایگزین آمادهٔ اضافه (مثلاً نوشتار لاتین خواننده/آهنگ)
    variants_fn  : اگر با عبارت اصلی نتیجهٔ خوب نیامد، با query صدا زده می‌شود و عبارت‌های جایگزین برمی‌گرداند
                   (مثلاً یک مدل زبانی که «هایده ساقی» را به «Hayedeh Saghi» برمی‌گرداند)
    خروجی        : {"song","artist","lyrics","source","score","more_url"} یا None (پیدا نشد)
    استثنا       : LyricsUnavailable اگر هیچ منبعی جواب نداد.
    """
    q = re.sub(r"\s+", " ", query or "").strip()
    if not q:
        return None
    ck = _norm(q)
    cached = _CACHE.get(ck)
    if cached:
        return dict(cached)

    ctx = _Ctx(budget if budget is not None else _BUDGET)
    extra = [v for v in (variants or []) if isinstance(v, str) and v.strip()]
    queries = [q] + [v.strip() for v in extra if _norm(v) != ck]
    norms = [_norm(x) for x in queries]
    best = _search_round(ctx, queries[:1 + _MAX_VARIANTS], norms)

    # اگر هیچ منبعی جواب نداده (بلاک/شبکه)، صدا زدن مدل زبانی بی‌فایده است
    if ctx.answered and (not best or best["score"] < _GOOD_SCORE) and variants_fn and ctx.left() > 3:
        try:
            more = [v.strip() for v in (variants_fn(q) or []) if isinstance(v, str) and v.strip()]
        except Exception as e:
            logger.warning(f"[LYRICS] variants_fn شکست خورد: {type(e).__name__}: {e}")
            more = []
        more = [v for v in dict.fromkeys(more) if _norm(v) not in norms][:_MAX_VARIANTS]
        if more:
            logger.info(f"[LYRICS] عبارت‌های جایگزین: {more}")
            norms += [_norm(v) for v in more]
            second = _search_round(ctx, more, norms)
            if second and (not best or second["score"] > best["score"]):
                best = second

    if not ctx.answered:
        raise LyricsUnavailable("هیچ منبع متن آهنگی جواب نداد")
    if not best or best["score"] < _ACCEPT_SCORE:
        logger.info(f"[LYRICS] برای {q[:40]!r} نتیجهٔ مناسب نبود (best={best and round(best['score'], 2)})")
        return None

    best["more_url"] = _more_url(best["artist"], best["song"])
    best["song"] = best["song"] or q
    logger.info(f"[LYRICS] {q[:40]!r} ← {best['artist']} - {best['song']} | منبع={best['source']} "
                f"امتیاز={best['score']:.2f} خط‌ها={_n_lines(best['lyrics'])}")
    _CACHE.set(ck, best)
    return dict(best)
