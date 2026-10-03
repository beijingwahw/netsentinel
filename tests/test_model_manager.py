"""A144 活动模型管理器测试(全离线:接缝注入伪 tester,零外呼)。

覆盖:往返 / 四来源 / 校验拒绝与 --force / apply 两分支 / 原子写并发 /
损坏重建 / parse 失败中文 / providers 缺席 RuntimeError / status / clear /
跨实例持久化 / tester 注入协议。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import pytest

from netsentinel.contracts import Config
from netsentinel.vision import model_manager as mm
from netsentinel.vision.model_manager import ModelManager

#: 契约登记的四个切换来源(§2:takeover / wizard / cli / rest)
SWITCH_SOURCES = ["takeover", "wizard", "cli", "rest"]


class _Recorder:
    """伪 tester:记录 (spec, cfg) 调用并返回成功结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, spec, cfg):
        self.calls.append((spec, cfg))
        return {"ok": True, "latency_ms": 3, "model": str(spec)}


def make_cfg() -> Config:
    cfg = Config()
    cfg.classifier = "glm:glm-4.5v"
    cfg.ensemble_members = ["glm", "openai"]
    return cfg


@pytest.fixture(autouse=True)
def offline_runtime(monkeypatch):
    """全离线兜底:缺省 tester / 缺省 cfg 接缝全部替换为内存伪实现。

    仓库中 A147 connectivity 可能已就位,若不替换会触发真实回环探测;
    本文件任何用例都不允许外呼(红线 32)。
    """
    rec = _Recorder()
    monkeypatch.setattr(mm, "_load_connectivity_tester", lambda: rec)
    monkeypatch.setattr(mm, "_load_default_cfg", lambda: None)
    return rec


# --------------------------------------------------------------- 往返与落盘


def test_set_get_roundtrip(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "model_runtime.json"))
    assert mgr.get_active() is None
    mgr.set_active("qwen:qwen-vl-max")
    assert mgr.get_active() == "qwen:qwen-vl-max"


def test_roundtrip_preserves_spec_verbatim(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    spec = "openrouter:qwen/qwen2-vl-7b:free"  # 模型名内部含冒号
    mgr.set_active(spec)
    assert mgr.get_active() == spec


def test_spec_whitespace_stripped(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("  ollama:llava  ")
    assert mgr.get_active() == "ollama:llava"


def test_set_active_returns_full_status_and_writes_record(tmp_path: Path):
    path = tmp_path / "m.json"
    mgr = ModelManager(str(path))
    result = mgr.set_active("ollama:llava", switched_by="wizard")
    assert set(result) == {"spec", "switched_by", "switched_at"}
    assert result["spec"] == "ollama:llava"
    assert result["switched_by"] == "wizard"
    dt.datetime.fromisoformat(result["switched_at"])  # ISO 时间戳可解析
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {"spec", "switched_at", "switched_by"}
    assert raw["spec"] == "ollama:llava"


# --------------------------------------------------------------- 切换来源


@pytest.mark.parametrize("source", SWITCH_SOURCES)
def test_switched_by_four_sources_persisted(tmp_path: Path, source: str):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("glm:glm-4.5v", switched_by=source)
    assert mgr.status()["switched_by"] == source
    assert (
        json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))["switched_by"]
        == source
    )


def test_switched_by_defaults_to_manual(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("ollama:llava")  # 未指定来源
    assert mgr.status()["switched_by"] == "manual"


# --------------------------------------------------------------- 连通性校验


def test_validate_failure_rejected_keeps_previous(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("openai", switched_by="cli", validate=False)  # 基线
    failing = lambda spec, cfg: {"ok": False, "error": "连接超时:本地端口无响应"}
    with pytest.raises(ValueError) as excinfo:
        mgr.set_active("glm:glm-4.5v", tester=failing)
    message = str(excinfo.value)
    assert "模型连通性测试未通过" in message
    assert "glm:glm-4.5v" in message
    assert "连接超时" in message  # 失败原因必须透出
    assert mgr.get_active() == "openai"  # 拒绝后原状态不动
    assert mgr.status()["switched_by"] == "cli"


def test_validate_failure_writes_nothing(tmp_path: Path):
    path = tmp_path / "m.json"
    mgr = ModelManager(str(path))
    failing = lambda spec, cfg: {"ok": False, "error": "密钥无效"}
    with pytest.raises(ValueError):
        mgr.set_active("ollama:llava", tester=failing)
    assert not path.exists()  # 连临时产物都不留
    assert list(tmp_path.glob("*.tmp")) == []


def test_force_validate_false_bypasses_tester(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    rec = _Recorder()

    def failing(spec, cfg):  # pragma: no cover - 不应被调用
        rec.calls.append((spec, cfg))
        return {"ok": False, "error": "连接超时"}

    mgr.set_active("ollama:llava", validate=False, tester=failing)  # --force 语义
    assert mgr.get_active() == "ollama:llava"
    assert rec.calls == []  # 测试器根本不被调用


def test_injected_tester_receives_spec_and_cfg(tmp_path: Path):
    cfg = make_cfg()
    mgr = ModelManager(str(tmp_path / "m.json"), cfg=cfg)
    rec = _Recorder()
    mgr.set_active("glm", validate=True, tester=rec)
    assert len(rec.calls) == 1
    spec_arg, cfg_arg = rec.calls[0]
    assert spec_arg == "glm"
    assert cfg_arg is cfg  # 注入协议第二参 = 构造时给的 cfg


def test_default_tester_used_when_not_injected(tmp_path: Path, offline_runtime):
    mgr = ModelManager(str(tmp_path / "m.json"))  # 未注入 cfg → cfg=None
    mgr.set_active("ollama:llava")  # validate 缺省 True
    assert len(offline_runtime.calls) == 1
    assert offline_runtime.calls[0] == ("ollama:llava", None)
    assert mgr.get_active() == "ollama:llava"


def test_default_tester_absent_degrades_to_syntax_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mm, "_load_connectivity_tester", lambda: None)  # A147 缺席
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("openai")  # 仅语法校验,不测连通
    assert mgr.get_active() == "openai"


def test_default_tester_failure_message(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        mm,
        "_load_connectivity_tester",
        lambda: lambda spec, cfg: {"ok": False, "error": "本地端口无响应"},
    )
    mgr = ModelManager(str(tmp_path / "m.json"))
    with pytest.raises(ValueError, match="本地端口无响应"):
        mgr.set_active("vllm:test-model")


def test_tester_non_dict_result_rejected(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    with pytest.raises(ValueError, match="无法识别的结果"):
        mgr.set_active("ollama:llava", tester=lambda spec, cfg: "看起来通了")
    assert mgr.get_active() is None


def test_tester_exception_rejected_with_reason(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))

    def boom(spec, cfg):
        raise RuntimeError("探测包构造失败")

    with pytest.raises(ValueError, match="探测包构造失败"):
        mgr.set_active("ollama:llava", tester=boom)
    assert mgr.get_active() is None


# --------------------------------------------------------------- 语法校验


@pytest.mark.parametrize("bad", ["nope:x", "火星大模型"])
def test_unknown_provider_chinese_valueerror(tmp_path: Path, bad: str):
    mgr = ModelManager(str(tmp_path / "m.json"))
    with pytest.raises(ValueError, match="未知视觉模型提供方"):
        mgr.set_active(bad)
    assert mgr.get_active() is None


def test_providers_absent_raises_runtimeerror(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mm, "_load_parse_spec", lambda: None)  # A61 缺席
    mgr = ModelManager(str(tmp_path / "m.json"))
    with pytest.raises(RuntimeError, match="尚未就位"):
        mgr.set_active("ollama:llava")
    assert mgr.get_active() is None


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_spec_rejected(tmp_path: Path, empty):
    mgr = ModelManager(str(tmp_path / "m.json"))
    with pytest.raises(ValueError, match="不能为空"):
        mgr.set_active(empty)


# --------------------------------------------------------------- stub 与 apply


def test_stub_skips_connectivity_and_replaces_ensemble(tmp_path: Path, offline_runtime):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("stub")  # validate 缺省 True,但离线桩无外部服务可测
    assert offline_runtime.calls == []  # 缺省 tester 不被调用
    assert mgr.get_active() == "stub"
    cfg = make_cfg()
    result = mgr.apply(cfg)
    assert result is cfg
    assert cfg.classifier == "stub"
    assert cfg.ensemble_members == ["stub"]  # 整套替换(红线 34)


def test_apply_normal_spec_keeps_ensemble(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    mgr.set_active("ollama:llava")
    cfg = make_cfg()
    mgr.apply(cfg)
    assert cfg.classifier == "ollama:llava"
    assert cfg.ensemble_members == ["glm", "openai"]  # 不动集成成员


def test_apply_without_active_is_noop(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    cfg = make_cfg()
    result = mgr.apply(cfg)
    assert result is cfg
    assert cfg.classifier == "glm:glm-4.5v"  # 保持原样
    assert cfg.ensemble_members == ["glm", "openai"]


# --------------------------------------------------------------- status / clear


def test_status_shapes_empty_and_active(tmp_path: Path):
    mgr = ModelManager(str(tmp_path / "m.json"))
    assert mgr.status() == {"spec": None}  # 未连接:仅 spec=None
    mgr.set_active("glm:glm-4.5v", switched_by="rest")
    status = mgr.status()
    assert set(status) == {"spec", "switched_by", "switched_at"}
    assert status["spec"] == "glm:glm-4.5v"
    assert status["switched_by"] == "rest"


def test_clear_resets_to_empty(tmp_path: Path):
    path = tmp_path / "m.json"
    mgr = ModelManager(str(path))
    mgr.set_active("ollama:llava")
    mgr.clear()
    assert mgr.get_active() is None
    assert mgr.status() == {"spec": None}
    assert json.loads(path.read_text(encoding="utf-8")) == {}


# --------------------------------------------------------------- 损坏与容错


@pytest.mark.parametrize("garbage", ["not-json{{{", "[1, 2, 3]", ""])
def test_corrupt_json_rebuilt_as_empty(tmp_path: Path, garbage: str):
    path = tmp_path / "m.json"
    path.write_text(garbage, encoding="utf-8")
    mgr = ModelManager(str(path))
    assert mgr.get_active() is None
    assert mgr.status() == {"spec": None}
    assert json.loads(path.read_text(encoding="utf-8")) == {}  # 已重建
    mgr.set_active("ollama:llava")  # 重建后可正常写入
    assert mgr.get_active() == "ollama:llava"


def test_nonstring_spec_in_record_treated_as_none(tmp_path: Path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"spec": 123}), encoding="utf-8")  # 手改坏值
    mgr = ModelManager(str(path))
    assert mgr.get_active() is None
    assert mgr.status() == {"spec": None}


def test_hand_edited_record_gets_defaults(tmp_path: Path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"spec": "openai"}), encoding="utf-8")  # 缺来源/时间
    mgr = ModelManager(str(path))
    status = mgr.status()
    assert status["spec"] == "openai"
    assert status["switched_by"] == "manual"
    assert status["switched_at"] == ""


# --------------------------------------------------------------- 持久化与并发


def test_persistence_across_instances(tmp_path: Path):
    path = str(tmp_path / "m.json")
    ModelManager(path).set_active("glm:glm-4.5v", switched_by="takeover")
    mgr2 = ModelManager(path)  # 新实例直读同一文件
    assert mgr2.get_active() == "glm:glm-4.5v"
    assert mgr2.status()["switched_by"] == "takeover"


def test_concurrent_8_threads_final_consistency(tmp_path: Path):
    path = tmp_path / "m.json"
    mgr = ModelManager(str(path))
    threads_count, per_thread = 8, 12
    expected = set()
    barrier = threading.Barrier(threads_count)

    def worker(idx: int) -> None:
        barrier.wait()
        for k in range(per_thread):
            mgr.set_active(f"ollama:llava-{idx}-{k}", switched_by="cli", validate=False)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_count)]
    expected = {
        f"ollama:llava-{i}-{k}" for i in range(threads_count) for k in range(per_thread)
    }
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = json.loads(path.read_text(encoding="utf-8"))  # 文件仍是合法 JSON
    assert set(raw) == {"spec", "switched_at", "switched_by"}
    assert raw["spec"] in expected  # 最后一致:是某次完整写入
    assert raw["switched_by"] == "cli"
    assert mgr.get_active() == raw["spec"]
    assert mgr.status()["spec"] == raw["spec"]
    assert list(tmp_path.glob("*.tmp")) == []  # 无临时文件残留


def test_parent_directory_auto_created(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "model_runtime.json"
    mgr = ModelManager(str(path))
    mgr.set_active("ollama:llava")
    assert path.is_file()
    assert mgr.get_active() == "ollama:llava"
