"""A54:netsentinel.security.bundle_sign 单元测试(离线,只写 tmp_path)。

V5:多块大文件流式哈希与整读复算一致、resolve 调用计数(提出循环)、
telemetry(sign.manifest / sign.verify 计时,sign.verify_fail 仅失败计数)。

A192:策略模式双算法——Ed25519(RFC 8032)sign→verify 往返、签名块新字段
(algo / public_key / timestamp_proof)、时间证明入签(篡改即拒)、独立验签
(无需 HMAC 密钥)、旧格式包(无 algo 字段)逐字节兼容回归。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import urllib.request

import pytest

from netsentinel import telemetry
from netsentinel.security import bundle_sign, timestamp as ts_mod
from netsentinel.security.bundle_sign import KEY_FILE_NAME, BundleSigner

ENV_NAME = "NETSENTINEL_SIGN_KEY"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")

#: A192 确定性测试种子(固定,便于复算公钥)
SEED_A = bytes.fromhex("11" * 32)
SEED_B = bytes.fromhex("22" * 32)

# 在 autouse 屏蔽补丁生效前留存真实实现,供权限收紧专项用例直接调用
_REAL_TIGHTEN = bundle_sign._tighten_key_file_permissions


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """保证默认环境下没有签名密钥环境变量,隔离真实机器配置。"""
    monkeypatch.delenv(ENV_NAME, raising=False)


@pytest.fixture(autouse=True)
def _no_tighten(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通用例不真正执行权限收紧(icacls/chmod 由专项用例覆盖)。"""
    monkeypatch.setattr(bundle_sign, "_tighten_key_file_permissions", lambda path: None)


def _make_bundle(root: pathlib.Path, name: str = "bundle") -> tuple[pathlib.Path, dict]:
    """构造最小证据包:manifest.json + 2 个证据文件 + 1 个清单外附加文件 summary.md。"""
    bdir = root / name
    bdir.mkdir(parents=True)
    (bdir / "a.png").write_bytes(b"\x89PNG-image-A")
    (bdir / "b.png").write_bytes(b"\x89PNG-image-B")
    manifest = {
        "report": {"site_url": "http://example.invalid", "verdict": "nsfw"},
        "files": [
            {"path": "a.png", "sha256": "0" * 64, "bytes": 14, "role": "screenshot"},
            {"path": "b.png", "sha256": "0" * 64, "bytes": 14, "role": "image"},
        ],
    }
    (bdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (bdir / "summary.md").write_text("# 摘要\n\n声明:本证据包须经人工核实。\n", encoding="utf-8")
    return bdir, manifest


def _load_manifest(bdir: pathlib.Path) -> dict:
    return json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))


def _save_manifest(bdir: pathlib.Path, data: dict) -> None:
    (bdir / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _signer(tmp_path: pathlib.Path, sub: str = "data") -> BundleSigner:
    return BundleSigner(data_dir=str(tmp_path / sub))


# ---------------------------------------------------------------------------
# 签名往返与签名块结构
# ---------------------------------------------------------------------------


def test_sign_verify_roundtrip(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    value = signer.sign_manifest(str(bdir))

    assert isinstance(value, str) and _HEX64.match(value)
    ok, msg = signer.verify(str(bdir))
    assert ok is True
    assert msg == "校验通过:2 个文件,完整性完好"


def test_signature_block_fields(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    value = _signer(tmp_path).sign_manifest(str(bdir))

    sig = _load_manifest(bdir)["signature"]
    assert sig["algo"] == "HMAC-SHA256"
    assert sig["value"] == value
    assert isinstance(sig["signed_at"], str) and "T" in sig["signed_at"]
    assert sig["files_hashed"] == 2
    # 报告等既有字段保持原样,只追加 signature 键
    assert _load_manifest(bdir)["report"]["site_url"] == "http://example.invalid"


def test_signature_formula_matches_contract(tmp_path: pathlib.Path) -> None:
    """按契约口径手工复算:HMAC-SHA256(密钥, dumps({"files","extra"}, sort_keys))。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    value = signer.sign_manifest(str(bdir))

    key = (tmp_path / "data" / KEY_FILE_NAME).read_text(encoding="utf-8").strip()
    files = {
        "a.png": hashlib.sha256((bdir / "a.png").read_bytes()).hexdigest(),
        "b.png": hashlib.sha256((bdir / "b.png").read_bytes()).hexdigest(),
    }
    extra = [
        {
            "path": "summary.md",
            "sha256": hashlib.sha256((bdir / "summary.md").read_bytes()).hexdigest(),
        }
    ]
    payload = json.dumps({"files": files, "extra": extra}, sort_keys=True, ensure_ascii=False)
    expected = hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    assert value == expected


def test_resign_is_deterministic(tmp_path: pathlib.Path) -> None:
    """同一密钥重复签名结果一致(payload 与 signed_at 无关)。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    v1 = signer.sign_manifest(str(bdir))
    v2 = signer.sign_manifest(str(bdir))
    assert v1 == v2
    assert signer.verify(str(bdir))[0] is True


# ---------------------------------------------------------------------------
# 篡改检出
# ---------------------------------------------------------------------------


def test_tampered_evidence_file_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "a.png").write_bytes(b"\x89PNG-image-A-TAMPERED")
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert msg == "签名不匹配:内容被篡改或密钥不符"


def test_tampered_extra_file_detected(tmp_path: pathlib.Path) -> None:
    """清单外的 summary.md 在链尾 extra 段内,动一个字也检出。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "summary.md").write_text("# 摘要\n\n被人改过的结论文本\n", encoding="utf-8")
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "签名不匹配" in msg


def test_extra_file_added_after_signing_detected(tmp_path: pathlib.Path) -> None:
    """签名后往目录塞入新文件(extra 段变化)→ 检出。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "rogue.png").write_bytes(b"smuggled")
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "签名不匹配" in msg


def test_deleted_file_reports_missing(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "b.png").unlink()
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "缺失" in msg
    assert "b.png" in msg
    assert "签名比对" in msg  # 仍附签名比对结果


def test_missing_at_sign_time_still_signs(tmp_path: pathlib.Path) -> None:
    """签名时文件缺失:不拒绝签名,记 MISSING,verify 判 False 并点名文件。"""
    bdir, _ = _make_bundle(tmp_path)
    (bdir / "b.png").unlink()
    signer = _signer(tmp_path)

    value = signer.sign_manifest(str(bdir))
    assert _HEX64.match(value)
    sig = _load_manifest(bdir)["signature"]
    assert sig["files_hashed"] == 1  # 只哈希到 a.png

    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "缺失" in msg and "b.png" in msg


def test_manifest_list_add_entry_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["files"].append({"path": "ghost.png", "sha256": "1" * 64, "bytes": 3, "role": "image"})
    _save_manifest(bdir, data)
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "ghost.png" in msg  # 新增项无对应文件 → 缺失 + 比对不匹配


def test_manifest_list_remove_entry_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["files"].pop(0)  # 删掉 a.png 清单项:文件沦为 extra,链变化
    _save_manifest(bdir, data)
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "签名不匹配" in msg


def test_signature_value_tampered_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["signature"]["value"] = "0" * 64
    _save_manifest(bdir, data)
    assert signer.verify(str(bdir)) == (False, "签名不匹配:内容被篡改或密钥不符")


def test_unsigned_bundle(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    ok, msg = _signer(tmp_path).verify(str(bdir))
    assert ok is False
    assert msg == "未签名"


def test_broken_signature_field(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["signature"] = {"algo": "HMAC-SHA256"}  # 缺 value
    _save_manifest(bdir, data)
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "损坏" in msg

    data = _load_manifest(bdir)
    data["signature"] = "not-an-object"
    _save_manifest(bdir, data)
    assert signer.verify(str(bdir))[0] is False


def test_bad_or_missing_manifest(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)

    ok, msg = signer.verify(str(tmp_path / "no-such-bundle"))
    assert ok is False and "不存在" in msg

    (bdir / "manifest.json").write_text("{broken json", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        signer.sign_manifest(str(bdir))
    ok, msg = signer.verify(str(bdir))
    assert ok is False and "manifest" in msg


# ---------------------------------------------------------------------------
# 密钥:环境变量优先 / 自动生成与复用 / 轮换 / 无密钥提示
# ---------------------------------------------------------------------------


def test_env_key_priority_over_generated(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """env 密钥优先:用 env 密钥签 → env 存在时验过;换生成密钥验 → False。"""
    bdir, _ = _make_bundle(tmp_path)
    monkeypatch.setenv(ENV_NAME, "env-secret-key-material")
    signer = _signer(tmp_path)

    signer.sign_manifest(str(bdir))
    assert not (tmp_path / "data" / KEY_FILE_NAME).exists()  # env 优先,不生成文件
    assert signer.verify(str(bdir))[0] is True

    monkeypatch.delenv(ENV_NAME, raising=False)  # 改用自动生成的密钥验
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "签名不匹配" in msg


def test_custom_env_name(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bdir, _ = _make_bundle(tmp_path)
    monkeypatch.setenv("NETSENTINEL_OTHER_SIGN_KEY", "another-secret")
    signer = BundleSigner(data_dir=str(tmp_path / "d"), env_name="NETSENTINEL_OTHER_SIGN_KEY")

    signer.sign_manifest(str(bdir))
    assert signer.verify(str(bdir))[0] is True


def test_signkey_autogenerated_and_reused(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)

    _signer(tmp_path).sign_manifest(str(bdir))
    key_file = tmp_path / "data" / KEY_FILE_NAME
    assert key_file.is_file()
    first = key_file.read_text(encoding="utf-8").strip()
    assert _HEX64.match(first)  # hex 32B

    # 新实例复用同一密钥文件(不重新生成),旧签名仍可验过
    ok, msg = BundleSigner(data_dir=str(tmp_path / "data")).verify(str(bdir))
    assert ok is True
    assert key_file.read_text(encoding="utf-8").strip() == first


def test_rotate_key_invalidates_old_signature(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))
    old_key = (tmp_path / "data" / KEY_FILE_NAME).read_text(encoding="utf-8").strip()
    assert signer.verify(str(bdir))[0] is True

    new_key = signer.rotate_key()
    assert _HEX64.match(new_key)
    assert new_key != old_key
    assert (tmp_path / "data" / KEY_FILE_NAME).read_text(encoding="utf-8").strip() == new_key

    ok, msg = signer.verify(str(bdir))  # 旧签名失效
    assert ok is False
    assert "签名不匹配" in msg

    signer.sign_manifest(str(bdir))  # 用新密钥重签后恢复
    assert signer.verify(str(bdir))[0] is True


def test_rotate_with_env_key_only_rotates_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env 密钥生效时轮换只改文件:清除 env 前旧签名仍可验过。"""
    bdir, _ = _make_bundle(tmp_path)
    monkeypatch.setenv(ENV_NAME, "env-secret-key-material")
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    new_key = signer.rotate_key()
    assert (tmp_path / "data" / KEY_FILE_NAME).read_text(encoding="utf-8").strip() == new_key
    assert signer.verify(str(bdir))[0] is True  # env 仍优先,旧签名未失效


def test_no_available_key_returns_hint(tmp_path: pathlib.Path) -> None:
    """密钥既无 env 又无法自动生成(数据目录不可写)→ verify 提示、sign 抛错。"""
    bdir, _ = _make_bundle(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("不是一个目录", encoding="utf-8")
    signer = BundleSigner(data_dir=str(blocker))

    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "密钥" in msg
    with pytest.raises(ValueError, match="密钥"):
        signer.sign_manifest(str(bdir))


# ---------------------------------------------------------------------------
# 权限收紧(真实调用,失败仅提示不抛出)
# ---------------------------------------------------------------------------


def test_real_permission_tighten_never_raises(tmp_path: pathlib.Path) -> None:
    key_file = tmp_path / KEY_FILE_NAME
    key_file.write_text("f" * 64, encoding="utf-8")
    _REAL_TIGHTEN(key_file)  # POSIX chmod 600 / Windows icacls;失败仅记日志
    assert key_file.read_text(encoding="utf-8") == "f" * 64


# ---------------------------------------------------------------------------
# V5:流式哈希正确性 / resolve 调用计数 / telemetry
# ---------------------------------------------------------------------------


def test_v5_streaming_hash_large_file_matches_full_read(
    tmp_path: pathlib.Path,
) -> None:
    """V5 流式确认:>1MiB 多块文件的签名与整读手工复算(契约口径)一致。"""
    bdir, _ = _make_bundle(tmp_path)
    payload = os.urandom(3 * 1024 * 1024 + 123)  # 跨 3 个 1MiB 哈希分块
    (bdir / "a.png").write_bytes(payload)
    signer = _signer(tmp_path)
    value = signer.sign_manifest(str(bdir))

    key = (tmp_path / "data" / KEY_FILE_NAME).read_text(encoding="utf-8").strip()
    files = {
        "a.png": hashlib.sha256(payload).hexdigest(),
        "b.png": hashlib.sha256((bdir / "b.png").read_bytes()).hexdigest(),
    }
    extra = [
        {
            "path": "summary.md",
            "sha256": hashlib.sha256((bdir / "summary.md").read_bytes()).hexdigest(),
        }
    ]
    expected = hmac.new(
        key.encode("utf-8"),
        json.dumps(
            {"files": files, "extra": extra}, sort_keys=True, ensure_ascii=False
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert value == expected
    assert signer.verify(str(bdir)) == (True, "校验通过:2 个文件,完整性完好")


def test_v5_sign_verify_telemetry(tmp_path: pathlib.Path) -> None:
    """V5 可观测:sign.manifest / sign.verify 计时;verify_fail 仅失败时计数。"""
    telemetry.reset()
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)

    signer.sign_manifest(str(bdir))
    assert telemetry.snapshot()["timers"]["sign.manifest"]["count"] == 1
    assert "sign.verify" not in telemetry.snapshot()["timers"]  # sign 不触发 verify 计时

    assert signer.verify(str(bdir))[0] is True
    snap = telemetry.snapshot()
    assert snap["timers"]["sign.verify"]["count"] == 1
    assert "sign.verify_fail" not in snap["counters"]  # 通过时绝不计数

    (bdir / "a.png").write_bytes(b"\x89PNG-image-A-TAMPERED")
    assert signer.verify(str(bdir))[0] is False
    assert telemetry.snapshot()["counters"]["sign.verify_fail"] == 1


def test_v5_resolve_hoisted_out_of_loop(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 性能(调用计数):bundle 根目录只 resolve 一次。

    4 个清单文件 → resolve 调用 ≤ 5 次(base 1 次 + 每文件 1 次);
    旧实现为每文件 2 次(base + target)共 9 次。
    """
    bdir, _ = _make_bundle(tmp_path)
    for name in ("c.png", "d.png"):
        (bdir / name).write_bytes(name.encode())
    data = _load_manifest(bdir)
    data["files"].extend(
        {"path": name, "sha256": "0" * 64, "bytes": 5, "role": "image"}
        for name in ("c.png", "d.png")
    )
    _save_manifest(bdir, data)

    calls = {"n": 0}
    real_resolve = pathlib.Path.resolve

    def counting_resolve(self, *args, **kwargs):
        calls["n"] += 1
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", counting_resolve)
    _signer(tmp_path).sign_manifest(str(bdir))
    monkeypatch.undo()  # 计数到此为止

    assert calls["n"] <= 5  # 1(base)+ 4(清单文件);旧实现 2n+1 = 9


# ---------------------------------------------------------------------------
# A192:Ed25519 策略(双算法签名 + 时间证明入签)
# ---------------------------------------------------------------------------


def _ed_signer(tmp_path: pathlib.Path, sub: str = "d-ed", seed: bytes = SEED_A) -> BundleSigner:
    """Ed25519 签名器:seed 构造注入,计数器落独立目录。"""
    return BundleSigner(
        data_dir=str(tmp_path / sub), algo="ed25519", ed25519_seed=seed
    )


def test_a192_ed25519_sign_verify_roundtrip(tmp_path: pathlib.Path) -> None:
    """Ed25519 策略:签名值 128 hex,sign→verify 往返通过。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    value = signer.sign_manifest(str(bdir))

    assert isinstance(value, str) and _HEX128.match(value)
    ok, msg = signer.verify(str(bdir))
    assert ok is True
    assert msg.startswith("校验通过:2 个文件,完整性完好")


def test_a192_ed25519_signature_block_fields(tmp_path: pathlib.Path) -> None:
    """签名块新字段:algo=ed25519、public_key(hex 且等于派生公钥)、timestamp_proof。"""
    from netsentinel.security import ed25519

    bdir, _ = _make_bundle(tmp_path)
    _ed_signer(tmp_path).sign_manifest(str(bdir))

    sig = _load_manifest(bdir)["signature"]
    assert sig["algo"] == "ed25519"
    assert sig["public_key"] == ed25519.public_key(SEED_A).hex()
    assert _HEX64.match(sig["public_key"])
    assert sig["files_hashed"] == 2
    proof = sig["timestamp_proof"]
    assert isinstance(proof, dict) and proof["mono_counter"] == 1
    assert proof["source"] == "local"
    assert proof["utc_iso"].endswith("+00:00")
    # 旧字段仍在,报告正文不受影响
    assert isinstance(sig["signed_at"], str) and "T" in sig["signed_at"]
    assert _load_manifest(bdir)["report"]["site_url"] == "http://example.invalid"


def test_a192_ed25519_verify_without_hmac_key(tmp_path: pathlib.Path) -> None:
    """非对称价值:验证方数据目录不可写(无 HMAC 密钥)仍可独立验签。"""
    bdir, _ = _make_bundle(tmp_path)
    _ed_signer(tmp_path).sign_manifest(str(bdir))

    blocker = tmp_path / "blocker"
    blocker.write_text("不是一个目录", encoding="utf-8")
    independent = BundleSigner(
        data_dir=str(blocker), algo="ed25519", ed25519_seed=SEED_B  # seed 都不需要对
    )
    assert independent.verify(str(bdir)) == _ed_signer(tmp_path).verify(str(bdir))
    assert independent.verify(str(bdir))[0] is True


def test_a192_ed25519_tampered_evidence_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "a.png").write_bytes(b"\x89PNG-image-A-TAMPERED")
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert msg == "签名不匹配:内容被篡改或公钥不符"


def test_a192_ed25519_extra_file_added_detected(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "rogue.png").write_bytes(b"smuggled")
    assert signer.verify(str(bdir))[0] is False


def test_a192_ed25519_missing_file_reports_missing(tmp_path: pathlib.Path) -> None:
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    signer.sign_manifest(str(bdir))

    (bdir / "b.png").unlink()
    ok, msg = signer.verify(str(bdir))
    assert ok is False and "缺失" in msg and "b.png" in msg


def test_a192_ed25519_wrong_public_key_rejected(tmp_path: pathlib.Path) -> None:
    """签名块公钥被替换成别家合法公钥 → 验签不通过。"""
    from netsentinel.security import ed25519

    bdir, _ = _make_bundle(tmp_path)
    _ed_signer(tmp_path).sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["signature"]["public_key"] = ed25519.public_key(SEED_B).hex()
    _save_manifest(bdir, data)
    ok, msg = _ed_signer(tmp_path).verify(str(bdir))
    assert ok is False
    assert msg == "签名不匹配:内容被篡改或公钥不符"


def test_a192_ed25519_timestamp_proof_is_signed(tmp_path: pathlib.Path) -> None:
    """时间证明在签名链内:回拨/改动 utc_iso 或 mono_counter 都验签失败。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    signer.sign_manifest(str(bdir))

    for field, value in (("utc_iso", "1999-01-01T00:00:00+00:00"), ("mono_counter", 999)):
        data = _load_manifest(bdir)
        data["signature"]["timestamp_proof"][field] = value
        _save_manifest(bdir, data)
        assert signer.verify(str(bdir))[0] is False, f"篡改 {field} 应被检出"


def test_a192_ed25519_signature_block_corruption(tmp_path: pathlib.Path) -> None:
    """签名块结构损坏:缺公钥 / 缺时间证明 / value 非 hex → 中文损坏提示。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _ed_signer(tmp_path)
    signer.sign_manifest(str(bdir))

    mutations = (
        lambda sig: sig.pop("public_key"),
        lambda sig: sig.pop("timestamp_proof"),
        lambda sig: sig.update(timestamp_proof="not-an-object"),
        lambda sig: sig.update(value="zz" * 64),  # 非 hex 签名值
        lambda sig: sig.update(value="0" * 127),  # hex 但长度非 64 字节
    )
    for mutate in mutations:
        data = _load_manifest(bdir)
        mutate(data["signature"])
        _save_manifest(bdir, data)
        ok, msg = signer.verify(str(bdir))
        assert ok is False, f"{mutate} 后应验签失败"
        assert "损坏" in msg or "签名不匹配" in msg


def test_a192_ed25519_missing_seed_raises(tmp_path: pathlib.Path) -> None:
    """ed25519 策略未注入 seed → sign 抛 ValueError(密钥注入风格:构造期校验)。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = BundleSigner(data_dir=str(tmp_path / "d-no-seed"), algo="ed25519")
    with pytest.raises(ValueError, match="ed25519_seed"):
        signer.sign_manifest(str(bdir))


def test_a192_constructor_algo_and_seed_validation() -> None:
    """构造期防错:未知 algo → ValueError;坏 seed 长度/类型 → ValueError;大小写不敏感。"""
    with pytest.raises(ValueError, match="不支持的签名算法"):
        BundleSigner(data_dir="d", algo="rsa-4096")
    with pytest.raises(ValueError, match="32 字节"):
        BundleSigner(data_dir="d", algo="ed25519", ed25519_seed=b"short")
    with pytest.raises(ValueError, match="字节类型"):
        BundleSigner(data_dir="d", algo="ed25519", ed25519_seed="11" * 32)  # type: ignore[arg-type]

    # 大小写不敏感归一化
    assert BundleSigner(data_dir="d", algo="HMAC-SHA256").algo == "hmac-sha256"
    assert BundleSigner(data_dir="d", algo="Ed25519", ed25519_seed=SEED_A).algo == "ed25519"
    assert BundleSigner(data_dir="d").algo == "hmac-sha256"  # 默认策略


# ---------------------------------------------------------------------------
# A192:HMAC 默认路径兼容 + 旧格式包(无 algo 字段)逐字节兼容回归
# ---------------------------------------------------------------------------


def test_a192_hmac_signature_block_gains_timestamp_proof(tmp_path: pathlib.Path) -> None:
    """HMAC 默认策略:签名块附带 timestamp_proof 元数据,但签名值口径不变。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    sig = _load_manifest(bdir)["signature"]
    assert sig["algo"] == "HMAC-SHA256"  # manifest 口径保持旧值
    assert "public_key" not in sig  # HMAC 不写公钥
    proof = sig["timestamp_proof"]
    assert proof["source"] == "local" and proof["mono_counter"] == 1
    assert signer.verify(str(bdir)) == (True, "校验通过:2 个文件,完整性完好")


def test_a192_hmac_value_unchanged_by_proof(tmp_path: pathlib.Path) -> None:
    """HMAC 载荷不含时间证明:重复签名 value 恒定(证明只作元数据)。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    v1 = signer.sign_manifest(str(bdir))
    first_counter = _load_manifest(bdir)["signature"]["timestamp_proof"]["mono_counter"]
    v2 = signer.sign_manifest(str(bdir))
    second_counter = _load_manifest(bdir)["signature"]["timestamp_proof"]["mono_counter"]

    assert v1 == v2  # 载荷与旧口径逐字节一致
    assert second_counter == first_counter + 1  # 计数器照常单调
    assert signer.verify(str(bdir))[0] is True


def test_a192_legacy_bundle_without_algo_field_verifies(tmp_path: pathlib.Path) -> None:
    """旧格式包回归:手工剥掉 algo 字段(最旧格式),原路径逐字节兼容验过。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    value = signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    del data["signature"]["algo"]  # 旧格式:无 algo 字段
    del data["signature"]["timestamp_proof"]  # 旧格式更没有时间证明字段
    _save_manifest(bdir, data)

    assert data["signature"]["value"] == value  # 签名值未被改写
    assert signer.verify(str(bdir)) == (True, "校验通过:2 个文件,完整性完好")
    # 篡改仍照常检出
    (bdir / "a.png").write_bytes(b"tampered")
    assert signer.verify(str(bdir))[0] is False


def test_a192_unsupported_algo_rejected(tmp_path: pathlib.Path) -> None:
    """manifest 写了未知名算法 → (False, 算法不受支持)。"""
    bdir, _ = _make_bundle(tmp_path)
    signer = _signer(tmp_path)
    signer.sign_manifest(str(bdir))

    data = _load_manifest(bdir)
    data["signature"]["algo"] = "rsa-sha256"
    _save_manifest(bdir, data)
    ok, msg = signer.verify(str(bdir))
    assert ok is False
    assert "签名算法不受支持" in msg and "rsa-sha256" in msg


def test_a192_verify_dispatches_on_manifest_algo_not_self(
    tmp_path: pathlib.Path,
) -> None:
    """验签按 manifest 的 algo 分发,与验证方自身策略无关(验证方零配置)。"""
    bdir, _ = _make_bundle(tmp_path)
    shared = str(tmp_path / "shared")  # 同一 data_dir:env→文件→自动生成密钥共源
    hmac_signer = BundleSigner(data_dir=shared)
    ed_signer = BundleSigner(data_dir=shared, algo="ed25519", ed25519_seed=SEED_A)

    hmac_signer.sign_manifest(str(bdir))
    assert ed_signer.verify(str(bdir))[0] is True  # HMAC 包,Ed25519 验证方可验

    ed_signer.sign_manifest(str(bdir))  # 重签为 ed25519
    assert hmac_signer.verify(str(bdir))[0] is True  # 反之亦然


def test_a192_injected_timestamp_prover_shared_counter(
    tmp_path: pathlib.Path,
) -> None:
    """时间证明签发器可注入:两个签名器共享计数器,mono_counter 跨包单调递增。"""
    prover = ts_mod.TimestampProver(counter_path=tmp_path / "c.json")
    signer_a = BundleSigner(
        data_dir=str(tmp_path / "d1"), algo="ed25519", ed25519_seed=SEED_A,
        timestamp_prover=prover,
    )
    signer_b = BundleSigner(
        data_dir=str(tmp_path / "d2"), algo="hmac-sha256",
        timestamp_prover=prover,
    )
    bdir1, _ = _make_bundle(tmp_path, name="bundle1")
    bdir2, _ = _make_bundle(tmp_path, name="bundle2")

    signer_a.sign_manifest(str(bdir1))
    c1 = _load_manifest(bdir1)["signature"]["timestamp_proof"]["mono_counter"]
    signer_b.sign_manifest(str(bdir2))
    c2 = _load_manifest(bdir2)["signature"]["timestamp_proof"]["mono_counter"]
    signer_a.sign_manifest(str(bdir1))
    c3 = _load_manifest(bdir1)["signature"]["timestamp_proof"]["mono_counter"]

    assert [c1, c2, c3] == [1, 2, 3]


def test_a192_default_prover_offline_and_persisted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认时间证明:tsa_url=None 零网络(urlopen 未被调用),计数器落 data_dir。"""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("默认禁网被违反")),
    )
    signer = _ed_signer(tmp_path, sub="d-off")
    bdir, _ = _make_bundle(tmp_path)
    signer.sign_manifest(str(bdir))
    assert signer.verify(str(bdir))[0] is True  # sign+verify 全程零网络

    counter_file = tmp_path / "d-off" / ts_mod.COUNTER_FILE_NAME
    assert json.loads(counter_file.read_text(encoding="utf-8"))["mono_counter"] == 1
