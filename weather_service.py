# -*- coding: utf-8 -*-
"""
weather_service.py — ماژول هواشناسی ربات چند، بر پایهٔ The Weather Channel

این فایل کنار bot.py قرار می‌گیرد و دو کار انجام می‌دهد:
  ۱) رابط مورد انتظار bot.py را برای هوش مصنوعی فراهم می‌کند:
     fetch_weather(city) / format_weather_for_ai / format_weather_for_user /
     PREDEFINED_CITIES / WEATHER_HAS_FORECAST
  ۲) دستور /هوا و /weather را (با register_weather_handlers) به ربات اضافه می‌کند.
     بدون نام شهر: شهر ذخیره‌شدهٔ همان کاربر؛ این شهر را bot.py از «تنظیم پرامپت شخصی» کاربر درمی‌آورد
     (دیگر دکمه یا دستور تغییر شهر در این ماژول نیست). اگر شهری ثبت نبود، راهنمایی می‌دهد.

سرعت:
  • یک requests.Session مشترک با Keep-Alive و استخر اتصال (بدون هندشیک TLS تازه برای هر درخواست)
  • پارس JSON با orjson (اگر نصب باشد؛ وگرنه json استاندارد)
  • کش TTL در حافظه (پیش‌فرض ۵ دقیقه) + کش کوتاه خطا، تا API بی‌دلیل زده نشود

کلید خودکار (Auto-Renew) و چتر نجات:
  • کلید اولیه همان TWC_API_KEY (یا مقدار پیش‌فرض داخل فایل) است.
  • به‌محض خطای 401/403، کلید باطل می‌شود و get_dynamic_twc_key() از صفحهٔ weather.com
    (HTML و اسکریپت‌های سایت) کلید جدید را با Regex استخراج، با یک تماس آزمایشی تأیید و در حافظه
    کش می‌کند؛ پس تماس‌های بعدی هیچ ریکوئست اضافه‌ای به سایت مبدأ نمی‌زنند.
  • اگر کلید پیدا نشد یا weather.com/api.weather.com در دسترس نبود، پاسخ همان قالب همیشگی از
    Open-Meteo (مدل ecmwf_ifs025) پر می‌شود؛ برچسب منبع در پیام درست نمایش داده می‌شود.

متغیرهای محیطی اختیاری (.env):
  TWC_API_KEY         کلید API اولیه (پیش‌فرض: همان کلید داخل فایل)
  WEATHER_CACHE_SEC   مدت کش وضعیت فعلی به ثانیه (پیش‌فرض 300)
  WEATHER_FORECAST_CACHE_SEC  مدت کش پیش‌بینی روزها (پیش‌فرض 1800)
  WEATHER_PROXY       پروکسی اختصاصی، مثلاً http://127.0.0.1:8118
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from cachetools import TTLCache

try:  # پارس سریع‌تر؛ اختیاری
    import orjson as _jsonlib
except ImportError:  # pragma: no cover
    import json as _jsonlib

logger = logging.getLogger("ChandBot.weather")

# ------------------------------------------------------------------
# مختصات شهرهای اصلی ایران
# ------------------------------------------------------------------
IRAN_CITIES = {
    "تهران": (35.6892, 51.3890),
    "مشهد": (36.2972, 59.6067),
    "اصفهان": (32.6546, 51.6680),
    "شیراز": (29.5918, 52.5837),
    "تبریز": (38.0800, 46.2919),
    "کرج": (35.8400, 50.9391),
    "اهواز": (31.3183, 48.6706),
    "قم": (34.6401, 50.8764),
    "رشت": (37.2809, 49.5924),
    "کرمانشاه": (34.3142, 47.0650),
    "ارومیه": (37.5527, 45.0761),
    "یزد": (31.8974, 54.3569),
    "همدان": (34.7982, 48.5146),
    "کرمان": (30.2839, 57.0788),
    "بندرعباس": (27.1832, 56.2666),
    "بوشهر": (28.9234, 50.8203),
    "ساری": (36.5633, 53.0601),
    "گرگان": (36.8427, 54.4439),
    "زاهدان": (29.4963, 60.8629),
    "قزوین": (36.2797, 50.0049),
    "زنجان": (36.6736, 48.4787),
    "سنندج": (35.3144, 46.9961),
    "خرم‌آباد": (33.4878, 48.3558),
    "اراک": (34.0954, 49.7013),
    "اردبیل": (38.2498, 48.2933),
    "کیش": (26.5578, 53.9858),
    "قشم": (26.9582, 56.2718),
}

# نام مورد انتظار bot.py
PREDEFINED_CITIES = IRAN_CITIES

# پیش‌بینی روزانه (امروز + ۷ روز) از همین منبع گرفته می‌شود؛ bot.py با True بودن این پرچم
# برای «فردا/هفته» دیگر سراغ جستجوی وب نمی‌رود و داده را مستقیم به هوش مصنوعی می‌دهد.
WEATHER_HAS_FORECAST = True

_DEFAULT_TWC_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
TWC_API_KEY = (os.environ.get("TWC_API_KEY") or _DEFAULT_TWC_KEY).strip()   # کلید اولیه؛ کلید زنده را get_dynamic_twc_key() می‌دهد
TWC_OBS_URL = "https://api.weather.com/v3/wx/observations/current"
TWC_LOC_URL = "https://api.weather.com/v3/location/search"
TWC_FC_URL = "https://api.weather.com/v3/wx/forecast/daily/{n}day"
_FC_DAYS_TRY = (7, 5, 3)   # اولین endpoint که برای این کلید جواب بدهد انتخاب و یادآوری می‌شود
# برچسبی که زیر پیام به کاربر نشان داده می‌شود (با WEATHER_SOURCE_LABEL در .env قابل‌تغییر است)
SOURCE_NAME = (os.environ.get("WEATHER_SOURCE_LABEL") or "Apple Weather").strip()

# چتر نجات: Open-Meteo (عمومی، بدون کلید)
OM_URL = "https://api.open-meteo.com/v1/forecast"
OM_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
OM_MODEL = "ecmwf_ifs025"
OM_SOURCE_NAME = "Open-Meteo (ECMWF)"

try:
    _CACHE_SEC = max(30, int(os.environ.get("WEATHER_CACHE_SEC", "300")))
except ValueError:
    _CACHE_SEC = 300

# ------------------------------------------------------------------
# سشن HTTP مشترک (Keep-Alive + استخر اتصال + retry سبک)
# ------------------------------------------------------------------
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    s.mount("https://", HTTPAdapter(
        pool_connections=4, pool_maxsize=16,
        max_retries=Retry(
            total=2, connect=2, read=0, backoff_factor=0.25,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        ),
    ))
    proxy = os.environ.get("WEATHER_PROXY", "").strip()
    if proxy:
        s.trust_env = False
        s.proxies.update({"http": proxy, "https": proxy})
    return s


_session = _build_session()
_TIMEOUT = (3.5, 6.0)  # (اتصال، خواندن)

# TTLCache ترد-سیف نیست → همهٔ دسترسی‌ها زیر قفل
_lock = threading.Lock()
_obs_cache: TTLCache = TTLCache(maxsize=512, ttl=_CACHE_SEC)
_fail_cache: TTLCache = TTLCache(maxsize=512, ttl=15)      # جلوگیری از کوبیدن API وقتی از دسترس خارج است
_geo_cache: TTLCache = TTLCache(maxsize=512, ttl=86400)
_geo_miss: TTLCache = TTLCache(maxsize=512, ttl=600)       # «شهر پیدا نشد» فقط ۱۰ دقیقه کش می‌شود (نه ۲۴ ساعت) تا قطعی موقت دائمی نشود
_om_cache: TTLCache = TTLCache(maxsize=512, ttl=_CACHE_SEC)
_om_fail_cache: TTLCache = TTLCache(maxsize=512, ttl=15)
try:
    _FC_CACHE_SEC = max(60, int(os.environ.get("WEATHER_FORECAST_CACHE_SEC", "1800")))
except ValueError:
    _FC_CACHE_SEC = 1800
_fc_cache: TTLCache = TTLCache(maxsize=512, ttl=_FC_CACHE_SEC)
_fc_fail_cache: TTLCache = TTLCache(maxsize=512, ttl=15)
_fc_days_ok: int | None = None
_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="wx")
_MISSING = object()


def _get_json_status(url: str, params: dict):
    """GET + پارس JSON → (status_code یا None در خطای شبکه, داده یا None).
    هرگز متن خطای requests را لاگ نمی‌کنیم چون URL (با apiKey) داخلش است."""
    try:
        r = _session.get(url, params=params, timeout=_TIMEOUT)
    except Exception as e:
        logger.warning("[WEATHER] request failed: %s", type(e).__name__)
        return None, None
    if r.status_code != 200:
        logger.warning("[WEATHER] HTTP %s from %s", r.status_code, url.rsplit("/", 1)[-1])
        return r.status_code, None
    try:
        return 200, _jsonlib.loads(r.content)
    except Exception:
        logger.warning("[WEATHER] invalid JSON from %s", url.rsplit("/", 1)[-1])
        return 200, None


def _get_json(url: str, params: dict):
    return _get_json_status(url, params)[1]


# ------------------------------------------------------------------
# مدیریت کلید TWC — استخراج و تمدید خودکار
# ------------------------------------------------------------------
TWC_SITE_URL = "https://weather.com"
_WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# الگوی اول همان الگوی ساده؛ الگوی دوم حالت‌های JSON/جاوااسکریپت ("apiKey":"..." ، SUN_API_KEY ، apikey=...)
_KEY_PATTERNS = (
    re.compile(r'apiKey[=:]["\'\s]*([a-f0-9]{32})(?![a-f0-9])'),
    re.compile(r'(?:api[_-]?key|SUN_API_KEY)["\']?\s*[=:]\s*["\']?([a-f0-9]{32})(?![a-f0-9])', re.I),
)
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
_SCRIPT_HOSTS = ("weather.com", "w-x.co", "imwx.com")   # فقط اسکریپت‌های خود سایت/CDN آن
_SCRIPT_HINT_RE = re.compile(r"main|app|framework|config|sun|runtime", re.I)
_KEY_RETRY_SEC = 120.0       # بعد از یک استخراج ناموفق، تا این مدت دوباره سراغ سایت نمی‌رویم
_KEY_BUDGET_SEC = 14.0       # سقف زمان یک دور استخراج
_KEY_MAX_SCRIPTS = 8
_KEY_MAX_BYTES = 2_500_000
_KEY_MAX_PROBES = 4

_tk_lock = threading.Lock()            # حالت کلید
_tk_refresh_lock = threading.Lock()    # فقط یک ترد در لحظه استخراج می‌کند
_tk = {"key": TWC_API_KEY or None, "revoked": [], "next_try": 0.0, "fetched_at": 0.0}


def _mask_key(k) -> str:
    return ("…" + k[-4:]) if k else "—"


def _revoke_locked(k) -> None:
    """کلید را باطل می‌کند (قفل _tk_lock باید گرفته شده باشد)."""
    if not k:
        return
    if k not in _tk["revoked"]:
        _tk["revoked"].append(k)
        del _tk["revoked"][:-20]
    if _tk["key"] == k:
        _tk["key"] = None
        logger.warning("[WEATHER] TWC key %s revoked", _mask_key(k))


def _probe_key(k: str):
    """تماس آزمایشی سبک (رصد فعلی تهران) → status code یا None (خطای شبکه)."""
    return _get_json_status(TWC_OBS_URL, {
        "geocode": "35.6892,51.3890", "units": "m", "language": "en-US",
        "format": "json", "apiKey": k,
    })[0]


def _scan_for_keys(text: str) -> list:
    out = []
    for pat in _KEY_PATTERNS:
        for m in pat.finditer(text or ""):
            k = m.group(1).lower()
            if k not in out:
                out.append(k)
    return out


def _http_get_text(url: str, deadline: float):
    """متن یک صفحه/اسکریپت (حداکثر _KEY_MAX_BYTES) یا None."""
    left = deadline - time.time()
    if left < 1.0:
        return None
    try:
        r = _session.get(
            url, stream=True, timeout=(min(4.0, left), min(8.0, max(1.0, left))),
            headers={"User-Agent": _WEB_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
    except Exception as e:
        logger.warning("[WEATHER] site fetch failed: %s", type(e).__name__)
        return None
    try:
        if r.status_code != 200:
            return None
        buf, size = [], 0
        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            buf.append(chunk)
            size += len(chunk)
            if size >= _KEY_MAX_BYTES or time.time() > deadline:
                break
        return b"".join(buf).decode("utf-8", "ignore")
    except Exception as e:
        logger.warning("[WEATHER] site read failed: %s", type(e).__name__)
        return None
    finally:
        try:
            r.close()
        except Exception:
            pass


def _extract_key_from_site():
    """جدیدترین کلید فعال کلاینت وب را از weather.com برمی‌دارد: اول HTML صفحهٔ اصلی، بعد
    اسکریپت‌های باندل سایت. هر کاندید با یک تماس آزمایشی تأیید می‌شود. کلید معتبر یا None."""
    deadline = time.time() + _KEY_BUDGET_SEC
    with _tk_lock:
        revoked = set(_tk["revoked"])
    tried, probes = set(), 0

    def _validate(cands):
        nonlocal probes
        for k in cands:
            if k in revoked or k in tried or probes >= _KEY_MAX_PROBES:
                continue
            tried.add(k)
            probes += 1
            if _probe_key(k) == 200:
                return k
        return None

    page = _http_get_text(TWC_SITE_URL, deadline)
    if not page:
        return None
    hit = _validate(_scan_for_keys(page))
    if hit:
        return hit

    scripts = []
    for src in _SCRIPT_SRC_RE.findall(page):
        try:
            u = urljoin(TWC_SITE_URL + "/", src.strip())
            p = urlparse(u)
        except Exception:
            continue
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not any(host == h or host.endswith("." + h) for h in _SCRIPT_HOSTS):
            continue
        if u not in scripts:
            scripts.append(u)
    scripts.sort(key=lambda u: 0 if _SCRIPT_HINT_RE.search(u) else 1)   # sort پایدار است
    for u in scripts[:_KEY_MAX_SCRIPTS]:
        if time.time() > deadline or probes >= _KEY_MAX_PROBES:
            break
        js = _http_get_text(u, deadline)
        if not js:
            continue
        hit = _validate(_scan_for_keys(js))
        if hit:
            return hit
    return None


def get_dynamic_twc_key(force_refresh: bool = False, bad_key: str | None = None):
    """کلید فعال TWC را برمی‌گرداند (از کش حافظه)؛ در صورت نبودن کلید یا باطل‌شدن آن، خودکار از
    weather.com استخراج و کش می‌کند.
      bad_key       : کلیدی که همین الان 401/403 گرفته → فوراً باطل می‌شود و کلید تازه گرفته می‌شود
      force_refresh : حتی با داشتن کلید هم دوباره از سایت بخوان (مثلاً برای به‌روزرسانی دستی)
    خروجی None یعنی فعلاً کلیدی نیست (سایت در دسترس نیست / کول‌داون) و کد باید به فالبک برود.
    بین ترددها هماهنگ است: فقط یک ترد استخراج می‌کند و بقیه بدون انتظار None/کلید جاری می‌گیرند."""
    now = time.time()
    with _tk_lock:
        if bad_key:
            _revoke_locked(bad_key)
        cur = _tk["key"]
        if cur and not force_refresh:
            return cur
        if not force_refresh and now < _tk["next_try"]:
            return None
        if force_refresh and cur and now - _tk["fetched_at"] < 10:
            return cur
    if not _tk_refresh_lock.acquire(blocking=False):
        with _tk_lock:
            return _tk["key"]            # ترد دیگری در حال استخراج است؛ منتظر نمی‌مانیم
    try:
        with _tk_lock:                   # شاید ترد دیگری همین الان کار را تمام کرده
            cur = _tk["key"]
            if cur and not force_refresh:
                return cur
        logger.info("[WEATHER] extracting a fresh TWC key from %s ...", TWC_SITE_URL)
        try:
            new = _extract_key_from_site()
        except Exception as e:
            logger.warning("[WEATHER] key extraction error: %s", type(e).__name__)
            new = None
        with _tk_lock:
            if new:
                _tk["key"], _tk["fetched_at"], _tk["next_try"] = new, time.time(), 0.0
                logger.info("[WEATHER] new TWC key acquired (%s) and cached", _mask_key(new))
                return new
            _tk["next_try"] = time.time() + _KEY_RETRY_SEC
            logger.warning("[WEATHER] TWC key extraction failed — fallback active for %ds", int(_KEY_RETRY_SEC))
            return _tk["key"]
    finally:
        _tk_refresh_lock.release()


def _twc_request(url: str, params: dict):
    """GET به API هواشناسی TWC با کلید پویا → (status یا None, داده یا None).
    401/403: اگر خودِ کلید مرده باشد (برای endpoint رصد، یا تماس آزمایشی هم رد شود) فوراً باطل،
    کلید تازه استخراج و همان درخواست یک بار دوباره زده می‌شود. اگر کلید سالم است و فقط همین endpoint
    برای آن بسته است (مثلاً پیش‌بینی ۷ روزه)، کلید باطل نمی‌شود و کد بالاتر endpoint بعدی را امتحان می‌کند."""
    status, data = None, None
    for attempt in (0, 1):
        key = get_dynamic_twc_key()
        if not key:
            return None, None
        p = dict(params)
        p["apiKey"] = key
        status, data = _get_json_status(url, p)
        if status not in (401, 403):
            return status, data
        if url != TWC_OBS_URL and _probe_key(key) == 200:
            return status, data                  # کلید سالم، endpoint بسته
        logger.warning("[WEATHER] TWC key rejected (HTTP %s)", status)
        if attempt == 1:
            with _tk_lock:
                _revoke_locked(key)
            return status, None
        if not get_dynamic_twc_key(bad_key=key):
            return status, None
    return status, None


# ------------------------------------------------------------------
# تشخیص شهر از متن کاربر
# ------------------------------------------------------------------
def _norm(s: str) -> str:
    s = (s or "").lower()
    s = (s.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
          .replace("ۀ", "ه").replace("ة", "ه"))
    s = re.sub(r"[\u064b-\u065f\u0640]", "", s)   # اعراب و کشیده
    return re.sub(r"\s+", " ", s).strip()


_CITY_INDEX = {_norm(n): n for n in IRAN_CITIES}
_CITY_INDEX_NS = {k.replace(" ", ""): n for k, n in _CITY_INDEX.items()}  # «خرم آباد» = «خرم‌آباد»


def resolve_city(query: str) -> str | None:
    """نام استاندارد شهر از لیست ایران، یا None.
    اول تطابق کامل، بعد شهر داخل جمله (طولانی‌ترین نام برنده است)، بعد پیشوند.
    (نسخهٔ قبلی substring ساده بود و «کرمان» را به «کرمانشاه» می‌برد.)"""
    q = _norm(query)
    q = re.sub(r"^(شهر)\s+", "", q)
    if not q:
        return None
    hit = _CITY_INDEX.get(q) or _CITY_INDEX_NS.get(q.replace(" ", ""))
    if hit:
        return hit
    padded = f" {q} "
    inside = [k for k in _CITY_INDEX if f" {k} " in padded]
    if inside:
        return _CITY_INDEX[max(inside, key=len)]
    if len(q) >= 3:
        pref = [k for k in _CITY_INDEX if k.startswith(q)]
        if pref:
            return _CITY_INDEX[min(pref, key=len)]
    return None


def _geocode_openmeteo(name: str):
    """جستجوی مکان Open-Meteo (فالبک وقتی TWC در دسترس نیست یا شهر را نمی‌شناسد)."""
    d = _get_json(OM_GEO_URL, {"name": name, "count": 1, "language": "fa", "format": "json"})
    try:
        r = ((d or {}).get("results") or [None])[0]
        if r and r.get("latitude") is not None and r.get("longitude") is not None:
            return float(r["latitude"]), float(r["longitude"]), str(r.get("name") or name)
    except Exception:
        pass
    return None


def _geocode(name: str):
    """شهرهای خارج از لیست (مثلاً Paris یا یک شهر کوچک) — جستجوی مکان TWC و در صورت شکست Open-Meteo.
    (lat, lon, label) یا None."""
    name = re.sub(r"[^\w\s\u200c-]", " ", name or "", flags=re.UNICODE).strip()[:40]
    if len(name) < 2:
        return None
    key = _norm(name)
    with _lock:
        cached = _geo_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        if key in _geo_miss:
            return None
    _, d = _twc_request(TWC_LOC_URL, {
        "query": name, "locationType": "city", "language": "fa-IR", "format": "json",
    })
    res = None
    try:
        loc = (d or {}).get("location") or {}
        lats, lons = loc.get("latitude") or [], loc.get("longitude") or []
        if lats and lons:
            label = (loc.get("city") or loc.get("displayName") or [name])[0] or name
            res = (float(lats[0]), float(lons[0]), str(label))
    except Exception:
        res = None
    if res is None:
        res = _geocode_openmeteo(name)
    with _lock:
        if res is not None:
            _geo_cache[key] = res
        else:
            _geo_miss[key] = True
    return res


# ------------------------------------------------------------------
# دریافت دیتا
# ------------------------------------------------------------------
def _fetch_observation(lat: float, lon: float) -> dict | None:
    key = (round(lat, 2), round(lon, 2))
    with _lock:
        hit = _obs_cache.get(key)
        if hit is not None:
            return dict(hit)
        if key in _fail_cache:
            return None
    _, d = _twc_request(TWC_OBS_URL, {
        "geocode": f"{lat},{lon}",
        "units": "m",          # سانتی‌گراد و کیلومتر بر ساعت
        "language": "fa-IR",   # متن وضعیت به فارسی
        "format": "json",
    })
    if not isinstance(d, dict) or d.get("temperature") is None:
        with _lock:
            _fail_cache[key] = True
        return None
    data = {
        "temp": d.get("temperature"),
        "feel": d.get("temperatureFeelsLike"),
        "condition": d.get("wxPhraseLong") or d.get("wxPhraseMedium") or "نامشخص",
        "humidity": d.get("relativeHumidity"),
        "wind_speed": d.get("windSpeed"),
        "uv": d.get("uvIndex"),
        "observed": str(d.get("validTimeLocal") or "")[:16].replace("T", " "),
        "source": SOURCE_NAME,
    }
    with _lock:
        _obs_cache[key] = data
    return dict(data)


def _pick(arr, i):
    try:
        return arr[i] if arr is not None and 0 <= i < len(arr) else None
    except Exception:
        return None


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _parse_forecast(d: dict) -> list:
    """پاسخ خام TWC (آرایه‌های موازی) → لیست روزها. هر روز یک dict است.
    daypart: برای هر روز دو خانه دارد (۲i = روز، ۲i+1 = شب)؛ برای امروزِ عصر خانهٔ روز None می‌شود."""
    times = d.get("validTimeLocal") or []
    dps = d.get("daypart") or []
    dp = dps[0] if dps and isinstance(dps[0], dict) else {}
    out = []
    for i, t in enumerate(times):
        di, ni = 2 * i, 2 * i + 1
        pops = [v for v in (_pick(dp.get("precipChance"), di), _pick(dp.get("precipChance"), ni)) if v is not None]
        out.append({
            "date": str(t)[:10],
            "tmax": _first(_pick(d.get("calendarDayTemperatureMax"), i), _pick(d.get("temperatureMax"), i)),
            "tmin": _first(_pick(d.get("calendarDayTemperatureMin"), i), _pick(d.get("temperatureMin"), i)),
            "day_cond": _pick(dp.get("wxPhraseLong"), di),
            "night_cond": _pick(dp.get("wxPhraseLong"), ni),
            "pop": max(pops) if pops else None,
            "rain_mm": _pick(d.get("qpf"), i),
            "snow_mm": _pick(d.get("qpfSnow"), i),
            "wind": _first(_pick(dp.get("windSpeed"), di), _pick(dp.get("windSpeed"), ni)),
            "uv": _first(_pick(dp.get("uvIndex"), di), _pick(dp.get("uvIndex"), ni)),
            "humidity": _first(_pick(dp.get("relativeHumidity"), di), _pick(dp.get("relativeHumidity"), ni)),
        })
    return [x for x in out if x["date"]]


def _fetch_forecast(lat: float, lon: float):
    """پیش‌بینی روزانه (حداکثر ۷ روز) یا None."""
    global _fc_days_ok
    key = (round(lat, 2), round(lon, 2))
    with _lock:
        hit = _fc_cache.get(key)
        if hit is not None:
            return [dict(x) for x in hit]
        if key in _fc_fail_cache:
            return None
    params = {
        "geocode": f"{lat},{lon}", "units": "m", "language": "fa-IR",
        "format": "json",
    }
    order = (_fc_days_ok,) if _fc_days_ok else _FC_DAYS_TRY
    days = None
    for n in order:
        status, d = _twc_request(TWC_FC_URL.format(n=n), params)
        if status == 200 and isinstance(d, dict) and d.get("validTimeLocal"):
            try:
                days = _parse_forecast(d)
            except Exception:
                logger.warning("[WEATHER] forecast parse failed")
                days = None
            if days:
                _fc_days_ok = n
            break
        if status in (400, 401, 403, 404):
            continue            # این طول پیش‌بینی برای کلید باز نیست؛ بعدی
        break                   # خطای شبکه/سرور: بقیه را امتحان نکن
    if not days:
        with _lock:
            _fc_fail_cache[key] = True
        return None
    with _lock:
        _fc_cache[key] = days
    return [dict(x) for x in days]


# کد وضعیت WMO (خروجی Open-Meteo) → متن فارسی
_WMO_FA = {
    0: "آسمان صاف", 1: "عمدتاً صاف", 2: "نیمه‌ابری", 3: "ابری",
    45: "مه", 48: "مه یخ‌زده",
    51: "نم‌نم باران ضعیف", 53: "نم‌نم باران", 55: "نم‌نم باران شدید",
    56: "نم‌نم باران یخ‌زده", 57: "نم‌نم باران یخ‌زده شدید",
    61: "باران ضعیف", 63: "باران", 65: "باران شدید",
    66: "باران یخ‌زده", 67: "باران یخ‌زده شدید",
    71: "برف ضعیف", 73: "برف", 75: "برف شدید", 77: "دانه‌های برف",
    80: "رگبار ضعیف", 81: "رگبار", 82: "رگبار شدید",
    85: "بارش برف ضعیف", 86: "بارش برف شدید",
    95: "رعدوبرق", 96: "رعدوبرق با تگرگ ضعیف", 99: "رعدوبرق با تگرگ شدید",
}
_OM_FULL = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
    "precipitation_sum,snowfall_sum,wind_speed_10m_max,uv_index_max",
)
_OM_MIN = ("temperature_2m,weather_code", "weather_code,temperature_2m_max,temperature_2m_min")


def _wmo_text(code):
    try:
        return _WMO_FA.get(int(code))
    except (TypeError, ValueError):
        return None


def _parse_openmeteo(d: dict):
    """پاسخ Open-Meteo → (obs یا None, لیست روزها) با همان ساختار داده‌ی TWC."""
    cur = d.get("current") or {}
    daily = d.get("daily") or {}
    obs = None
    if cur.get("temperature_2m") is not None:
        obs = {
            "temp": cur.get("temperature_2m"),
            "feel": cur.get("apparent_temperature"),
            "condition": _wmo_text(cur.get("weather_code")) or "نامشخص",
            "humidity": cur.get("relative_humidity_2m"),
            "wind_speed": cur.get("wind_speed_10m"),
            "uv": _pick(daily.get("uv_index_max"), 0),
            "observed": str(cur.get("time") or "")[:16].replace("T", " "),
            "source": OM_SOURCE_NAME,
        }
    fc = []
    for i, t in enumerate(daily.get("time") or []):
        txt = _wmo_text(_pick(daily.get("weather_code"), i))
        fc.append({
            "date": str(t)[:10],
            "tmax": _pick(daily.get("temperature_2m_max"), i),
            "tmin": _pick(daily.get("temperature_2m_min"), i),
            "day_cond": txt, "night_cond": txt,
            "pop": _pick(daily.get("precipitation_probability_max"), i),
            "rain_mm": _pick(daily.get("precipitation_sum"), i),
            "snow_mm": _pick(daily.get("snowfall_sum"), i),     # Open-Meteo: سانتی‌متر
            "wind": _pick(daily.get("wind_speed_10m_max"), i),
            "uv": _pick(daily.get("uv_index_max"), i),
            "humidity": None,
        })
    return obs, [x for x in fc if x["date"]]


def _fetch_openmeteo(lat: float, lon: float):
    """چتر نجات: وضعیت فعلی + پیش‌بینی ۷ روزه از Open-Meteo (مدل ecmwf_ifs025) → (obs, fc) یا None.
    اگر مدل بعضی متغیرها را نداشت (HTTP 400)، با مجموعهٔ کمینه دوباره تلاش می‌شود."""
    key = (round(lat, 2), round(lon, 2))
    with _lock:
        hit = _om_cache.get(key)
        if hit is not None:
            return (dict(hit[0]) if hit[0] else None, [dict(x) for x in hit[1]])
        if key in _om_fail_cache:
            return None
    base = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "models": OM_MODEL,
        "timezone": "auto", "wind_speed_unit": "kmh", "forecast_days": 7,
    }
    parsed = None
    for cur_vars, daily_vars in (_OM_FULL, _OM_MIN):
        status, d = _get_json_status(OM_URL, dict(base, current=cur_vars, daily=daily_vars))
        if status == 200 and isinstance(d, dict):
            try:
                parsed = _parse_openmeteo(d)
            except Exception:
                logger.warning("[WEATHER] open-meteo parse failed")
                parsed = None
            break
        if status != 400:
            break
    if not parsed or (not parsed[0] and not parsed[1]):
        with _lock:
            _om_fail_cache[key] = True
        return None
    with _lock:
        _om_cache[key] = parsed
    return (dict(parsed[0]) if parsed[0] else None, [dict(x) for x in parsed[1]])


def _fetch_both(lat: float, lon: float):
    """وضعیت فعلی و پیش‌بینی را هم‌زمان می‌گیرد → (obs, fc, fc_از_open_meteo).
    هر بخشی که TWC ندهد (کلید/سایت/شبکه) بی‌صدا از Open-Meteo پر می‌شود."""
    fut = _pool.submit(_fetch_forecast, lat, lon)
    obs = _fetch_observation(lat, lon)
    try:
        fc = fut.result(timeout=25)
    except Exception:
        fc = None
    fc_om = False
    if obs is None or not fc:
        om = _fetch_openmeteo(lat, lon)
        if om:
            om_obs, om_fc = om
            if obs is None and om_obs:
                obs = om_obs
            if not fc and om_fc:
                fc, fc_om = om_fc, True
    return obs, fc, fc_om


def resolve_location(query: str):
    """(label, lat, lon) یا None — اول لیست شهرهای ایران، بعد جستجوی مکان TWC."""
    name = resolve_city(query)
    if name:
        lat, lon = IRAN_CITIES[name]
        return name, lat, lon
    geo = _geocode(query)
    if geo:
        lat, lon, label = geo
        return label, lat, lon
    return None


def fetch_weather(city_or_lat, lon: float | None = None, label: str | None = None) -> dict | None:
    """دو شکل فراخوانی:
       fetch_weather("مشهد")        ← شکل مورد استفادهٔ bot.py (اسم شهر؛ خارج از لیست هم جستجو می‌شود)
       fetch_weather(35.68, 51.38)  ← شکل فایل اصلی (مختصات)؛ label اختیاری = نام نمایشی شهر
    خروجی: dict شامل city, temp, feel, condition, humidity, wind_speed, uv, forecast (لیست روزها) … یا None."""
    try:
        if lon is not None:
            lat, lng = float(city_or_lat), float(lon)
        else:
            name = resolve_city(str(city_or_lat))
            if name:
                (lat, lng), label = IRAN_CITIES[name], name
            else:
                geo = _geocode(str(city_or_lat))
                if not geo:
                    return None
                lat, lng, label = geo
    except (TypeError, ValueError):
        return None
    obs, fc, fc_om = _fetch_both(lat, lng)
    if not obs and not fc:
        return None
    data = dict(obs) if obs else {
        "temp": None, "feel": None, "condition": None, "humidity": None,
        "wind_speed": None, "uv": None, "observed": "",
        "source": OM_SOURCE_NAME if fc_om else SOURCE_NAME,
    }
    if fc_om and OM_SOURCE_NAME not in str(data.get("source") or ""):
        data["source"] = f"{data.get('source') or SOURCE_NAME} + {OM_SOURCE_NAME}"
    data["forecast"] = fc or []
    data["city"] = label or f"{lat:.2f}, {lng:.2f}"
    return data


# ------------------------------------------------------------------
# تاریخ شمسی و نام روز هفته
# ------------------------------------------------------------------
_FA_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")  # date.weekday(): دوشنبه=0
_FA_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
              "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def _g2j(gy: int, gm: int, gd: int):
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
            + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def _day_label(i: int, iso: str) -> tuple:
    """(برچسب نسبی, توضیح تاریخ) مثلاً («فردا», «دوشنبه 13 مهر»)."""
    rel = {0: "امروز", 1: "فردا", 2: "پس‌فردا"}.get(i)
    try:
        d = _date.fromisoformat(iso)
        jy, jm, jd = _g2j(d.year, d.month, d.day)
        wd = _FA_WEEKDAYS[d.weekday()]
        md = f"{jd} {_FA_MONTHS[jm - 1]}"
        text = f"{wd} {md}" if rel else md      # برچسب روز هفته تکرار نشود
    except Exception:
        wd, text = iso, iso
    return (rel or wd), text


# ------------------------------------------------------------------
# قالب‌بندی خروجی
# ------------------------------------------------------------------
def _n(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(round(f))) if abs(f - round(f)) < 0.05 else f"{f:.1f}"


# ------------------------------------------------------------------
# راست‌به‌چپ‌سازی پیام (رفع قاطی‌شدن عدد، °، %، km/h و ایموجی با متن فارسی)
#   • اگر اولین حرف «قوی» یک خط لاتین باشد (مثل C در «22°C»)، تلگرام کل خط را چپ‌به‌راست می‌چیند و خط‌ها
#     تراز و ترتیب متفاوت می‌گیرند → اول هر خط یک RLM می‌گذاریم تا همهٔ خط‌ها یکسان راست‌به‌چپ باشند.
#   • هر قطعهٔ عددی/لاتین (22°C ، 29% ، 26 km/h ، UV 3) بین دو LRM قرار می‌گیرد تا به‌صورت یک بلوک
#     چسبیده بماند و علامت‌هایش به عدد کناری نپرد.
# ------------------------------------------------------------------
_RLM = "\u200f"
_LRM = "\u200e"
_DIGITS_RE = re.compile(r"\d+")


def _ltr(s) -> str:
    """قطعهٔ چپ‌به‌راست داخل متن فارسی."""
    return f"{_LRM}{s}{_LRM}"


def _ltr_digits(s: str) -> str:
    """هر عدد داخل یک متن فارسی (مثل «12 مهر») را به‌صورت بلوک چپ‌به‌راست درمی‌آورد."""
    return _DIGITS_RE.sub(lambda m: _ltr(m.group(0)), s or "")


def _val(v, unit: str = "") -> str:
    """عدد + واحد به‌صورت یک بلوک LTR؛ اگر مقدار نبود فقط «—»."""
    if v is None:
        return "—"
    return _ltr(f"{_n(v)}{unit}")


# ------------------------------------------------------------------
# متن‌های قابل‌ویرایش از پنل ادمین (✏️ ویرایش پیام‌ها)
# bot.py این دیکشنری را به TEXT_SETTING_META اضافه می‌کند و با set_text_provider
# تابع render_text_or_default را وصل می‌کند. بدون bot.py (اجرای مستقل) همین پیش‌فرض‌ها استفاده می‌شوند.
# ------------------------------------------------------------------
TEXT_DEFAULTS = {
    "wx_title": {
        "label": "هوا — عنوان پیام آب‌وهوا",
        "vars": "{city} نام شهر — خالی = پیش‌فرض",
        "empty": "default",
        "default": "🌤 <b>هوای {city}</b>",
    },
    "wx_no_forecast": {
        "label": "هوا — وقتی پیش‌بینی روزهای آینده در دسترس نیست",
        "vars": "بدون متغیر — خالی = پیش‌فرض",
        "empty": "default",
        "default": "ℹ️ پیش‌بینی روزهای آینده الان در دسترس نیست.",
    },
    "wx_no_city": {
        "label": "هوا — شهر کاربر ثبت نیست (راهنمای تنظیم از پرامپت شخصی)",
        "vars": "بدون متغیر — خالی = پیش‌فرض",
        "empty": "default",
        "default": (
            "📍 شهر یا روستات برای /هوا ثبت نیست.\n"
            "توی پیوی ربات روی «تنظیم پرامپت شخصی» بزن و توی متنت بنویس کجا زندگی می‌کنی "
            "(مثلاً «ساکن شیراز هستم»).\n"
            "یا همین‌جا اسم شهر رو بعد از دستور بنویس: <code>/هوا مشهد</code>"
        ),
    },
    "wx_change_hint": {
        "label": "هوا — کاربر «/هوا تغییر» زد (راهنمای تغییر شهر)",
        "vars": "بدون متغیر — خالی = پیش‌فرض",
        "empty": "default",
        "default": (
            "📍 شهر یا روستات رو از «تنظیم پرامپت شخصی» توی پیوی ربات عوض کن؛ "
            "کافیه توی متنت بنویسی کجا زندگی می‌کنی.\n"
            "برای یک بار هم می‌تونی بنویسی: <code>/هوا مشهد</code>"
        ),
    },
    "wx_city_not_found": {
        "label": "هوا — شهرِ داخل دستور پیدا نشد (/هوا اشتباه)",
        "vars": "{city} نامی که کاربر نوشته — خالی = پیش‌فرض",
        "empty": "default",
        "default": "❌ شهر «{city}» پیدا نشد.\n\nمثال دستور: <code>/هوا مشهد</code> یا <code>/هوا اصفهان</code>",
    },
    "wx_api_error": {
        "label": "هوا — خطای ارتباط با ایستگاه هواشناسی",
        "vars": "بدون متغیر — خالی = پیش‌فرض",
        "empty": "default",
        "default": "⚠️ خطا در برقراری ارتباط با ایستگاه هواشناسی. لطفاً چند لحظه بعد تلاش کنید.",
    },
    "wx_body_current": {
        "label": "هوا — بدنهٔ وضعیت فعلی (دما، رطوبت، باد، UV)",
        "vars": (
            "{city} شهر — {temp} دما — {feel} دمای حسی — {feel_part} «(حسی ..)» فقط وقتی با دما فرق دارد — "
            "{condition} وضعیت — {cond_part} «· وضعیت» فقط اگر باشد — {humidity} رطوبت — {wind} سرعت باد — "
            "{uv} شاخص UV — {observed} زمان رصد — خالی = پیش‌فرض"
        ),
        "empty": "default",
        "default": "🌡 <b>{temp}</b>{feel_part}{cond_part}\n💧 {humidity}   💨 {wind}   ☀️ {uv}",
    },
    "wx_body_day": {
        "label": "هوا — بدنهٔ پیش‌بینی هر روز (امروز/فردا/…)",
        "vars": (
            "{city} شهر — {day} برچسب روز (امروز، فردا، دوشنبه) — {date} تاریخ شمسی — "
            "{day_head} روز و تاریخ آماده — {tmin} حداقل دما — {tmax} حداکثر دما — {pop} احتمال بارش — "
            "{rain_mm} میزان بارندگی — {rain_part} «☔ بارش» فقط اگر باشد — {snow} برف — {wind} باد — "
            "{day_cond} وضعیت روز — {night_cond} وضعیت شب — {cond_lines} وضعیت روز و شب آماده — "
            "این بدنه برای هر روز یک‌بار تکرار می‌شود — خالی = پیش‌فرض"
        ),
        "empty": "default",
        "default": "📅 {day_head}\n🌡 {tmin} تا {tmax}{rain_part}\n{cond_lines}",
    },
    "wx_footer": {
        "label": "هوا — خط پایانی پیام (منبع داده)",
        "vars": "{source} نام منبع — {city} شهر — خالی = پیش‌فرض",
        "empty": "default",
        "default": "<i>{source}</i>",
    },
}

_text_provider = None


def set_text_provider(fn) -> None:
    """fn(key, **vars) → متن رندرشده (در bot.py: render_text_or_default)."""
    global _text_provider
    _text_provider = fn


class _SafeFmt(dict):
    def __missing__(self, k):
        return ""


def _t(key: str, **kw) -> str:
    """متن قابل‌ویرایش؛ اگر ادمین چیزی تنظیم نکرده یا خالی گذاشته، پیش‌فرض."""
    default = (TEXT_DEFAULTS.get(key) or {}).get("default", "")
    if _text_provider is not None:
        try:
            out = _text_provider(key, **kw)
            if out and out.strip():
                return out
        except Exception as e:
            logger.warning("[WEATHER] text provider failed for %s: %s", key, type(e).__name__)
    try:
        return default.format_map(_SafeFmt(kw))
    except Exception:
        return default


# پاک‌شدن خودکار پیام‌های /هوا (پیام دستور کاربر + جواب ربات) — پیش‌فرض ۱ دقیقه، فقط در گروه.
# با WEATHER_AUTO_DELETE_SEC در .env قابل‌تغییر است (0 = خاموش)؛ برای پیوی: WEATHER_AUTO_DELETE_PRIVATE=1
try:
    WEATHER_AUTO_DELETE_SEC = float(os.environ.get("WEATHER_AUTO_DELETE_SEC", "60") or 60)
except ValueError:
    WEATHER_AUTO_DELETE_SEC = 60.0
WEATHER_AUTO_DELETE_PRIVATE = (os.environ.get("WEATHER_AUTO_DELETE_PRIVATE", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


def _rtl_lines(text: str) -> str:
    """اول هر خط غیرخالی یک RLM می‌گذارد."""
    return "\n".join((_RLM + ln) if ln.strip() else ln for ln in (text or "").split("\n"))


def _cond_text(day: dict) -> str:
    dc, nc = day.get("day_cond"), day.get("night_cond")
    if dc and nc and dc != nc:
        return f"روز: {dc} | شب: {nc}"
    return dc or nc or "—"


def format_weather_message(city_name: str, data: dict, future: bool = False, days: int | None = None) -> str:
    """پیام HTML تلگرام: وضعیت فعلی + پیش‌بینی (پیش‌فرض امروز و فردا؛ future=True → تا ۷ روز).
    days: تعداد روز پیش‌بینی را مستقیم تعیین می‌کند. همهٔ خط‌ها راست‌به‌چپ و بلوک‌های عددی چسبیده‌اند.
    عنوان، بدنهٔ وضعیت فعلی، بدنهٔ هر روز و خط پایانی همگی از پنل ادمین (✏️ ویرایش پیام‌ها) قابل‌ویرایش‌اند."""
    e = html.escape
    city = e(str(city_name))
    out = _t("wx_title", city=city) + "\n"
    if data.get("temp") is not None:
        cond = str(data.get("condition") or "").strip()
        feel = data.get("feel")
        feel_txt = _val(feel, "°") if feel is not None else ""
        feel_part = f" (حسی {feel_txt})" if feel is not None and _n(feel) != _n(data.get("temp")) else ""
        cond_part = f" · {e(cond)}" if cond else ""
        uv = data.get("uv")
        uv_txt = _ltr(f"UV {_n(uv)}") if uv is not None else _ltr("UV —")
        body = _t(
            "wx_body_current", city=city,
            temp=_val(data.get("temp"), "°C"), feel=feel_txt, feel_part=feel_part,
            condition=e(cond), cond_part=cond_part,
            humidity=_val(data.get("humidity"), "%"), wind=_val(data.get("wind_speed"), " km/h"),
            uv=uv_txt, observed=e(str(data.get("observed") or "")),
        )
        out += "\n" + body.rstrip() + "\n"
    fc = data.get("forecast") or []
    n_days = days if days is not None else (7 if future else 2)
    if fc:
        for i, day in enumerate(fc[:max(1, n_days)]):
            rel, when = _day_label(i, day.get("date", ""))
            date_txt = _ltr_digits(e(when)) if when and when != rel else ""
            head = f"<b>{e(rel)}</b>" + (f" · {date_txt}" if date_txt else "")
            pop, rain, snow, wnd = day.get("pop"), day.get("rain_mm"), day.get("snow_mm"), day.get("wind")
            pop_txt = _val(pop, "%") if pop is not None else ""
            rain_txt = _val(rain, " mm") if rain else ""
            rain_part = ""
            if pop is not None:
                rain_part = f"   ☔ {pop_txt}" + (f" ({rain_txt})" if rain_txt else "")
            dc, nc = day.get("day_cond"), day.get("night_cond")
            if dc and nc and dc != nc:
                cond_lines = f"☀️ {e(dc)}\n🌙 {e(nc)}"
            elif dc or nc:
                cond_lines = f"☁️ {e(dc or nc)}"
            else:
                cond_lines = ""
            body = _t(
                "wx_body_day", city=city, day=e(rel), date=date_txt, day_head=head,
                tmin=_val(day.get("tmin"), "°"), tmax=_val(day.get("tmax"), "°C"),
                pop=pop_txt, rain_mm=rain_txt, rain_part=rain_part,
                snow=_val(snow, " cm") if snow else "", wind=_val(wnd, " km/h") if wnd is not None else "",
                day_cond=e(dc or ""), night_cond=e(nc or ""), cond_lines=cond_lines,
            )
            out += "\n" + body.rstrip() + "\n"
    elif future or days:
        out += "\n" + _t("wx_no_forecast") + "\n"
    out += "\n" + _t("wx_footer", source=e(str(data.get("source") or SOURCE_NAME)), city=city)
    return _rtl_lines(out)


def format_weather_for_user(data: dict, future: bool = False) -> str:
    """جواب مستقیم به کاربر (فالبک وقتی همهٔ AIها از دسترس خارج‌اند)."""
    return format_weather_message(data.get("city") or "—", data, future=future)


def format_weather_for_ai(data: dict) -> str:
    """بلوک متنی ساده برای تزریق به پرامپت هوش مصنوعی (وضعیت فعلی + پیش‌بینی روزها)."""
    lines = [f"شهر: {data.get('city') or '—'}"]
    if data.get("temp") is not None:
        lines += [
            f"دما: {_n(data.get('temp'))}°C (دمای حسی {_n(data.get('feel'))}°C)",
            f"وضعیت: {data.get('condition') or '—'}",
            f"رطوبت: {_n(data.get('humidity'))}%",
            f"سرعت باد: {_n(data.get('wind_speed'))} km/h",
            f"شاخص UV: {_n(data.get('uv'))}",
        ]
        if data.get("observed"):
            lines.append(f"زمان رصد (محلی): {data['observed']}")
    else:
        lines.append("وضعیت فعلی: در دسترس نیست")
    fc = data.get("forecast") or []
    if fc:
        lines.append("")
        lines.append("پیش‌بینی روزهای آینده (دما سانتی‌گراد؛ تاریخ میلادی و شمسی هر روز آمده):")
        for i, day in enumerate(fc):
            rel, when = _day_label(i, day.get("date", ""))
            parts = [f"حداقل {_n(day.get('tmin'))}، حداکثر {_n(day.get('tmax'))}"]
            if day.get("day_cond"):
                parts.append(f"روز: {day['day_cond']}")
            if day.get("night_cond"):
                parts.append(f"شب: {day['night_cond']}")
            if day.get("pop") is not None:
                parts.append(f"احتمال بارش {_n(day['pop'])}٪")
            if day.get("rain_mm"):
                parts.append(f"بارندگی حدود {_n(day['rain_mm'])} میلی‌متر")
            if day.get("snow_mm"):
                parts.append(f"برف حدود {_n(day['snow_mm'])} سانتی‌متر")
            if day.get("wind") is not None:
                parts.append(f"باد {_n(day['wind'])} km/h")
            lines.append(f"{rel} ({day.get('date')}، {when}): " + "؛ ".join(parts))
        lines.append(f"منبع: {data.get('source') or SOURCE_NAME} (وضعیت فعلی + پیش‌بینی {len(fc)} روز)")
    else:
        lines.append(f"منبع: {data.get('source') or SOURCE_NAME} (فقط وضعیت فعلی؛ داده پیش‌بینی روزهای آینده الان در دسترس نیست)")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# هندلر ربات تلگرام: /هوا ، /weather ، /hava
#   /هوا مشهد  → هوای همان شهر (چیزی ذخیره نمی‌شود)
#   /هوا       → شهر ذخیره‌شدهٔ همان کاربر. این شهر از «تنظیم پرامپت شخصی» (پیوی ربات) می‌آید
#                و bot.py آن را در دیتابیس می‌نویسد؛ اگر ثبت نبود، راهنمای ثبت نشان داده می‌شود.
# دکمه و دستور «تغییر شهر» حذف شده؛ «/هوا تغییر» هم همان راهنما را نشان می‌دهد.
# ذخیره‌سازی از بیرون تزریق می‌شود (این ماژول به دیتابیس وابسته نیست):
#   get_location(user_id) → {"city","lat","lon"} یا None
#   save_location → دیگر استفاده نمی‌شود (فقط برای سازگاری با فراخوانی‌های قدیمی نگه داشته شده)
# ------------------------------------------------------------------
_CHANGE_WORDS = frozenset(("تغییر", "تغییر شهر", "شهر من", "شهرمن", "change", "set"))


def register_weather_handlers(bot, spam_check=None, get_location=None, save_location=None):
    """spam_check: تابع اختیاری (chat_id) → bool؛ اگر True برگرداند پیام نادیده گرفته می‌شود
    (در bot.py همان is_spam خود ربات پاس داده می‌شود)."""
    _per_user = get_location is not None

    def _spam(chat_id) -> bool:
        if spam_check is None:
            return False
        try:
            return bool(spam_check(chat_id))
        except Exception:
            return False

    def _try_delete(chat_id, message_id) -> bool:
        """حذف پیام؛ اگر ربات اجازه نداشته باشد (ادمین نیست/پیام قدیمی) بی‌صدا False برمی‌گرداند."""
        if not message_id or message_id <= 0:
            return False
        try:
            bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            return False

    def _autodel(chat, *message_ids):
        """بعد از WEATHER_AUTO_DELETE_SEC ثانیه پیام‌ها را پاک می‌کند (فقط گروه، مگر WEATHER_AUTO_DELETE_PRIVATE=1)."""
        if WEATHER_AUTO_DELETE_SEC <= 0:
            return
        if getattr(chat, "type", "") == "private" and not WEATHER_AUTO_DELETE_PRIVATE:
            return
        ids = [m for m in message_ids if m]
        if not ids:
            return
        chat_id = chat.id

        def _job():
            for mid in ids:
                _try_delete(chat_id, mid)
        t = threading.Timer(WEATHER_AUTO_DELETE_SEC, _job)
        t.daemon = True
        t.start()

    def _reply(message, text, **kw):
        """ریپلای به پیام دستور + زمان‌بندی پاک‌شدن هر دو."""
        sent = bot.reply_to(message, text, **kw)
        _autodel(message.chat, message.message_id, getattr(sent, "message_id", None))
        return sent

    def _send_weather(message, w_data):
        _reply(message, format_weather_for_user(w_data), parse_mode="HTML")

    def _show_for_query(message, city_query: str):
        w_data = fetch_weather(city_query)
        if not w_data:
            if resolve_city(city_query) is None:
                _reply(message, _t("wx_city_not_found", city=html.escape(city_query[:40])), parse_mode="HTML")
            else:
                _reply(message, _t("wx_api_error"))
            return
        _send_weather(message, w_data)

    @bot.message_handler(commands=["هوا", "weather", "hava"])
    def cmd_weather(message):
        if _spam(message.chat.id):
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not _per_user:                       # سازگاری با نسخهٔ قبل: پیش‌فرض تهران
            _show_for_query(message, arg or "تهران")
            return

        uid = message.from_user.id if message.from_user else None
        if uid is None:
            _show_for_query(message, arg or "تهران")
            return

        if arg and _norm(arg) in {_norm(w) for w in _CHANGE_WORDS}:
            _reply(message, _t("wx_change_hint"), parse_mode="HTML")  # تغییر شهر فقط از پرامپت شخصی
            return
        if arg:
            _show_for_query(message, arg)
            return

        try:
            loc = get_location(uid)
        except Exception as e:
            logger.warning("[WEATHER] get_location failed: %s", type(e).__name__)
            loc = None
        if not loc:
            _reply(message, _t("wx_no_city"), parse_mode="HTML")
            return

        w_data = fetch_weather(loc["lat"], loc["lon"], label=loc["city"])
        if not w_data:
            _reply(message, _t("wx_api_error"))
            return
        _send_weather(message, w_data)
