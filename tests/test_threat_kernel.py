"""A136 netsentinel.security.threat_kernel 安全内核·模糊测试的测试。

离线、确定性;除全量矩阵冒烟落盘临时文件(用后即删)外无 IO / 无网络。
覆盖(CONTRACTS-V7 §2 A136 + 红线 31):

- 四生成器规模(各 ≥30)与**两次生成逐字节相同**(模块级种子随机);
- all_cases 汇总:类别齐全、(kind, text) 形状、全量唯一、顺序固定;
- 语料敌意类别齐备:超长 10KB / 控制字符 / 换页符 \\x0c / 空字节 /
  坏 UTF-8 替换符 \\ufffd / 500 层深嵌套 / 边界数字 / 巨数字 / 负数 /
  空串 / 仅空白;全语料可 UTF-8 落盘(无孤立代理项);
- smoke harness:良性全 ok;允许集内异常(ValueError/RuntimeError/
  TypeError/RecursionError)记 ok;KeyError 违规捕获;ran==ok+违规数
  恒等式;裸字符串用例;**不吞 KeyboardInterrupt/SystemExit**(即便
  调用方把 BaseException 传进 allowed);
- TARGETS 注册表:五目标 / 唯一路径类(policy)/ conformal JSON 适配 /
  各内核允许集;懒导入(子进程验证:import 本模块不加载任何兄弟内核);
  绑定失败 → 中文 RuntimeError;
- 路径类入口:落盘临时文件用后即删(前后缀计数相等);
- conformal 适配语义:健康 31 对 / n<30 降级 / 坏 JSON / 非数组 /
  元数错误 → ValueError,字符串分值 → 内核 ValueError;
- 全量矩阵冒烟:**零新发现违规**;已登记违规恰与 SKIP_LIST 一一匹配
  (实测 1 例:conformal 巨整分值 OverflowError,如实登记待修复;
  若内核已修复则要求移除失配登记,不许静默);
- test_v7_bench_matrix_operation_count:用例×目标计数 == 执行次数
  (计数器注入,零墙钟,红线 31);
- kernel_selfcheck(A138 字段)与 CLI main(过滤 / 报告 / 退出码 0/2 /
  未知过滤词中文报错)。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile

import pytest

from netsentinel.security.threat_kernel import (
    DEFAULT_ALLOWED,
    SKIP_LIST,
    TARGETS,
    KnownViolation,
    TargetSpec,
    all_cases,
    bind_target,
    fit_threshold_from_json,
    fuzz_html,
    fuzz_json,
    fuzz_url,
    fuzz_yaml,
    kernel_selfcheck,
    main,
    run_suite,
    smoke,
)
from netsentinel.security.threat_kernel import (
    _DEPTH,
    _LONG,
    _TMP_PREFIX,
    _rng,
)

# ---------------------------------------------------------------------------
# 生成器:规模 / 确定性 / 唯一性
# ---------------------------------------------------------------------------

_GENERATORS = {
    "url": fuzz_url,
    "html": fuzz_html,
    "yaml": fuzz_yaml,
    "json": fuzz_json,
}


@pytest.mark.parametrize("kind", sorted(_GENERATORS))
def test_generator_scale_and_byte_identical_determinism(kind: str) -> None:
    """各生成器 ≥30 变体;同进程两次调用**逐字节相同**(模块级种子随机)。"""
    gen = _GENERATORS[kind]
    first, second = gen(), gen()
    assert len(first) >= 30, f"{kind} 生成器变体数 {len(first)} < 30"
    assert all(isinstance(t, str) for t in first)
    assert first == second, f"{kind} 两次生成不一致:确定性被破坏"


@pytest.mark.parametrize("kind", sorted(_GENERATORS))
def test_generator_no_duplicate_texts(kind: str) -> None:
    """各类语料内部无重复文本(报告可按预览唯一定位)。"""
    corpus = _GENERATORS[kind]()
    assert len(set(corpus)) == len(corpus)


def test_all_cases_kinds_shape_and_uniqueness() -> None:
    """all_cases:类别齐全、(kind, text) 形状、全量唯一、顺序 url→html→yaml→json。"""
    cases = all_cases()
    assert len(cases) >= 120  # 四类各 ≥30
    kinds = [c[0] for c in cases]
    assert set(kinds) == {"url", "html", "yaml", "json"}
    # 顺序固定:四类各自连续
    order = [k for i, k in enumerate(kinds) if i == 0 or kinds[i - 1] != k]
    assert order == ["url", "html", "yaml", "json"]
    for case in cases:
        assert isinstance(case, tuple) and len(case) == 2
        assert isinstance(case[1], str)
    assert len(set(cases)) == len(cases)  # (kind, text) 全量唯一


def test_rng_factory_idempotent_and_kind_isolated() -> None:
    """种子随机源:同 kind 两次派生序列一致;不同 kind 序列不同(独立子种子)。"""
    assert [_rng("url").random() for _ in range(8)] == [
        _rng("url").random() for _ in range(8)
    ]
    assert _rng("url").random() != _rng("json").random()


# ---------------------------------------------------------------------------
# 语料:敌意类别齐备 + 可落盘约束
# ---------------------------------------------------------------------------


def test_corpus_hostile_categories_present() -> None:
    """契约 §2 A136 要求的敌意类别逐项在册(各类语料)。"""
    for kind, gen in _GENERATORS.items():
        corpus = gen()
        joined = "\n".join(corpus)
        assert "" in corpus, f"{kind}:缺空串"
        assert any(t.strip() == "" and t != "" for t in corpus), f"{kind}:缺仅空白"
        assert any(len(t) >= _LONG for t in corpus), f"{kind}:缺 ≥10KB 超长用例"
        assert "\x0c" in joined, f"{kind}:缺换页符 \\x0c"
        assert "\x01" in joined, f"{kind}:缺控制字符"
        assert "\x00" in joined, f"{kind}:缺空字节"
        assert "\ufffd" in joined, f"{kind}:缺坏 UTF-8 替换符"
    # 深嵌套 500 层:html / yaml / json 三类逐串在册(与模块常量同源)
    assert "<div>" * _DEPTH + "深" + "</div>" * _DEPTH in fuzz_html()
    assert "[" * _DEPTH + "]" * _DEPTH + "\n" in fuzz_yaml()
    assert "".join("  " * i + f"k{i}:\n" for i in range(_DEPTH)) in fuzz_yaml()
    assert "[" * _DEPTH + "]" * _DEPTH in fuzz_json()
    assert '{"a": ' * _DEPTH + "1" + "}" * _DEPTH in fuzz_json()
    # URL 特有:500 层子域标签 / 巨数字与巨负数查询
    url_corpus = fuzz_url()
    assert "http://" + "a." * _DEPTH + "com" in url_corpus
    assert any("9" * 100 in t for t in url_corpus)
    assert any("-99999" in t for t in url_corpus)
    # JSON 特有:边界数字 / 巨数字 / 负数
    json_corpus = fuzz_json()
    for boundary in ('{"nsfw_prob": 0}', '{"nsfw_prob": -0}',
                     '{"nsfw_prob": 1e308}', '{"nsfw_prob": 1e309}',
                     '{"nsfw_prob": -1e309}'):
        assert boundary in json_corpus
    assert "1" + "0" * 400 in "\n".join(json_corpus)  # 400 位巨整数
    assert "-1" + "0" * 400 in "\n".join(json_corpus)  # 400 位巨负整数


def test_corpus_utf8_encodable_no_lone_surrogates() -> None:
    """全语料可 UTF-8 编码(无孤立代理项)——路径类目标落盘与报告打印的前提。"""
    for _kind, text in all_cases():
        text.encode("utf-8")  # 抛异常即失败


# ---------------------------------------------------------------------------
# smoke harness
# ---------------------------------------------------------------------------


def test_smoke_benign_fn_all_ok() -> None:
    """良性 fn:全 ok,ran==用例数,violations 空,每个文本恰喂一次。"""
    seen: list[str] = []

    def fn(text: str) -> None:
        seen.append(text)

    result = smoke(fn, all_cases())
    assert result["ran"] == len(all_cases())
    assert result["ok"] == result["ran"]
    assert result["violations"] == []
    assert seen == [c[-1] for c in all_cases()]


@pytest.mark.parametrize(
    "exc", [ValueError("中文错误"), RuntimeError("栈深"), TypeError("类型"), RecursionError()]
)
def test_smoke_allowed_exceptions_counted_ok(exc: Exception) -> None:
    """允许集内异常(含 RecursionError ⊂ RuntimeError)一律记 ok。"""
    def fn(_text: str) -> None:
        raise exc

    result = smoke(fn, [("url", "x"), ("html", "y")])
    assert result == {"ran": 2, "ok": 2, "violations": []}


def test_smoke_violation_capture_keyerror() -> None:
    """允许集之外的 KeyError:逐 case 捕获为 (case, exc),恒等式 ran==ok+违规数。"""

    def fn(_text: str) -> None:
        raise KeyError("炸了")

    cases = all_cases()
    result = smoke(fn, cases)
    assert result["ran"] == len(cases)
    assert result["ok"] == 0
    assert len(result["violations"]) == len(cases)
    assert all(isinstance(exc, KeyError) for _case, exc in result["violations"])
    # 违规条目保留原 case(报告据此定位)
    assert [c for c, _e in result["violations"]] == cases
    assert result["ran"] == result["ok"] + len(result["violations"])


def test_smoke_mixed_outcomes_arithmetic() -> None:
    """混合结局(部分正常 / ValueError / KeyError):恒等式与违规归类。"""
    def fn(text: str) -> None:
        if "\x00" in text:
            raise KeyError("失控")
        if "\x0c" in text:
            raise ValueError("体面")
        return None

    cases = all_cases()
    result = smoke(fn, cases)
    viol_kinds = {type(exc) for _c, exc in result["violations"]}
    assert viol_kinds == {KeyError}
    assert all("\x00" in c[-1] for c, _e in result["violations"])
    assert result["ran"] == result["ok"] + len(result["violations"])


def test_smoke_does_not_swallow_keyboard_interrupt_or_system_exit() -> None:
    """KeyboardInterrupt / SystemExit 原样上抛;即便 allowed 含 BaseException 也如此。"""

    def boom_ki(_text: str) -> None:
        raise KeyboardInterrupt

    def boom_se(_text: str) -> None:
        raise SystemExit(3)

    with pytest.raises(KeyboardInterrupt):
        smoke(boom_ki, [("url", "x")])
    with pytest.raises(SystemExit) as ei:
        smoke(boom_se, [("url", "x")])
    assert ei.value.code == 3
    # 极端 allowed 也绝不吞人的中断/退出
    with pytest.raises(KeyboardInterrupt):
        smoke(boom_ki, [("url", "x")], allowed=(BaseException,))


def test_smoke_accepts_plain_string_cases() -> None:
    """用例既可 (kind, text) 也可裸字符串(取整串为文本)。"""
    calls: list[str] = []

    def fn(text: str) -> None:
        calls.append(text)

    result = smoke(fn, ["裸串A", "裸串B"])
    assert result == {"ran": 2, "ok": 2, "violations": []}
    assert calls == ["裸串A", "裸串B"]


# ---------------------------------------------------------------------------
# TARGETS 注册表 / 懒绑定
# ---------------------------------------------------------------------------


def test_targets_registry_shape() -> None:
    """五目标、键序稳定;policy 是唯一路径类;conformal 走 JSON 适配。"""
    assert [s.key for s in TARGETS] == [
        "canonical", "text_intel", "response_repair", "policy", "conformal",
    ]
    path_specs = [s for s in TARGETS if s.input_kind == "path"]
    assert [s.key for s in path_specs] == ["policy"]
    by_key = {s.key: s for s in TARGETS}
    assert by_key["conformal"].wrap == "fit_threshold_from_json"
    assert by_key["policy"].extra_allowed == (ImportError,)
    assert all(s.module_path.startswith("netsentinel.") for s in TARGETS)
    assert all(s.title for s in TARGETS)  # 中文名齐备(报告展示)


def test_bind_target_allowed_sets() -> None:
    """允许集:文本类=默认集;policy=默认集+ImportError(PyYAML 惰性依赖契约)。"""
    for spec in TARGETS:
        bound = bind_target(spec)
        assert callable(bound.fn)
        expected = DEFAULT_ALLOWED + tuple(spec.extra_allowed)
        assert bound.allowed == expected


def test_bind_target_lazy_import_contract() -> None:
    """契约 §2 惰性导入:干净解释器 import 本模块不加载任何兄弟内核模块。"""
    code = (
        "import sys, netsentinel.security.threat_kernel as tk; "
        "leaked = [m for m in sys.modules if m.startswith('netsentinel.') "
        "and m not in ('netsentinel', 'netsentinel.security', "
        "'netsentinel.security.threat_kernel')]; "
        "print('LEAK:' + ','.join(leaked) if leaked else 'CLEAN')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=os.getcwd()
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("CLEAN"), f"惰性导入被破坏:{proc.stdout}"


def test_bind_target_missing_entry_runtime_error() -> None:
    """注册表指向不存在的模块/属性:中文 RuntimeError(注册表脱钩必须立刻暴露)。"""
    for spec in (
        TargetSpec("ghost", "幽灵", "netsentinel.intel.canonical", "no_such_fn"),
        TargetSpec("ghost2", "幽灵", "netsentinel.intel.no_such_module", "f"),
    ):
        with pytest.raises(RuntimeError, match="懒绑定内核入口失败"):
            bind_target(spec)


def test_path_entrypoint_cleans_temp_files() -> None:
    """路径类入口:临时文件用后即删(前缀计数前后相等),返回值正常透传。"""
    bound = bind_target(next(s for s in TARGETS if s.key == "policy"))
    pattern = os.path.join(tempfile.gettempdir(), _TMP_PREFIX + "*")
    before = len(glob.glob(pattern))
    rules = bound.fn("- name: r1\n  action: queue\n")
    assert isinstance(rules, list) and rules[0].name == "r1"
    assert isinstance(bound.fn(""), list)  # 空文件 → 默认政策(不异常)
    assert len(glob.glob(pattern)) == before, "临时文件未清理"


# ---------------------------------------------------------------------------
# conformal JSON 适配入口
# ---------------------------------------------------------------------------


def test_fit_threshold_from_json_semantics() -> None:
    """适配层:健康校准集走通;形状问题一律 ValueError;内核异常原样透传。"""
    from netsentinel.decision.conformal import fit_threshold

    entry = fit_threshold_from_json(fit_threshold)
    # 健康 31 对(首对 0.9/true,其余 0.1/false):n=31、k=1 精度 1.0
    healthy = "[" + ",".join(["[0.9, true]"] + ["[0.1, false]"] * 30) + "]"
    fit = entry(healthy)
    assert fit["valid"] is True and fit["threshold"] == 0.9
    # n<30 → 内核降级(不异常、不给担保)
    small = entry("[[0.9, true], [0.1, false]]")
    assert small["valid"] is False and small["threshold"] is None
    # 适配层形状校验:坏 JSON / 非数组 / 元数错误 → ValueError(允许集内)
    for bad in ["{", "null", '"数组"', '"x"', "[[0.9]]", "[[0.9, true, false]]"]:
        with pytest.raises(ValueError):
            entry(bad)
    # 内核本体异常类型原样透传(绝不遮蔽):非数值分值 → ValueError
    with pytest.raises(ValueError):
        entry('[[0.9, true], ["x", false]]')


# ---------------------------------------------------------------------------
# 全量矩阵:既有内核冒烟(诚实口径)
# ---------------------------------------------------------------------------


def test_full_matrix_smoke_no_unexpected_violations() -> None:
    """全量 目标×用例 冒烟:零**新发现**违规;违规恰与 SKIP_LIST 一一匹配。

    实测登记 1 例既有内核违规(如实报告,待项目负责人修复;本内核按
    红线 29 不改既有模块):
      - conformal × json 巨整分值 → OverflowError(int too large to convert
        to float;OverflowError ⊂ ArithmeticError,不在允许集内)。
    若该内核修复后违规消失,SKIP_LIST 对应项会失配(stale)——本断言
    随之失败并提示移除登记,保证 skip 清单永不静默遮蔽真实回归。
    """
    result = run_suite()
    assert result["total_ran"] == result["cases_n"] * result["targets_n"] == 810
    for t in result["targets"]:
        assert t["ran"] == result["cases_n"], f"[{t['key']}] 漏跑用例"
        assert t["ran"] == t["ok"] + len(t["violations"])
    assert result["unexpected"] == [], (
        "发现未登记的新违规:" + "; ".join(
            f"[{k}] {c[0]} {_preview_short(c[-1])} → {type(e).__name__}: {e}"
            for k, c, e in result["unexpected"]
        )
    )
    # 已登记违规与 SKIP_LIST 双向匹配:每条违规命中登记,每条登记被命中
    assert len(result["expected"]) == len(
        [v for v in result["violations"]]
    )
    matched_entries = {id(entry) for _k, _c, _e, entry in result["expected"]}
    assert matched_entries == {id(entry) for entry in SKIP_LIST}
    assert result["stale_skips"] == [], "skip 登记已失配(内核可能已修复,请移除登记)"


def _preview_short(text: str) -> str:
    return repr(text[:40])


# ---------------------------------------------------------------------------
# 基准(红线 31:操作计数断言,零墙钟)
# ---------------------------------------------------------------------------


def test_v7_bench_matrix_operation_count() -> None:
    """bench:用例×目标计数 == 实际执行次数(计数器注入,零墙钟,红线 31)。

    对五个真实目标分别注入计数包装:每目标恰执行 len(all_cases()) 次,
    总执行数恰为 用例数 × 目标数(每格一次、不多不少);kernel_selfcheck
    的 total_ran 同口径。
    """
    cases = all_cases()
    total_calls = 0
    for spec in TARGETS:
        bound = bind_target(spec)
        counter = {"n": 0}

        def counting(text: str, _inner=bound.fn, _c=counter) -> None:
            _c["n"] += 1
            _inner(text)

        result = smoke(counting, cases, allowed=bound.allowed)
        assert counter["n"] == len(cases), f"[{spec.key}] 执行次数 != 用例数"
        assert result["ran"] == counter["n"]
        total_calls += counter["n"]
    assert total_calls == len(cases) * len(TARGETS)
    assert kernel_selfcheck()["total_ran"] == len(cases) * len(TARGETS)


def test_kernel_selfcheck_fields_and_determinism() -> None:
    """kernel_selfcheck(A138):字段齐备、确定性、新发现违规基线为 0。"""
    first = kernel_selfcheck()
    second = kernel_selfcheck()
    assert first == second  # 确定性(同一矩阵两次运行结果一致)
    assert first["name"] == "threat_kernel"
    assert first["metric"] and first["baseline"] == 0
    assert first["value"] == 0  # 新发现违规 = 0(已登记违规单列,不静默)
    assert first["expected_registered"] == len(SKIP_LIST)


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------


def test_main_cli_clean_scope_exit_zero(capsys: pytest.CaptureFixture) -> None:
    """url 子域全绿:退出码 0,报告含中文标题与「违规清单:空」。"""
    rc = main(["url"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "安全内核模糊测试报告" in out
    assert "违规清单:空" in out
    assert "结论:零违规——退出码 0" in out
    n_url = len([c for c in all_cases() if c[0] == "url"])
    assert f"用例 {n_url} 条 × 目标 5 个 = 执行 {n_url * 5} 次" in out


def test_main_cli_full_matrix_exit_code_matches_violations(
    capsys: pytest.CaptureFixture,
) -> None:
    """全量 CLI:退出码与库口径一致(有违规即 2);登记项原样打印不静默。"""
    rc = main([])
    out = capsys.readouterr().out
    result = run_suite()
    expected_rc = 2 if result["violations"] else 0
    assert rc == expected_rc
    assert "执行 810 次" in out
    if result["violations"]:
        assert "skip 清单" in out
        assert "OverflowError" in out
        assert "登记依据" in out
        assert f"已登记 {len(result['expected'])} + 新发现 {len(result['unexpected'])}" in out
    else:
        assert "违规清单:空" in out


def test_main_cli_target_and_kind_filter(capsys: pytest.CaptureFixture) -> None:
    """目标×类别混合过滤:conformal × json 子矩阵规模精确反映在报告里。"""
    rc = main(["conformal", "json"])
    out = capsys.readouterr().out
    n_json = len([c for c in all_cases() if c[0] == "json"])
    assert rc == 0  # conformal 巨整溢出已由负责人修复,该子矩阵现零违规
    assert f"用例 {n_json} 条 × 目标 1 个 = 执行 {n_json} 次" in out
    assert "[conformal]" in out
    assert "[canonical]" not in out  # 其他目标被过滤


def test_main_cli_unknown_filter_rejected(capsys: pytest.CaptureFixture) -> None:
    """未知过滤词:中文用法报错 + 退出码 2(绝不静默跑空矩阵)。"""
    rc = main(["bogus"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "未知过滤词" in out and "bogus" in out
