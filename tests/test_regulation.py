"""A51 netsentinel.intel.regulation 本地法规检索(RAG)测试。

纯离线、零网络、零真实 VLM 调用;语料来自仓库 docs/regulations/。
覆盖:
- 索引加载:块数 ≥ 语料文件数、九大类节齐全、块的四个字段形状;
- 无二级标题文件 → 整文件一块;语料目录不存在 → 中文 ValueError;
- search:空 query / 纯空白 → [];top_k 生效;无关词无命中;
  "色情 举报" 首命中 12377 语料的"色情类"节,"低俗 举报" 首命中"低俗类"节;
- suggest_category:三档默认(agg≥nsfw 线→色情类,≥复核线→低俗类,否则其他类)、
  强色情文本情报检索覆盖 → 色情类、赌博情报覆盖 → 赌博类、自定义 cfg 注入;
  返回 {category, basis, snippet} 且 basis 以"检索依据"开头、snippet ≤80 字;
- legal_basis_for:九大类全部非空、色情类含未成年人保护法、全部含
  虚假举报法律责任提示、未知类别仍返回责任提示。
"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel.contracts import Config, SiteReport, Verdict
from netsentinel.intel.regulation import (
    CATEGORIES,
    CATEGORY_FILE,
    LEGAL_FILE,
    RegulationIndex,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "docs" / "regulations"


# ---------------------------------------------------------------------------
# 造数工具
# ---------------------------------------------------------------------------


def make_report(
    agg: float,
    intel: dict | None = None,
    *,
    needs_review: bool = True,
    nsw_image_count: int = 0,
) -> SiteReport:
    """构造最小判定报告;verdict 与 needs_review 保持一致口径。"""
    if needs_review:
        verdict = Verdict.NSFW if agg >= 0.90 else Verdict.SUSPECT
    else:
        verdict = Verdict.CLEAN
    return SiteReport(
        site_url="https://example.test",
        agg_nsw_prob=agg,
        nsw_image_count=nsw_image_count,
        verdict=verdict,
        needs_review=needs_review,
        intel=intel or {},
    )


def porn_intel(porn_hits: int = 6) -> dict:
    """强色情文本情报(结构对齐 netsentinel.intel.text_intel.text_features)。"""
    return {
        "text": {
            "features": {
                "porn_hits": porn_hits,
                "lure_hits": 0,
                "gambling_hits": 0,
                "title_suspect": 1,
            },
            "risk": 0.88,
            "explain": ["命中色情低俗词 6 个(共 12 次)", "页面标题含色情低俗词"],
        },
        "url": {"features": {}, "risk": 0.0, "explain": []},
    }


@pytest.fixture()
def index() -> RegulationIndex:
    return RegulationIndex(str(CORPUS_DIR))


# ---------------------------------------------------------------------------
# 索引与切块
# ---------------------------------------------------------------------------


class TestIndex:
    def test_blocks_cover_all_corpus_files(self, index: RegulationIndex) -> None:
        """索引块数 ≥ 语料 md 文件数(每文件至少一块),且文件全覆盖。"""
        md_files = {p.name for p in CORPUS_DIR.glob("*.md")}
        assert len(md_files) >= 3  # 契约:至少 3 个中文语料文件
        blocks = index.blocks()
        assert len(index) == len(blocks) >= len(md_files)
        assert {b["file"] for b in blocks} == md_files
        for b in blocks:
            assert set(b) == {"file", "title", "snippet", "text"}
            assert b["title"].strip() and b["text"].strip()

    def test_nine_category_sections_present(self, index: RegulationIndex) -> None:
        titles = {
            b["title"] for b in index.blocks() if b["file"] == CATEGORY_FILE
        }
        assert set(CATEGORIES) <= titles

    def test_snippet_capped_at_80_chars(self, index: RegulationIndex) -> None:
        for b in index.blocks():
            assert len(b["snippet"]) <= 80

    def test_file_without_headings_is_single_block(
        self, tmp_path: pathlib.Path
    ) -> None:
        """无二级标题的文件 → 整文件一块,标题取一级标题。"""
        d = tmp_path / "corpus"
        d.mkdir()
        (d / "plain.md").write_text(
            "# 单块语料\n\n这是一份没有二级标题的语料,应当整体作为一块被索引。\n",
            encoding="utf-8",
        )
        idx = RegulationIndex(str(d))
        assert len(idx) == 1
        block = idx.blocks()[0]
        assert block["title"] == "单块语料"
        assert "没有二级标题" in block["text"]

    def test_missing_corpus_dir_raises_chinese_valueerror(
        self, tmp_path: pathlib.Path
    ) -> None:
        with pytest.raises(ValueError, match="语料目录"):
            RegulationIndex(str(tmp_path / "nope"))


# ---------------------------------------------------------------------------
# search:2-gram + BM25-lite
# ---------------------------------------------------------------------------


class TestSearch:
    def test_porn_query_first_hit_is_12377_category_section(
        self, index: RegulationIndex
    ) -> None:
        hits = index.search("色情 举报")
        assert hits, "语料检索应至少命中一块"
        assert hits[0]["file"] == CATEGORY_FILE
        assert hits[0]["title"] == "色情类"

    def test_vulgar_query_first_hit(self, index: RegulationIndex) -> None:
        hits = index.search("低俗 举报")
        assert hits[0]["file"] == CATEGORY_FILE
        assert hits[0]["title"] == "低俗类"

    def test_empty_query_returns_empty_list(self, index: RegulationIndex) -> None:
        assert index.search("") == []
        assert index.search("   ") == []

    def test_top_k_respected(self, index: RegulationIndex) -> None:
        assert len(index.search("色情 举报", top_k=1)) == 1
        assert len(index.search("色情 举报", top_k=2)) <= 2

    def test_no_match_returns_empty(self, index: RegulationIndex) -> None:
        assert index.search("量子纠缠旋光仪") == []

    def test_scores_sorted_desc(self, index: RegulationIndex) -> None:
        hits = index.search("色情 图片 举报")
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0 for s in scores)

    def test_result_shape(self, index: RegulationIndex) -> None:
        hits = index.search("儿童色情 举报")
        assert hits
        for h in hits:
            assert {"file", "title", "snippet", "score"} <= set(h)


# ---------------------------------------------------------------------------
# suggest_category:档位默认 + 检索覆盖
# ---------------------------------------------------------------------------


class TestSuggestCategory:
    def test_tier_defaults(self, index: RegulationIndex) -> None:
        cfg = Config()  # nsfw_threshold=0.90, review_threshold=0.50
        assert index.suggest_category(make_report(0.95), cfg=cfg)["category"] == "色情类"
        assert index.suggest_category(make_report(0.60), cfg=cfg)["category"] == "低俗类"
        assert (
            index.suggest_category(make_report(0.10, needs_review=False), cfg=cfg)[
                "category"
            ]
            == "其他类"
        )

    def test_custom_cfg_thresholds(self, index: RegulationIndex) -> None:
        cfg = Config(nsfw_threshold=0.5, review_threshold=0.3)
        assert index.suggest_category(make_report(0.60), cfg=cfg)["category"] == "色情类"
        assert index.suggest_category(make_report(0.35), cfg=cfg)["category"] == "低俗类"

    def test_retrieval_override_porn_over_tier(self, index: RegulationIndex) -> None:
        """聚合分仅到低俗档,但文本情报强色情 → 检索覆盖为色情类。"""
        result = index.suggest_category(make_report(0.60, porn_intel()))
        assert result["category"] == "色情类"
        assert CATEGORY_FILE in result["basis"]

    def test_retrieval_override_gambling(self, index: RegulationIndex) -> None:
        intel = {
            "text": {
                "features": {"porn_hits": 0, "lure_hits": 0, "gambling_hits": 5},
                "risk": 0.5,
                "explain": ["命中博彩混合信号 5 个"],
            }
        }
        result = index.suggest_category(make_report(0.20, intel))
        assert result["category"] == "赌博类"

    def test_result_shape(self, index: RegulationIndex) -> None:
        for agg in (0.95, 0.60, 0.10):
            result = index.suggest_category(make_report(agg))
            assert set(result) == {"category", "basis", "snippet"}
            assert result["category"] in CATEGORIES
            assert result["basis"].startswith("检索依据")
            assert "匹配度" in result["basis"]
            assert len(result["snippet"]) <= 80

    def test_basis_references_12377_file_for_tier_hits(
        self, index: RegulationIndex
    ) -> None:
        assert CATEGORY_FILE in index.suggest_category(make_report(0.95))["basis"]

    def test_defensive_against_malformed_intel(
        self, index: RegulationIndex
    ) -> None:
        """intel 结构异常(非 dict / 非法数值)不崩溃,按缺失处理。"""
        report = SiteReport(
            site_url="https://example.test",
            agg_nsw_prob=0.6,
            intel={"text": "不是dict", "url": {"risk": "高", "explain": "也不是列表"}},
        )
        result = index.suggest_category(report)
        assert result["category"] == "低俗类"

    def test_empty_report_defaults_to_other(
        self, index: RegulationIndex
    ) -> None:
        result = index.suggest_category(SiteReport(site_url="https://example.test"))
        assert result["category"] == "其他类"


# ---------------------------------------------------------------------------
# legal_basis_for
# ---------------------------------------------------------------------------


class TestLegalBasis:
    @pytest.mark.parametrize("category", CATEGORIES)
    def test_nonempty_for_every_category(
        self, index: RegulationIndex, category: str
    ) -> None:
        titles = index.legal_basis_for(category)
        assert titles, f"{category} 的法规依据不应为空"
        assert all(isinstance(t, str) and t for t in titles)

    def test_porn_category_includes_minor_law(
        self, index: RegulationIndex
    ) -> None:
        titles = index.legal_basis_for("色情类")
        assert any("未成年人保护法" in t for t in titles)
        assert any("网络安全法" in t for t in titles)

    @pytest.mark.parametrize("category", CATEGORIES)
    def test_every_category_includes_false_report_warning(
        self, index: RegulationIndex, category: str
    ) -> None:
        assert any("虚假举报" in t for t in index.legal_basis_for(category))

    def test_unknown_category_still_returns_warning(
        self, index: RegulationIndex
    ) -> None:
        titles = index.legal_basis_for("不存在的类别")
        assert titles == ["虚假举报的法律责任提示"]

    def test_titles_come_from_legal_basis_file(
        self, index: RegulationIndex
    ) -> None:
        legal_titles = {
            b["title"] for b in index.blocks() if b["file"] == LEGAL_FILE
        }
        assert set(index.legal_basis_for("侵权类")) <= legal_titles


# ---------------------------------------------------------------------------
# V5 升级锁定:文件级索引缓存 / 倒排检索等价 / 遥测
# ---------------------------------------------------------------------------


class TestV5Upgrades:
    def _make_corpus(self, tmp_path: pathlib.Path) -> pathlib.Path:
        d = tmp_path / "corpus"
        d.mkdir()
        (d / "a.md").write_text(
            "# 甲语料\n\n正文甲一。\n\n## 节甲二\n\n正文甲二。\n",
            encoding="utf-8",
        )
        return d

    def test_v5_file_cache_reuses_unchanged_corpus(
        self, tmp_path: pathlib.Path
    ) -> None:
        """指纹未变时第二次构造复用缓存并记 regulation.cache_hit。"""
        from netsentinel import telemetry

        d = self._make_corpus(tmp_path)
        telemetry.reset()
        try:
            idx1 = RegulationIndex(str(d))
            assert telemetry.snapshot()["counters"].get("regulation.cache_hit") is None
            idx2 = RegulationIndex(str(d))
            assert len(idx2) == len(idx1) == 2  # 文件头块 + "节甲二" 块
            assert telemetry.snapshot()["counters"].get("regulation.cache_hit") >= 1
            # 检索结果跨实例一致(共享缓存不改变行为)
            assert idx1.search("正文甲二") == idx2.search("正文甲二")
        finally:
            telemetry.reset()

    def test_v5_file_cache_invalidates_on_edit(
        self, tmp_path: pathlib.Path
    ) -> None:
        """语料编辑(大小/mtime 变化)→ 缓存失效重建,新内容可见。"""
        d = self._make_corpus(tmp_path)
        idx1 = RegulationIndex(str(d))
        before = idx1.search("正文甲二", top_k=3)
        assert before

        # 追加新节(内容长度变化,指纹必变)
        (d / "a.md").write_text(
            (d / "a.md").read_text(encoding="utf-8")
            + "\n## 节新增\n\n全新增补的内容关键词量子谐振。\n",
            encoding="utf-8",
        )
        idx2 = RegulationIndex(str(d))
        hits = idx2.search("量子谐振", top_k=3)
        assert hits and hits[0]["title"] == "节新增"
        assert len(idx2) == len(idx1) + 1

    def test_v5_search_postings_skip_zero_score_blocks(
        self, index: RegulationIndex
    ) -> None:
        """倒排候选检索与全量逐块打分结果完全一致(含并列得分的次序)。"""
        from collections import Counter

        from netsentinel.intel.regulation import (
            BM25_B,
            BM25_K1,
            _tokenize,
        )

        def full_scan(idx: RegulationIndex, query: str, top_k: int = 3):
            qtf = Counter(_tokenize(query))
            scored = []
            for tf, dl in zip(idx._tfs, idx._doclens):
                len_norm = BM25_B * (dl / idx._avgdl) if idx._avgdl > 0 else 0.0
                denom = BM25_K1 * (1.0 - BM25_B + len_norm)
                s = 0.0
                for term, q in qtf.items():
                    f = tf.get(term, 0)
                    if not f:
                        continue
                    s += q * idx._idf(term) * (f * (BM25_K1 + 1.0) / (f + denom))
                scored.append(s)
            order = sorted(range(len(scored)), key=lambda i: (-scored[i], i))
            out = []
            for i in order:
                if scored[i] <= 0.0:
                    break
                ch = idx._chunks[i]
                out.append({
                    "file": ch["file"], "title": ch["title"],
                    "snippet": ch["snippet"], "score": round(scored[i], 4),
                })
                if len(out) >= top_k:
                    break
            return out

        queries = [
            "色情 举报", "低俗 举报", "儿童色情 举报", "赌博 博彩 举报",
            "网络安全法 举报", "侵权 盗版 举报", "量子纠缠旋光仪",
            "谣言 类别 举报", "诈骗 类别 举报", "色情 图片 举报 需人工复核",
        ]
        for q in queries:
            assert index.search(q, top_k=5) == full_scan(index, q, top_k=5), q

    def test_v5_telemetry_timers(self, tmp_path: pathlib.Path) -> None:
        """构造记 regulation.index_build;search 记 regulation.search。"""
        from netsentinel import telemetry

        d = self._make_corpus(tmp_path)
        telemetry.reset()
        try:
            idx = RegulationIndex(str(d))
            idx.search("正文甲二")
            idx.search("无命中词量子纠缠")
            snap = telemetry.snapshot()
            assert snap["timers"]["regulation.index_build"]["count"] >= 1
            assert snap["timers"]["regulation.search"]["count"] >= 1
        finally:
            telemetry.reset()
