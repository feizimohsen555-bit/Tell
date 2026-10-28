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
                                            pv_blocked=_pv_fj_blocked)
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


def _unpack(raw: bytes):
    """بایت‌های خام → JSON (تست gzip / zlib / raw-deflate / base64 / brotli) یا None."""
    def _load(b):
        return json.loads(b.decode("utf-8"))
    for fn in (lambda b: _load(b),
               lambda b: _load(gzip.decompress(b)),
               lambda b: _load(zlib.decompress(b)),
               lambda b: _load(zlib.decompress(b, -zlib.MAX_WBITS)),
               lambda b: _b64_json(b)):
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
    for key in ("location", "point", "center", "geometry", "position"):
        v = item.get(key)
        if isinstance(v, dict):
            loc = v
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
    addr = _s(item.get("address"))
    if addr:
        return addr
    parts = [_s(item.get(k)) for k in ("region", "neighbourhood", "neighborhood", "city", "state")]
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


# ----------------------------- search -----------------------------
def search_neshan_location(query: str, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG,
                           timeout: float | None = None) -> dict | None:
    """جستجو در نشان → {"title","address","latitude","longitude"} یا None (خطا/تایم‌اوت/بدون نتیجه)."""
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
                                    r.headers.get("Content-Encoding"), len(r.content), _has_br, r.content[:1600])
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
    return _pick(items)


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


# ----------------------------- telegram -----------------------------
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
                             keyword_fn=None):
    """هندلر /map /لوکیشن /نقشه را روی bot ثبت می‌کند.
    spam_check(chat_id)->bool ؛ group_blocked(chat)->bool ؛ pv_blocked(user)->bool ؛
    keyword_fn(text)->list[str] (تبدیل آدرس طولانی به کلیدواژه با AI) — همه اختیاری."""
    if not NESHAN_ENABLED:
        logger.info("[NESHAN] غیرفعال (NESHAN_ENABLED=0)")
        return

    def _uname() -> str:
        try:
            return (bot_username_fn() if bot_username_fn else getattr(bot.get_me(), "username", "")) or ""
        except Exception:
            return ""

    def _flt(m) -> bool:
        try:
            return bool(m.from_user) and _parse(getattr(m, "text", "") or "", _uname())[0]
        except Exception:
            return False

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

            hit = search_neshan_smart(query, keyword_fn)

            if status is not None:
                try:
                    bot.delete_message(msg.chat.id, status.message_id)
                except Exception:
                    pass
                status = None

            if not hit:
                bot.reply_to(msg, "😕 چیزی پیدا نکردم یا سرویس نقشه جواب نداد. اسم رو دقیق‌تر بنویس و دوباره امتحان کن.")
                return

            venue = dict(latitude=hit["latitude"], longitude=hit["longitude"],
                         title=hit["title"], address=hit["address"])
            try:
                from telebot import types as _t
                venue["reply_parameters"] = _t.ReplyParameters(message_id=msg.message_id)
            except Exception:  # pyTelegramBotAPI قدیمی
                venue["reply_to_message_id"] = msg.message_id
            bot.send_venue(msg.chat.id, **venue)
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

    logger.info("[NESHAN] دستورهای /map /لوکیشن /نقشه ثبت شد")
