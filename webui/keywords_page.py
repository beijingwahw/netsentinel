"""净网哨兵 · 关键词工作台页(独立入口,不改动既有页面)。

定位:面向运营者的**发现关键词实时编辑与切换**视图——发现子系统的搜索词
(红线 28:代码零内置关键词)此前只能在本地手工编辑 .txt 后经
``python -m netsentinel.discovery --keywords-file`` 使用;本页把"当前关键词"
与"词组库"搬进浏览器,编辑/切换即时落盘,**下一次发现运行即生效**(发现每次
运行都重新装载关键词文件,无需重启任何服务)。

文件布局(全部在 ``<data_dir>/keywords/`` 下,随 ``data/`` 整体不入库):
- ``active.txt``  当前关键词——发现命令 ``--keywords-file`` 指向的唯一活文件;
- ``templates.txt`` 查询模板活文件(V10.20)——``{kw}`` 占位符,每行一条,
  ``#`` 注释;发现命令 ``--template-file`` 指向它,本页展开预览默认读它;
- ``<名称>.txt``   词组库(命名词组,一键"启用"即整组拷贝为 active.txt);
- ``*.bak``        每次覆盖保存前留下的上一版内容(单份滚动)。

结构约定(与 tests/test_keywords_page.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、兄弟模块一律函数内惰性导入):
  * :func:`keywords_root` / :func:`active_path`  数据目录 → 关键词目录/活文件路径;
  * :func:`templates_path` / :func:`read_templates` / :func:`save_templates` /
    :func:`clear_templates` / :func:`parse_templates_stats`
    模板活文件路径 / 读 / 原子写(.bak 滚动)/ 清空 / 实时统计(V10.20);
  * :func:`parse_stats`     编辑文本 → {词数, 跳过} 实时统计(不落盘);
  * :func:`read_active` / :func:`save_active`   活文件读/原子写(.bak 滚动);
  * :func:`list_sets` / :func:`read_set`        词组库清单/读取;
  * :func:`create_set` / :func:`delete_set`     新建(拒覆盖)/删除;
  * :func:`switch_active`   词组 → 活文件整组拷贝(启用即切换);
  * :func:`save_active_as_set` 当前内容另存为新词组;
  * :func:`preview_queries` 关键词 × 模板展开预览(纯本地,零外呼);
  * :func:`cli_hint`        下一步 CLI 命令提示串(模板存在时追加
    ``--template-file``,V10.20 与 discovery CLI 同名参数联动)。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 三页签:①当前关键词(编辑/保存/模板持久化/展开预览/
    命令提示)②词组库(清单/启用/载入编辑器/删除/新建/另存)③红线与用法说明。

安全红线(必须体现在代码里):
- **零内置关键词**(红线 28):本页不预置任何搜索词,新建/保存一律复用
  ``discovery.keywords.load_keywords`` 校验(为空即中文报错);模板同为
  运营者自拟,保存复用 ``discovery.keywords.load_templates`` 同源校验
  (空表即中文报错,代码同样不内置任何模板);
- **常规浏览零外呼**:本页只做本地文件读写与纯函数展开预览,**绝不发起
  发现/搜索**——发现须运营者显式在 CLI 运行且受 ``discovery_online``
  与密钥门槛约束(页内只展示命令提示,不代跑);
- **原子落盘**:覆盖保存走 V10.12 ``atomic.replace_with_retry``,写前留
  ``.bak`` 上一版,进程中断不产生半写文件;
- **词组名防穿越**:仅允许字母/数字/下划线/连字符/中文 1-24 位,且保留名
  ``active`` / ``templates`` 拒用(分别与活文件 active.txt、模板活文件
  templates.txt 冲突)。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
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
    "ACTIVE_BASENAME",
    "CLI_HINT_ENGINE_DEFAULT",
    "PREVIEW_CAP",
    "TEMPLATES_BASENAME",
    "cli_hint",
    "clear_templates",
    "create_set",
    "delete_set",
    "keywords_root",
    "list_sets",
    "main",
    "parse_stats",
    "parse_templates_stats",
    "preview_queries",
    "read_active",
    "read_set",
    "read_templates",
    "render",
    "save_active",
    "save_active_as_set",
    "save_templates",
    "set_action_key",
    "switch_active",
    "templates_path",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 当前关键词活文件名(发现命令 ``--keywords-file`` 指向它)。
ACTIVE_BASENAME: str = "active.txt"

#: 查询模板活文件名(V10.20;发现命令 ``--template-file`` 指向它,
#: 关键词页展开预览默认读它)。
TEMPLATES_BASENAME: str = "templates.txt"

#: 词组库文件后缀(词组以 ``<名称>.txt`` 存放)。
SET_SUFFIX: str = ".txt"

#: 覆盖保存前的滚动备份后缀(单份:``<文件>.bak``)。
BAK_SUFFIX: str = ".bak"

#: 展开预览最多展示的查询条数(纯展示上限,不影响实际展开)。
PREVIEW_CAP: int = 200

#: cli_hint 缺省引擎名(与 discovery CLI ``--engine`` 缺省口径一致)。
CLI_HINT_ENGINE_DEFAULT: str = "yandex"

#: 词组名合法字符:字母/数字/下划线/连字符/中文,1-24 位。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,24}$")

#: 保留词组名(与活文件 active.txt / 模板活文件 templates.txt 冲突,拒用)。
_RESERVED_NAMES = frozenset({"active", "templates"})


def keywords_root(data_dir: Any) -> Path:
    """数据目录 → 关键词目录路径(不创建;调用方按需 mkdir)。"""
    return Path(str(data_dir or "data")) / "keywords"


def active_path(data_dir: Any) -> Path:
    """数据目录 → 当前关键词活文件完整路径。"""
    return keywords_root(data_dir) / ACTIVE_BASENAME


def templates_path(data_dir: Any) -> Path:
    """数据目录 → 查询模板活文件完整路径(V10.20;``--template-file`` 指向它)。"""
    return keywords_root(data_dir) / TEMPLATES_BASENAME


def _set_path(data_dir: Any, name: str) -> Path:
    """词组名 → 词组文件完整路径(先经 :func:`_safe_name` 校验)。"""
    return keywords_root(data_dir) / (_safe_name(name) + SET_SUFFIX)


def _safe_name(name: str) -> str:
    """校验词组名:非法/保留名抛中文 ValueError,合法则原样返回(去空白)。"""
    text = str(name or "").strip()
    if not text:
        raise ValueError("词组名不能为空")
    if not _SAFE_NAME_RE.match(text):
        raise ValueError(
            "词组名仅允许字母/数字/下划线/连字符/中文,长度 1-24:" f"{name!r}"
        )
    if text in _RESERVED_NAMES:
        raise ValueError(
            f"词组名 {text} 为保留名(与活文件 active.txt / 模板文件 "
            "templates.txt 冲突),请换一个"
        )
    return text


def _validate_words(text: str) -> list[str]:
    """复用发现子系统的装载校验(红线 28 同源):空表抛中文 ValueError。"""
    from netsentinel.discovery.keywords import load_keywords  # 惰性

    return load_keywords([ln for ln in str(text or "").splitlines()])


def parse_stats(text: str) -> dict:
    """编辑文本 → 实时统计(不落盘):{words, dropped}。

    有效词口径与 :func:`_validate_words` 同源(空行/#注释跳过、去重保序),
    但为空时**不抛错**而返回 words=0——编辑中途的实时反馈不应打断输入;
    ``dropped`` 统计被跳过的行数(空行/注释/**重复词**各按一行计)。
    """
    seen: list[str] = []
    dropped = 0
    for ln in str(text or "").splitlines():
        word = ln.strip()
        if not word or word.startswith("#") or word in seen:
            dropped += 1
            continue
        seen.append(word)
    return {"words": len(seen), "dropped": dropped}


def _atomic_write(path: Path, text: str) -> None:
    """原子写文件:临时文件 + V10.12 replace_with_retry(Windows 重试)。"""
    from netsentinel.atomic import replace_with_retry  # 惰性

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    replace_with_retry(tmp, path)


def _write_with_bak(path: Path, text: str) -> None:
    """覆盖保存:旧内容(若存在)先滚动为 ``.bak`` 再原子写新内容。"""
    if path.is_file():
        bak = path.with_name(path.name + BAK_SUFFIX)
        try:
            bak.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        except OSError:  # noqa: BLE001 - 备份失败不应阻断保存,旧内容仍在原文件
            pass
    _atomic_write(path, text)


def read_active(data_dir: Any) -> str:
    """读当前关键词活文件;不存在时返回空串(页面引导新建)。"""
    path = active_path(data_dir)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:  # noqa: BLE001 - 读失败按未配置处理,UI 层提示
        return ""


def save_active(data_dir: Any, text: str) -> dict:
    """保存当前关键词(原子写 + .bak 滚动);空表抛中文 ValueError。

    :returns: ``{"words": 词数, "path": 活文件路径字符串}``
    """
    words = _validate_words(text)
    path = active_path(data_dir)
    _write_with_bak(path, str(text))
    return {"words": len(words), "path": str(path)}


# ---------------------------------------------------------------------------
# 查询模板活文件(V10.20:读取/原子写/清空/统计;与关键词同款纪律)
# ---------------------------------------------------------------------------


def _parse_template_lines(text: Any) -> tuple[list[str], int]:
    """模板文本 → ``(有效模板列表, 跳过行数)``;空行/#注释/重复行各计一行。"""
    kept: list[str] = []
    dropped = 0
    for ln in str(text or "").splitlines():
        tpl = ln.strip()
        if not tpl or tpl.startswith("#") or tpl in kept:
            dropped += 1
            continue
        kept.append(tpl)
    return kept, dropped


def parse_templates_stats(text: str) -> dict:
    """模板编辑文本 → 实时统计(不落盘):``{templates, dropped}``。

    口径与 :func:`parse_stats` 对称:编辑中途的实时反馈不应打断输入,
    为空时**不抛错**而返回 ``templates=0``。
    """
    kept, dropped = _parse_template_lines(text)
    return {"templates": len(kept), "dropped": dropped}


def read_templates(data_dir: Any) -> str:
    """读查询模板活文件;不存在时返回空串(预览回退原样查询)。"""
    path = templates_path(data_dir)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:  # noqa: BLE001 - 读失败按未配置处理
        return ""


def save_templates(data_dir: Any, text: str) -> dict:
    """保存查询模板活文件(原子写 + .bak 滚动);空表抛中文 ValueError。

    校验与 :func:`save_active` 同源(红线 28 的模板版):复用
    ``discovery.keywords.load_templates``,空表/全注释即拒绝,代码零内置模板。

    :returns: ``{"templates": 模板数, "path": 模板文件路径字符串}``
    """
    from netsentinel.discovery.keywords import load_templates  # 惰性

    tpls = load_templates([ln for ln in str(text or "").splitlines()])
    path = templates_path(data_dir)
    _write_with_bak(path, str(text))
    return {"templates": len(tpls), "path": str(path)}


def clear_templates(data_dir: Any) -> bool:
    """清空查询模板:删除模板活文件(预览回退原样查询)。

    ``.bak`` 保留(后悔药;下次保存会覆盖滚动)。文件本就不存在时无事发生。

    :returns: 是否真的删除了文件。
    """
    path = templates_path(data_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:  # noqa: BLE001 - 占用等删除失败由 UI 层提示
        raise


def list_sets(data_dir: Any) -> list[dict]:
    """词组库清单:``{name, words, mtime, size}`` 按名称排序。

    解析失败的词组 ``words=None``(不中断清单;UI 标红引导修复)。
    活文件(active.txt)与 ``.bak``/``.tmp`` 不在词组库内。
    """
    root = keywords_root(data_dir)
    rows: list[dict] = []
    if not root.is_dir():
        return rows
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        if not item.is_file() or item.suffix.lower() != SET_SUFFIX:
            continue
        if (
            item.name == ACTIVE_BASENAME
            or item.name == TEMPLATES_BASENAME  # V10.20:模板活文件不是词组
            or item.name.endswith(BAK_SUFFIX)
        ):
            continue
        if item.name.endswith(".tmp"):
            continue
        try:
            stat = item.stat()
        except OSError:  # noqa: BLE001 - 竞态删除按缺席处理
            continue
        try:
            count = len(_validate_words(item.read_text(encoding="utf-8-sig")))
        except (ValueError, OSError):  # noqa: BLE001 - 坏文件如实标注
            count = -1
        rows.append(
            {
                "name": item.name[: -len(SET_SUFFIX)],
                "words": count,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
                "size": stat.st_size,
            }
        )
    return rows


def read_set(data_dir: Any, name: str) -> str:
    """读词组内容;不存在抛中文 ValueError。"""
    path = _set_path(data_dir, name)
    if not path.is_file():
        raise ValueError(f"词组不存在:{_safe_name(name)}")
    return path.read_text(encoding="utf-8-sig")


def create_set(data_dir: Any, name: str, text: str) -> dict:
    """新建词组(校验 + 拒绝覆盖同名);返回 ``{"words": 词数}``。"""
    safe = _safe_name(name)
    words = _validate_words(text)
    path = keywords_root(data_dir) / (safe + SET_SUFFIX)
    if path.exists():
        raise ValueError(f"词组已存在:{safe}(如需覆盖请先删除再新建)")
    _atomic_write(path, str(text))
    return {"words": len(words)}


def delete_set(data_dir: Any, name: str) -> None:
    """删除词组;不存在抛中文 ValueError(活文件 active.txt 不是词组,不可经此删)。"""
    path = _set_path(data_dir, name)
    if not path.is_file():
        raise ValueError(f"词组不存在:{_safe_name(name)}")
    path.unlink()


def switch_active(data_dir: Any, name: str) -> dict:
    """启用词组:整组内容原子拷贝为当前关键词(即"实时切换")。

    词组内容仍会经装载校验(为空/全注释即拒绝切换,活文件不受影响)。
    :returns: ``{"words": 词数, "path": 活文件路径字符串}``
    """
    text = read_set(data_dir, name)
    return save_active(data_dir, text)


def save_active_as_set(data_dir: Any, name: str) -> dict:
    """把当前关键词另存为新词组(拒覆盖;当前为空抛中文 ValueError)。"""
    text = read_active(data_dir)
    if not str(text).strip():
        raise ValueError("当前关键词为空,无法另存(请先在'当前关键词'页保存内容)")
    return create_set(data_dir, name, text)


def preview_queries(text: str, templates: str = "") -> list[str]:
    """关键词 × 模板展开预览(纯本地零外呼);空表抛中文 ValueError。

    ``templates`` 每行一个模板,``{kw}`` 为占位符(与 discovery 同口径);
    空白则按原样查询展开。返回去重保序的查询列表(**不截断**,截断仅是
    UI 展示口径 :data:`PREVIEW_CAP`)。
    """
    words = _validate_words(text)
    tpls = [ln.strip() for ln in str(templates or "").splitlines() if ln.strip()]
    from netsentinel.discovery.keywords import expand_queries  # 惰性

    return expand_queries(words, tpls or None)


def cli_hint(
    data_dir: Any,
    engine: str = CLI_HINT_ENGINE_DEFAULT,
    templates_text: str | None = None,
) -> str:
    """下一步发现命令提示(仅提示,页面绝不代跑;受 discovery_online/密钥门槛)。

    V10.20:查询模板活文件存在且有有效模板时,自动追加
    ``--template-file "<templates.txt 路径>"``(与 discovery CLI 同名参数
    联动;任务原文与 docs/DISCOVERY.md 的 V10.20 口径一致)。``templates_text``
    供测试注入模板内容(缺省 ``None`` = 现场读活文件,零外呼)。
    """
    hint = (
        "python -m netsentinel.discovery"
        f" --keywords-file \"{active_path(data_dir)}\""
        f" --engine {engine} --out leads.txt"
    )
    text = read_templates(data_dir) if templates_text is None else str(templates_text)
    kept, _dropped = _parse_template_lines(text)
    if kept:
        hint += f" --template-file \"{templates_path(data_dir)}\""
    return hint


#: 词组操作控件的种类(switch 启用 / load 载入编辑器 / delete 删除)。
_SET_KEY_KINDS: tuple[str, ...] = ("switch", "load", "delete")


def set_action_key(kind: str, name: str) -> str:
    """词组库逐组操作按钮的控件 key(V10.20 形式化;纯逻辑层)。

    词组名经 ``list_sets`` 来自文件名(文件系统保证唯一),key = 操作种类 +
    词组名,同名文件不会出现;唯一性由 tests/test_webui_key_audit.py 锁定。

    示例::

        >>> set_action_key("switch", "色情线索")
        'kw_switch_色情线索'
    """
    return f"kw_{kind}_{name}"


# ===========================================================================
# UI 层(streamlit 惰性;缺依赖时 main() 拦截)
# ===========================================================================

_PAGE_TITLE = "关键词工作台"
_MOTTO = "发现搜索词的实时编辑与切换 · 零内置关键词 · 页面零外呼"


def render() -> None:
    """渲染关键词工作台(三页签:当前关键词 / 词组库 / 红线与用法)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,双保险
        raise RuntimeError(
            "streamlit 不可用,无法渲染关键词页;请先安装 python -m pip install -e \".[ui]\""
        )

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from webui.app import apply_data_dir

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🗝️", layout="wide")
    st.title(f"🗝️ {_PAGE_TITLE}")
    st.caption(_MOTTO)

    cfg = apply_data_dir(load_config())
    data_dir = str(getattr(cfg, "data_dir", "data") or "data")

    tab_active, tab_sets, tab_help = st.tabs(["当前关键词", "词组库", "红线与用法"])
    with tab_active:
        _render_active(data_dir)
    with tab_sets:
        _render_sets(data_dir)
    with tab_help:
        _render_help(data_dir)


def _render_active(data_dir: str) -> None:
    """页签①:当前关键词编辑器(保存/实时统计/模板持久化/展开预览/命令提示)。"""
    editor_key = "kw_active_editor"
    if editor_key not in st.session_state:
        st.session_state[editor_key] = read_active(data_dir)

    path = active_path(data_dir)
    if path.is_file():
        st.success(f"当前关键词文件:`{path}`(发现每次运行都重新装载,保存后下次即生效)")
    else:
        st.warning(
            f"尚无当前关键词文件(`{path}`)。在下方编辑并保存,"
            "或在'词组库'页签一键启用某个词组。"
        )

    text = st.text_area(
        "关键词(每行一个,`#` 开头为注释;词组库由你维护,代码不内置任何关键词)",
        key=editor_key,
        height=260,
    )
    stats = parse_stats(text)
    st.caption(
        f"实时统计:有效关键词 {stats['words']} 个 · 跳过空行/注释 {stats['dropped']} 行"
    )

    if st.button("💾 保存当前关键词", type="primary", key="kw_save_active"):
        try:
            saved = save_active(data_dir, text)
            st.success(
                f"已保存:{saved['words']} 个关键词 → `{saved['path']}`"
                "(旧内容已滚动备份为 .bak)"
            )
        except ValueError as exc:
            st.error(str(exc))

    _render_templates(data_dir)
    _render_preview(data_dir, text)

    st.subheader("下一步:运行发现(CLI 显式执行,页面不代跑)")
    st.code(cli_hint(data_dir), language="bash")
    st.caption(
        "发现外呼受 `discovery_online`(默认关)与引擎密钥门槛约束;"
        "产出 leads.txt 后走批量导入规划(`python -m netsentinel.ops.bulk_intake`)。"
    )


def _render_templates(data_dir: str) -> None:
    """页签①:查询模板持久化编辑区(读取/编辑/保存/清空,V10.20)。

    模板是"展开预览"与 discovery CLI ``--template-file`` 共用的持久对象:
    每行一条、``{kw}`` 占位符、``#`` 注释;与关键词同款原子写 + .bak 滚动
    + 空表中文拒绝(代码零内置模板,红线 28 同源)。
    """
    with st.expander("🧩 查询模板(持久;`{kw}` 占位符,发现 CLI --template-file 联动)"):
        tpl_path = templates_path(data_dir)
        editor_key = "kw_templates_editor"
        if editor_key not in st.session_state:
            st.session_state[editor_key] = read_templates(data_dir)
        if tpl_path.is_file():
            st.caption(f"模板活文件:`{tpl_path}`(保存后展开预览与发现 CLI 均默认使用)")
        else:
            st.caption(
                f"尚无模板文件(预览按原样查询)。在下方编辑并保存后生成 `{tpl_path}`。"
            )

        tpl_text = st.text_area(
            "模板(每行一个,`{kw}` 为占位符;留空保存 = 拒绝,清空请用下方按钮)",
            key=editor_key,
            height=90,
        )
        tpl_stats = parse_templates_stats(tpl_text)
        st.caption(
            f"实时统计:有效模板 {tpl_stats['templates']} 个 · "
            f"跳过空行/注释/重复 {tpl_stats['dropped']} 行"
        )

        col_save, col_clear = st.columns(2)
        with col_save:
            if st.button("💾 保存查询模板", key="kw_templates_save"):
                try:
                    save_templates(data_dir, tpl_text)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()  # 刷新命令提示里的 --template-file 追加
        with col_clear:

            def _on_clear_templates() -> None:
                # on_click 回调在控件实例化**之前**执行:这里改编辑器
                # session_state 合法(handler 内直接改会抛
                # StreamlitWidgetAlreadyInstantiatedError)。
                st.session_state[editor_key] = ""

            if st.button(
                "🧹 清空模板(恢复原样查询)",
                key="kw_templates_clear",
                on_click=_on_clear_templates,
            ):
                try:
                    clear_templates(data_dir)
                except OSError as exc:
                    st.error(f"清空失败:{exc}")
                else:
                    st.rerun()


def _render_preview(data_dir: str, keywords_text: str) -> None:
    """页签①:展开预览——默认读持久模板,临时输入可覆盖(V10.20)。"""
    with st.expander("🔍 展开预览(关键词 × 模板 → 实际查询;纯本地,零外呼)"):
        templates = st.text_area(
            "临时模板覆盖(每行一个,`{kw}` 为占位符;留空 = 用上方持久模板,"
            "仍无则原样查询)",
            value="",
            height=90,
            key="kw_templates",
        )
        persistent = read_templates(data_dir)
        if templates.strip():
            effective, source = templates, "临时覆盖"
        elif persistent.strip():
            effective, source = persistent, "持久模板"
        else:
            effective, source = "", "原样查询"
        try:
            queries = preview_queries(keywords_text, effective)
            shown = queries[:PREVIEW_CAP]
            st.caption(
                f"模板来源:{source} · 共展开 {len(queries)} 条查询" + (
                    f"(仅展示前 {PREVIEW_CAP} 条)" if len(queries) > PREVIEW_CAP else ""
                )
            )
            st.code("\n".join(shown), language=None)
        except ValueError as exc:
            st.info(str(exc))


def _render_sets(data_dir: str) -> None:
    """页签②:词组库(清单/启用/载入编辑器/删除/新建/另存当前)。"""
    rows = list_sets(data_dir)
    st.subheader(f"词组库({len(rows)} 个)")
    if rows:
        header = {"name": "词组", "words": "词数", "mtime": "修改时间", "size": "字节"}
        st.table([header, *(  # type: ignore[list-item]
            {
                "name": r["name"],
                "words": r["words"] if r["words"] >= 0 else "解析失败",
                "mtime": r["mtime"],
                "size": r["size"],
            }
            for r in rows
        )])
    else:
        st.info("词组库为空。可在下方新建,或把'当前关键词'另存为词组。")

    st.divider()
    st.markdown("**逐组操作**")
    for row in rows:
        name = str(row["name"])
        col_pick, col_load, col_del, col_info = st.columns([1.2, 1.4, 1, 2.4])
        with col_pick:
            if st.button(f"✅ 启用:{name}", key=set_action_key("switch", name)):
                try:
                    switched = switch_active(data_dir, name)
                    st.session_state["kw_active_editor"] = read_active(data_dir)
                    st.success(
                        f"已切换当前关键词为词组“{name}”"
                        f"({switched['words']} 个词)——下次发现运行即生效"
                    )
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with col_load:
            if st.button(f"⬆️ 载入到编辑器:{name}", key=set_action_key("load", name)):
                st.session_state["kw_active_editor"] = read_set(data_dir, name)
                st.success(f"已把词组“{name}”载入编辑器(到'当前关键词'页保存生效)")
                st.rerun()
        with col_del:
            if st.button(f"🗑️ 删除:{name}", key=set_action_key("delete", name)):
                try:
                    delete_set(data_dir, name)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with col_info:
            bad = int(row["words"]) < 0
            st.caption(
                f"{row['mtime']} · {row['size']} 字节" + (" · ⚠️ 内容为空或全注释" if bad else "")
            )

    st.divider()
    col_new, col_save = st.columns(2)
    with col_new:
        st.markdown("**新建词组**")
        new_name = st.text_input(
            "词组名(字母/数字/下划线/连字符/中文,1-24 位;`active` 为保留名)",
            value="",
            key="kw_new_name",
        )
        new_text = st.text_area("关键词(每行一个)", value="", height=140, key="kw_new_text")
        if st.button("➕ 创建词组", key="kw_create"):
            try:
                made = create_set(data_dir, new_name, new_text)
            except ValueError as exc:
                st.error(str(exc))
            else:
                # V10.20:成功后清空输入并刷新词组表(本轮表格渲染于按钮之前,
                # 不 rerun 则新建词组直到下一次交互才出现)。
                st.session_state["kw_new_name"] = ""
                st.session_state["kw_new_text"] = ""
                st.rerun()
    with col_save:
        st.markdown("**把当前关键词另存为词组**")
        as_name = st.text_input("新词组名", value="", key="kw_as_name")
        if st.button("📦 另存当前为词组", key="kw_save_as"):
            try:
                save_active_as_set(data_dir, as_name)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state["kw_as_name"] = ""
                st.rerun()


def _render_help(data_dir: str) -> None:
    """页签③:红线与用法说明。"""
    st.markdown(
        "### 红线与用法\n\n"
        "- **零内置关键词(红线 28)**:本页与代码都不预置任何搜索词;"
        "所有关键词由你显式提供,保存/新建/切换一律经发现子系统装载校验"
        "(为空即拒绝)。查询模板同理(V10.20):代码零内置模板,"
        "保存经 ``load_templates`` 同源校验。\n"
        "- **页面零外呼**:本页只做本地文件读写与纯函数展开预览,"
        "**绝不发起搜索/发现**;发现须在 CLI 显式运行,且受"
        " `discovery_online`(默认关)与引擎密钥门槛约束。\n"
        "- **实时生效**:`active.txt` 是发现命令消费的唯一活文件,"
        "发现每次运行都重新装载——编辑/切换保存后,**下一次发现运行即生效**,"
        "无需重启服务。模板活文件 `templates.txt` 同理(V10.20):"
        "发现 CLI 加 `--template-file` 即消费,命令提示会自动追加。\n"
        "- **原子与备份**:覆盖保存走原子替换(Windows 重试),"
        "旧内容滚动备份为同目录 `.bak`(单份;清空模板保留 `.bak` 作后悔药)。\n"
        "- **数据不入库**:关键词文件位于数据目录"
        f"(`{keywords_root(data_dir)}`),随 `data/` 整体被版本库忽略"
        "(搜索意图属敏感信息)。\n"
        "- **礼貌与授权**:关键词只用于发现**你有权核查**的目标线索;"
        "批量导入前请复核清单(watchlist 同口径)。"
    )


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,关键词页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/keywords_page.py",
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
