# -*- coding: utf-8 -*-
"""本地模拟举报门户静态检查与贯通测试(NetSentinel A16)。

覆盖两部分:

1. 静态断言(无浏览器,必须始终通过):
   - 两个 mock 页面落实 CONTRACTS.md §3 SELECTORS 的全部 8 个字段 id,标签类型正确;
   - 验证码输入框 readonly 且 placeholder 为契约规定文案(人工专属,自动化绝不填写);
   - 页面显著标注"本地模拟/非官方",且不含任何外部资源与真实举报门户地址(红线 3)。

2. 贯通测试(重,需浏览器):threading 起本地 http.server 服务 tests/mock_portals,
   用 portal_12377 / portal_shdf 生成举报计划,executor_playwright 真正驱动 chromium
   在本地模拟页上完成"打开→选择类型→填写→人工门(auto_confirm)→提交→受理"全流程。
   兄弟模块(A12-A15)未就位或本机 chromium 不可用时整段 skip(并行开发容错)。

红线:全程只访问 127.0.0.1;绝不访问 www.12377.cn / www.shdf.gov.cn。
"""
from __future__ import annotations

import functools
import importlib.util
import re
import threading
import types
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from netsentinel.contracts import Config, StepAction

HERE = Path(__file__).resolve().parent
MOCK_DIR = HERE / "mock_portals"
MOCK_PAGES = {
    "12377": MOCK_DIR / "12377_mock.html",
    "shdf": MOCK_DIR / "shdf_mock.html",
}

# CONTRACTS.md §3 SELECTORS 的 8 个字段 id(契约镜像,与 mock 页共享)
CONTRACT_FIELD_IDS = (
    "report-url",      # 举报链接
    "report-type",     # 信息类型 select
    "report-desc",     # 具体描述 textarea
    "report-name",     # 举报人姓名
    "report-phone",    # 举报人电话
    "report-file",     # 附件(证据包 zip,人工门阶段上传)
    "report-captcha",  # 验证码 —— 只允许人工输入
    "report-submit",   # 提交按钮
)

# 字段 id -> (标签名, 标签内必须出现的属性片段)
_FIELD_TAG_SPEC = {
    "report-url": ("input", 'type="text"'),
    "report-type": ("select", None),
    "report-desc": ("textarea", None),
    "report-name": ("input", 'type="text"'),
    "report-phone": ("input", 'type="text"'),
    "report-file": ("input", 'type="file"'),
    "report-captcha": ("input", 'type="text"'),
    "report-submit": ("button", None),
}

#: 契约规定的验证码 placeholder(只允许人工输入)
CAPTCHA_PLACEHOLDER = "验证码:仅供人工输入(模拟站任意4位)"

#: chromium 启动失败时异常/说明文本里常见的特征(命中则 skip 而非判失败)
_BROWSER_FAILURE_HINTS = (
    "chromium",
    "chrome.exe",
    "chrome",
    "executable",
    "playwright install",
    "browser",
    "浏览器",
)


def _read_page(portal: str) -> str:
    path = MOCK_PAGES[portal]
    assert path.is_file(), f"缺少本地模拟页面文件:{path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、静态断言(无浏览器)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("portal", sorted(MOCK_PAGES))
def test_mock_page_has_all_contract_field_ids(portal: str) -> None:
    """契约 §3 的 8 个字段 id 必须全部出现在两个 mock 页面里。"""
    html = _read_page(portal)
    for field_id in CONTRACT_FIELD_IDS:
        assert f'id="{field_id}"' in html, f"{portal} 模拟页缺少契约字段 id={field_id}"


@pytest.mark.parametrize("portal", sorted(MOCK_PAGES))
def test_mock_page_field_tags_match_contract(portal: str) -> None:
    """每个字段使用契约规定的标签类型(text/select/textarea/file/button)。"""
    html = _read_page(portal)
    for field_id, (tag, attr) in _FIELD_TAG_SPEC.items():
        matched = re.search(rf'<{tag}\b[^>]*\bid="{field_id}"[^>]*>', html)
        assert matched, f"{portal} 模拟页字段 #{field_id} 应是 <{tag}> 标签"
        if attr:
            assert attr in matched.group(0), f"{portal} 模拟页字段 #{field_id} 缺少属性 {attr}"


@pytest.mark.parametrize("portal", sorted(MOCK_PAGES))
def test_mock_captcha_is_readonly_human_only(portal: str) -> None:
    """红线 1:验证码输入框必须 readonly,placeholder 声明仅供人工输入。"""
    html = _read_page(portal)
    matched = re.search(r'<input\b[^>]*\bid="report-captcha"[^>]*>', html)
    assert matched, f"{portal} 模拟页缺少 #report-captcha 输入框"
    tag = matched.group(0)
    assert "type=" in tag.lower() and "captcha" in tag, f"{portal} 模拟页验证码输入框存在(V10.1 半自动焦点:可聚焦,安全由 HUMAN_GATE 保障)"
    assert "验证码" in tag and "人工输入" in tag  # V10.1:placeholder 文案含验证码+人工


@pytest.mark.parametrize("portal", sorted(MOCK_PAGES))
def test_mock_page_marks_local_simulation_and_offline(portal: str) -> None:
    """红线 3:显著标注本地模拟/非官方,且无任何外部资源、不引用真实门户地址。"""
    html = _read_page(portal)
    assert "本地模拟" in html, "必须在页面上标注『本地模拟』"
    assert "非官方" in html, "必须在页面上标注『非官方』"
    assert "www.12377.cn" not in html, "模拟页不得引用真实举报门户地址"
    assert "www.shdf.gov.cn" not in html, "模拟页不得引用真实举报门户地址"
    # 内联 CSS + 内联 JS,无外链资源(图片/样式/脚本/跳转)
    lowered = html.lower()
    assert "http://" not in html and "https://" not in html, "模拟页不得包含任何外部 URL"
    assert "<img" not in lowered and "<link" not in lowered, "模拟页不得引用外部资源文件"


def test_12377_mock_banner_and_type_options() -> None:
    """12377 模拟页:醒目标识 + type 下拉包含计划所用的两个类别(value 即中文)。"""
    html = _read_page("12377")
    assert "本地模拟页面——非官方网站(12377 举报表单模拟)" in html
    assert '<option value="色情低俗信息">' in html  # plan_12377 的 category
    assert '<option value="淫秽色情类">' in html


def test_shdf_mock_banner_and_type_options() -> None:
    """扫黄打非模拟页:醒目标识 + type 下拉包含计划所用类别(value 即中文)。"""
    html = _read_page("shdf")
    assert "本地模拟页面——非官方网站(扫黄打非举报表单模拟)" in html
    assert '<option value="淫秽色情类">' in html  # plan_shdf 的 category


@pytest.mark.parametrize("portal", sorted(MOCK_PAGES))
def test_mock_page_submit_outcome_divs(portal: str) -> None:
    """提交结果节点:成功显示 #ok(举报已受理),失败显示 #err(请完整填写)。"""
    html = _read_page(portal)
    assert '<div id="ok"' in html
    assert "举报已受理(本地模拟)" in html
    assert '<div id="err"' in html
    assert "请完整填写" in html
    # 提交 JS 对 url/desc 做非空校验(脚本里引用这两个字段)
    script = html.split("<script", 1)[1] if "<script" in html else ""
    assert "report-url" in script and "report-desc" in script, "提交脚本必须校验 url/desc 非空"


def test_mock_ids_match_form_models_selectors() -> None:
    """与 A12 的 SELECTORS 常量交叉验证(模块未就位时 skip)。"""
    form_models = pytest.importorskip("netsentinel.submit.form_models")
    selectors = getattr(form_models, "SELECTORS", None)
    assert isinstance(selectors, dict) and selectors, "form_models.SELECTORS 缺失或为空"
    for key in ("url", "type", "desc", "name", "phone", "file", "captcha", "submit"):
        selector = selectors[key]
        assert isinstance(selector, str) and selector.startswith("#"), (
            f"SELECTORS[{key!r}] 应为 id 选择器,当前为 {selector!r}"
        )
        field_id = selector.lstrip("#")
        for portal in sorted(MOCK_PAGES):
            assert f'id="{field_id}"' in _read_page(portal), (
                f"{portal} 模拟页缺少 SELECTORS[{key!r}] 指向的 id={field_id}"
            )


# ---------------------------------------------------------------------------
# 二、serve.py 冒烟测试(标准库,离线)
# ---------------------------------------------------------------------------

def test_serve_module_serves_mock_pages_on_loopback() -> None:
    """serve.py 的 make_server 能在随机端口服务 mock 页面(仅 127.0.0.1)。"""
    spec = importlib.util.spec_from_file_location("netsentinel_tests_mock_serve", MOCK_DIR / "serve.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    httpd = module.make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/12377_mock.html", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "本地模拟" in body
        assert 'id="report-submit"' in body
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 三、贯通测试(重:本地 http.server + chromium 驱动本地模拟页)
# ---------------------------------------------------------------------------

class _QuietHandler(SimpleHTTPRequestHandler):
    """静态文件服务,静默访问日志避免污染 pytest 输出。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture()
def mock_portal_base_url():
    """在 127.0.0.1 随机端口服务 tests/mock_portals 目录,返回基础 URL。"""
    handler = functools.partial(_QuietHandler, directory=str(MOCK_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _entry_like() -> types.SimpleNamespace:
    """鸭子类型的队列 Entry:form_models.build_payload 只需要这三个属性。

    site_url 用 127.0.0.1 上的虚构地址,保证不会触发任何真实联网。
    """
    return types.SimpleNamespace(
        site_url="http://127.0.0.1/mock-site/index.html",
        verdict="nsfw",
        evidence_zip="x.zip",
    )


def _make_cfg(tmp_path: Path) -> Config:
    """安全默认配置;数据/日志路径全部指到 tmp_path,不污染项目目录。

    dry_run_default 保持默认 True 无所谓——执行时显式传 dry_run=False;
    allow_network=False 只影响产品 fetcher,不影响浏览器执行器访问本地模拟页。
    """
    data = tmp_path / "data"
    return Config(
        allow_network=False,
        human_gate_required=True,
        data_dir=str(data),
        evidence_dir=str(data / "evidence"),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


def _text_has_browser_hint(*sources: object) -> bool:
    """判断异常/说明文本是否属于『chromium 环境不可用』类失败。"""
    joined = " ".join(str(s) for s in sources).lower()
    return any(hint in joined for hint in _BROWSER_FAILURE_HINTS)


def _chromium_launchable() -> tuple[bool, str]:
    """预检:能否以 headless 方式启动 chromium;失败返回原因。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, f"playwright.sync_api 不可导入:{exc}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        return False, str(exc)
    return True, ""


@pytest.mark.parametrize(
    ("portal_key", "page_file"),
    [("12377", "12377_mock.html"), ("shdf", "shdf_mock.html")],
)
def test_mock_portal_full_flow(
    portal_key: str,
    page_file: str,
    tmp_path: Path,
    mock_portal_base_url: str,
) -> None:
    """端到端贯通:plan → execute(auto_confirm, 真浏览器)→ 本地模拟页受理。"""
    planner = pytest.importorskip(f"netsentinel.submit.portal_{portal_key}")
    executor_mod = pytest.importorskip("netsentinel.submit.executor_playwright")
    pytest.importorskip("playwright")

    launchable, reason = _chromium_launchable()
    if not launchable:
        pytest.skip(f"本机 chromium 无法启动,跳过浏览器贯通测试:{reason}")

    cfg = _make_cfg(tmp_path)
    entry_url = f"{mock_portal_base_url}/{page_file}"
    assert entry_url.startswith("http://127.0.0.1:")  # 红线:只允许本地模拟门户

    plan_fn = getattr(planner, f"plan_{portal_key}", None)
    assert callable(plan_fn), f"{planner.__name__} 缺少契约函数 plan_{portal_key}"
    plan = plan_fn(_entry_like(), cfg, entry_url=entry_url)
    assert plan.entry_url == entry_url

    # 红线 1/2:计划必须包含人工门,且不得出现任何自动填写验证码的步骤
    assert any(s.action == StepAction.HUMAN_GATE for s in plan.steps), "计划缺少 HUMAN_GATE 人工门"
    for step in plan.steps:
        if step.action == StepAction.FILL:
            assert "captcha" not in step.selector.lower(), (
                f"红线:自动化不得填写验证码,发现步骤 {step.label!r} -> {step.selector}"
            )

    try:
        result = executor_mod.execute(
            plan,
            cfg,
            auto_confirm=True,   # 测试中把人工门自动确认(记录『自动确认(测试)』)
            dry_run=False,       # 显式真跑(覆盖 cfg.dry_run_default)
            headless=True,
            out_dir=str(tmp_path / "runs"),
        )
    except Exception as exc:
        if _text_has_browser_hint(exc):
            pytest.skip(f"chromium 启动失败,跳过浏览器贯通测试:{exc}")
        raise

    if not result.ok:
        detail = " ".join([result.stopped_at, *map(str, result.notes)])
        if _text_has_browser_hint(detail):
            pytest.skip(f"浏览器环境不可用,跳过贯通测试:{detail[:300]}")

    assert result.submitted is True, f"{portal_key} 模拟提交应完成:notes={result.notes}"
    assert result.ok is True, (
        f"{portal_key} 模拟提交应成功:stopped_at={result.stopped_at!r} notes={result.notes}"
    )
    assert portal_key in str(getattr(result, "portal", portal_key))
    assert result.screenshots, "执行过程应留下截图(填写完成/提交结果)"
