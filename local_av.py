#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_av.py — اسکنر ویروس‌توتال با API رسمی v3 (چند کلید، چرخشی + فالبک)
==========================================================================
فقط یک درخواست: GET https://www.virustotal.com/api/v3/files/{sha256}
فایل هرگز آپلود نمی‌شود؛ فقط هش SHA-256 آن (به‌صورت تکه‌ای، بدون لود کامل در RAM) استعلام می‌شود.

استفاده در ربات (رابط بدون تغییر نسبت به نسخهٔ قبل):
    import local_av
    res = local_av.scan_fileobj(tf, "update.apk")      # tf = فایل موقت باز (seekable)
    if res["verdict"] in ("MALICIOUS", "SUSPICIOUS"): ...

.env / Environment در Render:
    LOCALAV_VT_KEYS=key1,key2,key3   چند کلید با کاما (یا فاصله/خط جدید/;) — به‌ترتیب چرخشی استفاده می‌شوند
                                     و اگر یکی 429 (محدودیت) یا 401/403 (نامعتبر) بدهد، خودکار کلید بعدی امتحان می‌شود.
                                     (نام‌های VT_API_KEYS و VT_API_KEY هم پذیرفته می‌شوند)
    LOCALAV_VT_MIN=5                 حداقل تعداد موتور «مخرب» برای وضعیت MALICIOUS (حذف+میوت)؛ کمتر از آن فقط هشدار
    LOCALAV_VT_CACHE_SEC=600         مدت کش نتیجهٔ هر هش (برای صرفه‌جویی در سهمیه)؛ 0 = بدون کش

سهمیهٔ کلید رایگان: حدود ۴ درخواست در دقیقه و ۵۰۰ در روز برای هر کلید.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import threading
import time
from typing import BinaryIO, Optional

import requests

__version__ = "3.0-vt-api-multikey"

logger = logging.getLogger("local_av")

API_URL = "https://www.virustotal.com/api/v3/files/{sha256}"
TIMEOUT = 12
CHUNK = 64 * 1024

COOLDOWN_RATE = 65        # ثانیه — بعد از 429 (محدودیت در دقیقه)
COOLDOWN_DAILY = 3600     # ثانیه — سهمیهٔ روزانه تمام شده
COOLDOWN_BAD_KEY = 3600   # ثانیه — 401/403 (کلید نامعتبر/مسدود)

try:
    _VT_BLOCK_MIN = max(1, int((os.environ.get("LOCALAV_VT_MIN") or "5").strip()))
except ValueError:
    _VT_BLOCK_MIN = 5

try:
    _CACHE_SEC = max(0, int((os.environ.get("LOCALAV_VT_CACHE_SEC") or "600").strip()))
except ValueError:
    _CACHE_SEC = 600


# ---------------------------------------------------------------------------
# مدیریت کلیدها
# ---------------------------------------------------------------------------
def _load_keys() -> list:
    keys, seen = [], set()
    for name in ("LOCALAV_VT_KEYS", "VT_API_KEYS", "VT_API_KEY"):
        raw = os.environ.get(name) or ""
        for k in re.split(r"[,;\s]+", raw):
            k = k.strip().strip("\"'")
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


KEYS: list = _load_keys()
_lock = threading.Lock()
_cooldown_until: dict = {}   # key -> epoch تا زمانی که نباید استفاده شود
_rr_counter = 0              # شمارندهٔ چرخش (Round-Robin)
_cache: dict = {}            # sha256 -> (epoch, result)


def keys_count() -> int:
    return len(KEYS)


def is_configured() -> bool:
    return bool(KEYS)


def reload_keys() -> int:
    """بازخوانی کلیدها از env (بدون ری‌استارت، اگر env در پروسه عوض شده باشد)."""
    global KEYS
    with _lock:
        KEYS = _load_keys()
        _cooldown_until.clear()
    return len(KEYS)


def _tag(key: str) -> str:
    """نام امن کلید برای لاگ (هرگز کلید کامل لاگ نمی‌شود)."""
    try:
        return f"#{KEYS.index(key) + 1}(…{key[-4:]})"
    except ValueError:
        return f"(…{key[-4:]})"


def _available_keys() -> list:
    """کلیدهای در دسترس، با شروع چرخشی تا بار بین کلیدها پخش شود."""
    global _rr_counter
    now = time.time()
    with _lock:
        n = len(KEYS)
        if n == 0:
            return []
        start = _rr_counter % n
        _rr_counter += 1
        order = KEYS[start:] + KEYS[:start]
        return [k for k in order if _cooldown_until.get(k, 0) <= now]


def _mark(key: str, seconds: int, why: str) -> None:
    with _lock:
        _cooldown_until[key] = time.time() + seconds
    logger.warning(f"[LOCALAV] کلید {_tag(key)} برای {seconds}s کنار گذاشته شد: {why}")


def _err_text(res) -> str:
    try:
        e = (res.json() or {}).get("error") or {}
        return f"{e.get('code', '')} {e.get('message', '')}".strip()
    except Exception:
        return ""


# برچسب وضعیت هر موتور (مثل صفحهٔ خود سایت)
_CAT_LABEL = {
    "malicious": ("🔴", "Malicious"),
    "suspicious": ("🟠", "Suspicious"),
    "undetected": ("✅", "Undetected"),
    "harmless": ("✅", "Harmless"),
    "type-unsupported": ("⚪", "Unable to process file type"),
    "timeout": ("⏱", "Timeout"),
    "confirmed-timeout": ("⏱", "Timeout"),
    "failure": ("⚠️", "Failure"),
}
_CAT_ORDER = {"malicious": 0, "suspicious": 1}
_TG_LIMIT = 4000   # سقف امن پیام تلگرام (۴۰۹۶)


# ---------------------------------------------------------------------------
# استعلام
# ---------------------------------------------------------------------------
def _parse_ok(sha256: str, res) -> dict:
    data = (res.json().get("data") or {}).get("attributes") or {}
    stats = data.get("last_analysis_stats") or {}
    malicious = int(stats.get("malicious", 0) or 0)
    suspicious = int(stats.get("suspicious", 0) or 0)
    undetected = int(stats.get("undetected", 0) or 0)
    tags = data.get("tags") or []
    file_type = data.get("type_description") or data.get("magic") or "نامشخص"

    detections, engines = [], []
    for engine, details in (data.get("last_analysis_results") or {}).items():
        d = details or {}
        cat = d.get("category") or ""
        name = d.get("engine_name") or engine
        if cat == "malicious":
            detections.append(f"{engine}: {d.get('result')}")
        engines.append({"engine": str(name), "category": cat, "result": d.get("result")})
    # مثل سایت: اول تشخیص‌ها (مخرب/مشکوک)، بعد بقیه به‌ترتیب حروف الفبا
    engines.sort(key=lambda x: (_CAT_ORDER.get(x["category"], 2), x["engine"].lower()))

    return {
        "success": True,
        "found": True,
        "sha256": sha256,
        "is_dangerous": (malicious > 0),
        "malicious": malicious,
        "suspicious": suspicious,
        "clean": undetected,
        "tags": tags,
        "type": file_type,
        "detections": detections[:6],
        "engines": engines,
    }


def _query_vt(sha256: str) -> dict:
    """استعلام نتیجهٔ اسکن از API رسمی ویروس‌توتال با چرخش/فالبک بین کلیدها."""
    if not KEYS:
        return {"success": False, "msg": "کلید ویروس‌توتال تنظیم نشده (LOCALAV_VT_KEYS)."}

    if _CACHE_SEC:
        with _lock:
            hit = _cache.get(sha256)
        if hit and time.time() - hit[0] < _CACHE_SEC:
            return dict(hit[1])

    candidates = _available_keys()
    if not candidates:
        return {"success": False, "msg": "همهٔ کلیدهای ویروس‌توتال موقتاً محدود شده‌اند."}

    url = API_URL.format(sha256=sha256)
    last_msg = "پاسخ نامعتبر"
    for key in candidates:
        try:
            res = requests.get(
                url,
                headers={"x-apikey": key, "Accept": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            # خطای شبکه به کلید ربطی ندارد؛ کلید بعدی کمکی نمی‌کند
            return {"success": False, "msg": f"خطای شبکه: {type(e).__name__}"}

        code = res.status_code
        if code == 200:
            try:
                out = _parse_ok(sha256, res)
            except Exception as e:
                return {"success": False, "msg": f"پاسخ قابل‌خواندن نیست: {type(e).__name__}"}
        elif code == 404:
            out = {
                "success": True,
                "found": False,
                "sha256": sha256,
                "msg": "این فایل کاملاً جدیده و تا حالا روی ویروس‌توتال اسکن نشده.",
            }
        elif code == 429:
            txt = _err_text(res)
            daily = "daily" in txt.lower()
            _mark(key, COOLDOWN_DAILY if daily else COOLDOWN_RATE, f"429 {txt[:80]}")
            last_msg = "سهمیهٔ کلیدها تمام شده (429)."
            continue
        elif code in (401, 403):
            _mark(key, COOLDOWN_BAD_KEY, f"{code} {_err_text(res)[:80]}")
            last_msg = f"کلید نامعتبر/مسدود ({code})."
            continue
        else:
            return {"success": False, "msg": f"کد وضعیت نامعتبر: {code} {_err_text(res)[:80]}".strip()}

        if _CACHE_SEC:
            with _lock:
                if len(_cache) > 512:
                    _cache.clear()
                _cache[sha256] = (time.time(), out)
        return dict(out)

    return {"success": False, "msg": last_msg}


def scan_file_no_key(file_bytes: bytes) -> dict:
    """نسخهٔ بایتی (نام قدیمی حفظ شده): هش فایل را حساب و استعلام می‌کند."""
    return _query_vt(hashlib.sha256(file_bytes).hexdigest())


# ---------------------------------------------------------------------------
# اتصال به ربات (bot.py): هش تکه‌ای از فایل موقت + تبدیل نتیجه به فرمت کنترلر اجرایی
# ---------------------------------------------------------------------------
def scan_fileobj(fp: BinaryIO, name: str = "", size: Optional[int] = None) -> dict:
    """
    فایل باز (seekable) را تکه‌تکه (۶۴KB) هش می‌کند و از ویروس‌توتال می‌پرسد.
    خروجی = نتیجهٔ استعلام + کلیدهای verdict / score / threats / reasons / ms
    verdict ∈ MALICIOUS | SUSPICIOUS | CLEAN | UNKNOWN (هنوز اسکن نشده) | ERROR (کلید/سهمیه/شبکه)
    """
    t0 = time.time()
    if not KEYS:
        res = {"success": False, "msg": "کلید ویروس‌توتال تنظیم نشده (LOCALAV_VT_KEYS)."}
    else:
        h = hashlib.sha256()
        fp.seek(0)
        while True:
            ch = fp.read(CHUNK)
            if not ch:
                break
            h.update(ch)
        res = _query_vt(h.hexdigest())

    res["name"] = name
    res["threats"], res["reasons"], res["score"] = [], [], 0

    if not res.get("success"):
        res["verdict"] = "ERROR"
        res["reasons"] = [res.get("msg", "")]
    elif not res.get("found"):
        res["verdict"] = "UNKNOWN"
        res["reasons"] = [res.get("msg", "")]
    else:
        mal = int(res["malicious"])
        res["threats"] = [d.split(": ", 1)[-1] for d in res["detections"][:3] if d]
        res["reasons"] = list(res["detections"])
        if mal >= _VT_BLOCK_MIN:
            res["verdict"], res["score"] = "MALICIOUS", 90
        elif mal > 0:
            res["verdict"], res["score"] = "SUSPICIOUS", 50
        else:
            res["verdict"] = "CLEAN"
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def to_enforce_finding(res: dict) -> dict:
    """{module, score, threat, reasons} — فرمت _enf_finding_* در bot.py"""
    thr = ", ".join(res.get("threats") or []) or "تهدید ناشناخته"
    return {"module": "virustotal", "score": int(res.get("score", 0)),
            "threat": f"ویروس‌توتال ({res.get('malicious', 0)} موتور): {thr}",
            "reasons": list(res.get("reasons") or [])}


def format_report(result: dict) -> str:
    """متن HTML تلگرام (با html.escape برای مقادیر پویا)."""
    e = lambda x: html.escape(str(x))
    if not result["success"]:
        return f"❌ خطا: {e(result['msg'])}"
    if not result["found"]:
        return f"ℹ️ {e(result['msg'])}\n\nهش فایل:\n<code>{e(result['sha256'])}</code>"

    tags_formatted = " ".join([f"#{e(t)}" for t in result["tags"]]) if result["tags"] else "—"
    verdict = "🔴 مخرب" if result["is_dangerous"] else "🟢 پاک"

    text = (
        f"🛡 <b>نتیجه بررسی امنیتی فایل (ویروس‌توتال)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚖️ <b>وضعیت:</b> {verdict}\n"
        f"📦 <b>نوع:</b> {e(result['type'])}\n"
        f"🏷 <b>برچسب‌ها:</b> {tags_formatted}\n\n"
        f"📊 <b>آمار موتورها:</b>\n"
        f"• آلوده: <b>{result['malicious']}</b>\n"
        f"• مشکوک: <b>{result['suspicious']}</b>\n"
        f"• بدون مشکل: <b>{result['clean']}</b>\n"
    )

    engines = result.get("engines") or []
    if engines:
        # لیست کامل موتورها مثل سایت: «نام موتور — وضعیت» (تشخیص‌ها اول)
        text += "\n🔍 <b>تحلیل موتورها:</b>\n"
        lines = []
        for en in engines:
            cat = en.get("category") or ""
            emoji, label = _CAT_LABEL.get(cat, ("❔", cat or "?"))
            if cat in ("malicious", "suspicious") and en.get("result"):
                lines.append(f"{emoji} {e(en['engine'])} — <code>{e(en['result'])}</code>")
            else:
                lines.append(f"{emoji} {e(en['engine'])} — {label}")
        out, used = [], len(text)
        for i, ln in enumerate(lines):
            if used + len(ln) + 1 > _TG_LIMIT - 40:   # جا برای خط «و N موتور دیگر»
                out.append(f"… و {len(lines) - i} موتور دیگر")
                break
            out.append(ln)
            used += len(ln) + 1
        text += "\n".join(out)
    elif result["detections"]:
        text += "\n☣️ <b>تشخیص‌های مهم:</b>\n" + "\n".join([f"• <code>{e(d)}</code>" for d in result["detections"]])

    return text
