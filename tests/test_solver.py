"""求解器单元测试：约束、最优性（对拍暴力枚举）、输入校验。"""

from __future__ import annotations

import json
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.solver import MAX_PODS, MAX_PVS, ValidationError, plan  # noqa: E402
from bruteforce import max_schedulable, valid_solution  # noqa: E402


def load_sample() -> dict:
    return json.loads((ROOT / "app" / "samples" / "contention.json").read_text())


class SampleTests(unittest.TestCase):
    def test_contention_sample_is_valid_and_optimal(self):
        payload = load_sample()
        result = plan(payload)
        self.assertIsNone(valid_solution(payload, result))
        self.assertEqual(
            result["summary"]["scheduled_count"], max_schedulable(payload)
        )
        self.assertEqual(result["summary"]["scheduled_count"], 5)
        self.assertEqual(result["unscheduled"], ["redis-0"])

    def test_unscheduled_reason_mentions_contended_pv(self):
        result = plan(load_sample())
        redis = next(p for p in result["pods"] if p["name"] == "redis-0")
        self.assertFalse(redis["scheduled"])
        self.assertTrue(redis["blocking"])
        contended = {item["pv"] for item in redis["blocking"]}
        self.assertTrue(
            contended & {"pv-a-fast-100", "pv-b-fast-100", "pv-b-fast-90"},
            contended,
        )
        self.assertIn("争用同一块盘", redis["reason"])


class ConstraintTests(unittest.TestCase):
    def test_two_pods_share_one_disk_only_one_wins(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": 100}
            ],
            "pods": [
                {
                    "name": "p1",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 10}],
                },
                {
                    "name": "p2",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 10}],
                },
            ],
        }
        result = plan(payload)
        self.assertEqual(result["summary"]["scheduled_count"], 1)
        self.assertEqual(len(result["unscheduled"]), 1)

    def test_same_pod_must_use_one_node(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-a", "node": "n1", "storage_class": "fast", "capacity_gi": 100},
                {"name": "pv-b", "node": "n2", "storage_class": "fast", "capacity_gi": 100},
            ],
            "pods": [
                {
                    "name": "p1",
                    "candidate_nodes": ["n1", "n2"],
                    "claims": [
                        {"name": "c1", "storage_class": "fast", "capacity_gi": 10},
                        {"name": "c2", "storage_class": "fast", "capacity_gi": 10},
                    ],
                }
            ],
        }
        result = plan(payload)
        self.assertEqual(result["summary"]["scheduled_count"], 0)
        self.assertIn("同一个候选节点", result["pods"][0]["reason"])

    def test_capacity_and_storage_class_filtering(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-small", "node": "n1", "storage_class": "fast", "capacity_gi": 10},
                {"name": "pv-wrong-class", "node": "n1", "storage_class": "slow", "capacity_gi": 100},
            ],
            "pods": [
                {
                    "name": "p1",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 50}],
                }
            ],
        }
        result = plan(payload)
        self.assertEqual(result["summary"]["scheduled_count"], 0)
        self.assertIn("没有满足要求的 PV", result["pods"][0]["reason"])

    def test_greedy_trap_requires_reassignment(self):
        # p1 只能放 n1 且必须用 pv-big；贪心若先把 pv-big 给了 p2 就会漏解。
        payload = {
            "persistent_volumes": [
                {"name": "pv-big", "node": "n1", "storage_class": "fast", "capacity_gi": 100},
                {"name": "pv-small", "node": "n1", "storage_class": "fast", "capacity_gi": 20},
            ],
            "pods": [
                {
                    "name": "p1",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 80}],
                },
                {
                    "name": "p2",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 10}],
                },
            ],
        }
        result = plan(payload)
        self.assertEqual(result["summary"]["scheduled_count"], 2)
        self.assertIsNone(valid_solution(payload, result))

    def test_tradeoff_prefers_more_pods(self):
        # bulky 一次吃掉两块盘，只能启动 1~2 个 Pod；放掉它可启动 3 个 tiny。
        payload = {
            "persistent_volumes": [
                {"name": "pv-large", "node": "n1", "storage_class": "fast", "capacity_gi": 100},
                {"name": "pv-tiny-a", "node": "n1", "storage_class": "fast", "capacity_gi": 10},
                {"name": "pv-tiny-b", "node": "n1", "storage_class": "fast", "capacity_gi": 10},
            ],
            "pods": [
                {
                    "name": "bulky",
                    "candidate_nodes": ["n1"],
                    "claims": [
                        {"name": "data", "storage_class": "fast", "capacity_gi": 90},
                        {"name": "wal", "storage_class": "fast", "capacity_gi": 10},
                    ],
                },
                *[
                    {
                        "name": f"tiny-{i}",
                        "candidate_nodes": ["n1"],
                        "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 10}],
                    }
                    for i in range(1, 4)
                ],
            ],
        }
        result = plan(payload)
        self.assertNotIn("bulky", result["scheduled"])
        self.assertEqual(result["summary"]["scheduled_count"], 3)
        self.assertEqual(result["summary"]["scheduled_count"], max_schedulable(payload))


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, payload, fragment):
        with self.assertRaises(ValidationError) as ctx:
            plan(payload)
        self.assertTrue(
            any(fragment in message for message in ctx.exception.errors),
            ctx.exception.errors,
        )

    def test_too_many_pvs(self):
        payload = {
            "persistent_volumes": [
                {"name": f"pv-{i}", "node": "n1", "storage_class": "fast", "capacity_gi": 1}
                for i in range(MAX_PVS + 1)
            ],
            "pods": [
                {
                    "name": "p",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 1}],
                }
            ],
        }
        self.assert_invalid(payload, f"最多支持 {MAX_PVS} 块")

    def test_too_many_pods(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": 1}
            ],
            "pods": [
                {
                    "name": f"p{i}",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 1}],
                }
                for i in range(MAX_PODS + 1)
            ],
        }
        self.assert_invalid(payload, f"最多支持 {MAX_PODS} 个")

    def test_bad_capacity_and_unknown_field(self):
        payload = {
            "persistent_volumes": [
                {
                    "name": "pv-1",
                    "node": "n1",
                    "storage_class": "fast",
                    "capacity_gi": "abc",
                    "extra": 1,
                }
            ],
            "pods": [],
        }
        with self.assertRaises(ValidationError) as ctx:
            plan(payload)
        text = "\n".join(ctx.exception.errors)
        self.assertIn("capacity_gi", text)
        self.assertIn("extra", text)

    def test_duplicate_pv_name(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": 1},
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": 2},
            ],
            "pods": [],
        }
        self.assert_invalid(payload, "重复")

    def test_empty_candidates(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": 1}
            ],
            "pods": [
                {
                    "name": "p",
                    "candidate_nodes": [],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": 1}],
                }
            ],
        }
        self.assert_invalid(payload, "candidate_nodes")

    def test_capacity_accepts_gi_string(self):
        payload = {
            "persistent_volumes": [
                {"name": "pv-1", "node": "n1", "storage_class": "fast", "capacity_gi": "100Gi"}
            ],
            "pods": [
                {
                    "name": "p",
                    "candidate_nodes": ["n1"],
                    "claims": [{"name": "c", "storage_class": "fast", "capacity_gi": "50Gi"}],
                }
            ],
        }
        self.assertEqual(plan(payload)["summary"]["scheduled_count"], 1)


class RandomDifferentialTests(unittest.TestCase):
    """随机小规模输入上与暴力枚举对拍，验证最优性和约束合法性。"""

    def test_random_instances(self):
        rng = random.Random(20261125)
        classes = ["fast", "slow"]
        nodes = ["n1", "n2", "n3"]
        for case in range(60):
            pvs = []
            for i in range(rng.randint(1, 5)):
                pvs.append(
                    {
                        "name": f"pv-{i}",
                        "node": rng.choice(nodes),
                        "storage_class": rng.choice(classes),
                        "capacity_gi": rng.choice([10, 20, 50, 100]),
                    }
                )
            pods = []
            for p in range(rng.randint(1, 4)):
                claims = [
                    {
                        "name": f"c{j}",
                        "storage_class": rng.choice(classes),
                        "capacity_gi": rng.choice([10, 20, 50]),
                    }
                    for j in range(rng.randint(1, 2))
                ]
                pods.append(
                    {
                        "name": f"pod-{p}",
                        "candidate_nodes": rng.sample(nodes, rng.randint(1, 3)),
                        "claims": claims,
                    }
                )
            payload = {"persistent_volumes": pvs, "pods": pods}
            result = plan(payload)
            self.assertIsNone(
                valid_solution(payload, result), f"case {case}: {payload}"
            )
            self.assertEqual(
                result["summary"]["scheduled_count"],
                max_schedulable(payload),
                f"case {case}: {payload} -> {result['scheduled']}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
