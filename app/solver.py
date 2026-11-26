"""Kubernetes 本地盘上线预检：PV / PVC 分配求解器。

纯标准库实现，便于脱离 Web 层做单元测试和命令行试算。
规则：
  * PV 必须匹配 PVC 声明的 StorageClass，且容量 >= 声明容量；
  * 每块 PV 最多分配给一项 PVC；
  * 同一 Pod 的所有卷必须位于同一个候选节点；
  * 目标是同时启动尽可能多的 Pod。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_PVS = 16
MAX_PODS = 6

ClaimKey = Tuple[str, str]  # (pod_name, claim_name)


class ValidationError(Exception):
    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class PV:
    name: str
    node: str
    storage_class: str
    capacity_gi: int


@dataclass(frozen=True)
class Claim:
    name: str
    storage_class: str
    capacity_gi: int


@dataclass(frozen=True)
class Pod:
    name: str
    candidate_nodes: Tuple[str, ...]
    claims: Tuple[Claim, ...]


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

_TOP_LEVEL_KEYS = {"persistent_volumes", "pods"}
_PV_KEYS = {"name", "node", "storage_class", "capacity_gi"}
_POD_KEYS = {"name", "candidate_nodes", "claims"}
_CLAIM_KEYS = {"name", "storage_class", "capacity_gi"}

_CAPACITY_RE = re.compile(r"^\s*(\d+)\s*(?:Gi)?\s*$")


def _non_empty_str(value: Any, path: str, errors: List[str]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path} 必须是非空字符串")
        return None
    return value.strip()


def _capacity(value: Any, path: str, errors: List[str]) -> Optional[int]:
    if isinstance(value, bool):
        errors.append(f"{path} 必须是正整数（单位 Gi）")
        return None
    if isinstance(value, int):
        if value <= 0:
            errors.append(f"{path} 必须大于 0")
            return None
        return value
    if isinstance(value, str):
        match = _CAPACITY_RE.fullmatch(value)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    errors.append(f"{path} 必须是正整数（单位 Gi），例如 100 或 \"100Gi\"")
    return None


def _check_mapping(value: Any, path: str, allowed: set, errors: List[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{path} 必须是 JSON 对象")
        return False
    unknown = sorted(set(value) - allowed)
    for key in unknown:
        errors.append(f"{path}.{key} 不是支持的字段（支持：{', '.join(sorted(allowed))}）")
    return True


def parse_payload(payload: Any) -> Tuple[List[PV], List[Pod]]:
    """把原始 JSON 解析为 PV / Pod，失败时汇总所有错误后抛出 ValidationError。"""
    errors: List[str] = []
    if not _check_mapping(payload, "$", _TOP_LEVEL_KEYS, errors):
        raise ValidationError(errors)

    raw_pvs = payload.get("persistent_volumes")
    raw_pods = payload.get("pods")
    if raw_pvs is None:
        errors.append("$.persistent_volumes 缺失（应为数组）")
        raw_pvs = []
    if raw_pods is None:
        errors.append("$.pods 缺失（应为数组）")
        raw_pods = []
    if not isinstance(raw_pvs, list):
        errors.append("$.persistent_volumes 必须是数组")
        raw_pvs = []
    if not isinstance(raw_pods, list):
        errors.append("$.pods 必须是数组")
        raw_pods = []

    if isinstance(raw_pvs, list) and len(raw_pvs) > MAX_PVS:
        errors.append(f"最多支持 {MAX_PVS} 块本地 PV，当前提供了 {len(raw_pvs)} 块")
    if isinstance(raw_pods, list) and len(raw_pods) > MAX_PODS:
        errors.append(f"最多支持 {MAX_PODS} 个待启动 Pod，当前提供了 {len(raw_pods)} 个")

    pvs: List[PV] = []
    seen_pv_names = set()
    for index, raw in enumerate(raw_pvs):
        path = f"$.persistent_volumes[{index}]"
        if not _check_mapping(raw, path, _PV_KEYS, errors):
            continue
        name = _non_empty_str(raw.get("name"), f"{path}.name", errors)
        node = _non_empty_str(raw.get("node"), f"{path}.node", errors)
        storage_class = _non_empty_str(
            raw.get("storage_class"), f"{path}.storage_class", errors
        )
        capacity = _capacity(raw.get("capacity_gi"), f"{path}.capacity_gi", errors)
        if name is not None:
            if name in seen_pv_names:
                errors.append(f"{path}.name 重复：PV 名称 {name!r} 出现多次")
            seen_pv_names.add(name)
        if name and node and storage_class and capacity is not None:
            pvs.append(PV(name, node, storage_class, capacity))

    pods: List[Pod] = []
    seen_pod_names = set()
    for pod_index, raw in enumerate(raw_pods):
        path = f"$.pods[{pod_index}]"
        if not _check_mapping(raw, path, _POD_KEYS, errors):
            continue
        name = _non_empty_str(raw.get("name"), f"{path}.name", errors)
        if name is not None:
            if name in seen_pod_names:
                errors.append(f"{path}.name 重复：Pod 名称 {name!r} 出现多次")
            seen_pod_names.add(name)

        raw_nodes = raw.get("candidate_nodes")
        nodes: List[str] = []
        if not isinstance(raw_nodes, list) or not raw_nodes:
            errors.append(f"{path}.candidate_nodes 必须是非空数组")
        else:
            for node_index, raw_node in enumerate(raw_nodes):
                node = _non_empty_str(
                    raw_node, f"{path}.candidate_nodes[{node_index}]", errors
                )
                if node is not None:
                    if node in nodes:
                        errors.append(f"{path}.candidate_nodes 重复：{node!r}")
                    else:
                        nodes.append(node)

        raw_claims = raw.get("claims")
        claims: List[Claim] = []
        seen_claim_names = set()
        if not isinstance(raw_claims, list) or not raw_claims:
            errors.append(f"{path}.claims 必须是非空数组")
        else:
            for claim_index, raw_claim in enumerate(raw_claims):
                claim_path = f"{path}.claims[{claim_index}]"
                if not _check_mapping(raw_claim, claim_path, _CLAIM_KEYS, errors):
                    continue
                claim_name = _non_empty_str(
                    raw_claim.get("name"), f"{claim_path}.name", errors
                )
                storage_class = _non_empty_str(
                    raw_claim.get("storage_class"),
                    f"{claim_path}.storage_class",
                    errors,
                )
                capacity = _capacity(
                    raw_claim.get("capacity_gi"),
                    f"{claim_path}.capacity_gi",
                    errors,
                )
                if claim_name is not None:
                    if claim_name in seen_claim_names:
                        errors.append(
                            f"{claim_path}.name 重复：同一 Pod 内 PVC 名称 {claim_name!r} 出现多次"
                        )
                    seen_claim_names.add(claim_name)
                if claim_name and storage_class and capacity is not None:
                    claims.append(Claim(claim_name, storage_class, capacity))

        if name and nodes and claims:
            pods.append(Pod(name, tuple(nodes), tuple(claims)))

    if errors:
        raise ValidationError(errors)
    return pvs, pods


# ---------------------------------------------------------------------------
# 分配求解
# ---------------------------------------------------------------------------


def _eligible(pv: PV, claim: Claim) -> bool:
    return (
        pv.storage_class == claim.storage_class
        and pv.capacity_gi >= claim.capacity_gi
    )


class _Matcher:
    """按节点做二分图增广路匹配。

    matches 记录 pv_name -> claim_key；claim_nodes 记录每个已放置的卷组
    （此时恰好是一个 Pod）被放在哪个节点。增广时沿着“卷 -> 占用它的 Pod
    所在节点”继续搜索，保证同一 Pod 的卷始终落在同一节点。
    """

    def __init__(self, pvs: List[PV], claims_by_key: Dict[ClaimKey, Claim]):
        self.pools: Dict[str, List[PV]] = {}
        for pv in sorted(pvs, key=lambda item: (item.node, item.name)):
            self.pools.setdefault(pv.node, []).append(pv)
        self.claims_by_key = claims_by_key

    def _augment(
        self,
        claim_key: ClaimKey,
        node_of_claim: Dict[ClaimKey, str],
        node: str,
        matches: Dict[str, ClaimKey],
        visited: set,
    ) -> bool:
        claim = self.claims_by_key[claim_key]
        for pv in self.pools.get(node, ()):
            if pv.name in visited or not _eligible(pv, claim):
                continue
            visited.add(pv.name)
            holder = matches.get(pv.name)
            if holder is None or self._augment(
                holder, node_of_claim, node_of_claim[holder], matches, visited
            ):
                matches[pv.name] = claim_key
                return True
        return False

    def try_place(
        self,
        claims: Iterable[Tuple[ClaimKey, Claim]],
        node: str,
        base_matches: Dict[str, ClaimKey],
        base_nodes: Dict[ClaimKey, str],
    ) -> Optional[Tuple[Dict[str, ClaimKey], Dict[ClaimKey, str]]]:
        """在现有匹配的副本上把一组卷放到同一个节点；全部成功才返回新状态。"""
        matches = dict(base_matches)
        node_of_claim = dict(base_nodes)
        items = list(claims)
        # 先放可选 PV 最少的卷，减少无效回溯；输出顺序不受影响。
        ordered = sorted(
            items,
            key=lambda item: (
                sum(1 for pv in self.pools.get(node, ()) if _eligible(pv, item[1])),
                item[0],
            ),
        )
        for claim_key, _claim in ordered:
            node_of_claim[claim_key] = node
            if not self._augment(claim_key, node_of_claim, node, matches, set()):
                return None
        return matches, node_of_claim

    def fits_available(
        self,
        claims: Iterable[Tuple[ClaimKey, Claim]],
        node: str,
        available: set,
    ) -> bool:
        """仅用 available 中的 PV，判断一组卷能否同节点放下（用于解释原因）。

        同样用增广路做精确判断，避免贪心误判为“放不下”。
        """
        pools = [pv for pv in self.pools.get(node, ()) if pv.name in available]
        matches: Dict[str, ClaimKey] = {}
        for claim_key, _claim in claims:
            if not self._augment_in(claim_key, pools, matches, set()):
                return False
        return True

    def _augment_in(
        self,
        claim_key: ClaimKey,
        pools: List[PV],
        matches: Dict[str, ClaimKey],
        visited: set,
    ) -> bool:
        claim = self.claims_by_key[claim_key]
        for pv in pools:
            if pv.name in visited or not _eligible(pv, claim):
                continue
            visited.add(pv.name)
            holder = matches.get(pv.name)
            if holder is None or self._augment_in(holder, pools, matches, visited):
                matches[pv.name] = claim_key
                return True
        return False


def _solve(pvs: List[PV], pods: List[Pod]) -> Dict[str, Any]:
    claims_by_key: Dict[ClaimKey, Claim] = {}
    pod_claim_items: Dict[str, List[Tuple[ClaimKey, Claim]]] = {}
    for pod in pods:
        items = []
        for claim in pod.claims:
            key = (pod.name, claim.name)
            claims_by_key[key] = claim
            items.append((key, claim))
        pod_claim_items[pod.name] = items

    matcher = _Matcher(pvs, claims_by_key)

    # 单个 Pod 在哪些节点可独立放下（不考虑其他 Pod 的占用）。
    individually_feasible: Dict[str, List[str]] = {}
    for pod in pods:
        nodes = []
        for node in sorted(pod.candidate_nodes):
            placed = matcher.try_place(pod_claim_items[pod.name], node, {}, {})
            if placed is not None:
                nodes.append(node)
        individually_feasible[pod.name] = nodes

    best_count = 0
    best_nodes: Dict[str, str] = {}
    best_matches: Dict[str, ClaimKey] = {}

    def dfs(
        index: int,
        matches: Dict[str, ClaimKey],
        node_of_claim: Dict[ClaimKey, str],
        chosen: Dict[str, str],
    ) -> None:
        nonlocal best_count, best_nodes, best_matches
        if index == len(pods):
            if len(chosen) > best_count:
                best_count = len(chosen)
                best_nodes = dict(chosen)
                best_matches = dict(matches)
            return
        if len(chosen) + (len(pods) - index) <= best_count:
            return

        pod = pods[index]
        for node in sorted(individually_feasible[pod.name]):
            placed = matcher.try_place(
                pod_claim_items[pod.name], node, matches, node_of_claim
            )
            if placed is None:
                continue
            new_matches, new_node_of_claim = placed
            chosen[pod.name] = node
            dfs(index + 1, new_matches, new_node_of_claim, chosen)
            del chosen[pod.name]

        # 该 Pod 本次不启动。
        dfs(index + 1, matches, node_of_claim, chosen)

    dfs(0, {}, {}, {})

    assigned_by_pod: Dict[str, Dict[str, str]] = {
        pod.name: {} for pod in pods
    }
    pv_holder: Dict[str, ClaimKey] = {}
    for pv_name, claim_key in best_matches.items():
        assigned_by_pod[claim_key[0]][claim_key[1]] = pv_name
        pv_holder[pv_name] = claim_key

    pv_details = []
    for pv in pvs:
        holder = pv_holder.get(pv.name)
        pv_details.append(
            {
                "name": pv.name,
                "node": pv.node,
                "storage_class": pv.storage_class,
                "capacity_gi": pv.capacity_gi,
                "in_use": holder is not None,
                "used_by": (
                    {"pod": holder[0], "claim": holder[1]} if holder else None
                ),
            }
        )

    scheduled_pods = []
    unscheduled_pods = []
    for pod in pods:
        individually = individually_feasible[pod.name]
        if pod.name in best_nodes:
            assignments = [
                {
                    "claim": claim.name,
                    "storage_class": claim.storage_class,
                    "capacity_gi": claim.capacity_gi,
                    "pv": assigned_by_pod[pod.name][claim.name],
                }
                for claim in pod.claims
            ]
            scheduled_pods.append(
                {
                    "name": pod.name,
                    "candidate_nodes": list(pod.candidate_nodes),
                    "node": best_nodes[pod.name],
                    "claims": [
                        {
                            "name": claim.name,
                            "storage_class": claim.storage_class,
                            "capacity_gi": claim.capacity_gi,
                        }
                        for claim in pod.claims
                    ],
                    "assignments": assignments,
                    "scheduled": True,
                    "reason": None,
                    "blocking": [],
                }
            )
            continue

        reason, blocking = _explain_unscheduled(
            pod,
            pod_claim_items[pod.name],
            individually,
            matcher,
            best_matches,
            pv_holder,
        )
        unscheduled_pods.append(
            {
                "name": pod.name,
                "candidate_nodes": list(pod.candidate_nodes),
                "node": None,
                "claims": [
                    {
                        "name": claim.name,
                        "storage_class": claim.storage_class,
                        "capacity_gi": claim.capacity_gi,
                    }
                    for claim in pod.claims
                ],
                "assignments": [],
                "scheduled": False,
                "reason": reason,
                "blocking": blocking,
            }
        )

    unassigned_pvs = [pv for pv in pvs if pv.name not in pv_holder]
    warnings = []
    if unassigned_pvs:
        warnings.append(
            "未参与分配的 PV："
            + "、".join(pv.name for pv in unassigned_pvs)
            + "（可能因 StorageClass/容量不匹配，或当前最优组合未用到）"
        )

    # 保持输入顺序返回 Pod。
    pod_details = []
    scheduled_by_name = {item["name"]: item for item in scheduled_pods}
    unscheduled_by_name = {item["name"]: item for item in unscheduled_pods}
    for pod in pods:
        pod_details.append(
            scheduled_by_name.get(pod.name) or unscheduled_by_name[pod.name]
        )

    summary = {
        "pv_count": len(pvs),
        "pod_count": len(pods),
        "scheduled_count": len(scheduled_pods),
        "unscheduled_count": len(unscheduled_pods),
        "claim_count": sum(len(pod.claims) for pod in pods),
        "assigned_claim_count": len(best_matches),
    }
    return {
        "limits": {"max_pvs": MAX_PVS, "max_pods": MAX_PODS},
        "summary": summary,
        "pvs": pv_details,
        "pods": pod_details,
        "scheduled": [item["name"] for item in scheduled_pods],
        "unscheduled": [item["name"] for item in unscheduled_pods],
        "warnings": warnings,
    }


def _explain_unscheduled(
    pod: Pod,
    claim_items: List[Tuple[ClaimKey, Claim]],
    individually_feasible: List[str],
    matcher: _Matcher,
    best_matches: Dict[str, ClaimKey],
    pv_holder: Dict[str, ClaimKey],
) -> Tuple[str, List[Dict[str, str]]]:
    missing = []
    for claim in pod.claims:
        has_any = any(
            _eligible(pv, claim)
            for node in pod.candidate_nodes
            for pv in matcher.pools.get(node, ())
        )
        if not has_any:
            missing.append(claim)
    if missing:
        detail = "；".join(
            f"PVC {claim.name}（{claim.storage_class}，{claim.capacity_gi}Gi）"
            for claim in missing
        )
        return (
            f"候选节点上没有满足要求的 PV：{detail}",
            [],
        )

    if not individually_feasible:
        return (
            "同一 Pod 的所有卷无法落在同一个候选节点（各卷可用节点不一致或节点内 PV 数量不足）",
            [],
        )

    free_pv_names = {
        pv.name for pvs in matcher.pools.values() for pv in pvs
    } - set(best_matches)
    if any(
        matcher.fits_available(claim_items, node, free_pv_names)
        for node in pod.candidate_nodes
    ):
        return (
            "存在可行节点，但该组合会减少同时启动的 Pod 总数，因此本 Pod 未被选中",
            [],
        )

    blocking = []
    seen = set()
    for node in sorted(pod.candidate_nodes):
        for claim in pod.claims:
            for pv in matcher.pools.get(node, ()):
                if not _eligible(pv, claim) or pv.name not in pv_holder:
                    continue
                holder_pod, holder_claim = pv_holder[pv.name]
                key = (pv.name, holder_pod, holder_claim)
                if key in seen:
                    continue
                seen.add(key)
                blocking.append(
                    {
                        "pv": pv.name,
                        "pod": holder_pod,
                        "claim": holder_claim,
                    }
                )
    if blocking:
        shown = "、".join(
            f"{item['pv']}（已分给 {item['pod']}/{item['claim']}）"
            for item in blocking
        )
        return (
            f"候选节点剩余 PV 无法同时满足全部卷，争用同一块盘：{shown}",
            blocking,
        )
    return (
        "候选节点剩余 PV 无法同时满足全部卷（容量或 StorageClass 不满足）",
        [],
    )


def plan(payload: Any) -> Dict[str, Any]:
    pvs, pods = parse_payload(payload)
    return _solve(pvs, pods)


def plan_json_text(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError([f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"])
    return plan(payload)
