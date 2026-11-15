"""webui/assistant_page.py 测试(V10.26 席位B,离线,零外呼,REST 全 mock)。

覆盖(webui 双段结构铁律对应的三个层面):

- 纯逻辑层(本文件顶部成功 import 即证明:不依赖 streamlit 运行时):
  * rest_request:成功 JSON / HTTPError(JSON 错误体与非 JSON 错误体)/
    URLError 与裸 OSError 连接失败三态、POST body JSON 序列化与请求头、
    base/path 拼接、非 JSON 成功体、空响应体、timeout 透传;
  * status_card:完整 dict 的七行口径(GLM 密钥/Yandex 会话/关键词/队列
    四计数/三个 config 开关)、坏 dict(None/非 dict/缺键/键类型错)容错;
  * job_line:queued/running/done/failed 四态中文、失败错误摘要截断
    80 字、无错误详情兜底、坏 dict"任务状态未知";
  * scan_result_hint:未完成返回空串、完成行(机器判定中文口径 +
    综合分 + 入队去向三分支)、坏 dict 容错;
  * needs_captcha_card:yandex_profile 未建立/坏 dict → True、会话在但
    发现 job 报验证码(中文"验证码"与英文 captcha 大小写)→ True、
    无关错误 → False;
  * lead_rows:域名短显 + 完整 url 保留 + line 转字符串、空/脏容错
    (None/非 list/非 dict 条目/空 url 跳过)、域名解析失败回退原串;
  * poll_hint:非空中文文案(手动轮询 + 后台执行口径)。
- 红线:模块源码零新依赖(不 import httpx/requests,只用 urllib 标准库);
  纯逻辑层函数源码不引用 streamlit 符号(UI 与逻辑零耦合)。
- UI 守卫(webui_smoke / draft_flow 惯例对齐):streamlit 缺失时可导入、
  main() 打印中文安装提示退出码 1、render() 抛 RuntimeError、已装环境
  importorskip。**不做真渲染 AppTest**(REST mock 注入 render 太重),
  纯逻辑覆盖即可。
- 全程零外呼:rest_request 的 urlopen 全部 monkeypatch mock,绝不连接
  127.0.0.1:8765 或任何真实地址。
"""
from __future__ import annotations

import inspect
import io
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import webui.assistant_page as assistant_page
from webui.assistant_page import (
    REST_DOWN_HINT,
    job_line,
    lead_rows,
    needs_captcha_card,
    poll_hint,
    rest_request,
    scan_result_hint,
    status_card,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_SOURCE = (_PROJECT_ROOT / "webui" / "assistant_page.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# 造数小工具:假响应 / 假 urlopen
# ---------------------------------------------------------------------------

class _FakeResponse:
    """urlopen 返回的极简假响应(支持 with 上下文)。"""

    def __init__(self, raw: bytes, code: int = 200) -> None:
        self._raw = raw
        self.status = code

    def read(self) -> bytes:  # noqa: D102
        return self._raw

    def getcode(self) -> int:  # noqa: D102
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def urllib_request_mod():
    """取 rest_request 实际使用的 urllib.request 模块对象(同对象打桩)。"""
    import urllib.request

    return urllib.request


class _Recorder:
    """记录传给 urlopen 的 Request 与 kwargs,再按预设出结果。"""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.request = None
        self.kwargs: dict | None = None

    def __call__(self, request, **kwargs):
        self.request = request
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _install(monkeypatch: pytest.MonkeyPatch, outcome) -> _Recorder:
    recorder = _Recorder(outcome)
    monkeypatch.setattr(urllib_request_mod(), "urlopen", recorder)
    return recorder


def _http_error(body: bytes, code: int = 404) -> HTTPError:
    """构造带可读错误体的 HTTPError(fp 用 BytesIO)。"""
    return HTTPError("http://127.0.0.1:8765/x", code, "err", None, io.BytesIO(body))


# ---------------------------------------------------------------------------
# rest_request:urllib 封装三态 + 请求构造
# ---------------------------------------------------------------------------
class TestRestRequest:
    def test_success_json(self, monkeypatch):
        rec = _install(monkeypatch, _FakeResponse(b'{"job_id": "abc"}', code=202))
        code, payload = rest_request("POST", "/discover", body={})
        assert code == 202
        assert payload == {"job_id": "abc"}
        # 请求构造:方法 / URL 拼接 / JSON 体 / JSON 头 / 超时透传
        assert rec.request.method == "POST"
        assert rec.request.full_url == "http://127.0.0.1:8765/discover"
        assert json.loads(rec.request.data.decode("utf-8")) == {}
        assert rec.request.get_header("Content-type") == "application/json"
        assert rec.kwargs is not None and rec.kwargs.get("timeout") == 10

    def test_get_without_body(self, monkeypatch):
        rec = _install(monkeypatch, _FakeResponse(b'{"status": "done"}'))
        code, payload = rest_request("GET", "/jobs/abc")
        assert (code, payload) == (200, {"status": "done"})
        assert rec.request.method == "GET"
        assert rec.request.data is None  # 无 body 时不发请求体

    def test_base_path_join_tolerates_slashes(self, monkeypatch):
        rec = _install(monkeypatch, _FakeResponse(b"{}"))
        rest_request("GET", "assistant/status", base="http://127.0.0.1:9999/")
        assert rec.request.full_url == "http://127.0.0.1:9999/assistant/status"

    def test_http_error_json_detail(self, monkeypatch):
        _install(
            monkeypatch,
            _http_error('{"detail": "扫描任务不存在"}'.encode("utf-8"), code=404),
        )
        code, payload = rest_request("GET", "/jobs/nope")
        assert code == 404
        assert payload == {"detail": "扫描任务不存在"}

    def test_http_error_non_json_body(self, monkeypatch):
        _install(monkeypatch, _http_error(b"Bad Gateway", code=502))
        code, payload = rest_request("GET", "/jobs/x")
        assert code == 502
        assert payload == "Bad Gateway"

    @pytest.mark.parametrize(
        "outcome",
        [
            URLError(OSError(10061, "连接拒绝")),
            ConnectionRefusedError(10061, "连接拒绝"),
            OSError("timeout"),
        ],
        ids=["urlerror", "connection-refused", "oserror"],
    )
    def test_connection_failure_returns_zero_and_hint(self, monkeypatch, outcome):
        _install(monkeypatch, outcome)
        code, payload = rest_request("GET", "/assistant/status")
        assert code == 0
        assert payload == REST_DOWN_HINT == "REST 服务未启动:python -m service.app"

    def test_success_non_json_body(self, monkeypatch):
        _install(monkeypatch, _FakeResponse("纯文本".encode("utf-8"), code=200))
        code, payload = rest_request("GET", "/x")
        assert (code, payload) == (200, "纯文本")

    def test_empty_body(self, monkeypatch):
        _install(monkeypatch, _FakeResponse(b"", code=200))
        assert rest_request("GET", "/x") == (200, "")

    def test_timeout_forwarded(self, monkeypatch):
        rec = _install(monkeypatch, _FakeResponse(b"{}", code=200))
        rest_request("GET", "/x", timeout=3)
        assert rec.kwargs is not None and rec.kwargs.get("timeout") == 3


# ---------------------------------------------------------------------------
# status_card:七行口径 + 坏 dict 容错
# ---------------------------------------------------------------------------
class TestStatusCard:
    def test_full_dict_rows(self):
        rows = status_card(
            {
                "glm_key": True,
                "yandex_profile": True,
                "keywords_count": 3,
                "queue": {"pending": 2, "approved": 1, "rejected": 4, "submitted": 5},
                "config": {
                    "vlm_online": True,
                    "discovery_online": False,
                    "classifier": "glm-4.6v",
                },
            }
        )
        assert [r["label"] for r in rows] == [
            "GLM 密钥",
            "Yandex 会话",
            "发现关键词",
            "复核队列",
            "VLM 在线(vlm_online)",
            "发现在线(discovery_online)",
            "分类器(classifier)",
        ]
        assert [r["value"] for r in rows] == [
            "已配置",
            "已建立",
            "3 个",
            "待复核 2 · 已批准 1 · 已驳回 4 · 已提交 5",
            "开",
            "关",
            "glm-4.6v",
        ]
        assert [r["ok"] for r in rows] == [True, True, True, True, True, False, True]

    @pytest.mark.parametrize("bad", [None, {}, "坏数据", 123, [], {"glm_key": "yes"}])
    def test_bad_dict_tolerated(self, bad):
        rows = status_card(bad)
        assert len(rows) == 7  # 坏 dict 不抛错,仍是七行
        if isinstance(bad, dict) and bad.get("glm_key") == "yes":
            assert rows[0]["value"] == "已配置"  # 宽松判真:"yes" 认作真
            assert rows[1]["value"] == "未建立"
        else:
            assert rows[0]["value"] == "未配置"
            assert rows[1]["value"] == "未建立"
        assert rows[2]["value"] == "0 个"
        assert rows[3]["value"] == "待复核 0 · 已批准 0 · 已驳回 0 · 已提交 0"
        assert rows[4]["value"] == "关"
        assert rows[5]["value"] == "关"
        assert rows[6]["value"] == "未配置"

    def test_wrong_typed_keys(self):
        rows = status_card(
            {
                "glm_key": "false",  # 字符串假
                "yandex_profile": 0,
                "keywords_count": "5",  # 非数字按 0
                "queue": "not-a-dict",
                "config": {"vlm_online": "on", "discovery_online": True},
            }
        )
        assert rows[0]["value"] == "未配置"
        assert rows[1]["value"] == "未建立"
        assert rows[2]["value"] == "0 个"
        assert rows[3]["value"] == "待复核 0 · 已批准 0 · 已驳回 0 · 已提交 0"
        assert rows[4]["value"] == "开"  # "on" 宽松判真
        assert rows[5]["value"] == "开"
        assert rows[6]["value"] == "未配置"


# ---------------------------------------------------------------------------
# job_line:四态中文 + 失败摘要截断
# ---------------------------------------------------------------------------
class TestJobLine:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [("queued", "排队中"), ("running", "进行中"), ("done", "已完成")],
    )
    def test_normal_states(self, status, expected):
        assert job_line({"status": status}) == expected
        assert job_line({"status": status.upper()}) == expected  # 大小写不敏感

    def test_failed_with_error_truncated_to_80(self):
        line = job_line({"status": "failed", "error": "错" * 200})
        assert line.startswith("失败:" + "错" * 79)
        assert line.endswith("…")
        assert len(line) == len("失败:") + 80

    def test_failed_short_error_kept(self):
        assert job_line({"status": "failed", "error": "密钥未配置"}) == "失败:密钥未配置"

    def test_failed_without_error(self):
        assert job_line({"status": "failed"}) == "失败:未提供错误详情"
        assert job_line({"status": "failed", "error": "   "}) == "失败:未提供错误详情"

    @pytest.mark.parametrize("bad", [None, "坏数据", {}, {"status": ""}, {"status": "weird"}, 42])
    def test_bad_dict_unknown(self, bad):
        assert job_line(bad) == "任务状态未知"


# ---------------------------------------------------------------------------
# scan_result_hint:完成行口径
# ---------------------------------------------------------------------------
class TestScanResultHint:
    @pytest.mark.parametrize(
        "bad",
        [None, {}, {"status": "running"}, {"status": "failed"}, "坏数据"],
    )
    def test_not_done_returns_empty(self, bad):
        assert scan_result_hint(bad) == ""

    def test_done_full_result(self):
        hint = scan_result_hint(
            {"status": "done", "result": {"verdict": "nsfw", "agg": 0.87, "needs_review": True}}
        )
        assert "机器判定:高置信" in hint
        assert "综合分 0.87" in hint
        assert "已入复核队列" in hint and "复核台" in hint

    def test_done_clean_not_queued(self):
        hint = scan_result_hint(
            {"status": "done", "result": {"verdict": "clean", "needs_review": False}}
        )
        assert "机器判定:未见违规" in hint
        assert "未入复核队列" in hint

    def test_done_result_missing(self):
        assert scan_result_hint({"status": "done"}) == "判定入复核队列后请到复核台人工拍板"

    def test_done_bad_result_tolerated(self):
        assert scan_result_hint({"status": "done", "result": "坏"}) == (
            "判定入复核队列后请到复核台人工拍板"
        )

    def test_done_unknown_verdict(self):
        hint = scan_result_hint(
            {"status": "done", "result": {"verdict": "weird", "agg": True, "needs_review": True}}
        )
        assert "机器判定:未知" in hint
        assert "综合分" not in hint  # agg=True(bool)不按数字展示


# ---------------------------------------------------------------------------
# needs_captcha_card:验证会话卡显示条件
# ---------------------------------------------------------------------------
class TestNeedsCaptchaCard:
    @pytest.mark.parametrize("bad", [None, {}, "坏数据", {"yandex_profile": False}])
    def test_profile_missing_or_bad(self, bad):
        assert needs_captcha_card(bad) is True

    def test_profile_ok_no_error(self):
        assert needs_captcha_card({"yandex_profile": True}) is False
        assert needs_captcha_card({"yandex_profile": True}, "普通超时错误") is False

    @pytest.mark.parametrize("error", ["遇到验证码拦截", "CAPTCHA required", "captcha"])
    def test_captcha_error_triggers(self, error):
        assert needs_captcha_card({"yandex_profile": True}, error) is True


# ---------------------------------------------------------------------------
# lead_rows:表行口径 + 空/脏容错
# ---------------------------------------------------------------------------
class TestLeadRows:
    def test_valid_leads(self):
        rows = lead_rows(
            [
                {"url": "https://a.example.com/x?y=1", "line": 3},
                {"url": "http://b.other.io/", "line": "12"},
            ]
        )
        assert rows == [
            {"domain": "a.example.com", "url": "https://a.example.com/x?y=1", "line": "3"},
            {"domain": "b.other.io", "url": "http://b.other.io/", "line": "12"},
        ]

    def test_domain_fallback_when_unparseable(self):
        rows = lead_rows([{"url": "not-a-url", "line": None}])
        assert rows == [{"domain": "not-a-url", "url": "not-a-url", "line": ""}]

    @pytest.mark.parametrize("bad", [None, "坏数据", 42, {}])
    def test_bad_container(self, bad):
        assert lead_rows(bad) == []

    def test_dirty_items_skipped(self):
        rows = lead_rows(
            [
                "坏条目",
                {"line": 1},  # 无 url
                {"url": "   "},  # 空 url
                None,
                {"url": "https://ok.example.com"},  # 无 line → 空串
            ]
        )
        assert rows == [{"domain": "ok.example.com", "url": "https://ok.example.com", "line": ""}]


# ---------------------------------------------------------------------------
# poll_hint:轮询提示文案
# ---------------------------------------------------------------------------
def test_poll_hint_copywriting():
    text = poll_hint()
    assert isinstance(text, str) and text.strip()
    assert "127.0.0.1:8765" in text
    assert "后台" in text and "刷新进度" in text  # 后台执行 + 手动轮询口径


# ---------------------------------------------------------------------------
# 红线:零新依赖 + 纯逻辑层零 streamlit 符号
# ---------------------------------------------------------------------------
def test_module_zero_new_dependencies():
    """页面零新依赖:只用 urllib 标准库,绝不 import httpx/requests。"""
    assert "import urllib.request" in _MODULE_SOURCE
    assert "import httpx" not in _MODULE_SOURCE
    assert "import requests" not in _MODULE_SOURCE


def test_sibling_imports_are_lazy_in_ui_layer():
    """兄弟模块导入必须发生在函数体内(双段铁律:纯逻辑层零兄弟耦合)。

    顶层导入区只允许标准库、惰性 streamlit 与 ``webui._labels`` 纯常量;
    ``webui.keywords_page`` 等兄弟模块一律函数内导入(UI 层)。
    """
    import ast

    tree = ast.parse(_MODULE_SOURCE)
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert not (module.startswith("webui.") and module != "webui._labels"), (
                f"顶层导入兄弟模块 {module} 违反惰性纪律(应函数内导入)"
            )


# 匹配独立的 st. 引用(前面不是字母/数字/下划线/点,排除 urllib.request 之类)
_ST_SYMBOL_RE = re.compile(r"(?<![\w.])st\.")


@pytest.mark.parametrize(
    "fn",
    [rest_request, status_card, job_line, scan_result_hint, lead_rows, needs_captcha_card, poll_hint],
    ids=lambda fn: fn.__name__,
)
def test_pure_layer_never_touches_streamlit_symbols(fn):
    """纯逻辑层函数源码不得引用 st(UI 与逻辑零耦合,双段结构铁律)。

    用带边界的正则而非朴素子串:``urllib.request``/``Request`` 里的
    "st." 不是 streamlit 符号,不得误伤。
    """
    assert not _ST_SYMBOL_RE.search(inspect.getsource(fn)), (
        f"{fn.__name__} 源码引用了 streamlit 符号,违反双段结构铁律"
    )


# ---------------------------------------------------------------------------
# UI 守卫(webui_smoke / draft_flow 惯例对齐:惰性导入断言)
# ---------------------------------------------------------------------------
def test_module_importable_without_streamlit() -> None:
    # 顶部已成功 import:即证明纯逻辑层不依赖 streamlit(webui 双段结构铁律)。
    assert isinstance(assistant_page._HAS_ST, bool)
    for name in (
        "rest_request",
        "status_card",
        "job_line",
        "scan_result_hint",
        "needs_captcha_card",
        "lead_rows",
        "poll_hint",
        "render",
        "main",
    ):
        assert callable(getattr(assistant_page, name)), name


def test_main_without_streamlit_hint(capsys) -> None:
    if assistant_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    assert assistant_page.main() == 1
    err = capsys.readouterr().err
    assert "Streamlit" in err and "pip install" in err
    assert "assistant_page" in err  # 提示里给出本页面的启动命令


def test_render_without_streamlit_raises() -> None:
    if assistant_page._HAS_ST:  # pragma: no cover - 已装 streamlit 的环境跳过
        pytest.skip("本环境已安装 streamlit,缺失分支不可测")
    with pytest.raises(RuntimeError, match="streamlit"):
        assistant_page.render()


def test_streamlit_ui_guard() -> None:
    pytest.importorskip("streamlit")
    assert assistant_page._HAS_ST is True
    assert callable(assistant_page.render)
    assert callable(assistant_page.main)


def test_rest_base_loopback_only() -> None:
    """红线:页面只打本机回环 REST,默认基址不得指向外网地址。"""
    assert assistant_page.REST_BASE == "http://127.0.0.1:8765"
