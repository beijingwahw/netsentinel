"""缓存内核·双层记忆(V7 · A132)。

进程内 LRU 结果缓存,包装**任意**既有 :class:`~netsentinel.vision.classifier_base.NsfwClassifier`
(stub / nudenet / clip / glm / ensemble 均可),使同一张图的重复评分
**零内层调用**(见 CONTRACTS-V7.md §2 A132 行):

- :class:`MemoLRU`:``OrderedDict`` 实现的线程安全 LRU,自带命中/未命中计数与
  :meth:`MemoLRU.hit_stats` 命中率统计;``capacity <= 0`` 时退化为**直通**
  (put 为无操作、get 永远 miss,即完全不缓存);
- :class:`CachedClassifier`:缓存装饰器。缓存键 = ``img.sha256``(缺失时回退
  ``"path#" + sha256(路径)``,与既有 vlm_cache 的"同图同分"语义一致);
  命中直接返回先前存下的 :class:`~netsentinel.contracts.ImageScore`(原对象),
  未命中才调用 ``inner.classify`` 并把结果 put 进 LRU;
  ``classify_batch`` 不覆写——复用基类逐张循环,天然"每张独立缓存"。

遥测:命中/未命中分别累计 ``cache2.hit`` / ``cache2.miss``(与 vlm_cache 的
``vlm_cache.hit`` 计数体系并存,互不干扰)。

红线遵守(V7 §0):

- **红线 29(零 API 破坏)**:本模块为纯新增文件,不向 classifier_base 注册表
  注册任何名字、不修改任何既有模块;``CachedClassifier`` 只能显式构造包装,
  默认路径(工厂/编排器)行为与 V6 完全一致;
- **红线 31(基准可复现)**:``tests/test_cache2.py`` 的
  ``test_v7_bench_repeat_classify_inner_calls_5_to_1`` 以**内层调用计数**断言
  同图重复 classify 5 次 → 内层恰调用 1 次,不依赖墙钟。

线程安全:``MemoLRU`` 全操作持 ``threading.Lock``(get 的"查 + move_to_end +
计数"与 put 的"写 + 淘汰"均为锁内原子步骤);``CachedClassifier`` 自身无
共享可变态(全部委托给 LRU 与内层分类器)。

仅使用标准库;离线运行,不发起任何网络请求。

用法示例::

    from netsentinel.vision.cache2 import CachedClassifier, MemoLRU
    from netsentinel.vision.stub_classifier import StubClassifier

    clf = CachedClassifier(StubClassifier(cfg), lru=MemoLRU(256))
    s1 = clf.classify(img)   # miss → 内层打分并缓存
    s2 = clf.classify(img)   # hit  → 零内层调用,直接返回缓存结果
    clf.lru.hit_stats()      # {"hits": 1, "misses": 1, "hit_rate": 0.5}
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence, ImageScore
from netsentinel.vision.classifier_base import NsfwClassifier

__all__ = ["MemoLRU", "CachedClassifier"]


class MemoLRU:
    """线程安全 LRU 缓存(``OrderedDict`` 实现,容量 0 = 直通)。

    语义要点:

    - ``get`` 命中会把键移到"最新"端(``move_to_end``),即**读也刷新新鲜度**;
    - ``put`` 超容量时从"最旧"端淘汰(``popitem(last=False)``),同键重写不增长;
    - ``hit_stats`` 返回 ``{"hits", "misses", "hit_rate"}``;零查询时
      ``hit_rate`` 为 ``0.0``(而非除零);
    - ``capacity <= 0``:put 为无操作、get 永远未命中 → **直通不缓存**
      (供调用方一键关闭缓存,回到 V6 无缓存行为)。
    """

    def __init__(self, capacity: int = 256) -> None:
        self.capacity = max(0, int(capacity))
        self._lock = threading.Lock()
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: Any) -> Any | None:
        """取值:命中返回缓存值并刷新新鲜度,未命中返回 ``None``。

        两种情况均计数(命中/未命中),供 :meth:`hit_stats` 汇总。
        """
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._hits += 1
                return self._data[key]
            self._misses += 1
            return None

    def put(self, key: Any, value: Any) -> None:
        """写入/覆盖一个键值对;超过容量时淘汰最旧键;直通模式下为无操作。"""
        if self.capacity <= 0:
            return
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def hit_stats(self) -> dict[str, Any]:
        """命中率统计:``{"hits": int, "misses": int, "hit_rate": float}``。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total else 0.0,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class CachedClassifier(NsfwClassifier):
    """缓存装饰器:命中零内层调用,未命中透传内层结果并写入缓存。

    - ``name = f"cached({inner.name})"``(如 ``cached(stub)``、``cached(glm)``),
      便于日志/报告直接看出"谁被缓存了";
    - 缓存的 :class:`ImageScore` 原样返回(含 ``model`` 字段仍为内层名,
      即 **model 字段透传**——下游 ensemble/仲裁看到的是真实评分来源);
    - ``classify_batch`` 继承基类逐张循环:每张独立查/写缓存,
      同批重复图与跨批重复图同样受益。
    """

    def __init__(self, inner: NsfwClassifier, lru: MemoLRU | None = None) -> None:
        self.inner = inner
        self.lru = lru if lru is not None else MemoLRU(256)
        self.name = f"cached({inner.name})"

    @staticmethod
    def _cache_key(img: ImageEvidence) -> str:
        """缓存键:``img.sha256`` 优先;缺失时回退路径哈希(``"path#"`` 前缀)。"""
        if img.sha256:
            return img.sha256
        return "path#" + hashlib.sha256(img.path.encode("utf-8")).hexdigest()

    def classify(self, img: ImageEvidence) -> ImageScore:
        """单图打分:先查缓存,命中直接返回;未命中调用内层并缓存其结果。"""
        key = self._cache_key(img)
        cached = self.lru.get(key)
        if cached is not None:
            telemetry.inc("cache2.hit")
            return cached
        telemetry.inc("cache2.miss")
        score = self.inner.classify(img)
        self.lru.put(key, score)
        return score


def kernel_selfcheck() -> dict:
    """V7 内核自检(A138 总控):同图 5 次分类,内层仅 1 次调用(计数替身)。"""
    from netsentinel.contracts import Config, ImageEvidence

    calls = {"n": 0}

    class _Inner(NsfwClassifier):
        name = "inner"

        def classify(self, img):
            calls["n"] += 1
            from netsentinel.contracts import ImageScore

            return ImageScore(image=img, model=self.name, scores={}, nsfw_prob=0.5)

    img = ImageEvidence(path="x.png", url="http://x/x.png", source_page="http://x/", sha256="a" * 64)
    cached = CachedClassifier(_Inner(), MemoLRU(8))
    for _ in range(5):
        cached.classify(img)
    return {
        "name": "cache2",
        "metric": "inner_calls_for_5_same_images",
        "value": calls["n"],
        "baseline": 5,
        "extra": {"hit_rate": cached.lru.hit_stats()["hit_rate"]},
        "pass_": calls["n"] == 1,
    }
