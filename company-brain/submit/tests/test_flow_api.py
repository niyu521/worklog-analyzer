import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class FlowApiTests(unittest.TestCase):
    def setUp(self):
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def test_flow_type_list_returns_versioned_payload(self):
        payload = {
            "schema_version": "1.0",
            "flow_types": [{"flow_type_id": "invoice_creation"}],
        }
        with patch.object(server, "list_flow_types", return_value=payload, create=True):
            response = self.client.get("/flow-types")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), payload)

    def test_flow_type_detail_passes_pagination(self):
        payload = {
            "schema_version": "1.0",
            "flow_type": {"flow_type_id": "invoice_creation"},
            "instances": [],
        }
        with patch.object(
            server, "get_flow_type", return_value=payload, create=True
        ) as get_flow_type:
            response = self.client.get(
                "/flow-types/invoice_creation?limit=25&offset=50"
            )
        self.assertEqual(response.status_code, 200)
        get_flow_type.assert_called_once_with("invoice_creation", 25, 50)

    def test_unknown_flow_type_returns_404(self):
        with patch.object(server, "get_flow_type", return_value=None, create=True):
            response = self.client.get("/flow-types/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "flow type not found")

    def test_invalid_pagination_returns_400(self):
        invalid_urls = [
            "/flow-types/invoice_creation?limit=0",
            "/flow-types/invoice_creation?limit=201",
            "/flow-types/invoice_creation?offset=-1",
            "/flow-types/invoice_creation?limit=nope",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 400)
                self.assertIn("pagination", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
