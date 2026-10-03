"""webui/models_page.py 纯逻辑层测试(A161,离线,零外呼,不依赖 streamlit)。

- 只测纯函数(model_rows / switch_banner / key_status / can_switch)及
  展示常量:本文件顶部成功 import 即证明纯逻辑层可独立导入(本机未装
  streamlit 时同样成立);
- 密钥隔离:monkeypatch 清空全部提供方环境变量 + 把 ``pathlib.Path.home``
  指向 tmp 目录(隔离 ``~/.netsentinel/keys``),再向 ``cfg.vlm_api_keys``
  注入密钥,断言输出里**只有掩码、绝无密钥本体**(红线 33);
- key_status 的惰性/容错分支用 monkeypatch 替换 A70 接口(configured 抛错、
  返回坏形态、get_key 失败、模块置 None)验证降级路径;
- 全程不联网、不探测端口、不启动 streamlit 服务、不写活动模型状态文件。
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

import netsentinel.security
from netsentinel.contracts import Config
from netsentinel.security import keys as real_keys
from webui import models_page
from webui.models_page import (
    BANNER_NONE,
    MAX_MODELS_SHOWN,
    STUB_SPEC,
    can_switch,
    key_status,
    model_rows,
    switch_banner,
)

# ---------------------------------------------------------------------------
# 公共辅助:环境与家目录隔离(与 test_providers_page.py 同款口径)
# ---------------------------------------------------------------------------

_ALL_PROVIDERS = (
    "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
    "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
    "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
)

_ROW_KEYS = {"provider", "port", "base_url", "vision_count", "models", "is_active"}


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 pathlib.Path.home() 指向 tmp 目录,隔离真实家目录与密钥文件。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


@pytest.fixture()
def clean_env(home: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部提供方密钥环境变量(约定两形态 + 目录 key_envs 全覆盖)。"""
    for name in _ALL_PROVIDERS:
        upper = name.upper()
        monkeypatch.delenv(f"NETSENTINEL_{upper}_API_KEY", raising=False)
        monkeypatch.delenv(f"{upper}_API_KEY", raising=False)
    for extra in ("DASHSCOPE_API_KEY", "ARK_API_KEY", "QIANFAN_API_KEY"):
        monkeypatch.delenv(extra, raising=False)


def _secret(tag: str) -> str:
    """构造形似真实密钥的注入用密钥值(只用于断言"绝不回显")。"""
    return f"sk-A161-{tag}-0000000000000000"


def _scan_row(
    provider: str = "ollama",
    port: str = "11434",
    models: list[str] | None = None,
    *,
    ok: bool = True,
    error: str = "",
) -> dict:
    """构造 A143 LocalVisionScanner.scan() 形态的行(七键)。"""
    return {
        "provider": provider,
        "port": port,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "models": models if models is not None else [],
        "ok": ok,
        "error": error,
        "unfiltered": False,
    }


# ---------------------------------------------------------------------------
# model_rows:字段 / is_active 两判式 / 截断 / 离线过滤 / 空扫描 / 鸭子容错
# ---------------------------------------------------------------------------


def test_model_rows_exact_keys_and_field_types() -> None:
    """在线行固定六键,值类型规整(计数 int / 模型 list[str] / 活动 bool)。"""
    rows = model_rows([_scan_row(models=["llava:13b", "qwen2.5vl:7b"])])
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["provider"] == "ollama"
    assert row["port"] == "11434"
    assert row["base_url"] == "http://127.0.0.1:11434/v1"
    assert row["vision_count"] == 2
    assert row["models"] == ["llava:13b", "qwen2.5vl:7b"]
    assert isinstance(row["is_active"], bool)
    assert row["is_active"] is False  # 未传 active


def test_model_rows_is_active_first_model_spec() -> None:
    """判式一:active == "{provider}:{首个视觉模型}" → 该行标记为活动。"""
    rows = model_rows(
        [
            _scan_row(provider="ollama", port="11434", models=["llava:13b"]),
            _scan_row(provider="vllm", port="8000", models=["qwen2.5vl:7b"]),
        ],
        active="ollama:llava:13b",
    )
    assert [r["is_active"] for r in rows] == [True, False]


def test_model_rows_is_active_provider_only() -> None:
    """判式二:active == provider(目录默认模型写法)→ 该行标记为活动。"""
    rows = model_rows(
        [_scan_row(provider="ollama", models=["llava:13b"])], active="ollama"
    )
    assert rows[0]["is_active"] is True


def test_model_rows_is_active_negative_for_other_specs() -> None:
    """非首模型 / 其他提供方的 spec 不命中任何行(保持 False)。"""
    rows = model_rows(
        [_scan_row(provider="ollama", models=["llava:13b", "minicpm-v"])],
        active="ollama:minicpm-v",  # 只认首个视觉模型
    )
    assert rows[0]["is_active"] is False
    assert model_rows([_scan_row(models=["llava:13b"])], active="vllm")[0]["is_active"] is False


def test_model_rows_models_truncated_to_five_with_full_count() -> None:
    """models 只展示前 5 个;vision_count 始终是全量数。"""
    many = [f"model-{i}" for i in range(1, 8)]  # 7 个视觉模型
    row = model_rows([_scan_row(models=many)])[0]
    assert len(row["models"]) == MAX_MODELS_SHOWN == 5
    assert row["models"] == many[:5]
    assert row["vision_count"] == 7


def test_model_rows_skips_offline_rows_keeps_empty_online() -> None:
    """离线(ok=False)行不进表;在线但无视觉模型的行保留(vision_count=0)。"""
    rows = model_rows(
        [
            _scan_row(provider="lmstudio", port="1234", models=[], ok=True),
            _scan_row(provider="vllm", port="8000", models=["qwen2.5vl:7b"], ok=False, error="无法连接"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["provider"] == "lmstudio"
    assert rows[0]["vision_count"] == 0
    assert rows[0]["models"] == []
    assert rows[0]["is_active"] is False
    # 无模型行仍可被 provider 写法判为活动
    assert model_rows(
        [_scan_row(provider="lmstudio", models=[])], active="lmstudio"
    )[0]["is_active"] is True


def test_model_rows_empty_or_missing_scan() -> None:
    """空扫描 / None → [](页面显示"未发现"提示,不抛错)。"""
    assert model_rows([]) == []
    assert model_rows(None) == []
    assert model_rows([], active="ollama:llava") == []


def test_model_rows_duck_typed_entries_and_messy_models() -> None:
    """鸭子容错:行可为对象;模型列表里的 None/空串/重复/空白项被清洗。"""
    duck = SimpleNamespace(
        provider="xinference",
        port="9997",
        base_url="http://127.0.0.1:9997/v1",
        models=[None, "llava:13b", "", "  llava:13b  ", "moondream"],
        ok=True,
        error="",
    )
    rows = model_rows([duck], active="xinference:llava:13b")  # type: ignore[arg-type]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == _ROW_KEYS
    assert row["models"] == ["llava:13b", "moondream"]
    assert row["vision_count"] == 2
    assert row["is_active"] is True  # 清洗后的首个模型参与判式
    # provider 为空 / ok 缺失的行直接跳过,不抛错
    assert model_rows([{"port": "1"}, {"provider": "x", "ok": 0}]) == []


# ---------------------------------------------------------------------------
# switch_banner:两态
# ---------------------------------------------------------------------------


def test_switch_banner_active_model() -> None:
    """已连接 → "当前视觉模型:{spec}"。"""
    assert switch_banner("ollama:llava") == "当前视觉模型:ollama:llava"
    assert switch_banner(STUB_SPEC) == "当前视觉模型:stub"
    assert switch_banner("  glm:glm-4.5v ") == "当前视觉模型:glm:glm-4.5v"


def test_switch_banner_none_blank_and_whitespace() -> None:
    """未连接(None / 空白)→ 固定向导指引文案。"""
    assert switch_banner(None) == BANNER_NONE
    assert switch_banner("") == BANNER_NONE
    assert switch_banner("   ") == BANNER_NONE
    assert "尚未连接视觉模型" in BANNER_NONE
    assert "python -m netsentinel.modelmgr serve" in BANNER_NONE


# ---------------------------------------------------------------------------
# key_status:掩码无本体 / 全未配置 / A70 缺席与异常降级
# ---------------------------------------------------------------------------


def test_key_status_masked_without_secret_leak(clean_env) -> None:
    """已配置 → 前 4 位掩码;输出序列化后绝无密钥本体(红线 33)。"""
    secret = _secret("CFG")
    cfg = Config()
    cfg.vlm_api_keys = {"qwen": secret}
    status = key_status(cfg)
    assert set(status) == set(real_keys.configured(cfg))
    assert status["qwen"] == secret[:4] + "****"
    assert status["glm"] is None  # 其余未配置 → None
    # 红线 33 专项:任何值里都不含密钥本体(全表序列化排查)
    dumped = repr(status)
    assert secret not in dumped
    assert all(v is None or (isinstance(v, str) and v.endswith("****")) for v in status.values())


def test_key_status_all_none_on_clean_environment(clean_env) -> None:
    """干净环境下全部 20 家 → None(已配置 0 家)。"""
    status = key_status(Config())
    assert len(status) == len(_ALL_PROVIDERS)
    assert set(status.values()) == {None}


def test_key_status_without_keys_module_returns_empty(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A70 模块缺席(monkeypatch 置 None)→ {}(页面降级提示,不抛错)。"""
    monkeypatch.setattr(netsentinel.security, "keys", None, raising=False)
    assert key_status(Config()) == {}


def test_key_status_tolerates_configured_failure_and_bad_shape(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """configured 抛异常 / 返回非 dict / 返回 None → {}(鸭子容错,页面不崩)。"""
    def _boom(cfg: object) -> dict:
        raise RuntimeError("A70 内部异常")

    monkeypatch.setattr(real_keys, "configured", _boom)
    assert key_status(Config()) == {}
    monkeypatch.setattr(real_keys, "configured", lambda cfg: ["不是", "字典"])
    assert key_status(Config()) == {}
    monkeypatch.setattr(real_keys, "configured", lambda cfg: None)
    assert key_status(Config()) == {}


def test_key_status_masks_even_when_get_key_fails(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """configured 为真但 get_key 失败 / 返回空 → 仍只给纯掩码 "****",绝不猜值。"""
    def _unreadable(provider: str, cfg: object) -> str:
        raise OSError("密钥文件读不了")

    def _empty(provider: str, cfg: object) -> str:
        return ""

    monkeypatch.setattr(real_keys, "configured", lambda cfg: {"qwen": True, "glm": True})
    monkeypatch.setattr(real_keys, "get_key", _unreadable)
    assert key_status(Config()) == {"qwen": "****", "glm": "****"}
    monkeypatch.setattr(real_keys, "get_key", _empty)
    assert key_status(Config()) == {"qwen": "****", "glm": "****"}


# ---------------------------------------------------------------------------
# can_switch:三分支(stub / parse_spec 通过 / 失败)
# ---------------------------------------------------------------------------


def test_can_switch_stub_variants() -> None:
    """分支一:离线桩 stub(含空白)→ True(离线桩永远可用,红线 34)。"""
    assert can_switch("stub") is True
    assert can_switch(" stub ") is True
    assert can_switch(STUB_SPEC) is True


def test_can_switch_valid_specs() -> None:
    """分支二:parse_spec 通过(提供方:模型 / 仅提供方)→ True。"""
    assert can_switch("ollama:llava") is True
    assert can_switch("glm:glm-4.5v") is True
    assert can_switch("glm") is True  # 目录默认模型写法


def test_can_switch_invalid_or_empty_specs() -> None:
    """分支三:未知提供方 / 空 / 非字符串 → False。"""
    assert can_switch("nope:model") is False
    assert can_switch("") is False
    assert can_switch("   ") is False
    assert can_switch(None) is False
    assert can_switch(123) is False  # type: ignore[arg-type]


def test_can_switch_without_parse_spec(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A61 目录在但 parse_spec 缺席 / 目录缺席 → False(惰性降级,不抛错)。"""
    from netsentinel.vision import providers as real_providers

    monkeypatch.setattr(real_providers, "parse_spec", None)
    assert can_switch("ollama:llava") is False
    assert can_switch("stub") is True  # stub 分支不依赖 parse_spec


# ---------------------------------------------------------------------------
# UI 守卫:streamlit 缺失时可导入,缺失时 main 打印中文提示退出码 1
# ---------------------------------------------------------------------------


def test_module_importable_without_streamlit() -> None:
    # 顶部已成功 import:即证明纯逻辑层不依赖 streamlit。
    assert isinstance(models_page._HAS_ST, bool)
    assert callable(models_page.render)
    assert callable(models_page.main)
    assert callable(model_rows)
    assert callable(switch_banner)
    assert callable(key_status)
    assert callable(can_switch)


def test_main_without_streamlit_prints_hint_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    if models_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    assert models_page.main() == 1
    err = capsys.readouterr().err
    assert "Streamlit" in err and "pip install" in err
    assert "models_page" in err  # 提示里给出本页面的启动命令


def test_render_without_streamlit_raises() -> None:
    if models_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    with pytest.raises(RuntimeError, match="streamlit"):
        models_page.render()


def test_streamlit_ui_guard() -> None:
    pytest.importorskip("streamlit")
    assert models_page._HAS_ST is True
    assert callable(models_page.render)
