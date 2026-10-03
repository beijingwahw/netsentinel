"""净网哨兵 · 门户草稿工作流页(A220,独立入口,不改动既有 webui 页面)。

定位:把 A209 ``form_scanner`` 的门户 YAML 草稿生成能力摆上复核台,补齐
"**扫描 → 人工确认 → 落位**"三步工作流的最后两步——本页之前,草稿只能
经 CLI 打印 / 写到项目外文件,人工确认与放入 ``portals/`` 目录全靠手工;
本页把确认过程结构化为可离线单测的状态机,**任何草稿都必须经显式人工
确认动作才允许落位**。

A233 闭环升级:落位即登记(自动写 ``enabled: false`` 头),配合
``netsentinel.submit.portal_defs`` 的动态发现
(``discover_portals`` / ``list_portals`` / ``get_portal``)构成完整
登记闭环——**落位 ≠ 生效**:文件以 disabled 登记,人工核验后把
``enabled`` 改 ``true``(或经本页「口令确认启用」/
:func:`confirm_enable_portal` 复述门户名确认)方自动生效,全程无需
代码登记 ``PORTAL_FILES``。

安全红线(违反即失败,必须体现在代码里):
- **草稿绝不自动生效**:落位前必须 (1) 全部契约字段逐项人工勾选确认
  (默认全不勾,置信度只作参考高亮,**绝不自动勾选**);(2) 确认声明
  原样复述门户名;(3) 落位口令「确认落位」二次确认。状态机
  (:class:`DraftConfirmFlow`)非法跳转一律拒绝,:func:`persist_draft`
  在纯逻辑层重复校验全部门槛(UI 按钮禁用只是第一道,不是唯一一道);
- **落位登记为 disabled,绝不自动生效**(A233,门户侧红线):
  ``persist_draft`` 写入的 YAML 头部自动带 ``enabled: false`` 与人工
  确认指引注释;生效必须再走一次**显式人工确认**——人工把 ``enabled``
  改 ``true``,或 :func:`confirm_enable_portal` 显式口令(原样复述
  门户名)确认(写前对新文本再跑 ``load_portal_def`` 全链校验,失败
  零残留);状态机对应 ``placed → enabled`` 新转移,仍无任何批量捷径;
- **落位前全链校验**:草稿 YAML(含登记头)先写入临时文件经
  :func:`netsentinel.submit.portal_defs.load_portal_def` 全链校验
  (schema v2 候选链 / 验证码别名红线 / entry_url_key),校验失败拒绝
  写入并展示原因,**绝不把非法 YAML 写进 portals/**;
- **落位路径仅项目 ``portals/`` 目录**:目标文件必须位于 portals 目录
  直下(短名只允许字母数字连字符下划线,拒绝路径穿越),同名文件
  拒绝覆盖(防静默替换既有门户定义);
- **零外呼**:扫描只接受本地 HTML(上传内容或本地文件路径,URL 一律
  拒绝,与 form_scanner CLI 同口径),本页不发起任何网络请求。

结构约定(与 tests/test_draft_flow_page.py 对应,webui 双段结构铁律):
- 纯逻辑层(本文件上半部分,无 streamlit,兄弟模块一律函数内惰性导入,
  可独立导入、离线单测):
  * :func:`draft_parse`          HTML → 草稿包(YAML 文本 + 字段映射行 +
    警告列表),包装 A209 scan_html / generate_draft;
  * :class:`DraftConfirmFlow`    逐项人工确认状态机
    (confirming → ready → placed → enabled,非法跳转拒绝,无任何批量
    确认捷径);
  * :func:`persist_draft`        落位:门槛复查 → 注入 disabled 登记头 →
    load_portal_def 全链校验(临时文件)→ 写 portals/<短名>.yaml →
    状态机封存(placed);
  * :func:`placed_notice`        落位成功提示文案(已登记为 disabled,
    人工改 enabled 后自动生效);
  * :func:`confirm_enable_portal` 启用确认:复述门户名口令 → 全链校验 →
    enabled 改 true(同目录临时文件原子替换,失败零残留)→ 状态机
    placed → enabled。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 三页签:①扫描(离线)②人工逐字段确认(默认全不勾
    + 声明复述)③落位(全链校验 + 二次确认口令 + 落位后口令启用)。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netsentinel import telemetry

# ---------------------------------------------------------------------------
# streamlit 惰性导入:缺失时纯逻辑层仍可被测试导入(UI 在 main() 里拦截)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    import streamlit as st

    _HAS_ST = True
except ImportError:  # pragma: no cover - 取决于运行环境
    st = None  # type: ignore[assignment]
    _HAS_ST = False

__all__ = [
    "PERSIST_PHRASE",
    "PHASE_CONFIRMING",
    "PHASE_READY",
    "PHASE_PLACED",
    "PHASE_ENABLED",
    "DraftBundle",
    "draft_parse",
    "DraftConfirmFlow",
    "persist_draft",
    "placed_notice",
    "confirm_enable_portal",
    "render",
    "main",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 落位二次确认口令(写入 portals/ 前必须原样输入;纯逻辑层硬校验)。
PERSIST_PHRASE: str = "确认落位"

#: 状态机四阶段:确认中(未全确认)→ 就绪(全确认+声明复述)→
#: 已落位(封存,disabled 登记)→ 已启用(enabled: true,人工确认生效)。
PHASE_CONFIRMING: str = "confirming"
PHASE_READY: str = "ready"
PHASE_PLACED: str = "placed"
PHASE_ENABLED: str = "enabled"

#: 门户短名(落位文件名):字母/数字开头,仅字母数字连字符下划线,防路径穿越。
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: 落位登记头(A233):persist_draft 写 YAML 时自动注入文件头部——
#: ``enabled: false`` + 人工确认指引注释。语义:落位 = 登记 + disabled,
#: **绝不自动生效**;动态发现(portal_defs.discover_portals / list_portals /
#: get_portal)只认显式 ``enabled: true`` 的人工确认标记。
_REGISTRATION_HEADER: str = (
    "# ---- 动态门户登记(draft_flow_page 落位自动写入)----\n"
    "# 本文件以 enabled: false 登记:未经人工确认,动态发现绝不把它\n"
    "# 当作可用提交通道(discover_portals 返回但标注 disabled,\n"
    "# list_portals / get_portal 不加载)。\n"
    "# 人工确认指引:逐字段核验无误后,把下方 enabled 改为 true\n"
    "# (或经草稿工作流页「口令确认启用」/ confirm_enable_portal\n"
    "# 复述门户名确认),新门户即自动生效,无需代码登记。\n"
    "enabled: false\n"
)

#: 顶层 enabled 键行(YAML 1.1 假值族:false/no/off 及大小写变体;
#: 启用改写只动值本身,键后间隔与行尾注释逐字保留)。
_TOP_ENABLED_FALSE_RE = re.compile(
    r"(?m)^(?P<key>enabled[ \t]*:[ \t]*)(?i:false|no|off)(?P<trail>[ \t]*\r?(?:#.*)?)$"
)

#: 推断状态 reason → 中文展示(与 form_scanner.FieldInference.reason 对齐)。
_REASON_CN: dict[str, str] = {
    "mapped": "已识别(自动推断候选链)",
    "no-id": "无 id,已回退缺省选择器(须人工补齐)",
    "conflict-css": "id 冲突,已回退缺省选择器(须人工补齐)",
    "fallback": "页面未识别到,已回退缺省选择器",
    "captcha-excluded": "验证码红线:人工核验改写(仅限人工输入)",
}

#: 字段映射表行的固定键(顺序即展示顺序)。
_FIELD_ROW_KEYS: tuple[str, ...] = (
    "key",
    "status_cn",
    "confidence",
    "evidence",
    "chain",
    "control",
    "manual_required",
)


@dataclass
class DraftBundle:
    """一次扫描的草稿包(draft_parse 产物,确定性:同输入同输出)。

    属性:
        draft_yaml: 门户 YAML v2 草稿文本(含人工确认横幅,尚未生效)。
        portal_name: 草稿门户展示名(确认声明须原样复述该值)。
        field_rows: 字段映射表行(中文列,含置信度与推断依据)。
        warnings: 警告列表(验证码排除置顶,来自 ScanResult + 草稿层)。
        field_keys: 待逐项确认的契约字段键(契约 §3 键序)。
        mapped_count / manual_count: 已推断字段数 / 须人工补齐字段数。
        source: 来源 HTML 标识(本地路径或"上传:<文件名>",可追溯)。
    """

    draft_yaml: str
    portal_name: str
    field_rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    field_keys: list[str] = field(default_factory=list)
    mapped_count: int = 0
    manual_count: int = 0
    source: str = ""


def _chain_text(candidates: list[dict[str, str]]) -> str:
    """候选链 → 单行中文串(``#id → role+name(…) → label(…)``)。"""
    parts: list[str] = []
    for cand in candidates:
        if isinstance(cand, dict) and "css" in cand:
            parts.append(str(cand["css"]))
        elif isinstance(cand, dict) and "role" in cand:
            parts.append(f"role+name({cand.get('name', '')})")
        elif isinstance(cand, dict) and "label" in cand:
            parts.append(f"label({cand['label']})")
        elif isinstance(cand, dict) and "placeholder" in cand:
            parts.append(f"placeholder({cand['placeholder']})")
        elif isinstance(cand, dict) and "text" in cand:
            parts.append(f"text({cand['text']})")
    return " → ".join(parts)


def draft_parse(
    html_text: str,
    *,
    source: str,
    name: str | None = None,
    entry_url_key: str | None = None,
    category: str | None = None,
) -> DraftBundle:
    """HTML 文本 → 草稿包(纯逻辑层包装 A209 ``scan_html`` / ``generate_draft``)。

    参数:
        html_text: 本地表单页面 HTML 文本(调用方负责从上传内容 / 本地文件
            读取;本函数绝不联网)。
        source: 来源标识(本地路径或 ``上传:<文件名>``)。**含 URL 方案
            (``://`` 或 http(s)/file 开头)一律 ValueError 拒绝**——离线
            解析红线与 form_scanner CLI 同口径。
        name / entry_url_key / category: 门户元数据;``None`` 时沿用
            form_scanner 占位缺省(人工确认时必须改写)。

    :return: :class:`DraftBundle`(field_rows 含中文状态 / 置信度 / 推断
        依据 / 候选链列,与 UI 字段映射表一一对应)。
    :raises ValueError: source 是 URL / 页面无表单控件 / 元数据非法
        (中文消息,来自 form_scanner 同款校验)。
    """
    src = str(source).strip() if source is not None else ""
    if "://" in src or src.lower().startswith(("http://", "https://", "file://")):
        raise ValueError(
            f"来源必须是本地 HTML(上传内容或本地文件路径),禁止 URL:{src}"
            "(红线:离线解析,绝不联网抓取)"
        )
    if not isinstance(html_text, str) or not html_text.strip():
        raise ValueError("HTML 内容为空:请上传本地表单页面或填写本地文件路径")

    from netsentinel.submit import form_scanner  # noqa: PLC0415 惰性导入

    result = form_scanner.scan_html(html_text)
    if not result.controls:
        raise ValueError(
            f"未在 {src or '(未命名来源)'} 中发现任何表单控件"
            "(input/select/textarea/button),无法起草"
        )
    kwargs: dict[str, Any] = {"source": src}
    if name is not None:
        kwargs["name"] = name
    if entry_url_key is not None:
        kwargs["entry_url_key"] = entry_url_key
    if category is not None:
        kwargs["category"] = category
    draft_yaml = form_scanner.generate_draft(result, **kwargs)
    telemetry.inc("draft_flow_page.scan")

    field_rows: list[dict] = []
    field_keys: list[str] = []
    mapped = 0
    manual = 0
    for key, inf in result.fields.items():
        field_keys.append(str(key))
        if inf.reason == "mapped":
            mapped += 1
        if inf.manual_required:
            manual += 1
        control = inf.control.describe() if inf.control is not None else ""
        field_rows.append(
            {
                "key": str(key),
                "status_cn": _REASON_CN.get(str(inf.reason), str(inf.reason)),
                "confidence": round(float(inf.confidence), 2),
                "evidence": ";".join(str(e) for e in inf.evidence[:4]),
                "chain": _chain_text(list(inf.candidates)),
                "control": control,
                "manual_required": bool(inf.manual_required),
            }
        )
    portal_name = str(kwargs.get("name", form_scanner._DEFAULT_NAME))
    return DraftBundle(
        draft_yaml=draft_yaml,
        portal_name=portal_name,
        field_rows=field_rows,
        warnings=list(result.warnings),
        field_keys=field_keys,
        mapped_count=mapped,
        manual_count=manual,
        source=src,
    )


class DraftConfirmFlow:
    """门户草稿的逐项人工确认状态机(未确认 → 逐项确认 → 全确认 → 可落位)。

    四阶段(:attr:`phase`):

    - ``confirming``:尚未全部确认(初始即此态,**零个字段被确认**);
    - ``ready``:全部字段逐项确认 **且** 确认声明原样复述门户名;
    - ``placed``:落位完成(enabled: false 登记),确认状态封存,
      任何变更一律拒绝;
    - ``enabled``:落位后的启用确认完成(enabled 改 true),终态封存
      (A233 新转移,**仅** ``placed → enabled``;:func:`persist_draft`
      落位 + :func:`confirm_enable_portal` 口令确认是唯一合法路径)。

    红线语义(违反即缺陷):

    - **只有逐项确认接口**——``confirm_field`` 一次确认一个字段;本类
      刻意**不提供**任何批量 / 全选捷径(人工确认语义:每一下勾选都是
      一次显式人工动作);
    - **非法跳转拒绝**::meth:`mark_placed` 仅在 ``ready`` 态可用;
      :meth:`mark_enabled` 仅在 ``placed`` 态可用;落位或启用后
      confirm / unconfirm / set_declaration / 再次 mark_placed /
      再次 mark_enabled 全部 :class:`ValueError`(中文消息);
    - **声明复述门户名**::meth:`set_declaration` 传入文本 strip 后必须
      与门户名**完全一致**(复述 ≠ 包含);复述错误即回到未声明态。

    构造校验:门户名非空字符串;字段键非空列表且无重复(中文 ValueError)。
    """

    def __init__(self, portal_name: str, field_keys: list[str] | tuple[str, ...]) -> None:
        name = str(portal_name).strip() if portal_name is not None else ""
        if not name:
            raise ValueError(f"门户名必须是非空字符串(确认声明须复述该值),当前值:{portal_name!r}")
        keys = [str(k).strip() for k in (field_keys or [])]
        if not keys or any(not k for k in keys):
            raise ValueError("待确认字段键必须是非空列表(草稿未解析出字段,无从确认)")
        if len(set(keys)) != len(keys):
            raise ValueError(f"待确认字段键存在重复:{keys}")
        self._portal_name = name
        self._field_keys: tuple[str, ...] = tuple(keys)
        self._confirmed: set[str] = set()
        self._declared = False
        self._placed = False
        self._enabled = False

    # ------------------------------------------------------------------
    # 只读查询(无副作用,可任意调用)
    # ------------------------------------------------------------------
    @property
    def portal_name(self) -> str:
        """门户展示名(确认声明须原样复述该值)。"""
        return self._portal_name

    @property
    def field_keys(self) -> tuple[str, ...]:
        """待确认字段键(契约键序;副本,改返回值不影响内部)。"""
        return tuple(self._field_keys)

    @property
    def phase(self) -> str:
        """当前阶段::data:`PHASE_CONFIRMING` / :data:`PHASE_READY` /
        :data:`PHASE_PLACED` / :data:`PHASE_ENABLED`(启用后终态)。"""
        if self._enabled:
            return PHASE_ENABLED
        if self._placed:
            return PHASE_PLACED
        return PHASE_READY if self.can_persist() else PHASE_CONFIRMING

    def is_confirmed(self, key: str) -> bool:
        """字段是否已人工确认(未知键按未确认,不抛错——UI 查询友好)。"""
        return str(key) in self._confirmed

    def confirmed_keys(self) -> list[str]:
        """已确认字段键,按契约键序返回(确定性,与勾选顺序无关)。"""
        return [k for k in self._field_keys if k in self._confirmed]

    def missing_keys(self) -> list[str]:
        """尚未确认的字段键(契约键序)。"""
        return [k for k in self._field_keys if k not in self._confirmed]

    def declaration_ok(self) -> bool:
        """确认声明是否已原样复述门户名。"""
        return self._declared

    def can_persist(self) -> bool:
        """是否满足落位门槛:未落位 + 全字段确认 + 声明复述正确。"""
        return not self._placed and not self.missing_keys() and self._declared

    def summary(self) -> dict[str, Any]:
        """状态快照(UI 展示 / 测试断言用)::``{phase, portal_name, total,
        confirmed, missing, declaration_ok, can_persist, placed}``。"""
        return {
            "phase": self.phase,
            "portal_name": self._portal_name,
            "total": len(self._field_keys),
            "confirmed": len(self._confirmed),
            "missing": self.missing_keys(),
            "declaration_ok": self._declared,
            "can_persist": self.can_persist(),
            "placed": self._placed,
        }

    # ------------------------------------------------------------------
    # 状态转移(全部显式人工动作;落位后封存)
    # ------------------------------------------------------------------
    def _assert_mutable(self) -> None:
        if self._placed:
            raise ValueError("草稿已落位,状态机封存——确认状态不可再变更(如需调整请重新扫描)")

    def confirm_field(self, key: str) -> None:
        """人工确认单个字段(一次一个,无批量捷径)。

        :raises ValueError: 落位后确认 / 字段键不在草稿字段集内(中文消息)。
        """
        self._assert_mutable()
        k = str(key)
        if k not in self._field_keys:
            raise ValueError(f"未知字段键:{k!r}(本草稿待确认字段:{list(self._field_keys)})")
        self._confirmed.add(k)

    def unconfirm_field(self, key: str) -> None:
        """撤回单个字段的确认(仅落位前可用;撤回只会让门槛更严)。"""
        self._assert_mutable()
        k = str(key)
        if k not in self._field_keys:
            raise ValueError(f"未知字段键:{k!r}(本草稿待确认字段:{list(self._field_keys)})")
        self._confirmed.discard(k)

    def set_declaration(self, text: str) -> bool:
        """确认声明:文本 strip 后必须与门户名完全一致(复述)。

        :return: 声明是否通过;复述错误会**清除**既有通过状态(回到未声明)。
        :raises ValueError: 落位后变更声明。
        """
        self._assert_mutable()
        self._declared = (
            isinstance(text, str) and text.strip() == self._portal_name
        )
        return self._declared

    def mark_placed(self) -> None:
        """落位封存(仅 :func:`persist_draft` 写入成功后调用)。

        :raises ValueError: 未满足落位门槛(缺字段 / 声明未复述)或已落位。
        """
        if self._placed:
            raise ValueError("草稿已落位,不可重复落位")
        if not self.can_persist():
            reasons: list[str] = []
            missing = self.missing_keys()
            if missing:
                reasons.append(f"尚有 {len(missing)} 个字段未人工确认:{missing}")
            if not self._declared:
                reasons.append("确认声明未原样复述门户名")
            raise ValueError("落位门槛未满足,拒绝封存:" + ";".join(reasons))
        self._placed = True

    def is_enabled(self) -> bool:
        """启用确认是否完成(placed → enabled 转移后的终态标记)。"""
        return self._enabled

    def mark_enabled(self) -> None:
        """启用封存(A233 新转移:仅 :func:`confirm_enable_portal` 口令
        确认成功后调用;唯一合法前置是 ``placed``)。

        :raises ValueError: 未落位(无法跳过落位直接启用)/ 已启用。
        """
        if self._enabled:
            raise ValueError("草稿已启用,不可重复启用")
        if not self._placed:
            raise ValueError(
                "启用门槛未满足:仅已落位(placed)的草稿可进入 enabled 阶段"
                "(placed → enabled 是唯一合法转移,须先经 persist_draft 落位)"
            )
        self._enabled = True


def _derive_slug(portal_name: str) -> str:
    """从门户名推导落位文件短名:仅保留 ASCII 字母数字连字符下划线。"""
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(portal_name))


def _registration_yaml(draft_yaml: str) -> str:
    """给草稿 YAML 注入落位登记头(:data:`_REGISTRATION_HEADER`)。

    草稿文本已含顶层 ``enabled`` 键时拒绝(中文 ValueError)——落位登记
    的 ``enabled`` 恒由本工具写 ``false``,启用只能走
    :func:`confirm_enable_portal` 的人工确认路径;放行既有 ``enabled``
    键会造成同文件双键(YAML 后者胜),可能把未确认门户静默写成启用。
    """
    for line in draft_yaml.splitlines():
        if line.startswith("enabled") and ":" in line:
            raise ValueError(
                "草稿 YAML 已含顶层 enabled 键,拒绝落位:落位登记恒写"
                " enabled: false,启用须经人工确认(confirm_enable_portal"
                " 口令或人工改 true),不得在草稿里自带启用标记"
            )
    text = draft_yaml if draft_yaml.endswith("\n") else draft_yaml + "\n"
    return _REGISTRATION_HEADER + text


def persist_draft(
    draft_yaml: str,
    flow: DraftConfirmFlow,
    *,
    portals_dir: str | os.PathLike[str],
    confirm_phrase: str,
    slug: str | None = None,
) -> Path:
    """落位:把已全链校验的草稿写入 ``portals/<短名>.yaml`` 并封存状态机。

    门槛(纯逻辑层硬校验,UI 禁用按钮只是第一道防线):

    1. ``confirm_phrase`` 必须原样等于 :data:`PERSIST_PHRASE`(「确认落位」
       二次确认口令——**显式人工动作**,防止误触);
    2. ``flow.can_persist()`` 必须为真(全部字段逐项人工确认 + 声明复述
       门户名),否则中文 ValueError 列出缺失项;
    3. 目标路径必须位于 ``portals_dir`` **直下**:短名(显式传入或从门户
       名推导)只允许字母数字连字符下划线且字母数字开头,已存在同名文件
       **拒绝覆盖**(防静默替换既有门户定义);
    4. 草稿文本先注入 **disabled 登记头**(:data:`_REGISTRATION_HEADER`,
       ``enabled: false`` + 人工确认指引注释;草稿自带顶层 ``enabled``
       键一律拒绝),再写**临时文件**(系统临时目录,不在 portals 内)
       经 :func:`netsentinel.submit.portal_defs.load_portal_def` 全链校验
       (YAML 合法性 / schema v2 候选链 / 验证码别名红线 / entry_url_key),
       失败即拒绝写入 portals 并原样展示原因。

    成功:写入目标文件(含登记头)、``flow.mark_placed()`` 封存状态机、
    计数 ``telemetry.inc("draft_flow_page.persist")``,返回目标文件路径。
    落位 ≠ 生效:文件以 ``enabled: false`` 登记,须经
    :func:`confirm_enable_portal` 人工确认(或人工改 ``enabled: true``)
    后才会被 :func:`netsentinel.submit.portal_defs.get_portal` 动态发现加载。
    失败:portals 目录不留任何残留(临时文件已清理),状态机保持落位前
    状态(可补确认后重试)。

    :param portals_dir: 落位目录(UI 层固定传项目 ``portals/`` 目录;测试
        可传 tmp 目录——"仅项目 portals/ 目录"的约束由 UI 层构造路径保证)。
    :raises ValueError: 上述任一门槛不满足 / 草稿文本非法 / 校验失败。
    """
    if confirm_phrase != PERSIST_PHRASE:
        raise ValueError(
            f'落位口令不符:必须原样输入「{PERSIST_PHRASE}」(二次确认,当前值:{confirm_phrase!r})'
        )
    if not isinstance(draft_yaml, str) or not draft_yaml.strip():
        raise ValueError("草稿 YAML 文本为空,无从落位")
    if not flow.can_persist():
        info = flow.summary()
        raise ValueError(
            "落位门槛未满足,拒绝写入:"
            f"已确认 {info['confirmed']}/{info['total']} 个字段,"
            f"声明复述{'通过' if info['declaration_ok'] else '未通过'};"
            f"待确认:{info['missing'] or '(无)'}"
        )

    from netsentinel.submit.portal_defs import load_portal_def  # noqa: PLC0415 惰性导入

    # A233:落位 = 登记 + disabled(头部自动写 enabled: false 与确认指引)。
    final_yaml = _registration_yaml(draft_yaml)
    short = (slug if slug is not None else _derive_slug(flow.portal_name)).strip()
    if not _SLUG_RE.fullmatch(short):
        raise ValueError(
            "门户短名(落位文件名)非法:只允许字母/数字开头,仅含字母数字连字符"
            f"下划线(长度 ≤ 64),当前值:{short!r};中文门户名无法自动推导,请显式填写短名"
        )
    portals = Path(portals_dir)
    target = portals / f"{short}.yaml"
    try:
        resolved_dir = portals.resolve()
        resolved_target = target.resolve()
    except OSError as exc:  # pragma: no cover - 路径解析失败(平台相关)
        raise ValueError(f"落位目录无法解析:{portals}({exc})") from exc
    if resolved_target.parent != resolved_dir:
        raise ValueError(
            f"落位路径必须位于 portals 目录直下:{target}(拒绝路径穿越)"
        )
    if target.exists():
        raise ValueError(
            f"已存在同名门户定义,拒绝覆盖:{target}(须先人工核验并移除既有文件)"
        )

    # 全链校验:临时文件(写入含登记头的最终文本)→ load_portal_def;
    # 失败拒绝写入并透传中文原因。
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="netsentinel_draft_", suffix=".yaml")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(final_yaml)
        try:
            load_portal_def(tmp_path)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise ValueError(
                f"落位前 load_portal_def 全链校验失败,已拒绝写入 portals/:{exc}"
            ) from exc
        portals.mkdir(parents=True, exist_ok=True)
        target.write_text(final_yaml, encoding="utf-8")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 临时文件清理失败不影响主流程
            pass
    flow.mark_placed()
    telemetry.inc("draft_flow_page.persist")
    return target


def placed_notice(target: Path) -> str:
    """落位成功提示文案(纯逻辑层,UI 与测试共用;A233 登记闭环口径)。

    文案语义:落位即登记为 **disabled**,人工把 ``enabled`` 改 ``true``
    后**自动**被动态发现生效——不再要求代码登记 ``PORTAL_FILES``。
    """
    return (
        f"已落位:{target}。该门户已登记为 disabled(enabled: false)——"
        "人工核验后把文件头部的 enabled 改为 true(或复述门户名经"
        " confirm_enable_portal 口令确认),新门户即自动被 discover_portals"
        " / get_portal 发现生效,无需代码登记。"
    )


def _flip_enabled_true(text: str) -> str:
    """把 YAML 文本顶层的 ``enabled`` 假值行改写为 ``enabled: true``。

    只动值本身(键后间隔与行尾注释逐字保留,其余内容零改动);顶层无
    ``enabled`` 键时在文件头插入 ``enabled: true``(手写动态 YAML 的
    启用路径)。改写是否真正生效由调用方对结果再跑全链校验复核
    (流式单行映射等无法行级改写的形态会被显式拒绝而非静默放过)。
    """
    def _repl(match: "re.Match[str]") -> str:
        return f"{match.group('key')}true{match.group('trail')}"

    new_text, count = _TOP_ENABLED_FALSE_RE.subn(_repl, text)
    if count == 0:
        return "enabled: true\n" + text
    return new_text


def confirm_enable_portal(
    yaml_path: str | os.PathLike[str],
    phrase: str,
    *,
    flow: DraftConfirmFlow | None = None,
) -> Path:
    """启用确认(A233,placed → enabled 的显式人工动作):把 ``enabled``
    改 ``true``,使动态门户被 :func:`~netsentinel.submit.portal_defs.get_portal`
    自动发现生效。

    门槛(纯逻辑层硬校验,拒绝时**文件零残留改动**):

    1. 文件必须存在且能通过 :func:`~netsentinel.submit.portal_defs.load_portal_def`
       全链校验(YAML 合法性 / schema v2 候选链 / 验证码别名红线 /
       entry_url_key 指向 Config 现有字段);
    2. ``phrase`` 为**显式口令**:strip 后必须与定义文件里的门户名
       (``name``)**完全一致**(复述门户名,证明确认的是这一份定义);
    3. 定义不得已处于启用态(``enabled: true``)——重复启用即拒绝;
    4. 传入 ``flow`` 时:必须处于 ``placed`` 阶段且门户名与定义一致
       (状态机 placed → enabled 唯一新转移;不传 ``flow`` 则只改文件,
       供登记台对任意已核验文件使用)。

    写入(原子,零残留):顶层 ``enabled`` 行 false → true(注释与其余
    内容逐字保留;无该键则在文件头插入);改写后的**新文本**先写同目录
    临时文件再跑一次全链校验(且复核 ``enabled is True``),通过才
    ``os.replace`` 原子替换原文件——任何失败原文件保持字节不变,
    临时文件即时清理。

    成功:``flow.mark_enabled()``(若传入)、计数
    ``telemetry.inc("draft_flow_page.confirm_enable")``,返回文件路径。
    仍然无任何批量捷径:一次口令只启用一份定义文件。

    :raises ValueError: 上述任一门槛不满足 / 校验失败 / 改写未生效。
    """
    path = Path(yaml_path)
    if not path.is_file():
        raise ValueError(f"门户定义文件不存在:{path}(启用确认须指向已落位的 YAML)")

    from netsentinel.submit.portal_defs import load_portal_def  # noqa: PLC0415 惰性导入

    try:
        defn = load_portal_def(path)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise ValueError(f"启用前 load_portal_def 全链校验失败,拒绝修改:{exc}") from exc
    if not isinstance(phrase, str) or phrase.strip() != defn.name:
        raise ValueError(
            f"启用口令不符:必须原样复述门户名「{defn.name}」(当前值:{phrase!r})"
        )
    if defn.enabled is True:
        raise ValueError(f"该门户定义已启用(enabled: true),无需重复确认:{path}")

    if flow is not None:
        if flow.phase != PHASE_PLACED:
            raise ValueError(
                f"状态机仅支持 placed → enabled 转移:当前阶段是 {flow.phase!r},"
                "仅已落位(placed)的草稿可启用"
            )
        if flow.portal_name != defn.name:
            raise ValueError(
                f"状态机门户名({flow.portal_name!r})与定义文件门户名"
                f"({defn.name!r})不一致,拒绝启用(确认对象错位)"
            )

    new_text = _flip_enabled_true(path.read_text(encoding="utf-8"))
    # 写前对新文本再跑全链校验 + 启用复核:同目录临时文件 + 原子替换,
    # 失败零残留(原文件字节不变)。
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".netsentinel_enable_", suffix=".yaml", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        try:
            reloaded = load_portal_def(tmp_path)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise ValueError(f"启用后文本全链校验失败,文件保持原样:{exc}") from exc
        if reloaded.enabled is not True:
            raise ValueError(
                "启用改写未生效(enabled 仍非 true),文件保持原样:"
                "请检查定义文件顶层的 enabled 声明形态"
            )
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 临时文件清理失败不影响主流程
            pass
    if flow is not None:
        flow.mark_enabled()
    telemetry.inc("draft_flow_page.confirm_enable")
    return path


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "门户草稿工作流"
_MOTTO = (
    "扫描 → 人工确认 → 落位:本地表单 HTML 离线起草门户 YAML,逐字段人工"
    "确认后方可写入 portals/ 目录;全程零外呼,URL 一律拒绝。"
)
_MANUAL_NOTICE = (
    "⚠️ **人工确认是硬性语义**:字段默认全不勾,置信度只作参考高亮,系统"
    "绝不自动勾选;落位须满足「全部字段确认 + 声明复述门户名 + 口令二次"
    "确认」,且写入前经 load_portal_def 全链校验——UI 不提供任何绕过路径。"
)

#: 项目 portals/ 目录(落位唯一允许的目标目录;webui/ 上一级即项目根)。
_PROJECT_PORTALS: Path = Path(__file__).resolve().parents[1] / "portals"

#: session_state 键。
_SS_BUNDLE = "draft_flow_bundle"
_SS_FLOW = "draft_flow_flow"
_SS_SLUG = "draft_flow_slug"
_SS_TARGET = "draft_flow_target"
_SS_ENABLE_PHRASE = "draft_flow_enable_phrase"
_SS_CONFIRM_PREFIX = "draft_confirm_"


def _render_scan() -> None:
    """页签①扫描:上传 / 选择本地 HTML → 离线解析 → 草稿 YAML + 映射表。"""
    st.subheader("① 扫描(离线解析,零外呼)")
    st.caption(
        "只接受本地 HTML(下方上传或本地文件路径;URL 一律拒绝);解析复用 "
        "form_scanner 的 stdlib 可访问性语义(label/aria/placeholder),绝不联网。"
    )
    uploaded = st.file_uploader(
        "上传本地表单页面 HTML", type=["html", "htm"], help="离线解析,绝不联网抓取"
    )
    path_text = st.text_input(
        "或填写本地 HTML 文件路径",
        value="",
        help="如 tests/mock_portals/12377_mock.html;含 :// 的 URL 输入会被拒绝。",
    )
    name = st.text_input(
        "门户展示名(必填;确认声明与落位均以该名为准)",
        value="",
        help="人工确认时改为门户正式展示名,如「中央网信办违法和不良信息举报」。",
    )
    slug = st.text_input(
        "门户短名(落位文件名;仅字母数字连字符下划线,如 newportal)",
        value="",
        key=_SS_SLUG,
        help="落位将写入 portals/<短名>.yaml;留空时从门户名推导(中文名无法推导,须显式填写)。",
    )
    entry_url_key = st.text_input(
        "Config 入口字段名(占位;必须是 Config 现有字段,如 portal_12377_base)",
        value="",
    )
    category = st.text_input("举报类目(占位;人工确认时改为正式类目文案)", value="")

    if st.button("开始扫描(离线)", type="primary"):
        try:
            if uploaded is not None:
                html_text = uploaded.read().decode("utf-8-sig", errors="replace")
                source = f"上传:{uploaded.name}"
            elif path_text.strip():
                source = path_text.strip()
                if "://" in source or source.lower().startswith(("http://", "https://", "file://")):
                    raise ValueError(
                        f"输入必须是本地 HTML 文件路径,禁止 URL:{source}(红线:离线解析)"
                    )
                html_text = Path(source).read_text(encoding="utf-8-sig", errors="replace")
            else:
                raise ValueError("请先上传本地 HTML 或填写本地文件路径")
            if not name.strip():
                raise ValueError("请先填写门户展示名(确认声明须复述该名,占位名无法复述)")
            kwargs: dict[str, Any] = {"name": name.strip()}
            if entry_url_key.strip():
                kwargs["entry_url_key"] = entry_url_key.strip()
            if category.strip():
                kwargs["category"] = category.strip()
            bundle = draft_parse(html_text, source=source, **kwargs)
        except (ValueError, OSError) as exc:
            st.error(f"扫描失败:{exc}")
        else:
            # 新草稿:清空上一份的逐字段勾选与落位目标,杜绝陈旧勾选/路径带入
            for key in list(st.session_state):
                if key.startswith(_SS_CONFIRM_PREFIX):
                    del st.session_state[key]
            st.session_state.pop(_SS_TARGET, None)
            st.session_state.pop(_SS_ENABLE_PHRASE, None)
            st.session_state[_SS_BUNDLE] = bundle
            st.session_state[_SS_FLOW] = DraftConfirmFlow(
                bundle.portal_name, bundle.field_keys
            )
            st.success(
                f"扫描完成:识别 {bundle.mapped_count} 个契约字段,"
                f"{bundle.manual_count} 个须人工补齐;警告 {len(bundle.warnings)} 条。"
                "请到「② 人工确认」逐字段确认。"
            )

    bundle = st.session_state.get(_SS_BUNDLE)
    if bundle is None:
        st.info("尚无草稿。完成一次扫描后,这里会展示草稿 YAML、字段映射表与警告列表。")
        return

    if bundle.warnings:
        with st.expander(f"警告列表(共 {len(bundle.warnings)} 条,验证码排除置顶)", expanded=False):
            for warning in bundle.warnings:
                st.markdown(f"- {warning}")
    st.subheader("草稿 YAML(尚未生效)")
    st.code(bundle.draft_yaml, language="yaml")
    st.subheader("字段映射表(含置信度与推断依据)")
    st.table(
        [
            {
                "字段": r["key"],
                "状态": r["status_cn"],
                "置信度": f"{r['confidence']:.2f}",
                "推断依据": r["evidence"] or "(页面缺失,无推断依据)",
                "候选链": r["chain"],
                "控件": r["control"] or "(未识别到控件)",
            }
            for r in bundle.field_rows
        ]
    )
    st.caption(f"来源:{bundle.source or '(未知)'};manual_required 字段 {bundle.manual_count} 个须人工补齐。")


def _render_confirm() -> None:
    """页签②人工确认:逐字段勾选(默认全不勾)+ 确认声明复述门户名。"""
    st.subheader("② 人工逐字段确认")
    bundle: DraftBundle | None = st.session_state.get(_SS_BUNDLE)
    flow: DraftConfirmFlow | None = st.session_state.get(_SS_FLOW)
    if bundle is None or flow is None:
        st.info("请先在「① 扫描」页签完成一次扫描。")
        return
    st.caption(
        "红线:默认全不勾;置信度仅作参考高亮(⭐ ≥ 0.90),系统绝不自动勾选——"
        "每一下勾选都是一次显式人工确认动作;已勾选可在落位前撤回。"
    )
    if flow.phase == PHASE_PLACED:
        st.success("本草稿已落位,确认状态已封存(只读)。")
        return

    for row in bundle.field_rows:
        key = row["key"]
        star = " ⭐高置信" if row["confidence"] >= 0.90 else ""
        label = f"{key} — {row['status_cn']}(置信度 {row['confidence']:.2f}{star})"
        checked = st.checkbox(label, value=False, key=f"{_SS_CONFIRM_PREFIX}{key}")
        try:
            if checked and not flow.is_confirmed(key):
                flow.confirm_field(key)
            elif not checked and flow.is_confirmed(key):
                flow.unconfirm_field(key)
        except ValueError as exc:  # 已落位等非法变更:如实展示,不绕过
            st.error(str(exc))

    info = flow.summary()
    cols = st.columns(3)
    cols[0].metric("已确认字段", f"{info['confirmed']} / {info['total']}")
    cols[1].metric("待确认", len(info["missing"]))
    cols[2].metric("声明复述", "✅ 通过" if info["declaration_ok"] else "❌ 未通过")
    if info["missing"]:
        st.warning("待确认字段:" + ", ".join(info["missing"]))
    declaration = st.text_input(
        f"确认声明:请原样复述门户名「{flow.portal_name}」",
        value="",
        help="复述必须与门户名完全一致——证明确认的是这一份草稿,而非顺手勾选。",
    )
    try:
        flow.set_declaration(declaration)
    except ValueError as exc:
        st.error(str(exc))
        return
    if flow.can_persist():
        st.success("全部字段已确认且声明复述正确——「③ 落位」页签已满足确认门槛。")
    else:
        st.info("完成全部字段勾选与声明复述后,「③ 落位」才会启用。")


def _render_place() -> None:
    """页签③落位:门槛面板 + 口令二次确认 + 写入 portals/(全链校验)。"""
    st.subheader("③ 落位(写入项目 portals/ 目录)")
    bundle: DraftBundle | None = st.session_state.get(_SS_BUNDLE)
    flow: DraftConfirmFlow | None = st.session_state.get(_SS_FLOW)
    if bundle is None or flow is None:
        st.info("请先在「① 扫描」页签完成一次扫描。")
        return
    st.warning(
        f"落位 = 把草稿写入 `{_PROJECT_PORTALS}/<短名>.yaml`:写入前经 load_portal_def "
        "全链校验,失败拒绝;同名文件拒绝覆盖。落位 ≠ 生效:文件以 enabled: false "
        "登记(disabled),人工核验后把 enabled 改 true(或经下方口令确认启用)"
        "方被动态发现(get_portal)自动加载。"
    )
    info = flow.summary()
    if info["placed"]:
        target_text = st.session_state.get(_SS_TARGET)
        if target_text:
            st.success(placed_notice(Path(target_text)))
        else:  # pragma: no cover - 旧会话无目标路径的兜底文案
            st.success("本草稿已落位(确认状态已封存);如需再次落位请重新扫描。")
        if flow.phase == PHASE_ENABLED:
            st.info("该门户已启用(enabled: true):discover_portals / get_portal 即刻可发现。")
        elif target_text:
            # 口令确认启用(placed → enabled):复述门户名后把 enabled 改 true。
            enable_phrase = st.text_input(
                f"启用确认:请原样复述门户名「{flow.portal_name}」",
                value="",
                key=_SS_ENABLE_PHRASE,
                help="复述必须与门户名完全一致;确认后文件头部 enabled 改为 true,"
                "新门户自动被动态发现加载(无批量捷径,一次只启用一份)。",
            )
            if st.button(
                "启用(把 enabled 改为 true)",
                disabled=not enable_phrase.strip(),
            ):
                try:
                    confirm_enable_portal(Path(target_text), enable_phrase, flow=flow)
                except ValueError as exc:
                    st.error(f"启用被拒绝:{exc}")
                else:
                    st.success(
                        "已启用:enabled 已改为 true,该门户即刻可被 get_portal 按短名加载。"
                    )
        return

    st.markdown(f"- 确认进度:{info['confirmed']} / {info['total']}")
    st.markdown(f"- 声明复述:{'✅ 通过' if info['declaration_ok'] else '❌ 未通过'}")
    st.markdown(f"- 落位门槛:{'✅ 已满足' if info['can_persist'] else '❌ 未满足'}")
    if info["missing"]:
        st.markdown("- 待确认字段:" + ", ".join(info["missing"]))
    slug = _clean(st.session_state.get(_SS_SLUG))
    phrase = st.text_input(
        f"二次确认:请输入落位口令「{PERSIST_PHRASE}」", value=""
    )
    ready = flow.can_persist() and phrase == PERSIST_PHRASE
    if st.button("落位(写入 portals/ 目录)", type="primary", disabled=not ready):
        try:
            target = persist_draft(
                bundle.draft_yaml,
                flow,
                portals_dir=_PROJECT_PORTALS,
                confirm_phrase=phrase,
                slug=slug or None,
            )
        except ValueError as exc:
            st.error(f"落位被拒绝:{exc}")
        else:
            # 记录目标路径,供落位后的「口令确认启用」使用(A233 闭环)。
            st.session_state[_SS_TARGET] = str(target)
            st.success(placed_notice(target))


def _clean(value: Any) -> str:
    """UI 输入规整:转字符串去空白(非字符串按空串)。"""
    return str(value).strip() if isinstance(value, str) else ""


def render() -> None:
    """门户草稿工作流主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染门户草稿工作流;请先安装 python -m pip install -e \".[ui]\""
        )

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="📝", layout="wide")
    st.title(f"📝 净网哨兵 · {_PAGE_TITLE}")
    st.caption(_MOTTO)
    st.warning(_MANUAL_NOTICE)  # 页面顶部声明:人工确认语义,无绕过路径

    tab_scan, tab_confirm, tab_place = st.tabs(["① 扫描", "② 人工确认", "③ 落位"])
    with tab_scan:
        _render_scan()
    with tab_confirm:
        _render_confirm()
    with tab_place:
        _render_place()


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,门户草稿工作流页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/draft_flow_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
