"""A39 巡查调度器(netsentinel.ops.scheduler)单元测试。

全部离线:run_scan / sleep / memory / notify / random / graph 一律注入
fake,零网络、零真实 sleep;不依赖兄弟模块(A32 notify.hub / A33
site_memory)是否就位(缺省工厂的"未就位降级"路径本身也被覆盖)。
A211 动态 TTL(V10.4 temporal 联动)追加专节:开关默认关 = 现状快照
逐字节一致;开启 = 爆发站点建议 TTL 收缩、无边站点用库级默认、图谱
异常安全降级;红线 35 = 频控 / 礼貌间隔参数零引用、停顿节奏不变。
"""
from __future__ import annotations

import datetime as _dt
import logging
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

import netsentinel.ops.scheduler as scheduler
from netsentinel import telemetry
from netsentinel.contracts import Config, Verdict
from netsentinel.intel.temporal import burst_factor, kleinberg_bursts, suggest_ttl
from netsentinel.ops.scheduler import WatchItem, load_watchlist, main, run_once

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

SITE_A = "https://site-a.example.com/"
SITE_B = "https://site-b.example.com/list"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeSleep:
    """记录每次停顿时长,绝不真实等待。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


class FakeMemory:
    """A33 SiteMemory 契约 fake:记住每站点指纹,供下一轮去重判断。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.remembered: list[tuple[str, str]] = []

    def fingerprint(self, report: Any) -> str:
        return "fp:" + str(getattr(report, "site_url", ""))

    def remember(self, site_url: str, fp: str) -> None:
        self.store[site_url] = fp
        self.remembered.append((site_url, fp))

    def last_fingerprint(self, site_url: str) -> str | None:
        return self.store.get(site_url)

    def should_rescan(self, site_url: str, fp: str) -> tuple[bool, str]:
        if self.store.get(site_url) == fp:
            return (False, "指纹一致且未过 TTL(fake)")
        return (True, "")


def make_report(
    url: str,
    *,
    verdict: Verdict = Verdict.NSFW,
    agg: float = 0.97,
    needs_review: bool = True,
) -> SimpleNamespace:
    """鸭子类型的 SiteReport fake(调度器只读 verdict/agg/needs_review/site_url)。"""
    return SimpleNamespace(
        site_url=url, verdict=verdict, agg_nsw_prob=agg, needs_review=needs_review
    )


def make_cfg(tmp_path: pathlib.Path) -> Config:
    """把所有落盘路径都收进 tmp_path 的 Config(含 watchlist 路径)。"""
    data = tmp_path / "data"
    return Config(
        watchlist_path=str(tmp_path / "watchlist.yaml"),
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


def write(path: pathlib.Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def ok_notify(cfg: Any, event: str, text: str) -> bool:
    """注入用通知 fake:记录调用并报告成功。"""
    ok_notify.calls.append((event, text))  # type: ignore[attr-defined]
    return True


ok_notify.calls = []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# load_watchlist:缺失 / 三种格式 / 损坏 / enabled
# ---------------------------------------------------------------------------
def test_load_watchlist_missing_returns_empty_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        items = load_watchlist(str(tmp_path / "nope.yaml"))
    assert items == []
    assert "未找到 watchlist" in caplog.text


def test_load_watchlist_empty_and_comment_only(tmp_path):
    assert load_watchlist(write(tmp_path / "w.yaml", "")) == []
    assert load_watchlist(write(tmp_path / "w.yaml", "# 只有注释\n\n# 另一行\n")) == []
    assert load_watchlist(write(tmp_path / "w.yaml", "items: []\n")) == []


def test_load_watchlist_yaml_list_form(tmp_path):
    text = (
        "- https://a.example.com/\n"
        "- url: https://b.example.com/\n"
        "  note: 复访站点\n"
        "  enabled: false\n"
    )
    items = load_watchlist(write(tmp_path / "w.yaml", text))
    assert items == [
        WatchItem(url="https://a.example.com/"),
        WatchItem(url="https://b.example.com/", note="复访站点", enabled=False),
    ]


def test_load_watchlist_yaml_items_mapping(tmp_path):
    text = (
        "items:\n"
        "  - url: https://a.example.com/\n"
        "    note: \"示例\"\n"
        "    enabled: true\n"
        "  - url: https://b.example.com/\n"
    )
    items = load_watchlist(write(tmp_path / "w.yaml", text))
    assert [it.url for it in items] == ["https://a.example.com/", "https://b.example.com/"]
    assert items[0].note == "示例" and items[0].enabled is True
    assert items[1].note == "" and items[1].enabled is True


def test_load_watchlist_plain_text(tmp_path):
    text = (
        "# 巡查目标(纯文本:每行一个 URL)\n"
        "\n"
        "https://a.example.com/\n"
        "https://b.example.com/path\n"
    )
    items = load_watchlist(write(tmp_path / "w.txt", text))
    assert items == [
        WatchItem(url="https://a.example.com/"),
        WatchItem(url="https://b.example.com/path"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "items: [1, 2\n",                       # 未闭合流式序列 → YAML 解析错误
        "foo: bar: baz\n",                       # 非法映射 → YAML 解析错误
        "foo: bar\n",                            # 顶层映射缺 items 列表
        "not-a-url\n",                           # 纯文本行不是合法 URL
        "items:\n  - 123\n",                     # 条目既非字符串也非映射
        "items:\n  - url: 42\n",                 # url 字段类型错误
        "items:\n  - url: https://a.example.com/\n    enabled: 是\n",
    ],
)
def test_load_watchlist_corrupted_raises_value_error(tmp_path, text):
    with pytest.raises(ValueError):
        load_watchlist(write(tmp_path / "w.yaml", text))


def test_load_watchlist_plain_text_without_pyyaml(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_import_yaml_optional", lambda: None)
    items = load_watchlist(write(tmp_path / "w.txt", "https://a.example.com/\n"))
    assert items == [WatchItem(url="https://a.example.com/")]
    with pytest.raises(ValueError, match="PyYAML"):
        load_watchlist(write(tmp_path / "w.yaml", "items:\n  - url: https://a.example.com/\n"))


def test_load_watchlist_example_file_is_valid():
    items = load_watchlist(str(REPO_ROOT / "watchlist.example.yaml"))
    assert len(items) == 3
    assert all(it.url.startswith("https://") for it in items)
    assert [it.enabled for it in items] == [True, True, False]


# ---------------------------------------------------------------------------
# run_once:空表 / 全量扫描+通知 / CLEAN / 失败续跑 / enabled 过滤
# ---------------------------------------------------------------------------
def test_run_once_empty_watchlist_summary_all_zero(tmp_path):
    cfg = make_cfg(tmp_path)  # watchlist 不存在 → []
    calls: list[str] = []

    def scan(url, cfg):  # pragma: no cover - 不应被调用
        calls.append(url)
        return make_report(url)

    summary = run_once(
        cfg, run_scan=scan, sleep=FakeSleep(), memory=FakeMemory(), notify=ok_notify
    )
    assert summary == {
        "total": 0, "scanned": 0, "skipped": 0,
        "failed": 0, "notified": 0, "errors": [],
    }
    assert calls == []


def test_run_once_scans_both_and_notifies(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    seen: list[tuple[str, Config]] = []

    def scan(url, cfg):
        seen.append((url, cfg))
        return make_report(url)  # NSFW / needs_review=True / agg=0.97

    pause = FakeSleep()
    memory = FakeMemory()
    ok_notify.calls.clear()
    summary = run_once(
        cfg, run_scan=scan, sleep=pause, memory=memory, notify=ok_notify,
        random=lambda: 0.0,
    )
    assert summary["total"] == 2
    assert summary["scanned"] == 2
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    assert summary["errors"] == []
    assert summary["notified"] == 2  # 两项均 needs_review
    assert [u for u, _ in seen] == [SITE_A, SITE_B]  # 顺序扫描
    assert all(c is cfg for _, c in seen)  # cfg 原样透传
    # 通知事件与文本:站点 + verdict + agg(保留两位小数)
    assert [ev for ev, _ in ok_notify.calls] == ["pending_review", "pending_review"]
    assert all(
        t.startswith(SITE_A) or t.startswith(SITE_B) for _, t in ok_notify.calls
    )
    assert all("verdict=nsfw" in t and "agg=0.97" in t for _, t in ok_notify.calls)
    # 每个扫描项之后都礼貌停顿一次(注入 random=0 → 恰为下界 1.0)
    assert pause.calls == [1.0, 1.0]
    # 先扫后记:两个站点都已写入指纹记忆
    assert sorted(memory.remembered) == [(SITE_A, f"fp:{SITE_A}"), (SITE_B, f"fp:{SITE_B}")]


def test_run_once_sleep_jitter_range(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    pause = FakeSleep()
    run_once(
        cfg, run_scan=lambda url, cfg: make_report(url), sleep=pause,
        memory=FakeMemory(), notify=ok_notify,  # random 走缺省 random.random
    )
    assert len(pause.calls) == 2
    assert all(1.0 <= s < 1.5 for s in pause.calls)


def test_run_once_clean_site_not_notified(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n")
    cfg = make_cfg(tmp_path)
    ok_notify.calls.clear()
    summary = run_once(
        cfg,
        run_scan=lambda url, cfg: make_report(
            url, verdict=Verdict.CLEAN, agg=0.02, needs_review=False
        ),
        sleep=FakeSleep(), memory=FakeMemory(), notify=ok_notify,
    )
    assert summary["scanned"] == 1
    assert summary["notified"] == 0  # CLEAN 不通知
    assert ok_notify.calls == []


def test_run_once_single_failure_continues(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)

    def scan(url, cfg):
        if url == SITE_A:
            raise RuntimeError("站点抓取超时(fake)")
        return make_report(url)

    ok_notify.calls.clear()
    pause = FakeSleep()
    summary = run_once(
        cfg, run_scan=scan, sleep=pause, memory=FakeMemory(), notify=ok_notify
    )
    assert summary["total"] == 2
    assert summary["failed"] == 1
    assert summary["scanned"] == 1  # 失败不中断:第二项照常扫描
    assert summary["notified"] == 1
    assert summary["errors"] == [(SITE_A, "站点抓取超时(fake)")]
    assert len(pause.calls) == 2  # 失败项与成功项之后都停顿


def test_run_once_disabled_items_are_skipped_from_total(tmp_path):
    text = (
        f"items:\n"
        f"  - url: {SITE_A}\n"
        f"  - url: {SITE_B}\n"
        f"    enabled: false\n"
    )
    write(tmp_path / "watchlist.yaml", text)
    cfg = make_cfg(tmp_path)
    seen: list[str] = []

    def scan(url, cfg):
        seen.append(url)
        return make_report(url)

    summary = run_once(
        cfg, run_scan=scan, sleep=FakeSleep(), memory=FakeMemory(), notify=ok_notify
    )
    assert summary["total"] == 1  # enabled=False 不计入、不扫描
    assert seen == [SITE_A]


# ---------------------------------------------------------------------------
# run_once:第二轮同指纹 → 跳过;通知异常不拖垮巡查
# ---------------------------------------------------------------------------
def test_run_once_second_round_same_fingerprint_skips_all(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    memory = FakeMemory()
    seen: list[str] = []
    pause1, pause2 = FakeSleep(), FakeSleep()

    def scan(url, cfg):
        seen.append(url)
        return make_report(url)

    run_once(cfg, run_scan=scan, sleep=pause1, memory=memory, notify=ok_notify)
    assert seen == [SITE_A, SITE_B]

    seen.clear()
    ok_notify.calls.clear()
    summary2 = run_once(cfg, run_scan=scan, sleep=pause2, memory=memory, notify=ok_notify)
    # FakeMemory 记住了上轮指纹:本轮全部跳过,不触网、不通知、不停顿
    assert summary2["total"] == 2
    assert summary2["skipped"] == 2
    assert summary2["scanned"] == 0
    assert summary2["notified"] == 0
    assert seen == []
    assert pause2.calls == []
    assert ok_notify.calls == []


def test_run_once_skip_disabled_via_flag(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n")
    cfg = make_cfg(tmp_path)
    memory = FakeMemory()
    run_once(
        cfg, run_scan=lambda url, cfg: make_report(url),
        sleep=FakeSleep(), memory=memory, notify=ok_notify,
    )
    seen: list[str] = []
    summary2 = run_once(
        cfg, run_scan=lambda url, cfg: seen.append(url) or make_report(url),
        sleep=FakeSleep(), memory=memory, notify=ok_notify,
        skip_if_unchanged=False,  # 强制全量重扫(仍会记忆)
    )
    assert summary2["scanned"] == 1 and summary2["skipped"] == 0
    assert seen == [SITE_A]


def test_run_once_notify_exception_does_not_break_round(tmp_path):
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n")
    cfg = make_cfg(tmp_path)

    def bad_notify(cfg, event, text):
        raise RuntimeError("webhook 不可达(fake)")

    summary = run_once(
        cfg, run_scan=lambda url, cfg: make_report(url),
        sleep=FakeSleep(), memory=FakeMemory(), notify=bad_notify,
    )
    assert summary["scanned"] == 1
    assert summary["notified"] == 0  # 单发失败不重试、不计数
    assert summary["failed"] == 0


def test_run_once_default_memory_degrades_gracefully(tmp_path, monkeypatch, caplog):
    """site_memory(A33)未就位时:告警降级为不去重,本轮照常完成。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n")
    cfg = make_cfg(tmp_path)

    def broken_memory(cfg):
        raise RuntimeError("模块 netsentinel.intel.site_memory 未就位(fake)")

    monkeypatch.setattr(scheduler, "_default_memory", broken_memory)
    with caplog.at_level(logging.WARNING):
        summary = run_once(
            cfg, run_scan=lambda url, cfg: make_report(url),
            sleep=FakeSleep(), notify=ok_notify,
        )
    assert summary["scanned"] == 1 and summary["skipped"] == 0
    assert "不去重" in caplog.text


# ---------------------------------------------------------------------------
# main CLI:--list / --once(全离线注入)
# ---------------------------------------------------------------------------
def write_cli_env(tmp_path: pathlib.Path) -> Config:
    """写一份指向 tmp 的 config.yaml + watchlist,并返回对应 Config。"""
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        f"items:\n"
        f"  - url: {SITE_A}\n"
        f"    note: 复查中\n"
        f"  - url: {SITE_B}\n"
        f"    enabled: false\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "watchlist_path: " + watchlist.as_posix() + "\n"
        "data_dir: " + (tmp_path / "data").as_posix() + "\n"
        "db_path: " + (data / "review_queue.db").as_posix() + "\n"
        "audit_path: " + (data / "audit.jsonl").as_posix() + "\n"
        "log_path: " + (data / "logs" / "netsentinel.log").as_posix() + "\n",
        encoding="utf-8",
    )
    return Config(
        watchlist_path=str(watchlist),
        data_dir=str(data),
        db_path=str(data / "review_queue.db"),
        audit_path=str(data / "audit.jsonl"),
        log_path=str(data / "logs" / "netsentinel.log"),
    )


def test_main_list_prints_watchlist(tmp_path, capsys):
    write_cli_env(tmp_path)
    rc = main(["--config", str(tmp_path / "config.yaml"), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert SITE_A in out and SITE_B in out
    assert "2 个条目" in out and "1 启用 / 1 停用" in out
    assert "[启用]" in out and "[停用]" in out
    assert "备注:复查中" in out


def test_main_list_empty_watchlist(tmp_path, capsys):
    cfg = write_cli_env(tmp_path)
    pathlib.Path(cfg.watchlist_path).unlink()
    rc = main(["--config", str(tmp_path / "config.yaml"), "--list"])
    assert rc == 0
    assert "无巡查目标" in capsys.readouterr().out


def test_main_once_runs_single_round_offline(tmp_path, capsys, monkeypatch):
    write_cli_env(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        scheduler, "_default_run_scan",
        lambda url, cfg: seen.append(url) or make_report(url),
    )
    pause = FakeSleep()
    monkeypatch.setattr(scheduler, "_default_sleep", pause)
    ok_notify.calls.clear()
    monkeypatch.setattr(scheduler, "_default_notify", ok_notify)

    rc = main(["--config", str(tmp_path / "config.yaml"), "--once"])
    assert rc == 0
    assert seen == [SITE_A]  # 只有启用项被扫描
    assert len(pause.calls) == 1 and 1.0 <= pause.calls[0] < 1.5  # 零真实 sleep
    out = capsys.readouterr().out
    assert "本轮巡查完成" in out
    assert "总计 1 项,已扫描 1" in out and "已通知 1" in out


def test_main_corrupted_watchlist_returns_two(tmp_path, capsys):
    cfg = write_cli_env(tmp_path)
    pathlib.Path(cfg.watchlist_path).write_text("foo: bar\n", encoding="utf-8")
    rc = main(["--config", str(tmp_path / "config.yaml"), "--once"])
    assert rc == 2
    assert "watchlist" in capsys.readouterr().out


def test_main_rejects_bad_interval(tmp_path):
    write_cli_env(tmp_path)
    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "config.yaml"), "--loop", "--interval-min", "0"])


# ---------------------------------------------------------------------------
# V5 升级:单遍解析(Windows 行尾)、遥测(round/item timer + 三计数器)、
# --loop 优雅退出
# ---------------------------------------------------------------------------
def test_v5_load_watchlist_single_pass_crlf_line_endings(tmp_path):
    """单遍行扫描兼容 Windows CRLF:YAML 与纯文本两种格式均正确解析。"""
    yaml_text = (
        "items:\r\n"
        "  - url: https://a.example.com/\r\n"
        "    note: Windows 记事本\r\n"
        "  - url: https://b.example.com/\r\n"
        "    enabled: false\r\n"
    )
    items = load_watchlist(write(tmp_path / "w.yaml", yaml_text))
    assert [it.url for it in items] == ["https://a.example.com/", "https://b.example.com/"]
    assert items[0].note == "Windows 记事本"
    assert items[1].enabled is False

    plain_text = "# 注释\r\nhttps://a.example.com/\r\n\r\nhttps://b.example.com/x\r\n"
    plain = load_watchlist(write(tmp_path / "w.txt", plain_text))
    assert plain == [
        WatchItem(url="https://a.example.com/"),
        WatchItem(url="https://b.example.com/x"),
    ]


def test_v5_run_once_telemetry_round_item_and_counters(tmp_path):
    """round/item 计时与 scanned/skipped/failed 计数器逐项对账。"""
    site_c = "https://site-c.example.com/"
    write(
        tmp_path / "watchlist.yaml",
        f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n  - url: {site_c}\n",
    )
    cfg = make_cfg(tmp_path)
    memory = FakeMemory()
    memory.store[SITE_A] = memory.fingerprint(make_report(SITE_A))  # 预记 → 跳过

    def scan(url, cfg):
        if url == SITE_B:
            raise RuntimeError("失败(fake)")
        return make_report(url)

    telemetry.reset()
    summary = run_once(
        cfg, run_scan=scan, sleep=FakeSleep(), memory=memory,
        notify=ok_notify, random=lambda: 0.0,
    )
    assert (summary["skipped"], summary["scanned"], summary["failed"]) == (1, 1, 1)
    snap = telemetry.snapshot()
    assert snap["counters"]["scheduler.skipped"] == 1
    assert snap["counters"]["scheduler.scanned"] == 1
    assert snap["counters"]["scheduler.failed"] == 1
    assert snap["timers"]["scheduler.round"]["count"] == 1   # 整轮一次
    assert snap["timers"]["scheduler.item"]["count"] == 3    # 每个启用条目一次


def test_v5_run_once_telemetry_skip_counter_second_round(tmp_path):
    """第二轮同指纹全跳过:skipped 计数与 item 计时样本数一致。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    memory = FakeMemory()
    run_once(
        cfg, run_scan=lambda url, cfg: make_report(url),
        sleep=FakeSleep(), memory=memory, notify=ok_notify,
    )
    telemetry.reset()
    summary2 = run_once(
        cfg, run_scan=lambda url, cfg: make_report(url),
        sleep=FakeSleep(), memory=memory, notify=ok_notify,
    )
    assert summary2["skipped"] == 2
    snap = telemetry.snapshot()
    assert snap["counters"]["scheduler.skipped"] == 2
    assert snap["counters"].get("scheduler.scanned", 0) == 0
    assert snap["timers"]["scheduler.item"]["count"] == 2


def test_v5_main_loop_keyboardinterrupt_graceful_exit(tmp_path, capsys, monkeypatch):
    """--loop 模式:KeyboardInterrupt(SIGINT)优雅退出,退出码 0。"""
    write_cli_env(tmp_path)
    rounds: list[Config] = []

    def fake_run_once(cfg):
        rounds.append(cfg)
        if len(rounds) >= 2:
            raise KeyboardInterrupt  # 第二轮结束后、轮间隔前收到 Ctrl-C
        return {
            "total": 1, "scanned": 1, "skipped": 0,
            "failed": 0, "notified": 0, "errors": [],
        }

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    pause = FakeSleep()
    monkeypatch.setattr(scheduler, "_default_sleep", pause)

    rc = main(
        ["--config", str(tmp_path / "config.yaml"), "--loop", "--interval-min", "1"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "优雅退出" in out
    assert len(rounds) == 2
    # 两轮之间恰等了一个轮间隔(1 分钟,注入 fake 不真睡);中断后不再等待
    assert pause.calls == [60.0]


# ---------------------------------------------------------------------------
# A211 动态 TTL:cfg.dynamic_ttl(默认关)→ 图谱边时间戳 → temporal 爆发链
# → should_rescan(..., ttl_hours=建议值);频控 / 礼貌间隔零触碰(红线 35)
# ---------------------------------------------------------------------------
#: 区分"未传 ttl_hours 关键字"与"传了 None"(现状调用形态逐字节一致断言)
_MISSING = object()

#: 动态 TTL 测试的固定时间基:事件 = 基准时刻 + 偏移小时;now 同量纲
_DYN_BASE = _dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc)
_DYN_UNIX = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

GANG_MATE = "https://gang-mate.example.com/"      # 与爆发站点关联的"同伙"
UNRELATED_A = "https://unrelated-a.example.com/"  # 与 watchlist 无关的站点对
UNRELATED_B = "https://unrelated-b.example.com/"


class TtlSpyMemory(FakeMemory):
    """FakeMemory 扩展:记录每次 should_rescan 是否携带 ttl_hours 关键字。"""

    def __init__(self) -> None:
        super().__init__()
        self.ttl_calls: list[tuple[str, object]] = []

    def should_rescan(self, site_url: str, fp: str, ttl_hours: object = _MISSING):
        self.ttl_calls.append((site_url, ttl_hours))
        return super().should_rescan(site_url, fp)


class AgingMemory(FakeMemory):
    """SiteMemory 语义 fake:指纹未变时按"距上次更新 age_hours vs 生效 TTL"决策。

    与真实 :class:`~netsentinel.intel.site_memory.SiteMemory` 同款四分支中
    与 TTL 相关的两支:指纹不同 → 必扫;指纹相同但 age ≥ 生效 TTL(库级
    ``ttl_hours`` 或单次覆写 ``ttl_hours`` 经 int() 截断)→ 必扫;否则跳过。
    """

    ttl_hours = 72  # 同名属性:scheduler 取基准 TTL 用(与真实 SiteMemory 一致)

    def __init__(self, age_hours: float = 30.0) -> None:
        super().__init__()
        self.age_hours = float(age_hours)
        self.ttl_calls: list[tuple[str, float | None]] = []

    def should_rescan(
        self, site_url: str, fp: str, ttl_hours: float | None = None
    ) -> tuple[bool, str]:
        self.ttl_calls.append((site_url, ttl_hours))
        if self.store.get(site_url) != fp:
            return (True, "首次收录或内容已变化(fake)")
        effective = self.ttl_hours if ttl_hours is None else int(ttl_hours)
        if self.age_hours >= effective:
            return (True, "超过 TTL 需复查(fake)")
        return (False, f"指纹未变化,未超 TTL({effective} 小时)(fake),本轮跳过")


class FakeGraph:
    """EvidenceGraph 只读消费口(export_json)的 fake:返回预置边集并计数。"""

    def __init__(self, edges: list[dict[str, Any]]) -> None:
        self.edges = list(edges)
        self.export_calls = 0

    def export_json(self) -> dict[str, list[dict[str, Any]]]:
        self.export_calls += 1
        return {"nodes": [], "edges": self.edges}


class ExplodingGraph:
    """export_json 即抛错的 fake:验证图谱查询失败的安全降级路径。"""

    def __init__(self) -> None:
        self.export_calls = 0

    def export_json(self) -> dict[str, Any]:
        self.export_calls += 1
        raise RuntimeError("图谱库损坏(fake)")


def _dyn_edge(src: str, dst: str, kind: str, created_at: str) -> dict[str, Any]:
    """构造一条形如真实图谱 export_json 输出的边(src/dst 带 site: 前缀)。"""
    return {
        "src": f"site:{src}",
        "dst": f"site:{dst}",
        "kind": kind,
        "weight": 1.0,
        "created_at": created_at,
    }


def _dyn_at(h_offset: float) -> str:
    """基准时刻 + h_offset 小时 → ISO8601 串(带 UTC 时区,同 graph._now_iso 形态)。"""
    return (_DYN_BASE + _dt.timedelta(hours=h_offset)).isoformat(timespec="seconds")


def _dyn_now(h_offset: float = 302.0) -> float:
    """固定"当前时刻"(自 Unix epoch 的小时数):落在密集簇 [300, 302.9] 内。"""
    return (_DYN_BASE - _DYN_UNIX).total_seconds() / 3600.0 + h_offset


def _dyn_event_hours() -> list[float]:
    """爆发站点的事件偏移小时:0~216 每 24h 一条的稀疏背景 + 密集簇 [300, 302.9]。"""
    background = [24.0 * i for i in range(10)]
    cluster = [300.0 + 0.1 * i for i in range(30)]
    return sorted(background + cluster)


def _dyn_burst_graph() -> FakeGraph:
    """带密集 phash_near/redirect 边的 mock 图谱(含两类"不得贡献"的干扰边)。"""
    edges = [
        _dyn_edge(SITE_A, GANG_MATE, "phash_near", _dyn_at(h))
        for h in _dyn_event_hours()
    ]
    edges.append(_dyn_edge(SITE_A, GANG_MATE, "redirect", _dyn_at(300.05)))
    # 干扰 1:接触 SITE_B 的 shared_image 密集边 —— 边种过滤,不得让 B 产生建议 TTL
    edges += [
        _dyn_edge(SITE_B, GANG_MATE, "shared_image", _dyn_at(300.0 + 0.1 * i))
        for i in range(30)
    ]
    # 干扰 2:与 watchlist 无关站点对的 phash_near 密集边 —— 端点过滤,不得贡献 A
    edges += [
        _dyn_edge(UNRELATED_A, UNRELATED_B, "phash_near", _dyn_at(300.0 + 0.1 * i))
        for i in range(30)
    ]
    return FakeGraph(edges)


def _dyn_expected_ttl(base_ttl_h: float = 72.0) -> float:
    """用同一份事件集直接跑 temporal 链,得到建议 TTL 的期望值(确定性)。"""
    base_hours = (_DYN_BASE - _DYN_UNIX).total_seconds() / 3600.0
    events = [base_hours + h for h in _dyn_event_hours()] + [base_hours + 300.05]
    return suggest_ttl(base_ttl_h, burst_factor(kleinberg_bursts(events), _dyn_now()))


def test_dyn_ttl_default_off_matches_status_quo_snapshot(tmp_path, monkeypatch):
    """开关默认关(cfg.dynamic_ttl 一等字段缺省 False):图谱零咨询、调用形态与现状一致。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    # V13 收录批预改(A201/A210 追认先例):dynamic_ttl 升格为一等字段,
    # 断言语义由"属性不存在"改为"一等字段且默认 False"(getattr 缺省口径不变)
    assert hasattr(cfg, "dynamic_ttl")
    assert cfg.dynamic_ttl is False  # 缺省 False = 现状(动态 TTL 关)

    graph = ExplodingGraph()  # 若被咨询即抛错 → 证明零 IO

    def boom_default_graph(cfg):  # pragma: no cover - 不应被调用
        raise AssertionError("动态 TTL 关闭时不得打开缺省图谱")

    monkeypatch.setattr(scheduler, "_default_graph", boom_default_graph)
    memory = TtlSpyMemory()

    # 现状快照:第一轮全扫 + 每项一次停顿;第二轮同指纹全跳过、零停顿
    pause1 = FakeSleep()
    s1 = run_once(
        cfg, run_scan=lambda u, c: make_report(u), sleep=pause1, memory=memory,
        graph=graph, notify=ok_notify, random=lambda: 0.0,
    )
    assert (s1["scanned"], s1["skipped"], s1["failed"]) == (2, 0, 0)
    assert pause1.calls == [1.0, 1.0]

    pause2 = FakeSleep()
    telemetry.reset()
    s2 = run_once(
        cfg, run_scan=lambda u, c: make_report(u), sleep=pause2, memory=memory,
        graph=graph, notify=ok_notify, random=lambda: 0.0,
    )
    assert (s2["scanned"], s2["skipped"]) == (0, 2)
    assert pause2.calls == []

    # 图谱从未被打开 / 查询;should_rescan 从未收到 ttl_hours 关键字(逐字节同现状)。
    # 第一轮无记忆指纹、现状即不咨询 should_rescan;第二轮两次咨询也均为原形态。
    assert graph.export_calls == 0
    assert len(memory.ttl_calls) == 2
    assert all(ttl is _MISSING for _, ttl in memory.ttl_calls)
    assert telemetry.snapshot()["counters"].get("scheduler.dyn_ttl.degraded", 0) == 0


def test_dyn_ttl_on_burst_site_shrinks_ttl_edgeless_site_uses_default(
    tmp_path, monkeypatch
):
    """开启后:爆发站点建议 TTL 收缩并触发重扫;无边站点沿用库级默认 TTL。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    cfg.dynamic_ttl = True
    graph = _dyn_burst_graph()
    monkeypatch.setattr(scheduler, "_now_hours", lambda: _dyn_now())

    memory = AgingMemory(age_hours=30.0)  # 两站指纹均未变、距上次 30 小时
    for url in (SITE_A, SITE_B):
        memory.store[url] = memory.fingerprint(make_report(url))

    seen: list[str] = []
    pause = FakeSleep()
    ok_notify.calls.clear()
    summary = run_once(
        cfg,
        run_scan=lambda u, c: seen.append(u) or make_report(u),
        sleep=pause, memory=memory, graph=graph, notify=ok_notify,
        random=lambda: 0.0,
    )

    # 爆发站点 A:收到的建议 TTL = temporal 链对同一事件集的输出,收缩且未越下限
    expected = _dyn_expected_ttl()
    assert expected < 72.0 and expected >= 6.0
    assert memory.ttl_calls[0][0] == SITE_A
    ttl_a = memory.ttl_calls[0][1]
    assert isinstance(ttl_a, float) and 6.0 <= ttl_a < 72.0
    assert ttl_a == pytest.approx(expected)
    # 无边站点 B(其密集边是 shared_image,不在 DYN_TTL_EDGE_KINDS 内)→ None = 库级默认
    assert memory.ttl_calls[1] == (SITE_B, None)

    # 消费效果:A 的建议 TTL(截断后 < 30h)→ 指纹未变也必须重扫;B 沿用 72h → 跳过
    assert seen == [SITE_A]
    assert (summary["scanned"], summary["skipped"], summary["failed"]) == (1, 1, 0)
    # 停顿节奏与现状完全相同:只有真实触网的 A 停顿一次,公式仍是 1.0 + 0.5*random
    assert pause.calls == [1.0]
    # 每轮只做一次图谱查询(索引一次备好,循环内零查询)
    assert graph.export_calls == 1


def test_dyn_ttl_deterministic_same_inputs_same_outputs(tmp_path, monkeypatch):
    """确定性:同一图谱 / 时钟 / 记忆构造,两轮输出(摘要 + TTL 值 + 停顿)逐项相等。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    cfg.dynamic_ttl = True
    monkeypatch.setattr(scheduler, "_now_hours", lambda: _dyn_now())

    rounds: list[tuple[Any, Any, Any]] = []
    for _ in range(2):
        graph = _dyn_burst_graph()
        memory = AgingMemory(age_hours=30.0)
        for url in (SITE_A, SITE_B):
            memory.store[url] = memory.fingerprint(make_report(url))
        pause = FakeSleep()
        summary = run_once(
            cfg, run_scan=lambda u, c: make_report(u), sleep=pause,
            memory=memory, graph=graph, notify=ok_notify, random=lambda: 0.0,
        )
        rounds.append((summary, list(memory.ttl_calls), list(pause.calls)))

    assert rounds[0] == rounds[1]
    assert rounds[0][1][0][1] == pytest.approx(_dyn_expected_ttl())


def test_dyn_ttl_degrades_to_default_when_graph_query_fails(tmp_path, caplog):
    """图谱查询抛错:安全降级默认 TTL,遥测计数 scheduler.dyn_ttl.degraded,巡查照常。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    cfg.dynamic_ttl = True
    graph = ExplodingGraph()
    memory = AgingMemory()  # 空库:两站均"首次收录" → 全扫

    telemetry.reset()
    with caplog.at_level(logging.WARNING):
        summary = run_once(
            cfg, run_scan=lambda u, c: make_report(u), sleep=FakeSleep(),
            memory=memory, graph=graph, notify=ok_notify, random=lambda: 0.0,
        )

    assert (summary["scanned"], summary["skipped"], summary["failed"]) == (2, 0, 0)
    assert graph.export_calls == 1
    assert telemetry.snapshot()["counters"].get("scheduler.dyn_ttl.degraded") == 1
    assert all(ttl is None for _, ttl in memory.ttl_calls)  # 全部走库级默认 TTL
    assert "默认 TTL" in caplog.text


def test_dyn_ttl_degrades_when_default_graph_unavailable(tmp_path, monkeypatch):
    """未注入 graph 且缺省图谱工厂失败:同样安全降级,站点沿用库级默认 TTL。"""
    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n")
    cfg = make_cfg(tmp_path)
    cfg.dynamic_ttl = True

    def broken_default_graph(cfg):  # 模拟 intel.graph 未就位
        raise RuntimeError("模块 netsentinel.intel.graph 未就位(fake)")

    monkeypatch.setattr(scheduler, "_default_graph", broken_default_graph)
    memory = AgingMemory(age_hours=30.0)  # 30h < 库级 72h → 未降级语义下应跳过
    memory.store[SITE_A] = memory.fingerprint(make_report(SITE_A))

    telemetry.reset()
    summary = run_once(
        cfg, run_scan=lambda u, c: make_report(u), sleep=FakeSleep(),
        memory=memory, notify=ok_notify, random=lambda: 0.0,
    )
    assert (summary["skipped"], summary["scanned"]) == (1, 0)
    assert telemetry.snapshot()["counters"].get("scheduler.dyn_ttl.degraded") == 1
    assert memory.ttl_calls == [(SITE_A, None)]  # 未传覆写 → 库级默认


def test_dyn_ttl_with_real_evidence_graph_wiring(tmp_path, monkeypatch):
    """端到端接线:真实 EvidenceGraph(tmp 库)写边 → scheduler 只读消费出收缩 TTL。"""
    from netsentinel.intel.graph import EvidenceGraph

    write(tmp_path / "watchlist.yaml", f"items:\n  - url: {SITE_A}\n  - url: {SITE_B}\n")
    cfg = make_cfg(tmp_path)
    cfg.dynamic_ttl = True

    graph = EvidenceGraph(str(tmp_path / "graph.db"))
    clock = {"dt": _DYN_BASE}

    def fixed_now():  # 按写入次序推进图谱时钟,created_at 落在预定小时偏移上
        return clock["dt"]

    monkeypatch.setattr(graph, "_now", fixed_now)
    # 真实图谱以 (src, dst, kind) 为主键去重(重写只刷权重、保留首建 created_at),
    # 因此每个事件时间戳各写一条到不同"同伙"端点的边,才有 40 个独立事件
    for i, h in enumerate(_dyn_event_hours()):
        clock["dt"] = _DYN_BASE + _dt.timedelta(hours=h)
        graph.add_edge(
            SITE_A, f"https://gang-{i:02d}.example.com/", kind="phash_near", weight=1.0
        )
    clock["dt"] = _DYN_BASE + _dt.timedelta(hours=300.05)
    graph.add_edge(
        SITE_A, "https://gang-redirect.example.com/", kind="redirect", weight=1.0
    )

    monkeypatch.setattr(scheduler, "_now_hours", lambda: _dyn_now())
    memory = AgingMemory(age_hours=30.0)
    for url in (SITE_A, SITE_B):
        memory.store[url] = memory.fingerprint(make_report(url))

    telemetry.reset()  # 隔离前置用例累计的降级计数,本例必须零降级
    try:
        summary = run_once(
            cfg, run_scan=lambda u, c: make_report(u), sleep=FakeSleep(),
            memory=memory, graph=graph, notify=ok_notify, random=lambda: 0.0,
        )
    finally:
        graph.close()

    # 真实图谱的 site: 前缀节点 id 与 ISO created_at 被正确消费:A 收缩重扫、B 默认跳过
    assert memory.ttl_calls[0][1] == pytest.approx(_dyn_expected_ttl())
    assert memory.ttl_calls[1] == (SITE_B, None)
    assert (summary["scanned"], summary["skipped"]) == (1, 1)
    assert telemetry.snapshot()["counters"].get("scheduler.dyn_ttl.degraded", 0) == 0


def test_dyn_ttl_never_references_politeness_or_rate_limits():
    """红线 35:动态 TTL 路径零引用 fetch_delay / 频控参数;礼貌抖动常量保持 A39 基准。"""
    src = pathlib.Path(scheduler.__file__).read_text(encoding="utf-8")
    banned = (
        "fetch_delay",            # 全局礼貌间隔(crawler 链执行,scheduler 无权读取)
        "submit_min_interval",    # 举报频控(红线)
        "submit_max_per_day",     # 举报日上限(红线)
        "batch_item_interval",    # 批内提交间隔(红线)
        "discovery_query_delay",  # 引擎限速(红线)
        "rate_limit",             # 任何形态的限速字段
        "concurrency_tier",       # 并发档位(红线 35 同源约束)
    )
    for name in banned:
        assert name not in src, f"调度器源码不得引用礼貌/频控参数:{name}"
    # 礼貌抖动公式保持模块级纯数字常量(未被任何动态逻辑推导或放宽)
    assert scheduler.PAUSE_BASE_S == 1.0 and scheduler.PAUSE_JITTER_S == 0.5


def test_dyn_ttl_iso_hours_parsing_edges():
    """created_at → 小时换算:naive 按 UTC、带偏移换算等价、脏数据返回 None。"""
    assert scheduler._iso_hours("") is None
    assert scheduler._iso_hours(None) is None  # type: ignore[arg-type]
    assert scheduler._iso_hours("not-a-timestamp") is None
    naive = scheduler._iso_hours("2025-01-01T00:00:00")          # 无时区 → 按 UTC
    utc = scheduler._iso_hours("2025-01-01T00:00:00+00:00")       # UTC 偏移
    offset = scheduler._iso_hours("2024-12-31T19:00:00-05:00")    # 同一时刻的 -05:00 写法
    assert naive == pytest.approx(utc) and offset == pytest.approx(utc)
    assert naive == pytest.approx((_DYN_BASE - _DYN_UNIX).total_seconds() / 3600.0)
    # 量纲自证:1 小时后 = +1.0
    later = scheduler._iso_hours("2025-01-01T01:00:00+00:00")
    assert later - naive == pytest.approx(1.0)
