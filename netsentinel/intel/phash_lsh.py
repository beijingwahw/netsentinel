"""检索内核·分带 LSH(64bit 感知指纹的亚线性近邻索引,A127)。

依据 CONTRACTS-V7.md §2 A127 与红线 29/31:

- **只新增文件**:不修改 ``intel/phash.py`` 及任何既有模块;``PhashRegistry``
  经惰性导入只读互操作(``build_from``),零 API 破坏(红线 29)。
- **基准可复现**(红线 31):``LSHIndex.compare_calls`` 累计计数每次查询
  实际执行的汉明比较次数,是 "LSH vs 全表扫描" 的操作计数依据;
  ``kernel_selfcheck()`` 供 A138 基准总控离线调用。
- 本模块无任何网络行为(沿用红线 12:哈希数据仅本地)。

动机:``PhashRegistry.find_similar`` 对汉明距离只能全表扫描(本地库小,
可接受);当跨案件哈希积累到 10^3~10^5 量级时,扫描代价线性增长。
分带 LSH(band-based LSH / "分桶法")把 64bit 指纹切成若干**带**,任一带
桶键相同即成为候选,再对候选做精确汉明过滤——将近邻检索从 O(N) 次比较
降到 O(候选数) 次,且在 ``d ≤ bands-1`` 距离内**保证不漏**(每个翻转位
至多破坏一个带;距离 d 的近邻至多破坏 d 个带,只要 d < bands 就必有
一带完好 → 必为候选)。

碰撞率(默认 4 带 × 16bit 桶键):两条**随机无关**指纹至少一个带落入
同桶的概率 ≈ ``4 / 2^16 ≈ 6.1e-5``(每带 16bit 全同的概率 2^-16,4 带
联合上界相加)。据此:

- 10^3 条随机指纹索引上,单次查询期望候选数 ≈ ``10^3 × 4/2^16 ≈ 0.06``,
  即几乎总是 0~1 次汉明比较(全表扫描需 10^3 次)——约 4 个数量级的代差;
- 代价是 ``d ≥ bands`` 的近邻可能漏检(每带恰好各翻一个位时四个带全毁),
  属 LSH 方法论的固有取舍:召回优先场景调大 ``bands``(如 8 带 8bit,
  随机同桶概率 ≈ ``8/2^8 ≈ 3.1%``,d ≤ 7 保证召回,候选数相应增多)。

对应配置项:``Config.phash_lsh_bands``(默认 4,约束 1~8);本类自身
接受 1~64(便于离线实验),由装配线(A139)负责把配置传入。

A195 增量:``MultiTableLSH`` 多表 multi-probe LSH(对标 Lv et al. 2007
multi-probe LSH / 10^6 级 ANN 索引标准形态)。单表分带的精确桶匹配在
d ≥ bands 时召回断崖(每带各翻一位即全毁,d=16 时召回 ≈ 0);多表方案
以 K 张独立随机置换表 + 桶键邻域探针把高距区的召回从 0 拉回 99%+,
同时保持亚线性比较次数(操作计数见 ``compare_calls`` / ``probe_calls``)。
召回下界论证与 sqlite 持久化协议详见该类 docstring;旧 ``LSHIndex``
一字不动,两类并存。

A229 泛化(多哈希生产接线,V14 §2 指纹纵深第 4 项):``MultiTableLSH``
增 ``name`` 实例标识参数(缺省 ``""``,旧构造签名 / 行为完全兼容)——
多哈希接线后主 phash 与 mirror 各持一个实例(``name="mirror"``)并存,
name 只用于 repr 与运维日志定位,**不进**参数指纹、不影响桶布局与
持久化格式;实例间数据隔离由各自独立的 ``db_path`` 承担(按
``<phash_db>.mtlsh`` / ``<phash_db>.mtlsh.mirror`` 惯例派生,见
kernel_wire 的 ``_mt_lsh_db_path`` / ``_mirror_lsh_db_path``)。
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
from itertools import combinations
from math import comb
from pathlib import Path
from typing import Any

from netsentinel import telemetry

__all__ = [
    "BITS",
    "DEFAULT_BANDS",
    "DEFAULT_KEY_BITS",
    "DEFAULT_PROBE_DEPTH",
    "DEFAULT_TABLES",
    "LSHIndex",
    "MT_MAX_PROBE_KEYS",
    "MAX_TABLES",
    "MultiTableLSH",
    "kernel_selfcheck",
    "multitable_selfcheck",
]

logger = logging.getLogger(__name__)

#: 指纹位宽(64bit pHash,16 位十六进制)。
BITS = 64

#: 默认分带数(与 ``Config.phash_lsh_bands`` 缺省一致:4 带 × 16bit)。
DEFAULT_BANDS = 4

#: 读 PhashRegistry 时的跨连接锁等待毫秒数(与全仓 sqlite 口径一致)。
_BUSY_TIMEOUT_MS = 5000

_HEX_DIGITS = frozenset("0123456789abcdef")


def _normalize_hex64(value: object, what: str) -> str:
    """校验并归一化 64bit 指纹(去空白、转小写);非法抛中文 ValueError。

    与 ``intel/phash.py`` 的 ``_normalize_hex`` 同款语义,但额外要求长度
    恰为 16 位十六进制(64bit):分带切片必须固定位宽,长度不明的哈希
    无法确定带边界。
    """
    h = str(value if value is not None else "").strip().lower()
    if not h or any(ch not in _HEX_DIGITS for ch in h):
        raise ValueError(f"非法{what}(需为十六进制字符):{value!r}")
    if len(h) != 16:
        raise ValueError(
            f"{what}长度无效:64bit 指纹需 16 位十六进制,得到 {len(h)} 位"
        )
    return h


def _band_layout(bands: int) -> tuple[tuple[int, int], ...]:
    """把 64bit 均分为 ``bands`` 个带,返回每带的 ``(shift, width)``。

    带 0 覆盖最低 ``width`` 位,依次向高位推进;64 不能整除时前
    ``64 % bands`` 个带多分 1 位,保证 **64 位全部被覆盖**(不丢信息)。
    """
    base, extra = divmod(BITS, bands)
    layout: list[tuple[int, int]] = []
    shift = 0
    for band in range(bands):
        width = base + (1 if band < extra else 0)
        layout.append((shift, width))
        shift += width
    return tuple(layout)


class LSHIndex:
    """64bit 感知指纹分带 LSH 内存索引(近邻检索内核,A127)。

    用法::

        index = LSHIndex(bands=4)          # 4 带 × 16bit 桶键
        index.insert("0f1e2d3c4b5a6978", {"case": 1})
        hits = index.query("0f1e2d3c4b5a6979", max_distance=8)

    - ``insert(hex64, payload)``:登记一条指纹与任意负载(不去重,同指纹
      可多条记录,各自独立参与查询);
    - ``query(hex64, max_distance=8)``:任一带同桶 → 候选,先按条目去重,
      再做精确汉明过滤(≤ max_distance),按 (距离, 插入序) 升序返回负载;
    - ``build_from(registry)``:惰性导入并遍历 ``PhashRegistry`` 全表登记
      (只读兄弟库,不清空本索引已有内容,可多次调用合并多个库;库中
      非 64bit 的脏哈希行跳过不中断,与 ``find_similar`` 口径一致);
    - ``stats()``:``{"entries", "buckets", "avg_bucket"}``;
    - ``compare_calls``:累计汉明比较计数器(bench 依据,红线 31)。

    **碰撞率与取舍**(默认 4 带 16bit):随机无关指纹同桶概率 ≈
    ``4/2^16 ≈ 6.1e-5``,即 10^3 条索引上单查询期望候选 ≈ 0.06 个
    (全表扫描要比较 10^3 次);代价是 ``d ≥ bands`` 的近邻可能漏检
    (每带恰好各翻一位时全带皆毁),``d ≤ bands-1`` 内保证 100% 召回。
    需要更高召回上限时加大 ``bands``(如 8 带 8bit:d ≤ 7 保证召回,
    随机同桶概率升到 ≈ ``8/2^8 ≈ 3.1%``,候选与比较次数相应增多)。
    ``bands=1`` 退化为 64bit 精确匹配(仅同指纹命中,零碰撞也零容错)。

    汉明比较直接用 ``(a ^ b).bit_count()``(与 ``phash.hamming`` 对两个
    已归一化的 64bit hex 完全等价),避免候选循环里的字符串重校验开销。

    线程安全:全部读写经一把 ``threading.RLock`` 串行化,可在扫描 /
    复核线程间共用一个实例(``build_from`` 内部复用 ``insert``,
    可重入锁保证嵌套安全)。
    """

    def __init__(self, bands: int = DEFAULT_BANDS) -> None:
        if not isinstance(bands, int) or isinstance(bands, bool) \
                or bands < 1 or bands > BITS:
            raise ValueError(f"bands 无效:须为 1~{BITS} 的整数,得到 {bands!r}")
        self.bands = int(bands)
        self._layout = _band_layout(self.bands)
        #: 每带一个 {桶键: [条目下标, ...]};条目下标即插入序。
        self._buckets: list[dict[int, list[int]]] = [
            {} for _ in range(self.bands)
        ]
        #: (归一化 hex, int 值, payload) 三元组,按下标即插入序存放。
        self._entries: list[tuple[str, int, Any]] = []
        #: 查询累计汉明比较次数(操作计数 bench 依据,红线 31)。
        self.compare_calls = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 内部:桶键
    # ------------------------------------------------------------------

    def _band_keys(self, value: int) -> tuple[int, ...]:
        """整数指纹值 → 各带桶键(int 切片:右移 + 掩码)。"""
        return tuple(
            (value >> shift) & ((1 << width) - 1)
            for shift, width in self._layout
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert(self, hex64: str, payload: Any) -> None:
        """登记一条 64bit 指纹与任意负载;非法哈希抛中文 ValueError。

        同指纹重复插入不去重(各自独立命中);带键冲突(不同指纹某带
        同桶)是 LSH 的正常现象,由查询阶段的汉明过滤兜底。
        """
        h = _normalize_hex64(hex64, "指纹 hex64")
        value = int(h, 16)
        with self._lock:
            seq = len(self._entries)
            self._entries.append((h, value, payload))
            keys = self._band_keys(value)
            for band, key in enumerate(keys):
                self._buckets[band].setdefault(key, []).append(seq)

    def build_from(self, registry: Any) -> int:
        """遍历 ``PhashRegistry`` 全表登记进本索引,返回成功登记条数。

        惰性导入兄弟模块(红线:兄弟只读、不反向依赖);以独立只读
        sqlite 连接读 ``registry.db_path`` 的 ``hashes`` 全表,不触碰 /
        不修改原库。负载为 ``{"sha256", "site", "verdict_tag"}`` 字典,
        与 ``find_similar`` 的键口径对齐(便于上层无差别消费)。

        非 64bit 的脏哈希行(长度不匹配 / 非法字符)跳过不中断,记
        debug 日志与 ``telemetry.inc("phash.lsh_dirty_hash")``;不清空
        本索引既有内容,可多次调用合并多个库。
        """
        from netsentinel.intel.phash import PhashRegistry  # 惰性导入(只读)

        if not isinstance(registry, PhashRegistry):
            raise TypeError(
                f"build_from 需要 PhashRegistry 实例,得到 "
                f"{type(registry).__name__}"
            )
        conn = sqlite3.connect(registry.db_path)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            rows = conn.execute(
                "SELECT sha256, phash, site_url, verdict_tag FROM hashes"
            ).fetchall()
        finally:
            conn.close()

        added = 0
        skipped = 0
        for sha, phash_hex, site_url, verdict_tag in rows:
            try:
                hex64 = _normalize_hex64(phash_hex, "库内 phash")
            except ValueError:
                skipped += 1
                logger.debug("跳过库中非 64bit 哈希记录:sha=%s", sha)
                continue
            self.insert(hex64, {
                "sha256": str(sha),
                "site": str(site_url),
                "verdict_tag": str(verdict_tag),
            })
            added += 1
        if skipped:
            telemetry.inc("phash.lsh_dirty_hash", skipped)
        telemetry.inc("phash.lsh_build", added)
        logger.debug(
            "LSH 索引自 PhashRegistry 装载完成:登记 %d 条,跳过脏行 %d 条",
            added, skipped,
        )
        return added

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(self, hex64: str, max_distance: int = 8) -> list[Any]:
        """查找与查询指纹汉明距离 ≤ max_distance 的全部负载。

        流程:计算各带桶键 → 任一带同桶的条目并为候选集(**按条目去重**,
        多带命中只比一次)→ 对每个候选做一次精确汉明比较(计入
        ``self.compare_calls``)→ 过滤 ≤ max_distance → 按 (距离, 插入序)
        升序返回负载。非法哈希 / max_distance 越界抛中文 ValueError。

        遥测:每次查询 ``phash.lsh_query`` +1,比较次数累加
        ``phash.lsh_compare``(只存数字,红线 17)。
        """
        h = _normalize_hex64(hex64, "查询 hex64")
        if not isinstance(max_distance, int) or isinstance(max_distance, bool) \
                or max_distance < 0 or max_distance > BITS:
            raise ValueError(
                f"max_distance 无效:须为 0~{BITS} 的整数,得到 {max_distance!r}"
            )
        value = int(h, 16)
        with self._lock:
            candidates: set[int] = set()
            for band, key in enumerate(self._band_keys(value)):
                bucket = self._buckets[band].get(key)
                if bucket:
                    candidates.update(bucket)
            hits: list[tuple[int, int, Any]] = []
            for seq in sorted(candidates):
                _, entry_value, payload = self._entries[seq]
                distance = (value ^ entry_value).bit_count()
                self.compare_calls += 1
                if distance <= max_distance:
                    hits.append((distance, seq, payload))
            hits.sort(key=lambda item: (item[0], item[1]))
            telemetry.inc("phash.lsh_query")
            telemetry.inc("phash.lsh_compare", len(candidates))
            return [payload for _, _, payload in hits]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """索引统计:条目数 / 非空桶数 / 平均每桶条目数。

        ``buckets`` 为各带非空桶键总数之和(每条目在每带各占一桶位,
        故桶位数 = entries × bands);``avg_bucket`` = 桶位总数 ÷ 非空桶数,
        空索引为 0.0。该值即"随机查询落入某桶时平均要比较多少条"的
        直观刻度,用于调带数权衡。
        """
        with self._lock:
            entries = len(self._entries)
            buckets = sum(len(band) for band in self._buckets)
            placements = sum(
                len(members)
                for band in self._buckets
                for members in band.values()
            )
            avg_bucket = placements / buckets if buckets else 0.0
            return {
                "entries": entries,
                "buckets": buckets,
                "avg_bucket": avg_bucket,
            }


# ---------------------------------------------------------------------------
# 离线自检(A138 基准总控调用;红线 31:操作计数,不依赖墙钟)
# ---------------------------------------------------------------------------


def kernel_selfcheck() -> dict[str, Any]:
    """离线可复现微基准:1000 条随机指纹索引上单查询的比较比例。

    构造 1000 条随机 64bit 指纹(固定种子,确定性),以表中第 0 条为
    查询,断言 LSH 命中集合与全表扫描完全一致(该距离域内保证召回),
    返回 ``{"name", "metric", "value", "baseline"}``:value = LSH 实际
    汉明比较次数 ÷ 全表扫描比较次数,baseline = 1.0(全表扫描)。
    """
    rng = random.Random(20261002)
    index = LSHIndex(bands=DEFAULT_BANDS)
    entries: list[tuple[str, int]] = []
    for seq in range(1000):
        hex64 = f"{rng.getrandbits(BITS):016x}"
        index.insert(hex64, seq)
        entries.append((hex64, seq))

    probe_hex, probe_id = entries[0]
    probe_value = int(probe_hex, 16)
    before = index.compare_calls
    hits = index.query(probe_hex, max_distance=8)
    used = index.compare_calls - before

    brute_ids = sorted(
        seq
        for hex64, seq in entries
        if (probe_value ^ int(hex64, 16)).bit_count() <= 8
    )
    assert sorted(hits) == brute_ids, "LSH 命中必须与全表扫描一致(自查)"
    ratio = used / len(entries)
    assert ratio <= 0.1, f"LSH 比较比例 {ratio:.4f} 超过全表 10%(自查)"
    return {
        "name": "phash_lsh",
        "metric": "query_compare_ratio_vs_full_scan",
        "value": round(ratio, 6),
        "baseline": 1.0,
    }


# ---------------------------------------------------------------------------
# 多表 multi-probe LSH(A195,与上方单表 LSHIndex 并存、互不影响)
# ---------------------------------------------------------------------------

#: 多表默认表数 K(每表一张独立的 64bit 位置换)。
DEFAULT_TABLES = 8

#: 多表默认桶键位宽 B(每表取置换后前 B 位作桶键)。
DEFAULT_KEY_BITS = 16

#: 默认探针深度 p(原桶 + 桶键翻转 1~p 位的一切邻桶;p=0 即关闭探针)。
DEFAULT_PROBE_DEPTH = 2

#: 多表置换缺省种子(确定性:同种子 → 同 K 张置换 → 同桶布局)。
DEFAULT_MT_SEED = 20261003

#: 表数上限(防误配爆内存;1024 张已覆盖 10^6 级索引的实用域)。
MAX_TABLES = 1024

#: 单次查询的探针组合数上限(Σ C(B,i), i ≤ p;防大 B 大 p 误配)。
MT_MAX_PROBE_KEYS = 65536

#: 多表持久化 schema(三张表,原子 ``CREATE IF NOT EXISTS`` 建立)。
_MT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mt_lsh_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mt_lsh_entries (
    seq     INTEGER PRIMARY KEY,
    hex64   TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS mt_lsh_buckets (
    table_no   INTEGER NOT NULL,
    bucket_key INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    PRIMARY KEY (table_no, bucket_key, seq)
) WITHOUT ROWID;
"""


def _probe_deltas(key_bits: int, probe_depth: int) -> tuple[int, ...]:
    """枚举探针掩码:``[0] + 翻转 1~p 位的一切掩码``(按半径分层,确定序)。

    掩码集合即桶键汉明球 ``{m : popcount(m) ≤ p}``;查询时用 ``原键 ^ 掩码``
    逐个取邻桶。组合数超过 :data:`MT_MAX_PROBE_KEYS` 时抛中文 ValueError
    (防 ``key_bits=64, probe_depth=5`` 之类误配把内存探针表撑爆;先用
    组合数公式验总额,不做无效枚举)。
    """
    total = sum(comb(key_bits, i) for i in range(probe_depth + 1))
    if total > MT_MAX_PROBE_KEYS:
        raise ValueError(
            f"probe_depth 无效:B={key_bits}、p={probe_depth} 的探针组合数 "
            f"{total} 超过上限 {MT_MAX_PROBE_KEYS}"
        )
    deltas = [0]
    for radius in range(1, probe_depth + 1):
        for combo in combinations(range(key_bits), radius):
            mask = 0
            for bit in combo:
                mask |= 1 << bit
            deltas.append(mask)
    return tuple(deltas)


def _build_luts(
    seed: int, tables: int, key_bits: int
) -> tuple[tuple[tuple[int, tuple[int, ...]], ...], ...]:
    """由 ``random.Random(seed)`` 单流确定地生成 K 张位置换的掩码查表。

    每张表先均匀洗牌出 64bit 位置换 ``perm``(``out[i] = in[perm[i]]``),
    再折叠成 16 组「半字节 → 输出位掩码」查表:置换值 = 各半字节贡献的
    异或(置换是双射,贡献位两两不交,异或即按位或)。**只保留映射到
    桶键区(置换后前 ``key_bits`` 位)的贡献组**——落在高位丢弃区的半
    字节对桶键恒为 0,直接不查,把每次键计算压到 ~(key_bits/4) 次表查找。
    """
    rng = random.Random(seed)
    luts: list[tuple[tuple[int, tuple[int, ...]], ...]] = []
    for _ in range(tables):
        perm = list(range(BITS))
        rng.shuffle(perm)
        inv = [0] * BITS  # inv[源位] = 输出位
        for out_pos, src in enumerate(perm):
            inv[src] = out_pos
        pairs: list[tuple[int, tuple[int, ...]]] = []
        for nib in range(BITS // 4):
            base = nib * 4
            row = [0] * 16
            for val in range(16):
                mask = 0
                for bit in range(4):
                    if (val >> bit) & 1:
                        out = inv[base + bit]
                        if out < key_bits:
                            mask |= 1 << out
                row[val] = mask
            if any(row):  # 全零行 = 该半字节只喂丢弃区,跳过
                pairs.append((base, tuple(row)))
        luts.append(tuple(pairs))
    return tuple(luts)


class MultiTableLSH:
    """64bit 指纹多表 multi-probe LSH 索引(A195,单表版见 ``LSHIndex``)。

    用法::

        index = MultiTableLSH(tables=8, key_bits=16, probe_depth=2)
        index.insert("0f1e2d3c4b5a6978", {"case": 1})
        hits = index.query("0f1e2d3c4b5a6979", max_distance=8)

    ``name``(A229,缺省 ``""`` 完全向后兼容):实例标识——多哈希生产
    接线后主 phash 与 mirror 各持一实例(后者 ``name="mirror"``),日志 /
    repr 以 ``[mirror]`` 前缀区分;name 不进参数指纹、不影响桶布局与
    持久化格式(实例间数据隔离由各自独立 ``db_path`` 承担),旧实例
    (无 name)的日志与行为逐字节不变。

    结构(对标 Lv et al. 2007 multi-probe LSH):

    - **K 张随机置换掩码表**(``random.Random(seed)`` 单流确定生成,同种子
      必得同桶布局):表 t 把 64bit 指纹位置换后取**前 ``key_bits`` 位**
      作桶键,插入即写 K 个桶;
    - **查询 = multi-probe**:每表取原桶 + 桶键翻转 1~``probe_depth``
      位的一切邻桶(汉明球枚举,掩码表预生成),K 表候选按条目去重后
      做精确汉明过滤 ≤ ``max_distance``,按 (距离, 插入序) 升序返回;
      ``probe_depth=0`` 退化为「仅原桶」(multi-probe 关闭,用于对照);
    - 与单表版的本质差异:单表靠「任一带完好」保证 d ≤ bands-1,带间
      翻转直接漏检(d=16 召回 ≈ 0);本类靠**多表独立置换 + 邻桶探针**,
      单表漏检须 K 张表同时漏检,高距区召回由 0 拉回 99%+(实测见
      ``multitable_selfcheck`` 与 tests/test_phash_lsh.py 的 10^4 规模用例)。

    **召回下界论证(d ≤ 2·probe_depth)**:设近邻与查询的汉明距离
    d ≤ 2p。对任一张表,置换把 d 个翻转位一一映射到 64 个置换位,落入
    桶键区的个数 J 服从超几何分布,期望 ``E[J] = d·B/64 ≤ 2pB/64 =
    pB/32``(默认 B=16 时 ≤ p/2)。近邻桶键与查询桶键的汉明距离恰为 J,
    而探针恰好覆盖距查询桶键 ≤ p 的一切邻桶,故**该表漏检 ⇔ J > p**。
    由 Markov 不等式::

        P(J > p) ≤ E[J]/(p+1) ≤ pB / (32·(p+1))     # 默认参数 < 1/3

    K 张表的置换独立生成、漏检事件独立,故::

        P(全表漏检) ≤ (pB/(32·(p+1)))^K ≤ 3^-K      # 默认 K=8 → ≤ 0.015%

    即 **d ≤ 2·probe_depth 时召回率 ≥ 1 - (pB/(32(p+1)))^K**(默认参数
    下 d ≤ 4,漏检上界 1.7e-11 量级;精确超几何值远好于此 Markov 界)。
    相比单表版的确定性保证(d ≤ bands-1),该界是概率性的,但覆盖距离
    翻倍,且 d=12/16 高距区实测仍保持高召回(单表在该域召回 ≈ 0)。

    **操作计数**(红线 31,不依赖墙钟):

    - ``compare_calls``:查询实际执行的精确汉明比较次数(= 去重后候选数);
    - ``probe_calls``:查询执行的桶查找次数(= K × 探针掩码数,含落空)。

    **sqlite 持久化(``db_path`` 注入,可选;缺省纯内存)**:

    - schema 三表:``mt_lsh_meta``(参数指纹 schema_version/seed/tables/
      key_bits)、``mt_lsh_entries``(seq 主键, hex64, payload 的 JSON
      文本或 NULL)、``mt_lsh_buckets``(table_no, bucket_key, seq 联合
      主键, WITHOUT ROWID);构造时以单事务原子建表;
    - **flush 协议**:``insert``/``query`` 只走内存(进程内零磁盘 IO);
      ``flush()`` 先并入尚未惰性加载的持久态,再以单事务**全量覆盖**
      重写三张表(幂等);负载需可 JSON 序列化(dict/list/str/数字/
      bool/None),不可序列化时 flush 抛中文 ValueError、内存态不受影响;
    - **惰性加载**:带 ``db_path`` 构造后**首次** insert/query/stats 时
      才把持久态并入内存一次(之后继续内存写,直到下次 flush);
    - **安全重建**:库文件损坏(非 sqlite 格式)→ 告警 + 删除重建空库
      (安全方向失效:丢的只是索引缓存,上游重灌即可);库内脏行(坏
      hex / 坏 JSON / 悬空 seq)跳过并计 telemetry;参数指纹不匹配(换
      seed/K/B 复用旧库)→ 不加载旧桶位、由条目按当前参数重算(告警 +
      telemetry)。

    序列化兼容口径:``probe_depth`` 是**查询期**参数、不影响桶布局,故
    不入参数指纹;内存负载为任意对象,持久负载经 JSON 往返(dict 键序
    不保序,内容等价)。

    线程安全:全部读写经一把 ``threading.RLock`` 串行化,sqlite 连接
    ``check_same_thread=False``(与 ``PhashRegistry`` 同口径)。
    """

    def __init__(
        self,
        tables: int = DEFAULT_TABLES,
        key_bits: int = DEFAULT_KEY_BITS,
        probe_depth: int = DEFAULT_PROBE_DEPTH,
        seed: int = DEFAULT_MT_SEED,
        db_path: str | None = None,
        name: str = "",
    ) -> None:
        if not isinstance(tables, int) or isinstance(tables, bool) \
                or tables < 1 or tables > MAX_TABLES:
            raise ValueError(
                f"tables 无效:须为 1~{MAX_TABLES} 的整数,得到 {tables!r}"
            )
        if not isinstance(key_bits, int) or isinstance(key_bits, bool) \
                or key_bits < 1 or key_bits > BITS:
            raise ValueError(
                f"key_bits 无效:须为 1~{BITS} 的整数,得到 {key_bits!r}"
            )
        if not isinstance(probe_depth, int) or isinstance(probe_depth, bool) \
                or probe_depth < 0 or probe_depth > key_bits:
            raise ValueError(
                f"probe_depth 无效:须为 0~{key_bits} 的整数,"
                f"得到 {probe_depth!r}"
            )
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError(f"seed 无效:须为整数,得到 {seed!r}")
        if not isinstance(name, str):
            raise ValueError(f"name 无效:须为字符串,得到 {name!r}")
        self.tables = int(tables)
        self.key_bits = int(key_bits)
        self.probe_depth = int(probe_depth)
        self.seed = int(seed)
        self.db_path = str(db_path) if db_path is not None else None
        #: 实例标识(A229,缺省 ""= 旧口径):只用于 repr 与运维日志,
        #: 不进参数指纹 / 桶布局 / 持久化格式;实例隔离由 db_path 承担。
        self.name = str(name)
        self._log_tag = f"[{self.name}] " if self.name else ""
        #: 每表一组 (半字节基址, 16 项掩码行) 查表(只含喂桶键区的组)。
        self._luts = _build_luts(self.seed, self.tables, self.key_bits)
        self._probe_deltas = _probe_deltas(self.key_bits, self.probe_depth)
        #: 每表一个 {桶键: 条目下标 或 下标元组};下标即插入序。
        self._tables: list[dict[int, int | tuple[int, ...]]] = [
            {} for _ in range(self.tables)
        ]
        #: (归一化 hex, int 值, payload) 三元组,按下标即插入序存放。
        self._entries: list[tuple[str, int, Any]] = []
        #: 查询累计精确汉明比较次数(操作计数,红线 31)。
        self.compare_calls = 0
        #: 查询累计桶查找次数(含落空;multi-probe 成本刻度)。
        self.probe_calls = 0
        self._lock = threading.RLock()
        self._conn = self._open() if self.db_path else None
        self._loaded = False

    # ------------------------------------------------------------------
    # 内部:桶键与桶写入
    # ------------------------------------------------------------------

    @staticmethod
    def _bucket_key(
        lut_pairs: tuple[tuple[int, tuple[int, ...]], ...], value: int
    ) -> int:
        """整数指纹 → 桶键(各喂桶键区半字节的掩码行异或)。"""
        key = 0
        for shift, row in lut_pairs:
            key ^= row[(value >> shift) & 0xF]
        return key

    @staticmethod
    def _put(
        buckets: dict[int, int | tuple[int, ...]], key: int, seq: int
    ) -> None:
        """写入一个桶位:空桶存 int,首个碰撞升格 tuple(K 表大索引省内存)。

        桶成员绝大多数为单条(10^4 条 × 2^26 键空间时每表碰撞期望 < 1),
        单独存 int 比每桶一个 list 省一个数量级的桶对象内存;查询侧按
        ``type(m) is int`` 分支展开。
        """
        prev = buckets.get(key)
        if prev is None:
            buckets[key] = seq
        elif type(prev) is int:
            buckets[key] = (prev, seq)
        else:
            buckets[key] = prev + (seq,)

    def _table_keys(self, value: int) -> tuple[int, ...]:
        """整数指纹 → K 张表各自的桶键(自检 / 测试用)。"""
        return tuple(
            self._bucket_key(lut, value) for lut in self._luts
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def insert(self, hex64: str, payload: Any) -> None:
        """登记一条 64bit 指纹与任意负载;非法哈希抛中文 ValueError。

        同指纹重复插入不去重;只写内存(K 张桶表 + 条目表),持久化由
        ``flush`` 显式触发(见类 docstring 的 flush 协议)。
        """
        h = _normalize_hex64(hex64, "指纹 hex64")
        value = int(h, 16)
        with self._lock:
            self._ensure_loaded()
            seq = len(self._entries)
            self._entries.append((h, value, payload))
            for buckets, lut in zip(self._tables, self._luts):
                self._put(buckets, self._bucket_key(lut, value), seq)

    # ------------------------------------------------------------------
    # 查询(multi-probe)
    # ------------------------------------------------------------------

    def query(self, hex64: str, max_distance: int = 8) -> list[Any]:
        """multi-probe 查询与查询指纹汉明距离 ≤ max_distance 的全部负载。

        流程:每表算原桶键 → 探 ``原键 ^ 掩码``(掩码覆盖桶键汉明球
        半径 ``probe_depth``,含 0 = 原桶)→ K 表命中并入候选集(**按
        条目去重**)→ 每候选一次精确汉明比较(计入 ``compare_calls``)
        → 过滤 ≤ max_distance → 按 (距离, 插入序) 升序返回负载。
        非法哈希 / max_distance 越界抛中文 ValueError。

        遥测:每次查询 ``phash.mt_lsh_query`` +1,比较次数累加
        ``phash.mt_lsh_compare``(只存数字,红线 17)。
        """
        h = _normalize_hex64(hex64, "查询 hex64")
        if not isinstance(max_distance, int) or isinstance(max_distance, bool) \
                or max_distance < 0 or max_distance > BITS:
            raise ValueError(
                f"max_distance 无效:须为 0~{BITS} 的整数,得到 {max_distance!r}"
            )
        value = int(h, 16)
        with self._lock:
            self._ensure_loaded()
            candidates: set[int] = set()
            deltas = self._probe_deltas
            probes = 0
            for buckets, lut in zip(self._tables, self._luts):
                key0 = self._bucket_key(lut, value)
                get = buckets.get
                for delta in deltas:
                    probes += 1
                    members = get(key0 ^ delta)
                    if members is not None:
                        if type(members) is int:
                            candidates.add(members)
                        else:
                            candidates.update(members)
            hits: list[tuple[int, int, Any]] = []
            for seq in sorted(candidates):
                _, entry_value, payload = self._entries[seq]
                distance = (value ^ entry_value).bit_count()
                self.compare_calls += 1
                if distance <= max_distance:
                    hits.append((distance, seq, payload))
            hits.sort(key=lambda item: (item[0], item[1]))
            self.probe_calls += probes
            telemetry.inc("phash.mt_lsh_query")
            telemetry.inc("phash.mt_lsh_compare", len(candidates))
            return [payload for _, _, payload in hits]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """索引统计:条目数 / 表数 / 键宽 / 探针深度 / 非空桶数 / 平均桶员。

        ``buckets`` 为 K 张表非空桶键总数;``avg_bucket`` = 桶员总数 ÷
        非空桶数(空索引为 0.0),即随机查询落入某桶时平均要比较多少条
        的直观刻度(未含邻桶探针的放大,探针成本看 ``probe_calls``)。
        """
        with self._lock:
            self._ensure_loaded()
            entries = len(self._entries)
            buckets = sum(len(table) for table in self._tables)
            members = sum(
                1 if type(m) is int else len(m)
                for table in self._tables
                for m in table.values()
            )
            return {
                "entries": entries,
                "tables": self.tables,
                "key_bits": self.key_bits,
                "probe_depth": self.probe_depth,
                "buckets": buckets,
                "avg_bucket": members / buckets if buckets else 0.0,
            }

    # ------------------------------------------------------------------
    # sqlite 持久化(flush 协议,见类 docstring)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """建立连接并原子建表(WAL + busy_timeout,全仓 sqlite 口径)。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)  # type: ignore[arg-type]
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_MT_SCHEMA_SQL)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            conn.close()
            raise

    def _open(self) -> sqlite3.Connection:
        """打开(或新建)持久库;文件损坏时删除重建为空库(安全方向失效)。"""
        parent = Path(self.db_path).parent  # type: ignore[arg-type]
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            logger.warning(
                "%s多表 LSH 持久库损坏,已删除重建(丢的只是索引缓存,"
                "上游重灌即可):%s",
                self._log_tag,
                self.db_path,
                exc_info=True,
            )
            telemetry.inc("phash.mt_lsh_rebuild")
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(self.db_path + suffix).unlink(missing_ok=True)  # type: ignore[arg-type]
            return self._connect()

    def _ensure_loaded(self) -> None:
        """惰性把持久态并入内存一次(带 db_path 构造后的首次访问)。

        参数指纹(seed/tables/key_bits)匹配时直接装载存好的桶位;不匹配
        时只装条目、按当前参数重算桶位(旧桶位对新参数无意义);库内脏
        行(坏 hex / 坏 JSON / 悬空 seq)跳过并计 ``phash.mt_lsh_dirty_row``。
        """
        if self._conn is None or self._loaded:
            return
        self._loaded = True
        conn = self._conn
        try:
            meta = dict(conn.execute("SELECT key, value FROM mt_lsh_meta"))
            rows = conn.execute(
                "SELECT seq, hex64, payload FROM mt_lsh_entries ORDER BY seq"
            ).fetchall()
        except sqlite3.DatabaseError:
            logger.warning(
                "%s多表 LSH 持久库读取失败,按空库重建:%s",
                self._log_tag,
                self.db_path,
                exc_info=True,
            )
            telemetry.inc("phash.mt_lsh_rebuild")
            return
        params_match = (
            meta.get("schema_version") == "1"
            and meta.get("seed") == str(self.seed)
            and meta.get("tables") == str(self.tables)
            and meta.get("key_bits") == str(self.key_bits)
        )
        if meta and not params_match:
            logger.warning(
                "%s多表 LSH 持久库参数指纹不匹配(seed/tables/key_bits),"
                "旧桶位不适用于当前参数,改由条目重算桶位:%s",
                self._log_tag,
                self.db_path,
            )
            telemetry.inc("phash.mt_lsh_param_mismatch")

        dirty = 0
        remap: dict[int, int] = {}
        for seq, hex_raw, payload_text in rows:
            try:
                hex64 = _normalize_hex64(hex_raw, "库内指纹")
                payload = (
                    json.loads(payload_text) if payload_text is not None else None
                )
            except ValueError:
                dirty += 1
                logger.debug("跳过多表 LSH 库中脏行:seq=%s", seq)
                continue
            remap[int(seq)] = len(self._entries)
            self._entries.append((hex64, int(hex64, 16), payload))

        if params_match:
            for table_no, bucket_key, seq in conn.execute(
                "SELECT table_no, bucket_key, seq FROM mt_lsh_buckets"
            ):
                new_seq = remap.get(int(seq))
                if new_seq is None or not 0 <= int(table_no) < self.tables:
                    dirty += 1
                    continue
                self._put(self._tables[int(table_no)], int(bucket_key), new_seq)
        else:
            for seq, (_, value, _) in enumerate(self._entries):
                for buckets, lut in zip(self._tables, self._luts):
                    self._put(buckets, self._bucket_key(lut, value), seq)
        if dirty:
            telemetry.inc("phash.mt_lsh_dirty_row", dirty)
        if self._entries:
            telemetry.inc("phash.mt_lsh_load", len(self._entries))
            logger.debug(
                "%s多表 LSH 持久库惰性加载完成:条目 %d,脏行 %d,参数指纹%s",
                self._log_tag,
                len(self._entries), dirty, "匹配" if params_match else "不匹配(重算桶位)",
            )

    def flush(self) -> int:
        """把内存态(含惰性并入的持久态)单事务全量覆写进 sqlite,返回条数。

        覆盖语义:以当前内存内容为准重写三张表(幂等,可反复调用)。
        负载需可 JSON 序列化,不可序列化时抛中文 ValueError且**不落任何
        半笔**(事务回滚,内存态不受影响)。纯内存索引(无 db_path)抛
        中文 ValueError。
        """
        with self._lock:
            self._ensure_loaded()
            if self._conn is None:
                raise ValueError("纯内存索引未注入 db_path,无法 flush")
            entry_rows: list[tuple[int, str, str | None]] = []
            for seq, (hex64, _value, payload) in enumerate(self._entries):
                if payload is None:
                    entry_rows.append((seq, hex64, None))
                    continue
                try:
                    encoded = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"flush 失败:第 {seq} 条负载不可 JSON 序列化"
                        f"({exc});持久化负载须为 dict/list/str/数字/bool/None"
                    ) from exc
                entry_rows.append((seq, hex64, encoded))
            bucket_rows: list[tuple[int, int, int]] = []
            for table_no, buckets in enumerate(self._tables):
                for bucket_key, members in buckets.items():
                    if type(members) is int:
                        bucket_rows.append((table_no, bucket_key, members))
                    else:
                        bucket_rows.extend(
                            (table_no, bucket_key, seq) for seq in members
                        )
            conn = self._conn
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM mt_lsh_meta")
                conn.execute("DELETE FROM mt_lsh_entries")
                conn.execute("DELETE FROM mt_lsh_buckets")
                conn.executemany(
                    "INSERT OR REPLACE INTO mt_lsh_meta VALUES (?, ?)",
                    (
                        ("schema_version", "1"),
                        ("seed", str(self.seed)),
                        ("tables", str(self.tables)),
                        ("key_bits", str(self.key_bits)),
                    ),
                )
                conn.executemany(
                    "INSERT INTO mt_lsh_entries VALUES (?, ?, ?)", entry_rows
                )
                conn.executemany(
                    "INSERT INTO mt_lsh_buckets VALUES (?, ?, ?)", bucket_rows
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            telemetry.inc("phash.mt_lsh_flush", len(entry_rows))
            return len(entry_rows)

    def close(self) -> None:
        """关闭 sqlite 连接(幂等);支持 ``with`` 上下文。未 flush 的内存态丢弃。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return (
            f"MultiTableLSH(name={self.name!r}, tables={self.tables}, "
            f"key_bits={self.key_bits}, probe_depth={self.probe_depth}, "
            f"db_path={self.db_path!r})"
        )

    def __enter__(self) -> "MultiTableLSH":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 多表离线自检(A195;与 kernel_selfcheck 并存,红线 31:操作计数)
# ---------------------------------------------------------------------------


def multitable_selfcheck() -> dict[str, Any]:
    """离线可复现微基准:默认参数多表索引在 d=4 保证域的召回与比较比例。

    构造 2000 条随机 64bit 指纹 + 60 个 d=4 已知近邻(d=4 ≤ 2×probe_depth
    = 4,落在类 docstring 的可证召回域内),以基址查询 ``max_distance=4``:
    断言 60 个近邻全部召回且单查询平均汉明比较 ≤ 全表扫描的 10%,
    返回 ``{"name", "metric", "value", "baseline", "recall_d4"}``。
    """
    rng = random.Random(20261006)
    index = MultiTableLSH()
    entries: list[tuple[str, int]] = []
    for seq in range(2000):
        hex64 = f"{rng.getrandbits(BITS):016x}"
        index.insert(hex64, seq)
        entries.append((hex64, seq))
    probes: list[tuple[str, int]] = []
    for seq in range(60):
        base = f"{rng.getrandbits(BITS):016x}"
        value = int(base, 16)
        for pos in rng.sample(range(BITS), 4):  # d=4 恰在 2p 保证域边界
            value ^= 1 << pos
        neighbor = f"{value:016x}"
        index.insert(neighbor, 2000 + seq)
        probes.append((base, 2000 + seq))

    recalled = 0
    before = index.compare_calls
    for base, neighbor_seq in probes:
        if neighbor_seq in index.query(base, max_distance=4):
            recalled += 1
    used = index.compare_calls - before
    total = len(entries) + 60
    assert recalled == len(probes), (
        f"d=4(≤ 2·probe_depth)保证域漏检:{len(probes) - recalled} 个(自查)"
    )
    ratio = used / (total * len(probes))
    assert ratio <= 0.1, f"多表 LSH 平均比较比例 {ratio:.4f} 超过全表 10%(自查)"
    return {
        "name": "phash_lsh_mt",
        "metric": "query_compare_ratio_vs_full_scan",
        "value": round(ratio, 6),
        "baseline": 1.0,
        "recall_d4": round(recalled / len(probes), 6),
    }
