import os
import tempfile
import unittest
from pathlib import Path

import app


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def test_create_and_list_synthetic_request(self):
        request_id = app.create_demo_request()
        rows = app.list_requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], request_id)
        self.assertEqual(rows[0]["status"], "Новая")
        self.assertEqual(rows[0]["customer_name"], "Тестовый клиент")

    def test_consent_required(self):
        with self.assertRaises(ValueError):
            app.create_demo_request(consent=False)
        self.assertEqual(app.list_requests(), [])

    def test_service_type_validated(self):
        with self.assertRaises(ValueError):
            app.create_demo_request(service_type="Неизвестная услуга")

    def test_area_validated(self):
        with self.assertRaises(ValueError):
            app.create_demo_request(area_m2=0)

    def test_status_update(self):
        request_id = app.create_demo_request()
        self.assertTrue(app.update_status(request_id, "Подтверждена"))
        self.assertEqual(app.list_requests()[0]["status"], "Подтверждена")
        self.assertFalse(app.update_status(99999, "Подтверждена"))

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            app.update_status(1, "random")


if __name__ == "__main__":
    unittest.main()
