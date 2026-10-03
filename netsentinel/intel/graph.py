"""站点关联图谱(A46):共享图片 / 模板 / 重定向 → 站点团伙发现。

依据 CONTRACTS-V3.md §3 A46 条目:执法视角下,同一伙违法站点往往复用
同一批图片素材、同一套页面模板,或在站点之间互相导流(重定向)。本模块
把这些"共同痕迹"沉淀为一张本地证据图 ``EvidenceGraph``:

- 节点(nodes)三类:``site``(站点)/ ``image``(图片 sha256)/
  ``template``(页面 simhash);image / template 节点的 meta 记录其
  出现过的站点列表(sites),供团伙归并;
- 边(edges)为站点与站点之间的关联,种类 ``shared_image``(共享图片)/
  ``phash_near``(感知哈希近重复,A204 起经公开 ``add_edge`` 写入)/
  ``shared_template``(共享模板)/ ``redirect``(重定向,A47 追踪器
  经 ``add_redirect`` 写入)/ ``mirror_near``(镜像近重复候选,A244 起
  在席——A229 mirror 源建边的**独立边种类**:镜像规范形丢弃翻转方向
  信息、互为翻转的异图不可分,命中只是候选关联而非判定,故与
  ``phash_near`` 分通道落边,消费侧(:func:`netsentinel.pipeline.
  kernel_wire.resolve_gangs`)按来源降权 / 默认不直接并团);A204 增公开
  通用写口 ``add_edge``(kind 白名单校验,五种边种类全通道)。

红线 12:图谱仅存本地 SQLite、不外发;本模块零网络行为、零第三方依赖,
只用标准库 sqlite3 / json / threading / datetime(pathlib 仅用于建目录
与清理伴生文件)。

线程安全:连接以 ``check_same_thread=False`` 建立,全部读写经同一把
``threading.Lock`` 串行化,可在扫描池 / 调度器多线程中共用;库文件损坏
时告警并删除重建(节点可由重新采集补回、关联边可由重新 link 恢复,
不影响证据本体——安全方向失效)。

权重语义:``link_shared_images`` / ``link_templates`` 对站点对 (X, Y)
取 ``weight = 共享素材数 / 该站点对涉及的素材总数(并集)``,即仅对
共享素材的 Jaccard 相似度;重跑会刷新权重并清掉已不再成立的陈旧边,
返回值恒为"本次新增的边数"(幂等:数据未变时重跑返回 0)。
``mirror_near`` 边权重同为 0~1 的近重复占比(写入方 kernel_wire
mirror 源:该站镜像命中次数 ÷ 本批成功哈希图数),降权与并团把关在
消费侧 resolve_gangs 完成,图本身不区分强弱。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
import threading
from pathlib import Path

from netsentinel import telemetry

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DB_PATH",
    "EDGE_MIRROR_NEAR",
    "EDGE_PHASH_NEAR",
    "EDGE_REDIRECT",
    "EDGE_SHARED_IMAGE",
    "EDGE_SHARED_TEMPLATE",
    "NODE_IMAGE",
    "NODE_SITE",
    "NODE_TEMPLATE",
    "EvidenceGraph",
]

#: 默认库路径(与 contracts.Config.graph_db 的 "data/graph.db" 约定一致)。
DEFAULT_DB_PATH = "data/graph.db"

#: sqlite 连接统一设置:写前等待锁的最长毫秒数(V5,与全仓 sqlite 口径一致)。
_BUSY_TIMEOUT_MS = 5000

# ---- 节点种类(契约:kind ∈ site|image|template)----
NODE_SITE = "site"
NODE_IMAGE = "image"
NODE_TEMPLATE = "template"

# ---- 边种类(契约:kind ∈ shared_image|phash_near|shared_template|
# ----           redirect|mirror_near)----
EDGE_SHARED_IMAGE = "shared_image"
EDGE_PHASH_NEAR = "phash_near"
EDGE_SHARED_TEMPLATE = "shared_template"
EDGE_REDIRECT = "redirect"
#: A244:镜像近重复**候选**边(A229 mirror 源建边此前挂 phash_near,
#: 图上无法区分来源;独立边种类后消费侧可按来源降权 / 不直接并团)。
EDGE_MIRROR_NEAR = "mirror_near"

#: 公开写口 :meth:`EvidenceGraph.add_edge` 的 kind 白名单(与 schema
#: CHECK 同口径;A204 前唯一无公开写通道的边种类是 phash_near,本白名单
#: 补齐;A244 增 mirror_near,五种全通道)。
_EDGE_KINDS = frozenset(
    {
        EDGE_SHARED_IMAGE,
        EDGE_PHASH_NEAR,
        EDGE_SHARED_TEMPLATE,
        EDGE_REDIRECT,
        EDGE_MIRROR_NEAR,
    }
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id   TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('site', 'image', 'template')),
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('shared_image', 'phash_near', 'shared_template',
                               'redirect', 'mirror_near')),
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst);
"""

#: 旧库(四种类 CHECK)edges 表整表重建 DDL(A244 迁移;列与主键与新库
#: :data:`_SCHEMA_SQL` 完全一致,仅表名带 ``_new`` 后缀以便原子换名)。
_EDGES_REBUILD_SQL = """
CREATE TABLE edges_new (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('shared_image', 'phash_near', 'shared_template',
                               'redirect', 'mirror_near')),
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (src, dst, kind)
)
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表(schema 全量)+ 旧库 kind CHECK 迁移(:func:`_migrate_edges_kind_check`)。

    新库由 ``CREATE TABLE IF NOT EXISTS`` 直接建新口径;旧库四种类 CHECK
    无法 ALTER(SQLite 不支持改既有 CHECK),由迁移函数整表重建补齐。
    """
    conn.executescript(_SCHEMA_SQL)
    _migrate_edges_kind_check(conn)


def _migrate_edges_kind_check(conn: sqlite3.Connection) -> None:
    """旧库 edges 表 kind CHECK 约束重建为含 ``mirror_near`` 的新口径(A244)。

    迁移策略(与库内既有探测/迁移惯例同思路):phash.py(A219)与
    decision/review_queue 对**缺列**用 ``PRAGMA table_info`` 探测后
    ``ALTER TABLE ADD COLUMN`` 尾部补建;本处要改的是既有列上的**值域
    CHECK**,SQLite 不支持 ALTER 修改,故沿用同一"先探测、后补建、老数据
    零迁移"的原则,以**整表重建**落地:

    1. 探测:读 ``sqlite_master`` 中 edges 的建表 DDL,不含
       ``mirror_near`` 即判定为旧口径库(新库直接建新口径,DDL 已含,
       本函数空操作返回);
    2. 重建(单事务,原子):建 ``edges_new``(新 CHECK)→ 旧边逐行
       **原样拷贝**(SELECT 保值;新 CHECK 的值域是旧 CHECK 的超集,
       旧数据必然通过——零迁移、零改写)→ DROP 旧表 → 改名 → 重建
       ``idx_edges_src`` / ``idx_edges_dst`` 两索引;
    3. 成功记 ``telemetry.inc("graph.edges_check_migrated")``;失败
       (IO / 意外约束冲突)告警 + ``graph.edges_check_migrate_skipped``
       计数后**不上抛**——旧种类读写完全不受影响,仅 ``mirror_near``
       写入会因旧 CHECK 拒绝而失败(增强项,安全方向失效)。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'edges'"
        ).fetchone()
        ddl = str(row["sql"]) if row is not None and row["sql"] else ""
        if not ddl or "mirror_near" in ddl:
            return  # 新口径(或探测不到 DDL 的异常库):无需迁移
        conn.execute("BEGIN")
        try:
            conn.execute(_EDGES_REBUILD_SQL)
            conn.execute(
                "INSERT INTO edges_new (src, dst, kind, weight, created_at) "
                "SELECT src, dst, kind, weight, created_at FROM edges"
            )
            conn.execute("DROP TABLE edges")
            conn.execute("ALTER TABLE edges_new RENAME TO edges")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        telemetry.inc("graph.edges_check_migrated")
        logger.info(
            "edges 表 kind CHECK 已重建为含 mirror_near 的新口径"
            "(旧边原样保留,零迁移)"
        )
    except Exception:  # noqa: BLE001 - 迁移失败不阻断开库,旧种类照常可用
        telemetry.inc("graph.edges_check_migrate_skipped")
        logger.warning(
            "edges 表 kind CHECK 迁移未完成(mirror_near 写入将不可用,"
            "其余边种类读写不受影响)",
            exc_info=True,
        )

_UPSERT_EDGE_SQL = """
INSERT INTO edges (src, dst, kind, weight, created_at) VALUES (?, ?, ?, ?, ?)
ON CONFLICT(src, dst, kind) DO UPDATE SET weight = excluded.weight
"""


def _site_id(url: str) -> str:
    """站点 URL → 节点 id(加 ``site:`` 前缀隔离三类 id 命名空间)。"""
    return f"site:{url}"


def _site_url(node_id: str) -> str:
    """节点 id → 站点 URL(剥掉 ``site:`` 前缀;容忍无前缀的旧数据)。"""
    if node_id.startswith("site:"):
        return node_id[len("site:") :]
    return node_id


def _load_meta(raw: str | None) -> dict:
    """解析节点 meta JSON;脏数据时退化为空 dict(读口不抛错)。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class EvidenceGraph:
    """基于本地 SQLite 的站点关联证据图。

    用法(典型流)::

        graph = EvidenceGraph(cfg.graph_db)
        graph.add_site("https://a.example.com/")
        graph.add_image(sha256, "https://a.example.com/")   # 自动补建 image 节点
        graph.link_shared_images()                          # 共享图 → 站点边
        graph.related_sites("https://a.example.com/", depth=2)

    - 写口 ``add_site`` / ``add_image`` / ``add_template`` / ``add_redirect`` /
      ``add_edge`` 全部幂等,重复调用不产生重复行;
    - ``link_shared_images`` / ``link_templates`` 由采集方在批次结束后调用,
      把多站点共用的素材折叠成站点间关联边(权重 = 共享数 / 并集数),
      重跑刷新权重并清理陈旧边,返回新增边数;
    - ``related_sites`` 从某站点出发 BFS 展开 ``depth`` 跳,聚合每跳边种类
      (via)并取最大权重;
    - ``export_json`` / ``stats`` 供复核台(A56)与报表消费。

    线程安全:所有数据库访问都持 ``self._lock``;支持 ``with`` 上下文。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        logger.debug("站点关联图谱已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 连接与损坏重建
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """打开(或新建)数据库;文件损坏时删除重建为空库。

        V5:两个路径都启用 ``PRAGMA journal_mode=WAL``(扫描线程写入与
        复核台 / 仪表盘读取并发不阻塞)与 ``busy_timeout=5000``(跨连接
        短暂锁竞争时等待重试,替代立即抛 ``database is locked``)。
        A244:两个路径都经 :func:`_ensure_schema` 建表 + 旧库 edges 表
        kind CHECK 迁移(增补 mirror_near,老边零迁移)。
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            _ensure_schema(conn)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            logger.warning(
                "站点关联图谱库损坏,已删除重建(旧图谱丢弃,可重新采集 + link 恢复):%s",
                self.db_path,
                exc_info=True,
            )
            telemetry.inc("graph.rebuild")
            conn.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(self.db_path + suffix).unlink(missing_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            _ensure_schema(conn)
            conn.commit()
            return conn

    # ------------------------------------------------------------------
    # 时间缝:测试可 monkeypatch _now 推进时钟
    # ------------------------------------------------------------------

    def _now(self) -> _dt.datetime:
        """当前时刻(本地时区,带时区信息;与 contracts.now_iso 同口径)。"""
        return _dt.datetime.now(_dt.timezone.utc).astimezone()

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # 内部小工具(调用方必须已持 self._lock)
    # ------------------------------------------------------------------

    def _ensure_site(self, url: str) -> None:
        """确保 site 节点存在(INSERT OR IGNORE,幂等)。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO nodes (id, kind, meta) VALUES (?, ?, '{}')",
            (_site_id(url), NODE_SITE),
        )

    def _upsert_site_edge(self, url_a: str, url_b: str, kind: str, weight: float) -> bool:
        """在两个站点之间写入一条边(端点排序规范化,自环忽略)。

        返回是否真的执行了写入(自环返回 False)。已存在同 (src,dst,kind)
        的边只刷新权重,created_at 保留首建时间。
        """
        a, b = sorted((_site_id(url_a), _site_id(url_b)))
        if a == b:
            logger.debug("忽略自环边:%s(kind=%s)", url_a, kind)
            return False
        self._conn.execute(_UPSERT_EDGE_SQL, (a, b, kind, float(weight), self._now_iso()))
        return True

    def _add_media_node(self, kind: str, key: str, site_url: str) -> None:
        """记录"某站点出现过某素材"(image / template 共用)。

        自动补建素材节点与站点节点,并把 site_url 并入 meta.sites(去重、
        排序);重复调用同一 (素材, 站点) 不产生任何变化——幂等。
        """
        if not key or not site_url:
            logger.debug("忽略空素材键或空站点:%r / %r(kind=%s)", key, site_url, kind)
            return
        with self._lock:
            self._ensure_site(site_url)
            node_id = f"{kind}:{key}"
            row = self._conn.execute(
                "SELECT meta FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            meta = _load_meta(row["meta"]) if row is not None else {}
            sites = sorted({str(s) for s in meta.get("sites", []) if s} | {site_url})
            if row is None:
                self._conn.execute(
                    "INSERT INTO nodes (id, kind, meta) VALUES (?, ?, ?)",
                    (node_id, kind, json.dumps({"sites": sites}, ensure_ascii=False)),
                )
            elif sites != list(meta.get("sites", [])):
                self._conn.execute(
                    "UPDATE nodes SET meta = ? WHERE id = ?",
                    (json.dumps({"sites": sites}, ensure_ascii=False), node_id),
                )
            self._conn.commit()
        logger.debug("已记录素材:%s:%s @ %s", kind, key[:16], site_url)

    # ------------------------------------------------------------------
    # 写口(全部幂等)
    # ------------------------------------------------------------------

    def add_site(self, url: str) -> None:
        """收录一个站点节点(已存在则不动)。"""
        with self._lock:
            self._ensure_site(url)
            self._conn.commit()
        logger.debug("已收录站点节点:%s", url)

    def add_image(self, sha256: str, site_url: str) -> None:
        """记录图片 sha256 出现在某站点:自动补建 image 节点,meta 记 sites 列表。"""
        self._add_media_node(NODE_IMAGE, sha256, site_url)

    def add_template(self, simhash: str, site_url: str) -> None:
        """记录页面模板 simhash 出现在某站点(同 add_image 语义)。"""
        self._add_media_node(NODE_TEMPLATE, simhash, site_url)

    def add_redirect(self, src_url: str, dst_url: str, weight: float = 1.0) -> None:
        """记录一条站点间重定向边(A47 trace_redirects 每跳调用;幂等)。

        端点站点节点不存在时自动补建;weight 默认 1.0(调用方可按跳数
        或置信度传入 0~1 的值)。
        """
        with self._lock:
            self._ensure_site(src_url)
            self._ensure_site(dst_url)
            self._upsert_site_edge(src_url, dst_url, EDGE_REDIRECT, weight)
            self._conn.commit()
        logger.debug("已记录重定向边:%s -> %s", src_url, dst_url)

    def add_edge(
        self, src: str, dst: str, *, kind: str, weight: float = 1.0
    ) -> bool:
        """在两个站点之间显式写入一条关联边(A204 公开写口;通用边通道)。

        语义(与库内既有惯例逐项对齐):

        - ``kind`` 白名单:仅接受 ``shared_image`` / ``phash_near`` /
          ``shared_template`` / ``redirect`` / ``mirror_near`` 五种契约边
          种类(与 schema CHECK 同口径),非法值抛中文 ``ValueError``
          ——A194 时代 ``phash_near`` 只能借私有 ``_upsert_site_edge``
          探测写入,A204 公开口补齐全部边种类的公开通道;A244 增
          ``mirror_near``(镜像近重复**候选**边,写入方为 kernel_wire
          mirror 源;语义同 ``phash_near`` 的近重复占比权重,但候选性
          弱于确证近重复——消费侧 resolve_gangs 按来源降权 / 默认不
          直接并团,见该处 docstring);
        - 端点为站点 URL:站点节点不存在时自动补建(同 :meth:`add_redirect`),
          端点经 ``_upsert_site_edge`` 排序规范化(``add_edge(A, B)`` 与
          ``add_edge(B, A)`` 落同一条边);
        - **自环(src == dst)拒绝写入**:返回 ``False``、不落边、不抛错
          (与 ``add_redirect`` / ``_upsert_site_edge`` 的"自环忽略"惯例
          一致;端点站点节点仍会被补建);
        - **幂等 = 末次写入刷新权重**:重复写同一 ``(src, dst, kind)``
          只刷新 ``weight``(与 ``_UPSERT_EDGE_SQL`` / ``link_*`` 重跑刷新
          同口径;不累加、不取最大),``created_at`` 保留首建时间;
        - 返回是否真的写入(自环 ``False``,其余 ``True``);
        - 本类查询口(``related_sites`` / ``export_json`` / ``stats``)每次
          都直读 SQLite,无进程内缓存,**写后无需(也无从)失效缓存**——
          新边在紧随其后的查询中立即可见。
        """
        if kind not in _EDGE_KINDS:
            raise ValueError(
                f"边种类无效:{kind!r}(仅支持 {' / '.join(sorted(_EDGE_KINDS))})"
            )
        with self._lock:
            self._ensure_site(src)
            self._ensure_site(dst)
            written = self._upsert_site_edge(src, dst, kind, float(weight))
            self._conn.commit()
        if written:
            logger.debug("已写入关联边:%s -> %s(kind=%s)", src, dst, kind)
        return written

    # ------------------------------------------------------------------
    # 关联发现:把共享素材折叠成站点间边
    # ------------------------------------------------------------------

    def _link_shared_kind(self, node_kind: str, edge_kind: str) -> int:
        """公共实现:同 kind 素材出现在 ≥2 站点 → 两两站点间建边。

        - 权重 = 该站点对共享的该类素材数 / 两站点该类素材并集数;
        - 重跑刷新:已有边 UPSERT 为新权重(保留 created_at),已不再
          共享任何素材的陈旧边被删除;
        - 返回本次**新增**的边数(数据未变时重跑返回 0)。

        V5 性能:全表只读一次(nodes 按 kind 一次取全 + edges 按 kind 一次
        取全),内存集合运算聚合出全部站点对,再以**两次 executemany**
        (upsert 批 + delete 批)落库——替代旧的"每条边一次 execute"逐条
        写;站点对枚举本身是输出规模所必需(Σk² 个对),但每对一次
        SQL 往返的开销被整批摊平。整体耗时记
        ``telemetry.timer("graph.link")``。
        """
        with telemetry.timer("graph.link"), self._lock:
            rows = self._conn.execute(
                "SELECT id, meta FROM nodes WHERE kind = ?", (node_kind,)
            ).fetchall()
            site_ids = {
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM nodes WHERE kind = ?", (NODE_SITE,)
                )
            }
            # 站点 → 其名下该类素材集合(用于算并集分母)
            site_items: dict[str, set[str]] = {}
            # 站点对 → 共享的素材集合(分子,只在 ≥2 站点出现时产生)
            pair_shared: dict[tuple[str, str], set[str]] = {}
            for row in rows:
                sites = sorted(
                    {
                        str(s)
                        for s in _load_meta(row["meta"]).get("sites", [])
                        if s and _site_id(str(s)) in site_ids
                    }
                )
                for site in sites:
                    site_items.setdefault(site, set()).add(row["id"])
                for i in range(len(sites)):
                    for j in range(i + 1, len(sites)):
                        pair_shared.setdefault((sites[i], sites[j]), set()).add(row["id"])

            existing = {
                (row["src"], row["dst"])
                for row in self._conn.execute(
                    "SELECT src, dst FROM edges WHERE kind = ?", (edge_kind,)
                )
            }
            wanted: dict[tuple[str, str], float] = {}
            for (site_a, site_b), shared in pair_shared.items():
                union = site_items.get(site_a, set()) | site_items.get(site_b, set())
                weight = (len(shared) / len(union)) if union else 0.0
                wanted[(_site_id(site_a), _site_id(site_b))] = weight

            now = self._now_iso()
            new_count = 0
            upserts: list[tuple[str, str, str, float, str]] = []
            for (src, dst), weight in sorted(wanted.items()):
                if (src, dst) not in existing:
                    new_count += 1
                upserts.append((src, dst, edge_kind, weight, now))
            deletes = [
                (src, dst, edge_kind)
                for src, dst in sorted(existing - set(wanted))
            ]
            if upserts:
                self._conn.executemany(_UPSERT_EDGE_SQL, upserts)
            if deletes:
                self._conn.executemany(
                    "DELETE FROM edges WHERE src = ? AND dst = ? AND kind = ?",
                    deletes,
                )
            self._conn.commit()
        logger.debug(
            "link(%s) 完成:现存 %d 条边,本次新增 %d 条",
            edge_kind,
            len(wanted),
            new_count,
        )
        return new_count

    def link_shared_images(self) -> int:
        """同一 image(sha256)出现在 ≥2 站点 → 两两站点间 shared_image 边。

        返回新增边数;重跑幂等(返回 0)并刷新权重 / 清理陈旧边。
        """
        return self._link_shared_kind(NODE_IMAGE, EDGE_SHARED_IMAGE)

    def link_templates(self) -> int:
        """同一 template(simhash)出现在 ≥2 站点 → 两两站点间 shared_template 边。"""
        return self._link_shared_kind(NODE_TEMPLATE, EDGE_SHARED_TEMPLATE)

    # ------------------------------------------------------------------
    # 查询口
    # ------------------------------------------------------------------

    def _adjacency(self) -> dict[str, list[tuple[str, str, float]]]:
        """全量边的无向邻接表快照(须持锁调用)。"""
        adj: dict[str, list[tuple[str, str, float]]] = {}
        for row in self._conn.execute("SELECT src, dst, kind, weight FROM edges"):
            weight = float(row["weight"] or 0.0)
            adj.setdefault(row["src"], []).append((row["dst"], row["kind"], weight))
            adj.setdefault(row["dst"], []).append((row["src"], row["kind"], weight))
        return adj

    def related_sites(
        self, url: str, depth: int = 1
    ) -> list[dict[str, object]]:
        """BFS 查询与某站点关联的站点(边视为无向)。

        - ``depth`` 为跳数上限(1=只看直接关联);``depth <= 0`` 或站点
          未收录时返回空列表;结果不含起点自身;
        - 每个结果形如 ``{"site": 站点URL, "via": [边种类…], "weight": 最大权重}``:
          ``via`` 聚合 BFS 展开中抵达该站点的所有边种类(去重排序),
          ``weight`` 取其中最大值(多跳时即路径上最强的一跳);
        - 边种类无过滤,``mirror_near`` 边默认参与展开(A244 谨慎取舍:
          BFS 是"关联可见性"查询而非判定——镜像候选边让翻转站群在
          related_sites 里可见、via 注明 ``mirror_near`` 来源,权照其
          近重复占比;**降权与并团把关在消费侧** resolve_gangs 完成,
          本口不做语义降权);
        - 排序:权重降序,同权重按站点 URL 升序(输出确定)。
        """
        depth = int(depth)
        start_id = _site_id(url)
        if depth <= 0:
            return []
        with self._lock:
            found = self._conn.execute(
                "SELECT 1 FROM nodes WHERE id = ?", (start_id,)
            ).fetchone()
            if found is None:
                return []
            adjacency = self._adjacency()

        best: dict[str, dict[str, object]] = {}
        seen: set[str] = {start_id}
        frontier: list[str] = [start_id]
        for _level in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                for neighbor, kind, weight in adjacency.get(node, ()):
                    if neighbor == start_id:
                        continue
                    entry = best.setdefault(neighbor, {"via": set(), "weight": 0.0})
                    entry["via"].add(kind)  # type: ignore[union-attr]
                    if weight > float(entry["weight"]):  # type: ignore[union-attr]
                        entry["weight"] = weight
                    if neighbor not in seen:
                        seen.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = sorted(next_frontier)

        results: list[dict[str, object]] = [
            {
                "site": _site_url(node_id),
                "via": sorted(entry["via"]),  # type: ignore[arg-type]
                "weight": float(entry["weight"]),  # type: ignore[arg-type]
            }
            for node_id, entry in best.items()
        ]
        results.sort(key=lambda item: (-float(item["weight"]), str(item["site"])))
        return results

    def export_json(self) -> dict[str, list[dict[str, object]]]:
        """整图导出为可 JSON 序列化的 dict(复核台 / 报表消费)。

        返回 ``{"nodes": [{"id","kind","meta"(已解析为 dict)}…],
        "edges": [{"src","dst","kind","weight","created_at"}…]}``,
        两个列表均排序输出(确定性)。
        """
        with self._lock:
            node_rows = self._conn.execute(
                "SELECT id, kind, meta FROM nodes ORDER BY id"
            ).fetchall()
            edge_rows = self._conn.execute(
                "SELECT src, dst, kind, weight, created_at FROM edges "
                "ORDER BY src, dst, kind"
            ).fetchall()
        nodes = [
            {"id": row["id"], "kind": row["kind"], "meta": _load_meta(row["meta"])}
            for row in node_rows
        ]
        edges = [
            {
                "src": row["src"],
                "dst": row["dst"],
                "kind": row["kind"],
                "weight": float(row["weight"]),
                "created_at": row["created_at"],
            }
            for row in edge_rows
        ]
        return {"nodes": nodes, "edges": edges}

    def stats(self) -> dict[str, object]:
        """图谱规模统计:各节点类计数、节点总数、边总数与按边种类计数。"""
        with self._lock:
            node_rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind"
            ).fetchall()
            edge_total = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
            )
            edge_rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM edges GROUP BY kind"
            ).fetchall()
        by_kind = {row["kind"]: int(row["n"]) for row in node_rows}
        return {
            "sites": by_kind.get(NODE_SITE, 0),
            "images": by_kind.get(NODE_IMAGE, 0),
            "templates": by_kind.get(NODE_TEMPLATE, 0),
            "nodes": sum(by_kind.values()),
            "edges": edge_total,
            "edges_by_kind": {row["kind"]: int(row["n"]) for row in edge_rows},
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        """关闭底层连接(容忍重复关闭)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭站点关联图谱连接时出现异常", exc_info=True)

    def __enter__(self) -> EvidenceGraph:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
