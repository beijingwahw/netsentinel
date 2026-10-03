# -*- coding: utf-8 -*-
"""A61 netsentinel.vision.providers 测试:提供方目录 / parse_spec / resolve。

全程离线(红线 20:测试零外呼):

- 目录完整性:20 项、方言合法、端点 http(s)、本地四家免密钥环境变量、
  值与契约 §2 表逐项一致;
- parse_spec 各形态(带模型 / 不带 / 空模型 / 模型内含冒号 / 未知项);
- resolve 三级覆盖优先级(cfg.vlm_provider_models / base_urls / api_keys >
  目录默认)、``提供方:模型`` 完整写法;
- 本地提供方必填模型校验(中文报错含 ollama:llava 示例);
- 密钥解析:cfg.vlm_api_keys > key_envs 环境变量顺序 > 空串;
  A70 security.keys 未就位 / 调用异常时的兜底路径;
- repr 不回显密钥(红线 17);导入零副作用(顶层不加载兄弟模块)。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.vision import providers
from netsentinel.vision.providers import (
    LOCAL_PROVIDERS,
    PROVIDERS,
    STYLES,
    ResolvedProvider,
    is_local,
    parse_spec,
    provider_names,
    resolve,
)

#: 契约 §2 表的 20 个提供方(键名逐一核对)
EXPECTED_PROVIDERS = {
    "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
    "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
    "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
}

#: 目录里出现过的全部密钥环境变量名(测试前统一清空,隔离宿主机环境)
ALL_KEY_ENVS = [env for spec in PROVIDERS.values() for env in spec.key_envs]

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空所有提供方密钥环境变量,保证测试不受宿主机环境污染。"""
    for env in ALL_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """把 Path.home() 指到临时目录,隔离真实 ~/.netsentinel/keys 密钥文件。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------------------
# 目录完整性
# ---------------------------------------------------------------------------


def test_catalog_has_exactly_20_providers() -> None:
    assert len(PROVIDERS) == 20
    assert set(PROVIDERS) == EXPECTED_PROVIDERS
    assert len(provider_names()) == 20
    assert set(provider_names()) == EXPECTED_PROVIDERS


def test_catalog_styles_valid() -> None:
    for name, spec in PROVIDERS.items():
        assert spec.style in STYLES, f"{name} 方言非法:{spec.style}"


def test_catalog_base_urls_are_http() -> None:
    for name, spec in PROVIDERS.items():
        assert spec.base_url.startswith(("http://", "https://")), f"{name} 端点非法"
        assert not spec.base_url.endswith("/"), f"{name} 端点不应带尾斜杠"


def test_catalog_local_four_need_no_key_envs() -> None:
    assert set(LOCAL_PROVIDERS) == {"ollama", "vllm", "lmstudio", "xinference"}
    for name in LOCAL_PROVIDERS:
        spec = PROVIDERS[name]
        assert spec.local is True
        assert spec.key_envs == [], f"本地提供方 {name} 不应有密钥环境变量"
        assert spec.base_url.startswith("http://127.0.0.1:")
    for name, spec in PROVIDERS.items():
        if name not in LOCAL_PROVIDERS:
            assert spec.local is False, f"{name} 不应标记为本地"
            assert spec.key_envs, f"云端提供方 {name} 缺少密钥环境变量"
            assert spec.default_model, f"云端提供方 {name} 缺少默认模型(提示值)"
        assert spec.notes, f"{name} 缺少中文备注"
        assert spec.key == name, f"{name} 条目 key 字段与字典键不一致"


def test_catalog_values_match_contract() -> None:
    """关键值与契约 §2 表逐项照抄(均为提示值,以官方文档为准)。"""
    assert PROVIDERS["glm"].base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert PROVIDERS["glm"].default_model == "glm-5.3-flash"
    assert PROVIDERS["glm"].key_envs == ["NETSENTINEL_GLM_API_KEY", "GLM_API_KEY"]
    assert PROVIDERS["openai"].base_url == "https://api.openai.com/v1"
    assert PROVIDERS["openai"].default_model == "gpt-4o-mini"
    assert PROVIDERS["anthropic"].style == "anthropic"
    assert PROVIDERS["anthropic"].base_url == "https://api.anthropic.com/v1"
    assert PROVIDERS["anthropic"].key_envs == [
        "NETSENTINEL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY",
    ]
    assert PROVIDERS["gemini"].style == "gemini"
    assert PROVIDERS["gemini"].base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert PROVIDERS["qwen"].base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert PROVIDERS["qwen"].key_envs == ["NETSENTINEL_QWEN_API_KEY", "DASHSCOPE_API_KEY"]
    assert PROVIDERS["doubao"].default_model == "doubao-1.5-vision-pro"
    assert PROVIDERS["hunyuan"].key_envs == ["NETSENTINEL_HUNYUAN_API_KEY"]
    assert PROVIDERS["minimax"].default_model == "MiniMax-VL-01"
    assert PROVIDERS["siliconflow"].default_model == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert PROVIDERS["ernie"].key_envs == ["NETSENTINEL_ERNIE_API_KEY", "QIANFAN_API_KEY"]
    assert PROVIDERS["openrouter"].default_model == "qwen/qwen2.5-vl-72b-instruct:free"
    assert PROVIDERS["groq"].base_url == "https://api.groq.com/openai/v1"
    assert PROVIDERS["xai"].base_url == "https://api.x.ai/v1"
    assert PROVIDERS["ollama"].base_url == "http://127.0.0.1:11434/v1"
    assert PROVIDERS["ollama"].default_model == "llava"
    assert PROVIDERS["vllm"].base_url == "http://127.0.0.1:8000/v1"
    assert PROVIDERS["lmstudio"].base_url == "http://127.0.0.1:1234/v1"
    assert PROVIDERS["xinference"].base_url == "http://127.0.0.1:9997/v1"
    for name in ("vllm", "lmstudio", "xinference"):
        assert PROVIDERS[name].default_model == "", f"{name} 目录不应预置默认模型"


# ---------------------------------------------------------------------------
# parse_spec
# ---------------------------------------------------------------------------


def test_parse_spec_with_model() -> None:
    assert parse_spec("openai:gpt-4o-mini") == ("openai", "gpt-4o-mini")
    assert parse_spec("qwen:qwen-vl-max") == ("qwen", "qwen-vl-max")


def test_parse_spec_without_model() -> None:
    assert parse_spec("anthropic") == ("anthropic", None)
    assert parse_spec("glm") == ("glm", None)


def test_parse_spec_strips_whitespace() -> None:
    assert parse_spec("  openai : gpt-4o-mini  ") == ("openai", "gpt-4o-mini")
    assert parse_spec(" ollama ") == ("ollama", None)


def test_parse_spec_empty_model_means_none() -> None:
    assert parse_spec("ollama:") == ("ollama", None)


def test_parse_spec_model_may_contain_colon() -> None:
    """openrouter 免费档模型名自带冒号:只按第一个冒号拆分。"""
    assert parse_spec("openrouter:qwen/qwen2.5-vl-72b-instruct:free") == (
        "openrouter",
        "qwen/qwen2.5-vl-72b-instruct:free",
    )


@pytest.mark.parametrize("bad", ["foo", "foo:bar", "", "  ", ":nope", "Glm"])
def test_parse_spec_unknown_provider_raises(bad: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_spec(bad)
    message = str(exc_info.value)
    assert "未知" in message
    # 中文错误里列出全部 20 个可用提供方
    for name in EXPECTED_PROVIDERS:
        assert name in message, f"错误消息缺少可用提供方 {name}"


# ---------------------------------------------------------------------------
# resolve:目录默认值
# ---------------------------------------------------------------------------


def test_resolve_defaults_from_catalog(isolated_home: pathlib.Path) -> None:
    resolved = resolve("openai", Config())
    assert isinstance(resolved, ResolvedProvider)
    assert resolved.provider == "openai"
    assert resolved.base_url == "https://api.openai.com/v1"
    assert resolved.model == "gpt-4o-mini"
    assert resolved.style == "openai"
    assert resolved.local is False
    assert resolved.api_key == ""  # 无任何来源时退化为空串,由传输层拒发
    assert resolve("gemini", Config()).style == "gemini"
    assert resolve("anthropic", Config()).model == "claude-sonnet-4"


def test_resolve_strips_trailing_slash_from_base_url(isolated_home: pathlib.Path) -> None:
    cfg = Config()
    cfg.vlm_provider_base_urls = {"openai": "https://proxy.example.com/v1/"}
    assert resolve("openai", cfg).base_url == "https://proxy.example.com/v1"


def test_resolve_unknown_provider_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve("nope", Config())
    assert "未知" in str(exc_info.value)
    assert "openai" in str(exc_info.value)


# ---------------------------------------------------------------------------
# resolve:cfg 三级覆盖 > 目录
# ---------------------------------------------------------------------------


def test_resolve_cfg_model_and_base_url_override_catalog(isolated_home: pathlib.Path) -> None:
    cfg = Config()
    cfg.vlm_provider_models = {"openai": "gpt-4o"}
    cfg.vlm_provider_base_urls = {"openai": "https://gateway.internal/v1"}
    resolved = resolve("openai", cfg)
    assert resolved.model == "gpt-4o"
    assert resolved.base_url == "https://gateway.internal/v1"


def test_resolve_cfg_api_key_beats_env(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    monkeypatch.setenv("NETSENTINEL_OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key-2")
    cfg = Config()
    cfg.vlm_api_keys = {"openai": "cfg-key"}
    assert resolve("openai", cfg).api_key == "cfg-key"


def test_resolve_spec_form_model_beats_cfg_default(isolated_home: pathlib.Path) -> None:
    """完整写法 qwen:my-vl 中的模型是更明确的当场选择,优先于配置覆盖。"""
    cfg = Config()
    cfg.vlm_provider_models = {"qwen": "cfg-model"}
    assert resolve("qwen:my-vl", cfg).model == "my-vl"


def test_resolve_ignores_other_providers_overrides(isolated_home: pathlib.Path) -> None:
    cfg = Config()
    cfg.vlm_provider_models = {"openai": "gpt-4o"}
    cfg.vlm_provider_base_urls = {"openai": "https://gateway.internal/v1"}
    resolved = resolve("glm", cfg)
    assert resolved.model == "glm-5.3-flash"
    assert resolved.base_url == "https://open.bigmodel.cn/api/paas/v4"


# ---------------------------------------------------------------------------
# 密钥:环境变量兜底顺序
# ---------------------------------------------------------------------------


def test_api_key_env_order_first_wins(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    monkeypatch.setenv("NETSENTINEL_GLM_API_KEY", "primary")
    monkeypatch.setenv("GLM_API_KEY", "secondary")
    assert resolve("glm", Config()).api_key == "primary"


def test_api_key_env_order_second_used(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    monkeypatch.setenv("GLM_API_KEY", "secondary")
    assert resolve("glm", Config()).api_key == "secondary"


def test_api_key_empty_when_no_source(isolated_home: pathlib.Path) -> None:
    assert resolve("hunyuan", Config()).api_key == ""


def test_api_key_fallback_when_keys_module_missing(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    """A70 security.keys 未就位:按 key_envs 顺序读 os.environ,再退化空串。"""
    monkeypatch.setattr(providers, "_load_get_key", lambda: None)
    cfg = Config()
    cfg.vlm_api_keys = {"openai": "cfg-key"}
    assert resolve("openai", cfg).api_key == "cfg-key"  # 兜底路径仍先看配置项
    monkeypatch.setenv("NETSENTINEL_OPENAI_API_KEY", "env-1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-2")
    cfg.vlm_api_keys = {}
    assert resolve("openai", cfg).api_key == "env-1"
    monkeypatch.delenv("NETSENTINEL_OPENAI_API_KEY")
    assert resolve("openai", cfg).api_key == "env-2"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert resolve("openai", cfg).api_key == ""


def test_api_key_fallback_when_get_key_raises(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    """A70 调用异常时不硬依赖:降级环境变量兜底。"""

    def _boom(provider: str, cfg: object) -> str:
        raise RuntimeError("A70 内部异常")

    monkeypatch.setattr(providers, "_load_get_key", lambda: _boom)
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "env-qwen")
    assert resolve("qwen", Config()).api_key == "env-qwen"


def test_delegates_to_security_keys_when_available(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    """A70 就位时惰性委托 get_key(本仓已落地,验证真实委托路径)。"""
    calls: list[str] = []

    def _fake_get_key(provider: str, cfg: object) -> str:
        calls.append(provider)
        return "delegated"

    monkeypatch.setattr(providers, "_load_get_key", lambda: _fake_get_key)
    assert resolve("moonshot", Config()).api_key == "delegated"
    assert calls == ["moonshot"]


# ---------------------------------------------------------------------------
# 本地提供方
# ---------------------------------------------------------------------------


def test_resolve_local_ollama_uses_default_model(isolated_home: pathlib.Path) -> None:
    resolved = resolve("ollama", Config())
    assert resolved.local is True
    assert resolved.model == "llava"
    assert resolved.api_key == ""
    assert resolved.base_url == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("provider", ["vllm", "lmstudio", "xinference"])
def test_resolve_local_requires_model(provider: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve(provider, Config())
    message = str(exc_info.value)
    assert "本地提供方必须指定模型" in message
    assert "ollama:llava" in message
    assert provider in message


def test_resolve_local_model_via_cfg_override(
    monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    cfg = Config()
    cfg.vlm_provider_models = {"vllm": "Qwen2.5-VL-7B-Instruct"}
    resolved = resolve("vllm", cfg)
    assert resolved.model == "Qwen2.5-VL-7B-Instruct"
    assert resolved.local is True
    # 本地提供方免密钥:即使误配了密钥也不带出
    cfg.vlm_api_keys = {"vllm": "should-be-ignored"}
    assert resolve("vllm", cfg).api_key == ""


def test_resolve_local_model_via_spec_form(isolated_home: pathlib.Path) -> None:
    resolved = resolve("lmstudio:qwen2-vl-7b", Config())
    assert resolved.model == "qwen2-vl-7b"
    assert resolved.provider == "lmstudio"
    assert resolved.local is True


def test_is_local() -> None:
    for name in ("ollama", "vllm", "lmstudio", "xinference"):
        assert is_local(name) is True
    for name in ("glm", "openai", "anthropic", "gemini", "qwen"):
        assert is_local(name) is False
    assert is_local("nope") is False
    assert is_local("") is False


def test_provider_names_cover_catalog() -> None:
    names = provider_names()
    assert isinstance(names, list)
    assert set(names) == EXPECTED_PROVIDERS
    assert all(is_local(name) is True for name in LOCAL_PROVIDERS)


# ---------------------------------------------------------------------------
# 红线 17:密钥不落 repr / 错误消息;零副作用导入
# ---------------------------------------------------------------------------


def test_repr_masks_api_key(monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path) -> None:
    monkeypatch.setenv("NETSENTINEL_OPENAI_API_KEY", "sk-super-secret-value")
    resolved = resolve("openai", Config())
    assert resolved.api_key == "sk-super-secret-value"
    assert "sk-super-secret-value" not in repr(resolved)
    assert "sk-super-secret-value" not in str(resolved)
    assert "已配置" in repr(resolved)
    empty = resolve("hunyuan", Config())
    assert "未配置" in repr(empty)


def test_import_has_no_side_effects_on_siblings() -> None:
    """顶层不加载 vision/security 兄弟模块(防循环导入,一切惰性)。"""
    code = (
        "import sys\n"
        "import netsentinel.vision.providers\n"
        "bad = [m for m in sys.modules\n"
        "       if (m.startswith('netsentinel.security')\n"
        "           or (m.startswith('netsentinel.vision.')\n"
        "               and m != 'netsentinel.vision.providers'))]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# V5 升级(A89):resolve 热点 LRU 缓存(密钥绝不缓存)+ 遥测计数
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_resolve_cache() -> None:
    """每个 V5 缓存用例前后清空模块级 LRU,隔离其余用例留下的缓存项。"""
    providers._resolve_static.cache_clear()
    yield
    providers._resolve_static.cache_clear()


def _counter(name: str) -> float:
    return telemetry.snapshot()["counters"].get(name, 0.0)


def test_v5_resolve_cache_hit_reuses_static_parts(
    fresh_resolve_cache: None, isolated_home: pathlib.Path
) -> None:
    """同键第二次 resolve 命中 LRU:静态部分一致,telemetry 记 providers.resolve_cached。"""
    first = resolve("qwen", Config())
    # 计数器与缓存为会话级共享:一律用前后差值断言,不受其他用例累计影响
    cached_before = _counter("providers.resolve_cached")
    hits_before = providers._resolve_static.cache_info().hits
    second = resolve("qwen", Config())
    assert (second.base_url, second.model, second.style, second.local) == (
        first.base_url, first.model, first.style, first.local,
    )
    assert _counter("providers.resolve_cached") - cached_before == 1.0
    assert providers._resolve_static.cache_info().hits - hits_before == 1
    # 缓存容量受 RESOLVE_CACHE_MAX 约束(≤64)
    assert providers._resolve_static.cache_info().currsize <= providers.RESOLVE_CACHE_MAX


def test_v5_resolve_cache_never_caches_api_key(
    monkeypatch: pytest.MonkeyPatch, fresh_resolve_cache: None, isolated_home: pathlib.Path
) -> None:
    """缓存命中路径的 api_key 仍每次现取:密钥来源变化即时生效(绝不被缓存)。"""
    returns = iter(["key-first", "key-second", "key-third"])

    def _rotating_get_key(provider: str, cfg: object) -> str:
        return next(returns)

    monkeypatch.setattr(providers, "_load_get_key", lambda: _rotating_get_key)
    assert resolve("openai", Config()).api_key == "key-first"   # 未命中:现取
    assert resolve("openai", Config()).api_key == "key-second"  # 命中缓存:仍现取
    assert resolve("openai", Config()).api_key == "key-third"   # 再命中:再现取
    # 缓存值里确实没有密钥:缓存只存 (base_url, model, style, local, key_envs)
    cached = providers._resolve_static("openai", "", "")
    assert len(cached) == 5
    assert all("key" not in str(part) for part in cached[:4])


def test_v5_resolve_cache_invalidates_on_cfg_override_change(
    fresh_resolve_cache: None, isolated_home: pathlib.Path
) -> None:
    """缓存键包含 cfg 覆盖项:覆盖一变即落入新键,拿到新值而非陈旧缓存。"""
    cfg = Config()
    first = resolve("glm", cfg)
    assert first.model == "glm-5.3-flash"
    cfg.vlm_provider_models = {"glm": "glm-5.3"}
    cfg.vlm_provider_base_urls = {"glm": "https://proxy.example.com/v4/"}
    second = resolve("glm", cfg)
    assert second.model == "glm-5.3"
    assert second.base_url == "https://proxy.example.com/v4"
    assert second.style == first.style  # 未覆盖的部分仍来自同一缓存结构
    # 两次不同键各自入缓存:互相不污染
    assert resolve("glm", Config()).model == "glm-5.3-flash"


def test_v5_resolve_cache_bounded_lru(
    monkeypatch: pytest.MonkeyPatch, fresh_resolve_cache: None, isolated_home: pathlib.Path
) -> None:
    """LRU 容量 ≤ RESOLVE_CACHE_MAX:塞入远超容量的组合后 currsize 仍受上限约束。"""
    monkeypatch.setattr(providers, "_load_get_key", lambda: None)
    for i in range(providers.RESOLVE_CACHE_MAX + 40):
        resolve("openai", Config(vlm_provider_models={"openai": f"m-{i}"}))
    info = providers._resolve_static.cache_info()
    assert info.currsize <= providers.RESOLVE_CACHE_MAX
    assert info.maxsize == providers.RESOLVE_CACHE_MAX


def test_v5_resolve_cache_does_not_cache_failures(
    fresh_resolve_cache: None, isolated_home: pathlib.Path
) -> None:
    """解析失败(ValueError)不写缓存:失败后补上模型立即可解析,无陈旧负结果。"""
    with pytest.raises(ValueError, match="本地提供方必须指定模型"):
        resolve("vllm", Config())
    assert providers._resolve_static.cache_info().currsize == 0  # 失败未入缓存
    cfg = Config()
    cfg.vlm_provider_models = {"vllm": "Qwen2.5-VL-7B"}
    assert resolve("vllm", cfg).model == "Qwen2.5-VL-7B"  # 新键正常求值
    assert providers._resolve_static.cache_info().currsize == 1


def test_v5_resolve_telemetry_counters() -> None:
    """providers.resolve / providers.errors 计数:成功累加总数,失败另计错误。"""
    total_before = _counter("providers.resolve")
    errors_before = _counter("providers.errors")
    resolve("ollama", Config())
    resolve("ollama:llava", Config())
    assert _counter("providers.resolve") - total_before == 2.0
    assert _counter("providers.errors") - errors_before == 0.0
    before_error = _counter("providers.resolve")
    with pytest.raises(ValueError):
        resolve("no-such-provider", Config())
    with pytest.raises(ValueError):
        resolve("vllm", Config())
    assert _counter("providers.resolve") - before_error == 2.0
    assert _counter("providers.errors") - errors_before == 2.0
