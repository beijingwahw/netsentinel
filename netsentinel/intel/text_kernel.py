"""语言内核·字符 n-gram TF-IDF(V7 · A124)。

与 :mod:`netsentinel.intel.text_intel`(词表命中式启发)并存的**学习型**文本
风险评估内核:把正/负两组种子样张与待测文本统一放进一个语料库,用字符
2-gram + 3-gram TF-IDF 向量 + 组内均值向量的余弦相似度给文本打分——
不看任何具体词是否命中,而看"文本长得像哪一组样张",因此对未收录在
词表里的同风格变体表述同样敏感。

算法(纯标准库,零外呼——红线 30):

- 分词:统一小写、折叠空白后,滑窗取全部字符 2-gram 与 3-gram
  (键带 ``(2|3, gram)`` 前缀,两阶互不串味);
- TF:词频 = 该 gram 在文档中的次数 / 文档 gram 总数(L1 归一,长短文本可比);
- IDF:语料 = 全部种子文档 + 待测输入文档(共 N 篇),
  ``idf(g) = log((N+1)/(df+1)) + 1``(df = 含该 gram 的文档数,平滑防除零);
- 组向量:pos / neg 组各取成员文档 TF-IDF 向量的**均值向量**(质心);
- 评分:输入向量与两组质心分别余弦,::

      risk = clamp(0.5 + 2.0 * (cos_pos - cos_neg), 0, 1)

  即"两组都不像"→0.5 中性,像正组→上推,像负组→下压。

种子与依赖:

- 缺省 ``seed_pos``:惰性 ``import netsentinel.intel.text_intel``,取其
  ``LURE_PHRASES + PORN_KEYWORDS`` 词表**组合织入 ≥6 段诱导文案样张**
  (词表级规范词,不含具体淫秽描写;text_intel 缺席时回退到本模块内置
  同表拷贝,行为不变);
- 缺省 ``seed_neg``:自写 ≥6 段正常资讯文样张(科技/天气/体育);
- HTML 输入:惰性复用 ``text_intel.extract_visible_text`` 剥离标签
  (缺席时用本模块 stdlib 正则剥标签回退);本模块**只读**兄弟模块,
  绝不修改 text_intel(契约 §2 A124);
- 零联网、不训练外模型:仅消费调用方传入的本地文本与内置合成样张。

局限(校准口径):risk 为相似度几何映射而非概率,未经概率校准;建议作为
排序/辅助特征消费(与 fusion 中 text 特征同级),不要直接当阈值概率用。

用法::

    from netsentinel.intel.text_kernel import TextKernel

    kern = TextKernel()
    kern.score("<title>免费观看</title><p>深夜福利 无码专区</p>")
    # {"risk": 0.9xxx, "cos_pos": 0.4x, "cos_neg": 0.0x}

关键入口记 ``telemetry.inc("text_kernel.score")``(只存计数,不存文本)。
"""
from __future__ import annotations

import html as _html
import math
import re

from netsentinel import telemetry

__all__ = ["TextKernel", "kernel_selfcheck"]

_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[a-zA-Z!/][^>]*>")
_TAG_STRIP_RE = re.compile(r"<[^>]*>")

# --- 内置同表(text_intel 缺席时的回退;与 text_intel.py 词表保持一致) --
# 仅当 ``netsentinel.intel.text_intel`` 不可导入时启用,内容为其词表快照。
_FALLBACK_LURE_PHRASES: tuple[str, ...] = (
    "免费观看", "无码专区", "手机看片", "深夜福利", "爽片在线", "在线撸",
    "日韩欧美", "每日更新资源", "扫码进群看片", "点击观看完整版",
    "未满十八岁勿入", "附近的人可约", "大尺度直播",
)

_FALLBACK_PORN_KEYWORDS: tuple[str, ...] = (
    "色情", "情色", "淫秽", "淫秽物品", "成人电影", "成人视频", "成人网站",
    "黄色电影", "黄色网站", "裸聊", "裸秀", "援交", "卖淫", "嫖娼",
    "一夜情", "约炮", "无码", "有码", "中文字幕AV", "激情电影", "激情视频",
    "情欲电影", "艳照", "不雅视频", "私密部位", "性爱视频", "做爱视频",
    "处女膜", "性行为直播", "裸体直播", "福利姬", "外围女",
)

# 缺省正组样张模板:词表词按序分组织入({words} 为占位),连接文案全部
# 为平台审核通用规范用语(专区/资源/直播/在线等),不含具体淫秽描写。
_POS_TEMPLATES: tuple[str, ...] = (
    "【今日更新】{words}——高清完整版在线免费观看,海量资源每日更新。",
    "深夜专属福利:{words},手机看片一步到位,无需下载扫码进群。",
    "本站专区强势上线:{words},日韩欧美精品随心看,未满十八岁勿入。",
    "独家资源首发:{words},大尺度直播全天候在线,点击观看完整版。",
    "会员免费领取:{words},爽片在线看不停,高清流畅不卡顿。",
    "限时开放注册:{words},激情视频随便看,每日更新资源不断档。",
    "福利集合地:{words},在线撸福利多多,扫码进群看片更精彩。",
)

#: 缺省正组样张段数(契约:≥6 段诱导文案样张)
_POS_DOC_COUNT = 7

# 缺省负组样张:自写正常资讯文(科技/天气/体育),风格与正组互斥。
_DEFAULT_SEED_NEG: tuple[str, ...] = (
    # 科技 ×3
    "记者从国家超算中心获悉,新一代国产芯片完成流片,单核性能较上一代提升"
    "约两成,预计明年初投入商用。",
    "某民营航天公司今天上午完成可回收火箭的第三次垂直起降试验,着陆精度"
    "达到预定指标,为明年入轨发射奠定基础。",
    "在昨日闭幕的国际消费电子展上,多家厂商发布搭载端侧大模型的新款笔记本"
    "电脑,现场观众排队体验语音助手功能。",
    # 天气 ×3
    "中央气象台今天早晨发布台风橙色预警,预计未来两天沿海地区将有暴雨到"
    "大暴雨,局部阵风可达十二级,提醒渔船及时回港避风。",
    "受冷空气持续影响,本周末北方多地气温将下降八到十摄氏度,并伴有四五级"
    "偏北风,气象部门提示公众适时添衣保暖。",
    "气象卫星监测显示,今晨华北大部出现能见度不足五百米的大雾,多条高速"
    "公路临时封闭,出行请注意交通安全。",
    # 体育 ×3
    "在昨晚结束的足球联赛半决赛中,主队凭借加时赛的一记头球以二比一险胜"
    "对手,决赛将于下周日在主场打响。",
    "全国马拉松锦标赛本周末鸣枪起跑,共有三万余名跑者报名参赛,组委会在"
    "城市沿线设置二十个补给站与医疗点。",
    "中国女子排球队在世界联赛分站赛中直落三局击败对手,主教练赛后表示,"
    "年轻队员的拦网表现超出预期。",
)


def _load_vocab() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """惰性读取 text_intel 词表;缺席/异常回退内置同表(绝不修改其属性)。"""
    try:
        from netsentinel.intel import text_intel as _ti  # 惰性导入,只读
        lure = tuple(_ti.LURE_PHRASES)
        porn = tuple(_ti.PORN_KEYWORDS)
        if lure and porn:
            return lure, porn
    except Exception:  # ImportError 及任何意外 → 内置同表,行为不变
        pass
    return _FALLBACK_LURE_PHRASES, _FALLBACK_PORN_KEYWORDS


def _default_seed_pos() -> list[str]:
    """由 LURE_PHRASES + PORN_KEYWORDS 组合织入 ≥6 段诱导文案样张。"""
    lure, porn = _load_vocab()
    words = [w for w in (*lure, *porn) if w]
    docs: list[str] = []
    if words:
        size = max(1, math.ceil(len(words) / _POS_DOC_COUNT))
        for i, tmpl in enumerate(_POS_TEMPLATES):
            chunk = words[i * size:(i + 1) * size]
            if not chunk:
                break
            docs.append(tmpl.format(words="、".join(chunk)))
    return docs


def _normalize(text: str) -> str:
    """小写 + 空白折叠(与 text_intel 的空白口径一致)。"""
    return _WS_RE.sub(" ", text).strip().lower()


def _strip_tags_fallback(html_text: str) -> str:
    """text_intel 缺席时的 stdlib 剥标签回退(正则 + 实体解码)。"""
    return _html.unescape(_TAG_STRIP_RE.sub(" ", html_text))


def _char_ngram_counts(text: str) -> dict[tuple[int, str], int]:
    """字符 2-gram + 3-gram 计数;键带阶前缀,两阶互不冲突。"""
    counts: dict[tuple[int, str], int] = {}
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            g = (n, text[i:i + n])
            counts[g] = counts.get(g, 0) + 1
    return counts


def _cosine(a: dict[tuple[int, str], float],
            b: dict[tuple[int, str], float]) -> float:
    """稀疏向量余弦;任一侧为空 → 0.0(无共同信息,不打偏分)。"""
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = 0.0
    for g, v in small.items():
        w = large.get(g)
        if w is not None:
            dot += v * w
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


class TextKernel:
    """字符 2/3-gram TF-IDF + 组质心余弦的语言风险内核(V7 · A124)。

    参数:
        seed_pos: 正组种子样张(诱导文案风格);缺省由 text_intel 的
            ``LURE_PHRASES + PORN_KEYWORDS`` 词表组合生成。
        seed_neg: 负组种子样张(正常资讯文风格);缺省用内置自写样张
            (科技/天气/体育)。

    输出 ``score()`` 的 risk 是相似度几何映射(未做概率校准),适合
    排序与作为融合辅助特征;详见模块 docstring「局限」。
    """

    def __init__(self, seed_pos: list[str] | None = None,
                 seed_neg: list[str] | None = None) -> None:
        pos = list(seed_pos) if seed_pos is not None else _default_seed_pos()
        neg = list(seed_neg) if seed_neg is not None else list(_DEFAULT_SEED_NEG)
        if not pos or not neg:
            raise ValueError("seed_pos 与 seed_neg 均需至少一段非空样张")
        #: 正组种子样张(调用方可见的冻结拷贝)
        self.seed_pos: list[str] = pos
        #: 负组种子样张(调用方可见的冻结拷贝)
        self.seed_neg: list[str] = neg

        # 预计算:各文档 (gram 计数, gram 总数) + gram 文档频率(种子侧 df)
        self._pos_docs = [self._doc_entry(d) for d in pos]
        self._neg_docs = [self._doc_entry(d) for d in neg]
        self._seed_df: dict[tuple[int, str], int] = {}
        for counts, _total in self._pos_docs + self._neg_docs:
            for g in counts:
                self._seed_df[g] = self._seed_df.get(g, 0) + 1
        self._n_seed_docs = len(self._pos_docs) + len(self._neg_docs)

    # --- 内部 -------------------------------------------------------------

    @staticmethod
    def _doc_entry(doc: str) -> tuple[dict[tuple[int, str], int], int]:
        counts = _char_ngram_counts(_normalize(doc))
        return counts, sum(counts.values())

    def _to_plain_text(self, raw: str) -> str:
        """HTML → 纯文本(惰性复用 text_intel,缺席走 stdlib 回退),再归一化。"""
        if _HTML_TAG_RE.search(raw):
            try:
                from netsentinel.intel.text_intel import (
                    extract_visible_text,  # 惰性导入,只读复用
                )
                title, body = extract_visible_text(raw)
                raw = f"{title} {body}" if title else body
            except Exception:
                raw = _strip_tags_fallback(raw)
        return _normalize(raw)

    def _centroid(self, docs, idf_of) -> dict[tuple[int, str], float]:
        """组内成员 TF-IDF 向量的均值向量(空文档自动跳过)。"""
        acc: dict[tuple[int, str], float] = {}
        used = 0
        for counts, total in docs:
            if total <= 0:
                continue
            used += 1
            for g, c in counts.items():
                acc[g] = acc.get(g, 0.0) + (c / total) * idf_of(g, False)
        if not used:
            return {}
        return {g: v / used for g, v in acc.items()}

    # --- 公开 API ----------------------------------------------------------

    def score(self, text_or_html: str) -> dict:
        """评估一段文本/HTML 的语言风险。

        返回 ``{"risk": 0~1, "cos_pos": 0~1, "cos_neg": 0~1}``;
        ``risk = clamp(0.5 + 2.0*(cos_pos - cos_neg), 0, 1)``。
        空文本无信息 → 中性 0.5(两组余弦均为 0)。
        """
        telemetry.inc("text_kernel.score")
        if not isinstance(text_or_html, str):
            raise TypeError("text_or_html 必须是 str")

        text = self._to_plain_text(text_or_html or "")
        in_grams = _char_ngram_counts(text)
        if not in_grams:
            return {"risk": 0.5, "cos_pos": 0.0, "cos_neg": 0.0}

        n_docs = self._n_seed_docs + 1  # 语料 = 种子 + 输入
        df_seed = self._seed_df

        def idf_of(g: tuple[int, str], in_input: bool) -> float:
            # 输入含该 gram → df+1;仅种子含 → 原种子 df
            df = df_seed.get(g, 0) + (1 if in_input else 0)
            return math.log((n_docs + 1) / (df + 1)) + 1.0

        total_in = sum(in_grams.values())
        in_vec = {g: (c / total_in) * idf_of(g, True)
                  for g, c in in_grams.items()}
        pos_vec = self._centroid(self._pos_docs, idf_of)
        neg_vec = self._centroid(self._neg_docs, idf_of)

        cos_pos = _cosine(in_vec, pos_vec)
        cos_neg = _cosine(in_vec, neg_vec)
        risk = min(1.0, max(0.0, 0.5 + 2.0 * (cos_pos - cos_neg)))
        return {"risk": round(risk, 4),
                "cos_pos": round(cos_pos, 6),
                "cos_neg": round(cos_neg, 6)}


# --- A138 基准总控自检(内置 held-out 合成语料,与种子零句重复) ---------

_SELF_POS: tuple[str, ...] = (
    "深夜福利专场,无码高清片源免费观看,手机在线即点即看。",
    "大尺度直播凌晨开播,在线互动不停,点击观看完整版。",
    "激情电影在线播放,中文字幕高清,每日更新资源不断。",
    "附近的人可约,一夜情速配,未满十八岁勿入本站。",
    "裸秀直播每晚开场,不雅视频打包,扫码进群看片享福利。",
)

_SELF_NEG: tuple[str, ...] = (
    "记者从发布会现场获悉,新款处理器今日亮相,能效比提升明显。",
    "气象台预报明日多云转晴,最高气温二十六摄氏度,适宜户外活动。",
    "联赛昨晚继续进行,客队末节逆转取胜,球员赛后感谢球迷。",
    "研究机构发布报告称,第三季度智能手机出货量同比增长。",
    "冷空气南下带来大风降温,北方部分地区今晨出现初雪,交通平稳。",
)


def _rank_auc(kernel: TextKernel, pos: list[str], neg: list[str]) -> float:
    """Mann-Whitney AUC:正样本 risk 高于负样本的配对比例(平局计 0.5)。"""
    ps = [kernel.score(t)["risk"] for t in pos]
    ns = [kernel.score(t)["risk"] for t in neg]
    total = len(ps) * len(ns)
    won = 0.0
    for p in ps:
        for n in ns:
            won += 1.0 if p > n else (0.5 if p == n else 0.0)
    return won / total


def kernel_selfcheck() -> dict:
    """A138 ``kernel_bench`` 总控自检:内置合成语料上的排序 AUC。"""
    kernel = TextKernel()
    auc = _rank_auc(kernel, list(_SELF_POS), list(_SELF_NEG))
    return {"name": "text_kernel", "metric": "AUC",
            "value": round(auc, 4), "baseline": 0.9}
