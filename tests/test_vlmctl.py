# -*- coding: utf-8 -*-
"""A67 · vlmctl 诊断 CLI 测试(全部离线,零外呼)。

兄弟模块(providers / vlm_client / model_catalog / local_gateway /
security.keys)用注入 ``sys.modules`` 的桩模块替代(与 A61–A66/A70 契约
同构的极简实现):并行开发期真模块未就位时可测,落地后本桩仍确定性地
覆盖真实实现。vlm_client 桩内置**可计数传输层**,逐一断言各子命令的外呼
次数(红线 20:list/models/doctor 默认零外呼,ping --offline 零外呼,
ping 成功恰好外呼 1 次)。

密钥红线 17:设入密钥字面量后断言输出(out+err)绝不包含它。
预算红线 19:ping 成功路径断言 vlm_cache 记账 +1;预算耗尽时拒绝外呼。

A243 成本旁路落账:ping 落账点开关矩阵(缺省关零落账 / 附加属性开启后
补记一条 / 客户端自落账时不双记 / 落账异常不影响 ping 返回码),以及
仓库 config.example.yaml 的 load_config 往返断言(无未知键告警)。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import types

import pytest

import netsentinel.security as _security_pkg
import netsentinel.vision as _vision_pkg
from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import vlmctl

# ---------------------------------------------------------------------------
# 常量:与 CONTRACTS-V4 §2 权威目录一致的 20 个提供方桩数据
# ---------------------------------------------------------------------------

#: (提供方, 方言, 默认模型, 密钥环境变量列表) —— 云端 16 个
_CLOUD: list[tuple[str, str, str, list[str]]] = [
    ("glm", "openai", "glm-5.3-flash", ["NETSENTINEL_GLM_API_KEY", "GLM_API_KEY"]),
    ("openai", "openai", "gpt-4o-mini", ["NETSENTINEL_OPENAI_API_KEY", "OPENAI_API_KEY"]),
    ("anthropic", "anthropic", "claude-sonnet-4", ["NETSENTINEL_ANTHROPIC_API_KEY"]),
    ("gemini", "gemini", "gemini-2.0-flash", ["NETSENTINEL_GEMINI_API_KEY"]),
    ("qwen", "openai", "qwen-vl-max", ["NETSENTINEL_QWEN_API_KEY"]),
    ("doubao", "openai", "doubao-1.5-vision-pro", ["NETSENTINEL_DOUBAO_API_KEY"]),
    ("hunyuan", "openai", "hunyuan-vision", ["NETSENTINEL_HUNYUAN_API_KEY"]),
    ("moonshot", "openai", "kimi-latest", ["NETSENTINEL_MOONSHOT_API_KEY"]),
    ("minimax", "openai", "MiniMax-VL-01", ["NETSENTINEL_MINIMAX_API_KEY"]),
    ("stepfun", "openai", "step-1v-8k", ["NETSENTINEL_STEPFUN_API_KEY"]),
    ("siliconflow", "openai", "Qwen/Qwen2.5-VL-7B-Instruct", ["NETSENTINEL_SILICONFLOW_API_KEY"]),
    ("ernie", "openai", "ernie-4.5-vl", ["NETSENTINEL_ERNIE_API_KEY"]),
    ("openrouter", "openai", "qwen/qwen2.5-vl-72b-instruct:free", ["NETSENTINEL_OPENROUTER_API_KEY"]),
    ("groq", "openai", "meta-llama/llama-4-scout-17b-16e-instruct", ["NETSENTINEL_GROQ_API_KEY"]),
    ("together", "openai", "meta-llama/Llama-4-Scout-17B-16E-Instruct", ["NETSENTINEL_TOGETHER_API_KEY"]),
    ("xai", "openai", "grok-2-vision-1212", ["NETSENTINEL_XAI_API_KEY"]),
]

#: 本地 4 个(免密钥、必填模型者无默认模型)
_LOCAL: list[tuple[str, str]] = [("ollama", "llava"), ("vllm", ""), ("lmstudio", ""), ("xinference", "")]

_ALL_NAMES = [t[0] for t in _CLOUD] + [t[0] for t in _LOCAL]

#: 密钥字面量(用于红线 17:任何输出不得出现)
SECRET_CFG = "sk-CFG-SECRET-a67-DO-NOT-PRINT"
SECRET_ENV = "sk-ENV-SECRET-b67-DO-NOT-PRINT"


# ---------------------------------------------------------------------------
# 兄弟模块桩工厂(与契约 §4 签名同构)
# ---------------------------------------------------------------------------


def _make_providers() -> types.ModuleType:
    """A61 providers 桩:ProviderSpec/PROVIDERS/parse_spec/resolve。"""
    mod = types.ModuleType("netsentinel.vision.providers")

    class ProviderSpec:
        def __init__(self, key, base_url, style, default_model, key_envs, local=False, notes=""):
            self.key = key
            self.base_url = base_url
            self.style = style
            self.default_model = default_model
            self.key_envs = key_envs
            self.local = local
            self.notes = notes

    class ResolvedProvider:
        def __init__(self, base_url, model, api_key, style, local):
            self.base_url = base_url
            self.model = model
            self.api_key = api_key
            self.style = style
            self.local = local

    specs: dict[str, ProviderSpec] = {}
    for name, style, model, envs in _CLOUD:
        specs[name] = ProviderSpec(name, f"https://{name}.example/api/v1", style, model, envs)
    for i, (name, model) in enumerate(_LOCAL):
        specs[name] = ProviderSpec(name, f"http://127.0.0.1:{11434 + i}/v1", "openai", model, [], local=True)

    mod.ProviderSpec = ProviderSpec
    mod.ResolvedProvider = ResolvedProvider
    mod.PROVIDERS = specs

    def parse_spec(text):
        provider, _, model = str(text).partition(":")
        model = model or None
        if provider not in specs:
            raise ValueError(f"未知提供方:{provider};可用:{', '.join(specs)}")
        return provider, model

    def resolve(provider, cfg):
        spec = specs[provider]
        model = (cfg.vlm_provider_models or {}).get(provider) or spec.default_model or ""
        if not model:
            raise ValueError(f"{provider} 必须指定模型,如 {provider}:llava")
        base = (cfg.vlm_provider_base_urls or {}).get(provider) or spec.base_url
        if spec.local:
            key = ""
        else:
            key = (cfg.vlm_api_keys or {}).get(provider) or ""
            if not key:
                key = next((os.environ.get(e, "") for e in spec.key_envs if os.environ.get(e)), "")
        return ResolvedProvider(base, model, key, spec.style, spec.local)

    mod.parse_spec = parse_spec
    mod.resolve = resolve
    return mod


def _fresh_state() -> dict:
    """vlm_client 桩的外呼计数器。"""
    return {"transport": 0, "chat_json": 0, "constructed": 0}


def _make_vlm_client(state: dict, *, behavior: str = "success") -> types.ModuleType:
    """A62 vlm_client 桩:可计数传输层;behavior ∈ success/fail/config_error。"""
    mod = types.ModuleType("netsentinel.vision.vlm_client")

    class VlmConfigError(RuntimeError):
        pass

    def _transport(url, headers, payload, timeout):
        state["transport"] += 1
        if behavior == "fail":
            return 500, json.dumps({"error": {"message": "mock:模拟服务端错误"}}, ensure_ascii=False)
        content = json.dumps(
            {"nsfw_prob": 0.02, "categories": ["正常"], "reasoning": "mock 测试图", "confidence": 0.9},
            ensure_ascii=False,
        )
        return 200, json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)

    class UniversalVLMClient:
        def __init__(self, resolved, cfg, *, transport=None):
            state["constructed"] += 1
            self.resolved = resolved
            self.cfg = cfg
            self.model = None
            self._transport = transport or _transport

        def chat_json(self, messages, *, image_paths=None):
            state["chat_json"] += 1
            if behavior == "config_error":
                raise VlmConfigError("mock:vlm_online=False 或密钥未配置,拒绝外呼")
            status, body = self._transport(self.resolved.base_url, {}, {"messages": messages}, 30)
            if status != 200:
                raise RuntimeError(f"mock:接口返回 HTTP {status}")
            self.model = self.resolved.model
            return json.loads(json.loads(body)["choices"][0]["message"]["content"])

    mod.VlmConfigError = VlmConfigError
    mod.UniversalVLMClient = UniversalVLMClient
    return mod


def _make_model_catalog() -> types.ModuleType:
    """A65 model_catalog 桩:MODELS / ModelInfo。"""
    mod = types.ModuleType("netsentinel.vision.model_catalog")

    class ModelInfo:
        def __init__(self, id, tags, note=""):
            self.id = id
            self.tags = tags
            self.note = note

    mod.ModelInfo = ModelInfo
    mod.MODELS = {
        "glm": [
            ModelInfo("glm-5.3-flash", ["cheap", "balanced"], "提示值,以官方为准"),
            ModelInfo("glm-4.6v-plus", ["flagship"], ""),
        ],
        "openai": [ModelInfo("gpt-4o-mini", ["cheap"]), ModelInfo("gpt-4o", ["flagship"])],
        "ollama": [ModelInfo("llava", ["local"], "本地模型")],
    }
    return mod


def _make_local_gateway(state: dict) -> types.ModuleType:
    """A66 local_gateway 桩:probe/local_status 计数(不真正联网)。"""
    mod = types.ModuleType("netsentinel.vision.local_gateway")

    def probe(base_url, timeout=2):
        state["probe"] = state.get("probe", 0) + 1
        return {"ok": True, "models": ["llava"], "error": ""}

    def local_status(cfg):
        state["local_status"] = state.get("local_status", 0) + 1
        return {
            "ollama": {"ok": True, "models": ["llava"], "error": ""},
            "vllm": {"ok": False, "models": [], "error": "连接失败(mock)"},
            "lmstudio": {"ok": False, "models": [], "error": "连接失败(mock)"},
            "xinference": {"ok": False, "models": [], "error": "连接失败(mock)"},
        }

    mod.probe = probe
    mod.local_status = local_status
    mod.is_local = lambda provider: provider in {n for n, _ in _LOCAL}
    return mod


def _make_keys(configured_map: dict) -> types.ModuleType:
    """A70 security.keys 桩:configured 只回布尔。"""
    mod = types.ModuleType("netsentinel.security.keys")
    mod.configured = lambda cfg: dict(configured_map)
    mod.get_key = lambda provider, cfg: "mock-key-value" if configured_map.get(provider) else ""
    return mod


def _install_siblings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    providers: types.ModuleType | None = None,
    vlm_client: types.ModuleType | None = None,
    model_catalog: types.ModuleType | None = None,
    local_gateway: types.ModuleType | None = None,
    keys: types.ModuleType | None = None,
) -> None:
    """把桩模块挂进 sys.modules(并同步父包属性),结束后自动还原。"""
    for module, dotted in (
        (providers, "netsentinel.vision.providers"),
        (vlm_client, "netsentinel.vision.vlm_client"),
        (model_catalog, "netsentinel.vision.model_catalog"),
        (local_gateway, "netsentinel.vision.local_gateway"),
        (keys, "netsentinel.security.keys"),
    ):
        if module is None:
            continue
        monkeypatch.setitem(sys.modules, dotted, module)
        parent, _, leaf = dotted.rpartition(".")
        pkg = _vision_pkg if parent == "netsentinel.vision" else _security_pkg
        monkeypatch.setattr(pkg, leaf, module, raising=False)


def _provider_rows(out: str) -> list[str]:
    """从表格输出中提取以提供方名开头的行(list/doctor 的数据行)。"""
    return [line for line in out.splitlines() if line.split() and line.split()[0] in _ALL_NAMES]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_shows_20_providers(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers())
    monkeypatch.setenv("NETSENTINEL_GLM_API_KEY", "k")
    rc = vlmctl.main(["list"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    rows = _provider_rows(out)
    assert len(rows) == 20
    assert {r.split()[0] for r in rows} == set(_ALL_NAMES)
    assert "共 20 个提供方" in out
    # 密钥布尔:glm 走环境变量回退判定为已配置;xai 未配置
    assert next(r for r in rows if r.startswith("glm")).rstrip().endswith("✓已配置")
    assert next(r for r in rows if r.startswith("xai")).rstrip().endswith("✗")


def test_list_key_column_only_booleans(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers(), keys=_make_keys({"openai": True}))
    rc = vlmctl.main(["list"], cfg=Config())
    out, _ = capsys.readouterr()
    assert rc == 0
    allowed = {"✓已配置", "✗", "免密钥"}
    rows = _provider_rows(out)
    assert len(rows) == 20
    for line in rows:
        assert line.split()[-1] in allowed, line
    # openai 经 security.keys 判定;gemini 未配置;本地提供方免密钥
    assert next(r for r in rows if r.startswith("openai")).rstrip().endswith("✓已配置")
    assert next(r for r in rows if r.startswith("gemini")).rstrip().endswith("✗")
    assert next(r for r in rows if r.startswith("ollama")).rstrip().endswith("免密钥")


def test_list_marks_cfg_model_override(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers())
    cfg = Config(vlm_provider_models={"glm": "glm-x-custom"})
    rc = vlmctl.main(["list"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 0
    glm_line = next(r for r in _provider_rows(out) if r.startswith("glm"))
    assert "glm-x-custom" in glm_line and "cfg 覆盖" in glm_line


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


def test_models_lists_catalog_and_marks_default(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers(), model_catalog=_make_model_catalog())
    rc = vlmctl.main(["models", "glm"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "glm-5.3-flash" in out and "glm-4.6v-plus" in out
    starred = next(l for l in out.splitlines() if "glm-5.3-flash" in l)
    unstarred = next(l for l in out.splitlines() if "glm-4.6v-plus" in l)
    assert "★" in starred and "★" not in unstarred
    assert "cheap" in out and "以官方为准" in out


def test_models_respects_cfg_default_override(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers(), model_catalog=_make_model_catalog())
    cfg = Config(vlm_provider_models={"glm": "glm-4.6v-plus"})
    rc = vlmctl.main(["models", "glm"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "★" in next(l for l in out.splitlines() if "glm-4.6v-plus" in l)
    assert "★" not in next(l for l in out.splitlines() if "glm-5.3-flash" in l)
    assert "cfg.vlm_provider_models 覆盖" in out


def test_models_unknown_provider_exit_1(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers(), model_catalog=_make_model_catalog())
    rc = vlmctl.main(["models", "no-such-provider"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未知提供方" in err


# ---------------------------------------------------------------------------
# ping:成功 / 失败 / --offline 零外呼 / 预算
# ---------------------------------------------------------------------------


def test_ping_success_once_with_budget(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(vlm_online=True, vlm_api_keys={"glm": "mock"}, vlm_cache_db=str(tmp_path / "c.db"))
    rc = vlmctl.main(["ping", "glm:glm-5.3-flash"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    # 恰好一次外呼(chat_json 与传输层各 1)
    assert state["chat_json"] == 1
    assert state["transport"] == 1
    for fragment in ("延迟", "模型回显", "nsfw_prob", "解析", "✅"):
        assert fragment in out
    assert "glm-5.3-flash" in out
    # 红线 19:真实外呼必须记账
    from netsentinel.vision.vlm_cache import VlmCache

    assert VlmCache(cfg.vlm_cache_db).budget_state()["used"] == 1


def test_ping_failure_exit_1(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state, behavior="fail"))
    cfg = Config(vlm_online=True, vlm_api_keys={"glm": "mock"}, vlm_cache_db=str(tmp_path / "c.db"))
    rc = vlmctl.main(["ping", "glm"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "错误" in err and "HTTP 500" in err
    assert state["chat_json"] == 1 and state["transport"] == 1


def test_ping_config_error_exit_1(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(
        monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state, behavior="config_error")
    )
    cfg = Config(vlm_online=True, vlm_api_keys={"glm": "mock"}, vlm_cache_db=str(tmp_path / "c.db"))
    rc = vlmctl.main(["ping", "glm"], cfg=cfg)
    _, err = capsys.readouterr()
    assert rc == 1
    assert "拒绝外呼" in err  # VlmConfigError 中文透传
    assert state["chat_json"] == 1 and state["transport"] == 0


def test_ping_offline_zero_outbound_when_ready(monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(vlm_online=True, vlm_api_keys={"openai": "mock"})
    rc = vlmctl.main(["ping", "openai", "--offline"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    # 零外呼:传输层与 chat_json 均未被调用
    assert state["transport"] == 0
    assert state["chat_json"] == 0
    assert state["constructed"] == 1
    assert "--offline" in out and "✓已配置" in out


def test_ping_offline_gates_unmet_exit_1(monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config()  # vlm_online=False 且无密钥
    rc = vlmctl.main(["ping", "openai", "--offline"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 1
    assert state["transport"] == 0 and state["chat_json"] == 0
    assert "✗未配置" in out and "vlm_online" in out


def test_ping_offline_local_provider_ready(monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config()  # 本地提供方免 vlm_online 闸门(红线 16)
    rc = vlmctl.main(["ping", "ollama:llava", "--offline"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state["transport"] == 0 and state["chat_json"] == 0
    assert "免密钥" in out


def test_ping_missing_image_file(monkeypatch, capsys, tmp_path):
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(_fresh_state()))
    rc = vlmctl.main(["ping", "glm", "--image", str(tmp_path / "nope.png"), "--offline"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "不存在" in err


def test_ping_custom_image_used(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    image = tmp_path / "custom_probe.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake-bytes")
    cfg = Config(vlm_online=True, vlm_api_keys={"glm": "mock"}, vlm_cache_db=str(tmp_path / "c.db"))
    rc = vlmctl.main(["ping", "glm", "--image", str(image)], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert str(image) in out  # --image 替换内嵌 1x1 测试图


def test_ping_requires_model_for_vllm(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(_fresh_state()))
    rc = vlmctl.main(["ping", "vllm", "--offline"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "必须指定模型" in err


def test_ping_budget_exhausted_refuses_outbound(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(
        vlm_online=True,
        vlm_api_keys={"glm": "mock"},
        vlm_cache_db=str(tmp_path / "c.db"),
        vlm_daily_budget=0,  # 当日额度为 0
    )
    rc = vlmctl.main(["ping", "glm"], cfg=cfg)
    _, err = capsys.readouterr()
    assert rc == 1
    assert "预算" in err  # 红线 19:记账失败拒绝外呼
    assert state["chat_json"] == 0 and state["transport"] == 0


# ---------------------------------------------------------------------------
# doctor:默认零外呼 / fallback 链体检 / --probe
# ---------------------------------------------------------------------------


def test_doctor_default_zero_outbound(monkeypatch, capsys):
    state = _fresh_state()
    probe_state: dict = {}
    _install_siblings(
        monkeypatch,
        providers=_make_providers(),
        vlm_client=_make_vlm_client(state),
        local_gateway=_make_local_gateway(probe_state),
    )
    rc = vlmctl.main(["doctor"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    # 默认零外呼:不探本地网关、不 ping 云端
    assert probe_state.get("local_status", 0) == 0
    assert probe_state.get("probe", 0) == 0
    assert state["chat_json"] == 0 and state["transport"] == 0
    # 体检输出:本地就绪 ✅、云端未配置 ⚠️
    assert "零外呼" in out
    assert "✅" in out and "⚠️" in out
    assert "未配置" in out
    rows = _provider_rows(out)
    assert len(rows) == 20


def test_doctor_invalid_fallback_chain_warns(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers())
    cfg = Config(vlm_fallback_chain=["no-such-provider:m", "glm:glm-5.3-flash"])
    rc = vlmctl.main(["doctor"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 0  # 链条非法是 ⚠️ 提示,不是 ❌ 错误
    assert "无法解析" in out and "⚠️" in out
    assert "'glm:glm-5.3-flash' 可解析" in out


def test_doctor_unknown_provider_in_overrides_errors(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers())
    cfg = Config(vlm_api_keys={"not-a-provider": "x"})
    rc = vlmctl.main(["doctor"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 1
    assert "未知提供方" in out and "vlm_api_keys" in out


def test_doctor_ready_provider_marked_ok(monkeypatch, capsys):
    _install_siblings(monkeypatch, providers=_make_providers())
    monkeypatch.setenv("NETSENTINEL_GLM_API_KEY", "k")
    rc = vlmctl.main(["doctor"], cfg=Config(vlm_online=True))
    out, _ = capsys.readouterr()
    assert rc == 0
    glm_line = next(r for r in _provider_rows(out) if r.startswith("glm"))
    assert "✅" in glm_line and "就绪" in glm_line


def test_doctor_probe_local_and_cloud(tmp_path, monkeypatch, capsys):
    state = _fresh_state()
    probe_state: dict = {}
    _install_siblings(
        monkeypatch,
        providers=_make_providers(),
        vlm_client=_make_vlm_client(state),
        local_gateway=_make_local_gateway(probe_state),
    )
    cfg = Config(
        vlm_online=True,
        vlm_api_keys={"openai": "mock"},
        vlm_provider="openai",
        vlm_fallback_chain=["openai:gpt-4o-mini"],
        vlm_cache_db=str(tmp_path / "c.db"),
    )
    rc = vlmctl.main(["doctor", "--probe"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert probe_state.get("local_status", 0) == 1  # 本地网关全量探测一次
    assert state["chat_json"] == 1 and state["transport"] == 1  # 云端 ping 恰好一次
    assert "在线" in out and "云端 ping" in out
    assert "ollama" in out and "连接失败(mock)" in out


def test_doctor_probe_cloud_failure_is_error(monkeypatch, capsys, tmp_path):
    state = _fresh_state()
    _install_siblings(
        monkeypatch,
        providers=_make_providers(),
        vlm_client=_make_vlm_client(state, behavior="fail"),
        local_gateway=_make_local_gateway({}),
    )
    cfg = Config(
        vlm_online=True,
        vlm_api_keys={"openai": "mock"},
        vlm_provider="openai",
        vlm_cache_db=str(tmp_path / "c.db"),
    )
    rc = vlmctl.main(["doctor", "--probe"], cfg=cfg)
    out, _ = capsys.readouterr()
    assert rc == 1  # 云端 ping 失败计入 ❌
    assert "云端 ping 失败" in out


# ---------------------------------------------------------------------------
# 红线 17:密钥值绝不回显
# ---------------------------------------------------------------------------


def test_key_values_never_echoed(monkeypatch, capsys, tmp_path):
    _install_siblings(
        monkeypatch,
        providers=_make_providers(),
        model_catalog=_make_model_catalog(),
        vlm_client=_make_vlm_client(_fresh_state()),
    )
    monkeypatch.setenv("NETSENTINEL_OPENAI_API_KEY", SECRET_ENV)
    cfg = Config(vlm_api_keys={"glm": SECRET_CFG}, vlm_cache_db=str(tmp_path / "c.db"))
    blobs: list[str] = []
    for argv in (["list"], ["models", "glm"], ["ping", "glm", "--offline"], ["doctor"]):
        vlmctl.main(argv, cfg=cfg)
        out, err = capsys.readouterr()
        blobs += [out, err]
    # 真实 ping 路径同样不得回显
    vlmctl.main(["ping", "glm"], cfg=Config(vlm_online=True, vlm_api_keys={"glm": SECRET_CFG}, vlm_cache_db=str(tmp_path / "c.db")))
    out, err = capsys.readouterr()
    blobs += [out, err]
    blob = "\n".join(blobs)
    assert SECRET_CFG not in blob
    assert SECRET_ENV not in blob


# ---------------------------------------------------------------------------
# 兄弟模块未就位 / 参数错误
# ---------------------------------------------------------------------------


def test_providers_missing_gives_chinese_error(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", None)  # 强制"未就位"
    rc = vlmctl.main(["list"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "providers" in err and "A61" in err


def test_ping_vlm_client_missing_gives_chinese_error(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", _make_providers())
    monkeypatch.setitem(sys.modules, "netsentinel.vision.vlm_client", None)
    rc = vlmctl.main(["ping", "glm", "--offline"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "vlm_client" in err


def test_models_catalog_missing_gives_chinese_error(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", _make_providers())
    monkeypatch.setitem(sys.modules, "netsentinel.vision.model_catalog", None)
    rc = vlmctl.main(["models", "glm"], cfg=Config())
    _, err = capsys.readouterr()
    assert rc == 1
    assert "未就位" in err and "model_catalog" in err


def test_no_args_is_chinese_error(capsys):
    assert vlmctl.main([], cfg=Config()) == 1
    _, err = capsys.readouterr()
    assert "参数错误" in err


def test_unknown_command_is_chinese_error(capsys):
    assert vlmctl.main(["frobnicate"], cfg=Config()) == 1
    _, err = capsys.readouterr()
    assert "参数错误" in err


# ---------------------------------------------------------------------------
# 入口杂项
# ---------------------------------------------------------------------------


def test_main_loads_default_config_when_absent(monkeypatch, capsys, tmp_path):
    """cfg 未注入时读 ./config.yaml;文件不存在则回落默认配置。"""
    _install_siblings(monkeypatch, providers=_make_providers())
    monkeypatch.chdir(tmp_path)  # 空目录:无 config.yaml
    rc = vlmctl.main(["list"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "共 20 个提供方" in out


def test_embedded_1x1_png_is_valid_header():
    # 内嵌测试图是合法 PNG 魔数 + IHDR 尺寸 1x1
    assert vlmctl._MINI_PNG.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in vlmctl._MINI_PNG
    import struct as _struct

    width, height = _struct.unpack(">II", vlmctl._MINI_PNG[16:24])
    assert (width, height) == (1, 1)


# ---------------------------------------------------------------------------
# V5 升级:密钥表单次获取 / 遥测接入 / 慢路径告警
# ---------------------------------------------------------------------------


def _make_counting_keys(state: dict) -> types.ModuleType:
    """A70 security.keys 桩:统计 configured() 被调次数(V5 性能锁定用)。"""
    mod = types.ModuleType("netsentinel.security.keys")

    def configured(cfg):
        state["configured"] = state.get("configured", 0) + 1
        return {"openai": True}

    mod.configured = configured
    mod.get_key = lambda provider, cfg: "mock-key-value" if provider == "openai" else ""
    return mod


def test_v5_list_and_doctor_fetch_keys_map_once(monkeypatch, capsys):
    """V5 性能:security.keys.configured 整表只取一次,而非逐提供方各调一次。

    旧实现对 20 家目录逐家调用 configured()(每次又遍历全目录 20 家
    get_key)≈ 400 次 get_key;现在每条命令恰好 1 次(≈ 20 次 get_key)。
    """
    state: dict = {}
    _install_siblings(monkeypatch, providers=_make_providers(), keys=_make_counting_keys(state))
    rc = vlmctl.main(["list"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state["configured"] == 1  # 整条 list 命令只调一次(旧实现为 20)
    # 布尔判定结果不变:openai 命中映射,gemini 回退 cfg/环境变量判定
    rows = _provider_rows(out)
    assert next(r for r in rows if r.startswith("openai")).rstrip().endswith("✓已配置")
    assert next(r for r in rows if r.startswith("gemini")).rstrip().endswith("✗")

    rc = vlmctl.main(["doctor"], cfg=Config())
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert state["configured"] == 2  # doctor 再取一次(旧实现累计 40)
    assert len(_provider_rows(out)) == 20


def test_v5_telemetry_records_subcommands(monkeypatch, capsys):
    """V5 可观测:各子命令耗时入 telemetry.vlmctl.<命令>,错误计 vlmctl.errors。"""
    telemetry.reset()
    _install_siblings(monkeypatch, providers=_make_providers(), model_catalog=_make_model_catalog())
    assert vlmctl.main(["list"], cfg=Config()) == 0
    assert vlmctl.main(["models", "glm"], cfg=Config()) == 0
    assert vlmctl.main(["models", "no-such-provider"], cfg=Config()) == 1  # 错误路径
    capsys.readouterr()
    snap = telemetry.snapshot()
    assert snap["timers"]["vlmctl.list"]["count"] == 1
    # models 成功 + 失败各一次(finally 记时,失败同样计时)
    assert snap["timers"]["vlmctl.models"]["count"] == 2
    assert snap["counters"]["vlmctl.errors"] == 1


def test_v5_ping_latency_reuses_telemetry(tmp_path, monkeypatch, capsys):
    """V5 可观测:ping 延迟样本记入 telemetry.vlmctl.ping_latency,不自造统计。"""
    telemetry.reset()
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(vlm_online=True, vlm_api_keys={"glm": "mock"}, vlm_cache_db=str(tmp_path / "c.db"))
    rc = vlmctl.main(["ping", "glm:glm-5.3-flash"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    snap = telemetry.snapshot()
    assert snap["counters"]["vlmctl.ping"] == 1  # 外呼计数
    stats = snap["timers"]["vlmctl.ping_latency"]
    assert stats["count"] == 1  # 延迟样本恰好一份
    assert stats["avg_ms"] >= 0.0
    assert snap["timers"]["vlmctl.ping"]["count"] == 1  # 子命令整体计时
    assert "延迟" in out


def test_v5_slow_command_logs_warning(monkeypatch, capsys, caplog):
    """V5 可观测:子命令超过 _SLOW_COMMAND_S 秒记 WARNING(阈值压 0 触发)。"""
    _install_siblings(monkeypatch, providers=_make_providers())
    monkeypatch.setattr(vlmctl, "_SLOW_COMMAND_S", 0.0)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.vlmctl"):
        rc = vlmctl.main(["list"], cfg=Config())
    capsys.readouterr()
    assert rc == 0
    assert any("耗时" in record.message and "list" in record.message for record in caplog.records)


def test_v5_doctor_probe_uses_concurrent_local_status(tmp_path, monkeypatch, capsys):
    """V5 性能:doctor --probe 对支持 workers 的 local_gateway 传并发参数。

    旧桩(签名无 workers)自动保守串行——既有用例已锁定;本用例锁定
    真模块(A66 V5)接入后的并发分支被正确启用。
    """
    state = _fresh_state()
    seen: dict = {}

    mod = types.ModuleType("netsentinel.vision.local_gateway")

    def local_status(cfg, *, timeout=2.0, workers=1):  # V5 真模块形态
        seen["workers"] = workers
        return {"ollama": {"ok": True, "models": ["llava"], "error": ""}}

    mod.local_status = local_status
    mod.LOCAL_PROVIDERS = ("ollama", "vllm", "lmstudio", "xinference")
    _install_siblings(
        monkeypatch,
        providers=_make_providers(),
        vlm_client=_make_vlm_client(state),
        local_gateway=mod,
    )
    cfg = Config(
        vlm_online=True,
        vlm_api_keys={"openai": "mock"},
        vlm_provider="openai",
        vlm_cache_db=str(tmp_path / "c.db"),
    )
    rc = vlmctl.main(["doctor", "--probe"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert seen["workers"] == 4  # 四家并发
    assert "在线" in out


# ---------------------------------------------------------------------------
# A243:ping 旁路成本落账(默认关;桩客户端不自落账时由本层补记)
# ---------------------------------------------------------------------------


def _ledger_path(tmp_path) -> pathlib.Path:
    """ping 落账账本路径:<data_dir>/vlm_cost.jsonl(与 finishflow/webui 同口径)。"""
    return pathlib.Path(tmp_path) / "data" / "vlm_cost.jsonl"


def _ledger_rows(tmp_path) -> list[dict]:
    path = _ledger_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_a243_ping_ledger_default_off_zero_rows(tmp_path, monkeypatch, capsys):
    """开关缺省关:ping 成功后零落账(现状向后兼容,账本文件不创建)。"""
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(
        vlm_online=True, vlm_api_keys={"glm": "mock"},
        vlm_cache_db=str(tmp_path / "c.db"), data_dir=str(tmp_path / "data"),
    )
    assert vlmctl.main(["ping", "glm:glm-5.3-flash"], cfg=cfg) == 0
    capsys.readouterr()
    assert _ledger_rows(tmp_path) == []


def test_a243_ping_ledger_enabled_records_one_row(tmp_path, monkeypatch, capsys):
    """开启(附加属性)后:ping 成功由本层补记恰好一条,维度与口径正确。"""
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(
        vlm_online=True, vlm_api_keys={"glm": "mock"},
        vlm_cache_db=str(tmp_path / "c.db"), data_dir=str(tmp_path / "data"),
    )
    cfg.cost_ledger_enabled = True   # 附加实例属性开关(非配置文件键)
    cfg.run_id = "run-vlmctl-a243"   # A232 指引 ②:批次标识沿 cfg 附加属性透传
    assert vlmctl.main(["ping", "glm:glm-5.3-flash"], cfg=cfg) == 0
    capsys.readouterr()
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "glm" and row["model"] == "glm-5.3-flash"
    assert row["images"] == 1  # ping 恰好送审 1 张测试图
    assert row["run_id"] == "run-vlmctl-a243"
    assert isinstance(row["duration_s"], float) and row["duration_s"] >= 0.0
    # PRICE_HINTS glm:glm-5.3-flash = 5.0 元/千次 × 1 图 → 两位小数 0.01
    assert row["est_cost"] == 0.01
    assert "tokens" not in row  # ping 侧拿不到响应 usage(客户端内部消化),诚实不落键


def test_a243_ping_ledger_skipped_when_client_self_ledgers(tmp_path, monkeypatch, capsys):
    """客户端自带 _ledger_records_cost 标记(真实 UniversalVLMClient 形态):
    vlmctl 不补记(客户端层已落账),避免同一笔调用双记。"""
    state = _fresh_state()
    client_mod = _make_vlm_client(state)
    client_mod.UniversalVLMClient._ledger_records_cost = True  # 模拟真实客户端标记
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=client_mod)
    cfg = Config(
        vlm_online=True, vlm_api_keys={"glm": "mock"},
        vlm_cache_db=str(tmp_path / "c.db"), data_dir=str(tmp_path / "data"),
    )
    cfg.cost_ledger_enabled = True
    cfg.run_id = "run-vlmctl-a243"
    assert vlmctl.main(["ping", "glm"], cfg=cfg) == 0
    capsys.readouterr()
    assert state["chat_json"] == 1  # 调用本身照常成功
    assert _ledger_rows(tmp_path) == []  # 本层不落(由客户端层负责)


def test_a243_ping_ledger_failure_never_breaks_ping(tmp_path, monkeypatch, capsys):
    """旁路红线:记账抛异常 → 静默降级 + 计数 vision.cost_log.failed,
    ping 返回码与结论输出不受任何影响。"""
    from netsentinel.vision import cost_meter as cost_meter_mod
    from netsentinel.vision import vlmctl as vlmctl_mod

    def _boom(self, *args, **kwargs):
        raise RuntimeError("mock:账盘只读,记账失败")

    monkeypatch.setattr(cost_meter_mod.CostMeter, "record", _boom)
    state = _fresh_state()
    _install_siblings(monkeypatch, providers=_make_providers(), vlm_client=_make_vlm_client(state))
    cfg = Config(
        vlm_online=True, vlm_api_keys={"glm": "mock"},
        vlm_cache_db=str(tmp_path / "c.db"), data_dir=str(tmp_path / "data"),
    )
    cfg.cost_ledger_enabled = True
    telemetry.reset()
    rc = vlmctl_mod.main(["ping", "glm"], cfg=cfg)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "✅" in out  # 结论照常
    assert telemetry.snapshot()["counters"]["vision.cost_log.failed"] == 1.0


# ---------------------------------------------------------------------------
# A243:config.example.yaml 往返断言(load_config 可解析、无未知键告警)
# ---------------------------------------------------------------------------

#: V11–V14 已收录字段(config.example.yaml 样例须与 Config 默认值一字不差)
_V11_V14_KEYS = (
    "graph_wire", "gang_mode", "gang_weight_threshold", "gang_template_weight_factor",
    "gang_resolution", "ensemble_reliability_weights", "cascade_risk_budget",
    "abstain_threshold", "bundle_sign_algo", "ed25519_seed_hex", "tsa_url",
    "trace_enabled", "abstain_enabled", "dynamic_ttl", "phash_mt_lsh_db",
    "guard_model_path", "guard_family", "bayes_reliability", "bayes_half_life",
)


def test_a243_config_example_loads_without_unknown_keys(tmp_path, caplog):
    """仓库示例配置可被 load_config 解析:零未知键告警;V11–V14 样例值与
    contracts.py 默认值一字不差(含类型);save→load 往返等值。"""
    from netsentinel.config import _reset_unknown_key_warnings, load_config, save_config

    example = pathlib.Path(vlmctl.__file__).resolve().parents[2] / "config.example.yaml"
    assert example.is_file(), f"仓库缺少示例配置:{example}"
    _reset_unknown_key_warnings()
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        cfg = load_config(str(example))
    assert not [r for r in caplog.records if "未知配置键" in r.getMessage()]
    defaults = Config()
    for name in _V11_V14_KEYS:
        got, want = getattr(cfg, name), getattr(defaults, name)
        assert got == want, f"{name}:样例值 {got!r} 应等于默认值 {want!r}"
        assert type(got) is type(want), f"{name}:类型应一致({type(got).__name__})"
    # 往返断言:示例加载结果 save 后再 load 等值(可安全作为运营者起始配置)
    target = tmp_path / "roundtrip.yaml"
    save_config(cfg, str(target))
    assert load_config(str(target)) == cfg
