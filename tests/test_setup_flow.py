"""A153:netsentinel.setup.flow 单元测试(离线,只注入 fake / monkeypatch)。

并行态确定性:A150 ``vision.capability`` 可能就位也可能未就位,autouse 夹具
经 ``sys.modules[name] = None`` 强制缺席,需要严格视觉判定的用例再按需注入
fake 模块——两种并行态下结论一致。

覆盖(契约 §3 A153 行 + §0 红线 34):

- ``SetupState``:str Enum 四态、值字符串互认;
- ``next_step``:四态中文提示逐一断言;STUB_ONLY 文案含"离线桩 /
  非模型判定 / 仅供参考"(红线 34 明示);字符串入参;非法入参中文报错;
- ``recommend`` 三分支:本地优先(端口→spec 映射 11434/1234/8000/9997、
  视觉过滤、ok 过滤、首个命中、模型字典形态、A150 缺席全返回、本地压过
  云密钥)、云密钥(显式表 / 缺省惰性 keys.configured / 体检异常降级)、
  全无 → wizard;
- ``persist``/``load``:四态往返、损坏 JSON 回退、未知值回退、非 dict
  回退、文件缺席回退、非法状态中文报错、自动建父目录、遥测计数;
- 转移表 ``ALLOWED``:合法六对(向导完成本地/云激活 + stub 任意态可入)
  与非法方向逐一断言;``can_transition`` 字符串规整与未知拒绝;
  ``allowed_targets``。
"""
from __future__ import annotations

import json
import pathlib
import sys
import types
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.security import keys
from netsentinel.setup import flow
from netsentinel.setup.flow import SetupState

#: 被测模块惰性依赖的兄弟模块(autouse 强制缺席,用例按需注入 fake)
SIBLINGS = ("netsentinel.vision.capability",)

#: autouse 之前抓下的真实 keys.configured(供"缺省惰性体检"真实路径用例)
REAL_CONFIGURED = keys.configured

#: 四态值字符串(str Enum 的 value)
STATE_VALUES = ("no_model", "local_connected", "cloud_connected", "stub_only")


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:兄弟模块强制缺席 + 密钥体检退空表 + 遥测清零。"""
    for name in SIBLINGS:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(keys, "configured", lambda cfg: {})
    telemetry.reset()


def make_cfg(**overrides: Any) -> Config:
    """构造测试配置(无文件副作用;recommend 只读密钥体检)。"""
    kw: dict[str, Any] = {}
    kw.update(overrides)
    return Config(**kw)


def install_capability(monkeypatch: pytest.MonkeyPatch,
                       markers: tuple[str, ...] = ("llava", "qwen2.5vl")) -> None:
    """注入 fake A150 模块:名字含任一标记才判视觉(严格过滤,便于反例)。"""
    mod = types.ModuleType("netsentinel.vision.capability")
    mod.is_vision_model = lambda name: any(m in str(name) for m in markers)
    monkeypatch.setitem(sys.modules, "netsentinel.vision.capability", mod)


def entry(provider: str = "ollama", base_url: str = "http://127.0.0.1:11434",
          models: list[Any] | None = None, ok: bool = True) -> dict[str, Any]:
    """构造一条 A143 形态的扫描结果。"""
    return {"provider": provider, "base_url": base_url,
            "models": models if models is not None else [], "ok": ok}


def stub_keys(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, bool]) -> None:
    """把 keys.configured 替换为受控布尔表(覆盖 autouse 的空表)。"""
    monkeypatch.setattr(keys, "configured", lambda cfg: dict(mapping))


class RecordingKeys:
    """记录 cfg 入参并返回受控布尔表的伪 keys.configured。"""

    def __init__(self, mapping: dict[str, bool]) -> None:
        self.mapping = mapping
        self.calls: list[Config] = []

    def __call__(self, cfg: Config) -> dict[str, bool]:
        self.calls.append(cfg)
        return dict(self.mapping)


def clear_all_provider_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空内置 20 家约定两形态密钥环境变量,防宿主机污染。"""
    for provider in keys.BUILTIN_PROVIDERS:
        upper = provider.upper()
        for name in (f"NETSENTINEL_{upper}_API_KEY", f"{upper}_API_KEY"):
            monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# SetupState:枚举形态
# ---------------------------------------------------------------------------


class TestSetupState:
    def test_four_members_with_lowercase_values(self):
        """四态齐备,值为约定小写字符串(str Enum 可直接序列化)。"""
        assert [m.value for m in SetupState] == list(STATE_VALUES)
        assert len(set(STATE_VALUES)) == 4

    def test_str_enum_equality_with_values(self):
        """str Enum:成员与其值字符串相等,按值查成员互认。"""
        assert SetupState.NO_MODEL == "no_model"
        assert SetupState("cloud_connected") is SetupState.CLOUD_CONNECTED


# ---------------------------------------------------------------------------
# next_step:四态中文提示(含红线 34 文案)
# ---------------------------------------------------------------------------


class TestNextStep:
    @pytest.mark.parametrize("state, expected", [
        (SetupState.NO_MODEL, "请扫描本机服务或配置云平台密钥"),
        (SetupState.LOCAL_CONNECTED, "已连接本地视觉模型,可直接开始扫描"),
        (SetupState.CLOUD_CONNECTED, "已连接云平台(注意 vlm_online 与预算)"),
        (SetupState.STUB_ONLY, "正在使用离线桩,非模型判定,结果仅供参考"),
    ])
    def test_hint_text_for_each_state(self, state, expected):
        """四态中文提示与契约逐一一致。"""
        assert flow.next_step(state) == expected

    def test_stub_hint_red_line_34_wording(self):
        """红线 34:STUB_ONLY 提示必须明示"离线桩""非模型判定""仅供参考"。"""
        hint = flow.next_step(SetupState.STUB_ONLY)
        assert "离线桩" in hint
        assert "非模型判定" in hint
        assert "仅供参考" in hint

    def test_accepts_value_and_name_strings(self):
        """字符串入参(值 / 名称、大小写不敏感)与成员同义。"""
        assert flow.next_step("no_model") == flow.next_step(SetupState.NO_MODEL)
        assert flow.next_step("LOCAL_CONNECTED") == flow.next_step(
            SetupState.LOCAL_CONNECTED)

    def test_invalid_state_raises_chinese(self):
        """未知状态抛中文 ValueError(列出全部合法值,宁可显式失败)。"""
        with pytest.raises(ValueError, match="未知的向导流程状态"):
            flow.next_step("connected_to_everything")
        with pytest.raises(ValueError, match="no_model"):
            flow.next_step(None)


# ---------------------------------------------------------------------------
# recommend:本地优先分支
# ---------------------------------------------------------------------------


class TestRecommendLocal:
    def test_ollama_port_maps_to_spec(self):
        """11434 端口 → ollama,spec 为 提供方:首个视觉模型。"""
        result = flow.recommend(
            [entry(models=["llava:13b"])], make_cfg())
        assert result == {"action": "activate_local", "spec": "ollama:llava:13b",
                          "state": SetupState.LOCAL_CONNECTED}

    @pytest.mark.parametrize("port, provider", [
        ("1234", "lmstudio"),
        ("8000", "vllm"),
        ("9997", "xinference"),
    ])
    def test_other_local_ports_map_to_providers(self, port, provider):
        """1234/8000/9997 端口分别映射 lmstudio/vllm/xinference。"""
        result = flow.recommend(
            [entry(provider=provider,
                   base_url=f"http://127.0.0.1:{port}",
                   models=["qwen2.5vl:7b"])],
            make_cfg(), keys_configured={})
        assert result["action"] == "activate_local"
        assert result["spec"] == f"{provider}:qwen2.5vl:7b"
        assert result["state"] is SetupState.LOCAL_CONNECTED

    def test_first_vision_model_wins_in_order(self):
        """多个视觉模型时按扫描顺序取首个(llava 在前则不用 qwen2.5vl)。"""
        result = flow.recommend(
            [entry(models=["llava:7b", "qwen2.5vl:7b"])], make_cfg())
        assert result["spec"] == "ollama:llava:7b"

    def test_text_only_models_not_recommended(self, monkeypatch):
        """视觉过滤:只有文本模型(llama3,A150 严格判非视觉)不走本地。"""
        install_capability(monkeypatch)
        result = flow.recommend(
            [entry(models=["llama3:8b", "gemma-7b"])],
            make_cfg(), keys_configured={})
        assert result == {"action": "wizard", "state": SetupState.NO_MODEL}

    def test_not_ok_entries_skipped(self, monkeypatch):
        """ok=False 的条目(服务未就绪)不算,后续 ok 条目仍可命中。"""
        install_capability(monkeypatch)
        result = flow.recommend(
            [entry(models=["llava:13b"], ok=False),
             entry(models=["qwen2.5vl:7b"])],
            make_cfg())
        assert result["action"] == "activate_local"
        assert result["spec"] == "ollama:qwen2.5vl:7b"

    def test_dict_shaped_models_supported(self, monkeypatch):
        """健壮性:模型为 {"id": ...} 字典形态同样识别。"""
        install_capability(monkeypatch)
        result = flow.recommend(
            [entry(models=[{"id": "llava:7b"}])], make_cfg())
        assert result["spec"] == "ollama:llava:7b"

    def test_capability_absent_degrades_to_any_model(self):
        """A150 缺席(autouse 强制)→ 缺席全返回:文本模型名也算候选。"""
        result = flow.recommend(
            [entry(models=["some-text-model"])], make_cfg(), keys_configured={})
        assert result["action"] == "activate_local"
        assert result["spec"] == "ollama:some-text-model"

    def test_local_priority_over_cloud_keys(self):
        """本地优先:本地命中时即使云密钥已配也推荐 activate_local。"""
        result = flow.recommend(
            [entry(models=["llava"])],
            make_cfg(), keys_configured={"glm": True, "openai": True})
        assert result["action"] == "activate_local"
        assert result["state"] is SetupState.LOCAL_CONNECTED

    def test_unknown_port_slug_provider_fallback(self):
        """端口未知时回退条目 provider(白名单形态),如自定义端口跑 lmstudio。"""
        result = flow.recommend(
            [entry(provider="lmstudio", base_url="http://127.0.0.1:5000",
                   models=["llava"])],
            make_cfg(), keys_configured={})
        assert result["action"] == "activate_local"
        assert result["spec"] == "lmstudio:llava"

    def test_unknown_port_descriptive_provider_skipped(self, monkeypatch):
        """描述性 provider("openai 兼容")不是合法 spec 提供方 → 跳过该条,
        降级到云密钥分支。"""
        install_capability(monkeypatch)
        result = flow.recommend(
            [entry(provider="openai 兼容", base_url="http://127.0.0.1:5000",
                   models=["llava"])],
            make_cfg(), keys_configured={"glm": True})
        assert result["action"] == "use_cloud"

    def test_empty_and_garbage_results(self):
        """空列表 / 非字典条目 / 空模型列表都不至于抛错或误命中。"""
        for results in ([], ["not-a-dict"], [entry(models=[])], None):
            outcome = flow.recommend(results, make_cfg(), keys_configured={})
            assert outcome == {"action": "wizard", "state": SetupState.NO_MODEL}


# ---------------------------------------------------------------------------
# recommend:云密钥分支与缺省惰性体检
# ---------------------------------------------------------------------------


class TestRecommendCloud:
    def test_any_configured_key_uses_cloud(self):
        """无本地但任一密钥 True → use_cloud + CLOUD_CONNECTED,无 spec 键。"""
        result = flow.recommend(
            [], make_cfg(), keys_configured={"glm": True, "openai": False})
        assert result == {"action": "use_cloud", "state": SetupState.CLOUD_CONNECTED}
        assert "spec" not in result

    def test_all_false_keys_fall_to_wizard(self):
        """密钥表全 False → wizard(还需向导引导)。"""
        result = flow.recommend(
            [], make_cfg(), keys_configured={"glm": False, "qwen": False})
        assert result == {"action": "wizard", "state": SetupState.NO_MODEL}

    def test_default_lazy_keys_configured_receives_cfg(self, monkeypatch):
        """keys_configured 缺省 → 惰性调 keys.configured 且传入原 cfg。"""
        recorder = RecordingKeys({"glm": True})
        monkeypatch.setattr(keys, "configured", recorder)
        cfg = make_cfg()
        result = flow.recommend([], cfg)
        assert recorder.calls == [cfg]
        assert result["action"] == "use_cloud"

    def test_keys_configured_error_degrades_to_wizard(self, monkeypatch):
        """keys.configured 抛错 → 按未配置降级(不向上抛),推荐向导。"""
        def boom(cfg):
            raise RuntimeError("密钥环异常")

        monkeypatch.setattr(keys, "configured", boom)
        assert flow.recommend([], make_cfg()) == {
            "action": "wizard", "state": SetupState.NO_MODEL}

    def test_real_env_key_source_uses_cloud(self, tmp_path, monkeypatch):
        """真实 keys.configured 路径:清空全部密钥环境变量后仅设
        NETSENTINEL_QWEN_API_KEY → use_cloud;家目录指向 tmp 防文件污染。"""
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
        monkeypatch.setattr(keys, "configured", REAL_CONFIGURED)
        monkeypatch.setattr(keys, "_load_catalog", lambda: None)
        clear_all_provider_envs(monkeypatch)
        monkeypatch.setenv("NETSENTINEL_QWEN_API_KEY", "sk-test-12345678")
        assert flow.recommend([], make_cfg())["action"] == "use_cloud"


# ---------------------------------------------------------------------------
# persist / load:持久化与损坏回退
# ---------------------------------------------------------------------------


class TestPersistLoad:
    @pytest.mark.parametrize("state", list(SetupState))
    def test_roundtrip_all_states(self, tmp_path, state):
        """四态 persist → load 往返保真。"""
        path = tmp_path / "setup_state.json"
        flow.persist(path, state)
        assert flow.load(path) is state

    def test_corrupt_file_falls_back_to_no_model(self, tmp_path):
        """损坏 JSON(乱码文本)→ 回退 NO_MODEL,不抛。"""
        path = tmp_path / "setup_state.json"
        path.write_text("{这不是合法json", encoding="utf-8")
        assert flow.load(path) is SetupState.NO_MODEL

    def test_unknown_state_value_falls_back(self, tmp_path):
        """合法 JSON 但状态值未知 → 回退 NO_MODEL。"""
        path = tmp_path / "setup_state.json"
        path.write_text(json.dumps({"state": "connected_everywhere"}),
                        encoding="utf-8")
        assert flow.load(path) is SetupState.NO_MODEL

    def test_non_dict_json_falls_back(self, tmp_path):
        """非 dict JSON(裸串 / 列表)→ 回退 NO_MODEL。"""
        path = tmp_path / "setup_state.json"
        path.write_text(json.dumps(["local_connected"]), encoding="utf-8")
        assert flow.load(path) is SetupState.NO_MODEL

    def test_missing_file_is_no_model(self, tmp_path):
        """文件缺席 → 全新安装缺省态 NO_MODEL。"""
        assert flow.load(tmp_path / "absent.json") is SetupState.NO_MODEL

    def test_persist_invalid_state_raises_chinese(self, tmp_path):
        """persist 非法状态 → 中文 ValueError,不落盘。"""
        path = tmp_path / "setup_state.json"
        with pytest.raises(ValueError, match="未知的向导流程状态"):
            flow.persist(path, "half_connected")
        assert not path.exists()

    def test_persist_creates_parents_and_payload_shape(self, tmp_path):
        """自动建父目录;文件为 JSON,含 state 值与 updated_at 时间戳。"""
        path = tmp_path / "data" / "nested" / "setup_state.json"
        returned = flow.persist(path, "stub_only")
        assert returned == path and path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["state"] == "stub_only"
        assert "updated_at" in data

    def test_persist_telemetry_counter(self, tmp_path):
        """persist 成功记 setup.state_persisted 一次。"""
        flow.persist(tmp_path / "s.json", SetupState.CLOUD_CONNECTED)
        assert telemetry.snapshot()["counters"].get(
            flow.STATE_PERSISTED_METRIC) == 1

    def test_fallback_telemetry_counter_on_corruption(self, tmp_path):
        """损坏回退记 setup.state_fallback(自愈可观测)。"""
        path = tmp_path / "setup_state.json"
        path.write_text("garbage", encoding="utf-8")
        flow.load(path)
        assert telemetry.snapshot()["counters"].get(
            flow.STATE_FALLBACK_METRIC) == 1


# ---------------------------------------------------------------------------
# 转移表 ALLOWED / can_transition / allowed_targets
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_allowed_table_exact_pairs(self):
        """合法转移恰为六对:向导完成本地/云激活 + stub 从任意态(含自身)进入。"""
        expected = {
            (SetupState.NO_MODEL, SetupState.LOCAL_CONNECTED),
            (SetupState.NO_MODEL, SetupState.CLOUD_CONNECTED),
            (SetupState.NO_MODEL, SetupState.STUB_ONLY),
            (SetupState.LOCAL_CONNECTED, SetupState.STUB_ONLY),
            (SetupState.CLOUD_CONNECTED, SetupState.STUB_ONLY),
            (SetupState.STUB_ONLY, SetupState.STUB_ONLY),
        }
        assert set(flow.ALLOWED) == expected

    @pytest.mark.parametrize("source, target", [
        (SetupState.NO_MODEL, SetupState.LOCAL_CONNECTED),
        (SetupState.NO_MODEL, SetupState.CLOUD_CONNECTED),
        (SetupState.LOCAL_CONNECTED, SetupState.STUB_ONLY),
    ])
    def test_can_transition_legal(self, source, target):
        """合法方向 can_transition 为 True。"""
        assert flow.can_transition(source, target) is True

    @pytest.mark.parametrize("source, target", [
        # 同态自留不算转移(stub 重入除外)
        (SetupState.NO_MODEL, SetupState.NO_MODEL),
        (SetupState.LOCAL_CONNECTED, SetupState.LOCAL_CONNECTED),
        (SetupState.CLOUD_CONNECTED, SetupState.CLOUD_CONNECTED),
        # 已连接不退回无模型
        (SetupState.LOCAL_CONNECTED, SetupState.NO_MODEL),
        (SetupState.CLOUD_CONNECTED, SetupState.NO_MODEL),
        (SetupState.STUB_ONLY, SetupState.NO_MODEL),
        # 本地/云互切须经显式切换入口,不在向导首启状态机内
        (SetupState.LOCAL_CONNECTED, SetupState.CLOUD_CONNECTED),
        (SetupState.CLOUD_CONNECTED, SetupState.LOCAL_CONNECTED),
        (SetupState.STUB_ONLY, SetupState.LOCAL_CONNECTED),
        (SetupState.STUB_ONLY, SetupState.CLOUD_CONNECTED),
    ])
    def test_can_transition_illegal(self, source, target):
        """非法方向 can_transition 为 False。"""
        assert flow.can_transition(source, target) is False

    def test_can_transition_accepts_strings(self):
        """字符串入参与成员同义(值 / 名称均识别)。"""
        assert flow.can_transition("no_model", "stub_only") is True
        assert flow.can_transition("NO_MODEL", "LOCAL_CONNECTED") is True
        assert flow.can_transition("local_connected", "cloud_connected") is False

    def test_can_transition_unknown_state_rejected(self):
        """任一端未知 → False(宁可拒绝也不猜)。"""
        assert flow.can_transition("mystery", "stub_only") is False
        assert flow.can_transition(None, "stub_only") is False
        assert flow.can_transition("no_model", "mystery") is False

    def test_allowed_targets_per_state(self):
        """allowed_targets:NO_MODEL 三去向;连接态只能进桩;桩态仅自入。"""
        assert flow.allowed_targets(SetupState.NO_MODEL) == {
            SetupState.LOCAL_CONNECTED, SetupState.CLOUD_CONNECTED,
            SetupState.STUB_ONLY}
        assert flow.allowed_targets(SetupState.LOCAL_CONNECTED) == {
            SetupState.STUB_ONLY}
        assert flow.allowed_targets(SetupState.CLOUD_CONNECTED) == {
            SetupState.STUB_ONLY}
        assert flow.allowed_targets(SetupState.STUB_ONLY) == {
            SetupState.STUB_ONLY}

    def test_allowed_targets_invalid_raises_chinese(self):
        """allowed_targets 非法入参沿用中文 ValueError 口径。"""
        with pytest.raises(ValueError, match="未知的向导流程状态"):
            flow.allowed_targets("half_connected")
