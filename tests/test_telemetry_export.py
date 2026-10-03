"""观测内核·指标导出测试(V7 · A137;见 CONTRACTS-V7.md §2 A137 行 / 红线 29、31)。

覆盖::

- ``to_prometheus``:整份输出**逐行字节精确**断言(HELP/TYPE/值)、
  counter ``_total`` / gauge / timer 四指标各节、节序与节内排序、
  插入序无关的确定性、空快照仅注释头、缺节容忍、末尾换行;
- 名称清洗各形态:点/横线/空白/非 ASCII→``_``、首字符数字补 ``ns_``、
  冒号保留、空名回退 ``ns_unnamed``、非 str 入参强转;
- 值格式:整值浮点压整数、小数 ``repr``、负数/大数;
- HELP 转义:原始名含反斜杠/换行不破坏行结构,恶性名集合导出→解析往返成功;
- ``parse_prometheus``:注释/空行忽略、坏行抛 ``ValueError``;
- ``diff_snaps`` 四键全分支:counters_added / counters_delta(零增量剔除、
  负增量)/ gauges_changed(变化/新增/消失,None 哨兵)/ timers_changed
  (四字段视图、新增、未变剔除)、相同/空快照、入参不被修改;
- ``render_metrics``:monkeypatch 固定快照的直通等价、真实 telemetry 的
  集成(reset 后可控)、docstring 挂载说明存在;
- ``kernel_selfcheck`` 返回结构;
- **红线 31 基准**::func:`test_v7_bench_export_parse_roundtrip`
  以"指标行数 == 计数器 + 仪表 + 4×计时器"与 (名称, 值) 逐对相等、
  HELP/TYPE 族数 == 指标数等**操作计数断言**证明往返零失真,不依赖墙钟。

全部测试离线,不发起网络请求。
"""
from __future__ import annotations

import copy

import pytest

from netsentinel import telemetry, telemetry_export
from netsentinel.telemetry_export import (
    clean_name,
    diff_snaps,
    kernel_selfcheck,
    parse_prometheus,
    render_metrics,
    to_prometheus,
)

# ---------------------------------------------------------------------------
# 共享构造
# ---------------------------------------------------------------------------

#: 规范小快照:三类指标各一条,值覆盖整数化(5/7/20)与小数(16.667)
SNAP = {
    "ts": "2026-10-02T08:00:00+08:00",
    "counters": {"fetch.page": 5},
    "gauges": {"queue.pending": 7},
    "timers": {
        "scan.duration": {
            "count": 3,
            "avg_ms": 16.667,
            "p95_ms": 20.0,
            "max_ms": 20.0,
        }
    },
}

#: 规范快照的完整期望输出(逐行精确;与模块 docstring 示例一致)
EXPECTED_FULL = (
    "# NetSentinel telemetry export(V7 A137 观测内核)\n"
    '# 快照时间戳 ts=2026-10-02T08:00:00+08:00\n'
    '# HELP fetch_page_total 计数器 "fetch.page"\n'
    "# TYPE fetch_page_total counter\n"
    "fetch_page_total 5\n"
    '# HELP queue_pending 仪表 "queue.pending"\n'
    "# TYPE queue_pending gauge\n"
    "queue_pending 7\n"
    '# HELP scan_duration_count 计时样本数(环形缓冲内) "scan.duration"\n'
    "# TYPE scan_duration_count gauge\n"
    "scan_duration_count 3\n"
    '# HELP scan_duration_avg_ms 计时均值(毫秒) "scan.duration"\n'
    "# TYPE scan_duration_avg_ms gauge\n"
    "scan_duration_avg_ms 16.667\n"
    '# HELP scan_duration_p95_ms 计时 p95(毫秒) "scan.duration"\n'
    "# TYPE scan_duration_p95_ms gauge\n"
    "scan_duration_p95_ms 20\n"
    '# HELP scan_duration_max_ms 计时最大值(毫秒) "scan.duration"\n'
    "# TYPE scan_duration_max_ms gauge\n"
    "scan_duration_max_ms 20\n"
)


def _metric_lines(text: str) -> list[str]:
    """非注释、非空的样本行(即真实指标行)。"""
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


@pytest.fixture()
def tel():
    """隔离 telemetry 全局态:进入/退出均清零(reset 仅供测试使用)。"""
    telemetry.reset()
    yield telemetry
    telemetry.reset()


# ---------------------------------------------------------------------------
# to_prometheus:格式逐行断言
# ---------------------------------------------------------------------------


def test_to_prometheus_full_exact_output() -> None:
    """规范快照 → 整份输出逐行字节精确(头两行注释 + 三节指标族)。"""
    assert to_prometheus(SNAP) == EXPECTED_FULL


def test_to_prometheus_counter_family_lines() -> None:
    """counter → `<name>_total <v>`,HELP/TYPE 各恰一行,TYPE 为 counter。"""
    lines = to_prometheus({"counters": {"fetch.page": 5}}).splitlines()
    assert lines[1:] == [
        '# HELP fetch_page_total 计数器 "fetch.page"',
        "# TYPE fetch_page_total counter",
        "fetch_page_total 5",
    ]


def test_to_prometheus_gauge_family_lines() -> None:
    """gauge → `<name> <v>`(无后缀),TYPE 为 gauge。"""
    lines = to_prometheus({"gauges": {"queue.pending": 7}}).splitlines()
    assert lines[1:] == [
        '# HELP queue_pending 仪表 "queue.pending"',
        "# TYPE queue_pending gauge",
        "queue_pending 7",
    ]


def test_to_prometheus_timer_four_metrics() -> None:
    """timer → count/avg_ms/p95_ms/max_ms 四条序列,值取自快照统计。"""
    lines = to_prometheus({"timers": {"scan.duration": SNAP["timers"]["scan.duration"]}}).splitlines()
    assert lines[1:] == [
        '# HELP scan_duration_count 计时样本数(环形缓冲内) "scan.duration"',
        "# TYPE scan_duration_count gauge",
        "scan_duration_count 3",
        '# HELP scan_duration_avg_ms 计时均值(毫秒) "scan.duration"',
        "# TYPE scan_duration_avg_ms gauge",
        "scan_duration_avg_ms 16.667",
        '# HELP scan_duration_p95_ms 计时 p95(毫秒) "scan.duration"',
        "# TYPE scan_duration_p95_ms gauge",
        "scan_duration_p95_ms 20",
        '# HELP scan_duration_max_ms 计时最大值(毫秒) "scan.duration"',
        "# TYPE scan_duration_max_ms gauge",
        "scan_duration_max_ms 20",
    ]


def test_to_prometheus_section_order_and_sorted_names() -> None:
    """节序 counters → gauges → timers;节内按原始名排序输出。"""
    snap = {
        "counters": {"b.two": 2, "a.one": 1},
        "gauges": {"z.gauge": 9, "a.gauge": 8},
        "timers": {"t.b": {"count": 1, "avg_ms": 1.0, "p95_ms": 1.0, "max_ms": 1.0}},
    }
    names = [ln.split()[0] for ln in _metric_lines(to_prometheus(snap))]
    assert names == [
        "a_one_total",
        "b_two_total",
        "a_gauge",
        "z_gauge",
        "t_b_count",
        "t_b_avg_ms",
        "t_b_p95_ms",
        "t_b_max_ms",
    ]


def test_to_prometheus_insertion_order_independent() -> None:
    """同一指标的不同插入序 → 输出逐字节一致(确定性,便于 diff)。"""
    snap_a = {"counters": {"x.y": 1, "p.q": 2}, "gauges": {"m.n": 3, "k.l": 4}}
    snap_b = {"counters": {"p.q": 2, "x.y": 1}, "gauges": {"k.l": 4, "m.n": 3}}
    assert to_prometheus(snap_a) == to_prometheus(snap_b)


def test_to_prometheus_empty_snapshot_only_header() -> None:
    """空快照({} 与三节皆空)→ 仅注释头,无任何指标行,仍以换行结尾。"""
    assert to_prometheus({}) == "# NetSentinel telemetry export(V7 A137 观测内核)\n"
    text = to_prometheus({"ts": "t0", "counters": {}, "gauges": {}, "timers": {}})
    assert text == (
        "# NetSentinel telemetry export(V7 A137 观测内核)\n# 快照时间戳 ts=t0\n"
    )
    assert _metric_lines(text) == []
    assert all(ln.startswith("#") for ln in text.splitlines())


def test_to_prometheus_missing_sections_tolerated() -> None:
    """缺 ts/缺任意节视同空:只渲染存在的指标,不抛异常。"""
    text = to_prometheus({"counters": {"only.counter": 1}})
    assert text.splitlines()[1:] == [
        '# HELP only_counter_total 计数器 "only.counter"',
        "# TYPE only_counter_total counter",
        "only_counter_total 1",
    ]


def test_to_prometheus_ends_with_newline() -> None:
    """输出以换行符结束(exposition 格式要求);无空行夹杂。"""
    text = to_prometheus(SNAP)
    assert text.endswith("\n")
    assert "" not in text.splitlines()


# ---------------------------------------------------------------------------
# 名称清洗与值格式
# ---------------------------------------------------------------------------


def test_clean_name_rule_forms() -> None:
    """清洗规则各形态:点/横线/空白/制表→_、非 ASCII→_、数字开头补 ns_、
    冒号与下划线保留、空名回退、非 str 强转。"""
    assert clean_name("fetch.page") == "fetch_page"
    assert clean_name("queue-depth") == "queue_depth"
    assert clean_name("a b\tc") == "a_b_c"
    assert clean_name("空间") == "__"          # 2 个非 ASCII 字节位 → 2 个 _
    assert clean_name("9lives") == "ns_9lives"  # 首字符数字 → 补 ns_
    assert clean_name("0x:ab") == "ns_0x:ab"   # 冒号合法保留,仅数字开头补前缀
    assert clean_name("a:b") == "a:b"           # 冒号合法,保留
    assert clean_name(":private") == ":private"
    assert clean_name("_under") == "_under"
    assert clean_name("UPPER.lower") == "UPPER_lower"
    assert clean_name("...") == "___"
    assert clean_name("") == "ns_unnamed"
    assert clean_name(42) == "ns_42"            # 非 str 先强转再清洗


def test_clean_name_applies_before_total_suffix() -> None:
    """counter 先清洗再补 ``_total``:数字开头名 → ``ns_..._total``。"""
    text = to_prometheus({"counters": {"9up": 4, "ok.y": 5}})
    names = [ln.split()[0] for ln in _metric_lines(text)]
    assert names == ["ns_9up_total", "ok_y_total"]


def test_value_formatting_integral_and_fractional() -> None:
    """整值浮点压成整数文本;小数取 repr;负数/大数合法。"""
    snap = {
        "gauges": {"g.int": 5.0, "g.frac": 0.025, "g.neg": -3.5, "g.big": 1e6, "g.zero": 0}
    }
    lines = _metric_lines(to_prometheus(snap))
    # 节内按名排序:g.big < g.frac < g.int < g.neg < g.zero
    assert lines == ["g_big 1000000", "g_frac 0.025", "g_int 5", "g_neg -3.5", "g_zero 0"]
    assert parse_prometheus("\n".join(lines)) == {
        "g_int": 5.0,
        "g_frac": 0.025,
        "g_neg": -3.5,
        "g_big": 1000000.0,
        "g_zero": 0.0,
    }


def test_help_escapes_and_nasty_names_roundtrip() -> None:
    """原始名含反斜杠/换行:HELP 按规则转义不破坏行结构;恶性名集合
    导出 → 解析往返全部成功(清洗后名字必为合法指标名)。"""
    nasty = {
        "9x": 1,            # → ns_9x_total
        "a.b-c d": 2,       # → a_b_c_d_total
        "x\\y": 6,          # 反斜杠 → x_y_total,HELP 内转义为 x\\y
        "a\nb": 7,          # 换行 → a_b_total,HELP 内转义为字面 \n
        ":lead": 8,         # 冒号开头合法
    }
    text = to_prometheus({"counters": nasty})
    # 每行要么注释要么恰两词:换行未被带进 HELP,行结构完好
    assert len(text.splitlines()) == 1 + 3 * len(nasty)
    parsed = parse_prometheus(text)
    assert parsed == {
        "ns_9x_total": 1.0,
        "a_b_c_d_total": 2.0,
        "x_y_total": 6.0,
        "a_b_total": 7.0,
        ":lead_total": 8.0,
    }
    assert '# HELP a_b_total 计数器 "a\\nb"' in text  # 真换行 → 字面 \n
    assert '# HELP x_y_total 计数器 "x\\\\y"' in text  # 单反斜杠 → 双反斜杠


# ---------------------------------------------------------------------------
# parse_prometheus
# ---------------------------------------------------------------------------


def test_parse_prometheus_ignores_comments_blank_and_last_wins() -> None:
    """注释/空行忽略;重名样本最后一次出现生效。"""
    text = (
        "# 头注释\n"
        "\n"
        "# HELP a_total x\n"
        "# TYPE a_total counter\n"
        "a_total 3\n"
        "b_gauge 0.5\n"
        "a_total 4\n"
    )
    assert parse_prometheus(text) == {"a_total": 4.0, "b_gauge": 0.5}


def test_parse_prometheus_rejects_malformed() -> None:
    """坏样本行(词数≠2 / 非法名 / 非数)抛 ValueError,中文消息带行号。"""
    with pytest.raises(ValueError, match="第 2 行"):
        parse_prometheus("a_total 1\nbroken_line\n")
    with pytest.raises(ValueError, match="第 1 行"):
        parse_prometheus("9bad_name 1\n")          # 数字开头指标名非法
    with pytest.raises(ValueError, match="第 1 行"):
        parse_prometheus("a_total 1 extra_token\n")  # 三个词


# ---------------------------------------------------------------------------
# diff_snaps:四键全分支
# ---------------------------------------------------------------------------


def test_diff_snaps_counters_added_and_delta() -> None:
    """counters_added = b 新增;counters_delta = 双侧增量(零增量剔除)。"""
    a = {"counters": {"c.stay": 10, "c.move": 5, "c.flat": 3}}
    b = {"counters": {"c.stay": 10, "c.move": 8, "c.flat": 3, "c.new": 7}}
    result = diff_snaps(a, b)
    assert result["counters_added"] == {"c.new": 7.0}
    assert result["counters_delta"] == {"c.move": 3.0}  # 零增量被剔除
    assert result["gauges_changed"] == {} and result["timers_changed"] == {}


def test_diff_snaps_counters_negative_delta() -> None:
    """负增量(telemetry.inc 负数)同样入 delta,值为负。"""
    result = diff_snaps({"counters": {"c": 10}}, {"counters": {"c": 4}})
    assert result["counters_delta"] == {"c": -6.0}


def test_diff_snaps_gauges_changed_with_none_sentinels() -> None:
    """仪表:变化给 from/to;新增 from=None;消失 to=None;未变剔除。"""
    a = {"gauges": {"g.same": 1, "g.up": 2, "g.gone": 5}}
    b = {"gauges": {"g.same": 1, "g.up": 9, "g.fresh": 4}}
    assert diff_snaps(a, b)["gauges_changed"] == {
        "g.gone": {"from": 5.0, "to": None},
        "g.fresh": {"from": None, "to": 4.0},
        "g.up": {"from": 2.0, "to": 9.0},
    }


def test_diff_snaps_timers_changed_views() -> None:
    """计时器:四字段任一变化才入;值为四字段视图;新增 from=None;
    多余键不参与比较;四字段全同则剔除。"""
    same = {"count": 2, "avg_ms": 10.0, "p95_ms": 12.0, "max_ms": 12.0}
    a = {
        "timers": {
            "t.same": same,
            "t.grow": {"count": 1, "avg_ms": 5.0, "p95_ms": 5.0, "max_ms": 5.0},
            "t.extra": {**same, "note": "old"},  # 多余键,四字段相同 → 未变
        }
    }
    b = {
        "timers": {
            "t.same": same,
            "t.grow": {"count": 2, "avg_ms": 7.5, "p95_ms": 9.0, "max_ms": 9.0},
            "t.extra": {**same, "note": "new"},
            "t.new": {"count": 1, "avg_ms": 1.0, "p95_ms": 1.0, "max_ms": 1.0},
        }
    }
    assert diff_snaps(a, b)["timers_changed"] == {
        "t.grow": {
            "from": {"count": 1, "avg_ms": 5.0, "p95_ms": 5.0, "max_ms": 5.0},
            "to": {"count": 2, "avg_ms": 7.5, "p95_ms": 9.0, "max_ms": 9.0},
        },
        "t.new": {
            "from": None,
            "to": {"count": 1, "avg_ms": 1.0, "p95_ms": 1.0, "max_ms": 1.0},
        },
    }


def test_diff_snaps_identical_and_empty() -> None:
    """相同快照 / 双空快照 → 四键皆为空 dict;键集合恰为契约四键。"""
    result = diff_snaps(SNAP, copy.deepcopy(SNAP))
    assert result == {
        "counters_added": {},
        "counters_delta": {},
        "gauges_changed": {},
        "timers_changed": {},
    }
    assert diff_snaps({}, {}) == result
    assert set(diff_snaps({}, {"counters": {"x": 1}})) == {
        "counters_added",
        "counters_delta",
        "gauges_changed",
        "timers_changed",
    }


def test_diff_snaps_does_not_mutate_input() -> None:
    """入参只读:diff 前后 a/b 深度相等(纯函数契约)。"""
    a = copy.deepcopy(SNAP)
    b = copy.deepcopy(SNAP)
    b["counters"]["fetch.page"] = 9
    a_before, b_before = copy.deepcopy(a), copy.deepcopy(b)
    diff_snaps(a, b)
    to_prometheus(a)
    assert a == a_before and b == b_before


# ---------------------------------------------------------------------------
# render_metrics:直通与集成
# ---------------------------------------------------------------------------


def test_render_metrics_is_snapshot_passthrough(monkeypatch) -> None:
    """固定快照下 render_metrics() == to_prometheus(snapshot())(直通,无加工)。"""
    fixed = {
        "ts": "2026-10-02T09:00:00+08:00",
        "counters": {"x.y": 2},
        "gauges": {"z.w": 0.5},
        "timers": {},
    }
    monkeypatch.setattr(telemetry, "snapshot", lambda: fixed)
    assert render_metrics() == to_prometheus(fixed)
    assert "x_y_total 2" in render_metrics().splitlines()


def test_render_metrics_integration_real_telemetry(tel) -> None:
    """真实 telemetry 集成:reset 后写入可控指标,渲染→解析逐值核对。"""
    tel.inc("itest.counter", 3)
    tel.gauge("itest.gauge", 7)
    tel.observe("itest.timer", 0.025)  # 25ms,单样本
    text = render_metrics()
    assert text.startswith("#")  # 注释头在最前
    parsed = parse_prometheus(text)
    assert parsed["itest_counter_total"] == 3.0
    assert parsed["itest_gauge"] == 7.0
    assert parsed["itest_timer_count"] == 1.0
    assert parsed["itest_timer_avg_ms"] == 25.0
    assert parsed["itest_timer_p95_ms"] == 25.0
    assert parsed["itest_timer_max_ms"] == 25.0


def test_render_metrics_docstring_documents_mounting() -> None:
    """docstring 提供 service 挂 /metrics 的说明(契约 A137:不改 service)。"""
    doc = render_metrics.__doc__
    assert "/metrics" in doc and "create_app" in doc
    assert "text/plain; version=0.0.4" in doc


# ---------------------------------------------------------------------------
# kernel_selfcheck(A138 基准总控入口)
# ---------------------------------------------------------------------------


def test_kernel_selfcheck_shape() -> None:
    """自检返回 {"name","metric","value","baseline"} 且往返保真率 = 1.0。"""
    info = kernel_selfcheck()
    assert set(info) == {"name", "metric", "value", "baseline"}
    assert info["name"] == "telemetry_export"
    assert info["metric"] == "roundtrip_pair_recovery_ratio"
    assert info["value"] == 1.0 and info["baseline"] == 1.0


# ---------------------------------------------------------------------------
# 红线 31 基准:导出 → 解析往返(操作计数,不依赖墙钟)
# ---------------------------------------------------------------------------


def test_v7_bench_export_parse_roundtrip() -> None:
    """红线 31:33 计数器 + 21 仪表 + 17 计时器 = 122 指标的确定性快照上,

    - 样本行数 == 33 + 21 + 4×17 == 122(操作计数);
    - HELP/TYPE 族数 == 122(每族恰一对注释);
    - 解析恢复的 (名称, 值) 与期望**逐对相等**(值取 0.5 步长,二进制精确,
      往返零失真,无浮点噪声);
    全程无墙钟读数,离线可复现。
    """
    n_c, n_g, n_t = 33, 21, 17
    snap = {
        "ts": "2026-10-02T00:00:00+08:00",
        "counters": {f"bench.counter.{i:02d}": i * 0.5 for i in range(n_c)},
        "gauges": {f"bench.gauge.{i:02d}": (i % 5) * 0.5 for i in range(n_g)},
        "timers": {
            f"bench.timer.{i:02d}": {
                "count": i % 4,
                "avg_ms": (i % 7) * 0.5,
                "p95_ms": (i % 9) * 0.5,
                "max_ms": (i % 11) * 0.5,
            }
            for i in range(n_t)
        },
    }
    expected = {f"bench_counter_{i:02d}_total": i * 0.5 for i in range(n_c)}
    expected.update({f"bench_gauge_{i:02d}": (i % 5) * 0.5 for i in range(n_g)})
    for i in range(n_t):
        for suffix, field in (
            ("count", "count"),
            ("avg_ms", "avg_ms"),
            ("p95_ms", "p95_ms"),
            ("max_ms", "max_ms"),
        ):
            expected[f"bench_timer_{i:02d}_{suffix}"] = float(
                snap["timers"][f"bench.timer.{i:02d}"][field]
            )

    text = to_prometheus(snap)
    sample_lines = _metric_lines(text)
    assert len(sample_lines) == n_c + n_g + 4 * n_t == 122  # 行数 == 指标数
    help_count = sum(1 for ln in text.splitlines() if ln.startswith("# HELP "))
    type_count = sum(1 for ln in text.splitlines() if ln.startswith("# TYPE "))
    assert help_count == type_count == 122                  # 每指标恰一对注释
    assert parse_prometheus(text) == expected               # 值逐对相等
    assert len(parse_prometheus(text)) == 122               # 无重名冲撞


def test_module_all_exports_match_contract() -> None:
    """契约 API(to_prometheus/diff_snaps/render_metrics)在 __all__ 中;
    模块为纯新增,未向 telemetry 注入任何属性(红线 29)。"""
    for name in ("to_prometheus", "diff_snaps", "render_metrics"):
        assert name in telemetry_export.__all__
    for injected in ("to_prometheus", "diff_snaps", "render_metrics", "parse_prometheus"):
        assert not hasattr(telemetry, injected)  # 冻结模块零污染
