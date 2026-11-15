# -*- coding: utf-8 -*-
"""净网哨兵 · 标注工作台页(独立入口,不改动既有页面)。

定位:面向运营者的**校准标注集人工标注视图**——V10.33 建成的误判率/校准
测量体系(``netsentinel.vision.calibration``)以**人工标签**为唯一可信
ground truth(机器标签绝不冒充人标,种子标签单独分层),但其 ``label``
子命令是终端 ``input()`` 交互,35+ 张真实证据图逐张看图打字效率低。本页
把标注搬进浏览器:看图 → 点击 clean/nsfw → 即时原子落盘 → 实时门禁进度。

核心约定(V10.33 红线的页面化):
- **人工标签唯一可信来源**:页面只把**你的点击**写成
  ``labeled_by: "human:webui"``(与 CLI 的 ``human:console`` 溯源并列,
  报告按来源分层,绝不混淆 seed);页面**绝不预填、绝不猜测、零模型调用**;
- **门禁 C1 可视**:正/负样本各 ≥20 是 SPRT/级联开启的硬前提
  (calibration.GATE_DEFAULTS),侧栏实时显示进度差口;
- **语料可扩充**:证据语料天然偏 nsfw,负样本不足时经"补充语料目录"
  并入新 pending(calibration.add_pending_from_dirs,sha 去重、已标资产
  零触碰);
- **改标合法**:人工纠错是诚实行为——已标清单可改标(溯源与时间戳随
  之更新,不留假档案);
- **原子落盘**:每次点击整集重写经 V10.12 ``atomic.replace_with_retry``
  (Windows 重试),进程中断不产生半写标注集;
- **页面零外呼**:只做本地文件读写与标注集维护;score/report(真实
  VLM 评分)仍按红线在 CLI 显式运行,页内只展示命令提示,不代跑。

数据布局:标注集 ``<data_dir>/calibration/labeled.jsonl``(与
``python -m netsentinel.vision.calibration`` 同一文件、同一 JSONL 格式;
两边的修改互相可见,CLI ``label`` 与本页可交替使用)。

结构约定(与 tests/test_label_page.py 对应):
- 纯逻辑层(上半部分,无 streamlit;兄弟模块一律函数内惰性导入);
- UI 层(``render``,streamlit 顶部惰性导入,缺依赖时 ``main()`` 打印
  中文安装提示并返回退出码 1)。
"""
from __future__ import annotations

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
    "LABEL_SET_REL",
    "LABEL_SOURCE",
    "DEFAULT_SEED_DIRS",
    "RECENT_LABELED_CAP",
    "add_pending",
    "apply_label",
    "cli_hint",
    "gate_view",
    "image_missing",
    "import_labeled",
    "load_or_seed",
    "main",
    "progress",
    "resolve_image",
    "save_set",
    "set_path",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 标注集相对数据目录的路径(与 calibration CLI 缺省同源)。
LABEL_SET_REL: str = "calibration/labeled.jsonl"

#: 本页写入的人工标签溯源(与 CLI ``human:console`` 并列;报告按来源分层)。
LABEL_SOURCE: str = "human:webui"

#: 缺省种子语料目录(与 V10.33 校准 init 同源:夹具 + 真实证据)。
DEFAULT_SEED_DIRS: tuple[str, ...] = ("tests/fixtures/demo_site", "data/evidence")

#: "已标清单"折叠区最多展示条数(纯展示上限,不影响数据)。
RECENT_LABELED_CAP: int = 30


def set_path(data_dir: Any) -> Path:
    """数据目录 → 标注集路径(``<data_dir>/calibration/labeled.jsonl``)。"""
    return Path(str(data_dir)) / Path(LABEL_SET_REL)


def load_or_seed(
    data_dir: Any, seed_dirs: tuple[str, ...] | list[str] | None = None
) -> tuple[Any, bool]:
    """装载标注集;文件不存在时按种子语料初始化(与 CLI init 同源)。

    :return: ``(LabeledSet, created)``——created=True 表示本次新建(页顶
        提示一次性说明来源;种子规则见 calibration.seed_from_dirs:
        仅内容可验证的合成图标 clean,真实图一律 pending 待人工)。
    """
    from netsentinel.vision.calibration import LabeledSet, seed_from_dirs

    p = set_path(data_dir)
    if p.is_file():
        return LabeledSet.load(p), False
    s, _pending = seed_from_dirs(list(seed_dirs or DEFAULT_SEED_DIRS))
    save_set(s, p)
    return s, True


def save_set(s: Any, path: str | Path) -> None:
    """整集原子落盘(与 LabeledSet.save 同一 JSONL 格式;Windows 重试)。"""
    import json

    from netsentinel.atomic import replace_with_retry

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(it.as_dict(), ensure_ascii=False) for it in s.items]
    text = "\n".join(lines) + ("\n" if lines else "")
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    replace_with_retry(tmp, target)


def apply_label(s: Any, sha256: str, label: str, source: str = LABEL_SOURCE) -> bool:
    """把一张图的标签写成人工判定(按 sha256 定位;合法标签白名单校验)。

    - ``label`` 只认 clean / nsfw(calibration.VALID_LABELS 同源);
    - 改标合法(人工纠错):覆写旧值并更新 ``labeled_by`` / ``labeled_at``,
      绝不留"表面是人标、实际是旧值"的假档案;
    - 找不到该 sha → 返回 False(页面据此提示);**调用方负责 save**。
    """
    from netsentinel import timesource
    from netsentinel.vision.calibration import LABEL_CLEAN, LABEL_NSFW, VALID_LABELS

    if label not in VALID_LABELS:
        raise ValueError(f"非法标签 {label!r}(合法:{LABEL_CLEAN}/{LABEL_NSFW})")
    for it in s.items:
        if it.sha256 == sha256:
            it.label = label
            it.labeled_by = source
            it.labeled_at = timesource.now_iso()
            return True
    return False


def progress(s: Any) -> dict[str, Any]:
    """标注进度汇总:labeled/pending/正负样本计数 + 来源分层(纯计算)。"""
    stats = s.stats()
    by_label = stats.get("by_label") or {}
    return {
        "total": stats.get("total", 0),
        "labeled": stats.get("labeled", 0),
        "pending": stats.get("pending", 0),
        "pos": int(by_label.get("nsfw", 0)),   # 正样本 = nsfw(FN 门禁对象)
        "neg": int(by_label.get("clean", 0)),  # 负样本 = clean(FP 门禁对象)
        "by_source": stats.get("by_source") or {},
    }


def gate_view(s: Any, min_pos: int | None = None, min_neg: int | None = None) -> dict[str, Any]:
    """门禁 C1(样本量)进度视图:正/负样本各 ≥N 才可开 SPRT/级联数字门禁。

    缺省阈值取 ``calibration.GATE_DEFAULTS``(pos/neg 各 20);只看 C1——
    C2–C5(校准/误判)须 score+report 之后才有数字,页面不预判。
    """
    from netsentinel.vision.calibration import GATE_DEFAULTS

    floor_pos = int(min_pos if min_pos is not None else GATE_DEFAULTS["min_pos"])
    floor_neg = int(min_neg if min_neg is not None else GATE_DEFAULTS["min_neg"])
    prog = progress(s)
    return {
        "min_pos": floor_pos,
        "min_neg": floor_neg,
        "pos": prog["pos"],
        "neg": prog["neg"],
        "pos_gap": max(0, floor_pos - prog["pos"]),
        "neg_gap": max(0, floor_neg - prog["neg"]),
        "pos_ok": prog["pos"] >= floor_pos,
        "neg_ok": prog["neg"] >= floor_neg,
        "ready": prog["pos"] >= floor_pos and prog["neg"] >= floor_neg,
    }


def add_pending(s: Any, dirs: list[str]) -> int:
    """并入补充语料目录(sha 去重;已标资产零触碰);**调用方负责 save**。"""
    from netsentinel.vision.calibration import add_pending_from_dirs

    return add_pending_from_dirs(s, dirs)


def import_labeled(s: Any, dirs: list[str], label: str) -> dict[str, int]:
    """整组导入已标注目录(正/负样本批量入口);**调用方负责 save**。

    语义:你对该目录**全部图片内容**作出整组判定(clean / nsfw)——
    新图直接入集为已标、集内 pending 升级、**已标跳过绝不覆盖**(改标
    走显式改标路径);溯源 ``human:dir-import``(与逐张点击
    ``human:webui`` 并列,报告按来源分层、note 留目录审计线索)。
    """
    from netsentinel.vision.calibration import import_labeled_dir

    return import_labeled_dir(s, dirs, label)


def resolve_image(path_str: str) -> Path:
    """标注集里的图片路径 → 本地 Path(相对路径按当前工作目录解析)。

    标注集由 CLI/页面在项目根运行时生成,路径即项目根相对路径;
    Streamlit 服务按 start_services 以项目根为 cwd 启动,直连解析。
    """
    p = Path(path_str)
    return p if p.is_absolute() else (Path.cwd() / p)


def image_missing(path_str: str) -> bool:
    """该图当前是否缺失(移动/清理过的语料;页面提示后可跳过)。"""
    return not resolve_image(path_str).is_file()


def cli_hint(model: str = "glm:glm-5.3-flash") -> str:
    """标注完成后的下一步 CLI 命令提示(只提示,不代跑)。"""
    return (
        "python -m netsentinel.vision.calibration score "
        f"--model {model} --config config.yaml\n"
        "python -m netsentinel.vision.calibration report "
        "--scores data/calibration/scores_<model>.jsonl --out data/calibration/report.md"
    )


# ===========================================================================
# UI 层(渲染;全部读写经纯逻辑层)
# ===========================================================================
def render() -> None:
    """标注工作台主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError("streamlit 不可用,无法渲染标注工作台;请先安装 python -m pip install -e \".[ui]\"")

    # 兄弟模块函数内导入(契约:兄弟模块一律惰性导入)
    from netsentinel.config import load_config
    from webui.app import apply_data_dir

    st.set_page_config(page_title="标注工作台 · 净网哨兵", page_icon="🧷", layout="wide")
    st.title("🧷 标注工作台(校准门禁 · 人工标签)")
    st.caption(
        "给 GLM 误判率/校准测量体系供**人工 ground truth**:你看图打标签,"
        "SPRT/级联的开启门禁(正/负样本各 ≥20 + 校准数字)才有可复算的地基。"
        "本页只记录**你的点击**(`human:webui` 溯源),绝不预填、零模型调用。"
    )

    cfg = apply_data_dir(load_config())
    data_dir = str(cfg.data_dir)
    s, created = load_or_seed(data_dir)
    if created:
        st.info(
            f"标注集不存在,已按缺省语料初始化(夹具 + 真实证据;合成图标种子"
            f" clean、真实图一律 pending 待人工):`{set_path(data_dir)}`"
        )

    # ---------------- 侧栏:进度 + 门禁 C1 + 路径说明 ----------------
    with st.sidebar:
        st.header("标注进度")
        prog = progress(s)
        m1, m2 = st.columns(2)
        m1.metric("已标", prog["labeled"])
        m2.metric("待标", prog["pending"])
        m3, m4 = st.columns(2)
        m3.metric("正样本(nsfw)", prog["pos"])
        m4.metric("负样本(clean)", prog["neg"])

        st.divider()
        gate = gate_view(s)
        st.subheader("门禁 C1(样本量)")
        st.progress(min(1.0, prog["pos"] / max(1, gate["min_pos"])),
                    text=f"正样本 {prog['pos']}/{gate['min_pos']}")
        st.progress(min(1.0, prog["neg"] / max(1, gate["min_neg"])),
                    text=f"负样本 {prog['neg']}/{gate['min_neg']}")
        if gate["ready"]:
            st.success("C1 已达标:正/负样本均 ≥20;下一步 score + report 出全量门禁判定")
        else:
            gap = []
            if gate["pos_gap"]:
                gap.append(f"正样本还差 {gate['pos_gap']}")
            if gate["neg_gap"]:
                gap.append(f"负样本还差 {gate['neg_gap']}")
            st.warning("未达标:" + "、".join(gap) + "(负样本不足可经下方「补充语料目录」扩 clean 图)")

        st.divider()
        src = prog["by_source"]
        if src:
            st.caption("已标来源分层:" + "、".join(f"{k}×{v}" for k, v in sorted(src.items())))
        st.subheader("数据目录")
        st.markdown(
            f"- 标注集:`{set_path(data_dir)}`\n"
            "- 与 CLI 互通:`calibration label` / 本页读写同一 JSONL\n"
            f"- 数据根:`{data_dir}`(环境变量 `NETSENTINEL_DATA_DIR` 可覆盖)"
        )

    # ---------------- 主区①:逐张标注 ----------------
    pending = s.pending()
    if not pending:
        st.success("全部条目已标注完成 🎉 下一步(CLI 显式运行,真实 VLM 评分):")
        st.code(cli_hint(), language="bash")
    else:
        if "label_idx" not in st.session_state:
            st.session_state.label_idx = 0
        idx = st.session_state.label_idx % len(pending)
        item = pending[idx]
        st.markdown(
            f"**第 {idx + 1} / {len(pending)} 张** · sha `{item.sha256[:16]}…` · "
            f"{item.note or '(无提示)'}"
        )
        img = resolve_image(item.path)
        if img.is_file():
            st.image(str(img), caption=item.path, use_container_width=True)
        else:
            st.warning(f"图片文件缺失:{item.path}(可能语料被移动/清理;可跳过)")

        def _label_and_advance(label: str) -> None:
            ok = apply_label(s, item.sha256, label)
            if ok:
                save_set(s, set_path(data_dir))
            st.session_state.label_idx = idx + 1
            st.rerun()

        col_prev, col_clean, col_nsfw, col_skip = st.columns(4)
        if col_clean.button("✅ clean(正常)", type="primary", use_container_width=True):
            _label_and_advance("clean")
        if col_nsfw.button("🔞 nsfw(色情)", type="primary", use_container_width=True):
            _label_and_advance("nsfw")
        if col_skip.button("⏭ 跳过(下一张)", use_container_width=True):
            st.session_state.label_idx = idx + 1
            st.rerun()
        if col_prev.button("⏮ 上一张", use_container_width=True):
            st.session_state.label_idx = max(0, idx - 1)
            st.rerun()

    # ---------------- 主区②:已标清单(改标 = 人工纠错) ----------------
    with st.expander(f"已标清单(可改标,最近 {RECENT_LABELED_CAP} 条)", expanded=False):
        labeled = s.labeled()
        if not labeled:
            st.caption("还没有已标条目。")
        else:
            shown = labeled[-RECENT_LABELED_CAP:]
            options = {f"{it.label} · {Path(it.path).name} · {it.labeled_by}": it for it in shown}
            choice = st.selectbox("选择条目", list(options.keys()))
            target = options[choice]
            c1, c2 = st.columns(2)
            if c1.button("改标 clean", key=f"relab_clean_{target.sha256[:8]}"):
                apply_label(s, target.sha256, "clean")
                save_set(s, set_path(data_dir))
                st.rerun()
            if c2.button("改标 nsfw", key=f"relab_nsfw_{target.sha256[:8]}"):
                apply_label(s, target.sha256, "nsfw")
                save_set(s, set_path(data_dir))
                st.rerun()

    # ---------------- 主区③:补充语料目录(pending)与 整组导入(已标注) ----------------
    with st.expander("补充语料目录(新图入 pending,待逐张标注)", expanded=False):
        st.caption(
            "证据语料天然偏 nsfw;门禁 C1 需要负样本(clean)≥20——把你确认为"
            "正常内容的图片目录填进来(递归扫描,sha 去重,已标资产零触碰,新图全部 pending)。"
        )
        extra_dir = st.text_input("目录路径(相对项目根或绝对路径)", value="", key="extra_dir")
        if st.button("并入新图", key="btn_add_pending"):
            if not extra_dir.strip():
                st.error("请先填写目录路径")
            else:
                try:
                    added = add_pending(s, [extra_dir.strip()])
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    save_set(s, set_path(data_dir))
                    if added:
                        st.success(f"已并入 {added} 张新图(pending,回到上方继续标注)")
                        st.rerun()
                    else:
                        st.info("该目录没有新内容(全部与既有条目 sha 重复)")

    with st.expander("📥 整组导入正/负样本(目录即标签,批量入口)", expanded=False):
        st.caption(
            "你确信整组同类的目录(如过往案件中确认的色情图目录)可**一键整组导入**:"
            "新图直接入集为已标、集内 pending 同步升级、**已标条目跳过绝不覆盖**。"
            "溯源 `human:dir-import`(与逐张点击并列的人工判定,note 留目录线索)。"
        )
        import_dir = st.text_input(
            "目录路径(相对项目根或绝对路径)", value="", key="import_dir"
        )
        st.checkbox(
            "我已核实并对该目录全部图片内容作出整组判定(导入即视为人工标注)",
            value=False, key="import_confirm",
        )
        col_imp_clean, col_imp_nsfw = st.columns(2)
        do_import = False
        import_label = ""
        if col_imp_clean.button("整组导入为 clean(负样本)", key="btn_import_clean",
                                use_container_width=True):
            do_import, import_label = True, "clean"
        if col_imp_nsfw.button("整组导入为 nsfw(正样本)", type="primary",
                               key="btn_import_nsfw", use_container_width=True):
            do_import, import_label = True, "nsfw"
        if do_import:
            if not import_dir.strip():
                st.error("请先填写目录路径")
            elif not st.session_state.get("import_confirm", False):
                st.error("请先勾选确认(整组导入 = 你对目录内容的整组人工判定)")
            else:
                try:
                    result = import_labeled(s, [import_dir.strip()], import_label)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    if result["added"] or result["promoted"]:
                        save_set(s, set_path(data_dir))
                        st.success(
                            f"已导入({import_label}):新增 {result['added']} 张、"
                            f"pending 升级 {result['promoted']} 张、已标跳过 "
                            f"{result['skipped']} 张——侧栏进度已更新"
                        )
                        st.rerun()
                    else:
                        st.info("该目录没有可导入内容(全部为已标条目,已标绝不覆盖)")

    # ---------------- 主区④:红线与用法 ----------------
    with st.expander("红线与用法说明", expanded=False):
        st.markdown(
            "- **人工标签唯一可信来源**:本页只把你的点击记为 `human:webui`;"
            "种子标签(seed:*)单独分层,绝不冒充人标(V10.33 诚实分级)。\n"
            "- **改标合法**:看错点错随时在「已标清单」改标——溯源与时间戳"
            "随之更新,不留假档案。\n"
            "- **页面零外呼、零模型**:本页绝不调用任何 VLM、绝不代跑评分;"
            "score(真实 VLM,预算一本账)与 report 在 CLI 显式运行。\n"
            "- **标注口径**:按**图片内容**判 clean/nsfw(正常 / 色情),"
            "与生产三阈值(0.5/0.8/0.9)的判定对象一致;模糊难判的选跳过,"
            "宁缺毋滥。\n"
            "- **原子落盘**:每次点击整集原子重写(Windows 重试),"
            "进程中断不产生半写标注集。\n"
            "- **数据不入库**:标注集位于数据目录,随 `data/` 整体被"
            "版本库忽略(内容含真实证据)。"
        )


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,标注工作台无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/label_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    _exit_code = main()
    # V10.20(SystemExit 收尾修复,同 webui/app.py):streamlit 脚本线程内
    # 不抛 SystemExit(BaseException 会杀死脚本线程,AppTest 通道失效);
    # 仅真直跑才转进程退出码(保留缺依赖提示退出码 1 契约)。
    _in_script_thread = False
    if _HAS_ST:
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx

            _in_script_thread = get_script_run_ctx() is not None
        except Exception:  # noqa: BLE001 - 内部 API 缺席按直跑处理
            _in_script_thread = False
    if not _in_script_thread:
        raise SystemExit(_exit_code)
