"""净网哨兵 NetSentinel 模型管理 REST 路由(A151,V8 视觉模型自动接管批次)。

把 V8 的"活动模型"机制(model_runtime.json)API 化,构成手动切换三入口
(向导页 / CLI ``python -m netsentinel.modelmgr`` / REST)中的 **REST 入口**;
由负责人在既有本机服务 ``service/app.py`` 上挂载(CONTRACTS-V8 §4,代理勿改)::

    from netsentinel.service.model_api import create_model_router

    app.include_router(create_model_router(cfg))   # 一行接线

fastapi 为**惰性导入**(可选依赖 ``pip install '.[api]'``,仅在
:func:`create_model_router` 函数内 import);兄弟模块(A143 local_probe /
A144 model_manager / A147 connectivity,并行开发)一律经模块级惰性获取缝
在**请求时**导入——兄弟缺席或暂不可导入时端点返回 503 中文错误,
绝不让服务进程崩溃。

端点一览(统一前缀 ``/model``):
- ``GET  /model/active``  查询当前活动模型 →
  ``{"spec": str|None, "switched_by": str|None, "switched_at": str|None}``;
- ``POST /model/switch``  ``{"spec"}`` → A144 ``set_active(spec,
  switched_by="rest")``(经 providers.parse_spec 校验,validate 时惰性调
  A147 连通性测试)→ 成功 ``{"ok": true, "status": {...三键...}}``;
  spec 非法或连通性不通过 → **409 中文**;请求体缺 spec → 422 中文;
- ``POST /model/probe``   显式触发 A143 本地探测 → ``{"local":
  [{"provider","base_url","models","ok"}, ...]}``;
- ``POST /model/test``    ``{"spec"}`` → A147 ``test_connection(spec, cfg)``
  **原样透传**(``{"ok","latency_ms","model","error"?}``;A147 契约保证
  异常转中文 error 字段、不抛出)。

安全红线(违反即缺陷,CONTRACTS-V8 红线 32/33):
33. **本路由绝不提供任何写密钥端点**(不注册任何密钥写入/读取路径):
    密钥只能经连接向导页面(A145,密码语义输入)或 CLI 通道写入,落盘仅经
    ``netsentinel.security.keys`` 的密钥落盘函数;本路由所有响应与日志
    **绝不包含密钥本体**——出参一律走 :func:`_active_payload` 三键白名单,
    即使 A144 ``status()`` 未来携带掩码信息也不会外漏。
32. 本地探测仅在用户显式请求(``POST /model/probe``)时触发,A143 只探测
    127.0.0.0/8 上 ``cfg.local_probe_ports`` 列出的端口,绝不外扫、绝不扫
    回环全端口段。

测试约定(见 tests/test_model_api.py):三个获取缝
(:func:`_get_manager_cls` / :func:`_get_scanner_cls` /
:func:`_get_test_connection`)是兄弟模块的替换缝,
``monkeypatch.setattr(model_api, "_get_manager_cls", lambda: FakeManager)``
即可离线注入替身,全程零网络、零真实密钥。
"""
from __future__ import annotations

import logging
from typing import Any

from netsentinel.contracts import Config

__all__ = ["create_model_router"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 兄弟模块惰性获取缝(测试经 monkeypatch 模块属性注入替身,全离线)
# ---------------------------------------------------------------------------
def _get_manager_cls() -> Any:
    """惰性导入 A144 ``ModelManager`` 类;未就位时抛中文 RuntimeError。"""
    try:
        from netsentinel.vision import model_manager
    except Exception as exc:  # noqa: BLE001 - 并行开发期兄弟模块可能未就位
        raise RuntimeError(
            f"模型管理依赖 netsentinel.vision.model_manager(A144),当前不可用:{exc}"
        ) from exc
    return model_manager.ModelManager


def _get_scanner_cls() -> Any:
    """惰性导入 A143 ``LocalVisionScanner`` 类;未就位时抛中文 RuntimeError。"""
    try:
        from netsentinel.vision import local_probe
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"本地探测依赖 netsentinel.vision.local_probe(A143),当前不可用:{exc}"
        ) from exc
    return local_probe.LocalVisionScanner


def _get_test_connection() -> Any:
    """惰性导入 A147 ``test_connection`` 函数;未就位时抛中文 RuntimeError。"""
    try:
        from netsentinel.vision import connectivity
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"连通性测试依赖 netsentinel.vision.connectivity(A147),当前不可用:{exc}"
        ) from exc
    return connectivity.test_connection


def _active_payload(status: Any) -> dict[str, Any]:
    """把 A144 ``status()`` 结果规整为对外三键白名单(红线 33 收敛出口)。

    只放行 ``spec`` / ``switched_by`` / ``switched_at`` 三个键——本路由
    绝不透传状态里的其他字段,从根上保证响应不可能夹带密钥(哪怕掩码)。
    兼容 dict 与对象属性两种形态。
    """
    if isinstance(status, dict):
        data = status
    else:
        data = {k: getattr(status, k, None) for k in ("spec", "switched_by", "switched_at")}
    return {
        "spec": data.get("spec"),
        "switched_by": data.get("switched_by"),
        "switched_at": data.get("switched_at"),
    }


# ---------------------------------------------------------------------------
# 路由工厂
# ---------------------------------------------------------------------------
def create_model_router(cfg: Config | None = None) -> "APIRouter":
    """构建模型管理子路由(fastapi 可选依赖,函数内惰性导入)。

    :param cfg: 全局配置;缺省 ``load_config()``(默认找 ``./config.yaml``)。
    :return: ``APIRouter``(前缀 ``/model``,四个只读/切换端点;
        **不含任何写密钥端点**,红线 33),由 ``service/app.py`` 挂载。

    示例::

        router = create_model_router(cfg)
        app.include_router(router)          # service/app.py 接线
        with TestClient(app) as client:
            assert client.get("/model/active").json()["spec"] is None

    端点内兄弟模块一律经请求期惰性缝获取(A143/A144/A147),缺席 → 503 中文;
    切换失败(spec 非法/连通性不通过)→ 409 中文;请求体缺字段 → 422 中文。
    """
    from fastapi import APIRouter, HTTPException

    if cfg is None:
        from netsentinel.config import load_config

        cfg = load_config()

    def _body_str(body: dict[str, Any] | None, key: str, err: str) -> str:
        """从 JSON 请求体取字符串字段;缺失或类型不符 → 422 中文错误。

        不用 Pydantic 模型,手工校验保证 422 文案为中文(同 service.app)。
        """
        value = (body or {}).get(key)
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=err)
        return value.strip()

    # 注:本环境 fastapi 的 APIRouter 构造器不接受 summary/description 参数,
    # 红线 33 声明放在模块 docstring 与各端点 summary 里。
    router = APIRouter(prefix="/model", tags=["model"])

    # ------------------------------------------------------------------
    # GET /model/active:查询当前活动模型
    # ------------------------------------------------------------------
    @router.get("/active", summary="查询当前活动模型(spec/来源/时间)")
    def model_active() -> dict[str, Any]:
        try:
            manager_cls = _get_manager_cls()
        except RuntimeError as exc:  # 兄弟模块缺席 → 503 中文
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            manager = manager_cls(cfg.model_runtime_path)
            return _active_payload(manager.status())
        except Exception as exc:  # noqa: BLE001 - 状态文件异常等
            raise HTTPException(
                status_code=500, detail=f"读取活动模型失败:{exc}"
            ) from exc

    # ------------------------------------------------------------------
    # POST /model/switch:切换活动模型(switched_by 固定为 "rest",
    # 不接受请求体指定来源;失败 409 中文)
    # ------------------------------------------------------------------
    @router.post("/switch", summary="切换活动模型(REST 入口,失败 409)")
    def model_switch(request: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = _body_str(
            request, "spec", "spec 不合法:请求体必须含字符串字段 spec"
        )
        if not spec:
            raise HTTPException(status_code=422, detail="spec 不合法:不能为空")
        try:
            manager_cls = _get_manager_cls()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            manager = manager_cls(cfg.model_runtime_path)
            manager.set_active(spec, switched_by="rest")
        except ValueError as exc:
            # A144:spec 非法(parse_spec)或 validate 连通性测试不通过
            # → 中文 ValueError → 409
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 构造/切换内部异常
            raise HTTPException(status_code=500, detail=f"切换失败:{exc}") from exc
        try:
            status_payload = _active_payload(manager.status())
        except Exception as exc:  # noqa: BLE001 - 切换已生效,状态回读失败不回滚
            logger.warning("切换成功但状态回读失败(spec=%s):%s", spec, exc)
            status_payload = {"spec": spec, "switched_by": "rest", "switched_at": None}
        return {"ok": True, "status": status_payload}

    # ------------------------------------------------------------------
    # POST /model/probe:显式触发本地探测(A143,仅回环端口,红线 32)
    # ------------------------------------------------------------------
    @router.post("/probe", summary="探测本机视觉服务(仅回环端口,红线 32)")
    def model_probe() -> dict[str, Any]:
        try:
            scanner_cls = _get_scanner_cls()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            scanner = scanner_cls(list(cfg.local_probe_ports or []))
            results = scanner.scan()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500, detail=f"本地探测失败:{exc}"
            ) from exc
        return {"local": list(results or [])}

    # ------------------------------------------------------------------
    # POST /model/test:连通性测试(A147 原样透传,契约保证不抛)
    # ------------------------------------------------------------------
    @router.post("/test", summary="测试模型连通性(A147 结果透传)")
    def model_test(request: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = _body_str(
            request, "spec", "spec 不合法:请求体必须含字符串字段 spec"
        )
        if not spec:
            raise HTTPException(status_code=422, detail="spec 不合法:不能为空")
        try:
            test_connection = _get_test_connection()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            result = test_connection(spec, cfg)
        except Exception as exc:  # noqa: BLE001 - A147 契约不抛,此处仅兜底
            raise HTTPException(
                status_code=500, detail=f"连通性测试失败:{exc}"
            ) from exc
        return dict(result or {})

    return router
