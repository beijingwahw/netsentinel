"""图谱内核·增量并查集(A128)+ 加权社区检测(A194)。

依据 CONTRACTS-V7.md §2 A128 与红线 29/31:为 A46 站点关联图谱
(EvidenceGraph,SQLite 证据图)补一个**纯内存、零依赖、全增量**的团伙
归并内核 ``UnionFindKernel``:

- **并查集**:路径压缩 + 按秩合并,``union`` / ``find`` / ``connected``
  均摊 O(α(n))(反阿克曼函数,实践中视为常数);
- **惰性分量缓存**:``components()`` 只在缓存失效后的首次查询时重算
  (O(n·α)),其后查询 O(1) 命中同一对象;**有效合并 / 新增节点**才
  丢弃缓存,冗余 union 不失效——``recompute_count`` / ``invalidate_count``
  导出重算与失效次数,供 kernelbench 断言"查询零全量重算"(红线 31);
- **邻接表同步维护**:每条边(含已同分量的环边)同时进入邻接表与导出
  边集,``related(host, depth)`` 在邻接表上做 BFS(不含自身;深放时
  即"分量内可达"语义);
- **与 A46 共存**:A46 负责 SQLite 持久化 / 边种类 / 权重,本内核负责
  内存中的增量归并与邻域查询;两者仅经 ``export_json()`` / ``export()``
  互导数据。本模块**零网络、零第三方依赖、不 import 任何 netsentinel
  模块、不改 A46**(红线 12/29/30)。

复杂度一览(α 为反阿克曼函数)::

    union / find          均摊 O(α(n))(路径压缩 + 按秩合并;find 迭代实现)
    connected             O(α(n));全量压缩后每次至多 2 次父指针跳转
    components            缓存命中 O(1);失效后首次 O(n·α)
    related(host, depth)  O(深度内展开子图) BFS
    export                O(N log N)(排序,输出确定)

操作计数导出(红线 31:基准以操作计数断言,禁墙钟):

- ``recompute_count``  —— ``components()`` 真实重算连通分量的次数;
- ``invalidate_count`` —— 有效缓存被写口丢弃的次数(缓存本就失效时
  不重复计数,故 bulk 灌入期恒为 0);
- ``find_hops``        —— ``find()`` 根查找阶段的父指针跳转累计次数。

非目标:不做持久化(内存工作集,持久化归 A46)、不加锁(单线程装配线
使用;跨线程请外部串行化)。``kernel_selfcheck()`` 供 A138 内核基准
总控调用。

A194 追加**加权社区检测**(对标无监督加权社区检测 SOTA 的 Louvain
方法,纯函数零 IO):

- :func:`louvain_pass` —— 单层贪心模块度局部移动(local moving):
  每轮按节点 id 序扫描,把节点移入使加权模块度增量最大的邻接社区,
  平局取社区键最小者(确定性);迭代上限 ``max_sweeps`` / 收敛阈值
  ``threshold`` / 分辨率 ``resolution`` 可配;
- :func:`louvain_communities` —— 多层包装:单层结果聚合为超节点
  (社区内边折叠为自环)后重复调用 ``louvain_pass``,至划分稳定
  (社区数不再减少 / 归一或达到 ``max_levels``),标签映射回原始节点;
- :func:`louvain_modularity` —— 加权模块度 Q(resolution 参数化),
  供"模块度单调不减"自证与上层调参消费。

模块度口径(自环 w 计入度两次、计入 Σ_in 一次;2m = Σ 全部度数)::

    Q = Σ_c [ Σ_in,c / 2m − resolution · (Σ_tot,c / 2m)² ]
"""
from __future__ import annotations

import math
import random
from collections.abc import Iterable

__all__ = [
    "UnionFindKernel",
    "kernel_selfcheck",
    "louvain_communities",
    "louvain_modularity",
    "louvain_pass",
]


class UnionFindKernel:
    """增量并查集 + 同步邻接表:O(α) 团伙归并、O(1) 分量缓存、BFS 邻域。

    用法(典型流)::

        kernel = UnionFindKernel()
        kernel.ingest_edges([("site:a", "site:b"), ("site:b", "site:c")])
        kernel.connected("site:a", "site:c")      # True(传递成团)
        kernel.components()                        # {root: {"site:a","site:b","site:c"}}
        kernel.related("site:a", depth=1)          # {"site:b"}

    - 节点为任意可哈希对象(实践中为 A46 的 ``site:<url>`` id);
      ``union`` / ``find`` 遇到未登记节点会**自动登记**(自成一棵单点树),
      ``connected`` 是纯查询、绝不产生副作用;
    - ``union(a, b)`` 仅在两端原本属于不同分量(发生真实合并)时返回
      ``True``;冗余边(含自环)返回 ``False`` 但仍进入邻接表与导出边集
      ——团伙归属(并查集)与邻域拓扑(邻接表)是两套同步维护的视图;
    - ``components()`` 返回**惰性缓存的活视图**(重复调用返回同一 dict,
      O(1));调用方只读,写口会自动失效重建。
    """

    def __init__(self) -> None:
        # 并查集核心:parent[x] 为父节点(根指向自身);rank 仅作合并依据。
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}
        # 同步邻接表(无向,去重)与规范化边集(导出口径,排序后确定)。
        self._adjacency: dict[str, set[str]] = {}
        self._edges: set[tuple[str, str]] = set()
        # 惰性分量缓存:None = 已失效 / 从未物化。
        self._components_cache: dict[str, set[str]] | None = None
        #: ``components()`` 自实例创建以来真实重算连通分量的次数(bench 用)。
        self.recompute_count: int = 0
        #: 有效缓存被写口(真实合并 / 新增节点)丢弃的次数;缓存本就失效不计数。
        self.invalidate_count: int = 0
        #: ``find()`` 根查找阶段的父指针跳转累计次数(操作计数,红线 31)。
        self.find_hops: int = 0

    # ------------------------------------------------------------------
    # 内部:缓存失效与边登记
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """丢弃已物化的分量缓存(缓存本就失效时不动、不计数)。"""
        if self._components_cache is not None:
            self._components_cache = None
            self.invalidate_count += 1

    def _record_edge(self, a: str, b: str) -> None:
        """把一条无向边登记进邻接表与导出边集(自环忽略、重复去重)。

        调用方须保证 a / b 已登记(``find`` 已自动补建)。
        """
        if a == b:
            return
        key = (a, b) if a <= b else (b, a)
        if key in self._edges:
            return
        self._edges.add(key)
        self._adjacency[a].add(b)
        self._adjacency[b].add(a)

    # ------------------------------------------------------------------
    # 节点与并查集核心
    # ------------------------------------------------------------------

    def add(self, a: str) -> bool:
        """登记一个节点(自成一棵单点树);返回是否为新登记。

        幂等:重复 add 同一节点返回 ``False`` 且不失效分量缓存;
        新登记会令已物化的分量缓存失效(多出一个单点分量)。
        """
        if a in self._parent:
            return False
        self._parent[a] = a
        self._rank[a] = 0
        self._adjacency[a] = set()
        self._invalidate()
        return True

    def find(self, a: str) -> str:
        """返回 a 所在分量的根;未登记节点自动登记并自成根。

        迭代实现(两趟:先找根、再路径压缩),长链也不会触及递归上限;
        根查找阶段每次父指针跳转计入 ``find_hops`` 供 bench 断言复杂度。
        """
        parent = self._parent
        if a not in parent:
            self.add(a)
            return a
        root = a
        while parent[root] != root:
            root = parent[root]
            self.find_hops += 1
        # 路径压缩:把沿途节点直接挂到根上。
        while parent[a] != root:
            parent[a], a = root, parent[a]
        return root

    def union(self, a: str, b: str) -> bool:
        """合并 a、b 所在分量;返回是否发生**真实合并**。

        - 两端原本同分量(含自环 / 环边)→ ``False``,不失效分量缓存,
          但边仍进入邻接表与导出边集(邻域拓扑照实记录);
        - 真实合并 → 按秩合并(小树挂大树,等秩则根的秩 +1)并失效
          分量缓存;两端未登记时自动登记。
        """
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            self._record_edge(a, b)
            return False
        if self._rank[root_a] < self._rank[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        if self._rank[root_a] == self._rank[root_b]:
            self._rank[root_a] += 1
        self._record_edge(a, b)
        self._invalidate()
        return True

    def connected(self, a: str, b: str) -> bool:
        """纯查询:a、b 是否同分量;未登记节点返回 ``False``(零副作用)。"""
        if a not in self._parent or b not in self._parent:
            return False
        return self.find(a) == self.find(b)

    # ------------------------------------------------------------------
    # 分量查询:惰性缓存
    # ------------------------------------------------------------------

    def components(self) -> dict[str, set[str]]:
        """返回 ``{根: 成员集合}``;惰性缓存,命中时 O(1) 零重算。

        - 首次调用(或缓存被真实合并 / 新节点失效后的首次调用)重算
          全部分量并计入 ``recompute_count``;其后调用返回**同一对象**;
        - 返回值是缓存的活视图,调用方只读;任何写口之后应重新调用
          获取新视图(旧视图不再被内部引用)。
        """
        cache = self._components_cache
        if cache is None:
            cache = {}
            for node in self._parent:
                cache.setdefault(self.find(node), set()).add(node)
            self._components_cache = cache
            self.recompute_count += 1
        return cache

    # ------------------------------------------------------------------
    # 批量灌入与邻域查询
    # ------------------------------------------------------------------

    def ingest_edges(self, edges: Iterable[tuple[str, str]]) -> int:
        """批量灌入无向边(接受任意可迭代对象,含生成器)。

        逐条走 ``union`` 语义(未登记端点自动登记、环边进邻接表);
        返回发生**真实合并**的边数(= 节点数 − 最终分量数)。
        """
        merged = 0
        for a, b in edges:
            if self.union(a, b):
                merged += 1
        return merged

    def related(self, host: str, depth: int = 1) -> set[str]:
        """BFS 查询 host 的 ``depth`` 跳邻域(基于邻接表,不含自身)。

        - ``depth <= 0``、host 未登记或为孤立节点 → 空集合;
        - 深度放大后即"分量内可达"语义:``depth`` ≥ 分量直径时返回
          整个分量去掉 host 自身;
        - 边为无向去重边;环边(已同分量的冗余边)同样计入邻域。
        """
        depth = int(depth)
        if depth <= 0 or host not in self._adjacency:
            return set()
        seen = {host}
        frontier = [host]
        for _level in range(depth):
            next_frontier: list[str] = []
            for node in frontier:
                for neighbor in self._adjacency.get(node, ()):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        next_frontier.append(neighbor)
            if not next_frontier:
                break
            frontier = next_frontier
        seen.discard(host)
        return seen

    # ------------------------------------------------------------------
    # 导出与杂项
    # ------------------------------------------------------------------

    def export(self) -> dict[str, list]:
        """整图导出为确定性结构:``{"nodes": 排序节点列表, "edges": 排序边列表}``。

        边为端点排序规范化的 ``(小, 大)`` 二元组,自环不入库、重复去重;
        两个列表均排序输出,可直接 JSON 序列化(供复核台 / 与 A46 互导)。
        """
        return {"nodes": sorted(self._parent), "edges": sorted(self._edges)}

    def __len__(self) -> int:
        """节点数。"""
        return len(self._parent)

    def __repr__(self) -> str:  # pragma: no cover - 调试口径
        state = "cached" if self._components_cache is not None else "dirty"
        return (
            f"<UnionFindKernel nodes={len(self._parent)} edges={len(self._edges)} "
            f"components={state} recomputes={self.recompute_count}>"
        )


# ---------------------------------------------------------------------------
# 加权社区检测(A194):Louvain 单层局部移动 + 多层聚合(纯函数,零 IO)
# ---------------------------------------------------------------------------


def _normalize_weighted_graph(
    adjacency: object,
) -> tuple[list[str], dict[tuple[str, str], float], dict[str, float]]:
    """把调用方邻接 dict 规整为 ``(排序节点表, 无向边权重, 自环权重)``。

    - 入参口径 ``{节点: {邻居: 非负权重}}``;邻居与键共同构成节点全集
      (只作为邻居出现、未单独登记的节点同样成点);
    - **无向语义**:``adj[a][b]`` 与 ``adj[b][a]`` 视为同一条边的两个
      方向,权重求和(调用方通常只写一个方向);``a == b`` 的自环单列;
    - 权重 0 的边丢弃(对模块度零贡献);负数 / NaN / Inf 抛中文
      ``ValueError``(Jaccard 类权重恒非负,负权属调用方错误);
    - 逐键**排序遍历**,规整结果与输入 dict 的插入顺序无关(确定性)。
    """
    if not isinstance(adjacency, dict):
        raise ValueError(
            "adjacency 无效:需为 {节点: {邻居: 权重}} 字典,"
            f"得到 {type(adjacency).__name__}"
        )
    nodes: set[str] = set()
    edges: dict[tuple[str, str], float] = {}
    loops: dict[str, float] = {}
    for a in sorted(adjacency):
        neighbors = adjacency[a]
        if neighbors is None:
            neighbors = {}
        if not isinstance(neighbors, dict):
            raise ValueError(
                f"adjacency[{a!r}] 无效:需为 {{邻居: 权重}} 字典,"
                f"得到 {type(neighbors).__name__}"
            )
        nodes.add(a)
        for b in sorted(neighbors):
            nodes.add(b)
            weight = float(neighbors[b])
            if not math.isfinite(weight):
                raise ValueError(f"边 {a!r}-{b!r} 权重非法(须为有限数):{weight!r}")
            if weight < 0.0:
                raise ValueError(f"边 {a!r}-{b!r} 权重须非负,得到 {weight!r}")
            if weight == 0.0:
                continue  # 零权边对模块度零贡献,直接丢弃
            if a == b:
                loops[a] = loops.get(a, 0.0) + weight
            else:
                key = (a, b) if a <= b else (b, a)
                edges[key] = edges.get(key, 0.0) + weight
    return sorted(nodes), edges, loops


def _materialize_adjacency(
    nodes: Iterable[str],
    edges: dict[tuple[str, str], float],
    loops: dict[str, float],
) -> dict[str, dict[str, float]]:
    """把 (节点表, 无向边, 自环) 物化回单方向邻接 dict(自环入自身键)。"""
    adjacency: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for node, weight in loops.items():
        if node in adjacency:
            adjacency[node][node] = adjacency[node].get(node, 0.0) + weight
    for (a, b), weight in edges.items():
        adjacency[a][b] = adjacency[a].get(b, 0.0) + weight  # 只写一个方向
    return adjacency


def _canonical_labels(nodes: Iterable[str], labels: dict[str, str]) -> dict[str, int]:
    """把任意社区键的划分重标为 0..k-1:按节点 id 序首见定标签(确定性)。"""
    order: dict[str, int] = {}
    out: dict[str, int] = {}
    for node in nodes:  # nodes 已排序
        key = labels[node]
        if key not in order:
            order[key] = len(order)
        out[node] = order[key]
    return out


def louvain_pass(
    adjacency: dict[str, dict[str, float]],
    *,
    resolution: float = 1.0,
    max_sweeps: int = 100,
    threshold: float = 1e-7,
    stats: dict[str, int] | None = None,
) -> dict[str, int]:
    """加权模块度贪心**单层**局部移动:返回 ``{节点: 社区标签}``(纯函数)。

    算法(Louvain 第一阶段 local moving,加权模块度参数化 resolution):

    - 每节点初始自成一社区;逐轮按**节点 id 升序**扫描,先把节点从当前
      社区摘出,再在邻接社区(含摘出前的原社区)里取模块度增量最大者
      移入——单次移动的净增量恒 ``2/2m · (score_best − score_home)``,
      采纳条件为严格更优,故**每次移动都严格不降模块度**;
    - 平局处理:候选社区按社区键升序评估,严格 ``>`` 比较使**键最小者
      胜出**,全程零随机(同输入同输出);
    - 收敛:一轮零移动,或本轮全部移动的模块度增量合计 < ``threshold``,
      或达到 ``max_sweeps`` 轮上限;
    - 输出经 :func:`_canonical_labels` 重标为 0..k-1(按最小成员节点序)。

    :param adjacency:  ``{节点: {邻居: 非负权重}}``;双向都给出时同边权重
        求和;自环允许(聚合层产生),权重 0 / 负 / NaN 抛中文 ValueError。
    :param resolution: 分辨率 γ(Q 的期望项系数),> 1 偏好更小社区。
    :param max_sweeps: 单层扫描轮数上限(>= 1)。
    :param threshold:  收敛阈值:一轮移动的模块度增量合计低于它即停(>= 0)。
    :param stats:      可选操作计数累加器(``sweeps`` / ``moves`` /
                       ``gain_evals``;多次调用累加,便于多层包装汇总)。
    :return: ``{节点: 社区标签(int, 0..k-1, 确定性)}``;空图 → 空 dict。
    """
    nodes, edges, loops = _normalize_weighted_graph(adjacency)
    resolution = float(resolution)
    if not math.isfinite(resolution) or resolution <= 0.0:
        raise ValueError(f"resolution 无效:须为正有限数,得到 {resolution!r}")
    max_sweeps = int(max_sweeps)
    if max_sweeps < 1:
        raise ValueError(f"max_sweeps 无效:须为 >= 1 的整数,得到 {max_sweeps!r}")
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold < 0.0:
        raise ValueError(f"threshold 无效:须为 >= 0 的有限数,得到 {threshold!r}")

    neighbor_of: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for (a, b), weight in edges.items():
        neighbor_of[a][b] = weight
        neighbor_of[b][a] = weight
    degree = {
        node: sum(neighbor_of[node].values()) + 2.0 * loops.get(node, 0.0)
        for node in nodes
    }
    total_two_m = sum(degree.values())  # 2m = 全部度数之和(自环双计)

    if total_two_m <= 0.0 or not edges:  # 全孤立 / 零权:各成一区
        if stats is not None:
            stats["sweeps"] = stats.get("sweeps", 0) + 0
            stats["moves"] = stats.get("moves", 0) + 0
            stats["gain_evals"] = stats.get("gain_evals", 0) + 0
        return _canonical_labels(nodes, {node: node for node in nodes})

    community: dict[str, str] = {node: node for node in nodes}
    sigma_tot: dict[str, float] = dict(degree)  # 各社区总度(初始=单点)
    sweeps = 0
    total_moves = 0
    gain_evals = 0
    while sweeps < max_sweeps:
        sweeps += 1
        moved = 0
        sweep_gain = 0.0
        for node in nodes:  # 节点 id 升序(确定性)
            home = community[node]
            sigma_tot[home] -= degree[node]  # 先摘出
            weights_to_com: dict[str, float] = {}
            for other, weight in neighbor_of[node].items():
                com = community[other]
                weights_to_com[com] = weights_to_com.get(com, 0.0) + weight
            # 各候选社区的插入得分:ΔQ(c) ∝ w_ic − 2·res·Σ_tot,c·k_i / 2m
            # (推导:ΔQ_c = (w_ic + s_i)/T − res·(2Σ_tot,c·k_i + k_i²)/T²,
            #  T = 2m;s_i 与 k_i² 与候选无关可省;净增量 = score 差 / T)。
            home_score = (
                weights_to_com.get(home, 0.0)
                - 2.0 * resolution * sigma_tot[home] * degree[node] / total_two_m
            )
            best_com, best_score = home, home_score
            for com in sorted(weights_to_com):  # 社区键升序:平局取最小
                if com == home:
                    continue
                score = (
                    weights_to_com[com]
                    - 2.0 * resolution * sigma_tot[com] * degree[node] / total_two_m
                )
                gain_evals += 1
                if score > best_score:  # 严格更优才换(平局保持先到者)
                    best_com, best_score = com, score
            community[node] = best_com
            sigma_tot[best_com] += degree[node]
            if best_com != home:
                moved += 1
                sweep_gain += (best_score - home_score) / total_two_m
        total_moves += moved
        if moved == 0 or sweep_gain < threshold:
            break
    if stats is not None:
        stats["sweeps"] = stats.get("sweeps", 0) + sweeps
        stats["moves"] = stats.get("moves", 0) + total_moves
        stats["gain_evals"] = stats.get("gain_evals", 0) + gain_evals
    return _canonical_labels(nodes, community)


def louvain_communities(
    adjacency: dict[str, dict[str, float]],
    *,
    resolution: float = 1.0,
    max_levels: int = 32,
    max_sweeps: int = 100,
    threshold: float = 1e-7,
    stats: dict[str, int] | None = None,
) -> dict[str, int]:
    """Louvain 多层包装:重复"单层局部移动 + 聚合"至划分稳定(纯函数)。

    每层调用一次 :func:`louvain_pass`;随后把每个社区折叠为一个超节点
    (社区内部边权求和折叠为超节点自环,跨社区边权求和折叠为超节点间
    边),在聚合图上重复局部移动——这是 Louvain 第二阶段(community
    aggregation)的标准做法,模块度口径逐层等价。收敛条件(任一):

    1. 本层未发生任何合并(社区数 == 超节点数 → 划分已稳定);
    2. 全部节点归一社区(局部移动只并社区不拆分,继续无意义);
    3. 达到 ``max_levels`` 层上限。

    各层社区标签最终映射回**原始节点**,经 :func:`_canonical_labels`
    重标 0..k-1(确定性);``stats`` 在各层间累加,另计 ``levels`` 层数。

    :param adjacency: 同 :func:`louvain_pass`(单方向给出即可)。
    :param max_levels: 聚合层数上限(>= 1)。
    :return: ``{原始节点: 社区标签}``;空图 → 空 dict。
    """
    nodes, edges, loops = _normalize_weighted_graph(adjacency)
    max_levels = int(max_levels)
    if max_levels < 1:
        raise ValueError(f"max_levels 无效:须为 >= 1 的整数,得到 {max_levels!r}")

    # 折叠映射:原始节点 → 当前层超节点(初始为自身,逐层随聚合更新)。
    fold: dict[str, object] = {node: node for node in nodes}
    level_nodes: list[object] = list(nodes)
    level_edges: dict[tuple[object, object], float] = dict(edges)
    level_loops: dict[object, float] = dict(loops)
    levels = 0
    while True:
        levels += 1
        part = louvain_pass(
            _materialize_adjacency(level_nodes, level_edges, level_loops),
            resolution=resolution,
            max_sweeps=max_sweeps,
            threshold=threshold,
            stats=stats,
        )
        # 先把本层划分折叠进映射(所有出口一致,含 max_levels 截停)。
        fold = {orig: part[fold[orig]] for orig in fold}
        supernode_count = len(set(part.values()))
        if (
            levels >= max_levels
            or supernode_count == len(level_nodes)  # 本层零合并:已稳定
            or supernode_count == 1  # 全归一:后续层无法再并
        ):
            break
        # 聚合:社区标签 → 超节点;内边折叠为自环、跨边求和;无边的社区
        # 标签同样保留为(孤立)超节点,保证下一层划分覆盖全部节点。
        new_edges: dict[tuple[object, object], float] = {}
        new_loops: dict[object, float] = {}
        for (a, b), weight in level_edges.items():
            la, lb = part[a], part[b]
            if la == lb:
                new_loops[la] = new_loops.get(la, 0.0) + weight
            else:
                key = (la, lb) if la <= lb else (lb, la)
                new_edges[key] = new_edges.get(key, 0.0) + weight
        for node, weight in level_loops.items():
            label = part[node]
            new_loops[label] = new_loops.get(label, 0.0) + weight
        level_nodes = sorted(set(part.values()))
        level_edges = new_edges
        level_loops = new_loops
    if stats is not None:
        stats["levels"] = stats.get("levels", 0) + levels
    # 映射回原始节点并按最小成员重标(确定性);折叠键为本层社区标签。
    return _canonical_labels(nodes, {node: fold[node] for node in nodes})


def louvain_modularity(
    adjacency: dict[str, dict[str, float]],
    partition: dict[str, object],
    *,
    resolution: float = 1.0,
) -> float:
    """计算划分的加权模块度 Q(纯函数;供单调性自证与调参消费)。

    Q = Σ_c [ Σ_in,c / 2m − resolution · (Σ_tot,c / 2m)² ];自环计入
    Σ_in 一次、计入度两次;``partition`` 未覆盖的节点按单点社区处理;
    2m = 0(全孤立 / 零权图)时 Q ≡ 0.0。
    """
    nodes, edges, loops = _normalize_weighted_graph(adjacency)
    resolution = float(resolution)
    if not math.isfinite(resolution) or resolution <= 0.0:
        raise ValueError(f"resolution 无效:须为正有限数,得到 {resolution!r}")

    neighbor_of: dict[str, dict[str, float]] = {node: {} for node in nodes}
    for (a, b), weight in edges.items():
        neighbor_of[a][b] = weight
        neighbor_of[b][a] = weight
    degree = {
        node: sum(neighbor_of[node].values()) + 2.0 * loops.get(node, 0.0)
        for node in nodes
    }
    total_two_m = sum(degree.values())
    if total_two_m <= 0.0:
        return 0.0

    label_of = {
        node: partition.get(node, node) for node in nodes  # 未覆盖节点自成单点
    }
    sigma_in: dict[object, float] = {}
    sigma_tot: dict[object, float] = {}
    for node in nodes:
        label = label_of[node]
        sigma_tot[label] = sigma_tot.get(label, 0.0) + degree[node]
    for (a, b), weight in edges.items():
        if label_of[a] == label_of[b]:
            label = label_of[a]
            sigma_in[label] = sigma_in.get(label, 0.0) + weight
    for node, weight in loops.items():
        label = label_of[node]
        sigma_in[label] = sigma_in.get(label, 0.0) + weight

    quality = 0.0
    for label in sorted(sigma_tot, key=repr):  # 确定性求和顺序
        internal = sigma_in.get(label, 0.0)
        quality += internal / total_two_m - resolution * (
            sigma_tot[label] / total_two_m
        ) ** 2
    return quality


def kernel_selfcheck() -> dict[str, object]:
    """A138 内核基准总控自检:确定性微基准(红线 31:操作计数,非墙钟)。

    场景一(并查集,主键口径与 A128 完全一致):
    2×10^3 条 seeded 随机边灌入 → 一次 ``components()``(顺带全量
    路径压缩)→ 2×10^3 次 ``connected`` 查询。``value`` 为查询阶段的
    ``find_hops`` 总数(路径压缩后每次 connected 至多 2 跳);``baseline``
    为朴素口径——每次查询全表 BFS 扫描全部 n 条边,即 n×n 次边扫描。

    场景二(A194 加权社区检测,**附加键 ``louvain``**,主键四项不变):
    双 8-团(内边权 1.0)+ 2 条 0.2 弱桥 + 2 个孤立点的确定性加权图,
    ``louvain_communities`` 必须切出"团 A / 团 B / 两个单点"四社区,
    且模块度严格优于全单点划分;``moves`` / ``gain_evals`` / ``levels``
    为操作计数(红线 31)。两次调用输出完全一致(离线可复现)。
    """
    rng = random.Random(1_281_282)
    n = 2_000
    edges = [(f"h{i}", f"h{rng.randrange(n)}") for i in range(n)]
    queries = [
        (f"h{rng.randrange(n)}", f"h{rng.randrange(n)}") for _ in range(n)
    ]

    kernel = UnionFindKernel()
    kernel.ingest_edges(edges)
    kernel.components()  # 物化分量 + 全量压缩,一次重算
    start = kernel.find_hops
    for a, b in queries:
        kernel.connected(a, b)

    # ---- A194:双团 + 弱桥 + 孤立点的确定性社区检测微基准 ----
    adjacency: dict[str, dict[str, float]] = {f"s{i}": {} for i in range(8)}
    adjacency.update({f"t{i}": {} for i in range(8)})
    adjacency["u0"] = {}
    adjacency["u1"] = {}
    clique_a = [f"s{i}" for i in range(8)]
    clique_b = [f"t{i}" for i in range(8)]
    for members in (clique_a, clique_b):
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                adjacency[members[i]][members[j]] = 1.0  # 单方向给出即可
    adjacency["s7"]["t0"] = 0.2  # 团间弱桥:不足以把两团误并
    adjacency["s6"]["t1"] = 0.2

    louvain_stats: dict[str, int] = {}
    partition = louvain_communities(adjacency, stats=louvain_stats)
    labels_a = {partition[node] for node in clique_a}
    labels_b = {partition[node] for node in clique_b}
    assert len(labels_a) == 1 and len(labels_b) == 1, "每个 8-团应各自归一社区(自查)"
    assert labels_a != labels_b, "弱桥不得把两个独立团误并为同一社区(自查)"
    assert partition["u0"] != partition["u1"], "孤立点互不相干(自查)"
    community_count = len(set(partition.values()))
    assert community_count == 4, f"应为 4 个社区(团A/团B/两单点),得到 {community_count}"
    quality = louvain_modularity(adjacency, partition)
    singleton_quality = louvain_modularity(
        adjacency, {node: idx for idx, node in enumerate(sorted(adjacency))}
    )
    assert quality > singleton_quality, "社区划分的模块度必须优于全单点(自查)"

    return {
        "name": "graph_kernel.union_find",
        "metric": (
            f"{n} 次 connected 查询的父指针跳转总数"
            f"(路径压缩后;朴素口径 = 每查询全表扫描 {n} 条边)"
        ),
        "value": kernel.find_hops - start,
        "baseline": n * n,
        # A194 附加键(旧四键口径不变;A138 总控将其收入 extra 留档):
        # 操作计数 moves / gain_evals / levels + 结构断言数值。
        "louvain": {
            "nodes": len(adjacency),
            "communities": community_count,
            "levels": int(louvain_stats.get("levels", 0)),
            "moves": int(louvain_stats.get("moves", 0)),
            "gain_evals": int(louvain_stats.get("gain_evals", 0)),
            "modularity": round(quality, 6),
            "singleton_modularity": round(singleton_quality, 6),
        },
    }
