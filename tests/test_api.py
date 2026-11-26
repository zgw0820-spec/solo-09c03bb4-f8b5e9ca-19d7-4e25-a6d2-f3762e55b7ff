"""HTTP 层测试：页面上传、粘贴、样例接口和错误返回。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient

    from app.main import app

    HAVE_CLIENT = True
except Exception:  # pragma: no cover - 依赖缺失时跳过
    HAVE_CLIENT = False


@unittest.skipUnless(HAVE_CLIENT, "需要 fastapi/httpx 才能运行 HTTP 测试")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.sample_text = (ROOT / "app" / "samples" / "contention.json").read_text("utf-8")

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["limits"], {"max_pvs": 16, "max_pods": 6})

    def test_index_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Kubernetes 本地盘上线预检", resp.text)
        self.assertIn("/static/app.js", resp.text)

    def test_static_assets(self):
        for asset in ("/static/app.js", "/static/style.css"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)

    def test_sample_endpoint_matches_expected(self):
        resp = self.client.get("/api/sample")
        self.assertEqual(resp.status_code, 200)
        result = self.client.post(
            "/api/plan", json={"json_text": resp.json()["json_text"]}
        ).json()
        self.assertEqual(result["summary"]["scheduled_count"], 5)
        self.assertEqual(result["unscheduled"], ["redis-0"])

    def test_plan_from_text(self):
        resp = self.client.post("/api/plan", json={"json_text": self.sample_text})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["summary"]["scheduled_count"], 5
        )

    def test_plan_from_upload(self):
        resp = self.client.post(
            "/api/plan/upload",
            files={"file": ("contention.json", self.sample_text.encode(), "application/json")},
        )
        self.assertEqual(resp.status_code, 200)
        pod = next(p for p in resp.json()["pods"] if p["name"] == "redis-0")
        self.assertFalse(pod["scheduled"])

    def test_upload_missing_file(self):
        resp = self.client.post("/api/plan/upload", data={"other": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_upload_too_large(self):
        big = b"{}" + b" " * 1_000_001
        resp = self.client.post(
            "/api/plan/upload", files={"file": ("big.json", big, "application/json")}
        )
        self.assertEqual(resp.status_code, 413)

    def test_invalid_payload_returns_field_errors(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv", "node": "n", "storage_class": "s", "capacity_gi": -1}
            ],
            "pods": [],
        }
        resp = self.client.post("/api/plan/json", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertTrue(any("capacity_gi" in e for e in resp.json()["detail"]))

    def test_malformed_json(self):
        resp = self.client.post("/api/plan", json={"json_text": "{"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("JSON 解析失败", resp.json()["detail"][0])

    def test_unknown_sample_404(self):
        self.assertEqual(self.client.get("/api/samples/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
