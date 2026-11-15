"""V10.21:NETSENTINEL_DATA_DIR 全链路接入(load_config 统一环境覆盖)单元测试。

覆盖口径:
- ``apply_data_dir`` 纯函数:env 设置 → data_dir + 派生子路径按默认布局整体
  迁移;env 未设/空串/纯空白 → 原样返回(缺省行为逐字节不变);
- ``load_config`` 三个返回路径(文件缺失默认 / 空 YAML 默认 / 正常加载)
  统一应用覆盖;**优先级 env > config.yaml 文件值 > 字段默认**(webui 现状
  ``apply_data_dir(load_config())`` 即"加载后再覆盖",env 胜出);
- 与 webui/app.py 的 ``apply_data_dir`` 语义逐字段对齐(webui 双重覆盖幂等)。

离线安全:全部写入 tmp_path,绝不触碰真实 data/。
"""
from __future__ import annotations

import os
import pathlib

import pytest

from netsentinel.config import DATA_DIR_ENV, apply_data_dir, load_config
from netsentinel.contracts import Config

#: Config 的 7 个数据路径字段 → 默认值(contracts.py 冻结口径)。
_DEFAULT_PATHS: dict[str, str] = {
    "data_dir": "data",
    "evidence_dir": "data/evidence",
    "db_path": "data/review_queue.db",
    "audit_path": "data/audit.jsonl",
    "log_path": "data/logs/netsentinel.log",
    "vlm_cache_db": "data/vlm_cache.db",
}

#: 环境覆盖时随 root 重派生的子路径布局(webui apply_data_dir 同款)。
def _derived(root: str) -> dict[str, str]:
    p = pathlib.Path(root)
    return {
        "data_dir": root,
        "evidence_dir": str(p / "evidence"),
        "db_path": str(p / "review_queue.db"),
        "audit_path": str(p / "audit.jsonl"),
        "log_path": str(p / "logs" / "netsentinel.log"),
        "vlm_cache_db": str(p / "vlm_cache.db"),
    }


@pytest.fixture()
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保 NETSENTINEL_DATA_DIR 未设置(缺省行为测试的前置)。"""
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)


@pytest.fixture()
def isolated_cwd(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """把 cwd 切到空 tmp_path,隔离 load_config() 的默认 ./config.yaml。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _assert_paths(cfg: Config, expected: dict[str, str]) -> None:
    for field, want in expected.items():
        got = getattr(cfg, field)
        assert got == want, f"cfg.{field} 应为 {want!r},实际 {got!r}"


# ---------------------------------------------------------------------------
# apply_data_dir 纯函数
# ---------------------------------------------------------------------------

class TestApplyDataDirPure:
    def test_env_override_migrates_all_derived_paths(self, tmp_path: pathlib.Path) -> None:
        """env 设置 → data_dir 与 5 个派生子路径按默认布局整体迁移。"""
        cfg = apply_data_dir(Config(), env={DATA_DIR_ENV: str(tmp_path)})
        _assert_paths(cfg, _derived(str(tmp_path)))

    def test_missing_env_returns_cfg_unchanged(self) -> None:
        """env 未设置 → 原样返回(与默认 Config 逐字段一致)。"""
        cfg = apply_data_dir(Config(), env={})
        _assert_paths(cfg, _DEFAULT_PATHS)

    def test_blank_env_treated_as_unset(self) -> None:
        """空串 / 纯空白 env 视为未设置(webui 同款 strip 语义)。"""
        for blank in ("", "   ", "\t"):
            cfg = apply_data_dir(Config(), env={DATA_DIR_ENV: blank})
            _assert_paths(cfg, _DEFAULT_PATHS)

    def test_env_value_stripped(self, tmp_path: pathlib.Path) -> None:
        """带首尾空白的有效值按去除空白后的 root 派生。"""
        root = str(tmp_path)
        cfg = apply_data_dir(Config(), env={DATA_DIR_ENV: f"  {root}  "})
        _assert_paths(cfg, _derived(root))

    def test_override_is_idempotent(self, tmp_path: pathlib.Path) -> None:
        """幂等:双重覆盖结果一致(webui 的 load_config+apply 双层依赖此性质)。"""
        once = apply_data_dir(Config(), env={DATA_DIR_ENV: str(tmp_path)})
        twice = apply_data_dir(
            apply_data_dir(Config(), env={DATA_DIR_ENV: str(tmp_path)}),
            env={DATA_DIR_ENV: str(tmp_path)},
        )
        _assert_paths(twice, _derived(str(tmp_path)))
        assert once == twice

    def test_process_env_used_when_env_param_none(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 参数缺省时读进程环境 os.environ。"""
        monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
        cfg = apply_data_dir(Config())
        _assert_paths(cfg, _derived(str(tmp_path)))

    def test_phash_graph_discovery_db_not_migrated(self, tmp_path: pathlib.Path) -> None:
        """phash_db/graph_db/discovery_cache_db 不在迁移清单(webui 现状口径)。"""
        cfg = apply_data_dir(Config(), env={DATA_DIR_ENV: str(tmp_path)})
        assert cfg.phash_db == "data/phash.db"
        assert cfg.graph_db == "data/graph.db"
        assert cfg.discovery_cache_db == "data/discovery_cache.db"


# ---------------------------------------------------------------------------
# load_config 统一接入
# ---------------------------------------------------------------------------

class TestLoadConfigEnvOverride:
    def test_env_override_on_missing_config_file(
        self, isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """文件缺失返回默认路径 + env 覆盖(CLI 无 config.yaml 的主路径)。"""
        monkeypatch.setenv(DATA_DIR_ENV, str(isolated_cwd))
        cfg = load_config()
        _assert_paths(cfg, _derived(str(isolated_cwd)))

    def test_env_override_on_empty_yaml(
        self, isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空 YAML → 全默认 + env 覆盖。"""
        (isolated_cwd / "config.yaml").write_text("", encoding="utf-8")
        monkeypatch.setenv(DATA_DIR_ENV, str(isolated_cwd))
        cfg = load_config()
        _assert_paths(cfg, _derived(str(isolated_cwd)))

    def test_env_beats_explicit_yaml_values(
        self, isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """优先级:env > config.yaml 显式 data_dir/db_path(webui 现状口径)。"""
        (isolated_cwd / "config.yaml").write_text(
            "\n".join([
                f"data_dir: {isolated_cwd / 'from-file'}",
                f"db_path: {isolated_cwd / 'from-file' / 'review_queue.db'}",
                f"evidence_dir: {isolated_cwd / 'from-file' / 'evidence'}",
                "max_pages: 3",
            ]) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(DATA_DIR_ENV, str(isolated_cwd))
        cfg = load_config()
        _assert_paths(cfg, _derived(str(isolated_cwd)))
        # 非路径字段仍走文件值
        assert cfg.max_pages == 3

    def test_without_env_defaults_byte_identical(
        self, isolated_cwd: pathlib.Path, no_env: None
    ) -> None:
        """无 env → 与默认 Config 逐字节一致(等值 + 各路径字段原样)。"""
        cfg = load_config()
        assert cfg == Config()
        _assert_paths(cfg, _DEFAULT_PATHS)

    def test_without_env_file_values_still_win(
        self, isolated_cwd: pathlib.Path, no_env: None
    ) -> None:
        """无 env → config.yaml 显式路径照常生效(升级前行为不变)。"""
        (isolated_cwd / "config.yaml").write_text(
            f"data_dir: {isolated_cwd / 'from-file'}\n"
            f"db_path: {isolated_cwd / 'from-file' / 'review_queue.db'}\n",
            encoding="utf-8",
        )
        cfg = load_config()
        assert cfg.data_dir == str(isolated_cwd / "from-file")
        assert cfg.db_path == str(isolated_cwd / "from-file" / "review_queue.db")

    def test_empty_env_string_treated_as_unset(
        self, isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空串 env 视为未设 → 默认逐字节不变。"""
        monkeypatch.setenv(DATA_DIR_ENV, "")
        cfg = load_config()
        assert cfg == Config()
        _assert_paths(cfg, _DEFAULT_PATHS)

    def test_whitespace_env_treated_as_unset(
        self, isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """纯空白 env 视为未设 → 默认逐字节不变。"""
        monkeypatch.setenv(DATA_DIR_ENV, "   ")
        cfg = load_config()
        _assert_paths(cfg, _DEFAULT_PATHS)


# ---------------------------------------------------------------------------
# 与 webui 语义对齐(契约:两副本并行,语义必须一致)
# ---------------------------------------------------------------------------

class TestWebuiSemanticsAlignment:
    def test_same_output_as_webui_apply_data_dir(self, tmp_path: pathlib.Path) -> None:
        """同一 env 下,config 版与 webui 版输出逐字段一致。"""
        webui_app = pytest.importorskip("webui.app", reason="webui.app 不可导入")
        env = {DATA_DIR_ENV: str(tmp_path)}
        core = apply_data_dir(Config(), env=env)
        webui = webui_app.apply_data_dir(Config(), env=env)
        for field in _DEFAULT_PATHS:
            assert getattr(core, field) == getattr(webui, field), f"{field} 两副本不一致"

    def test_double_apply_webui_over_core_is_stable(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """webui 实际调用序 load_config→apply_data_dir 的双重覆盖结果稳定。"""
        webui_app = pytest.importorskip("webui.app", reason="webui.app 不可导入")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
        cfg = webui_app.apply_data_dir(load_config())
        _assert_paths(cfg, _derived(str(tmp_path)))


# ---------------------------------------------------------------------------
# CLI 侧消费口径(cfg 在手的 getattr 兜底点自动受益)
# ---------------------------------------------------------------------------

def test_getattr_data_dir_fallback_sees_override(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仓内 ``getattr(cfg, "data_dir", "data")`` 兜底点读到覆盖后的值。

    batchflow/sessionflow/finishflow/summary_agent/setup.daemon/vlm 等消费方
    均持 cfg 调 load_config,无需各自改动即随本席接入受益——此处以同一读取
    口径回归该性质。
    """
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    cfg = load_config()
    assert str(getattr(cfg, "data_dir", "data") or "data") == str(tmp_path)
    assert os.path.abspath(getattr(cfg, "db_path", "data/review_queue.db")).startswith(
        str(tmp_path)
    )
