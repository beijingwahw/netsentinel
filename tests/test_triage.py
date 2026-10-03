"""netsentinel.decision.triage 单元测试(决策论复核分诊)。

覆盖:
* priority 四分量公式与冷启动均匀先验(Beta(1,1) → 0.5);
* 单调性:p(NSFW) 更高 / 判定档位更高(nsfw>suspect>clean)/ 更老 / 档位
  翻案率更高 → 优先级更高(老化分量饱和于 1);
* OverturnStats 估计器:from_history / from_sqlite(只读现有表结构)、
  pending 不入样本、后验均值公式、档位无历史回退全局、表缺失回落冷启动;
* aging_factor:缺失/坏时间戳保守取 0、naive 按 UTC、精确比例、未来时间
  钳 0、非法时长中文报错;
* sort_entries:同分按 id FIFO 平局决断、纯函数不改输入、确定性(同输入
  同输出)、p_nsfw_by_url 注入覆盖档位代理、等键稳定;
* 红线:排序是纯顺序变换,不产生任何写副作用。

全部离线;from_sqlite 用裸 sqlite3 内存/文件库,不依赖 review_queue。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from netsentinel.decision.triage import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_WEIGHTS,
    OVERTURN_STATUS,
    RESOLVED_STATUSES,
    SORT_FIFO,
    SORT_TRIAGE,
    VERDICT_HARM,
    VERDICT_P_NSFW_PROXY,
    OverturnStats,
    TriageWeights,
    aging_factor,
    priority,
    sort_entries,
)

_NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def iso(delta: timedelta | None = None) -> str:
    """以固定 _NOW 为基准生成 ISO 时间戳(默认当前)。"""
    moment = _NOW if delta is None else _NOW + delta
    return moment.isoformat()


@dataclass
class FakeEntry:
    """最小鸭子类型条目(triage 只读 verdict/status/created_at/site_url/id)。"""

    id: int
    site_url: str = ""
    verdict: str = "suspect"
    status: str = "pending"
    created_at: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# 权重对象
# ---------------------------------------------------------------------------


class TestTriageWeights:
    def test_default_weights_moderate_and_sum_to_one(self):
        w = TriageWeights()
        assert w == DEFAULT_WEIGHTS
        assert w.w_p_nsfw == 0.4 and w.w_harm == 0.3
        assert w.w_overturn == 0.1 and w.w_age == 0.2
        assert w.w_p_nsfw + w.w_harm + w.w_overturn + w.w_age == pytest.approx(1.0)
        # 单分量上限 0.4:默认是温和重排,不是激进全反转。
        assert max(w.w_p_nsfw, w.w_harm, w.w_overturn, w.w_age) <= 0.4

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"w_p_nsfw": float("inf")},
            {"w_harm": float("nan")},
            {"w_overturn": float("-inf")},
            {"w_age": True},  # bool 虽是 int 子类,但不应被当作权重
            {"w_p_nsfw": "0.4"},
        ],
    )
    def test_invalid_weights_raise_chinese(self, kwargs):
        with pytest.raises(ValueError, match="分诊权重"):
            TriageWeights(**kwargs)

    def test_custom_weights_allowed(self):
        w = TriageWeights(0.0, 0.0, 1.0, 0.0)  # 只按翻案概率排
        assert w.w_overturn == 1.0


# ---------------------------------------------------------------------------
# priority:冷启动均匀先验 + 公式 + 单调性
# ---------------------------------------------------------------------------


class TestPriorityColdStart:
    def test_uniform_prior_means_half_for_any_verdict(self):
        stats = OverturnStats()  # 冷启动:无任何历史
        for verdict in ("nsfw", "suspect", "clean", "unknown-verdict"):
            assert stats.overturn_prob(verdict) == pytest.approx(0.5)

    def test_formula_with_default_weights_and_cold_start(self):
        e = FakeEntry(1, verdict="nsfw", created_at=iso())  # 全新,age=0
        got = priority(e, now=_NOW)
        expected = (
            DEFAULT_WEIGHTS.w_p_nsfw * VERDICT_P_NSFW_PROXY["nsfw"]
            + DEFAULT_WEIGHTS.w_harm * VERDICT_HARM["nsfw"]
            + DEFAULT_WEIGHTS.w_overturn * 0.5
            + DEFAULT_WEIGHTS.w_age * 0.0
        )
        assert got == pytest.approx(expected)

    def test_unknown_verdict_falls_back_to_zero_proxy_and_harm(self):
        e = FakeEntry(1, verdict="totally-unknown", created_at=iso())
        assert priority(e, now=_NOW) == pytest.approx(DEFAULT_WEIGHTS.w_overturn * 0.5)


class TestPriorityMonotonicity:
    def test_higher_p_nsfw_means_higher_priority(self):
        low = FakeEntry(1, verdict="suspect", created_at=iso())
        high = FakeEntry(2, verdict="suspect", created_at=iso())
        p_low = priority(low, now=_NOW, p_nsfw=0.1)
        p_high = priority(high, now=_NOW, p_nsfw=0.9)
        assert p_high > p_low

    def test_harm_tier_monotone_nsfw_over_suspect_over_clean(self):
        # 显式固定 p_nsfw,隔离危害分量:nsfw > suspect > clean。
        entries = [
            FakeEntry(1, verdict=v, created_at=iso())
            for v in ("clean", "suspect", "nsfw")
        ]
        scores = [priority(e, now=_NOW, p_nsfw=0.5) for e in entries]
        assert scores[0] < scores[1] < scores[2]

    def test_older_entry_means_higher_priority(self):
        fresh = FakeEntry(1, verdict="suspect", created_at=iso())
        old = FakeEntry(2, verdict="suspect", created_at=iso(-timedelta(days=5)))
        assert priority(old, now=_NOW) > priority(fresh, now=_NOW)

    def test_age_factor_saturates_at_one(self):
        three_days = FakeEntry(1, verdict="suspect", created_at=iso(-timedelta(days=3)))
        hundred_days = FakeEntry(
            2, verdict="suspect", created_at=iso(-timedelta(days=100))
        )
        p3 = priority(three_days, now=_NOW)  # 3d = 72h = DEFAULT_MAX_AGE → 饱和
        p100 = priority(hundred_days, now=_NOW)
        assert aging_factor(three_days, now=_NOW) == pytest.approx(1.0)
        assert p3 == p100  # 超过基准时长后老化分量不再增长(防老条目无限霸榜)

    def test_overturn_component_monotone_when_dominant(self):
        # 只开翻案分量:历史上高驳回率的档位优先(信息价值更高)。
        stats = OverturnStats(
            {"suspect": 9, "nsfw": 0}, {"suspect": 10, "nsfw": 4}
        )
        w = TriageWeights(0.0, 0.0, 1.0, 0.0)
        suspect = FakeEntry(1, verdict="suspect", created_at=iso())
        nsfw = FakeEntry(2, verdict="nsfw", created_at=iso())
        assert stats.overturn_prob("suspect") == pytest.approx((9 + 1) / (10 + 2))
        assert stats.overturn_prob("nsfw") == pytest.approx((0 + 1) / (4 + 2))
        assert priority(suspect, weights=w, overturn_stats=stats, now=_NOW) > priority(
            nsfw, weights=w, overturn_stats=stats, now=_NOW
        )


class TestPriorityValidation:
    def test_non_finite_p_nsfw_raises_chinese(self):
        e = FakeEntry(1)
        with pytest.raises(ValueError, match="p\\(NSFW\\)"):
            priority(e, p_nsfw=float("nan"), now=_NOW)

    def test_non_positive_max_age_raises_chinese(self):
        e = FakeEntry(1)
        with pytest.raises(ValueError, match="老化基准时长"):
            priority(e, max_age_seconds=0, now=_NOW)


# ---------------------------------------------------------------------------
# aging_factor
# ---------------------------------------------------------------------------


class TestAgingFactor:
    def test_exact_half_at_half_max_age(self):
        e = FakeEntry(1, created_at=iso(-timedelta(hours=36)))  # 36h / 72h
        assert aging_factor(e, now=_NOW) == pytest.approx(0.5)

    def test_missing_or_garbage_created_at_is_conservatively_zero(self):
        assert aging_factor(FakeEntry(1, created_at=""), now=_NOW) == 0.0
        assert aging_factor(FakeEntry(1, created_at="not-a-date"), now=_NOW) == 0.0

    def test_naive_timestamp_treated_as_utc(self):
        e = FakeEntry(1, created_at="2026-01-01T00:00:00")  # 无时区 → 按 UTC
        assert 0.0 < aging_factor(e, now=_NOW) <= 1.0

    def test_future_created_at_clamps_to_zero(self):
        e = FakeEntry(1, created_at=iso(+timedelta(hours=1)))  # 时钟偏移
        assert aging_factor(e, now=_NOW) == 0.0

    def test_invalid_max_age_raises(self):
        with pytest.raises(ValueError, match="老化基准时长"):
            aging_factor(FakeEntry(1, created_at=iso()), max_age_seconds=-5)

    def test_default_max_age_is_72_hours(self):
        assert DEFAULT_MAX_AGE_SECONDS == 72 * 3600


# ---------------------------------------------------------------------------
# OverturnStats 估计器
# ---------------------------------------------------------------------------


class TestOverturnStats:
    def test_pending_not_sampled_and_beta_posterior_mean(self):
        entries = [
            FakeEntry(1, verdict="suspect", status="rejected"),
            FakeEntry(2, verdict="suspect", status="rejected"),
            FakeEntry(3, verdict="suspect", status="rejected"),
            FakeEntry(4, verdict="suspect", status="approved"),
            FakeEntry(5, verdict="suspect", status="pending"),  # 未定案,不入样本
            FakeEntry(6, verdict="nsfw", status="submitted"),
        ]
        stats = OverturnStats.from_history(entries)
        # suspect:3 翻案 / 4 已定案 → (3+1)/(4+2) = 2/3
        assert stats.overturn_prob("suspect") == pytest.approx(2 / 3)
        # nsfw:0 翻案 / 1 已定案 → 1/3
        assert stats.overturn_prob("nsfw") == pytest.approx(1 / 3)
        assert stats.total_resolved() == 5  # pending 不计
        assert stats.total_overturns() == 3

    def test_cold_start_returns_half_for_any_verdict(self):
        stats = OverturnStats()
        assert stats.overturn_prob("nsfw") == pytest.approx(0.5)
        assert stats.total_resolved() == 0

    def test_empty_bucket_falls_back_to_global_posterior(self):
        # nsfw 档位无历史 → 借全局经验 (3+1)/(4+2) = 2/3。
        stats = OverturnStats({"suspect": 3}, {"suspect": 4})
        assert stats.overturn_prob("nsfw") == pytest.approx(2 / 3)
        assert stats.overturn_prob("suspect") == pytest.approx(2 / 3)

    def test_from_sqlite_read_only_aggregation(self, tmp_path):
        db = str(tmp_path / "hist.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE entries (id INTEGER PRIMARY KEY, site_url TEXT,"
            " verdict TEXT, status TEXT DEFAULT 'pending', evidence_zip TEXT"
            " DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',"
            " note TEXT DEFAULT '')"
        )
        rows = [
            ("http://a.test/", "nsfw", "rejected"),
            ("http://b.test/", "nsfw", "approved"),
            ("http://c.test/", "nsfw", "submitted"),
            ("http://d.test/", "suspect", "pending"),
            ("http://e.test/", "suspect", "rejected"),
        ]
        conn.executemany(
            "INSERT INTO entries (site_url, verdict, status) VALUES (?, ?, ?)", rows
        )
        conn.commit()

        stats = OverturnStats.from_sqlite(conn)
        assert stats.overturn_prob("nsfw") == pytest.approx((1 + 1) / (3 + 2))
        assert stats.overturn_prob("suspect") == pytest.approx((1 + 1) / (1 + 2))
        assert stats.total_resolved() == 4

        # 只读:不创建任何新表/新索引,不修改任何行。
        objects = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert objects == {"entries"}
        conn.close()

    def test_from_sqlite_missing_table_falls_back_to_cold_start(self):
        conn = sqlite3.connect(":memory:")  # 无 entries 表
        stats = OverturnStats.from_sqlite(conn)
        assert stats.overturn_prob("nsfw") == pytest.approx(0.5)
        conn.close()

    def test_overturn_status_constants(self):
        assert OVERTURN_STATUS == "rejected"
        assert set(RESOLVED_STATUSES) == {"rejected", "approved", "submitted"}
        assert SORT_FIFO == "fifo" and SORT_TRIAGE == "triage"


# ---------------------------------------------------------------------------
# sort_entries:确定性与稳定性
# ---------------------------------------------------------------------------


class TestSortEntries:
    def test_same_score_breaks_tie_by_id_fifo(self):
        ts = iso()
        entries = [
            FakeEntry(3, site_url=f"http://s3.test/", created_at=ts),
            FakeEntry(1, site_url=f"http://s1.test/", created_at=ts),
            FakeEntry(2, site_url=f"http://s2.test/", created_at=ts),
        ]
        out = sort_entries(entries, now=_NOW)
        assert [e.id for e in out] == [1, 2, 3]  # 同分 → id 升序(FIFO 平局)

    def test_triage_reorders_older_nsfw_above_newer_suspect(self):
        entries = [
            FakeEntry(1, site_url="http://new-suspect.test/", verdict="suspect",
                      created_at=iso()),
            FakeEntry(2, site_url="http://old-nsfw.test/", verdict="nsfw",
                      created_at=iso(-timedelta(days=10))),
        ]
        assert [e.id for e in entries] == [1, 2]  # FIFO 顺序
        out = sort_entries(entries, now=_NOW)
        assert [e.id for e in out] == [2, 1]  # 分诊:老 nsfw 优先

    def test_same_verdict_older_first(self):
        entries = [
            FakeEntry(1, verdict="suspect", created_at=iso()),
            FakeEntry(2, verdict="suspect", created_at=iso(-timedelta(days=5))),
        ]
        out = sort_entries(entries, now=_NOW)
        assert [e.id for e in out] == [2, 1]

    def test_pure_function_does_not_mutate_input(self):
        entries = [
            FakeEntry(1, verdict="suspect", created_at=iso()),
            FakeEntry(2, verdict="nsfw", created_at=iso(-timedelta(days=1))),
        ]
        snapshot = list(entries)
        sort_entries(entries, now=_NOW)
        assert entries == snapshot

    def test_deterministic_across_repeated_calls(self):
        ts = iso()
        entries = [
            FakeEntry(i, site_url=f"http://s{i}.test/", created_at=ts)
            for i in (5, 2, 9, 1)
        ]
        first = sort_entries(entries, now=_NOW)
        for _ in range(3):
            assert [e.id for e in sort_entries(entries, now=_NOW)] == [
                e.id for e in first
            ]

    def test_equal_keys_stable_for_duplicate_ids(self):
        ts = iso()
        entries = [
            FakeEntry(7, site_url="http://x.test/", created_at=ts, note="first"),
            FakeEntry(7, site_url="http://x.test/", created_at=ts, note="second"),
        ]
        out = sort_entries(entries, now=_NOW)
        assert [e.note for e in out] == ["first", "second"]  # 等键保持输入序

    def test_p_nsfw_by_url_overrides_verdict_proxy(self):
        ts = iso()
        entries = [
            FakeEntry(1, site_url="http://low.test/", verdict="suspect",
                      created_at=ts),
            FakeEntry(2, site_url="http://high.test/", verdict="suspect",
                      created_at=ts),
        ]
        out = sort_entries(entries, now=_NOW, p_nsfw_by_url={
            "http://low.test/": 0.01, "http://high.test/": 0.99
        })
        assert [e.id for e in out] == [2, 1]

    def test_accepts_any_iterable(self):
        ts = iso()
        out = sort_entries(
            (e for e in [FakeEntry(1, created_at=ts)]), now=_NOW
        )
        assert isinstance(out, list) and len(out) == 1


# ---------------------------------------------------------------------------
# boost_by_url:分歧弃权提权加项(默认 None = 现状;加在四分量优先级之上)
# ---------------------------------------------------------------------------


def _flat_entries(n: int = 3) -> list[FakeEntry]:
    """同分条目批:同 verdict / 同 created_at → priority 完全相等。"""
    ts = iso()
    return [
        FakeEntry(i, site_url=f"http://flat-{i}.test/", verdict="suspect",
                  created_at=ts)
        for i in range(1, n + 1)
    ]


class TestBoostByUrl:
    def test_default_none_and_empty_map_match_legacy_order(self):
        """默认 None / 空映射 = 现状:与不传该参数的输出逐项一致。"""
        entries = _flat_entries()
        legacy = sort_entries(entries, now=_NOW)
        assert sort_entries(entries, now=_NOW, boost_by_url=None) == legacy
        assert sort_entries(entries, now=_NOW, boost_by_url={}) == legacy

    def test_zero_boost_equals_none(self):
        """加 0 等价于不加:排序与现状完全一致。"""
        entries = _flat_entries()
        legacy = [e.id for e in sort_entries(entries, now=_NOW)]
        boosted = [
            e.id
            for e in sort_entries(
                entries, now=_NOW,
                boost_by_url={e.site_url: 0.0 for e in entries},
            )
        ]
        assert boosted == legacy

    def test_boost_breaks_fifo_tie_promoting_boosted_entry(self):
        """同分平局:被提权的条目先看,其余条目仍按 id FIFO 平局决断。"""
        entries = _flat_entries()
        out = sort_entries(
            entries, now=_NOW, boost_by_url={"http://flat-3.test/": 0.5}
        )
        assert [e.id for e in out] == [3, 1, 2]

    def test_boost_is_monotone_in_weight(self):
        """单调性:加项越大排位越靠前;小加项翻不过老化分量差距时次序不变。

        老化分量差距 = 0.2 * (5d/72h) ≈ 0.0139:加 0.01 不够(仍 old 在前),
        加 1.0 足够(new + boost 反超)。
        """
        fresh = FakeEntry(1, site_url="http://fresh.test/", created_at=iso())
        old = FakeEntry(
            2, site_url="http://old.test/", created_at=iso(-timedelta(days=5))
        )
        entries = [fresh, old]
        assert [e.id for e in sort_entries(entries, now=_NOW)] == [2, 1]

        small = sort_entries(
            entries, now=_NOW, boost_by_url={"http://fresh.test/": 0.01}
        )
        assert [e.id for e in small] == [2, 1]  # 加项不足:基线次序保持

        big = sort_entries(
            entries, now=_NOW, boost_by_url={"http://fresh.test/": 1.0}
        )
        assert [e.id for e in big] == [1, 2]  # 加项足够:提权反超

        bigger = sort_entries(
            entries, now=_NOW, boost_by_url={"http://fresh.test/": 100.0}
        )
        assert [e.id for e in bigger] == [1, 2]  # 更大加项不劣化(单调不降)

    def test_boost_beats_verdict_tier_when_large(self):
        """大加项可以越过档位差距(clean 提到 suspect 之前)——提权是显式
        上游信号(如弃权分歧),优先级由调用方掌握。"""
        clean = FakeEntry(1, site_url="http://clean.test/", verdict="clean",
                          created_at=iso())
        suspect = FakeEntry(2, site_url="http://suspect.test/", verdict="suspect",
                            created_at=iso())
        assert [e.id for e in sort_entries([clean, suspect], now=_NOW)] == [2, 1]
        out = sort_entries(
            [clean, suspect], now=_NOW, boost_by_url={"http://clean.test/": 1.0}
        )
        assert [e.id for e in out] == [1, 2]

    def test_boost_combines_with_p_nsfw_by_url(self):
        """两映射正交叠加:p_nsfw 覆盖档位代理,boost 加在其上。"""
        ts = iso()
        low = FakeEntry(1, site_url="http://low.test/", verdict="suspect",
                        created_at=ts)
        high = FakeEntry(2, site_url="http://high.test/", verdict="suspect",
                         created_at=ts)
        out = sort_entries(
            [low, high],
            now=_NOW,
            p_nsfw_by_url={"http://low.test/": 0.01, "http://high.test/": 0.99},
            boost_by_url={"http://low.test/": 0.5},
        )
        # high 基线优势 = 0.4 * (0.99 - 0.01) = 0.392 < 0.5 → low 提权反超
        assert [e.id for e in out] == [1, 2]

    def test_boost_unknown_url_is_noop(self):
        """未命中的 URL 不受影响(与现状一致)。"""
        entries = _flat_entries()
        out = sort_entries(entries, now=_NOW, boost_by_url={"http://other.test/": 9.0})
        assert [e.id for e in out] == [1, 2, 3]

    @pytest.mark.parametrize(
        "value",
        [
            float("nan"),
            float("inf"),
            -0.1,
            "0.5",
            True,
        ],
    )
    def test_invalid_boost_raises_chinese(self, value):
        entries = _flat_entries()
        with pytest.raises(ValueError, match="提权加项"):
            sort_entries(entries, now=_NOW, boost_by_url={"http://flat-1.test/": value})

    def test_boost_keeps_purity_and_determinism(self):
        """提权不改变纯函数性质:不修改输入、重复调用同输出。"""
        entries = [
            FakeEntry(1, site_url="http://b1.test/", created_at=iso()),
            FakeEntry(2, site_url="http://b2.test/",
                      created_at=iso(-timedelta(days=2))),
        ]
        snapshot = list(entries)
        first = sort_entries(entries, now=_NOW, boost_by_url={"http://b1.test/": 0.3})
        for _ in range(3):
            assert [e.id for e in sort_entries(
                entries, now=_NOW, boost_by_url={"http://b1.test/": 0.3}
            )] == [e.id for e in first]
        assert entries == snapshot  # 输入未被修改

    def test_priority_formula_untouched_by_boost(self):
        """红线:boost 只进排序键,priority() 四分量公式本身零改动。"""
        e = FakeEntry(1, verdict="nsfw", created_at=iso())
        expected = (
            DEFAULT_WEIGHTS.w_p_nsfw * VERDICT_P_NSFW_PROXY["nsfw"]
            + DEFAULT_WEIGHTS.w_harm * VERDICT_HARM["nsfw"]
            + DEFAULT_WEIGHTS.w_overturn * 0.5
            + DEFAULT_WEIGHTS.w_age * 0.0
        )
        assert priority(e, now=_NOW) == pytest.approx(expected)
        # 带 boost 排序后,条目的四分量优先级仍与公式一致(未被写回/篡改)。
        sort_entries([e], now=_NOW, boost_by_url={"": 5.0})
        assert priority(e, now=_NOW) == pytest.approx(expected)
