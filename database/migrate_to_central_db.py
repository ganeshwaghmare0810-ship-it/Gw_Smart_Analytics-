import os
import shutil
import sqlite3
from pathlib import Path

BASE_DIR = Path(r"F:\ganesh\smart\42435")
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(parents=True, exist_ok=True)
CENTRAL_DB_PATH = DB_DIR / "gw_smart_analytics.db"

# Source database to restore from (the comprehensive 45MB database)
SOURCE_DB_PATH = Path(r"C:\Users\whari\.gemini\antigravity\brain\91f7058a-ca24-46f1-be7a-a2cd7f9dde03\scratch\gw_smart_analytics2.db")

print(f"Target Central Database: {CENTRAL_DB_PATH}")
print(f"Source Database: {SOURCE_DB_PATH} (exists: {SOURCE_DB_PATH.exists()}, size: {SOURCE_DB_PATH.stat().st_size:,} bytes)")

# 1. Copy cleanly via sqlite3 backup API
print("Copying via SQLite backup API...")
src_conn = sqlite3.connect(SOURCE_DB_PATH)
dst_conn = sqlite3.connect(CENTRAL_DB_PATH)
src_conn.backup(dst_conn)
src_conn.close()

# 2. Check integrity
dst_conn.execute("PRAGMA foreign_keys = ON;")
dst_conn.execute("PRAGMA busy_timeout = 30000;")
integrity = dst_conn.execute("PRAGMA integrity_check;").fetchall()
print(f"Integrity check after copy: {integrity}")

# 3. Apply schema additions for contact_messages, dashboard_configs, settings
cur = dst_conn.cursor()
cur.execute("BEGIN IMMEDIATE;")

# Contact messages table
cur.execute("""
CREATE TABLE IF NOT EXISTS contact_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT DEFAULT 'NEW',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

# Dashboard configs table
cur.execute("""
CREATE TABLE IF NOT EXISTS dashboard_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    config_name TEXT NOT NULL,
    config_data TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
""")

# Settings table (for gwpeyment or system key-value settings)
cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")

# Verify all required tables exist
required_tables = [
    "users",
    "payments",
    "subscriptions",
    "gateway_orders",
    "receipts",
    "dashboard_configs",
    "audit_logs",
    "auth_tokens",
    "contact_messages",
    "customers",
    "products",
    "sales",
    "expenses",
    "inventory_movements",
    "dashboards",
    "reports",
    "dashboard_access",
    "dashboard_artifacts",
    "special_access_codes",
    "special_access_attempts",
    "special_access_redemptions",
    "uploads",
    "user_sessions",
    "swati_conversations",
    "swati_messages",
    "swati_datasets",
    "swati_reports",
    "shops",
    "sale_items",
    "settings",
    "payment_webhooks",
    "gw_dashboard_payment_migrations"
]

# Populate dashboard_configs with initial configs from dashboards if empty
cur.execute("SELECT count(*) FROM dashboard_configs;")
if cur.fetchone()[0] == 0:
    cur.execute("""
    INSERT INTO dashboard_configs (user_id, config_name, config_data, created_at, updated_at)
    SELECT user_id, name, dashboard_json, created_at, created_at FROM dashboards;
    """)
    print(f"Populated {cur.rowcount} configs into dashboard_configs from existing dashboards.")

dst_conn.commit()

# Print status of all tables
print("\n=== Central Database Table Status ===")
cur.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name;")
all_tables = [r[0] for r in cur.fetchall()]
print(f"Total tables and views in central DB: {len(all_tables)}")

for t in sorted(all_tables):
    if not t.startswith("sqlite_"):
        cur.execute(f'SELECT count(*) FROM "{t}"')
        cnt = cur.fetchone()[0]
        status = "[REQUIRED]" if t in required_tables else "[APP]"
        print(f"  {status} {t:32s} : {cnt:4d} rows")

# Foreign key validation
cur.execute("PRAGMA foreign_key_check;")
fk_issues = cur.fetchall()
print(f"\nForeign key check: {len(fk_issues)} issues found.")
if fk_issues:
    for issue in fk_issues:
        print("  FK Issue:", issue)

dst_conn.close()
print("\nCentral database migration completed successfully!")
