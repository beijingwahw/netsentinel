"""A155:netsentinel.pipeline.takeover 单元测试(全离线,只注入 fake / monkeypatch)。

并行态确定性:兄弟模块 A143(local_probe)/ A144(model_manager)/
A150(capability)/ A149(setup.trigger)可能就位也可能未就位,autouse 夹具
把四者一律经 ``sys.modules[name] = None`` 强制缺席(该技巧使 importlib 对其
抛 ImportError),需要真实行为的用例再按需注入 fake 模块覆盖——两种并行态
下结论一致。providers / keys / logging_util 为冻结的既有模块,使用真实实现。

覆盖(契约 §3 A155 + 协调任务清单):

- disabled(takeover_auto=False)/ 会话幂等 already / kept 沿用不重探;
- local 命中:spec 由扫描条目 provider:首个视觉模型 构成(端口→提供方映射
  同 A143)、set_active(switched_by="takeover", validate=False)——注入
  tester 断言**永不被调**(红线 32:扫描已证实存在,不做二次连通测试);
- cloud:keys.configured 任一 True → 取首家(目录顺序)set_active(提供方名
  = 目录默认模型);keys 缺失 → wizard;
- wizard:url 透传 / None 透传 / ensure 异常容错;扫描全失败(ok=False)落 wizard;
- 兄弟缺失(A144/A143/A149)→ error 不抛;set_active / get_active 异常容错;
- 审计:每决策路径一行 log_event("takeover", action=..., spec=...) 落
  cfg.audit_path(tmp path);审计写失败不阻断;
- 遥测:telemetry.inc(f"takeover.{action}") 各路径恰一次;already 零计数;
- 缺省惰性接线:manager/scanner/ensure 三缝的缺省模块加载路径。
"""
from __future__ import annotations

import json
import pathlib
import sys
import types
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.pipeline import takeover
from netsentinel.security import keys

#: 被测模块惰性依赖的 V8 兄弟模块(autouse 强制缺席,用例按需注入 fake)
SIBLINGS = (
    "netsentinel.vision.model_manager",
    "netsentinel.vision.local_probe",
    "netsentinel.vision.capability",
    "netsentinel.setup.trigger",
)

#: fake 向导地址
URL = "http://127.0.0.1:8766/"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:会话幂等标记清零 + 兄弟模块强制缺席 + 遥测清零。"""
    monkeypatch.setattr(takeover, "_session_done", False)
    for name in SIBLINGS:
        monkeypatch.setitem(sys.modules, name, None)
    telemetry.reset()


def make_cfg(tmp_path: pathlib.Path, **overrides: Any) -> Config:
    """构造指向 tmp 目录的配置(runtime/audit 均落在临时区,绝不碰 data/)。"""
    kw: dict[str, Any] = {
        "model_runtime_path": str(tmp_path / "model_runtime.json"),
        "audit_path": str(tmp_path / "audit.jsonl"),
    }
    kw.update(overrides)
    return Config(**kw)


def stub_keys(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bool]) -> None:
    """把 keys.configured 替换为受控布尔表(避免宿主机密钥环境变量污染)。"""
    monkeypatch.setattr(keys, "configured", lambda cfg: dict(mapping))


def install_capability(monkeypatch: pytest.MonkeyPatch,
                       markers: tuple[str, ...] = ("llava", "qwen2.5vl")) -> None:
    """注入 fake A150 模块:名字含任一标记才判视觉(严格过滤,便于反例)。"""
    mod = types.ModuleType("netsentinel.vision.capability")
    mod.is_vision_model = lambda name: any(m in str(name).lower() for m in markers)
    monkeypatch.setitem(sys.modules, "netsentinel.vision.capability", mod)


class FakeManager:
    """伪 A144 管理器:记录 set_active 调用参数,可注入读/写异常。"""

    def __init__(self, active: str | None = None,
                 exc_on_set: Exception | None = None,
                 exc_on_get: Exception | None = None) -> None:
        self.active = active
        self.exc_on_set = exc_on_set
        self.exc_on_get = exc_on_get
        self.set_calls: list[dict[str, Any]] = []

    def get_active(self) -> str | None:
        if self.exc_on_get is not None:
            raise self.exc_on_get
        return self.active

    def set_active(self, spec: str, *, switched_by: str = "manual",
                   validate: bool = True, tester: Any = None) -> dict[str, Any]:
        self.set_calls.append(
            {"spec": spec, "switched_by": switched_by, "validate": validate}
        )
        if self.exc_on_set is not None:
            raise self.exc_on_set
        self.active = spec
        return {"spec": spec, "switched_by": switched_by}


class FakeScanner:
    """伪本地扫描器:返回固定结果,或按指令抛错(A143 条目形态)。"""

    def __init__(self, results: list[dict[str, Any]] | None = None,
                 exc: Exception | None = None) -> None:
        self.results = results or []
        self.exc = exc
        self.scan_calls = 0

    def scan(self) -> list[dict[str, Any]]:
        self.scan_calls += 1
        if self.exc is not None:
            raise self.exc
        return self.results


class FakeTester:
    """伪连通性测试器:只记录调用(本模块策略下必须零调用)。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, spec: str, cfg: Any) -> dict[str, Any]:
        self.calls.append(spec)
        return {"ok": True, "latency_ms": 1, "model": spec}


class FakeEnsure:
    """伪 A149 触发器:记录 cfg,返回固定地址 / None,或按指令抛错。"""

    def __init__(self, url: str | None = URL, exc: Exception | None = None) -> None:
        self.url = url
        self.exc = exc
        self.calls: list[Config] = []

    def __call__(self, cfg: Config) -> str | None:
        self.calls.append(cfg)
        if self.exc is not None:
            raise self.exc
        return self.url


def ollama_entry(models: list[str], *, ok: bool = True) -> dict[str, Any]:
    """构造 A143 形态的 ollama 扫描条目。"""
    return {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "models": models,
        "ok": ok,
    }


def read_audit(tmp_path: pathlib.Path) -> list[dict[str, Any]]:
    """读取 cfg.audit_path 的全部审计行(json 逐行)。"""
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def counters() -> dict[str, float]:
    return telemetry.snapshot()["counters"]


# ---------------------------------------------------------------------------
# 0) 配置关闭 disabled
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_disabled_returns_action_and_chinese_detail(self, tmp_path, monkeypatch):
        """takeover_auto=False → disabled(含"配置关闭"),零探测零切换。"""
        stub_keys(monkeypatch, {})
        manager = FakeManager()
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        result = takeover.takeover_once(
            make_cfg(tmp_path, takeover_auto=False), scanner=scanner, manager=manager
        )
        assert result["action"] == "disabled"
        assert "配置关闭" in result["detail"]
        assert scanner.scan_calls == 0
        assert manager.set_calls == []
        assert counters()["takeover.disabled"] == 1

    def test_disabled_writes_audit_without_spec(self, tmp_path, monkeypatch):
        """disabled 也是一条决策路径:审计一行,无 spec 字段。"""
        stub_keys(monkeypatch, {})
        takeover.takeover_once(
            make_cfg(tmp_path, takeover_auto=False),
            scanner=FakeScanner(), manager=FakeManager(),
        )
        lines = read_audit(tmp_path)
        assert len(lines) == 1
        assert lines[0]["event"] == "takeover"
        assert lines[0]["action"] == "disabled"
        assert "spec" not in lines[0]

    def test_disabled_does_not_consume_session_quota(self, tmp_path, monkeypatch):
        """disabled 不置会话标记:同一进程随后开启配置可正常接管。"""
        stub_keys(monkeypatch, {})
        takeover.takeover_once(
            make_cfg(tmp_path, takeover_auto=False),
            scanner=FakeScanner(), manager=FakeManager(),
        )
        install_capability(monkeypatch)
        manager = FakeManager()
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        result = takeover.takeover_once(
            make_cfg(tmp_path), scanner=scanner, manager=manager
        )
        assert result["action"] == "local"


# ---------------------------------------------------------------------------
# 会话幂等 already
# ---------------------------------------------------------------------------


class TestSessionIdempotent:
    def test_second_call_already_zero_side_effects(self, tmp_path, monkeypatch):
        """二次调用 → already(含"本会话已完成接管"):不重扫、不再写审计、不重复计数。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        cfg = make_cfg(tmp_path)
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        manager = FakeManager()
        first = takeover.takeover_once(cfg, scanner=scanner, manager=manager)
        assert first["action"] == "local"
        second = takeover.takeover_once(cfg, scanner=scanner, manager=manager)
        assert second["action"] == "already"
        assert "本会话已完成接管" in second["detail"]
        assert scanner.scan_calls == 1
        assert len(manager.set_calls) == 1
        assert len(read_audit(tmp_path)) == 1
        assert counters()["takeover.local"] == 1
        assert "takeover.already" not in counters()

    def test_idempotent_after_kept(self, tmp_path, monkeypatch):
        """kept 也占用会话名额:二次调用 already。"""
        stub_keys(monkeypatch, {})
        manager = FakeManager(active="glm:glm-4.5v")
        scanner = FakeScanner()
        cfg = make_cfg(tmp_path)
        assert takeover.takeover_once(cfg, scanner=scanner, manager=manager)["action"] == "kept"
        assert takeover.takeover_once(cfg, scanner=scanner, manager=manager)["action"] == "already"

    def test_idempotent_after_wizard(self, tmp_path, monkeypatch):
        """wizard 也占用会话名额:ensure 只被调一次。"""
        stub_keys(monkeypatch, {})
        ensure = FakeEnsure()
        scanner = FakeScanner([])
        cfg = make_cfg(tmp_path)
        assert takeover.takeover_once(
            cfg, scanner=scanner, manager=FakeManager(), ensure=ensure
        )["action"] == "wizard"
        assert takeover.takeover_once(
            cfg, scanner=scanner, manager=FakeManager(), ensure=ensure
        )["action"] == "already"
        assert len(ensure.calls) == 1

    def test_error_does_not_consume_session_quota(self, tmp_path, monkeypatch):
        """error 不置会话标记:兄弟随后就位(注入)即可重试成功。"""
        stub_keys(monkeypatch, {})
        cfg = make_cfg(tmp_path)
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        # 第一次:manager 兄弟缺席(未注入且模块被强制缺席)→ error
        assert takeover.takeover_once(cfg, scanner=scanner)["action"] == "error"
        # 第二次:注入 manager(兄弟"落地")→ 正常接管
        install_capability(monkeypatch)
        result = takeover.takeover_once(cfg, scanner=scanner, manager=FakeManager())
        assert result["action"] == "local"

    def test_reset_session_allows_manual_rerun(self, tmp_path, monkeypatch):
        """reset_session() 清标记后可重跑(手动触发场景)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        cfg = make_cfg(tmp_path)
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        takeover.takeover_once(cfg, scanner=scanner, manager=FakeManager())
        takeover.reset_session()
        result = takeover.takeover_once(cfg, scanner=scanner, manager=FakeManager())
        assert result["action"] == "local"
        assert scanner.scan_calls == 2


# ---------------------------------------------------------------------------
# 1) kept:沿用已连接模型(不重探)
# ---------------------------------------------------------------------------


class TestKept:
    def test_kept_reuses_active_spec_without_reprobe(self, tmp_path, monkeypatch):
        """已有活动 spec → kept 原样返回;不扫描、不问密钥、不起向导。"""
        stub_keys(monkeypatch, {"glm": True})  # 即使密钥已配也不动
        scanner = FakeScanner([ollama_entry(["llava:13b"])])
        ensure = FakeEnsure()
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=scanner,
            manager=FakeManager(active="glm:glm-4.5v"),
            ensure=ensure,
        )
        assert result["action"] == "kept"
        assert result["spec"] == "glm:glm-4.5v"
        assert "沿用已连接模型" in result["detail"]
        assert scanner.scan_calls == 0
        assert ensure.calls == []
        assert counters()["takeover.kept"] == 1

    def test_kept_blank_active_spec_falls_through_to_local(self, tmp_path, monkeypatch):
        """空白 spec 视为未连接:继续走本地扫描一级。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=FakeManager(active="   "),
        )
        assert result["action"] == "local"
        assert result["spec"] == "ollama:llava:13b"

    def test_kept_writes_audit_with_spec(self, tmp_path, monkeypatch):
        """kept 审计行含 action=kept 与 spec。"""
        stub_keys(monkeypatch, {})
        takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner(), manager=FakeManager(active="ollama:llava"),
        )
        lines = read_audit(tmp_path)
        assert len(lines) == 1
        assert lines[0]["action"] == "kept"
        assert lines[0]["spec"] == "ollama:llava"


# ---------------------------------------------------------------------------
# 2) local:本地扫描命中
# ---------------------------------------------------------------------------


class TestLocal:
    def test_local_spec_from_provider_and_first_vision_model(self, tmp_path, monkeypatch):
        """spec 映射正确:条目 provider + 首个视觉模型(文本模型跳过)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llama3:8b", "llava:13b"])]),
            manager=FakeManager(),
        )
        assert result["action"] == "local"
        assert result["spec"] == "ollama:llava:13b"
        assert "接管" in result["detail"]

    def test_local_set_active_takeover_validate_false(self, tmp_path, monkeypatch):
        """set_active 恰以 switched_by="takeover"、validate=False 调用(红线 32)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        manager = FakeManager()
        takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=manager,
        )
        assert manager.set_calls == [
            {"spec": "ollama:llava:13b", "switched_by": "takeover", "validate": False}
        ]

    def test_local_never_calls_tester(self, tmp_path, monkeypatch):
        """validate=False:注入 tester 断言零调用(不做二次连通测试)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        tester = FakeTester()
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=FakeManager(),
            tester=tester,
        )
        assert result["action"] == "local"
        assert tester.calls == []

    def test_local_skips_non_vision_entry_uses_next_provider(self, tmp_path, monkeypatch):
        """首个 ok 条目只有文本模型 → 取下一 ok 条目(lmstudio 端口映射提供方)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([
                ollama_entry(["llama3:8b"]),
                {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234",
                 "models": ["qwen2.5vl-7b"], "ok": True},
            ]),
            manager=FakeManager(),
        )
        assert result["action"] == "local"
        assert result["spec"] == "lmstudio:qwen2.5vl-7b"

    def test_local_capability_absent_takes_first_model(self, tmp_path, monkeypatch):
        """A150 缺席(autouse 强制)→ "缺席全返回":取首个非空模型。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llama3:8b", "llava:13b"])]),
            manager=FakeManager(),
        )
        assert result["action"] == "local"
        assert result["spec"] == "ollama:llama3:8b"

    def test_local_unknown_provider_entry_skipped(self, tmp_path, monkeypatch):
        """未映射端口的条目(provider="openai 兼容")不在目录中 → 跳过,不报错。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        ensure = FakeEnsure()
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([{
                "provider": "openai 兼容", "base_url": "http://127.0.0.1:9000",
                "models": ["llava:13b"], "ok": True,
            }]),
            manager=FakeManager(),
            ensure=ensure,
        )
        assert result["action"] == "wizard"
        assert result["wizard"] == URL

    def test_local_empty_models_entry_ignored(self, tmp_path, monkeypatch):
        """ok 但模型列表为空 → 不算命中,继续下一级。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry([])]),
            manager=FakeManager(),
            ensure=FakeEnsure(),
        )
        assert result["action"] == "wizard"

    def test_local_scan_raises_degrades_to_cloud(self, tmp_path, monkeypatch):
        """scan() 抛错 → 按"未发现"降级,继续云端一级(不 error)。"""
        stub_keys(monkeypatch, {"glm": True})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner(exc=OSError("连接超时")),
            manager=FakeManager(),
        )
        assert result["action"] == "cloud"
        assert result["spec"] == "glm"

    def test_local_not_ok_entries_fall_to_wizard(self, tmp_path, monkeypatch):
        """扫描全失败(ok=False,即使含 llava)→ 落向导。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"], ok=False)]),
            manager=FakeManager(),
            ensure=FakeEnsure(),
        )
        assert result["action"] == "wizard"
        assert result["wizard"] == URL


# ---------------------------------------------------------------------------
# 3) cloud:云端密钥已配
# ---------------------------------------------------------------------------


class TestCloud:
    def test_cloud_takes_configured_provider_as_spec(self, tmp_path, monkeypatch):
        """任一密钥已配 → 取首家为 spec(仅提供方名=目录默认模型),validate=False。"""
        stub_keys(monkeypatch, {"glm": True, "openai": False})
        manager = FakeManager()
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=manager,
        )
        assert result["action"] == "cloud"
        assert result["spec"] == "glm"
        assert ":" not in result["spec"]  # 只写提供方名,模型由目录默认解析
        assert manager.set_calls == [
            {"spec": "glm", "switched_by": "takeover", "validate": False}
        ]
        assert counters()["takeover.cloud"] == 1

    def test_cloud_first_configured_in_catalog_order_wins(self, tmp_path, monkeypatch):
        """多家已配时按 keys.configured 迭代顺序(目录顺序)取首家,结果确定。"""
        stub_keys(monkeypatch, {"openai": True, "glm": True})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=FakeManager(),
        )
        assert result["action"] == "cloud"
        assert result["spec"] == "openai"

    def test_cloud_keys_missing_falls_to_wizard(self, tmp_path, monkeypatch):
        """keys 缺失(全 False/空表)+ 本地未命中 → wizard。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=FakeManager(),
            ensure=FakeEnsure(),
        )
        assert result["action"] == "wizard"

    def test_cloud_keys_check_raises_degrades_to_wizard(self, tmp_path, monkeypatch):
        """keys.configured 抛错 → 按"未配置"降级(同 A149 口径),落向导。"""
        def _boom(cfg):
            raise RuntimeError("密钥环损坏")

        monkeypatch.setattr(keys, "configured", _boom)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=FakeManager(),
            ensure=FakeEnsure(),
        )
        assert result["action"] == "wizard"

    def test_cloud_writes_audit_with_spec(self, tmp_path, monkeypatch):
        """cloud 审计行含 action=cloud 与 spec。"""
        stub_keys(monkeypatch, {"qwen": True})
        takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=FakeManager(),
        )
        lines = read_audit(tmp_path)
        assert len(lines) == 1
        assert lines[0]["action"] == "cloud"
        assert lines[0]["spec"] == "qwen"


# ---------------------------------------------------------------------------
# 4) wizard:全无 → 连接向导
# ---------------------------------------------------------------------------


class TestWizard:
    def test_wizard_url_passthrough_and_ensure_gets_cfg(self, tmp_path, monkeypatch):
        """向导 url 原样透传;ensure 收到的就是传入的 cfg。"""
        stub_keys(monkeypatch, {})
        ensure = FakeEnsure()
        cfg = make_cfg(tmp_path)
        result = takeover.takeover_once(cfg, scanner=FakeScanner([]),
                                        manager=FakeManager(), ensure=ensure)
        assert result["action"] == "wizard"
        assert result["wizard"] == URL
        assert "向导" in result["detail"]
        assert URL in result["detail"]
        assert ensure.calls == [cfg]
        assert counters()["takeover.wizard"] == 1

    def test_wizard_none_url_still_wizard_action(self, tmp_path, monkeypatch):
        """ensure 返回 None(未启动)→ wizard 字段 None,给出手动入口中文详情。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]), manager=FakeManager(),
            ensure=FakeEnsure(url=None),
        )
        assert result["action"] == "wizard"
        assert result["wizard"] is None
        assert "modelmgr" in result["detail"]

    def test_wizard_ensure_raises_tolerated(self, tmp_path, monkeypatch):
        """ensure 抛错 → 容错为 wizard(地址 None),不抛出。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]), manager=FakeManager(),
            ensure=FakeEnsure(exc=RuntimeError("端口被占用")),
        )
        assert result["action"] == "wizard"
        assert result["wizard"] is None


# ---------------------------------------------------------------------------
# 5) 兄弟缺失 / 异常 → error,绝不抛出
# ---------------------------------------------------------------------------


class TestSiblingMissingAndErrors:
    def test_manager_sibling_absent_error_no_raise(self, tmp_path, monkeypatch):
        """A144 缺席(未注入且模块强制缺席)→ error(含 A144 中文说明),不抛。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path), scanner=FakeScanner([ollama_entry(["llava:13b"])])
        )
        assert result["action"] == "error"
        assert "A144" in result["detail"]
        assert counters()["takeover.error"] == 1

    def test_scanner_sibling_absent_error_even_with_keys(self, tmp_path, monkeypatch):
        """A143 缺席 → error(即使云端密钥已配也不降级跳过本地探测)。"""
        stub_keys(monkeypatch, {"glm": True})
        result = takeover.takeover_once(
            make_cfg(tmp_path), manager=FakeManager()
        )
        assert result["action"] == "error"
        assert "A143" in result["detail"]

    def test_ensure_sibling_absent_error(self, tmp_path, monkeypatch):
        """A149 缺席(走到向导一级)→ error(含 A149 中文说明),不抛。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]), manager=FakeManager(),
        )
        assert result["action"] == "error"
        assert "A149" in result["detail"]

    def test_set_active_exception_tolerated_as_error(self, tmp_path, monkeypatch):
        """manager.set_active 抛错 → 容错为 error(含"设置活动模型失败"),不抛。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=FakeManager(exc_on_set=ValueError("模型规格校验异常")),
        )
        assert result["action"] == "error"
        assert "设置活动模型失败" in result["detail"]

    def test_get_active_exception_tolerated_as_error(self, tmp_path, monkeypatch):
        """manager.get_active 抛错 → 容错为 error,不抛。"""
        stub_keys(monkeypatch, {})
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([]),
            manager=FakeManager(exc_on_get=RuntimeError("状态文件被锁")),
        )
        assert result["action"] == "error"

    def test_error_writes_audit_with_detail(self, tmp_path, monkeypatch):
        """error 路径也留审计痕迹(含中文 detail 便于排障)。"""
        stub_keys(monkeypatch, {})
        takeover.takeover_once(
            make_cfg(tmp_path), scanner=FakeScanner([]), manager=FakeManager()
        )
        lines = read_audit(tmp_path)
        assert len(lines) == 1
        assert lines[0]["action"] == "error"
        assert "A149" in lines[0]["detail"]


# ---------------------------------------------------------------------------
# 6) 审计与遥测基建
# ---------------------------------------------------------------------------


class TestAuditAndTelemetry:
    def test_audit_written_to_tmp_path_local(self, tmp_path, monkeypatch):
        """本地命中 → cfg.audit_path(tmp)落一行 event=takeover/action/spec。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=FakeManager(),
        )
        lines = read_audit(tmp_path)
        assert len(lines) == 1
        assert lines[0]["event"] == "takeover"
        assert lines[0]["action"] == "local"
        assert lines[0]["spec"] == "ollama:llava:13b"

    def test_audit_failure_tolerated(self, tmp_path, monkeypatch):
        """审计器 log_event 抛错 → 只告警,接管结果不受影响。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)

        class _BrokenAudit:
            def log_event(self, event: str, **fields: Any) -> None:
                raise OSError("磁盘已满")

        monkeypatch.setattr(takeover, "_load_audit_logger", lambda cfg: _BrokenAudit())
        result = takeover.takeover_once(
            make_cfg(tmp_path),
            scanner=FakeScanner([ollama_entry(["llava:13b"])]),
            manager=FakeManager(),
        )
        assert result["action"] == "local"
        assert counters()["takeover.local"] == 1  # 遥测照常

    def test_telemetry_counts_each_action_exactly_once(self, tmp_path, monkeypatch):
        """五种决策路径各计数恰一次;already 不产生任何 takeover.* 计数。"""
        stub_keys(monkeypatch, {"glm": True})
        hit_scanner = FakeScanner([ollama_entry(["llava:13b"])])
        miss_scanner = FakeScanner([])

        # disabled
        takeover.takeover_once(make_cfg(tmp_path, takeover_auto=False),
                               scanner=miss_scanner, manager=FakeManager())
        # kept
        takeover.reset_session()
        takeover.takeover_once(make_cfg(tmp_path), scanner=miss_scanner,
                               manager=FakeManager(active="glm:glm-4.5v"))
        # local
        takeover.reset_session()
        install_capability(monkeypatch)
        takeover.takeover_once(make_cfg(tmp_path), scanner=hit_scanner,
                               manager=FakeManager())
        # cloud
        takeover.reset_session()
        takeover.takeover_once(make_cfg(tmp_path), scanner=miss_scanner,
                               manager=FakeManager())
        # wizard
        takeover.reset_session()
        stub_keys(monkeypatch, {})
        takeover.takeover_once(make_cfg(tmp_path), scanner=miss_scanner,
                               manager=FakeManager(), ensure=FakeEnsure())
        # already(不计数)
        takeover.takeover_once(make_cfg(tmp_path), scanner=miss_scanner,
                               manager=FakeManager(), ensure=FakeEnsure())

        snap = counters()
        assert snap["takeover.disabled"] == 1
        assert snap["takeover.kept"] == 1
        assert snap["takeover.local"] == 1
        assert snap["takeover.cloud"] == 1
        assert snap["takeover.wizard"] == 1
        assert "takeover.already" not in snap


# ---------------------------------------------------------------------------
# 7) 缺省惰性接线(不注入时的模块加载路径)
# ---------------------------------------------------------------------------


class TestLazyDefaultWiring:
    def test_default_manager_lazy_from_model_manager_module(self, tmp_path, monkeypatch):
        """未注入 manager → 惰性加载 A144 模块并按 cfg.model_runtime_path 构造。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        mod = types.ModuleType("netsentinel.vision.model_manager")
        constructed: list[str] = []
        set_calls: list[dict[str, Any]] = []

        class _FakeRuntimeManager:
            def __init__(self, path: str) -> None:
                constructed.append(str(path))

            def get_active(self) -> None:
                return None

            def set_active(self, spec: str, *, switched_by: str = "manual",
                           validate: bool = True, tester: Any = None) -> dict[str, Any]:
                set_calls.append({"spec": spec, "switched_by": switched_by,
                                  "validate": validate})
                return {"spec": spec}

        mod.ModelManager = _FakeRuntimeManager
        monkeypatch.setitem(sys.modules, "netsentinel.vision.model_manager", mod)

        cfg = make_cfg(tmp_path)
        result = takeover.takeover_once(cfg, scanner=FakeScanner([ollama_entry(["llava:13b"])]))
        assert result["action"] == "local"
        assert constructed == [cfg.model_runtime_path]
        assert set_calls[0]["spec"] == "ollama:llava:13b"

    def test_default_scanner_lazy_with_cfg_ports(self, tmp_path, monkeypatch):
        """未注入 scanner → 惰性构造 A143 LocalVisionScanner(cfg.local_probe_ports)。"""
        stub_keys(monkeypatch, {})
        install_capability(monkeypatch)
        mod = types.ModuleType("netsentinel.vision.local_probe")
        constructed: list[list[str]] = []

        class _FakeLocalScanner:
            def __init__(self, ports: list[str], timeout: float = 1.5) -> None:
                constructed.append(list(ports))

            def scan(self) -> list[dict[str, Any]]:
                return [{"provider": "ollama", "ok": True, "models": ["llava:13b"]}]

        mod.LocalVisionScanner = _FakeLocalScanner
        monkeypatch.setitem(sys.modules, "netsentinel.vision.local_probe", mod)

        result = takeover.takeover_once(
            make_cfg(tmp_path, local_probe_ports=["11434", "1234"]),
            manager=FakeManager(),
        )
        assert result["action"] == "local"
        assert constructed == [["11434", "1234"]]

    def test_default_ensure_lazy_from_trigger_module(self, tmp_path, monkeypatch):
        """未注入 ensure → 惰性调 A149 ensure_setup(cfg) 并透传地址。"""
        stub_keys(monkeypatch, {})
        mod = types.ModuleType("netsentinel.setup.trigger")
        calls: list[Config] = []

        def _ensure_setup(cfg: Config, *, open_browser: bool | None = None,
                          server_factory: Any = None) -> str:
            calls.append(cfg)
            return URL

        mod.ensure_setup = _ensure_setup
        monkeypatch.setitem(sys.modules, "netsentinel.setup.trigger", mod)

        cfg = make_cfg(tmp_path)
        result = takeover.takeover_once(cfg, scanner=FakeScanner([]), manager=FakeManager())
        assert result["action"] == "wizard"
        assert result["wizard"] == URL
        assert calls == [cfg]
