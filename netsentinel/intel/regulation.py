"""本地法规检索(RAG)与举报类别建议(A51,CONTRACTS-V3.md §3)。

为每份站点判定报告建议最匹配的举报类别与法规依据,全部基于本地语料:

- :class:`RegulationIndex`:加载 ``docs/regulations/*.md`` 语料目录,每个文件按
  ``## `` 二级标题切块(无二级标题则整文件一块),中文 2-gram 分词后用
  BM25-lite(k1=1.5,b=0.75,纯 Python 全量计算)打分检索;
- :meth:`RegulationIndex.suggest_category`:依据报告的聚合分(agg)/复核标记与
  intel 中 URL/文本情报的 risk、explain 拼检索查询,命中 12377 分类语料的
  "色情类"/"低俗类"等节则映射对应类别,否则按聚合分档位给默认类别
  (agg≥nsfw_threshold→色情类,≥review_threshold→低俗类,其余→其他类);
- :meth:`RegulationIndex.legal_basis_for`:返回 legal_basis.md 中与类别相关的
  法规条目标题列表(含虚假举报法律责任提示)。

安全红线:
1. 纯本地语料 + 本地检索,零外呼、零联网、零 VLM 调用;
2. 语料只写法规/受理范围的标题与适用要点,不杜撰条文细节、不编造文号,
   不确定之处一律写"以官方发布为准"(语料 md 文件同此约定);
3. 建议结果仅供人工参考,举报类别最终由受理平台与人工确认。

V5 性能 / 可观测性:

- **文件级索引缓存**:语料文件(路径、大小、mtime_ns)指纹未变时,
  跨实例复用已解析的切块与 2-gram 词频(``_FILE_CACHE``),重复构造
  ``RegulationIndex``(如逐报告新建)不再重复读盘 + 分词;
- **检索两处去重复计算**:查询词 idf 每词算一次(旧实现对每个文档×
  每个查询词都重算一次 ``ln``);倒排表(postings)只对含查询词的块
  打分,零分块天然不进结果,输出与全量计算逐字节一致;
- 关键入口记 ``telemetry.timer("regulation.search")`` 与
  ``telemetry.timer("regulation.index_build")``,缓存命中记
  ``telemetry.inc("regulation.cache_hit")``(只存计数,红线 17)。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, SiteReport

__all__ = [
    "CATEGORIES",
    "CATEGORY_FILE",
    "LEGAL_FILE",
    "RegulationIndex",
]

logger = logging.getLogger(__name__)

#: 12377 首页九大类(docs/portal_12377_notes.md A13 调研结论)。
CATEGORIES: tuple[str, ...] = (
    "政治类", "暴恐类", "诈骗类", "色情类", "低俗类",
    "赌博类", "侵权类", "谣言类", "其他类",
)

#: 12377 分类语料文件名(类别映射只认这个文件里的节)。
CATEGORY_FILE: str = "12377_categories.md"

#: 法规依据语料文件名。
LEGAL_FILE: str = "legal_basis.md"

#: BM25-lite 参数(契约规定:k1=1.5,b=0.75;语料规模小,全量纯 Python 计算)。
BM25_K1: float = 1.5
BM25_B: float = 0.75

#: 检索摘要(snippet)与 suggest 输出的截断长度(前 80 字)。
SNIPPET_LEN: int = 80

#: explain 行最多取几条、每条最多多少字符,防止超长查询拖慢检索。
_MAX_EXPLAIN_LINES: int = 3
_MAX_EXPLAIN_CHARS: int = 40

#: URL 情报 risk 达到该值才把"可疑链接"信号词写进查询。
_URL_RISK_SIGNAL: float = 0.5

# 中文连串(CJK)与拉丁/数字连串:前者切 2-gram,后者整词小写。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RUN = re.compile(r"[A-Za-z0-9]+")

# 二级标题行(### 及更深层不属于切块边界,归入当前节正文)。
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
# 一级标题行(用于无二级标题文件/文件头的块标题)。
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")

#: 类别 → 法规条目标题关键词(与 legal_basis.md 的节标题做包含匹配)。
_CATEGORY_LEGAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "色情类": ("网络安全法", "未成年人保护法", "互联网信息服务管理办法", "出版管理条例"),
    "低俗类": ("网络安全法", "互联网信息服务管理办法", "未成年人保护法"),
    "赌博类": ("网络安全法", "互联网信息服务管理办法"),
    "侵权类": ("出版管理条例", "网络安全法", "互联网信息服务管理办法"),
    "谣言类": ("网络安全法", "互联网信息服务管理办法"),
    "政治类": ("网络安全法", "互联网信息服务管理办法"),
    "暴恐类": ("网络安全法", "互联网信息服务管理办法"),
    "诈骗类": ("网络安全法", "互联网信息服务管理办法"),
    "其他类": ("网络安全法", "互联网信息服务管理办法"),
}


def _tokenize(text: str) -> list[str]:
    """中文 2-gram + 拉丁/数字整词小写的分词。

    中文连串滑窗取相邻两字(单字段退为该字本身),拉丁字母/数字连串按整词
    小写作为一个 token。空串返回空列表。
    """
    tokens: list[str] = []
    for run in _CJK_RUN.findall(text or ""):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(w.lower() for w in _WORD_RUN.findall(text or ""))
    return tokens


def _clean_line(line: str) -> str:
    """去掉行首 markdown 引用/标题/列表标记,供摘要展示用。"""
    return re.sub(r"^\s*(?:>\s*)+", "", line or "")


def _snippet(body_lines: list[str]) -> str:
    """块的摘要:合并正文行、折叠空白后取前 SNIPPET_LEN 字。"""
    text = re.sub(r"\s+", " ", " ".join(_clean_line(ln) for ln in body_lines)).strip()
    return text[:SNIPPET_LEN]


def _to_float(value: object, default: float = 0.0) -> float:
    """防御式取数:bool/非数字一律按 default 计(对齐 fusion 的红线 8 口径)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _as_dict(value: object) -> dict[str, Any]:
    """把"应该是 dict"的 intel 字段安全规整;None/异常结构一律空 dict。"""
    return value if isinstance(value, dict) else {}


def _same_file(chunk_file: str, name: str) -> bool:
    """按文件主干名比较(容忍大小写与路径差异)。"""
    return Path(chunk_file).stem == Path(name).stem


# ---------------------------------------------------------------------------
# V5 文件级索引缓存:语料指纹未变 → 跨实例复用切块与词频
# ---------------------------------------------------------------------------

#: ``{文件路径: (指纹, 切块列表, 各块词频列表)}``;指纹为
#: ``(文件大小, st_mtime_ns)``,指纹变化即重建(文件被编辑后自动失效)。
#: 只缓存语料路径(数量有限),无界增长风险可忽略;并发构造时最坏
#: 重复解析一次、后写覆盖,结果幂等无害。
_FILE_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, str]], list[Counter]]] = {}


def _file_fingerprint(path: Path) -> tuple[int, int]:
    """语料文件指纹:(大小, mtime_ns)。读取失败按 (−1, −1) 处理。"""
    try:
        st = path.stat()
    except OSError:
        return (-1, -1)
    return (st.st_size, st.st_mtime_ns)


def _load_file_cached(path: Path) -> tuple[list[dict[str, str]], list[Counter]]:
    """读取 + 分块 + 2-gram 分词(带文件级缓存)。

    指纹命中直接返回缓存的切块与词频(浅共享:``blocks()`` 返回逐块
    dict 拷贝,调用方拿不到可变内部状态);指纹变化(编辑 / 替换)则
    重新解析并覆盖缓存。
    """
    key = str(path)
    fp = _file_fingerprint(path)
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == fp:
        telemetry.inc("regulation.cache_hit")
        return cached[1], cached[2]
    chunks = _load_file_uncached(path)
    tfs = [Counter(_tokenize(ch["text"])) for ch in chunks]
    _FILE_CACHE[key] = (fp, chunks, tfs)
    return chunks, tfs


def _load_file_uncached(path: Path) -> list[dict[str, str]]:
    """读取单个语料文件并切块(原 ``RegulationIndex._load_file`` 逻辑)。"""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    h1 = ""
    for ln in lines:
        m = _H1_RE.match(ln)
        if m:
            h1 = m.group(1)
            break

    chunks: list[dict[str, str]] = []
    preamble: list[str] = []
    title: str | None = None
    body: list[str] = []

    def _flush(t: str | None, body_lines: list[str]) -> None:
        if t is None:
            return
        chunks.append(
            {
                "file": path.name,
                "title": t,
                "snippet": _snippet(body_lines),
                "text": f"{t}\n" + "\n".join(body_lines),
            }
        )

    for ln in lines:
        m = _HEADING_RE.match(ln)
        if m:
            _flush(title, body)
            title, body = m.group(1), []
        elif title is None:
            preamble.append(ln)
        else:
            body.append(ln)
    _flush(title, body)

    # 文件头块:去掉 H1 行本身;正文为空(纯标题文件)则不建块
    pre_body = [ln for ln in preamble if not _H1_RE.match(ln)]
    if any(ln.strip() for ln in pre_body):
        head_title = h1 or path.stem
        chunks.insert(
            0,
            {
                "file": path.name,
                "title": head_title,
                "snippet": _snippet(pre_body),
                "text": f"{head_title}\n" + "\n".join(pre_body),
            },
        )
    return chunks


class RegulationIndex:
    """本地法规语料索引:切块 + 中文 2-gram + BM25-lite 检索。

    用法::

        idx = RegulationIndex("docs/regulations")
        hits = idx.search("色情 举报", top_k=3)
        advice = idx.suggest_category(report)
        titles = idx.legal_basis_for("色情类")
    """

    def __init__(self, corpus_dir: str) -> None:
        root = Path(corpus_dir)
        if not root.is_dir():
            raise ValueError(
                f"法规语料目录不存在或不是目录:{root}"
                "(请确认 docs/regulations 下的语料 md 文件已就位)"
            )
        self.corpus_dir = root
        with telemetry.timer("regulation.index_build"):
            self._chunks: list[dict[str, str]] = []
            self._tfs: list[Counter[str]] = []
            for md in sorted(root.glob("*.md")):
                try:
                    chunks, tfs = _load_file_cached(md)
                except OSError as exc:  # 单文件损坏只告警跳过,不拖垮整个索引
                    logger.warning("法规语料文件读取失败,已跳过:%s(%s)", md.name, exc)
                    continue
                self._chunks.extend(chunks)
                self._tfs.extend(tfs)
            # BM25 统计:每块长度、平均长度、各词文档频率、倒排表(V5)
            self._doclens: list[int] = [len(tf) for tf in self._tfs]
            self._dfs: Counter[str] = Counter()
            self._postings: dict[str, list[int]] = {}
            for i, tf in enumerate(self._tfs):
                for term in tf:
                    self._dfs[term] += 1
                    self._postings.setdefault(term, []).append(i)
            total = sum(self._doclens)
            self._avgdl: float = (total / len(self._doclens)) if self._doclens else 0.0

    # ------------------------------------------------------------------ 加载

    # ------------------------------------------------------------------ 检索

    def __len__(self) -> int:
        return len(self._chunks)

    def blocks(self) -> list[dict[str, str]]:
        """返回全部语料块的浅拷贝列表(键:file/title/snippet/text)。"""
        return [dict(ch) for ch in self._chunks]

    def _idf(self, term: str) -> float:
        """BM25+ 式 idf:ln(1 + (N - df + 0.5)/(df + 0.5)),恒为正。"""
        n = len(self._chunks)
        df = self._dfs.get(term, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """BM25-lite 检索:返回按相关度降序的前 top_k 块。

        每个结果为 ``{"file", "title", "snippet", "score"}``(score 为 BM25
        得分,四舍五入 4 位);query 与语料都按中文 2-gram 分词,查询词重复
        出现(词频)会加权。空 query / 纯空白 query / top_k<=0 / 无语料 → []。
        只返回得分 > 0 的块。

        V5:查询词 idf 每词只算一次;经倒排表只对包含查询词的块打分
        (不含任何查询词的块得分恒为 0,本就不进结果,输出与全量逐块
        计算完全一致);耗时记 ``telemetry.timer("regulation.search")``。
        """
        q_tokens = _tokenize((query or "").strip())
        if not q_tokens or top_k <= 0 or not self._chunks:
            return []
        with telemetry.timer("regulation.search"):
            qtf = Counter(q_tokens)
            # 查询词 idf 预计算(旧实现嵌在文档循环里,每块×每词重算一次 ln)
            idf = {term: self._idf(term) for term in qtf}
            # 倒排表取候选块:含至少一个查询词(其余块得分必为 0)
            candidates = set()
            for term in qtf:
                candidates.update(self._postings.get(term, ()))

            scores: dict[int, float] = {}
            for i in candidates:
                tf = self._tfs[i]
                dl = self._doclens[i]
                len_norm = (
                    BM25_B * (dl / self._avgdl) if self._avgdl > 0 else 0.0
                )
                denom_base = BM25_K1 * (1.0 - BM25_B + len_norm)
                s = 0.0
                for term, q in qtf.items():
                    f = tf.get(term, 0)
                    if not f:
                        continue
                    s += q * idf[term] * (
                        f * (BM25_K1 + 1.0) / (f + denom_base)
                    )
                if s > 0.0:
                    scores[i] = s

            order = sorted(scores, key=lambda i: (-scores[i], i))
            results: list[dict[str, Any]] = []
            for i in order:
                ch = self._chunks[i]
                results.append(
                    {
                        "file": ch["file"],
                        "title": ch["title"],
                        "snippet": ch["snippet"],
                        "score": round(scores[i], 4),
                    }
                )
                if len(results) >= top_k:
                    break
        return results

    # -------------------------------------------------------------- 类别建议

    def _build_query(self, report: SiteReport, cfg: Config) -> str:
        """从判定报告拼检索查询(如"举报 色情 图片 命中色情低俗词 6 个")。

        组成:基础词(举报)+ 聚合分档位词(色情/低俗/其他)+ 复核与图片信号
        + intel 里 URL/文本情报的 risk 阈值信号与 explain 原文(截断)。
        """
        parts: list[str] = ["举报"]
        agg = _to_float(getattr(report, "agg_nsw_prob", 0.0))
        if agg >= cfg.nsfw_threshold:
            parts.append("色情")
        elif agg >= cfg.review_threshold:
            parts.append("低俗")
        else:
            parts.append("其他")
        if getattr(report, "needs_review", False):
            parts.append("需人工复核")
        if _to_int(getattr(report, "nsw_image_count", 0)) > 0:
            parts.append("图片")

        intel = _as_dict(getattr(report, "intel", None))
        url_feat = _as_dict(intel.get("url"))
        text_feat = _as_dict(intel.get("text"))

        if _to_float(url_feat.get("risk")) >= _URL_RISK_SIGNAL:
            parts.append("可疑链接")
        feats = _as_dict(text_feat.get("features"))
        porn_hits = _to_int(feats.get("porn_hits"))
        if porn_hits > 0:
            parts.append("色情")
            # 强色情信号(命中词多或标题可疑)追加语料专有短语,让检索明显
            # 倒向"色情类"节,而不是被档位默认词/混合解释带偏。
            if porn_hits >= 3 or _to_int(feats.get("title_suspect")) > 0:
                parts.append("色情图片")
                parts.append("色情视频")
        if _to_int(feats.get("lure_hits")) > 0:
            parts.append("低俗 诱导")
        if _to_int(feats.get("gambling_hits")) > 0:
            parts.append("赌博 博彩")

        for feat in (url_feat, text_feat):
            lines = feat.get("explain")
            if not isinstance(lines, list):
                continue
            taken = 0
            for line in lines:
                if taken >= _MAX_EXPLAIN_LINES:
                    break
                text = str(line).strip()
                if not text:
                    continue
                parts.append(text[:_MAX_EXPLAIN_CHARS])
                taken += 1

        seen: set[str] = set()
        unique: list[str] = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return " ".join(unique)

    def suggest_category(
        self, report: SiteReport, *, cfg: Config | None = None
    ) -> dict[str, str]:
        """为判定报告建议 12377 举报类别(纯本地检索,零外呼)。

        规则:检索命中 CATEGORY_FILE 语料里的类别节(标题恰为九大类之一)则
        按命中文档映射类别(按相关度排序取首个类别节);否则按聚合分档位给
        默认类别。返回::

            {"category": "色情类" | "低俗类" | ...,
             "basis":   "检索依据:<title>(<file>) 匹配度 x.xx",
             "snippet": 最佳块正文前 80 字}
        """
        conf = cfg if cfg is not None else Config()
        query = self._build_query(report, conf)
        hits = self.search(query, top_k=3)

        best = hits[0] if hits else None
        category: str | None = None
        for h in hits:
            if _same_file(h["file"], CATEGORY_FILE) and h["title"] in CATEGORIES:
                category = h["title"]
                best = h
                break

        if category is None:  # 检索未映射到类别节 → 按聚合分档位给默认
            agg = _to_float(getattr(report, "agg_nsw_prob", 0.0))
            if agg >= conf.nsfw_threshold:
                category = "色情类"
            elif agg >= conf.review_threshold:
                category = "低俗类"
            else:
                category = "其他类"

        if best is not None:
            basis = (
                f"检索依据:{best['title']}({best['file']})"
                f" 匹配度 {best['score']:.2f}"
            )
            snippet = best["snippet"]
        else:
            basis = "检索依据:语料无命中,按聚合分档位给默认类别"
            snippet = ""
        return {"category": category, "basis": basis, "snippet": snippet}

    # -------------------------------------------------------------- 法规依据

    def legal_basis_for(self, category: str) -> list[str]:
        """返回 legal_basis.md 中与类别相关的条目标题列表。

        匹配口径:条目标题包含该类别在 ``_CATEGORY_LEGAL_KEYWORDS`` 里登记的
        任意法规关键词;任何类别都会附带"虚假举报的法律责任提示"条目
        (每次举报都负有如实陈述义务)。未知类别只返回责任提示;语料缺
        legal_basis.md 时返回空列表。
        """
        keywords = _CATEGORY_LEGAL_KEYWORDS.get(category, ())
        titles: list[str] = []
        for ch in self._chunks:
            if not _same_file(ch["file"], LEGAL_FILE):
                continue
            title = ch["title"]
            if "虚假举报" in title or any(k in title for k in keywords):
                titles.append(title)
        return titles
