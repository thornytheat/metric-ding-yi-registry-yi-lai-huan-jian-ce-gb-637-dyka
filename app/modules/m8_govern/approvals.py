"""审批依赖台账解析：一行一条 metric → depends_on 的审批关系。

台账是审批侧的人工输入，与 DSL 表达式解析走不同路径：
- status 只认 active / disabled，其余归一为 active 并留备注；
- 重复行（同 metric+depends_on）只采信首条，其余标 duplicate，
  这样重试 / 重复导入不会把同一条审批关系记成两条；
- 解析只做结构清洗，不判断指标是否注册（注册对账在 engine 里做），
  保证未注册指标的审批也能作为证据保留下来。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from ... import config


@dataclass
class ApprovalRow:
    line_no: int
    metric: str
    depends_on: str
    status: str
    approver: str
    approved_on: str
    source_version: str
    note: str
    duplicate: bool = False
    raw: str = ""
    issues: list[str] = field(default_factory=list)


REQUIRED_COLUMNS = ("metric", "depends_on", "status", "approver",
                    "approved_on", "source_version")


def parse_approvals_text(text: str) -> dict:
    """返回结构化审批行与解析问题；缺列、空键都显式报出来，不静默丢行。"""
    reader = csv.DictReader(io.StringIO(text))
    rows: list[ApprovalRow] = []
    issues: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(reader, start=2):  # 表头占第 1 行
        metric = (raw.get("metric") or "").strip()
        depends_on = (raw.get("depends_on") or "").strip()
        if not metric or not depends_on:
            issues.append({"line_no": index, "code": "G_APPROVAL_UNKNOWN",
                           "detail": "审批行缺少 metric 或 depends_on，已跳过"})
            continue
        status_text = (raw.get("status") or "").strip().lower()
        if status_text not in (config.APPROVAL_ACTIVE, config.APPROVAL_DISABLED):
            status_text = config.APPROVAL_ACTIVE
        key = (metric, depends_on)
        duplicate = key in seen
        seen.add(key)
        row = ApprovalRow(
            line_no=index, metric=metric, depends_on=depends_on,
            status=status_text,
            approver=(raw.get("approver") or "").strip() or "—",
            approved_on=(raw.get("approved_on") or "").strip() or "—",
            source_version=(raw.get("source_version") or "").strip() or "未标注版本",
            note=(raw.get("note") or "").strip(),
            duplicate=duplicate,
            raw=",".join((raw.get(c) or "") for c in REQUIRED_COLUMNS),
        )
        if duplicate:
            row.issues.append("duplicate")
        rows.append(row)
    return {"rows": rows, "issues": issues,
            "columns": reader.fieldnames or []}
