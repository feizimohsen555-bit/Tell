#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_av.py — اسکنر ویروس‌توتال بدون API Key (جایگزین آنتی‌ویروس لوکال قبلی)
==========================================================================
فقط یک درخواست: GET https://www.virustotal.com/ui/files/{sha256}
فایل هرگز آپلود نمی‌شود؛ فقط هش SHA-256 آن (به‌صورت تکه‌ای، بدون لود کامل در RAM) استعلام می‌شود.

استفاده در ربات:
    import local_av
    res = local_av.scan_fileobj(tf, "update.apk")      # tf = فایل موقت باز (seekable)
    if res["verdict"] in ("MALICIOUS", "SUSPICIOUS"): ...

.env (اختیاری):
    LOCALAV_VT_MIN=5      حداقل تعداد موتور «مخرب» برای وضعیت MALICIOUS (حذف+میوت)؛ کمتر از آن فقط هشدار
"""

from __future__ import annotations

import hashlib
import html
import os
import random
import time
from typing import BinaryIO, Optional

import requests

__version__ = "2.0-vt-nokey"

# لیست پروکسی برای چرخش (اختیاری - اگر نداری خالی بذار تا مستقیم بره)
PROXIES = [
    # "http://user:pass@ip:port",
    # "socks5://127.0.0.1:1080",
]

# لیست هدرهای شبیه مرورگر واقعی برای رد شدن از فیلتر
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15"
]

try:
    _VT_BLOCK_MIN = max(1, int((os.environ.get("LOCALAV_VT_MIN") or "5").strip()))
except ValueError:
    _VT_BLOCK_MIN = 5

CHUNK = 64 * 1024


def _query_vt(sha256: str) -> dict:
    """استعلام نتیجه اسکن و برچسب‌ها از ویروس‌توتال بدون API Key (روی هش آماده)"""

    # اندپوینت استخراج‌شده از ترافیک پروکسی‌من
    url = f"https://www.virustotal.com/ui/files/{sha256}"

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.virustotal.com/",
        "X-Tool": "vt-ui-main"
    }

    proxy_dict = None
    if PROXIES:
        p = random.choice(PROXIES)
        proxy_dict = {"http": p, "https": p}

    try:
        res = requests.get(url, headers=headers, proxies=proxy_dict, timeout=12)

        # اگر فایل در پایگاه داده ویروس‌توتال ثبت شده باشد
        if res.status_code == 200:
            data = res.json().get("data", {}).get("attributes", {})
            stats = data.get("last_analysis_stats", {})

            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            undetected = stats.get("undetected", 0)
            tags = data.get("tags", [])
            file_type = data.get("type_description") or data.get("magic") or "نامشخص"

            # استخراج نام آنتی‌ویروس‌هایی که فایل را مخرب شناسایی کردند
            detections = []
            results = data.get("last_analysis_results", {})
            for engine, details in results.items():
                if details.get("category") == "malicious":
                    detections.append(f"{engine}: {details.get('result')}")

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
                "detections": detections[:6]
            }

        elif res.status_code == 404:
            # فایل جدیده و تا حالا کسی نفرستاده ویروس‌توتال
            return {
                "success": True,
                "found": False,
                "sha256": sha256,
                "msg": "این فایل کاملاً جدیده و تا حالا روی ویروس‌توتال اسکن نشده."
            }

        elif res.status_code in (403, 429):
            return {
                "success": False,
                "msg": "کلودفلر موقتاً جلوی IP رو گرفته. باید پروکسی عوض بشه."
            }

    except Exception as e:
        return {"success": False, "msg": str(e)}

    return {"success": False, "msg": f"کد وضعیت نامعتبر: {res.status_code}"}


def scan_file_no_key(file_bytes: bytes) -> dict:
    """نسخهٔ بایتی (دقیقاً همان تابع اصلی): هش فایل را حساب و استعلام می‌کند."""
    # ۱. محاسبه هش فایل
    sha256 = hashlib.sha256(file_bytes).hexdigest()
    return _query_vt(sha256)


# ---------------------------------------------------------------------------
# اتصال به ربات (bot.py): هش تکه‌ای از فایل موقت + تبدیل نتیجه به فرمت کنترلر اجرایی
# ---------------------------------------------------------------------------
def scan_fileobj(fp: BinaryIO, name: str = "", size: Optional[int] = None) -> dict:
    """
    فایل باز (seekable) را تکه‌تکه (۶۴KB) هش می‌کند و از ویروس‌توتال بدون کلید می‌پرسد.
    خروجی = نتیجهٔ scan_file_no_key + کلیدهای verdict / score / threats / reasons / ms
    verdict ∈ MALICIOUS | SUSPICIOUS | CLEAN | UNKNOWN (هنوز اسکن نشده) | ERROR (بلاک/خطای شبکه)
    """
    t0 = time.time()
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
    """متن HTML تلگرام — همان قالب‌بندی متن راهنما (با html.escape برای مقادیر پویا)."""
    e = lambda x: html.escape(str(x))
    if not result["success"]:
        return f"❌ خطا: {e(result['msg'])}"
    if not result["found"]:
        return f"ℹ️ {e(result['msg'])}\n\nهش فایل:\n<code>{e(result['sha256'])}</code>"

    tags_formatted = " ".join([f"#{e(t)}" for t in result["tags"]]) if result["tags"] else "—"
    verdict = "🔴 مخرب" if result["is_dangerous"] else "🟢 پاک"

    text = (
        f"🛡 <b>نتیجه بررسی امنیتی فایل (بدون کلید)</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚖️ <b>وضعیت:</b> {verdict}\n"
        f"📦 <b>نوع:</b> {e(result['type'])}\n"
        f"🏷 <b>برچسب‌ها:</b> {tags_formatted}\n\n"
        f"📊 <b>آمار موتورها:</b>\n"
        f"• آلوده: <b>{result['malicious']}</b>\n"
        f"• مشکوک: <b>{result['suspicious']}</b>\n"
        f"• بدون مشکل: <b>{result['clean']}</b>\n"
    )

    if result["detections"]:
        text += "\n☣️ <b>تشخیص‌های مهم:</b>\n" + "\n".join([f"• <code>{e(d)}</code>" for d in result["detections"]])

    return text
