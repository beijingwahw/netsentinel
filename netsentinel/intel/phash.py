"""感知哈希(pHash)与跨案件近重复图片库(A43)。

依据 CONTRACTS-V3.md §3 A43 与红线 12:

- ``phash(path)``:Pillow 可用时打开图片 → 灰度 → 缩放到 32x32 →
  手写 2D DCT-II(可分离余弦基,仅用 stdlib ``math``,32x32 规模双重循环
  完全可接受)→ 取左上 8x8 低频块,以"除 DC 外中位数"为阈值得到 64bit,
  输出 16 位小写十六进制;Pillow 缺失时退化为 ``ahash`` 均值哈希路径
  (若无 Pillow 连解码都不可能,则抛出中文 ValueError 提示安装)。
- ``ahash(path)``:8x8 灰度均值阈值哈希,独立可用,同为 16 位 hex。
- ``hamming(a, b)``:十六进制异或 popcount(``int.bit_count``),
  长度不等或非法十六进制 → 中文 ValueError。
- ``PhashRegistry(db_path)``:SQLite 近重复登记库(表 ``hashes``):
  同一批违规图换站重传(重新编码 / 缩放)哈希几乎不变,据此跨案件关联。
  线程安全(单连接 ``check_same_thread=False`` + 全读写共持一把
  ``threading.Lock``),库文件损坏时告警并删除重建(丢的只是历史关联,
  重扫即可补回——安全方向失效)。

**多哈希列扩展(V12 · A219)**:``register`` 增可选 ``mirror_hash`` /
``pyramid_hash`` 字段(镜像不变 / 多尺度指纹,计算见
``netsentinel.vision.phash2``;缺省 ``None`` 完全向后兼容);``find_similar``
增可选 ``hash_kind`` 参数("phash"(缺省,行为不变)/ "mirror" /
"pyramid")——镜像/金字塔列缺失的旧记录回退现有 phash 列比较,多哈希
列任一命中即候选(同 sha256 去重)。schema 演进沿用全仓惯例:
``PRAGMA table_info`` 探测缺列后 ``ALTER TABLE ADD COLUMN ... DEFAULT ''``
(老数据零迁移成本,列版本 :data:`SCHEMA_VERSION`)。

红线 12:哈希库数据仅存本地,绝不外发;近重复比对只对运营者自己采集的
证据进行。本模块无任何网络行为。
"""
from __future__ import annotations

import importlib
import logging
import math
import sqlite3
import statistics
import threading
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import now_iso

__all__ = [
    "DEFAULT_DB_PATH",
    "HASH_KINDS",
    "MAX_FIND_RESULTS",
    "MIRROR_HASH_HEX_LEN",
    "PYRAMID_HASH_HEX_LEN",
    "SCHEMA_VERSION",
    "PhashRegistry",
    "ahash",
    "hamming",
    "phash",
]
logger = logging.getLogger(__name__)

#: 默认库路径(与 Config.phash_db 缺省一致)。
DEFAULT_DB_PATH = "data/phash.db"

#: ``find_similar`` 返回条数上限(全表扫保持;超限时截断并记 warning,
#: 提示调用方收窄 max_distance 或清理哈希库——V5)。
MAX_FIND_RESULTS = 200

#: sqlite 连接统一设置:写前等待锁的最长毫秒数(V5,与全仓 sqlite 口径一致)。
_BUSY_TIMEOUT_MS = 5000

_DCT_SIZE = 32   # DCT 前统一缩放尺寸
_LOW_FREQ = 8    # 只取左上 8x8 低频系数
_HEX_LEN = 16    # 64bit → 16 个十六进制字符
_HEX_DIGITS = frozenset("0123456789abcdef")

#: 预计算余弦基表:``_COS_TABLE[u][x] = cos((2x+1)·u·π / (2N))``,
#: u ∈ [0, 8), x ∈ [0, 32)。2D DCT 可分离,行/列两个方向共用该表。
_COS_TABLE: tuple[tuple[float, ...], ...] = tuple(
    tuple(
        math.cos((2 * x + 1) * u * math.pi / (2 * _DCT_SIZE))
        for x in range(_DCT_SIZE)
    )
    for u in range(_LOW_FREQ)
)


# ---------------------------------------------------------------------------
# Pillow 惰性加载(与 vision/preprocess.py 同款策略)
# ---------------------------------------------------------------------------


def _load_pil():
    """惰性导入 ``PIL.Image``;缺失 / 半初始化一律返回 None。

    先导入父包 ``PIL`` 再导入 ``PIL.Image``:测试以 ``sys.modules["PIL"] = None``
    屏蔽 Pillow 时,若只导 ``PIL.Image`` 会命中缓存中的子模块而漏检,
    先导父包才能可靠感知"不可用"。
    """
    try:
        importlib.import_module("PIL")
        return importlib.import_module("PIL.Image")
    except Exception:  # None 注入 / 未安装 / 损坏的 PIL 均按"未安装"处理
        return None


def _normalize_hex(value: object, what: str) -> str:
    """校验并归一化十六进制哈希串(去空白、转小写);非法抛中文 ValueError。"""
    h = str(value or "").strip().lower()
    if not h or any(ch not in _HEX_DIGITS for ch in h):
        raise ValueError(f"非法{what}(需为非空十六进制字符串):{value!r}")
    return h


# ---------------------------------------------------------------------------
# 像素读取与哈希计算
# ---------------------------------------------------------------------------


def _open_gray_rows(path: Path, size: int) -> list[list[float]]:
    """打开图片 → 灰度("L")→ 缩放到 size×size,返回逐行像素值(0~255)。

    未安装 Pillow、文件损坏 / 非图片内容时抛中文 ValueError。
    """
    pil_image = _load_pil()
    if pil_image is None:
        raise ValueError(
            "未安装 Pillow,无法解码图片计算哈希(pip install Pillow 后启用)"
        )
    try:
        with pil_image.open(path) as im:
            resampling = getattr(pil_image, "Resampling", pil_image)
            gray = im.convert("L").resize((size, size), resampling.LANCZOS)
            # Pillow 12 起 getdata 弃用(14 移除),优先用 get_flattened_data
            getter = getattr(gray, "get_flattened_data", None) or gray.getdata
            data = list(getter())
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法解码图片文件:{path}({exc})") from exc
    return [
        [float(v) for v in data[row * size:(row + 1) * size]]
        for row in range(size)
    ]


def _dct_hash(rows: list[list[float]]) -> str:
    """对 size×size 灰度矩阵做 2D DCT-II,取左上 8x8 低频块生成 64bit hex。

    变换是可分离的,分两趟完成(规模 32×32,双重循环开销可忽略):

    1. 行方向 ``G[y][v] = Σ_x p[y][x]·cos_v[x]``(v < 8);
    2. 列方向 ``F[u][v] = Σ_y G[y][v]·cos_u[y]``(u, v < 8)。

    阈值取 8x8 系数中"除 DC(左上角 [0][0])外 63 个"的中位数:整体亮度
    线性缩放时全部 AC 系数同比例缩放、符号不变,哈希几乎不动——这正是
    近重复识别想要的不变性。位序:flat[0](DC)为最高位,输出 16 位 hex。
    """
    n = _DCT_SIZE
    k = _LOW_FREQ
    g = [
        [sum(px * cx for px, cx in zip(rows[y], _COS_TABLE[v])) for v in range(k)]
        for y in range(n)
    ]
    coeffs = [
        [sum(g[y][v] * cu_y for y, cu_y in enumerate(_COS_TABLE[u])) for v in range(k)]
        for u in range(k)
    ]
    flat = [coeffs[u][v] for u in range(k) for v in range(k)]
    median = statistics.median(flat[1:])  # 除 DC 外的中位阈值
    bits = 0
    for i, value in enumerate(flat):
        if value > median:
            bits |= 1 << (63 - i)
    return f"{bits:016x}"


def phash(path: str) -> str:
    """计算图片感知哈希(pHash),返回 16 位小写十六进制。

    流程:灰度 → 32x32 → 2D DCT(手写余弦基)→ 左上 8x8 除 DC 中位阈值
    → 64bit。对缩放 / 轻微亮度变化 / 重编码保持稳定,适合跨案件近重复
    识别。Pillow 缺失时退化为 ``ahash`` 路径(算法降级,接口不变);
    文件不存在 / 损坏抛中文 ValueError。计算耗时记
    ``telemetry.timer("phash.compute")``(V5)。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    if _load_pil() is None:
        logger.info(
            "未安装 Pillow,phash 退化为 aHash 均值哈希(pip install Pillow 启用 DCT 路径)"
        )
        return ahash(str(src))
    with telemetry.timer("phash.compute"):
        return _dct_hash(_open_gray_rows(src, _DCT_SIZE))


def ahash(path: str) -> str:
    """计算图片均值哈希(aHash),返回 16 位小写十六进制;独立可用。

    8x8 灰度、以 64 像素均值为阈值。比 pHash 简陋但无任何额外依赖路径,
    也用于 Pillow 存在但调用方只需要粗粒度哈希的场景。文件不存在 / 损坏 /
    未安装 Pillow 抛中文 ValueError。计算耗时同样记
    ``telemetry.timer("phash.compute")``(V5)。
    """
    src = Path(path)
    if not src.is_file():
        raise ValueError(f"图片文件不存在:{src}")
    with telemetry.timer("phash.compute"):
        rows = _open_gray_rows(src, 8)
        flat = [value for row in rows for value in row]
        mean = sum(flat) / len(flat)
        bits = 0
        for i, value in enumerate(flat):
            if value > mean:
                bits |= 1 << (63 - i)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """两个十六进制哈希的汉明距离(异或后置位计数)。

    长度不等或内容非法十六进制 → 中文 ValueError;大小写与首尾空白容忍。
    """
    ha = _normalize_hex(a, "哈希 a")
    hb = _normalize_hex(b, "哈希 b")
    if len(ha) != len(hb):
        raise ValueError(
            f"哈希长度不一致,无法比较:{len(ha)} 位与 {len(hb)} 位"
        )
    return (int(ha, 16) ^ int(hb, 16)).bit_count()


# ---------------------------------------------------------------------------
# 近重复登记库
# ---------------------------------------------------------------------------

#: hashes 表 schema 版本(A219:v2 增 mirror_hash / pyramid_hash 两列;
#: 演进方式 = 探测缺列后 ALTER 补建,不做破坏性迁移)。
SCHEMA_VERSION = 2

#: mirror_hash 位宽(64bit → 16 hex,与 vision.phash2.mirror_hash 一致)。
MIRROR_HASH_HEX_LEN = 16

#: pyramid_hash 位宽(108bit → 27 hex,与 vision.phash2.pyramid_hash 一致)。
PYRAMID_HASH_HEX_LEN = 27

#: find_similar 支持的哈希种类("phash" 为缺省旧行为)。
HASH_KINDS: tuple[str, ...] = ("phash", "mirror", "pyramid")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hashes (
    sha256      TEXT PRIMARY KEY,
    phash       TEXT NOT NULL DEFAULT '',
    site_url    TEXT NOT NULL DEFAULT '',
    verdict_tag TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    mirror_hash   TEXT NOT NULL DEFAULT '',
    pyramid_hash  TEXT NOT NULL DEFAULT ''
)
"""

#: 旧库(v1:无 mirror_hash / pyramid_hash 列)补建 DDL——仅当
#: ``PRAGMA table_info`` 探测到缺列时执行(与 decision.review_queue 的
#: priority_weight 列同款惯例;ADD COLUMN 尾部追加,老数据补空串)。
_ADD_COLUMN_SQL: tuple[str, ...] = (
    "ALTER TABLE hashes ADD COLUMN mirror_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE hashes ADD COLUMN pyramid_hash TEXT NOT NULL DEFAULT ''",
)


def _normalize_len_hex(value: object, what: str, length: int) -> str:
    """校验并归一化定长十六进制哈希;``None``/空串归一为 ``''``(未登记)。

    非空但非法(非十六进制 / 长度不符)抛中文 ValueError——长度口径:
    mirror 16 hex(64bit)、pyramid 27 hex(108bit)。
    """
    if value is None:
        return ""
    h = str(value).strip().lower()
    if not h:
        return ""
    if any(ch not in _HEX_DIGITS for ch in h) or len(h) != length:
        raise ValueError(
            f"非法{what}(需为 {length} 位十六进制字符串):{value!r}"
        )
    return h


def _pyramid_distance_hex(a: str, b: str) -> int:
    """两个 108bit 金字塔哈希的查询距离:三层(每层 9 hex)汉明取最小。

    与 ``netsentinel.vision.phash2.pyramid_distance`` 口径**完全一致**的
    本地实现——不直接 import 是因为主干分层契约(vision 层在 intel 层
    之上,intel 不得上溯依赖),两处实现由
    ``tests/test_phash_invariant.py`` 的一致性用例互相锁定。
    """
    ha = str(a).strip().lower()
    hb = str(b).strip().lower()
    seg = PYRAMID_HASH_HEX_LEN // 3  # 每层 9 hex
    return min(
        hamming(ha[i * seg:(i + 1) * seg], hb[i * seg:(i + 1) * seg])
        for i in range(3)
    )


class PhashRegistry:
    """跨案件近重复图片哈希库(SQLite,本地单文件,红线 12:不外发)。

    - ``register(sha256, phash_hex, site_url, verdict_tag="", *,
      mirror_hash=None, pyramid_hash=None)``:UPSERT——同一 sha 重复登记
      时覆盖 phash / 站点 / 标签 / 时间与两个可选哈希列(A219);可选列
      缺省 ``None`` = 不登记该指纹,行为与旧调用方零差异;
    - ``find_similar(phash_hex, max_distance=8, *, exclude_sha256="",
      hash_kind="phash")``:全表扫 + 距离过滤,``hash_kind`` 选择比对列
      ——"phash"(缺省,旧行为逐字节不变)/ "mirror"(64bit 镜像规范
      形,先比 mirror_hash 列,列缺失回退 phash 列,两列任一命中即候选)
      / "pyramid"(108bit 三层,用三层取 min 的金字塔距离,列缺失同样
      回退;27 hex 查询对 16 hex 旧记录长度不可比,按现有"脏记录跳过"
      语义处理);结果按距离升序(并列按 sha256 稳定排序),同 sha256
      只产出一条(多列命中取最小距离,即"任一命中即候选 + 去重");
      ``exclude_sha256`` 用于排除"当前这张图自身"的记录;
    - ``stats()``:``{"total": 记录数, "sites": 去重站点数}``;
    - ``close()``:关闭连接(幂等),支持 ``with`` 上下文。

    线程安全:连接以 ``check_same_thread=False`` 建立且所有读写都经
    ``self._lock`` 串行化,可在扫描 / 复核线程间共用一个实例。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        logger.debug("感知哈希库已就绪:%s", self.db_path)

    # ------------------------------------------------------------------
    # 连接与损坏重建
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """建表 + 旧库补列(v1 → v2:mirror_hash / pyramid_hash 尾部追加)。

        与 decision.review_queue 的 priority_weight 列同款惯例:先
        ``CREATE TABLE IF NOT EXISTS``(新库直接建 v2 全列),再以
        ``PRAGMA table_info`` 探测缺列、``ALTER TABLE ADD COLUMN ... DEFAULT ''``
        补建——老数据零迁移成本,新列读出为空串(未登记该指纹)。
        """
        conn.execute(_SCHEMA_SQL)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(hashes)")
        }
        for ddl in _ADD_COLUMN_SQL:
            # ALTER 语句里的列名即要补建的列名(第 6 个词,ALTER TABLE <表> ADD COLUMN <列>)。
            column = ddl.split()[5]
            if column not in columns:
                conn.execute(ddl)

    def _open(self) -> sqlite3.Connection:
        """打开(或新建)数据库;文件损坏时删除重建为空库。

        V5:两个路径都启用 ``PRAGMA journal_mode=WAL``(读写不互相阻塞,
        扫描线程登记与复核线程查询可并发)与 ``busy_timeout=5000``
        (跨连接短暂锁竞争时等待重试,替代立即抛 ``database is locked``)。
        A219:两个路径都经 :meth:`_ensure_schema` 建表 + v1 旧库补列。
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema(conn)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            logger.warning(
                "感知哈希库损坏,已删除重建(旧关联记录丢失,重扫即可补回):%s",
                self.db_path,
                exc_info=True,
            )
            telemetry.inc("phash.rebuild")
            conn.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(self.db_path + suffix).unlink(missing_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema(conn)
            conn.commit()
            return conn

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def register(
        self,
        sha256: str,
        phash_hex: str,
        site_url: str,
        verdict_tag: str = "",
        *,
        mirror_hash: str | None = None,
        pyramid_hash: str | None = None,
    ) -> None:
        """登记一张图片的哈希(UPSERT:同 sha256 再次登记即覆盖)。

        A219 可选字段(仅关键字传参,缺省 ``None`` = 该列置空,完全向后
        兼容):``mirror_hash``(16 hex,64bit 镜像不变规范形)、
        ``pyramid_hash``(27 hex,108bit 多尺度三层)——均由
        ``netsentinel.vision.phash2`` 的同名函数产出;非法(非十六进制 /
        长度不符)抛中文 ValueError。注意:再次登记不传可选字段会把对应
        列**覆盖回空**(UPSERT 全列覆盖语义,与 phash 列一致)。
        """
        sha = str(sha256 or "").strip().lower()
        if not sha:
            raise ValueError("sha256 不能为空")
        h = _normalize_hex(phash_hex, "phash")
        m = _normalize_len_hex(mirror_hash, "mirror_hash", MIRROR_HASH_HEX_LEN)
        p = _normalize_len_hex(pyramid_hash, "pyramid_hash", PYRAMID_HASH_HEX_LEN)
        with self._lock:
            self._conn.execute(
                "INSERT INTO hashes (sha256, phash, site_url, verdict_tag, "
                "created_at, mirror_hash, pyramid_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sha256) DO UPDATE SET "
                "phash = excluded.phash, site_url = excluded.site_url, "
                "verdict_tag = excluded.verdict_tag, "
                "created_at = excluded.created_at, "
                "mirror_hash = excluded.mirror_hash, "
                "pyramid_hash = excluded.pyramid_hash",
                (sha, h, str(site_url), str(verdict_tag), now_iso(), m, p),
            )
            self._conn.commit()
        logger.debug("已登记图片哈希:sha=%s… site=%s", sha[:12], site_url)

    def find_similar(
        self,
        phash_hex: str,
        max_distance: int = 8,
        *,
        exclude_sha256: str = "",
        hash_kind: str = "phash",
    ) -> list[dict[str, Any]]:
        """查找与查询哈希距离 ≤ max_distance 的登记记录。

        返回 ``[{"sha256", "site", "distance"}]``,距离升序(并列按 sha256
        排序保证稳定);``exclude_sha256`` 排除同 sha256 记录——典型用法:
        对新图先 ``register`` 再用其 sha 排除自身,查"以前在别的站点是否
        见过近重复"。库中无法比较(非法 / 长度不匹配)的记录跳过并记
        debug 日志与 ``telemetry.inc("phash.dirty_hash")``,不让脏数据中断
        查询。

        A219 ``hash_kind``(缺省 "phash" = 旧行为逐字节不变):

        - ``"mirror"``:查询须为 16 hex 镜像规范形;优先比 ``mirror_hash``
          列,**列缺失(空串,旧记录)回退现有行为**——用 ``phash`` 列按
          汉明比较(16 vs 16 可比);两列都登记时**任一命中即候选**,距
          离取命中列最小值(同 sha256 去重);
        - ``"pyramid"``:查询须为 27 hex;用**三层取 min** 的金字塔距离
          (与 ``vision.phash2.pyramid_distance`` 同口径,本地实现)比
          ``pyramid_hash`` 列,列缺失回退 ``phash`` 列(27 vs 16 长度不可
          比 → 按现有"脏记录跳过"语义处理);建议 ``max_distance`` 传
          ``vision.phash2.PYRAMID_SUGGESTED_MAX_DISTANCE``(11),不要沿用
          64bit 习惯值。

        V5:全表扫保持不变(本地哈希库规模小,距离无法走 SQL 索引);
        结果条数超过 :data:`MAX_FIND_RESULTS`(默认 200)时截断到上限并记
        warning 提示;每次查询命中数累加 ``telemetry.inc("phash.registry_hit",
        len(results))``(只存数字,红线 17)。
        """
        if hash_kind not in HASH_KINDS:
            raise ValueError(
                f"hash_kind 取值非法:{hash_kind!r}(允许:{list(HASH_KINDS)})"
            )
        query = _normalize_hex(phash_hex, "查询 phash")
        expected_len = {
            "phash": 0,
            "mirror": MIRROR_HASH_HEX_LEN,
            "pyramid": PYRAMID_HASH_HEX_LEN,
        }[hash_kind]
        if expected_len and len(query) != expected_len:
            raise ValueError(
                f"{hash_kind} 查询须为 {expected_len} 位十六进制哈希,"
                f"实际收到 {len(query)} 位:{phash_hex!r}"
            )
        compare = _pyramid_distance_hex if hash_kind == "pyramid" else hamming
        # 比对列:主列(kind 对应列)+ 回退列 phash;kind="phash" 只看 phash
        # (旧行为零变化)。主列为空串 = 该记录未登记此指纹 → 回退。
        primary_column = {
            "phash": "phash",
            "mirror": "mirror_hash",
            "pyramid": "pyramid_hash",
        }[hash_kind]
        columns = ("phash",) if hash_kind == "phash" else (primary_column, "phash")
        exclude = str(exclude_sha256 or "").strip().lower()
        with self._lock:
            rows = self._conn.execute(
                "SELECT sha256, phash, site_url, mirror_hash, pyramid_hash FROM hashes"
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            sha = str(row["sha256"])
            if exclude and sha == exclude:
                continue
            distances: list[int] = []
            dirty = False
            for column in columns:
                value = str(row[column] or "").strip().lower()
                if not value:
                    if column == "phash":
                        dirty = True  # phash 列为空 = 脏记录(与旧口径一致)
                    continue  # kind 主列为空 = 未登记该指纹 → 回退 phash 列
                try:
                    distances.append(compare(query, value))
                except ValueError:
                    dirty = True  # 长度不匹配 / 非法十六进制:按脏哈希口径处理
            if not distances:
                if dirty:
                    logger.debug("跳过库中无法比较的哈希记录:sha=%s kind=%s", sha, hash_kind)
                    telemetry.inc("phash.dirty_hash")
                continue
            distance = min(distances)
            if distance <= max_distance:
                results.append(
                    {
                        "sha256": sha,
                        "site": str(row["site_url"]),
                        "distance": distance,
                    }
                )
        results.sort(key=lambda item: (item["distance"], item["sha256"]))
        if len(results) > MAX_FIND_RESULTS:
            logger.warning(
                "find_similar 命中 %d 条,超过上限 %d 已截断;建议收窄 "
                "max_distance 或清理哈希库", len(results), MAX_FIND_RESULTS,
            )
            del results[MAX_FIND_RESULTS:]
        if results:
            telemetry.inc("phash.registry_hit", len(results))
        return results

    # ------------------------------------------------------------------
    # 统计与生命周期
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """库统计:``{"total": 登记哈希数, "sites": 去重站点数}``。"""
        with self._lock:
            total = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM hashes").fetchone()["n"]
            )
            sites = int(
                self._conn.execute(
                    "SELECT COUNT(DISTINCT site_url) AS n FROM hashes "
                    "WHERE site_url != ''"
                ).fetchone()["n"]
            )
        return {"total": total, "sites": sites}

    def close(self) -> None:
        """关闭底层连接(幂等容忍)。"""
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭异常无需上抛
            logger.debug("关闭感知哈希库连接时出现异常", exc_info=True)

    def __enter__(self) -> PhashRegistry:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
