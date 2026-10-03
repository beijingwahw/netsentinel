"""声明式政策引擎 —— 运营者用 YAML 描述"什么判级/风险走什么流程"(NetSentinel · A49)。

设计动机:v1/v2 的分流逻辑(是否入列 / 是否提醒)散落在 orchestrator 的
if-else 里,运营者调整流程必须改代码。本引擎把"分流规则"抽成数据:

    policy.yaml(运营者写)──load_policy──> list[Rule] ──decide──> Decision

匹配语义:
- 规则自上而下逐条评估,首条命中即生效(顺序即优先级);
- when 中声明的条件全部满足才算命中(AND);when 为空 = 匹配一切;
- 报告侧字段存在才比较:intel 里没有 url/risk(或值非法)时,
  min_url_risk 条件视为不命中——宁可漏配也不凭空放大;
- 全部未命中 → 兜底 Decision("fallback", "queue", …):默认进入人工复核。

安全红线(CONTRACTS-V3 §0 红线 11,政策只能增加审批、不能削弱人工门):
- 任何 action(queue / notify / ignore / four_eyes)都不能跳过 HUMAN_GATE
  与人工确认;引擎不提供任何"自动提交"语义;
- four_eyes 仅表示在人工门之上追加第二审核人(对接 A50 四眼复核队列);
- 所有兜底方向(queue / fallback / 默认政策)一律回到人工复核。

PyYAML 为惰性依赖:未安装时仅内置默认政策可用(文件不存在 / decide
不传 rules 都不需要 YAML);一旦要解析 yaml 文件,会给出中文安装提示。

V5 性能升级(规则编译一次):``when`` 条件在加载/首次 decide 时预解析为
判断闭包(verdict 列表预建 frozenset、阈值预提取为局部 float),缓存在
Rule 对象上(repr/相等性均忽略该缓存);decide 热路径不再逐字段做
dict 成员查询与集合重建。用法示例::

    from netsentinel.policy import decide, load_policy

    rules = load_policy("policy.yaml")   # 加载时即完成编译
    decision = decide(report, rules)     # 热路径:一次闭包调用/规则

P4 安全审计清偿(策略哈希入审计):``load_policy`` 加载时**一次**计算政策
文件内容 sha256,随规则列表携带(:class:`PolicyRules`.sha256);每次分流
决策经注入的审计 sink 落一条 ``policy_decide`` 事件,恒含 ``policy_sha256``
字段——决策可归因到具体政策版本(OPA/Cedar 审计惯例)。本模块不直接依赖
审计日志器:sink 由调用方显式注入(与 telemetry_trace 同款接线),默认
未注入 = 不发任何事件、零行为变化::

    from netsentinel.policy.engine import configure_audit_sink

    configure_audit_sink(lambda payload: audit_logger.log_event(**payload))
"""
from __future__ import annotations

import hashlib
import logging
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import SiteReport, Verdict

__all__ = [
    "DECISION_AUDIT_EVENT",
    "VALID_ACTIONS",
    "VALID_WHEN_KEYS",
    "Decision",
    "PolicyRules",
    "Rule",
    "configure_audit_sink",
    "decide",
    "default_rules",
    "describe_actions",
    "load_policy",
]

logger = logging.getLogger(__name__)

#: 合法 action 全集(分流动作,任何 action 均不能跳过人工门)。
VALID_ACTIONS: tuple[str, ...] = ("queue", "notify", "ignore", "four_eyes")

#: when 条件的合法键全集。
VALID_WHEN_KEYS: tuple[str, ...] = ("verdict", "min_agg", "min_url_risk", "needs_review")

#: 单条规则的合法字段全集(name/when/action/note)。
_RULE_KEYS: tuple[str, ...] = ("name", "when", "action", "note")

#: Verdict 枚举的字符串值全集(when.verdict 的合法取值)。
_VERDICT_VALUES: frozenset[str] = frozenset(v.value for v in Verdict)

#: 内置默认政策(缺文件 / rules=None 时使用)的规则名。
DEFAULT_RULE_NAME = "default"

#: 兜底决策(未命中任何规则)的规则名。
FALLBACK_RULE_NAME = "fallback"

#: 兜底决策说明(契约固定文案)。
FALLBACK_NOTE = "未命中任何规则,默认进入人工复核"

#: 红线说明后缀:describe_actions() 的每条文案都注明。
_RED_LINE_NOTE = "任何 action 均不能跳过人工门;four_eyes 表示追加第二审核人。"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    """一条声明式分流规则。

    属性:
        name:   规则名(唯一标识,报错与 Decision 回溯用);
        when:   匹配条件(AND 语义),合法键见 :data:`VALID_WHEN_KEYS`,
                空 dict = 匹配一切报告;
        action: 命中后的分流动作,合法集合见 :data:`VALID_ACTIONS`;
        note:   中文备注(可选),原样进入 :class:`Decision`.note。

    V5:``_match`` 为 when 的编译缓存(预解析判断闭包),首次使用时构建、
    之后复用;``repr=False, compare=False`` 使其完全不影响规则对象的
    打印与相等性比较(既有测试与调用方无感知)。
    """

    name: str
    when: dict[str, Any]
    action: str
    note: str = ""
    _match: Callable[[SiteReport], bool] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass
class Decision:
    """一次分流决策的结果。

    属性:
        rule_name: 命中的规则名;未命中任何规则时为 ``"fallback"``;
        action:    分流动作(合法集合见 :data:`VALID_ACTIONS`);
        note:      规则备注或兜底说明(中文);
        matched:   是否命中了显式规则(兜底决策为 False)。
    """

    rule_name: str
    action: str
    note: str
    matched: bool = False


def default_rules() -> list[Rule]:
    """内置默认政策:单条 queue-all(全部进入人工复核)。

    文件缺失、rules=None 时都使用它——默认方向永远回到人工,符合红线 11。
    每次调用返回新对象,调用方修改不会污染模块级状态;返回前即完成
    when 编译(V5:decide 热路径零解析开销)。
    """
    return [_warm(Rule(name=DEFAULT_RULE_NAME, when={}, action="queue", note="默认:全部进入人工复核"))]


def describe_actions() -> dict[str, str]:
    """返回每个 action 的中文说明(运营者文档 / 复核台展示用)。

    每条说明都注明红线:任何 action 均不能跳过人工门;four_eyes 表示
    追加第二审核人(只增审批,不替代人工确认)。
    """
    return {
        "queue": "进入人工复核队列,由人工拍板是否举报。" + _RED_LINE_NOTE,
        "notify": "仅发送待复核提醒(webhook),不自动执行任何提交动作。" + _RED_LINE_NOTE,
        "ignore": "仅记录、不再重复提醒;已进入人工复核队列的条目不受影响。" + _RED_LINE_NOTE,
        "four_eyes": "在人工复核之上追加第二审核人(四眼原则),两人齐批才可继续。" + _RED_LINE_NOTE,
    }


# ---------------------------------------------------------------------------
# 决策审计(P4 清偿:policy_sha256 入审计,sink 注入式零依赖接线)
# ---------------------------------------------------------------------------

#: 决策审计事件名(载荷可直接 ``log_event(**payload)`` 展开,JSONL 的 event 字段)。
DECISION_AUDIT_EVENT = "policy_decide"

#: 决策审计 sink 的模块级注入点(默认 None = 不发任何事件,现状零行为变化)。
_AUDIT_SINK: Callable[[dict[str, Any]], None] | None = None

#: 与 :data:`_AUDIT_SINK` 配套的锁:注入/读取快照原子化,并发 decide 安全。
_AUDIT_LOCK = threading.Lock()


class PolicyRules(list):
    """携带政策文件 sha256 的规则列表(:func:`load_policy` 的返回类型)。

    - 仍是普通 ``list``(相等性 / 切片 / 迭代 / repr / isinstance 全部继承,
      既有调用方与测试零感知),仅多一个 ``sha256`` 属性;
    - ``sha256``:加载时一次计算的政策文件内容 sha256(hex);决策审计据此
      把每次分流归因到具体政策版本。内置默认政策 / 程序化构造的普通 list
      无此来源,审计字段相应记 ``None``(诚实口径:无文件即无哈希);
    - 政策内容变更后重新 ``load_policy`` → 新列表携带新哈希;陈旧列表对象
      的哈希不变——用哪份规则决策,就归因到哪份版本。
    """

    #: 政策文件内容的 sha256(hex);非文件来源(内置默认)为 None。
    sha256: str | None = None


def configure_audit_sink(sink: Callable[[dict[str, Any]], None] | None = None) -> None:
    """注入/清除决策审计 sink;默认未注入 = 行为与历史版本完全一致。

    - ``sink`` 收单参载荷 dict(含 ``event`` 键,可直接展开进
      ``JsonlAuditLogger.log_event``),典型接线::

          configure_audit_sink(lambda p: audit_logger.log_event(**p))

    - 传 ``None``(或无参调用)即清除;注入非可调用对象抛 :class:`TypeError`
      (中文);
    - sink 只是审计旁路:匹配语义 / :class:`Decision` 返回值 / 决策遥测计数
      均不受影响;sink 自身异常也绝不反过来影响决策(见
      :func:`_emit_decision_audit`)。本模块不直接 import 审计日志器,
      与 telemetry_trace 的 audit_sink 注入同款零依赖口径。
    """
    global _AUDIT_SINK
    if sink is not None and not callable(sink):
        raise TypeError(f"决策审计 sink 需为可调用,当前为 {type(sink).__name__}")
    with _AUDIT_LOCK:
        _AUDIT_SINK = sink


def _emit_decision_audit(
    report: SiteReport, decision: Decision, rules: list[Rule]
) -> None:
    """把一次分流决策作为审计事件发给已注入的 sink;未注入 = 零行为。

    - 载荷恒七键:``{"event": "policy_decide", "site_url", "verdict",
      "rule_name", "action", "matched", "policy_sha256"}``;
      ``policy_sha256`` 取自规则列表携带的加载期缓存(热路径零文件 IO);
      报告侧字段经防御式 ``getattr`` 读取(兼容 WebUI 的鸭子报告代理,
      字段缺席记 None,与 fusion/policy 的防御式口径一致);
    - sink 未注入:不发事件、不计数(默认,与历史行为逐位一致);
    - 成功送达计数 ``policy.decision.audited``;sink 异常绝不影响决策本身:
      吞掉 + 计数 ``policy.decision.audit_error`` + debug 日志(载荷不含
      任何密钥,异常信息也不回显政策文件内容)。
    """
    with _AUDIT_LOCK:
        sink = _AUDIT_SINK
    if sink is None:
        return
    verdict = getattr(report, "verdict", None)
    payload: dict[str, Any] = {
        "event": DECISION_AUDIT_EVENT,
        "site_url": getattr(report, "site_url", None),
        "verdict": getattr(verdict, "value", verdict),
        "rule_name": decision.rule_name,
        "action": decision.action,
        "matched": decision.matched,
        "policy_sha256": getattr(rules, "sha256", None),
    }
    try:
        sink(payload)
    except Exception:  # noqa: BLE001 审计旁路失败绝不中断决策主流程
        telemetry.inc("policy.decision.audit_error")
        logger.debug("决策审计事件投递失败(不影响决策本身)", exc_info=True)
        return
    telemetry.inc("policy.decision.audited")


# ---------------------------------------------------------------------------
# YAML 解析(惰性 PyYAML)
# ---------------------------------------------------------------------------

def _import_yaml() -> Any:
    """惰性导入 PyYAML;缺失时抛带中文安装提示的 ImportError。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ImportError(
            "解析 YAML 政策文件需要 PyYAML,请先安装:pip install PyYAML;"
            "未安装时仅内置默认政策可用(全部进入人工复核,decide 不受影响)"
        ) from exc
    return yaml


class _LineDict(dict):
    """记录来源行号的 dict(仅用于政策解析的中文报错定位,业务层当普通 dict 用)。"""

    line: int = 0


def _build_rule_loader(yaml: Any) -> Any:
    """构造记录映射起始行号的 SafeLoader 变体(报错可定位到行,1 起)。"""

    class _RuleLoader(yaml.SafeLoader):
        """SafeLoader 子类:构造 mapping 时附带节点起始行号。"""

        def construct_yaml_map(self, node: Any) -> Any:
            data = _LineDict()
            yield data
            data.update(self.construct_mapping(node))
            mark = getattr(node, "start_mark", None)
            line = getattr(mark, "line", -1)
            data.line = int(line) + 1 if isinstance(line, int) else 0

    _RuleLoader.add_constructor(
        "tag:yaml.org,2002:map", _RuleLoader.construct_yaml_map
    )
    return _RuleLoader


def _rule_location(item: Any, index: int) -> str:
    """生成报错定位串:优先行号,退化为序号(第 N 条)。"""
    line = getattr(item, "line", 0)
    if isinstance(line, int) and line > 0:
        return f"第 {index + 1} 条规则(位于文件第 {line} 行)"
    return f"第 {index + 1} 条规则"


def _parse_rule(item: Any, index: int, path: pathlib.Path) -> Rule:
    """逐条校验并解析一个 YAML 规则元素;不合法抛 ValueError(中文,含定位)。"""
    loc = _rule_location(item, index)
    if not isinstance(item, dict):
        raise ValueError(
            f"政策文件 {path} 中{loc}应为包含 name/when/action/note 的映射,"
            f"当前类型为 {type(item).__name__}"
        )

    unknown_keys = [str(k) for k in item if str(k) not in _RULE_KEYS]
    if unknown_keys:
        raise ValueError(
            f"政策文件 {path} 中{loc}含未知字段:{'、'.join(unknown_keys)}"
            f"(合法字段:{'/'.join(_RULE_KEYS)})"
        )

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"政策文件 {path} 中{loc}缺少合法的 name 字段(应为非空字符串),当前为 {name!r}"
        )

    action = item.get("action")
    if not isinstance(action, str) or action not in VALID_ACTIONS:
        raise ValueError(
            f"政策文件 {path} 中{loc}的 action 非法:{action!r}"
            f"(合法集合:{'/'.join(VALID_ACTIONS)};任何 action 均不能跳过人工门)"
        )

    when_raw = item.get("when")
    if when_raw is None:  # when 缺省 / 显式置空 → 无条件匹配
        when_raw = {}
    if not isinstance(when_raw, dict):
        raise ValueError(
            f"政策文件 {path} 中{loc}的 when 应为条件映射,当前类型为 {type(when_raw).__name__}"
        )
    unknown_when = [str(k) for k in when_raw if str(k) not in VALID_WHEN_KEYS]
    if unknown_when:
        raise ValueError(
            f"政策文件 {path} 中{loc}的 when 含未知键:{'、'.join(unknown_when)}"
            f"(合法键:{'/'.join(VALID_WHEN_KEYS)})"
        )

    when: dict[str, Any] = {}
    for key, value in when_raw.items():
        if key == "verdict":
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(v, str) for v in value)
            ):
                raise ValueError(
                    f"政策文件 {path} 中{loc}的 when.verdict 应为非空字符串列表,"
                    f"当前为 {value!r}"
                )
            bad = [v for v in value if v not in _VERDICT_VALUES]
            if bad:
                raise ValueError(
                    f"政策文件 {path} 中{loc}的 when.verdict 含未知判级:{'、'.join(repr(v) for v in bad)}"
                    f"(合法判级:{'/'.join(sorted(_VERDICT_VALUES))})"
                )
            when["verdict"] = list(value)
        elif key in ("min_agg", "min_url_risk"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"政策文件 {path} 中{loc}的 when.{key} 应为 0~1 的数字,当前为 {value!r}"
                )
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(
                    f"政策文件 {path} 中{loc}的 when.{key} 应在 0~1 之间,当前为 {value!r}"
                )
            when[key] = float(value)
        else:  # needs_review
            if not isinstance(value, bool):
                raise ValueError(
                    f"政策文件 {path} 中{loc}的 when.needs_review 应为布尔"
                    f"(true/false),当前为 {value!r}"
                )
            when["needs_review"] = value

    note = item.get("note", "")
    if not isinstance(note, str):
        raise ValueError(
            f"政策文件 {path} 中{loc}的 note 应为字符串,当前为 {note!r}"
        )
    return Rule(name=name.strip(), when=when, action=action, note=note)


def load_policy(path: str) -> list[Rule]:
    """从 YAML 文件加载政策规则列表。

    - 文件不存在:记 warning 并返回内置默认政策(单条 queue-all,
      全部进入人工复核)——缺政策绝不导致漏审;
    - 文件存在但 PyYAML 未安装:抛带中文安装提示的 ImportError
      (此时仅内置默认政策可用);
    - 顶层必须为列表,元素形如 ``{name, when, action, note}``;逐条校验,
      action 非法 / when 未知键 / 字段类型不符均抛 :class:`ValueError`
      (中文消息,尽量含行号定位);
    - 空文件或空列表:同样回退内置默认政策并告警;
    - V5:解析完成的规则即完成 when 编译(一次加载一次编译,decide 热路径
      直接调用缓存的判断闭包);
    - P4:成功从文件解析出非空规则时,**加载时一次**计算文件字节内容的
      sha256 附着在返回的 :class:`PolicyRules`.sha256 上(哈希与解析读取
      同一份字节,绝不二次读盘);回退内置默认政策的各路径不带哈希
      (决策审计字段记 None);解析失败抛错时哈希不落地(未生效即无版本)。

    :param path: 政策文件路径(通常来自 ``cfg.policy_path``,默认 ``policy.yaml``)。
    """
    p = pathlib.Path(path)
    if not p.is_file():
        logger.warning("政策文件不存在,使用内置默认政策(全部进入人工复核):%s", p)
        return default_rules()

    yaml = _import_yaml()
    try:
        raw_bytes = p.read_bytes()  # P4:字节只读一次,哈希与解析同源
        raw: Any = yaml.load(raw_bytes.decode("utf-8"), Loader=_build_rule_loader(yaml))
    except yaml.YAMLError as exc:
        raise ValueError(f"政策文件损坏,不是合法 YAML:{p}({exc})") from exc

    if raw is None:  # 空文件 / 仅注释
        logger.warning("政策文件为空,使用内置默认政策(全部进入人工复核):%s", p)
        return default_rules()
    if not isinstance(raw, list):
        raise ValueError(
            f"政策文件顶层应为规则列表(每项含 name/when/action/note),"
            f"当前类型为 {type(raw).__name__}:{p}"
        )

    rules = PolicyRules(_warm(_parse_rule(item, i, p)) for i, item in enumerate(raw))
    if not rules:
        logger.warning("政策文件不含任何规则,使用内置默认政策(全部进入人工复核):%s", p)
        return default_rules()
    rules.sha256 = hashlib.sha256(raw_bytes).hexdigest()  # P4:一次计算,随规则携带
    return rules


# ---------------------------------------------------------------------------
# 匹配与决策
# ---------------------------------------------------------------------------

def _url_risk(report: SiteReport) -> float | None:
    """读取报告 intel["url"]["risk"];缺失或非法返回 None(字段存在才比较)。

    对齐 fusion 的防御式读取:intel 里的非法值一律按缺失处理,
    绝不做任何解释或执行(红线 8 同款思路)。
    """
    intel = report.intel or {}
    url_feat = intel.get("url")
    if not isinstance(url_feat, dict):
        return None
    raw = url_feat.get("risk")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _compile_when(when: dict[str, Any] | None) -> Callable[[SiteReport], bool]:
    """把 when 条件编译为**单个**早退判断闭包(V5:规则只解析一次)。

    编译产物是一条与旧版逐字段解释结构相同的函数(条件 AND、评估顺序
    verdict → min_agg → min_url_risk → needs_review、比较取精确否定式,
    NaN 等边界行为逐位一致),区别仅在:verdict 列表预建 frozenset(旧实现
    每次 decide 重建集合)、阈值/布尔目标预绑定为闭包局部量(decide 热路径
    不再做 ``"键" in when`` 成员查询与取值)。空 when / 仅含未知键(程序化
    构造、未经 load_policy 校验)→ 恒真闭包,与旧实现"只检查四个合法键"
    的忽略语义一致。

    用法::

        match = _compile_when({"verdict": ["nsfw"], "min_agg": 0.9})
        if match(report): ...          # 等价于旧 _rule_matches(report, rule)
    """
    w: dict[str, Any] = when or {}
    allowed = frozenset(str(v) for v in w["verdict"]) if "verdict" in w else None
    has_agg = "min_agg" in w
    lo_agg = w.get("min_agg")
    has_risk = "min_url_risk" in w
    lo_risk = w.get("min_url_risk")
    has_review = "needs_review" in w
    target_review = w.get("needs_review")

    def match(r: SiteReport) -> bool:
        if allowed is not None and r.verdict.value not in allowed:
            return False
        if has_agg and r.agg_nsw_prob < lo_agg:
            return False
        if has_risk:
            risk = _url_risk(r)  # 只读取一次
            if risk is None or risk < lo_risk:
                return False
        if has_review and bool(r.needs_review) is not target_review:
            return False
        return True

    return match


def _warm(rule: Rule) -> Rule:
    """确保规则的 when 已编译并缓存(幂等);返回同一 Rule 对象。

    load_policy / default_rules 加载期预热;decide 对程序化构造的规则
    惰性编译一次后复用——任一路径下每条规则全生命周期至多解析一次。
    """
    if rule._match is None:
        rule._match = _compile_when(rule.when)
    return rule


def decide(report: SiteReport, rules: list[Rule] | None = None) -> Decision:
    """对一份站点报告做政策分流决策。

    - ``rules`` 为 None:使用内置默认政策(单条 queue-all,不读文件、
      不依赖 PyYAML);
    - 逐条匹配,首条命中即返回(顺序即优先级);
    - 全部未命中:返回兜底 ``Decision("fallback", "queue",
      "未命中任何规则,默认进入人工复核", matched=False)``;
      (兜底动作同为 queue,故同样计入 ``policy.decide.queue`` 遥测);
    - 传入的规则 action 非法时抛 :class:`ValueError`(中文)——
      与 load_policy 的逐条校验互为双保险(action 校验保留在每次 decide,
      规则对象被中途改动的异常仍会被拦截)。

    红线:决策只决定"入列 / 提醒 / 记录 / 追加第二审核人",任何 action
    都不能跳过人工门;本函数不做任何提交动作。

    可观测性(V5):每次决策按生效动作计数 ``policy.decide.<action>``
    (queue / notify / ignore / four_eyes,含兜底 queue)。

    决策审计(P4):已注入审计 sink(:func:`configure_audit_sink`)时,
    每次决策(命中与兜底)落一条 ``policy_decide`` 事件,含
    ``policy_sha256``(来自规则列表的加载期缓存);成功送达计数
    ``policy.decision.audited``。默认未注入 = 零行为变化。
    """
    if rules is None:
        rules = default_rules()
    for rule in rules:
        if rule.action not in VALID_ACTIONS:
            raise ValueError(
                f"规则 {rule.name!r} 的 action 非法:{rule.action!r}"
                f"(合法集合:{'/'.join(VALID_ACTIONS)})"
            )
        match = rule._match
        if match is None:  # 程序化构造的规则:首次触达时编译并缓存
            match = _warm(rule)._match
        assert match is not None  # _warm 保证已编译
        if match(report):
            telemetry.inc(f"policy.decide.{rule.action}")
            hit = Decision(
                rule_name=rule.name, action=rule.action, note=rule.note, matched=True
            )
            _emit_decision_audit(report, hit, rules)
            return hit
    telemetry.inc("policy.decide.queue")  # 兜底动作固定 queue
    fallback = Decision(
        rule_name=FALLBACK_RULE_NAME,
        action="queue",
        note=FALLBACK_NOTE,
        matched=False,
    )
    _emit_decision_audit(report, fallback, rules)
    return fallback
