# -*- coding: utf-8 -*-
"""
tg_proxy.py — منبع زنده + اسکنر پروکسی MTProto تلگرام (برای ربات نخود)

کار این ماژول:
  1) لیست پروکسی را از چند منبع که دائم آپدیت می‌شوند می‌گیرد (لیست‌های گیت‌هاب، اختیاری: کانال‌های عمومی تلگرام).
  2) هر پروکسی را «واقعاً» اسکن می‌کند (نه فقط باز بودن پورت):
       سطح ۱ (tcp)  : پورت باز است.
       سطح ۲ (tls)  : پروکسیِ FakeTLS (secret با ee) با HMAC درست جواب هندشیک را داد → secret درست است.
       سطح ۳ (full) : هندشیک obfuscated2 + درخواست req_pq_multi به دیتاسنتر تلگرام فرستاده شد و resPQ برگشت
                       → یعنی پروکسی واقعاً به تلگرام وصل می‌کند. (نیاز به پکیج cryptography)
  3) نتیجه را کش می‌کند (حافظه + فایل JSON)، در پس‌زمینه تازه نگه می‌دارد، و موقع درخواست
     قبل از دادن، چند پروکسیِ برتر را «همین الان» دوباره چک می‌کند.

نکتهٔ صادقانه: اسکن از روی سرورِ ربات انجام می‌شود. پروکسی‌ای که از سرور ربات سالم است ممکن است
داخل ایران توسط ISP بسته باشد؛ پس نتیجه «سالم از دید سرور» است، نه تضمینِ وصل شدن برای همهٔ کاربران.

.env (همه اختیاری):
  PROXY_ENABLED=1            خاموش/روشن
  PROXY_BG_SCAN=1            اسکن پس‌زمینه (0 = فقط موقع درخواست)
  PROXY_REFRESH_MIN=15       هر چند دقیقه یک دور (گرفتن لیست + اسکن)
  PROXY_SOURCES=url1,url2    منبع‌های اضافه (کنار پیش‌فرض‌ها)
  PROXY_SOURCES_ONLY=0       1 = فقط PROXY_SOURCES (پیش‌فرض‌ها نادیده)
  PROXY_TG_CHANNELS=a,b      یوزرنیم کانال‌های عمومی تلگرام که پروکسی می‌گذارند (از t.me/s/ خوانده می‌شود)
  PROXY_SCAN_WORKERS=40      تعداد اسکن همزمان
  PROXY_SCAN_TIMEOUT=8       مهلت هر پروکسی (ثانیه)
  PROXY_MAX_SCAN=500         سقف تعداد کاندیدا در هر دور پس‌زمینه
  PROXY_ONDEMAND_SEC=28      سقف زمان اسکن زنده وقتی کاربر پروکسی می‌خواهد و کش کافی نیست
  PROXY_GIVE_MAX=5           سقف تعداد پروکسی در هر پاسخ
  PROXY_CACHE_FILE=...       مسیر فایل کش (پیش‌فرض: کنار همین فایل)
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import random
import re
import socket
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import parse_qs, urlsplit, unquote

import requests

try:  # فقط برای سطح ۳ (هندشیک MTProto). نبودنش ماژول را از کار نمی‌اندازد.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAS_CRYPTO = True
except Exception:  # pragma: no cover
    HAS_CRYPTO = False

log = logging.getLogger("ChandBot.tgproxy")


# ----------------------------------------------------------------------------- تنظیمات
def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "off", "no")


def _env_num(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        v = float(default)
    return max(lo, min(hi, v))


ENABLED = _env_bool("PROXY_ENABLED", True)
BG_ENABLED = _env_bool("PROXY_BG_SCAN", True)
REFRESH_MIN = int(_env_num("PROXY_REFRESH_MIN", 15, 3, 360))
SCAN_WORKERS = int(_env_num("PROXY_SCAN_WORKERS", 40, 4, 200))
SCAN_TIMEOUT = _env_num("PROXY_SCAN_TIMEOUT", 8, 3, 25)
MAX_SCAN = int(_env_num("PROXY_MAX_SCAN", 500, 20, 5000))
ONDEMAND_SEC = _env_num("PROXY_ONDEMAND_SEC", 28, 5, 90)
GIVE_MAX = int(_env_num("PROXY_GIVE_MAX", 5, 1, 10))
MAX_CANDS = 8000                 # سقف کاندیداهای نگه‌داری‌شده
RECHECK_SEC = 900                # پروکسی سالم هر ۱۵ دقیقه دوباره چک می‌شود
ALIVE_TTL_SEC = 6 * 3600         # سالمِ بدون چک تازه‌تر از این، فقط اگر چیز بهتری نباشد داده می‌شود
FETCH_MIN_GAP_SEC = 10 * 60      # حداقل فاصلهٔ دو بار گرفتن منبع‌ها
CANDIDATE_TTL_SEC = 3 * 86400    # کاندیدایی که ۳ روز در هیچ منبعی دیده نشود حذف می‌شود
ALLOW_PRIVATE = _env_bool("PROXY_ALLOW_PRIVATE", False)   # فقط برای تست محلی؛ در تولید خاموش بماند

DEFAULT_SOURCES = [
    # همه لیست‌های عمومی و خودکارِ گیت‌هاب‌اند؛ منبعی که از دسترس خارج شود بی‌صدا رد می‌شود.
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/MTProtoProxy/main/mtproto.txt",
    "https://raw.githubusercontent.com/Argh94/Proxy-List/main/mtproto.txt",
    "https://raw.githubusercontent.com/shablin/mtproto-proxy/main/data/valid_proxy.txt",
]

# پورت‌های سرویس‌های معروف که نباید به‌خاطر یک لیست خارجی بهشان وصل شویم
_BLOCKED_PORTS = frozenset({22, 23, 25, 53, 110, 135, 139, 143, 445, 465, 587, 993, 995,
                            1433, 3306, 3389, 5432, 6379, 9200, 11211, 27017})

LV_TCP, LV_TLS, LV_FULL = 1, 2, 3
LEVEL_NAME = {LV_TCP: "tcp", LV_TLS: "tls", LV_FULL: "full"}

_UA = "Mozilla/5.0 (compatible; NokhodBot/1.5; +proxy-scan)"


def _sources() -> list[str]:
    extra = [u.strip() for u in re.split(r"[,\s]+", os.environ.get("PROXY_SOURCES", "") or "") if u.strip()]
    base = [] if _env_bool("PROXY_SOURCES_ONLY", False) else list(DEFAULT_SOURCES)
    out = []
    for u in base + extra:
        if u.lower().startswith(("http://", "https://")) and u not in out:
            out.append(u)
    for ch in re.split(r"[,\s]+", os.environ.get("PROXY_TG_CHANNELS", "") or ""):
        ch = ch.strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", ch):
            u = f"https://t.me/s/{ch}"
            if u not in out:
                out.append(u)
    return out


# ----------------------------------------------------------------------------- پارس
_LINK_RE = re.compile(r"(?:tg://proxy|https?://(?:t|telegram)\.me/proxy)\?[^\s\"'<>)\]}]+", re.I)
_HOSTPORT_RE = re.compile(
    r"(?<![\w.\-])((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)+)"
    r":(\d{2,5})[:|,\s]+(?:mtproto[:|,\s]+)?([0-9a-fA-F]{32,})(?![0-9a-zA-Z])", re.I)
_HOST_OK_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*$")


def _norm_secret(s: str) -> str | None:
    s = unquote((s or "").strip()).replace(" ", "+").strip("'\"")
    if not s:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{32,}", s) and len(s) % 2 == 0:
        return s.lower()
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        return raw.hex() if len(raw) >= 16 else None
    except (binascii.Error, ValueError):
        return None


def secret_info(secret_hex: str):
    """secret → (mode, key16, domain). mode: 'tls' (ee) | 'dd' | 'plain'. نامعتبر → None."""
    try:
        b = bytes.fromhex(secret_hex)
    except ValueError:
        return None
    if len(b) >= 18 and b[0] == 0xEE:
        dom = b[17:].decode("ascii", "ignore").strip().lower()
        if not _HOST_OK_RE.match(dom):
            return None
        return "tls", b[1:17], dom
    if len(b) == 17 and b[0] == 0xDD:
        return "dd", b[1:17], ""
    if len(b) == 16:
        return "plain", b, ""
    return None


def _valid_host(h: str) -> str | None:
    h = (h or "").strip().strip("[]").rstrip(".").lower()
    if not h or h == "localhost":
        return None
    try:
        ip = ipaddress.ip_address(h)
        return h if (ALLOW_PRIVATE or ip.is_global) else None
    except ValueError:
        pass
    return h if _HOST_OK_RE.match(h) and "." in h else None


def _key(server: str, port: int, secret: str) -> str:
    return f"{server}:{port}:{secret}"


def _add(out: dict, server, port, secret) -> None:
    host = _valid_host(str(server or ""))
    try:
        p = int(str(port).strip())
    except (TypeError, ValueError):
        return
    sec = _norm_secret(str(secret or ""))
    if not host or not (1 <= p <= 65535) or p in _BLOCKED_PORTS or not sec or secret_info(sec) is None:
        return
    out[_key(host, p, sec)] = {"server": host, "port": p, "secret": sec}


def _walk_json(node, out: dict, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, dict):
        host = node.get("host") or node.get("server") or node.get("ip") or node.get("address")
        if host and node.get("port") and node.get("secret"):
            _add(out, host, node.get("port"), node.get("secret"))
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk_json(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:20000]:
            _walk_json(v, out, depth + 1)


def parse_text(text: str) -> list[dict]:
    """هر متنی (txt/JSON/HTML صفحهٔ کانال) → لیست {server, port, secret(hex)}."""
    if not text:
        return []
    t = html.unescape(text)
    out: dict = {}
    s = t.lstrip()
    if s[:1] in "[{":
        try:
            _walk_json(json.loads(s), out)
        except Exception:
            pass
    for m in _LINK_RE.finditer(t):
        link = m.group(0).rstrip(".,;")
        q = parse_qs(urlsplit(link).query)
        _add(out, (q.get("server") or [""])[0], (q.get("port") or [""])[0], (q.get("secret") or [""])[0])
    for m in _HOSTPORT_RE.finditer(t):
        _add(out, m.group(1), m.group(2), m.group(3))
    return list(out.values())


def tme_link(item: dict) -> str:
    return f"https://t.me/proxy?server={item['server']}&port={item['port']}&secret={item['secret']}"


# ----------------------------------------------------------------------------- اسکن یک پروکسی
def _resolve_public(host: str, port: int):
    """اولین IP عمومی میزبان (IPv4 ترجیحاً). IP خصوصی/لوکال = رد (SSRF)."""
    try:
        ip = ipaddress.ip_address(host)
        if ALLOW_PRIVATE or ip.is_global:
            return (socket.AF_INET6 if ip.version == 6 else socket.AF_INET), str(ip)
        return None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    for fam, _t, _p, _c, sa in infos:
        try:
            ip = ipaddress.ip_address(sa[0])
        except ValueError:
            continue
        if ALLOW_PRIVATE or ip.is_global:
            return fam, sa[0]
    return None


def _recv_exact(sock, n: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise socket.timeout("deadline")
        sock.settimeout(rem)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


def _read_tls_record(sock, deadline: float, idle: float | None = None):
    """یک رکورد TLS. اگر idle داده شود و تا آن مهلت هیچ بایتی نیامد → None (پایان جریان)."""
    first_deadline = deadline if idle is None else min(deadline, time.monotonic() + idle)
    try:
        b0 = _recv_exact(sock, 1, first_deadline)
    except socket.timeout:
        if idle is not None and time.monotonic() < deadline:
            return None
        raise
    hdr = b0 + _recv_exact(sock, 4, deadline)
    ln = struct.unpack(">H", hdr[3:5])[0]
    if ln > 16384 + 2048:
        raise ValueError("bad tls length")
    body = _recv_exact(sock, ln, deadline) if ln else b""
    return hdr[0], hdr + body, body


def _tls_client_hello(key16: bytes, domain: str):
    """ClientHello شبیه کروم + digest مخصوص MTProxy (HMAC روی کل رکورد، ۴ بایت آخر xor با timestamp)."""
    host = domain.encode()

    def ext(t: int, data: bytes) -> bytes:
        return struct.pack(">HH", t, len(data)) + data

    alpn = b"\x02h2" + b"\x08http/1.1"
    exts_core = [
        ext(0x0000, struct.pack(">H", len(host) + 3) + b"\x00" + struct.pack(">H", len(host)) + host),
        ext(0x0017, b""),
        ext(0xFF01, b"\x00"),
        ext(0x000A, struct.pack(">H", 6) + struct.pack(">HHH", 0x001D, 0x0017, 0x0018)),
        ext(0x000B, b"\x01\x00"),
        ext(0x0023, b""),
        ext(0x0010, struct.pack(">H", len(alpn)) + alpn),
        ext(0x0005, b"\x01\x00\x00\x00\x00"),
        ext(0x000D, struct.pack(">H", 16) + struct.pack(">8H", 0x0403, 0x0804, 0x0401, 0x0503, 0x0805, 0x0501, 0x0806, 0x0601)),
        ext(0x0012, b""),
        ext(0x0033, struct.pack(">H", 36) + struct.pack(">HH", 0x001D, 32) + os.urandom(32)),
        ext(0x002D, b"\x01\x01"),
        ext(0x002B, b"\x04\x03\x04\x03\x03"),
    ]
    suites = struct.pack(">15H", 0x1301, 0x1302, 0x1303, 0xC02B, 0xC02F, 0xC02C, 0xC030, 0xCCA9, 0xCCA8,
                         0xC013, 0xC014, 0x009C, 0x009D, 0x002F, 0x0035)

    def build(exts: bytes) -> bytes:
        body = (b"\x03\x03" + b"\x00" * 32 + b"\x20" + os.urandom(32) + struct.pack(">H", len(suites)) + suites
                + b"\x01\x00" + struct.pack(">H", len(exts)) + exts)
        hs = b"\x01" + len(body).to_bytes(3, "big") + body
        return b"\x16\x03\x01" + struct.pack(">H", len(hs)) + hs

    exts = b"".join(exts_core)
    rec = build(exts)
    pad = 517 - len(rec) - 4                       # کروم ClientHello را تا ۵۱۷ بایت پد می‌کند
    if pad > 0:
        rec = build(exts + ext(0x0015, b"\x00" * pad))
    d = hmac.new(key16, rec, hashlib.sha256).digest()
    ts = struct.pack("<I", int(time.time()) & 0xFFFFFFFF)
    client_random = d[:28] + bytes(a ^ b for a, b in zip(d[28:], ts))
    return rec[:11] + client_random + rec[43:], client_random


def _verify_server_flight(key16: bytes, client_random: bytes, raw_records: list[bytes]) -> bool:
    """HMAC(secret, client_random + serverFlight با ۳۲ بایت صفر در جای random) == random سرور."""
    for n in range(len(raw_records), 0, -1):
        blob = b"".join(raw_records[:n])
        if len(blob) < 43:
            continue
        calc = hmac.new(key16, client_random + blob[:11] + b"\x00" * 32 + blob[43:], hashlib.sha256).digest()
        if hmac.compare_digest(calc, blob[11:43]):
            return True
    return False


class _Obf2:
    """obfuscated2 سمت کلاینت: init 64 بایتی + دو جریان AES-256-CTR."""

    def __init__(self, key16: bytes, tag: bytes, dc: int = 2):
        while True:
            init = bytearray(os.urandom(64))
            if init[0] == 0xEF:
                continue
            if bytes(init[:4]) in (b"HEAD", b"POST", b"GET ", b"OPTI", b"\xee\xee\xee\xee", b"\xdd\xdd\xdd\xdd", b"\x16\x03\x01\x02"):
                continue
            if init[4:8] == b"\x00\x00\x00\x00":
                continue
            break
        init[56:60] = tag
        init[60:62] = struct.pack("<h", dc)
        kiv = bytes(init[8:56])
        enc_key = hashlib.sha256(kiv[:32] + key16).digest()
        rev = kiv[::-1]
        dec_key = hashlib.sha256(rev[:32] + key16).digest()
        be = default_backend()
        self._enc = Cipher(algorithms.AES(enc_key), modes.CTR(kiv[32:48]), backend=be).encryptor()
        self._dec = Cipher(algorithms.AES(dec_key), modes.CTR(rev[32:48]), backend=be).encryptor()
        enc_init = self._enc.update(bytes(init))
        self.handshake = bytes(init[:56]) + enc_init[56:64]

    def enc(self, data: bytes) -> bytes:
        return self._enc.update(data)

    def dec(self, data: bytes) -> bytes:
        return self._dec.update(data)


def _req_pq_frame(padded: bool):
    """بستهٔ req_pq_multi بدون رمز (auth_key_id=0) در ترنسپورت intermediate / padded-intermediate."""
    nonce = os.urandom(16)
    body = struct.pack("<I", 0xBE7E8EF1) + nonce
    msg_id = (int(time.time()) << 32) | (random.getrandbits(20) << 2)
    payload = b"\x00" * 8 + struct.pack("<Q", msg_id) + struct.pack("<I", len(body)) + body
    if padded:
        payload += os.urandom(random.randint(0, 3))
    return struct.pack("<I", len(payload)) + payload, nonce


_RESPQ = b"\x63\x24\x16\x05"


def _mtproto_handshake(sock, obf: "_Obf2", tls: bool, padded: bool, deadline: float) -> bool:
    frame, nonce = _req_pq_frame(padded)
    out = obf.handshake + obf.enc(frame)
    if tls:
        sock.sendall(b"\x14\x03\x03\x00\x01\x01" + b"\x17\x03\x03" + struct.pack(">H", len(out)) + out)
    else:
        sock.sendall(out)
    got = bytearray()
    while len(got) < 44:
        if tls:
            rec = _read_tls_record(sock, deadline)
            if rec[0] == 0x14:
                continue
            if rec[0] != 0x17:
                return False
            got += obf.dec(rec[2])
        else:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = sock.recv(4096)
            if not chunk:
                return False
            got += obf.dec(chunk)
    return (_RESPQ + nonce) in bytes(got[:96])


def check_proxy(server: str, port: int, secret_hex: str, timeout: float = SCAN_TIMEOUT, deep: bool = True):
    """→ (level, ms) یا None (مرده/نامعتبر)."""
    info = secret_info(secret_hex)
    if info is None or port in _BLOCKED_PORTS:
        return None
    mode, key16, domain = info
    r = _resolve_public(server, port)
    if r is None:
        return None
    fam, ip = r
    t_all = time.monotonic()
    deadline = t_all + timeout
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(min(5.0, timeout))
        t0 = time.monotonic()
        s.connect((ip, port))
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None
    tcp_ms = int((time.monotonic() - t0) * 1000)
    level, ms = LV_TCP, tcp_ms
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if mode == "tls":
            hello, client_random = _tls_client_hello(key16, domain)
            t1 = time.monotonic()
            s.sendall(hello)
            first = _read_tls_record(s, deadline)
            if first[0] != 0x16:
                return level, ms                      # TLS نیست → فقط پورت باز
            recs = [first[1]]
            while len(recs) < 8:
                nxt = _read_tls_record(s, deadline, idle=0.35)
                if nxt is None:
                    break
                recs.append(nxt[1])
            if not _verify_server_flight(key16, client_random, recs):
                return level, ms                      # شبیه TLS ولی secret تأیید نشد (احتمالاً دامنهٔ فرانت)
            level, ms = LV_TLS, int((time.monotonic() - t1) * 1000)
            if deep and HAS_CRYPTO:
                t2 = time.monotonic()
                if _mtproto_handshake(s, _Obf2(key16, b"\xdd\xdd\xdd\xdd"), True, True, deadline):
                    level, ms = LV_FULL, int((time.monotonic() - t2) * 1000)
        elif deep and HAS_CRYPTO:
            padded = mode == "dd"
            tag = b"\xdd\xdd\xdd\xdd" if padded else b"\xee\xee\xee\xee"
            t2 = time.monotonic()
            if _mtproto_handshake(s, _Obf2(key16, tag), False, padded, deadline):
                level, ms = LV_FULL, int((time.monotonic() - t2) * 1000)
    except (OSError, ConnectionError, ValueError, socket.timeout):
        pass                                          # هر خطای بعد از TCP = همان سطحی که تا اینجا گرفتیم
    except Exception as e:  # pragma: no cover
        log.debug(f"[PROXY] probe error {type(e).__name__}: {e}")
    finally:
        try:
            s.close()
        except Exception:
            pass
    return level, ms


# ----------------------------------------------------------------------------- دریافت منبع‌ها
def _http_text(url: str, timeout: float = 14.0, max_bytes: int = 6_000_000) -> str:
    with requests.get(url, headers={"User-Agent": _UA, "Accept": "*/*"}, timeout=timeout, stream=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > max_bytes:
                break
        return bytes(buf).decode("utf-8", "ignore")


# ----------------------------------------------------------------------------- استخر پروکسی
class ProxyPool:
    def __init__(self, path: str | None = None):
        self._lock = threading.RLock()
        self._bg_lock = threading.Lock()          # فقط یک دور پس‌زمینه
        self._fetch_lock = threading.Lock()
        self._fg_sem = threading.BoundedSemaphore(2)   # حداکثر ۲ اسکن زندهٔ همزمان (درخواست کاربر)
        self.cands: dict[str, dict] = {}
        self.alive: dict[str, dict] = {}
        self.last_fetch = 0.0
        self.last_cycle = 0.0
        self.src_stats: dict[str, str] = {}
        self._given: dict[str, OrderedDict] = {}
        self._last_save = 0.0
        self.path = path or os.environ.get("PROXY_CACHE_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "proxy_cache.json")
        self._load()

    # ---------- ذخیره/بارگذاری
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.cands = {k: v for k, v in (d.get("cands") or {}).items() if isinstance(v, dict) and "server" in v}
            self.alive = {k: v for k, v in (d.get("alive") or {}).items() if k in self.cands}
            self.last_fetch = float(d.get("last_fetch") or 0)
            self.last_cycle = float(d.get("last_cycle") or 0)
            log.info(f"[PROXY] کش بارگذاری شد: {len(self.cands)} کاندیدا، {len(self.alive)} سالم")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[PROXY] کش خوانده نشد: {type(e).__name__}: {e}")

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_save < 20:
            return
        self._last_save = now
        with self._lock:
            data = {"cands": self.cands, "alive": self.alive, "last_fetch": self.last_fetch, "last_cycle": self.last_cycle}
            blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning(f"[PROXY] ذخیرهٔ کش ناموفق: {type(e).__name__}: {e}")

    # ---------- منبع‌ها
    def fetch_sources(self, force: bool = False) -> int:
        """همهٔ منبع‌ها را موازی می‌گیرد و کاندیداهای جدید را اضافه می‌کند. خروجی: تعداد جدید."""
        if not force and time.time() - self.last_fetch < FETCH_MIN_GAP_SEC and self.cands:
            return 0
        if not self._fetch_lock.acquire(timeout=20):
            return 0
        try:
            srcs = _sources()
            results: dict[str, list[dict]] = {}
            stats: dict[str, str] = {}

            def one(u: str):
                try:
                    items = parse_text(_http_text(u))
                    results[u] = items
                    stats[u] = f"ok {len(items)}"
                except Exception as e:
                    stats[u] = f"err {type(e).__name__}: {str(e)[:60]}"

            with ThreadPoolExecutor(max_workers=min(6, max(1, len(srcs)))) as ex:
                list(ex.map(one, srcs))
            now = time.time()
            new = 0
            with self._lock:
                self.src_stats = stats
                for u, items in results.items():
                    for it in items:
                        k = _key(it["server"], it["port"], it["secret"])
                        c = self.cands.get(k)
                        if c is None:
                            self.cands[k] = {**it, "src": u.split("/")[2] if "//" in u else u, "seen": now,
                                             "last": now, "checks": 0, "fails": 0, "last_check": 0}
                            new += 1
                        else:
                            c["last"] = now
                if results:                       # فقط اگر حداقل یک منبع جواب داد، زمان ثبت می‌شود
                    self.last_fetch = now
                self._prune(now)
            ok_n = sum(1 for v in stats.values() if v.startswith("ok"))
            log.info(f"[PROXY] منبع‌ها: {ok_n}/{len(srcs)} پاسخ دادند، {new} کاندیدای جدید، کل {len(self.cands)}")
            return new
        finally:
            self._fetch_lock.release()

    def _prune(self, now: float) -> None:
        for k in [k for k, c in self.cands.items() if now - c.get("last", now) > CANDIDATE_TTL_SEC and k not in self.alive]:
            self.cands.pop(k, None)
        if len(self.cands) > MAX_CANDS:
            order = sorted(self.cands, key=lambda k: (k in self.alive, -self.cands[k].get("fails", 0), self.cands[k].get("last", 0)))
            for k in order[: len(self.cands) - MAX_CANDS]:
                self.cands.pop(k, None)
                self.alive.pop(k, None)
        for k in [k for k in self.alive if k not in self.cands]:
            self.alive.pop(k, None)

    # ---------- اسکن
    def _check_key(self, key: str, timeout: float | None = None) -> int:
        with self._lock:
            c = self.cands.get(key)
            if not c:
                return 0
            server, port, secret = c["server"], c["port"], c["secret"]
        res = check_proxy(server, port, secret, timeout or SCAN_TIMEOUT)
        now = time.time()
        with self._lock:
            c = self.cands.get(key)
            if c is not None:
                c["checks"] = c.get("checks", 0) + 1
                c["last_check"] = now
                if res:
                    c["fails"] = 0
                else:
                    c["fails"] = c.get("fails", 0) + 1
            if res:
                self.alive[key] = {"level": res[0], "ms": res[1], "ts": now}
                return res[0]
            self.alive.pop(key, None)
            return 0

    def _scan_keys(self, keys: list[str], budget: float, workers: int, stop_after: int | None = None,
                   timeout: float | None = None, stop_level: int = LV_TCP) -> tuple[int, int]:
        """keys را با پنجرهٔ لغزان اسکن می‌کند. خروجی: (چک‌شده، تعدادِ سالمِ هم‌سطح یا بالاتر از stop_level).
        با رسیدن به stop_after یا پایان بودجه می‌ایستد."""
        if not keys:
            return 0, 0
        t_end = time.monotonic() + budget
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="px-scan")
        it = iter(keys)
        pending: set = set()
        checked = found = 0
        try:
            exhausted = False
            while True:
                while not exhausted and len(pending) < workers * 2 and time.monotonic() < t_end:
                    k = next(it, None)
                    if k is None:
                        exhausted = True
                        break
                    pending.add(ex.submit(self._check_key, k, timeout))
                if not pending:
                    break
                done, pending = wait(pending, timeout=0.4, return_when=FIRST_COMPLETED)
                for f in done:
                    checked += 1
                    try:
                        if f.result() >= stop_level:
                            found += 1
                    except Exception:
                        pass
                if stop_after is not None and found >= stop_after:
                    break
                if time.monotonic() >= t_end + (timeout or SCAN_TIMEOUT) + 1:
                    break
                if time.monotonic() >= t_end and not pending:
                    break
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        return checked, found

    def _plan(self, max_items: int) -> list[str]:
        now = time.time()
        with self._lock:
            recheck = sorted((k for k, a in self.alive.items() if now - a.get("ts", 0) > RECHECK_SEC),
                             key=lambda k: self.alive[k].get("ts", 0))
            fresh = [k for k, c in self.cands.items() if c.get("checks", 0) == 0 and k not in self.alive]
            retry = [k for k, c in self.cands.items()
                     if c.get("checks", 0) > 0 and k not in self.alive
                     and now - c.get("last_check", 0) > min(6 * 3600, 1200 * (2 ** min(c.get("fails", 0), 5)))]
        random.shuffle(fresh)
        random.shuffle(retry)
        return (recheck[:150] + fresh + retry)[:max_items]

    def background_cycle(self) -> None:
        """کار زمان‌بند: منبع‌ها را بگیر، کاندیداها را اسکن کن، کش را ذخیره کن."""
        if not ENABLED or not self._bg_lock.acquire(blocking=False):
            return
        t0 = time.time()
        try:
            self.fetch_sources(force=True)
            plan = self._plan(MAX_SCAN)
            checked, found = self._scan_keys(plan, budget=max(60.0, REFRESH_MIN * 60 * 0.6), workers=SCAN_WORKERS)
            with self._lock:
                self.last_cycle = time.time()
                by = {LV_TCP: 0, LV_TLS: 0, LV_FULL: 0}
                for a in self.alive.values():
                    by[a["level"]] = by.get(a["level"], 0) + 1
            self.save(force=True)
            log.info(f"[PROXY] دور پس‌زمینه: {checked} اسکن، {found} سالم | استخر: full={by[LV_FULL]} tls={by[LV_TLS]} "
                     f"tcp={by[LV_TCP]} | {time.time() - t0:.0f}s")
        except Exception as e:
            log.warning(f"[PROXY] دور پس‌زمینه خطا داد: {type(e).__name__}: {e}")
        finally:
            self._bg_lock.release()

    # ---------- درخواست کاربر
    def _given_set(self, chat_key: str) -> OrderedDict:
        g = self._given.get(chat_key)
        if g is None:
            if len(self._given) > 600:
                for k in list(self._given)[:100]:
                    self._given.pop(k, None)
            g = self._given[chat_key] = OrderedDict()
        now = time.time()
        for k in [k for k, ts in g.items() if now - ts > 3 * 3600]:
            g.pop(k, None)
        return g

    def _item(self, key: str) -> dict:
        c, a = self.cands[key], self.alive[key]
        return {"server": c["server"], "port": c["port"], "secret": c["secret"], "level": a["level"], "ms": a["ms"],
                "mode": (secret_info(c["secret"]) or ("?",))[0], "key": key}

    def get_working(self, count: int = 3, chat_key: str = "", budget: float | None = None) -> dict:
        """اول اسکن/تأیید، بعد دادن. خروجی: {ok, items, scanned, pool, age, err}."""
        count = max(1, min(int(count or 3), GIVE_MAX))
        budget = ONDEMAND_SEC if budget is None else budget
        res = {"ok": False, "items": [], "scanned": 0, "pool": 0, "age": None, "err": None}
        if not ENABLED:
            res["err"] = "disabled"
            return res
        t_end = time.monotonic() + budget
        want_lv = LV_FULL if HAS_CRYPTO else LV_TLS      # بالاترین سطحی که این سرور می‌تواند تأیید کند
        try:
            if not self.cands or time.time() - self.last_fetch > 30 * 60:
                self.fetch_sources()
            if not self.cands:
                res["err"] = "no_sources"
                return res
            now = time.time()
            with self._lock:
                given = self._given_set(chat_key)
                ranked = sorted((k for k, a in self.alive.items() if k in self.cands and now - a["ts"] <= ALIVE_TTL_SEC),
                                key=lambda k: (-self.alive[k]["level"], self.alive[k]["ms"]))
                fresh_first = [k for k in ranked if k not in given] or ranked
            # بین بهترین‌ها بچرخان تا همه همان چند تای اول را نگیرند
            picks = fresh_first[: max(count * 3, 8)]
            random.shuffle(picks)
            picks = picks[: max(count * 2, 6)]
            good: list[str] = []
            if picks:
                _c, _f = self._scan_keys(picks, budget=min(10.0, max(3.0, t_end - time.monotonic())),
                                         workers=min(16, len(picks)), stop_after=count, timeout=min(6.0, SCAN_TIMEOUT),
                                         stop_level=want_lv)
                res["scanned"] += _c
                with self._lock:
                    good = [k for k in picks if k in self.alive and time.time() - self.alive[k]["ts"] < 30]
            n_top = sum(1 for k in good if self.alive[k]["level"] >= want_lv)
            if n_top < count and time.monotonic() < t_end - 3:
                if self._fg_sem.acquire(timeout=max(0.1, min(5.0, t_end - time.monotonic()))):
                    try:
                        need = count - n_top
                        with self._lock:
                            tried = set(picks)
                            fresh = [k for k, c in self.cands.items()
                                     if c.get("checks", 0) == 0 and k not in self.alive and k not in tried]
                        random.shuffle(fresh)
                        _c, _f = self._scan_keys(fresh[:400], budget=max(3.0, t_end - time.monotonic()),
                                                 workers=min(SCAN_WORKERS, 40), stop_after=need, stop_level=want_lv)
                        res["scanned"] += _c
                        with self._lock:
                            for k, a in self.alive.items():
                                if k not in good and time.time() - a["ts"] < max(60, budget + 5) and k in self.cands and k not in tried:
                                    good.append(k)
                    finally:
                        self._fg_sem.release()
            with self._lock:
                good = [k for k in dict.fromkeys(good) if k in self.alive]
                good.sort(key=lambda k: (-self.alive[k]["level"], self.alive[k]["ms"]))
                chosen = good[:count]
                items = [self._item(k) for k in chosen]
                for k in chosen:
                    given[k] = time.time()
                while len(given) > 60:
                    given.popitem(last=False)
                res["pool"] = len(self.alive)
            res["items"] = items
            res["ok"] = bool(items)
            res["age"] = int(time.time() - self.last_cycle) if self.last_cycle else None
            if not items:
                res["err"] = "none_alive"
            self.save()
        except Exception as e:
            log.warning(f"[PROXY] get_working خطا: {type(e).__name__}: {e}", exc_info=True)
            res["err"] = "error"
        return res

    # ---------- وضعیت (برای ادمین)
    def stats(self) -> dict:
        with self._lock:
            by = {"full": 0, "tls": 0, "tcp": 0}
            for a in self.alive.values():
                by[LEVEL_NAME.get(a["level"], "tcp")] += 1
            return {"cands": len(self.cands), "alive": len(self.alive), "by_level": by,
                    "last_fetch": self.last_fetch, "last_cycle": self.last_cycle,
                    "sources": dict(self.src_stats), "crypto": HAS_CRYPTO}


_POOL: ProxyPool | None = None
_POOL_LOCK = threading.Lock()


def pool() -> ProxyPool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ProxyPool()
    return _POOL


def get_working(count: int = 3, chat_key: str = "", budget: float | None = None) -> dict:
    return pool().get_working(count, chat_key, budget)


def background_cycle() -> None:
    pool().background_cycle()


def stats() -> dict:
    return pool().stats()
