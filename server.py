"""
GW Analytics - Python Flask Server & API Gateway
=================================================
Features:
- Unique User Registration (/api/register) -> Enforces unique email & scrypt password hashes
- User Login & Session (/api/login, /api/logout, /api/me) -> Sets session & sends automatic login notification
- Subscription & Payment API (/api/subscription/status, /api/payment/submit, /api/payment/receipt)
  * Holds UPI claims for administrator verification
  * Grants 30-day access and creates a receipt only after admin approval
- Dataset Upload API (/api/upload) -> Saves CSV/Excel datasets for dashboards
- Frontend Page Routing:
    * /                  -> index.html (Home Page)
    * /h                 -> h.html (GW Dashboard Generator)
    * /up                -> up.html (GW Smart Analytics Dashboard)
    * /login             -> login.html
    * /register          -> register.html
    * /about             -> about.html
    * /contact           -> contact.html
    * Static resources (images, css, js)

CHANGES IN THIS VERSION
-----------------------
1. FIXED  : submit_payment_api() crashed on  data.get("user_id", type=int)
            (dict.get() has no `type=` argument -> TypeError -> HTML 500 page
            -> "Unexpected token '<'" in the browser).
2. ADDED  : JSON error handler for every /api/* route, so the browser always
            gets a JSON message instead of an HTML error page.
3. FIXED  : duplicate /h.html route removed.
4. ADDED  : amount is fixed on the server (PLAN_PRICE), not trusted from browser.
5. ADDED  : UPI payment claims remain pending until an administrator verifies them.
"""

from flask import Flask, Response, request, jsonify, send_from_directory, redirect, session, g
from werkzeug.exceptions import HTTPException
from pathlib import Path
from datetime import datetime
import os
import re
import smtplib
import json
import logging
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

import database
import dashboard_payments
import gwpeyment
import mailer
import upload_storage
import assistent
from auth_widget import inject_current_account_widget

UPLOAD_DIR = database.UPLOAD_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = upload_storage.ALLOWED_EXTENSIONS
MAX_FILE_SIZE = int(os.getenv("GW_MAX_UPLOAD_MB", "50")) * 1024 * 1024

# ---- Payment settings -------------------------------------------------
PLAN_PRICE = float(os.getenv("GW_PLAN_PRICE", "10.0"))
PLAN_VALIDITY_DAYS = int(os.getenv("GW_PLAN_VALIDITY_DAYS", "30"))
COOKIE_SECURE = os.getenv("GW_COOKIE_SECURE", "0") == "1"
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or os.urandom(32).hex()
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv(
        "GW_ALLOWED_ORIGINS",
        "http://127.0.0.1:5000,http://localhost:5000,http://127.0.0.1:8501,http://localhost:8501",
    ).split(",")
    if origin.strip()
}

# -----------------------------------------------------------------------

# Browser identity is held in an opaque database-backed cookie, not Flask's client-side session.
app = Flask(__name__, static_folder=None)
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = COOKIE_SECURE

try:
    from flask_cors import CORS
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": sorted(ALLOWED_ORIGINS)}})
except ImportError:
    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,DELETE,OPTIONS"
        return response

# Auto-initialize SQLite database on launch
database.init_db()
configured_admin_email = os.getenv("GW_ADMIN_EMAIL", "").strip()
if configured_admin_email and not database.configure_admin_user(configured_admin_email):
    logging.getLogger(__name__).warning(
        "GW_ADMIN_EMAIL does not match exactly one existing account; no administrator was promoted."
    )
dashboard_payments.init_db()

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/register",
    "/api/login",
    "/api/logout",
    "/api/me",
    "/api/contact",
    "/api/dashboard-payments/webhook",
}

app.register_blueprint(dashboard_payments.blueprint)
app.register_blueprint(assistent.assistant_bp)


@app.before_request
def authenticate_api_request():
    if request.path.startswith("/api/") and request.path not in PUBLIC_API_PATHS and not request.path.startswith("/api/swati/") and not request.path.startswith("/api/assistant"):
        user = database.validate_web_session(request.cookies.get("gw_web_session", ""))
        if not user:
            return jsonify({"success": False, "authenticated": False, "message": "Login required."}), 401
        g.current_user = user


@app.after_request
def add_security_headers(response):
    response = inject_current_account_widget(response)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def current_user() -> Dict[str, Any]:
    """Returns the account validated by the per-request database session check."""
    user = getattr(g, "current_user", None)
    if user is None:
        raise RuntimeError("The route requires a validated account session.")
    return user


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def to_int_or_none(value):
    """Safely converts a value to int, returns None if not possible."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def current_admin():
    """Returns the signed-in user only when their database role is admin."""
    user = current_user()
    if user and str(user.get("role", "")).lower() == "admin":
        return user
    return None


# =========================================================
# SYSTEM & HEALTH CHECK
# =========================================================

@app.route("/api/health", methods=["GET"])
def health_check():
    """Returns health status of backend and SQLite database."""
    return jsonify({
        "success": True,
        "status": "online",
        "message": "GW Analytics Python Backend is running",
        "database": f"SQLite ({database.DB_PATH.name})",
        "upi_id": "9529579858-d3cb-2@ybl",
        "port": 5000,
        "timestamp": datetime.now().isoformat()
    }), 200


# =========================================================
# FRONTEND PAGE ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def index():
    """Serves the Home / Landing page."""
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/index.html", methods=["GET"])
def index_html():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/dashboard", methods=["GET"])
def streamlit_dashboard_page():
    user = database.validate_web_session(request.cookies.get("gw_web_session", ""))
    if not user:
        return redirect("/login.html")

    bridge_token = database.create_session_bridge_token(
        user["id"], web_session_id=user.get("web_session_id")
    )
    streamlit_url = os.getenv("GW_STREAMLIT_URL", "http://127.0.0.1:8501").rstrip("/")
    return redirect(f"{streamlit_url}/?gw_auth_token={bridge_token}")


@app.route("/p", methods=["GET"])
def dashboard_page():
    """Serves the GW Dashboard Generator page."""
    return send_from_directory(BASE_DIR, "h.html")


@app.route("/h.html", methods=["GET"])
def peyment_page():
    return send_from_directory(BASE_DIR, "h.html")


@app.route("/admin-payments", methods=["GET"])
@app.route("/admin-payments.html", methods=["GET"])
def admin_payments_page():
    return send_from_directory(BASE_DIR, "admin-payments.html")


@app.route("/shoppulse", methods=["GET"])
def shoppulse_page():
    """Serves the new ShopPulse Smart Shop Analytics application."""
    return send_from_directory(BASE_DIR, "shoppulse.html")


@app.route("/shoppulse.html", methods=["GET"])
def shoppulse_page_html():
    return send_from_directory(BASE_DIR, "shoppulse.html")



@app.route("/up", methods=["GET"])
def up_page():
    """Serves the GW Smart Analytics Dashboard."""
    return send_from_directory(BASE_DIR, "up.html")


@app.route("/up.html", methods=["GET"])
def up_page_html():
    return send_from_directory(BASE_DIR, "up.html")


@app.route("/register", methods=["GET"])
def register_page():
    return send_from_directory(BASE_DIR, "register.html")


@app.route("/register.html", methods=["GET"])
def register_page_html():
    return send_from_directory(BASE_DIR, "register.html")


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(BASE_DIR, "login.html")


@app.route("/login.html", methods=["GET"])
def login_page_html():
    return send_from_directory(BASE_DIR, "login.html")


@app.route("/about", methods=["GET"])
def about_page():
    return send_from_directory(BASE_DIR, "about.html")


@app.route("/about.html", methods=["GET"])
def about_page_html():
    return send_from_directory(BASE_DIR, "about.html")


@app.route("/contact", methods=["GET"])
def contact_page():
    return send_from_directory(BASE_DIR, "contact.html")


@app.route("/contact.html", methods=["GET"])
def contact_page_html():
    return send_from_directory(BASE_DIR, "contact.html")


@app.route("/scaner.png", methods=["GET"])
def scaner_png_alias():
    """Serves the UPI QR scanner image for scaner.png path."""
    return send_from_directory(BASE_DIR, "scanner.png")


@app.route("/<path:filename>", methods=["GET"])
def public_asset(filename: str):
    """Serves public presentation assets without exposing source, database, or uploads."""
    relative = Path(filename)
    if (
        not relative.parts
        or any(part.startswith(".") or part == ".." for part in relative.parts)
        or relative.parts[0].lower() in {"uploads", database.DB_PATH.name.lower()}
    ):
        return jsonify({"success": False, "message": "Not found."}), 404
    allowed_extensions = {
        ".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".webmanifest",
    }
    if relative.suffix.lower() not in allowed_extensions or (relative.suffix.lower() == ".html" and len(relative.parts) != 1):
        return jsonify({"success": False, "message": "Not found."}), 404
    return send_from_directory(BASE_DIR, filename)


# =========================================================
# USER AUTHENTICATION & SESSION APIS (SQLite)
# =========================================================

@app.route("/api/register", methods=["POST"])
def register_api():
    """
    Registers a new unique user in SQLite users table.
    Enforces unique email address.
    """
    data = request.get_json(silent=True) or request.form.to_dict()

    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "")
    phone = str(data.get("phone", data.get("mobile", ""))).strip()

    if not name or not email or not password or not phone:
        return jsonify({
            "success": False,
            "message": "नाव (Name), Email, Mobile Number आणि Password आवश्यक आहेत."
        }), 400

    result = database.create_user(name=name, email=email, password=password, phone=phone)

    if result["success"]:
        return jsonify(result), 201
    else:
        return jsonify(result), 400


@app.route("/api/login", methods=["POST"])
def login_api():
    """
    Authenticates user, creates session, and dispatches automated login email:
    "You have successfully logged in to Gw Smart Analytics. Welcome to Gw Smart Analytics."
    """
    data = request.get_json(silent=True) or request.form.to_dict()

    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({
            "success": False,
            "message": "Email आणि Password दोन्ही आवश्यक आहेत."
        }), 400

    result = database.authenticate_user(email=email, password=password)

    if result["success"]:
        user_info = result["user"]
        session.clear()
        web_session = database.create_web_session(user_info["id"])
        bridge_token = database.create_session_bridge_token(
            user_info["id"], web_session_id=web_session["session_id"]
        )

        # Dispatch automated login email notification
        try:
            mailer.send_login_notification(user_info["name"], user_info["email"])
        except Exception as e:
            print(f"[Login Notification Warning] {e}")

        response = jsonify({
            "success": True,
            "message": result["message"],
            "user": user_info,
            "subscription": result["subscription"]
        })
        response.set_cookie(
            "gw_web_session",
            web_session["token"],
            max_age=database.WEB_SESSION_IDLE_TTL_SECONDS,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="Lax",
            path="/",
        )
        response.set_cookie(
            "gw_session_token",
            bridge_token,
            max_age=database.WEB_SESSION_IDLE_TTL_SECONDS,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="Lax",
            path="/",
        )
        return response, 200
    else:
        return jsonify(result), 401


@app.route("/api/logout", methods=["POST", "GET"])
def logout_api():
    """Clears user session and logs out."""
    database.revoke_web_session(request.cookies.get("gw_web_session", ""))
    session.clear()
    response = jsonify({
        "success": True,
        "message": "लॉगआउट यशस्वी झाले. (Logged out successfully)"
    })
    response.delete_cookie(
        "gw_web_session",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    response.delete_cookie(
        "gw_session_token",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    return response, 200


@app.route("/api/me", methods=["GET"])
def get_current_user_api():
    """Returns currently logged-in user profile and subscription status from session."""
    session_token = request.cookies.get("gw_web_session", "")
    user = database.validate_web_session(session_token)
    if not user:
        status = 401 if session_token else 200
        return jsonify({"authenticated": False, "user": None}), status

    sub_status = user.get("subscription") or database.check_subscription_status(user_id=user["id"])
    user["is_paid"] = sub_status["active"]

    return jsonify({
        "authenticated": True,
        "user": user,
        "subscription": sub_status
    }), 200


@app.route("/api/users", methods=["GET"])
def get_users_api():
    """Returns list of registered users."""
    if not current_admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    users = database.get_all_users()
    return jsonify({
        "success": True,
        "count": len(users),
        "users": users
    })


@app.route("/api/users/<int:user_id>", methods=["GET"])
def get_single_user_api(user_id):
    """Returns a specific user by ID."""
    user = current_user()
    if not user or (user["id"] != user_id and str(user.get("role", "")).lower() != "admin"):
        return jsonify({"success": False, "message": "Access denied."}), 403
    user = database.get_user_by_id(user_id)
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404
    return jsonify({"success": True, "user": user})


# =========================================================
# SUBSCRIPTIONS & PAYMENT APIS (UPI & DATABASE)
# =========================================================

@app.route("/api/subscription/status", methods=["GET"])
def check_subscription_status_api():
    """
    Returns the signed-in user's active subscription and latest payment-review status.
    Strictly verifies if status == 'ACTIVE' and end_date is in the future.
    """
    user = current_user()
    sub = database.check_subscription_status(user_id=user["id"])
    sub["paid"] = sub["active"]  # Compatibility alias
    sub["payment"] = database.get_latest_payment_status(user["id"])
    return jsonify(sub), 200


@app.route("/api/payment/submit", methods=["POST"])
@app.route("/api/payment", methods=["POST"])
def submit_payment_api():
    """
    Stores a user's UPI claim as pending; access and receipt wait for admin approval.
    """
    data = request.get_json(silent=True) or request.form.to_dict()

    user = current_user()
    user_id = user["id"]

    # Price is decided by the server, never trusted from the browser.
    amount = PLAN_PRICE

    method = data.get("method") or "UPI (9529579858-d3cb-2@ybl)"
    utr_number = (data.get("utr_number") or data.get("transaction_id") or "").strip()
    if not re.fullmatch(r"\d{12}", utr_number):
        return jsonify({
            "success": False,
            "message": "कृपया बरोबर 12-अंकी UTR नंबर टाका (Enter a valid 12-digit UTR)."
        }), 400

    result = database.submit_pending_payment(
        user_id=user_id,
        email=user["email"],
        amount=amount,
        method=method,
        utr_number=utr_number
    )

    if not result["success"]:
        return jsonify(result), 400

    return jsonify({
        "success": True,
        "paid": False,
        "pending": True,
        "payment_request_id": result["payment_request_id"],
        "message": result["message"],
        "timestamp": datetime.now().isoformat()
    }), 202


@app.route("/api/payment/create-order", methods=["POST"])
def create_gateway_order_api():
    """Creates a Razorpay order for the logged-in user at the configured plan price."""
    if not gwpeyment.RAZORPAY_KEY_ID or not gwpeyment.RAZORPAY_KEY_SECRET:
        return jsonify({
            "success": False,
            "message": "Razorpay is not configured. Use the UPI option or configure the gateway keys."
        }), 503

    amount_paise = int(round(PLAN_PRICE * 100))
    user = current_user()
    try:
        order = gwpeyment.razorpay_create_order(str(user["id"]), amount_paise=amount_paise)
    except gwpeyment.ApiError as error:
        return jsonify({"success": False, "message": error.message}), error.code

    order_id = str(order.get("id", ""))
    if (
        not order_id
        or order.get("amount") != amount_paise
        or order.get("currency") != "INR"
    ):
        app.logger.error("Razorpay returned an incomplete or mismatched order.")
        return jsonify({"success": False, "message": "Payment gateway returned an invalid order."}), 502

    result = database.create_gateway_order(user["id"], order_id, amount_paise)
    if not result["success"]:
        return jsonify({"success": False, "message": result["message"]}), 409
    return jsonify({
        "success": True,
        "order_id": order_id,
        "key_id": gwpeyment.RAZORPAY_KEY_ID,
        "amount": amount_paise,
        "currency": "INR",
        "days": PLAN_VALIDITY_DAYS,
    }), 200


@app.route("/api/payment/verify", methods=["POST"])
def verify_gateway_payment_api():
    """Verifies Razorpay signature and captured payment before activating shared access."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("razorpay_order_id", "")).strip()
    payment_id = str(data.get("razorpay_payment_id", "")).strip()
    signature = str(data.get("razorpay_signature", "")).strip()
    if not order_id or not payment_id or not signature:
        return jsonify({"success": False, "message": "Payment verification details are incomplete."}), 400

    order = database.get_gateway_order(user["id"], order_id)
    if not order:
        return jsonify({"success": False, "message": "Payment order was not found for this account."}), 404
    try:
        signature_valid = gwpeyment.razorpay_verify_signature(order_id, payment_id, signature)
        if not signature_valid:
            return jsonify({"success": False, "message": "Razorpay signature verification failed."}), 400
        payment = gwpeyment.razorpay_get_payment(payment_id)
    except gwpeyment.ApiError as error:
        return jsonify({"success": False, "message": error.message}), error.code

    if (
        payment.get("id") != payment_id
        or payment.get("order_id") != order_id
        or payment.get("amount") != order["amount_paise"]
        or payment.get("currency") != "INR"
        or payment.get("status") != "captured"
    ):
        return jsonify({
            "success": False,
            "message": "Payment is not captured or does not match the order. The subscription was not activated."
        }), 400

    result = database.complete_gateway_order(
        user["id"], order_id, payment_id, validity_days=PLAN_VALIDITY_DAYS
    )
    if not result["success"]:
        return jsonify({"success": False, "message": result["message"]}), 409
    return jsonify({
        "success": True,
        "verified": True,
        "subscription_active": True,
        "receipt": result["receipt"],
    }), 200


@app.route("/api/admin/payments/pending", methods=["GET"])
def list_pending_payments_api():
    """Lists UPI claims for authenticated administrators to verify."""
    if not current_admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    return jsonify({"success": True, "payments": database.get_pending_payments()}), 200


@app.route("/api/admin/payments/<int:payment_row_id>/approve", methods=["POST"])
def approve_pending_payment_api(payment_row_id):
    """Activates a monthly subscription only after admin verifies the UPI payment."""
    admin = current_admin()
    if not admin:
        return jsonify({"success": False, "message": "Admin access required."}), 403

    result = database.approve_pending_payment(payment_row_id, PLAN_VALIDITY_DAYS)
    if not result["success"]:
        return jsonify(result), 409

    receipt = result["receipt"]
    try:
        mailer.send_payment_notification(
            user_name=receipt["customer_name"],
            to_email=receipt["customer_email"],
            receipt=receipt
        )
    except Exception as error:
        app.logger.warning("Payment confirmation email failed: %s", error)

    return jsonify({"success": True, "paid": True, "receipt": receipt}), 200


@app.route("/api/admin/payments/<int:payment_row_id>/reject", methods=["POST"])
def reject_pending_payment_api(payment_row_id):
    """Rejects a payment claim without granting access or issuing a receipt."""
    if not current_admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    result = database.reject_pending_payment(payment_row_id)
    return jsonify(result), 200 if result["success"] else 409


@app.route("/api/payment/receipt", methods=["GET"])
@app.route("/api/payment/receipt/<path:payment_id>", methods=["GET"])
def get_payment_receipt_api(payment_id=None):
    """Returns official receipt details for display or PDF generation."""
    user = current_user()
    receipt = database.get_latest_receipt(user_id=user["id"], payment_id=payment_id)
    if not receipt:
        return jsonify({"success": False, "message": "पावती सापडली नाही (Receipt not found)."}), 404

    return jsonify({"success": True, "receipt": receipt}), 200


@app.route("/api/payment/history", methods=["GET"])
def payment_history_api():
    user = current_user()
    return jsonify({"success": True, "payments": database.get_user_payment_history(user["id"])}), 200


@app.route("/api/subscription/history", methods=["GET"])
def subscription_history_api():
    user = current_user()
    return jsonify({"success": True, "subscriptions": database.get_user_subscription_history(user["id"])}), 200


@app.route("/api/contact", methods=["POST"])
def submit_contact_message_api():
    """Sends a contact form message to the GW Smart Analytics support inbox."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    sender_email = str(data.get("email", "")).strip()
    subject = str(data.get("subject", "")).strip()
    message = str(data.get("message", "")).strip()

    if not name or len(name) > 120 or any(char in name for char in "\r\n"):
        return jsonify({"success": False, "message": "Please enter a valid name (up to 120 characters)."}), 400
    if len(sender_email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender_email):
        return jsonify({"success": False, "message": "Please enter a valid email address."}), 400
    if subject not in {
        "General Enquiry", "Dashboard Support", "Report Support",
        "Payment Support", "Technical Support", "Other"
    }:
        return jsonify({"success": False, "message": "Please select a valid subject."}), 400
    if not message or len(message) > 10000:
        return jsonify({"success": False, "message": "Message is required and must be 10,000 characters or fewer."}), 400

    try:
        mailer.send_contact_message(name, sender_email, subject, message)
    except RuntimeError as error:
        app.logger.error("Contact message email could not be sent: %s", error)
        return jsonify({
            "success": False,
            "message": "Email sending is not ready. Copy .env.example to .env, set your Gmail App Password, then restart the server. Or email gwsmartanalytics0810@gmail.com directly."
        }), 503

    return jsonify({
        "success": True,
        "message": "Your message was sent to GW Smart Analytics support."
    }), 200


# =========================================================
# FILE UPLOAD APIS
# =========================================================

@app.route("/api/parse-dataset", methods=["POST"])
def parse_dataset():
    """Parses a supported tabular upload in memory for the browser dashboards."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file selected."}), 400
    raw = file.read()
    if not raw:
        return jsonify({"success": False, "error": "The uploaded file is empty."}), 400
    if len(raw) > MAX_FILE_SIZE:
        return jsonify({"success": False, "error": "The uploaded file exceeds the size limit."}), 413
    try:
        options = upload_storage.dataset_options(file.filename, raw)
        selection = request.form.get("selection")
        if options and not selection and len(options) > 1:
            return jsonify({"success": True, "requires_selection": True, "options": options}), 200
        frame = upload_storage.read_dataset(file.filename, raw, selection)
        rows = json.loads(frame.to_json(orient="records", date_format="iso", default_handler=str))
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    except Exception:
        app.logger.exception("Dataset parsing failed.")
        return jsonify({"success": False, "error": "The dataset could not be parsed."}), 400
    return jsonify({"success": True, "columns": [str(column) for column in frame.columns], "rows": rows}), 200


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Validates and stores a dataset privately under the authenticated account."""
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "message": "No file received"}), 400

    if not file.filename:
        return jsonify({"success": False, "message": "Please select a file"}), 400

    if not allowed_file(file.filename):
        return jsonify({
            "success": False,
            "message": "This file type is not supported."
        }), 400

    try:
        upload = upload_storage.save_dataset_upload(file, current_user()["id"])
    except ValueError as error:
        return jsonify({"success": False, "message": str(error)}), 400
    except Exception:
        app.logger.exception("Dataset upload could not be stored.")
        return jsonify({"success": False, "message": "The dataset could not be stored. Please retry."}), 500

    return jsonify({
        "success": True,
        "message": "File uploaded and saved successfully",
        **upload,
        "saved_at": datetime.now().isoformat(timespec="seconds")
    }), 201


@app.route("/api/uploads", methods=["GET"])
def list_uploads():
    """Lists the authenticated user's upload history, never directory contents."""
    uploads = database.get_user_uploads(current_user()["id"])
    files = [
        {
            "name": row["saved_name"],
            "original_name": row["original_name"],
            "size_bytes": row["file_size"],
            "modified": row["created_at"],
            "id": row["id"],
        }
        for row in uploads
    ]
    return jsonify({"success": True, "count": len(files), "files": files, "uploads": uploads})


@app.route("/api/uploads/<path:filename>", methods=["GET"])
def get_upload(filename):
    """Downloads only a file recorded as owned by the authenticated user."""
    saved_name = Path(filename).name
    upload = database.get_user_upload(current_user()["id"], saved_name)
    if not upload:
        return jsonify({"success": False, "message": "Upload not found."}), 404
    return send_from_directory(
        UPLOAD_DIR,
        upload["saved_name"],
        as_attachment=True,
        download_name=upload["original_name"],
    )


@app.route("/api/uploads/<int:upload_id>", methods=["DELETE"])
def delete_upload(upload_id):
    """Deletes an upload only after the database confirms account ownership."""
    upload = database.delete_user_upload(current_user()["id"], upload_id)
    if not upload:
        return jsonify({"success": False, "message": "Upload not found."}), 404
    path = (UPLOAD_DIR / upload["saved_name"]).resolve()
    if path.parent != UPLOAD_DIR.resolve():
        app.logger.error("Refusing to delete upload outside the configured storage directory.")
        return jsonify({"success": False, "message": "Upload storage key is invalid."}), 500
    try:
        path.unlink(missing_ok=True)
    except OSError:
        app.logger.exception("Owned upload metadata was removed but its file could not be deleted.")
        return jsonify({
            "success": False,
            "message": "Upload record was deleted, but its stored file could not be removed.",
        }), 500
    return jsonify({"success": True, "message": "Upload deleted."}), 200


# =========================================================
# SHOPPULSE REST APIS (Multi-Shop Isolation & Live Sync)
# =========================================================

def get_active_shop_context():
    """Resolves ShopPulse scope exclusively from the validated account session."""
    user_id = current_user()["id"]
    if not user_id:
        # Check Authorization or custom header
        auth_user = request.headers.get("X-User-Id")
        if auth_user and auth_user.isdigit():
            user_id = int(auth_user)

    if not user_id:
        # Fallback to demo admin user or create one
        conn = database.get_db_connection()
        row = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            user_id = row["id"]
        else:
            res = database.create_user(name="Shop Admin", email="shopowner@shoppulse.io", password="admin", phone="9876543210")
            user_id = res["user"]["id"] if res.get("user") else 1
        conn.close()

    shop = database.get_or_create_shop(user_id)
    return user_id, shop


@app.route("/api/shoppulse/shop", methods=["GET", "POST", "PUT"])
def shoppulse_shop_api():
    """Gets or updates current shop profile configuration."""
    user_id, shop = get_active_shop_context()
    if request.method in ["POST", "PUT"]:
        data = request.get_json(silent=True) or request.form.to_dict()
        shop = database.update_shop_settings(shop["id"], data)
        return jsonify({"success": True, "shop": shop, "message": "Shop profile updated successfully."})
    return jsonify({"success": True, "shop": shop})


@app.route("/api/shoppulse/dashboard", methods=["GET"])
def shoppulse_dashboard_api():
    """Computes all 12 KPI cards and chart aggregations from database records."""
    _, shop = get_active_shop_context()
    date_filter = request.args.get("filter", "this_month")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    analytics = database.get_shop_dashboard_analytics(
        shop_id=shop["id"],
        date_filter=date_filter,
        start_date=start_date,
        end_date=end_date
    )
    return jsonify({"success": True, "shop": shop, "analytics": analytics})


@app.route("/api/shoppulse/products", methods=["GET", "POST"])
def shoppulse_products_api():
    """List or create products."""
    _, shop = get_active_shop_context()
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        res = database.save_product(shop["id"], data)
        status_code = 201 if res["success"] else 400
        return jsonify(res), status_code

    search = request.args.get("search", "")
    category = request.args.get("category", "")
    stock_status = request.args.get("stock_status", "")
    products = database.get_shop_products(shop["id"], search=search, category=category, stock_status=stock_status)
    return jsonify({"success": True, "products": products, "count": len(products)})


@app.route("/api/shoppulse/products/<int:pid>", methods=["PUT", "DELETE"])
def shoppulse_single_product_api(pid):
    """Update or delete a single product."""
    _, shop = get_active_shop_context()
    if request.method == "DELETE":
        res = database.delete_product(shop["id"], pid)
        return jsonify(res)

    data = request.get_json(silent=True) or request.form.to_dict()
    data["id"] = pid
    res = database.save_product(shop["id"], data)
    return jsonify(res)


@app.route("/api/shoppulse/sales", methods=["GET", "POST"])
def shoppulse_sales_api():
    """List or create sales with multi-product items and automatic inventory deduction."""
    _, shop = get_active_shop_context()
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        res = database.create_sale(shop["id"], data)
        status_code = 201 if res["success"] else 400
        return jsonify(res), status_code

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    status = request.args.get("status")
    search = request.args.get("search")
    sales = database.get_shop_sales(shop["id"], start_date=start_date, end_date=end_date, status=status, search=search)
    return jsonify({"success": True, "sales": sales, "count": len(sales)})


@app.route("/api/shoppulse/sales/<int:sid>", methods=["DELETE"])
def shoppulse_delete_sale_api(sid):
    """Deletes a sale and restores stock."""
    _, shop = get_active_shop_context()
    res = database.cancel_or_delete_sale(shop["id"], sid, action="delete")
    return jsonify(res)


@app.route("/api/shoppulse/sales/<int:sid>/cancel", methods=["POST"])
def shoppulse_cancel_sale_api(sid):
    """Cancels a sale and restores stock."""
    _, shop = get_active_shop_context()
    res = database.cancel_or_delete_sale(shop["id"], sid, action="cancel")
    return jsonify(res)


@app.route("/api/shoppulse/customers", methods=["GET", "POST"])
def shoppulse_customers_api():
    """List or create customers."""
    _, shop = get_active_shop_context()
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        res = database.save_customer(shop["id"], data)
        status_code = 201 if res["success"] else 400
        return jsonify(res), status_code

    search = request.args.get("search", "")
    customers = database.get_shop_customers(shop["id"], search=search)
    return jsonify({"success": True, "customers": customers, "count": len(customers)})


@app.route("/api/shoppulse/customers/<int:cid>", methods=["PUT", "DELETE"])
def shoppulse_single_customer_api(cid):
    """Update or delete a customer."""
    _, shop = get_active_shop_context()
    if request.method == "DELETE":
        res = database.delete_customer(shop["id"], cid)
        return jsonify(res)

    data = request.get_json(silent=True) or request.form.to_dict()
    data["id"] = cid
    res = database.save_customer(shop["id"], data)
    return jsonify(res)


@app.route("/api/shoppulse/expenses", methods=["GET", "POST"])
def shoppulse_expenses_api():
    """List or create expenses."""
    _, shop = get_active_shop_context()
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        res = database.save_expense(shop["id"], data)
        status_code = 201 if res["success"] else 400
        return jsonify(res), status_code

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    category = request.args.get("category")
    expenses = database.get_shop_expenses(shop["id"], start_date=start_date, end_date=end_date, category=category)
    return jsonify({"success": True, "expenses": expenses, "count": len(expenses)})


@app.route("/api/shoppulse/expenses/<int:eid>", methods=["PUT", "DELETE"])
def shoppulse_single_expense_api(eid):
    """Update or delete an expense."""
    _, shop = get_active_shop_context()
    if request.method == "DELETE":
        res = database.delete_expense(shop["id"], eid)
        return jsonify(res)

    data = request.get_json(silent=True) or request.form.to_dict()
    data["id"] = eid
    res = database.save_expense(shop["id"], data)
    return jsonify(res)


@app.route("/api/shoppulse/inventory", methods=["GET"])
def shoppulse_inventory_api():
    """Gets inventory movements log."""
    _, shop = get_active_shop_context()
    pid = to_int_or_none(request.args.get("product_id"))
    movements = database.get_inventory_movements(shop["id"], product_id=pid)
    return jsonify({"success": True, "movements": movements, "count": len(movements)})


@app.route("/api/shoppulse/inventory/adjust", methods=["POST"])
def shoppulse_adjust_inventory_api():
    """Manually adjusts product inventory."""
    _, shop = get_active_shop_context()
    data = request.get_json(silent=True) or request.form.to_dict()
    pid = to_int_or_none(data.get("product_id"))
    if pid is None:
        return jsonify({"success": False, "message": "A valid product_id is required."}), 400
    mtype = data.get("movement_type", "Restock")
    qty = int(data.get("quantity", 0))
    reason = data.get("reason", "")

    res = database.adjust_inventory(shop["id"], pid, mtype, qty, reason)
    return jsonify(res)


@app.route("/api/shoppulse/ai-assistant", methods=["POST"])
def shoppulse_ai_assistant_api():
    """Answers business questions using verified database math."""
    _, shop = get_active_shop_context()
    data = request.get_json(silent=True) or request.form.to_dict()
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"success": False, "message": "Question is required."}), 400

    reply = database.query_ai_assistant(shop["id"], question)
    return jsonify({"success": True, **reply})


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def file_too_large(error):
    return jsonify({
        "success": False,
        "message": "File is too large. Maximum size is 50 MB."
    }), 413


@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "success": False,
        "message": "Endpoint not found"
    }), 404


@app.errorhandler(Exception)
def handle_any_error(e: Exception) -> Response | tuple[Response, int]:
    """
    Any error inside /api/* always returns JSON (never an HTML page),
    so the browser shows the real message instead of "Unexpected token '<'".
    The full traceback is still printed in this terminal.
    """
    if request.path.startswith("/api/"):
        if isinstance(e, HTTPException):
            return jsonify(success=False, message=e.description), e.code or 500
        app.logger.exception(e)
        return jsonify(success=False, message=f"Server error: {e}"), 500

    # Non-API pages: keep default Flask behaviour
    if isinstance(e, HTTPException):
        return app.make_response(e.get_response())
    raise e


if __name__ == "__main__":
    port = int(os.getenv("GW_FLASK_PORT", "5000"))
    print("\n========================================================")
    print(f"   GW Smart Analytics Server running at http://127.0.0.1:{port}")
    print(f"   - Home Page:       http://127.0.0.1:{port}/")
    print(f"   - Generator:       http://127.0.0.1:{port}/h")
    print(f"   - Smart Analytics: http://127.0.0.1:{port}/up")
    print(f"   - Register:        http://127.0.0.1:{port}/register")
    print(f"   - Login:           http://127.0.0.1:{port}/login")
    print("   - Receiving UPI:   9529579858-d3cb-2@ybl")
    print("   - Scanner Image:   scanner.png / scaner.png")
    print(f"   - SQLite Database: {database.DB_PATH.name}")
    print("========================================================\n")
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )