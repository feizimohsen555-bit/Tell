const http = require('http');
const https = require('https');
const crypto = require('crypto');
const { URL } = require('url');

// کلید رمزنگاری مشترک بین سرور و ربات
const SECRET_KEY = process.env.SECRET_KEY || "change_this_secret_key_in_render_env";

// سرورهای بالادستی DNS
const UPSTREAM_HOST = 'cloudflare-dns.com';
const UPSTREAM_PATH = '/dns-query';

// لیست دامنه‌های تبلیغاتی و ردیاب‌ها
const BLOCKED_DOMAINS = new Set([
  'ads.soundcloud.com', 'ad.soundcloud.com', 'promoted.soundcloud.com',
  'adswizz.com', 'delivery-cdn-cf.adswizz.com', 'media.adswizz.com',
  'scorecardresearch.com', 'app-measurement.com', 'cdn.branch.io',
  'ads.pinterest.com', 'ct.pinterest.com', 'trk.pinterest.com',
  'googlesyndication.com', 'googleadservices.com', 'adclick.g.doubleclick.net',
  'unityads.unity3d.com', 'applovin.com', 'applvn.com', 'ironsrc.com',
  'vungle.com', 'chartboost.com', 'mintegral.com', 'tapsell.ir', 'yektanet.com'
]);

// اعتبارسنجی توکن امضا و تاریخ انقضا
function verifyAccess(urlObj) {
  const uid = urlObj.searchParams.get('uid');
  const exp = urlObj.searchParams.get('exp');
  const sign = urlObj.searchParams.get('sign');

  if (!uid || !exp || !sign) return false;

  // بررسی انقضا
  const now = Math.floor(Date.now() / 1000);
  if (now > parseInt(exp, 10)) return false;

  // بررسی امضای دیجیتال
  const expectedSign = crypto
    .createHmac('sha256', SECRET_KEY)
    .update(`${uid}:${exp}`)
    .digest('hex');

  return sign === expectedSign;
}

// ساخت پاسخ پوچ (0.0.0.0) برای دامنه‌های مسدودشده
function createBlockResponse(queryBuffer) {
  try {
    let offset = 12;
    while (offset < queryBuffer.length) {
      const len = queryBuffer[offset++];
      if (len === 0) break;
      if ((len & 0xc0) === 0xc0) break;
      offset += len;
    }
    offset += 4;
    const question = queryBuffer.subarray(0, offset);
    const answer = Buffer.from([0xc0, 0x0c, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x05, 0x00, 0x04, 0, 0, 0, 0]);
    const resp = Buffer.concat([question, answer]);
    resp.writeUInt16BE(0x8180, 2);
    resp.writeUInt16BE(1, 4);
    resp.writeUInt16BE(1, 6);
    return resp;
  } catch (e) {
    return queryBuffer;
  }
}

// استخراج نام دامنه از پکت باینری DNS
function extractDomain(buffer) {
  try {
    let offset = 12;
    const labels = [];
    while (offset < buffer.length) {
      const len = buffer[offset++];
      if (len === 0) break;
      if ((len & 0xc0) === 0xc0) break;
      labels.push(buffer.toString('utf8', offset, offset + len));
      offset += len;
    }
    return labels.join('.');
  } catch (e) {
    return null;
  }
}

function isBlocked(domain) {
  if (!domain) return false;
  domain = domain.toLowerCase();
  const parts = domain.split('.');
  for (let i = 0; i < parts.length - 1; i++) {
    if (BLOCKED_DOMAINS.has(parts.slice(i).join('.'))) return true;
  }
  return false;
}

const server = http.createServer((req, res) => {
  const host = req.headers.host || 'localhost';
  const urlObj = new URL(req.url, `https://${host}`);

  // ۱. روت پینگ برای بیدار نگه داشتن سرور (Keep-Alive)
  if (urlObj.pathname === '/' || urlObj.pathname === '/ping') {
    res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('AdBlock DNS Server Online 🟢');
  }

  // ۲. روت صدور مستقیم پروفایل برای مرورگر سافاری آیفون
  if (urlObj.pathname === '/install-profile') {
    if (!verifyAccess(urlObj)) {
      res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
      return res.end('لینک نامعتبر یا اشتراک شما منقضی شده است.');
    }

    const uid = urlObj.searchParams.get('uid');
    const exp = urlObj.searchParams.get('exp');
    const sign = urlObj.searchParams.get('sign');
    const dohUrl = `https://${host}/dns-query?uid=${uid}&amp;exp=${exp}&amp;sign=${sign}`;

    const profileXml = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>PayloadContent</key>
    <array>
        <dict>
            <key>DNSSettings</key>
            <dict>
                <key>DNSProtocol</key>
                <string>HTTPS</string>
                <key>ServerURL</key>
                <string>${dohUrl}</string>
            </dict>
            <key>PayloadDescription</key>
            <string>پیکربندی هوشمند مسدودسازی تبلیغات</string>
            <key>PayloadDisplayName</key>
            <string>AdBlock DNS</string>
            <key>PayloadIdentifier</key>
            <string>com.adblock.dns.${uid}</string>
            <key>PayloadType</key>
            <string>com.apple.dnsSettings.managed</string>
            <key>PayloadUUID</key>
            <string>${crypto.randomUUID()}</string>
            <key>PayloadVersion</key>
            <integer>1</integer>
        </dict>
    </array>
    <key>PayloadDescription</key>
    <string>پروفایل رمزنگاری‌شده ضد تبلیغ</string>
    <key>PayloadDisplayName</key>
    <string>AdBlock Shield</string>
    <key>PayloadIdentifier</key>
    <string>com.adblock.profile.${uid}</string>
    <key>PayloadRemovalDisallowed</key>
    <false/>
    <key>PayloadType</key>
    <string>Configuration</string>
    <key>PayloadUUID</key>
    <string>${crypto.randomUUID()}</string>
    <key>PayloadVersion</key>
    <integer>1</integer>
</dict>
</plist>`;

    res.writeHead(200, {
      'Content-Type': 'application/x-apple-aspen-config; charset=utf-8',
      'Content-Disposition': 'attachment; filename="adblock.mobileconfig"'
    });
    return res.end(profileXml);
  }

  // ۳. هسته پردازش DoH
  if (urlObj.pathname === '/dns-query') {
    if (!verifyAccess(urlObj)) {
      res.writeHead(403, { 'Content-Type': 'text/plain' });
      return res.end('Unauthorized / Expired');
    }

    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      let dnsBuffer = null;
      if (req.method === 'POST') {
        dnsBuffer = Buffer.concat(chunks);
      } else if (req.method === 'GET' && urlObj.searchParams.has('dns')) {
        dnsBuffer = Buffer.from(urlObj.searchParams.get('dns'), 'base64url');
      }

      if (dnsBuffer && dnsBuffer.length > 12) {
        const domain = extractDomain(dnsBuffer);
        if (domain && isBlocked(domain)) {
          const fakeResp = createBlockResponse(dnsBuffer);
          res.writeHead(200, { 'Content-Type': 'application/dns-message' });
          return res.end(fakeResp);
        }
      }

      // فوروارد درخواست به بالادست
      const options = {
        hostname: UPSTREAM_HOST,
        port: 443,
        path: req.method === 'GET' ? `${UPSTREAM_PATH}?dns=${urlObj.searchParams.get('dns')}` : UPSTREAM_PATH,
        method: req.method,
        headers: {
          'accept': 'application/dns-message',
          'content-type': 'application/dns-message',
        }
      };

      const upstreamReq = https.request(options, upstreamRes => {
        res.writeHead(upstreamRes.statusCode, {
          'Content-Type': 'application/dns-message',
          'Cache-Control': 'public, max-age=120'
        });
        upstreamRes.pipe(res);
      });

      upstreamReq.on('error', () => {
        res.writeHead(502);
        res.end('Upstream Error');
      });

      if (dnsBuffer && req.method === 'POST') {
        upstreamReq.write(dnsBuffer);
      }
      upstreamReq.end();
    });
    return;
  }

  res.writeHead(404);
  res.end('Not Found');
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
});
