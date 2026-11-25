# ============================================================
# GW SMART ANALYTICS  -  hh.py   (run with:  streamlit run hh.py)
#   PART 1 : Flask API (login / register / upload / contact)  - starts FIRST
#   PART 2 : Streamlit Sales Intelligence Dashboard
# ============================================================
import os
import re
import time
import base64
import secrets
import smtplib
import threading
import math
import html as _html
import json as _json
import sys
from io import BytesIO
from pathlib import Path
from datetime import datetime
from functools import wraps
from email.message import EmailMessage
from typing import Any, Dict, Optional

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from sklearn.ensemble import GradientBoostingRegressor, IsolationForest, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.seasonal import STL
except ImportError:
    ExponentialSmoothing = None
    SARIMAX = None
    STL = None

import matplotlib
matplotlib.use("Agg")  # Non-GUI backend for reliable PDF chart rendering
import matplotlib.pyplot as plt
from PIL import Image as PILImage

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
)

from flask import Flask, request, jsonify, session, send_from_directory, redirect, Response, g
from dotenv import load_dotenv

import mailer
import database
import upload_storage
from auth_widget import _CURRENT_ACCOUNT_WIDGET

BASE_DIR = APP_DIR
load_dotenv(APP_DIR / ".env")

# ------------------------------------------------------------
# CONFIG (all overridable from .env -> easy to deploy later)
# ------------------------------------------------------------
# Keep the legacy Streamlit API separate from the main payment API in server.py.
GW_STREAMLIT_API_PORT = int(os.getenv("GW_STREAMLIT_API_PORT", "5001"))
# 127.0.0.1 = only this PC. Set GW_FLASK_HOST=0.0.0.0 in .env to open the site on phone / LAN.
GW_FLASK_HOST = os.getenv("GW_FLASK_HOST", "127.0.0.1")
GW_BASE_URL = os.getenv("GW_BASE_URL", f"http://127.0.0.1:{GW_STREAMLIT_API_PORT}").rstrip("/")
GW_STREAMLIT_URL = os.getenv("GW_STREAMLIT_URL", "http://127.0.0.1:8501").rstrip("/")
GW_HOME_URL = f"{GW_BASE_URL}/index.html"
GW_UPLOAD_DIR = database.UPLOAD_DIR
GW_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
GW_MAX_UPLOAD_MB = int(os.getenv("GW_MAX_UPLOAD_MB", "25"))
GW_COOKIE_SECURE = os.getenv("GW_COOKIE_SECURE", "0") == "1"   # set 1 when served over HTTPS

# SECURITY: never ship a hardcoded secret. Set FLASK_SECRET_KEY in your .env file.
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("FLASK_SECRET_KEY"):
    print("[GW] WARNING: FLASK_SECRET_KEY is not set in .env - using a temporary random key.")

# SECURITY: only these origins may call the API with cookies
GW_ALLOWED_ORIGINS = {
    o.strip() for o in os.getenv(
        "GW_ALLOWED_ORIGINS",
        "http://127.0.0.1:5000,http://localhost:5000,http://127.0.0.1:8501,http://localhost:8501"
    ).split(",") if o.strip()
}

# Gmail/SMTP settings for registration welcome emails.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "GW Smart Analytics")

# Floating assistant widget (gw_assistant.py must be in the same folder)
_assistant_err = ""
try:
    from gw_assistant import render_gw_assistant
except Exception as _imp_err:
    render_gw_assistant = None
    _assistant_err = repr(_imp_err)


# ============================================================
# PAGE CONFIG  (must be the first Streamlit command)
# ============================================================
def _load_page_icon():
    try:
        p = APP_DIR / "logo.png"
        return PILImage.open(p) if p.exists() else "📊"
    except Exception:
        return "📊"


st.set_page_config(
    page_title="Sales Intelligence Dashboard",
    page_icon=_load_page_icon(),
    layout="wide",
    initial_sidebar_state="expanded"
)


@st.cache_resource(show_spinner=False)
def _init_database():
    """Create DB tables ONCE, before anything tries to read users."""
    database.init_db()
    return True


_init_database()


# ============================================================
# PART 1 :  FLASK API  (login / register / upload / contact)
# Started here (top of file) so it still runs even when the
# dashboard calls st.stop() (empty filters, bad upload, etc.)
# ============================================================
# SECURITY FIX: the old code used static_folder=BASE_DIR with static_url_path="",
# which exposed EVERY file (.env, database, hh.py source) over HTTP.
# Now static serving is disabled and only a safe whitelist is served (see route below).
gw_api = Flask("gw_secure_api", static_folder=None)
gw_api.secret_key = FLASK_SECRET_KEY
gw_api.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=GW_COOKIE_SECURE,
    MAX_CONTENT_LENGTH=GW_MAX_UPLOAD_MB * 1024 * 1024,
)


def inject_current_account_widget(response):
    if response.mimetype != "text/html":
        return response

    response.direct_passthrough = False
    page = response.get_data(as_text=True)
    if "data-gw-current-account-widget" in page:
        return response

    position = page.lower().rfind("</body>")
    if position >= 0:
        page = page[:position] + _CURRENT_ACCOUNT_WIDGET + page[position:]
    else:
        page += _CURRENT_ACCOUNT_WIDGET
    response.set_data(page)
    return response


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# --- simple in-memory brute-force protection for login ---
_LOGIN_FAILS = {}
_LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_SEC = 300


def _login_key(email):
    return f"{request.remote_addr}|{email}"


def _login_blocked(key):
    now = time.time()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW_SEC]
        _LOGIN_FAILS[key] = fails
        return len(fails) >= LOGIN_MAX_FAILS


def _login_fail(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(key, []).append(time.time())


def _login_clear(key):
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(key, None)


@gw_api.after_request
def gw_cors_headers(response):
    response = inject_current_account_widget(response)
    origin = request.headers.get("Origin")
    if origin and origin in GW_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,DELETE,OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@gw_api.before_request
def gw_authenticate_api_request():
    public_paths = {
        "/api/health",
        "/api/register",
        "/api/login",
        "/api/logout",
        "/api/me",
        "/api/contact",
    }
    if request.path.startswith("/api/") and request.path not in public_paths:
        user = database.validate_web_session(request.cookies.get("gw_web_session", ""))
        if not user:
            return jsonify({"success": False, "authenticated": False, "error": "Login required."}), 401
        g.current_user = user


@gw_api.errorhandler(413)
def gw_too_large(_e):
    return jsonify({"success": False, "error": f"File too large (max {GW_MAX_UPLOAD_MB} MB)."}), 413


def gw_current_user() -> Optional[Dict[str, Any]]:
    user = getattr(g, "current_user", None)
    return user or database.validate_web_session(request.cookies.get("gw_web_session", ""))


def gw_authenticated_user() -> Dict[str, Any]:
    user = gw_current_user()
    if user is None:
        raise RuntimeError("The route requires a validated account session.")
    return user


def gw_require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not gw_current_user():
            return jsonify({"error": "Login required", "authenticated": False}), 401
        return fn(*args, **kwargs)
    return wrapper


def send_welcome_email(name, email):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        return False, "SMTP is not configured"
    msg = EmailMessage()
    msg["Subject"] = "Welcome to GW Smart Analytics 🎉"
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USERNAME}>"
    msg["To"] = email
    safe_name = name or "User"
    msg.set_content(f"""Hello {safe_name},

Welcome to GW Smart Analytics!

Your registration has been successfully completed.
Your account is now ready to use.

Thank you for registering with GW Smart Analytics.

Regards,
GW Smart Analytics Team
""")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, str(e)


# ---------------- API ENDPOINTS ----------------
@gw_api.route("/api/health", methods=["GET"])
def gw_health():
    return jsonify({
        "success": True, "status": "online",
        "service": "GW Smart Analytics API", "free_access": True
    })


@gw_api.route("/api/register", methods=["POST"])
def gw_register():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("phone", data.get("mobile", ""))).strip()
    password = str(data.get("password", ""))

    if not name or not email or not password:
        return jsonify({"success": False, "message": "Name, email and password are required."}), 400
    if len(name) > 120:
        return jsonify({"success": False, "message": "Name is too long (max 120 characters)."}), 400
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return jsonify({"success": False, "message": "Please enter a valid email address."}), 400
    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters."}), 400

    result = database.create_user(name=name, email=email, password=password, phone=phone)
    if not result.get("success"):
        return jsonify(result), 400

    # Welcome email in background so registration never waits on SMTP
    threading.Thread(target=send_welcome_email, args=(name, email), daemon=True).start()

    return jsonify({
        "success": True,
        "message": "Registration successful! You can now login.",
        "user": result["user"]
    }), 201


@gw_api.route("/api/login", methods=["POST"])
def gw_login():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required."}), 400

    key = _login_key(email)
    if _login_blocked(key):
        return jsonify({
            "success": False,
            "message": "Too many failed attempts. Please try again after 5 minutes."
        }), 429

    result = database.authenticate_user(email=email, password=password)
    if not result.get("success"):
        _login_fail(key)
        return jsonify({"success": False, "message": result.get("message", "Invalid email or password.")}), 401
    _login_clear(key)

    user = result["user"]
    session.clear()

    web_session = database.create_web_session(user["id"])
    bridge_token = database.create_session_bridge_token(
        user["id"], web_session_id=web_session["session_id"]
    )

    resp = jsonify({
        "success": True,
        "message": "Login successful!",
        "user": {
            "id": user["id"], "name": user["name"], "email": user["email"],
            "mobile": user.get("mobile") or user.get("phone") or ""
        },
        "subscription": result.get("subscription"),
        "redirect": "/index.html"
    })
    resp.set_cookie(
        "gw_web_session",
        web_session["token"],
        max_age=database.WEB_SESSION_IDLE_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=GW_COOKIE_SECURE,
        path="/",
    )
    resp.set_cookie(
        "gw_session_token",
        bridge_token,
        max_age=database.WEB_SESSION_IDLE_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=GW_COOKIE_SECURE,
        path="/",
    )
    return resp, 200


@gw_api.route("/api/me", methods=["GET"])
def gw_me():
    user = gw_current_user()
    if not user:
        token = request.cookies.get("gw_web_session", "")
        return jsonify({"authenticated": False}), 401 if token else 200
    phone_val = user.get("mobile") or user.get("phone") or ""
    return jsonify({
        "authenticated": True,
        "user": {"id": user["id"], "name": user["name"], "email": user["email"], "mobile": phone_val}
    }), 200


@gw_api.route("/api/logout", methods=["POST", "GET"])
def gw_logout():
    database.revoke_web_session(request.cookies.get("gw_web_session", ""))
    session.clear()
    if request.method == "GET":
        resp = redirect("/login.html")
    else:
        resp = jsonify({"success": True, "message": "Logged out successfully."})
    resp.delete_cookie("gw_session_token")
    resp.delete_cookie("gw_web_session", path="/", httponly=True,
                       secure=GW_COOKIE_SECURE, samesite="Lax")
    return resp


@gw_api.route("/api/upload", methods=["POST"])
@gw_require_login
def gw_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file selected"}), 400

    try:
        upload = upload_storage.save_dataset_upload(file, gw_authenticated_user()["id"])
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    except Exception:
        gw_api.logger.exception("Dataset upload could not be stored.")
        return jsonify({"success": False, "error": "The dataset could not be stored. Please retry."}), 500
    return jsonify({"success": True, **upload}), 201


@gw_api.route("/api/parse-dataset", methods=["POST"])
@gw_require_login
def gw_parse_dataset():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file selected."}), 400
    raw = file.read()
    max_size = GW_MAX_UPLOAD_MB * 1024 * 1024
    if not raw:
        return jsonify({"success": False, "error": "The uploaded file is empty."}), 400
    if len(raw) > max_size:
        return jsonify({"success": False, "error": f"File too large (max {GW_MAX_UPLOAD_MB} MB)."}), 413
    try:
        options = upload_storage.dataset_options(file.filename, raw)
        selection = request.form.get("selection")
        if options and not selection and len(options) > 1:
            return jsonify({"success": True, "requires_selection": True, "options": options}), 200
        frame = upload_storage.read_dataset(file.filename, raw, selection)
        rows = _json.loads(frame.to_json(orient="records", date_format="iso", default_handler=str))
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    except Exception:
        gw_api.logger.exception("Dataset parsing failed.")
        return jsonify({"success": False, "error": "The dataset could not be parsed."}), 400
    return jsonify({"success": True, "columns": [str(column) for column in frame.columns], "rows": rows}), 200


@gw_api.route("/api/uploads", methods=["GET"])
@gw_require_login
def gw_list_uploads():
    uploads = database.get_user_uploads(gw_authenticated_user()["id"])
    return jsonify({"success": True, "uploads": uploads, "count": len(uploads)}), 200


@gw_api.route("/api/uploads/<path:filename>", methods=["GET"])
@gw_require_login
def gw_download_upload(filename):
    upload = database.get_user_upload(gw_authenticated_user()["id"], Path(filename).name)
    if not upload:
        return jsonify({"success": False, "error": "Upload not found."}), 404
    return send_from_directory(
        GW_UPLOAD_DIR,
        upload["saved_name"],
        as_attachment=True,
        download_name=upload["original_name"],
    )


@gw_api.route("/api/uploads/<int:upload_id>", methods=["DELETE"])
@gw_require_login
def gw_delete_upload(upload_id):
    upload = database.delete_user_upload(gw_authenticated_user()["id"], upload_id)
    if not upload:
        return jsonify({"success": False, "error": "Upload not found."}), 404
    path = (GW_UPLOAD_DIR / upload["saved_name"]).resolve()
    if path.parent != GW_UPLOAD_DIR.resolve():
        return jsonify({"success": False, "error": "Upload storage key is invalid."}), 500
    try:
        path.unlink(missing_ok=True)
    except OSError:
        gw_api.logger.exception("Owned upload metadata was removed but its file could not be deleted.")
        return jsonify({"success": False, "error": "Upload record was deleted but the stored file could not be removed."}), 500
    return jsonify({"success": True}), 200


@gw_api.route("/api/contact", methods=["POST"])
def gw_contact_message():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    sender_email = str(data.get("email", "")).strip()
    subject = str(data.get("subject", "")).strip()
    message = str(data.get("message", "")).strip()

    if not name or len(name) > 120 or any(char in name for char in "\r\n"):
        return jsonify({"success": False, "message": "Please enter a valid name (up to 120 characters)."}), 400
    if len(sender_email) > 254 or not EMAIL_RE.fullmatch(sender_email):
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
        gw_api.logger.error("Contact message email could not be sent: %s", error)
        return jsonify({
            "success": False,
            "message": "Email sending is not ready. Copy .env.example to .env, set your Gmail App Password, then restart the server. Or email gwsmartanalytics0810@gmail.com directly."
        }), 503

    return jsonify({"success": True, "message": "Your message was sent to GW Smart Analytics support."}), 200


# ---------------- FRONTEND PAGE ROUTING ----------------
@gw_api.route("/register.html")
@gw_api.route("/register")
def gw_register_page():
    return send_from_directory(str(BASE_DIR), "register.html")


# Injected into login.html: when /api/login succeeds, return to the home page.
GW_LOGIN_REDIRECT_JS = """<script>
(function(){
  var _fetch = window.fetch;
  if(!_fetch) return;
  window.fetch = function(){
    var a = arguments;
    var url = String((a[0] && a[0].url) || a[0] || "");
    return _fetch.apply(this, a).then(function(res){
      if(url.indexOf("/api/login") !== -1 && res.ok){
        return res.clone().json().then(function(d){
          if(d && d.success && d.redirect){
            window.location.href = d.redirect;
            return new Promise(function(){});
          }
          return res;
        }).catch(function(){ return res; });
      }
      return res;
    });
  };
})();
</script>
"""


@gw_api.route("/login.html")
@gw_api.route("/login")
def gw_login_page():
    try:
        page = (BASE_DIR / "login.html").read_text(encoding="utf-8")
    except Exception:
        return send_from_directory(str(BASE_DIR), "login.html")
    if re.search(r"</head>", page, re.I):
        page = re.sub(r"</head>", lambda m: GW_LOGIN_REDIRECT_JS + m.group(0), page, count=1, flags=re.I)
    else:
        page = GW_LOGIN_REDIRECT_JS + page
    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@gw_api.route("/index.html")
def gw_index_page():
    """Serve index.html and tell it who is logged in (from the Flask session)."""
    try:
        page = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    except Exception:
        return send_from_directory(str(BASE_DIR), "index.html")

    user = gw_current_user()
    if user:
        payload = {
            "name": _html.escape(str(user.get("name") or "")),
            "email": _html.escape(str(user.get("email") or "")),
        }
        payload_js = _json.dumps(payload).replace("<", "\\u003c")
        inject = f"<script>try{{localStorage.setItem('gw_user', {_json.dumps(payload_js)});}}catch(e){{}}</script>\n"
    else:
        inject = "<script>try{localStorage.removeItem('gw_user');}catch(e){}</script>\n"

    if re.search(r"</head>", page, re.I):
        page = re.sub(r"</head>", lambda m: inject + m.group(0), page, count=1, flags=re.I)
    else:
        page = inject + page

    resp = Response(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@gw_api.route("/about.html")
@gw_api.route("/about")
def gw_about_page():
    return send_from_directory(str(BASE_DIR), "about.html")


@gw_api.route("/contact.html")
@gw_api.route("/contact")
def gw_contact_page():
    return send_from_directory(str(BASE_DIR), "contact.html")


@gw_api.route("/Features.html")
@gw_api.route("/features")
def gw_features_page():
    return send_from_directory(str(BASE_DIR), "Features.html")


@gw_api.route("/howtowoek.html")
@gw_api.route("/howtowork")
def gw_howtowoek_page():
    return send_from_directory(str(BASE_DIR), "howtowoek.html")


@gw_api.route("/logo.png")
def gw_logo():
    return send_from_directory(str(BASE_DIR), "logo.png")


@gw_api.route("/assistant-logo.png")
def gw_assistant_logo():
    preferred = BASE_DIR / "clean_high_resolution_modern_ui_style_icon_brand.png"
    if preferred.exists():
        return send_from_directory(str(BASE_DIR), preferred.name)
    return send_from_directory(str(BASE_DIR), "logo.png")


@gw_api.route("/scanner.png")
@gw_api.route("/scaner.png")
def gw_scanner():
    target = "scanner.png" if (BASE_DIR / "scanner.png").exists() else "scaner.png"
    return send_from_directory(str(BASE_DIR), target)


# Safe whitelist static server (css/js/images/fonts for your html pages)
GW_STATIC_EXT = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".webmanifest",
}
GW_BLOCKED_TOP_DIRS = {"uploads", "__pycache__", "venv", ".venv", "env", "node_modules"}


@gw_api.route("/<path:filename>")
def gw_public_static(filename):
    p = Path(filename)
    parts = p.parts
    if not parts or any(part.startswith(".") for part in parts) or parts[0].lower() in GW_BLOCKED_TOP_DIRS:
        return jsonify({"error": "Not found"}), 404
    ext = p.suffix.lower()
    allowed = ext in GW_STATIC_EXT or (ext == ".html" and len(parts) == 1)
    if not allowed:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(str(BASE_DIR), filename)


@gw_api.route("/dashboard")
def gw_dashboard_page():
    user = gw_current_user()
    if not user:
        return redirect("/login.html")
    bridge_token = database.create_session_bridge_token(
        user["id"], web_session_id=user.get("web_session_id")
    )
    return redirect(f"{GW_STREAMLIT_URL}/?gw_auth_token={bridge_token}")


@gw_api.route("/")
def gw_root():
    user = gw_current_user()
    if user:
        return redirect("/dashboard")
    return redirect("/login.html")


def gw_start_flask_once():
    if os.environ.get("GW_FLASK_THREAD_STARTED") == "1":
        return
    os.environ["GW_FLASK_THREAD_STARTED"] = "1"

    def runner():
        gw_api.run(host=GW_FLASK_HOST, port=GW_STREAMLIT_API_PORT, debug=False, use_reloader=False, threaded=True)

    threading.Thread(target=runner, daemon=True).start()


gw_start_flask_once()


# ============================================================
# PART 2 :  STREAMLIT DASHBOARD
# ============================================================
# ---------- helpers: logos as base64 ----------
def img_to_data_uri(*names, size=None):
    """Return a data: URI for the first image that exists in the project folder."""
    for name in names:
        p = APP_DIR / name
        if p.exists():
            try:
                if size:
                    im = PILImage.open(p).convert("RGBA")
                    im.thumbnail((size, size))
                    b = BytesIO()
                    im.save(b, format="PNG")
                    raw = b.getvalue()
                else:
                    raw = p.read_bytes()
                return "data:image/png;base64," + base64.b64encode(raw).decode()
            except Exception:
                continue
    return ""


@st.cache_data(show_spinner=False)
def _logo_uris():
    return (
        img_to_data_uri("logo.png", size=128),
        img_to_data_uri("clean_high_resolution_modern_ui_style_icon_brand.png", "logo.png", size=128),
    )


LOGO_URI, ASSISTANT_LOGO_URI = _logo_uris()


def md_html(html_str):
    """Render HTML through st.markdown safely (strip indentation + blank lines)."""
    cleaned = "\n".join(line.strip() for line in html_str.splitlines() if line.strip())
    st.markdown(cleaned, unsafe_allow_html=True)


# ---------- PDF fonts (Helvetica has no ₹ glyph -> DejaVu Sans from matplotlib) ----------
def _register_pdf_fonts():
    try:
        ttf_dir = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        reg = os.path.join(ttf_dir, "DejaVuSans.ttf")
        bold = os.path.join(ttf_dir, "DejaVuSans-Bold.ttf")
        obl = os.path.join(ttf_dir, "DejaVuSans-Oblique.ttf")
        if not (os.path.exists(reg) and os.path.exists(bold)):
            return "Helvetica", "Helvetica-Bold"
        pdfmetrics.registerFont(TTFont("DejaVuSans", reg))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bold))
        if os.path.exists(obl):
            pdfmetrics.registerFont(TTFont("DejaVuSans-Oblique", obl))
        pdfmetrics.registerFontFamily(
            "DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold",
            italic="DejaVuSans-Oblique" if os.path.exists(obl) else "DejaVuSans",
            boldItalic="DejaVuSans-Bold"
        )
        return "DejaVuSans", "DejaVuSans-Bold"
    except Exception:
        return "Helvetica", "Helvetica-Bold"


PDF_FONT, PDF_FONT_BOLD = _register_pdf_fonts()

# Everything is FREE: all reports and PDF downloads are unlocked for everyone.
st.session_state["payment_verified"] = True

# ============================================================
# AUTH BRIDGE (FLASK <-> STREAMLIT)
# ============================================================
gw_token = None
try:
    if hasattr(st, "query_params"):
        gw_token = st.query_params.get("gw_auth_token") or st.query_params.get("token")
    else:
        gw_token = st.experimental_get_query_params().get("gw_auth_token", [None])[0]
except Exception:
    pass

if gw_token:
    bridge_user = database.validate_session_bridge_token(gw_token)
    if bridge_user:
        st.session_state["gw_user_id"] = bridge_user["id"]
        st.session_state["gw_bridge_token"] = gw_token
        try:
            if hasattr(st, "query_params"):
                for _k in ("gw_auth_token", "token"):
                    if _k in st.query_params:
                        del st.query_params[_k]
        except Exception:
            pass

# Refresh identity from the primary web session first; the bridge token is a fallback.
try:
    cookies = getattr(st.context, "cookies", {})
    web_session_token = cookies.get("gw_web_session")
    web_session_user = database.validate_web_session(web_session_token) if web_session_token else None

    cookie_token = cookies.get("gw_session_token") or st.session_state.get("gw_bridge_token")
    bridge_user = database.validate_session_bridge_token(cookie_token) if cookie_token else None

    if web_session_user:
        st.session_state["gw_user_id"] = web_session_user["id"]
        st.session_state.pop("gw_bridge_token", None)
    elif bridge_user:
        st.session_state["gw_user_id"] = bridge_user["id"]
        if cookie_token:
            st.session_state["gw_bridge_token"] = cookie_token
    else:
        st.session_state.pop("gw_user_id", None)
        st.session_state.pop("gw_bridge_token", None)
except Exception as auth_error:
    print(f"[GW] account verification failed: {auth_error!r}")   # keep internals out of the UI
    st.sidebar.warning("Could not verify the current account. Please log in again.")

current_uid = st.session_state.get("gw_user_id")
authenticated_user = database.get_user_by_id(current_uid) if current_uid else None

if authenticated_user:
    st.sidebar.markdown("### 👤 Current Account")
    with st.sidebar:
        st.write(authenticated_user["name"])
        st.caption(f"✉️ {authenticated_user['email']}")
        st.caption(f"🆔 User ID: {authenticated_user['id']}")
        st.caption("✅ Free Access — all features unlocked")

# ============================================================
# THEME ENGINE
# ============================================================
st.sidebar.markdown("### 🎨 Dashboard Themes")
theme = st.sidebar.selectbox(
    "Select Color Theme",
    ["🌌 Midnight Blue", "☀️ Crisp White", "🌲 Emerald Forest", "🔮 Royal Purple"],
    index=0
)

st.sidebar.markdown("### 📄 Report Settings")
report_type = st.sidebar.selectbox(
    "Report Type",
    ["Executive Overview", "Financial Summary", "Operational Review", "Customer & Growth", "Custom Analytics"],
    index=0,
)

THEMES = {
    "🌌 Midnight Blue": {
        "page": "#0b1329", "panel": "#131f37", "panel2": "#1a2b4c", "text": "#f8fafc",
        "muted": "#94a3b8", "border": "rgba(255,255,255,0.14)", "grid": "rgba(255,255,255,0.08)",
        "mpl_border": "#334155", "mpl_grid": "#1e293b", "accent": "#38bdf8",
        "accent_grad": "linear-gradient(135deg, #38bdf8, #818cf8)",
        "btn_bg": "linear-gradient(135deg, #0284c7, #6366f1)", "btn_text": "#ffffff",
        "kpi_bg": "#162238", "plotly_template": "plotly_dark"
    },
    "☀️ Crisp White": {
        "page": "#f8fafc", "panel": "#ffffff", "panel2": "#f1f5f9", "text": "#0f172a",
        "muted": "#64748b", "border": "rgba(0,0,0,0.12)", "grid": "rgba(0,0,0,0.06)",
        "mpl_border": "#cbd5e1", "mpl_grid": "#e2e8f0", "accent": "#0b3d91",
        "accent_grad": "linear-gradient(135deg, #0b3d91, #2563eb)",
        "btn_bg": "linear-gradient(135deg, #0b3d91, #1d4ed8)", "btn_text": "#ffffff",
        "kpi_bg": "#ffffff", "plotly_template": "plotly_white"
    },
    "🌲 Emerald Forest": {
        "page": "#06281e", "panel": "#0a382a", "panel2": "#104c3a", "text": "#f0fdf4",
        "muted": "#a7f3d0", "border": "rgba(52,211,153,0.22)", "grid": "rgba(255,255,255,0.08)",
        "mpl_border": "#064e3b", "mpl_grid": "#0f3e30", "accent": "#34d399",
        "accent_grad": "linear-gradient(135deg, #34d399, #059669)",
        "btn_bg": "linear-gradient(135deg, #059669, #047857)", "btn_text": "#ffffff",
        "kpi_bg": "#0a382a", "plotly_template": "plotly_dark"
    },
    "🔮 Royal Purple": {
        "page": "#130924", "panel": "#1f1038", "panel2": "#2b174d", "text": "#faf5ff",
        "muted": "#d8b4fe", "border": "rgba(192,132,252,0.22)", "grid": "rgba(255,255,255,0.08)",
        "mpl_border": "#3b1764", "mpl_grid": "#260e42", "accent": "#c084fc",
        "accent_grad": "linear-gradient(135deg, #c084fc, #9333ea)",
        "btn_bg": "linear-gradient(135deg, #9333ea, #7e22ce)", "btn_text": "#ffffff",
        "kpi_bg": "#1f1038", "plotly_template": "plotly_dark"
    }
}
T = THEMES[theme]

st.markdown(f"""
<style>
    .block-container {{ padding-top: 1.2rem; max-width: 96%; }}
    header[data-testid="stHeader"] {{
        background: {T['panel']} !important;
        border-bottom: 1px solid {T['border']} !important;
        height: 64px !important;
    }}
    html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {{
        background-color: {T['page']} !important;
        color: {T['text']} !important;
    }}
    .dashboard-title {{ font-size: 32px; font-weight: 800; color: {T['text']}; margin-bottom: 4px; letter-spacing: -0.5px; }}
    .dashboard-subtitle {{ font-size: 15px; color: {T['muted']} !important; margin-bottom: 22px; }}
    h1, h2, h3, h4, h5, h6, p, span, label {{ color: {T['text']} !important; }}
    div[data-testid="stMetric"] {{
        background: {T['kpi_bg']} !important; border: 1px solid {T['border']} !important;
        border-radius: 16px !important; padding: 14px 18px !important; box-shadow: 0 8px 24px rgba(0,0,0,0.15);
    }}
    div[data-testid="stMetric"] label {{
        color: {T['muted']} !important; font-weight: 700 !important; font-size: 12px !important;
        text-transform: uppercase; letter-spacing: 0.5px;
    }}
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {{
        color: {T['text']} !important; font-size: 28px !important; font-weight: 800 !important;
    }}
    .stTabs [data-baseweb="tab-list"] {{
        background: {T['panel']} !important; border-radius: 14px; padding: 6px;
        border: 1px solid {T['border']}; gap: 6px; flex-wrap: wrap;
    }}
    .stTabs [data-baseweb="tab"] {{
        color: {T['muted']} !important; border-radius: 10px; padding: 8px 16px; font-weight: 700; font-size: 13px;
    }}
    .stTabs [aria-selected="true"] {{
        color: {T['text']} !important; background: {T['panel2']} !important; box-shadow: 0 4px 12px rgba(0,0,0,0.1);
    }}
    section[data-testid="stSidebar"] {{
        background-color: {T['panel']} !important; border-right: 1px solid {T['border']} !important;
    }}
    .stButton button {{
        background: {T['btn_bg']} !important; color: {T['btn_text']} !important; border: none !important;
        border-radius: 10px !important; padding: 10px 20px !important; font-weight: 700 !important;
        transition: all 0.2s ease !important; box-shadow: 0 4px 14px rgba(0,0,0,0.2) !important;
    }}
    .stButton button p {{ color: {T['btn_text']} !important; }}
    .stButton button:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.3) !important; }}
    [data-testid="stDownloadButton"] button {{
        background: {T['btn_bg']} !important; color: {T['btn_text']} !important;
        border: 1px solid {T['border']} !important; border-radius: 10px !important; font-weight: 700 !important;
    }}
    [data-testid="stDownloadButton"] button p,
    [data-testid="stDownloadButton"] button span {{ color: {T['btn_text']} !important; }}
    div[data-testid="stDataFrame"], div[data-testid="stExpander"] {{
        border: 1px solid {T['border']} !important; border-radius: 14px !important; background: {T['panel']} !important;
    }}
    input, select, textarea, [data-baseweb="select"] > div {{
        background-color: {T['panel2']} !important; color: {T['text']} !important;
        border: 1px solid {T['border']} !important; border-radius: 10px !important;
    }}
    [data-testid="stFileUploaderDropzone"] {{
        background-color: {T['panel2']} !important; border: 1.5px dashed {T['border']} !important; border-radius: 12px !important;
    }}
    [data-testid="stFileUploaderDropzoneInstructions"] span,
    [data-testid="stFileUploaderDropzoneInstructions"] small,
    [data-testid="stFileUploaderDropzoneInstructions"] div {{ color: {T['text']} !important; }}
    [data-testid="stFileUploaderFile"] {{ background-color: {T['panel2']} !important; border-radius: 10px !important; }}
    [data-testid="stDateInput"] > div > div {{
        background-color: {T['panel2']} !important; border: 1px solid {T['border']} !important; border-radius: 10px !important;
    }}
</style>
""", unsafe_allow_html=True)

# ---------- GW brand logo (base64, always visible) ----------
_logo_img = f'<img src="{LOGO_URI}" alt="GW">' if LOGO_URI else ""
st.markdown(
    f"""<style>
.gw-site-brand {{ position: fixed; top: 9px; left: 18px; z-index: 1000002; height: 46px;
display: flex; align-items: center; gap: 10px; pointer-events: none; }}
.gw-site-brand img {{ width: 42px; height: 42px; object-fit: contain; border-radius: 10px;
filter: drop-shadow(0 3px 8px rgba(0,0,0,.28)); }}
.gw-site-brand .gw-brand-name {{ color: {T['text']} !important; font-size: 16px; font-weight: 800;
letter-spacing: .2px; white-space: nowrap; }}
header[data-testid="stHeader"]::after {{ content: none !important; }}
@media (max-width: 700px) {{ .gw-site-brand .gw-brand-name {{ display: none; }} .gw-site-brand {{ left: 10px; }} }}
</style>
<div class="gw-site-brand">{_logo_img}<span class="gw-brand-name">GW Smart Analytics</span></div>""",
    unsafe_allow_html=True,
)


def style_chart(fig):
    fig.update_layout(
        template=T["plotly_template"],
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=T["text"], family="Segoe UI, Arial, sans-serif"),
        title=dict(font=dict(size=16, color=T["text"])),
        margin=dict(l=20, r=20, t=50, b=25),
        hoverlabel=dict(bgcolor=T["panel2"], font_color=T["text"]),
        xaxis=dict(showgrid=True, gridcolor=T["grid"], zeroline=False),
        yaxis=dict(showgrid=True, gridcolor=T["grid"], zeroline=False),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=T["text"])),
    )
    return fig


# ---------- Back to Home + Login/Logout bar ----------
def _resolve_home_url():
    """Use the same host the user opened the dashboard with (works on phone/LAN too)."""
    if os.getenv("GW_BASE_URL"):          # .env madhe set asel tar tech vapar
        return GW_HOME_URL
    try:
        headers = st.context.headers
        host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").split(":")[0]
        scheme = headers.get("X-Forwarded-Proto", "http")
        if host:
            return f"{scheme}://{host}:{GW_STREAMLIT_API_PORT}/index.html"
    except Exception:
        pass
    return GW_HOME_URL


HOME_URL = _resolve_home_url()

st.markdown(f"""<style>
.home-btn {{
position: fixed; top: 14px; right: 150px; z-index: 2147483647; display: inline-block;
background: {T['btn_bg']}; color: {T['btn_text']} !important; padding: 10px 18px;
border-radius: 10px; font-size: 13px; font-weight: 700; line-height: 1.2;
text-decoration: none !important; border: 1px solid {T['border']};
box-shadow: 0 4px 14px rgba(0,0,0,0.25); transition: box-shadow 0.2s ease, filter 0.2s ease;
pointer-events: auto !important; cursor: pointer;
touch-action: manipulation; -webkit-tap-highlight-color: transparent;
}}
.home-btn:hover, .home-btn:active {{ filter: brightness(1.12); box-shadow: 0 6px 20px rgba(0,0,0,0.35); color: {T['btn_text']} !important; }}
@media (max-width: 700px) {{ .home-btn {{ right: 110px; padding: 8px 12px; font-size: 11px; top: 14px; }} }}
</style>
<a href="{HOME_URL}" target="_self" rel="noopener" class="home-btn">🏠 Back to Home</a>""", unsafe_allow_html=True)

_profile_name = _html.escape(
    str((authenticated_user or {}).get("name") or (authenticated_user or {}).get("email") or "Account"),
    quote=True,
)
_profile_email = _html.escape(str((authenticated_user or {}).get("email") or ""), quote=True)
_api_logout_url = _json.dumps(f"{GW_BASE_URL}/api/logout")
_home_url_json = _json.dumps(HOME_URL)
if authenticated_user:
    _auth_bar = f"""
    <div class="gw-profile" id="gw-profile">
      <button type="button" class="gw-profile-toggle" id="gw-profile-toggle"
        aria-haspopup="menu" aria-expanded="false" aria-controls="gw-profile-menu">
        <span class="gw-profile-icon" aria-hidden="true">👤</span>
        <span class="gw-profile-name" title="{_profile_name}">{_profile_name}</span>
        <span class="gw-profile-arrow" aria-hidden="true">▼</span>
      </button>
      <div class="gw-profile-menu" id="gw-profile-menu" role="menu" hidden>
        <div class="gw-profile-summary">
          <strong>{_profile_name}</strong>
          <span>{_profile_email}</span>
        </div>
        <button type="button" class="gw-profile-logout" id="gw-profile-logout" role="menuitem">↪ Logout</button>
        <p class="gw-profile-error" id="gw-profile-error" role="alert" hidden></p>
      </div>
    </div>
    """
else:
    _auth_bar = (
        f'<div class="gw-guest"><a href="{_html.escape(GW_BASE_URL, quote=True)}/login.html">Sign in</a>'
        f'<a href="{_html.escape(GW_BASE_URL, quote=True)}/register.html">Register</a></div>'
    )

st.markdown(f"""<style>
.gw-authbar {{ position: fixed; top: 14px; right: 300px; z-index: 2147483000; font: 600 14px/1.4 Inter,Segoe UI,Arial,sans-serif; }}
.gw-profile {{ position: relative; }}
.gw-profile-toggle {{ display: flex; align-items: center; gap: 8px; max-width: min(38vw,320px); padding: 9px 12px; border: 1px solid #d7e3f2; border-radius: 11px; background: #fff; color: #164b91; box-shadow: 0 3px 12px rgba(15,48,88,.08); font: inherit; cursor: pointer; }}
.gw-profile-toggle:hover,.gw-profile-toggle[aria-expanded=true] {{ background: #f0f7ff; border-color: #9fc4ef; }}
.gw-profile-icon {{ display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: #e8f2ff; }}
.gw-profile-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.gw-profile-arrow {{ font-size: 10px; transition: transform .18s ease; }}
.gw-profile-toggle[aria-expanded=true] .gw-profile-arrow {{ transform: rotate(180deg); }}
.gw-profile-menu {{ position: absolute; top: calc(100% + 9px); right: 0; width: min(300px,calc(100vw - 24px)); padding: 8px; border: 1px solid #dce7f5; border-radius: 14px; background: #fff; color: #14213d; box-shadow: 0 18px 44px rgba(15,39,75,.2); }}
.gw-profile-menu[hidden],.gw-profile-error[hidden] {{ display: none; }}
.gw-profile-summary {{ display: grid; gap: 3px; padding: 10px 11px 12px; border-bottom: 1px solid #e8eef6; }}
.gw-profile-summary strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.gw-profile-summary span {{ color: #64748b; font-size: 12px; overflow-wrap: anywhere; }}
.gw-profile-logout {{ width: 100%; margin-top: 6px; padding: 10px 11px; border: 0; border-radius: 9px; background: transparent; color: #b42335; text-align: left; font: inherit; cursor: pointer; }}
.gw-profile-logout:hover {{ background: #fff1f2; }}
.gw-profile-error {{ margin: 5px 10px 7px; color: #b42335; font-size: 12px; }}
.gw-guest {{ display: flex; gap: 12px; }}
.gw-guest a {{ color: #38bdf8 !important; font-weight: 700; text-decoration: none !important; }}
.gw-guest a:hover {{ text-decoration: underline !important; }}
@media (max-width: 900px) {{ .gw-authbar {{ right: 120px; top: 56px; }} .gw-profile-toggle {{ max-width: 52vw; padding: 8px 9px; font-size: 12px; }} }}
</style>
<div class="gw-authbar">{_auth_bar}</div>
<script>
(function(){{
  var root=document.getElementById("gw-profile");
  if(!root) return;
  var toggle=document.getElementById("gw-profile-toggle");
  var menu=document.getElementById("gw-profile-menu");
  var logout=document.getElementById("gw-profile-logout");
  var error=document.getElementById("gw-profile-error");
  function closeMenu(returnFocus){{
    menu.hidden=true;
    toggle.setAttribute("aria-expanded","false");
    if(returnFocus) toggle.focus();
  }}
  toggle.addEventListener("click",function(){{
    var open=toggle.getAttribute("aria-expanded")==="true";
    menu.hidden=open;
    toggle.setAttribute("aria-expanded",String(!open));
  }});
  document.addEventListener("click",function(event){{
    if(!root.contains(event.target)) closeMenu(false);
  }});
  document.addEventListener("keydown",function(event){{
    if(event.key==="Escape" && toggle.getAttribute("aria-expanded")==="true") closeMenu(true);
  }});
  logout.addEventListener("click",async function(){{
    logout.disabled=true;
    try{{
      var response=await fetch({_api_logout_url},{{method:"POST",credentials:"include",cache:"no-store"}});
      if(!response.ok) throw new Error("Logout failed ("+response.status+").");
      window.location.assign({_home_url_json});
    }}catch(err){{
      error.textContent=err.message || "Logout failed. Please try again.";
      error.hidden=false;
      logout.disabled=false;
    }}
  }});
}})();
</script>""", unsafe_allow_html=True)

# ---------- Title ----------
st.markdown('<div class="dashboard-title">📊 Sales Intelligence & Forecasting Suite</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="dashboard-subtitle">'
    'Deep diagnostic analytics, AI forecasting, cost breakdowns, and executive intelligence reporting.'
    '</div>',
    unsafe_allow_html=True
)

# ============================================================
# DATA PIPELINE  (cached -> dashboard no longer recomputes on every click)
# ============================================================
@st.cache_data(show_spinner=False)
def create_sample_data():
    """Vectorised demo dataset (much faster than the old row-by-row loop)."""
    rng = np.random.default_rng(42)
    dates = pd.date_range(start="2024-01-01", end="2026-08-01", freq="D")
    daily_orders = rng.integers(2, 8, size=len(dates))
    date_col = np.repeat(dates.values, daily_orders)
    n = len(date_col)

    products = np.array(["Laptop", "Mobile", "Tablet", "Monitor", "Keyboard", "Mouse", "Headphones", "Printer"])
    categories = {
        "Laptop": "Electronics", "Mobile": "Electronics", "Tablet": "Electronics",
        "Monitor": "Electronics", "Keyboard": "Accessories", "Mouse": "Accessories",
        "Headphones": "Accessories", "Printer": "Office"
    }
    base_price = {
        "Laptop": 55000, "Mobile": 22000, "Tablet": 30000, "Monitor": 18000,
        "Keyboard": 2500, "Mouse": 1200, "Headphones": 3000, "Printer": 15000
    }
    product = rng.choice(products, size=n)
    quantity = rng.integers(1, 8, size=n)
    selling = pd.Series(product).map(base_price).to_numpy() * rng.uniform(0.90, 1.10, size=n)
    cost = selling * rng.uniform(0.55, 0.92, size=n)
    discount = rng.uniform(0, 1, size=n) * selling * quantity * 0.08

    return pd.DataFrame({
        "Date": date_col,
        "Order_ID": np.arange(1000, 1000 + n),
        "Product": product,
        "Category": pd.Series(product).map(categories).to_numpy(),
        "Region": rng.choice(["North", "South", "East", "West"], size=n),
        "Customer": rng.choice([f"Customer_{i}" for i in range(1, 101)], size=n),
        "Quantity": quantity,
        "Selling_Price": selling.round(2),
        "Cost_Price": cost.round(2),
        "Discount": discount.round(2),
        "Operating_Expense": rng.uniform(100, 1000, size=n).round(2),
        "Cost_Type": rng.choice(["Variable", "Fixed"], p=[0.8, 0.2], size=n),
    })


@st.cache_data(show_spinner="Reading file...")
def _parse_upload(filename, raw, selection=None):
    return upload_storage.read_dataset(filename, raw, selection)


def read_file(uploaded_file, selection=None):
    try:
        return _parse_upload(uploaded_file.name, uploaded_file.getvalue(), selection)
    except Exception as e:
        st.error(f"❌ File reading error: {e}")
        return None


# Friendly column names shop owners commonly use -> standard names
COLUMN_ALIASES = {
    "order_date": "date", "sale_date": "date", "invoice_date": "date",
    "item": "product", "item_name": "product", "product_name": "product",
    "qty": "quantity", "units": "quantity",
    "price": "selling_price", "unit_price": "selling_price", "sale_price": "selling_price",
    "cost": "cost_price", "purchase_price": "cost_price",
    "customer_name": "customer", "client": "customer",
}


def standardize_columns(df):
    df = df.copy()
    df.columns = (
        df.columns.astype(str).str.strip().str.lower()
        .str.replace(" ", "_", regex=False).str.replace("-", "_", regex=False)
    )
    for old, new in COLUMN_ALIASES.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})
    return df


@st.cache_data(show_spinner=False)
def _prepare_core(raw_df):
    df = standardize_columns(raw_df)
    required = ["date", "product", "quantity", "selling_price", "cost_price"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return None, missing, 0

    defaults = {"discount": 0, "customer": "Unknown", "category": "General", "region": "Unknown",
                "operating_expense": 0, "cost_type": "Variable"}
    for col, val in defaults.items():
        if col not in df.columns:
            df[col] = val
    if "order_id" not in df.columns:
        df["order_id"] = np.arange(1, len(df) + 1)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["quantity", "selling_price", "cost_price", "discount", "operating_expense"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    before = len(df)
    df = df.dropna(subset=["date", "product"])
    dropped = before - len(df)

    df["sales"] = df["quantity"] * df["selling_price"]
    df["total_cost"] = df["quantity"] * df["cost_price"]
    df["gross_profit"] = df["sales"] - df["total_cost"]
    df["net_profit"] = df["gross_profit"] - df["discount"] - df["operating_expense"]
    df["profit"] = df["net_profit"]
    df["profit_margin"] = np.where(df["sales"] != 0, (df["net_profit"] / df["sales"]) * 100, 0)

    df["day"] = df["date"].dt.date
    df["week"] = df["date"].dt.to_period("W").apply(lambda x: x.start_time)
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    df["quarter"] = df["date"].dt.to_period("Q").dt.to_timestamp()
    df["year"] = df["date"].dt.year
    df["month_name"] = df["date"].dt.strftime("%B")
    return df, [], dropped


def prepare_data(raw_df):
    df, missing, dropped = _prepare_core(raw_df)
    if missing:
        st.error("❌ Required columns are missing:")
        for col in missing:
            st.write(f"• `{col}`")
        st.caption("Tip: accepted names include date, product, quantity (qty), selling_price (price), cost_price (cost).")
        return None
    if dropped:
        st.warning(f"⚠️ {dropped:,} row(s) were skipped because the date or product was missing/invalid.")
    return df


# ============================================================
# SIDEBAR CONTROLS
# ============================================================
st.sidebar.title("⚙️ Dashboard Controls")
st.sidebar.success("✅ **Free Access** — all reports & PDF downloads are unlocked")

uploaded_file = st.sidebar.file_uploader(
    "📂 Upload Sales File",
    type=["csv", "xlsx", "xls", "json", "parquet", "tsv", "db", "sqlite", "sqlite3", "sql",
          "npy", "npz", "arff", "txt", "xml"],
    help="Pickle (.pkl/.pickle) is deliberately disabled because opening untrusted pickle files can execute code."
)

if uploaded_file is None:
    st.info("ℹ️ Displaying demonstration dataset. Upload your own CSV/Excel file in the sidebar.")
    raw_data = create_sample_data()
    dataset_name = "Sample_Electronics_Sales.csv"
else:
    upload_selection = None
    try:
        upload_options = upload_storage.dataset_options(uploaded_file.name, uploaded_file.getvalue())
        if upload_options:
            upload_selection = st.sidebar.selectbox(
                "Choose table / array", upload_options, key="uploaded_data_selection"
            )
    except Exception as upload_error:
        st.error(f"❌ Could not inspect the uploaded file: {upload_error}")
        st.stop()
    raw_data = read_file(uploaded_file, upload_selection)
    dataset_name = uploaded_file.name
    if raw_data is None:
        st.stop()

df = prepare_data(raw_data)
if df is None:
    st.stop()

st.sidebar.markdown("---")
st.sidebar.subheader("🔍 Filter Records")

min_date = df["date"].min().date()
max_date = df["date"].max().date()
date_range = st.sidebar.date_input("📅 Date Range", value=(min_date, max_date), min_value=min_date, max_value=max_date)

# FIX: picking only ONE date used to silently ignore the filter
if isinstance(date_range, (tuple, list)):
    if len(date_range) == 2:
        start_date, end_date = date_range
    elif len(date_range) == 1:
        start_date = end_date = date_range[0]
    else:
        start_date, end_date = min_date, max_date
else:
    start_date = end_date = date_range
filtered_df = df[(df["date"].dt.date >= start_date) & (df["date"].dt.date <= end_date)].copy()

years = sorted(filtered_df["year"].unique())
selected_years = st.sidebar.multiselect("📆 Year", years, default=years)
filtered_df = filtered_df[filtered_df["year"].isin(selected_years)]

regions = sorted(filtered_df["region"].astype(str).unique())
selected_regions = st.sidebar.multiselect("🌍 Region", regions, default=regions)
filtered_df = filtered_df[filtered_df["region"].astype(str).isin(selected_regions)]

categories = sorted(filtered_df["category"].astype(str).unique())
selected_categories = st.sidebar.multiselect("📦 Category", categories, default=categories)
filtered_df = filtered_df[filtered_df["category"].astype(str).isin(selected_categories)]

products = sorted(filtered_df["product"].astype(str).unique())
selected_products = st.sidebar.multiselect("🛒 Product", products, default=products)
filtered_df = filtered_df[filtered_df["product"].astype(str).isin(selected_products)]

if filtered_df.empty:
    st.warning("⚠️ No data available for the selected filters. Please adjust your criteria.")
    st.stop()

# ============================================================
# KPI CALCULATIONS
# ============================================================
total_sales = filtered_df["sales"].sum()
total_cost = filtered_df["total_cost"].sum()
gross_profit = filtered_df["gross_profit"].sum()
net_profit = filtered_df["net_profit"].sum()
profit_margin = (net_profit / total_sales * 100) if total_sales != 0 else 0
orders = filtered_df["order_id"].nunique()
quantity = filtered_df["quantity"].sum()
customers_count = filtered_df["customer"].nunique()
total_discount = filtered_df["discount"].sum()
discount_pct = (total_discount / total_sales * 100) if total_sales != 0 else 0
avg_order_value = (total_sales / orders) if orders else 0
gross_margin_pct = (gross_profit / total_sales * 100) if total_sales != 0 else 0

monthly_sales = filtered_df.groupby("month")["sales"].sum().reset_index()
fixed_cost = filtered_df.loc[filtered_df["cost_type"].astype(str).str.lower() == "fixed", "total_cost"].sum()
variable_cost = filtered_df.loc[filtered_df["cost_type"].astype(str).str.lower() == "variable", "total_cost"].sum()
operating_expenses = filtered_df["operating_expense"].sum()

st.subheader("📌 Key Performance Indicators (KPIs)")
k1, k2, k3, k4 = st.columns(4)
k1.metric("💰 Total Sales", f"₹{total_sales:,.0f}")
k2.metric("💸 Total Cost", f"₹{total_cost:,.0f}")
k3.metric("📈 Gross Profit", f"₹{gross_profit:,.0f}")
k4.metric("💵 Net Profit", f"₹{net_profit:,.0f}")

k5, k6, k7, k8 = st.columns(4)
k5.metric("📊 Net Profit Margin", f"{profit_margin:.2f}%")
k6.metric("🧾 Orders", f"{orders:,}")
k7.metric("📦 Quantity Sold", f"{quantity:,.0f}")
k8.metric("👥 Active Customers", f"{customers_count:,}")

k9, k10, k11, k12 = st.columns(4)
k9.metric("🧮 Avg Order Value", f"₹{avg_order_value:,.0f}")
k10.metric("🏷️ Total Discount", f"₹{total_discount:,.0f}")
k11.metric("📉 Discount % of Sales", f"{discount_pct:.2f}%")
k12.metric("📐 Gross Margin", f"{gross_margin_pct:.2f}%")


# ============================================================
# DEEP STATISTICS (self-contained: works for any dataframe slice)
# ============================================================
def _f(x, default=0.0):
    try:
        return float(x) if pd.notna(x) else default
    except Exception:
        return default


def get_detailed_stats(df_in):
    s, p, c = df_in["sales"], df_in["net_profit"], df_in["total_cost"]
    tot_sales, tot_cost, tot_profit = s.sum(), c.sum(), p.sum()
    margin = (tot_profit / tot_sales * 100) if tot_sales != 0 else 0

    mean_s, std_s = _f(s.mean()), _f(s.std())
    volatility = (std_s / mean_s * 100) if mean_s != 0 else 0
    outlier_count = int((np.abs(s - mean_s) > 2 * std_s).sum()) if std_s > 0 else 0

    prod = df_in.groupby("product").agg(Sales=("sales", "sum"), Profit=("net_profit", "sum"))
    prod["Margin"] = np.where(prod["Sales"] != 0, prod["Profit"] / prod["Sales"] * 100, 0)
    best_p = prod["Sales"].idxmax()
    top3_pct = (prod["Sales"].nlargest(3).sum() / tot_sales * 100) if tot_sales != 0 else 0

    cat_sales = df_in.groupby("category")["sales"].sum()
    top_c = cat_sales.idxmax()
    top_c_pct = (cat_sales.max() / tot_sales * 100) if tot_sales != 0 else 0

    m_sales = df_in.groupby("month")["sales"].sum()
    return {
        "dataset_rows": len(df_in),
        "total_sales": tot_sales, "total_cost": tot_cost, "net_profit": tot_profit, "profit_margin": margin,
        "orders": df_in["order_id"].nunique(), "customers": df_in["customer"].nunique(),
        "total_discount": df_in["discount"].sum(),
        "discount_pct": (df_in["discount"].sum() / tot_sales * 100) if tot_sales != 0 else 0,
        "mean_sales": mean_s, "median_sales": _f(s.median()), "std_sales": std_s,
        "min_sales": _f(s.min()), "max_sales": _f(s.max()),
        "iqr_sales": _f(s.quantile(0.75) - s.quantile(0.25)),
        "mean_profit": _f(p.mean()), "median_profit": _f(p.median()), "std_profit": _f(p.std()),
        "min_profit": _f(p.min()), "max_profit": _f(p.max()),
        "iqr_profit": _f(p.quantile(0.75) - p.quantile(0.25)),
        "mean_cost": _f(c.mean()), "median_cost": _f(c.median()), "std_cost": _f(c.std()),
        "volatility": volatility, "outlier_count": outlier_count,
        "best_product": best_p, "best_sales": _f(prod.loc[best_p, "Sales"]), "top3_pct": top3_pct,
        "best_profit_product": prod["Profit"].idxmax(), "worst_profit_product": prod["Profit"].idxmin(),
        "low_margin_count": int((prod["Margin"] < 10).sum()),
        "top_cat": top_c, "top_cat_pct": top_c_pct,
        "best_month": m_sales.idxmax().strftime("%b %Y"), "worst_month": m_sales.idxmin().strftime("%b %Y"),
        "best_month_sales": _f(m_sales.max()), "worst_month_sales": _f(m_sales.min()),
        "avg_monthly_sales": _f(m_sales.mean()),
    }


# ============================================================
# REPORT CHARTS (matplotlib) - cached; theme-aware for UI, light theme for PDF
# ============================================================
@st.cache_data(show_spinner=False)
def build_report_charts(df_in, th):
    """Returns {name: png_bytes}. `th` is a theme dict."""
    imgs = {}

    def _new_fig(title):
        fig, ax = plt.subplots(figsize=(7, 3), facecolor=th["page"])
        ax.set_facecolor(th["panel"])
        ax.set_title(title, color=th["text"], fontsize=11, fontweight="bold")
        return fig, ax

    def _style_axes(ax, axis="both"):
        ax.tick_params(colors=th["muted"], labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(th["mpl_border"])
        ax.grid(True, color=th["mpl_grid"], ls="--", alpha=0.5, axis=axis)

    def _save(fig):
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()

    pie_colors = ["#38bdf8", "#818cf8", "#34d399", "#f59e0b", "#ec4899", "#22d3ee", "#a3e635", "#fb7185"]

    # 1. Trend (+ 3-month moving average)
    m = df_in.groupby("month")["sales"].sum()
    fig, ax = _new_fig("Sales Progression Over Time")
    ax.plot(m.index, m.values, color="#38bdf8", lw=2.5, marker="o", markersize=4, label="Monthly sales")
    if len(m) >= 4:
        ax.plot(m.index, m.rolling(3).mean(), color="#f59e0b", lw=1.8, ls="--", label="3-month average")
        leg = ax.legend(fontsize=7, facecolor=th["panel"], edgecolor=th["mpl_border"])
        for t in leg.get_texts():
            t.set_color(th["text"])
    _style_axes(ax)
    fig.autofmt_xdate()
    imgs["trend"] = _save(fig)

    # 2. Category donut
    cat = df_in.groupby("category")["sales"].sum()
    fig, ax = _new_fig("Category Portfolio Distribution")
    if cat.sum() > 0:
        ax.pie(cat, labels=cat.index, autopct="%1.1f%%", colors=pie_colors, startangle=140,
               textprops=dict(color=th["text"], fontsize=8),
               wedgeprops=dict(width=0.45, edgecolor=th["page"]))
    else:
        ax.text(0.5, 0.5, "No category data", ha="center", va="center", color=th["text"], transform=ax.transAxes)
        ax.axis("off")
    imgs["category"] = _save(fig)

    # 3. Top products
    top_p = df_in.groupby("product")["sales"].sum().sort_values(ascending=False).head(6)
    fig, ax = _new_fig("Top 6 Products by Gross Revenue")
    ax.bar(top_p.index.astype(str), top_p.values, color="#818cf8", edgecolor=th["page"], width=0.55)
    _style_axes(ax, axis="y")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    imgs["products"] = _save(fig)

    # 4. Cost structure
    ctype = df_in["cost_type"].astype(str).str.lower()
    cost_series = pd.Series({
        "Fixed Cost": df_in.loc[ctype == "fixed", "total_cost"].sum(),
        "Variable Cost": df_in.loc[ctype == "variable", "total_cost"].sum(),
        "Operating": df_in["operating_expense"].sum(),
    })
    fig, ax = _new_fig("Operational Cost Distribution")
    if cost_series.sum() > 0:
        ax.pie(cost_series, labels=cost_series.index, autopct="%1.1f%%",
               colors=["#f59e0b", "#38bdf8", "#ec4899"], startangle=140,
               textprops=dict(color=th["text"], fontsize=8),
               wedgeprops=dict(width=0.45, edgecolor=th["page"]))
    else:
        ax.text(0.5, 0.5, "No cost data", ha="center", va="center", color=th["text"], transform=ax.transAxes)
        ax.axis("off")
    imgs["cost"] = _save(fig)
    return imgs


# ============================================================
# PDF REPORT GENERATOR (REPORTLAB)
# ============================================================
def _pdf_image(png_bytes, width):
    """Keep the chart's real aspect ratio (old code forced fixed heights = stretched charts)."""
    buf = BytesIO(png_bytes)
    w, h = PILImage.open(buf).size
    buf.seek(0)
    return Image(buf, width=width, height=width * h / w)


def _pdf_footer(canv, doc_obj):
    canv.saveState()
    canv.setFont(PDF_FONT, 7.5)
    canv.setFillColor(colors.HexColor("#64748b"))
    canv.drawString(32, 18, "GW Smart Analytics • Sales Intelligence Report")
    canv.drawRightString(A4[0] - 32, 18, f"Page {doc_obj.page}")
    canv.restoreState()


def build_comprehensive_report_pdf(stats, chart_imgs, ds_name, report_type="Executive Overview"):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=32, leftMargin=32, topMargin=32, bottomMargin=36)
    esc = lambda x: _html.escape(str(x))

    h1 = ParagraphStyle("H1", fontName=PDF_FONT_BOLD, fontSize=18, leading=22, textColor=colors.HexColor("#0b3d91"), spaceAfter=6)
    h2 = ParagraphStyle("H2", fontName=PDF_FONT_BOLD, fontSize=13, leading=17, textColor=colors.HexColor("#0f172a"), spaceBefore=10, spaceAfter=6)
    body = ParagraphStyle("Bdy", fontName=PDF_FONT, fontSize=8.5, leading=12.5, textColor=colors.HexColor("#334155"))
    exp = ParagraphStyle("Exp", fontName=PDF_FONT, fontSize=8, leading=11.5, textColor=colors.HexColor("#1e293b"))

    S = stats
    story = []
    story.append(Paragraph(f"GW DATA ANALYTICS • {str(report_type).upper()} REPORT", h1))
    story.append(Paragraph(
        f"<b>Dataset Analyzed:</b> {esc(ds_name)} &nbsp;|&nbsp; <b>Report Type:</b> {esc(report_type)} "
        f"&nbsp;|&nbsp; <b>Records:</b> {S['dataset_rows']:,} "
        f"&nbsp;|&nbsp; <b>Date:</b> {datetime.now().strftime('%d %b %Y, %I:%M %p')}", body))
    story.append(Spacer(1, 8))

    story.append(Paragraph("1. Executive Summary & Overview", h2))
    story.append(Paragraph(
        f"This executive intelligence audit analyzes <b>{S['dataset_rows']:,} transactions</b> with a cumulative sales volume of "
        f"<b>₹{S['total_sales']:,.0f}</b> and net earnings of <b>₹{S['net_profit']:,.0f}</b> (Net Margin: <b>{S['profit_margin']:.2f}%</b>). "
        f"Operations spanned <b>{S['orders']:,} distinct orders</b> and <b>{S['customers']:,} unique customer accounts</b>. "
        f"The primary product driver was <b>{esc(S['best_product'])}</b> generating ₹{S['best_sales']:,.0f} in revenue. "
        f"Total discounts given were <b>₹{S['total_discount']:,.0f}</b> ({S['discount_pct']:.2f}% of sales). "
        f"Sales volatility (coefficient of variation) is <b>{S['volatility']:.1f}%</b>, with "
        f"<b>{S['outlier_count']} statistical anomaly records</b> outside the 2σ threshold.", body))
    story.append(Spacer(1, 8))

    story.append(Paragraph("2. Deep Statistical Indicators", h2))
    rows = [
        ["METRIC", "SALES (₹)", "NET PROFIT (₹)", "COST (₹)"],
        ["Aggregate Total", f"₹{S['total_sales']:,.0f}", f"₹{S['net_profit']:,.0f}", f"₹{S['total_cost']:,.0f}"],
        ["Arithmetic Mean (Avg)", f"₹{S['mean_sales']:,.2f}", f"₹{S['mean_profit']:,.2f}", f"₹{S['mean_cost']:,.2f}"],
        ["Median (50th Percentile)", f"₹{S['median_sales']:,.2f}", f"₹{S['median_profit']:,.2f}", f"₹{S['median_cost']:,.2f}"],
        ["Standard Deviation (σ)", f"₹{S['std_sales']:,.2f}", f"₹{S['std_profit']:,.2f}", f"₹{S['std_cost']:,.2f}"],
        ["Min / Max Boundary", f"₹{S['min_sales']:,.0f} / ₹{S['max_sales']:,.0f}", f"₹{S['min_profit']:,.0f} / ₹{S['max_profit']:,.0f}", "-"],
        ["Interquartile Range (IQR)", f"₹{S['iqr_sales']:,.2f}", f"₹{S['iqr_profit']:,.2f}", "-"],
    ]
    t = Table(rows, colWidths=[2.2 * inch, 1.7 * inch, 1.7 * inch, 1.6 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
        ("FONTNAME", (0, 1), (-1, -1), PDF_FONT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("PADDING", (0, 0), (-1, -1), 4.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
    ]))
    story.append(t)
    story.append(PageBreak())

    story.append(Paragraph("3. Visual Diagnostics & In-Depth Interpretations", h2))
    W = 6.8 * inch
    if "trend" in chart_imgs:
        story.append(_pdf_image(chart_imgs["trend"], W))
        story.append(Paragraph(
            "<b>Detailed Trend Explanation:</b> Monthly sales over the selected period, with the 3-month moving average smoothing short-term noise. "
            f"The average monthly sales of ₹{S['avg_monthly_sales']:,.0f} is the baseline. "
            f"Best month: <b>{S['best_month']}</b> (₹{S['best_month_sales']:,.0f}); weakest: "
            f"<b>{S['worst_month']}</b> (₹{S['worst_month_sales']:,.0f}). Net profit margin: {S['profit_margin']:.2f}%.", exp))
        story.append(Spacer(1, 10))

    if "category" in chart_imgs:
        story.append(_pdf_image(chart_imgs["category"], W * 0.8))
        note = ("High reliance on a single segment introduces portfolio risk; consider growing secondary categories."
                if S["top_cat_pct"] > 50 else "Revenue is reasonably balanced across categories.")
        story.append(Paragraph(
            "<b>Detailed Category Breakdown Explanation:</b> How revenue is distributed across categories. "
            f"The leading segment, <i>{esc(S['top_cat'])}</i>, represents <b>{S['top_cat_pct']:.1f}%</b> of total sales. {note}", exp))
        story.append(PageBreak())

    if "products" in chart_imgs:
        story.append(Paragraph("3. Visual Diagnostics (Continued)", h2))
        story.append(_pdf_image(chart_imgs["products"], W))
        story.append(Paragraph(
            "<b>Detailed Product Performance Explanation:</b> Top products by revenue. "
            f"<b>{esc(S['best_product'])}</b> delivers the highest gross revenue, and the top 3 products together account for "
            f"<b>{S['top3_pct']:.1f}%</b> of total revenue.", exp))
        story.append(Spacer(1, 10))

    if "cost" in chart_imgs:
        story.append(_pdf_image(chart_imgs["cost"], W * 0.8))
        story.append(Paragraph(
            "<b>Detailed Cost Structure Explanation:</b> Split between fixed cost, variable product cost and operating expenses. "
            "A higher share of variable cost gives more flexibility when demand slows down.", exp))
        story.append(Spacer(1, 12))

    # Data-driven recommendations (names the real products from the data)
    story.append(Paragraph("4. Actionable Strategic Recommendations", h2))
    story.append(Paragraph(
        f"• <b>1. High-Margin Scaling:</b> Prioritise marketing and stock for <b>{esc(S['best_profit_product'])}</b>, "
        "the top net-profit driver.", body))
    if S["low_margin_count"] > 0:
        story.append(Paragraph(
            f"• <b>2. Margin Protection:</b> <b>{S['low_margin_count']}</b> product(s) earn below 10% net margin. "
            f"Review supplier cost and discounts, starting with <b>{esc(S['worst_profit_product'])}</b> (weakest net profit).", body))
    else:
        story.append(Paragraph(
            "• <b>2. Margin Protection:</b> All products keep a net margin above 10%. Keep watching discounts "
            f"(currently {S['discount_pct']:.2f}% of sales).", body))
    story.append(Paragraph(
        f"• <b>3. Outlier Review:</b> {S['outlier_count']} unusually large/small transactions were detected; "
        "investigate them to repeat positive cases and prevent errors.", body))
    story.append(Paragraph("• <b>4. Forecasted Capacity:</b> Align procurement with the 12-month demand forecast in the dashboard.", body))
    story.append(Spacer(1, 14))
    story.append(Paragraph("Report Certified by: <b>GW Data Analytics Automated Intelligence Engine v4.6</b>", exp))

    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    buf.seek(0)
    return buf.getvalue()


# ============================================================
# FORECAST ENGINE  (linear trend + seasonality, with confidence band)
# ============================================================
def run_forecast(m_df, value_col, horizon=12, floor_zero=True):
    """RandomForest alone cannot extrapolate a trend (forecast goes flat).
    So: LinearRegression captures the trend, RandomForest learns month-of-year
    seasonality from the residuals. Band = approx. 80% range from model residuals."""
    m_df = m_df.sort_values("month").reset_index(drop=True)
    t = np.arange(len(m_df)).reshape(-1, 1)
    y = m_df[value_col].to_numpy(dtype=float)

    lin = LinearRegression().fit(t, y)
    resid = y - lin.predict(t)
    rf = RandomForestRegressor(n_estimators=200, min_samples_leaf=2, random_state=42)
    rf.fit(m_df[["month"]].assign(m=m_df["month"].dt.month)[["m"]], resid)

    fitted = lin.predict(t) + rf.predict(m_df[["month"]].assign(m=m_df["month"].dt.month)[["m"]])
    sigma = float(np.std(y - fitted))

    future_months = pd.date_range(start=m_df["month"].max() + pd.DateOffset(months=1), periods=horizon, freq="MS")
    t_f = np.arange(len(m_df), len(m_df) + horizon).reshape(-1, 1)
    pred = lin.predict(t_f) + rf.predict(pd.DataFrame({"m": future_months.month}))
    lower, upper = pred - 1.28 * sigma, pred + 1.28 * sigma
    if floor_zero:
        pred, lower, upper = np.clip(pred, 0, None), np.clip(lower, 0, None), np.clip(upper, 0, None)
    return pd.DataFrame({"month": future_months, "Forecast": pred, "Lower": lower, "Upper": upper})


def _forecast_features(months, salary_effect=None, offset=0):
    months = pd.DatetimeIndex(months)
    if salary_effect is None or len(salary_effect) != len(months):
        salary_effect = np.zeros(len(months), dtype=float)
    return pd.DataFrame({
        "trend": np.arange(offset, offset + len(months), dtype=float),
        "month_sin": np.sin(2 * np.pi * months.month / 12),
        "month_cos": np.cos(2 * np.pi * months.month / 12),
        "diwali": months.month.isin([10, 11]).astype(int),
        "dussehra": (months.month == 10).astype(int),
        "gudi_padwa": months.month.isin([3, 4]).astype(int),
        "christmas": (months.month == 12).astype(int),
        "month_end_salary_effect": np.asarray(salary_effect, dtype=float),
    })


def _forecast_one_model(train, future_months, model_name, value_col):
    y = train[value_col].to_numpy(dtype=float)
    train_months = pd.DatetimeIndex(train["month"])
    salary = train.get("salary_effect", pd.Series(np.zeros(len(train)))).to_numpy(dtype=float)
    future_salary = float(np.nanmean(salary[-12:])) if len(salary) else 0.0
    future_salary = 0.0 if not np.isfinite(future_salary) else future_salary
    x_train = _forecast_features(train_months, salary)
    x_future = _forecast_features(
        future_months, np.full(len(future_months), future_salary), offset=len(train)
    )
    extra_features = [
        name for name in ("Avg_Selling_Price", "discount_rate", "cost_rate", "profit_margin")
        if name in train.columns
    ]
    for name in extra_features:
        historical = pd.to_numeric(train[name], errors="coerce").replace([np.inf, -np.inf], np.nan)
        fill_value = float(historical.tail(12).mean()) if historical.notna().any() else 0.0
        x_train[name] = historical.fillna(fill_value).to_numpy()
        x_future[name] = fill_value
    if model_name == "Linear + RF":
        linear = LinearRegression().fit(x_train[["trend"]], y)
        residuals = y - linear.predict(x_train[["trend"]])
        residual_model = RandomForestRegressor(
            n_estimators=120, min_samples_leaf=2, random_state=42, n_jobs=1
        ).fit(x_train.drop(columns="trend"), residuals)
        predicted = linear.predict(x_future[["trend"]]) + residual_model.predict(
            x_future.drop(columns="trend")
        )
        return predicted, pd.DataFrame(columns=["Feature", "Importance"])
    if model_name == "Gradient Boosting":
        model = GradientBoostingRegressor(
            n_estimators=100, max_depth=2, learning_rate=0.04, random_state=42
        ).fit(x_train, y)
        return model.predict(x_future), pd.DataFrame({
            "Feature": x_train.columns, "Importance": model.feature_importances_
        }).sort_values("Importance", ascending=False)
    if model_name == "Holt-Winters (ETS)":
        if ExponentialSmoothing is None:
            raise ImportError("statsmodels is not installed")
        seasonal = "add" if len(y) >= 24 and np.any(y) else None
        ets_fit = ExponentialSmoothing(
            y, trend="add", seasonal=seasonal,
            seasonal_periods=12 if seasonal else None,
            initialization_method="estimated",
        ).fit(optimized=True)
        return np.asarray(ets_fit.forecast(len(future_months))), pd.DataFrame(columns=["Feature", "Importance"])
    if model_name == "SARIMA":
        if SARIMAX is None:
            raise ImportError("statsmodels is not installed")
        seasonal_order = (1, 1, 1, 12) if len(y) >= 24 else (0, 0, 0, 0)
        sarima_fit = SARIMAX(
            y, order=(1, 1, 1), seasonal_order=seasonal_order,
            enforce_stationarity=False, enforce_invertibility=False,
        ).fit(disp=False, maxiter=80)
        forecast_method = getattr(sarima_fit, "forecast", None)
        if not callable(forecast_method):
            raise TypeError("SARIMA fitted model does not expose a forecast method")
        return np.asarray(forecast_method(len(future_months))), pd.DataFrame(columns=["Feature", "Importance"])
    raise ValueError(f"Unknown forecast model: {model_name}")


@st.cache_data(show_spinner="Comparing forecast models...")
def _cached_forecast_comparison(monthly, value_col, horizon, confidence):
    monthly = monthly.sort_values("month").reset_index(drop=True).copy()
    months = pd.date_range(monthly["month"].min(), monthly["month"].max(), freq="MS")
    monthly = monthly.set_index("month").reindex(months).rename_axis("month").reset_index()
    for col in ("Sales", "Quantity", "Profit", value_col):
        if col in monthly.columns:
            monthly[col] = pd.to_numeric(monthly[col], errors="coerce").fillna(0.0)
    for col in ("Avg_Selling_Price", "discount_rate", "cost_rate", "profit_margin"):
        if col in monthly.columns:
            monthly[col] = pd.to_numeric(monthly[col], errors="coerce").interpolate(
                limit_direction="both"
            ).fillna(0.0)
    if "salary_effect" not in monthly:
        monthly["salary_effect"] = 0.0
    else:
        monthly["salary_effect"] = monthly["salary_effect"].fillna(0.0)

    n = len(monthly)
    if n >= 9:
        holdout = min(6, max(3, int(round(n * 0.2))), n - 6)
    else:
        holdout = max(1, min(3, n - 6))
    holdout = max(1, holdout)
    split = max(1, n - holdout)
    actual = monthly[value_col].to_numpy(dtype=float)[split:]
    test_months = pd.DatetimeIndex(monthly["month"].iloc[split:])
    model_names = ["Linear + RF", "Holt-Winters (ETS)", "SARIMA", "Gradient Boosting"]
    rows, successful = [], {}
    for name in model_names:
        try:
            pred, _ = _forecast_one_model(monthly.iloc[:split], test_months, name, value_col)
            pred = np.asarray(pred, dtype=float)
            if len(pred) != len(actual) or not np.isfinite(pred).all():
                raise ValueError("model returned non-finite or incomplete predictions")
            errors = pred - actual
            mae = float(np.mean(np.abs(errors)))
            rmse = float(np.sqrt(np.mean(errors ** 2)))
            nonzero = np.abs(actual) > 1e-9
            mape = float(np.mean(np.abs(errors[nonzero] / actual[nonzero])) * 100) if nonzero.any() else np.nan
            wape = float(np.sum(np.abs(errors)) / max(np.sum(np.abs(actual)), 1e-9) * 100)
            rows.append({"Model": name, "MAPE %": mape, "MAE": mae, "RMSE": rmse, "WAPE %": wape, "Status": "OK"})
            successful[name] = (mape if np.isfinite(mape) else wape, mae)
        except Exception as model_error:
            rows.append({"Model": name, "MAPE %": np.nan, "MAE": np.nan, "RMSE": np.nan,
                         "WAPE %": np.nan, "Status": str(model_error)[:100]})

    comparison = pd.DataFrame(rows)
    if successful:
        best_model = min(successful, key=lambda name: successful[name])
    else:
        best_model = "Baseline (recent average)"
    future_months = pd.date_range(monthly["month"].max() + pd.DateOffset(months=1), periods=horizon, freq="MS")
    importance = pd.DataFrame(columns=["Feature", "Importance"])
    try:
        future_pred, importance = _forecast_one_model(monthly, future_months, best_model, value_col)
    except Exception:
        tail = monthly[value_col].tail(min(3, n)).to_numpy(dtype=float)
        level = float(np.mean(tail)) if len(tail) else 0.0
        slope = float(np.mean(np.diff(tail))) if len(tail) > 1 else 0.0
        future_pred = np.array([level + slope * (i + 1) for i in range(horizon)], dtype=float)
        best_model = "Baseline (recent average)"

    best_row = comparison.loc[comparison["Model"] == best_model] if best_model in model_names else pd.DataFrame()
    err_scale = float(best_row["RMSE"].iloc[0]) if not best_row.empty and pd.notna(best_row["RMSE"].iloc[0]) else 0.0
    z_values = {80: 1.282, 90: 1.645, 95: 1.96}
    z_score = z_values.get(int(confidence), 1.645)
    forecast = pd.DataFrame({
        "month": future_months,
        "Forecast": np.maximum(future_pred, 0) if value_col != "Profit" else future_pred,
        "Lower": future_pred - z_score * err_scale,
        "Upper": future_pred + z_score * err_scale,
    })
    if value_col != "Profit":
        forecast["Lower"] = forecast["Lower"].clip(lower=0)
    reliability_error = float(best_row["MAPE %"].iloc[0]) if not best_row.empty and pd.notna(best_row["MAPE %"].iloc[0]) else np.nan
    reliability_metric = reliability_error if np.isfinite(reliability_error) else (
        float(best_row["WAPE %"].iloc[0]) if not best_row.empty else np.nan
    )
    reliability = "High" if np.isfinite(reliability_metric) and reliability_metric < 15 else (
        "Medium" if np.isfinite(reliability_metric) and reliability_metric < 30 else "Low"
    )
    holdout_frame = pd.DataFrame({"month": test_months, "Actual": actual})
    return comparison, forecast, best_model, reliability, holdout_frame, importance, holdout


@st.cache_data(show_spinner="Calculating customer segments...")
def _cached_customer_insights(data):
    d = data.copy()
    last_date = d["date"].max()
    grouped = d.groupby("customer").agg(
        Recency=("date", lambda dates: int((last_date - dates.max()).days)),
        Frequency=("order_id", "nunique"), Monetary=("sales", "sum"),
        Last_Purchase=("date", "max"), Quantity=("quantity", "sum")
    ).reset_index()
    if grouped.empty:
        return grouped, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    r_med, f_med, m_med = grouped["Recency"].median(), grouped["Frequency"].median(), grouped["Monetary"].median()
    grouped["Segment"] = np.select(
        [
            (grouped["Recency"] <= r_med) & (grouped["Frequency"] >= f_med) & (grouped["Monetary"] >= m_med),
            (grouped["Recency"] <= r_med) & (grouped["Frequency"] >= f_med),
            (grouped["Recency"] > max(60, r_med * 1.5)) & (grouped["Frequency"] > 1),
            grouped["Recency"] > 180,
        ],
        ["Champions", "Loyal", "At-Risk", "Lost"], default="Potential Loyalist"
    )
    features = np.log1p(grouped[["Recency", "Frequency", "Monetary"]].clip(lower=0))
    if len(grouped) >= 4:
        scaled = StandardScaler().fit_transform(features)
        clusters = KMeans(n_clusters=min(4, len(grouped)), random_state=42, n_init=10).fit_predict(scaled)
        grouped["Cluster"] = clusters
        cluster_value = grouped.groupby("Cluster")["Monetary"].mean().sort_values(ascending=False)
        rank = {cluster: i + 1 for i, cluster in enumerate(cluster_value.index)}
        grouped["Cluster"] = grouped["Cluster"].map(lambda cluster: f"C{rank[cluster]}")
    else:
        grouped["Cluster"] = "Insufficient customers"
    months_observed = max((last_date - d["date"].min()).days / 30.44, 1)
    grouped["Estimated CLV"] = (
        grouped["Monetary"] / months_observed * 12 *
        (1 + grouped["Frequency"].gt(1).astype(float))
    )
    segment_summary = grouped.groupby("Segment", as_index=False).agg(
        Customers=("customer", "count"), Sales=("Monetary", "sum"),
        Avg_Recency_Days=("Recency", "mean"), Avg_CLV=("Estimated CLV", "mean")
    )

    churn_rows = []
    cutoff_dates = pd.date_range(
        d["date"].min() + pd.Timedelta(days=180),
        last_date - pd.Timedelta(days=90), freq="30D"
    )
    for cutoff in cutoff_dates:
        past = d[d["date"] <= cutoff]
        future = d[(d["date"] > cutoff) & (d["date"] <= cutoff + pd.Timedelta(days=90))]
        past_features = past.groupby("customer").agg(
            Recency=("date", lambda dates: (cutoff - dates.max()).days),
            Frequency=("order_id", "nunique"), Monetary=("sales", "sum")
        )
        returned = set(future["customer"].astype(str))
        for customer, row in past_features.iterrows():
            churn_rows.append([row["Recency"], row["Frequency"], row["Monetary"],
                               int(str(customer) not in returned)])
    churn_data = pd.DataFrame(churn_rows, columns=["Recency", "Frequency", "Monetary", "Churn"])
    if len(churn_data) >= 20 and churn_data["Churn"].nunique() == 2:
        from sklearn.linear_model import LogisticRegression
        churn_model = LogisticRegression(max_iter=300, class_weight="balanced", random_state=42)
        churn_model.fit(np.log1p(churn_data[["Recency", "Frequency", "Monetary"]].clip(lower=0)),
                        churn_data["Churn"])
        current_features = np.log1p(grouped[["Recency", "Frequency", "Monetary"]].clip(lower=0))
        grouped["Churn Risk %"] = churn_model.predict_proba(current_features)[:, 1] * 100
        grouped["Churn Model"] = "Historical 90-day return classifier"
    else:
        grouped["Churn Risk %"] = np.clip(grouped["Recency"] / 180 * 100, 0, 100)
        grouped["Churn Model"] = "Recency-based fallback (limited history)"
    return grouped, segment_summary, churn_data, grouped.sort_values("Churn Risk %", ascending=False)


@st.cache_data(show_spinner="Checking transaction anomalies...")
def _cached_anomaly_scores(data):
    d = data.copy()
    columns = ["sales", "quantity", "profit_margin"]
    values = d[columns].replace([np.inf, -np.inf], np.nan).fillna(0)
    if len(d) < 10:
        d["IF_Anomaly"] = False
        d["IF_Score"] = 0.0
    else:
        model = IsolationForest(contamination="auto", random_state=42, n_estimators=100)
        d["IF_Anomaly"] = model.fit_predict(values) == -1
        d["IF_Score"] = -model.score_samples(values)
    mean_sales, std_sales = d["sales"].mean(), d["sales"].std()
    d["2-Sigma Anomaly"] = (np.abs(d["sales"] - mean_sales) > 2 * std_sales) if std_sales > 0 else False
    return d


@st.cache_data(show_spinner=False)
def _cached_product_quarter_outlook(data):
    monthly = data.groupby(["product", "month"], as_index=False).agg(
        Sales=("sales", "sum"), Quantity=("quantity", "sum")
    )
    rows = []
    for product, frame in monthly.groupby("product"):
        frame = frame.sort_values("month").tail(12)
        vals = frame["Sales"].to_numpy(dtype=float)
        if len(vals) >= 2:
            trend = LinearRegression().fit(np.arange(len(vals)).reshape(-1, 1), vals)
            next_sales = np.maximum(trend.predict(np.arange(len(vals), len(vals) + 3).reshape(-1, 1)), 0)
            base = float(np.mean(vals[-min(3, len(vals)):]))
            growth = (float(np.mean(next_sales)) / base - 1) * 100 if base > 0 else 0.0
        else:
            next_sales, growth = np.repeat(vals[-1] if len(vals) else 0.0, 3), 0.0
        rows.append({"Product": product, "Predicted Next-Quarter Sales": float(next_sales.sum()),
                     "Predicted Growth %": float(growth)})
    return pd.DataFrame(rows).sort_values("Predicted Growth %", ascending=False)


@st.cache_data(show_spinner=False)
def _cached_basket_rules(data):
    baskets = data.groupby("order_id")["product"].apply(lambda values: set(map(str, values)))
    baskets = baskets[baskets.map(len) > 1]
    if baskets.empty:
        return pd.DataFrame(columns=["Product", "Also buy", "Support %", "Confidence %", "Lift"])
    product_counts, pair_counts = {}, {}
    for basket in baskets:
        for product in basket:
            product_counts[product] = product_counts.get(product, 0) + 1
        items = sorted(basket)
        for i, item in enumerate(items):
            for other in items[i + 1:]:
                pair_counts[(item, other)] = pair_counts.get((item, other), 0) + 1
    total = len(baskets)
    rows = []
    for (left, right), count in pair_counts.items():
        support = count / total
        for source, target in ((left, right), (right, left)):
            confidence = count / product_counts[source]
            lift = confidence / (product_counts[target] / total)
            rows.append({"Product": source, "Also buy": target, "Support %": support * 100,
                         "Confidence %": confidence * 100, "Lift": lift})
    return pd.DataFrame(rows).sort_values(["Lift", "Confidence %"], ascending=False).head(30)


def _excel_bytes(sheets):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return output.getvalue()


def _build_predictive_pdf(forecast_table, scenario_table, customer_table, summary, dataset_name):
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=32, leftMargin=32, topMargin=36, bottomMargin=36)
    title_style = ParagraphStyle("PredictiveTitle", fontName=PDF_FONT_BOLD, fontSize=17, leading=22,
                                 textColor=colors.HexColor("#0b3d91"), spaceAfter=12)
    section_style = ParagraphStyle("PredictiveSection", fontName=PDF_FONT_BOLD, fontSize=12, leading=16,
                                   textColor=colors.HexColor("#0f172a"), spaceBefore=10, spaceAfter=6)
    body_style = ParagraphStyle("PredictiveBody", fontName=PDF_FONT, fontSize=8.5, leading=12,
                                textColor=colors.HexColor("#334155"))
    story: list[Any] = [
        Paragraph("GW Smart Analytics • Predictive & Prescriptive Addendum", title_style),
        Paragraph(f"Dataset: {_html.escape(str(dataset_name))} | Generated: {datetime.now():%d %b %Y}", body_style),
        Paragraph("AI Insight Summary", section_style),
        Paragraph(_html.escape(summary), body_style),
    ]
    for heading, frame in (("Forecast & Reliability", forecast_table),
                           ("Base vs Scenario", scenario_table),
                           ("Customer RFM / Churn", customer_table)):
        story.append(Paragraph(heading, section_style))
        if frame.empty:
            story.append(Paragraph("No data available for this section.", body_style))
            continue
        columns = list(frame.columns[:6])
        rows = [columns]
        for _, row in frame[columns].head(12).iterrows():
            rows.append([_html.escape(str(value)[:70]) for value in row.tolist()])
        table = Table(rows, repeatRows=1, colWidths=[(A4[0] - 64) / len(columns)] * len(columns))
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
            ("FONTNAME", (0, 1), (-1, -1), PDF_FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(table)
    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    output.seek(0)
    return output.getvalue()


# ============================================================
# MAIN TABS
# ============================================================
tabs = st.tabs([
    "📊 Sales", "💰 Cost", "📈 Profit & Loss", "🛒 Products", "👥 Customers",
    "⏱️ Time", "📈 Growth", "🔮 Forecast", "🚨 Risk & Alerts", "📑 Comprehensive AI Report",
    "🧠 Predictive & Prescriptive"
])

# ---------------- 1. SALES ----------------
with tabs[0]:
    st.header("📊 Sales Analysis")
    daily_sales = filtered_df.groupby("day")["sales"].sum().reset_index()
    yearly_sales = filtered_df.groupby("year")["sales"].sum().reset_index()

    col1, col2 = st.columns(2)
    with col1:
        fig = px.line(daily_sales, x="day", y="sales", title="Daily Sales Trajectory")
        st.plotly_chart(style_chart(fig), use_container_width=True)
    with col2:
        fig = px.line(monthly_sales, x="month", y="sales", markers=True, title="Monthly Aggregated Sales")
        st.plotly_chart(style_chart(fig), use_container_width=True)

    fig_year = px.bar(yearly_sales, x="year", y="sales", title="Yearly Sales Overview", text_auto=".2s")
    st.plotly_chart(style_chart(fig_year), use_container_width=True)

    # NEW: region analysis
    region_sales = filtered_df.groupby("region")["sales"].sum().reset_index().sort_values("sales", ascending=False)
    r1, r2 = st.columns(2)
    with r1:
        fig = px.bar(region_sales, x="region", y="sales", title="Sales by Region", text_auto=".2s")
        st.plotly_chart(style_chart(fig), use_container_width=True)
    with r2:
        pivot = filtered_df.pivot_table(index="product", columns="region", values="sales", aggfunc="sum").fillna(0)
        fig = px.imshow(pivot, text_auto=".2s", aspect="auto", title="Product × Region Sales Heatmap")
        st.plotly_chart(style_chart(fig), use_container_width=True)

    prod_s = filtered_df.groupby("product")["sales"].sum().sort_values(ascending=False)
    c1, c2 = st.columns(2)
    c1.success(f"🏆 Best Product by Sales: **{prod_s.index[0]}** (₹{prod_s.iloc[0]:,.0f})")
    c2.warning(f"⚠️ Lowest Product by Sales: **{prod_s.index[-1]}** (₹{prod_s.iloc[-1]:,.0f})")

# ---------------- 2. COST ----------------
with tabs[1]:
    st.header("💰 Cost Structure Analysis")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("💸 Total Cost", f"₹{total_cost:,.0f}")
    c2.metric("📌 Fixed Costs", f"₹{fixed_cost:,.0f}")
    c3.metric("🔄 Variable Costs", f"₹{variable_cost:,.0f}")
    c4.metric("🏢 Operating Expenses", f"₹{operating_expenses:,.0f}")

    col_a, col_b = st.columns(2)
    with col_a:
        cost_product = filtered_df.groupby("product")["total_cost"].sum().reset_index().sort_values("total_cost", ascending=False)
        fig = px.bar(cost_product, x="product", y="total_cost", title="Product-Wise Cost Allocation")
        st.plotly_chart(style_chart(fig), use_container_width=True)
    with col_b:
        cost_pie = pd.DataFrame({
            "Type": ["Fixed Cost", "Variable Cost", "Operating Expense"],
            "Amount": [fixed_cost, variable_cost, operating_expenses]
        })
        if cost_pie["Amount"].sum() > 0:
            fig = px.pie(cost_pie, names="Type", values="Amount", hole=0.45, title="Proportional Cost Distribution")
            st.plotly_chart(style_chart(fig), use_container_width=True)
        else:
            st.info("No cost data available for the selected filters.")

# ---------------- 3. PROFIT & LOSS ----------------
with tabs[2]:
    st.header("📈 Profit & Loss Trends")
    monthly_pl = filtered_df.groupby("month").agg(
        Sales=("sales", "sum"), Cost=("total_cost", "sum"),
        Gross_Profit=("gross_profit", "sum"), Net_Profit=("net_profit", "sum")
    ).reset_index()
    fig = px.line(monthly_pl, x="month", y=["Gross_Profit", "Net_Profit"], markers=True, title="Monthly Profit Trajectory")
    st.plotly_chart(style_chart(fig), use_container_width=True)

    product_profit = filtered_df.groupby("product").agg(
        Sales=("sales", "sum"), Cost=("total_cost", "sum"), Profit=("net_profit", "sum")
    ).reset_index()
    product_profit["Margin %"] = np.where(product_profit["Sales"] != 0, product_profit["Profit"] / product_profit["Sales"] * 100, 0)
    fig_bar = px.bar(product_profit, x="product", y="Profit", color="Profit", title="Product-Wise Net Profit / Loss", text_auto=".2s")
    st.plotly_chart(style_chart(fig_bar), use_container_width=True)

# ---------------- 4. PRODUCTS ----------------
with tabs[3]:
    st.header("🛒 Product Performance Rankings")
    product_table = filtered_df.groupby("product").agg(
        Qty=("quantity", "sum"), Sales=("sales", "sum"), Cost=("total_cost", "sum"), Profit=("net_profit", "sum")
    ).reset_index()
    product_table["Margin %"] = np.where(product_table["Sales"] != 0, (product_table["Profit"] / product_table["Sales"]) * 100, 0)
    avg_profit = product_table["Profit"].mean()
    product_table["Status"] = product_table.apply(
        lambda r: "Loss" if r["Profit"] < 0 else ("Star Performer" if r["Profit"] > avg_profit else "Normal"), axis=1
    )
    product_table = product_table.sort_values("Profit", ascending=False)

    st.dataframe(
        product_table.style.format({"Sales": "₹{:,.0f}", "Cost": "₹{:,.0f}", "Profit": "₹{:,.0f}", "Margin %": "{:.2f}%"}),
        use_container_width=True
    )
    fig = px.bar(product_table, x="product", y=["Sales", "Cost", "Profit"], barmode="group", title="Sales vs Cost vs Profit Comparison")
    st.plotly_chart(style_chart(fig), use_container_width=True)

# ---------------- 5. CUSTOMERS ----------------
with tabs[4]:
    st.header("👥 Customer Analytics & Retention")
    if customers_count == 1 and str(filtered_df["customer"].iloc[0]) == "Unknown":
        st.info("ℹ️ Your file has no `customer` column, so customer analytics are limited. Add a customer column for richer insights.")

    cust_summary = filtered_df.groupby("customer").agg(
        Orders=("order_id", "nunique"), Quantity=("quantity", "sum"),
        Sales=("sales", "sum"), Profit=("net_profit", "sum")
    ).reset_index()
    c_orders = cust_summary["Orders"]
    retention_pct = ((c_orders > 1).sum() / len(cust_summary) * 100) if len(cust_summary) > 0 else 0
    top10_share = (cust_summary.nlargest(10, "Sales")["Sales"].sum() / total_sales * 100) if total_sales else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("👥 Total Customers", f"{len(cust_summary):,}")
    c2.metric("🆕 New (1 order)", f"{(c_orders == 1).sum():,}")
    c3.metric("🔄 Returning", f"{(c_orders > 1).sum():,}")
    c4.metric("🔁 Retention", f"{retention_pct:.1f}%")
    c5.metric("🏅 Top-10 Share", f"{top10_share:.1f}%")

    top_cust = cust_summary.sort_values("Sales", ascending=False).head(10)
    fig = px.bar(top_cust, x="customer", y="Sales", title="Top 10 High-Value Customers")
    st.plotly_chart(style_chart(fig), use_container_width=True)

# ---------------- 6. TIME ----------------
with tabs[5]:
    st.header("⏱️ Multi-Horizon Time Analysis")
    freq = st.selectbox("Select Frequency Horizon", ["Daily", "Weekly", "Monthly", "Quarterly", "Yearly"], index=2)
    freq_map = {"Daily": "day", "Weekly": "week", "Monthly": "month", "Quarterly": "quarter", "Yearly": "year"}
    time_col = freq_map[freq]
    time_agg = filtered_df.groupby(time_col).agg(Sales=("sales", "sum"), Profit=("net_profit", "sum")).reset_index()
    fig = px.line(time_agg, x=time_col, y=["Sales", "Profit"], markers=True, title=f"{freq} Sales & Profit Movement")
    st.plotly_chart(style_chart(fig), use_container_width=True)

    # NEW: best weekday
    order_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    wd = (filtered_df.assign(weekday=filtered_df["date"].dt.day_name())
          .groupby("weekday")["sales"].sum().reindex(order_days).fillna(0).reset_index())
    fig = px.bar(wd, x="weekday", y="sales", title="Sales by Day of Week", text_auto=".2s")
    st.plotly_chart(style_chart(fig), use_container_width=True)
    if wd["sales"].sum() > 0:
        st.success(f"📅 Strongest day: **{wd.loc[wd['sales'].idxmax(), 'weekday']}** • Weakest day: **{wd.loc[wd['sales'].idxmin(), 'weekday']}**")

# ---------------- 7. GROWTH ----------------
with tabs[6]:
    st.header("📈 Month-over-Month Growth Analysis")
    growth_df = filtered_df.groupby("month").agg(Sales=("sales", "sum"), Profit=("net_profit", "sum")).reset_index()
    growth_df["Sales Growth %"] = growth_df["Sales"].pct_change().replace([np.inf, -np.inf], np.nan) * 100
    growth_df["Profit Growth %"] = growth_df["Profit"].pct_change().replace([np.inf, -np.inf], np.nan) * 100

    g1, g2, g3 = st.columns(3)
    g1.metric("Avg MoM Sales Growth", f"{growth_df['Sales Growth %'].mean():.2f}%" if len(growth_df) > 1 else "N/A")
    g2.metric("Best Growth Month", growth_df.loc[growth_df["Sales Growth %"].idxmax(), "month"].strftime("%b %Y") if growth_df["Sales Growth %"].notna().any() else "N/A")
    g3.metric("Weakest Growth Month", growth_df.loc[growth_df["Sales Growth %"].idxmin(), "month"].strftime("%b %Y") if growth_df["Sales Growth %"].notna().any() else "N/A")

    fig = px.line(growth_df, x="month", y=["Sales Growth %", "Profit Growth %"], markers=True, title="Monthly Percentage Growth Dynamics")
    st.plotly_chart(style_chart(fig), use_container_width=True)
    st.caption("ℹ️ If the last month in your data is incomplete, its growth % will look unusually low.")

# ---------------- 8. FORECAST ----------------
with tabs[7]:
    st.header("🔮 12-Month Predictive Forecasting")
    target = st.radio("Forecast target", ["Sales", "Net Profit"], horizontal=True)
    val_col, src_col = ("Sales", "sales") if target == "Sales" else ("Profit", "net_profit")

    m_forecast = filtered_df.groupby("month").agg(Sales=("sales", "sum"), Profit=("net_profit", "sum")).reset_index().sort_values("month")

    # An incomplete last month drags the trend down -> let the user exclude it
    last_day = filtered_df["date"].max().normalize()
    partial_last = last_day != (last_day + pd.offsets.MonthEnd(0)).normalize()
    if partial_last:
        excl = st.checkbox(f"Exclude incomplete last month ({last_day.strftime('%b %Y')}, data only till {last_day.strftime('%d %b')})", value=True)
        if excl:
            m_forecast = m_forecast[m_forecast["month"] < last_day.to_period("M").to_timestamp()]

    if len(m_forecast) < 6:
        st.warning("⚠️ At least 6 months of historical data is recommended for accurate forecasting.")
    else:
        if len(m_forecast) < 12:
            st.info("ℹ️ Less than 12 months of data: seasonality cannot be learned reliably, so the forecast is mostly trend-based.")

        fc = run_forecast(m_forecast, val_col, horizon=12, floor_zero=(val_col == "Sales"))

        c1, c2, c3 = st.columns(3)
        c1.metric(f"Next Month {target}", f"₹{fc.iloc[0]['Forecast']:,.0f}")
        c2.metric(f"Next 3 Months {target}", f"₹{fc['Forecast'].head(3).sum():,.0f}")
        c3.metric(f"Next 12 Months {target}", f"₹{fc['Forecast'].sum():,.0f}")

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=m_forecast["month"], y=m_forecast[val_col], mode="lines+markers", name="Historical"))
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["Upper"], mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["Lower"], mode="lines", line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(56,189,248,0.18)", name="Likely range (~80%)", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=fc["month"], y=fc["Forecast"], mode="lines+markers", name="Forecast", line=dict(dash="dash")))
        fig.update_layout(title=f"Historical & 12-Month Projected {target}")
        st.plotly_chart(style_chart(fig), use_container_width=True)

        fc_show = fc.assign(Month=fc["month"].dt.strftime("%b %Y"))[["Month", "Forecast", "Lower", "Upper"]]
        with st.expander("📋 View forecast table"):
            st.dataframe(fc_show.style.format({"Forecast": "₹{:,.0f}", "Lower": "₹{:,.0f}", "Upper": "₹{:,.0f}"}), use_container_width=True)
        st.download_button("📥 Download Forecast (CSV)", data=fc_show.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"GW_Forecast_{target.replace(' ', '_')}.csv", mime="text/csv")
        st.caption("Forecasts are statistical estimates (trend + seasonality), not guarantees.")

# ---------------- 9. RISK & ALERTS ----------------
with tabs[8]:
    st.header("🚨 Operational Risk & Vulnerability Alerts")
    risk_products = filtered_df.groupby("product").agg(Sales=("sales", "sum"), Profit=("net_profit", "sum")).reset_index()
    risk_products["Margin %"] = np.where(risk_products["Sales"] != 0, risk_products["Profit"] / risk_products["Sales"] * 100, 0)

    loss_items = risk_products[risk_products["Profit"] < 0]
    low_margin = risk_products[(risk_products["Margin %"] >= 0) & (risk_products["Margin %"] < 10)]

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("🔴 Loss-Making Products")
        if loss_items.empty:
            st.success("🎉 No loss-making products identified.")
        else:
            st.dataframe(loss_items.style.format({"Sales": "₹{:,.0f}", "Profit": "₹{:,.0f}", "Margin %": "{:.2f}%"}), use_container_width=True)
    with c2:
        st.subheader("🟠 Low Margin Items (<10%)")
        if low_margin.empty:
            st.success("🎉 All non-loss products maintain healthy margins (>10%).")
        else:
            st.dataframe(low_margin.style.format({"Sales": "₹{:,.0f}", "Profit": "₹{:,.0f}", "Margin %": "{:.2f}%"}), use_container_width=True)

    # NEW: loss-making orders + unusual transactions
    st.subheader("🧾 Loss-Making Orders")
    loss_orders = filtered_df[filtered_df["net_profit"] < 0]
    st.metric("Orders sold at a loss", f"{len(loss_orders):,}", f"{len(loss_orders) / len(filtered_df) * 100:.1f}% of all rows", delta_color="off")

    st.subheader("🔍 Unusual Transactions (beyond 2σ)")
    _m, _s = filtered_df["sales"].mean(), filtered_df["sales"].std()
    if pd.notna(_s) and _s > 0:
        odd = filtered_df[np.abs(filtered_df["sales"] - _m) > 2 * _s].nlargest(10, "sales")
        if odd.empty:
            st.success("🎉 No unusual transactions found.")
        else:
            st.dataframe(odd[["date", "order_id", "product", "customer", "quantity", "sales", "net_profit"]]
                         .style.format({"sales": "₹{:,.0f}", "net_profit": "₹{:,.0f}"}), use_container_width=True)
    else:
        st.info("Not enough variation to detect unusual transactions.")

# ---------------- 10. COMPREHENSIVE REPORT ----------------
with tabs[9]:
    st.header("📑 Comprehensive AI Analytics Intelligence Report")
    st.caption("Deep mathematical profiling, visual diagnostics with written explanations, and multi-page executive export.")

    detailed_stats = get_detailed_stats(filtered_df)

    md_html(f"""
    <div style="background:{T['panel']};border:1px solid {T['border']};border-radius:14px;padding:18px;margin-bottom:18px;line-height:1.7;">
    <h4 style="margin-bottom:8px;color:{T['accent']};">1. Executive Dataset Summary</h4>
    Analyzing <b>{len(filtered_df):,} records</b> across <b>{len(filtered_df.columns)} dimensions</b>. Cumulative revenue stands at
    <b>₹{total_sales:,.0f}</b> with <b>₹{net_profit:,.0f}</b> in bottom-line profits (Net Margin: <b>{profit_margin:.2f}%</b>).
    Category analysis shows <b>{_html.escape(str(detailed_stats['top_cat']))}</b> driving <b>{detailed_stats['top_cat_pct']:.1f}%</b> of all volume.
    Volatility is indexed at <b>{detailed_stats['volatility']:.1f}%</b> with <b>{detailed_stats['outlier_count']} outlier anomalies</b> detected.
    </div>
    """)

    st.subheader("2. Deep Statistical Indicators")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Sales Mean (Average)", f"₹{detailed_stats['mean_sales']:,.0f}", f"Median: ₹{detailed_stats['median_sales']:,.0f}", delta_color="off")
    s2.metric("Std Deviation (σ)", f"₹{detailed_stats['std_sales']:,.0f}", f"Volatility: {detailed_stats['volatility']:.1f}%", delta_color="off")
    s3.metric("Sales Range", f"₹{(detailed_stats['max_sales'] - detailed_stats['min_sales']):,.0f}", f"Max: ₹{detailed_stats['max_sales']:,.0f}", delta_color="off")
    s4.metric("Outlier Anomalies", f"{detailed_stats['outlier_count']}", "> 2σ Threshold", delta_color="off")

    st.subheader("3. Visual Diagnostics with Comprehensive Explanations")
    ui_charts = build_report_charts(filtered_df, T)

    col_t1, col_t2 = st.columns([1.4, 1])
    with col_t1:
        st.image(ui_charts["trend"], use_container_width=True)
    with col_t2:
        st.markdown(f"""
**📊 Detailed Trend Explanation:**
* **Direction:** Revenue trajectory across consecutive months (dashed line = 3-month average).
* **Baseline Anchor:** Average monthly turnover is ₹{detailed_stats['avg_monthly_sales']:,.0f}.
* **Best / Weakest Month:** {detailed_stats['best_month']} (₹{detailed_stats['best_month_sales']:,.0f}) vs {detailed_stats['worst_month']} (₹{detailed_stats['worst_month_sales']:,.0f}).
""")

    col_c1, col_c2 = st.columns([1.4, 1])
    with col_c1:
        st.image(ui_charts["category"], use_container_width=True)
    with col_c2:
        conc = ("High dependency on a single pillar; diversification is advised."
                if detailed_stats["top_cat_pct"] > 50 else "Balanced distribution observed across catalog.")
        st.markdown(f"""
**🥧 Detailed Category Share Explanation:**
* **Leading Segment:** *{detailed_stats['top_cat']}* commands **{detailed_stats['top_cat_pct']:.1f}%** of total sales.
* **Portfolio Concentration Risk:** {conc}
* **Actionable Takeaway:** Reallocate ad spend to nurture secondary categories into resilient revenue engines.
""")

    col_p1, col_p2 = st.columns([1.4, 1])
    with col_p1:
        st.image(ui_charts["products"], use_container_width=True)
    with col_p2:
        st.markdown(f"""
**📶 Detailed Product Performance Explanation:**
* **Revenue Anchor:** **{detailed_stats['best_product']}** leads with ₹{detailed_stats['best_sales']:,.0f} in revenue.
* **Pareto Ranking:** The top 3 products account for **{detailed_stats['top3_pct']:.1f}%** of total revenue.
* **Actionable Takeaway:** Protect inventory buffers on star performers while optimizing marketing spend for mid-tier products.
""")

    col_k1, col_k2 = st.columns([1.4, 1])
    with col_k1:
        st.image(ui_charts["cost"], use_container_width=True)
    with col_k2:
        st.markdown("""
**💰 Detailed Cost Structure Explanation:**
* **Fixed vs Variable:** Proportion of fixed commitments vs volume-dependent variable costs.
* **Operating Leverage:** High variable flexibility protects profit margins against cyclical demand shocks.
* **Actionable Takeaway:** Review recurring operating overheads to enhance overall operating margin efficiency.
""")

    st.markdown("---")
    st.subheader("📥 Download Full Comprehensive Report")
    st.success("✅ Free access — generate and download the full report PDF.")

    # PDF is built only when asked (it used to be rebuilt on EVERY click anywhere in the app)
    pdf_sig = (dataset_name, len(filtered_df), round(float(total_sales), 2), str(start_date), str(end_date))
    if st.button("⚙️ Generate PDF Report"):
        with st.spinner("Building your PDF report..."):
            pdf_charts = build_report_charts(filtered_df, THEMES["☀️ Crisp White"])   # light charts print better
            pdf_bytes = build_comprehensive_report_pdf(detailed_stats, pdf_charts, dataset_name, report_type=report_type)
            st.session_state["gw_pdf"] = {"sig": pdf_sig, "bytes": pdf_bytes}

    saved_pdf = st.session_state.get("gw_pdf")
    if saved_pdf and saved_pdf["sig"] == pdf_sig:
        safe_ds = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(dataset_name).stem)
        st.download_button(
            "📥 Download Comprehensive Intelligence Report (PDF)",
            data=saved_pdf["bytes"],
            file_name=f"GW_Comprehensive_Sales_Report_{safe_ds}.pdf",
            mime="application/pdf",
        )
    elif saved_pdf:
        st.info("ℹ️ Filters/data changed since the last PDF was generated. Click **Generate PDF Report** again.")

# ============================================================
# ACTIONABLE INSIGHTS & EXPORTS
# ============================================================
st.markdown("---")
st.header("💡 Actionable Business Insights")

p_insights = filtered_df.groupby("product").agg(Sales=("sales", "sum"), Profit=("net_profit", "sum")).reset_index()
best = p_insights.loc[p_insights["Profit"].idxmax()]
worst = p_insights.loc[p_insights["Profit"].idxmin()]
highest_sales = p_insights.loc[p_insights["Sales"].idxmax()]

i1, i2 = st.columns(2)
with i1:
    st.success(f"🏆 **Top Profit Driver:** {best['product']} (Net Profit: ₹{best['Profit']:,.0f})")
    st.info(f"💰 **Highest Revenue Product:** {highest_sales['product']} (Gross Sales: ₹{highest_sales['Sales']:,.0f})")
with i2:
    st.error(f"⚠️ **Lowest / Weakest Product:** {worst['product']} (Net Profit: ₹{worst['Profit']:,.0f})")
    st.warning("📊 Use the **'Comprehensive AI Report'** tab to view detailed statistical diagnostics and download the full executive briefing.")

with st.expander("👀 View Filtered Raw Dataset"):
    st.dataframe(filtered_df, use_container_width=True)
    _export = filtered_df.drop(columns=["day", "week", "quarter", "month_name"], errors="ignore")
    d1, d2 = st.columns(2)
    d1.download_button("📥 Download Filtered Data (CSV)", data=_export.to_csv(index=False).encode("utf-8-sig"),
                       file_name="GW_Filtered_Data.csv", mime="text/csv")
    try:
        _xl = BytesIO()
        with pd.ExcelWriter(_xl, engine="openpyxl") as writer:
            _export.to_excel(writer, index=False, sheet_name="Filtered Data")
        d2.download_button("📥 Download Filtered Data (Excel)", data=_xl.getvalue(),
                           file_name="GW_Filtered_Data.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception:
        d2.caption("Excel export needs `openpyxl` (pip install openpyxl).")

st.markdown("---")
st.caption("📊 GW Smart Analytics • Sales Intelligence & AI Forecasting Platform | Powered by Python, Streamlit & Scikit-learn")

# ============================================================
# GW FLOATING ASSISTANT (chat widget)
# ============================================================
# ctx["kpis"] gives the assistant real numbers, so it can answer from the live dashboard
# (useful for the GPT-powered Swati upgrade).
if render_gw_assistant is not None:
    try:
        render_gw_assistant(T, dict(
            dataset=dataset_name,
            rows=len(filtered_df),
            paid=st.session_state["payment_verified"],
            user=authenticated_user["name"] if authenticated_user else None,
            logo=ASSISTANT_LOGO_URI,
            kpis=dict(
                total_sales=round(float(total_sales), 2),
                net_profit=round(float(net_profit), 2),
                profit_margin_pct=round(float(profit_margin), 2),
                orders=int(orders),
                customers=int(customers_count),
                avg_order_value=round(float(avg_order_value), 2),
                top_product=str(highest_sales["product"]),
                weakest_product=str(worst["product"]),
                date_from=str(start_date),
                date_to=str(end_date),
            ),
        ))
    except Exception as _e:
        st.sidebar.error(f"🤖 Assistant error: {_e}")
else:
    st.sidebar.warning(
        "🤖 Assistant NOT loaded. Put gw_assistant.py in the same folder as hh.py "
        f"and restart Streamlit. Details: {_assistant_err}"
    )

# ============================================================
# PREDICTIVE & PRESCRIPTIVE ADD-ON
# ============================================================
with tabs[10]:
    st.header("🧠 Predictive & Prescriptive Analytics")
    st.caption("Separate add-on analytics; existing dashboard tabs, theme, chart styling and PDF remain unchanged.")

    st.subheader("🔮 Backtested, Multi-Model Forecast")
    granularity = st.selectbox(
        "Forecast breakdown", ["Overall", "Product", "Category", "Region"], key="ai_forecast_dimension"
    )
    dimension_columns = {"Product": "product", "Category": "category", "Region": "region"}
    forecast_scope = filtered_df
    if granularity != "Overall":
        dimension_col = dimension_columns[granularity]
        dimension_values = sorted(forecast_scope[dimension_col].astype(str).unique())
        chosen_dimension = st.selectbox(
            f"Select {granularity.lower()}", dimension_values, key="ai_forecast_value"
        )
        forecast_scope = forecast_scope[forecast_scope[dimension_col].astype(str) == chosen_dimension]

    horizon = st.select_slider(
        "Forecast horizon (months)", options=[3, 6, 12, 24], value=12, key="ai_forecast_horizon"
    )
    confidence = st.selectbox(
        "Confidence interval", [80, 90, 95], index=1, format_func=lambda value: f"{value}%",
        key="ai_confidence_level"
    )
    forecast_target = st.radio(
        "Forecast metric", ["Sales", "Quantity", "Profit"], horizontal=True, key="ai_forecast_target"
    )

    monthly_ai = forecast_scope.groupby("month").agg(
        Sales=("sales", "sum"), Quantity=("quantity", "sum"), Profit=("net_profit", "sum"),
        Avg_Selling_Price=("selling_price", "mean"), Discount=("discount", "sum"),
        Cost=("total_cost", "sum")
    ).reset_index()
    monthly_ai["discount_rate"] = monthly_ai["Discount"] / monthly_ai["Sales"].replace(0, np.nan)
    monthly_ai["cost_rate"] = monthly_ai["Cost"] / monthly_ai["Sales"].replace(0, np.nan)
    monthly_ai["profit_margin"] = monthly_ai["Profit"] / monthly_ai["Sales"].replace(0, np.nan)
    monthly_ai[["discount_rate", "cost_rate", "profit_margin"]] = monthly_ai[
        ["discount_rate", "cost_rate", "profit_margin"]
    ].replace([np.inf, -np.inf], np.nan).fillna(0)
    salary_rows = forecast_scope.assign(
        _month_end_sales=np.where(forecast_scope["date"].dt.day >= 25, forecast_scope["sales"], 0)
    ).groupby("month").agg(_sales=("sales", "sum"), _salary_sales=("_month_end_sales", "sum"))
    monthly_ai["salary_effect"] = monthly_ai["month"].map(
        (salary_rows["_salary_sales"] / salary_rows["_sales"].replace(0, np.nan)).fillna(0)
    ).fillna(0)
    monthly_ai["month"] = pd.to_datetime(monthly_ai["month"])
    forecast_columns = {"Sales": "Sales", "Quantity": "Quantity", "Profit": "Profit"}
    model_result = _cached_forecast_comparison(
        monthly_ai, forecast_columns[forecast_target], int(horizon), int(confidence)
    )
    model_comparison, forecast_ai, best_model, reliability, holdout_frame, feature_importance, holdout_months = model_result

    if len(monthly_ai) < 12:
        reliability = "Low"
        st.warning(
            f"Only {len(monthly_ai)} monthly observations are available. At least 12 months are recommended; "
            "seasonality and backtest reliability will be limited."
        )
    if len(monthly_ai) < 6:
        st.warning("Fewer than 6 months are available; the display uses a recent-average fallback where models cannot fit.")
    badge = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(reliability, "⚪")
    metric_suffix = "₹" if forecast_target != "Quantity" else ""
    top_metrics = st.columns(4)
    top_metrics[0].metric("Best Model", best_model)
    top_metrics[1].metric("Forecast Reliability", f"{badge} {reliability}")
    top_metrics[2].metric(f"Next Month {forecast_target}", f"{metric_suffix}{forecast_ai.iloc[0]['Forecast']:,.0f}")
    top_metrics[3].metric(f"Next {horizon} Months", f"{metric_suffix}{forecast_ai['Forecast'].sum():,.0f}")

    comparison_view = model_comparison.copy()
    st.dataframe(
        comparison_view.style.format({
            "MAPE %": "{:.2f}%", "MAE": "{:,.2f}", "RMSE": "{:,.2f}", "WAPE %": "{:.2f}%"
        }, na_rep="N/A"),
        use_container_width=True
    )
    st.caption(f"Backtest held out {holdout_months} final month(s). Holiday month indicators and the observed month-end salary-sales share are model inputs.")

    plot_history = monthly_ai[["month", forecast_target]].rename(columns={forecast_target: "Actual"})
    forecast_fig = go.Figure()
    forecast_fig.add_trace(go.Scatter(x=plot_history["month"], y=plot_history["Actual"], mode="lines+markers", name="History"))
    forecast_fig.add_trace(go.Scatter(x=forecast_ai["month"], y=forecast_ai["Upper"], mode="lines", line=dict(width=0),
                                      showlegend=False, hoverinfo="skip"))
    forecast_fig.add_trace(go.Scatter(x=forecast_ai["month"], y=forecast_ai["Lower"], mode="lines", line=dict(width=0),
                                      fill="tonexty", fillcolor="rgba(56,189,248,0.18)",
                                      name=f"{confidence}% interval", hoverinfo="skip"))
    forecast_fig.add_trace(go.Scatter(x=forecast_ai["month"], y=forecast_ai["Forecast"], mode="lines+markers",
                                      name=f"Forecast • {best_model}", line=dict(dash="dash")))
    forecast_fig.update_layout(title=f"{granularity} {forecast_target} Forecast")
    st.plotly_chart(style_chart(forecast_fig), use_container_width=True)
    forecast_export = forecast_ai.assign(Month=forecast_ai["month"].dt.strftime("%b %Y"))[
        ["Month", "Forecast", "Lower", "Upper"]
    ]
    forecast_export["Model"] = best_model
    fcsv, fxlsx = st.columns(2)
    fcsv.download_button(
        "📥 Forecast CSV", forecast_export.to_csv(index=False).encode("utf-8-sig"),
        "GW_Advanced_Forecast.csv", "text/csv", key="advanced_forecast_csv"
    )
    try:
        fxlsx.download_button(
            "📥 Forecast Excel", _excel_bytes({"Forecast": forecast_export}),
            "GW_Advanced_Forecast.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="advanced_forecast_xlsx"
        )
    except Exception as export_error:
        fxlsx.warning(f"Excel export unavailable: {export_error}")

    st.subheader("📉 Seasonality Decomposition")
    decomposition = monthly_ai[["month", forecast_target]].copy()
    values = decomposition[forecast_target].astype(float)
    if len(values) >= 3:
        if STL is not None and len(values) >= 24:
            parts = STL(values, period=12, robust=True).fit()
            decomposition["Trend"] = parts.trend
            decomposition["Seasonal"] = parts.seasonal
            decomposition["Residual"] = parts.resid
        else:
            window = min(3, len(values) if len(values) % 2 else len(values) - 1)
            decomposition["Trend"] = values.rolling(window=max(1, window), center=True, min_periods=1).mean()
            detrended = values - decomposition["Trend"]
            decomposition["Seasonal"] = detrended.groupby(decomposition["month"].dt.month).transform("mean")
            decomposition["Residual"] = values - decomposition["Trend"] - decomposition["Seasonal"]
        season_profile = decomposition.groupby(decomposition["month"].dt.month)["Seasonal"].mean()
        month_labels = {i: datetime(2000, i, 1).strftime("%B") for i in range(1, 13)}
        peak_month = month_labels[int(season_profile.idxmax())]
        weak_month = month_labels[int(season_profile.idxmin())]
        st.info(f"Peak seasonal month: **{peak_month}** • Weak seasonal month: **{weak_month}**")
        decomp_long = decomposition.melt(id_vars="month", value_vars=["Trend", "Seasonal", "Residual"],
                                         var_name="Component", value_name="Value")
        decomp_fig = px.line(decomp_long, x="month", y="Value", color="Component",
                             title="Trend + Seasonal + Residual")
        st.plotly_chart(style_chart(decomp_fig), use_container_width=True)
    else:
        st.info("At least 3 monthly observations are needed for decomposition.")

    st.subheader("🎯 Sales Target Probability")
    target_value = st.number_input(
        f"Target for the next {horizon} months ({'units' if forecast_target == 'Quantity' else '₹'})",
        min_value=0.0, value=float(max(0, forecast_ai["Forecast"].sum())), step=100.0,
        key="ai_sales_target"
    )
    forecast_total = float(forecast_ai["Forecast"].sum())
    forecast_sigma = float(model_comparison.loc[model_comparison["Model"] == best_model, "RMSE"].iloc[0]) \
        if best_model in model_comparison["Model"].values and pd.notna(
            model_comparison.loc[model_comparison["Model"] == best_model, "RMSE"].iloc[0]
        ) else 0.0
    total_sigma = max(forecast_sigma * np.sqrt(horizon), 1e-9)
    target_probability = 100 * (1 - 0.5 * (1 + math.erf((target_value - forecast_total) / total_sigma / np.sqrt(2))))
    st.metric("Estimated target-achievement probability", f"{np.clip(target_probability, 0, 100):.1f}%")
    st.caption("Probability is an approximation based on backtest RMSE; it is not a guarantee.")

    st.subheader("🌱 Next-Quarter Product Outlook")
    quarter_outlook = _cached_product_quarter_outlook(filtered_df)
    growing = quarter_outlook.head(5).copy()
    declining = quarter_outlook.sort_values("Predicted Growth %").head(5).copy()
    outlook_left, outlook_right = st.columns(2)
    with outlook_left:
        st.markdown("**Top 5 predicted growers**")
        st.dataframe(growing, use_container_width=True)
    with outlook_right:
        st.markdown("**Top 5 predicted decliners**")
        st.dataframe(declining, use_container_width=True)

    st.subheader("🧪 What-if Scenario Simulator")
    sliders = st.columns(4)
    price_change = sliders[0].slider("Price change %", -30, 30, 0, key="ai_price_change")
    discount_change = sliders[1].slider("Discount change %", -50, 100, 0, key="ai_discount_change")
    cost_change = sliders[2].slider("Cost change %", -30, 30, 0, key="ai_cost_change")
    demand_growth = sliders[3].slider("Demand growth %", -50, 100, 0, key="ai_demand_growth")
    quantity_comparison = _cached_forecast_comparison(monthly_ai, "Quantity", int(horizon), int(confidence))
    quantity_forecast = quantity_comparison[1]["Forecast"].to_numpy(dtype=float)
    if forecast_target == "Quantity":
        quantity_forecast = forecast_ai["Forecast"].to_numpy(dtype=float)
    revenue_forecast = quantity_forecast * float(
        forecast_scope["selling_price"].replace([np.inf, -np.inf], np.nan).dropna().mean()
        if forecast_scope["selling_price"].notna().any() else 0
    )
    avg_price = float(forecast_scope["selling_price"].mean())
    avg_cost = float(forecast_scope["cost_price"].mean())
    avg_discount_per_unit = float(forecast_scope["discount"].sum() / max(forecast_scope["quantity"].sum(), 1))
    avg_opex_per_unit = float(forecast_scope["operating_expense"].sum() / max(forecast_scope["quantity"].sum(), 1))
    base_quantity = quantity_forecast
    scenario_quantity = np.maximum(base_quantity * (1 + demand_growth / 100), 0)
    scenario_price = avg_price * (1 + price_change / 100)
    scenario_cost = avg_cost * (1 + cost_change / 100)
    scenario_discount = avg_discount_per_unit * (1 + discount_change / 100)
    base_revenue = base_quantity * avg_price
    scenario_revenue = scenario_quantity * scenario_price
    base_profit = base_quantity * (avg_price - avg_cost - avg_discount_per_unit - avg_opex_per_unit)
    scenario_profit = scenario_quantity * (scenario_price - scenario_cost - scenario_discount - avg_opex_per_unit)
    scenario_export = pd.DataFrame({
        "Month": forecast_ai["month"].dt.strftime("%b %Y"),
        "Base Revenue": base_revenue, "Scenario Revenue": scenario_revenue,
        "Base Profit": base_profit, "Scenario Profit": scenario_profit,
        "Profit Impact": scenario_profit - base_profit,
    })
    scenario_metrics = st.columns(3)
    scenario_metrics[0].metric("Base forecast profit", f"₹{base_profit.sum():,.0f}")
    scenario_metrics[1].metric("Scenario profit", f"₹{scenario_profit.sum():,.0f}")
    scenario_metrics[2].metric("Profit impact", f"₹{(scenario_profit - base_profit).sum():,.0f}")
    scenario_chart_data = pd.DataFrame({
        "Month": forecast_ai["month"], "Base": base_profit, "Expected": scenario_profit,
        "Best": scenario_quantity * (avg_price * (1 + max(price_change, 5) / 100)
             - avg_cost * (1 + min(cost_change, 0) / 100) - avg_discount_per_unit * (1 + min(discount_change, 0) / 100)
             - avg_opex_per_unit),
        "Worst": np.maximum(base_quantity * 0.8, 0) * (
            avg_price * (1 + min(price_change, -5) / 100) - avg_cost * (1 + max(cost_change, 0) / 100)
            - avg_discount_per_unit * (1 + max(discount_change, 0) / 100) - avg_opex_per_unit
        ),
    }).melt(id_vars="Month", var_name="Scenario", value_name="Profit")
    scenario_fig = px.line(scenario_chart_data, x="Month", y="Profit", color="Scenario",
                           title="Base vs Scenario • Best / Expected / Worst Profit")
    st.plotly_chart(style_chart(scenario_fig), use_container_width=True)
    scenario_downloads = st.columns(2)
    scenario_downloads[0].download_button(
        "📥 Scenario CSV", scenario_export.to_csv(index=False).encode("utf-8-sig"),
        "GW_Scenario.csv", "text/csv", key="advanced_scenario_csv"
    )
    try:
        scenario_downloads[1].download_button(
            "📥 Scenario Excel", _excel_bytes({"Scenario": scenario_export}),
            "GW_Scenario.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="advanced_scenario_xlsx"
        )
    except Exception as export_error:
        scenario_downloads[1].warning(f"Excel export unavailable: {export_error}")

    st.subheader("⚖️ Break-even Analysis & Discount Elasticity")
    unit_economics = filtered_df.groupby("product").agg(
        Avg_Price=("selling_price", "mean"), Avg_Cost=("cost_price", "mean"),
        Avg_Discount=("discount", "sum"), Units=("quantity", "sum"),
        Operating_Expense=("operating_expense", "sum")
    ).reset_index()
    unit_economics["Discount / Unit"] = unit_economics["Avg_Discount"] / unit_economics["Units"].clip(lower=1)
    unit_economics["Operating Expense / Unit"] = unit_economics["Operating_Expense"] / unit_economics["Units"].clip(lower=1)
    unit_economics["Contribution / Unit"] = (
        unit_economics["Avg_Price"] - unit_economics["Avg_Cost"] -
        unit_economics["Discount / Unit"]
    )
    unit_economics["Break-even Units"] = np.where(
        unit_economics["Contribution / Unit"] > 0,
        np.ceil(unit_economics["Operating_Expense"] / unit_economics["Contribution / Unit"]), np.nan
    )
    unit_economics["Break-even Revenue"] = unit_economics["Break-even Units"] * unit_economics["Avg_Price"]
    st.dataframe(unit_economics[["product", "Contribution / Unit", "Break-even Units", "Break-even Revenue"]]
                 .style.format({"Contribution / Unit": "₹{:,.2f}", "Break-even Revenue": "₹{:,.0f}"}),
                 use_container_width=True)
    effective_price = (
        filtered_df["selling_price"] - filtered_df["discount"] / filtered_df["quantity"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)
    valid_elasticity = (effective_price > 0) & (filtered_df["quantity"] > 0)
    if valid_elasticity.sum() >= 3 and effective_price[valid_elasticity].nunique() > 1:
        elasticity = float(np.polyfit(
            np.log(effective_price[valid_elasticity]),
            np.log(filtered_df.loc[valid_elasticity, "quantity"]), 1
        )[0])
        st.caption(f"Observed log(quantity)-log(effective price) elasticity: {elasticity:.2f}. Directional association only; this is not causal.")
        discount_steps = pd.DataFrame({"Discount increase (percentage points)": [0, 5, 10, 15, 20]})
        current_discount_rate = float((filtered_df["discount"].sum() / max(filtered_df["sales"].sum(), 1)))
        discount_steps["Estimated quantity change %"] = [
            0 if current_discount_rate >= 0.99 else
            ((max(0.01, 1 - current_discount_rate + increase / 100) / max(0.01, 1 - current_discount_rate)) ** elasticity - 1) * 100
            for increase in discount_steps["Discount increase (percentage points)"]
        ]
        discount_steps["Estimated profit change %"] = (
            (1 + discount_steps["Estimated quantity change %"] / 100) *
            (1 - current_discount_rate - discount_steps["Discount increase (percentage points)"] / 100) /
            max(1e-9, 1 - current_discount_rate) - 1
        ) * 100
        st.dataframe(discount_steps, use_container_width=True)
    else:
        st.info("Not enough variation in price and quantity to estimate discount elasticity.")

    st.subheader("📦 Inventory & Prescriptive Actions")
    lead_days = st.slider("Supplier lead time (days)", 7, 60, 14, key="ai_lead_days")
    service_z = {80: 0.842, 90: 1.282, 95: 1.645}[int(confidence)]
    inventory_rows = []
    stock_col = next((name for name in ("stock_on_hand", "current_stock", "stock") if name in filtered_df), None)
    daily = filtered_df.set_index("date").groupby("product")["quantity"].resample("D").sum().reset_index()
    for product, frame in daily.groupby("product"):
        mean_demand = float(frame["quantity"].mean())
        std_demand = float(frame["quantity"].std()) if len(frame) > 1 else 0.0
        safety_stock = service_z * std_demand * np.sqrt(lead_days)
        reorder_point = mean_demand * lead_days + safety_stock
        if stock_col:
            stock_level = float(pd.to_numeric(filtered_df.loc[filtered_df["product"] == product, stock_col],
                                               errors="coerce").dropna().iloc[-1]) if pd.to_numeric(
                filtered_df.loc[filtered_df["product"] == product, stock_col], errors="coerce"
            ).notna().any() else np.nan
            coverage_days = stock_level / mean_demand if mean_demand > 0 and np.isfinite(stock_level) else np.nan
            stock_basis = "Uploaded stock"
        else:
            latest = filtered_df.loc[filtered_df["product"] == product, "date"].max()
            stock_level = float(filtered_df.loc[
                (filtered_df["product"] == product) &
                (filtered_df["date"] > latest - pd.Timedelta(days=30)), "quantity"
            ].sum())
            coverage_days = stock_level / mean_demand if mean_demand > 0 else np.nan
            stock_basis = "30-day sales proxy"
        inventory_rows.append({
            "Product": product, "Avg daily units": mean_demand, "Safety stock": safety_stock,
            "Reorder point": reorder_point, "Estimated stock": stock_level, "Coverage days": coverage_days,
            "Stock basis": stock_basis,
            "Stock-out risk": "High" if coverage_days < lead_days else ("Medium" if coverage_days < lead_days * 1.5 else "Low"),
            "Overstock risk": "High" if coverage_days > 90 else ("Medium" if coverage_days > 60 else "Low"),
        })
    inventory_table = pd.DataFrame(inventory_rows)
    st.dataframe(inventory_table, use_container_width=True)
    if not stock_col:
        st.caption("No on-hand inventory column found; risk and stock estimates use a labelled recent-sales proxy.")

    abcxyz = filtered_df.groupby(["product", "month"]).agg(Sales=("sales", "sum"), Quantity=("quantity", "sum")).reset_index()
    abc = abcxyz.groupby("product")["Sales"].sum().sort_values(ascending=False)
    cumulative = abc.cumsum() / max(abc.sum(), 1)
    abc_labels = pd.Series(np.where(cumulative <= 0.8, "A", np.where(cumulative <= 0.95, "B", "C")), index=abc.index)
    demand_cv = abcxyz.pivot(index="product", columns="month", values="Quantity").fillna(0).apply(
        lambda values: values.std() / values.mean() if values.mean() else 0, axis=1
    )
    classification = pd.DataFrame({
        "Product": abc.index, "ABC": abc_labels.reindex(abc.index).values,
        "XYZ": demand_cv.reindex(abc.index).fillna(0).map(
            lambda value: "X" if value <= 0.5 else ("Y" if value <= 1 else "Z")
        ).values, "Sales": abc.values,
    })
    st.markdown("**ABC–XYZ value / demand variability classes**")
    st.dataframe(classification, use_container_width=True)

    latest_month = filtered_df["month"].max()
    recent_months = pd.date_range(end=latest_month, periods=4, freq="MS")
    alert_rows = []
    for dimension in ("product", "region", "customer"):
        series = filtered_df.groupby([dimension, "month"])["sales"].sum().reset_index()
        for label, frame in series.groupby(dimension):
            frame = frame.set_index("month")["sales"].reindex(recent_months, fill_value=0)
            baseline = float(frame.iloc[:3].mean())
            recent = float(frame.iloc[-1])
            if baseline > 0 and (recent < baseline * 0.7 or recent > baseline * 1.3):
                direction = "drop" if recent < baseline else "spike"
                alert_rows.append({"Type": dimension.title(), "Name": str(label), "Alert": direction,
                                   "Latest sales": recent, "Prior-3-month average": baseline,
                                   "Reason hint": f"{dimension} monthly sales shifted {((recent / baseline) - 1) * 100:.0f}%"})
    st.markdown("**Sudden sales drops / spikes by product, region or customer**")
    st.dataframe(pd.DataFrame(alert_rows) if alert_rows else pd.DataFrame(
        columns=["Type", "Name", "Alert", "Latest sales", "Prior-3-month average", "Reason hint"]),
        use_container_width=True
    )

    margin_monthly = filtered_df.groupby(["product", "month"]).agg(
        Sales=("sales", "sum"), Profit=("net_profit", "sum")
    ).reset_index()
    margin_monthly["Margin %"] = np.where(
        margin_monthly["Sales"] != 0, margin_monthly["Profit"] / margin_monthly["Sales"] * 100, np.nan
    )
    erosion = []
    for product, frame in margin_monthly.groupby("product"):
        frame = frame.sort_values("month").tail(3)
        if len(frame) == 3 and frame["Margin %"].notna().all():
            if frame["Margin %"].iloc[-1] < frame["Margin %"].iloc[0] and np.polyfit(
                np.arange(3), frame["Margin %"].to_numpy(), 1
            )[0] < 0:
                erosion.append({"Product": product, "Start margin %": frame["Margin %"].iloc[0],
                                "Latest margin %": frame["Margin %"].iloc[-1], "Alert": "3-month erosion"})
    st.markdown("**Margin erosion alerts (3 consecutive observed months)**")
    st.dataframe(pd.DataFrame(erosion) if erosion else pd.DataFrame(
        columns=["Product", "Start margin %", "Latest margin %", "Alert"]
    ), use_container_width=True)

    product_health = filtered_df.groupby("product").agg(
        Sales=("sales", "sum"), Profit=("net_profit", "sum")
    ).join(quarter_outlook.set_index("Product")[["Predicted Growth %"]])
    actions = []
    for product, row in product_health.iterrows():
        if row["Profit"] < 0 and row["Predicted Growth %"] < -10:
            actions.append({"Product": product, "Recommended action": "Review discontinuation; persistent loss and declining outlook"})
        elif row["Profit"] > 0 and row["Predicted Growth %"] > 10:
            actions.append({"Product": product, "Recommended action": "Push / promote and protect inventory"})
        elif row["Profit"] > 0 and row["Sales"] > 0 and row["Profit"] / row["Sales"] < 0.1:
            actions.append({"Product": product, "Recommended action": "Review price increase, supplier cost and discounts"})
    st.markdown("**Recommended Actions**")
    st.dataframe(pd.DataFrame(actions) if actions else pd.DataFrame(
        columns=["Product", "Recommended action"]), use_container_width=True)

    st.subheader("👥 Customer RFM, CLV, Churn & Cross-sell")
    customer_insights, segment_summary, churn_training, churn_ranked = _cached_customer_insights(filtered_df)
    if customer_insights["customer"].nunique() <= 1 and str(customer_insights["customer"].iloc[0]) == "Unknown":
        st.info("Customer intelligence is limited because this dataset has no customer IDs.")
    st.dataframe(segment_summary, use_container_width=True)
    st.caption("CLV is an annualized historical-spend estimate; churn risk uses a 90-day return classifier when enough history exists, otherwise a recency fallback.")
    st.markdown("**Customers with highest estimated 60–90 day non-return risk**")
    customer_export = churn_ranked[[
        "customer", "Segment", "Cluster", "Recency", "Frequency", "Monetary",
        "Estimated CLV", "Churn Risk %", "Churn Model"
    ]].head(100)
    st.dataframe(customer_export, use_container_width=True)
    customer_downloads = st.columns(2)
    customer_downloads[0].download_button(
        "📥 RFM CSV", customer_export.to_csv(index=False).encode("utf-8-sig"),
        "GW_RFM_Customers.csv", "text/csv", key="advanced_rfm_csv"
    )
    try:
        customer_downloads[1].download_button(
            "📥 RFM Excel", _excel_bytes({"RFM": customer_export, "Segments": segment_summary}),
            "GW_RFM_Customers.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="advanced_rfm_xlsx"
        )
    except Exception as export_error:
        customer_downloads[1].warning(f"Excel export unavailable: {export_error}")

    basket_rules = _cached_basket_rules(filtered_df)
    st.markdown("**Order-level market basket / cross-sell suggestions**")
    if basket_rules.empty:
        st.info("No order IDs with multiple distinct products were found.")
    else:
        st.dataframe(basket_rules, use_container_width=True)

    st.subheader("🚨 IsolationForest Anomaly Detection")
    anomaly_data = _cached_anomaly_scores(filtered_df)
    anomaly_counts = st.columns(2)
    anomaly_counts[0].metric("IsolationForest anomalies", f"{int(anomaly_data['IF_Anomaly'].sum()):,}")
    anomaly_counts[1].metric("Existing 2σ anomalies", f"{int(anomaly_data['2-Sigma Anomaly'].sum()):,}")
    anomaly_rows = anomaly_data[anomaly_data["IF_Anomaly"] | anomaly_data["2-Sigma Anomaly"]].sort_values(
        "IF_Score", ascending=False
    ).head(50)
    st.dataframe(anomaly_rows[[
        "date", "product", "region", "customer", "sales", "quantity", "profit_margin",
        "IF_Anomaly", "IF_Score", "2-Sigma Anomaly"
    ]], use_container_width=True)

    st.subheader("🧹 Data Quality")
    raw_quality = standardize_columns(raw_data)
    unique_dates = pd.Series(pd.to_datetime(filtered_df["date"], errors="coerce").dropna().dt.normalize().unique()).sort_values()
    date_gaps = int((unique_dates.diff().dt.days > 1).sum()) if len(unique_dates) > 1 else 0
    raw_price_cols = [name for name in ("selling_price", "cost_price") if name in raw_quality]
    negative_price_count = int(sum(
        (pd.to_numeric(raw_quality[name], errors="coerce") < 0).sum() for name in raw_price_cols
    ))
    quality_table = pd.DataFrame([
        {"Check": "Raw missing values", "Count": int(raw_quality.isna().sum().sum())},
        {"Check": "Duplicate raw rows", "Count": int(raw_quality.duplicated().sum())},
        {"Check": "Negative selling / cost prices", "Count": negative_price_count},
        {"Check": "Observed date gaps (>1 day)", "Count": date_gaps},
        {"Check": "Rows dropped for invalid date / product", "Count": int(len(raw_quality) - len(df))},
    ])
    st.dataframe(quality_table, use_container_width=True)

    st.subheader("📊 Forecast Feature Importance")
    importance_to_show = feature_importance
    if importance_to_show.empty and len(monthly_ai) >= 3:
        try:
            future_month = pd.date_range(monthly_ai["month"].max() + pd.DateOffset(months=1), periods=1, freq="MS")
            _, importance_to_show = _forecast_one_model(
                monthly_ai, future_month, "Gradient Boosting", forecast_columns[forecast_target]
            )
        except Exception as importance_error:
            st.info(f"Feature importance unavailable: {importance_error}")
    if not importance_to_show.empty:
        importance_fig = px.bar(importance_to_show, x="Importance", y="Feature", orientation="h",
                                title="Relative importance of calendar / festival / salary factors")
        st.plotly_chart(style_chart(importance_fig), use_container_width=True)
    else:
        st.info("Feature importance needs more monthly observations.")

    st.subheader("💡 AI Insight Summary")
    summary_language = st.radio("Summary language", ["English", "मराठी"], horizontal=True, key="ai_summary_language")
    growth_leader = quarter_outlook.iloc[0]["Product"] if not quarter_outlook.empty else "N/A"
    risk_customers = int((churn_ranked["Churn Risk %"] >= 60).sum()) if not churn_ranked.empty else 0
    if summary_language == "मराठी":
        insight_summary = (
            f"सर्वोत्तम मॉडेल {best_model} असून forecast reliability {reliability} आहे. "
            f"पुढील {horizon} महिन्यांचा अपेक्षित {forecast_target} {forecast_total:,.0f} आहे. "
            f"पुढील तिमाहीत {growth_leader} हा वाढीचा आघाडीचा उत्पादन संकेत आहे; "
            f"{risk_customers} ग्राहकांचा अंदाजित churn risk 60% किंवा अधिक आहे. "
            "स्टॉक/किंमत निर्णय घेण्यापूर्वी forecast intervals आणि उपलब्ध inventory तपासा."
        )
    else:
        insight_summary = (
            f"{best_model} is the best model on the held-out backtest ({reliability} reliability). "
            f"Expected {forecast_target.lower()} over the next {horizon} months is {forecast_total:,.0f}. "
            f"{growth_leader} leads the next-quarter growth outlook; {risk_customers} customers have an estimated "
            "churn risk of at least 60%. Check the forecast interval and actual stock before acting."
        )
    st.info(insight_summary)

    st.subheader("📄 Predictive / Scenario / Customer PDF Addendum")
    try:
        predictive_pdf = _build_predictive_pdf(
            forecast_export, scenario_export, customer_export.head(20), insight_summary, dataset_name
        )
        st.download_button(
            "📥 Download Predictive Insights PDF", predictive_pdf,
            "GW_Predictive_Scenario_Customer_Insights.pdf", "application/pdf",
            key="predictive_pdf_addendum"
        )
    except Exception as pdf_error:
        st.error(f"Could not build the predictive PDF addendum: {pdf_error}")