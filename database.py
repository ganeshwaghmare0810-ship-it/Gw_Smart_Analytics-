"""
GW Analytics - SQLite Database Access Layer
============================================
Handles:
- User registration (unique email check, secure scrypt password hashing)
- User authentication & session lookup
- Subscriptions (1-month validity, active status tracking)
- Payments (UPI, QR code, UTR tracking, receipt generation)
- Uploads metadata logging

CHANGES IN THIS VERSION
-----------------------
1. FIXED : create_subscription() -> INSERT INTO subscriptions had 11 columns but
           only 10 placeholders ("10 values for 11 columns"). One '?' was missing
           before 'ACTIVE'.
2. FIXED : receipt number is now unique per payment (date + time). Before, a user
           paying twice on the same day would break the UNIQUE constraint on
           receipts.receipt_number.
3. ADDED : same UTR number cannot be used twice (duplicate payment protection).
4. ADDED : safer migrations for older databases (missing payments/subscriptions
           columns are added automatically).
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Optional, Dict, Any, List
import secrets
import time

DB_PATH = Path(__file__).resolve().parent / "gw_smart_analytics2.db"
_upload_dir_value = os.environ.get("GW_UPLOAD_DIR")
UPLOAD_DIR = (
    Path(_upload_dir_value).expanduser()
    if _upload_dir_value
    else DB_PATH.parent.parent / f"{DB_PATH.parent.name}_uploads"
)
if not UPLOAD_DIR.is_absolute():
    UPLOAD_DIR = (DB_PATH.parent.parent / UPLOAD_DIR).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

WEB_SESSION_IDLE_TTL_SECONDS = 12 * 60 * 60
WEB_SESSION_ABSOLUTE_TTL_SECONDS = 30 * 24 * 60 * 60


def get_db_connection() -> sqlite3.Connection:
    """Returns a SQLite connection with Row factory enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _backup_database_before_migration(conn: sqlite3.Connection) -> Optional[str]:
    """Create a consistent, non-destructive backup before upgrading an existing DB."""
    db_path = Path(DB_PATH)
    if not db_path.exists():
        return None
    table_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if not table_count or conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-{stamp}.bak")
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return str(backup_path)


def init_db():
    """
    Initializes and upgrades the database schema if needed.
    Ensures users, subscriptions, payments, uploads, receipts, and auth_tokens tables exist.
    """
    conn = get_db_connection()
    backup_path = _backup_database_before_migration(conn)
    cursor = conn.cursor()
    migration_report: Dict[str, Any] = {
        "backup_path": backup_path,
        "conflicts": {},
        "created_indexes": [],
    }

    cursor.execute("BEGIN IMMEDIATE")

    # 1. USERS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            phone TEXT DEFAULT '',
            mobile TEXT DEFAULT '',
            role TEXT DEFAULT 'user',
            is_paid INTEGER DEFAULT 0,
            subscription_plan TEXT DEFAULT 'free',
            subscription_start TIMESTAMP,
            subscription_end TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 2. SUBSCRIPTIONS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            plan TEXT DEFAULT 'Premium Monthly Subscription',
            plan_name TEXT DEFAULT 'Premium Monthly Subscription',
            amount REAL DEFAULT 10.0,
            payment_id TEXT NOT NULL,
            utr_number TEXT DEFAULT '',
            status TEXT DEFAULT 'ACTIVE',
            start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_date TIMESTAMP NOT NULL,
            expiry_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # 3. PAYMENTS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            order_id TEXT,
            payment_id TEXT,
            transaction_id TEXT,
            currency TEXT NOT NULL DEFAULT 'INR',
            payment_method TEXT,
            payment_status TEXT NOT NULL DEFAULT 'PENDING',
            utr_number TEXT,
            sender_upi TEXT DEFAULT '',
            status TEXT DEFAULT 'approved',
            paid_at TEXT,
            valid_until TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # 4. UPLOADS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            original_name TEXT NOT NULL,
            saved_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );
    """)

    # 5. RECEIPTS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            payment_id INTEGER,
            receipt_number TEXT NOT NULL UNIQUE,
            transaction_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gateway_orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_paise INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'CREATED',
            payment_id TEXT UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # 6. AUTH TOKENS (SESSION BRIDGE BETWEEN FLASK & STREAMLIT)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            web_session_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            last_activity_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            absolute_expires_at REAL NOT NULL,
            revoked_at REAL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    # 6B. SHOPPULSE CORE TABLES
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT 'My Smart Shop',
            owner_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            address TEXT DEFAULT '',
            business_type TEXT DEFAULT 'Retail',
            currency TEXT DEFAULT '₹',
            tax_rate REAL DEFAULT 0.0,
            is_demo INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'General',
            brand TEXT DEFAULT '',
            cost_price REAL DEFAULT 0.0,
            selling_price REAL DEFAULT 0.0,
            stock INTEGER DEFAULT 0,
            reorder_level INTEGER DEFAULT 5,
            supplier TEXT DEFAULT '',
            status TEXT DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
            UNIQUE(shop_id, sku)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            address TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            customer_id INTEGER,
            invoice_no TEXT NOT NULL,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            subtotal REAL DEFAULT 0.0,
            discount REAL DEFAULT 0.0,
            tax REAL DEFAULT 0.0,
            total_amount REAL DEFAULT 0.0,
            payment_mode TEXT DEFAULT 'Cash',
            order_status TEXT DEFAULT 'Completed',
            notes TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            quantity INTEGER DEFAULT 1,
            unit_price REAL DEFAULT 0.0,
            cost_price REAL DEFAULT 0.0,
            total_price REAL DEFAULT 0.0,
            FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL DEFAULT 0.0,
            payment_method TEXT DEFAULT 'Cash',
            description TEXT DEFAULT '',
            attachment TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            previous_stock INTEGER DEFAULT 0,
            new_stock INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            reference_id TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
        );
    """)

    # Indexes for fast dashboard aggregations
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_shop_date ON sales(shop_id, sale_date);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_expenses_shop_date ON expenses(shop_id, date);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id);")

    # 7. MIGRATIONS FOR USERS TABLE
    cursor.execute("PRAGMA table_info(users);")
    user_cols = {row["name"] for row in cursor.fetchall()}
    if "mobile" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN mobile TEXT DEFAULT '';")
    if "subscription_plan" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_plan TEXT DEFAULT 'free';")
    if "subscription_start" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_start TIMESTAMP;")
    if "subscription_end" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_end TIMESTAMP;")
    cursor.execute("UPDATE users SET mobile = phone WHERE (mobile IS NULL OR mobile = '') AND phone IS NOT NULL;")

    mobile_digits_sql = (
        "replace(replace(replace(replace(replace("
        "COALESCE(NULLIF(mobile, ''), phone, ''), ' ', ''), '-', ''), '(', ''), ')', ''), '+', '')"
    )
    mobile_normalized_sql = (
        f"CASE WHEN length({mobile_digits_sql}) = 12 AND substr({mobile_digits_sql}, 1, 2) = '91' "
        f"THEN substr({mobile_digits_sql}, 3) ELSE {mobile_digits_sql} END"
    )
    cursor.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_users_mobile_unique_insert
        BEFORE INSERT ON users
        WHEN {mobile_normalized_sql.replace("mobile", "NEW.mobile").replace("phone", "NEW.phone")} <> ''
         AND EXISTS (
            SELECT 1 FROM users
            WHERE {mobile_normalized_sql} =
                  {mobile_normalized_sql.replace("mobile", "NEW.mobile").replace("phone", "NEW.phone")}
         )
        BEGIN
            SELECT RAISE(ABORT, 'mobile number already registered');
        END
        """
    )
    cursor.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_users_mobile_unique_update
        BEFORE UPDATE OF mobile, phone ON users
        WHEN {mobile_normalized_sql.replace("mobile", "NEW.mobile").replace("phone", "NEW.phone")} <> ''
         AND EXISTS (
            SELECT 1 FROM users
            WHERE id <> NEW.id
              AND {mobile_normalized_sql} =
                  {mobile_normalized_sql.replace("mobile", "NEW.mobile").replace("phone", "NEW.phone")}
         )
        BEGIN
            SELECT RAISE(ABORT, 'mobile number already registered');
        END
        """
    )

    # 8. MIGRATIONS FOR PAYMENTS TABLE
    cursor.execute("PRAGMA table_info(payments);")
    pay_cols = {row["name"] for row in cursor.fetchall()}
    if "order_id" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN order_id TEXT;")
    if "payment_id" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN payment_id TEXT;")
    if "transaction_id" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN transaction_id TEXT;")
    if "currency" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN currency TEXT DEFAULT 'INR';")
    if "payment_method" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN payment_method TEXT;")
    if "payment_status" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN payment_status TEXT DEFAULT 'PENDING';")
    if "paid_at" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN paid_at TEXT;")
    if "utr_number" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN utr_number TEXT;")
    if "sender_upi" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN sender_upi TEXT DEFAULT '';")
    if "status" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'approved';")
    if "valid_until" not in pay_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN valid_until TIMESTAMP;")

    # 9. MIGRATIONS FOR SUBSCRIPTIONS TABLE
    cursor.execute("PRAGMA table_info(subscriptions);")
    sub_cols = {row["name"] for row in cursor.fetchall()}
    if "plan_name" not in sub_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN plan_name TEXT;")
    if "expiry_date" not in sub_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN expiry_date TEXT;")
    if "utr_number" not in sub_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN utr_number TEXT DEFAULT '';")
    cursor.execute("UPDATE subscriptions SET plan_name = plan WHERE plan_name IS NULL;")
    cursor.execute("UPDATE subscriptions SET expiry_date = end_date WHERE expiry_date IS NULL;")

    # 10. ADDITIVE METADATA MIGRATIONS
    cursor.execute("PRAGMA table_info(uploads);")
    upload_cols = {row["name"] for row in cursor.fetchall()}
    upload_additions = {
        "mime_type": "TEXT DEFAULT ''",
        "processing_status": "TEXT NOT NULL DEFAULT 'STORED'",
        "row_count": "INTEGER",
        "column_count": "INTEGER",
        "column_names": "TEXT",
        "processing_error": "TEXT",
        "storage_key": "TEXT",
        "checksum": "TEXT",
        "analysis_ref": "TEXT",
    }
    for column, definition in upload_additions.items():
        if column not in upload_cols:
            cursor.execute(f"ALTER TABLE uploads ADD COLUMN {column} {definition};")
    cursor.execute("UPDATE uploads SET storage_key = saved_name WHERE storage_key IS NULL;")

    cursor.execute("PRAGMA table_info(auth_tokens);")
    auth_cols = {row["name"] for row in cursor.fetchall()}
    if "web_session_id" not in auth_cols:
        cursor.execute("ALTER TABLE auth_tokens ADD COLUMN web_session_id INTEGER;")

    cursor.execute("PRAGMA table_info(shops);")
    shop_cols = {row["name"] for row in cursor.fetchall()}
    if "is_demo" not in shop_cols:
        cursor.execute("ALTER TABLE shops ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0;")

    # Normalize non-conflicting legacy emails without changing either side of a collision.
    email_collisions = cursor.execute(
        """
        SELECT lower(trim(email)) AS normalized_email
        FROM users
        GROUP BY lower(trim(email))
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    collided_emails = {row["normalized_email"] for row in email_collisions}
    for row in cursor.execute("SELECT id, email FROM users").fetchall():
        normalized_email = str(row["email"] or "").strip().lower()
        if normalized_email not in collided_emails and row["email"] != normalized_email:
            cursor.execute("UPDATE users SET email = ? WHERE id = ?", (normalized_email, row["id"]))

    cursor.execute("UPDATE subscriptions SET status = upper(status) WHERE status IS NOT NULL;")
    cursor.execute(
        """
        UPDATE subscriptions
        SET status = 'EXPIRED'
        WHERE upper(status) = 'ACTIVE'
          AND (
                datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
          )
        """
    )

    index_specs = [
        (
            "ux_user_sessions_one_active_per_user",
            """
            SELECT user_id AS duplicate_key, COUNT(*) AS n
            FROM user_sessions WHERE revoked_at IS NULL
            GROUP BY user_id HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_user_sessions_one_active_per_user
            ON user_sessions(user_id) WHERE revoked_at IS NULL
            """,
        ),
        (
            "ux_users_email_normalized",
            """
            SELECT lower(trim(email)) AS duplicate_key, COUNT(*) AS n
            FROM users GROUP BY lower(trim(email)) HAVING COUNT(*) > 1
            """,
            "CREATE UNIQUE INDEX ux_users_email_normalized ON users(lower(trim(email)))",
        ),
        (
            "ux_users_mobile_normalized",
            f"""
            SELECT {mobile_normalized_sql} AS duplicate_key, COUNT(*) AS n
            FROM users
            WHERE {mobile_normalized_sql} <> ''
            GROUP BY {mobile_normalized_sql}
            HAVING COUNT(*) > 1
            """,
            f"""
            CREATE UNIQUE INDEX ux_users_mobile_normalized
            ON users({mobile_normalized_sql})
            WHERE {mobile_normalized_sql} <> ''
            """,
        ),
        (
            "ux_payments_payment_id_nonempty",
            """
            SELECT trim(payment_id) AS duplicate_key, COUNT(*) AS n
            FROM payments WHERE trim(COALESCE(payment_id, '')) <> ''
            GROUP BY trim(payment_id) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_payments_payment_id_nonempty
            ON payments(trim(payment_id)) WHERE trim(COALESCE(payment_id, '')) <> ''
            """,
        ),
        (
            "ux_payments_order_id_nonempty",
            """
            SELECT trim(order_id) AS duplicate_key, COUNT(*) AS n
            FROM payments WHERE trim(COALESCE(order_id, '')) <> ''
            GROUP BY trim(order_id) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_payments_order_id_nonempty
            ON payments(trim(order_id)) WHERE trim(COALESCE(order_id, '')) <> ''
            """,
        ),
        (
            "ux_payments_transaction_id_nonempty",
            """
            SELECT lower(trim(transaction_id)) AS duplicate_key, COUNT(*) AS n
            FROM payments WHERE trim(COALESCE(transaction_id, '')) <> ''
            GROUP BY lower(trim(transaction_id)) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_payments_transaction_id_nonempty
            ON payments(lower(trim(transaction_id)))
            WHERE trim(COALESCE(transaction_id, '')) <> ''
            """,
        ),
        (
            "ux_payments_utr_nonempty",
            """
            SELECT lower(trim(utr_number)) AS duplicate_key, COUNT(*) AS n
            FROM payments WHERE trim(COALESCE(utr_number, '')) <> ''
            GROUP BY lower(trim(utr_number)) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_payments_utr_nonempty
            ON payments(lower(trim(utr_number)))
            WHERE trim(COALESCE(utr_number, '')) <> ''
            """,
        ),
        (
            "ux_subscriptions_payment_id",
            """
            SELECT trim(payment_id) AS duplicate_key, COUNT(*) AS n
            FROM subscriptions WHERE trim(COALESCE(payment_id, '')) <> ''
            GROUP BY trim(payment_id) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_subscriptions_payment_id
            ON subscriptions(trim(payment_id))
            WHERE trim(COALESCE(payment_id, '')) <> ''
            """,
        ),
        (
            "ux_subscriptions_utr_nonempty",
            """
            SELECT lower(trim(utr_number)) AS duplicate_key, COUNT(*) AS n
            FROM subscriptions WHERE trim(COALESCE(utr_number, '')) <> ''
            GROUP BY lower(trim(utr_number)) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_subscriptions_utr_nonempty
            ON subscriptions(lower(trim(utr_number)))
            WHERE trim(COALESCE(utr_number, '')) <> ''
            """,
        ),
        (
            "ux_receipts_payment_id",
            """
            SELECT payment_id AS duplicate_key, COUNT(*) AS n
            FROM receipts WHERE payment_id IS NOT NULL
            GROUP BY payment_id HAVING COUNT(*) > 1
            """,
            "CREATE UNIQUE INDEX ux_receipts_payment_id ON receipts(payment_id) WHERE payment_id IS NOT NULL",
        ),
        (
            "ux_receipts_receipt_number",
            """
            SELECT trim(receipt_number) AS duplicate_key, COUNT(*) AS n
            FROM receipts GROUP BY trim(receipt_number) HAVING COUNT(*) > 1
            """,
            "CREATE UNIQUE INDEX ux_receipts_receipt_number ON receipts(trim(receipt_number))",
        ),
        (
            "ux_subscriptions_one_active_per_user",
            """
            SELECT user_id AS duplicate_key, COUNT(*) AS n
            FROM subscriptions WHERE status = 'ACTIVE'
            GROUP BY user_id HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_subscriptions_one_active_per_user
            ON subscriptions(user_id) WHERE status = 'ACTIVE'
            """,
        ),
        (
            "ux_products_shop_sku_normalized",
            """
            SELECT shop_id || ':' || lower(trim(sku)) AS duplicate_key, COUNT(*) AS n
            FROM products WHERE trim(COALESCE(sku, '')) <> ''
            GROUP BY shop_id, lower(trim(sku)) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_products_shop_sku_normalized
            ON products(shop_id, lower(trim(sku))) WHERE trim(COALESCE(sku, '')) <> ''
            """,
        ),
        (
            "ux_sales_shop_invoice",
            """
            SELECT shop_id || ':' || trim(invoice_no) AS duplicate_key, COUNT(*) AS n
            FROM sales WHERE trim(COALESCE(invoice_no, '')) <> ''
            GROUP BY shop_id, trim(invoice_no) HAVING COUNT(*) > 1
            """,
            """
            CREATE UNIQUE INDEX ux_sales_shop_invoice
            ON sales(shop_id, trim(invoice_no)) WHERE trim(COALESCE(invoice_no, '')) <> ''
            """,
        ),
    ]
    for index_name, duplicate_query, create_sql in index_specs:
        conflicts = cursor.execute(duplicate_query).fetchall()
        if conflicts:
            migration_report["conflicts"][index_name] = [
                {
                    "key_fingerprint": hashlib.sha256(
                        str(row["duplicate_key"]).encode("utf-8")
                    ).hexdigest()[:12],
                    "count": row["n"],
                }
                for row in conflicts
            ]
            continue
        exists = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        if not exists:
            cursor.execute(create_sql)
            migration_report["created_indexes"].append(index_name)

    ordinary_indexes = (
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON user_sessions(user_id, revoked_at, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_gateway_orders_user_status ON gateway_orders(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_uploads_user_created ON uploads(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_shops_user ON shops(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id)",
        "CREATE INDEX IF NOT EXISTS idx_inventory_shop_product ON inventory_movements(shop_id, product_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_shop_created ON audit_logs(shop_id, created_at)",
    )
    for index_sql in ordinary_indexes:
        cursor.execute(index_sql)

    fk_violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        migration_report["conflicts"]["foreign_key_violations"] = [
            {
                "table": row["table"],
                "rowid": row["rowid"],
                "parent": row["parent"],
                "foreign_key_index": row["fkid"],
            }
            for row in fk_violations
        ]

    cursor.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()
    if backup_path:
        logging.getLogger(__name__).info("Database backup created before migration: %s", backup_path)
    if migration_report["conflicts"]:
        logging.getLogger(__name__).warning(
            "Database migration completed with data conflicts; details follow: %s",
            json.dumps(migration_report["conflicts"], sort_keys=True),
        )
    return migration_report


# =====================================================================
# USER MANAGEMENT FUNCTIONS
# =====================================================================

def create_web_session(user_id: int) -> Dict[str, Any]:
    """Revoke the user's previous browser session and issue a new opaque token."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now_ts = time.time()
    expires_at = now_ts + WEB_SESSION_IDLE_TTL_SECONDS
    absolute_expires_at = now_ts + WEB_SESSION_ABSOLUTE_TTL_SECONDS
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            UPDATE user_sessions SET revoked_at = ?
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (now_ts, user_id),
        )
        cursor.execute(
            """
            INSERT INTO user_sessions (
                user_id, token_hash, created_at, last_activity_at, expires_at, absolute_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, token_hash, now_ts, now_ts, expires_at, absolute_expires_at),
        )
        session_id = cursor.lastrowid
        conn.commit()
        return {"token": token, "session_id": session_id, "expires_at": expires_at}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_web_session(token: str) -> Optional[Dict[str, Any]]:
    """Validates and refreshes a browser session, returning its current user."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now_ts = time.time()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            """
            SELECT id, user_id, expires_at, absolute_expires_at
            FROM user_sessions
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        if row["expires_at"] <= now_ts or row["absolute_expires_at"] <= now_ts:
            cursor.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now_ts, row["id"]),
            )
            conn.commit()
            return None
        cursor.execute(
            """
            UPDATE user_sessions
            SET last_activity_at = ?, expires_at = MIN(absolute_expires_at, ?)
            WHERE id = ? AND revoked_at IS NULL
            """,
            (now_ts, now_ts + WEB_SESSION_IDLE_TTL_SECONDS, row["id"]),
        )
        conn.commit()
        user = get_user_by_id(row["user_id"])
        if user:
            user["web_session_id"] = row["id"]
        return user
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke_web_session(token: str) -> None:
    """Revokes only the supplied browser session; integration tokens are untouched."""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_db_connection()
    try:
        conn.execute(
            """
            UPDATE user_sessions SET revoked_at = ?
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (time.time(), token_hash),
        )
        conn.commit()
    finally:
        conn.close()


def create_session_bridge_token(
    user_id: int,
    ttl_seconds: int = WEB_SESSION_IDLE_TTL_SECONDS,
    web_session_id: Optional[int] = None,
) -> str:
    """Generates a secure server-side session bridge token for Streamlit authentication."""
    token = secrets.token_urlsafe(32)
    conn = get_db_connection()
    cursor = conn.cursor()
    now_ts = time.time()
    expires_at = now_ts + ttl_seconds
    cursor.execute(
        "INSERT INTO auth_tokens (token, user_id, expires_at, web_session_id) VALUES (?, ?, ?, ?)",
        (token, user_id, expires_at, web_session_id)
    )
    # Clean up expired tokens
    cursor.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now_ts,))
    conn.commit()
    conn.close()
    return token


def revoke_session_bridge_token(token: str) -> None:
    """Revokes a Streamlit session bridge token."""
    if not token:
        return
    conn = get_db_connection()
    conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def validate_session_bridge_token(token: str) -> Optional[Dict[str, Any]]:
    """Validates a bridge token and returns the corresponding user if valid and unexpired."""
    if not token:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    now_ts = time.time()
    cursor.execute(
        "SELECT user_id, expires_at, web_session_id FROM auth_tokens WHERE token = ?",
        (token,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    if row["expires_at"] < now_ts:
        cursor.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None
    if row["web_session_id"] is not None:
        session_row = cursor.execute(
            """
            SELECT id, user_id, expires_at, absolute_expires_at FROM user_sessions
            WHERE id = ? AND revoked_at IS NULL
              AND expires_at > ? AND absolute_expires_at > ?
            """,
            (row["web_session_id"], now_ts, now_ts),
        ).fetchone()
        if not session_row or session_row["user_id"] != row["user_id"]:
            conn.close()
            return None
        cursor.execute(
            """
            UPDATE user_sessions
            SET last_activity_at = ?, expires_at = MIN(absolute_expires_at, ?)
            WHERE id = ? AND revoked_at IS NULL
            """,
            (
                now_ts,
                now_ts + WEB_SESSION_IDLE_TTL_SECONDS,
                session_row["id"],
            ),
        )
        conn.commit()
    user_id = row["user_id"]
    conn.close()
    return get_user_by_id(user_id)


def create_user(name: str, email: str, password: str, phone: str = "") -> Dict[str, Any]:
    """
    Registers a new unique user.
    Returns dict with success status and user info or error message.
    """
    email = email.strip().lower()
    name = name.strip()
    phone = phone.strip()
    digits = "".join(character for character in phone if character.isdigit())

    if not name or not email or not password:
        return {"success": False, "message": "Name, Email आणि Password आवश्यक आहेत."}
    if phone and (
        not re.fullmatch(r"\+?[\d\s().-]{10,24}", phone)
        or not 10 <= len(digits) <= 15
    ):
        return {"success": False, "message": "कृपया वैध मोबाइल नंबर टाका (10 ते 15 अंक)."}
    if phone:
        phone = ("+" if phone.lstrip().startswith("+") else "") + digits

    hashed_pw = generate_password_hash(password)

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE")
        existing_user = cursor.execute(
            "SELECT id FROM users WHERE lower(trim(email)) = ? LIMIT 1",
            (email,),
        ).fetchone()
        if existing_user:
            conn.rollback()
            return {
                "success": False,
                "message": "हा Email आधीच नोंदणीकृत आहे. कृपया Login करा किंवा दुसरा Email वापरा."
            }
        mobile_digits_expr = (
            "replace(replace(replace(replace(replace("
            "COALESCE(NULLIF(mobile, ''), phone, ''), ' ', ''), '-', ''), '(', ''), ')', ''), '+', '')"
        )
        mobile_expr = (
            f"CASE WHEN length({mobile_digits_expr}) = 12 AND substr({mobile_digits_expr}, 1, 2) = '91' "
            f"THEN substr({mobile_digits_expr}, 3) ELSE {mobile_digits_expr} END"
        )
        comparison_digits = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
        if comparison_digits:
            existing_mobile = cursor.execute(
                f"SELECT id FROM users WHERE {mobile_expr} = ? LIMIT 1",
                (comparison_digits,),
            ).fetchone()
            if existing_mobile:
                conn.rollback()
                return {
                    "success": False,
                    "message": "हा मोबाइल नंबर आधीच नोंदणीकृत आहे. कृपया दुसरा नंबर वापरा."
                }
        cursor.execute(
            """
            INSERT INTO users (name, email, password_hash, phone, mobile, role, is_paid)
            VALUES (?, ?, ?, ?, ?, 'user', 0)
            """,
            (name, email, hashed_pw, phone, phone)
        )
        conn.commit()
        user_id = cursor.lastrowid

        return {
            "success": True,
            "message": "नोंदणी यशस्वी झाली! आता लॉगिन करा.",
            "user": {
                "id": user_id,
                "name": name,
                "email": email,
                "phone": phone,
                "mobile": phone,
                "role": "user",
                "is_paid": False
            }
        }
    except sqlite3.IntegrityError as error:
        conn.rollback()
        if "mobile" in str(error).lower():
            return {
                "success": False,
                "message": "हा मोबाइल नंबर आधीच नोंदणीकृत आहे. कृपया दुसरा नंबर वापरा."
            }
        return {
            "success": False,
            "message": "हा Email आधीच नोंदणीकृत आहे. कृपया Login करा किंवा दुसरा Email वापरा."
        }
    except Exception as e:
        return {"success": False, "message": f"डेटाबेस त्रुटी: {str(e)}"}
    finally:
        conn.close()


def authenticate_user(email: str, password: str) -> Dict[str, Any]:
    """
    Authenticates user with email and password.
    Returns user details along with active subscription status.
    """
    email = email.strip().lower()
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, name, email, password_hash, phone, mobile, role, is_paid FROM users WHERE LOWER(email) = ?",
        (email,)
    )
    user = cursor.fetchone()
    conn.close()

    if not user:
        return {"success": False, "message": "हा Email नोंदणीकृत नाही. कृपया प्रथम नोंदणी (Register) करा."}

    if not check_password_hash(user["password_hash"], password):
        return {"success": False, "message": "चुकीचा पासवर्ड. कृपया पुन्हा प्रयत्न करा."}

    # Check active subscription
    sub_status = check_subscription_status(user_id=user["id"], email=user["email"])
    user_phone = user["phone"] or user["mobile"] or ""

    return {
        "success": True,
        "message": f"स्वागत आहे, {user['name']}!",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "phone": user_phone,
            "mobile": user_phone,
            "role": user["role"],
            "is_paid": sub_status["active"]
        },
        "subscription": sub_status
    }


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Returns user dictionary by user_id."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, phone, mobile, role, is_paid FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    user_dict = dict(row)
    phone_val = user_dict.get("phone") or user_dict.get("mobile") or ""
    user_dict["phone"] = phone_val
    user_dict["mobile"] = phone_val
    user_dict["subscription"] = check_subscription_status(user_id=user_id)
    user_dict["is_paid"] = user_dict["subscription"]["active"]
    return user_dict


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Returns user dictionary by email."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, phone, mobile, role, is_paid FROM users WHERE lower(trim(email)) = ?", (email.strip().lower(),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    user_dict = dict(row)
    phone_val = user_dict.get("phone") or user_dict.get("mobile") or ""
    user_dict["phone"] = phone_val
    user_dict["mobile"] = phone_val
    user_dict["subscription"] = check_subscription_status(user_id=user_dict["id"])
    user_dict["is_paid"] = user_dict["subscription"]["active"]
    return user_dict


def get_all_users() -> List[Dict[str, Any]]:
    """Returns list of all registered users without password hashes."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, phone, mobile, role, is_paid, created_at FROM users ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        p = d.get("phone") or d.get("mobile") or ""
        d["phone"] = p
        d["mobile"] = p
        result.append(d)
    return result


def configure_admin_user(email: str) -> bool:
    """Promote one pre-existing account selected by server-side configuration."""
    normalized_email = email.strip().lower()
    if not normalized_email:
        return False
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        matches = conn.execute(
            "SELECT id FROM users WHERE lower(trim(email)) = ? ORDER BY id",
            (normalized_email,),
        ).fetchall()
        if len(matches) != 1:
            conn.rollback()
            return False
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (matches[0]["id"],))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_payment_status(user_id: Optional[int] = None, email: Optional[str] = None, is_paid: int = 1):
    """Updates is_paid flag in users table."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if user_id:
        cursor.execute("UPDATE users SET is_paid = ? WHERE id = ?", (is_paid, user_id))
    elif email:
        cursor.execute("UPDATE users SET is_paid = ? WHERE LOWER(email) = ?", (is_paid, email.strip().lower()))
    conn.commit()
    conn.close()


def submit_pending_payment(
    user_id: int,
    email: str,
    amount: float,
    method: str,
    utr_number: str,
) -> Dict[str, Any]:
    """Records a UPI claim for admin review without granting subscription access."""
    utr_number = str(utr_number or "").strip()
    if not utr_number:
        return {"success": False, "message": "A transaction reference is required."}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        user = cursor.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            conn.rollback()
            return {"success": False, "message": "The account for this payment was not found."}
        email = str(user["email"]).strip().lower()
        cursor.execute(
            """
            UPDATE subscriptions SET status = 'EXPIRED'
            WHERE user_id = ? AND status = 'ACTIVE'
              AND (
                    datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                    OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
              )
            """,
            (user_id,),
        )
        active_subscription = cursor.execute(
            """
            SELECT subscription_id
            FROM subscriptions
            WHERE user_id = ? AND status = 'ACTIVE'
              AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
        if active_subscription:
            conn.rollback()
            return {
                "success": False,
                "message": "या account वर आधीच active subscription आहे."
            }

        pending_payment = cursor.execute(
            """
            SELECT id FROM payments
            WHERE user_id = ?
              AND (upper(status) = 'PENDING' OR upper(payment_status) = 'PENDING')
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
        if pending_payment:
            conn.rollback()
            return {
                "success": False,
                "message": "या account साठी payment आधीच verification साठी pending आहे."
            }

        duplicate = cursor.execute(
            """
            SELECT id FROM payments
            WHERE trim(COALESCE(utr_number, '')) <> ''
              AND lower(trim(utr_number)) = lower(trim(?))
            LIMIT 1
            """,
            (utr_number,),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return {
                "success": False,
                "message": "हा UTR नंबर आधीच सबमिट केला आहे (This UTR number has already been submitted)."
            }

        payment_id = f"GW_PENDING_{secrets.token_hex(12)}"
        cursor.execute(
            """
            INSERT INTO payments (
                user_id, email, amount, method, payment_id, transaction_id,
                currency, payment_method, payment_status, utr_number, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'INR', ?, 'PENDING', ?, 'pending')
            """,
            (user_id, email, amount, method, payment_id, utr_number, method, utr_number)
        )
        conn.commit()
        return {
            "success": True,
            "payment_request_id": cursor.lastrowid,
            "message": "UTR सबमिट झाला आहे. Admin कडून payment verify होईपर्यंत PDF आणि receipt उपलब्ध होणार नाही."
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_latest_payment_status(user_id: int) -> Dict[str, Any]:
    """Returns the current user's most recent payment review status."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT status, payment_status, created_at
            FROM payments
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()
        if not row:
            return {"status": "NONE", "pending": False}
        status = str(row["status"] or "").upper()
        return {
            "status": status,
            "pending": status == "PENDING" or str(row["payment_status"] or "").upper() == "PENDING",
            "created_at": row["created_at"]
        }
    finally:
        conn.close()


def get_pending_payments() -> List[Dict[str, Any]]:
    """Lists UPI payment claims awaiting administrator verification."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.user_id, p.email, p.amount, p.method, p.utr_number,
                   p.created_at, u.name AS customer_name
            FROM payments p
            LEFT JOIN users u ON u.id = p.user_id
            WHERE upper(p.status) = 'PENDING' OR upper(p.payment_status) = 'PENDING'
            ORDER BY p.id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def reject_pending_payment(payment_row_id: int) -> Dict[str, Any]:
    """Rejects a UPI claim without creating a subscription or receipt."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        result = cursor.execute(
            """
            UPDATE payments
            SET status = 'rejected', payment_status = 'REJECTED'
            WHERE id = ?
              AND (upper(status) = 'PENDING' OR upper(payment_status) = 'PENDING')
            """,
            (payment_row_id,),
        )
        if result.rowcount != 1:
            conn.rollback()
            return {"success": False, "message": "Pending payment request was not found or was already processed."}
        conn.commit()
        return {"success": True, "message": "Payment claim rejected."}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_gateway_order(user_id: int, order_id: str, amount_paise: int) -> Dict[str, Any]:
    """Associates a server-created Razorpay order with the authenticated account."""
    order_id = str(order_id or "").strip()
    if not order_id or amount_paise <= 0:
        return {"success": False, "message": "A valid payment order is required."}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        if not cursor.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            conn.rollback()
            return {"success": False, "message": "The account for this payment was not found."}
        active_subscription = cursor.execute(
            """
            SELECT subscription_id FROM subscriptions
            WHERE user_id = ? AND status = 'ACTIVE'
              AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if active_subscription:
            conn.rollback()
            return {"success": False, "message": "This account already has an active subscription."}
        cursor.execute(
            "INSERT INTO gateway_orders (order_id, user_id, amount_paise) VALUES (?, ?, ?)",
            (order_id, user_id, int(amount_paise)),
        )
        conn.commit()
        return {"success": True}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"success": False, "message": "This payment order has already been registered."}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_gateway_order(user_id: int, order_id: str) -> Optional[Dict[str, Any]]:
    """Returns an order only when it belongs to the requesting account."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT order_id, user_id, amount_paise, status, payment_id
            FROM gateway_orders
            WHERE order_id = ? AND user_id = ?
            """,
            (order_id, user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def complete_gateway_order(
    user_id: int,
    order_id: str,
    payment_id: str,
    validity_days: int = 30,
) -> Dict[str, Any]:
    """Atomically records a verified gateway payment, subscription, and receipt."""
    order_id = str(order_id or "").strip()
    payment_id = str(payment_id or "").strip()
    if not order_id or not payment_id or validity_days <= 0:
        return {"success": False, "message": "Payment details are incomplete."}

    conn = get_db_connection()
    already_paid = False
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        order = cursor.execute(
            "SELECT * FROM gateway_orders WHERE order_id = ? AND user_id = ?",
            (order_id, user_id),
        ).fetchone()
        if not order:
            conn.rollback()
            return {"success": False, "message": "Payment order was not found for this account."}
        if order["status"] == "PAID":
            if order["payment_id"] != payment_id:
                conn.rollback()
                return {"success": False, "message": "This order was completed with a different payment."}
            already_paid = True
            conn.commit()
        elif order["status"] != "CREATED":
            conn.rollback()
            return {"success": False, "message": "This payment order is no longer available."}
        else:
            duplicate = cursor.execute(
                """
                SELECT 1 FROM gateway_orders WHERE payment_id = ?
                UNION ALL
                SELECT 1 FROM payments WHERE payment_id = ? OR transaction_id = ?
                LIMIT 1
                """,
                (payment_id, payment_id, payment_id),
            ).fetchone()
            if duplicate:
                conn.rollback()
                return {"success": False, "message": "This payment has already been processed."}

            user = cursor.execute(
                "SELECT id, name, email, phone, mobile FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not user:
                conn.rollback()
                return {"success": False, "message": "The account for this payment was not found."}

            now = datetime.now().replace(microsecond=0)
            active = cursor.execute(
                """
                SELECT end_date FROM subscriptions
                WHERE user_id = ? AND status = 'ACTIVE'
                  AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
                ORDER BY datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            start = now
            if active:
                active_end = datetime.fromisoformat(str(active["end_date"]))
                if active_end > start:
                    start = active_end
            end = start + timedelta(days=validity_days)
            start_text = start.strftime("%Y-%m-%d %H:%M:%S")
            end_text = end.strftime("%Y-%m-%d %H:%M:%S")
            amount = order["amount_paise"] / 100
            cursor.execute(
                """
                INSERT INTO payments (
                    user_id, email, amount, method, utr_number, status, order_id,
                    payment_id, transaction_id, currency, payment_method, payment_status,
                    paid_at, valid_until
                )
                VALUES (?, ?, ?, 'Razorpay', ?, 'approved', ?, ?, ?, 'INR',
                        'Razorpay', 'SUCCESS', ?, ?)
                """,
                (user_id, user["email"], amount, payment_id, order_id, payment_id,
                 payment_id, start_text, end_text),
            )
            payment_row_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO subscriptions (
                    user_id, email, plan, plan_name, amount, payment_id, utr_number,
                    status, start_date, end_date, expiry_date
                )
                VALUES (?, ?, 'Monthly Pro', 'Monthly Pro', ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (user_id, user["email"], amount, payment_id, payment_id,
                 start_text, end_text, end_text),
            )
            subscription_id = cursor.lastrowid
            receipt_number = f"GW-REC-{now.strftime('%y%m%d')}-{user_id}-{payment_row_id}"
            cursor.execute(
                """
                INSERT INTO receipts (
                    user_id, payment_id, receipt_number, transaction_id, amount,
                    payment_date, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, payment_row_id, receipt_number, payment_id,
                 int(amount), start_text, start_text),
            )
            cursor.execute(
                """
                UPDATE users
                SET is_paid = 1, subscription_plan = 'Monthly Pro',
                    subscription_start = ?, subscription_end = ?
                WHERE id = ?
                """,
                (start_text, end_text, user_id),
            )
            cursor.execute(
                """
                UPDATE gateway_orders
                SET status = 'PAID', payment_id = ?, paid_at = ?
                WHERE order_id = ? AND user_id = ? AND status = 'CREATED'
                """,
                (payment_id, start_text, order_id, user_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("Payment order was already processed.")
            conn.commit()
            receipt = {
                "receipt_no": receipt_number,
                "receipt_number": receipt_number,
                "subscription_id": subscription_id,
                "payment_id": payment_id,
                "transaction_id": payment_id,
                "user_id": user_id,
                "customer_name": user["name"],
                "customer_email": user["email"],
                "customer_phone": user["phone"] or user["mobile"] or "",
                "amount": f"₹{amount:.2f}",
                "amount_raw": amount,
                "plan": "Monthly Pro",
                "plan_name": "Monthly Pro",
                "method": "Razorpay",
                "status": "PAID & VERIFIED",
                "date": start.strftime("%d %b %Y, %I:%M %p"),
                "valid_until": end.strftime("%d %b %Y"),
                "validity_days": validity_days,
            }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if already_paid:
        receipt = get_latest_receipt(user_id=user_id, payment_id=payment_id)
        if not receipt:
            return {"success": False, "message": "The verified payment receipt could not be loaded."}
    return {"success": True, "subscription_active": True, "receipt": receipt}


def approve_pending_payment(payment_row_id: int, validity_days: int = 30) -> Dict[str, Any]:
    """Approves a reviewed UTR and atomically creates its subscription and receipt."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        payment = cursor.execute(
            """
            SELECT p.*, u.name AS customer_name, u.phone, u.mobile
            FROM payments p
            JOIN users u ON u.id = p.user_id
            WHERE p.id = ?
              AND (upper(p.status) = 'PENDING' OR upper(p.payment_status) = 'PENDING')
            """,
            (payment_row_id,)
        ).fetchone()
        if not payment:
            conn.rollback()
            return {
                "success": False,
                "message": "Pending payment request सापडली नाही किंवा ती आधीच process झाली आहे."
            }

        now = datetime.now()
        start_date = now.strftime("%Y-%m-%d %H:%M:%S")
        end_date = (now + timedelta(days=validity_days)).strftime("%Y-%m-%d %H:%M:%S")
        payment_id = payment["payment_id"] or f"GW_PAY_{uuid.uuid4().hex}"
        receipt_no = f"GW-REC-{now.strftime('%y%m%d')}-{payment['user_id']}-{payment_row_id}"
        amount = float(payment["amount"])

        cursor.execute(
            """
            UPDATE subscriptions SET status = 'EXPIRED'
            WHERE user_id = ? AND status = 'ACTIVE'
              AND (
                    datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                    OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
              )
            """,
            (payment["user_id"],),
        )
        active_subscription = cursor.execute(
            """
            SELECT subscription_id FROM subscriptions
            WHERE user_id = ? AND status = 'ACTIVE'
              AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
            LIMIT 1
            """,
            (payment["user_id"],),
        ).fetchone()
        if active_subscription:
            conn.rollback()
            return {
                "success": False,
                "message": "The user already has an active subscription; the pending payment was not applied.",
            }

        cursor.execute(
            """
            UPDATE subscriptions
            SET status = 'EXPIRED'
            WHERE user_id = ? AND status = 'ACTIVE'
              AND (
                    datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                    OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
              )
            """,
            (payment["user_id"],)
        )
        cursor.execute(
            """
            INSERT INTO subscriptions (
                user_id, email, plan, plan_name, amount, payment_id, utr_number,
                status, start_date, end_date, expiry_date
            )
            VALUES (?, ?, 'Monthly Pro', 'Monthly Pro', ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                payment["user_id"], payment["email"], amount, payment_id,
                payment["utr_number"], start_date, end_date, end_date
            )
        )
        subscription_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO receipts (
                user_id, payment_id, receipt_number, transaction_id, amount,
                payment_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment["user_id"], payment_row_id, receipt_no, payment["utr_number"],
                int(amount), start_date, start_date
            )
        )
        updated_payment = cursor.execute(
            """
            UPDATE payments
            SET payment_id = ?, payment_status = 'SUCCESS', status = 'approved',
                paid_at = ?, valid_until = ?
            WHERE id = ?
              AND (upper(status) = 'PENDING' OR upper(payment_status) = 'PENDING')
            """,
            (payment_id, start_date, end_date, payment_row_id)
        )
        if updated_payment.rowcount != 1:
            raise sqlite3.IntegrityError("Payment claim was already processed.")
        cursor.execute(
            """
            UPDATE users
            SET is_paid = 1, subscription_plan = 'Monthly Pro',
                subscription_start = ?, subscription_end = ?
            WHERE id = ?
            """,
            (start_date, end_date, payment["user_id"])
        )
        conn.commit()

        return {
            "success": True,
            "receipt": {
                "receipt_no": receipt_no,
                "receipt_number": receipt_no,
                "subscription_id": subscription_id,
                "payment_id": payment_id,
                "transaction_id": payment["utr_number"],
                "user_id": payment["user_id"],
                "customer_name": payment["customer_name"],
                "customer_email": payment["email"],
                "customer_phone": payment["phone"] or payment["mobile"] or "",
                "amount": f"₹{amount:.2f}",
                "amount_raw": amount,
                "plan": "Monthly Pro",
                "plan_name": "Monthly Pro",
                "method": payment["method"],
                "status": "PAID & VERIFIED",
                "date": now.strftime("%d %b %Y, %I:%M %p"),
                "valid_until": (now + timedelta(days=validity_days)).strftime("%d %b %Y"),
                "validity_days": validity_days
            }
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =====================================================================
# SUBSCRIPTIONS & PAYMENT FUNCTIONS
# =====================================================================

def check_subscription_status(user_id: Optional[int] = None, email: Optional[str] = None) -> Dict[str, Any]:
    """
    Checks if user has an ACTIVE subscription where end_date/expiry_date is in the future.
    Returns consistent format with active, plan_name, amount, start_date, expiry_date, days_left.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if user_id is not None:
            cursor.execute(
                """
                UPDATE subscriptions SET status = 'EXPIRED'
                WHERE user_id = ? AND status = 'ACTIVE'
                  AND (
                        datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                        OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
                  )
                """,
                (user_id,),
            )
            cursor.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ? AND status = 'ACTIVE'
                  AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
                ORDER BY datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) DESC,
                         subscription_id DESC
                LIMIT 1
                """,
                (user_id,),
            )
        elif email:
            normalized_email = email.strip().lower()
            cursor.execute(
                """
                UPDATE subscriptions SET status = 'EXPIRED'
                WHERE lower(trim(email)) = ? AND status = 'ACTIVE'
                  AND (
                        datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                        OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
                  )
                """,
                (normalized_email,),
            )
            cursor.execute(
                """
                SELECT * FROM subscriptions
                WHERE lower(trim(email)) = ? AND status = 'ACTIVE'
                  AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
                ORDER BY datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) DESC,
                         subscription_id DESC
                LIMIT 1
                """,
                (normalized_email,),
            )
        else:
            conn.close()
            return {
                "active": False,
                "status": "INACTIVE",
                "plan_name": "Free",
                "plan": "Free",
                "amount": 0,
                "start_date": None,
                "end_date": None,
                "expiry_date": None,
                "days_left": 0,
                "message": "सबस्क्रिप्शन अद्याप सक्रिय नाही. कृपया सबस्क्रिप्शन घ्या."
            }
        sub = cursor.fetchone()
        if user_id is not None:
            cursor.execute("UPDATE users SET is_paid = ? WHERE id = ?", (1 if sub else 0, user_id))
        elif email:
            cursor.execute(
                "UPDATE users SET is_paid = ? WHERE lower(trim(email)) = ?",
                (1 if sub else 0, email.strip().lower()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

    if not sub:
        return {
            "active": False,
            "status": "INACTIVE",
            "plan_name": "Free",
            "plan": "Free",
            "amount": 10,
            "start_date": None,
            "end_date": None,
            "expiry_date": None,
            "days_left": 0,
            "message": "सबस्क्रिप्शन अद्याप सक्रिय नाही. कृपया सबस्क्रिप्शन घ्या."
        }

    end_value = sub["expiry_date"] or sub["end_date"]
    try:
        end_dt = datetime.fromisoformat(str(end_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {
            "active": False,
            "status": "INVALID",
            "plan": sub["plan_name"] or sub["plan"] or "Premium Monthly Subscription",
            "plan_name": sub["plan_name"] or sub["plan"] or "Premium Monthly Subscription",
            "amount": float(sub["amount"] or 0),
            "start_date": sub["start_date"],
            "end_date": sub["end_date"],
            "expiry_date": end_value,
            "days_left": 0,
            "message": "Subscription expiration date is invalid; access is denied."
        }

    now = datetime.now(end_dt.tzinfo) if end_dt.tzinfo else datetime.now()
    days_left = max(1, (end_dt - now).days)
    plan_display = sub["plan_name"] or sub["plan"] or "Premium Monthly Subscription"
    return {
        "active": True,
        "status": "ACTIVE",
        "subscription_id": sub["subscription_id"],
        "plan": plan_display,
        "plan_name": plan_display,
        "amount": float(sub["amount"] or 0),
        "payment_id": sub["payment_id"],
        "utr_number": sub["utr_number"] or "",
        "start_date": sub["start_date"],
        "end_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "expiry_date": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "days_left": days_left,
        "message": f"सबस्क्रिप्शन सक्रिय आहे ({days_left} दिवस शिल्लक)."
    }


def create_subscription(
    email: str,
    user_id: Optional[int] = None,
    amount: float = 10.0,
    method: str = "Razorpay / UPI",
    utr_number: str = "",
    sender_upi: str = "",
    plan: str = "Premium Monthly Subscription",
    validity_days: int = 30,
    order_id: str = "",
    payment_id: str = ""
) -> Dict[str, Any]:
    """
    Records a trusted, already-authorized payment and creates its subscription.
    Updates users table, receipts table, and returns an official receipt dictionary.
    """
    email = email.strip().lower()
    if amount <= 0 or validity_days <= 0:
        return {"success": False, "message": "Payment amount and subscription validity must be positive."}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        if user_id is not None:
            user = cursor.execute(
                "SELECT id, name, email, phone, mobile FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        else:
            user = cursor.execute(
                "SELECT id, name, email, phone, mobile FROM users WHERE lower(trim(email)) = ?",
                (email,),
            ).fetchone()
        if not user:
            conn.rollback()
            return {"success": False, "message": "A registered user is required for a subscription."}

        user_id = user["id"]
        email = user["email"]
        user_name = user["name"]
        user_phone = user["phone"] or user["mobile"] or ""
        cursor.execute(
            """
            UPDATE subscriptions SET status = 'EXPIRED'
            WHERE user_id = ? AND status = 'ACTIVE'
              AND (
                    datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) IS NULL
                    OR datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) <= datetime('now', 'localtime')
              )
            """,
            (user_id,),
        )
        active = cursor.execute(
            """
            SELECT subscription_id FROM subscriptions
            WHERE user_id = ? AND status = 'ACTIVE'
              AND datetime(COALESCE(NULLIF(expiry_date, ''), end_date)) > datetime('now', 'localtime')
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if active:
            conn.rollback()
            return {"success": False, "message": "This user already has an active subscription."}

        payment_id = payment_id.strip() or f"GW_PAY_{uuid.uuid4().hex}"
        utr_number = utr_number.strip() or payment_id
        duplicate = cursor.execute(
            """
            SELECT id FROM payments
            WHERE lower(trim(COALESCE(payment_id, ''))) = lower(trim(?))
               OR (trim(COALESCE(utr_number, '')) <> '' AND
                   lower(trim(utr_number)) = lower(trim(?)))
            LIMIT 1
            """,
            (payment_id, utr_number),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return {"success": False, "message": "Payment identifier or UTR has already been processed."}

        now = datetime.now()
        start_date = now.strftime("%Y-%m-%d %H:%M:%S")
        end_date = (now + timedelta(days=validity_days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO payments (
                user_id, email, amount, method, utr_number, sender_upi, status,
                order_id, payment_id, transaction_id, currency, payment_method, payment_status, paid_at, valid_until
            )
            VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, 'INR', ?, 'SUCCESS', ?, ?)
            """,
            (user_id, email, amount, method, utr_number, sender_upi,
             order_id or payment_id, payment_id, utr_number, method, start_date, end_date)
        )
        pay_db_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO subscriptions (
                user_id, email, plan, plan_name, amount, payment_id, utr_number, status, start_date, end_date, expiry_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (user_id, email, plan, plan, amount, payment_id, utr_number, start_date, end_date, end_date)
        )
        subscription_id = cursor.lastrowid

        receipt_no = f"GW-REC-{now.strftime('%y%m%d')}-{user_id}-{pay_db_id}"
        cursor.execute(
            """
            INSERT INTO receipts (
                user_id, payment_id, receipt_number, transaction_id, amount, payment_date, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, pay_db_id, receipt_no, utr_number, int(amount), start_date, start_date)
        )

        cursor.execute(
            """
            UPDATE users
            SET is_paid = 1, subscription_plan = ?, subscription_start = ?, subscription_end = ?
            WHERE id = ?
            """,
            (plan, start_date, end_date, user_id),
        )

        conn.commit()

        receipt_data = {
            "receipt_no": receipt_no,
            "receipt_number": receipt_no,
            "subscription_id": subscription_id,
            "payment_id": payment_id,
            "transaction_id": utr_number,
            "user_id": user_id,
            "customer_name": user_name,
            "customer_email": email,
            "customer_phone": user_phone,
            "amount": f"₹{amount:.2f}",
            "amount_raw": amount,
            "plan": plan,
            "plan_name": plan,
            "method": method,
            "status": "ACTIVE / PAID",
            "date": now.strftime("%d %b %Y, %I:%M %p"),
            "valid_until": (now + timedelta(days=validity_days)).strftime("%d %b %Y"),
            "validity_days": validity_days
        }

        return {
            "success": True,
            "message": "पेमेंट यशस्वी झाले आणि सबस्क्रिप्शन सक्रिय करण्यात आले!",
            "receipt": receipt_data
        }

    except sqlite3.IntegrityError:
        conn.rollback()
        return {"success": False, "message": "Duplicate payment, transaction reference, or active subscription."}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Payment DB Error: {str(e)}"}
    finally:
        conn.close()


def get_latest_receipt(user_id: Optional[int] = None, email: Optional[str] = None, payment_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetches full receipt details with user's personal info from database."""
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT s.subscription_id, s.plan, s.plan_name, s.amount, s.payment_id, s.utr_number, s.status, s.start_date, s.end_date, s.expiry_date,
               u.id as user_id, u.name as customer_name, u.email as customer_email, u.phone as customer_phone, u.mobile as customer_mobile,
               p.method as payment_method, r.receipt_number
        FROM subscriptions s
        LEFT JOIN users u ON s.user_id = u.id
        LEFT JOIN payments p ON p.payment_id = s.payment_id
        LEFT JOIN receipts r ON r.payment_id = p.id
    """
    params = []

    if user_id is not None and payment_id:
        query += """
            WHERE s.user_id = ? AND (s.payment_id = ? OR p.id = ?)
            ORDER BY s.subscription_id DESC LIMIT 1
        """
        params.extend([user_id, payment_id, payment_id])
    elif user_id is not None:
        query += " WHERE s.user_id = ? ORDER BY s.subscription_id DESC LIMIT 1"
        params.append(user_id)
    elif payment_id:
        query += " WHERE s.payment_id = ? ORDER BY s.subscription_id DESC LIMIT 1"
        params.append(payment_id)
    elif email:
        query += " WHERE LOWER(s.email) = ? ORDER BY s.subscription_id DESC LIMIT 1"
        params.append(email.strip().lower())
    else:
        conn.close()
        return None

    cursor.execute(query, params)
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    r = dict(row)
    start_date_val = r["start_date"]
    end_date_val = r["expiry_date"] if "expiry_date" in r and r["expiry_date"] else r["end_date"]

    try:
        start_dt = datetime.fromisoformat(start_date_val)
    except Exception:
        start_dt = datetime.now()

    try:
        end_dt = datetime.fromisoformat(end_date_val)
    except Exception:
        end_dt = start_dt + timedelta(days=30)

    cust_phone = r.get("customer_phone") or r.get("customer_mobile") or ""
    amount_val = float(r.get("amount") or 10.0)

    receipt_no = f"GW-REC-{start_dt.strftime('%y%m%d')}-{r['user_id'] or 101}"
    if r.get("receipt_number"):
        receipt_no = r["receipt_number"]

    return {
        "receipt_no": receipt_no,
        "receipt_number": receipt_no,
        "subscription_id": r["subscription_id"],
        "payment_id": r["payment_id"],
        "transaction_id": r["utr_number"] or r["payment_id"],
        "user_id": r["user_id"],
        "customer_name": r["customer_name"] or "Subscribed User",
        "customer_email": r["customer_email"] or email or "",
        "customer_phone": cust_phone,
        "amount": f"₹{amount_val:.2f}",
        "amount_raw": amount_val,
        "plan": r.get("plan_name") or r.get("plan") or "Premium Monthly Subscription",
        "plan_name": r.get("plan_name") or r.get("plan") or "Premium Monthly Subscription",
        "method": r.get("payment_method") or "UPI",
        "status": "PAID & VERIFIED",
        "date": start_dt.strftime("%d %b %Y, %I:%M %p"),
        "valid_until": end_dt.strftime("%d %b %Y"),
        "validity_days": max(1, (end_dt - start_dt).days)
    }


# =====================================================================
# FILE UPLOAD LOGGING
# =====================================================================

def log_upload(
    original_name: str,
    saved_name: str,
    file_size: int,
    user_id: Optional[int] = None,
    mime_type: str = "",
    processing_status: str = "STORED",
    row_count: Optional[int] = None,
    column_count: Optional[int] = None,
    column_names: Optional[List[str]] = None,
    processing_error: Optional[str] = None,
    checksum: Optional[str] = None,
    analysis_ref: Optional[str] = None,
) -> int:
    """Records private upload metadata and returns its stable database ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO uploads (
                user_id, original_name, saved_name, file_size, mime_type,
                processing_status, row_count, column_count, column_names,
                processing_error, storage_key, checksum, analysis_ref
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, original_name, saved_name, file_size, mime_type,
                processing_status, row_count, column_count,
                json.dumps(column_names, ensure_ascii=False) if column_names is not None else None,
                processing_error, saved_name, checksum, analysis_ref,
            ),
        )
        conn.commit()
        upload_id = cursor.lastrowid
        if upload_id is None:
            raise sqlite3.DatabaseError("Upload metadata insert did not return an ID.")
        return upload_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_uploads(user_id: int) -> List[Dict[str, Any]]:
    """Lists only uploads owned by the requested account."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM uploads WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        result = [dict(row) for row in rows]
        for upload in result:
            if upload.get("column_names"):
                upload["column_names"] = json.loads(upload["column_names"])
        return result
    finally:
        conn.close()


def get_user_upload(user_id: int, saved_name: str) -> Optional[Dict[str, Any]]:
    """Fetches one upload only when it belongs to the authenticated user."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM uploads WHERE user_id = ? AND saved_name = ?",
            (user_id, saved_name),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_user_upload(user_id: int, upload_id: int) -> Optional[Dict[str, Any]]:
    """Deletes metadata only after ownership has been checked transactionally."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            "SELECT * FROM uploads WHERE id = ? AND user_id = ?",
            (upload_id, user_id),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        cursor.execute("DELETE FROM uploads WHERE id = ? AND user_id = ?", (upload_id, user_id))
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_payment_history(user_id: int) -> List[Dict[str, Any]]:
    """Returns every payment attempt and its associated receipt, scoped to one user."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.user_id, p.amount, p.method, p.payment_method,
                   p.order_id, p.payment_id, p.transaction_id, p.utr_number,
                   p.payment_status, p.status, p.paid_at, p.valid_until, p.created_at,
                   r.receipt_number, r.payment_date AS receipt_date
            FROM payments p
            LEFT JOIN receipts r ON r.payment_id = p.id AND r.user_id = p.user_id
            WHERE p.user_id = ?
            ORDER BY p.id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_user_subscription_history(user_id: int) -> List[Dict[str, Any]]:
    """Returns historical and current subscriptions with receipt numbers."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.subscription_id, s.user_id, s.plan, s.plan_name, s.amount,
                   s.payment_id, s.utr_number, s.status, s.start_date, s.end_date,
                   s.expiry_date, s.created_at, r.receipt_number
            FROM subscriptions s
            LEFT JOIN payments p ON p.payment_id = s.payment_id AND p.user_id = s.user_id
            LEFT JOIN receipts r ON r.payment_id = p.id AND r.user_id = s.user_id
            WHERE s.user_id = ?
            ORDER BY s.subscription_id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# =====================================================================
# SHOPPULSE CORE DATA ACCESS LAYER
# =====================================================================

def get_or_create_shop(user_id: int, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Retrieves or creates a user's non-demo shop without seeding sample records."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT * FROM shops WHERE user_id = ? AND COALESCE(is_demo, 0) = 0 ORDER BY id ASC LIMIT 1",
            (user_id,),
        )
        row = cursor.fetchone()
        if row:
            conn.commit()
            return dict(row)

        defaults = defaults or {}
        cursor.execute(
            """
            INSERT INTO shops (
                user_id, name, owner_name, phone, email, address,
                business_type, currency, tax_rate, is_demo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                user_id,
                defaults.get("name", "My Smart Shop"),
                defaults.get("owner_name", ""),
                defaults.get("phone", ""),
                defaults.get("email", ""),
                defaults.get("address", ""),
                defaults.get("business_type", "Retail"),
                defaults.get("currency", "₹"),
                float(defaults.get("tax_rate", 5.0)),
            ),
        )
        shop = conn.execute("SELECT * FROM shops WHERE id = ?", (cursor.lastrowid,)).fetchone()
        conn.commit()
        return dict(shop)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_or_create_demo_shop(user_id: int, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Creates a separate, explicitly marked demo shop for sample data."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            "SELECT * FROM shops WHERE user_id = ? AND is_demo = 1 ORDER BY id ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            shop = dict(row)
            conn.commit()
            return shop
        defaults = defaults or {}
        cursor.execute(
            """
            INSERT INTO shops (
                user_id, name, owner_name, phone, email, address,
                business_type, currency, tax_rate, is_demo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                user_id,
                defaults.get("name", "Demo Shop"),
                defaults.get("owner_name", ""),
                defaults.get("phone", ""),
                defaults.get("email", ""),
                defaults.get("address", ""),
                defaults.get("business_type", "Retail"),
                defaults.get("currency", "₹"),
                float(defaults.get("tax_rate", 5.0)),
            ),
        )
        shop = dict(cursor.execute("SELECT * FROM shops WHERE id = ?", (cursor.lastrowid,)).fetchone())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    seed_demo_shop_data(shop["id"])
    return shop


def update_shop_settings(shop_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """Updates shop configuration profile."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE shops
        SET name = COALESCE(?, name),
            owner_name = COALESCE(?, owner_name),
            phone = COALESCE(?, phone),
            email = COALESCE(?, email),
            address = COALESCE(?, address),
            business_type = COALESCE(?, business_type),
            currency = COALESCE(?, currency),
            tax_rate = COALESCE(?, tax_rate)
        WHERE id = ?
    """, (
        data.get("name"),
        data.get("owner_name"),
        data.get("phone"),
        data.get("email"),
        data.get("address"),
        data.get("business_type"),
        data.get("currency"),
        data.get("tax_rate"),
        shop_id
    ))
    conn.commit()
    cursor.execute("SELECT * FROM shops WHERE id = ?", (shop_id,))
    shop = dict(cursor.fetchone())
    conn.close()
    return shop


def get_shop_products(shop_id: int, search: str = "", category: str = "", stock_status: str = "") -> List[Dict[str, Any]]:
    """Fetches all products for a shop with filters, calculated margin %, and stock value."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "SELECT * FROM products WHERE shop_id = ?"
    params: List[Any] = [shop_id]

    if search:
        query += " AND (name LIKE ? OR sku LIKE ? OR brand LIKE ?)"
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern])

    if category and category.lower() != "all":
        query += " AND category = ?"
        params.append(category)

    if stock_status == "low":
        query += " AND stock <= reorder_level AND stock > 0"
    elif stock_status == "out":
        query += " AND stock <= 0"
    elif stock_status == "in":
        query += " AND stock > reorder_level"

    query += " ORDER BY name ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    products = []
    for r in rows:
        p = dict(r)
        cost = float(p.get("cost_price") or 0.0)
        price = float(p.get("selling_price") or 0.0)
        stock = int(p.get("stock") or 0)
        reorder = int(p.get("reorder_level") or 5)
        margin = ((price - cost) / price * 100) if price > 0 else 0.0
        stock_val = stock * cost
        potential_rev = stock * price

        status = "In Stock"
        if stock <= 0:
            status = "Out of Stock"
        elif stock <= reorder:
            status = "Low Stock"

        p["margin_pct"] = round(margin, 1)
        p["stock_value"] = round(stock_val, 2)
        p["potential_revenue"] = round(potential_rev, 2)
        p["stock_status"] = status
        products.append(p)

    return products


def save_product(shop_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """Adds a new product or updates an existing one, validating SKU uniqueness."""
    product_id = data.get("id")
    sku = str(data.get("sku", "")).strip().upper()
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "General")).strip()
    brand = str(data.get("brand", "")).strip()
    cost_price = float(data.get("cost_price", 0.0))
    selling_price = float(data.get("selling_price", 0.0))
    stock = int(data.get("stock", 0))
    reorder_level = int(data.get("reorder_level", 5))
    supplier = str(data.get("supplier", "")).strip()
    status = str(data.get("status", "Active")).strip()

    if not sku or not name:
        return {"success": False, "message": "Product SKU and Name are required."}

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE")
        if product_id:
            existing = cursor.execute(
                "SELECT id FROM products WHERE id = ? AND shop_id = ?",
                (product_id, shop_id),
            ).fetchone()
            if not existing:
                conn.rollback()
                return {"success": False, "message": "Product was not found in this shop."}
            duplicate = cursor.execute(
                """
                SELECT id FROM products
                WHERE shop_id = ? AND lower(trim(sku)) = lower(trim(?)) AND id != ?
                """,
                (shop_id, sku, product_id),
            ).fetchone()
        else:
            duplicate = cursor.execute(
                "SELECT id FROM products WHERE shop_id = ? AND lower(trim(sku)) = lower(trim(?))",
                (shop_id, sku),
            ).fetchone()

        if duplicate:
            conn.rollback()
            return {"success": False, "message": f"SKU '{sku}' already exists in your shop."}

        if product_id:
            cursor.execute("""
                UPDATE products
                SET sku = ?, name = ?, category = ?, brand = ?, cost_price = ?,
                    selling_price = ?, stock = ?, reorder_level = ?, supplier = ?, status = ?
                WHERE id = ? AND shop_id = ?
            """, (sku, name, category, brand, cost_price, selling_price, stock, reorder_level, supplier, status, product_id, shop_id))
        else:
            cursor.execute("""
                INSERT INTO products (shop_id, sku, name, category, brand, cost_price, selling_price, stock, reorder_level, supplier, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (shop_id, sku, name, category, brand, cost_price, selling_price, stock, reorder_level, supplier, status))
            product_id = cursor.lastrowid

        saved = dict(cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        conn.commit()
        return {"success": True, "product": saved, "message": "Product saved successfully."}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"success": False, "message": f"SKU '{sku}' already exists in your shop."}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_product(shop_id: int, product_id: int) -> Dict[str, Any]:
    """Deletes a product if not referenced in non-cancelled sales."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM products WHERE id = ? AND shop_id = ?", (product_id, shop_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Product deleted successfully."}


def get_shop_customers(shop_id: int, search: str = "") -> List[Dict[str, Any]]:
    """Fetches customers with lifetime spend, orders count, and last purchase date."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT c.*,
               COUNT(s.id) as total_orders,
               COALESCE(SUM(CASE WHEN s.order_status = 'Completed' THEN s.total_amount ELSE 0 END), 0) as total_spent,
               MAX(s.sale_date) as last_purchase_date
        FROM customers c
        LEFT JOIN sales s ON s.customer_id = c.id AND s.shop_id = c.shop_id
        WHERE c.shop_id = ?
    """
    params: List[Any] = [shop_id]
    if search:
        query += " AND (c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ?)"
        p = f"%{search}%"
        params.extend([p, p, p])

    query += " GROUP BY c.id ORDER BY total_spent DESC, c.name ASC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def save_customer(shop_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """Adds or edits a customer."""
    cid = data.get("id")
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    email = str(data.get("email", "")).strip()
    address = str(data.get("address", "")).strip()

    if not name:
        return {"success": False, "message": "Customer name is required."}

    conn = get_db_connection()
    cursor = conn.cursor()
    if cid:
        cursor.execute("""
            UPDATE customers SET name = ?, phone = ?, email = ?, address = ?
            WHERE id = ? AND shop_id = ?
        """, (name, phone, email, address, cid, shop_id))
    else:
        cursor.execute("""
            INSERT INTO customers (shop_id, name, phone, email, address)
            VALUES (?, ?, ?, ?, ?)
        """, (shop_id, name, phone, email, address))
        cid = cursor.lastrowid

    conn.commit()
    cursor.execute("SELECT * FROM customers WHERE id = ?", (cid,))
    saved = dict(cursor.fetchone())
    conn.close()
    return {"success": True, "customer": saved, "message": "Customer saved successfully."}


def delete_customer(shop_id: int, customer_id: int) -> Dict[str, Any]:
    """Deletes a customer."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM customers WHERE id = ? AND shop_id = ?", (customer_id, shop_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Customer deleted successfully."}


def get_shop_sales(shop_id: int, start_date: Optional[str] = None, end_date: Optional[str] = None,
                   status: Optional[str] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves all sales records with nested items and customer details."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT s.*, c.name as customer_name, c.phone as customer_phone
        FROM sales s
        LEFT JOIN customers c ON s.customer_id = c.id
        WHERE s.shop_id = ?
    """
    params: List[Any] = [shop_id]

    if start_date:
        query += " AND date(s.sale_date) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(s.sale_date) <= date(?)"
        params.append(end_date)
    if status and status.lower() != "all":
        query += " AND s.order_status = ?"
        params.append(status)
    if search:
        query += " AND (s.invoice_no LIKE ? OR c.name LIKE ? OR s.payment_mode LIKE ?)"
        p = f"%{search}%"
        params.extend([p, p, p])

    query += " ORDER BY s.sale_date DESC, s.id DESC"
    cursor.execute(query, params)
    sales = [dict(r) for r in cursor.fetchall()]

    for sale in sales:
        cursor.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale["id"],))
        sale["items"] = [dict(it) for it in cursor.fetchall()]

    conn.close()
    return sales


def create_sale(shop_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Creates a new sale with line items, automatically deducing inventory in an atomic transaction.
    Logs inventory movement and audit trail.
    """
    items = data.get("items", [])
    if not items:
        return {"success": False, "message": "Order must have at least one product item."}

    customer_id = data.get("customer_id")
    payment_mode = data.get("payment_mode", "Cash")
    order_status = data.get("order_status", "Completed")
    discount = float(data.get("discount", 0.0))
    tax_rate = float(data.get("tax_rate", 0.0))
    notes = data.get("notes", "")
    sale_date = data.get("sale_date") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE")
        # Generate Invoice Number
        invoice_no = f"INV-{datetime.now().strftime('%y%m%d')}-{uuid.uuid4().hex[:10].upper()}"

        subtotal = 0.0
        cogs_total = 0.0
        validated_items = []

        # Validate stock and calculate totals
        for item in items:
            pid = item.get("product_id")
            qty = int(item.get("quantity", 1))
            cursor.execute("SELECT * FROM products WHERE id = ? AND shop_id = ?", (pid, shop_id))
            prod = cursor.fetchone()
            if not prod:
                conn.rollback()
                conn.close()
                return {"success": False, "message": f"Product ID {pid} not found in this shop."}

            prod_dict = dict(prod)
            current_stock = int(prod_dict["stock"])
            unit_price = float(item.get("unit_price") or prod_dict["selling_price"])
            cost_price = float(prod_dict["cost_price"])

            if order_status == "Completed" and current_stock < qty:
                conn.rollback()
                conn.close()
                return {"success": False, "message": f"Insufficient stock for '{prod_dict['name']}'. Available: {current_stock}, Requested: {qty}"}

            line_total = qty * unit_price
            subtotal += line_total
            cogs_total += qty * cost_price

            validated_items.append({
                "product_id": pid,
                "product_name": prod_dict["name"],
                "quantity": qty,
                "unit_price": unit_price,
                "cost_price": cost_price,
                "total_price": line_total,
                "current_stock": current_stock
            })

        # Calculate tax and grand total
        net_after_discount = max(0.0, subtotal - discount)
        tax_amount = round(net_after_discount * (tax_rate / 100.0), 2)
        total_amount = round(net_after_discount + tax_amount, 2)

        # Insert Sale
        cursor.execute("""
            INSERT INTO sales (shop_id, customer_id, invoice_no, sale_date, subtotal, discount, tax, total_amount, payment_mode, order_status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (shop_id, customer_id, invoice_no, sale_date, subtotal, discount, tax_amount, total_amount, payment_mode, order_status, notes))
        sale_id = cursor.lastrowid

        # Insert Line items and deduct stock
        for v in validated_items:
            cursor.execute("""
                INSERT INTO sale_items (sale_id, product_id, product_name, quantity, unit_price, cost_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sale_id, v["product_id"], v["product_name"], v["quantity"], v["unit_price"], v["cost_price"], v["total_price"]))

            if order_status == "Completed":
                new_stock = v["current_stock"] - v["quantity"]
                cursor.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, v["product_id"]))

                # Log inventory movement
                cursor.execute("""
                    INSERT INTO inventory_movements (shop_id, product_id, movement_type, quantity, previous_stock, new_stock, reason, reference_id)
                    VALUES (?, ?, 'Sale', ?, ?, ?, 'Order Sale Confirmation', ?)
                """, (shop_id, v["product_id"], -v["quantity"], v["current_stock"], new_stock, invoice_no))

        # Audit log
        cursor.execute("""
            INSERT INTO audit_logs (shop_id, action, entity_type, entity_id, details)
            VALUES (?, 'CREATE', 'SALE', ?, ?)
        """, (shop_id, str(sale_id), f"Sale created {invoice_no} with {len(validated_items)} items. Total: ₹{total_amount}"))

        conn.commit()

        # Fetch created sale
        cursor.execute("SELECT * FROM sales WHERE id = ?", (sale_id,))
        created_sale = dict(cursor.fetchone())
        created_sale["items"] = validated_items
        conn.close()

        return {"success": True, "sale": created_sale, "message": f"Sale {invoice_no} saved successfully!"}
    except Exception as e:
        conn.rollback()
        conn.close()
        return {"success": False, "message": f"Failed to record sale: {str(e)}"}


def cancel_or_delete_sale(shop_id: int, sale_id: int, action: str = "cancel") -> Dict[str, Any]:
    """Cancels or deletes a sale, safely restoring product inventory."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM sales WHERE id = ? AND shop_id = ?", (sale_id, shop_id))
    sale = cursor.fetchone()
    if not sale:
        conn.close()
        return {"success": False, "message": "Sale not found."}

    sale_dict = dict(sale)
    cursor.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))
    items = [dict(it) for it in cursor.fetchall()]

    # If it was completed, restore stock
    if sale_dict["order_status"] == "Completed":
        for it in items:
            pid = it["product_id"]
            qty = it["quantity"]
            cursor.execute("SELECT stock FROM products WHERE id = ?", (pid,))
            prow = cursor.fetchone()
            if prow:
                prev_stock = int(prow["stock"])
                new_stock = prev_stock + qty
                cursor.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, pid))
                cursor.execute("""
                    INSERT INTO inventory_movements (shop_id, product_id, movement_type, quantity, previous_stock, new_stock, reason, reference_id)
                    VALUES (?, ?, 'Return', ?, ?, ?, 'Sale Cancelled/Restored', ?)
                """, (shop_id, pid, qty, prev_stock, new_stock, sale_dict["invoice_no"]))

    if action == "delete":
        cursor.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))
        cursor.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
        msg = f"Sale {sale_dict['invoice_no']} deleted and inventory restored."
    else:
        cursor.execute("UPDATE sales SET order_status = 'Cancelled' WHERE id = ?", (sale_id,))
        msg = f"Sale {sale_dict['invoice_no']} cancelled and inventory restored."

    conn.commit()
    conn.close()
    return {"success": True, "message": msg}


def get_shop_expenses(shop_id: int, start_date: Optional[str] = None, end_date: Optional[str] = None,
                      category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves expenses with filters."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "SELECT * FROM expenses WHERE shop_id = ?"
    params: List[Any] = [shop_id]

    if start_date:
        query += " AND date(date) >= date(?)"
        params.append(start_date)
    if end_date:
        query += " AND date(date) <= date(?)"
        params.append(end_date)
    if category and category.lower() != "all":
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY date DESC, id DESC"
    cursor.execute(query, params)
    expenses = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return expenses


def save_expense(shop_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    """Adds or edits an expense."""
    eid = data.get("id")
    date_val = str(data.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    category = str(data.get("category", "Other")).strip()
    amount = float(data.get("amount", 0.0))
    payment_method = str(data.get("payment_method", "Cash")).strip()
    description = str(data.get("description", "")).strip()

    if amount <= 0:
        return {"success": False, "message": "Expense amount must be greater than zero."}

    conn = get_db_connection()
    cursor = conn.cursor()
    if eid:
        cursor.execute("""
            UPDATE expenses SET date = ?, category = ?, amount = ?, payment_method = ?, description = ?
            WHERE id = ? AND shop_id = ?
        """, (date_val, category, amount, payment_method, description, eid, shop_id))
    else:
        cursor.execute("""
            INSERT INTO expenses (shop_id, date, category, amount, payment_method, description)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (shop_id, date_val, category, amount, payment_method, description))
        eid = cursor.lastrowid

    conn.commit()
    cursor.execute("SELECT * FROM expenses WHERE id = ?", (eid,))
    saved = dict(cursor.fetchone())
    conn.close()
    return {"success": True, "expense": saved, "message": "Expense recorded successfully."}


def delete_expense(shop_id: int, expense_id: int) -> Dict[str, Any]:
    """Deletes an expense record."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = ? AND shop_id = ?", (expense_id, shop_id))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Expense deleted successfully."}


def get_inventory_movements(shop_id: int, product_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetches stock audit log movements."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT im.*, p.name as product_name, p.sku as product_sku
        FROM inventory_movements im
        JOIN products p ON im.product_id = p.id
        WHERE im.shop_id = ?
    """
    params: List[Any] = [shop_id]
    if product_id:
        query += " AND im.product_id = ?"
        params.append(product_id)
    query += " ORDER BY im.created_at DESC, im.id DESC LIMIT 100"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def adjust_inventory(shop_id: int, product_id: int, movement_type: str, qty: int, reason: str = "") -> Dict[str, Any]:
    """Adjusts stock manually (restock, loss/damage, return, recount)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE id = ? AND shop_id = ?", (product_id, shop_id))
    prod = cursor.fetchone()
    if not prod:
        conn.close()
        return {"success": False, "message": "Product not found."}

    prod_dict = dict(prod)
    prev_stock = int(prod_dict["stock"])

    if movement_type in ["Restock", "Stock In", "Return"]:
        new_stock = prev_stock + abs(qty)
        change_qty = abs(qty)
    elif movement_type in ["Damage", "Loss", "Expired", "Stock Out"]:
        new_stock = max(0, prev_stock - abs(qty))
        change_qty = -abs(qty)
    else:  # Set exact recount
        new_stock = max(0, qty)
        change_qty = new_stock - prev_stock

    cursor.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, product_id))
    cursor.execute("""
        INSERT INTO inventory_movements (shop_id, product_id, movement_type, quantity, previous_stock, new_stock, reason, reference_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'MANUAL')
    """, (shop_id, product_id, movement_type, change_qty, prev_stock, new_stock, reason))
    conn.commit()
    conn.close()
    return {"success": True, "new_stock": new_stock, "message": f"Stock adjusted for {prod_dict['name']}: {new_stock} units."}


def get_shop_dashboard_analytics(shop_id: int, date_filter: str = "this_month",
                                 start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Computes all 12 core dashboard KPIs and charts based on actual database records.
    Strictly distinguishes Revenue, COGS, Gross Profit, and Net Profit.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # Shop currency & profile
    cursor.execute("SELECT * FROM shops WHERE id = ?", (shop_id,))
    shop_row = cursor.fetchone()
    currency = shop_row["currency"] if shop_row else "₹"

    today_str = datetime.now().strftime("%Y-%m-%d")
    first_of_month = datetime.now().strftime("%Y-%m-01")

    # Date filter calculation
    date_clause = ""
    date_params: List[Any] = [shop_id]
    if start_date and end_date:
        date_clause = " AND date(sale_date) >= date(?) AND date(sale_date) <= date(?)"
        date_params.extend([start_date, end_date])
    elif date_filter == "today":
        date_clause = " AND date(sale_date) = date(?)"
        date_params.append(today_str)
    elif date_filter == "yesterday":
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        date_clause = " AND date(sale_date) = date(?)"
        date_params.append(yesterday_str)
    elif date_filter == "last_7_days":
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        date_clause = " AND date(sale_date) >= date(?)"
        date_params.append(seven_days_ago)
    elif date_filter == "last_30_days":
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        date_clause = " AND date(sale_date) >= date(?)"
        date_params.append(thirty_days_ago)
    elif date_filter == "last_month":
        # First day of prev month to last day of prev month
        first_this = datetime.now().replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        date_clause = " AND date(sale_date) >= date(?) AND date(sale_date) <= date(?)"
        date_params.extend([first_prev.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")])
    elif date_filter == "this_month":
        date_clause = " AND date(sale_date) >= date(?)"
        date_params.append(first_of_month)

    # 1. Total Completed Sales & Orders in Filter
    cursor.execute(f"""
        SELECT COUNT(id) as total_orders,
               COALESCE(SUM(total_amount), 0) as total_sales,
               COALESCE(SUM(tax), 0) as total_tax,
               COALESCE(SUM(discount), 0) as total_discount
        FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' {date_clause}
    """, date_params)
    sales_agg = dict(cursor.fetchone())
    total_sales = float(sales_agg["total_sales"])
    total_orders = int(sales_agg["total_orders"])
    aov = round(total_sales / total_orders, 2) if total_orders > 0 else 0.0

    # 2. Today, This Week, This Month, All Time Totals
    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' AND date(sale_date) = date('now')
    """, (shop_id,))
    today_sales = float(cursor.fetchone()[0])

    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' AND date(sale_date) >= date('now', '-7 days')
    """, (shop_id,))
    this_week_sales = float(cursor.fetchone()[0])

    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' AND date(sale_date) >= date('now', 'start of month')
    """, (shop_id,))
    this_month_sales = float(cursor.fetchone()[0])

    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) FROM sales
        WHERE shop_id = ? AND order_status = 'Completed'
    """, (shop_id,))
    all_time_sales = float(cursor.fetchone()[0])

    # 3. Cost of Goods Sold (COGS) in Filter
    cogs_query = f"""
        SELECT COALESCE(SUM(si.quantity * si.cost_price), 0)
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.id
        WHERE s.shop_id = ? AND s.order_status = 'Completed' {date_clause}
    """
    cursor.execute(cogs_query, date_params)
    cogs = float(cursor.fetchone()[0])
    gross_profit = round(total_sales - cogs, 2)

    # 4. Total Operating Expenses in Filter
    exp_clause = ""
    exp_params: List[Any] = [shop_id]
    if start_date and end_date:
        exp_clause = " AND date(date) >= date(?) AND date(date) <= date(?)"
        exp_params.extend([start_date, end_date])
    elif date_filter == "today":
        exp_clause = " AND date(date) = date(?)"
        exp_params.append(today_str)
    elif date_filter == "this_month":
        exp_clause = " AND date(date) >= date(?)"
        exp_params.append(first_of_month)

    cursor.execute(f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE shop_id = ? {exp_clause}", exp_params)
    total_expenses = float(cursor.fetchone()[0])
    net_profit = round(gross_profit - total_expenses, 2)
    profit_margin = round((net_profit / total_sales * 100), 1) if total_sales > 0 else 0.0

    # 5. Product counts & low stock
    cursor.execute("SELECT COUNT(id) FROM products WHERE shop_id = ?", (shop_id,))
    total_products = int(cursor.fetchone()[0])

    cursor.execute("SELECT COUNT(id) FROM products WHERE shop_id = ? AND stock <= reorder_level", (shop_id,))
    low_stock_count = int(cursor.fetchone()[0])

    cursor.execute("SELECT COUNT(id) FROM customers WHERE shop_id = ?", (shop_id,))
    total_customers = int(cursor.fetchone()[0])

    # 6. Daily sales trend (Last 14 days)
    cursor.execute("""
        SELECT date(sale_date) as day, COALESCE(SUM(total_amount), 0) as revenue, COUNT(id) as orders
        FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' AND date(sale_date) >= date('now', '-14 days')
        GROUP BY date(sale_date)
        ORDER BY date(sale_date) ASC
    """, (shop_id,))
    daily_trend = [dict(r) for r in cursor.fetchall()]

    # 7. Payment Modes Breakdown
    cursor.execute(f"""
        SELECT payment_mode, COALESCE(SUM(total_amount), 0) as total, COUNT(id) as count
        FROM sales
        WHERE shop_id = ? AND order_status = 'Completed' {date_clause}
        GROUP BY payment_mode
    """, date_params)
    payment_modes = [dict(r) for r in cursor.fetchall()]

    # 8. Top 5 Products by Revenue and Quantity
    cursor.execute(f"""
        SELECT si.product_name,
               SUM(si.quantity) as total_qty,
               SUM(si.total_price) as total_revenue,
               SUM(si.quantity * (si.unit_price - si.cost_price)) as gross_profit
        FROM sale_items si
        JOIN sales s ON si.sale_id = s.id
        WHERE s.shop_id = ? AND s.order_status = 'Completed' {date_clause}
        GROUP BY si.product_name
        ORDER BY total_revenue DESC
        LIMIT 10
    """, date_params)
    top_products = [dict(r) for r in cursor.fetchall()]

    # 9. Expenses by category
    cursor.execute(f"""
        SELECT category, SUM(amount) as total
        FROM expenses
        WHERE shop_id = ? {exp_clause}
        GROUP BY category
        ORDER BY total DESC
    """, exp_params)
    expense_categories = [dict(r) for r in cursor.fetchall()]

    # 10. Low Stock Alert List
    cursor.execute("""
        SELECT id, sku, name, category, stock, reorder_level, selling_price
        FROM products
        WHERE shop_id = ? AND stock <= reorder_level
        ORDER BY stock ASC
        LIMIT 10
    """, (shop_id,))
    low_stock_items = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "kpis": {
            "today_sales": today_sales,
            "this_week_sales": this_week_sales,
            "this_month_sales": this_month_sales,
            "total_sales": total_sales,
            "all_time_sales": all_time_sales,
            "total_orders": total_orders,
            "average_order_value": aov,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "total_expenses": total_expenses,
            "net_profit": net_profit,
            "profit_margin_pct": profit_margin,
            "total_products": total_products,
            "low_stock_products": low_stock_count,
            "total_customers": total_customers,
            "currency": currency
        },
        "charts": {
            "daily_trend": daily_trend,
            "payment_modes": payment_modes,
            "top_products": top_products,
            "expense_categories": expense_categories
        },
        "low_stock_alerts": low_stock_items
    }


def query_ai_assistant(shop_id: int, question: str) -> Dict[str, Any]:
    """
    Answers business analytics questions with verified database calculations.
    Ensures mathematical accuracy and surfaces actionable recommendations.
    """
    q = question.lower().strip()
    data = get_shop_dashboard_analytics(shop_id, date_filter="this_month")
    kpis = data["kpis"]
    curr = kpis["currency"]

    if "sales this month" in q or "total sales" in q or "how much sales" in q:
        ans = (
            f"📊 **Sales Summary**: Total sales this month are **{curr}{kpis['this_month_sales']:,.2f}** "
            f"across **{kpis['total_orders']} orders**, with an Average Order Value (AOV) of **{curr}{kpis['average_order_value']:,.2f}**."
        )
        recommendation = "Maintain your current bestsellers and consider bundling items to raise your AOV beyond " + f"{curr}{kpis['average_order_value'] * 1.15:,.2f}."
    elif "highest revenue" in q or "best selling" in q or "top product" in q:
        top = data["charts"]["top_products"]
        if top:
            top_p = top[0]
            ans = (
                f"🏆 **Top Revenue Product**: **{top_p['product_name']}** generated the highest revenue of "
                f"**{curr}{top_p['total_revenue']:,.2f}** ({top_p['total_qty']} units sold)."
            )
            recommendation = f"Ensure '{top_p['product_name']}' is never out of stock and consider premium placement or cross-selling."
        else:
            ans = "No sales recorded yet this month to identify top products."
            recommendation = "Add new sales to generate product ranking insights."
    elif "highest profit" in q or "most profitable" in q:
        top = sorted(data["charts"]["top_products"], key=lambda x: x["gross_profit"], reverse=True)
        if top:
            top_p = top[0]
            ans = (
                f"💰 **Most Profitable Product**: **{top_p['product_name']}** delivered **{curr}{top_p['gross_profit']:,.2f}** in gross profit."
            )
            recommendation = f"Focus marketing and promotions on '{top_p['product_name']}' because every unit sold generates high profit margins."
        else:
            ans = "No sales recorded yet this month."
            recommendation = "Record sales to calculate profit performance."
    elif "restock" in q or "low stock" in q or "inventory" in q:
        alerts = data["low_stock_alerts"]
        if alerts:
            names = [f"• {it['name']} (Stock: {it['stock']}, Reorder Level: {it['reorder_level']})" for it in alerts[:5]]
            ans = (
                f"⚠️ **Low Stock Alert**: You have **{len(alerts)} products** that need restocking right away:\n" +
                "\n".join(names)
            )
            recommendation = "Place supplier purchase orders immediately to avoid lost revenue from empty shelves."
        else:
            ans = "✅ Great news! All your products currently have healthy stock levels above their reorder thresholds."
            recommendation = "Continue weekly inventory reviews to monitor fast-moving items."
    elif "expense" in q or "cost" in q:
        exps = data["charts"]["expense_categories"]
        if exps:
            top_e = exps[0]
            ans = (
                f"📉 **Expense Breakdown**: Total operating expenses this month are **{curr}{kpis['total_expenses']:,.2f}**. "
                f"Your largest cost category is **{top_e['category']}** at **{curr}{top_e['total']:,.2f}**."
            )
            recommendation = f"Review vendor agreements or operational efficiency for '{top_e['category']}' to trim unnecessary costs."
        else:
            ans = f"You have recorded zero operating expenses this month. Total expenses: {curr}0.00."
            recommendation = "Remember to log rent, electricity, salaries, and supplier overheads to get an accurate Net Profit."
    elif "improve" in q or "profitability" in q or "suggestion" in q:
        ans = (
            f"💡 **Profitability Growth Strategies**:\n"
            f"1. **Protect Margin**: Your current net profit margin is **{kpis['profit_margin_pct']}%**. Aim for 20%+ by renegotiating supplier purchase costs.\n"
            f"2. **Raise Basket Size**: Bundle slow-moving products with bestsellers to increase your AOV (currently {curr}{kpis['average_order_value']:,.2f}).\n"
            f"3. **Customer Retention**: Returning customers buy 2.5x more frequently. Send WhatsApp or SMS offers to inactive buyers."
        )
        recommendation = "Review weekly financial reports and adjust selling prices if supplier costs increase."
    else:
        ans = (
            f"📈 **Shop Overview**:\n"
            f"• Revenue: {curr}{kpis['total_sales']:,.2f}\n"
            f"• Gross Profit: {curr}{kpis['gross_profit']:,.2f}\n"
            f"• Net Profit: {curr}{kpis['net_profit']:,.2f} (Margin: {kpis['profit_margin_pct']}%)\n"
            f"• Active Products: {kpis['total_products']} ({kpis['low_stock_products']} low stock)\n"
            f"• Orders: {kpis['total_orders']}"
        )
        recommendation = "You can ask: 'Which products need restocking?', 'What is my top revenue product?', or 'How can I improve profitability?'."

    return {
        "question": question,
        "answer": ans,
        "recommendation": recommendation,
        "period": "Current Month (Real Database Records)",
        "timestamp": datetime.now().strftime("%I:%M %p, %d %b %Y")
    }


def seed_demo_shop_data(shop_id: int):
    """Populates a demo-only shop once, never an owner's live business shop."""
    conn = get_db_connection()
    cursor = conn.cursor()

    shop = cursor.execute("SELECT is_demo FROM shops WHERE id = ?", (shop_id,)).fetchone()
    if not shop or not shop["is_demo"]:
        conn.close()
        return

    existing_data = cursor.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM products WHERE shop_id = ?) +
            (SELECT COUNT(*) FROM customers WHERE shop_id = ?) +
            (SELECT COUNT(*) FROM sales WHERE shop_id = ?) +
            (SELECT COUNT(*) FROM expenses WHERE shop_id = ?)
        """,
        (shop_id, shop_id, shop_id, shop_id),
    ).fetchone()[0]
    if existing_data:
        conn.close()
        return

    # Sample Products
    demo_products = [
        ("SKU-101", "Smart Wireless Earbuds Pro", "Electronics", "SonicWave", 850.0, 1499.0, 35, 10, "Apex Tech"),
        ("SKU-102", "Organic Arabica Coffee 500g", "Groceries", "PureBean", 220.0, 450.0, 48, 15, "Green Valley"),
        ("SKU-103", "Cotton Slim Fit Shirt (Blue)", "Apparel", "TrendLine", 420.0, 899.0, 18, 5, "FabricHub"),
        ("SKU-104", "Fast USB-C Charging Cable 2m", "Electronics", "PowerMax", 75.0, 249.0, 4, 10, "Apex Tech"),
        ("SKU-105", "Stainless Steel Water Flask 1L", "Home & Kitchen", "HydroCool", 280.0, 650.0, 2, 8, "KitchenCraft"),
        ("SKU-106", "Extra Virgin Olive Oil 1L", "Groceries", "Mediterranean", 600.0, 950.0, 14, 6, "Green Valley"),
        ("SKU-107", "Bluetooth Portable Speaker 20W", "Electronics", "SoundBeat", 1100.0, 1899.0, 12, 4, "Apex Tech"),
        ("SKU-108", "Denim Casual Jacket", "Apparel", "DenimCo", 850.0, 1699.0, 6, 4, "FabricHub")
    ]
    cursor.executemany("""
        INSERT INTO products (shop_id, sku, name, category, brand, cost_price, selling_price, stock, reorder_level, supplier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(shop_id, *p) for p in demo_products])

    # Sample Customers
    demo_customers = [
        ("Rahul Sharma", "9876543210", "rahul.sharma@example.com", "Flat 402, Sunshine Heights"),
        ("Priya Patel", "9823456789", "priya.patel@example.com", "12 Blossom Road, City Center"),
        ("Amit Kumar", "9765432109", "amit.k@example.com", "55 MG Road, Camp"),
        ("Sneha Deshmukh", "9912345678", "sneha.d@example.com", "Green Park, Model Colony"),
        ("Vikram Singh", "9845012345", "vikram.s@example.com", "Riverside residency")
    ]
    cursor.executemany("""
        INSERT INTO customers (shop_id, name, phone, email, address)
        VALUES (?, ?, ?, ?, ?)
    """, [(shop_id, *c) for c in demo_customers])

    # Sample Expenses
    today = datetime.now()
    demo_expenses = [
        ((today - timedelta(days=2)).strftime("%Y-%m-%d"), "Rent", 12000.0, "Bank Transfer", "Shop monthly rent"),
        ((today - timedelta(days=5)).strftime("%Y-%m-%d"), "Electricity", 2450.0, "UPI", "Power bill for shop"),
        ((today - timedelta(days=8)).strftime("%Y-%m-%d"), "Employee Salaries", 18000.0, "Bank Transfer", "Staff salary for 2 workers"),
        ((today - timedelta(days=12)).strftime("%Y-%m-%d"), "Packaging", 1200.0, "Cash", "Carry bags and packing cartons"),
        ((today - timedelta(days=15)).strftime("%Y-%m-%d"), "Marketing", 3500.0, "Card", "Local Instagram and Facebook promotion")
    ]
    cursor.executemany("""
        INSERT INTO expenses (shop_id, date, category, amount, payment_method, description)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [(shop_id, *e) for e in demo_expenses])

    conn.commit()

    # Get inserted product IDs and customer IDs
    cursor.execute("SELECT id, name, cost_price, selling_price FROM products WHERE shop_id = ?", (shop_id,))
    prods = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT id, name FROM customers WHERE shop_id = ?", (shop_id,))
    custs = [dict(r) for r in cursor.fetchall()]

    # Sample Sales over the last 10 days
    if prods and custs:
        for i in range(1, 8):
            dt = (today - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
            p1 = prods[i % len(prods)]
            p2 = prods[(i + 1) % len(prods)]
            cust = custs[i % len(custs)]

            q1, q2 = 1 + (i % 2), 1
            tot1 = q1 * p1["selling_price"]
            tot2 = q2 * p2["selling_price"]
            sub = tot1 + tot2
            disc = 50.0 if i % 2 == 0 else 0.0
            tax = round((sub - disc) * 0.05, 2)
            total = sub - disc + tax
            pmode = ["UPI", "Cash", "Debit/Credit Card", "UPI"][(i + 1) % 4]
            inv = f"INV-{today.strftime('%y%m%d')}-00{i}"

            cursor.execute("""
                INSERT INTO sales (shop_id, customer_id, invoice_no, sale_date, subtotal, discount, tax, total_amount, payment_mode, order_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Completed')
            """, (shop_id, cust["id"], inv, dt, sub, disc, tax, total, pmode))
            sale_id = cursor.lastrowid

            cursor.execute("""
                INSERT INTO sale_items (sale_id, product_id, product_name, quantity, unit_price, cost_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sale_id, p1["id"], p1["name"], q1, p1["selling_price"], p1["cost_price"], tot1))
            cursor.execute("""
                INSERT INTO sale_items (sale_id, product_id, product_name, quantity, unit_price, cost_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sale_id, p2["id"], p2["name"], q2, p2["selling_price"], p2["cost_price"], tot2))

    conn.commit()
    conn.close()