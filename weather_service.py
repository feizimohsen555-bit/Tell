# -*- coding: utf-8 -*-
"""
weather_service.py — ماژول آب‌وهوای فوق‌دقیق به سبک Apple Weather
مبتنی بر مدل تفکیک‌بالای ECMWF + پیش‌بینی ساعتی و شاخص کیفیت هوا (AQI).
کاملاً سازگار با bot.py و تنظیمات پنل ادمین.
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from cachetools import TTLCache

try:
    import orjson as _jsonlib
except ImportError:
    import json as _jsonlib

logger = logging.getLogger("ChandBot.weather")

# ------------------------------------------------------------------
# مختصات دقیق شهرهای ایران (مرکز جمعیتی و تصحیح‌شده)
# ------------------------------------------------------------------
IRAN_CITIES = {
    "تهران": (35.7219, 51.3347),
    "مشهد": (36.2972, 59.6067),
    "اصفهان": (32.6546, 51.6680),
    "شیراز": (29.5918, 52.5837),
    "تبریز": (38.0800, 46.2919),
    "کرج": (35.8327, 50.9915),
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
    "ایلام": (33.6374, 46.4227),
    "یاسوج": (30.6684, 51.5876),
    "سمنان": (35.5769, 53.3953),
    "بجنورد": (37.4761, 57.3283),
    "بیرجند": (32.8663, 59.2211),
    "شهرکرد": (32.3256, 50.8644),
}

PREDEFINED_CITIES = IRAN_CITIES
WEATHER_HAS_FORECAST = True
SOURCE_NAME = os.environ.get("WEATHER_SOURCE_LABEL", "Apple Weather Engine (ECMWF)").strip()

OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OM_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
OM_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"

try:
    _CACHE_SEC = max(30, int(os.environ.get("WEATHER_CACHE_SEC", "300")))
except ValueError:
    _CACHE_SEC = 300

# ------------------------------------------------------------------
# راه‌اندازی Session با استخر اتصال سریع
# ------------------------------------------------------------------
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "ChandBot-Weather/2.0 (Apple-Style Engine)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    s.mount("https://", HTTPAdapter(
        pool_connections=4, pool_maxsize=16,
        max_retries=Retry(
            total=2, connect=2, read=0, backoff_factor=0.2,
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
_TIMEOUT = (3.5, 6.0)

_lock = threading.Lock()
_weather_cache: TTLCache = TTLCache(maxsize=512, ttl=_CACHE_SEC)
_geo_cache: TTLCache = TTLCache(maxsize=512, ttl=86400)
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wx_apple")

# ------------------------------------------------------------------
# سیستم کدگذاری دقیق پدیده‌ها (مشابه نمادهای Apple Weather)
# ------------------------------------------------------------------
_WMO_MAP = {
    0: ("آسمان صاف", "☀️", "🌙"),
    1: ("عمدتاً آفتابی", "🌤", "🌤"),
    2: ("نیمه‌ابری", "⛅", "⛅"),
    3: ("ابری", "☁️", "☁️"),
    45: ("مه‌آلود", "🌫", "🌫"),
    48: ("مه یخ‌زده", "🌫", "🌫"),
    51: ("باران خفیف", "🌦", "🌧"),
    53: ("باران نم‌نم", "🌧", "🌧"),
    55: ("نم‌نم متراکم", "🌧", "🌧"),
    61: ("باران ملایم", "🌧", "🌧"),
    63: ("بارش باران", "🌧", "🌧"),
    65: ("باران رگباری شدید", "🌧", "🌧"),
    66: ("باران منجمد", "🌧", "🌧"),
    67: ("باران یخی شدید", "🌧", "🌧"),
    71: ("بارش برف خفیف", "🌨", "🌨"),
    73: ("بارش برف", "🌨", "🌨"),
    75: ("کولاک برف", "❄️", "❄️"),
    77: ("دانه‌های برف", "❄️", "❄️"),
    80: ("رگبار خفیف", "🌦", "🌧"),
    81: ("رگبار باران", "🌧", "🌧"),
    82: ("رگبار بسیار شدید", "⛈", "⛈"),
    85: ("رگبار پراکنده برف", "🌨", "🌨"),
    86: ("رگبار سنگین برف", "❄️", "❄️"),
    95: ("رعدوبرق", "⛈", "⛈"),
    96: ("طوفان همراه تگرگ", "⛈", "⛈"),
    99: ("طوفان صاعقه شدید", "⛈", "⛈"),
}


def _get_wmo(code: int, is_day: int = 1) -> tuple[str, str]:
    item = _WMO_MAP.get(int(code or 0), ("صاف", "☀️", "🌙"))
    desc = item[0]
    icon = item[1] if is_day else item[2]
    return desc, icon


def _wind_dir_fa(deg: float | None) -> str:
    if deg is None:
        return ""
    dirs = ["شمالی", "شمال‌شرقی", "شرقی", "جنوب‌شرقی", "جنوبی", "جنوب‌غربی", "غربی", "شمال‌غربی"]
    ix = int((deg + 22.5) / 45.0) % 8
    return dirs[ix]


def _aqi_status(aqi: int | None) -> tuple[str, str]:
    if aqi is None:
        return "نامشخص", "⚪"
    if aqi <= 50:
        return "پاک", "🟢"
    elif aqi <= 100:
        return "قابل قبول", "🟡"
    elif aqi <= 150:
        return "ناسالم برای گروه‌های حساس", "🟠"
    elif aqi <= 200:
        return "ناسالم برای عموم", "🔴"
    elif aqi <= 300:
        return "بسیار ناسالم", "🟣"
    return "خطرناک", "🟤"


# ------------------------------------------------------------------
# دریافت دیتای هواشناسی + کیفیت هوا
# ------------------------------------------------------------------
def _fetch_from_open_meteo(lat: float, lon: float) -> dict | None:
    cache_key = (round(lat, 3), round(lon, 3))
    with _lock:
        if cache_key in _weather_cache:
            return dict(_weather_cache[cache_key])

    # پارامترهای موتور ECMWF و تفکیک بالا
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
            "precipitation,weather_code,wind_speed_10m,wind_direction_10m,surface_pressure"
        ),
        "hourly": "temperature_2m,weather_code,is_day,precipitation_probability",
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
            "apparent_temperature_min,sunrise,sunset,uv_index_max,precipitation_sum,"
            "precipitation_probability_max,wind_speed_10m_max"
        ),
        "timezone": "auto",
        "forecast_days": 8,
    }

    aqi_params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "us_aqi,pm2_5,pm10",
        "timezone": "auto",
    }

    try:
        r = _session.get(OM_FORECAST_URL, params=params, timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        d = _jsonlib.loads(r.content)

        # دریافت اختیاری و موازی کیفیت هوا
        aqi_val = None
        try:
            r_aqi = _session.get(OM_AIR_QUALITY_URL, params=aqi_params, timeout=(2.0, 3.0))
            if r_aqi.status_code == 200:
                aqi_d = _jsonlib.loads(r_aqi.content)
                aqi_val = aqi_d.get("current", {}).get("us_aqi")
        except Exception:
            pass

    except Exception as e:
        logger.warning("[WEATHER] خطا در دریافت دیتای موتور هواشناسی: %s", e)
        return None

    cur = d.get("current", {})
    daily = d.get("daily", {})
    hourly = d.get("hourly", {})

    w_code = cur.get("weather_code", 0)
    is_day = cur.get("is_day", 1)
    cond_desc, cond_icon = _get_wmo(w_code, is_day)

    # تجزیه پیش‌بینی ساعتی (۱۲ ساعت آینده مثل نوار اپل ودر)
    h_times = hourly.get("time", [])
    h_temps = hourly.get("temperature_2m", [])
    h_codes = hourly.get("weather_code", [])
    h_isday = hourly.get("is_day", [])

    cur_iso = str(cur.get("time", ""))
    cur_hour_idx = 0
    for idx, t_str in enumerate(h_times):
        if t_str >= cur_iso:
            cur_hour_idx = idx
            break

    timeline = []
    for i in range(cur_hour_idx, min(cur_hour_idx + 12, len(h_times))):
        dt_str = h_times[i]
        hr = dt_str[11:16] if len(dt_str) >= 16 else dt_str
        _, ic = _get_wmo(h_codes[i], h_isday[i])
        timeline.append({
            "hour": hr,
            "temp": round(h_temps[i]),
            "icon": ic
        })

    # تجزیه پیش‌بینی روزهای آینده
    days_list = []
    d_times = daily.get("time", [])
    for i in range(len(d_times)):
        day_code = daily.get("weather_code", [])[i]
        desc, ic = _get_wmo(day_code, 1)
        sr = (daily.get("sunrise", [])[i] or "")[11:16]
        ss = (daily.get("sunset", [])[i] or "")[11:16]

        days_list.append({
            "date": d_times[i],
            "tmax": daily.get("temperature_2m_max", [])[i],
            "tmin": daily.get("temperature_2m_min", [])[i],
            "day_cond": desc,
            "night_cond": desc,
            "icon": ic,
            "pop": daily.get("precipitation_probability_max", [])[i],
            "rain_mm": daily.get("precipitation_sum", [])[i],
            "wind": daily.get("wind_speed_10m_max", [])[i],
            "uv": daily.get("uv_index_max", [])[i],
            "sunrise": sr,
            "sunset": ss,
        })

    today_daily = days_list[0] if days_list else {}

    res_data = {
        "temp": cur.get("temperature_2m"),
        "feel": cur.get("apparent_temperature"),
        "tmax": today_daily.get("tmax"),
        "tmin": today_daily.get("tmin"),
        "condition": cond_desc,
        "icon": cond_icon,
        "humidity": cur.get("relative_humidity_2m"),
        "wind_speed": cur.get("wind_speed_10m"),
        "wind_dir": _wind_dir_fa(cur.get("wind_direction_10m")),
        "pressure": cur.get("surface_pressure"),
        "uv": today_daily.get("uv"),
        "aqi": aqi_val,
        "sunrise": today_daily.get("sunrise"),
        "sunset": today_daily.get("sunset"),
        "observed": cur_iso.replace("T", " "),
        "hourly_strip": timeline,
        "forecast": days_list,
        "source": SOURCE_NAME,
    }

    with _lock:
        _weather_cache[cache_key] = res_data

    return dict(res_data)


# ------------------------------------------------------------------
# سیستم تطبیق و Geocoding شهرها
# ------------------------------------------------------------------
def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه").replace("ة", "ه")
    s = re.sub(r"[\u064b-\u065f\u0640]", "", s)
    return re.sub(r"\s+", " ", s).strip()


_CITY_INDEX = {_norm(n): n for n in IRAN_CITIES}
_CITY_INDEX_NS = {k.replace(" ", ""): n for k, n in _CITY_INDEX.items()}


def resolve_city(query: str) -> str | None:
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


def _geocode(name: str):
    name_clean = re.sub(r"[^\w\s\u200c-]", " ", name or "", flags=re.UNICODE).strip()[:40]
    if len(name_clean) < 2:
        return None
    key = _norm(name_clean)
    with _lock:
        if key in _geo_cache:
            return _geo_cache[key]

    try:
        r = _session.get(OM_GEO_URL, params={"name": name_clean, "count": 1, "language": "fa", "format": "json"}, timeout=_TIMEOUT)
        if r.status_code == 200:
            res = _jsonlib.loads(r.content).get("results", [])
            if res:
                item = res[0]
                lat, lon = float(item["latitude"]), float(item["longitude"])
                lbl = str(item.get("name") or name_clean)
                with _lock:
                    _geo_cache[key] = (lat, lon, lbl)
                return lat, lon, lbl
    except Exception:
        pass
    return None


def fetch_weather(city_or_lat, lon: float | None = None, label: str | None = None) -> dict | None:
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

    data = _fetch_from_open_meteo(lat, lng)
    if not data:
        return None
    data["city"] = label or f"{lat:.2f}, {lng:.2f}"
    return data


# ------------------------------------------------------------------
# تقویم شمسی دقیق
# ------------------------------------------------------------------
_FA_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
_FA_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def _g2j(gy: int, gm: int, gd: int):
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
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


def _day_label(i: int, iso: str) -> tuple[str, str]:
    rel = {0: "امروز", 1: "فردا", 2: "پس‌فردا"}.get(i)
    try:
        d = _date.fromisoformat(iso)
        jy, jm, jd = _g2j(d.year, d.month, d.day)
        wd = _FA_WEEKDAYS[d.weekday()]
        md = f"{jd} {_FA_MONTHS[jm - 1]}"
        text = f"{wd} {md}" if rel else md
    except Exception:
        wd, text = iso, iso
    return (rel or wd), text


# ------------------------------------------------------------------
# قالب‌بندی نمایش مشابه رابط کاربری Apple Weather
# ------------------------------------------------------------------
_RLM = "\u200f"   # شروع هر خط با این علامت یعنی خط راست‌چین بماند
_LRM = "\u200e"   # دورِ عدد و متن لاتین می‌آید تا ترتیبش بهم نریزد

# اعداد فارسی در پیام کاربر (WEATHER_FA_DIGITS=0 برای برگشت به اعداد لاتین)
_FA_DIGITS_ON = (os.environ.get("WEATHER_FA_DIGITS", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
_FA_TR = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")


def _n(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return str(int(round(f))) if abs(f - round(f)) < 0.05 else f"{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


def _fa(v) -> str:
    """عدد/ساعت را برای نمایش فارسی آماده می‌کند (فقط خروجی کاربر؛ متن مخصوص هوش مصنوعی دست‌نخورده می‌ماند)."""
    s = str(v)
    return s.translate(_FA_TR) if _FA_DIGITS_ON else s


def _ltr(s) -> str:
    """یک تکهٔ چپ‌به‌راست (عدد + واحد) داخل متن راست‌چین؛ جلوی جابه‌جاشدن °C و % و ساعت را می‌گیرد."""
    return f"{_LRM}{s}{_LRM}"


def _val(v, unit: str = "") -> str:
    return _ltr(_fa(_n(v)) + unit)


# ------------------------------------------------------------------
# متن‌های قابل‌ویرایش از پنل ادمین (✏️ ویرایش پیام‌ها) — bot.py خودش این‌ها را ثبت می‌کند
# ------------------------------------------------------------------
_CARD_VARS_HELP = (
    "{city} {icon} {temp} {cond} {tmax} {tmin} {feel} {wind} {wind_dir} {humidity} {uv} "
    "{aqi_dot} {aqi_desc} {aqi} {sunrise} {sunset} {hourly_block} {forecast_block} {source} "
    "— HTML تلگرام مجاز است — خالی = متن پیش‌فرض"
)

_CARD_DEFAULT = (
    "📍 <b>{city}</b>\n"
    "{icon} <b>{temp}</b> · {cond}\n"
    "🌡 بیشینه: <b>{tmax}</b>  ┊  کمینه: <b>{tmin}</b>\n"
    "🧥 دمای حسی: <b>{feel}</b>\n"
    "\n"
    "{hourly_block}\n"
    "\n"
    "📊 <b>جزئیات وضعیت:</b>\n"
    "💨 باد: <b>{wind}</b> {wind_dir}\n"
    "💧 رطوبت: <b>{humidity}</b>\n"
    "☀️ شاخص فرابنفش: <b>{uv}</b>\n"
    "{aqi_dot} کیفیت هوا: <b>{aqi_desc}</b> ({aqi})\n"
    "🌅 طلوع آفتاب: <b>{sunrise}</b>\n"
    "🌇 غروب: <b>{sunset}</b>\n"
    "\n"
    "{forecast_block}\n"
    "\n"
    "<i>{source}</i>"
)

TEXT_DEFAULTS = {
    "weather_card": {
        "label": "آب‌وهوا — کارت اصلی /هوا",
        "vars": _CARD_VARS_HELP,
        "empty": "default",
        "default": _CARD_DEFAULT,
    },
    "weather_hourly_head": {
        "label": "آب‌وهوا — تیتر پیش‌بینی ساعت‌به‌ساعت",
        "vars": "بدون متغیر — HTML تلگرام مجاز است — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "🕒 <b>پیش‌بینی ساعت‌به‌ساعت:</b>",
    },
    "weather_forecast_head": {
        "label": "آب‌وهوا — تیتر پیش‌بینی روزهای آینده",
        "vars": "بدون متغیر — HTML تلگرام مجاز است — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "📅 <b>پیش‌بینی روزهای آینده:</b>",
    },
    "weather_not_found": {
        "label": "آب‌وهوا — شهر پیدا نشد",
        "vars": "{city} نام واردشده — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "❌ اطلاعات آب‌وهوا برای «{city}» پیدا نشد.",
    },
    "weather_no_city": {
        "label": "آب‌وهوا — کاربر هنوز شهری ثبت نکرده",
        "vars": "بدون متغیر — HTML تلگرام مجاز است — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "📍 هنوز شهری برات ثبت نشده. نام شهرت رو بعد از دستور بنویس:\nمثال: <code>/هوا شیراز</code>",
    },
    "weather_fetch_error": {
        "label": "آب‌وهوا — خطا در دریافت اطلاعات",
        "vars": "بدون متغیر — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "⚠️ خطایی در ارتباط با ایستگاه هواشناسی رخ داد. کمی بعد تلاش کن.",
    },
    "weather_change_hint": {
        "label": "آب‌وهوا — راهنمای تغییر شهر (/هوا تغییر)",
        "vars": "بدون متغیر — خالی = متن پیش‌فرض",
        "empty": "default",
        "default": "📍 شهرت رو از بخش «تنظیم پرامپت شخصی» در پیوی مشخص کن.",
    },
}

_text_fn = None


def set_text_provider(fn):
    """bot.py اینجا render_text_or_default را می‌دهد تا متن‌های ویرایش‌شده از دیتابیس خوانده شوند."""
    global _text_fn
    _text_fn = fn


class _SafeFmt(dict):
    def __missing__(self, k):
        return ""


def _t(key: str, **kw) -> str:
    safe = {k: ("" if v is None else v) for k, v in kw.items()}
    if _text_fn is not None:
        try:
            out = _text_fn(key, **safe)
            if out and str(out).strip():
                return str(out)
        except Exception as e:
            logger.warning("[WEATHER] text provider error (%s): %s", key, e)
    default = TEXT_DEFAULTS[key]["default"]
    try:
        return default.format_map(_SafeFmt(safe))
    except Exception:
        return default


def _finish_card(text: str) -> str:
    """هر خطِ غیرخالی با RLM شروع می‌شود (راست‌چین)، فاصله‌های اضافهٔ آخر خط و خط‌های خالی پشت‌سرهم تمیز می‌شوند."""
    out = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        ln = ln.rstrip()
        if ln.strip() and not ln.startswith(_RLM):
            ln = _RLM + ln
        out.append(ln)
    res = "\n".join(out)
    res = re.sub(r"\n{3,}", "\n\n", res)
    return res.strip()


def _card_vars(city_name: str, data: dict, future: bool, days: int | None) -> dict:
    R = _RLM
    wnd = data.get("wind_speed")
    aqi_num = data.get("aqi")
    aqi_desc, aqi_dot = _aqi_status(aqi_num)
    if _FA_DIGITS_ON:
        wind_txt = f"{_fa(_n(wnd))} کیلومتر بر ساعت"
    else:
        wind_txt = _val(wnd, " km/h")

    # پیش‌بینی ساعت‌به‌ساعت (هر دو ساعت یک‌بار؛ ردیف‌های سه‌تایی)
    hourly_block = ""
    timeline = data.get("hourly_strip") or []
    if timeline:
        rows = [_t("weather_hourly_head")]
        cells = [f"{_ltr(_fa(h['hour']))} {h['icon']} <b>{_val(h['temp'], '°')}</b>" for h in timeline[::2]]
        for i in range(0, len(cells), 3):
            rows.append(f"{R}" + "  ┊  ".join(cells[i:i + 3]))
        hourly_block = "\n".join(rows)

    # پیش‌بینی روزهای آینده — هر روز دو خط
    forecast_block = ""
    fc = data.get("forecast") or []
    n_days = days if days is not None else (7 if future else 3)
    if fc:
        rows = [_t("weather_forecast_head")]
        for i, d in enumerate(fc[:n_days]):
            rel, when = _day_label(i, d.get("date", ""))
            d_icon = d.get("icon") or "🌤"
            pop = d.get("pop")
            rain_txt = f"   ☔ <b>{_val(pop, '%')}</b>" if (pop and pop > 15) else ""
            rows.append(f"{R}• <b>{html.escape(str(rel))}</b> ({_fa(html.escape(str(when)))})")
            rows.append(
                f"{R}\u2003{d_icon} <b>{_val(d.get('tmin'), '°')}</b> تا <b>{_val(d.get('tmax'), '°C')}</b>{rain_txt}"
            )
        forecast_block = "\n".join(rows)

    return {
        "city": html.escape(str(city_name)),
        "icon": data.get("icon") or "🌤",
        "cond": html.escape(str(data.get("condition") or "صاف")),
        "temp": _val(data.get("temp"), "°C"),
        "tmax": _val(data.get("tmax"), "°"),
        "tmin": _val(data.get("tmin"), "°"),
        "feel": _val(data.get("feel"), "°"),
        "wind": wind_txt,
        "wind_dir": data.get("wind_dir") or "",
        "humidity": _val(data.get("humidity"), "%"),
        "uv": _ltr("UV " + _fa(_n(data.get("uv")))),
        "aqi_dot": aqi_dot,
        "aqi_desc": aqi_desc,
        "aqi": _ltr(_fa(aqi_num)) if aqi_num is not None else "—",
        "sunrise": _ltr(_fa(data.get("sunrise") or "—")),
        "sunset": _ltr(_fa(data.get("sunset") or "—")),
        "hourly_block": hourly_block,
        "forecast_block": forecast_block,
        "source": _ltr(html.escape(str(data.get("source") or SOURCE_NAME))),
    }


def format_weather_message(city_name: str, data: dict, future: bool = False, days: int | None = None) -> str:
    """کارت آب‌وهوا: قالب از پنل ادمین (کلید weather_card) خوانده می‌شود و متغیرها داخلش جایگزین می‌شوند.
    اگر قالب خالی/خراب بود، قالب پیش‌فرض استفاده می‌شود."""
    v = _card_vars(city_name, data, future, days)
    return _finish_card(_t("weather_card", **v))


def format_weather_for_user(data: dict, future: bool = False) -> str:
    return format_weather_message(data.get("city") or "—", data, future=future)


def format_weather_for_ai(data: dict) -> str:
    """خروجی فشرده و دقیق برای کانتکست هوش مصنوعی ربات."""
    lines = [
        f"شهر: {data.get('city') or '—'}",
        f"دمای فعلی: {_n(data.get('temp'))}°C (دمای حسی: {_n(data.get('feel'))}°C)",
        f"بیشینه و کمینه امروز: {_n(data.get('tmin'))}°C تا {_n(data.get('tmax'))}°C",
        f"وضعیت جوی: {data.get('condition') or '—'}",
        f"رطوبت: {_n(data.get('humidity'))}% | سرعت باد: {_n(data.get('wind_speed'))} km/h {data.get('wind_dir') or ''}",
        f"شاخص فرابنفش: UV {_n(data.get('uv'))} | شاخص کیفیت هوا (AQI): {data.get('aqi') or 'نامشخص'}",
        f"طلوع آفتاب: {data.get('sunrise') or '—'} | غروب آفتاب: {data.get('sunset') or '—'}",
    ]
    fc = data.get("forecast") or []
    if fc:
        lines.append("پیش‌بینی روزهای آینده:")
        for i, d in enumerate(fc[:7]):
            rel, when = _day_label(i, d.get("date", ""))
            lines.append(f"{rel} ({when}): {_n(d.get('tmin'))}° تا {_n(d.get('tmax'))}°C - {d.get('day_cond')} (احتمال بارش: {d.get('pop') or 0}%)")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------
# هندلرهای ربات تلگرام (/هوا ، /weather)
# ------------------------------------------------------------------
try:
    WEATHER_AUTO_DELETE_SEC = float(os.environ.get("WEATHER_AUTO_DELETE_SEC", "120") or 120)
except ValueError:
    WEATHER_AUTO_DELETE_SEC = 120.0

WEATHER_AUTO_DELETE_PRIVATE = (os.environ.get("WEATHER_AUTO_DELETE_PRIVATE", "0") or "0").strip().lower() in ("1", "true", "yes", "on")
_CHANGE_WORDS = frozenset(("تغییر", "تغییر شهر", "شهر من", "شهرمن", "change", "set"))

def register_weather_handlers(bot, spam_check=None, get_location=None, save_location=None):
    _per_user = get_location is not None

    def _spam(chat_id) -> bool:
        if spam_check is None:
            return False
        try:
            return bool(spam_check(chat_id))
        except Exception:
            return False

    def _try_delete(chat_id, message_id) -> bool:
        if not message_id or message_id <= 0:
            return False
        try:
            bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            return False

    def _autodel(chat, *message_ids):
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
        sent = bot.reply_to(message, text, **kw)
        _autodel(message.chat, message.message_id, getattr(sent, "message_id", None))
        return sent

    def _send_weather(message, w_data):
        _reply(message, format_weather_for_user(w_data), parse_mode="HTML")

    def _show_for_query(message, city_query: str):
        w_data = fetch_weather(city_query)
        if not w_data:
            _reply(message, _t("weather_not_found", city=html.escape(city_query[:40])), parse_mode="HTML")
            return
        _send_weather(message, w_data)

    @bot.message_handler(commands=["هوا", "weather", "hava"])
    def cmd_weather(message):
        if _spam(message.chat.id):
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not _per_user or not message.from_user:
            _show_for_query(message, arg or "تهران")
            return

        uid = message.from_user.id
        if arg and _norm(arg) in {_norm(w) for w in _CHANGE_WORDS}:
            _reply(message, _t("weather_change_hint"), parse_mode="HTML")
            return
        if arg:
            _show_for_query(message, arg)
            return

        try:
            loc = get_location(uid)
        except Exception as e:
            logger.warning("[WEATHER] get_location error: %s", e)
            loc = None

        if not loc:
            _reply(message, _t("weather_no_city"), parse_mode="HTML")
            return

        w_data = fetch_weather(loc["lat"], loc["lon"], label=loc["city"])
        if not w_data:
            _reply(message, _t("weather_fetch_error"), parse_mode="HTML")
            return
        _send_weather(message, w_data)
