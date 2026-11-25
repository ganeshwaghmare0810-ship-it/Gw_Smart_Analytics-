# API DOCUMENTATION

## APPENDIX A: COMPLETE REST API REFERENCE

The Flask backend exposes authenticated REST APIs across account, payment, dataset, and ShopPulse controllers. The existing subscription and UPI endpoints remain available; the per-dashboard Cashfree endpoints below are additive.

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
| `POST` | `/api/dashboard-payments/dashboards` | Authenticated | `{dashboard_name, report_name, dataset_name, rows}` | `{"dashboard_id": "...", "report_id": "..."}` | Persists an owned dashboard and paired report in SQLite |
| `GET` | `/api/dashboard-payments/dashboards` | Authenticated | None | `{"dashboards": [...]}` | Lists only the current user's dashboards and access status |
| `GET` | `/api/dashboard-payments/dashboards/<id>/access` | Authenticated / Owner | None | `{"allowed": false, "payment_status": "..."}` | Gets per-dashboard entitlement and transaction state |
| `POST` | `/api/dashboard-payments/dashboards/<id>/orders` | Authenticated / Owner | None | `{order_id, payment_session_id, amount: 10}` | Creates/reuses a server-priced Cashfree order |
| `POST` | `/api/dashboard-payments/verify` | Authenticated | `{order_id}` | `{"verified": true, "transaction_id": "..."}` | Queries Cashfree server-to-server and atomically grants access only for a matching INR 10 success |
| `POST` | `/api/dashboard-payments/webhook` | Cashfree signature | Cashfree raw webhook | `{"success": true}` | Verifies the timestamped HMAC signature and deduplicates webhook events |
| `POST` | `/api/dashboard-payments/dashboards/<id>/special-access` | Authenticated / Owner | `{code}` | `{"allowed": true, "access_type": "SPECIAL_CODE"}` | Server-only code validation and per-dashboard redemption tracking |
| `POST` | `/api/dashboard-payments/dashboards/<id>/artifacts` | Authenticated / Owner | `multipart/form-data: artifact_type, file` | `{"artifact_id": "..."}` | Stores a generated PDF/CSV/XLSX/PNG without exposing it for download |
| `GET` | `/api/dashboard-payments/dashboards/<id>/downloads/<type>` | Authenticated / Owner + Entitled | None | Binary File Attachment | Blocks the download unless that dashboard has verified paid or special-code access |
| `GET` | `/api/dashboard-payments/history` | Authenticated | None | `{"payments": [...]}` | Returns the current user's per-dashboard Cashfree transaction history |
| `GET` | `/api/dashboard-payments/admin/overview` | Admin | None | `{"users": [...], "payments": [...], "free_users": [...]}` | Lists users, paid/free access counts, and Cashfree transaction history |
| `GET` | `/api/dashboard-payments/admin/codes` | Admin | None | `{"codes": [{"is_active": true, "redemption_count": 0}]}` | Shows special-code status without returning the secret code |
| `POST` | `/api/dashboard-payments/admin/codes/<id>` | Admin | `{active: true}` | `{"success": true}` | Activates/deactivates a configured special code |
| `GET` | `/api/dashboard-payments/admin/users/<id>/dashboards` | Admin | None | `{"dashboards": [...]}` | Views a user's dashboard/report and transaction metadata |
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

## Dashboard Payment Guarantees
- `dashboards`, `reports`, `dashboard_access`, `special_access_codes`, `special_access_redemptions`, `dashboard_artifacts`, and `payment_webhooks` are created by an additive SQLite migration. Existing `payments` rows are retained; dashboard-specific gateway fields are added to that table.
- The dashboard amount is fixed to INR 10 in the backend. The browser never supplies a trusted amount or payment status.
- Cashfree success is confirmed by querying the order and its payment records from the backend. Webhooks are accepted only after timestamp + raw-body HMAC verification; duplicate event IDs are ignored.
- The dashboard ID and account ID are checked for every dashboard, report, special-code, artifact, and download operation. The download API requires a `dashboard_access` row.
- CSV/Excel/PDF/PNG dashboard artifacts are stored in SQLite and served only through the protected download API. Original user uploads continue to use their existing owner-checked upload routes.
- Store `GW_SPECIAL_ACCESS_CODE` in the private server `.env`; its raw value is not returned by any API or embedded in HTML/JavaScript. Admin activation controls new redemptions; existing redemptions remain in the audit history.
