"""A18 提交频控(RateLimiter)单元测试:全部离线,只写 tmp_path。

覆盖:间隔不足拒绝、当日超限拒绝、record 后状态变化与跨实例持久化、
损坏状态文件重置、跨天次数清零、aware/naive 时间混用、构造参数校验。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from netsentinel.submit.rate_limit import RateLimiter

T0 = datetime(2026, 10, 1, 9, 0, 0)  # 周四本地 naive 基准时刻


def make(tmp_path: Path, name: str = "rl.json",
         min_interval_s: int = 60, max_per_day: int = 5) -> RateLimiter:
    return RateLimiter(str(tmp_path / name), min_interval_s, max_per_day)


def test_interval_too_soon_rejected(tmp_path: Path) -> None:
    """距上次提交不足最小间隔 → 拒绝并给出中文原因。"""
    rl = make(tmp_path, min_interval_s=60, max_per_day=5)
    rl.record(T0)
    ok, reason = rl.can_submit(T0 + timedelta(seconds=30))
    assert ok is False
    assert "间隔" in reason and "60" in reason
    # 恰好达到间隔后放行
    ok2, reason2 = rl.can_submit(T0 + timedelta(seconds=60))
    assert ok2 is True and reason2 == ""


def test_daily_cap_rejected(tmp_path: Path) -> None:
    """当日(本地时区)提交次数达到上限 → 即使间隔充足也拒绝。"""
    rl = make(tmp_path, min_interval_s=30, max_per_day=2)
    rl.record(T0)
    rl.record(T0 + timedelta(minutes=1))
    ok, reason = rl.can_submit(T0 + timedelta(minutes=10))
    assert ok is False
    assert "每日" in reason or "上限" in reason


def test_record_changes_can_submit_and_persists(tmp_path: Path) -> None:
    """record 前允许、record 后立刻被拦;状态落盘可跨实例生效。"""
    state = str(tmp_path / "state" / "rl.json")
    rl = RateLimiter(state, min_interval_s=60, max_per_day=5)
    assert rl.can_submit(T0) == (True, "")
    rl.record(T0)
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False

    # 新实例读取同一状态文件:频控依然生效(持久化)。
    rl2 = RateLimiter(state, min_interval_s=60, max_per_day=5)
    assert rl2.can_submit(T0 + timedelta(seconds=5))[0] is False
    rl2.record(T0 + timedelta(seconds=60))
    ok, _ = rl2.can_submit(T0 + timedelta(seconds=200))
    assert ok is True
    # 落盘内容为 JSONL:每行一个合法 JSON 字符串(ISO 时间戳),共 2 条
    # (V5 更新:原断言读取旧版整文件 dict 格式,现按流式 JSONL 校验)
    lines = [ln for ln in Path(state).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all(isinstance(json.loads(ln), str) for ln in lines)


def test_corrupt_state_file_reset(tmp_path: Path) -> None:
    """状态文件损坏 → 按空状态重置(允许提交),record 后恢复正常。"""
    nested = tmp_path / "deep" / "nested" / "rl.json"
    rl = RateLimiter(str(nested), min_interval_s=60, max_per_day=5)
    assert nested.parent.is_dir()  # 目录自动创建
    nested.write_text("not-json{{{", encoding="utf-8")
    ok, reason = rl.can_submit(T0)
    assert ok is True and reason == ""

    rl.record(T0)
    # (V5 更新:原断言读取旧版整文件 dict 格式;record 自愈后应为干净 JSONL,
    # 垃圾行已被整体重写清除,仅剩本次提交一条)
    lines = [ln for ln in nested.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == [json.dumps(T0.isoformat())]
    assert rl.can_submit(T0 + timedelta(seconds=10))[0] is False


def test_corrupt_structure_reset(tmp_path: Path) -> None:
    """JSON 合法但结构不符(缺 timestamps / 类型错误)同样重置为空。"""
    p = tmp_path / "rl.json"
    rl = make(tmp_path)
    p.write_text(json.dumps({"wrong": 1}), encoding="utf-8")
    assert rl.can_submit(T0) == (True, "")
    p.write_text(json.dumps({"timestamps": "not-a-list"}), encoding="utf-8")
    assert rl.can_submit(T0) == (True, "")
    # 单条无法解析的时间戳被跳过,不影响其余记录
    p.write_text(
        json.dumps({"timestamps": ["garbage", T0.isoformat()]}), encoding="utf-8"
    )
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False


def test_cross_day_counter_reset(tmp_path: Path) -> None:
    """昨天的提交不计入当日次数,跨天后计数清零。"""
    rl = make(tmp_path, min_interval_s=30, max_per_day=2)
    yesterday = T0 - timedelta(days=1)
    for i in range(3):  # 昨天已提交 3 次,超过每日上限 2
        rl.record(yesterday + timedelta(minutes=i))
    ok, reason = rl.can_submit(T0)  # 今天:计数清零,且距昨天间隔早已足够
    assert ok is True and reason == ""


def test_aware_datetimes_accepted(tmp_path: Path) -> None:
    """aware datetime 自动换算为本地 naive,naive/aware 混用不抛 TypeError。"""
    rl = make(tmp_path, min_interval_s=60, max_per_day=5)
    aware = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    rl.record(aware)
    ok, _ = rl.can_submit(aware + timedelta(seconds=120))
    assert ok is True
    # aware 记录 + naive 查询:不抛异常且仍受间隔约束
    ok2, _ = rl.can_submit(aware.replace(tzinfo=None) + timedelta(seconds=1))
    assert isinstance(ok2, bool)


def test_constructor_validation(tmp_path: Path) -> None:
    """非法参数构造直接拒绝(中文 ValueError)。"""
    with pytest.raises(ValueError, match="max_per_day"):
        RateLimiter(str(tmp_path / "a.json"), 60, 0)
    with pytest.raises(ValueError, match="min_interval_s"):
        RateLimiter(str(tmp_path / "a.json"), -1, 5)


# ---------------------------------------------------------------------------
# V5 升级:JSONL 流式落盘(追加写/自愈重写/旧格式兼容)
# + can_submit 单遍语义 + ratelimit.blocked 遥测
# ---------------------------------------------------------------------------
from netsentinel import telemetry  # noqa: E402


def test_v5_record_appends_one_line_without_rewrite(tmp_path: Path) -> None:
    """健康状态文件追加写:历史行原样保留(V5 前每次 record 整体重写)。"""
    rl = make(tmp_path)
    rl.record(T0)
    first = (tmp_path / "rl.json").read_text(encoding="utf-8")
    rl.record(T0 + timedelta(minutes=5))
    second = (tmp_path / "rl.json").read_text(encoding="utf-8")
    assert second.startswith(first)                 # 旧行字节不变
    assert second.count("\n") == first.count("\n") + 1  # 恰好新增一行
    assert all(isinstance(json.loads(ln), str) for ln in second.splitlines())


def test_v5_legacy_dict_state_still_enforced(tmp_path: Path) -> None:
    """旧版整文件 JSON 状态仍可读取,频控照常生效(部署升级零破坏)。"""
    p = tmp_path / "rl.json"
    p.write_text(json.dumps({"timestamps": [T0.isoformat()]}, indent=2), encoding="utf-8")
    rl = make(tmp_path)
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False   # 间隔拦截
    assert rl.can_submit(T0 + timedelta(seconds=60))[0] is True


def test_v5_legacy_state_migrated_to_jsonl_on_record(tmp_path: Path) -> None:
    """首次 record 把旧版 dict 格式整体迁移为 JSONL(按时间排序)。"""
    later = T0 + timedelta(minutes=1)
    p = tmp_path / "rl.json"
    p.write_text(json.dumps({"timestamps": [later.isoformat()]}, indent=2), encoding="utf-8")
    rl = make(tmp_path)
    rl.record(T0)  # 早于已有记录:重写时按时间排序
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == [json.dumps(T0.isoformat()), json.dumps(later.isoformat())]


def test_v5_corrupt_line_self_heals_on_record(tmp_path: Path) -> None:
    """损坏行:can_submit 按空状态放行;record 自愈重写清除垃圾行。"""
    p = tmp_path / "rl.json"
    p.write_text("garbage-line\n", encoding="utf-8")
    rl = make(tmp_path)
    assert rl.can_submit(T0) == (True, "")
    rl.record(T0)
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == [json.dumps(T0.isoformat())]   # 垃圾行已被清除
    assert rl.can_submit(T0 + timedelta(seconds=10))[0] is False


def test_v5_bare_iso_line_tolerated(tmp_path: Path) -> None:
    """手工写入的裸 ISO 行(无 JSON 引号)同样可解析。"""
    p = tmp_path / "rl.json"
    p.write_text(T0.isoformat() + "\n", encoding="utf-8")
    rl = make(tmp_path)
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False


def test_v5_blocked_counter_on_both_rejection_reasons(tmp_path: Path) -> None:
    """间隔拒绝与当日上限拒绝各计一次 ratelimit.blocked;放行不计。"""
    telemetry.reset()
    rl = make(tmp_path, min_interval_s=60, max_per_day=1)
    rl.record(T0)
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False      # 间隔拒绝
    assert rl.can_submit(T0 + timedelta(hours=1))[0] is False        # 当日上限拒绝
    assert rl.can_submit(T0 + timedelta(days=1))[0] is True          # 跨天放行
    assert telemetry.snapshot()["counters"].get("ratelimit.blocked") == 2.0


def test_v5_single_pass_latest_and_daily_count(tmp_path: Path) -> None:
    """乱序落盘(文件内时间戳非升序)时单遍扫描仍正确取最近一次 + 当日数。"""
    p = tmp_path / "rl.json"
    t1 = T0
    t2 = T0 + timedelta(hours=1)
    t3 = T0 + timedelta(hours=2)
    for stamp in (t2, t1, t3):  # 故意乱序写入
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stamp.isoformat()) + "\n")
    rl = make(tmp_path, min_interval_s=60, max_per_day=3)
    # 最近一次是 t3:距 t3 不足间隔 → 拒绝
    assert rl.can_submit(t3 + timedelta(seconds=30))[0] is False
    # 当日已有 3 次 = 上限 → 即使间隔足够也拒绝
    assert rl.can_submit(t3 + timedelta(minutes=5))[0] is False
