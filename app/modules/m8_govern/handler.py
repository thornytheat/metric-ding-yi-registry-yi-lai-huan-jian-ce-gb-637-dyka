"""M8 入口：双源关系发布（断点续跑）、三版整页视图、审批口径下线判断。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m7_retire import plan as retire_plan
from . import publish, retire_judge, view as view_mod

router = APIRouter(tags=["M8 双源关系治理"])


def _require_publish(ctx: Ctx, batch_id: str | None) -> dict:
    if batch_id:
        run = ctx.store.get_publish(batch_id)
        if run is not None and run["status"] == "done":
            return run
    else:
        for run in ctx.store.list_publishes():
            if run["status"] == "done":
                return ctx.store.get_publish(run["publish_id"])
    raise errors.NotFound("还没有已完成的关系发布，先 POST /api/publish",
                          code="E_EMPTY_INPUT")


def _field(payload, name: str, default=None):
    if isinstance(payload, dict):
        return payload.get(name, default)
    return default


@router.post("/api/publish")
def api_publish(payload: dict | None = Body(default=None),
                ctx: Ctx = Depends(get_ctx)) -> dict:
    text = _field(payload, "text")
    sample = _field(payload, "sample") or "metrics_dirty.dsl"
    approval_text = _field(payload, "approval_text")
    fail_at = _field(payload, "fail_at")
    if text is not None and not isinstance(text, str):
        raise errors.BadRequest("text 必须是字符串", code="E_EMPTY_INPUT")
    result = publish.publish(ctx.settings, ctx.store, text=text, sample=sample,
                             approval_text=approval_text, fail_at=fail_at)
    return result


@router.get("/api/publish")
def api_publish_list(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"runs": ctx.store.list_publishes(),
            "latest": ctx.store.latest_publish()}


@router.get("/api/relations")
def api_relations(view: str = "live", batch_id: str | None = None,
                  ctx: Ctx = Depends(get_ctx)) -> dict:
    run = _require_publish(ctx, batch_id)
    return view_mod.build_view(ctx.store, run["batch_id"], view)


@router.get("/api/retire/governed")
def api_retire_governed(targets: str, batch_id: str | None = None,
                        ctx: Ctx = Depends(get_ctx)) -> dict:
    run = _require_publish(ctx, batch_id)
    parsed = retire_plan.parse_targets(targets)
    if parsed["empty"]:
        raise errors.BadRequest("没有指定下线指标", code="R_NO_TARGETS")
    payload = retire_judge.governance_plan(
        ctx.store, ctx.settings, run["batch_id"], parsed["targets"])
    if payload is None:
        raise errors.NotFound("该批次还没有双源关系证据", code="E_EMPTY_INPUT")
    if payload["unknown_targets"]:
        raise errors.NotFound(
            "下线目标未注册：" + ", ".join(payload["unknown_targets"]),
            code="R_TARGET_UNKNOWN",
            detail={"unknown": payload["unknown_targets"]})
    payload["batch_id"] = run["batch_id"]
    return payload


@router.get("/dependencies")
def page_dependencies(request: Request, view: str = "live",
                      batch_id: str | None = None,
                      ctx: Ctx = Depends(get_ctx)):
    run = ctx.store.get_publish(batch_id) if batch_id else ctx.store.latest_publish()
    payload = None
    if run is not None and run["status"] == "done":
        payload = view_mod.build_view(ctx.store, run["batch_id"], view)
        payload["publish"] = {
            "publish_id": run["publish_id"], "status": run["status"],
            "attempts": run["attempts"], "checkpoint": run["checkpoint"],
            "checkpoints": ctx.store.load_checkpoints(run["publish_id"]),
        }
    elif run is not None:
        payload = {"publish": {
            "publish_id": run["publish_id"], "status": run["status"],
            "attempts": run["attempts"], "checkpoint": run["checkpoint"],
            "checkpoints": ctx.store.load_checkpoints(run["publish_id"]),
            "last_error": run["last_error"]}}
    return render(request, "dependencies.html", batch=None, page="dependencies",
                  payload=payload, current_view=view,
                  page_title="双源依赖关系与环检测")
