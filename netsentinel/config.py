"""NetSentinel(净网哨兵)配置读写模块。[A01]

职责:
- ``load_config``:默认读取 ``./config.yaml``;文件不存在时返回纯默认 :class:`Config`;
  YAML 顶层键与 ``Config`` 字段同名映射,未知键 ``logging.warning`` 后忽略;
  按 CONTRACTS §2 做范围校验,违反抛 ``ValueError``(中文消息,含字段名与当前值)。
- ``save_config``:``dataclasses.asdict`` 后写 YAML,自动创建父目录。

安全红线(必须体现在代码里):
- ``human_gate_required=True`` 不可通过配置关闭,尝试关闭时强制恢复并告警。
- PyYAML 惰性导入:未安装且没有自定义配置文件时,直接返回默认 ``Config``,
  不让纯默认场景因缺少三方库而失败。

V5 工程升级:
- 路径探测只做一次 ``stat``(默认路径存在时原先探测两次);
- 未知键告警按进程内同键去重(同一未知键只告警一次,重扫描/热重载不再刷屏);
- ``load_config``/``save_config`` 接入 ``telemetry.timer("config.load"/"config.save")``;
- 范围校验一次性收集全部违规,按字段分组后抛出(单条违规文案与升级前逐字一致)。

V11 配置正式收录(附加属性升格为一等 :class:`Config` 字段):

- V10.4 期间 ``ensemble_reliability_weights`` 曾以"附加实例属性"特殊路径
  拦截注入(默认关闭 = 等权,行为与历史版本完全一致);V10.5 的 A194/A197
  又以 ``getattr(cfg, 名, 缺省)`` 动态读取 ``graph_wire`` / ``gang_*`` /
  ``cascade_risk_budget`` 等附加属性。V11 起这些键全部升格为 Config 的
  **一等 dataclass 字段**(contracts.py ``# ---- V11:`` 段,11 个新字段),
  本模块的附加属性特殊路径随之删除——新键走常规加载/落盘:
  未知键告警路径不变、类型按字段分组规整、取值域进入 ``_validate``;
- 全部默认值精确保持升格前行为:**开关全关**(graph_wire /
  ensemble_reliability_weights = False)、**静态带**(cascade_risk_budget =
  None → 冷启动静态带)、**等权**(ensemble_reliability_weights = False)、
  **connectivity 现状口径**(gang_mode = "connectivity";其权重阈值/降权
  系数仅 community 模式消费,connectivity 模式忽略,缺省行为零变化);
- 消费方原有的 ``getattr(cfg, 名, 旧缺省)`` 读取路径无缝切换:同名字段
  getattr 命中 dataclass 字段,旧缺省值与新字段默认值语义一致;
- ``save_config`` 持久化语义对齐既有布尔/数值字段惯例(asdict 全量落盘,
  None 值写出为 ``null``);V10.4 的"默认关闭不写该键"特殊落盘逻辑随
  附加属性路径一并删除。

V12 接线收口(``trace_enabled`` / ``abstain_enabled`` 升格为一等字段):

- A202 在 V11 接线期以 ``getattr(cfg, "trace_enabled", False)`` /
  ``getattr(cfg, "abstain_enabled", False)`` 附加属性方式读取的两开关,
  V12 起升格为 Config 一等布尔字段(contracts.py ``# ---- V12:`` 段);
- 默认值精确保持升格前行为:**False = 现状**(红线 45:trace/弃权默认
  可关且不触判定——无头、无 span、无 intel 键、入队不带 kwargs);
- 走常规布尔组加载/校验/保存:非布尔值按既有布尔字段口径抛中文
  ``ValueError``;``save_config`` 经 asdict 全量落盘(``false`` 字面);
- 消费方(orchestrator ``run_scan`` 的 getattr 门控)同名字段无缝命中,
  读取路径与缺省语义零变化。

V13 接线残余批配置收录(``dynamic_ttl`` / ``phash_mt_lsh_db`` /
``guard_model_path`` / ``guard_family`` 升格为一等字段,CONTRACTS-V13.md
§2 点名):

- A211 scheduler(``dynamic_ttl``)/ A204 kernel_wire(``phash_mt_lsh_db``)/
  A217 guard_adapter(``guard_model_path`` / ``guard_family``)在 V13 波以
  ``getattr(cfg, 名, 缺省)`` 附加属性方式读取的四键,V13 收录批起升格为
  Config 一等字段(contracts.py ``# ---- V13:`` 段);
- 默认值精确保持收录前行为:**dynamic_ttl = False**(动态 TTL 关,零图谱
  咨询)、**phash_mt_lsh_db = None**(空 = 按 ``phash_db`` 推导
  ``<phash_db>.mtlsh``,与 kernel_wire._mt_lsh_db_path 的 falsy 分支一致)、
  **guard_model_path / guard_family = ""**(守卫模型未注入,由构造参数决定);
- 加载口径:``dynamic_ttl`` 走布尔组;``phash_mt_lsh_db`` 走可空字符串组
  (显式 null / 空串 / 纯空白 → None = 走推导);``guard_model_path`` 常规
  字符串;``guard_family`` 取值域 "" 或 guard_adapter 的 GUARD_FAMILIES
  三族(字面量在本模块复写并注释来源,核心配置层不 import vision);
- ``save_config`` 惯例对齐:asdict 全量落盘(None 写出为 ``null``);
  消费方 getattr 读取路径同名字段无缝命中,缺省语义零变化。

V14 接线残余批配置收录(``bayes_reliability`` / ``bayes_half_life`` 升格为
一等字段,CONTRACTS-V14.md §2 点名"bayes_reliability 待 V14 收录 Config"):

- A223 贝叶斯回流在 V14 波以 ``getattr(cfg, "bayes_reliability", False)``
  附加属性方式读取的开关,连同其遗忘半衰期参数 ``bayes_half_life``
  (任务口径:漂移感知伴随参数,None = 关闭遗忘 = 现状)V14 收录批起
  升格为 Config 一等字段(contracts.py ``# ---- V14:`` 段);
- 默认值精确保持收录前行为:**bayes_reliability = False**(开关关 = 现状
  等权,优先级门控语义不变)、**bayes_half_life = None**(关闭遗忘——
  贝叶斯重放对没有时间信息的源不做指数衰减);
- 加载口径:``bayes_reliability`` 走布尔组;``bayes_half_life`` 走可空
  数值组(显式 null → None = 关闭遗忘),取值域为**正有限数**(单位天;
  0 / 负数 / NaN / inf 均抛中文 ``ValueError``);
- ``save_config`` 惯例对齐:asdict 全量落盘(None 写出为 ``null``);
  消费方(orchestrator 贝叶斯重放)getattr 读取路径同名字段无缝命中,
  缺省语义零变化。

用法示例::

    from netsentinel.config import load_config, save_config

    cfg = load_config("config.yaml")     # 文件不存在则返回纯默认 Config
    save_config(cfg, "out/config.yaml")  # 自动创建父目录
"""
from __future__ import annotations

import dataclasses
import logging
import math
import pathlib
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "load_config",
    "save_config",
]

logger = logging.getLogger(__name__)

#: 默认配置文件名(相对当前工作目录解析)
DEFAULT_CONFIG_FILENAME = "config.yaml"

# Config 各字段的标量类型分组(未列出的字段一律按 str 处理)
_FLOAT_FIELDS = frozenset({
    "nsfw_threshold",
    "review_threshold",
    "prob_count_line",
    "fetch_timeout_s",
    "fetch_delay_s",
    "vlm_escalate_above",
    "vlm_escalate_below",
    "conformal_target_precision",
    "vlm_request_timeout_s",
    "vlm_max_image_mb",
    "group_merge_phash_overlap",
    "discovery_query_delay_s",
    "sprt_alpha",
    "sprt_beta",
    # V11:配置正式收录(社区检测阈值刻度/弃权阈值)
    "gang_weight_threshold",
    "gang_template_weight_factor",
    "gang_resolution",
    "abstain_threshold",
})
_INT_FIELDS = frozenset({
    "min_nsw_images",
    "min_image_px",
    "max_pages",
    "max_images_per_page",
    "max_image_mb",
    "submit_min_interval_s",
    "submit_max_per_day",
    "vlm_max_images_per_site",
    "vlm_daily_budget",
    "service_port",
    "redirect_max_hops",
    "video_max_frames",
    "adaptive_base_interval_h",
    "batch_max_items",
    "batch_item_interval_s",
    "discovery_max_per_query",
    "discovery_max_total",
    "discovery_cache_ttl_h",
    "phash_lsh_bands",
    "agents_max_workers",
    "setup_port",
    "cpu_reserve",
})
_BOOL_FIELDS = frozenset({
    "respect_robots",
    "allow_network",
    "human_gate_required",
    "dry_run_default",
    "vlm_online",
    "use_fusion",
    "vlm_cascade",
    "four_eyes_required",
    "group_merge_template",
    "batch_require_attestation",
    "discovery_online",
    "use_sprt",
    "use_reliability_fusion",
    "browser_session_reuse",
    "sched_priority",
    "onboarding_auto_open",
    "plugin_supervised_api",
    "takeover_auto",
    "concurrency_auto",
    "summary_agent_enabled",
    "ensemble_reliability_weights",
    # V11:配置正式收录(图谱通电总开关)
    "graph_wire",
    # V12:接线收口(A202 附加属性升格:全链追踪/分歧弃权两布尔开关)
    "trace_enabled",
    "abstain_enabled",
    # V13:接线残余批收录(A211 scheduler 附加属性升格:动态 TTL 布尔开关)
    "dynamic_ttl",
    # V14:接线残余批收录(A223 贝叶斯回流开关升格;默认 False = 现状等权)
    "bayes_reliability",
})
_LIST_STR_FIELDS = frozenset(
    {"ensemble_members", "glm_models_fallback", "vlm_fallback_chain", "local_probe_ports"}
)
_DICT_STR_FIELDS = frozenset(
    {"vlm_api_keys", "vlm_provider_base_urls", "vlm_provider_models"}
)

# V11 可空标量字段:YAML 显式 null / 未写键 → None(= 现状默认);否则按
# 基础类型规整(空串/纯空白字符串视同未设置,归一为 None 保持"未配置"语义)。
# V14 增补:``bayes_half_life``(可空数值,None = 关闭遗忘 = 现状;数值语义
# 不做空白归一——它是数字不是字符串,仅 null / 数字两态)。
_OPT_FLOAT_FIELDS = frozenset({
    "cascade_risk_budget",
    # V14:接线残余批收录(A223 贝叶斯遗忘半衰期,单位天;取值域正有限数
    # 见 _validate,None = 关闭遗忘 = 收录前现状)
    "bayes_half_life",
})
_OPT_STR_FIELDS = frozenset({
    "tsa_url",
    "ed25519_seed_hex",
    # V13:接线残余批收录(A204 kernel_wire 附加属性升格;None = 按 phash_db
    # 推导 <phash_db>.mtlsh,空串/纯空白同 null——"未配置即走推导"语义)
    "phash_mt_lsh_db",
})

# V13:守卫模型族合法取值。字面量复写自 netsentinel/vision/guard_adapter.py
# 的 GUARD_FAMILIES 常量(shieldgemma2 / llamaguard / custom-prompt)——本模块
# 属核心配置层,刻意不 import vision(零内部依赖分层;vision 侧改族清单时
# 以其测试 test_guard_adapter 的 GUARD_FAMILIES 断言为准,此处须人工同步)。
_GUARD_FAMILIES: tuple[str, ...] = ("shieldgemma2", "llamaguard", "custom-prompt")
#: guard_family 合法取值域:三族 + ""(未配置 = 按 model_path 推断)。
_GUARD_FAMILY_VALUES: tuple[str, ...] = ("",) + _GUARD_FAMILIES

_KNOWN_FIELDS = frozenset(f.name for f in dataclasses.fields(Config))

#: 进程内已告警过的未知键(同键只告警一次;去重,避免热重载/重扫描刷屏)
_WARNED_UNKNOWN_KEYS: set[str] = set()


def _reset_unknown_key_warnings() -> None:
    """清空未知键告警去重状态(仅测试使用,业务代码无需调用)。"""
    _WARNED_UNKNOWN_KEYS.clear()


def _import_yaml() -> Any:
    """惰性导入 PyYAML;缺失时抛带中文安装提示的 ImportError。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise ImportError(
            "读取/写入 YAML 配置需要 PyYAML,请先安装:pip install PyYAML"
        ) from exc
    return yaml


def _coerce_field(name: str, value: Any) -> Any:
    """把 YAML 标量规整为 Config 字段类型;类型不符抛 ValueError(中文)。"""
    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"配置项 {name} 的值应为布尔(true/false),当前为 {value!r}")
        return value
    if name in _FLOAT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"配置项 {name} 的值应为数字,当前为 {value!r}")
        return float(value)
    if name in _INT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
                return int(value)
            raise ValueError(f"配置项 {name} 的值应为整数,当前为 {value!r}")
        return int(value)
    if name in _LIST_STR_FIELDS:
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"配置项 {name} 的值应为字符串列表,当前为 {value!r}")
        return list(value)
    if name in _DICT_STR_FIELDS:
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError(
                f"配置项 {name} 的值应为「提供方: 字符串」映射,当前为 {value!r}"
            )
        return dict(value)
    if name in _OPT_FLOAT_FIELDS:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"配置项 {name} 的值应为数字或 null,当前为 {value!r}")
        return float(value)
    if name in _OPT_STR_FIELDS:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"配置项 {name} 的值应为字符串或 null,当前为 {value!r}")
        return value.strip() or None  # 空串/纯空白 = 未配置(与消费方缺省语义一致)
    if not isinstance(value, str):
        raise ValueError(f"配置项 {name} 的值应为字符串,当前为 {value!r}")
    return value


def _validate(cfg: Config) -> None:
    """按 CONTRACTS §2 校验范围;违反抛 ValueError(中文,含字段名与当前值)。

    V5:一次性收集全部违规再抛出。单条违规的消息与升级前逐字一致;多条违规时
    逐条换行列出,并追加一行「校验失败字段分组」汇总(字段名 × 次数)便于定位。
    """
    errors: list[tuple[str, str]] = []  # (字段名, 完整中文消息)

    def fail(field: str, message: str) -> None:
        errors.append((field, message))

    if cfg.review_threshold <= 0:
        fail(
            "review_threshold",
            f"配置项 review_threshold={cfg.review_threshold} 无效:必须满足 0 < review_threshold",
        )
    if cfg.review_threshold > cfg.nsfw_threshold:
        fail(
            "review_threshold",
            f"配置项 review_threshold={cfg.review_threshold} 无效:"
            f"必须满足 review_threshold <= nsfw_threshold(当前 nsfw_threshold={cfg.nsfw_threshold})",
        )
    if cfg.nsfw_threshold > 1:
        fail(
            "nsfw_threshold",
            f"配置项 nsfw_threshold={cfg.nsfw_threshold} 无效:必须满足 nsfw_threshold <= 1",
        )
    if cfg.prob_count_line > cfg.nsfw_threshold:
        fail(
            "prob_count_line",
            f"配置项 prob_count_line={cfg.prob_count_line} 无效:"
            f"必须满足 prob_count_line <= nsfw_threshold(当前 nsfw_threshold={cfg.nsfw_threshold})",
        )
    if cfg.submit_min_interval_s < 30:
        fail(
            "submit_min_interval_s",
            f"配置项 submit_min_interval_s={cfg.submit_min_interval_s} 无效:"
            f"必须满足 submit_min_interval_s >= 30",
        )
    if not 1 <= cfg.submit_max_per_day <= 20:
        fail(
            "submit_max_per_day",
            f"配置项 submit_max_per_day={cfg.submit_max_per_day} 无效:"
            f"必须满足 1 <= submit_max_per_day <= 20",
        )
    if cfg.max_pages < 1:
        fail(
            "max_pages",
            f"配置项 max_pages={cfg.max_pages} 无效:必须满足 max_pages >= 1",
        )

    # V6:批量举报红线 26 —— 批内间隔不得短于全局提交最小间隔
    if cfg.batch_item_interval_s < cfg.submit_min_interval_s:
        fail(
            "batch_item_interval_s",
            f"配置项 batch_item_interval_s={cfg.batch_item_interval_s} 无效:批量模式下频控不得放宽,"
            f"必须 >= submit_min_interval_s={cfg.submit_min_interval_s}",
        )
    if cfg.batch_max_items < 1 or cfg.batch_max_items > 50:
        fail(
            "batch_max_items",
            f"配置项 batch_max_items={cfg.batch_max_items} 无效:单批条数须在 1~50 之间",
        )

    # V6.5:发现层限速与预算红线 28 —— 上限与间隔不得为绕过引擎限速而放宽到离谱值
    if cfg.discovery_max_per_query < 1 or cfg.discovery_max_per_query > 50:
        fail(
            "discovery_max_per_query",
            f"配置项 discovery_max_per_query={cfg.discovery_max_per_query} 无效:单查询取数须在 1~50 之间",
        )
    if cfg.discovery_max_total < 1 or cfg.discovery_max_total > 500:
        fail(
            "discovery_max_total",
            f"配置项 discovery_max_total={cfg.discovery_max_total} 无效:单轮线索总数须在 1~500 之间",
        )
    if cfg.discovery_query_delay_s < 1.0:
        fail(
            "discovery_query_delay_s",
            f"配置项 discovery_query_delay_s={cfg.discovery_query_delay_s} 无效:查询间隔不得小于 1 秒(引擎礼貌限速)",
        )

    # V7:内核开关取值域
    if not (0 < cfg.sprt_alpha < 0.5) or not (0 < cfg.sprt_beta < 0.5):
        fail(
            "sprt_alpha",
            f"配置项 sprt_alpha/sprt_beta={cfg.sprt_alpha}/{cfg.sprt_beta} 无效:错误率须在 (0, 0.5) 区间",
        )
    if cfg.phash_lsh_bands < 1 or cfg.phash_lsh_bands > 8:
        fail(
            "phash_lsh_bands",
            f"配置项 phash_lsh_bands={cfg.phash_lsh_bands} 无效:分带数须在 1~8 之间",
        )

    # V8:多开 Agent 并发上限
    if cfg.agents_max_workers < 1 or cfg.agents_max_workers > 16:
        fail(
            "agents_max_workers",
            f"配置项 agents_max_workers={cfg.agents_max_workers} 无效:并发 Agent 数须在 1~16 之间",
        )

    # V9:并发档位与资源治理(红线 37)
    if cfg.concurrency_tier not in ("low", "mid", "high"):
        fail(
            "concurrency_tier",
            f"配置项 concurrency_tier={cfg.concurrency_tier!r} 无效:必须为 low / mid / high 三档之一",
        )
    if cfg.cpu_reserve < 0 or cfg.cpu_reserve > 64:
        fail(
            "cpu_reserve",
            f"配置项 cpu_reserve={cfg.cpu_reserve} 无效:保留核心数须在 0~64 之间",
        )

    # V11:配置正式收录——升格字段的取值域(gang_*/graph_wire 见 A194,
    # cascade_risk_budget 见 A197,abstain_threshold 见 A196,签名三键见 A192)
    if cfg.gang_mode not in ("connectivity", "community"):
        fail(
            "gang_mode",
            f"配置项 gang_mode={cfg.gang_mode!r} 无效:必须为 connectivity / community 之一",
        )
    if not 0 <= cfg.gang_weight_threshold <= 1:
        fail(
            "gang_weight_threshold",
            f"配置项 gang_weight_threshold={cfg.gang_weight_threshold} 无效:"
            f"必须满足 0 <= gang_weight_threshold <= 1",
        )
    if not 0 <= cfg.gang_template_weight_factor <= 1:
        fail(
            "gang_template_weight_factor",
            f"配置项 gang_template_weight_factor={cfg.gang_template_weight_factor} 无效:"
            f"必须满足 0 <= gang_template_weight_factor <= 1",
        )
    if cfg.gang_resolution <= 0:
        fail(
            "gang_resolution",
            f"配置项 gang_resolution={cfg.gang_resolution} 无效:必须满足 gang_resolution > 0",
        )
    if not 0 <= cfg.abstain_threshold <= 1:
        fail(
            "abstain_threshold",
            f"配置项 abstain_threshold={cfg.abstain_threshold} 无效:"
            f"必须满足 0 <= abstain_threshold <= 1",
        )
    if cfg.cascade_risk_budget is not None and not 0 <= cfg.cascade_risk_budget <= 1:
        fail(
            "cascade_risk_budget",
            f"配置项 cascade_risk_budget={cfg.cascade_risk_budget} 无效:"
            f"必须满足 0 <= cascade_risk_budget <= 1,或置 null 表示不设预算(静态带)",
        )
    if cfg.bundle_sign_algo not in ("hmac-sha256", "ed25519"):
        fail(
            "bundle_sign_algo",
            f"配置项 bundle_sign_algo={cfg.bundle_sign_algo!r} 无效:"
            f"必须为 hmac-sha256 / ed25519 之一",
        )
    if cfg.ed25519_seed_hex is not None:
        seed = cfg.ed25519_seed_hex
        if len(seed) != 64 or any(c not in "0123456789abcdefABCDEF" for c in seed):
            # 不回显 seed 内容(密钥不入日志/异常文案),只报长度
            fail(
                "ed25519_seed_hex",
                f"配置项 ed25519_seed_hex 无效:必须为 64 位十六进制字符串"
                f"(32 字节 Ed25519 私钥 seed),当前长度 {len(seed)}",
            )

    # V13:接线残余批收录——升格字段的取值域(guard_family 见 A217;字面量
    # 复写自 vision/guard_adapter.py 的 GUARD_FAMILIES,见 _GUARD_FAMILIES 注释)
    if cfg.guard_family not in _GUARD_FAMILY_VALUES:
        fail(
            "guard_family",
            f"配置项 guard_family={cfg.guard_family!r} 无效:"
            f"必须为 {'/'.join(_GUARD_FAMILIES)} 之一,或留空表示按模型路径推断",
        )

    # V14:接线残余批收录——bayes_half_life 取值域(正有限数或 None = 关闭遗忘)。
    # 与 BayesianReliabilityTracker 构造口径一致(half_life 必须为正有限数,
    # inf 在配置层即拒绝——tracker 虽把 inf 视同关闭遗忘,配置语义上
    # "想关闭遗忘"应显式写 null,不让 YAML 的 .inf 字面量隐式通过)。
    hl = cfg.bayes_half_life
    if hl is not None:
        hl_valid = (
            isinstance(hl, (int, float))
            and not isinstance(hl, bool)
            and math.isfinite(float(hl))
            and float(hl) > 0.0
        )
        if not hl_valid:
            fail(
                "bayes_half_life",
                f"配置项 bayes_half_life={hl} 无效:"
                f"必须为正有限数(贝叶斯遗忘半衰期,单位天),或置 null 表示关闭遗忘",
            )

    if not errors:
        return
    if len(errors) == 1:
        raise ValueError(errors[0][1])
    grouped: dict[str, int] = {}
    for field, _ in errors:
        grouped[field] = grouped.get(field, 0) + 1
    summary = "、".join(f"{field}({count} 处)" for field, count in grouped.items())
    raise ValueError("\n".join(message for _, message in errors) + f"\n校验失败字段分组:{summary}")


def load_config(path: str | None = None) -> Config:
    """加载配置。

    - ``path`` 为空时默认找 ``./config.yaml``,不存在则返回纯默认 :class:`Config`。
    - YAML 顶层键与 ``Config`` 字段同名;未知键告警并忽略(进程内同键只告警一次)。
    - V11 起原附加实例属性键(``ensemble_reliability_weights`` / ``graph_wire`` /
      ``gang_*`` / ``cascade_risk_budget`` / ``abstain_threshold`` / 签名三键)均为
      :class:`Config` 一等字段,走常规同名映射加载(默认值 = 升格前行为)。
    - 范围校验见 CONTRACTS §2,违反抛 :class:`ValueError`(中文消息;多条违规
      按字段分组列出,便于一次定位全部问题)。
    - 安全红线:``human_gate_required: false`` 会被强制恢复为 ``True`` 并告警。
    - 耗时计入 ``telemetry.timer("config.load")``。

    示例::

        cfg = load_config()                  # 默认 ./config.yaml,缺省即默认值
        cfg = load_config("other.yaml")      # 指定文件,不存在时告警并回退默认
    """
    with telemetry.timer("config.load"):
        # 路径只解析/探测一次(原先默认路径存在时会 stat 两次)
        file_path = (
            pathlib.Path(DEFAULT_CONFIG_FILENAME) if path is None else pathlib.Path(path)
        )
        if not file_path.is_file():
            if path is not None:
                logger.warning("配置文件不存在,使用默认配置:%s", file_path)
            return Config()

        yaml = _import_yaml()
        raw: Any = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        if raw is None:  # 空 YAML 文件 → 全默认
            return Config()
        if not isinstance(raw, dict):
            raise ValueError(f"配置文件顶层应为键值映射,当前类型为:{type(raw).__name__}")

        values: dict[str, Any] = {}
        for key, value in raw.items():
            name = str(key)
            if name not in _KNOWN_FIELDS:
                if name not in _WARNED_UNKNOWN_KEYS:  # 同键只告警一次
                    _WARNED_UNKNOWN_KEYS.add(name)
                    logger.warning("忽略未知配置键:%s(值=%r)", name, value)
                continue
            values[name] = _coerce_field(name, value)

        cfg = Config(**values)

        # 安全红线:人工门不可关闭
        if values.get("human_gate_required") is False:
            logger.warning("安全红线:human_gate_required 不可通过配置关闭,已强制恢复为 True")
            cfg.human_gate_required = True

        _validate(cfg)
        return cfg


def save_config(cfg: Config, path: str) -> None:
    """把配置写为 YAML 文件(自动创建父目录);耗时计入 ``telemetry.timer("config.save")``。

    注意:``human_gate_required=False`` 属于红线配置,写出时会被强制置为 ``True``
    并记录告警;数值范围不合法时抛 :class:`ValueError`(中文消息)。
    V11 起 ``ensemble_reliability_weights`` / ``graph_wire`` / ``gang_*`` 等
    升格字段随 :func:`dataclasses.asdict` 按既有布尔/数值字段惯例全量落盘
    (None 值写出为 ``null``);V10.4 的"默认关闭不写该键"特殊逻辑已随
    附加属性路径删除。

    示例::

        save_config(Config(), "out/config.yaml")  # 自动创建 out/ 目录
    """
    with telemetry.timer("config.save"):
        _validate(cfg)
        data = dataclasses.asdict(cfg)
        if not data.get("human_gate_required", True):
            logger.warning("安全红线:human_gate_required 不可关闭,写出时已强制置为 True")
            data["human_gate_required"] = True

        file_path = pathlib.Path(path)
        if not file_path.parent.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)

        yaml = _import_yaml()
        file_path.write_text(
            yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
