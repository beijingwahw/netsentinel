"""A11 netsentinel.evidence.packager 证据包构建测试。

离线、只在 tmp_path 下读写:伪造 SiteReport(2 页,各 1 截图 + 2 图片,
小文件真实落盘),验证目录结构 / manifest.json(sha256)/ summary.md 中文摘要 /
zip 完整性;另覆盖缺失文件跳过与同名冲突加序号两个分支。
V5:单遍流式"边复制边哈希"的读取调用计数与 sha 正确性、同名冲突集合检测、
复制失败清理半成品、telemetry(packager.bundle / files / missing)。
A205:产包签名策略接线——默认配置零签名零新字段(现状逐字节)、ed25519
配置(seed hex 注入,env > cfg)签名块含 algo/public_key/timestamp_proof
且可独立验签、坏 hex/短 seed/构造异常回退 HMAC+中文告警、签名失败回滚
绝不中断打包。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import zipfile
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ImageEvidence,
    ImageScore,
    PageSample,
    SiteReport,
    Verdict,
)
from netsentinel.evidence import packager
from netsentinel.evidence.packager import build_bundle
from netsentinel.security import ed25519
from netsentinel.security.bundle_sign import BundleSigner

SITE = "https://bad.example.test/"

#: A205 确定性测试种子(固定 hex,便于复算公钥;与 test_bundle_sign.SEED_A 同源)
SEED_A = bytes.fromhex("11" * 32)
SEED_B = bytes.fromhex("22" * 32)

#: 固定 HMAC 密钥(回退路径用,经环境变量注入;确定性,禁随机)
HMAC_KEY = "a205-fallback-hmac-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _scrub_sign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离签名相关环境变量,保证用例在干净确定的环境下运行。"""
    monkeypatch.delenv("NETSENTINEL_SIGN_KEY", raising=False)
    monkeypatch.delenv(packager.ED25519_SEED_ENV_NAME, raising=False)


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _write_file(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def make_cfg(tmp_path: Path) -> Config:
    """evidence_dir 与 data_dir 均指向 tmp_path,绝不写项目目录(签名密钥/计数器也隔离)。"""
    return Config(evidence_dir=str(tmp_path), data_dir=str(tmp_path / "data"))


def make_report(tmp_path: Path) -> SiteReport:
    """伪造报告:2 页 × (1 截图 + 2 图片)共 6 个真实小文件;评分含 ensemble 与非 ensemble。"""
    src = tmp_path / "src"
    pages: list[PageSample] = []
    evidences: list[ImageEvidence] = []
    for i in (1, 2):
        shot = _write_file(src / f"p{i}" / "shot.png", f"SCREENSHOT-P{i}".encode() * 4)
        ev1 = _write_file(src / f"p{i}" / "img_a.png", f"IMAGE-P{i}-A".encode() * 8)
        ev2 = _write_file(src / f"p{i}" / "img_b.png", f"IMAGE-P{i}-B".encode() * 8)
        e1 = ImageEvidence(
            path=ev1,
            url=f"{SITE}p{i}/img_a.png",
            source_page=f"{SITE}p{i}.html",
            width=400,
            height=300,
        )
        e2 = ImageEvidence(
            path=ev2,
            url=f"{SITE}p{i}/img_b.png",
            source_page=f"{SITE}p{i}.html",
            width=640,
            height=480,
        )
        pages.append(
            PageSample(
                url=f"{SITE}p{i}.html",
                screenshot_path=shot,
                image_evidences=[e1, e2],
                text_hint_hits=["提示词"],
            )
        )
        evidences.extend([e1, e2])

    scores = [
        ImageScore(image=evidences[0], model="stub", nsfw_prob=0.72),
        ImageScore(image=evidences[0], model="ensemble", nsfw_prob=0.9123),
        ImageScore(image=evidences[1], model="ensemble", nsfw_prob=0.9801),
        ImageScore(image=evidences[2], model="clip", nsfw_prob=0.55),
        ImageScore(image=evidences[3], model="ensemble", nsfw_prob=0.6400),
    ]
    return SiteReport(
        site_url=SITE,
        pages=pages,
        image_scores=scores,
        agg_nsw_prob=0.9801,
        nsw_image_count=2,
        verdict=Verdict.NSFW,
        needs_review=True,
        created_at="2026-10-01T10:00:00+08:00",
    )


# ---------------------------------------------------------------------------
# 主流程:目录 / manifest / summary / zip
# ---------------------------------------------------------------------------


def test_build_bundle_full_layout(tmp_path: Path) -> None:
    """完整打包:目录命名、manifest 文件数与 sha256、中文摘要、zip 全量可列。"""
    cfg = make_cfg(tmp_path)
    report = make_report(tmp_path)
    bundle = build_bundle(report, cfg)

    assert isinstance(bundle, EvidenceBundle)
    assert bundle.site_url == SITE
    assert bundle.zip_path  # 始终非空

    # 目录结构:<evidence_dir>/<safe_host>_<ts>/
    bundle_dir = Path(bundle.dir_path)
    assert bundle_dir.is_dir()
    assert bundle_dir.parent == tmp_path
    assert re.fullmatch(r"bad\.example\.test_\d{8}_\d{6}", bundle_dir.name)

    # manifest.json:可 json.load,报告全文 + 6 个文件的 sha256 清单
    assert bundle.manifest_path == str(bundle_dir / "manifest.json")
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert manifest["report"]["site_url"] == SITE
    assert manifest["report"]["verdict"] == "nsfw"
    assert len(manifest["report"]["pages"]) == 2
    assert len(manifest["report"]["image_scores"]) == 5

    files = manifest["files"]
    assert len(files) == 6  # 2 截图 + 4 图片
    roles = [e["role"] for e in files]
    assert roles.count("screenshot") == 2
    assert roles.count("image") == 4
    for entry in files:
        assert set(entry) == {"path", "sha256", "bytes", "role"}
        copied = bundle_dir / entry["path"]
        assert copied.is_file()
        assert entry["bytes"] == copied.stat().st_size > 0
        assert entry["sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()

    # summary.md:中文关键词 + 文件名 + 声明 + Top10(仅 ensemble、降序)
    summary = (bundle_dir / "summary.md").read_text(encoding="utf-8")
    assert "高置信" in summary  # verdict=nsfw 的中文映射
    assert "本证据包由辅助系统自动生成" in summary
    assert "人工核实" in summary
    assert "img_a.png" in summary and "img_b.png" in summary
    # 降序:0.9801 在 0.9123 之前,0.9123 在 0.6400 之前
    assert summary.index("0.9801") < summary.index("0.9123") < summary.index("0.6400")
    # 非 ensemble 评分不进 Top 表
    assert "0.7200" not in summary and "0.5500" not in summary

    # zip:存在、DEFLATED、能列出全部 8 个文件(6 证据 + manifest + summary)
    zip_path = Path(bundle.zip_path)
    assert zip_path.is_file()
    assert zip_path == bundle_dir.parent / f"{bundle_dir.name}.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        prefix = f"{bundle_dir.name}/"
        assert f"{prefix}manifest.json" in names
        assert f"{prefix}summary.md" in names
        for entry in files:
            assert f"{prefix}{entry['path']}" in names
        assert len(names) == 8
        assert all(
            info.compress_type == zipfile.ZIP_DEFLATED for info in zf.infolist()
        )


# ---------------------------------------------------------------------------
# 缺失文件:跳过不抛
# ---------------------------------------------------------------------------


def test_missing_files_skipped_without_error(tmp_path: Path) -> None:
    """截图与图片路径均不存在 → warning 跳过,manifest files 为空,zip 照常生成。"""
    cfg = make_cfg(tmp_path)
    page = PageSample(
        url=SITE,
        screenshot_path=str(tmp_path / "nope" / "shot.png"),
        image_evidences=[
            ImageEvidence(
                path=str(tmp_path / "nope" / "img.png"),
                url=f"{SITE}img.png",
                source_page=SITE,
            )
        ],
    )
    report = SiteReport(
        site_url=SITE,
        pages=[page],
        image_scores=[],
        agg_nsw_prob=0.60,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=True,
    )
    bundle = build_bundle(report, cfg)  # 不抛即通过

    bundle_dir = Path(bundle.dir_path)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert manifest["files"] == []
    assert manifest["report"]["verdict"] == "suspect"

    summary = (bundle_dir / "summary.md").read_text(encoding="utf-8")
    assert "疑似" in summary  # verdict=suspect 的中文映射
    assert "无图片评分记录" in summary

    assert bundle.zip_path
    with zipfile.ZipFile(bundle.zip_path) as zf:
        names = zf.namelist()
    assert len(names) == 2  # 仅 manifest.json + summary.md


# ---------------------------------------------------------------------------
# 同名冲突:加序号,不覆盖
# ---------------------------------------------------------------------------


def test_duplicate_names_get_suffix_and_no_overwrite(tmp_path: Path) -> None:
    """三页各带同名 same.png(内容不同)→ same.png / same_1.png / same_2.png,内容全保留。"""
    cfg = make_cfg(tmp_path)
    contents = [b"AAAAscreenshot", b"BBBBevidence", b"CCCCextra"]
    pages: list[PageSample] = []
    for i, data in enumerate(contents):
        path = _write_file(tmp_path / "src" / f"p{i}" / "same.png", data)
        pages.append(PageSample(url=f"{SITE}p{i}.html", screenshot_path=path))
    report = SiteReport(
        site_url=SITE,
        pages=pages,
        image_scores=[],
        agg_nsw_prob=0.60,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=True,
    )

    bundle = build_bundle(report, cfg)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    names = sorted(e["path"] for e in manifest["files"])
    assert names == ["same.png", "same_1.png", "same_2.png"]

    bundle_dir = Path(bundle.dir_path)
    got = {(bundle_dir / e["path"]).read_bytes() for e in manifest["files"]}
    assert got == set(contents)  # 三份内容都在,没有任何一份被覆盖


# ---------------------------------------------------------------------------
# safe_host:取不到主机名 → unknown;非法字符替换
# ---------------------------------------------------------------------------


def test_dir_name_safe_host_fallback(tmp_path: Path) -> None:
    """无主机名 → unknown_;含非法字符 → 替换为 _;CLEAN 映射为"未发现"。"""
    cfg = make_cfg(tmp_path)

    no_host = SiteReport(site_url="not a url at all", verdict=Verdict.CLEAN)
    b1 = build_bundle(no_host, cfg)
    assert Path(b1.dir_path).name.startswith("unknown_")
    assert Path(b1.zip_path).name.startswith("unknown_")
    assert Path(b1.zip_path).is_file()
    summary = (Path(b1.dir_path) / "summary.md").read_text(encoding="utf-8")
    assert "未发现" in summary  # verdict=clean 的中文映射

    weird = SiteReport(site_url="https://exa|mple.test/", verdict=Verdict.CLEAN)
    b2 = build_bundle(weird, cfg)
    assert Path(b2.dir_path).name.startswith("exa_mple.test_")


# ---------------------------------------------------------------------------
# V5:单遍流式"边复制边哈希"(读取调用计数,不用墙钟)
# ---------------------------------------------------------------------------


def test_v5_single_pass_copy_hash_large_file(
    tmp_path: Path, monkeypatch
) -> None:
    """V5 性能:大文件(跨 3 个 1MiB 块)单遍"边复制边哈希"。

    - 正确性:manifest 的 sha256 / bytes 与整读源文件的 hashlib 结果一致;
    - 读取调用计数:源文件恰好被打开读 **1 次**;证据目录内的目标文件
      **零次**读打开(旧实现 copy 后须整读目标一次算哈希)。
    """
    payload = os.urandom(3 * 1024 * 1024 + 777)  # 跨 3 个分块 + 非整块尾巴
    src = _write_file(tmp_path / "src" / "shot.png", payload)
    report = SiteReport(
        site_url=SITE,
        pages=[PageSample(url=SITE, screenshot_path=src)],
        image_scores=[],
        agg_nsw_prob=0.10,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=False,
    )

    read_opens = {"src": 0, "dest": 0}
    real_open = pathlib.Path.open

    def counting_open(self, mode="r", *args, **kwargs):
        if "r" in mode and self.suffix == ".png":
            read_opens["src" if self.parent.name == "src" else "dest"] += 1
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)
    bundle = build_bundle(report, make_cfg(tmp_path))
    monkeypatch.undo()  # 计数到此为止,后续断言的读取不再计入

    assert read_opens == {"src": 1, "dest": 0}

    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    (entry,) = manifest["files"]
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["bytes"] == len(payload)
    assert (Path(bundle.dir_path) / entry["path"]).read_bytes() == payload


# ---------------------------------------------------------------------------
# V5:telemetry(packager.bundle / packager.files / packager.missing)
# ---------------------------------------------------------------------------


def test_v5_packager_telemetry(tmp_path: Path) -> None:
    """V5 可观测:整体耗时记 timer("packager.bundle");文件数记 packager.files。"""
    telemetry.reset()
    bundle = build_bundle(make_report(tmp_path), make_cfg(tmp_path))
    assert bundle.zip_path
    snap = telemetry.snapshot()
    assert snap["timers"]["packager.bundle"]["count"] == 1
    assert snap["counters"]["packager.files"] == 6  # 2 截图 + 4 图片
    assert "packager.missing" not in snap["counters"]  # 无缺失时不计数


def test_v5_missing_evidence_counted(tmp_path: Path) -> None:
    """V5 可观测:缺失源文件计入 packager.missing,空路径(无证据)不算缺失。"""
    telemetry.reset()
    page = PageSample(
        url=SITE,
        screenshot_path=str(tmp_path / "nope" / "shot.png"),
        image_evidences=[
            ImageEvidence(
                path=str(tmp_path / "nope" / "img.png"),
                url=f"{SITE}img.png",
                source_page=SITE,
            )
        ],
    )
    report = SiteReport(
        site_url=SITE,
        pages=[page],
        image_scores=[],
        agg_nsw_prob=0.50,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=True,
    )
    bundle = build_bundle(report, make_cfg(tmp_path))
    snap = telemetry.snapshot()
    assert snap["counters"]["packager.missing"] == 2
    assert snap["counters"]["packager.files"] == 0
    assert bundle.zip_path  # 缺失只告警,打包照常完成


# ---------------------------------------------------------------------------
# V5:复制失败(如磁盘满)→ 清理半成品并跳过,绝不中断打包
# ---------------------------------------------------------------------------


def test_v5_copy_failure_skips_and_cleans(tmp_path: Path, monkeypatch) -> None:
    """V5 健壮性:目标写入抛 OSError → 该证据跳过、半成品被清理、打包不中断。"""
    src = _write_file(tmp_path / "src" / "shot.png", b"SHOULD-NOT-SURVIVE" * 16)
    report = SiteReport(
        site_url=SITE,
        pages=[PageSample(url=SITE, screenshot_path=src)],
        image_scores=[],
        agg_nsw_prob=0.10,
        nsw_image_count=0,
        verdict=Verdict.CLEAN,
        needs_review=False,
    )
    real_open = pathlib.Path.open

    def disk_full_open(self, mode="r", *args, **kwargs):
        if mode == "wb" and self.suffix == ".png":
            raise OSError("simulated disk full")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", disk_full_open)
    bundle = build_bundle(report, make_cfg(tmp_path))  # 绝不抛出
    monkeypatch.undo()

    bundle_dir = Path(bundle.dir_path)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert manifest["files"] == []
    assert list(bundle_dir.glob("*.png")) == []  # 半成品已清理
    with zipfile.ZipFile(bundle.zip_path) as zf:  # manifest + summary 照常打包
        assert len(zf.namelist()) == 2


# ---------------------------------------------------------------------------
# V5:同名冲突集合检测(多文件名唯一性)
# ---------------------------------------------------------------------------


def test_v5_many_duplicate_names_all_unique(tmp_path: Path) -> None:
    """V5 性能(集合冲突检测):8 个同名文件 → 8 个互不相同落盘名,内容全保留。"""
    contents = [f"DUPLICATE-CONTENT-{i}".encode() * 4 for i in range(8)]
    pages = [
        PageSample(
            url=f"{SITE}p{i}.html",
            screenshot_path=_write_file(
                tmp_path / "src" / f"p{i}" / "same.png", data
            ),
        )
        for i, data in enumerate(contents)
    ]
    report = SiteReport(
        site_url=SITE,
        pages=pages,
        image_scores=[],
        agg_nsw_prob=0.50,
        nsw_image_count=0,
        verdict=Verdict.SUSPECT,
        needs_review=True,
    )
    bundle = build_bundle(report, make_cfg(tmp_path))
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    names = [e["path"] for e in manifest["files"]]
    assert len(names) == len(set(names)) == 8
    bundle_dir = Path(bundle.dir_path)
    got = {(bundle_dir / n).read_bytes() for n in names}
    assert got == set(contents)  # 8 份内容全在,无覆盖


# ---------------------------------------------------------------------------
# A205:产包签名策略接线(默认零签名 / ed25519 显式配置 / 回退与安全)
# ---------------------------------------------------------------------------


def test_a205_default_config_no_signature_status_quo(tmp_path: Path) -> None:
    """兼容锁定:默认配置不签名——manifest 顶层键与现状一致(无 signature 字段)。

    不配置 bundle_sign_algo(或值为默认 hmac-sha256)时完全不构造签名器、
    不新增任何读写:manifest 仍是 {report, files} 两键,签名块"字段缺失"
    的语义与升级前逐字节一致;telemetry 也不出现任何 bundle.sign.* 计数。
    """
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        assert not hasattr(cfg, "bundle_sign_algo") or "hmac" in str(
            getattr(cfg, "bundle_sign_algo", "hmac-sha256")
        ).lower()
        bundle = build_bundle(make_report(tmp_path), cfg)

        manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
        assert set(manifest) == {"report", "files"}  # 快照:零新字段、零签名块
        assert len(manifest["files"]) == 6  # 本体行为与现状一致

        # 现状语义:未签名包在验签侧如实报"未签名"
        os.environ["NETSENTINEL_SIGN_KEY"] = HMAC_KEY
        try:
            ok, msg = BundleSigner(data_dir=str(tmp_path / "d")).verify(bundle.dir_path)
        finally:
            os.environ.pop("NETSENTINEL_SIGN_KEY", None)
        assert ok is False and msg == "未签名"

        snap = telemetry.snapshot()["counters"]
        assert not [k for k in snap if k.startswith("bundle.sign.")]  # 零签名计数
    finally:
        telemetry.reset()


def test_a205_ed25519_config_signs_and_verifies(tmp_path: Path) -> None:
    """显式 ed25519 + seed hex:签名块含 algo/public_key/timestamp_proof,独立验签通过。"""
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        cfg.bundle_sign_algo = "ed25519"
        cfg.ed25519_seed_hex = SEED_A.hex()
        bundle = build_bundle(make_report(tmp_path), cfg)

        manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
        sig = manifest["signature"]
        assert sig["algo"] == "ed25519"
        assert sig["public_key"] == ed25519.public_key(SEED_A).hex()
        assert re.fullmatch(r"[0-9a-f]{64}", sig["public_key"])
        assert re.fullmatch(r"[0-9a-f]{128}", sig["value"])
        assert sig["files_hashed"] == 6  # 2 截图 + 4 图片全部入签
        proof = sig["timestamp_proof"]
        assert isinstance(proof, dict) and proof["mono_counter"] >= 1
        assert proof["source"] == "local"  # 未配 tsa_url → 全离线
        assert manifest["report"]["site_url"] == SITE  # 报告正文不受影响

        # 独立验签:验证方不需要 HMAC 密钥,seed 对不对都行(公钥自足)
        independent = BundleSigner(
            data_dir=str(tmp_path / "independent"), algo="ed25519", ed25519_seed=SEED_B
        )
        ok, msg = independent.verify(bundle.dir_path)
        assert ok is True and msg.startswith("校验通过:6 个文件")

        # zip 内即已签名 manifest(签名发生在压缩之前,目录与 zip 口径一致)
        with zipfile.ZipFile(bundle.zip_path) as zf:
            name = next(n for n in zf.namelist() if n.endswith("/manifest.json"))
            zipped = json.loads(zf.read(name).decode("utf-8"))
        assert zipped["signature"] == sig

        snap = telemetry.snapshot()["counters"]
        assert snap["bundle.sign.ed25519"] == 1
        assert "bundle.sign.ed25519/hmac_fallback" not in snap
        assert "bundle.sign.skipped" not in snap
    finally:
        telemetry.reset()


def test_a205_env_seed_overrides_cfg_seed(tmp_path: Path) -> None:
    """seed 来源优先级:环境变量 NETSENTINEL_ED25519_SEED_HEX > cfg.ed25519_seed_hex。"""
    cfg = make_cfg(tmp_path)
    cfg.bundle_sign_algo = "ed25519"
    cfg.ed25519_seed_hex = SEED_B.hex()  # 配置给 B
    os.environ[packager.ED25519_SEED_ENV_NAME] = SEED_A.hex()  # 环境变量给 A → A 胜出
    try:
        bundle = build_bundle(make_report(tmp_path), cfg)
    finally:
        os.environ.pop(packager.ED25519_SEED_ENV_NAME, None)

    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
    assert manifest["signature"]["algo"] == "ed25519"
    assert manifest["signature"]["public_key"] == ed25519.public_key(SEED_A).hex()


@pytest.mark.parametrize(
    ("seed_hex", "why"),
    [
        (None, "缺 seed"),
        ("zz" * 32, "坏 hex"),
        ("11" * 16, "短 seed(16 字节)"),
        ("", "空字符串"),
    ],
)
def test_a205_ed25519_bad_seed_falls_back_to_hmac(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    seed_hex: str | None,
    why: str,
) -> None:
    """坏 hex/短 seed/缺 seed:中文 warning + 回退 HMAC-SHA256 默认策略(无新字段)。"""
    monkeypatch.setenv("NETSENTINEL_SIGN_KEY", HMAC_KEY)  # 固定 HMAC 密钥,确定性
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        cfg.bundle_sign_algo = "ed25519"
        if seed_hex is not None:
            cfg.ed25519_seed_hex = seed_hex
        with caplog.at_level(logging.WARNING, logger="netsentinel.evidence.packager"):
            bundle = build_bundle(make_report(tmp_path), cfg)

        assert "回退" in caplog.text and "HMAC-SHA256" in caplog.text, why
        sig = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))[
            "signature"
        ]
        assert sig["algo"] == "HMAC-SHA256"  # 默认算法保持 hmac-sha256
        assert "public_key" not in sig  # 签名块无 ed25519 专属新字段
        ok, _ = BundleSigner(data_dir=str(tmp_path / "d")).verify(bundle.dir_path)
        assert ok is True, why  # 回退签名同样完整可验
        assert (
            telemetry.snapshot()["counters"]["bundle.sign.ed25519/hmac_fallback"] == 1
        )
    finally:
        telemetry.reset()


def test_a205_signer_construction_error_safe_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """signer 构造异常(ed25519 路径抛错)→ 安全回退默认 HMAC 策略,打包不中断。"""
    monkeypatch.setenv("NETSENTINEL_SIGN_KEY", HMAC_KEY)

    class _FailingEd25519(BundleSigner):
        """ed25519 构造即抛错(其余策略照常)的替身。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            if str(kwargs.get("algo", "hmac-sha256")).lower() == "ed25519":
                raise ValueError("模拟 Ed25519 签名器构造失败")
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(packager, "BundleSigner", _FailingEd25519)
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        cfg.bundle_sign_algo = "ed25519"
        cfg.ed25519_seed_hex = SEED_A.hex()
        with caplog.at_level(logging.WARNING, logger="netsentinel.evidence.packager"):
            bundle = build_bundle(make_report(tmp_path), cfg)  # 绝不抛出

        assert "回退" in caplog.text
        sig = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))[
            "signature"
        ]
        assert sig["algo"] == "HMAC-SHA256"
        assert (
            telemetry.snapshot()["counters"]["bundle.sign.ed25519/hmac_fallback"] == 1
        )
    finally:
        telemetry.reset()


def test_a205_sign_failure_rolls_back_and_never_breaks_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """签名中途失败:manifest 回滚为未签名版本,打包照常完成(绝不中断)。"""
    monkeypatch.setattr(
        BundleSigner,
        "sign_manifest",
        lambda self, bundle_dir: (_ for _ in ()).throw(RuntimeError("模拟签名失败")),
    )
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        cfg.bundle_sign_algo = "ed25519"
        cfg.ed25519_seed_hex = SEED_A.hex()
        bundle = build_bundle(make_report(tmp_path), cfg)  # 绝不抛出

        manifest = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))
        assert set(manifest) == {"report", "files"}  # 回滚干净:无残留签名块
        assert len(manifest["files"]) == 6
        assert Path(bundle.zip_path).is_file()
        with zipfile.ZipFile(bundle.zip_path) as zf:
            assert len(zf.namelist()) == 8  # manifest + summary + 6 证据照常打包
        assert telemetry.snapshot()["counters"]["bundle.sign.skipped"] == 1
    finally:
        telemetry.reset()


def test_a205_tsa_url_wired_into_timestamp_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tsa_url 接线:显式配置 TSA 地址时时间证明带上该地址;请求失败安全降级本地。"""
    import urllib.request

    def _no_network(*args: object, **kwargs: object) -> None:
        raise OSError("测试环境不外呼")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    cfg = make_cfg(tmp_path)
    cfg.bundle_sign_algo = "ed25519"
    cfg.ed25519_seed_hex = SEED_A.hex()
    cfg.tsa_url = "http://127.0.0.1:19999/tsa"
    bundle = build_bundle(make_report(tmp_path), cfg)

    proof = json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))[
        "signature"
    ]["timestamp_proof"]
    assert proof["tsa_url"] == "http://127.0.0.1:19999/tsa"  # 已接进签发器
    assert proof["source"] == "local"  # 外呼失败 → 安全降级本地证明
    assert "TSA" in proof.get("reason", "")
    ok, _ = BundleSigner(
        data_dir=str(tmp_path / "d2"), algo="ed25519", ed25519_seed=SEED_B
    ).verify(bundle.dir_path)
    assert ok is True  # 降级不影响签名完整性


# ---------------------------------------------------------------------------
# A224 公开口:sign_bundle / zip_bundle(V14/A235 收口:私有别名已移除)
# ---------------------------------------------------------------------------
def test_a224_public_names_exported_and_v14_aliases_removed() -> None:
    """公开口导出:sign_bundle / zip_bundle 在 __all__ 且可调用;
    V14(A235)收口:一代兼容别名 _sign_bundle / _zip_bundle 已移除
    (符号缺席即回归通过,防止别名悄悄回流)。"""
    assert "sign_bundle" in packager.__all__ and "zip_bundle" in packager.__all__
    assert callable(packager.sign_bundle) and callable(packager.zip_bundle)
    # V14(A235,A224 遗留清理):私有别名从模块命名空间消失
    assert not hasattr(packager, "_sign_bundle")
    assert not hasattr(packager, "_zip_bundle")


def test_a224_public_zip_bundle_direct_call(tmp_path: Path) -> None:
    """公开 zip_bundle 直调:对既有证据目录压 zip,内含顶层目录名与全部文件。"""
    cfg = make_cfg(tmp_path)
    bundle = build_bundle(make_report(tmp_path), cfg)
    bundle_dir = Path(bundle.dir_path)

    zip2 = tmp_path / "direct.zip"
    packager.zip_bundle(bundle_dir, zip2)
    with zipfile.ZipFile(zip2) as zf:
        names = set(zf.namelist())
    prefix = bundle_dir.name
    assert f"{prefix}/manifest.json" in names
    assert f"{prefix}/summary.md" in names
    assert sum(1 for n in names if n.endswith(".png")) == 6  # 6 份证据全在


def test_a224_public_sign_bundle_direct_call(tmp_path: Path) -> None:
    """公开 sign_bundle 直调:默认配置 no-op(manifest 字节不变);
    ed25519 配置下落签名块且独立验签通过(与 build_bundle 内同一实现)。"""
    telemetry.reset()
    try:
        cfg = make_cfg(tmp_path)
        bundle = build_bundle(make_report(tmp_path), cfg)
        manifest_path = Path(bundle.manifest_path)
        before = manifest_path.read_bytes()

        # 默认配置:未显式请求签名 → 完全 no-op,字节不变
        packager.sign_bundle(Path(bundle.dir_path), manifest_path, cfg)
        assert manifest_path.read_bytes() == before

        # ed25519 配置:直调公开口落签名块(merge/parallel 复用的同一实现)
        sign_cfg = make_cfg(tmp_path / "sign")
        sign_cfg.bundle_sign_algo = "ed25519"
        sign_cfg.ed25519_seed_hex = SEED_A.hex()
        packager.sign_bundle(Path(bundle.dir_path), manifest_path, sign_cfg)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["signature"]["algo"] == "ed25519"
        assert manifest["signature"]["files_hashed"] == 6
        ok, msg = BundleSigner(
            data_dir=str(tmp_path / "verify"), algo="ed25519", ed25519_seed=SEED_B
        ).verify(bundle.dir_path)
        assert ok is True and msg.startswith("校验通过:6 个文件")
    finally:
        telemetry.reset()
