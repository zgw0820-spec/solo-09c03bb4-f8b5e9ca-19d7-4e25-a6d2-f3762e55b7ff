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

    def test_plan_accepts_min_pods_and_time_limit_in_body(self):
        resp = self.client.post(
            "/api/plan",
            json={"json_text": self.sample_text, "min_pods": 5, "time_limit_ms": 5000},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["release"]["status"], "go")
        self.assertEqual(data["search"]["time_limit_ms"], 5000)
        self.assertEqual(data["search"]["min_pods"], 5)

    def test_plan_accepts_options_in_query(self):
        resp = self.client.post(
            "/api/plan?min_pods=6&time_limit_ms=3000",
            json={"json_text": self.sample_text},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["release"]["status"], "postpone")
        self.assertEqual(data["search"]["time_limit_ms"], 3000)

    def test_plan_rejects_invalid_options_with_chinese_422(self):
        resp = self.client.post(
            "/api/plan", json={"json_text": self.sample_text, "time_limit_ms": 0}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("大于 0", resp.json()["detail"][0])
        resp = self.client.post(
            "/api/plan?min_pods=99", json={"json_text": self.sample_text}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("上限", resp.json()["detail"][0])
        resp = self.client.post(
            "/api/plan?time_limit_ms=abc", json={"json_text": self.sample_text}
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("整数", resp.json()["detail"][0])

    def test_plan_json_strips_option_fields(self):
        payload = json.loads(self.sample_text)
        payload["min_pods"] = 5
        resp = self.client.post("/api/plan/json", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["release"]["status"], "go")

    def test_upload_accepts_query_and_form_options(self):
        resp = self.client.post(
            "/api/plan/upload?min_pods=6",
            files={"file": ("c.json", self.sample_text.encode(), "application/json")},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["release"]["status"], "postpone")
        resp = self.client.post(
            "/api/plan/upload",
            data={"time_limit_ms": "1000"},
            files={"file": ("c.json", self.sample_text.encode(), "application/json")},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["search"]["time_limit_ms"], 1000)

    def test_response_contains_search_release_and_status_fields(self):
        resp = self.client.post(
            "/api/plan", json={"json_text": self.sample_text, "min_pods": 5}
        )
        data = resp.json()
        for key in ("search", "release", "summary"):
            self.assertIn(key, data)
        for key in ("lower_bound", "upper_bound", "optimal", "timed_out", "elapsed_ms"):
            self.assertIn(key, data["search"])
        for pod in data["pods"]:
            self.assertIn("status", pod)
            self.assertIn("status_text", pod)

    def test_unknown_sample_404(self):
        self.assertEqual(self.client.get("/api/samples/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
