"""A01:netsentinel.config 单元测试(离线,只写 tmp_path)。"""
from __future__ import annotations

import logging
import pathlib
import sys

import pytest

from netsentinel import telemetry
from netsentinel.config import (
    _reset_unknown_key_warnings,
    load_config,
    save_config,
)
from netsentinel.contracts import Config


@pytest.fixture()
def isolated_cwd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """把 cwd 切到空 tmp_path,隔离 load_config() 的默认 ./config.yaml。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _assert_chinese(msg: str) -> None:
    """错误消息应为中文。"""
    assert any("\u4e00" <= ch <= "\u9fff" for ch in msg), f"错误消息应为中文:{msg}"


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

def test_load_config_defaults_without_file(isolated_cwd: pathlib.Path) -> None:
    cfg = load_config()
    assert isinstance(cfg, Config)
    # 判定阈值
    assert cfg.nsfw_threshold == 0.90
    assert cfg.review_threshold == 0.50
    assert cfg.prob_count_line == 0.80
    assert cfg.min_nsw_images == 3
    assert cfg.min_image_px == 200
    # 抓取预算
    assert cfg.max_pages == 5
    assert cfg.max_images_per_page == 12
    assert cfg.max_image_mb == 8
    assert cfg.fetch_timeout_s == 15.0
    assert cfg.fetch_delay_s == 1.0
    assert cfg.respect_robots is True
    # 安全默认(红线)
    assert cfg.allow_network is False
    assert cfg.human_gate_required is True
    assert cfg.dry_run_default is True
    assert cfg.submit_min_interval_s == 60
    assert cfg.submit_max_per_day == 5
    # 分类器与路径
    assert cfg.classifier == "stub"
    assert cfg.ensemble_members == ["stub"]
    assert cfg.log_path == "data/logs/netsentinel.log"


def test_load_config_defaults_when_pyyaml_missing(
    isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyYAML 缺失且无自定义文件 → 直接返回默认,不报错。"""
    monkeypatch.setitem(sys.modules, "yaml", None)  # 使 import yaml 抛 ImportError
    cfg = load_config()
    assert cfg == Config()


def test_load_config_missing_explicit_file_returns_defaults(
    isolated_cwd: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        cfg = load_config(str(isolated_cwd / "nope.yaml"))
    assert cfg == Config()
    assert "不存在" in caplog.text


def test_load_config_missing_pyyaml_with_custom_file_raises_hint(
    isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = isolated_cwd / "config.yaml"
    p.write_text("max_pages: 3\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(ImportError, match="PyYAML"):
        load_config(str(p))


def test_load_config_empty_yaml_returns_defaults(isolated_cwd: pathlib.Path) -> None:
    p = isolated_cwd / "config.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(str(p)) == Config()


# ---------------------------------------------------------------------------
# YAML 覆盖
# ---------------------------------------------------------------------------

def test_yaml_overrides_known_fields(isolated_cwd: pathlib.Path) -> None:
    p = isolated_cwd / "custom.yaml"
    p.write_text(
        "\n".join([
            "nsfw_threshold: 1",            # int → 自动转 float
            "review_threshold: 0.6",
            "max_pages: 3",
            "fetch_timeout_s: 20.5",
            "ensemble_members: [nudenet, clip]",
            "portal_12377_base: http://127.0.0.1:8900",
            "log_path: data/logs/other.log",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.nsfw_threshold == 1.0
    assert isinstance(cfg.nsfw_threshold, float)
    assert cfg.review_threshold == 0.6
    assert cfg.max_pages == 3
    assert cfg.fetch_timeout_s == 20.5
    assert cfg.ensemble_members == ["nudenet", "clip"]
    assert cfg.portal_12377_base == "http://127.0.0.1:8900"
    assert cfg.log_path == "data/logs/other.log"
    # 未覆盖的键保持默认
    assert cfg.submit_max_per_day == 5


def test_default_path_config_yaml_picked_up(isolated_cwd: pathlib.Path) -> None:
    (isolated_cwd / "config.yaml").write_text(
        "max_pages: 3\n", encoding="utf-8"
    )
    cfg = load_config()
    assert cfg.max_pages == 3


# ---------------------------------------------------------------------------
# 范围校验(违反抛 ValueError,中文消息含字段名与当前值)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("yaml_text", "field", "bad_value"),
    [
        ("review_threshold: 0.95\nnsfw_threshold: 0.9\n", "review_threshold", "0.95"),
        ("review_threshold: 0\n", "review_threshold", "0"),
        ("nsfw_threshold: 1.5\n", "nsfw_threshold", "1.5"),
        ("prob_count_line: 0.95\nnsfw_threshold: 0.9\n", "prob_count_line", "0.95"),
        ("submit_min_interval_s: 10\n", "submit_min_interval_s", "10"),
        ("submit_max_per_day: 99\n", "submit_max_per_day", "99"),
        ("submit_max_per_day: 0\n", "submit_max_per_day", "0"),
        ("max_pages: 0\n", "max_pages", "0"),
    ],
)
def test_invalid_ranges_raise_value_error(
    isolated_cwd: pathlib.Path, yaml_text: str, field: str, bad_value: str
) -> None:
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    assert field in msg
    assert bad_value in msg
    _assert_chinese(msg)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "max_pages: many\n",          # int 字段给字符串
        "allow_network: yes-please\n",  # bool 字段给字符串
        "ensemble_members: stub\n",   # 列表字段给标量
        "data_dir: 123\n",            # str 字段给数字
        "- 1\n- 2\n",                 # 顶层不是映射
    ],
)
def test_wrong_types_raise_value_error(
    isolated_cwd: pathlib.Path, yaml_text: str
) -> None:
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    _assert_chinese(str(ei.value))


# ---------------------------------------------------------------------------
# 未知键 / 安全红线
# ---------------------------------------------------------------------------

def test_unknown_key_warns_but_loads(
    isolated_cwd: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = isolated_cwd / "config.yaml"
    p.write_text("max_pages: 4\nbogus_key: hello\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        cfg = load_config(str(p))
    assert cfg.max_pages == 4      # 已知键仍生效
    assert cfg.allow_network is False  # 默认不受影响
    assert "bogus_key" in caplog.text  # 未知键有告警


def test_human_gate_cannot_be_disabled_by_config(
    isolated_cwd: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """红线:human_gate_required=false 必须被强制恢复为 True。"""
    p = isolated_cwd / "config.yaml"
    p.write_text("human_gate_required: false\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        cfg = load_config(str(p))
    assert cfg.human_gate_required is True
    assert "human_gate_required" in caplog.text


# ---------------------------------------------------------------------------
# save → load 往返
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(isolated_cwd: pathlib.Path) -> None:
    cfg = Config(
        nsfw_threshold=0.88,
        review_threshold=0.4,
        prob_count_line=0.75,
        max_pages=7,
        max_image_mb=4,
        allow_network=True,
        classifier="nudenet",
        ensemble_members=["nudenet", "clip"],
        portal_12377_base="http://127.0.0.1:8900",
        data_dir="data2",
        log_path="data2/logs/x.log",
    )
    target = isolated_cwd / "nested" / "dir" / "config.yaml"  # 印证自动建目录
    save_config(cfg, str(target))
    assert target.is_file()
    assert load_config(str(target)) == cfg


def test_save_load_default_config_roundtrip(isolated_cwd: pathlib.Path) -> None:
    target = isolated_cwd / "config.yaml"
    save_config(Config(), str(target))
    assert load_config(str(target)) == Config()


def test_save_config_writes_human_gate_true_forced(
    isolated_cwd: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """红线:即使内存中被改成 False,落盘也强制为 true。"""
    target = isolated_cwd / "config.yaml"
    with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
        save_config(Config(human_gate_required=False), str(target))
    assert "human_gate_required" in caplog.text
    assert load_config(str(target)).human_gate_required is True


def test_save_config_invalid_config_raises(isolated_cwd: pathlib.Path) -> None:
    with pytest.raises(ValueError) as ei:
        save_config(Config(submit_max_per_day=99), str(isolated_cwd / "bad.yaml"))
    _assert_chinese(str(ei.value))
    assert not (isolated_cwd / "bad.yaml").exists()


# ---------------------------------------------------------------------------
# V5 工程升级
# ---------------------------------------------------------------------------

def test_v5_load_config_stats_default_path_once(
    isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 性能:默认路径存在时,加载全程只做一次路径探测(原先 stat 两次)。"""
    (isolated_cwd / "config.yaml").write_text("max_pages: 3\n", encoding="utf-8")
    calls: list[str] = []
    orig_is_file = pathlib.Path.is_file

    def counting_is_file(self: pathlib.Path, *args: object, **kwargs: object) -> bool:
        calls.append(str(self))
        return orig_is_file(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "is_file", counting_is_file)
    cfg = load_config()
    assert cfg.max_pages == 3
    # 默认路径(相对 cwd 的 config.yaml)整个加载过程只探测一次
    assert calls == [str(pathlib.Path("config.yaml"))]


def test_v5_load_config_explicit_path_still_single_stat(
    isolated_cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V5 性能:显式路径分支同样只探测一次。"""
    p = isolated_cwd / "custom.yaml"
    p.write_text("max_pages: 4\n", encoding="utf-8")
    calls: list[str] = []
    orig_is_file = pathlib.Path.is_file

    def counting_is_file(self: pathlib.Path, *args: object, **kwargs: object) -> bool:
        calls.append(str(self))
        return orig_is_file(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pathlib.Path, "is_file", counting_is_file)
    assert load_config(str(p)).max_pages == 4
    assert calls == [str(p)]


def test_v5_unknown_key_warned_once_per_key(
    isolated_cwd: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """V5 健壮性:同一未知键进程内只告警一次;重置后可再次告警。"""
    p = isolated_cwd / "config.yaml"
    p.write_text("max_pages: 4\nmystery_v5_key: hello\n", encoding="utf-8")
    _reset_unknown_key_warnings()
    try:
        with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
            load_config(str(p))
        assert "mystery_v5_key" in caplog.text  # 首次加载有告警

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
            cfg = load_config(str(p))
        assert "mystery_v5_key" not in caplog.text  # 同键不再重复告警
        assert cfg.max_pages == 4  # 已知键仍生效

        _reset_unknown_key_warnings()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="netsentinel.config"):
            load_config(str(p))
        assert "mystery_v5_key" in caplog.text  # 重置后恢复告警
    finally:
        _reset_unknown_key_warnings()  # 不污染其他用例(如 test_unknown_key_warns_but_loads)


def test_v5_load_and_save_config_telemetry_timers(isolated_cwd: pathlib.Path) -> None:
    """V5 可观测:加载/保存分别计入 telemetry.timer("config.load"/"config.save")。"""
    p = isolated_cwd / "config.yaml"
    p.write_text("max_pages: 3\n", encoding="utf-8")
    telemetry.reset()
    try:
        cfg = load_config(str(p))
        save_config(cfg, str(isolated_cwd / "out" / "config.yaml"))
        snap = telemetry.snapshot()
        assert snap["timers"]["config.load"]["count"] == 1
        assert snap["timers"]["config.save"]["count"] == 1
    finally:
        telemetry.reset()


def test_v5_single_violation_message_byte_identical(isolated_cwd: pathlib.Path) -> None:
    """V5 兼容锁定:单条违规的错误文案与升级前逐字一致(不改前缀与关键字)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text("max_pages: 0\n", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    assert str(ei.value) == "配置项 max_pages=0 无效:必须满足 max_pages >= 1"


def test_v5_validate_groups_multiple_fields(isolated_cwd: pathlib.Path) -> None:
    """V5 质量:多条违规一次性全部抛出,并附字段分组汇总行便于定位。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "review_threshold: 0.95\nnsfw_threshold: 0.9\nmax_pages: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    # 既有文案逐条保留(前缀/关键字不变)
    assert "配置项 review_threshold=0.95 无效" in msg
    assert "配置项 max_pages=0 无效" in msg
    # 新增:字段分组汇总行
    summary = msg.rsplit("校验失败字段分组:", 1)[1]
    assert "review_threshold" in summary
    assert "max_pages" in summary
    _assert_chinese(summary)
    # 保存路径同样享受分组报错
    with pytest.raises(ValueError) as ei2:
        save_config(
            Config(review_threshold=0, max_pages=0), str(isolated_cwd / "bad.yaml")
        )
    assert "校验失败字段分组" in str(ei2.value)
    assert "review_threshold" in str(ei2.value)
    assert "max_pages" in str(ei2.value)


# ---------------------------------------------------------------------------
# V11 配置正式收录(附加属性升格为一等字段)
# ---------------------------------------------------------------------------

#: V11 新字段 → 精确默认值(缺省 = 升格前行为:开关全关/静态带/等权/connectivity)。
_V11_DEFAULTS: dict[str, object] = {
    "graph_wire": False,
    "gang_mode": "connectivity",
    "gang_weight_threshold": 0.3,
    "gang_template_weight_factor": 0.5,
    "gang_resolution": 1.0,
    "ensemble_reliability_weights": False,
    "cascade_risk_budget": None,
    "abstain_threshold": 0.35,
    "bundle_sign_algo": "hmac-sha256",
    "ed25519_seed_hex": None,
    "tsa_url": None,
}

_V11_SEED_HEX = "ab" * 32  # 合法 32 字节 seed 的十六进制(仅测试,无真实密钥)


def test_v11_defaults_are_first_class_fields() -> None:
    """纯默认 Config 即带全部 V11 一等字段;值 = 升格前 getattr 缺省行为。"""
    cfg = Config()
    for name, want in _V11_DEFAULTS.items():
        assert hasattr(cfg, name), f"Config 缺 V11 字段 {name}"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值应为 {want!r},实为 {got!r}"
        )
        # 消费方既有 getattr 读取路径命中字段(缺省值语义不变)
        assert getattr(cfg, name, "未命中") != "未命中"


def test_v11_old_yaml_without_new_keys_keeps_defaults(
    isolated_cwd: pathlib.Path,
) -> None:
    """旧 YAML(无任何新键)加载结果与升格前行为一致(快照断言):
    新字段全部落默认值,既有键照常覆盖,整体 == 显式等值 Config。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "nsfw_threshold: 0.92",
            "review_threshold: 0.4",
            "max_pages: 3",
            "use_sprt: true",
            "concurrency_tier: high",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    # 快照:逐字段等于默认(升格前"属性不存在 + getattr 缺省"的现状行为)
    for name, want in _V11_DEFAULTS.items():
        assert getattr(cfg, name) == want, f"旧 YAML 下 {name} 应保持默认 {want!r}"
    # 整体等值:与升格前显式构造的 Config 完全相等(无隐藏差异)
    assert cfg == Config(
        nsfw_threshold=0.92, review_threshold=0.4, max_pages=3,
        use_sprt=True, concurrency_tier="high",
    )


def test_v11_explicit_values_load(isolated_cwd: pathlib.Path) -> None:
    """显式开:全部新键合法取值按类型规整后生效(可空键 null / 空白串 → None)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "graph_wire: true",
            "gang_mode: community",
            "gang_weight_threshold: 0.4",
            "gang_template_weight_factor: 0.7",
            "gang_resolution: 1.5",
            "ensemble_reliability_weights: true",
            "cascade_risk_budget: 0.9",
            "abstain_threshold: 0.2",
            "bundle_sign_algo: ed25519",
            f"ed25519_seed_hex: {_V11_SEED_HEX}",
            "tsa_url: https://tsa.example.com/ts",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.graph_wire is True
    assert cfg.gang_mode == "community"
    assert cfg.gang_weight_threshold == 0.4
    assert cfg.gang_template_weight_factor == 0.7
    assert cfg.gang_resolution == 1.5
    assert cfg.ensemble_reliability_weights is True
    assert cfg.cascade_risk_budget == 0.9
    assert isinstance(cfg.cascade_risk_budget, float)
    assert cfg.abstain_threshold == 0.2
    assert cfg.bundle_sign_algo == "ed25519"
    assert cfg.ed25519_seed_hex == _V11_SEED_HEX
    assert cfg.tsa_url == "https://tsa.example.com/ts"
    # int → float 自动规整(与既有 float 字段惯例一致)
    p.write_text("cascade_risk_budget: 1\n", encoding="utf-8")
    cfg2 = load_config(str(p))
    assert cfg2.cascade_risk_budget == 1.0 and isinstance(cfg2.cascade_risk_budget, float)


@pytest.mark.parametrize(
    ("yaml_text", "field"),
    [
        ("gang_mode: fuzzy\n", "gang_mode"),
        ("gang_mode: 123\n", "gang_mode"),                    # 非字符串类型拒绝
        ("bundle_sign_algo: rsa\n", "bundle_sign_algo"),
        ("gang_weight_threshold: 1.5\n", "gang_weight_threshold"),
        ("gang_weight_threshold: -0.1\n", "gang_weight_threshold"),
        ("gang_template_weight_factor: 1.2\n", "gang_template_weight_factor"),
        ("gang_resolution: 0\n", "gang_resolution"),
        ("gang_resolution: -1.0\n", "gang_resolution"),
        ("abstain_threshold: 2\n", "abstain_threshold"),
        ("cascade_risk_budget: 1.2\n", "cascade_risk_budget"),
        ("cascade_risk_budget: -0.1\n", "cascade_risk_budget"),
        ("ed25519_seed_hex: zzzz\n", "ed25519_seed_hex"),     # 非十六进制
        (f"ed25519_seed_hex: {'ab' * 16}\n", "ed25519_seed_hex"),  # 长度不足 64
        ("graph_wire: yes-please\n", "graph_wire"),           # bool 字段给字符串
        ("ensemble_reliability_weights: 1\n", "ensemble_reliability_weights"),
        ("cascade_risk_budget: high\n", "cascade_risk_budget"),   # 可空 float 给字符串
        ("tsa_url: 123\n", "tsa_url"),                        # 可空 str 给数字
    ],
)
def test_v11_invalid_values_rejected_chinese(
    isolated_cwd: pathlib.Path, yaml_text: str, field: str
) -> None:
    """非法值拒绝:ValueError、中文、含字段名;且不写任何输出。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    assert field in msg
    _assert_chinese(msg)


def test_v11_null_and_blank_normalise_to_none(isolated_cwd: pathlib.Path) -> None:
    """可空键三态:显式 null / 空串 / 纯空白 → None(未配置 = 现状语义)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "cascade_risk_budget: null\ntsa_url: \"\"\ned25519_seed_hex: \"  \"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.cascade_risk_budget is None
    assert cfg.tsa_url is None
    assert cfg.ed25519_seed_hex is None


def test_v11_save_load_roundtrip_explicit(isolated_cwd: pathlib.Path) -> None:
    """往返 save/load:显式开的新字段逐项读回等值(整体 ==)。"""
    cfg = Config(
        graph_wire=True,
        gang_mode="community",
        gang_weight_threshold=0.4,
        gang_template_weight_factor=0.7,
        gang_resolution=1.5,
        ensemble_reliability_weights=True,
        cascade_risk_budget=0.9,
        abstain_threshold=0.2,
        bundle_sign_algo="ed25519",
        ed25519_seed_hex=_V11_SEED_HEX,
        tsa_url="https://tsa.example.com/ts",
    )
    target = isolated_cwd / "v11.yaml"
    save_config(cfg, str(target))
    loaded = load_config(str(target))
    assert loaded == cfg
    assert loaded.graph_wire is True and loaded.cascade_risk_budget == 0.9


def test_v11_save_default_config_roundtrip(isolated_cwd: pathlib.Path) -> None:
    """默认往返:全默认 save → load 仍 == Config()(None 键落盘 null 后读回 None);
    新键按既有布尔/数值字段惯例全量落盘(asdict 口径)。"""
    target = isolated_cwd / "default.yaml"
    save_config(Config(), str(target))
    text = target.read_text(encoding="utf-8")
    # 持久化语义对齐既有字段惯例:全量写出(含 false / null)
    assert "graph_wire: false" in text
    assert "ensemble_reliability_weights: false" in text
    assert "cascade_risk_budget: null" in text
    assert "tsa_url: null" in text
    assert load_config(str(target)) == Config()


def test_v11_save_invalid_config_raises(isolated_cwd: pathlib.Path) -> None:
    """保存侧同样过取值域校验:非法 V11 值直接构造的 Config 保存时拒绝。"""
    with pytest.raises(ValueError) as ei:
        save_config(
            Config(gang_mode="fuzzy", bundle_sign_algo="rsa"),
            str(isolated_cwd / "bad.yaml"),
        )
    msg = str(ei.value)
    assert "gang_mode" in msg and "bundle_sign_algo" in msg  # 多条违规一次抛出
    _assert_chinese(msg)
    assert not (isolated_cwd / "bad.yaml").exists()


def test_v11_new_keys_are_known_not_unknown(isolated_cwd: pathlib.Path) -> None:
    """新键为已知字段:不触发未知键告警(YAML 未知键告警路径保持不变)。"""
    _reset_unknown_key_warnings()
    try:
        p = isolated_cwd / "config.yaml"
        p.write_text(
            "graph_wire: true\ncascade_risk_budget: 0.5\n", encoding="utf-8"
        )
        import logging as _logging

        records: list[str] = []
        handler = _logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logger = _logging.getLogger("netsentinel.config")
        logger.addHandler(handler)
        try:
            cfg = load_config(str(p))
        finally:
            logger.removeHandler(handler)
        assert cfg.graph_wire is True and cfg.cascade_risk_budget == 0.5
        assert not any("未知配置键" in r for r in records)
    finally:
        _reset_unknown_key_warnings()


def test_v11_consumer_getattr_paths_hit_fields() -> None:
    """消费方兼容:kernel_wire/orchestrator/cascade 的 getattr 读取路径
    命中同名字段,缺省行为与升格前 getattr 缺省完全一致。"""
    from netsentinel.pipeline import kernel_wire, orchestrator
    from netsentinel.vision import cascade

    cfg = Config()
    # orchestrator._default_graph_wire 的门控读法
    assert getattr(cfg, "graph_wire", False) is False
    # kernel_wire.resolve_gangs_from_config 的读法(connectivity 忽略阈值刻度,
    # 故 0.3/0.5 与旧 getattr 缺省 0.0/1.0 的现状行为零差异)
    assert getattr(cfg, "gang_mode", "connectivity") == "connectivity"
    assert getattr(cfg, "gang_resolution", 1.0) == 1.0
    edges = [("site:a", "site:b", "shared_template", 0.2)]
    assert kernel_wire.resolve_gangs_from_config(edges, cfg) == [["site:a", "site:b"]]
    # cascade 的预算读法:默认 None = 静态带(冷启动)
    assert getattr(cfg, "cascade_risk_budget", None) is None
    assert cascade.CascadeClassifier is not None  # 模块可达(真实读法由其测试覆盖)


# ---------------------------------------------------------------------------
# V12 接线收口(trace_enabled / abstain_enabled 升格为一等字段)
# ---------------------------------------------------------------------------

#: V12 新字段 → 精确默认值(缺省 False = 升格前 orchestrator 附加属性
#: getattr 缺省口径;红线 45:trace/弃权默认可关且不触判定)。
_V12_DEFAULTS: dict[str, object] = {
    "trace_enabled": False,
    "abstain_enabled": False,
}


def test_v12_defaults_are_first_class_fields() -> None:
    """纯默认 Config 即带两个 V12 一等布尔字段;值 = 升格前 getattr 缺省行为。"""
    cfg = Config()
    for name, want in _V12_DEFAULTS.items():
        assert hasattr(cfg, name), f"Config 缺 V12 字段 {name}"
        got = getattr(cfg, name)
        assert got is want and type(got) is bool, (
            f"字段 {name} 默认值应为 {want!r},实为 {got!r}"
        )
        # 消费方既有 getattr 读取路径命中字段(缺省值语义不变)
        assert getattr(cfg, name, "未命中") != "未命中"


def test_v12_old_yaml_without_new_keys_keeps_defaults(
    isolated_cwd: pathlib.Path,
) -> None:
    """旧 YAML(无新键)加载结果与升格前行为一致(快照断言):
    两字段落默认 False,既有键照常覆盖,整体 == 显式等值 Config。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "nsfw_threshold: 0.92",
            "review_threshold: 0.4",
            "max_pages: 3",
            "concurrency_tier: high",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    # 快照:逐字段等于默认(升格前"属性不存在 + getattr 缺省 False"的现状行为)
    for name, want in _V12_DEFAULTS.items():
        assert getattr(cfg, name) is want, f"旧 YAML 下 {name} 应保持默认 {want!r}"
    # 整体等值:与升格前显式构造的 Config 完全相等(无隐藏差异)
    assert cfg == Config(
        nsfw_threshold=0.92, review_threshold=0.4, max_pages=3,
        concurrency_tier="high",
    )


def test_v12_explicit_values_load(isolated_cwd: pathlib.Path) -> None:
    """显式三态之"开":两键 true 生效;显式 false 与缺省完全等价。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "trace_enabled: true\nabstain_enabled: true\n", encoding="utf-8"
    )
    cfg = load_config(str(p))
    assert cfg.trace_enabled is True
    assert cfg.abstain_enabled is True
    # 显式 false = 缺省(向后兼容口径)
    p.write_text(
        "trace_enabled: false\nabstain_enabled: false\n", encoding="utf-8"
    )
    cfg2 = load_config(str(p))
    assert cfg2.trace_enabled is False and cfg2.abstain_enabled is False
    assert cfg2 == Config()


@pytest.mark.parametrize(
    ("yaml_text", "field"),
    [
        ("trace_enabled: yes-please\n", "trace_enabled"),        # bool 字段给字符串
        ("trace_enabled: 1\n", "trace_enabled"),                 # bool 字段给 int(YAML 1 非 true)
        ("trace_enabled: null\n", "trace_enabled"),              # 布尔组不接受 null
        ("abstain_enabled: on-record\n", "abstain_enabled"),
        ("abstain_enabled: 0\n", "abstain_enabled"),
    ],
)
def test_v12_invalid_values_rejected_chinese(
    isolated_cwd: pathlib.Path, yaml_text: str, field: str
) -> None:
    """非法值拒绝:ValueError、中文、含字段名;且不写任何输出。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    assert field in msg
    _assert_chinese(msg)


def test_v12_save_load_roundtrip(isolated_cwd: pathlib.Path) -> None:
    """往返 save/load:显式开的字段读回等值;全量落盘含 false 字面。"""
    cfg = Config(trace_enabled=True, abstain_enabled=True)
    target = isolated_cwd / "v12.yaml"
    save_config(cfg, str(target))
    loaded = load_config(str(target))
    assert loaded == cfg
    assert loaded.trace_enabled is True and loaded.abstain_enabled is True
    # 默认往返:布尔字段按既有惯例全量写出(false 字面),读回 == Config()
    default_target = isolated_cwd / "default.yaml"
    save_config(Config(), str(default_target))
    text = default_target.read_text(encoding="utf-8")
    assert "trace_enabled: false" in text
    assert "abstain_enabled: false" in text
    assert load_config(str(default_target)) == Config()


def test_v12_new_keys_are_known_not_unknown(isolated_cwd: pathlib.Path) -> None:
    """新键为已知字段:不触发未知键告警(YAML 未知键告警路径保持不变)。"""
    _reset_unknown_key_warnings()
    try:
        p = isolated_cwd / "config.yaml"
        p.write_text("trace_enabled: true\n", encoding="utf-8")
        import logging as _logging

        records: list[str] = []
        handler = _logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logger = _logging.getLogger("netsentinel.config")
        logger.addHandler(handler)
        try:
            cfg = load_config(str(p))
        finally:
            logger.removeHandler(handler)
        assert cfg.trace_enabled is True
        assert not any("未知配置键" in r for r in records)
    finally:
        _reset_unknown_key_warnings()


def test_v12_consumer_getattr_paths_hit_fields() -> None:
    """消费方兼容:orchestrator run_scan 的 getattr 门控读法命中同名字段,
    缺省行为与升格前 getattr 缺省完全一致(红线 45:默认关且不触判定)。"""
    from netsentinel.pipeline import orchestrator

    cfg = Config()
    # orchestrator.run_scan 的两处门控读法(trace / abstain)
    assert getattr(cfg, "trace_enabled", False) is False
    assert getattr(cfg, "abstain_enabled", False) is False
    # 升格后显式赋值路径依旧可用(A202 测试的 cfg.trace_enabled = True 写法)
    cfg.trace_enabled = True
    cfg.abstain_enabled = True
    assert getattr(cfg, "trace_enabled", False) is True
    assert getattr(cfg, "abstain_enabled", False) is True
    assert orchestrator.run_scan is not None  # 模块可达(真实链路由其测试覆盖)


# ---------------------------------------------------------------------------
# V13 接线残余批配置收录(dynamic_ttl / phash_mt_lsh_db / guard_model_path /
# guard_family 升格为一等字段,CONTRACTS-V13.md §2 点名)
# ---------------------------------------------------------------------------

#: V13 新字段 → 精确默认值(缺省 = 收录前各消费方 getattr 缺省口径:
#: A211 scheduler 动态 TTL 关 / A204 kernel_wire LSH 库走 <phash_db>.mtlsh
#: 推导 / A217 guard_adapter 守卫模型未注入)。
_V13_DEFAULTS: dict[str, object] = {
    "dynamic_ttl": False,
    "phash_mt_lsh_db": None,
    "guard_model_path": "",
    "guard_family": "",
}


def test_v13_defaults_are_first_class_fields() -> None:
    """纯默认 Config 即带全部 V13 一等字段;值 = 收录前 getattr 缺省行为。"""
    cfg = Config()
    for name, want in _V13_DEFAULTS.items():
        assert hasattr(cfg, name), f"Config 缺 V13 字段 {name}"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值应为 {want!r},实为 {got!r}"
        )
        # 消费方既有 getattr 读取路径命中字段(缺省值语义不变)
        assert getattr(cfg, name, "未命中") != "未命中"


def test_v13_old_yaml_without_new_keys_keeps_defaults(
    isolated_cwd: pathlib.Path,
) -> None:
    """旧 YAML(无任何新键)加载结果与收录前行为一致(快照断言):
    四字段落默认值,既有键照常覆盖,整体 == 显式等值 Config。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "nsfw_threshold: 0.92",
            "review_threshold: 0.4",
            "max_pages: 3",
            "concurrency_tier: high",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    # 快照:逐字段等于默认(收录前"属性不存在 + getattr 缺省"的现状行为)
    for name, want in _V13_DEFAULTS.items():
        assert getattr(cfg, name) == want, f"旧 YAML 下 {name} 应保持默认 {want!r}"
    # 整体等值:与收录前显式构造的 Config 完全相等(无隐藏差异)
    assert cfg == Config(
        nsfw_threshold=0.92, review_threshold=0.4, max_pages=3,
        concurrency_tier="high",
    )


def test_v13_explicit_values_load(isolated_cwd: pathlib.Path) -> None:
    """显式三态之"配置":四键合法取值生效(类型逐项核对)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "dynamic_ttl: true",
            "phash_mt_lsh_db: data/custom.mtlsh",
            "guard_model_path: models/shieldgemma-2-4b-it",
            "guard_family: llamaguard",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.dynamic_ttl is True
    assert cfg.phash_mt_lsh_db == "data/custom.mtlsh"
    assert isinstance(cfg.phash_mt_lsh_db, str)
    assert cfg.guard_model_path == "models/shieldgemma-2-4b-it"
    assert cfg.guard_family == "llamaguard"
    # 显式关闭/清空 = 缺省(向后兼容口径)
    p.write_text(
        "dynamic_ttl: false\nphash_mt_lsh_db: null\n"
        "guard_model_path: \"\"\nguard_family: \"\"\n",
        encoding="utf-8",
    )
    cfg2 = load_config(str(p))
    assert cfg2 == Config()


@pytest.mark.parametrize(
    ("yaml_text", "field"),
    [
        ("dynamic_ttl: yes-please\n", "dynamic_ttl"),         # bool 字段给字符串
        ("dynamic_ttl: 1\n", "dynamic_ttl"),                  # bool 字段给 int
        ("dynamic_ttl: null\n", "dynamic_ttl"),               # 布尔组不接受 null
        ("phash_mt_lsh_db: 123\n", "phash_mt_lsh_db"),        # 可空 str 给数字
        ("phash_mt_lsh_db: [a]\n", "phash_mt_lsh_db"),        # 可空 str 给列表
        ("guard_model_path: 99\n", "guard_model_path"),       # str 字段给数字
        ("guard_family: fuzzy\n", "guard_family"),            # 取值域外
        ("guard_family: 123\n", "guard_family"),              # 非字符串类型拒绝
    ],
)
def test_v13_invalid_values_rejected_chinese(
    isolated_cwd: pathlib.Path, yaml_text: str, field: str
) -> None:
    """非法值拒绝:ValueError、中文、含字段名;且不写任何输出。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    assert field in msg
    _assert_chinese(msg)


def test_v13_null_and_blank_normalise_to_none(isolated_cwd: pathlib.Path) -> None:
    """phash_mt_lsh_db 三态:显式 null / 空串 / 纯空白 → None(未配置 = 走
    phash_db 推导的语义;guard_model_path/guard_family 为普通字符串键,
    空串即字面缺省,不归一)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "phash_mt_lsh_db: null\n"
        "guard_model_path: \"\"\n"
        "guard_family: \"\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.phash_mt_lsh_db is None
    assert cfg.guard_model_path == "" and cfg.guard_family == ""
    p.write_text("phash_mt_lsh_db: \"  \"\n", encoding="utf-8")
    assert load_config(str(p)).phash_mt_lsh_db is None


def test_v13_save_load_roundtrip_explicit(isolated_cwd: pathlib.Path) -> None:
    """往返 save/load:显式配置的四字段逐项读回等值(整体 ==)。"""
    cfg = Config(
        dynamic_ttl=True,
        phash_mt_lsh_db="data/custom.mtlsh",
        guard_model_path="models/shieldgemma-2-4b-it",
        guard_family="shieldgemma2",
    )
    target = isolated_cwd / "v13.yaml"
    save_config(cfg, str(target))
    loaded = load_config(str(target))
    assert loaded == cfg
    assert loaded.dynamic_ttl is True and loaded.guard_family == "shieldgemma2"
    # 默认往返:布尔全量写出(false 字面),None 写出 null,读回 == Config()
    default_target = isolated_cwd / "default.yaml"
    save_config(Config(), str(default_target))
    text = default_target.read_text(encoding="utf-8")
    assert "dynamic_ttl: false" in text
    assert "phash_mt_lsh_db: null" in text
    assert "guard_model_path: ''" in text
    assert "guard_family: ''" in text
    assert load_config(str(default_target)) == Config()


def test_v13_save_invalid_config_raises(isolated_cwd: pathlib.Path) -> None:
    """保存侧同样过取值域校验:guard_family 非法的 Config 保存时拒绝。"""
    with pytest.raises(ValueError) as ei:
        save_config(
            Config(guard_family="gpt-guard"),
            str(isolated_cwd / "bad.yaml"),
        )
    msg = str(ei.value)
    assert "guard_family" in msg
    _assert_chinese(msg)
    assert not (isolated_cwd / "bad.yaml").exists()


def test_v13_new_keys_are_known_not_unknown(isolated_cwd: pathlib.Path) -> None:
    """新键为已知字段:不触发未知键告警(YAML 未知键告警路径保持不变)。"""
    _reset_unknown_key_warnings()
    try:
        p = isolated_cwd / "config.yaml"
        p.write_text(
            "dynamic_ttl: true\nphash_mt_lsh_db: data/x.mtlsh\n", encoding="utf-8"
        )
        import logging as _logging

        records: list[str] = []
        handler = _logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logger = _logging.getLogger("netsentinel.config")
        logger.addHandler(handler)
        try:
            cfg = load_config(str(p))
        finally:
            logger.removeHandler(handler)
        assert cfg.dynamic_ttl is True and cfg.phash_mt_lsh_db == "data/x.mtlsh"
        assert not any("未知配置键" in r for r in records)
    finally:
        _reset_unknown_key_warnings()


def test_v13_consumer_getattr_paths_hit_fields() -> None:
    """消费方兼容(只读验证):三处 getattr 读取路径命中同名字段,缺省行为
    与收录前 getattr 缺省完全一致——含 kernel_wire 的 None → 推导分支。"""
    from netsentinel.pipeline import kernel_wire
    from netsentinel.vision import guard_adapter

    cfg = Config()
    # A211 scheduler 的门控读法(scheduler.py:bool(getattr(cfg,...,False)))
    assert getattr(cfg, "dynamic_ttl", False) is False
    # A217 guard_adapter 的注入读法(默认空串 = 未注入,由构造参数决定)
    assert getattr(cfg, "guard_model_path", "") == ""
    assert getattr(cfg, "guard_family", "") == ""
    # A204 kernel_wire._mt_lsh_db_path:字段缺省 None 经 ``str(None or "")``
    # 得 falsy 空串 → 仍走 <phash_db>.mtlsh 推导,路径逐字节不变
    assert cfg.phash_mt_lsh_db is None
    assert kernel_wire._mt_lsh_db_path(cfg) == f"{cfg.phash_db}.mtlsh"
    assert kernel_wire._mt_lsh_db_path(Config(phash_db="data/x.db")) == "data/x.db.mtlsh"
    # 显式配置时覆盖推导(收录前同款注入语义:非空串生效)
    override = Config(phash_mt_lsh_db="data/custom.mtlsh")
    assert kernel_wire._mt_lsh_db_path(override) == "data/custom.mtlsh"
    # 取值域字面量与 guard_adapter.GUARD_FAMILIES 常量口径一致(config 侧
    # 为复写字面量、不 import;此处运行时对账防漂移)
    from netsentinel.config import _GUARD_FAMILIES

    assert _GUARD_FAMILIES == guard_adapter.GUARD_FAMILIES
    # 升格后显式赋值路径依旧可用(A211/A217 测试的 cfg.xxx = ... 写法)
    cfg.dynamic_ttl = True
    cfg.guard_model_path = "models/sg2"
    cfg.guard_family = "llamaguard"
    assert getattr(cfg, "dynamic_ttl", False) is True
    assert cfg.guard_family == "llamaguard"
    assert guard_adapter.GuardModelAdapter is not None  # 模块可达(真实链路由其测试覆盖)


# ---------------------------------------------------------------------------
# V14 接线残余批配置收录(bayes_reliability / bayes_half_life 升格为一等
# 字段,CONTRACTS-V14.md §2 点名"bayes_reliability 待 V14 收录 Config")
# ---------------------------------------------------------------------------

#: V14 新字段 → 精确默认值(缺省 = 收录前 A223 贝叶斯回流的 getattr 缺省
#: 口径:开关关 = 等权现状 / None = 关闭遗忘 = 现状)。
_V14_DEFAULTS: dict[str, object] = {
    "bayes_reliability": False,
    "bayes_half_life": None,
}


def test_v14_defaults_are_first_class_fields() -> None:
    """纯默认 Config 即带全部 V14 一等字段;值 = 收录前 getattr 缺省行为。"""
    cfg = Config()
    for name, want in _V14_DEFAULTS.items():
        assert hasattr(cfg, name), f"Config 缺 V14 字段 {name}"
        got = getattr(cfg, name)
        assert got == want and type(got) is type(want), (
            f"字段 {name} 默认值应为 {want!r},实为 {got!r}"
        )
        # 消费方既有 getattr 读取路径命中字段(缺省值语义不变)
        assert getattr(cfg, name, "未命中") != "未命中"


def test_v14_old_yaml_without_new_keys_keeps_defaults(
    isolated_cwd: pathlib.Path,
) -> None:
    """旧 YAML(无任何新键)加载结果与收录前行为一致(快照断言):
    两字段落默认值,既有键照常覆盖,整体 == 显式等值 Config。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "nsfw_threshold: 0.92",
            "review_threshold: 0.4",
            "max_pages: 3",
            "concurrency_tier: high",
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    # 快照:逐字段等于默认(收录前"属性不存在 + getattr 缺省"的现状行为)
    for name, want in _V14_DEFAULTS.items():
        assert getattr(cfg, name) == want, f"旧 YAML 下 {name} 应保持默认 {want!r}"
    # 整体等值:与收录前显式构造的 Config 完全相等(无隐藏差异)
    assert cfg == Config(
        nsfw_threshold=0.92, review_threshold=0.4, max_pages=3,
        concurrency_tier="high",
    )


def test_v14_explicit_values_load(isolated_cwd: pathlib.Path) -> None:
    """显式三态之"配置":布尔开关 + 半衰期(int 自动转 float);极端正值
    (1.0e-9 天)属正有限数,照常加载。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(
        "\n".join([
            "bayes_reliability: true",
            "bayes_half_life: 7",      # int → 自动转 float
        ]) + "\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.bayes_reliability is True
    assert cfg.bayes_half_life == 7.0
    assert isinstance(cfg.bayes_half_life, float)
    # 极小正数边界:1.0e-9 天(86.4 微秒)仍是正有限数,合法(YAML 1.1 的
    # 科学计数需带小数点才解析为数字,故不用 1e-9 写法)
    p.write_text("bayes_half_life: 1.0e-9\n", encoding="utf-8")
    cfg2 = load_config(str(p))
    assert cfg2.bayes_half_life == 1.0e-9
    # 显式关闭 = 缺省(向后兼容口径)
    p.write_text("bayes_reliability: false\nbayes_half_life: null\n", encoding="utf-8")
    cfg3 = load_config(str(p))
    assert cfg3 == Config()


@pytest.mark.parametrize(
    ("yaml_text", "field"),
    [
        ("bayes_reliability: yes-please\n", "bayes_reliability"),   # bool 字段给字符串
        ("bayes_reliability: 1\n", "bayes_reliability"),            # bool 字段给 int
        ("bayes_reliability: null\n", "bayes_reliability"),         # 布尔组不接受 null
        ("bayes_half_life: 0\n", "bayes_half_life"),                # 非正(零)
        ("bayes_half_life: -3\n", "bayes_half_life"),               # 非正(负)
        ("bayes_half_life: .inf\n", "bayes_half_life"),             # 非有限(YAML inf)
        ("bayes_half_life: .nan\n", "bayes_half_life"),             # 非有限(YAML NaN)
        ("bayes_half_life: week\n", "bayes_half_life"),             # 可空数值给字符串
        ("bayes_half_life: true\n", "bayes_half_life"),             # 可空数值给布尔
        ("bayes_half_life: [7]\n", "bayes_half_life"),              # 可空数值给列表
    ],
)
def test_v14_invalid_values_rejected_chinese(
    isolated_cwd: pathlib.Path, yaml_text: str, field: str
) -> None:
    """非法值拒绝:ValueError、中文、含字段名;且不写任何输出。"""
    p = isolated_cwd / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_config(str(p))
    msg = str(ei.value)
    assert field in msg
    _assert_chinese(msg)


def test_v14_null_normalises_to_none(isolated_cwd: pathlib.Path) -> None:
    """bayes_half_life 显式 null → None = 关闭遗忘(未配置语义,两态:
    null / 数字,无字符串空白归一)。"""
    p = isolated_cwd / "config.yaml"
    p.write_text("bayes_half_life: null\n", encoding="utf-8")
    assert load_config(str(p)).bayes_half_life is None


def test_v14_save_load_roundtrip_explicit(isolated_cwd: pathlib.Path) -> None:
    """往返 save/load:显式配置的两字段逐项读回等值(整体 ==)。"""
    cfg = Config(bayes_reliability=True, bayes_half_life=7.5)
    target = isolated_cwd / "v14.yaml"
    save_config(cfg, str(target))
    loaded = load_config(str(target))
    assert loaded == cfg
    assert loaded.bayes_reliability is True and loaded.bayes_half_life == 7.5
    # 默认往返:布尔全量写出(false 字面),None 写出 null,读回 == Config()
    default_target = isolated_cwd / "default.yaml"
    save_config(Config(), str(default_target))
    text = default_target.read_text(encoding="utf-8")
    assert "bayes_reliability: false" in text
    assert "bayes_half_life: null" in text
    assert load_config(str(default_target)) == Config()


def test_v14_save_invalid_config_raises(isolated_cwd: pathlib.Path) -> None:
    """保存侧同样过取值域校验:半衰期零/负/NaN 的 Config 保存时拒绝。"""
    for bad in (0.0, -3.0, float("inf"), float("nan")):
        with pytest.raises(ValueError) as ei:
            save_config(
                Config(bayes_half_life=bad),
                str(isolated_cwd / "bad.yaml"),
            )
        msg = str(ei.value)
        assert "bayes_half_life" in msg
        _assert_chinese(msg)
        assert not (isolated_cwd / "bad.yaml").exists()


def test_v14_new_keys_are_known_not_unknown(isolated_cwd: pathlib.Path) -> None:
    """新键为已知字段:不触发未知键告警(YAML 未知键告警路径保持不变)。"""
    _reset_unknown_key_warnings()
    try:
        p = isolated_cwd / "config.yaml"
        p.write_text(
            "bayes_reliability: true\nbayes_half_life: 7\n", encoding="utf-8"
        )
        import logging as _logging

        records: list[str] = []
        handler = _logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
        logger = _logging.getLogger("netsentinel.config")
        logger.addHandler(handler)
        try:
            cfg = load_config(str(p))
        finally:
            logger.removeHandler(handler)
        assert cfg.bayes_reliability is True and cfg.bayes_half_life == 7.0
        assert not any("未知配置键" in r for r in records)
    finally:
        _reset_unknown_key_warnings()


def test_v14_consumer_getattr_paths_hit_fields() -> None:
    """消费方兼容(只读验证):orchestrator 贝叶斯回流的两处 getattr 读取
    路径命中同名字段,缺省行为与收录前 getattr 缺省完全一致。"""
    from netsentinel.pipeline import orchestrator

    cfg = Config()
    # _reliability_ensemble_weights 的开关门控读法
    assert getattr(cfg, "bayes_reliability", False) is False
    # _default_bayes_reliability_tracker 的半衰期读法(None = 关闭遗忘)
    assert getattr(cfg, "bayes_half_life", None) is None
    # 升格后显式赋值路径依旧可用(A223 测试的 cfg.bayes_reliability = True 写法)
    cfg.bayes_reliability = True
    cfg.bayes_half_life = 7.0
    assert getattr(cfg, "bayes_reliability", False) is True
    assert getattr(cfg, "bayes_half_life", None) == 7.0
    assert orchestrator.run_scan is not None  # 模块可达(真实链路由其测试覆盖)
