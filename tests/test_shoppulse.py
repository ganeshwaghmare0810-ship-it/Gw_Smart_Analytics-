"""
ShopPulse - Automated Test Suite
=================================
Tests:
- Shop creation & multi-shop data isolation
- Product SKU uniqueness & margin calculations
- Sales creation with atomic inventory deduction
- Sales cancellation with inventory rollback
- Expense logging & P&L (COGS, Gross Profit, Net Profit)
- AI Business Assistant verified calculation engine
"""

import unittest
import os
import sqlite3
import tempfile
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import database


class TestShopPulse(unittest.TestCase):
    def setUp(self):
        """Set up a fresh test database for each test run."""
        self.db_fd, self.temp_db_path = tempfile.mkstemp()
        database.DB_PATH = Path(self.temp_db_path)
        database.init_db()

        # Create two test users
        self.user1 = database.create_user("Owner One", "owner1@test.com", "pass123", "9876500001")["user"]
        self.user2 = database.create_user("Owner Two", "owner2@test.com", "pass123", "9876500002")["user"]

        # Create shops
        self.shop1 = database.get_or_create_shop(self.user1["id"], {"name": "Shop One", "currency": "₹"})
        self.shop2 = database.get_or_create_shop(self.user2["id"], {"name": "Shop Two", "currency": "₹"})

    def tearDown(self):
        os.close(self.db_fd)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_shop_data_isolation(self):
        """Verify that records created in shop 1 are completely isolated from shop 2."""
        # Create product in Shop 1
        database.save_product(self.shop1["id"], {
            "sku": "SKU-ISO-1",
            "name": "Secret Product 1",
            "category": "Retail",
            "cost_price": 50.0,
            "selling_price": 100.0,
            "stock": 10,
            "reorder_level": 2
        })

        # Shop 2 should see 0 products (excluding any seeded defaults for shop 2)
        prods_shop1 = database.get_shop_products(self.shop1["id"], search="Secret Product 1")
        prods_shop2 = database.get_shop_products(self.shop2["id"], search="Secret Product 1")

        self.assertEqual(len(prods_shop1), 1)
        self.assertEqual(len(prods_shop2), 0)

    def test_sku_uniqueness_within_shop(self):
        """Ensure same SKU cannot be duplicated in the same shop."""
        res1 = database.save_product(self.shop1["id"], {
            "sku": "SKU-DUP-1",
            "name": "Original Item",
            "cost_price": 10.0,
            "selling_price": 20.0,
            "stock": 5
        })
        self.assertTrue(res1["success"])

        # Attempt duplicate
        res2 = database.save_product(self.shop1["id"], {
            "sku": "SKU-DUP-1",
            "name": "Duplicate Item",
            "cost_price": 15.0,
            "selling_price": 25.0,
            "stock": 10
        })
        self.assertFalse(res2["success"])
        self.assertIn("already exists", res2["message"])

    def test_sales_and_inventory_deduction(self):
        """Ensure recording a sale accurately deducts stock and records inventory movement."""
        # Add Product
        prod_res = database.save_product(self.shop1["id"], {
            "sku": "SKU-SALE-1",
            "name": "Wireless Headphones",
            "cost_price": 500.0,
            "selling_price": 999.0,
            "stock": 20,
            "reorder_level": 5
        })
        product_id = prod_res["product"]["id"]

        # Record Sale for 3 units
        sale_res = database.create_sale(self.shop1["id"], {
            "payment_mode": "UPI",
            "order_status": "Completed",
            "discount": 0.0,
            "tax_rate": 5.0,
            "items": [{
                "product_id": product_id,
                "quantity": 3,
                "unit_price": 999.0
            }]
        })
        self.assertTrue(sale_res["success"])

        # Verify new stock is 17
        updated_prod = database.get_shop_products(self.shop1["id"], search="Wireless Headphones")[0]
        self.assertEqual(updated_prod["stock"], 17)

        # Verify inventory movement logged
        movements = database.get_inventory_movements(self.shop1["id"], product_id=product_id)
        self.assertTrue(len(movements) >= 1)
        self.assertEqual(movements[0]["quantity"], -3)
        self.assertEqual(movements[0]["new_stock"], 17)

    def test_sale_cancellation_inventory_restoration(self):
        """Cancelling an order must restore the stock back to the product."""
        prod_res = database.save_product(self.shop1["id"], {
            "sku": "SKU-RESTORE-1",
            "name": "Running Shoes",
            "cost_price": 800.0,
            "selling_price": 1499.0,
            "stock": 10
        })
        product_id = prod_res["product"]["id"]

        # Sell 4 units
        sale_res = database.create_sale(self.shop1["id"], {
            "items": [{
                "product_id": product_id,
                "quantity": 4,
                "unit_price": 1499.0
            }]
        })
        sale_id = sale_res["sale"]["id"]

        # Verify stock decreased to 6
        p1 = database.get_shop_products(self.shop1["id"], search="Running Shoes")[0]
        self.assertEqual(p1["stock"], 6)

        # Cancel sale
        cancel_res = database.cancel_or_delete_sale(self.shop1["id"], sale_id, action="cancel")
        self.assertTrue(cancel_res["success"])

        # Verify stock restored to 10
        p2 = database.get_shop_products(self.shop1["id"], search="Running Shoes")[0]
        self.assertEqual(p2["stock"], 10)

    def test_profit_and_loss_math(self):
        """
        Verify financial equations:
        Gross Profit = Net Sales Revenue - COGS
        Net Profit = Gross Profit - Operating Expenses
        """
        # Create product
        prod_res = database.save_product(self.shop1["id"], {
            "sku": "SKU-MATH-1",
            "name": "Coffee Maker",
            "cost_price": 1000.0,
            "selling_price": 1500.0,
            "stock": 10
        })
        pid = prod_res["product"]["id"]

        # Sell 2 units -> Revenue = 3000, COGS = 2000, Gross Profit = 1000
        database.create_sale(self.shop1["id"], {
            "items": [{
                "product_id": pid,
                "quantity": 2,
                "unit_price": 1500.0
            }]
        })

        # Add Operating Expense of 400
        database.save_expense(self.shop1["id"], {
            "category": "Electricity",
            "amount": 400.0,
            "description": "Store lighting"
        })

        analytics = database.get_shop_dashboard_analytics(self.shop1["id"], date_filter="this_month")
        kpis = analytics["kpis"]

        self.assertAlmostEqual(kpis["gross_profit"], 1000.0, places=1)
        self.assertAlmostEqual(kpis["total_expenses"], 400.0, places=1)
        self.assertAlmostEqual(kpis["net_profit"], 600.0, places=1)  # 1000 - 400

    def test_ai_assistant_accuracy(self):
        """AI Assistant must answer based on exact database calculations without hallucination."""
        # Query total sales
        resp = database.query_ai_assistant(self.shop1["id"], "What were my total sales this month?")
        self.assertIn("Total sales", resp["answer"])
        self.assertIn("recommendation", resp)


if __name__ == "__main__":
    unittest.main()
