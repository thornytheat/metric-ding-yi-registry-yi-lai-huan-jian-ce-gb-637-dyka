"""审批口径的下线判断：迁移次序与阻断项都建立在有效审批边上。

与 M7（按真实引用边）并存且互不替代：
- 迁移次序、环阻断只看有效审批关系——审批决定下线许可；
- 真实引用里存在、却没有有效审批支撑的依赖，单列「未授权阻断」，
  绝不因为表达式里真实引用了就自动放行；
- 已停用审批项对应仍在引用的依赖，单列「审批停用阻断」，下线许可被收回。
没有 M8 关系数据时返回 available=False，调用方回退到 M7 老口径。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m4_graph import tarjan
from ..m7_retire import cycles, order, plan as legacy_plan


def _safe(loader, batch_id: str, *, default):
    try:
        return loader(batch_id)
    except Exception:  # noqa: BLE001 - M8 不强制依赖 M1 的附属台账
        return default


def _active_approval_adj(names: list[str], relations: list[dict]) -> dict[str, list[str]]:
    adj = {name: [] for name in names}
    for rel in relations:
        if rel["kind"] != config.REL_APPROVAL:
            continue
        if rel["status"] != config.APPROVAL_ACTIVE:
            continue
        if not (rel["known_src"] and rel["known_dst"]):
            continue
        adj[rel["src"]].append(rel["dst"])
    for name in adj:
        adj[name] = sorted(set(adj[name]))
    return adj


def governance_plan(store, settings, batch_id: str, targets: list[str]) -> dict | None:
    relations = store.load_relations(batch_id)
    if not relations:
        return None
    metrics = store.load_metrics(batch_id)
    # M8 只写关系台账，不写 M1 指标表；指标名以关系图里的注册节点为准，
    # 这样下线判断不依赖是否先跑过 M1。
    names = sorted({r["src"] for r in relations if r["known_src"]}
                   | {r["dst"] for r in relations if r["known_dst"]})
    if metrics:
        names = sorted(set(names) | {m["name"] for m in metrics})
    reports = _safe(store.load_reports, batch_id, default=[])
    versions = _safe(store.load_versions, batch_id, default=[])
    known = set(names)

    adj = _active_approval_adj(names, relations)
    reverse = legacy_plan.reverse_adjacency(adj)

    unknown = [n for n in targets if n not in known]
    usable = sorted({n for n in targets if n in known})

    approval_pairs = {(r["src"], r["dst"]) for r in relations
                      if r["kind"] == config.REL_APPROVAL
                      and r["known_src"] and r["known_dst"]}
    active_pairs = {p for p in approval_pairs
                    if next(r for r in relations
                            if r["kind"] == config.REL_APPROVAL
                            and (r["src"], r["dst"]) == p)["status"]
                    == config.APPROVAL_ACTIVE}
    disabled_pairs = approval_pairs - active_pairs
    reference_pairs = {(r["src"], r["dst"]) for r in relations
                       if r["kind"] == config.REL_REFERENCE and r["known_dst"]}

    comps = tarjan.strongly_connected(names, adj)
    rings = cycles.minimal_rings(
        [{"members": c} for c in comps], adj)
    cycle_set = cycles.ring_members(rings)
    isolated = cycles.isolated_nodes(names, adj)

    # 真实引用图求环：环检测按真实引用，隐藏环里的指标不许靠审批图自动放行
    ref_adj = {name: [] for name in names}
    for (src, dst) in reference_pairs:
        if src in known and dst in known:
            ref_adj[src].append(dst)
    ref_comps = tarjan.strongly_connected(names, ref_adj)
    ref_rings = cycles.minimal_rings([{"members": c} for c in ref_comps], ref_adj)
    ref_cycle_set = cycles.ring_members(ref_rings)
    hidden_cycle_members = sorted(ref_cycle_set - cycle_set)

    # 受影响面：目标 + 审批口径反向可达
    affected: set[str] = set(usable)
    for name in usable:
        affected.update(legacy_plan.downstream([name], reverse))
    affected |= set(usable)

    blocked_metrics = sorted(affected & cycle_set)
    ordering = order.retire_order(affected, adj, cycle_set)
    dependents = order.dependents_index(set(ordering["order"]), adj)
    reports_map = {name: [r["report_id"] for r in reports
                          if r["resolved"] and r["metric"] == name]
                   for name in ordering["order"]}
    checklist = order.step_rows(ordering["order"], dependents=dependents,
                                reports_by_metric=reports_map)

    blockers: list[dict] = []
    for name in blocked_metrics:
        blockers.append(_block(name, "R_CYCLE_BLOCKED", "环内"))
    # 隐藏环阻断：审批图无环，但真实引用成环，下线许可同样不成立
    for name in sorted(affected & set(hidden_cycle_members)):
        blockers.append(_block(
            name, "G_REF_CYCLE_HIDDEN", "隐藏环",
            f"{name} 在真实引用环内，审批图缺边，环被未申报引用藏起来"))
    # 许可阻断：受影响指标的每条真实引用边，缺有效审批即不放行
    for name in sorted(affected):
        outgoing = {dst for (src, dst) in reference_pairs if src == name}
        for dst in sorted(outgoing):
            if (name, dst) in active_pairs:
                continue
            if (name, dst) in disabled_pairs:
                blockers.append(_block(
                    name, "R_DEP_APPROVAL_DISABLED", "审批停用",
                    f"{name} → {dst} 的审批已停用，下线许可被收回"))
            else:
                blockers.append(_block(
                    name, "R_DEP_NOT_APPROVED", "未授权",
                    f"{name} 真实引用 {dst} 但无有效审批，不能靠真实引用自动放行"))
    blockers.sort(key=lambda b: (reasons.LEVEL_ORDER[b["level"]],
                                 b["code"], b["metric"]))

    metrics_index = {m["name"]: m for m in metrics}
    rows = [_target_row(
        name, metrics_index=metrics_index, reverse=reverse,
        reports=reports, versions=versions,
        eval_day=settings.eval_date, cycle_set=cycle_set) for name in usable]

    diagnostics = [{"code": b["code"], "level": b["level"], "stage": "retire",
                    "metric": b["metric"], "detail": b["detail"]} for b in blockers]
    diagnostics.sort(key=lambda d: (d["metric"] or "", d["code"]))

    return {
        "mode": "approval",
        "targets": usable, "unknown_targets": unknown,
        "rows": rows,
        "affected": sorted(affected), "affected_count": len(affected),
        "order": ordering["order"], "stuck": ordering["stuck"],
        "orderable_count": len(ordering["order"]),
        "checklist": checklist, "blockers": blockers,
        "rings": rings, "isolated": isolated,
        "counters": {
            "targets": len(usable), "affected": len(affected),
            "orderable": len(ordering["order"]),
            "blocked": len(blocked_metrics),
            "hidden_cycle_blocked": sum(
                1 for b in blockers if b["code"] == "G_REF_CYCLE_HIDDEN"),
            "permission_denied": sum(
                1 for b in blockers if b["code"] in
                ("R_DEP_NOT_APPROVED", "R_DEP_APPROVAL_DISABLED")),
            "rings": len(rings),
            "reference_rings": len(ref_rings), "isolated": len(isolated),
        },
        "diagnostics": diagnostics,
        "rules": {"order": config.RETIRE_ORDER_RULE,
                  "license": "审批关系决定下线许可，真实引用不自动放行"},
    }


def _block(metric: str, code: str, label: str, detail: str | None = None) -> dict:
    return {"metric": metric, "code": code, "level": reasons.level_of(code),
            "label": label, "detail": detail or reasons.get(code).title}


def _target_row(name: str, *, metrics_index: dict, reverse: dict,
                reports: list[dict], versions: list[dict],
                eval_day, cycle_set: set[str]) -> dict:
    """下线目标行：自洽版本，M1 指标 / 报表 / 版本台账缺失时给空值而非报错。"""
    down = legacy_plan.downstream([name], reverse)
    hit = [r for r in reports if r.get("resolved") and r["metric"] in (set(down) | {name})]
    hit.sort(key=lambda r: r["report_id"])
    weight = sum((r.get("weight", 0) for r in hit), 0)
    weight_text = _quantize(weight)
    metric = metrics_index.get(name)
    if metric is not None and versions:
        version = legacy_plan.version_state(name, versions, eval_day)
    else:
        version = {"total": 0, "effective": 0, "versions": [], "flags": [],
                   "state": "missing", "label": "无台账"}
    codes: list[str] = []
    if name in cycle_set:
        codes.append("R_CYCLE_BLOCKED")
    return {
        "metric": name,
        "line_no": metric.get("line_no") if metric else None,
        "downstream": down, "downstream_count": len(down),
        "reports": [{"report_id": r["report_id"],
                     "report_name": r.get("report_name", r["report_id"]),
                     "weight": _quantize(r.get("weight", 0))} for r in hit],
        "report_count": len(hit),
        "weight_sum": weight_text,
        "version": version, "codes": codes,
        "state": "blocked" if codes else "ok",
        "state_label": "阻断" if codes else "可下线",
    }


def _quantize(value) -> str:
    """权重统一两位小数字符串；接受 Decimal 或普通数值。"""
    if hasattr(value, "quantize"):
        return str(value.quantize(config.DECIMAL_QUANT))
    return str(Decimal(str(value)).quantize(config.DECIMAL_QUANT))
