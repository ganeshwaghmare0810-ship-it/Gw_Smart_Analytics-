# CHAPTER 7: MODULE-WISE IMPLEMENTATION

---

## 7.1 User Authentication & Session Security Module
Implemented in [`database.py`](file:///f:/ganesh/smart/42435/database.py#L98-L115) and [`server.py`](file:///f:/ganesh/smart/42435/server.py#L301-L435):
- **Registration (`create_user`)**: Enforces email format validation, checks uniqueness against `users.email`, generates an scrypt hash using `werkzeug.security.generate_password_hash`, and commits a clean record.
- **Authentication (`authenticate_user`)**: Fetches record by email, executes `check_password_hash(user["password_hash"], password)`, and inspects active subscription dates in `subscriptions`.
- **Opaque Web Session Management (`create_web_session`)**:
  Generates a cryptographically random 32-byte hexadecimal token (`secrets.token_hex(32)`), stores the SHA-256 digest in `user_sessions.token_hash`, and sets the cookie `gw_web_session` on the HTTP response.
- **Session Bridge Token (`create_session_bridge_token`)**:
  Creates a short-lived (1-hour) cryptographic token in `auth_tokens` enabling cross-application session synchronization.

---

## 7.2 File Upload & Dataset Processing Engine
Implemented in [`upload_storage.py`](file:///f:/ganesh/smart/42435/upload_storage.py) and [`server.py`](file:///f:/ganesh/smart/42435/server.py#L710-L794):
- **Path Sandboxing**: The upload directory (`UPLOAD_DIR`) is determined from `GW_UPLOAD_DIR` or defaulted to a parent directory outside the web application root, preventing path traversal attacks.
- **MIME & Extension Whitelist**: Permitted extensions are strictly locked to `.csv`, `.xlsx`, `.xls`, `.tsv`, `.txt`, `.json`, `.parquet`.
- **Unique Storage Keys**: Original filenames are sanitized using regex (`[A-Za-z0-9_.-]`), prepended with random UUID4 hex identifiers, and verified via `resolve()`:
  ```python
  if resolved_path.parent != UPLOAD_DIR.resolve():
      raise ValueError("Invalid storage destination.")
  ```
- **Metadata Logging**: Records user ownership, original filename, hashed storage name, and byte size in the `uploads` table.

---

## 7.3 POS Sales & Automatic Stock Deduction Module
Implemented in [`database.py`](file:///f:/ganesh/smart/42435/database.py#L2480-L2600):
- **Atomic Checkout (`create_sale`)**:
  Executes within an atomic transaction. For each item in the purchase cart:
  1. Verifies that `products.stock >= quantity`.
  2. Decrements `products.stock` by the sold quantity.
  3. Inserts a line item into `sale_items`, preserving snapshots of `unit_price` and `cost_price`.
  4. Inserts an audit movement into `inventory_movements` with `movement_type = 'Sale'`.
- **Atomic Rollback on Sale Cancellation (`cancel_or_delete_sale`)**:
  Restores physical stock levels for all associated line items, marks invoice status as `'Cancelled'`, and records compensatory adjustment logs.

---

## 7.4 Inventory Tracking & Movement Audit Engine
Implemented in [`database.py`](file:///f:/ganesh/smart/42435/database.py#L2750-L2830):
- **Audit Logging (`log_inventory_movement`)**:
  Whenever stock alters via POS sale, manual restock, or damaged write-off, an immutable record is appended into `inventory_movements` recording:
  - `shop_id`, `product_id`, `movement_type`, `quantity`, `previous_stock`, `new_stock`, `reason`, `reference_id`.
- **Stock Valuation Calculation**: Computes live warehouse capital:
  $$\text{Inventory Cost Valuation} = \sum (\text{stock} \times \text{cost\_price})$$
  $$\text{Retail Sales Valuation} = \sum (\text{stock} \times \text{selling\_price})$$

---

## 7.5 Expenses & Operating Overhead Calculation Module
Implemented in [`database.py`](file:///f:/ganesh/smart/42435/database.py#L2850-L2930):
- **Expense Ingestion (`save_expense`)**: Categorizes operating disbursements (`Rent`, `Salary`, `Electricity`, `Transport`, `Packaging`, `Marketing`).
- **P&L Aggregation Engine**: Groups expenses by monthly date ranges and computes expense-to-revenue ratios to surface operational burn rates.

---

## 7.6 Automated PDF Report & Receipt Generation Engine
Implemented in client-side HTML/JS and [`hh.py`](file:///f:/ganesh/smart/42435/hh.py#L1800-L1950):
- **Client Canvas Rendering**: Utilizes `html2canvas` to capture the DOM state of charts, KPI cards, and receipt containers at 2× pixel density, generating crisp vector-aligned PDFs via `jspdf`.
- **Server-Side ReportLab Engine**: Constructs multi-page executive dossiers complete with cover pages, KPI summary tables, embedded Matplotlib visualizations, and financial statement tables.

---

## 7.7 Email Notification & SMTP Dispatch Engine
Implemented in [`mailer.py`](file:///f:/ganesh/smart/42435/mailer.py):
- **Transport Security**: Connects to Gmail SMTP (`smtp.gmail.com:587`) initiating mandatory `starttls()` encryption.
- **Event-Driven Dispatch**:
  1. `send_welcome_email`: Dispatched immediately upon user registration.
  2. `send_login_notification`: Notifies account owner whenever a new session token is granted.
  3. `send_payment_notification`: Delivers an official payment receipt and access confirmation upon administrator approval.
  4. `send_contact_message`: Routes user support inquiries from `/contact.html` directly to the administrative inbox.
- **Failure Resilience**: If network issues or invalid credentials occur, warnings are logged locally to `email_notifications.log` without failing the user's primary API transaction.
