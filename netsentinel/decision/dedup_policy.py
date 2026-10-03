"""组间去重策略(A106):两组合并判定的规则化封装与举报用"主站+镜像"文本。

唯一实现依据:CONTRACTS-V6.md §3/§4 A106 条目。本模块把"两个案件组是否
合并"的判定抽成**可单独复述的规则**(每条依据配中文文案,供复核台展示与
举报包留痕),并为一组生成举报用"主站 + 镜像/关联清单"一行文本。

判定优先级(``should_merge_groups`` 先命中先返回,依据即证据):
1. 同站:``rules.same_site`` 开启且两组任一 URL 对的 canonical 可注册域相同
   (canonical 惰性导入 A103 ``intel.canonical``;未就位或解析失败时退化为
   host 全等兜底);
2. 图片指纹:两组 ``image_sha_set`` 的 Jaccard 重叠率 ≥ ``rules.phash_overlap``;
3. 检索图谱(A46)直接边 ``shared_image`` / ``phash_near``;
4. 图谱边 ``shared_template``,仅当 ``rules.template`` 开启。

约束:纯标准库、零网络、零外呼;组对象按鸭子类型访问(``site_urls`` /
``image_sha_set`` / ``aliases``,缺失字段安全降级),不导入 A104/A105 避免
并行开发的循环依赖;graph 只要求提供 ``related_sites(url)`` 查询口。

用法示例(离线)::

    from netsentinel.decision.dedup_policy import (
        DedupRules, dedup_report_url, from_config, should_merge_groups,
    )

    rules = from_config(cfg)                       # 读 group_merge_* 配置
    ok, reason = should_merge_groups(ga, gb, rules, graph=graph)
    if ok:
        print(reason)                              # 例:"同一站点(可注册域相同)"
        print(dedup_report_url(ga))                # 主站+镜像清单(≤200 字)
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DedupRules",
    "dedup_report_url",
    "from_config",
    "should_merge_groups",
]

#: 举报文本长度上限(字符数):"主站+镜像清单"须保持一行可读,超限截断加"等"。
REPORT_TEXT_LIMIT = 200

#: 图谱边种类(A46 ``intel.graph`` 约定值;此处按字面量解耦,不导入该模块)。
_IMAGE_EDGE_KINDS = frozenset({"shared_image", "phash_near"})
_TEMPLATE_EDGE_KIND = "shared_template"

#: 无镜像/关联站点时的占位文案。
_NO_MIRROR_TEXT = "无"


# ---------------------------------------------------------------------------
# 规则对象
# ---------------------------------------------------------------------------


@dataclass
class DedupRules:
    """组间去重判定规则(与 CONTRACTS-V6 §1 的三个 ``group_merge_*`` 配置对应)。

    - ``phash_overlap``:两组图片指纹集合(image_sha_set)Jaccard 重叠率的
      合并阈值,达标即视为同一团伙(默认 0.3);
    - ``template``:图谱 ``shared_template`` 边是否构成合并依据(默认开启;
      关闭可避免"同模板建站工具"造成的误并);
    - ``same_site``:同站(canonical 可注册域相同)是否直接合并(默认开启,
      仅由本策略层控制,config 无对应开关)。
    """

    phash_overlap: float = 0.3
    template: bool = True
    same_site: bool = True


def from_config(cfg: Any) -> DedupRules:
    """从 ``contracts.Config``(鸭子类型)构造规则。

    读 ``cfg.group_merge_phash_overlap`` / ``cfg.group_merge_template``;
    ``same_site`` 无配置项,保持默认开启。
    """
    return DedupRules(
        phash_overlap=float(cfg.group_merge_phash_overlap),
        template=bool(cfg.group_merge_template),
        same_site=True,
    )


# ---------------------------------------------------------------------------
# 内部工具:canonical 惰性加载与 host 提取
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _canonical_key_fn() -> Any:
    """惰性加载 A103 ``intel.canonical.canonical_key``;模块未就位返回 None。

    A103–A105 与本模块并行开发:canonical 落地前(或独立部署本模块时)
    优雅降级为 host 全等(见 :func:`_site_key`)。进程内只加载一次。
    """
    try:
        from netsentinel.intel.canonical import canonical_key  # A103,可能未就位
    except ImportError:  # 模块尚未落地:不算错误,走 host 兜底
        return None
    return canonical_key


def _host(url: str) -> str:
    """提取 URL 的 host(小写、去端口);无 scheme 的裸域名视自身为 host。"""
    if not isinstance(url, str):
        return ""
    text = url.strip()
    if not text:
        return ""
    if "//" not in text:  # "b.example.com" 之类裸域名 → urlsplit 才能取到 hostname
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:  # 形如 "https://[坏端口" 的畸形输入:降级为空
        host = None
    return (host or "").lower()


def _site_key(url: str) -> str:
    """站点归一键:优先 canonical 可注册域;未就位/解析失败退化为 host。"""
    canonical_key = _canonical_key_fn()
    if canonical_key is not None:
        try:
            key = canonical_key(url)
        except Exception:  # A103 实现异常时不阻断判定,降级 host  # pragma: no cover
            key = ""
        if key:
            return str(key).lower()
    return _host(url)


def _urls_of(group: Any) -> list[str]:
    """鸭子访问组对象的 URL 清单:优先 ``site_urls``,缺失时退回 ``aliases``。"""
    urls = getattr(group, "site_urls", None)
    if not urls:
        urls = getattr(group, "aliases", None) or ()
    return [str(u) for u in urls]


def _sha_set(group: Any) -> set[str]:
    """鸭子访问 ``image_sha_set``;缺失或 None 时视为空集(不构成指纹证据)。"""
    return set(getattr(group, "image_sha_set", None) or ())


# ---------------------------------------------------------------------------
# 判定与文案生成
# ---------------------------------------------------------------------------


def _graph_hits(
    a_urls: list[str], b_urls: set[str], b_keys: set[str], graph: Any
) -> tuple[bool, bool]:
    """在图谱中查找 a→b 的直接关联边,返回 (图片类证据命中, 模板证据命中)。

    ``related_sites`` 边为无向,只需单向往返一侧;命中按"精确 URL 相同或
    站点归一键相同"匹配,容忍尾部斜杠等记录差异。graph 为 None、查询口
    缺失或抛错均按无证据处理(安全方向:宁可漏并,交由其他依据兜底)。
    """
    if graph is None:
        return False, False
    image_hit = False
    template_hit = False
    for url in dict.fromkeys(a_urls):  # 去重保序,避免同 URL 重复查询
        try:
            related = graph.related_sites(url)
        except Exception:
            continue
        for item in related or ():
            if not isinstance(item, dict):
                continue
            site = item.get("site")
            if not site:
                continue
            if site not in b_urls and _site_key(str(site)) not in b_keys:
                continue
            via = item.get("via") or ()
            if any(kind in via for kind in _IMAGE_EDGE_KINDS):
                image_hit = True
            if _TEMPLATE_EDGE_KIND in via:
                template_hit = True
    return image_hit, template_hit


def should_merge_groups(
    a: Any, b: Any, rules: DedupRules, *, graph: Any = None
) -> tuple[bool, str]:
    """判定两个案件组是否应合并;返回 ``(是否合并, 中文依据)``。

    组对象鸭子访问 ``site_urls`` / ``image_sha_set``(A104 CaseGroup 或任何
    同形对象)。依据按优先级先命中先返回:

    1. ``rules.same_site`` 且存在 URL 对 canonical 同 → "同一站点(可注册域相同)";
    2. 指纹 Jaccard ≥ ``rules.phash_overlap`` → "图片指纹重叠 {百分比}";
    3. 图谱 shared_image / phash_near 边 → "共享图片证据";
    4. 图谱 shared_template 边且 ``rules.template`` → "共享页面模板";

    全部不命中返回 ``(False, "")``。判定纯函数、零网络。
    """
    a_urls = _urls_of(a)
    b_urls = _urls_of(b)

    # 1) 同站(canonical 同;未就位按 host 全等)
    if rules.same_site and a_urls and b_urls:
        b_keys = {_site_key(u) for u in b_urls}
        if any(_site_key(u) in b_keys for u in a_urls):
            return True, "同一站点(可注册域相同)"

    # 2) 图片指纹集合重叠率(Jaccard);两组指纹均为空时不构成证据(0/0 不判并)
    shas_a = _sha_set(a)
    shas_b = _sha_set(b)
    union = shas_a | shas_b
    if union:
        overlap = len(shas_a & shas_b) / len(union)
        if overlap >= rules.phash_overlap:
            return True, f"图片指纹重叠 {overlap:.0%}"

    # 3)/4) 检索图谱直接证据
    image_hit, template_hit = _graph_hits(
        a_urls, set(b_urls), {_site_key(u) for u in b_urls}, graph
    )
    if image_hit:
        return True, "共享图片证据"
    if template_hit and rules.template:
        return True, "共享页面模板"
    return False, ""


def dedup_report_url(group: Any) -> str:
    """生成举报用"主站+镜像/关联清单"一行文本(≤200 字)。

    - 主站:第一个 ``site_urls``(整条 URL 原样);组无任何 URL 时退回
      ``aliases`` 首项,再退回组名;
    - 镜像/关联:其余 site_urls 与全部 aliases 提取 host 后**去重**(保留
      首次出现顺序、剔除主站自身 host)以中文逗号拼接;一个都没有时写"无";
    - 总长超过 :data:`REPORT_TEXT_LIMIT` 字符时截断到上限并补"等"。

    纯函数、零网络;文案直接进入举报材料,保持单行可复制。
    """
    urls = _urls_of(group)
    main = urls[0] if urls else str(getattr(group, "name", "") or "")

    seen_hosts = {_host(main)}
    mirrors: list[str] = []
    for url in urls[1:]:
        host = _host(url)
        if host and host not in seen_hosts:
            seen_hosts.add(host)
            mirrors.append(host)
    for alias in getattr(group, "aliases", None) or ():
        host = _host(str(alias))
        if host and host not in seen_hosts:
            seen_hosts.add(host)
            mirrors.append(host)

    mirror_text = ",".join(mirrors) if mirrors else _NO_MIRROR_TEXT
    text = f"主站:{main};镜像/关联:{mirror_text}"
    if len(text) > REPORT_TEXT_LIMIT:  # 截断到恰好 200 字并以"等"收尾
        text = text[: REPORT_TEXT_LIMIT - 1] + "等"
    return text
