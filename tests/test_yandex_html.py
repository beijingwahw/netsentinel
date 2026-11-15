"""netsentinel/discovery/yandex_html.py 内置 Yandex 网页引擎测试(V10.23,离线零外呼)。

- parse_serp_links 纯函数:自然结果提取/跳转包装解码/自域剔除/去重保序/limit 截断;
- 验证码页判定:命中特征且自然结果 ≤3 → 中文 RuntimeError(红线 1:绝不绕过);
- 引擎协议:discovery_online 门禁/HTTP 非 200 中文报错/传输注入(browser/http/auto);
- 注册表:get_engine("yandex-html") 构造、别名、list_engines 含之;
- discover() 流水线接线:注入引擎实例走完整链(缓存排除);
- 全程不联网(传输全部注入替身),不读真实 data/。
"""
from __future__ import annotations

import pytest

from netsentinel.contracts import Config
from netsentinel.discovery.engines import (
    DiscoveryConfigError,
    get_engine,
    list_engines,
)
from netsentinel.discovery.yandex_html import (
    YandexHtmlEngine,
    parse_serp_links,
)

# ---------------------------------------------------------------------------
# SERP 解析(纯函数)
# ---------------------------------------------------------------------------

#: 构造的最小 SERP 片段:自然结果 + 自域链接 + 跳转包装 + 噪声。
_SERP = """
<html><body>
<nav><a href="https://yandex.com/">首页</a> <a href="/clck/jsredir?from=serp&url=https%3A%2F%2Fadv.yandex.com%2Fx">广告</a></nav>
<main id="search-result">
  <h2><a href="https://target-a.example.com/page1">结果甲</a></h2>
  <h2><a href="https://target-b.example.org/">结果乙</a></h2>
  <h2><a href="https://yandex.com/clck/jsredir?from=serp&text=abc&url=http%3A%2F%2Fwrapped.example.net%2Fdeep%3Fq%3D1">包装结果</a></h2>
  <h2><a href="https://target-a.example.com/page1">重复结果(应去重)</a></h2>
  <h2><a href="javascript:void(0)">噪声</a> <a href="mailto:x@y.z">邮件</a> <a href="/relative">相对</a></h2>
  <h2><a href="https://sub.yandex.ru/serp">自域子域</a></h2>
  <h2><a href="https://target-c.example.net/">结果丙</a></h2>
</main>
</body></html>
"""


def test_parse_extracts_filters_decodes_and_orders():
    """自然结果:解码包装、剔自域/协议噪声、去重保序、limit 截断。"""
    hits = parse_serp_links(_SERP, 10)
    urls = [h.url for h in hits]
    assert urls == [
        "https://target-a.example.com/page1",
        "https://target-b.example.org/",
        "http://wrapped.example.net/deep?q=1",
        "https://target-c.example.net/",
    ]
    assert all(h.engine == "yandex-html" for h in hits)
    assert hits[0].domain == "target-a.example.com"

    capped = parse_serp_links(_SERP, 2)
    assert [h.url for h in capped] == urls[:2]


def test_parse_captcha_page_raises_chinese_never_bypass():
    """验证码页:特征命中且结果 ≤3 → 中文 RuntimeError,提及红线 1 与处置建议。"""
    captcha_html = (
        "<html><body><div class='SmartCaptcha'>请确认您不是机器人</div>"
        "<a href='https://yandex.com/'>home</a></body></html>"
    )
    with pytest.raises(RuntimeError, match="验证码.*红线 1.*绝不自动识别或绕过"):
        parse_serp_links(captcha_html, 10)


def test_parse_captcha_word_in_normal_page_not_false_positive():
    """普通结果页偶含 captcha 字样但自然结果充足 → 不误判。"""
    normal = _SERP.replace("</main>", "<!-- captcha mentioned in footer --></main>")
    hits = parse_serp_links(normal, 10)
    assert len(hits) == 4


def test_parse_empty_and_garbage_html():
    """空页/坏 HTML:返回空清单不抛(零结果由上层如实报告)。"""
    assert parse_serp_links("", 5) == []
    assert parse_serp_links("<<<not html", 5) == []


# ---------------------------------------------------------------------------
# 引擎协议(传输注入替身,零外呼)
# ---------------------------------------------------------------------------


def _online_cfg() -> Config:
    cfg = Config()
    cfg.discovery_online = True
    return cfg


def test_engine_gate_discovery_online_offline_refuses():
    """discovery_online=False:构造可用但调用即拒(与 XML 引擎同纪律)。"""
    eng = YandexHtmlEngine(Config())
    with pytest.raises(DiscoveryConfigError, match="discovery_online"):
        eng.search("每日大赛", 10)


def test_engine_http_transport_success_and_non200():
    """注入传输:200+SERP → 线索;404 → 中文 RuntimeError 含限流提示。"""
    eng = YandexHtmlEngine(_online_cfg(), transport=lambda url, t: (200, _SERP))
    hits = eng.search("任意", 3)
    assert [h.engine for h in hits] == ["yandex-html"] * 3

    eng_bad = YandexHtmlEngine(_online_cfg(), transport=lambda url, t: (429, "slow down"))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        eng_bad.search("任意", 3)


def test_engine_transport_selection_modes():
    """mode 显式指定;非法值构造即拒;auto 在无 playwright 环境退 HTTP。"""
    assert YandexHtmlEngine(_online_cfg(), mode="http")._mode == "http"
    assert YandexHtmlEngine(_online_cfg(), mode="browser")._mode == "browser"
    with pytest.raises(DiscoveryConfigError, match="auto/browser/http"):
        YandexHtmlEngine(_online_cfg(), mode="ftp")
    # auto 模式选择不抛即可(本机是否装 playwright 均为合法结果)
    YandexHtmlEngine(_online_cfg())._select_fetch()


def test_engine_base_override():
    """端点覆盖(cfg 附加属性 yandex_html_base)进入请求 URL。"""
    seen: list[str] = []

    def fake(url: str, t: float) -> tuple[int, str]:
        seen.append(url)
        return 200, _SERP

    cfg = _online_cfg()
    cfg.yandex_html_base = "https://proxy.example.com/search"
    YandexHtmlEngine(cfg, transport=fake).search("词", 5)
    assert seen and seen[0].startswith("https://proxy.example.com/search/?text=")


def test_engine_base_from_yaml_config(tmp_path):
    """yandex_html_base 已是一等配置键:YAML 写入 → cfg 字段 → 请求 URL 全链生效。"""
    import textwrap

    from netsentinel.config import load_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        textwrap.dedent(
            """\
            discovery_online: true
            yandex_html_base: "https://yandex.com/search/"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(cfg_file))
    assert cfg.yandex_html_base == "https://yandex.com/search/"

    seen: list[str] = []

    def fake(url: str, t: float) -> tuple[int, str]:
        seen.append(url)
        return 200, _SERP

    YandexHtmlEngine(cfg, transport=fake).search("词", 5)
    assert seen and seen[0].startswith("https://yandex.com/search/?text=")


def test_engine_base_empty_falls_back_to_builtin():
    """yandex_html_base 留空(缺省)= 内置默认 https://yandex.com/search/。"""
    seen: list[str] = []

    def fake(url: str, t: float) -> tuple[int, str]:
        seen.append(url)
        return 200, _SERP

    cfg = _online_cfg()
    assert cfg.yandex_html_base == ""
    YandexHtmlEngine(cfg, transport=fake).search("词", 5)
    assert seen and seen[0].startswith("https://yandex.com/search/?text=")


# ---------------------------------------------------------------------------
# 注册表与流水线接线
# ---------------------------------------------------------------------------


def test_registry_names_and_aliases():
    """get_engine 构造正确类型;别名归一;list_engines 含 yandex-html。"""
    assert isinstance(get_engine("yandex-html", _online_cfg()), YandexHtmlEngine)
    assert isinstance(get_engine("Yandex_HTML", _online_cfg()), YandexHtmlEngine)
    assert "yandex-html" in list_engines()
    with pytest.raises(DiscoveryConfigError, match="未知搜索引擎"):
        get_engine("no-such-engine", _online_cfg())


def test_discover_pipeline_wiring_with_injected_engine(tmp_path):
    """discover() 全链接线:注入 yandex-html 实例,线索经 canonical 去重产出。"""
    from netsentinel.discovery.pipeline import discover

    cfg = _online_cfg()
    cfg.discovery_cache_db = ""  # 禁用缓存,零落盘
    eng = YandexHtmlEngine(cfg, transport=lambda url, t: (200, _SERP))
    result = discover(["每日大赛"], cfg, engine=eng, sleep=lambda s: None)
    assert result["queries"] == 1
    assert result["errors"] == []
    # 流水线按 canonical 可注册域去重(同站只留首条):
    # wrapped.example.net/deep 与 target-c.example.net 同域归并为一条 → 3 条。
    assert result["urls"] == [
        "https://target-a.example.com/page1",
        "https://target-b.example.org/",
        "http://wrapped.example.net/deep?q=1",
    ]


# ---------------------------------------------------------------------------
# 人工验证码门(有头模式;离线用假页面对象)
# ---------------------------------------------------------------------------


class _FakePage:
    """假 playwright 页面:url/content 按 polling 次数演化。"""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self._script = list(script)
        self._i = 0

    @property
    def url(self) -> str:
        return self._script[min(self._i, len(self._script) - 1)][0]

    def content(self) -> str:
        self._i += 1  # 每次轮询推进一幕
        return self._script[min(self._i, len(self._script)) - 1][1]


_CAPTCHA_PAGE = ("https://yandex.com/showcaptcha?x", "<div>SmartCaptcha 请验证</div>")
_SERP_PAGE = ("https://yandex.com/search/?text=x", _SERP)


def test_wait_human_captcha_solved_by_human():
    """验证码页 → 运营者两轮后完成 → 返回 True。"""
    from netsentinel.discovery.yandex_html import wait_human_captcha

    page = _FakePage([_CAPTCHA_PAGE, _CAPTCHA_PAGE, _SERP_PAGE])
    assert wait_human_captcha(page, 30.0, poll_s=0.01, sleeper=lambda s: None) is True


def test_wait_human_captcha_timeout_returns_false():
    """一直不完成 → 到期返回 False(调用方如实报错,绝不代填)。"""
    from netsentinel.discovery.yandex_html import wait_human_captcha

    page = _FakePage([_CAPTCHA_PAGE])
    assert wait_human_captcha(page, 0.05, poll_s=0.01, sleeper=lambda s: None) is False


def test_wait_human_captcha_transient_error_tolerated():
    """轮询中页面跳转抛错按未解除处理,不击穿人工门。"""

    class _Flaky(_FakePage):
        def content(self) -> str:
            if self._i == 1:
                self._i += 1
                raise RuntimeError("Execution context was destroyed")
            return super().content()

    page = _Flaky([_CAPTCHA_PAGE, _CAPTCHA_PAGE, _SERP_PAGE])
    from netsentinel.discovery.yandex_html import wait_human_captcha

    assert wait_human_captcha(page, 30.0, poll_s=0.01, sleeper=lambda s: None) is True


# ---------------------------------------------------------------------------
# 内置 Yandex 浏览器(V10.25:检测/优先级/会话持久化)
# ---------------------------------------------------------------------------


def test_detect_yandex_browser_env_override(tmp_path, monkeypatch):
    """env 显式指定优先;文件不存在时 env 也返回 None(不盲信)。"""
    from netsentinel.discovery.yandex_html import detect_yandex_browser

    fake = tmp_path / "browser.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("NETSENTINEL_YANDEX_BROWSER_PATH", str(fake))
    assert detect_yandex_browser() == str(fake)

    monkeypatch.setenv("NETSENTINEL_YANDEX_BROWSER_PATH", str(tmp_path / "不存在.exe"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert detect_yandex_browser() is None


def test_browser_launch_candidates_priority(tmp_path, monkeypatch):
    """通道优先级:Yandex Browser 检测到时居首,其后 chrome/msedge/chromium。"""
    from netsentinel.discovery import yandex_html as yh

    fake = tmp_path / "browser.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("NETSENTINEL_YANDEX_BROWSER_PATH", str(fake))
    cands = yh.browser_launch_candidates()
    assert cands[0] == {"executable_path": str(fake)}
    assert cands[1:] == [{"channel": "chrome"}, {"channel": "msedge"}, {}]

    monkeypatch.setenv("NETSENTINEL_YANDEX_BROWSER_PATH", "")
    # 隔离真实安装(本机 winget 装过真 Yandex Browser):LOCALAPPDATA 指
    # 向空目录后标准路径探测即稳定为"未检测到"。
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "空"))
    cands2 = yh.browser_launch_candidates()
    assert cands2 == [{"channel": "chrome"}, {"channel": "msedge"}, {}]


def test_profile_dir_on_by_default_and_kill_switch(monkeypatch, tmp_path):
    """会话持久化缺省在 <data>/browser_profiles/yandex_html;env=0 关闭。"""
    from netsentinel.discovery.yandex_html import _profile_dir

    monkeypatch.delenv("NETSENTINEL_DATA_DIR", raising=False)
    monkeypatch.delenv("NETSENTINEL_YANDEX_HTML_PROFILE", raising=False)
    assert str(_profile_dir()).replace("\\", "/").endswith("data/browser_profiles/yandex_html")

    monkeypatch.setenv("NETSENTINEL_DATA_DIR", str(tmp_path))
    assert _profile_dir() == tmp_path / "browser_profiles" / "yandex_html"

    monkeypatch.setenv("NETSENTINEL_YANDEX_HTML_PROFILE", "0")
    assert _profile_dir() is None
