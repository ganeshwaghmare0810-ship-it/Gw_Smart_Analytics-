# CHAPTER 3: REQUIREMENT ANALYSIS

---

## 3.1 Functional Requirements (FR)

The system enforces 15 primary functional requirements across all modules:

- **FR-01 (User Account Lifecycle)**: The system shall provide secure user registration (`/api/register`) and login (`/api/login`) enforcing unique email addresses and cryptographic password hashing (scrypt via Werkzeug).
- **FR-02 (Session Security & Validation)**: The system shall establish an opaque database-backed web session (`user_sessions` table) delivered via an HTTP-only, SameSite=Lax cookie (`gw_web_session`) with configurable idle (12h) and absolute (30d) time-to-live restrictions.
- **FR-03 (Automated Tabular File Ingestion)**: The system shall accept CSV and Excel (`.xlsx`) datasets up to 50 MB, parse tabular schemas client-side and server-side, and dynamically extract numerical metrics without schema rejection.
- **FR-04 (10-Domain Interactive Dashboard Formulation)**: The system shall generate 10 domain-specific dashboards (Business, Financial, Sales, Inventory, HR, Marketing, Student, Customer, Production, Data Analytics) complete with line, bar, and doughnut visualizations.
- **FR-05 (Dynamic Multi-Chart Cross-Filtering)**: Clicking any chart slice, bar, or legend item shall instantly filter all remaining charts across the dashboard in real time without triggering page reloads.
- **FR-06 (Multi-Tenant Retail Shop Configuration)**: The system shall allow authenticated users to configure independent shop profiles (shop name, owner name, currency symbol, tax rate %, and business category) isolated by user ID.
- **FR-07 (Catalog & SKU Collision Prevention)**: The system shall maintain product records enforcing unique SKU constraints per shop (`UNIQUE(shop_id, sku)`), recording cost price, selling price, current stock, and reorder threshold levels.
- **FR-08 (Point-of-Sale Checkout & Stock Atomicity)**: Creating a sales invoice (`/api/shoppulse/sales`) shall execute within an atomic transaction that records line items, verifies stock availability, deducts sold quantities from the catalog, and logs an immutable record in `inventory_movements`.
- **FR-09 (Sale Cancellation & Inventory Restoration)**: Cancelling or deleting an invoice (`/api/shoppulse/sales/<id>/cancel`) shall automatically recalculate shop metrics, restore original product inventory levels, and log a compensatory audit entry.
- **FR-10 (Deterministic P&L Accounting Engine)**: The system shall calculate Gross Profit ($Revenue - COGS$) and Net Profit ($Gross Profit - Operating Expenses$) deterministically across user-specified date ranges.
- **FR-11 (Customer CRM & Purchase History)**: The system shall track individual customer records, cumulative spend, total order count, and identify high-value repeat spenders.
- **FR-12 (Dual Payment Subsystem)**: The system shall support UPI payments requiring a valid 12-digit UTR and provide Razorpay gateway order creation and signature verification.
- **FR-13 (Administrative Payment Audit & Gatekeeping)**: The system shall restrict pending payment approvals (`/api/admin/payments/pending`) to authenticated administrator accounts (`role = 'admin'`), unlocking 30-day Pro entitlements upon verification.
- **FR-14 (Official PDF Receipt & Report Generation)**: The system shall generate tamper-proof official PDF receipts and full analytical executive reports using client-side canvas rendering and ReportLab.
- **FR-15 (Automated Multi-Event SMTP Dispatch)**: The system shall dispatch formatted notification emails via SMTP over TLS upon successful account registration, account login, contact form submission, and payment receipt confirmation.

---

## 3.2 Non-Functional Requirements (NFR)

### 3.2.1 Performance & Latency
- **NFR-P1 (Dashboard Ingestion Time)**: Parsing a 10,000-row tabular dataset and formulating interactive visualizations shall complete in under **1.2 seconds**.
- **NFR-P2 (API Latency)**: Analytical dashboard queries (`/api/shoppulse/dashboard`) shall return computed KPI aggregations within **250 milliseconds**.
- **NFR-P3 (Concurrent Transactions)**: The database layer shall handle concurrent POS checkouts using SQLite WAL mode / MySQL InnoDB row-level locking without deadlocks.

### 3.2.2 Security & Data Integrity
- **NFR-S1 (Zero Credential Leakage)**: Passwords shall never be stored in plaintext; all credentials must utilize scrypt hashing with unique cryptographic salts.
- **NFR-S2 (SQL Injection Immunity)**: 100% of database queries must utilize parameterized SQL placeholders (`?` or `%s`); zero dynamic string interpolation is permitted.
- **NFR-S3 (Cross-Site Scripting Defense)**: All user inputs rendered in HTML must undergo strict DOM escaping or sanitization via `html.escape()` and MarkupSafe.
- **NFR-S4 (Sandbox File Isolation)**: Uploaded dataset files must be saved under cryptographically randomized UUID filenames in a dedicated storage directory outside the public web server root.

### 3.2.3 Usability & Accessibility
- **NFR-U1 (Responsive Design)**: Layouts must dynamically adapt across mobile viewports (min 360px), tablet screens (768px), and desktop widescreen monitors (1920px+).
- **NFR-U2 (Visual Ergonomics)**: Interfaces must employ a high-contrast dark glassmorphic design palette (`#080f1e` background with `#162746` cards) reducing visual fatigue during extended operational shifts.

---

## 3.3 Hardware Requirements

| Component | Minimum Client Environment | Minimum Production Server Environment |
|---|---|---|
| **Processor** | Dual-core 1.8 GHz (Intel i3 / AMD Ryzen 3 / ARM) | Quad-core 2.4 GHz (Intel Xeon / AMD EPYC) |
| **Random Access Memory (RAM)** | 4 GB | 8 GB (16 GB Recommended for large datasets) |
| **Storage** | 500 MB free disk space | 50 GB SSD / NVMe Storage |
| **Network Interface** | 1 Mbps broadband / 4G mobile data | 100 Mbps / 1 Gbps dedicated uplink |
| **Display Resolution** | 1280 × 720 (Mobile compatible down to 360px) | Headless server (Web console administered) |

---

## 3.4 Software Requirements & Runtime Dependencies

- **Server Operating System**: Linux (Ubuntu 22.04 LTS / Debian 12), Windows 10/11, or Windows Server.
- **Runtime Environment**: Python 3.10 to Python 3.13 (Python 3.12+ recommended).
- **Web Application Framework**: Flask 3.1.1, Werkzeug 3.1.3, Jinja2 3.1.6.
- **Relational Database**: SQLite 3.35+ (with WAL mode enabled) or MySQL 8.0+ / MariaDB 10.6+.
- **Data Science & ML Libraries**: Pandas 3.0+, NumPy 2.2+, Scikit-learn 1.4+, Scipy.
- **Document & PDF Engines**: ReportLab 5.1+, `html2canvas` 1.4.1, `jsPDF` 2.5.1, `python-docx` 1.2+.
- **Frontend Core Libraries**:
  - Chart.js (v4.x via jsDelivr CDN)
  - PapaParse (v5.4.1 for multi-threaded streaming CSV parsing)
  - SheetJS (xlsx v0.18.5 for native Excel workbook parsing)
  - Razorpay Checkout SDK (`https://checkout.razorpay.com/v1/checkout.js`)
- **Client Web Browsers**: Google Chrome (v100+), Microsoft Edge (v100+), Mozilla Firefox (v100+), Apple Safari (v15+).

---

## 3.5 Technical Constraints & Boundary Conditions
1. **Single-Threaded File Ingestion Limit**: Individual uploaded dataset files are constrained to **50 MB** via Flask's `MAX_CONTENT_LENGTH` to protect server memory.
2. **Synchronous SMTP Dispatch**: Email notifications are handled securely via SMTP with fallback failure logging to prevent API hanging if upstream mail servers timeout.
3. **Deterministic Financial Verification**: All profit calculations adhere strictly to the accounting standard equation:
   $$\text{Net Earnings} = (\text{Total Revenue} - \text{Discounts} + \text{Tax}) - \text{COGS} - \sum \text{Operating Expenses}$$
