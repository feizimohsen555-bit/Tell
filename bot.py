#!/usr/bin/env python3
"""
ربات تلگرام کاهش حجم آهنگ
- کاربر آهنگ می‌فرستد
- ربات حجم و مشخصات را نشان می‌دهد و با دکمه شیشه‌ای می‌پرسد: زیر چند مگ؟
- آهنگ با FFmpeg (AAC / m4a) با بهترین کیفیتِ ممکن در همان حجم فشرده می‌شود
- همه تنظیمات از فایل .env خوانده می‌شود
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import copy
import functools
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    import mutagen
    from mutagen.asf import ASFTags
    from mutagen.flac import Picture
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags
except ImportError:  # بدون mutagen هم ربات بالا میاد؛ فقط متادیتای تحلیل خالی می‌ماند
    mutagen = None  # type: ignore[assignment]

load_dotenv()

# ───────────────────────── تنظیمات (.env) ─────────────────────────


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
LOCAL_API_URL = os.getenv("LOCAL_API_URL", "").strip().rstrip("/")
# ── سرور لوکال تلگرام ──
# حالت خودکار (برای هاست‌های بدون داکر مثل Falix): با AUTO_LOCAL_SERVER=1 ربات خودش فایل اجرایی
# telegram-bot-api رو دانلود و کنار خودش اجرا میکنه. بدون این حالت، LOCAL_API_URL باید به سروری که
# خودت بالا آوردی (مثلاً داکر) اشاره کنه.
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
AUTO_LOCAL_SERVER = os.getenv("AUTO_LOCAL_SERVER", "").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_SERVER_PORT = _int("LOCAL_SERVER_PORT", 8081)
LOCAL_SERVER_DIR = Path(os.getenv("LOCAL_SERVER_DIR", "./tgapi")).resolve()
LOCAL_SERVER_BIN_URL = os.getenv(
    "LOCAL_SERVER_BIN_URL",
    "https://github.com/BoBaHPyt/telegram-bot-api/releases/download/v7.2/telegram-bot-api",
).strip()
START_LOCAL_SERVER = AUTO_LOCAL_SERVER and not LOCAL_API_URL
if START_LOCAL_SERVER:
    LOCAL_API_URL = f"http://127.0.0.1:{LOCAL_SERVER_PORT}"
# پوشه‌ای که سرور لوکال فایل‌ها رو توش ذخیره می‌کنه (باید ربات هم به همین مسیر دسترسی داشته باشه)
LOCAL_FILES_DIR = Path(
    os.getenv("LOCAL_FILES_DIR") or (LOCAL_SERVER_DIR / "data" if START_LOCAL_SERVER else "/var/lib/telegram-bot-api")
)
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}
# آیدی عددی ادمین: فقط این کاربر دکمه‌ی «پنل ادمین» رو توی کیبورد ریپلای می‌بینه (0 = پنل غیرفعال)
ADMIN_ID = _int("ADMIN_ID", 0)
# دیتابیس PostgreSQL (مثلاً Neon رایگان) برای هاست‌هایی که دیسکشون با هر ری‌استارت/دیپلوی پاک میشه، مثل Render.
# اگه DATABASE_URL ست باشه، همه‌ی دیتا (کاربران، متن‌ها، منو، بن، پرمیوم، آمار) اونجا ذخیره میشه؛ اگه خالی باشه مثل قبل فایل محلی botdata.json.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PG_SWEEP_GAP = 1800  # ذخیره‌ی دوره‌ایِ بی‌تغییر-مهم (مثل «آخرین فعالیت») حداکثر هر این‌قدر ثانیه؛ تا دیتابیس رایگان کمتر بیدار بمونه
# دکمه‌ی «پشتیبانی» پیام شروع: پروفایل این آیدی عددی باز میشه (می‌تونی با SUPPORT_URL مثلاً https://t.me/username بذاری)
SUPPORT_ID = _int("SUPPORT_ID", 5637609683)
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip() or f"tg://user?id={SUPPORT_ID}"
# فایل ذخیره‌ی کاربران/آمار/لیست بن
DATA_FILE = Path(os.getenv("DATA_FILE", "./botdata.json"))
# محدودیت‌های پلن رایگان (0 = بدون محدودیت). کاربرهای پرمیوم و ادمین از این محدودیت‌ها معافن.
FREE_DAILY_FILES = _int("FREE_DAILY_FILES", 0)  # حداکثر تعداد آهنگ در روز
FREE_MAX_MB = _int("FREE_MAX_MB", 0)  # حداکثر حجم هر آهنگ ورودی (مگ)
SIZE_OPTIONS = [
    float(x) for x in re.findall(r"\d+(?:\.\d+)?", os.getenv("SIZE_OPTIONS_MB", "1,2,3,5,10,20"))
] or [1, 2, 3, 5, 10, 20]
MIN_KBPS = _int("MIN_BITRATE_KBPS", 32)
MAX_KBPS = _int("MAX_BITRATE_KBPS", 256)
AUTO_KBPS = _int("AUTO_BITRATE_KBPS", 128)
MAX_PARALLEL = max(1, _int("MAX_PARALLEL_JOBS", 2))
JOB_TTL = _int("JOB_TTL_MIN", 30) * 60
KEEP_TTL = _int("JOB_KEEP_MIN", 5) * 60  # بعد از ارسال فایل، آهنگ این‌قدر (از آخرین فعالیت) روی دیسک می‌مونه، مگه «تمام» زده بشه
TEMP_DIR = Path(os.getenv("TEMP_DIR", "./tmp"))
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_PATH", "ffprobe")

# ── وب‌سرور سلامت (برای Render) ──
# رندر برای Web Service حتماً یه پورت باز روی 0.0.0.0 می‌خواد؛ پورت از متغیر PORT خونده میشه.
PORT = _int("PORT", 10000)
# آدرس عمومی سرویس برای پینگ خودکار (رندر خودش RENDER_EXTERNAL_URL رو ست می‌کنه). خالی = پینگ خاموش.
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
# فاصله‌ی پینگ (ثانیه)؛ رندر بعد از ۱۵ دقیقه بدون ترافیک ورودی سرویس رایگان رو میخوابونه، پس باید کمتر از اون باشه.
KEEPALIVE_INTERVAL = max(60, _int("KEEPALIVE_INTERVAL_SEC", 600))

# محافظت از تصاویر: protect_content تلگرام = جلوگیری از فوروارد/شیر و ذخیره‌ی پیام (0 = خاموش)
PROTECT_IMAGES = os.getenv("PROTECT_IMAGES", "1").strip().lower() not in {"0", "false", "no", "off"}  # کارت‌های گرافیکی (عکس)
PROTECT_COVERS = os.getenv("PROTECT_COVERS", "1").strip().lower() not in {"0", "false", "no", "off"}  # فایل کاور استخراج‌شده

MB = 1024 * 1024
SAFETY = 0.96  # حاشیه امن برای سربار کانتینر
# سقف فایل ورودی: 100 مگ (با سرور لوکال تا 2 گیگ). توجه: Bot API معمولی تلگرام فقط تا 20 مگ دانلود میدهد.
DOWNLOAD_LIMIT = 2000 * MB if LOCAL_API_URL else 100 * MB
UPLOAD_LIMIT = 2000 * MB if LOCAL_API_URL else 50 * MB
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".ape", ".mp4"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")

SEM: Optional[asyncio.Semaphore] = None
BOT: Optional[Bot] = None

# ───────────────────────── ابزارهای نمایشی ─────────────────────────

_FA = str.maketrans("", "")  # همه‌ی اعداد لاتین هستن (تبدیل به ارقام فارسی عمداً خاموشه)


def fa(x) -> str:
    return str(x).translate(_FA)


def mb(nbytes: float) -> str:
    return fa(f"{nbytes / MB:.1f}")


def label_mb(v: float) -> str:
    return fa(int(v) if float(v).is_integer() else v)


def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    return fa(f"{sec // 60}:{sec % 60:02d}")


def quality_word(kbps: int) -> str:
    if kbps >= 128:
        return T("qual.1")
    if kbps >= 96:
        return T("qual.2")
    if kbps >= 64:
        return T("qual.3")
    if kbps >= 40:
        return T("qual.4")
    return T("qual.5")


# ───────────────────────── مدل Job ─────────────────────────


@dataclass
class Job:
    id: str
    user_id: int
    dir: Path
    src: Path
    name: str
    size: int
    duration: float
    kbps: int
    title: str
    artist: str
    thumb: Optional[Path]
    created: float
    busy: bool = False
    cover: Optional[Path] = None  # کاور بزرگ‌تر برای کارت گرافیکی
    has_card: bool = False  # اگه کارت گرافیکی فرستاده شده، اطلاعات توی عکسه و کپشن کوتاهه
    cl_parts: Optional[Tuple[bool, bool, bool]] = None  # (کاور دارد، ترانه دارد، ترانه تایم‌دار)؛ None = توی فایل نه کاور هست نه ترانه
    codec: str = ""  # کدک صوتی ورودی (ffprobe)؛ برای تبدیل فرمت
    sample_rate: int = 0
    pic_codec: str = ""  # کدک تصویر داخل فایل (mjpeg/png = کاور قابل‌کپی)
    # ── ویرایش تگ و کاور ──
    tg_info: Optional[Dict[str, Any]] = None  # عکس لحظه‌ای تگ‌های فایل (kind/title/artist/album/has_cover)؛ None = باید دوباره خونده بشه
    tg_new: Dict[str, str] = dc_field(default_factory=dict)  # تغییرات در انتظار اعمال (مقدار "" = پاک‌کردن تگ)
    tg_cover: Optional[Path] = None  # کاور جدیدِ آماده‌شده (در انتظار اعمال)
    tg_drop_cover: bool = False  # حذف کاور فعلی (در انتظار اعمال)
    # ── برش بخش دلخواه ──
    tr_start: Optional[float] = None  # شروع برش (ثانیه)؛ None = هنوز زمانی وارد نشده
    tr_end: Optional[float] = None  # پایان برش (ثانیه)
    tr_fade: bool = False  # محو شدن ابتدا و انتهای برش
    # ── بررسی اصالت کیفیت (طیفی) برای کارت اولیه؛ نتیجه‌ی همون موتور «آنالیز دقیق» ──
    verdict: Optional[str] = None  # genuine | suspicious | fake؛ None = بررسی نشده / نامشخص
    # ── نرمال‌سازی لودنس ──
    loud: Optional[Dict[str, float]] = None  # لودنس اندازه‌گیری‌شده‌ی فایل (lufs / lra / true_peak)؛ None = هنوز اندازه‌گیری نشده
    bg: Optional[Any] = None  # تسک پیش‌محاسبه‌ی تحلیل (پس‌زمینه)
    # ── گزینه‌های بعد از ارسال فایل ──
    used: set = dc_field(default_factory=set)  # کارهایی که روی این آهنگ انجام شده (red/cv/tr/nm/wm/ana/tg/cl)
    ttl: float = 0  # اگه صفر نباشه، جای JOB_TTL میاد (بعد از اولین ارسال = KEEP_TTL)
    more_msg: Optional[Tuple[int, int]] = None  # (chat_id, message_id) پیامی که الان دکمه‌های «گزینه‌های دیگر» زیرشه
    timer: Optional[Any] = None  # تایمر پاک‌شدن خودکار


JOBS: Dict[str, Job] = {}


class OutputTooBig(Exception):
    """فایل خروجی از سقف ارسال تلگرام بزرگ‌تر شد (آرگومان = حجم به بایت)."""


def drop_job(job_id: str) -> None:
    job = JOBS.pop(job_id, None)
    if job:
        if job.timer is not None:
            job.timer.cancel()
            job.timer = None
        _strip_more(job)
        if job.bg is not None and not job.bg.done():
            job.bg.cancel()
        forget_analysis(job.dir)
        shutil.rmtree(job.dir, ignore_errors=True)


def sweep() -> None:
    now = time.time()
    for jid, job in list(JOBS.items()):
        if not job.busy and now - job.created > (job.ttl or JOB_TTL):
            drop_job(jid)


def _strip_more(job: Job) -> None:
    """دکمه‌های «گزینه‌های دیگر» رو از زیر پیام آخر برمی‌داره (بعد از پاک‌شدن فایل‌ها دیگه کار نمیکنن)."""
    ref, job.more_msg = job.more_msg, None
    if ref is None or BOT is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.ensure_future(_quiet(BOT.edit_message_reply_markup(chat_id=ref[0], message_id=ref[1], reply_markup=None)))


def _expire_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if job is None:
        return
    left = job.created + KEEP_TTL - time.time()
    if job.busy or left > 0:  # فعالیتی بوده یا کاری در جریانه؛ از آخرین فعالیت دوباره ۵ دقیقه
        wait = 15 if job.busy else left + 1
        job.timer = asyncio.get_running_loop().call_later(wait, _expire_job, job_id)
        return
    drop_job(job_id)


def arm_expiry(job: Job) -> None:
    """از این لحظه، آهنگ KEEP_TTL ثانیه بعد از آخرین فعالیت خودکار پاک میشه (یا با دکمه‌ی «تمام»)."""
    job.ttl = KEEP_TTL
    job.created = time.time()
    if job.timer is not None:
        job.timer.cancel()
    job.timer = asyncio.get_running_loop().call_later(KEEP_TTL + 1, _expire_job, job.id)


async def sweeper() -> None:
    while True:
        await asyncio.sleep(300)
        sweep()
        prune_premium()
        db_save(urgent=False)


# ───────────────────────── FFmpeg ─────────────────────────


async def run(*args: str) -> Tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def probe(path: Path) -> dict:
    rc, out, err = await run(
        FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)
    )
    if rc != 0:
        raise ValueError("ffprobe failed: " + err.decode(errors="ignore")[-200:])
    data = json.loads(out or b"{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("no audio stream")
    duration = 0.0
    for v in (fmt.get("duration"), audio.get("duration")):
        try:
            duration = float(v)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    tags = {k.lower(): v for k, v in {**audio.get("tags", {}), **fmt.get("tags", {})}.items()}
    pic = next((s for s in streams if s.get("codec_type") == "video"), None)
    try:
        sample_rate = int(audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    return {
        "duration": duration,
        "title": (tags.get("title") or "").strip(),
        "artist": (tags.get("artist") or tags.get("album_artist") or "").strip(),
        "codec": (audio.get("codec_name") or "").lower(),
        "sample_rate": sample_rate,
        "pic_codec": ((pic or {}).get("codec_name") or "").lower(),
    }


async def extract_thumb(src: Path, dst: Path) -> Optional[Path]:
    rc, _, _ = await run(
        FFMPEG, "-y", "-v", "error", "-i", str(src), "-an", "-frames:v", "1",
        "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "4", str(dst),
    )
    if rc == 0 and dst.exists() and 0 < dst.stat().st_size < 200 * 1024:
        return dst
    return None


async def extract_cover(src: Path, dst: Path) -> Optional[Path]:
    """کاور داخل فایل (در صورت وجود) با کیفیت بهتر برای کارت گرافیکی"""
    rc, _, _ = await run(
        FFMPEG, "-y", "-v", "error", "-i", str(src), "-an", "-frames:v", "1",
        "-vf", "scale=640:640:force_original_aspect_ratio=decrease", "-q:v", "3", str(dst),
    )
    if rc == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    return None


def audio_params(kbps: int) -> Tuple[int, int]:
    """(تعداد کانال، sample rate) مناسب برای هر بیت‌ریت تا کیفیت در حجم کم بهتر بماند"""
    if kbps >= 64:
        return 2, 44100
    if kbps >= 40:
        return 1, 32000
    return 1, 24000


async def encode(job: Job, kbps: int, limit: Optional[int]) -> Tuple[Path, int, int]:
    """فشرده‌سازی؛ اگر حجم از limit بیشتر شد با بیت‌ریت کمتر دوباره تلاش می‌کند."""
    out: Optional[Path] = None
    size = 0
    for _ in range(3):
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        out = job.dir / f"out_{uuid.uuid4().hex[:6]}.m4a"
        ch, ar = audio_params(kbps)
        rc, _, err = await run(
            FFMPEG, "-y", "-v", "error", "-i", str(job.src),
            "-map", "0:a:0", "-vn", "-map_metadata", "0",
            "-c:a", "aac", "-b:a", f"{kbps}k", "-ac", str(ch), "-ar", str(ar),
            "-movflags", "+faststart", str(out),
        )
        if rc != 0 or not out.exists():
            raise RuntimeError(err.decode(errors="ignore")[-300:])
        size = out.stat().st_size
        if limit is None or size <= limit or kbps <= MIN_KBPS:
            return out, kbps, size
        kbps = max(MIN_KBPS, int(kbps * limit / size * 0.97))
    return out, kbps, size  # type: ignore[return-value]


# ───────────────────────── منطق گزینه‌ها ─────────────────────────


def kbps_for(limit_bytes: float, duration: float) -> int:
    return int(limit_bytes * SAFETY * 8 / duration / 1000)


def plan(job: Job):
    opts = []
    hidden = False
    for v in SIZE_OPTIONS:
        limit = v * MB
        if limit >= job.size * 0.9:
            continue
        k = min(MAX_KBPS, kbps_for(limit, job.duration))
        if k < MIN_KBPS:
            hidden = True
            continue
        opts.append((v, k))
    show_auto = job.kbps > AUTO_KBPS * 1.15
    return opts, hidden, show_auto


def _song(job: Job) -> str:
    return job.title + (f" — {job.artist}" if job.artist else "")


def render(job: Job, note: str = ""):
    opts, hidden, show_auto = plan(job)
    head = T("red.head", song=_song(job), title=job.title, artist=job.artist)
    text = "" if job.has_card else (  # با کارت گرافیکی، مشخصات توی خود عکسه
        f"{head}\n"
        + T("red.info", size=mb(job.size), duration=fmt_dur(job.duration), kbps=fa(job.kbps)) + "\n\n"
    )
    buttons = []
    if not opts and not show_auto:
        text += T("red.small")
    else:
        text += T("red.ask")
        row = []
        for v, k in opts:
            row.append(InlineKeyboardButton(T("btn.size", mb=label_mb(v), kbps=fa(k)), callback_data=f"sz:{job.id}:{v:g}", api_kwargs=BLUE))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if show_auto:
            buttons.append([InlineKeyboardButton(T("btn.auto", kbps=fa(AUTO_KBPS)), callback_data=f"sz:{job.id}:auto", api_kwargs=BLUE)])
        if hidden:
            min_mb = MIN_KBPS * 1000 * job.duration / 8 / SAFETY / MB
            text += "\n\n" + T("red.hidden", min_mb=fa(f"{min_mb:.1f}"))
    buttons.append([
        InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back"),
        InlineKeyboardButton(T("btn.cancel"), callback_data=f"sz:{job.id}:x", api_kwargs=STYLE_RED)
    ])
    if note:
        text = f"{note}\n\n{text}"
    return text, InlineKeyboardMarkup(buttons)


def render_menu(job: Job, note: str = ""):
    if job.has_card:
        text = T("menu.ask")
    else:
        verdict = {"fake": T("menu.fake") + "\n", "suspicious": T("menu.suspicious") + "\n"}.get(job.verdict or "", "")
        text = (
            T("menu.head", song=_song(job), title=job.title, artist=job.artist) + "\n"
            + T("menu.info", size=mb(job.size), duration=fmt_dur(job.duration), kbps=fa(job.kbps)) + "\n"
            + verdict
            + "\n" + T("menu.ask")
        )
    if note:
        text = f"{note}\n\n{text}"
    rows = [[InlineKeyboardButton(label, callback_data=f"mn:{job.id}:{act}", api_kwargs={"style": "primary"})
             for act, label in grp] for grp in menu_layout(menu_actions(job))]
    rows.append([InlineKeyboardButton(T("btn.cancel_delete"), callback_data=f"sz:{job.id}:x", api_kwargs=STYLE_RED)])
    return text, InlineKeyboardMarkup(rows)


STYLE_BLUE = {"style": "primary"}  # دکمه‌ی آبی (Bot API 9.4+) مثل دکمه‌ی «پشتیبانی»
STYLE_RED = {"style": "danger"}  # دکمه‌ی قرمز (لغو / هشدار)
BLUE = STYLE_BLUE  # برای کدهای قبلی


# ترتیب پیش‌فرض دکمه‌های منوی آهنگ + اسم هرکدوم برای پنل ادمین («ترتیب دکمه‌های منو»)
MENU_DEFAULT_ORDER: List[str] = ["red", "cv", "tr", "nm", "bs", "wm", "ana", "tg", "cl"]
MENU_ACT_NAMES: Dict[str, str] = {
    "red": "🗜 کاهش حجم", "cv": "🔄 تبدیل فرمت", "tr": "✂️ برش بخش دلخواه", "nm": "🔊 نرمال‌سازی لودنس",
    "bs": "🔈 افزایش بیس", "wm": "✍️ امضای فرکانسی", "ana": "🔍 آنالیز دقیق", "tg": "✏️ ویرایش تگ و کاور",
    "cl": "🖼📝 کاور و ترانه",
}


def menu_order() -> List[str]:
    """ترتیب کامل همه‌ی دکمه‌های منو: اول چیزی که ادمین چیده، بقیه (جدیدها/نچیده‌ها) به ترتیب پیش‌فرض."""
    saved = [a for a in DB.get("menu_order", []) if a in MENU_DEFAULT_ORDER]
    out: List[str] = []
    for a in saved + MENU_DEFAULT_ORDER:
        if a not in out:
            out.append(a)
    return out


def menu_layout(acts: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    """دکمه‌ها رو ردیف‌بندی میکنه: دکمه‌ای که ادمین «کنار قبلی» زده، با دکمه‌ی قبلیِ قابل‌نمایش توی یه ردیف میاد (حداکثر ۲ تا)."""
    joined = set(DB.get("menu_join", []))
    rows: List[List[Tuple[str, str]]] = []
    for a, label in acts:
        if rows and a in joined and len(rows[-1]) < 2:
            rows[-1].append((a, label))
        else:
            rows.append([(a, label)])
    return rows


def menu_actions(job: Job, also: Optional[str] = None) -> List[Tuple[str, str]]:
    """(کار، متن دکمه) برای همه‌ی کارهایی که هنوز روی این آهنگ انجام نشده؛ also = کاری که همین الان داره تموم میشه."""
    used = set(job.used) | ({also} if also else set())
    acts: List[Tuple[str, str]] = [("red", T("btn.red"))]
    if CONVERT_ENABLED:
        acts.append(("cv", T("btn.cv")))
    if TRIM_ENABLED:
        acts.append(("tr", T("btn.tr")))
    if NORM_ENABLED:
        acts.append(("nm", T("btn.nm", lufs=fa(f'{NORM_TARGET:.0f}'))))
    if BASS_ENABLED:
        acts.append(("bs", T("btn.bs")))
    if WM_ENABLED:
        acts.append(("wm", T("btn.wm")))
    acts.append(("ana", T("btn.ana")))
    if TAGS_ENABLED:
        acts.append(("tg", T("btn.tg")))
    cl_label = cl_button_label(job.cl_parts)
    if cl_label:  # فقط وقتی توی فایل کاور/ترانه هست
        acts.append(("cl", cl_label))
    rank = {a: i for i, a in enumerate(menu_order())}
    acts.sort(key=lambda x: rank.get(x[0], len(rank)))  # sort پایدار؛ ترتیب ادمین، وگرنه پیش‌فرض
    return acts  # همه‌ی دکمه‌ها همیشه نمایش داده میشن (دکمه‌ی استفاده‌شده حذف نمیشه)


def more_markup(job: Job, also: Optional[str] = None) -> InlineKeyboardMarkup:
    """دکمه‌های زیر فایل ارسالی: بقیه‌ی کارهای استفاده‌نشده برای همین آهنگ + «تمام»."""
    rows = [[InlineKeyboardButton(label, callback_data=f"ot:{job.id}:{act}", api_kwargs={"style": "primary"})
             for act, label in grp] for grp in menu_layout(menu_actions(job, also))]
    rows.append([InlineKeyboardButton(T("btn.finish"), callback_data=f"ot:{job.id}:done", api_kwargs=STYLE_RED)])
    return InlineKeyboardMarkup(rows)


async def after_delivery(job: Job, action: str, sent: Any) -> None:
    """فایل با موفقیت رفت: کار «استفاده‌شده» حساب میشه، دکمه‌های قبلی (اگه بود) برداشته میشن و تایمر ۵ دقیقه‌ای شروع میشه."""
    try:
        job.used.add(action)
        old = job.more_msg
        new = (sent.chat_id, sent.message_id) if sent is not None else None
        if old is not None and old != new and BOT is not None:
            await _quiet(BOT.edit_message_reply_markup(chat_id=old[0], message_id=old[1], reply_markup=None))
        job.more_msg = new
        arm_expiry(job)
    except Exception:  # noqa: BLE001 — خطای این بخش نباید جلوی کار اصلی رو بگیره
        log.exception("after_delivery failed")


class PanelDone(str):
    """متنِ «فایل ارسال شد…»؛ فقط یه علامتِ نوع‌دار تا safe_edit بتونه پنل‌های موقت رو به‌جای ادیت، جمع کنه."""


# پنل‌های موقتی که زیر فایل ارسالی باز میشن (on_more)؛ بعد از ارسال فایل، پیام «فایل ارسال شد» دیگه لازم نیست
# (دکمه‌ها همون زیر خود فایلن) پس پنل جمع میشه و چت یه پیام اضافه نمی‌گیره.
TRANSIENT_PANELS: set = set()


def _pkey(message) -> Tuple[int, int]:
    return (message.chat_id, message.message_id)


def panel_done_text(note: str = "") -> str:
    return PanelDone((f"{note}\n\n" if note else "") + T("more.panel_done", minutes=fa(max(1, KEEP_TTL // 60))))


async def safe_edit(message, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if isinstance(text, PanelDone) and _pkey(message) in TRANSIENT_PANELS:
        TRANSIENT_PANELS.discard(_pkey(message))
        try:
            await message.delete()
            return
        except TelegramError:
            log.warning("transient panel delete failed; editing instead")
    try:
        if message.photo:  # پیام کارتِ گرافیکیه؛ متن همون کپشنه
            await message.edit_caption(caption=text[:1024], reply_markup=markup)
        else:
            await message.edit_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


async def _quiet(coro) -> None:
    """کار تلگرامیِ پس‌زمینه؛ خطای شبکه/تلگرام فقط لاگ میشه (نباید کار اصلی رو خراب کنه)"""
    try:
        await coro
    except TelegramError as e:
        log.warning("background telegram call failed: %s", e)


def edit_bg(message, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> "asyncio.Future":
    """پیام وضعیت («در حال ... ») رو در پس‌زمینه ادیت میکنه تا کار اصلی منتظر یه رفت‌وبرگشت به تلگرام نمونه.
    قبل از ادیت بعدی همون پیام حتماً باید await settle(task) بشه تا ترتیب ادیت‌ها بهم نخوره."""
    return asyncio.ensure_future(_quiet(safe_edit(message, text, markup)))


def ack_edit_bg(q, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> "asyncio.Future":
    """answer() کلیک + ادیت پیام وضعیت، هر دو هم‌زمان و در پس‌زمینه"""
    return asyncio.ensure_future(asyncio.gather(_quiet(q.answer()), _quiet(safe_edit(q.message, text, markup))))


async def settle(task: Optional["asyncio.Future"]) -> None:
    if task is not None:
        with contextlib.suppress(Exception):
            await task


async def _drop_user_input(msg) -> bool:
    """پیامِ ورودیِ کاربر (متن / عکس) رو پاک میکنه تا چت شلوغ نشه؛ فایل آهنگ هرگز پاک نمیشه.
    خروجی: True = از نوع ورودیِ پاک‌شدنی بود."""
    if not (msg.text or msg.photo or (msg.document and _tg_is_image_doc(msg.document))):
        return False
    with contextlib.suppress(TelegramError):
        await msg.delete()
    return True


# ───────────────────────── ذخیره‌سازی کاربران و آمار ─────────────────────────

START_TIME = time.time()
DB: Dict[str, Any] = {"users": {}, "banned": [], "files_in": 0, "files_out": 0, "premium": {}, "texts": {}, "ad": {}, "menu_order": [], "menu_join": []}
BANNED: set = set()


def _parse_db(raw: str) -> bool:
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if isinstance(data, dict):
        for k in DB:
            if k in data:
                DB[k] = data[k]
    BANNED.clear()
    BANNED.update(int(x) for x in DB.get("banned", []))
    if not isinstance(DB.get("texts"), dict):  # متن‌های ویرایش‌شده‌ی ادمین
        DB["texts"] = {}
    if not isinstance(DB.get("ad"), dict):  # تنظیمات بنر تبلیغات
        DB["ad"] = {}
    if not isinstance(DB.get("menu_order"), list):  # ترتیب دکمه‌های منوی آهنگ (ادمین)
        DB["menu_order"] = []
    if not isinstance(DB.get("menu_join"), list):  # دکمه‌هایی که کنار دکمه‌ی قبلی توی یه ردیف میشینن
        DB["menu_join"] = []
    return True


# ── لایه‌ی ذخیره‌سازی ──
# DATABASE_URL ست باشه → کل دیتا به‌صورت یک ردیف توی PostgreSQL (جدول kv) ذخیره میشه و با ری‌استارت پاک نمیشه.
# ست نباشه → فایل محلی DATA_FILE (مثل قبل).
_DB_DIRTY = False
_DB_SEQ = 0  # شماره‌ی اسنپ‌شات؛ تا یه اسنپ‌شات قدیمی‌تر هیچ‌وقت روی جدیدتر نوشته نشه
_DB_WRITTEN_SEQ = 0
_DB_HASH = ""
_PG_READY = False  # فقط بعد از یه لود موفق از دیتابیس True میشه؛ قبلش نوشتن ممنوعه (تا دیتای خالی روی دیتای واقعی نیفته)
_PG_LAST_WRITE = 0.0
_DB_LOCK = threading.Lock()


def _hash(data: str) -> str:
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def _pg_connect() -> Any:
    try:
        import psycopg
    except ImportError as e:
        raise SystemExit("DATABASE_URL ست شده ولی psycopg نصب نیست؛ `psycopg[binary]>=3.2.10` رو به requirements.txt اضافه کن.") from e
    # هر بار یه اتصال کوتاه: با خوابیدنِ خودکارِ دیتابیس (scale-to-zero) مشکل «اتصال مُرده» پیش نمیاد
    return psycopg.connect(DATABASE_URL, connect_timeout=10, autocommit=True)


def _pg_read() -> Optional[str]:
    with _pg_connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated TIMESTAMPTZ NOT NULL DEFAULT now())")
        row = c.execute("SELECT v FROM kv WHERE k = 'botdata'").fetchone()
    return row[0] if row else None


def _pg_write(data: str) -> None:
    with _pg_connect() as c:
        c.execute(
            "INSERT INTO kv (k, v) VALUES ('botdata', %s) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated = now()",
            (data,),
        )


def _db_dump() -> str:
    DB["banned"] = sorted(BANNED)
    return json.dumps(DB, ensure_ascii=False)


def _db_snapshot() -> Tuple[int, str]:
    """همیشه توی event loop صدا زده میشه (سریالایز بدون ریسک تغییر هم‌زمان دیکشنری)."""
    global _DB_SEQ
    _DB_SEQ += 1
    return _DB_SEQ, _db_dump()


def db_load() -> None:
    global _PG_READY, _DB_HASH, _DB_DIRTY
    raw: Optional[str] = None
    if DATABASE_URL:
        err: Optional[Exception] = None
        for attempt in range(1, 6):
            try:
                raw = _pg_read()
                _PG_READY = True
                break
            except Exception as e:  # noqa: BLE001
                err = e
                log.warning("اتصال به دیتابیس ناموفق (تلاش %d از 5): %s: %s", attempt, type(e).__name__, e)
                time.sleep(4)
        if not _PG_READY:
            # عمداً بالا نمیاد: با دیتای خالی بالا اومدن و بعد نوشتن روی دیتابیس = از دست رفتن همه‌چیز
            raise SystemExit(f"به دیتابیس وصل نشد ({type(err).__name__}: {err}). DATABASE_URL رو چک کن.")
    from_file = False
    if raw is None:  # دیتابیس خالیه (یا حالت فایلی): اگه botdata.json محلی هست ازش لود کن
        try:
            raw = DATA_FILE.read_text(encoding="utf-8")
            from_file = True
        except OSError:
            raw = None
    if raw is None or not _parse_db(raw):
        log.info("دیتای قبلی پیدا نشد؛ با دیتای خالی شروع میشه.")
        return
    if DATABASE_URL:
        if from_file:  # ایمپورت یک‌باره‌ی botdata.json به دیتابیس
            log.info("botdata.json محلی به دیتابیس منتقل میشه.")
            _DB_DIRTY = True
        else:
            _DB_HASH = _hash(_db_dump())
        log.info("دیتا از PostgreSQL لود شد (%d کاربر).", len(DB.get("users", {})))


def _db_write(seq: int, data: str) -> bool:
    """نوشتن توی thread امنه. True = موفق (یا چیزی برای نوشتن نبود)."""
    global _DB_WRITTEN_SEQ, _DB_HASH, _PG_LAST_WRITE
    with _DB_LOCK:
        if seq < _DB_WRITTEN_SEQ:
            return True
        try:
            if DATABASE_URL:
                if not _PG_READY:
                    return False
                h = _hash(data)
                if h != _DB_HASH:  # بدون تغییر = بدون اتصال (دیتابیس رایگان می‌تونه بخوابه)
                    _pg_write(data)
                    _DB_HASH = h
                    _PG_LAST_WRITE = time.time()
            else:
                tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, DATA_FILE)
        except Exception:  # noqa: BLE001
            log.exception("db save failed")
            return False
        _DB_WRITTEN_SEQ = seq
        return True


def db_save(urgent: bool = True, now: bool = False) -> None:
    global _DB_DIRTY
    if DATABASE_URL and not now:
        # توی event loop به شبکه وصل نمیشیم؛ db_saver حداکثر ۳ ثانیه بعد توی thread مینویسه
        if urgent or time.time() - _PG_LAST_WRITE >= PG_SWEEP_GAP:
            _DB_DIRTY = True
        return
    seq, data = _db_snapshot()
    _db_write(seq, data)


# ذخیره‌ی دیتا روی هر آهنگ/هر کاربر جدید، همون لحظه و توی event loop انجام نمیشه (دیسک/شبکه‌ی کند = کل ربات منتظر).
# فقط علامت میخوره و حداکثر چند ثانیه بعد، نوشتن توی thread جدا انجام میشه.
def db_save_soon() -> None:
    global _DB_DIRTY
    _DB_DIRTY = True


async def db_saver() -> None:
    global _DB_DIRTY
    while True:
        await asyncio.sleep(3)
        if _DB_DIRTY:
            _DB_DIRTY = False
            seq, data = _db_snapshot()
            ok = await asyncio.to_thread(_db_write, seq, data)
            if not ok:  # دیتابیس در دسترس نبود: بعداً دوباره
                _DB_DIRTY = True
                await asyncio.sleep(12)


def touch_user(user: Any) -> None:
    """ثبت/به‌روزرسانی کاربر (فقط کسانی که اجازه‌ی استفاده دارن)."""
    if user is None:
        return
    key = str(user.id)
    new = key not in DB["users"]
    rec = DB["users"].setdefault(key, {"first": time.time(), "files": 0})
    rec["name"] = user.full_name or ""
    rec["username"] = user.username or ""
    rec["last"] = time.time()
    if new:
        db_save_soon()


def count_file(user_id: int, kind: str) -> None:
    """kind: 'in' = فایل دریافت‌شده، 'out' = فایل فشرده‌ی ارسال‌شده."""
    DB["files_" + kind] = int(DB.get("files_" + kind, 0)) + 1
    if kind == "in":
        rec = DB["users"].get(str(user_id))
        if rec is not None:
            rec["files"] = int(rec.get("files", 0)) + 1
            today = time.strftime("%Y-%m-%d")
            if rec.get("day") != today:
                rec["day"], rec["day_n"] = today, 0
            rec["day_n"] = int(rec.get("day_n", 0)) + 1
    db_save_soon()


def files_today(user_id: int) -> int:
    rec = DB["users"].get(str(user_id))
    if rec and rec.get("day") == time.strftime("%Y-%m-%d"):
        return int(rec.get("day_n", 0))
    return 0


def is_admin(user_id: Optional[int]) -> bool:
    return bool(ADMIN_ID) and user_id == ADMIN_ID


# مدت‌های پرمیوم: کلید ← (ثانیه، برچسب). None = دائمی
PREMIUM_DURS: Dict[str, Tuple[Optional[int], str]] = {
    "1d": (86400, "یک روزه"),
    "30d": (30 * 86400, "یک ماهه"),
    "perm": (None, "دائمی"),
}


def premium_status(user_id: int) -> Optional[float]:
    """None = پرمیوم نیست | math.inf = دائمی | در غیر این صورت ثانیه‌های باقی‌مانده."""
    v = DB.get("premium", {}).get(str(user_id))
    if v is None:
        return None
    if v == -1:
        return math.inf
    rem = float(v) - time.time()
    return rem if rem > 0 else None


def is_premium(user_id: int) -> bool:
    return is_admin(user_id) or premium_status(user_id) is not None


def grant_premium(user_id: int, dur: str) -> None:
    secs = PREMIUM_DURS[dur][0]
    prem = DB.setdefault("premium", {})
    if secs is None:
        prem[str(user_id)] = -1
    else:
        cur = prem.get(str(user_id), 0)
        base = max(time.time(), float(cur)) if cur and cur > 0 else time.time()  # تمدید از انتهای پلن فعلی
        prem[str(user_id)] = base + secs
    db_save()


def revoke_premium(user_id: int) -> bool:
    had = DB.get("premium", {}).pop(str(user_id), None) is not None
    if had:
        db_save()
    return had


def prune_premium() -> None:
    prem = DB.get("premium", {})
    for k in [k for k, v in prem.items() if v != -1 and float(v) <= time.time()]:
        prem.pop(k, None)


def resolve_user(text: str) -> Optional[int]:
    """آیدی عددی یا @username (یا لینک t.me) ← آیدی عددی. یوزرنیم فقط برای کسانی که قبلاً به ربات پیام دادن پیدا میشه."""
    t = re.sub(r"^(?:https?://)?t\.me/", "", text.strip(), flags=re.I).lstrip("@").strip().lower()
    if re.fullmatch(r"\d+", t):
        return int(t)
    if re.fullmatch(r"[a-z0-9_]{3,}", t):
        for uid, rec in DB["users"].items():
            if (rec.get("username") or "").lower() == t:
                return int(uid)
    return None


def user_label(user_id: int) -> str:
    rec = DB["users"].get(str(user_id)) or {}
    name = (rec.get("name") or "").strip()
    uname = f"@{rec['username']}" if rec.get("username") else ""
    parts = [x for x in (name, uname) if x]
    return f"{' '.join(parts)} ({user_id})" if parts else str(user_id)


# ───────────────────────── متن‌های قابل‌ویرایش از پنل ادمین ─────────────────────────
# همه‌ی هدرها، بدنه‌ها و اسم دکمه‌هایی که کاربر می‌بینه اینجا ثبت شدن (کلید ← متن پیش‌فرض).
# ادمین از «پنل ادمین ← ویرایش متن‌ها» هر کدوم رو عوض میکنه؛ تغییرات توی DB["texts"] (فایل botdata.json) ذخیره میشه
# و بلافاصله و بدون ری‌استارت اعمال میشه. توی متن‌ها {نام} = جای مقدار پویا (مثلاً {size} = حجم آهنگ).
# استفاده توی کد: T("کلید", size=...) ← متن نهایی (ویرایش‌شده یا پیش‌فرض).


@dataclass
class TextDef:
    key: str
    group: str
    kind: str  # head | body | btn
    label: str
    default: str
    vars: Tuple[str, ...] = ()


TEXTS: Dict[str, TextDef] = {}
TEXT_GROUPS: List[str] = []
TEXT_IDS: Dict[str, str] = {}  # شناسه‌ی کوتاه (برای callback_data) ← کلید
TEXT_SID: Dict[str, str] = {}  # کلید ← شناسه‌ی کوتاه
KIND_ICON = {"head": "🏷", "body": "📝", "btn": "🔘"}
KIND_NAME = {"head": "هدر", "body": "بدنه", "btn": "دکمه"}
TX_MAX_BTN = 60  # حداکثر طول اسم دکمه
TX_MAX_TEXT = 1000  # حداکثر طول هدر/بدنه (کپشن عکس تلگرام 1024 کاراکتره)
TX_PAGE = 8  # تعداد آیتم در هر صفحه‌ی پنل
_PH = re.compile(r"\{(\w+)\}")

# توضیح متغیرها (نمایش توی پنل ادمین) و مقدار نمونه (برای پیش‌نمایش)
PH_DOC: Dict[str, str] = {
    "title": "عنوان آهنگ", "artist": "نام خواننده (ممکنه خالی باشه)", "song": "«عنوان — خواننده»",
    "size": "حجم", "duration": "مدت آهنگ (دقیقه:ثانیه)", "kbps": "بیت‌ریت (عدد)",
    "min_mb": "حداقل حجم منطقی (مگ)", "mb": "عدد مگ", "label": "برچسب فرمت/حجم (مثلاً «MP3 · 192k»)",
    "name": "نام فرمت", "src": "کدک فایل اصلی + « · » (ممکنه خالی باشه)", "est": "حجم تقریبی خروجی (مگ)",
    "old": "حجم قبلی (مگ)", "limit": "سقف/محدودیت", "max": "حداکثر مجاز", "quality": "واژه‌ی کیفیت (عالی/خوب/…)",
    "start": "زمان شروع برش", "end": "زمان پایان برش", "length": "طول برش", "m4r_max": "سقف طول رینگتون (ثانیه)",
    "min": "حداقل مجاز", "dur": "مدت پلن پرمیوم", "lufs": "عدد LUFS", "word": "توصیف لودنس (نزدیک هدف/خیلی بلند/…)",
    "target": "هدف لودنس (LUFS)", "peak": "True Peak (dBTP)", "gain": "تغییر صدا (dB)", "before": "لودنس قبل",
    "after": "لودنس بعد", "emoji": "ایموجی بخش", "field": "نام بخش (عنوان/خواننده/آلبوم)", "current": "مقدار فعلی",
    "what": "نام کار/بخش", "a": "مورد اول", "b": "مورد دوم", "dims": "ابعاد تصویر (ممکنه خالی باشه)",
    "ext": "پسوند فایل", "state": "وضعیت کاور (دارد/ندارد/…)", "used": "تعداد استفاده‌ی امروز", "left": "زمان باقی‌مانده", "level": "سطح اطمینان",
    "text": "متن امضا", "minutes": "مدت نگهداری فایل (دقیقه)",
    "db": "مقدار تقویت بیس (dB)", "lvl": "نام شدت بیس (ملایم/متعادل/قوی)",
}
PH_SAMPLE: Dict[str, str] = {
    "title": "Shape of You", "artist": "Ed Sheeran", "song": "Shape of You — Ed Sheeran", "size": "8.4",
    "duration": "3:54", "kbps": "256", "min_mb": "1.5", "mb": "5", "label": "MP3 · 192k", "name": "MP3",
    "src": "FLAC · ", "est": "5.6", "old": "31.2", "limit": "5", "max": "50", "quality": "عالی", "start": "0:30",
    "end": "1:00", "length": "0:30", "m4r_max": "40", "min": "1", "dur": "یک ماهه", "lufs": "-14",
    "word": "نزدیک هدف", "target": "-14", "peak": "-0.8", "gain": "+3.2", "before": "-17.2", "after": "-14.1",
    "emoji": "🎵", "field": "عنوان", "current": "Shape of You", "what": "کاور", "a": "کاور", "b": "متن ترانه",
    "dims": "1000×1000 • ", "ext": "JPG", "state": "دارد", "used": "3", "left": "29 روز دیگه", "level": "بالا",
    "text": "@mychannel", "minutes": "5", "db": "5", "lvl": "متعادل",
}


def _reg(group: str, kind: str, key: str, label: str, default: str, vars_: Tuple[str, ...]) -> None:
    if key in TEXTS:
        raise ValueError(f"duplicate text key: {key}")
    unknown = set(_PH.findall(default)) - set(vars_)
    if unknown:
        raise ValueError(f"{key}: default uses undeclared vars {sorted(unknown)}")
    sid = hashlib.md5(key.encode()).hexdigest()[:8]
    if sid in TEXT_IDS:
        raise ValueError(f"text id collision: {key}")
    if group not in TEXT_GROUPS:
        TEXT_GROUPS.append(group)
    TEXTS[key] = TextDef(key, group, kind, label, default, tuple(vars_))
    TEXT_IDS[sid] = key
    TEXT_SID[key] = sid


def _mk(group: str):
    """(h, b, n) = ثبت‌کننده‌ی هدر / بدنه / دکمه برای یه گروه."""

    def h(key: str, label: str, default: str, *vars_: str) -> None:
        _reg(group, "head", key, label, default, vars_)

    def b(key: str, label: str, default: str, *vars_: str) -> None:
        _reg(group, "body", key, label, default, vars_)

    def n(key: str, label: str, default: str, *vars_: str) -> None:
        _reg(group, "btn", key, label, default, vars_)

    return h, b, n


def _fill(tpl: str, kw: Dict[str, Any]) -> str:
    """{نام} ← مقدار؛ متغیر ناشناخته دست‌نخورده می‌مونه (هیچ‌وقت خطا نمیده)."""
    return _PH.sub(lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), tpl)


def T(key: str, **kw: Any) -> str:
    """متن نهایی یه کلید: نسخه‌ی ویرایش‌شده‌ی ادمین، وگرنه پیش‌فرض."""
    custom = DB.get("texts", {}).get(key)
    if isinstance(custom, str) and custom.strip():
        return _fill(custom, kw)
    return _fill(TEXTS[key].default, kw)


# ── پیام شروع ──
h, b, n = _mk("👋 پیام شروع (/start)")
h("start.head", "سلام اول پیام شروع", "سلام 👋")
b("start.body", "متن اصلی پیام شروع", (
    "آهنگت رو بفرست (به‌صورت آهنگ یا فایل)، بعد انتخاب می‌کنی چند مگ بشه.\n"
    "من حجمش رو میارم پایین و تا جای ممکن کیفیتش رو حفظ می‌کنم 🎧\n"
    "اگه آهنگت کاور یا ترانه داشته باشه، میتونی با کیفیت اصلی ازش بگیری 🖼📝\n"
    "فرمتش رو هم میتونم عوض کنم (MP3 / Opus / M4A / FLAC) 🔄\n"
    "هر بخش دلخواهی رو هم میتونی ببری، برای رینگتون یا پیش‌نمایش ✂️\n"
    "صدای آهنگ‌های خیلی بلند یا خیلی کم رو هم میتونم روی لودنس استاندارد تنظیم کنم 🔊\n"
    "عنوان، خواننده، آلبوم و کاورش رو هم میتونی ویرایش کنی ✏️"
))
b("start.limit", "خط حداکثر حجم ورودی", "حداکثر حجم فایل ورودی: {limit}", "limit")
b("start.footer", "متن آخر پیام شروع (بالای دکمه‌ها)",
  "👇 برای دیدن راهنمای کامل یا ارتباط با پشتیبانی از دکمه‌های زیر استفاده کن.")
n("btn.help", "دکمه‌ی راهنما (پیام شروع)", "📖 راهنما")
n("btn.support", "دکمه‌ی پشتیبانی (پیام شروع)", "💬 پشتیبانی")
b("help.body", "متن راهنما (با دکمه‌ی «راهنما» یا /help نمایش داده میشه)", (
    "📖 راهنمای ربات\n\n"
    "1️⃣ آهنگت رو بفرست (به‌صورت آهنگ یا فایل صوتی).\n"
    "2️⃣ از منویی که میاد کاری که میخوای رو انتخاب کن:\n"
    "🗜 کاهش حجم: انتخاب کن زیر چند مگ بشه\n"
    "🔄 تبدیل فرمت (MP3 / Opus / M4A / FLAC)\n"
    "✂️ برش بخش دلخواه برای رینگتون یا پیش‌نمایش\n"
    "🔊 نرمال‌سازی لودنس\n"
    "🔈 افزایش بیس (طبیعی و بدون خفه‌شدن وکال)\n"
    "🔍 آنالیز دقیق کیفیت و مشخصات\n"
    "✏️ ویرایش عنوان، خواننده، آلبوم و کاور\n"
    "🖼📝 استخراج کاور و ترانه\n"
    "3️⃣ فایل آماده رو همین‌جا تحویل میگیری.\n\n"
    "با دستور /plan وضعیت پلنت رو ببین. اگه سوال یا مشکلی داشتی روی «پشتیبانی» بزن 💬"
))
b("start.admin_on", "پیام فعال‌بودن پنل (فقط ادمین)", "👑 پنل ادمین فعاله.")

# ── دکمه‌های منو (منوی آهنگ + دکمه‌های /start + دکمه‌های مشترک) ──
h, b, n = _mk("🔘 دکمه‌های منو و دکمه‌های مشترک")
n("btn.red", "دکمه: کاهش حجم", "🗜 کاهش حجم")
n("btn.cv", "دکمه: تبدیل فرمت", "🔄 تبدیل فرمت (MP3 / Opus / …)")
n("btn.tr", "دکمه: برش", "✂️ برش بخش دلخواه (رینگتون / پیش‌نمایش)")
n("btn.nm", "دکمه: نرمال‌سازی لودنس", "🔊 نرمال‌سازی لودنس (حدود {lufs} LUFS)", "lufs")
n("btn.bs", "دکمه: افزایش بیس", "🔈 افزایش بیس (طبیعی)")
n("btn.wm", "دکمه: امضای فرکانسی", "✍️ امضای فرکانسی")
n("btn.ana", "دکمه: آنالیز دقیق", "🔍 آنالیز دقیق کیفیت و مشخصات")
n("btn.tg", "دکمه: ویرایش تگ و کاور", "✏️ ویرایش تگ و کاور")
n("btn.cl_cover", "دکمه‌ی منو وقتی فایل کاور دارد", "🖼 {what}", "what")
n("btn.cl_lyrics", "دکمه‌ی منو وقتی فقط ترانه دارد", "📝 {what}", "what")
b("cl.w.cover", "کلمه‌ی «کاور» (داخل دکمه)", "کاور")
b("cl.w.synced", "کلمه‌ی «ترانه‌ی تایم‌دار» (داخل دکمه)", "ترانه‌ی تایم‌دار")
b("cl.w.lyrics", "کلمه‌ی «متن ترانه» (داخل دکمه)", "متن ترانه")
b("cl.join", "اتصال «کاور و ترانه» داخل دکمه", "{a} و {b}", "a", "b")
n("btn.cancel_delete", "دکمه: لغو و حذف فایل", "❌ لغو و حذف فایل")
n("btn.help_back", "دکمه: بازگشت از راهنما به پیام شروع", "🔙 بازگشت")
n("btn.finish", "دکمه: تمام (زیر فایل ارسالی)", "✅ تمام")
n("btn.back_menu", "دکمه: بازگشت به منو (همه‌ی صفحه‌ها)", "🔙 بازگشت به منو")
n("btn.cancel", "دکمه: لغو (صفحه‌ی انتخاب حجم)", "❌ لغو")
n("btn.cancel_x", "دکمه: انصراف (برش و ویرایش تگ)", "↩️ انصراف")

# ── منوی آهنگ ──
h, b, n = _mk("🎵 منوی آهنگ")
h("menu.head", "هدر منو (نام آهنگ)", "🎵 {song}", "song", "title", "artist")
b("menu.info", "خط مشخصات (وقتی کارت گرافیکی نیست)", "📦 حجم: {size} مگ  |  ⏱ {duration}  |  🎚 حدود {kbps} kbps",
  "size", "duration", "kbps")
b("menu.fake", "هشدار فیک بودن", "🚨 مشکوک به فیک: آپ‌اسکیل‌شده")
b("menu.suspicious", "هشدار مشکوک بودن", "⚠️ مشکوک به آپ‌اسکیل")
b("menu.ask", "سؤال منو", "چه کاری روی این آهنگ انجام بدم؟")

# ── کاهش حجم ──
h, b, n = _mk("🗜 کاهش حجم")
h("red.head", "هدر صفحه‌ی انتخاب حجم (نام آهنگ)", "🎵 {song}", "song", "title", "artist")
b("red.info", "خط مشخصات", "📦 حجم فعلی: {size} مگ  |  ⏱ {duration}  |  🎚 حدود {kbps} kbps", "size", "duration", "kbps")
b("red.small", "پیام «از قبل کم‌حجمه»",
  "این فایل از قبل کم‌حجمه و کمتر از این نمیشه بدون افت محسوس کیفیت کوچیکش کرد 👌")
b("red.ask", "سؤال انتخاب حجم", "دوست داری چند مگ باشه؟ (عدد کنار هر گزینه = کیفیت خروجی)")
b("red.hidden", "توضیح گزینه‌های حذف‌شده",
  "ℹ️ کمتر از حدود {min_mb} مگ برای این آهنگ باعث افت شدید کیفیت میشه، پس گزینه‌اش نیست.", "min_mb")
n("btn.size", "دکمه‌ی انتخاب حجم", "زیر {mb} مگ · {kbps}k", "mb", "kbps")
n("btn.auto", "دکمه‌ی خودکار", "✨ خودکار (بهترین تعادل · {kbps}k)", "kbps")
b("red.label", "برچسب حجم (داخل پیام «در حال…»)", "زیر {mb} مگ", "mb")
b("red.progress", "پیام در حال پردازش", "⏳ دارم کیفیت رو بهینه می‌کنم ({label})…", "label")
b("red.caption", "کپشن فایل ارسالی", "🎧 {size} مگ  •  {kbps} kbps ({quality})\nحجم قبلی: {old} مگ",
  "size", "kbps", "quality", "old")
b("red.caption_over", "هشدار کپشن: کمی بیشتر از حد خواسته‌شده",
  "⚠️ چون آهنگ خیلی طولانیه، کمی بیشتر از حد خواسته‌شده شد.")
b("more.panel_done", "پیام پنل بعد از ارسال فایل (دکمه‌ها زیر خود فایل هستن)",
  "✅ فایل ارسال شد. برای کار دیگه روی همین آهنگ از دکمه‌های زیر فایل استفاده کن.\nبا زدن «تمام» یا {minutes} دقیقه بعد از آخرین کار، فایل‌ها از سرور پاک میشن.", "minutes")
b("more.opening", "پیام موقت هنگام باز شدن یه گزینه از زیر فایل", "⏳ ...")
b("more.finished", "پیام بعد از زدن «تمام»", "✅ تمام شد و فایل‌ها از سرور پاک شدن.\nبرای کار دیگه، آهنگ رو دوباره بفرست.")
b("red.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد.\nیه حجم کوچک‌تر انتخاب کن.", "size", "limit")
b("red.fail", "خطای کلی", "❌ مشکلی پیش اومد. یه بار دیگه امتحان کن.")
b("red.cancelled", "پیام لغو", "❌ لغو شد.")
b("qual.1", "واژه‌ی کیفیت: بالای 128k", "عالی")
b("qual.2", "واژه‌ی کیفیت: 96 تا 127k", "خیلی خوب")
b("qual.3", "واژه‌ی کیفیت: 64 تا 95k", "خوب")
b("qual.4", "واژه‌ی کیفیت: 40 تا 63k", "قابل‌قبول")
b("qual.5", "واژه‌ی کیفیت: زیر 40k", "پایین")

# ── تبدیل فرمت ──
h, b, n = _mk("🔄 تبدیل فرمت")
h("cv.fmt.head", "هدر صفحه‌ی انتخاب فرمت", "🔄 فایل فعلی: {src}{size} مگ", "src", "size")
b("cv.fmt.ask", "سؤال انتخاب فرمت", "به چه فرمتی تبدیل کنم؟")
n("btn.cvf.mp3", "دکمه‌ی فرمت MP3", "🎵 MP3 (سازگار با همه)")
n("btn.cvf.opus", "دکمه‌ی فرمت Opus", "🪶 Opus (حجم خیلی کم)")
n("btn.cvf.aac", "دکمه‌ی فرمت M4A/AAC", "🎼 M4A / AAC")
n("btn.cvf.flac", "دکمه‌ی فرمت FLAC", "💎 FLAC (بدون افت)")
h("cv.rate.head", "هدر صفحه‌ی انتخاب بیت‌ریت", "{name} — بیت‌ریت رو انتخاب کن (کنار هر گزینه = حجم تقریبی خروجی)", "name")
b("cv.rate.toolong", "خطا: آهنگ خیلی طولانیه", "😕 این آهنگ اونقدر طولانیه که خروجی‌ش از سقف ارسال تلگرام بیشتر میشه.")
b("cv.rate.opus", "توضیح Opus", "ℹ️ Opus توی حجم کم از MP3 کیفیت بهتری میده؛ فقط بعضی پلیرهای قدیمی پخشش نمی‌کنن.")
b("cv.rate.lossy", "هشدار فایل اصلی لاسی",
  "⚠️ فایل اصلی لاسیه (حدود {kbps}k)؛ تبدیلش کمی افت کیفیت داره و بیت‌ریت بالاتر از اون چیزی رو بهتر نمیکنه.", "kbps")
n("btn.cv.v0", "دکمه‌ی MP3 VBR V0", "✨ VBR V0 (بهترین) · ≈{est} مگ", "est")
n("btn.cv.rate", "دکمه‌ی بیت‌ریت", "{kbps}k · ≈{est} مگ", "kbps", "est")
n("btn.cv.back", "دکمه‌ی بازگشت به انتخاب فرمت", "🔙 انتخاب فرمت")
b("cv.progress", "پیام در حال تبدیل", "⏳ دارم به {label} تبدیل می‌کنم…", "label")
b("cv.caption", "کپشن فایل ارسالی", "🎧 {label} • {size} مگ\nحجم قبلی: {old} مگ", "label", "size", "old")
b("cv.done", "پیام موفقیت", "✅ به {label} تبدیل شد.", "label")
b("cv.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد؛ بیت‌ریت کمتری انتخاب کن.", "size", "limit")
b("cv.fail", "خطای کلی", "❌ تبدیل ناموفق بود. یه بار دیگه امتحان کن.")

# ── برش ──
h, b, n = _mk("✂️ برش بخش دلخواه")
h("tr.prompt.head", "هدر صفحه‌ی وارد کردن زمان", "✂️ برش بخش دلخواه")
b("tr.prompt.body", "بدنه‌ی صفحه‌ی وارد کردن زمان", (
    "⏱ مدت آهنگ: {duration}\n\n"
    "زمان شروع و پایان رو بفرست (با فاصله یا خط تیره):\n\n"
    "• 0:30 1:00\n• 1:10-1:40\n• 45 75  (ثانیه)\n• 2:00 تا آخر\n\n"
    "💡 رینگتون معمولاً 20 تا 30 ثانیه‌ست (آیفون حداکثر {m4r_max} ثانیه)."
), "duration", "m4r_max")
h("tr.fmt.head", "هدر صفحه‌ی انتخاب فرمت برش", "✂️ برش بخش دلخواه")
b("tr.fmt.body", "بدنه‌ی صفحه‌ی انتخاب فرمت برش", (
    "▶️ از {start} تا {end}  •  مدت: {length}\n"
    "🎚 محو شدن (Fade) ابتدا و انتها: {fade}\n\n"
    "خروجی رو به چه فرمتی بدم؟"
), "start", "end", "length", "fade")
b("tr.fade.on", "کلمه‌ی Fade روشن (توی متن)", "روشن")
b("tr.fade.off", "کلمه‌ی Fade خاموش (توی متن)", "خاموش")
b("tr.fmt.m4r_note", "توضیح محدودیت M4R", "ℹ️ برای رینگتون آیفون (M4R) طول برش باید حداکثر {m4r_max} ثانیه باشه.", "m4r_max")
n("btn.tr.mp3", "دکمه‌ی MP3", "🎵 MP3")
n("btn.tr.aac", "دکمه‌ی M4A", "🎼 M4A (AAC)")
n("btn.tr.m4r", "دکمه‌ی M4R", "📱 M4R (رینگتون آیفون)")
n("btn.tr.voice", "دکمه‌ی ارسال به‌صورت ویس", "🎙 به‌صورت ویس")
n("btn.tr.fade_on", "دکمه‌ی Fade وقتی روشنه", "🎚 Fade: ✅ روشن")
n("btn.tr.fade_off", "دکمه‌ی Fade وقتی خاموشه", "🎚 Fade: ⬜ خاموش")
n("btn.tr.change", "دکمه‌ی تغییر زمان", "✏️ تغییر زمان")
b("tr.e.usage", "خطا: زمان‌ها فهمیده نشد", (
    "😕 متوجه زمان‌ها نشدم. شروع و پایان رو بنویس، مثلاً:\n"
    "0:30 1:00\n1:10-1:40\n45 75  (ثانیه)\n2:00 تا آخر\n\n"
    "یا برای برگشت «انصراف» رو بزن."
))
b("tr.e.start", "خطا: شروع بعد از پایان آهنگ", "😕 شروع باید قبل از پایان آهنگ باشه (مدت آهنگ: {dur}).", "dur")
b("tr.e.end", "خطا: پایان قبل از شروع", "😕 پایان باید بعد از شروع باشه.")
b("tr.e.min", "خطا: برش خیلی کوتاه", "😕 بخش انتخابی باید حداقل {min} ثانیه باشه.", "min")
b("tr.note.clamped", "یادآوری: پایان تا آخر آهنگ بریده شد", "ℹ️ پایانِ واردشده از طول آهنگ بیشتر بود؛ تا آخر آهنگ برش میخوره.")
b("tr.alert_m4r", "پاپ‌آپ: رینگتون خیلی بلنده", "رینگتون آیفون حداکثر {m4r_max} ثانیه‌ست؛ برش رو کوتاه‌تر کن.", "m4r_max")
b("tr.progress", "پیام در حال برش", "⏳ دارم بخش {start} تا {end} رو به {label} می‌برم…", "start", "end", "label")
b("tr.caption", "کپشن فایل ارسالی", "✂️ برش: {start} → {end}\n⏱ مدت: {length}  •  {label}  •  {size}",
  "start", "end", "length", "label", "size")
b("tr.done", "پیام موفقیت", "✅ برش به {label} ارسال شد. میتونی همین برش رو به فرمت دیگه‌ای هم بگیری.", "label")
b("tr.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد؛ بخش کوتاه‌تری انتخاب کن.", "size", "limit")
b("tr.fail", "خطای کلی", "❌ برش ناموفق بود. یه بار دیگه امتحان کن.")

# ── امضای فرکانسی ──
h, b, n = _mk("✍️ امضای فرکانسی")
h("wm.prompt.head", "هدر صفحه‌ی وارد کردن متن امضا", "✍️ امضای فرکانسی")
b("wm.prompt.body", "بدنه‌ی صفحه‌ی وارد کردن متن امضا", (
    "متنی که میخوای توی طیف‌نگار (اسپکتروگرام) آهنگ دیده بشه رو بفرست:\n\n"
    "• حداکثر ۳ خط، هر خط تا {max} کاراکتر\n"
    "• فارسی و انگلیسی پشتیبانی میشه"
), "max")
b("wm.e.long", "خطا: متن خالی یا خیلی بلند", "😕 متن باید خالی نباشه و هر خط حداکثر {max} کاراکتر باشه.", "max")
b("wm.progress", "پیام در حال ثبت امضا", "⏳ دارم امضای «{text}» رو روی طیف آهنگ ثبت میکنم… (ممکنه چند ثانیه طول بکشه)", "text")
b("wm.caption", "کپشن فایل ارسالی", "✍️ امضای فرکانسی: {text}\n🎧 {song}  •  {size}", "text", "song", "size")
b("wm.done", "پیام موفقیت", "✅ امضا ثبت و فایل ارسال شد. با باز کردنش توی یه طیف‌نگار (مثل Spek) متن دیده میشه.")
b("wm.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد.", "size", "limit")
b("wm.fail", "خطای کلی", "❌ ثبت امضا ناموفق بود. یه بار دیگه امتحان کن.")

# ── نرمال‌سازی ──
h, b, n = _mk("🔊 نرمال‌سازی لودنس")
b("nm.noloud", "خطا: لودنس اندازه‌گیری نشد", "😕 نتونستم لودنس این فایل رو اندازه بگیرم (شاید فایل خیلی ساکته یا خرابه).")
h("nm.head", "هدر صفحه‌ی نرمال‌سازی", "🔊 نرمال‌سازی لودنس")
b("nm.cur", "خط لودنس فعلی", "🎚 لودنس فعلی: {lufs} LUFS ({word})", "lufs", "word")
b("nm.target", "خط هدف", "🎯 هدف: {target} LUFS", "target")
b("nm.peak", "خط True Peak", "📈 True Peak فعلی: {peak} dBTP", "peak")
b("nm.ok", "پیام «نیازی به نرمال‌سازی نیست»", "👌 لودنس این آهنگ از قبل نزدیک هدفه؛ نیازی به نرمال‌سازی نیست.")
b("nm.gain", "خط تغییر صدا", "↕️ تغییر صدا: {gain} dB", "gain")
b("nm.limited", "هشدار لیمیتر", "⚠️ پیک آهنگ بالاست؛ برای رسیدن به هدف یه لیمیتر ملایم هم اعمال میشه (ممکنه کمی کمتر از هدف بمونه).")
b("nm.maxgain", "توضیح سقف تغییر صدا", "ℹ️ حداکثر تغییر {max} dB اعمال میشه.", "max")
b("nm.lossy", "هشدار فایل اصلی lossy", "ℹ️ فایل اصلی lossy هست؛ خروجی دوباره انکود میشه و افت کیفیت خیلی جزئی داره.")
b("nm.ask", "سؤال انتخاب فرمت", "خروجی رو به چه فرمتی بدم؟")
n("btn.nm.same", "دکمه‌ی «همان فرمت»", "✅ همان فرمت ({label})", "label")
b("nm.w.vloud", "توصیف: خیلی بلند", "خیلی بلند")
b("nm.w.loud", "توصیف: بلندتر از هدف", "بلندتر از هدف")
b("nm.w.near", "توصیف: نزدیک هدف", "نزدیک هدف")
b("nm.w.quiet", "توصیف: کم‌صداتر از هدف", "کم‌صداتر از هدف")
b("nm.w.vquiet", "توصیف: خیلی کم‌صدا", "خیلی کم‌صدا")
b("nm.measuring", "پیام در حال اندازه‌گیری", "⏳ دارم لودنس آهنگ رو اندازه می‌گیرم…")
b("nm.progress", "پیام در حال نرمال‌سازی", "⏳ دارم لودنس رو روی {target} LUFS تنظیم می‌کنم ({label})…", "target", "label")
h("nm.cap_head", "هدر کپشن فایل ارسالی", "🔊 لودنس نرمال شد")
b("nm.cap_after", "کپشن: قبل ← بعد", "🎚 قبل: {before} LUFS → بعد: {after} LUFS", "before", "after")
b("nm.cap_target", "کپشن: قبل ← هدف", "🎚 قبل: {before} LUFS → هدف: {target} LUFS", "before", "target")
b("nm.cap_fmt", "کپشن: فرمت و حجم", "🎧 {label} • {size} مگ", "label", "size")
b("nm.cap_limited", "کپشن: هشدار لیمیتر", "⚠️ برای جلوگیری از کلیپ شدن، لیمیتر ملایم اعمال شد.")
b("nm.done", "پیام موفقیت", "✅ فایل با لودنس نرمال‌شده ارسال شد.")
b("nm.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد؛ فرمت دیگه‌ای انتخاب کن.", "size", "limit")
b("nm.fail", "خطای کلی", "❌ نرمال‌سازی ناموفق بود. یه بار دیگه امتحان کن.")

# ── افزایش بیس ──
h, b, n = _mk("🔈 افزایش بیس")
h("bs.head", "هدر صفحه‌ی افزایش بیس", "🔈 افزایش بیس")
b("bs.about", "توضیح قابلیت",
  "بیس فقط توی محدوده‌ی صدای بم و به‌صورت طبیعی تقویت میشه؛ وکال و سازها دست‌نخورده می‌مونن، "
  "صدای کل آهنگ جبران میشه و کلیپ یا خفه‌شدن اتفاق نمیفته.")
b("bs.lossy", "هشدار فایل اصلی lossy", "ℹ️ فایل اصلی lossy هست؛ خروجی دوباره انکود میشه و افت کیفیت خیلی جزئی داره.")
b("bs.out", "خط فرمت خروجی", "🎧 خروجی: همان فرمت ({label})", "label")
b("bs.ask", "سؤال انتخاب شدت", "شدت بیس رو انتخاب کن:")
n("btn.bs.soft", "دکمه: ملایم", "🔈 ملایم (+{db} dB)", "db")
n("btn.bs.mid", "دکمه: متعادل", "🔉 متعادل – پیشنهادی (+{db} dB)", "db")
n("btn.bs.strong", "دکمه: قوی", "🔊 قوی (+{db} dB)", "db")
n("btn.bs.extreme", "دکمه: خیلی قوی", "💥 خیلی قوی (+{db} dB)", "db")
b("bs.w.soft", "نام شدت: ملایم", "ملایم")
b("bs.w.mid", "نام شدت: متعادل", "متعادل")
b("bs.w.strong", "نام شدت: قوی", "قوی")
b("bs.w.extreme", "نام شدت: خیلی قوی", "خیلی قوی")
b("bs.progress", "پیام در حال پردازش", "⏳ دارم بیس رو تقویت می‌کنم ({lvl})…", "lvl")
h("bs.cap_head", "هدر کپشن فایل ارسالی", "🔈 بیس تقویت شد")
b("bs.cap_level", "کپشن: شدت", "💪 شدت: {lvl} (+{db} dB)", "lvl", "db")
b("bs.cap_auto", "کپشن: تنظیم خودکار", "🎛 چون آهنگ از قبل بیس زیادی داشت، میزان تقویت خودکار کمتر شد.")
b("bs.cap_fmt", "کپشن: فرمت و حجم", "🎧 {label} • {size} مگ", "label", "size")
b("bs.cap_limited", "کپشن: هشدار لیمیتر", "⚠️ برای جلوگیری از کلیپ شدن، لیمیتر ملایم اعمال شد.")
b("bs.cap_system", "کپشن: هشدار سیستم صوتی قوی",
  "⚠️ این شدت برای سیستم‌های صوتی قوی (ماشین، باشگاه، ساب‌ووفر) تنظیم شده؛ رو اسپیکر گوشی یا هندزفری معمولی ممکنه اونقدری که باید حس نشه.")
b("bs.done", "پیام موفقیت", "✅ فایل با بیس تقویت‌شده ارسال شد.")
b("bs.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام",
  "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد.", "size", "limit")
b("bs.fail", "خطای کلی", "❌ افزایش بیس ناموفق بود. یه بار دیگه امتحان کن.")

# ── ویرایش تگ و کاور ──
h, b, n = _mk("✏️ ویرایش تگ و کاور")
b("tg.unsupported", "خطا: فرمت پشتیبانی نمیشه", (
    "😕 ویرایش تگ برای این فرمت پشتیبانی نمیشه.\n"
    "فرمت‌های قابل‌ویرایش: MP3 / M4A / FLAC / OGG / Opus\n\n"
    "با «تبدیل فرمت» یه نسخه‌ی MP3 یا M4A ازش بگیر، بعد همون فایل رو دوباره برام بفرست و تگ‌هاش رو عوض کن."
))
h("tg.head", "هدر صفحه‌ی ویرایش تگ", "✏️ ویرایش تگ و کاور")
b("tg.label.title", "کلمه‌ی «عنوان»", "عنوان")
b("tg.label.artist", "کلمه‌ی «خواننده»", "خواننده")
b("tg.label.album", "کلمه‌ی «آلبوم»", "آلبوم")
b("tg.line", "خط هر تگ", "{emoji} {field}: {current}", "emoji", "field", "current")
b("tg.line_pending", "خط هر تگ (تغییر در انتظار)", "{emoji} {field}: {current} ✏️", "emoji", "field", "current")
b("tg.clear_word", "کلمه‌ی «پاک میشه»", "پاک میشه")
b("tg.cover_line", "خط کاور", "🖼 کاور: {state}", "state")
b("tg.cover_new", "وضعیت کاور: جدید", "جدید ✏️")
b("tg.cover_drop", "وضعیت کاور: حذف میشه", "حذف میشه ✏️")
b("tg.cover_has", "وضعیت کاور: دارد", "دارد")
b("tg.cover_no", "وضعیت کاور: ندارد", "ندارد")
b("tg.hint_pending", "راهنما وقتی تغییر در انتظار است", "✏️ = تغییرِ در انتظار؛ برای اعمالش «ساخت و ارسال فایل» رو بزن.")
b("tg.hint", "راهنما وقتی تغییری نیست", "هر بخشی رو که میخوای عوض کنی بزن 👇")
n("btn.tg.title", "دکمه‌ی عنوان", "🎵 عنوان")
n("btn.tg.artist", "دکمه‌ی خواننده", "🎤 خواننده")
n("btn.tg.album", "دکمه‌ی آلبوم", "💿 آلبوم")
n("btn.tg.cover", "دکمه‌ی کاور جدید", "🖼 کاور جدید")
n("btn.tg.dropcover", "دکمه‌ی حذف کاور", "🗑 حذف کاور")
n("btn.tg.go", "دکمه‌ی ساخت و ارسال فایل", "✅ ساخت و ارسال فایل")
n("btn.tg.reset", "دکمه‌ی برگردوندن تغییرات", "↩️ برگردوندن تغییرات")
n("btn.tg.clear", "دکمه‌ی پاک کردن این تگ", "🧹 پاک کردن این تگ")
b("tg.p.cover", "صفحه‌ی فرستادن کاور جدید", (
    "🖼 عکس کاور جدید رو بفرست.\n\n"
    "💡 برای حفظ کیفیت، عکس رو به‌صورت «فایل» (Send as File) بفرست؛ "
    "اگه عکس معمولی بفرستی تلگرام فشرده‌ش میکنه."
))
b("tg.p.field", "صفحه‌ی فرستادن متن جدید یک تگ", "{emoji} {field} جدید رو بفرست (فقط متن).\n\nفعلی: {current}",
  "emoji", "field", "current")
b("tg.e.notimg", "خطا: عکس نفرستاد", "🖼 باید عکس بفرستی (بهتره به‌صورت «فایل»). یا «انصراف» رو بزن.")
b("tg.e.big", "خطا: عکس خیلی بزرگ", "😕 این عکس خیلی بزرگه (حداکثر {max} مگ).", "max")
b("tg.preparing", "پیام آماده‌سازی کاور", "⏳ دارم کاور رو آماده می‌کنم…")
b("tg.e.unread", "خطا: عکس خونده نشد", "❌ این عکس رو نتونستم بخونم؛ یه عکس دیگه (JPG یا PNG) بفرست. یا «انصراف» رو بزن.")
b("tg.e.nottext", "خطا: متن نفرستاد", "✏️ این بخش متنیه؛ متن بفرست. یا «انصراف» رو بزن.")
b("tg.e.empty", "خطا: متن خالی", "متن خالیه 🙂 چیزی بنویس، یا برای حذف تگ دکمه‌ی «پاک کردن این تگ» رو بزن.")
b("tg.e.long", "خطا: متن خیلی طولانی", "😕 خیلی طولانیه؛ حداکثر {max} کاراکتر.", "max")
b("tg.applying", "پیام در حال اعمال تگ‌ها", "⏳ دارم تگ‌ها رو اعمال می‌کنم…")
h("tg.cap_head", "هدر کپشن فایل ارسالی", "✏️ تگ‌ها ویرایش شد")
b("tg.cap_title", "کپشن: عنوان", "🎵 {title}", "title")
b("tg.cap_artist", "کپشن: خواننده", "🎤 {artist}", "artist")
b("tg.cap_album", "کپشن: آلبوم", "💿 {album}", "album")
b("tg.cap_cover_new", "کپشن: کاور عوض شد", "🖼 کاور عوض شد")
b("tg.cap_cover_drop", "کپشن: کاور حذف شد", "🖼 کاور حذف شد")
b("tg.done", "پیام موفقیت", "✅ فایل با تگ‌های جدید ارسال شد.")
b("tg.toobig", "خطا: خروجی بزرگ‌تر از سقف تلگرام", "😕 خروجی ({size} مگ) از سقف ارسال تلگرام ({limit} مگ) بیشتر شد.", "size", "limit")
b("tg.fail", "خطای کلی", "❌ اعمال تغییرات ناموفق بود. یه بار دیگه امتحان کن.")

# ── کاور و ترانه ──
h, b, n = _mk("🖼 استخراج کاور و ترانه")
b("cl.progress", "پیام در حال استخراج", "🖼 دارم کاور و متن ترانه رو از فایل بیرون میارم…")
b("cl.none", "پیام: چیزی پیدا نشد", "😕 توی این فایل نه کاور پیدا کردم نه متن ترانه.")
b("cl.done", "پیام موفقیت", "✅ کاور و ترانه ارسال شد.")
b("cl.fail", "خطای کلی", "❌ استخراج ناموفق بود. یه بار دیگه امتحان کن.")
b("cl.cap_cover", "کپشن فایل کاور", "🖼 کاور با کیفیت اصلی • {dims}{ext} • {size}", "dims", "ext", "size")
b("cl.cap_lrc", "کپشن ترانه‌ی تایم‌دار", "⏱ ترانه‌ی تایم‌دار (LRC)")
b("cl.cap_txt", "کپشن متن ترانه", "📝 متن ترانه")
b("cl.cap_txt_nosync", "کپشن متن ترانه (کنار نسخه‌ی تایم‌دار)", "📝 متن ترانه (بدون زمان)")

# ── آنالیز دقیق ──
h, b, n = _mk("🔍 آنالیز دقیق")
b("ana.progress", "پیام در حال تحلیل", "🔍 درحال اسکن و تحلیل طیف فرکانسی...")
b("ana.fail", "خطای تحلیل", "❌ تحلیل فایل ناموفق بود. یه بار دیگه امتحان کن.")
b("ana.cap_deleted", "ته کپشن گزارش", "🗑 فایل از سرور پاک شد؛ برای کار دیگه آهنگ رو دوباره بفرست.")
h("ana.v.genuine", "هدر نتیجه: سالم", "✅ نتیجه: کیفیت واقعی و سالم")
h("ana.v.suspicious", "هدر نتیجه: مشکوک", "⚠️ نتیجه: مشکوک به آپ‌اسکیل")
h("ana.v.fake", "هدر نتیجه: فیک", "🚨 نتیجه: فیک / آپ‌اسکیل‌شده")
h("ana.v.inconclusive", "هدر نتیجه: نامشخص", "❔ نتیجه: تحلیل طیفی قطعی نبود")
b("ana.conf", "عبارت اطمینان (کنار هدر نتیجه)", "(اطمینان: {level})", "level")
b("ana.c.high", "سطح اطمینان: بالا", "بالا")
b("ana.c.medium", "سطح اطمینان: متوسط", "متوسط")
b("ana.c.low", "سطح اطمینان: کم", "کم")

# ── دریافت فایل ──
h, b, n = _mk("📥 دریافت فایل و محدودیت‌ها")
b("media.only_audio", "خطا: فایل آهنگ نیست", "🎵 فقط فایل آهنگ بفرست.")
b("media.toobig", "خطا: فایل خیلی بزرگ", "😕 این فایل خیلی بزرگه. حداکثر {max} مگ میتونم دریافت کنم.", "max")
b("media.free_max", "محدودیت حجم پلن رایگان",
  "😕 توی پلن رایگان حداکثر حجم هر آهنگ {max} مگه.\n💎 برای حجم بیشتر باید پرمیوم بشی؛ به ادمین پیام بده.", "max")
b("media.free_daily", "محدودیت روزانه‌ی پلن رایگان",
  "😕 سهمیه‌ی امروزت ({limit} آهنگ در روز) تموم شده؛ فردا دوباره امتحان کن.\n💎 برای استفاده‌ی نامحدود باید پرمیوم بشی؛ به ادمین پیام بده.",
  "limit")
b("media.receiving", "پیام در حال دریافت", "⏳ در حال دریافت و بررسی فایل…")
b("media.e.tg_big", "خطا: تلگرام اجازه‌ی دانلود نمیده", "😕 فایل خیلی بزرگه و تلگرام اجازه دانلودش رو نمیده.")
b("media.e.fail", "خطا: دریافت ناموفق", "❌ دریافت فایل ناموفق بود.")
b("media.e.local", "خطا: دسترسی به سرور لوکال", "❌ ربات به فایلِ دانلودشده توی سرور لوکال دسترسی نداره (تنظیم Volume).")
b("media.e.unreadable", "خطا: فایل خونده نشد", "❌ این فایل رو نتونستم بخونم. مطمئنی فایل صوتیِ سالمه؟")

# ── پلن و پرمیوم ──
h, b, n = _mk("💎 پلن (/plan) و پیام‌های پرمیوم")
b("plan.admin", "پلن ادمین", "👑 تو ادمین هستی؛ بدون هیچ محدودیتی.")
b("plan.premium", "پلن پرمیوم", "💎 پلن شما: پرمیوم ({left})\nبدون محدودیت.", "left")
h("plan.free_head", "هدر پلن رایگان", "🆓 پلن شما: رایگان")
b("plan.free_daily", "خط سهمیه‌ی روزانه", "📅 امروز: {used} از {limit} آهنگ", "used", "limit")
b("plan.free_max", "خط حداکثر حجم", "📦 حداکثر حجم هر آهنگ: {max} مگ", "max")
b("plan.free_tail", "خط آخر پلن رایگان", "💎 برای برداشتن محدودیت‌ها به ادمین پیام بده.")
b("pm.revoked", "پیام لغو پرمیوم به کاربر", "ℹ️ پلن پرمیوم شما لغو شد.")
b("pm.granted", "پیام اعطای پرمیوم به کاربر",
  "🎉 پلن شما پرمیوم شد ({dur})!\nحالا بدون محدودیت میتونی استفاده کنی. /plan", "dur")

# ── هشدارهای پاپ‌آپ ──
h, b, n = _mk("⚠️ هشدارهای پاپ‌آپ")
b("alert.expired", "درخواست منقضی شده", "این درخواست منقضی شده؛ آهنگ رو دوباره بفرست.")
b("alert.not_yours", "دکمه مال کاربر دیگه‌ست", "این مال تو نیست 🙂")
b("alert.busy", "کار قبلی در حال انجامه", "هنوز دارم کار قبلی رو انجام میدم ⏳")
b("alert.disabled", "قابلیت غیرفعاله", "این قابلیت فعال نیست.")
b("alert.nochange", "تغییری ندادی", "هنوز چیزی رو تغییر ندادی 🙂")
del h, b, n


# ── ابزارهای پنل ادمین برای ویرایش متن‌ها ──


def tx_custom(key: str) -> Optional[str]:
    v = DB.get("texts", {}).get(key)
    return v if isinstance(v, str) and v.strip() else None


def tx_group_keys(gi: int) -> List[str]:
    g = TEXT_GROUPS[gi]
    return [k for k, d in TEXTS.items() if d.group == g]


def tx_vars_doc(d: TextDef) -> List[str]:
    return [f"{{{v}}} = {PH_DOC.get(v, '')}" for v in d.vars]


def tx_main_text() -> str:
    total = len(TEXTS)
    done = sum(1 for k in TEXTS if tx_custom(k))
    return (
        "✏️ ویرایش متن‌ها و دکمه‌ها\n\n"
        f"🏷 هدر  |  📝 بدنه  |  🔘 دکمه\n"
        f"کل: {fa(total)} مورد  •  ویرایش‌شده: {fa(done)}\n\n"
        "یه بخش رو انتخاب کن:"
    )


def tx_main_markup() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for gi, g in enumerate(TEXT_GROUPS):
        n_done = sum(1 for k in tx_group_keys(gi) if tx_custom(k))
        label = g + (f" ({fa(n_done)}✏️)" if n_done else "")
        row.append(InlineKeyboardButton(label, callback_data=f"ad:txg:{gi}:0"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↕️ ترتیب دکمه‌های منوی آهنگ", callback_data="ad:mo")])
    rows.append([InlineKeyboardButton("♻️ بازنشانی همه به پیش‌فرض", callback_data="ad:txra")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ad:home")])
    return InlineKeyboardMarkup(rows)


def tx_group_view(gi: int, page: int) -> Tuple[str, InlineKeyboardMarkup]:
    keys = tx_group_keys(gi)
    pages = max(1, math.ceil(len(keys) / TX_PAGE))
    page = min(max(page, 0), pages - 1)
    text = (
        f"{TEXT_GROUPS[gi]}\n"
        f"🏷 هدر  |  📝 بدنه  |  🔘 دکمه   (✏️ = ویرایش‌شده)\n"
        f"صفحه‌ی {fa(page + 1)} از {fa(pages)}\n\n"
        "کدوم مورد رو میخوای ویرایش کنی؟"
    )
    rows = []
    for k in keys[page * TX_PAGE:(page + 1) * TX_PAGE]:
        d = TEXTS[k]
        mark = " ✏️" if tx_custom(k) else ""
        rows.append([InlineKeyboardButton(f"{KIND_ICON[d.kind]} {d.label}{mark}", callback_data=f"ad:txi:{TEXT_SID[k]}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"ad:txg:{gi}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"ad:txg:{gi}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ad:tx")])
    return text, InlineKeyboardMarkup(rows)


def tx_item_text(key: str) -> str:
    d = TEXTS[key]
    custom = tx_custom(key)
    cur = custom if custom is not None else d.default
    lines = [
        f"{KIND_ICON[d.kind]} {d.label}",
        f"نوع: {KIND_NAME[d.kind]}  |  وضعیت: {'✏️ ویرایش‌شده' if custom is not None else 'پیش‌فرض'}",
        "",
        "📌 متن فعلی:",
        cur,
    ]
    if custom is not None:
        lines += ["", "↩️ متن پیش‌فرض:", d.default]
    if d.vars:
        lines += ["", "🔣 متغیرها (با همین شکل «{نام}» توی متن بنویس):", *tx_vars_doc(d), "",
                  "👁 نمونه با مقدارهای فرضی:", _fill(cur, PH_SAMPLE)]
    return "\n".join(lines)[:4000]


def tx_item_markup(key: str) -> InlineKeyboardMarkup:
    d = TEXTS[key]
    gi = TEXT_GROUPS.index(d.group)
    page = tx_group_keys(gi).index(key) // TX_PAGE
    sid = TEXT_SID[key]
    rows = [[InlineKeyboardButton("✏️ ویرایش", callback_data=f"ad:txe:{sid}")]]
    if tx_custom(key) is not None:
        rows.append([InlineKeyboardButton("♻️ بازگشت به پیش‌فرض", callback_data=f"ad:txr:{sid}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"ad:txg:{gi}:{page}")])
    return InlineKeyboardMarkup(rows)


def tx_edit_prompt(key: str) -> str:
    d = TEXTS[key]
    cur = tx_custom(key) or d.default
    lines = [f"✏️ {d.label}", "", "متن فعلی:", cur, "", "👇 متن جدید رو بفرست."]
    if d.vars:
        lines += ["", "🔣 متغیرهای مجاز:", *tx_vars_doc(d)]
    limit = TX_MAX_BTN if d.kind == "btn" else TX_MAX_TEXT
    lines += ["", f"ℹ️ فقط متن ساده (بدون بولد/ایتالیک)، حداکثر {fa(limit)} کاراکتر."]
    if d.kind == "btn":
        lines.append("ℹ️ اسم دکمه باید تک‌خطی باشه.")
    return "\n".join(lines)[:4000]


def tx_validate(key: str, text: str) -> Optional[str]:
    """None = معتبر | در غیر این صورت پیام خطا."""
    d = TEXTS[key]
    if not text.strip():
        return "😕 متن خالیه."
    if d.kind == "btn":
        if "\n" in text:
            return "😕 اسم دکمه باید تک‌خطی باشه."
        if len(text) > TX_MAX_BTN:
            return f"😕 اسم دکمه خیلی طولانیه (حداکثر {fa(TX_MAX_BTN)} کاراکتر، الان {fa(len(text))})."
    elif len(text) > TX_MAX_TEXT:
        return f"😕 متن خیلی طولانیه (حداکثر {fa(TX_MAX_TEXT)} کاراکتر، الان {fa(len(text))})."
    bad = sorted(set(_PH.findall(text)) - set(d.vars))
    if bad:
        ok = "، ".join("{" + v + "}" for v in d.vars) or "هیچ متغیری"
        return f"😕 متغیر نامعتبر: {'، '.join('{' + v + '}' for v in bad)}\nمتغیرهای مجاز این مورد: {ok}"
    return None


# ───────────────────────── تحلیل تخصصی صوت (audio_analyzer) ─────────────────────────
# توابع قابل استفاده:
#   analyze_audio(path)        -> dict  (همگام)
#   analyze_audio_async(path)  -> dict  (داخل asyncio؛ event loop بلاک نمی‌شود)
#   format_report(result)      -> str   (پیام فارسی آماده ارسال)
# نیازمندی‌ها: mutagen, numpy

# ───────────────────────── ثابت‌ها ─────────────────────────

PROBE_TIMEOUT = 30            # ثانیه
DECODE_TIMEOUT = 60           # ثانیه برای هر قطعه
MAX_ANALYSIS_SR = 96_000      # بالاتر از این به 96k تبدیل می‌شود
SPEC_COLS = 640               # تعداد ستون‌های زمانیِ اسپکتروگرام
SPEC_ROWS = 256               # تعداد ردیف‌های فرکانسی اسپکتروگرام
SPEC_TIMEOUT = 150            # ثانیه برای دیکود کل آهنگ (اسپکتروگرام)
SPEC_MAX_DURATION = 2400      # آهنگ‌های بلندتر از 40 دقیقه اسپکتروگرام نمی‌گیرن
SG_FLOOR_DB = -120.0          # کف مقیاس رنگ (dBFS)؛ سقف = 0
MAX_NATIVE_SR = 400_000       # DSD و مشابه: تحلیل طیفی انجام نمی‌شود
SILENCE_RMS = 10 ** (-65 / 20)  # فریم‌های ساکت‌تر از -65dBFS نادیده گرفته می‌شوند
MIN_FRAMES = 20               # حداقل فریم معتبر برای اعتماد به طیف

SEARCH_MIN_HZ = 6_000.0       # جست‌وجوی «پرتگاه» طیف از این فرکانس به بالا
CLIFF_WINDOW_HZ = 400.0       # پهنای پنجره‌ی مقایسه‌ی بالا/پایین پرتگاه
CLIFF_SMOOTH_HZ = 150.0       # هموارسازی طیف
CLIFF_MIN_DB = 15.0           # حداقل افت برای قبول «قطع فرکانسی»
CLIFF_STRONG_DB = 30.0        # افت قوی → اطمینان بالا
CLIFF_TAIL_MARGIN_DB = 10.0   # بالای پرتگاه نباید دوباره انرژی برگردد

LOSSLESS_EXT = {".flac", ".wav", ".ape", ".aiff", ".aif", ".aifc", ".wv", ".alac", ".tta"}
LOSSY_CODECS = {"mp3", "mp2", "aac", "vorbis", "opus", "wmav1", "wmav2", "wmapro", "ac3", "eac3", "dts"}
LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "tta", "wmalossless", "truehd", "mlp"}

# نام اول format_name در ffprobe → (نام نمایشی، پسوندهای معتبر)
_CONTAINERS: Dict[str, Tuple[str, set]] = {
    "mp3": ("MP3", {".mp3", ".mp2"}),
    "flac": ("FLAC", {".flac"}),
    "wav": ("WAV", {".wav", ".wave"}),
    "w64": ("Wave64", {".w64"}),
    "aiff": ("AIFF", {".aiff", ".aif", ".aifc"}),
    "ogg": ("Ogg", {".ogg", ".oga", ".opus", ".spx"}),
    "mov": ("MP4/M4A", {".m4a", ".mp4", ".m4b", ".m4r", ".3gp", ".3g2", ".mov", ".alac"}),
    "matroska": ("Matroska/WebM", {".mka", ".mkv", ".webm"}),
    "asf": ("ASF/WMA", {".wma", ".asf", ".wmv"}),
    "ape": ("Monkey's Audio", {".ape"}),
    "aac": ("AAC (ADTS)", {".aac"}),
    "wv": ("WavPack", {".wv"}),
    "tta": ("TrueAudio", {".tta"}),
    "dsf": ("DSF", {".dsf"}),
    "caf": ("CAF", {".caf"}),
    "ac3": ("AC-3", {".ac3"}),
}

_CODEC_LABELS = {
    "mp3": "MP3 (MPEG Layer III)", "mp2": "MP2 (MPEG Layer II)", "alac": "ALAC (Apple Lossless)",
    "flac": "FLAC", "vorbis": "Vorbis", "opus": "Opus", "wmav1": "WMA v1", "wmav2": "WMA v2",
    "wmapro": "WMA Pro", "wmalossless": "WMA Lossless", "ape": "Monkey's Audio (APE)",
    "wavpack": "WavPack", "tta": "TTA", "ac3": "AC-3 (Dolby Digital)", "eac3": "E-AC-3",
    "dts": "DTS", "amr_nb": "AMR-NB", "amr_wb": "AMR-WB", "truehd": "Dolby TrueHD", "mlp": "MLP",
}

# (حداکثر cutoff به Hz, کد کلاس, بیت‌ریت تقریبی)
_TIERS = (
    (12_000.0, "<=64", 64),
    (15_500.0, "96", 96),
    (17_600.0, "128", 128),
    (19_300.0, "192", 192),
    (21_000.0, "256-320", 320),
)


# ───────────────────────── ابزارهای کوچک ─────────────────────────


def _fa(x: Any) -> str:
    return str(x).translate(_FA)


def _num(v: Any, cast=float) -> Optional[Any]:
    try:
        r = cast(v)
        return r if r == r else None  # NaN → None
    except (TypeError, ValueError):
        return None


def _clip(s: Optional[str], n: int = 80) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _pick_encoder(md: Dict[str, Any], t: Dict[str, Any]) -> Optional[str]:
    """اسم انکودر؛ فیلد هدر LAME فقط 9 کاراکتره (مثلاً «Lavc61.3.» از «Lavc61.3.100» بریده میشه)،
    پس اگه تگ کامل‌تری هست اون رو ترجیح میدیم."""
    cands = [str(x).strip() for x in (md.get("encoder_info"), t.get("encoder_tag"), md.get("encoder_tag"))
             if x and str(x).strip()]
    if not cands:
        return None
    first = cands[0]
    if len(first) <= 9:
        for c in cands[1:]:
            if len(c) > len(first):
                return c
    return first


def _khz(hz: float) -> str:
    return _fa(f"{hz / 1000:.1f}")


def _dur(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return _fa(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")


def _mb(nbytes: float) -> str:
    return _fa(f"{nbytes / (1024 * 1024):.1f}")


@functools.lru_cache(maxsize=1)
def _resolve_binaries() -> Tuple[str, str]:
    ffmpeg = os.getenv("FFMPEG_PATH", "ffmpeg")
    ffprobe = os.getenv("FFPROBE_PATH", "ffprobe")
    if not (shutil.which(ffmpeg) and shutil.which(ffprobe)):
        try:  # روی هاست‌های بدون apt
            import static_ffmpeg

            static_ffmpeg.add_paths()
        except Exception as e:  # noqa: BLE001
            log.warning("static-ffmpeg load failed: %s", e)
    f, p = shutil.which(ffmpeg), shutil.which(ffprobe)
    if not f or not p:
        raise FileNotFoundError("ffmpeg/ffprobe not found")
    return f, p


def _run(cmd: List[str], timeout: float) -> Tuple[int, bytes, bytes]:
    # در صورت timeout، subprocess.run خودش پروسه را kill می‌کند (بدون پروسه‌ی زامبی)
    proc = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ───────────────────────── موازی‌سازی و کش تحلیل ─────────────────────────
# هر مرحله‌ی سنگین تحلیل (طیف، عمق بیت، لودنس، اسپکتروگرام) مستقل از بقیه‌ست؛ پس هم‌زمان اجرا میشن و
# زمان کل = کندترین مرحله (نه جمع همه). decode قطعه‌ها هم موازیه. نتیجه‌ی هر مرحله کش میشه تا
# «بررسی سریع موقع آپلود»، «آنالیز دقیق» و «نرمال‌سازی» هیچ کاری رو دوباره از صفر انجام ندن.
# نتایج دقیقاً همون محاسبه‌ی قبلیه (همون تابع‌ها، همون پارامترها)؛ فقط ترتیب اجرا عوض شده.
_CPU = os.cpu_count() or 2
# استخر مرحله‌ها (فقط منتظر استخر decode میمونن) و استخر decode (کارهای کوتاه و بدون انتظار) جدا هستن تا deadlock نشه
_TOP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ana")
_DEC_POOL = ThreadPoolExecutor(max_workers=max(3, min(8, _CPU + 2)), thread_name_prefix="dec")

_MEMO: "OrderedDict[tuple, Future]" = OrderedDict()
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 48


def _memo(path: Path, key: tuple, fn):
    """fn() -> (value, errs). همزمان فقط یک نفر محاسبه میکنه و بقیه منتظر همون نتیجه میمونن (single-flight).
    فقط نتیجه‌ی «تمیز» (بدون خطا/timeout) کش میشه تا یه خطای گذرا ماندگار نشه. همیشه کپی برمی‌گردونه."""
    try:
        st = path.stat()
        k = ((str(path), st.st_size, st.st_mtime_ns), key)
    except OSError:
        return fn()
    with _MEMO_LOCK:
        fut = _MEMO.get(k)
        owner = fut is None
        if owner:
            fut = Future()
            _MEMO[k] = fut
            while len(_MEMO) > _MEMO_MAX:
                _MEMO.popitem(last=False)
        else:
            _MEMO.move_to_end(k)
    if not owner:
        return copy.deepcopy(fut.result())
    try:
        val = fn()
    except BaseException as e:
        with _MEMO_LOCK:
            if _MEMO.get(k) is fut:
                del _MEMO[k]
        fut.set_exception(e)
        raise
    if val[1]:  # خطا/هشدار داشت؛ کش نکن
        with _MEMO_LOCK:
            if _MEMO.get(k) is fut:
                del _MEMO[k]
    fut.set_result(val)
    return copy.deepcopy(val)


def forget_analysis(directory: Path) -> None:
    """کش تحلیل فایل‌های یه پوشه (وقتی job پاک میشه)"""
    prefix = str(directory)
    with _MEMO_LOCK:
        for k in [k for k in _MEMO if k[0][0].startswith(prefix)]:
            del _MEMO[k]


# ───────────────────────── 1) متادیتا (mutagen) ─────────────────────────

_TAG_KEYS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "id3": {
        "title": ("TIT2",), "artist": ("TPE1",), "album": ("TALB",), "album_artist": ("TPE2",),
        "date": ("TDRC", "TYER", "TDRL", "TORY"), "genre": ("TCON",), "track": ("TRCK",),
        "disc": ("TPOS",), "composer": ("TCOM",), "bpm": ("TBPM",), "copyright": ("TCOP",),
        "publisher": ("TPUB",), "encoder_tag": ("TSSE", "TENC"), "isrc": ("TSRC",),
    },
    "mp4": {
        "title": ("\xa9nam",), "artist": ("\xa9ART",), "album": ("\xa9alb",), "album_artist": ("aART",),
        "date": ("\xa9day",), "genre": ("\xa9gen",), "track": ("trkn",), "disc": ("disk",),
        "composer": ("\xa9wrt",), "comment": ("\xa9cmt",), "lyrics": ("\xa9lyr",), "bpm": ("tmpo",),
        "copyright": ("cprt",), "encoder_tag": ("\xa9too",),
    },
    "vorbis": {  # Vorbis comment (FLAC/Ogg/Opus) و APEv2 (کلیدها case-insensitive)
        "title": ("title",), "artist": ("artist",), "album": ("album",),
        "album_artist": ("albumartist", "album artist"), "date": ("date", "year"),
        "genre": ("genre",), "track": ("tracknumber", "track"), "disc": ("discnumber", "disc"),
        "composer": ("composer",), "comment": ("comment", "description"),
        "lyrics": ("lyrics", "unsyncedlyrics"), "bpm": ("bpm",), "copyright": ("copyright",),
        "publisher": ("label", "organization", "publisher"), "encoder_tag": ("encoder", "encoded-by"),
        "isrc": ("isrc",), "track_total": ("tracktotal", "totaltracks"), "disc_total": ("disctotal", "totaldiscs"),
    },
    "asf": {
        "title": ("Title",), "artist": ("Author", "WM/Author"), "album": ("WM/AlbumTitle",),
        "album_artist": ("WM/AlbumArtist",), "date": ("WM/Year",), "genre": ("WM/Genre",),
        "track": ("WM/TrackNumber",), "disc": ("WM/PartOfSet",), "composer": ("WM/Composer",),
        "comment": ("Description",), "lyrics": ("WM/Lyrics",), "bpm": ("WM/BeatsPerMinute",),
        "copyright": ("Copyright",), "publisher": ("WM/Publisher",), "encoder_tag": ("WM/EncodingSettings",),
        "isrc": ("WM/ISRC",),
    },
}


def _empty_metadata() -> Dict[str, Any]:
    return {
        "title": None, "artist": None, "album": None, "album_artist": None,
        "year": None, "date": None, "genre": None,
        "track_number": None, "track_total": None, "disc_number": None, "disc_total": None,
        "composer": None, "comment": None, "has_lyrics": None, "bpm": None,
        "copyright": None, "publisher": None, "isrc": None,
        "encoder_tag": None, "encoder_info": None, "bitrate_mode": None, "tag_format": None,
        "cover": {"present": None, "mime": None, "width": None, "height": None,
                  "size_bytes": None, "saved_to": None},
    }


def _to_str(v: Any) -> Optional[str]:
    """مقدار تگ (لیست / فریم ID3 / attr ASF / bytes / عدد) → رشته‌ی تمیز یا None"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        for item in v:
            s = _to_str(item)
            if s:
                return s
        return None
    if hasattr(v, "text") and not isinstance(v, (str, bytes)):
        return _to_str(v.text)
    val = getattr(v, "value", v)
    s = bytes(val).decode("utf-8", "replace") if isinstance(val, (bytes, bytearray)) else str(val)
    s = s.replace("\x00", " ").strip()
    return s or None


def _get_text(tags: Any, kind: str, field: str) -> Optional[str]:
    for key in _TAG_KEYS[kind].get(field, ()):
        try:
            s = _to_str(tags.get(key))
        except Exception:  # noqa: BLE001
            continue
        if s:
            return s
    return None


def _parse_pair(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None
    m = re.match(r"^\s*(\d+)\s*(?:/|of|-)\s*(\d+)\s*$", text)
    if m:
        return int(m.group(1)), int(m.group(2)) or None
    m = re.search(r"\d+", text)
    return (int(m.group()) if m else None), None


def _tag_kind(tags: Any) -> str:
    if isinstance(tags, ID3):
        return "id3"
    if isinstance(tags, MP4Tags):
        return "mp4"
    if isinstance(tags, ASFTags):
        return "asf"
    return "vorbis"


def _sniff_mime(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def _image_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    """ابعاد PNG/JPEG بدون نیاز به Pillow"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w, h
        if data[:3] == b"\xff\xd8\xff":
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _parse_wm_picture(raw: bytes) -> Optional[Tuple[bytes, str]]:
    """ساختار WM/Picture در ASF: type(1) size(4) mime(utf16\\0) desc(utf16\\0) data"""
    try:
        size = struct.unpack_from("<I", raw, 1)[0]
        i = 5
        for _ in range(2):  # mime سپس description
            j = i
            while raw[j:j + 2] != b"\x00\x00":
                j += 2
            if _ == 0:
                mime = raw[i:j].decode("utf-16-le", "replace")
            i = j + 2
        return raw[i:i + size], mime
    except Exception:  # noqa: BLE001
        return None


def _find_cover(audio: Any, tags: Any) -> Optional[Tuple[bytes, Optional[str], Optional[int], Optional[int]]]:
    """(data, mime, width, height) یا None"""
    pics = getattr(audio, "pictures", None)  # FLAC
    if pics:
        p = next((x for x in pics if x.type == 3), pics[0])
        return p.data, p.mime, p.width or None, p.height or None
    if tags is None:
        return None
    if isinstance(tags, ID3):
        frames = tags.getall("APIC")
        if frames:
            f = next((x for x in frames if x.type == 3), frames[0])
            return f.data, f.mime, None, None
        return None
    if isinstance(tags, MP4Tags):
        covers = tags.get("covr")
        if covers:
            c = covers[0]
            mime = "image/png" if getattr(c, "imageformat", None) == 14 else "image/jpeg"
            return bytes(c), mime, None, None
        return None
    if isinstance(tags, ASFTags):
        for attr in tags.get("WM/Picture") or []:
            parsed = _parse_wm_picture(getattr(attr, "value", b""))
            if parsed and parsed[0]:
                return parsed[0], parsed[1], None, None
        return None
    # Ogg Vorbis / Opus: تصویر داخل metadata_block_picture (base64)
    try:
        for b64 in tags.get("metadata_block_picture") or []:
            try:
                p = Picture(base64.b64decode(b64))
                return p.data, p.mime, p.width or None, p.height or None
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    if type(tags).__name__ == "APEv2":
        raw = getattr(tags.get("Cover Art (Front)"), "value", None)
        if isinstance(raw, (bytes, bytearray)) and b"\x00" in raw:
            return bytes(raw.split(b"\x00", 1)[1]), None, None, None
    return None


def _fill_metadata(md: Dict[str, Any], path: Path, cover_output_path: Optional[Path]) -> None:
    if mutagen is None:
        raise RuntimeError("mutagen is not installed (pip install mutagen)")
    audio = mutagen.File(str(path))
    if audio is None:
        raise ValueError("file format not recognized by mutagen")

    info = getattr(audio, "info", None)
    md["encoder_info"] = _to_str(getattr(info, "encoder_info", None))
    bm = getattr(info, "bitrate_mode", None)
    if bm is not None:
        name = str(getattr(bm, "name", bm)).split(".")[-1].strip().upper()  # "BitrateMode.CBR" -> "CBR"
        name = {"1": "CBR", "2": "VBR", "3": "ABR"}.get(name, name)
        md["bitrate_mode"] = name if name in ("CBR", "VBR", "ABR") else None

    tags = audio.tags
    if tags is not None:
        kind = _tag_kind(tags)
        if kind == "id3":
            ver = getattr(tags, "version", (2, 4, 0))
            md["tag_format"] = f"ID3v2.{ver[1]}" if len(ver) > 1 else "ID3v2"
        elif kind == "mp4":
            md["tag_format"] = "MP4 (iTunes)"
        elif kind == "asf":
            md["tag_format"] = "ASF"
        else:
            md["tag_format"] = "APEv2" if type(tags).__name__ == "APEv2" else "Vorbis Comment"

        for f in ("title", "artist", "album", "album_artist", "genre", "composer", "copyright",
                  "publisher", "isrc", "encoder_tag", "comment"):
            md[f] = _get_text(tags, kind, f)

        # تاریخ / سال
        md["date"] = _get_text(tags, kind, "date")
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", md["date"] or "")
        md["year"] = int(m.group(1)) if m else None

        # ترک و دیسک
        if kind == "mp4":
            for fld, key in (("track", "trkn"), ("disc", "disk")):
                raw = tags.get(key)
                if raw and isinstance(raw[0], tuple) and raw[0]:
                    n = raw[0][0] or None
                    t = raw[0][1] if len(raw[0]) > 1 and raw[0][1] else None
                    md[f"{fld}_number"], md[f"{fld}_total"] = n, t
        else:
            md["track_number"], md["track_total"] = _parse_pair(_get_text(tags, kind, "track"))
            md["disc_number"], md["disc_total"] = _parse_pair(_get_text(tags, kind, "disc"))
            if kind == "vorbis":
                for fld in ("track_total", "disc_total"):
                    if md[fld] is None:
                        md[fld] = _parse_pair(_get_text(tags, kind, fld))[0]

        # BPM
        bpm = _num(_get_text(tags, kind, "bpm"))
        md["bpm"] = int(round(bpm)) if bpm else None

        # کامنت و متن ترانه
        if kind == "id3":
            md["comment"] = next((s for s in (_to_str(f) for f in tags.getall("COMM")) if s), None)
            md["has_lyrics"] = any(_to_str(f) for f in tags.getall("USLT"))
        else:
            md["has_lyrics"] = bool(_get_text(tags, kind, "lyrics"))
        md["comment"] = _clip(md["comment"], 300) or None

    # کاور
    found = _find_cover(audio, tags)
    cover = md["cover"]
    if not found or not found[0]:
        cover["present"] = False
        return
    data, mime, w, h = found
    mime = mime if (mime or "").startswith("image/") else _sniff_mime(data)
    if not (w and h):
        w, h = _image_size(data)
    cover.update(present=True, mime=mime, width=w, height=h, size_bytes=len(data))
    if cover_output_path:
        out = Path(cover_output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        cover["saved_to"] = str(out)


# ───────────────────────── 2) مشخصات فنی (ffprobe) ─────────────────────────


def _probe(path: Path, ffprobe: str) -> Dict[str, Any]:
    rc, out, err = _run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        PROBE_TIMEOUT,
    )
    if rc != 0:
        raise ValueError("ffprobe failed: " + err.decode("utf-8", "ignore")[-300:].strip())
    data = json.loads(out or b"{}")
    fmt = data.get("format") or {}
    audio = next((s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("no audio stream found")

    codec = (audio.get("codec_name") or "").lower()
    ext = path.suffix.lower()
    tags = {k.lower(): v for k, v in {**(fmt.get("tags") or {}), **(audio.get("tags") or {})}.items()}

    # کانتینر واقعی (ffprobe از روی محتوا تشخیص می‌دهد نه پسوند)
    fname = fmt.get("format_name") or ""
    first = fname.split(",")[0]
    label, exts = _CONTAINERS.get(first, (fname or None, None))
    if first == "mov":
        brand = (tags.get("major_brand") or "").strip().lower()
        label = "M4A (MP4)" if brand == "m4a" else "QuickTime MOV" if brand == "qt" else "MP4"
    ext_mismatch = (ext not in exts) if (exts is not None and ext) else None

    # کدک / انکودر
    profile = (audio.get("profile") or "").strip()
    pcm = re.match(r"pcm_([suf])(\d+)", codec)
    if codec == "aac":
        codec_label = profile if profile.upper().startswith("HE") else (f"AAC-{profile}" if profile else "AAC")
    elif pcm:
        codec_label = f"PCM {pcm.group(2)}-bit" + (" float" if pcm.group(1) == "f" else "")
    else:
        codec_label = _CODEC_LABELS.get(codec) or audio.get("codec_long_name") or codec or None
    is_lossless = codec in LOSSLESS_CODECS or bool(pcm) or codec.startswith("dsd")

    # بیت‌دپث (برای کدک‌های lossy معنی ندارد → None)
    bit_depth = None
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        v = _num(audio.get(key), int)
        if v and v > 0:
            bit_depth = v
            break
    if bit_depth is None and is_lossless:  # بعضی لاسلس‌ها (APE/WavPack/...) فقط sample_fmt دارن
        if pcm:
            bit_depth = int(pcm.group(2))
        else:  # s32 عمداً نیست: می‌تونه 24 بیتِ جاسازی‌شده توی 32 باشه
            bit_depth = {"u8": 8, "u8p": 8, "s16": 16, "s16p": 16}.get(audio.get("sample_fmt") or "")

    duration = next((d for d in (_num(fmt.get("duration")), _num(audio.get("duration"))) if d and d > 0), 0.0)
    size = _num(fmt.get("size"), int) or path.stat().st_size

    bitrate, br_src = None, None
    for src, v in (("stream", audio.get("bit_rate")), ("format", fmt.get("bit_rate"))):
        b = _num(v, int)
        if b and b > 0:
            bitrate, br_src = b, src
            break
    if bitrate is None and duration > 0:
        bitrate, br_src = int(size * 8 / duration), "computed"

    channels = _num(audio.get("channels"), int)
    return {
        "container": label,
        "container_raw": fname or None,
        "container_long_name": fmt.get("format_long_name"),
        "extension": ext or None,
        "extension_mismatch": ext_mismatch,
        "codec": codec or None,
        "codec_label": codec_label,
        "codec_profile": profile or None,
        "is_lossless": is_lossless,
        "sample_rate": _num(audio.get("sample_rate"), int),
        "bit_depth": bit_depth,
        "sample_fmt": audio.get("sample_fmt"),
        "channels": channels,
        "channel_layout": audio.get("channel_layout"),
        "bitrate_kbps": round(bitrate / 1000) if bitrate else None,
        "bitrate_source": br_src,
        "bitrate_mode": None,  # بعداً از mutagen (MP3) پر می‌شود
        "duration": duration,
        "size_bytes": size,
        "encoder_tag": tags.get("encoder"),
        "has_attached_picture": any(
            s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") == 1
            for s in data.get("streams") or []
        ),
    }


# ───────────────────────── 3) تحلیل طیف فرکانسی (FFT) ─────────────────────────

BAND_HZ = 250.0              # پهنای باندهای درشت (برای حفره‌های طیفی)
SHARP_WIDTH_HZ = 1_000.0     # گذار باریک‌تر از این = فیلتر lowpass دیجیتالِ انکودر
GENTLE_WIDTH_HZ = 2_500.0    # گذار پهن‌تر از این = افت طبیعی (نوار / وینیل / مستر قدیمی)
DEEP_FLOOR_DB = -70.0        # کف طیف بالای قطع (نسبت به سطح 1–4kHz) پایین‌تر از این = حذف دیجیتال
HOLE_DROP_DB = 20.0          # باندی که 20dB از هر دو همسایه‌اش پایین‌تر باشد = «حفره‌ی طیفی»
HOLE_MIN_RATIO = 0.04        # حداقل نسبت حفره‌ها برای نشانه‌ی انکود lossy
SEG_AGREE_HZ = 700.0         # قطع قطعه‌ها تا این فاصله از قطع کلی «هم‌نظر» حساب می‌شود
CLIP_LEVEL = 0.999           # نمونه‌ی بالاتر از این = سقف دیجیتال
CLIP_RATIO_WARN = 1e-4       # نسبت نمونه‌های کلیپ‌شده (سه‌تایی پشت‌سرهم) برای هشدار
BLOCK_SEC = 3.0              # طول بلوک برای تخمین دینامیک (DR)
EBU_TIMEOUT = 180            # ثانیه برای اندازه‌گیری لودنس کل فایل
BITDEPTH_SEGMENTS = 4
FLOAT_FMTS = {"flt", "fltp", "dbl", "dblp"}


def _plan_segments(duration: float, max_segments: int, seg_len: float) -> List[Tuple[float, float]]:
    """آهنگ کوتاه: کل آهنگ در قطعه‌های پشت‌سرهم؛ آهنگ بلند: نمونه‌برداری یکنواخت"""
    max_segments = max(1, int(max_segments))
    if duration <= 0:
        return [(0.0, seg_len)]
    if duration <= max_segments * seg_len:
        n = int(math.ceil(duration / seg_len))
        return [(i * seg_len, min(seg_len, duration - i * seg_len)) for i in range(n)]
    first, last = duration * 0.05, max(0.0, duration * 0.95 - seg_len)
    step = (last - first) / (max_segments - 1) if max_segments > 1 else 0.0
    return [(first + i * step, seg_len) for i in range(max_segments)]


def _decode_pcm(ffmpeg: str, path: Path, start: float, length: float, sr: int, ch: int) -> Optional[np.ndarray]:
    """یک قطعه را float32 از pipe می‌خواند؛ خروجی شکل (نمونه، کانال)"""
    cmd = [
        ffmpeg, "-nostdin", "-v", "error", "-hide_banner",
        "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(ch), "-ar", str(sr),
        "-f", "f32le", "pipe:1",
    ]
    rc, out, err = _run(cmd, DECODE_TIMEOUT)
    n = (len(out) // 4 // ch) * ch
    if n == 0:
        if rc != 0:
            raise RuntimeError(err.decode("utf-8", "ignore")[-200:].strip() or f"ffmpeg rc={rc}")
        return None
    x = np.nan_to_num(np.frombuffer(out, dtype="<f4", count=n), nan=0.0, posinf=0.0, neginf=0.0)
    return x.reshape(-1, ch)


def _accumulate(
    x: np.ndarray, nfft: int, hop: int, window: np.ndarray, acc: np.ndarray,
    edges: np.ndarray, counts: np.ndarray, rows: List[np.ndarray],
) -> int:
    """
    طیف توانی فریم‌های غیرساکت را به acc اضافه می‌کند (دسته‌ای، با حافظه‌ی ثابت) و
    سطح هر باند 250Hz (به dB) را برای هر فریم در rows نگه می‌دارد.
    """
    if len(x) < nfft:
        return 0
    view = np.lib.stride_tricks.sliding_window_view(x, nfft)[::hop]
    kept = 0
    for i in range(0, view.shape[0], 128):
        chunk = view[i:i + 128]
        rms = np.sqrt(np.mean(np.square(chunk, dtype=np.float64), axis=1))
        chunk = chunk[rms > SILENCE_RMS]
        if chunk.shape[0] == 0:
            continue
        spec = np.fft.rfft(chunk * window, axis=1)
        pwr = spec.real ** 2 + spec.imag ** 2
        acc += pwr.sum(axis=0)
        rows.append(10.0 * np.log10(np.add.reduceat(pwr, edges, axis=1) / counts + 1e-30))
        kept += chunk.shape[0]
    return kept


def _smooth(db: np.ndarray, df: float) -> np.ndarray:
    k = max(3, int(round(CLIFF_SMOOTH_HZ / df))) | 1  # فرد
    padded = np.pad(db, k // 2, mode="edge")
    return np.convolve(padded, np.ones(k) / k, mode="valid")


def _band_mean(s: np.ndarray, df: float, lo: float, hi: float) -> Optional[float]:
    a, b = int(lo / df), min(len(s), int(hi / df) + 1)
    return float(np.mean(s[a:b])) if b > a else None


def _find_cliff(s: np.ndarray, df: float) -> Tuple[Optional[int], float]:
    """
    بزرگ‌ترین «پرتگاه» طیف بالای 6kHz را پیدا می‌کند:
    اختلاف میانگین سطح (dB) در 400Hz پایین‌تر و 400Hz بالاتر از هر نقطه.
    خروجی: (اندیس بین، مقدار افت dB) یا (None, افت)
    """
    n = len(s)
    w = max(4, int(round(CLIFF_WINDOW_HZ / df)))
    lo, hi = int(SEARCH_MIN_HZ / df) + w, n - w - 1
    if hi <= lo:
        return None, 0.0
    cs = np.concatenate(([0.0], np.cumsum(s, dtype=np.float64)))
    i = np.arange(lo, hi)
    below = (cs[i + 1] - cs[i + 1 - w]) / w   # میانگین s[i-w+1 .. i]
    above = (cs[i + 1 + w] - cs[i + 1]) / w   # میانگین s[i+1 .. i+w]
    drop = below - above
    k = int(np.argmax(drop))
    idx, drop_db = int(i[k]), float(drop[k])
    if drop_db < CLIFF_MIN_DB:
        return None, drop_db
    # بالای پرتگاه نباید انرژی برگردد (جلوگیری از اشتباه گرفتن notch با lowpass)
    if float(np.percentile(s[idx + 1:], 90)) > float(below[k]) - CLIFF_TAIL_MARGIN_DB:
        return None, drop_db
    return idx, drop_db


def _edge_profile(s: np.ndarray, df: float, idx: int) -> Optional[Dict[str, float]]:
    """
    مشخصات دقیق لبه‌ی قطع: فرکانس نقطه‌ی نیمه‌ی افت، پهنای گذار (10% تا 90% افت)،
    سطح بالای لبه (plateau) و کف طیف بالای قطع. گذار باریک = فیلتر دیجیتال انکودر؛
    گذار پهن = افت طبیعی (آنالوگ/مستر قدیمی).
    """
    n = len(s)
    w = max(4, int(round(CLIFF_WINDOW_HZ / df)))
    a, b = max(0, idx - 6 * w), max(1, idx - 2 * w)
    plateau = float(np.median(s[a:b])) if b > a else float(s[idx])
    c, d = min(n - 1, idx + 2 * w), min(n, idx + 8 * w + 1)
    if d - c < 3:  # لبه خیلی نزدیک Nyquist است؛ از انتهای طیف استفاده کن
        c = max(idx + 1, n - max(3, w // 2))
        d = n
    floor = float(np.median(s[c:d])) if d > c else float(s[-1])
    total = plateau - floor
    if total < 6.0:
        return None
    hi_lv, lo_lv, mid_lv = plateau - 0.1 * total, floor + 0.1 * total, plateau - 0.5 * total
    j = idx
    while j > 0 and s[j] < hi_lv:
        j -= 1
    k = idx
    while k < n - 1 and s[k] > lo_lv:
        k += 1
    m = j
    while m < k and s[m] > mid_lv:
        m += 1
    return {
        "freq_hz": (m + 0.5) * df,
        "width_hz": max(df, (k - j) * df),
        "plateau_db": plateau,
        "floor_db": floor,
        "drop_db": total,
    }


def _hole_ratio(rows: List[np.ndarray], centers: np.ndarray, cutoff: Optional[float]) -> Optional[float]:
    """
    نسبت «حفره‌های طیفی» (باندی که از هر دو همسایه‌اش ≥20dB پایین‌تر است) بین 8kHz و قبل از قطع.
    انکودرهای lossy با بیت‌ریت کم بعضی باندها را کاملاً صفر می‌کنند؛ موسیقی طبیعی تقریباً هیچ‌وقت.
    """
    if not rows or cutoff is None:
        return None
    B = np.vstack(rows)
    sel = np.where((centers >= 8_000) & (centers <= cutoff - 1_500))[0]
    sel = sel[(sel > 0) & (sel < B.shape[1] - 1)]
    if len(sel) < 3:
        return None
    hole = B[:, sel] < np.minimum(B[:, sel - 1], B[:, sel + 1]) - HOLE_DROP_DB
    return float(hole.mean())


def _dynamics_estimate(block_rms: List[float], block_peak: List[float]) -> Optional[Dict[str, float]]:
    """تخمین دینامیک به سبک DR: پیکِ دوم در مقابل RMS بلندترین 20% بلوک‌های 3 ثانیه‌ای"""
    pairs = [(r, p) for r, p in zip(block_rms, block_peak) if r > SILENCE_RMS]
    if len(pairs) < 4:
        return None
    rms = np.array([r for r, _ in pairs])
    pk = np.array([p for _, p in pairs])
    top = max(1, int(round(len(rms) * 0.2)))
    rms_top = float(np.sqrt(np.mean(np.sort(rms)[::-1][:top] ** 2)))
    peak2 = float(np.sort(pk)[-2])
    rms_all = float(np.sqrt(np.mean(rms ** 2)))
    if rms_top <= 0 or peak2 <= 0 or rms_all <= 0:
        return None
    return {
        "dr": round(20 * math.log10(peak2 / rms_top), 1),
        "crest_db": round(20 * math.log10(float(pk.max()) / rms_all), 1),
        "peak_dbfs": round(20 * math.log10(float(pk.max())), 1),
        "blocks": len(pairs),
    }


def _spectrum(
    path: Path, ffmpeg: str, duration: float, native_sr: Optional[int], channels: Optional[int],
    max_segments: int, seg_len: float, include_curve: bool, errors: List[str],
) -> Dict[str, Any]:
    sr = int(min(native_sr or 44_100, MAX_ANALYSIS_SR))
    nyq = sr / 2.0
    nfft = 4096 if sr <= 50_000 else 8192
    hop = nfft // 2
    df = sr / nfft
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)  # Hann دوره‌ای
    nbins = nfft // 2 + 1
    step = max(1, int(round(BAND_HZ / df)))
    edges = np.arange(0, nbins, step)
    counts = np.diff(np.append(edges, nbins)).astype(np.float64)
    centers = (edges + counts / 2.0) * df
    ch = 2 if channels == 2 else 1

    acc = np.zeros(nbins, dtype=np.float64)
    seg_acc: List[Tuple[np.ndarray, int]] = []
    band_rows: List[np.ndarray] = []
    st = np.zeros(5)  # LL, RR, LR, MM, SS
    n_samples = clip_samples = clip_runs = 0
    block_rms: List[float] = []
    block_peak: List[float] = []
    blk = max(1, int(BLOCK_SEC * sr))
    frames, seconds, used = 0, 0.0, 0

    segs = _plan_segments(duration, max_segments, seg_len)
    # decode همه‌ی قطعه‌ها هم‌زمان شروع میشه؛ پردازش (FFT و آمار) بعدش به همون ترتیب قبلی انجام میشه (نتیجه یکسان)
    decodes = [_DEC_POOL.submit(_decode_pcm, ffmpeg, path, start, length, sr, ch) for start, length in segs]
    for (start, length), dec in zip(segs, decodes):
        try:
            x = dec.result()
        except subprocess.TimeoutExpired:
            errors.append(f"spectrum: decode timeout at {start:.0f}s")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"spectrum: decode failed at {start:.0f}s: {e}")
            continue
        if x is None or len(x) < nfft:
            continue
        mid = x.mean(axis=1) if ch == 2 else x[:, 0]
        seg = np.zeros(nbins, dtype=np.float64)
        rows: List[np.ndarray] = []
        kept = _accumulate(mid, nfft, hop, window, seg, edges, counts, rows)
        seconds += len(x) / sr
        if kept:
            acc += seg
            frames += kept
            used += 1
            seg_acc.append((seg, kept))
            band_rows.extend(rows)

        if ch == 2:  # آمار استریو
            l, r = x[:, 0].astype(np.float64), x[:, 1].astype(np.float64)
            st += [float(l @ l), float(r @ r), float(l @ r), float(np.sum((l + r) ** 2) / 4), float(np.sum((l - r) ** 2) / 4)]
        hit = np.abs(x) >= CLIP_LEVEL  # کلیپینگ: سه نمونه‌ی پشت‌سرهم در سقف
        n_samples += hit.size
        clip_samples += int(hit.sum())
        for c in range(ch):
            col = hit[:, c]
            if col.size >= 3:
                clip_runs += int(np.sum(col[:-2] & col[1:-1] & col[2:]))
        for b0 in range(0, len(mid) - blk + 1, blk):  # بلوک‌های 3ثانیه‌ای برای DR
            m = mid[b0:b0 + blk]
            block_rms.append(float(np.sqrt(np.mean(np.square(m, dtype=np.float64)))))
            block_peak.append(float(np.max(np.abs(x[b0:b0 + blk]))))
        del x, mid  # آزادسازی فوری بافر قطعه

    if frames < MIN_FRAMES:
        return {"analyzed": False, "error": "not enough non-silent audio to analyze",
                "sample_rate_used": sr, "nyquist_hz": nyq}

    db = 10.0 * np.log10(acc / frames + 1e-30)
    s = _smooth(db - db.max(), df)
    ref = _band_mean(s, df, 1_000, 4_000) or 0.0
    idx, cliff_db = _find_cliff(s, df)
    edge = _edge_profile(s, df, idx) if idx is not None else None
    cutoff = (edge["freq_hz"] if edge else (idx + 0.5) * df) if idx is not None else None

    # قطع هر قطعه جدا؛ فایل lossy در همه‌ی قطعه‌ها یک سقف ثابت دارد
    seg_cuts: List[Optional[float]] = []
    for sacc, k in seg_acc:
        if k < MIN_FRAMES:
            continue
        sdb = 10.0 * np.log10(sacc / k + 1e-30)
        i2, _ = _find_cliff(_smooth(sdb - sdb.max(), df), df)
        seg_cuts.append((i2 + 0.5) * df if i2 is not None else None)
    seg_agree = None
    if len(seg_cuts) >= 2 and cutoff is not None:
        seg_agree = sum(1 for c in seg_cuts if c is not None and abs(c - cutoff) <= SEG_AGREE_HZ) / len(seg_cuts)

    bands = {}
    for lo, hi in ((12_000, 16_000), (16_000, 18_000), (18_000, 20_000), (20_000, 22_000)):
        if hi <= nyq + 1:
            m = _band_mean(s, df, lo, hi)
            if m is not None:
                bands[f"{lo // 1000}-{hi // 1000}kHz"] = round(m - ref, 1)

    out: Dict[str, Any] = {
        "analyzed": True,
        "sample_rate_used": sr,
        "nyquist_hz": nyq,
        "segments_used": used,
        "seconds_analyzed": round(seconds, 1),
        "frames": frames,
        "cutoff_hz": round(cutoff, 0) if cutoff else None,
        "cliff_db": round(cliff_db, 1),
        "edge_width_hz": round(edge["width_hz"], 0) if edge else None,
        "floor_db": round(edge["floor_db"] - ref, 1) if edge else None,
        "seg_count": len(seg_cuts),
        "seg_agree": round(seg_agree, 2) if seg_agree is not None else None,
        "seg_cutoffs_hz": [round(c, 0) if c else None for c in seg_cuts],
        "holes_ratio": None,
        "band_levels_db": bands,
        "dynamics": _dynamics_estimate(block_rms, block_peak),
        "clipping": {
            "ratio": clip_runs / n_samples if n_samples else 0.0,
            "samples": clip_samples,
        },
        "stereo": None,
    }
    hr = _hole_ratio(band_rows, centers, cutoff)
    out["holes_ratio"] = round(hr, 3) if hr is not None else None

    LL, RR, LR, MM, SS = st
    if ch == 2 and LL > 0 and RR > 0:
        out["stereo"] = {
            "correlation": round(LR / math.sqrt(LL * RR), 3),
            "side_db": round(10 * math.log10((SS + 1e-30) / (MM + 1e-30)), 1),
            "balance_db": round(10 * math.log10(LL / RR), 1),
        }
    elif ch == 2 and (LL > 0 or RR > 0):  # یک کانال کاملاً ساکت
        out["stereo"] = {"correlation": 0.0, "side_db": 0.0, "balance_db": 99.0 if LL > 0 else -99.0}

    if include_curve:
        pts = np.linspace(0, len(s) - 1, min(400, len(s))).astype(int)
        out["spectrum"] = {
            "freq_hz": [round(float(p * df), 1) for p in pts],
            "level_db": [round(float(s[p] - ref), 1) for p in pts],
        }
    return out


def _spectrogram(
    path: Path, ffmpeg: str, duration: float, native_sr: Optional[int], errors: List[str],
) -> Optional[Dict[str, Any]]:
    """
    اسپکتروگرام کل آهنگ (زمان × فرکانس، dBFS) با حافظه‌ی ثابت: یک بار دیکود استریمی مونو و
    برای هر ستونِ زمانی چند فریمِ FFT میانگین‌گیری میشه. خروجی uint8 کوچیک (SPEC_ROWS × ستون‌ها)؛
    ردیف 0 = پایین‌ترین فرکانس، 0 → -120 dBFS روی 255 → 0 نگاشت میشه.
    """
    if duration < 5 or duration > SPEC_MAX_DURATION:
        return None
    sr = int(min(native_sr or 44_100, 48_000))
    nfft = 2048
    nbins = nfft // 2 + 1
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)
    sumw = float(window.sum())
    total = int(duration * sr)
    cols = int(max(40, min(SPEC_COLS, duration * 8)))
    span = total / cols
    k_per_col = 3
    last = max(0, total - nfft)
    plan: List[Tuple[int, int]] = []  # (ستون، نمونه‌ی شروع فریم)؛ شروع‌ها صعودی‌اند
    for c in range(cols):
        for k in range(k_per_col):
            center = (c + (k + 0.5) / k_per_col) * span
            plan.append((c, int(min(max(0.0, center - nfft / 2), last))))

    pw = np.zeros((cols, nbins), dtype=np.float64)
    cnt = np.zeros(cols, dtype=np.int64)
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-hide_banner", "-i", str(path),
           "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(SPEC_TIMEOUT, proc.kill)
    timer.start()
    buf = np.zeros(0, dtype=np.float32)
    base, i, n, eof = 0, 0, len(plan), False
    try:
        while i < n:
            need = plan[i][1] + nfft
            while base + len(buf) < need and not eof:
                chunk = proc.stdout.read(1 << 20)  # مضرب 4 بایت؛ فقط آخرین تکه ممکنه نباشه
                if not chunk:
                    eof = True
                    break
                buf = np.concatenate([buf, np.frombuffer(chunk[: len(chunk) // 4 * 4], dtype="<f4")])
            avail = base + len(buf)
            j = i
            while j < n and j - i < 256 and plan[j][1] + nfft <= avail:
                j += 1
            if j == i:
                break  # جریان زودتر تموم شد (مدت تخمینی کمی بلندتر از واقعیت بود)
            fr = np.stack([buf[plan[k][1] - base: plan[k][1] - base + nfft] for k in range(i, j)]).astype(np.float64)
            amp = np.abs(np.fft.rfft(np.nan_to_num(fr) * window, axis=1)) * 2.0 / sumw
            p = amp * amp
            for r, k in enumerate(range(i, j)):
                pw[plan[k][0]] += p[r]
                cnt[plan[k][0]] += 1
            i = j
            keep = plan[i][1] if i < n else base + len(buf)
            if keep > base:
                buf = buf[keep - base:]
                base = keep
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(Exception):
            proc.stdout.close()
        proc.wait()

    valid = int(np.argmax(cnt == 0)) if (cnt == 0).any() else cols  # اولین ستونِ خالی = پایانِ داده
    if valid < 20:
        errors.append("spectrogram: not enough decoded audio")
        return None
    if valid < cols:
        errors.append(f"spectrogram: partial ({valid}/{cols} columns)")
    m = pw[:valid] / cnt[:valid, None]
    rows = (m[:, : SPEC_ROWS * 4].reshape(valid, SPEC_ROWS, 4)).mean(axis=2).T  # (ردیف، ستون)
    db = 10.0 * np.log10(rows + 1e-12)
    q8 = np.clip((db - SG_FLOOR_DB) / (-SG_FLOOR_DB) * 255.0, 0, 255).round().astype(np.uint8)
    return {"img": q8, "fmax": sr / 2.0, "duration": duration * valid / cols, "cols": valid}


# ───────────────────────── 4) عمق بیت واقعی و لودنس ─────────────────────────


def _bit_depth_check(path: Path, ffmpeg: str, duration: float, errors: List[str]) -> Optional[Dict[str, Any]]:
    """
    فایل لاسلس را بدون تغییر به s32 می‌خواند و پایین‌ترین بیتِ مورداستفاده در کل نمونه‌ها را پیدا می‌کند.
    اگر فایلِ «24 بیتی» همه‌ی 8 بیت پایینش صفر باشد، در اصل 16 بیتی بوده و به 24 بیت کش آمده.
    """
    or_all, nz_total = 0, 0

    def _dec(start: float, length: float) -> bytes:
        cmd = [
            ffmpeg, "-nostdin", "-v", "error", "-hide_banner",
            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(path),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "pcm_s32le", "-f", "s32le", "pipe:1",
        ]
        return _run(cmd, DECODE_TIMEOUT)[1]

    segs = _plan_segments(duration, BITDEPTH_SEGMENTS, 8.0)
    decodes = [_DEC_POOL.submit(_dec, start, length) for start, length in segs]  # قطعه‌ها هم‌زمان decode میشن
    for (start, length), dec in zip(segs, decodes):
        try:
            out = dec.result()
        except subprocess.TimeoutExpired:
            errors.append(f"bitdepth: decode timeout at {start:.0f}s")
            continue
        n = len(out) // 4
        if n == 0:
            continue
        x = np.frombuffer(out, dtype="<i4", count=n)
        nz = x[x != 0]
        if nz.size == 0:
            continue
        nz_total += int(nz.size)
        or_all |= int(np.bitwise_or.reduce(nz.astype(np.int64) & 0xFFFFFFFF))
    if nz_total < 2000 or or_all == 0:
        return None
    tz = (or_all & -or_all).bit_length() - 1
    return {"effective_bits": 32 - tz, "samples_checked": nz_total}


def _loudness_ok(res: Dict[str, float]) -> bool:
    """عدد قابل‌قبول؟ -70 = کف اندازه‌گیری (سکوت)؛ بالای -1 LUFS هم برای موسیقی واقعی غیرممکنه (خروجی خراب ffmpeg)."""
    v = res.get("lufs")
    return v is not None and -69.9 < v < -1.0


def _loudness(path: Path, ffmpeg: str, errors: List[str]) -> Optional[Dict[str, float]]:
    """لودنس استاندارد EBU R128: اول فیلتر ebur128، اگه خروجیش خراب/ناخونا بود فیلتر loudnorm (LUFS، LRA و True Peak)"""
    num = r"(-?\d+(?:\.\d+)?)"
    base = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn"]
    last_tail = ""
    for flt in ("ebur128=peak=true:framelog=quiet", "ebur128=peak=true"):
        try:
            _, _, err = _run(base + ["-af", flt, "-f", "null", "-"], EBU_TIMEOUT)
        except subprocess.TimeoutExpired:
            errors.append("loudness: timeout")
            return None
        txt = err.decode("utf-8", "ignore")
        k = txt.rfind("Summary:")
        if k < 0:
            continue
        tail = last_tail = txt[k:]
        res: Dict[str, float] = {}
        for key, pat in (("lufs", rf"\n\s+I:\s+{num}\s+LUFS"), ("lra", rf"\n\s+LRA:\s+{num}\s+LU"),
                         ("true_peak", rf"\n\s+Peak:\s+{num}\s+dBFS")):
            m = re.search(pat, tail)
            if m:
                res[key] = float(m.group(1))
        if _loudness_ok(res):
            return res

    # روش جایگزین: loudnorm در حالت اندازه‌گیری (خروجی JSON)
    if last_tail:
        log.warning("ebur128 summary نامعتبر بود؛ میرم سراغ loudnorm. summary:\n%s", last_tail[:400])
    try:
        _, _, err = _run(base + ["-af", "loudnorm=print_format=json", "-f", "null", "-"], EBU_TIMEOUT)
    except subprocess.TimeoutExpired:
        errors.append("loudness: timeout")
        return None
    txt = err.decode("utf-8", "ignore")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", txt)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        res = {"lufs": float(d["input_i"]), "lra": float(d["input_lra"]), "true_peak": float(d["input_tp"])}
    except (ValueError, KeyError, TypeError):
        return None
    return res if _loudness_ok(res) else None


# ───────────────────────── حکم نهایی (اعتبارسنجی کیفیت) ─────────────────────────

_SEV = {"genuine": 0, "suspicious": 1, "fake": 2}
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _tier(cutoff: Optional[float]) -> Tuple[str, Optional[int]]:
    if cutoff is None:
        return "full", None
    for limit, code, kbps in _TIERS:
        if cutoff < limit:
            return code, kbps
    return "full", None


def _lossy_thresholds(codec: str, declared: Optional[int]) -> Optional[Tuple[float, float]]:
    """(حد fake, حد suspicious) برای cutoff؛ بر اساس کدک و بیت‌ریت اعلام‌شده"""
    if not declared:
        return None
    if codec == "opus":
        if declared >= 128:
            return 13_000.0, 17_000.0
        if declared >= 64:
            return 0.0, 11_000.0
        return None
    if codec == "vorbis":
        if declared >= 224:
            return 16_500.0, 18_500.0
        if declared >= 160:
            return 15_000.0, 16_800.0
        if declared >= 96:
            return 0.0, 12_000.0
        return None
    if codec == "aac":
        if declared >= 224:
            return 17_000.0, 19_000.0
        if declared >= 160:
            return 15_000.0, 17_000.0
        if declared >= 96:
            return 0.0, 13_000.0
        return None
    if declared >= 300:  # MP3 و بقیه
        return 17_800.0, 19_000.0
    if declared >= 224:
        return 16_800.0, 18_000.0
    if declared >= 160:
        return 15_500.0, 16_500.0
    if declared >= 96:
        return 0.0, 12_500.0
    return None


def _edge_score(spec: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, bool]]:
    """
    قدرت شواهد برای «برش مصنوعی انکودر» (0 تا 7):
    گذار تیز، ثبات قطع در قطعه‌ها، کف خیلی پایین بالای قطع، افت قوی، حفره‌های طیفی.
    """
    flags = {"sharp": False, "gentle": False, "consistent": False, "deep": False, "holes": False}
    if not spec or spec.get("cutoff_hz") is None:
        return 0, flags
    w, drop = spec.get("edge_width_hz"), spec.get("cliff_db") or 0.0
    flags["sharp"] = w is not None and w <= SHARP_WIDTH_HZ and drop >= 25
    flags["gentle"] = (w is not None and w >= GENTLE_WIDTH_HZ) or drop < 20
    flags["consistent"] = (spec.get("seg_count") or 0) >= 2 and (spec.get("seg_agree") or 0) >= 0.7
    flags["deep"] = spec.get("floor_db") is not None and spec["floor_db"] <= DEEP_FLOOR_DB
    flags["holes"] = (spec.get("holes_ratio") or 0) >= HOLE_MIN_RATIO
    sc = 2 * flags["sharp"] + 2 * flags["consistent"] + flags["deep"] + (drop >= CLIFF_STRONG_DB) + flags["holes"]
    if flags["gentle"]:
        sc = max(0, sc - 2)
    return int(sc), flags


def _conf_from_score(sc: int) -> str:
    return "high" if sc >= 5 else "medium" if sc >= 3 else "low"


def _judge(
    tech: Dict[str, Any], spec: Optional[Dict[str, Any]],
    bd: Optional[Dict[str, Any]] = None, loud: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    # (کد، شدت، پارامترها، اطمینان)
    reasons: List[Tuple[str, str, Dict[str, Any], str]] = []
    notes: List[Tuple[str, Dict[str, Any]]] = []
    lossless, codec = tech["is_lossless"], tech.get("codec") or ""
    declared, sr, ext = tech.get("bitrate_kbps"), tech.get("sample_rate"), tech.get("extension")

    # 1) پسوند «لاسلس» ولی محتوای واقعی lossy
    if ext in LOSSLESS_EXT and codec in LOSSY_CODECS:
        reasons.append(("ext_codec_mismatch", "fake", {"ext": ext, "codec": tech.get("codec_label")}, "high"))
    elif tech.get("extension_mismatch"):
        reasons.append(("container_mismatch", "info", {"ext": ext, "container": tech.get("container")}, "medium"))

    # 2) عمق بیت واقعی (فقط لاسلس)
    if bd and lossless:
        eff, dec = bd["effective_bits"], tech.get("bit_depth")
        if dec and dec >= 20 and eff <= 16:
            reasons.append(("bitdepth_padded", "fake", {"declared": dec, "eff": eff}, "high"))
        elif dec and eff <= dec:
            reasons.append(("bitdepth_ok", "info", {"declared": dec, "eff": eff}, "medium"))

    cut = cliff = nyq = None
    tier_code, tier_kbps = "full", None
    sc, flags = 0, {}
    analyzed = bool(spec and spec.get("analyzed"))
    if analyzed:
        cut, cliff, nyq = spec["cutoff_hz"], spec["cliff_db"], spec["nyquist_hz"]
        tier_code, tier_kbps = _tier(cut)
        sc, flags = _edge_score(spec)
        conf = _conf_from_score(sc)
        wide_ok = (spec.get("seg_count") or 0) >= 3

        if cut is None and nyq < 19_000:
            reasons.append(("limited_nyquist", "info", {"nyq": nyq}, "low"))
        elif lossless:
            fake_thr, susp_thr = min(19_300.0, 0.9 * nyq), min(21_000.0, 0.95 * nyq)
            hires_fake = bool(sr and sr >= 88_200 and cut is not None and cut <= max(24_500.0, 0.275 * sr))
            if cut is not None and cut < fake_thr:
                if flags["gentle"] and sc < 3:
                    reasons.append(("lossless_natural_rolloff", "suspicious", {"cut": cut}, "low"))
                else:
                    reasons.append(("lossless_from_lossy", "fake", {"cut": cut, "tier": tier_code}, conf))
            elif cut is not None and cut < susp_thr and not hires_fake:
                lvl_conf = "medium" if (sc >= 5 and cut < 20_000) else "low"
                reasons.append(("lossless_suspect", "suspicious", {"cut": cut}, lvl_conf))
            elif hires_fake:
                lvl = "fake" if (flags["sharp"] or flags["deep"]) else "suspicious"
                reasons.append(("hires_upsampled", lvl, {"sr": sr, "cut": cut}, conf if lvl == "fake" else "low"))
            else:
                reasons.append(("genuine_lossless", "genuine", {"nyq": nyq}, "high" if wide_ok else "medium"))
            # آپ‌سمپل از CD به 48k یا بیشتر (کیفیت واقعی همان CD است؛ جعل حساب نمی‌شود)
            if (sr and sr > 44_100 and cut is not None and 20_800 <= cut <= 22_300
                    and flags["sharp"] and not hires_fake):
                notes.append(("resampled_cd", {"sr": sr}))
        else:
            thr = _lossy_thresholds(codec, declared)
            if thr is not None and cut is not None:
                fake_thr, susp_thr = min(thr[0], 0.9 * nyq), min(thr[1], 0.93 * nyq)
                if cut < fake_thr:
                    if flags["gentle"] and sc < 3:
                        reasons.append(("lossy_suspect", "suspicious", {"cut": cut, "declared": declared, "tier": tier_code}, "low"))
                    else:
                        reasons.append(("lossy_upscaled", "fake", {"cut": cut, "declared": declared, "tier": tier_code}, conf))
                elif cut < susp_thr:
                    reasons.append(("lossy_suspect", "suspicious", {"cut": cut, "declared": declared, "tier": tier_code},
                                    "medium" if sc >= 5 else "low"))
                else:
                    reasons.append(("genuine_lossy", "genuine", {"declared": declared}, "high" if wide_ok else "medium"))
            else:
                reasons.append(("genuine_lossy", "genuine", {"declared": declared}, "medium"))

        # حفره‌های طیفی: شاهد مکمل برای انکود lossy
        if flags.get("holes") and any(r[1] in ("fake", "suspicious") for r in reasons):
            reasons.append(("holes", "info", {"ratio": spec.get("holes_ratio")}, "medium"))

        # استریو
        stp = spec.get("stereo")
        if stp:
            if abs(stp["balance_db"]) >= 30:
                notes.append(("dead_channel", {}))
            elif stp["side_db"] <= -60:
                notes.append(("dual_mono", {}))
            elif stp["correlation"] <= -0.3:
                notes.append(("phase_inverted", {"corr": stp["correlation"]}))
            elif stp["side_db"] <= -30:
                notes.append(("near_mono", {}))
        # کلیپینگ
        clp = spec.get("clipping") or {}
        if clp.get("ratio", 0) >= CLIP_RATIO_WARN:
            notes.append(("clipping", {"ratio": clp["ratio"]}))

    # لودنس / دینامیک
    if loud and loud.get("true_peak") is not None and loud["true_peak"] > 0.0:
        notes.append(("true_peak_over", {"tp": loud["true_peak"]}))
    dyn = (spec or {}).get("dynamics") if analyzed else None
    if loud and dyn and loud.get("lufs") is not None and loud["lufs"] > -8.0 and dyn["dr"] <= 6:
        notes.append(("loud_master", {"lufs": loud["lufs"], "dr": dyn["dr"]}))

    graded = [r for r in reasons if r[1] in _SEV]
    if graded:
        top = max(_SEV[r[1]] for r in graded)
        verdict = [k for k, v in _SEV.items() if v == top][0]
        confidence = max((r[3] for r in graded if r[1] == verdict), key=_CONF_RANK.__getitem__)
        if verdict != "genuine":  # شواهد «مصنوعی» ملاک است؛ ادعای سالم‌بودنش را در متن نیار
            reasons = [r for r in reasons if r[1] != "genuine"]
    else:
        verdict, confidence = "inconclusive", "low"
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason_codes": [r[0] for r in reasons],
        "reasons": [_reason_text(r[0], r[2]) for r in reasons],
        "notes": [(c, _reason_text(c, p)) for c, p in notes],
        "edge_score": sc,
        "edge_flags": flags,
        "estimated_class": tier_code if (analyzed and nyq and not (cut is None and nyq < 19_000)) else None,
        "estimated_bitrate_kbps": tier_kbps,
    }


_TIER_FA = {
    "<=64": "64 kbps و کمتر", "96": "حدود 96 kbps", "128": "حدود 128 kbps",
    "192": "حدود 160 تا 192 kbps", "256-320": "حدود 256 تا 320 kbps",
    "full": "کامل (320 kbps به بالا یا لاسلس)",
}


def _reason_text(code: str, p: Dict[str, Any]) -> str:
    if code == "ext_codec_mismatch":
        return f"پسوند فایل {p['ext']} است ولی محتوای واقعی {p['codec']} (فشرده با افت کیفیت) است."
    if code == "container_mismatch":
        return f"کانتینر واقعی {p['container']} است و با پسوند {p['ext']} نمی‌خواند."
    if code == "bitdepth_padded":
        return (f"فایل {_fa(p['declared'])} بیتی اعلام شده ولی فقط {_fa(p['eff'])} بیت داده‌ی واقعی داره؛ "
                "بیت‌های اضافه صفره و 16 بیت به 24 بیت کش اومده.")
    if code == "bitdepth_ok":
        return f"عمق بیت واقعی ({_fa(p['eff'])} بیت) با {_fa(p['declared'])} بیت اعلام‌شده جور درمیاد."
    if code == "limited_nyquist":
        return f"Sample Rate پایین است و بالاتر از {_khz(p['nyq'])} kHz قابل بررسی نیست."
    if code == "lossless_from_lossy":
        return (f"فایل به‌عنوان لاسلس ذخیره شده ولی طیف با برش تیز در حدود {_khz(p['cut'])} kHz قطع شده؛ "
                f"یعنی از یک منبع {_TIER_FA[p['tier']]} (MP3/AAC) تبدیل شده است.")
    if code == "lossless_natural_rolloff":
        return (f"طیف از حدود {_khz(p['cut'])} kHz کم میشه ولی افتش تدریجیه، نه برش تیز دیجیتال؛ "
                "بیشتر شبیه ضبط/مستر قدیمی یا آنالوگه تا تبدیل از MP3. قطعی نیست.")
    if code == "lossless_suspect":
        return (f"طیف لاسلس در حدود {_khz(p['cut'])} kHz قطع شده؛ ممکن است از MP3/AAC با کیفیت بالا آمده باشد "
                "(یا فقط فیلتر مسترینگ باشد).")
    if code == "genuine_lossless":
        return f"طیف تا نزدیک {_khz(p['nyq'])} kHz ادامه دارد و نشانه‌ای از تبدیل از فایل فشرده دیده نشد."
    if code == "hires_upsampled":
        return (f"Sample Rate برابر {_khz(p['sr'])} kHz است ولی محتوا بالای {_khz(p['cut'])} kHz خالی است؛ "
                "یعنی از 44.1/48 kHz آپ‌سمپل شده.")
    if code == "lossy_upscaled":
        return (f"بیت‌ریت اعلام‌شده {_fa(p['declared'])} kbps است ولی طیف در {_khz(p['cut'])} kHz قطع شده "
                f"که معادل {_TIER_FA[p['tier']]} است؛ فایل آپ‌اسکیل شده.")
    if code == "lossy_suspect":
        return (f"بیت‌ریت اعلام‌شده {_fa(p['declared'])} kbps است ولی قطع فرکانس در {_khz(p['cut'])} kHz "
                f"کمی پایین‌تر از انتظار است (معادل {_TIER_FA[p['tier']]}).")
    if code == "genuine_lossy":
        return "قطع فرکانسی با بیت‌ریت اعلام‌شده سازگار است."
    if code == "holes":
        return (f"حفره‌های طیفی هم دیده شد ({_fa(f'{(p['ratio'] or 0) * 100:.0f}')}% باندها)؛ "
                "نشانه‌ی انکود lossy با بیت‌ریت کمه.")
    if code == "resampled_cd":
        return (f"Sample Rate {_khz(p['sr'])} kHz است ولی محتوا در حدود 21 تا 22 kHz تموم میشه؛ "
                "احتمالاً از 44.1 kHz (CD) آپ‌سمپل شده و کیفیت واقعی همون CD است.")
    if code == "dual_mono":
        return "هر دو کانال کاملاً یکسانه؛ استریو واقعی نیست (مونوی تکرارشده)."
    if code == "near_mono":
        return "تصویر استریو خیلی باریکه (تقریباً مونو)."
    if code == "dead_channel":
        return "یکی از کانال‌ها تقریباً ساکته."
    if code == "phase_inverted":
        return "دو کانال تا حد زیادی خلاف فازند؛ در پخش مونو بخشی از صدا حذف میشه."
    if code == "clipping":
        return (f"کلیپینگ دیجیتال دیده شد (حدود {_fa(f'{p['ratio'] * 100:.2f}')}% نمونه‌ها)؛ "
                "ممکنه صدا تحریف یا خش داشته باشه.")
    if code == "true_peak_over":
        return (f"True Peak برابر {_fa(f'{p['tp']:+.1f}')} dBTP است (بالای 0)؛ "
                "ممکنه موقع پخش یا تبدیل به MP3/AAC تحریف بده.")
    if code == "loud_master":
        return (f"مستر خیلی بلند و فشرده است ({_fa(f'{p['lufs']:.1f}')} LUFS و DR≈{_fa(int(p['dr']))})؛ "
                "دینامیک کمه.")
    return code


# ───────────────────────── پیام قالب‌بندی‌شده ─────────────────────────

_VERDICT_HEAD = {
    "genuine": "✅ نتیجه: کیفیت واقعی و سالم",
    "suspicious": "⚠️ نتیجه: مشکوک به آپ‌اسکیل",
    "fake": "🚨 نتیجه: فیک / آپ‌اسکیل‌شده",
    "inconclusive": "❔ نتیجه: تحلیل طیفی قطعی نبود",
}
_CONF_FA = {"high": "بالا", "medium": "متوسط", "low": "کم"}
_SEP = "━━━━━━━━━━━━━━"


def _channels_label(n: Optional[int]) -> Optional[str]:
    if not n:
        return None
    return {1: "مونو (1)", 2: "استریو (2)"}.get(n, f"{_fa(n)} کاناله")


def _sgn(v: float, digits: int = 1) -> str:
    return _fa(f"{v:.{digits}f}")


def format_report(result: Dict[str, Any]) -> str:
    """پیام فارسیِ آماده‌ی ارسال (متن ساده؛ بدون نیاز به parse_mode)"""
    if not result.get("ok"):
        return result.get("message") or "❌ فایل صوتی قابل بررسی نبود."
    md, t, q = result["metadata"], result["technical"], result["quality"]
    L: List[str] = []

    # سربرگ
    head = f"🎵 {_clip(md['title'] or result['file']['stem'])}"
    if md["artist"]:
        head += f" — {_clip(md['artist'])}"
    L.append(head)
    row = []
    if md["album"]:
        row.append(f"💿 {_clip(md['album'], 60)}")
    if md["year"]:
        row.append(f"📅 {_fa(md['year'])}")
    if md["genre"]:
        row.append(f"🎼 {_clip(md['genre'], 30)}")
    if row:
        L.append("  |  ".join(row))
    row = []
    if md["album_artist"] and md["album_artist"] != md["artist"]:
        row.append(f"👤 {_clip(md['album_artist'], 40)}")
    if md["track_number"]:
        row.append(f"🔢 ترک {_fa(md['track_number'])}" + (f" از {_fa(md['track_total'])}" if md["track_total"] else ""))
    if md["composer"]:
        row.append(f"✍️ {_clip(md['composer'], 40)}")
    if row:
        L.append("  |  ".join(row))
    cv = md["cover"]
    if cv["present"]:
        dims = f" ({_fa(cv['width'])}×{_fa(cv['height'])})" if cv["width"] and cv["height"] else ""
        L.append(f"🖼 کاور: دارد{dims}")
    elif cv["present"] is False:
        L.append("🖼 کاور: ندارد")

    # مشخصات فنی
    L += ["", _SEP, "⚙️ مشخصات فنی"]
    if t["container"]:
        L.append(f"📦 کانتینر: {t['container']}")
    if t["codec_label"]:
        L.append(f"🧩 انکودر: {t['codec_label']}" + (" • لاسلس" if t["is_lossless"] else " • lossy"))
    if _pick_encoder(md, t):
        L.append(f"🛠 نرم‌افزار انکود: {_clip(_pick_encoder(md, t), 50)}")
    if t["sample_rate"]:
        L.append(f"🎚 Sample Rate: {_fa(f'{t['sample_rate'] / 1000:g}')} kHz")
    bdc = q.get("bit_depth_check")
    if t["bit_depth"]:
        extra = f" (عمق واقعی: {_fa(bdc['effective_bits'])} بیت)" if bdc and bdc["effective_bits"] != t["bit_depth"] else ""
        L.append(f"🔢 Bit Depth: {_fa(t['bit_depth'])} بیت{extra}")
    else:
        L.append("🔢 Bit Depth: ندارد (کدک lossy)" if not t["is_lossless"] else "🔢 Bit Depth: نامشخص")
    if _channels_label(t["channels"]):
        L.append(f"🔊 کانال: {_channels_label(t['channels'])}")
    if t["bitrate_kbps"]:
        mode = f" ({t['bitrate_mode']})" if t.get("bitrate_mode") else ""
        L.append(f"📊 بیت‌ریت: {_fa(t['bitrate_kbps'])} kbps{mode}")
    L.append(f"⏱ مدت: {_dur(t['duration'])}  |  📁 حجم: {_mb(t['size_bytes'])} مگ")

    # تحلیل طیف
    L += ["", _SEP, "🔬 تحلیل طیف فرکانسی"]
    if q.get("analyzed"):
        if q["cutoff_hz"]:
            kind = ""
            if q.get("edge_width_hz"):
                shape = ("برش تیز دیجیتال" if q["edge_width_hz"] <= SHARP_WIDTH_HZ
                         else "افت تدریجی" if q["edge_width_hz"] >= GENTLE_WIDTH_HZ else "افت نسبتاً تند")
                kind = f"، {shape}"
            L.append(f"✂️ قطع فرکانس: حدود {_khz(q['cutoff_hz'])} kHz (افت {_fa(int(q['cliff_db']))} dB{kind})")
            if q.get("seg_count", 0) >= 2 and q.get("seg_agree") is not None:
                ok = int(round(q["seg_agree"] * q["seg_count"]))
                L.append(f"📍 ثبات قطع: در {_fa(ok)} از {_fa(q['seg_count'])} بخش آهنگ یکسانه")
            if q.get("floor_db") is not None:
                L.append(f"🔇 کف طیف بالای قطع: {_sgn(q['floor_db'])} dB")
            if q.get("holes_ratio") is not None:
                L.append(f"🕳 حفره‌های طیفی: {_fa(f'{q['holes_ratio'] * 100:.0f}')}% باندها")
        else:
            L.append(f"✂️ قطع فرکانسی: ندارد (طیف تا {_khz(q['nyquist_hz'])} kHz ادامه دارد)")
        if q.get("estimated_class"):
            L.append(f"📉 کیفیت واقعی (تخمین): {_TIER_FA[q['estimated_class']]}")
        L.append(f"🧮 بررسی روی {_fa(q['segments_used'])} بخش ({_fa(int(q['seconds_analyzed']))} ثانیه)")
    else:
        L.append("تحلیل طیفی انجام نشد.")

    # دینامیک و لودنس
    ld, dyn, clp, stp = q.get("loudness"), q.get("dynamics"), q.get("clipping"), q.get("stereo")
    if ld or dyn or stp:
        L += ["", _SEP, "🎛 دینامیک، لودنس و استریو"]
        if ld:
            parts = [f"{_sgn(ld['lufs'])} LUFS"]
            if ld.get("lra") is not None:
                parts.append(f"LRA {_sgn(ld['lra'])}")
            if ld.get("true_peak") is not None:
                parts.append(f"True Peak {_fa(f'{ld['true_peak']:+.1f}')} dBTP")
            L.append("🔊 لودنس: " + " | ".join(parts))
        if dyn:
            L.append(f"📈 دینامیک (تخمین): DR≈{_fa(int(round(dyn['dr'])))} | Crest {_sgn(dyn['crest_db'])} dB")
        if clp is not None and q.get("analyzed"):
            L.append("✔️ کلیپینگ: ندارد" if clp["ratio"] < CLIP_RATIO_WARN
                     else f"⚠️ کلیپینگ: {_fa(f'{clp['ratio'] * 100:.2f}')}% نمونه‌ها")
        if stp:
            L.append(f"🎧 استریو: همبستگی {_sgn(stp['correlation'], 2)} | عرض (Side/Mid) {_sgn(stp['side_db'], 0)} dB")

    # حکم
    L += ["", _VERDICT_HEAD[q["verdict"]]
          + (f" (اطمینان: {_CONF_FA[q['confidence']]})" if q["verdict"] != "inconclusive" else "")]
    L += [f"• {r}" for r in q["reasons"]]
    if q.get("notes"):
        L += ["", "📝 نکته‌های تکمیلی"]
        L += [f"• {txt}" for _, txt in q["notes"]]
    if q["verdict"] in ("fake", "suspicious") and q["confidence"] != "high":
        L.append("ℹ️ این تشخیص بر پایه‌ی تحلیل طیف است؛ برای منابع قدیمی/آنالوگ یا مسترینگ خاص قطعی نیست.")
    return "\n".join(L)


# ───────────────────────── مرحله‌های کش‌دار تحلیل ─────────────────────────


def _piece_spectrum(path: Path, ffmpeg: str, tech: Dict[str, Any], max_segments: int, seg_len: float):
    def go():
        errs: List[str] = []
        spec = _spectrum(path, ffmpeg, tech["duration"], tech.get("sample_rate"), tech.get("channels"),
                         max_segments, seg_len, True, errs)  # منحنی همیشه ساخته میشه (ارزون)؛ اگه لازم نبود بعداً حذف میشه
        return spec, errs

    return _memo(path, ("spec", max_segments, seg_len), go)


def _piece_bitdepth(path: Path, ffmpeg: str, duration: float):
    def go():
        errs: List[str] = []
        return _bit_depth_check(path, ffmpeg, duration, errs), errs

    return _memo(path, ("bd",), go)


def _piece_loudness(path: Path, ffmpeg: str):
    def go():
        errs: List[str] = []
        return _loudness(path, ffmpeg, errs), errs

    return _memo(path, ("loud",), go)


def _piece_spectrogram(path: Path, ffmpeg: str, duration: float, sr: Optional[int]):
    def go():
        errs: List[str] = []
        return _spectrogram(path, ffmpeg, duration, sr, errs), errs

    return _memo(path, ("sgram",), go)


# ───────────────────────── تابع اصلی ─────────────────────────


def analyze_audio(
    file_path: "str | os.PathLike[str]",
    *,
    cover_output_path: "Optional[str | os.PathLike[str]]" = None,
    deep_scan: bool = True,
    include_spectrum: bool = False,
    max_segments: int = 8,
    segment_seconds: float = 10.0,
    loudness: bool = True,
) -> Dict[str, Any]:
    """
    تحلیل کامل یک فایل صوتی. هیچ‌وقت exception پرتاب نمی‌کند؛ خطاها در result["errors"] می‌آیند.

    cover_output_path : اگر داده شود کاور آلبوم (در صورت وجود) در این مسیر ذخیره می‌شود.
    deep_scan         : False → تحلیل طیف/عمق بیت/لودنس انجام نمی‌شود (فقط متادیتا + مشخصات فنی).
    include_spectrum  : True → منحنی طیف (400 نقطه) هم در result["quality"]["spectrum"] می‌آید.
    max_segments      : حداکثر تعداد قطعه‌های نمونه‌برداری برای FFT.
    segment_seconds   : طول هر قطعه (ثانیه).
    loudness          : False → اندازه‌گیری لودنس EBU R128 (کل فایل) انجام نمی‌شود.
    """
    errors: List[str] = []
    result: Dict[str, Any] = {
        "ok": False,
        "file": {"path": str(file_path), "name": None, "stem": None, "size_bytes": None, "extension": None},
        "metadata": _empty_metadata(),
        "technical": {},
        "quality": {
            "analyzed": False, "verdict": "inconclusive", "confidence": "low",
            "reason_codes": [], "reasons": [], "notes": [], "cutoff_hz": None, "cliff_db": 0.0,
            "nyquist_hz": None, "estimated_class": None, "estimated_bitrate_kbps": None,
            "declared_bitrate_kbps": None, "band_levels_db": {}, "loudness": None,
            "bit_depth_check": None, "dynamics": None, "stereo": None, "clipping": None,
        },
        "message": "",
        "errors": errors,
    }
    try:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            errors.append("file not found")
            result["message"] = "❌ فایل پیدا نشد."
            return result
        result["file"].update(
            path=str(path), name=path.name, stem=path.stem,
            size_bytes=path.stat().st_size, extension=path.suffix.lower() or None,
        )
        try:
            ffmpeg, ffprobe = _resolve_binaries()
        except Exception as e:  # noqa: BLE001
            errors.append(f"binaries: {e}")
            result["message"] = "❌ ffmpeg/ffprobe پیدا نشد."
            return result

        # 1) متادیتا
        try:
            _fill_metadata(result["metadata"], path, Path(cover_output_path) if cover_output_path else None)
        except Exception as e:  # noqa: BLE001
            errors.append(f"metadata: {type(e).__name__}: {e}")

        # 2) مشخصات فنی
        try:
            tech = _probe(path, ffprobe)
        except subprocess.TimeoutExpired:
            errors.append("ffprobe: timeout")
            result["message"] = "❌ بررسی فایل بیش از حد طول کشید."
            return result
        except Exception as e:  # noqa: BLE001
            errors.append(f"ffprobe: {e}")
            result["message"] = "❌ این فایل رو نتونستم بخونم؛ مطمئنی فایل صوتیِ سالمه؟"
            return result
        tech["bitrate_mode"] = result["metadata"].get("bitrate_mode")
        result["technical"] = tech
        result["ok"] = True

        # 3) طیف فرکانسی (+ استریو، کلیپینگ، دینامیک)
        spec: Optional[Dict[str, Any]] = None
        bd: Optional[Dict[str, Any]] = None
        loud: Optional[Dict[str, float]] = None
        f_sg = None
        sr = tech.get("sample_rate")
        if not deep_scan:
            errors.append("spectrum: skipped (deep_scan=False)")
        elif (sr or 0) > MAX_NATIVE_SR or (tech.get("codec") or "").startswith("dsd"):
            errors.append("spectrum: unsupported sample rate / DSD")
        else:
            # هر چهار مرحله‌ی سنگین هم‌زمان شروع میشن (قبلاً پشت‌سرهم بودن)؛ زمان کل = کندترین مرحله
            seg_len = max(1.0, float(segment_seconds))
            dur = tech["duration"]
            f_spec = _TOP_POOL.submit(_piece_spectrum, path, ffmpeg, tech, max_segments, seg_len)
            f_bd = f_loud = f_sg = None
            # 4) عمق بیت واقعی (لاسلس صحیح‌عددی) و لودنس EBU R128
            if tech["is_lossless"] and (tech.get("sample_fmt") or "") not in FLOAT_FMTS:
                f_bd = _TOP_POOL.submit(_piece_bitdepth, path, ffmpeg, dur)
            if loudness:
                f_loud = _TOP_POOL.submit(_piece_loudness, path, ffmpeg)
            if include_spectrum:  # اسپکتروگرام فقط وقتی طیف معتبر بود استفاده میشه؛ ولی هم‌زمان شروع میشه تا منتظر نمونیم
                f_sg = _TOP_POOL.submit(_piece_spectrogram, path, ffmpeg, dur, sr)

            try:
                spec, errs = f_spec.result()
                errors.extend(errs)
                if not include_spectrum:
                    spec.pop("spectrum", None)  # منحنی طیف فقط وقتی خواسته شده باشه
                if not spec.get("analyzed"):
                    errors.append(f"spectrum: {spec.get('error')}")
            except Exception as e:  # noqa: BLE001
                spec = None
                log.exception("spectral analysis failed")
                errors.append(f"spectrum: {type(e).__name__}: {e}")
            if f_bd is not None:
                try:
                    bd, errs = f_bd.result()
                    errors.extend(errs)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"bitdepth: {type(e).__name__}: {e}")
            if f_loud is not None:
                try:
                    loud, errs = f_loud.result()
                    errors.extend(errs)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"loudness: {type(e).__name__}: {e}")

        sgram: Optional[Dict[str, Any]] = None
        if f_sg is not None:
            if spec and spec.get("analyzed"):
                try:
                    sgram, errs = f_sg.result()
                    errors.extend(errs)
                except Exception as e:  # noqa: BLE001
                    log.exception("spectrogram failed")
                    errors.append(f"spectrogram: {type(e).__name__}: {e}")
            else:
                f_sg.cancel()

        q = result["quality"]
        verdict = _judge(tech, spec, bd, loud)
        q.update(verdict)
        q["declared_bitrate_kbps"] = tech.get("bitrate_kbps")
        if spec:
            q.update({k: v for k, v in spec.items() if k not in ("error",)})
        q["bit_depth_check"] = bd
        q["loudness"] = loud
        q["spectrogram"] = sgram
        result["message"] = format_report(result)
    except Exception as e:  # noqa: BLE001 — آخرین خط دفاعی
        log.exception("analyze_audio crashed")
        errors.append(f"unexpected: {type(e).__name__}: {e}")
        if not result["message"]:
            result["message"] = "❌ خطای غیرمنتظره در تحلیل فایل."
    return result


async def analyze_audio_async(file_path: "str | os.PathLike[str]", **kwargs: Any) -> Dict[str, Any]:
    """نسخه‌ی async: کار سنگین در thread جدا اجرا می‌شود تا event loop بلاک نشود."""
    return await asyncio.to_thread(analyze_audio, file_path, **kwargs)



import colorsys
import io
import unicodedata

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features as _pil_features

    PIL_OK = True
except ImportError:  # بدون Pillow ربات با متن ساده کار می‌کنه
    PIL_OK = False

try:  # raqm = شکل‌دهی و راست‌به‌چپ داخل خود Pillow (نیاز به libraqm + fribidi)
    _RAQM = PIL_OK and bool(_pil_features.check("raqm")) and bool(_pil_features.check("fribidi"))
except Exception:  # noqa: BLE001
    _RAQM = False

_reshape = _get_display = None
if PIL_OK and not _RAQM:  # راه جایگزین: پکیج‌های خالص پایتون
    try:
        import arabic_reshaper as _ar

        _reshaper = _ar.ArabicReshaper({"delete_harakat": False, "support_ligatures": True})
        _reshape = _reshaper.reshape
        try:
            from bidi import get_display as _get_display  # python-bidi >= 0.5
        except ImportError:
            from bidi.algorithm import get_display as _get_display  # python-bidi قدیمی
    except Exception:  # noqa: BLE001
        _reshape = _get_display = None

CARD_W = 1000
FONT_DIR = Path(os.getenv("CARD_FONT_DIR") or (Path(__file__).resolve().parent / "fonts"))
FONT_URL_BASE = os.getenv(
    "CARD_FONT_URL_BASE", "https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/fonts/ttf"
).rstrip("/")
_REG_NAMES = ("Vazirmatn-Regular.ttf", "Vazir-Regular.ttf", "Vazir.ttf", "DejaVuSans.ttf", "NotoNaskhArabic-Regular.ttf")
_BOLD_NAMES = ("Vazirmatn-Bold.ttf", "Vazir-Bold.ttf", "Vazir-Bold-FD.ttf", "DejaVuSans-Bold.ttf", "NotoNaskhArabic-Bold.ttf")
_SYS_DIRS = (
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/noto", "/usr/share/fonts/truetype/freefont", "/Library/Fonts",
    "C:/Windows/Fonts",
)
_font_paths: Dict[bool, Optional[str]] = {}
_font_cache: Dict[Tuple[bool, int], Any] = {}


def _find_font(bold: bool) -> Optional[str]:
    env = os.getenv("CARD_FONT_BOLD" if bold else "CARD_FONT")
    if env and Path(env).is_file():
        return env
    names = _BOLD_NAMES if bold else _REG_NAMES
    for name in names:  # اولویت با ترتیب نام‌ها (Vazirmatn بهتره)، بعد پوشه‌ها
        for d in (FONT_DIR, Path("./fonts"), *map(Path, _SYS_DIRS)):
            p = d / name
            if p.is_file():
                return str(p)
    return None


def _font(size: int, bold: bool = False):
    key = (bold, size)
    if key not in _font_cache:
        if bold not in _font_paths:
            _font_paths[bold] = _find_font(bold) or _find_font(False)
        path = _font_paths[bold]
        if not path:
            raise FileNotFoundError("no font with Persian support found (put Vazirmatn-Regular.ttf / Bold in ./fonts)")
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def cards_available() -> bool:
    if not PIL_OK:
        return False
    try:
        _font(20)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_card_fonts() -> None:
    """اگه Vazirmatn کنار ربات نبود، یه‌بار تلاش می‌کنه از گیت‌هاب بگیره (خطا مهم نیست؛ فونت جایگزین استفاده میشه)."""
    import urllib.request

    try:
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf"):
            dst = FONT_DIR / name
            if dst.is_file() and dst.stat().st_size > 50_000:
                continue
            with urllib.request.urlopen(f"{FONT_URL_BASE}/{name}", timeout=25) as r:  # noqa: S310
                data = r.read()
            if len(data) < 50_000 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                raise ValueError("bad font file")
            dst.write_bytes(data)
            log.info("card font downloaded: %s", name)
        _font_paths.clear()
        _font_cache.clear()
    except Exception as e:  # noqa: BLE001
        log.info("card font download skipped: %s", e)


# ───────────────────────── متن: شکل‌دهی، راست‌به‌چپ، اندازه‌گیری ─────────────────────────


def _is_rtl(text: str) -> bool:
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            return True
        if b == "L":
            return False
    return False


_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:%+/\-→≈#<]*(?: [A-Za-z0-9][A-Za-z0-9.:%+/\-→≈#<]*)*")


def _ltr_runs(text: str) -> str:
    """تکه‌های لاتین/عددی داخل جمله‌ی فارسی (مثل «16.8 kHz») رو با LRM قفل می‌کنه تا ترتیبشون به‌هم نریزه"""
    return _LATIN_RUN.sub(lambda m: "\u200e" + m.group(0) + "\u200e", text)


def _shape(text: str, rtl: Optional[bool] = None) -> str:
    """فقط برای حالت بدون raqm لازمه: حروف فارسی رو بهم می‌چسبونه و ترتیب نمایش (RTL) رو درست می‌کنه"""
    if _RAQM or not text or _reshape is None:
        return text
    try:
        return _get_display(_reshape(text), base_dir="R" if rtl else None)
    except Exception:  # noqa: BLE001
        return text


def _tw(d, text: str, font) -> float:
    return d.textlength(_shape(text), font=font)


def _text(d, xy, text: str, font, fill, anchor: str = "la", rtl: Optional[bool] = None) -> None:
    """rtl=True: جمله‌ی فارسی (جهت پایه راست‌به‌چپ حتی اگه با کلمه‌ی لاتین شروع بشه)"""
    kw = {"direction": "rtl"} if (rtl and _RAQM) else {}
    if rtl and _is_rtl(text):
        text = _ltr_runs(text)
    d.text(xy, _shape(text, rtl), font=font, fill=fill, anchor=anchor, **kw)


def _wrap(d, text: str, font, max_w: float) -> List[str]:
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if _tw(d, trial, font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _ellipsize(d, text: str, font, max_w: float) -> str:
    if _tw(d, text, font) <= max_w:
        return text
    while text and _tw(d, text + "…", font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _fit_lines(d, text: str, plan: Tuple[Tuple[int, int], ...], max_w: float, bold: bool):
    """plan = ((اندازه، حداکثر خط), ...) از بزرگ به کوچک؛ اولین گزینه‌ای که جا بشه انتخاب میشه.
    اگه هیچ‌کدوم جا نشد، آخری با سه‌نقطه بریده میشه."""
    for size, max_lines in plan:
        f = _font(size, bold)
        lines = _wrap(d, text, f, max_w)
        if len(lines) <= max_lines and all(_tw(d, ln, f) <= max_w for ln in lines):
            return f, lines
    size, max_lines = plan[-1]
    f = _font(size, bold)
    full = _wrap(d, text, f, max_w)
    lines = full[:max_lines]
    if len(full) > max_lines:
        lines[-1] = lines[-1] + " …"
    return f, [_ellipsize(d, ln, f, max_w) for ln in lines]


# ───────────────────────── بنر پیش‌فرض تبلیغات («جای تبلیغات شما») ─────────────────────────

AD_W, AD_H = 1280, 640
AD_TEXT = "جای تبلیغات شما"
AD_HANDLE = os.getenv("AD_BANNER_HANDLE", "@TheAudioBoxBot").strip()


def _rrect_path(x0: float, y0: float, x1: float, y1: float, r: float, step: float = 2.0) -> List[Tuple[float, float]]:
    """نقطه‌های مسیر یه مستطیل گوشه‌گرد، پشت‌سرهم (برای کشیدن خط‌چین)"""
    pts: List[Tuple[float, float]] = []

    def line(ax, ay, bx, by):
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        pts.extend((ax + (bx - ax) * i / n, ay + (by - ay) * i / n) for i in range(n))

    def arc(cx, cy, a0, a1):
        n = max(2, int(abs(a1 - a0) * math.pi / 180 * r / step))
        pts.extend(
            (cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
            for i in range(n)
        )

    line(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0 + r, -90, 0)
    line(x1, y0 + r, x1, y1 - r)
    arc(x1 - r, y1 - r, 0, 90)
    line(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1 - r, 90, 180)
    line(x0, y1 - r, x0, y0 + r)
    arc(x0 + r, y0 + r, 180, 270)
    return pts


def render_default_ad() -> Optional[bytes]:
    """بنر افقی پیش‌فرض (JPEG) با نوشته‌ی «جای تبلیغات شما»؛ بدون Pillow یا فونت فارسی None برمی‌گردونه."""
    if not PIL_OK:
        return None
    try:
        import random

        W, H = AD_W, AD_H
        # پس‌زمینه: گرادیان مورب + درخشش مرکزی
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        t = (xx / W * 0.55 + yy / H * 0.45)[..., None]
        c0, c1 = np.array([9, 13, 36], np.float32), np.array([62, 26, 116], np.float32)
        arr = c0 + (c1 - c0) * t
        rad = np.hypot(xx - W / 2, (yy - H / 2) * 1.5) / (W * 0.5)
        arr = arr + (np.clip(1 - rad, 0, 1) ** 2)[..., None] * np.array([34, 26, 80], np.float32)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

        # موج صوتی (اکولایزر) نیمه‌شفاف پشت متن
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        rnd = random.Random(7)
        bar_w, gap = 14, 12
        n = W // (bar_w + gap)
        x = (W - n * (bar_w + gap) + gap) // 2
        for i in range(n):
            env = math.sin(math.pi * (i + 0.5) / n) ** 0.8  # وسط بلندتر
            amp = (0.25 + 0.75 * rnd.random()) * env
            h = max(10, int(amp * H * 0.55))
            col = (255, 255, 255, 30) if i % 2 else (140, 190, 255, 40)
            ld.rounded_rectangle((x, H // 2 - h // 2, x + bar_w, H // 2 + h // 2), radius=bar_w // 2, fill=col)
            x += bar_w + gap
        img = Image.alpha_composite(img, layer)

        # کادر خط‌چین (جای خالی تبلیغ)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        pts = _rrect_path(34, 34, W - 34, H - 34, 36)
        dash, space = 11, 7  # بر حسب تعداد نقطه‌ی مسیر (هر نقطه ≈ 2px)
        i = 0
        while i < len(pts):
            seg = pts[i : i + dash + 1]
            if len(seg) > 1:
                ld.line(seg, fill=(255, 255, 255, 120), width=4, joint="curve")
            i += dash + space
        img = Image.alpha_composite(img, layer)

        # متن اصلی با سایه‌ی نرم
        probe = ImageDraw.Draw(img)
        size = 150
        while size > 60 and _tw(probe, AD_TEXT, _font(size, True)) > W - 260:
            size -= 6
        f_main = _font(size, True)
        cx, cy = W / 2, H / 2 - 18
        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _text(ImageDraw.Draw(shadow), (cx, cy + 8), AD_TEXT, f_main, (0, 0, 0, 190), anchor="mm", rtl=True)
        img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(12)))
        d = ImageDraw.Draw(img)
        _text(d, (cx, cy), AD_TEXT, f_main, (255, 255, 255, 255), anchor="mm", rtl=True)

        # خط تاکید زیر متن
        bar_y = int(cy + size * 0.78)
        d.rounded_rectangle((W // 2 - 90, bar_y, W // 2 + 90, bar_y + 8), radius=4, fill=(255, 196, 61, 255))

        # آیدی ربات پایین بنر
        if AD_HANDLE:
            _text(d, (W / 2, H - 82), AD_HANDLE, _font(36), (196, 206, 236, 255), anchor="mm")

        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        log.warning("ساخت بنر پیش‌فرض ناموفق بود: %s", e)
        return None



# ───────────────────────── امضای فرکانسی (Spectral Watermark): DSP ─────────────────────────

_WM_LOSSLESS = frozenset({
    "flac", "alac", "wav", "wave", "aiff", "aif", "w64", "wavpack", "ape", "tta", "mlp", "truehd", "wmalossless",
})
_WM_HQ_MIN_KBPS = {"mp3": 256, "aac": 256, "vorbis": 224, "opus": 128}  # از این بیت‌ریت به بالا = «کیفیت بالا»
_WM_ROW_HZ = 45.0                      # فاصله‌ی تقریبی بین خط‌های سینوسی (هر خط = یک ردیف پیکسل)
_WM_HOP_SEC = 0.02                     # گام زمانی ستون‌های تصویر (۲۰ms)
_WM_CEIL = 10.0 ** (-0.1 / 20.0)       # سقف امضا: -0.1 dBFS (امضا هیچ‌وقت مجموع رو از این بالاتر نمی‌بره)
_WM_LIMIT = 0.999                      # مجانب Soft Clip؛ خروجی نهایی هرگز به 1.0 نمی‌رسد


def _wm_plan(sr: int, codec: str, bitrate_kbps: int) -> Tuple[float, float, float]:
    """(f_low, f_high, gain_dbfs) تطبیقی با نوع فایل و سمپل‌ریت.
    gain = دامنه‌ی هر خط سینوسی در پیکسل سفید (همان چیزی که در Spek/Audacity دیده می‌شود)."""
    c = (codec or "").strip().lower()
    kb = int(bitrate_kbps or 0)
    nyq = sr / 2.0
    if c.startswith(("pcm_", "dsd")) or c in _WM_LOSSLESS:  # بدون افت
        if sr >= 88200:  # های‌رز
            lo, hi, g = 22000.0, 35000.0, -45.0
        else:
            lo, hi, g = 18500.0, 21500.0, -46.0
    elif kb >= _WM_HQ_MIN_KBPS.get(c, 256):  # فشرده‌ی باکیفیت (MP3 320 / AAC 256)
        lo, hi, g = 16500.0, 19500.0, -48.0
    else:  # بیت‌ریت پایین‌تر: زیر کات‌اف لو‌پس انکودر (۱۲۸k ≈ 15.5k ، ۱۹۲k+ ≈ 16.5k)
        t = min(max((kb - 128) / 64.0, 0.0), 1.0)
        lo, hi, g = 14000.0, 15500.0 + 1000.0 * t, -50.0
    hi = min(hi, nyq * 0.975)  # هرگز نزدیک/بالاتر از نایکوئیست نرود (سمپل‌ریت‌های کم)
    lo = max(1000.0, min(lo, hi - 1500.0))
    return lo, hi, g


def _wm_text_image(text: str, rows: int, cols: int) -> "np.ndarray":
    """متن/آیدی → تصویر مونوکروم (سفید روی سیاه) با ابعاد rows×cols؛ ردیف صفر = بالاترین فرکانس.
    فونت و شکل‌دهی/راست‌به‌چپ همان زیرساخت کارت‌های پروژه است (_font/_text). خروجی float32 در [0..1]."""
    if not PIL_OK:
        raise RuntimeError("Pillow is required for the spectral watermark")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
    if not lines:
        raise ValueError("empty watermark text")
    size = 160  # اندازه‌ی کار بزرگ؛ بعداً با LANCZOS کوچک میشه (لبه‌های نرم)
    font = _font(size, True)  # Bold: خطوط ضخیم‌تر = خواناتر در طیف‌نگار
    probe = ImageDraw.Draw(Image.new("L", (8, 8), 0))
    crops = []
    for ln in lines:
        w = int(_tw(probe, ln, font)) + size * 2
        im = Image.new("L", (w, size * 3), 0)
        _text(ImageDraw.Draw(im), (w / 2, size * 1.5), ln, font, 255, "mm", rtl=_is_rtl(ln))
        box = im.getbbox()
        if box:
            crops.append(im.crop(box))  # برش دقیقِ جوهر؛ فونت‌متریک مهم نیست
    if not crops:
        raise ValueError("watermark text has no visible glyphs")
    gap = int(max(c.height for c in crops) * 0.30)
    W = max(c.width for c in crops)
    H = sum(c.height for c in crops) + gap * (len(crops) - 1)
    px, py = int(W * 0.04) + 4, int(H * 0.10) + 4
    canvas = Image.new("L", (W + 2 * px, H + 2 * py), 0)
    y = py
    for c in crops:
        canvas.paste(c, (px + (W - c.width) // 2, y))
        y += c.height + gap
    canvas = canvas.point(lambda p: 255 if p >= 110 else 0)  # سیاه‌وسفید واقعی
    resample = getattr(Image, "Resampling", Image).LANCZOS
    a = np.asarray(canvas.resize((cols, rows), resample), dtype=np.float32) / 255.0
    return np.clip((a - 0.04) / 0.92, 0.0, 1.0)  # حذف ringing لبه‌ها


def _wm_soft_clip(x: "np.ndarray", ceil: float = _WM_CEIL) -> "np.ndarray":
    """فقط نمونه‌های بالاتر از ceil را با tanh نرم فشرده می‌کند (مشتق پیوسته)؛ in-place. خروجی ≤ _WM_LIMIT"""
    a = np.abs(x)
    over = a > ceil
    if over.any():
        knee = _WM_LIMIT - ceil
        x[over] = np.sign(x[over]) * (ceil + knee * np.tanh((a[over] - ceil) / knee))
    return x


def generate_spectral_watermark(
    pcm: "np.ndarray", sr: int, duration: float, text: str, codec: str, bitrate_kbps: int,
    *, start: Optional[float] = None, gain_dbfs: Optional[float] = None, inplace: bool = False,
) -> "np.ndarray":
    """
    متن/آیدی را به‌صورت تصویر روی طیف آهنگ می‌نشاند (سنتز جمعی سینوسی، هر ردیف پیکسل = یک سینوسی).

    pcm        : شکل (نمونه، کانال) یا (نمونه,) ؛ float [-1..1] (خروجیِ _decode_pcm). ورودی تغییر نمی‌کند.
    duration   : مدت زمانی (ثانیه) که متن روی آن پهن می‌شود؛ ≤0 یا بزرگ‌تر از طول آهنگ = کل آهنگ.
    start      : شروع (ثانیه)؛ None = وسط آهنگ (اگر duration کوتاه‌تر از آهنگ باشد).
    gain_dbfs  : جایگزین دامنه‌ی پیش‌فرضِ فرمت (به بازه‌ی [-60, -40] محدود میشود).
    خروجی      : float32 هم‌شکل ورودی؛ پیک‌ها کنترل‌شده (< 0 dBFS). کار CPU-bound است → با asyncio.to_thread صدا بزن.
    """
    sr = int(sr)
    if sr <= 0:
        raise ValueError("invalid sample rate")
    text = (text or "").strip()[:120]
    if not text:
        raise ValueError("empty watermark text")

    x = np.asarray(pcm)
    if np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float32) / float(np.iinfo(x.dtype).max)
    mono_in = x.ndim == 1
    if x.ndim not in (1, 2):
        raise ValueError("pcm must be shaped (samples,) or (samples, channels)")
    if inplace and x.dtype == np.float32:
        # روی همون بافر (مثلاً np.memmap روی دیسک) کار میکنه؛ بدون کپی کاملِ آهنگ توی رم
        out = x.reshape(-1, 1) if mono_in else x
        for i in range(0, out.shape[0], 1 << 20):
            np.nan_to_num(out[i:i + (1 << 20)], copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    else:
        out = np.array(x.reshape(-1, 1) if mono_in else x, dtype=np.float32, copy=True)
        out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    n_total = out.shape[0]
    if n_total < sr:  # کمتر از ۱ ثانیه: متن خوانا نمی‌شود
        return out[:, 0] if mono_in else out

    # ── بازه‌ی زمانی امضا
    total_sec = n_total / sr
    span = float(duration) if (duration and math.isfinite(float(duration)) and duration > 0) else total_sec
    span = max(1.0, min(span, total_sec))
    n_wm = int(span * sr)
    if start is None:
        s0 = (n_total - n_wm) // 2
    else:
        s0 = int(min(max(float(start), 0.0) * sr, n_total - n_wm))

    # ── بازه‌ی فرکانسی و دامنه (تطبیقی با فرمت)
    lo, hi, g = _wm_plan(sr, codec, bitrate_kbps)
    if gain_dbfs is not None:
        g = float(np.clip(gain_dbfs, -60.0, -40.0))
    amp = 10.0 ** (g / 20.0)
    rows = int(np.clip(round((hi - lo) / _WM_ROW_HZ), 36, 128))
    freqs = hi - (np.arange(rows) + 0.5) * (hi - lo) / rows  # ردیف ۰ = بالاترین فرکانس
    log.debug("spectral wm: %.0f-%.0f Hz, %.0f dBFS, rows=%d, span=%.1fs", lo, hi, g, rows, span)

    # ── تصویر → پوشش دامنه‌ی هر خط در طول زمان
    hop = max(1, int(round(sr * _WM_HOP_SEC)))  # نمونه در هر ستون
    blocks = max(1, 16384 // hop)  # ستون در هر chunk
    chunk = hop * blocks
    n_chunks = -(-n_wm // chunk)
    n_cols = -(-n_wm // hop) + 1
    env = _wm_text_image(text, rows, n_cols) * np.float32(amp)
    nf = min(max(2, int(round(0.06 / _WM_HOP_SEC))), n_cols // 2)  # فید ~۶۰ms دو سر (بدون کلیک)
    ramp = (np.sin(np.linspace(0.0, np.pi / 2, nf)) ** 2).astype(np.float32)
    env[:, :nf] *= ramp
    env[:, -nf:] *= ramp[::-1]
    need = n_chunks * blocks + 1
    if need > env.shape[1]:
        env = np.pad(env, ((0, 0), (0, need - env.shape[1])))

    # ── جدول چرخشی یک‌بارمصرف (به‌جای sin برای هر chunk) + فاز تصادفیِ ثابت (از روی متن)
    rng = np.random.default_rng(int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16))
    phi0 = rng.uniform(0.0, 2.0 * np.pi, rows)  # فاز تصادفی: جلوگیری از پالس‌قطار (فرکانس‌های هم‌فاصله)
    wr = 2.0 * np.pi * freqs / sr  # rad/sample
    ang = np.outer(wr, np.arange(chunk, dtype=np.float64))
    cos_t, sin_t = np.cos(ang).astype(np.float32), np.sin(ang).astype(np.float32)
    del ang
    wgt = ((np.arange(chunk) % hop) / hop).astype(np.float32)  # درون‌یابی خطی بین ستون‌ها
    iw = np.float32(1.0) - wgt

    # ── سنتز chunk به chunk + ادغام با کنترل پیک
    for ci in range(n_chunks):
        n0, n1 = ci * chunk, min((ci + 1) * chunk, n_wm)
        c0 = ci * blocks
        e0, e1 = env[:, c0:c0 + blocks], env[:, c0 + 1:c0 + blocks + 1]
        act = np.flatnonzero((e0.max(axis=1) > 1e-5) | (e1.max(axis=1) > 1e-5))  # فقط خط‌های روشن
        if act.size == 0:
            continue
        e = np.repeat(e0[act], hop, axis=1) * iw + np.repeat(e1[act], hop, axis=1) * wgt
        ph = phi0[act] + wr[act] * n0  # float64 → بدون خطای فاز در آهنگ‌های طولانی
        a = np.cos(ph).astype(np.float32)[:, None]
        b = np.sin(ph).astype(np.float32)[:, None]
        sig = np.einsum("kc,kc->c", e, a * cos_t[act] - b * sin_t[act])[: n1 - n0]  # Σ A_k·cos(ω_k n + φ_k)

        seg = out[s0 + n0:s0 + n1]  # view
        # امضا فقط تا سقف -0.1dBFS اضافه می‌شود؛ موسیقی اصلی دست‌نخورده می‌ماند
        add = np.clip(sig[:, None], np.minimum(-_WM_CEIL - seg, 0.0), np.maximum(_WM_CEIL - seg, 0.0))
        # نمونه‌هایی که خودِ آهنگ از قبل بالای سقف بوده‌اند (اوورِ دیکودِ MP3/AAC) اصلاً امضا نمی‌گیرند
        add[np.abs(seg) > _WM_CEIL] = 0.0
        seg += add

    # soft-clip سراسری عمداً برداشته شد: روی پیک‌های خودِ آهنگ هم اثر می‌گذاشت (تغییر موج اصلی).
    # حالا هر نمونه یا دقیقاً مثل اصل است یا اصل + امضا (با سقف -0.1dBFS).
    return out[:, 0] if mono_in else out


# ───────────────────────── رنگ و پس‌زمینه ─────────────────────────


def _rgb(h: float, s: float, l: float) -> Tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0, min(1, l)), max(0, min(1, s)))
    return int(r * 255), int(g * 255), int(b * 255)


def _seed_hue(seed: str) -> float:
    return int(hashlib.md5(seed.encode("utf-8", "ignore")).hexdigest()[:4], 16) / 65535.0


def _open_cover(path: Optional[Path]):
    if not path:
        return None
    try:
        im = Image.open(path)
        im.load()
        return ImageOps.exif_transpose(im).convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def _accent(cover, seed: str) -> Tuple[int, int, int]:
    """رنگ شاخص: از کاور (پر‌رنگ‌ترین فام) یا از روی نام آهنگ"""
    hue, sat = _seed_hue(seed), 0.62
    if cover is not None:
        small = cover.resize((32, 32))
        sx = sy = wsum = ssum = 0.0
        for r, g, b in small.getdata():
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            w = s * v * v
            sx += math.cos(h * 2 * math.pi) * w
            sy += math.sin(h * 2 * math.pi) * w
            wsum += w
            ssum += s * w
        if wsum > 3.0:
            hue = (math.atan2(sy, sx) / (2 * math.pi)) % 1.0
            sat = min(0.85, max(0.5, ssum / wsum))
    return _rgb(hue, sat, 0.62)


def _mix(a, b, t: float) -> Tuple[int, int, int]:
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))  # type: ignore[return-value]


def _gradient(w: int, h: int, c0, c1, diagonal: bool = True):
    """گرادیان خطی سریع (با numpy اگه باشه)"""
    try:
        import numpy as np

        yy, xx = np.mgrid[0:h, 0:w].astype("float32")
        t = ((xx / w) * 0.55 + (yy / h) * 0.45) if diagonal else (yy / h)
        arr = np.empty((h, w, 3), dtype="uint8")
        for i in range(3):
            arr[..., i] = (c0[i] * (1 - t) + c1[i] * t).astype("uint8")
        return Image.fromarray(arr, "RGB")
    except Exception:  # noqa: BLE001
        base = Image.new("RGB", (w, h), c0)
        top = Image.new("RGB", (w, h), c1)
        mask = Image.linear_gradient("L").resize((w, h))
        return Image.composite(top, base, mask)


def _background(w: int, h: int, cover, accent) -> "Image.Image":
    dark = _mix(accent, (8, 10, 20), 0.82)
    darker = _mix(accent, (4, 5, 12), 0.93)
    if cover is not None:
        bg = ImageOps.fit(cover, (w, h), Image.LANCZOS)
        bg = bg.resize((max(1, w // 6), max(1, h // 6)), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9))
        bg = bg.resize((w, h), Image.BICUBIC)
        shade = _gradient(w, h, dark, darker)
        bg = Image.blend(bg, shade, 0.62)
    else:
        bg = _gradient(w, h, dark, darker)
    img = bg.convert("RGB")
    # درخشش رنگی نرم گوشه‌ها
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((w * 0.55, -h * 0.55, w * 1.25, h * 0.45), fill=accent + (70,))
    gd.ellipse((-w * 0.3, h * 0.65, w * 0.35, h * 1.5), fill=accent + (40,))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    _comp(img, glow, (0, 0))
    return img


# ───────────────────────── اشکال پایه ─────────────────────────


# کارت گزارش دو بار کشیده میشه: یه بار فقط برای اندازه‌گیری ارتفاع، یه بار واقعی. توی پاس اندازه‌گیری، عملیات پیکسلیِ سنگین
# (گوشه‌گرد، سایه، چسبوندن) هیچ اثری روی چیدمان/ارتفاع نداره، پس رد میشن (خروجی نهایی بایت‌به‌بایت همونه).
_MEASURE = threading.local()


def _measuring() -> bool:
    return getattr(_MEASURE, "on", False)


def _comp(img, layer, xy) -> None:
    """layer (RGBA) رو روی img (RGB یا RGBA) با شفافیت درست می‌چسبونه"""
    if _measuring():
        return
    xy = (int(xy[0]), int(xy[1]))
    if img.mode == "RGBA":
        img.alpha_composite(layer, xy)
    else:
        img.paste(layer.convert("RGB"), xy, layer.getchannel("A"))


def _rrect(img, box, r: int, fill) -> None:
    """مستطیل گوشه‌گرد با لبه‌ی نرم (anti-alias)؛ fill = (r,g,b[,a])"""
    if _measuring():
        return
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    S = 3
    m = Image.new("L", (w * S, h * S), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * S - 1, h * S - 1), min(r, w // 2, h // 2) * S, fill=255)
    m = m.resize((w, h), Image.LANCZOS)
    if len(fill) == 4 and fill[3] < 255:
        a = fill[3]
        m = m.point(lambda v: v * a // 255)
    layer = Image.new("RGBA", (w, h), tuple(fill[:3]) + (255,))
    layer.putalpha(m)
    _comp(img, layer, (x0, y0))


def _paste_rounded(img, tile, xy, r: int) -> None:
    if _measuring():
        return
    w, h = tile.size
    S = 3
    m = Image.new("L", (w * S, h * S), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * S - 1, h * S - 1), r * S, fill=255)
    m = m.resize((w, h), Image.LANCZOS)
    t = tile.convert("RGBA")
    t.putalpha(m)
    _comp(img, t, xy)


def _shadow(img, box, r: int, blur: int = 22, alpha: int = 150, dy: int = 10) -> None:
    if _measuring():
        return
    x0, y0, x1, y1 = box
    pad = blur * 3
    layer = Image.new("RGBA", (int(x1 - x0) + pad * 2, int(y1 - y0) + pad * 2), (0, 0, 0, 0))
    _rrect(layer, (pad, pad, pad + (x1 - x0), pad + (y1 - y0)), r, (0, 0, 0, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    _comp(img, layer, (int(x0) - pad, int(y0) - pad + dy))


def _note_icon(d, cx: float, cy: float, s: float, fill) -> None:
    """نت موسیقی ساده برای وقتی که آهنگ کاور نداره"""
    hr = s * 0.17
    lx, rx = cx - s * 0.2, cx + s * 0.2
    ly = ry = cy + s * 0.24
    sw = max(2, int(s * 0.055))
    d.ellipse((lx - hr * 1.15, ly - hr * 0.85, lx + hr * 1.15, ly + hr * 0.85), fill=fill)
    d.ellipse((rx - hr * 1.15, ry - hr * 0.85, rx + hr * 1.15, ry + hr * 0.85), fill=fill)
    d.rectangle((lx + hr * 0.75, cy - s * 0.3, lx + hr * 0.75 + sw, ly), fill=fill)
    d.rectangle((rx + hr * 0.75, cy - s * 0.38, rx + hr * 0.75 + sw, ry), fill=fill)
    d.polygon([(lx + hr * 0.75, cy - s * 0.3), (rx + hr * 0.75 + sw, cy - s * 0.38),
               (rx + hr * 0.75 + sw, cy - s * 0.22), (lx + hr * 0.75, cy - s * 0.14)], fill=fill)


def _art_tile(cover, size: int, accent, seed: str):
    if cover is not None:
        return ImageOps.fit(cover, (size, size), Image.LANCZOS)
    c0 = _mix(accent, (255, 255, 255), 0.10)
    c1 = _rgb(_seed_hue(seed) + 0.12, 0.6, 0.30)
    tile = _gradient(size, size, c0, c1).convert("RGB")
    ov = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for i, a in enumerate((40, 28, 18)):  # حلقه‌های نیمه‌شفاف مثل صفحه‌ی گرامافون
        rr = size * (0.46 - i * 0.11)
        d.ellipse((size / 2 - rr, size / 2 - rr, size / 2 + rr, size / 2 + rr), outline=(255, 255, 255, a), width=3)
    _note_icon(d, size / 2, size / 2, size * 0.5, (255, 255, 255, 235))
    _comp(tile, ov, (0, 0))
    return tile


def _to_jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92, subsampling=0, optimize=True)
    return buf.getvalue()


# ───────────────────────── اعداد (همه لاتین) ─────────────────────────


def _fmt_size(nbytes: float) -> Tuple[str, str]:
    mb_ = nbytes / (1024 * 1024)
    if mb_ >= 1000:
        return f"{mb_ / 1024:.2f}", "GB"
    return f"{mb_:.1f}", "MB"


def _fmt_time(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _quality_level(kbps: int) -> Tuple[int, str]:
    if kbps >= 128:
        return 5, "عالی"
    if kbps >= 96:
        return 4, "خیلی خوب"
    if kbps >= 64:
        return 3, "خوب"
    if kbps >= 40:
        return 2, "قابل‌قبول"
    return 1, "پایین"


def _stat_tile(img, d, box, label: str, value: str, unit: str, accent) -> None:
    x0, y0, x1, y1 = box
    _rrect(img, box, 26, (255, 255, 255, 30))
    _rrect(img, (x0 + 26, y0, x1 - 26, y0 + 5), 3, accent + (255,))
    cx = (x0 + x1) / 2
    _text(d, (cx, y0 + 38), label, _font(26), (255, 255, 255, 175), "mm")
    fv, fu = _font(52, True), _font(26)
    wv = _tw(d, value, fv)
    wu = _tw(d, unit, fu) if unit else 0
    gap = 10 if unit else 0
    left = cx - (wv + gap + wu) / 2
    base = y0 + 108
    _text(d, (left, base), value, fv, (255, 255, 255, 255), "ls")
    if unit:
        _text(d, (left + wv + gap, base), unit, fu, (255, 255, 255, 180), "ls")


# ───────────────────────── کارت 1: کاور اطلاعات آهنگ ─────────────────────────


def render_info_card(
    title: str, artist: str, size_bytes: float, duration: float, kbps: int,
    cover_path: Optional[Path] = None, ext: str = "", verdict: Optional[str] = None,
) -> bytes:
    W, H = CARD_W, 604
    seed = f"{title}|{artist}"
    cover = _open_cover(cover_path)
    accent = _accent(cover, seed)
    img = _background(W, H, cover, accent)
    d = ImageDraw.Draw(img, "RGBA")

    pad, art = 48, 340
    ax0, ay0 = pad, pad
    _shadow(img, (ax0, ay0, ax0 + art, ay0 + art), 34)
    _paste_rounded(img, _art_tile(cover, art, accent, seed), (ax0, ay0), 34)
    # برچسب فرمت روی کاور
    ext = (ext or "").strip(". ").upper()[:5]
    if ext:
        f = _font(24, True)
        bw = _tw(d, ext, f) + 36
        _rrect(img, (ax0 + 16, ay0 + art - 58, ax0 + 16 + bw, ay0 + art - 16), 21, (0, 0, 0, 150))
        _text(d, (ax0 + 16 + bw / 2, ay0 + art - 37), ext, f, (255, 255, 255, 255), "mm")

    # ستون متن
    tx0, tx1 = ax0 + art + 44, W - pad
    tw = tx1 - tx0
    t_rtl = _is_rtl(title)
    f_title, lines = _fit_lines(d, title or "Unknown", ((58, 1), (50, 2), (42, 2), (36, 3)), tw, True)
    lh = int(f_title.size * 1.42)
    y = ay0 + 8
    for ln in lines:
        if t_rtl:
            _text(d, (tx1, y), ln, f_title, (255, 255, 255, 255), "ra")
        else:
            _text(d, (tx0, y), ln, f_title, (255, 255, 255, 255), "la")
        y += lh
    if artist:
        a_rtl = _is_rtl(artist)
        f_art = _font(36)
        art_txt = _ellipsize(d, artist, f_art, tw)
        y += 6
        if a_rtl:
            _text(d, (tx1, y), art_txt, f_art, accent + (255,), "ra")
        else:
            _text(d, (tx0, y), art_txt, f_art, accent + (255,), "la")

    # نوار کیفیت (پایین ستون متن، هم‌راستا با پایین کاور)
    lvl, word = _quality_level(kbps)
    bar_on = accent + (255,)
    qtxt = f"کیفیت: {word}"
    # اگه بررسی طیفی گفته فایل آپ‌اسکیله، کارت نباید «عالی» نشون بده (بیت‌ریت تنها گول‌زنکه)
    if verdict == "fake":
        lvl, qtxt, bar_on = min(lvl, 2), "آپ‌اسکیل‌شده", _VERDICT_STYLE["fake"][1] + (255,)
    elif verdict == "suspicious":
        lvl, qtxt, bar_on = min(lvl, 3), "مشکوک به آپ‌اسکیل", _VERDICT_STYLE["suspicious"][1] + (255,)
    bar_w, bar_h, gap = 42, 14, 8
    my = ay0 + art - 30
    total_bars = 5 * bar_w + 4 * gap
    fq = _font(28)
    for size in (28, 26, 24, 22):  # متن بلند نباید روی کاور یا لبه‌ی کارت بیفته
        fq = _font(size)
        if total_bars + 18 + _tw(d, qtxt, fq) <= tw:
            break
    rtl_col = t_rtl or (not title and _is_rtl(artist))
    bx0 = tx1 - total_bars if rtl_col else tx0
    for i in range(5):
        x = bx0 + i * (bar_w + gap)
        col = bar_on if i < lvl else (255, 255, 255, 45)
        _rrect(img, (x, my, x + bar_w, my + bar_h), 7, col)
    if rtl_col:
        _text(d, (bx0 - 18, my + bar_h / 2), qtxt, fq, (255, 255, 255, 215), "rm")
    else:
        _text(d, (bx0 + total_bars + 18, my + bar_h / 2), qtxt, fq, (255, 255, 255, 215), "lm")

    # ردیف آمار
    sy0, sy1 = ay0 + art + 34, H - pad + 6
    gap = 22
    tile_w = (W - 2 * pad - 2 * gap) / 3
    sv, su = _fmt_size(size_bytes)
    tiles = [("حجم فایل", sv, su), ("مدت زمان", _fmt_time(duration), ""), ("بیت‌ریت", str(int(kbps)), "kbps")]
    for i, (lab, val, unit) in enumerate(tiles):
        x0 = pad + i * (tile_w + gap)
        _stat_tile(img, d, (x0, sy0, x0 + tile_w, sy1), lab, val, unit, accent)
    return _to_jpeg(img)


# ───────────────────────── کارت 2: گزارش تحلیل کیفیت ─────────────────────────
# از این نام‌ها در بخش تحلیل صوت (بالاتر در همین فایل) استفاده میشه: _TIER_FA, SHARP_WIDTH_HZ,
# GENTLE_WIDTH_HZ, CLIP_RATIO_WARN

_VERDICT_STYLE = {
    "genuine": ("کیفیت واقعی و سالم", (52, 199, 128)),
    "suspicious": ("مشکوک به آپ‌اسکیل", (245, 166, 35)),
    "fake": ("فیک / آپ‌اسکیل‌شده", (239, 68, 68)),
    "inconclusive": ("تحلیل طیفی قطعی نبود", (140, 152, 175)),
}
_CONF_TXT = {"high": "بالا", "medium": "متوسط", "low": "کم"}
_WHITE = (255, 255, 255, 255)
_MUTED = (255, 255, 255, 165)


def _verdict_icon(kind: str, size: int, color) -> "Image.Image":
    S = 4
    n = size * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, n - 1, n - 1), fill=color + (255,))
    w = int(n * 0.09)
    wh = (255, 255, 255, 255)
    if kind == "genuine":
        pts = [(n * 0.28, n * 0.52), (n * 0.44, n * 0.68), (n * 0.74, n * 0.34)]
        d.line(pts, fill=wh, width=w, joint="curve")
        for p in (pts[0], pts[-1]):
            d.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=wh)
    elif kind == "fake":
        a, b = n * 0.32, n * 0.68
        d.line([(a, a), (b, b)], fill=wh, width=w)
        d.line([(a, b), (b, a)], fill=wh, width=w)
        for p in ((a, a), (b, b), (a, b), (b, a)):
            d.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=wh)
    elif kind == "suspicious":
        d.rounded_rectangle((n * 0.455, n * 0.24, n * 0.545, n * 0.58), w // 2, fill=wh)
        d.ellipse((n * 0.45, n * 0.67, n * 0.55, n * 0.77), fill=wh)
    else:
        d.text((n / 2, n * 0.53), "?", font=_font(int(n * 0.62), True), fill=wh, anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


def _heading(img, d, y: int, text: str, accent, x0: int, x1: int) -> int:
    f = _font(32, True)
    _rrect(img, (x1 - 8, y + 4, x1, y + 40), 4, accent + (255,))
    _text(d, (x1 - 24, y + 22), text, f, _WHITE, "rm", rtl=True)
    return y + 62


def _para_rtl(d, x1: float, y: int, text: str, font, fill, max_w: float, lh: int, dot=None) -> int:
    """بند راست‌چین؛ dot = رنگ بولت (سمت راست)"""
    indent = 30 if dot else 0
    lines = _wrap(d, text, font, max_w - indent)
    for i, ln in enumerate(lines):
        if dot and i == 0:
            cy = y + lh * 0.5
            d.ellipse((x1 - 12, cy - 6, x1, cy + 6), fill=dot + (255,))
        _text(d, (x1 - indent, y), ln, font, fill, "ra", rtl=True)
        y += lh
    return y


def _kv_tiles(img, d, y: int, items: List[Tuple[str, str, Optional[Tuple[int, int, int]]]], x0: int, x1: int,
              accent, cols: int = 3, h: int = 112, gap: int = 16) -> int:
    """کاشی‌های برچسب/مقدار؛ ترتیب از راست به چپ (RTL)"""
    tw_ = (x1 - x0 - gap * (cols - 1)) / cols
    for i, (label, value, color) in enumerate(items):
        r, c = divmod(i, cols)
        bx1 = x1 - c * (tw_ + gap)
        bx0 = bx1 - tw_
        by0 = y + r * (h + gap)
        _rrect(img, (bx0, by0, bx1, by0 + h), 24, (255, 255, 255, 28))
        _text(d, ((bx0 + bx1) / 2, by0 + 30), label, _font(23), _MUTED, "mm", rtl=True)
        mw = tw_ - 28
        lines: Optional[List[str]] = None
        f = _font(34, True)
        for sz in (34, 30, 27, 24, 21):  # اول کوچیک‌ترش میکنیم تا تک‌خطی جا بشه
            f = _font(sz, True)
            if _tw(d, value, f) <= mw:
                lines = [value]
                break
        if lines is None:  # بعد دو خطی (بدون بریدن)
            for sz in (24, 21):
                f = _font(sz, True)
                wrapped = _wrap(d, value, f, mw)
                if len(wrapped) <= 2 and all(_tw(d, ln, f) <= mw for ln in wrapped):
                    lines = wrapped
                    break
        if lines is None:  # آخرین راه: سه‌نقطه
            f = _font(21, True)
            wrapped = _wrap(d, value, f, mw)
            lines = [_ellipsize(d, ln, f, mw) for ln in wrapped[:2]] or [""]
            if len(wrapped) > 2:
                lines[-1] = _ellipsize(d, lines[-1] + " …", f, mw)
        col = (color + (255,)) if color else _WHITE
        if len(lines) == 1:
            _text(d, ((bx0 + bx1) / 2, by0 + 74), lines[0], f, col, "mm")
        else:
            _text(d, ((bx0 + bx1) / 2, by0 + 64), lines[0], f, col, "mm")
            _text(d, ((bx0 + bx1) / 2, by0 + 92), lines[1], f, col, "mm")
    rows = (len(items) + cols - 1) // cols
    return y + rows * (h + gap)


def _fmt_ch(n: Optional[int]) -> Optional[str]:
    if not n:
        return None
    return {1: "Mono", 2: "Stereo"}.get(n, f"{n} ch")


def _draw_spectrum(img, d, y: int, q: Dict[str, Any], vcolor, accent, x0: int, x1: int) -> int:
    sp = q.get("spectrum") or {}
    freqs, levels = sp.get("freq_hz") or [], sp.get("level_db") or []
    ch_h = 560
    _rrect(img, (x0, y, x1, y + ch_h), 30, (255, 255, 255, 22))
    px0, px1, py0, py1 = x0 + 84, x1 - 30, y + 74, y + 74 + 330
    nyq = float(q.get("nyquist_hz") or 22050.0)
    xmax = min(nyq, 24000.0)
    xmax = max(8000.0, math.ceil(xmax / 2000.0) * 2000.0 if xmax < nyq or nyq <= 24000 else xmax)
    xmax = min(xmax, 24000.0)
    pts = [(f, l) for f, l in zip(freqs, levels) if f <= xmax]
    lo, hi = -100.0, 10.0
    if pts:
        hi = max(10.0, math.ceil(min(30.0, max(l for _, l in pts)) / 10.0) * 10.0)
    span = hi - lo

    def X(f: float) -> float:
        return px0 + (min(f, xmax) / xmax) * (px1 - px0)

    def Y(db: float) -> float:
        return py0 + (hi - max(lo, min(hi, db))) / span * (py1 - py0)

    fs, ft = _font(20), _font(21)
    for db in range(int(lo), int(hi) + 1, 20):  # خطوط افقی (dB)
        yy = Y(db)
        d.line((px0, yy, px1, yy), fill=(255, 255, 255, 34), width=1)
        _text(d, (px0 - 12, yy), f"{db}", fs, (255, 255, 255, 120), "rm")
    for k in range(0, int(xmax / 1000) + 1, 4 if xmax <= 24000 else 4):  # خطوط عمودی (kHz)
        xx = X(k * 1000.0)
        d.line((xx, py0, xx, py1), fill=(255, 255, 255, 22), width=1)
        _text(d, (xx, py1 + 22), f"{k}k", ft, (255, 255, 255, 140), "mm")
    if pts:
        poly = [(X(f), Y(l)) for f, l in pts]
        # پر کردن زیر منحنی با گرادیان محو
        ov = Image.new("RGBA", (int(px1 - px0) + 2, int(py1 - py0) + 2), (0, 0, 0, 0))
        mask = Image.new("L", ov.size, 0)
        ImageDraw.Draw(mask).polygon([(p[0] - px0, p[1] - py0) for p in poly] + [(poly[-1][0] - px0, py1 - py0), (0, py1 - py0)], fill=255)
        grad = Image.linear_gradient("L").resize(ov.size).transpose(Image.FLIP_TOP_BOTTOM).point(lambda v: int(v * 0.42))
        mask = Image.composite(grad, Image.new("L", ov.size, 0), mask)
        ov = Image.new("RGBA", ov.size, accent + (255,))
        ov.putalpha(mask)
        _comp(img, ov, (px0, py0))
        d.line(poly, fill=accent + (255,), width=4, joint="curve")
        cut = q.get("cutoff_hz")
        if cut and cut <= xmax:
            cx = X(float(cut))
            for yy in range(int(py0), int(py1), 14):  # خط‌چین قطع فرکانس
                d.line((cx, yy, cx, min(py1, yy + 7)), fill=vcolor + (255,), width=3)
            lab = f"{float(cut) / 1000:.1f} kHz"
            f = _font(24, True)
            bw = _tw(d, lab, f) + 28
            bx = min(max(cx - bw / 2, px0), px1 - bw)
            _rrect(img, (bx, py0 - 40, bx + bw, py0 - 6), 17, vcolor + (255,))
            _text(d, (bx + bw / 2, py0 - 23), lab, f, (14, 16, 24, 255), "mm")
    else:
        _text(d, ((px0 + px1) / 2, (py0 + py1) / 2), "داده‌ی نمودار موجود نیست", _font(28), _MUTED, "mm", rtl=True)
    # توضیح زیر نمودار
    yy = py1 + 58
    rows: List[str] = []
    if q.get("cutoff_hz"):
        shape = ""
        if q.get("edge_width_hz"):
            shape = ("، برش تیز دیجیتال" if q["edge_width_hz"] <= SHARP_WIDTH_HZ
                     else "، افت تدریجی" if q["edge_width_hz"] >= GENTLE_WIDTH_HZ else "، افت نسبتاً تند")
        rows.append(f"قطع فرکانس: حدود {q['cutoff_hz'] / 1000:.1f} kHz (افت {int(q['cliff_db'])} dB{shape})")
        if q.get("seg_count", 0) >= 2 and q.get("seg_agree") is not None:
            ok = int(round(q["seg_agree"] * q["seg_count"]))
            rows.append(f"ثبات قطع: در {ok} از {q['seg_count']} بخش آهنگ یکسانه")
    elif q.get("analyzed"):
        rows.append(f"قطع فرکانسی ندارد (طیف تا {nyq / 1000:.1f} kHz ادامه داره)")
    if q.get("estimated_class"):
        rows.append(f"کیفیت واقعی (تخمین): {_TIER_FA[q['estimated_class']]}")
    for r_ in rows[:3]:
        _text(d, (x1 - 28, yy), r_, _font(26), (255, 255, 255, 225), "rm", rtl=True)
        yy += 40
    return y + ch_h


_SG_STOPS = (
    (0.00, (0, 0, 0)), (0.18, (22, 8, 72)), (0.38, (112, 18, 132)), (0.58, (206, 40, 74)),
    (0.78, (250, 146, 28)), (0.91, (255, 228, 92)), (1.00, (255, 255, 255)),
)
_sg_lut_cache: Optional[Any] = None


def _sg_lut() -> Any:
    global _sg_lut_cache
    if _sg_lut_cache is None:
        t = np.linspace(0.0, 1.0, 256)
        xs = [p for p, _ in _SG_STOPS]
        _sg_lut_cache = np.stack(
            [np.interp(t, xs, [c[k] for _, c in _SG_STOPS]) for k in range(3)], axis=1
        ).round().astype(np.uint8)
    return _sg_lut_cache


def _sg_time_label(t: float) -> str:
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _draw_spectrogram(img, d, y: int, q: Dict[str, Any], vcolor, x0: int, x1: int) -> int:
    sg = q.get("spectrogram")
    H = 484
    _rrect(img, (x0, y, x1, y + H), 30, (255, 255, 255, 22))
    px0, px1, py0, py1 = x0 + 84, x1 - 112, y + 28, y + 28 + 360
    fmax, dur = float(sg["fmax"]), float(sg["duration"])
    rgb = _sg_lut()[sg["img"][::-1]]  # ردیف 0 (فرکانس پایین) پایین تصویر
    pic = Image.fromarray(rgb, "RGB").resize((int(px1 - px0), int(py1 - py0)), Image.BILINEAR)
    img.paste(pic, (int(px0), int(py0)))
    fs, ft = _font(20), _font(21)

    def Y(f: float) -> float:
        return py1 - min(f, fmax) / fmax * (py1 - py0)

    for k in range(0, int(fmax / 1000) + 1, 4):  # محور فرکانس (kHz)
        yy = Y(k * 1000.0)
        if k:
            d.line((px0, yy, px1, yy), fill=(255, 255, 255, 46), width=1)
        _text(d, (px0 - 12, yy), f"{k}k", fs, (255, 255, 255, 150), "rm")
    step = next((s_ for s_ in (10, 15, 30, 60, 120, 300, 600, 1200) if dur / s_ <= 7), 1800)
    t = 0.0
    while t <= dur + 0.01:  # محور زمان
        xx = px0 + t / dur * (px1 - px0)
        d.line((xx, py1, xx, py1 + 8), fill=(255, 255, 255, 120), width=2)
        _text(d, (xx, py1 + 26), _sg_time_label(t), ft, (255, 255, 255, 150), "mm")
        t += step
    cut = q.get("cutoff_hz")
    if cut and cut < fmax:  # خط‌چین قطع فرکانس
        cy = Y(float(cut))
        for xx in range(int(px0), int(px1), 16):
            d.line((xx, cy, min(px1, xx + 8), cy), fill=vcolor + (255,), width=3)
        lab, f = f"{float(cut) / 1000:.1f} kHz", _font(23, True)
        bw = _tw(d, lab, f) + 26
        by = cy - 38 if cy - 38 > py0 + 4 else cy + 6
        _rrect(img, (px1 - bw - 10, by, px1 - 10, by + 32), 16, vcolor + (255,))
        _text(d, (px1 - 10 - bw / 2, by + 16), lab, f, (14, 16, 24, 255), "mm")
    # نوار رنگ (dBFS)
    cx0, cx1 = px1 + 26, px1 + 44
    bar = _sg_lut()[np.linspace(255, 0, int(py1 - py0)).round().astype(np.uint8)]
    img.paste(Image.fromarray(np.repeat(bar[:, None, :], int(cx1 - cx0), axis=1), "RGB"), (int(cx0), int(py0)))
    for db in (0, -40, -80, -120):
        yy = py0 + (0 - db) / 120.0 * (py1 - py0)
        d.line((cx1, yy, cx1 + 6, yy), fill=(255, 255, 255, 140), width=2)
        _text(d, (cx1 + 10, min(max(yy, py0 + 8), py1 - 8)), f"{db}", _font(19), (255, 255, 255, 150), "lm")
    _text(d, (cx0 + 9, py0 - 12), "dBFS", _font(17), (255, 255, 255, 120), "mb")
    _text(d, (x1 - 28, py1 + 64), "محور افقی: زمان  •  محور عمودی: فرکانس  •  رنگ روشن‌تر = انرژی بیشتر",
          _font(22), _MUTED, "rm", rtl=True)
    return y + H


def render_report_card(result: Dict[str, Any], cover_path: Optional[Path] = None, ext: str = "") -> bytes:
    """کارت گزارش تحلیل؛ result خروجی analyze_audio (بهتره با include_spectrum=True)"""
    md, t, q = result["metadata"], result["technical"], result["quality"]
    stem = (result.get("file") or {}).get("stem") or ""
    title = (md.get("title") or stem or "Unknown").strip()
    artist = (md.get("artist") or "").strip()
    cover = _open_cover(cover_path)
    seed = f"{title}|{artist}"
    accent = _accent(cover, seed)
    vlabel, vcolor = _VERDICT_STYLE.get(q["verdict"], _VERDICT_STYLE["inconclusive"])
    W, pad = CARD_W, 44
    x0, x1 = pad, W - pad

    def draw_all(img) -> int:
        d = ImageDraw.Draw(img, "RGBA")
        y = pad
        # ── سربرگ
        art = 196
        _shadow(img, (x0, y, x0 + art, y + art), 30, blur=16, alpha=130, dy=8)
        _paste_rounded(img, _art_tile(cover, art, accent, seed), (x0, y), 30)
        tx0, tx1 = x0 + art + 34, x1
        t_rtl = _is_rtl(title)
        f_t, lines = _fit_lines(d, title, ((46, 1), (40, 2), (34, 3)), tx1 - tx0, True)
        ty = y + 4
        for ln in lines:
            _text(d, (tx1 if t_rtl else tx0, ty), ln, f_t, _WHITE, "ra" if t_rtl else "la")
            ty += int(f_t.size * 1.4)
        if artist:
            f_a = _font(32)
            _text(d, (tx1 if _is_rtl(artist) else tx0, ty + 2), _ellipsize(d, artist, f_a, tx1 - tx0), f_a,
                  accent + (255,), "ra" if _is_rtl(artist) else "la")
            ty += 48
        meta = [m for m in (md.get("album"), str(md["year"]) if md.get("year") else None, md.get("genre")) if m]
        if md.get("track_number"):
            meta.append(f"#{md['track_number']}" + (f"/{md['track_total']}" if md.get("track_total") else ""))
        if meta:
            f_m = _font(25)
            mtxt = _ellipsize(d, "  ·  ".join(meta), f_m, tx1 - tx0)
            _text(d, (tx1 if _is_rtl(mtxt) else tx0, ty + 4), mtxt, f_m, _MUTED, "ra" if _is_rtl(mtxt) else "la")
        y += art + 34

        # ── حکم نهایی
        reasons = list(q.get("reasons") or [])[:6]
        f_r = _font(28)
        ry = 0
        probe_lines = 0
        for r_ in reasons:
            probe_lines += len(_wrap(d, r_, f_r, (x1 - x0) - 56 - 30))
        b_h = 140 + (probe_lines * 44 + len(reasons) * 14 + 8 if reasons else 0)
        _rrect(img, (x0, y, x1, y + b_h), 34, (vcolor[0], vcolor[1], vcolor[2], 44))
        _rrect(img, (x1 - 10, y + 26, x1 - 2, y + b_h - 26), 4, vcolor + (255,))
        ic = 84
        _comp(img, _verdict_icon(q["verdict"], ic, vcolor), (x1 - 34 - ic, y + 28))
        _text(d, (x1 - 34 - ic - 22, y + 50), vlabel, _font(40, True), _WHITE, "rm", rtl=True)
        if q["verdict"] != "inconclusive":
            _text(d, (x1 - 34 - ic - 22, y + 92), f"اطمینان: {_CONF_TXT.get(q['confidence'], '')}", _font(26),
                  _mix(vcolor, (255, 255, 255), 0.45) + (255,), "rm", rtl=True)
        ry = y + 140
        for r_ in reasons:
            ry = _para_rtl(d, x1 - 30, ry, r_, f_r, (255, 255, 255, 235), (x1 - x0) - 56, 44, dot=vcolor) + 14
        y += b_h + 36

        # ── مشخصات فنی
        y = _heading(img, d, y, "مشخصات فنی", accent, x0, x1)
        bdc = q.get("bit_depth_check")
        items: List[Tuple[str, str, Any]] = []
        if t.get("container"):
            items.append(("کانتینر", str(t["container"]), None))
        if t.get("codec_label"):
            items.append(("کدک · " + ("لاسلس" if t.get("is_lossless") else "فشرده (lossy)"), str(t["codec_label"]),
                          (52, 199, 128) if t.get("is_lossless") else None))
        if t.get("sample_rate"):
            items.append(("Sample Rate", f"{t['sample_rate'] / 1000:g} kHz", None))
        if t.get("bit_depth"):
            v = f"{t['bit_depth']} bit"
            if bdc and bdc.get("effective_bits") != t["bit_depth"]:
                v = f"{t['bit_depth']} → {bdc['effective_bits']} bit"
            items.append(("عمق بیت", v, (245, 166, 35) if "→" in v else None))
        elif not t.get("is_lossless"):  # AAC/MP3/Opus/... اصلاً عمق بیت ندارن (MediaInfo هم چیزی نشون نمیده)
            items.append(("عمق بیت · lossy", "ندارد", None))
        else:
            items.append(("عمق بیت", "نامشخص", None))
        if _fmt_ch(t.get("channels")):
            items.append(("کانال", _fmt_ch(t["channels"]) or "", None))
        if t.get("bitrate_kbps"):
            mode = f" {t['bitrate_mode']}" if t.get("bitrate_mode") else ""
            items.append(("بیت‌ریت", f"{t['bitrate_kbps']} kbps{mode}", None))
        items.append(("مدت زمان", _fmt_time(t.get("duration") or 0), None))
        sv, su = _fmt_size(t.get("size_bytes") or 0)
        items.append(("حجم فایل", f"{sv} {su}", None))
        enc = _pick_encoder(md, t)
        if enc:
            items.append(("نرم‌افزار انکود", enc, None))
        elif ext:
            items.append(("پسوند فایل", "." + ext.strip(".").lower(), None))
        y = _kv_tiles(img, d, y, items, x0, x1, accent) + 14

        # ── نمودار طیف
        if q.get("analyzed"):
            y = _heading(img, d, y, "تحلیل طیف فرکانسی", accent, x0, x1)
            y = _draw_spectrum(img, d, y, q, vcolor, accent, x0, x1) + 36
            if q.get("spectrogram") is not None:
                y = _heading(img, d, y, "اسپکتروگرام", accent, x0, x1)
                y = _draw_spectrogram(img, d, y, q, vcolor, x0, x1) + 36
        else:
            y = _heading(img, d, y, "تحلیل طیف فرکانسی", accent, x0, x1)
            _text(d, (x1, y), "تحلیل طیفی برای این فایل انجام نشد.", _font(28), _MUTED, "ra", rtl=True)
            y += 70

        # ── دینامیک و لودنس
        ld, dyn, clp, stp = q.get("loudness"), q.get("dynamics"), q.get("clipping"), q.get("stereo")
        dt: List[Tuple[str, str, Any]] = []
        if ld:
            dt.append(("لودنس", f"{ld['lufs']:.1f} LUFS", None))
            if ld.get("lra") is not None:
                dt.append(("LRA", f"{ld['lra']:.1f} LU", None))
            if ld.get("true_peak") is not None:
                over = ld["true_peak"] > 0
                dt.append(("True Peak", f"{ld['true_peak']:+.1f} dBTP", (239, 68, 68) if over else None))
        if dyn:
            dt.append(("دینامیک (DR)", f"≈ {int(round(dyn['dr']))}", None))
            dt.append(("Crest Factor", f"{dyn['crest_db']:.1f} dB", None))
        if clp is not None and q.get("analyzed"):
            bad = clp["ratio"] >= CLIP_RATIO_WARN
            dt.append(("کلیپینگ", f"{clp['ratio'] * 100:.2f}%" if bad else "ندارد", (245, 166, 35) if bad else (52, 199, 128)))
        if stp:
            dt.append(("همبستگی استریو", f"{stp['correlation']:.2f}", None))
            dt.append(("عرض استریو", "تقریباً صفر" if stp["side_db"] <= -60 else f"{stp['side_db']:.0f} dB", None))
        if dt:
            y = _heading(img, d, y, "دینامیک، لودنس و استریو", accent, x0, x1)
            y = _kv_tiles(img, d, y, dt, x0, x1, accent) + 14

        # ── نکته‌ها
        notes = [txt for _, txt in (q.get("notes") or [])][:5]
        if notes:
            y = _heading(img, d, y, "نکته‌های تکمیلی", accent, x0, x1)
            for n_ in notes:
                y = _para_rtl(d, x1 - 6, y, n_, _font(27), (255, 255, 255, 220), (x1 - x0) - 12, 42, dot=(245, 166, 35)) + 12
            y += 8
        if q["verdict"] in ("fake", "suspicious") and q.get("confidence") != "high":
            y = _para_rtl(d, x1 - 6, y, "این تشخیص بر پایه‌ی تحلیل طیف است؛ برای منابع قدیمی/آنالوگ یا مسترینگ خاص قطعی نیست.",
                          _font(24), _MUTED, (x1 - x0) - 12, 38) + 10
        if q.get("analyzed") and q.get("segments_used"):
            _text(d, (W / 2, y + 14), f"بررسی روی {q['segments_used']} بخش ({int(q.get('seconds_analyzed') or 0)} ثانیه از آهنگ)",
                  _font(22), (255, 255, 255, 110), "mm", rtl=True)
            y += 40
        return y + pad - 14

    scratch = Image.new("RGB", (W, 5200), (0, 0, 0))
    _MEASURE.on = True
    try:
        H = min(max(draw_all(scratch), 700), 5000)
    finally:
        _MEASURE.on = False
    img = _background(W, H, cover, accent)
    draw_all(img)
    return _to_jpeg(img)


# ───────────────────────── ساخت کارت‌ها (async) ─────────────────────────

CARDS_ENABLED = os.getenv("GRAPHIC_CARDS", "1").strip().lower() not in {"0", "false", "no", "off"} and PIL_OK


QUALITY_CHECK = os.getenv("QUALITY_CHECK", "1").strip().lower() not in {"0", "false", "no", "off"}


async def quick_quality(job: "Job") -> None:
    """بررسی طیفیِ اصالت کیفیت با همون موتورِ «آنالیز دقیق» (بدون لودنس و بدون ساخت گزارش)؛ فقط برای اینکه
    کارت اولیه با نتیجه‌ی آنالیز ناسازگار نباشه. هر خطایی نادیده گرفته میشه و کارت مثل قبل بیت‌ریت‌محور میمونه.
    خاموش‌کردن: QUALITY_CHECK=0 (بدون بررسی، کارت اولیه سریع‌تر میاد)."""
    if not QUALITY_CHECK:
        return
    try:
        async with SEM:
            res = await analyze_audio_async(job.src, include_spectrum=False, loudness=False)
        q = res.get("quality") or {}
        if res.get("ok") and q.get("analyzed") and q.get("verdict") in ("genuine", "suspicious", "fake"):
            job.verdict = q["verdict"]
    except Exception:  # noqa: BLE001
        log.exception("quick quality check failed")


# پیش‌محاسبه‌ی «آنالیز دقیق» (لودنس، اسپکتروگرام، ...) در پس‌زمینه، درست بعد از اینکه منوی آهنگ برای کاربر اومد؛
# تا کاربر فکر کنه و دکمه رو بزنه، نتیجه آماده‌ست و آنالیز/نرمال‌سازی تقریباً آنی جواب میدن (نتیجه‌ی کاملاً یکسان).
# خاموش‌کردن (روی هاست‌های خیلی ضعیف): PREFETCH_ANALYSIS=0
PREFETCH_ANALYSIS = os.getenv("PREFETCH_ANALYSIS", "1").strip().lower() not in {"0", "false", "no", "off"}


async def _prefetch_analysis(job: "Job") -> None:
    try:
        async with SEM:
            if job.id not in JOBS:
                return
            await asyncio.gather(
                analyze_audio_async(job.src, include_spectrum=CARDS_ENABLED),
                warm_mediainfo(job.src, f"{job.name}{job.src.suffix}"),
                return_exceptions=True,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("analysis prefetch failed", exc_info=True)


def start_prefetch(job: "Job") -> None:
    if PREFETCH_ANALYSIS and QUALITY_CHECK and job.id in JOBS and job.bg is None:
        job.bg = asyncio.create_task(_prefetch_analysis(job))


async def build_info_card(job: "Job") -> Optional[bytes]:
    if not CARDS_ENABLED:
        return None
    try:
        return await asyncio.to_thread(
            render_info_card, job.title or job.name, job.artist, job.size, job.duration, job.kbps,
            job.cover, job.src.suffix, job.verdict,
        )
    except Exception:  # noqa: BLE001
        log.exception("info card render failed")
        return None


async def build_report_card(result: Dict[str, Any], cover: Optional[Path], ext: str) -> Optional[bytes]:
    try:
        return await asyncio.to_thread(render_report_card, result, cover, ext)
    except Exception:  # noqa: BLE001
        log.exception("report card render failed")
        return None


# ───────────────────────── MediaInfo (لینک زیر کپشن تحلیل) ─────────────────────────
# خروجی کامل MediaInfo (فرمت، متادیتا، کاور، متن ترانه و ...) توی یه صفحه‌ی Telegraph ساخته میشه و
# لینکش زیر کپشن کارت تحلیل میاد؛ تلگرام خودش اون رو به شکل Instant View باز میکنه.
#   اولویت تولید متن: CLI برنامه‌ی mediainfo  ←  پکیج pymediainfo  ←  ffprobe (قالب مشابه)
MEDIAINFO_BIN = os.getenv("MEDIAINFO_PATH", "mediainfo").strip() or "mediainfo"
MEDIAINFO_LABEL = os.getenv("MEDIAINFO_LINK_TEXT", "MediaInfo").strip() or "MediaInfo"
MEDIAINFO_ENABLED = os.getenv("MEDIAINFO_LINK", "1").strip().lower() not in {"0", "false", "no", "off"}
TELEGRAPH_TOKEN_FILE = Path(os.getenv("TELEGRAPH_TOKEN_FILE", "./telegraph_token.txt"))
TELEGRAPH_API = "https://api.telegra.ph"
TELEGRAPH_MAX_BYTES = 48_000  # سقف Telegraph حدود 64KB است؛ با حاشیه‌ی امن
_TG_TOKEN: Optional[str] = None
_TG_CLIENT: Any = None  # کلاینت HTTP مشترک (keep-alive)؛ هر بار handshake/TLS جدید نمیخوایم
try:  # حداکثر چند ثانیه برای لینک MediaInfo صبر میکنیم؛ دیرتر آماده شد، گزارش بدون لینک میره و لینک بعداً به کپشن اضافه میشه
    MEDIAINFO_WAIT = max(0.0, float(os.getenv("MEDIAINFO_WAIT_SEC", "1.0").replace(",", ".")))
except ValueError:
    MEDIAINFO_WAIT = 1.0
_BG_TASKS: set = set()  # نگه‌داشتن رفرنس تسک‌های پس‌زمینه تا garbage-collect نشن

_MI_PAD = 41  # عرض ستون نام فیلد توی خروجی MediaInfo


def _mi_kv(key: str, value: Any) -> str:
    return f"{key:<{_MI_PAD}}: {value}"


def _mi_size(nbytes: float) -> str:
    return f"{nbytes / MB:.1f} MiB" if nbytes >= MB else f"{nbytes / 1024:.0f} KiB"


def _mi_duration(sec: float) -> str:
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} h {m} min {s} s"
    return f"{m} min {s} s" if m else f"{s} s"


def _mediainfo_from_ffprobe(path: Path) -> Optional[str]:
    """جایگزین وقتی نه mediainfo هست نه pymediainfo: همون قالب General / Audio / Image با ffprobe."""
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60,
        )
        data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    if not audio:
        return None
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    tags.update({str(k).lower(): v for k, v in (audio.get("tags") or {}).items() if str(k).lower() not in tags})

    container = {"mov,mp4,m4a,3gp,3g2,mj2": "MPEG-4", "mp3": "MPEG Audio", "flac": "FLAC", "ogg": "Ogg",
                 "wav": "Wave", "matroska,webm": "Matroska", "asf": "Windows Media", "aiff": "AIFF",
                 }.get(str(fmt.get("format_name")), str(fmt.get("format_name") or "?"))
    size = _num(fmt.get("size"), int) or path.stat().st_size
    dur = _num(fmt.get("duration")) or _num(audio.get("duration")) or 0.0

    L = ["General", _mi_kv("Complete name", path.name), _mi_kv("Format", container),
         _mi_kv("File size", _mi_size(size))]
    if dur:
        L.append(_mi_kv("Duration", _mi_duration(dur)))
    if fmt.get("bit_rate"):
        L.append(_mi_kv("Overall bit rate", f"{int(fmt['bit_rate']) / 1000:.0f} kb/s"))
    for key, label in (("title", "Title"), ("album", "Album"), ("album_artist", "Album/Performer"),
                       ("artist", "Performer"), ("composer", "Composer"), ("genre", "Genre"),
                       ("date", "Recorded date"), ("track", "Track name/Position"), ("publisher", "Publisher"),
                       ("copyright", "Copyright"), ("comment", "Comment")):
        if tags.get(key):
            L.append(_mi_kv(label, str(tags[key]).strip()))
    lyr = tags.get("lyrics") or tags.get("unsyncedlyrics") or tags.get("lyrics-eng")
    if lyr:
        lines = str(lyr).replace("\r\n", "\n").strip().split("\n")
        L.append(_mi_kv("Lyrics", lines[0]))
        L += [" " * _MI_PAD + "  " + ln for ln in lines[1:]]

    codec = str(audio.get("codec_name") or "?")
    prof = str(audio.get("profile") or "")
    afmt = {"aac": "AAC", "mp3": "MPEG Audio", "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis",
            "alac": "ALAC", "wmav2": "WMA", "ac3": "AC-3"}.get(codec, codec.upper())
    if codec.startswith("pcm"):
        afmt = "PCM"
    if codec == "aac" and prof:
        afmt += f" {prof}"
    L += ["", "Audio", _mi_kv("ID", audio.get("index", 0) + 1), _mi_kv("Format", afmt),
          _mi_kv("Codec ID", audio.get("codec_tag_string") or codec)]
    adur = _num(audio.get("duration")) or dur
    if adur:
        L.append(_mi_kv("Duration", _mi_duration(adur)))
    if audio.get("bit_rate"):
        L.append(_mi_kv("Bit rate", f"{int(audio['bit_rate']) / 1000:.0f} kb/s"))
    if audio.get("channels"):
        n = int(audio["channels"])
        L.append(_mi_kv("Channel(s)", f"{n} channel" + ("s" if n != 1 else "")))
    if audio.get("channel_layout"):
        L.append(_mi_kv("Channel layout", str(audio["channel_layout"])))
    if audio.get("sample_rate"):
        L.append(_mi_kv("Sampling rate", f"{int(audio['sample_rate']) / 1000:g} kHz"))
    depth = audio.get("bits_per_raw_sample") or (audio.get("bits_per_sample") if audio.get("bits_per_sample") else None)
    if depth and str(depth) != "0":
        L.append(_mi_kv("Bit depth", f"{depth} bits"))

    pic = next((x for x in streams if x.get("codec_type") == "video"), None)
    if pic:
        L += ["", "Image", _mi_kv("Type", "Cover"),
              _mi_kv("Format", {"mjpeg": "JPEG", "png": "PNG", "bmp": "BMP"}.get(str(pic.get("codec_name")),
                                                                                  str(pic.get("codec_name")).upper())),
              _mi_kv("Width", f"{pic.get('width')} pixels"), _mi_kv("Height", f"{pic.get('height')} pixels")]
    return "\n".join(L)


def _mediainfo_text(path: Path, display_name: str) -> Optional[str]:
    """متن کامل MediaInfo (همگام). اسم فایل توی Complete name با اسم اصلی فایلِ کاربر عوض میشه."""
    txt: Optional[str] = None
    exe = shutil.which(MEDIAINFO_BIN)
    if exe:
        try:
            p = subprocess.run([exe, path.name], cwd=str(path.parent), capture_output=True, timeout=90)
            out = p.stdout.decode("utf-8", "replace")
            if p.returncode == 0 and out.strip():
                txt = out
        except (OSError, subprocess.SubprocessError):
            log.warning("mediainfo CLI failed", exc_info=True)
    if txt is None:
        try:
            from pymediainfo import MediaInfo  # type: ignore[import-not-found]

            out = MediaInfo.parse(str(path), output="text")
            if isinstance(out, str) and out.strip():
                txt = out
        except Exception:  # noqa: BLE001
            pass
    if txt is None:
        txt = _mediainfo_from_ffprobe(path)
    if not txt:
        return None
    txt = txt.replace("\r\n", "\n").replace("\r", "\n").strip()
    txt = re.sub(r"(?m)^(Complete name\s*:\s*).*$", lambda m: m.group(1) + display_name, txt, count=1)
    return txt


def _clip_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore").rsplit("\n", 1)[0] + "\n…"


async def _telegraph_post(client: Any, method: str, data: Dict[str, Any]) -> Dict[str, Any]:
    r = await client.post(f"{TELEGRAPH_API}/{method}", data=data)
    js = r.json()
    if not js.get("ok"):
        raise RuntimeError(f"telegraph {method}: {js.get('error')}")
    return js["result"]


async def _telegraph_token(client: Any) -> str:
    global _TG_TOKEN
    if _TG_TOKEN:
        return _TG_TOKEN
    with contextlib.suppress(OSError):
        tok = TELEGRAPH_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            _TG_TOKEN = tok
            return tok
    acc = await _telegraph_post(client, "createAccount", {"short_name": "MediaInfo", "author_name": "MediaInfo"})
    _TG_TOKEN = acc["access_token"]
    with contextlib.suppress(OSError):
        TELEGRAPH_TOKEN_FILE.write_text(_TG_TOKEN, encoding="utf-8")
    return _TG_TOKEN


def _telegraph_client() -> Any:
    global _TG_CLIENT
    if _TG_CLIENT is None or getattr(_TG_CLIENT, "is_closed", False):
        import httpx

        limits = httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120)
        try:
            _TG_CLIENT = httpx.AsyncClient(timeout=30, proxy=PROXY_URL, limits=limits)
        except TypeError:  # httpx قدیمی
            _TG_CLIENT = httpx.AsyncClient(timeout=30, proxies=PROXY_URL, limits=limits)
    return _TG_CLIENT


async def _publish_telegraph(title: str, body: str) -> str:
    client = _telegraph_client()
    token = await _telegraph_token(client)
    content = json.dumps([{"tag": "pre", "children": [body]}], ensure_ascii=False)
    page = await _telegraph_post(client, "createPage", {
        "access_token": token, "title": title[:256], "content": content, "return_content": "false",
    })
    return page["url"]


def _mediainfo_text_cached(path: Path, display_name: str) -> Optional[str]:
    """متن MediaInfo (کش‌دار)؛ پیش‌محاسبه‌ی پس‌زمینه همین رو پر میکنه، پس موقع آنالیز فقط ارسال به Telegraph می‌مونه."""
    return _memo(path, ("mi", display_name), lambda: (_mediainfo_text(path, display_name), []))[0]


async def warm_mediainfo(path: Path, display_name: str) -> None:
    if MEDIAINFO_ENABLED:
        await asyncio.to_thread(_mediainfo_text_cached, path, display_name)


async def build_mediainfo_link(path: Path, display_name: str) -> Optional[str]:
    """ساخت صفحه‌ی MediaInfo روی Telegraph و برگردوندن لینک؛ هر خطایی => None (کپشن بدون لینک میره)."""
    if not MEDIAINFO_ENABLED:
        return None
    try:
        text = await asyncio.to_thread(_mediainfo_text_cached, path, display_name)
        if not text:
            return None
        return await _publish_telegraph(f"MediaInfo — {display_name}", _clip_bytes(text, TELEGRAPH_MAX_BYTES))
    except Exception:  # noqa: BLE001
        log.exception("mediainfo page failed")
        return None


def _with_mediainfo_link(plain: str, url: Optional[str]) -> str:
    """متن ساده -> HTML امن + لینک MediaInfo زیرش (برای parse_mode=HTML)."""
    out = html.escape(plain)
    if url:
        out += f'\n\n📋 <a href="{html.escape(url, quote=True)}">{html.escape(MEDIAINFO_LABEL)}</a>'
    return out


# ───────────────────────── استخراج کاور (کیفیت اصلی) و متن ترانه (.lrc / .txt) ─────────────────────────
# قابلیت مستقل: دکمه‌ی «کاور و ترانه» توی منوی هر آهنگ. تنظیم: COVER_LYRICS=0 در .env برای خاموش‌کردن.
# توابع: cl_inspect_async(path) ← ClExtracted ، cl_parts_of(ex) ← اجزا ، cl_button_label(parts) ← متن دکمه یا None ، cl_export_files(ex, name) ← فایل‌های آماده‌ی ارسال
# نیازمندی: mutagen (برای SYLT و تگ‌ها) ؛ ffmpeg/ffprobe به‌عنوان فال‌بک.

CL_ENABLED = os.getenv("COVER_LYRICS", "1").strip().lower() not in {"0", "false", "no", "off"}


# ── ابزارهای مشترک ──

_CL_PROBE_TIMEOUT = 30
_CL_FFMPEG_TIMEOUT = 60


def _cl_open(path: Path) -> Any:
    """mutagen.File یا None (فرمت ناشناخته / خراب / mutagen نصب نیست)."""
    if mutagen is None:
        return None
    try:
        return mutagen.File(str(path))
    except Exception:  # noqa: BLE001
        return None


def _cl_ffprobe(path: Path) -> Dict[str, Any]:
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=_CL_PROBE_TIMEOUT,
        )
        if p.returncode != 0:
            return {}
        data = json.loads(p.stdout or b"{}")
        return data if isinstance(data, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _cl_clean(s: str) -> str:
    """یکدست‌سازی متن تگ: BOM، null، پایان خط ویندوزی/مک."""
    s = s.replace("\ufeff", "").replace("\x00", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


_CL_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _cl_safe_name(name: str, default: str = "song", maxlen: int = 80) -> str:
    name = _CL_BAD_NAME.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:maxlen].strip(" .")) or default


# ── کاور ──

_CL_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}


@dataclass
class ClCover:
    data: bytes
    mime: str
    ext: str
    width: Optional[int]
    height: Optional[int]
    source: str  # "tag" = همان بایت‌های داخل فایل | "ffmpeg" = استریم تصویر فایل


def _cl_sniff(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def _cl_img_size(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:3] == b"\xff\xd8\xff":
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except Exception:  # noqa: BLE001
        pass
    try:  # WebP/BMP و موارد نادر: اگه Pillow نصب باشه
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None, None


def _cl_wm_picture(raw: bytes) -> Optional[Tuple[bytes, str, int]]:
    """ساختار WM/Picture در ASF: type(1) size(4) mime(utf16\\0) desc(utf16\\0) data"""
    try:
        ptype = raw[0]
        size = struct.unpack_from("<I", raw, 1)[0]
        i = 5
        mime = ""
        for k in range(2):
            j = i
            while raw[j:j + 2] != b"\x00\x00":
                j += 2
            if k == 0:
                mime = raw[i:j].decode("utf-16-le", "replace")
            i = j + 2
        return raw[i:i + size], mime, ptype
    except Exception:  # noqa: BLE001
        return None


def _cl_cover_candidates(audio: Any) -> List[Tuple[bytes, Optional[str], int]]:
    """همه‌ی تصویرهای داخل تگ: (data, mime, picture_type) — type 3 = جلد جلو."""
    out: List[Tuple[bytes, Optional[str], int]] = []
    for p in getattr(audio, "pictures", None) or []:  # FLAC
        out.append((p.data, p.mime, p.type))
    tags = getattr(audio, "tags", None)
    if tags is None:
        return out
    try:
        if isinstance(tags, ID3):
            for f in tags.getall("APIC"):
                out.append((f.data, f.mime, f.type))
        elif isinstance(tags, MP4Tags):
            for c in tags.get("covr") or []:
                mime = "image/png" if getattr(c, "imageformat", None) == 14 else "image/jpeg"
                out.append((bytes(c), mime, 3))
        elif isinstance(tags, ASFTags):
            for attr in tags.get("WM/Picture") or []:
                parsed = _cl_wm_picture(getattr(attr, "value", b""))
                if parsed and parsed[0]:
                    out.append(parsed)
        else:
            for b64 in tags.get("metadata_block_picture") or []:  # Ogg Vorbis / Opus
                try:
                    p = Picture(base64.b64decode(b64))
                    out.append((p.data, p.mime, p.type))
                except Exception:  # noqa: BLE001
                    continue
            if type(tags).__name__ == "APEv2":
                raw = getattr(tags.get("Cover Art (Front)"), "value", None)
                if isinstance(raw, (bytes, bytearray)) and b"\x00" in raw:
                    out.append((bytes(raw.split(b"\x00", 1)[1]), None, 3))
    except Exception:  # noqa: BLE001
        pass
    return out


def _cl_cover_tags(audio: Any) -> Optional[ClCover]:
    good = []
    for data, mime, ptype in _cl_cover_candidates(audio):
        if not data:
            continue
        real = _cl_sniff(data)  # تصویر واقعی؟ (بعضی تگ‌ها فقط لینک دارن)
        if real is None:
            continue
        good.append((data, real, ptype))
    if not good:
        return None
    # جلد جلو اولویت داره؛ بین هم‌رده‌ها بزرگ‌ترین (بیشترین کیفیت)
    best = max(good, key=lambda x: (x[2] == 3, len(x[0])))
    data, mime, _ = best
    w, h = _cl_img_size(data)
    return ClCover(data=data, mime=mime, ext=_CL_EXT.get(mime, "jpg"), width=w, height=h, source="tag")


def _cl_cover_ffmpeg(path: Path) -> Optional[ClCover]:
    """فال‌بک: استریم تصویر داخل فایل (m4a/mkv/ogg…) یا وقتی mutagen در دسترس نیست."""
    streams = [s for s in _cl_ffprobe(path).get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        return None
    s = streams[0]
    ext = {"mjpeg": "jpg", "png": "png", "webp": "webp", "bmp": "bmp", "gif": "gif"}.get(s.get("codec_name", ""))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / f"cover.{ext or 'png'}"
        cmd = [FFMPEG, "-y", "-v", "error", "-i", str(path), "-map", f"0:{s.get('index', 0)}", "-frames:v", "1"]
        if ext:
            cmd += ["-c", "copy"]  # بدون بازکدگذاری = همان کیفیت اصلی
        cmd += [str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=_CL_FFMPEG_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return None
        data = out.read_bytes()
    mime = _cl_sniff(data)
    if mime is None:
        return None
    w, h = _cl_img_size(data)
    return ClCover(data=data, mime=mime, ext=_CL_EXT.get(mime, "jpg"), width=w, height=h, source="ffmpeg")


def cl_extract_cover(path: Path) -> Optional[ClCover]:
    audio = _cl_open(path)
    cover = _cl_cover_tags(audio) if audio is not None else None
    return cover or _cl_cover_ffmpeg(path)


# ── ترانه ──

@dataclass
class ClLyrics:
    lrc: Optional[str]  # متن LRC کامل (فقط وقتی تایم‌دار باشه)
    plain: str  # متن بدون زمان
    source: str  # مثلا "ID3 SYLT" ، "ID3 USLT" ، "Vorbis LYRICS"

    @property
    def synced(self) -> bool:
        return self.lrc is not None


# ───────────────────────── LRC ─────────────────────────

_CL_TS = r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]"
_CL_TS_LINE = re.compile(rf"^\s*(?:{_CL_TS}\s*)+")
_CL_TS_ANY = re.compile(_CL_TS)
_CL_WORD_TS = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")  # LRC نوع Enhanced (زمان هر کلمه)
_CL_META = re.compile(r"^\s*\[(?:ti|ar|al|au|by|re|ve|la|tool|offset|length|id|lang):[^\]]*\]\s*$", re.I)


def _cl_looks_like_lrc(text: str) -> bool:
    lines = [ln for ln in text.split("\n") if ln.strip() and not _CL_META.match(ln)]
    if not lines:
        return False
    stamped = sum(1 for ln in lines if _CL_TS_LINE.match(ln))
    return stamped >= 2 and stamped / len(lines) >= 0.3


def _cl_lrc_to_plain(text: str) -> str:
    out: List[str] = []
    for ln in text.split("\n"):
        if _CL_META.match(ln):
            continue
        ln = _CL_TS_ANY.sub("", ln)
        ln = _CL_WORD_TS.sub("", ln).strip()
        if not ln and out and not out[-1]:
            continue  # چند خط خالی پشت‌سرهم → یکی
        out.append(ln)
    return "\n".join(out).strip()


def _cl_stamp(sec: float) -> str:
    cs = max(0, int(round(sec * 100)))
    return f"[{cs // 6000:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}]"


def _cl_lrc_header(meta: Dict[str, Optional[str]]) -> str:
    rows = []
    for key, fld in (("ti", "title"), ("ar", "artist"), ("al", "album")):
        v = (meta.get(fld) or "").replace("\n", " ").replace("]", ")").strip()
        if v:
            rows.append(f"[{key}:{v}]")
    return "\n".join(rows)


def _cl_build_lrc(lines: List[Tuple[float, str]], meta: Dict[str, Optional[str]]) -> str:
    body = "\n".join(f"{_cl_stamp(t)}{text}" for t, text in lines)
    head = _cl_lrc_header(meta)
    return (head + "\n" + body if head else body).strip() + "\n"


# ───────────────────────── SYLT (ID3 تایم‌دار) ─────────────────────────


def _cl_sylt_lines(entries: List[Tuple[str, float]]) -> List[Tuple[float, str]]:
    """entries = [(متن, ثانیه)]. هر قطعه می‌تونه خط کامل یا بخشی از خط باشه؛ خط با \\n تموم میشه."""
    has_nl = any("\n" in t for t, _ in entries)
    if not has_nl:
        return [(t, s.strip()) for s, t in entries]
    lines: List[Tuple[float, str]] = []
    cur_t: Optional[float] = None
    cur = ""
    for text, t in entries:
        parts = text.split("\n")
        for idx, seg in enumerate(parts):
            if idx > 0:  # \n دیده شد → خط جاری بسته میشه
                if cur_t is not None:
                    lines.append((cur_t, cur.strip()))
                elif not cur:
                    lines.append((t, ""))  # خط خالی (مکث/بی‌کلام)
                cur_t, cur = None, ""
            if seg:
                if cur_t is None:
                    cur_t = t
                cur += seg
    if cur_t is not None:
        lines.append((cur_t, cur.strip()))
    return lines


def _cl_frames_to_sec(audio: Any):
    """SYLT با فرمت 1 (فریم MPEG) → ثانیه. نیاز به sample rate و طول هر فریم."""
    info = getattr(audio, "info", None)
    sr = getattr(info, "sample_rate", None) or 44100
    layer = getattr(info, "layer", 3)
    ver = getattr(info, "version", 1)
    spf = 384 if layer == 1 else (1152 if (layer == 2 or ver == 1) else 576)
    return lambda frame: frame * spf / sr


def _cl_id3_sylt(audio: Any, tags: Any, meta: Dict[str, Optional[str]]) -> Optional[ClLyrics]:
    frames = [f for f in tags.getall("SYLT") if getattr(f, "text", None)]
    if not frames:
        return None
    f = max(frames, key=lambda x: len(x.text))
    conv = _cl_frames_to_sec(audio) if getattr(f, "format", 2) == 1 else (lambda ms: ms / 1000.0)
    entries = [(str(txt), conv(ts)) for txt, ts in f.text]
    lines = sorted(_cl_sylt_lines(entries), key=lambda x: x[0])
    if not any(s for _, s in lines):
        return None
    lrc = _cl_build_lrc(lines, meta)
    return ClLyrics(lrc=lrc, plain=_cl_lrc_to_plain(lrc), source="ID3 SYLT")


# ───────────────────────── خواندن از هر نوع تگ ─────────────────────────


def _cl_to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return max((_cl_to_text(x) for x in v), key=len, default="")
    if hasattr(v, "text") and not isinstance(v, (str, bytes)):
        return _cl_to_text(v.text)
    val = getattr(v, "value", v)
    if isinstance(val, (bytes, bytearray)):
        for enc in ("utf-8", "utf-16"):
            try:
                return _cl_clean(bytes(val).decode(enc))
            except UnicodeDecodeError:
                continue
        return ""
    return _cl_clean(str(val))


def _cl_text_candidates(audio: Any, tags: Any) -> List[Tuple[str, str]]:
    """[(متن, منبع)] از همه‌ی جاهایی که ترانه‌ی متنی ممکنه باشه."""
    out: List[Tuple[str, str]] = []

    def add(text: str, src: str) -> None:
        if text:
            out.append((text, src))

    if isinstance(tags, ID3):
        for f in tags.getall("USLT"):
            add(_cl_to_text(f), "ID3 USLT")
        for f in tags.getall("TXXX"):
            if (getattr(f, "desc", "") or "").strip().upper() in {"LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS", "LRC"}:
                add(_cl_to_text(f), "ID3 TXXX")
    elif isinstance(tags, MP4Tags):
        add(_cl_to_text(tags.get("\xa9lyr")), "MP4 ©lyr")
    elif isinstance(tags, ASFTags):
        add(_cl_to_text(tags.get("WM/Lyrics")), "ASF WM/Lyrics")
    else:  # Vorbis comment (FLAC/Ogg/Opus) و APEv2
        try:
            keys = list(tags.keys())
        except Exception:  # noqa: BLE001
            keys = []
        for k in keys:
            kl = str(k).lower()
            if kl in {"lyrics", "unsyncedlyrics", "syncedlyrics", "lrc", "unsynced lyrics"} or kl.startswith("lyrics-"):
                try:
                    add(_cl_to_text(tags.get(k)), "Vorbis/APE " + str(k).upper())
                except Exception:  # noqa: BLE001
                    continue
    return out


def _cl_lyrics_ffprobe(path: Path) -> Optional[ClLyrics]:
    """فال‌بک بدون mutagen: فقط ترانه‌ی متنی (SYLT باینری رو ffprobe نمی‌خونه)."""
    info = _cl_ffprobe(path)
    tags: Dict[str, str] = {}
    for src in [info.get("format", {})] + list(info.get("streams", [])):
        for k, v in (src.get("tags") or {}).items():
            tags[str(k).lower()] = str(v)
    cands = [(_cl_clean(v), "ffprobe " + k) for k, v in tags.items()
             if k.startswith(("lyrics", "unsyncedlyrics", "syncedlyrics"))]
    return _cl_decide([c for c in cands if c[0]], {})


def _cl_decide(cands: List[Tuple[str, str]], meta: Dict[str, Optional[str]]) -> Optional[ClLyrics]:
    cands = [c for c in cands if c[0].strip()]
    if not cands:
        return None
    synced = [c for c in cands if _cl_looks_like_lrc(c[0])]
    if synced:
        text, src = max(synced, key=lambda c: len(c[0]))
        lrc = text if text.endswith("\n") else text + "\n"
        return ClLyrics(lrc=lrc, plain=_cl_lrc_to_plain(text), source=src + " (LRC)")
    text, src = max(cands, key=lambda c: len(c[0]))
    return ClLyrics(lrc=None, plain=text, source=src)


def cl_extract_lyrics(path: Path, meta: Optional[Dict[str, Optional[str]]] = None) -> Optional[ClLyrics]:
    meta = meta or {}
    audio = _cl_open(path)
    if audio is None:
        return _cl_lyrics_ffprobe(path)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return _cl_lyrics_ffprobe(path)
    if isinstance(tags, ID3):
        try:
            sylt = _cl_id3_sylt(audio, tags, meta)
        except Exception:  # noqa: BLE001
            sylt = None
        if sylt:
            return sylt
    try:
        found = _cl_decide(_cl_text_candidates(audio, tags), meta)
    except Exception:  # noqa: BLE001
        found = None
    return found or _cl_lyrics_ffprobe(path)


# ── API اصلی ──

@dataclass
class ClExtracted:
    cover: Optional[ClCover]
    lyrics: Optional[ClLyrics]


@dataclass
class ClFile:
    name: str
    data: bytes
    caption: str


def _cl_basic_tags(path: Path) -> Dict[str, Optional[str]]:
    """عنوان/خواننده/آلبوم برای هدر LRC (اختیاری؛ هر خطایی نادیده گرفته میشه)."""
    audio = _cl_open(path)
    out: Dict[str, Optional[str]] = {}
    tags = getattr(audio, "tags", None)
    if tags is None:
        return out
    keys = {
        "title": ("TIT2", "\xa9nam", "title", "Title"),
        "artist": ("TPE1", "\xa9ART", "artist", "Author"),
        "album": ("TALB", "\xa9alb", "album", "WM/AlbumTitle"),
    }
    for fld, names in keys.items():
        for k in names:
            try:
                v = tags.get(k)
            except Exception:  # noqa: BLE001
                continue
            if v is None:
                continue
            v = getattr(v, "text", v)
            while isinstance(v, (list, tuple)) and v:
                v = v[0]
            v = getattr(v, "value", v)
            if v:
                out[fld] = str(v).strip()
                break
    return out


def cl_inspect(path: Path) -> ClExtracted:
    path = Path(path)
    return ClExtracted(cover=cl_extract_cover(path), lyrics=cl_extract_lyrics(path, _cl_basic_tags(path)))


async def cl_inspect_async(path: Path) -> ClExtracted:
    return await asyncio.to_thread(cl_inspect, path)


def cl_parts_of(ex: ClExtracted) -> Optional[Tuple[bool, bool, bool]]:
    """چیزی که واقعاً توی فایل هست: (کاور، ترانه، ترانه‌ی تایم‌دار)؛ اگه هیچی نبود None."""
    if not (ex.cover or ex.lyrics):
        return None
    return (bool(ex.cover), bool(ex.lyrics), bool(ex.lyrics and ex.lyrics.synced))


def cl_button_label(parts: Optional[Tuple[bool, bool, bool]]) -> Optional[str]:
    """متن دکمه‌ی منو؛ هر بار موقع رسم منو ساخته میشه (نه موقع آپلود) تا ویرایش ادمین بلافاصله اعمال بشه."""
    if not parts:
        return None
    cover, lyrics, synced = parts
    words: List[str] = []
    if cover:
        words.append(T("cl.w.cover"))
    if lyrics:
        words.append(T("cl.w.synced") if synced else T("cl.w.lyrics"))
    if not words:
        return None
    what = words[0] if len(words) == 1 else T("cl.join", a=words[0], b=words[1])
    return T("btn.cl_cover" if cover else "btn.cl_lyrics", what=what)


def _cl_kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def cl_export_files(ex: ClExtracted, base_name: str) -> List[ClFile]:
    base = _cl_safe_name(base_name)
    files: List[ClFile] = []
    c = ex.cover
    if c:
        dims = f"{c.width}×{c.height} • " if c.width and c.height else ""
        files.append(ClFile(
            name=f"{base} - cover.{c.ext}", data=c.data,
            caption=T("cl.cap_cover", dims=dims, ext=c.ext.upper(), size=_cl_kb(len(c.data))),
        ))
    ly = ex.lyrics
    if ly:
        if ly.synced:
            files.append(ClFile(
                name=f"{base}.lrc", data=ly.lrc.encode("utf-8"),  # type: ignore[union-attr]
                caption=T("cl.cap_lrc"),
            ))
        if ly.plain.strip():
            files.append(ClFile(
                name=f"{base}.txt", data=(ly.plain.strip() + "\n").encode("utf-8"),
                caption=T("cl.cap_txt_nosync") if ly.synced else T("cl.cap_txt"),
            ))
    return files


# ───────────────────────── تبدیل فرمت (MP3 / Opus / M4A / FLAC) ─────────────────────────
# قابلیت مستقل: دکمه‌ی «تبدیل فرمت» توی منوی هر آهنگ ← انتخاب فرمت ← انتخاب بیت‌ریت ← ارسال.
# تنظیمات اختیاری .env: CONVERT=0 (خاموش) ، CONVERT_MP3_KBPS ، CONVERT_OPUS_KBPS ، CONVERT_AAC_KBPS
# FLAC فقط وقتی پیشنهاد میشه که فایل اصلی لاسلس (FLAC/ALAC/WAV/APE/…) باشه؛ تبدیل لاسی به FLAC بی‌معنیه.

CONVERT_ENABLED = os.getenv("CONVERT", "1").strip().lower() not in {"0", "false", "no", "off"}


def _conv_kbps(name: str, default: List[int], lo: int, hi: int) -> List[int]:
    vals = sorted({int(x) for x in re.findall(r"\d+", os.getenv(name, "")) if lo <= int(x) <= hi})
    return vals or default


CONV_KBPS: Dict[str, List[int]] = {
    "mp3": _conv_kbps("CONVERT_MP3_KBPS", [128, 192, 256, 320], 32, 320),
    "opus": _conv_kbps("CONVERT_OPUS_KBPS", [24, 32, 48, 64, 96, 128], 6, 256),
    "aac": _conv_kbps("CONVERT_AAC_KBPS", [96, 128, 192, 256], 32, 320),
}
CONV_INFO: Dict[str, Dict[str, str]] = {
    "mp3": {"label": "MP3", "ext": "mp3", "btn": "🎵 MP3 (سازگار با همه)"},
    "opus": {"label": "Opus", "ext": "opus", "btn": "🪶 Opus (حجم خیلی کم)"},
    "aac": {"label": "M4A (AAC)", "ext": "m4a", "btn": "🎼 M4A / AAC"},
    "flac": {"label": "FLAC", "ext": "flac", "btn": "💎 FLAC (بدون افت)"},
}
CONV_MP3_V0 = "v0"  # VBR با بهترین کیفیت MP3
CONV_MP3_V0_KBPS = 245  # میانگین تقریبیِ V0؛ فقط برای تخمین حجم
_CONV_PIC_CODECS = {"mjpeg", "png"}  # کاورهایی که بدون بازکدگذاری داخل خروجی کپی میشن
_CONV_NO_COVER: set = set()  # فرمت‌هایی که ffmpeg نصب‌شده نمی‌تونه توشون کاور بذاره (مثلاً Opus روی ffmpeg قدیمی)
_CONV_RATES = {
    "mp3": {48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000},
    "aac": {48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350},
}


def conv_is_lossless(job: Job) -> bool:
    return job.codec in LOSSLESS_CODECS or job.codec.startswith("pcm_")


def conv_formats(job: Job) -> List[str]:
    return ["mp3", "opus", "aac"] + (["flac"] if conv_is_lossless(job) else [])


def conv_options(job: Job, fmt: str) -> List[Tuple[str, int]]:
    """[(token, kbps تقریبی)]؛ گزینه‌هایی که خروجی‌شون از سقف ارسال تلگرام بیشتر میشه نمایش داده نمیشن."""
    opts = [(str(k), k) for k in CONV_KBPS[fmt]]
    if fmt == "mp3":
        opts.append((CONV_MP3_V0, CONV_MP3_V0_KBPS))
    return [(t, k) for t, k in opts if k * 125 * job.duration * 1.02 <= UPLOAD_LIMIT]


def conv_label(fmt: str, token: str) -> str:
    name = CONV_INFO[fmt]["label"]
    if fmt == "flac":
        return name
    if token == CONV_MP3_V0:
        return f"{name} · VBR V0"
    return f"{name} · {fa(token)}k"


def _conv_rate(sr: int, fmt: str) -> Optional[int]:
    """sample rate خروجی؛ None = همون ورودی. Opus همیشه 48k، لاسی‌ها اگه ریت ورودی پشتیبانی نشه یا های‌رز باشه تغییر می‌کنه."""
    if fmt == "flac":
        return None
    if fmt == "opus":
        return 48000
    if sr > 48000:
        return 48000 if sr % 48000 == 0 else 44100
    return sr if sr in _CONV_RATES[fmt] else 44100


def _conv_cmd(job: Job, fmt: str, token: str, out: Path, with_cover: bool, af: Optional[str] = None) -> List[str]:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(job.src), "-map", "0:a:0"]
    if with_cover:
        cmd += ["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    cmd += ["-map_metadata", "0"]
    if af:  # فقط نرمال‌سازی لودنس: فیلتر صوتی + پاک‌کردن تگ‌های ReplayGain/R128 قدیمی (بعد از تغییر صدا غلط میشن)
        cmd += ["-af", af]
        for key in ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain",
                    "replaygain_album_peak", "r128_track_gain", "r128_album_gain"):
            cmd += ["-metadata", f"{key}="]
    rate = _conv_rate(job.sample_rate, fmt)
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame"]
        cmd += ["-q:a", "0"] if token == CONV_MP3_V0 else ["-b:a", f"{token}k"]
        cmd += ["-id3v2_version", "3"]  # ID3v2.3: سازگاری بیشتر با پلیرها و ویندوز
    elif fmt == "opus":
        cmd += ["-c:a", "libopus", "-b:a", f"{token}k", "-vbr", "on", "-compression_level", "10",
                "-application", "audio"]
    elif fmt == "aac":
        cmd += ["-c:a", "aac", "-b:a", f"{token}k", "-movflags", "+faststart"]
    else:
        cmd += ["-c:a", "flac", "-compression_level", "8"]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd.append(str(out))
    return cmd


async def convert(job: Job, fmt: str, token: str, af: Optional[str] = None) -> Tuple[Path, int]:
    """تبدیل با ffmpeg. کاور داخل فایل (JPEG/PNG) بدون تغییر کپی میشه؛ اگه فرمت مقصد نپذیرفتش، بدون کاور دوباره تلاش می‌کنه."""
    out = job.dir / f"conv_{uuid.uuid4().hex[:6]}.{CONV_INFO[fmt]['ext']}"
    cover_first = job.pic_codec in _CONV_PIC_CODECS and fmt not in _CONV_NO_COVER
    err = b""
    for with_cover in ([True, False] if cover_first else [False]):
        with contextlib.suppress(OSError):
            out.unlink()
        rc, _, err = await run(*_conv_cmd(job, fmt, token, out, with_cover, af))
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            if cover_first and not with_cover:
                _CONV_NO_COVER.add(fmt)
            return out, out.stat().st_size
    raise RuntimeError(err.decode(errors="ignore")[-300:])


# ───────────────────────── امضای فرکانسی: پایپ‌لاین FFmpeg ─────────────────────────

WM_MAX_PCM = _int("WM_MAX_PCM_MB", 1500) * MB  # سقف حجم PCM (float32)؛ روی دیسک (memmap) نگه داشته میشه نه رم؛ آهنگ‌های بزرگ‌تر امضا نمی‌شوند
WM_SPAN_SEC = 0.0  # متن روی چند ثانیه پهن شود؛ 0 = کل آهنگ
# رفتار برای فایل‌های lossy (MP3/AAC/Opus/Vorbis) — چون امضا یعنی دیکود + انکودِ دوباره:
#   hq   (پیش‌فرض) : همان کدک ولی با بالاترین بیت‌ریتِ «شفاف» و بدون لوی‌پسِ اضافه → افتِ نسل دوم عملاً صفر
#   flac           : خروجی FLAC 24بیتی (دیکودِ بیت‌به‌بیتِ فایل کاربر + امضا؛ هیچ افتِ اضافه‌ای) ؛ حجم و پسوند عوض میشه
#   same           : رفتار قدیمی (همان کدک و همان بیت‌ریت؛ بیشترین افت)
WM_LOSSY_MODE = os.getenv("WM_LOSSY_MODE", "hq").strip().lower()
WM_ENABLED = os.getenv("WATERMARK", "1").strip().lower() not in {"0", "false", "no", "off"} and PIL_OK
WM_TEXT_MAX = 40  # حداکثر کاراکتر هر خط (پایپ‌لاین حداکثر ۳ خط می‌خونه)
_WM_HQ_LADDER = {  # بیت‌ریت‌های «شفاف» (برای استریو) از بهترین به کمتر؛ اولین گزینه‌ای که هم ≥ منبع باشد هم توی سقف ارسال جا شود
    "mp3": (320,), "aac": (320, 256), "opus": (256, 192), "vorbis": (320, 256),
}
_WM_MP3_RATES_V1 = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)  # MPEG-1 (۳۲kHz به بالا)
_WM_MP3_RATES_V2 = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)  # MPEG-2
_WM_MP4_EXT = {".m4a", ".mp4", ".m4b", ".mov", ".3gp"}


def _wm_mp3_std_rate(sr: int, kbps: int) -> int:
    """نزدیک‌ترین بیت‌ریت استانداردِ MP3 (MPEG-1 یا MPEG-2 بسته به سمپل‌ریت)"""
    rates = _WM_MP3_RATES_V1 if sr >= 32000 else _WM_MP3_RATES_V2
    return min(rates, key=lambda r: abs(r - kbps))


def _wm_pick_kbps(codec: str, src_kbps: int, ch: int, duration: float) -> int:
    """بیت‌ریت خروجی برای منبع lossy: هیچ‌وقت کمتر از منبع نیست؛ بالاترین کیفیتِ شفافی که توی سقف ارسال جا بشه.
    اگه هیچ‌کدوم جا نشد، همان بیت‌ریت منبع (رفتار قدیمی)."""
    ladder = _WM_HQ_LADDER.get(codec, ())
    scale = 0.5 if ch == 1 else max(1.0, ch / 2.0)  # مونو نصف، چندکاناله متناسب
    src = max(0, int(src_kbps or 0))
    for k in ladder:
        k = int(round(k * scale))
        if k >= src and k * 125 * duration * 1.02 <= UPLOAD_LIMIT:
            return k
    return src or (int(round(ladder[-1] * scale)) if ladder else 0)


def _wm_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


async def _wm_probe(path: Path) -> Dict[str, Any]:
    """مشخصات دقیق استریم صوتی اول: کدک، ریت، کانال، بیت‌ریت واقعی استریم و عمق بیت"""
    rc, out, err = await run(
        FFPROBE, "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bit_rate,sample_fmt,bits_per_raw_sample,bits_per_coded_sample:format=bit_rate",
        "-of", "json", str(path),
    )
    if rc != 0:
        raise ValueError("ffprobe failed: " + err.decode(errors="ignore")[-200:])
    data = json.loads(out or b"{}")
    st = (data.get("streams") or [{}])[0]
    depth = _wm_int(st.get("bits_per_raw_sample")) or _wm_int(st.get("bits_per_coded_sample"))
    if not depth:
        depth = 16 if str(st.get("sample_fmt", "")).startswith("s16") else 24
    return {
        "codec": (st.get("codec_name") or "").lower(),
        "sr": _wm_int(st.get("sample_rate")),
        "ch": _wm_int(st.get("channels")),
        "kbps": round((_wm_int(st.get("bit_rate")) or _wm_int((data.get("format") or {}).get("bit_rate"))) / 1000),
        "depth": min(depth, 24),
    }


def _wm_codec_args(info: Dict[str, Any], kbps: int, cutoff: int, suffix: str) -> Tuple[str, List[str]]:
    """(کلید فرمت برای _CONV_NO_COVER ، آرگومان‌های انکودر) = «همان کدک و همان تنظیمات» فایل اصلی"""
    c, sr, depth = info["codec"], info["sr"], info["depth"]
    if c == "mp3":
        # -cutoff = lowpass خود LAME؛ بالاتر از امضا تا فیلتر پیش‌فرض (۱۲۸k≈17k ، ۲۵۶k≈19.7k) بخشی از متن را نبُرد
        return "mp3", [
            "-c:a", "libmp3lame", "-b:a", f"{_wm_mp3_std_rate(sr, kbps)}k",
            "-cutoff", str(cutoff), "-compression_level", "2", "-id3v2_version", "3",
        ]
    if c == "aac":
        args = ["-c:a", "aac", "-b:a", f"{max(16, kbps)}k", "-cutoff", str(cutoff)]
        if suffix in _WM_MP4_EXT:
            args += ["-movflags", "+faststart"]
        return "aac", args
    if c == "opus":
        return "opus", ["-c:a", "libopus", "-b:a", f"{max(16, kbps)}k", "-vbr", "on", "-compression_level", "10",
                        "-application", "audio", "-cutoff", str(cutoff)]
    if c == "vorbis":
        return "vorbis", ["-c:a", "libvorbis", "-b:a", f"{max(16, kbps)}k", "-cutoff", str(cutoff)]
    if c == "flac":
        return "flac", ["-c:a", "flac", "-compression_level", "8"] + (
            ["-sample_fmt", "s16"] if depth <= 16 else ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"])
    if c == "alac":
        return "alac", ["-c:a", "alac"] + (
            ["-sample_fmt", "s16p"] if depth <= 16 else ["-sample_fmt", "s32p", "-bits_per_raw_sample", "24"])
    if c == "wavpack":
        return "wavpack", ["-c:a", "wavpack", "-sample_fmt", "s16" if depth <= 16 else "s32"]
    if c.startswith("pcm_"):  # WAV/AIFF: همان فرمت نمونه
        return c, ["-c:a", c]
    raise ValueError(f"spectral watermark: unsupported codec '{c}'")


def _wm_cmd(job: Job, sr: int, ch: int, enc: List[str], out: Path, with_cover: bool) -> List[str]:
    # ورودی ۰ = فایل اصلی (تگ/کاور) ؛ ورودی ۱ = PCM امضاشده از pipe (فقط صدا)
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(job.src),
           "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "pipe:0", "-map", "1:a:0"]
    if with_cover:
        cmd += ["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    # تگ‌های سراسری + تگ‌های خودِ استریم (Ogg/Opus) از فایل اصلی؛ صدای ورودی ۱ تگی ندارد
    cmd += ["-map_metadata", "0", "-map_metadata:s:a:0", "0:s:a:0"]
    cmd += enc
    cmd.append(str(out))
    return cmd


async def _wm_feed(cmd: List[str], pcm: "np.ndarray") -> Tuple[int, bytes]:
    """PCM را تکه‌تکه (۱ مگ) به stdin ffmpeg می‌فرستد؛ بدون کپی دوم در رم"""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    err_task = asyncio.ensure_future(proc.stderr.read())
    try:
        mv = memoryview(pcm.reshape(-1)).cast("B")
        step = 1 << 20
        try:
            for i in range(0, len(mv), step):
                proc.stdin.write(mv[i:i + step])
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):  # ffmpeg زودتر خطا داده؛ پیامش توی stderr است
            pass
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        err = await err_task
        await proc.wait()
        return proc.returncode, err
    except BaseException:  # لغو تسک / خطای غیرمنتظره: پروسه‌ی ffmpeg رها نشود
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        err_task.cancel()
        raise


async def watermark_encode(job: Job, text: str) -> Tuple[Path, int]:
    """
    صدا → PCM float32 (pipe) → امضای طیفی (thread جدا) → انکود با همان کدک (pipe) ؛
    فایل‌های lossless: همان کدک و تنظیمات (بدون افت). فایل‌های lossy: بسته به WM_LOSSY_MODE (hq / flac / same).
    تگ‌ها و کاور از فایل اصلی کپی میشوند. اگر حجم از UPLOAD_LIMIT بیشتر شود OutputTooBig(size) پرتاب میشود.
    فایل خروجی در job.dir ساخته میشود و پاک‌کردنش با caller است. (SEM را caller نگه می‌دارد.)
    """
    info = await _wm_probe(job.src)
    codec = info["codec"] or job.codec
    info["codec"] = codec
    sr = info["sr"] or job.sample_rate
    ch = info["ch"] or 2
    info["sr"] = sr
    if sr <= 0 or not 1 <= ch <= 8:
        raise ValueError("spectral watermark: bad audio stream")
    if job.duration * sr * ch * 4 > WM_MAX_PCM:
        raise RuntimeError("spectral watermark: track too long")

    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    src_kbps = 0 if lossless else (info["kbps"] or job.kbps)
    suffix = job.src.suffix.lower() or ".m4a"
    # بیت‌ریتِ «منبع» (گردشده به استاندارد برای MP3) فقط برای انتخاب باند/دامنه‌ی امضا استفاده میشه تا جای امضا
    # روی طیف عوض نشه؛ بیت‌ریت و لوی‌پسِ خروجی جدا و بر اساس WM_LOSSY_MODE تعیین میشه.
    plan_codec = codec
    plan_kbps = _wm_mp3_std_rate(sr, src_kbps) if codec == "mp3" else src_kbps
    out_info, out_kbps = info, src_kbps
    _, hi, _ = _wm_plan(sr, plan_codec, plan_kbps)
    cutoff = int(min(hi + 1500.0, sr / 2 * 0.99))  # رفتار قدیمی: کمی بالاتر از بالاترین خط امضا

    if not lossless and WM_LOSSY_MODE == "flac":
        # دیکودِ دقیقِ فایل کاربر + امضا، بدون هیچ انکودِ lossy دوباره (FLAC 24 بیت)
        out_info = dict(info, codec="flac", depth=24)
        out_kbps, suffix = 0, ".flac"
        plan_codec, plan_kbps = "flac", 0
        _, hi, _ = _wm_plan(sr, plan_codec, plan_kbps)
        cutoff = int(min(hi + 1500.0, sr / 2 * 0.99))
    elif not lossless and WM_LOSSY_MODE != "same":
        # hq: همان کدک، بیت‌ریتِ بالاتر از منبع، و لوی‌پس تا نزدیک نایکوئیست (هیچ بخشی از موسیقی بریده نشه)
        out_kbps = _wm_pick_kbps(codec, src_kbps, ch, job.duration)
        cutoff = int(min(sr / 2 * 0.99, 22000.0))
    key, enc = _wm_codec_args(out_info, out_kbps, cutoff, suffix)  # کدک نامعتبر: قبل از دیکود خطا

    # ۱) دیکود → PCM float32 با همان ریت/کانال؛ روی دیسک نوشته میشه (نه رم) تا هاست‌های کم‌رم نترکن
    raw_path = job.dir / f"wm_pcm_{uuid.uuid4().hex[:6]}.f32"
    out = job.dir / f"wm_{uuid.uuid4().hex[:6]}{suffix}"
    try:
        rc, _, err = await run(
            FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(job.src), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", str(ch), "-ar", str(sr), "-f", "f32le", str(raw_path),
        )
        frames = (raw_path.stat().st_size // 4 // ch) if raw_path.exists() else 0
        if rc != 0 or frames == 0:
            raise RuntimeError("watermark decode failed: " + err.decode(errors="ignore")[-200:])
        pcm = np.memmap(raw_path, dtype="<f4", mode="r+", shape=(frames, ch))

        # ۲) DSP در thread جدا (event loop بلاک نشود)؛ درجا روی همون فایلِ memmap
        wm = await asyncio.to_thread(
            generate_spectral_watermark, pcm, sr, WM_SPAN_SEC, text, plan_codec, plan_kbps, inplace=True)

        # ۳) انکود با همان کدک + کاور/تگ؛ اگه فرمت کاور نپذیرفت بدون کاور دوباره
        cover_first = job.pic_codec in _CONV_PIC_CODECS and not codec.startswith("pcm_") and key not in _CONV_NO_COVER
        err = b""
        ok = False
        for with_cover in ([True, False] if cover_first else [False]):
            with contextlib.suppress(OSError):
                out.unlink()
            rc, err = await _wm_feed(_wm_cmd(job, sr, ch, enc, out, with_cover), wm)
            if rc == 0 and out.exists() and out.stat().st_size > 0:
                if cover_first and not with_cover:
                    _CONV_NO_COVER.add(key)
                ok = True
                break
        if not ok:
            raise RuntimeError("watermark encode failed: " + err.decode(errors="ignore")[-300:])
        size = out.stat().st_size
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        return out, size
    except BaseException:
        with contextlib.suppress(OSError):
            out.unlink()
        raise
    finally:
        pcm = wm = None  # memmap بسته بشه قبل از پاک کردن فایل
        with contextlib.suppress(OSError):
            raw_path.unlink()


def render_conv_formats(job: Job, note: str = ""):
    src = f"{job.codec.upper()} · " if job.codec else ""
    text = T("cv.fmt.head", src=src, size=mb(job.size)) + "\n" + T("cv.fmt.ask")
    if note:
        text = f"{note}\n\n{text}"
    row, rows = [], []
    for fmt in conv_formats(job):
        row.append(InlineKeyboardButton(T(f"btn.cvf.{fmt}"), callback_data=f"cv:{job.id}:f:{fmt}", api_kwargs=BLUE))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back")])
    return text, InlineKeyboardMarkup(rows)


def render_conv_rates(job: Job, fmt: str, note: str = ""):
    opts = conv_options(job, fmt)
    name = CONV_INFO[fmt]["label"]
    if not opts:
        text = T("cv.rate.toolong")
    else:
        text = T("cv.rate.head", name=name)
        if fmt == "opus":
            text += "\n\n" + T("cv.rate.opus")
        if job.codec in LOSSY_CODECS:
            text += "\n\n" + T("cv.rate.lossy", kbps=fa(job.kbps))
    if note:
        text = f"{note}\n\n{text}"
    rows, row = [], []
    for token, k in opts:
        est = mb(k * 125 * job.duration)
        if token == CONV_MP3_V0:
            if row:
                rows.append(row)
                row = []
            rows.append([InlineKeyboardButton(T("btn.cv.v0", est=est), callback_data=f"cv:{job.id}:b:{fmt}:{token}", api_kwargs=BLUE)])
            continue
        row.append(InlineKeyboardButton(T("btn.cv.rate", kbps=fa(k), est=est), callback_data=f"cv:{job.id}:b:{fmt}:{token}", api_kwargs=BLUE))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(T("btn.cv.back"), callback_data=f"mn:{job.id}:cv")])
    return text, InlineKeyboardMarkup(rows)


# ───────────────────────── برش بخش دلخواه (رینگتون / پیش‌نمایش) ─────────────────────────
# قابلیت مستقل: دکمه‌ی «برش بخش دلخواه» توی منوی هر آهنگ ← کاربر زمان شروع و پایان رو می‌نویسه
# (مثلاً «1:10 1:40» یا «70-100» یا «2:00 تا آخر») ← انتخاب فرمت خروجی (MP3 / M4A / M4R رینگتون آیفون) ← ارسال.
# برش دقیقه (بدون جابه‌جایی چند صدم‌ثانیه‌ای) و با بیت‌ریت بالا انکود میشه؛ تگ‌ها و کاور آهنگ توی MP3/M4A حفظ میشن.
# تنظیمات اختیاری .env: TRIM=0 (خاموش) ، TRIM_KBPS (بیت‌ریت خروجی؛ پیش‌فرض 192)

TRIM_ENABLED = os.getenv("TRIM", "1").strip().lower() not in {"0", "false", "no", "off"}
TRIM_KBPS = min(320, max(64, _int("TRIM_KBPS", 192)))
TRIM_VOICE_KBPS = min(64, max(16, _int("TRIM_VOICE_KBPS", 32)))  # بیت‌ریت Opus برای حالت ویس
TRIM_MIN_SEC = 1.0  # کوتاه‌ترین برش (ثانیه)
TRIM_M4R_MAX = 40.0  # سقف طول رینگتون آیفون (ثانیه)
TRIM_FADE_SEC = 2.0  # حداکثر طول محو شدن ابتدا/انتها (برای برش‌های کوتاه، یک‌چهارم طول برش)
TRIM_INFO: Dict[str, Dict[str, str]] = {
    "mp3": {"label": "MP3", "ext": "mp3", "btn": "🎵 MP3"},
    "aac": {"label": "M4A", "ext": "m4a", "btn": "🎼 M4A (AAC)"},
    "m4r": {"label": "M4R", "ext": "m4r", "btn": "📱 M4R (رینگتون آیفون)"},
    "voice": {"label": "ویس", "ext": "ogg", "btn": "🎙 به‌صورت ویس"},
}
# ارقام فارسی/عربی و ممیز عربی ← لاتین
_TR_DIGITS = {
    **{0x06F0 + i: 0x30 + i for i in range(10)},
    **{0x0660 + i: 0x30 + i for i in range(10)},
    0x066B: 0x2E,
}
_TR_TOKEN = re.compile(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?")
_TR_TO_END = re.compile(r"آخر|پایان|انتها|\bend\b", re.I)


def tr_time(sec: float) -> str:
    """ثانیه ← m:ss (یا h:mm:ss)؛ اگه کسر داشت با یه رقم اعشار (مثلاً 1:05.5)."""
    sec = round(max(0.0, float(sec)), 1)
    whole = int(sec)
    tenth = int(round((sec - whole) * 10))
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    txt = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{txt}.{tenth}" if tenth else txt


def _tr_to_sec(tok: str) -> float:
    vals = [float(p) for p in tok.split(":")]
    if len(vals) > 1 and any(v >= 60 for v in vals[1:]):
        raise ValueError("bad time")
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return sec


def tr_parse(text: str, duration: float) -> Tuple[float, float, bool]:
    """متن کاربر ← (شروع، پایان، پایان_به_طول_آهنگ_بریده_شد). خطا = ValueError با پیام فارسی آماده.
    فرمت‌های قابل‌قبول: «0:30 1:00» ، «1:10-1:40» ، «45 75» (ثانیه) ، «2:00 تا آخر» (ارقام فارسی هم اوکیه)."""
    t = text.translate(_TR_DIGITS)
    toks = _TR_TOKEN.findall(t)
    usage = T("tr.e.usage")
    try:
        if len(toks) == 2:
            start, end = _tr_to_sec(toks[0]), _tr_to_sec(toks[1])
        elif len(toks) == 1 and _TR_TO_END.search(t):
            start, end = _tr_to_sec(toks[0]), duration
        else:
            raise ValueError(usage)
    except ValueError as e:
        raise ValueError(usage) from e
    dur_txt = fa(tr_time(duration))
    if start >= duration - 0.05:
        raise ValueError(T("tr.e.start", dur=dur_txt))
    if end <= start:
        raise ValueError(T("tr.e.end"))
    clamped = end > duration + 0.05
    end = min(end, duration)
    if end - start < TRIM_MIN_SEC:
        raise ValueError(T("tr.e.min", min=fa(int(TRIM_MIN_SEC))))
    return start, end, clamped


def render_trim_prompt(job: Job, note: str = ""):
    text = (
        T("tr.prompt.head") + "\n"
        + T("tr.prompt.body", duration=fa(tr_time(job.duration)), m4r_max=fa(int(TRIM_M4R_MAX)))
    )
    if note:
        text = f"{note}\n\n{text}"
    rows = []
    if job.tr_start is not None and job.tr_end is not None:  # قبلاً برشی انتخاب شده؛ میشه بدون تغییر برگشت
        rows.append([InlineKeyboardButton(T("btn.cancel_x"), callback_data=f"tr:{job.id}:x", api_kwargs=STYLE_RED)])
    rows.append([InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back")])
    return text, InlineKeyboardMarkup(rows)


def render_trim_formats(job: Job, note: str = ""):
    if job.tr_start is None or job.tr_end is None:
        return render_trim_prompt(job, note)
    s, e = job.tr_start, job.tr_end
    length = e - s
    text = (
        T("tr.fmt.head") + "\n"
        + T("tr.fmt.body", start=fa(tr_time(s)), end=fa(tr_time(e)), length=fa(tr_time(length)),
            fade=T("tr.fade.on") if job.tr_fade else T("tr.fade.off"))
    )
    rows = [[
        InlineKeyboardButton(T("btn.tr.mp3"), callback_data=f"tr:{job.id}:f:mp3", api_kwargs=BLUE),
        InlineKeyboardButton(T("btn.tr.aac"), callback_data=f"tr:{job.id}:f:aac", api_kwargs=BLUE),
    ]]
    if length <= TRIM_M4R_MAX + 0.05:
        rows.append([InlineKeyboardButton(T("btn.tr.m4r"), callback_data=f"tr:{job.id}:f:m4r", api_kwargs=BLUE)])
    else:
        text += "\n\n" + T("tr.fmt.m4r_note", m4r_max=fa(int(TRIM_M4R_MAX)))
    rows.append([InlineKeyboardButton(T("btn.tr.voice"), callback_data=f"tr:{job.id}:f:voice", api_kwargs=BLUE)])
    rows.append([InlineKeyboardButton(
        T("btn.tr.fade_on") if job.tr_fade else T("btn.tr.fade_off"), callback_data=f"tr:{job.id}:fd",
        api_kwargs=BLUE)])
    rows.append([
        InlineKeyboardButton(T("btn.tr.change"), callback_data=f"tr:{job.id}:t"),
        InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back"),
    ])
    if note:
        text = f"{note}\n\n{text}"
    return text, InlineKeyboardMarkup(rows)


def _trim_cmd(job: Job, fmt: str, start: float, length: float, fade: bool, out: Path, with_cover: bool) -> List[str]:
    # -ss/-t قبل از -i = برش دقیق روی ورودی؛ کاور از ورودی دوم (بدون seek) میاد تا موقع برش گم نشه
    cmd = [FFMPEG, "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(job.src)]
    if with_cover:
        cmd += ["-i", str(job.src)]
    cmd += ["-map", "0:a:0"]
    if with_cover:
        cmd += ["-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    cmd += ["-map_metadata", "0"]
    if fade:
        f = min(TRIM_FADE_SEC, length / 4)
        cmd += ["-af", f"afade=t=in:st=0:d={f:.2f},afade=t=out:st={length - f:.3f}:d={f:.2f}"]
    if fmt == "mp3":
        rate = _conv_rate(job.sample_rate, "mp3")
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{TRIM_KBPS}k", "-id3v2_version", "3"]
    elif fmt == "voice":
        rate = None  # ویس تلگرام همیشه مونو و ۴۸کیلوهرتزه؛ جدا پایین ست میشه
        cmd += ["-c:a", "libopus", "-b:a", f"{TRIM_VOICE_KBPS}k", "-ac", "1", "-ar", "48000", "-vbr", "on"]
    else:
        rate = _conv_rate(job.sample_rate, "aac")
        cmd += ["-c:a", "aac", "-b:a", f"{TRIM_KBPS}k"]
        cmd += ["-f", "ipod"] if fmt == "m4r" else ["-movflags", "+faststart"]
    if rate:
        cmd += ["-ar", str(rate)]
    cmd.append(str(out))
    return cmd


async def trim_cut(job: Job, fmt: str, start: float, end: float, fade: bool) -> Tuple[Path, int]:
    """برش با ffmpeg. کاور داخل فایل (JPEG/PNG) توی MP3/M4A کپی میشه؛ اگه نشد، بدون کاور دوباره تلاش می‌کنه."""
    length = end - start
    out = job.dir / f"cut_{uuid.uuid4().hex[:6]}.{TRIM_INFO[fmt]['ext']}"
    cover_first = fmt not in ("m4r", "voice") and job.pic_codec in _CONV_PIC_CODECS  # رینگتون و ویس کاور نمیخوان
    err = b""
    for with_cover in ([True, False] if cover_first else [False]):
        with contextlib.suppress(OSError):
            out.unlink()
        rc, _, err = await run(*_trim_cmd(job, fmt, start, length, fade, out, with_cover))
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            return out, out.stat().st_size
    raise RuntimeError(err.decode(errors="ignore")[-300:])


async def trim_apply(job: Job, context: ContextTypes.DEFAULT_TYPE, message, fmt: str) -> None:
    """برش + ارسال. بعدش job سر جاش می‌مونه تا بشه همون برش رو به فرمت دیگه‌ای هم گرفت یا کار دیگه‌ای کرد."""
    assert job.tr_start is not None and job.tr_end is not None
    start, end, fade = job.tr_start, job.tr_end, job.tr_fade
    length = end - start
    info = TRIM_INFO[fmt]
    job.busy = True
    st = edit_bg(message, T("tr.progress", start=fa(tr_time(start)), end=fa(tr_time(end)), label=info["label"]))
    out: Optional[Path] = None
    note = ""
    ok = False
    try:
        async with SEM:
            out, size = await trim_cut(job, fmt, start, end, fade)
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        cap = T("tr.caption", start=fa(tr_time(start)), end=fa(tr_time(end)), length=fa(tr_time(length)),
                label=info["label"], size=_cl_kb(size))
        common = dict(
            chat_id=message.chat_id, filename=f"{_cl_safe_name(job.name + ' - cut')}.{info['ext']}",
            caption=cap, reply_markup=more_markup(job, "tr"), read_timeout=300, write_timeout=300, connect_timeout=60,
        )
        with out.open("rb") as f:
            if fmt == "m4r":  # تلگرام M4R رو آهنگ حساب نمیکنه؛ به‌صورت فایل میره
                sent = await context.bot.send_document(document=f, **common)
            elif fmt == "voice":  # پیام صوتیِ گرد؛ بدون تایتل/خواننده/کاور (ویس اصلاً نداره)
                sent = await context.bot.send_voice(voice=f, duration=int(round(length)), **common)
            else:
                try:
                    sent = await context.bot.send_audio(
                        audio=f, title=job.title, performer=job.artist or None, duration=int(round(length)),
                        thumbnail=job.thumb.read_bytes() if job.thumb else None, **common,
                    )
                except BadRequest:
                    f.seek(0)
                    sent = await context.bot.send_document(document=f, **common)
        count_file(job.user_id, "out")
        note = T("tr.done", label=info["label"])
        ok = True
        await after_delivery(job, "tr", sent)
    except OutputTooBig as e:
        log.warning("trimmed output %s bytes exceeds upload limit", e)
        note = T("tr.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("trim failed")
        note = T("tr.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:
            job.created = time.time()
            if ok:
                text, markup = panel_done_text(note), None
            else:
                text, markup = render_trim_formats(job, note)
            await safe_edit(message, text, markup)


# ───────────────────────── نرمال‌سازی لودنس (حدود -14 LUFS) ─────────────────────────
# قابلیت مستقل: دکمه‌ی «نرمال‌سازی لودنس» توی منوی هر آهنگ ← لودنس فعلی (EBU R128) اندازه‌گیری و نشون داده میشه
# ← انتخاب فرمت خروجی ← فایل با لودنس هدف (پیش‌فرض -14 LUFS، استاندارد استریم) ارسال میشه.
# روش: یه گِین ثابت (volume) = هدف − لودنس فعلی؛ پس دینامیک آهنگ دست‌نخورده می‌مونه. اگه بالابردن صدا باعث
# رد‌شدن پیک از سقف -1.5 dBTP بشه، یه لیمیتر ملایم هم اضافه میشه. بعد از ساخت فایل، لودنس خروجی دوباره اندازه‌گیری
# و توی کپشن نوشته میشه (اگه لیمیتر لازم بوده، ممکنه کمی پایین‌تر از هدف بمونه).
# تنظیمات اختیاری .env: NORMALIZE=0 (خاموش) ، NORMALIZE_LUFS (هدف؛ پیش‌فرض -14)

NORM_ENABLED = os.getenv("NORMALIZE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _norm_target() -> float:
    try:
        v = float(os.getenv("NORMALIZE_LUFS", "-14").strip().replace(",", "."))
    except ValueError:
        return -14.0
    return v if -30.0 <= v <= -5.0 else -14.0


NORM_TARGET = _norm_target()
NORM_TOLERANCE = 1.0  # اگه لودنس فعلی تا این‌قدر (LU) به هدف نزدیکه، نرمال‌سازی لازم نیست
NORM_CEIL_DB = -1.5  # سقف پیک بعد از نرمال‌سازی (dBFS)
NORM_MAX_GAIN = 20.0  # حداکثر تغییر صدا (dB)؛ آهنگ‌های تقریباً ساکت بیشتر از این بالا نمیرن


def _norm_measure(path: Path, cached: bool = True) -> Optional[Dict[str, float]]:
    """لودنس EBU R128 کل فایل (همگام) ← {'lufs', 'lra', 'true_peak'} یا None.
    cached=True: اگه همین فایل قبلاً (مثلاً توی آنالیز یا پیش‌محاسبه) اندازه‌گیری شده باشه، از همون نتیجه استفاده میشه."""
    errors: List[str] = []
    ffmpeg, _ = _resolve_binaries()
    if cached:
        res, errors = _piece_loudness(path, ffmpeg)
    else:
        res = _loudness(path, ffmpeg, errors)
    if res is None and errors:
        log.warning("loudness measure failed: %s", errors)
    return res


def norm_filter(loud: Dict[str, float]) -> Tuple[str, float, bool]:
    """(فیلتر ffmpeg، گین dB، لیمیتر لازمه؟)"""
    gain = max(-NORM_MAX_GAIN, min(NORM_MAX_GAIN, NORM_TARGET - loud["lufs"]))
    af = f"volume={gain:.2f}dB"
    tp = loud.get("true_peak")
    limited = gain > 0 and (tp is None or tp + gain > NORM_CEIL_DB)
    if limited:  # level=0: بدون auto-level (وگرنه لیمیتر خودش دوباره صدا رو نرمال میکنه)
        af += f",alimiter=limit={10 ** (NORM_CEIL_DB / 20):.4f}:level=0"
    return af, gain, limited


def norm_needed(loud: Dict[str, float]) -> bool:
    return abs(NORM_TARGET - loud["lufs"]) >= NORM_TOLERANCE


def _norm_kbps(job: Job, lo: int, hi: int) -> str:
    return str(min(hi, max(lo, job.kbps)))


def norm_options(job: Job) -> List[Tuple[str, str]]:
    """[(فرمت، توکن)]؛ اولی = همون فرمت آهنگ اصلی (لاسلس ← FLAC ، Opus ← Opus ، MP3 ← MP3 ، بقیه ← M4A)."""
    c = job.codec
    if conv_is_lossless(job):
        same = ("flac", "")
    elif c == "mp3":
        same = ("mp3", _norm_kbps(job, 128, 320))
    elif c == "opus":
        same = ("opus", _norm_kbps(job, 64, 256))
    else:
        # انکودر AAC داخلی ffmpeg حدود ۱۰٪ کمتر از بیت‌ریتِ درخواستی خروجی میده؛ برای اینکه حجم/کیفیت از منبع کمتر نشه ۲۵٪ بالاتر میگیریم
        same = ("aac", str(min(320, max(128, int(job.kbps * 1.25)))))
    opts = [same]
    for fmt, lo in (("mp3", 192), ("aac", 192)):
        if fmt != same[0]:
            opts.append((fmt, _norm_kbps(job, lo, 320)))
    # خروجی‌های لاسی که از سقف ارسال تلگرام بزرگ‌تر میشن نمایش داده نمیشن
    return [(f, t) for f, t in opts if f == "flac" or int(t) * 125 * job.duration * 1.02 <= UPLOAD_LIMIT]


def _norm_word(lufs: float) -> str:
    d = lufs - NORM_TARGET
    if d >= 6:
        return T("nm.w.vloud")
    if d >= NORM_TOLERANCE:
        return T("nm.w.loud")
    if d > -NORM_TOLERANCE:
        return T("nm.w.near")
    if d > -6:
        return T("nm.w.quiet")
    return T("nm.w.vquiet")


def render_norm(job: Job, note: str = ""):
    back = InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back")
    loud = job.loud
    if not loud:
        text = T("nm.noloud")
        return (f"{note}\n\n{text}" if note else text), InlineKeyboardMarkup([[back]])
    lufs, tgt = loud["lufs"], NORM_TARGET
    lines = [
        T("nm.head"),
        T("nm.cur", lufs=fa(f'{lufs:.1f}'), word=_norm_word(lufs)),
        T("nm.target", target=fa(f'{tgt:.0f}' if float(tgt).is_integer() else f'{tgt:.1f}')),
    ]
    if loud.get("true_peak") is not None:
        lines.append(T("nm.peak", peak=fa(f'{loud['true_peak']:+.1f}')))
    if not norm_needed(loud):
        lines += ["", T("nm.ok")]
        rows = [[back]]
    else:
        _, gain, limited = norm_filter(loud)
        lines += ["", T("nm.gain", gain=fa(f'{gain:+.1f}'))]
        if limited:
            lines.append(T("nm.limited"))
        if abs(NORM_TARGET - lufs) > NORM_MAX_GAIN:
            lines.append(T("nm.maxgain", max=fa(int(NORM_MAX_GAIN))))
        if job.codec in LOSSY_CODECS:
            lines.append(T("nm.lossy"))
        lines += ["", T("nm.ask")]
        opts = norm_options(job)
        rows = []
        for i, (fmt, tok) in enumerate(opts):
            label = conv_label(fmt, tok)
            if i == 0:
                label = T("btn.nm.same", label=label)
            rows.append(InlineKeyboardButton(label, callback_data=f"nm:{job.id}:g:{fmt}:{tok}", api_kwargs=BLUE))
        rows = [rows[:1]] + ([rows[1:]] if len(rows) > 1 else [])
        rows.append([back])
    text = "\n".join(lines)
    if note:
        text = f"{note}\n\n{text}"
    return text, InlineKeyboardMarkup(rows)


async def norm_show(message, job: Job, note: str = "") -> None:
    """لودنس رو (اگه قبلاً اندازه‌گیری نشده) میگیره و صفحه‌ی نرمال‌سازی رو نشون میده."""
    if job.loud is None:
        job.busy = True
        st = edit_bg(message, T("nm.measuring"))
        try:
            async with SEM:
                job.loud = await asyncio.to_thread(_norm_measure, job.src)  # اگه پیش‌محاسبه آماده باشه تقریباً آنیه
        except Exception:  # noqa: BLE001
            log.exception("loudness measure crashed")
            job.loud = None
        finally:
            await settle(st)
            job.busy = False
    if job.id not in JOBS:
        return
    job.created = time.time()
    text, markup = render_norm(job, note)
    await safe_edit(message, text, markup)


async def norm_apply(job: Job, context: ContextTypes.DEFAULT_TYPE, message, fmt: str, token: str) -> None:
    """نرمال‌سازی + ارسال. job سر جاش می‌مونه تا بشه فرمت دیگه‌ای هم گرفت یا کار دیگه‌ای کرد."""
    assert job.loud is not None
    before = job.loud["lufs"]
    af, gain, limited = norm_filter(job.loud)
    label = conv_label(fmt, token)
    job.busy = True
    st = edit_bg(message, T("nm.progress", target=fa(f'{NORM_TARGET:.0f}'), label=label))
    out: Optional[Path] = None
    note = ""
    ok = False
    try:
        async with SEM:
            out, size = await convert(job, fmt, token, af=af)
            after = await asyncio.to_thread(_norm_measure, out, False)  # لودنس واقعی خروجی (برای کپشن؛ بدون کش)
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        cap = [
            T("nm.cap_head"),
            (T("nm.cap_after", before=fa(f'{before:.1f}'), after=fa(f'{after['lufs']:.1f}')) if after
             else T("nm.cap_target", before=fa(f'{before:.1f}'), target=fa(f'{NORM_TARGET:.0f}'))),
            T("nm.cap_fmt", label=label, size=mb(size)),
        ]
        if limited:
            cap.append(T("nm.cap_limited"))
        common = dict(
            chat_id=message.chat_id, filename=f"{_cl_safe_name(job.name + ' - normalized')}.{CONV_INFO[fmt]['ext']}",
            caption="\n".join(cap), reply_markup=more_markup(job, "nm"),
            read_timeout=300, write_timeout=300, connect_timeout=60,
        )
        with out.open("rb") as f:
            try:
                sent = await context.bot.send_audio(
                    audio=f, title=job.title, performer=job.artist or None, duration=int(job.duration),
                    thumbnail=job.thumb.read_bytes() if job.thumb else None, **common,
                )
            except BadRequest:  # فرمتی که تلگرام به‌عنوان آهنگ قبول نمیکنه ← به‌صورت فایل
                f.seek(0)
                sent = await context.bot.send_document(document=f, **common)
        count_file(job.user_id, "out")
        note = T("nm.done")
        ok = True
        await after_delivery(job, "nm", sent)
    except OutputTooBig as e:
        log.warning("normalized output %s bytes exceeds upload limit", e)
        note = T("nm.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("normalize failed")
        note = T("nm.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:
            job.created = time.time()
            if ok:
                text, markup = panel_done_text(note), None
            else:
                text, markup = render_norm(job, note)
            await safe_edit(message, text, markup)


# ───────────────────────── افزایش بیس (طبیعی) ─────────────────────────
# قابلیت مستقل: دکمه‌ی «افزایش بیس» توی منوی هر آهنگ ← انتخاب شدت (ملایم / متعادل / قوی) ← فایل با همون فرمتِ آهنگ اصلی ارسال میشه.
# اصل کار (روش مسترینگ، نه «بیس‌بوستر ارزون»):
#   ۱) هایپس ۲۸Hz: انرژیِ زیرِ شنوایی که فقط headroom رو میخوره و لیمیتر رو به پمپ‌کردن میندازه حذف میشه.
#   ۲) تقویتِ باریک: یه زنگوله‌ی نرم دور ۶۰Hz (بدنه‌ی کیک و بیس) + یه شلف خیلی ملایم از ~۱۰۰Hz ← بالای ۲۰۰Hz تقریباً دست‌نخورده
#      (وکال و ساز‌ها همون‌جا که بودن می‌مونن).
#   ۳) تنظیم خودکار: اگه آهنگ از قبل بیس زیادی داره (اندازه‌گیری انرژی زیر ۱۴۰Hz نسبت به کل)، تقویت تا نصف کم میشه.
#   ۴) جبران صدا: چون بیس انرژی اضافه میکنه، ولوم کل ۶۰٪ همون افزایش پایین میاد تا آهنگ «بلندتر» نشه و وکال له نشه.
#   ۵) فقط اگه پیکِ بیس از سقف رد بشه، یه لیمیتر ملایم (حداکثر حدود ۳ دسی‌بل) جلوی کلیپ‌شدن رو میگیره.
# تنظیم اختیاری .env: BASS_BOOST=0 (خاموش)

BASS_ENABLED = os.getenv("BASS_BOOST", "1").strip().lower() not in {"0", "false", "no", "off"}
try:
    BASS_EXTREME_DB = max(10.0, min(24.0, float(os.getenv("BASS_EXTREME_DB", "18"))))  # شدت «خیلی قوی» (dB)؛ با .env قابل تغییره
except ValueError:
    BASS_EXTREME_DB = 18.0
BASS_LEVELS: Dict[str, float] = {"soft": 3.0, "mid": 5.0, "strong": 7.0, "extreme": BASS_EXTREME_DB}  # شدت ← dB (در حوالی ۶۰Hz)
BASS_HEAVY_DB = 12.0  # بالاتر از این (فقط «خیلی قوی») حالت سنگین: ساب‌بیس اضافه، تنظیم خودکار کمتر، لیمیتر آزادتر
BASS_SPLIT_HZ = 140  # محدوده‌ای که «بیس» حساب میشه (برای تشخیص آهنگ‌های از قبل بم‌دار)
BASS_COMP = 0.6  # چند درصد از افزایشِ لودنس با پایین‌آوردن ولوم جبران بشه
BASS_MAX_LIMIT_DB = 3.0  # حداکثر کاهشی که لیمیتر مجازه روی پیک‌ها اعمال کنه


def bass_chain(gain: float) -> str:
    """فیلتر ffmpeg برای تقویت طبیعی بیس (gain = dB حوالی ۶۰Hz)."""
    chain = (
        "highpass=f=28"
        f",equalizer=f=60:t=q:w=1.0:g={gain * 0.6:.2f}"
        f",bass=g={gain * 0.5:.2f}:f=100:t=s:w=0.5"
    )
    if gain > BASS_HEAVY_DB:  # حالت سنگین: یه زنگوله‌ی اضافه روی ساب‌بیس (~۴۲Hz) برای بیس عمیق و پر
        chain += f",equalizer=f=42:t=q:w=1.2:g={(gain - BASS_HEAVY_DB) * 0.8:.2f}"
    return chain


def _bass_measure(path: Path, pre_af: str = "") -> Optional[Dict[str, float]]:
    """لودنس EBU R128 و True Peak بعد از اعمال pre_af (بدون ساخت فایل)؛ نشد ← None."""
    ffmpeg, _ = _resolve_binaries()
    num = r"(-?\d+(?:\.\d+)?)"
    flt = (pre_af + "," if pre_af else "") + "ebur128=peak=true:framelog=quiet"
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
           "-vn", "-sn", "-dn", "-af", flt, "-f", "null", "-"]
    try:
        _, _, err = _run(cmd, EBU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    txt = err.decode("utf-8", "ignore")
    k = txt.rfind("Summary:")
    if k < 0:
        return None
    tail = txt[k:]
    res: Dict[str, float] = {}
    for key, pat in (("lufs", rf"\n\s+I:\s+{num}\s+LUFS"), ("true_peak", rf"\n\s+Peak:\s+{num}\s+dBFS")):
        m = re.search(pat, tail)
        if m:
            res[key] = float(m.group(1))
    return res if "lufs" in res and -69.9 < res["lufs"] < 0.0 else None


def bass_plan(job: Job, level_db: float) -> Dict[str, Any]:
    """اندازه‌گیری + ساخت فیلتر نهایی ← {'af', 'db' (تقویت واقعی), 'auto' (تقویت خودکار کم شد؟), 'limited'}
    ولوم میان‌ و بالای آهنگ دست‌نخورده می‌مونه (همون ولوم فایل اصلی)؛ فقط بیس اضافه میشه.
    پیک‌های اضافه رو لیمیتر میگیره؛ فقط اگه لیمیتر بیشتر از سقف مجاز لازم شد، ولوم کل به‌اندازه‌ی باقی‌مونده کم میشه."""
    loud0 = job.loud or _norm_measure(job.src)
    heavy = level_db > BASS_HEAVY_DB  # «خیلی قوی»: کاربر بیس خالص و سنگین میخواد، پس تقویت کمتر کم نشه
    factor, auto = 1.0, False
    if loud0:
        low = _bass_measure(job.src, f"lowpass=f={BASS_SPLIT_HZ}")
        if low:
            ratio = low["lufs"] - loud0["lufs"]  # هرچه به صفر نزدیک‌تر ← بم‌دارتر (مثلاً -6 = خیلی بم‌دار، -16 = نازک)
            factor = min(1.0, max(0.85 if heavy else 0.5, 1.0 + (-10.0 - ratio) * 0.1))
            auto = factor < 0.95
    gain = level_db * factor
    chain = bass_chain(gain)
    after = _bass_measure(job.src, chain) if loud0 else None
    limited = False
    ceil = NORM_CEIL_DB
    if loud0 and after and after.get("true_peak") is not None:
        # سقف = پیکِ فایل اصلی (حداکثر -0.8) تا خروجی هیچ‌وقت بلندتر از اصل پیک نزنه
        ceil = max(NORM_CEIL_DB, min(loud0.get("true_peak", NORM_CEIL_DB), -0.8))
        over = after["true_peak"] - ceil  # پیک‌های بیس چند دسی‌بل از سقف بالاترن
        g = 0.0
        if over > 0:
            limited = True
            g = -max(0.0, over - (BASS_MAX_LIMIT_DB + (2.0 if heavy else 0.0)))  # فقط مازادِ تحمل لیمیتر ولوم رو کم میکنه
        g = max(-12.0, g)
    else:  # اندازه‌گیری نشد ← محافظه‌کارانه: کمی ولوم کم + لیمیتر
        g, limited = -0.4 * gain, True
    af = chain + (f",volume={g:.2f}dB" if abs(g) >= 0.05 else "")
    if limited:  # level=0: بدون auto-level (وگرنه لیمیتر خودش دوباره صدا رو نرمال میکنه)
        af += f",alimiter=limit={10 ** (ceil / 20):.4f}:attack=5:release=150:level=0"
    return {"af": af, "db": gain, "auto": auto, "limited": limited}


def _bass_name(lvl: str) -> str:
    return T(f"bs.w.{lvl}")


def render_bass(job: Job, note: str = ""):
    back = InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back")
    fmt, tok = norm_options(job)[0]
    lines = [T("bs.head"), T("bs.about")]
    if job.codec in LOSSY_CODECS:
        lines.append(T("bs.lossy"))
    lines += ["", T("bs.out", label=conv_label(fmt, tok)), "", T("bs.ask")]
    rows = [
        [InlineKeyboardButton(T(f"btn.bs.{lvl}", db=fa(f"{db:g}")), callback_data=f"bs:{job.id}:g:{lvl}", api_kwargs=BLUE)]
        for lvl, db in BASS_LEVELS.items()
    ]
    rows.append([back])
    text = "\n".join(lines)
    if note:
        text = f"{note}\n\n{text}"
    return text, InlineKeyboardMarkup(rows)


async def bass_apply(job: Job, context: ContextTypes.DEFAULT_TYPE, message, lvl: str) -> None:
    """افزایش بیس + ارسال. job سر جاش می‌مونه تا بشه کار دیگه‌ای هم کرد."""
    fmt, token = norm_options(job)[0]
    label = conv_label(fmt, token)
    name = _bass_name(lvl)
    job.busy = True
    st = edit_bg(message, T("bs.progress", lvl=name))
    out: Optional[Path] = None
    note = ""
    ok = False
    try:
        async with SEM:
            plan = await asyncio.to_thread(bass_plan, job, BASS_LEVELS[lvl])
            out, size = await convert(job, fmt, token, af=plan["af"])
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        cap = [
            T("bs.cap_head"),
            T("bs.cap_level", lvl=name, db=fa(f"{plan['db']:.1f}")),
            T("bs.cap_fmt", label=label, size=mb(size)),
        ]
        if plan["auto"]:
            cap.append(T("bs.cap_auto"))
        if plan["limited"]:
            cap.append(T("bs.cap_limited"))
        if lvl == "extreme":
            cap.append(T("bs.cap_system"))
        common = dict(
            chat_id=message.chat_id, filename=f"{_cl_safe_name(job.name + ' - bass')}.{CONV_INFO[fmt]['ext']}",
            caption="\n".join(cap), reply_markup=more_markup(job, "bs"),
            read_timeout=300, write_timeout=300, connect_timeout=60,
        )
        with out.open("rb") as f:
            try:
                sent = await context.bot.send_audio(
                    audio=f, title=job.title, performer=job.artist or None, duration=int(job.duration),
                    thumbnail=job.thumb.read_bytes() if job.thumb else None, **common,
                )
            except BadRequest:  # فرمتی که تلگرام به‌عنوان آهنگ قبول نمیکنه ← به‌صورت فایل
                f.seek(0)
                sent = await context.bot.send_document(document=f, **common)
        count_file(job.user_id, "out")
        note = T("bs.done")
        ok = True
        await after_delivery(job, "bs", sent)
    except OutputTooBig as e:
        log.warning("bass output %s bytes exceeds upload limit", e)
        note = T("bs.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("bass boost failed")
        note = T("bs.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:
            job.created = time.time()
            if ok:
                text, markup = panel_done_text(note), None
            else:
                text, markup = render_bass(job, note)
            await safe_edit(message, text, markup)


async def on_bass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """bs:{job}:g:{level} ← افزایش بیس با شدت انتخابی و ارسال"""
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) != 4 or parts[2] != "g" or parts[3] not in BASS_LEVELS:
        await q.answer()
        return
    job = JOBS.get(parts[1])
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if not BASS_ENABLED:
        await q.answer(T("alert.disabled"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()
    await q.answer()
    await bass_apply(job, context, q.message, parts[3])


# ───────────────────────── ویرایش تگ و کاور ─────────────────────────
# قابلیت مستقل: دکمه‌ی «ویرایش تگ و کاور» توی منوی هر آهنگ ← انتخاب بخش (عنوان / خواننده / آلبوم / کاور)
# ← ربات منتظر پیام بعدی کاربر میمونه (متن یا عکس) ← «ساخت و ارسال فایل».
# ویرایش با mutagen و بدون بازکدگذاری صداست؛ کیفیت آهنگ و بقیه‌ی تگ‌ها (مثل ترانه) دست‌نخورده می‌مونن.
# فرمت‌های پشتیبانی‌شده: MP3 / M4A (MP4) / FLAC / Ogg Vorbis / Opus
# تنظیم اختیاری .env: TAG_EDIT=0 (خاموش)

TAGS_ENABLED = os.getenv("TAG_EDIT", "1").strip().lower() not in {"0", "false", "no", "off"} and mutagen is not None
TG_MAX_TEXT = 200  # حداکثر طول عنوان/خواننده/آلبوم
TG_COVER_MAX_PX = 1600  # کاورهای بزرگ‌تر از این (ضلع بلندتر) کوچیک میشن
TG_COVER_MAX_BYTES = 3 * MB  # کاورهای سنگین‌تر از این دوباره JPEG میشن
TG_COVER_IN_LIMIT = 20 * MB  # سقف حجم عکس ورودی
_TG_FIELDS = {"title": ("🎵", "عنوان"), "artist": ("🎤", "خواننده"), "album": ("💿", "آلبوم")}
_TG_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_TG_EMPTY: Dict[str, Any] = {"kind": None, "title": "", "artist": "", "album": "", "has_cover": False}


def _tg_kind(audio: Any) -> Optional[str]:
    """نوع تگ قابل‌ویرایش: id3 | mp4 | flac | ogg؛ بقیه‌ی فرمت‌ها None (پشتیبانی نمیشن)."""
    if audio is None:
        return None
    name = type(audio).__name__
    if name == "MP3":
        return "id3"
    if name == "MP4":
        return "mp4"
    if name == "FLAC":
        return "flac"
    if name in ("OggVorbis", "OggOpus"):
        return "ogg"
    return None


def tg_read(path: Path) -> Dict[str, Any]:
    """تگ‌های فعلی فایل (برای نمایش توی صفحه‌ی ویرایش)."""
    info = dict(_TG_EMPTY)
    audio = _cl_open(path)
    kind = _tg_kind(audio)
    if kind is None:
        return info
    info["kind"] = kind
    basic = _cl_basic_tags(path)
    for f in _TG_FIELDS:
        info[f] = (basic.get(f) or "").strip()
    try:
        found = _find_cover(audio, audio.tags)
        info["has_cover"] = bool(found and found[0])
    except Exception:  # noqa: BLE001
        pass
    return info


def _tg_vc_set(tags: Any, key: str, val: str) -> None:
    """تنظیم/پاک‌کردن یه کلید Vorbis comment (FLAC/Ogg)."""
    if val:
        tags[key] = [val]
    else:
        with contextlib.suppress(KeyError):
            del tags[key]


def _tg_picture(data: bytes, mime: str) -> Any:
    pic = Picture()
    pic.type = 3  # Cover (front)
    pic.mime = mime
    pic.desc = ""
    pic.data = data
    w, h = _cl_img_size(data)
    pic.width, pic.height, pic.depth = int(w or 0), int(h or 0), 24
    return pic


def tg_write(
    src: Path, dst: Path, edits: Dict[str, str], cover: Optional[Tuple[bytes, str]], drop_cover: bool
) -> None:
    """src را کپی میکنه توی dst و تغییرات رو روی کپی اعمال میکنه (فایل اصلی دست نمی‌خوره).
    edits: {"title"|"artist"|"album": مقدار} (مقدار خالی = پاک‌کردن تگ) ، cover: (بایت‌ها، mime) ، drop_cover: حذف کاور."""
    shutil.copyfile(src, dst)
    audio = mutagen.File(str(dst))
    kind = _tg_kind(audio)
    if kind is None:
        raise ValueError("unsupported format for tag editing")
    change_cover = bool(cover) or drop_cover

    if kind == "id3":
        from mutagen.id3 import APIC, TALB, TIT2, TPE1

        with dst.open("rb") as f:
            head = f.read(4)
        v4 = head[:3] == b"ID3" and head[3:4] == b"\x04"  # نسخه‌ی ID3 فایل رو حفظ میکنیم (پیش‌فرض 2.3 برای سازگاری)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        frames = {"title": TIT2, "artist": TPE1, "album": TALB}
        for fld, val in edits.items():
            tags.delall(frames[fld].__name__)
            if val:
                tags.add(frames[fld](encoding=1, text=[val]))  # UTF-16؛ هم توی 2.3 هم 2.4 معتبره
        if change_cover:
            tags.delall("APIC")
            if cover:
                tags.add(APIC(encoding=0, mime=cover[1], type=3, desc="Cover", data=cover[0]))
        if not v4:
            tags.update_to_v23()
        audio.save(v2_version=4 if v4 else 3)

    elif kind == "mp4":
        from mutagen.mp4 import MP4Cover

        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb"}
        for fld, val in edits.items():
            if val:
                tags[keys[fld]] = [val]
            elif keys[fld] in tags:
                del tags[keys[fld]]
        if change_cover:
            if "covr" in tags:
                del tags["covr"]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if cover[1] == "image/png" else MP4Cover.FORMAT_JPEG
                tags["covr"] = [MP4Cover(cover[0], imageformat=fmt)]
        audio.save()

    elif kind == "flac":
        if audio.tags is None:
            audio.add_tags()
        for fld, val in edits.items():
            _tg_vc_set(audio.tags, fld, val)
        if change_cover:
            audio.clear_pictures()
            if cover:
                audio.add_picture(_tg_picture(cover[0], cover[1]))
        audio.save()

    else:  # ogg (Vorbis / Opus): کاور توی metadata_block_picture (base64) نگه‌داری میشه
        if audio.tags is None:
            audio.add_tags()
        for fld, val in edits.items():
            _tg_vc_set(audio.tags, fld, val)
        if change_cover:
            for k in ("metadata_block_picture", "coverart", "coverartmime"):
                with contextlib.suppress(KeyError):
                    del audio.tags[k]
            if cover:
                blob = base64.b64encode(_tg_picture(cover[0], cover[1]).write()).decode("ascii")
                audio.tags["metadata_block_picture"] = [blob]
        audio.save()


async def tg_prepare_cover(raw: Path, dst_dir: Path) -> Optional[Path]:
    """عکس ورودی رو برای جاسازی آماده میکنه: JPEG/PNG معقول همون‌طور میمونه؛ بقیه (WebP/BMP/GIF/خیلی بزرگ) JPEG با کیفیت بالا میشه."""
    data = await asyncio.to_thread(raw.read_bytes)
    mime = _cl_sniff(data)
    w, h = _cl_img_size(data)
    uid = uuid.uuid4().hex[:6]
    if mime in ("image/jpeg", "image/png") and w and h and max(w, h) <= TG_COVER_MAX_PX and len(data) <= TG_COVER_MAX_BYTES:
        out = dst_dir / f"cover_new_{uid}.{'png' if mime == 'image/png' else 'jpg'}"
        await asyncio.to_thread(out.write_bytes, data)
        return out
    out = dst_dir / f"cover_new_{uid}.jpg"
    m = TG_COVER_MAX_PX
    rc, _, _ = await run(
        FFMPEG, "-y", "-v", "error", "-i", str(raw), "-frames:v", "1",
        "-vf", f"scale='min({m},iw)':'min({m},ih)':force_original_aspect_ratio=decrease",
        "-q:v", "2", str(out),
    )
    if rc == 0 and out.exists() and out.stat().st_size > 0:
        return out
    return None


def tg_pending(job: Job) -> bool:
    return bool(job.tg_new) or job.tg_cover is not None or job.tg_drop_cover


def tg_reset(job: Job) -> None:
    job.tg_new.clear()
    if job.tg_cover:
        with contextlib.suppress(OSError):
            job.tg_cover.unlink()
    job.tg_cover = None
    job.tg_drop_cover = False


def _tg_is_image_doc(doc: Any) -> bool:
    mime = (getattr(doc, "mime_type", "") or "").lower()
    return mime.startswith("image/") or Path(getattr(doc, "file_name", "") or "").suffix.lower() in _TG_IMG_EXT


def render_tags(job: Job, note: str = ""):
    info = job.tg_info or _TG_EMPTY
    back = InlineKeyboardButton(T("btn.back_menu"), callback_data=f"mn:{job.id}:back")
    if not info.get("kind"):
        text = T("tg.unsupported")
        if note:
            text = f"{note}\n\n{text}"
        return text, InlineKeyboardMarkup([[back]])

    lines = [T("tg.head"), ""]
    for f, (emoji, _) in _TG_FIELDS.items():
        field = T(f"tg.label.{f}")
        if f in job.tg_new:
            lines.append(T("tg.line_pending", emoji=emoji, field=field, current=_clip(job.tg_new[f], 60) or T("tg.clear_word")))
        else:
            lines.append(T("tg.line", emoji=emoji, field=field, current=_clip(info.get(f) or "", 60) or "—"))
    if job.tg_cover:
        cover_txt = T("tg.cover_new")
    elif job.tg_drop_cover:
        cover_txt = T("tg.cover_drop")
    else:
        cover_txt = T("tg.cover_has") if info.get("has_cover") else T("tg.cover_no")
    lines.append(T("tg.cover_line", state=cover_txt))
    pending = tg_pending(job)
    lines += ["", T("tg.hint_pending") if pending else T("tg.hint")]
    text = "\n".join(lines)
    if note:
        text = f"{note}\n\n{text}"

    jid = job.id
    rows = [
        [InlineKeyboardButton(T("btn.tg.title"), callback_data=f"tg:{jid}:e:title", api_kwargs=BLUE),
         InlineKeyboardButton(T("btn.tg.artist"), callback_data=f"tg:{jid}:e:artist", api_kwargs=BLUE)],
        [InlineKeyboardButton(T("btn.tg.album"), callback_data=f"tg:{jid}:e:album", api_kwargs=BLUE),
         InlineKeyboardButton(T("btn.tg.cover"), callback_data=f"tg:{jid}:c", api_kwargs=BLUE)],
    ]
    if job.tg_cover or (info.get("has_cover") and not job.tg_drop_cover):
        rows.append([InlineKeyboardButton(T("btn.tg.dropcover"), callback_data=f"tg:{jid}:dc", api_kwargs=STYLE_RED)])
    if pending:
        rows.append([InlineKeyboardButton(T("btn.tg.go"), callback_data=f"tg:{jid}:go", api_kwargs=BLUE)])
        rows.append([InlineKeyboardButton(T("btn.tg.reset"), callback_data=f"tg:{jid}:rs")])
    rows.append([back])
    return text, InlineKeyboardMarkup(rows)


def render_tag_prompt(job: Job, field: str, note: str = ""):
    """صفحه‌ی «منتظر پیام بعدی»؛ field = title | artist | album | cover"""
    jid = job.id
    if field == "cover":
        text = T("tg.p.cover")
        if note:
            text = f"{note}\n\n{text}"
        return text, InlineKeyboardMarkup([[InlineKeyboardButton(T("btn.cancel_x"), callback_data=f"tg:{jid}:x", api_kwargs=STYLE_RED)]])
    emoji, _ = _TG_FIELDS[field]
    info = job.tg_info or _TG_EMPTY
    cur = job.tg_new[field] if field in job.tg_new else (info.get(field) or "")
    text = T("tg.p.field", emoji=emoji, field=T(f"tg.label.{field}"), current=_clip(cur, 80) or "—")
    if note:
        text = f"{note}\n\n{text}"
    return text, InlineKeyboardMarkup([[
        InlineKeyboardButton(T("btn.tg.clear"), callback_data=f"tg:{jid}:clr:{field}"),
        InlineKeyboardButton(T("btn.cancel_x"), callback_data=f"tg:{jid}:x", api_kwargs=STYLE_RED),
    ]])


async def tg_load(job: Job) -> Dict[str, Any]:
    if job.tg_info is None:
        try:
            job.tg_info = await asyncio.to_thread(tg_read, job.src)
        except Exception:  # noqa: BLE001
            log.exception("tag read failed")
            job.tg_info = dict(_TG_EMPTY)
    return job.tg_info


async def tg_show(message, job: Job, note: str = "") -> None:
    await tg_load(job)
    text, markup = render_tags(job, note)
    await safe_edit(message, text, markup)


async def tg_refresh(job: Job) -> None:
    """بعد از اعمال تغییرات، مشخصات مشتق‌شده‌ی job (حجم، کاور/تامبنیل، دکمه‌ی «کاور و ترانه») رو با فایل جدید هماهنگ میکنه."""
    try:
        job.size = job.src.stat().st_size
        job.kbps = int(job.size * 8 / job.duration / 1000)
        job.pic_codec = (await probe(job.src))["pic_codec"]
        job.thumb = await extract_thumb(job.src, job.dir / "thumb.jpg")
        job.cover = await extract_cover(job.src, job.dir / "cover.jpg") if CARDS_ENABLED else None
        if CL_ENABLED:
            job.cl_parts = cl_parts_of(await cl_inspect_async(job.src))
    except Exception:  # noqa: BLE001 — فقط هماهنگ‌سازیه؛ خطاش نباید جلوی نتیجه رو بگیره
        log.exception("tag refresh failed")


async def tg_apply(job: Job, context: ContextTypes.DEFAULT_TYPE, message) -> None:
    """اعمال تغییرات روی یه کپی، ارسال فایل جدید، و بعدش جایگزین‌کردن فایل job (تا کارهای بعدی روی نسخه‌ی ویرایش‌شده انجام بشن)."""
    job.busy = True
    await safe_edit(message, T("tg.applying"))
    edits = dict(job.tg_new)
    cover_path = job.tg_cover
    drop = job.tg_drop_cover
    out: Optional[Path] = None
    note, ok = "", False
    try:
        cover = None
        if cover_path:
            data = await asyncio.to_thread(cover_path.read_bytes)
            cover = (data, _cl_sniff(data) or "image/jpeg")
        out = job.dir / f"tag_{uuid.uuid4().hex[:6]}{job.src.suffix}"
        async with SEM:
            await asyncio.to_thread(tg_write, job.src, out, edits, cover, drop)
        size = out.stat().st_size
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)

        title = (edits["title"] if "title" in edits else job.title) or job.name
        artist = edits["artist"] if "artist" in edits else job.artist
        if "title" in edits or "artist" in edits:
            base = f"{artist} - {title}" if artist else title
        else:
            base = job.name
        fname = _cl_safe_name(base, default=job.name) + job.src.suffix
        thumb = job.thumb
        if cover_path:
            thumb = await extract_thumb(cover_path, job.dir / "tg_thumb.jpg")
        elif drop:
            thumb = None
        info = job.tg_info or _TG_EMPTY
        album = edits["album"] if "album" in edits else (info.get("album") or "")
        cap = [T("tg.cap_head"), T("tg.cap_title", title=title)]
        if artist:
            cap.append(T("tg.cap_artist", artist=artist))
        if album:
            cap.append(T("tg.cap_album", album=album))
        if cover_path:
            cap.append(T("tg.cap_cover_new"))
        elif drop:
            cap.append(T("tg.cap_cover_drop"))

        common = dict(
            chat_id=message.chat_id, filename=fname, caption="\n".join(cap)[:1024],
            thumbnail=thumb.read_bytes() if thumb else None, reply_markup=more_markup(job, "tg"),
            read_timeout=300, write_timeout=300, connect_timeout=60,
        )
        with out.open("rb") as f:
            try:
                sent = await context.bot.send_audio(
                    audio=f, title=title, performer=artist or None, duration=int(job.duration), **common
                )
            except BadRequest:  # بعضی فرمت‌ها (مثل OGG) رو تلگرام به‌عنوان آهنگ قبول نمیکنه ← به‌صورت فایل
                f.seek(0)
                sent = await context.bot.send_document(document=f, **common)
        count_file(job.user_id, "out")
        ok = True
        note = T("tg.done")
        await after_delivery(job, "tg", sent)

        applied = False
        with contextlib.suppress(OSError):
            os.replace(out, job.src)
            out, applied = None, True
        if applied:  # از این به بعد همه‌ی کارها (کاهش حجم، تبدیل، آنالیز…) روی نسخه‌ی ویرایش‌شده انجام میشه
            if "title" in edits:
                job.title = edits["title"] or job.name
            if "artist" in edits:
                job.artist = edits["artist"]
            tg_reset(job)
            job.tg_info = None
            await tg_refresh(job)
    except OutputTooBig as e:
        log.warning("tagged output %s bytes exceeds upload limit", e)
        note = T("tg.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("tag edit failed")
        note = T("tg.fail")
    finally:
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:  # فایل می‌مونه تا کاربر کار دیگه‌ای هم روش بکنه (تا JOB_TTL)
            job.created = time.time()
            if ok:
                text, markup = panel_done_text(note), None
            else:  # تغییرات در انتظار می‌مونن تا دوباره امتحان کنه
                text, markup = render_tags(job, note)
            await safe_edit(message, text, markup)


# ───────────────────────── هندلرها ─────────────────────────


def allowed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if user_id in BANNED:
        return False
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


async def fetch_file(att: Any, dest: Path) -> None:
    """دانلود فایل ورودی در dest.

    حالت لوکال: سرور لوکال فایل رو روی دیسک میذاره و file_path یه مسیر مطلق برمی‌گردونه؛
    ما مستقیم از همون دیسک کپی می‌کنیم (بدون HTTP) و بعدش نسخه‌ی سرور رو پاک می‌کنیم
    تا دیسک پر نشه. حالت عادی: دانلود معمولی از API تلگرام.
    """
    tg_file = await att.get_file()
    fp = tg_file.file_path or ""
    if LOCAL_API_URL and os.path.isabs(fp):
        srcp = Path(fp)
        if not srcp.is_file():
            raise FileNotFoundError(
                f"{fp} روی دیسکِ ربات پیدا نشد؛ Volume مشترک بین سرور لوکال و ربات رو چک کن."
            )
        def _take() -> None:
            try:  # همون پارتیشن: جابه‌جایی آنی بدون کپی داده
                os.replace(srcp, dest)
            except OSError:  # پارتیشن‌های جدا: کپی و حذف مبدأ
                shutil.copyfile(srcp, dest)
                with contextlib.suppress(OSError):
                    srcp.unlink()

        await asyncio.to_thread(_take)
        return
    await tg_file.download_to_drive(dest)


# ───────────────────────── بنر تبلیغات (قبل از پیام شروع) ─────────────────────────
# ادمین از «پنل ادمین ← بنر تبلیغات» عکس دلخواه میذاره؛ اگه نذاشته باشه بنر پیش‌فرض «جای تبلیغات شما» نمایش داده میشه.
# تنظیمات توی DB["ad"] ذخیره میشه: on (روشن/خاموش)، fid (file_id عکس ادمین)، def_fid (file_id کش‌شده‌ی بنر پیش‌فرض)


def ad_cfg() -> Dict[str, Any]:
    cfg = DB.get("ad")
    if not isinstance(cfg, dict):
        cfg = DB["ad"] = {}
    return cfg


def ad_enabled() -> bool:
    return bool(ad_cfg().get("on", True))


async def send_ad_banner(message, force: bool = False) -> bool:
    """بنر رو (عکس ادمین یا پیش‌فرض) به‌صورت پیام جدا میفرسته. خروجی: True = فرستاده شد.
    force=True یعنی حتی اگه خاموشه بفرست (برای پیش‌نمایش ادمین). خطا فقط لاگ میشه تا /start خراب نشه."""
    if not (force or ad_enabled()):
        return False
    cfg = ad_cfg()
    kw = {"protect_content": PROTECT_IMAGES}
    try:
        if cfg.get("fid"):
            try:
                await message.reply_photo(photo=cfg["fid"], **kw)
                return True
            except BadRequest as e:  # file_id خراب/منقضی ← میریم سراغ بنر پیش‌فرض
                log.warning("بنر ادمین فرستاده نشد (%s)؛ بنر پیش‌فرض نمایش داده میشه.", e)
        if cfg.get("def_fid"):
            try:
                await message.reply_photo(photo=cfg["def_fid"], **kw)
                return True
            except BadRequest:
                cfg.pop("def_fid", None)  # دوباره ساخته و آپلود میشه
        data = await asyncio.to_thread(render_default_ad)
        if data is None:
            log.warning("بنر پیش‌فرض ساخته نشد (Pillow/فونت فارسی لازمه).")
            return False
        sent = await message.reply_photo(photo=data, **kw)
        if sent.photo:
            cfg["def_fid"] = sent.photo[-1].file_id  # دفعه‌ی بعد بدون آپلود مجدد
            db_save_soon()
        return True
    except TelegramError as e:
        log.warning("ارسال بنر تبلیغات ناموفق: %s", e)
        return False


def start_text() -> str:
    limit_txt = "100 مگ" if not LOCAL_API_URL else "2 گیگ"
    return (
        T("start.head") + "\n" + T("start.body") + "\n\n"
        + T("start.limit", limit=limit_txt) + "\n\n" + T("start.footer")
    )


async def _start_panel_alive(bot: Any, chat_id: int, user_id: int) -> bool:
    """پنلِ /start قبلاً توی پیوی این کاربر فرستاده شده و هنوز هست؟
    وجودِ پیام با یه ری‌اکشنِ خالی (بی‌اثر و نامرئی) چک میشه؛ اگه پیام حذف شده باشه، تلگرام BadRequest میده."""
    mid = (DB["users"].get(str(user_id)) or {}).get("panel")
    if not mid:
        return False
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=int(mid), reaction=[])
        return True
    except BadRequest:
        return False  # پیام پنل پاک شده ← دوباره فرستاده میشه
    except (TelegramError, AttributeError):
        return True  # خطای شبکه / نسخه‌ی قدیمیِ کتابخونه؛ احتیاطاً پنل رو موجود فرض می‌کنیم


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    touch_user(update.effective_user)
    msg = update.effective_message
    private = update.effective_chat is not None and update.effective_chat.type == "private"
    if private and await _start_panel_alive(context.bot, update.effective_chat.id, update.effective_user.id):
        # پنل همین الان توی پیوی هست: پیامِ /start کاربر فقط پاک میشه و ربات هیچ واکنشی نشون نمیده
        with contextlib.suppress(TelegramError):
            await msg.delete()
        return
    context.user_data.pop("tg_wait", None)
    context.user_data.pop("tr_wait", None)
    context.user_data.pop("wm_wait", None)
    context.user_data.pop("want", None)  # /start دوباره = انتخاب قبلیِ دکمه‌ها لغو
    admin = is_admin(update.effective_user.id)
    await send_ad_banner(msg)  # بنر تبلیغات، قبل از پیام شروع
    panel = await msg.reply_text(start_text(), reply_markup=start_markup())
    if private:  # پیام شروع پین میشه (بدون نوتیف)
        with contextlib.suppress(TelegramError):
            await context.bot.pin_chat_message(
                chat_id=update.effective_chat.id, message_id=panel.message_id, disable_notification=True)
    if private:  # شناسه‌ی پنل ذخیره میشه تا /start بعدی (وقتی پنل هنوز هست) بی‌صدا پاک بشه
        rec = DB["users"].get(str(update.effective_user.id))
        if rec is not None:
            rec["panel"] = panel.message_id
            db_save_soon()
    if admin:  # کیبورد ریپلای (پنل ادمین) با دکمه‌ی شیشه‌ای توی یه پیام جا نمیشه؛ جدا ست میشه
        await update.effective_message.reply_text(T("start.admin_on"), reply_markup=ADMIN_KB)


# رنگ دکمه‌ها (Bot API 9.4+): "danger" = قرمز | "primary" = آبی | "success" = سبز
# با api_kwargs فرستاده میشه تا روی نسخه‌های قدیمی‌تر python-telegram-bot هم کار کنه.
def support_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(T("btn.support"), url=SUPPORT_URL, api_kwargs={"style": "primary"})


def start_markup() -> InlineKeyboardMarkup:
    """زیر پیام شروع فقط دو دکمه: راهنما (متنش از پنل ادمین ویرایش میشه) و پشتیبانی (باز کردن پروفایل)."""
    help_btn = InlineKeyboardButton(T("btn.help"), callback_data="hp:show", api_kwargs={"style": "danger"})
    return InlineKeyboardMarkup([[help_btn, support_button()]])


async def on_pin_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام سرویسیِ «ربات یه پیام رو پین کرد» از پیوی کاربر پاک میشه."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is not None and chat is not None and chat.type == "private":
        with contextlib.suppress(TelegramError):
            await msg.delete()


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    touch_user(update.effective_user)
    await update.effective_message.reply_text(T("help.body"), reply_markup=InlineKeyboardMarkup([[support_button()]]))


async def on_help_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not allowed(q.from_user.id):
        await q.answer()
        return
    touch_user(q.from_user)
    if q.data == "hp:back":  # برگشت به پیام شروع (همون پیام ادیت میشه)
        await asyncio.gather(_quiet(q.answer()), _quiet(safe_edit(q.message, start_text(), start_markup())))
        return
    back = InlineKeyboardButton(T("btn.help_back"), callback_data="hp:back")
    await asyncio.gather(  # راهنما جای همین پیام میشینه؛ پیام جدید نمیاد
        _quiet(q.answer()),
        _quiet(safe_edit(q.message, T("help.body"), InlineKeyboardMarkup([[support_button()], [back]]))),
    )


def plan_text(user_id: int) -> str:
    if is_admin(user_id):
        return T("plan.admin")
    st = premium_status(user_id)
    if st is not None:
        left = "دائمی" if st == math.inf else f"{_uptime(st)} دیگه"
        return T("plan.premium", left=left)
    lines = [T("plan.free_head")]
    if FREE_DAILY_FILES:
        lines.append(T("plan.free_daily", used=fa(files_today(user_id)), limit=fa(FREE_DAILY_FILES)))
    if FREE_MAX_MB:
        lines.append(T("plan.free_max", max=fa(FREE_MAX_MB)))
    if len(lines) > 1:
        lines.append("\n" + T("plan.free_tail"))
    return "\n".join(lines)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    touch_user(update.effective_user)
    await update.effective_message.reply_text(plan_text(update.effective_user.id))


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not allowed(user.id):
        return
    touch_user(user)
    att = msg.audio or msg.voice or msg.document
    if att is None:
        return
    name = getattr(att, "file_name", None) or ("voice.ogg" if msg.voice else "audio.mp3")
    if msg.document:
        mime = (att.mime_type or "").lower()
        if not (mime.startswith("audio/") or Path(name).suffix.lower() in AUDIO_EXT):
            await msg.reply_text(T("media.only_audio"))
            return
    if att.file_size and att.file_size > DOWNLOAD_LIMIT:
        await msg.reply_text(T("media.toobig", max=mb(DOWNLOAD_LIMIT)))
        return
    if not is_premium(user.id):
        if FREE_MAX_MB and att.file_size and att.file_size > FREE_MAX_MB * MB:
            await msg.reply_text(T("media.free_max", max=fa(FREE_MAX_MB)))
            return
        if FREE_DAILY_FILES and files_today(user.id) >= FREE_DAILY_FILES:
            await msg.reply_text(T("media.free_daily", limit=fa(FREE_DAILY_FILES)))
            return

    context.user_data.pop("tr_wait", None)  # آهنگ جدید اومد؛ انتظار زمانِ برشِ آهنگ قبلی لغو میشه
    context.user_data.pop("wm_wait", None)  # همین‌طور انتظار متن امضا
    want = context.user_data.pop("want", None)  # کاری که کاربر از دکمه‌های /start انتخاب کرده بود (اگه بود)
    sweep()
    # پیام وضعیت هم‌زمان با شروع دانلود فرستاده میشه (نه قبل از اون)؛ یه رفت‌وبرگشت به تلگرام کمتر منتظر میمونیم
    status_task = asyncio.ensure_future(msg.reply_text(T("media.receiving")))
    job_id = uuid.uuid4().hex[:8]
    jdir = TEMP_DIR / job_id
    jdir.mkdir(parents=True, exist_ok=True)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(name).suffix) or ".mp3"
    src = jdir / f"src{suffix}"
    try:
        await fetch_file(att, src)
        info = await probe(src)
        if info["duration"] <= 1:
            raise ValueError("bad duration")
    except BadRequest as e:
        shutil.rmtree(jdir, ignore_errors=True)
        txt = T("media.e.tg_big") if "too big" in str(e).lower() else T("media.e.fail")
        await safe_edit(await status_task, txt)
        return
    except FileNotFoundError as e:
        log.error("local file missing: %s", e)
        shutil.rmtree(jdir, ignore_errors=True)
        await safe_edit(await status_task, T("media.e.local"))
        return
    except Exception:
        log.exception("download/probe failed")
        shutil.rmtree(jdir, ignore_errors=True)
        await safe_edit(await status_task, T("media.e.unreadable"))
        return
    status = await status_task

    size = src.stat().st_size
    title = info["title"] or getattr(att, "title", None) or Path(name).stem
    artist = info["artist"] or getattr(att, "performer", None) or ""
    job = Job(
        id=job_id, user_id=user.id, dir=jdir, src=src, name=Path(name).stem, size=size,
        duration=info["duration"], kbps=int(size * 8 / info["duration"] / 1000),
        title=title, artist=artist, thumb=None, created=time.time(), cover=None,
        codec=info["codec"], sample_rate=info["sample_rate"], pic_codec=info["pic_codec"],
    )

    async def _no_cover() -> None:
        return None

    async def _cl_label() -> Optional[Tuple[bool, bool, bool]]:
        if not CL_ENABLED:
            return None
        try:
            return cl_parts_of(await cl_inspect_async(src))
        except Exception:  # noqa: BLE001 — این قابلیت اختیاریه؛ خطاش نباید جلوی کار اصلی رو بگیره
            log.exception("cover/lyrics inspect failed")
            return None

    JOBS[job_id] = job
    count_file(user.id, "in")
    # بندانگشتی، کاور، بررسی کاور/ترانه و بررسی اصالت کیفیت کاملاً مستقل از هم‌ان؛ هم‌زمان اجرا میشن
    # (قبلاً پشت‌سرهم بودن و زمان‌هاشون جمع میشد). نتیجه همونه.
    job.thumb, job.cover, job.cl_parts, _ = await asyncio.gather(
        extract_thumb(src, jdir / "thumb.jpg"),
        extract_cover(src, jdir / "cover.jpg") if CARDS_ENABLED else _no_cover(),
        _cl_label(),
        quick_quality(job),
    )
    card = await build_info_card(job)
    if card is not None:  # کاور گرافیکی؛ منو زیر همون عکس میاد
        job.has_card = True
        text, markup = render_menu(job)
        try:
            panel = await msg.reply_photo(photo=card, caption=text, reply_markup=markup, protect_content=PROTECT_IMAGES)
            with contextlib.suppress(TelegramError):
                await status.delete()
            await _after_menu(update, context, job, panel, want)
            return
        except TelegramError:
            log.exception("sending info card failed; falling back to text")
            job.has_card = False
    text, markup = render_menu(job)
    await safe_edit(status, text, markup)
    await _after_menu(update, context, job, status, want)


class _AutoQuery:
    """کلیکِ ساختگی روی دکمه‌ی منو: کاری که کاربر از پیام /start انتخاب کرده بود، بعد از رسیدن آهنگ دقیقاً مثل کلیک روی منو اجرا میشه."""

    def __init__(self, data: str, message: Any, user: Any) -> None:
        self.data, self.message, self.from_user = data, message, user

    async def answer(self, *a: Any, **k: Any) -> None:
        return None


async def _after_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, job: Job, panel: Any, want: Optional[str]) -> None:
    if want != "ana":  # آنالیز همین الان شروع میشه؛ بقیه‌ی حالت‌ها از پیش‌محاسبه سود می‌برن
        start_prefetch(job)
    if not want or job.id not in JOBS:
        return
    try:
        fake = types.SimpleNamespace(callback_query=_AutoQuery(f"mn:{job.id}:{want}", panel, update.effective_user))
        await on_menu(fake, context)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — منوی آهنگ که اومده؛ خطای ادامه‌ی خودکار نباید چیزی رو خراب کنه
        log.exception("auto action %s failed", want)


async def _link_within(mi_task: "asyncio.Future", timeout: float) -> Optional[str]:
    """لینک MediaInfo اگه تا timeout ثانیه آماده شد؛ وگرنه None (تسک ادامه میده)"""
    if not mi_task.done() and timeout > 0:
        await asyncio.wait({mi_task}, timeout=timeout)
    if mi_task.done() and not mi_task.cancelled():
        return mi_task.result()
    return None


def _attach_link_later(sent: Any, mi_task: "asyncio.Future", make_text, photo: bool, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """گزارش بدون لینک رفته؛ وقتی لینک MediaInfo آماده شد، کپشن/متن همون پیام ادیت میشه و لینک اضافه میشه."""

    async def go() -> None:
        try:
            link = await asyncio.wait_for(asyncio.shield(mi_task), 90)
            if not link:
                return
            text = make_text(link)
            if photo:
                await sent.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
            else:
                await sent.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        except Exception:  # noqa: BLE001 — لینک اختیاریه؛ نبودنش مهم نیست
            log.debug("late mediainfo link failed", exc_info=True)

    task = asyncio.create_task(go())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _verdict_caption(q: Dict[str, Any]) -> str:
    """هدر نتیجه‌ی آنالیز (زیر کارت) + سطح اطمینان؛ متن‌ها از پنل ادمین ویرایش میشن."""
    v = q.get("verdict", "")
    cap = T(f"ana.v.{v}") if f"ana.v.{v}" in TEXTS else ""
    if v != "inconclusive":
        conf = q.get("confidence")
        level = T(f"ana.c.{conf}") if f"ana.c.{conf}" in TEXTS else _CONF_FA.get(conf, "")
        cap += " " + T("ana.conf", level=level)
    return cap


async def process_audio_analysis(
    chat_id: int, file_path: Path, cover: Optional[Path] = None, display_name: Optional[str] = None, panel=None,
    ready: Optional["asyncio.Future"] = None, markup: Optional[InlineKeyboardMarkup] = None,
    on_sent: Optional[Any] = None,
) -> bool:
    """تحلیل فایل و ارسال نتیجه به کاربر (کارت گرافیکی؛ اگه نشد متن).
    زیر کپشن، لینک MediaInfo (صفحه‌ی Telegraph با همه‌ی متادیتا و فرمت) هم میاد.
    اگه panel (پیام منوی همین آهنگ) داده بشه، نتیجه جای همون پیام ادیت میشه تا پیام جدیدی توی چت نیاد؛
    اگه نشد (مثلاً نتیجه متنیه و پیام منو عکسه) پیام جدا میره. خروجی: True = نتیجه توی panel نشست.
    ready: ادیتِ پس‌زمینه‌ی «در حال تحلیل…»؛ قبل از اولین ادیتِ نتیجه حتماً تموم میشه (ترتیب ادیت‌ها بهم نخوره).
    اگه لینک MediaInfo تا MEDIAINFO_WAIT ثانیه آماده نشه، نتیجه همون لحظه میره و لینک بعداً به کپشن اضافه میشه."""
    # ساخت صفحه‌ی MediaInfo موازی با تحلیل طیف انجام میشه تا زمان انتظار بیشتر نشه
    mi_task = asyncio.create_task(build_mediainfo_link(file_path, display_name or file_path.name))
    late = False
    t_start = time.monotonic()

    def left() -> float:  # سقف انتظار برای لینک از لحظه‌ی شروع حساب میشه (نه بعد از تموم‌شدن تحلیل/کارت)
        return MEDIAINFO_WAIT - (time.monotonic() - t_start)

    try:
        result = await analyze_audio_async(file_path, include_spectrum=CARDS_ENABLED)
        if result.get("ok") and CARDS_ENABLED:
            img = await build_report_card(result, cover, file_path.suffix)
            if img is not None:
                try:
                    cap = _verdict_caption(result["quality"])
                    link = await _link_within(mi_task, left())
                    pending = link is None and not mi_task.done()
                    await settle(ready)
                    if panel is not None and panel.photo:  # جای کارتِ منو، کارت گزارش میشینه (محافظت پیام همون می‌مونه)
                        try:
                            cap_in = cap if len(cap) > 800 else cap + "\n\n" + T("ana.cap_deleted")

                            def make_in(lnk: Optional[str], _c: str = cap_in) -> str:
                                return _with_mediainfo_link(_c[:900], lnk)[:1024]

                            sent = await panel.edit_media(InputMediaPhoto(
                                media=img, caption=make_in(link), parse_mode="HTML",
                            ), reply_markup=markup)
                            if on_sent is not None:
                                await on_sent(sent)
                            if pending:
                                late = True
                                _attach_link_later(sent, mi_task, make_in, True, markup)
                            return True
                        except TelegramError:
                            log.exception("editing menu message with report failed; sending a new message")

                    def make_new(lnk: Optional[str], _c: str = cap) -> str:
                        return _with_mediainfo_link(_c[:900], lnk)[:1024]

                    sent = await BOT.send_photo(
                        chat_id, photo=img, caption=make_new(link),
                        parse_mode="HTML", read_timeout=120, write_timeout=120,
                        protect_content=PROTECT_IMAGES, reply_markup=markup,
                    )
                    if on_sent is not None:
                        await on_sent(sent)
                    if pending:
                        late = True
                        _attach_link_later(sent, mi_task, make_new, True, markup)
                    return False
                except TelegramError:
                    log.exception("sending report card failed; falling back to text")
        text = result["message"]
        if len(text) > 3600:  # سقف تلگرام 4096 کاراکتر است (بعد از escape کمی بلندتر میشه)
            text = text[:3590] + "\n..."
        link = await _link_within(mi_task, left())
        pending = link is None and not mi_task.done()
        await settle(ready)

        def make_text(lnk: Optional[str]) -> str:
            return _with_mediainfo_link(text, lnk)

        if panel is not None and not panel.photo:  # پیام منو متنیه؛ نتیجه‌ی متنی رو همون‌جا ادیت کن
            try:
                sent = await panel.edit_text(
                    make_text(link), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
                if on_sent is not None:
                    await on_sent(sent)
                if pending:
                    late = True
                    _attach_link_later(sent, mi_task, make_text, False, markup)
                return True
            except TelegramError:
                log.exception("editing menu message with text report failed; sending a new message")
        sent = await BOT.send_message(
            chat_id, make_text(link), parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
        if on_sent is not None:
            await on_sent(sent)
        if pending:
            late = True
            _attach_link_later(sent, mi_task, make_text, False, markup)
        return False
    finally:
        if not late and not mi_task.done():
            mi_task.cancel()


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, job_id, action = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    job = JOBS.get(job_id)
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()  # با هر کلیک، زمان انقضا از نو شروع میشه
    context.user_data.pop("tg_wait", None)  # اگه منتظر ورودی تگ/کاور بودیم، با کلیک روی منو لغو میشه
    context.user_data.pop("tr_wait", None)  # همین‌طور منتظر زمان برش
    context.user_data.pop("wm_wait", None)  # همین‌طور منتظر متن امضا

    if action == "red":  # مسیر فعلی: انتخاب حجم ← on_choice
        await q.answer()
        text, markup = render(job)
        await safe_edit(q.message, text, markup)
    elif action == "back":  # بازگشت از صفحه‌ی انتخاب حجم به منوی اصلی
        await q.answer()
        text, markup = render_menu(job)
        await safe_edit(q.message, text, markup)
    elif action == "cv":  # صفحه‌ی انتخاب فرمت؛ ادامه‌اش توی on_convert
        if not CONVERT_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        await q.answer()
        text, markup = render_conv_formats(job)
        await safe_edit(q.message, text, markup)
    elif action == "tg":  # ویرایش تگ و کاور؛ ادامه‌اش توی on_tags
        if not TAGS_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        await q.answer()
        await tg_show(q.message, job)
    elif action == "tr":  # برش بخش دلخواه؛ ادامه‌اش توی on_trim / on_trim_input
        if not TRIM_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        context.user_data["tr_wait"] = {"job": job.id, "msg": q.message}
        await q.answer()
        text, markup = render_trim_prompt(job)
        await safe_edit(q.message, text, markup)
    elif action == "nm":  # نرمال‌سازی لودنس؛ ادامه‌اش توی on_norm
        if not NORM_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        await q.answer()
        await norm_show(q.message, job)
    elif action == "bs":  # افزایش بیس؛ ادامه‌اش توی on_bass
        if not BASS_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        await q.answer()
        text, markup = render_bass(job)
        await safe_edit(q.message, text, markup)
    elif action == "wm":  # امضای فرکانسی؛ ادامه‌اش توی on_watermark_input
        if not WM_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        context.user_data["wm_wait"] = {"job": job.id, "msg": q.message}
        await q.answer()
        text, markup = render_wm_prompt(job)
        await safe_edit(q.message, text, markup)
    elif action == "cl":  # استخراج کاور (کیفیت اصلی) + ترانه (.lrc / .txt)
        if not CL_ENABLED:
            await q.answer(T("alert.disabled"), show_alert=True)
            return
        job.busy = True
        st = ack_edit_bg(q, T("cl.progress"))
        note = ""
        cl_ok = False
        try:
            async with SEM:
                files = cl_export_files(await cl_inspect_async(job.src), job.name)
            if not files:
                note = T("cl.none")
            sent = None
            for i, f in enumerate(files):
                sent = await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=f.data,
                    filename=f.name,
                    caption=f.caption,
                    reply_markup=more_markup(job, "cl") if i == len(files) - 1 else None,  # دکمه‌ها زیر آخرین فایل
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                    protect_content=PROTECT_COVERS and Path(f.name).suffix.lower() in _TG_IMG_EXT,  # فقط فایل کاور؛ ترانه آزاده
                )
            if files:
                note = T("cl.done")
                cl_ok = True
                await after_delivery(job, "cl", sent)
        except Exception:
            log.exception("cover/lyrics export failed")
            note = T("cl.fail")
        finally:
            await settle(st)
            job.busy = False
            if job.id in JOBS:  # فایل می‌مونه تا کاربر بتونه کار دیگه‌ای هم روی همین آهنگ بکنه
                job.created = time.time()
                if cl_ok:
                    text, markup = panel_done_text(note), None
                else:
                    text, markup = render_menu(job, note)
                await safe_edit(q.message, text, markup)
    elif action == "ana":
        job.busy = True
        done = False
        in_panel = False  # نتیجه‌ی تحلیل جای همین پیام نشست؟
        note = ""
        st = ack_edit_bg(q, T("ana.progress"))  # تحلیل همون لحظه شروع میشه؛ منتظر ادیت پیام نمیمونه
        try:
            async with SEM:
                async def _ana_sent(sent: Any) -> None:
                    await after_delivery(job, "ana", sent)

                in_panel = await process_audio_analysis(
                    q.message.chat_id, job.src, job.cover, display_name=f"{job.name}{job.src.suffix}",
                    panel=q.message, ready=st, markup=more_markup(job, "ana"), on_sent=_ana_sent,
                )
            done = True
        except Exception:
            log.exception("analysis failed")
            note = T("ana.fail")
        finally:
            await settle(st)
            job.busy = False
            if done:  # جواب رفت؛ فایل‌ها تا «تمام» یا ۵ دقیقه می‌مونن
                if not in_panel:  # گزارش پیام جدا شده؛ این پیام فقط وضعیت رو نشون میده
                    await safe_edit(q.message, panel_done_text())
            elif job.id in JOBS:  # خطا: فایل می‌مونه تا کاربر دوباره امتحان کنه (تا JOB_TTL)
                job.created = time.time()
                text, markup = render_menu(job, note)
                await safe_edit(q.message, text, markup)
    else:
        await q.answer()


async def _finish_in_place(message, note: str) -> bool:
    """پیام پایان رو به‌جای پیام جدید، ته کپشن/متنِ همون پیام (فایل یا گزارش) اضافه میکنه و دکمه‌هاش رو برمیداره.
    خروجی: True = ادیت شد."""
    has_caption = bool(message.audio or message.document or message.photo or message.video or message.voice)
    if has_caption:
        plain, rich, limit = message.caption or "", message.caption_html or "", 1024
    else:
        plain, rich, limit = message.text or "", message.text_html or "", 4096
    body = rich + "\n\n" + html.escape(note) if plain and len(plain) + 2 + len(note) <= limit else html.escape(note)
    try:
        if has_caption:
            await message.edit_caption(caption=body, parse_mode="HTML", reply_markup=None)
        else:
            await message.edit_text(body, parse_mode="HTML", reply_markup=None)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return True
        log.warning("finish edit failed: %s", e)
    except TelegramError as e:
        log.warning("finish edit failed: %s", e)
    return False


async def on_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ot:{job}:{action} ← دکمه‌های زیر فایل ارسالی. action = done (تمام: پاک‌کردن فایل‌ها) یا یکی از کارهای منو."""
    q = update.callback_query
    try:
        _, job_id, action = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    job = JOBS.get(job_id)
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        with contextlib.suppress(TelegramError):
            await q.message.edit_reply_markup(reply_markup=None)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    if action == "done":
        drop_job(job_id)  # فایل‌ها پاک میشن و دکمه‌های زیر فایل برداشته میشه
        ok = await _finish_in_place(q.message, T("more.finished"))  # پیام جدید نمیاد؛ همون پیامِ فایل ادیت میشه
        if ok:
            await _quiet(q.answer())
        else:
            await _quiet(q.answer(T("more.finished"), show_alert=True))
            with contextlib.suppress(TelegramError):
                await q.message.edit_reply_markup(reply_markup=None)
        return
    if action not in {a for a, _ in menu_actions(job)}:
        await q.answer()
        with contextlib.suppress(TelegramError):
            await q.message.edit_reply_markup(reply_markup=more_markup(job))
        return
    job.created = time.time()
    await q.answer()
    try:
        panel = await q.message.reply_text(T("more.opening"))  # پنل جدا زیر فایل؛ خود فایل و دکمه‌هاش دست‌نخورده می‌مونن
    except TelegramError:
        log.exception("opening panel failed")
        return
    TRANSIENT_PANELS.add(_pkey(panel))  # بعد از ارسال فایل جمع میشه (به‌جای باقی‌موندنِ پیام «فایل ارسال شد»)
    fake = types.SimpleNamespace(callback_query=_AutoQuery(f"mn:{job.id}:{action}", panel, q.from_user))
    await on_menu(fake, context)  # type: ignore[arg-type]


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, job_id, choice = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    job = JOBS.get(job_id)
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if choice == "x":
        drop_job(job_id)
        await q.answer()
        await safe_edit(q.message, T("red.cancelled"))
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return

    if choice == "auto":
        kbps, limit, label = AUTO_KBPS, None, f"{fa(AUTO_KBPS)}k"
    else:
        try:
            v = float(choice)
        except ValueError:
            await q.answer()
            return
        limit = int(v * MB)
        kbps = min(MAX_KBPS, max(MIN_KBPS, kbps_for(limit, job.duration)))
        label = T("red.label", mb=label_mb(v))

    job.busy = True
    st = ack_edit_bg(q, T("red.progress", label=label))  # انکود همون لحظه شروع میشه
    out: Optional[Path] = None
    done = False
    note = ""
    try:
        async with SEM:
            out, final_kbps, size = await encode(job, kbps, limit)
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        caption = T("red.caption", size=mb(size), kbps=fa(final_kbps), quality=quality_word(final_kbps), old=mb(job.size))
        if limit and size > limit:
            caption += "\n" + T("red.caption_over")
        with out.open("rb") as f:
            sent = await context.bot.send_audio(
                chat_id=q.message.chat_id,
                audio=f,
                filename=f"{job.name}.m4a",
                title=job.title,
                performer=job.artist or None,
                duration=int(job.duration),
                thumbnail=job.thumb.read_bytes() if job.thumb else None,
                caption=caption,
                reply_markup=more_markup(job, "red"),
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )
        count_file(job.user_id, "out")
        done = True
        await after_delivery(job, "red", sent)
    except OutputTooBig as e:
        log.warning("output %s bytes exceeds upload limit", e)
        note = T("red.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("encode/send failed")
        note = T("red.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if done:  # آهنگ رفت؛ فایل‌ها تا «تمام» یا ۵ دقیقه می‌مونن و گزینه‌های دیگه زیر فایل نشون داده میشه
            await safe_edit(q.message, panel_done_text())
        elif job.id in JOBS:  # خطا: فایل می‌مونه تا کاربر دوباره امتحان کنه (تا JOB_TTL)
            job.created = time.time()
            text, markup = render(job, note)
            await safe_edit(q.message, text, markup)


async def on_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """cv:{job}:f:{fmt} ← فرمت انتخاب شد | cv:{job}:b:{fmt}:{token} ← بیت‌ریت انتخاب شد (FLAC مستقیم تبدیل میشه)"""
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) < 4 or parts[2] not in {"f", "b"}:
        await q.answer()
        return
    job = JOBS.get(parts[1])
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if not CONVERT_ENABLED:
        await q.answer(T("alert.disabled"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()
    step, fmt = parts[2], parts[3]
    if fmt not in conv_formats(job):
        await q.answer()
        return
    if step == "f" and fmt != "flac":  # بعد از انتخاب فرمت، صفحه‌ی انتخاب بیت‌ریت
        await q.answer()
        text, markup = render_conv_rates(job, fmt)
        await safe_edit(q.message, text, markup)
        return
    token = ""
    if fmt != "flac":
        token = parts[4] if len(parts) > 4 else ""
        if token not in {t for t, _ in conv_options(job, fmt)}:
            await q.answer()
            return
    job.busy = True
    label = conv_label(fmt, token)
    st = ack_edit_bg(q, T("cv.progress", label=label))
    out: Optional[Path] = None
    note = ""
    failed = False
    try:
        async with SEM:
            out, size = await convert(job, fmt, token)
        if size > UPLOAD_LIMIT:
            raise OutputTooBig(size)
        caption = T("cv.caption", label=label, size=mb(size), old=mb(job.size))
        with out.open("rb") as f:
            sent = await context.bot.send_audio(
                chat_id=q.message.chat_id,
                audio=f,
                filename=f"{job.name}.{CONV_INFO[fmt]['ext']}",
                title=job.title,
                performer=job.artist or None,
                duration=int(job.duration),
                thumbnail=job.thumb.read_bytes() if job.thumb else None,
                caption=caption,
                reply_markup=more_markup(job, "cv"),
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )
        count_file(job.user_id, "out")
        note = T("cv.done", label=label)
        await after_delivery(job, "cv", sent)
    except OutputTooBig as e:
        failed = True
        log.warning("converted output %s bytes exceeds upload limit", e)
        note = T("cv.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        failed = True
        log.exception("convert/send failed")
        note = T("cv.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:  # فایل اصلی می‌مونه تا بشه به فرمت/بیت‌ریت دیگه‌ای هم تبدیلش کرد (تا JOB_TTL)
            job.created = time.time()
            if failed and fmt != "flac":
                text, markup = render_conv_rates(job, fmt, note)
            elif failed:
                text, markup = render_menu(job, note)
            else:
                text, markup = panel_done_text(note), None
            await safe_edit(q.message, text, markup)


async def on_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """tg:{job}:e:{title|artist|album} ← ویرایش متن | c ← کاور جدید | dc ← حذف کاور | clr:{فیلد} ← پاک‌کردن تگ
    x ← انصراف | rs ← برگردوندن تغییرات | go ← ساخت و ارسال فایل"""
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) < 3:
        await q.answer()
        return
    job = JOBS.get(parts[1])
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if not TAGS_ENABLED:
        await q.answer(T("alert.disabled"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()
    act = parts[2]
    field = parts[3] if len(parts) > 3 else ""
    ud = context.user_data
    info = await tg_load(job)
    if not info.get("kind"):  # فرمت پشتیبانی‌نشده
        ud.pop("tg_wait", None)
        await q.answer()
        await tg_show(q.message, job)
        return

    if act == "e" and field in _TG_FIELDS:
        ud.pop("ad_wait", None)
        ud["tg_wait"] = {"job": job.id, "field": field, "msg": q.message}
        await q.answer()
        text, markup = render_tag_prompt(job, field)
        await safe_edit(q.message, text, markup)
    elif act == "c":
        ud.pop("ad_wait", None)
        ud["tg_wait"] = {"job": job.id, "field": "cover", "msg": q.message}
        await q.answer()
        text, markup = render_tag_prompt(job, "cover")
        await safe_edit(q.message, text, markup)
    elif act == "x":
        ud.pop("tg_wait", None)
        await q.answer()
        await tg_show(q.message, job)
    elif act == "clr" and field in _TG_FIELDS:
        ud.pop("tg_wait", None)
        if (info.get(field) or "").strip():
            job.tg_new[field] = ""
        else:  # از قبل خالیه؛ چیزی برای پاک‌کردن نیست
            job.tg_new.pop(field, None)
        await q.answer()
        await tg_show(q.message, job)
    elif act == "dc":
        if job.tg_cover:
            with contextlib.suppress(OSError):
                job.tg_cover.unlink()
            job.tg_cover = None
        job.tg_drop_cover = bool(info.get("has_cover"))
        await q.answer()
        await tg_show(q.message, job)
    elif act == "rs":
        ud.pop("tg_wait", None)
        tg_reset(job)
        await q.answer()
        await tg_show(q.message, job)
    elif act == "go":
        if not tg_pending(job):
            await q.answer(T("alert.nochange"), show_alert=True)
            return
        ud.pop("tg_wait", None)
        await q.answer()
        await tg_apply(job, context, q.message)
    else:
        await q.answer()


async def on_tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام بعدیِ کاربر وقتی منتظر ورودی تگ/کاور هستیم (گروه -1؛ فقط وقتی منتظریم پیام رو برمیداره و جلوی بقیه‌ی هندلرها رو میگیره)."""
    ud = context.user_data
    wait = ud.get("tg_wait")
    msg = update.effective_message
    user = update.effective_user
    if not wait or msg is None or user is None or not allowed(user.id):
        return
    if msg.text and msg.text.strip() == ADMIN_BTN:  # دکمه‌ی پنل ادمین: ورودی تگ نیست
        ud.pop("tg_wait", None)
        return
    job = JOBS.get(wait["job"])
    if job is None or job.user_id != user.id:
        ud.pop("tg_wait", None)
        await _drop_user_input(msg)
        await safe_edit(wait["msg"], T("alert.expired"))
        raise ApplicationHandlerStop
    if job.busy:  # کار قبلی در حالِ انجامه (وضعیتش توی همون پیام اصلی دیده میشه)
        if not await _drop_user_input(msg):  # فایل آهنگ و مشابهش پاک نمیشه؛ فقط خبر میدیم
            await msg.reply_text(T("alert.busy"))
        raise ApplicationHandlerStop
    job.created = time.time()
    field = wait["field"]
    is_img = bool(msg.photo) or bool(msg.document and _tg_is_image_doc(msg.document))
    if msg.document and not is_img:  # فایل غیرتصویری (مثلاً آهنگ جدید) ← بذار on_media کار خودش رو بکنه
        return

    if field == "cover":
        if not is_img:
            await _drop_user_input(msg)
            text, markup = render_tag_prompt(job, "cover", T("tg.e.notimg"))
            await safe_edit(wait["msg"], text, markup)
            raise ApplicationHandlerStop
        att = msg.photo[-1] if msg.photo else msg.document
        if getattr(att, "file_size", None) and att.file_size > TG_COVER_IN_LIMIT:
            await _drop_user_input(msg)
            text, markup = render_tag_prompt(job, "cover", T("tg.e.big", max=mb(TG_COVER_IN_LIMIT)))
            await safe_edit(wait["msg"], text, markup)
            raise ApplicationHandlerStop
        await safe_edit(wait["msg"], T("tg.preparing"))
        raw = job.dir / f"cover_raw_{uuid.uuid4().hex[:6]}"
        prepared: Optional[Path] = None
        try:
            await fetch_file(att, raw)
            prepared = await tg_prepare_cover(raw, job.dir)
        except Exception:
            log.exception("cover download/prepare failed")
        finally:
            with contextlib.suppress(OSError):
                raw.unlink()
        if prepared is None:
            await _drop_user_input(msg)
            text, markup = render_tag_prompt(
                job, "cover", T("tg.e.unread"))
            await safe_edit(wait["msg"], text, markup)
            raise ApplicationHandlerStop
        if job.tg_cover:
            with contextlib.suppress(OSError):
                job.tg_cover.unlink()
        job.tg_cover, job.tg_drop_cover = prepared, False
        ud.pop("tg_wait", None)
        await _drop_user_input(msg)  # عکسِ فرستاده‌شده مصرف شد؛ از چت پاک میشه تا شلوغ نشه
        await tg_show(wait["msg"], job)
        raise ApplicationHandlerStop

    # فیلد متنی
    err = None
    value = ""
    if not msg.text:
        err = T("tg.e.nottext")
    else:
        value = re.sub(r"\s+", " ", _cl_clean(msg.text))
        if not value:
            err = T("tg.e.empty")
        elif len(value) > TG_MAX_TEXT:
            err = T("tg.e.long", max=fa(TG_MAX_TEXT))
    if err:
        await _drop_user_input(msg)
        text, markup = render_tag_prompt(job, field, err)
        await safe_edit(wait["msg"], text, markup)
        raise ApplicationHandlerStop
    cur = ((job.tg_info or _TG_EMPTY).get(field) or "").strip()
    if value == cur:  # با مقدار فعلی یکیه؛ تغییری نیست
        job.tg_new.pop(field, None)
    else:
        job.tg_new[field] = value
    ud.pop("tg_wait", None)
    with contextlib.suppress(TelegramError):  # پیام کاربر رو پاک میکنیم تا چت شلوغ نشه
        await msg.delete()
    await tg_show(wait["msg"], job)
    raise ApplicationHandlerStop


async def on_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """tr:{job}:t ← (تغییر) زمان | x ← انصراف | fd ← روشن/خاموش Fade | f:{mp3|aac|m4r} ← برش و ارسال"""
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) < 3:
        await q.answer()
        return
    job = JOBS.get(parts[1])
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if not TRIM_ENABLED:
        await q.answer(T("alert.disabled"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()
    act = parts[2]
    ud = context.user_data

    if act == "t":
        ud["tr_wait"] = {"job": job.id, "msg": q.message}
        await q.answer()
        text, markup = render_trim_prompt(job)
        await safe_edit(q.message, text, markup)
    elif act == "x":
        ud.pop("tr_wait", None)
        await q.answer()
        text, markup = render_trim_formats(job) if job.tr_start is not None else render_menu(job)
        await safe_edit(q.message, text, markup)
    elif act == "fd":
        job.tr_fade = not job.tr_fade
        await q.answer()
        text, markup = render_trim_formats(job)
        await safe_edit(q.message, text, markup)
    elif act == "f":
        fmt = parts[3] if len(parts) > 3 else ""
        if fmt not in TRIM_INFO or job.tr_start is None or job.tr_end is None:
            await q.answer()
            return
        if fmt == "m4r" and job.tr_end - job.tr_start > TRIM_M4R_MAX + 0.05:
            await q.answer(T("tr.alert_m4r", m4r_max=fa(int(TRIM_M4R_MAX))), show_alert=True)
            return
        ud.pop("tr_wait", None)
        await q.answer()
        await trim_apply(job, context, q.message, fmt)
    else:
        await q.answer()


async def on_trim_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام متنیِ بعدیِ کاربر وقتی منتظر زمان شروع/پایان برش هستیم (گروه -2؛ فقط وقتی منتظریم پیام رو برمی‌داره)."""
    ud = context.user_data
    wait = ud.get("tr_wait")
    msg = update.effective_message
    user = update.effective_user
    if not wait or msg is None or user is None or not msg.text or not allowed(user.id):
        return
    if msg.text.strip() == ADMIN_BTN:  # دکمه‌ی پنل ادمین: ورودی برش نیست
        ud.pop("tr_wait", None)
        return
    job = JOBS.get(wait["job"])
    if job is None or job.user_id != user.id:
        ud.pop("tr_wait", None)
        await _drop_user_input(msg)
        await safe_edit(wait["msg"], T("alert.expired"))
        raise ApplicationHandlerStop
    if job.busy:  # کار قبلی در حالِ انجامه (وضعیتش توی همون پیام اصلی دیده میشه)؛ پیام اضافه نمیدیم
        await _drop_user_input(msg)
        raise ApplicationHandlerStop
    job.created = time.time()
    try:
        start, end, clamped = tr_parse(msg.text, job.duration)
    except ValueError as e:
        await _drop_user_input(msg)  # خطا توی همون پیام اصلی نشون داده میشه، نه پیام جدید
        text, markup = render_trim_prompt(job, str(e))
        await safe_edit(wait["msg"], text, markup)
        raise ApplicationHandlerStop
    job.tr_start, job.tr_end = start, end
    ud.pop("tr_wait", None)
    await _drop_user_input(msg)  # پیام کاربر رو پاک میکنیم تا چت شلوغ نشه
    note = T("tr.note.clamped") if clamped else ""
    text, markup = render_trim_formats(job, note)
    await safe_edit(wait["msg"], text, markup)
    raise ApplicationHandlerStop


def render_wm_prompt(job: Job, note: str = ""):
    text = T("wm.prompt.head") + "\n" + T("wm.prompt.body", max=fa(WM_TEXT_MAX))
    if note:
        text = f"{note}\n\n{text}"
    rows = [[InlineKeyboardButton(T("btn.cancel_x"), callback_data=f"mn:{job.id}:back", api_kwargs=STYLE_RED)]]
    return text, InlineKeyboardMarkup(rows)


async def on_watermark_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """متن امضا از کاربر (گروه -3؛ فقط وقتی منتظر متن امضا هستیم پیام رو برمی‌داره)."""
    ud = context.user_data
    wait = ud.get("wm_wait")
    msg = update.effective_message
    user = update.effective_user
    if not wait or msg is None or user is None or not msg.text or not allowed(user.id):
        return
    if msg.text.strip() == ADMIN_BTN:  # دکمه‌ی پنل ادمین: ورودی امضا نیست
        ud.pop("wm_wait", None)
        return
    job = JOBS.get(wait["job"])
    if job is None or job.user_id != user.id:
        ud.pop("wm_wait", None)
        await _drop_user_input(msg)
        await safe_edit(wait["msg"], T("alert.expired"))
        raise ApplicationHandlerStop
    if job.busy:
        await _drop_user_input(msg)
        raise ApplicationHandlerStop
    job.created = time.time()
    message = wait["msg"]

    lines = [ln.strip() for ln in msg.text.splitlines() if ln.strip()][:3]
    text = "\n".join(lines)
    if not lines or max(len(ln) for ln in lines) > WM_TEXT_MAX:
        await _drop_user_input(msg)  # خطا توی همون پیام اصلی نشون داده میشه
        t, m = render_wm_prompt(job, T("wm.e.long", max=fa(WM_TEXT_MAX)))
        await safe_edit(message, t, m)
        raise ApplicationHandlerStop

    # قبل از هر await: استیت مصرف و job قفل میشه تا دو پیام پشت‌سرهم دوبار اجرا نشن
    ud.pop("wm_wait", None)
    job.busy = True
    await _drop_user_input(msg)
    one_line = " / ".join(lines)
    st = edit_bg(message, T("wm.progress", text=one_line))
    out: Optional[Path] = None
    note = ""
    ok = False
    try:
        async with SEM:
            out, size = await watermark_encode(job, text)
        common = dict(
            chat_id=message.chat_id,
            filename=f"{_cl_safe_name(job.name + ' - signed')}{out.suffix}",
            caption=T("wm.caption", text=one_line, song=_song(job), size=_cl_kb(size))[:1024],
            reply_markup=more_markup(job, "wm"), read_timeout=300, write_timeout=300, connect_timeout=60,
        )
        with out.open("rb") as f:
            try:
                sent = await context.bot.send_audio(
                    audio=f, title=job.title, performer=job.artist or None, duration=int(round(job.duration)),
                    thumbnail=job.thumb.read_bytes() if job.thumb else None, **common,
                )
            except BadRequest:
                f.seek(0)
                sent = await context.bot.send_document(document=f, **common)
        count_file(job.user_id, "out")
        note = T("wm.done")
        ok = True
        await after_delivery(job, "wm", sent)
    except OutputTooBig as e:
        log.warning("watermarked output %s bytes exceeds upload limit", e)
        note = T("wm.toobig", size=mb(int(str(e))), limit=mb(UPLOAD_LIMIT))
    except Exception:
        log.exception("watermark failed")
        note = T("wm.fail")
    finally:
        await settle(st)
        job.busy = False
        if out:
            with contextlib.suppress(OSError):
                out.unlink()
        if job.id in JOBS:
            job.created = time.time()
            if ok:
                t, m = panel_done_text(note), None
            else:
                t, m = render_menu(job, note)  # بازگشت به منوی آهنگ
            await safe_edit(message, t, m)
    raise ApplicationHandlerStop


async def on_norm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """nm:{job}:g:{fmt}:{token} ← نرمال‌سازی و ارسال با فرمت انتخابی"""
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) < 4 or parts[2] != "g":
        await q.answer()
        return
    job = JOBS.get(parts[1])
    if job is None:
        await q.answer(T("alert.expired"), show_alert=True)
        return
    if q.from_user.id != job.user_id:
        await q.answer(T("alert.not_yours"), show_alert=True)
        return
    if not NORM_ENABLED:
        await q.answer(T("alert.disabled"), show_alert=True)
        return
    if job.busy:
        await q.answer(T("alert.busy"))
        return
    job.created = time.time()
    fmt, token = parts[3], (parts[4] if len(parts) > 4 else "")
    if job.loud is None or not norm_needed(job.loud) or (fmt, token) not in norm_options(job):
        await q.answer()
        return
    await q.answer()
    await norm_apply(job, context, q.message, fmt, token)


# ───────────────────────── پنل ادمین ─────────────────────────

ADMIN_BTN = "🛠 پنل ادمین"
ADMIN_KB = ReplyKeyboardMarkup([[KeyboardButton(ADMIN_BTN)]], resize_keyboard=True)
PANEL_TEXT = "🛠 پنل ادمین\nیکی از گزینه‌ها رو انتخاب کن:"


def admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار", callback_data="ad:stats"),
            InlineKeyboardButton("👥 کاربران", callback_data="ad:users"),
        ],
        [
            InlineKeyboardButton("💎 پرمیوم", callback_data="ad:pm"),
            InlineKeyboardButton("📢 پیام همگانی", callback_data="ad:bc"),
        ],
        [
            InlineKeyboardButton("🚫 بن کاربر", callback_data="ad:ban"),
            InlineKeyboardButton("✅ آنبن کاربر", callback_data="ad:unban"),
        ],
        [InlineKeyboardButton("🖼 بنر تبلیغات", callback_data="ad:bn")],
        [InlineKeyboardButton("📖 ویرایش متن راهنما", callback_data=f"ad:txi:{TEXT_SID['help.body']}")],
        [InlineKeyboardButton("✏️ ویرایش متن‌ها و دکمه‌ها", callback_data="ad:tx")],
        [InlineKeyboardButton("↕️ ترتیب دکمه‌های منوی آهنگ", callback_data="ad:mo")],
        [InlineKeyboardButton("🧹 پاکسازی فایل‌های موقت", callback_data="ad:clean")],
        [InlineKeyboardButton("✖️ بستن", callback_data="ad:close")],
    ])


def mo_enabled(act: str) -> bool:
    return {
        "cv": CONVERT_ENABLED, "tr": TRIM_ENABLED, "nm": NORM_ENABLED, "bs": BASS_ENABLED,
        "wm": WM_ENABLED, "tg": TAGS_ENABLED, "cl": CL_ENABLED,
    }.get(act, True)


def mo_text() -> str:
    lines = [
        "↕️ ترتیب دکمه‌های منوی آهنگ",
        "با ⬆️ و ⬇️ جای هر دکمه رو عوض کن و با «↔️» بذارش کنار دکمه‌ی قبلی (دو تا دکمه توی یه ردیف). همون لحظه ذخیره میشه و برای منوی آهنگ‌های بعدی "
        "(و دکمه‌های زیر فایل ارسالی) اعمال میشه.",
        "",
    ]
    joined = set(DB.get("menu_join", []))
    for i, a in enumerate(menu_order(), 1):
        off = "   ⛔ (توی .env خاموشه)" if not mo_enabled(a) else ""
        side = "↔️ " if (i > 1 and a in joined) else ""
        lines.append(f"{fa(i)}. {side}{MENU_ACT_NAMES[a]}{off}")
    lines += ["", "ℹ️ «کاور و ترانه» فقط وقتی توی فایل آهنگ کاور یا ترانه باشه نشون داده میشه."]
    return "\n".join(lines)


def mo_markup() -> InlineKeyboardMarkup:
    order = menu_order()
    joined = set(DB.get("menu_join", []))
    rows: List[List[InlineKeyboardButton]] = []
    for i, a in enumerate(order):
        up = InlineKeyboardButton("⬆️", callback_data=f"ad:mov:{a}:u") if i > 0 else InlineKeyboardButton("▫️", callback_data="ad:mon")
        down = (
            InlineKeyboardButton("⬇️", callback_data=f"ad:mov:{a}:d") if i < len(order) - 1
            else InlineKeyboardButton("▫️", callback_data="ad:mon")
        )
        if i == 0:
            side = InlineKeyboardButton("▫️", callback_data="ad:mon")
        else:
            side = InlineKeyboardButton("✅ کنار قبلی" if a in joined else "↔️", callback_data=f"ad:moj:{a}")
        rows.append([InlineKeyboardButton(f"{fa(i + 1)}. {MENU_ACT_NAMES[a]}", callback_data="ad:mon"), up, down, side])
    rows.append([InlineKeyboardButton("♻️ ترتیب پیش‌فرض", callback_data="ad:mor")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ad:home")])
    return InlineKeyboardMarkup(rows)


def _back_markup(*extra: InlineKeyboardButton, to: str = "home") -> InlineKeyboardMarkup:
    row = [*extra, InlineKeyboardButton("🔙 بازگشت", callback_data=f"ad:{to}")]
    return InlineKeyboardMarkup([row])


def ad_panel_text() -> str:
    cfg = ad_cfg()
    src = "🖼 عکس دلخواه خودت" if cfg.get("fid") else "📌 بنر پیش‌فرض («جای تبلیغات شما»)"
    return (
        "🖼 بنر تبلیغات\n"
        "این عکس قبل از پیام شروع (/start) برای کاربرها فرستاده میشه.\n\n"
        f"وضعیت: {'🟢 روشن' if ad_enabled() else '🔴 خاموش'}\n"
        f"محتوا: {src}"
    )


def ad_panel_markup() -> InlineKeyboardMarkup:
    cfg = ad_cfg()
    rows = [
        [InlineKeyboardButton("🔴 خاموش کردن" if ad_enabled() else "🟢 روشن کردن", callback_data="ad:bnt")],
        [InlineKeyboardButton("✏️ تغییر بنر", callback_data="ad:bne"), InlineKeyboardButton("👁 پیش‌نمایش", callback_data="ad:bnp")],
    ]
    if cfg.get("fid"):
        rows.append([InlineKeyboardButton("🗑 حذف عکسم (برگشت به پیش‌فرض)", callback_data="ad:bnd")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="ad:home")])
    return InlineKeyboardMarkup(rows)


PREMIUM_TEXT = "💎 مدیریت پرمیوم\nیکی از گزینه‌ها رو انتخاب کن:"


def premium_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ اعطای پرمیوم", callback_data="ad:pg"),
            InlineKeyboardButton("➖ حذف پرمیوم", callback_data="ad:pr"),
        ],
        [InlineKeyboardButton("📋 لیست پرمیوم‌ها", callback_data="ad:pl")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="ad:home")],
    ])


def duration_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏳ {label}", callback_data=f"ad:pd:{key}") for key, (_, label) in PREMIUM_DURS.items()],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="ad:pm")],
    ])


def admin_premium_text() -> str:
    items = [(int(k), premium_status(int(k))) for k in DB.get("premium", {})]
    items = [(uid, st) for uid, st in items if st is not None]
    if not items:
        return "💎 هنوز هیچ کاربر پرمیومی نداری."
    items.sort(key=lambda x: x[1], reverse=True)
    lines = [f"💎 کاربران پرمیوم ({fa(len(items))} نفر):\n"]
    for i, (uid, st) in enumerate(items[:30], 1):
        left = "دائمی" if st == math.inf else f"{_uptime(st)} دیگه"
        lines.append(f"{fa(i)}. {user_label(uid)}\n    ⏳ {left}")
    if len(items) > 30:
        lines.append(f"\n… و {fa(len(items) - 30)} نفر دیگه")
    return "\n".join(lines)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            with contextlib.suppress(OSError):
                if f.is_file():
                    total += f.stat().st_size
    except OSError:
        pass
    return total


def _uptime(sec: float) -> str:
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append(f"{fa(d)} روز")
    if h:
        parts.append(f"{fa(h)} ساعت")
    parts.append(f"{fa(m)} دقیقه")
    return " و ".join(parts)


def admin_stats_text() -> str:
    now = time.time()
    users = DB["users"]
    active = sum(1 for u in users.values() if now - u.get("last", 0) < 86400)
    busy = sum(1 for j in JOBS.values() if j.busy)
    return (
        "📊 آمار ربات\n\n"
        f"👥 کاربران: {fa(len(users))}  (فعال 24 ساعت اخیر: {fa(active)})\n"
        f"💎 پرمیوم فعال: {fa(sum(1 for k in DB.get('premium', {}) if premium_status(int(k)) is not None))}\n"
        f"🚫 بن‌شده: {fa(len(BANNED))}\n"
        f"📥 فایل دریافتی: {fa(DB.get('files_in', 0))}\n"
        f"📤 فایل فشرده‌شده و ارسالی: {fa(DB.get('files_out', 0))}\n\n"
        f"⚙️ کار در حال انتظار: {fa(len(JOBS))}  (در حال پردازش: {fa(busy)})\n"
        f"💾 فایل‌های موقت: {mb(_dir_size(TEMP_DIR))} مگ\n"
        f"🌐 سرور لوکال: {'فعال' if LOCAL_API_URL else 'غیرفعال'}\n"
        f"⏱ آپتایم: {_uptime(now - START_TIME)}"
    )


def admin_users_text(limit: int = 15) -> str:
    users = sorted(DB["users"].items(), key=lambda kv: kv[1].get("last", 0), reverse=True)
    if not users:
        return "👥 هنوز کاربری ثبت نشده."
    lines = [f"👥 کاربران ({fa(len(users))} نفر) — {fa(min(limit, len(users)))} تای آخر:\n"]
    for i, (uid, u) in enumerate(users[:limit], 1):
        name = (u.get("name") or "بی‌نام").strip()
        uname = f" @{u['username']}" if u.get("username") else ""
        ban = (" 🚫" if int(uid) in BANNED else "") + (" 💎" if premium_status(int(uid)) is not None else "")
        lines.append(f"{fa(i)}. {name}{uname}{ban}\n    {uid} • {fa(u.get('files', 0))} فایل")
    return "\n".join(lines)


async def _broadcast(bot: Bot, text: str) -> Tuple[int, int, int]:
    ok = blocked = failed = 0
    for key in list(DB["users"]):
        uid = int(key)
        if uid in BANNED or is_admin(uid):
            continue
        for attempt in (1, 2):
            try:
                await bot.send_message(uid, text)
                ok += 1
            except RetryAfter as e:
                if attempt == 1:
                    await asyncio.sleep(float(e.retry_after) + 1)
                    continue
                failed += 1
            except Forbidden:
                blocked += 1
            except TelegramError:
                failed += 1
            break
        await asyncio.sleep(0.05)
    return ok, blocked, failed


async def on_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("ad_wait", None)
    context.user_data.pop("tg_wait", None)
    context.user_data.pop("tr_wait", None)
    context.user_data.pop("wm_wait", None)
    await update.effective_message.reply_text(PANEL_TEXT, reply_markup=admin_markup())


async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دکمه‌ی کیبورد ریپلای + پاسخ‌های متنی ادمین (متن پیام همگانی / آیدی برای بن و آنبن)."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    ud = context.user_data
    if text == ADMIN_BTN:
        ud.pop("ad_wait", None)
        await msg.reply_text(PANEL_TEXT, reply_markup=admin_markup())
        return
    wait = ud.get("ad_wait")
    if not wait:
        return
    if wait.startswith("tx:"):  # ویرایش یکی از متن‌ها/دکمه‌های ربات
        sid = wait[3:]
        key = TEXT_IDS.get(sid)
        if key is None:
            ud.pop("ad_wait", None)
            await msg.reply_text("این مرحله منقضی شده؛ دوباره از «ویرایش متن‌ها» شروع کن.", reply_markup=_back_markup(to="tx"))
            return
        err = tx_validate(key, text)
        if err:
            await msg.reply_text(f"{err}\n\nدوباره بفرست، یا برگرد.", reply_markup=_back_markup(to=f"txi:{sid}"))
            return
        ud.pop("ad_wait", None)
        if text == TEXTS[key].default:
            DB["texts"].pop(key, None)
            note = "♻️ این متن با پیش‌فرض یکی بود؛ به حالت پیش‌فرض برگشت."
        else:
            DB["texts"][key] = text
            note = "✅ ذخیره شد و همین الان اعمال میشه."
        db_save()
        await msg.reply_text(f"{note}\n\n{tx_item_text(key)}", reply_markup=tx_item_markup(key))
        return
    if wait == "bn":  # منتظر عکس بنر بودیم ولی متن فرستاد
        await msg.reply_text(
            "🖼 باید یه عکس بفرستی (به‌صورت Photo، نه File). یا برگرد.", reply_markup=_back_markup(to="bn")
        )
        return
    if wait == "bc":
        ud.pop("ad_wait", None)
        ud["ad_bc"] = text
        targets = sum(1 for k in DB["users"] if int(k) not in BANNED and not is_admin(int(k)))
        await msg.reply_text(
            f"📢 پیش‌نمایش پیام:\n\n{text}\n\n— — —\nبه {fa(targets)} نفر ارسال بشه؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ ارسال", callback_data="ad:bcgo"),
                InlineKeyboardButton("❌ لغو", callback_data="ad:home"),
            ]]),
        )
        return
    # بن / آنبن / اعطای پرمیوم / حذف پرمیوم: ورودی = آیدی عددی یا یوزرنیم
    back = "pm" if wait in ("pg", "pr") else "home"
    uid = resolve_user(text)
    if uid is None:
        await msg.reply_text(
            "😕 کاربر پیدا نشد. آیدی عددی بفرست، یا یوزرنیمی که قبلاً به ربات پیام داده. (یا برگرد)",
            reply_markup=_back_markup(to=back),
        )
        return
    ud.pop("ad_wait", None)
    if wait == "pg":
        ud["ad_pt"] = uid
        await msg.reply_text(f"💎 کاربر {user_label(uid)}\nچه مدت پرمیوم بشه؟", reply_markup=duration_markup())
        return
    if wait == "pr":
        if revoke_premium(uid):
            done = f"➖ پرمیوم {user_label(uid)} حذف شد."
            with contextlib.suppress(TelegramError):
                await context.bot.send_message(uid, T("pm.revoked"))
        else:
            done = f"{user_label(uid)} پرمیوم نبود."
        await msg.reply_text(done, reply_markup=_back_markup(to="pm"))
        return
    if wait == "ban":
        if is_admin(uid):
            await msg.reply_text("نمیشه خودت رو بن کنی 🙂", reply_markup=_back_markup())
            return
        BANNED.add(uid)
        done = f"🚫 کاربر {user_label(uid)} بن شد."
    else:
        BANNED.discard(uid)
        done = f"✅ کاربر {user_label(uid)} آنبن شد."
    db_save()
    await msg.reply_text(done, reply_markup=_back_markup())


async def on_admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عکس جدید بنر تبلیغات (فقط وقتی ادمین توی مرحله‌ی «تغییر بنر» باشه)."""
    ud = context.user_data
    msg = update.effective_message
    if ud.get("ad_wait") != "bn" or not msg.photo:
        return
    ud.pop("ad_wait", None)
    ad_cfg()["fid"] = msg.photo[-1].file_id
    db_save()
    await msg.reply_text("✅ بنر جدید ذخیره شد.\n\n" + ad_panel_text(), reply_markup=ad_panel_markup())


async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("دسترسی نداری 🙂", show_alert=True)
        return
    action = q.data.split(":", 1)[1]
    ud = context.user_data
    if action != "bcgo":
        ud.pop("ad_wait", None)
        ud.pop("tg_wait", None)
        ud.pop("tr_wait", None)
        ud.pop("wm_wait", None)

    if action == "home":
        ud.pop("ad_bc", None)
        ud.pop("ad_pt", None)
        await q.answer()
        await safe_edit(q.message, PANEL_TEXT, admin_markup())
    elif action == "pm":
        ud.pop("ad_pt", None)
        await q.answer()
        await safe_edit(q.message, PREMIUM_TEXT, premium_markup())
    elif action in ("pg", "pr"):
        ud["ad_wait"] = action
        await q.answer()
        what = "پرمیوم بشه" if action == "pg" else "پرمیومش حذف بشه"
        await safe_edit(
            q.message,
            f"آیدی عددی یا یوزرنیم (مثل @name) کاربری که میخوای {what} رو بفرست:\n\n"
            "ℹ️ یوزرنیم فقط برای کسانی کار میکنه که قبلاً به ربات پیام دادن؛ با آیدی عددی برای همه جواب میده.",
            _back_markup(to="pm"),
        )
    elif action == "pl":
        await q.answer()
        await safe_edit(q.message, admin_premium_text(), _back_markup(to="pm"))
    elif action.startswith("pd:"):
        uid = ud.pop("ad_pt", None)
        dur = action[3:]
        if uid is None or dur not in PREMIUM_DURS:
            await q.answer("این مرحله منقضی شده؛ دوباره از بخش پرمیوم شروع کن.", show_alert=True)
            return
        grant_premium(uid, dur)
        st = premium_status(uid)
        left = "دائمی" if st == math.inf else f"{_uptime(st or 0)} دیگه"
        await q.answer()
        await safe_edit(
            q.message,
            f"💎 {user_label(uid)}\nپرمیوم شد: {PREMIUM_DURS[dur][1]}\n⏳ اعتبار فعلی: {left}",
            _back_markup(to="pm"),
        )
        with contextlib.suppress(TelegramError):
            await context.bot.send_message(uid, T("pm.granted", dur=PREMIUM_DURS[dur][1]))
    elif action == "stats":
        await q.answer()
        await safe_edit(
            q.message, admin_stats_text(),
            _back_markup(InlineKeyboardButton("🔄 تازه‌سازی", callback_data="ad:stats")),
        )
    elif action == "users":
        await q.answer()
        await safe_edit(q.message, admin_users_text(), _back_markup())
    elif action == "bc":
        ud["ad_wait"] = "bc"
        await q.answer()
        await safe_edit(q.message, "📢 متن پیام همگانی رو بفرست:", _back_markup())
    elif action in ("ban", "unban"):
        ud["ad_wait"] = action
        await q.answer()
        what = "بن" if action == "ban" else "آنبن"
        await safe_edit(q.message, f"آیدی عددی یا یوزرنیم کاربری که میخوای {what} بشه رو بفرست:", _back_markup())
    elif action == "bcgo":
        text = ud.pop("ad_bc", None)
        if not text:
            await q.answer("پیامی برای ارسال نیست.", show_alert=True)
            return
        await q.answer()
        await safe_edit(q.message, "⏳ در حال ارسال پیام همگانی…")
        ok, blocked, failed = await _broadcast(context.bot, text)
        await safe_edit(
            q.message,
            f"📢 ارسال تموم شد\n✅ موفق: {fa(ok)}\n🚷 ربات رو بلاک کرده: {fa(blocked)}\n⚠️ ناموفق: {fa(failed)}",
            _back_markup(),
        )
    elif action == "clean":
        n = 0
        for jid, job in list(JOBS.items()):
            if not job.busy:
                drop_job(jid)
                n += 1
        await q.answer()
        await safe_edit(q.message, f"🧹 {fa(n)} کار بی‌استفاده پاک شد.\n💾 فایل‌های موقت: {mb(_dir_size(TEMP_DIR))} مگ", _back_markup())
    elif action == "tx":
        await q.answer()
        await safe_edit(q.message, tx_main_text(), tx_main_markup())
    elif action.startswith("txg:"):
        try:
            _, gi_s, pg_s = action.split(":")
            gi, pg = int(gi_s), int(pg_s)
        except ValueError:
            await q.answer()
            return
        if not 0 <= gi < len(TEXT_GROUPS):
            await q.answer()
            return
        await q.answer()
        text, markup = tx_group_view(gi, pg)
        await safe_edit(q.message, text, markup)
    elif action.startswith(("txi:", "txe:", "txr:")):
        op, sid = action.split(":", 1)
        key = TEXT_IDS.get(sid)
        if key is None:
            await q.answer("این مورد دیگه وجود نداره.", show_alert=True)
            return
        if op == "txi":
            await q.answer()
            await safe_edit(q.message, tx_item_text(key), tx_item_markup(key))
        elif op == "txe":
            ud["ad_wait"] = f"tx:{sid}"
            await q.answer()
            await safe_edit(q.message, tx_edit_prompt(key), _back_markup(to=f"txi:{sid}"))
        else:  # txr: برگردوندن به پیش‌فرض
            DB["texts"].pop(key, None)
            db_save()
            await q.answer("♻️ به حالت پیش‌فرض برگشت.")
            await safe_edit(q.message, tx_item_text(key), tx_item_markup(key))
    elif action == "txra":
        n_done = sum(1 for k in TEXTS if tx_custom(k))
        await q.answer()
        await safe_edit(
            q.message,
            f"♻️ همه‌ی {fa(n_done)} متن ویرایش‌شده به حالت پیش‌فرض برگرده؟\nاین کار قابل‌برگشت نیست.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله، بازنشانی کن", callback_data="ad:txrac"),
                InlineKeyboardButton("❌ نه", callback_data="ad:tx"),
            ]]),
        )
    elif action == "txrac":
        DB["texts"].clear()
        db_save()
        await q.answer("♻️ همه به حالت پیش‌فرض برگشتن.")
        await safe_edit(q.message, tx_main_text(), tx_main_markup())
    elif action in ("mo", "mon", "mor") or action.startswith(("mov:", "moj:")):  # ترتیب دکمه‌های منوی آهنگ
        if action == "mon":  # ردیف اسم / خانه‌ی خالی؛ فقط کلیک رو می‌بندیم
            await q.answer()
            return
        if action == "mor":
            DB["menu_order"] = []
            DB["menu_join"] = []
            db_save()
            await q.answer("♻️ ترتیب پیش‌فرض برگشت.")
        elif action.startswith("moj:"):  # کنار قبلی روشن/خاموش
            act = action.split(":", 1)[1]
            if act in MENU_DEFAULT_ORDER and menu_order().index(act) > 0:
                join = [a for a in DB.get("menu_join", []) if a in MENU_DEFAULT_ORDER]
                if act in join:
                    join.remove(act)
                else:
                    join.append(act)
                DB["menu_join"] = join
                db_save()
            await q.answer()
        elif action.startswith("mov:"):
            bits = action.split(":")
            order = menu_order()
            if len(bits) == 3 and bits[1] in order and bits[2] in ("u", "d"):
                i = order.index(bits[1])
                j = i - 1 if bits[2] == "u" else i + 1
                if 0 <= j < len(order):
                    order[i], order[j] = order[j], order[i]
                    DB["menu_order"] = order
                    db_save()
            await q.answer()
        else:
            await q.answer()
        await safe_edit(q.message, mo_text(), mo_markup())
    elif action in ("bn", "bnt", "bne", "bnd", "bnp"):  # بنر تبلیغات
        if action == "bne":
            ud["ad_wait"] = "bn"
            await q.answer()
            await safe_edit(
                q.message,
                "🖼 عکس بنر جدید رو بفرست (به‌صورت Photo، نه File).\n"
                "پیشنهاد: عکس افقی، مثلاً با نسبت 2:1 یا 16:9.",
                _back_markup(to="bn"),
            )
            return
        if action == "bnp":
            await q.answer()
            if not await send_ad_banner(q.message, force=True):
                await q.message.reply_text("⚠️ بنر ارسال نشد؛ لاگ ربات رو ببین.")
            return
        if action == "bnt":
            cfg = ad_cfg()
            cfg["on"] = not ad_enabled()
            db_save()
            await q.answer("🟢 بنر روشن شد" if cfg["on"] else "🔴 بنر خاموش شد")
        elif action == "bnd":
            ad_cfg().pop("fid", None)
            db_save()
            await q.answer("🗑 حذف شد؛ بنر پیش‌فرض نمایش داده میشه.")
        else:
            await q.answer()
        await safe_edit(q.message, ad_panel_text(), ad_panel_markup())
    elif action == "close":
        await q.answer()
        with contextlib.suppress(TelegramError):
            await q.message.delete()
    else:
        await q.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


# ───────────────────────── راه‌اندازی ─────────────────────────


async def _health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_web_server() -> None:
    """وب‌سرور سبک aiohttp برای هلث‌چک رندر؛ تا وقتی تسک کنسل نشه روی 0.0.0.0:PORT می‌مونه."""
    runner = web.AppRunner(_make_web_app(), access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
    except OSError as e:
        log.error("وب‌سرور روی پورت %s بالا نیومد: %s", PORT, e)
        await runner.cleanup()
        return
    log.info("Health web server listening on 0.0.0.0:%s", PORT)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _make_web_app() -> web.Application:
    web_app = web.Application()
    web_app.router.add_get("/", _health)  # HEAD هم خودکار پشتیبانی میشه
    web_app.router.add_get("/health", _health)
    return web_app


async def keep_alive() -> None:
    """هر KEEPALIVE_INTERVAL ثانیه به آدرس عمومی خودش درخواست میزنه تا رندر سرویس رو نخوابونه."""
    if not KEEPALIVE_URL:
        log.info("KEEPALIVE_URL/RENDER_EXTERNAL_URL تنظیم نشده؛ پینگ خودکار غیرفعاله.")
        return
    await asyncio.sleep(30)  # صبر کن وب‌سرور کامل بالا بیاد
    log.info("Keep-alive ON | هر %s ثانیه", KEEPALIVE_INTERVAL)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        while True:
            try:
                async with session.get(KEEPALIVE_URL) as r:
                    if r.status != 200:
                        log.warning("Keep-alive ping: HTTP %s", r.status)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Keep-alive ping failed: %s", e)
            await asyncio.sleep(KEEPALIVE_INTERVAL)


async def post_init(app: Application) -> None:
    global SEM, BOT
    # اول از همه پورت باز بشه تا رندر زود هلث‌چک رو پاس کنه (polling رو بلاک نمی‌کنه)
    app.bot_data["web_server"] = asyncio.create_task(run_web_server())
    app.bot_data["keep_alive"] = asyncio.create_task(keep_alive())
    SEM = asyncio.Semaphore(MAX_PARALLEL)
    BOT = app.bot
    me = await app.bot.get_me()
    if LOCAL_API_URL:
        log.info("Local Bot API: %s | bot=@%s | files dir=%s", LOCAL_API_URL, me.username, LOCAL_FILES_DIR)
        if not LOCAL_FILES_DIR.is_dir():
            log.warning("پوشه %s وجود نداره؛ Volume مشترک با سرور لوکال mount نشده.", LOCAL_FILES_DIR)
    else:
        log.info("Using cloud Bot API | bot=@%s", me.username)
    app.bot_data["sweeper"] = asyncio.create_task(sweeper())
    app.bot_data["db_saver"] = asyncio.create_task(db_saver())
    global CARDS_ENABLED
    if CARDS_ENABLED:
        if not cards_available():
            await asyncio.to_thread(ensure_card_fonts)  # اگه فونت نبود، یه‌بار تلاش برای دانلود Vazirmatn
        if not cards_available():
            CARDS_ENABLED = False
            log.warning("کارت گرافیکی خاموش شد: فونت فارسی پیدا نشد (Vazirmatn-Regular.ttf رو بذار توی پوشه‌ی fonts).")
        else:
            log.info("Graphic cards ON | raqm=%s | reshaper=%s", _RAQM, _reshape is not None)
    if ADMIN_ID:
        # با هر بار روشن‌شدن، کیبورد ادمین هم برای ادمین ظاهر میشه (بدون نیاز به /start)
        try:
            await app.bot.send_message(ADMIN_ID, "✅ ربات روشن شد.", reply_markup=ADMIN_KB)
        except TelegramError as e:
            log.warning("پیام شروع به ادمین ارسال نشد (اول به ربات /start بده): %s", e)
    else:
        log.info("ADMIN_ID تنظیم نشده؛ پنل ادمین غیرفعاله.")


async def post_shutdown(app: Application) -> None:
    for key in ("keep_alive", "web_server"):
        task = app.bot_data.get(key)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    db_save(now=True)  # آخرین ذخیره‌ی همزمان قبل از خاموشی
    if _TG_CLIENT is not None:
        with contextlib.suppress(Exception):
            await _TG_CLIENT.aclose()


# ───────────────────────── مدیریت سرور لوکال (حالت خودکار) ─────────────────────────

_SERVER_PROC: Optional[subprocess.Popen] = None


def _stop_local_server() -> None:
    if _SERVER_PROC and _SERVER_PROC.poll() is None:
        _SERVER_PROC.terminate()
        try:
            _SERVER_PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _SERVER_PROC.kill()


def _http_json(url: str, timeout: float = 15) -> Dict[str, Any]:
    """GET و برگردوندن JSON؛ جواب خطای HTTP (مثل 401) هم جواب معتبر حساب میشه. URL رو لاگ نکن (توکن داره)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}


def _ensure_server_binary() -> Path:
    LOCAL_SERVER_DIR.mkdir(parents=True, exist_ok=True)
    binp = LOCAL_SERVER_DIR / "telegram-bot-api"
    if not binp.exists():
        log.info("دانلود telegram-bot-api از %s ...", LOCAL_SERVER_BIN_URL)
        part = LOCAL_SERVER_DIR / "telegram-bot-api.part"
        req = urllib.request.Request(LOCAL_SERVER_BIN_URL, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, part.open("wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            raise SystemExit(f"دانلود فایل اجرایی سرور لوکال ناموفق بود: {e}") from e
        part.chmod(0o755)
        part.replace(binp)
        h = hashlib.sha256()
        with binp.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        log.info("دانلود شد (%s مگ) sha256=%s", mb(binp.stat().st_size), h.hexdigest())
    binp.chmod(0o755)
    try:
        out = subprocess.run([str(binp), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        binp.unlink(missing_ok=True)
        raise SystemExit(f"فایل اجرایی سرور لوکال روی این هاست اجرا نمیشه: {e}") from e
    if out.returncode != 0:
        raise SystemExit(
            f"telegram-bot-api اجرا نشد (کد {out.returncode}): {(out.stderr or out.stdout).strip()[:500]}\n"
            "احتمالاً کتابخونه‌ی سیستمی (libssl/zlib/glibc) روی هاست سازگار نیست."
        )
    log.info("telegram-bot-api: %s", (out.stdout or out.stderr).strip()[:200])
    return binp


def _cloud_logout_once() -> None:
    """ربات باید قبل از رفتن به سرور لوکال از سرور ابری logOut بشه (فقط یک‌بار)."""
    # علامت برای هر توکن جدا نگه داشته میشه تا چند ربات روی یک سرور لوکال هرکدوم یک‌بار logOut بشن
    marker = LOCAL_SERVER_DIR / f".cloud_logged_out_{hashlib.sha1(BOT_TOKEN.encode()).hexdigest()[:10]}"
    if marker.exists():
        return
    log.info("logOut از سرور ابری تلگرام (فقط یک‌بار)...")
    try:
        res = _http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/logOut", timeout=30)
    except Exception as e:  # noqa: BLE001
        log.warning("logOut انجام نشد (%s). اگه سرور لوکال Unauthorized داد، ربات رو دوباره اجرا کن.", e)
        return
    if res.get("ok") or res.get("error_code") == 401:
        marker.write_text(str(time.time()))
    log.info("logOut: %s", "ok" if res.get("ok") else res.get("description", res))


def start_local_server() -> None:
    global _SERVER_PROC
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH):
        raise SystemExit("TELEGRAM_API_ID و TELEGRAM_API_HASH توی .env تنظیم نشده.")
    binp = _ensure_server_binary()
    _cloud_logout_once()
    LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
    tmpd = LOCAL_SERVER_DIR / "tmp"
    tmpd.mkdir(parents=True, exist_ok=True)
    getme = f"{LOCAL_API_URL}/bot{BOT_TOKEN}/getMe"

    alive = False
    try:  # اگه از اجرای قبلی هنوز یه سرور روی همین پورت زنده‌ست، همون رو استفاده کن
        _http_json(getme, timeout=3)
        alive = True
        log.info("سرور لوکال از قبل روی پورت %s بالاست.", LOCAL_SERVER_PORT)
    except Exception:  # noqa: BLE001
        pass

    if not alive:
        base = [
            str(binp), f"--api-id={TELEGRAM_API_ID}", f"--api-hash={TELEGRAM_API_HASH}", "--local",
            f"--http-port={LOCAL_SERVER_PORT}", f"--dir={LOCAL_FILES_DIR}", f"--temp-dir={tmpd}",
        ]
        logf = (LOCAL_SERVER_DIR / "server.log").open("ab")
        proc: Optional[subprocess.Popen] = None
        for extra in (["--http-ip-address=127.0.0.1"], []):  # اول فقط روی localhost؛ اگه نسخه نشناخت بدون اون
            proc = subprocess.Popen(base + extra, stdout=logf, stderr=subprocess.STDOUT)
            time.sleep(2)
            if proc.poll() is None:
                break
        else:
            tail = (LOCAL_SERVER_DIR / "server.log").read_text(errors="ignore")[-800:]
            raise SystemExit(f"سرور لوکال بالا نیومد. آخر server.log:\n{tail}")
        _SERVER_PROC = proc
        atexit.register(_stop_local_server)

        deadline = time.time() + 90
        while True:
            if proc.poll() is not None:
                tail = (LOCAL_SERVER_DIR / "server.log").read_text(errors="ignore")[-800:]
                raise SystemExit(f"سرور لوکال وسط راه‌اندازی بسته شد. آخر server.log:\n{tail}")
            try:
                _http_json(getme, timeout=10)
                break
            except Exception:  # noqa: BLE001
                if time.time() > deadline:
                    raise SystemExit("سرور لوکال تا 90 ثانیه جواب نداد (server.log رو ببین).") from None
                time.sleep(1.5)

    res = _http_json(getme, timeout=20)
    if res.get("ok"):
        log.info("✅ سرور لوکال سالمه | bot=@%s", res["result"].get("username"))
    else:
        log.warning("سرور لوکال بالاست ولی getMe خطا داد: %s (چند دقیقه بعد دوباره امتحان کن)", res.get("description"))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN توی فایل .env تنظیم نشده.")
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        # روی هاست‌هایی که apt ندارن (مثل فالیکس): ffmpeg از پکیج pip گرفته میشه
        try:
            import static_ffmpeg

            static_ffmpeg.add_paths()
        except Exception as e:  # noqa: BLE001
            log.warning("static-ffmpeg load failed: %s", e)
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        raise SystemExit("ffmpeg/ffprobe پیدا نشد. static-ffmpeg رو توی requirements.txt نصب کن یا ffmpeg رو نصب کن.")
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if START_LOCAL_SERVER:
        start_local_server()

    builder = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(64)  # با پیش‌فرض کوچک، یه آپلود طولانی بقیه‌ی درخواست‌های API (ادیت پیام، answer، ...) رو پشت خودش صف میکرد
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if LOCAL_API_URL:
        builder = (
            builder.base_url(f"{LOCAL_API_URL}/bot")
            .base_file_url(f"{LOCAL_API_URL}/file/bot")
            .local_mode(True)
        )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)

    log.info("Storage: %s", "PostgreSQL" if DATABASE_URL else f"local file {DATA_FILE}")
    db_load()
    app = builder.build()
    if ADMIN_ID:
        admin_only = filters.User(user_id=ADMIN_ID)
        app.add_handler(CommandHandler("admin", on_admin_cmd, filters=admin_only))
        app.add_handler(MessageHandler(admin_only & filters.TEXT & ~filters.COMMAND, on_admin_text))
        app.add_handler(MessageHandler(admin_only & filters.PHOTO, on_admin_photo))
        app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^ad:"))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, on_pin_service))
    app.add_handler(CallbackQueryHandler(on_help_btn, pattern=r"^hp:"))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.ALL, on_media))
    if WM_ENABLED:
        # گروه -3: قبل از on_trim_input (-2) و on_tag_input (-1)؛ فقط وقتی منتظر متن امضا باشیم پیام رو برمی‌داره
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_watermark_input), group=-3)
    if TAGS_ENABLED:
        # گروه -1: قبل از بقیه‌ی هندلرها چک میشه؛ فقط وقتی کاربر منتظر ورودی تگ/کاور باشه پیام رو برمی‌داره
        app.add_handler(
            MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL, on_tag_input),
            group=-1,
        )
        app.add_handler(CallbackQueryHandler(on_tags, pattern=r"^tg:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^mn:"))
    app.add_handler(CallbackQueryHandler(on_more, pattern=r"^ot:"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^sz:"))
    app.add_handler(CallbackQueryHandler(on_convert, pattern=r"^cv:"))
    if TRIM_ENABLED:
        # گروه -2: قبل از on_tag_input (گروه -1)؛ فقط وقتی منتظر زمان برش باشیم متن رو برمی‌داره
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_trim_input), group=-2)
        app.add_handler(CallbackQueryHandler(on_trim, pattern=r"^tr:"))
    if NORM_ENABLED:
        app.add_handler(CallbackQueryHandler(on_norm, pattern=r"^nm:"))
    if BASS_ENABLED:
        app.add_handler(CallbackQueryHandler(on_bass, pattern=r"^bs:"))
    app.add_error_handler(on_error)

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
