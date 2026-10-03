"""a11y 门户理解器:从本地表单 HTML 自动起草门户 YAML(schema v2 候选链)。

对标可访问性树(accessibility tree)式的表单理解:新门户接入时,把保存到
本地的表单页面 HTML 交给本模块,即可得到一份**门户 YAML v2 草稿**——
每字段一条"id 打头 + role/label/placeholder/text 兜底"的自愈候选链,
附逐字段置信度与推断依据。目标:新门户接入从"人工逐字段抄选择器"的
小时级降到"跑一次扫描 + 人工确认"的分钟级。

三层结构:

1. **解析层**(:class:`_FormHTMLParser`,纯 stdlib ``html.parser``):
   提取 input/select/textarea/button 表单模型——``{tag, id, name, type,
   aria-label, role, placeholder, 关联 label, 按钮文本, select 选项,
   文档位置序}``;label 关联走"for/id 显式配对优先,祖先 label 文本兜底"
   的可访问性语义。Playwright 完全可选:本模块**永不导入** Playwright,
   缺席时天然走 stdlib 路径(无任何功能损失,解析本来就不依赖浏览器)。
2. **语义推断**(:func:`infer_fields`):中文关键词 + type 属性 + 标签
   形态的启发式字段分类器(网址/类型/描述/姓名/电话/邮箱/身份证/地址/
   邮编/单位/附件/提交按钮),每字段输出 0-1 置信度与逐条推断依据。
3. **草稿生成**(:func:`generate_draft`):产出 portals YAML v2 文本
   (手写模板,键序固定 = 契约 §3 顺序,注释可读、同输入同输出)。

安全红线(违反即缺陷,与 portal_defs 的加载校验逐条对齐):

1. **产出仅为"门户 YAML 草稿",绝不自动加载/生效**——人工确认是硬性
   语义(对齐 HUMAN_GATE 哲学):草稿头部固定注释
   "草稿:人工确认字段映射后方可放入 portals/ 目录生效";CLI 拒绝把
   ``--out`` 直接写进项目 ``portals/`` 目录;本模块不调用
   ``load_portal_def`` 注册任何门户,不回写任何既有文件。
2. **验证码类字段必须识别并排除 + 显著警告**——任何候选信号(id/name/
   label/aria/placeholder/按钮文本/选项)含"验证码/校验码/captcha"
   字样即判为验证码类:**绝不生成任何指向 captcha 的候选**,该字段记入
   warning 列表并在草稿头部显著警告;反向同理——非 captcha 字段的任何
   兜底候选文本一旦含验证码字样,该候选立即剔除并警告(保证草稿能通过
   portal_defs 加载期的逐候选红线校验)。
3. **零第三方依赖**:解析用 stdlib ``html.parser``;YAML 草稿为手写模板
   (标量经 JSON 转义保证可被 ``yaml.safe_load`` 解析),不依赖 PyYAML
   即可生成;仅入口键合法性预检复用 portal_defs 既有逻辑(同包只读)。
4. **只读领地外**:**URL 输入一律拒绝**——CLI 只接受本地 HTML 文件路径
   (``http://``/``https://``/任何 ``://`` 方案直接退出码 1),绝不联网抓取。

round-trip 保证:生成的草稿必须能通过 ``portal_defs.load_portal_def``
全链校验(``_validate_selectors`` / ``_validate_candidate`` / entry_url_key
检查)——契约要求 selectors 提供全部键且链首 css(#id)打头,因此:

- 语义识别成功且控件有 id → 生成完整候选链(css #id 打头);
- 语义识别成功但控件无 id → 无法满足"css 打头"的 v2 规则,该字段标
  ``manual_required`` **排除出链**,回退契约缺省选择器并显著标注;
- 页面未出现该字段 → 回退契约缺省选择器(manual_required 标注);
- captcha 键 → 沿用契约缺省 ``#report-captcha``,由人工确认改写
  (该选择器只允许用于 FOCUS/HUMAN_GATE 人工输入)。

用法::

    python -m netsentinel.submit.form_scanner 页面.html --out 草稿.yaml

    # 或编程式:
    from netsentinel.submit import form_scanner
    result = form_scanner.scan_html(html_text)
    draft = form_scanner.generate_draft(result, source="页面.html")
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Sequence

from netsentinel import telemetry
from netsentinel.submit import form_models, portal_defs

__all__ = [
    "FormControl",
    "FieldInference",
    "ScanResult",
    "parse_form",
    "scan_html",
    "infer_fields",
    "generate_draft",
    "main",
]


# ---------------------------------------------------------------------------
# 常量:契约键序 / 关键词表 / 验证码字样
# ---------------------------------------------------------------------------
#: 契约 §3 字段键序(草稿 selectors 按此固定顺序输出,与 portals/*.yaml 一致)。
_CONTRACT_KEYS: tuple[str, ...] = tuple(form_models.SELECTORS)

#: 验证码字样(比 portal_defs._CAPTCHA_WORD 更严:多覆盖"校验码/captcha"):
#: 任何候选信号命中即判验证码类 → 识别并排除 + 显著警告。
_CAPTCHA_WORDS: tuple[str, ...] = ("验证码", "校验码", "captcha")

#: 信号来源 → 置信度权重(label/aria 最可信,placeholder 次之,id/name 再次,
#: select 选项文本最弱)。
_SIGNAL_WEIGHT: dict[str, float] = {
    "label": 1.0,
    "aria-label": 1.0,
    "text": 1.0,
    "placeholder": 0.9,
    "id": 0.8,
    "name": 0.8,
    "options": 0.7,
}

#: 字段关键词表:{契约键: ((关键词, 基础置信度), ...)}。
#: 关键词在信号文本中做子串匹配(ASCII 关键词不区分大小写)。
_FIELD_KEYWORDS: dict[str, tuple[tuple[str, float], ...]] = {
    "url": (("链接", 0.85), ("网址", 0.85), ("url", 0.80), ("站点", 0.70), ("网站", 0.65)),
    "type": (("类目", 0.80), ("分类", 0.75), ("类型", 0.70), ("类别", 0.70), ("type", 0.65)),
    "desc": (("描述", 0.85), ("详情", 0.80), ("违法事实", 0.75), ("说明", 0.60)),
    "name": (("姓名", 0.85), ("名字", 0.80), ("昵称", 0.55), ("称呼", 0.50), ("name", 0.50)),
    "phone": (("电话", 0.85), ("手机", 0.85), ("tel", 0.80), ("phone", 0.80),
              ("mobile", 0.80), ("联系方式", 0.70)),
    "email": (("邮箱", 0.85), ("email", 0.85), ("e-mail", 0.85), ("邮件", 0.80), ("mail", 0.75)),
    "id": (("身份证", 0.90), ("证件号", 0.80), ("idcard", 0.75), ("id_no", 0.75), ("idno", 0.75)),
    "address": (("通讯地址", 0.90), ("联系地址", 0.88), ("住址", 0.85), ("地址", 0.62)),
    "postcode": (("邮政编码", 0.90), ("邮编", 0.90), ("zip", 0.80), ("postal", 0.80),
                 ("postcode", 0.80)),
    "org": (("单位", 0.78), ("机构", 0.75), ("组织", 0.72), ("公司", 0.65)),
}

#: 提交按钮关键词(只在 button/submit 型控件上扫描,"举报"单独出现也算——
#: 举报门户的按钮几乎总是提交入口,但置信度低于"提交")。
_SUBMIT_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("提交", 0.95), ("举报", 0.85), ("发送", 0.60), ("确认", 0.60), ("确定", 0.60),
)

#: 多信号交叉加成:每多一个独立信号来源 +0.03,置信度上限 0.99。
_CROSS_BONUS: float = 0.03
_CONF_MAX: float = 0.99

#: 低于该置信度不映射契约键(记入未识别控件,交人工)。
_MIN_CONF: float = 0.50

#: 草稿头部人工确认横幅(硬性语义:人工确认后方可生效)。
_DRAFT_BANNER: str = "草稿：人工确认字段映射后方可放入 portals/ 目录生效"

#: CLI 缺省占位值(round-trip 可加载:均为合法值,人工确认时必须改写)。
_DEFAULT_NAME: str = "未命名门户(form_scanner 自动草稿)"
_DEFAULT_ENTRY_URL_KEY: str = "portal_12377_base"
_DEFAULT_CATEGORY: str = "色情低俗信息"

#: 项目 portals/ 目录(草稿禁止直接写入,防止"未经确认即落位生效")。
_PORTALS_DIR: Path = portal_defs._PROJECT_ROOT / "portals"

#: 纯标识符形态的标量(entry_url_key 等)可不加引号,其余一律 JSON 双引号
#: (JSON 字符串是合法的 YAML 双引号标量,保证 yaml.safe_load 可解析)。
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")

#: label 文本清洗:去掉必填星号与因此留下的空括号("举报链接( * )" → "举报链接")。
_STAR_RE = re.compile(r"[*＊]")
_EMPTY_PARENS_RE = re.compile(r"[（(]\s*[)）]")


def _collapse(text: str) -> str:
    """折叠空白为单个空格并去首尾(保证单行,warning/YAML 安全)。"""
    return " ".join(text.split())


def _tidy_label(text: str) -> str:
    """label/按钮文本清洗:折叠空白 → 去必填星号 → 去空括号。"""
    tidied = _EMPTY_PARENS_RE.sub("", _STAR_RE.sub("", _collapse(text)))
    return _collapse(tidied)


def _yaml_scalar(value: str) -> str:
    """把字符串渲染为 YAML 标量:标识符裸写,其余 JSON 双引号转义。"""
    if _PLAIN_SCALAR_RE.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(eq=False)
class FormControl:
    """一个表单控件的可访问性快照(解析层产物,按文档位置序排列)。

    属性:
        tag: 元素标签(input/select/textarea/button)。
        pos: 文档位置序(1 起,确定性排序用)。
        id / name / type / aria_label / role / placeholder / value: 对应属性。
        label_text: 关联 label 文本(for/id 显式配对优先,祖先 label 兜底)。
        text: 按钮内文本 / textarea 初始内容 / input 的 value 语义文本。
        options: select 的选项文本(用于 type 字段推断)。
    """

    tag: str
    pos: int
    id: str = ""
    name: str = ""
    type: str = ""
    aria_label: str = ""
    role: str = ""
    placeholder: str = ""
    value: str = ""
    label_text: str = ""
    text: str = ""
    options: list[str] = field(default_factory=list)

    def signal_texts(self) -> list[tuple[str, str]]:
        """返回 (信号来源, 文本) 列表,供分类器/验证码检查统一消费。"""
        out: list[tuple[str, str]] = []
        if self.label_text:
            out.append(("label", self.label_text))
        if self.aria_label:
            out.append(("aria-label", self.aria_label))
        if self.placeholder:
            out.append(("placeholder", self.placeholder))
        if self.id:
            out.append(("id", self.id))
        if self.name:
            out.append(("name", self.name))
        if self.text:
            out.append(("text", self.text))
        if self.options:
            out.append(("options", "/".join(self.options[:6])))
        return out

    def describe(self) -> str:
        """单行中文描述(注释/警告用),如 ``input#report-url(name=url, type=text)``。"""
        head = f"{self.tag}#{self.id}" if self.id else f"{self.tag}(无 id)"
        parts: list[str] = []
        if self.name:
            parts.append(f"name={self.name}")
        if self.type and self.tag in ("input", "button"):
            parts.append(f"type={self.type}")
        if self.aria_label:
            parts.append(f"aria-label={self.aria_label}")
        return f"{head}({', '.join(parts)})" if parts else head


@dataclass
class FieldInference:
    """一个契约字段的推断结果(含候选链与人工确认状态)。

    属性:
        key: 契约字段键(url/type/.../captcha/submit)。
        control: 命中的控件;None = 页面未识别到(fallback)。
        confidence: 推断置信度 0-1(未识别=0.00;captcha 识别=0.99)。
        evidence: 逐条推断依据(中文,注释直出)。
        manual_required: True = 候选链是契约缺省回退,必须人工补齐。
        reason: 推断状态:
            ``mapped`` 常规映射;``no-id`` 语义识别但控件无 id(排除出链);
            ``conflict-css`` 控件 id 与其他字段重复(排除出链);
            ``fallback`` 页面未识别到该字段;``captcha-excluded`` 验证码红线。
        candidates: 生成的 v2 候选链(css #id 打头 + 兜底;manual_required
            时为契约缺省单元素链)。
    """

    key: str
    control: FormControl | None
    confidence: float
    evidence: list[str] = field(default_factory=list)
    manual_required: bool = False
    reason: str = "mapped"
    candidates: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ScanResult:
    """一次扫描的完整结果(确定性:同输入同输出)。"""

    controls: list[FormControl] = field(default_factory=list)
    fields: dict[str, FieldInference] = field(default_factory=dict)
    captcha_controls: list[FormControl] = field(default_factory=list)
    unclassified: list[FormControl] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ranked_fields(self) -> list[FieldInference]:
        """按置信度降序(同置信度按契约键序)返回字段推断列表。"""
        order = {key: i for i, key in enumerate(_CONTRACT_KEYS)}
        return sorted(self.fields.values(), key=lambda f: (-f.confidence, order.get(f.key, 99)))


# ---------------------------------------------------------------------------
# 解析层:stdlib html.parser → 表单控件模型
# ---------------------------------------------------------------------------
_DEFAULT_TYPE: dict[str, str] = {"button": "button", "textarea": "textarea", "select": "select"}
_CONTROL_TAGS: tuple[str, ...] = ("input", "select", "textarea", "button")


class _FormHTMLParser(HTMLParser):
    """离线 HTML → :class:`FormControl` 列表(纯 stdlib,不依赖浏览器)。

    - label 关联:``<label for="id">`` 显式配对优先;控件嵌在 ``<label>``
      祖先内时记录隐式文本(可访问性语义两种来源都覆盖);
    - script/style 内容跳过(页面脚本里的"验证码"字样不算表单信号);
    - input 为 void 元素立即落账;select 收集 option 文本;button/
      textarea 收集内嵌文本;
    - 结束标签容错:从未闭合帧向上找同类型帧一并弹出(本地 mock/真实
      页面都可能有轻微不规范标记)。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[FormControl] = []
        self.labels_by_for: dict[str, str] = {}
        self._pos: int = 0
        self._stack: list[tuple[str, dict[str, Any]]] = []
        self._bufs: list[list[str]] = []
        self._skip_depth: int = 0

    # -- 开始标签 ----------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in ("script", "style"):
                self._skip_depth += 1
            return
        if tag in ("script", "style"):
            self._skip_depth = 1
            return
        raw = {k: (v or "") for k, v in attrs}
        if tag == "label":
            buf: list[str] = []
            self._stack.append(
                ("label", {"for": _collapse(raw.get("for", "")), "buf": buf, "controls": []})
            )
            self._bufs.append(buf)
            return
        if tag in _CONTROL_TAGS:
            ctype = _collapse(raw.get("type", "")).lower() or _DEFAULT_TYPE.get(tag, "")
            if ctype == "hidden":
                return  # 隐藏控件不可见,不参与可访问性表单理解
            self._pos += 1
            ctrl = FormControl(
                tag=tag,
                pos=self._pos,
                id=_collapse(raw.get("id", "")),
                name=_collapse(raw.get("name", "")),
                type=ctype,
                aria_label=_collapse(raw.get("aria-label", "")),
                role=_collapse(raw.get("role", "")),
                placeholder=_collapse(raw.get("placeholder", "")),
                value=_collapse(raw.get("value", "")),
            )
            self._register(ctrl)
            if tag == "input":
                ctrl.text = ctrl.value  # input 的可见文本语义即 value
                return
            buf = []
            self._stack.append(("control", {"ctrl": ctrl, "buf": buf}))
            self._bufs.append(buf)
            return
        if tag == "option" and self._enclosing_select() is not None:
            buf = []
            self._stack.append(("option", {"buf": buf}))
            self._bufs.append(buf)

    # -- 文本 --------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        for buf in self._bufs:
            buf.append(data)

    # -- 结束标签 ----------------------------------------------------------
    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in ("script", "style"):
                self._skip_depth -= 1
            return
        if tag == "input":
            return  # void 元素,从未入栈
        want: str | None = None
        if tag == "label":
            want = "label"
        elif tag in ("select", "textarea", "button"):
            want = "control"
        elif tag == "option":
            want = "option"
        if want is None:
            return
        idx: int | None = None
        for i in range(len(self._stack) - 1, -1, -1):
            kind, ctx = self._stack[i]
            if kind != want:
                continue
            if want == "control" and ctx["ctrl"].tag != tag:
                continue
            idx = i
            break
        if idx is None:
            return
        closed = self._stack[idx:]
        del self._stack[idx:]
        for kind, ctx in closed:
            buf: list[str] = ctx["buf"]
            if buf in self._bufs:
                self._bufs.remove(buf)
            text = _collapse("".join(buf))
            if kind == "label":
                if ctx["for"]:
                    self.labels_by_for.setdefault(ctx["for"], text)
                for ctrl in ctx["controls"]:
                    if not ctrl.label_text:
                        ctrl.label_text = text  # 祖先 label 隐式关联(显式配对稍后覆盖)
            elif kind == "control":
                ctrl: FormControl = ctx["ctrl"]
                if ctrl.tag != "select":
                    ctrl.text = text
            elif kind == "option":
                select = self._enclosing_select()
                if select is not None:
                    select.options.append(text)

    # -- 工具 --------------------------------------------------------------
    def _register(self, ctrl: FormControl) -> None:
        """控件落账,并登记到所有祖先 label 上下文(隐式关联)。"""
        self.controls.append(ctrl)
        for kind, ctx in self._stack:
            if kind == "label":
                ctx["controls"].append(ctrl)

    def _enclosing_select(self) -> FormControl | None:
        """栈上最近的 select 控件(option 归属)。"""
        for kind, ctx in reversed(self._stack):
            if kind == "control" and ctx["ctrl"].tag == "select":
                return ctx["ctrl"]
        return None


def parse_form(html_text: str) -> list[FormControl]:
    """解析 HTML 文本,返回按文档位置序排列的表单控件列表(纯 stdlib)。"""
    parser = _FormHTMLParser()
    parser.feed(html_text)
    parser.close()
    for ctrl in parser.controls:
        # label 解析终局:for/id 显式配对优先于祖先 label 隐式文本。
        explicit = parser.labels_by_for.get(ctrl.id, "") if ctrl.id else ""
        ctrl.label_text = _collapse(explicit or ctrl.label_text)
    return parser.controls


# ---------------------------------------------------------------------------
# 语义推断层:字段分类器(置信度 0-1 + 推断依据)
# ---------------------------------------------------------------------------
def _captcha_hit(ctrl: FormControl) -> tuple[str, str, str] | None:
    """验证码检查:任一信号文本含"验证码/校验码/captcha"即命中。

    :return: ``(信号来源, 文本, 命中字样)``;未命中返回 None。
    """
    for source, text in ctrl.signal_texts():
        low = text.lower()
        for word in _CAPTCHA_WORDS:
            if word in low:
                return source, text, word
    return None


def _is_button_like(ctrl: FormControl) -> bool:
    """button 元素或 input[type=submit|button] 视为提交按钮候选。"""
    return ctrl.tag == "button" or ctrl.type in ("submit", "button")


def _implicit_role(ctrl: FormControl) -> str:
    """无显式 role 属性时的隐式 ARIA 角色(textbox/combobox/button)。"""
    if ctrl.tag == "select":
        return "combobox"
    if ctrl.tag == "button":
        return "button"
    if ctrl.type in ("checkbox", "radio"):
        return ctrl.type
    return "textbox"


def _hard_hits(ctrl: FormControl) -> list[tuple[str, float, str, str]]:
    """type 属性 / 标签形态的硬信号:``(契约键, 置信度, 依据, 信号来源)``。"""
    out: list[tuple[str, float, str, str]] = []
    if ctrl.type == "file":
        out.append(("file", 0.95, 'type="file"(文件上传控件)', "type"))
    elif ctrl.type == "tel":
        out.append(("phone", 0.95, 'type="tel"(电话输入)', "type"))
    elif ctrl.type == "email":
        out.append(("email", 0.95, 'type="email"(邮箱输入)', "type"))
    elif ctrl.type == "url":
        out.append(("url", 0.95, 'type="url"(网址输入)', "type"))
    if ctrl.tag == "textarea":
        out.append(("desc", 0.85, "标签 textarea(多行文本通常是具体描述)", "tag"))
    elif ctrl.tag == "select":
        out.append(("type", 0.70, "标签 select(下拉框通常是信息类型)", "tag"))
    return out


def _classify(ctrl: FormControl) -> tuple[str, float, list[str]]:
    """单控件字段分类:返回 ``(契约键, 置信度, 推断依据列表)``。

    - 提交按钮类控件只参与 submit 竞争(button 不映射 url/desc 等);
    - 其余控件:硬信号(type/标签形态)+ 关键词扫描(label/aria/placeholder/
      id/name/text/options 加权)聚合,每键置信度 = 最高命中 +
      0.03×(额外独立信号来源数),上限 0.99;
    - 胜者按 (置信度, 契约键序靠前) 决出;置信度不足 0.5 返回
      ``("", 0.0, [])`` 记入未识别控件。
    """
    scores: dict[str, dict[str, Any]] = {}

    def add(key: str, conf: float, evidence: str, source: str) -> None:
        slot = scores.setdefault(key, {"conf": 0.0, "sources": set(), "evidence": []})
        slot["conf"] = max(slot["conf"], conf)
        slot["sources"].add(source)
        slot["evidence"].append(evidence)

    if _is_button_like(ctrl):
        if ctrl.type == "submit":
            add("submit", 0.90, 'type="submit"(标准提交按钮)', "type")
        for source, text in ctrl.signal_texts():
            if source == "options":
                continue
            weight = _SIGNAL_WEIGHT[source]
            low = text.lower()
            for word, base in _SUBMIT_KEYWORDS:
                if word in low:
                    add("submit", round(base * weight, 4),
                        f'{source}「{text}」命中关键词"{word}"({base * weight:.2f})', source)
        if "submit" not in scores:
            add("submit", 0.50, "按钮控件未命中提交关键词,按控件形态推断(type=button)", "tag")
    else:
        for key, conf, evidence, source in _hard_hits(ctrl):
            add(key, conf, evidence, source)
        for source, text in ctrl.signal_texts():
            weight = _SIGNAL_WEIGHT[source]
            low = text.lower()
            for key, words in _FIELD_KEYWORDS.items():
                for word, base in words:
                    if word.lower() in low:
                        scored = round(base * weight, 4)
                        add(key, scored,
                            f'{source}「{text}」命中关键词"{word}"({scored:.2f})', source)

    if not scores:
        return "", 0.0, []
    order = {key: i for i, key in enumerate(_CONTRACT_KEYS)}
    best_key, best = min(
        scores.items(),
        key=lambda item: (-min(item[1]["conf"] + _CROSS_BONUS * (len(item[1]["sources"]) - 1),
                               _CONF_MAX), order.get(item[0], 99)),
    )
    conf = round(min(best["conf"] + _CROSS_BONUS * (len(best["sources"]) - 1), _CONF_MAX), 2)
    if conf < _MIN_CONF or best_key not in order:
        return "", 0.0, []
    return best_key, conf, best["evidence"]


def _text_captcha_word(text: str) -> str | None:
    """文本型候选值的验证码字样检查(剔除用);返回命中的字样或 None。"""
    low = text.lower()
    for word in _CAPTCHA_WORDS:
        if word in low:
            return word
    return None


def _build_candidates(
    ctrl: FormControl, key: str, forbidden_css: set[str], warnings: list[str]
) -> list[dict[str, str]]:
    """为已映射且有 id 的控件生成 v2 候选链(css #id 打头 + 兜底)。

    兜底顺序固定:role+name(aria 语义)→ label → placeholder →
    text(仅提交按钮)。任何文本型候选含验证码字样立即剔除并警告
    (红线:绝不生成任何指向 captcha 的候选);css 与验证码选择器或
    其他字段重复时整链放弃(交由调用方回退 manual_required)。
    """
    css = f"#{ctrl.id}"
    if css in forbidden_css:
        warnings.append(
            f"字段 {key} 的控件 {ctrl.describe()} 的 id 与验证码选择器冲突,"
            f"已放弃该链并回退契约缺省选择器(manual_required)"
        )
        return []
    role = ctrl.role or _implicit_role(ctrl)
    name = ctrl.aria_label or _tidy_label(ctrl.label_text)
    chain: list[dict[str, str]] = [{"css": css}]

    def push(candidate: dict[str, str], kind: str, text: str) -> None:
        word = _text_captcha_word(text)
        if word:
            warnings.append(
                f'候选已剔除:字段 {key} 的 {kind}「{text}」含"{word}"字样'
                f"(红线:绝不生成任何指向验证码的候选)"
            )
            return
        if candidate not in chain:
            chain.append(candidate)

    if role and name:
        push({"role": role, "name": name}, f"role+name(name={name})", name)
    if ctrl.label_text:
        label = _tidy_label(ctrl.label_text)
        if label:
            push({"label": label}, "label", label)
    if ctrl.placeholder:
        push({"placeholder": ctrl.placeholder}, "placeholder", ctrl.placeholder)
    if key == "submit" and ctrl.text:
        text = _tidy_label(ctrl.text)
        if text:
            push({"text": text}, "text", text)
    return chain


def infer_fields(controls: Sequence[FormControl]) -> ScanResult:
    """语义推断:控件列表 → 契约字段映射 + 验证码排除 + 警告列表。

    流程(确定性,与控件顺序无关的稳定决出规则):
    1. 验证码类字段先行识别并**整体排除**(绝不生成候选),逐控件记警告;
    2. 其余控件逐个分类,按契约键竞争:置信度高者胜,同分取位置靠前者;
       落败且置信度 ≥0.5 的记"多候选控件"警告(人工确认歧义);
    3. 逐契约键建链:有 id → 完整候选链;无 id / css 冲突 → manual_required
       回退契约缺省;页面缺失 → fallback 回退契约缺省。
    """
    warnings: list[str] = []
    captcha_controls = [ctrl for ctrl in controls if _captcha_hit(ctrl)]
    for ctrl in captcha_controls:
        source, text, word = _captcha_hit(ctrl) or ("", "", "")
        warnings.append(
            f"[验证码红线] 已识别验证码类字段并排除:{ctrl.describe()}"
            f'(信号:{source}「{text}」含"{word}")——绝不生成任何指向验证码的候选;'
            "captcha 键沿用契约缺省选择器,请人工确认(仅用于 FOCUS/HUMAN_GATE 人工输入)"
        )
    captcha_ids = {id(ctrl) for ctrl in captcha_controls}
    forbidden_css = {form_models.SELECTORS["captcha"], "#report-captcha"}
    forbidden_css.update(f"#{ctrl.id}" for ctrl in captcha_controls if ctrl.id)

    best: dict[str, dict[str, Any]] = {}
    unclassified: list[FormControl] = []
    for ctrl in controls:
        if id(ctrl) in captcha_ids:
            continue
        key, conf, evidence = _classify(ctrl)
        if not key:
            unclassified.append(ctrl)
            continue
        current = best.get(key)
        if current is None or (conf, -ctrl.pos) > (current["conf"], -current["ctrl"].pos):
            if current is not None and current["conf"] >= _MIN_CONF:
                warnings.append(
                    f"字段 {key} 存在多个候选控件,已取置信度更高者:"
                    f"{ctrl.describe()}(置信度 {conf:.2f})胜出,"
                    f"{current['ctrl'].describe()}(置信度 {current['conf']:.2f})被替代——人工确认"
                )
            best[key] = {"conf": conf, "ctrl": ctrl, "evidence": evidence}
        elif conf >= _MIN_CONF:
            warnings.append(
                f"字段 {key} 的次优候选控件 {ctrl.describe()}(置信度 {conf:.2f})"
                f"未采用(低于胜者 {best[key]['conf']:.2f})——人工确认"
            )

    fields: dict[str, FieldInference] = {}
    used_css: set[str] = set()
    for key in _CONTRACT_KEYS:
        if key == "captcha":
            cap = captcha_controls[0] if captcha_controls else None
            evidence = []
            if cap is not None:
                source, text, word = _captcha_hit(cap) or ("", "", "")
                evidence = [f'{source}「{text}」含"{word}"(已按红线排除,不生成任何候选)']
            fields[key] = FieldInference(
                key=key,
                control=cap,
                confidence=0.99 if cap else 0.0,
                evidence=evidence,
                manual_required=True,
                reason="captcha-excluded",
                candidates=[{"css": form_models.SELECTORS["captcha"]}],
            )
            continue
        entry = best.get(key)
        if entry is None:
            fields[key] = FieldInference(
                key=key,
                control=None,
                confidence=0.0,
                evidence=[],
                manual_required=True,
                reason="fallback",
                candidates=[{"css": form_models.SELECTORS[key]}],
            )
            continue
        ctrl: FormControl = entry["ctrl"]
        conf: float = entry["conf"]
        evidence: list[str] = entry["evidence"]
        if not ctrl.id:
            warnings.append(
                f"[manual_required] 字段 {key} 语义识别为该字段(置信度 {conf:.2f},"
                f"控件 {ctrl.describe()}),但控件无 id——v2 候选链必须 css(#id)打头,"
                f"该字段已排除出链并回退契约缺省选择器 {form_models.SELECTORS[key]},须人工补齐"
            )
            fields[key] = FieldInference(
                key=key, control=ctrl, confidence=conf, evidence=evidence,
                manual_required=True, reason="no-id",
                candidates=[{"css": form_models.SELECTORS[key]}],
            )
            continue
        css = f"#{ctrl.id}"
        if css in used_css:
            warnings.append(
                f"[manual_required] 字段 {key} 的控件 {ctrl.describe()} 的 id "
                f"与其他字段重复({css})——已排除出链并回退契约缺省选择器,须人工补齐"
            )
            fields[key] = FieldInference(
                key=key, control=ctrl, confidence=conf, evidence=evidence,
                manual_required=True, reason="conflict-css",
                candidates=[{"css": form_models.SELECTORS[key]}],
            )
            continue
        used_css.add(css)
        candidates = _build_candidates(ctrl, key, forbidden_css, warnings)
        if not candidates:
            fields[key] = FieldInference(
                key=key, control=ctrl, confidence=conf, evidence=evidence,
                manual_required=True, reason="conflict-css",
                candidates=[{"css": form_models.SELECTORS[key]}],
            )
            continue
        fields[key] = FieldInference(
            key=key, control=ctrl, confidence=conf, evidence=evidence,
            manual_required=False, reason="mapped", candidates=candidates,
        )

    return ScanResult(
        controls=list(controls),
        fields=fields,
        captcha_controls=captcha_controls,
        unclassified=unclassified,
        warnings=warnings,
    )


def scan_html(html_text: str) -> ScanResult:
    """一站式:HTML 文本 → 解析 + 语义推断(纯 stdlib,离线确定性)。"""
    telemetry.inc("form_scanner.scan")
    return infer_fields(parse_form(html_text))


# ---------------------------------------------------------------------------
# 草稿生成层:ScanResult → 门户 YAML v2 文本(手写模板,保证键序与注释)
# ---------------------------------------------------------------------------
def _field_comment(inf: FieldInference) -> list[str]:
    """字段的注释块(状态行 + 依据行),中文、单行、确定性。"""
    if inf.reason == "captcha-excluded":
        lines = [
            f"  # [{inf.key}] 安全红线:验证码字段已识别并排除——本工具绝不生成任何指向验证码的候选 | manual_required",
        ]
        if inf.evidence:
            lines.append(f"  #   识别信号: {inf.evidence[0]}")
        lines.append(
            "  #   此键请人工确认改写;该选择器只允许用于 FOCUS/HUMAN_GATE 人工输入,"
        )
        lines.append("  #   任何自动填写都会在加载期/计划自检期被拒绝。")
        return lines
    if inf.reason == "fallback":
        status = "页面未识别到对应控件——沿用契约缺省选择器 | 置信度 0.00 | manual_required"
    elif inf.reason == "mapped":
        status = f"识别控件:{inf.control.describe()} | 置信度 {inf.confidence:.2f}"
    elif inf.reason == "no-id":
        status = (
            f"语义识别为该字段(置信度 {inf.confidence:.2f},控件 {inf.control.describe()})"
            "但控件无 id,无法生成 css 主候选 | manual_required(已回退契约缺省选择器)"
        )
    else:  # conflict-css
        status = (
            f"语义识别为该字段(置信度 {inf.confidence:.2f},控件 {inf.control.describe()})"
            "但 id 冲突,无法生成唯一 css 主候选 | manual_required(已回退契约缺省选择器)"
        )
    lines = [f"  # [{inf.key}] {status}"]
    for i, item in enumerate(inf.evidence[:4]):
        prefix = "  #   依据: " if i == 0 else "  #         "
        lines.append(f"{prefix}{item}")
    return lines


def _emit_candidates(candidates: list[dict[str, str]]) -> list[str]:
    """候选链 → YAML 行(缩进与 portals/*.yaml 逐字对齐)。"""
    lines: list[str] = []
    for cand in candidates:
        if "css" in cand:
            lines.append(f'    - css: {_yaml_scalar(cand["css"])}')
        elif "role" in cand:
            lines.append(f'    - role: {_yaml_scalar(cand["role"])}')
            lines.append(f'      name: {_yaml_scalar(cand["name"])}')
        elif "label" in cand:
            lines.append(f'    - label: {_yaml_scalar(cand["label"])}')
        elif "placeholder" in cand:
            lines.append(f'    - placeholder: {_yaml_scalar(cand["placeholder"])}')
        elif "text" in cand:
            lines.append(f'    - text: {_yaml_scalar(cand["text"])}')
    return lines


def generate_draft(
    result: ScanResult,
    *,
    source: str,
    name: str = _DEFAULT_NAME,
    entry_url_key: str = _DEFAULT_ENTRY_URL_KEY,
    category: str = _DEFAULT_CATEGORY,
) -> str:
    """把扫描结果渲染为门户 YAML v2 草稿文本(不落盘、不加载、不生效)。

    参数:
        result: :func:`scan_html` 的结果。
        source: 来源 HTML 标识(本地文件路径,进头部注释与 note,可追溯)。
        name / entry_url_key / category: 占位元数据;``entry_url_key`` 必须
            是 ``netsentinel.contracts.Config`` 现有公开字段(复用
            portal_defs 的加载前预检,非法即中文 ValueError)——即便是
            合法值也只是**占位**,人工确认时必须改为新门户真实字段。

    :return: YAML 文本(UTF-8 字符串;头部含人工确认横幅与全部警告)。
    :raises ValueError: name/category 为空,或 entry_url_key 不合 Config 契约。
    """
    for label, value in (("name", name), ("category", category)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"草稿的 {label} 必须是非空字符串,当前值:{value!r}")
    try:
        portal_defs._validate_entry_url_key(entry_url_key)
    except ValueError as exc:
        raise ValueError(
            f"草稿的 entry_url_key 非法({exc});当前 Config 可用入口字段:"
            "portal_12377_base / portal_shdf_base——人工确认时必须改为新门户的入口字段"
        ) from exc
    telemetry.inc("form_scanner.draft")

    warnings = list(result.warnings)
    warnings.append(
        f"entry_url_key={entry_url_key} 为占位值——人工确认时必须改为新门户在"
        " netsentinel.contracts.Config 上真实配置的入口字段名(当前仅有"
        " portal_12377_base / portal_shdf_base)"
    )
    warnings.append("name/category 为占位值——人工确认时必须改为门户正式展示名与举报类目文案")

    mapped = [f for f in result.fields.values() if f.reason == "mapped"]
    manual = [f for f in result.fields.values() if f.manual_required and f.reason != "captcha-excluded"]

    lines: list[str] = []
    bar = "# " + "=" * 68
    lines.append(bar)
    lines.append("# NetSentinel 门户定义草稿(form_scanner 自动生成)——尚未生效")
    lines.append(f"# {_DRAFT_BANNER}")
    lines.append("#")
    lines.append(f"# 来源: {source}(本地 HTML 离线解析;共 {len(result.controls)} 个表单控件,"
                 f"识别出 {len(mapped)} 个契约字段)")
    lines.append("# 解析引擎: stdlib html.parser(可访问性语义:label for/祖先配对、aria-label、"
                 "placeholder)")
    lines.append("#")
    lines.append("# 人工确认清单(逐项确认后方可放入 portals/ 目录生效):")
    lines.append("# 1. entry_url_key 当前为占位值——必须改为新门户的 Config 入口字段名;")
    lines.append("# 2. name/category 当前为占位值——必须改为门户正式展示名与举报类目;")
    lines.append("# 3. captcha 键:本工具按红线【绝不】生成任何指向验证码的候选,")
    lines.append("#    下方 captcha 键沿用契约缺省选择器,须人工核验改写")
    lines.append("#    (该选择器只允许用于 FOCUS/HUMAN_GATE 人工输入);")
    if manual:
        lines.append(f"# 4. manual_required 字段(共 {len(manual)} 个,已回退契约缺省选择器,"
                     "须人工补齐):")
        lines.append(f"#    {', '.join(f.key for f in sorted(manual, key=lambda f: _CONTRACT_KEYS.index(f.key)))}")
    else:
        lines.append("# 4. manual_required 字段:无(全部契约字段均已生成推断候选链);")
    lines.append("# 5. 逐字段核对候选链与置信度/推断依据(见各字段注释);")
    lines.append("#    候选链胜者只进运行时缓存,生效文件不会被自动回写。")
    if result.unclassified:
        described = "; ".join(ctrl.describe() for ctrl in result.unclassified[:8])
        more = f"(共 {len(result.unclassified)} 个)" if len(result.unclassified) > 8 else ""
        lines.append("#")
        lines.append(f"# 未识别控件{more}(不参与草稿,人工确认时可参考): {described}")
    lines.append("#")
    lines.append(f"# 警告(共 {len(warnings)} 条,验证码排除置顶):")
    for warning in warnings:
        lines.append(f"# - {warning}")
    lines.append(bar)
    lines.append(f"name: {_yaml_scalar(name)}")
    lines.append(f"entry_url_key: {_yaml_scalar(entry_url_key)}")
    lines.append(f"category: {_yaml_scalar(category)}")
    lines.append(f"note: {_yaml_scalar('form_scanner 自动草稿(来源:' + _collapse(source) + ');'
                 + _DRAFT_BANNER + '。')}")
    lines.append("selectors:")
    for key in _CONTRACT_KEYS:
        inf = result.fields[key]
        lines.extend(_field_comment(inf))
        lines.append(f"  {key}:")
        lines.extend(_emit_candidates(inf.candidates))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI:python -m netsentinel.submit.form_scanner <本地 html> [--out ...]
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.submit.form_scanner",
        description="a11y 门户理解器:从本地表单 HTML 生成门户 YAML v2 草稿"
                    "(仅草稿,人工确认后方可放入 portals/ 生效;URL 输入一律拒绝)",
    )
    parser.add_argument("html", help="本地 HTML 文件路径(禁止 URL:仅离线解析)")
    parser.add_argument("--out", default=None, help="草稿输出文件(缺省打印到 stdout;"
                        "禁止直接写入项目 portals/ 目录)")
    parser.add_argument("--name", default=_DEFAULT_NAME, help="门户展示名占位值(人工确认)")
    parser.add_argument("--entry-url-key", default=_DEFAULT_ENTRY_URL_KEY,
                        help="Config 入口字段名占位值(须为 Config 现有公开字段)")
    parser.add_argument("--category", default=_DEFAULT_CATEGORY, help="举报类目占位值(人工确认)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口,退出码 0=草稿生成成功 / 1=拒绝或失败(中文错误到 stderr)。"""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return 1  # 参数错误也归一为退出码 1(契约:0/1)

    raw = args.html
    if "://" in raw or raw.lower().startswith(("http://", "https://", "file://")):
        print(
            f"[form_scanner][错误] 输入必须是本地 HTML 文件路径,禁止 URL:{raw}"
            "(红线:离线解析,绝不联网抓取)",
            file=sys.stderr,
        )
        return 1
    path = Path(raw)
    if not path.is_file():
        print(f"[form_scanner][错误] HTML 文件不存在:{path}", file=sys.stderr)
        return 1

    out_path: Path | None = None
    if args.out:
        out_path = Path(args.out)
        try:
            resolved_parent = out_path.resolve().parent
            portals_resolved = _PORTALS_DIR.resolve()
        except OSError:
            resolved_parent = None
        if resolved_parent is not None and resolved_parent == portals_resolved:
            print(
                f"[form_scanner][错误] 拒绝把草稿直接写入 portals/ 目录:{out_path}"
                "——产出仅为草稿,人工确认字段映射后方可放入(红线:绝不自动生效)",
                file=sys.stderr,
            )
            return 1

    html_text = path.read_text(encoding="utf-8-sig", errors="replace")
    result = scan_html(html_text)
    if not result.controls:
        print(
            f"[form_scanner][错误] 未在 {path} 中发现任何表单控件"
            "(input/select/textarea/button),无法起草",
            file=sys.stderr,
        )
        return 1

    try:
        draft = generate_draft(
            result,
            source=raw,
            name=args.name,
            entry_url_key=args.entry_url_key,
            category=args.category,
        )
    except ValueError as exc:
        print(f"[form_scanner][错误] {exc}", file=sys.stderr)
        return 1

    for warning in result.warnings:
        print(f"[form_scanner][警告] {warning}", file=sys.stderr)

    if out_path is not None:
        try:
            out_path.write_text(draft, encoding="utf-8")
        except OSError as exc:
            print(f"[form_scanner][错误] 草稿写入失败:{out_path}({exc})", file=sys.stderr)
            return 1
        print(
            f"[form_scanner] 草稿已写入:{out_path}"
            f"(警告 {len(result.warnings)} 条;人工确认字段映射后方可放入 portals/ 目录生效)",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(draft)
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工离线使用入口
    sys.exit(main())
