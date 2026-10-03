"""A165 三档并发持久状态与会话一次性自动建议(netsentinel.ops.tier_state)单元测试。

全部离线:零网络、零真实 CPU 探测依赖(注入 fake detector)、零 repo 内
文件副作用(持久档一律指向 ``tmp_path``;默认路径用例经 ``monkeypatch.chdir``
隔离)。并发原子性用 8 写线程 + 4 读线程压测(无 torn 记录断言)。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.ops import tier_state
from netsentinel.ops.tier_state import (
    TIERS,
    TierState,
    resolve_tier,
    tier_once,
)


# ---------------------------------------------------------------------------
# 公共夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_session():
    """每个用例独立会话:清幂等标记与遥测(结束后同样清,防污染兄弟用例)。"""
    tier_state._reset_once_guard()
    telemetry.reset()
    yield
    tier_state._reset_once_guard()
    telemetry.reset()


class FakeDetector:
    """鸭子等价于 A163 ops.cpu_profile 的离线探测器(公式与 §1 逐字一致)。"""

    def __init__(self, cores: int = 16, tier: str | None = None) -> None:
        self.cores = cores
        self._tier = tier or ("high" if cores > 8 else ("mid" if cores > 2 else "low"))
        self.detect_calls = 0
        self.recommend_calls = 0
        self.workers_calls = 0
        self.reserve_seen: int | None = None

    def detect(self) -> dict:
        self.detect_calls += 1
        return {
            "cores": self.cores,
            "arch": "AMD64",
            "platform": "Windows/win32",
            "psutil": False,
        }

    def recommend(self, profile: dict) -> str:
        self.recommend_calls += 1
        return self._tier

    def tier_workers(self, tier: str, *, reserve: int = 1, cores: int | None = None) -> int:
        self.workers_calls += 1
        self.reserve_seen = reserve
        n = cores if isinstance(cores, int) and cores >= 1 else 2
        if tier == "low":
            return max(1, n // 4)
        if tier == "mid":
            return max(1, n // 2)
        return max(1, n - max(0, int(reserve)))


def _make_cfg(**kw) -> Config:
    kw.setdefault("audit_path", "")  # 缺省关闭审计文件,避免误写 repo data/
    return Config(**kw)


# ---------------------------------------------------------------------------
# TierState:set/get 往返与校验
# ---------------------------------------------------------------------------
class TestTierStateSetGet:
    def test_set_get_roundtrip_exact_four_keys(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        returned = st.set("high", workers=15, cores=16, set_by="user")
        assert returned == {"tier": "high", "workers": 15, "cores": 16, "set_by": "user"}
        loaded = st.get()
        assert loaded == {"tier": "high", "workers": 15, "cores": 16, "set_by": "user"}
        assert set(loaded) == {"tier", "workers", "cores", "set_by"}  # 恰好四键
        # 文件本体也是可读 JSON(展示用途)
        data = json.loads((tmp_path / "concurrency.json").read_text(encoding="utf-8"))
        assert data["tier"] == "high"

    def test_set_creates_nested_parent_dirs(self, tmp_path) -> None:
        st = TierState(tmp_path / "a" / "b" / "concurrency.json")
        st.set("low", workers=1, cores=4, set_by="user")
        assert st.get() is not None

    @pytest.mark.parametrize("bad", ["ultra", "MID", "", None, 3])
    def test_set_rejects_invalid_tier(self, tmp_path, bad) -> None:
        st = TierState(tmp_path / "concurrency.json")
        with pytest.raises(ValueError, match="low / mid / high"):
            st.set(bad, workers=2, cores=4, set_by="user")  # type: ignore[arg-type]
        assert st.get() is None  # 拒绝写:文件不落地

    @pytest.mark.parametrize("bad", [0, -1, "4", True, 1.5])
    def test_set_rejects_invalid_workers(self, tmp_path, bad) -> None:
        st = TierState(tmp_path / "concurrency.json")
        with pytest.raises(ValueError, match="workers"):
            st.set("mid", workers=bad, cores=4, set_by="user")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, -2, "8", False])
    def test_set_rejects_invalid_cores(self, tmp_path, bad) -> None:
        st = TierState(tmp_path / "concurrency.json")
        with pytest.raises(ValueError, match="cores"):
            st.set("mid", workers=2, cores=bad, set_by="user")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["", "   ", None, 7])
    def test_set_rejects_invalid_set_by(self, tmp_path, bad) -> None:
        st = TierState(tmp_path / "concurrency.json")
        with pytest.raises(ValueError, match="set_by"):
            st.set("mid", workers=2, cores=4, set_by=bad)  # type: ignore[arg-type]

    def test_invalid_set_leaves_existing_file_unchanged(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        st.set("mid", workers=4, cores=8, set_by="user")
        with pytest.raises(ValueError):
            st.set("ultra", workers=4, cores=8, set_by="user")
        assert st.get() == {"tier": "mid", "workers": 4, "cores": 8, "set_by": "user"}

    def test_get_missing_file_returns_none(self, tmp_path) -> None:
        assert TierState(tmp_path / "nope.json").get() is None

    def test_get_corrupt_json_returns_none(self, tmp_path) -> None:
        p = tmp_path / "concurrency.json"
        p.write_text('{"tier": "high", "wor', encoding="utf-8")
        assert TierState(p).get() is None

    def test_get_empty_file_returns_none(self, tmp_path) -> None:
        p = tmp_path / "concurrency.json"
        p.write_text("", encoding="utf-8")
        assert TierState(p).get() is None

    @pytest.mark.parametrize(
        "raw",
        [
            "[]",
            '"low"',
            "3",
            "{}",
            '{"tier": "low", "workers": 1, "cores": 2}',  # 缺 set_by
            '{"tier": "ultra", "workers": 1, "cores": 2, "set_by": "user"}',
            '{"tier": "low", "workers": "1", "cores": 2, "set_by": "user"}',
            '{"tier": "low", "workers": 0, "cores": 2, "set_by": "user"}',
            '{"tier": "low", "workers": 1, "cores": true, "set_by": "user"}',
            '{"tier": "low", "workers": 1, "cores": 2, "set_by": ""}',
        ],
    )
    def test_get_wrong_shape_returns_none(self, tmp_path, raw) -> None:
        p = tmp_path / "concurrency.json"
        p.write_text(raw, encoding="utf-8")
        assert TierState(p).get() is None  # 结构不符视同损坏 → None

    def test_set_overwrites_and_leaves_no_tmp(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        st.set("low", workers=1, cores=4, set_by="user")
        st.set("high", workers=3, cores=4, set_by="cli")
        assert st.get() == {"tier": "high", "workers": 3, "cores": 4, "set_by": "cli"}
        assert list(tmp_path.glob("*.tmp")) == []  # 原子写:无临时文件残留


# ---------------------------------------------------------------------------
# TierState:并发原子性(8 写线程 / 4 读线程)
# ---------------------------------------------------------------------------
class TestTierStateAtomic:
    def test_concurrent_8_threads_sets_all_succeed(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")

        def hammer(i: int) -> int:
            tier = TIERS[i % 3]
            for _ in range(20):
                st.set(tier, workers=i + 1, cores=32, set_by=f"t{i}")
            return i

        with ThreadPoolExecutor(max_workers=8) as pool:
            done = list(pool.map(hammer, range(8)))  # 任何一次 set 抛错则 map 抛出
        assert done == list(range(8))
        final = st.get()
        assert final is not None
        assert final["tier"] in TIERS
        assert final["cores"] == 32
        assert list(tmp_path.glob("*.tmp")) == []  # 全程无残留临时文件

    def test_concurrent_reads_never_see_torn_records(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        stop = threading.Event()
        seen: list = []
        seen_lock = threading.Lock()

        def reader() -> None:
            local = []
            while not stop.is_set():
                local.append(st.get())
                time.sleep(0.002)  # 让出文件句柄与 GIL:采样真实 contention 而非饿死写者
            local.append(st.get())  # 收尾再读一次
            with seen_lock:
                seen.extend(local)

        def hammer(i: int) -> None:
            for _ in range(20):
                st.set(TIERS[i % 3], workers=i + 1, cores=16, set_by=f"t{i}")

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        try:
            for t in readers:
                t.start()
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(hammer, range(8)))
        finally:
            stop.set()  # 写侧无论成败都必须放行读者,防悬挂
        for t in readers:
            t.join(timeout=10)
        assert seen, "读线程应有采样"
        # 原子性:任何时刻读到的要么 None(缺失/瞬时占用)要么完整规范记录
        for rec in seen:
            assert rec is None or (
                set(rec) == {"tier", "workers", "cores", "set_by"}
                and rec["tier"] in TIERS
                and rec["workers"] >= 1
                and rec["cores"] == 16
            )


# ---------------------------------------------------------------------------
# resolve_tier:cfg 恒优先
# ---------------------------------------------------------------------------
class TestResolveTier:
    def test_cfg_explicit_tier_wins_over_persisted(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        st.set("low", workers=1, cores=4, set_by="user")
        assert resolve_tier(_make_cfg(concurrency_tier="high"), state=st) == "high"

    def test_cfg_default_mid_still_wins(self, tmp_path) -> None:
        # "显式 mid"与"默认 mid"无从区分 → 约定 cfg 恒优先(mid 不让位持久 high)
        st = TierState(tmp_path / "concurrency.json")
        st.set("high", workers=15, cores=16, set_by="auto")
        assert resolve_tier(_make_cfg(), state=st) == "mid"  # Config 默认 concurrency_tier="mid"

    def test_invalid_cfg_falls_back_to_persisted(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        st.set("low", workers=1, cores=4, set_by="user")
        cfg = _make_cfg()
        object.__setattr__(cfg, "concurrency_tier", "ultra")  # 绕过配置校验的脏数据
        assert resolve_tier(cfg, state=st) == "low"

    def test_invalid_cfg_without_state_returns_mid(self, tmp_path) -> None:
        cfg = _make_cfg()
        object.__setattr__(cfg, "concurrency_tier", "")
        assert resolve_tier(cfg, state=TierState(tmp_path / "nope.json")) == "mid"

    def test_default_state_path_when_state_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)  # data/concurrency.json 解析进 tmp
        TierState(tier_state.DEFAULT_STATE_PATH).set("low", workers=1, cores=4, set_by="user")
        cfg = _make_cfg()
        object.__setattr__(cfg, "concurrency_tier", "ultra")
        assert resolve_tier(cfg) == "low"  # state=None → 缺省路径


# ---------------------------------------------------------------------------
# tier_once:disabled / 幂等 / kept / auto 四分支
# ---------------------------------------------------------------------------
class TestTierOnce:
    def test_disabled_branch(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        det = FakeDetector(cores=16)
        result = tier_once(_make_cfg(concurrency_auto=False), state=st, detector=det)
        assert result == {"action": "disabled"}
        assert st.get() is None  # 零文件副作用
        assert det.detect_calls == 0
        assert telemetry.snapshot()["counters"].get("tier.once.disabled") == 1

    def test_idempotent_within_session(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        det = FakeDetector(cores=16)
        cfg = _make_cfg(concurrency_auto=True)
        first = tier_once(cfg, state=st, detector=det)
        assert first == {"action": "auto", "tier": "high", "workers": 15}
        first["tier"] = "篡改"  # 返回值是浅拷贝:外部篡改不污染缓存
        second = tier_once(cfg, state=st, detector=det)
        assert second == {"action": "auto", "tier": "high", "workers": 15}
        assert det.detect_calls == 1  # 会话幂等:不再探测
        assert telemetry.snapshot()["counters"].get("tier.once.auto") == 1  # 不重复计数

    def test_kept_branch_does_not_overwrite(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        st.set("mid", workers=4, cores=8, set_by="user")
        det = FakeDetector(cores=16)  # 即便建议 high 也不覆盖既有档
        result = tier_once(_make_cfg(concurrency_auto=True), state=st, detector=det)
        assert result == {"action": "kept", "tier": "mid"}
        assert st.get() == {"tier": "mid", "workers": 4, "cores": 8, "set_by": "user"}
        assert det.detect_calls == 0  # 既有档无需探测
        assert telemetry.snapshot()["counters"].get("tier.once.kept") == 1

    def test_auto_branch_writes_state(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        det = FakeDetector(cores=16)
        result = tier_once(_make_cfg(concurrency_auto=True), state=st, detector=det)
        assert result == {"action": "auto", "tier": "high", "workers": 15}
        assert det.reserve_seen == 1  # 透传 cfg.cpu_reserve 默认值
        assert st.get() == {"tier": "high", "workers": 15, "cores": 16, "set_by": "auto"}
        assert telemetry.snapshot()["counters"].get("tier.once.auto") == 1

    def test_auto_respects_cpu_reserve(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        det = FakeDetector(cores=16)
        result = tier_once(
            _make_cfg(concurrency_auto=True, cpu_reserve=2), state=st, detector=det
        )
        assert result == {"action": "auto", "tier": "high", "workers": 14}  # 16-2
        assert det.reserve_seen == 2

    def test_auto_small_machine_recommends_low(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        det = FakeDetector(cores=2, tier="low")
        result = tier_once(_make_cfg(concurrency_auto=True), state=st, detector=det)
        assert result == {"action": "auto", "tier": "low", "workers": 1}
        assert st.get()["set_by"] == "auto"

    def test_auto_writes_audit_lazily(self, tmp_path) -> None:
        st = TierState(tmp_path / "concurrency.json")
        audit = tmp_path / "audit.jsonl"
        tier_once(
            _make_cfg(concurrency_auto=True, audit_path=str(audit)),
            state=st,
            detector=FakeDetector(cores=16),
        )
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "tier_once"
        assert rec["action"] == "auto"
        assert rec["tier"] == "high"
        assert rec["workers"] == 15
        assert rec["cores"] == 16
        assert rec["set_by"] == "auto"

    def test_no_audit_file_for_disabled_and_kept(self, tmp_path) -> None:
        audit = tmp_path / "audit.jsonl"
        st = TierState(tmp_path / "concurrency.json")
        tier_once(
            _make_cfg(concurrency_auto=False, audit_path=str(audit)), state=st,
            detector=FakeDetector(cores=16),
        )
        assert not audit.exists()  # 审计惰性:disabled 无状态变更不落审计
        tier_state._reset_once_guard()  # 换分支重入会话
        st.set("low", workers=1, cores=4, set_by="user")
        tier_once(
            _make_cfg(concurrency_auto=True, audit_path=str(audit)), state=st,
            detector=FakeDetector(cores=16),
        )
        assert not audit.exists()  # kept 同样零审计

    def test_auto_lazy_imports_cpu_profile_when_detector_none(self, tmp_path, monkeypatch) -> None:
        from netsentinel.ops import cpu_profile

        st = TierState(tmp_path / "concurrency.json")
        monkeypatch.setattr(
            cpu_profile, "detect", lambda: {"cores": 16, "arch": "AMD64",
                                            "platform": "Windows/win32", "psutil": False}
        )
        result = tier_once(_make_cfg(concurrency_auto=True), state=st, detector=None)
        # 真实 recommend(cores=16>8 且无占用采样→high)+ 真实 tier_workers(§1 公式)
        assert result == {"action": "auto", "tier": "high", "workers": 15}

    def test_default_state_path_written_when_state_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = tier_once(
            _make_cfg(concurrency_auto=True), state=None, detector=FakeDetector(cores=8, tier="mid")
        )
        assert result == {"action": "auto", "tier": "mid", "workers": 4}
        assert TierState(tier_state.DEFAULT_STATE_PATH).get() == {
            "tier": "mid", "workers": 4, "cores": 8, "set_by": "auto",
        }

    def test_auto_missing_detector_module_raises_chinese(self, tmp_path, monkeypatch) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "netsentinel.ops.cpu_profile", None)  # 惰性导入失败
        st = TierState(tmp_path / "concurrency.json")
        with pytest.raises(RuntimeError, match="cpu_profile"):
            tier_once(_make_cfg(concurrency_auto=True), state=st, detector=None)
