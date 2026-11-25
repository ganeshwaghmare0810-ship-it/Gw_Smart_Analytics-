"""Per-dashboard payment, special-access, and protected download APIs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any

from flask import Blueprint, g, jsonify, request, send_file
from werkzeug.utils import secure_filename

import cashfree_gateway
import database


blueprint = Blueprint("dashboard_payments", __name__, url_prefix="/api/dashboard-payments")
logger = logging.getLogger(__name__)
PRICE_RUPEES = 10.00
PRICE_PAISE = 1000
MAX_DASHBOARD_JSON_BYTES = 45 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_REQUEST_BYTES = MAX_ARTIFACT_BYTES + 1024 * 1024
ARTIFACT_CHUNK_BYTES = 1024 * 1024
ARTIFACT_TYPES = {
    "dashboard_pdf": (".pdf", "application/pdf"),
    "report_pdf": (".pdf", "application/pdf"),
    "csv": (".csv", "text/csv; charset=utf-8"),
    "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "chart_png": (".png", "image/png"),
}


def init_db() -> None:
    """Apply the additive dashboard-payment migration without replacing legacy tables."""
    conn = database.get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gw_dashboard_payment_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboards (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                dataset_name TEXT NOT NULL DEFAULT '',
                dashboard_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(id, user_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                dashboard_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(id, dashboard_id, user_id),
                FOREIGN KEY(dashboard_id, user_id)
                    REFERENCES dashboards(id, user_id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        payment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(payments)").fetchall()
        }
        additions = {
            "dashboard_id": "TEXT REFERENCES dashboards(id) ON DELETE CASCADE",
            "report_id": "TEXT REFERENCES reports(id) ON DELETE SET NULL",
            "gateway_provider": "TEXT NOT NULL DEFAULT ''",
            "gateway_session_id": "TEXT",
            "gateway_transaction_id": "TEXT",
            "access_type": "TEXT NOT NULL DEFAULT 'PAID'",
            "download_permission": "INTEGER NOT NULL DEFAULT 0",
            "amount_paise": "INTEGER",
        }
        for column, definition in additions.items():
            if column not in payment_columns:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {column} {definition}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS special_access_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_hash TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT 'Special access',
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_by INTEGER,
                updated_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_access (
                dashboard_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                access_type TEXT NOT NULL CHECK(access_type IN ('PAID', 'SPECIAL_CODE')),
                payment_id INTEGER,
                code_id INTEGER,
                granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(dashboard_id, user_id)
                    REFERENCES dashboards(id, user_id) ON DELETE CASCADE,
                FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE SET NULL,
                FOREIGN KEY(code_id) REFERENCES special_access_codes(id) ON DELETE SET NULL,
                CHECK(
                    (access_type = 'PAID' AND payment_id IS NOT NULL AND code_id IS NULL)
                    OR
                    (access_type = 'SPECIAL_CODE' AND code_id IS NOT NULL)
                )
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS special_access_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_id INTEGER NOT NULL,
                dashboard_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code_id, dashboard_id),
                FOREIGN KEY(code_id) REFERENCES special_access_codes(id) ON DELETE RESTRICT,
                FOREIGN KEY(dashboard_id, user_id)
                    REFERENCES dashboards(id, user_id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS special_access_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ip_hash TEXT NOT NULL,
                code_id INTEGER,
                succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1)),
                attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(code_id) REFERENCES special_access_codes(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_artifacts (
                id TEXT PRIMARY KEY,
                dashboard_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                artifact_type TEXT NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL,
                storage_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dashboard_id, artifact_type),
                FOREIGN KEY(dashboard_id, user_id)
                    REFERENCES dashboards(id, user_id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        artifact_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(dashboard_artifacts)").fetchall()
        }
        if "storage_path" not in artifact_columns:
            conn.execute("ALTER TABLE dashboard_artifacts ADD COLUMN storage_path TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_webhooks (
                event_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_payment_owner_insert
            BEFORE INSERT ON payments
            WHEN NEW.dashboard_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM dashboards
                WHERE id = NEW.dashboard_id AND user_id = NEW.user_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'dashboard payment owner mismatch');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_dashboard_payment_owner_update
            BEFORE UPDATE OF dashboard_id, user_id ON payments
            WHEN NEW.dashboard_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM dashboards
                WHERE id = NEW.dashboard_id AND user_id = NEW.user_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'dashboard payment owner mismatch');
            END
            """
        )

        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_dashboards_user_created ON dashboards(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_reports_user_created ON reports(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_dashboard_access_user ON dashboard_access(user_id, granted_at)",
            "CREATE INDEX IF NOT EXISTS idx_special_redemptions_user ON special_access_redemptions(user_id, redeemed_at)",
            "CREATE INDEX IF NOT EXISTS idx_special_attempts_ip_time ON special_access_attempts(ip_hash, attempted_at)",
            "CREATE INDEX IF NOT EXISTS idx_dashboard_payments_user_created ON payments(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_dashboard_payments_dashboard ON payments(dashboard_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_payment_webhooks_order ON payment_webhooks(order_id, received_at)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_dashboard_payment_pending
            ON payments(dashboard_id)
            WHERE dashboard_id IS NOT NULL
              AND gateway_provider = 'cashfree'
              AND payment_status = 'PENDING'
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_dashboard_gateway_transaction
            ON payments(trim(gateway_transaction_id))
            WHERE trim(COALESCE(gateway_transaction_id, '')) <> ''
            """,
        ):
            conn.execute(sql)
        conn.execute(
            """
            INSERT OR IGNORE INTO gw_dashboard_payment_migrations(version)
            VALUES (1)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    seed_configured_special_code()


def _special_code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def seed_configured_special_code() -> None:
    code = os.getenv("GW_SPECIAL_ACCESS_CODE", "gwanalitics0810").strip() or "gwanalitics0810"
    conn = database.get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO special_access_codes(code_hash, label, is_active)
            VALUES (?, 'GW Smart Analytics special access (gwanalitics0810)', 1)
            """,
            (_special_code_hash(code),),
        )
        conn.commit()
    finally:
        conn.close()


def _user() -> dict[str, Any]:
    return g.current_user


def _admin() -> bool:
    return str(_user().get("role", "")).lower() == "admin"


def _owned_dashboard(conn: sqlite3.Connection, dashboard_id: str, user_id: int):
    return conn.execute(
        "SELECT * FROM dashboards WHERE id = ? AND user_id = ?",
        (dashboard_id, user_id),
    ).fetchone()


def _access(conn: sqlite3.Connection, dashboard_id: str, user_id: int):
    return conn.execute(
        """
        SELECT da.access_type, da.granted_at, p.order_id, p.gateway_transaction_id,
               p.payment_status, p.payment_id, sac.label AS code_label
        FROM dashboard_access da
        LEFT JOIN payments p ON p.id = da.payment_id
        LEFT JOIN special_access_codes sac ON sac.id = da.code_id
        WHERE da.dashboard_id = ? AND da.user_id = ?
        """,
        (dashboard_id, user_id),
    ).fetchone()


def _dashboard_access_payload(conn: sqlite3.Connection, dashboard_id: str, user_id: int) -> dict[str, Any]:
    access = _access(conn, dashboard_id, user_id)
    if access:
        return {
            "allowed": True,
            "access_type": access["access_type"],
            "granted_at": access["granted_at"],
            "transaction_id": access["gateway_transaction_id"] or access["payment_id"],
            "order_id": access["order_id"],
        }
    payment = conn.execute(
        """
        SELECT order_id, payment_status, gateway_transaction_id
        FROM payments
        WHERE dashboard_id = ? AND user_id = ? AND gateway_provider = 'cashfree'
        ORDER BY id DESC LIMIT 1
        """,
        (dashboard_id, user_id),
    ).fetchone()
    return {
        "allowed": False,
        "access_type": None,
        "payment_status": payment["payment_status"] if payment else "NOT_STARTED",
        "order_id": payment["order_id"] if payment else None,
        "transaction_id": payment["gateway_transaction_id"] if payment else None,
    }


def create_dashboard_record(
    user_id: int,
    dashboard_name: str,
    dataset_name: str,
    rows: list[dict[str, Any]],
    report_name: str,
) -> dict[str, str]:
    dashboard_id = uuid.uuid4().hex
    report_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    dashboard_json = json.dumps(
        {"rows": rows, "row_count": len(rows)},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    report_json = json.dumps(
        {
            "dashboard_id": dashboard_id,
            "row_count": len(rows),
            "columns": list(rows[0].keys()) if rows else [],
            "generated_at": created_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(dashboard_json.encode("utf-8")) > MAX_DASHBOARD_JSON_BYTES:
        raise ValueError("This dashboard dataset is too large to save. Reduce the dataset and retry.")

    conn = database.get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO dashboards(id, user_id, name, dataset_name, dashboard_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dashboard_id, user_id, dashboard_name, dataset_name, dashboard_json),
        )
        conn.execute(
            """
            INSERT INTO reports(id, dashboard_id, user_id, name, report_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, dashboard_id, user_id, report_name, report_json),
        )
        conn.commit()
        return {"dashboard_id": dashboard_id, "report_id": report_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_payment(order_id: str, transaction_id: str, amount_paise: int, currency: str) -> bool:
    if amount_paise != PRICE_PAISE or currency.upper() != "INR" or not transaction_id:
        return False
    conn = database.get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        payment = conn.execute(
            """
            SELECT id, user_id, dashboard_id, payment_status, gateway_transaction_id,
                   amount_paise, amount
            FROM payments
            WHERE order_id = ? AND gateway_provider = 'cashfree'
            """,
            (order_id,),
        ).fetchone()
        if not payment:
            conn.rollback()
            return False
        if payment["payment_status"] == "SUCCESS":
            conn.rollback()
            return payment["gateway_transaction_id"] == transaction_id
        recorded_paise = payment["amount_paise"]
        if recorded_paise is None:
            recorded_paise = int((Decimal(str(payment["amount"])) * 100).quantize(Decimal("1")))
        if recorded_paise != PRICE_PAISE:
            conn.rollback()
            return False
        duplicate = conn.execute(
            """
            SELECT id FROM payments
            WHERE gateway_transaction_id = ? AND id <> ?
            """,
            (transaction_id, payment["id"]),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return False

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE payments
            SET payment_status = 'SUCCESS', status = 'approved', paid_at = ?,
                payment_method = 'Cashfree', payment_id = ?, transaction_id = ?,
                gateway_transaction_id = ?, download_permission = 1, access_type = 'PAID'
            WHERE id = ?
            """,
            (now, transaction_id, transaction_id, transaction_id, payment["id"]),
        )
        conn.execute(
            """
            INSERT INTO dashboard_access(dashboard_id, user_id, access_type, payment_id)
            VALUES (?, ?, 'PAID', ?)
            ON CONFLICT(dashboard_id) DO NOTHING
            """,
            (payment["dashboard_id"], payment["user_id"], payment["id"]),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_payment_failure(order_id: str, status: str) -> None:
    normalized = status.upper()
    if normalized not in {"FAILED", "CANCELLED", "EXPIRED", "TERMINATED"}:
        return
    conn = database.get_db_connection()
    try:
        conn.execute(
            """
            UPDATE payments
            SET payment_status = ?, status = lower(?), download_permission = 0
            WHERE order_id = ? AND gateway_provider = 'cashfree'
              AND payment_status = 'PENDING'
            """,
            (normalized, normalized, order_id),
        )
        conn.commit()
    finally:
        conn.close()


def _cashfree_amount_paise(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
        return int((amount * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError, TypeError):
        return None


@blueprint.get("/config")
def gateway_config():
    return jsonify({"success": True, "configured": cashfree_gateway.is_configured(), "mode": cashfree_gateway.mode()})


@blueprint.post("/dashboards")
def create_dashboard_api():
    data = request.get_json(silent=True) or {}
    name = str(data.get("dashboard_name", "")).strip()
    report_name = str(data.get("report_name", "")).strip()
    dataset_name = str(data.get("dataset_name", "")).strip()
    rows = data.get("rows")
    if not name or len(name) > 160 or not report_name or len(report_name) > 200:
        return jsonify({"success": False, "message": "A valid dashboard and report name are required."}), 400
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        return jsonify({"success": False, "message": "Generate a dashboard with valid dataset rows first."}), 400
    try:
        ids = create_dashboard_record(_user()["id"], name, dataset_name[:255], rows, report_name)
    except (ValueError, TypeError) as error:
        return jsonify({"success": False, "message": str(error)}), 400
    except Exception:
        logger.exception("Dashboard and report could not be saved.")
        return jsonify({"success": False, "message": "The dashboard could not be saved."}), 500
    return jsonify({"success": True, **ids, "amount": PRICE_RUPEES, "payment_status": "NOT_STARTED"}), 201


@blueprint.get("/dashboards")
def list_dashboards_api():
    user_id = _user()["id"]
    conn = database.get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT d.id AS dashboard_id, d.name AS dashboard_name, d.dataset_name, d.created_at,
                   r.id AS report_id, r.name AS report_name, da.access_type,
                   da.granted_at, p.gateway_transaction_id
            FROM dashboards d
            JOIN reports r ON r.dashboard_id = d.id AND r.user_id = d.user_id
            LEFT JOIN dashboard_access da ON da.dashboard_id = d.id AND da.user_id = d.user_id
            LEFT JOIN payments p ON p.id = da.payment_id
            WHERE d.user_id = ?
            ORDER BY d.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return jsonify({"success": True, "dashboards": [dict(row) for row in rows]})
    finally:
        conn.close()


@blueprint.get("/dashboards/<dashboard_id>/access")
def dashboard_access_api(dashboard_id: str):
    conn = database.get_db_connection()
    try:
        dashboard = _owned_dashboard(conn, dashboard_id, _user()["id"])
        if not dashboard:
            return jsonify({"success": False, "message": "Dashboard not found."}), 404
        report = conn.execute(
            "SELECT id, name FROM reports WHERE dashboard_id = ? AND user_id = ?",
            (dashboard_id, _user()["id"]),
        ).fetchone()
        return jsonify(
            {
                "success": True,
                "dashboard_id": dashboard_id,
                "dashboard_name": dashboard["name"],
                "report": dict(report) if report else None,
                "amount": PRICE_RUPEES,
                **_dashboard_access_payload(conn, dashboard_id, _user()["id"]),
            }
        )
    finally:
        conn.close()


@blueprint.post("/dashboards/<dashboard_id>/orders")
def create_dashboard_order_api(dashboard_id: str):
    user = _user()
    if not cashfree_gateway.is_configured():
        return jsonify({"success": False, "message": "Cashfree sandbox credentials are not configured on the server."}), 503

    conn = database.get_db_connection()
    try:
        dashboard = _owned_dashboard(conn, dashboard_id, user["id"])
        if not dashboard:
            return jsonify({"success": False, "message": "Dashboard not found."}), 404
        if _access(conn, dashboard_id, user["id"]):
            return jsonify({"success": True, "already_authorized": True, **_dashboard_access_payload(conn, dashboard_id, user["id"])})
        account = conn.execute(
            "SELECT email, phone, mobile FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT order_id, gateway_session_id FROM payments
            WHERE dashboard_id = ? AND user_id = ? AND gateway_provider = 'cashfree'
              AND payment_status = 'PENDING'
            ORDER BY id DESC LIMIT 1
            """,
            (dashboard_id, user["id"]),
        ).fetchone()
        if pending:
            if not pending["gateway_session_id"]:
                return jsonify({"success": False, "message": "A payment order is already being prepared. Retry shortly."}), 409
            return jsonify(
                {
                    "success": True,
                    "order_id": pending["order_id"],
                    "payment_session_id": pending["gateway_session_id"],
                    "amount": PRICE_RUPEES,
                    "currency": "INR",
                    "mode": cashfree_gateway.mode(),
                }
            )
        report = conn.execute(
            "SELECT id FROM reports WHERE dashboard_id = ? AND user_id = ?",
            (dashboard_id, user["id"]),
        ).fetchone()
        order_id = "gw_" + uuid.uuid4().hex
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO payments(
                    user_id, email, amount, method, order_id, currency, payment_status,
                    status, dashboard_id, report_id, gateway_provider, access_type,
                    download_permission, amount_paise
                )
                VALUES (?, ?, ?, 'Cashfree', ?, 'INR', 'PENDING', 'pending', ?, ?,
                        'cashfree', 'PAID', 0, ?)
                """,
                (user["id"], account["email"], PRICE_RUPEES, order_id, dashboard_id, report["id"], PRICE_PAISE),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = conn.execute(
                """
                SELECT order_id, gateway_session_id FROM payments
                WHERE dashboard_id = ? AND user_id = ? AND gateway_provider = 'cashfree'
                  AND payment_status = 'PENDING'
                ORDER BY id DESC LIMIT 1
                """,
                (dashboard_id, user["id"]),
            ).fetchone()
            if existing and existing["gateway_session_id"]:
                return jsonify(
                    {
                        "success": True,
                        "order_id": existing["order_id"],
                        "payment_session_id": existing["gateway_session_id"],
                        "amount": PRICE_RUPEES,
                        "currency": "INR",
                        "mode": cashfree_gateway.mode(),
                    }
                )
            return jsonify({"success": False, "message": "A payment order is already being prepared. Retry shortly."}), 409

        phone = re.sub(r"\D", "", str(account["mobile"] or account["phone"] or ""))
        if len(phone) < 10:
            _set_payment_failure(order_id, "FAILED")
            return jsonify({"success": False, "message": "Add a valid mobile number to your account before paying."}), 400
        public_base = os.getenv("GW_PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
        return_url = f"{public_base}/h.html?payment_order_id={{order_id}}"
        try:
            gateway_order = cashfree_gateway.create_order(
                order_id=order_id,
                amount=PRICE_RUPEES,
                customer_id=str(user["id"]),
                customer_email=account["email"],
                customer_phone=phone[-10:],
                return_url=return_url,
            )
        except cashfree_gateway.CashfreeError as error:
            _set_payment_failure(order_id, "FAILED")
            return jsonify({"success": False, "message": str(error)}), error.status_code

        returned_amount = _cashfree_amount_paise(gateway_order.get("order_amount"))
        if (
            gateway_order.get("order_id") != order_id
            or returned_amount != PRICE_PAISE
            or str(gateway_order.get("order_currency", "")).upper() != "INR"
            or not gateway_order.get("payment_session_id")
        ):
            logger.error("Cashfree returned an incomplete or mismatched order.")
            _set_payment_failure(order_id, "FAILED")
            return jsonify({"success": False, "message": "Cashfree returned an invalid payment order."}), 502

        session_id = str(gateway_order["payment_session_id"])
        conn.execute(
            """
            UPDATE payments SET gateway_session_id = ?
            WHERE order_id = ? AND user_id = ? AND payment_status = 'PENDING'
            """,
            (session_id, order_id, user["id"]),
        )
        conn.commit()
        return jsonify(
            {
                "success": True,
                "order_id": order_id,
                "payment_session_id": session_id,
                "amount": PRICE_RUPEES,
                "currency": "INR",
                "mode": cashfree_gateway.mode(),
            }
        ), 201
    finally:
        conn.close()


@blueprint.post("/verify")
def verify_dashboard_order_api():
    user = _user()
    data = request.get_json(silent=True) or {}
    order_id = str(data.get("order_id", "")).strip()
    if not re.fullmatch(r"gw_[a-f0-9]{32}", order_id):
        return jsonify({"success": False, "message": "A valid payment order ID is required."}), 400
    conn = database.get_db_connection()
    try:
        payment = conn.execute(
            """
            SELECT dashboard_id, payment_status, gateway_transaction_id, amount_paise
            FROM payments WHERE order_id = ? AND user_id = ? AND gateway_provider = 'cashfree'
            """,
            (order_id, user["id"]),
        ).fetchone()
    finally:
        conn.close()
    if not payment:
        return jsonify({"success": False, "message": "Payment order was not found for this account."}), 404
    if payment["payment_status"] == "SUCCESS":
        conn = database.get_db_connection()
        try:
            return jsonify(
                {
                    "success": True,
                    "verified": True,
                    "order_id": order_id,
                    "transaction_id": payment["gateway_transaction_id"],
                    **_dashboard_access_payload(conn, payment["dashboard_id"], user["id"]),
                }
            )
        finally:
            conn.close()

    try:
        gateway_order = cashfree_gateway.fetch_order(order_id)
        payments = cashfree_gateway.fetch_order_payments(order_id)
    except cashfree_gateway.CashfreeError as error:
        return jsonify({"success": False, "message": str(error)}), error.status_code

    if (
        gateway_order.get("order_id") != order_id
        or _cashfree_amount_paise(gateway_order.get("order_amount")) != PRICE_PAISE
        or str(gateway_order.get("order_currency", "")).upper() != "INR"
        or payment["amount_paise"] != PRICE_PAISE
    ):
        return jsonify({"success": False, "message": "Cashfree order details did not match this dashboard."}), 400

    successful = next(
        (
            item for item in payments
            if item.get("order_id") == order_id
            and str(item.get("payment_status", "")).upper() == "SUCCESS"
            and str(item.get("payment_currency", "INR")).upper() == "INR"
            and _cashfree_amount_paise(item.get("payment_amount")) == PRICE_PAISE
            and item.get("cf_payment_id") is not None
        ),
        None,
    )
    if successful:
        transaction_id = str(successful["cf_payment_id"])
        if not finalize_payment(order_id, transaction_id, PRICE_PAISE, "INR"):
            return jsonify({"success": False, "message": "Payment verification could not be completed. Contact support."}), 409
        conn = database.get_db_connection()
        try:
            return jsonify(
                {
                    "success": True,
                    "verified": True,
                    "order_id": order_id,
                    "transaction_id": transaction_id,
                    **_dashboard_access_payload(conn, payment["dashboard_id"], user["id"]),
                }
            )
        finally:
            conn.close()

    order_status = str(gateway_order.get("order_status", "PENDING")).upper()
    _set_payment_failure(order_id, order_status)
    conn = database.get_db_connection()
    try:
        return jsonify(
            {
                "success": True,
                "verified": False,
                "allowed": False,
                "payment_status": order_status,
                "message": "Payment is not confirmed by Cashfree yet.",
                **_dashboard_access_payload(conn, payment["dashboard_id"], user["id"]),
            }
        ), 202
    finally:
        conn.close()


@blueprint.post("/webhook")
def cashfree_webhook_api():
    raw_body = request.get_data(cache=True)
    if len(raw_body) > 1024 * 1024:
        return jsonify({"success": False, "message": "Webhook payload is too large."}), 413
    timestamp = request.headers.get("x-webhook-timestamp") or request.headers.get("cf-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature") or request.headers.get("cf-webhook-signature", "")
    if not cashfree_gateway.verify_webhook_signature(timestamp, signature, raw_body):
        return jsonify({"success": False, "message": "Invalid Cashfree webhook signature."}), 401
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"success": False, "message": "Invalid webhook payload."}), 400
    if not isinstance(payload, dict):
        return jsonify({"success": False, "message": "Invalid webhook payload."}), 400

    event_type = str(payload.get("type", ""))
    data_value = payload.get("data")
    data: dict[str, Any] = data_value if isinstance(data_value, dict) else {}
    order_value = data.get("order")
    order: dict[str, Any] = order_value if isinstance(order_value, dict) else {}
    payment_value = data.get("payment")
    payment: dict[str, Any] = payment_value if isinstance(payment_value, dict) else {}
    order_id = str(order.get("order_id") or payment.get("order_id") or "")
    event_id = str(payload.get("id") or hashlib.sha256(timestamp.encode() + raw_body).hexdigest())
    conn = database.get_db_connection()
    try:
        try:
            conn.execute(
                """
                INSERT INTO payment_webhooks(event_id, order_id, event_type, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, order_id, event_type, raw_body.decode("utf-8")),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return jsonify({"success": True, "duplicate": True}), 200
    finally:
        conn.close()

    processing_status = "IGNORED"
    if event_type == "PAYMENT_SUCCESS_WEBHOOK" and str(payment.get("payment_status", "")).upper() == "SUCCESS":
        amount_paise = _cashfree_amount_paise(order.get("order_amount"))
        payment_amount_paise = _cashfree_amount_paise(payment.get("payment_amount"))
        currency = str(order.get("order_currency") or payment.get("payment_currency") or "").upper()
        if (
            order_id
            and amount_paise == PRICE_PAISE
            and payment_amount_paise == PRICE_PAISE
            and currency == "INR"
            and str(payment.get("payment_currency", "INR")).upper() == "INR"
            and payment.get("cf_payment_id") is not None
            and finalize_payment(order_id, str(payment["cf_payment_id"]), PRICE_PAISE, currency)
        ):
            processing_status = "PROCESSED"
        else:
            processing_status = "REJECTED"
    elif event_type in {"PAYMENT_FAILED_WEBHOOK", "PAYMENT_USER_DROPPED_WEBHOOK"}:
        status = str(payment.get("payment_status", "FAILED")).upper()
        _set_payment_failure(order_id, "CANCELLED" if status == "USER_DROPPED" else "FAILED")
        processing_status = "PROCESSED"

    conn = database.get_db_connection()
    try:
        conn.execute(
            """
            UPDATE payment_webhooks SET processing_status = ?, processed_at = ?
            WHERE event_id = ?
            """,
            (processing_status, datetime.now(timezone.utc).isoformat(), event_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "processed": processing_status == "PROCESSED"}), 200


@blueprint.post("/dashboards/<dashboard_id>/special-access")
def apply_special_access_api(dashboard_id: str):
    data = request.get_json(silent=True) or {}
    code = data.get("code")
    if not isinstance(code, str) or not code.strip() or len(code) > 256:
        return jsonify({"success": False, "message": "Enter a valid special access code."}), 400
    conn = database.get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ip_hash = hashlib.sha256((request.remote_addr or "unknown").encode("utf-8")).hexdigest()
        attempts = conn.execute(
            """
            SELECT COUNT(*) FROM special_access_attempts
            WHERE user_id = ? AND ip_hash = ?
              AND attempted_at >= datetime('now', '-15 minutes')
            """,
            (_user()["id"], ip_hash),
        ).fetchone()[0]
        if attempts >= 10:
            conn.rollback()
            return jsonify({"success": False, "message": "Too many code attempts. Try again in 15 minutes."}), 429
        dashboard = _owned_dashboard(conn, dashboard_id, _user()["id"])
        if not dashboard:
            conn.rollback()
            return jsonify({"success": False, "message": "Dashboard not found."}), 404
        if _access(conn, dashboard_id, _user()["id"]):
            conn.rollback()
            return jsonify({"success": True, "already_authorized": True, **_dashboard_access_payload(conn, dashboard_id, _user()["id"])})
        code_row = conn.execute(
            """
            SELECT id FROM special_access_codes
            WHERE code_hash = ? AND is_active = 1
            """,
            (_special_code_hash(code),),
        ).fetchone()
        if not code_row:
            conn.execute(
                """
                INSERT INTO special_access_attempts(user_id, ip_hash, succeeded)
                VALUES (?, ?, 0)
                """,
                (_user()["id"], ip_hash),
            )
            conn.commit()
            return jsonify({"success": False, "message": "Invalid or inactive special access code."}), 400
        conn.execute(
            """
            INSERT INTO dashboard_access(dashboard_id, user_id, access_type, code_id)
            VALUES (?, ?, 'SPECIAL_CODE', ?)
            """,
            (dashboard_id, _user()["id"], code_row["id"]),
        )
        conn.execute(
            """
            INSERT INTO special_access_redemptions(code_id, dashboard_id, user_id)
            VALUES (?, ?, ?)
            """,
            (code_row["id"], dashboard_id, _user()["id"]),
        )
        conn.execute(
            """
            INSERT INTO special_access_attempts(user_id, ip_hash, code_id, succeeded)
            VALUES (?, ?, ?, 1)
            """,
            (_user()["id"], ip_hash, code_row["id"]),
        )
        conn.commit()
        return jsonify(
            {
                "success": True,
                "allowed": True,
                "access_type": "SPECIAL_CODE",
                "message": "Special access applied. Downloads are enabled for this dashboard and report.",
            }
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return jsonify({"success": False, "message": "Special access could not be applied. Refresh and retry."}), 409
    finally:
        conn.close()


@blueprint.post("/dashboards/<dashboard_id>/artifacts")
def upload_dashboard_artifact_api(dashboard_id: str):
    request.max_content_length = MAX_ARTIFACT_REQUEST_BYTES
    user_id = _user()["id"]
    artifact_type = str(request.form.get("artifact_type", ""))
    if artifact_type not in ARTIFACT_TYPES:
        return jsonify({"success": False, "message": "Unsupported download format."}), 400
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "message": "The generated file is missing."}), 400
    suffix, mime_type = ARTIFACT_TYPES[artifact_type]
    original_name = secure_filename(uploaded.filename)
    if not original_name.lower().endswith(suffix):
        original_name = (original_name or artifact_type) + suffix
    artifact_id = uuid.uuid4().hex
    artifact_dir = database.UPLOAD_DIR / "dashboard_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = artifact_dir / f".{artifact_id}.uploading"
    storage_name = f"{artifact_id}{suffix}"
    storage_path = artifact_dir / storage_name
    committed = False
    conn = database.get_db_connection()
    try:
        dashboard = _owned_dashboard(conn, dashboard_id, user_id)
        if not dashboard:
            return jsonify({"success": False, "message": "Dashboard not found."}), 404
        previous_artifact = conn.execute(
            "SELECT storage_path FROM dashboard_artifacts WHERE dashboard_id = ? AND artifact_type = ?",
            (dashboard_id, artifact_type),
        ).fetchone()
        total_bytes = 0
        with temporary_path.open("xb") as destination:
            while chunk := uploaded.stream.read(ARTIFACT_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > MAX_ARTIFACT_BYTES:
                    return jsonify(
                        {"success": False, "message": "The generated file exceeds the 1 GB limit."}
                    ), 413
                destination.write(chunk)
        if total_bytes == 0:
            return jsonify({"success": False, "message": "The generated file is empty."}), 400
        os.replace(temporary_path, storage_path)
        conn.execute(
            """
            INSERT INTO dashboard_artifacts(
                id, dashboard_id, user_id, artifact_type, file_name, mime_type, content, storage_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dashboard_id, artifact_type) DO UPDATE SET
                id = excluded.id, user_id = excluded.user_id, file_name = excluded.file_name,
                mime_type = excluded.mime_type, content = excluded.content,
                storage_path = excluded.storage_path,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                artifact_id, dashboard_id, user_id, artifact_type, original_name,
                mime_type, sqlite3.Binary(b""), storage_name,
            ),
        )
        conn.commit()
        committed = True
        if previous_artifact and previous_artifact["storage_path"]:
            previous_path = (artifact_dir / previous_artifact["storage_path"]).resolve()
            if previous_path.parent == artifact_dir.resolve() and previous_path.is_file():
                try:
                    previous_path.unlink()
                except OSError:
                    logger.warning("Unable to remove replaced dashboard artifact: %s", previous_path)
        return jsonify({"success": True, "artifact_id": artifact_id}), 201
    finally:
        conn.close()
        if not committed:
            for path in (temporary_path, storage_path):
                if path.exists():
                    path.unlink()


@blueprint.get("/dashboards/<dashboard_id>/downloads/<artifact_type>")
def download_dashboard_artifact_api(dashboard_id: str, artifact_type: str):
    if artifact_type not in ARTIFACT_TYPES:
        return jsonify({"success": False, "message": "Unsupported download format."}), 404
    conn = database.get_db_connection()
    try:
        artifact = conn.execute(
            """
            SELECT a.id, a.file_name, a.mime_type, a.content, a.storage_path
            FROM dashboard_artifacts a
            JOIN dashboard_access da
              ON da.dashboard_id = a.dashboard_id AND da.user_id = a.user_id
            WHERE a.dashboard_id = ? AND a.user_id = ? AND a.artifact_type = ?
            """,
            (dashboard_id, _user()["id"], artifact_type),
        ).fetchone()
        if not artifact:
            dashboard = _owned_dashboard(conn, dashboard_id, _user()["id"])
            if not dashboard:
                return jsonify({"success": False, "message": "Dashboard not found."}), 404
            if not _access(conn, dashboard_id, _user()["id"]):
                return jsonify({"success": False, "payment_required": True, "message": "Payment or special access is required before downloading."}), 402
            return jsonify({"success": False, "message": "Generate this file again before downloading."}), 404
        if artifact["storage_path"]:
            artifact_dir = (database.UPLOAD_DIR / "dashboard_artifacts").resolve()
            artifact_path = (artifact_dir / artifact["storage_path"]).resolve()
            if artifact_path.parent != artifact_dir or not artifact_path.is_file():
                logger.error("Stored dashboard artifact is missing or has an invalid path: %s", artifact["id"])
                return jsonify({"success": False, "message": "The stored download is unavailable."}), 500
            file = artifact_path
        else:
            file = BytesIO(artifact["content"])
        return send_file(
            file,
            mimetype=artifact["mime_type"],
            as_attachment=True,
            download_name=artifact["file_name"],
            max_age=0,
        )
    finally:
        conn.close()


@blueprint.get("/history")
def dashboard_payment_history_api():
    conn = database.get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.order_id, p.user_id, p.dashboard_id, p.amount,
                   p.currency, p.payment_status, p.gateway_transaction_id,
                   p.paid_at, p.created_at, p.access_type, p.download_permission,
                   d.name AS dashboard_name, r.name AS report_name
            FROM payments p
            LEFT JOIN dashboards d ON d.id = p.dashboard_id
            LEFT JOIN reports r ON r.id = p.report_id
            WHERE p.user_id = ? AND p.gateway_provider = 'cashfree'
            ORDER BY p.id DESC
            """,
            (_user()["id"],),
        ).fetchall()
        return jsonify({"success": True, "payments": [dict(row) for row in rows]})
    finally:
        conn.close()


@blueprint.get("/admin/overview")
def admin_billing_overview_api():
    if not _admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    conn = database.get_db_connection()
    try:
        users = conn.execute(
            """
            SELECT u.id AS user_id, u.name, u.email, u.mobile, u.created_at,
                   COUNT(DISTINCT d.id) AS dashboard_count,
                   SUM(CASE WHEN da.access_type = 'PAID' THEN 1 ELSE 0 END) AS paid_count,
                   SUM(CASE WHEN da.access_type = 'SPECIAL_CODE' THEN 1 ELSE 0 END) AS free_count
            FROM users u
            LEFT JOIN dashboards d ON d.user_id = u.id
            LEFT JOIN dashboard_access da ON da.dashboard_id = d.id AND da.user_id = u.id
            GROUP BY u.id
            ORDER BY u.id DESC
            """
        ).fetchall()
        payments = conn.execute(
            """
            SELECT p.id, p.user_id, u.name AS user_name, u.email, p.dashboard_id,
                   d.name AS dashboard_name, p.report_id, r.name AS report_name,
                   p.order_id, p.amount, p.currency, p.payment_status, p.access_type,
                   p.gateway_transaction_id, p.download_permission, p.paid_at, p.created_at
            FROM payments p
            JOIN users u ON u.id = p.user_id
            LEFT JOIN dashboards d ON d.id = p.dashboard_id
            LEFT JOIN reports r ON r.id = p.report_id
            WHERE p.gateway_provider = 'cashfree'
            ORDER BY p.id DESC
            LIMIT 1000
            """
        ).fetchall()
        free_users = conn.execute(
            """
            SELECT DISTINCT u.id AS user_id, u.name, u.email, u.mobile,
                   COUNT(DISTINCT da.dashboard_id) AS free_dashboard_count
            FROM users u
            JOIN dashboard_access da ON da.user_id = u.id AND da.access_type = 'SPECIAL_CODE'
            GROUP BY u.id
            ORDER BY u.id DESC
            """
        ).fetchall()
        paid_users = conn.execute(
            """
            SELECT DISTINCT u.id AS user_id, u.name, u.email, u.mobile,
                   COUNT(DISTINCT da.dashboard_id) AS paid_dashboard_count
            FROM users u
            JOIN dashboard_access da ON da.user_id = u.id AND da.access_type = 'PAID'
            GROUP BY u.id
            ORDER BY u.id DESC
            """
        ).fetchall()
        return jsonify(
            {
                "success": True,
                "users": [dict(row) for row in users],
                "payments": [dict(row) for row in payments],
                "paid_users": [dict(row) for row in paid_users],
                "free_users": [dict(row) for row in free_users],
            }
        )
    finally:
        conn.close()


@blueprint.get("/admin/codes")
def admin_special_codes_api():
    if not _admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    conn = database.get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.label, c.is_active, c.created_at, c.updated_at,
                   COUNT(r.id) AS redemption_count
            FROM special_access_codes c
            LEFT JOIN special_access_redemptions r ON r.code_id = c.id
            GROUP BY c.id
            ORDER BY c.id
            """
        ).fetchall()
        return jsonify({"success": True, "codes": [dict(row) for row in rows]})
    finally:
        conn.close()


@blueprint.post("/admin/codes/<int:code_id>")
def set_special_code_status_api(code_id: int):
    if not _admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    data = request.get_json(silent=True) or {}
    active = data.get("active")
    if not isinstance(active, bool):
        return jsonify({"success": False, "message": "active must be true or false."}), 400
    conn = database.get_db_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE special_access_codes
            SET is_active = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(active), _user()["id"], code_id),
        )
        conn.commit()
        if not cursor.rowcount:
            return jsonify({"success": False, "message": "Special access code not found."}), 404
        return jsonify({"success": True, "active": active})
    finally:
        conn.close()


@blueprint.get("/admin/users/<int:user_id>/dashboards")
def admin_user_dashboards_api(user_id: int):
    if not _admin():
        return jsonify({"success": False, "message": "Admin access required."}), 403
    conn = database.get_db_connection()
    try:
        user = conn.execute(
            "SELECT id, name, email, mobile FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            return jsonify({"success": False, "message": "User not found."}), 404
        rows = conn.execute(
            """
            SELECT d.id AS dashboard_id, d.name AS dashboard_name, d.dataset_name,
                   d.created_at, r.id AS report_id, r.name AS report_name,
                   da.access_type, da.granted_at, p.order_id, p.payment_status,
                   p.gateway_transaction_id, p.amount
            FROM dashboards d
            LEFT JOIN reports r ON r.dashboard_id = d.id AND r.user_id = d.user_id
            LEFT JOIN dashboard_access da ON da.dashboard_id = d.id AND da.user_id = d.user_id
            LEFT JOIN payments p ON p.id = da.payment_id
            WHERE d.user_id = ?
            ORDER BY d.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return jsonify({"success": True, "user": dict(user), "dashboards": [dict(row) for row in rows]})
    finally:
        conn.close()
