"""自定义关键词装载与查询展开(V6.5)。

红线 28:本模块**零内置关键词**——所有搜索词必须来自运营者显式提供:
Python 列表 / .txt(每行一词,# 注释)/ .yaml(列表或 {keywords: [...]})。
模板(可选)支持 ``{kw}`` 占位符,同样由运营者提供,默认空(原样查询)。

V10.20:新增 :func:`load_templates`——模板文件的装载辅助(每行一模板,
``#`` 注释/空行跳过),供 discovery CLI ``--template-file`` 与关键词页联动
预留;未显式传入文件时不产生任何行为变化(默认模板仍为原样查询)。
"""
from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["load_keywords", "load_templates", "expand_queries"]

logger = logging.getLogger(__name__)


def load_keywords(source: str | list[str] | Path) -> list[str]:
    """装载关键词:列表直取;.txt/.yaml 文件解析;去空去重保序。

    :raises ValueError: 文件不存在/格式不符/最终为空(中文消息)。
    """
    if isinstance(source, (list, tuple)):
        words = [str(x).strip() for x in source]
    else:
        path = Path(str(source))
        if not path.is_file():
            raise ValueError(f"关键词文件不存在:{path}")
        text = path.read_text(encoding="utf-8-sig")
        suffix = path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # 惰性
            except ImportError as exc:  # pragma: no cover
                raise ValueError(
                    "读取 YAML 关键词文件需要 PyYAML:pip install PyYAML"
                ) from exc
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                data = data.get("keywords") or data.get("words")
            if not isinstance(data, list):
                raise ValueError(
                    f"关键词 YAML 应为列表或 {{keywords: [...]}} 形态:{path}"
                )
            words = [str(x).strip() for x in data]
        else:
            words = [ln.strip() for ln in text.splitlines()]
    seen: list[str] = []
    dropped = 0
    for w in words:
        if not w or w.startswith("#"):
            dropped += 1
            continue
        if w not in seen:
            seen.append(w)
    if dropped:
        logger.debug("关键词装载:跳过空行/注释 %d 条", dropped)
    if not seen:
        raise ValueError("关键词为空:请提供至少一个非空搜索词(代码不内置任何关键词)")
    return seen


def load_templates(source: str | list[str] | Path) -> list[str]:
    """装载查询模板:列表直取;.txt 每行一模板(``#`` 注释/空行跳过);去空去重保序。

    与 :func:`load_keywords` 同一套文件纪律(utf-8-sig 兼容 BOM、# 注释、
    去重保序);模板可含 ``{kw}`` 占位符(无占位符按前缀拼接,见
    :func:`expand_queries`)。缺省约定文件名 ``<data>/keywords/templates.txt``
    (由调用方拼路径,本函数不做任何路径推断)。

    :raises ValueError: 文件不存在 / 最终为空(中文消息)。
    """
    if isinstance(source, (list, tuple)):
        raw = [str(x).strip() for x in source]
    else:
        path = Path(str(source))
        if not path.is_file():
            raise ValueError(f"模板文件不存在:{path}")
        raw = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines()]
    kept: list[str] = []
    dropped = 0
    for t in raw:
        if not t or t.startswith("#"):
            dropped += 1
            continue
        if t not in kept:
            kept.append(t)
    if dropped:
        logger.debug("模板装载:跳过空行/注释 %d 条", dropped)
    if not kept:
        raise ValueError("模板为空:请提供至少一个非空查询模板(代码不内置任何模板)")
    return kept


def expand_queries(
    keywords: list[str], templates: list[str] | None = None
) -> list[str]:
    """关键词 × 模板展开为查询列表(默认模板为原样查询)。

    模板含 ``{kw}`` 占位符(如 ``"{kw} site:cn"``);无占位符的模板按前缀拼接;
    结果去重保序;空模板列表 → 每词一查询。
    """
    tpl = [t.strip() for t in (templates or []) if t and t.strip()] or ["{kw}"]
    out: list[str] = []
    for kw in keywords:
        for t in tpl:
            q = t.replace("{kw}", kw) if "{kw}" in t else f"{t} {kw}".strip()
            if q and q not in out:
                out.append(q)
    return out
