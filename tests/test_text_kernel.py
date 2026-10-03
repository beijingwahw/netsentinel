"""A124 text_kernel 测试(离线纯函数 + V7 基准 AUC)。

覆盖:缺省种子生成(text_intel 词表组合 / 缺席回退)、TF-IDF 精确数学、
HTML 复用与回退剥标签、risk 区间与钳位、空文本中性、自定义种子、
遥测计数、与 text_intel 并存零修改、零外呼约束,以及
``test_v7_bench_auc``(构造正负语料各 10 段的排序 AUC 断言,红线 31:
确定性合成数据上的精确结果,不依赖墙钟)。
"""
from __future__ import annotations

import ast
import inspect
import sys

import pytest

from netsentinel import telemetry
from netsentinel.intel import text_kernel as tk
from netsentinel.intel import text_intel
from netsentinel.intel.text_kernel import TextKernel, kernel_selfcheck

# --- 测试语料:围绕种子风格自写,与种子/自检语料零句重复 ------------------

POS_EVAL = [
    "深夜福利大放送,高清无码资源免费观看,手机在线看片不卡顿。",
    "无码专区今日上新,日韩欧美精品汇聚,扫码进群看片享福利。",
    "大尺度直播凌晨开播,裸聊主播在线互动,点击观看完整版。",
    "激情电影免费在线播放,中文字幕AV每日更新资源不断。",
    "附近的人可约,一夜情速配,未满十八岁勿入本站。",
    "爽片在线看,福利姬私密视频泄露,援交约会一夜搞定。",
    "黄色网站大全收录,成人视频一站看全,手机看片更方便。",
    "淫秽图片区免费开放,性爱视频高清直播,在线撸不停。",
    "裸秀直播每晚十点开场,不雅视频打包下载,免费观看。",
    "约炮神器悄悄上线,附近的人可约,大尺度直播等你解锁。",
]

NEG_EVAL = [
    "记者从工信部获悉,国产操作系统最新版本今日发布,系统响应速度提升明显。",
    "气象台预报显示,明日全市多云转晴,最高气温二十六摄氏度,适宜户外活动。",
    "CBA常规赛昨晚继续进行,客队末节打出高潮逆转取胜,球员赛后感谢球迷支持。",
    "研究机构发布报告称,第三季度全球智能手机出货量同比增长百分之五。",
    "受暖湿气流影响,未来三天江南地区持续降雨,局部伴有雷电,请注意防范。",
    "全国游泳锦标赛落幕,东道主代表队夺得四枚金牌,小将表现令人眼前一亮。",
    "新款电动汽车开始交付,续航里程突破七百公里,订单排至明年三月。",
    "冷空气南下带来大风降温,北方部分地区今晨出现初雪,交通总体运行平稳。",
    "城市马拉松赛今晨鸣枪,两万名跑者参与,赛事组织方沿途设置多个补给点。",
    "天文台通报,本周将出现月偏食天象,我国大部分地区可在傍晚观测。",
]


def _auc(kernel: TextKernel, pos: list[str], neg: list[str]) -> float:
    """Mann-Whitney 排序 AUC:正样本 risk 压过负样本的配对比例(平局 0.5)。"""
    ps = [kernel.score(t)["risk"] for t in pos]
    ns = [kernel.score(t)["risk"] for t in neg]
    won = sum(1.0 if p > n else 0.5 if p == n else 0.0
              for p in ps for n in ns)
    return won / (len(ps) * len(ns))


@pytest.fixture()
def isolate_text_intel(monkeypatch):
    """把 text_intel 模块"移走"(sys.modules 置 None + 父包删属性),用毕恢复。"""
    pkg = sys.modules["netsentinel.intel"]
    real = sys.modules.get("netsentinel.intel.text_intel")
    monkeypatch.setitem(sys.modules, "netsentinel.intel.text_intel", None)
    monkeypatch.delattr(pkg, "text_intel", raising=False)
    yield
    if real is not None:  # 直接复原(monkeypatch 卸载时同样恢复到真值)
        sys.modules["netsentinel.intel.text_intel"] = real
        setattr(pkg, "text_intel", real)


# --- 缺省种子 -------------------------------------------------------------

def test_default_seeds_shape():
    k = TextKernel()
    assert len(k.seed_pos) >= 6
    assert len(k.seed_neg) >= 6
    assert all(isinstance(d, str) and d.strip() for d in k.seed_pos + k.seed_neg)
    # 负组覆盖科技/天气/体育三类资讯
    joined = "".join(k.seed_neg)
    for word in ("芯片", "气象", "联赛"):
        assert word in joined


def test_seed_pos_covers_text_intel_vocab():
    k = TextKernel()
    joined = "".join(k.seed_pos)
    for w in text_intel.LURE_PHRASES + text_intel.PORN_KEYWORDS:
        assert w in joined, f"词表词未织入样张: {w}"


def test_fallback_vocab_without_text_intel(isolate_text_intel):
    k = TextKernel()  # text_intel 缺席 → 内置同表回退
    assert len(k.seed_pos) >= 6
    joined = "".join(k.seed_pos)
    for w in tk._FALLBACK_LURE_PHRASES + tk._FALLBACK_PORN_KEYWORDS:
        assert w in joined
    # 回退表与真实表一致 → 生成的样张与常态完全相同
    assert k.seed_pos == TextKernel().seed_pos
    assert k.seed_neg == TextKernel().seed_neg


def test_empty_seed_rejected():
    with pytest.raises(ValueError):
        TextKernel(seed_pos=[], seed_neg=["正常新闻一段。"])
    with pytest.raises(ValueError):
        TextKernel(seed_pos=["诱导文案一段。"], seed_neg=[])


# --- score 契约 ------------------------------------------------------------

def test_score_contract_keys_and_range():
    r = TextKernel().score(POS_EVAL[0])
    assert set(r) == {"risk", "cos_pos", "cos_neg"}
    assert 0.0 <= r["risk"] <= 1.0
    assert 0.0 <= r["cos_pos"] <= 1.0
    assert 0.0 <= r["cos_neg"] <= 1.0


def test_pos_style_text_risk_high():
    r = TextKernel().score(POS_EVAL[2])
    assert r["risk"] >= 0.6
    assert r["cos_pos"] > r["cos_neg"]


def test_neg_style_text_risk_low():
    r = TextKernel().score(NEG_EVAL[3])
    assert r["risk"] < 0.5
    assert r["cos_neg"] > r["cos_pos"]


def test_pos_neg_margin_separated():
    k = TextKernel()
    pos_min = min(k.score(t)["risk"] for t in POS_EVAL)
    neg_max = max(k.score(t)["risk"] for t in NEG_EVAL)
    assert pos_min > neg_max  # 两族完全线性可分(确定性合成数据)


def test_score_deterministic():
    k = TextKernel()
    assert k.score(POS_EVAL[1]) == k.score(POS_EVAL[1])
    assert k.score(NEG_EVAL[1]) == TextKernel().score(NEG_EVAL[1])


def test_score_clamps_at_bounds():
    k = TextKernel()
    assert k.score(k.seed_pos[0])["risk"] == 1.0   # 正样张自身 → 上钳位
    assert k.score(k.seed_neg[0])["risk"] == 0.0   # 负样张自身 → 下钳位


def test_empty_and_whitespace_neutral():
    k = TextKernel()
    for blank in ("", "   ", "\n\t "):
        r = k.score(blank)
        assert r == {"risk": 0.5, "cos_pos": 0.0, "cos_neg": 0.0}


def test_non_string_rejected():
    with pytest.raises(TypeError):
        TextKernel().score(12345)


# --- 精确数学(红线 31:确定性构造数据上的精确结果) ------------------------

def test_exact_tfidf_math():
    k = TextKernel(seed_pos=["abcd"], seed_neg=["wxyz"])
    # 语料 = 2 种子 + 输入,共 3 篇;输入与正种子 gram 集合完全一致 → cos=1
    r = k.score("abcd")
    assert r["cos_pos"] == pytest.approx(1.0)
    assert r["cos_neg"] == 0.0
    assert r["risk"] == 1.0
    r = k.score("wxyz")
    assert r["cos_neg"] == pytest.approx(1.0)
    assert r["risk"] == 0.0
    # 对称构造:与两组各恰共享一个同 idf 的 2-gram → 两余弦相等,risk=0.5
    r = k.score("abwx")
    assert r["cos_pos"] == pytest.approx(r["cos_neg"])
    assert r["risk"] == pytest.approx(0.5)


def test_idf_formula_and_group_mean():
    # 手算验证整条链路:tf 归一(÷gram 总数)、idf=log((N+1)/(df+1))+1 的
    # 两个分支(df=1 / df=2)、组质心权重、余弦与钳位。
    import math
    k = TextKernel(seed_pos=["ab"], seed_neg=["yz"])
    r = k.score("abz")
    # 语料 N=3(2 种子+输入);"ab" df=2(正种子+输入),"bz"/"abz" df=1(仅输入)
    idf_ab = math.log(4 / 3) + 1
    idf_rare = math.log(4 / 2) + 1
    v_ab, v_bz, v_abz = idf_ab / 3, idf_rare / 3, idf_rare / 3
    norm = math.sqrt(v_ab ** 2 + v_bz ** 2 + v_abz ** 2)
    # 正质心仅含 "ab"(权重 = 1.0*idf_ab),cos = v_ab/norm;负组零重叠
    assert r["cos_pos"] == pytest.approx(v_ab / norm)
    assert r["cos_neg"] == 0.0
    assert r["risk"] == 1.0  # 0.5+2*cos > 1 → 上钳位


# --- HTML 路径 --------------------------------------------------------------

def test_html_reuses_text_intel_extraction():
    k = TextKernel()
    body = "今日发布新款芯片,性能提升百分之三十。"
    assert k.score(f"<p>{body}</p>") == k.score(body)  # 剥标签后逐位一致


def test_html_fallback_strips_tags(isolate_text_intel):
    k = TextKernel()  # text_intel 缺席 → stdlib 正则剥标签回退
    html = ("<html><head><title>免费观看</title></head>"
            "<body><p>深夜福利 无码专区 高清资源</p></body></html>")
    r = k.score(html)
    assert r["risk"] > 0.5
    assert r["cos_pos"] > r["cos_neg"]


# --- 自定义种子 -------------------------------------------------------------

def test_custom_seeds_switch_domain():
    k = TextKernel(seed_pos=["内幕消息 私募建仓 拉升出货 荐股群"],
                   seed_neg=["天气预报 晴 气温 降水"])
    r_pos = k.score("荐股群流出内幕消息,私募建仓即将拉升出货。")
    r_neg = k.score("明天天气预报晴,气温回升,降水概率低。")
    assert r_pos["risk"] > r_neg["risk"]
    assert r_pos["cos_neg"] < r_neg["cos_neg"]
    assert r_neg["cos_neg"] > r_neg["cos_pos"]


def test_custom_seed_not_polluted_by_defaults():
    k = TextKernel(seed_pos=["唯一正向种子词词词"], seed_neg=["唯一负向种子字字字"])
    r = k.score("完全无关的中性文本内容示例")
    assert 0.0 <= r["risk"] <= 1.0


# --- 遥测 / 并存 / 零外呼 ----------------------------------------------------

def test_telemetry_counter():
    telemetry.reset()
    k = TextKernel()
    k.score(POS_EVAL[0])
    k.score(NEG_EVAL[0])
    k.score("")
    assert telemetry.snapshot()["counters"]["text_kernel.score"] == 3.0


def test_coexist_with_text_intel_zero_mutation():
    lure_before = text_intel.LURE_PHRASES
    porn_before = text_intel.PORN_KEYWORDS
    attrs_before = set(vars(text_intel))
    k = TextKernel()
    k.score("<title>免费观看</title><p>深夜福利</p>")
    # 词表对象原封不动,模块命名空间零增删(text_intel 不被 import 修改)
    assert text_intel.LURE_PHRASES is lure_before
    assert text_intel.PORN_KEYWORDS is porn_before
    assert set(vars(text_intel)) == attrs_before
    # 反向:text_intel 不感知 text_kernel
    assert "text_kernel" not in inspect.getsource(text_intel)


def test_stdlib_only_and_no_network_imports():
    src = inspect.getsource(tk)
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {"html", "math", "re", "netsentinel",
                        "netsentinel.intel", "netsentinel.intel.text_intel",
                        "__future__"}
    banned = ("socket", "urllib", "http", "requests", "subprocess", "ssl")
    for name in imported:
        assert not any(b in name for b in banned), f"疑似外呼依赖: {name}"


# --- 基准总控自检(A138 挂钩) ----------------------------------------------

def test_kernel_selfcheck_shape():
    r = kernel_selfcheck()
    assert r["name"] == "text_kernel"
    assert r["metric"] == "AUC"
    assert r["value"] >= r["baseline"] == 0.9


# --- V7 基准:构造语料 AUC(红线 31,无墙钟断言) ---------------------------

def test_v7_bench_auc():
    assert len(POS_EVAL) >= 10 and len(NEG_EVAL) >= 10
    k = TextKernel()
    seeds = set(k.seed_pos) | set(k.seed_neg)
    # 评测语料与种子零句重复(风格相近但不是原文 → 考察泛化,非背答案)
    assert not (set(POS_EVAL) | set(NEG_EVAL)) & seeds
    auc = _auc(k, POS_EVAL, NEG_EVAL)
    assert auc >= 0.9, f"排序 AUC {auc:.4f} < 0.9"
