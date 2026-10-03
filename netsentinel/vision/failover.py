"""跨平台故障转移路由(A68)——GLM 挂了自动切 OpenAI,再挂切本地 Ollama 的可用性保险丝。

链取自 ``cfg.vlm_fallback_chain``(如 ``["glm:glm-5.3-flash", "openai:gpt-4o-mini",
"ollama:llava"]``),``FailoverClassifier.classify`` 按序逐成员尝试,成功即返回并标注
``model="failover→{胜者名}"``、``scores.fallback_from=[先前失败成员]``。

V5 升级(A88 路由容错组)——**断路器**(升级菜单 §1 健壮性,默认开启):

- 每个成员规格一个 :class:`CircuitBreaker`:**连续 3 次**失败(非配置类异常,或返回
  带 ``scores.error`` 的降级分)→ **熔断 30 秒**(时钟可注入,便于测试);熔断期间
  ``classify`` 直接跳过该成员去下一家——跳过视为该成员失败(计入 ``fallback_from``,
  并在胜者 ``scores`` 补 ``note="熔断"``,不覆盖成员自带 note);
- **半开**:冷却期满后该成员的**下一次尝试放行**;成功 → 断路器复位(连续计数清零);
  失败 → 再次熔断 30 秒;
- **不改变既有链尽语义**:配置类错误(``VlmConfigError`` / ``ModelNotFoundError``)
  **不计入**断路器——配置问题不随时间自愈,且必须保住"全链尽→上抛最后一个非配置
  异常 / 全配置错→VlmConfigError 中文汇总"的原语义;仅当全部成员都处于熔断状态被
  跳过时,链尽抛出带"熔断"说明的中文 ``RuntimeError``;
- 可观测性:每次成员转移 ``telemetry.inc("failover.switch")``、每次 ``classify`` 后
  ``telemetry.gauge("failover.circuit_open", 当前熔断成员数)`` 与
  ``telemetry.timer("failover.classify")`` 计时。

预算红线(契约 V4 第 19 条):每次真实外呼由各成员分类器自行经 ``vlm_cache.spend_one``
记账——本模块不绕过、不重复扣;成员自身的评分缓存已兜底,本模块不做二级缓存。

仅使用标准库;兄弟模块(multi_provider / vlm_client)一律惰性导入,未就位时给出中文
RuntimeError,不影响并行开发与离线测试(测试零外呼)。

用法示例(离线替身,零外呼)::

    from netsentinel.contracts import Config, ImageEvidence
    from netsentinel.vision.failover import FailoverClassifier

    fc = FailoverClassifier(Config(vlm_fallback_chain=["glm:glm-5.3-flash",
                                                       "ollama:llava"]))
    score = fc.classify(img)             # glm 失败自动切 ollama
    score.model                          # "failover→ollama:llava"
    score.scores["fallback_from"]        # ["glm:glm-5.3-flash"]
    # 连续 3 次失败后 glm 熔断 30s:期间每张图都直接由 ollama 应答(scores.note="熔断")
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore

try:  # 基座缺失时(并行开发期)静默降级为不注册,模块本身仍可独立使用
    from netsentinel.vision.classifier_base import NsfwClassifier, register_classifier
except ImportError:  # pragma: no cover - 仅并行开发期出现
    NsfwClassifier = object  # type: ignore[assignment,misc]
    register_classifier = None  # type: ignore[assignment]

__all__ = [
    "CIRCUIT_FAILURE_THRESHOLD",
    "CIRCUIT_COOLDOWN_SECONDS",
    "CIRCUIT_SKIP_NOTE",
    "CircuitBreaker",
    "FailoverClassifier",
]

logger = logging.getLogger(__name__)

#: 成员构建器类型:零参可调用,返回一个就绪的分类器实例
MemberBuilder = Callable[[], "NsfwClassifier"]

#: 断路器阈值:同一成员规格**连续**失败达到该次数即熔断(V5 升级菜单 §1)
CIRCUIT_FAILURE_THRESHOLD: int = 3

#: 熔断冷却时长(秒):期满进入半开,放行该成员的下一次尝试
CIRCUIT_COOLDOWN_SECONDS: float = 30.0

#: 熔断跳过成员时在胜者 ``scores["note"]`` 补充的固定中文标注
CIRCUIT_SKIP_NOTE: str = "熔断"


class CircuitBreaker:
    """单成员断路器:连续失败计数 + 可注入冷却时钟,closed / open / half_open 三态。

    状态机(V5 §1,阈值与冷却时长为模块常量,可按实例覆盖)::

        closed ──连续 CIRCUIT_FAILURE_THRESHOLD 次失败──▶ open
        open ──冷却 CIRCUIT_COOLDOWN_SECONDS 秒期满──▶ half_open(放行一次尝试)
        half_open ──成功──▶ closed(计数清零) / ──失败──▶ open(重新计时)

    用法示例::

        breaker = CircuitBreaker(clock=fake_clock)     # 时钟可注入,便于测试
        if breaker.allow():                            # open 期间返回 False
            try:
                result = member.classify(img)
            except Exception:
                breaker.record_failure()               # 达阈值 → 返回 True(本次触发熔断)
            else:
                breaker.record_success()               # 任意成功 → 复位 closed

    :param failure_threshold: 连续失败熔断阈值(缺省 :data:`CIRCUIT_FAILURE_THRESHOLD`)。
    :param cooldown_seconds: 熔断冷却秒数(缺省 :data:`CIRCUIT_COOLDOWN_SECONDS`)。
    :param clock: 时钟注入(零参可调用,返回单调秒);缺省 :func:`time.monotonic`。
    """

    __slots__ = ("_threshold", "_cooldown", "_clock", "_failures", "_opened_at")

    def __init__(
        self,
        *,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: float = CIRCUIT_COOLDOWN_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = max(1, int(failure_threshold))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._failures: int = 0
        self._opened_at: float | None = None  # 最近一次进入 open 的时钟读数

    # -- 状态查询 ---------------------------------------------------------
    @property
    def state(self) -> str:
        """当前状态:``"closed"``(计数中)/ ``"open"``(熔断中)/ ``"half_open"``(探活)。"""
        if self._opened_at is None:
            return "closed"
        return "open" if self._clock() < self._opened_at + self._cooldown else "half_open"

    @property
    def is_open(self) -> bool:
        """是否处于熔断中(open;半开探活不计)。"""
        return self.state == "open"

    @property
    def failures(self) -> int:
        """当前连续失败次数(成功后清零)。"""
        return self._failures

    @property
    def cooldown_seconds(self) -> float:
        """冷却时长(秒,只读)。"""
        return self._cooldown

    def remaining_cooldown(self) -> float:
        """熔断剩余秒数(closed/half_open 返回 0;时钟读数只作计算,不落日志)。"""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._opened_at + self._cooldown - self._clock())

    # -- 状态迁移 ---------------------------------------------------------
    def allow(self) -> bool:
        """本次尝试是否放行:closed / half_open 放行,open 拒绝。"""
        return self.state != "open"

    def record_success(self) -> None:
        """记一次成功:连续失败计数清零,断路器复位 closed。"""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> bool:
        """记一次失败;连续失败达到阈值 → 进入 open(返回 True 表示本次触发熔断)。

        半开探活失败同样经此方法立即再熔断(重新起算冷却窗口)。
        """
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()
            return True
        return False


class _ChainConfigError(RuntimeError):
    """故障转移链的配置类错误(本地兜底形态)。

    A62 ``vlm_client.VlmConfigError`` 就位后,链耗尽的汇总异常会优先使用真实类型;
    本类始终被识别为"配置问题"(配置问题≠失败重试),保证并行开发期语义完整。
    """


#: 配置类异常类型集合(惰性缓存;测试可 monkeypatch 注入自定义类型)
_CONFIG_EXC_TYPES: tuple[type[BaseException], ...] | None = None


def _config_exc_types() -> tuple[type[BaseException], ...]:
    """返回应视为"配置问题"的异常类型集合(惰性缓存)。

    始终包含本地兜底 ``_ChainConfigError``;A62 ``vlm_client`` 就位后并入其
    ``VlmConfigError``(密钥缺失 / vlm_online 未开等)与 ``ModelNotFoundError``
    (模型不存在)——这两类属于"换一家提供方就能解决"的问题,跳过而非重试。
    """
    global _CONFIG_EXC_TYPES
    if _CONFIG_EXC_TYPES is None:
        types: list[type[BaseException]] = [_ChainConfigError]
        try:
            from netsentinel.vision import vlm_client  # 惰性:兄弟模块
        except ImportError:
            logger.debug("vlm_client 未就位:配置类异常仅识别本地兜底类型 _ChainConfigError")
        else:
            for attr in ("VlmConfigError", "ModelNotFoundError"):
                exc_type = getattr(vlm_client, attr, None)
                if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
                    types.append(exc_type)
        _CONFIG_EXC_TYPES = tuple(types)
    return _CONFIG_EXC_TYPES


def _new_config_error(message: str) -> RuntimeError:
    """构造"全部成员配置不可用"的汇总异常(优先 A62 的 VlmConfigError,兜底本地类型)。"""
    try:
        from netsentinel.vision import vlm_client  # 惰性:兄弟模块
    except ImportError:
        return _ChainConfigError(message)
    config_cls = getattr(vlm_client, "VlmConfigError", None)
    if isinstance(config_cls, type) and issubclass(config_cls, BaseException):
        return config_cls(message)
    return _ChainConfigError(message)  # pragma: no cover - 仅 vlm_client 接口异常时出现


class FailoverClassifier(NsfwClassifier):
    """可用性保险丝:沿 ``vlm_fallback_chain`` 逐成员尝试的分类器,注册名 ``"failover"``。

    尝试顺序即链顺序;每个成员的真实外呼与评分缓存由成员自身负责(一本账,红线 19),
    本模块只做路由与失败标注:

    - 成功(未抛异常、``nsfw_prob >= 0`` 且 ``scores`` 无 ``error``)→ 立即返回
      ``ImageScore(model="failover→{成员.name}", scores={"fallback_from": [...], **原 scores})``,
      其中 ``fallback_from`` 为空列表(首位即成功)或先前失败成员名列表;
    - 配置类异常(``VlmConfigError`` / ``ModelNotFoundError``,如密钥缺失、vlm_online
      未开、模型不存在)→ 记入 ``fallback_from`` 跳过该成员(配置问题≠失败重试,
      **不计入断路器**);
    - 其他异常 / 成员返回带 ``scores.error`` 的降级分 → 同样跳过并记 debug 日志
      (**计入断路器**:连续 :data:`CIRCUIT_FAILURE_THRESHOLD` 次后熔断
      :data:`CIRCUIT_COOLDOWN_SECONDS` 秒,熔断期间直接跳过该成员——跳过同样计入
      ``fallback_from`` 并在胜者 ``scores`` 补 ``note=``:data:`CIRCUIT_SKIP_NOTE`);
    - 全链尽:有非配置异常则上抛最后一个非配置异常;全为配置错则抛 ``VlmConfigError``
      语义的中文汇总(缺什么、查什么);全部成员熔断跳过则抛带"熔断"说明的中文
      ``RuntimeError``;成员构建失败(如 multi_provider 未就位)按非配置异常同样处理。

    ``classify_batch`` 继承基类默认实现(逐图走 ``classify``):成员健康状态可能随时间
    变化,因此**允许同一批内不同图片落到不同成员**——每张图独立走完整条链,顺序与输入一致。

    :param circuit_breaker: V5 断路器开关(默认开启;``False`` 时完全恢复 A68 旧行为)。
    :param clock: 断路器时钟注入(零参可调用,返回单调秒;缺省 :func:`time.monotonic`),
        仅供测试推进冷却窗口使用。
    """

    name = "failover"

    def __init__(
        self,
        cfg: Config | None = None,
        *,
        builders: dict[str, MemberBuilder] | None = None,
        specs: list[str] | None = None,
        circuit_breaker: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # cfg 位置参数兼容 classifier_base.get_classifier 的 cls(cfg) 工厂调用
        self._cfg = cfg if cfg is not None else Config()
        chain = (
            list(specs)
            if specs is not None
            else list(getattr(self._cfg, "vlm_fallback_chain", None) or [])
        )
        self._specs = self._normalize_chain(chain)
        self._members: dict[str, NsfwClassifier] = {}
        self._circuit_enabled = bool(circuit_breaker)
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        # V5:每成员规格一个断路器(连续失败熔断 → 半开探活,时钟可注入)
        self._breakers: dict[str, CircuitBreaker] = {
            spec: CircuitBreaker(clock=self._clock) for spec in self._specs
        }
        if builders is None:
            # 缺省惰性构建器:spec -> multi_provider.build_classifier(spec, cfg)
            self._builders: dict[str, MemberBuilder] = {
                spec: self._make_default_builder(spec) for spec in self._specs
            }
        else:
            self._builders = dict(builders)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    @property
    def specs(self) -> list[str]:
        """故障转移链(只读副本,按尝试顺序)。"""
        return list(self._specs)

    def classify(self, img: ImageEvidence) -> ImageScore:
        """沿链逐成员尝试:成功即返回,失败逐级转移(完整语义见类 docstring)。

        真实外呼预算(spend_one)与评分缓存均由各成员自理,本方法不做二级缓存。
        V5:整段计时 ``telemetry.timer("failover.classify")``,结束时分成员维护
        ``gauge("failover.circuit_open", 熔断成员数)``。
        """
        with telemetry.timer("failover.classify"):
            try:
                return self._classify(img)
            finally:
                self._gauge_circuits()

    def _classify(self, img: ImageEvidence) -> ImageScore:
        """classify 主链路(公开方法负责遥测计时与熔断仪表,见 :meth:`classify`)。"""
        config_types = _config_exc_types()
        fallback_from: list[str] = []
        config_failures: list[str] = []
        circuit_skipped: list[str] = []
        last_config_exc: BaseException | None = None
        last_non_config: BaseException | None = None
        last_inband: str | None = None

        for spec in self._specs:
            # ---- V5 断路器:熔断中直接跳过该成员(视为失败,直接下一家) ----
            breaker = self._breakers.get(spec)
            if self._circuit_enabled and breaker is not None and not breaker.allow():
                label = self._label(spec, self._members.get(spec))
                fallback_from.append(label)
                circuit_skipped.append(label)
                telemetry.inc("failover.switch")
                logger.info(
                    "故障转移:成员 '%s' 处于熔断状态(此前连续失败 %d 次,剩约 %.0f 秒),"
                    "本次跳过直接下一成员",
                    label,
                    breaker.failures,
                    breaker.remaining_cooldown(),
                )
                continue

            member: NsfwClassifier | None = None
            try:
                member = self._get_member(spec)
                score = member.classify(img)
            except Exception as exc:  # noqa: BLE001 - 单成员失败必须转移到下一成员
                label = self._label(spec, member)
                fallback_from.append(label)
                telemetry.inc("failover.switch")
                if isinstance(exc, config_types):
                    config_failures.append(f"{label}:{exc}")
                    last_config_exc = last_config_exc or exc
                    logger.info(
                        "故障转移:成员 '%s' 配置不可用(不重试、不计熔断),跳到下一成员:%s",
                        label,
                        exc,
                    )
                else:
                    last_non_config = exc
                    self._record_circuit_failure(spec, breaker, label)
                    logger.debug(
                        "故障转移:成员 '%s' 识别异常,跳到下一成员:%s", label, exc
                    )
                continue

            # 成功判定:有效 nsfw_prob 且无带内错误(scores.error 是 A63/GLM 的降级语义)
            valid = isinstance(score, ImageScore) and isinstance(
                score.nsfw_prob, (int, float)
            ) and not isinstance(score.nsfw_prob, bool) and score.nsfw_prob >= 0
            scores = score.scores if (valid and isinstance(score.scores, dict)) else {}
            error_text = str(scores.get("error") or "")
            if valid and not error_text:
                if breaker is not None:
                    breaker.record_success()  # V5:成功复位断路器(连续计数清零)
                merged = {"fallback_from": list(fallback_from), **scores}
                if circuit_skipped:
                    # V5:有成员因熔断被跳过——标注 note,不覆盖成员自带 note
                    merged.setdefault("note", CIRCUIT_SKIP_NOTE)
                winner = self._label(spec, member)
                logger.debug(
                    "故障转移:成员 '%s' 评分成功(此前跳过 %d 个),胜者=%s",
                    spec,
                    len(fallback_from),
                    winner,
                )
                return ImageScore(
                    image=score.image if getattr(score, "image", None) is not None else img,
                    model=f"failover→{winner}",
                    nsfw_prob=float(score.nsfw_prob),
                    scores=merged,
                )
            # 带内失败:成员未抛异常但返回了降级分(计入断路器连续失败)
            reason = error_text or "成员返回了无效评分"
            label = self._label(spec, member)
            last_inband = f"{label}:{reason}"
            fallback_from.append(label)
            telemetry.inc("failover.switch")
            self._record_circuit_failure(spec, breaker, label)
            logger.debug(
                "故障转移:成员 '%s' 返回错误评分(%s),跳到下一成员", spec, reason
            )

        # ---- 全链尽(既有语义:非配置异常上抛 > 全配置错 VlmConfigError 汇总)----
        if last_non_config is not None:
            logger.error(
                "故障转移链 %d 个成员全部失败,上抛最后一个非配置异常:%s",
                len(self._specs),
                last_non_config,
            )
            raise last_non_config
        if config_failures:
            summary = (
                "故障转移链全部成员配置不可用("
                + ";".join(config_failures)
                + ")。请检查 vlm_online 开关、vlm_api_keys 各提供方密钥与 "
                "vlm_fallback_chain 链配置(密钥值请勿写入日志)。"
            )
            logger.error(summary)
            raise _new_config_error(summary) from last_config_exc
        if last_inband is not None:
            suffix = (
                f"(另有 {len(circuit_skipped)} 个成员熔断跳过)" if circuit_skipped else ""
            )
            raise RuntimeError(
                f"故障转移链全部成员识别失败(最后失败:{last_inband}){suffix}"
            )
        # 仅剩可能:全部成员因熔断被跳过(V5 新增的链尽形态,同样必须上抛)
        raise RuntimeError(
            "故障转移链 "
            f"{len(self._specs)} 个成员全部处于熔断状态被跳过({'、'.join(circuit_skipped)}),"
            "请等待熔断期满半开重试,或检查这些成员的可用性"
        )

    def classify_batch(self, imgs: list[ImageEvidence]) -> list[ImageScore]:
        """批量打分:继承基类默认的逐张 ``classify`` 语义。

        每张图独立走完整条故障转移链,因此**同一批内不同图片允许落到不同成员**
        (前一张图击穿到备用成员,不代表后一张图也绕开主成员);返回顺序与输入一致。
        """
        return super().classify_batch(imgs)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_chain(chain: list[str]) -> list[str]:
        """校验并按序去重故障转移链;空链抛 ``ValueError``(中文)。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for spec in chain:
            if not isinstance(spec, str) or not spec.strip():
                raise ValueError(
                    f"故障转移链含无效成员 spec:{spec!r}(需形如 'glm:glm-5.3-flash')"
                )
            spec = spec.strip()
            if spec in seen:
                logger.debug("故障转移链成员 '%s' 重复,已去重", spec)
                continue
            seen.add(spec)
            normalized.append(spec)
        if not normalized:
            raise ValueError(
                "故障转移链为空:请在配置 vlm_fallback_chain 填 "
                "['glm:glm-5.3-flash', ...]"
            )
        return normalized

    def _make_default_builder(self, spec: str) -> MemberBuilder:
        """为 spec 生成缺省惰性构建器:A63 ``multi_provider.build_classifier``。

        multi_provider 未就位时抛中文 ``RuntimeError``(按非配置异常参与链转移)。
        """
        cfg = self._cfg

        def _build() -> NsfwClassifier:
            try:
                from netsentinel.vision import multi_provider  # 惰性:兄弟模块
            except ImportError as exc:
                raise RuntimeError(
                    f"统一分类器模块 multi_provider 未就位,无法构建故障转移链成员 '{spec}'"
                ) from exc
            build = getattr(multi_provider, "build_classifier", None)
            if not callable(build):  # pragma: no cover - 仅接口异常时出现
                raise RuntimeError(
                    f"multi_provider 缺少 build_classifier 接口,无法构建成员 '{spec}'"
                )
            return build(spec, cfg)

        return _build

    def _get_member(self, spec: str) -> NsfwClassifier:
        """取(或惰性构建并缓存实例)spec 对应的成员分类器;构建失败抛异常交由链处理。"""
        member = self._members.get(spec)
        if member is not None:
            return member
        builder = self._builders.get(spec)
        if builder is None:
            raise RuntimeError(
                f"故障转移链成员 '{spec}' 缺少构建器:builders 注入表未覆盖该项"
            )
        built = builder()
        if not isinstance(built, NsfwClassifier):
            raise RuntimeError(
                f"故障转移链成员 '{spec}' 的构建器返回了无效对象:"
                f"{built!r}(需为 NsfwClassifier 实例)"
            )
        self._members[spec] = built
        return built

    @staticmethod
    def _label(spec: str, member: NsfwClassifier | None) -> str:
        """失败/胜者标注用的成员名:已构建用成员自身 ``name``,构建阶段失败用 spec。"""
        name = str(getattr(member, "name", "") or "") if member is not None else ""
        return name or spec

    # -- V5 断路器内部工具 -------------------------------------------------
    def _record_circuit_failure(
        self, spec: str, breaker: CircuitBreaker | None, label: str
    ) -> None:
        """记一次非配置失败;达到连续阈值 → 熔断并告警(只记成员名与次数,不落敏感内容)。"""
        if not self._circuit_enabled or breaker is None:
            return
        if breaker.record_failure():
            logger.warning(
                "故障转移:成员 '%s' 连续失败 %d 次,熔断 %.0f 秒(期间各图直接跳到下一成员)",
                label,
                breaker.failures,
                breaker.cooldown_seconds,
            )

    def _gauge_circuits(self) -> None:
        """把当前熔断中的成员数写入 ``gauge("failover.circuit_open")``(断路器关闭时不发)。"""
        if not self._circuit_enabled:
            return
        open_count = sum(1 for b in self._breakers.values() if b.is_open)
        telemetry.gauge("failover.circuit_open", open_count)


if register_classifier is not None:  # 正常情况:导入即注册 "failover"
    register_classifier("failover", FailoverClassifier)
