"""净网哨兵 · 复核台"最近运行"页(A175,独立入口,不改动既有 app.py /
dashboard.py / providers_page.py / groups_page.py / models_page.py)。

定位:面向**复核员/运营者**的"最近批次收官汇总 + 本机 CPU 建议"只读视图
——最近运行汇总表(批次 / 站点 / 判定分布中文串 / 案件组数 / 档位徽章 /
就绪条目 / 生成时间)、CPU 建议卡(A163 ``detect`` 惰性探测,仅点击后)、
以及"汇总不触发任何提交"的红线声明。与收官 CLI
``python -m netsentinel.finishflow`` 互补:本页**只读展示**,一切举报仍走
"逐组声明 → 逐条人工门"的人工链路。

结构约定(与 tests/test_runs_page.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、无兄弟模块顶层依赖,可独立导入):
  * :func:`run_rows`   最近运行汇总 list → 表行(批次/站点/判定分布中文串/
    组数/档位徽章/就绪/生成时间;入参鸭子容错,坏行跳过不抛错);
  * :func:`tier_badge` 档位 → 中文徽章(低🟢 中🟡 高🔴,未知❓);
  * :func:`cpu_advice` CPU 画像 → 中文建议(≤2 核低档 / ≤8 核中档 /
    >8 核高档压榨本地计算,对外频控不变);
  * :func:`advice_tier` CPU 画像 → 建议档位名(low/mid/high,同阈值);
  * :func:`dist_line`  判定分布 dict → 中文一行串("未发现 2 / 疑似 1 /
    高置信 3";固定三档顺序,未知档位原样保留)。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 三区:①最近运行汇总表(tier_badge 列)②CPU 建议卡
    (A163 detect 惰性,仅点击"检测"后探测)③红线说明(汇总不触发任何提交)。

安全红线(必须体现在代码里):
- **红线 36(结案代理无自主提交权)**:本页任何位置都不提供提交流程,
  固定声明"汇总不触发任何提交";举报须逐组声明(A111 batch_tui)→
  逐条人工门(A112 顺序批量链)完成;
- **红线 35(压榨边界)**:CPU 建议卡的高档文案如实标注"只压榨本地计算,
  对外频控不变"——礼貌间隔 / 引擎限速 / 举报频控一概不放宽;
- 本页零网络行为:汇总数据只来自本地 ``data/runs`` 目录下的 JSON 文件
  (收官流程落盘后才有),CPU 画像仅本地探测(A163 detect 零外呼)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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
    "ADVICE_HIGH",
    "ADVICE_LOW",
    "ADVICE_MID",
    "BADGE_HIGH",
    "BADGE_LOW",
    "BADGE_MID",
    "BADGE_UNKNOWN",
    "MAX_RUNS_SHOWN",
    "VERDICT_LABELS_CN",
    "advice_tier",
    "cpu_advice",
    "dist_line",
    "main",
    "render",
    "run_rows",
    "tier_badge",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 三档并发档位徽章(与 CONTRACTS-V9 §1 三档一一对应;图标色 = 资源占用)。
BADGE_LOW: str = "🟢 低(后台)"
BADGE_MID: str = "🟡 中(默认)"
BADGE_HIGH: str = "🔴 高(全压榨)"

#: 未知档位徽章(空串 / 拼写错 / 非字符串一律落到这里,保留排查线索)。
BADGE_UNKNOWN: str = "❓ 未知档位"

#: 判定档位 → 中文标签(A175 口径:未发现/疑似/高置信)。
VERDICT_LABELS_CN: dict[str, str] = {
    "clean": "未发现",
    "suspect": "疑似",
    "nsfw": "高置信",
}

#: 判定分布的固定展示顺序(clean → suspect → nsfw,由轻到重;与 A117
#: ``group_stats.verdict_dist`` 三键恒在的口径对齐,0 也如实展示)。
_VERDICT_ORDER: tuple[str, ...] = ("clean", "suspect", "nsfw")

#: 判定分布缺省文案(空分布 / 坏输入;与 A168 最小稿口径一致)。
_DIST_EMPTY: str = "无数据"

#: 三档 CPU 建议(与 A163 ``recommend`` 的核数阈值同口径:≤2→low;≤8→mid;
#: >8→high;高档只压榨本地计算,红线 35)。
ADVICE_LOW: str = "核数较少,建议低档避免卡顿"
ADVICE_MID: str = "中档均衡"
ADVICE_HIGH: str = "可开高档压榨本地计算(对外频控不变)"

#: 建议换算的缺省核数(画像缺失 / 坏输入按 2 容错;与 A163 DEFAULT_CORES 同值,
#: 本页独立声明避免顶层耦合)。
_DEFAULT_CORES: int = 2

#: 最近运行汇总表最多展示的批次数(其余仍留在 data/runs,不在页面展开)。
MAX_RUNS_SHOWN: int = 20

#: run_rows 每行的固定键(顺序即展示顺序;不含任何证据 / 密钥 / 站点内容)。
_ROW_KEYS: tuple[str, ...] = (
    "batch",
    "sites",
    "verdict_cn",
    "groups",
    "tier_badge",
    "ready",
    "generated_at",
)


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _clean_str(value: Any) -> str:
    """规整为去空白字符串(非字符串 / None → 空串)。"""
    return str(value).strip() if isinstance(value, str) else ""


def _as_int(value: Any, default: int = 0) -> int:
    """宽容整数转换(duck 值可能是 None / 浮点 / 字符串;折不动回默认)。"""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _count(value: Any) -> int:
    """"组数"类字段宽容计数:数值直取,列表/集合/字典取长度,其余 0。

    字符串 / bytes 一律按 0(文本无计数语义,如 ``"很多"`` 长度无意义)。
    """
    if value is None or isinstance(value, (str, bytes)):
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return len(value)
    except TypeError:
        return 0


# ---------------------------------------------------------------------------
# tier_badge:档位 → 中文徽章
# ---------------------------------------------------------------------------

def tier_badge(tier: Any) -> str:
    """并发档位 → 中文徽章(三态 + 未知兜底)。

    - ``low`` → :data:`BADGE_LOW`("🟢 低(后台)");
    - ``mid`` → :data:`BADGE_MID`("🟡 中(默认)");
    - ``high`` → :data:`BADGE_HIGH`("🔴 高(全压榨)");
    - 其余(空串 / 拼写错 / ``None`` / 非字符串 / 未知枚举)→
      :data:`BADGE_UNKNOWN`("❓ 未知档位"),原值不进徽章。

    容错:前后空白与大小写规整后匹配(``" High "`` 同 ``"high"``);
    枚举实例先取 ``.value`` 再判断。

    示例::

        >>> tier_badge("low"), tier_badge("high"), tier_badge("turbo")
        ('🟢 低(后台)', '🔴 高(全压榨)', '❓ 未知档位')
    """
    raw = getattr(tier, "value", tier)  # 兼容 str 枚举实例
    text = _clean_str(raw).lower()
    if text == "low":
        return BADGE_LOW
    if text == "mid":
        return BADGE_MID
    if text == "high":
        return BADGE_HIGH
    return BADGE_UNKNOWN


# ---------------------------------------------------------------------------
# cpu_advice:CPU 画像 → 中文建议
# ---------------------------------------------------------------------------

def _coerce_cores(value: Any) -> int:
    """把任意输入宽容地折成 ≥1 的核数;折不动按 :data:`_DEFAULT_CORES`。"""
    n = _as_int(value, 0)
    return n if n >= 1 else _DEFAULT_CORES


def cpu_advice(profile: Any) -> str:
    """依 CPU 画像给中文并发档位建议(与 A163 ``recommend`` 核数阈值同口径)。

    规则(核数取向保守,只给文案不做任何换算):

    - cores ≤ 2 → :data:`ADVICE_LOW`("核数较少,建议低档避免卡顿");
    - cores ≤ 8 → :data:`ADVICE_MID`("中档均衡");
    - cores > 8 → :data:`ADVICE_HIGH`("可开高档压榨本地计算(对外频控不变)"
      "——红线 35:高档只加速本地计算,礼貌间隔 / 举报频控一概不放宽)。

    容错:``profile`` 为 ``None`` / 非字典 / 缺 ``cores`` / 非整数 / <1 一律
    按 cores=2 处理(保守落到低档建议);鸭子对象(带 ``cores`` 属性)兼容。

    示例::

        >>> cpu_advice({"cores": 16})
        '可开高档压榨本地计算(对外频控不变)'
        >>> cpu_advice({})
        '核数较少,建议低档避免卡顿'
    """
    raw: Any = None
    if profile is not None:
        try:
            raw = _field(profile, "cores", None)
        except Exception:  # noqa: BLE001 - 奇异对象的 __getattr__ 抛错按无画像
            raw = None
    cores = _coerce_cores(raw)
    if cores <= 2:
        return ADVICE_LOW
    if cores <= 8:
        return ADVICE_MID
    return ADVICE_HIGH


def advice_tier(profile: Any) -> str:
    """CPU 画像 → 建议档位名(供徽章展示;与 :func:`cpu_advice` 同阈值)。

    纯本地换算:cores ≤ 2 → ``low``;≤ 8 → ``mid``;> 8 → ``high``
    (与 A163 ``recommend`` 的核数基础档一致,不采样系统占用)。
    坏画像按 cores=2 容错(→ ``low``)。文案版建议走 :func:`cpu_advice`,
    徽章版走 ``tier_badge(advice_tier(profile))``。

    示例::

        >>> advice_tier({"cores": 16}), advice_tier(None)
        ('high', 'low')
    """
    raw: Any = None
    if profile is not None:
        try:
            raw = _field(profile, "cores", None)
        except Exception:  # noqa: BLE001 - 奇异对象按无画像
            raw = None
    cores = _coerce_cores(raw)
    if cores <= 2:
        return "low"
    if cores <= 8:
        return "mid"
    return "high"


# ---------------------------------------------------------------------------
# dist_line:判定分布 → 中文一行串
# ---------------------------------------------------------------------------

def dist_line(verdict_dist: Any) -> str:
    """判定分布 dict → 中文一行串("未发现 2 / 疑似 1 / 高置信 3")。

    - 已知三档(clean / suspect / nsfw)映射 :data:`VERDICT_LABELS_CN`
      (未发现 / 疑似 / 高置信),并**固定按由轻到重的顺序**输出——与入参
      键序无关;计数为 0 也如实展示(A117 ``verdict_dist`` 三键恒在口径);
    - 未知档位键保留原样文本追加在已知三档之后(不丢弃排查线索);
    - 空分布 / ``None`` / 非字典(折不成 dict)→ :data:`_DIST_EMPTY`
      ("无数据");计数折不动的按 0。

    示例::

        >>> dist_line({"clean": 2, "suspect": 1, "nsfw": 3})
        '未发现 2 / 疑似 1 / 高置信 3'
        >>> dist_line({"nsfw": 1})
        '高置信 1'
    """
    try:
        items = dict(verdict_dist).items()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DIST_EMPTY
    counts: dict[str, int] = {}
    for key, value in items:
        text = _clean_str(key).lower() if isinstance(key, str) else str(key).strip()
        if not text:
            continue
        counts[text] = _as_int(value, 0)
    if not counts:
        return _DIST_EMPTY
    parts: list[str] = []
    for known in _VERDICT_ORDER:  # 已知三档固定顺序优先
        if known in counts:
            parts.append(f"{VERDICT_LABELS_CN[known]} {counts.pop(known)}")
    for key, n in counts.items():  # 未知档位原样保留,入参序
        parts.append(f"{key} {n}")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# run_rows:最近运行汇总 list → 表行
# ---------------------------------------------------------------------------

def run_rows(summaries: list[dict] | None) -> list[dict]:
    """把最近运行汇总(A168 收官统计 / ``data/runs`` JSON)规整为汇总表行。

    每行固定键 ``{batch, sites, verdict_cn, groups, tier_badge, ready,
    generated_at}``:

    - ``batch``:批次标识(``batch`` 缺失回退 ``batch_id``);**两处都空的行
      整行跳过**——批次是本表主键,无名行无从对号;
    - ``sites``:站点数(int 宽容转换,折不动 0);
    - ``verdict_cn``::func:`dist_line` 的中文判定分布串;
    - ``groups``:案件组数——数值直取,list/dict/集合取长度,其余 0
      (A168 ``groups`` 为行列表,A172 ``groups`` 为计数,两种形态都吃得下);
    - ``tier_badge``::func:`tier_badge` 徽章(档位缺失按默认 mid);
    - ``ready``:就绪条目数(``ready`` 缺失回退 ``ready_count``);
    - ``generated_at``:生成时间原样字符串(缺失空串,UI 显示 —)。

    入参容错:``None`` / 非列表一律 ``[]``;行可为 dict 或鸭子对象;
    ``None`` / 字符串 / 数字等**坏行直接跳过**,绝不抛错。

    示例::

        >>> row = run_rows([{"batch": "B1", "sites": 2, "tier": "high",
        ...                  "verdict_dist": {"nsfw": 1, "clean": 1},
        ...                  "groups": [{"name": "g"}, {"name": "h"}],
        ...                  "ready_count": 3}])[0]
        >>> row["batch"], row["sites"], row["groups"], row["ready"]
        ('B1', 2, 2, 3)
        >>> row["verdict_cn"], row["tier_badge"]
        ('未发现 1 / 高置信 1', '🔴 高(全压榨)')
    """
    rows: list[dict] = []
    for item in summaries if isinstance(summaries, (list, tuple)) else []:
        if item is None or isinstance(item, (str, bytes, int, float, bool)):
            continue  # 坏行(None / 字面量)跳过,不抛错
        batch = _clean_str(_field(item, "batch", "")) or _clean_str(
            _field(item, "batch_id", "")
        )
        if not batch:
            continue  # 无批次标识的行无从对号,整行跳过
        ready = _field(item, "ready", None)
        if ready is None:
            ready = _field(item, "ready_count", 0)
        rows.append(
            {
                "batch": batch,
                "sites": _as_int(_field(item, "sites", 0), 0),
                "verdict_cn": dist_line(_field(item, "verdict_dist", None)),
                "groups": _count(_field(item, "groups", 0)),
                "tier_badge": tier_badge(_field(item, "tier", "mid")),
                "ready": _as_int(ready, 0),
                "generated_at": _clean_str(_field(item, "generated_at", "")),
            }
        )
    return rows


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "最近运行 · 收官汇总页"
_MOTTO = (
    "批量跑完后的收官战况一目了然:最近批次汇总表(判定分布/组数/档位徽章)"
    "· CPU 档位建议 · 只读展示;本页面不执行任何举报提交。"
)
#: 红线 36 声明:本页只读汇总,绝不触发提交。
_SUBMIT_NOTE = (
    "⚠️ 汇总不触发任何提交(红线 36):本页只读展示收官汇总;"
    "举报须逐组声明(batch_tui)→ 逐条人工门(finishflow --report)完成,"
    "频控与每日额度照常生效(红线 24/26)。"
)
_CPU_NOTE = (
    "三档只作用于本地计算与本地回环 IO(红线 35):对外礼貌间隔、引擎限速、"
    "举报频控一概不放宽;点击「检测本机 CPU」后才发起一次本地探测(零外呼)。"
)

#: 判定 JSON"像一份运行汇总"的提示键(命中任一即收;其余 JSON 文件忽略)。
_SUMMARY_HINT_KEYS = frozenset(
    {
        "batch",
        "batch_id",
        "sites",
        "verdict_dist",
        "groups",
        "tier",
        "workers",
        "ready",
        "ready_count",
        "attest_pending",
        "generated_at",
    }
)

#: CPU 画像检测结果在 session_state 里的键(仅点击"检测"后探测一次)。
_SS_PROFILE = "runs_page_cpu_profile"

#: 汇总表列名(中文表头 → 行键;顺序即展示顺序)。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("批次", "batch"),
    ("站点数", "sites"),
    ("判定分布", "verdict_cn"),
    ("案件组数", "groups"),
    ("并发档位", "tier_badge"),
    ("就绪条目", "ready"),
    ("生成时间", "generated_at"),
)


def _load_summaries(data_dir: Any) -> list[dict]:
    """从 ``<data_dir>/runs`` 惰性收集最近运行汇总 JSON(UI 层专用)。

    只读扫描 ``runs`` 目录(含子目录,按路径倒序最多看 64 个 JSON);
    顶层为 dict 且命中任一 :data:`_SUMMARY_HINT_KEYS` 提示键才收——
    ``batch`` 缺失时回退**父目录名**(收官流程按 ``runs/<批次>/…`` 落盘)。
    排序:``generated_at`` 降序(字符串序,ISO 时间戳可比),缺时间排尾;
    最多 :data:`MAX_RUNS_SHOWN` 条。目录不存在 / 坏 JSON / 零外呼,
    任何异常一律返回 ``[]``,绝不让页面崩溃。
    """
    root = Path(str(data_dir or "data")) / "runs"
    try:
        candidates = sorted(root.rglob("*.json"), reverse=True)[:64]
    except Exception:  # noqa: BLE001 - 目录不可读按无记录
        return []
    found: list[dict] = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏 JSON / 编码问题跳过该文件
            continue
        if not isinstance(data, dict):
            continue
        if not (_SUMMARY_HINT_KEYS & {str(k) for k in data}):
            continue  # 形态不符(如 manifest / 状态文件)不进汇总表
        if not str(data.get("batch") or data.get("batch_id") or "").strip():
            data["batch"] = path.parent.name or path.stem
        found.append(data)
    found.sort(key=lambda d: str(d.get("generated_at", "")), reverse=True)
    return found[:MAX_RUNS_SHOWN]


def _render_runs(cfg: Any) -> None:
    """区①最近运行汇总表:批次/站点/判定分布/组数/档位徽章/就绪/时间。"""
    st.subheader("最近运行汇总")
    st.caption(
        "数据来源:data/runs 目录下收官流程落盘的 JSON 汇总(只读,最多展示"
        f"前 {MAX_RUNS_SHOWN} 条);当前尚无记录时,先跑一次 "
        "`python -m netsentinel.finishflow --input 清单.txt` 收官。"
    )
    data_dir = str(getattr(cfg, "data_dir", "data") or "data")
    rows = run_rows(_load_summaries(data_dir))
    if not rows:
        st.info("尚无收官汇总记录。批量扫描 + 收官汇总完成后,本表自动出现最近批次。")
        return
    st.table(
        [
            {
                cn: (str(row[key]) if row[key] != "" else "—")
                for cn, key in _COLUMNS
            }
            for row in rows
        ]
    )
    st.caption(
        "「就绪条目」= 已声明且已批准(pending→approved)的举报条目数;"
        "「并发档位」徽章:🟢 低(后台)/ 🟡 中(默认)/ 🔴 高(全压榨,"
        "仅本地计算)。本表只读,不在页面内提供任何提交入口。"
    )


def _render_cpu_advice() -> None:
    """区②CPU 建议卡:显式点击才经 A163 detect 本地探测(零外呼)。"""
    st.subheader("CPU 档位建议(本机)")
    st.caption(_CPU_NOTE)
    if st.button("检测本机 CPU", type="primary"):
        try:
            from netsentinel.ops.cpu_profile import detect  # 惰性导入 A163

            st.session_state[_SS_PROFILE] = detect()
        except Exception as exc:  # noqa: BLE001 - A163 允许缺席(并行开发期)
            st.error(f"CPU 画像模块(netsentinel.ops.cpu_profile,A163)未就位:{exc}")
    profile = st.session_state.get(_SS_PROFILE)
    if not isinstance(profile, dict):
        st.info("尚未检测。点击「检测本机 CPU」发起一次本地画像探测(不点不探,零外呼)。")
        return
    left, right = st.columns([3, 2])
    left.metric("逻辑核数", profile.get("cores", "—"))
    left.caption(
        f"架构 {profile.get('arch', '—')} · 平台 {profile.get('platform', '—')}"
        f" · psutil {'可用' if profile.get('psutil') else '不可用(不影响换算)'}"
    )
    right.metric("建议档位", tier_badge(advice_tier(profile)))
    right.caption(cpu_advice(profile))


def _render_help() -> None:
    """区③说明:数据来源 / 档位徽章 / 红线 36 提交流程指引。"""
    st.subheader("说明")
    st.markdown(
        "- **数据来源**:收官流程(`python -m netsentinel.finishflow`)落盘的"
        "运行汇总 JSON(本页只读,不写任何文件、不改任何状态);\n"
        "- **档位徽章**:🟢 低(后台,N//4)/ 🟡 中(默认,N//2)/ "
        "🔴 高(全压榨,N-reserve)——只加速本地计算与回环 IO,"
        "对外频控不变(红线 35);\n"
        f"- {_SUBMIT_NOTE}\n"
        "- **下一步**:核对无误后,先 `python -m netsentinel.cli.batch_tui` "
        "逐组声明,再 `python -m netsentinel.finishflow --report --resume 批次号` "
        "先干跑、确认后加 `--exec` 逐条人工门执行。"
    )


def render() -> None:
    """最近运行页主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染最近运行页;请先安装 python -m pip install -e \".[ui]\""
        )

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from webui.app import apply_data_dir

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🧾", layout="wide")
    st.title(f"🧾 {_PAGE_TITLE}")
    st.caption(_MOTTO)
    st.warning(_SUBMIT_NOTE)  # 页面顶部声明:汇总不触发任何提交(红线 36)

    cfg = apply_data_dir(load_config())
    _render_runs(cfg)
    _render_cpu_advice()
    _render_help()


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,最近运行页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/runs_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
