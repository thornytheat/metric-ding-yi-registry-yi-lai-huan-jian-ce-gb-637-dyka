"""关系发布流水线：六个校验点顺序执行，失败后从上次校验点续跑。

设计要点：
- 每个校验点把产物写进 publish_runs.state（JSON），成功即落 checkpoint；
  重跑时已完成的校验点直接从 state 取回，不重算。
- 关系落库按 (batch_id, relation_id) 唯一键幂等写入，续跑 / 重试不会
  新增重复关系；整版切换前再用 replace_relations 做一次同事务先删后插。
- 失败把 run 置 failed 并记录断点与错误，修正输入后用同一 publish 键再提交
  即从断点续跑；已完成批次（done）再提交直接复用，不新增关系。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ... import config, errors, manifest, reasons
from ..m1_ingest import reader
from ..m3_diagnostics import service as diag_service
from ..m4_graph import service as graph_service
from ..m5_versions import service as version_service
from ..m6_impact import service as impact_service
from . import approvals, engine

APPROVAL_FILE = "dependency_approvals.csv"


def _now() -> str:
    return dt.datetime.now(config.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_sample(settings, name: str) -> tuple[str, str]:
    path = Path(settings.sample_dir) / name
    data = path.read_bytes()
    return data.decode("utf-8-sig"), manifest.digest_bytes(data)


class StageFailure(Exception):
    """测试与续跑逻辑用：在校验点处模拟 / 抛出失败。"""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def publish_key(dsl_text: str, approval_text: str, rule_version: str) -> str:
    return manifest.digest_parts(
        [dsl_text, approval_text, rule_version])[:32]


def _batch_id(pub_key: str, run_id: str) -> str:
    return manifest.digest_parts(["govern", pub_key, run_id])[:16]


def publish(settings, store, *, text: str | None = None,
            sample: str = "metrics_dirty.dsl",
            approval_text: str | None = None,
            fail_at: str | None = None) -> dict:
    decoded, source_label = reader.read_source(
        Path(settings.sample_dir), text=text, name=sample)
    if approval_text is None:
        approval_text, _ = _read_sample(settings, APPROVAL_FILE)
    pub_key = publish_key(decoded.text, approval_text, settings.rule_version)

    existing = store.find_publish_by_key(pub_key)
    if existing is not None and existing["status"] == "done":
        return _done_replay(store, existing)

    if existing is not None and existing["status"] == "failed":
        run = existing  # 续跑：复用 publish_id、batch_id 与已存 state
        resumed = True
    else:
        run_id = manifest.new_run_id(prefix="MP")
        run = {
            "publish_id": manifest.digest_parts([pub_key, run_id])[:16],
            "batch_id": _batch_id(pub_key, run_id),
            "publish_key": pub_key, "run_id": run_id,
            "status": "running", "checkpoint": "",
            "created_at": _now(), "updated_at": _now(),
            "last_error": "", "attempts": 0,
            "state": {"source_label": source_label},
        }
        resumed = False
    run["attempts"] = int(run.get("attempts", 0)) + 1

    state: dict = dict(run.get("state", {}))
    resumed_from = run.get("checkpoint", "") if resumed else ""
    stages = list(config.PUBLISH_STAGES)
    try:
        for stage in stages:
            if stage in state.get("done_stages", []):
                continue  # 断点续跑：已完成校验点不重算
            if fail_at == stage:
                raise StageFailure(stage, f"模拟在 {stage} 校验点失败")
            state = _run_stage(stage, state, settings, decoded.text,
                               approval_text, run, store)
            state.setdefault("done_stages", []).append(stage)
            run["checkpoint"] = stage
            run["state"] = state
            run["updated_at"] = _now()
            store.upsert_publish(run)
            store.mark_checkpoint(run["publish_id"], stage, "done", _now())
    except StageFailure as exc:
        run["status"] = "failed"
        run["last_error"] = f"{exc.stage}: {exc.message}"
        run["updated_at"] = _now()
        run["state"] = state
        store.upsert_publish(run)
        store.mark_checkpoint(run["publish_id"], exc.stage, "failed", _now(),
                              exc.message)
        raise errors.BadRequest(
            reasons.get("B_PUBLISH_STAGE_FAILED").title,
            code="B_PUBLISH_STAGE_FAILED",
            detail={"publish_id": run["publish_id"], "checkpoint": exc.stage,
                    "resume": "用同一输入再次 POST /api/publish 即可续跑"})

    run["status"] = "done"
    run["checkpoint"] = "persist"
    run["updated_at"] = _now()
    run["state"] = state
    store.upsert_publish(run)
    return {
        "status": "done", "resumed": resumed,
        "resumed_from": resumed_from,
        "note": reasons.get("B_PUBLISH_RESUMED").title if resumed else "",
        "publish_id": run["publish_id"], "batch_id": run["batch_id"],
        "publish_key": pub_key, "run_id": run["run_id"],
        "attempts": run["attempts"],
        "checkpoints": store.load_checkpoints(run["publish_id"]),
        "summary": state.get("summary", {}),
        "diagnostics": state.get("govern_diagnostics", []),
    }


def _run_stage(stage: str, state: dict, settings, dsl_text: str,
               approval_text: str, run: dict, store) -> dict:
    out = dict(state)
    if stage == "decode":
        raw_rows, crlf = reader.split_lines(dsl_text)
        out["raw_rows"] = [{"line_no": r.line_no, "raw": r.raw, "crlf": r.crlf}
                           for r in raw_rows]
        out["crlf"] = crlf
        return out

    if stage == "diagnose":
        raw_rows = reader.split_lines(dsl_text)[0]
        inspect = diag_service.inspect_lines(raw_rows)
        out["metrics"] = inspect["metrics"]
        out["diag_diagnostics"] = inspect["diagnostics"]
        return out

    if stage == "reference":
        metrics = out["metrics"]
        reports_text, _ = _read_sample(settings, "report_usage.csv")
        report_rows = graph_service.parse_reports_text(reports_text)
        graph = graph_service.analyse(metrics, report_rows)
        out["graph"] = {
            "nodes": graph["nodes"], "edges": graph["edges"],
            "components": graph["components"], "conflicts": graph["conflicts"],
            "reports": graph["reports"],
        }
        return out

    if stage == "approval":
        parsed = approvals.parse_approvals_text(approval_text)
        out["approval_rows"] = [r.__dict__ for r in parsed["rows"]]
        return out

    if stage == "cycle":
        gov = engine.build_relations(
            metrics=out["metrics"],
            approval_rows=[approvals.ApprovalRow(**r)
                           for r in out["approval_rows"]],
            batch_id=run["batch_id"], rule_version=settings.rule_version,
            input_digest=run["publish_key"])
        out["relations"] = gov["relations"]
        out["govern_diagnostics"] = gov["diagnostics"]
        out["reference_graph"] = {
            "components": gov["reference"]["components"],
            "rings": gov["reference"]["rings"]}
        out["approval_graph"] = {
            "components": gov["approval"]["components"],
            "rings": gov["approval"]["rings"]}
        out["hidden_rings"] = gov["hidden_rings"]
        out["summary"] = gov["summary"]
        return out

    if stage == "persist":
        # 整版先删后插（同事务），保证切换不留残边；唯一键再兜底幂等。
        store.replace_relations(run["batch_id"], out["relations"])
        # 版本仍走原台账，供下线判断取评估日生效版本
        versions = version_service.load(settings, out["metrics"])
        out["versions"] = versions["rows"]
        accounts = impact_service.accounts(
            summary={"total": len(out["raw_rows"]), "blank": 0,
                     "comment": 0, "definition": len(out["raw_rows"])},
            metrics=out["metrics"], nodes=out["graph"]["nodes"],
            edges=out["graph"]["edges"],
            components=out["graph"]["components"],
            isolated_lines=0, reports=out["graph"]["reports"])
        out["accounts"] = accounts
        return out

    raise StageFailure(stage, f"未知校验点 {stage}")


def _done_replay(store, existing: dict) -> dict:
    diag = [d for d in existing["state"].get("govern_diagnostics", [])]
    return {
        "status": "done", "resumed": False, "reused": True,
        "note": reasons.get("B_IDEMPOTENT_REPLAY").title,
        "publish_id": existing["publish_id"], "batch_id": existing["batch_id"],
        "publish_key": existing["publish_key"], "run_id": existing["run_id"],
        "attempts": existing["attempts"],
        "checkpoints": store.load_checkpoints(existing["publish_id"]),
        "summary": existing["state"].get("summary", {}),
        "diagnostics": diag,
    }
