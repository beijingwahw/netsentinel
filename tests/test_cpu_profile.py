"""A163 netsentinel.ops.cpu_profile CPU 画像与三档换算测试。

纯离线、零外呼、零网络、零真实等待。覆盖:

- detect:键集合恰为契约四键 ``{"cores","arch","platform","psutil"}``、
  类型逐键断言(cores 为 ≥1 的 int、psutil 为 bool、arch/platform 非空
  str 且 platform 含 ``sys.platform``);缺省核数与 ``os.cpu_count() or 2``
  一致;``NETSENTINEL_FAKE_CORES`` 正整数覆盖(含带空白)、非法值
  (0 / 负数 / 非整数 / 空串)一律忽略回退;psutil 双态(sys.modules 注入
  fake → True;置 None 阻断 → False);
- tier_workers 公式参数化:N=1/2/4/8/16/32 × 三档(reserve 缺省 1)逐值
  对表;reserve=0(high=全核)与 reserve=4 边界对表;reserve 只作用于
  high 档(low/mid 不随 reserve 变);reserve ≥ cores → 1(cores=1/8 与
  reserve=8/20/99 全退化 1);负 reserve 按 0;reserve 类型宽容(int() 可
  折)与非法(中文 ValueError);cores=None 缺省走 detect(受 FAKE_CORES
  影响);cores 坏输入按 2 容错;红线 37 全景:N=1..33 × 三档 ×
  reserve∈{0,1,4} 恒 1 ≤ workers ≤ N;
- 非法档位:空串 / 大小写混写 / "fast" / None / 整数 → 中文
  ValueError(tier_workers 与 validate_tier 双口径);
- validate_tier:三合法值原样返回;非法抛错;
- recommend:基础档参数化(cores 1/2→low,3/4/8→mid,9/16/64→high);
  psutil 可用且占用>0.8 降一档(high→mid、mid→low、low 不再降);
  0.8 恰不降 / 0.81 降(边界);占用百分数(95)与分数(0.95)等价;
  占位读不出(非数 / None / NaN)不降;psutil=False 不采样;sys.modules
  注入 fake psutil(cpu_percent=90.0 → 降档;10.0 → 不降;抛异常 → 不降);
  sys.modules 置 None(不可用)不降;坏画像(None / 非字典 / 缺 cores /
  非整数 / 0 / 负数)一律按 cores=2 → low;
- 零外呼:源码级断言无 socket / urllib / requests / httpx 等网络库字样,
  运行态命名空间无网络模块属性(红线:本模块只读本地)。
"""
from __future__ import annotations

import os
import platform as platform_mod
import sys
import types
from pathlib import Path

import pytest

from netsentinel.ops import cpu_profile as cpu_profile_mod
from netsentinel.ops.cpu_profile import (
    DEFAULT_CORES,
    ENV_FAKE_CORES,
    TIERS,
    detect,
    recommend,
    tier_workers,
    validate_tier,
)

DETECT_KEYS = {"cores", "arch", "platform", "psutil"}

#: 网络库字样黑名单(源码级零外呼断言;模块内连单词都不应出现)
NETWORK_TOKENS = ("socket", "urllib", "requests", "httpx", "ftp", "telnet")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前清掉 FAKE_CORES,避免开发机环境泄漏干扰断言。"""
    monkeypatch.delenv(ENV_FAKE_CORES, raising=False)


# ---------------------------------------------------------------------------
# detect:键集 / 类型 / 缺省与 FAKE_CORES 覆盖 / psutil 双态
# ---------------------------------------------------------------------------
def test_detect_keys_exact_and_types() -> None:
    p = detect()
    assert set(p) == DETECT_KEYS  # 恰好四键,不多不少
    assert isinstance(p["cores"], int) and not isinstance(p["cores"], bool)
    assert p["cores"] >= 1
    assert isinstance(p["arch"], str) and p["arch"]
    assert isinstance(p["platform"], str) and p["platform"]
    assert isinstance(p["psutil"], bool)


def test_detect_platform_contains_sys_platform() -> None:
    p = detect()
    assert sys.platform in p["platform"]
    assert platform_mod.system() in p["platform"]


def test_detect_cores_fallback_matches_cpu_count() -> None:
    p = detect()
    assert p["cores"] == (os.cpu_count() or DEFAULT_CORES)


def test_detect_arch_matches_platform_machine() -> None:
    assert detect()["arch"] == (platform_mod.machine() or "unknown")


@pytest.mark.parametrize(
    "raw,expected",
    [("4", 4), ("16", 16), (" 12 ", 12), ("1", 1), ("032", 32)],
)
def test_detect_fake_cores_override(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(ENV_FAKE_CORES, raw)
    assert detect()["cores"] == expected


@pytest.mark.parametrize("raw", ["", "0", "-3", "abc", "2.5", "1e2", "  "])
def test_detect_fake_cores_invalid_ignored(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV_FAKE_CORES, raw)
    assert detect()["cores"] == (os.cpu_count() or DEFAULT_CORES)


def test_detect_psutil_is_lazy_bool() -> None:
    # 不预置任何 sys.modules 状态:真机 psutil 装没装都成立(惰性探测)
    assert isinstance(detect()["psutil"], bool)


def test_detect_psutil_fake_module_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("psutil")
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert detect()["psutil"] is True


def test_detect_psutil_blocked_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # None = 阻断导入
    assert detect()["psutil"] is False


# ---------------------------------------------------------------------------
# tier_workers:§1 公式参数化 + reserve 边界 + 缺省核 + 坏输入
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "n,low,mid,high",
    [
        (1, 1, 1, 1),
        (2, 1, 1, 1),
        (4, 1, 2, 3),
        (8, 2, 4, 7),
        (16, 4, 8, 15),
        (32, 8, 16, 31),
    ],
)
def test_tier_workers_formula_default_reserve(n: int, low: int, mid: int, high: int) -> None:
    assert tier_workers("low", cores=n) == low
    assert tier_workers("mid", cores=n) == mid
    assert tier_workers("high", cores=n) == high  # reserve 缺省 1


@pytest.mark.parametrize(
    "n,high_full", [(1, 1), (2, 2), (4, 4), (8, 8), (16, 16), (32, 32)]
)
def test_tier_workers_high_reserve_zero_full_cores(n: int, high_full: int) -> None:
    assert tier_workers("high", reserve=0, cores=n) == high_full  # 全核压榨


@pytest.mark.parametrize(
    "n,high_r4", [(1, 1), (2, 1), (4, 1), (8, 4), (16, 12), (32, 28)]
)
def test_tier_workers_high_reserve_four(n: int, high_r4: int) -> None:
    assert tier_workers("high", reserve=4, cores=n) == high_r4


@pytest.mark.parametrize("reserve", [0, 1, 4])
def test_tier_workers_low_mid_ignore_reserve(reserve: int) -> None:
    # reserve 只作用于 high 档(契约 §1):low/mid 与 reserve 无关
    assert tier_workers("low", reserve=reserve, cores=16) == 4
    assert tier_workers("mid", reserve=reserve, cores=16) == 8


@pytest.mark.parametrize("cores,reserve", [(1, 99), (8, 8), (8, 20), (2, 7)])
def test_tier_workers_reserve_ge_cores_clamps_to_one(cores: int, reserve: int) -> None:
    assert tier_workers("high", reserve=reserve, cores=cores) == 1


@pytest.mark.parametrize("n", [1, 2, 4, 8, 16, 32])
def test_tier_workers_negative_reserve_treated_as_zero(n: int) -> None:
    assert tier_workers("high", reserve=-5, cores=n) == n
    assert tier_workers("high", reserve=0, cores=n) == n


def test_tier_workers_reserve_coercion() -> None:
    assert tier_workers("high", reserve=2.9, cores=8) == 6  # int() 宽容折算
    assert tier_workers("high", reserve="3", cores=8) == 5


@pytest.mark.parametrize("bad_reserve", ["abc", None, [1]])
def test_tier_workers_bad_reserve_value_error(bad_reserve: object) -> None:
    with pytest.raises(ValueError, match="reserve"):
        tier_workers("high", reserve=bad_reserve, cores=8)  # type: ignore[arg-type]


def test_tier_workers_default_cores_from_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FAKE_CORES, "12")
    # cores=None 缺省走 detect():受 FAKE_CORES 覆盖影响
    assert tier_workers("low") == 3
    assert tier_workers("mid") == 6
    assert tier_workers("high") == 11


def test_tier_workers_explicit_cores_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FAKE_CORES, "12")
    assert tier_workers("mid", cores=16) == 8  # 显式注入优先


@pytest.mark.parametrize("bad_cores", ["abc", 0, -4, object()])
def test_tier_workers_bad_cores_tolerated_as_two(bad_cores: object) -> None:
    # 坏核数按 cores=2 容错:low/mid/high 全部退化 1
    # (注意 cores=None 不是坏输入——那是"缺省走 detect()"的合法语义)
    assert tier_workers("low", cores=bad_cores) == 1  # type: ignore[arg-type]
    assert tier_workers("mid", cores=bad_cores) == 1  # type: ignore[arg-type]
    assert tier_workers("high", cores=bad_cores) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize("n", list(range(1, 34)))
def test_tier_workers_redline37_bounded_by_cores(n: int) -> None:
    # 红线 37 全景:三档 × reserve∈{0,1,4} 恒 1 ≤ workers ≤ N
    for tier in TIERS:
        for reserve in (0, 1, 4):
            w = tier_workers(tier, reserve=reserve, cores=n)
            assert 1 <= w <= n, (tier, reserve, n, w)


# ---------------------------------------------------------------------------
# 非法档位(tier_workers / validate_tier 双口径)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_tier", ["", "fast", "LOW", "High", "mid ", " low", "ultra", "中档"]
)
def test_tier_workers_invalid_tier_chinese_error(bad_tier: str) -> None:
    with pytest.raises(ValueError) as ei:
        tier_workers(bad_tier, cores=8)
    assert "档位" in str(ei.value)
    assert "low / mid / high" in str(ei.value)


@pytest.mark.parametrize("bad_tier", [None, 123, 3.5, ["low"], {"tier": "low"}])
def test_tier_workers_non_str_tier_rejected(bad_tier: object) -> None:
    with pytest.raises(ValueError):
        tier_workers(bad_tier, cores=8)  # type: ignore[arg-type]


@pytest.mark.parametrize("tier", TIERS)
def test_validate_tier_ok_returns_same(tier: str) -> None:
    assert validate_tier(tier) == tier


@pytest.mark.parametrize("bad_tier", ["", "HIGH", "fast", None, 7])
def test_validate_tier_invalid_raises(bad_tier: object) -> None:
    with pytest.raises(ValueError, match="档位"):
        validate_tier(bad_tier)


def test_validate_tier_message_names_all_tiers() -> None:
    with pytest.raises(ValueError) as ei:
        validate_tier("turbo")
    assert "low / mid / high" in str(ei.value)
    assert "'turbo'" in str(ei.value)


# ---------------------------------------------------------------------------
# recommend:基础档 / 降档 / 边界 / fake psutil / 坏画像
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cores,expected",
    [(1, "low"), (2, "low"), (3, "mid"), (4, "mid"), (8, "mid"), (9, "high"),
     (16, "high"), (64, "high")],
)
def test_recommend_base_tiers(cores: int, expected: str) -> None:
    assert recommend({"cores": cores, "psutil": False}) == expected


def test_recommend_high_downgrades_to_mid_on_busy_cpu() -> None:
    for usage in (0.81, 0.9, 0.99):
        assert recommend({"cores": 16, "psutil": True, "cpu_usage": usage}) == "mid"


def test_recommend_mid_downgrades_to_low_on_busy_cpu() -> None:
    assert recommend({"cores": 8, "psutil": True, "cpu_usage": 0.9}) == "low"
    assert recommend({"cores": 3, "psutil": True, "cpu_usage": 0.95}) == "low"


@pytest.mark.parametrize("cores", [1, 2])
def test_recommend_low_is_floor(cores: int) -> None:
    # low 已是底线,系统再忙也无处可降
    assert recommend({"cores": cores, "psutil": True, "cpu_usage": 0.99}) == "low"


@pytest.mark.parametrize(
    "usage,expected",
    [(0.0, "high"), (0.5, "high"), (0.79, "high"), (0.8, "high"), (0.81, "mid")],
)
def test_recommend_usage_threshold_boundary(usage: float, expected: str) -> None:
    # 0.8 恰不降(须严格大于),0.81 降档
    assert recommend({"cores": 16, "psutil": True, "cpu_usage": usage}) == expected


@pytest.mark.parametrize("usage", [95, 90.0, "85", 100])
def test_recommend_percent_usage_equivalent(usage: object) -> None:
    # >1 的占用值按百分数折算:>80% 即降档
    assert recommend({"cores": 16, "psutil": True, "cpu_usage": usage}) == "mid"


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, "high"),        # 显式给但读不出 → 不降档
        ("n/a", "high"),       # 非数字 → 不降档
        (float("nan"), "high"),  # NaN → 视为读不出
        (-1, "high"),          # 负数夹到 0.0,不超阈
        (200, "mid"),          # 200% 折 2.0 夹到 1.0,仍 > 0.8 → 降档
    ],
)
def test_recommend_unreadable_usage(usage: object, expected: str) -> None:
    assert recommend({"cores": 16, "psutil": True, "cpu_usage": usage}) == expected


def test_recommend_no_psutil_no_sampling() -> None:
    # 画像显式 psutil=False:即便核多也不做占用采样 → 不降档
    assert recommend({"cores": 64, "psutil": False, "cpu_usage": None}) == "high"
    assert recommend({"cores": 64, "psutil": False}) == "high"


def test_recommend_fake_psutil_module_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("psutil")
    fake.cpu_percent = lambda interval=None: 90.0  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "psutil", fake)
    # 画像不带 cpu_usage / psutil 键 → 惰性 import 命中 fake → 降档
    assert recommend({"cores": 16}) == "mid"


def test_recommend_fake_psutil_low_load_keeps_high(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("psutil")
    fake.cpu_percent = lambda interval=None: 10.0  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert recommend({"cores": 16}) == "high"


def test_recommend_fake_psutil_sample_error_no_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(interval: object = None) -> float:
        raise RuntimeError("采样失败")

    fake = types.ModuleType("psutil")
    fake.cpu_percent = _boom  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert recommend({"cores": 16, "psutil": True}) == "high"


def test_recommend_psutil_unavailable_no_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # 导入被阻断
    assert recommend({"cores": 32, "psutil": True}) == "high"
    assert recommend({"cores": 32}) == "high"


@pytest.mark.parametrize(
    "bad_profile",
    [None, {}, "profile", 42, ["cores", 16], {"cores": "abc"}, {"cores": None},
     {"cores": 0}, {"cores": -5}, {"cores": [16]}],
)
def test_recommend_bad_profile_tolerated_as_two_cores(bad_profile: object) -> None:
    assert recommend(bad_profile) == "low"  # type: ignore[arg-type]


def test_recommend_detect_pipeline_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    # 与 detect() 产物直连(真实画像):档位落在三档之内即为契约行为
    monkeypatch.setitem(sys.modules, "psutil", None)  # 阻断真实采样保确定性
    tier = recommend(detect())
    assert tier in TIERS


# ---------------------------------------------------------------------------
# 零外呼:源码级 + 运行态断言
# ---------------------------------------------------------------------------
def test_zero_outbound_no_network_tokens_in_source() -> None:
    src = Path(cpu_profile_mod.__file__).read_text(encoding="utf-8")
    for token in NETWORK_TOKENS:
        assert token not in src, f"cpu_profile 源码不得出现网络库字样:{token}"


def test_zero_outbound_no_network_attr_after_calls() -> None:
    detect()
    tier_workers("mid", cores=8)
    recommend({"cores": 8, "psutil": False})
    for token in NETWORK_TOKENS:
        assert not hasattr(cpu_profile_mod, token)
