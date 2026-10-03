"""A109 netsentinel.evidence.merge_bundles 多站点证据包合并测试。

离线、只在 tmp_path 下读写:手工构造若干"单站证据包"鸭子对象
(目录 + 证据文件 + manifest.json + zip,风格对齐 A11 packager 产物),
覆盖契约 §4 A109 全部行为:

- 主流程:两包合并文件并集、sha256 全局去重(同名同内容 / 异名同内容)、
  重名不同内容加序号、manifest 结构(title/site_urls/sub_reports/
  skipped/files/merged_at)、单包退化;
- 健壮性:坏 manifest(非法 JSON / 非对象 / 文件缺失)跳过计 skipped、
  全坏仍产出空合并包、子包缺 report 键、证据文件缺失计 missing、
  相对路径越界防穿越;
- 展示:标题缺省与自定义、summary 中文关键词、Top10 各子报告 ensemble
  分并集降序(取最高分、非 ensemble 不入表、封顶 10 行)、损坏子包行;
- 产物:zip 可列且 DEFLATED、返回 EvidenceBundle 形状与目录命名、
  site_url " | " 拼接与 200 截断、out_dir 自动创建与同秒目录避让;
- telemetry:timer(merge_bundles.merge)/ counter(merge_bundles.files)。

A214 追加:合并产物签名接线——cfg=None 与默认 hmac-sha256 配置零签名
(现状快照);ed25519 显式配置下合并 manifest 含签名块且独立验签通过
(zip 内即已签名);签名失败回滚不中断;签名器不可导入降级。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, EvidenceBundle
from netsentinel.evidence.merge_bundles import merge_bundles
from netsentinel.security import ed25519
from netsentinel.security.bundle_sign import BundleSigner

SITE_A = "https://a.example.test/"
SITE_B = "https://b.example.test/"
SITE_C = "https://c.example.test/"


# ---------------------------------------------------------------------------
# 辅助工厂(手工伪造 packager 风格的单站证据包)
# ---------------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_report(
    site_url: str,
    ensemble: dict[str, float] | None = None,
    others: dict[str, list[float]] | None = None,
    *,
    verdict: str = "nsfw",
    agg: float = 0.9500,
    nsw: int = 2,
) -> dict:
    """构造子包 manifest 里的 report 摘要(风格对齐 SiteReport.as_dict)。

    ``ensemble``:图片绝对路径 → ensemble 分;``others``:非 ensemble
    模型名 → 分值列表(图片路径自动编造,验证其不进 Top 表)。
    """
    scores: list[dict] = [
        {"image": path, "model": "ensemble", "nsfw_prob": prob, "scores": {}}
        for path, prob in (ensemble or {}).items()
    ]
    for model, probs in (others or {}).items():
        scores.extend(
            {
                "image": f"/src/other/{model}_{i}.png",
                "model": model,
                "nsfw_prob": p,
                "scores": {},
            }
            for i, p in enumerate(probs)
        )
    return {
        "site_url": site_url,
        "pages": [],
        "image_scores": scores,
        "agg_nsw_prob": agg,
        "nsw_image_count": nsw,
        "verdict": verdict,
        "needs_review": True,
        "created_at": "2026-10-01T10:00:00+08:00",
    }


def make_sub_bundle(
    root: Path,
    name: str,
    site_url: str,
    files: dict[str, bytes],
    *,
    report: dict | None = None,
    manifest_text: str | None = None,
    drop_files: set[str] | None = None,
) -> EvidenceBundle:
    """手工构造一个"单站证据包"鸭子:目录 + 证据文件 + manifest.json + zip。

    - ``manifest_text`` 覆写 manifest 原文(构造损坏用例);
    - ``drop_files``:清单照登、盘上不写(构造"清单有而文件缺失"用例)。
    """
    bdir = root / name
    bdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for fname, data in files.items():
        if not (drop_files and fname in drop_files):
            (bdir / fname).write_bytes(data)
        entries.append(
            {"path": fname, "sha256": _sha(data), "bytes": len(data), "role": "image"}
        )
    manifest = {
        "report": report if report is not None else make_report(site_url),
        "files": entries,
    }
    (bdir / "manifest.json").write_text(
        manifest_text
        if manifest_text is not None
        else json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    zip_path = root / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(bdir.iterdir()):
            if item.is_file():
                zf.write(item, f"{name}/{item.name}")
    return EvidenceBundle(
        site_url=site_url,
        dir_path=str(bdir),
        manifest_path=str(bdir / "manifest.json"),
        zip_path=str(zip_path),
    )


def read_manifest(bundle: EvidenceBundle) -> dict:
    return json.loads(Path(bundle.manifest_path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 空列表 → ValueError 中文
# ---------------------------------------------------------------------------
def test_empty_bundles_value_error(tmp_path: Path) -> None:
    """空列表 → ValueError,中文消息含"不能为空"。"""
    with pytest.raises(ValueError, match="不能为空"):
        merge_bundles([], tmp_path / "out")


# ---------------------------------------------------------------------------
# 两包合并:文件并集 + sha256 全局去重
# ---------------------------------------------------------------------------
def test_two_bundles_union_and_sha_dedup(tmp_path: Path) -> None:
    """A(a.png+shared.png)与 B(b.png+同内容 shared.png)→ 3 份文件并集。

    同内容 shared 只保留首份(归属 A);顺序 = 输入子包顺序 × 子包清单顺序;
    每份文件的 sha256 / bytes / from_site 与盘上事实一致。
    """
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "bundle_a", SITE_A,
        {"a.png": b"CONTENT-A" * 4, "shared.png": b"SHARED-CONTENT" * 4},
    )
    b = make_sub_bundle(
        src, "bundle_b", SITE_B,
        {"b.png": b"CONTENT-B" * 4, "shared.png": b"SHARED-CONTENT" * 4},
    )
    merged = merge_bundles([a, b], tmp_path / "out")

    manifest = read_manifest(merged)
    files = manifest["files"]
    assert [e["path"] for e in files] == ["a.png", "shared.png", "b.png"]
    assert len({e["sha256"] for e in files}) == 3  # 全局 sha 唯一

    bdir = Path(merged.dir_path)
    for entry in files:
        data = (bdir / entry["path"]).read_bytes()
        assert entry["sha256"] == _sha(data)
        assert entry["bytes"] == len(data)
    assert (bdir / "shared.png").read_bytes() == b"SHARED-CONTENT" * 4

    by_name = {e["path"]: e for e in files}
    assert by_name["shared.png"]["from_site"] == SITE_A  # 重复内容归属首站
    assert by_name["a.png"]["from_site"] == SITE_A
    assert by_name["b.png"]["from_site"] == SITE_B


def test_same_content_different_names_deduped(tmp_path: Path) -> None:
    """文件名不同但内容相同(x.png / y.png 同字节)→ 只保留首份 x.png。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "ba", SITE_A, {"x.png": b"SAME-BYTES"})
    b = make_sub_bundle(
        src, "bb", SITE_B, {"y.png": b"SAME-BYTES", "z.png": b"DIFFERENT"}
    )
    merged = merge_bundles([a, b], tmp_path / "out")

    manifest = read_manifest(merged)
    assert [e["path"] for e in manifest["files"]] == ["x.png", "z.png"]
    bdir = Path(merged.dir_path)
    assert (bdir / "x.png").read_bytes() == b"SAME-BYTES"
    assert not (bdir / "y.png").exists()  # 重复副本已清理


# ---------------------------------------------------------------------------
# manifest 结构
# ---------------------------------------------------------------------------
def test_manifest_structure_full(tmp_path: Path) -> None:
    """manifest.json 六键齐全;sub_reports 与输入顺序逐一对齐。"""
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "sa", SITE_A, {"a.png": b"A"},
        report=make_report(SITE_A, {"/src/a/img.png": 0.9801}),
    )
    b = make_sub_bundle(
        src, "sb", SITE_B, {"b.png": b"B"},
        report=make_report(SITE_B, verdict="suspect", agg=0.4321, nsw=0),
    )
    merged = merge_bundles([a, b], tmp_path / "out")

    manifest = read_manifest(merged)
    assert set(manifest) == {
        "title", "site_urls", "sub_reports", "skipped", "files", "merged_at",
    }
    assert manifest["site_urls"] == [SITE_A, SITE_B]
    assert manifest["skipped"] == 0
    assert len(manifest["sub_reports"]) == 2  # 与输入对齐
    assert manifest["sub_reports"][0]["site_url"] == SITE_A
    assert manifest["sub_reports"][0]["image_scores"][0]["nsfw_prob"] == 0.9801
    assert manifest["sub_reports"][1]["verdict"] == "suspect"
    assert len(manifest["files"]) == 2
    for entry in manifest["files"]:
        assert set(entry) == {"path", "sha256", "bytes", "from_site"}
    datetime.fromisoformat(manifest["merged_at"])  # ISO 时间戳可解析


# ---------------------------------------------------------------------------
# 坏 manifest:跳过 + 计 skipped
# ---------------------------------------------------------------------------
def test_bad_manifest_variants_skipped(tmp_path: Path) -> None:
    """非法 JSON / 非 JSON 对象 / manifest 文件缺失 三种损坏都计入 skipped。"""
    src = tmp_path / "src"
    bad_json = make_sub_bundle(
        src, "bad_json", SITE_A, {"x.png": b"X"}, manifest_text="{不是合法JSON"
    )
    bad_array = make_sub_bundle(
        src, "bad_array", SITE_C, {"y.png": b"Y"}, manifest_text="[1, 2, 3]"
    )
    gone = make_sub_bundle(src, "gone", "https://gone.example.test/", {"z.png": b"Z"})
    Path(gone.manifest_path).unlink()  # manifest 文件整个缺失
    good = make_sub_bundle(src, "good", SITE_B, {"g.png": b"G"})

    merged = merge_bundles([bad_json, good, bad_array, gone], tmp_path / "out")
    manifest = read_manifest(merged)

    assert manifest["skipped"] == 3
    assert manifest["site_urls"] == [SITE_B]  # 坏包站点不并入
    assert manifest["sub_reports"] == [
        None, manifest["sub_reports"][1], None, None,
    ]
    assert manifest["sub_reports"][1]["site_url"] == SITE_B
    assert [e["path"] for e in manifest["files"]] == ["g.png"]  # 坏包文件不并入


def test_manifestless_bundle_skipped(tmp_path: Path) -> None:
    """manifest_path 与 dir_path 均空的鸭子 → 无法定位 manifest,计 skipped。"""
    empty_duck = EvidenceBundle(
        site_url="https://nowhere.example.test/", dir_path="", manifest_path=""
    )
    a = make_sub_bundle(tmp_path / "src", "sa", SITE_A, {"a.png": b"A"})
    merged = merge_bundles([empty_duck, a], tmp_path / "out")
    manifest = read_manifest(merged)
    assert manifest["skipped"] == 1
    assert manifest["site_urls"] == [SITE_A]
    assert manifest["sub_reports"][0] is None


def test_all_bad_manifests_still_produces_bundle(tmp_path: Path) -> None:
    """全部子包损坏 → 0 站点空合并包,manifest/summary/zip 照常产出。"""
    src = tmp_path / "src"
    bad1 = make_sub_bundle(src, "b1", SITE_A, {}, manifest_text="not-json")
    bad2 = make_sub_bundle(src, "b2", SITE_B, {}, manifest_text='"字符串"')
    merged = merge_bundles([bad1, bad2], tmp_path / "out")

    manifest = read_manifest(merged)
    assert manifest["skipped"] == 2
    assert manifest["site_urls"] == []
    assert manifest["sub_reports"] == [None, None]
    assert manifest["files"] == []
    assert manifest["title"] == "合并证据包(0 个站点)"
    assert merged.site_url == ""
    assert merged.zip_path  # zip 始终产出

    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")
    assert "本包由 0 个站点证据合并生成,提交前须经人工核实" in summary
    assert "(无:全部子包 manifest 损坏被跳过)" in summary

    with zipfile.ZipFile(merged.zip_path) as zf:
        assert len(zf.namelist()) == 2  # 仅 manifest.json + summary.md


def test_sub_manifest_without_report_key(tmp_path: Path) -> None:
    """manifest 合法但无 report 键 → 站点与文件照常并入,sub_reports 记 None。"""
    src = tmp_path / "src"
    bundle = make_sub_bundle(src, "noreport", SITE_A, {"f.png": b"F"})
    raw = read_manifest(bundle)
    del raw["report"]
    Path(bundle.manifest_path).write_text(json.dumps(raw), encoding="utf-8")

    merged = merge_bundles([bundle], tmp_path / "out")
    manifest = read_manifest(merged)
    assert manifest["skipped"] == 0  # manifest 本身有效,不算损坏
    assert manifest["site_urls"] == [SITE_A]
    assert manifest["sub_reports"] == [None]
    assert [e["path"] for e in manifest["files"]] == ["f.png"]


# ---------------------------------------------------------------------------
# 标题:缺省与自定义
# ---------------------------------------------------------------------------
def test_default_title_counts_merged_sites(tmp_path: Path) -> None:
    """缺省标题 = "合并证据包({n} 个站点)",n 只数成功并入的站点。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "ta", SITE_A, {"a.png": b"A"})
    b = make_sub_bundle(src, "tb", SITE_B, {"b.png": b"B"})
    merged = merge_bundles([a, b], tmp_path / "out")
    assert read_manifest(merged)["title"] == "合并证据包(2 个站点)"


def test_custom_title_overrides(tmp_path: Path) -> None:
    """自定义 title 进 manifest 与 summary;缺省模板不再出现。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "ca", SITE_A, {"a.png": b"A"})
    b = make_sub_bundle(src, "cb", SITE_B, {"b.png": b"B"})
    merged = merge_bundles([a, b], tmp_path / "out", title="某专案组七月合并包")

    manifest = read_manifest(merged)
    assert manifest["title"] == "某专案组七月合并包"
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")
    assert "某专案组七月合并包" in summary
    assert "合并证据包(2 个站点)" not in summary
    # 声明仍按站点数渲染
    assert "本包由 2 个站点证据合并生成,提交前须经人工核实" in summary


# ---------------------------------------------------------------------------
# summary:关键词与 Top10
# ---------------------------------------------------------------------------
def test_summary_keywords(tmp_path: Path) -> None:
    """summary.md 中文关键词:标题/站点列表/子报告表/声明/判定中文映射。"""
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "ka", SITE_A, {"a.png": b"A"},
        report=make_report(SITE_A, {"/src/a/img.png": 0.9801},
                           verdict="nsfw", agg=0.9500, nsw=2),
    )
    b = make_sub_bundle(
        src, "kb", SITE_B, {"b.png": b"B"},
        report=make_report(SITE_B, verdict="suspect", agg=0.4321, nsw=0),
    )
    merged = merge_bundles([a, b], tmp_path / "out")
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")

    assert "# 合并证据包摘要(净网哨兵 NetSentinel)" in summary
    assert "- 标题:合并证据包(2 个站点)" in summary
    assert "- 涉及站点数:2" in summary
    assert f"  - {SITE_A}" in summary and f"  - {SITE_B}" in summary
    assert "## 子报告一览" in summary
    assert "高置信(nsfw)" in summary and "疑似(suspect)" in summary
    assert "0.9500" in summary and "0.4321" in summary
    assert "## Top 10 图片分值表" in summary
    assert "本包由 2 个站点证据合并生成,提交前须经人工核实" in summary


def test_summary_top10_ensemble_union_desc(tmp_path: Path) -> None:
    """Top10 = 各子报告 ensemble 分并集降序;同图取最高分;非 ensemble 不入表。"""
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "ua", SITE_A, {},
        report=make_report(
            SITE_A,
            ensemble={
                "/src/a/img_b.png": 0.9801,
                "/src/a/img_a.png": 0.6400,
                "/src/shared/img_s.png": 0.5234,
            },
            others={"stub": [0.9999]},
            agg=0.9500,
        ),
    )
    b = make_sub_bundle(
        src, "ub", SITE_B, {},
        report=make_report(
            SITE_B,
            ensemble={
                "/src/b/img_c.png": 0.9123,
                "/src/shared/img_s.png": 0.7777,
            },
            others={"clip": [0.8888]},
            verdict="suspect",
            agg=0.4321,
        ),
    )
    merged = merge_bundles([a, b], tmp_path / "out")
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")

    # 并集降序:0.9801 → 0.9123 → 0.7777(img_s 取两报告最高分)→ 0.6400
    assert (
        summary.index("0.9801")
        < summary.index("0.9123")
        < summary.index("0.7777")
        < summary.index("0.6400")
    )
    assert summary.count("img_s.png") == 1  # 同图并集去重
    assert "0.5234" not in summary  # 低分副本被并集吞掉
    assert "0.9999" not in summary and "0.8888" not in summary  # 非 ensemble
    assert "img_b.png" in summary and "img_c.png" in summary


def test_summary_top10_capped_at_10(tmp_path: Path) -> None:
    """12 条 ensemble 分 → Top 表恰好 10 行,最低两条被截掉。"""
    ensemble = {f"/src/big/n{i:02d}.png": (i + 1) / 100 for i in range(12)}
    bundle = make_sub_bundle(
        tmp_path / "src", "cap", SITE_A, {},
        report=make_report(SITE_A, ensemble=ensemble, agg=0.9500),
    )
    merged = merge_bundles([bundle], tmp_path / "out")
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")

    rows = [line for line in summary.splitlines() if line.startswith("| n")]
    assert len(rows) == 10
    assert rows[0].startswith("| n11.png |")  # 最高分 0.1200 居首
    assert rows[0].split("|")[2].strip() == "0.1200"
    assert "n10.png" in summary and "n02.png" in summary  # 恰好第 10 名保留
    assert "n01.png" not in summary and "n00.png" not in summary  # 截断


def test_summary_marks_skipped_row(tmp_path: Path) -> None:
    """损坏子包在子报告表占一行(展示其站点 URL 与损坏说明)。"""
    src = tmp_path / "src"
    bad = make_sub_bundle(
        src, "sb_row", SITE_A, {"x.png": b"X"}, manifest_text="broken"
    )
    good = make_sub_bundle(src, "sg_row", SITE_B, {"g.png": b"G"})
    merged = merge_bundles([bad, good], tmp_path / "out")
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")
    assert "manifest 损坏" in summary
    assert SITE_A in summary  # 坏包站点仍在表中留痕
    assert "跳过子包数(manifest 损坏):1" in summary


def test_summary_no_scores_placeholder(tmp_path: Path) -> None:
    """子报告无任何 ensemble 分 → Top 表显示"(无图片评分记录)"。"""
    bundle = make_sub_bundle(
        tmp_path / "src", "nos", SITE_A, {"a.png": b"A"},
        report=make_report(SITE_A, others={"stub": [0.5]}),
    )
    merged = merge_bundles([bundle], tmp_path / "out")
    summary = (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")
    assert "|(无图片评分记录)| - | - |" in summary


# ---------------------------------------------------------------------------
# zip:可列、DEFLATED、完整
# ---------------------------------------------------------------------------
def test_zip_listable_deflated(tmp_path: Path) -> None:
    """zip 存在、DEFLATED、可列出 manifest/summary/全部证据且校验无损。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "za", SITE_A, {"a.png": b"AAA"})
    b = make_sub_bundle(src, "zb", SITE_B, {"b.png": b"BBB"})
    merged = merge_bundles([a, b], tmp_path / "out")

    zip_path = Path(merged.zip_path)
    assert zip_path.is_file()
    prefix = Path(merged.dir_path).name
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None  # CRC 全部无损
        names = zf.namelist()
        assert f"{prefix}/manifest.json" in names
        assert f"{prefix}/summary.md" in names
        for entry in read_manifest(merged)["files"]:
            assert f"{prefix}/{entry['path']}" in names
        assert len(names) == 4  # 2 证据 + manifest + summary
        assert all(
            info.compress_type == zipfile.ZIP_DEFLATED for info in zf.infolist()
        )


# ---------------------------------------------------------------------------
# 重名不同内容:加序号,内容全保留
# ---------------------------------------------------------------------------
def test_duplicate_names_get_sequence_suffix(tmp_path: Path) -> None:
    """三个子包各带同名 dup.png(内容互不相同)→ dup/…_1/…_2,内容全保留。"""
    src = tmp_path / "src"
    contents = [b"ONE", b"TWO", b"THREE"]
    bundles = [
        make_sub_bundle(src, f"d{i}", url, {"dup.png": data})
        for i, (url, data) in enumerate(
            zip([SITE_A, SITE_B, SITE_C], contents)
        )
    ]
    merged = merge_bundles(bundles, tmp_path / "out")

    manifest = read_manifest(merged)
    names = [e["path"] for e in manifest["files"]]
    assert names == ["dup.png", "dup_1.png", "dup_2.png"]

    bdir = Path(merged.dir_path)
    got = {(bdir / n).read_bytes() for n in names}
    assert got == set(contents)  # 三份内容都在,没有任何一份被覆盖


# ---------------------------------------------------------------------------
# 单包退化
# ---------------------------------------------------------------------------
def test_single_bundle_degenerate(tmp_path: Path) -> None:
    """单包合并 = 原样并入:标题数 1、site_url 无拼接符、文件全量。"""
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "only", SITE_A, {"a.png": b"A", "b.png": b"B"},
        report=make_report(SITE_A, {"/src/a/img.png": 0.9801}),
    )
    merged = merge_bundles([a], tmp_path / "out")

    manifest = read_manifest(merged)
    assert manifest["title"] == "合并证据包(1 个站点)"
    assert manifest["site_urls"] == [SITE_A]
    assert manifest["skipped"] == 0
    assert len(manifest["sub_reports"]) == 1
    assert [e["path"] for e in manifest["files"]] == ["a.png", "b.png"]
    assert merged.site_url == SITE_A  # 单站点无 " | " 拼接符
    assert " | " not in merged.site_url
    assert "本包由 1 个站点证据合并生成,提交前须经人工核实" in (
        (Path(merged.dir_path) / "summary.md").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 返回值形状与目录命名
# ---------------------------------------------------------------------------
def test_return_bundle_shape_and_dir_naming(tmp_path: Path) -> None:
    """返回 EvidenceBundle;目录名 merged_<ts>;manifest/zip 路径成对。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "ra", SITE_A, {"a.png": b"A"})
    b = make_sub_bundle(src, "rb", SITE_B, {"b.png": b"B"})
    out = tmp_path / "out"
    merged = merge_bundles([a, b], out)

    assert isinstance(merged, EvidenceBundle)
    bdir = Path(merged.dir_path)
    assert bdir.is_dir()
    assert bdir.parent == out
    assert re.fullmatch(r"merged_\d{8}_\d{6}", bdir.name)
    assert merged.manifest_path == str(bdir / "manifest.json")
    assert (bdir / "summary.md").is_file()
    assert Path(merged.zip_path) == bdir.parent / f"{bdir.name}.zip"
    assert Path(merged.zip_path).is_file()


def test_site_url_join_and_truncation(tmp_path: Path) -> None:
    """site_url = " | ".join(site_urls) 截断 200;短列表不截断。"""
    src = tmp_path / "src"
    long_urls = [f"https://s{i}.example.test/" + "x" * 50 for i in range(5)]
    bundles = [
        make_sub_bundle(src, f"l{i}", u, {}) for i, u in enumerate(long_urls)
    ]
    merged = merge_bundles(bundles, tmp_path / "out")
    joined = " | ".join(long_urls)
    assert len(joined) > 200  # 前提:确实超长
    assert merged.site_url == joined[:200]
    assert len(merged.site_url) == 200

    a = make_sub_bundle(src, "ja", SITE_A, {})
    b = make_sub_bundle(src, "jb", SITE_B, {})
    merged2 = merge_bundles([a, b], tmp_path / "out2")
    assert merged2.site_url == f"{SITE_A} | {SITE_B}"


def test_out_dir_auto_created_and_unique_dirs(tmp_path: Path) -> None:
    """out_dir 多级缺失自动创建;同秒重复合并目录避让,互不覆盖。"""
    src = tmp_path / "src"
    a = make_sub_bundle(src, "ua", SITE_A, {"a.png": b"A"})
    out = tmp_path / "deep" / "nested" / "out"

    first = merge_bundles([a], out)
    second = merge_bundles([a], out)  # 同秒再合并

    assert Path(first.dir_path).is_dir() and Path(second.dir_path).is_dir()
    assert first.dir_path != second.dir_path  # 目录避让
    assert Path(first.zip_path).is_file() and Path(second.zip_path).is_file()
    assert re.fullmatch(
        r"merged_\d{8}_\d{6}(_\d+)?", Path(second.dir_path).name
    )
    # 两份合并包各自完整,第二份不混入第一份的文件
    assert [e["path"] for e in read_manifest(second)["files"]] == ["a.png"]


# ---------------------------------------------------------------------------
# 健壮性:证据文件缺失 / 路径越界
# ---------------------------------------------------------------------------
def test_missing_evidence_file_skips_and_counts(tmp_path: Path) -> None:
    """清单有而盘上无的文件 → warning 跳过,计 merge_bundles.missing。"""
    telemetry.reset()
    bundle = make_sub_bundle(
        tmp_path / "src", "hole", SITE_A,
        {"here.png": b"HERE", "gone.png": b"GONE"},
        drop_files={"gone.png"},
    )
    merged = merge_bundles([bundle], tmp_path / "out")

    manifest = read_manifest(merged)
    assert [e["path"] for e in manifest["files"]] == ["here.png"]
    snap = telemetry.snapshot()
    assert snap["counters"]["merge_bundles.missing"] == 1
    assert snap["counters"]["merge_bundles.files"] == 1
    assert manifest["skipped"] == 0  # 文件缺失不算子包损坏


def test_path_escape_skipped(tmp_path: Path) -> None:
    """清单里的 ../ 相对路径越出子包目录 → 拒绝并入(防穿越)。"""
    src = tmp_path / "src"
    bundle = make_sub_bundle(src, "esc", SITE_A, {"ok.png": b"OK"})
    (src / "secret.txt").write_bytes(b"SECRET-OUTSIDE")  # 子包目录外的文件
    raw = read_manifest(bundle)
    raw["files"].append(
        {
            "path": "../secret.txt",
            "sha256": _sha(b"SECRET-OUTSIDE"),
            "bytes": 15,
            "role": "image",
        }
    )
    Path(bundle.manifest_path).write_text(json.dumps(raw), encoding="utf-8")

    merged = merge_bundles([bundle], tmp_path / "out")
    manifest = read_manifest(merged)
    assert [e["path"] for e in manifest["files"]] == ["ok.png"]
    assert not (Path(merged.dir_path) / "secret.txt").exists()


# ---------------------------------------------------------------------------
# telemetry:timer(merge_bundles.merge)/ counter(merge_bundles.files)
# ---------------------------------------------------------------------------
def test_telemetry_merge_timer_and_files_counter(tmp_path: Path) -> None:
    """timer 记一次合并耗时;files 计去重后的证据文件数;无异常时不计 skipped。"""
    telemetry.reset()
    src = tmp_path / "src"
    a = make_sub_bundle(
        src, "ma", SITE_A, {"a.png": b"A", "shared.png": b"S"},
    )
    b = make_sub_bundle(
        src, "mb", SITE_B, {"b.png": b"B", "shared.png": b"S"},  # shared 重复
    )
    merged = merge_bundles([a, b], tmp_path / "out")
    assert merged.zip_path

    snap = telemetry.snapshot()
    assert snap["timers"]["merge_bundles.merge"]["count"] == 1
    assert snap["counters"]["merge_bundles.files"] == 3  # 去重后 3 份
    assert "merge_bundles.skipped" not in snap["counters"]  # 无损坏不计数
    assert "merge_bundles.missing" not in snap["counters"]  # 无缺失不计数


# ---------------------------------------------------------------------------
# A214:合并产物签名(复用 A205 packager.sign_bundle 公开口,单一实现)
# ---------------------------------------------------------------------------
#: A214 确定性测试种子(与 test_packager.SEED_A/SEED_B 同源,便于复算公钥)
SEED_A = bytes.fromhex("11" * 32)
SEED_B = bytes.fromhex("22" * 32)

#: 合并包 manifest 的既有六键(签名块只增不改;回滚断言用)
_SIX_KEYS = {"title", "site_urls", "sub_reports", "skipped", "files", "merged_at"}


@pytest.fixture(autouse=True)
def _scrub_sign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """签名用例环境隔离:清掉可能泄漏的签名密钥环境变量(确定性)。"""
    monkeypatch.delenv("NETSENTINEL_SIGN_KEY", raising=False)
    monkeypatch.delenv("NETSENTINEL_ED25519_SEED_HEX", raising=False)


def _sign_cfg(tmp_path: Path, *, algo: str = "ed25519") -> Config:
    """签名用例配置:evidence/data 均在 tmp_path(密钥/计数器隔离)。"""
    cfg = Config(evidence_dir=str(tmp_path / "e"), data_dir=str(tmp_path / "d"))
    cfg.bundle_sign_algo = algo
    cfg.ed25519_seed_hex = SEED_A.hex()
    return cfg


def _two_sub_bundles(tmp_path: Path) -> list[EvidenceBundle]:
    src = tmp_path / "src"
    return [
        make_sub_bundle(
            src, "sg_a", SITE_A, {"a.png": b"CONTENT-A"},
            report=make_report(SITE_A, {"/src/a/img.png": 0.9801}),
        ),
        make_sub_bundle(src, "sg_b", SITE_B, {"b.png": b"CONTENT-B"}),
    ]


def test_a214_default_and_none_cfg_zero_signature(tmp_path: Path) -> None:
    """兼容锁定:cfg=None(缺省)与默认 hmac-sha256 配置都零签名——manifest
    六键快照不变、无 signature 字段、零 bundle.sign.* / sign_skipped 计数。"""
    telemetry.reset()
    try:
        bundles = _two_sub_bundles(tmp_path)
        # ① cfg=None(向后兼容:既有调用零变化)
        merged_none = merge_bundles(bundles, tmp_path / "out-none")
        assert set(read_manifest(merged_none)) == _SIX_KEYS

        # ② cfg 传入但为默认 hmac-sha256 配置 = 现状零签名行为
        cfg = Config(evidence_dir=str(tmp_path / "e"), data_dir=str(tmp_path / "d"))
        assert cfg.bundle_sign_algo == "hmac-sha256"  # V12 一等字段缺省
        merged_default = merge_bundles(bundles, tmp_path / "out-default", cfg=cfg)
        manifest = read_manifest(merged_default)
        assert set(manifest) == _SIX_KEYS  # 快照:零新字段、零签名块
        assert [e["path"] for e in manifest["files"]] == ["a.png", "b.png"]

        ok, msg = BundleSigner(data_dir=str(tmp_path / "v")).verify(
            merged_default.dir_path
        )
        assert ok is False and msg == "未签名"

        snap = telemetry.snapshot()["counters"]
        assert not [k for k in snap if k.startswith("bundle.sign.")]  # 零签名计数
        assert not [k for k in snap if k.startswith("merge_bundles.sign")]
    finally:
        telemetry.reset()


def test_a214_ed25519_cfg_signs_merged_manifest_and_verifies(
    tmp_path: Path,
) -> None:
    """显式 ed25519 + seed:合并 manifest 含签名块(algo/public_key/时间证明),
    独立验签通过(验证方无需 seed);zip 内即已签名 manifest;子包输入原样。"""
    telemetry.reset()
    try:
        bundles = _two_sub_bundles(tmp_path)
        sub_manifest_before = [
            Path(b.manifest_path).read_bytes() for b in bundles
        ]
        cfg = _sign_cfg(tmp_path)

        merged = merge_bundles(bundles, tmp_path / "out", cfg=cfg)

        manifest = read_manifest(merged)
        sig = manifest["signature"]
        assert set(manifest) == _SIX_KEYS | {"signature"}  # 只增签名一键
        assert sig["algo"] == "ed25519"
        assert sig["public_key"] == ed25519.public_key(SEED_A).hex()
        assert re.fullmatch(r"[0-9a-f]{64}", sig["public_key"])
        assert re.fullmatch(r"[0-9a-f]{128}", sig["value"])
        assert sig["files_hashed"] == 2  # a.png + b.png 全部入签
        assert sig["timestamp_proof"]["source"] == "local"  # 未配 tsa_url → 离线
        assert manifest["title"] == "合并证据包(2 个站点)"  # 本体内容不受影响

        # 子包是只读输入:manifest 字节原样
        for bundle, before in zip(bundles, sub_manifest_before):
            assert Path(bundle.manifest_path).read_bytes() == before

        # 独立验签:验证方 seed 不同也通过(公钥自足,无需共享密钥)
        ok, msg = BundleSigner(
            data_dir=str(tmp_path / "v"), algo="ed25519", ed25519_seed=SEED_B
        ).verify(merged.dir_path)
        assert ok is True and msg.startswith("校验通过:2 个文件")

        # zip 内即已签名 manifest(签名发生在压缩之前,目录与 zip 口径一致)
        with zipfile.ZipFile(merged.zip_path) as zf:
            prefix = Path(merged.dir_path).name
            zipped = json.loads(zf.read(f"{prefix}/manifest.json").decode("utf-8"))
        assert zipped["signature"] == sig

        snap = telemetry.snapshot()["counters"]
        assert snap["bundle.sign.ed25519"] == 1
        assert "bundle.sign.ed25519/hmac_fallback" not in snap
        assert "bundle.sign.skipped" not in snap
        assert "merge_bundles.sign_skipped" not in snap
    finally:
        telemetry.reset()


def test_a214_sign_failure_rolls_back_and_never_breaks_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """签名中途失败:manifest 回滚为未签名版本,合并照常完成(绝不中断);
    zip 内同样未签名;telemetry 记 bundle.sign.skipped。"""
    monkeypatch.setattr(
        BundleSigner,
        "sign_manifest",
        lambda self, bundle_dir: (_ for _ in ()).throw(RuntimeError("模拟签名失败")),
    )
    telemetry.reset()
    try:
        cfg = _sign_cfg(tmp_path)
        merged = merge_bundles(_two_sub_bundles(tmp_path), tmp_path / "out", cfg=cfg)

        manifest = read_manifest(merged)
        assert set(manifest) == _SIX_KEYS  # 回滚干净:无残留签名块
        assert [e["path"] for e in manifest["files"]] == ["a.png", "b.png"]
        assert Path(merged.zip_path).is_file()
        with zipfile.ZipFile(merged.zip_path) as zf:
            prefix = Path(merged.dir_path).name
            zipped = json.loads(zf.read(f"{prefix}/manifest.json").decode("utf-8"))
        assert "signature" not in zipped  # zip 内同样为回滚后的未签名版本
        assert telemetry.snapshot()["counters"]["bundle.sign.skipped"] == 1
        assert "merge_bundles.sign_skipped" not in (
            telemetry.snapshot()["counters"]
        )
    finally:
        telemetry.reset()


def test_a214_signer_unavailable_degrades_without_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """签名器不可导入(极端并行态):中文告警 + merge_bundles.sign_skipped,
    合并照常完成且产物未签名(增强项缺席绝不中断)。"""
    caplog.set_level(logging.WARNING, logger="netsentinel.evidence.merge_bundles")
    monkeypatch.setitem(sys.modules, "netsentinel.evidence.packager", None)
    telemetry.reset()
    try:
        cfg = _sign_cfg(tmp_path)
        merged = merge_bundles(_two_sub_bundles(tmp_path), tmp_path / "out", cfg=cfg)

        assert set(read_manifest(merged)) == _SIX_KEYS  # 未签名,合并完整
        assert [e["path"] for e in read_manifest(merged)["files"]] == [
            "a.png", "b.png",
        ]
        assert Path(merged.zip_path).is_file()
        snap = telemetry.snapshot()["counters"]
        assert snap["merge_bundles.sign_skipped"] == 1
        assert any("签名器" in r.getMessage() for r in caplog.records)
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# A224:合并签名步消费 packager 公开口(签名逻辑单源,公开化不复制)
# ---------------------------------------------------------------------------
def test_a224_merge_sign_consumes_public_sign_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatch packager.sign_bundle 为记录替身(仍透传真实现):
    合并签名步经**公开名**消费且恰一次(V14/A235 起私有别名已移除,
    替身不被任何别名旁路)。"""
    from netsentinel.evidence import packager as pkg

    calls: list[tuple[str, str]] = []
    real = pkg.sign_bundle

    def _spy(bundle_dir, manifest_path, cfg):
        calls.append((str(bundle_dir), str(manifest_path)))
        return real(bundle_dir, manifest_path, cfg)

    monkeypatch.setattr(pkg, "sign_bundle", _spy)
    telemetry.reset()
    try:
        cfg = _sign_cfg(tmp_path)
        merged = merge_bundles(_two_sub_bundles(tmp_path), tmp_path / "out", cfg=cfg)

        assert calls == [(merged.dir_path, merged.manifest_path)]  # 公开口恰一次
        assert read_manifest(merged)["signature"]["algo"] == "ed25519"  # 真实现透传
        assert not hasattr(pkg, "_sign_bundle")  # V14(A235):别名已移除,替身无旁路
    finally:
        telemetry.reset()


def test_v14_packager_private_aliases_removed() -> None:
    """依赖面断言:packager 公开签名/压缩口在席且可调用;V14(A235)收口后
    一代兼容别名 _sign_bundle / _zip_bundle 已从命名空间移除(零私有名引用)。"""
    from netsentinel.evidence import packager as pkg

    assert callable(pkg.sign_bundle) and callable(pkg.zip_bundle)
    assert "sign_bundle" in pkg.__all__ and "zip_bundle" in pkg.__all__
    assert not hasattr(pkg, "_sign_bundle")
    assert not hasattr(pkg, "_zip_bundle")
