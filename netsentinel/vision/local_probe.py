"""A143 · 本地视觉服务探测(NetSentinel V8):清点本机 OpenAI 兼容服务的视觉模型。

V8 自动接管(A155 takeover)/连接向导(A145 setup)/模型页(A161)的公共
本地探测件:按端口逐个 GET ``http://127.0.0.1:{port}/v1/models``,把命中的
**视觉模型**清点出来(过滤委托 A150 capability)。

红线 32(本地探测仅限回环)在本模块的落实:

- 探测主机**恒为 127.0.0.1**:URL 只由端口拼进 :func:`_base_url`,不接受
  任何 host 参数——绝不扫外网/内网,绝不扫回环全端口段,只打构造/调用时
  传入的端口;
- 端口先验合法(1-65535 数字),非法端口直接中文报错行,**零网络**;
- **不跟随重定向**:3xx 一律按错误回报(重定向可能把探测引离回环);
- 响应体读取上限 :data:`MAX_RESPONSE_BYTES`,异常服务的超大响应即停。

provider 判定 = 端口映射目录 :data:`PORT_PROVIDERS`:11434→ollama、
1234→lmstudio、8000→vllm、9997→xinference,其余端口一律
``"openai-compat"``(OpenAI 兼容服务统称,含任意自建网关)。

视觉过滤(委托 A150,惰性导入):``capability.filter_vision`` 未就位
(并行开发期或测试注入)或调用异常时**全保留**并标 ``"unfiltered": True``,
绝不因兄弟模块缺席而丢模型——宁多报不漏报,由向导页向用户如实标注。

用法示例::

    from netsentinel.vision.local_probe import LocalVisionScanner

    scanner = LocalVisionScanner()   # 缺省端口 ["11434", "1234", "8000", "9997"]
    for row in scanner.scan():
        # {"provider": "ollama", "port": "11434",
        #  "base_url": "http://127.0.0.1:11434/v1",
        #  "models": ["llava:13b"], "ok": True, "error": "", "unfiltered": False}
        ...

行语义(每端口一行,顺序与端口表一致;任何分支都**不抛出**,七键恒定,
便于向导/REST 直接 JSON 序列化):

- 服务在线且响应可解析 → ``ok=True``(``models`` 可能为空列表 = 在线但
  无视觉模型,同样是有效清点结果);
- 端口非法/连接失败/超时/HTTP 错/坏 JSON/缺 data 字段/响应超大 →
  ``ok=False`` + 中文 ``error``(``models`` 恒为 ``[]``);
- ``"unfiltered"``:``True`` = capability 缺席未过滤(全保留);
  ``False`` = 已过滤(或无模型)。

遥测(只存名称与数字,红线 17):每次 :meth:`LocalVisionScanner.scan` 计
``local_probe.scan``;每个命中视觉模型的端口计 ``local_probe.hit``。
"""
from __future__ import annotations

import importlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from netsentinel import telemetry

__all__ = [
    "DEFAULT_PORTS",
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "PORT_PROVIDERS",
    "LocalVisionScanner",
]

logger = logging.getLogger(__name__)

#: 缺省探测端口表(与 ``contracts.Config.local_probe_ports`` 同源:
#: ollama / lmstudio / vllm / xinference 的惯例端口)
DEFAULT_PORTS: list[str] = ["11434", "1234", "8000", "9997"]

#: 缺省单端口探测超时(秒):本地回环应当秒回,1.5s 已属宽松
DEFAULT_TIMEOUT_S: float = 1.5

#: 单端口响应体读取上限(字节;与 A66 local_gateway / A147 connectivity 同口径)
MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB

#: 端口 → 提供方映射目录(契约 A143;其余端口一律 openai-compat)
PORT_PROVIDERS: dict[str, str] = {
    "11434": "ollama",
    "1234": "lmstudio",
    "8000": "vllm",
    "9997": "xinference",
}

#: 映射目录之外的端口统一按 OpenAI 兼容服务回报
_FALLBACK_PROVIDER = "openai-compat"

#: 探测主机(红线 32:恒为回环字面地址,模块内唯一来源,不解析、不可注入)
_PROBE_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# 兄弟模块惰性导入(A150 capability;并行开发期未就位 → None,不抛)
# ---------------------------------------------------------------------------


def _load_sibling(dotted: str) -> Any:
    """惰性导入兄弟模块;未就位(含测试用 ``sys.modules`` 注入 ``None`` 拦截)返回 None。

    刻意**不做进程内缓存**:每次现查 ``sys.modules``,测试注入的替身/拦截
    即时生效(与 A147 connectivity 同款)。
    """
    try:
        return importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - ImportError / 并行期 SyntaxError 一律降级
        logger.debug("兄弟模块 %s 未就位(忽略):%s", dotted, exc)
        return None


# ---------------------------------------------------------------------------
# URL 构造 / opener / 端口规整(红线 32:主机恒回环、不跟随重定向)
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝跟随重定向(A66/A147 同款纪律):3xx 一律按错误回报,探测绝不离开回环。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


#: 不跟随重定向的 opener(仅本地 GET 探测使用;测试可整体替换注入)
_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _base_url(port: str) -> str:
    """探测基址:``http://127.0.0.1:{port}/v1``(主机恒为 :data:`_PROBE_HOST`)。"""
    return f"http://{_PROBE_HOST}:{port}/v1"


def _normalize_ports(ports: Any) -> list[str]:
    """端口表规整:逐项转字符串并去空白,去重保序,丢弃空白项。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in ports if ports is not None else []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _validate_port(port: str) -> str:
    """端口合法性检查:通过返回空串;非法返回中文原因(不发起任何网络请求)。"""
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return f"端口 {port} 不是有效端口号(应为 1-65535 的数字),未发起探测"
    return ""


# ---------------------------------------------------------------------------
# HTTP GET + 响应解析(纯标准库 urllib,风格对齐 A66/A147)
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout_s: float) -> tuple[int, str]:
    """单次 GET。

    - 固定超时 ``timeout_s``;``HTTPError``(含被拒绝的 3xx)转为
      ``(状态码, 错误体)`` 返回,由调用方按状态码判错;
    - 超时/套接字错误原样上抛,由调用方分级转中文;
    - 响应体读取上限 :data:`MAX_RESPONSE_BYTES`(读 N+1 字节以探测越界)。
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method="GET"
    )
    try:
        with _OPENER.open(request, timeout=float(timeout_s)) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(getattr(response, "status", 200) or 200)
            return status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx/被拒 3xx:转状态码 + 错误体
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 错误体读取失败不应掩盖状态码
            body = ""
        return int(exc.code), body


def _parse_model_ids(body: str) -> tuple[list[str] | None, str]:
    """解析 OpenAI 兼容 /v1/models 响应;返回 ``(模型 id 列表或 None, 中文错误)``。"""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None, "本地服务返回的不是合法 JSON,无法解析模型列表"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "本地服务响应缺少模型列表字段 data,接口形态与 OpenAI /v1/models 不符"
    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip() and model_id not in models:
                models.append(model_id)
    return models, ""


def _filter_vision_models(model_ids: list[str]) -> tuple[list[str], bool]:
    """视觉过滤(委托 A150,惰性导入);返回 ``(过滤结果, unfiltered)``。

    capability 未就位或 ``filter_vision`` 调用异常 → 全保留且
    ``unfiltered=True``(宁多报不漏报,由调用方向用户如实标注"未过滤")。
    """
    capability = _load_sibling("netsentinel.vision.capability")
    if capability is not None:
        try:
            return list(capability.filter_vision(list(model_ids))), False
        except Exception as exc:  # noqa: BLE001 - A150 接口异常时降级全保留
            logger.debug("capability.filter_vision 调用异常,降级全保留:%s", exc)
    return list(model_ids), True


# ---------------------------------------------------------------------------
# 扫描器
# ---------------------------------------------------------------------------


class LocalVisionScanner:
    """本地视觉服务扫描器(仅回环,红线 32;无共享可变状态,可并发 scan)。

    参数:

    - ``ports``:探测端口表(字符串,或可转字符串的数字);``None`` →
      :data:`DEFAULT_PORTS`。构造时**快照 + 规整**(逐项去空白、去重保序、
      丢空白项),之后修改传入的列表对象不影响本扫描器;
    - ``timeout``:单端口 GET 超时(秒),缺省 :data:`DEFAULT_TIMEOUT_S`;
      非正数/非数值抛中文 :class:`ValueError`。
    """

    def __init__(self, ports: list[str] | None = None, *, timeout: float = DEFAULT_TIMEOUT_S):
        self._ports = _normalize_ports(ports if ports is not None else DEFAULT_PORTS)
        try:
            self._timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"探测超时必须为数值(秒):{exc}") from exc
        if self._timeout <= 0:
            raise ValueError(f"探测超时必须为正数(秒),当前为 {timeout!r}")

    @property
    def ports(self) -> list[str]:
        """当前生效的端口表快照(:meth:`scan` 的 ``ports`` 临时覆盖不改变它)。"""
        return list(self._ports)

    @property
    def timeout(self) -> float:
        """单端口探测超时(秒)。"""
        return self._timeout

    def scan(self, ports: list[str] | None = None) -> list[dict]:
        """逐端口探测,返回结果行列表(顺序与端口表一致;任何分支都不抛出)。

        - ``ports``:覆盖**本次**探测的端口表(仅本次生效);``None`` →
          构造时快照的端口表;
        - 每端口一行,七键恒定::

              {"provider": "ollama", "port": "11434",
               "base_url": "http://127.0.0.1:11434/v1",
               "models": ["llava:13b"], "ok": True,
               "error": "", "unfiltered": False}

        - 命中(在线且清点到视觉模型)计 ``telemetry.inc("local_probe.hit")``。
        """
        telemetry.inc("local_probe.scan")
        targets = self._ports if ports is None else _normalize_ports(ports)
        return [self._probe_one(port) for port in targets]

    # -- 单端口探测 ----------------------------------------------------------

    def _probe_one(self, port: str) -> dict:
        row: dict = {
            "provider": PORT_PROVIDERS.get(port, _FALLBACK_PROVIDER),
            "port": port,
            "base_url": _base_url(port),
            "models": [],
            "ok": False,
            "error": "",
            "unfiltered": False,
        }
        problem = _validate_port(port)
        if problem:
            row["error"] = problem
            return row
        return self._probe_endpoint(row, port)

    def _probe_endpoint(self, row: dict, port: str) -> dict:
        """对 ``{base_url}/models`` 发一次 GET,把任何失败转中文 error 落进 row。"""
        url = row["base_url"] + "/models"
        try:
            status, body = _http_get(url, self._timeout)
        except TimeoutError:
            row["error"] = (
                f"本地服务探测超时({self._timeout:g}s 内未响应,"
                "服务可能未启动或响应过慢)"
            )
            return row
        except urllib.error.URLError as exc:  # urllib 会把套接字错误包进 reason
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                row["error"] = (
                    f"本地服务探测超时({self._timeout:g}s 内未响应,"
                    "服务可能未启动或响应过慢)"
                )
            else:
                row["error"] = (
                    f"无法连接本地服务(端口 {port}):"
                    f"{reason if reason is not None else exc};服务可能未启动或端口不符"
                )
            return row
        except OSError as exc:  # 连接拒绝 / 套接字层错误直抛形态
            row["error"] = (
                f"无法连接本地服务(端口 {port}):{exc};服务可能未启动或端口不符"
            )
            return row
        except Exception as exc:  # noqa: BLE001 - 注入 opener 的任意异常兜底
            row["error"] = f"本地服务探测失败({type(exc).__name__}):{exc}"
            return row

        if not 200 <= status < 300:  # 4xx/5xx/被拒 3xx 一律按错误回报
            row["error"] = f"本地服务返回 HTTP {status},模型清点接口不可用或服务未就绪"
            return row
        if len(body) > MAX_RESPONSE_BYTES:
            row["error"] = f"本地服务响应超过 {MAX_RESPONSE_BYTES} 字节上限,疑似异常服务"
            return row

        model_ids, parse_error = _parse_model_ids(body)
        if model_ids is None:
            row["error"] = parse_error
            return row

        models, unfiltered = _filter_vision_models(model_ids)
        row["models"] = models
        row["unfiltered"] = unfiltered
        row["ok"] = True
        if models:
            telemetry.inc("local_probe.hit")
            logger.info(
                "本地探测命中:%s(%s),%d 个视觉模型",
                row["base_url"],
                row["provider"],
                len(models),
            )
        else:
            logger.debug("本地服务在线但无视觉模型:%s", row["base_url"])
        return row
