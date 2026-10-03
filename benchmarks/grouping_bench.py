# -*- coding: utf-8 -*-
"""NetSentinel 分组质量基准(A118)—— 合成语料量化"归纳同名"的分对没有。

依据 CONTRACTS-V6.md §4 A118 条目实现 V6 "分组质量基准":V6 的核心承诺是
"同可注册域的镜像归并 + 跨域团伙并组",本框架用**程序合成的带真值标签
语料**对该链路做离线量化验收:

- ``make_synthetic(n_sites, mirrors_per_site, seed)``:合成 ``(url, label)``
  语料——每站一个主域 + 若干镜像变体(www 前缀 / 端口 8080 / 子域 m. /
  路径),同站同标签 ``site{i}``;另造 2 个**团伙对**(跨可注册域但共享
  标签 ``gang{j}``,模拟图片指纹重叠线索)。标签即真值;
- ``evaluate(grouping, labels)``:purity = Σ各组最大同标签数 / 总数;
  completeness = 对每个真标签,其成员被分到的组中同标签占比的(按成员
  加权)平均;均落在 [0, 1] 保留两位小数,另附 ``n_groups``;
- ``run(out_dir, *, grouper=None)``:默认走**真实链**(A104
  ``case_group.group_entries`` 伪 entries + A105 ``group_linker.merge_groups``
  无图谱,全部惰性导入;未就位抛 :class:`GroupingBenchError` 中文错误,
  CLI 以退出码 2 结束),也可注入 ``grouper(pairs) -> grouping`` 做对照
  实验;产出中文 ``grouping_report.md`` + ``grouping_report.json``。

设计要点(为什么主域是 ``site{i}.example`` 而不是 ``site{i}.example.com``):

- A103 的 canonical 归一到**可注册域**——``site0.example.com`` 与
  ``site3.example.com`` 的 canonical 同为 ``example.com``,12 个站会按
  TLD 坍缩成 3 组,与"每站一个真值标签"直接冲突;
- 故主域取保留 TLD ``.example``(RFC 2606,天然适合合成语料):host 为
  ``site{i}.example`` 时 canonical 恰为 ``site{i}.example``,各站独立;
  com/net/org 以**路径后缀**轮换(``/com`` / ``/net`` / ``/org``),
  同时兼顾"路径差异视为同站变体"的镜像语义;
- 团伙对 host 为 ``gang{j}a.example`` / ``gang{j}b.example``:两者
  canonical 不同(验证跨域并组),但报告的图片指纹集合重叠
  (Jaccard = 0.5 ≥ 默认阈值 0.3),据此 A105 应把它们并入同一案件组。

指标语义(注意与聚类文献的惯用命名有差异,以本模块公式为准):

- purity 衡量"组里混没混别人":过度合并(全并一组)会显著拉低;
- completeness 衡量"成员所在组的同标签浓度":混组稀释会拉低;把组拆成
  单例并不扣分(单例组内同标签占比恒为 1)——欠合并不惩罚、误并才惩罚,
  这与"宁可少并、不可误并"的举报合规取向一致。

安全红线:全程离线、零外呼、零真实提交(红线 24 不涉及);语料为程序
合成,不含任何真实站点;仅依赖标准库(netsentinel 真实链按需惰性导入)。

命令行::

    python benchmarks/grouping_bench.py --out benchmarks/out          # 默认语料
    python benchmarks/grouping_bench.py --out benchmarks/out --seed 7
    python benchmarks/grouping_bench.py --n-sites 20 --mirrors-per-site 3
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# 直接以脚本运行(python benchmarks/grouping_bench.py)时,保证项目根在
# sys.path 上,使 netsentinel 真实链可导入;经包导入(tests)时为空操作。
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

__all__ = [
    "GANG_PAIRS",
    "GroupingBenchError",
    "evaluate",
    "fingerprint_shas",
    "make_synthetic",
    "render_markdown",
    "run",
    "main",
]


class GroupingBenchError(RuntimeError):
    """分组基准流程中可预期的错误(中文消息;CLI 捕获后以退出码 2 结束)。"""


#: 团伙对数量(契约给定固定 2 对,不随 n_sites 缩放)。
GANG_PAIRS: int = 2

#: 主域路径后缀轮换表(com/net/org;路径差异视为同站变体,见模块 docstring)。
_TLD_PATHS: tuple[str, str, str] = ("/com", "/net", "/org")

#: 指纹合成盐:同标签 → 同指纹;不同标签(含 site/gang 前缀差异)互不相交。
_SHA_SALT = "netsentinel-grouping-bench"

#: 团伙并组阈值缺省(与 Config.group_merge_phash_overlap 生产默认一致)。
_DEFAULT_OVERLAP = 0.3
_DEFAULT_MERGE_TEMPLATE = True


# ---------------------------------------------------------------------------
# 合成语料生成
# ---------------------------------------------------------------------------


def _variant_pool(i: int) -> list[str]:
    """站点 i 的镜像变体池(www 前缀 / 端口 8080 / 子域 m. / 路径,均合法 http(s))。"""
    tld_path = _TLD_PATHS[i % 3]
    return [
        f"http://www.site{i}.example/",
        f"http://site{i}.example:8080/",
        f"http://m.site{i}.example/",
        f"https://site{i}.example{tld_path}/mirror",
    ]


def make_synthetic(
    n_sites: int = 12,
    mirrors_per_site: int = 2,
    seed: int = 42,
) -> list[tuple[str, str]]:
    """生成带真值标签的合成分组语料,返回 ``(url, label)`` 列表(全 URL 唯一)。

    构成(n_sites=12 / mirrors_per_site=2 / seed=42 时共 40 条):

    - 每站 ``i``:主域 ``http://site{i}.example/com|net|org``(路径按 i 轮换)
      + ``mirrors_per_site`` 个镜像变体(从 www 前缀 / 端口 8080 / 子域 m. /
      https 路径池中按 ``seed`` 抽取;超出 4 个时以端口轮换补足),
      同站全部同标签 ``site{i}`` —— 验证 canonical 镜像归并;
    - 另造 :data:`GANG_PAIRS` 个团伙对:host ``gang{j}a.example`` 与
      ``gang{j}b.example`` **跨可注册域**但共享标签 ``gang{j}``,其报告的
      图片指纹集合重叠(见 :func:`fingerprint_shas`)—— 验证 A105 指纹并组。

    标签即真值;同参数(含 seed)输出决定性一致,纯本地计算零外呼。
    ``n_sites`` / ``mirrors_per_site`` 为负抛 :class:`ValueError`(中文)。
    """
    if n_sites < 0:
        raise ValueError(f"n_sites 不能为负数(当前 {n_sites})")
    if mirrors_per_site < 0:
        raise ValueError(f"mirrors_per_site 不能为负数(当前 {mirrors_per_site})")

    rng = random.Random(seed)
    pairs: list[tuple[str, str]] = []
    for i in range(n_sites):
        label = f"site{i}"
        pairs.append((f"http://site{i}.example{_TLD_PATHS[i % 3]}", label))
        pool = _variant_pool(i)
        if mirrors_per_site >= len(pool):
            chosen = list(pool)
        else:
            chosen = rng.sample(pool, mirrors_per_site)
        pairs.extend((url, label) for url in chosen)
        for k in range(len(pool) + 1, mirrors_per_site + 1):
            pairs.append((f"http://site{i}.example:{8080 + k}/mirror{k}", label))

    for j in range(GANG_PAIRS):
        pairs.append((f"http://gang{j}a.example/", f"gang{j}"))
        pairs.append((f"https://gang{j}b.example/", f"gang{j}"))
    return pairs


def _sha(text: str) -> str:
    """指纹单元:字符串 → sha256 十六进制(合成语料无需真实图片)。"""
    return hashlib.sha256(f"{_SHA_SALT}|{text}".encode("utf-8")).hexdigest()


def fingerprint_shas(label: str, url: str) -> set[str]:
    """按真值标签合成该 URL 报告的图片指纹集合(重叠线索即由此构造)。

    - 站点标签(site{i}):同站主域与全部镜像返回**同一**指纹集合
      (3 个 sha),不同站互不相交 —— 镜像指纹天然一致、异站零重叠;
    - 团伙标签(gang{j}):成员 a 取 {|1,|2,|3}、成员 b 取 {|2,|3,|4},
      Jaccard = 2/4 = 0.5 ≥ 默认阈值 0.3 —— 跨域团伙应被 A105 并组;
      与全部站点集合互不相交(不同前缀的 sha 必不相同)。

    成员身份由 URL host 首标签末字符判定(gang0a → a / gang0b → b)。
    """
    label = str(label)
    if label.startswith("gang"):
        host = (urlsplit(str(url)).hostname or "").split(".")[0]
        member = "b" if host.endswith("b") else "a"
        keys = (2, 3, 4) if member == "b" else (1, 2, 3)
        return {_sha(f"{label}|{k}") for k in keys}
    return {_sha(f"{label}|{k}") for k in (0, 1, 2)}


# ---------------------------------------------------------------------------
# 分组质量指标
# ---------------------------------------------------------------------------


def evaluate(
    grouping: Iterable[Iterable[str]],
    labels: dict[str, str],
) -> dict[str, Any]:
    """对一次分组结果按真值标签打分,返回 ``{"purity", "completeness", "n_groups"}``。

    口径(详见模块 docstring;两指标均 [0, 1] 保留两位小数):

    - purity = Σ各组最大同标签数 / 总数 —— 组里混入别家标签会拉低
      (过度合并 / 误并的信号);
    - completeness = 对每个真标签,其每个成员所在组中同标签成员占比,
      按成员数加权平均 —— 混组稀释会拉低(单例拆分不扣分,欠合并不惩罚)。

    容错约定:评测宇宙 = ``labels`` 的全部 URL;分组里未知的 URL 丢弃;
      ``labels`` 中未被任何组覆盖的 URL 按**单例组**计(漏分不豁免);
      空组丢弃;``labels`` 为空 → 全 0 且 ``n_groups=0``。
    """
    truth = {str(url): str(label) for url, label in (labels or {}).items()}
    total = len(truth)
    if total == 0:
        return {"purity": 0.0, "completeness": 0.0, "n_groups": 0}

    buckets: list[list[str]] = []
    covered: set[str] = set()
    for raw in grouping or []:
        members = [str(url) for url in (raw or []) if str(url) in truth]
        if not members:
            continue
        buckets.append(members)
        covered.update(members)
    for url in sorted(set(truth) - covered):
        buckets.append([url])  # 真值宇宙内未被分组的 URL 按单例计

    majority_total = 0
    completeness_sum = 0.0
    for members in buckets:
        counts = Counter(truth[url] for url in members)
        majority_total += max(counts.values())
        completeness_sum += sum(counts[truth[url]] / len(members) for url in members)

    return {
        "purity": round(majority_total / total, 2),
        "completeness": round(completeness_sum / total, 2),
        "n_groups": len(buckets),
    }


# ---------------------------------------------------------------------------
# 真实分组链(A104 case_group + A105 group_linker,惰性导入)
# ---------------------------------------------------------------------------


@dataclass
class _PseudoEntry:
    """伪复核条目(鸭子满足 A104 EntryLike:id / site_url / verdict / evidence_zip)。"""

    id: int
    site_url: str
    verdict: str = "nsfw"
    evidence_zip: str = ""


@dataclass
class _PseudoEvidence:
    sha256: str = ""


@dataclass
class _PseudoPage:
    image_evidences: list[_PseudoEvidence] = field(default_factory=list)


@dataclass
class _PseudoReport:
    """伪站点报告:pages[].image_evidences[].sha256 供 A104 汇集 image_sha_set。"""

    site_url: str
    pages: list[_PseudoPage] = field(default_factory=list)
    agg_nsw_prob: float = 0.95


def _load_real_chain() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """惰性导入真实链 ``(group_entries, merge_groups)``;未就位抛中文错误。"""
    try:
        case_group = importlib.import_module("netsentinel.intel.case_group")
        group_linker = importlib.import_module("netsentinel.intel.group_linker")
    except Exception as exc:  # noqa: BLE001 - 守卫宽:任何异常按未就位处理
        raise GroupingBenchError(
            "真实分组链未就位:无法导入 netsentinel.intel.case_group / "
            f"netsentinel.intel.group_linker({exc});请先落地 A104/A105 模块,"
            "或经 run(..., grouper=...) 注入分组器后再跑基准"
        ) from exc
    return case_group.group_entries, group_linker.merge_groups


def _chain_params() -> tuple[float, bool]:
    """读取并组阈值/模板开关(惰性 Config;读取失败回退生产默认值)。"""
    try:
        from netsentinel.contracts import Config  # 惰性:仅真实链需要

        cfg = Config()
        return (
            float(getattr(cfg, "group_merge_phash_overlap", _DEFAULT_OVERLAP)),
            bool(getattr(cfg, "group_merge_template", _DEFAULT_MERGE_TEMPLATE)),
        )
    except Exception:  # noqa: BLE001 - 配置不可用时用与生产一致的默认值
        return _DEFAULT_OVERLAP, _DEFAULT_MERGE_TEMPLATE


def _real_chain_grouping(
    pairs: list[tuple[str, str]],
) -> tuple[list[list[str]], dict[str, Any]]:
    """把合成语料喂给真实链,返回 ``(分组, 链路元信息)``。

    链路:伪 entries(带 verdict/evidence_zip 鸭子字段)+ 伪 reports(指纹
    集合按真值标签合成,团伙对重叠、异站互斥)→ A104 canonical 镜像归并出
    基础组 → A105 无图谱仅凭指纹重叠并组(graph=None)→ 案件组的
    site_urls 即分组结果。
    """
    group_entries, merge_groups = _load_real_chain()
    overlap, merge_template = _chain_params()

    entries = [
        _PseudoEntry(id=idx, site_url=url)
        for idx, (url, _label) in enumerate(pairs, start=1)
    ]
    reports: dict[int, _PseudoReport] = {}
    for entry, (url, label) in zip(entries, pairs):
        shas = fingerprint_shas(label, url)
        reports[entry.id] = _PseudoReport(
            site_url=url,
            pages=[
                _PseudoPage(
                    image_evidences=[_PseudoEvidence(sha256=s) for s in sorted(shas)]
                )
            ],
        )

    base = list(group_entries(entries, reports))
    merged = list(merge_groups(base, graph=None, overlap_threshold=overlap, merge_template=merge_template))
    grouping = [list(getattr(group, "site_urls", []) or []) for group in merged]
    info = {
        "base_groups": len(base),
        "case_groups": len(merged),
        "overlap_threshold": overlap,
        "merge_template": merge_template,
    }
    return grouping, info


def _normalize_grouping(raw: Any) -> list[list[str]]:
    """把 grouper 返回值归一为非空 URL 二维列表(容忍 set / 生成器 / 空组)。"""
    groups: list[list[str]] = []
    for item in raw or []:
        members = [str(url) for url in (item or []) if str(url)]
        if members:
            groups.append(members)
    return groups


# ---------------------------------------------------------------------------
# 主流程与报告
# ---------------------------------------------------------------------------


def run(
    out_dir: str | Path,
    *,
    grouper: Callable[[list[tuple[str, str]]], Any] | None = None,
    n_sites: int = 12,
    mirrors_per_site: int = 2,
    seed: int = 42,
) -> dict[str, Any]:
    """跑一次分组质量基准:合成语料 → 分组 → 指标 → 写报告,返回 payload。

    - ``grouper`` 注入时签名 ``grouper(pairs) -> grouping``(list/set 的
      集合均可),用于对照实验(完美分组 / 全并一组 / 打散重排等);
    - 缺省走真实链(A104 + A105,惰性导入;未就位抛
      :class:`GroupingBenchError` 中文错误,CLI 转退出码 2);
    - 产出 ``out_dir/grouping_report.md``(中文)与
      ``out_dir/grouping_report.json``;返回写入 json 的同一份 payload。
    """
    pairs = make_synthetic(n_sites, mirrors_per_site, seed)
    labels = dict(pairs)

    if grouper is not None:
        grouping = _normalize_grouping(grouper(list(pairs)))
        chain: dict[str, Any] = {"mode": "注入 grouper(对照实验)"}
    else:
        grouping, info = _real_chain_grouping(pairs)
        chain = {"mode": "真实链(case_group.group_entries → group_linker.merge_groups)", **info}

    metrics = evaluate(grouping, labels)

    group_rows: list[dict[str, Any]] = []
    for index, members in enumerate(grouping, start=1):
        counts = Counter(labels[url] for url in members if url in labels)
        if counts:
            top_label, top_count = counts.most_common(1)[0]
            row = {
                "index": index,
                "members": len(members),
                "majority_label": top_label,
                "label_kinds": len(counts),
                "majority_share": round(top_count / len(members), 2),
            }
        else:
            row = {
                "index": index,
                "members": len(members),
                "majority_label": "",
                "label_kinds": 0,
                "majority_share": 0.0,
            }
        group_rows.append(row)

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": "grouping",
        "corpus": {
            "synthetic": True,
            "seed": seed,
            "n_sites": n_sites,
            "mirrors_per_site": mirrors_per_site,
            "gang_pairs": GANG_PAIRS,
            "n_urls": len(pairs),
            "n_labels": len(set(labels.values())),
        },
        "chain": chain,
        "eval": metrics,
        "groups": group_rows,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "grouping_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "grouping_report.md").write_text(render_markdown(payload), encoding="utf-8")
    return payload


def _fmt2(value: float) -> str:
    """报告用的两位小数格式化。"""
    return f"{float(value):.2f}"


def render_markdown(payload: dict[str, Any]) -> str:
    """把基准 payload 渲染为中文 Markdown 报告(单遍拼接,无外部依赖)。"""
    corpus = payload["corpus"]
    chain = payload["chain"]
    ev = payload["eval"]

    lines: list[str] = []
    lines.append("# NetSentinel 分组质量基准报告(A118)")
    lines.append("")
    lines.append(f"- 生成时间:{payload['generated_at']}")
    lines.append(
        f"- 语料规模:合成语料——{corpus['n_sites']} 个主站 ×(主域 + "
        f"{corpus['mirrors_per_site']} 镜像)+ {corpus['gang_pairs']} 个团伙对,"
        f"共 {corpus['n_urls']} 个 URL、{corpus['n_labels']} 个真值标签"
        f"(seed={corpus['seed']})"
    )
    if "base_groups" in chain:
        lines.append(
            f"- 分组链:{chain['mode']};基础组 {chain['base_groups']} 个 → "
            f"案件组 {chain['case_groups']} 个(无图谱,指纹重叠阈值 "
            f"{chain['overlap_threshold']},共享模板并组={'开' if chain['merge_template'] else '关'})"
        )
    else:
        lines.append(f"- 分组链:{chain['mode']}")
    lines.append(
        "- 指标口径:purity = Σ各组最大同标签数 / 总数;completeness = 成员所在组内"
        "同标签占比的加权平均;均取 [0, 1] 保留两位小数"
    )
    lines.append("")

    lines.append("## 一、指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 含义 |")
    lines.append("| --- | ---: | --- |")
    lines.append(
        f"| purity(纯度) | {_fmt2(ev['purity'])} | 组内主标签浓度:混入别家标签"
        "(过度合并 / 误并)会拉低 |"
    )
    lines.append(
        f"| completeness(完整度) | {_fmt2(ev['completeness'])} | 同标签成员所在组"
        "的同标签浓度:混组稀释会拉低;单例拆分不扣分(欠合并不惩罚) |"
    )
    lines.append(
        f"| n_groups(组数) | {ev['n_groups']} | 分组产出组数;真值标签数 = "
        f"{corpus['n_labels']}(两者接近说明分得齐) |"
    )
    lines.append("")

    lines.append("## 二、逐组明细")
    lines.append("")
    lines.append("| # | 成员数 | 主标签 | 标签种数 | 主标签占比 |")
    lines.append("| ---: | ---: | --- | ---: | ---: |")
    for row in payload["groups"]:
        lines.append(
            f"| {row['index']} | {row['members']} | {row['majority_label'] or '—'} "
            f"| {row['label_kinds']} | {_fmt2(row['majority_share'])} |"
        )
    lines.append("")

    lines.append("## 三、说明")
    lines.append("")
    lines.append(
        "- **合成语料仅验证归并逻辑**:URL 与图片指纹均由生成器程序合成"
        "(保留域 .example),不含真实站点内容,指标不可外推到真实业务分布;"
    )
    lines.append(
        "- 镜像变体(www 前缀 / 端口 8080 / 子域 m. / 路径)与主域同标签 site{i},"
        "验证 A104 canonical 镜像归并是否把同站变体分到同一组;"
    )
    lines.append(
        "- 团伙对跨可注册域(gang{j}a / gang{j}b)但共享图片指纹重叠线索"
        "(Jaccard = 0.5 ≥ 0.3),验证 A105 无图谱时能否仅凭指纹并组;"
    )
    lines.append(
        "- 主域取 site{i}.example 而非 site{i}.example.com:后者在可注册域归一 "
        "下会跨站坍缩为 example.com,与真值标签冲突(com/net/org 以路径轮换);"
    )
    lines.append(
        "- 全程离线、零外呼、零真实提交(红线 24);阈值调优与误并排查参见 "
        "docs/GROUPING.md。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _ensure_utf8_stdio() -> None:
    """Windows 控制台编码非 UTF-8 时切换标准流编码,避免中文输出报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if (
                stream is not None
                and stream.encoding
                and stream.encoding.lower() not in ("utf-8", "utf8")
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    返回码:0 成功;2 真实链未就位 / 参数非法等可预期错误(中文提示到 stderr)。
    """
    _ensure_utf8_stdio()
    default_out = str(Path(__file__).resolve().parent / "out")
    parser = argparse.ArgumentParser(
        prog="python benchmarks/grouping_bench.py",
        description=(
            "NetSentinel 分组质量基准:合成语料 × 分组链 → purity / completeness"
            "(全程离线;默认走 A104+A105 真实链,未就位以退出码 2 结束)"
        ),
    )
    parser.add_argument(
        "--out", default=default_out, help=f"报告输出目录(默认 {default_out})"
    )
    parser.add_argument("--seed", type=int, default=42, help="合成语料随机种子(默认 42)")
    parser.add_argument(
        "--n-sites", type=int, default=12, help="主站数量(默认 12)"
    )
    parser.add_argument(
        "--mirrors-per-site", type=int, default=2, help="每站镜像变体数(默认 2)"
    )
    args = parser.parse_args(argv)

    try:
        payload = run(
            args.out,
            n_sites=args.n_sites,
            mirrors_per_site=args.mirrors_per_site,
            seed=args.seed,
        )
    except (GroupingBenchError, ValueError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    corpus = payload["corpus"]
    ev = payload["eval"]
    print(
        "分组基准完成:语料 {} URL / {} 标签 → purity={} completeness={} 组数={}".format(
            corpus["n_urls"],
            corpus["n_labels"],
            _fmt2(ev["purity"]),
            _fmt2(ev["completeness"]),
            ev["n_groups"],
        )
    )
    out_dir = Path(args.out)
    print(f"报告已写出:{out_dir / 'grouping_report.md'} 与 {out_dir / 'grouping_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
