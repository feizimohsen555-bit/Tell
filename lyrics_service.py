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
# چند آدرس LRCLIB (آینه/میرور) با کاما؛ اگر اولی ۵۰۳/خطا داد، بعدی امتحان می‌شود. پیش‌فرض فقط LRCLIB_BASE_URL
_LRCLIB_BASES = [b.strip().rstrip("/") for b in (os.environ.get("LRCLIB_BASE_URLS") or "").split(",") if b.strip()] or [_LRCLIB_BASE]
_SCAN_ARTIST_FILTER = (os.environ.get("LYRICS_SCAN_ARTIST_FILTER", "1").strip().lower() not in ("0", "false", "off", "no"))
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

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        """(status, json|None). خطای شبکه/وقت تمام ← (0, None). یک بار هم روی 429/5xx دوباره تلاش می‌کند."""
        for attempt in (0, 1, 2):
            left = self.left()
            if left <= 0.5:
                return 0, None
            try:
                r = _session.get(url, params=params, headers=headers,
                                 timeout=(min(4.0, left), min(_TIMEOUT, left)))
            except requests.RequestException as e:
                logger.warning(f"[LYRICS] خطای شبکه {url[:60]}: {type(e).__name__}: {str(e)[:120]}")
                return 0, None
            st = r.status_code
            if st in (429, 502, 503, 504) and attempt < 2 and self.left() > 3:
                time.sleep(0.8 * (attempt + 1))          # ۰٫۸s ← ۱٫۶s؛ LRCLIB گاهی چند ثانیه ۵۰۳ می‌دهد
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
def _lrclib_search(ctx: _Ctx, q: str) -> Tuple[int, Any]:
    """جستجوی LRCLIB؛ اگر آدرس اول جواب نداد (۵xx/خطا) آدرس‌های بعدی (LRCLIB_BASE_URLS) امتحان می‌شود."""
    st, data = 0, None
    for base in _LRCLIB_BASES:
        st, data = ctx.get(f"{base}/api/search", {"q": _fa_light(q)})
        if st in (200, 404):
            break
    return st, data


def _p_lrclib(ctx: _Ctx, q: str) -> List[Dict[str, Any]]:
    st, data = _lrclib_search(ctx, q)
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


# ============================================================================
# شناسایی آهنگ از روی یک بیت — جستجوی متنی + تأیید با متن واقعی
# ----------------------------------------------------------------------------
# مشکل روش «عنوان صفحهٔ وب + حدس مدل»: اسم آهنگ از عنوان سایت‌ها می‌آید و هیچ‌جا چک نمی‌شود که آن آهنگ واقعاً آن بیت را دارد.
# اینجا دو چیز اضافه می‌شود:
#   ۱) identify_by_lyrics : جستجوی تمام‌متن در Genius (روی خودِ متن آهنگ‌ها ایندکس دارد؛ تکهٔ مطابق را هم برمی‌گرداند)
#   ۲) scan_artist        : متن آهنگ‌های یک خواننده را از LRCLIB می‌گیرد و بیت را داخلشان می‌گردد ← اسم دقیقِ آهنگ.
#                           برای «تأیید یا اصلاح» اسمی که وب/مدل زبانی پیشنهاد داده هم همین استفاده می‌شود.
# هر خطا ← None (ربات هرگز کرش نمی‌کند). Genius اگر ۳ بار پشت‌سرهم جواب نداد ۳۰ دقیقه کنار گذاشته می‌شود.
#   LYRICS_GENIUS=0    خاموش‌کردن Genius          GENIUS_BASE_URL  پیش‌فرض https://genius.com
#   LYRICS_ID_BUDGET_SEC سقف زمان یک شناسایی (پیش‌فرض 16)
# ============================================================================
_GENIUS_ON = (os.environ.get("LYRICS_GENIUS", "1").strip().lower() not in ("0", "false", "off", "no"))
_GENIUS_BASE = (os.environ.get("GENIUS_BASE_URL") or "https://genius.com").strip().rstrip("/")
_ID_BUDGET = _env_float("LYRICS_ID_BUDGET_SEC", 16.0)
_GENIUS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
_VERIFY_SCORE = 0.78          # حداقل شباهت بیت به متن برای «تأیید»
_VERIFY_SCORE_SHORT = 0.90    # بیت کوتاه (کمتر از ۴ کلمه) یا خوانندهٔ ناهمخوان ← سخت‌گیرتر
_genius_state = {"fails": 0, "until": 0.0}
_genius_lock = threading.Lock()
_SCAN_CACHE = _TTL(6 * 3600)


def phrase_score(phrase: str, lyrics: str) -> float:
    """بیشترین شباهت (۰..۱) بیت به یک پنجرهٔ هم‌اندازه از کلمه‌های متن؛ تحمل غلط املایی/یک‌دو کلمهٔ فرق."""
    pw, lw = _norm(phrase).split(), _norm(lyrics).split()
    if not pw or not lw:
        return 0.0
    p = " ".join(pw)
    n = len(pw)
    sm = difflib.SequenceMatcher(None)
    sm.set_seq2(p)
    best = 0.0
    for size in sorted({max(1, n - 1), n, n + 1}):
        for i in range(max(1, len(lw) - size + 1)):
            sm.set_seq1(" ".join(lw[i:i + size]))
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            best = max(best, sm.ratio())
            if best >= 0.98:
                return best
    return best


def _need_score(phrase: str, artist_ok: bool = True) -> float:
    return _VERIFY_SCORE if (len(_norm(phrase).split()) >= 4 and artist_ok) else _VERIFY_SCORE_SHORT


# ------------------------------------------------------------------ Genius (جستجوی تمام‌متن)
def _genius_candidates(ctx: _Ctx, phrase: str) -> List[Dict[str, Any]]:
    if not _GENIUS_ON or time.time() < _genius_state["until"]:
        return []
    st, data = ctx.get(f"{_GENIUS_BASE}/api/search/multi", {"per_page": 5, "q": _fa_light(phrase)[:200]},
                       headers={"User-Agent": _GENIUS_UA, "Accept": "application/json"})
    with _genius_lock:
        if st != 200 or not isinstance(data, dict):
            _genius_state["fails"] += 1
            if _genius_state["fails"] >= 3:
                _genius_state["until"] = time.time() + 1800
                _genius_state["fails"] = 0
                logger.warning("[LYRICS-ID] Genius ۳ بار پشت‌سرهم جواب نداد ← ۳۰ دقیقه کنار گذاشته شد")
            return []
        _genius_state["fails"] = 0
    out: List[Dict[str, Any]] = []
    seen = set()
    for sec in ((data.get("response") or {}).get("sections") or []):
        for hit in (sec.get("hits") or []):
            res = hit.get("result") or {}
            if (hit.get("type") or res.get("_type")) != "song":
                continue
            title = str(res.get("title") or "").strip()
            artist = str((res.get("primary_artist") or {}).get("name") or res.get("artist_names") or "").strip()
            if not title or not artist or (artist.lower().startswith("genius") and "translation" in artist.lower()):
                continue                                    # صفحه‌های «ترجمه» همان متن را تکرار می‌کنند
            snippet = " ".join(re.sub(r"<[^>]+>", " ", str(h.get("value") or ""))
                               for h in (hit.get("highlights") or [])
                               if isinstance(h, dict) and h.get("property") == "lyrics")
            if (artist, title) in seen:
                continue
            seen.add((artist, title))
            out.append({"title": title, "artist": artist, "snippet": _html.unescape(snippet),
                        "url": str(res.get("url") or ""), "source": "Genius"})
    return out


# ------------------------------------------------------------------ اسکن کاتالوگ یک خواننده در LRCLIB
def _artist_in(cand: str, names: Iterable[str]) -> bool:
    """آیا artistName نتیجه همان خواننده (یکی از املاهایش) است؟ (هم‌پوشانی کلمه‌ها یا شمول رشته‌ای)"""
    cn = _norm(cand)
    if not cn:
        return False
    ct = set(cn.split())
    for n in names:
        nn = _norm(n)
        if not nn:
            continue
        nt = set(nn.split())
        if nn in cn or cn in nn or (nt and len(nt & ct) / len(nt) >= 0.5):
            return True
    return False


def scan_artist(artist: str, phrase: str, titles: Iterable[str] = (),
                name_fn: Optional[Callable[[str], Iterable[str]]] = None,
                budget: Optional[float] = None, _ctx: Optional[_Ctx] = None) -> Dict[str, Any]:
    """
    متن آهنگ‌های «artist» را از LRCLIB می‌گیرد و بیت را داخلشان می‌گردد.
    خروجی: {"best": {"title","artist","score","source"} | None, "scanned": تعداد آهنگ‌های دارای متن که بررسی شد}
    name_fn(artist) → املاهای دیگر اسم خواننده (مثلاً فارسی ↔ لاتین)؛ titles: اسم‌های پیشنهادی برای تأیید مستقیم.
    """
    artist = re.sub(r"\s+", " ", artist or "").strip()
    phrase = re.sub(r"\s+", " ", phrase or "").strip()
    titles = [t.strip() for t in titles if isinstance(t, str) and t.strip()][:2]
    empty: Dict[str, Any] = {"best": None, "scanned": 0}
    if not artist or not phrase or "lrclib" not in _PROVIDER_ORDER:
        return empty
    ck = _norm(artist) + "|" + _norm(phrase) + "|" + "|".join(_norm(t) for t in titles)
    hit = _SCAN_CACHE.get(ck)
    if hit is not None:
        return dict(hit)
    ctx = _ctx or _Ctx(budget if budget is not None else _ID_BUDGET)

    names = [artist]
    if name_fn:
        try:
            names += [n.strip() for n in (name_fn(artist) or []) if isinstance(n, str) and n.strip()]
        except Exception as e:
            logger.warning(f"[LYRICS-ID] name_fn شکست خورد: {type(e).__name__}: {e}")
    queries: List[str] = []
    for t in titles:                                         # تأیید مستقیم: «خواننده + اسم پیشنهادی» اول
        queries.append(f"{artist} {t}")
    for n in names:
        queries.append(n)
    queries = list(dict.fromkeys(q for q in queries if q))[:5]

    best: Optional[Dict[str, Any]] = None
    scanned, seen = 0, set()
    for q in queries:
        if ctx.left() <= 1.0:
            break
        st, data = _lrclib_search(ctx, q)
        if st != 200 or not isinstance(data, list):
            continue
        for it in data[:25]:
            if not isinstance(it, dict) or it.get("instrumental"):
                continue
            lyrics = _clean_lyrics(it.get("plainLyrics")) or _clean_lyrics(it.get("syncedLyrics"))
            if _n_lines(lyrics) < 4:
                continue
            a, s = str(it.get("artistName") or "").strip(), str(it.get("trackName") or it.get("name") or "").strip()
            if (a, s) in seen:
                continue
            if _SCAN_ARTIST_FILTER and not _artist_in(a, names):
                continue                                     # آهنگ خواننده‌ای دیگر (یا اسم خواننده فقط در عنوان) ← نباید اسم را عوض کند
            seen.add((a, s))
            scanned += 1
            sc = phrase_score(phrase, lyrics)
            if best is None or sc > best["score"]:
                best = {"title": s, "artist": a, "score": sc, "source": "LRCLIB"}
    out = {"best": best, "scanned": scanned}
    if scanned:                                              # فقط نتیجهٔ واقعی کش می‌شود (نه خطای شبکه)
        _SCAN_CACHE.set(ck, out)
    logger.info(f"[LYRICS-ID] اسکن {artist[:30]!r}: {scanned} آهنگ بررسی شد | بهترین="
                f"{best and (best['title'], round(best['score'], 2))}")
    return dict(out)


# ------------------------------------------------------------------ متد عمومی
def identify_by_lyrics(phrase: str, artists: Iterable[str] = (), titles: Iterable[str] = (),
                       name_fn: Optional[Callable[[str], Iterable[str]]] = None,
                       budget: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    بیت ← آهنگ، فقط وقتی «تأیید با متن» شده باشد.
    artists : خواننده‌های محتمل (اختیاری؛ کاتالوگشان هم اسکن می‌شود)
    خروجی   : {"title","artist","score","source","snippet"} یا None
    """
    phrase = re.sub(r"\s+", " ", phrase or "").strip()
    if len(_norm(phrase).split()) < 2:
        return None
    ctx = _Ctx(budget if budget is not None else _ID_BUDGET)
    try:
        gen = _genius_candidates(ctx, phrase)
    except Exception as e:
        logger.warning(f"[LYRICS-ID] Genius خطا: {type(e).__name__}: {e}")
        gen = []
    best: Optional[Dict[str, Any]] = None
    for c in gen:
        c["score"] = phrase_score(phrase, c["snippet"]) if c["snippet"] else 0.0
        if c["score"] >= _need_score(phrase) and (best is None or c["score"] > best["score"]):
            best = c
    if best:
        logger.info(f"[LYRICS-ID] Genius: {best['artist']} - {best['title']} امتیاز={best['score']:.2f}")
        return dict(best)

    pool = [a for a in artists if isinstance(a, str) and a.strip()] + [c["artist"] for c in gen[:3]]
    for a in list(dict.fromkeys(pool))[:3]:
        if ctx.left() <= 2:
            break
        r = scan_artist(a, phrase, titles=titles, name_fn=name_fn, _ctx=ctx)
        b = r["best"]
        if b and b["score"] >= _need_score(phrase) and (best is None or b["score"] > best["score"]):
            best = dict(b, snippet="")
    if best:
        logger.info(f"[LYRICS-ID] LRCLIB: {best['artist']} - {best['title']} امتیاز={best['score']:.2f}")
    return best


# ----------------------------------------------------------------------------- آخرین کار یک خواننده (Deezer)
# LRCLIB/lyrics.ovh فقط متن آهنگ دارند و تاریخ انتشار ندارند؛ برای «آخرین آهنگ فلانی» از کاتالوگ Deezer
# (رایگان، بدون کلید) استفاده می‌شود. فقط وقتی اسم خواننده واقعاً با نتیجه بخواند جواب می‌دهد؛ وگرنه None.
_DEEZER_BASE = (os.environ.get("DEEZER_BASE_URL") or "https://api.deezer.com").strip().rstrip("/")


def _g2j(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    """میلادی ← شمسی."""
    g_dm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_dm[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm, jd = (1 + days // 31, 1 + days % 31) if days < 186 else (7 + (days - 186) // 30, 1 + (days - 186) % 30)
    return jy, jm, jd


def _fa_digits(s: str) -> str:
    return str(s).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _artist_match(wanted_norm: str, cand: str) -> float:
    cn = _norm(cand)
    if not wanted_norm or not cn:
        return 0.0
    if wanted_norm in cn or cn in wanted_norm:
        return 1.0
    ws, cs = set(wanted_norm.split()), set(cn.split())
    return max(len(ws & cs) / max(1, len(ws)), difflib.SequenceMatcher(None, wanted_norm, cn).ratio())


def latest_release(artist: str, variants_fn: Optional[Callable[[str], Iterable[str]]] = None,
                   budget: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """آخرین آلبوم/سینگل ثبت‌شدهٔ یک خواننده ← {"artist","title","date","type"} یا None (پیدا نشد).
    استثنا: LyricsUnavailable اگر Deezer اصلاً جواب نداد. تاریخ‌های آینده نادیده گرفته می‌شوند."""
    q = re.sub(r"\s+", " ", artist or "").strip()
    if not q:
        return None
    ctx = _Ctx(budget if budget is not None else 15.0)
    names = [q]
    tried_variants = False
    today = time.strftime("%Y-%m-%d")
    while names:
        name = names.pop(0)
        st, data = ctx.get(f"{_DEEZER_BASE}/search/artist", {"q": name, "limit": 5})
        cands = data.get("data") if st == 200 and isinstance(data, dict) else None
        best, best_s = None, 0.0
        for c in (cands or []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            sc = _artist_match(_norm(name), str(c.get("name") or ""))
            if sc > best_s:
                best, best_s = c, sc
        if best and best_s >= 0.6:
            albums: List[Dict[str, Any]] = []
            url, params = f"{_DEEZER_BASE}/artist/{best['id']}/albums", {"limit": 100}
            for _ in range(3):                              # حداکثر ۳ صفحه
                st, d = ctx.get(url, params)
                if st != 200 or not isinstance(d, dict):
                    break
                albums += [a for a in (d.get("data") or []) if isinstance(a, dict)]
                nxt = d.get("next")
                if not nxt:
                    break
                url, params = nxt, None
            dated = [a for a in albums if re.match(r"^\d{4}-\d{2}-\d{2}$", str(a.get("release_date") or ""))
                     and a["release_date"] <= today]
            if dated:
                top = max(dated, key=lambda a: a["release_date"])
                y, m, dd = (int(x) for x in top["release_date"].split("-"))
                jy, jm, jd = _g2j(y, m, dd)
                return {"artist": str(best.get("name") or name), "title": str(top.get("title") or "").strip(),
                        "date": _fa_digits(f"{jy}/{jm:02d}/{jd:02d}") + f" ({top['release_date']})",
                        "type": str(top.get("record_type") or "").lower()}
        if not names and not tried_variants and variants_fn is not None:
            tried_variants = True
            try:
                names = [v for v in (variants_fn(q) or []) if isinstance(v, str) and v.strip()][:_MAX_VARIANTS]
            except Exception as e:
                logger.warning(f"[LYRICS] latest variants خطا: {type(e).__name__}: {str(e)[:100]}")
    if not ctx.answered:
        raise LyricsUnavailable("deezer unreachable")
    return None
