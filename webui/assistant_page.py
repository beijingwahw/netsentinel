"""净网哨兵 · 净网助手页(V10.26,webui 第 9 个独立入口页面)。

定位:"所有操作后台进行,用户只看见净网助手页面"的一站式控制台——
**发现 → 扫描 → 复核**全链路经本机 REST 服务(默认
``http://127.0.0.1:8765``,即 ``python -m service.app``)后台执行,本页
只发起请求与轮询展示,**不弹终端、不弹窗口**(唯一例外:Yandex 验证会话
按钮,用户显式点击才弹一次浏览器窗口,人工完成一次验证)。

REST 契约(与 service.app 席位共享;本页零新依赖——只用 urllib 标准库):

- ``POST /discover`` ``{"keywords": [...]?}`` → 202 ``{"job_id"}``
  (keywords 省略时服务端读刚保存的 ``<data>/keywords/active.txt``);
- ``GET /jobs/{id}`` → ``{"status": "queued/running/done/failed",
  "error"?, "result"?}``(既有端点,三态轮询);
- ``GET /leads`` → ``{"leads": [{"url", "line"}], "count", "mtime"}``;
- ``POST /scan-lead`` ``{"url"}`` → 202 ``{"job_id"}``;
- ``POST /captcha/session`` → 202 ``{"job_id"}``(result 含 returncode);
- ``GET /assistant/status`` → ``{"glm_key", "yandex_profile",
  "keywords_count", "queue": {pending/approved/rejected/submitted},
  "config": {vlm_online, discovery_online, classifier}}``(本地只读零外呼)。

结构约定(与 tests/test_assistant_page.py 对应,双段结构铁律):
- 纯逻辑层(本文件上半部分,无 streamlit;测试离线覆盖、REST 全 mock):
  * :func:`rest_request`   urllib 封装 → ``(http 状态码, JSON dict 或文本)``;
    连接失败返回 ``(0, "REST 服务未启动:python -m service.app")``;
  * :func:`status_card`    /assistant/status dict → 展示行列表(密钥/会话/
    关键词/队列四计数/三个 config 开关;坏 dict 容错);
  * :func:`job_line`       job dict → 一行中文状态(排队/进行中/完成/失败
    +错误摘要截断 80 字);
  * :func:`scan_result_hint` 扫描 job 完成后的中文提示行(机器判定/综合分/
    是否入队;坏 dict 容错);
  * :func:`needs_captcha_card` 是否显示验证会话卡(yandex_profile 未建立
    或发现 job 报验证码失败);
  * :func:`lead_rows`      /leads 列表 → 表行(域名短显 + 完整 url 供 tooltip);
  * :func:`poll_hint`      轮询提示文案(手动轮询、后台执行的口径)。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):状态卡 / 关键词区(经 webui.keywords_page
  的 read_active/save_active 落盘 active.txt)/ 发现区(job 行 + 手动刷新
  进度,完成后自动拉线索)/ 线索区(逐条 [扫描] 后台任务)/ 验证会话卡
  (唯一弹窗操作)/ 复核区(队列四计数 + 跳转复核台 8501)/ 红线页脚。

安全红线(必须体现在代码里):
- **页面零外呼**:本页只请求 ``127.0.0.1:8765`` 本机 REST(含状态卡的
  /assistant/status 本地只读),绝不直接访问搜索引擎/门户/任意外网;
- **唯一弹窗**:验证会话按钮是全页唯一会弹窗的操作,且仅在你显式点击后
  才发生;其余一切(发现/扫描)都在服务端后台线程执行;
- **人工门不变**:扫描判定只做机器初筛,结果入复核队列后仍须到复核台
  (webui/app.py,默认 8501)人工拍板;举报提交永远走既有 CLI/人工门,
  本页没有任何提交入口;
- **轮询不自动**:页面不做 st_autorefresh 之类的自动刷新——进度全靠
  [刷新进度] 按钮手动轮询(零新依赖,也让"后台执行"始终可见可控)。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse

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
    "ERROR_SUMMARY_LIMIT",
    "REST_BASE",
    "REST_DOWN_HINT",
    "job_line",
    "lead_rows",
    "main",
    "needs_captcha_card",
    "poll_hint",
    "render",
    "rest_request",
    "scan_result_hint",
    "status_card",
]

# ---------------------------------------------------------------------------
# 共享标签口径(webui/_labels.py 单一权威定义;双路导入兼容脚本直跑)
# ---------------------------------------------------------------------------
try:
    from webui._labels import VERDICT_CN
except ImportError:  # streamlit run webui/assistant_page.py 时 sys.path[0]=webui/
    from _labels import VERDICT_CN  # type: ignore[no-redef]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;不 import 兄弟模块——REST 之外的纯函数)
# ===========================================================================

#: 本机 REST 服务基址(service.app 默认绑定,仅回环,无鉴权)。
REST_BASE: str = "http://127.0.0.1:8765"

#: REST 连接失败时返回的中文提示(状态码 0 专用)。
REST_DOWN_HINT: str = "REST 服务未启动:python -m service.app"

#: job 失败时错误摘要的截断上限(字符)。
ERROR_SUMMARY_LIMIT: int = 80

#: job 状态 → 中文(与 GET /jobs 的四态口径一致)。
_JOB_STATUS_CN: dict[str, str] = {
    "queued": "排队中",
    "running": "进行中",
    "done": "已完成",
    "failed": "失败",
}

#: 触发"验证会话卡"的错误关键词(发现 job 的 error 里命中即提示人工验证)。
_CAPTCHA_MARKERS: tuple[str, ...] = ("验证码", "captcha")


def _parse_body(raw: Any) -> Any:
    """响应体 bytes → JSON dict/list;空体返回空串;非 JSON 返回文本。

    解码失败不抛错(errors="replace"),坏 JSON 原样按文本返回——REST
    契约之外的不规范响应只影响展示,不应让页面崩溃。
    """
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", errors="replace").strip()
    else:
        text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def rest_request(
    method: str,
    path: str,
    body: Any = None,
    base: str = REST_BASE,
    timeout: float = 10,
) -> tuple[int, Any]:
    """对本地 REST 服务发一次请求 → ``(http 状态码, 解析后的响应体)``。

    - ``method``/``path`` 拼成 ``{base}/{path}``(容忍 base 尾斜杠);
    - ``body`` 非 None 时 JSON 序列化随请求发送(POST 场景),并声明
      ``Content-Type: application/json``;
    - 成功与 HTTPError 都返回真实状态码(错误体里的 ``{"detail": ...}``
      同样解析成 dict);**连接失败(拒绝/超时/URLError)返回
      ``(0, REST_DOWN_HINT)``**,由 UI 层直接展示启动指引;
    - 只用 urllib 标准库——本页零新依赖(红线:不用 httpx/requests)。

    示例::

        >>> rest_request("GET", "/assistant/status", base="http://127.0.0.1:1")
        (0, 'REST 服务未启动:python -m service.app')
    """
    url = f"{str(base).rstrip('/')}/{str(path).lstrip('/')}"
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=str(method or "GET").upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            code = int(getattr(resp, "status", None) or resp.getcode() or 0)
    except HTTPError as exc:  # 4xx/5xx:带状态码与错误体返回,不算"服务未启动"
        code = int(exc.code or 0)
        try:
            raw = exc.read()
        except OSError:  # pragma: no cover - 错误体读失败的极端场景
            raw = b""
    except OSError:  # URLError 本就是 OSError 子类(连接拒绝/超时/DNS)
        return 0, REST_DOWN_HINT
    return code, _parse_body(raw)


def _truthy(value: Any) -> bool:
    """REST 布尔字段的宽松判真:字符串只认 1/true/yes/on,其余按 bool。"""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _count(value: Any) -> int:
    """REST 计数字段的容错读取:非数字(bool 排除)一律按 0。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _truncate(text: str, limit: int = ERROR_SUMMARY_LIMIT) -> str:
    """错误摘要截断:超长时保留前 ``limit-1`` 字并追加省略号。"""
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def status_card(status: Any) -> list[dict]:
    """/assistant/status dict → 状态卡展示行(每行 ``{label, value, ok}``)。

    七行口径:GLM 密钥(已配置/未配置)、Yandex 会话(已建立/未建立)、
    发现关键词(N 个)、复核队列(四计数一行)、VLM 在线(vlm_online)、
    发现在线(discovery_online)、分类器(classifier 名)。坏 dict(非 dict
    /缺键/键类型错)一律容错为"未配置/未建立/0 个/关",绝不抛错。

    示例::

        >>> rows = status_card({"glm_key": True, "yandex_profile": False,
        ...                     "keywords_count": 3, "queue": {"pending": 2},
        ...                     "config": {"vlm_online": True,
        ...                                "discovery_online": False,
        ...                                "classifier": "glm-4.6v"}})
        >>> [r["value"] for r in rows[:4]]
        ['已配置', '未建立', '3 个', '待复核 2 · 已批准 0 · 已驳回 0 · 已提交 0']
        >>> status_card(None)[0]["value"]
        '未配置'
    """
    data = status if isinstance(status, dict) else {}
    queue_raw = data.get("queue")
    queue = queue_raw if isinstance(queue_raw, dict) else {}
    config_raw = data.get("config")
    config = config_raw if isinstance(config_raw, dict) else {}

    glm_ok = _truthy(data.get("glm_key"))
    yandex_ok = _truthy(data.get("yandex_profile"))
    keywords = _count(data.get("keywords_count"))
    pending = _count(queue.get("pending"))
    approved = _count(queue.get("approved"))
    rejected = _count(queue.get("rejected"))
    submitted = _count(queue.get("submitted"))
    vlm_online = _truthy(config.get("vlm_online"))
    discovery_online = _truthy(config.get("discovery_online"))
    classifier = str(config.get("classifier") or "").strip()

    return [
        {
            "label": "GLM 密钥",
            "value": "已配置" if glm_ok else "未配置",
            "ok": glm_ok,
        },
        {
            "label": "Yandex 会话",
            "value": "已建立" if yandex_ok else "未建立",
            "ok": yandex_ok,
        },
        {
            "label": "发现关键词",
            "value": f"{keywords} 个",
            "ok": keywords > 0,
        },
        {
            "label": "复核队列",
            "value": (
                f"待复核 {pending} · 已批准 {approved} · "
                f"已驳回 {rejected} · 已提交 {submitted}"
            ),
            "ok": True,
        },
        {
            "label": "VLM 在线(vlm_online)",
            "value": "开" if vlm_online else "关",
            "ok": vlm_online,
        },
        {
            "label": "发现在线(discovery_online)",
            "value": "开" if discovery_online else "关",
            "ok": discovery_online,
        },
        {
            "label": "分类器(classifier)",
            "value": classifier or "未配置",
            "ok": bool(classifier),
        },
    ]


def job_line(job: Any) -> str:
    """job dict → 一行中文状态(排队/进行中/完成/失败+错误摘要截断 80 字)。

    坏 dict(非 dict/缺 status/未知状态)返回"任务状态未知",绝不抛错。

    示例::

        >>> job_line({"status": "queued"})
        '排队中'
        >>> job_line({"status": "failed", "error": "x" * 200})[:6]
        '失败:xxx'
        >>> job_line("坏数据")
        '任务状态未知'
    """
    data = job if isinstance(job, dict) else {}
    status = str(data.get("status") or "").strip().lower()
    label = _JOB_STATUS_CN.get(status)
    if label is None:
        return "任务状态未知"
    if status == "failed":
        error = str(data.get("error") or "").strip()
        return f"失败:{_truncate(error or '未提供错误详情')}"
    return label


def scan_result_hint(job: Any) -> str:
    """扫描 job 完成后的中文提示行;未完成/坏 dict 返回空串。

    完成行口径:机器判定(判定档位中文,口径取 webui/_labels)+ 综合分
    (result.agg 为数字时)+ 入队去向(needs_review=True → 已入复核队列,
    须到复核台人工拍板;False → 未见违规未入队;缺失 → 通用提示)。
    """
    data = job if isinstance(job, dict) else {}
    if str(data.get("status") or "").strip().lower() != "done":
        return ""
    result_raw = data.get("result")
    result = result_raw if isinstance(result_raw, dict) else {}
    parts: list[str] = []
    verdict = str(result.get("verdict") or "").strip().lower()
    if verdict:
        parts.append(f"机器判定:{VERDICT_CN.get(verdict, '未知')}")
    agg = result.get("agg")
    if isinstance(agg, (int, float)) and not isinstance(agg, bool):
        parts.append(f"综合分 {float(agg):.2f}")
    needs_review = result.get("needs_review")
    if needs_review is True:
        tail = "已入复核队列——请到复核台人工拍板"
    elif needs_review is False:
        tail = "机器初筛未见违规,未入复核队列"
    else:
        tail = "判定入复核队列后请到复核台人工拍板"
    return " · ".join(parts) + " —— " + tail if parts else tail


def _domain_short(url: str) -> str:
    """url → 域名短显;解析不出 netloc 时回退原串。"""
    text = str(url or "").strip()
    try:
        netloc = urlparse(text).netloc
    except ValueError:  # pragma: no cover - urlparse 对怪串几乎不抛
        netloc = ""
    return netloc or (text or "(空)")


def lead_rows(leads: Any) -> list[dict]:
    """/leads 列表 → 表行,每行 ``{domain, url, line}``。

    ``domain`` 为域名短显(解析失败回退原串),``url`` 完整保留供 tooltip,
    ``line`` 原样转字符串。空/脏容错:非 list 返回空表;非 dict 条目、
    空 url 条目一律跳过,绝不抛错。

    示例::

        >>> lead_rows([{"url": "https://a.example.com/x", "line": 3}])
        [{'domain': 'a.example.com', 'url': 'https://a.example.com/x', 'line': '3'}]
        >>> lead_rows(None), lead_rows(["坏条目"])
        ([], [])
    """
    rows: list[dict] = []
    if not isinstance(leads, list):
        return rows
    for item in leads:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        line_raw = item.get("line")
        rows.append(
            {
                "domain": _domain_short(url),
                "url": url,
                "line": "" if line_raw is None else str(line_raw).strip(),
            }
        )
    return rows


def needs_captcha_card(status: Any, discover_error: str = "") -> bool:
    """是否显示"验证会话卡":Yandex 会话未建立,或发现 job 报验证码失败。

    - ``status`` 的 ``yandex_profile`` 判假(含坏 dict)→ True(引导人工
      完成一次验证);
    - 会话已建立时,再看 ``discover_error``(发现 job 的 error 文本)是否
      命中验证码关键词(验证码/captcha,大小写不敏感)。
    """
    data = status if isinstance(status, dict) else {}
    if not _truthy(data.get("yandex_profile")):
        return True
    error = str(discover_error or "").strip().lower()
    return any(marker.lower() in error for marker in _CAPTCHA_MARKERS)


def poll_hint() -> str:
    """轮询提示文案:手动轮询 + 后台执行的口径(页面不自动刷新)。"""
    return (
        "所有操作经本机 REST(127.0.0.1:8765)在服务端后台执行,页面只展示;"
        "进度不自动刷新——点击对应 [刷新进度] 按钮手动轮询一次,"
        "发现完成后线索表会自动拉取。"
    )


# ===========================================================================
# UI 层(streamlit 惰性;缺依赖时 main() 拦截;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "净网助手"
_MOTTO = "发现 → 扫描 → 复核 一站式;所有操作后台执行;人工门不变。"

#: 任务提交成功的 HTTP 状态码集合(202 为主,容忍 200/201 的实现差异)。
_ACCEPTED_CODES: tuple[int, ...] = (200, 201, 202)


def _flash(msg: str, kind: str = "success") -> None:
    """跨 rerun 的临时消息队列(st.rerun 会清掉本轮已渲染的提示)。"""
    st.session_state.setdefault("as_flash", []).append(
        {"msg": str(msg), "kind": str(kind)}
    )


def _drain_flash() -> None:
    """渲染并清空 flash 队列(在页面标题之后第一时间执行)。"""
    for item in st.session_state.pop("as_flash", None) or []:
        msg = str(item.get("msg") or "")
        kind = str(item.get("kind") or "success")
        if kind == "error":
            st.error(msg)
        elif kind == "warning":
            st.warning(msg)
        else:
            st.success(msg)


def _render_rest_error(code: int, payload: Any) -> None:
    """REST 请求失败统一提示:状态码 0 走"服务未启动"指引,其余带 detail。"""
    if code == 0:
        st.error(f"{payload} —— 本页全部后台操作(发现/扫描/验证会话)均依赖该服务。")
        return
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("detail") or payload)
    st.error(f"REST 请求失败(HTTP {code}):{detail or payload}")


def _refresh_status() -> None:
    """拉取 /assistant/status 存入 session(本地只读零外呼)。"""
    code, payload = rest_request("GET", "/assistant/status")
    st.session_state["as_status"] = {"code": code, "payload": payload}


def _render_status_card() -> dict:
    """状态卡:密钥/会话/关键词/队列/config 开关(打开页面即拉一次)。

    :returns: /assistant/status 的 dict(拉取失败时返回空 dict,后续
        复核区按零计数展示)。
    """
    st.subheader("📊 服务状态")
    if "as_status" not in st.session_state:
        _refresh_status()
    cached = st.session_state["as_status"]
    code, payload = cached["code"], cached["payload"]
    if st.button("🔄 刷新状态", key="as_status_refresh"):
        _refresh_status()
        st.rerun()
    st.caption("GET /assistant/status(本地只读,零外呼)")
    if code == 0:
        st.error(
            f"{payload} —— 状态卡不可用;启动 REST 服务后回到本页,"
            "或点击上方 [刷新状态] 重试。"
        )
        return {}
    if code != 200 or not isinstance(payload, dict):
        st.warning(f"状态拉取异常(HTTP {code}):{payload}")
        return {}
    rows = status_card(payload)
    st.table(
        [
            {
                "项目": row["label"],
                "状态": ("✅ " if row["ok"] else "⚠️ ") + str(row["value"]),
            }
            for row in rows
        ]
    )
    return payload


def _render_keywords(data_dir: str) -> None:
    """关键词区:编辑 active.txt(复用关键词工作台的纯函数落盘)。"""
    from webui.keywords_page import parse_stats, read_active, save_active  # 惰性

    st.subheader("🗝️ 发现关键词(active.txt)")
    st.caption(
        "保存经关键词工作台同款校验原子落盘(零内置关键词);发现任务在"
        "服务端读取该活文件,保存后下一次发现即生效。"
    )
    editor_key = "as_kw_editor"
    if editor_key not in st.session_state:
        st.session_state[editor_key] = read_active(data_dir)
    text = st.text_area(
        "关键词(每行一个,`#` 开头为注释)",
        key=editor_key,
        height=180,
    )
    stats = parse_stats(text)
    st.caption(
        f"实时统计:有效关键词 {stats['words']} 个 · "
        f"跳过空行/注释/重复 {stats['dropped']} 行"
    )
    if st.button("💾 保存关键词", key="as_kw_save", type="primary"):
        try:
            saved = save_active(data_dir, text)
        except ValueError as exc:
            st.error(str(exc))
        else:
            _flash(f"已保存 {saved['words']} 个关键词 → {saved['path']}(状态卡已重拉)。")
            _refresh_status()  # 成功后重拉状态卡(keywords_count 即时反映)
            st.rerun()


def _pull_leads() -> None:
    """拉取 /leads 并转表行存入 session(只读)。"""
    code, payload = rest_request("GET", "/leads")
    if code == 200 and isinstance(payload, dict):
        st.session_state["as_leads"] = {
            "rows": lead_rows(payload.get("leads")),
            "count": payload.get("count"),
            "mtime": str(payload.get("mtime") or ""),
        }
    else:
        st.session_state["as_leads"] = {"rows": [], "count": 0, "mtime": ""}
        _flash(f"线索拉取失败(HTTP {code})", "error")


def _submit_job(path: str, body: Any, session_key: str, label: str) -> bool:
    """提交后台任务(POST → 202 {job_id})并写入 session;返回是否成功。"""
    code, payload = rest_request("POST", path, body=body)
    if (
        code in _ACCEPTED_CODES
        and isinstance(payload, dict)
        and str(payload.get("job_id") or "").strip()
    ):
        st.session_state[session_key] = {
            "job_id": str(payload["job_id"]),
            "job": {"status": "queued"},
        }
        _flash(f"{label}已提交(后台执行,点击 [刷新进度] 轮询)。")
        st.rerun()
        return True
    _render_rest_error(code, payload)
    return False


def _poll_job(session_key: str) -> None:
    """手动轮询一次 session 里登记的后台任务(GET /jobs/{id})。"""
    state = st.session_state.get(session_key) or {}
    job_id = str(state.get("job_id") or "")
    if not job_id:
        return
    code, payload = rest_request("GET", f"/jobs/{job_id}")
    if code == 200 and isinstance(payload, dict):
        state["job"] = payload
        st.rerun()
    else:
        _render_rest_error(code, payload)


def _render_discover() -> None:
    """发现区:[开始发现] 提交后台任务 + 手动刷新进度(不自动轮询)。"""
    st.subheader("🛰️ 发现(后台)")
    st.caption(
        "POST /discover —— 服务端后台线程执行搜索发现(默认读取刚保存的 "
        "active.txt;受 discovery_online 与密钥门槛约束),页面只展示任务状态。"
    )
    if st.button("🛰️ 开始发现", key="as_discover_start", type="primary"):
        _submit_job("/discover", {}, "as_discover", "发现任务")
    state = st.session_state.get("as_discover")
    if not state:
        st.info("尚未提交发现任务。")
        return
    st.info(f"发现任务:{job_line(state.get('job'))}")
    if st.button("🔄 刷新进度", key="as_discover_refresh"):
        _poll_job("as_discover")
    job = state.get("job") if isinstance(state.get("job"), dict) else {}
    if str(job.get("status") or "").lower() == "done":
        st.success("发现完成——线索表已自动拉取(见下方「线索」区)。")
        if "as_leads" not in st.session_state:
            _pull_leads()
            st.rerun()
    elif str(job.get("status") or "").lower() == "failed":
        st.error(f"发现失败:{_truncate(str(job.get('error') or ''))}")


def _render_leads() -> None:
    """线索区:域名短显 + 完整 url tooltip + 逐条 [扫描] 后台任务。"""
    st.subheader("🧾 线索(最近一次发现产出)")
    if st.button("🔄 拉取线索表", key="as_leads_refresh"):
        _pull_leads()
        st.rerun()
    cache = st.session_state.get("as_leads")
    if not cache:
        st.info(
            "尚无线索:完成一次发现后自动拉取,或点击上方按钮手动拉取"
            "(GET /leads,只读)。"
        )
        return
    rows = cache.get("rows") or []
    count = cache.get("count")
    count_text = str(count) if isinstance(count, int) else str(len(rows))
    st.caption(f"共 {count_text} 条 · 线索文件时间:{cache.get('mtime') or '-'}")
    if not rows:
        st.info("线索文件为空(发现可能未产出结果,或被验证码拦下)。")
        return

    scan_jobs: dict[str, dict] = st.session_state.get("as_scan_jobs") or {}
    if scan_jobs and st.button("🔄 刷新全部扫描进度", key="as_scan_refresh"):
        for state in scan_jobs.values():
            code, payload = rest_request("GET", f"/jobs/{state['job_id']}")
            if code == 200 and isinstance(payload, dict):
                state["job"] = payload
        st.rerun()

    for index, row in enumerate(rows):
        col_url, col_line, col_scan = st.columns([3.2, 0.8, 1.6])
        col_url.text(row["domain"], help=row["url"])  # 完整 url 作 tooltip
        col_line.caption(str(row["line"] or "-"))
        if col_scan.button("🔍 扫描", key=f"as_scan_{index}", help=row["url"]):
            # 与 _submit_job 同款提交,但任务按 url 分桶登记(同 url 重扫覆盖)
            code, payload = rest_request("POST", "/scan-lead", body={"url": row["url"]})
            if (
                code in _ACCEPTED_CODES
                and isinstance(payload, dict)
                and str(payload.get("job_id") or "").strip()
            ):
                jobs = st.session_state.setdefault("as_scan_jobs", {})
                jobs[row["url"]] = {
                    "job_id": str(payload["job_id"]),
                    "job": {"status": "queued"},
                }
                _flash("扫描任务已提交(后台执行,点击 [刷新全部扫描进度] 轮询)。")
                st.rerun()
            else:
                _render_rest_error(code, payload)
        state = (st.session_state.get("as_scan_jobs") or {}).get(row["url"])
        if state:
            col_scan.caption(job_line(state.get("job")))
            hint = scan_result_hint(state.get("job"))
            if hint:
                col_scan.caption(hint)


def _render_captcha_card(status_dict: dict) -> None:
    """验证会话卡:唯一弹窗操作(用户显式点击才发生)。

    显示条件::func:`needs_captcha_card`(yandex_profile 未建立,或发现
    job 报验证码失败)。
    """
    discover_state = st.session_state.get("as_discover") or {}
    discover_job = (
        discover_state.get("job") if isinstance(discover_state.get("job"), dict) else {}
    )
    discover_error = str(discover_job.get("error") or "")
    if not status_dict and not discover_error.strip():
        # REST 未启动时拿不到 yandex_profile,不猜"未建立"——避免误导性弹窗指引。
        return
    if not needs_captcha_card(status_dict, discover_error):
        return
    st.subheader("🔐 Yandex 验证会话")
    st.warning(
        "Yandex 搜索会话未建立(或发现任务报验证码失败):搜索引擎要求人工"
        "完成一次验证。本按钮是本页**唯一会弹窗**的操作,且仅在你显式点击后"
        "才发生;人工完成一次后即可长期使用。"
    )
    if st.button("🔓 打开验证窗口(人工完成一次)", key="as_captcha_open"):
        _submit_job("/captcha/session", None, "as_captcha", "验证会话任务")
    state = st.session_state.get("as_captcha")
    if state:
        st.info(f"验证会话任务:{job_line(state.get('job'))}")
        if st.button("🔄 刷新验证会话进度", key="as_captcha_refresh"):
            _poll_job("as_captcha")
        job = state.get("job") if isinstance(state.get("job"), dict) else {}
        if str(job.get("status") or "").lower() == "done":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            returncode = result.get("returncode")
            suffix = (
                f"(窗口进程退出码 {returncode})"
                if returncode is not None
                else "(未报告退出码)"
            )
            st.success(f"验证会话任务已完成{suffix}——回到 [发现] 重新发起即可。")


def _render_review(status_dict: dict) -> None:
    """复核区:队列四计数 + 跳转复核台(8501)提示(人工门不变)。"""
    st.subheader("🛡️ 人工复核(人工门不变)")
    queue_raw = status_dict.get("queue") if isinstance(status_dict, dict) else None
    queue = queue_raw if isinstance(queue_raw, dict) else {}
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("待复核", _count(queue.get("pending")))
    col2.metric("已批准", _count(queue.get("approved")))
    col3.metric("已驳回", _count(queue.get("rejected")))
    col4.metric("已提交", _count(queue.get("submitted")))
    st.info(
        "机器只初筛:扫描判定入复核队列后,请到人工复核台逐条拍板——"
        "`streamlit run webui/app.py`(默认 http://localhost:8501)。"
        "举报提交永远走既有 CLI/人工门,本页没有任何提交入口。"
    )


def _render_footer() -> None:
    """红线页脚。"""
    st.divider()
    st.markdown(
        "**红线**:机器只初筛 · 人工门必经 · 举报须真实(虚假举报违法)。"
        "本页所有操作仅经本机 REST 后台执行,页面零外呼。"
    )
    st.caption(poll_hint())


def render() -> None:
    """渲染净网助手(状态卡/关键词/发现/线索/验证会话/复核/红线页脚)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,双保险
        raise RuntimeError(
            "streamlit 不可用,无法渲染净网助手页;请先安装 python -m pip install -e \".[ui]\""
        )

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from webui.app import apply_data_dir

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🧭", layout="wide")
    st.title(f"🧭 {_PAGE_TITLE}")
    st.caption(_MOTTO)
    _drain_flash()

    cfg = apply_data_dir(load_config())
    data_dir = str(getattr(cfg, "data_dir", "data") or "data")

    status_dict = _render_status_card()
    _render_keywords(data_dir)
    _render_discover()
    _render_leads()
    _render_captcha_card(status_dict)
    _render_review(status_dict)
    _render_footer()


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,净网助手页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/assistant_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    _exit_code = main()
    # V10.20(SystemExit 收尾修复,同 webui/keywords_page.py):streamlit
    # 脚本线程内不抛 SystemExit(BaseException 会杀死脚本线程,AppTest 通道
    # 失效);仅真直跑才转进程退出码(保留缺依赖提示退出码 1 契约)。
    _in_script_thread = False
    if _HAS_ST:
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx

            _in_script_thread = get_script_run_ctx() is not None
        except Exception:  # noqa: BLE001 - 内部 API 缺席按直跑处理
            _in_script_thread = False
    if not _in_script_thread:
        raise SystemExit(_exit_code)
