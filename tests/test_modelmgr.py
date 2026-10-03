# -*- coding: utf-8 -*-
"""A148 · modelmgr 模型管理 CLI 测试(全部离线,零外呼、零落盘)。

兄弟模块(A143 local_probe / A144 model_manager / A147 connectivity /
A154 setup.daemon / A155 pipeline.takeover / A65 model_catalog /
A70 security.keys)用注入 ``sys.modules`` 的桩模块替代(与契约 §3 签名
同构);scanner/manager/tester/takeover 走 ``main`` 注入缝。

覆盖面(契约 §3 A148):

* status 各形态:有/无活动模型、stub 明示(红线 34)、云密钥布尔
  (红线 33:密钥值绝不出现)、本地服务在线/全不在线/降级;
* probe 扫描表:端口/提供方/视觉模型数/模型前 5、未过滤标注、
  "仅 127.0.0.1 回环"字样(红线 32);
* list:目录 + 本地追加标"本地"、按提供方过滤、去重、降级;
* switch:成功 / --force 跳连通性测试 / 测试失败退出 1 / stub 警示;
* test:显式 SPEC / 缺省用活动 spec / 失败退出 1;
* takeover:注入透传(cfg 同一对象)、向导 URL、模块缺失退出 1;
* serve:打印 URL、NO_BROWSER 不自动开、--port 覆盖;
* 退出码三分支:0 / 1(错误)/ 2(用法错),含 --help 与模块入口。
"""
from __future__ import annotations

import sys
import types

import pytest

import netsentinel as _root_pkg
import netsentinel.pipeline as _pipeline_pkg
import netsentinel.security as _security_pkg
import netsentinel.vision as _vision_pkg
from netsentinel import modelmgr
from netsentinel.contracts import Config

#: 密钥字面量(红线 33:任何输出不得出现)
SECRET = "sk-A148-SECRET-never-print-me"


# ---------------------------------------------------------------------------
# 注入缝 fake(与契约 §3 签名同构)
# ---------------------------------------------------------------------------


class FakeManager:
    """A144 ModelManager 同构桩:记录 set_active 调用,可注入失败。"""

    def __init__(self, spec=None, status=None, error=None):
        self.spec = spec
        self.error = error
        self._status = status if status is not None else (
            {"spec": spec, "switched_by": "takeover", "switched_at": "2026-10-01 09:00:00"}
            if spec
            else {}
        )
        self.calls = []

    def get_active(self):
        return self.spec

    def status(self):
        return dict(self._status)

    def set_active(self, spec, *, switched_by, validate=True):
        self.calls.append({"spec": spec, "switched_by": switched_by, "validate": validate})
        if self.error is not None:
            raise self.error
        self.spec = spec
        return True

    def clear(self):
        self.spec = None


class FakeScanner:
    """A143 LocalVisionScanner 同构桩:返回预置条目或抛错。"""

    def __init__(self, entries=None, error=None, raw=None):
        self.entries = entries if entries is not None else []
        self.error = error
        self.raw = raw
        self.scans = 0

    def scan(self):
        self.scans += 1
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return list(self.entries)


def _make_keys(configured_map, *, error=None):
    """A70 security.keys 桩:configured 只回布尔(另设 get_key 供红线 33 断言)。"""
    mod = types.ModuleType("netsentinel.security.keys")

    def configured(cfg):
        if error is not None:
            raise error
        return dict(configured_map)

    mod.configured = configured
    mod.get_key = lambda provider, cfg: SECRET if configured_map.get(provider) else ""
    return mod


def _make_catalog():
    """A65 model_catalog 桩:MODELS / ModelInfo(小目录,断言可控)。"""
    mod = types.ModuleType("netsentinel.vision.model_catalog")

    class ModelInfo:
        def __init__(self, id, tags, note=""):
            self.id = id
            self.tags = tags
            self.note = note

    mod.ModelInfo = ModelInfo
    mod.MODELS = {
        "glm": [
            ModelInfo("glm-4.5v", ["flagship"], "智谱旗舰;以官方为准"),
            ModelInfo("glm-5.3-flash", ["cheap", "balanced"], "轻量档;以官方为准"),
        ],
        "ollama": [ModelInfo("llava", ["local", "cheap"], "本地轻量;数据不出本机")],
        "openai": [ModelInfo("gpt-4o-mini", ["cheap"], "轻量多模态;以官方为准")],
    }
    return mod


def _make_local_probe(state):
    """A143 local_probe 桩:记录构造参数(ports/timeout),scan 返回一条在线。"""
    mod = types.ModuleType("netsentinel.vision.local_probe")

    class LocalVisionScanner:
        def __init__(self, ports, timeout=1.5):
            state.append({"ports": list(ports), "timeout": timeout})

        def scan(self):
            return [
                {
                    "provider": "ollama",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "models": ["llava"],
                    "ok": True,
                }
            ]

    mod.LocalVisionScanner = LocalVisionScanner
    return mod


def _make_model_manager(state, spec="ollama:llava"):
    """A144 model_manager 桩:记录构造路径与 set_active 参数。"""
    mod = types.ModuleType("netsentinel.vision.model_manager")

    class ModelManager:
        def __init__(self, path):
            state.append({"path": path})
            self._spec = spec
            self.path = path

        def get_active(self):
            return self._spec

        def status(self):
            return {"spec": self._spec, "switched_by": "wizard", "switched_at": "2026-10-02 08:00:00"}

        def set_active(self, spec, *, switched_by, validate=True):
            state.append({"set": (spec, switched_by, validate)})
            self._spec = spec

    mod.ModelManager = ModelManager
    return mod


def _make_connectivity(state):
    """A147 connectivity 桩:记录 (spec, cfg),返回成功结果。"""
    mod = types.ModuleType("netsentinel.vision.connectivity")

    def test_connection(spec, cfg, *, transport=None, spend=None):
        state.append({"spec": spec, "cfg": cfg})
        return {"ok": True, "latency_ms": 42.0, "model": spec.split(":")[-1]}

    mod.test_connection = test_connection
    return mod


def _make_takeover(state, result=None):
    """A155 pipeline.takeover 桩:记录 cfg,返回预置结果。"""
    mod = types.ModuleType("netsentinel.pipeline.takeover")

    def takeover_once(cfg, *, scanner=None, manager=None, ensure=None):
        state.append({"cfg": cfg})
        return dict(result if result is not None else {"action": "local", "detail": "mock"})

    mod.takeover_once = takeover_once
    return mod


def _make_daemon(state, url="http://127.0.0.1:8766/"):
    """A154 setup.daemon 桩:记录 (cfg, open_browser),返回预置 URL。"""
    mod = types.ModuleType("netsentinel.setup.daemon")

    def ensure_setup_server(cfg, *, open_browser=None):
        state.append({"cfg": cfg, "open_browser": open_browser})
        return url

    mod.ensure_setup_server = ensure_setup_server
    mod.stop_setup_server = lambda: None
    return mod


# ---------------------------------------------------------------------------
# 安装/拦截工具
# ---------------------------------------------------------------------------

#: 桩模块 -> 父包(用于同步父包属性,与 test_vlmctl 同构)
_PARENTS = {
    "netsentinel.vision": _vision_pkg,
    "netsentinel.security": _security_pkg,
    "netsentinel.pipeline": _pipeline_pkg,
    "netsentinel": _root_pkg,
}


def _install(monkeypatch: pytest.MonkeyPatch, **modules: types.ModuleType) -> None:
    """把桩模块挂进 sys.modules(含 netsentinel.setup 父包),结束后自动还原。"""
    for dotted, module in modules.items():
        monkeypatch.setitem(sys.modules, dotted, module)
        parent, _, leaf = dotted.rpartition(".")
        pkg = sys.modules.get(parent)
        if pkg is not None:
            monkeypatch.setattr(pkg, leaf, module, raising=False)


def _block(monkeypatch: pytest.MonkeyPatch, *dotted: str) -> None:
    """把指定兄弟模块标记为"未就位"(_try_import 返回 None),测降级分支。"""
    real = modelmgr._try_import

    def fake(name):
        if name in dotted:
            return None
        return real(name)

    monkeypatch.setattr(modelmgr, "_try_import", fake)


#: 通用在线/离线条目(probe/status/list 共用)
_ONLINE_OLLAMA = {
    "provider": "ollama",
    "base_url": "http://127.0.0.1:11434/v1",
    "models": ["llava", "qwen2.5vl"],
    "ok": True,
}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_active_full(monkeypatch, capsys):
    keys = _make_keys({"glm": True, "openai": True, "ollama": False, "vllm": False})
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    rc = modelmgr.main(
        ["status"], scanner=FakeScanner([_ONLINE_OLLAMA]), manager=FakeManager("ollama:llava"), cfg=Config()
    )
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "活动模型:ollama:llava" in out
    assert "来源:takeover" in out
    assert "切换时间:2026-10-01 09:00:00" in out
    assert "云密钥:已配置 2/4 家(glm、openai)" in out
    assert "本地服务:1 个在线" in out
    assert "127.0.0.1:11434(ollama)视觉模型 2 个" in out


def test_status_no_active_model(monkeypatch, capsys):
    keys = _make_keys({})
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    rc = modelmgr.main(["status"], scanner=FakeScanner([]), manager=FakeManager(None), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "活动模型:未设置" in out
    assert "云密钥:" in out and "本地服务:" in out  # 三行摘要齐全


def test_status_stub_marks_offline(monkeypatch, capsys):
    keys = _make_keys({})
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    rc = modelmgr.main(["status"], scanner=FakeScanner([]), manager=FakeManager("stub"), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "活动模型:stub" in out
    assert "离线桩,非模型判定" in out  # 红线 34


def test_status_keys_missing_degrades(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.security.keys")
    rc = modelmgr.main(["status"], scanner=FakeScanner([]), manager=FakeManager("ollama:llava"), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "云密钥:无法判定" in out


def test_status_keys_exception_degrades(monkeypatch, capsys):
    keys = _make_keys({}, error=RuntimeError("mock keys 崩了"))
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    rc = modelmgr.main(["status"], scanner=FakeScanner([]), manager=FakeManager("ollama:llava"), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "云密钥:无法判定" in out and "configured 异常" in out


def test_status_scanner_missing_degrades(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.vision.local_probe")
    rc = modelmgr.main(["status"], manager=FakeManager("ollama:llava"), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "本地服务:扫描不可用" in out


def test_status_scanner_exception_degrades(monkeypatch, capsys):
    rc = modelmgr.main(
        ["status"], scanner=FakeScanner(error=RuntimeError("mock 连接失败")), manager=FakeManager("ollama:llava"), cfg=Config()
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "本地服务:扫描失败" in out and "mock 连接失败" in out


def test_status_no_local_online(monkeypatch, capsys):
    keys = _make_keys({})
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    offline = {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1", "models": [], "ok": False}
    rc = modelmgr.main(["status"], scanner=FakeScanner([offline]), manager=FakeManager("glm:glm-4.5v"), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "本地服务:未发现在线本地视觉服务" in out
    assert "11434/1234/8000/9997" in out  # 探测端口=cfg.local_probe_ports(默认)


def test_status_manager_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.vision.model_manager")
    rc = modelmgr.main(["status"], scanner=FakeScanner([]), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "model_manager" in err


def test_status_manager_lazy_default(monkeypatch, capsys):
    state: list = []
    _install(
        monkeypatch,
        **{
            "netsentinel.vision.model_manager": _make_model_manager(state),
            "netsentinel.security.keys": _make_keys({"glm": True}),
        }
    )
    cfg = Config(model_runtime_path="data/custom_runtime.json")
    rc = modelmgr.main(["status"], scanner=FakeScanner([_ONLINE_OLLAMA]), cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and state[0]["path"] == "data/custom_runtime.json"  # 路径取 cfg
    assert "活动模型:ollama:llava" in out and "来源:wizard" in out
    assert "云密钥:已配置 1/1 家(glm)" in out


def test_status_manager_shapes_tolerant(monkeypatch, capsys):
    _install(monkeypatch, **{"netsentinel.security.keys": _make_keys({})})

    class OnlyGetActive:
        def get_active(self):
            return "ollama:llava"

    rc = modelmgr.main(["status"], manager=OnlyGetActive(), scanner=FakeScanner([]), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "活动模型:ollama:llava" in out and "来源:未知" in out  # 无 status() 也能出摘要

    class BadStatus:
        def get_active(self):
            return None

        def status(self):
            return "not-a-dict"  # 非字典被忽略,不崩

    rc2 = modelmgr.main(["status"], manager=BadStatus(), scanner=FakeScanner([]), cfg=Config())
    out2, _ = capsys.readouterr()
    assert rc2 == 0
    assert "活动模型:未设置" in out2


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_table(monkeypatch, capsys):
    entries = [
        {  # 7 个模型:只显示前 5 + 省略号
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "models": ["m1", "m2", "m3", "m4", "m5", "m6", "m7"],
            "ok": True,
        },
        {  # A150 缺席时的"未过滤"标注
            "provider": "lmstudio",
            "base_url": "http://127.0.0.1:1234/v1",
            "models": ["qwen2.5-vl-7b-instruct"],
            "ok": True,
            "unfiltered": True,
        },
        {"provider": "vllm", "base_url": "http://127.0.0.1:8000/v1", "models": [], "ok": False},
    ]
    rc = modelmgr.main(["probe"], scanner=FakeScanner(entries), cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "仅 127.0.0.1 回环" in out and "红线 32" in out  # 红线 32 显式可见
    assert "端口" in out and "提供方" in out and "视觉模型数" in out and "模型(前 5)" in out
    assert "11434" in out and "ollama" in out
    assert "1234" in out and "lmstudio" in out
    assert "m1、m2、m3、m4、m5…" in out
    assert "m6" not in out and "m7" not in out  # 超出前 5 不显示
    assert "1(未过滤)" in out
    assert "vllm" not in out  # ok=False 不进表
    assert "在线 2 个" in out


def test_probe_none_online(capsys):
    rc = modelmgr.main(["probe"], scanner=FakeScanner([]), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "(未发现在线本地视觉服务)" in out and "在线 0 个" in out


def test_probe_scanner_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.vision.local_probe")
    rc = modelmgr.main(["probe"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "local_probe" in err


def test_probe_scan_exception_exit1(capsys):
    rc = modelmgr.main(["probe"], scanner=FakeScanner(error=RuntimeError("mock 超时")), cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 1
    assert "本地扫描失败" in err
    assert "已切换" not in out and "结论" not in out


def test_probe_scan_bad_shape_exit1(capsys):
    rc = modelmgr.main(["probe"], scanner=FakeScanner(raw={"not": "a list"}), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "形态异常" in err


def test_probe_lazy_default_ports_timeout(monkeypatch, capsys):
    state: list = []
    _install(monkeypatch, **{"netsentinel.vision.local_probe": _make_local_probe(state)})
    cfg = Config(local_probe_ports=["11434", "1234"])
    rc = modelmgr.main(["probe"], cfg=cfg)  # 未注入 scanner → 惰性构造 A143
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and state[0]["ports"] == ["11434", "1234"]  # 端口取 cfg
    assert state[0]["timeout"] == 1.5  # 超时受控
    assert "ollama" in out and "llava" in out


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_catalog_plus_local(monkeypatch, capsys):
    entries = [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1", "models": ["llava", "minicpm-v"], "ok": True},
        {"provider": "openai 兼容", "base_url": "http://127.0.0.1:9997/v1", "models": ["custom-vlm"], "ok": True},
    ]
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    rc = modelmgr.main(["list"], scanner=FakeScanner(entries), cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "glm-4.5v" in out and "llava" in out  # 目录条目
    assert "(本地)minicpm-v" in out  # 目录外追加,标"本地"
    assert "openai 兼容(本地服务):" in out  # 目录外提供方单列
    assert "(本地)custom-vlm" in out
    assert "共 3 家提供方 / 4 个目录模型" in out
    assert "本地追加 2 个" in out


def test_list_provider_filter(monkeypatch, capsys):
    entries = [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1", "models": ["minicpm-v"], "ok": True}
    ]
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    rc = modelmgr.main(["list", "GLM"], scanner=FakeScanner(entries), cfg=Config())  # 大小写不敏感
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "glm:" in out and "glm-4.5v" in out
    assert "ollama:" not in out and "minicpm-v" not in out  # 其他提供方与本地发现被过滤


def test_list_unknown_provider_exit1(monkeypatch, capsys):
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    rc = modelmgr.main(["list", "nope"], scanner=FakeScanner([]), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未知提供方" in err


def test_list_scan_degrades_on_error(monkeypatch, capsys):
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    rc = modelmgr.main(["list"], scanner=FakeScanner(error=RuntimeError("mock 扫描失败")), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0  # 扫描只是辅助:降级不致命
    assert "本地扫描降级" in out and "mock 扫描失败" in out
    assert "glm-4.5v" in out and "本地追加 0 个" in out


def test_list_scanner_missing_degrades(monkeypatch, capsys):
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    _block(monkeypatch, "netsentinel.vision.local_probe")
    rc = modelmgr.main(["list"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "本地扫描降级" in out and "扫描不可用" in out
    assert "glm-4.5v" in out


def test_list_dedup_against_catalog(monkeypatch, capsys):
    entries = [{"provider": "ollama", "base_url": "http://127.0.0.1:11434/v1", "models": ["llava"], "ok": True}]
    _install(monkeypatch, **{"netsentinel.vision.model_catalog": _make_catalog()})
    rc = modelmgr.main(["list"], scanner=FakeScanner(entries), cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "(本地)llava" not in out  # 目录已有同名条目:不重复追加
    assert "本地追加 0 个" in out


def test_list_catalog_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.vision.model_catalog")
    rc = modelmgr.main(["list"], scanner=FakeScanner([]), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "model_catalog" in err


# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------


def test_switch_success(capsys):
    fm = FakeManager("glm:glm-4.5v")
    rc = modelmgr.main(["switch", "ollama:llava"], manager=fm, cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert fm.calls == [{"spec": "ollama:llava", "switched_by": "cli", "validate": True}]
    assert "连通性测试通过" in out
    assert "已切换:ollama:llava(下一条扫描生效)" in out


def test_switch_force_skips_validation(capsys):
    fm = FakeManager("glm:glm-4.5v")
    rc = modelmgr.main(["switch", "glm:glm-4.5v", "--force"], manager=fm, cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert fm.calls and fm.calls[0]["validate"] is False  # --force → validate=False
    assert fm.calls[0]["switched_by"] == "cli"
    assert "跳过连通性测试" in out
    assert "已切换:glm:glm-4.5v(下一条扫描生效)" in out


def test_switch_validation_failure_exit1(capsys):
    fm = FakeManager("glm:glm-4.5v", error=ValueError("连通性测试失败:11434 上无该模型"))
    rc = modelmgr.main(["switch", "ollama:llava"], manager=fm, cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 1
    assert "连通性测试失败" in err  # A144 的中文 ValueError 原样透传
    assert "已切换" not in out  # 失败绝不打印成功横幅


def test_switch_stub_warns_offline(capsys):
    fm = FakeManager("ollama:llava")
    rc = modelmgr.main(["switch", "stub", "--force"], manager=fm, cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "离线桩,非模型判定" in out  # 红线 34
    assert "已切换:stub(下一条扫描生效)" in out


def test_switch_missing_spec_usage_exit2(capsys):
    rc = modelmgr.main(["switch"], manager=FakeManager(), cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 2
    assert "用法错误" in err


def test_switch_manager_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.vision.model_manager")
    rc = modelmgr.main(["switch", "ollama:llava"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_test_explicit_spec_ok(capsys):
    seen: list = []

    def tester(spec):
        seen.append(spec)
        return {"ok": True, "latency_ms": 88.8, "model": "glm-4.5v"}

    rc = modelmgr.main(["test", "glm:glm-4.5v"], tester=tester, cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert seen == ["glm:glm-4.5v"]
    assert "测试目标:glm:glm-4.5v" in out
    assert "88.8 ms" in out and "模型回显:glm-4.5v" in out
    assert "✅ 连通正常" in out


def test_test_defaults_to_active(capsys):
    seen: list = []

    def tester(spec):
        seen.append(spec)
        return {"ok": True, "latency_ms": 10.0, "model": "llava"}

    rc = modelmgr.main(["test"], manager=FakeManager("ollama:llava"), tester=tester, cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert seen == ["ollama:llava"]  # 缺省测活动 spec
    assert "测试目标:ollama:llava" in out


def test_test_no_active_no_spec_exit1(capsys):
    rc = modelmgr.main(["test"], manager=FakeManager(None), tester=lambda s: {"ok": True}, cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "无活动模型" in err


def test_test_failure_exit1(capsys):
    rc = modelmgr.main(
        ["test", "ollama:nope"],
        tester=lambda spec: {"ok": False, "error": "本地 11434 无该模型", "latency_ms": None},
        cfg=Config(),
    )
    out, _ = capsys.readouterr()
    assert rc == 1
    assert "❌ 连通失败:本地 11434 无该模型" in out
    assert "(未测量)" in out  # latency 缺失容错


def test_test_lazy_default_module(monkeypatch, capsys):
    state: list = []
    _install(monkeypatch, **{"netsentinel.vision.connectivity": _make_connectivity(state)})
    cfg = Config()
    rc = modelmgr.main(["test", "ollama:llava"], cfg=cfg)  # 未注入 tester → 惰性调 A147
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and state[0]["spec"] == "ollama:llava" and state[0]["cfg"] is cfg
    assert "✅ 连通正常" in out


# ---------------------------------------------------------------------------
# takeover
# ---------------------------------------------------------------------------


def test_takeover_passthrough(capsys):
    seen: list = []

    def takeover(cfg):
        seen.append(cfg)
        return {"action": "local", "spec": "ollama:llava", "detail": "发现本地 ollama,已接管 llava"}

    cfg = Config()
    rc = modelmgr.main(["takeover"], takeover=takeover, cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert seen and seen[0] is cfg  # 透传同一 cfg 对象
    assert "动作:local" in out and "接管模型:ollama:llava" in out
    assert "详情:发现本地 ollama,已接管 llava" in out
    assert "红线 32" in out


def test_takeover_wizard_url(capsys):
    def takeover(cfg):
        return {"action": "wizard", "wizard": "http://127.0.0.1:8766/"}

    rc = modelmgr.main(["takeover"], takeover=takeover, cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "动作:wizard" in out and "已触发连接向导:http://127.0.0.1:8766/" in out


def test_takeover_disabled_still_zero(capsys):
    rc = modelmgr.main(["takeover"], takeover=lambda cfg: {"action": "disabled", "detail": "takeover_auto=False"}, cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0  # "禁用"是正常结果,不是错误
    assert "动作:disabled" in out


def test_takeover_module_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.pipeline.takeover")
    rc = modelmgr.main(["takeover"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "pipeline.takeover" in err


def test_takeover_exception_exit1(capsys):
    def takeover(cfg):
        raise RuntimeError("mock 接管崩溃")

    rc = modelmgr.main(["takeover"], takeover=takeover, cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "takeover 执行失败" in err and "mock 接管崩溃" in err


def test_takeover_lazy_default_module(monkeypatch, capsys):
    state: list = []
    _install(monkeypatch, **{"netsentinel.pipeline.takeover": _make_takeover(state, {"action": "local", "spec": "ollama:llava"})})
    rc = modelmgr.main(["takeover"], cfg=Config())  # 未注入 takeover → 惰性调 A155
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and "动作:local" in out and "接管模型:ollama:llava" in out


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def test_serve_prints_url_and_opens_browser(monkeypatch, capsys):
    state: list = []
    setup_pkg = types.ModuleType("netsentinel.setup")
    _install(
        monkeypatch,
        **{"netsentinel.setup": setup_pkg, "netsentinel.setup.daemon": _make_daemon(state)}
    )
    monkeypatch.delenv("NETSENTINEL_NO_BROWSER", raising=False)
    rc = modelmgr.main(["serve"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "向导地址:http://127.0.0.1:8766/" in out
    assert state and state[0]["open_browser"] is True  # 缺省自动开
    assert "已请求自动打开" in out
    assert state[0]["cfg"].setup_port == 8766  # 缺省端口取 cfg


def test_serve_no_browser_env(monkeypatch, capsys):
    state: list = []
    setup_pkg = types.ModuleType("netsentinel.setup")
    _install(
        monkeypatch,
        **{"netsentinel.setup": setup_pkg, "netsentinel.setup.daemon": _make_daemon(state)}
    )
    monkeypatch.setenv("NETSENTINEL_NO_BROWSER", "1")
    rc = modelmgr.main(["serve"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and state[0]["open_browser"] is False  # NO_BROWSER 不自动开
    assert "不自动打开浏览器" in out and "NETSENTINEL_NO_BROWSER" in out
    assert "向导地址:" in out


def test_serve_port_override(monkeypatch, capsys):
    state: list = []
    setup_pkg = types.ModuleType("netsentinel.setup")
    _install(
        monkeypatch,
        **{"netsentinel.setup": setup_pkg, "netsentinel.setup.daemon": _make_daemon(state)}
    )
    rc = modelmgr.main(["serve", "--port", "9999"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state and state[0]["cfg"].setup_port == 9999  # --port 覆盖 cfg
    assert "--port 9999" in out and "8766" in out  # 提示覆盖了哪个配置值


def test_serve_returns_none_exit1(monkeypatch, capsys):
    setup_pkg = types.ModuleType("netsentinel.setup")
    _install(monkeypatch, **{"netsentinel.setup": setup_pkg, "netsentinel.setup.daemon": _make_daemon([], url=None)})
    rc = modelmgr.main(["serve"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未能启动" in err


def test_serve_daemon_missing_exit1(monkeypatch, capsys):
    _block(monkeypatch, "netsentinel.setup.daemon")
    rc = modelmgr.main(["serve"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "setup.daemon" in err


# ---------------------------------------------------------------------------
# 退出码三分支(0/1/2)与入口
# ---------------------------------------------------------------------------


def test_no_args_usage_exit2(capsys):
    rc = modelmgr.main([], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 2
    assert "用法错误" in err


def test_unknown_command_usage_exit2(capsys):
    rc = modelmgr.main(["frobnicate"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 2
    assert "用法错误" in err


def test_bad_flag_usage_exit2(capsys):
    rc = modelmgr.main(["switch", "--nope", "x"], manager=FakeManager(), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 2


def test_help_exit0(capsys):
    rc = modelmgr.main(["--help"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "status" in out and "switch" in out and "serve" in out
    assert "python -m netsentinel.modelmgr" in out


def test_module_entry_point(monkeypatch, capsys):
    """python -m netsentinel.modelmgr 入口:runpy 以 __main__ 执行 --help。"""
    import runpy
    import warnings

    monkeypatch.setattr(sys, "argv", ["modelmgr", "--help"])
    with warnings.catch_warnings():
        # runpy 对已导入模块的常规提示(本测试刻意复跑 __main__),与行为无关
        warnings.filterwarnings("ignore", message=".*found in sys.modules.*")
        with pytest.raises(SystemExit) as ei:
            runpy.run_module("netsentinel.modelmgr", run_name="__main__", alter_sys=True)
    assert ei.value.code in (0, None)
    out = capsys.readouterr().out
    assert "status" in out and "modelmgr" in out


def test_unexpected_exception_exit1(monkeypatch, capsys):
    """兜底分支:命令内部冒出的未知异常也转中文退出码 1。"""

    class BoomManager:
        def get_active(self):
            raise KeyError("mock 内部炸了")

    rc = modelmgr.main(["status"], manager=BoomManager(), scanner=FakeScanner([]), cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "读取活动模型失败" in err


# ---------------------------------------------------------------------------
# 红线 33:密钥值绝不出现(只进不显)
# ---------------------------------------------------------------------------


def test_secret_never_printed(monkeypatch, capsys):
    keys = _make_keys({"glm": True, "openai": False})  # get_key 会返回 SECRET
    _install(monkeypatch, **{"netsentinel.security.keys": keys})
    rc = modelmgr.main(
        ["status"], scanner=FakeScanner([_ONLINE_OLLAMA]), manager=FakeManager("ollama:llava"), cfg=Config()
    )
    out, err = capsys.readouterr()
    assert rc == 0
    assert "云密钥:已配置 1/2 家(glm)" in out
    assert SECRET not in out and SECRET not in err  # 红线 33:密钥本体绝不回显
