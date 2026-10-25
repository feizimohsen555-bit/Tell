#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newspaper_service.py — «روزنامه شبانه نخود»

هر شب سر ساعت مشخص، اتفاقات ۲۴ ساعت گذشتهٔ گروه را جمع می‌کند و به شکل پوستر یک روزنامهٔ
قدیمی دههٔ شصتی (کاغذ زردشده، قاب دوخطی، سرمقاله، ستون‌بندی) برای گروه می‌فرستد.

این فایل مستقل است (کنار bot.py بگذار). bot.py فقط این‌ها را به آن می‌دهد:
    init(db_cursor, llm_fn, send_photo_fn, send_text_fn, get_groups_fn, is_admin_fn, to_jalali_fn, bot_name)
    record(m)           ← از شنوندهٔ سراسری پیام‌ها
    job()               ← از scheduler هر ۱ دقیقه
    handle_command(m)   ← دستورهای «روزنامه ...» در گروه

وابستگی‌ها: Pillow (الزامی). برای شکل‌دهی درست فارسی یکی از این دو:
    ۱) libraqm در Pillow (ImageFont.Layout.RAQM) — بهترین حالت
    ۲) pip install arabic-reshaper python-bidi
فونت: پوشهٔ fonts/ کنار bot.py (Vazirmatn-Bold.ttf برای متن، Lalezar-Regular.ttf یا هر فونت تیتر برای
نام روزنامه). اگر نبود، DejaVu/Noto سیستم استفاده می‌شود.
"""

import io
import os
import re
import json
import time
import random
import logging
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("newspaper")

# ───────────────────────── تنظیمات (ENV) ─────────────────────────
def _env_int(name, default, lo, hi):
    try:
        v = int(str(os.environ.get(name, default)).strip())
    except Exception:
        v = default
    return max(lo, min(hi, v))


NEWS_ENABLED = os.environ.get("NEWS_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
NEWS_DEFAULT_ON = os.environ.get("NEWS_DEFAULT_ON", "0").strip().lower() in ("1", "true", "on", "yes")  # گروه‌های جدید پیش‌فرض روشن؟
NEWS_DEFAULT_TIME = (os.environ.get("NEWS_TIME") or "23:00").strip()         # ساعت پیش‌فرض (به وقت تهران)
NEWS_MIN_MSGS = _env_int("NEWS_MIN_MSGS", 15, 1, 5000)                       # کمتر از این → روزنامه منتشر نمی‌شود
NEWS_WINDOW_HOURS = _env_int("NEWS_WINDOW_HOURS", 24, 1, 72)
NEWS_CATCHUP_MIN = _env_int("NEWS_CATCHUP_MIN", 90, 5, 600)                  # اگر ربات سرِ ساعت خاموش بود تا این‌قدر بعدش هم منتشر کن
NEWS_KEEP_HOURS = NEWS_WINDOW_HOURS + 6
NEWS_MAX_PER_CHAT = _env_int("NEWS_MAX_PER_CHAT", 6000, 200, 50000)
NEWS_LLM_MAX_LINES = _env_int("NEWS_LLM_MAX_LINES", 320, 40, 1500)
NEWS_MANUAL_COOLDOWN = _env_int("NEWS_MANUAL_COOLDOWN_MIN", 20, 1, 1440) * 60
NEWS_TITLE = os.environ.get("NEWS_TITLE", "روزنامه شبانه نخود").strip() or "روزنامه شبانه نخود"

TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
              "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
_FA_WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # Monday=0

# ───────────────────────── اتصال به bot.py ─────────────────────────
_db = None
_llm = None
_send_photo = None
_send_text = None
_get_groups = None
_is_admin = None
_owner_id = None            # مالک ربات؛ فقط او می‌تواند نسخهٔ دستی («روزنامه الان») چاپ کند


def set_owner_id(uid) -> None:
    global _owner_id
    _owner_id = int(uid) if uid else None
_to_jalali = None
_bot_name = "نخود"
_ready = False
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="newspaper")
_inflight = set()
_inflight_lock = threading.Lock()
_manual_last = {}


def init(db_cursor, llm_fn, send_photo_fn, send_text_fn, get_groups_fn, is_admin_fn,
         to_jalali_fn=None, bot_name="نخود"):
    """llm_fn(system, user, max_tokens, timeout) -> str|None ؛ send_photo_fn(chat_id, jpeg_bytes, caption) -> bool"""
    global _db, _llm, _send_photo, _send_text, _get_groups, _is_admin, _to_jalali, _bot_name, _ready
    _db, _llm, _send_photo, _send_text = db_cursor, llm_fn, send_photo_fn, send_text_fn
    _get_groups, _is_admin, _to_jalali, _bot_name = get_groups_fn, is_admin_fn, to_jalali_fn, bot_name
    try:
        with _db() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS news_log (
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    ts DOUBLE PRECISION NOT NULL,
                    user_id BIGINT NULL,
                    author VARCHAR(80) NOT NULL,
                    kind VARCHAR(16) NOT NULL DEFAULT 'text',
                    body VARCHAR(400) NOT NULL DEFAULT '',
                    reply_to BIGINT NULL,
                    is_bot SMALLINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, message_id)
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_news_log_ts ON news_log (chat_id, ts)")
            c.execute("""
                CREATE TABLE IF NOT EXISTS news_cfg (
                    chat_id BIGINT PRIMARY KEY,
                    enabled SMALLINT NOT NULL DEFAULT 0,
                    at_time VARCHAR(5) NOT NULL DEFAULT '23:00',
                    last_date VARCHAR(10) NULL,
                    issue_no INTEGER NOT NULL DEFAULT 0,
                    fails SMALLINT NOT NULL DEFAULT 0,
                    first_date VARCHAR(10) NULL
                )""")
        _ready = True
        logger.info("[NEWS] آماده شد (جدول‌ها بررسی شد)")
    except Exception as e:
        logger.warning(f"[NEWS] init ناموفق: {e}")
        _ready = False


# ───────────────────────── ثبت پیام‌ها ─────────────────────────
_KIND_BY_CT = {"photo": "photo", "video": "video", "voice": "voice", "video_note": "voice", "audio": "audio",
               "sticker": "sticker", "animation": "gif", "document": "file", "poll": "poll"}
_buf = deque(maxlen=20000)
_buf_lock = threading.Lock()
_last_flush = 0.0
_ctrl = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _clean_name(s: str, n: int = 24) -> str:
    """فقط حروف/عدد/فاصله — ایموجی و نمادها (که در فونت روزنامه نیستند) حذف می‌شوند."""
    s = _ctrl.sub("", s or "")
    s = "".join(ch for ch in s if ch.isalnum() or ch in " ‌-_.")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] or "ناشناس"


def record(m) -> None:
    if not (_ready and NEWS_ENABLED):
        return
    try:
        chat = getattr(m, "chat", None)
        if chat is None or getattr(chat, "type", "") not in ("group", "supergroup"):
            return
        ct = getattr(m, "content_type", "") or ""
        text = (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()
        if ct == "poll" and getattr(m, "poll", None):
            text = (getattr(m.poll, "question", "") or "").strip()
        if text.startswith("/"):
            return
        kind = "text" if (ct == "text" or (text and ct not in _KIND_BY_CT)) else _KIND_BY_CT.get(ct)
        if kind is None:                       # پیام سرویسی (عضو جدید، پین، ...)
            return
        fu = getattr(m, "from_user", None)
        sc = getattr(m, "sender_chat", None)
        is_bot = 0
        uid = None
        if sc is not None:
            name = "ادمین گروه" if sc.id == chat.id else (getattr(sc, "title", None) or "کانال")
        elif fu is not None:
            if getattr(fu, "is_bot", False):
                bid = _bot_id_getter() if _bot_id_getter else None
                if not (bid and fu.id == bid):
                    return                      # رباتهای دیگر نه
                is_bot = 1
            uid = fu.id
            name = getattr(fu, "first_name", None) or getattr(fu, "username", None) or "کاربر"
        else:
            return
        r = getattr(m, "reply_to_message", None)
        rid = getattr(r, "message_id", None) if r is not None else None
        row = (chat.id, m.message_id, time.time(), uid, _clean_name(name, 40),
               kind, text[:400], rid, is_bot)
        with _buf_lock:
            _buf.append(row)
        if len(_buf) >= 200 or time.time() - _last_flush > 45:
            _pool.submit(flush)
    except Exception as e:
        logger.debug(f"[NEWS] record: {e}")


_bot_id_getter = None


def set_bot_id_getter(fn):
    global _bot_id_getter
    _bot_id_getter = fn


def flush() -> int:
    global _last_flush
    if not _ready:
        return 0
    with _buf_lock:
        rows = list(_buf)
        _buf.clear()
    _last_flush = time.time()
    if not rows:
        return 0
    try:
        with _db() as c:
            c.executemany(
                "INSERT INTO news_log (chat_id,message_id,ts,user_id,author,kind,body,reply_to,is_bot) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (chat_id,message_id) DO NOTHING", rows)
        return len(rows)
    except Exception as e:
        logger.warning(f"[NEWS] flush ناموفق ({len(rows)} پیام): {e}")
        with _buf_lock:
            for r in reversed(rows):
                _buf.appendleft(r)
        return 0


def cleanup() -> None:
    """پیام‌های قدیمی‌تر از NEWS_KEEP_HOURS را پاک می‌کند (هر ساعت از scheduler)."""
    if not _ready:
        return
    try:
        flush()
        with _db() as c:
            c.execute("DELETE FROM news_log WHERE ts < %s", (time.time() - NEWS_KEEP_HOURS * 3600,))
    except Exception as e:
        logger.warning(f"[NEWS] cleanup: {e}")


# ───────────────────────── تنظیمات هر گروه ─────────────────────────
def _norm_time(s: str):
    s = (s or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    m = re.search(r"(\d{1,2})\s*[:٫.]\s*(\d{2})", s) or re.search(r"\b(\d{1,2})\b", s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def _cfg(chat_id: int) -> dict:
    default_t = _norm_time(NEWS_DEFAULT_TIME) or "23:00"
    with _db() as c:
        c.execute("SELECT enabled, at_time, last_date, issue_no, fails, first_date FROM news_cfg WHERE chat_id=%s",
                  (chat_id,))
        r = c.fetchone()
    if not r:
        return {"enabled": 1 if NEWS_DEFAULT_ON else 0, "at_time": default_t, "last_date": None,
                "issue_no": 0, "fails": 0, "first_date": None, "exists": False}
    return {"enabled": int(r["enabled"]), "at_time": r["at_time"], "last_date": r["last_date"],
            "issue_no": int(r["issue_no"]), "fails": int(r["fails"]), "first_date": r["first_date"], "exists": True}


def _set_cfg(chat_id: int, **kw) -> None:
    cur = _cfg(chat_id)
    cur.update(kw)
    with _db() as c:
        c.execute(
            "INSERT INTO news_cfg (chat_id,enabled,at_time,last_date,issue_no,fails,first_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (chat_id) DO UPDATE SET "
            "enabled=EXCLUDED.enabled, at_time=EXCLUDED.at_time, last_date=EXCLUDED.last_date, "
            "issue_no=EXCLUDED.issue_no, fails=EXCLUDED.fails, first_date=EXCLUDED.first_date",
            (chat_id, cur["enabled"], cur["at_time"], cur["last_date"], cur["issue_no"], cur["fails"],
             cur["first_date"]))


# ───────────────────────── جمع‌آوری و آمار ─────────────────────────
def _fetch(chat_id: int, since: float):
    flush()
    with _db() as c:
        c.execute("SELECT message_id, ts, user_id, author, kind, body, reply_to, is_bot FROM news_log "
                  "WHERE chat_id=%s AND ts>=%s ORDER BY ts ASC LIMIT %s", (chat_id, since, NEWS_MAX_PER_CHAT))
        return [dict(r) for r in (c.fetchall() or ())]


def _fa(n) -> str:
    return str(n).translate(_FA_DIGITS)


def compute_stats(rows: list) -> dict:
    humans = [r for r in rows if not r["is_bot"]]
    by_author = Counter(r["author"] for r in humans)
    kinds = Counter(r["kind"] for r in humans)
    hours = Counter(datetime.fromtimestamp(r["ts"], TEHRAN_TZ).hour for r in humans)
    replied = Counter(r["reply_to"] for r in humans if r["reply_to"])
    by_id = {r["message_id"]: r for r in rows}
    hot = None
    for mid, n in replied.most_common(5):
        r = by_id.get(mid)
        if r and r["body"] and n >= 2 and not r["is_bot"]:
            hot = {"author": r["author"], "text": r["body"], "replies": n}
            break
    night_owl = None
    for r in humans:                             # بیدارترین عضو: پیام بین ۲۳ تا ۵ صبح
        h = datetime.fromtimestamp(r["ts"], TEHRAN_TZ).hour
        if h >= 23 or h < 5:
            night_owl = r["author"]
    peak = hours.most_common(1)[0] if hours else None
    return {
        "total": len(humans),
        "members": len(by_author),
        "top": by_author.most_common(3),
        "kinds": dict(kinds),
        "peak_hour": peak[0] if peak else None,
        "hot": hot,
        "night_owl": night_owl,
    }


def _pick_lines(rows: list) -> list:
    """حداکثر NEWS_LLM_MAX_LINES پیام متنی برای LLM؛ پیام‌های مهم‌تر (ریپلای‌خورده/بلندتر) اولویت دارند."""
    text_rows = [r for r in rows if r["body"] and not r["is_bot"] and r["kind"] in ("text", "poll", "photo", "video")]
    if len(text_rows) <= NEWS_LLM_MAX_LINES:
        return text_rows
    replied = Counter(r["reply_to"] for r in text_rows if r["reply_to"])
    scored = sorted(text_rows, key=lambda r: (replied.get(r["message_id"], 0) * 50 + min(len(r["body"]), 160)),
                    reverse=True)[:NEWS_LLM_MAX_LINES]
    scored.sort(key=lambda r: r["ts"])
    return scored


_SYSTEM = (
    "تو سردبیر یک روزنامهٔ قدیمی و جدی ایرانی در دههٔ ۱۳۶۰ هستی که طنزِ خشک و بامزه دارد "
    "و گزارش «شبانهٔ یک گروه تلگرامی دوستانه» را به سبک خبر رسمی و سرمقاله‌های آن دوران می‌نویسد "
    "(لحن: «به گزارش خبرنگار ما»، «در پی»، «ذیلاً»، «مراتب»...). فقط و فقط بر اساس پیام‌های داده‌شده بنویس؛ "
    "هیچ ماجرا یا نقل‌قولی از خودت نساز. اسم‌ها را دقیقاً همان‌طور که در لاگ هست بنویس. "
    "توهین، تمسخر شخصی، سیاست، مذهب، موضوع جنسی و خشونت ممنوع؛ طنز مهربان و دوستانه باشد. "
    "اگر گفتگوی خصوصی/حساس دیدی (شماره، رمز، آدرس) آن را نیاور. "
    "خروجی فقط یک JSON معتبر باشد، بدون هیچ متن دیگر و بدون بک‌تیک، با این کلیدها:\n"
    '{"headline": "تیتر درشت صفحه اول، حداکثر ۹ کلمه",\n'
    ' "subhead": "زیرتیتر، حداکثر ۱۴ کلمه",\n'
    ' "lead": "گزارش اصلی، ۵۰ تا ۷۰ کلمه",\n'
    ' "articles": [{"title": "تیتر خبر، حداکثر ۷ کلمه", "body": "۲۵ تا ۴۵ کلمه"}, ... دقیقاً ۳ مورد],\n'
    ' "briefs": ["خبر کوتاه یک‌جمله‌ای تا ۱۸ کلمه", ... دقیقاً ۳ مورد],\n'
    ' "quote": {"text": "یک پیام عیناً از لاگ که ارزش نقل‌قول دارد (حداکثر ۱۸ کلمه)", "who": "نام گوینده"},\n'
    ' "ad": "یک آگهی طنز کوتاه به سبک آگهی‌های نیازمندی‌های قدیم، تا ۲۵ کلمه، مرتبط با اتفاقات گروه",\n'
    ' "forecast": "پیش‌بینی هوای فردای گروه به سبک هواشناسی، یک جمله تا ۲۰ کلمه"}'
)


def _build_user_prompt(rows: list, stats: dict, group_title: str) -> str:
    lines = []
    for r in _pick_lines(rows):
        hhmm = datetime.fromtimestamp(r["ts"], TEHRAN_TZ).strftime("%H:%M")
        tag = {"photo": "[عکس] ", "video": "[ویدیو] "}.get(r["kind"], "")
        lines.append(f"{hhmm} {r['author']}: {tag}{r['body'][:160]}")
    st = [f"تعداد پیام انسانی: {stats['total']}", f"اعضای فعال: {stats['members']}"]
    if stats["top"]:
        st.append("پرکارترین‌ها: " + "، ".join(f"{a} ({n})" for a, n in stats["top"]))
    if stats["hot"]:
        st.append(f"پرگفتگوترین پیام: «{stats['hot']['text'][:120]}» از {stats['hot']['author']} "
                  f"({stats['hot']['replies']} ریپلای)")
    k = stats["kinds"]
    extra = [f"{v} {n}" for n, v in (("ویس", k.get("voice", 0)), ("عکس", k.get("photo", 0)),
                                      ("استیکر", k.get("sticker", 0)), ("گیف", k.get("gif", 0))) if v]
    if extra:
        st.append("رسانه: " + "، ".join(extra))
    return (f"نام گروه: {group_title or 'گروه'}\n" + "\n".join(st) +
            "\n\nلاگ پیام‌های ۲۴ ساعت گذشته (ساعت، نام: متن):\n" + "\n".join(lines))


def _parse_json(s: str):
    if not s:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        return None


def _norm_fa(s: str) -> str:
    s = _ctrl.sub("", s or "").replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", s).strip()


def _validate(d: dict, rows: list) -> dict:
    def sx(v, n):
        return _norm_fa(str(v or ""))[:n]
    arts = []
    for a in (d.get("articles") or [])[:3]:
        if isinstance(a, dict) and a.get("title") and a.get("body"):
            arts.append({"title": sx(a["title"], 60), "body": sx(a["body"], 330)})
    briefs = [sx(b, 150) for b in (d.get("briefs") or [])[:3] if b]
    out = {
        "headline": sx(d.get("headline"), 80) or "شبی آرام در گروه",
        "subhead": sx(d.get("subhead"), 110),
        "lead": sx(d.get("lead"), 520),
        "articles": arts,
        "briefs": briefs,
        "ad": sx(d.get("ad"), 190),
        "forecast": sx(d.get("forecast"), 150),
        "quote": None,
    }
    q = d.get("quote")                           # نقل‌قول باید عیناً در لاگ باشد
    if isinstance(q, dict) and q.get("text"):
        qt = _norm_fa(str(q["text"])).strip(" «»\"'")
        hay = [_norm_fa(r["body"]) for r in rows if r["body"]]
        if len(qt) >= 6 and any(qt in h for h in hay):
            out["quote"] = {"text": qt[:140], "who": sx(q.get("who"), 30)}
    return out


def _fallback_edition(stats: dict) -> dict:
    top = stats["top"][0][0] if stats["top"] else "اعضای گروه"
    return {
        "headline": "گروه، شبی پرتکاپو را پشت سر گذاشت",
        "subhead": f"{_fa(stats['total'])} پیام در ۲۴ ساعت گذشته مبادله شد",
        "lead": (f"به گزارش خبرنگار ما، طی ۲۴ ساعت گذشته {_fa(stats['members'])} نفر از اعضا در گروه فعالیت داشتند "
                 f"و {top} بیشترین سهم را در گفتگوها برعهده داشت."),
        "articles": [], "briefs": [], "quote": None, "ad": "", "forecast": "",
    }


# ───────────────────────── رندر پوستر ─────────────────────────
W, H = 1240, 1754                                  # A-ratio
PAPER = (233, 219, 178)
INK = (34, 28, 22)
RED = (150, 32, 24)

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_BODY_CANDS = ["Vazirmatn-Bold.ttf", "Vazirmatn-Regular.ttf", "Vazir-Bold.ttf", "Vazir.ttf", "Estedad-Bold.ttf",
               "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
               "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_BODY_REG_CANDS = ["Vazirmatn-Regular.ttf", "Vazir.ttf", "Estedad-Regular.ttf",
                   "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
                   "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_TITLE_CANDS = ["Lalezar-Regular.ttf", "ArefRuqaa-Bold.ttf", "ReemKufi-Bold.ttf", "Vazirmatn-Black.ttf",
                "Vazirmatn-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_fcache = {}
_shape_mode = None            # 'raqm' | 'reshaper' | 'none'


def _find_font(cands):
    for c in cands:
        p = c if os.path.isabs(c) else os.path.join(_FONT_DIR, c)
        if os.path.exists(p):
            return p
    return None


def _font(kind: str, size: int):
    from PIL import ImageFont
    key = (kind, size)
    if key in _fcache:
        return _fcache[key]
    cands = {"body": _BODY_REG_CANDS, "bold": _BODY_CANDS, "title": _TITLE_CANDS}[kind]
    p = _find_font(cands)
    try:
        if p and _shape_detect() == "raqm":
            f = ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.RAQM)
        elif p:
            f = ImageFont.truetype(p, size)
        else:
            f = ImageFont.load_default()
    except Exception:
        f = ImageFont.load_default()
    _fcache[key] = f
    return f


def _shape_detect() -> str:
    global _shape_mode
    if _shape_mode:
        return _shape_mode
    try:
        from PIL import features
        if features.check("raqm"):
            _shape_mode = "raqm"
            return _shape_mode
    except Exception:
        pass
    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
        _shape_mode = "reshaper"
    except Exception:
        _shape_mode = "none"
        logger.warning("[NEWS] نه raqm هست نه arabic-reshaper/python-bidi؛ متن فارسی به‌هم‌ریخته رندر می‌شود. "
                       "نصب: pip install arabic-reshaper python-bidi")
    return _shape_mode


_reshaper = None


def _shape(text: str) -> str:
    """برای حالت reshaper: حروف را به‌هم می‌چسباند و جهت RTL را درست می‌کند."""
    global _reshaper
    if _shape_detect() != "reshaper":
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display
    if _reshaper is None:
        _reshaper = arabic_reshaper.ArabicReshaper({"support_ligatures": True, "delete_harakat": True})
    return get_display(_reshaper.reshape(text))


def _tw(draw, text: str, font) -> float:
    t = _shape(text)
    kw = {"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}
    try:
        return draw.textlength(t, font=font, **kw)
    except Exception:
        return draw.textlength(t, font=font)


def _draw_rtl(draw, xr: int, y: int, text: str, font, fill, anchor_right=True):
    """متن را با لبهٔ راست xr می‌کشد."""
    t = _shape(text)
    kw = {"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}
    try:
        w = draw.textlength(t, font=font, **kw)
    except Exception:
        w = draw.textlength(t, font=font)
    x = xr - w if anchor_right else xr
    try:
        draw.text((x, y), t, font=font, fill=fill, **kw)
    except Exception:
        draw.text((x, y), t, font=font, fill=fill)
    return w


def _wrap(draw, text: str, font, max_w: int) -> list:
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if _tw(draw, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _para(draw, xr: int, y: int, text: str, font, fill, col_w: int, lh: int, max_lines: int = 99) -> int:
    """پاراگراف راست‌چین؛ y بعدی را برمی‌گرداند."""
    lines = _wrap(draw, text, font, col_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ،.") + " …"
    for ln in lines:
        _draw_rtl(draw, xr, y, ln, font, fill)
        y += lh
    return y


def _rules(draw, x0, x1, y, thick=True):
    draw.line([(x0, y), (x1, y)], fill=INK, width=4 if thick else 2)
    draw.line([(x0, y + 7), (x1, y + 7)], fill=INK, width=1)


def _fa_date(dt: datetime) -> str:
    wd = _FA_WEEKDAYS[dt.weekday()]
    if _to_jalali:
        try:
            jy, jm, jd = _to_jalali(dt.date())
            return f"{wd} {_fa(jd)} {_FA_MONTHS[jm - 1]} {_fa(jy)}"
        except Exception:
            pass
    return f"{wd} {_fa(dt.strftime('%Y/%m/%d'))}"


def render_poster(ed: dict, stats: dict, issue_no: int, now: datetime, group_title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    rnd = random.Random(now.strftime("%Y%m%d") + str(issue_no))

    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    M = 56                                        # حاشیه
    XL, XR = M + 18, W - M - 18                   # ناحیهٔ متن

    # قاب دوخطی
    d.rectangle([M - 14, M - 14, W - M + 14, H - M + 14], outline=INK, width=6)
    d.rectangle([M - 2, M - 2, W - M + 2, H - M + 2], outline=INK, width=2)

    # ── سربرگ ──
    y = M + 8
    f_small = _font("body", 22)
    f_small_b = _font("bold", 22)
    _draw_rtl(d, XR, y, f"سال اول  ◂  شماره {_fa(issue_no)}", f_small_b, INK)
    _draw_rtl(d, XL, y, "بها: ۵ ریال", f_small_b, INK, anchor_right=False)
    cx = W // 2
    mid = "نشریهٔ مستقل  •  بی‌طرف  •  شبانه"
    d.text((cx - _tw(d, mid, f_small_b) / 2, y), _shape(mid), font=f_small_b, fill=RED,
           **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))
    y += 44

    # نام روزنامه
    title = NEWS_TITLE
    size = 138
    f_title = _font("title", size)
    while _tw(d, title, f_title) > (XR - XL) - 40 and size > 70:
        size -= 6
        f_title = _font("title", size)
    tw = _tw(d, title, f_title)
    d.text((cx - tw / 2 + 4, y + 4), _shape(title), font=f_title, fill=(120, 105, 80),
           **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))   # سایهٔ جوهر
    d.text((cx - tw / 2, y), _shape(title), font=f_title, fill=INK,
           **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))
    y += int(size * 1.45)
    _rules(d, XL, XR, y)
    y += 20
    date_txt = _fa_date(now)
    gt = f"ارگان خبری «{_clean_name(group_title, 28)}»" if group_title else ""
    _draw_rtl(d, XR, y, date_txt, f_small_b, INK)
    if gt:
        _draw_rtl(d, XL, y, gt, f_small_b, INK, anchor_right=False)
    y += 38
    _rules(d, XL, XR, y, thick=False)
    y += 30

    # ── تیتر اصلی ──
    f_h = _font("title", 76)
    hs = 76
    while hs > 44 and len(_wrap(d, ed["headline"], f_h, XR - XL)) > 2:
        hs -= 6
        f_h = _font("title", hs)
    for ln in _wrap(d, ed["headline"], f_h, XR - XL)[:2]:
        d.text((cx - _tw(d, ln, f_h) / 2, y), _shape(ln), font=f_h, fill=INK,
               **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))
        y += int(hs * 1.3)
    if ed.get("subhead"):
        f_sh = _font("bold", 30)
        for ln in _wrap(d, ed["subhead"], f_sh, XR - XL - 80)[:2]:
            d.text((cx - _tw(d, ln, f_sh) / 2, y), _shape(ln), font=f_sh, fill=RED,
                   **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))
            y += 44
    y += 10
    _rules(d, XL, XR, y)
    y += 28

    # ── ستون‌بندی: ستون راست = گزارش اصلی ؛ دو ستون چپ = خبرها ──
    gap = 26
    colw = (XR - XL - 2 * gap) // 3
    cols_top = y
    col_x = [XR, XR - colw - gap, XR - 2 * (colw + gap)]       # لبهٔ راست هر ستون
    f_body = _font("body", 25)
    f_art_t = _font("bold", 31)
    lh = 40

    # ستون ۱: گزارش اصلی (تعداد خط‌ها بر اساس فضای بالای نوار پایین محدود می‌شود)
    bottom_top = H - 560
    cy = cols_top
    d.rectangle([col_x[0] - colw, cy, col_x[0], cy + 44], fill=INK)
    _draw_rtl(d, col_x[0] - 14, cy + 4, "گزارش ویژه", _font("bold", 28), PAPER)
    cy += 62
    lead_lines = max(4, (bottom_top - 24 - cy) // lh)
    cy = _para(d, col_x[0], cy, ed["lead"], f_body, INK, colw, lh, max_lines=lead_lines)
    col1_end = cy

    # ستون ۲ و ۳: خبرها
    arts = ed["articles"][:3]
    slots = [(1, 0), (2, 0), (1, 1)] if len(arts) >= 3 else [(1, 0), (2, 0)]
    ys = {1: cols_top, 2: cols_top}
    for art, (ci, _) in zip(arts, slots):
        x = col_x[ci]
        yy = ys[ci]
        yy = _para(d, x, yy, art["title"], f_art_t, INK, colw, 42, max_lines=2)
        d.line([(x - colw, yy + 2), (x, yy + 2)], fill=INK, width=2)
        yy += 14
        yy = _para(d, x, yy, art["body"], f_body, INK, colw, lh, max_lines=9)
        ys[ci] = yy + 30

    # فضای آزاد ستون‌های ۲ و ۳: «سخن شب» در ستونی که جا بیشتر دارد، مهر در دیگری
    free = {1: bottom_top - 24 - ys[1], 2: bottom_top - 24 - ys[2]}
    q_col = 1 if free[1] >= free[2] else 2
    s_col = 2 if q_col == 1 else 1
    if ed.get("quote") and free[q_col] >= 170:
        qx = col_x[q_col]
        qy = bottom_top - 24 - min(free[q_col], 250)
        qlines = _wrap(d, f"«{ed['quote']['text']}»", _font("bold", 27), colw - 36)[:5]
        qh = 52 + len(qlines) * 40 + (40 if ed["quote"]["who"] else 8)
        qy = max(qy, bottom_top - 24 - qh)
        d.rectangle([qx - colw, qy, qx, qy + qh], outline=INK, width=3)
        _draw_rtl(d, qx - 16, qy + 8, "سخن شب", _font("bold", 26), RED)
        yy = qy + 50
        for ln in qlines:
            _draw_rtl(d, qx - 16, yy, ln, _font("bold", 27), INK)
            yy += 40
        if ed["quote"]["who"]:
            _draw_rtl(d, qx - 16, yy, f"— {ed['quote']['who']}", f_body, INK)
        free[q_col] = 0
    if free[s_col] >= 250 or (free[q_col] >= 250):
        sc_ = s_col if free[s_col] >= 250 else q_col
        sx = col_x[sc_] - colw // 2
        sy_ = bottom_top - 24 - 220
        _stamp(img, sx - 110, sy_ - 20, rnd, d)

    y = bottom_top
    _rules(d, XL, XR, y)
    y += 28

    # جعبهٔ آمار شبانه (راست) + خبرهای کوتاه (وسط) + آگهی/هواشناسی (چپ)
    box_h = H - M - 30 - y - 56
    # آمار
    bx1, bx0 = col_x[0], col_x[0] - colw
    d.rectangle([bx0, y, bx1, y + box_h], outline=INK, width=3)
    d.rectangle([bx0 + 6, y + 6, bx1 - 6, y + box_h - 6], outline=INK, width=1)
    sy = y + 16
    _draw_rtl(d, bx1 - 18, sy, "آمار شبانه", _font("bold", 30), RED)
    sy += 46
    f_st = _font("body", 24)
    lines = [f"پیام‌های مبادله‌شده: {_fa(stats['total'])}", f"اعضای فعال: {_fa(stats['members'])} نفر"]
    for i, (a, n) in enumerate(stats["top"][:3]):
        lines.append(f"{['اول', 'دوم', 'سوم'][i]}: {a} ({_fa(n)})")
    if stats.get("peak_hour") is not None:
        lines.append(f"اوج گفتگو: ساعت {_fa(stats['peak_hour'])}")
    if stats.get("night_owl"):
        lines.append(f"جغد شب: {stats['night_owl']}")
    k = stats["kinds"]
    for key, lab in (("voice", "ویس"), ("photo", "عکس"), ("sticker", "استیکر")):
        if k.get(key):
            lines.append(f"{lab}: {_fa(k[key])}")
    for ln in lines:
        if sy + 34 > y + box_h - 12:
            break
        sy = _para(d, bx1 - 18, sy, ln, f_st, INK, colw - 36, 34, max_lines=1)

    # خبرهای کوتاه
    mx1 = col_x[1]
    _draw_rtl(d, mx1, y + 4, "کوتاه از همه جا", _font("bold", 30), RED)
    my = y + 52
    for b in ed["briefs"][:3]:
        if my > y + box_h - 80:
            break
        d.rectangle([mx1 - 12, my + 12, mx1, my + 24], fill=RED)
        my = _para(d, mx1 - 24, my, b, f_st, INK, colw - 26, 34, max_lines=3) + 14

    # آگهی و هواشناسی
    lx = col_x[2]
    ly = y + 4
    if ed.get("ad"):
        bw0 = lx - colw
        ad_lines = _wrap(d, ed["ad"], _font("body", 23), colw - 28)[:6]
        ah = 50 + len(ad_lines) * 34 + 18
        d.rectangle([bw0, ly, lx, ly + ah], outline=INK, width=3)
        _draw_rtl(d, lx - 14, ly + 8, "آگهی", _font("bold", 28), RED)
        ay = ly + 48
        for ln in ad_lines:
            _draw_rtl(d, lx - 14, ay, ln, _font("body", 23), INK)
            ay += 34
        ly += ah + 22
    if ed.get("forecast") and ly < y + box_h - 90:
        _draw_rtl(d, lx, ly, "هواشناسی گروه", _font("bold", 28), RED)
        ly += 42
        _para(d, lx, ly, ed["forecast"], _font("body", 23), INK, colw, 34, max_lines=4)

    # پاورقی
    fy = H - M - 44
    _rules(d, XL, XR, fy - 14, thick=False)
    foot = f"چاپ و نشر: دفتر مرکزی ربات «{_bot_name}»  ◂  بدون مجوز، با محبت"
    d.text((cx - _tw(d, foot, f_small) / 2, fy), _shape(foot), font=f_small, fill=INK,
           **({"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}))

    return _age(img, rnd)


def _stamp(img, x, y, rnd, _d=None):
    from PIL import Image, ImageDraw
    stamp = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.ellipse([10, 10, 290, 290], outline=RED + (200,), width=8)
    sd.ellipse([28, 28, 272, 272], outline=RED + (200,), width=3)
    f1 = _font("title", 56)
    kw = {"direction": "rtl", "language": "fa"} if _shape_detect() == "raqm" else {}
    for txt, yy in (("نسخهٔ", 82), ("شبانه", 150)):
        sd.text((150 - _tw(sd, txt, f1) / 2, yy), _shape(txt), font=f1, fill=RED + (205,), **kw)
    stamp = stamp.rotate(rnd.uniform(-18, -9), expand=True, resample=Image.BICUBIC).resize((220, 220))
    img.paste(stamp, (int(x), int(y)), stamp)


def _age(img, rnd) -> bytes:
    """کاغذ کهنه: لکه، دانهٔ نویز، تاخوردگی، حاشیهٔ تیره، جوهر کمی نفوذکرده."""
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    w, h = img.size
    # جوهر کمی پخش
    blurred = img.filter(ImageFilter.GaussianBlur(0.8))
    img = Image.blend(img, blurred, 0.45)
    # لکه‌های کهنگی
    stains = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(stains)
    for _ in range(46):
        r = rnd.randint(40, 190)
        x, y = rnd.randint(-60, w + 60), rnd.randint(-60, h + 60)
        sd.ellipse([x - r, y - r, x + r, y + r], fill=rnd.randint(6, 22))
    stains = stains.filter(ImageFilter.GaussianBlur(70))
    brown = Image.new("RGB", (w, h), (140, 100, 50))
    img = Image.composite(brown, img, stains.point(lambda v: min(255, v * 3)))
    # نویز
    noise = Image.effect_noise((w, h), 28).convert("RGB")
    img = ImageChops.multiply(img, Image.blend(Image.new("RGB", (w, h), (255, 255, 255)), noise, 0.10))
    # تاخوردگی افقی و عمودی
    fold = Image.new("L", (w, h), 0)
    fd = ImageDraw.Draw(fold)
    fd.line([(0, h // 2), (w, h // 2)], fill=60, width=3)
    fd.line([(w // 2, 0), (w // 2, h)], fill=38, width=3)
    fold = fold.filter(ImageFilter.GaussianBlur(5))
    img = Image.composite(Image.new("RGB", (w, h), (90, 70, 45)), img, fold.point(lambda v: min(255, v)))
    # ویگنت
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([-w * 0.25, -h * 0.2, w * 1.25, h * 1.2], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(160))
    dark = Image.new("RGB", (w, h), (70, 50, 28))
    img = Image.composite(img, dark, vig)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


# ───────────────────────── تولید و انتشار ─────────────────────────
def build_edition(chat_id: int, group_title: str, issue_no: int, force: bool = False):
    """(jpeg_bytes, caption) یا (None, دلیل)"""
    now = datetime.now(TEHRAN_TZ)
    rows = _fetch(chat_id, time.time() - NEWS_WINDOW_HOURS * 3600)
    stats = compute_stats(rows)
    if stats["total"] < NEWS_MIN_MSGS and not force:
        return None, f"فقط {stats['total']} پیام بود (حداقل {NEWS_MIN_MSGS})"
    if stats["total"] < 3:
        return None, "تقریباً هیچ پیامی در ۲۴ ساعت گذشته ثبت نشده"
    ed = None
    try:
        raw = _llm(_SYSTEM, _build_user_prompt(rows, stats, group_title), 1600, 45)
        d = _parse_json(raw)
        if d:
            ed = _validate(d, rows)
            if not ed["lead"] or len(ed["articles"]) < 2:
                ed = None
    except Exception as e:
        logger.warning(f"[NEWS] LLM ناموفق: {e}")
    if ed is None:
        ed = _fallback_edition(stats)
    jpeg = render_poster(ed, stats, issue_no, now, group_title)
    cap = f"📰 {NEWS_TITLE} — شمارهٔ {_fa(issue_no)}\n{_fa_date(now)}"
    return jpeg, cap


def _publish(chat_id: int, title: str, manual: bool = False) -> tuple:
    cfg = _cfg(chat_id)
    issue = cfg["issue_no"] + 1
    jpeg, info = build_edition(chat_id, title, issue, force=manual)
    if jpeg is None:
        return False, info
    ok = _send_photo(chat_id, jpeg, info)
    if not ok:
        return False, "ارسال عکس ناموفق بود"
    today = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d")
    _set_cfg(chat_id, issue_no=issue, last_date=today if not manual else cfg["last_date"], fails=0,
             first_date=cfg["first_date"] or today)
    return True, "ok"


def _run_scheduled(chat_id: int, title: str):
    today = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d")
    try:
        ok, info = _publish(chat_id, title)
        if ok:
            logger.info(f"[NEWS] روزنامه گروه {chat_id} منتشر شد")
        elif info.startswith("فقط") or info.startswith("تقریباً"):
            _set_cfg(chat_id, last_date=today, fails=0)       # شب کم‌پیام: امشب دیگر تلاش نکن
            logger.info(f"[NEWS] گروه {chat_id}: {info}")
        else:
            cfg = _cfg(chat_id)
            f = cfg["fails"] + 1
            _set_cfg(chat_id, fails=f, last_date=today if f >= 3 else cfg["last_date"])
            logger.warning(f"[NEWS] گروه {chat_id} ناموفق ({f}/3): {info}")
    except Exception as e:
        logger.warning(f"[NEWS] گروه {chat_id}: {type(e).__name__}: {e}")
    finally:
        with _inflight_lock:
            _inflight.discard(chat_id)


def job():
    """هر ۱ دقیقه از scheduler. سبک است؛ کار سنگین در ترد‌پول انجام می‌شود."""
    if not (_ready and NEWS_ENABLED):
        return
    now = datetime.now(TEHRAN_TZ)
    today = now.strftime("%Y-%m-%d")
    nowm = now.hour * 60 + now.minute
    try:
        flush()
        with _db() as c:
            c.execute("SELECT chat_id, at_time, last_date, fails FROM news_cfg WHERE enabled=1")
            cfgs = list(c.fetchall() or ())
        groups = {int(g["group_id"]): g for g in (_get_groups() or ())}
        known = {int(r["chat_id"]) for r in cfgs}
        if NEWS_DEFAULT_ON:                                     # گروه‌هایی که هنوز تنظیمی ندارند
            default_t = _norm_time(NEWS_DEFAULT_TIME) or "23:00"
            for gid in groups:
                if gid not in known:
                    cfgs.append({"chat_id": gid, "at_time": default_t, "last_date": None, "fails": 0})
    except Exception as e:
        logger.warning(f"[NEWS] job: {e}")
        return
    for r in cfgs:
        gid = int(r["chat_id"])
        if gid not in groups or r["last_date"] == today:
            continue
        hh, mm = (r["at_time"] or "23:00").split(":")
        slot = int(hh) * 60 + int(mm)
        if not (slot <= nowm < slot + NEWS_CATCHUP_MIN):
            continue
        with _inflight_lock:
            if gid in _inflight:
                continue
            _inflight.add(gid)
        _pool.submit(_run_scheduled, gid, groups[gid].get("group_title") or "")


# ───────────────────────── دستورهای گروه ─────────────────────────
_ARG = (r"(?:روشن|خاموش|فعال|غیرفعال|on|off|الان|همین\s*الان|نمونه|now|test|"
        r"ساعت\s*[\d۰-۹٠-٩]{1,2}(?:\s*[:٫.]\s*[\d۰-۹٠-٩]{2})?|[\d۰-۹٠-٩]{1,2}\s*[:٫.]\s*[\d۰-۹٠-٩]{2})")
_CMD_RE = re.compile(r"^\s*(/?)(?:news|روزنامه)(?:@\w+)?(?:\s+(" + _ARG + r"))?\s*$", re.I)


def is_command(text: str) -> bool:
    return bool(text and len(text) < 80 and _CMD_RE.match(text.strip()))


def handle_command(m, reply) -> bool:
    """reply(text) پاسخ HTML می‌دهد. فقط ادمین‌های گروه. True یعنی پیام مصرف شد."""
    text = (getattr(m, "text", "") or "").strip()
    mt = _CMD_RE.match(text)
    if not mt or not _ready:
        return False
    if not NEWS_ENABLED:
        reply("📰 روزنامه شبانه در این ربات غیرفعال است.")
        return True
    chat_id = m.chat.id
    sc = getattr(m, "sender_chat", None)
    admin = True if (sc is not None and sc.id == chat_id) else _is_admin(chat_id, m.from_user.id)
    if not admin:
        if mt.group(1):                          # فقط اگر با / نوشته شده جواب بده؛ «روزنامه» ساده نادیده گرفته می‌شود
            reply("⛔️ فقط ادمین‌های گروه می‌توانند روزنامه را مدیریت کنند.")
        return True
    arg = _norm_fa(mt.group(2) or "")
    cfg = _cfg(chat_id)
    if re.match(r"^(روشن|on|فعال)$", arg, re.I):
        _set_cfg(chat_id, enabled=1, fails=0)
        reply(f"📰 روزنامه شبانه <b>روشن</b> شد؛ هر شب ساعت <b>{_fa(cfg['at_time'])}</b> منتشر می‌شود.")
    elif re.match(r"^(خاموش|off|غیرفعال)$", arg, re.I):
        _set_cfg(chat_id, enabled=0)
        reply("📰 روزنامه شبانه <b>خاموش</b> شد.")
    elif arg.startswith("ساعت") or re.match(r"^[\d۰-۹٠-٩]", arg):
        t = _norm_time(arg)
        if not t:
            reply("ساعت را این‌طوری بنویس: <code>روزنامه ساعت 23:30</code>")
        else:
            _set_cfg(chat_id, at_time=t, enabled=1, last_date=None if t > datetime.now(TEHRAN_TZ).strftime("%H:%M") else cfg["last_date"])
            reply(f"📰 از این پس هر شب ساعت <b>{_fa(t)}</b> (به وقت تهران) روزنامه منتشر می‌شود.")
    elif re.match(r"^(الان|همین الان|نمونه|now|test)$", arg, re.I):
        if getattr(getattr(m, "from_user", None), "id", None) != _owner_id:
            reply("⛔️ چاپ دستی روزنامه فقط برای مالک ربات است.")
            return True
        last = _manual_last.get(chat_id, 0)
        if time.time() - last < NEWS_MANUAL_COOLDOWN:
            reply(f"⏳ هر {_fa(NEWS_MANUAL_COOLDOWN // 60)} دقیقه فقط یک نسخهٔ دستی می‌شود چاپ کرد.")
            return True
        _manual_last[chat_id] = time.time()
        reply("🖨 دستگاه چاپ روشن شد؛ چند ثانیه صبر کن ...")

        def _go():
            ok, info = _publish(chat_id, getattr(m.chat, "title", "") or "", manual=True)
            if not ok:
                reply(f"📰 چاپ نشد: {info}")
        _pool.submit(_go)
    else:
        st = "✅ روشن" if cfg["enabled"] else "⛔️ خاموش"
        is_owner = getattr(getattr(m, "from_user", None), "id", None) == _owner_id
        reply("📰 <b>روزنامه شبانه</b>\n"
              f"وضعیت: {st}\nساعت انتشار: <b>{_fa(cfg['at_time'])}</b>\n\n"
              "<code>روزنامه روشن</code> / <code>روزنامه خاموش</code>\n"
              "<code>روزنامه ساعت 23:30</code>"
              + ("\n<code>روزنامه الان</code> (چاپ نسخهٔ نمونه)" if is_owner else ""))
    return True
