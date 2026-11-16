"""双源关系引擎：把审批台账与表达式真实引用拆成两套关系并各自建图、求环。

关键边界（对应需求里的两条硬约束）：
1. 审批关系继续决定下线许可——有效审批边才进「现网图」，停用项只留证据；
   引擎不会因为表达式里出现了真实引用，就自动补一条审批关系放行。
2. 环检测按真实引用建图——未申报引用在审批图里缺边，环被藏起来；
   真实引用图把这些边补全后求环，再与审批图比对，露出「隐藏环」。

两张图都只在已注册指标之间连边；指向未注册指标的关系保留为证据，
但不进邻接表（叶子都不是注册指标，无法参与指标→指标的环）。
"""
from __future__ import annotations

from ... import config, manifest
from ..m4_graph import tarjan
from ..m7_retire import cycles

# 真实引用关系没有审批状态，用 observed 表示「在表达式里观测到」
OBSERVED = "observed"


def relation_id(batch_id: str, kind: str, src: str, dst: str) -> str:
    return manifest.digest_parts([batch_id, kind, src, dst])[:16]


def _ref_source_version(rule_version: str, batch_id: str, line_no: int) -> str:
    """真实引用的来源版本：规则版本 + 批次摘要 + DSL 行号，可回溯到具体定义。"""
    return f"{rule_version}/batch-{batch_id[:10]}/L{line_no}"


def build_relations(*, metrics: list[dict], approval_rows: list,
                    batch_id: str, rule_version: str,
                    input_digest: str) -> dict:
    names = {m["name"] for m in metrics}
    by_name = {m["name"]: m for m in metrics}

    relations: list[dict] = []
    diagnostics: list[dict] = []

    # ---- 真实引用关系（表达式 ref 实际血缘）----
    ref_pairs: dict[tuple[str, str], dict] = {}
    reference_adj: dict[str, list[str]] = {name: [] for name in names}
    for metric in sorted(metrics, key=lambda m: m["line_no"]):
        declared = set(metric["depends_declared"])
        for target in sorted(set(metric["metrics"])):
            known = target in names
            pair = (metric["name"], target)
            rel = {
                "relation_id": relation_id(batch_id, config.REL_REFERENCE, *pair),
                "kind": config.REL_REFERENCE,
                "src": metric["name"], "dst": target,
                "status": OBSERVED,
                "source_version": _ref_source_version(
                    rule_version, batch_id, metric["line_no"]),
                "line_no": metric["line_no"],
                "known_src": True, "known_dst": known,
                "evidence": {
                    "source": "dsl-expression",
                    "line_no": metric["line_no"],
                    "expr_text": metric.get("expr_text", ""),
                    "declared": target in declared,
                    "batch_id": batch_id, "rule_version": rule_version,
                    "input_digest": input_digest,
                },
            }
            relations.append(rel)
            ref_pairs[pair] = rel
            if known and target != metric["name"]:
                reference_adj[metric["name"]].append(target)
            elif known and target == metric["name"]:
                reference_adj[metric["name"]].append(target)  # 自环也要进图
    for name in reference_adj:
        reference_adj[name] = sorted(set(reference_adj[name]))

    # ---- 审批关系（审批台账）----
    appr_active: dict[tuple[str, str], dict] = {}
    appr_disabled: dict[tuple[str, str], dict] = {}
    appr_all: dict[tuple[str, str], dict] = {}
    approval_adj: dict[str, list[str]] = {name: [] for name in names}
    for row in approval_rows:
        pair = (row.metric, row.depends_on)
        if pair in appr_all:
            # 台账重复行只采信首条，留一条诊断，不新增关系（重试也不重复）
            diagnostics.append(_diag(
                "G_APPROVAL_DUPLICATE", metric=row.metric,
                detail=f"审批台账第 {row.line_no} 行与前面重复，只采信首条"))
            continue
        known_src = row.metric in names
        known_dst = row.depends_on in names
        rel = {
            "relation_id": relation_id(batch_id, config.REL_APPROVAL, *pair),
            "kind": config.REL_APPROVAL,
            "src": row.metric, "dst": row.depends_on,
            "status": row.status,
            "source_version": row.source_version,
            "line_no": row.line_no,
            "known_src": known_src, "known_dst": known_dst,
            "evidence": {
                "source": "approval-ledger",
                "line_no": row.line_no,
                "approver": row.approver,
                "approved_on": row.approved_on,
                "note": row.note,
                "ledger_status": row.status,
                "batch_id": batch_id, "rule_version": rule_version,
                "input_digest": input_digest,
            },
        }
        relations.append(rel)
        appr_all[pair] = rel
        if row.status == config.APPROVAL_ACTIVE:
            appr_active[pair] = rel
        else:
            appr_disabled[pair] = rel
        if (row.status == config.APPROVAL_ACTIVE and known_src and known_dst):
            approval_adj[row.metric].append(row.depends_on)
    for name in approval_adj:
        approval_adj[name] = sorted(set(approval_adj[name]))

    # ---- 双向对账：审批 vs 真实引用 ----
    for pair, rel in sorted(ref_pairs.items()):
        if not rel["known_dst"]:
            continue  # 未注册引用由 M4 的 G_REF_UNKNOWN 负责，这里不重复报
        if pair in appr_active:
            continue
        if pair in appr_disabled:
            diagnostics.append(_diag(
                "G_APPROVAL_DISABLED", metric=pair[0],
                detail=f"{pair[0]} → {pair[1]} 的审批已停用，但表达式仍真实引用"))
        else:
            diagnostics.append(_diag(
                "G_APPROVAL_MISSING", metric=pair[0],
                detail=f"{pair[0]} → {pair[1]} 真实引用存在，但没有有效审批依赖"))
    for pair, rel in sorted(appr_all.items()):
        if not (rel["known_src"] and rel["known_dst"]):
            diagnostics.append(_diag(
                "G_APPROVAL_UNKNOWN", metric=pair[0],
                detail=f"审批关系 {pair[0]} → {pair[1]} 指向未注册指标，不进现网图"))
            continue
        if pair not in ref_pairs:
            diagnostics.append(_diag(
                "G_APPROVAL_UNUSED", metric=pair[0],
                detail=f"审批关系 {pair[0]} → {pair[1]} 有效，但表达式没有真实引用"))

    # ---- 双图环检测 ----
    ordered_names = sorted(names)
    reference = _graph(ordered_names, reference_adj)
    approval = _graph(ordered_names, approval_adj)

    appr_ring_sets = {frozenset(r["members"]) for r in approval["rings"]}
    hidden_rings: list[dict] = []
    for ring in reference["rings"]:
        if frozenset(ring["members"]) not in appr_ring_sets:
            hidden = dict(ring)
            hidden["hidden"] = True
            hidden_rings.append(hidden)
            diagnostics.append(_diag(
                "G_REF_CYCLE_HIDDEN", metric=ring["head"],
                detail="真实引用环 " + " → ".join(ring["path"])
                       + " 在审批图里缺边，被未申报引用藏起来"))

    # 回填每条关系的对账标记，供页面分开呈现
    for rel in relations:
        pair = (rel["src"], rel["dst"])
        if rel["kind"] == config.REL_REFERENCE:
            rel["matched_approval"] = (
                "active" if pair in appr_active
                else "disabled" if pair in appr_disabled else "none")
        else:
            rel["matched_reference"] = pair in ref_pairs

    diagnostics.sort(key=lambda d: (d["rank"], d.get("metric") or "", d["code"]))
    return {
        "relations": relations,
        "diagnostics": diagnostics,
        "names": ordered_names,
        "by_name": by_name,
        "reference": {**reference, "adj": reference_adj},
        "approval": {**approval, "adj": approval_adj},
        "hidden_rings": hidden_rings,
        "summary": _summary(relations, reference, approval, hidden_rings),
    }


def _graph(nodes: list[str], adj: dict[str, list[str]]) -> dict:
    components = tarjan.strongly_connected(nodes, adj)
    comp_dicts = [{"members": c} for c in components]
    rings = cycles.minimal_rings(comp_dicts, adj)
    comp_rows = [{"comp_id": i, "size": len(c), "members": c,
                  "is_cycle": tarjan.is_cycle(c, adj),
                  "path": tarjan.cycle_path(c, adj) if tarjan.is_cycle(c, adj) else []}
                 for i, c in enumerate(components)]
    return {"components": comp_rows, "rings": rings}


def _summary(relations, reference, approval, hidden_rings) -> dict:
    ref = [r for r in relations if r["kind"] == config.REL_REFERENCE]
    appr = [r for r in relations if r["kind"] == config.REL_APPROVAL]
    return {
        "reference_edges": len(ref),
        "approval_edges": len(appr),
        "approval_active": sum(1 for r in appr if r["status"] == config.APPROVAL_ACTIVE),
        "approval_disabled": sum(1 for r in appr
                                 if r["status"] == config.APPROVAL_DISABLED),
        "reference_cycles": len(reference["rings"]),
        "approval_cycles": len(approval["rings"]),
        "hidden_cycles": len(hidden_rings),
        "reference_components": len(reference["components"]),
        "approval_components": len(approval["components"]),
    }


def _diag(code: str, *, metric: str | None = None, detail: str = "") -> dict:
    from ... import reasons
    reason = reasons.get(code)
    try:
        rank = config.DIAG_ORDER.index(code)
    except ValueError:
        rank = len(config.DIAG_ORDER)
    return {"code": code, "level": reason.level, "stage": reason.stage,
            "line_no": None, "metric": metric, "field": None,
            "raw_value": None, "detail": detail or reason.title, "rank": rank}
