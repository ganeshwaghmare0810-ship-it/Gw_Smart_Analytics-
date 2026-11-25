import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import cashfree_gateway
import dashboard_payments
import database
from flask import Flask, g


class TestDashboardPayments(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        database.DB_PATH = Path(self.db_path)
        self.original_upload_dir = database.UPLOAD_DIR
        self.upload_dir = tempfile.TemporaryDirectory()
        database.UPLOAD_DIR = Path(self.upload_dir.name)
        database.init_db()
        with patch.dict(os.environ, {"GW_SPECIAL_ACCESS_CODE": "local-test-code"}):
            dashboard_payments.init_db()
        self.user = database.create_user(
            "Payment Test", "payments@example.test", "TestPass123!", "9876500100"
        )["user"]
        self.api = Flask(__name__)
        self.api.register_blueprint(dashboard_payments.blueprint)
        self.api.config["test_user_id"] = self.user["id"]

        @self.api.before_request
        def set_test_user():
            g.current_user = database.get_user_by_id(self.api.config["test_user_id"])

        self.client = self.api.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        database.UPLOAD_DIR = self.original_upload_dir
        self.upload_dir.cleanup()

    def create_dashboard(self):
        return dashboard_payments.create_dashboard_record(
            self.user["id"],
            "Sales Dashboard",
            "sales.csv",
            [{"date": "2026-01-01", "amount": 10}],
            "Sales Report",
        )

    def test_mobile_registration_is_unique_after_normalization(self):
        duplicate = database.create_user(
            "Another User", "another@example.test", "TestPass123!", "+91 98765-00100"
        )
        self.assertFalse(duplicate["success"])
        self.assertIn("मोबाइल", duplicate["message"])

    def test_additive_schema_migration_is_idempotent(self):
        dashboard_payments.init_db()
        conn = database.get_db_connection()
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "dashboards",
                    "reports",
                    "dashboard_access",
                    "dashboard_artifacts",
                    "special_access_codes",
                    "payment_webhooks",
                }.issubset(tables)
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM gw_dashboard_payment_migrations").fetchone()[0],
                1,
            )
            artifact_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(dashboard_artifacts)")
            }
            self.assertIn("storage_path", artifact_columns)
        finally:
            conn.close()

    def test_successful_payment_is_transactional_and_idempotent(self):
        ids = self.create_dashboard()
        conn = database.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO payments(
                    user_id, email, amount, method, order_id, currency, payment_status,
                    status, dashboard_id, report_id, gateway_provider, amount_paise
                )
                VALUES (?, ?, 10, 'Cashfree', 'gw_' || ?, 'INR', 'PENDING',
                        'pending', ?, ?, 'cashfree', 1000)
                """,
                (self.user["id"], self.user["email"], "a" * 32, ids["dashboard_id"], ids["report_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        order_id = "gw_" + "a" * 32
        self.assertTrue(dashboard_payments.finalize_payment(order_id, "cf_txn_101", 1000, "INR"))
        self.assertTrue(dashboard_payments.finalize_payment(order_id, "cf_txn_101", 1000, "INR"))
        self.assertFalse(dashboard_payments.finalize_payment(order_id, "cf_txn_102", 1000, "INR"))

        conn = database.get_db_connection()
        try:
            payment = conn.execute(
                "SELECT payment_status, download_permission, gateway_transaction_id FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            access = conn.execute(
                "SELECT access_type, payment_id FROM dashboard_access WHERE dashboard_id = ?",
                (ids["dashboard_id"],),
            ).fetchone()
            self.assertEqual(payment["payment_status"], "SUCCESS")
            self.assertEqual(payment["download_permission"], 1)
            self.assertEqual(payment["gateway_transaction_id"], "cf_txn_101")
            self.assertEqual(access["access_type"], "PAID")
            self.assertIsNotNone(access["payment_id"])
        finally:
            conn.close()

    def test_payment_amount_and_currency_are_mandatory(self):
        ids = self.create_dashboard()
        conn = database.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO payments(
                    user_id, email, amount, method, order_id, currency, payment_status,
                    status, dashboard_id, report_id, gateway_provider, amount_paise
                )
                VALUES (?, ?, 10, 'Cashfree', 'gw_' || ?, 'INR', 'PENDING',
                        'pending', ?, ?, 'cashfree', 1000)
                """,
                (self.user["id"], self.user["email"], "b" * 32, ids["dashboard_id"], ids["report_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertFalse(
            dashboard_payments.finalize_payment("gw_" + "b" * 32, "cf_txn_201", 999, "INR")
        )
        self.assertFalse(
            dashboard_payments.finalize_payment("gw_" + "b" * 32, "cf_txn_201", 1000, "USD")
        )

    def test_download_api_requires_dashboard_access_and_enforces_owner(self):
        ids = self.create_dashboard()
        response = self.client.post(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/artifacts",
            data={
                "artifact_type": "dashboard_pdf",
                "file": (BytesIO(b"%PDF-1.4 test artifact"), "dashboard.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        locked = self.client.get(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/downloads/dashboard_pdf"
        )
        self.assertEqual(locked.status_code, 402)

        order_id = "gw_" + "c" * 32
        conn = database.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO payments(
                    user_id, email, amount, method, order_id, currency, payment_status,
                    status, dashboard_id, report_id, gateway_provider, amount_paise
                )
                VALUES (?, ?, 10, 'Cashfree', ?, 'INR', 'PENDING',
                        'pending', ?, ?, 'cashfree', 1000)
                """,
                (self.user["id"], self.user["email"], order_id, ids["dashboard_id"], ids["report_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(dashboard_payments.finalize_payment(order_id, "cf_txn_301", 1000, "INR"))
        downloaded = self.client.get(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/downloads/dashboard_pdf"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"%PDF-1.4 test artifact")
        downloaded.close()

        other_user = database.create_user(
            "Other User", "other@example.test", "TestPass123!", "9876500101"
        )["user"]
        self.api.config["test_user_id"] = other_user["id"]
        cross_user = self.client.get(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/downloads/dashboard_pdf"
        )
        self.assertEqual(cross_user.status_code, 404)

    def test_artifact_larger_than_previous_limit_is_stored_on_disk(self):
        ids = self.create_dashboard()
        content = b"x" * (20 * 1024 * 1024 + 1)
        response = self.client.post(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/artifacts",
            data={
                "artifact_type": "dashboard_pdf",
                "file": (BytesIO(content), "large-dashboard.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(dashboard_payments.MAX_ARTIFACT_BYTES, 1024 * 1024 * 1024)

        conn = database.get_db_connection()
        try:
            artifact = conn.execute(
                "SELECT content, storage_path FROM dashboard_artifacts WHERE dashboard_id = ?",
                (ids["dashboard_id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(artifact["content"], b"")
        artifact_path = database.UPLOAD_DIR / "dashboard_artifacts" / artifact["storage_path"]
        self.assertEqual(artifact_path.stat().st_size, len(content))
        access = self.client.post(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/special-access",
            json={"code": "local-test-code"},
        )
        self.assertEqual(access.status_code, 200)
        downloaded = self.client.get(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/downloads/dashboard_pdf"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(len(downloaded.data), len(content))
        downloaded.close()

    def test_special_access_code_is_server_validated_and_tracked(self):
        ids = self.create_dashboard()
        invalid = self.client.post(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/special-access",
            json={"code": "not-the-code"},
        )
        self.assertEqual(invalid.status_code, 400)
        valid = self.client.post(
            f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/special-access",
            json={"code": "local-test-code"},
        )
        self.assertEqual(valid.status_code, 200)
        valid_payload = valid.get_json()
        self.assertIsNotNone(valid_payload)
        assert valid_payload is not None
        self.assertEqual(valid_payload["access_type"], "SPECIAL_CODE")
        conn = database.get_db_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM special_access_redemptions WHERE dashboard_id = ? AND user_id = ?",
                (ids["dashboard_id"], self.user["id"]),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_special_access_code_attempts_are_rate_limited(self):
        ids = self.create_dashboard()
        endpoint = f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/special-access"
        for attempt in range(10):
            response = self.client.post(endpoint, json={"code": f"invalid-{attempt}"})
            self.assertEqual(response.status_code, 400)
        limited = self.client.post(endpoint, json={"code": "local-test-code"})
        self.assertEqual(limited.status_code, 429)

    def test_cashfree_order_is_server_priced_and_verified_by_gateway_query(self):
        ids = self.create_dashboard()
        with patch.dict(
            os.environ,
            {"CASHFREE_APP_ID": "sandbox-id", "CASHFREE_SECRET_KEY": "sandbox-secret"},
        ):
            with patch.object(
                cashfree_gateway,
                "create_order",
                side_effect=lambda **kwargs: {
                    "order_id": kwargs["order_id"],
                    "order_amount": 10.0,
                    "order_currency": "INR",
                    "payment_session_id": "cashfree-session",
                },
            ):
                created = self.client.post(
                    f"/api/dashboard-payments/dashboards/{ids['dashboard_id']}/orders"
                )
            self.assertEqual(created.status_code, 201)
            order = created.get_json()
            self.assertIsNotNone(order)
            assert order is not None
            self.assertEqual(order["amount"], 10.0)
            self.assertEqual(order["currency"], "INR")

            with patch.object(
                cashfree_gateway,
                "fetch_order",
                return_value={
                    "order_id": order["order_id"],
                    "order_amount": 10.0,
                    "order_currency": "INR",
                    "order_status": "PAID",
                },
            ), patch.object(
                cashfree_gateway,
                "fetch_order_payments",
                return_value=[
                    {
                        "order_id": order["order_id"],
                        "payment_status": "SUCCESS",
                        "payment_amount": 10.0,
                        "payment_currency": "INR",
                        "cf_payment_id": "cf_order_test_1",
                    }
                ],
            ):
                verified = self.client.post(
                    "/api/dashboard-payments/verify",
                    json={"order_id": order["order_id"]},
                )
        self.assertEqual(verified.status_code, 200)
        verified_payload = verified.get_json()
        self.assertIsNotNone(verified_payload)
        assert verified_payload is not None
        self.assertTrue(verified_payload["verified"])
        self.assertTrue(verified_payload["allowed"])
        self.assertEqual(verified_payload["transaction_id"], "cf_order_test_1")

    def test_signed_webhook_is_idempotent(self):
        ids = self.create_dashboard()
        order_id = "gw_" + "d" * 32
        conn = database.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO payments(
                    user_id, email, amount, method, order_id, currency, payment_status,
                    status, dashboard_id, report_id, gateway_provider, amount_paise
                )
                VALUES (?, ?, 10, 'Cashfree', ?, 'INR', 'PENDING',
                        'pending', ?, ?, 'cashfree', 1000)
                """,
                (self.user["id"], self.user["email"], order_id, ids["dashboard_id"], ids["report_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        payload = json.dumps(
            {
                "id": "evt_test_1",
                "type": "PAYMENT_SUCCESS_WEBHOOK",
                "data": {
                    "order": {"order_id": order_id, "order_amount": 10.0, "order_currency": "INR"},
                    "payment": {
                        "order_id": order_id,
                        "payment_status": "SUCCESS",
                        "payment_amount": 10.0,
                        "payment_currency": "INR",
                        "cf_payment_id": "cf_webhook_test_1",
                    },
                },
            }
        )
        headers = {"x-webhook-timestamp": "1700000000000", "x-webhook-signature": "valid"}
        with patch.object(cashfree_gateway, "verify_webhook_signature", return_value=True):
            first = self.client.post(
                "/api/dashboard-payments/webhook", data=payload, headers=headers,
                content_type="application/json",
            )
            duplicate = self.client.post(
                "/api/dashboard-payments/webhook", data=payload, headers=headers,
                content_type="application/json",
            )
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()
        self.assertIsNotNone(first_payload)
        assert first_payload is not None
        self.assertTrue(first_payload["processed"])
        self.assertEqual(duplicate.status_code, 200)
        duplicate_payload = duplicate.get_json()
        self.assertIsNotNone(duplicate_payload)
        assert duplicate_payload is not None
        self.assertTrue(duplicate_payload["duplicate"])

    def test_cashfree_webhook_signature_is_verified_over_timestamp_and_raw_body(self):
        body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
        timestamp = "1700000000000"
        with patch.dict(os.environ, {"CASHFREE_SECRET_KEY": "test-secret", "CASHFREE_WEBHOOK_SECRET": ""}):
            signature = base64.b64encode(
                hmac.new(b"test-secret", timestamp.encode() + body, hashlib.sha256).digest()
            ).decode()
            self.assertTrue(cashfree_gateway.verify_webhook_signature(timestamp, signature, body))
            self.assertFalse(cashfree_gateway.verify_webhook_signature(timestamp, "invalid", body))


if __name__ == "__main__":
    unittest.main()
