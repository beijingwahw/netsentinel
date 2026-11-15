"""A18 提交频控(RateLimiter)单元测试:全部离线,只写 tmp_path。

覆盖:间隔不足拒绝、当日超限拒绝、record 后状态变化与跨实例持久化、
损坏状态文件重置、跨天次数清零、aware/naive 时间混用、构造参数校验。
"""
from __future__ import annotations

import itertools
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


# ---------------------------------------------------------------------------
# 进程级互斥(文件锁):双线程并发 can_submit+record 验证不超发
# ---------------------------------------------------------------------------
def _read_all_stamps(path: Path) -> list[datetime]:
    """直接解析状态文件全部行(独立于被测代码的读取口径)。"""
    stamps: list[datetime] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        text = json.loads(line)
        stamps.append(datetime.fromisoformat(text.strip()))
    return stamps


def test_concurrent_can_submit_and_record_no_overissue(tmp_path: Path) -> None:
    """双线程并发 can_submit+record:状态零丢失、频控不放宽(不超发)。

    竞态场景(升级前):两线程同时触发"旧格式迁移"的整体重写,后写者
    覆盖前写者 → 丢时间戳行 → 当日计数被低估 → 频控被放宽 = 超发;
    并发只读还会读到半写文件(误判损坏 → 再触发自愈重写)。
    加锁后:读-改-写全程互斥,每日恰好用满上限、一条不丢、无坏行。

    线程分工:A 线程做 can_submit+record(顺序推进,当日上限应精确生效);
    B 线程同时只读 can_submit(并发读压测,不得抛错、不得影响状态)。
    """
    import threading

    state = tmp_path / "rl.json"
    # 旧版整文件 JSON(前 2 天 1 条):首次 record 走迁移重写的脏路径,
    # 是并发竞态暴露面最大的形态。
    day0 = T0 - timedelta(days=2)
    state.write_text(
        json.dumps({"timestamps": [day0.isoformat()]}, indent=2),
        encoding="utf-8",
    )
    max_per_day = 5
    rl = RateLimiter(
        str(state), min_interval_s=0, max_per_day=max_per_day,
        lock_stale_timeout_s=5.0,
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    reads_ok = 0
    read_lock = threading.Lock()

    def recorder() -> None:
        """A 线程:当日 8 次尝试,上限 5 → 恰好 5 次成功(不超发)。"""
        try:
            barrier.wait()
            for i in range(8):
                now = T0 + timedelta(minutes=i)
                ok, _reason = rl.can_submit(now)
                if ok:
                    rl.record(now)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须带回主线程
            errors.append(exc)

    def reader() -> None:
        """B 线程:并发只读 can_submit(不 record,验证读侧稳定)。"""
        nonlocal reads_ok
        try:
            barrier.wait()
            for i in range(60):
                ok, _reason = rl.can_submit(T0 + timedelta(minutes=i / 10))
                if ok:
                    with read_lock:
                        reads_ok += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=recorder), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "工作线程未在超时内结束(疑似锁死等)"

    assert not errors, f"工作线程抛错:{errors}"
    # 状态零丢失、无坏行:旧 1 条 + 当日恰好 5 条 = 6 条全部在盘且可解析
    # (升级前并发迁移重写会丢行 → 计数低估 → 频控被放宽)。
    stamps = _read_all_stamps(state)
    today = [s for s in stamps if s.date() == T0.date()]
    assert len(today) == max_per_day, f"当日记录 {len(today)} 次(期望恰好 {max_per_day})"
    assert len(stamps) == 1 + max_per_day
    assert day0 in stamps
    # 频控语义未被并发写放宽:当日已满额 → 再试一律拒绝(不超发)。
    ok, reason = rl.can_submit(T0 + timedelta(hours=10))
    assert ok is False and ("上限" in reason or "每日" in reason)
    # B 线程的并发读不落任何状态:文件行数不因只读而增长(已由上面断言覆盖)。


def test_concurrent_records_globally_ordered_no_lost_updates(
    tmp_path: Path,
) -> None:
    """双线程并发 record(时间戳全局有序):每一条都落盘,读-改-写零丢失。

    生产形态:两次提交本身隔着礼貌间隔(时间戳天然有序),竞态在状态文件
    的并发读-改-写(迁移重写互踩 / 半写覆盖)。全程加锁后 20 条并发写入
    一条不少——丢行即当日计数低估、频控被放宽(超发),本测试锁定之。

    注:并发判定(can_submit)与全局最近时间戳的交错属频控语义本身
    (晚到判定对照更晚的已记录提交必被拦),故本测试聚焦 record 的
    读-改-写互斥;并发读路径由上一测试的 reader 线程覆盖。
    """
    import threading

    state = tmp_path / "rl.json"
    day0 = T0 - timedelta(days=2)
    state.write_text(
        json.dumps({"timestamps": [day0.isoformat()]}, indent=2),
        encoding="utf-8",
    )
    rl = RateLimiter(
        str(state), min_interval_s=60, max_per_day=100, lock_stale_timeout_s=5.0
    )
    barrier = threading.Barrier(2)
    counter = itertools.count()
    counter_lock = threading.Lock()
    recorded: list[datetime] = []
    recorded_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(10):
                with counter_lock:  # 全局有序时间戳:模拟真实提交的先后次序
                    idx = next(counter)
                now = T0 + timedelta(seconds=61 * idx)
                rl.record(now)
                with recorded_lock:
                    recorded.append(now)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "工作线程未在超时内结束(疑似锁死等)"
    assert not errors, f"工作线程抛错:{errors}"
    assert len(recorded) == 20
    # 每一条 record 都在盘上(零丢失、无坏行),旧格式迁移也未丢历史。
    stamps = _read_all_stamps(state)
    assert len(stamps) == 21
    assert day0 in stamps
    # 频控未被放宽:距最近一条不足间隔 → 拒绝。
    latest = max(recorded)
    ok, reason = rl.can_submit(latest + timedelta(seconds=30))
    assert ok is False and "间隔" in reason


def test_state_file_lock_created_and_released(tmp_path: Path) -> None:
    """锁文件 <state>.lock 与状态文件同目录;with 退出后锁即释放(可再入)。"""
    import netsentinel.submit.rate_limit as rl_module

    state = tmp_path / "rl.json"
    rl = RateLimiter(str(state), min_interval_s=60, max_per_day=5)
    rl.record(T0)
    lock_path = state.with_name(state.name + ".lock")
    assert lock_path.exists()  # 锁文件长驻(不删除,避免释放-重建竞态)
    # 上一条 record 已走完 with(锁已释放):再次进出锁不会卡 stale 超时。
    with rl_module._StateFileLock(rl.state_path, stale_timeout_s=1.0):
        pass  # 立即拿到 = 上一把确实释放了
    rl.record(T0 + timedelta(minutes=1))


def test_lock_degrade_platform_without_primitives(tmp_path: Path, monkeypatch) -> None:
    """平台锁探测失败 → 降级为无锁 + 功能照常(频控绝不被锁故障误拦)。"""
    import netsentinel.submit.rate_limit as rl_module

    monkeypatch.setattr(rl_module, "_LOCK_FLAVOR", None)
    monkeypatch.setattr(rl_module, "_LOCK_FLAVOR_PROBED", True)
    rl = RateLimiter(str(tmp_path / "rl.json"), min_interval_s=60, max_per_day=5)
    rl.record(T0)
    assert rl.can_submit(T0 + timedelta(seconds=1))[0] is False
    assert len(_read_all_stamps(tmp_path / "rl.json")) == 1


def test_lock_stale_timeout_degrades_to_unlocked(tmp_path: Path) -> None:
    """锁被他方持有时,stale 超时后放弃等待、降级为无锁继续(不死等)。"""
    import threading
    import time as _time

    import netsentinel.submit.rate_limit as rl_module

    state = tmp_path / "rl.json"
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with rl_module._StateFileLock(state, stale_timeout_s=1.0):
            entered.set()
            release.wait(5.0)  # 持锁直到主线程放行(约 3s > 被测超时 0.5s)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(timeout=5)
    try:
        waiter = rl_module._StateFileLock(state, stale_timeout_s=0.5, poll_s=0.02)
        started = _time.monotonic()
        with waiter:  # 超时降级:不抛错、不死等
            elapsed = _time.monotonic() - started
        assert 0.4 <= elapsed < 2.5, f"等待耗时异常:{elapsed:.2f}s"
    finally:
        release.set()
        t.join(timeout=10)
    # 持有者退出后,新锁可正常立即获取(锁已释放,无残留)。
    with rl_module._StateFileLock(state, stale_timeout_s=1.0):
        pass


def test_locked_section_reentrant_no_deadlock(tmp_path: Path) -> None:
    """locked() 段内 can_submit/record/嵌套 locked() 免重入:不锁死、语义照常(V10.18)。

    段内的 can_submit / record 不再重复取锁(共享外层临界区);嵌套
    locked() 同样免重入;退出段后重入计数清零,常规调用恢复"各自持锁"。
    """
    state = tmp_path / "rl.jsonl"
    rl = RateLimiter(
        str(state), min_interval_s=0, max_per_day=5, lock_stale_timeout_s=5.0
    )
    with rl.locked() as rl_outer:
        assert rl_outer is rl  # locked() yield 频控器自身
        ok, reason = rl.can_submit(T0)
        assert ok is True, reason
        rl.record(T0)  # 段内 record 免重入(否则将死等自身的文件锁)
        with rl.locked():  # 嵌套 locked():免重入
            ok2, reason2 = rl.can_submit(T0 + timedelta(minutes=1))
            assert ok2 is True, reason2
            rl.record(T0 + timedelta(minutes=1))
    # 段外:重入计数已清零,常规路径行为与未用过 locked() 一致
    ok3, reason3 = rl.can_submit(T0 + timedelta(minutes=2))
    assert ok3 is True, reason3
    rl.record(T0 + timedelta(minutes=2))
    stamps = _read_all_stamps(state)
    assert stamps == [
        T0,
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=2),
    ]


def test_locked_closes_toctou_daily_quota_two_threads(tmp_path: Path) -> None:
    """双线程同时走 locked() 闭路段:当日配额不超发(判定→记账原子化)。

    竞态场景(无 locked() 时):两线程同时 can_submit 通过判定(各读到
    同一当日计数)→ 各自 record → 当日实际记账数超过 max_per_day = 超发
    (TOCTOU 决策间隙,V10.17 遗留③)。locked() 把"判定 → 记账"闭进同一
    文件锁临界区后,当日成功记账总数**恰好**等于 max_per_day,一条不超。
    """
    import threading

    state = tmp_path / "rl.jsonl"
    max_per_day = 3
    rl = RateLimiter(
        str(state), min_interval_s=0, max_per_day=max_per_day,
        lock_stale_timeout_s=5.0,
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    wins: list[int] = []
    wins_lock = threading.Lock()
    # 共享单调时间戳源:在临界区内取号,保证任何时刻"已落盘时间戳 <
    # 本次判定时刻"(时间戳乱序会触发最小间隔拒绝,混入无关变量)。
    tick_lock = threading.Lock()
    tick = itertools.count(1)

    def next_now() -> datetime:
        with tick_lock:
            return T0 + timedelta(minutes=next(tick))

    def worker() -> None:
        got = 0
        try:
            barrier.wait()  # 两线程同时开跑,最大化临界区争抢
            for _ in range(10):
                with rl.locked():
                    now = next_now()
                    ok, _reason = rl.can_submit(now)
                    if ok:
                        rl.record(now)
                        got += 1
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须带回主线程
            errors.append(exc)
        finally:
            with wins_lock:
                wins.append(got)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "工作线程未在超时内结束(疑似锁死等)"
    assert not errors, f"工作线程抛错:{errors}"
    # 不超发:两线程当日成功记账总数恰好等于上限(判定+记账同一临界区)
    total_wins = sum(wins)
    assert total_wins == max_per_day, (
        f"当日记账 {total_wins} 次(期望恰好 {max_per_day} 次)"
    )
    stamps = _read_all_stamps(state)
    assert len(stamps) == max_per_day
    assert all(s.date() == T0.date() for s in stamps)
    # 满额后再试一律拒绝(频控未被并发放宽)
    ok, reason = rl.can_submit(T0 + timedelta(hours=10))
    assert ok is False and "上限" in reason
