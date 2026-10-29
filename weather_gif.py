# -*- coding: utf-8 -*-
"""
weather_gif.py — ساخت گیف انیمیشنی آب‌وهوا به سبک Apple Weather

ورودی: همان دیکشنری‌ای که weather_service.fetch_weather() برمی‌گرداند.
خروجی: بایت‌های یک GIF لوپ‌شونده (۴۸۰×۸۶۰) که بالایش صحنهٔ متحرک آسمان است
(خورشید و پرتوهای چرخان / ماه و ستارهٔ چشمک‌زن / ابرهای شناور / باران / رعد و برق + فلاش / برف / مه)
و زیرش کارت‌های شیشه‌ای «ساعت‌به‌ساعت»، «روزهای آینده» و «جزئیات».

فقط به Pillow نیاز دارد. برای نوشتن درست فارسی یکی از این دو لازم است:
  ۱) Pillow با raqm  (روی لینوکس: apt install libraqm0 libfribidi0)
  ۲) pip install arabic-reshaper python-bidi
فونت: بهتر است Vazirmatn-Regular.ttf و Vazirmatn-Bold.ttf را کنار این فایل یا داخل پوشهٔ fonts/ بگذاری
(یا مسیرش را در WEATHER_FONT / WEATHER_FONT_BOLD بدهی)؛ وگرنه فونت‌های سیستم (DejaVu / Noto) استفاده می‌شوند.
"""
from __future__ import annotations

import glob
import io
import logging
import math
import os
import random
import re
from datetime import date as _date

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

logger = logging.getLogger("ChandBot.weather.gif")

# ------------------------------------------------------------------
# ابعاد و زمان‌بندی
# ------------------------------------------------------------------
W, H = 480, 896          # ابعاد کل گیف
SCENE_H = 470            # ارتفاعی که انیمیشن آسمان تا آنجا کشیده می‌شود
N_FRAMES = 30            # تعداد فریم یک چرخهٔ کامل (لوپ بی‌درز)
FRAME_MS = 70            # مدت هر فریم (~۱۴ فریم بر ثانیه)

_FA_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
_TAU = math.tau


# ------------------------------------------------------------------
# فونت و شکل‌دهی متن فارسی
# ------------------------------------------------------------------
_RAQM = bool(features.check("raqm"))
_AR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")

try:                                    # فالبک وقتی raqm نیست
    import arabic_reshaper as _ar_reshaper
    from bidi.algorithm import get_display as _bidi_display
    _HAS_RESHAPER = True
except Exception:
    _ar_reshaper = _bidi_display = None
    _HAS_RESHAPER = False

_REG_NAMES = ("Vazirmatn-Regular", "Vazirmatn-Medium", "Vazirmatn[wght]", "Vazir", "Vazir-Regular", "Shabnam", "Sahel",
              "IRANSans", "NotoSansArabic-Regular", "NotoSansArabicUI-Regular", "NotoNaskhArabic-Regular", "DejaVuSans")
_BOLD_NAMES = ("Vazirmatn-Bold", "Vazirmatn-SemiBold", "Vazirmatn[wght]", "Vazir-Bold", "Shabnam-Bold", "Sahel-Bold",
               "IRANSans-Bold", "NotoSansArabic-Bold", "NotoSansArabicUI-Bold", "NotoNaskhArabic-Bold", "DejaVuSans-Bold")
_FONT_PATH_CACHE: dict = {}
_FONT_CACHE: dict = {}


def _font_dirs() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [here, os.path.join(here, "fonts"), os.path.join(os.getcwd(), "fonts"), os.getcwd()]
    dirs += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
             os.path.expanduser("~/.local/share/fonts")]
    return dirs


def _find_font(bold: bool):
    key = "bold" if bold else "reg"
    if key in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[key]
    env = os.environ.get("WEATHER_FONT_BOLD" if bold else "WEATHER_FONT", "").strip()
    found = env if env and os.path.isfile(env) else None
    if not found:
        names = _BOLD_NAMES if bold else _REG_NAMES
        for nm in names:
            for d in _font_dirs():
                if not os.path.isdir(d):
                    continue
                direct = os.path.join(d, nm + ".ttf")
                if os.path.isfile(direct):
                    found = direct
                    break
                try:
                    hits = glob.glob(os.path.join(glob.escape(d), "**", glob.escape(nm) + ".[to]tf"), recursive=True)
                except Exception:
                    hits = []
                if hits:
                    found = hits[0]
                    break
            if found:
                break
    if not found:
        logger.warning("[WEATHER-GIF] فونت فارسی پیدا نشد؛ Vazirmatn-Bold.ttf را کنار weather_gif.py بگذار.")
    _FONT_PATH_CACHE[key] = found
    return found


def _font(size: int, bold: bool = False):
    key = (size, bold)
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    path = _find_font(bold)
    try:
        if path:
            kw = {"layout_engine": ImageFont.Layout.RAQM} if _RAQM else {}
            f = ImageFont.truetype(path, size, **kw)
            if "[wght]" in os.path.basename(path):          # فونت متغیر (Vazirmatn[wght].ttf)
                try:
                    f.set_variation_by_axes([700 if bold else 400])
                except Exception:
                    pass
        else:
            f = ImageFont.load_default()
    except Exception as e:
        logger.warning("[WEATHER-GIF] بارگذاری فونت ناموفق: %s", e)
        f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


_NUM_RE = re.compile(r"[-+\u2212]?\d+(?:[.,]\d+)?(?:\s?(?:°C?|%|km/h))?")


def _prep(s: str):
    """(متن آمادهٔ نقاشی، kwargs برای draw.text/textlength)"""
    s = str(s)
    if not _AR_RE.search(s):
        return s, {}
    if _RAQM:
        s = _NUM_RE.sub(lambda m: "\u2066" + m.group(0) + "\u2069", s)      # عدد+° داخل جملهٔ فارسی چپ‌به‌راست بماند
        return s, {"direction": "rtl", "language": "fa"}
    if _HAS_RESHAPER:
        try:
            return _bidi_display(_ar_reshaper.reshape(s)), {}
        except Exception:
            pass
    return s, {}


def _tlen(d, s, font) -> float:
    t, kw = _prep(s)
    try:
        return d.textlength(t, font=font, **kw)
    except Exception:
        return d.textlength(t, font=font)


def _text(d, x, y, s, font, fill=(255, 255, 255, 255), anchor="m"):
    """متن را با «x افقی (l/m/r)» و «y مرکز عمودی» می‌نویسد."""
    t, kw = _prep(s)
    w = _tlen(d, s, font)
    left = x - w if anchor == "r" else (x - w / 2 if anchor == "m" else x)
    try:
        d.text((left, y), t, font=font, fill=fill, anchor="lm", **kw)
    except Exception:
        d.text((left, y), t, font=font, fill=fill)
    return w


# ------------------------------------------------------------------
# تشخیص نوع صحنه از کد WMO
# ------------------------------------------------------------------
def _scene_from_code(code: int):
    """→ (kind, heavy)  kind ∈ clear/partly/cloudy/fog/rain/storm/snow"""
    c = int(code)
    if c in (0, 1):
        return "clear", False
    if c == 2:
        return "partly", False
    if c == 3:
        return "cloudy", False
    if c in (45, 48):
        return "fog", False
    if c in (51, 53, 55, 56, 57, 61, 80):
        return "rain", False
    if c in (63, 65, 66, 67, 81):
        return "rain", True
    if c == 82 or c >= 95:
        return "storm", True
    if c in (71, 73, 75, 77, 85, 86):
        return "snow", c in (75, 86)
    return "clear", False


def _icon_from_code(code: int) -> str:
    c = int(code)
    if c == 0:
        return "clear"
    if c in (1, 2):
        return "partly"
    if c == 3:
        return "cloud"
    if c in (45, 48):
        return "fog"
    if c in (71, 73, 75, 77, 85, 86):
        return "snow"
    if c == 82 or c >= 95:
        return "storm"
    return "rain"


def _icon_from_emoji(e: str) -> str:
    e = e or ""
    if "⛈" in e:
        return "storm"
    if "🌨" in e or "❄" in e:
        return "snow"
    if "🌧" in e or "🌦" in e:
        return "rain"
    if "🌫" in e:
        return "fog"
    if "⛅" in e or "🌤" in e:
        return "partly"
    if "☁" in e:
        return "cloud"
    return "clear"


_KIND_FROM_ICON = {"clear": ("clear", False), "partly": ("partly", False), "cloud": ("cloudy", False),
                   "fog": ("fog", False), "rain": ("rain", False), "storm": ("storm", True), "snow": ("snow", False)}


def scene_kind(data: dict):
    """→ (kind, is_day, heavy)"""
    is_day = 1 if data.get("is_day", 1) in (1, True, None) else 0
    code = data.get("code")
    if code is not None:
        kind, heavy = _scene_from_code(code)
    else:
        kind, heavy = _KIND_FROM_ICON[_icon_from_emoji(data.get("icon") or "")]
    return kind, is_day, heavy


# ------------------------------------------------------------------
# ابزارهای تصویری (فقط Pillow)
# ------------------------------------------------------------------
def _clamp(v, a=0.0, b=1.0):
    return a if v < a else (b if v > b else v)


def _mix(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _vgrad(w, h, top, bot):
    mask = Image.linear_gradient("L").resize((w, h), Image.BILINEAR)
    return Image.composite(Image.new("RGB", (w, h), bot), Image.new("RGB", (w, h), top), mask)


def _glow(size, color, strength=1.0, power=2.0):
    g = Image.radial_gradient("L").resize((size, size), Image.BILINEAR)
    # در Pillow مقدار گرادیان وسط لبه‌ها ≈۱۸۱ است (۲۵۵ فقط در گوشه‌ها)؛ بدون این نرمال‌سازی لبهٔ مربعی دیده می‌شود
    a = g.point(lambda v: int(255 * strength * (1.0 - min(1.0, v / 181.0)) ** power))
    sp = Image.new("RGBA", (size, size), tuple(color) + (0,))
    sp.putalpha(a)
    return sp


def _disc(r, c_in, c_out, ss=3):
    s = 2 * r
    big = s * ss
    rad = Image.radial_gradient("L").resize((big, big), Image.BILINEAR).point(lambda v: min(255, int(v * 255 / 181)))
    col = Image.composite(Image.new("RGB", (big, big), c_out), Image.new("RGB", (big, big), c_in), rad)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    out = col.convert("RGBA")
    out.putalpha(mask)
    return out.resize((s, s), Image.LANCZOS)


def _cloud(w, h, top, bot, alpha=255):
    """ابر نرم و پفی با سایهٔ بالا→پایین."""
    S = 3
    W2, H2 = w * S, h * S
    m = Image.new("L", (W2, H2), 0)
    d = ImageDraw.Draw(m)
    bh = int(H2 * 0.46)
    d.rounded_rectangle((int(W2 * 0.03), H2 - bh, int(W2 * 0.97), H2 - 1), radius=bh // 2, fill=255)

    def puff(cx, cy, r):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)

    puff(W2 * 0.29, H2 * 0.57, H2 * 0.40)
    puff(W2 * 0.52, H2 * 0.47, H2 * 0.46)
    puff(W2 * 0.75, H2 * 0.61, H2 * 0.34)
    m = m.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8))
    if alpha < 255:
        m = m.point(lambda v: v * alpha // 255)
    out = _vgrad(w, h, top, bot).convert("RGBA")
    out.putalpha(m)
    return out


def _over(im, sprite, xy):
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    layer.paste(sprite, xy)
    return Image.alpha_composite(im, layer)


def _paste(frame, sprite, cx, cy, scale=1.0):
    """sprite (RGBA) را با مرکز (cx,cy) روی فریم RGB می‌چسباند؛ scale = ضریب شفافیت."""
    a = sprite.getchannel("A")
    if scale < 0.999:
        a = a.point(lambda v: int(v * scale))
    frame.paste(sprite, (int(cx - sprite.width / 2), int(cy - sprite.height / 2)), a)


# ------------------------------------------------------------------
# آیکن‌های کوچک (ساعتی/روزانه) — با فونت ایموجی کاری نداریم، خودمان می‌کشیم
# ------------------------------------------------------------------
_ICON_CACHE: dict = {}


def _icon(kind: str, is_day: int, size: int):
    key = (kind, is_day, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    S = 4
    n = size * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))

    def sun(im, cx, cy, r, rays=True):
        d = ImageDraw.Draw(im)
        if rays:
            for i in range(8):
                a = i * math.pi / 4
                d.line((cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45,
                        cx + math.cos(a) * r * 1.85, cy + math.sin(a) * r * 1.85),
                       fill=(255, 206, 64, 255), width=max(2, int(r * 0.24)))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 196, 50, 255))
        d.ellipse((cx - r * 0.85, cy - r * 0.85, cx + r * 0.55, cy + r * 0.55), fill=(255, 224, 108, 255))
        return im

    def moon(im, cx, cy, r):
        m = Image.new("L", im.size, 0)
        md = ImageDraw.Draw(m)
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        md.ellipse((cx + 0.30 * r - r * 0.9, cy - 0.40 * r - r * 0.9, cx + 0.30 * r + r * 0.9, cy - 0.40 * r + r * 0.9), fill=0)
        col = Image.new("RGBA", im.size, (255, 236, 168, 255))
        col.putalpha(m)
        return Image.alpha_composite(im, col)

    def cl(im, x, y, w, h, top=(255, 255, 255), bot=(205, 218, 236)):
        return _over(im, _cloud(int(w), int(h), top, bot), (int(x), int(y)))

    if kind == "clear":
        im = sun(im, n * 0.5, n * 0.5, n * 0.2) if is_day else moon(im, n * 0.5, n * 0.5, n * 0.30)
    elif kind == "partly":
        im = sun(im, n * 0.36, n * 0.34, n * 0.15) if is_day else moon(im, n * 0.36, n * 0.34, n * 0.24)
        im = cl(im, n * 0.16, n * 0.40, n * 0.80, n * 0.46)
    elif kind == "cloud":
        im = cl(im, n * 0.06, n * 0.26, n * 0.88, n * 0.52, (246, 248, 252), (196, 207, 224))
    elif kind == "fog":
        d = ImageDraw.Draw(im)
        for i, (xa, xb) in enumerate(((0.16, 0.84), (0.26, 0.92), (0.10, 0.74), (0.22, 0.80))):
            y = n * (0.26 + i * 0.16)
            d.rounded_rectangle((n * xa, y - n * 0.035, n * xb, y + n * 0.035), radius=n * 0.035, fill=(228, 235, 244, 255))
    else:
        top, bot = ((196, 206, 222), (128, 140, 160)) if kind != "storm" else ((150, 156, 176), (88, 94, 118))
        im = cl(im, n * 0.06, n * 0.10, n * 0.88, n * 0.52, top, bot)
        d = ImageDraw.Draw(im)
        if kind == "rain":
            for x in (0.30, 0.50, 0.70):
                d.line((n * x, n * 0.68, n * (x - 0.06), n * 0.90), fill=(104, 176, 255, 255), width=max(3, int(n * 0.055)))
        elif kind == "snow":
            for x, y in ((0.30, 0.76), (0.50, 0.88), (0.70, 0.76)):
                r = n * 0.045
                d.ellipse((n * x - r, n * y - r, n * x + r, n * y + r), fill=(255, 255, 255, 255))
        elif kind == "storm":
            pts = [(0.53, 0.58), (0.40, 0.80), (0.50, 0.80), (0.44, 0.97), (0.67, 0.71), (0.55, 0.71), (0.63, 0.58)]
            d.polygon([(n * x, n * y) for x, y in pts], fill=(255, 214, 60, 255))
    out = im.resize((size, size), Image.LANCZOS)
    _ICON_CACHE[key] = out
    return out


# ------------------------------------------------------------------
# پالت آسمان و ابرها
# ------------------------------------------------------------------
_SKY = {
    ("clear", 1): ((34, 114, 232), (122, 198, 250)), ("clear", 0): ((7, 11, 34), (34, 48, 98)),
    ("partly", 1): ((58, 126, 222), (140, 192, 240)), ("partly", 0): ((13, 19, 46), (46, 60, 106)),
    ("cloudy", 1): ((98, 118, 144), (166, 182, 202)), ("cloudy", 0): ((28, 34, 50), (64, 74, 96)),
    ("fog", 1): ((136, 148, 164), (196, 202, 212)), ("fog", 0): ((42, 48, 62), (84, 92, 106)),
    ("rain", 1): ((54, 68, 92), (108, 124, 148)), ("rain", 0): ((20, 26, 40), (50, 60, 80)),
    ("storm", 1): ((32, 36, 58), (80, 84, 114)), ("storm", 0): ((14, 16, 30), (46, 48, 76)),
    ("snow", 1): ((116, 138, 166), (194, 208, 224)), ("snow", 0): ((36, 46, 70), (84, 98, 126)),
}


def _cloud_tints(kind, is_day):
    if kind in ("clear", "partly"):
        return ((255, 255, 255), (212, 225, 243)) if is_day else ((152, 162, 194), (96, 106, 142))
    if kind == "cloudy":
        return ((236, 241, 248), (182, 195, 212)) if is_day else ((122, 132, 156), (72, 80, 102))
    if kind == "rain":
        return ((172, 182, 198), (108, 118, 138)) if is_day else ((98, 106, 128), (56, 62, 82))
    if kind == "storm":
        return ((122, 128, 152), (64, 68, 92)) if is_day else ((86, 90, 116), (44, 46, 70))
    if kind == "snow":
        return ((228, 236, 246), (176, 190, 210)) if is_day else ((132, 144, 170), (86, 98, 124))
    return ((230, 236, 244), (190, 198, 210))


# (x, y, w, h, ampX, ampY, phase, layer)
_CLOUDS = {
    "clear": [(-26, 96, 138, 66, 9, 3, 0.4, "back")],
    "partly": [(262, 112, 260, 118, 12, 4, 0.5, "front"), (-34, 196, 236, 100, 10, 3, 2.0, "back"),
               (118, 52, 176, 84, 8, 3, 3.5, "back")],
    "cloudy": [(-24, 54, 300, 132, 14, 4, 0.0, "back"), (226, 26, 288, 124, 12, 4, 1.3, "back"),
               (58, 168, 334, 142, 16, 5, 2.6, "front"), (300, 206, 262, 112, 12, 4, 4.0, "front"),
               (-64, 252, 280, 112, 10, 3, 5.1, "back")],
    "rain": [(-34, 36, 334, 142, 14, 4, 0.2, "back"), (206, 6, 304, 132, 12, 4, 1.6, "back"),
             (78, 108, 364, 152, 16, 5, 3.0, "front"), (326, 148, 236, 102, 10, 3, 4.4, "front")],
    "snow": [(-30, 44, 320, 136, 12, 4, 0.3, "back"), (210, 18, 296, 126, 12, 4, 1.7, "back"),
             (70, 116, 350, 146, 14, 5, 3.2, "front")],
    "fog": [],
}
_CLOUDS["storm"] = _CLOUDS["rain"]


# ------------------------------------------------------------------
# صحنهٔ متحرک
# ------------------------------------------------------------------
class _Scene:
    def __init__(self, kind: str, is_day: int, heavy: bool, seed: int = 7):
        self.kind, self.is_day, self.heavy = kind, is_day, heavy
        self.rnd = random.Random(seed)
        top, bot = _SKY[(kind, is_day)]
        bg = _vgrad(W, H, top, bot)
        noise = Image.effect_noise((W, H), 2.4)                 # دیتر ثابت: بَندینگ گرادیان گیف را می‌گیرد و حجم را بالا نمی‌برد
        self.bg = ImageChops.add(bg, Image.merge("RGB", (noise, noise, noise)), 1.0, -128)
        self.sun_xy = (W - 108, 120)

        ct, cb = _cloud_tints(kind, is_day)
        self.clouds = []
        for (x, y, w, h, ax, ay, ph, layer) in _CLOUDS.get(kind, []):
            self.clouds.append((_cloud(w, h, ct, cb), x, y, ax, ay, ph, layer))

        self.has_sun = kind in ("clear", "partly") and is_day
        self.has_moon = kind in ("clear", "partly") and not is_day
        if self.has_sun:
            self.sun_glow = _glow(460, (255, 236, 150), 0.95, 2.2)
            self.sun_glow2 = _glow(190, (255, 250, 214), 1.0, 1.6)
            self.sun_body = _disc(54, (255, 252, 214), (255, 196, 64))
            self.rays = self._make_rays()
        if self.has_moon:
            self.moon_glow = _glow(400, (176, 198, 255), 0.7, 2.4)
            self.moon_body = self._make_moon(46)
        if (self.has_moon or kind in ("cloudy", "snow")) and not is_day:
            self.stars = [(self.rnd.uniform(6, W - 6), self.rnd.uniform(8, 330), self.rnd.uniform(0.9, 2.0),
                           self.rnd.uniform(0, _TAU), self.rnd.choice((1, 1, 2)), self.rnd.random() < 0.12)
                          for _ in range(70 if self.has_moon else 24)]
        else:
            self.stars = []

        self.P = SCENE_H + 70
        if kind in ("rain", "storm"):
            n = 215 if heavy else 120
            self.drops = []
            for _ in range(n):
                m = self.rnd.choice((2, 2, 3)) if heavy else self.rnd.choice((1, 1, 2))
                L = {1: self.rnd.uniform(15, 22), 2: self.rnd.uniform(26, 34), 3: self.rnd.uniform(40, 50)}[m]
                a = {1: self.rnd.uniform(70, 105), 2: self.rnd.uniform(110, 150), 3: self.rnd.uniform(130, 170)}[m]
                self.drops.append((self.rnd.uniform(-20, W + 20), self.rnd.uniform(0, self.P), m, L, a, 2 if m == 3 else 1))
        if kind == "snow":
            self.tile = 250
            n = 85 if heavy else 60
            self.flakes = [(self.rnd.uniform(0, W), self.rnd.uniform(0, self.tile), self.rnd.choice((1, 1, 2)),
                            self.rnd.uniform(1.4, 3.6), self.rnd.uniform(0, _TAU), self.rnd.choice((1, 2)),
                            self.rnd.uniform(8, 18)) for _ in range(n)]
        if kind == "fog":
            band = Image.new("L", (520, 90), 0)
            ImageDraw.Draw(band).ellipse((20, 14, 500, 76), fill=255)
            band = band.filter(ImageFilter.GaussianBlur(20))
            spr = Image.new("RGBA", band.size, (238, 242, 247, 0))
            spr.putalpha(band.point(lambda v: int(v * 0.62)))
            self.fog_band = spr
            self.fog_rows = [(70 + i * 78, (1 if i % 2 == 0 else -1) * 34, self.rnd.uniform(0, _TAU), -40 + i * 60) for i in range(6)]
        if kind == "storm":
            self.bolts = {"A": self._bolt(random.Random(11), 150), "B": self._bolt(random.Random(5), 335)}
            # فریم → (شدت فلاش، کدام صاعقه)
            self.flash = {6: (0.55, "A"), 7: (0.10, None), 8: (0.42, "A"), 9: (0.14, "A"),
                          20: (0.50, "B"), 21: (0.10, None), 22: (0.34, "B")}
            self.flash_mask = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize((W, 600), Image.BILINEAR)

    # --- ساخت اجزا ---------------------------------------------------
    def _make_rays(self):
        S = 2
        size = 460 * S
        m = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(m)
        cx = cy = size / 2
        for i in range(12):
            for j, (r0, r1, hw, al) in enumerate(((66, 190, 9, 70), (66, 132, 8, 52))):
                a = (i * 30 + j * 15) * math.pi / 180
                p0 = (cx + math.cos(a) * r0 * S, cy + math.sin(a) * r0 * S)
                nx, ny = -math.sin(a), math.cos(a)
                pl = (cx + math.cos(a) * r1 * S + nx * hw * S * 0.25, cy + math.sin(a) * r1 * S + ny * hw * S * 0.25)
                pr = (cx + math.cos(a) * r1 * S - nx * hw * S * 0.25, cy + math.sin(a) * r1 * S - ny * hw * S * 0.25)
                b0 = (p0[0] + nx * hw * S, p0[1] + ny * hw * S)
                b1 = (p0[0] - nx * hw * S, p0[1] - ny * hw * S)
                d.polygon([b0, pl, pr, b1], fill=al)
        m = m.filter(ImageFilter.GaussianBlur(5.0 * S)).resize((460, 460), Image.LANCZOS)
        im = Image.new("RGBA", (460, 460), (255, 244, 196, 0))
        im.putalpha(m)
        return im

    def _make_moon(self, r):
        S = 3
        n = r * 2 * S
        m = Image.new("L", (n, n), 0)
        d = ImageDraw.Draw(m)
        cx = cy = n / 2
        d.ellipse((cx - r * S, cy - r * S, cx + r * S, cy + r * S), fill=255)
        o = r * S * 0.42
        d.ellipse((cx + o - r * S * 0.92, cy - o * 0.8 - r * S * 0.92, cx + o + r * S * 0.92, cy - o * 0.8 + r * S * 0.92), fill=0)
        m = m.resize((r * 2, r * 2), Image.LANCZOS)
        col = _vgrad(r * 2, r * 2, (255, 248, 214), (236, 220, 160)).convert("RGBA")
        col.putalpha(m)
        return col

    def _bolt(self, rnd, x0):
        pts, x, y = [(x0, 20)], float(x0), 20.0
        while y < 330:
            y += rnd.uniform(26, 46)
            x += rnd.uniform(-26, 26)
            pts.append((x, y))
        branch = []
        bi = rnd.randint(2, len(pts) - 3)
        bx, by = pts[bi]
        for _ in range(3):
            by += rnd.uniform(22, 36)
            bx += rnd.uniform(8, 30)
            branch.append((bx, by))
        return pts, [pts[bi]] + branch

    # --- یک فریم -----------------------------------------------------
    def frame(self, k: int):
        t = k / N_FRAMES
        fr = self.bg.copy()
        d = ImageDraw.Draw(fr, "RGBA")
        sx, sy = self.sun_xy

        if self.has_sun:
            pulse = 0.82 + 0.18 * math.sin(_TAU * t)
            _paste(fr, self.sun_glow, sx, sy, 0.70 * pulse)
            _paste(fr, self.sun_glow2, sx, sy, pulse)
            ray = self.rays.rotate(-30.0 * t, resample=Image.BICUBIC)
            _paste(fr, ray, sx, sy, 0.8 + 0.2 * math.sin(_TAU * t + 1.2))
            _paste(fr, self.sun_body, sx, sy)
        if self.has_moon:
            _paste(fr, self.moon_glow, sx, sy, 0.75 + 0.15 * math.sin(_TAU * t))
            _paste(fr, self.moon_body, sx, sy)
        for (x, y, r, ph, f, sparkle) in self.stars:
            a = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(_TAU * t * f + ph))
            if (x - sx) ** 2 + (y - sy) ** 2 < 70 ** 2:
                continue
            col = (255, 255, 255, int(225 * a))
            if sparkle:
                ln = 3.5 + 2.5 * a
                d.line((x - ln, y, x + ln, y), fill=col, width=1)
                d.line((x, y - ln, x, y + ln), fill=col, width=1)
            d.ellipse((x - r * 0.6, y - r * 0.6, x + r * 0.6, y + r * 0.6), fill=col)

        if self.kind == "fog":
            for (y, amp, ph, x0) in self.fog_rows:
                x = x0 + amp * math.sin(_TAU * t + ph)
                fr.paste(self.fog_band, (int(x), int(y)), self.fog_band.getchannel("A"))

        def draw_clouds(layer):
            for (sp, x, y, ax, ay, ph, lay) in self.clouds:
                if lay != layer:
                    continue
                cx = x + ax * math.sin(_TAU * t + ph)
                cy = y + ay * math.sin(_TAU * t * 1 + ph * 1.3)
                fr.paste(sp, (int(cx), int(cy)), sp.getchannel("A"))

        draw_clouds("back")

        if self.kind in ("rain", "storm"):
            P = self.P
            for (x, y0, m, L, a, w) in self.drops:
                y = (y0 + k * P * m / N_FRAMES) % P - 40
                f = _clamp((SCENE_H - y) / 150.0)
                if f <= 0.02 or y + L < 0:
                    continue
                d.line((x, y, x - L * 0.16, y + L), fill=(206, 220, 244, int(a * f)), width=w)

        if self.kind == "snow":
            T = self.tile
            for (x0, y0, m, r, ph, f, amp) in self.flakes:
                yy = (y0 + k * T * m / N_FRAMES) % T
                x = x0 + amp * math.sin(_TAU * t * f + ph)
                for off in (-T, 0, T):
                    y = yy + off
                    fd = _clamp((SCENE_H - y) / 150.0)
                    if fd <= 0.02 or y < -6:
                        continue
                    d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, int((150 + 70 * (r / 3.6)) * fd)))

        draw_clouds("front")

        if self.kind == "storm":
            ev = self.flash.get(k)
            if ev:
                inten, which = ev
                if which:
                    pts, br = self.bolts[which]
                    glow = Image.new("RGBA", (W, SCENE_H), (0, 0, 0, 0))
                    gd = ImageDraw.Draw(glow)
                    gd.line(pts, fill=(176, 188, 255, 200), width=14, joint="curve")
                    gd.line(br, fill=(176, 188, 255, 150), width=9, joint="curve")
                    glow = glow.filter(ImageFilter.GaussianBlur(6))
                    fr.paste(glow, (0, 0), glow.getchannel("A"))
                    d.line(pts, fill=(255, 255, 255, 255), width=4, joint="curve")
                    d.line(br, fill=(255, 255, 255, 235), width=2, joint="curve")
                m = self.flash_mask.point(lambda v: int(v * inten))
                fr.paste(Image.new("RGB", (W, 600), (226, 232, 255)), (0, 0), m)
        return fr


# ------------------------------------------------------------------
# رابط کاربری ثابت (متن‌ها، کارت‌های شیشه‌ای)
# ------------------------------------------------------------------
def _fmt_t(v, unit="°"):
    if v is None:
        return "—"
    try:
        return f"{int(round(float(v)))}{unit}"
    except (TypeError, ValueError):
        return "—"


def _fmt_n(v, unit=""):
    if v is None:
        return "—"
    try:
        f = float(v)
        return (str(int(round(f))) if abs(f - round(f)) < 0.05 else f"{f:.1f}") + unit
    except (TypeError, ValueError):
        return "—"


def _aqi_color(aqi):
    if aqi is None:
        return (190, 190, 190)
    if aqi <= 50:
        return (80, 220, 120)
    if aqi <= 100:
        return (250, 220, 70)
    if aqi <= 150:
        return (255, 160, 50)
    if aqi <= 200:
        return (255, 90, 80)
    if aqi <= 300:
        return (190, 100, 230)
    return (160, 70, 70)


def _default_day_label(i: int, iso: str):
    rel = {0: "امروز", 1: "فردا", 2: "پس‌فردا"}.get(i)
    if rel:
        return rel
    try:
        return _FA_WEEKDAYS[_date.fromisoformat(iso).weekday()]
    except Exception:
        return iso or ""


def _hour_label(h: str, first: bool) -> str:
    if first:
        return "الان"
    s = str(h or "")[:2]
    return str(int(s)) if s.isdigit() else s


def _temp_color(t, lo, hi):
    x = 0.5 if hi - lo < 0.01 else _clamp((t - lo) / (hi - lo))
    stops = [(0.0, (88, 178, 255)), (0.5, (255, 214, 64)), (1.0, (255, 128, 52))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if x <= b:
            return _mix(ca, cb, (x - a) / (b - a))
    return stops[-1][1]


def _build_ui(data: dict, kind: str, is_day: int, day_label, n_days: int):
    ui = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))              # لایهٔ سایه (محو می‌شود)
    d = ImageDraw.Draw(ui)
    ds = ImageDraw.Draw(sh)
    WHITE = (255, 255, 255, 255)
    SOFT = (255, 255, 255, 190)

    def shadow_text(x, y, s, font, anchor="m", a=95):
        _text(ds, x, y + 2, s, font, (0, 0, 0, a), anchor)

    cx = W // 2
    city = str(data.get("city") or "—")
    # --- هدر
    f_city, f_temp, f_cond, f_hl = _font(32, True), _font(136, True), _font(30, True), _font(22)
    shadow_text(cx, 46, city, f_city)
    _text(d, cx, 46, city, f_city, WHITE)
    temp_s = _fmt_t(data.get("temp"), "°")
    shadow_text(cx + 10, 134, temp_s, f_temp, a=80)
    _text(d, cx + 10, 134, temp_s, f_temp, WHITE)                 # +10: وزن بصری علامت ° را جبران می‌کند
    cond = str(data.get("condition") or "")
    shadow_text(cx, 216, cond, f_cond)
    _text(d, cx, 216, cond, f_cond, WHITE)
    hl = f"بیشینه {_fmt_t(data.get('tmax'))}   ·   کمینه {_fmt_t(data.get('tmin'))}"
    shadow_text(cx, 253, hl, f_hl)
    _text(d, cx, 253, hl, f_hl, WHITE)

    panel_fill = (6, 22, 52, 66) if is_day else (255, 255, 255, 24)
    border = (255, 255, 255, 52)

    def panel(x0, y0, x1, y1):
        d.rounded_rectangle((x0, y0, x1, y1), radius=24, fill=panel_fill, outline=border, width=1)

    f_title, f_lbl, f_val = _font(16, True), _font(18), _font(21, True)

    # --- پنل ساعت‌به‌ساعت
    y0, y1 = 290, 436
    panel(18, y0, W - 18, y1)
    _text(d, W - 40, y0 + 22, "پیش‌بینی ساعتی", f_title, SOFT, "r")
    d.line((36, y0 + 44, W - 36, y0 + 44), fill=(255, 255, 255, 46), width=1)
    strip = (data.get("hourly_strip") or [])[::2][:6]
    colw = (W - 72) / 6.0
    for i, h in enumerate(strip):
        x = (W - 36) - colw * (i + 0.5)                           # راست‌به‌چپ: «الان» سمت راست
        ic_kind = _icon_from_code(h["code"]) if h.get("code") is not None else _icon_from_emoji(h.get("icon") or "")
        _text(d, x, y0 + 64, _hour_label(h.get("hour"), i == 0), f_lbl, SOFT)
        ic = _icon(ic_kind, 1 if h.get("is_day", 1) else 0, 40)
        ui.alpha_composite(ic, (int(x - 20), y0 + 78))
        _text(d, x, y0 + 130, _fmt_t(h.get("temp")), f_val, WHITE)

    # --- پنل روزهای آینده
    fc = (data.get("forecast") or [])[:n_days]
    row_h = 58
    y0 = 450
    y1 = y0 + 50 + row_h * max(1, len(fc)) + 4
    panel(18, y0, W - 18, y1)
    _text(d, W - 40, y0 + 22, "پیش‌بینی روزهای آینده", f_title, SOFT, "r")
    d.line((36, y0 + 44, W - 36, y0 + 44), fill=(255, 255, 255, 46), width=1)
    if fc:
        lo_all = min(float(x["tmin"]) for x in fc if x.get("tmin") is not None) if any(x.get("tmin") is not None for x in fc) else 0.0
        hi_all = max(float(x["tmax"]) for x in fc if x.get("tmax") is not None) if any(x.get("tmax") is not None for x in fc) else 1.0
        bx0, bx1 = 112, 246                                      # محدودهٔ نوار دما (کم‌ترین سمت راست)
        bw = bx1 - bx0
        grad = Image.new("RGBA", (bw, 8), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for px in range(bw):                                      # راست = سرد، چپ = گرم
            gd.line((px, 0, px, 8), fill=_temp_color(lo_all + (1 - px / max(1, bw - 1)) * (hi_all - lo_all), lo_all, hi_all) + (255,))
        rng = max(0.5, hi_all - lo_all)
        for i, dd in enumerate(fc):
            cy = y0 + 50 + row_h * i + row_h // 2
            if i:
                d.line((36, cy - row_h // 2, W - 36, cy - row_h // 2), fill=(255, 255, 255, 30), width=1)
            lab = day_label(i, dd.get("date", ""))
            _text(d, W - 40, cy, str(lab), _font(21, True), WHITE, "r")
            ic_kind = _icon_from_code(dd["code"]) if dd.get("code") is not None else _icon_from_emoji(dd.get("icon") or "")
            ic = _icon(ic_kind, 1, 36)
            icx = 330
            ui.alpha_composite(ic, (icx - 18, cy - 25))
            pop = dd.get("pop")
            if pop is not None and pop > 15:
                _text(d, icx, cy + 20, f"{int(pop)}%", _font(13, True), (130, 200, 255, 255))
            tmin, tmax = dd.get("tmin"), dd.get("tmax")
            _text(d, 298, cy, _fmt_t(tmin), _font(21), SOFT, "r")
            _text(d, 100, cy, _fmt_t(tmax), _font(21, True), WHITE, "r")
            d.rounded_rectangle((bx0, cy - 4, bx1, cy + 4), radius=4, fill=(255, 255, 255, 50))
            if tmin is not None and tmax is not None:
                a = bx1 - (float(tmin) - lo_all) / rng * bw          # سمت سرد (راست)
                b = bx1 - (float(tmax) - lo_all) / rng * bw
                x_r, x_l = int(round(a)), int(round(min(b, a - 8)))
                x_l, x_r = max(bx0, x_l), min(bx1, x_r)
                seg = grad.crop((x_l - bx0, 0, x_r - bx0, 8))
                mk = Image.new("L", seg.size, 0)
                ImageDraw.Draw(mk).rounded_rectangle((0, 0, seg.width - 1, 7), radius=4, fill=255)
                ui.paste(seg, (x_l, cy - 4), mk)
                if i == 0 and data.get("temp") is not None:
                    dx = int(bx1 - (_clamp(float(data["temp"]), lo_all, hi_all) - lo_all) / rng * bw)
                    dx = max(x_l + 3, min(x_r - 3, dx))
                    d.ellipse((dx - 5, cy - 5, dx + 5, cy + 5), fill=(255, 255, 255, 255), outline=(0, 0, 0, 70))

    # --- پنل جزئیات
    py0 = y1 + 14
    py1 = py0 + 72
    panel(18, py0, W - 18, py1)
    aqi = data.get("aqi")
    cells = [("دمای حسی", _fmt_t(data.get("feel")), None),
             ("باد", _fmt_n(data.get("wind_speed")), "km/h"),
             ("رطوبت", _fmt_n(data.get("humidity"), "%"), None),
             ("فرابنفش", _fmt_n(data.get("uv")), None),
             ("کیفیت هوا", _fmt_n(aqi), "dot")]
    cw = (W - 36) / 5.0
    for i, (lab, val, extra) in enumerate(cells):
        x = (W - 18) - cw * (i + 0.5)
        _text(d, x, py0 + 24, lab, _font(14), SOFT)
        vw = _tlen(d, val, _font(22, True))
        _text(d, x, py0 + 50, val, _font(22, True), WHITE)
        if extra == "km/h":
            _text(d, x, py0 + 67, "km/h", _font(11), SOFT)
        elif extra == "dot":
            r = 4
            col = _aqi_color(aqi) + (255,)
            d.ellipse((x + vw / 2 + 6, py0 + 50 - r, x + vw / 2 + 6 + 2 * r, py0 + 50 + r), fill=col)

    sh = sh.filter(ImageFilter.GaussianBlur(3))
    return Image.alpha_composite(sh, ui)


# ------------------------------------------------------------------
# خروجی گیف
# ------------------------------------------------------------------
def _encode_gif(frames, duration_ms: int) -> bytes:
    idx = list(range(0, len(frames), 4)) + [6 % len(frames)]
    tiles = [frames[i].resize((W // 2, H // 2), Image.BILINEAR) for i in dict.fromkeys(idx)]
    mont = Image.new("RGB", (W // 2, (H // 2) * len(tiles)))
    for i, t in enumerate(tiles):
        mont.paste(t, (0, i * (H // 2)))
    pal = mont.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    buf = io.BytesIO()
    q[0].save(buf, format="GIF", save_all=True, append_images=q[1:], duration=duration_ms, loop=0,
              optimize=False, disposal=1)
    return buf.getvalue()


def render_weather_gif(data: dict, day_label=None, n_days: int = 5, scale: float | None = None) -> bytes:
    """data = خروجی fetch_weather(). برمی‌گرداند: بایت‌های GIF.
    day_label(i, iso_date) → عنوان روز؛ پیش‌فرض: امروز/فردا/پس‌فردا/نام روز هفته."""
    if scale is None:
        try:
            scale = float(os.environ.get("WEATHER_GIF_SCALE", "1") or 1)
        except ValueError:
            scale = 1.0
    kind, is_day, heavy = scene_kind(data)
    seed = abs(hash(str(data.get("city")))) % 1000
    scene = _Scene(kind, is_day, heavy, seed=7 + seed % 5)
    ui = _build_ui(data, kind, is_day, day_label or _default_day_label, n_days)
    out = []
    for k in range(N_FRAMES):
        fr = scene.frame(k)
        fr.paste(ui, (0, 0), ui)
        out.append(fr)
    if 0.3 <= scale < 0.999:
        sz = (int(W * scale) // 2 * 2, int(H * scale) // 2 * 2)
        out = [f.resize(sz, Image.LANCZOS) for f in out]
    return _encode_gif(out, FRAME_MS)
