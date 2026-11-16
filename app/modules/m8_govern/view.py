"""只读视图：从落库的双源关系组装「现网图 / 真实引用 / 下线判断」三版整页。

视图本身不做任何写操作，所有结论都来自 relations 表里带来源版本的证据：
- live：只取有效审批边（active），即决定下线许可的现网拓扑；
- actual：取全部真实引用边，用来发现被未申报引用藏起来的环；
- retire：在 live 基础上叠加许可核对，真实引用存在却没有有效审批即不放行。
"""
from __future__ import annotations

from ... import config, reasons
from ..m4_graph import tarjan
from ..m7_retire import cycles


def _adjacency(names: list[str], relations: list[dict], *, kind: str,
               only_active: bool) -> dict[str, list[str]]:
    adj = {name: [] for name in names}
    for rel in relations:
        if rel["kind"] != kind:
            continue
        if only_active and rel["status"] != config.APPROVAL_ACTIVE:
            continue
        if not (rel["known_src"] and rel["known_dst"]):
            continue
        adj[rel["src"]].append(rel["dst"])
    for name in adj:
        adj[name] = sorted(set(adj[name]))
    return adj


def _rings(names: list[str], adj: dict[str, list[str]]) -> tuple[list[dict], list[dict]]:
    comps = tarjan.strongly_connected(names, adj)
    comp_rows = [{"comp_id": i, "size": len(c), "members": c,
                  "is_cycle": tarjan.is_cycle(c, adj),
                  "path": tarjan.cycle_path(c, adj) if tarjan.is_cycle(c, adj) else []}
                 for i, c in enumerate(comps)]
    rings = cycles.minimal_rings([{"members": c} for c in comps], adj)
    return comp_rows, rings


def build_view(store, batch_id: str, view: str) -> dict:
    relations = store.load_relations(batch_id)
    names = sorted({r["src"] for r in relations if r["known_src"]}
                   | {r["dst"] for r in relations if r["known_dst"]})
    approval = [r for r in relations if r["kind"] == config.REL_APPROVAL]
    reference = [r for r in relations if r["kind"] == config.REL_REFERENCE]

    live_adj = _adjacency(names, relations, kind=config.REL_APPROVAL,
                          only_active=True)
    actual_adj = _adjacency(names, relations, kind=config.REL_REFERENCE,
                            only_active=False)
    live_comps, live_rings = _rings(names, live_adj)
    actual_comps, actual_rings = _rings(names, actual_adj)

    live_sets = {frozenset(r["members"]) for r in live_rings}
    hidden_member_sets = {frozenset(r["members"]) for r in actual_rings
                          if frozenset(r["members"]) not in live_sets}
    # 给真实引用环直接打 hidden 标记，模板按标记判断，不依赖 dict 相等比较
    actual_rings = [dict(r, hidden=frozenset(r["members"]) in hidden_member_sets)
                    for r in actual_rings]
    hidden_rings = [r for r in actual_rings if r["hidden"]]

    # 下线许可核对：真实引用方 → 该边是否有有效审批支撑
    active_pairs = {(r["src"], r["dst"]) for r in approval
                    if r["status"] == config.APPROVAL_ACTIVE
                    and r["known_src"] and r["known_dst"]}
    disabled_pairs = {(r["src"], r["dst"]) for r in approval
                      if r["status"] == config.APPROVAL_DISABLED}
    permission_rows = _permission_rows(reference, active_pairs, disabled_pairs)

    view = view if view in (config.VIEW_LIVE, config.VIEW_ACTUAL,
                            config.VIEW_RETIRE) else config.VIEW_LIVE
    chosen_adj = {config.VIEW_LIVE: live_adj, config.VIEW_ACTUAL: actual_adj,
                  config.VIEW_RETIRE: live_adj}[view]
    chosen_comps = {config.VIEW_LIVE: live_comps, config.VIEW_ACTUAL: actual_comps,
                    config.VIEW_RETIRE: live_comps}[view]
    chosen_rings = {config.VIEW_LIVE: live_rings, config.VIEW_ACTUAL: actual_rings,
                    config.VIEW_RETIRE: live_rings}[view]

    return {
        "view": view,
        "view_label": config.VIEW_LABELS[view],
        "names": names,
        "relations": _grouped(approval, reference),
        "adjacency": chosen_adj,
        "components": chosen_comps,
        "rings": chosen_rings,
        "live": {"components": live_comps, "rings": live_rings,
                 "ring_count": len(live_rings), "edge_count": sum(
                     len(v) for v in live_adj.values())},
        "actual": {"components": actual_comps, "rings": actual_rings,
                   "ring_count": len(actual_rings), "edge_count": sum(
                       len(v) for v in actual_adj.values())},
        "hidden_rings": hidden_rings,
        "permission": permission_rows,
        "summary": {
            "approval_total": len(approval),
            "approval_active": sum(1 for r in approval
                                   if r["status"] == config.APPROVAL_ACTIVE),
            "approval_disabled": sum(1 for r in approval
                                     if r["status"] == config.APPROVAL_DISABLED),
            "reference_total": len(reference),
            "reference_unknown": sum(1 for r in reference if not r["known_dst"]),
            "hidden_cycles": len(hidden_rings),
            "permission_denied": sum(1 for r in permission_rows
                                     if r["verdict"] != "licensed"),
        },
        "view_labels": config.VIEW_LABELS,
        "kind_labels": config.RELATION_KIND_LABELS,
        "status_labels": config.APPROVAL_STATUS_LABELS,
    }


def _grouped(approval: list[dict], reference: list[dict]) -> dict:
    """两类关系分开成表，各自带 source_version 与证据，页面分两块呈现。"""
    def shape(rel: dict) -> dict:
        return {
            "relation_id": rel["relation_id"], "src": rel["src"], "dst": rel["dst"],
            "status": rel["status"], "source_version": rel["source_version"],
            "line_no": rel.get("line_no"),
            "known_src": rel["known_src"], "known_dst": rel["known_dst"],
            "matched_approval": rel.get("matched_approval", ""),
            "matched_reference": rel.get("matched_reference", ""),
            "evidence": rel.get("evidence", {}),
        }
    return {"approval": [shape(r) for r in approval],
            "reference": [shape(r) for r in reference]}


def _permission_rows(reference: list[dict], active_pairs: set,
                     disabled_pairs: set) -> list[dict]:
    """每条真实引用边给一个许可结论：licensed / disabled / unapproved。

    审批关系决定下线许可，真实引用不会自动放行；停用的审批项收回许可。
    """
    rows: list[dict] = []
    for rel in sorted(reference, key=lambda r: (r["src"], r["dst"])):
        if not rel["known_dst"]:
            continue
        pair = (rel["src"], rel["dst"])
        if pair in active_pairs:
            verdict, code = "licensed", ""
        elif pair in disabled_pairs:
            verdict, code = "disabled", "R_DEP_APPROVAL_DISABLED"
        else:
            verdict, code = "unapproved", "R_DEP_NOT_APPROVED"
        rows.append({
            "src": rel["src"], "dst": rel["dst"], "verdict": verdict,
            "verdict_label": {"licensed": "已授权", "disabled": "审批停用",
                              "unapproved": "未授权"}[verdict],
            "code": code,
            "source_version": rel["source_version"],
            "detail": reasons.get(code).title if code else "有效审批依赖存在",
        })
    rows.sort(key=lambda r: (r["verdict"] == "licensed", r["src"], r["dst"]))
    return rows
