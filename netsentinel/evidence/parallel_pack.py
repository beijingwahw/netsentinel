"""NetSentinel(净网哨兵)并行证据包构建(evidence.parallel_pack,A171)。

把一批 :class:`SiteReport` 分发给线程池逐站 ``builder(report, cfg)``
(缺省惰性 :func:`netsentinel.evidence.packager.build_bundle`),
**保序**返回 ``list[EvidenceBundle | None]``;单包失败逐包容错为
``None`` 占位 + 中文 warning + ``parallel_pack.failures`` 计数,绝不
影响其余站点、不中断整批(与 A166 并行分类同款容错口径)。

执行器三级解析(契约 §2 A171 行):

1. 调用方注入 ``executor``(用完**不关闭**,归属调用方);
2. 缺省惰性复用 A164 ``ops.concurrency.thread_pool(cfg, label="pack")``
   (兄弟模块并行开发期可能缺席,缺席静默跳过);
3. 兜底自建 ``ThreadPoolExecutor(max_workers=io_workers)``,档位公式与
   契约 §1 / A163 / A164 逐条一致(low=cores//4 / mid=cores//2 /
   high=cores-reserve)。

**并发安全口径(同 host 串行化)**:A11 ``packager.build_bundle`` 的
证据目录名 = ``<safe_host>_<秒级时间戳>``(同秒冲突时 ``exists()``
探测 + 加序号避让)。该探测—创建序列**不是原子的**:多线程对**同一
host** 同秒并发构建时,两个线程可能同时探测到目录不存在,后者
``mkdir(exist_ok=False)`` 抛 ``FileExistsError`` 导致整包失败。本模块
不改冻结的 A11,改为**host→锁字典**(:data:`_HOST_LOCKS`)串行化**同
safe_host** 的整段构建:不同 host(常规输入:一批不同站点)仍真并发
压榨下载/哈希 IO;同 host 多报告只损失理论上的并发度,换取目录命名
零竞争。

安全红线:

35. 压榨边界——本模块只加速**本地 IO**(证据文件复制 / sha256 / zip);
    对外抓取的礼貌间隔(``fetch_delay_s``)、引擎限速、举报频控一概
    不在本模块触碰。
37. 资源治理——缺省线程池 workers 经 §1 公式天然 ≤ 物理核数;单批
    总量上限 :data:`MAX_BATCH`、单包取结果超时 :data:`RESULT_TIMEOUT_S`
    (超时置 None 占位并尽力 cancel);缺省线程池随 ``with`` 关闭,
    不留孤儿线程;注入执行器不受限,归属调用方。

遥测:``parallel_pack.all`` 计时整批;``parallel_pack.failures`` 按失败
包数逐包累加;``parallel_pack.bundles`` 按成功包数一次性累计。

A214 并行产物签名接线(默认关闭,与 A205 单站包同一策略同一实现):

- 缺省 builder(= A11 ``packager.build_bundle``)的产物已在 A205 内于
  **zip 落盘前**签名,本模块不重复签、不扰动既有签名;
- 注入 builder 产出的**未签名**产物在收集后按 ``cfg`` 同一策略补签
  (只读复用 ``packager.sign_bundle``/``zip_bundle`` 公开口,绝不复制
  签名逻辑:三键 getattr 防御式读取、默认 hmac-sha256 配置 = 现状零签名
  行为、ed25519 显式 opt-in、失败回滚不中断);补签后刷新 zip,保证
  目录与 zip 口径一致;补签只作用于"路径在盘 + zip 布局符合 packager
  约定 + manifest 尚无签名块"的产物,测试替身/外部布局不干预;
- 签名任何失败只中文告警 + ``parallel_pack.sign_skipped`` 计数,该包
  结果**原样保留**——绝不因签名把成功包打成 None 占位。

测试约定:``tests/test_parallel_pack.py`` 全离线 fake builder(核数
monkeypatch 固定;A164 双态——在场走委托 / 强制缺席走内置兜底),
另含一条真 ``build_bundle`` 集成用例(依赖缺席 importorskip)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from netsentinel import telemetry
from netsentinel.contracts import Config, EvidenceBundle, SiteReport

__all__ = ["pack_all", "MAX_BATCH", "RESULT_TIMEOUT_S"]

logger = logging.getLogger(__name__)

#: 单批站点报告总量上限(红线 37「所有并行任务有总量与超时上限」);
#: 超出抛中文 ValueError,调用方应自行分片。
MAX_BATCH = 512

#: 单包取结果超时(秒);超时按该包失败容错(None 占位 + 失败计数),
#: 并尽力 cancel 未起跑的任务,不拖垮整批。
RESULT_TIMEOUT_S = 600.0

#: 核数探测兜底值(契约 §1:``os.cpu_count()`` 返回 None 时按 2)
_FALLBACK_CORES = 2

#: 目录名/锁键中不允许出现的主机名字符(与 A11 packager._safe_host 同款,
#: 保证锁键与真实目录前缀逐字符一致)
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9.-]")

#: host → 互斥锁注册表:同 safe_host 的构建整段串行化(防 A11 目录名
#: "探测—创建" 竞争);注册表自身由守护锁保护。
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# host 键与锁注册表(键与 A11 目录前缀逐字符一致)
# ---------------------------------------------------------------------------
def _safe_host(site_url: str) -> str:
    """提取站点主机名并替换非法字符(镜像 A11 ``packager._safe_host``,
    不依赖其私有符号);取不到主机名时返回 ``unknown``。"""
    host = urllib.parse.urlparse(site_url).hostname or ""
    cleaned = _UNSAFE_CHARS.sub("_", host).rstrip(".")
    return cleaned or "unknown"


def _host_lock(site_url: str) -> threading.Lock:
    """取该站点 host 的互斥锁(同 host 同锁,不同 host 各自独立)。"""
    key = _safe_host(site_url)
    with _HOST_LOCKS_GUARD:
        lock = _HOST_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _HOST_LOCKS[key] = lock
        return lock


# ---------------------------------------------------------------------------
# 档位公式与 workers 解析(惰性优先 A164,缺席内置兜底——同 §1)
# ---------------------------------------------------------------------------
def _ops_concurrency():
    """惰性取 A164 ``netsentinel.ops.concurrency``;缺席/不可用返回 None。

    并行开发期兄弟模块可能未就位或部分写入(非 ImportError 一并按缺席
    处理,只记 debug 日志),此时走本模块内置兜底,结论与契约一致。
    """
    try:
        from netsentinel.ops import concurrency as _mod  # 惰性:A164 并行开发
    except Exception as exc:  # noqa: BLE001 - 并行开发期任何导入问题都按缺席
        logger.debug("ops.concurrency 不可用,按内置公式兜底:%s", exc)
        return None
    return _mod if hasattr(_mod, "thread_pool") else None


def _tier_of(cfg: Config) -> str:
    """读 ``cfg.concurrency_tier``;读不到按缺省 "mid"。"""
    return str(getattr(cfg, "concurrency_tier", "mid") or "mid")


def _reserve_of(cfg: Config) -> int:
    """读 ``cfg.cpu_reserve``(高档保留核数);读不到/非法按缺省 1。"""
    try:
        return max(0, int(getattr(cfg, "cpu_reserve", 1)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _fallback_tier_workers(
    tier: str, *, reserve: int = 1, cores: int | None = None
) -> int:
    """契约 §1 内置兜底公式(与 A163 ``tier_workers`` / A164 同款):

    - low:max(1, N//4) —— 后台/省电;
    - mid:max(1, N//2) —— 默认;
    - high:max(1, N - reserve) —— 极限压榨(reserve=0 即全核);
    - 非法档位:中文 ValueError(不依赖兄弟模块报错文案)。
    """
    n = int(cores) if cores and int(cores) > 0 else (os.cpu_count() or _FALLBACK_CORES)
    try:
        r = max(0, int(reserve))
    except (TypeError, ValueError):
        r = 1
    tier = str(tier).strip().lower()
    if tier == "low":
        return max(1, n // 4)
    if tier == "mid":
        return max(1, n // 2)
    if tier == "high":
        return max(1, n - r)
    raise ValueError(f"未知并发档位:'{tier}'(合法值:low / mid / high)")


def _io_workers(cfg: Config) -> int:
    """线程池 workers:优先 A164 ``io_workers(cfg)`` 委托,缺席走内置公式。"""
    mod = _ops_concurrency()
    if mod is not None and hasattr(mod, "io_workers"):
        try:
            return max(1, int(mod.io_workers(cfg)))
        except Exception as exc:  # noqa: BLE001 - 兄弟异常仅降级到内置公式
            logger.warning("ops.concurrency.io_workers 调用失败,按内置公式兜底:%s", exc)
    return _fallback_tier_workers(_tier_of(cfg), reserve=_reserve_of(cfg))


# ---------------------------------------------------------------------------
# builder 解析与单包构建
# ---------------------------------------------------------------------------
def _default_builder() -> Callable[[SiteReport, Config], EvidenceBundle]:
    """惰性解析缺省 builder:A11 ``packager.build_bundle``。

    导入发生在调用时(每次 pack_all 重新解析,便于测试注入替身);
    A11 缺席(并行开发极端态)转中文 RuntimeError(原始异常保留为
    ``__cause__``,不静默降级——没有 builder 就没有证据包)。
    """
    try:
        from netsentinel.evidence.packager import build_bundle  # 惰性:A11 冻结模块
    except Exception as exc:  # noqa: BLE001 - 任何导入问题统一转中文 RuntimeError
        raise RuntimeError(
            "缺省证据包构建器 netsentinel.evidence.packager.build_bundle 不可用:"
            f"请注入 builder= 或检查 A11 模块安装。原始错误:{exc}"
        ) from exc
    return build_bundle


def _pack_one(
    build: Callable[[SiteReport, Config], EvidenceBundle],
    report: SiteReport,
    cfg: Config,
) -> EvidenceBundle:
    """构建单个证据包;**同 safe_host 整段持锁**(目录名"探测—创建"
    竞争防御),不同 host 互不阻塞。"""
    with _host_lock(report.site_url):
        return build(report, cfg)


def _collect_ordered(reports: list[SiteReport], futures: list) -> list:
    """按提交顺序收集 future 结果(保序);单包异常/超时 → None 占位 +
    中文 warning + ``parallel_pack.failures`` 计数。"""
    results: list = []
    for report, fut in zip(reports, futures):
        try:
            results.append(fut.result(timeout=RESULT_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - 单包失败只容错该包
            fut.cancel()  # 超时场景尽力清掉未起跑的排队任务(红线 37)
            logger.warning(
                "证据包构建失败,已置 None 占位:%s(%s: %s)",
                report.site_url,
                type(exc).__name__,
                exc,
            )
            telemetry.inc("parallel_pack.failures")
            results.append(None)
    return results


# ---------------------------------------------------------------------------
# A214 并行产物签名(复用 A205 packager.sign_bundle 公开口;默认零签名)
# ---------------------------------------------------------------------------
def _wants_ed25519(cfg: Config) -> bool:
    """防御式读取 ``bundle_sign_algo``:显式 ``ed25519``(opt-in)才补签。

    与 A205 ``packager._make_signer`` 同一口径:默认 ``hmac-sha256`` /
    未配置 → False,本模块完全不构造签名器、零新增读写(现状行为)。
    """
    raw = getattr(cfg, "bundle_sign_algo", None)
    return raw is not None and str(raw).strip().lower() == "ed25519"


def _unsigned_product_dir(bundle: EvidenceBundle) -> tuple[Path, Path] | None:
    """返回需补签产物的 ``(bundle_dir, manifest_path)``;不满足前提返回 None。

    补签前提(全部满足才动笔,任一不满足静默跳过、绝不报错):

    - ``dir_path`` / ``manifest_path`` / ``zip_path`` 三路径非空且都在盘上
      (测试替身 / 外部构建器的虚构路径不干预);
    - zip 布局符合 packager 约定(与 bundle 目录同父、同名 + ``.zip``)——
      只有能**安全刷新 zip** 时才允许改 manifest,避免目录与 zip 口径分裂;
    - manifest 可解析且**尚无** ``signature`` 签名块(缺省 build_bundle
      产物已在 A205 内于 zip 前签名,不重复签、不扰动既有签名)。
    """
    dir_path = str(getattr(bundle, "dir_path", "") or "")
    manifest_path = str(getattr(bundle, "manifest_path", "") or "")
    zip_path = str(getattr(bundle, "zip_path", "") or "")
    if not (dir_path and manifest_path and zip_path):
        return None
    bdir, mpath, zpath = Path(dir_path), Path(manifest_path), Path(zip_path)
    if not (bdir.is_dir() and mpath.is_file() and zpath.is_file()):
        return None
    if zpath.parent != bdir.parent or zpath.name != f"{bdir.name}.zip":
        return None
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(manifest, dict) and "signature" in manifest:
        return None
    return bdir, mpath


def _sign_unsigned_products(results: list, cfg: Config) -> None:
    """按 ``cfg`` 同一签名策略给**未签名**的并行产物补签并刷新 zip。

    - 签名实现**只读复用** A205 ``netsentinel.evidence.packager.sign_bundle``
      与 ``packager.zip_bundle``(A224 起的公开口;模块级函数惰性 import,
      绝不复制签名逻辑):三键 getattr 防御式读取、默认 hmac-sha256 =
      现状零签名、ed25519 opt-in、签名失败回滚不中断——全部语义由该
      单一实现承载;
    - 补签落块(manifest 字节已变)后经 ``packager.zip_bundle`` 刷新 zip,
      目录与 zip 口径一致(zip 内即已签名 manifest);未落块(失败已
      回滚)则 zip 原样;
    - 写口缺席(极端并行态:packager 部分写入)→ 一条中文告警 + 按包
      计数 ``parallel_pack.sign_skipped``,整批照常(增强项缺席绝不中断,
      降级路径与 A214 原口径一致,仅符号名换公开口);
    - 单包补签任何异常只中文告警 + 计数,该包结果**原样保留**——
      绝不因签名把成功包打成 None 占位(与容错口径的分工:构建失败
      才 None,签名失败只降级)。
    """
    if not _wants_ed25519(cfg):
        return  # 未显式请求 ed25519:现状零签名行为,零新增读写
    pending = sum(1 for item in results if item is not None)
    try:
        from netsentinel.evidence import packager  # A205 单一实现,只读复用
    except Exception as exc:  # noqa: BLE001 - 签名器缺席按增强项降级
        telemetry.inc("parallel_pack.sign_skipped", pending)
        logger.warning(
            "产包签名器 packager 不可用,并行产物保持未签名(整批不受影响):%s",
            exc,
        )
        return
    sign_bundle = getattr(packager, "sign_bundle", None)
    zip_bundle = getattr(packager, "zip_bundle", None)
    if not (callable(sign_bundle) and callable(zip_bundle)):
        telemetry.inc("parallel_pack.sign_skipped", pending)
        logger.warning(
            "packager 缺少 sign_bundle/zip_bundle 公开写口(A205 起在席,"
            "A224 起公开),并行产物保持未签名"
        )
        return
    for bundle in results:
        if bundle is None:
            continue
        try:
            pair = _unsigned_product_dir(bundle)
            if pair is None:
                continue
            bdir, mpath = pair
            unsigned_bytes = mpath.read_bytes()
            sign_bundle(bdir, mpath, cfg)
            if mpath.read_bytes() == unsigned_bytes:
                continue  # 策略未落签(如签名失败已回滚):zip 无需刷新
            zip_bundle(bdir, bdir.parent / f"{bdir.name}.zip")
        except Exception as exc:  # noqa: BLE001 - 签名是增强项:失败绝不丢包
            telemetry.inc("parallel_pack.sign_skipped")
            logger.warning(
                "并行产物签名已跳过(该证据包照常返回):%s(%s)",
                getattr(bundle, "site_url", ""),
                exc,
            )


# ---------------------------------------------------------------------------
# 契约入口
# ---------------------------------------------------------------------------
def pack_all(
    reports: list[SiteReport],
    cfg: Config,
    *,
    builder: Callable[[SiteReport, Config], EvidenceBundle] | None = None,
    executor: Executor | None = None,
) -> list[EvidenceBundle | None]:
    """并行构建一批站点证据包,**保序**返回与输入等长的结果列表。

    - 空列表直接返回 ``[]``(不建池、不计时);
    - ``builder`` 缺省惰性 A11 ``packager.build_bundle``(缺席抛中文
      RuntimeError);注入的替身签名同 ``(report, cfg) -> EvidenceBundle``;
    - ``executor`` 注入时直接使用且**不关闭**(归属调用方);缺省走
      A164 ``thread_pool(cfg, label="pack")``(惰性,缺席内置公式兜底);
    - **同 safe_host 串行化**:host→锁字典防 A11 时间戳目录名竞争,
      不同 host 真并发(IO 型:复制/哈希/压缩);
    - 单包失败(含取结果超时)→ 该位 ``None`` 占位 + 中文 warning +
      ``parallel_pack.failures`` 计数,整批不受影响;
    - (A214)收集后按 ``cfg`` 同一签名策略给**未签名**的并行产物补签
      (:func:`_sign_unsigned_products`,复用 A205 ``packager.sign_bundle``
      / ``zip_bundle`` 公开口):缺省 build_bundle 产物已在 zip 前签名
      不重复签;默认 hmac-sha256 配置 = 现状零签名行为;补签失败只降级
      告警,该包结果原样保留(绝不成 None 占位);
    - 批量超过 :data:`MAX_BATCH` 抛中文 ValueError(红线 37 总量上限);
    - 遥测:整批 ``parallel_pack.all`` 计时;成功包数一次性累计
      ``parallel_pack.bundles``;失败包逐包累计 ``parallel_pack.failures``。

    用法示例::

        bundles = pack_all(reports, cfg)                      # 缺省线程池
        bundles = pack_all(reports, cfg, builder=fake_build)  # 注入替身
    """
    if not reports:
        return []
    if len(reports) > MAX_BATCH:
        raise ValueError(
            f"单批站点报告数 {len(reports)} 超过上限 {MAX_BATCH},请分片后重试"
        )
    build = builder if builder is not None else _default_builder()

    def _one(report: SiteReport) -> EvidenceBundle:
        return _pack_one(build, report, cfg)

    with telemetry.timer("parallel_pack.all"):
        if executor is not None:
            # 注入执行器:直接使用,不关闭(归属调用方)。
            futures = [executor.submit(_one, r) for r in reports]
            results = _collect_ordered(reports, futures)
        else:
            mod = _ops_concurrency()
            if mod is not None:
                # 缺省 ①:惰性复用 A164 thread_pool(上下文管理器,退出必关闭)。
                with mod.thread_pool(cfg, label="pack") as pool:
                    futures = [pool.submit(_one, r) for r in reports]
                    results = _collect_ordered(reports, futures)
            else:
                # 缺省 ②:A164 缺席兜底自建(io_workers 内置公式;with 关闭兜底)。
                with ThreadPoolExecutor(
                    max_workers=_io_workers(cfg), thread_name_prefix="pack"
                ) as pool:
                    futures = [pool.submit(_one, r) for r in reports]
                    results = _collect_ordered(reports, futures)
        # A214:按 cfg 同一签名策略给未签名的并行产物补签(缺省 builder
        # 产物已在 A205 内 zip 前签名;默认 hmac-sha256 配置零行为)
        _sign_unsigned_products(results, cfg)
        ok_count = sum(1 for item in results if item is not None)
        telemetry.inc("parallel_pack.bundles", ok_count)
        return results
