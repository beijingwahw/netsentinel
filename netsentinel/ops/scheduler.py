"""NetSentinel(净网哨兵)巡查调度器(ops.scheduler,A39)。

面向运营者维护的 watchlist 做**定期顺序巡查**:

- :func:`load_watchlist` 解析 watchlist,自动识别三种格式:
  YAML 列表(``- https://...``)、YAML 映射(``items: [{url, note, enabled}]``)、
  纯文本(每行一个 URL,``#`` 开头为注释;PyYAML 缺失时的最小格式);
- :func:`run_once` 执行一轮:逐项调 ``run_scan``(缺省惰性导入编排器,
  可注入 fake)→ 站点指纹去重 → 需人工复核时发一条通知 → 项间礼貌抖动;
- :func:`main` 提供 CLI:``--list`` / ``--once`` / ``--loop --interval-min``。

站点指纹去重语义("先扫后记"简化):本轮开始时若记忆库中存有该站点上一轮
的指纹且未过 TTL,则直接跳过(``should_rescan(url, remembered_fp)`` 返回
不必重扫);本轮扫描成功后用报告重算指纹并 ``remember``。指纹只能来自
扫描产出的报告,因此"跳过"判断永远基于上轮记忆,不会凭空造指纹。

动态 TTL(A211,默认关):cfg 附加属性 ``dynamic_ttl``(getattr 读取,
缺省 ``False`` = 现状逐字节)开启时,对每个待跳过判定的站点取图谱
(:mod:`netsentinel.intel.graph`,只读)中与其相关的 ``phash_near`` /
``redirect`` 边 ``created_at`` 时间戳,经 :mod:`netsentinel.intel.temporal`
的爆发检测链(kleinberg_bursts → burst_factor → suggest_ttl)给出**收缩后
的建议 TTL**,以 ``should_rescan(..., ttl_hours=建议值)`` 单次覆写跳过档;
无边 / 无爆发 / 图谱异常一律回退库级默认 TTL。红线 35:建议 TTL **只**
影响"是否需要重扫"的判定,绝不缩短礼貌停顿(:data:`PAUSE_BASE_S` /
:data:`PAUSE_JITTER_S`)、引擎限速或举报频控中的任何一个数值——巡查
节奏与现状完全相同,变的只是"指纹未变时跳过多久"这一档。

安全红线(必须体现在代码里):
1. **绝不为未列入 watchlist 的目标发起扫描**——本模块唯一的目标来源是
   ``load_watchlist`` 的返回值,没有任何旁路入口;
2. 遵守 ``allow_network``(由下层 fetcher 把关,本模块自身不直接发起
   任何网络请求);
3. 单轮中单项扫描失败只计数并继续,绝不中断整轮巡查;
4. 通知**单发不重试**:发送失败仅记 warning,不轰炸 webhook。

测试约定:``run_scan`` / ``sleep`` / ``memory`` / ``notify`` / ``random``
/ ``graph`` 均可注入;缺省实现收敛到模块级 ``_default_*`` 工厂(内部惰性导入
兄弟模块,未就位时优雅降级),全部离线、零真实 sleep 可测。动态 TTL 的
"当前时刻"经模块级 :func:`_now_hours` 注入(小时,自 Unix epoch),测试
monkeypatch 即可固定时钟,确定性断言。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import pathlib
import random as _random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.intel.temporal import burst_factor, kleinberg_bursts, suggest_ttl

__all__ = ["WatchItem", "load_watchlist", "run_once", "main"]

logger = logging.getLogger(__name__)

#: 循环模式的默认轮间隔(分钟):12 小时
DEFAULT_INTERVAL_MIN = 720

#: 相邻两次真实扫描之间的基础停顿(秒),叠加 0~0.5s 随机抖动(礼貌抓取)
PAUSE_BASE_S = 1.0
PAUSE_JITTER_S = 0.5

#: 站点指纹记忆的默认 TTL(小时)与落盘文件名(相对 cfg.data_dir)
MEMORY_TTL_HOURS = 72
MEMORY_DB_NAME = "site_memory.db"

#: 动态 TTL(A211)参与爆发检测的边种类:团伙扩张的时序痕迹——
#: 感知哈希近重复(phash_near)与重定向(redirect);shared_image /
#: shared_template 由采集批次批量折叠,时间密度不反映"新团伙冒头"。
DYN_TTL_EDGE_KINDS = ("phash_near", "redirect")

#: 动态 TTL 降级(图谱不可用 / 查询失败 / 计算异常)的遥测计数器名。
DYN_TTL_DEGRADED_METRIC = "scheduler.dyn_ttl.degraded"

#: created_at 小时量纲换算的固定纪元:Unix epoch(1970-01-01T00:00:00Z)。
#: events 与 now(见 :func:`_now_hours` / :func:`_iso_hours`)全部以
#: "自本纪元起的小时数"为量纲,与 intel.temporal 的入参约定一致。
_UNIX_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

#: YAML 顶层键形态(如 ``items:``);用于区分 YAML 与纯文本格式
_TOP_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*\s*:(\s|$)")


# ---------------------------------------------------------------------------
# watchlist 数据结构与解析
# ---------------------------------------------------------------------------
@dataclass
class WatchItem:
    """watchlist 中的一条巡查目标。"""

    url: str                    # 完整站点地址,必须 http(s):// 开头
    note: str = ""              # 中文备注(线索来源、加入原因等)
    enabled: bool = True        # False 时每轮跳过(留档不巡查)


def _import_yaml_optional() -> Any | None:
    """惰性导入 PyYAML;未安装返回 None(此时仅支持纯文本格式)。"""
    try:
        import yaml
    except ImportError:
        return None
    return yaml


def _check_url(url: str, path: pathlib.Path) -> str:
    """校验单个 URL 形态并返回去除首尾空白后的值;不合法抛 ValueError(中文)。

    这是调度器的目标准入检查:凡进入巡查流程的 URL 必须显式写在
    watchlist 里且形如 http(s)://……。
    """
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"watchlist({path})条目 URL 必须以 http:// 或 https:// 开头,当前为:{u!r}"
        )
    return u


def _scan_lines(text: str) -> tuple[list[str], bool]:
    """单遍扫描 watchlist 文本:收集非空非注释行,同时判定是否应按 YAML 解析。

    与旧实现(先建过滤行集合、再对其二次遍历判断 YAML 形态)行为完全
    等价:首个有效行以 ``- `` / ``[`` 开头、或任一有效行匹配顶层键 /
    列表项形态 → YAML;区别只在每个物理行只 strip 一次(V5 性能升级,
    行级处理量从 2N 降为 N)。CRLF 行尾由 :meth:`str.strip` 一并吸收。
    """
    content: list[str] = []
    looks_yaml = False
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if not content and (ln.startswith("- ") or ln.startswith("[")):
            looks_yaml = True
        if _TOP_KEY_RE.match(ln) or ln.startswith("- "):
            looks_yaml = True
        content.append(ln)
    return content, looks_yaml


def _parse_plain_items(lines: list[str], path: pathlib.Path) -> list[WatchItem]:
    """纯文本格式:每行一个 URL(# 开头整行为注释,已在调用方剔除)。"""
    return [WatchItem(url=_check_url(ln, path)) for ln in lines]


def _element_to_item(elem: Any, path: pathlib.Path) -> WatchItem:
    """把 YAML 列表中的一个元素(str 或映射)规整为 WatchItem。"""
    if isinstance(elem, str):
        return WatchItem(url=_check_url(elem, path))
    if isinstance(elem, dict):
        url = elem.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"watchlist({path})条目缺少合法的 url 字段(字符串),当前为:{elem!r}"
            )
        note = elem.get("note", "")
        if not isinstance(note, str):
            raise ValueError(
                f"watchlist({path})条目 note 应为字符串,当前为:{elem!r}"
            )
        enabled = elem.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"watchlist({path})条目 enabled 应为布尔(true/false),当前为:{elem!r}"
            )
        for key in elem:
            if key not in ("url", "note", "enabled"):
                logger.warning("忽略 watchlist 条目中的未知字段:%s(条目 %s)", key, url)
        return WatchItem(url=_check_url(url, path), note=note, enabled=enabled)
    raise ValueError(
        f"watchlist({path})条目应为 URL 字符串或含 url/note/enabled 的映射,当前为:{elem!r}"
    )


def _load_yaml_items(
    text: str, content_lines: list[str], path: pathlib.Path
) -> list[WatchItem]:
    """解析 YAML 格式的 watchlist(列表 / ``items:`` 映射两种顶层形态)。"""
    yaml = _import_yaml_optional()
    if yaml is None:
        raise ValueError(
            f"解析 YAML 格式的 watchlist({path})需要 PyYAML:pip install PyYAML;"
            f"或改用纯文本格式(每行一个 URL,# 开头为注释)"
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"watchlist 文件损坏,不是合法 YAML:{path}({exc})") from exc

    if data is None:  # 空文件 / 仅注释
        return []
    if isinstance(data, str):
        # 多行纯文本会被 YAML 折叠成单个字符串标量,退回逐行解析
        lines = [
            ln.strip()
            for ln in data.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        return _parse_plain_items(lines, path)
    if isinstance(data, dict):
        elems = data.get("items")
        if not isinstance(elems, list):
            raise ValueError(
                f"watchlist({path})顶层映射必须包含 items 列表,"
                f"当前缺失或类型为 {type(elems).__name__}"
            )
    elif isinstance(data, list):
        elems = data
    else:
        raise ValueError(
            f"watchlist({path})顶层应为列表、或包含 items 列表的映射,"
            f"当前类型为 {type(data).__name__}"
        )
    return [_element_to_item(e, path) for e in elems]


def load_watchlist(path: str) -> list[WatchItem]:
    """加载巡查目标表。

    - 文件不存在:返回 ``[]`` 并记 warning"未找到 watchlist,本轮无巡查目标";
    - 自动识别格式:YAML 列表 / ``{"items": [...]}`` 映射 / 纯文本(每行一个
      URL,``#`` 注释;PyYAML 未安装时唯一可用格式);
    - 文件损坏或结构 / URL 不合法:抛 :class:`ValueError`(中文消息)。

    :param path: watchlist 文件路径(通常来自 ``cfg.watchlist_path``)。
    """
    p = pathlib.Path(path)
    if not p.is_file():
        logger.warning("未找到 watchlist,本轮无巡查目标:%s", p)
        return []
    text = p.read_text(encoding="utf-8")
    content, looks_yaml = _scan_lines(text)  # 单遍:过滤 + YAML 形态判定
    if not content:  # 空文件 / 仅注释
        return []
    if looks_yaml:
        return _load_yaml_items(text, content, p)
    return _parse_plain_items(content, p)


# ---------------------------------------------------------------------------
# 缺省实现工厂(模块级,便于测试 monkeypatch;内部惰性导入兄弟模块)
# ---------------------------------------------------------------------------
def _default_run_scan(url: str, cfg: Config) -> Any:
    """缺省扫描实现:惰性导入编排器 run_scan(返回 SiteReport 或鸭子等价物)。"""
    try:
        from netsentinel.pipeline import orchestrator
    except ImportError as exc:
        raise RuntimeError(
            f"模块 netsentinel.pipeline.orchestrator 未就位:{exc}"
        ) from exc
    return orchestrator.run_scan(url, cfg)


def _default_sleep(seconds: float) -> None:
    """缺省停顿实现(测试注入 fake 以避免真实等待)。"""
    time.sleep(seconds)


def _default_random() -> float:
    """缺省抖动随机源:返回 [0, 1) 内一个浮点。"""
    return _random.random()


def _default_memory(cfg: Config) -> Any:
    """缺省站点指纹记忆:独立路径 ``<data_dir>/site_memory.db``,TTL 72h。

    惰性导入 A33 ``netsentinel.intel.site_memory.SiteMemory``;未就位时抛
    中文 RuntimeError,由 :func:`run_once` 捕获后降级为"本轮不去重"。
    """
    try:
        from netsentinel.intel.site_memory import SiteMemory
    except ImportError as exc:
        raise RuntimeError(
            f"模块 netsentinel.intel.site_memory 未就位:{exc}"
        ) from exc
    db_path = str(pathlib.Path(cfg.data_dir) / MEMORY_DB_NAME)
    return SiteMemory(db_path, ttl_hours=MEMORY_TTL_HOURS)


def _default_notify(cfg: Config, event: str, text: str) -> bool:
    """缺省通知实现:惰性导入 notify.hub;单发、不重试、异常不外抛。"""
    try:
        from netsentinel.notify.hub import notify
    except ImportError as exc:
        logger.warning("通知模块 netsentinel.notify.hub 未就位,本次提醒未发送:%s", exc)
        return False
    try:
        return bool(notify(cfg, event, text))
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不拖垮巡查
        logger.warning("通知发送失败(单发不重试):%s", exc)
        return False


def _default_graph(cfg: Config) -> Any:
    """缺省图谱实现:惰性导入 A46 ``netsentinel.intel.graph.EvidenceGraph``。

    仅用于动态 TTL 的**只读**消费(:meth:`EvidenceGraph.export_json`);
    未就位时抛中文 RuntimeError,由调用方捕获后安全降级为库级默认 TTL
    (详见 :func:`_load_edge_hours`)。本模块绝不向图谱写入任何数据。
    """
    try:
        from netsentinel.intel.graph import EvidenceGraph
    except ImportError as exc:
        raise RuntimeError(
            f"模块 netsentinel.intel.graph 未就位:{exc}"
        ) from exc
    return EvidenceGraph(getattr(cfg, "graph_db", "data/graph.db"))


# ---------------------------------------------------------------------------
# 指纹记忆的读写辅助(尽力而为,任何异常都不影响巡查本身)
# ---------------------------------------------------------------------------
#: 进程内"轮次间指纹"兜底缓存:key=(记忆库标识, 站点 URL) -> 上轮指纹。
#: 仅当注入的记忆对象未提供取指纹方法时启用(真实 SiteMemory 带 db_path
#: 属性即可在 --loop 模式下跨轮去重;跨进程去重以记忆库自身落盘为准)。
_REMEMBERED_FP: dict[tuple[str, str], str] = {}


def _memory_key(memory: Any) -> str:
    """记忆库标识:有 db_path 属性用其路径,否则返回空串(不启用兜底)。"""
    db_path = str(getattr(memory, "db_path", "") or "")
    return f"db:{db_path}" if db_path else ""


def _last_fingerprint(memory: Any, url: str) -> str | None:
    """读记忆库中该站点上轮记住的指纹;取不到返回 None(视为必扫,安全方向)。"""
    for name in (
        "last_fingerprint",
        "remembered_fingerprint",
        "get_fingerprint",
        "fingerprint_of",
    ):
        getter = getattr(memory, name, None)
        if callable(getter):
            try:
                fp = getter(url)
            except Exception:  # noqa: BLE001 - 读失败按必扫处理
                return None
            return fp if isinstance(fp, str) and fp else None
    key = _memory_key(memory)
    if key:
        return _REMEMBERED_FP.get((key, url))
    return None


def _remember_fingerprint(memory: Any, url: str, fp: str) -> None:
    """把指纹写入记忆库并同步进程内兜底表;失败仅告警。"""
    try:
        memory.remember(url, fp)
    except Exception as exc:  # noqa: BLE001 - 记忆失败不影响本轮结果
        logger.warning("写入站点指纹记忆失败(不影响本轮结果):%s %s", url, exc)
        return
    key = _memory_key(memory)
    if key:
        _REMEMBERED_FP[(key, url)] = fp


# ---------------------------------------------------------------------------
# 动态 TTL(A211):消费 intel.temporal 爆发检测的建议重扫间隔
# 红线 35:以下全部代码只影响 should_rescan 的 ttl_hours 入参,绝不触碰
# 礼貌抖动(PAUSE_BASE_S/PAUSE_JITTER_S)、引擎限速或举报频控的任何数值。
# ---------------------------------------------------------------------------
def _now_hours() -> float:
    """当前时刻(小时,自 Unix epoch)——动态 TTL 的时间注入缝。

    测试 monkeypatch 本函数即可固定时钟(确定性断言);生产路径即
    ``time.time() / 3600``。量纲与 :func:`_iso_hours` 一致,二者相减 /
    比较(经 intel.temporal 的 burst_factor)才有意义。
    """
    return time.time() / 3600.0


def _iso_hours(ts: Any) -> float | None:
    """ISO8601 时间戳(如图谱边 created_at)→ 自 Unix epoch 的小时数。

    无时区信息按 UTC 对待(与 intel.site_memory._parse_ts 同口径);空串 /
    无法解析返回 None(调用方按"该边不贡献事件"处理,安全方向)。
    """
    if not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return (dt - _UNIX_EPOCH).total_seconds() / 3600.0


def _edge_hours_index(graph: Any) -> dict[str, list[float]]:
    """图谱只读快查:站点节点 id(或裸 URL)→ 相关边 created_at 小时序。

    一次 :meth:`export_json` 全量导出后过滤(每轮一次,不在循环内反复
    查询),只保留 :data:`DYN_TTL_EDGE_KINDS` 两种边(phash_near /
    redirect);端点两个方向都建索引(图谱边视为无向);created_at 无法
    解析的边直接跳过(不贡献事件,安全方向)。返回的键同时含
    ``site:<url>``(真实图谱的节点 id 形态)与裸 URL(容忍简化 fake)。
    """
    data = graph.export_json() or {}
    index: dict[str, list[float]] = {}
    for edge in data.get("edges", ()) or ():
        kind = str(edge.get("kind", "") or "")
        if kind not in DYN_TTL_EDGE_KINDS:
            continue
        hours = _iso_hours(edge.get("created_at", ""))
        if hours is None:
            continue
        for endpoint in (edge.get("src", ""), edge.get("dst", "")):
            key = str(endpoint or "")
            if key:
                index.setdefault(key, []).append(hours)
    return index


def _load_edge_hours(cfg: Config, graph: Any | None) -> dict[str, list[float]] | None:
    """备好本轮动态 TTL 的边时间索引;任何失败安全降级为 ``None``。

    ``None`` 语义 = 本轮全部站点沿用库级默认 TTL(现状行为),并累加遥测
    计数 :data:`DYN_TTL_DEGRADED_METRIC`(``scheduler.dyn_ttl.degraded`):
    覆盖图谱模块未就位、库打开失败、查询异常三类情形。仅当调用方未注入
    graph 时才自行打开 ``cfg.graph_db``(用毕即关);注入的 graph 归调用
    方管理,本函数不关闭。
    """
    own = False
    if graph is None:
        try:
            graph = _default_graph(cfg)
            own = True
        except Exception as exc:  # noqa: BLE001 - 图谱不可用只影响动态 TTL
            telemetry.inc(DYN_TTL_DEGRADED_METRIC)
            logger.warning(
                "动态 TTL 图谱不可用,本轮全部站点沿用库级默认 TTL:%s", exc
            )
            return None
    try:
        return _edge_hours_index(graph)
    except Exception as exc:  # noqa: BLE001 - 查询失败只影响动态 TTL
        telemetry.inc(DYN_TTL_DEGRADED_METRIC)
        logger.warning(
            "动态 TTL 图谱查询失败,本轮全部站点沿用库级默认 TTL:%s", exc
        )
        return None
    finally:
        if own:
            closer = getattr(graph, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 关闭失败无需上抛
                    logger.debug("关闭动态 TTL 图谱连接时出现异常", exc_info=True)


def _dynamic_ttl_for(
    url: str,
    edge_index: dict[str, list[float]] | None,
    base_ttl_h: float,
    now_hours: float,
) -> float | None:
    """计算单站建议重扫 TTL(小时);一切"算不出/不必算"均返回 ``None``。

    链路(A207 时序内核,量纲全部为自 Unix epoch 的小时数):

    1. 取该站点相关边(端点匹配 ``site:<url>`` 或裸 URL)的 created_at
       小时序;无边 → ``None``(用库级默认,不进爆发检测);
    2. ``kleinberg_bursts(events)`` 检测爆发段;无 burst → 强度 0;
    3. ``burst_factor(bursts, now_hours)`` 取当前爆发强度 [0, 1];
       强度 ≤ 0(稳态/爆发久远已衰减尽)→ ``None``——保证**无爆发时
       与现状逐字节一致**;
    4. ``suggest_ttl(base_ttl_h, factor)`` 给出收缩后的建议值(对 factor
       单调不增,内部钳制在 [6, 720] 小时,与 ops.adaptive 同口径)。

    计算异常同样安全降级:计 :data:`DYN_TTL_DEGRADED_METRIC` 并返回
    ``None``(该站点用库级默认 TTL)。红线 35:返回值只喂给
    ``should_rescan(..., ttl_hours=...)``,不参与任何停顿 / 频控计算。
    """
    if edge_index is None:
        return None
    events = edge_index.get(f"site:{url}") or edge_index.get(url) or ()
    if not events:
        return None
    try:
        bursts = kleinberg_bursts(events)
        factor = burst_factor(bursts, now_hours)
        if factor <= 0.0:
            return None
        return suggest_ttl(base_ttl_h, factor)
    except Exception as exc:  # noqa: BLE001 - 单站失败不影响其余站点
        telemetry.inc(DYN_TTL_DEGRADED_METRIC)
        logger.warning(
            "动态 TTL 计算失败,该站点沿用库级默认 TTL:%s %s", url, exc
        )
        return None


# ---------------------------------------------------------------------------
# 单轮巡查
# ---------------------------------------------------------------------------
def run_once(
    cfg: Config,
    *,
    run_scan: Callable[[str, Config], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    memory: Any | None = None,
    graph: Any | None = None,
    notify: Callable[..., bool] | None = None,
    random: Callable[[], float] | None = None,
    skip_if_unchanged: bool = True,
) -> dict[str, Any]:
    """执行一轮巡查,返回本轮摘要。

    流程(对每个 ``enabled`` 条目顺序执行):

    1. 指纹去重(``skip_if_unchanged=True`` 时):取记忆库中该站点上轮指纹,
       ``should_rescan(url, remembered_fp)`` 判定不必重扫(指纹未变且未过
       TTL)则跳过并计数;cfg 附加属性 ``dynamic_ttl``(默认 False=关)
       开启时,先经图谱边时间戳 + 爆发检测算出收缩后的建议 TTL,再以
       ``should_rescan(url, fp, ttl_hours=建议值)`` 单次覆写跳过档
       (A211;无边 / 无爆发 / 图谱异常均回退库级默认,与现状一致);
    2. 调 ``run_scan(url, cfg)``;单项抛异常只记入 ``errors`` 并继续下一项;
    3. 扫描成功后 ``fp = memory.fingerprint(report)`` 并 ``remember(url, fp)``
       供下一轮去重("先扫后记":指纹只能来自报告);
    4. 报告 ``needs_review`` 时经 ``notify`` 单发一条 ``pending_review`` 提醒
       (站点 + verdict + agg),发送失败不重试;
    5. 每个真实触网的项(扫描成功或失败)之后停顿
       ``1.0 + random()*0.5`` 秒(礼貌抖动;纯跳过的项不停顿)。

    可观测性(V5):整轮耗时记 ``telemetry.timer("scheduler.round")``,
    每个条目的处理耗时记 ``telemetry.timer("scheduler.item")``,并按结局
    累加 ``scheduler.scanned`` / ``scheduler.skipped`` / ``scheduler.failed``
    三个计数器(只存名称与数字,不涉及站点内容)。动态 TTL 降级时累加
    ``scheduler.dyn_ttl.degraded``(A211)。

    用法示例::

        summary = run_once(cfg, run_scan=fake_scan, sleep=fake_sleep)
        assert summary["scanned"] == 2 and summary["failed"] == 0

    :param run_scan: 注入的扫描函数,签名 ``(url, cfg) -> report``;
        缺省惰性导入 ``pipeline.orchestrator.run_scan``。
    :param sleep: 注入的停顿函数,签名 ``sleep(seconds)``;缺省 ``time.sleep``。
    :param memory: 注入的站点指纹记忆(须实现 fingerprint/remember/
        should_rescan);缺省惰性构造 ``SiteMemory``(未就位则降级为不去重)。
    :param graph: 注入的站点关联图谱(仅 ``dynamic_ttl`` 开启时消费其
        ``export_json()``,只读);缺省惰性构造 ``EvidenceGraph(cfg.graph_db)``。
    :param notify: 注入的通知函数,签名 ``(cfg, event, text) -> bool``;
        缺省惰性导入 ``notify.hub``。
    :param random: 注入的 [0,1) 随机源;缺省 ``random.random``。
    :param skip_if_unchanged: False 时忽略指纹记忆,全部重扫(仍会记忆)。
    :return: ``{"total", "scanned", "skipped", "failed", "notified",
        "errors": [(url, 错误信息), ...]}``。
    """
    with telemetry.timer("scheduler.round"):
        return _run_round(
            cfg,
            run_scan=run_scan, sleep=sleep, memory=memory, graph=graph,
            notify=notify, random=random, skip_if_unchanged=skip_if_unchanged,
        )


def _run_round(
    cfg: Config,
    *,
    run_scan: Callable[[str, Config], Any] | None,
    sleep: Callable[[float], None] | None,
    memory: Any | None,
    graph: Any | None,
    notify: Callable[..., bool] | None,
    random: Callable[[], float] | None,
    skip_if_unchanged: bool,
) -> dict[str, Any]:
    """单轮巡查主体(:func:`run_once` 的实现,外层负责 round 级计时)。"""
    items = [it for it in load_watchlist(cfg.watchlist_path) if it.enabled]
    scan = run_scan if run_scan is not None else _default_run_scan
    pause = sleep if sleep is not None else _default_sleep
    jitter = random if random is not None else _default_random
    notify_fn = notify if notify is not None else _default_notify

    if memory is None:
        try:
            memory = _default_memory(cfg)
        except Exception as exc:  # noqa: BLE001 - 记忆库不可用只影响去重
            logger.warning("站点指纹记忆不可用,本轮不去重(全部重扫):%s", exc)
            memory = None

    # A211 动态 TTL:开关关闭(缺省)时以下备料一步都不执行(零 IO、零
    # 行为差异 = 现状);开启时每轮只做一次图谱查询与时钟采样,任何失败
    # 安全降级为 edge_index=None(全部站点沿用库级默认 TTL)。
    edge_index: dict[str, list[float]] | None = None
    base_ttl_h = 0.0
    now_hours = 0.0
    if (
        bool(getattr(cfg, "dynamic_ttl", False))
        and skip_if_unchanged
        and memory is not None
        and items
    ):
        base_ttl_h = float(getattr(memory, "ttl_hours", MEMORY_TTL_HOURS))
        now_hours = _now_hours()
        edge_index = _load_edge_hours(cfg, graph)

    summary: dict[str, Any] = {
        "total": len(items),
        "scanned": 0,
        "skipped": 0,
        "failed": 0,
        "notified": 0,
        "errors": [],
    }
    logger.info(
        "本轮巡查开始:watchlist=%s,启用条目 %d 个", cfg.watchlist_path, len(items)
    )

    for item in items:
        url = item.url

        with telemetry.timer("scheduler.item"):  # 每项处理耗时(含跳过项)
            # 1) 指纹去重:上轮已记住且未过 TTL 的站点直接跳过。
            #    A211 动态 TTL:建议值非 None 时以 ttl_hours 单次覆写跳过档
            #    (仅本次决策生效);None = 库级默认,调用形态与现状一致。
            if skip_if_unchanged and memory is not None:
                fp_last = _last_fingerprint(memory, url)
                if fp_last:
                    ttl_hours = _dynamic_ttl_for(
                        url, edge_index, base_ttl_h, now_hours
                    )
                    try:
                        if ttl_hours is None:
                            need, reason = memory.should_rescan(url, fp_last)
                        else:
                            need, reason = memory.should_rescan(
                                url, fp_last, ttl_hours=ttl_hours
                            )
                    except Exception as exc:  # noqa: BLE001 - 比对失败按必扫处理
                        logger.warning("指纹比对异常,按需要重扫处理:%s %s", url, exc)
                        need, reason = True, ""
                    if not need:
                        summary["skipped"] += 1
                        telemetry.inc("scheduler.skipped")
                        logger.info("跳过(指纹未变且未过 TTL):%s %s", url, reason)
                        continue

            # 2) 扫描:单项失败不中断整轮
            try:
                report = scan(url, cfg)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不中断(安全红线)
                summary["failed"] += 1
                telemetry.inc("scheduler.failed")
                summary["errors"].append((url, str(exc)))
                logger.warning("单项扫描失败,继续下一项:%s %s", url, exc)
                pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter())
                continue

            summary["scanned"] += 1
            telemetry.inc("scheduler.scanned")

            # 3) 先扫后记:指纹来自报告,记住供下一轮去重
            if memory is not None:
                try:
                    fp = str(memory.fingerprint(report) or "")
                except Exception as exc:  # noqa: BLE001 - 算不出指纹就不记忆
                    logger.warning("计算站点指纹失败(本轮不记忆):%s %s", url, exc)
                    fp = ""
                if fp:
                    _remember_fingerprint(memory, url, fp)

            # 4) 需人工复核 → 单发通知(不重试)
            if getattr(report, "needs_review", False):
                verdict_raw = getattr(report, "verdict", "")
                verdict = getattr(verdict_raw, "value", verdict_raw)
                try:
                    agg = float(getattr(report, "agg_nsw_prob", 0.0) or 0.0)
                except (TypeError, ValueError):
                    agg = 0.0
                text = f"{url} verdict={verdict} agg={agg:.2f}"
                try:
                    sent = bool(notify_fn(cfg, "pending_review", text))
                except Exception as exc:  # noqa: BLE001 - 通知异常不拖垮巡查
                    logger.warning("通知发送异常(单发不重试):%s", exc)
                    sent = False
                if sent:
                    summary["notified"] += 1
                    logger.info("已发送待复核提醒:%s", text)
                else:
                    logger.info("待复核提醒未能发出(单发不重试):%s", text)

        # 5) 礼貌抖动:真实触网后的项间停顿(计时之外——停顿不是处理耗时)
        pause(PAUSE_BASE_S + PAUSE_JITTER_S * jitter())

    logger.info("本轮巡查结束:%s", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.ops.scheduler",
        description=(
            "净网哨兵巡查调度器:按 watchlist 顺序扫描"
            "(指纹去重、礼貌抖动、结果通知)"
        ),
    )
    parser.add_argument(
        "--config", default=None, help="配置文件路径(默认 ./config.yaml)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true", help="执行一轮巡查后退出(默认行为)"
    )
    mode.add_argument(
        "--loop", action="store_true", help="循环巡查,轮间隔为 --interval-min 分钟"
    )
    parser.add_argument(
        "--interval-min",
        type=int,
        default=DEFAULT_INTERVAL_MIN,
        help=f"循环模式的轮间隔(分钟,默认 {DEFAULT_INTERVAL_MIN})",
    )
    parser.add_argument(
        "--list", action="store_true", help="仅打印当前 watchlist 条目后退出"
    )
    return parser


def _print_watchlist(items: list[WatchItem], path: str) -> None:
    """中文打印 watchlist 概览(--list 模式)。"""
    if not items:
        print(f"watchlist 为空或未找到,当前无巡查目标(路径:{path})")
        return
    enabled = sum(1 for it in items if it.enabled)
    print(
        f"巡查 watchlist({path})共 {len(items)} 个条目:"
        f"{enabled} 启用 / {len(items) - enabled} 停用"
    )
    for i, it in enumerate(items, 1):
        state = "启用" if it.enabled else "停用"
        note = f"  备注:{it.note}" if it.note else ""
        print(f"  {i}. [{state}] {it.url}{note}")


def _print_summary(summary: dict[str, Any]) -> None:
    """中文打印单轮摘要。"""
    print(
        "本轮巡查完成:总计 {total} 项,已扫描 {scanned},指纹未变跳过 {skipped},"
        "失败 {failed},已通知 {notified}".format(**summary)
    )
    for url, err in summary.get("errors", []):
        print(f"  失败:{url} —— {err}")


def _load_config(path: str | None) -> Config:
    """加载配置(包一层便于测试替换;A01 模块,标准库实现)。"""
    from netsentinel.config import load_config

    return load_config(path)


def _setup_logging(cfg: Config) -> None:
    """初始化全局日志(A01);模块缺失时退回默认配置。"""
    try:
        from netsentinel.logging_util import setup_logging
    except ImportError as exc:  # pragma: no cover - A01 已就位
        logger.warning("日志模块未就位,使用默认日志配置:%s", exc)
        return
    setup_logging(cfg)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:``--list`` / ``--once`` / ``--loop --interval-min``。

    - ``--list``:打印 watchlist 条目(含启用状态与备注)后返回 0;
    - ``--once``(未指定模式时的默认):执行一轮巡查,打印摘要后返回 0
      (单项失败不改变退出码,详见摘要与 errors 列表);
    - ``--loop``:循环执行,轮间隔 ``--interval-min`` 分钟(默认 720);
      Ctrl-C(KeyboardInterrupt)优雅退出并返回 0;
    - 配置或 watchlist 损坏(ValueError)打印中文错误并返回 2。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.interval_min < 1:
        parser.error("--interval-min 必须 >= 1(单位:分钟)")

    try:
        cfg = _load_config(args.config)
        items = load_watchlist(cfg.watchlist_path)
    except ValueError as exc:
        print(f"配置或 watchlist 错误:{exc}")
        return 2

    if args.list:
        _print_watchlist(items, cfg.watchlist_path)
        return 0

    _setup_logging(cfg)
    try:
        if args.loop:
            while True:
                _print_summary(run_once(cfg))
                _default_sleep(args.interval_min * 60)
        else:
            _print_summary(run_once(cfg))
    except KeyboardInterrupt:
        print("收到中断信号,巡查调度器已优雅退出")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
