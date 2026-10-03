"""缓存内核·双层记忆测试(V7 · A132;见 CONTRACTS-V7.md §2 / 红线 29、31)。

覆盖::

- ``MemoLRU``:put/get 往返、LRU 淘汰序(容量 3,第 4 个挤掉最旧)、
  读刷新新鲜度、同键重写不增长、命中率统计(含零查询)、容量 0/负数直通、
  多线程并发不变量(无异常、计数守恒、长度不越界);
- ``CachedClassifier``:name 包装、与真 StubClassifier 组合的 name/model 透传、
  键取 sha256 优先/路径哈希回退、不同图各自 miss、batch 每张独立缓存与顺序、
  共享 LRU 跨包装实例、容量 0 直通、cache2.hit/miss 遥测;
- **红线 31 基准**::func:`test_v7_bench_repeat_classify_inner_calls_5_to_1`
  以内层调用计数断言同图重复 classify 5 次 → 内层恰调用 1 次(操作计数,
  不依赖墙钟,离线可复现)。

全部测试离线,不发起网络请求。
"""
from __future__ import annotations

import threading

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence, ImageScore
from netsentinel.vision.cache2 import CachedClassifier, MemoLRU
from netsentinel.vision.classifier_base import NsfwClassifier
from netsentinel.vision.stub_classifier import StubClassifier

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


def make_img(idx: int = 0, *, sha: str = "", path: str = "") -> ImageEvidence:
    """构造一张受检图:缺省 64 位十六进制 sha256 互不相同,便于"不同图"场景。"""
    p = path or f"data/pic_{idx}.jpg"
    return ImageEvidence(
        path=p,
        url=f"https://example.invalid/{p}",
        source_page="https://example.invalid/index.html",
        sha256=sha if sha is not None and sha != "" else f"{idx:064x}",
        width=64,
        height=64,
    )


class CountingStub(NsfwClassifier):
    """计数桩分类器:每次 classify 自增 calls,返回按路径确定的分数。"""

    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def classify(self, img: ImageEvidence) -> ImageScore:
        self.calls += 1
        prob = (abs(hash(img.path)) % 100) / 100.0
        return ImageScore(
            image=img,
            model=self.name,
            scores={"call_seq": self.calls},
            nsfw_prob=prob,
        )


def _counter(name: str) -> float:
    """基线相对读取遥测计数(与 test_stub_classifier 的做法一致,不 reset 全局)。"""
    return telemetry.snapshot()["counters"].get(name, 0.0)


# ---------------------------------------------------------------------------
# MemoLRU:基础语义与淘汰序
# ---------------------------------------------------------------------------


def test_memo_lru_put_get_roundtrip() -> None:
    """put 后 get 命中并返回原值;值可为任意对象。"""
    lru = MemoLRU(8)
    sentinel = object()
    lru.put("a", 1)
    lru.put("b", sentinel)
    assert lru.get("a") == 1
    assert lru.get("b") is sentinel
    assert len(lru) == 2


def test_memo_lru_unknown_key_returns_none_and_counts_miss() -> None:
    """未写入的键返回 None 并计入 miss。"""
    lru = MemoLRU(8)
    lru.put("a", 1)
    assert lru.get("nope") is None
    stats = lru.hit_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1


def test_memo_lru_eviction_capacity_three() -> None:
    """LRU 淘汰序:容量 3,第 4 个键写入挤掉最旧的键,其余保留。"""
    lru = MemoLRU(3)
    lru.put("a", 1)
    lru.put("b", 2)
    lru.put("c", 3)
    assert len(lru) == 3
    lru.put("d", 4)  # a 最旧 → 被淘汰
    assert len(lru) == 3
    assert lru.get("a") is None
    assert lru.get("b") == 2
    assert lru.get("c") == 3
    assert lru.get("d") == 4


def test_memo_lru_get_refreshes_recency() -> None:
    """get 命中会刷新新鲜度:被访问过的键免于下一轮淘汰。"""
    lru = MemoLRU(2)
    lru.put("a", 1)
    lru.put("b", 2)
    assert lru.get("a") == 1  # a 移到最新端,b 变为最旧
    lru.put("c", 3)  # 淘汰 b
    assert lru.get("b") is None
    assert lru.get("a") == 1
    assert lru.get("c") == 3


def test_memo_lru_reput_same_key_no_growth() -> None:
    """同键重写:长度不增长,新值覆盖旧值,且不触发误淘汰。"""
    lru = MemoLRU(2)
    lru.put("a", 1)
    lru.put("a", 99)
    assert len(lru) == 1
    assert lru.get("a") == 99
    lru.put("b", 2)  # 若误淘汰将挤掉 a
    assert lru.get("a") == 99
    assert lru.get("b") == 2


# ---------------------------------------------------------------------------
# MemoLRU:命中率统计
# ---------------------------------------------------------------------------


def test_hit_stats_counts_and_rate() -> None:
    """2 命中 + 1 未命中 → hit_rate = 2/3。"""
    lru = MemoLRU(4)
    lru.put("a", 1)
    lru.get("a")  # hit
    lru.get("a")  # hit
    lru.get("zzz")  # miss
    stats = lru.hit_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 2 / 3


def test_hit_stats_zero_queries_rate_is_zero() -> None:
    """零查询时 hit_rate 为 0.0,不得除零。"""
    stats = MemoLRU(4).hit_stats()
    assert stats == {"hits": 0, "misses": 0, "hit_rate": 0.0}


def test_hit_stats_all_misses_rate_zero() -> None:
    """全部未命中时 hit_rate 为 0.0。"""
    lru = MemoLRU(2)
    lru.get("x")
    lru.get("y")
    stats = lru.hit_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 2
    assert stats["hit_rate"] == 0.0


# ---------------------------------------------------------------------------
# MemoLRU:容量 0(与负数)直通
# ---------------------------------------------------------------------------


def test_capacity_zero_is_passthrough() -> None:
    """容量 0:put 为无操作,get 永远未命中(完全不缓存)。"""
    lru = MemoLRU(0)
    lru.put("a", 1)
    lru.put("b", 2)
    assert len(lru) == 0
    assert lru.get("a") is None
    stats = lru.hit_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1


def test_negative_capacity_normalized_to_passthrough() -> None:
    """负容量归一为 0,同样直通。"""
    lru = MemoLRU(-5)
    assert lru.capacity == 0
    lru.put("a", 1)
    assert lru.get("a") is None
    assert len(lru) == 0


# ---------------------------------------------------------------------------
# MemoLRU:线程安全
# ---------------------------------------------------------------------------


def test_memo_lru_thread_safety_invariants() -> None:
    """并发 get/put:无异常、命中+未命中 == get 总次数、长度恒不越界。"""
    capacity = 16
    lru = MemoLRU(capacity)
    workers, iters = 8, 200
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def work(w: int) -> None:
        try:
            barrier.wait()
            for i in range(iters):
                key = f"k{(w * iters + i) % 20}"
                lru.put(key, (w, i))
                got = lru.get(f"k{i % 20}")
                if got is not None and not isinstance(got, tuple):
                    errors.append(AssertionError(f"脏值:{got!r}"))
                if len(lru) > capacity:
                    errors.append(AssertionError("长度越界"))
        except BaseException as exc:  # noqa: BLE001 — 收集任何线程内故障
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    stats = lru.hit_stats()
    total_gets = workers * iters
    assert stats["hits"] + stats["misses"] == total_gets
    assert stats["hits"] >= 0 and stats["misses"] >= 0
    assert len(lru) <= capacity


# ---------------------------------------------------------------------------
# CachedClassifier:构造、键与透传
# ---------------------------------------------------------------------------


def test_cached_classifier_name_and_defaults() -> None:
    """name = cached(inner.name);缺省 lru 为 MemoLRU(256);是 NsfwClassifier 子类。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    assert clf.name == "cached(counting)"
    assert clf.inner is inner
    assert isinstance(clf.lru, MemoLRU)
    assert clf.lru.capacity == 256
    assert isinstance(clf, NsfwClassifier)


def test_cached_classifier_with_real_stub_name_model_passthrough() -> None:
    """与真 StubClassifier 组合:name 包装为 cached(stub);model 字段透传为 stub。"""
    clf = CachedClassifier(StubClassifier(Config()))
    assert clf.name == "cached(stub)"
    img = make_img(path="data/x_nsfw_hi.jpg")
    s1 = clf.classify(img)
    assert isinstance(s1, ImageScore)
    assert s1.model == "stub"  # model 透传:下游看到的真实评分来源
    assert s1.nsfw_prob == 0.97
    assert s1.scores["hint"] == "nsfw_hi"
    # 第二次(命中)返回同一评分对象,model 仍为 stub
    s2 = clf.classify(img)
    assert s2 is s1
    assert s2.model == "stub" and s2.nsfw_prob == 0.97


def test_key_prefers_sha256_over_path() -> None:
    """sha256 优先:路径不同但内容(sha)相同 → 视为同一图,内层仅调用一次。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    a = make_img(path="data/first_copy.jpg", sha="f" * 64)
    b = make_img(path="data/second_copy.jpg", sha="f" * 64)
    clf.classify(a)
    clf.classify(b)
    assert inner.calls == 1


def test_key_falls_back_to_path_hash_when_no_sha() -> None:
    """无 sha256 时回退路径哈希:同路径(不同 evidence 对象)→ 内层仅调用一次。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    a = ImageEvidence(
        path="data/no_sha.jpg",
        url="https://example.invalid/no_sha.jpg",
        source_page="https://example.invalid/",
    )
    b = ImageEvidence(
        path="data/no_sha.jpg",
        url="https://other.example/no_sha.jpg",  # url/source_page 不参与键
        source_page="https://other.example/",
    )
    clf.classify(a)
    clf.classify(b)
    assert inner.calls == 1
    # 路径不同则键不同
    clf.classify(
        ImageEvidence(
            path="data/other.jpg",
            url="https://example.invalid/other.jpg",
            source_page="https://example.invalid/",
        )
    )
    assert inner.calls == 2


# ---------------------------------------------------------------------------
# CachedClassifier:红线 31 基准(操作计数,离线可复现)
# ---------------------------------------------------------------------------


def test_v7_bench_repeat_classify_inner_calls_5_to_1() -> None:
    """红线 31 基准:同图重复 classify 5 次,内层调用计数恰为 1(5 → 1)。

    以**内层调用计数**(操作计数)断言代差,不依赖墙钟;命中返回缓存的原
    ImageScore 对象(对象同一性),证明命中路径零内层计算。
    """
    inner = CountingStub()
    clf = CachedClassifier(inner)
    img = make_img(0)
    results = [clf.classify(img) for _ in range(5)]
    assert inner.calls == 1  # 5 次评分,内层只算 1 次
    assert all(r is results[0] for r in results)  # 命中返回同一缓存对象
    assert all(r.nsfw_prob == results[0].nsfw_prob for r in results)
    stats = clf.lru.hit_stats()
    assert stats == {"hits": 4, "misses": 1, "hit_rate": 0.8}


def test_distinct_images_each_miss_then_all_hit() -> None:
    """不同图各自 miss:4 张图首轮 4 次内层调用;重放轮全部命中、计数不增。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    imgs = [make_img(i) for i in range(4)]
    first = [clf.classify(img) for img in imgs]
    assert inner.calls == 4
    second = [clf.classify(img) for img in imgs]
    assert inner.calls == 4  # 全命中,零新增
    assert first == second
    stats = clf.lru.hit_stats()
    assert stats["hits"] == 4 and stats["misses"] == 4
    assert stats["hit_rate"] == 0.5


# ---------------------------------------------------------------------------
# CachedClassifier:batch / 共享 LRU / 直通 / 遥测
# ---------------------------------------------------------------------------


def test_classify_batch_each_image_cached_independently() -> None:
    """classify_batch 复用基类循环:批内重复图第二次走缓存,批间全命中。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    a, b = make_img(1), make_img(2)
    batch = clf.classify_batch([a, b, a])  # 批内 a 出现两次
    assert inner.calls == 2  # a 首次 miss、b miss、a 第二次命中
    assert [s.image.path for s in batch] == [a.path, b.path, a.path]  # 顺序保持
    assert batch[0] is batch[2]  # 同图命中返回同一缓存对象
    # 整批重放:全部命中
    again = clf.classify_batch([a, b, a])
    assert inner.calls == 2
    assert again == batch


def test_classify_batch_matches_single_results() -> None:
    """batch 结果与逐张 classify 逐项一致(顺序与数值)。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    imgs = [make_img(i) for i in range(5)]
    batch = clf.classify_batch(imgs)
    singles = [clf.classify(img) for img in imgs]
    assert batch == singles
    assert [s.nsfw_prob for s in batch] == [s.nsfw_prob for s in singles]
    assert inner.calls == 5


def test_shared_lru_reused_across_wrapper_instances() -> None:
    """注入同一 MemoLRU:两个 CachedClassifier 共享缓存(换内层也零调用)。"""
    shared = MemoLRU(64)
    inner1, inner2 = CountingStub(), CountingStub()
    clf1, clf2 = CachedClassifier(inner1, lru=shared), CachedClassifier(inner2, lru=shared)
    img = make_img(7)
    clf1.classify(img)
    assert inner1.calls == 1
    clf2.classify(img)  # 命中共享缓存 → inner2 零调用
    assert inner2.calls == 0
    assert shared.hit_stats() == {"hits": 1, "misses": 1, "hit_rate": 0.5}


def test_cached_classifier_capacity_zero_passthrough() -> None:
    """容量 0 的包装器:直通不缓存,每次都真实调用内层并计 miss。"""
    inner = CountingStub()
    clf = CachedClassifier(inner, lru=MemoLRU(0))
    img = make_img(3)
    for _ in range(5):
        clf.classify(img)
    assert inner.calls == 5  # 直通:5 次评分 5 次内层调用
    stats = clf.lru.hit_stats()
    assert stats["hits"] == 0 and stats["misses"] == 5


def test_telemetry_cache2_hit_miss_counters() -> None:
    """遥测:未命中累计 cache2.miss,命中累计 cache2.hit(基线相对)。"""
    before_hit = _counter("cache2.hit")
    before_miss = _counter("cache2.miss")
    inner = CountingStub()
    clf = CachedClassifier(inner)
    img = make_img(9)
    clf.classify(img)  # miss
    clf.classify(img)  # hit
    assert _counter("cache2.miss") == before_miss + 1
    assert _counter("cache2.hit") == before_hit + 1


def test_telemetry_counters_across_batch() -> None:
    """batch 场景遥测:首批 2 miss + 1 hit,重放批 3 hit。"""
    before_hit = _counter("cache2.hit")
    before_miss = _counter("cache2.miss")
    clf = CachedClassifier(CountingStub())
    a, b = make_img(10), make_img(11)
    clf.classify_batch([a, b, a])
    clf.classify_batch([a, b, a])
    assert _counter("cache2.miss") == before_miss + 2
    assert _counter("cache2.hit") == before_hit + 4


def test_cached_classifier_concurrent_classify_safe() -> None:
    """并发 classify:无异常、结果一致;内层调用数被缓存收敛到 [1, N](竞态上界)。"""
    inner = CountingStub()
    clf = CachedClassifier(inner)
    img = make_img(12)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[ImageScore] = [None] * workers  # type: ignore[list-item]
    errors: list[BaseException] = []

    def work(w: int) -> None:
        try:
            barrier.wait()
            results[w] = clf.classify(img)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert 1 <= inner.calls <= workers  # 至少收敛 1 次真实调用,至多竞态各自 miss
    probs = {r.nsfw_prob for r in results}
    assert len(probs) == 1  # 所有线程拿到同一评分
    stats = clf.lru.hit_stats()
    assert stats["hits"] + stats["misses"] == workers  # 计数守恒
