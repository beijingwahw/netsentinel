"""A70:netsentinel.security.keys 单元测试(离线,只写 tmp_path + monkeypatch)。

目录(A61 providers.py)可能就位也可能未就位:凡依赖目录行为的用例一律
monkeypatch ``keys._load_catalog`` 注入受控目录,保证两种并行态下结论一致;
另设真实目录态的健壮性用例(只断言类型 / 形态,不锁具体内容)。

P4 安全审计清偿新增(test_p4_* 前缀):OS 密钥库 keyring 优先托管——伪
keyring 模块(monkeypatch sys.modules)命中/抛异常/缺席三态降级链断言、
store_key_to_keyring 显式写入往返、读取路径绝不自动写回、密钥永不入日志。
全文件 autouse 隔离真实 keyring,结论不依赖宿主机是否恰装 keyring。
"""
from __future__ import annotations

import logging
import os
import pathlib
import stat
import sys
from types import SimpleNamespace

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys
from netsentinel.security.keys import (
    configured,
    get_key,
    redact_keys,
    set_key,
    store_key_to_keyring,
)

# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 pathlib.Path.home() 指向 tmp 目录,隔离真实家目录与密钥文件。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


@pytest.fixture()
def no_tighten(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通用例不真正执行权限收紧(chmod / icacls 由专门用例覆盖)。"""
    monkeypatch.setattr(keys, "_tighten_key_file_permissions", lambda path: None)


@pytest.fixture()
def no_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 A61 目录未就位:退回约定环境变量两形态 + 内置 20 家清单。"""
    monkeypatch.setattr(keys, "_load_catalog", lambda: None)


@pytest.fixture()
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """注入受控提供方目录:qwen 双环境变量、ollama 本地免密钥、glm 双环境变量。"""
    catalog: dict[str, object] = {
        "qwen": SimpleNamespace(key_envs=["NETSENTINEL_QWEN_API_KEY", "DASHSCOPE_API_KEY"]),
        "glm": SimpleNamespace(key_envs=["NETSENTINEL_GLM_API_KEY", "GLM_API_KEY"]),
        "ollama": SimpleNamespace(key_envs=[]),
    }
    monkeypatch.setattr(keys, "_load_catalog", lambda: catalog)
    return catalog


#: 契约 §2 里除 NETSENTINEL_* 形态外的第二环境变量名(清空防宿主机污染)
_EXTRA_SECOND_FORM_ENVS = (
    "GLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY",
    "ARK_API_KEY",
    "MOONSHOT_API_KEY",
    "QIANFAN_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
)


def _clear_all_provider_envs(monkeypatch: pytest.MonkeyPatch, *providers: str) -> None:
    """清空全部可能被查询的密钥环境变量:内置 20 家约定两形态 + §2 第二形态。"""
    names = providers or keys.BUILTIN_PROVIDERS
    for name in names:
        for env in keys._conventional_env_names(name):
            monkeypatch.delenv(env, raising=False)
    for env in _EXTRA_SECOND_FORM_ENVS:
        monkeypatch.delenv(env, raising=False)


def _make_key_file(
    home_dir: pathlib.Path, provider: str, content: str
) -> pathlib.Path:
    """在家目录下伪造 ``~/.netsentinel/keys/<provider>`` 密钥文件。"""
    d = home_dir / ".netsentinel" / "keys"
    d.mkdir(parents=True, exist_ok=True)
    p = d / provider
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# get_key:三来源优先级(cfg > 环境变量 > 文件)
# ---------------------------------------------------------------------------


def test_cfg_beats_env_and_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-env0000000000")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash000000000")
    _make_key_file(home, "qwen", "sk-file0000000000\n")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    key = get_key("qwen", Config(vlm_api_keys={"qwen": "sk-cfg00000000000"}))

    assert key == "sk-cfg00000000000"
    assert "配置项" in caplog.text
    assert "sk-cfg00000000000" not in caplog.text


def test_env_beats_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_key_file(home, "qwen", "sk-file0000000000\n")
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-env0000000000")

    assert get_key("qwen", Config()) == "sk-env0000000000"


def test_blank_cfg_value_falls_through(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-env0000000000")
    cfg = Config(vlm_api_keys={"qwen": "   "})  # 纯空白配置项视为未配置

    assert get_key("qwen", cfg) == "sk-env0000000000"


def test_blank_env_falls_through_to_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "   ")  # 空白环境变量视为未设置
    _make_key_file(home, "qwen", "sk-file0000000000\n")

    assert get_key("qwen", Config()) == "sk-file0000000000"


def test_missing_everywhere_returns_empty_with_chinese_hint(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_all_provider_envs(monkeypatch, "qwen")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert get_key("qwen", Config()) == ""
    assert "未配置密钥" in caplog.text


# ---------------------------------------------------------------------------
# get_key:目录 key_envs 多环境变量顺序 / 约定退化 / 本地提供方
# ---------------------------------------------------------------------------


def test_env_order_first_key_env_wins(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-first000000000")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-second0000000")

    assert get_key("qwen", Config()) == "sk-first000000000"


def test_env_fallback_to_second_key_env(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash000000000")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert get_key("qwen", Config()) == "sk-dash000000000"
    assert "DASHSCOPE_API_KEY" in caplog.text


def test_convention_two_forms_when_catalog_missing(
    home: pathlib.Path,
    no_catalog: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目录未就位:任意提供方都退回 约定两形态(NETSENTINEL_X_API_KEY / X_API_KEY)
    monkeypatch.setenv("ZZZTEST_API_KEY", "sk-conv0000000000")
    assert get_key("zzztest", Config()) == "sk-conv0000000000"

    monkeypatch.delenv("ZZZTEST_API_KEY")
    monkeypatch.setenv("NETSENTINEL_ZZZTEST_API_KEY", "sk-conv0000000001")
    assert get_key("zzztest", Config()) == "sk-conv0000000001"


def test_convention_differs_from_catalog_second_form(
    home: pathlib.Path,
    no_catalog: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 目录未就位时只认约定两形态:qwen 的目录第二来源 DASHSCOPE_API_KEY 不被查询
    _clear_all_provider_envs(monkeypatch, "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash000000000")

    assert get_key("qwen", Config()) == ""


def test_local_provider_empty_key_envs_ignores_env(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NETSENTINEL_OLLAMA_API_KEY", "sk-ollama0000000")

    assert get_key("ollama", Config()) == ""  # 本地提供方不看环境变量


def test_unsafe_provider_name_returns_empty(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=keys.logger.name)

    assert get_key("../evil", Config()) == ""
    assert get_key("a/b", Config()) == ""
    assert get_key("..", Config()) == ""

    assert "非法" in caplog.text


def test_env_names_real_catalog_or_convention() -> None:
    """真实目录态(就位与否):未知提供方一律约定两形态;已知提供方返回字符串列表。"""
    envs = keys._env_names_for("qwen")
    assert isinstance(envs, list) and envs
    assert all(isinstance(name, str) for name in envs)

    assert keys._env_names_for("zzz-unknown") == [
        "NETSENTINEL_ZZZ-UNKNOWN_API_KEY",
        "ZZZ-UNKNOWN_API_KEY",
    ]


# ---------------------------------------------------------------------------
# get_key:文件来源(首行 / 尾随空白 / 空文件)
# ---------------------------------------------------------------------------


def test_file_first_line_stripped(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_all_provider_envs(monkeypatch, "qwen")
    key_path = _make_key_file(home, "qwen", "  sk-file1234567890  \nsecond-line-not-used\n")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert get_key("qwen", Config()) == "sk-file1234567890"  # 仅首行且去首尾空白
    assert str(key_path) in caplog.text


def test_blank_or_empty_file_returns_empty(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_provider_envs(monkeypatch, "qwen")
    _make_key_file(home, "qwen", "   \n")  # 首行纯空白
    assert get_key("qwen", Config()) == ""

    _make_key_file(home, "qwen", "")  # 空文件不得抛 IndexError
    assert get_key("qwen", Config()) == ""


# ---------------------------------------------------------------------------
# set_key:落盘 / 读回 / 空密钥拒绝 / 目录自动创建 / base 展开
# ---------------------------------------------------------------------------


def test_set_key_writes_first_line_and_creates_dirs(
    tmp_path: pathlib.Path, no_tighten: None
) -> None:
    base = tmp_path / "deep" / "nested" / "keys"  # 不存在,应自动逐级创建

    path = set_key("qwen", "  sk-new1234567890  ", base=str(base))

    p = pathlib.Path(path)
    assert str(p) == str(base / "qwen")
    assert p.is_file()
    assert p.read_text(encoding="utf-8") == "sk-new1234567890\n"  # 首行 + 换行,已 strip
    assert base.is_dir()


def test_set_key_default_base_read_back_by_get_key(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_all_provider_envs(monkeypatch, "qwen")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    path = set_key("qwen", "sk-default0000000")

    assert pathlib.Path(path) == home / ".netsentinel" / "keys" / "qwen"
    assert get_key("qwen", Config()) == "sk-default0000000"  # 落盘后可按默认路径读回
    assert "NETSENTINEL_QWEN_API_KEY" in caplog.text  # 日志提示环境变量替代写法
    assert "sk-default0000000" not in caplog.text


def test_set_key_base_tilde_expands_via_path_home(
    home: pathlib.Path, no_tighten: None
) -> None:
    path = set_key("glm", "sk-tilde000000000", base="~/custom-keys")

    assert pathlib.Path(path) == home / "custom-keys" / "glm"
    assert (home / "custom-keys" / "glm").is_file()


def test_set_key_empty_or_blank_rejected(tmp_path: pathlib.Path) -> None:
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="密钥不能为空"):
            set_key("qwen", bad, base=str(tmp_path / "keys"))
    with pytest.raises(ValueError, match="密钥不能为空"):  # 非字符串同样拒绝
        set_key("qwen", 123, base=str(tmp_path / "keys"))  # type: ignore[arg-type]


def test_set_key_invalid_provider_rejected(tmp_path: pathlib.Path) -> None:
    for bad in ("", "a/b", "..", "../evil"):
        with pytest.raises(ValueError, match="提供方名称非法"):
            set_key(bad, "sk-x000000000000", base=str(tmp_path / "keys"))


# ---------------------------------------------------------------------------
# set_key:权限收紧(POSIX chmod 600 / Windows icacls)
# ---------------------------------------------------------------------------


def test_posix_chmod_600_branch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """os.name=posix 分支:chmod 0o600,成功记中文日志,不抛异常。

    直接调用收紧函数(与 vault 同款口径):Python 3.14 起在 Windows 上伪造
    os.name 后再新建 Path 会抛 UnsupportedOperation,故不经由 set_key。
    """
    monkeypatch.setattr(keys.os, "name", "posix")
    p = tmp_path / "qwen_key"
    chmod_calls: list[tuple[object, int]] = []

    def _record_chmod(path: object, mode: int) -> None:
        chmod_calls.append((path, mode))

    monkeypatch.setattr(keys.os, "chmod", _record_chmod)
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    keys._tighten_key_file_permissions(p)

    assert chmod_calls == [(p, stat.S_IRUSR | stat.S_IWUSR)]  # 0o600
    assert "chmod 600" in caplog.text


def test_set_key_invokes_permission_tightening(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tighten_calls: list[pathlib.Path] = []
    monkeypatch.setattr(
        keys, "_tighten_key_file_permissions", lambda p: tighten_calls.append(p)
    )

    path = set_key("qwen", "sk-tighten0000000", base=str(tmp_path / "keys"))

    assert tighten_calls == [pathlib.Path(path)]  # 落盘后即收紧


def test_set_key_windows_icacls_bytes_captured_nonzero_only_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """os.name=nt 且 icacls 退出码非 0:字节输出不解码,不抛异常,仅中文提示。"""
    monkeypatch.setattr(keys.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "tester")

    calls: list[tuple[list[str], dict[str, object]]] = []

    class _FakeResult:  # stdout/stderr 为非法 utf-8 字节:证明按字节捕获不解码
        returncode = 1
        stdout = b"\xd5\xd5\xca\xd4\xb2\xe2\xca\xd4"
        stderr = b"\x80\x81"

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeResult:
        calls.append((list(cmd), dict(kwargs)))
        return _FakeResult()

    monkeypatch.setattr(keys.subprocess, "run", _fake_run)
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    path = set_key("qwen", "sk-icacls0000000", base=str(tmp_path / "keys"))

    assert pathlib.Path(path).is_file()  # 密钥文件照常写入
    assert len(calls) == 1 and calls[0][0][0] == "icacls"  # 确实尝试过 icacls
    cmd, kwargs = calls[0]
    assert "tester:F" in cmd  # 授权当前用户
    assert kwargs.get("capture_output") is True  # 字节捕获
    assert not kwargs.get("text")  # 绝不解码输出,只看退出码
    assert "未能自动收紧权限" in caplog.text
    assert "sk-icacls0000000" not in caplog.text


def test_set_key_windows_icacls_success_logs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(keys.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "tester")

    class _FakeResult:
        returncode = 0
        stdout = b"processed"
        stderr = b""

    monkeypatch.setattr(keys.subprocess, "run", lambda *a, **k: _FakeResult())
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    set_key("qwen", "sk-icacls-ok-0000", base=str(tmp_path / "keys"))

    assert "已通过 icacls" in caplog.text


def test_set_key_icacls_launch_failure_only_warns(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(keys.os, "name", "nt")

    def _boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("icacls 不可用")

    monkeypatch.setattr(keys.subprocess, "run", _boom)
    caplog.set_level(logging.WARNING, logger=keys.logger.name)

    path = set_key("qwen", "sk-noicacls00000", base=str(tmp_path / "keys"))

    assert pathlib.Path(path).is_file()  # 写入不受权限收紧失败影响
    assert "无法自动收紧" in caplog.text


# ---------------------------------------------------------------------------
# configured:全目录布尔体检
# ---------------------------------------------------------------------------


def test_configured_builtin_20_all_boolean(
    home: pathlib.Path, no_catalog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_provider_envs(monkeypatch)
    cfg = Config(vlm_api_keys={"glm": "sk-glm00000000000", "openai": "sk-oai00000000000"})

    result = configured(cfg)

    assert set(result) == set(keys.BUILTIN_PROVIDERS)  # 内置 20 家清单
    assert len(result) == 20
    assert all(isinstance(v, bool) for v in result.values())
    assert result["glm"] is True
    assert result["openai"] is True
    others = {k: v for k, v in result.items() if k not in ("glm", "openai")}
    assert others and all(v is False for v in others.values())  # 其余全 False
    for local_name in ("ollama", "vllm", "lmstudio", "xinference"):  # 本地免密钥
        assert result[local_name] is False


def test_configured_uses_catalog_when_available(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_provider_envs(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dash000000000")  # qwen 第二环境变量

    result = configured(Config())

    assert set(result) == {"qwen", "ollama", "glm"}  # 目录就位时遍历目录而非内置清单
    assert result == {"qwen": True, "ollama": False, "glm": False}


def test_configured_real_catalog_state_all_boolean(
    home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实目录态(A61 就位与否都成立):返回非空布尔表,规模不小于内置清单。"""
    _clear_all_provider_envs(monkeypatch)

    result = configured(Config())

    assert result
    assert len(result) >= len(keys.BUILTIN_PROVIDERS)
    assert all(isinstance(v, bool) for v in result.values())


# ---------------------------------------------------------------------------
# redact_keys:vlm_api_keys 特例 + vault.redact 同构语义
# ---------------------------------------------------------------------------


def test_redact_keys_masks_vlm_api_keys_values() -> None:
    d = {
        "vlm_api_keys": {
            "glm": "sk-abcdefgh123456",
            "qwen": "0123456789abcdef0123456789abcdef",
            "short": "ab",
            "broken": 7,  # 非字符串值整体置 ****
        },
        "vlm_provider": "glm",  # 提供方名不是敏感值,保留
    }

    out = redact_keys(d)

    assert out["vlm_api_keys"] == {
        "glm": "sk-a****",
        "qwen": "0123****",
        "short": "ab****",
        "broken": "****",
    }
    assert out["vlm_provider"] == "glm"


def test_redact_keys_vlm_api_keys_non_dict_masked() -> None:
    assert redact_keys({"vlm_api_keys": "oops"}) == {"vlm_api_keys": "****"}


def test_redact_keys_vault_semantics_kept() -> None:
    assert redact_keys("sk-abcdefgh123456") == "sk-a****"
    assert redact_keys("id-12345678abcdef") == "id-1****"
    assert redact_keys("sk-abc") == "sk-abc"  # 前缀后不足 8 位不打码
    assert redact_keys("a" * 32) == "aaaa****"
    assert redact_keys("a" * 31) == "a" * 31  # 不足 32 位不动
    assert redact_keys("Bearer sk-abcdefgh12345") == "Bear****"
    assert redact_keys({"api_key": "any", "UserPassword": 123}) == {
        "api_key": "****",
        "UserPassword": "****",
    }


def test_redact_keys_nested_containers() -> None:
    d = {
        "cfg": {
            "vlm_api_keys": {"openai": "sk-openai0000000"},
            "note": ["sk-nest000000000", {"auth_token": "t"}],
        },
        "auth": "Bearer sk-abcdefgh12345",
        "pair": ("sk-tup0000000000", 5),
        "meta": {"url": "https://e.com/a?x=1", "ver": "1.0.2"},
        "n": 7,
        "ratio": 0.5,
        "flag": True,
        "none": None,
    }

    out = redact_keys(d)

    assert out["cfg"]["vlm_api_keys"] == {"openai": "sk-o****"}
    assert out["cfg"]["note"][0] == "sk-n****"
    assert out["cfg"]["note"][1] == {"auth_token": "****"}
    assert out["auth"] == "Bear****"
    assert out["pair"] == ("sk-t****", 5)
    assert isinstance(out["pair"], tuple)
    assert out["meta"] == {"url": "https://e.com/a?x=1", "ver": "1.0.2"}
    assert (out["n"], out["ratio"], out["flag"], out["none"]) == (7, 0.5, True, None)


def test_redact_keys_does_not_mutate_input() -> None:
    inner = {"vlm_api_keys": {"glm": "sk-orig0000000000"}}
    original = {"api_key": "v", "items": inner, "auth": "Bearer abcdef"}
    snapshot = {
        "api_key": "v",
        "items": {"vlm_api_keys": {"glm": "sk-orig0000000000"}},
        "auth": "Bearer abcdef",
    }

    out = redact_keys(original)

    assert original == snapshot  # 入参保持不变
    assert original["items"] is inner
    assert out is not original
    assert out["items"] is not inner  # 返回同构新对象
    assert out == {
        "api_key": "****",
        "items": {"vlm_api_keys": {"glm": "sk-o****"}},
        "auth": "Bear****",
    }


# ---------------------------------------------------------------------------
# 红线 17:密钥绝不入日志
# ---------------------------------------------------------------------------


def test_secrets_never_appear_in_logs(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=keys.logger.name)
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "ENVSECRET-qwen-0001")

    get_key("qwen", Config(vlm_api_keys={"glm": "CFGSECRET-glm-00001"}))
    set_key("moonshot", "FILESECRET-moon-0001")  # 默认 base → 落在 tmp 家目录
    get_key("moonshot", Config())  # 文件来源
    get_key("anthropic", Config())  # 未配置分支

    for secret in ("ENVSECRET-qwen-0001", "CFGSECRET-glm-00001", "FILESECRET-moon-0001"):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# V5 升级:configured 批量化 / environ 快照 / redact 深度防护 / 遥测
# ---------------------------------------------------------------------------


class _CountingEnvProxy:
    """代理 os.environ:统计 ``.get`` 穿透次数(证明 configured 只读一次快照)。"""

    def __init__(self, real) -> None:
        self._real = real
        self.get_calls = 0

    def get(self, key: str, default: str | None = None) -> str | None:
        self.get_calls += 1
        return self._real.get(key, default)

    def keys(self):  # dict(proxy) 走 keys() + __getitem__
        return list(self._real.keys())

    def __getitem__(self, key: str) -> str:
        return self._real[key]

    def __setitem__(self, key: str, value: str) -> None:  # pytest 写 PYTEST_CURRENT_TEST 用
        self._real[key] = value

    def __delitem__(self, key: str) -> None:
        del self._real[key]

    def __iter__(self):
        return iter(self._real)

    def __contains__(self, key: object) -> bool:
        return key in self._real


def test_v5_configured_counts_telemetry(
    home: pathlib.Path, no_catalog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 可观测:keys.configured 计数 = 已配置家数(只存数字,红线 17)。"""
    _clear_all_provider_envs(monkeypatch)
    telemetry.reset()
    cfg = Config(vlm_api_keys={"glm": "sk-glm00000000000", "openai": "sk-oai00000000000"})

    result = configured(cfg)

    assert sum(1 for v in result.values() if v) == 2
    assert telemetry.snapshot()["counters"]["keys.configured"] == 2.0


def test_v5_configured_snapshots_environ_once(
    home: pathlib.Path, no_catalog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 性能:configured 全程零次 os.environ.get 穿透(一次 dict 快照批量传)。"""
    _clear_all_provider_envs(monkeypatch)
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-v5snap0000000")  # 先设好,再装代理
    proxy = _CountingEnvProxy(os.environ)
    monkeypatch.setattr(keys.os, "environ", proxy)

    result = configured(Config())

    assert len(result) == 20
    assert proxy.get_calls == 0  # 全部经 dict(os.environ) 快照,零次 .get 穿透
    # 单次 get_key 缺省路径仍现查 os.environ(对外行为不变)
    assert get_key("qwen", Config()) == "sk-v5snap0000000"
    assert proxy.get_calls >= 1


def test_v5_get_key_accepts_environ_mapping(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V5:environ 关键字注入快照;快照未命中(空白)仍回落密钥文件。"""
    _clear_all_provider_envs(monkeypatch, "qwen")
    assert (
        get_key("qwen", Config(), environ={"NETSENTINEL_QWEN_API_KEY": "sk-map000000000"})
        == "sk-map000000000"
    )
    _make_key_file(home, "qwen", "sk-file0000000000\n")
    assert (
        get_key("qwen", Config(), environ={"NETSENTINEL_QWEN_API_KEY": "   "})
        == "sk-file0000000000"
    )


def test_v5_redact_keys_depth_cap_and_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V5 健壮:超 50 层嵌套整体打码 + WARNING;处理条数计入 keys.redact_items。"""
    telemetry.reset()
    deep: object = {"secret": "sk-deep0000000000"}
    for _ in range(keys._MAX_REDACT_DEPTH + 10):
        deep = {"level": deep}
    with caplog.at_level(logging.WARNING, logger=keys.logger.name):
        out = redact_keys(deep)  # 不得抛 RecursionError
    assert "恶意嵌套" in caplog.text

    node = out
    for _ in range(keys._MAX_REDACT_DEPTH + 1):  # 顶层深度 0,深度 51 起整体打码
        assert isinstance(node, dict)
        node = node["level"]
    assert node == "****"

    assert telemetry.snapshot()["counters"]["keys.redact_items"] >= keys._MAX_REDACT_DEPTH


def test_v5_set_key_counts_telemetry(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V5 可观测:set_key 计 keys.set_key。"""
    telemetry.reset()
    monkeypatch.setattr(keys, "_tighten_key_file_permissions", lambda path: None)
    set_key("qwen", "sk-v5cnt000000000", base=str(tmp_path / "keys"))
    assert telemetry.snapshot()["counters"]["keys.set_key"] == 1


# ---------------------------------------------------------------------------
# P4 安全审计清偿:OS 密钥库 keyring 优先托管(可选 extra,缺席回退现状)
# ---------------------------------------------------------------------------


class _FakeKeyring:
    """伪 keyring 后端:内存字典模拟 OS 密钥库 get/set,记录调用供断言。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.set_calls: list[tuple[str, str]] = []
        self.get_raises: BaseException | None = None
        self.set_raises: BaseException | None = None

    def get_password(self, service: str, username: str) -> str | None:
        self.get_calls.append((service, username))
        if self.get_raises is not None:
            raise self.get_raises
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls.append((service, username))
        if self.set_raises is not None:
            raise self.set_raises
        self.store[(service, username)] = password


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """全文件默认置 keyring 为"缺席"(sys.modules 哨兵 None → import 抛 ImportError)。

    既有全部用例因此保持历史行为(无 keyring 一步);P4 用例再按需覆盖安装
    伪 keyring(后注入的 setitem 先生效),结论不依赖宿主机是否恰好装有 keyring。
    """
    monkeypatch.setitem(sys.modules, "keyring", None)


def _install_fake_keyring(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeKeyring
) -> _FakeKeyring:
    """把伪 keyring 实例装进 sys.modules(惰性 import 即取到它)。"""
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


# ---- 降级链三态:命中 / 抛异常 / 缺席 --------------------------------------


def test_p4_keyring_sits_between_env_and_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """keyring 命中 → 排在文件来源之前返回(服务名 netsentinel,用户名=提供方)。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    _clear_all_provider_envs(monkeypatch, "qwen")
    _make_key_file(home, "qwen", "sk-file0000000000\n")
    fake.store[(keys.KEYRING_SERVICE, "qwen")] = "sk-kr0000000000000"
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert get_key("qwen", Config()) == "sk-kr0000000000000"

    assert fake.get_calls == [(keys.KEYRING_SERVICE, "qwen")]
    assert "keyring" in caplog.text
    assert "sk-kr0000000000000" not in caplog.text  # 密钥绝不入日志


def test_p4_env_beats_keyring(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量优先于 keyring:命中即短路,不会触达密钥库。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-env0000000000")
    fake.store[(keys.KEYRING_SERVICE, "qwen")] = "sk-kr0000000000000"

    assert get_key("qwen", Config()) == "sk-env0000000000"
    assert fake.get_calls == []  # 环境变量先行,keyring 未被查询


def test_p4_cfg_beats_keyring(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置项(显式参数)仍是最高优先级:keyring 未被查询。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    fake.store[(keys.KEYRING_SERVICE, "qwen")] = "sk-kr0000000000000"

    assert get_key("qwen", Config(vlm_api_keys={"qwen": "sk-cfg00000000000"})) == (
        "sk-cfg00000000000"
    )
    assert fake.get_calls == []


def test_p4_keyring_exception_degrades_to_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """keyring 抛异常 → 静默降级文件路径(仅 debug 日志),解析绝不中断。"""
    fake = _FakeKeyring()
    fake.get_raises = RuntimeError("密钥库后端模拟故障")
    _install_fake_keyring(monkeypatch, fake)
    _clear_all_provider_envs(monkeypatch, "qwen")
    _make_key_file(home, "qwen", "sk-file0000000000\n")
    caplog.set_level(logging.DEBUG, logger=keys.logger.name)

    assert get_key("qwen", Config()) == "sk-file0000000000"

    assert "降级" in caplog.text  # debug 级中文提示,不构成告警噪音
    assert "密钥库后端模拟故障" not in caplog.text  # 异常消息不回显(防泄漏)
    assert "sk-file0000000000" not in caplog.text


def test_p4_keyring_absent_keeps_file_behavior(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keyring 缺席(未安装):解析链与历史版本一致,文件来源照常生效。"""
    _clear_all_provider_envs(monkeypatch, "qwen")
    _make_key_file(home, "qwen", "sk-file0000000000\n")

    assert get_key("qwen", Config()) == "sk-file0000000000"
    assert get_key("anthropic", Config()) == ""  # 无任何来源 → 空串


def test_p4_keyring_miss_falls_through_to_file(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keyring 已安装但未命中(返回 None)→ 落到文件来源。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    _clear_all_provider_envs(monkeypatch, "qwen")
    _make_key_file(home, "qwen", "sk-file0000000000\n")

    assert get_key("qwen", Config()) == "sk-file0000000000"
    assert fake.get_calls == [(keys.KEYRING_SERVICE, "qwen")]


# ---- store_key_to_keyring:显式写入 / 往返 / 读取绝不写回 --------------------


def test_p4_store_key_to_keyring_roundtrip(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """store 助手显式写入(首尾空白已 strip)→ get_key 经 keyring 读回一致;
    全程不落任何明文文件,密钥绝不入日志。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    _clear_all_provider_envs(monkeypatch, "qwen")
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert store_key_to_keyring("qwen", "  sk-kr0000000000000  ") is True

    assert fake.store == {(keys.KEYRING_SERVICE, "qwen"): "sk-kr0000000000000"}
    assert fake.set_calls == [(keys.KEYRING_SERVICE, "qwen")]
    assert get_key("qwen", Config()) == "sk-kr0000000000000"  # keyring 读回
    assert not (home / ".netsentinel").exists()  # 未落明文文件
    assert "sk-kr0000000000000" not in caplog.text


def test_p4_read_path_never_writes_back(
    fake_catalog: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """读取路径绝不自动写回:get_key 多轮(命中/未命中)后 set_password 零调用。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    fake.store[(keys.KEYRING_SERVICE, "qwen")] = "sk-kr0000000000000"

    assert get_key("qwen", Config()) == "sk-kr0000000000000"
    assert get_key("anthropic", Config()) == ""  # 未命中也绝不写回

    assert fake.set_calls == []


def test_p4_store_absent_returns_false_with_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """keyring 缺席:store 返回 False(绝不抛),日志给 pip install 指引。"""
    caplog.set_level(logging.INFO, logger=keys.logger.name)

    assert store_key_to_keyring("qwen", "sk-kr0000000000000") is False

    assert "pip install keyring" in caplog.text
    assert "sk-kr0000000000000" not in caplog.text


def test_p4_store_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """set_password 抛异常 → 返回 False 不抛出;警告只含异常类型,不回显消息。"""
    fake = _FakeKeyring()
    fake.set_raises = RuntimeError("写入时把密钥回显进消息:sk-leak-0000")
    _install_fake_keyring(monkeypatch, fake)
    caplog.set_level(logging.WARNING, logger=keys.logger.name)

    assert store_key_to_keyring("qwen", "sk-kr0000000000000") is False

    assert "OS 密钥库" in caplog.text
    assert "sk-leak-0000" not in caplog.text  # 后端异常消息不回显(防泄漏)


def test_p4_store_rejects_bad_name_and_blank_value() -> None:
    """用法错误当场暴露(与 set_key 同口径):非法提供方名/空白密钥 → ValueError。"""
    with pytest.raises(ValueError, match="提供方名称非法"):
        store_key_to_keyring("a/b", "sk-x000000000000")
    with pytest.raises(ValueError, match="密钥不能为空"):
        store_key_to_keyring("qwen", "   ")
    with pytest.raises(ValueError, match="密钥不能为空"):
        store_key_to_keyring("qwen", 123)  # type: ignore[arg-type]


def test_p4_store_counts_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功入库计数 keys.keyring.store(仅数字,红线 17);失败不计数。"""
    telemetry.reset()
    _install_fake_keyring(monkeypatch, _FakeKeyring())

    assert store_key_to_keyring("qwen", "sk-kr0000000000000") is True
    assert telemetry.snapshot()["counters"]["keys.keyring.store"] == 1.0


def test_p4_keyring_secret_never_in_logs(
    home: pathlib.Path,
    fake_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """全链路(命中读取/显式入库/未命中)日志永不包含 keyring 中的密钥。"""
    fake = _install_fake_keyring(monkeypatch, _FakeKeyring())
    fake.store[(keys.KEYRING_SERVICE, "qwen")] = "KRSECRET-qwen-0001"
    caplog.set_level(logging.DEBUG, logger=keys.logger.name)

    assert get_key("qwen", Config()) == "KRSECRET-qwen-0001"
    store_key_to_keyring("moonshot", "KRSECRET-moon-001")
    assert get_key("anthropic", Config()) == ""

    assert "KRSECRET-qwen-0001" not in caplog.text
    assert "KRSECRET-moon-001" not in caplog.text
