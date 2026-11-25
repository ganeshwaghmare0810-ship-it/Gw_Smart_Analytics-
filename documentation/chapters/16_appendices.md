# APPENDICES & TECHNICAL REFERENCES

---

## APPENDIX A: COMPLETE REST API REFERENCE

The Flask backend exposes 28 verified REST API endpoints across 6 functional controllers:

| Method | Endpoint | Required Role | Request Payload | Response Schema | Description |
|---|---|---|---|---|---|
| `GET` | `/api/health` | Public | None | `{"status": "online", "database": "...", "port": 5000}` | Returns health status of server and database |
| `POST` | `/api/register` | Public | `{name, email, password, phone}` | `{"success": true, "user": {...}}` | Registers new account with scrypt password hash |
| `POST` | `/api/login` | Public | `{email, password}` | `{"success": true, "user": {...}}` | Authenticates user and issues `gw_web_session` cookie |
| `POST` | `/api/logout` | Public | None | `{"success": true, "message": "..."}` | Revokes web session and deletes browser cookies |
| `GET` | `/api/me` | Authenticated | None | `{"authenticated": true, "user": {...}}` | Returns current profile and subscription status |
| `GET` | `/api/users` | Admin | None | `{"success": true, "users": [...]}` | Lists all registered platform accounts |
| `GET` | `/api/users/<id>` | Admin / Owner | None | `{"success": true, "user": {...}}` | Retrieves single user account details |
| `GET` | `/api/subscription/status` | Authenticated | None | `{"active": true, "plan": "...", "paid": true}` | Checks active 30-day Pro plan status |
| `POST` | `/api/payment/submit` | Authenticated | `{method, utr_number}` | `{"success": true, "pending": true}` | Submits 12-digit UTR for admin approval |
| `POST` | `/api/payment/create-order`| Authenticated | None | `{"order_id": "...", "amount": 1000}` | Creates Razorpay order for ₹10.00 plan |
| `POST` | `/api/payment/verify` | Authenticated | `{order_id, payment_id, signature}` | `{"verified": true, "receipt": {...}}` | Verifies HMAC-SHA256 signature and activates access |
| `GET` | `/api/admin/payments/pending`| Admin | None | `{"success": true, "payments": [...]}` | Lists pending UPI claims for verification |
| `POST` | `/api/admin/payments/<id>/approve`| Admin | None | `{"success": true, "receipt": {...}}` | Approves payment, creates receipt, triggers email |
| `POST` | `/api/admin/payments/<id>/reject`| Admin | None | `{"success": true, "message": "..."}` | Rejects invalid or fraudulent claim |
| `GET` | `/api/payment/receipt` | Authenticated | None | `{"success": true, "receipt": {...}}` | Retrieves official payment receipt |
| `GET` | `/api/payment/history` | Authenticated | None | `{"success": true, "payments": [...]}` | Returns user's transaction history |
| `POST` | `/api/upload` | Authenticated | `multipart/form-data: file` | `{"success": true, "saved_name": "..."}` | Sandboxes dataset and stores metadata |
| `GET` | `/api/uploads` | Authenticated | None | `{"success": true, "files": [...]}` | Lists user's owned dataset uploads |
| `GET` | `/api/uploads/<file>` | Authenticated | None | Binary File Attachment | Securely downloads owned dataset file |
| `DELETE`| `/api/uploads/<id>` | Authenticated | None | `{"success": true, "message": "..."}` | Deletes dataset metadata and physical file |
| `GET` | `/api/shoppulse/shop` | Authenticated | None | `{"success": true, "shop": {...}}` | Retrieves current merchant shop settings |
| `POST` | `/api/shoppulse/shop` | Authenticated | `{name, currency, tax_rate, ...}` | `{"success": true, "shop": {...}}` | Updates shop configuration profile |
| `GET` | `/api/shoppulse/dashboard` | Authenticated | `?filter=this_month` | `{"analytics": {"kpis": {...}, ...}}` | Computes 12 live KPI cards and chart metrics |
| `GET` | `/api/shoppulse/products` | Authenticated | `?search=...&category=...` | `{"success": true, "products": [...]}` | Lists catalog products with stock levels |
| `POST` | `/api/shoppulse/products` | Authenticated | `{sku, name, cost_price, ...}` | `{"success": true, "product": {...}}` | Creates or updates catalog product |
| `GET` | `/api/shoppulse/sales` | Authenticated | `?start_date=...` | `{"success": true, "sales": [...]}` | Lists sales invoices with line items |
| `POST` | `/api/shoppulse/sales` | Authenticated | `{items: [...], payment_mode}` | `{"success": true, "invoice_no": "..."}`| Atomic POS sale with stock deduction |
| `POST` | `/api/shoppulse/sales/<id>/cancel`| Authenticated | None | `{"success": true, "message": "..."}` | Cancels sale and restores physical stock |

---

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
# Or install core modules directly:
python -m pip install Flask Werkzeug pandas numpy python-docx reportlab cashfree_pg requests
```

### Step 4: Environment Variable Configuration (`.env`)
Create a `.env` file in the project root:
```env
# Flask Server Configuration
GW_FLASK_PORT=5000
GW_FLASK_HOST=127.0.0.1
FLASK_SECRET_KEY=generate_a_64_character_random_hex_secret_here

# Security & CORS
GW_COOKIE_SECURE=0
GW_ALLOWED_ORIGINS=http://127.0.0.1:5000,http://localhost:5000

# Monetization & Limits
GW_PLAN_PRICE=10.0
GW_PLAN_VALIDITY_DAYS=30
GW_MAX_UPLOAD_MB=50

# SMTP Email Configuration (Gmail App Password)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_16_digit_google_app_password
SMTP_FROM_NAME="GW Smart Analytics"

# Optional Payment Gateways
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
CASHFREE_APP_ID=
CASHFREE_SECRET_KEY=
```

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

---

## APPENDIX C: REFERENCES & BIBLIOGRAPHY

1. Grinberg, M. (2018). *Flask Web Development: Developing Web Applications with Python*. O'Reilly Media.
2. Silberschatz, A., Korth, H. F., & Sudarshan, S. (2020). *Database System Concepts* (7th ed.). McGraw-Hill.
3. Chart.js Community. (2024). *Chart.js: Simple yet flexible JavaScript charting for designers & developers*. https://www.chartjs.org/
4. Werkzeug Developers. (2024). *Werkzeug Security Utilities & Password Hashing*. Pallets Projects. https://werkzeug.palletsprojects.com/
5. National Institute of Standards and Technology (NIST). (2020). *Digital Identity Guidelines: Authentication and Lifecycle Management* (SP 800-63B). U.S. Department of Commerce.
6. Mozilla Developer Network (MDN). (2024). *Web Speech API & SpeechRecognition Documentation*. https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API
7. ReportLab Europe Ltd. (2024). *ReportLab PDF Generation User Guide*. https://www.reportlab.com/documentation/
