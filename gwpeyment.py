# -*- coding: utf-8 -*-
"""
GW Smart Analytics - COMPLETE PAYMENT SYSTEM (single file)

Kasa chalvaycha:
    python gw_payment.py
    mag browser madhe ughda:  http://localhost:8080

Kahi install karaychi garaj nahi (fakt Python 3.8+ pahije). Sagla kahi ya ek file madhe aahe:
  1. New user registration (unique user ID + email + password)
  2. Subscription Rs.800 / 30 divas
  3. Razorpay payment (server-side verification + webhook)
  4. Database verification (unique transaction ID + status + subscription)
  5. Dashboard / Reports unlock (fakt active subscription asel tevha)
  6. Automatic PDF receipt

DEMO MODE: jar Razorpay keys bharlya nahit, tar page "demo" madhe chalto (khari payment hot nahi,
fakt test button). Khari payment sathi khali RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET bhara.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

# =====================================================================
# SETTINGS  (yethe badal kara, kiva environment variable vapra)
# =====================================================================
# Razorpay Dashboard -> Settings -> API Keys. Pahile TEST keys vapra (rzp_test_...).
# Ya keys konalahi dakhvu naka, public GitHub var takun naka.
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
# Razorpay Dashboard -> Settings -> Webhooks (event: payment.captured). Optional pan recommended.
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

PRICE_RUPEES = 800          # subscription price
SUBSCRIPTION_DAYS = 30      # subscription validity
HOST = os.environ.get("HOST", "127.0.0.1")   # internet var chalvtana "0.0.0.0" kara
PORT = int(os.environ.get("PORT", "8080"))
DB_FILE = os.environ.get("GW_PAYMENT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gw_payments.db"))
TOKEN_HOURS = 24 * 7        # login kiti divas tikto
# =====================================================================

DEMO_MODE = not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
PRICE_PAISE = PRICE_RUPEES * 100
IST = timezone(timedelta(hours=5, minutes=30))
MAX_BODY = 64 * 1024


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_FILE, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL REFERENCES users(id),
            order_id    TEXT NOT NULL UNIQUE,
            payment_id  TEXT UNIQUE,
            amount      INTEGER NOT NULL,
            currency    TEXT NOT NULL DEFAULT 'INR',
            status      TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            paid_at     INTEGER
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL REFERENCES users(id),
            payment_id  TEXT NOT NULL UNIQUE,
            starts_at   INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL
        );
        """
    )
    row = con.execute("SELECT value FROM settings WHERE key='token_secret'").fetchone()
    if not row:
        con.execute("INSERT INTO settings (key, value) VALUES ('token_secret', ?)", (secrets.token_hex(32),))
        row = con.execute("SELECT value FROM settings WHERE key='token_secret'").fetchone()
    con.close()
    return row["value"].encode()


TOKEN_SECRET = None


# ---------------------------------------------------------------------
# Passwords + login tokens
# ---------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(password, stored):
    try:
        salt_b64, _ = stored.split("$")
        salt = base64.b64decode(salt_b64)
    except Exception:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user_id, email):
    if TOKEN_SECRET is None:
        raise RuntimeError("The standalone payment server has not been initialized.")
    payload = b64u(json.dumps({"id": user_id, "email": email, "exp": int(time.time()) + TOKEN_HOURS * 3600}).encode())
    sig = b64u(hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).digest())
    return payload + "." + sig


def read_token(token):
    try:
        payload, sig = token.split(".")
        good = b64u(hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(b64u_dec(payload))
        return data if data["exp"] > time.time() else None
    except Exception:
        return None


# Simple brute-force protection (per IP)
_attempts = {}


def rate_limited(ip, limit=10, window=300):
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < window]
    hits.append(now)
    _attempts[ip] = hits
    return len(hits) > limit


# ---------------------------------------------------------------------
# Subscription / payment logic
# ---------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def active_sub(con, user_id):
    return con.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
        (user_id, int(time.time())),
    ).fetchone()


def activate_payment(order_id, payment_id):
    """Payment 'paid' mark karto ani subscription banvto. Don dahaa vela call kela tari surakshit (idempotent)."""
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
        if not p:
            raise ValueError("Unknown order")
        if p["status"] == "paid":
            con.execute("COMMIT")
            return
        now = int(time.time())
        # payment_id UNIQUE aahe: ekach transaction dusrya order sathi vaparta yet nahi
        con.execute(
            "UPDATE payments SET status='paid', payment_id=?, paid_at=? WHERE order_id=?",
            (payment_id, now, order_id),
        )
        cur = active_sub(con, p["user_id"])
        start = cur["expires_at"] if cur else now  # active plan asel tar tyachya shevtapasun vadhva
        con.execute(
            "INSERT INTO subscriptions (user_id, payment_id, starts_at, expires_at) VALUES (?,?,?,?)",
            (p["user_id"], payment_id, start, start + SUBSCRIPTION_DAYS * 86400),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


def razorpay_create_order(user_id, amount_paise=None):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise ApiError(503, "Razorpay keys set kelele nahit.")
    amount = PRICE_PAISE if amount_paise is None else int(amount_paise)
    if amount <= 0:
        raise ApiError(400, "Payment amount chukichi aahe.")
    body = json.dumps(
        {
            "amount": amount,  # amount SERVER var thevla aahe, user badlu shakat nahi
            "currency": "INR",
            "receipt": "gw_" + secrets.token_hex(6),
            "notes": {"user_id": user_id},
        }
    ).encode()
    req = urllib.request.Request("https://api.razorpay.com/v1/orders", data=body, method="POST")
    cred = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req.add_header("Authorization", "Basic " + cred)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("Razorpay error:", e.code, e.read()[:300], file=sys.stderr)
        raise ApiError(502, "Payment gateway ne order nakarla. Keys tapasa.")
    except Exception as e:
        print("Razorpay connection error:", e, file=sys.stderr)
        raise ApiError(502, "Payment gateway la connect hot nahi. Internet tapasa.")


def razorpay_verify_signature(order_id, payment_id, signature):
    if not RAZORPAY_KEY_SECRET:
        raise ApiError(503, "Razorpay secret set kelele nahit.")
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def razorpay_get_payment(payment_id):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise ApiError(503, "Razorpay keys set kelele nahit.")
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/payments/" + quote(str(payment_id), safe=""),
        method="GET",
    )
    cred = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req.add_header("Authorization", "Basic " + cred)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        print("Razorpay payment lookup error:", error.code, error.read()[:300], file=sys.stderr)
        raise ApiError(502, "Razorpay payment status verify karta ala nahi.")
    except Exception as error:
        print("Razorpay payment lookup connection error:", error, file=sys.stderr)
        raise ApiError(502, "Payment gateway la connect hot nahi. Internet tapasa.")


# ---------------------------------------------------------------------
# PDF receipt (library nako - PDF hatane banvla jato)
# ---------------------------------------------------------------------
def make_pdf(title, subtitle, rows, big_line, footer):
    def esc(s):
        s = str(s).encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = [
        f"BT /F2 22 Tf 50 780 Td ({esc(title)}) Tj ET",
        f"BT /F1 13 Tf 50 755 Td ({esc(subtitle)}) Tj ET",
        "0.8 w 50 742 m 545 742 l S",
    ]
    y = 715
    for label, value in rows:
        ops.append(f"BT /F2 11 Tf 50 {y} Td ({esc(label)}) Tj ET")
        ops.append(f"BT /F1 11 Tf 190 {y} Td ({esc(value)}) Tj ET")
        y -= 24
    y -= 10
    ops.append("0.8 w 50 %d m 545 %d l S" % (y + 15, y + 15))
    ops.append(f"BT /F2 15 Tf 50 {y - 12} Td ({esc(big_line)}) Tj ET")
    ops.append(f"BT /F1 9 Tf 50 60 Td ({esc(footer)}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def fmt_ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d-%b-%Y %I:%M %p IST")


def build_receipt(user_id, payment_id):
    con = db()
    try:
        r = con.execute(
            """SELECT p.*, u.email, s.starts_at, s.expires_at
                 FROM payments p
                 JOIN users u ON u.id = p.user_id
                 JOIN subscriptions s ON s.payment_id = p.payment_id
                WHERE p.payment_id=? AND p.user_id=? AND p.status='paid'""",
            (payment_id, user_id),
        ).fetchone()
    finally:
        con.close()
    if not r:
        return None
    receipt_no = "GW-%s-%05d" % (datetime.fromtimestamp(r["paid_at"], IST).strftime("%Y%m%d"), r["id"])
    is_demo = str(r["payment_id"]).startswith("demo_")
    rows = [
        ("Receipt No:", receipt_no),
        ("User ID:", r["user_id"]),
        ("Email:", r["email"]),
        ("Transaction ID:", r["payment_id"]),
        ("Order ID:", r["order_id"]),
        ("Payment Status:", "PAID" + (" (DEMO - not a real payment)" if is_demo else "")),
        ("Payment Date:", fmt_ist(r["paid_at"])),
        ("Plan:", f"{SUBSCRIPTION_DAYS}-day subscription"),
        ("Valid From:", fmt_ist(r["starts_at"])),
        ("Valid Until:", fmt_ist(r["expires_at"])),
    ]
    pdf = make_pdf(
        "GW Smart Analytics",
        "Payment Receipt",
        rows,
        "Amount Paid: INR %.2f" % (r["amount"] / 100),
        "This is a computer-generated receipt and does not require a signature.",
    )
    return receipt_no, pdf


# ---------------------------------------------------------------------
# Web page (HTML + JS)
# ---------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GW Smart Analytics - Payment</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#6b7280; --accent:#1f9d55; --border:#e3e6eb; --err:#c0392b; --warn:#fff4d6; --warnt:#7a5b00; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171c; --card:#1d2229; --text:#e8ebf0; --muted:#98a1ae; --accent:#35c07a; --border:#2d343d; --err:#ff7b6b; --warn:#3a3217; --warnt:#ffd877; }
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
  main { max-width:540px; margin:28px auto; padding:0 16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:16px; }
  h1 { font-size:21px; margin:0 0 16px; } h2 { font-size:16px; margin:0 0 12px; }
  input { width:100%; padding:11px; margin-bottom:10px; border:1px solid var(--border); border-radius:8px; background:var(--bg); color:var(--text); font-size:15px; }
  button { padding:11px 18px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.6; cursor:wait; }
  button.secondary { background:transparent; color:var(--text); border:1px solid var(--border); }
  .price { font-size:34px; font-weight:700; color:var(--accent); margin:4px 0; }
  .muted { color:var(--muted); font-size:13px; } .err { color:var(--err); font-size:14px; min-height:20px; margin:8px 0 0; }
  .row { display:flex; gap:8px; flex-wrap:wrap; } .hidden { display:none; }
  .locked { opacity:.55; } li { margin:7px 0; word-break:break-all; } a { color:var(--accent); }
  .demo { background:var(--warn); color:var(--warnt); padding:10px 14px; border-radius:8px; font-size:13px; margin-bottom:16px; }
  .ok { color:var(--accent); font-weight:600; }
</style>
</head>
<body>
<main>
  <h1>GW Smart Analytics</h1>
  <div id="demoBanner" class="demo hidden"><b>DEMO MODE:</b> Razorpay keys bharlya nahit, tyamule khari payment hot nahi. Fakt test sathi.</div>

  <!-- 1. Registration / Login -->
  <section id="authBox" class="card">
    <h2>Account banva kiva login kara</h2>
    <input id="email" type="email" placeholder="Email" autocomplete="email">
    <input id="password" type="password" placeholder="Password (kamit kami 8 characters)" autocomplete="current-password">
    <div class="row">
      <button id="registerBtn">Register</button>
      <button id="loginBtn" class="secondary">Login</button>
    </div>
    <p id="authErr" class="err"></p>
  </section>

  <section id="appBox" class="hidden">
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <div><strong id="who"></strong><div class="muted" id="uid"></div></div>
        <button id="logoutBtn" class="secondary">Logout</button>
      </div>
    </div>

    <!-- 2 + 3. Subscription + Payment -->
    <div class="card">
      <h2>Subscription</h2>
      <div class="price">&#8377;<span id="amount">800</span></div>
      <div class="muted"><span id="days">30</span> divas Dashboard ani Reports cha access</div>
      <p><button id="payBtn">Pay securely</button></p>
      <p id="payErr" class="err"></p>
      <p id="status" class="muted"></p>
    </div>

    <!-- 5. Unlocked features -->
    <div id="dashCard" class="card locked"><h2>Dashboard <span id="dashLock">&#128274;</span></h2><div id="dashBody" class="muted">Unlock karnyasathi subscription ghya.</div></div>
    <div id="repCard" class="card locked"><h2>Reports <span id="repLock">&#128274;</span></h2><div id="repBody" class="muted">Unlock karnyasathi subscription ghya.</div></div>

    <!-- 6. Receipts -->
    <div class="card"><h2>Payment receipts (PDF)</h2><ul id="receipts" class="muted"><li>Ajun payment nahi.</li></ul></div>
  </section>
</main>

<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem('gw_token');
let config = {};

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || 'Request failed'), { status: res.status });
  return data;
}

async function authenticate(kind) {
  $('authErr').textContent = '';
  try {
    const data = await api('/api/' + kind, {
      method: 'POST',
      body: JSON.stringify({ email: $('email').value, password: $('password').value }),
    });
    token = data.token;
    localStorage.setItem('gw_token', token);
    await loadApp();
  } catch (e) { $('authErr').textContent = e.message; }
}

async function downloadReceipt(paymentId) {
  const res = await fetch('/api/receipt/' + encodeURIComponent(paymentId), { headers: { Authorization: 'Bearer ' + token } });
  if (!res.ok) { alert('Receipt milali nahi'); return; }
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'GW-Receipt-' + paymentId + '.pdf';
  document.body.appendChild(a); a.click(); a.remove();
}

async function loadReceipts() {
  const list = await api('/api/payments');
  const ul = $('receipts');
  ul.innerHTML = '';
  if (!list.length) { ul.innerHTML = '<li>Ajun payment nahi.</li>'; return; }
  list.forEach((p) => {
    const li = document.createElement('li');
    li.textContent = '\u20B9' + (p.amount / 100) + ' \u00B7 ' + new Date(p.paid_at * 1000).toLocaleDateString() + ' \u00B7 ' + p.payment_id + ' ';
    const a = document.createElement('a');
    a.href = '#'; a.textContent = 'Download PDF';
    a.onclick = (e) => { e.preventDefault(); downloadReceipt(p.payment_id); };
    li.appendChild(a);
    ul.appendChild(li);
  });
}

async function loadApp() {
  try {
    config = await api('/api/config');
    const me = await api('/api/me');
    $('demoBanner').classList.toggle('hidden', !config.demo);
    $('authBox').classList.add('hidden');
    $('appBox').classList.remove('hidden');
    $('who').textContent = me.email;
    $('uid').textContent = 'User ID: ' + me.userId;
    $('amount').textContent = config.amount;
    $('days').textContent = config.days;

    if (me.subscribed) {
      $('status').innerHTML = '<span class="ok">Subscription active</span> - ' + new Date(me.expiresAt * 1000).toLocaleString() + ' paryant';
      $('payBtn').textContent = 'Renew / vadhva';
      const dash = await api('/api/dashboard');
      const rep = await api('/api/reports');
      $('dashBody').textContent = 'Visitors: ' + dash.stats.visitors + ' | Conversions: ' + dash.stats.conversions + ' | Revenue: \u20B9' + dash.stats.revenue;
      $('repBody').textContent = rep.reports.map((r) => r.name).join(' | ');
      $('dashCard').classList.remove('locked'); $('repCard').classList.remove('locked');
      $('dashLock').textContent = '\u2705'; $('repLock').textContent = '\u2705';
    } else {
      $('status').textContent = 'Active subscription nahi.';
    }
    await loadReceipts();
  } catch (e) {
    if (e.status === 401) logout();
  }
}

async function pay() {
  $('payErr').textContent = '';
  $('payBtn').disabled = true;
  try {
    const order = await api('/api/create-order', { method: 'POST' });

    if (config.demo) {
      if (!confirm('DEMO: khari payment honar nahi. Success manun test karu?')) return;
      await api('/api/demo-pay', { method: 'POST', body: JSON.stringify({ order_id: order.orderId }) });
      await loadApp();
      return;
    }

    const rzp = new Razorpay({
      key: config.keyId,
      order_id: order.orderId,
      amount: order.amount,
      currency: order.currency,
      name: 'GW Smart Analytics',
      description: config.days + '-day subscription',
      prefill: { email: $('who').textContent },
      handler: async (response) => {
        try {
          await api('/api/verify-payment', { method: 'POST', body: JSON.stringify(response) });
          await loadApp();
        } catch (e) { $('payErr').textContent = e.message; }
      },
    });
    rzp.on('payment.failed', (r) => { $('payErr').textContent = 'Payment fail zali: ' + r.error.description; });
    rzp.open();
  } catch (e) { $('payErr').textContent = e.message; }
  finally { $('payBtn').disabled = false; }
}

function logout() { localStorage.removeItem('gw_token'); token = null; location.reload(); }

$('registerBtn').onclick = () => authenticate('register');
$('loginBtn').onclick = () => authenticate('login');
$('payBtn').onclick = pay;
$('logoutBtn').onclick = logout;
fetch('/api/config').then((r) => r.json()).then((c) => { config = c; $('demoBanner').classList.toggle('hidden', !c.demo); });
if (token) loadApp();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "GWPay/1.0"

    # ---- helpers ----
    def send_bytes(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def raw_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            raise ApiError(413, "Request khup motha aahe")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        try:
            data = json.loads(self.raw_body() or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            raise ApiError(400, "Chukichi request")

    def current_user(self):
        auth = self.headers.get("Authorization", "")
        data = read_token(auth[7:]) if auth.startswith("Bearer ") else None
        if not data:
            raise ApiError(401, "Krupaya login kara")
        con = db()
        try:
            if not con.execute("SELECT 1 FROM users WHERE id=?", (data["id"],)).fetchone():
                raise ApiError(401, "Krupaya login kara")
        finally:
            con.close()
        return data

    def require_subscription(self, user):
        con = db()
        try:
            sub = active_sub(con, user["id"])
        finally:
            con.close()
        if not sub:
            raise ApiError(402, "Active subscription pahije")
        return sub

    def client_ip(self):
        return self.client_address[0]

    # ---- routing ----
    def handle_request(self, method):
        try:
            path = self.path.split("?")[0]
            if method == "GET":
                return self.route_get(path)
            return self.route_post(path)
        except ApiError as e:
            self.send_json(e.code, {"error": e.message})
        except Exception as e:  # unexpected
            print("Server error:", repr(e), file=sys.stderr)
            self.send_json(500, {"error": "Server madhe adchan aali. Punha prayatna kara."})

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def route_get(self, path):
        if path in ("/", "/payment"):
            return self.send_bytes(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/api/config":
            return self.send_json(
                200, {"keyId": RAZORPAY_KEY_ID, "amount": PRICE_RUPEES, "days": SUBSCRIPTION_DAYS, "demo": DEMO_MODE}
            )

        user = self.current_user()

        if path == "/api/me":
            con = db()
            try:
                sub = active_sub(con, user["id"])
            finally:
                con.close()
            return self.send_json(
                200,
                {"userId": user["id"], "email": user["email"], "subscribed": bool(sub),
                 "expiresAt": sub["expires_at"] if sub else None},
            )

        if path == "/api/payments":
            con = db()
            try:
                rows = con.execute(
                    "SELECT order_id, payment_id, amount, status, paid_at FROM payments "
                    "WHERE user_id=? AND status='paid' ORDER BY paid_at DESC",
                    (user["id"],),
                ).fetchall()
            finally:
                con.close()
            return self.send_json(200, [dict(r) for r in rows])

        if path.startswith("/api/receipt/"):
            payment_id = path[len("/api/receipt/"):]
            result = build_receipt(user["id"], payment_id)
            if not result:
                raise ApiError(404, "Receipt milali nahi")
            receipt_no, pdf = result
            return self.send_bytes(
                200, pdf, "application/pdf",
                {"Content-Disposition": f'attachment; filename="{receipt_no}.pdf"'},
            )

        # ---- paid features (subscription pahije) ----
        # Yethe tumchya kharya analytics cha data ghala.
        if path == "/api/dashboard":
            sub = self.require_subscription(user)
            return self.send_json(
                200, {"subscriptionExpires": sub["expires_at"],
                      "stats": {"visitors": 1280, "conversions": 87, "revenue": 45200}}
            )

        if path == "/api/reports":
            self.require_subscription(user)
            return self.send_json(
                200, {"reports": [{"id": 1, "name": "Monthly Summary"}, {"id": 2, "name": "User Growth"},
                                  {"id": 3, "name": "Revenue Breakdown"}]}
            )

        raise ApiError(404, "Page milala nahi")

    def route_post(self, path):
        # Webhook: raw body pahije, login nako (Razorpay swatah pathavto)
        if path == "/api/webhook":
            return self.webhook()

        if path in ("/api/register", "/api/login"):
            if rate_limited(self.client_ip()):
                raise ApiError(429, "Khup prayatna zale. Thoda vel thamba.")
            data = self.json_body()
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            con = db()
            try:
                if path == "/api/register":
                    if not EMAIL_RE.match(email) or len(email) > 254:
                        raise ApiError(400, "Valid email taka")
                    if len(password) < 8 or len(password) > 200:
                        raise ApiError(400, "Password kamit kami 8 characters cha pahije")
                    user_id = str(uuid.uuid4())  # unique user ID
                    try:
                        con.execute(
                            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
                            (user_id, email, hash_password(password), int(time.time())),
                        )
                    except sqlite3.IntegrityError:
                        raise ApiError(409, "Ha email already registered aahe")
                    return self.send_json(200, {"token": make_token(user_id, email), "userId": user_id})
                row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if not row or not check_password(password, row["password_hash"]):
                    raise ApiError(401, "Email kiva password chukicha aahe")
                return self.send_json(200, {"token": make_token(row["id"], email), "userId": row["id"]})
            finally:
                con.close()

        user = self.current_user()

        # Step: order banva
        if path == "/api/create-order":
            if DEMO_MODE:
                order_id, amount = "demo_order_" + secrets.token_hex(8), PRICE_PAISE
            else:
                order = razorpay_create_order(user["id"])
                order_id, amount = order["id"], order["amount"]
            con = db()
            try:
                con.execute(
                    "INSERT INTO payments (user_id, order_id, amount, currency, status, created_at) "
                    "VALUES (?,?,?,'INR','created',?)",
                    (user["id"], order_id, amount, int(time.time())),
                )
            finally:
                con.close()
            return self.send_json(200, {"orderId": order_id, "amount": amount, "currency": "INR"})

        # Step: server-side payment verification
        if path == "/api/verify-payment":
            if DEMO_MODE:
                raise ApiError(400, "Demo mode madhe real verification nahi")
            data = self.json_body()
            order_id = str(data.get("razorpay_order_id", ""))
            payment_id = str(data.get("razorpay_payment_id", ""))
            signature = str(data.get("razorpay_signature", ""))
            if not (order_id and payment_id and signature):
                raise ApiError(400, "Payment details adhurya aahet")
            con = db()
            try:
                p = con.execute(
                    "SELECT * FROM payments WHERE order_id=? AND user_id=?", (order_id, user["id"])
                ).fetchone()
                if not p:
                    raise ApiError(404, "Order milali nahi")
                expected = hmac.new(
                    RAZORPAY_KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(expected, signature):
                    con.execute("UPDATE payments SET status='failed' WHERE order_id=? AND status='created'", (order_id,))
                    raise ApiError(400, "Payment verification fail zale")
            finally:
                con.close()
            try:
                activate_payment(order_id, payment_id)
            except sqlite3.IntegrityError:
                raise ApiError(409, "Ha transaction already vaparla aahe")
            return self.send_json(200, {"success": True, "receiptUrl": "/api/receipt/" + payment_id})

        # Fakt DEMO sathi: khoti success payment
        if path == "/api/demo-pay":
            if not DEMO_MODE:
                raise ApiError(404, "Page milala nahi")
            order_id = str(self.json_body().get("order_id", ""))
            con = db()
            try:
                p = con.execute(
                    "SELECT payment_id, status FROM payments WHERE order_id=? AND user_id=?", (order_id, user["id"])
                ).fetchone()
            finally:
                con.close()
            if not p:
                raise ApiError(404, "Order milali nahi")
            if p["status"] == "paid":
                return self.send_json(200, {"success": True, "receiptUrl": "/api/receipt/" + p["payment_id"]})
            payment_id = "demo_pay_" + secrets.token_hex(8)
            activate_payment(order_id, payment_id)
            return self.send_json(200, {"success": True, "receiptUrl": "/api/receipt/" + payment_id})

        raise ApiError(404, "Page milala nahi")

    def webhook(self):
        if not RAZORPAY_WEBHOOK_SECRET:
            raise ApiError(503, "Webhook secret set kela nahi")
        body = self.raw_body()
        expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Razorpay-Signature", "")):
            raise ApiError(400, "Invalid signature")
        event = json.loads(body or b"{}")
        if event.get("event") == "payment.captured":
            entity = event["payload"]["payment"]["entity"]
            try:
                activate_payment(entity["order_id"], entity["id"])
            except (ValueError, sqlite3.IntegrityError):
                pass  # ya aaplya system cha order nahi kiva already processed
        self.send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    global TOKEN_SECRET
    TOKEN_SECRET = init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60)
    print(" GW Smart Analytics Payment System")
    print(f" Open:  http://localhost:{PORT}")
    print(" Mode:  " + ("DEMO (Razorpay keys nahit - khari payment hot nahi)" if DEMO_MODE else "LIVE Razorpay"))
    print(f" Price: Rs.{PRICE_RUPEES} / {SUBSCRIPTION_DAYS} days    DB: {DB_FILE}")
    print(" Thambavnyasathi Ctrl+C dabva")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBand kele.")


if __name__ == "__main__":
    main()