"""V10.20 Seat C:webui 八页面 AppTest 真渲染冒烟(渲染期回归网)。

背景:V10.19 实跑抓到两处"只有渲染时才炸"的 UI 真 bug(页签控件 key 碰撞、
证据图相对路径双拼)——静态导入与纯逻辑测试都覆盖不到渲染分支。本文件用
streamlit 官方 AppTest 通道(``streamlit.testing.v1.AppTest``)对全部 8 个
页面做**真渲染**(脚本线程完整执行:set_page_config / tabs 全页签体 / 控件
注册 / 数据装载),任何渲染期未捕获异常都会以 ``at.exception`` 形式暴露。

配套修复(V10.20,八个页面 ``__main__`` 块):``raise SystemExit(main())``
属 BaseException,会杀死 streamlit 脚本线程(收尾事件不发),AppTest 通道
直接 KeyError 失效——已改为"仅真直跑(无脚本上下文)才转进程退出码",
本文件的 8 个用例即是对该修复的锁定。

约定:
- streamlit 缺席时整体跳过(importorskip);
- ``NETSENTINEL_DATA_DIR`` 指向 tmp_path(绝不读写真实 data/);
- AppTest 版本随 streamlit ≥1.28 提供,老版本跳过;
- 断言:渲染零异常 + 页面标题存在(title 元素非空)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 八个独立入口页面(与 webui/README.md 页面清单一致)。
_RENDER_PAGES: tuple[tuple[str, str], ...] = (
    ("assistant_page.py", "净网助手"),
    ("app.py", "复核台"),
    ("dashboard.py", "运营仪表盘"),
    ("keywords_page.py", "关键词工作台"),
    ("label_page.py", "标注工作台"),
    ("providers_page.py", "提供方面板"),
    ("groups_page.py", "分组复核页"),
    ("models_page.py", "模型页"),
    ("runs_page.py", "最近运行页"),
    ("draft_flow_page.py", "门户草稿工作流"),
)


def _apptest():
    """取 AppTest 类;streamlit / testing API 缺席时跳过用例。"""
    pytest.importorskip("streamlit", reason="需要 streamlit 才能做真渲染冒烟")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:  # pragma: no cover - streamlit < 1.28 无 testing.v1
        pytest.skip("streamlit.testing.v1.AppTest 不可用(需 streamlit >= 1.28)")
    return AppTest


@pytest.fixture()
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录(含复核队列库/关键词目录)指到 tmp_path,不碰真实 data/。"""
    data = tmp_path / "smoke_data"
    data.mkdir()
    monkeypatch.setenv("NETSENTINEL_DATA_DIR", str(data))
    monkeypatch.chdir(_PROJECT_ROOT)
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    return data


@pytest.mark.parametrize(("script", "name"), _RENDER_PAGES, ids=[p[0] for p in _RENDER_PAGES])
def test_render_smoke_no_exception(script: str, name: str, _isolated_data_dir: Path) -> None:
    """页面真渲染零异常 + 标题存在(空数据目录下的空态渲染一并覆盖)。"""
    AppTest = _apptest()
    at = AppTest.from_file(str(_PROJECT_ROOT / "webui" / script), default_timeout=90)
    at.run()
    assert not at.exception, f"{script}({name}) 渲染期异常:{[e.message for e in at.exception]}"
    assert at.title, f"{script}({name}) 未渲染出页面标题"


def test_render_smoke_direct_run_still_exits(_isolated_data_dir: Path) -> None:
    """V10.20 入口契约:``python webui/app.py`` 直跑仍以退出码 1 结束(缺依赖提示口径)。

    SystemExit 收尾修复只影响 streamlit 脚本线程内;真直跑(无 ScriptRunContext)
    必须保留"main() 退出码 → 进程退出码"的既有行为。
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, "webui", "app.py"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env={"NETSENTINEL_DATA_DIR": str(_isolated_data_dir), "PATH": ""},
    )
    # 有 streamlit 时直跑是 bare-mode(退出码 0);无 streamlit 时退出码 1。
    # 两种环境都不得崩溃(非零非 1 的意外退出码)。
    assert proc.returncode in (0, 1), f"直跑出现意外退出码 {proc.returncode}:{proc.stderr[-300:]}"
