# ShopPulse – Smart Shop Analytics & 10-in-1 Dashboard Generator

A complete, production-grade web application built for shop owners, retail managers, and data analysts to manage business data and automatically generate high-impact, interactive dashboards.

---

## 🌟 Key Features

### 1. 14 Comprehensive Shop Management Modules (`peyment.html`)
1. **Landing Page Overview**: Modern SaaS presentation with value propositions, feature cards, and instant launch actions.
2. **Shop Setup & Profile**: Configure shop name, owner name, currency (`₹`, `$`, `€`, `£`), business category, tax rate, and address.
3. **Main Shop Dashboard**: 12 real-time KPI cards (Today's Sales, This Week's Sales, This Month's Sales, Total Sales, Orders, AOV, Gross Profit, Net Profit, Total Expenses, Active SKUs, Low Stock count, Total Customers).
4. **Sales Management**: Multi-item line rows, barcode/product picker, item discounts, tax calculation, payment modes, receipt preview, and cancel/delete with automatic inventory rollback.
5. **Product Management**: SKU duplicate prevention within shop, cost price, selling price, profit margin %, stock valuation, and reorder levels.
6. **Top Products Analysis**: Top 5, 10, 20 ranking, best-sellers by quantity, highest revenue, highest gross profit, and slow-moving items alert.
7. **Customer CRM & Insights**: Customer purchase frequency, lifetime value (LTV), repeat customer %, and VIP spenders leaderboard.
8. **Inventory Tracking**: Stock valuation, low-stock red alerts, out-of-stock warnings, and transaction audit trails (Restock, Damage, Return, Count).
9. **Expenses Management**: Rent, electricity, employee salaries, transportation, packaging, marketing, and expense-to-revenue ratio analysis.
10. **Profit & Financial Analysis**: Transparent equation breakdown distinguishing Sales Revenue, COGS, Gross Margin, Operating Overheads, and Net Earnings.
11. **Payment Modes Distribution**: Analysis of UPI, Cash, Cards, and Net Banking with interactive doughnut charts.
12. **CSV & Excel Importer**: Drag-and-drop file upload with PapaParse & SheetJS, smart column mapping, and downloadable templates.
13. **AI Business Assistant**: Trusted natural language analytics engine answering business questions with verified database calculations.
14. **Reports & Exports**: Professional PDF download (via html2canvas & jsPDF), Excel (.xlsx) export, CSV export, and print formatting.

---

## 📊 10 Dashboards Every Business Needs (From the PDF Analysis)
The application integrates the 10 specialized dashboards analyzed directly from the official guide:

1. **🏢 Business Dashboard**: Sales vs Cost line, Top products by profit bar, Sales by category donut.
2. **💰 Financial Dashboard**: Clustered bars (Revenue vs Expenses), Cumulative cash flow line, Expense breakdown donut.
3. **🛒 Sales Dashboard**: Daily sales trend line, Top products bar, Regional sales donut.
4. **📦 Inventory Dashboard**: Stock by category bar, Low stock red alert list, In-stock health donut.
5. **👥 HR Dashboard**: Employees by department bar, Hires vs exits line, Gender/age mix donut.
6. **📢 Marketing Dashboard**: Leads by campaign bar, Weekly leads trend line, Lead source share donut.
7. **🎓 Student Dashboard**: Marks by subject bar, Monthly attendance line, Grade distribution donut.
8. **👤 Customer Dashboard**: Customer growth line, Top customers by spend bar, Customer segments donut.
9. **🏭 Production Dashboard**: Daily output vs target line, Cost by production line bar, Defect types donut.
10. **📈 Data Analytics Dashboard**: Histogram distribution, Category counts bar, Completeness health donut.
- **🧭 Decision Guide**: Interactive selector matrix based on Page 12 of the guide.

---

## ⚡ Live Auto-Update Engine
- Adding a sale instantly deducts stock, recalculates all 12 KPIs, and refreshes charts in real time.
- Cancelling a sale restores product stock and recalculates profit and inventory logs.
- Adding an expense immediately updates Net Profit, Expense Ratio, and Financial breakdowns.
- Importing CSV or Excel refreshes the entire dashboard without needing a page reload.

---

## 🔌 REST API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET/POST` | `/api/shoppulse/shop` | Retrieve or update shop profile settings |
| `GET` | `/api/shoppulse/dashboard` | 12 KPIs and chart aggregations with date filters |
| `GET/POST` | `/api/shoppulse/products` | List and create catalog products |
| `PUT/DELETE` | `/api/shoppulse/products/<id>` | Update or delete a product |
| `GET/POST` | `/api/shoppulse/sales` | List and create sales with stock deduction |
| `POST` | `/api/shoppulse/sales/<id>/cancel` | Cancel sale and restore stock |
| `GET/POST` | `/api/shoppulse/customers` | Customer CRM operations |
| `GET/POST` | `/api/shoppulse/expenses` | Record operating expenses |
| `GET` | `/api/shoppulse/inventory` | Stock movement audit trail |
| `POST` | `/api/shoppulse/inventory/adjust` | Manual restock / damage adjustments |
| `POST` | `/api/shoppulse/ai-assistant` | Query AI assistant with verified DB math |
| `POST` | `/api/payment/submit` | Submit a UPI UTR for administrator verification |
| `POST` | `/api/payment/create-order` | Create an authenticated Razorpay order |
| `POST` | `/api/payment/verify` | Verify captured Razorpay payment and activate access |

---

## 🚀 Execution & Setup Instructions (Windows)

### Option 1: Standalone Web Browser (Zero Setup)
Simply double click or open `peyment.html` in Chrome, Edge, or Firefox:
```text
file:///C:/Users/SHIVNERI/Documents/smart/Gw%20Smart%20data%20Analist/peyment.html
```
All features (1-Click Demo Data, Sales, Products, 10 Dashboards, CSV/Excel import/export, Chart.js, PDF generation, AI Assistant) run in the browser with reactive local persistence!

### Option 2: Running with Python Flask Backend
1. Open PowerShell or Terminal in this project directory:
   ```powershell
   cd "C:\Users\SHIVNERI\Documents\smart\Gw Smart data Analist"
   ```
2. Start the server:
   ```powershell
   python server.py
   ```
3. Open your browser:
   - **ShopPulse Generator**: [http://127.0.0.1:5000/peyment](http://127.0.0.1:5000/peyment)
   - **Home Page**: [http://127.0.0.1:5000/](http://127.0.0.1:5000/)

### Running Automated Tests
```powershell
python -m unittest discover -s tests -p "test_*.py"
```

### Account, migration, and private upload configuration

The default plan remains **₹10 for 30 days**. The same signed-in account and subscription status are shared by `h.html`, `up.html`, and `shoppulse.html`; successful payment on any of them unlocks the other two. Razorpay checkout is enabled by setting `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`. Without those keys, users can still submit a UPI UTR for administrator verification; a UPI claim does not activate access until approval. Copy `.env.example` to `.env` and set a persistent random `FLASK_SECRET_KEY`. For an HTTPS deployment set `GW_COOKIE_SECURE=1`, configure `GW_ALLOWED_ORIGINS` with the exact site origins, and keep `FLASK_DEBUG=0`.

The application stores account, session, subscription, payment, receipt, upload metadata, and ShopPulse records in `gw_smart_analytics2.db` beside `database.py`. The first application startup against an existing database creates a SQLite-consistent backup beside the database before applying schema changes. The backup name is `gw_smart_analytics2.db.pre-migration-<timestamp>.bak`. The migration preserves rows, checks legacy duplicate values, and logs conflicts instead of deleting records or forcing an unsafe unique index. Review startup warnings and resolve any reported conflicts before relying on the corresponding uniqueness constraint.

Uploads are stored outside the application directory by default in a sibling `vscode_uploads` directory, with per-account metadata in SQLite. Set `GW_UPLOAD_DIR` to an absolute, private persistent directory when deploying (for example, `C:\GW-Smart-Analytics-Private-Uploads`). Back up this directory together with the SQLite database. Do not serve it through a web server's static-file configuration.

Windows setup and verification:

```powershell
Copy-Item .env.example .env
# Edit .env and replace FLASK_SECRET_KEY and SMTP_PASSWORD before production use.
python -m unittest discover -s tests -p "test_*.py"
python server.py
```

For a live database, stop the application and take an additional manual backup before deployment:

```powershell
python -c "import sqlite3; src=sqlite3.connect(r'gw_smart_analytics2.db'); dst=sqlite3.connect(r'gw_smart_analytics2.db.manual-backup'); src.backup(dst); dst.close(); src.close()"
```

Deploy the application and private upload directory together on persistent storage, serve the site only behind HTTPS, and verify that the startup log reports a backup and no unresolved migration conflicts. The login cookie is database-backed, idle-expiring, and replaced on each successful login; the previous browser's next protected API request receives HTTP 401. Logging out revokes only the browser session, while Streamlit bridge tokens remain associated with and invalidated by that session without deleting unrelated integration tokens.

New ShopPulse accounts start with an empty live shop. Demo records are only created through the separate demo-shop helper, so sample data is not mixed into real business records or re-seeded on normal visits.
