"""净网哨兵 · 分组复核页(A115,独立入口,不改动既有 webui 页面)。

定位:V6 批量案件流水线的**分组批量视图**——把 A104 归并出的案件组摆上
复核台:分组总表 / 组详情(逐站人工核验)/ 批量确认声明(红线 25 留痕)/
批量队列预览。与人工复核台(app.py,逐条拍板)、运营仪表盘(dashboard.py,
趋势治理)、提供方面板(providers_page.py)互补,四个页面各自独立入口。

安全红线(必须体现在代码里):
- **红线 24(页面不含真实提交)**:本页面没有任何提交流程——批量队列
  页签只做只读预览,并以醒目红条提示"请用 batch_tui 声明后经 CLI --exec
  逐条人工门执行";每条举报的验证码输入与最终确认仍由人工在执行器
  HUMAN_GATE 完成;
- **红线 25(批量确认声明留痕)**:声明按钮须先勾选"我已逐站人工核实",
  再调 ``BatchReview.attest`` 落库留痕(组名/条数/审核人/声明文本);
  无声明的组不进入批量队列(A110 ``ready_entries`` 门控);
- **红线 26(频控不放宽)**:批量队列预览文案如实展示
  ``batch_item_interval_s`` 与 ``batch_max_items``,提示频控与每日额度
  在批量模式下照常生效;
- 本页面零网络行为:数据只来自本地 SQLite 复核队列 / 声明台账与证据包
  manifest,不发起任何外呼。

结构约定(与 tests/test_groups_page.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、无兄弟模块顶层依赖,可独立导入):
  * :func:`group_rows`     案件组列表 → 总表行(组名/条目数/站点数/别名
    域数/agg 两位小数/判定中文/是否已声明);
  * :func:`attest_badge`   布尔 → 中文声明徽章("✓ 已声明"/"✗ 未声明");
  * :func:`batch_preview`  待批量条目 + 配置 → 中文一行批量预览(含频控
    数字);
  * :func:`portal_cn`      门户标识 → 中文名("shdf" → "扫黄打非")。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 三页签:①分组总表 ②组详情(核验+声明)③批量队列
    (只预览,不提交)。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any

from netsentinel import telemetry

# ---------------------------------------------------------------------------
# streamlit 惰性导入:缺失时纯逻辑层仍可被测试导入(UI 在 main() 里拦截)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    import streamlit as st

    _HAS_ST = True
except ImportError:  # pragma: no cover - 取决于运行环境
    st = None  # type: ignore[assignment]
    _HAS_ST = False

__all__ = [
    "VERDICT_CN",
    "PORTAL_CN",
    "BADGE_ATTESTED",
    "BADGE_UNATTESTED",
    "group_rows",
    "attest_badge",
    "batch_preview",
    "portal_cn",
    "render",
    "main",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 判定档位 → 中文(与 netsentinel.__main__ 的 _VERDICT_CN 同口径)。
VERDICT_CN: dict[str, str] = {
    "clean": "无风险",
    "suspect": "疑似",
    "nsfw": "高置信色情",
}

#: 举报门户标识 → 中文展示名(12377 本身即号码,原样展示)。
PORTAL_CN: dict[str, str] = {
    "12377": "12377",
    "shdf": "扫黄打非",
}

#: 声明徽章文案(已声明 / 未声明)。
BADGE_ATTESTED: str = "✓ 已声明"
BADGE_UNATTESTED: str = "✗ 未声明"

#: batch_item_interval_s 缺失 / 非法时的展示兜底(与 Config 默认一致)。
_DEFAULT_INTERVAL_S: int = 90

#: batch_max_items 缺失 / 非法时的展示兜底(与 Config 默认一致)。
_DEFAULT_MAX_ITEMS: int = 20

#: 数据目录覆盖环境变量(与 webui/app.py / dashboard.py 同名约定,默认 ./data)。
DATA_DIR_ENV: str = "NETSENTINEL_DATA_DIR"


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _count(seq: Any) -> int:
    """容错计数:``len(seq)``;None / 无长度概念的一律按 0,不抛错。"""
    try:
        return len(seq)
    except TypeError:
        return 0


def _verdict_cn(value: Any) -> str:
    """判定值(Verdict 枚举 / 字符串 / 缺失)→ 中文档位名。

    已知档位(clean/suspect/nsfw)映射 :data:`VERDICT_CN`;未知档位原样
    展示(保留排查线索);完全缺失按"未知"。
    """
    raw = getattr(value, "value", value)  # 兼容 Verdict(str, Enum) 实例
    text = str(raw or "").strip()
    return VERDICT_CN.get(text.lower()) or text or "未知"


# ---------------------------------------------------------------------------
# group_rows:案件组列表 → 总表行
# ---------------------------------------------------------------------------


def group_rows(groups: list | None, attested: set[str] | None = None) -> list[dict]:
    """把案件组(:class:`~netsentinel.intel.case_group.CaseGroup` 鸭子)转成总表行。

    - 每行固定七键 ``{name, entries, sites, aliases, agg_max, verdict,
      attested}``:
      * ``name``:组名(canonical 主名);
      * ``entries`` / ``sites`` / ``aliases``:条目数 ``len(entry_ids)`` /
        站点数 ``len(site_urls)`` / 别名域数 ``len(aliases)``;
      * ``agg_max``:组内最高风险分,**四舍五入两位小数**(非法值按 0.0);
      * ``verdict``:判定中文(:data:`VERDICT_CN`,未知原样,缺失"未知");
      * ``attested``:组名是否出现在 ``attested`` 集合中(批量确认声明,
        红线 25);``attested`` 为 ``None`` / 空集时恒为 ``False``。
    - CaseGroup 为鸭子类型:getattr / dict 双路容错,缺失属性按空值处理,
      绝不抛错;``groups`` 为 ``None`` / 空列表返回 ``[]``。
    - 保持输入顺序(排序由上游 ``group_entries`` 的 sort_key 决定)。

    示例::

        >>> group_rows([CaseGroup(name="a.com", entry_ids=[1, 2],
        ...                       site_urls=["https://a.com/"],
        ...                       aliases=["a.com", "www.a.com"],
        ...                       agg_max=0.876, verdict="nsfw")])
        [{'name': 'a.com', 'entries': 2, 'sites': 1, 'aliases': 2,
          'agg_max': 0.88, 'verdict': '高置信色情', 'attested': False}]
    """
    names = attested or set()
    rows: list[dict] = []
    for group in groups or []:
        name = str(_field(group, "name", "") or "").strip()
        agg_raw = _field(group, "agg_max", 0.0)
        try:
            agg = round(float(agg_raw), 2)
        except (TypeError, ValueError):
            agg = 0.0
        rows.append(
            {
                "name": name,
                "entries": _count(_field(group, "entry_ids", [])),
                "sites": _count(_field(group, "site_urls", [])),
                "aliases": _count(_field(group, "aliases", [])),
                "agg_max": agg,
                "verdict": _verdict_cn(_field(group, "verdict", "")),
                "attested": bool(name) and name in names,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# attest_badge:声明状态 → 中文徽章
# ---------------------------------------------------------------------------


def attest_badge(ok: Any) -> str:
    """布尔声明状态 → 中文徽章:真 → "✓ 已声明",假 → "✗ 未声明"。

    示例::

        >>> attest_badge(True)
        '✓ 已声明'
        >>> attest_badge(0)
        '✗ 未声明'
    """
    return BADGE_ATTESTED if bool(ok) else BADGE_UNATTESTED


# ---------------------------------------------------------------------------
# batch_preview:待批量条目 → 中文一行预览(含频控数字,红线 26)
# ---------------------------------------------------------------------------


def batch_preview(items: list | None, cfg: Any) -> str:
    """把待批量条目与配置合成中文一行批量预览(只读文案,不含任何提交动作)。

    输出形如::

        将依次提交 3 条,间隔 ≥90s,每条需人工输入验证码;单批上限 20

    - 条数 ``N = len(items)``(``None`` / 非列表按 0);
    - 间隔取 ``cfg.batch_item_interval_s``、上限取 ``cfg.batch_max_items``
      (鸭子容错:对象 / dict 均可,缺失 / 非法回退 90 / 20 默认值);
    - "每条需人工输入验证码"是红线 24 的固定提示——批量只是顺序编排,
      每一条的验证码与最终确认仍由人工完成。

    每次预览计入 ``telemetry.inc("groups_page.batch_preview")``(V5 起
    可观测性约定)。
    """
    try:
        interval = int(_field(cfg, "batch_item_interval_s", _DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        interval = _DEFAULT_INTERVAL_S
    try:
        max_items = int(_field(cfg, "batch_max_items", _DEFAULT_MAX_ITEMS))
    except (TypeError, ValueError):
        max_items = _DEFAULT_MAX_ITEMS
    telemetry.inc("groups_page.batch_preview")
    return (
        f"将依次提交 {_count(items)} 条,间隔 ≥{interval}s,"
        f"每条需人工输入验证码;单批上限 {max_items}"
    )


# ---------------------------------------------------------------------------
# portal_cn:门户标识 → 中文名
# ---------------------------------------------------------------------------


def portal_cn(portal: Any) -> str:
    """举报门户标识 → 中文展示名:"shdf" → "扫黄打非","12377" 原样。

    未知 / 空标识原样返回(排查线索不丢);仅做查表,不触碰门户模块。

    示例::

        >>> portal_cn("shdf")
        '扫黄打非'
        >>> portal_cn("12377")
        '12377'
    """
    text = str(portal or "").strip()
    return PORTAL_CN.get(text, text)


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内惰性导入)
# ===========================================================================

_PAGE_TITLE = "案件分组 · 批量复核"
_MOTTO = (
    "分组批量视图:案件组总表 · 组详情核验 · 批量声明留痕 · 队列预览;"
    "本页面不执行任何举报提交(红线 24)。"
)

#: 红线 24 的醒目提示(批量队列页签顶部红条,文案固定)。
_NO_SUBMIT_NOTICE: str = (
    "本页面不执行提交:请用 batch_tui 声明后经 CLI --exec 逐条人工门执行"
)

#: 声明确认勾选框文案(红线 25:勾选 = 自述已逐站人工核验)。
_CONFIRM_LABEL = "我已逐站人工核实"

#: 声明按钮落库的声明文本(含 A110 要求的"人工核实"四字)。
_ATTEST_TEXT = "我已逐站人工核实该组全部站点 URL 与证据包,同意进入批量举报队列"

#: 重建分组时最多回读证据包 manifest 的条数(最近优先,IO 保护)。
_MAX_MANIFEST_ENTRIES: int = 100


def _agg_proxies(db_path: str, entries: list) -> dict[int, Any]:
    """从证据包 manifest 回读各条目的 agg_nsw_prob,包装成 group_entries
    可用的 report 鸭子(``SimpleNamespace(agg_nsw_prob=…)``)。

    manifest 缺失 / 无 agg / 数值非法的条目不提供代理(该条目不贡献组内
    最高分);全部失败时返回空映射,分组退化为纯条目归并,页面不崩。
    """
    from webui.app import load_report_dict  # 兄弟模块函数内导入

    proxies: dict[int, Any] = {}
    for entry in entries[-_MAX_MANIFEST_ENTRIES:]:
        zip_path = str(_field(entry, "evidence_zip", "") or "")
        if not zip_path:
            continue
        manifest = load_report_dict(zip_path)
        agg = manifest.get("agg_nsw_prob") if isinstance(manifest, dict) else None
        if isinstance(agg, bool) or not isinstance(agg, (int, float)):
            continue
        proxies[int(_field(entry, "id", 0) or 0)] = SimpleNamespace(
            agg_nsw_prob=float(agg)
        )
    return proxies


def _load_groups(db_path: str) -> tuple[list, list]:
    """从本地复核队列重建案件分组;返回 ``(groups, entries)``。

    复用 A104 ``group_entries``(canonical 归并)与 A30
    ``load_report_dict``(manifest 定位),全部惰性导入;调用方负责兜底
    异常(库文件缺失 / 兄弟模块未就位时页面显示中文提示而非崩溃)。
    """
    from netsentinel.decision.review_queue import ReviewQueue  # 兄弟模块函数内导入
    from netsentinel.intel.case_group import group_entries  # 兄弟模块函数内导入

    entries = ReviewQueue(db_path).list(None)
    groups = group_entries(entries, _agg_proxies(db_path, entries))
    return groups, entries


def _safe_load_groups(db_path: str) -> tuple[list, list]:
    """_load_groups 的兜底包装:任何异常 → ([], []) + 中文提示。"""
    try:
        return _load_groups(db_path)
    except Exception as exc:  # noqa: BLE001 - 库缺失/模块未就位不让页面崩溃
        st.warning(f"读取复核队列或重建案件分组失败:{exc}")
        return [], []


def _open_review(db_path: str) -> Any:
    """惰性打开批量声明台账(A110 BatchReview);未就位 / 打开失败返回 None。"""
    try:
        from netsentinel.decision.batch_review import BatchReview  # 兄弟模块函数内导入
    except Exception as exc:  # noqa: BLE001 - A110 允许缺席(并行开发期)
        st.warning(f"批量声明台账模块(netsentinel.decision.batch_review)未就位:{exc}")
        return None
    try:
        return BatchReview(db_path)
    except Exception as exc:  # noqa: BLE001 - 库文件异常不让页面崩溃
        st.warning(f"打开批量声明台账失败:{exc}")
        return None


def _attested_names(review: Any) -> set[str]:
    """已声明组名集合;台账不可用 / 暂无声明时返回空集。"""
    if review is None:
        return set()
    try:
        return {a.group_name for a in review.list_attestations()}
    except Exception:  # noqa: BLE001 - 读不到声明按"全部未声明"处理
        return set()


def _render_overview(groups: list, attested: set[str]) -> None:
    """页签①分组总表:规模指标卡 + group_rows 总表(含声明徽章)。"""
    st.subheader("分组总表")
    rows = group_rows(groups, attested)
    if not rows:
        st.info("复核队列暂无条目,尚未形成任何案件分组。")
        return
    n_attested = sum(1 for r in rows if r["attested"])
    cols = st.columns(4)
    cols[0].metric("案件组", len(rows))
    cols[1].metric("已声明组", n_attested)
    cols[2].metric("未声明组", len(rows) - n_attested)
    cols[3].metric("涉及条目", sum(r["entries"] for r in rows))
    st.table(
        [
            {
                "组名": r["name"] or "(未命名)",
                "条目数": r["entries"],
                "站点数": r["sites"],
                "别名域数": r["aliases"],
                "最高风险分": f"{r['agg_max']:.2f}",
                "判定": r["verdict"],
                "声明状态": attest_badge(r["attested"]),
            }
            for r in rows
        ]
    )
    st.caption(
        "分组口径:同 canonical 可注册域(含 www / 子域 / 端口变体)→ 同一案件组;"
        "最高风险分取组内各条目 agg 的最大值;行序按(判定档, 风险分, 组规模)降序;"
        "未声明(✗)的组不得进入批量队列(红线 25)。"
    )


def _render_detail(groups: list, entries: list, review: Any) -> None:
    """页签②组详情:选组 → URL/别名/证据包清单(人工核验)→ 声明按钮。"""
    st.subheader("组详情与批量声明")
    if not groups:
        st.info("暂无案件分组,请先在「分组总表」确认分组已生成。")
        return
    options = {str(g.name or "(未命名)"): g for g in groups}
    label = st.selectbox("选择案件组", list(options))
    group = options[label]
    entry_ids = list(_field(group, "entry_ids", []) or [])
    site_urls = list(_field(group, "site_urls", []) or [])
    aliases = list(_field(group, "aliases", []) or [])

    st.markdown(f"**站点 URL({len(site_urls)} 条)**")
    for url in site_urls:
        st.markdown(f"- `{url}`")
    st.caption(
        f"别名域({len(aliases)} 个):{('、'.join(aliases)) if aliases else '(无)'}"
    )

    evidence_map = {
        int(_field(e, "id", 0) or 0): str(_field(e, "evidence_zip", "") or "")
        for e in entries
    }
    st.markdown(f"**证据包路径({len(entry_ids)} 条,请逐站人工核验)**")
    for eid in entry_ids:
        st.markdown(f"- 条目 #{eid}:`{evidence_map.get(eid) or '(尚未打包)'}`")
    st.caption(
        "请逐条打开证据包,人工核验 URL 与页面截图是否对应、证据是否真实完整;"
        "核验完成前不得声明,更不得进入批量队列(红线 24/25)。"
    )

    if review is None:
        st.warning("批量声明台账不可用,无法在本页完成声明;可先用 batch_tui 完成。")
        return

    # ---- 批量确认声明(红线 25:勾选自述 + 落库留痕)----
    st.divider()
    st.markdown("**批量确认声明(留痕)**")
    reviewer = st.text_input("审核人姓名", value="", key=f"gp_reviewer_{label}")
    confirmed = st.checkbox(
        _CONFIRM_LABEL,
        key=f"gp_confirm_{label}",
        help="勾选即自述:已逐站人工核验本组全部 URL 与证据包(声明文本将入台账留痕)。",
    )
    if st.button(
        "写入批量声明(留痕)",
        key=f"gp_attest_{label}",
        type="primary",
        disabled=not confirmed,
    ):
        name = reviewer.strip()
        if not name:
            st.warning("请填写审核人姓名(每份批量声明必须留痕审核人)。")
        else:
            try:
                att = review.attest(
                    label, items=len(entry_ids), reviewer=name, text=_ATTEST_TEXT
                )
            except ValueError as exc:
                st.warning(f"{exc}")
            else:
                st.success(
                    f"声明已留痕:组「{att.group_name}」/ {att.items} 条 /"
                    f" 审核人 {att.reviewer} / {att.ts};本页面仍不执行任何提交。"
                )
    st.caption(
        "声明只完成核验留痕(组名/条数/审核人入台账);举报提交须在 CLI/TUI "
        "经 --exec 逐条人工门执行(红线 24)。"
    )


def _load_ready(cfg: Any, db_path: str) -> list[dict] | None:
    """取待批量清单(A110 ready_entries,红线 25 门控);失败返回 None。"""
    try:
        from netsentinel.decision.batch_review import (  # 兄弟模块函数内导入
            BatchReview,
            ready_entries,
        )
        from netsentinel.decision.review_queue import ReviewQueue  # 兄弟模块函数内导入
    except Exception as exc:  # noqa: BLE001 - A110 允许缺席(并行开发期)
        st.warning(f"批量复核模块未就位,无法生成待批量清单:{exc}")
        return None
    queue = ReviewQueue(db_path)
    review = BatchReview(db_path)
    try:
        items = ready_entries(cfg, queue=queue, review=review)
    except Exception as exc:  # noqa: BLE001 - 清单失败不让页面崩溃
        st.warning(f"生成待批量清单失败:{exc}")
        return None
    finally:
        # 只关闭本函数自己构造的实例(与 A110 的注入口径一致)。
        queue.close()
        review.close()
    return list(items or [])


def _render_batch(cfg: Any, db_path: str) -> None:
    """页签③批量队列:只读预览,绝不执行提交(红线 24 醒目红条)。"""
    st.subheader("批量队列(只预览,不提交)")
    st.error(_NO_SUBMIT_NOTICE)  # 红线 24:页面不含真实提交的固定醒目提示
    items = _load_ready(cfg, db_path)
    if items is None:
        return
    st.info(batch_preview(items, cfg))
    if not items:
        st.info(
            "当前没有待批量条目:需条目已在复核台批准,且其所在案件组完成批量"
            "确认声明(「组详情」页签或 batch_tui attest)后才会出现在此清单。"
        )
    else:
        st.table(
            [
                {
                    "序号": i,
                    "条目号": str(item.get("entry_id", "?")),
                    "组名": str(item.get("group_name", "?")),
                    "站点": "、".join(
                        str(u)
                        for u in (
                            item.get("site_urls")
                            or ([item["site_url"]] if item.get("site_url") else [])
                        )
                    )
                    or "-",
                    "证据包": str(item.get("evidence_zip", "") or "-"),
                    "门户": portal_cn(item.get("portal", "12377")),
                }
                for i, item in enumerate(items, start=1)
            ]
        )
    st.caption(
        "批量频控不因批量模式放宽(红线 26):批内两次提交间隔 ≥ "
        "batch_item_interval_s(且不低于 submit_min_interval_s),每日上限 "
        "submit_max_per_day 照常生效,额度用尽自动挂起、可续批;"
        "每一条提交的验证码输入与最终确认均由人工在执行器 HUMAN_GATE 完成。"
    )


def render() -> None:
    """分组复核页主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染分组复核页;请先安装 python -m pip install -e \".[ui]\""
        )

    from pathlib import Path

    from netsentinel.config import load_config  # 兄弟模块函数内导入

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🗂️", layout="wide")
    st.title(f"🗂️ {_PAGE_TITLE}")
    st.caption(_MOTTO)

    cfg = load_config()
    with st.sidebar:
        st.header("运行参数")
        root = st.text_input(
            "数据根目录",
            value=os.environ.get(DATA_DIR_ENV, "data"),
            help=f"初值取环境变量 {DATA_DIR_ENV}(默认 ./data);复核队列与声明台账从该目录读取。",
        )
        db_path = str(Path(root) / "review_queue.db")
        st.caption(f"- 复核队列 / 声明台账:`{db_path}`")
        st.divider()
        st.caption("本页面只做分组查看、批量声明留痕与队列预览,不执行任何举报提交(红线 24)。")

    groups, entries = _safe_load_groups(db_path)
    review = _open_review(db_path)
    attested = _attested_names(review)

    tab_all, tab_detail, tab_batch = st.tabs(["分组总表", "组详情", "批量队列"])
    with tab_all:
        _render_overview(groups, attested)
    with tab_detail:
        _render_detail(groups, entries, review)
    with tab_batch:
        _render_batch(cfg, db_path)


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,分组复核页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/groups_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
