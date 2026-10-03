"""VLM 调用成本计量器(V4 · A75)。

职责:跨平台视觉模型调用的记账与估算——**花在哪家、花了多少(提示值口径)**。

口径与红线(契约 V4 §0 红线 18):
- ``PRICE_HINTS`` 中每一条都是**提示值,以账单为准**:仅保留数量级合理性,
  不构成任何精确报价;上线前必须按各平台官方定价核验一次,可用配置覆盖。
- 目录缺失的 ``provider:model`` → :meth:`CostMeter.estimate` 返回 ``None``
  (绝不猜价,汇总时另计 ``unpriced_calls``)。
- 本地提供方(``LOCAL_FREE``,红线 16)数据不出本机,成本按本地算力记 ``0.0``。

记账落盘为 jsonl(每行一次调用):``{"ts","provider","model","images","est_cost"}``;
``est_cost`` 为 ``null`` 表示当时无价格提示。本模块管**金额侧**账本,与
``vlm_cache.spend_one`` 的调用次数预算(红线 19:跨平台共用一本预算账)并行互补。

线程安全:仅一个 ``threading.Lock`` 保护追加写;汇总读取为无锁快照。
损坏的 jsonl 行跳过并告警,不影响其余账目。

V5 升级:``summary`` / ``today`` 改为**流式逐行读取**(不再 ``read_text``
整载后 ``splitlines``),内存占用由 O(账本全量) 降为 O(按提供方聚合桶),
文件句柄由 ``with`` 保证释放(Windows 句柄复查);每次成功记账计
``telemetry.inc("cost.record")``。

V13 升级(per-run 成本归集):``record`` 增可选维度 ``run_id``(批次标识)
/ ``tokens``(可得时)/ ``duration_s``(**缺省不写键——不传 run_id 时行为
与行形态逐字节不变**);新增纯聚合 :func:`aggregate`(``by=run_id|model|day``,
离线可单测、不触文件与遥测),旧账目行按缺维度回退:无 ``run_id`` 归
"(未标记批次)" 桶、``ts`` 不可解析归 "(未知日期)" 桶;返回行结构带
中文表头映射(:data:`AGG_HEADERS_ZH`),收官流程(finishflow)据此按批次
算账落盘。金额仍为提示值口径(以账单为准,红线 18)。

A232 批预算熔断(每批费用口径,哨兵不是执行器):新增纯函数
:func:`check_budget`(``run_id`` + ``max_cost`` → :class:`BudgetStatus`,
含 unpriced 诚实计数与中文建议文案;``max_cost=None`` 即关闭)与
:meth:`CostMeter.check_budget` 账本级便捷读侧;收官流程(finishflow)在
批次成本归集步之后按 cfg 附加属性 ``batch_cost_budget``(getattr 缺省
None = 不检查)做超限告警——**只告警 + 标记 + 建议停止后续扫描,绝不
删除已产出的证据包与成本账、绝不改复核队列状态**(哨兵不是执行器,
是否继续由人工决定)。

A232 run_id 全链透传·接线指引(**领地外待办,如实说明**):截至本任务,
vision 调用链(``vlmctl`` / ``vlm_client.UniversalVLMClient`` /
``multi_provider`` / ``parallel_classify`` 等,均在 cost_meter 领地之外)
**尚不存在任何 ``cost_meter.record()`` 调用点**——金额侧账本
``vlm_cost.jsonl`` 目前仅由测试与手工路径写入,故"finish() 站点扫描循环
侧把 run_id 传到 record()"在领地内无就近注入点(收官侧 finishflow 只
**读**账本聚合,从不记账)。领地外接线建议:①真实外呼统一落账点选
``vlmctl._do_ping_call`` 与 ``UniversalVLMClient.chat_json`` 返回处,按
``(provider, model, images)`` 调 ``CostMeter(<data_dir>/vlm_cost.jsonl)
.record(..., run_id=<当前批标识>)``;②run_id 携带方式二选一:沿
finishflow.finish 已有的 cfg 原对象透传链(红线 35 保证 cfg 原样转发到
scan/summary)挂附加实例属性 ``cfg.run_id``(getattr 动态读取,与 V11
前开关挂载同款),或沿 batch_scan → run_pool → run_scan 既有关键字链
显式下传;③接线前旧行/新行照常归 "(未标记批次)" 桶,既有行为零变化。

用法示例::

    from netsentinel.vision.cost_meter import CostMeter

    meter = CostMeter("data/costs.jsonl")
    meter.record("glm", "glm-5.3-flash", 20)   # -> 本次记账条目(est_cost 提示值)
    meter.summary()["total_est"]               # -> 全账本估算总额(以账单为准)
    meter.today()["unpriced_calls"]            # -> 当日无价提示调用数
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable, Iterator

from netsentinel import telemetry

__all__ = [
    "PRICE_HINTS",
    "LOCAL_FREE",
    "CostMeter",
    "BudgetStatus",
    "AGG_BY_VALUES",
    "AGG_HEADERS_ZH",
    "UNMARKED_RUN",
    "UNKNOWN_DAY",
    "aggregate",
    "check_budget",
]

logger = logging.getLogger(__name__)

#: 本地提供方:免密钥、数据不出本机,成本按本地算力记 0(红线 16)。
LOCAL_FREE = {"ollama", "vllm", "lmstudio", "xinference"}

#: 价格提示表。单位:**人民币元 / 每千次图像调用**。
#:
#: 全部为"提示值,以账单为准"(红线 18):只保留数量级合理性,刻意不写
#: 小数级精确;上线前按各平台官方定价核验一次。键为 "provider:model"。
PRICE_HINTS: dict[str, float] = {
    # 智谱:glm-4v-flash 档历史免费(以官方现行政策为准)
    "glm:glm-4v-flash": 0.0,
    "glm:glm-5.3-flash": 5.0,
    # OpenAI:mini 档约每千次个位数到十元级,旗舰档高一个量级以上
    "openai:gpt-4o-mini": 10.0,
    "openai:gpt-4o": 150.0,
    # Anthropic:旗舰档,量级显著高于 mini 档
    "anthropic:claude-sonnet-4": 300.0,
    # Google:flash 档便宜(存在免费额度,以官方为准)
    "gemini:gemini-2.0-flash": 1.0,
    # 通义:max 为旗舰档,plus 为均衡档
    "qwen:qwen-vl-max": 20.0,
    "qwen:qwen-vl-plus": 5.0,
    # 豆包 / 混元 / Kimi / 阶跃:国内平台均衡档量级
    "doubao:doubao-1.5-vision-pro": 5.0,
    "hunyuan:hunyuan-vision": 5.0,
    "moonshot:kimi-latest": 10.0,
    "stepfun:step-1v-8k": 5.0,
    # 硅基流动托管开源小模型:便宜档
    "siliconflow:Qwen/Qwen2.5-VL-7B-Instruct": 1.0,
    # OpenRouter 的 :free 后缀模型走免费池(限速,以官方为准)
    "openrouter:qwen/qwen2.5-vl-72b-instruct:free": 0.0,
}


def _now_iso() -> str:
    """当前本地时间(带时区)的 ISO 字符串,供 ts 字段与当日过滤使用。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _round2(value: float) -> float:
    """金额统一保留两位小数输出。"""
    return round(float(value), 2)


# ---------------------------------------------------------------------------
# V13 per-run 成本归集:聚合维度 / 缺维度回退桶 / 中文表头
# ---------------------------------------------------------------------------
#: :func:`aggregate` 的合法聚合维度
AGG_BY_VALUES: tuple[str, ...] = ("run_id", "model", "day")

#: run 维度回退桶:旧账目行(V13 前落盘)没有 run_id 字段,聚合时归入此桶
UNMARKED_RUN: str = "(未标记批次)"

#: day 维度回退桶:ts 缺失/不可解析的记录聚合时归入此桶
UNKNOWN_DAY: str = "(未知日期)"

#: 聚合行字段 → 中文表头(收官成本表 / 导出直接可用;键 = 行字段名)
AGG_HEADERS_ZH: dict[str, str] = {
    "run_id": "批次标识",
    "model": "模型(提供方:模型)",
    "day": "日期",
    "provider": "提供方",
    "calls": "调用次数",
    "images": "图片数",
    "tokens": "tokens",
    "duration_s": "耗时(秒)",
    "est_cost": "估算费用(元)",
    "unpriced_calls": "无价格提示调用",
}


def _as_number(value: Any) -> float | None:
    """宽容数值:int/float(排除 bool)→ float,其余(None/字符串等)→ None。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _day_key(ts: Any) -> str:
    """ts → 本地日期键 ``YYYY-MM-DD``;不可解析回退 :data:`UNKNOWN_DAY`。"""
    try:
        return _dt.datetime.fromisoformat(str(ts)).date().isoformat()
    except (TypeError, ValueError):
        return UNKNOWN_DAY


def _run_key(value: Any) -> str:
    """run_id 字段 → 聚合键;缺失 / 空 / 非标量回退 :data:`UNMARKED_RUN`。"""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            return text
    return UNMARKED_RUN


def aggregate(records: Iterable[Any], by: str = "model") -> dict[str, Any]:
    """纯聚合:把账目行按 ``by`` 维度归桶(**纯函数,离线可单测**,不触文件/遥测)。

    维度与键口径:

    - ``by="run_id"``:行键 = 记录的 ``run_id``;缺失(旧账目行)归
      :data:`UNMARKED_RUN` 桶(向后兼容读旧行);
    - ``by="model"``:行键 = ``"provider:model"``(与 :data:`PRICE_HINTS`
      键口径一致),行额外携带 ``provider`` 字段;
    - ``by="day"``:行键 = ``ts`` 的本地日期;不可解析归 :data:`UNKNOWN_DAY`。

    累计口径(与 :meth:`CostMeter.summary` 对齐):

    - 每行 ``calls`` +1、``images`` 累加;``tokens`` / ``duration_s`` 缺失
      或非数值按 0 累计(可选维度,可得时才记);
    - ``est_cost`` 为数值则累加,``null``/缺失按 0 求和并计入
      ``unpriced_calls``(不猜价,以账单为准,红线 18);
    - 非映射 / 核心字段(provider/model/images)不合法的记录整行跳过
      (与 :meth:`CostMeter._iter_records` 同口径)。

    返回 ``{"by", "rows", "totals", "headers_zh"}``:

    - ``rows``:按维度键**升序**(确定性),每行 ``{<维度键字段>, …,
      calls/images/tokens/duration_s/est_cost/unpriced_calls}``;
    - ``totals``:全量合计(字段同 rows 去掉维度键);
    - ``headers_zh``::data:`AGG_HEADERS_ZH` 副本(字段 → 中文表头)。

    ``by`` 非法时抛中文 :class:`ValueError`。
    """
    if by not in AGG_BY_VALUES:
        raise ValueError(f"聚合维度 by 必须是 {AGG_BY_VALUES} 之一:{by!r}")
    slots: dict[str, dict[str, Any]] = {}
    totals: dict[str, Any] = {
        "calls": 0, "images": 0, "tokens": 0, "duration_s": 0.0,
        "est_cost": 0.0, "unpriced_calls": 0,
    }
    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        provider = rec.get("provider")
        model = rec.get("model")
        images = rec.get("images")
        if not (
            isinstance(provider, str) and bool(provider)
            and isinstance(model, str)
            and isinstance(images, int) and not isinstance(images, bool)
            and images >= 0
        ):
            continue  # 与 _iter_records 同口径:核心字段不合法整行跳过
        if by == "run_id":
            key = _run_key(rec.get("run_id"))
        elif by == "model":
            key = f"{provider}:{model}"
        else:  # by == "day"
            key = _day_key(rec.get("ts"))
        slot = slots.get(key)
        if slot is None:
            slot = {
                by: key, "calls": 0, "images": 0, "tokens": 0,
                "duration_s": 0.0, "est_cost": 0.0, "unpriced_calls": 0,
            }
            if by == "model":
                slot["provider"] = provider
            slots[key] = slot
        tokens = _as_number(rec.get("tokens"))
        duration = _as_number(rec.get("duration_s"))
        est = _as_number(rec.get("est_cost"))
        slot["calls"] += 1
        slot["images"] += int(images)
        totals["calls"] += 1
        totals["images"] += int(images)
        if tokens is not None:
            slot["tokens"] += tokens
            totals["tokens"] += tokens
        if duration is not None:
            slot["duration_s"] += duration
            totals["duration_s"] += duration
        if est is not None:
            slot["est_cost"] += est
            totals["est_cost"] += est
        else:
            # est_cost 为 null/缺失:按 0 参与求和,但单独计数(不猜价)
            slot["unpriced_calls"] += 1
            totals["unpriced_calls"] += 1

    def _finalize(bucket: dict[str, Any]) -> dict[str, Any]:
        bucket["tokens"] = int(round(bucket["tokens"]))
        bucket["duration_s"] = round(float(bucket["duration_s"]), 3)
        bucket["est_cost"] = _round2(bucket["est_cost"])
        return bucket

    rows = [_finalize(slots[key]) for key in sorted(slots)]
    return {
        "by": by,
        "rows": rows,
        "totals": _finalize(totals),
        "headers_zh": dict(AGG_HEADERS_ZH),
    }


# ---------------------------------------------------------------------------
# A232 批预算熔断(每批费用口径;哨兵不是执行器——只判定与建议)
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class BudgetStatus:
    """批预算检查结果(纯数据;构造见 :func:`check_budget`)。

    字段:``over``(是否超限:批次 est_cost 总额 **严格大于** max_cost)/
    ``spent``(该批估算费用,元,两位小数,不含 unpriced 调用)/
    ``max_cost``(本批预算上限,元)/ ``unpriced_calls``(该批无价格提示
    调用数——**诚实计数**,这些调用未计入 spent,实际费用可能更高)/
    ``calls``(该批有效调用次数)/ ``advice``(中文建议文案)。
    """

    over: bool
    spent: float
    max_cost: float
    unpriced_calls: int
    calls: int
    advice: str


def _budget_advice(
    run_key: str, over: bool, spent: float, max_cost: float,
    unpriced: int, calls: int,
) -> str:
    """组装中文建议文案(超限 → 醒目处置建议;未超 → 口径提示)。"""
    if over:
        parts = [
            f"批次「{run_key}」估算费用 {spent:.2f} 元已超批预算 {max_cost:.2f} 元"
            f"(超出 {round(spent - max_cost, 2):.2f} 元,共 {calls} 次调用)。",
            "建议停止发起后续扫描,人工核查本批成本账后再决定;"
            "本检查只是哨兵——已产出的证据包与复核队列不受任何影响。",
        ]
        if unpriced:
            parts.append(
                f"另有 {unpriced} 次无价格提示调用未计入上述金额,"
                "实际费用可能更高(以账单为准,红线 18)。"
            )
        return "".join(parts)
    parts = [
        f"批次「{run_key}」估算费用 {spent:.2f} 元未超批预算 "
        f"{max_cost:.2f} 元(提示值口径,以账单为准)。"
    ]
    if unpriced:
        parts.append(
            f"另有 {unpriced} 次无价格提示调用未计入(以账单为准,红线 18)。"
        )
    return "".join(parts)


def check_budget(
    records: Iterable[Any] | None,
    *,
    run_id: str | int | None,
    max_cost: float | None,
) -> BudgetStatus | None:
    """批预算检查(**纯函数,离线可单测**,不触文件与遥测;哨兵不是执行器)。

    口径:

    - 只统计 ``run_id`` 匹配该批的记录(按字符串等值,与
      :meth:`CostMeter.aggregate(run_id=...)` 过滤同口径;**注意语义差异**:
      那里 ``run_id=None`` 表示"不过滤全账本",而这里 ``run_id=None``
      表示预算 **"(未标记批次)" 桶**——即统计没有 run_id 的旧账目行,
      与 :func:`aggregate` 的缺维度回退一致);
    - ``spent`` = 该批 ``est_cost`` 总额(经 :func:`aggregate` 规整,两位
      小数);``est_cost`` 为 null/缺失的调用**不计入金额、单独计入
      ``unpriced_calls``**(不猜价,以账单为准,红线 18);
    - ``over`` = ``spent > max_cost``(严格大于:刚好用满不算超限);
    - 核心字段不合法的记录整行跳过(与 :func:`aggregate` 同口径)。

    参数:

    - ``max_cost=None`` → 返回 ``None``(**关闭检查**,调用方零行为);
      非数值(bool 除外规则:bool 不算数值)或负数 → 中文
      :class:`ValueError`(预算是人工配置,配错应当场报错而不是静默跳过)。

    返回 :class:`BudgetStatus`(含中文建议文案);**本函数只做判定与建议,
    不删除任何文件、不改任何队列状态**——熔断执行(停止后续扫描)由
    人工或调用方决定(哨兵不是执行器)。

    用法示例::

        st = check_budget(rows, run_id="run-a", max_cost=12.0)
        st.over            # -> False
        st.spent           # -> 5.0(该批 est_cost 总额,不含 unpriced)
        st.unpriced_calls  # -> 1(无价格提示调用,诚实单独计数)
        check_budget(rows, run_id="run-a", max_cost=None)  # -> None(关闭)
    """
    if max_cost is None:
        return None  # 关闭检查:调用方按零行为处理
    if isinstance(max_cost, bool) or not isinstance(max_cost, (int, float)):
        raise ValueError(f"批预算 max_cost 必须是非负数值(元,提示值口径):{max_cost!r}")
    budget = float(max_cost)
    if budget < 0:
        raise ValueError(f"批预算 max_cost 必须是非负数值(元,提示值口径):{max_cost!r}")
    wanted = _run_key(run_id)  # None/缺失 → "(未标记批次)" 桶
    matched = [
        rec for rec in (records or ())
        if isinstance(rec, Mapping) and _run_key(rec.get("run_id")) == wanted
    ]
    # 复用 aggregate 的累计/容错口径(非法行跳过、unpriced 单独计数、两位小数)
    totals = aggregate(matched, by="run_id")["totals"]
    spent = float(totals["est_cost"])
    unpriced = int(totals["unpriced_calls"])
    calls = int(totals["calls"])
    over = spent > budget
    return BudgetStatus(
        over=over,
        spent=spent,
        max_cost=budget,
        unpriced_calls=unpriced,
        calls=calls,
        advice=_budget_advice(wanted, over, spent, budget, unpriced, calls),
    )


class CostMeter:
    """jsonl 追加式成本账本:record 落盘、estimate 估算、summary/today 汇总。"""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self._lock = threading.Lock()

    # -- 估算 -------------------------------------------------------------
    def estimate(self, provider: str, model: str, images: int) -> float | None:
        """按提示值估算一次调用成本(人民币,两位小数)。

        - 本地提供方 → ``0.0``(本地算力,不出本机);
        - ``PRICE_HINTS`` 精确键 ``provider:model`` → 单价 × images / 1000;
        - 无提示 → ``None``(不猜价,以账单为准)。
        """
        if provider in LOCAL_FREE:
            return 0.0
        key = f"{provider}:{model}"
        price = PRICE_HINTS.get(key)
        if price is None:
            logger.debug("无价格提示(以账单为准,计入 unpriced_calls):%s", key)
            return None
        return _round2(price * images / 1000.0)

    # -- 记账 -------------------------------------------------------------
    def record(
        self,
        provider: str,
        model: str,
        images: int,
        *,
        run_id: str | int | None = None,
        tokens: int | float | None = None,
        duration_s: float | int | None = None,
    ) -> dict[str, Any]:
        """记一次调用:估算结果一并落盘(无提示时 ``est_cost=null``)。

        V13 per-run 可选维度(**全部缺省不写键——不传时不改变既有行形态**):

        - ``run_id``:批次/收官标识(收官成本归集按此分批);缺省沿用现状
          (不写该键,聚合时归 "(未标记批次)" 桶);
        - ``tokens``:可得时记录(部分平台按 token 计费,仍以账单为准);
        - ``duration_s``:本次调用耗时(秒,三位小数)。

        每次成功落盘计 ``telemetry.inc("cost.record")``(V5 可观测性)。
        返回写入的那条记录(dict),便于调用方核账。
        """
        est = self.estimate(provider, model, images)
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "provider": provider,
            "model": model,
            "images": int(images),
            "est_cost": est,
        }
        # V13:可选维度仅在显式给定時計入行(缺省行形态与旧版逐字节一致)
        if run_id is not None:
            entry["run_id"] = str(run_id)
        if tokens is not None:
            entry["tokens"] = int(tokens)
        if duration_s is not None:
            entry["duration_s"] = round(float(duration_s), 3)
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        telemetry.inc("cost.record")
        if est is None:
            logger.info(
                "已记账(无价格提示,以账单为准):%s:%s × %d 图", provider, model, images
            )
        else:
            logger.debug(
                "已记账:%s:%s × %d 图,提示成本 %.2f 元", provider, model, images, est
            )
        return entry

    # -- 汇总 -------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """全量汇总(V5:流式逐行读取,不整载账本)。

        返回 ``{"by_provider": {p: {"calls","images","est_cost","unpriced_calls"}},
        "total_est": float, "unpriced_calls": int}``;
        ``est_cost`` 为 null 的记录按 0 参与求和,但计入 ``unpriced_calls``。
        """
        return self._aggregate(self._iter_records())

    def today(self) -> dict[str, Any]:
        """仅统计当日(本地时区)记录,返回结构与 :meth:`summary` 相同。

        ts 缺失或不可解析的记录无法判定归属日期,不计入当日(仍在 summary 中)。
        """
        day = _dt.date.today()
        kept = []
        for rec in self._iter_records():
            try:
                rec_day = _dt.datetime.fromisoformat(str(rec.get("ts"))).date()
            except (TypeError, ValueError):
                logger.debug("记录 ts 不可解析,不计入当日:%r", rec.get("ts"))
                continue
            if rec_day == day:
                kept.append(rec)
        return self._aggregate(kept)

    # -- 聚合(V13 per-run 成本归集) --------------------------------------
    def aggregate(self, by: str = "model", run_id: str | int | None = None) -> dict[str, Any]:
        """账本级聚合(纯读侧):按 ``by`` 维度聚合当前账本。

        - ``run_id`` 给定时只统计该批次的记录(按字符串等值比较;
          无 ``run_id`` 字段的旧行不匹配任何显式批次);
        - ``run_id`` 缺省(现状口径)统计**全账本**,含旧行(归
          "(未标记批次)" 桶)——与 :meth:`summary` 一样只读、不遥测;
        - 维度键、缺维度回退与返回结构见模块级纯函数 :func:`aggregate`。
        """
        records: list[dict] = list(self._iter_records())
        if run_id is not None:
            wanted = str(run_id)
            records = [
                rec for rec in records
                if rec.get("run_id") is not None and str(rec["run_id"]) == wanted
            ]
        return aggregate(records, by=by)

    def check_budget(
        self, *, run_id: str | int | None, max_cost: float | None
    ) -> BudgetStatus | None:
        """账本级批预算检查(便捷读侧):对当前账本做纯函数 :func:`check_budget`。

        - 只读账本、不落盘、不遥测;``max_cost=None`` → ``None``(关闭);
        - ``run_id=None`` 语义同纯函数:预算 **"(未标记批次)" 桶**(与
          :meth:`aggregate` 的"缺省不过滤"不同,见纯函数说明);
        - 判定与建议口径(over 严格大于 / unpriced 诚实计数 / 两位小数)
          与纯函数逐字段一致;**只判定不执行**——哨兵不是执行器。
        """
        # 方法名与模块级纯函数同名:方法体内裸名解析走模块全局(类作用域
        # 不参与),故此处调用的正是模块级 check_budget 纯函数。
        return check_budget(
            list(self._iter_records()), run_id=run_id, max_cost=max_cost
        )

    # -- 内部 -------------------------------------------------------------
    def _iter_records(self) -> Iterator[dict]:
        """流式逐行产出全部有效账目(生成器;V5:不整载文件、with 保证句柄释放)。

        损坏行(非 JSON / 非 dict / 字段缺失或类型不对)跳过并告警;
        读取中途 OSError 时告警并停止迭代(已产出记录保留)。
        """
        if not self.state_path.exists():
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                for lineno, raw in enumerate(fh, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "成本账本第 %d 行损坏,已跳过:%r", lineno, line[:80]
                        )
                        continue
                    if not isinstance(data, dict):
                        logger.warning(
                            "成本账本第 %d 行不是对象,已跳过:%r", lineno, line[:80]
                        )
                        continue
                    if not self._valid_record(data):
                        logger.warning(
                            "成本账本第 %d 行字段缺失或类型不对,已跳过:%r",
                            lineno, line[:80],
                        )
                        continue
                    yield data
        except OSError as exc:
            logger.warning(
                "成本账本读取失败,已停止读取(已读部分保留)(%s):%s",
                self.state_path, exc,
            )

    @staticmethod
    def _valid_record(data: dict) -> bool:
        provider = data.get("provider")
        model = data.get("model")
        images = data.get("images")
        return (
            isinstance(provider, str)
            and bool(provider)
            and isinstance(model, str)
            and isinstance(images, int)
            and not isinstance(images, bool)
            and images >= 0
        )

    @staticmethod
    def _aggregate(records: Iterable[dict]) -> dict[str, Any]:
        by_provider: dict[str, dict] = {}
        for rec in records:
            slot = by_provider.setdefault(
                rec["provider"],
                {"calls": 0, "images": 0, "est_cost": 0.0, "unpriced_calls": 0},
            )
            slot["calls"] += 1
            slot["images"] += int(rec["images"])
            est = rec.get("est_cost")
            if isinstance(est, (int, float)) and not isinstance(est, bool):
                slot["est_cost"] += float(est)
            else:
                # est_cost 为 null/缺失:按 0 参与求和,但单独计数
                slot["unpriced_calls"] += 1
        for slot in by_provider.values():
            slot["est_cost"] = _round2(slot["est_cost"])
        return {
            "by_provider": dict(sorted(by_provider.items())),
            "total_est": _round2(sum(s["est_cost"] for s in by_provider.values())),
            "unpriced_calls": sum(s["unpriced_calls"] for s in by_provider.values()),
        }
