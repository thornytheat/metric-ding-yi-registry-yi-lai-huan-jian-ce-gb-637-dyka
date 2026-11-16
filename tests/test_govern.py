"""M8 双源关系治理：审批/真实引用分源、证据留痕、双图环、视图切换、断点续跑、幂等。

钉死的几条不变量：
- 审批关系决定下线许可，真实引用不自动放行（未授权/审批停用/隐藏环都要阻断）；
- 环检测按真实引用，能发现被未申报引用藏在审批图之外的隐藏环；
- 每条关系带来源版本与证据，两类关系分开成表；
- 发布失败从上次校验点续跑，重试不新增重复关系。
"""
from __future__ import annotations

import pytest

from app import config
from app.modules.m8_govern import approvals, engine, publish
from app.store import Store


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def published(client_with_run):
    """client 已跑过 M1；再发一次 M8 关系发布。"""
    resp = client_with_run.post("/api/publish", json={})
    assert resp.status_code == 200
    return client_with_run, resp.json()


# ------------------------------------------------------ 审批台账解析
def test_approval_ledger_parses_status_and_dedup():
    text = (
        "metric,depends_on,status,approver,approved_on,source_version,note\n"
        "A,B,active,u1,2026-10-01,V1,\n"
        "A,B,disabled,u1,2026-10-02,V2,重复行\n"
        "C,D,what,u2,2026-10-03,V3,非法状态归一\n"
        "E,,active,u3,2026-10-04,V4,空依赖\n"
    )
    parsed = approvals.parse_approvals_text(text)
    rows = parsed["rows"]
    assert [(r.metric, r.depends_on, r.status) for r in rows[:3]] == [
        ("A", "B", "active"), ("A", "B", "disabled"), ("C", "D", "active")]
    assert rows[1].duplicate is True
    assert rows[2].status == config.APPROVAL_ACTIVE  # 非法状态归一
    # 空 depends_on 行被跳过并留问题，不静默进表
    assert len(rows) == 3
    assert parsed["issues"] and parsed["issues"][0]["code"] == "G_APPROVAL_UNKNOWN"


def test_duplicate_ledger_row_emits_dedicated_reason_and_no_extra_relation(client):
    text = (
        "metric,depends_on,status,approver,approved_on,source_version,note\n"
        "毛利率,毛利额,active,u1,2026-10-01,V1,\n"
        "毛利率,毛利额,active,u1,2026-10-02,V2,重复\n"
    )
    body = client.post("/api/publish", json={"approval_text": text}).json()
    codes = {d["code"] for d in body["diagnostics"]}
    assert "G_APPROVAL_DUPLICATE" in codes
    bid = body["batch_id"]
    appr = [r for r in client.app.state.store.load_relations(bid)
            if r["kind"] == config.REL_APPROVAL]
    assert [(r["src"], r["dst"]) for r in appr].count(("毛利率", "毛利额")) == 1


# ------------------------------------------------------ 双源引擎分账
def test_engine_splits_two_relation_kinds_with_evidence(published):
    client, body = published
    pid = body["publish_id"]
    view = client.get(f"/api/relations?view=live&batch_id={pid}").json()
    appr = view["relations"]["approval"]
    ref = view["relations"]["reference"]
    assert appr and ref
    assert {r["src"] for r in appr}  # 审批关系独立成表
    # 每条关系都带来源版本与证据
    assert all(r["source_version"] for r in appr + ref)
    assert all(r["evidence"] for r in appr + ref)
    # 真实引用证据能回溯到 DSL 行号，审批证据能回溯到审批人与版本
    assert all(("line_no" in r["evidence"]) for r in ref)
    assert all(r["evidence"].get("approver") for r in appr)


def test_approval_does_not_auto_adopt_real_reference(published):
    """未申报引用：真实图有边，审批图没有，且不自动补审批。"""
    client, body = published
    pid = body["publish_id"]
    view = client.get(f"/api/relations?view=live&batch_id={pid}").json()
    active_pairs = {(r["src"], r["dst"]) for r in view["relations"]["approval"]
                    if r["status"] == config.APPROVAL_ACTIVE}
    ref_pairs = {(r["src"], r["dst"]) for r in view["relations"]["reference"]
                 if r["known_dst"]}
    # 未声明引用 → 毛利率：真实引用存在，审批里没有
    assert ("未声明引用", "毛利率") in ref_pairs
    assert ("未声明引用", "毛利率") not in active_pairs
    codes = {d["code"] for d in body["diagnostics"]}
    assert "G_APPROVAL_MISSING" in codes


def test_disabled_approval_kept_as_evidence_and_flagged(published):
    client, body = published
    pid = body["publish_id"]
    view = client.get(f"/api/relations?view=live&batch_id={pid}").json()
    disabled = [r for r in view["relations"]["approval"]
                if r["status"] == config.APPROVAL_DISABLED]
    assert ("毛利率", "销售额") in {(r["src"], r["dst"]) for r in disabled}
    # 停用项仍保留证据，没有被删除
    assert all(r["source_version"] for r in disabled)
    codes = {d["code"] for d in body["diagnostics"]}
    assert "G_APPROVAL_DISABLED" in codes


def test_unknown_approval_target_kept_but_excluded_from_graph(published):
    client, body = published
    pid = body["publish_id"]
    view = client.get(f"/api/relations?view=live&batch_id={pid}").json()
    dangling = [r for r in view["relations"]["approval"] if not r["known_dst"]]
    assert ("悬空引用", "已下线指标") in {(r["src"], r["dst"]) for r in dangling}
    codes = {d["code"] for d in body["diagnostics"]}
    assert "G_APPROVAL_UNKNOWN" in codes


# ------------------------------------------------------ 双图环检测
def test_reference_graph_finds_hidden_cycles_absent_in_approval_graph(published):
    client, body = published
    pid = body["publish_id"]
    live = client.get(f"/api/relations?view=live&batch_id={pid}").json()
    actual = client.get(f"/api/relations?view=actual&batch_id={pid}").json()
    # 审批（现网）图无环，真实引用图 3 个环，全部是隐藏环
    assert live["live"]["ring_count"] == 0
    assert actual["actual"]["ring_count"] == 3
    assert len(actual["hidden_rings"]) == 3
    hidden_sets = {tuple(sorted(r["members"])) for r in actual["hidden_rings"]}
    assert ("净增量", "基期销售额") in hidden_sets
    assert ("自比值",) in hidden_sets
    codes = {d["code"] for d in body["diagnostics"]}
    assert "G_REF_CYCLE_HIDDEN" in codes


def test_full_page_view_switch_changes_graph_and_counts(published):
    """整版切换 live/actual/retire，环与边按视图来源变化。"""
    client, body = published
    pid = body["publish_id"]
    live = client.get(f"/dependencies?view=live&batch_id={pid}")
    actual = client.get(f"/dependencies?view=actual&batch_id={pid}")
    retire = client.get(f"/dependencies?view=retire&batch_id={pid}")
    assert live.status_code == actual.status_code == retire.status_code == 200
    assert "现网图" in live.text and "真实引用" in actual.text
    # 隐藏环提示只在非真实引用视图出现
    assert "隐藏环" in live.text


# ------------------------------------------------------ 下线判断按审批
def test_retire_blocks_unapproved_real_reference(published):
    client, _ = published
    pid = client.get("/api/publish").json()["latest"]["publish_id"]
    resp = client.get(f"/api/retire/governed?targets=周转天数&batch_id={pid}")
    payload = resp.json()
    codes = {(b["code"], b["metric"]) for b in payload["blockers"]}
    assert ("R_DEP_NOT_APPROVED", "周转天数") in codes


def test_retire_blocks_disabled_approval(published):
    client, _ = published
    pid = client.get("/api/publish").json()["latest"]["publish_id"]
    payload = client.get(
        f"/api/retire/governed?targets=毛利率&batch_id={pid}").json()
    codes = {(b["code"], b["metric"]) for b in payload["blockers"]}
    assert ("R_DEP_APPROVAL_DISABLED", "毛利率") in codes


def test_retire_blocks_hidden_cycle_members(published):
    client, _ = published
    pid = client.get("/api/publish").json()["latest"]["publish_id"]
    payload = client.get(
        f"/api/retire/governed?targets=动销率&batch_id={pid}").json()
    codes = {b["code"] for b in payload["blockers"]}
    assert "G_REF_CYCLE_HIDDEN" in codes


def test_retire_licensed_edge_is_not_blocked(published):
    """有有效审批支撑的依赖不产生许可阻断（不靠真实引用自动放行的正向面）。"""
    client, _ = published
    pid = client.get("/api/publish").json()["latest"]["publish_id"]
    payload = client.get(
        f"/api/retire/governed?targets=净增量&batch_id={pid}").json()
    # 净增量→基期销售额 有 active 审批，不出现针对这条边的未授权阻断
    bad = [b for b in payload["blockers"]
           if b["metric"] == "净增量" and b["code"] == "R_DEP_NOT_APPROVED"]
    assert bad == []


# ------------------------------------------------------ 断点续跑与幂等
def test_publish_resumes_from_last_checkpoint(client):
    # 第一次在 cycle 校验点失败
    fail = client.post("/api/publish", json={"fail_at": "cycle"})
    assert fail.status_code == 400
    assert fail.json()["detail"]["checkpoint"] == "cycle"
    run = client.get("/api/publish").json()["latest"]
    assert run["status"] == "failed"
    done_before = [c["stage"] for c in
                   client.get("/api/publish").json()["latest"].get("checkpoints", [])]
    # 失败态下关系视图不可用
    assert client.get("/api/relations").status_code == 404

    # 第二次不带 fail_at，从断点续跑成功
    ok = client.post("/api/publish", json={})
    assert ok.status_code == 200
    body = ok.json()
    assert body["resumed"] is True
    assert body["resumed_from"] == "approval"  # 上次成功的校验点
    assert body["note"]
    stages = [c["stage"] for c in body["checkpoints"]]
    assert stages == list(config.PUBLISH_STAGES)
    # 已完成校验点不重算：attempts 累加但只跑剩余阶段
    assert body["attempts"] == 2


def test_retry_does_not_add_duplicate_relations(client):
    client.post("/api/publish", json={"fail_at": "persist"})
    first = client.post("/api/publish", json={}).json()
    bid = first["batch_id"]
    store: Store = client.app.state.store
    before = store.load_relations(bid)
    # 再续跑 / 重放多次
    client.post("/api/publish", json={})
    client.post("/api/publish", json={})
    after = store.load_relations(bid)
    assert len(after) == len(before)
    ids = [r["relation_id"] for r in after]
    assert len(ids) == len(set(ids))


def test_completed_publish_replays_without_new_run(client):
    first = client.post("/api/publish", json={}).json()
    second = client.post("/api/publish", json={}).json()
    assert second.get("reused") is True
    assert second["publish_id"] == first["publish_id"]
    runs = client.get("/api/publish").json()["runs"]
    assert len(runs) == 1


# ------------------------------------------------------ 纯引擎确定性
def test_engine_is_deterministic_and_partitions_total():
    metrics = [
        dict(name="a", line_no=1, metrics=["b"], depends_declared=["b"],
             expr_text="ref(b)", fields=[], group_by=[], aggregators=[],
             where=None, signature="s"),
        dict(name="b", line_no=2, metrics=["a"], depends_declared=[],
             expr_text="ref(a)", fields=[], group_by=[], aggregators=[],
             where=None, signature="s"),
    ]
    rows = approvals.parse_approvals_text(
        "metric,depends_on,status,approver,approved_on,source_version,note\n"
        "a,b,active,u,2026-10-01,V1,\n"
    )["rows"]
    g1 = engine.build_relations(metrics=metrics, approval_rows=rows,
                                batch_id="b1", rule_version="r", input_digest="d")
    g2 = engine.build_relations(metrics=metrics, approval_rows=rows,
                                batch_id="b1", rule_version="r", input_digest="d")
    assert [r["relation_id"] for r in g1["relations"]] == \
           [r["relation_id"] for r in g2["relations"]]
    # b→a 未申报：审批图无环，真实图有一个隐藏 2 环
    assert g1["approval"]["rings"] == []
    assert len(g1["reference"]["rings"]) == 1
    assert len(g1["hidden_rings"]) == 1
