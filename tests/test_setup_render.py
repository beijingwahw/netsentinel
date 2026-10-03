# -*- coding: utf-8 -*-
"""netsentinel/setup/render.py 渲染注入测试(A156,离线,零外呼)。

覆盖契约三处注入 + 容错:

- 横幅两态:``active`` 非空 → ``当前视觉模型:…``(html.escape);None/空串/空白
  → 保持"尚未连接视觉模型";
- 状态 JSON 节点 ``ns-state``:位于 ``</head>`` 之前、全文唯一;``json.loads``
  可回读且与输入一致(``ensure_ascii=False`` 中文原样);
- ``</script>`` 注入防护(专项):恶意键名/值/active/local 均不得产生
  ``</script><script`` 逃逸序列,回读仍完整;
- local_models 初始行:local-list 容器内注入、字段逐项转义、``ok=False`` 跳过、
  字符串与 ``{"id"}`` 对象模型名兼容、空/None 不注入;
- 坏输入容错:status None/非 dict、active 非字符串、local_models 非 list、
  嵌套 >10 截断(含自引用)、A146 页面缺席抛中文 RuntimeError;
- 确定性 / 体积 / 模块纯净(page 惰性导入,顶层仅标准库)。

全程不联网、不启动服务器、不渲染浏览器。
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re
import sys

import pytest

from netsentinel.setup import render as render_mod
from netsentinel.setup.page import STUB_NOTE_TEXT
from netsentinel.setup.render import (
    MAX_DEPTH,
    STATE_SCRIPT_ID,
    render_page,
    state_payload,
)

#: 页面自带脚本块(内联 JS)数量;渲染后 = 模板 1 + ns-state 1。
_PAGE_SCRIPTS = 2

_STATE_NODE_RE = re.compile(
    r'<script id="' + STATE_SCRIPT_ID + r'" type="application/json">([\s\S]*?)</script>'
)
_BANNER_RE = re.compile(
    r'<div\b[^>]*\bdata-testid="active-banner"[^>]*>([\s\S]*?)</div>'
)
_ROW_MODEL_RE = re.compile(r'data-testid="row-model">([^<]*)</span>')


def _state_text(rendered: str) -> str:
    """抽取 ns-state 节点原始文本(未解析)。"""
    match = _STATE_NODE_RE.search(rendered)
    assert match, "ns-state 状态节点缺失"
    return match.group(1)


def _banner(rendered: str) -> str:
    """抽取切换横幅元素的内部文本。"""
    match = _BANNER_RE.search(rendered)
    assert match, "active-banner 横幅缺失"
    return match.group(1)


def _local_region(rendered: str) -> str:
    """抽取 local-list 容器内、行模板之后到卡片结束的静态行区域。"""
    anchor = rendered.find('data-testid="local-list"')
    assert anchor != -1, "local-list 容器缺失"
    close = rendered.find("</template>", anchor)
    assert close != -1, "行模板缺失"
    end = rendered.find("</section>", close)
    assert end != -1
    return rendered[close + len("</template>") : end]


def _deep_dict(levels: int) -> dict:
    """构造指定层数的嵌套字典(最深处置值)。"""
    root: dict = {}
    node = root
    for _ in range(levels):
        node["d"] = {}
        node = node["d"]
    node["leaf"] = "深处的值"
    return root


# ---------------------------------------------------------------------------
# 整体结构:完整 HTML / 三处注入均在位
# ---------------------------------------------------------------------------


def test_returns_full_html_with_all_injections() -> None:
    rendered = render_page(
        status={"active": "ollama:llava"},
        active="ollama:llava",
        local_models=[
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
             "models": ["llava"], "ok": True}
        ],
    )
    assert rendered.lstrip().lower().startswith("<!doctype html>")
    assert rendered.rstrip().endswith("</html>")
    # 红线 34 文案在注入后依然完整保留
    assert STUB_NOTE_TEXT in rendered
    assert rendered.count(f'<script id="{STATE_SCRIPT_ID}"') == 1
    assert rendered.count('data-testid="local-row"') == 2  # 模板 1 + 注入 1


def test_state_node_inserted_before_head_close() -> None:
    rendered = render_page(status={"k": "v"}, active="glm:glm-4v", local_models=[])
    node_at = rendered.find(f'<script id="{STATE_SCRIPT_ID}"')
    head_open = rendered.find("<head>")
    head_close = rendered.find("</head>")
    assert head_open != -1 and node_at != -1 and head_close != -1
    assert head_open < node_at < head_close, "状态节点必须位于 </head> 之前"
    assert rendered.count("</head>") == 1
    # 模板脚本 + 状态节点,恰两个 <script
    assert rendered.count("<script") == _PAGE_SCRIPTS


# ---------------------------------------------------------------------------
# ① 横幅两态
# ---------------------------------------------------------------------------


def test_banner_default_when_active_none() -> None:
    for active in (None,):
        rendered = render_page(active=active)
        assert _banner(rendered) == "尚未连接视觉模型"
        assert "当前视觉模型" not in rendered


def test_banner_active_shows_spec() -> None:
    rendered = render_page(active="ollama:llava")
    assert _banner(rendered) == "当前视觉模型:ollama:llava"
    # 横幅元素内默认文案被替换(内联 JS 分支中的同文案不受影响,允许保留)
    assert _banner(rendered) != "尚未连接视觉模型"


def test_banner_active_html_escaped() -> None:
    evil = '<img src=x onerror="alert(1)"><script>evil()</script>'
    rendered = render_page(active=evil)
    banner = _banner(rendered)
    assert banner.startswith("当前视觉模型:&lt;img")
    # 横幅内不得出现任何裸标签(状态 JSON 数据块中的原文由专项防护测试覆盖:
    # 其内 "</" 一律转义,无法闭合脚本块,且 type=application/json 不执行)
    assert "<img" not in banner
    assert "<script>evil" not in banner


def test_banner_empty_or_whitespace_active_keeps_default() -> None:
    for active in ("", "   "):
        rendered = render_page(active=active)
        assert _banner(rendered) == "尚未连接视觉模型"
        assert "当前视觉模型" not in rendered


# ---------------------------------------------------------------------------
# ② 状态 JSON:回读 / 中文原样 / 注入防护
# ---------------------------------------------------------------------------


def test_state_json_roundtrip() -> None:
    status = {
        "active": "ollama:llava",
        "switched_by": "wizard",
        "switched_at": 1770000000,
        "keys": {"glm": True, "openai": False},
        "local_error": None,
    }
    local = [
        {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
         "models": ["llava", "llama3.2-vision"], "ok": True}
    ]
    rendered = render_page(status=status, active="ollama:llava", local_models=local)
    payload = state_payload(rendered)
    assert set(payload) == {"status", "active", "local"}
    assert payload["status"] == status
    assert payload["active"] == "ollama:llava"
    assert payload["local"] == local
    assert isinstance(payload, dict)


def test_state_json_ensure_ascii_false_chinese_literal() -> None:
    rendered = render_page(status={"note": "尚未连接视觉模型,可用离线桩兜底"})
    text = _state_text(rendered)
    assert "尚未连接视觉模型" in text
    assert "\\u" not in text, "ensure_ascii=False 不应产生 \\uXXXX 转义"
    assert state_payload(rendered)["status"]["note"] == "尚未连接视觉模型,可用离线桩兜底"


def test_script_injection_guard_no_escape_sequence() -> None:
    """专项:恶意键名/值/active/local 组合下,输出不得出现 </script><script。"""
    evil_key = "</script><script>alert(1)</script>"
    rendered = render_page(
        status={evil_key: "x", "note": "</script><script>alert(2)</script>"},
        active="</script><script>alert(3)</script>",
        local_models=[
            {"provider": "</script><script>alert(4)</script>",
             "base_url": "http://127.0.0.1:11434",
             "models": ["</script><script>alert(5)</script>"], "ok": True}
        ],
    )
    assert "</script><script" not in rendered
    assert "</script><" not in rendered, "任何闭合脚本标签后紧跟标签的开头都应被阻断"
    # 防护手段落地:序列化文本中 </ 一律变为 <\/
    assert "<\\/" in _state_text(rendered)
    # 回读仍成功:被防护的值完整还原
    payload = state_payload(rendered)
    assert payload["active"] == "</script><script>alert(3)</script>"
    assert payload["status"]["note"] == "</script><script>alert(2)</script>"


def test_malicious_key_name_neutralized_and_roundtrips() -> None:
    evil_key = "</script><script>alert(1)"
    rendered = render_page(status={evil_key: "值"})
    # 键名经 html.escape:原文中不存在恶意键的裸形态
    assert evil_key not in rendered
    payload = state_payload(rendered)
    assert list(payload["status"]) == ["&lt;/script&gt;&lt;script&gt;alert(1)"]


def test_value_with_close_tag_roundtrips() -> None:
    rendered = render_page(status={"v": "x</script>y"})
    assert "x<\\/script>y" in _state_text(rendered)
    assert state_payload(rendered)["status"]["v"] == "x</script>y"


# ---------------------------------------------------------------------------
# ③ local_models 初始行
# ---------------------------------------------------------------------------


def test_local_rows_injected_inside_container() -> None:
    rendered = render_page(
        local_models=[
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
             "models": ["llava", "qwen2.5vl"], "ok": True}
        ]
    )
    region = _local_region(rendered)
    assert region.count('data-testid="local-row"') == 2
    models = _ROW_MODEL_RE.findall(region)
    assert models == ["llava", "qwen2.5vl"], "行顺序应与 models 顺序一致"
    assert 'data-testid="row-provider">ollama<' in region
    assert "http://127.0.0.1:11434" in region
    # 全文 = 模板 1 + 注入 2
    assert rendered.count('data-testid="local-row"') == 3


def test_local_rows_fields_escaped() -> None:
    rendered = render_page(
        local_models=[
            {"provider": '<a href="j">提供商</a>', "base_url": "http://127.0.0.1:1234",
             "models": ["<b>llava&vision</b>"], "ok": True}
        ]
    )
    region = _local_region(rendered)
    assert "&lt;b&gt;llava&amp;vision&lt;/b&gt;" in region
    assert "&lt;a href=&quot;j&quot;&gt;提供商&lt;/a&gt;" in region
    assert "<b>llava" not in region
    assert "<a href" not in region


def test_local_rows_skip_ok_false_services() -> None:
    rendered = render_page(
        local_models=[
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
             "models": ["llava"], "ok": True},
            {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234",
             "models": ["gemma3-vision"], "ok": False},
        ]
    )
    region = _local_region(rendered)
    assert region.count('data-testid="local-row"') == 1
    assert "llava" in region and "gemma3-vision" not in region


def test_local_rows_accept_strings_and_id_objects() -> None:
    rendered = render_page(
        local_models=[
            "llava-plain",
            {"provider": "lmstudio", "base_url": "http://127.0.0.1:1234",
             "models": [{"id": "qwen2.5vl"}, {"name": "minicpm-v"}, "gemma3-vision"],
             "ok": True},
        ]
    )
    models = _ROW_MODEL_RE.findall(_local_region(rendered))
    assert models == ["llava-plain", "qwen2.5vl", "minicpm-v", "gemma3-vision"]


def test_no_local_rows_when_empty_or_none() -> None:
    for local in (None, []):
        rendered = render_page(local_models=local)
        assert 'data-testid="local-row"' not in _local_region(rendered)
        assert rendered.count('data-testid="local-row"') == 1  # 仅模板一处


# ---------------------------------------------------------------------------
# 坏输入容错 / 深度截断
# ---------------------------------------------------------------------------


def test_bad_status_tolerated() -> None:
    for bad in (None, "坏输入", 123, ["x"], ("a",), object()):
        rendered = render_page(status=bad)  # type: ignore[arg-type]
        assert rendered.lstrip().lower().startswith("<!doctype html>")
        assert state_payload(rendered)["status"] == {}


def test_bad_local_models_tolerated() -> None:
    for bad in ("junk", 42, {"provider": "ollama"}, [None, 3, {"ok": False}]):
        rendered = render_page(local_models=bad)  # type: ignore[arg-type]
        assert 'data-testid="local-row"' not in _local_region(rendered)
        assert rendered.count('data-testid="local-row"') == 1


def test_deep_nesting_truncated_beyond_max_depth() -> None:
    rendered = render_page(status={"deep": _deep_dict(15), "flat": {"a": {"b": "ok"}}})
    payload = state_payload(rendered)
    assert "(嵌套过深,已截断)" in _state_text(rendered)
    # 浅层内容不受影响
    assert payload["status"]["flat"] == {"a": {"b": "ok"}}
    node = payload["status"]["deep"]
    hops = 0
    while isinstance(node, dict):
        node = node["d"]
        hops += 1
    # status 为深度 0,deep 链上深度 1..10 的字典保留,第 11 层的值被截断
    assert node == "(嵌套过深,已截断)"
    assert hops == MAX_DEPTH


def test_self_referencing_status_terminated() -> None:
    loop: dict = {}
    loop["self"] = loop
    rendered = render_page(status=loop)
    assert "(嵌套过深,已截断)" in _state_text(rendered)
    state_payload(rendered)  # 仍是合法 JSON


def test_non_string_active_tolerated() -> None:
    for bad in (123, True, ["ollama:llava"], {"s": 1}):
        rendered = render_page(active=bad)  # type: ignore[arg-type]
        assert _banner(rendered) == "尚未连接视觉模型"
        assert state_payload(rendered)["active"] is None


# ---------------------------------------------------------------------------
# 页面缺席 / 确定性 / 体积 / 模块纯净
# ---------------------------------------------------------------------------


def test_page_absent_raises_chinese_runtimeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "netsentinel.setup.page", None)
    with pytest.raises(RuntimeError) as excinfo:
        render_page()
    message = str(excinfo.value)
    assert "netsentinel.setup.page" in message
    assert "未就位" in message
    assert re.search(r"[\u4e00-\u9fff]", message), "错误消息应为中文"


def test_deterministic_same_input_same_output() -> None:
    kwargs = {
        "status": {"active": "glm:glm-4v", "keys": {"glm": True}},
        "active": "glm:glm-4v",
        "local_models": [
            {"provider": "ollama", "base_url": "http://127.0.0.1:11434",
             "models": ["llava"], "ok": True}
        ],
    }
    assert render_page(**kwargs) == render_page(**kwargs)  # type: ignore[arg-type]


def test_size_reasonable() -> None:
    plain = len(render_page().encode("utf-8"))
    assert 4 * 1024 <= plain < 200 * 1024, plain
    many = render_page(
        local_models=[
            {"provider": f"p{i}", "base_url": "http://127.0.0.1:11434",
             "models": [f"model-{i}-{j}" for j in range(10)], "ok": True}
            for i in range(6)
        ]
    )
    assert plain <= len(many.encode("utf-8")) < 200 * 1024


def test_signature_all_optional() -> None:
    params = inspect.signature(render_page).parameters
    assert list(params) == ["status", "active", "local_models"]
    assert all(p.default is None for p in params.values())


def test_module_purity_lazy_page_import_only() -> None:
    src = pathlib.Path(render_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 顶层仅标准库(html/json/re + __future__)
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(a.name in {"__future__", "html", "json", "re"} for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module in {"__future__", "html", "json", "re"}
    # page 必须惰性导入(不在顶层),且确实被导入
    top_modules = [
        n.module for n in tree.body if isinstance(n, ast.ImportFrom)
    ]
    assert "netsentinel.setup.page" not in top_modules
    lazy = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "netsentinel.setup.page"
    ]
    assert lazy, "netsentinel.setup.page 必须函数内惰性导入"
    # 不读环境 / 文件 / 网络
    for bad in ("environ", "argv", "open(", "urllib", "socket", "requests",
                "subprocess"):
        assert bad not in src, bad
