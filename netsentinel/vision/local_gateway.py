# -*- coding: utf-8 -*-
"""本地推理网关探测(A66 · V4):Ollama / vLLM / LM Studio / Xinference。

"数据不出本机"路线(红线 16)的支点模块:四家本地推理服务都暴露
OpenAI 兼容的 ``GET {base_url}/models`` 模型清点接口(Ollama 的
``/v1/models`` 同形态),本模块仅用标准库 urllib 对其做**发现与健康检查**,
供 ``vlmctl doctor``(A67)与复核台提供方面板(A76)展示。

对外三个函数(签名见 CONTRACTS-V4 §4 A66):

- :func:`is_local`:查提供方目录(A61 ``providers.PROVIDERS``)的 ``local``
  标记;目录未就位时退回内置四家判断;
- :func:`probe`:对单个 ``base_url`` 发一次 GET ``/models`` 并解析
  ``data[].id`` → ``{"ok", "models", "error"}``;
- :func:`local_status`:对四家本地提供方逐一 probe 的汇总(每家附 ``base_url``),
  **不做任何缓存**,每次显式调用现查现算;V5 起支持 ``workers`` 并发探测
  (默认保守串行,行为与旧版逐比特一致)。

安全边界(红线 16 / 20):

- probe 只在**显式调用**时发起网络请求,本模块自身从不主动外呼;
- 只允许探测本机地址(host ∈ 127.0.0.1 / localhost / ::1),其余一律拒绝,
  且**不跟随重定向**(3xx 一律按错误回报,杜绝借 30x 跳出回环);
- 不持有、不发送任何密钥;错误消息全部为中文;日志只记地址与结果,无凭据。

V5 升级(契约 §1 菜单):

- **性能**:probe 超时分级(connect/read 两档,连接期短、读取期长),
  本地服务未就绪时更快定性;``local_status(workers=N)`` 并发探测,
  四家墙钟时间从 4×超时 降为 ≈1×超时;
- **健壮性**:响应体大小上限(:data:`_MAX_RESPONSE_BYTES`),异常本地服务
  的超大响应不再拖垮诊断进程;连接/读取/协议错误全部分级回报,绝不抛出;
- **可观测性**:``telemetry.inc("local.probe")`` / ``local.probe.errors`` /
  ``local.probe.denied`` 计数与 ``local.probe.duration`` 计时(只存名称与数字)。

用法示例::

    from netsentinel.vision import local_gateway

    local_gateway.is_local("ollama")                 # True
    local_gateway.probe("http://127.0.0.1:11434/v1") # {"ok": ..., "models": [...], ...}
    local_gateway.local_status(cfg, workers=4)       # 四家并发健康汇总
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from netsentinel import telemetry

__all__ = [
    "LOCAL_PROVIDERS",
    "DEFAULT_LOCAL_BASE_URLS",
    "DEFAULT_PROBE_TIMEOUT_S",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "is_local",
    "probe",
    "local_status",
]

logger = logging.getLogger(__name__)

#: 四家本地推理提供方(与 CONTRACTS-V4 §2 目录一致,顺序即 local_status 汇总顺序)
LOCAL_PROVIDERS: tuple[str, ...] = ("ollama", "vllm", "lmstudio", "xinference")

#: 目录未就位(A61 并行开发期)时的内置判断与缺省端点(契约 §2 提示值,
#: 上线前应经 vlmctl doctor 核验一次)
DEFAULT_LOCAL_BASE_URLS: dict[str, str] = {
    "ollama": "http://127.0.0.1:11434/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "xinference": "http://127.0.0.1:9997/v1",
}

#: 允许探测的主机白名单(仅本机回环;内网/公网地址一律拒绝,红线 20)
_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 非本机地址时的固定错误文案(契约 §4 A66 规定)
_REMOTE_DENIED_ERROR = "仅允许探测本机地址"

#: probe 缺省读取超时(秒):连接建立后等待 /models 响应的上限
DEFAULT_PROBE_TIMEOUT_S = 2.0

#: probe 缺省连接超时(秒,V5 分级超时):回环上 SYN 无响应(端口被
#: 防火墙静默丢弃等)通常 1 秒内即可定性,无需等满读取超时
DEFAULT_CONNECT_TIMEOUT_S = 1.0

#: 单次探测允许读取的最大响应体(字节,V5 健壮性):模型清点接口的合法
#: 响应远小于该值,超过即视为异常服务,停止读取并回报错误
MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB

# ---------------------------------------------------------------------------
# A61 提供方目录的惰性加载(未就位时静默降级到内置目录,不硬依赖)
# ---------------------------------------------------------------------------

_providers_module: Any = None
_providers_import_done = False


def _load_providers() -> Any:
    """惰性加载 A61 ``netsentinel.vision.providers``;未就位/损坏返回 None。

    导入结果进程内只尝试一次(这是对目录模块的加载缓存,不是探测结果缓存;
    probe/local_status 的网络结果永不缓存)。
    """
    global _providers_module, _providers_import_done
    if not _providers_import_done:
        _providers_import_done = True
        try:
            from netsentinel.vision import providers as _mod

            if hasattr(_mod, "PROVIDERS"):
                _providers_module = _mod
            else:  # pragma: no cover - 目录模块形态异常
                _providers_module = None
        except Exception:  # noqa: BLE001 - 并行开发期目录可能尚未就位
            _providers_module = None
    return _providers_module


# ---------------------------------------------------------------------------
# is_local
# ---------------------------------------------------------------------------


def is_local(provider: str) -> bool:
    """判断提供方是否本地推理网关(数据不出本机,免 ``vlm_online``,红线 16)。

    优先查 A61 目录 ``providers.PROVIDERS[name].local``;目录未就位或未收录
    该名称时,退回内置四家判断(ollama / vllm / lmstudio / xinference)。
    名称大小写不敏感,未知提供方一律 ``False``。
    """
    name = str(provider or "").strip().lower()
    mod = _load_providers()
    if mod is not None:
        spec = mod.PROVIDERS.get(name)
        if spec is not None:
            return bool(getattr(spec, "local", False))
    return name in DEFAULT_LOCAL_BASE_URLS


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def _fail(error: str) -> dict[str, Any]:
    """统一的失败返回形态。"""
    return {"ok": False, "models": [], "error": error}


def _check_local_and_build_url(base_url: str) -> tuple[str, str]:
    """校验仅本机地址,并拼出探测 URL;返回 ``(url, 错误)``,两者互斥。"""
    raw = str(base_url or "").strip()
    if not raw:
        return "", "base_url 为空,无法探测本地服务"
    parts = urlsplit(raw)
    host = (parts.hostname or "").strip().lower()
    if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
        return "", _REMOTE_DENIED_ERROR
    return raw.rstrip("/") + "/models", ""


def _split_endpoint(url: str) -> tuple[str, str, int, str]:
    """把探测 URL 拆成 ``(scheme, host, port, path)``(纯函数,便于单测)。

    缺省端口:http → 80,https → 443;路径缺省 ``/``,带查询串原样保留;
    IPv6 字面量主机去掉方括号(供 :func:`socket.create_connection` 使用)。
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path


def _open_connection(
    scheme: str, host: str, port: int, *, connect_timeout: float, read_timeout: float
) -> http.client.HTTPConnection:
    """建立分级超时的 HTTP(S) 连接(V5:connect 与 read 两档)。

    连接阶段用 ``connect_timeout``(较短,SYN 无响应快速定性);
    建立后把套接字超时上调为 ``read_timeout``(较长,容忍模型清点慢响应)。
    https 走默认证书校验的 TLS(与旧 urllib 路径同口径)。
    """
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    try:
        sock.settimeout(read_timeout)
        if scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
    except BaseException:
        sock.close()
        raise
    conn = http.client.HTTPConnection(host, port, timeout=read_timeout)
    conn.sock = sock
    return conn


def probe(
    base_url: str,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
    *,
    connect_timeout: float | None = None,
) -> dict[str, Any]:
    """探测单个本地推理网关:GET ``{base_url}/models`` → ``{"ok","models","error"}``。

    仅应在调用方显式要求时使用(ping/doctor 类人工诊断,红线 20);
    host 非 127.0.0.1 / localhost / ::1 时直接拒绝、**不发起任何网络请求**。

    - 成功:解析 OpenAI 兼容 ``/v1/models`` 形态的 ``data[].id``(Ollama 同),
      非字符串 id 与重复项剔除,返回 ``{"ok": True, "models": [...], "error": ""}``;
    - HTTP 错误 / 连接失败 / 超时 / 非法 JSON / 缺 ``data`` 字段 / 响应体超过
      :data:`MAX_RESPONSE_BYTES`:返回 ``{"ok": False, "models": [], "error": 中文原因}`,
      绝不抛出;3xx 重定向也按错误回报(不跟随,确保探测不离开回环)。

    V5 超时分级:``timeout`` 为读取超时;``connect_timeout`` 为连接超时
    (缺省 :data:`DEFAULT_CONNECT_TIMEOUT_S`,且不超过读取超时),回环上
    连不上基本 1 秒内定性,无需等满读取超时::

        probe("http://127.0.0.1:11434/v1", timeout=2.0)              # 缺省分级
        probe("http://127.0.0.1:11434/v1", connect_timeout=0.5)      # 自定连接档
    """
    telemetry.inc("local.probe")
    url, denied = _check_local_and_build_url(base_url)
    if denied:
        telemetry.inc("local.probe.denied")
        logger.warning("probe 拒绝非本机地址:%r", str(base_url or "").strip())
        return _fail(denied)

    scheme, host, port, path = _split_endpoint(url)
    read_timeout = float(timeout)
    connect_cap = DEFAULT_CONNECT_TIMEOUT_S if connect_timeout is None else float(connect_timeout)
    connect_cap = min(connect_cap, read_timeout)  # 分级不放大调用方给定的总上限

    logger.debug("探测本地网关:%s(连接 %.1fs / 读取 %.1fs)", url, connect_cap, read_timeout)
    with telemetry.timer("local.probe.duration"):
        try:
            conn = _open_connection(
                scheme, host, port, connect_timeout=connect_cap, read_timeout=read_timeout
            )
        except TimeoutError:
            error = f"连接超时({connect_cap:g}s 内无法建立连接,服务可能未启动或端口不符)"
            telemetry.inc("local.probe.errors")
            logger.info("本地网关探测失败 %s:%s", url, error)
            return _fail(error)
        except OSError as exc:  # 连接拒绝 / TLS 失败等套接字层错误
            error = f"无法连接本地服务:{exc}(服务可能未启动或端口不符)"
            telemetry.inc("local.probe.errors")
            logger.info("本地网关探测失败 %s:%s", url, error)
            return _fail(error)

        try:
            conn.request("GET", path, headers={"Accept": "application/json"})
            response = conn.getresponse()
            status = response.status
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except TimeoutError:  # 读取阶段超时(套接字已升级为 read_timeout)
            error = f"读取超时({read_timeout:g}s 内未返回完整响应,服务响应慢或接口挂起)"
            telemetry.inc("local.probe.errors")
            logger.info("本地网关探测失败 %s:%s", url, error)
            return _fail(error)
        except http.client.HTTPException as exc:  # 协议层错误(坏状态行等)
            error = f"本地服务响应异常:{exc}(接口形态与 HTTP 不符)"
            telemetry.inc("local.probe.errors")
            logger.info("本地网关探测失败 %s:%s", url, error)
            return _fail(error)
        except OSError as exc:  # 读取中断 / 连接被重置等
            error = f"无法连接本地服务:{exc}(服务可能未启动或端口不符)"
            telemetry.inc("local.probe.errors")
            logger.info("本地网关探测失败 %s:%s", url, error)
            return _fail(error)
        finally:
            conn.close()

    if not 200 <= status < 300:  # 4xx/5xx 原口径;3xx 不跟随,一并按错误回报
        error = f"本地服务返回 HTTP {status},接口不存在或服务未就绪"
        telemetry.inc("local.probe.errors")
        logger.info("本地网关探测失败 %s:%s", url, error)
        return _fail(error)
    if len(raw) > MAX_RESPONSE_BYTES:
        error = f"响应体超过 {MAX_RESPONSE_BYTES} 字节上限,疑似异常服务,已停止读取"
        telemetry.inc("local.probe.errors")
        logger.info("本地网关探测失败 %s:%s", url, error)
        return _fail(error)

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, TypeError):
        error = "响应不是合法 JSON,本地服务返回了无法解析的内容"
        telemetry.inc("local.probe.errors")
        logger.info("本地网关探测失败 %s:%s", url, error)
        return _fail(error)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        error = "响应缺少模型列表字段 data,接口形态与 OpenAI /v1/models 不符"
        telemetry.inc("local.probe.errors")
        logger.info("本地网关探测失败 %s:%s", url, error)
        return _fail(error)

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id and model_id not in models:
                models.append(model_id)
    logger.info("本地网关 %s 在线,发现 %d 个模型", url, len(models))
    return {"ok": True, "models": models, "error": ""}


# ---------------------------------------------------------------------------
# local_status
# ---------------------------------------------------------------------------


def _local_base_url(provider: str, cfg: Any) -> str:
    """解析某本地提供方的探测 base_url:cfg 显式覆盖 → A61 目录 → 内置缺省。"""
    overrides = getattr(cfg, "vlm_provider_base_urls", None)
    if isinstance(overrides, dict):
        override = str(overrides.get(provider, "") or "").strip()
        if override:
            return override
    mod = _load_providers()
    if mod is not None:
        try:  # resolve 会应用 cfg 覆盖并回落目录;本地提供方缺模型时抛 ValueError,属预期
            resolved = mod.resolve(provider, cfg)
            base = str(getattr(resolved, "base_url", "") or "").strip()
            if base:
                return base
        except Exception:  # noqa: BLE001 - 缺模型等配置问题不影响健康检查
            pass
        spec = mod.PROVIDERS.get(provider)
        catalog_base = str(getattr(spec, "base_url", "") or "").strip() if spec else ""
        if catalog_base:
            return catalog_base
    return DEFAULT_LOCAL_BASE_URLS.get(provider, "")


def local_status(
    cfg: Any,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
    connect_timeout: float | None = None,
    workers: int = 1,
) -> dict[str, dict[str, Any]]:
    """四家本地推理网关健康汇总(供 ``vlmctl doctor`` 与复核台使用)。

    对 ``LOCAL_PROVIDERS`` 逐一 probe:base_url 先取
    ``cfg.vlm_provider_base_urls`` 的显式覆盖,其次 A61 目录(``providers.resolve``,
    其因缺模型抛出的 ValueError 会被忽略并回落目录缺省端点),最后用内置缺省。

    返回 ``{provider: {"ok", "models", "error", "base_url"}}``,键序恒为
    ``LOCAL_PROVIDERS`` 顺序,与并发与否无关。

    **不做任何缓存**(契约 A66):每次显式调用都对四家现查现算,结果即时反映
    服务启停;probe 自带仅本机地址校验,非本机覆盖会被逐家拒绝并记录原因。

    V5 并发:``workers > 1`` 时用线程池并发 probe(探测是网络等待型,
    GIL 可让出)。四家各 2s 读取超时的最坏墙钟时间从 8s 降到 ≈2s;
    ``workers`` 缺省 1(串行),默认行为与旧版完全一致——含对注入桩
    ``probe`` 的调用顺序,便于既有测试与调用方零感知;``vlmctl doctor``
    会显式传 ``workers=4`` 取收益。::

        local_status(cfg)               # 串行(默认,行为不变)
        local_status(cfg, workers=4)    # 四家并发,≈1× 超时墙钟
    """
    kwargs: dict[str, Any] = {"timeout": timeout}
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    targets = [(provider, _local_base_url(provider, cfg)) for provider in LOCAL_PROVIDERS]

    if workers <= 1:
        status: dict[str, dict[str, Any]] = {}
        for provider, base_url in targets:
            entry = dict(probe(base_url, **kwargs))
            entry["base_url"] = base_url
            status[provider] = entry
        return status

    from concurrent.futures import ThreadPoolExecutor  # 惰性导入:仅并发路径需要

    def _probe_one(item: tuple[str, str]) -> dict[str, Any]:
        _provider, base_url = item
        entry = dict(probe(base_url, **kwargs))
        entry["base_url"] = base_url
        return entry

    max_workers = max(1, min(int(workers), len(targets)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="a66-local-status") as pool:
        entries = list(pool.map(_probe_one, targets))
    return {target[0]: entry for target, entry in zip(targets, entries)}
