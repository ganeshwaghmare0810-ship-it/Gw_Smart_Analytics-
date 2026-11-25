import hashlib
import io
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import database


class TestAccountDataSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db_path = database.DB_PATH
        cls.original_upload_dir = database.UPLOAD_DIR
        cls.bootstrap_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = Path(cls.bootstrap_dir.name) / "bootstrap.sqlite"
        database.UPLOAD_DIR = Path(cls.bootstrap_dir.name) / "uploads"
        database.UPLOAD_DIR.mkdir()

        import server

        cls.server = server
        cls.server.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls.original_db_path
        database.UPLOAD_DIR = cls.original_upload_dir
        cls.bootstrap_dir.cleanup()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = Path(self.temp_dir.name) / "gw_analytics.db"
        database.UPLOAD_DIR = Path(self.temp_dir.name) / "private_uploads"
        database.UPLOAD_DIR.mkdir()
        self.server.UPLOAD_DIR = database.UPLOAD_DIR
        database.init_db()
        self.client_a = self.server.app.test_client()
        self.client_b = self.server.app.test_client()
        self.user_a = database.create_user("Account A", " AccountA@Example.com ", "password1")["user"]
        self.user_b = database.create_user("Account B", "accountb@example.com", "password2")["user"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def login(self, client, user):
        with patch.object(self.server.mailer, "send_login_notification", return_value=True):
            response = client.post(
                "/api/login",
                json={"email": user["email"], "password": "password1" if user["id"] == self.user_a["id"] else "password2"},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def test_registration_normalizes_email_and_rejects_normalized_duplicate(self):
        duplicate = database.create_user("Duplicate", "accounta@example.com", "password3")
        self.assertFalse(duplicate["success"])
        self.assertEqual(database.get_user_by_email("  ACCOUNTA@EXAMPLE.COM ")["email"], "accounta@example.com")

    def test_account_widget_is_injected_into_html_pages(self):
        for page in ("/login.html", "/up.html", "/about.html"):
            with self.subTest(page=page):
                response = self.client_a.get(page)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn("data-gw-current-account-widget", html)
                self.assertIn('"/api/me"', html)
                self.assertIn('"/login.html"', html)
                self.assertIn("gw-account-menu", html)
                self.assertEqual(html.count("data-gw-current-account-widget"), 1)

    def test_account_widget_is_injected_into_every_html_page(self):
        for path in self.server.BASE_DIR.glob("*.html"):
            with self.subTest(page=path.name):
                response = self.client_a.get("/" + path.name)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn("data-gw-current-account-widget", html)
                self.assertEqual(html.count("data-gw-current-account-widget"), 1)

    def test_profile_is_server_validated_and_logout_ends_the_session(self):
        self.login(self.client_a, self.user_a)

        profile = self.client_a.get("/api/me")
        self.assertEqual(profile.status_code, 200)
        self.assertTrue(profile.get_json()["authenticated"])
        self.assertEqual(profile.get_json()["user"]["name"], self.user_a["name"])
        self.assertEqual(profile.get_json()["user"]["email"], self.user_a["email"])

        logout = self.client_a.post("/api/logout")
        self.assertEqual(logout.status_code, 200)
        profile_after_logout = self.client_a.get("/api/me")
        self.assertFalse(profile_after_logout.get_json()["authenticated"])
        self.assertEqual(self.client_a.get("/api/shoppulse/shop").status_code, 401)

    def test_dashboard_link_redirects_with_authenticated_session_bridge(self):
        guest_response = self.client_a.get("/dashboard")
        self.assertEqual(guest_response.status_code, 302)
        self.assertEqual(guest_response.headers["Location"], "/login.html")

        self.login(self.client_a, self.user_a)
        response = self.client_a.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        bridge_user = database.validate_session_bridge_token(query["gw_auth_token"][0])
        self.assertEqual(bridge_user["id"], self.user_a["id"])

    def test_configure_admin_user_promotes_only_an_existing_account(self):
        self.assertTrue(database.configure_admin_user("  ACCOUNTA@EXAMPLE.COM "))
        conn = database.get_db_connection()
        try:
            role = conn.execute(
                "SELECT role FROM users WHERE id = ?", (self.user_a["id"],)
            ).fetchone()["role"]
        finally:
            conn.close()
        self.assertEqual(role, "admin")
        self.assertFalse(database.configure_admin_user("missing@example.com"))

    def test_second_login_revokes_browser_a_and_logout_preserves_unrelated_bridge_tokens(self):
        self.login(self.client_a, self.user_a)
        old_web_token = self.client_a.get_cookie("gw_web_session").value
        old_session = database.validate_web_session(old_web_token)
        linked_bridge = database.create_session_bridge_token(
            self.user_a["id"], web_session_id=old_session["web_session_id"]
        )
        unrelated_bridge = database.create_session_bridge_token(self.user_a["id"])

        self.login(self.client_b, self.user_a)
        current_web_token = self.client_b.get_cookie("gw_web_session").value
        self.assertEqual(self.client_a.get("/api/shoppulse/shop").status_code, 401)
        self.assertEqual(self.client_b.get("/api/shoppulse/shop").status_code, 200)
        self.assertIsNone(database.validate_session_bridge_token(linked_bridge))

        self.client_b.post("/api/logout")
        self.assertEqual(self.client_b.get("/api/shoppulse/shop").status_code, 401)
        self.assertIsNone(database.validate_web_session(current_web_token))
        self.assertEqual(database.validate_session_bridge_token(unrelated_bridge)["id"], self.user_a["id"])

    def test_concurrent_session_creation_leaves_only_the_last_session_active(self):
        first = database.create_web_session(self.user_a["id"])
        second = database.create_web_session(self.user_a["id"])
        self.assertIsNone(database.validate_web_session(first["token"]))
        self.assertEqual(database.validate_web_session(second["token"])["id"], self.user_a["id"])

        conn = database.get_db_connection()
        try:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ? AND revoked_at IS NULL",
                (self.user_a["id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(active_count, 1)

    def test_default_bridge_token_lifetime_matches_web_session_idle_ttl(self):
        created_at = time.time()
        token = database.create_session_bridge_token(self.user_a["id"])
        conn = database.get_db_connection()
        try:
            row = conn.execute(
                "SELECT expires_at FROM auth_tokens WHERE token = ?", (token,)
            ).fetchone()
        finally:
            conn.close()

        self.assertGreaterEqual(
            row["expires_at"],
            created_at + database.WEB_SESSION_IDLE_TTL_SECONDS - 1,
        )

    def test_pending_payment_requires_admin_approval_and_receipt_is_idempotent(self):
        pending = database.submit_pending_payment(
            self.user_a["id"], "attacker@example.com", 10, "UPI", "123456789012"
        )
        self.assertTrue(pending["success"])
        self.assertFalse(database.check_subscription_status(user_id=self.user_a["id"])["active"])
        self.assertIsNone(database.get_latest_receipt(user_id=self.user_a["id"]))

        approved = database.approve_pending_payment(pending["payment_request_id"], validity_days=30)
        self.assertTrue(approved["success"])
        self.assertTrue(database.check_subscription_status(user_id=self.user_a["id"])["active"])
        self.assertFalse(database.approve_pending_payment(pending["payment_request_id"])["success"])
        self.assertFalse(
            database.submit_pending_payment(
                self.user_a["id"], self.user_a["email"], 10, "UPI", "555555555555"
            )["success"]
        )

        payments = database.get_user_payment_history(self.user_a["id"])
        subscriptions = database.get_user_subscription_history(self.user_a["id"])
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["payment_status"], "SUCCESS")
        self.assertEqual(len(subscriptions), 1)
        self.assertTrue(subscriptions[0]["receipt_number"])

        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM receipts WHERE user_id = ?", (self.user_a["id"],)).fetchone()[0], 1)
        finally:
            conn.close()

    def test_rejected_payment_does_not_create_access_or_receipt(self):
        pending = database.submit_pending_payment(
            self.user_a["id"], self.user_a["email"], 10, "UPI", "123456789013"
        )
        self.assertTrue(database.reject_pending_payment(pending["payment_request_id"])["success"])
        status = database.get_latest_payment_status(self.user_a["id"])
        self.assertEqual(status["status"], "REJECTED")
        self.assertFalse(status["pending"])
        self.assertFalse(database.check_subscription_status(user_id=self.user_a["id"])["active"])
        self.assertIsNone(database.get_latest_receipt(user_id=self.user_a["id"]))

    def test_expired_subscription_is_marked_expired_and_access_is_denied(self):
        pending = database.submit_pending_payment(
            self.user_a["id"], self.user_a["email"], 10, "UPI", "123456789014"
        )
        self.assertTrue(database.approve_pending_payment(pending["payment_request_id"])["success"])
        conn = database.get_db_connection()
        try:
            conn.execute(
                """
                UPDATE subscriptions
                SET end_date = '2000-01-01 00:00:00', expiry_date = '2000-01-01 00:00:00'
                WHERE user_id = ?
                """,
                (self.user_a["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertFalse(database.check_subscription_status(user_id=self.user_a["id"])["active"])
        conn = database.get_db_connection()
        try:
            row = conn.execute(
                "SELECT status FROM subscriptions WHERE user_id = ?",
                (self.user_a["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "EXPIRED")

    def test_payment_submission_uses_authenticated_account_and_rejects_duplicate_utr(self):
        self.login(self.client_a, self.user_a)
        response = self.client_a.post(
            "/api/payment/submit",
            json={"user_id": self.user_b["id"], "email": self.user_b["email"], "utr_number": "123456789015"},
        )
        self.assertEqual(response.status_code, 202, response.get_json())

        conn = database.get_db_connection()
        try:
            payment = conn.execute("SELECT user_id, status FROM payments").fetchone()
        finally:
            conn.close()
        self.assertEqual(payment["user_id"], self.user_a["id"])
        self.assertEqual(payment["status"], "pending")
        self.assertFalse(database.check_subscription_status(user_id=self.user_a["id"])["active"])

        self.login(self.client_b, self.user_b)
        duplicate = self.client_b.post(
            "/api/payment/submit",
            json={"utr_number": "123456789015"},
        )
        self.assertEqual(duplicate.status_code, 400)

    def test_gateway_payment_creates_shared_monthly_subscription_idempotently(self):
        order_id = "order_test_shared"
        payment_id = "pay_test_shared"
        amount_paise = int(round(self.server.PLAN_PRICE * 100))
        self.assertTrue(database.create_gateway_order(self.user_a["id"], order_id, amount_paise)["success"])

        wrong_user = database.complete_gateway_order(
            self.user_b["id"], order_id, payment_id, validity_days=30
        )
        self.assertFalse(wrong_user["success"])
        self.assertFalse(database.check_subscription_status(user_id=self.user_b["id"])["active"])

        completed = database.complete_gateway_order(
            self.user_a["id"], order_id, payment_id, validity_days=30
        )
        repeated = database.complete_gateway_order(
            self.user_a["id"], order_id, payment_id, validity_days=30
        )
        self.assertTrue(completed["success"])
        self.assertTrue(repeated["success"])
        self.assertEqual(completed["receipt"]["payment_id"], payment_id)
        self.assertEqual(repeated["receipt"]["payment_id"], payment_id)
        self.assertEqual(database.check_subscription_status(user_id=self.user_a["id"])["days_left"], 30)

        conn = database.get_db_connection()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (self.user_a["id"],)).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_gateway_api_authenticates_order_owner_and_verifies_captured_amount(self):
        self.login(self.client_a, self.user_a)
        amount_paise = int(round(self.server.PLAN_PRICE * 100))
        with patch.object(self.server.gwpeyment, "RAZORPAY_KEY_ID", "rzp_test_key"), patch.object(
            self.server.gwpeyment, "RAZORPAY_KEY_SECRET", "test_secret"
        ), patch.object(
            self.server.gwpeyment,
            "razorpay_create_order",
            return_value={"id": "order_api_test", "amount": amount_paise, "currency": "INR"},
        ):
            created = self.client_a.post("/api/payment/create-order", json={"plan": "monthly_pro"})
        self.assertEqual(created.status_code, 200, created.get_json())
        self.assertEqual(created.get_json()["order_id"], "order_api_test")

        self.login(self.client_b, self.user_b)
        with patch.object(
            self.server.gwpeyment, "razorpay_get_payment"
        ) as fetch_payment:
            wrong_owner = self.client_b.post(
                "/api/payment/verify",
                json={
                    "razorpay_order_id": "order_api_test",
                    "razorpay_payment_id": "pay_api_test",
                    "razorpay_signature": "valid",
                },
            )
        self.assertEqual(wrong_owner.status_code, 404)
        fetch_payment.assert_not_called()

        self.login(self.client_a, self.user_a)
        with patch.object(self.server.gwpeyment, "razorpay_verify_signature", return_value=True), patch.object(
            self.server.gwpeyment,
            "razorpay_get_payment",
            return_value={
                "id": "pay_api_test",
                "order_id": "order_api_test",
                "amount": amount_paise,
                "currency": "INR",
                "status": "captured",
            },
        ):
            verified = self.client_a.post(
                "/api/payment/verify",
                json={
                    "razorpay_order_id": "order_api_test",
                    "razorpay_payment_id": "pay_api_test",
                    "razorpay_signature": "valid",
                },
            )
        self.assertEqual(verified.status_code, 200, verified.get_json())
        self.assertTrue(verified.get_json()["subscription_active"])
        self.assertTrue(database.check_subscription_status(user_id=self.user_a["id"])["active"])

    def test_shop_pulse_uses_authenticated_owner_not_client_supplied_user_id(self):
        self.login(self.client_a, self.user_a)
        created = self.client_a.get(f"/api/shoppulse/shop?user_id={self.user_b['id']}")
        self.assertEqual(created.status_code, 200)
        shop_a = created.get_json()["shop"]
        self.assertEqual(shop_a["user_id"], self.user_a["id"])

        product = self.client_a.post(
            "/api/shoppulse/products",
            json={"sku": "sku-unique", "name": "Private product", "stock": 2},
        )
        self.assertEqual(product.status_code, 201)
        self.login(self.client_b, self.user_b)
        shop_b = self.client_b.get("/api/shoppulse/shop").get_json()["shop"]
        self.assertNotEqual(shop_a["id"], shop_b["id"])
        self.assertEqual(self.client_b.get("/api/shoppulse/products").get_json()["products"], [])
        self.assertEqual(
            self.server.app.test_client().get("/api/shoppulse/shop").status_code,
            401,
        )

    def test_uploads_are_persistent_private_and_scoped_to_the_owner(self):
        self.login(self.client_a, self.user_a)
        content = b"name,amount\nA,1\nB,2\n"
        first = self.client_a.post(
            "/api/upload",
            data={
                "user_id": str(self.user_b["id"]),
                "file": (io.BytesIO(content), "same.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        upload_a = first.get_json()
        self.assertEqual(upload_a["row_count"], 2)
        self.assertEqual(upload_a["column_names"], ["name", "amount"])
        self.assertEqual(upload_a["checksum"], hashlib.sha256(content).hexdigest())
        self.assertTrue((database.UPLOAD_DIR / upload_a["saved_name"]).is_file())

        self.login(self.client_b, self.user_b)
        second = self.client_b.post(
            "/api/upload",
            data={"file": (io.BytesIO(content), "same.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(second.status_code, 201, second.get_json())
        upload_b = second.get_json()
        self.assertNotEqual(upload_a["saved_name"], upload_b["saved_name"])
        self.assertEqual(self.client_b.get(f"/api/uploads/{upload_a['saved_name']}").status_code, 404)
        self.assertEqual(len(self.client_b.get("/api/uploads").get_json()["uploads"]), 1)

        self.login(self.client_a, self.user_a)
        download = self.client_a.get(f"/api/uploads/{upload_a['saved_name']}")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, content)
        download.close()

        invalid = self.client_a.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"bad"), "malware.exe")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)

        self.login(self.client_b, self.user_b)
        self.assertEqual(self.client_b.delete(f"/api/uploads/{upload_a['id']}").status_code, 404)

    def test_tabular_parser_is_authenticated_and_blocks_pickle_files(self):
        response = self.client_a.post(
            "/api/parse-dataset",
            data={"file": (io.BytesIO(b'[{"product":"A","sales":12}]'), "sales.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 401)

        self.login(self.client_a, self.user_a)
        parsed = self.client_a.post(
            "/api/parse-dataset",
            data={"file": (io.BytesIO(b'[{"product":"A","sales":12}]'), "sales.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(parsed.status_code, 200, parsed.get_json())
        self.assertEqual(parsed.get_json()["rows"][0]["sales"], 12)

        pickle_upload = self.client_a.post(
            "/api/parse-dataset",
            data={"file": (io.BytesIO(b"not a pickle"), "model.pkl")},
            content_type="multipart/form-data",
        )
        self.assertEqual(pickle_upload.status_code, 400)
        self.assertIn("arbitrary code", pickle_upload.get_json()["error"])

    def test_legacy_migration_backs_up_and_reports_duplicates_without_deleting_rows(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
                phone TEXT DEFAULT '', mobile TEXT DEFAULT '', role TEXT DEFAULT 'user',
                is_paid INTEGER DEFAULT 0, subscription_plan TEXT DEFAULT 'free',
                subscription_start TIMESTAMP, subscription_end TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
            [
                ("Legacy 1", "same@example.com", "hash"),
                ("Legacy 2", " Same@Example.com ", "hash"),
            ],
        )
        conn.commit()
        conn.close()

        previous_path = database.DB_PATH
        database.DB_PATH = legacy_path
        try:
            report = database.init_db()
            self.assertTrue(report["backup_path"])
            self.assertTrue(Path(report["backup_path"]).is_file())
            self.assertIn("ux_users_email_normalized", report["conflicts"])
            conn = database.get_db_connection()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
            finally:
                conn.close()
            duplicate_registration = database.create_user("Legacy 3", "same@example.com", "password")
            self.assertFalse(duplicate_registration["success"])
        finally:
            database.DB_PATH = previous_path


if __name__ == "__main__":
    unittest.main()
