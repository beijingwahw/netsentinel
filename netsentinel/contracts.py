"""NetSentinel(净网哨兵)共享契约。

全队规范文件:所有模块间的数据结构、枚举与常量以本文件为准,只有项目负责人可修改。
包名拼写务必为 netsentinel。Python 3.10+。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def now_iso() -> str:
    """本地时区 ISO8601 时间戳(秒级)。"""
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


class Verdict(str, Enum):
    """站点判定结果。"""

    CLEAN = "clean"        # 未发现明显色情内容
    SUSPECT = "suspect"    # 疑似,需人工复核
    NSFW = "nsfw"          # 高置信色情内容(仍须人工确认后才可举报)


class Portal(str, Enum):
    """举报渠道。"""

    P12377 = "12377"  # 中央网信办违法和不良信息举报中心 www.12377.cn
    SHDF = "shdf"     # 全国"扫黄打非"工作小组办公室 www.shdf.gov.cn


# ---------------------------------------------------------------------------
# 抓取与识别阶段
# ---------------------------------------------------------------------------

@dataclass
class ImageEvidence:
    """落盘后的一张受检图片。"""

    path: str                 # 本地文件路径
    url: str                  # 原始 URL
    source_page: str          # 抓到它的页面 URL
    sha256: str = ""
    width: int = 0
    height: int = 0


@dataclass
class ImageScore:
    """单个模型对一张图的评分。"""

    image: ImageEvidence
    model: str                       # stub / nudenet / clip / ensemble
    scores: dict[str, Any] = field(default_factory=dict)
    nsfw_prob: float = 0.0           # 归一化色情概率 [0,1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "image": self.image.path,
            "model": self.model,
            "nsfw_prob": round(self.nsfw_prob, 4),
            "scores": self.scores,
        }


@dataclass
class PageSample:
    """对单个页面的抽样结果。"""

    url: str
    screenshot_path: str = ""
    image_evidences: list[ImageEvidence] = field(default_factory=list)
    text_hint_hits: list[str] = field(default_factory=list)


@dataclass
class SiteReport:
    """站点级判定报告(ensemble 评分应已并入 image_scores,model='ensemble')。"""

    site_url: str
    pages: list[PageSample] = field(default_factory=list)
    image_scores: list[ImageScore] = field(default_factory=list)
    agg_nsw_prob: float = 0.0
    nsw_image_count: int = 0
    verdict: Verdict = Verdict.CLEAN
    needs_review: bool = False
    created_at: str = field(default_factory=now_iso)
    intel: dict[str, Any] = field(default_factory=dict)  # V2:URL/文本/页面级 VLM/融合特征与解释

    def as_dict(self) -> dict[str, Any]:
        d = {
            "site_url": self.site_url,
            "pages": [
                {
                    "url": p.url,
                    "screenshot_path": p.screenshot_path,
                    "images": [i.path for i in p.image_evidences],
                    "text_hint_hits": p.text_hint_hits,
                }
                for p in self.pages
            ],
            "image_scores": [s.as_dict() for s in self.image_scores],
            "agg_nsw_prob": round(self.agg_nsw_prob, 4),
            "nsw_image_count": self.nsw_image_count,
            "verdict": self.verdict.value,
            "needs_review": self.needs_review,
            "created_at": self.created_at,
        }
        if self.intel:
            d["intel"] = self.intel
        return d


@dataclass
class EvidenceBundle:
    """举报证据包(目录 + manifest + zip)。"""

    site_url: str
    dir_path: str
    manifest_path: str
    zip_path: str = ""


# ---------------------------------------------------------------------------
# 提交(举报)阶段
# ---------------------------------------------------------------------------

class StepAction(str, Enum):
    """SubmissionPlan 中允许的步骤类型。"""

    GOTO = "goto"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    FOCUS = "focus"             # V10.1:聚焦表单元素(验证码框)——半自动焦点
    HUMAN_GATE = "human_gate"   # 人工门:核对信息 / 人工输入验证码,绝不自动处理


@dataclass
class Step:
    """一步自动化动作;executor 按 action 解释 selector/text/value。"""

    action: StepAction
    label: str = ""              # 中文说明(playbook 展示用)
    selector: str = ""           # CSS 选择器(浏览器执行器)
    text: str = ""               # 人类可读目标文本(computer-use 场景)
    value: str = ""              # fill/select 的值 / goto 的 URL / wait 的秒数字符串
    timeout_s: float = 10.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubmissionPayload:
    """一次举报要填写的全部信息(含两门户完整个人信息)。"""

    portal: Portal
    site_url: str
    category: str = "色情低俗"
    description: str = ""
    evidence_zip: str = ""
    reporter_name: str = ""
    reporter_phone: str = ""
    reason: str = ""                      # 举报理由(自动生成,≤39 字,红线 39;同时作为 description 首行)
    # V10.2:两门户完整个人信息(空=不填,由 skippable 跳过)
    reporter_email: str = ""              # 电子邮箱
    reporter_id: str = ""                 # 身份证号
    reporter_address: str = ""            # 通讯地址
    reporter_postcode: str = ""           # 邮政编码
    reporter_type: str = ""               # 举报人类型(个人/企业/组织)
    reporter_org: str = ""                # 单位名称

    def validate(self) -> list[str]:
        """返回中文错误列表;空列表 = 可提交。"""
        errors: list[str] = []
        if not self.site_url.lower().startswith(("http://", "https://")):
            errors.append("site_url 必须是 http(s):// 开头的合法链接")
        if len(self.description.strip()) < 30:
            errors.append("description 至少 30 字,需说明事实与证据情况")
        if not self.evidence_zip:
            errors.append("必须附证据包 zip(evidence_zip 为空)")
        if not self.category.strip():
            errors.append("category 不能为空")
        return errors


@dataclass
class SubmissionPlan:
    """声明式举报步骤计划:同一份计划可由 playwright 执行器或 computer-use 驱动。"""

    portal: Portal
    entry_url: str                # 举报入口完整 URL
    payload: SubmissionPayload
    steps: list[Step] = field(default_factory=list)


@dataclass
class ExecutionResult:
    """计划执行结果。"""

    ok: bool = False
    portal: str = ""
    screenshots: list[str] = field(default_factory=list)
    stopped_at: str = ""          # 中止步骤的 label
    submitted: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 全局配置(默认值即安全值)
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """全局配置(默认值即安全值)。

    字段按落地版本分组(``# ---- V2:`` … ``# ---- V13:`` 注释行切分,
    tests/test_contracts_sync.py 以此为界做契约对账)。V11 组为"配置正式
    收录"批:把 V10.4/V10.5 期间以附加实例属性(getattr 动态读取)方式
    挂载的开关升格为一等字段,默认值精确保持升格前行为(开关全关 /
    静态带 / 等权),消费方原有的 ``getattr(cfg, 名, 旧缺省)`` 读取路径
    无缝切换为命中本字段。V12 组沿用同一收录模式:``trace_enabled`` /
    ``abstain_enabled``(A202 接线期的 orchestrator 附加属性)升格,
    默认 False = 升格前 getattr 缺省口径(红线 45:默认关且不触判定)。
    V13 组为接线残余批收录(CONTRACTS-V13.md §2 点名):``dynamic_ttl``
    (A211 scheduler)/ ``phash_mt_lsh_db``(A204 kernel_wire,缺省 None =
    走 ``<phash_db>.mtlsh`` 推导)/ ``guard_model_path`` 与 ``guard_family``
    (A217 guard_adapter)升格,默认值精确保持收录前 getattr 缺省行为
    (动态 TTL 关、LSH 库走推导、守卫模型未注入)。V14 组沿用同一收录
    模式(CONTRACTS-V14.md §2 点名):``bayes_reliability``(A223 贝叶斯
    可靠性权重回流开关)/ ``bayes_half_life``(A223 贝叶斯遗忘半衰期,
    单位天;None = 关闭遗忘)升格,默认值精确保持收录前 getattr 缺省
    行为(开关关、零遗忘)。
    """

    # 判定阈值
    nsfw_threshold: float = 0.90      # 单图 ensemble 分达到该值视为"达标色情图"
    review_threshold: float = 0.50    # agg 达到该值即进入人工复核
    prob_count_line: float = 0.80     # 计数线:单图分 >= 该值计入 nsw_image_count
    min_nsw_images: int = 3           # 达标图片数达到该值才可能判 NSFW
    min_image_px: int = 200           # 过小的图(如图标)不参与判定

    # 抓取预算
    max_pages: int = 5                # 每站点最多抽样页面数
    max_images_per_page: int = 12
    max_image_mb: int = 8
    fetch_timeout_s: float = 15.0
    fetch_delay_s: float = 1.0        # 相邻请求最小间隔(礼貌抓取)
    respect_robots: bool = True
    allow_network: bool = False       # 安全默认:除 127.0.0.1/localhost 外禁止真实联网

    # 分类器
    classifier: str = "stub"          # 主分类器名;ensemble_members 里多个则做集成
    ensemble_members: list[str] = field(default_factory=lambda: ["stub"])

    # 提交安全(红线)
    human_gate_required: bool = True  # 强制人工门,不可关闭
    dry_run_default: bool = True      # 默认干跑,不真正驱动浏览器提交
    submit_min_interval_s: int = 60   # 两次提交最小间隔
    submit_max_per_day: int = 5       # 每日最大提交数

    # 举报门户
    portal_12377_base: str = "https://www.12377.cn"
    portal_shdf_base: str = "https://www.shdf.gov.cn"

    # 路径
    data_dir: str = "data"
    evidence_dir: str = "data/evidence"
    db_path: str = "data/review_queue.db"
    audit_path: str = "data/audit.jsonl"
    log_path: str = "data/logs/netsentinel.log"

    # ---- V2:GLM 视觉大模型接入 ----
    glm_api_key: str = ""             # 缺省依次读环境变量 NETSENTINEL_GLM_API_KEY / security.vault
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-5.3-flash"  # 主模型;不可用时按 glm_models_fallback 依次回退
    glm_models_fallback: list[str] = field(
        default_factory=lambda: ["glm-5.3-flash", "glm-4.5v-flash", "glm-4v-flash"]
    )
    vlm_online: bool = False          # VLM 外呼总开关(默认关:图像数据不出本机)
    vlm_max_images_per_site: int = 8  # 每站点最多送 VLM 的图片数(费用保护)
    vlm_cache_db: str = "data/vlm_cache.db"
    vlm_daily_budget: int = 200       # 每日 VLM 调用上限

    # ---- V2:平台扩展 ----
    capture_engine: str = "v1"        # v1=browser.capture_page / v2=capture_v2(懒加载滚动采样)
    use_fusion: bool = True           # 判定后叠加 URL/页面级 VLM 特征融合
    notify_webhook: str = ""          # 待复核提醒 webhook(企微/钉钉/飞书/通用),空=关
    watchlist_path: str = "watchlist.yaml"
    service_host: str = "127.0.0.1"
    service_port: int = 8765

    # ---- V3:智能体编排与平台化 ----
    case_agent_model: str = ""            # 案件智能体/级联升级模型;空=跟随 glm_model
    vlm_cascade: bool = False             # 级联路由:flash 先行,不确定带内升级
    vlm_escalate_above: float = 0.85      # 不确定带上界:flash 分值落在 [below,above] 带内才升级大模型
    vlm_escalate_below: float = 0.15      # 不确定带下界:带外(高置信)直接用 flash 结果
    phash_db: str = "data/phash.db"       # 感知哈希库(跨案件近重复识别)
    graph_db: str = "data/graph.db"       # 站点关联图谱
    four_eyes_required: bool = False      # 双人复核(四眼原则):提交需两名审核人批准
    policy_path: str = "policy.yaml"      # 声明式政策文件
    redirect_max_hops: int = 5            # 重定向链最大跳数
    video_max_frames: int = 6             # 视频/GIF 每站最多采样帧数
    conformal_target_precision: float = 0.95  # 共形预测目标精度担保
    adaptive_base_interval_h: int = 72    # 自适应重扫基准间隔(小时)

    # ---- V4:全平台视觉模型统一接入 ----
    vlm_provider: str = "glm"             # 默认提供方(classifier 不带前缀时使用)
    vlm_api_keys: dict = field(default_factory=dict)            # 提供方 -> 密钥(最高优先级)
    vlm_provider_base_urls: dict = field(default_factory=dict)  # 提供方 -> base_url 覆盖
    vlm_provider_models: dict = field(default_factory=dict)     # 提供方 -> 默认模型覆盖
    vlm_fallback_chain: list = field(default_factory=list)      # ["glm:glm-5.3-flash", "openai:gpt-4o-mini"] 故障转移链
    vlm_request_timeout_s: float = 90.0   # 跨平台 VLM 请求超时
    vlm_max_image_mb: float = 8.0         # 送审单图大小上限

    # ---- V6:大批量筛选 / 归纳同名 / 批量举报 ----
    group_merge_phash_overlap: float = 0.3   # 组间合并:图片指纹集合重叠率阈值
    group_merge_template: bool = True        # 组间合并:共享模板也并入同组
    batch_max_items: int = 20                # 单批最大举报条数
    batch_item_interval_s: int = 90          # 批内两次提交最小间隔(须 ≥ submit_min_interval_s)
    batch_require_attestation: bool = True   # 批量确认须逐组核实声明(留痕审计)

    # ---- V6.5:搜索引擎线索发现(优先 Yandex,自定义关键词) ----
    discovery_online: bool = False            # 发现层外呼总开关(默认关:零外呼)
    discovery_engine: str = "yandex"          # yandex / searxng / mock
    discovery_max_per_query: int = 10         # 单查询最多取结果数
    discovery_max_total: int = 100            # 单轮发现总上限(线索预算)
    discovery_query_delay_s: float = 3.0      # 相邻查询最小间隔(引擎礼貌限速)
    discovery_cache_db: str = "data/discovery_cache.db"
    discovery_cache_ttl_h: int = 168          # 查询结果缓存天数(默认 7 天)
    yandex_xml_user: str = ""                 # 缺省读环境变量 NETSENTINEL_YANDEX_USER
    yandex_xml_key: str = ""                  # 缺省读环境变量 NETSENTINEL_YANDEX_KEY
    searxng_base_url: str = "http://127.0.0.1:8888"  # 自托管 SearXNG 实例

    # ---- V7:内核进化(全部默认关/兼容,旧调用方零感知) ----
    use_sprt: bool = False                # 决策内核:序贯检验早停(省 VLM 预算)
    sprt_alpha: float = 0.05              # SPRT 第一类错误上限
    sprt_beta: float = 0.05               # SPRT 第二类错误上限
    use_reliability_fusion: bool = False  # 融合内核:按平台可靠性(Brier)加权
    phash_lsh_bands: int = 4              # 检索内核:64bit 指纹分带数(1~8)
    browser_session_reuse: bool = True    # 执行内核:批量执行复用一次浏览器会话
    sched_priority: bool = False          # 调度内核:优先级+预算轮转(替代 FIFO)

    # ---- V8:插件化 / 视觉模型引导与切换 / 多开 Agent ----
    plugin_supervised_api: bool = True    # 插件模式:自动拉起并托管本地 API 服务
    onboarding_auto_open: bool = True     # 无视觉模型时自动弹出引导页(浏览器)
    agents_max_workers: int = 4           # 多开 Agent:最大并发数(1~16)
    agent_task_db: str = "data/agent_tasks.db"  # 多开任务认领账本(防重复处理)

    # ---- V8(本会话追加):视觉模型自动接管与连接向导 ----
    takeover_auto: bool = True            # 首次运行自动探测本地视觉服务并接管(与 plugin_supervised_api 协同)
    local_probe_ports: list = field(default_factory=lambda: ["11434", "1234", "8000", "9997"])
    setup_port: int = 8766                # 连接向导独立端口(纯标准库自托管)
    model_runtime_path: str = "data/model_runtime.json"  # 活动模型运行时配置(热切换)

    # ---- V9:CPU 自适应三档并发 + 收官汇总代理 ----
    concurrency_tier: str = "mid"         # low(cores/4) / mid(cores/2) / high(cores-reserve,极限压榨)
    concurrency_auto: bool = False        # 首次运行自动探测 CPU 并写入建议档位(用户显式配置优先)
    cpu_reserve: int = 1                  # 高档保留核心数(0=全压榨;低/中档自动预留)
    summary_agent_enabled: bool = True    # 批量跑完由结案代理分类汇总并进入举报准备(仍全人工门)

    # ---- V10:举报理由自动生成 + 个人信息模板 ----
    reporter_name: str = ""               # 举报人姓名(必填;优先级:传参>env>profile.yaml>此处)
    reporter_phone: str = ""              # 联系电话(必填;只进表单不进日志,红线 39)
    reporter_email: str = ""              # 电子邮箱(选填;接收处理结果)
    reporter_id: str = ""                 # 身份证号(部分门户必填;实名举报)
    reporter_address: str = ""            # 通讯地址(选填;邮寄书面回执)
    reporter_postcode: str = ""           # 邮政编码(选填)
    reporter_type: str = ""               # 举报人类型(个人/企业/组织)
    reporter_org: str = ""                # 单位名称(类型=企业/组织时必填)
    profile_path: str = ""              # 空=自动定位永久模板(~/.netsentinel/profile.yaml 优先,其次 ./profile.yaml)

    # ---- V11:配置正式收录(附加属性升格为一等字段;默认值精确保持升格前行为) ----
    graph_wire: bool = False                 # A194 图谱通电总开关(默认关 = 现状零通电)
    gang_mode: str = "connectivity"          # A194 团伙判定:connectivity(现状)/ community
    gang_weight_threshold: float = 0.3       # A194 community 模式边权重下限 [0,1](connectivity 忽略)
    gang_template_weight_factor: float = 0.5 # A194 community 模式 shared_template 降权系数 [0,1]
    gang_resolution: float = 1.0             # A194 community 模式 Louvain 分辨率(必须 > 0)
    ensemble_reliability_weights: bool = False  # V10.4→V11 校准驱动集成权重(默认关 = 等权)
    cascade_risk_budget: float | None = None # A197 级联风险预算 [0,1](默认 None = 静态带,冷启动)
    abstain_threshold: float = 0.35          # A196 分歧弃权阈值 [0,1](缺省 0.35 对齐 arbiter)
    bundle_sign_algo: str = "hmac-sha256"    # A192 证据包签名算法:hmac-sha256(默认)/ ed25519
    ed25519_seed_hex: str | None = None      # A192 Ed25519 私钥 seed(64 位十六进制;None = 走 HMAC)
    tsa_url: str | None = None               # A192 RFC3161-lite TSA 地址(None = 全离线本地证明)

    # ---- V12:接线收口(A202 附加属性升格为一等字段;默认值精确保持升格前行为) ----
    trace_enabled: bool = False              # 全链追踪开关(默认关 = 现状零 trace/零 span)
    abstain_enabled: bool = False            # 分歧弃权开关(默认关 = 现状不弃权/不入队提权)

    # ---- V13:接线残余批配置收录(附加属性升格为一等字段;默认值精确保持收录前行为) ----
    dynamic_ttl: bool = False                # A211 动态 TTL 开关(默认关 = 现状固定库级 TTL,零图谱咨询)
    phash_mt_lsh_db: str | None = None       # A204 多表 LSH 持久库路径(None = 按 phash_db 推导 <phash_db>.mtlsh)
    guard_model_path: str = ""               # A217 守卫模型本地目录(空 = 未注入,由构造参数决定)
    guard_family: str = ""                   # A217 守卫模型族(空 = 按 model_path 推断;shieldgemma2/llamaguard/custom-prompt)

    # ---- V14:接线残余批配置收录(A223 附加属性升格;默认值精确保持收录前行为) ----
    bayes_reliability: bool = False          # A223 贝叶斯可靠性权重回流开关(默认关 = 现状等权;开启时优先于 ensemble_reliability_weights)
    bayes_half_life: float | None = None     # A223 贝叶斯遗忘半衰期,单位天(None = 关闭遗忘 = 现状;正值 = 漂移感知,与 reliability.jsonl ts 字段的 epoch 秒量纲对齐,orchestrator 构造 tracker 时换算)
