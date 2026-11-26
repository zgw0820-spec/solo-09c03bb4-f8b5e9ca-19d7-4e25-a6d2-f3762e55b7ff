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

from app.solver import (  # noqa: E402
    MAX_PODS,
    MAX_PVS,
    ValidationError,
    normalize_options,
    plan,
)
from bruteforce import max_schedulable, valid_solution  # noqa: E402


class FakeClock:
    """第 1 次调用（求解器记录 start）返回 0；之后第 fail_on_call 次取钟触发超时。"""

    def __init__(self, fail_on_call: int):
        self.calls = 0
        self.fail_on_call = fail_on_call

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 1:
            return 0.0
        return 2000.0 if (self.calls - 1) >= self.fail_on_call else 0.0


def load_sample() -> dict:
    return json.loads((ROOT / "app" / "samples" / "contention.json").read_text())


def bulky_payload() -> dict:
    # 贪心陷阱：bulky 占两块盘，放掉它可启动 3 个 tiny；暴力最优为 3。
    return {
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


class TimeLimitTests(unittest.TestCase):
    """时限停止、上下界与发布判定。"""

    def test_no_time_limit_runs_exactly_and_bounds_coincide(self):
        result = plan(load_sample())
        search = result["search"]
        self.assertFalse(search["timed_out"])
        self.assertTrue(search["optimal"])
        self.assertEqual(search["lower_bound"], search["upper_bound"])
        self.assertEqual(search["lower_bound"], 5)
        # 精确求解时没分到卷的 Pod 仍是「未排上」。
        self.assertTrue(
            all(p["status"] == "unscheduled" for p in result["pods"] if not p["scheduled"])
        )

    def test_timeout_marks_not_optimal_and_unconfirmed(self):
        result = plan(bulky_payload(), time_limit_ms=1000, clock=FakeClock(1))
        search = result["search"]
        self.assertTrue(search["timed_out"])
        self.assertFalse(search["optimal"])
        self.assertLessEqual(search["lower_bound"], search["upper_bound"])
        # 未证最优时，没分到卷的 Pod 一律标为待确认。
        self.assertTrue(
            all(p["status"] == "unconfirmed" for p in result["pods"] if not p["scheduled"])
        )
        self.assertGreaterEqual(result["summary"]["unconfirmed_count"], 1)
        # 待确认原因要说明是时限停止、尚未证明排不上。
        unconfirmed = next(p for p in result["pods"] if p["status"] == "unconfirmed")
        self.assertIn("时限", unconfirmed["reason"])

    def test_upper_bound_always_dominates_true_optimum(self):
        payload = bulky_payload()
        optimum = max_schedulable(payload)
        saw_timeout = False
        for trigger in range(1, 200):
            result = plan(payload, time_limit_ms=1000, clock=FakeClock(trigger))
            search = result["search"]
            self.assertIsNone(valid_solution(payload, result), trigger)
            self.assertLessEqual(search["lower_bound"], optimum, trigger)
            self.assertLessEqual(optimum, search["upper_bound"], (trigger, search))
            self.assertEqual(search["optimal"], not search["timed_out"])
            saw_timeout = saw_timeout or search["timed_out"]
        self.assertTrue(saw_timeout)

    def test_timeout_upper_bound_deducts_skipped_pods(self):
        # 根节点立即超时：上界是全部 Pod 数（4），下界是贪心初解。
        result = plan(bulky_payload(), time_limit_ms=1000, clock=FakeClock(1))
        self.assertEqual(result["search"]["upper_bound"], 4)
        # 深入搜索后上界应能降到真实最优 3。
        lowered = False
        for trigger in range(2, 200):
            r = plan(bulky_payload(), time_limit_ms=1000, clock=FakeClock(trigger))
            if r["search"]["timed_out"] and r["search"]["upper_bound"] == 3:
                lowered = True
                break
        self.assertTrue(lowered)

    def test_timeout_always_returns_a_valid_assignment(self):
        # 即便第一次 DFS 检查就超时，贪心初解也保证手上有合法分配。
        for trigger in range(1, 30):
            result = plan(bulky_payload(), time_limit_ms=1000, clock=FakeClock(trigger))
            self.assertIsNone(valid_solution(bulky_payload(), result))
            self.assertGreaterEqual(result["search"]["lower_bound"], 1)


class ReleaseDecisionTests(unittest.TestCase):
    def test_without_min_pods_no_decision(self):
        result = plan(load_sample())
        self.assertIsNone(result["release"]["min_pods"])
        self.assertEqual(result["release"]["status"], "not_requested")

    def test_go_when_current_assignment_meets_minimum(self):
        result = plan(load_sample(), min_pods=5)
        self.assertEqual(result["release"]["status"], "go")
        self.assertIn("可上线", result["release"]["status_text"])
        # 未证最优但下界已够数，同样判可上线。
        early = plan(bulky_payload(), min_pods=2, time_limit_ms=1000, clock=FakeClock(1))
        self.assertTrue(early["search"]["timed_out"])
        self.assertEqual(early["release"]["status"], "go")

    def test_postpone_when_upper_bound_below_minimum(self):
        result = plan(load_sample(), min_pods=6)
        self.assertEqual(result["release"]["status"], "postpone")
        self.assertIn("推迟", result["release"]["status_text"])

    def test_review_when_lower_below_minimum_but_upper_allows(self):
        seen = False
        for trigger in range(1, 200):
            result = plan(
                bulky_payload(),
                min_pods=3,
                time_limit_ms=1000,
                clock=FakeClock(trigger),
            )
            if result["release"]["status"] == "review":
                search = result["search"]
                self.assertLess(search["lower_bound"], 3)
                self.assertGreaterEqual(search["upper_bound"], 3)
                self.assertIn("待复核", result["release"]["status_text"])
                seen = True
                break
        self.assertTrue(seen)

    def test_min_pods_zero_on_empty_input(self):
        result = plan({"persistent_volumes": [], "pods": []}, min_pods=0)
        self.assertEqual(result["release"]["status"], "go")
        self.assertTrue(result["search"]["optimal"])


class OptionValidationTests(unittest.TestCase):
    def assert_rejected(self, kwargs, fragment):
        with self.assertRaises(ValidationError) as ctx:
            plan(load_sample(), **kwargs)
        self.assertTrue(
            any(fragment in message for message in ctx.exception.errors),
            ctx.exception.errors,
        )

    def test_accepts_none_and_numeric_strings(self):
        min_pods, time_limit = normalize_options("5", "200")
        self.assertEqual((min_pods, time_limit), (5, 200))
        min_pods, time_limit = normalize_options("  ", None)
        self.assertIsNone(min_pods)
        self.assertIsNone(time_limit)

    def test_rejects_bad_options(self):
        self.assert_rejected({"min_pods": -1}, "非负整数")
        self.assert_rejected({"min_pods": MAX_PODS + 1}, "不能超过上限")
        self.assert_rejected({"time_limit_ms": 0}, "大于 0")
        self.assert_rejected({"time_limit_ms": -5}, "大于 0")
        self.assert_rejected({"time_limit_ms": "abc"}, "整数")
        self.assert_rejected({"time_limit_ms": 1.5}, "整数")
        self.assert_rejected({"min_pods": True}, "整数")

    def test_string_zero_time_limit_rejected_with_int_message(self):
        with self.assertRaises(ValidationError) as ctx:
            normalize_options(None, "0")
        self.assertIn("大于 0", ctx.exception.errors[0])


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
            # 同一实例在多个超时触发点上：上界必须夹住暴力最优，解始终合法。
            optimum = max_schedulable(payload)
            for trigger in (1, 2, 4, 8):
                stopped = plan(
                    payload, time_limit_ms=1000, clock=FakeClock(trigger)
                )
                search = stopped["search"]
                self.assertIsNone(valid_solution(payload, stopped), payload)
                self.assertLessEqual(search["lower_bound"], optimum)
                self.assertLessEqual(optimum, search["upper_bound"], search)


if __name__ == "__main__":
    unittest.main(verbosity=2)
