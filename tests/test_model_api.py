"""A151 模型管理 REST 路由(netsentinel/service/model_api.py)离线单元测试。

全部离线,零网络、零真实密钥:
- fastapi / httpx 为可选依赖,缺失时整体 skip(importorskip);
- 兄弟模块 A143(local_probe)/ A144(model_manager)/ A147(connectivity)
  并行开发、可能未就位,一律经 ``model_api`` 的三个惰性获取缝
  (``_get_manager_cls`` / ``_get_scanner_cls`` / ``_get_test_connection``)
  以 monkeypatch 模块属性的方式注入 fake 替身;
- 红线 33 专项:不提供任何写密钥端点(路由表 + 源码标识断言),
  所有响应不含密钥本体(即便请求体故意塞入密钥也绝不回显)。
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from netsentinel.contracts import Config  # noqa: E402
from netsentinel.service import model_api  # noqa: E402


# ---------------------------------------------------------------------------
# A144 ModelManager 替身:真实落盘 json(损坏重建语义子集)+ 中文校验错误
# ---------------------------------------------------------------------------
_KNOWN_PROVIDERS = {
    "ollama",
    "lmstudio",
    "vllm",
    "xinference",
    "openai",
    "anthropic",
    "qwen",
    "openrouter",
    "stub",
}


class FakeManager:
    """A144 替身:``set_active`` 校验提供方并落盘;失败抛中文 ValueError。"""

    #: 记录 (spec, switched_by) 调用序列
    saved: list[tuple[str, str]] = []
    #: 模拟 A147 连通性测试不通过的 spec(validate=True → 中文 ValueError)
    fail_specs: set[str] = set()

    def __init__(self, path: str) -> None:
        self.path = str(path)

    def _load(self) -> dict[str, Any] | None:
        try:
            return json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):  # 缺失或损坏 → None(重建语义)
            return None

    def get_active(self) -> str | None:
        return (self._load() or {}).get("spec")

    def set_active(self, spec: str, *, switched_by: str, validate: bool = True) -> None:
        provider = str(spec or "").split(":", 1)[0].strip()
        if provider not in _KNOWN_PROVIDERS:
            raise ValueError(
                f"未知视觉模型提供方:{provider or '(空)'}。可用提供方({'、'.join(sorted(_KNOWN_PROVIDERS))})。"
            )
        if validate and spec in FakeManager.fail_specs:
            raise ValueError(f"连通性测试失败:{spec} 不可达,切换被拒绝")
        FakeManager.saved.append((spec, switched_by))
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "spec": spec,
                    "switched_by": switched_by,
                    "switched_at": "2026-10-02T09:00:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        Path(self.path).unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        data = self._load() or {}
        return {
            "spec": data.get("spec"),
            "switched_by": data.get("switched_by"),
            "switched_at": data.get("switched_at"),
        }


# ---------------------------------------------------------------------------
# A143 LocalVisionScanner 替身
# ---------------------------------------------------------------------------
class FakeScanner:
    """A143 替身:返回固定本地模型列表;``error`` 非空时模拟扫描异常。"""

    last: "FakeScanner | None" = None
    error: Exception | None = None
    results: list[dict[str, Any]] = [
        {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "models": ["llava"],
            "ok": True,
        }
    ]

    def __init__(self, ports: list[str], timeout: float = 1.5) -> None:
        self.ports = list(ports)
        self.timeout = timeout
        FakeScanner.last = self

    def scan(self) -> list[dict[str, Any]]:
        if FakeScanner.error is not None:
            raise FakeScanner.error
        return [dict(r) for r in FakeScanner.results]


# ---------------------------------------------------------------------------
# A147 test_connection 替身
# ---------------------------------------------------------------------------
class FakeConnectivity:
    calls: list[tuple[str, str]] = []
    fail_specs: set[str] = set()


def fake_test_connection(
    spec: str, cfg: Config, *, transport: Any = None, spend: Any = None
) -> dict[str, Any]:
    FakeConnectivity.calls.append((spec, str(cfg.model_runtime_path)))
    if spec in FakeConnectivity.fail_specs:
        return {
            "ok": False,
            "latency_ms": None,
            "model": spec,
            "error": "连通性测试失败:密钥无效或服务不可达(中文错误)",
        }
    return {"ok": True, "latency_ms": 42.0, "model": spec}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def cfg(tmp_path: Any) -> Config:
    """运行时路径指向 tmp 的配置(不落任何真实 data/ 目录)。"""
    data = tmp_path / "data"
    c = Config()
    c.data_dir = str(data)
    c.model_runtime_path = str(data / "model_runtime.json")
    return c


@pytest.fixture()
def fakes(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> Config:
    """注入三个 fake 兄弟并复位替身状态;返回 cfg。"""
    FakeManager.saved = []
    FakeManager.fail_specs = set()
    FakeScanner.last = None
    FakeScanner.error = None
    FakeConnectivity.calls = []
    FakeConnectivity.fail_specs = set()
    monkeypatch.setattr(model_api, "_get_manager_cls", lambda: FakeManager)
    monkeypatch.setattr(model_api, "_get_scanner_cls", lambda: FakeScanner)
    monkeypatch.setattr(model_api, "_get_test_connection", lambda: fake_test_connection)
    return cfg


@pytest.fixture()
def client(fakes: Config) -> Any:
    app = fastapi.FastAPI()
    app.include_router(model_api.create_model_router(fakes))
    return TestClient(app)


def _seed_runtime(
    cfg: Config,
    spec: str = "ollama:llava",
    by: str = "takeover",
    at: str = "2026-10-01T10:00:00",
) -> None:
    """预置活动模型运行时文件(模拟接管/向导已写入)。"""
    path = Path(cfg.model_runtime_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"spec": spec, "switched_by": by, "switched_at": at}, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# GET /model/active 两态
# ---------------------------------------------------------------------------
def test_active_initial_none(client: Any) -> None:
    """未连接任何模型:spec/来源/时间均为 None(向导应据此弹出)。"""
    resp = client.get("/model/active")
    assert resp.status_code == 200
    assert resp.json() == {"spec": None, "switched_by": None, "switched_at": None}


def test_active_with_model(client: Any, cfg: Config) -> None:
    """已有活动模型:三键齐全透出(来源=takeover、时间=落盘值)。"""
    _seed_runtime(cfg, spec="ollama:llava", by="takeover", at="2026-10-01T10:00:00")
    body = client.get("/model/active").json()
    assert body == {
        "spec": "ollama:llava",
        "switched_by": "takeover",
        "switched_at": "2026-10-01T10:00:00",
    }


# ---------------------------------------------------------------------------
# POST /model/switch
# ---------------------------------------------------------------------------
def test_switch_success_persists_rest(client: Any, cfg: Config) -> None:
    """切换成功:switched_by 固定 rest,落盘生效,active 端点立即可见。"""
    resp = client.post("/model/switch", json={"spec": "ollama:llava"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"]["spec"] == "ollama:llava"
    assert body["status"]["switched_by"] == "rest"
    data = json.loads(Path(cfg.model_runtime_path).read_text(encoding="utf-8"))
    assert data["spec"] == "ollama:llava"
    assert data["switched_by"] == "rest"
    assert FakeManager.saved == [("ollama:llava", "rest")]
    assert client.get("/model/active").json()["spec"] == "ollama:llava"


def test_switch_unknown_provider_409(client: Any, cfg: Config) -> None:
    """spec 提供方未知(A144 parse_spec 校验)→ 409 中文,且不落盘。"""
    resp = client.post("/model/switch", json={"spec": "nonsense:llava"})
    assert resp.status_code == 409
    assert "未知视觉模型提供方" in resp.json()["detail"]
    assert not Path(cfg.model_runtime_path).exists()


def test_switch_connectivity_fail_409(client: Any, cfg: Config) -> None:
    """提供方合法但连通性测试不通过(validate=True)→ 409 中文,不落盘。"""
    FakeManager.fail_specs = {"openai:gpt-4o"}
    resp = client.post("/model/switch", json={"spec": "openai:gpt-4o"})
    assert resp.status_code == 409
    assert "连通性测试失败" in resp.json()["detail"]
    assert not Path(cfg.model_runtime_path).exists()
    assert FakeManager.saved == []


def test_switch_missing_spec_422(client: Any) -> None:
    resp = client.post("/model/switch", json={})
    assert resp.status_code == 422
    assert "必须含字符串字段 spec" in resp.json()["detail"]


def test_switch_non_string_spec_422(client: Any) -> None:
    resp = client.post("/model/switch", json={"spec": 123})
    assert resp.status_code == 422
    assert "spec 不合法" in resp.json()["detail"]


def test_switch_blank_spec_422(client: Any) -> None:
    resp = client.post("/model/switch", json={"spec": "   "})
    assert resp.status_code == 422
    assert "不能为空" in resp.json()["detail"]


def test_switch_ignores_client_switched_by_and_never_echoes_key(
    client: Any, cfg: Config
) -> None:
    """来源由服务端固定为 rest(不接受请求体指定);夹带的密钥绝不回显。"""
    resp = client.post(
        "/model/switch",
        json={"spec": "stub", "switched_by": "wizard", "key": "sk-secret-value-123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]["switched_by"] == "rest"
    assert FakeManager.saved == [("stub", "rest")]
    assert "sk-secret-value-123" not in json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# POST /model/probe
# ---------------------------------------------------------------------------
def test_probe_ok(client: Any) -> None:
    """探测返回 A143 扫描结果列表(仅回环,红线 32)。"""
    resp = client.post("/model/probe")
    assert resp.status_code == 200
    assert resp.json() == {
        "local": [
            {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "models": ["llava"],
                "ok": True,
            }
        ]
    }


def test_probe_uses_config_ports(client: Any, cfg: Config) -> None:
    """扫描器端口来自 cfg.local_probe_ports(绝不扫全端口段)。"""
    client.post("/model/probe")
    assert FakeScanner.last is not None
    assert FakeScanner.last.ports == list(cfg.local_probe_ports)


def test_probe_scanner_unavailable_503(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A143 未就位 → 503 中文(兄弟缺席不崩服务)。"""

    def _broken() -> Any:
        raise RuntimeError(
            "本地探测依赖 netsentinel.vision.local_probe(A143),当前不可用:No module named"
        )

    monkeypatch.setattr(model_api, "_get_scanner_cls", _broken)
    app = fastapi.FastAPI()
    app.include_router(model_api.create_model_router(cfg))
    resp = TestClient(app).post("/model/probe")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "A143" in detail and "不可用" in detail


def test_probe_scan_error_500(client: Any) -> None:
    """扫描过程异常 → 500 中文收敛。"""
    FakeScanner.error = RuntimeError("端口探测超时")
    resp = client.post("/model/probe")
    assert resp.status_code == 500
    assert "本地探测失败" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /model/test
# ---------------------------------------------------------------------------
def test_test_connection_ok_passthrough(client: Any, cfg: Config) -> None:
    """A147 结果原样透传(不增删字段)。"""
    resp = client.post("/model/test", json={"spec": "ollama:llava"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "latency_ms": 42.0, "model": "ollama:llava"}
    assert FakeConnectivity.calls == [("ollama:llava", str(cfg.model_runtime_path))]


def test_test_connection_error_in_body_not_raised(client: Any) -> None:
    """A147 契约:异常转中文 error 字段(200 返回,不抛 5xx)。"""
    FakeConnectivity.fail_specs = {"openai:gpt-4o"}
    resp = client.post("/model/test", json={"spec": "openai:gpt-4o"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "连通性测试失败" in body["error"]


def test_test_connection_missing_spec_422(client: Any) -> None:
    resp = client.post("/model/test", json={})
    assert resp.status_code == 422
    assert "必须含字符串字段 spec" in resp.json()["detail"]


def test_manager_unavailable_503(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A144 未就位 → active/switch 均 503 中文。"""

    def _broken() -> Any:
        raise RuntimeError(
            "模型管理依赖 netsentinel.vision.model_manager(A144),当前不可用:No module named"
        )

    monkeypatch.setattr(model_api, "_get_manager_cls", _broken)
    app = fastapi.FastAPI()
    app.include_router(model_api.create_model_router(cfg))
    client = TestClient(app)
    assert client.get("/model/active").status_code == 503
    resp = client.post("/model/switch", json={"spec": "ollama:llava"})
    assert resp.status_code == 503
    assert "A144" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 红线 33 专项:无写密钥端点 + 响应无密钥本体
# ---------------------------------------------------------------------------
def test_routes_exactly_four_and_no_key_paths(fakes: Config) -> None:
    """路由表恰好四个端点;任何路径不含 key 字样(不提供写密钥端点)。"""
    router = model_api.create_model_router(fakes)
    assert isinstance(router, fastapi.APIRouter)
    paths = {r.path for r in router.routes}
    assert paths == {"/model/active", "/model/switch", "/model/probe", "/model/test"}
    assert all("key" not in p.lower() for p in paths)


def test_source_has_no_key_write_identifiers() -> None:
    """源码级断言:不出现写密钥相关标识(红线 33:密钥仅向导/CLI 通道)。"""
    src = inspect.getsource(model_api).lower()
    for token in ("setkey", "set_key", "api_key", "apikey", "password"):
        assert token not in src, f"源码不应出现密钥写入标识:{token}"


def test_responses_contain_no_key_material(client: Any) -> None:
    """全部端点响应合起来也不含 key 字样/密钥前缀(红线 33)。"""
    client.post("/model/switch", json={"spec": "ollama:llava"})
    combined = json.dumps(
        [
            client.get("/model/active").json(),
            client.post("/model/probe").json(),
            client.post("/model/test", json={"spec": "ollama:llava"}).json(),
        ],
        ensure_ascii=False,
    ).lower()
    assert "key" not in combined
    assert "sk-" not in combined
    assert "password" not in combined


def test_fastapi_import_is_lazy() -> None:
    """模块顶层不 import fastapi(AST 断言,可选依赖惰性导入纪律)。"""
    tree = ast.parse(Path(model_api.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("fastapi") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("fastapi")
