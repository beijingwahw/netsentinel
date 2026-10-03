# -*- coding: utf-8 -*-
"""契约同步机(contract-as-code):契约文档声明的配置面 ⇄ contracts.py 的 Config 字段。

对标 2026 前沿的 contract-as-code 实践:契约文档与代码各自演化,靠人眼
保持同步必然漂移。本文件用 :mod:`ast` 从 ``netsentinel/contracts.py``
提取 :class:`~netsentinel.contracts.Config` dataclass 的字段清单(名称、
行号、默认值及其类型),再**宽松解析**契约 Markdown 的配置字段声明
(表格与正文),做机器对账。分两个对账段:

V9 段(历史,保持不变):
    CONTRACTS-V9.md 是 V9 轮增量契约,它只声明本轮落地的配置字段
    (声明行"V9 字段已落地:concurrency_tier(mid)/…")并在正文/表格中以
    ``cfg.<字段>`` 方式引用配置;全量字段表属于主契约 CONTRACTS.md,
    不在本文件领地。故双向一致性按 **V9 增量面**断言:

    - **代码 → 文档**:Config 中 V9 段(源码 ``# ---- V9:…`` 与下一条
      版本注释之间的字段,AST 行号定位)的每个字段,必须出现在 V9
      契约声明行里;差集非空 → 失败并打印"代码有而文档缺"清单;
    - **文档 → 代码**:声明行的每个字段必须存在于 Config 的 V9 段;
      差集非空 → 失败并打印"文档有而代码缺"清单;
    - **引用完整性**:文档正文/表格里的任何 ``cfg.<name>`` 引用都必须
      是 Config 的真实字段(契约文档指向不存在的字段 → 失败);
    - **默认值对账**:声明行带括号默认值提示的字段(如
      ``cpu_reserve(1)``),其字面类型与取值必须与代码 AST 默认值一致。

V11 段(配置正式收录批,A201):
    V11 轮把 V10.4/V10.5 的"附加实例属性"(ensemble_reliability_weights /
    graph_wire / gang_* / cascade_risk_budget / abstain_threshold / 签名三键)
    升格为 Config 一等字段(``# ---- V11:`` 注释段)。CONTRACTS-V11.md 属
    冻结文档(不可改写),其中既有精确字段名(``tsa_url`` / ``graph_wire``),
    也有散文记法(``gang_*`` 通配、``ed25519_seed`` 短名、A192 行
    ``algo=hmac-sha256``、A196 行"默认阈 0.35"锚点),而 V10.4 遗产字段
    ``ensemble_reliability_weights`` 的字面名只在 CONTRACTS-V10.md(A185 行)。
    因此 V11 对账经**显式证据表**桥接(``_V11_FIELD_EVIDENCE``:字段 →
    文档佐证片段 + 佐证文档),双向断言:

    - **代码 → 文档**:Config 的 V11 段字段集合 == 证据表键集合;每个
      字段的佐证片段必须真实出现在对应契约文档(V11 正文/§1 表格,或
      遗产字段的 V10 原始契约);
    - **文档 → 代码**:V11 文档中出现的 V11 命名空间记号(含 ``gang_*``
      通配与 ``ed25519_seed`` 短名,经 ``_V11_DOC_ALIASES`` 解析)必须
      全部命中 Config V11 段的真实字段(抓文档幽灵字段/改名漂移);
    - **默认值对账**:文档明示默认值的字段(graph_wire 默认关 / gang_mode
      connectivity 默认 / 默认阈 0.35 / algo=hmac-sha256 / tsa_url 默认
      None),运行时与 AST 字面默认必须一致。

V12 段(接线收口批,A210,沿用 V11 证据表模式):
    V12 轮把 A202 接线期以附加属性方式读取的 ``trace_enabled`` /
    ``abstain_enabled`` 升格为 Config 一等字段(``# ---- V12:`` 注释段)。
    CONTRACTS-V12.md 属冻结文档:§2"后续机会"行有"trace_enabled/
    abstain_enabled 收录 Config 字段"原文收录锚点,§0 红线 45 有
    "trace_enabled/abstain_enabled/TRACE_ENABLED 缺省 False"默认值锚点
    (大写 ``TRACE_ENABLED`` 是服务层环境变量记号,不属 Config 命名空间)。
    对账口径与 V11 相同(证据表 ``_V12_FIELD_EVIDENCE`` 双向集合对账 +
    佐证片段真实性 + 文档幽灵记号 + 默认值运行时/AST 双一致)。

V13 段(接线残余批配置收录,沿用证据表模式):
    V13 收录批把 V13 波以附加属性方式读取的 ``dynamic_ttl``(A211
    scheduler)/ ``phash_mt_lsh_db``(A204 kernel_wire)/ ``guard_model_path``
    与 ``guard_family``(A217 guard_adapter)升格为 Config 一等字段
    (``# ---- V13:`` 注释段)。CONTRACTS-V13.md 属冻结文档:§2"后续
    机会·接线残余批"行有"guard_model_path/guard_family、dynamic_ttl、
    phash_mt_lsh_db 收录 Config"原文收录锚点(四字段一次点名,并预告
    "scheduler 测试仍有 not hasattr 断言需预改"),§1 A211 行另有
    "dynamic_ttl 默认关"默认值锚点。对账口径与 V11/V12 相同(证据表
    ``_V13_FIELD_EVIDENCE`` 双向集合对账 + 佐证片段真实性 + 文档幽灵
    记号 + 默认值运行时/AST 双一致)。

V14 段(接线残余批配置收录,A223 贝叶斯回流开关升格,沿用证据表模式):
    V14 收录批把 A223 贝叶斯回流在 V14 波以附加属性方式读取的
    ``bayes_reliability`` 升格为 Config 一等字段(``# ---- V14:`` 注释段),
    连同其遗忘半衰期伴随参数 ``bayes_half_life``(收录任务口径:None =
    关闭遗忘 = 现状;契约文档未单独点名)。CONTRACTS-V14.md 属冻结文档:
    §2"后续机会·接线残余"行有"bayes_reliability 待 V14 收录 Config
    (附加属性模式)"原文收录锚点——原文只点名 ``bayes_reliability``,
    ``bayes_half_life`` 无独立文档锚点,属收录开关的伴随漂移参数,与
    开关共用同一收录行佐证(锚点点名检查仅覆盖 ``bayes_reliability``)。
    对账口径与 V11/V12/V13 相同(证据表 ``_V14_FIELD_EVIDENCE`` 双向
    集合对账 + 佐证片段真实性 + 文档幽灵记号 + 默认值运行时/AST 双一致)。

失败哲学:解析失败(找不到 Config、找不到版本段标记、找不到声明行/表格
标题、佐证文档失踪)一律 :func:`pytest.fail` 显式报错,**绝不静默跳过**
——同步机一旦哑火,漂移就会在无声中发生。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACTS_PY = _ROOT / "netsentinel" / "contracts.py"
_CONTRACTS_V9_MD = _ROOT / "CONTRACTS-V9.md"
_CONTRACTS_V10_MD = _ROOT / "CONTRACTS-V10.md"
_CONTRACTS_V11_MD = _ROOT / "CONTRACTS-V11.md"
_CONTRACTS_V12_MD = _ROOT / "CONTRACTS-V12.md"
_CONTRACTS_V13_MD = _ROOT / "CONTRACTS-V13.md"
_CONTRACTS_V14_MD = _ROOT / "CONTRACTS-V14.md"

#: Config 字面默认值的合法类型(dataclass 契约自检用)。
_LITERAL_TYPES = (str, int, float, bool)

#: Config 源码里的版本段注释,如 ``# ---- V9:CPU 自适应… ----``。
_MARKER_RE = re.compile(r"^\s*#\s*-{2,}\s*V(\d+(?:\.\d+)?)")


@dataclass
class _FieldInfo:
    """Config 单个字段的 AST 提取结果。"""

    name: str
    lineno: int
    #: None=必填字段;("literal", value)=字面默认;("factory", 名)=default_factory。
    default: tuple[str, object] | None


# ---------------------------------------------------------------------------
# 代码侧:ast 解析 contracts.py 的 Config
# ---------------------------------------------------------------------------


def _load_config() -> tuple[list[_FieldInfo], list[str]]:
    """解析 contracts.py → Config 字段清单 + 源码行;任何异常显式失败。"""
    if not _CONTRACTS_PY.is_file():
        pytest.fail(f"未找到 {_CONTRACTS_PY}:契约代码文件失踪,同步机拒绝静默")
    source = _CONTRACTS_PY.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - 防御:contracts.py 语法损坏
        pytest.fail(f"contracts.py 语法解析失败:{exc}(同步机要求代码可解析)")
    lines = source.splitlines()

    config_cls = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Config"
        ),
        None,
    )
    if config_cls is None:
        pytest.fail("contracts.py 中未找到 class Config:同步机无法对账")
    if not any(
        (isinstance(d, ast.Name) and d.id == "dataclass")
        or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
        for d in config_cls.decorator_list
    ):
        pytest.fail("class Config 未挂 @dataclass 装饰器:契约结构疑似被改坏")

    fields: list[_FieldInfo] = []
    for node in config_cls.body:
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue  # 类内注释 / 方法等非字段行
        info = _FieldInfo(name=node.target.id, lineno=node.lineno, default=None)
        if node.value is not None:
            info.default = _extract_default(node.value)
        fields.append(info)
    if len(fields) < 80:
        pytest.fail(
            f"Config 仅解析到 {len(fields)} 个字段(预期 ≥80):"
            f"AST 字段提取器与代码结构不符,请检查解析逻辑"
        )
    names = [f.name for f in fields]
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"Config 存在重名字段(dataclass 本会崩,疑似解析器错误):{dup}"
    return fields, lines


def _extract_default(value: ast.expr) -> tuple[str, object]:
    """把字段默认值 AST 归约为 ('literal', 值) 或 ('factory', 工厂名)。"""
    if isinstance(value, ast.Constant) and isinstance(value.value, _LITERAL_TYPES):
        return "literal", value.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "field"
    ):
        for kw in value.keywords:
            if kw.arg == "default_factory":
                target = kw.value
                if isinstance(target, ast.Name):  # list / dict / 具名工厂
                    return "factory", target.id
                if isinstance(target, ast.Lambda):
                    return "factory", "<lambda>"
        return "factory", "<field()>"
    return "other", ast.dump(value)[:60]


def _code_version_fields(
    fields: list[_FieldInfo], lines: list[str], version: str
) -> list[_FieldInfo]:
    """按源码版本注释切出 Config 指定版本段的字段;标记缺失即显式失败。

    标记只认 Config 字段行跨度内的注释行(模块其他位置的同版本字样不算),
    以 class Config 的首/末字段行号为界;段终点为下一条版本注释,或末字段行。
    """
    first_ln = min(f.lineno for f in fields)
    last_ln = max(f.lineno for f in fields)
    markers = [
        (i + 1, m.group(1))
        for i, raw in enumerate(lines)
        if first_ln <= i + 1 <= last_ln and (m := _MARKER_RE.match(raw))
    ]
    hits = [ln for ln, ver in markers if ver == version]
    if not hits:
        pytest.fail(
            f"contracts.py 的 Config 内未找到 '# ---- V{version}:' 版本段注释:"
            f"V{version} 增量字段边界无从判定(同步机拒绝猜测)"
        )
    start = min(hits)
    after = [ln for ln, _ver in markers if ln > start]
    # 末版本段无下一条注释:终点取末字段行的下一行(切分用严格小于号,
    # 直接用末字段行会漏掉该段最后一个字段)。
    end = min(after) if after else max(f.lineno for f in fields) + 1
    seg = [f for f in fields if start < f.lineno < end]
    if not seg:
        pytest.fail(
            f"Config 的 V{version} 段(第 {start}~{end} 行)没有提取到任何字段:"
            f"版本段标记与字段布局不符"
        )
    return seg


def _code_v9_fields(fields: list[_FieldInfo], lines: list[str]) -> list[_FieldInfo]:
    """按源码版本注释切出 Config 的 V9 段字段;标记缺失即显式失败。

    标记只认 Config 字段行跨度内的注释行(模块其他位置的 V9 字样不算),
    以 class Config 的首/末字段行号为界。
    """
    return _code_version_fields(fields, lines, "9")


# ---------------------------------------------------------------------------
# 文档侧:宽松解析 CONTRACTS-V9.md 的配置字段声明与 cfg.* 引用
# ---------------------------------------------------------------------------


def _parse_doc() -> tuple[dict[str, str | None], set[str]]:
    """→ (声明字段 → 默认值提示或 None, 全文 cfg.<name> 引用集)。

    宽松口径:

    - 声明行 = 含"字段已落地"的行(现位于引言 blockquote),取冒号后以
      ``/`` 分隔的 ``name`` 或 ``name(默认值提示)`` 记号,容忍行尾标点;
    - ``cfg.<name>`` 引用扫描全文(含 §2 的 Markdown 文件归属表格行,
      即表格中 ``cfg.concurrency_tier`` 这类写法同样计入文档引用面)。
    """
    if not _CONTRACTS_V9_MD.is_file():
        pytest.fail(f"未找到 {_CONTRACTS_V9_MD}:V9 契约文档失踪,同步机拒绝静默")
    text = _CONTRACTS_V9_MD.read_text(encoding="utf-8")

    declared: dict[str, str | None] = {}
    landing = next((ln for ln in text.splitlines() if "字段已落地" in ln), None)
    if landing is None:
        pytest.fail(
            "CONTRACTS-V9.md 未找到含'字段已落地'的配置声明行:"
            "文档侧配置面无从解析(同步机拒绝静默跳过)"
        )
    payload = re.split(r"[:：]", landing, maxsplit=1)[-1]
    for raw_token in payload.split("/"):
        token = raw_token.strip().strip("。;；,，")
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\(([^()]*)\))?", token)
        if m and m.group(1):
            declared[m.group(1)] = m.group(2)
    if not declared:
        pytest.fail(
            f"声明行解析到 0 个字段名(原文片段:{payload[:80]!r}):"
            f"宽松记号正则与文档格式不符,请修解析器而非跳过"
        )
    cfg_refs = set(re.findall(r"cfg\.([A-Za-z_][A-Za-z0-9_]*)", text))
    return declared, cfg_refs


def _hint_to_value(hint: str) -> object:
    """文档默认值提示 → Python 值:True/False → bool,数字 → int/float,其余按 str。"""
    if hint == "True":
        return True
    if hint == "False":
        return False
    for cast in (int, float):
        try:
            return cast(hint)
        except ValueError:
            continue
    return hint.strip("'\"")


# ---------------------------------------------------------------------------
# 同步断言(全部双向/对账,差集打印)
# ---------------------------------------------------------------------------


def test_config_field_extraction_sanity() -> None:
    """解析哨卫:Config 可解析、@dataclass、无重名、字面默认值类型合法。"""
    fields, _lines = _load_config()
    for f in fields:
        if f.default and f.default[0] == "literal":
            assert isinstance(f.default[1], _LITERAL_TYPES), (
                f"字段 {f.name} 的字面默认值类型 {type(f.default[1]).__name__}"
                f" 超出契约口径 {{str,int,float,bool}}"
            )
    # dataclass 能用这些字段真实实例化(默认值全部合法的端到端证明)
    from netsentinel.contracts import Config

    cfg = Config()
    for f in fields:
        assert hasattr(cfg, f.name), f"Config() 实例缺字段 {f.name}(AST 与运行时不同步)"
        if f.default and f.default[0] == "literal":
            got = getattr(cfg, f.name)
            assert type(got) is type(f.default[1]) and got == f.default[1], (
                f"字段 {f.name} 运行时默认值 {got!r} 与 AST 字面值 {f.default[1]!r} 不符"
            )


def test_doc_declared_and_referenced_fields_exist_in_code() -> None:
    """文档 → 代码:声明字段与全部 cfg.* 引用必须是 Config 真实字段。"""
    fields, _lines = _load_config()
    declared, cfg_refs = _parse_doc()
    known = {f.name for f in fields}
    ghosts = sorted((set(declared) | cfg_refs) - known)
    assert not ghosts, (
        "CONTRACTS-V9.md 声明/引用了 Config 不存在的字段(文档在指向幽灵字段):"
        f"{ghosts};代码侧实有字段参见 contracts.py Config"
    )


def test_v9_field_sets_bidirectional() -> None:
    """双向一致:Config 的 V9 段字段集合 == 契约声明行的字段集合(差集打印)。"""
    fields, lines = _load_config()
    code_v9 = {f.name for f in _code_v9_fields(fields, lines)}
    declared, _refs = _parse_doc()
    doc_v9 = set(declared)

    code_only = sorted(code_v9 - doc_v9)
    doc_only = sorted(doc_v9 - code_v9)
    assert not code_only, (
        "代码有而文档缺:contracts.py Config 的 V9 段存在未写进 CONTRACTS-V9.md "
        f"声明行的字段:{code_only}"
        "(新增 V9 配置必须同步契约声明行,否则文档漂移)"
    )
    assert not doc_only, (
        "文档有而代码缺:CONTRACTS-V9.md 声明了 Config V9 段不存在的字段:"
        f"{doc_only}(字段被改名/删除后契约未更新,或落地遗漏)"
    )


def test_v9_declared_defaults_match_code() -> None:
    """默认值对账:声明行带提示的默认值,类型与取值必须与代码一致(bool≠int)。"""
    fields, lines = _load_config()
    code_v9 = {f.name: f for f in _code_v9_fields(fields, lines)}
    declared, _refs = _parse_doc()
    for name, hint in declared.items():
        if hint is None:
            continue  # 裸字段名(无默认值提示)只参与集合对账
        info = code_v9.get(name)
        assert info is not None, f"字段 {name} 不在 Config V9 段(应已被双向测试拦截)"
        assert info.default is not None and info.default[0] == "literal", (
            f"字段 {name} 文档声明默认值 {hint!r},但代码无字面默认"
            f"(default={info.default!r})"
        )
        want, got = _hint_to_value(hint), info.default[1]
        assert type(want) is type(got) and want == got, (
            f"字段 {name} 默认值漂移:文档提示 {hint!r} → {want!r}"
            f"({type(want).__name__}),代码实为 {got!r}({type(got).__name__})"
        )


# ---------------------------------------------------------------------------
# V11 段对账(配置正式收录批:附加属性 → Config 一等字段)
# ---------------------------------------------------------------------------

#: V11 命名空间记号:CONTRACTS-V11.md 中形似 V11 配置字段的词(含 ``gang_*``
#: 通配记法);用于"文档 → 代码"幽灵字段检查。
_V11_NAMESPACE_RE = re.compile(
    r"^(?:graph_wire|gang_[A-Za-z0-9_]+|gang_\*"
    r"|ensemble_reliability_weights|cascade_risk_budget|abstain_threshold"
    r"|bundle_sign_algo|ed25519_seed|ed25519_seed_hex|tsa_url)$"
)

#: 文档散文记法 → 契约字段名(通配/短名;解析不到任何字段即判幽灵)。
_V11_DOC_ALIASES: dict[str, tuple[str, ...]] = {
    "gang_*": (
        "gang_mode",
        "gang_weight_threshold",
        "gang_template_weight_factor",
        "gang_resolution",
    ),
    "ed25519_seed": ("ed25519_seed_hex",),
}

#: 「文档未明示默认值」哨兵(证据表第三元占位;按对象身份判别)。
_NO_HINT = object()

#: V11 证据表:契约字段 → (文档佐证片段, 佐证文档, 文档明示默认值或省略)。
#: 佐证文档 "V11" = CONTRACTS-V11.md(§1 表格与正文);"V10" = 遗产字段在其
#: 原始契约 CONTRACTS-V10.md 中的字面名(V11 文档冻结不可改,遗产字段以
#: 其落地轮契约为准)。契约文档修改后此表必须同步复核。
_V11_FIELD_EVIDENCE: dict[str, tuple[str, str, object]] = {
    "graph_wire": ("graph_wire 默认关", "V11", False),
    "gang_mode": ("connectivity 默认", "V11", "connectivity"),
    "gang_weight_threshold": ("gang_*", "V11", _NO_HINT),
    "gang_template_weight_factor": ("gang_*", "V11", _NO_HINT),
    "gang_resolution": ("gang_*", "V11", _NO_HINT),
    "ensemble_reliability_weights": ("ensemble_reliability_weights", "V10", _NO_HINT),
    "cascade_risk_budget": ("风险质量约束", "V11", _NO_HINT),
    "abstain_threshold": ("默认阈 0.35", "V11", 0.35),
    "bundle_sign_algo": ("algo=hmac-sha256", "V11", "hmac-sha256"),
    "ed25519_seed_hex": ("ed25519_seed", "V11", _NO_HINT),
    "tsa_url": ("tsa_url", "V11", None),
}


def _load_md(path: Path, label: str) -> str:
    """读取契约 Markdown;文件失踪显式失败(同步机拒绝静默)。"""
    if not path.is_file():
        pytest.fail(f"未找到 {path}:{label} 失踪,同步机拒绝静默")
    return path.read_text(encoding="utf-8")


def _code_v11_fields(fields: list[_FieldInfo], lines: list[str]) -> list[_FieldInfo]:
    """切出 Config 的 V11 段(``# ---- V11:`` 起,至末字段);标记缺失即失败。"""
    return _code_version_fields(fields, lines, "11")


def _resolve_v11_token(token: str, code_v11: set[str]) -> list[str]:
    """文档记号 → 契约字段名列表(直名/别名/通配);空列表 = 幽灵记号。"""
    if token in code_v11:
        return [token]
    return [name for name in _V11_DOC_ALIASES.get(token, ()) if name in code_v11]


def test_v11_segment_matches_evidence_table_bidirectionally() -> None:
    """双向集合对账:Config V11 段字段 == 证据表键(差集打印)。

    证据表是代码段与(冻结的)契约文档之间的显式桥:新增 V11 字段必须
    补证据条目,删除/改名字段必须同步清理,双向都不许漂移。
    """
    fields, lines = _load_config()
    code_v11 = {f.name for f in _code_v11_fields(fields, lines)}
    table_keys = set(_V11_FIELD_EVIDENCE)
    code_only = sorted(code_v11 - table_keys)
    table_only = sorted(table_keys - code_v11)
    assert not code_only, (
        "代码有而证据表缺:contracts.py Config 的 V11 段存在未登记对账证据的字段:"
        f"{code_only}(新增 V11 字段必须补 _V11_FIELD_EVIDENCE 条目)"
    )
    assert not table_only, (
        "证据表有而代码缺:_V11_FIELD_EVIDENCE 登记了 Config V11 段不存在的字段:"
        f"{table_only}(字段被改名/删除后证据表未同步)"
    )


def test_v11_code_fields_have_real_doc_evidence() -> None:
    """代码 → 文档:每个 V11 字段的佐证片段必须真实出现在对应契约文档。"""
    fields, lines = _load_config()
    code_v11 = {f.name for f in _code_v11_fields(fields, lines)}
    assert code_v11, "V11 段为空(应已被段提取测试拦截)"
    docs = {
        "V11": _load_md(_CONTRACTS_V11_MD, "CONTRACTS-V11.md"),
        "V10": _load_md(_CONTRACTS_V10_MD, "CONTRACTS-V10.md"),
    }
    v11_text = docs["V11"]
    if "## 1." not in v11_text:
        pytest.fail(
            "CONTRACTS-V11.md 缺少 '## 1.' 归属表格标题:文档结构变化,"
            "V11 解析器需人工复核(同步机拒绝静默)"
        )
    missing = [
        f"{name}(佐证 {where}:{snippet!r})"
        for name, (snippet, where, _hint) in _V11_FIELD_EVIDENCE.items()
        if name in code_v11 and snippet not in docs[where]
    ]
    assert not missing, (
        "契约文档中找不到字段佐证片段(文档漂移,或字段被改名/证据写错):"
        f"{missing}"
    )


def test_v11_doc_mentions_resolve_to_code_fields() -> None:
    """文档 → 代码:V11 文档中的命名空间记号必须全部命中真实字段(抓幽灵)。"""
    fields, lines = _load_config()
    code_v11 = {f.name for f in _code_v11_fields(fields, lines)}
    text = _load_md(_CONTRACTS_V11_MD, "CONTRACTS-V11.md")
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_*]*", text))
    namespace_tokens = sorted(t for t in tokens if _V11_NAMESPACE_RE.match(t))
    assert namespace_tokens, (
        "CONTRACTS-V11.md 未解析到任何 V11 命名空间记号:"
        "文档与解析器口径疑似双双失效,请人工复核(_V11_NAMESPACE_RE)"
    )
    ghosts = [t for t in namespace_tokens if not _resolve_v11_token(t, code_v11)]
    assert not ghosts, (
        "CONTRACTS-V11.md 出现无法解析到 Config V11 段字段的命名空间记号"
        f"(文档幽灵字段/字段改名后未同步):{ghosts};代码实有字段:"
        f"{sorted(code_v11)}"
    )


def test_v11_doc_default_hints_match_code() -> None:
    """默认值对账:文档明示默认的字段,运行时默认值与 AST 字面默认一致。"""
    from netsentinel.contracts import Config

    fields, lines = _load_config()
    code_v11 = {f.name: f for f in _code_v11_fields(fields, lines)}
    hinted = {
        name: hint
        for name, (_snippet, _where, hint) in _V11_FIELD_EVIDENCE.items()
        if hint is not _NO_HINT
    }
    assert hinted, "证据表未登记任何默认值提示(应至少覆盖 graph_wire/tsa_url)"
    cfg = Config()
    for name, want in hinted.items():
        info = code_v11.get(name)
        assert info is not None, f"字段 {name} 不在 Config V11 段(应已被集合对账拦截)"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值漂移:文档明示 {want!r}({type(want).__name__}),"
            f"代码运行时实为 {got!r}({type(got).__name__})"
        )
        if want is not None:  # None 默认经 AST 归约为 'other',运行时已校验
            assert info.default is not None and info.default[0] == "literal", (
                f"字段 {name} 文档明示字面默认 {want!r},代码却无字面默认"
                f"(default={info.default!r})"
            )
            assert info.default[1] == want and type(info.default[1]) is type(want), (
                f"字段 {name} AST 默认值 {info.default[1]!r} 与文档明示 {want!r} 不符"
            )


# ---------------------------------------------------------------------------
# V12 段对账(接线收口批:A202 附加属性 → Config 一等字段,沿用证据表模式)
# ---------------------------------------------------------------------------

#: V12 命名空间记号:CONTRACTS-V12.md 中形似 V12 配置字段的词;大写
#: ``TRACE_ENABLED`` 是服务层环境变量记号(红线 45 行),不属 Config
#: 命名空间,刻意不在匹配集内(不算幽灵)。
_V12_NAMESPACE_RE = re.compile(r"^(?:trace_enabled|abstain_enabled)$")

#: V12 证据表:契约字段 → (收录佐证片段, 文档明示默认值)。
#: 收录佐证位于 CONTRACTS-V12.md §2"后续机会·接线残余"行(原文锚点
#: "trace_enabled/abstain_enabled 收录 Config 字段");默认值锚点见
#: :data:`_V12_DEFAULTS_ANCHOR`(§0 红线 45)。契约文档修改后此表必须同步复核。
_V12_FIELD_EVIDENCE: dict[str, tuple[str, object]] = {
    "trace_enabled": ("trace_enabled/abstain_enabled 收录 Config 字段", False),
    "abstain_enabled": ("trace_enabled/abstain_enabled 收录 Config 字段", False),
}

#: 默认值锚点:§0 红线 45 的原文(两字段缺省 False = 现状逐字一致)。
_V12_DEFAULTS_ANCHOR = "trace_enabled/abstain_enabled/TRACE_ENABLED 缺省 False"


def _code_v12_fields(fields: list[_FieldInfo], lines: list[str]) -> list[_FieldInfo]:
    """切出 Config 的 V12 段(``# ---- V12:`` 起,至末字段);标记缺失即失败。"""
    return _code_version_fields(fields, lines, "12")


def test_v12_segment_matches_evidence_table_bidirectionally() -> None:
    """双向集合对账:Config V12 段字段 == 证据表键(差集打印)。

    证据表是代码段与(冻结的)契约文档之间的显式桥:新增 V12 字段必须
    补证据条目,删除/改名字段必须同步清理,双向都不许漂移。
    """
    fields, lines = _load_config()
    code_v12 = {f.name for f in _code_v12_fields(fields, lines)}
    table_keys = set(_V12_FIELD_EVIDENCE)
    code_only = sorted(code_v12 - table_keys)
    table_only = sorted(table_keys - code_v12)
    assert not code_only, (
        "代码有而证据表缺:contracts.py Config 的 V12 段存在未登记对账证据的字段:"
        f"{code_only}(新增 V12 字段必须补 _V12_FIELD_EVIDENCE 条目)"
    )
    assert not table_only, (
        "证据表有而代码缺:_V12_FIELD_EVIDENCE 登记了 Config V12 段不存在的字段:"
        f"{table_only}(字段被改名/删除后证据表未同步)"
    )


def test_v12_code_fields_have_real_doc_evidence() -> None:
    """代码 → 文档:每个 V12 字段的收录佐证片段与默认值锚点必须真实
    出现在 CONTRACTS-V12.md(§2 收录锚点 + §0 红线 45 默认值锚点)。"""
    fields, lines = _load_config()
    code_v12 = {f.name for f in _code_v12_fields(fields, lines)}
    assert code_v12, "V12 段为空(应已被段提取测试拦截)"
    text = _load_md(_CONTRACTS_V12_MD, "CONTRACTS-V12.md")
    if "## 2." not in text:
        pytest.fail(
            "CONTRACTS-V12.md 缺少 '## 2.' 后续机会标题:文档结构变化,"
            "V12 解析器需人工复核(同步机拒绝静默)"
        )
    missing = [
        f"{name}(佐证 {snippet!r})"
        for name, (snippet, _hint) in _V12_FIELD_EVIDENCE.items()
        if name in code_v12 and snippet not in text
    ]
    assert not missing, (
        "契约文档中找不到字段收录佐证片段(文档漂移,或字段被改名/证据写错):"
        f"{missing}"
    )
    assert _V12_DEFAULTS_ANCHOR in text, (
        f"CONTRACTS-V12.md 缺少默认值锚点 {_V12_DEFAULTS_ANCHOR!r}:"
        "红线 45 文案漂移,V12 默认值对账无从锚定"
    )


def test_v12_doc_mentions_resolve_to_code_fields() -> None:
    """文档 → 代码:V12 文档中的命名空间记号必须全部命中真实字段(抓幽灵)。"""
    fields, lines = _load_config()
    code_v12 = {f.name for f in _code_v12_fields(fields, lines)}
    text = _load_md(_CONTRACTS_V12_MD, "CONTRACTS-V12.md")
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_*]*", text))
    namespace_tokens = sorted(t for t in tokens if _V12_NAMESPACE_RE.match(t))
    assert namespace_tokens, (
        "CONTRACTS-V12.md 未解析到任何 V12 命名空间记号:"
        "文档与解析器口径疑似双双失效,请人工复核(_V12_NAMESPACE_RE)"
    )
    ghosts = [t for t in namespace_tokens if t not in code_v12]
    assert not ghosts, (
        "CONTRACTS-V12.md 出现无法解析到 Config V12 段字段的命名空间记号"
        f"(文档幽灵字段/字段改名后未同步):{ghosts};代码实有字段:"
        f"{sorted(code_v12)}"
    )


def test_v12_doc_default_hints_match_code() -> None:
    """默认值对账:文档明示缺省 False(红线 45),运行时默认值与 AST 字面
    默认双一致;且默认值锚点原文确实点名了证据表的每个字段。"""
    from netsentinel.contracts import Config

    fields, lines = _load_config()
    code_v12 = {f.name: f for f in _code_v12_fields(fields, lines)}
    hinted = {
        name: hint for name, (_snippet, hint) in _V12_FIELD_EVIDENCE.items()
    }
    assert hinted and all(hint is False for hint in hinted.values()), (
        "V12 证据表应登记全部字段的 False 默认提示(红线 45:缺省 False=现状)"
    )
    cfg = Config()
    for name, want in hinted.items():
        info = code_v12.get(name)
        assert info is not None, f"字段 {name} 不在 Config V12 段(应已被集合对账拦截)"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值漂移:文档明示 {want!r}({type(want).__name__}),"
            f"代码运行时实为 {got!r}({type(got).__name__})"
        )
        assert info.default is not None and info.default[0] == "literal", (
            f"字段 {name} 文档明示字面默认 {want!r},代码却无字面默认"
            f"(default={info.default!r})"
        )
        assert info.default[1] == want and type(info.default[1]) is type(want), (
            f"字段 {name} AST 默认值 {info.default[1]!r} 与文档明示 {want!r} 不符"
        )
    # 锚点原文点名每个字段(防止锚点漂移成与字段无关的"缺省 False"字样)
    for name in hinted:
        assert name in _V12_DEFAULTS_ANCHOR, (
            f"默认值锚点未点名字段 {name}(_V12_DEFAULTS_ANCHOR 需人工复核)"
        )


# ---------------------------------------------------------------------------
# V13 段对账(接线残余批配置收录:A211/A204/A217 附加属性 → Config 一等
# 字段,沿用 V11/V12 证据表模式)
# ---------------------------------------------------------------------------

#: V13 命名空间记号:CONTRACTS-V13.md 中形似 V13 配置字段的词;用于
#: "文档 → 代码"幽灵字段检查(文档同行的 trace_id / FourEyesQueue 等记号
#: 不属 Config 命名空间,刻意不在匹配集内)。
_V13_NAMESPACE_RE = re.compile(
    r"^(?:dynamic_ttl|phash_mt_lsh_db|guard_model_path|guard_family)$"
)

#: V13 证据表:契约字段 → (收录佐证片段, 文档明示默认值或省略)。
#: 收录佐证锚定 CONTRACTS-V13.md §2"后续机会·接线残余批"行(原文一次点名
#: 四字段);默认值锚点:dynamic_ttl 见 §1 A211 行"dynamic_ttl 默认关"
#: (:data:`_V13_DEFAULTS_ANCHOR`),其余三键文档未明示默认(收录语义 =
#: 精确保持收录前 getattr 缺省行为)。契约文档修改后此表必须同步复核。
_V13_FIELD_EVIDENCE: dict[str, tuple[str, object]] = {
    "dynamic_ttl": ("dynamic_ttl、phash_mt_lsh_db 收录 Config", False),
    "phash_mt_lsh_db": ("dynamic_ttl、phash_mt_lsh_db 收录 Config", None),
    "guard_model_path": ("guard_model_path/guard_family", ""),
    "guard_family": ("guard_model_path/guard_family", ""),
}

#: 收录锚点原文:§2 接线残余批行(四字段的收录佐证所在;含"需预改
#: not hasattr 断言"的预告,即本批 tests/test_scheduler.py 的断言语义追认)。
_V13_LANDING_ANCHOR = "guard_model_path/guard_family、dynamic_ttl、phash_mt_lsh_db 收录 Config"

#: 默认值锚点:§1 A211 行原文(dynamic_ttl 默认关 = 现状固定库级 TTL)。
_V13_DEFAULTS_ANCHOR = "dynamic_ttl 默认关"


def _code_v13_fields(fields: list[_FieldInfo], lines: list[str]) -> list[_FieldInfo]:
    """切出 Config 的 V13 段(``# ---- V13:`` 起,至末字段);标记缺失即失败。"""
    return _code_version_fields(fields, lines, "13")


def test_v13_segment_matches_evidence_table_bidirectionally() -> None:
    """双向集合对账:Config V13 段字段 == 证据表键(差集打印)。

    证据表是代码段与(冻结的)契约文档之间的显式桥:新增 V13 字段必须
    补证据条目,删除/改名字段必须同步清理,双向都不许漂移。
    """
    fields, lines = _load_config()
    code_v13 = {f.name for f in _code_v13_fields(fields, lines)}
    table_keys = set(_V13_FIELD_EVIDENCE)
    code_only = sorted(code_v13 - table_keys)
    table_only = sorted(table_keys - code_v13)
    assert not code_only, (
        "代码有而证据表缺:contracts.py Config 的 V13 段存在未登记对账证据的字段:"
        f"{code_only}(新增 V13 字段必须补 _V13_FIELD_EVIDENCE 条目)"
    )
    assert not table_only, (
        "证据表有而代码缺:_V13_FIELD_EVIDENCE 登记了 Config V13 段不存在的字段:"
        f"{table_only}(字段被改名/删除后证据表未同步)"
    )


def test_v13_code_fields_have_real_doc_evidence() -> None:
    """代码 → 文档:每个 V13 字段的收录佐证片段与收录/默认值锚点必须真实
    出现在 CONTRACTS-V13.md(§2 收录锚点 + §1 A211 默认值锚点)。"""
    fields, lines = _load_config()
    code_v13 = {f.name for f in _code_v13_fields(fields, lines)}
    assert code_v13, "V13 段为空(应已被段提取测试拦截)"
    text = _load_md(_CONTRACTS_V13_MD, "CONTRACTS-V13.md")
    if "## 2." not in text:
        pytest.fail(
            "CONTRACTS-V13.md 缺少 '## 2.' 后续机会标题:文档结构变化,"
            "V13 解析器需人工复核(同步机拒绝静默)"
        )
    missing = [
        f"{name}(佐证 {snippet!r})"
        for name, (snippet, _hint) in _V13_FIELD_EVIDENCE.items()
        if name in code_v13 and snippet not in text
    ]
    assert not missing, (
        "契约文档中找不到字段收录佐证片段(文档漂移,或字段被改名/证据写错):"
        f"{missing}"
    )
    assert _V13_LANDING_ANCHOR in text, (
        f"CONTRACTS-V13.md 缺少收录锚点 {_V13_LANDING_ANCHOR!r}:"
        "§2 接线残余批文案漂移,V13 收录对账无从锚定"
    )
    assert _V13_DEFAULTS_ANCHOR in text, (
        f"CONTRACTS-V13.md 缺少默认值锚点 {_V13_DEFAULTS_ANCHOR!r}:"
        "§1 A211 行文案漂移,dynamic_ttl 默认值对账无从锚定"
    )


def test_v13_doc_mentions_resolve_to_code_fields() -> None:
    """文档 → 代码:V13 文档中的命名空间记号必须全部命中真实字段(抓幽灵)。"""
    fields, lines = _load_config()
    code_v13 = {f.name for f in _code_v13_fields(fields, lines)}
    text = _load_md(_CONTRACTS_V13_MD, "CONTRACTS-V13.md")
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_*]*", text))
    namespace_tokens = sorted(t for t in tokens if _V13_NAMESPACE_RE.match(t))
    assert namespace_tokens, (
        "CONTRACTS-V13.md 未解析到任何 V13 命名空间记号:"
        "文档与解析器口径疑似双双失效,请人工复核(_V13_NAMESPACE_RE)"
    )
    ghosts = [t for t in namespace_tokens if t not in code_v13]
    assert not ghosts, (
        "CONTRACTS-V13.md 出现无法解析到 Config V13 段字段的命名空间记号"
        f"(文档幽灵字段/字段改名后未同步):{ghosts};代码实有字段:"
        f"{sorted(code_v13)}"
    )


def test_v13_doc_default_hints_match_code() -> None:
    """默认值对账:运行时默认值与 AST 字面默认双一致(None 默认经 AST 归约为
    'other',仅运行时校验);收录锚点原文确实点名了证据表的每个字段。"""
    from netsentinel.contracts import Config

    fields, lines = _load_config()
    code_v13 = {f.name: f for f in _code_v13_fields(fields, lines)}
    hinted = {name: hint for name, (_snippet, hint) in _V13_FIELD_EVIDENCE.items()}
    assert hinted and all(
        hint in (False, None, "") for hint in hinted.values()
    ), "V13 证据表应登记全部字段的收录前 getattr 缺省口径(False/None/空串)"
    cfg = Config()
    for name, want in hinted.items():
        info = code_v13.get(name)
        assert info is not None, f"字段 {name} 不在 Config V13 段(应已被集合对账拦截)"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值漂移:证据表登记 {want!r}({type(want).__name__}),"
            f"代码运行时实为 {got!r}({type(got).__name__})"
        )
        if want is not None:  # None 默认经 AST 归约为 'other',运行时已校验
            assert info.default is not None and info.default[0] == "literal", (
                f"字段 {name} 证据表登记字面默认 {want!r},代码却无字面默认"
                f"(default={info.default!r})"
            )
            assert info.default[1] == want and type(info.default[1]) is type(want), (
                f"字段 {name} AST 默认值 {info.default[1]!r} 与证据表 {want!r} 不符"
            )
    # 收录锚点原文点名每个字段(防止锚点漂移成与字段无关的"收录 Config"字样)
    for name in hinted:
        assert name in _V13_LANDING_ANCHOR, (
            f"收录锚点未点名字段 {name}(_V13_LANDING_ANCHOR 需人工复核)"
        )


# ---------------------------------------------------------------------------
# V14 段对账(接线残余批配置收录:A223 贝叶斯回流开关升格,沿用证据表模式)
# ---------------------------------------------------------------------------

#: V14 命名空间记号:CONTRACTS-V14.md 中形似 V14 配置字段的词;§1 A223 行的
#: "bayes 41/64 权重生效"等数值记号不属 Config 命名空间,刻意不在匹配集内。
_V14_NAMESPACE_RE = re.compile(r"^(?:bayes_reliability|bayes_half_life)$")

#: V14 证据表:契约字段 → (收录佐证片段, 证据表登记默认值)。
#: 收录佐证锚定 CONTRACTS-V14.md §2"后续机会·接线残余"行(原文只点名
#: ``bayes_reliability``;``bayes_half_life`` 属收录开关的伴随漂移参数,
#: 无独立文档锚点,与开关共用同一收录行佐证——默认值 None = 关闭遗忘 =
#: 收录前现状,为收录任务口径而非文档明示)。契约文档修改后此表必须同步复核。
_V14_FIELD_EVIDENCE: dict[str, tuple[str, object]] = {
    "bayes_reliability": ("bayes_reliability 待 V14 收录 Config", False),
    "bayes_half_life": ("bayes_reliability 待 V14 收录 Config", None),
}

#: 收录锚点原文:§2 接线残余行(以 CONTRACTS-V14.md 原文措辞为准,逐字复制)。
_V14_LANDING_ANCHOR = "bayes_reliability 待 V14 收录 Config(附加属性模式)"

#: 锚点原文点名的字段(bayes_half_life 无独立锚点,不参与点名检查;
#: 其默认值经证据表登记口径对账)。
_V14_ANCHOR_NAMED = ("bayes_reliability",)


def _code_v14_fields(fields: list[_FieldInfo], lines: list[str]) -> list[_FieldInfo]:
    """切出 Config 的 V14 段(``# ---- V14:`` 起,至末字段);标记缺失即失败。"""
    return _code_version_fields(fields, lines, "14")


def test_v14_segment_matches_evidence_table_bidirectionally() -> None:
    """双向集合对账:Config V14 段字段 == 证据表键(差集打印)。

    证据表是代码段与(冻结的)契约文档之间的显式桥:新增 V14 字段必须
    补证据条目,删除/改名字段必须同步清理,双向都不许漂移。
    """
    fields, lines = _load_config()
    code_v14 = {f.name for f in _code_v14_fields(fields, lines)}
    table_keys = set(_V14_FIELD_EVIDENCE)
    code_only = sorted(code_v14 - table_keys)
    table_only = sorted(table_keys - code_v14)
    assert not code_only, (
        "代码有而证据表缺:contracts.py Config 的 V14 段存在未登记对账证据的字段:"
        f"{code_only}(新增 V14 字段必须补 _V14_FIELD_EVIDENCE 条目)"
    )
    assert not table_only, (
        "证据表有而代码缺:_V14_FIELD_EVIDENCE 登记了 Config V14 段不存在的字段:"
        f"{table_only}(字段被改名/删除后证据表未同步)"
    )


def test_v14_code_fields_have_real_doc_evidence() -> None:
    """代码 → 文档:每个 V14 字段的收录佐证片段与收录锚点必须真实出现在
    CONTRACTS-V14.md(§2 接线残余行原文)。"""
    fields, lines = _load_config()
    code_v14 = {f.name for f in _code_v14_fields(fields, lines)}
    assert code_v14, "V14 段为空(应已被段提取测试拦截)"
    text = _load_md(_CONTRACTS_V14_MD, "CONTRACTS-V14.md")
    if "## 2." not in text:
        pytest.fail(
            "CONTRACTS-V14.md 缺少 '## 2.' 后续机会标题:文档结构变化,"
            "V14 解析器需人工复核(同步机拒绝静默)"
        )
    missing = [
        f"{name}(佐证 {snippet!r})"
        for name, (snippet, _hint) in _V14_FIELD_EVIDENCE.items()
        if name in code_v14 and snippet not in text
    ]
    assert not missing, (
        "契约文档中找不到字段收录佐证片段(文档漂移,或字段被改名/证据写错):"
        f"{missing}"
    )
    assert _V14_LANDING_ANCHOR in text, (
        f"CONTRACTS-V14.md 缺少收录锚点 {_V14_LANDING_ANCHOR!r}:"
        "§2 接线残余行文案漂移,V14 收录对账无从锚定"
    )


def test_v14_doc_mentions_resolve_to_code_fields() -> None:
    """文档 → 代码:V14 文档中的命名空间记号必须全部命中真实字段(抓幽灵)。"""
    fields, lines = _load_config()
    code_v14 = {f.name for f in _code_v14_fields(fields, lines)}
    text = _load_md(_CONTRACTS_V14_MD, "CONTRACTS-V14.md")
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_*]*", text))
    namespace_tokens = sorted(t for t in tokens if _V14_NAMESPACE_RE.match(t))
    assert namespace_tokens, (
        "CONTRACTS-V14.md 未解析到任何 V14 命名空间记号:"
        "文档与解析器口径疑似双双失效,请人工复核(_V14_NAMESPACE_RE)"
    )
    ghosts = [t for t in namespace_tokens if t not in code_v14]
    assert not ghosts, (
        "CONTRACTS-V14.md 出现无法解析到 Config V14 段字段的命名空间记号"
        f"(文档幽灵字段/字段改名后未同步):{ghosts};代码实有字段:"
        f"{sorted(code_v14)}"
    )


def test_v14_doc_default_hints_match_code() -> None:
    """默认值对账:运行时默认值与 AST 字面默认双一致(None 默认经 AST 归约为
    'other',仅运行时校验);收录锚点原文确实点名了有独立锚点的字段。"""
    from netsentinel.contracts import Config

    fields, lines = _load_config()
    code_v14 = {f.name: f for f in _code_v14_fields(fields, lines)}
    hinted = {name: hint for name, (_snippet, hint) in _V14_FIELD_EVIDENCE.items()}
    assert hinted and all(
        hint in (False, None) for hint in hinted.values()
    ), "V14 证据表应登记全部字段的收录前 getattr 缺省口径(False/None)"
    cfg = Config()
    for name, want in hinted.items():
        info = code_v14.get(name)
        assert info is not None, f"字段 {name} 不在 Config V14 段(应已被集合对账拦截)"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值漂移:证据表登记 {want!r}({type(want).__name__}),"
            f"代码运行时实为 {got!r}({type(got).__name__})"
        )
        if want is not None:  # None 默认经 AST 归约为 'other',运行时已校验
            assert info.default is not None and info.default[0] == "literal", (
                f"字段 {name} 证据表登记字面默认 {want!r},代码却无字面默认"
                f"(default={info.default!r})"
            )
            assert info.default[1] == want and type(info.default[1]) is type(want), (
                f"字段 {name} AST 默认值 {info.default[1]!r} 与证据表 {want!r} 不符"
            )
    # 收录锚点原文点名检查(仅覆盖有独立锚点的字段;bayes_half_life 为
    # 伴随参数、文档未点名,不参与——见 _V14_ANCHOR_NAMED 注释)
    for name in _V14_ANCHOR_NAMED:
        assert name in _V14_LANDING_ANCHOR, (
            f"收录锚点未点名字段 {name}(_V14_LANDING_ANCHOR 需人工复核)"
        )
