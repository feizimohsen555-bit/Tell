#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pinterest Link Module for Nokhod Bot — فقط «دانلود از روی لینک»
(نسخهٔ سبک‌شدهٔ pinterest_service.py: جستجو، توکن/API موبایل، Bing/ddgs، هوش مصنوعی و فیلتر ابعاد حذف شده‌اند)

کنار bot.py بگذار (فایل pinterest_service.py قبلی دست‌نخورده می‌ماند).

    from pinterest_link import extract_pin_url, get_pin_media, download_pin_media
    url   = extract_pin_url(text)              # اولین لینک پینترست/pin.it داخل متن، یا None
    media = get_pin_media(url)                 # {"type": "photo"|"video", "url", "title", "width", "height"} یا None
    dl    = download_pin_media(media, max_bytes=45*1024*1024)   # {"data": bytes, "content_type", "ext", "url"} یا None

متغیرهای محیطی (همه اختیاری):
    PINTEREST_PROXY    پروکسی برای درخواست‌ها (اگر نبود AI_PROXY خوانده می‌شود)
    PINTEREST_COOKIES  کوکی(های) _pinterest_sess جداشده با کاما — برای پین‌هایی که بدون لاگین باز نمی‌شوند
"""

import os
import re
import base64
import time
import json
import html as _html
import logging
import urllib.parse
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter

try:                                    # retry خودکار روی خطای شبکه (RemoteDisconnected و …)
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

try:                                    # اختیاری: pip install curl_cffi → اثر انگشت TLS مرورگر برای وقتی که requests رد می‌شود
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover
    _cffi = None

logger = logging.getLogger("PinterestLink")

PINTEREST_PROXY = (os.environ.get("PINTEREST_PROXY") or os.environ.get("AI_PROXY") or "").strip()

# کوکی سشن مرورگر (_pinterest_sess)؛ ⚠️ معادل رمز عبور اکانت است و فقط در .env بماند. خالی = مهمان.
_AUTH_COOKIE_NAMES = ("_pinterest_sess", "_auth")


def _clean_cookie_value(v: str) -> str:
    v = v.split(";")[0].strip().strip("\"'").strip()          # «name=value; Path=/…» چسبانده شده باشد
    if v.lower().startswith("_pinterest_sess="):
        v = v.split("=", 1)[1].strip()
    return v


_RAW_COOKIES = os.environ.get("PINTEREST_COOKIES", "")
PINTEREST_COOKIES = list(dict.fromkeys(c for c in (_clean_cookie_value(x) for x in _RAW_COOKIES.split(",")) if c))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
_WEB_UA = USER_AGENTS[0]

_SIZE_SEG_RE = re.compile(r"/(?:\d+x(?:\d+)?|originals)/")
# پینترست بعضی originalها را HEIC/AVIF می‌دهد؛ تلگرام آن‌ها را باز نمی‌کند → نسخهٔ JPG ترجیح داده می‌شود
_MODERN_EXT_RE = re.compile(r"\.(?:heic|heif|avif)(?:[?#]|$)", re.I)


def _is_pin_host(url: str) -> bool:
    return "pinimg.com" in (url or "")


def _make_adapter() -> HTTPAdapter:
    """HTTPAdapter با retry: «Connection aborted / RemoteDisconnected» معمولاً یعنی کانکشن keep-alive کهنه (سرور بعد از چند دقیقه
    بسته) یا قطع لحظه‌ایِ پینترست؛ بدون retry (پیش‌فرض requests = صفر) همان یک خطا کل درخواست را می‌کشت."""
    if Retry is None:
        return HTTPAdapter(pool_connections=10, pool_maxsize=20)
    kw = dict(total=3, connect=2, read=2, backoff_factor=0.4, raise_on_status=False)
    try:
        retry = Retry(allowed_methods=frozenset(["GET", "HEAD"]), **kw)
    except TypeError:                   # urllib3 قدیمی (<1.26)
        retry = Retry(method_whitelist=frozenset(["GET", "HEAD"]), **kw)
    return HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)


class PinterestEngine:
    """موتور سبک: فقط session (پروکسی/کوکی) و ابزارهای وب لازم برای PinterestMediaExtractor."""

    def __init__(self):
        self._cookie_index = 0
        self._dead_cookies: Dict[str, float] = {}
        self._cookie_lock = threading.Lock()
        self._web_lock = threading.Lock()
        self._web_boot_ts = 0.0

        self.session = requests.Session()
        self.session.mount("https://", _make_adapter())
        self.session.mount("http://", _make_adapter())
        if PINTEREST_PROXY:
            self.session.proxies.update({"http": PINTEREST_PROXY, "https": PINTEREST_PROXY})
        self.session.headers.update({"Accept-Language": "en-US,en;q=0.9"})

    # ----------------------------------------------------------- کوکی‌های سشن وب
    def _get_next_cookie(self) -> Optional[str]:
        """کوکی _pinterest_sess بعدی (Round-Robin)؛ کوکی‌های کنار گذاشته‌شده رد می‌شوند. None = مهمان."""
        with self._cookie_lock:
            now = time.time()
            pool = [c for c in PINTEREST_COOKIES if self._dead_cookies.get(c, 0) <= now]
            if not pool:
                return None
            cookie = pool[self._cookie_index % len(pool)]
            self._cookie_index = (self._cookie_index + 1) % len(pool)
            return cookie

    def _mark_cookie_bad(self, cookie: str, cooldown_sec: int = 1800):
        with self._cookie_lock:
            self._dead_cookies[cookie] = time.time() + cooldown_sec
        n = PINTEREST_COOKIES.index(cookie) + 1 if cookie in PINTEREST_COOKIES else "?"
        logger.warning(f"[PINTEREST-WEB] کوکی سشن #{n} موقتاً کنار گذاشته شد برای {cooldown_sec} ثانیه.")

    def _cookie_header(self, cookie: str) -> str:
        """هدر Cookie کامل: کوکی‌های jar (از جمله csrftoken بدون تکرار) + _pinterest_sess فعال + _auth=1.
        نسخهٔ مهمانِ _pinterest_sess/_auth که خود پینترست در jar می‌گذارد حذف می‌شود تا کوکیِ تکراری ارسال نشود."""
        self._csrf_token()                      # نسخه‌های تکراری csrftoken را پاک می‌کند (منطق قبلی دست‌نخورده)
        pairs: Dict[str, str] = {}
        for c in list(self.session.cookies):
            if c.name not in _AUTH_COOKIE_NAMES:
                pairs[c.name] = c.value or ""
        pairs["_pinterest_sess"] = cookie
        pairs["_auth"] = "1"
        return "; ".join(f"{k}={v}" for k, v in pairs.items())

    def _web_get(self, url: str, *, headers: Dict[str, str], timeout: int,
                 params: Optional[Dict[str, Any]] = None, cookie: Optional[str] = None,
                 follow_redirects: bool = True):
        """اول requests (با retry)؛ اگر بعد از retryها هم خطای اتصال داد و curl_cffi نصب بود، یک بار با اثر انگشت کروم."""
        try:
            return self._web_get_requests(url, headers=headers, timeout=timeout, params=params,
                                          cookie=cookie, follow_redirects=follow_redirects)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            if _cffi is None:
                raise
            logger.info(f"[PINTEREST-WEB] requests ناموفق ({type(e).__name__}) ← تلاش با curl_cffi: {url[:70]}")
            try:
                return self._cffi_get(url, headers=headers, timeout=timeout, params=params,
                                      cookie=cookie, follow_redirects=follow_redirects)
            except Exception as e2:
                logger.warning(f"[PINTEREST-WEB] curl_cffi هم ناموفق: {type(e2).__name__}: {e2}")
                raise e

    def _cffi_get(self, url: str, *, headers: Dict[str, str], timeout: int, params=None,
                  cookie: Optional[str] = None, follow_redirects: bool = True):
        """درخواست با curl_cffi (impersonate=chrome). ریدایرکت‌ها دستی و فقط به میزبان‌های مجاز؛ کوکی فقط به pinterest.*"""
        kw: Dict[str, Any] = {"timeout": timeout, "allow_redirects": False, "impersonate": "chrome"}
        if PINTEREST_PROXY:
            kw["proxies"] = {"http": PINTEREST_PROXY, "https": PINTEREST_PROXY}
        resp = None
        for _ in range(4 if follow_redirects else 1):
            h = dict(headers)
            host = urllib.parse.urlparse(url).hostname or ""
            if cookie and _PIN_HOST_RE.match(host):
                h["Cookie"] = f"_pinterest_sess={cookie}; _auth=1"
            resp = _cffi.get(url, headers=h, params=params, **kw)
            loc = resp.headers.get("Location")
            if not follow_redirects or resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            url, params = urllib.parse.urljoin(url, loc), None
            if not PinterestMediaExtractor._is_allowed_host(url):
                break
        return resp

    def _web_get_requests(self, url: str, *, headers: Dict[str, str], timeout: int,
                 params: Optional[Dict[str, Any]] = None, cookie: Optional[str] = None,
                 follow_redirects: bool = True) -> requests.Response:
        """(مسیر requests) بدون cookie دقیقاً مثل session.get قبلی. با cookie، هدر Cookie را خودمان می‌سازیم
        (requests هدر دستی را با jar بازنویسی می‌کند) و ریدایرکت‌ها را دستی دنبال می‌کنیم تا لاگین از دست نرود."""
        if cookie and not _PIN_HOST_RE.match(urllib.parse.urlparse(url).hostname or ""):
            cookie = None                       # کوکی هرگز به میزبانِ غیر pinterest.* (مثل pin.it) فرستاده نمی‌شود
        if not cookie:
            return self.session.get(url, headers=headers, params=params, timeout=timeout,
                                    allow_redirects=follow_redirects)
        resp = None
        for _ in range(4 if follow_redirects else 1):
            prep = self.session.prepare_request(requests.Request("GET", url, headers=headers, params=params))
            prep.headers["Cookie"] = self._cookie_header(cookie)
            resp = self.session.send(prep, timeout=timeout, allow_redirects=False)
            loc = resp.headers.get("Location")
            if not follow_redirects or resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            url, params = urllib.parse.urljoin(resp.url, loc), None
            if not _PIN_HOST_RE.match(urllib.parse.urlparse(url).hostname or ""):
                break
        return resp

    @staticmethod
    def _upgrade_to_hd(url: str) -> str:
        if not url or "pinimg.com" not in url:
            return url
        # هر اندازهٔ کوچک (236x/474x/564x/736x/...) → originals (اگر موجود نبود، bot.py خودش به 1200x و 736x برمی‌گردد)
        return _SIZE_SEG_RE.sub("/originals/", url, count=1)

    # ----------------------------------------------------------- لایه ۲: وب با CSRF
    def _csrf_token(self) -> str:
        """مقدار کوکی csrftoken را امن می‌خواند.
        پینترست این کوکی را هم برای «.pinterest.com» و هم «www.pinterest.com» می‌گذارد و `cookies.get()` در requests
        با دو کوکیِ هم‌نام CookieConflictError می‌دهد. اینجا نسخه‌های تکراری حذف می‌شوند و فقط آخرینِ ست‌شده می‌ماند
        (تا هدر X-CSRFToken با همان کوکیِ ارسالی یکی باشد)."""
        jar = self.session.cookies
        found = [c for c in jar if c.name == "csrftoken"]
        if not found:
            return ""
        keep = found[-1]
        for c in found[:-1]:
            try:
                jar.clear(c.domain, c.path, c.name)
            except KeyError:
                pass
        return keep.value or ""

    def _web_bootstrap(self, force: bool = False) -> str:
        """یک بار صفحهٔ HTML را می‌گیرد تا کوکی csrftoken بنشیند؛ مقدار CSRF را برمی‌گرداند."""
        with self._web_lock:
            tok = self._csrf_token()
            if tok and not force and time.time() - self._web_boot_ts < 3600:
                return tok
            if force:
                self.session.cookies.clear()
            try:
                self.session.get(
                    "https://www.pinterest.com/",
                    headers={
                        "User-Agent": _WEB_UA,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                    timeout=7,
                )
                self._web_boot_ts = time.time()
            except Exception as e:
                logger.warning(f"[PINTEREST-WEB] دریافت کوکی اولیه ناموفق: {e}")
            return self._csrf_token()


_pinterest_engine = PinterestEngine()


# ===========================================================================
# استخراج لینک مستقیم مدیای پین (بدون تغییر نسبت به pinterest_service.py)
# ===========================================================================
_PIN_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*pinterest\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_PIN_ID_RE = re.compile(r"/pin/(?:[^/?#]*--)?(\d+)")
_CANONICAL_PIN_PATH_RE = re.compile(r"^/pin/(?:[^/]*--)?\d+/?$")
_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_QUALITY_RE = re.compile(r"(\d{3,4})P", re.I)

_MAX_REDIRECTS = 8
_IMG_SIZE_KEYS = ("orig", "1200x", "736x", "564x", "474x", "236x")


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class PinterestMediaExtractor:
    """
    از لینک پین (کوتاه pin.it یا کامل pinterest.com/pin/...) مستقیم‌ترین و باکیفیت‌ترین مدیا را بیرون می‌کشد.
    از همان session موتور اصلی (پروکسی/کوکی) استفاده می‌کند.
    """

    def __init__(self, engine: PinterestEngine):
        self.engine = engine

    # ----------------------------------------------------------- ابزارهای کوچک
    @staticmethod
    def _is_allowed_host(url: str) -> bool:
        """فقط pin.it و دامنه‌های pinterest مجازند؛ جلوی ریدایرکت به هر سایت دیگری گرفته می‌شود."""
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        return host == "pin.it" or bool(_PIN_HOST_RE.match(host))

    @classmethod
    def _normalize_url(cls, url: Any) -> str:
        if not isinstance(url, str):
            return ""
        url = "".join(url.split())
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url.lstrip("/")
        url = re.sub(r"^http://", "https://", url, flags=re.I)
        return url if cls._is_allowed_host(url) else ""

    @staticmethod
    def _walk(root: Any) -> Iterator[dict]:
        """پیمایش غیربازگشتی همهٔ دیکشنری‌های یک ساختار JSON."""
        stack = [root]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(v for v in node if isinstance(v, (dict, list)))

    @staticmethod
    def _url_ext(url: str) -> str:
        path = urllib.parse.urlparse(url).path.lower()
        if path.endswith(".mp4"):
            return "mp4"
        if path.endswith(".m3u8"):
            return "m3u8"
        return ""

    @staticmethod
    def _hls_to_mp4(url: str) -> str:
        """.../videos/.../hls/xxx.m3u8 → .../videos/.../720p/xxx.mp4 (روش مرسوم برای پین‌هایی که فقط HLS دارند)."""
        if "/hls/" not in url or not url.lower().split("?", 1)[0].endswith(".m3u8"):
            return ""
        base, _, query = url.partition("?")
        base = base.replace("/hls/", "/720p/", 1)
        base = re.sub(r"\.m3u8$", ".mp4", base, flags=re.I)
        return base + (f"?{query}" if query else "")

    def _probe(self, url: str) -> int:
        """کد وضعیت فایل روی CDN (۰ = خطای شبکه). HEAD؛ اگر CDN آن را رد کرد، یک GET یک‌بایتی (Range)."""
        hdr = {"User-Agent": _WEB_UA, "Referer": "https://www.pinterest.com/"}
        try:
            r = self.engine.session.head(url, headers=hdr, timeout=6, allow_redirects=True)
            if r.status_code in (403, 405, 501):
                r = self.engine.session.get(url, headers={**hdr, "Range": "bytes=0-0"}, timeout=6, stream=True)
                r.close()
            return r.status_code
        except requests.RequestException as e:
            logger.warning(f"[PINTEREST-MEDIA] بررسی وجود فایل ناموفق: {e}")
            return 0

    def _url_exists(self, url: str) -> bool:
        return self._probe(url) in (200, 206)

    # ----------------------------------------------------------- دریافت صفحه (با دنبال‌کردن ریدایرکت‌ها)
    def _fetch_page(self, url: str) -> Optional[Tuple[str, str]]:
        """خروجی: (آدرس نهایی، HTML). ریدایرکت‌ها دستی دنبال می‌شوند تا میزبان هر مرحله بررسی شود."""
        headers = {
            "User-Agent": _WEB_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.pinterest.com/",
        }
        current = url
        cookie = self.engine._get_next_cookie()
        for _ in range(_MAX_REDIRECTS):
            if not self._is_allowed_host(current):
                logger.warning(f"[PINTEREST-MEDIA] ریدایرکت به میزبان غیرمجاز رد شد: {current[:100]}")
                return None
            try:
                resp = self.engine._web_get(current, headers=headers, timeout=10, cookie=cookie, follow_redirects=False)
            except requests.RequestException as e:
                logger.warning(f"[PINTEREST-MEDIA] خطا در دریافت صفحه ({current[:80]}): {e}")
                return None
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    logger.warning("[PINTEREST-MEDIA] ریدایرکت بدون Location")
                    return None
                current = urllib.parse.urljoin(current, location)
                continue
            if cookie and resp.status_code in (401, 403):
                self.engine._mark_cookie_bad(cookie)
                cookie = None                   # همین آدرس یک بار دیگر به‌صورت مهمان
                continue
            if resp.status_code != 200:
                logger.warning(f"[PINTEREST-MEDIA] status={resp.status_code} برای {current[:80]}")
                return None
            return current, resp.text
        logger.warning(f"[PINTEREST-MEDIA] تعداد ریدایرکت‌ها از {_MAX_REDIRECTS} بیشتر شد")
        return None

    # ----------------------------------------------------------- پارس HTML
    @staticmethod
    def _json_blobs(page: str) -> List[Any]:
        blobs = []
        for attrs, body in _SCRIPT_TAG_RE.findall(page):
            if not ("__PWS_" in attrs or "data-relay-response" in attrs or "application/json" in attrs):
                continue
            try:
                blobs.append(json.loads(body))
            except (ValueError, TypeError):
                continue
        return blobs

    @staticmethod
    def _parse_meta(page: str) -> Dict[str, str]:
        """اولین مقدار هر تگ meta (og:* / twitter:* / name) را برمی‌گرداند."""
        meta: Dict[str, str] = {}
        for tag in _META_TAG_RE.findall(page):
            attrs = {m[0].lower(): _html.unescape(m[1] or m[2]) for m in _META_ATTR_RE.findall(tag)}
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content", "").strip()
            if key and content and key not in meta:
                meta[key] = content
        return meta

    @staticmethod
    def _clean_title(text: Any) -> str:
        text = re.sub(r"\s+", " ", _html.unescape(str(text or ""))).strip()
        return re.sub(r"\s*\|\s*Pinterest\s*$", "", text, flags=re.I)

    # ----------------------------------------------------------- استخراج از JSON صفحه
    @staticmethod
    def _node_ids(node: dict) -> set:
        """شناسه‌های ممکنِ یک نود: id / entityId / pinId؛ شناسه‌های base64 مثل «Pin:123» (داده‌های relay) هم رمزگشایی می‌شوند."""
        out = set()
        for key in ("id", "entityId", "pinId", "pin_id"):
            v = node.get(key)
            if v is None or isinstance(v, (dict, list)):
                continue
            v = str(v)
            out.add(v)
            if not v.isdigit() and len(v) >= 8:
                try:
                    dec = base64.b64decode(v + "=" * (-len(v) % 4)).decode("utf-8", "ignore")
                    m = re.search(r"(\d{6,})\s*$", dec)
                    if m:
                        out.add(m.group(1))
                except Exception:
                    pass
        return out

    def _pin_nodes(self, blobs: List[Any], pin_id: str) -> List[dict]:
        """دیکشنری‌هایی که خودِ پینِ مورد نظر هستند (نه پین‌های مرتبط)؛ بر اساس id."""
        nodes = []
        for js in blobs:
            for node in self._walk(js):
                if pin_id in self._node_ids(node) and any(
                        k in node for k in ("images", "videos", "story_pin_data", "video_list", "videoList")):
                    nodes.append(node)
        return nodes

    def _best_video(self, nodes: List[dict]) -> Optional[Dict]:
        mp4, hls = [], []
        for node in nodes:
            for sub in self._walk(node):
                entries = []
                for lst_key in ("video_list", "videoList"):
                    lst = sub.get(lst_key)
                    if isinstance(lst, dict):
                        entries += [(str(name), v) for name, v in lst.items() if isinstance(v, dict)]
                u0 = sub.get("url")
                if isinstance(u0, str) and _is_pin_host(u0):      # هر دیکشنری که خودش آدرس ویدیوی pinimg دارد (نام کلیدها فرق می‌کند)
                    entries.append((u0, sub))
                for name, v in entries:
                    u = str(v.get("url") or "")
                    ext = self._url_ext(u) if u.startswith("http") else ""
                    if not ext:
                        continue
                    q = _QUALITY_RE.search(str(name))
                    entry = {"url": u, "w": _safe_int(v.get("width")), "h": _safe_int(v.get("height")),
                             "q": int(q.group(1)) if q else 0}
                    (mp4 if ext == "mp4" else hls).append(entry)
        rank = lambda e: (e["w"] * e["h"], e["q"])
        if mp4:
            best = max(mp4, key=rank)
            return {"url": best["url"], "width": best["w"], "height": best["h"]}
        if hls:
            best = max(hls, key=rank)
            derived = self._hls_to_mp4(best["url"])
            if derived and self._url_exists(derived):
                return {"url": derived, "width": best["w"], "height": best["h"]}
            logger.warning("[PINTEREST-MEDIA] فقط HLS موجود بود و نسخهٔ mp4 مشتق‌شده معتبر نبود")
        return None

    def _best_image(self, nodes: List[dict]) -> Optional[Dict]:
        cands = []
        for node in nodes:
            for sub in self._walk(node):
                imgs = sub.get("images")
                if not isinstance(imgs, dict):
                    continue
                for key, v in imgs.items():
                    if isinstance(v, dict) and _is_pin_host(str(v.get("url") or "")):
                        w, h = _safe_int(v.get("width")), _safe_int(v.get("height"))
                        order = _IMG_SIZE_KEYS.index(key) if key in _IMG_SIZE_KEYS else len(_IMG_SIZE_KEYS)
                        cands.append((w * h, -order, v["url"], w, h))
        if not cands:
            return None
        cands = [c for c in cands if not _MODERN_EXT_RE.search(c[2])] or cands      # HEIC/AVIF فقط اگر چیز دیگری نبود
        gifs = [c for c in cands if urllib.parse.urlparse(c[2]).path.lower().endswith(".gif")]
        _, _, url, w, h = max(gifs or cands)        # گیف متحرک بر عکس ثابتِ همان پین می‌چربد
        return {"url": self._to_original(url), "width": w, "height": h}

    def _to_original(self, url: str) -> str:
        """به /originals/ ارتقا می‌دهد، ولی فقط اگر آن فایل واقعاً وجود داشته باشد."""
        hd = PinterestEngine._upgrade_to_hd(url)
        if hd == url:
            return url
        parts = urllib.parse.urlsplit(hd)
        stem, ext = os.path.splitext(parts.path)
        tries = [hd]
        if ext.lower() != ".gif":               # گیف همان پسوند خودش را دارد
            for e in (".jpg", ".png", ".webp", ".jpeg"):      # HEIC/AVIF عمداً نیست (تلگرام باز نمی‌کند)
                alt = urllib.parse.urlunsplit(parts._replace(path=stem + e))
                if alt not in tries:
                    tries.append(alt)
        codes = []
        for cand in tries:
            code = self._probe(cand)
            codes.append(f"{os.path.splitext(cand.split('?')[0])[1] or '?'}={code}")
            if code in (200, 206):
                logger.info(f"[PINTEREST-MEDIA] originals پیدا شد ({', '.join(codes)})")
                return cand
        logger.warning(f"[PINTEREST-MEDIA] originals پیدا نشد → همان نسخهٔ کم‌کیفیت ({', '.join(codes)}) | ...{hd[-50:]}")
        return url

    def _media_from_json(self, blobs: List[Any], pin_id: str) -> Optional[Dict]:
        nodes = self._pin_nodes(blobs, pin_id)
        if not nodes:
            return None
        title = ""
        for node in nodes:
            title = self._clean_title(node.get("title") or node.get("grid_title") or node.get("seo_title"))
            if title:
                break
        image = self._best_image(nodes)
        video = self._best_video(nodes)
        if video:
            # ابعاد ویدیو اگر معلوم نبود، نسبت و اندازهٔ تصویر کاور به‌عنوان جایگزین
            w, h = video["width"], video["height"]
            if (w <= 0 or h <= 0) and image:
                w, h = image["width"], image["height"]
            return {"type": "video", "url": video["url"], "title": title, "width": w, "height": h}
        if image:
            return {"type": "photo", "url": image["url"], "title": title,
                    "width": image["width"], "height": image["height"]}
        return None

    # ----------------------------------------------------------- API پینترست (وقتی صفحه فقط عکس ثابت داد)
    def _media_from_api(self, pin_id: str) -> Optional[Dict]:
        """صفحهٔ HTML برای پین‌های ویدیو / گیف گاهی فقط عکس کاور دارد. این‌جا از خودِ API پینترست پرسیده می‌شود:
        اول widgets (بدون CSRF)، اگر جواب نداد PinResource. خروجی همان شکل _media_from_json (photo | video) یا None."""
        sess = self.engine.session
        ref = {"User-Agent": _WEB_UA, "Referer": "https://www.pinterest.com/"}
        try:
            r = sess.get("https://widgets.pinterest.com/v3/pidgets/pins/info/", params={"pin_ids": pin_id},
                         headers=ref, timeout=7)
            if r.status_code == 200:
                media = self._media_from_json([r.json()], pin_id)
                if media:
                    return media
            else:
                logger.info(f"[PINTEREST-MEDIA] widgets status={r.status_code} برای پین {pin_id}")
        except (requests.RequestException, ValueError) as e:
            logger.info(f"[PINTEREST-MEDIA] widgets ناموفق: {e}")
        try:
            csrf = self.engine._web_bootstrap()
            src = f"/pin/{pin_id}/"
            r = self.engine._web_get("https://www.pinterest.com/resource/PinResource/get/",
                         cookie=self.engine._get_next_cookie(),
                         params={"source_url": src,
                                 "data": json.dumps({"options": {"id": pin_id, "field_set_key": "auth_web_main_pin",
                                                                 "noCache": True}, "context": {}})},
                         headers={**ref, "Accept": "application/json, text/javascript, */*, q=0.01",
                                  "X-Requested-With": "XMLHttpRequest", "X-APP-VERSION": "a1b2c3d",
                                  "X-Pinterest-AppState": "active", "X-Pinterest-Source-Url": src,
                                  "X-CSRFToken": csrf}, timeout=8)
            if r.status_code == 200:
                return self._media_from_json([r.json()], pin_id)
            logger.info(f"[PINTEREST-MEDIA] PinResource status={r.status_code} برای پین {pin_id}")
        except (requests.RequestException, ValueError) as e:
            logger.info(f"[PINTEREST-MEDIA] PinResource ناموفق: {e}")
        return None

    @staticmethod
    def _is_animated_still(media: Dict) -> bool:
        """true اگر مدیا ویدیو یا گیف متحرک است (یعنی «بهتر از یک عکس ثابت»)."""
        return media.get("type") == "video" or urllib.parse.urlparse(str(media.get("url") or "")).path.lower().endswith(".gif")

    # ----------------------------------------------------------- استخراج از OpenGraph (Fallback)
    def _media_from_meta(self, meta: Dict[str, str]) -> Optional[Dict]:
        title = self._clean_title(meta.get("og:title") or meta.get("twitter:title"))
        for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
            u = meta.get(key, "")
            ext = self._url_ext(u) if u.startswith("http") else ""
            if ext == "m3u8":
                u = self._hls_to_mp4(u)
                if not u or not self._url_exists(u):
                    continue
            elif ext != "mp4":
                continue
            return {"type": "video", "url": u, "title": title,
                    "width": _safe_int(meta.get("og:video:width")), "height": _safe_int(meta.get("og:video:height"))}
        for key in ("og:image", "og:image:url", "twitter:image:src", "twitter:image"):
            u = meta.get(key, "")
            if u.startswith("http") and _is_pin_host(u):
                return {"type": "photo", "url": self._to_original(u), "title": title,
                        "width": _safe_int(meta.get("og:image:width")),
                        "height": _safe_int(meta.get("og:image:height"))}
        return None

    # ----------------------------------------------------------- متد عمومی
    @staticmethod
    def _canonical_pin_url(url: str) -> str:
        """https://de.pinterest.com/pin/123/sent/?invite_code=… (یا هر زیردامنهٔ منطقه‌ای / slug) → https://www.pinterest.com/pin/123/
        pin.it و لینک‌های غیر /pin/ دست‌نخورده می‌مانند."""
        p = urllib.parse.urlparse(url)
        host = (p.hostname or "").lower()
        if host == "pin.it" or not _PIN_HOST_RE.match(host) or not p.path.startswith("/pin/"):
            return url
        m = _PIN_ID_RE.search(p.path)
        return f"https://www.pinterest.com/pin/{m.group(1)}/" if m else url

    def _media_without_page(self, pin_id: str) -> Optional[Dict]:
        """صفحهٔ HTML نیامد (بلاک/قطعی) ولی شناسهٔ پین را داریم ← مستقیم از API (widgets، بعد PinResource)."""
        media = self._media_from_api(pin_id)
        if media:
            logger.info(f"[PINTEREST-MEDIA] pin={pin_id}: صفحه نیامد؛ مدیا از API گرفته شد ({media.get('type')})")
        return media

    def get_pin_media(self, url: str) -> Optional[Dict]:
        try:
            clean = self._normalize_url(url)
            if not clean:
                logger.warning(f"[PINTEREST-MEDIA] لینک نامعتبر یا غیرپینترستی: {str(url)[:100]!r}")
                return None
            clean = self._canonical_pin_url(clean)

            fetched = self._fetch_page(clean)
            if not fetched:
                m0 = _PIN_ID_RE.search(urllib.parse.urlparse(clean).path)
                media = self._media_without_page(m0.group(1)) if m0 else None
                if not media:
                    return None
                return {"type": media["type"], "url": media["url"], "title": media.get("title") or "",
                        "width": int(media.get("width") or 0), "height": int(media.get("height") or 0)}
            final_url, page = fetched

            m = _PIN_ID_RE.search(urllib.parse.urlparse(final_url).path)
            if not m:
                logger.warning(f"[PINTEREST-MEDIA] آدرس نهایی لینک پین نیست: {final_url[:100]}")
                return None
            pin_id = m.group(1)

            # آدرس‌هایی مثل /pin/ID/sent/?invite_code=... → صفحهٔ اصلی پین
            if not _CANONICAL_PIN_PATH_RE.match(urllib.parse.urlparse(final_url).path):
                again = self._fetch_page(f"https://www.pinterest.com/pin/{pin_id}/")
                if again:
                    page = again[1]

            meta = self._parse_meta(page)
            media = self._media_from_json(self._json_blobs(page), pin_id) or self._media_from_meta(meta)
            if not media:
                logger.warning(f"[PINTEREST-MEDIA] مدیایی برای پین {pin_id} پیدا نشد")
                return None
            if not self._is_animated_still(media):
                # فقط عکس ثابت پیدا شد: شاید پین ویدیو / گیف باشد و صفحه فقط کاورش را داده؛ از API هم بپرس
                n_mp4 = len(re.findall(r"pinimg\.com/videos/[^\"\s\\]+\.mp4", page))
                n_hls = len(re.findall(r"\.m3u8", page))
                better = self._media_from_api(pin_id)
                logger.info(f"[PINTEREST-MEDIA] pin={pin_id}: صفحه عکس ثابت داد | آدرس mp4 در صفحه={n_mp4} m3u8={n_hls} | "
                            f"API→{(better or {}).get('type', 'هیچ')}"
                            f"{' (گیف)' if better and better['type'] == 'photo' and self._is_animated_still(better) else ''}")
                if better and self._is_animated_still(better):
                    better["title"] = better.get("title") or media["title"]
                    media = better
            if not media["title"]:
                media["title"] = self._clean_title(meta.get("og:title") or meta.get("twitter:title"))

            logger.info(f"[PINTEREST-MEDIA] pin={pin_id} | نوع={media['type']} | "
                        f"ابعاد={media['width']}x{media['height']} | آدرس=...{media['url'][-60:]}")
            return {"type": media["type"], "url": media["url"], "title": media["title"],
                    "width": int(media["width"]), "height": int(media["height"])}
        except Exception as e:
            logger.error(f"[PINTEREST-MEDIA] خطای غیرمنتظره: {e}", exc_info=True)
            return None


_pinterest_media = PinterestMediaExtractor(_pinterest_engine)


def get_pin_media(url: str) -> Optional[Dict]:
    """
    ورودی: لینک کوتاه (https://pin.it/...) یا کامل (https://www.pinterest.com/pin/...)
    خروجی: {"type": "photo" | "video", "url": str, "title": str, "width": int, "height": int}
           یا None در صورت لینک اشتباه/شکست استخراج (خطا لاگ می‌شود).
    """
    return _pinterest_media.get_pin_media(url)


# ===========================================================================
# پیدا کردن لینک داخل متن + دانلود فایل
# ===========================================================================
_PIN_URL_RE = re.compile(
    r"(?<![\w.@/-])(?:https?://)?"
    r"(?:(?:[a-z0-9-]+\.)*pinterest\.[a-z]{2,3}(?:\.[a-z]{2})?/pin/|pin\.it/)"
    r"[^\s<>\"'«»]+",
    re.I,
)
_URL_TRAIL = ".,;:!?)]}»…،؛؟"


def extract_pin_url(text: Any) -> str:
    """اولین لینک پینترست (pinterest.*/pin/... یا pin.it/...) داخل متن؛ اگر نبود رشتهٔ خالی."""
    if not isinstance(text, str) or not text:
        return ""
    m = _PIN_URL_RE.search(text)
    if not m:
        return ""
    url = m.group(0).rstrip(_URL_TRAIL)
    return url if PinterestMediaExtractor._normalize_url(url) else ""


_CT_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "video/mp4": "mp4",
}
_DL_HEADERS = {
    "User-Agent": _WEB_UA,
    "Referer": "https://www.pinterest.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,video/mp4,*/*;q=0.8",
}


def _download_candidates(url: str) -> List[str]:
    """originals بسته بود → همان عکس با اندازهٔ 1200x و 736x."""
    out = [url]
    if "/originals/" in url:
        out += [url.replace("/originals/", "/1200x/", 1), url.replace("/originals/", "/736x/", 1)]
    return out


def download_pin_media(media: Dict, max_bytes: int = 45 * 1024 * 1024) -> Optional[Dict]:
    """فایل خروجی get_pin_media را دانلود می‌کند (با همان session/پروکسی).
    خروجی: {"data": bytes, "content_type": str, "ext": str, "url": str} یا None (خطا / بزرگ‌تر از max_bytes)."""
    if not isinstance(media, dict) or not media.get("url"):
        return None
    want_video = media.get("type") == "video"
    for u in _download_candidates(str(media["url"])):
        if not _is_pin_host(u):
            continue
        r = None
        try:
            r = _pinterest_engine.session.get(u, headers=_DL_HEADERS, timeout=(6, 40), stream=True)
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status_code != 200 or not (ct.startswith("image/") or ct.startswith("video/")):
                logger.info(f"[PINTEREST-DL] رد شد status={r.status_code} type={ct or '-'} | ...{u[-50:]}")
                continue
            if want_video and not ct.startswith("video/"):
                logger.info(f"[PINTEREST-DL] انتظار ویدیو بود ولی {ct} آمد | ...{u[-50:]}")
                continue
            clen = _safe_int(r.headers.get("Content-Length"))
            if clen > max_bytes:
                logger.warning(f"[PINTEREST-DL] فایل بزرگ است: {clen // 1024}KB > {max_bytes // 1024}KB")
                return None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    logger.warning("[PINTEREST-DL] حجم فایل از سقف گذشت")
                    return None
            if len(buf) < 1024:
                continue
            ext = _CT_EXT.get(ct) or os.path.splitext(urllib.parse.urlparse(u).path)[1].lstrip(".").lower() or ("mp4" if want_video else "jpg")
            return {"data": bytes(buf), "content_type": ct, "ext": ext, "url": u}
        except requests.RequestException as e:
            logger.warning(f"[PINTEREST-DL] خطا: {e}")
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
    return None
