"""批量清单导入与扫描规划(netsentinel.ops.bulk_intake,A107)。

面向运营者手里的大批线索清单(TXT / CSV / YAML)做**加载 → 归一化 →
拒绝留痕 → 去重规划**,产出"这一批真正要扫哪些站"的执行前规划:

- :func:`load_bulk` 按扩展名分发解析,返回 ``(合法 URL 列表, 拒绝清单)``;
  行级归一化(strip、无 scheme 自动补 ``https://``、校验 http(s) 且有
  host),不合法条目**不中断整批**,而是连同中文原因记入拒绝清单;
  合法 URL 精确串去重保序;
- :func:`plan_scan` 对合法 URL 做 canonical(可注册域)去重——同站多个
  URL 只扫第一个(计 ``duplicate_urls``);再结合站点指纹记忆
  (A39 ``SiteMemory`` 语义,注入或惰性 ``<data_dir>/site_memory.db``)
  把"指纹未变"的站点计入 ``skipped_unchanged``;
- :func:`main` CLI:``--input`` / ``--config`` / ``--plan``,**只加载与
  规划、绝不扫描**——本模块不发起任何网络请求,真实扫描交给 A108。

格式约定:

- ``.txt``:每行一个 URL;``#`` 开头为注释,空行跳过;
- ``.csv``:utf-8(-sig, BOM 容错);``csv.DictReader``;表头需含
  ``url/URL/网址/链接`` 列(ASCII 表头大小写不敏感),缺列整体
  :class:`ValueError`(中文);其余列忽略;
- ``.yaml`` / ``.yml``:顶层为 URL 列表、或 ``{"urls": [...]}`` 映射
  (PyYAML 惰性导入,缺失时抛带安装提示的中文 ValueError)。

测试约定:全部离线;memory 可注入 Fake;惰性记忆路径用 tmp_path 落盘。
"""
from __future__ import annotations

import argparse
import csv
import logging
import pathlib
import re
import urllib.parse
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["load_bulk", "main", "plan_scan"]

logger = logging.getLogger(__name__)

#: 支持的清单扩展名(小写;分发用)
TXT_SUFFIXES = (".txt",)
CSV_SUFFIXES = (".csv",)
YAML_SUFFIXES = (".yaml", ".yml")

#: CSV 表头中可识别为"网址列"的名字(ASCII 项大小写不敏感匹配)
CSV_URL_HEADERS = ("url", "网址", "链接")

#: 站点指纹记忆落盘文件名与 TTL(语义对齐 A39 scheduler / A33 SiteMemory)
MEMORY_DB_NAME = "site_memory.db"
MEMORY_TTL_HOURS = 72

#: scheme 前缀识别(RFC3986 字符集;用于区分"缺 scheme 待补"与"显式坏 scheme")
_SCHEME_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

#: 主机名中不允许出现的字符(补全 https:// 后仍含这些即拒绝)
_BAD_HOST_CHARS = (" ", "\t", "\r", "\n", "\\")

#: fallback canonical 识别"纯 IP 主机"(IPv4 点分 / IPv6 冒号)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: fallback canonical 的常见多段后缀(CONTRACTS-V6 §2 内置表)
_MULTI_SUFFIXES = {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn"}


# ---------------------------------------------------------------------------
# 行级归一化与校验
# ---------------------------------------------------------------------------
def _normalize_entry(raw: str) -> tuple[str, str]:
    """归一化单条线索,返回 ``(合法 URL 或 "", 中文拒绝原因)``——互斥。

    规则:

    1. strip 后为空 → 拒绝"网址为空";
    2. 按 RFC3986 scheme 前缀识别:无 scheme(冒号前缀含点号视作
       ``host:port``,同样算无)→ 自动补 ``https://`` 再校验;
    3. :func:`urllib.parse.urlsplit` 解析失败(如坏 IPv6 括号)→ 拒绝;
    4. 显式 scheme 非 http/https(ftp / mailto / javascript 等)→ 拒绝;
    5. 必须有主机名(``https://`` / ``https:///x`` → 拒绝"无 host");
    6. 主机名含空格/反斜杠等非法字符(典型:无 scheme 的自然语句,
       补全后仍不可修)→ 拒绝。
    """
    s = raw.strip()
    if not s:
        return "", "网址为空"
    match = _SCHEME_PREFIX_RE.match(s)
    if match and "." not in match.group(1):
        scheme = match.group(1).lower()
        if scheme not in ("http", "https"):
            return s, f"协议仅支持 http/https(当前:{scheme})"
        candidate = s
    else:
        candidate = "https://" + s
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return s, "无法解析为合法 URL(格式非法)"
    if parts.scheme not in ("http", "https"):
        return s, f"协议仅支持 http/https(当前:{parts.scheme or '无'})"
    host = parts.hostname or ""
    if not host:
        return s, "缺少主机名(host),不是合法 URL"
    if any(ch in host for ch in _BAD_HOST_CHARS):
        return s, "主机名含空格或非法字符,补全 https:// 后仍不是合法网址"
    return candidate, ""


def _collect(
    raw: str,
    valid: list[str],
    rejected: list[tuple[str, str]],
    seen: set[str],
) -> None:
    """归一化一条 → 计入合法(去重保序)或拒绝清单;单条坏不中断整批。"""
    url, reason = _normalize_entry(raw)
    if reason:
        rejected.append((raw.strip(), reason))
        return
    if url not in seen:  # 精确串去重,保首次出现顺序
        seen.add(url)
        valid.append(url)


# ---------------------------------------------------------------------------
# 三种格式的解析器
# ---------------------------------------------------------------------------
def _load_txt(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """纯文本:每行一个 URL;``#`` 开头整行注释、空行跳过(不计拒绝)。"""
    valid: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()  # CRLF 行尾由 strip 吸收
        if not line or line.startswith("#"):
            continue
        _collect(line, valid, rejected, seen)
    return valid, rejected


def _csv_url_column(fieldnames: list[str] | None, path: pathlib.Path) -> str:
    """在 CSV 表头里找网址列(url/URL/网址/链接,ASCII 大小写不敏感)。

    找不到抛中文 :class:`ValueError`(整体拒绝,含实际表头便于排查)。
    """
    if not fieldnames:
        raise ValueError(
            f"CSV 清单({path})为空,缺少表头;表头需包含 "
            f"{'/'.join(CSV_URL_HEADERS)} 列"
        )
    for name in fieldnames:
        key = str(name or "").strip().lower()
        if key in CSV_URL_HEADERS:
            return str(name)
    raise ValueError(
        f"CSV 清单({path})缺少网址列:表头需包含 {'/'.join(CSV_URL_HEADERS)} "
        f"之一(大小写不敏感),当前表头:{fieldnames}"
    )


def _load_csv(path: pathlib.Path) -> tuple[list[str], list[tuple[str, str]]]:
    """CSV:utf-8-sig 容错读入;DictReader 逐行取网址列归一化。

    完全空行由 DictReader 自动跳过;网址单元格为空的行计入拒绝("网址为空")。
    """
    valid: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        column = _csv_url_column(reader.fieldnames, path)
        try:
            rows: list[dict[str, Any]] = list(reader)
        except csv.Error as exc:
            raise ValueError(f"CSV 清单({path})解析失败:{exc}") from exc
    for row in rows:
        _collect(str(row.get(column) or ""), valid, rejected, seen)
    return valid, rejected


def _import_yaml_optional() -> Any | None:
    """惰性导入 PyYAML;未安装返回 None(此时 .yaml 形态不可用)。"""
    try:
        import yaml
    except ImportError:
        return None
    return yaml


def _load_yaml(path: pathlib.Path, text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """YAML:顶层列表、或 ``{"urls": [...]}`` 映射;元素须为字符串。

    - PyYAML 未安装 → 中文 ValueError(带安装提示);
    - 顶层形态不对 / ``urls`` 不是列表 / YAML 语法损坏 → 整体 ValueError;
    - 列表里的非字符串元素 → 行级拒绝(不中断整批)。
    """
    yaml = _import_yaml_optional()
    if yaml is None:
        raise ValueError(
            f"解析 YAML 清单({path})需要 PyYAML:pip install PyYAML;"
            f"或改用 .txt / .csv 格式(无需额外依赖)"
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 清单损坏,不是合法 YAML:{path}({exc})") from exc

    if data is None:  # 空文件 / 仅注释
        return [], []
    if isinstance(data, dict):
        if "urls" not in data:
            raise ValueError(
                f"YAML 清单({path})顶层映射必须包含 urls 列表,当前键:"
                f"{list(data.keys()) or '(空)'}"
            )
        elems = data["urls"]
        if not isinstance(elems, list):
            raise ValueError(  # noqa: TRY004 - 契约 A107:结构错误统一中文 ValueError
                f"YAML 清单({path})的 urls 必须是列表,当前类型:"
                f"{type(elems).__name__}"
            )
    elif isinstance(data, list):
        elems = data
    else:
        raise ValueError(  # noqa: TRY004 - 契约 A107:结构错误统一中文 ValueError
            f"YAML 清单({path})顶层应为 URL 列表或包含 urls 列表的映射,"
            f"当前类型:{type(data).__name__}"
        )

    valid: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for elem in elems:
        if not isinstance(elem, str):
            rejected.append((str(elem), f"条目应为字符串 URL,当前类型:{type(elem).__name__}"))
            continue
        _collect(elem, valid, rejected, seen)
    return valid, rejected


# ---------------------------------------------------------------------------
# 总入口:按扩展名分发
# ---------------------------------------------------------------------------
def load_bulk(path: str) -> tuple[list[str], list[tuple[str, str]]]:
    """加载批量清单,返回 ``(合法 URL 列表, 拒绝清单[(原始行, 中文原因)])``。

    - 文件不存在 → 中文 :class:`ValueError`(与 watchlist 的"缺省空表"
      语义不同:批量导入是显式动作,丢文件必须立刻暴露);
    - 扩展名分发(大小写不敏感):``.txt`` / ``.csv`` / ``.yaml`` ``.yml``,
      其他扩展名 → 中文 ValueError;
    - 三种格式统一走行级归一化(:func:`_normalize_entry`),坏行只记入
      拒绝清单、绝不中断整批;
    - 合法 URL 按**精确串**去重保序(canonical 级去重在 :func:`plan_scan`)。

    用法示例::

        valid, rejected = load_bulk("leads.txt")
        assert rejected == [] or rejected[0][1]  # 拒绝原因恒为中文
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise ValueError(f"批量清单文件不存在:{p}")
    suffix = p.suffix.lower()
    with telemetry.timer("bulk_intake.load"):
        if suffix in TXT_SUFFIXES:
            valid, rejected = _load_txt(p.read_text(encoding="utf-8-sig"))
        elif suffix in CSV_SUFFIXES:
            valid, rejected = _load_csv(p)
        elif suffix in YAML_SUFFIXES:
            valid, rejected = _load_yaml(p, p.read_text(encoding="utf-8-sig"))
        else:
            raise ValueError(
                f"不支持的批量清单格式:{suffix or '(无扩展名)'};"
                f"仅支持 {'/'.join(TXT_SUFFIXES + CSV_SUFFIXES + YAML_SUFFIXES)}"
            )
    telemetry.inc("bulk_intake.valid", len(valid))
    telemetry.inc("bulk_intake.rejected", len(rejected))
    logger.info(
        "批量清单加载完成:%s,合法 %d 条,拒绝 %d 条", p, len(valid), len(rejected)
    )
    return valid, rejected


# ---------------------------------------------------------------------------
# canonical(A103 惰性)与 fallback
# ---------------------------------------------------------------------------
def _fallback_canonical_key(url: str) -> str:
    """A103 未就位时的最小 canonical:可注册域小写(IP/单标签原样)。

    与 A103 语义对齐的简化版:取 host;``www.`` 前缀剥掉;末两段若命中
    常见多段后缀(com.cn 等)则取末三段,否则取末两段;IP 直连返回 IP 串;
    解析不出 host 返回 ``""``。A103 就位后本函数不会被用到。
    """
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host or _IPV4_RE.match(host):  # IPv6 / IPv4 直连
        return host
    host = host.removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _canonical_key(url: str) -> str:
    """canonical key:惰性导入 A103 ``canonical.canonical_key``,未就位降级。

    解析失败得到空 key 时,调用方应以原串兜底(见 :func:`plan_scan`),
    避免多个坏 URL 塌缩到同一 key 被误判为"同站重复"。
    """
    try:
        from netsentinel.intel.canonical import canonical_key
    except ImportError:
        return _fallback_canonical_key(url)
    try:
        return str(canonical_key(url))
    except Exception:  # A103 异常时降级,规划不中断
        logger.warning("canonical_key(%r) 异常,降级为内置归一", url, exc_info=True)
        return _fallback_canonical_key(url)


# ---------------------------------------------------------------------------
# 站点指纹记忆(A39 语义:注入或惰性 SiteMemory)
# ---------------------------------------------------------------------------
def _default_memory(cfg: Config) -> Any | None:
    """惰性构造站点指纹记忆:``<cfg.data_dir>/site_memory.db``,TTL 72h。

    对齐 A39 scheduler 的缺省工厂;模块未就位或初始化失败时告警并返回
    None(降级为"不跳过",安全方向:宁可多扫,不可漏扫)。
    """
    try:
        from netsentinel.intel.site_memory import SiteMemory
    except ImportError as exc:
        logger.warning("站点指纹记忆模块未就位,规划阶段不做未变跳过:%s", exc)
        return None
    try:
        db_path = str(pathlib.Path(cfg.data_dir) / MEMORY_DB_NAME)
        return SiteMemory(db_path, ttl_hours=MEMORY_TTL_HOURS)
    except Exception as exc:  # noqa: BLE001 - 记忆库不可用只影响跳过
        logger.warning("站点指纹记忆初始化失败,规划阶段不做未变跳过:%s", exc)
        return None


def _last_fingerprint(memory: Any, url: str) -> str:
    """读记忆库中该站点上轮指纹;取不到 / 异常返回空串(视为必扫)。"""
    for name in ("last_fingerprint", "remembered_fingerprint", "get_fingerprint"):
        getter = getattr(memory, name, None)
        if callable(getter):
            try:
                fp = getter(url)
            except Exception:  # noqa: BLE001 - 读失败按必扫处理
                return ""
            return fp if isinstance(fp, str) else ""
    return ""


def _skip_unchanged(memory: Any, url: str) -> bool:
    """判定"指纹未变可不扫":last_fingerprint 非空且 should_rescan 判 False。

    兼容 should_rescan 返回 ``(bool, str)`` 元组或裸 bool;任何异常按
    "需要重扫"处理(安全方向)。
    """
    fp = _last_fingerprint(memory, url)
    if not fp:
        return False
    decider = getattr(memory, "should_rescan", None)
    if not callable(decider):
        return False
    try:
        verdict = decider(url, fp)
    except Exception:  # 比对失败按必扫处理(安全方向)
        logger.warning("指纹比对异常,按需要重扫处理:%s", url, exc_info=True)
        return False
    if isinstance(verdict, tuple):
        return not bool(verdict[0]) if verdict else False
    return not bool(verdict)


# ---------------------------------------------------------------------------
# 扫描规划
# ---------------------------------------------------------------------------
def plan_scan(
    urls: list[str], cfg: Config, *, memory: Any | None = None
) -> dict[str, Any]:
    """对合法 URL 列表做扫描规划(只规划,绝不扫描)。

    规则:

    1. **canonical 去重**:按 A103 ``canonical_key``(惰性,未就位用内置
       降级)归并同站——同站的后续 URL 不进待扫清单,计入
       ``duplicate_urls``;每站只保留**第一个**出现的 URL;
    2. **指纹未变跳过**:``memory`` 注入时直接用;否则惰性构造
       ``SiteMemory``(``<data_dir>/site_memory.db``,TTL 72h,A39 语义);
       ``last_fingerprint`` 非空且 ``should_rescan`` 判 False 的站点计入
       ``skipped_unchanged``;记忆不可用 / 比对异常一律按需扫(安全方向);
    3. 其余进入 ``to_scan``。

    :param urls: 合法 URL 列表(通常来自 :func:`load_bulk`;不做再校验)。
    :param cfg: 配置(仅用 ``data_dir`` 定位惰性记忆库)。
    :param memory: 注入的站点指纹记忆(实现 ``last_fingerprint`` /
        ``should_rescan`` 即可);None 时走惰性缺省。
    :return: ``{"to_scan": [...], "skipped_unchanged": int, "duplicate_urls": int}``。
    """
    mem = memory if memory is not None else _default_memory(cfg)
    to_scan: list[str] = []
    skipped = 0
    duplicates = 0
    seen_sites: set[str] = set()
    with telemetry.timer("bulk_intake.plan"):
        for raw in urls:
            url = raw.strip()
            key = _canonical_key(url) or url  # 坏 URL 不塌缩到同一 key
            if key in seen_sites:
                duplicates += 1
                continue
            seen_sites.add(key)
            if mem is not None and _skip_unchanged(mem, url):
                skipped += 1
                logger.info("规划跳过(指纹未变):%s", url)
                continue
            to_scan.append(url)
    telemetry.inc("bulk_intake.to_scan", len(to_scan))
    telemetry.inc("bulk_intake.skipped_unchanged", skipped)
    telemetry.inc("bulk_intake.duplicate_urls", duplicates)
    return {
        "to_scan": to_scan,
        "skipped_unchanged": skipped,
        "duplicate_urls": duplicates,
    }


# ---------------------------------------------------------------------------
# CLI(只加载/规划,不扫描)
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.ops.bulk_intake",
        description=(
            "批量清单导入:加载 TXT/CSV/YAML 线索清单并归一化去重"
            "(只加载与规划,不发起扫描)"
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help="批量清单路径(.txt/.csv/.yaml/.yml)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径(默认 ./config.yaml,缺失则用默认配置)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="加载后进一步做扫描规划(同站去重 + 指纹未变跳过)",
    )
    return parser


def _print_load_summary(valid: list[str], rejected: list[tuple[str, str]], path: str) -> None:
    """中文打印加载摘要:合法 N / 拒绝 M(前 5 条带原因)。"""
    print(f"批量清单({path})加载完成:合法 URL {len(valid)} 个,拒绝 {len(rejected)} 条")
    for raw, reason in rejected[:5]:
        print(f"  拒绝:{raw} —— {reason}")
    if len(rejected) > 5:
        print(f"  ……其余 {len(rejected) - 5} 条拒绝条目从略(共 {len(rejected)} 条)")


def _print_plan_summary(plan: dict[str, Any]) -> None:
    """中文打印规划摘要:待扫 / 跳过 / 同站重复。"""
    print(
        f"扫描规划:待扫 {len(plan['to_scan'])} 个,"
        f"指纹未变跳过 {plan['skipped_unchanged']} 个,"
        f"同站重复 {plan['duplicate_urls']} 条"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:``--input PATH [--config PATH] [--plan]``。

    - 默认只加载清单并打印中文摘要(合法 N / 拒绝 M,前 5 条附原因)→ 0;
    - ``--plan``:加载后再做扫描规划,追加打印 待扫/跳过/重复 → 0;
    - 清单文件不存在 / 格式缺列 / YAML 损坏等 :class:`ValueError` →
      打印中文错误并返回 2;配置错误同样返回 2;
    - 缺 ``--input`` 由 argparse 报错(SystemExit 2)。
    """
    args = _build_parser().parse_args(argv)
    try:
        from netsentinel.config import load_config

        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"配置错误:{exc}")
        return 2

    try:
        valid, rejected = load_bulk(args.input)
    except ValueError as exc:
        print(f"批量清单加载失败:{exc}")
        return 2

    _print_load_summary(valid, rejected, args.input)
    if args.plan:
        _print_plan_summary(plan_scan(valid, cfg))
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
