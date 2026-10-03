"""A38:netsentinel.security.vault 单元测试(离线,只写 tmp_path + monkeypatch)。"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import vault
from netsentinel.security.vault import AuditChain, get_glm_key, redact

FIXED_TS = "2026-01-01T08:00:00+08:00"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 pathlib.Path.home() 指向 tmp 目录,隔离真实家目录。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    return home_dir


@pytest.fixture()
def no_tighten(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通用例不真正执行权限收紧(icacls/chmod 由专门用例覆盖)。"""
    monkeypatch.setattr(vault, "_tighten_key_file_permissions", lambda path: None)


@pytest.fixture()
def fixed_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定 now_iso,保证 entry_hash 可精确复算与比较。"""
    monkeypatch.setattr(vault, "now_iso", lambda: FIXED_TS)


def _make_key_file(home_dir: pathlib.Path, content: str) -> pathlib.Path:
    d = home_dir / ".netsentinel"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "glm_key"
    p.write_text(content, encoding="utf-8")
    return p


def _write_jsonl(path: pathlib.Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def _build_chain(n: int = 3) -> list[dict]:
    """构造 n 条首尾相接的链式记录(时间戳恒定,便于复算)。"""
    chain = AuditChain()
    records: list[dict] = []
    prev = ""
    for i in range(n):
        rec = chain.wrap(prev, f"event_{i}", site_url=f"http://s{i}.example", score=i / n)
        records.append(rec)
        prev = rec["entry_hash"]
    return records


# ---------------------------------------------------------------------------
# get_glm_key:三来源优先级
# ---------------------------------------------------------------------------


def test_key_prefers_cfg_over_env_and_file(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(vault.ENV_GLM_API_KEY, "sk-env0000000000")
    _make_key_file(home, "sk-file0000000000\n")
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    key = get_glm_key(Config(glm_api_key="sk-cfg00000000000"))

    assert key == "sk-cfg00000000000"
    assert "配置项" in caplog.text


def test_key_env_beats_file(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _make_key_file(home, "sk-file0000000000\n")
    monkeypatch.setenv(vault.ENV_GLM_API_KEY, "sk-env0000000000")
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    key = get_glm_key(Config())

    assert key == "sk-env0000000000"
    assert vault.ENV_GLM_API_KEY in caplog.text
    assert "环境变量" in caplog.text


def test_key_from_file_first_line_stripped(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    no_tighten: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(vault.ENV_GLM_API_KEY, raising=False)
    key_path = _make_key_file(home, "  sk-file1234567890  \nsecond-line-not-used\n")
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    key = get_glm_key(Config())

    assert key == "sk-file1234567890"  # 仅首行且去除首尾空白
    assert str(key_path) in caplog.text


def test_key_empty_env_falls_through_to_file(
    home: pathlib.Path, monkeypatch: pytest.MonkeyPatch, no_tighten: None
) -> None:
    monkeypatch.setenv(vault.ENV_GLM_API_KEY, "   ")  # 空白环境变量视为未设置
    _make_key_file(home, "sk-file0000000000\n")

    assert get_glm_key(Config()) == "sk-file0000000000"


def test_key_missing_everywhere_returns_empty(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(vault.ENV_GLM_API_KEY, raising=False)
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    assert get_glm_key(Config()) == ""
    assert "未找到 GLM API 密钥" in caplog.text


def test_key_blank_file_returns_empty(
    home: pathlib.Path, monkeypatch: pytest.MonkeyPatch, no_tighten: None
) -> None:
    monkeypatch.delenv(vault.ENV_GLM_API_KEY, raising=False)
    _make_key_file(home, "")  # 空文件不得抛 IndexError

    assert get_glm_key(Config()) == ""


# ---------------------------------------------------------------------------
# get_glm_key:权限收紧(Windows icacls / POSIX chmod)
# ---------------------------------------------------------------------------


def test_windows_icacls_failure_only_warns(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """os.name=nt 且 icacls 进程启动失败:不抛异常,仅提示。"""
    monkeypatch.delenv(vault.ENV_GLM_API_KEY, raising=False)
    key_path = _make_key_file(home, "sk-file0000000000\n")
    monkeypatch.setattr(vault.os, "name", "nt")

    calls: list[list[str]] = []

    def _boom(*args: object, **kwargs: object) -> None:
        calls.append(list(args[0]))  # type: ignore[arg-type]
        raise FileNotFoundError("icacls 不可用")

    monkeypatch.setattr(vault.subprocess, "run", _boom)
    caplog.set_level(logging.WARNING, logger=vault.logger.name)

    assert get_glm_key(Config()) == "sk-file0000000000"  # 密钥照常返回
    assert calls and calls[0][0] == "icacls"  # 确实尝试过 icacls
    assert "无法自动收紧" in caplog.text  # 仅中文提示


def test_windows_icacls_nonzero_returncode_only_warns(
    home: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv(vault.ENV_GLM_API_KEY, raising=False)
    _make_key_file(home, "sk-file0000000000\n")
    monkeypatch.setattr(vault.os, "name", "nt")

    class _FakeResult:
        returncode = 1

    monkeypatch.setattr(
        vault.subprocess, "run", lambda *a, **k: _FakeResult()
    )
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    assert get_glm_key(Config()) == "sk-file0000000000"
    assert "未能自动收紧权限" in caplog.text


def test_posix_chmod_branch_logs_hint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(vault.os, "name", "posix")
    p = tmp_path / "glm_key"
    p.write_text("sk-x000000000000\n", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=vault.logger.name)

    vault._tighten_key_file_permissions(p)  # 不抛异常即可

    assert "chmod 600" in caplog.text


# ---------------------------------------------------------------------------
# redact:打码规则
# ---------------------------------------------------------------------------


def test_redact_masks_sk_and_id_prefixes() -> None:
    assert redact("sk-abcdefgh123456") == "sk-a****"
    assert redact("id-12345678abcdef") == "id-1****"
    # 前缀后不足 8 位:视为普通字符串,不打码
    assert redact("sk-abc") == "sk-abc"


def test_redact_masks_long_hex_like_tokens() -> None:
    assert redact("a" * 32) == "aaaa****"
    assert redact("0123456789abcdef0123456789abcdef") == "0123****"
    assert redact("a" * 31) == "a" * 31  # 不足 32 位不动


def test_redact_masks_bearer_strings() -> None:
    assert redact("Bearer sk-abcdefgh12345") == "Bear****"


def test_redact_sensitive_dict_keys() -> None:
    obj = {
        "api_key": "any-value",
        "UserPassword": 12345,
        "auth_token": ["x"],
        "client_secret": {"deep": "sk-inner00000000"},
        "name": "张三",
    }
    out = redact(obj)
    assert out["api_key"] == "****"
    assert out["UserPassword"] == "****"
    assert out["auth_token"] == "****"
    assert out["client_secret"] == "****"
    assert out["name"] == "张三"


def test_redact_nested_containers_and_short_strings_kept() -> None:
    obj = {
        "note": ["sk-nest000000000", {"refresh_token": "t"}],
        "meta": {"url": "https://example.com/a?x=1", "ver": "1.0.2"},
        "pair": ("sk-tup0000000000", 5),
        "n": 7,
        "ratio": 0.5,
        "flag": True,
        "none": None,
    }
    out = redact(obj)

    assert out["note"][0] == "sk-n****"
    assert out["note"][1] == {"refresh_token": "****"}
    assert out["meta"] == {"url": "https://example.com/a?x=1", "ver": "1.0.2"}
    assert out["pair"] == ("sk-t****", 5)
    assert isinstance(out["pair"], tuple)
    assert (out["n"], out["ratio"], out["flag"], out["none"]) == (7, 0.5, True, None)


def test_redact_does_not_mutate_input() -> None:
    inner = ["sk-orig0000000000"]
    original = {"api_key": "v", "items": inner, "auth": "Bearer abcdef"}
    snapshot = {"api_key": "v", "items": ["sk-orig0000000000"], "auth": "Bearer abcdef"}

    out = redact(original)

    assert original == snapshot  # 入参保持不变
    assert original["items"] is inner
    assert out is not original
    assert out["items"] is not inner  # 返回同构新对象
    assert out == {"api_key": "****", "items": ["sk-o****"], "auth": "Bear****"}


# ---------------------------------------------------------------------------
# AuditChain.wrap:哈希口径
# ---------------------------------------------------------------------------


def test_wrap_layout_and_hash_formula(fixed_ts: None) -> None:
    rec = AuditChain().wrap("prev0", "scan_started", site_url="http://a.example", ok=True)

    assert rec["ts"] == FIXED_TS
    assert rec["event"] == "scan_started"
    assert rec["site_url"] == "http://a.example"
    assert rec["ok"] is True
    assert rec["prev_hash"] == "prev0"
    assert len(rec["entry_hash"]) == 32

    rest = {k: v for k, v in rec.items() if k != "entry_hash"}
    expected = hashlib.sha256(
        ("prev0" + "|" + json.dumps(rest, sort_keys=True, ensure_ascii=False)).encode("utf-8")
    ).hexdigest()[:32]
    assert rec["entry_hash"] == expected


def test_wrap_hash_stable_to_field_order(fixed_ts: None) -> None:
    chain = AuditChain()
    h1 = chain.wrap("", "evt", a=1, b="中文")["entry_hash"]
    h2 = chain.wrap("", "evt", b="中文", a=1)["entry_hash"]
    assert h1 == h2


def test_wrap_hash_changes_with_field_value(fixed_ts: None) -> None:
    chain = AuditChain()
    base = chain.wrap("", "evt", a=1)
    assert chain.wrap("", "evt", a=2)["entry_hash"] != base["entry_hash"]
    assert chain.wrap("", "other", a=1)["entry_hash"] != base["entry_hash"]
    assert chain.wrap("", "evt", a=1) == base  # 同参确定性


# ---------------------------------------------------------------------------
# AuditChain.verify:链校验
# ---------------------------------------------------------------------------


def test_verify_passes_three_record_chain(tmp_path: pathlib.Path, fixed_ts: None) -> None:
    p = tmp_path / "audit.jsonl"
    records = _build_chain(3)
    _write_jsonl(p, [_dumps(r) for r in records])

    ok, msg = AuditChain().verify(str(p))

    assert ok is True
    assert msg == "校验通过:3 条哈希链记录,0 条旧格式跳过"


def test_verify_detects_tampered_middle_record(
    tmp_path: pathlib.Path, fixed_ts: None
) -> None:
    p = tmp_path / "audit.jsonl"
    records = _build_chain(3)
    tampered = dict(records[1])
    tampered["event"] = "伪造事件"
    _write_jsonl(p, [_dumps(records[0]), _dumps(tampered), _dumps(records[2])])

    ok, msg = AuditChain().verify(str(p))

    assert ok is False
    assert "第 2 行被篡改或断链" in msg


def test_verify_detects_broken_chain_after_line_removal(
    tmp_path: pathlib.Path, fixed_ts: None
) -> None:
    p = tmp_path / "audit.jsonl"
    records = _build_chain(3)
    del records[1]  # 删掉中间行 → 第 3 行 prev_hash 接不上
    _write_jsonl(p, [_dumps(r) for r in records])

    ok, msg = AuditChain().verify(str(p))

    assert ok is False
    assert "第 2 行被篡改或断链" in msg  # 断链定位到实际出错的物理行


def test_verify_counts_legacy_lines_and_still_passes(
    tmp_path: pathlib.Path, fixed_ts: None
) -> None:
    p = tmp_path / "audit.jsonl"
    legacy = {"ts": "2025-01-01T00:00:00+08:00", "event": "old_event", "site": "http://old"}
    records = _build_chain(2)
    _write_jsonl(
        p,
        [
            _dumps(legacy),            # 旧行在前
            _dumps(records[0]),
            _dumps(legacy),            # 旧行夹杂
            _dumps(records[1]),
        ],
    )

    ok, msg = AuditChain().verify(str(p))

    assert ok is True
    assert msg == "校验通过:2 条哈希链记录,2 条旧格式跳过"


def test_verify_empty_file_reports_no_records(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text("", encoding="utf-8")

    ok, msg = AuditChain().verify(str(p))

    assert ok is True
    assert msg == "无记录"


def test_verify_missing_file_reports_no_records(tmp_path: pathlib.Path) -> None:
    ok, msg = AuditChain().verify(str(tmp_path / "not_exist.jsonl"))
    assert ok is True
    assert msg == "无记录"


def test_verify_unparseable_line_is_tampering(tmp_path: pathlib.Path, fixed_ts: None) -> None:
    p = tmp_path / "audit.jsonl"
    records = _build_chain(2)
    _write_jsonl(p, [_dumps(records[0]), "{broken-json", _dumps(records[1])])

    ok, msg = AuditChain().verify(str(p))

    assert ok is False
    assert "第 2 行被篡改或断链" in msg


# ---------------------------------------------------------------------------
# V5 升级:verify 流式 / 非法 UTF-8 / redact 深度防护 / 遥测 / icacls 字节捕获
# ---------------------------------------------------------------------------


def test_v5_verify_streams_large_chain(tmp_path: pathlib.Path, fixed_ts: None) -> None:
    """V5 性能:5000 条链流式逐行校验(不再整读入内存),结论完整。"""
    p = tmp_path / "audit_big.jsonl"
    records = _build_chain(5000)
    with p.open("w", encoding="utf-8") as fh:  # 测试侧同样流式落盘
        for rec in records:
            fh.write(_dumps(rec) + "\n")

    ok, msg = AuditChain().verify(str(p))

    assert ok is True
    assert msg == "校验通过:5000 条哈希链记录,0 条旧格式跳过"


def test_v5_verify_invalid_utf8_is_tampering_not_crash(tmp_path: pathlib.Path) -> None:
    """V5 健壮:非法 UTF-8 字节不再抛 UnicodeDecodeError,按行篡改回报。"""
    p = tmp_path / "audit_bin.jsonl"
    p.write_bytes(b'{"event": "legacy"}\n\xff\xfe\xba\xad\n')

    ok, msg = AuditChain().verify(str(p))  # 不得抛 UnicodeDecodeError

    assert ok is False
    assert "第 2 行被篡改或断链" in msg


def test_v5_verify_telemetry_counters(tmp_path: pathlib.Path, fixed_ts: None) -> None:
    """V5 可观测:每次 verify 计 vault.verify,失败另计 vault.verify_failures。"""
    telemetry.reset()
    good = tmp_path / "good.jsonl"
    recs = _build_chain(2)
    _write_jsonl(good, [_dumps(r) for r in recs])
    bad = tmp_path / "bad.jsonl"
    _write_jsonl(bad, ["{broken"])

    assert AuditChain().verify(str(good))[0] is True
    assert AuditChain().verify(str(bad))[0] is False

    counters = telemetry.snapshot()["counters"]
    assert counters["vault.verify"] == 2
    assert counters["vault.verify_failures"] == 1


def test_v5_redact_depth_cap_masks_deep_nesting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V5 健壮:超 50 层嵌套整体打码 + WARNING,不抛 RecursionError。"""
    deep: Any = {"note": "sk-deep0000000000"}
    for _ in range(vault._MAX_REDACT_DEPTH + 10):
        deep = {"level": deep}
    with caplog.at_level(logging.WARNING, logger=vault.logger.name):
        out = redact(deep)  # 不得抛 RecursionError
    assert "恶意嵌套" in caplog.text

    node = out
    for _ in range(vault._MAX_REDACT_DEPTH + 1):  # 顶层深度 0,深度 51 起整体打码
        assert isinstance(node, dict)
        node = node["level"]
    assert node == "****"


def test_v5_redact_counts_items_in_telemetry() -> None:
    """V5 可观测:redact 处理条数计入 vault.redact_items(仅数字,红线 17)。"""
    telemetry.reset()
    redact({"a": [1, 2, 3], "b": {"c": "x"}})
    # 节点计数:根 dict + list + 3 个标量 + b dict + "x" = 7
    assert telemetry.snapshot()["counters"]["vault.redact_items"] == 7


def test_v5_icacls_output_captured_as_bytes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 健壮(Windows):vault 的 icacls 输出按字节捕获,绝不解码(与 keys 同口径)。"""
    monkeypatch.setattr(vault.os, "name", "nt")
    monkeypatch.setenv("USERNAME", "tester")

    calls: list[dict] = []

    class _FakeResult:
        returncode = 0
        stdout = b"\xd5\xd5\xca\xd4\xb2\xe2\xca\xd4"  # GBK 字节:证明不解码
        stderr = b""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeResult:
        calls.append(dict(kwargs))
        return _FakeResult()

    monkeypatch.setattr(vault.subprocess, "run", _fake_run)

    vault._tighten_key_file_permissions(tmp_path / "glm_key")

    assert len(calls) == 1
    assert calls[0]["capture_output"] is True
    assert not calls[0].get("text")  # 绝不解码 icacls 输出,只看退出码
