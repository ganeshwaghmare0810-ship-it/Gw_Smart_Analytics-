import io
import sqlite3
import unittest

import numpy as np
import pandas as pd

import upload_storage


class TestDatasetFormats(unittest.TestCase):
    def test_text_formats_load_into_dataframes(self):
        examples = [
            ("sales.csv", b"date,amount\n2025-01-01,10\n", "amount", 10),
            ("sales.tsv", b"date\tamount\n2025-01-01\t11\n", "amount", 11),
            ("sales.txt", b"date|amount\n2025-01-01|12\n", "amount", 12),
            ("sales.json", b'[{\"date\":\"2025-01-01\",\"amount\":13}]', "amount", 13),
            ("sales.jsonl", b'{\"date\":\"2025-01-01\",\"amount\":14}\n', "amount", 14),
            ("sales.xml", b"<rows><row><date>2025-01-01</date><amount>15</amount></row></rows>", "amount", 15),
            ("sales.arff", b"@RELATION sales\n@ATTRIBUTE amount NUMERIC\n@DATA\n16\n", "amount", 16),
        ]
        for filename, raw, column, expected in examples:
            with self.subTest(filename=filename):
                frame = upload_storage.read_dataset(filename, raw)
                self.assertEqual(frame.iloc[0][column], expected)

    def test_binary_table_and_array_formats(self):
        frame = pd.DataFrame({"value": [7, 8]})
        parquet = io.BytesIO()
        frame.to_parquet(parquet, index=False)
        self.assertEqual(upload_storage.read_dataset("sales.parquet", parquet.getvalue()).value.tolist(), [7, 8])

        array = io.BytesIO()
        np.save(array, np.array([[1, 2], [3, 4]]), allow_pickle=False)
        self.assertEqual(upload_storage.read_dataset("sales.npy", array.getvalue()).shape, (2, 2))

        archive = io.BytesIO()
        np.savez(archive, first=np.array([5, 6]), second=np.array([9]))
        self.assertEqual(upload_storage.dataset_options("sales.npz", archive.getvalue()), ["first", "second"])
        self.assertEqual(
            upload_storage.read_dataset("sales.npz", archive.getvalue(), "second").iloc[0, 0], 9
        )

    def test_sqlite_database_and_restricted_sql_dump(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE sales (amount INTEGER)")
        connection.executemany("INSERT INTO sales VALUES (?)", [(20,), (21,)])
        database_bytes = connection.serialize()
        connection.close()

        self.assertEqual(upload_storage.dataset_options("sales.db", database_bytes), ["sales"])
        self.assertEqual(
            upload_storage.read_dataset("sales.db", database_bytes).amount.tolist(), [20, 21]
        )

        dump = b"PRAGMA foreign_keys=OFF; CREATE TABLE sales (amount INTEGER); INSERT INTO sales VALUES (22);"
        self.assertEqual(upload_storage.read_dataset("sales.sql", dump).amount.tolist(), [22])

    def test_multi_table_files_require_an_explicit_table(self):
        dump = b"CREATE TABLE alpha (value INTEGER); CREATE TABLE beta (value INTEGER);"
        with self.assertRaisesRegex(ValueError, "Select a table"):
            upload_storage.read_dataset("multi.sql", dump)
        self.assertEqual(upload_storage.read_dataset("multi.sql", dump, "beta").columns.tolist(), ["value"])

    def test_sql_dump_rejects_non_data_operations(self):
        with self.assertRaisesRegex(ValueError, "unsupported operations"):
            upload_storage.read_dataset("unsafe.sql", b"ATTACH DATABASE ':memory:' AS other;")

    def test_pickle_files_are_rejected_without_deserialization(self):
        self.assertNotIn("pkl", upload_storage.ALLOWED_EXTENSIONS)
        with self.assertRaisesRegex(ValueError, "arbitrary code"):
            upload_storage.read_dataset("model.pkl", b"not a pickle")


if __name__ == "__main__":
    unittest.main()
