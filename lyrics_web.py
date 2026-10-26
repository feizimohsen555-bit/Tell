#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lyrics_web — شناسایی آهنگ و گرفتن متن آن از «خودِ صفحه‌های متن ترانه» (روش مبتنی بر شواهد)

چرا این ماژول؟
    LRCLIB / lyrics.ovh برای آهنگ‌های فارسی تقریباً خالی‌اند و Genius از سرور شما ۴۰۳ (کلودفلر) می‌دهد؛ پس «تأیید با متن»
    هیچ‌وقت انجام نمی‌شد و اسم آهنگ دور ریخته می‌شد. موتور جستجوی خودِ ربات (DDG/Yahoo/Startpage) ولی کار می‌کند.
    مشکل دوم: عنوان صفحه‌های فارسی معمولاً خودِ مصرع اول است («تو چشم اشکی نمونده که واست دریا بسازم - …») و اسم واقعی آهنگ
    فقط داخل صفحه است؛ اسنیپت جستجو آن را نشان نمی‌دهد.

روش (همان کاری که یک آدم می‌کند):
    ۱) جستجوی وب با چند شکل از بیت  ← فهرست صفحه‌های کاندید
    ۲) دانلود موازی صفحه‌ها (بدون کلودفلر/لاگین؛ هر صفحهٔ بلاک‌شده کنار گذاشته می‌شود)
    ۳) تأیید: بیت واقعاً در «متن» صفحه هست؟ (شباهت پنجره‌ای، تحمل غلط املایی) — نه فقط در عنوان
    ۴) استخراج اسم آهنگ/خواننده از ساختارِ صفحه (JSON-LD، og:title، h1، breadcrumb، الگوهای «متن آهنگ X از Y»)
    ۵) رأی‌گیری بین دامنه‌های مستقل؛ اسمی که فقط در یک سایت دیده شده «مطمئن نیستم» می‌خورد
    ۶) متن کامل همان صفحه (بلوک مصرع‌ها) برای «متنشو بده» نگه داشته می‌شود؛ پس بدون LRCLIB هم جواب می‌دهد

وابستگی بیرونی: requests (و bs4 اگر نصب بود؛ وگرنه regex). هیچ‌چیزِ ربات import نمی‌شود:
    search_fn(query, max_results) -> [{"title","href","body"}]   ← _search_web ربات
    llm_fn(prompt) -> str | None                                  ← (اختیاری) _song_gemini_ask
"""

import re
import time
import json
import html as _html
import difflib
import logging
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

try:                                    # اختیاری
    from bs4 import BeautifulSoup
except Exception:                       # pragma: no cover
    BeautifulSoup = None

logger = logging.getLogger("LyricsWeb")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0.0.0 Safari/537.36")
_MAX_BYTES = 700_000
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="lyrics-web")
_session = requests.Session()
_session.headers.update({"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml",
                         "Accept-Language": "fa,en;q=0.8"})


# ----------------------------------------------------------------------------- کش کوچک
class _TTL:
    def __init__(self, ttl: float, maxsize: int = 200):
        self.ttl, self.maxsize, self._d, self._l = ttl, maxsize, {}, threading.Lock()

    def get(self, k):
        with self._l:
            h = self._d.get(k)
            if not h or h[0] < time.time():
                self._d.pop(k, None)
                return None
            return h[1]

    def set(self, k, v):
        with self._l:
            if len(self._d) >= self.maxsize:
                now = time.time()
                for kk in [x for x, (e, _) in self._d.items() if e < now] or [next(iter(self._d))]:
                    self._d.pop(kk, None)
            self._d[k] = (time.time() + self.ttl, v)


_PAGE_CACHE = _TTL(3 * 3600, 150)         # url -> صفحهٔ پارس‌شده
_ID_CACHE = _TTL(6 * 3600, 200)           # norm(phrase) -> نتیجهٔ شناسایی
_LYR_CACHE = _TTL(12 * 3600, 200)         # norm(artist title) -> نتیجهٔ متن
# «پیدا نشد» فقط چند دقیقه کش می‌شود؛ قبلاً ۶ ساعت کش می‌شد و یک شکست موقت (ریت‌لیمیت موتور جستجو، صفحهٔ کند) تا ۶ ساعت
# جواب «پیدا نشد» می‌داد، حتی اگر کاربر دوباره می‌پرسید.
_NEG_CACHE = _TTL(120, 200)               # norm(phrase) -> {} (بیت در هیچ صفحه‌ای تأیید نشد)
_NEG_PAGE = _TTL(300, 300)                # url|phrase -> صفحه‌ای که دانلود/پارس نشد


# ----------------------------------------------------------------------------- نرمال‌سازی
_DIAC = re.compile("[\u064b-\u065f\u0670\u0640]")


def norm(s: str) -> str:
    t = _DIAC.sub("", (s or "").lower())
    t = (t.replace("\u200c", " ").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
          .replace("ۀ", "ه").replace("أ", "ا").replace("ؤ", "و").replace("إ", "ا"))
    t = t.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _domain(url: str) -> str:
    """دامنهٔ ثبت‌شده (text-music.ir از fa.m.text-music.ir) برای شمردنِ «منبع‌های مستقل»."""
    try:
        d = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""
    parts = [x for x in d.split(".") if x]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def phrase_score(phrase: str, text: str) -> float:
    """بیشترین شباهت (۰..۱) بیت به یک پنجرهٔ هم‌اندازه از کلمه‌های متن؛ تحمل غلط املایی و ±۱ کلمه."""
    pw, tw = norm(phrase).split(), norm(text).split()
    if not pw or not tw:
        return 0.0
    p, n = " ".join(pw), len(pw)
    sm = difflib.SequenceMatcher(None)
    sm.set_seq2(p)
    best = 0.0
    for size in sorted({max(1, n - 1), n, n + 1}):
        for i in range(max(1, len(tw) - size + 1)):
            sm.set_seq1(" ".join(tw[i:i + size]))
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            best = max(best, sm.ratio())
            if best >= 0.98:
                return best
    return best


# ----------------------------------------------------------------------------- دانلود صفحه
_CF_MARKERS = ("just a moment", "cf-browser-verification", "attention required", "enable javascript and cookies",
               "checking your browser", "cf-challenge")
_BAD_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com",
              "pinterest.com", "wikipedia.org", "linkedin.com", "t.me", "telegram.me", "aparat.com", "reddit.com",
              "amazon.", "ebay.", "digikala.com", "divar.ir", "google.", "bing.com", "yahoo.com", "duckduckgo.com")


def _is_bad_host(url: str) -> bool:
    d = _domain(url)
    if not d:
        return True
    labels = d.split(".")
    for b in _BAD_HOSTS:
        if b.endswith("."):                                   # «google.» ← هر TLD
            if b[:-1] in labels:
                return True
        elif d == b or d.endswith("." + b):
            return True
    return False


def _decode(r: requests.Response) -> str:
    raw = r.content[:_MAX_BYTES]
    for enc in (r.encoding if r.encoding and r.encoding.lower() not in ("iso-8859-1", "latin-1") else None,
                "utf-8", "windows-1256"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _fetch(url: str, timeout: float = 6.0) -> Optional[str]:
    """HTML صفحه یا None (خطا / بلاک / چالش کلودفلر / غیر HTML)."""
    try:
        r = _session.get(url, timeout=(3.5, timeout), allow_redirects=True, stream=False)
    except requests.RequestException as e:
        logger.info(f"[LWEB] fetch خطا {url[:70]}: {type(e).__name__}")
        return None
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code != 200 or ("html" not in ct and "text" not in ct and ct):
        logger.info(f"[LWEB] fetch {r.status_code} {url[:70]} server={r.headers.get('server')}")
        return None
    txt = _decode(r)
    low = txt[:4000].lower()
    if r.headers.get("cf-mitigated") or any(m in low for m in _CF_MARKERS):
        logger.info(f"[LWEB] چالش کلودفلر روی {url[:70]} ← رد شد")
        return None
    return txt


# ----------------------------------------------------------------------------- پارس HTML
_DROP_TAGS = re.compile(r"<(script|style|noscript|svg|nav|footer|header|aside|form|iframe|button|select)\b[^>]*>.*?</\1\s*>",
                        re.I | re.S)
_BR = re.compile(r"<\s*br\s*/?>|</\s*(?:p|div|li|h[1-6]|tr|blockquote|section|article)\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _meta(src: str, key: str) -> str:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\']',
                rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']'):
        m = re.search(pat, src, re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
    return ""


def _jsonld(src: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', src, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            it = stack.pop()
            if isinstance(it, dict):
                out.append(it)
                for v in it.values():
                    if isinstance(v, (dict, list)):
                        stack.extend(v if isinstance(v, list) else [v])
            elif isinstance(it, list):
                stack.extend(it)
    return out


def _h1(src: str) -> str:
    m = re.search(r"<h1\b[^>]*>(.*?)</h1\s*>", src, re.I | re.S)
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", m.group(1)))).strip() if m else ""


_SENT = "\x00"                                    # خط مرزی: جای عنصرهای حذف‌شده (ناوبری، تیتر، فوتر) ← بلوک ترانه از آن رد نمی‌شود
_HEAD = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.I | re.S)
_HEADINGS = re.compile(r"<(h[1-6])\b[^>]*>.*?</\1\s*>", re.I | re.S)


def _to_lines(src: str, keep_nl: bool = False) -> List[str]:
    """HTML ← خط‌ها. پیش‌فرض: فقط تگ‌ها (br/p/div/…) خط می‌شکنند (مثل مرورگر)؛ keep_nl=True: «\\n» داخل منبع هم خط‌شکن است
    (برای سایت‌هایی که متن را با white-space:pre-line می‌گذارند)."""
    s = _HEAD.sub(" ", src)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<pre\b[^>]*>(.*?)</pre\s*>", lambda m: m.group(1).replace("\n", "<br>"), s, flags=re.S | re.I)
    if not keep_nl:
        s = re.sub(r"[\r\n\t]+", " ", s)
    s = _DROP_TAGS.sub("\n" + _SENT + "\n", s)
    s = _HEADINGS.sub("\n" + _SENT + "\n", s)
    s = _BR.sub("\n", s)
    s = _html.unescape(_TAG.sub(" ", s))
    lines = []
    for ln in s.replace("\r", "").replace("\xa0", " ").split("\n"):
        ln = re.sub(r"[ \t\u200b\u200f\u200e]+", " ", ln).strip()
        lines.append(ln)
    return lines


# بخش‌هایی که وسط/بعد از متن می‌آیند و جزو ترانه نیستند
_STOP_LINE = re.compile(
    r"^(?:دانلود|پخش|لینک|کپی|اشتراک|نظر|دیدگاه|ارسال|ثبت|ورود|عضویت|جستجو|برچسب|تگ|منبع|امتیاز|بازدید|مشاهده|"
    r"آهنگ(?:‌|\s)?های(?:\s+(?:مرتبط|پیشنهادی|جدید|دیگر))|متن\s+(?:آهنگ|ترانه)(?:\s+های)?\s+(?:مرتبط|دیگر|جدید)|"
    r"مطالب\s+مرتبط|پست\s+های|share|download|copyright|©|tags?:|related|leave a|comments?)\b", re.I)
_NAV_JUNK = re.compile(r"(?:خانه|صفحه اصلی|تماس با ما|درباره ما|قوانین|حریم خصوصی|منو|فهرست)\s*$")


def lyric_block(lines: List[str], phrase: str, min_lines: int = 6) -> Tuple[str, float]:
    """بلوک پیوستهٔ مصرع‌ها که بیت را در بر دارد ← (متن, امتیاز تطبیق بیت)؛ پیدا نشد ← ("", بهترین امتیاز).
    مصرع = خط کوتاه (≤ ۹۰ نویسه)؛ خط خالی داخل بلوک مجاز است (بین بندها)."""
    if not lines:
        return "", 0.0
    pw = len(norm(phrase).split())
    # امتیاز هر خط (و ترکیب دو خط پشت‌سرهم برای بیت‌هایی که دو مصرع‌اند)
    best_i, best_s = -1, 0.0
    for i, ln in enumerate(lines):
        if not ln or ln == _SENT or len(ln) > 400:
            continue
        for span in (1, 2, 3):
            seg = " ".join(x for x in lines[i:i + span] if x and x != _SENT)
            if len(norm(seg).split()) < max(2, pw - 2):
                continue
            s = phrase_score(phrase, seg)
            if s > best_s:
                best_i, best_s = i, s
            if s >= 0.98:
                break
        if best_s >= 0.98:
            break
    if best_i < 0:
        return "", 0.0

    def _is_lyric_line(x: str) -> bool:
        if x == _SENT:
            return False                                       # مرز: ناوبری/تیتر/فوتر
        if not x:
            return True                                        # خط خالی بین بندها
        return (len(x) <= 90 and not _STOP_LINE.search(x) and not _NAV_JUNK.search(x)
                and not re.search(r"https?://|www\.|@\w+\.\w+", x))

    a = best_i
    blanks = 0
    while a > 0:
        prev = lines[a - 1]
        if prev == "":
            blanks += 1
            if blanks > 2:
                break
        elif _is_lyric_line(prev):
            blanks = 0
        else:
            break
        a -= 1
    b, blanks = best_i, 0
    while b + 1 < len(lines):
        nxt = lines[b + 1]
        if nxt == "":
            blanks += 1
            if blanks > 2:
                break
        elif _is_lyric_line(nxt):
            blanks = 0
        else:
            break
        b += 1
    blk = [x for x in lines[a:b + 1]]
    while blk and not blk[0]:
        blk.pop(0)
    while blk and not blk[-1]:
        blk.pop()
    # حذف خط‌های ابتدای بلوک که عنوان/ناوبری‌اند (خط کوتاهِ بدون تکرار قافیه معمولاً عنوان است؛ فقط اگر بعدش خط خالی آمد)
    n_real = sum(1 for x in blk if x)
    if n_real < min_lines:
        return "", best_s
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(blk)).strip()
    return text, best_s


def auto_block(lines: List[str], min_lines: int = 6) -> str:
    """بدون بیتِ لنگر: «بیشترین دنبالهٔ مصرع‌مانند» صفحه (خط‌های ۸..۹۰ نویسه با میانگین ≥۳ کلمه؛ بند‌ها با ≤۲ خط خالی جدا)."""
    runs: List[List[str]] = []
    cur: List[str] = []
    blanks = 0
    for ln in list(lines) + [_SENT]:
        if ln == "":
            if cur:
                blanks += 1
                if blanks > 2:
                    runs.append(cur)
                    cur, blanks = [], 0
                else:
                    cur.append("")
            continue
        ok = (ln != _SENT and 8 <= len(ln) <= 90 and not _STOP_LINE.search(ln) and not _NAV_JUNK.search(ln)
              and not re.search(r"https?://|www\.|@\w+\.\w+", ln))
        if ok:
            cur.append(ln)
            blanks = 0
        elif cur:
            runs.append(cur)
            cur, blanks = [], 0
    best, best_n = "", 0
    for r in runs:
        real = [x for x in r if x]
        if len(real) < min_lines or sum(len(norm(x).split()) for x in real) / len(real) < 3:
            continue
        if len(real) > best_n:
            best_n, best = len(real), re.sub(r"\n{3,}", "\n\n", "\n".join(r)).strip()
    return best


# ----------------------------------------------------------------------------- استخراج اسم آهنگ / خواننده
_SITE_WORDS = {"متن", "آهنگ", "اهنگ", "ترانه", "دانلود", "موزیک", "ویدیو", "کلیپ", "جدید", "کامل", "با", "صدای", "از", "و",
               "lyrics", "lyric", "mp3", "music", "song", "download", "official", "video", "audio", "feat", "ft", "به", "نام",
               "همراه", "ترجمه", "فارسی", "تکست", "اهنگ‌", "آهنگ‌", "بهترین", "بدون", "کلمات", "لیریک", "با‌متن", "و‌متن",
               "genius", "musixmatch", "azlyrics", "translation", "translations", "spotify", "deezer", "shazam"}
_SPLIT = re.compile(r"\s*(?:\||–|—|·|•|«|»|:\s|\s-\s|\s_\s|\s/\s)\s*")
_PRE = re.compile(r"^(?:دانلود\s+)?(?:متن|لیریک(?:س|ز)?|ترجمهٔ?|کلمات)?\s*(?:و\s+(?:متن|ترجمه)\s+)?(?:آهنگ|اهنگ|ترانه|موزیک(?:\s+ویدیو)?)\s+", re.I)

_PAT_TITLE_BY = [
    # «متن آهنگ X از Y» / «دانلود آهنگ X از Y» / «X از Y»
    re.compile(r"^(?:دانلود\s+)?(?:متن\s+)?(?:آهنگ|اهنگ|ترانه)\s+(?P<t>.+?)\s+(?:از|با\s+صدای|اثر|به\s+خوانندگی)\s+(?P<a>.+?)$"),
    # «دانلود آهنگ Y به نام X» / «آهنگ Y به نام X»
    re.compile(r"^(?:دانلود\s+)?(?:متن\s+)?(?:آهنگ|اهنگ|ترانه)\s+(?P<a>.+?)\s+(?:به\s+نام|بنام)\s+(?P<t>.+?)$"),
    # EN: «X by Y» / «Y - X lyrics»
    re.compile(r"^(?P<t>.+?)\s+(?:lyrics\s+)?by\s+(?P<a>.+?)(?:\s+lyrics)?$", re.I),
]


def _clean_name(s: str) -> str:
    s = _html.unescape(s or "")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)               # (Official Video) و …
    s = re.sub(r"[\"“”«»]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" \t-–—|:،,.")
    return s


def _strip_words(s: str) -> str:
    w = [x for x in s.split()]
    while w and norm(w[0]) in _SITE_WORDS:
        w.pop(0)
    while w and norm(w[-1]) in _SITE_WORDS:
        w.pop()
    return " ".join(w)


def title_hypotheses(raw: str, site_name: str = "") -> List[Tuple[str, str]]:
    """یک عنوان/تیتر صفحه ← فهرست (اسم آهنگ, خواننده)؛ خواننده ممکن است ''."""
    t = _clean_name(raw)
    if not t:
        return []
    if site_name:
        t = re.sub(rf"\s*[\|\-–—:]\s*{re.escape(site_name)}\s*$", "", t, flags=re.I)
    out: List[Tuple[str, str]] = []
    for pat in _PAT_TITLE_BY:
        m = pat.match(t)
        if m:
            tt, aa = _strip_words(_clean_name(m.group("t"))), _strip_words(_clean_name(m.group("a")))
            if tt and aa:
                out.append((tt, aa))
    segs = [s for s in _SPLIT.split(t) if s and s.strip()]
    segs = [_strip_words(_clean_name(s)) for s in segs]
    segs = [s for s in segs if s]
    if len(segs) >= 2:
        # «خواننده - اسم» و «اسم - خواننده»: هر دو را فرضیه می‌کنیم؛ رأی‌گیری/اسم خواننده انتخاب می‌کند
        out.append((segs[1], segs[0]))
        out.append((segs[0], segs[1]))
    elif len(segs) == 1:
        out.append((segs[0], ""))
    return [(a, b) for a, b in out if a]


def _page_candidates(src: str, url: str) -> List[Dict[str, Any]]:
    """همهٔ فرضیه‌های (title, artist) یک صفحه با وزن اعتماد: ساختاریافته > h1/og > <title>."""
    cands: List[Dict[str, Any]] = []
    site = _meta(src, "og:site_name")
    # اسم خودِ سایت («سونامی»، «گندم موزیک»…) نباید به‌عنوان خواننده/آهنگ رأی بیاورد
    site_set = {norm(x) for x in (site, _meta(src, "application-name"), _meta(src, "og:site"),
                                  _meta(src, "twitter:site")) if x and norm(x)}
    dom_label = norm(_domain(url).split(".")[0]) if url else ""
    if dom_label:
        site_set.add(dom_label)

    def _is_site(x: str) -> bool:
        nx = norm(x)
        return bool(nx) and any(nx == y or (len(nx) > 3 and (nx in y or y in nx)) for y in site_set if y)

    def add(t, a, w, how):
        t, a = _clean_name(t), _clean_name(a)
        if a and _is_site(a):
            a = ""
        if t and _is_site(t):
            return
        if t and 1 <= len(t) <= 80 and len(a) <= 60:
            cands.append({"title": t, "artist": a, "w": w, "how": how})

    for it in _jsonld(src):                                   # بهترین منبع: schema.org MusicRecording / MusicComposition
        ty = it.get("@type")
        ty = " ".join(ty) if isinstance(ty, list) else str(ty or "")
        if "Music" in ty and it.get("name"):
            by = it.get("byArtist") or it.get("author") or it.get("composer") or ""
            if isinstance(by, list):
                by = by[0] if by else ""
            if isinstance(by, dict):
                by = by.get("name", "")
            add(str(it["name"]), str(by or ""), 3.0, "jsonld")
    mus = _meta(src, "music:musician") or _meta(src, "og:music:musician")
    ogt = _meta(src, "og:title")
    if ogt and mus and not mus.startswith("http"):
        add(ogt, mus, 2.5, "og-music")
    for raw, w, how in ((_h1(src), 2.0, "h1"), (ogt, 1.6, "og"), (
            _html.unescape(re.sub(r"\s+", " ", (re.search(r"<title[^>]*>(.*?)</title>", src, re.I | re.S) or [None, ""])[1])).strip(),
            1.2, "title")):
        for t, a in title_hypotheses(raw, site):
            add(t, a, w if a else w * 0.5, how)
    # slug آدرس هم قرینه‌ای است (فقط وقتی لاتین است)
    return cands


def phrase_variants(phrase: str) -> List[str]:
    """خودِ عبارت + (برای عبارت‌های بلند) نیمه‌ها و ابتدا/انتها. اگر کاربر کنار بیت جملهٔ اضافه نوشته باشد
    («یه اهنگی بود میگفت … بیت … از کیه») تطبیق کل عبارت با متن صفحه هیچ‌وقت بالا نمی‌رود؛ یکی از تکه‌ها ولی بالا می‌رود."""
    w = (phrase or "").split()
    out = [phrase]
    if len(w) >= 9:
        h = len(w) // 2
        for part in (w[:h + 1], w[h - 1:], w[:7], w[-7:]):
            t = " ".join(part)
            if t and t not in out:
                out.append(t)
    return out


def _best_block(lines: List[str], phrase: str) -> Tuple[str, float, str]:
    """بهترین (بلوک, امتیاز, تکه‌ای که تطبیق شد) بین خودِ عبارت و تکه‌هایش؛ تکه‌ها فقط وقتی امتحان می‌شوند که کل عبارت نخورد."""
    best = ("", 0.0, phrase)
    for ph in phrase_variants(phrase):
        blk, sc = lyric_block(lines, ph)
        if (bool(blk), sc) > (bool(best[0]), best[1]):
            best = (blk, sc, ph)
        if best[0] and best[1] >= 0.8:
            break
    return best


def parse_page(url: str, src: str, phrase: str) -> Dict[str, Any]:
    lines = _to_lines(src)
    block, score, _ph = _best_block(lines, phrase)
    if not block:                                             # سایت‌هایی که مصرع‌ها را با «\\n» (white-space: pre-line) می‌گذارند
        lines2 = _to_lines(src, keep_nl=True)
        b2, s2, _ph2 = _best_block(lines2, phrase)
        if b2:
            lines, block, score = lines2, b2, s2
    if not block:                                             # بیت ممکن است در یک خطِ بلندِ <p> بدون <br> باشد
        flat = " ".join(x for x in lines if x and x != _SENT)
        if flat:
            score = max([score] + [phrase_score(ph, flat) for ph in phrase_variants(phrase)])
    _flat = block or " ".join(x for x in lines if x and x != _SENT)
    return {"url": url, "domain": _domain(url), "score": score, "lyrics": block,
            "cands": _page_candidates(src, url), "h1": _h1(src), "body_norm": norm(_flat)[:8000]}


def get_page(url: str, phrase: str) -> Optional[Dict[str, Any]]:
    key = url + "|" + norm(phrase)
    c = _PAGE_CACHE.get(key)
    if c is not None:
        return c or None
    if _NEG_PAGE.get(key) is not None:
        return None
    src = _fetch(url)
    if not src:
        _NEG_PAGE.set(key, {})                                # کوتاه‌مدت: خطای شبکه/تایم‌اوت نباید ساعت‌ها ماندگار شود
        return None
    try:
        p = parse_page(url, src, phrase)
    except Exception as e:                                    # پارسِ یک سایت نباید کل جستجو را بیندازد
        logger.warning(f"[LWEB] parse خطا {url[:60]}: {type(e).__name__}: {str(e)[:100]}")
        _NEG_PAGE.set(key, {})
        return None
    _PAGE_CACHE.set(key, p)
    return p


# ----------------------------------------------------------------------------- رأی‌گیری
def _same_name(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb.split() and len(na) > 3:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.84


def _title_is_lyric(phrase: str, title: str, body_norm: str = "") -> bool:
    """عنوانِ پیشنهادی در واقع خودِ مصرع/بیت است (سایت‌های فارسی زیاد عنوان صفحه را مصرع اول می‌گذارند).
    سه حالت: ۱) عنوان ≈ بیت  ۲) عنوان «بیت + چند کلمهٔ دیگر» است (مصرعِ بلندتر که بیت داخلش هست)  ۳) عنوان بلند
    (> ۸ کلمه) یا عیناً داخل بدنهٔ متن صفحه آمده (عنوان آهنگ‌ها کوتاه است و معمولاً چند کلمهٔ پیوسته در متن نیست)."""
    nt, np_ = norm(title), norm(phrase)
    if not nt:
        return True
    tw, pw = nt.split(), np_.split()
    if len(tw) >= 5 and phrase_score(title, phrase) >= 0.8:
        return True
    if difflib.SequenceMatcher(None, np_, nt).ratio() >= 0.8:
        return True
    if len(pw) >= 3 and len(tw) >= 4 and phrase_score(phrase, title) >= 0.85:      # بیت داخل عنوان است
        return True
    if len(tw) > 8:
        return True
    if body_norm and len(tw) >= 4 and (" " + nt + " ") in (" " + body_norm + " "):
        return True
    return False


def vote(pages: List[Dict[str, Any]], phrase: str, artist_hint: str = "") -> Optional[Dict[str, Any]]:
    """از صفحه‌های تأییدشده، اسم آهنگ و خواننده را با رأی وزن‌دار دامنه‌های مستقل انتخاب می‌کند."""
    groups: List[Dict[str, Any]] = []
    for p in pages:
        seen_in_page = set()
        for c in p["cands"]:
            if _title_is_lyric(phrase, c["title"], p.get("body_norm", "")):
                continue
            tn = norm(c["title"])
            if len(tn) < 2 or tn in seen_in_page and False:
                continue
            for g in groups:
                if _same_name(g["title"], c["title"]):
                    break
            else:
                g = {"title": c["title"], "artists": {}, "domains": {}, "w": 0.0, "hows": set()}
                groups.append(g)
            key = p["domain"]
            if g["domains"].get(key, 0) < c["w"]:
                g["domains"][key] = c["w"]
            if c["artist"]:
                an = norm(c["artist"])
                g["artists"][an] = g["artists"].get(an, [c["artist"], 0.0])
                g["artists"][an][1] += c["w"] * (1.0 + p["score"])
            g["hows"].add(c["how"])
            if len(c["title"]) < len(g["title"]) and norm(c["title"]) in norm(g["title"]):
                g["title"] = c["title"]                       # کوتاه‌ترین شکلِ هم‌خوان (بدون پسوند)
    for g in groups:
        g["w"] = sum(g["domains"].values())
        if artist_hint:
            for an, (raw, w) in g["artists"].items():
                if _same_name(raw, artist_hint) or _same_name(artist_hint, raw):
                    g["w"] += 1.5
                    break
    if not groups:
        return None
    groups.sort(key=lambda g: (g["w"], len(g["domains"])), reverse=True)
    g = groups[0]
    artist = ""
    if g["artists"]:
        artist = max(g["artists"].values(), key=lambda x: x[1])[0]
    nd = len(g["domains"])
    structured = bool(g["hows"] & {"jsonld", "og-music", "serp-lyrics"}) and bool(artist)   # ساختاریافته ولی بدون خواننده ← اطمینان بالا نه
    conf = "high" if (nd >= 2 or structured) else "low"
    # «الف - ب» بدون هیچ قرینه: ممکن است «خواننده - آهنگ» یا برعکس باشد؛ اگر گروه رقیب دقیقاً جابه‌جاشدهٔ همین باشد مبهم است
    ambiguous = False
    if conf == "low" and artist:
        for g2 in groups[1:]:
            if abs(g2["w"] - g["w"]) < 0.01 and _same_name(g2["title"], artist) and any(
                    _same_name(raw2, g["title"]) for raw2, _w in g2["artists"].values()):
                ambiguous = True
                break
    return {"title": g["title"], "artist": artist, "domains": sorted(g["domains"]), "n_domains": nd,
            "weight": round(g["w"], 2), "confidence": conf, "ambiguous": ambiguous}


# ----------------------------------------------------------------------------- شواهد خودِ نتایج جستجو (برای سایت‌های بلاک‌شده)
# Genius/Musixmatch… از سرور ما ۴۰۳ می‌دهند ولی «عنوان + اسنیپت» همان نتیجه در خروجی جستجو هست و اسنیپت خودِ مصرع را دارد.
_LYR_HOSTS = ("genius.com", "musixmatch.com", "azlyrics.com", "lyricstranslate.com", "lyrics.com", "songlyrics.com",
              "lyricsfreak.com", "metrolyrics.com", "lyricsmode.com")
_SERP_TAIL = re.compile(r"\s*[\|\-–—]\s*(?:Genius(?:\s+Lyrics)?|Musixmatch|AZLyrics|Lyrics\s*Translate|LyricsTranslate|"
                        r"Lyrics\.com|SongLyrics|Shazam|Spotify|Deezer|YouTube|Apple\s+Music|Last\.fm)\s*$", re.I)
_SERP_TRANS = re.compile(r"translation|translations|ترجمه|traduc|übersetz", re.I)


def serp_hypothesis(raw_title: str, domain: str = "") -> Optional[Tuple[str, str]]:
    """عنوان یک نتیجهٔ جستجو ← (اسم آهنگ, خواننده) یا None. «Artist – Title (فارسی) Lyrics | Genius Lyrics» ← («Title (فارسی)», Artist)."""
    t = _html.unescape(raw_title or "").strip()
    if not t or _SERP_TRANS.search(t):
        return None
    t = _SERP_TAIL.sub("", t)
    t = re.sub(r"\s+(?:lyrics|لیریکس|متن آهنگ|متن ترانه)\s*$", "", t, flags=re.I).strip()
    m = re.match(r"^(?P<a>.+?)\s+[–—-]\s+(?P<t>.+)$", t)
    if not m:
        return None
    a, ti = m.group("a").strip(), m.group("t").strip()
    fa = re.search(r"\(([^)]*[\u0600-\u06FF][^)]*)\)", ti)
    ti_lat = _clean_name(ti)
    ti_fa = _clean_name(fa.group(1)) if fa else ""
    title = f"{ti_lat} ({ti_fa})" if (ti_fa and ti_lat and norm(ti_fa) != norm(ti_lat)) else (ti_lat or ti_fa)
    a = _strip_words(_clean_name(a))
    title = _strip_words(title) if title else ""
    if not title or not a or len(title) > 80:
        return None
    return title, a


def serp_pages(results: List[Dict[str, str]], phrase: str, skip_urls: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """نتایج جستجویی که «بیت» در عنوان/اسنیپتشان هست و عنوانشان الگوی «خواننده – آهنگ» دارد ← صفحه‌های شاهد (بدون متن کامل)."""
    skip = set(skip_urls)
    out: List[Dict[str, Any]] = []
    for r in results or []:
        u = r.get("url") or ""
        if not u or u in skip:
            continue
        dom = _domain(u)
        if not any(dom == h or dom.endswith("." + h) for h in _LYR_HOSTS):
            continue                                       # فقط میزبان‌های متن ترانه‌ای که الگوی عنوانشان استاندارد است
        body = f"{r.get('title') or ''} {r.get('body') or ''}"
        sc = phrase_score(phrase, body)
        if sc < 0.85:
            continue
        hyp = serp_hypothesis(r.get("title") or "", dom)
        if not hyp:
            continue
        t, a = hyp
        out.append({"url": u, "domain": dom, "score": sc, "lyrics": "", "h1": "", "serp_title": r.get("title") or "",
                    "body_norm": "", "cands": [{"title": t, "artist": a, "w": 1.6, "how": "serp-lyrics"}]})
    return out


# ----------------------------------------------------------------------------- جستجوی صفحه‌ها
def _queries(phrase: str, artist: str = "") -> List[str]:
    """چند شکل از بیت: دقیق (گیومه)، دقیق+«متن آهنگ»، بدون گیومه (تحمل غلط املایی)، فقط کلمه‌های مهم، و برای بیت بلند نیمهٔ اولش.
    قبلاً فقط شکل‌های گیومه‌دار بود؛ یک کلمهٔ فرق (دستا/دست‌ها، می‌گه/میگه) کل جستجو را خالی می‌کرد."""
    p = re.sub(r"\s+", " ", phrase).strip()
    ws = p.split()
    qs = [f'"{p}" متن آهنگ', f'"{p}"', f"{p} متن ترانه"]
    core = [w for w in ws if len(norm(w)) >= 3]
    if len(core) >= 3:
        qs.append(" ".join(core[:7]) + " متن آهنگ")
    if len(ws) >= 9:
        qs.append('"' + " ".join(ws[:len(ws) // 2 + 1]) + '" متن آهنگ')
    if artist:
        qs.insert(0, f'{artist} "{p}"')
    return list(dict.fromkeys(qs))[:4]


def _cand_priority(c: Dict[str, str], phrase: str) -> float:
    """نتیجه‌ای که عنوان/اسنیپتش به بیت می‌خورد یا «متن آهنگ» است زودتر دانلود شود (سقف تعداد صفحه‌ها نتیجهٔ خوب را نبُرد)."""
    sc = 0.0
    try:
        sc = phrase_score(phrase, f"{c.get('title', '')} {c.get('body', '')}")
    except Exception:
        pass
    blob = (c.get("title", "") + " " + c.get("url", "")).lower()
    if re.search(r"متن|ترانه|lyric|lyrics|text-music|ganjoor", blob):
        sc += 0.15
    return sc


def _candidate_urls(search_fn: Callable[..., list], queries: List[str], limit: int = 8,
                    deadline: float = 0.0, phrase: str = "") -> List[Dict[str, str]]:
    seen, out = set(), []
    futs = {_POOL.submit(search_fn, q, 8): q for q in queries}
    try:
        for f in as_completed(futs, timeout=max(1.0, (deadline - time.time()) if deadline else 10.0)):
            try:
                res = f.result() or []
            except Exception as e:
                logger.info(f"[LWEB] search خطا: {type(e).__name__}")
                continue
            for r in res:
                u = (r.get("href") or "").strip()
                if not u.startswith("http") or _is_bad_host(u) or u in seen:
                    continue
                seen.add(u)
                out.append({"url": u, "title": r.get("title") or "", "body": r.get("body") or ""})
    except Exception:
        pass
    if phrase:
        out.sort(key=lambda c: _cand_priority(c, phrase), reverse=True)
    return out[:limit]


def _fetch_all(cands: List[Dict[str, str]], phrase: str, deadline: float) -> List[Dict[str, Any]]:
    futs = {_POOL.submit(get_page, c["url"], phrase): c for c in cands}
    pages: List[Dict[str, Any]] = []
    try:
        for f in as_completed(futs, timeout=max(1.0, deadline - time.time())):
            try:
                p = f.result()
            except Exception:
                p = None
            if p:
                p["serp_title"] = futs[f]["title"]
                pages.append(p)
    except Exception:
        pass
    return pages


def _llm_pick(llm_fn: Callable[[str], Optional[str]], phrase: str, pages: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """وقتی رأی‌گیری قطعی نشد: بسته‌ی شواهد فشرده به مدل داده می‌شود؛ جواب فقط وقتی قبول است که اسم واقعاً در شواهد باشد."""
    ev = []
    for p in pages[:5]:
        ev.append(f"- دامنه: {p['domain']} | عنوان جستجو: {p.get('serp_title', '')[:100]} | h1: {p.get('h1', '')[:100]}")
    prompt = (
        "From the web evidence below, identify the SONG TITLE and ARTIST of the song containing this lyric line.\n"
        f"LYRIC LINE: {phrase}\n"
        "EVIDENCE (page titles from pages that were verified to contain the lyric):\n" + "\n".join(ev) + "\n\n"
        "Rules: the title must be copied from the evidence (never invented); a page title that is just the lyric line "
        "itself is NOT a song title. If you cannot tell, use null. "
        'Reply ONLY with JSON: {"title": "...|null", "artist": "...|null"}')
    try:
        raw = llm_fn(prompt) or ""
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
    except Exception as e:
        logger.info(f"[LWEB] llm_pick خطا: {type(e).__name__}")
        return None
    t = str(d.get("title") or "").strip()
    a = str(d.get("artist") or "").strip()
    if not t or t.lower() == "null" or _title_is_lyric(phrase, t):
        return None
    blob = norm(" ".join(f"{p.get('serp_title', '')} {p.get('h1', '')} " + " ".join(c["title"] for c in p["cands"])
                         for p in pages))
    if norm(t) not in blob:
        logger.info(f"[LWEB] llm_pick رد شد (اسم در شواهد نیست): {t!r}")
        return None
    return {"title": t, "artist": "" if a.lower() == "null" else a}


# ----------------------------------------------------------------------------- متدهای عمومی
_MIN_VERIFY = 0.80


def identify_phrase(phrase: str, search_fn: Callable[..., list], artist_hint: str = "",
                    llm_fn: Optional[Callable[[str], Optional[str]]] = None,
                    budget: float = 14.0) -> Optional[Dict[str, Any]]:
    """بیت ← {"title","artist","confidence","domains","score","lyrics","url"} یا None.
    فقط صفحه‌هایی که بیت در «متنشان» تأیید شده رأی می‌دهند. lyrics = متن کاملِ بهترین صفحه (برای «متنشو بده»)."""
    phrase = re.sub(r"\s+", " ", phrase or "").strip()
    if len(norm(phrase).split()) < 2:
        return None
    ck = norm(phrase) + "|" + norm(artist_hint)
    c = _ID_CACHE.get(ck)
    if c is None and artist_hint:                             # همین بیت قبلاً بدون راهنمای خواننده شناسایی شده؟ دوباره جستجو نکن
        c = _ID_CACHE.get(norm(phrase) + "|") or None
    if c is None:
        c = _NEG_CACHE.get(ck)                                # «پیدا نشد»ِ تازه (۲ دقیقه)
    if c is not None:
        return dict(c) if c else None
    t0 = time.time()
    deadline = t0 + budget
    urls = _candidate_urls(search_fn, _queries(phrase, artist_hint), limit=10, deadline=t0 + budget * 0.45, phrase=phrase)
    if not urls:
        logger.info(f"[LWEB] هیچ صفحه‌ای از جستجو نیامد: {phrase[:30]!r}")
        return None
    pages = _fetch_all(urls, phrase, deadline)
    need = _MIN_VERIFY if len(norm(phrase).split()) >= 4 else 0.9
    ok = [p for p in pages if p["score"] >= need]
    n_page_ok = len(ok)
    try:                                                      # شواهد اسنیپت نتایج (Genius و …) حتی وقتی خودِ صفحه ۴۰۳ داد
        sp = serp_pages(urls, phrase, skip_urls={p["url"] for p in ok})
    except Exception as e:
        logger.info(f"[LWEB] serp_pages خطا: {type(e).__name__}: {str(e)[:80]}")
        sp = []
    ok = ok + sp
    logger.info(f"[LWEB] {phrase[:30]!r}: {len(urls)} صفحهٔ کاندید ← {len(pages)} دانلود ← {n_page_ok} تأییدشده "
                f"({', '.join(p['domain'] for p in ok if p not in sp)[:120]}) + {len(sp)} شاهد اسنیپت "
                f"({', '.join(p['domain'] for p in sp)[:80]}) {time.time() - t0:.1f}s")
    if not ok:
        top = sorted(pages, key=lambda p: p["score"], reverse=True)[:4]
        logger.info(f"[LWEB] بیت در هیچ صفحه‌ای تأیید نشد (نیاز={need}) | بهترین‌ها: "
                    + (", ".join(f"{p['domain']}={p['score']:.2f}" for p in top) or "هیچ صفحه‌ای دانلود نشد"))
        if pages:                                             # فقط وقتی واقعاً صفحه‌ای دیده شد؛ نبودِ صفحه = شکست موقت شبکه/جستجو ← کش نکن
            _NEG_CACHE.set(ck, {})
        return None
    res = vote(ok, phrase, artist_hint)
    if (not res or res["confidence"] == "low") and llm_fn and deadline - time.time() > 3:
        pick = _llm_pick(llm_fn, phrase, ok)
        if pick:
            res = {"title": pick["title"], "artist": pick["artist"] or (res or {}).get("artist", ""),
                   "domains": [p["domain"] for p in ok], "n_domains": len(ok), "weight": 0.0,
                   "confidence": "high" if len(ok) >= 2 else "low"}
    if res and res.get("ambiguous"):                          # ترتیب خواننده/آهنگ معلوم نیست و مدل هم نتوانست تصمیم بگیرد ← ادعا نکن
        logger.info(f"[LWEB] ترتیب «خواننده - آهنگ» مبهم است ({res.get('title')!r} / {res.get('artist')!r}) ← بدون ادعای اسم")
        res = None
    if not res:
        # بیت تأیید شد ولی اسمِ قابل‌اعتماد درنیامد؛ متن را نگه می‌داریم (برای «متنشو بده») بدون ادعای اسم
        best = max(ok, key=lambda p: (bool(p["lyrics"]), p["score"]))
        out = {"title": "", "artist": artist_hint, "confidence": "none", "domains": [p["domain"] for p in ok],
               "score": best["score"], "lyrics": best["lyrics"], "url": best["url"]}
        _ID_CACHE.set(ck, out)
        return dict(out)
    if res and _title_is_lyric(phrase, res.get("title", ""), max((p.get("body_norm", "") for p in ok), key=len, default="")):
        logger.info(f"[LWEB] عنوان نهایی شبیه مصرع است ← رد شد: {res.get('title')!r}")
        best = max(ok, key=lambda p: (bool(p["lyrics"]), p["score"]))
        out = {"title": "", "artist": artist_hint, "confidence": "none", "domains": [p["domain"] for p in ok],
               "score": best["score"], "lyrics": best["lyrics"], "url": best["url"]}
        _ID_CACHE.set(ck, out)
        return dict(out)
    with_text = [p for p in ok if p["lyrics"]]
    best = max(with_text or ok, key=lambda p: (p["score"], len(p["lyrics"])))
    out = dict(res, score=best["score"], lyrics=best["lyrics"], url=best["url"])
    _ID_CACHE.set(ck, out)
    logger.info(f"[LWEB] ✅ {out['artist']!r} - {out['title']!r} conf={out['confidence']} domains={out['domains']}")
    return dict(out)


def lyrics_from_web(artist: str, title: str, search_fn: Callable[..., list],
                    phrase: str = "", budget: float = 12.0) -> Optional[Dict[str, Any]]:
    """«خواننده + اسم» ← متن آهنگ از صفحه‌های وب.
    صفحه وقتی قبول است که اسم آهنگ (و در صورت وجود خواننده) در عنوان/تیترش باشد و بلوک مصرع‌ها (≥۶ خط) داشته باشد.
    اگر phrase داده شود (بیتی که شناسایی شد) باید در بلوک باشد. دو صفحهٔ هم‌خوان = اطمینان بالا."""
    artist, title = (artist or "").strip(), (title or "").strip()
    if not (title or artist):
        return None
    ck = norm(f"{artist} {title}")
    c = _LYR_CACHE.get(ck)
    if c is not None:
        return dict(c) if c else None
    t0 = time.time()
    deadline = t0 + budget
    qs = [f"متن آهنگ {title} {artist}".strip(), f"{artist} {title} lyrics".strip(), f"{title} {artist} متن ترانه".strip()]
    urls = _candidate_urls(search_fn, qs, limit=8, deadline=t0 + budget * 0.45)
    if not urls:
        return None
    # برای استخراج بلوک، «بیت» لازم است؛ اگر نداریم، از صفحهٔ بعدی‌ها خطِ میانیِ بلوک را به‌عنوان لنگر می‌گیریم
    anchor = phrase or ""
    futs = {_POOL.submit(_fetch, u["url"]): u for u in urls}
    srcs: List[Tuple[Dict[str, str], str]] = []
    try:
        for f in as_completed(futs, timeout=max(1.0, deadline - time.time())):
            try:
                s = f.result()
            except Exception:
                s = None
            if s:
                srcs.append((futs[f], s))
    except Exception:
        pass
    results = []
    qtoks = [w for w in norm(f"{artist} {title}").split() if len(w) > 1]
    for u, s in srcs:
        try:
            cands = _page_candidates(s, u["url"]) + [{"title": t, "artist": a, "w": 0.5, "how": "serp"}
                                                      for t, a in title_hypotheses(u["title"])]
            blob = norm(" ".join(c["title"] + " " + c["artist"] for c in cands) + " " + u["title"] + " " + _h1(s))
            btoks = set(blob.split())
            if artist:
                t_ok = (not title) or norm(title) in blob or _same_name(title, " ".join(blob.split()[:8]))
                a_ok = any(w in btoks for w in norm(artist).split())
            else:                                             # کوئری آزاد «خواننده اسم» (از find_lyrics): پوشش کلمه‌ها
                t_ok = a_ok = bool(qtoks) and sum(1 for w in qtoks if w in btoks) / len(qtoks) >= 0.75
            if not (t_ok and a_ok):
                continue
            blk, sc = "", 0.0
            for keep in (False, True):                        # اول معنای HTML؛ اگر بلوک درنیامد، «\\n» منبع هم خط‌شکن
                lines = _to_lines(s, keep_nl=keep)
                if anchor:
                    blk, sc = lyric_block(lines, anchor)
                else:                                         # بدون لنگر: بیشترین دنبالهٔ مصرع‌مانندِ صفحه
                    blk = auto_block(lines)
                    sc = 1.0 if blk else 0.0
                if blk:
                    break
            if blk and sc >= (0.8 if anchor else 0.95):
                results.append({"lyrics": blk, "url": u["url"], "domain": _domain(u["url"]), "cands": cands,
                                "n": sum(1 for x in blk.split("\n") if x.strip())})
        except Exception as e:
            logger.info(f"[LWEB] lyrics parse خطا {u['url'][:60]}: {type(e).__name__}")
    if not results:
        _LYR_CACHE.set(ck, {})
        return None
    results.sort(key=lambda r: r["n"], reverse=True)
    best = results[0]
    # اگر چند صفحه هست، میانه‌ی اندازه را ترجیح بده (کوتاه‌ترین‌ها ناقص‌اند؛ بلندترین‌ها گاهی شامل ترجمه/نظرات)
    if len(results) >= 3:
        best = results[len(results) // 2 - 1] if results[0]["n"] > 2.2 * results[-1]["n"] else results[0]
    named = vote([{"cands": r["cands"], "domain": r["domain"], "score": 1.0} for r in results], "", artist)
    out = {"lyrics": best["lyrics"], "url": best["url"], "source": best["domain"], "n_sources": len(results),
           "title": (named or {}).get("title") or title, "artist": (named or {}).get("artist") or artist}
    _LYR_CACHE.set(ck, out)
    logger.info(f"[LWEB] متن {artist!r}-{title!r} از {best['domain']} ({best['n']} خط، {len(results)} منبع)")
    return dict(out)


def lyrics_for_phrase(phrase: str, search_fn: Callable[..., list], artist_hint: str = "",
                      llm_fn: Optional[Callable[[str], Optional[str]]] = None, budget: float = 14.0) -> Optional[Dict[str, Any]]:
    """«متنشو بده» وقتی فقط بیت (و شاید خواننده) معلوم است: نتیجهٔ identify_phrase را بدون نیاز به اسم آهنگ برمی‌گرداند
    (اسم ممکن است '' باشد؛ ولی lyrics پر است). None = بیت در هیچ صفحهٔ قابل‌دسترسی تأیید نشد."""
    r = identify_phrase(phrase, search_fn, artist_hint=artist_hint, llm_fn=llm_fn, budget=budget)
    return r if (r and r.get("lyrics")) else None
