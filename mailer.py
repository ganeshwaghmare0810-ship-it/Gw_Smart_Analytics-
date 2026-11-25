"""
GW Analytics - Email Notification Dispatcher
============================================
Handles automated notifications:
- Login Alert: "You have successfully logged in to Gw Smart Analytics. Welcome to Gw Smart Analytics."
- Payment Confirmation: "You have successfully bought Gw Smart Analytics subscription. Enjoy your journey."

Features:
- Non-blocking background thread dispatch
- Persistent file audit logging (email_notifications.log)
- SMTP support with automatic fallback to local log delivery
"""

import os
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional, Dict, Any

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

LOG_FILE = BASE_DIR / "email_notifications.log"

# SMTP Configuration (Can be customized via environment variables)
SMTP_HOST = os.getenv("GW_SMTP_HOST") or os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("GW_SMTP_PORT") or os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("GW_SMTP_USER") or os.getenv("SMTP_USERNAME", "")
SMTP_PASS = os.getenv("GW_SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("GW_SENDER_EMAIL") or SMTP_USER
SENDER_NAME = "GW Smart Analytics"
SUPPORT_EMAIL = "gwsmartanalytics0810@gmail.com"


def send_contact_message(name: str, sender_email: str, subject: str, message: str):
    """Delivers a contact form submission to the configured support inbox."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("Email delivery is not configured. Copy .env.example to .env and set SMTP_USERNAME and SMTP_PASSWORD.")

    mail = EmailMessage()
    mail["Subject"] = f"[GW Smart Analytics] {subject}"
    mail["From"] = formataddr((SENDER_NAME, SENDER_EMAIL or SMTP_USER))
    mail["To"] = SUPPORT_EMAIL
    mail["Reply-To"] = sender_email
    mail.set_content(
        f"New message from the GW Smart Analytics contact form.\n\n"
        f"Name: {name}\n"
        f"Reply email: {sender_email}\n"
        f"Subject: {subject}\n\n"
        f"Message:\n{message}\n"
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(mail)
    except (OSError, smtplib.SMTPException) as error:
        raise RuntimeError("SMTP could not send the contact message.") from error

    _log_email_to_file(SUPPORT_EMAIL, mail["Subject"], mail.get_content(), "SENT via SMTP")


def _log_email_to_file(to_email: str, subject: str, body_text: str, status: str):
    """Appends dispatched email details to email_notifications.log for verification."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{status}]\n")
            f.write(f"TO: {to_email}\n")
            f.write(f"SUBJECT: {subject}\n")
            f.write(f"MESSAGE:\n{body_text.strip()}\n")
            f.write("-" * 60 + "\n")
    except Exception as e:
        print(f"[Mailer Log Error] {e}")


def _dispatch_worker(to_email: str, subject: str, body_text: str, body_html: Optional[str] = None):
    """Background worker that attempts SMTP transmission, falling back to audit logging."""
    if not to_email:
        return

    print(f"\n📧 [GW EMAIL DISPATCH] Sending to: {to_email}")
    print(f"   Subject: {subject}")
    print(f"   Content: {body_text.strip()[:100]}...\n")

    # If SMTP is configured, attempt real transmission
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
            msg["To"] = to_email

            part1 = MIMEText(body_text, "plain", "utf-8")
            msg.attach(part1)

            if body_html:
                part2 = MIMEText(body_html, "html", "utf-8")
                msg.attach(part2)

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SENDER_EMAIL, [to_email], msg.as_string())

            _log_email_to_file(to_email, subject, body_text, "SENT via SMTP")
            print(f"✅ [GW EMAIL] Successfully delivered via SMTP to {to_email}")
            return
        except Exception as e:
            print(f"⚠️ [GW EMAIL SMTP Warning] Delivery failed: {e}. Saved to local audit log.")

    # Fallback to local audit log
    _log_email_to_file(to_email, subject, body_text, "DISPATCHED (Audit Log)")


def send_async_email(to_email: str, subject: str, body_text: str, body_html: Optional[str] = None):
    """Spawns an asynchronous daemon thread to deliver email without blocking web request."""
    thread = threading.Thread(
        target=_dispatch_worker,
        args=(to_email, subject, body_text, body_html),
        daemon=True
    )
    thread.start()


# =====================================================================
# HIGH-LEVEL NOTIFICATION TRIGGERS
# =====================================================================

def send_login_notification(user_name: str, to_email: str):
    """
    Sends automatic login message:
    "You have successfully logged in to Gw Smart Analytics. Welcome to Gw Smart Analytics."
    """
    subject = "Login Alert - Gw Smart Analytics"

    body_text = f"""Hello {user_name},

You have successfully logged in to Gw Smart Analytics. Welcome to Gw Smart Analytics.

Your session is now active. You can explore dashboards, analytics, and sales intelligence tools.

Date & Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}
Platform: GW Smart Analytics (http://127.0.0.1:5000)

If this wasn't you, please secure your account immediately.

Warm Regards,
Team GW Smart Analytics
"""

    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 580px; margin: auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background: #ffffff;">
        <h2 style="color: #1e3a8a; margin-top: 0;">GW Smart Analytics</h2>
        <div style="padding: 16px; background: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 6px; margin-bottom: 20px;">
            <p style="margin: 0; font-size: 16px; color: #166534; font-weight: bold;">
                You have successfully logged in to Gw Smart Analytics. Welcome to Gw Smart Analytics.
            </p>
        </div>
        <p style="color: #334155; line-height: 1.6;">
            Hello <b>{user_name}</b>,<br>
            Your session is active. You have full access to view dashboards, prepare datasets, and explore sales intelligence tools.
        </p>
        <p style="font-size: 13px; color: #64748b;">
            <b>Time:</b> {datetime.now().strftime('%d %b %Y, %I:%M %p')}<br>
            <b>Portal:</b> <a href="http://127.0.0.1:5000">GW Smart Analytics</a>
        </p>
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
        <p style="font-size: 12px; color: #94a3b8; margin: 0;">
            This is an automated security alert from GW Smart Analytics.
        </p>
    </div>
    """

    send_async_email(to_email, subject, body_text, body_html)


def send_payment_notification(user_name: str, to_email: str, receipt: Dict[str, Any]):
    """
    Sends automatic payment success message:
    "You have successfully bought Gw Smart Analytics subscription. Enjoy your journey."
    """
    subject = "Subscription Activated - Gw Smart Analytics"

    amount_str = receipt.get("amount", "₹499.00")
    plan_str = receipt.get("plan", "Monthly Pro (1 Month)")
    valid_until = receipt.get("valid_until", "1 Month from today")
    txn_id = receipt.get("transaction_id", receipt.get("payment_id", "N/A"))

    body_text = f"""Dear {user_name},

You have successfully bought Gw Smart Analytics subscription. Enjoy your journey.

Your 1-Month subscription has been activated across all dashboards!

--- PAYMENT & SUBSCRIPTION DETAILS ---
Receipt Number: {receipt.get('receipt_no', 'GW-REC')}
Transaction ID: {txn_id}
Subscription Plan: {plan_str}
Amount Paid: {amount_str}
Validity: 1 Month Free / Pro (Valid until {valid_until})
Payment Method: {receipt.get('method', 'UPI')}
Customer Name: {user_name}
Customer Email: {to_email}

Unlocked Features:
✔ Unlimited Dashboard PDF Exports
✔ Comprehensive Executive AI Report PDF
✔ Full Access to GW Smart Analytics (up.html)
✔ Full Access to Sales Intelligence Dashboard (hh.py)

Access your dashboard anytime at: http://127.0.0.1:5000/dashboard

Thank you for choosing GW Smart Analytics!

Best Regards,
GW Smart Analytics Team
"""

    body_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background: #ffffff;">
        <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; margin-bottom: 16px;">
            <h2 style="color: #1e3a8a; margin: 0;">GW Smart Analytics</h2>
            <span style="background: #dcfce7; color: #166534; padding: 6px 14px; border-radius: 20px; font-weight: bold; font-size: 12px;">ACTIVE & PAID</span>
        </div>
        
        <div style="padding: 16px; background: #f0fdf4; border-left: 4px solid #16a34a; border-radius: 6px; margin-bottom: 20px;">
            <p style="margin: 0; font-size: 16px; color: #166534; font-weight: bold;">
                You have successfully bought Gw Smart Analytics subscription. Enjoy your journey.
            </p>
        </div>

        <p style="color: #334155; line-height: 1.6;">
            Dear <b>{user_name}</b>,<br>
            Your payment has been received and verified. Your 1-Month subscription is now active!
        </p>

        <table style="width: 100%; border-collapse: collapse; margin: 18px 0; background: #f8fafc; border-radius: 8px; overflow: hidden;">
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 14px; font-size: 13px; color: #64748b;">Receipt No:</td>
                <td style="padding: 10px 14px; font-size: 13px; font-weight: bold; color: #0f172a;">{receipt.get('receipt_no', 'N/A')}</td>
            </tr>
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 14px; font-size: 13px; color: #64748b;">Transaction ID / UTR:</td>
                <td style="padding: 10px 14px; font-size: 13px; font-weight: bold; color: #0f172a;">{txn_id}</td>
            </tr>
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 14px; font-size: 13px; color: #64748b;">Plan & Validity:</td>
                <td style="padding: 10px 14px; font-size: 13px; font-weight: bold; color: #16a34a;">{plan_str} (Until {valid_until})</td>
            </tr>
            <tr>
                <td style="padding: 10px 14px; font-size: 13px; color: #64748b;">Amount Paid:</td>
                <td style="padding: 10px 14px; font-size: 14px; font-weight: bold; color: #15803d;">{amount_str}</td>
            </tr>
        </table>

        <div style="text-align: center; margin: 24px 0;">
            <a href="http://127.0.0.1:5000/dashboard" style="background: linear-gradient(135deg, #1e40af, #3b82f6); color: white; padding: 12px 28px; text-decoration: none; border-radius: 8px; font-weight: bold; display: inline-block;">
                Launch Pro Dashboards
            </a>
        </div>

        <p style="font-size: 12px; color: #94a3b8; text-align: center; margin-top: 20px;">
            GW Smart Analytics Inc. • Official Tax Invoice & Payment Receipt • support@gw-analytics.com
        </p>
    </div>
    """

    send_async_email(to_email, subject, body_text, body_html)
