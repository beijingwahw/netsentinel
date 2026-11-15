# -*- coding: utf-8 -*-
"""scripts/entry_check.py(全入口离线体检器,V10.20 Seat D)单元测试。

只测体检器**自身机制**(清单结构 / 过滤 / runner 分类 / 退出码语义 /
JSON 报告 / 沙箱环境注入),不在单测里真跑全量体检(太慢,实跑交给
人工或显式门控样本)。

显式门控的真跑样本:设置 ``NETSENTINEL_ENTRY_CHECK_E2E=1`` 后本文件会
额外跑 ``--only help`` 快速条目(4 条,秒级),验证体检器对真实项目入口
的端到端可用性。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

#: 项目根(tests/conftest.py 已保证根目录在 sys.path;此处用路径直接装载)
ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = ROOT / "scripts" / "entry_check.py"


def _load_module():
    """以独立模块名装载 scripts/entry_check.py(scripts 不是包)。

    装载前须注册进 ``sys.modules``:dataclass 处理字符串注解时会按
    ``cls.__module__`` 回查模块字典,未注册会 AttributeError。
    """
    spec = importlib.util.spec_from_file_location("netsentinel_entry_check", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["netsentinel_entry_check"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("netsentinel_entry_check", None)
    return module


ec = _load_module()


#: 清单里允许出现的 argv 占位符键(与 Sandbox.placeholders/expand 对齐)
_KNOWN_PLACEHOLDERS = {
    "config", "data", "db", "queue", "ledger", "audit",
    "keywords", "leads", "draft", "root", "port",
    "model_runtime", "watchlist",
}

#: USAGE §1 主表必须覆盖的核心入口(缺一即清单不完整)
_REQUIRED_CLI_NAMES = {
    "main_help",
    "scan_local_fixture",
    "queue_list",
    "submit_help",
    "batchflow_help",
    "finishflow_help",
    "sessionflow_status",
    "discovery_offline_check",
    "review_tui_list",
    "batch_tui_groups",
    "modelmgr_status",
    "vlmctl_list",
    "profile_show",
    "golive_check_offline",
    "storage_replay_empty",
    "audit_verify_minimal",
    "scheduler_once_empty",
    "form_scanner_local_fixture",
}

_WEBUI_PAGE_NAMES = {
    "app", "dashboard", "draft_flow_page", "groups_page",
    "keywords_page", "label_page", "models_page", "providers_page", "runs_page",
}


# ---------------------------------------------------------------------------
# 假条目工厂(零外呼:只跑 python -c 内联代码)
# ---------------------------------------------------------------------------
def _fake(name: str, code: str, expect=(0,), timeout=30.0, **kw):
    return ec.CheckEntry(
        name=name, argv=("-c", code), expect=frozenset(expect), timeout=timeout, **kw
    )


def _pass(name: str = "pass_entry"):
    return _fake(name, "print('假条目通过'); raise SystemExit(0)")


_FAIL = _fake(
    "always_fail", "import sys; print('假条目失败', file=sys.stderr); raise SystemExit(3)"
)
_PASS = _pass("always_pass")


# ---------------------------------------------------------------------------
# 清单结构
# ---------------------------------------------------------------------------
class TestChecklist:
    """curated 清单完整性与无重复名。"""

    def test_no_duplicate_names(self):
        checks = ec.build_checks()
        names = [c.name for c in checks]
        assert len(names) == len(set(names)), "清单条目名必须唯一"

    def test_structure_and_required_entries(self):
        checks = ec.build_checks()
        names = {c.name for c in checks}
        assert _REQUIRED_CLI_NAMES <= names, "USAGE §1 核心入口必须全覆盖"
        assert {f"webui_import_{p}" for p in _WEBUI_PAGE_NAMES} <= names
        assert "service_app_factory" in names
        assert len(checks) >= 27  # 18 CLI + 8 WebUI + 1 service
        for c in checks:
            assert c.name and c.name == c.name.strip()
            assert isinstance(c.argv, tuple) and c.argv
            assert all(isinstance(p, str) and p for p in c.argv)
            assert isinstance(c.expect, frozenset) and c.expect
            assert all(isinstance(x, int) and x >= 0 for x in c.expect)
            assert c.timeout > 0

    def test_placeholders_known(self):
        """argv 模板里的 {占位符} 必须都在 Sandbox.expand 可提供的键内。"""
        used = set()
        for c in ec.build_checks():
            for part in c.argv:
                used |= set(re.findall(r"\{(\w+)\}", part))
        unknown = used - _KNOWN_PLACEHOLDERS
        assert not unknown, f"未知占位符:{unknown}"

    def test_scan_entry_uses_local_fixture_server(self):
        scan = next(c for c in ec.build_checks() if c.name == "scan_local_fixture")
        assert scan.needs_http
        assert "{port}" in " ".join(scan.argv)
        assert "127.0.0.1" in " ".join(scan.argv)  # 只触回环,零真实外呼

    def test_exactly_one_http_entry(self):
        http_entries = [c.name for c in ec.build_checks() if c.needs_http]
        assert http_entries == ["scan_local_fixture"]

    def test_tui_entries_drive_stdin(self):
        by_name = {c.name: c for c in ec.build_checks()}
        assert by_name["review_tui_list"].stdin == "list\nquit\n"
        assert by_name["batch_tui_groups"].stdin == "groups\nquit\n"


# ---------------------------------------------------------------------------
# --list 与 --only
# ---------------------------------------------------------------------------
class TestListAndFilter:
    def test_list_prints_all_without_running(self, capsys):
        rc = ec.main(["--list"])
        out = capsys.readouterr().out
        assert rc == ec.EXIT_OK
        assert "体检清单" in out and "期望退出码" in out
        for name in [c.name for c in ec.build_checks()]:
            assert name in out

    def test_only_substring_filter(self):
        checks = ec.build_checks()
        hits = ec._filter_checks(checks, ["help"])
        assert {c.name for c in hits} >= {
            "main_help", "submit_help", "batchflow_help", "finishflow_help",
        }
        assert all("help" in c.name.lower() for c in hits)

    def test_only_union_and_case_insensitive(self):
        hits = ec._filter_checks(ec.build_checks(), ["WEBUI_import_app", "golive"])
        assert {c.name for c in hits} == {"webui_import_app", "golive_check_offline"}

    def test_only_no_match_is_usage_error(self, capsys):
        rc = ec.main(["--only", "不存在的条目xyz"], checks=[_PASS])
        assert rc == ec.EXIT_USAGE
        assert "未命中" in capsys.readouterr().err

    def test_duplicate_injected_names_rejected(self, capsys):
        rc = ec.main([], checks=[_PASS, _PASS])
        assert rc == ec.EXIT_USAGE
        assert "重复名称" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# runner 分类(真子进程,假命令)
# ---------------------------------------------------------------------------
class TestRunnerClassification:
    @pytest.fixture()
    def sandbox(self):
        box = ec.Sandbox(ROOT)
        yield box
        box.cleanup()

    def test_pass(self, sandbox):
        r = ec.run_entry(_PASS, sandbox, port=None)
        assert r.ok and r.exit_code == 0 and not r.timeout
        assert r.expected == [0] and r.seconds >= 0
        assert any("假条目通过" in ln for ln in r.tail)

    def test_fail_by_exit_code(self, sandbox):
        r = ec.run_entry(_FAIL, sandbox, port=None)
        assert not r.ok and r.exit_code == 3 and r.expected == [0]
        assert any("假条目失败" in ln for ln in r.tail)

    def test_expect_set_membership(self, sandbox):
        entry = _fake("expect_3", "raise SystemExit(3)", expect=(0, 3))
        assert ec.run_entry(entry, sandbox, port=None).ok

    def test_timeout_classified_as_fail(self, sandbox):
        entry = _fake("slow", "import time; time.sleep(3)", timeout=1.0)
        r = ec.run_entry(entry, sandbox, port=None)
        assert not r.ok and r.timeout and r.exit_code is None
        assert any("超时" in ln for ln in r.tail)

    def test_stdin_piped_to_child(self, sandbox):
        entry = _fake(
            "echo_stdin", "import sys; print('回显:' + sys.stdin.read().strip())"
        )
        entry = ec.CheckEntry(
            name=entry.name, argv=entry.argv, stdin="list\nquit\n"
        )
        r = ec.run_entry(entry, sandbox, port=None)
        assert r.ok
        assert any("回显:list" in ln for ln in r.tail)

    def test_tail_keeps_three_lines(self, sandbox):
        entry = _fake("many_lines", "print('一'); print('二'); print('三'); print('四')")
        r = ec.run_entry(entry, sandbox, port=None)
        assert r.tail == ["二", "三", "四"]

    def test_placeholder_expansion(self, sandbox):
        argv = sandbox.expand(("-m", "netsentinel.cli.review_tui", "--db", "{db}"), port=None)
        assert argv[-1] == sandbox.placeholders["db"]
        assert "{db}" not in " ".join(argv)


# ---------------------------------------------------------------------------
# 退出码语义与 JSON 报告
# ---------------------------------------------------------------------------
class TestMainSemantics:
    def test_all_pass_returns_zero(self, capsys):
        assert ec.main([], checks=[_pass("p1"), _pass("p2")]) == ec.EXIT_OK
        assert "失败 0" in capsys.readouterr().out

    def test_any_fail_returns_one(self, capsys):
        assert ec.main([], checks=[_PASS, _FAIL]) == ec.EXIT_FAIL
        out = capsys.readouterr().out
        assert "失败 1" in out and "always_fail" in out

    def test_json_report_structure(self, capsys, tmp_path):
        report = tmp_path / "report.json"
        rc = ec.main(["--json", str(report)], checks=[_PASS, _FAIL])
        assert rc == ec.EXIT_FAIL  # 有失败仍是 1(导出成功不改语义)
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["tool"] == "netsentinel.entry_check"
        assert data["version"] == 1
        assert set(data["summary"]) == {"total", "passed", "failed"}
        assert data["summary"] == {"total": 2, "passed": 1, "failed": 1}
        assert isinstance(data["total_seconds"], (int, float))
        assert len(data["results"]) == 2
        fields = {
            "name", "ok", "exit_code", "expected", "timeout",
            "seconds", "tail", "argv", "note",
        }
        for item in data["results"]:
            assert fields <= set(item)
        by_name = {r["name"]: r for r in data["results"]}
        assert by_name["always_pass"]["ok"] is True
        assert by_name["always_fail"]["ok"] is False
        assert by_name["always_fail"]["exit_code"] == 3
        assert "JSON 报告已写入" in capsys.readouterr().out

    def test_json_write_error_is_usage_error(self, tmp_path, capsys):
        bad_dir = tmp_path / "not_a_dir.json"  # 用目录路径当报告路径 → 写入失败
        bad_dir.mkdir()
        rc = ec.main(["--json", str(bad_dir)], checks=[_PASS])
        assert rc == ec.EXIT_USAGE
        assert "写入失败" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 沙箱隔离:环境变量注入与 NETSENTINEL_* 剥离
# ---------------------------------------------------------------------------
class TestSandboxIsolation:
    @pytest.fixture()
    def sandbox(self):
        box = ec.Sandbox(ROOT)
        yield box
        box.cleanup()

    def test_env_overrides_injected_and_secrets_stripped(self, sandbox, monkeypatch):
        """子进程应拿到 NETSENTINEL_DATA_DIR/NO_BROWSER 注入,且本机遗留的
        NETSENTINEL_*(密钥等)被剥离——用 echo 类假条目在子进程内验证。"""
        monkeypatch.setenv("NETSENTINEL_GLM_API_KEY", "不该被继承的密钥")
        monkeypatch.setenv("NETSENTINEL_YANDEX_USER", "不该被继承的用户")
        code = (
            "import os, sys\n"
            "env = os.environ\n"
            "data_dir = env.get('NETSENTINEL_DATA_DIR', '')\n"
            "assert data_dir and os.path.isdir(data_dir), data_dir\n"
            "assert env.get('NETSENTINEL_NO_BROWSER') == '1'\n"
            "assert 'NETSENTINEL_GLM_API_KEY' not in env\n"
            "assert 'NETSENTINEL_YANDEX_USER' not in env\n"
            "print('DATA_DIR=' + data_dir)\n"
        )
        entry = _fake("env_probe", code)
        r = ec.run_entry(entry, sandbox, port=None)
        assert r.ok, f"环境注入断言失败:{r.tail}"

    def test_sandbox_config_isolation_switches(self, sandbox):
        """隔离 config.yaml 必须把落盘路径全部指向沙箱并关掉危险开关。"""
        import yaml

        cfg = yaml.safe_load(
            Path(sandbox.placeholders["config"]).read_text(encoding="utf-8")
        )
        box = str(sandbox.dir)
        for key in (
            "data_dir", "evidence_dir", "db_path", "audit_path", "log_path",
            "vlm_cache_db", "phash_db", "graph_db", "discovery_cache_db",
            "agent_task_db", "model_runtime_path", "watchlist_path",
        ):
            assert cfg[key].startswith(box), f"{key} 未指向沙箱:{cfg[key]}"
        assert cfg["takeover_auto"] is False  # 杜绝连接向导(不启动常驻服务)
        assert cfg["onboarding_auto_open"] is False
        assert cfg["vlm_online"] is False and cfg["discovery_online"] is False
        assert cfg["allow_network"] is False and cfg["dry_run_default"] is True

    def test_sandbox_artifacts_exist(self, sandbox):
        """replay/audit 所需工件(独立空账本+空队列、最小审计卷)就位。"""
        for key in ("db", "queue", "ledger", "audit", "keywords", "watchlist"):
            assert Path(sandbox.placeholders[key]).is_file(), key
        # 词表是体检器自建的体检示例词(不读真实 data/keywords/)
        text = Path(sandbox.placeholders["keywords"]).read_text(encoding="utf-8")
        assert "体检示例词甲" in text

    def test_cleanup_removes_sandbox(self, sandbox):
        box = ec.Sandbox(ROOT)
        target = box.dir
        assert target.is_dir()
        box.cleanup()
        assert not target.exists()


# ---------------------------------------------------------------------------
# 门控真跑样本(默认跳;NETSENTINEL_ENTRY_CHECK_E2E=1 启用)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.environ.get("NETSENTINEL_ENTRY_CHECK_E2E"),
    reason="设 NETSENTINEL_ENTRY_CHECK_E2E=1 启用真跑样本(--only help,秒级)",
)
def test_real_help_sample_e2e(capsys):
    """真跑 4 条 --help 快速条目:体检器对真实项目入口端到端可用。"""
    rc = ec.main(["--only", "help"])
    out = capsys.readouterr().out
    assert rc == ec.EXIT_OK, out[-2000:]
    assert "失败 0" in out
