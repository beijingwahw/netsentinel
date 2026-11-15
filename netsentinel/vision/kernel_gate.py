"""内核门禁执行器(V10.37,P2-6)——「门禁通过且运营者批准前不开」从纪律变成制度。

背景:SPRT 实时早停(P2-6)是性能重构提案里**唯一动准确率**的项——早停
改变「哪些图被评分」,其 α/β 错误率担保以**校准概率**为前提
(decision/sprt.py 校准假设)。V10.33 建成测量与门禁体系
(``vision/calibration.py`` 五判据 ``sprt_gate``),V10.36 建成标注工作台;
但「门禁通过 + 运营者批准后才可开」此前只写在文档里,**没有任何代码强制**
——本模块就是那道强制:

三道锁(全部可复算,任何一道缺失即拒开):

1. **测量锁**:`<data_dir>/calibration/report_<模型>.json` 存在且
   ``gate.passed == True``——开启的模型必须被**这一份**报告测过且五判据
   全过(报告模型 ≠ 当前主分类器 = 没测过 = 拒);
2. **批准锁**:`<data_dir>/calibration/kernel_approval.json` 存在,且
   ``kernel`` / ``model`` 与本次请求一致——批准是一次**显式人工动作**
   (``calibration approve`` 子命令,报告未过时拒绝写入),绝不自动生成;
3. **指纹锁**:批准文件里的 ``digest`` 必须等于当前报告的
   :func:`gate_digest`——批准与「被测量的那一份数据」绑定,模型重测 /
   标注集变化导致的报告更新会换指纹,**旧批准自动失效**(想再开必须
   重新人批一次)。

保守边界(如实登记,宁严勿松):

- **多成员集成未测不开**:SPRT 消费的是融合概率,校准测量
  (calibration score)只测过单一模型;``ensemble_members`` 含主分类器
  之外的成员时拒开(整环校准体系建成后再放行);
- 门禁**只读本地文件**,零外呼(红线 20:绝不自动 ping);
- 拒开 ≠ 拒扫:调用方(orchestrator)收到 ``allowed=False`` 时按「不开」
  降级为全量送审旧行为并响亮告警——扫描本身不被门禁阻塞。

本模块不 import 任何兄弟模块的私有符号;报告/评分文件的读写口径与
``vision/calibration.py`` CLI 完全一致(report 子命令 V10.37 起在 md 之外
同时落机读 JSON,即本模块的事实源)。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsentinel.contracts import Config

__all__ = [
    "APPROVAL_FILENAME",
    "GATED_KERNELS",
    "KernelGateResult",
    "gate_digest",
    "report_json_path",
    "approval_path",
    "load_report",
    "load_approval",
    "evaluate_kernel_gate",
    "write_approval",
]

logger = logging.getLogger(__name__)

#: 批准文件名(位于 ``<data_dir>/calibration/`` 下,与标注集/报告同目录)。
APPROVAL_FILENAME = "kernel_approval.json"

#: 已登记的受门禁内核(批准文件的 ``kernel`` 字段必须落在其中;新内核
#: 接入时在此登记——cascade 等待整环校准体系后加入)。
GATED_KERNELS: tuple[str, ...] = ("sprt",)

#: 报告/批准/标注集共用的根目录名(calibration CLI 同款约定)。
_CALIBRATION_DIR = "calibration"


def _model_tag(model: str) -> str:
    """模型名 → 文件名安全标签(与 calibration CLI ``_model_tag`` 同口径)。"""
    return "".join(ch if ch.isalnum() else "_" for ch in model)[:40] or "model"


@dataclass
class KernelGateResult:
    """门禁判定结果(每条 reason 对应一个人工可核验的事实)。"""

    kernel: str
    model: str
    allowed: bool = False
    reasons: list[str] = field(default_factory=list)
    gate_digest: str = ""
    report_path: str = ""
    approval_path: str = ""
    approved_at: str = ""

    def as_intel(self) -> dict[str, Any]:
        """intel 摘要(红线 17:只含名称/布尔/短句理由,不含路径内容)。"""
        info: dict[str, Any] = {"kernel": self.kernel, "allowed": self.allowed}
        if not self.allowed and self.reasons:
            info["reason"] = ";".join(self.reasons)
        if self.approved_at:
            info["approved_at"] = self.approved_at
        return info


def gate_digest(model: str, gate: dict[str, Any]) -> str:
    """测量指纹:模型 + 五判据结论的稳定 JSON → sha256。

    稳定序列化(``sort_keys`` + 紧凑分隔符)保证同一份报告在任一进程/
    平台算出同一指纹;判据的 ``detail`` 文本含具体数字(ECE/Brier/样本
    量),标注集或评分变化 → 判据 detail 变化 → 指纹变化 → 旧批准失效。
    """
    payload = json.dumps(
        {"model": model, "gate": gate},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def report_json_path(data_dir: str | Path, model: str) -> Path:
    """模型 → 机读报告路径(``<data_dir>/calibration/report_<tag>.json``)。"""
    return Path(data_dir) / _CALIBRATION_DIR / f"report_{_model_tag(model)}.json"


def approval_path(data_dir: str | Path) -> Path:
    """批准文件路径(全内核共用一份,按 kernel/model/digest 字段区分)。"""
    return Path(data_dir) / _CALIBRATION_DIR / APPROVAL_FILENAME


def load_report(data_dir: str | Path, model: str) -> dict[str, Any] | None:
    """读机读报告;缺失/损坏返回 None(调用方给出中文理由)。"""
    p = report_json_path(data_dir, model)
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def load_approval(data_dir: str | Path) -> dict[str, Any] | None:
    """读批准文件;缺失/损坏返回 None。"""
    p = approval_path(data_dir)
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def evaluate_kernel_gate(
    cfg: Config, kernel: str = "sprt", *, data_dir: str | Path | None = None
) -> KernelGateResult:
    """逐锁判定:测量锁 → 批准锁 → 指纹锁(全部可复算;零外呼)。

    :param cfg:    全局配置(读 classifier / ensemble_members / data_dir);
    :param kernel: 受门禁内核名(必须在 :data:`GATED_KERNELS` 内);
    :param data_dir: 覆盖数据目录(缺省取 ``cfg.data_dir``;测试用);
    :return:       :class:`KernelGateResult`;``allowed=True`` 当且仅当
                   三道锁全过;拒开时 ``reasons`` 逐条中文列出。
    :raises ValueError: kernel 未登记时(中文)。
    """
    if kernel not in GATED_KERNELS:
        raise ValueError(f"未登记的内核 {kernel!r}(受门禁内核:{GATED_KERNELS})")

    root = Path(data_dir) if data_dir is not None else Path(
        getattr(cfg, "data_dir", "data")
    )
    model = str(getattr(cfg, "classifier", "") or "")
    res = KernelGateResult(
        kernel=kernel,
        model=model,
        report_path=str(report_json_path(root, model)),
        approval_path=str(approval_path(root)),
    )

    # —— 前置:配置形态(没配模型 / 多成员集成未测,SPRT 担保无从谈起)——
    if not model:
        res.reasons.append("未配置主分类器(classifier),无从对齐校准测量")
    extra = [m for m in (getattr(cfg, "ensemble_members", None) or []) if m != model]
    if extra:
        res.reasons.append(
            f"集成成员含未测模型 {','.join(map(str, extra))}:校准测量只覆盖单一"
            "主分类器,融合概率的 SPRT 担保未建立(整环校准后再开)"
        )

    # —— 锁一:测量锁(报告存在 + 模型一致 + 五判据全过)——
    report = load_report(root, model) if model else None
    if model:
        if report is None:
            res.reasons.append(
                f"校准报告缺失或损坏:{report_json_path(root, model)}"
                "(先 calibration score → calibration report 落机读 JSON)"
            )
        else:
            if str(report.get("model", "")) != model:
                res.reasons.append(
                    f"报告测的模型是 {report.get('model')!r},不是当前主分类器"
                    f" {model!r}(换模型必须重测重批)"
                )
            gate = report.get("gate")
            if not isinstance(gate, dict) or gate.get("passed") is not True:
                failed = ""
                if isinstance(gate, dict):
                    names = [
                        str(c.get("criterion", "?"))
                        for c in gate.get("criteria", [])
                        if not c.get("pass")
                    ]
                    if names:
                        failed = f"(未过判据:{','.join(names)})"
                res.reasons.append(
                    f"门禁五判据未全过{failed}:维持关闭,补齐标注后复测"
                )
            else:
                res.gate_digest = gate_digest(model, gate)

    # —— 锁二 + 锁三:批准锁(显式人工动作)与指纹锁(绑死这一份数据)——
    approval = load_approval(root)
    if approval is None:
        res.reasons.append(
            f"无批准文件:{approval_path(root)}"
            "(门禁通过后由运营者执行 calibration approve 显式批准)"
        )
    else:
        res.approved_at = str(approval.get("approved_at", "") or "")
        if str(approval.get("kernel", "")) != kernel:
            res.reasons.append(
                f"批准文件的内核是 {approval.get('kernel')!r},不是 {kernel!r}"
            )
        if str(approval.get("model", "")) != model:
            res.reasons.append(
                f"批准文件的模型是 {approval.get('model')!r},不是当前主分类器"
                f" {model!r}(换模型必须重新批准)"
            )
        digest = str(approval.get("digest", "") or "")
        if res.gate_digest and digest != res.gate_digest:
            res.reasons.append(
                "批准指纹与当前报告不一致(模型已重测或标注集已变化):"
                "旧批准自动失效,需重新 calibration report → approve"
            )

    res.allowed = not res.reasons
    return res


def write_approval(
    data_dir: str | Path,
    kernel: str,
    model: str,
    note: str = "",
) -> Path:
    """写入批准文件(**运营者的显式动作**,approve CLI 的落盘器)。

    批准必须锚定一份**通过的**测量:当前模型的机读报告存在、模型一致、
    五判据全过,否则中文 :class:`ValueError` 拒绝写入——未过门禁的批准
    不存在,这与「开启需门禁 + 批准」的制度闭环一致(先有测量锁,才可能
    有批准锁)。重跑 approve 覆盖更新(批准永远是**最新测量**的快照)。

    :return: 批准文件路径(原子落盘:tmp + replace_with_retry,Windows 安全)。
    """
    if kernel not in GATED_KERNELS:
        raise ValueError(f"未登记的内核 {kernel!r}(受门禁内核:{GATED_KERNELS})")
    report = load_report(data_dir, model)
    if report is None:
        raise ValueError(
            f"校准报告缺失或损坏:{report_json_path(data_dir, model)}"
            "(先 score → report,批准必须锚定一份存在的测量)"
        )
    if str(report.get("model", "")) != model:
        raise ValueError(
            f"报告测的模型是 {report.get('model')!r},与请求的 {model!r} 不一致"
        )
    gate = report.get("gate")
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        raise ValueError(
            "门禁五判据未全过,拒绝批准(先补齐人工标注复测:"
            "python -m netsentinel.vision.calibration report --scores ...)"
        )

    from netsentinel import timesource
    from netsentinel.atomic import replace_with_retry

    payload = {
        "kernel": kernel,
        "model": model,
        "digest": gate_digest(model, gate),
        "approved_at": timesource.now_iso(),
        "note": note,
    }
    target = approval_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    replace_with_retry(tmp, target)
    logger.info(
        "内核批准已落盘:kernel=%s model=%s digest=%s...",
        kernel, model, payload["digest"][:12],
    )
    return target
