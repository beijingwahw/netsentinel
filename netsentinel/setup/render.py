# -*- coding: utf-8 -*-
"""A156 连接向导渲染注入(``netsentinel.setup.render``,依据 CONTRACTS-V8.md §3)。

在 A146 ``page_html()`` 模板(惰性导入,兄弟模块只读、允许缺席)基础上做
**三处确定性注入**,产出可直接由 A145 ``GET /`` 返回的完整 HTML:

① 切换横幅:``active`` 为非空字符串 → 横幅默认文案"尚未连接视觉模型"替换为
   ``当前视觉模型:{active}``(``html.escape`` 转义);``None`` / 空串 → 保持默认;
② 状态 JSON:``<script id="ns-state" type="application/json">…</script>`` 插入
   ``</head>`` 之前,内容为 ``json.dumps({"status","active","local"},
   ensure_ascii=False)``;**``</script>`` 注入防护**——序列化后把 ``"</"`` 一律
   替换为 ``"<\\/"``(JSON 合法转义,回读还原),任何值/键都无法闭合脚本块;
   键名额外经 ``html.escape``(值经 JSON 序列化+上述替换本就安全);
③ 本机模型初始行:``local_models`` 非空 → 在 local-list 容器(行模板之后)注入
   静态 ``data-testid="local-row"`` 行,provider/model/base_url 逐字段转义;
   服务结构对齐 A143(``{"provider","base_url","models","ok"}``),``ok=False``
   跳过,``models`` 项支持字符串或 ``{"id"/"name"}`` 对象。

坏输入容错(渲染永不因数据崩溃):``status`` 非 dict → ``{}``;``active`` 非字符串
→ 视为 ``None``;``local_models`` 非 list → 无行注入;嵌套深度超过
:data:`MAX_DEPTH`(10)的内容以中文截断标记替代;不可 JSON 序列化的对象字符串化。
模板层三处注入均先转义再拼接,且状态 JSON **最后**注入,注入内容无法干扰
前两步的锚点定位(转义后不含任何真实标签)。

模块缺席时抛中文 :class:`RuntimeError`(与 batchflow / crawler 兄弟惰性导入
口径一致),供 A145 服务器逐级回退 A146 原样页面。
"""
from __future__ import annotations

import html as _html
import json as _json
import re as _re

__all__ = ["MAX_DEPTH", "STATE_SCRIPT_ID", "render_page"]

#: 状态 JSON 节点 id(A145/A157 可据此取初值)。
STATE_SCRIPT_ID = "ns-state"

#: 容错截断的嵌套深度上限(契约:深度嵌套 >10 截断)。
MAX_DEPTH = 10

#: 深度超限时的确定性替换文案。
_TRUNCATED = "(嵌套过深,已截断)"

#: 横幅默认文案(与 A146 模板逐字一致)。
_BANNER_DEFAULT = "尚未连接视觉模型"

#: 横幅活动态前缀。
_BANNER_PREFIX = "当前视觉模型:"

#: 横幅元素(仅替换该元素内文本,不动内联 JS 中的同文案分支)。
_BANNER_RE = _re.compile(
    r'(<div\b[^>]*\bdata-testid="active-banner"[^>]*>)'
    + _re.escape(_BANNER_DEFAULT)
    + r"(</div>)"
)

#: 本机列表容器与行模板锚点。
_LOCAL_LIST_ANCHOR = 'data-testid="local-list"'
_TEMPLATE_CLOSE = "</template>"

#: 状态 JSON 节点(内容占位由 :func:`_state_script_html` 生成)。
_STATE_RE = _re.compile(
    r'<script id="' + STATE_SCRIPT_ID + r'" type="application/json">'
    r"([\s\S]*?)</script>"
)


def _esc(text: object) -> str:
    """HTML 文本转义(横幅/行字段唯一出口,quote 一并转义)。"""
    return _html.escape(str(text), quote=True)


def _load_page() -> str:
    """惰性取 A146 ``page_html()``;缺席/异常抛中文 ``RuntimeError``。"""
    try:
        from netsentinel.setup.page import page_html  # noqa: PLC0415 惰性导入(兄弟只读)
    except Exception as exc:  # noqa: BLE001 并行开发容错:统一转中文 RuntimeError
        raise RuntimeError(
            f"模块 netsentinel.setup.page 未就位,无法渲染向导页:{exc}"
        ) from exc
    rendered = page_html()
    if not isinstance(rendered, str) or not rendered.strip():
        raise RuntimeError("netsentinel.setup.page.page_html() 返回空页面,无法渲染向导页")
    return rendered


def _safe_value(value: object, depth: int) -> object:
    """深度受限的 JSON 安全化:键名转义、超深截断、异类字符串化、防自引用。"""
    if depth > MAX_DEPTH:
        return _TRUNCATED
    if isinstance(value, dict):
        return {
            _esc(key): _safe_value(item, depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)  # 日期/枚举等异类:字符串化,保证可序列化


def _active_text(active: object) -> str | None:
    """活动模型清洗:仅非空字符串有效,其余(None/空串/非字符串)→ None。"""
    if isinstance(active, str) and active.strip():
        return active
    return None


def _flatten_rows(local_models: object) -> list[dict[str, str]]:
    """A143 服务结果 → 展平行(``ok=False`` 跳过,口径与页面 JS renderLocal 一致)。"""
    rows: list[dict[str, str]] = []
    if not isinstance(local_models, list):
        return rows
    for service in local_models:
        if isinstance(service, str) and service.strip():
            rows.append({"provider": "", "base_url": "", "model": service})
            continue
        if not isinstance(service, dict) or service.get("ok") is False:
            continue
        provider = service.get("provider")
        base = service.get("base_url")
        models = service.get("models")
        if isinstance(models, list) and models:
            for item in models:
                name = _model_name(item)
                if name:
                    rows.append(
                        {
                            "provider": str(provider or "未知提供方"),
                            "base_url": str(base or ""),
                            "model": name,
                        }
                    )
            continue
        name = _model_name(service.get("model")) or _model_name(
            service.get("name")
        ) or _model_name(service.get("id"))
        if name:
            rows.append(
                {
                    "provider": str(provider or "未知提供方"),
                    "base_url": str(base or ""),
                    "model": name,
                }
            )
    return rows


def _model_name(item: object) -> str | None:
    """模型名提取:字符串直用,对象取 ``id``/``name``,其余无效。"""
    if isinstance(item, str) and item.strip():
        return item
    if isinstance(item, dict):
        for key in ("id", "name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _row_html(row: dict[str, str]) -> str:
    """单行静态 HTML(结构镜像 A146 行模板,字段全部转义)。"""
    return (
        '<div class="row" data-testid="local-row">'
        f'<span class="pill" data-testid="row-provider">{_esc(row["provider"])}</span>'
        f'<span class="model" data-testid="row-model">{_esc(row["model"])}</span>'
        f'<span class="muted" data-testid="row-base">{_esc(row["base_url"])}</span>'
        '<button type="button" class="btn" data-testid="btn-enable">启用</button>'
        "</div>"
    )


def _inject_banner(page: str, active: str | None) -> str:
    """① 横幅:active 有效 → 替换默认文案(仅首处、仅该元素)。"""
    if active is None:
        return page
    replaced = _BANNER_RE.sub(
        lambda m: m.group(1) + _BANNER_PREFIX + _esc(active) + m.group(2),
        page,
        count=1,
    )
    return replaced


def _inject_local_rows(page: str, rows: list[dict[str, str]]) -> str:
    """③ 本机初始行:插到 local-list 容器内、行模板之后(模板缺席则容器开头)。"""
    if not rows:
        return page
    anchor = page.find(_LOCAL_LIST_ANCHOR)
    if anchor == -1:
        return page  # 模板漂移容错:宁缺勿错,不硬塞
    close = page.find(_TEMPLATE_CLOSE, anchor)
    if close != -1:
        pos = close + len(_TEMPLATE_CLOSE)
    else:
        tag_end = page.find(">", anchor)
        if tag_end == -1:
            return page
        pos = tag_end + 1
    block = "\n" + "\n".join(_row_html(row) for row in rows)
    return page[:pos] + block + page[pos:]


def _state_script_html(payload: dict[str, object]) -> str:
    """② 状态 JSON 节点:序列化 + ``</`` → ``<\\/`` 注入防护。"""
    text = _json.dumps(payload, ensure_ascii=False)
    text = text.replace("</", "<\\/")
    return f'<script id="{STATE_SCRIPT_ID}" type="application/json">{text}</script>'


def _inject_state(page: str, script: str) -> str:
    """状态节点插入 ``</head>`` 前(缺席时依次回退 ``</body>`` 前 / 文末)。"""
    for marker in ("</head>", "</body>"):
        pos = page.find(marker)
        if pos != -1:
            return page[:pos] + script + "\n" + page[pos:]
    return page + "\n" + script + "\n"


def render_page(
    status: dict | None = None,
    active: str | None = None,
    local_models: list[dict] | None = None,
) -> str:
    """渲染向导页:A146 模板 + 三处注入,返回完整 HTML(确定性、离线、不落盘)。

    :param status: 状态字典(A145 ``status_payload()`` 形态);``None``/非 dict
        容错为 ``{}``,嵌套超过 :data:`MAX_DEPTH` 截断;
    :param active: 活动模型 spec;非空字符串→横幅显示"当前视觉模型:…";
    :param local_models: A143 本机扫描结果;非空→local-list 内注入初始行;
    :returns: 完整单文件 HTML(含 ``ns-state`` 状态 JSON 节点);
    :raises RuntimeError: A146 页面模块缺席/异常(中文消息,供上层回退)。
    """
    page = _load_page()
    safe_status = _safe_value(status, 0) if isinstance(status, dict) else {}
    active_text = _active_text(active)
    safe_local = _safe_value(local_models, 0) if isinstance(local_models, list) else []
    page = _inject_banner(page, active_text)
    page = _inject_local_rows(page, _flatten_rows(local_models))
    page = _inject_state(
        page,
        _state_script_html(
            {"status": safe_status, "active": active_text, "local": safe_local}
        ),
    )
    return page


def state_payload(rendered: str) -> dict:
    """从已渲染页面回读 ``ns-state`` JSON(测试/调测辅助;解析失败抛 ValueError)。"""
    match = _STATE_RE.search(rendered)
    if not match:
        raise ValueError("渲染结果中未找到 ns-state 状态节点")
    return _json.loads(match.group(1))
