# -*- coding: utf-8 -*-
"""
ماژول مستقل «قیمت خودرو» — فایل جدا کنار bot.py (مثل pinterest_service.py).

منبع: iranjib.ir  (صفحهٔ خودروهای تولید داخل: showgroup/45)
داده: HTML صفحه پارس می‌شود (JSON جدا ندارد). آخرین اسنپ‌شات سالم در «خودرو.txt» ذخیره
می‌شود تا بعد از ری‌استارت یا وقتی سایت در دسترس نیست، ربات خالی نماند.

.env (همه اختیاری):
  CAR_DATA_PATH       مسیر فایل اسنپ‌شات (پیش‌فرض: خودرو.txt کنار همین فایل)
  CAR_REFRESH_SEC     فاصلهٔ آپدیت پس‌زمینه (پیش‌فرض 600، حداقل 120)
  CAR_EXTRA_URLS      آدرس‌های اضافه (با کاما) با همان ساختار صفحه، مثلاً گروه وارداتی
  CAR_USER_AGENT      User-Agent درخواست

API ماژول (همه thread-safe):
  refresh(force=False) -> bool        دریافت از سایت و ذخیره
  ensure_loaded()                     بارگذاری از فایل اگر حافظه خالی است
  brands() -> list[str]
  format_menu() -> str
  format_brand(index) -> str
  format_search(query) -> str | None  None = چیزی پیدا نشد
  برای نمای دکمه‌ای (مثل بخش ارز):
  brand_page(index, page) -> (brand, items, page, pages) | None
  header_brand(brand, page, pages) -> str
  header_search(query) -> str
  price_label(v, note) -> str
  is_stale(max_age_sec) -> bool
"""
import html as _html
import json
import logging
import os
import re
import threading
import time
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import requests

logger = logging.getLogger("car_prices")

CAR_SOURCE_URL = "https://www.iranjib.ir/showgroup/45/" + quote("قیمت-خودرو-تولید-داخل") + "/"

CAR_DATA_PATH = os.environ.get("CAR_DATA_PATH") or str(Path(__file__).resolve().parent / "خودرو.txt")
try:
    CAR_REFRESH_SEC = max(120, int(os.environ.get("CAR_REFRESH_SEC", "600")))
except ValueError:
    CAR_REFRESH_SEC = 600
_UA = os.environ.get("CAR_USER_AGENT") or (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Mobile Safari/537.36"
)
_EXTRA_URLS = [u.strip() for u in (os.environ.get("CAR_EXTRA_URLS") or "").split(",") if u.strip()]

_lock = threading.Lock()
_state = {"items": [], "updated_text": "", "notice": "", "fetched_at": 0.0}

# ------------------------------------------------------------------ نرمال‌سازی

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(s: str) -> str:
    return (s or "").translate(_DIGITS)


def _norm(s: str) -> str:
    """نرمال‌سازی برای جستجو: ی/ک عربی، نیم‌فاصله، ارقام فارسی، همزه، حروف تکراری ی."""
    s = _digits(s or "").lower()
    s = (s.replace("ي", "ی").replace("ك", "ک").replace("ئ", "ی").replace("ؤ", "و")
          .replace("أ", "ا").replace("إ", "ا").replace("ۀ", "ه").replace("ة", "ه"))
    s = re.sub(r"[\u200c\u200d\u200e\u200f\u064b-\u065f\u0640]", " ", s)
    s = re.sub(r"[^\w\s+]", " ", s)
    s = re.sub(r"ی{2,}", "ی", s)
    return re.sub(r"\s+", " ", s).strip()


_ALIASES = {  # اسم رایج → چیزی که در لیست سایت هست
    "پراید": "151",
    "ریرا": "ری را",
    "کوئیک": "کوییک",
}

# ------------------------------------------------------------------ پارسر HTML


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.brand = ""
        self.items = []
        self._in_h2 = False
        self._h2 = []
        self._tr = None       # dict وضعیت ردیف جاری
        self._td = None       # لیست تکه‌های متن سلول جاری
        self._td_a = None     # متن لینک اول سلول
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "")
        if tag == "h2":
            self._in_h2, self._h2 = True, []
        elif tag == "tr":
            self._tr = {"cls": cls, "cells": [], "iid": None, "a": ""}
        elif tag == "td" and self._tr is not None:
            self._td, self._td_a, self._in_a = [], [], False
        elif tag == "a" and self._td is not None:
            self._in_a = True
            m = re.search(r"getchart\.php\?iid=(\d+)", a.get("href") or "")
            if m and self._tr is not None:
                self._tr["iid"] = int(m.group(1))

    def handle_endtag(self, tag):
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            name = re.sub(r"\s+", " ", "".join(self._h2)).strip()
            if name:
                self.brand = name
        elif tag == "a":
            self._in_a = False
        elif tag == "td" and self._td is not None and self._tr is not None:
            text = re.sub(r"\s+", " ", "".join(self._td)).strip()
            if not self._tr["cells"]:
                self._tr["a"] = re.sub(r"\s+", " ", "".join(self._td_a or [])).strip()
            self._tr["cells"].append(text)
            self._td = None
        elif tag == "tr" and self._tr is not None:
            tr, self._tr = self._tr, None
            self._finish_row(tr)

    def handle_data(self, data):
        if self._in_h2:
            self._h2.append(data)
        if self._td is not None:
            self._td.append(data)
            if self._in_a and self._td_a is not None:
                self._td_a.append(data)

    def _finish_row(self, tr):
        cls = tr["cls"]
        if "expand" in cls or "catsection" in cls or "header" in cls:
            return
        cells = tr["cells"]
        if len(cells) < 3 or not self.brand:
            return
        name = tr["a"] or cells[0]
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return
        market, market_note = _parse_price(cells[1])
        factory, factory_note = _parse_price(cells[2])
        pct = None
        if len(cells) > 3:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", _digits(cells[3]))
            if m:
                pct = float(m.group(1))
        self.items.append({
            "brand": self.brand, "name": name,
            "market": market, "market_note": market_note,
            "factory": factory, "factory_note": factory_note,
            "change_pct": pct, "iid": tr["iid"],
        })


def _parse_price(text: str):
    """→ (int تومان | None, توضیح متنی | '')"""
    t = _digits(text or "").replace(",", "").replace("٬", "").strip()
    if re.fullmatch(r"\d{4,}", t):
        return int(t), ""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if t in ("", "-", "--", "---", "—"):
        return None, "—"
    return None, t


def parse_page(html_text: str) -> dict:
    p = _Parser()
    p.feed(html_text)
    p.close()
    m = re.search(r'group-refreshdate-wrapper">([^<]+)<', html_text)
    updated = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    n = re.search(r'<div class="pricelinks">\s*(?:<div class="pricelinks">)?\s*([^<]{10,}?)\s*<', html_text)
    notice = re.sub(r"\s+", " ", n.group(1)).strip() if n else ""
    return {"items": p.items, "updated_text": updated, "notice": notice}


# ------------------------------------------------------------------ دریافت و ذخیره


def _fetch(url: str, timeout: float = 15.0) -> dict:
    r = requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "fa,en;q=0.8"}, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    if "charset" not in (r.headers.get("content-type") or "").lower():
        r.encoding = "utf-8"
    return parse_page(r.text)


def _save_file(snapshot: dict) -> None:
    try:
        path = Path(CAR_DATA_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.error(f"[CAR] write file failed: {e}")


def _load_file() -> dict | None:
    try:
        data = json.loads(Path(CAR_DATA_PATH).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            return data
    except Exception:
        pass
    return None


def ensure_loaded() -> None:
    with _lock:
        if _state["items"]:
            return
        data = _load_file()
        if data:
            _state.update(items=data["items"], updated_text=data.get("updated_text", ""),
                          notice=data.get("notice", ""), fetched_at=float(data.get("fetched_at", 0)))


def refresh(force: bool = False) -> bool:
    """دریافت از سایت. اگر پارس خراب بود (کمتر از ۵ آیتم) دادهٔ قبلی دست‌نخورده می‌ماند."""
    with _lock:
        if not force and _state["items"] and time.time() - _state["fetched_at"] < 60:
            return True
    items, updated, notice = [], "", ""
    try:
        for i, url in enumerate([CAR_SOURCE_URL] + _EXTRA_URLS):
            try:
                page = _fetch(url)
            except Exception as e:
                if i == 0:
                    raise
                logger.warning(f"[CAR] extra url failed {url}: {e}")
                continue
            items.extend(page["items"])
            if i == 0:
                updated, notice = page["updated_text"], page["notice"]
        if len(items) < 5:
            raise ValueError(f"فقط {len(items)} آیتم پارس شد؛ ساختار صفحه عوض شده؟")
    except Exception as e:
        logger.warning(f"[CAR] refresh failed, keeping last data: {e}")
        return False
    snap = {"items": items, "updated_text": updated, "notice": notice, "fetched_at": time.time()}
    with _lock:
        _state.update(snap)
    _save_file(snap)
    logger.info(f"[CAR] OK items={len(items)} brands={len(_brands_of(items))} source_update='{updated}'")
    return True


def is_stale(max_age_sec: float = 900) -> bool:
    return (time.time() - _state["fetched_at"]) > max_age_sec


# ------------------------------------------------------------------ قالب پیام


def _brands_of(items) -> list:
    seen = []
    for it in items:
        if it["brand"] not in seen:
            seen.append(it["brand"])
    return seen


def brands() -> list:
    with _lock:
        return _brands_of(_state["items"])


def _m(v) -> str:
    """تومان → میلیون تومان با جداکنندهٔ هزارگان"""
    return f"{round(v / 1_000_000):,}"


def _price_cell(v, note) -> str:
    return _m(v) if v else (note or "—")


def price_label(v, note) -> str:
    """متن قیمت برای دکمه (میلیون تومان)"""
    return _price_cell(v, note)


def _row(it) -> str:
    name = _html.escape(it.get("name") or "—")
    return (f"▫️ <b>{name}</b>\n"
            f"    🏷 {_price_cell(it.get('market'), it.get('market_note'))}   ·   "
            f"🏭 {_price_cell(it.get('factory'), it.get('factory_note'))}")


def _footer(state) -> str:
    out = ["", "🏷 بازار · 🏭 کارخانه — به <b>میلیون تومان</b>"]
    if state["updated_text"]:
        out.append(f"🕐 آخرین آپدیت منبع: {_html.escape(state['updated_text'])}")
    if state["notice"]:
        out.append(f"⚠️ <i>{_html.escape(state['notice'])}</i>")
    out.append("منبع: ایران‌جیب")
    return "\n".join(out)


def _snapshot() -> dict:
    with _lock:
        return dict(_state)


def format_menu() -> str:
    s = _snapshot()
    if not s["items"]:
        return "🚗 فعلاً قیمت خودرو در دسترس نیست؛ چند دقیقه بعد دوباره امتحان کن."
    lines = ["🚗 <b>قیمت خودرو (تولید داخل)</b>", "یکی از شرکت‌ها رو بزن، یا بنویس «خودرو سورن»."]
    return "\n".join(lines) + "\n" + _footer(s)


_TG_LIMIT = 3900


def _cut(text: str) -> str:
    return text if len(text) <= _TG_LIMIT else text[:_TG_LIMIT].rsplit("\n", 1)[0] + "\n…"


def format_brand(index: int) -> str:
    s = _snapshot()
    bl = _brands_of(s["items"])
    if not (0 <= index < len(bl)):
        return "این شرکت پیدا نشد."
    b = bl[index]
    rows = [_row(it) for it in s["items"] if it["brand"] == b]
    return _cut(f"🚗 <b>{_html.escape(b)}</b>\n\n" + "\n".join(rows) + "\n" + _footer(s))


def _score(q_tokens, q_norm, it) -> float:
    hay = _norm(it["name"] + " " + it["brand"])
    htoks = hay.split()
    if all(t in htoks or any(h.startswith(t) for h in htoks) for t in q_tokens):
        return 2.0 + len(q_norm) / max(len(hay), 1)
    if all(t in hay for t in q_tokens):
        return 1.5
    r = SequenceMatcher(None, q_norm, _norm(it["name"])).ratio()
    return r if r >= 0.72 else 0.0


def search(query: str, limit: int = 8) -> list:
    q = _norm(query)
    q = _ALIASES.get(q, q)
    toks = q.split()
    if not toks:
        return []
    items = _snapshot()["items"]
    scored = [(s, it) for it in items if (s := _score(toks, q, it)) > 0]
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def find_brand(query: str):
    """اگر کوئری اسم یک شرکت بود، ایندکسش را برمی‌گرداند."""
    q = _norm(query)
    if not q or q in ("خودرو", "ماشین", "شرکت", "قیمت"):
        return None
    for i, b in enumerate(brands()):
        nb = _norm(b)
        if q == nb or (len(q) >= 3 and (q in nb or nb in q)):
            return i
    return None


def format_search(query: str) -> str | None:
    bi = find_brand(query)
    if bi is not None:
        return format_brand(bi)
    hits = search(query)
    if not hits:
        return None
    s = _snapshot()
    parts, cur = [f"🚗 <b>نتیجهٔ «{_html.escape(query.strip())}»</b>\n"], None
    for it in hits:
        if it["brand"] != cur:
            cur = it["brand"]
            parts.append(f"\n<b>{_html.escape(cur)}</b>")
        parts.append(_row(it))
    return _cut("\n".join(parts) + "\n" + _footer(s))


# ------------------------------------------------------------------ نمای دکمه‌ای (مثل بخش ارز)

CAR_PAGE_SIZE = 10  # هر خودرو ۳ دکمه؛ تلگرام حداکثر ۱۰۰ دکمه در هر پیام


def brand_page(index: int, page: int = 0, per: int = CAR_PAGE_SIZE):
    """→ (brand, items_of_page, page, pages) یا None اگر شرکت پیدا نشد."""
    s = _snapshot()
    bl = _brands_of(s["items"])
    if not (0 <= index < len(bl)):
        return None
    b = bl[index]
    items = [it for it in s["items"] if it["brand"] == b]
    pages = max(1, -(-len(items) // per))
    page = min(max(page, 0), pages - 1)
    return b, items[page * per:(page + 1) * per], page, pages


def header_brand(brand: str, page: int = 0, pages: int = 1) -> str:
    t = f"🚗 <b>{_html.escape(brand)}</b>"
    if pages > 1:
        t += f"  ({page + 1}/{pages})"
    return t + "\n" + _footer(_snapshot())


def header_search(query: str) -> str:
    return f"🚗 <b>نتیجهٔ «{_html.escape(query.strip())}»</b>\n" + _footer(_snapshot())
