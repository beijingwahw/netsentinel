"""netsentinel/setup/page.py 纯字符串断言测试(A146,离线,零外呼)。

- 只测 ``page_html()`` 返回的单文件 HTML 字符串:全部 data-testid 存在且唯一、
  密钥输入 password 语义(红线 33)、离线桩红字"离线桩,非模型判定"(红线 34)、
  20 家云下拉与真实目录键集一致(``importorskip`` 保护 + 内置清单兜底断言)、
  无任何 http(s) 外链 / 无框架、模板为无参数纯函数(无用户输入拼接)、
  确定性与体积上限;
- 全程不联网、不启动服务器、不渲染浏览器;对兄弟模块
  ``netsentinel.vision.providers`` 仅惰性导入比对键集。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import sys

import pytest

from netsentinel.setup import page as setup_page
from netsentinel.setup.page import BUILTIN_PROVIDER_KEYS, STUB_NOTE_TEXT, page_html

#: 任务书要求的全部 data-testid(11 项)。
REQUIRED_TESTIDS = (
    "active-banner",
    "status-card",
    "btn-probe",
    "local-list",
    "btn-enable",
    "cloud-select",
    "key-input",
    "btn-setkey",
    "btn-test",
    "btn-stub",
    "warn-area",
)

#: 五个向导 API 端点(A145 契约)。
API_ENDPOINTS = (
    "/api/status",
    "/api/probe",
    "/api/activate",
    "/api/setkey",
    "/api/test",
)


def _cloud_options(html: str) -> list[str]:
    """从 HTML 中抽取 cloud-select 下拉的全部 option value。"""
    m = re.search(
        r'<select[^>]*data-testid="cloud-select"[^>]*>([\s\S]*?)</select>', html
    )
    assert m, "cloud-select 下拉缺失"
    return re.findall(r'<option value="([^"]*)">', m.group(1))


# ---------------------------------------------------------------------------
# 文档结构与必备 testid
# ---------------------------------------------------------------------------


def test_doctype_charset_and_lang() -> None:
    html = page_html()
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html
    assert '<html lang="zh-CN">' in html


def test_header_title_literal() -> None:
    html = page_html()
    expect = "净网哨兵 · 视觉模型连接向导/切换器"
    assert f"<title>{expect}</title>" in html
    m = re.search(r"<h1>([^<]*)</h1>", html)
    assert m and m.group(1).strip() == expect


def test_all_required_testids_present_once() -> None:
    html = page_html()
    for tid in REQUIRED_TESTIDS:
        assert f'data-testid="{tid}"' in html, tid
        # 全文恰好出现一次(JS 经选择器拼接触达,不产生重复字面量)
        assert html.count(f'data-testid="{tid}"') == 1, tid


def test_active_banner_default_text() -> None:
    html = page_html()
    m = re.search(r'data-testid="active-banner"[^>]*>\s*尚未连接视觉模型\s*<', html)
    assert m, "切换横幅默认文案应为“尚未连接视觉模型”"


def test_status_card_fields() -> None:
    html = page_html()
    assert 'data-testid="status-card"' in html
    for tid in ("status-active", "status-source", "status-time"):
        assert html.count(f'data-testid="{tid}"') == 1
    for label in ("活动模型", "切换来源", "切换时间"):
        assert label in html


def test_local_list_row_template() -> None:
    html = page_html()
    tpl = re.search(r"<template[^>]*>([\s\S]*?)</template>", html)
    assert tpl, "local-list 内应内嵌行模板 <template>"
    body = tpl.group(1)
    for tid in ("local-row", "row-provider", "row-model", "row-base", "btn-enable"):
        assert f'data-testid="{tid}"' in body, tid
    m = re.search(r'data-testid="btn-enable"[^>]*>\s*启用\s*<', body)
    assert m, "行模板启用按钮文案应为“启用”"
    # 模板位于列表容器内部
    li = html.find('data-testid="local-list"')
    assert li != -1 and html.find("<template", li) != -1


def test_button_labels_literal() -> None:
    html = page_html()
    for tid, text in (
        ("btn-probe", "扫描本机服务"),
        ("btn-setkey", "保存密钥"),
        ("btn-test", "测试连接"),
        ("btn-stub", "使用离线桩继续"),
    ):
        m = re.search(
            rf'data-testid="{tid}"[^>]*>\s*{re.escape(text)}\s*<', html
        )
        assert m, (tid, text)


def test_warn_area_empty_with_aria_live() -> None:
    html = page_html()
    m = re.search(r'<[^>]*data-testid="warn-area"[^>]*>', html)
    assert m and "aria-live" in m.group(0)
    start = html.find(">", m.start()) + 1
    end = html.find("<", start)
    assert html[start:end].strip() == "", "警示区默认应为空,内容仅由 JS 写入"


# ---------------------------------------------------------------------------
# 红线 33:密钥只进不显
# ---------------------------------------------------------------------------


def test_key_input_is_password_redline33() -> None:
    html = page_html()
    m = re.search(r'<input\b[^>]*data-testid="key-input"[^>]*>', html)
    assert m, "key-input 缺失"
    tag = m.group(0)
    assert 'type="password"' in tag
    assert 'autocomplete="new-password"' in tag
    assert 'type="text"' not in tag
    assert "value=" not in tag, "密钥输入不得预置 value"
    # 全页唯一输入框,且即密码语义
    assert html.count("<input") == 1


def test_key_never_echoed_in_dom_redline33() -> None:
    html = page_html()
    # 1) 对输入框的赋值只允许“清空”
    assigns = re.findall(r"keyInput\.value\s*=\s*([^;\n]+)", html)
    assert assigns, "保存密钥后应清空输入框"
    for rhs in assigns:
        assert rhs.strip() == '""', rhs
    # 2) 不存在把密钥变量写入任何 DOM 节点的路径
    assert not re.search(r"textContent\s*=\s*[^;]*\bkey\b", html)
    # 3) 成功路径只显示服务端掩码,不显示本体
    assert "masked" in html and "掩码" in html
    # 4) 密钥仅进入 JSON 请求体
    assert "JSON.stringify" in html


def test_dom_updates_via_textcontent_only() -> None:
    html = page_html()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert bad not in html, bad
    assert html.count(".textContent") >= 3
    assert "document.createElement" in html


# ---------------------------------------------------------------------------
# 红线 34:离线桩明示
# ---------------------------------------------------------------------------


def test_stub_warning_literal_redline34() -> None:
    html = page_html()
    assert STUB_NOTE_TEXT == "离线桩,非模型判定"
    assert STUB_NOTE_TEXT in html
    # 静态红字 + JS 启用后警示 + 页脚纪律声明,至少两处
    assert html.count(STUB_NOTE_TEXT) >= 2
    # 固定红字紧邻离线桩按钮
    i = html.find('data-testid="btn-stub"')
    j = html.find(STUB_NOTE_TEXT)
    assert i != -1 and j != -1 and i < j and j - i < 200
    # 红字样式为红色加粗
    m = re.search(r"\.stub-note\s*\{[^}]*\}", html)
    assert m and "#c62828" in m.group(0) and "font-weight" in m.group(0)


# ---------------------------------------------------------------------------
# 云下拉:20 家、与真实目录键一致、缺席兜底
# ---------------------------------------------------------------------------


def test_cloud_select_twenty_options_builtin() -> None:
    html = page_html()
    opts = _cloud_options(html)
    assert len(opts) == 20, len(opts)
    assert len(set(opts)) == 20, "选项不得重复"
    assert set(opts) == set(BUILTIN_PROVIDER_KEYS)
    for value in opts:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", value), value


def test_cloud_select_matches_real_providers_catalog() -> None:
    providers = pytest.importorskip("netsentinel.vision.providers")
    assert len(providers.PROVIDERS) == 20
    opts = _cloud_options(page_html())
    assert set(opts) == set(providers.PROVIDERS.keys())


def test_cloud_select_fallback_when_catalog_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 让惰性导入失败(目录缺席),页面应回退内置静态清单且仍为 20 家
    monkeypatch.setitem(sys.modules, "netsentinel.vision.providers", None)
    opts = _cloud_options(page_html())
    assert len(opts) == 20
    assert set(opts) == set(BUILTIN_PROVIDER_KEYS)


# ---------------------------------------------------------------------------
# 无外链 / 无框架 / 内联资源
# ---------------------------------------------------------------------------


def test_no_external_links_or_frameworks() -> None:
    html = page_html()
    assert re.findall(r'\bsrc="([^"]*)"', html) == []
    assert re.findall(r'\bhref="([^"]*)"', html) == []
    low = html.lower()
    assert "http" not in low, "页面不得出现任何 http(s) 字样"
    for tag in ("<iframe", "<img", "<link", "<base", "<object", "<embed"):
        assert tag not in low, tag
    for fw in ("react", "vue", "angular", "jquery", "bootstrap", "tailwind",
               "unpkg", "jsdelivr", "cdnjs", "googleapis"):
        assert fw not in low, fw


def test_single_inline_style_and_script() -> None:
    html = page_html()
    assert html.count("<style") == 1 and html.count("</style>") == 1
    assert html.count("<script") == 1 and html.count("</script>") == 1
    assert not re.search(r"<script[^>]*\ssrc=", html), "脚本必须内联"
    assert not re.search(r"<style[^>]*\shref=", html), "样式必须内联"
    assert 'type="module"' not in html


# ---------------------------------------------------------------------------
# JS 行为(挂载 / 五个 API / JSON 容错)
# ---------------------------------------------------------------------------


def test_js_fetches_all_five_apis() -> None:
    html = page_html()
    js = html[html.find("<script") :]
    for ep in API_ENDPOINTS:
        assert f'"{ep}"' in js, ep
    # status 走 GET;其余四个端点均经共享的 postJson 辅助函数以 POST 发出
    assert 'getJson("/api/status")' in js
    for ep in ("/api/probe", "/api/activate", "/api/setkey", "/api/test"):
        assert f'postJson("{ep}"' in js, ep
    assert js.count('method: "POST"') == 1  # 仅共享辅助函数内一处
    assert "fetch(" in js
    assert "XMLHttpRequest" not in js


def test_json_tolerance_no_native_dialogs() -> None:
    html = page_html()
    js = html[html.find("<script") :]
    # JSON 解析失败回退 null,由警示区呈现中文错误
    assert ".catch(" in js and "return null" in js
    assert js.count(".then(") >= 7, "应对成功/失败分支分别挂载处理器"
    for bad in ("alert(", "confirm(", "prompt(", "document.write", "eval("):
        assert bad not in html, bad
    # DOM 就绪挂载
    assert "DOMContentLoaded" in js and "addEventListener" in js


# ---------------------------------------------------------------------------
# 纯函数:确定性 / 无用户输入拼接 / 模块纯净
# ---------------------------------------------------------------------------


def test_pure_function_deterministic() -> None:
    assert list(inspect.signature(page_html).parameters) == []
    first = page_html()
    second = page_html()
    assert first == second and first.strip()


def test_size_reasonable() -> None:
    size = len(page_html().encode("utf-8"))
    assert 4 * 1024 <= size < 200 * 1024, size


def test_module_purity_lazy_import_only() -> None:
    src = pathlib.Path(setup_page.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods: set[str] = set()
    lazy = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
            if node.module == "netsentinel.vision.providers":
                lazy = True
    # 模块级仅标准库 html;providers 目录必须函数内惰性导入
    assert mods <= {"__future__", "html", "netsentinel.vision.providers"}
    assert lazy
    top_level_imports = [
        n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    assert all(
        not (isinstance(n, ast.ImportFrom) and n.module == "netsentinel.vision.providers")
        for n in top_level_imports
    ), "providers 必须惰性导入,不得出现在模块顶层"
    # 不读环境 / 参数 / 网络 / 文件:模板层无用户输入可拼接
    for bad in ("environ", "argv", "input(", "open(", "urllib", "socket", "requests"):
        assert bad not in src, bad
