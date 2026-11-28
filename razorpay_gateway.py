"""
razorpay_gateway.py - Razorpay payments for GW Smart Analytics (Flask).

Cashfree chya jagi he use hota. Fakt `requests` lagte (Razorpay SDK nako).

.env madhe he 3 values bhara:
    RAZORPAY_KEY_ID=rzp_test_xxxxxxxx
    RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxx
    RAZORPAY_WEBHOOK_SECRET=xxxxxxxx   # Razorpay Dashboard > Webhooks madhe tumhi swatah thevta

server.py madhe:
    from razorpay_gateway import razorpay_bp
    app.register_blueprint(razorpay_bp)
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid

import requests
from flask import Blueprint, jsonify, request

try:  # .env already load hot asel tar harkat nahi
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

razorpay_bp = Blueprint("razorpay", __name__)

API_BASE = "https://api.razorpay.com/v1"
DB_PATH = os.getenv("RAZORPAY_DB", "gw_razorpay.db")

# ---------------------------------------------------------------------------
# EDIT THIS: plan -> price in PAISE (Rs 1 = 100 paise). Price nehmi server var
# thharvaychi, browser kadun amount ghyaicha nahi (nahitar konihi kami kimat pathavel).
# ---------------------------------------------------------------------------
PLANS = {
    "starter": {"amount": 49900, "label": "Starter Plan"},
    "pro": {"amount": 99900, "label": "Pro Plan"},
    # Pratyek unique dashboard sathi Rs 10 (1000 paise)
    "dashboard": {"amount": 1000, "label": "Dashboard (Rs 10)"},
}
CURRENCY = "INR"


def _keys():
    key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET .env madhe set nahit")
    return key_id, key_secret


# ---------------------------------------------------------------------------
# Database (swatantra sqlite file, tumchya existing DB la dhakka lagat nahi)
# ---------------------------------------------------------------------------
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS razorpay_payments (
            order_id    TEXT PRIMARY KEY,
            payment_id  TEXT,
            plan        TEXT,
            user_ref    TEXT,
            amount      INTEGER,
            currency    TEXT,
            status      TEXT,      -- created | paid | failed
            receipt     TEXT,
            created_at  REAL,
            paid_at     REAL
        )"""
    )
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(razorpay_payments)")]
    if "dashboard_id" not in cols:  # junya DB sathi auto-upgrade
        conn.execute("ALTER TABLE razorpay_payments ADD COLUMN dashboard_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rzp_dash ON razorpay_payments(dashboard_id, status)"
    )
    return conn


def is_dashboard_paid(dashboard_id):
    """Server-side check. Dashboard generate/download karnya adhi HI function vapra."""
    if not dashboard_id:
        return False
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM razorpay_payments WHERE dashboard_id=? AND status='paid'",
            (str(dashboard_id),),
        ).fetchone()
    return row is not None


def on_payment_success(row):
    """
    EKDACH PAID zalyavar hi function call hote (verify kinva webhook, je adhi yeil).
    ITHE tumcha existing logic jod: plan activate kara, receipt/mail pathva,
    dashboard_payments.py chya table madhe entry kara, etc.
    `row` = dict(order_id, payment_id, plan, user_ref, amount, currency, receipt).
    """
    print("[razorpay] PAID:", dict(row))


def _mark_paid(order_id, payment_id):
    """Idempotent: verify aani webhook donhi aale tari hook fakt ekda chalto."""
    with _db() as conn:
        cur = conn.execute(
            "UPDATE razorpay_payments SET status='paid', payment_id=?, paid_at=? "
            "WHERE order_id=? AND status!='paid'",
            (payment_id, time.time(), order_id),
        )
        newly_paid = cur.rowcount == 1
        row = conn.execute(
            "SELECT * FROM razorpay_payments WHERE order_id=?", (order_id,)
        ).fetchone()
    if newly_paid and row:
        on_payment_success(dict(row))
    return newly_paid, (dict(row) if row else None)


def _mark_failed(order_id, payment_id):
    with _db() as conn:
        conn.execute(
            "UPDATE razorpay_payments SET status='failed', payment_id=? "
            "WHERE order_id=? AND status='created'",
            (payment_id, order_id),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@razorpay_bp.route("/api/razorpay/config", methods=["GET"])
def rzp_config():
    try:
        key_id, _ = _keys()
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, key_id=key_id, currency=CURRENCY)


@razorpay_bp.route("/api/razorpay/create-order", methods=["POST"])
def rzp_create_order():
    data = request.get_json(silent=True) or {}
    plan = str(data.get("plan", "")).strip()
    user_ref = str(data.get("user_ref", "")).strip()  # email / user id (optional)
    dashboard_id = str(data.get("dashboard_id", "")).strip()

    if plan not in PLANS:
        return jsonify(ok=False, error="Invalid plan"), 400
    if plan == "dashboard":
        if not dashboard_id:
            return jsonify(ok=False, error="dashboard_id required"), 400
        if is_dashboard_paid(dashboard_id):
            return jsonify(ok=False, error="Ya dashboard cha payment adhich zala aahe",
                           already_paid=True), 409
    try:
        key_id, key_secret = _keys()
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 500

    amount = PLANS[plan]["amount"]
    receipt = "gw_" + uuid.uuid4().hex[:20]  # Razorpay limit: 40 chars

    try:
        resp = requests.post(
            f"{API_BASE}/orders",
            auth=(key_id, key_secret),
            json={
                "amount": amount,
                "currency": CURRENCY,
                "receipt": receipt,
                "notes": {"plan": plan, "user_ref": user_ref, "dashboard_id": dashboard_id},
            },
            timeout=20,
        )
    except requests.RequestException as e:
        return jsonify(ok=False, error=f"Razorpay connect hot nahi: {e}"), 502

    if resp.status_code != 200:
        # Razorpay cha asli error ithe disel (chukichya keys, etc.)
        return jsonify(ok=False, error="Razorpay order failed", detail=resp.text), 502

    order = resp.json()
    with _db() as conn:
        conn.execute(
            "INSERT INTO razorpay_payments "
            "(order_id, plan, user_ref, amount, currency, status, receipt, created_at, dashboard_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (order["id"], plan, user_ref, amount, CURRENCY, "created", receipt, time.time(),
             dashboard_id),
        )
    return jsonify(
        ok=True,
        key_id=key_id,
        order_id=order["id"],
        amount=amount,
        currency=CURRENCY,
        name=PLANS[plan]["label"],
    )


@razorpay_bp.route("/api/razorpay/verify", methods=["POST"])
def rzp_verify():
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("razorpay_order_id", ""))
    payment_id = str(data.get("razorpay_payment_id", ""))
    signature = str(data.get("razorpay_signature", ""))
    if not (order_id and payment_id and signature):
        return jsonify(ok=False, error="Missing fields"), 400

    try:
        _, key_secret = _keys()
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 500

    expected = hmac.new(
        key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify(ok=False, error="Signature mismatch"), 400

    # Order amhi banavlela ahe ka te pan tapasto
    with _db() as conn:
        known = conn.execute(
            "SELECT 1 FROM razorpay_payments WHERE order_id=?", (order_id,)
        ).fetchone()
    if not known:
        return jsonify(ok=False, error="Unknown order"), 404

    _, row = _mark_paid(order_id, payment_id)
    return jsonify(ok=True, status="paid", order_id=order_id, payment_id=payment_id,
                   dashboard_id=(row or {}).get("dashboard_id"))


@razorpay_bp.route("/api/razorpay/webhook", methods=["POST"])
def rzp_webhook():
    """Browser band zala tari payment capture hote te yethe kalte."""
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
    if not secret:
        return jsonify(ok=False, error="Webhook secret not set"), 500

    raw = request.get_data()  # signature RAW body var aste, json.dumps var nahi
    sig = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return jsonify(ok=False, error="Bad signature"), 400

    event = json.loads(raw.decode("utf-8") or "{}")
    etype = event.get("event", "")
    payment = (event.get("payload", {}).get("payment", {}) or {}).get("entity", {}) or {}
    order_id = payment.get("order_id")
    payment_id = payment.get("id")

    if etype in ("payment.captured", "order.paid") and order_id:
        if etype == "order.paid":
            order = (event.get("payload", {}).get("order", {}) or {}).get("entity", {}) or {}
            order_id = order.get("id", order_id)
        _mark_paid(order_id, payment_id)
    elif etype == "payment.failed" and order_id:
        _mark_failed(order_id, payment_id)

    return jsonify(ok=True)  # nehmi 200 dya, nahitar Razorpay retry karat rahto


@razorpay_bp.route("/api/razorpay/status/<order_id>", methods=["GET"])
def rzp_status(order_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT order_id, payment_id, plan, amount, status FROM razorpay_payments "
            "WHERE order_id=?",
            (order_id,),
        ).fetchone()
    if not row:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(ok=True, **dict(row))


@razorpay_bp.route("/api/razorpay/is-paid/<dashboard_id>", methods=["GET"])
def rzp_is_paid(dashboard_id):
    return jsonify(ok=True, dashboard_id=dashboard_id, paid=is_dashboard_paid(dashboard_id))
