"""SubmissionPlan → 人类可读 playbook / JSON(A17,computer-use 备选路线配套)。

- plan_to_markdown: 计划 → 中文 markdown 操作手册,供人工或半自动(computer-use)执行。
- plan_to_json:     计划 → 纯 JSON dict(枚举转 value,字段名 snake_case),
                    可直接 json.dumps 落盘,交给 drivers/driver.mjs 消费。
- plan_from_dict:   plan_to_json 的输出 → SubmissionPlan(main 往返用)。
- main:             python -m netsentinel.submit.playbook_gen <plan.json> [-o out.md]

安全红线(CONTRACTS.md §0,必须在产物中体现):
- 验证码只能人工输入:HUMAN_GATE 步骤在 playbook 中整行标注"⚠️ 人工操作:",
  值列固定写"验证码/附件上传由人工完成"。
- 本模块只做本地文件读写,不发起任何网络请求(allow_network=False 默认)。

V5 升级(契约 §1):
- 性能:markdown 的静态区块(两段表头 + 声明/执行须知页脚)在导入时一次性
  预拼接为模块常量,渲染只拼接动态行并单遍 join;枚举 → value 转换经
  ``functools.lru_cache`` 缓存(枚举成员有限,进程内每成员只算一次)。
- 可观测性:每次 markdown 生成记 ``telemetry.inc("playbook.generated")``。

用法示例::

    from netsentinel.submit import playbook_gen
    md = playbook_gen.plan_to_markdown(plan)
    data = playbook_gen.plan_to_json(plan)   # 可直接 json.dumps
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from functools import lru_cache
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import (
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
)

__all__ = [
    "PORTAL_TITLES",
    "plan_to_markdown",
    "plan_to_json",
    "plan_from_dict",
    "main",
]

# 门户代码 → 展示名(未知代码原样展示)
PORTAL_TITLES: dict[str, str] = {
    Portal.P12377.value: "中央网信办违法和不良信息举报中心(12377)",
    Portal.SHDF.value: "全国“扫黄打非”工作小组办公室(扫黄打非)",
}

_DESC_PREVIEW_LEN = 80
_HUMAN_GATE_PREFIX = "⚠️ 人工操作:"
_HUMAN_GATE_VALUE = "验证码/附件上传由人工完成"
_FOOTER_NOTICE = "本 playbook 由系统生成,执行前须人工核对,验证码严禁自动处理"

#: 遥测指标名(V5 可观测性)
_GENERATED_METRIC = "playbook.generated"


@lru_cache(maxsize=None)
def _cached_action_value(action: Any) -> str:
    """StepAction → 契约 value 的缓存转换(枚举成员有限,只算一次)。"""
    return action.value if isinstance(action, StepAction) else str(action)


@lru_cache(maxsize=None)
def _cached_portal_value(portal: Any) -> str:
    """Portal → 契约 value 的缓存转换(成员有限,只算一次)。"""
    return portal.value if isinstance(portal, Portal) else str(portal)


def _portal_value(portal: Any) -> str:
    """枚举或字符串统一转为契约 value(缓存命中 O(1))。

    不可哈希输入(防御性,lru_cache 会抛 TypeError)退化为直算,行为不变。
    """
    try:
        return _cached_portal_value(portal)
    except TypeError:  # pragma: no cover - 契约外防御分支
        return portal.value if isinstance(portal, Portal) else str(portal)


def _portal_title(portal: Any) -> str:
    key = _portal_value(portal)
    return PORTAL_TITLES.get(key, key)


def _cell(text: Any) -> str:
    """markdown 表格单元格:空值 → —,转义竖线与换行。"""
    s = "" if text is None else str(text)
    if not s.strip():
        return "—"
    return s.replace("|", "\\|").replace("\n", " ")


def _action_value(action: Any) -> str:
    """枚举 → 契约 value(缓存命中 O(1);不可哈希输入退化为直算)。"""
    try:
        return _cached_action_value(action)
    except TypeError:  # pragma: no cover - 契约外防御分支
        return action.value if isinstance(action, StepAction) else str(action)


def _target_cell(step: Step) -> str:
    """目标列:CSS selector(代码样式)与人类可读 text 同时存在时一并展示。"""
    parts: list[str] = []
    if step.selector:
        parts.append(f"`{_cell(step.selector)}`")
    if step.text:
        parts.append(_cell(step.text))
    return " / ".join(parts) if parts else "—"


#: 摘要表静态表头(V5:导入时预拼接,渲染时零重复构造)
_SUMMARY_TABLE_HEADER = (
    "## 一、举报信息摘要(payload)\n\n| 字段 | 内容 |\n| --- | --- |"
)

#: 步骤表静态表头(五列,契约 §3)
_STEPS_TABLE_HEADER = (
    "## 二、执行步骤\n\n"
    "| # | 操作 | 说明 | 目标(selector/text) | 值 |\n| --- | --- | --- | --- | --- |"
)

#: 页脚静态区块(声明 + 执行须知;含结尾空行对应的换行)
_FOOTER_BLOCK = (
    "---\n\n"
    f"> **声明:{_FOOTER_NOTICE}。**\n\n"
    "执行须知:\n"
    "- 验证码只能人工输入;任何自动化组件(Playwright / computer-use)都不得代填。\n"
    "- 附件(证据包 zip)上传在人工门环节完成,v1 不做自动上传。\n"
    "- 提交须遵守频控(默认间隔 60s、每日 ≤5 次),由 Python 侧 orchestrator 强制,执行手册不得绕过。\n"
    "- 半自动执行:先用 plan_to_json 导出计划 JSON,再由 "
    "`node drivers/driver.mjs plan.json --engine=console|playwright|plugin` 驱动(见 docs/computer-use-integration.md)。\n"
)


def plan_to_markdown(plan: SubmissionPlan) -> str:
    """把 SubmissionPlan 渲染为中文人工执行 playbook(markdown)。

    V5:静态区块(表头 / 页脚)为导入时预拼接的模块常量,函数内只构造
    动态行并单遍 join;生成成功记 ``telemetry.inc("playbook.generated")``。
    """
    payload = plan.payload
    title = _portal_title(plan.portal)
    portal_code = _portal_value(plan.portal)
    desc_preview = (payload.description or "").strip()[:_DESC_PREVIEW_LEN]
    reporter = " / ".join(
        part if part else "(人工填写)"
        for part in (payload.reporter_name, payload.reporter_phone)
    )

    parts: list[str] = [
        f"# 举报 Playbook:{title}",
        "",
        f"- 举报门户:{title}",
        f"- 门户代码:`{portal_code}`",
        f"- 入口 URL:`{plan.entry_url}`",
        "",
        _SUMMARY_TABLE_HEADER,
        f"| 举报站点 | {_cell(payload.site_url)} |",
        f"| 信息类型 | {_cell(payload.category)} |",
        f"| 描述(前 {_DESC_PREVIEW_LEN} 字) | {_cell(desc_preview)} |",
        f"| 证据包 | {_cell(payload.evidence_zip)} |",
        f"| 举报人(姓名 / 电话) | {_cell(reporter)} |",
        "",
        _STEPS_TABLE_HEADER,
    ]
    for idx, step in enumerate(plan.steps, start=1):
        action = _action_value(step.action)
        if action == StepAction.HUMAN_GATE.value:
            # 红线:人工门整行标注,值列固定说明验证码/附件由人工完成
            op_cell = f"{_HUMAN_GATE_PREFIX}{action}(人工门)"
            value_cell = _HUMAN_GATE_VALUE
        else:
            op_cell = action
            value_cell = _cell(step.value)
        parts.append(
            f"| {idx} | {op_cell} | {_cell(step.label)} | "
            f"{_target_cell(step)} | {value_cell} |"
        )
    parts.append("")
    parts.append(_FOOTER_BLOCK)

    telemetry.inc(_GENERATED_METRIC)
    return "\n".join(parts)


def plan_to_json(plan: SubmissionPlan) -> dict[str, Any]:
    """计划 → 可直接 json.dumps 的 dict(枚举转 value,字段名 snake_case)。"""
    return {
        "portal": _portal_value(plan.portal),
        "entry_url": plan.entry_url,
        "payload": {
            "portal": _portal_value(plan.payload.portal),
            "site_url": plan.payload.site_url,
            "category": plan.payload.category,
            "description": plan.payload.description,
            "evidence_zip": plan.payload.evidence_zip,
            "reporter_name": plan.payload.reporter_name,
            "reporter_phone": plan.payload.reporter_phone,
        },
        "steps": [
            {
                "action": _action_value(step.action),
                "label": step.label,
                "selector": step.selector,
                "text": step.text,
                "value": step.value,
                "timeout_s": step.timeout_s,
                "meta": dict(step.meta),
            }
            for step in plan.steps
        ],
    }


def plan_from_dict(data: dict[str, Any]) -> SubmissionPlan:
    """plan_to_json 的输出 → SubmissionPlan(缺关键字段抛中文 ValueError)。"""
    if not isinstance(data, dict):
        raise ValueError("计划 JSON 根节点必须是对象")
    for key in ("portal", "entry_url", "payload", "steps"):
        if key not in data:
            raise ValueError(f"计划 JSON 缺少字段:{key}")

    raw_payload = data["payload"] or {}
    payload = SubmissionPayload(
        portal=Portal(raw_payload.get("portal") or data["portal"]),
        site_url=raw_payload.get("site_url", ""),
        category=raw_payload.get("category", ""),
        description=raw_payload.get("description", ""),
        evidence_zip=raw_payload.get("evidence_zip", ""),
        reporter_name=raw_payload.get("reporter_name", ""),
        reporter_phone=raw_payload.get("reporter_phone", ""),
    )
    steps = [
        Step(
            action=StepAction(item.get("action", "")),
            label=item.get("label", ""),
            selector=item.get("selector", ""),
            text=item.get("text", ""),
            value=item.get("value", ""),
            timeout_s=float(item.get("timeout_s", 10.0)),
            meta=dict(item.get("meta") or {}),
        )
        for item in data["steps"]
    ]
    return SubmissionPlan(
        portal=Portal(data["portal"]),
        entry_url=data["entry_url"],
        payload=payload,
        steps=steps,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.submit.playbook_gen",
        description="把 SubmissionPlan JSON(plan_to_json 输出)转为中文人工执行 playbook(markdown)",
    )
    parser.add_argument("plan", help="计划 JSON 文件路径")
    parser.add_argument(
        "-o", "--output", default=None,
        help="输出 markdown 文件路径;缺省打印到标准输出",
    )
    args = parser.parse_args(argv)

    try:
        data = json.loads(pathlib.Path(args.plan).read_text(encoding="utf-8"))
        markdown = plan_to_markdown(plan_from_dict(data))
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        print(f"[playbook_gen] 读取或解析计划失败:{exc}", file=sys.stderr)
        return 2

    if args.output:
        out = pathlib.Path(args.output)
        if out.parent and str(out.parent):
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"[playbook_gen] 已写入 {out}")
    else:
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
