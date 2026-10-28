# -*- coding: utf-8 -*-
"""
neshan_service.py — جستجوی مکان با وب‌سرویس نشان (Neshan) و ارسال لوکیشن/ونیوی نیتیو تلگرام.

دستورها: /map  /لوکیشن  /نقشه   (در پیوی و گروه؛ با @یوزرنیم ربات هم کار می‌کند)
مثال:    /map میدان آزادی

متغیرهای محیطی (اختیاری):
  NESHAN_TOKEN        توکن Bearer (پیش‌فرض: توکن داخل همین فایل — منقضی می‌شود؛ از env عوض کن)
  NESHAN_TIMEOUT_SEC  تایم‌اوت درخواست (پیش‌فرض ۸)
  NESHAN_ENABLED=0    غیرفعال‌سازی کامل

اتصال به ربات:
    import neshan_service
    neshan_service.register_neshan_handlers(bot, spam_check=is_spam,
                                            group_blocked=fj_group_blocked,
                                            pv_blocked=_pv_fj_blocked,
                                            base_url_fn=lambda: CHART_PREVIEW_BASE_URL)

لینک‌های «باز کردن در نشان / گوگل مپس»: تلگرام در دکمه‌ها فقط https قبول می‌کند، پس اگر base_url_fn آدرس https عمومی
ربات را بدهد، دکمه‌ها به صفحهٔ واسط  {base}/go/map  می‌روند (render_go_page) که کاربر را مستقیم داخل اپ باز می‌کند
(iOS: scheme اپ ، Android: intent با package). اگر آدرس عمومی نبود، لینک وب مثل قبل استفاده می‌شود.

از بیرون (مثلاً ایجنت ربات): agent_send_map(chat_id, reply_to, uid, query) — همان جستجو + ارسال لوکیشن/لیست انتخاب.
"""

import base64
import gzip
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
import zlib

import requests

logger = logging.getLogger(__name__)

# ----------------------------- config -----------------------------
NESHAN_URL = "https://geocoder.neshanmap.ir/suggestion/v2"
NESHAN_TIMEOUT_SEC = float(os.environ.get("NESHAN_TIMEOUT_SEC", "8") or 8)
NESHAN_ENABLED = os.environ.get("NESHAN_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")

# توکن اپ نشان (iOS). exp داخل JWT ≈ ۲۹ اکتبر ۲۰۲۶ است؛ بعدش 401 می‌گیری و باید NESHAN_TOKEN را عوض کنی.
_DEFAULT_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
    "eyJleHAiOjE3OTMzMTU3OTksImp0aSI6IjM4NjI3MjUwMzQ1MiwFWF0joxNzkzMDU2NTk1LCJ1c24iOiJCNjo2OUg4QUw_PEFBQURTSIhJV0xLSV1JT19fxk9SWEzCj8KSwozCmMKLwpXCjsKOWpPCpcKVXmFnZnhCMckGwpXCnsKiZcKbwqjCpylsInBpZCI6MTc0ODkyMjI1MTEsInRwIjpYWxzZSwiZGkljozNzYwNjA4MzgzN30."
    "-gFQmGfV-JA_WzugH2MrXYizNc-4-e9x9fvDTpuzExva6ZlumQFmaepiTuSqFixs"
)

# API رسمی نشان (پیشنهادی): کلید سرویس را از platform.neshan.org بگیر و در env بگذار → NESHAN_API_KEY
NESHAN_OFFICIAL_URL = "https://api.neshan.org/v1/search"

DEFAULT_LAT = 35.6997   # مرکز تهران
DEFAULT_LNG = 51.3380

# بودجهٔ کل زمان یک جستجو (AI + چند تلاش پشت‌سرهم)
NESHAN_TOTAL_BUDGET_SEC = float(os.environ.get("NESHAN_TOTAL_BUDGET_SEC", "20") or 20)
NESHAN_MAX_TRIES = 3

COMMANDS = ("map", "لوکیشن", "نقشه")

HELP_TEXT = (
    "📍 بعد از دستور، اسم مکان رو بنویس.\n"
    "مثال:\n<code>/map میدان آزادی</code>\n<code>/نقشه برج میلاد</code>\n<code>/لوکیشن کافه نزدیک ونک</code>"
)

_session = requests.Session()
_session_lock = threading.Lock()


def _token() -> str:
    return (os.environ.get("NESHAN_TOKEN") or _DEFAULT_TOKEN).strip()


def _api_key() -> str:
    return (os.environ.get("NESHAN_API_KEY") or "").strip()


NESHAN_PWA_URL = os.environ.get("NESHAN_PWA_URL", "https://neshan.org/maps/pwa-api/neshan-search/suggestion-v2")
_WEB_UUID = "web_" + str(uuid.uuid4())


def _pwa_headers() -> dict:
    """هدرهای نسخهٔ وب (PWA) نشان: بدون توکن، پاسخ JSON."""
    return {
        "X-Client-Version": "2038",
        "Content-Type": "application/json",
        "uuid": _WEB_UUID,
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
                       "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"),
        "Referer": "https://neshan.org/maps/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fa;q=1.0",
    }


def _pwa_url(term: str, lat: float, lng: float) -> str:
    payload = {"term": term, "center": {"x": float(lng), "y": float(lat)},
               "location": None, "uuid": _WEB_UUID}
    # معادل فرانت‌اند وب: btoa(encodeURIComponent(JSON.stringify(payload)))
    raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    quoted = urllib.parse.quote(raw_json, safe="!*'()")   # همان کاراکترهای امنِ encodeURIComponent
    b64 = base64.b64encode(quoted.encode("utf-8")).decode("ascii")
    # + و / و = در Base64 باید در query هم انکود شوند، وگرنه '+' سمت سرور فاصله می‌شود
    return NESHAN_PWA_URL + "?data=" + urllib.parse.quote(b64, safe="")


def _headers_json() -> dict:
    """نسخهٔ اول: درخواست صریح JSON، بدون هدرهای اختصاصی اپ iOS."""
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fa;q=1.0",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    }


def _b64_json(b: bytes):
    """متن Base64 (که داخلش JSON است؛ شاید با percent-encoding) → آبجکت JSON."""
    s = b.strip().replace(b"-", b"+").replace(b"_", b"/")
    s += b"=" * (-len(s) % 4)
    t = base64.b64decode(s).decode("utf-8")
    for cand in (t, urllib.parse.unquote(t)):
        try:
            return json.loads(cand)
        except ValueError:
            pass
    raise ValueError("base64 decoded but not JSON")


_DEBUG_BODY = os.environ.get("NESHAN_DEBUG_BODY", "0") == "1"   # دیباگ بدنهٔ PWA؛ فقط با NESHAN_DEBUG_BODY=1


def _b64_items_loose(b: bytes):
    """پاسخ PWA گاهی Base64 است که ابتدایش خراب/بی‌ترازِ ۴ است. هر چهار ترازِ ممکن را امتحان می‌کنیم و
    آبجکت‌های سالم را بیرون می‌کشیم. آیتم‌های خرابِ ابتدای پاسخ از دست می‌روند."""
    s = re.sub(rb"[^A-Za-z0-9+/_-]", b"", b.strip()).replace(b"-", b"+").replace(b"_", b"/")
    if _DEBUG_BODY:   # موقت: کل بدنه در چند خط تا لاگ بریده نشود
        for i in range(0, min(len(s), 6300), 700):
            logger.info("[NESHAN-PWA-BODY %d/%d] %s", i, len(s), s[i:i + 700].decode("ascii", "replace"))
    dec = json.JSONDecoder()
    best = []
    for off in range(4):
        t = s[off:]
        t = t[:len(t) // 4 * 4]
        try:
            txt = base64.b64decode(t).decode("utf-8", "ignore")
        except Exception:
            continue
        found, i = [], 0
        while True:
            i = txt.find('{"', i)
            if i < 0:
                break
            try:
                obj, end = dec.raw_decode(txt, i)
            except ValueError:
                i += 1
                continue
            if isinstance(obj, dict) and ("title" in obj or "id" in obj):
                found.append(obj)
            i = end
        if len(found) > len(best):
            best = found
    if not best:
        raise ValueError("no intact items in base64 body")
    logger.info("[NESHAN] پاسخ Base64 با پارس تحمل‌پذیر باز شد (%d آیتم سالم؛ آیتم‌های ابتدایی ممکن است خراب باشند)", len(best))
    return {"items": best}


def _unpack(raw: bytes):
    """بایت‌های خام → JSON (تست gzip / zlib / raw-deflate / base64 / brotli) یا None."""
    def _load(b):
        return json.loads(b.decode("utf-8"))
    for fn in (lambda b: _load(b),
               lambda b: _load(gzip.decompress(b)),
               lambda b: _load(zlib.decompress(b)),
               lambda b: _load(zlib.decompress(b, -zlib.MAX_WBITS)),
               lambda b: _b64_json(b),
               lambda b: _b64_items_loose(b)):
        try:
            return fn(raw)
        except Exception:
            pass
    try:
        import brotli  # اختیاری
        return _load(brotli.decompress(raw))
    except Exception:
        return None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "vc": "898",
        "os": "ios",
        "Accept": "*/*",
        "Accept-Language": "fa;q=1.0",
        "Content-Type": "application/json",
        "User-Agent": "Neshan/898 CFNetwork/Darwin",
    }


# ----------------------------- parsing -----------------------------
def _coords(item: dict):
    """(lat, lng) یا None. نشان: x = طول جغرافیایی (lng) ، y = عرض جغرافیایی (lat)."""
    loc = None
    md = item.get("metadata")
    for src in (item, md if isinstance(md, dict) else {}):   # وب نشان: item.metadata.location
        for key in ("location", "point", "center", "geometry", "position"):
            v = src.get(key)
            if isinstance(v, dict):
                loc = v
                break
        if loc is not None:
            break
    if loc is None:
        return None
    try:
        if "x" in loc and "y" in loc:
            lng, lat = float(loc["x"]), float(loc["y"])
        elif "longitude" in loc and "latitude" in loc:
            lng, lat = float(loc["longitude"]), float(loc["latitude"])
        elif "lng" in loc and "lat" in loc:
            lng, lat = float(loc["lng"]), float(loc["lat"])
        else:
            return None
    except (TypeError, ValueError):
        return None
    if abs(lat) > 90 and abs(lng) <= 90:   # جابه‌جایی احتمالی x/y
        lat, lng = lng, lat
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _address(item: dict, title: str) -> str:
    addr = _s(item.get("address")) or _s(item.get("subtitle"))
    if addr:
        return addr
    parts = [_s(item.get(k)) for k in ("region", "district", "neighbourhood", "neighborhood", "municipality", "city", "state")]
    parts = [p for p in parts if p and p != title]
    return "، ".join(dict.fromkeys(parts)) or title


def _pick(items: list):
    """اولین نتیجهٔ دارای مختصات معتبر (ترتیب نشان = ربط‌ترین اول)."""
    for it in items:
        if not isinstance(it, dict):
            continue
        c = _coords(it)
        title = _s(it.get("title") or it.get("name"))
        if c is None or not title:
            continue
        return {"title": title[:250], "address": _address(it, title)[:250], "latitude": c[0], "longitude": c[1]}
    return None


def _pick_all(items: list, limit: int = 5) -> list:
    """همهٔ نتیجه‌های دارای مختصات (بدون تکرار) به ترتیب ربط، حداکثر limit تا."""
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        c = _coords(it)
        title = _s(it.get("title") or it.get("name"))
        if c is None or not title:
            continue
        key = (title, round(c[0], 4), round(c[1], 4))
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title[:250], "address": _address(it, title)[:250], "latitude": c[0], "longitude": c[1]})
        if len(out) >= limit:
            break
    return out


# ----------------------------- search -----------------------------
def _search_items(query: str, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG,
                  timeout: float | None = None) -> list | None:
    """جستجو در نشان → لیست خام آیتم‌ها یا None (خطا/تایم‌اوت)."""
    term = (query or "").strip()
    if not term or not NESHAN_ENABLED:
        return None
    tmo = min(NESHAN_TIMEOUT_SEC, timeout) if timeout else NESHAN_TIMEOUT_SEC
    key = _api_key()
    try:
        with _session_lock:
            if key:   # API رسمی (GET + Api-Key)
                r = _session.get(NESHAN_OFFICIAL_URL,
                                 params={"term": term, "lat": float(lat), "lng": float(lng)},
                                 headers={"Api-Key": key, "Accept": "application/json"}, timeout=tmo)
            else:     # بدون کلید رسمی: اول اندپوینت وب (PWA، بدون توکن)، بعد اپ iOS
                try:
                    r = _session.get(_pwa_url(term, lat, lng), headers=_pwa_headers(), timeout=tmo)
                    if r.status_code >= 400 or _unpack(r.content) is None:
                        logger.info("[NESHAN] PWA جواب قابل‌استفاده نداد (status=%s ctype=%r)؛ می‌رویم سراغ اندپوینت iOS",
                                    r.status_code, r.headers.get("Content-Type"))
                        try:   # دیباگ موقت: دقیقاً چه چیزی برگشته؟ (بعد از روشن‌شدن ماجرا حذفش کن)
                            import importlib.util as _iu
                            _has_br = bool(_iu.find_spec("brotli") or _iu.find_spec("brotlicffi"))
                        except Exception:
                            _has_br = None
                        logger.info("[NESHAN-PWA-DEBUG] enc=%r len=%d brotli=%s head=%r",
                                    r.headers.get("Content-Encoding"), len(r.content), _has_br, r.content[:300])
                        r = None
                except requests.RequestException as e:
                    logger.info("[NESHAN] PWA خطا داد (%s)؛ می‌رویم سراغ اندپوینت iOS", type(e).__name__)
                    r = None
            if r is None:   # اندپوینت خصوصی اپ iOS (ناپایدار): اول هدر JSON، بعد هدر اصلی اپ
                payload = {
                    "term": term,
                    "location": {"x": float(lng), "y": float(lat)},
                    "center": {"x": float(lng), "y": float(lat)},
                }
                r = None
                for hdrs in (_headers_json(), _headers()):
                    r = _session.post(NESHAN_URL, json=payload, headers=hdrs, timeout=tmo)
                    if r.status_code >= 400 or "json" in (r.headers.get("Content-Type") or "").lower() \
                            or _unpack(r.content) is not None:
                        break
        src = "official" if key else ("pwa" if "pwa" in (r.url or "") else "private")
        if r.status_code in (401, 403):
            logger.warning("[NESHAN] کلید/توکن رد شد (%s, %s)؛ NESHAN_API_KEY/NESHAN_TOKEN را چک کن", r.status_code, src)
            return None
        if r.status_code >= 400:
            logger.warning("[NESHAN] HTTP %s (%s) body=%r", r.status_code, src, (r.text or "")[:200])
            return None
        try:
            data = r.json()
        except ValueError:
            data = _unpack(r.content)
            if data is not None:
                logger.info("[NESHAN] پاسخ با فشرده‌سازی خام باز شد (%s)", src)
        if data is None:
            # دیباگ: دقیقاً چی برگشته؟ (خالی؟ HTML؟ باینری؟)
            if not key:
                logger.error("[NESHAN] اندپوینت خصوصی iOS پاسخ رمزشده/باینری می‌دهد و قابل‌استفاده نیست؛ "
                             "NESHAN_API_KEY (کلید سرویس platform.neshan.org) را در env بگذار.")
            logger.warning("[NESHAN] پاسخ JSON نیست (%s): status=%s ctype=%r enc=%r len=%d head=%r",
                           src, r.status_code, r.headers.get("Content-Type"), r.headers.get("Content-Encoding"),
                           len(r.content), r.content[:80])
            return None
    except requests.Timeout:
        logger.warning("[NESHAN] timeout بعد از %.0fs", NESHAN_TIMEOUT_SEC)
        return None
    except requests.RequestException as e:
        logger.warning("[NESHAN] درخواست ناموفق: %s: %s", type(e).__name__, e)
        return None
    except Exception as e:  # هر خطای پیش‌بینی‌نشده نباید ربات را بیندازد
        logger.warning("[NESHAN] خطای غیرمنتظره: %s: %s", type(e).__name__, e)
        return None

    items = []
    if isinstance(data, dict):
        for key in ("items", "suggestions", "results"):
            v = data.get(key)
            if isinstance(v, list) and v:
                items = v
                break
    elif isinstance(data, list):
        items = data
    return items


def search_neshan_location(query: str, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG,
                           timeout: float | None = None) -> dict | None:
    """بهترین نتیجه → {"title","address","latitude","longitude"} یا None."""
    items = _search_items(query, lat, lng, timeout)
    return _pick(items) if items else None


def search_neshan_results(query: str, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG,
                          timeout: float | None = None, limit: int = 5) -> list:
    """چند نتیجهٔ اول (دارای مختصات) → لیست دیکشنری؛ خالی اگر چیزی نبود."""
    items = _search_items(query, lat, lng, timeout)
    return _pick_all(items, limit) if items else []


# ----------------------------- آدرس طولانی → کلیدواژه -----------------------------
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_NUM = r"[\d۰-۹٠-٩]+"
# پلاک/واحد/طبقه/کدپستی/بلوک: نشان با این‌ها نتیجه نمی‌دهد
_NOISE_RES = [
    re.compile(rf"(?:پلاک|پ)\s*[:：]?\s*{_NUM}(?:\s*(?:مکرر|الف|ب))?"),
    re.compile(rf"واحد\s*[:：]?\s*{_NUM}"),
    re.compile(rf"طبقه\s*[:：]?\s*(?:{_NUM}|[\u0600-\u06FF]+)"),
    re.compile(rf"(?:ط|طب)\s*[:：]?\s*{_NUM}"),
    re.compile(rf"کد\s*پستی\s*[:：]?\s*{_NUM}"),
    re.compile(rf"بلوک\s*[:：]?\s*\S+"),
    re.compile(r"\b\d{10}\b"),
]
_ABBR = {"خ": "خیابان", "ک": "کوچه", "م": "میدان", "بل": "بلوار", "بلو": "بلوار", "ب": "بلوار", "ا": "اتوبان", "اتو": "اتوبان"}
_FILLER = {"بین", "روبروی", "روبرو", "جنب", "نبش", "قبل", "بعد", "از", "به", "طرف", "سمت", "داخل", "انتهای", "ابتدای", "و", "در"}


def _norm(text: str) -> str:
    t = (text or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"\s+", " ", t).strip()


def clean_address_rules(text: str, max_words: int = 8) -> str:
    """فالبک بدون AI: حذف پلاک/واحد/طبقه/کدپستی، باز کردن مخفف‌ها، کوتاه‌کردن."""
    t = _norm(text)
    for rx in _NOISE_RES:
        t = rx.sub(" ", t)
    t = re.sub(r"[()\[\]{}،,;:؛\-–—/\\.]+", " ", t)
    words = []
    for w in t.split():
        w = _ABBR.get(w, w)
        if re.fullmatch(_NUM, w) and len(w) >= 5:   # عدد بلند (کدپستی و...) حذف؛ «۱۷ شهریور» می‌ماند
            continue
        words.append(w)
    # کلمات ربط/جهت را از ابتدای جمله نگه نمی‌داریم، ولی وسط جمله می‌ماند تا اسم‌ها از هم نپرند
    while words and words[0] in _FILLER:
        words.pop(0)
    return " ".join(words[:max_words]).strip()


def _looks_long(q: str) -> bool:
    n = _norm(q)
    return len(n.split()) > 4 or len(n) > 35 or bool(re.search(r"پلاک|واحد|طبقه|کد\s*پستی|[\d۰-۹]{3,}", n))


def _queries_for(query: str, keyword_fn) -> list:
    """لیست عبارت‌های جستجو به ترتیب تلاش (حداکثر NESHAN_MAX_TRIES، بدون تکرار)."""
    q = _norm(query)
    out = []

    def add(x):
        x = _norm(x)
        if x and x not in out:
            out.append(x)

    if not _looks_long(q):
        add(q)                                   # کوتاه و تمیز: مستقیم
    else:
        if keyword_fn:
            try:
                for k in (keyword_fn(q) or []):  # AI: ۱ تا ۲ عبارت، دقیق ← کلی
                    add(k)
            except Exception as e:
                logger.warning("[NESHAN] keyword_fn خطا: %s: %s", type(e).__name__, e)
        add(clean_address_rules(q))              # فالبک قاعده‌محور
    ruled = clean_address_rules(q, max_words=4)  # آخرین تلاش: ۴ کلمهٔ اول
    add(ruled)
    return out[:NESHAN_MAX_TRIES]


def search_neshan_smart(query: str, keyword_fn=None, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG) -> dict | None:
    """آدرس طولانی را به کلیدواژه تبدیل می‌کند (keyword_fn → لیست عبارت) و به ترتیب جستجو می‌کند؛
    اولین نتیجهٔ معتبر برگردانده می‌شود. keyword_fn(text)->list[str] اختیاری (مثلاً AI)."""
    t0 = time.time()
    for q in _queries_for(query, keyword_fn):
        left = NESHAN_TOTAL_BUDGET_SEC - (time.time() - t0)
        if left < 1.0:
            break
        hit = search_neshan_location(q, lat, lng, timeout=left)
        if hit:
            hit["query_used"] = q
            return hit
        logger.info("[NESHAN] بدون نتیجه برای %r", q)
    return None


def search_neshan_smart_many(query: str, keyword_fn=None, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG,
                             limit: int = 5) -> list:
    """مثل search_neshan_smart ولی چند نتیجه برمی‌گرداند (برای دکمه‌های انتخاب)."""
    t0 = time.time()
    for q in _queries_for(query, keyword_fn):
        left = NESHAN_TOTAL_BUDGET_SEC - (time.time() - t0)
        if left < 1.0:
            break
        hits = search_neshan_results(q, lat, lng, timeout=left, limit=limit)
        if hits:
            return hits
        logger.info("[NESHAN] بدون نتیجه برای %r", q)
    return []


# ----------------------------- لینک‌ها و حالت انتخاب -----------------------------
NESHAN_MAX_CHOICES = max(1, min(8, int(os.environ.get("NESHAN_MAX_CHOICES", "5") or 5)))
# قالب لینک وب نشان: {lat} و {lng}. «0» یا خالی = دکمهٔ نشان نمایش داده نشود.
_NESHAN_LINK_TMPL = os.environ.get("NESHAN_LINK_TEMPLATE", "https://neshan.org/maps/@{lat},{lng},17.00z").strip()
# شناسهٔ پکیج اپ‌ها برای intent اندروید (اگر پکیج اشتباه/نصب‌نشده باشد، intent خودش به لینک وب برمی‌گردد)
NESHAN_ANDROID_PKG = os.environ.get("NESHAN_ANDROID_PKG", "org.rajman.neshan.traffic.tehran.navigator").strip()
GMAPS_ANDROID_PKG = "com.google.android.apps.maps"
_PENDING: dict = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL = 1800
_PENDING_MAX = 300
_SEEN_MSGS: dict = {}          # (chat_id, message_id) -> ts ؛ جلوگیری از پردازش دوبارهٔ یک پیام /map
_SEEN_LOCK = threading.Lock()


def _neshan_link(lat: float, lng: float) -> str:
    """لینک وب نشان (فال‌بک وقتی اپ نصب نیست)."""
    if not _NESHAN_LINK_TMPL or _NESHAN_LINK_TMPL == "0":
        return ""
    return _NESHAN_LINK_TMPL.format(lat=f"{lat:.6f}", lng=f"{lng:.6f}")


def _google_link(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


def _web_link(app: str, lat: float, lng: float) -> str:
    if app == "neshan":
        return _neshan_link(lat, lng) or f"https://nshn.ir/?lat={lat:.6f}&lng={lng:.6f}"
    return _google_link(lat, lng)


def _go_link(app: str, hit: dict, base: str) -> str:
    """لینک https صفحهٔ واسط روی سرور خود ربات؛ همان صفحه کاربر را داخل اپ باز می‌کند."""
    q = urllib.parse.urlencode({"app": app, "lat": f"{hit['latitude']:.6f}", "lng": f"{hit['longitude']:.6f}",
                                "t": (hit.get("title") or "")[:80]})
    return f"{base.rstrip('/')}/go/map?{q}"


def render_go_page(app: str, lat: float, lng: float, title: str = "", user_agent: str = "") -> str:
    """HTML صفحهٔ واسط: روی موبایل اپ نقشه را باز می‌کند و اگر اپ نبود/باز نشد، به وب می‌رود.
    iOS: neshan://?destination=lat,lng  و  comgooglemaps://?q=lat,lng
    Android: intent://...#Intent;package=...;S.browser_fallback_url=...;end  (اپ نبود → خودکار وب)
    دسکتاپ: مستقیم وب."""
    import html as _html
    app = "google" if (app or "").strip().lower() in ("google", "gmaps", "g") else "neshan"
    lat = max(-90.0, min(90.0, float(lat)))
    lng = max(-180.0, min(180.0, float(lng)))
    ua = (user_agent or "").lower()
    ios = any(k in ua for k in ("iphone", "ipad", "ipod"))
    android = "android" in ua
    web = _web_link(app, lat, lng)
    ll = f"{lat:.6f},{lng:.6f}"
    native = ""
    if app == "google":
        app_name = "گوگل مپس"
        if ios:
            native = f"comgooglemaps://?q={ll}&center={ll}&zoom=17"
        elif android:
            native = ("intent://www.google.com/maps/search/?api=1&query=" + ll + "#Intent;scheme=https;package="
                      + GMAPS_ANDROID_PKG + ";S.browser_fallback_url=" + urllib.parse.quote(web, safe="") + ";end")
    else:
        app_name = "نشان"
        if ios:
            native = f"neshan://?destination={ll}"
        elif android:
            native = ("intent://nshn.ir/?lat=%.6f&lng=%.6f#Intent;scheme=https;package=%s;S.browser_fallback_url=%s;end"
                      % (lat, lng, NESHAN_ANDROID_PKG, urllib.parse.quote(web, safe="")))
    label = _html.escape((title or "").strip()[:80]) or "مکان"
    js_native = json.dumps(native).replace("</", "<\\/")
    js_web = json.dumps(web).replace("</", "<\\/")
    btn_native = (f'<a class="a" href="{_html.escape(native, quote=True)}">باز کردن در اپ {app_name}</a>' if native else "")
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{label}</title>
<style>
body{{font-family:system-ui,-apple-system,Tahoma,sans-serif;background:#0f172a;color:#e5e7eb;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center}}
.c{{padding:24px;width:100%;max-width:420px;box-sizing:border-box}}
h1{{font-size:18px;margin:0 0 6px}}p{{opacity:.75;margin:0 0 18px;font-size:14px}}
a{{display:block;padding:14px;border-radius:12px;margin:10px 0;text-decoration:none;font-weight:600}}
.a{{background:#2563eb;color:#fff}}.b{{background:#1f2937;color:#e5e7eb}}
</style></head><body><div class="c">
<h1>{label}</h1><p>در حال باز کردن {app_name}…</p>
{btn_native}
<a class="b" href="{_html.escape(web, quote=True)}">باز کردن در مرورگر (وب)</a>
</div>
<script>
(function(){{
  var N = {js_native}, W = {js_web}, left = false;
  document.addEventListener("visibilitychange", function(){{ if (document.hidden) left = true; }});
  window.addEventListener("pagehide", function(){{ left = true; }});
  if (!N) {{ location.replace(W); return; }}
  setTimeout(function(){{ location.href = N; }}, 60);
  setTimeout(function(){{ if (!left) location.replace(W); }}, 2600);
}})();
</script></body></html>"""


def _pending_put(entry: dict) -> str:
    now = time.time()
    with _PENDING_LOCK:
        for k in [k for k, v in _PENDING.items() if now - v["ts"] > _PENDING_TTL]:
            _PENDING.pop(k, None)
        while len(_PENDING) >= _PENDING_MAX:
            _PENDING.pop(min(_PENDING, key=lambda k: _PENDING[k]["ts"]), None)
        token = uuid.uuid4().hex[:8]
        entry["ts"] = now
        _PENDING[token] = entry
    return token


def _pending_get(token: str):
    with _PENDING_LOCK:
        e = _PENDING.get(token)
        if e and time.time() - e["ts"] > _PENDING_TTL:
            _PENDING.pop(token, None)
            return None
        return e


def _pending_pop(token: str):
    with _PENDING_LOCK:
        return _PENDING.pop(token, None)


def _seen_before(chat_id, message_id) -> bool:
    """True اگر همین پیام در ۱۲۰ ثانیهٔ اخیر یک بار پردازش شده (دلیل: تحویل دوباره/دو ترد)."""
    now = time.time()
    key = (chat_id, message_id)
    with _SEEN_LOCK:
        for k in [k for k, ts in _SEEN_MSGS.items() if now - ts > 120]:
            _SEEN_MSGS.pop(k, None)
        if key in _SEEN_MSGS:
            return True
        _SEEN_MSGS[key] = now
        return False


# ----------------------------- telegram -----------------------------

_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
_RLM = "\u200f"   # علامت راست‌به‌چپ؛ برای اینکه خط‌ها در تلگرام درست تراز بشوند


def _num_emoji(n: int) -> str:
    return _NUM_EMOJI[n - 1] if 1 <= n <= len(_NUM_EMOJI) else str(n)


def _choices_text(query: str, results: list) -> str:
    """متن پیام انتخاب: هر نتیجه با شماره، اسم (bold) و آدرس/مشخصات در خط بعد."""
    import html as _h
    lines = [f"{_RLM}🔎 چند نتیجه برای «{_h.escape(query[:60])}» پیدا کردم؛ شمارهٔ مورد نظرت رو بزن:", ""]
    for i, r in enumerate(results, 1):
        title = _h.escape(r["title"][:80])
        addr = (r.get("address") or "").strip()
        lines.append(f"{_RLM}{_num_emoji(i)} <b>{title}</b>")
        if addr and addr != r["title"]:
            lines.append(f"{_RLM}📍 {_h.escape(addr[:140])}")
        else:   # آدرس اضافه‌ای نبود؛ مختصات تا نتیجه‌ها از هم قابل‌تشخیص باشند
            lines.append(f"{_RLM}📍 {r['latitude']:.4f}, {r['longitude']:.4f}")
        lines.append("")
    return "\n".join(lines).rstrip()[:4000]

_RUNNER = {"fn": None}


def agent_send_map(chat_id, reply_to, uid, query: str) -> dict:
    """برای ایجنت/کدهای دیگر: جستجوی نشان + ارسال لوکیشن (یک نتیجه) یا لیست انتخاب (چند نتیجه).
    برمی‌گرداند {"kind": "single"|"list"|"none"|"error", "titles": [...]} — خودش پیام می‌فرستد."""
    fn = _RUNNER.get("fn")
    if fn is None:
        return {"kind": "error", "titles": []}
    try:
        return fn(chat_id, reply_to, uid, (query or "").strip()[:400])
    except Exception as e:
        logger.warning("[NESHAN] agent_send_map خطا: %s: %s", type(e).__name__, e)
        return {"kind": "error", "titles": []}


def _parse(text: str, bot_username: str = ""):
    """(منطبق؟، عبارت جستجو) برای «/map x» ، «/نقشه@bot x»."""
    t = (text or "").replace("\u200c", " ").strip()
    if not t.startswith("/"):
        return False, ""
    parts = t[1:].split(None, 1)
    if not parts:
        return False, ""
    cmd, _, mention = parts[0].partition("@")
    if cmd.lower() not in COMMANDS:
        return False, ""
    if mention and bot_username and mention.lower() != bot_username.lower():
        return False, ""
    return True, (parts[1].strip() if len(parts) > 1 else "")


def register_neshan_handlers(bot, spam_check=None, group_blocked=None, pv_blocked=None, bot_username_fn=None,
                             keyword_fn=None, base_url_fn=None):
    """هندلر /map /لوکیشن /نقشه را روی bot ثبت می‌کند.
    spam_check(chat_id)->bool ؛ group_blocked(chat)->bool ؛ pv_blocked(user)->bool ؛
    keyword_fn(text)->list[str] (تبدیل آدرس طولانی به کلیدواژه با AI) ؛
    base_url_fn()->str آدرس https عمومی ربات برای لینک‌های داخل-اپ — همه اختیاری."""
    if not NESHAN_ENABLED:
        logger.info("[NESHAN] غیرفعال (NESHAN_ENABLED=0)")
        return

    def _uname() -> str:
        try:
            return (bot_username_fn() if bot_username_fn else getattr(bot.get_me(), "username", "")) or ""
        except Exception:
            return ""

    def _base() -> str:
        try:
            b = ((base_url_fn() if base_url_fn else "") or "").strip().rstrip("/")
        except Exception:
            b = ""
        return b if b.lower().startswith("https://") else ""

    def _flt(m) -> bool:
        try:
            return bool(m.from_user) and _parse(getattr(m, "text", "") or "", _uname())[0]
        except Exception:
            return False

    def _reply_kwargs(reply_to):
        if not reply_to:
            return {}
        try:
            from telebot import types as _t
            return {"reply_parameters": _t.ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)}
        except Exception:  # pyTelegramBotAPI قدیمی
            return {"reply_to_message_id": reply_to}

    def _place_markup(hit: dict):
        from telebot import types as _t
        kb = _t.InlineKeyboardMarkup()
        row = []
        base = _base()
        la, ln = hit["latitude"], hit["longitude"]

        def _lnk(app):
            return _go_link(app, hit, base) if base else _web_link(app, la, ln)

        if _NESHAN_LINK_TMPL and _NESHAN_LINK_TMPL != "0":
            row.append(_t.InlineKeyboardButton("🧭 باز کردن در نشان", url=_lnk("neshan")))
        row.append(_t.InlineKeyboardButton("🗺 گوگل مپس", url=_lnk("google")))
        kb.row(*row)
        return kb

    def _send_place(chat_id, hit: dict, reply_to, via: str):
        venue = dict(latitude=hit["latitude"], longitude=hit["longitude"],
                     title=hit["title"][:250], address=(hit.get("address") or hit["title"])[:250])
        try:
            venue["reply_markup"] = _place_markup(hit)
        except Exception as e:
            logger.warning("[NESHAN] ساخت دکمه‌های لینک ناموفق: %s", e)
        venue.update(_reply_kwargs(reply_to))
        bot.send_venue(chat_id, **venue)
        logger.info("[NESHAN] venue فرستاده شد via=%s chat=%s title=%r", via, chat_id, hit["title"][:60])

    def run_query(chat_id, reply_to, uid, query: str) -> dict:
        """جستجو + ارسال. یک نتیجه → ونیو ؛ چند نتیجه → فقط لیست دکمه (ونیو فقط با لمس کاربر)؛ هیچ → {"kind":"none"}."""
        results = search_neshan_smart_many(query, keyword_fn, limit=NESHAN_MAX_CHOICES)
        if not results:
            return {"kind": "none", "titles": []}
        titles = [r["title"] for r in results]
        if len(results) == 1:
            _send_place(chat_id, results[0], reply_to, "single")
            return {"kind": "single", "titles": titles}
        from telebot import types as _t
        token = _pending_put({"uid": uid, "chat_id": chat_id, "reply_to": reply_to, "results": results})
        # مشخصات هر نتیجه داخل متن پیام (مرتب و شماره‌دار)؛ دکمه‌ها فقط شماره‌اند تا جا کم نیاید
        kb = _t.InlineKeyboardMarkup(row_width=4)
        kb.add(*[_t.InlineKeyboardButton(_num_emoji(i + 1), callback_data=f"nsh:{token}:{i}")
                 for i in range(len(results))])
        kb.add(_t.InlineKeyboardButton("✖️ بستن", callback_data=f"nsh:{token}:x"))
        sent = bot.send_message(chat_id, _choices_text(query, results), parse_mode="HTML",
                                reply_markup=kb, **_reply_kwargs(reply_to))
        e = _pending_get(token)
        if e is not None and sent is not None:
            e["list_msg"] = sent.message_id
        logger.info("[NESHAN] لیست انتخاب فرستاده شد chat=%s n=%d token=%s (ونیو فقط بعد از لمس)", chat_id, len(results), token)
        return {"kind": "list", "titles": titles}

    _RUNNER["fn"] = run_query

    def _remove_list(call):
        """لیست انتخاب را پاک کن؛ اگر نشد حداقل دکمه‌ها را بردار تا دوباره قابل لمس نباشد."""
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
            return
        except Exception as e:
            logger.warning("[NESHAN] حذف پیام لیست ناموفق (%s: %s)؛ دکمه‌ها برداشته می‌شود", type(e).__name__, e)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception as e:
            logger.warning("[NESHAN] برداشتن دکمه‌های لیست هم ناموفق: %s: %s", type(e).__name__, e)

    @bot.callback_query_handler(func=lambda c: bool(getattr(c, "data", None)) and c.data.startswith("nsh:"))
    def handle_neshan_choice(call):
        try:
            _p, token, pick = (call.data.split(":") + ["", ""])[:3]
            entry = _pending_get(token)
            if entry is None:
                bot.answer_callback_query(call.id, "این لیست منقضی شده؛ دوباره /map بزن.", show_alert=True)
                _remove_list(call)
                return
            if call.from_user.id != entry["uid"]:
                bot.answer_callback_query(call.id, "فقط کسی که جستجو کرده می‌تواند انتخاب کند.", show_alert=True)
                return
            entry = _pending_pop(token)          # اتمیک: فقط اولین لمس برنده است (لمس دوباره/تحویل تکراری بی‌اثر)
            if entry is None:
                bot.answer_callback_query(call.id)
                return
            bot.answer_callback_query(call.id)
            _remove_list(call)
            if pick == "x":
                logger.info("[NESHAN] لیست بسته شد token=%s uid=%s", token, call.from_user.id)
                return
            idx = int(pick)
            hit = entry["results"][idx]
            logger.info("[NESHAN] لمس دکمه token=%s uid=%s idx=%d title=%r", token, call.from_user.id, idx, hit["title"][:60])
            _send_place(entry["chat_id"], hit, entry["reply_to"], f"tap(uid={call.from_user.id},idx={idx})")
        except Exception as e:
            logger.warning("[NESHAN] انتخاب نتیجه ناموفق: %s: %s", type(e).__name__, e)
            try:
                bot.answer_callback_query(call.id, "خطا؛ دوباره /map بزن.")
            except Exception:
                pass

    @bot.message_handler(func=_flt, content_types=["text"], chat_types=["private", "group", "supergroup"])
    def handle_neshan(msg):
        status = None
        try:
            if msg.chat.type in ("group", "supergroup"):
                if group_blocked and group_blocked(msg.chat):
                    return
            elif pv_blocked and pv_blocked(msg.from_user):
                return
            if spam_check and spam_check(msg.chat.id):
                return
            if _seen_before(msg.chat.id, msg.message_id):
                logger.warning("[NESHAN] پیام %s/%s قبلاً پردازش شده؛ نادیده گرفته شد", msg.chat.id, msg.message_id)
                return

            _ok, query = _parse(msg.text or "", _uname())
            query = query[:400]
            if not query:
                bot.reply_to(msg, HELP_TEXT, parse_mode="HTML")
                return

            try:
                bot.send_chat_action(msg.chat.id, "find_location")
            except Exception:
                pass
            try:
                status = bot.reply_to(msg, "🔎 در حال جستجو...")
            except Exception:
                status = None

            try:
                res = run_query(msg.chat.id, msg.message_id, msg.from_user.id, query)
            finally:
                # وضعیت «در حال جستجو» بعد از آماده‌شدن نتیجه پاک می‌شود (ارسال نتیجه داخل run_query است)
                if status is not None:
                    try:
                        bot.delete_message(msg.chat.id, status.message_id)
                    except Exception:
                        pass
                    status = None

            if res and res.get("kind") == "none":
                bot.reply_to(msg, "😕 چیزی پیدا نکردم یا سرویس نقشه جواب نداد. اسم رو دقیق‌تر بنویس و دوباره امتحان کن.")
        except Exception as e:
            logger.warning("[NESHAN] هندلر ناموفق: %s: %s", type(e).__name__, e)
            if status is not None:
                try:
                    bot.delete_message(msg.chat.id, status.message_id)
                except Exception:
                    pass
            try:
                bot.reply_to(msg, "⚠️ ارسال لوکیشن ناموفق بود؛ چند لحظهٔ دیگه دوباره امتحان کن.")
            except Exception:
                pass

    logger.info("[NESHAN] دستورهای /map /لوکیشن /نقشه ثبت شد (لینک داخل-اپ: %s)",
                "فعال" if _base() else "منتظر آدرس عمومی؛ فعلاً وب")
