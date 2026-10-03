"""活动模型管理器(A144):model_runtime.json 的读写 / 校验 / 热切换。

职责(契约 V8 §2-§3):

- 维护"当前活动模型"持久化状态 ``{"spec", "switched_at", "switched_by"}``;
- ``set_active`` 切换前先经 ``providers.parse_spec`` 做语法校验(惰性导入,
  兄弟模块缺席 → 中文 RuntimeError),再按需做连通性测试(注入 ``tester`` 或
  惰性 A147 ``connectivity.test_connection`` 缺省),**测试不过即拒绝切换**
  (中文 ValueError 含原因;``--force`` 语义 = ``validate=False`` 放行);
- ``apply(cfg)`` 把活动 spec 写进 ``cfg.classifier``;仅当 spec 为
  ``"stub"`` 时同时把 ``cfg.ensemble_members`` 置为 ``["stub"]``(离线桩
  必须整套替换,红线 34);其余 spec 一律不动集成成员;
- 写入原子(临时文件 + ``os.replace``)+ 实例级线程安全锁;损坏的 JSON
  自动重建为空状态(视为"未连接"),绝不抛出到调用方。

tester 注入协议:``callable(spec, cfg) -> {"ok": bool, "error"?: str}``
(与 A147 ``test_connection`` 同构);返回非 dict 或 ``ok`` 为假均视为失败。
离线桩 ``"stub"`` 无外部服务可测,连通性校验一律跳过(兜底永远可用)。
顶层绝不 import 兄弟模块(防循环导入),全部惰性;本模块零外呼。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from typing import Any

from netsentinel.contracts import Config, now_iso

logger = logging.getLogger(__name__)

#: 离线桩规格名:非提供方目录条目,跳过 parse_spec 与连通性测试
STUB_SPEC = "stub"

#: ``switched_by`` 缺省来源(契约登记的来源:takeover / wizard / cli / rest / manual)
DEFAULT_SWITCHED_BY = "manual"

#: tester 协议类型:``(spec, cfg) -> {"ok", "error"?}``
Tester = Callable[[str, Any], "dict[str, Any]"]

__all__ = ["DEFAULT_SWITCHED_BY", "STUB_SPEC", "ModelManager", "Tester"]


# ---------------------------------------------------------------------------
# 兄弟模块惰性接缝(测试可 monkeypatch;缺席一律降级,绝不硬依赖)
# ---------------------------------------------------------------------------


def _load_parse_spec() -> Callable[[str], tuple[str, str | None]] | None:
    """惰性导入 ``providers.parse_spec``(A61);未就位返回 ``None``。

    顶层不 import 兄弟模块;并行开发期 A61 缺席或暂不可导入(语法错误未修完
    等)都降级为"未就位",由调用方决定抛中文 RuntimeError。
    """
    try:
        from netsentinel.vision import providers
    except Exception as exc:  # noqa: BLE001 - ImportError / 并行期 SyntaxError 等一律降级
        logger.debug("providers 暂不可用,模型规格语法校验降级:%s", exc)
        return None
    fn = getattr(providers, "parse_spec", None)
    return fn if callable(fn) else None


def _load_connectivity_tester() -> Tester | None:
    """惰性导入 A147 ``connectivity.test_connection`` 作为缺省 tester;缺席返回 ``None``。"""
    try:
        from netsentinel.vision import connectivity
    except Exception as exc:  # noqa: BLE001 - 兄弟缺席/暂坏一律降级为"跳过连通性"
        logger.debug("connectivity(A147)暂不可用,连通性校验降级:%s", exc)
        return None
    fn = getattr(connectivity, "test_connection", None)
    return fn if callable(fn) else None


def _load_default_cfg() -> Config | None:
    """惰性加载缺省 Config(供 A147 缺省 tester 使用);任何异常降级为 ``None``。"""
    try:
        from netsentinel.config import load_config
    except Exception as exc:  # noqa: BLE001
        logger.debug("config.load_config 暂不可用,连通性测试将以 cfg=None 进行:%s", exc)
        return None
    try:
        return load_config(None)
    except Exception as exc:  # noqa: BLE001 - 配置文件损坏等不阻断切换校验
        logger.debug("缺省配置加载失败,连通性测试将以 cfg=None 进行:%s", exc)
        return None


def _tester_reason(result: Any) -> str:
    """从 tester 返回值里取中文失败原因;取不到给出兜底文案。"""
    if isinstance(result, dict):
        raw = result.get("error")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return f"未知原因(测试器返回 {json.dumps(result, ensure_ascii=False, default=str)[:200]})"
    return f"未知原因(测试器返回了无法识别的结果:{str(result)[:200]})"


# ---------------------------------------------------------------------------
# ModelManager
# ---------------------------------------------------------------------------


class ModelManager:
    """活动模型管理器:单一 JSON 状态文件的线程安全读写器。

    参数:

    - ``path``:状态文件路径(如 ``cfg.model_runtime_path``),父目录不存在
      会自动创建;写入原子(同目录临时文件 + ``os.replace``);
    - ``cfg``:可选 Config,供缺省连通性测试使用;缺省惰性加载
      ``load_config()``(只读,失败降级为 ``None``)。
    """

    def __init__(self, path: str, *, cfg: Config | None = None) -> None:
        self._path = str(path)
        self._cfg = cfg
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 底层

    @property
    def path(self) -> str:
        """状态文件绝对路径。"""
        return os.path.abspath(self._path)

    def _write(self, record: dict[str, Any]) -> None:
        """原子写:同目录临时文件 + fsync + ``os.replace``(调用方须持锁)。"""
        target = os.path.abspath(self._path)
        directory = os.path.dirname(target)
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(target) + ".", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _read(self) -> dict[str, Any]:
        """读取状态;文件缺失 → ``{}``;损坏(非法 JSON / 非 dict)→ 重建为空。"""
        with self._lock:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    raw = fh.read()
            except FileNotFoundError:
                return {}
            except OSError as exc:
                logger.warning(
                    "活动模型状态文件不可读,按空状态处理:%s(%s)", self._path, exc
                )
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                logger.warning("活动模型状态文件损坏,已重建为空(未连接):%s", self._path)
                try:
                    self._write({})
                except OSError as exc:
                    logger.warning(
                        "活动模型状态文件重建失败,本次按空状态处理:%s(%s)",
                        self._path,
                        exc,
                    )
                return {}
            return data

    # ------------------------------------------------------------------ 查询

    def get_active(self) -> str | None:
        """当前活动模型 spec(如 ``"ollama:llava"`` / ``"stub"``);未连接返回 ``None``。"""
        spec = self._read().get("spec")
        if isinstance(spec, str) and spec.strip():
            return spec.strip()
        return None

    def status(self) -> dict[str, Any]:
        """状态快照:未连接 → ``{"spec": None}``;已连接 → 含来源与时间。"""
        data = self._read()
        spec = data.get("spec")
        if not (isinstance(spec, str) and spec.strip()):
            return {"spec": None}
        switched_by = str(data.get("switched_by") or "").strip() or DEFAULT_SWITCHED_BY
        switched_at = str(data.get("switched_at") or "").strip()
        return {
            "spec": spec.strip(),
            "switched_by": switched_by,
            "switched_at": switched_at,
        }

    # ------------------------------------------------------------------ 切换

    def set_active(
        self,
        spec: str,
        *,
        switched_by: str = DEFAULT_SWITCHED_BY,
        validate: bool = True,
        tester: Tester | None = None,
    ) -> dict[str, Any]:
        """设置活动模型并原子落盘,返回新状态(与 :meth:`status` 同构)。

        流程:①空 spec → 中文 ValueError;②``"stub"`` 跳过语法与连通性校验
        (离线桩永远可用,红线 34);③其余 spec 经 ``providers.parse_spec``
        语法校验(未知提供方 → 中文 ValueError;A61 缺席 → 中文
        RuntimeError);④``validate=True`` 时做连通性测试(显式 ``tester``
        优先,缺省惰性 A147;**不过即抛中文 ValueError,含原因**,文件
        保持原状);⑤原子写入 ``{"spec","switched_at","switched_by"}``。

        ``--force`` 语义 = ``validate=False``:跳过连通性测试直接落盘
        (语法校验仍执行,防手误写坏状态文件)。
        """
        text = str(spec if spec is not None else "").strip()
        if not text:
            raise ValueError(
                "模型规格不能为空:请传入 '提供方:模型'(如 ollama:llava、glm:glm-4.5v)"
                f"或 '{STUB_SPEC}'(离线桩,非模型判定)。"
            )
        if text != STUB_SPEC:
            parse_spec = _load_parse_spec()
            if parse_spec is None:
                raise RuntimeError(
                    "统一提供方目录模块 netsentinel.vision.providers(A61)尚未就位,"
                    f"无法校验模型规格 '{text}';请检查安装或稍后重试。"
                )
            try:
                parse_spec(text)  # 未知提供方 → ValueError(中文)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"模型规格校验异常:{text}({exc})") from exc
            if validate:
                self._run_test(text, tester)
        with self._lock:
            record = {
                "spec": text,
                "switched_at": now_iso(),
                "switched_by": (
                    str(switched_by) if switched_by is not None else ""
                ).strip()
                or DEFAULT_SWITCHED_BY,
            }
            self._write(record)
        logger.info("活动模型已切换:%s(来源:%s)", text, record["switched_by"])
        return self.status()

    def _run_test(self, spec: str, tester: Tester | None) -> None:
        """执行连通性测试;不过 / 测试器异常 → 中文 ValueError(含原因)。"""
        fn = tester if callable(tester) else _load_connectivity_tester()
        if fn is None:
            logger.debug(
                "未注入 tester 且 connectivity(A147)暂不可用,仅做语法校验:%s", spec
            )
            return
        cfg = self._cfg if self._cfg is not None else _load_default_cfg()
        try:
            result = fn(spec, cfg)
        except Exception as exc:
            raise ValueError(f"模型连通性测试未通过:{spec}。原因:{exc}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise ValueError(
                f"模型连通性测试未通过:{spec}。原因:{_tester_reason(result)}"
            )

    # ------------------------------------------------------------------ 清除 / 应用

    def clear(self) -> None:
        """清除活动模型(回到未连接状态);文件重建为空对象。"""
        with self._lock:
            self._write({})
        logger.info("活动模型已清除(回到未连接状态)")

    def apply(self, cfg: Config) -> Config:
        """把活动 spec 应用到 Config(原地修改并返回同一对象)。

        - 无活动模型:不做任何修改(由向导 / takeover 负责兜底);
        - spec == ``"stub"``:``classifier="stub"`` 且 ``ensemble_members=["stub"]``
          (离线桩必须整套替换,红线 34);
        - 其余 spec:仅 ``cfg.classifier = spec``,**不动 ensemble_members**。
        """
        spec = self.get_active()
        if not spec:
            return cfg
        cfg.classifier = spec
        if spec == STUB_SPEC:
            cfg.ensemble_members = [STUB_SPEC]
        return cfg
