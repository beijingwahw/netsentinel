"""A149:netsentinel.setup.trigger 单元测试(离线,只注入 fake / monkeypatch)。

并行态确定性:兄弟模块 A143/A144/A145/A150/A154 可能就位也可能未就位,
autouse 夹具把五者一律经 ``sys.modules[name] = None`` 强制缺席
(该技巧使 importlib 对其抛 ImportError),需要真实行为的用例再按需注入
fake 模块覆盖——两种并行态下结论一致。

- has_any_model:三来源各有模型即 True(注入 fake scanner / manager、
  monkeypatch keys.configured 与环境变量);
- ensure_setup:全无才启动(注入 fake factory 记录);open_browser 三态
  优先级(参数 > 环境变量 NETSENTINEL_NO_BROWSER > 配置);webbrowser.open
  被调 / 不调;返回地址;幂等(二次调用复用,factory 计数=1);中文提示
  capsys;telemetry 计数;缺省工厂走 A154 单例 / 兄弟全缺席抛中文错误。
"""
from __future__ import annotations

import pathlib
import sys
import types
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys
from netsentinel.setup import trigger

#: 被测模块依赖的全部兄弟模块(autouse 强制缺席,用例按需注入 fake)
SIBLINGS = (
    "netsentinel.vision.model_manager",
    "netsentinel.vision.local_probe",
    "netsentinel.vision.capability",
    "netsentinel.setup.daemon",
    "netsentinel.setup.server",
)

#: fake 向导地址
URL = "http://127.0.0.1:8766/"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:向导单例清零 + 兄弟模块强制缺席 + 遥测清零。"""
    monkeypatch.setattr(trigger, "_started_url", None)
    for name in SIBLINGS:
        monkeypatch.setitem(sys.modules, name, None)
    telemetry.reset()


def make_cfg(tmp_path: pathlib.Path, **overrides: Any) -> Config:
    """构造指向 tmp 目录的配置(model_runtime.json 落在临时区)。"""
    kw: dict[str, Any] = {"model_runtime_path": str(tmp_path / "model_runtime.json")}
    kw.update(overrides)
    return Config(**kw)


def stub_keys(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bool]) -> None:
    """把 keys.configured 替换为受控布尔表(避免宿主机环境变量污染)。"""
    monkeypatch.setattr(keys, "configured", lambda cfg: dict(mapping))


class FakeFactory:
    """记录调用并返回固定地址的伪服务器工厂。"""

    def __init__(self, url: str | None = URL) -> None:
        self.url = url
        self.calls: list[Config] = []

    def __call__(self, cfg: Config) -> str | None:
        self.calls.append(cfg)
        return self.url


class FakeScanner:
    """伪本地扫描器:返回固定结果,或按指令抛错。"""

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


class FakeBrowser:
    """替换 webbrowser.open:记录地址,可控制返回值 / 抛错。"""

    def __init__(self, retval: bool = True, exc: Exception | None = None) -> None:
        self.urls: list[str] = []
        self.retval = retval
        self.exc = exc

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.retval

    @property
    def called(self) -> bool:
        return bool(self.urls)


def install_manager(monkeypatch: pytest.MonkeyPatch, active: str | None,
                    exc: Exception | None = None) -> None:
    """注入 fake A144 模块:ModelManager(path).get_active() 返回受控 spec。"""
    mod = types.ModuleType("netsentinel.vision.model_manager")

    class _FakeManager:
        def __init__(self, path: str) -> None:
            self.path = path

        def get_active(self) -> str | None:
            if exc is not None:
                raise exc
            return active

    mod.ModelManager = _FakeManager
    monkeypatch.setitem(sys.modules, "netsentinel.vision.model_manager", mod)


def install_capability(monkeypatch: pytest.MonkeyPatch,
                       markers: tuple[str, ...] = ("llava", "qwen2.5vl")) -> None:
    """注入 fake A150 模块:名字含任一标记才判视觉(严格过滤,便于反例)。"""
    mod = types.ModuleType("netsentinel.vision.capability")
    mod.is_vision_model = lambda name: any(m in str(name) for m in markers)
    monkeypatch.setitem(sys.modules, "netsentinel.vision.capability", mod)


def install_local_probe(monkeypatch: pytest.MonkeyPatch,
                        results: list[dict[str, Any]]) -> list[list[str]]:
    """注入 fake A143 模块:LocalVisionScanner(ports) 记录构造参数。"""
    mod = types.ModuleType("netsentinel.vision.local_probe")
    constructed: list[list[str]] = []

    class _FakeLocalScanner:
        def __init__(self, ports: list[str], timeout: float = 1.5) -> None:
            constructed.append(list(ports))

        def scan(self) -> list[dict[str, Any]]:
            return results

    mod.LocalVisionScanner = _FakeLocalScanner
    monkeypatch.setitem(sys.modules, "netsentinel.vision.local_probe", mod)
    return constructed


def install_daemon(monkeypatch: pytest.MonkeyPatch, url: str | None = URL) -> list[tuple]:
    """注入 fake A154 模块:ensure_setup_server 记录 (cfg, open_browser) 调用。"""
    mod = types.ModuleType("netsentinel.setup.daemon")
    calls: list[tuple] = []

    def _ensure_setup_server(cfg: Config, *, open_browser: bool | None = None):
        calls.append((cfg, open_browser))
        return url

    mod.ensure_setup_server = _ensure_setup_server
    monkeypatch.setitem(sys.modules, "netsentinel.setup.daemon", mod)
    return calls


def clear_all_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空内置 20 家约定两形态密钥环境变量,防宿主机污染。"""
    for provider in keys.BUILTIN_PROVIDERS:
        upper = provider.upper()
        for name in (f"NETSENTINEL_{upper}_API_KEY", f"{upper}_API_KEY"):
            monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# has_any_model:三来源
# ---------------------------------------------------------------------------


class TestHasAnyModel:
    def test_all_sources_empty_returns_false(self, tmp_path, monkeypatch):
        """三来源全空(无运行时/无密钥/A143 缺席)→ False。"""
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is False

    def test_active_runtime_spec_present_true(self, tmp_path, monkeypatch):
        """来源1:活动 spec 非空 → True(优先短路,无需密钥/扫描)。"""
        install_manager(monkeypatch, "ollama:llava")
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is True

    def test_active_runtime_spec_empty_string_false(self, tmp_path, monkeypatch):
        """来源1:空串 spec 视为无,继续看后续来源。"""
        install_manager(monkeypatch, "")
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is False

    def test_active_runtime_spec_none_false(self, tmp_path, monkeypatch):
        """来源1:get_active() 返回 None → 无活动模型。"""
        install_manager(monkeypatch, None)
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is False

    def test_active_runtime_manager_error_degrades_false(self, tmp_path, monkeypatch):
        """来源1:A144 读运行时抛错 → 降级为无,不向上抛。"""
        install_manager(monkeypatch, "ollama:llava", exc=RuntimeError("损坏的 json"))
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is False

    def test_any_configured_key_true(self, tmp_path, monkeypatch):
        """来源2:任一云密钥已配(keys.configured 任一 True)→ True。"""
        stub_keys(monkeypatch, {"glm": True, "openai": False, "qwen": False})
        assert trigger.has_any_model(make_cfg(tmp_path)) is True

    def test_real_env_key_source_true(self, tmp_path, monkeypatch):
        """来源2(真实 keys.configured 路径):清空全部密钥环境变量后仅设
        NETSENTINEL_QWEN_API_KEY → True;家目录指向 tmp 防密钥文件污染。"""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
        monkeypatch.setattr(keys, "_load_catalog", lambda: None)
        clear_all_provider_envs(monkeypatch)
        monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-test-12345678")
        assert trigger.has_any_model(make_cfg(tmp_path)) is True

    def test_injected_scanner_vision_model_true(self, tmp_path, monkeypatch):
        """来源3:ok 条目含视觉模型(A150 严格判定 llava)→ True。"""
        install_capability(monkeypatch)
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "base_url": "http://127.0.0.1:11434",
                                "models": ["llava:13b"], "ok": True}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is True

    def test_injected_scanner_non_vision_model_false(self, tmp_path, monkeypatch):
        """来源3:ok 但只有文本模型(llama3,严格 A150 判非视觉)→ False。"""
        install_capability(monkeypatch)
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "base_url": "http://127.0.0.1:11434",
                                "models": ["llama3:8b"], "ok": True}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is False

    def test_injected_scanner_not_ok_entry_ignored(self, tmp_path, monkeypatch):
        """来源3:ok=False 的条目(服务未就绪)即使有 llava 也不算。"""
        install_capability(monkeypatch)
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "models": ["llava:13b"], "ok": False}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is False

    def test_injected_scanner_empty_models_false(self, tmp_path, monkeypatch):
        """来源3:ok 但模型列表为空 → False。"""
        install_capability(monkeypatch)
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "models": [], "ok": True}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is False

    def test_injected_scanner_scan_raises_false(self, tmp_path, monkeypatch):
        """来源3:scan() 抛错 → 降级为未发现,不向上抛。"""
        stub_keys(monkeypatch, {})
        scanner = FakeScanner(exc=OSError("连接超时"))
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is False

    def test_injected_scanner_dict_models_supported(self, tmp_path, monkeypatch):
        """来源3健壮性:模型为 {"id": ...} 字典形态同样识别。"""
        install_capability(monkeypatch)
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "ok": True, "models": [{"id": "llava:7b"}]}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is True

    def test_default_scanner_module_absent_false(self, tmp_path, monkeypatch):
        """来源3缺省路径:A143 缺席(autouse 强制)→ 跳过本地扫描,不抛。"""
        stub_keys(monkeypatch, {})
        assert trigger.has_any_model(make_cfg(tmp_path)) is False

    def test_default_scanner_lazy_constructed_with_cfg_ports(self, tmp_path, monkeypatch):
        """来源3缺省路径:A143 就位时按 cfg.local_probe_ports 惰性构造并扫描。"""
        constructed = install_local_probe(monkeypatch, [
            {"provider": "ollama", "ok": True, "models": ["llava:13b"]}])
        stub_keys(monkeypatch, {})
        cfg = make_cfg(tmp_path, local_probe_ports=["11434", "1234"])
        assert trigger.has_any_model(cfg) is True
        assert constructed == [["11434", "1234"]]

    def test_capability_absent_degrades_to_any_model(self, tmp_path, monkeypatch):
        """来源3降级:A150 缺席时按 A143 口径"缺席全返回"——任意非空模型
        名都算候选(宁可不误扰用户)。"""
        stub_keys(monkeypatch, {})
        scanner = FakeScanner([{"provider": "ollama", "ok": True, "models": ["some-text-model"]}])
        assert trigger.has_any_model(make_cfg(tmp_path), scanner=scanner) is True


# ---------------------------------------------------------------------------
# ensure_setup:不打扰 / 启动 / 弹窗三态 / 幂等 / 遥测 / 缺省工厂
# ---------------------------------------------------------------------------


class TestEnsureSetupNoDisturb:
    def test_returns_none_when_key_configured(self, tmp_path, monkeypatch):
        """密钥已配 → None,不起服务、不弹浏览器。"""
        stub_keys(monkeypatch, {"glm": True})
        factory = FakeFactory()
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        assert trigger.ensure_setup(make_cfg(tmp_path), server_factory=factory) is None
        assert factory.calls == []
        assert browser.called is False

    def test_returns_none_when_active_spec(self, tmp_path, monkeypatch):
        """活动模型存在 → None,零打扰。"""
        install_manager(monkeypatch, "ollama:llava")
        stub_keys(monkeypatch, {})
        factory = FakeFactory()
        assert trigger.ensure_setup(make_cfg(tmp_path), server_factory=factory) is None
        assert factory.calls == []

    def test_returns_none_when_local_vision_found(self, tmp_path, monkeypatch):
        """本地扫描命中视觉模型(缺省扫描路径)→ None,零打扰。"""
        install_capability(monkeypatch)
        install_local_probe(monkeypatch, [
            {"provider": "ollama", "ok": True, "models": ["llava:13b"]}])
        stub_keys(monkeypatch, {})
        factory = FakeFactory()
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        assert trigger.ensure_setup(make_cfg(tmp_path), server_factory=factory) is None
        assert factory.calls == []
        assert browser.called is False


class TestEnsureSetupLaunch:
    def test_launches_returns_url_and_calls_factory(self, tmp_path, monkeypatch):
        """全无模型 → 启动(factory 收到 cfg)、返回地址、打印/弹窗其一。"""
        stub_keys(monkeypatch, {})
        cfg = make_cfg(tmp_path)
        factory = FakeFactory()
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        url = trigger.ensure_setup(cfg, server_factory=factory)
        assert url == URL
        assert factory.calls == [cfg]

    def test_open_default_opens_browser_when_config_and_env_allow(
            self, tmp_path, monkeypatch, capsys):
        """缺省裁决(无参数/无环境变量/配置开)→ webbrowser.open(url),不打印。"""
        stub_keys(monkeypatch, {})
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        trigger.ensure_setup(make_cfg(tmp_path), server_factory=FakeFactory())
        assert browser.urls == [URL]
        assert "连接向导已启动" not in capsys.readouterr().out

    def test_open_false_prints_chinese_hint(self, tmp_path, monkeypatch, capsys):
        """open_browser=False → 不弹;打印中文提示与地址。"""
        stub_keys(monkeypatch, {})
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        url = trigger.ensure_setup(make_cfg(tmp_path), open_browser=False,
                                   server_factory=FakeFactory())
        out = capsys.readouterr().out
        assert browser.called is False
        assert "未检测到视觉模型" in out
        assert "连接向导已启动" in out
        assert url in out

    def test_env_no_browser_overrides_config_true(self, tmp_path, monkeypatch, capsys):
        """优先级:环境变量 NETSENTINEL_NO_BROWSER 压过 onboarding_auto_open=True。"""
        stub_keys(monkeypatch, {})
        monkeypatch.setenv(trigger.NO_BROWSER_ENV, "1")
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        cfg = make_cfg(tmp_path, onboarding_auto_open=True)
        assert trigger.ensure_setup(cfg, server_factory=FakeFactory()) == URL
        assert browser.called is False
        assert "连接向导已启动" in capsys.readouterr().out

    def test_param_false_overrides_config_true(self, tmp_path, monkeypatch, capsys):
        """优先级:参数 False 压过配置 True(无环境变量)。"""
        stub_keys(monkeypatch, {})
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        cfg = make_cfg(tmp_path, onboarding_auto_open=True)
        trigger.ensure_setup(cfg, open_browser=False, server_factory=FakeFactory())
        assert browser.called is False
        assert "连接向导已启动" in capsys.readouterr().out

    def test_param_true_overrides_env_and_config(self, tmp_path, monkeypatch, capsys):
        """优先级:参数 True 同时压过环境变量与配置 False。"""
        stub_keys(monkeypatch, {})
        monkeypatch.setenv(trigger.NO_BROWSER_ENV, "1")
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        cfg = make_cfg(tmp_path, onboarding_auto_open=False)
        assert trigger.ensure_setup(cfg, open_browser=True,
                                    server_factory=FakeFactory()) == URL
        assert browser.urls == [URL]
        assert "连接向导已启动" not in capsys.readouterr().out

    def test_config_false_no_env_param_none(self, tmp_path, monkeypatch, capsys):
        """优先级:无参数无环境变量,onboarding_auto_open=False → 只打印。"""
        stub_keys(monkeypatch, {})
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        cfg = make_cfg(tmp_path, onboarding_auto_open=False)
        assert trigger.ensure_setup(cfg, server_factory=FakeFactory()) == URL
        assert browser.called is False
        assert URL in capsys.readouterr().out

    def test_webbrowser_open_failure_falls_back_to_print(self, tmp_path, monkeypatch, capsys):
        """弹窗失败(webbrowser.open 返回 False)→ 退回打印地址,不抛。"""
        stub_keys(monkeypatch, {})
        monkeypatch.setattr(trigger.webbrowser, "open", FakeBrowser(retval=False))
        assert trigger.ensure_setup(make_cfg(tmp_path), server_factory=FakeFactory()) == URL
        out = capsys.readouterr().out
        assert "未检测到视觉模型" in out and URL in out


class TestEnsureSetupIdempotentAndTelemetry:
    def test_idempotent_second_call_reuses_server(self, tmp_path, monkeypatch, capsys):
        """幂等:二次调用 factory 只调 1 次,复用同一地址,不重复打印/弹窗。"""
        stub_keys(monkeypatch, {})
        factory = FakeFactory()
        browser = FakeBrowser()
        monkeypatch.setattr(trigger.webbrowser, "open", browser)
        cfg = make_cfg(tmp_path)
        first = trigger.ensure_setup(cfg, server_factory=factory)
        capsys.readouterr()
        second = trigger.ensure_setup(cfg, server_factory=factory)
        assert first == second == URL
        assert len(factory.calls) == 1
        assert len(browser.urls) == 1
        assert capsys.readouterr().out == ""

    def test_telemetry_wizard_shown_incremented_once(self, tmp_path, monkeypatch):
        """每次真实首启记 setup.wizard_shown;幂等复用不重复计数。"""
        stub_keys(monkeypatch, {})
        factory = FakeFactory()
        cfg = make_cfg(tmp_path)
        trigger.ensure_setup(cfg, server_factory=factory, open_browser=False)
        trigger.ensure_setup(cfg, server_factory=factory, open_browser=False)
        assert telemetry.snapshot()["counters"].get(trigger.WIZARD_SHOWN_METRIC) == 1

    def test_no_telemetry_when_model_present(self, tmp_path, monkeypatch):
        """不打扰路径零计数。"""
        stub_keys(monkeypatch, {"glm": True})
        trigger.ensure_setup(make_cfg(tmp_path), server_factory=FakeFactory())
        assert trigger.WIZARD_SHOWN_METRIC not in telemetry.snapshot()["counters"]

    def test_factory_returns_none_not_cached(self, tmp_path, monkeypatch):
        """工厂返回空(启动失败)→ 返回 None、不计数、不缓存,下次可重试。"""
        stub_keys(monkeypatch, {})
        factory = FakeFactory(url=None)
        cfg = make_cfg(tmp_path)
        assert trigger.ensure_setup(cfg, server_factory=factory) is None
        assert trigger.ensure_setup(cfg, server_factory=factory) is None
        assert len(factory.calls) == 2
        assert trigger.WIZARD_SHOWN_METRIC not in telemetry.snapshot()["counters"]


class TestEnsureSetupDefaultFactory:
    def test_default_factory_uses_daemon_singleton(self, tmp_path, monkeypatch, capsys):
        """缺省工厂:惰性走 A154 ensure_setup_server 单例,且 open_browser=False
        (浏览器决策收在本模块,防双开)。"""
        stub_keys(monkeypatch, {})
        calls = install_daemon(monkeypatch)
        cfg = make_cfg(tmp_path)
        assert trigger.ensure_setup(cfg, open_browser=False) == URL
        assert calls == [(cfg, False)]
        assert URL in capsys.readouterr().out

    def test_default_factory_absent_raises_chinese(self, tmp_path, monkeypatch):
        """A154/A145 均缺席(autouse 强制)→ 抛中文 RuntimeError(可测可读)。"""
        stub_keys(monkeypatch, {})
        with pytest.raises(RuntimeError, match="连接向导服务未就位"):
            trigger.ensure_setup(make_cfg(tmp_path), open_browser=False)
