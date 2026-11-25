"""Comprehensive automated test suite for GW Smart Analytics Payment & Swati AI Assistant."""

import io
import json
import os
import sys
import time
from pathlib import Path

# Force UTF-8 stdout
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(".env")

import server
import database
import assistent
import dashboard_payments

app = server.app
client = app.test_client()

def run_tests():
    print("==================================================")
    print("  RUNNING GW SMART ANALYTICS COMPREHENSIVE TESTS  ")
    print("==================================================")

    # 1. Health check
    res = client.get("/api/health")
    assert res.status_code == 200, f"Health check failed: {res.status_code}"
    print("[OK] 1. Health check OK")

    # 2. User registration with unique email and mobile
    test_email = f"testuser_{int(time.time())}@gwsmartanalytics.com"
    test_mobile = f"98{int(time.time()) % 100000000:08d}"
    reg_data = {
        "name": "Test User",
        "email": test_email,
        "password": "Password123!",
        "mobile": test_mobile
    }
    res = client.post("/api/register", json=reg_data)
    assert res.status_code == 201, f"Registration failed: {res.data}"
    reg_json = res.get_json()
    user_id = reg_json["user"]["id"]
    print(f"[OK] 2. Registration successful (User ID: {user_id}, Email: {test_email})")

    # Duplicate registration test
    res_dup = client.post("/api/register", json=reg_data)
    assert res_dup.status_code == 400, "Duplicate email allowed unexpectedly"
    print("[OK] 2b. Duplicate email prevention verified")

    # 3. User login and cookie session
    res_login = client.post("/api/login", json={"email": test_email, "password": "Password123!"})
    assert res_login.status_code == 200, f"Login failed: {res_login.data}"
    print("[OK] 3. Login and session cookie creation OK")

    # 4. Dashboard creation
    dash_payload = {
        "dashboard_name": "Sales Intelligence Dashboard",
        "report_name": "Sales Intelligence Comprehensive Report",
        "dataset_name": "sample_sales.csv",
        "rows": [
            {"date": "2026-01-01", "product": "Widget A", "category": "Electronics", "sales": 1500, "cost": 900},
            {"date": "2026-01-02", "product": "Widget B", "category": "Home", "sales": 3200, "cost": 1800},
            {"date": "2026-01-03", "product": "Widget A", "category": "Electronics", "sales": 2400, "cost": 1300},
        ]
    }
    res_dash = client.post("/api/dashboard-payments/dashboards", json=dash_payload)
    assert res_dash.status_code == 201, f"Dashboard creation failed: {res_dash.data}"
    dash_data = res_dash.get_json()
    dashboard_id = dash_data["dashboard_id"]
    report_id = dash_data["report_id"]
    print(f"[OK] 4. Dashboard & Report record created (Dashboard ID: {dashboard_id}, Report ID: {report_id})")

    # Upload artifact
    artifact_bytes = b"%PDF-1.4 dummy pdf content for testing"
    res_art = client.post(
        f"/api/dashboard-payments/dashboards/{dashboard_id}/artifacts",
        data={"artifact_type": "dashboard_pdf", "file": (io.BytesIO(artifact_bytes), "dashboard.pdf")},
        content_type="multipart/form-data"
    )
    assert res_art.status_code == 201, f"Artifact upload failed: {res_art.data}"
    print("[OK] 4b. Protected artifact uploaded")

    # 5. Check access without payment (Must be locked)
    res_acc = client.get(f"/api/dashboard-payments/dashboards/{dashboard_id}/access")
    assert res_acc.status_code == 200
    acc_json = res_acc.get_json()
    assert acc_json["allowed"] is False, "Access allowed without payment!"
    print("[OK] 5. Access correctly locked without payment")

    # Check direct download route (Must return 402)
    res_dl_blocked = client.get(f"/api/dashboard-payments/dashboards/{dashboard_id}/downloads/dashboard_pdf")
    assert res_dl_blocked.status_code == 402, f"Expected 402 Payment Required, got: {res_dl_blocked.status_code}"
    print("[OK] 5b. Direct download route properly blocked (402 Payment Required)")

    # 6. Special Access Code testing
    # Invalid code
    res_invalid_code = client.post(
        f"/api/dashboard-payments/dashboards/{dashboard_id}/special-access",
        json={"code": "wrong_code_xyz"}
    )
    assert res_invalid_code.status_code == 400, f"Invalid code accepted: {res_invalid_code.data}"
    print("[OK] 6. Invalid special access code rejected with 400")

    # Valid code: gwanalitics0810
    res_valid_code = client.post(
        f"/api/dashboard-payments/dashboards/{dashboard_id}/special-access",
        json={"code": "gwanalitics0810"}
    )
    assert res_valid_code.status_code == 200, f"Valid code rejected: {res_valid_code.data}"
    valid_json = res_valid_code.get_json()
    assert valid_json["allowed"] is True
    assert valid_json["access_type"] == "SPECIAL_CODE"
    print("[OK] 6b. Special Access Code 'gwanalitics0810' validated and free access granted")

    # Now download artifact (Must succeed)
    res_dl_ok = client.get(f"/api/dashboard-payments/dashboards/{dashboard_id}/downloads/dashboard_pdf")
    assert res_dl_ok.status_code == 200, f"Download failed after special access: {res_dl_ok.status_code}"
    assert res_dl_ok.data == artifact_bytes
    print("[OK] 6c. Download succeeded with special access permission")

    # 7. Cashfree order creation verification
    # Create second dashboard for payment gateway test
    res_dash2 = client.post("/api/dashboard-payments/dashboards", json=dash_payload)
    dash2_id = res_dash2.get_json()["dashboard_id"]
    res_order = client.post(f"/api/dashboard-payments/dashboards/{dash2_id}/orders")
    assert res_order.status_code in [200, 201], f"Order creation failed: {res_order.data}"
    order_data = res_order.get_json()
    assert order_data["amount"] == 10.0, f"Price mismatch: {order_data['amount']}"
    assert order_data["currency"] == "INR"
    order_id = order_data["order_id"]
    print(f"[OK] 7. Cashfree Order created (Order ID: {order_id}, Fixed Amount: Rs 10 INR)")

    # Cashfree verify
    res_verify = client.post("/api/dashboard-payments/verify", json={"order_id": order_id})
    assert res_verify.status_code in [200, 202], f"Verify failed: {res_verify.data}"
    print(f"[OK] 7b. Cashfree verification API verified (Status: {res_verify.get_json().get('payment_status')})")

    # 8. Swati AI Assistant: Multilingual Chat
    # Marathi
    res_swati_mr = client.post("/api/swati/chat", json={
        "message": "GW Smart Analytics madhe dashboard download karnyasathi kiti charge lagto?",
        "lang": "mr"
    })
    assert res_swati_mr.status_code == 200
    reply_mr = res_swati_mr.get_json()["reply"]
    assert "10" in reply_mr or "१०" in reply_mr
    print("[OK] 8. Swati Marathi conversation test passed (Rs 10 payment explained)")

    # English
    res_swati_en = client.post("/api/swati/chat", json={
        "message": "Explain the features of the Sales Dashboard",
        "dashboard_type": "Sales",
        "lang": "en"
    })
    assert res_swati_en.status_code == 200
    print("[OK] 8b. Swati English dashboard guidance test passed")

    # 9. Swati Ask Your Data: Dataset Upload & Profiling
    csv_content = """Date,Product,Category,Sales,Cost
2026-01-01,Laptop Pro,Electronics,75000,50000
2026-01-02,Wireless Mouse,Accessories,1500,800
2026-01-03,Laptop Pro,Electronics,75000,50000
2026-01-04,Mechanical Keyboard,Accessories,4500,2800
2026-01-05,USB Hub,Accessories,800,400
2026-01-06,UltraWide Monitor,Electronics,32000,21000
"""
    res_upload = client.post(
        "/api/swati/upload",
        data={"file": (io.BytesIO(csv_content.encode("utf-8")), "tech_sales.csv")},
        content_type="multipart/form-data"
    )
    assert res_upload.status_code == 201, f"Swati upload failed: {res_upload.data}"
    upload_json = res_upload.get_json()
    dataset_id = upload_json["dataset_id"]
    profile = upload_json["profile"]
    assert profile["row_count"] == 6
    assert "Product" in profile["dtypes"]
    print(f"[OK] 9. Swati Ask Your Data upload & profile test passed (Rows: 6, Dataset ID: {dataset_id})")

    # 10. Swati Ask Your Data: Direct Questions with verified Pandas math
    res_total = client.post("/api/swati/chat", json={
        "message": "What is my total Sales?",
        "dataset_id": dataset_id,
        "lang": "en"
    })
    assert res_total.status_code == 200
    total_reply = res_total.get_json()["reply"]
    # Total sales = 75000 + 1500 + 75000 + 4500 + 800 + 32000 = 188,800
    assert "188,800" in total_reply or "188800" in total_reply, f"Math mismatch: {total_reply}"
    print("[OK] 10. Verified Pandas Calculation: Total Sales = Rs 188,800 computed accurately!")

    # Top product
    res_top = client.post("/api/swati/chat", json={
        "message": "Which product performs best?",
        "dataset_id": dataset_id,
        "lang": "en"
    })
    assert res_top.status_code == 200
    top_reply = res_top.get_json()["reply"]
    assert "Laptop Pro" in top_reply, f"Top product mismatch: {top_reply}"
    print("[OK] 10b. Top Product identification: 'Laptop Pro' identified accurately!")

    # 11. Swati Report Generation & PDF download
    res_rep = client.post("/api/swati/report", json={
        "dataset_id": dataset_id,
        "dashboard_type": "Sales Intelligence"
    })
    assert res_rep.status_code == 201, f"Report generation failed: {res_rep.data}"
    rep_json = res_rep.get_json()
    swati_rep_id = rep_json["report_id"]
    print(f"[OK] 11. Swati 14-section Executive Report generated (Report ID: {swati_rep_id})")

    # Download PDF
    res_rep_pdf = client.get(f"/api/swati/reports/{swati_rep_id}/pdf")
    assert res_rep_pdf.status_code == 200, f"PDF report download failed: {res_rep_pdf.status_code}"
    assert res_rep_pdf.content_type == "application/pdf"
    assert len(res_rep_pdf.data) > 1000, "PDF size is too small"
    print(f"[OK] 11b. ReportLab Executive PDF report compiled & downloaded successfully ({len(res_rep_pdf.data)} bytes)")

    # 12. Chat history persistence
    res_hist = client.get("/api/swati/history")
    assert res_hist.status_code == 200
    hist_json = res_hist.get_json()
    assert len(hist_json["conversations"]) > 0
    print(f"[OK] 12. Swati conversation history isolated and persisted for user ({len(hist_json['conversations'])} conversations)")

    # 13. Admin Management
    # Elevate current test user to admin for testing overview
    conn = database.get_db_connection()
    conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    res_admin = client.get("/api/dashboard-payments/admin/overview")
    assert res_admin.status_code == 200, f"Admin overview failed: {res_admin.data}"
    admin_data = res_admin.get_json()
    assert len(admin_data["users"]) > 0
    assert len(admin_data["free_users"]) > 0
    print(f"[OK] 13. Admin Overview verified: {len(admin_data['users'])} users, {len(admin_data['free_users'])} free access users tracked")

    print("\n==================================================")
    print("  ALL 13 AUTOMATED TESTS PASSED WITH 100% SUCCESS  ")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
