"""A152:netsentinel.security.key_api 单元测试(离线,只写 tmp_path + monkeypatch)。

红线 33 专项:**密钥只进不显**——所有拒绝路径构造含哨兵子串的密钥,
断言哨兵既不出现在 ``str(exc)``,也不出现在 caplog;成功路径同样断言
日志只含提供方与长度。提供方目录(A61)就位与否两种并行态均覆盖:
真实目录态跑主链路,受控注入态模拟目录缺席退回内置清单。
"""
from __future__ import annotations

import logging
import pathlib
import socket

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import key_api
from netsentinel.security import keys
from netsentinel.security.key_api import accept_key_input, masked

# ---------------------------------------------------------------------------
# 公共辅助(口径对齐 tests/test_keys.py)
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
    """普通用例不真正执行权限收紧(chmod / icacls 不出测试进程)。"""
    monkeypatch.setattr(keys, "_tighten_key_file_permissions", lambda path: None)


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


def _clear_provider_envs(monkeypatch: pytest.MonkeyPatch, *providers: str) -> None:
    """清空全部可能被查询的密钥环境变量(内置 20 家两形态 + §2 第二形态)。"""
    names = providers or keys.BUILTIN_PROVIDERS
    for name in names:
        for env in keys._conventional_env_names(name):
            monkeypatch.delenv(env, raising=False)
    for env in _EXTRA_SECOND_FORM_ENVS:
        monkeypatch.delenv(env, raising=False)


def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 socket.socket 换成哨兵:任何真实联网尝试都会让测试失败。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("key_api 不得触网(红线 33:密钥受纳纯本地)")

    monkeypatch.setattr(socket, "socket", _boom)


# ---------------------------------------------------------------------------
# accept_key_input:成功受纳(返回结构 / 落盘读回 / strip 语义 / 长度边界)
# ---------------------------------------------------------------------------


def test_accept_success_returns_exact_payload(
    tmp_path: pathlib.Path, no_tighten: None
) -> None:
    result = accept_key_input("qwen", "sk-abcd1234567890", base=str(tmp_path / "keys"))

    assert result == {"ok": True, "masked": "sk-a****", "stored": True}


def test_accept_writes_file_readable_by_get_key(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
) -> None:
    _clear_provider_envs(monkeypatch, "qwen")

    result = accept_key_input("qwen", "sk-roundtrip-0001")  # 默认 base → tmp 家目录

    assert result["ok"] is True and result["stored"] is True
    key_file = home / ".netsentinel" / "keys" / "qwen"
    assert key_file.is_file()
    assert keys.get_key("qwen", Config()) == "sk-roundtrip-0001"  # 落盘后可读回一致


def test_accept_strips_surrounding_whitespace(
    tmp_path: pathlib.Path, no_tighten: None
) -> None:
    base = tmp_path / "keys"

    result = accept_key_input("glm", "  sk-stripme-00001  ", base=str(base))

    # 首尾空白被去掉后落盘;掩码按去空白后的前 4 位计算
    assert result["masked"] == "sk-s****"
    assert (base / "glm").read_text(encoding="utf-8") == "sk-stripme-00001\n"


def test_accept_min_length_boundary_ok(tmp_path: pathlib.Path, no_tighten: None) -> None:
    assert len("sk-12345") == 8  # 恰好 8 位:下边界应放行

    result = accept_key_input("moonshot", "sk-12345", base=str(tmp_path / "keys"))

    assert result == {"ok": True, "masked": "sk-1****", "stored": True}


# ---------------------------------------------------------------------------
# accept_key_input:拒绝路径(短 / 空 / 空白 / 换行 / 控制字符)——密钥绝不回显
# ---------------------------------------------------------------------------


def test_reject_short_key_no_echo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "SENTxy9"  # 7 位:过短,且含哨兵子串
    assert len(sentinel) == 7

    with caplog.at_level(logging.WARNING, logger=key_api.logger.name):
        with pytest.raises(ValueError) as excinfo:
            accept_key_input("qwen", sentinel, base=str(tmp_path / "keys"))

    msg = str(excinfo.value)
    assert "过短" in msg and "8" in msg  # 只给长度口径,不给内容
    assert sentinel not in msg  # 红线 33:异常消息不含密钥本体
    assert sentinel not in caplog.text  # 日志同样不含
    assert not (tmp_path / "keys").exists()  # 未落盘


def test_reject_empty_and_blank_no_file(
    tmp_path: pathlib.Path, no_tighten: None
) -> None:
    base = str(tmp_path / "keys")
    for bad in ("", "   ", "\t \n"):
        with pytest.raises(ValueError, match="空或仅空白"):
            accept_key_input("qwen", bad, base=base)
    assert not (tmp_path / "keys").exists()  # 拒绝路径绝不建目录写文件


def test_reject_inner_space_no_echo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "sk-SENTINEL-SP 773593"
    with caplog.at_level(logging.WARNING, logger=key_api.logger.name):
        with pytest.raises(ValueError, match="空白") as excinfo:
            accept_key_input("glm", sentinel, base=str(tmp_path / "keys"))

    assert "SENTINEL" not in str(excinfo.value)
    assert "SENTINEL" not in caplog.text
    assert not (tmp_path / "keys").exists()


def test_reject_newline_no_echo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "sk-SENTINEL-NL\n773593"
    with caplog.at_level(logging.WARNING, logger=key_api.logger.name):
        with pytest.raises(ValueError, match="换行") as excinfo:
            accept_key_input("qwen", sentinel, base=str(tmp_path / "keys"))

    assert "SENTINEL" not in str(excinfo.value)
    assert "SENTINEL" not in caplog.text


def test_reject_tab_and_control_chars_no_echo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = str(tmp_path / "keys")
    for sentinel, word in (("sk-SENTINEL-TB\t773", "空白"), ("sk-SENTINEL-CT\x0773", "控制")):
        with caplog.at_level(logging.WARNING, logger=key_api.logger.name):
            with pytest.raises(ValueError, match=word) as excinfo:
                accept_key_input("qwen", sentinel, base=base)
        assert "SENTINEL" not in str(excinfo.value)
        assert "SENTINEL" not in caplog.text


# ---------------------------------------------------------------------------
# accept_key_input:提供方合法性(未知 / 非字符串 / 目录缺席兜底 / 名称归一)
# ---------------------------------------------------------------------------


def test_reject_unknown_provider_real_catalog(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """真实目录态:目录清单之外的提供方一律拒绝,密钥同样不回显。"""
    assert "zzz-not-a-provider" not in key_api._known_providers()

    with caplog.at_level(logging.WARNING, logger=key_api.logger.name):
        with pytest.raises(ValueError, match="未知提供方") as excinfo:
            accept_key_input("zzz-not-a-provider", "sk-unknown-773593")

    assert "sk-unknown-773593" not in str(excinfo.value)
    assert "sk-unknown-773593" not in caplog.text


def test_reject_non_string_provider_and_key(tmp_path: pathlib.Path) -> None:
    base = str(tmp_path / "keys")
    with pytest.raises(ValueError, match="未知提供方"):
        accept_key_input(None, "sk-valid-0000001", base=base)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="格式错误"):
        accept_key_input("qwen", 123456789, base=base)  # type: ignore[arg-type]
    assert not (tmp_path / "keys").exists()


def test_provider_catalog_missing_falls_back_to_builtin(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
) -> None:
    """A61 目录缺席:退回 keys.BUILTIN_PROVIDERS 兜底——成员放行,陌生仍拒。"""
    monkeypatch.setattr(key_api, "_load_provider_catalog", lambda: None)
    base = str(tmp_path / "keys")

    assert "qwen" in keys.BUILTIN_PROVIDERS
    result = accept_key_input("qwen", "sk-fallback-0001", base=base)  # 兜底清单内:放行
    assert result["ok"] is True
    assert (tmp_path / "keys" / "qwen").is_file()

    with pytest.raises(ValueError, match="未知提供方"):  # 清单外:照旧拒绝
        accept_key_input("zzz-unknown", "sk-fallback-0002", base=base)


def test_provider_name_stripped_before_lookup(
    tmp_path: pathlib.Path, no_tighten: None
) -> None:
    result = accept_key_input("  qwen  ", "sk-pad0000000000", base=str(tmp_path / "keys"))

    assert result["ok"] is True
    assert (tmp_path / "keys" / "qwen").is_file()  # 前后空白被忽略,不进文件名


# ---------------------------------------------------------------------------
# base 注入 / 幂等重复写
# ---------------------------------------------------------------------------


def test_base_injection_tmp(
    home: pathlib.Path, tmp_path: pathlib.Path, no_tighten: None
) -> None:
    base = tmp_path / "custom" / "keys"

    result = accept_key_input("qwen", "sk-custom-773593", base=str(base))

    assert result["ok"] is True
    assert (base / "qwen").read_text(encoding="utf-8") == "sk-custom-773593\n"
    assert not (home / ".netsentinel").exists()  # 显式 base 时不碰默认家目录


def test_idempotent_repeat_write(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
) -> None:
    _clear_provider_envs(monkeypatch, "qwen")

    first = accept_key_input("qwen", "sk-first-000001")
    second = accept_key_input("qwen", "sk-second-0002")  # 同提供方重复受纳:覆盖

    assert first["ok"] is True and second["ok"] is True
    assert keys.get_key("qwen", Config()) == "sk-second-0002"  # 后写生效,幂等不报错
    assert (home / ".netsentinel" / "keys" / "qwen").read_text(encoding="utf-8") == (
        "sk-second-0002\n"
    )


# ---------------------------------------------------------------------------
# masked:两态 + 纯本地不触网
# ---------------------------------------------------------------------------


def test_masked_configured(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
) -> None:
    _clear_provider_envs(monkeypatch, "qwen")
    result = accept_key_input("qwen", "sk-masked-00001")  # 默认 base → tmp 家目录

    assert masked("qwen") == "sk-m****"
    assert masked("qwen") == result["masked"]  # 与受纳返回的掩码同口径


def test_masked_not_configured_returns_none(
    home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_provider_envs(monkeypatch, "qwen")

    assert masked("qwen") is None  # 未配置态:None,绝不抛错
    assert masked("../evil") is None  # 非法提供方名按未配置处理


def test_masked_and_accept_never_touch_network(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
) -> None:
    _block_network(monkeypatch)  # 任何 socket 构造都会让测试失败
    _clear_provider_envs(monkeypatch, "qwen")

    result = accept_key_input("qwen", "sk-offline-0001")  # 受纳落盘纯本地
    assert result["ok"] is True
    assert masked("qwen") == "sk-o****"  # 掩码查询纯本地
    assert masked("moonshot") is None


# ---------------------------------------------------------------------------
# 遥测 / 日志纪律(红线 33:只记提供方与长度)
# ---------------------------------------------------------------------------


def test_telemetry_counter_success_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, no_tighten: None
) -> None:
    telemetry.reset()
    base = str(tmp_path / "keys")

    with pytest.raises(ValueError):  # 拒绝路径不计受纳
        accept_key_input("qwen", "short7", base=base)
    assert "key_api.accept" not in telemetry.snapshot()["counters"]

    accept_key_input("qwen", "sk-count-0000001", base=base)
    assert telemetry.snapshot()["counters"]["key_api.accept"] == 1


def test_success_logs_provider_and_length_only(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _clear_provider_envs(monkeypatch, "qwen")
    secret = "sk-LOGSECRET-773593"
    caplog.set_level(logging.INFO)  # 连同 keys.set_key 的日志一并检查

    accept_key_input("qwen", secret)

    assert "qwen" in caplog.text  # 提供方可入日志
    assert "长度" in caplog.text  # 长度可入日志
    assert secret not in caplog.text  # 密钥本体绝不入日志(红线 33)
    assert secret[3:] not in caplog.text  # 片段同样不得出现
