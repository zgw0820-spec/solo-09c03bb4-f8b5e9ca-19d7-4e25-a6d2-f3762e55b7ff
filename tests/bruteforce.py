"""与求解器实现无关的暴力枚举，用于对拍验证最优解。

只覆盖小规模输入；枚举“哪些 Pod 启动 + 每个 Pod 选哪个节点”的全部组合，
对每个组合用回溯做 PV 分配，检查是否可行。
"""

from __future__ import annotations

from itertools import product
from typing import Any, Dict, List, Optional


def _pv_fits(pv: Dict[str, Any], claim: Dict[str, Any]) -> bool:
    return (
        pv["storage_class"] == claim["storage_class"]
        and pv["capacity_gi"] >= claim["capacity_gi"]
    )


def _can_assign(pvs, claims, used: set, index: int) -> bool:
    if index == len(claims):
        return True
    claim = claims[index]
    for pv in pvs:
        if pv["name"] in used or not _pv_fits(pv, claim):
            continue
        used.add(pv["name"])
        if _can_assign(pvs, claims, used, index + 1):
            return True
        used.remove(pv["name"])
    return False


def max_schedulable(payload: Dict[str, Any]) -> int:
    pvs = payload["persistent_volumes"]
    pods = payload["pods"]
    best = 0
    # 每个 Pod：None（不启动）或某个候选节点
    for choices in product(*[[None] + list(pod["candidate_nodes"]) for pod in pods]):
        selected = [(pod, node) for pod, node in zip(pods, choices) if node is not None]
        if len(selected) <= best:
            continue
        used: set = set()
        ok = True
        for pod, node in selected:
            node_pvs = [pv for pv in pvs if pv["node"] == node]
            if not _can_assign(node_pvs, pod["claims"], used, 0):
                ok = False
                break
        if ok:
            best = len(selected)
    return best


def valid_solution(payload: Dict[str, Any], result: Dict[str, Any]) -> Optional[str]:
    """校验求解器输出是否满足全部约束；合法返回 None，否则返回错误说明。"""
    pvs = {pv["name"]: pv for pv in payload["persistent_volumes"]}
    pods = {pod["name"]: pod for pod in payload["pods"]}
    used: Dict[str, str] = {}
    for pod_result in result["pods"]:
        pod = pods[pod_result["name"]]
        if not pod_result["scheduled"]:
            if pod_result["assignments"]:
                return f"{pod['name']} 未启动却带有分配"
            continue
        if pod_result["node"] not in pod["candidate_nodes"]:
            return f"{pod['name']} 节点不在候选列表"
        if len(pod_result["assignments"]) != len(pod["claims"]):
            return f"{pod['name']} PVC 未全部分配"
        assigned_claims = {item["claim"] for item in pod_result["assignments"]}
        if assigned_claims != {claim["name"] for claim in pod["claims"]}:
            return f"{pod['name']} 分配的 PVC 集合不一致"
        for item in pod_result["assignments"]:
            pv = pvs[item["pv"]]
            if pv["node"] != pod_result["node"]:
                return f"{pod['name']} 的卷 {item['pv']} 不在同一节点"
            claim = next(c for c in pod["claims"] if c["name"] == item["claim"])
            if not _pv_fits(pv, claim):
                return f"{pod['name']}/{item['claim']} 的 PV 不满足 StorageClass 或容量"
            if item["pv"] in used:
                return f"PV {item['pv']} 被重复分配"
            used[item["pv"]] = item["claim"]
    return None
