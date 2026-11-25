# INSTALLATION AND CONFIGURATION GUIDE

## APPENDIX B: INSTALLATION & CONFIGURATION GUIDE

### Step 1: System Prerequisites
- Operating System: Windows 10/11, Ubuntu 20.04/22.04 LTS, or macOS.
- Python: Version 3.10 to 3.13 installed.
- Pip package manager configured in PATH.

### Step 2: Clone & Directory Navigation
```powershell
cd "f:\ganesh\smart\42435"
```

### Step 3: Install Required Dependencies
```powershell
python -m pip install -r requirements.txt
```
The Cashfree integration uses Python's standard-library HTTPS client, so no Cashfree SDK or payment package is needed.

### Step 4: Environment Variable Configuration (`.env`)
Copy `.env.example` to `.env` in the project root and fill in secrets locally. Do not commit `.env`.
```env
# Flask Server Configuration
GW_FLASK_PORT=5000
# The legacy Streamlit API uses a separate port when running alongside server.py.
GW_STREAMLIT_API_PORT=5001
FLASK_SECRET_KEY=generate_a_64_character_random_hex_secret_here

# Security & CORS
GW_COOKIE_SECURE=0
GW_ALLOWED_ORIGINS=http://127.0.0.1:5000,http://localhost:5000

# Existing subscription settings; dashboard/report payments are fixed at INR 10 server-side.
GW_PLAN_PRICE=10.0
GW_PLAN_VALIDITY_DAYS=30
GW_MAX_UPLOAD_MB=50

# Cashfree sandbox keys from the Cashfree merchant dashboard
CASHFREE_ENV=sandbox
CASHFREE_API_VERSION=2023-08-01
CASHFREE_APP_ID=your_sandbox_app_id
CASHFREE_SECRET_KEY=your_sandbox_secret_key
CASHFREE_WEBHOOK_SECRET=your_webhook_secret_or_leave_blank_to_use_secret_key
GW_PUBLIC_BASE_URL=http://127.0.0.1:5000
# Email of an existing trusted account to promote to administrator at startup.
GW_ADMIN_EMAIL=admin@example.com

# Configure the supplied special access code in this server-only variable.
GW_SPECIAL_ACCESS_CODE=enter_the_code_here

# Optional legacy payment settings
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
```

Cashfree credentials, the special code, and webhook secrets are server-only. Configure a Cashfree webhook at
`https://<your-public-host>/api/dashboard-payments/webhook` for deployed environments. The localhost return URL works
for sandbox checkout; Cashfree webhooks need a publicly reachable HTTPS URL or a secure tunnel during local testing.
Create the administrator account through registration first, set its email as `GW_ADMIN_EMAIL`, and restart the
server. Only that existing account is promoted; `/admin-payments` then allows it to manage special-code activation.

### Step 5: Launching the Application
```powershell
python server.py
```
Open your browser to:
- **Home Landing Page**: [http://127.0.0.1:5000/](http://127.0.0.1:5000/)
- **10-in-1 Dashboard Generator**: [http://127.0.0.1:5000/p](http://127.0.0.1:5000/p)
- **Universal Analytics Engine**: [http://127.0.0.1:5000/up](http://127.0.0.1:5000/up)
- **ShopPulse Retail POS Suite**: [http://127.0.0.1:5000/shoppulse](http://127.0.0.1:5000/shoppulse)
- **Admin Approval Portal**: [http://127.0.0.1:5000/admin-payments](http://127.0.0.1:5000/admin-payments)

The payment API runs in `server.py` on port 5000. The optional legacy Streamlit API
uses port 5001 by default; do not assign both servers the same port.

### Cashfree Sandbox Walkthrough
1. Start the server with `CASHFREE_ENV=sandbox` and the sandbox App ID/Secret Key in `.env`.
2. Register or sign in with a unique email and mobile number, then open `/p`.
3. Upload a dataset, generate the dashboard, and click **Save Dashboard & Continue to Payment · ₹10**.
4. Choose **Pay Now** and complete Cashfree's sandbox checkout. The UI remains locked until the backend verifies the Cashfree order and successful payment; webhook delivery is processed idempotently as well.
5. After verification, download the dashboard/report PDF or export CSV/Excel. Repeat with a newly generated dashboard to test a separate ₹10 order.
6. To test free access, set `GW_SPECIAL_ACCESS_CODE` in `.env`, restart, save a new dashboard, and enter the code. An administrator can deactivate/reactivate it from `/admin-payments`.

If the page displays `Unexpected token '<'` while an API call is running, the browser received an HTML error or page
instead of the expected JSON API response. Run the app with `python server.py`, open it from `http://127.0.0.1:5000`,
and restart the server after updating the code. Save the dashboard first; its dashboard and report IDs must appear
before applying a special access code or starting payment.

---
