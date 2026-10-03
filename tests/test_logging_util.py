"""A01:netsentinel.logging_util 单元测试(离线,只写 tmp_path)。"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import shutil
import threading

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.logging_util import _HANDLER_MARK, JsonlAuditLogger, setup_logging
from netsentinel.security import merkle


def _own_handlers() -> list[logging.Handler]:
    root = logging.getLogger()
    return [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]


def _flush_own_file_handlers() -> None:
    for handler in _own_handlers():
        handler.flush()


@pytest.fixture()
def clean_logging():
    """用例结束后摘除 setup_logging 挂上的自有 handler 并恢复 root 级别。"""
    root = logging.getLogger()
    orig_level = root.level
    yield
    for handler in _own_handlers():
        root.removeHandler(handler)
        handler.close()
    root.setLevel(orig_level)


# ---------------------------------------------------------------------------
# JsonlAuditLogger
# ---------------------------------------------------------------------------

def test_log_event_writes_valid_json_lines(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "audit" / "nested" / "audit.jsonl"  # 父目录需自动创建
    audit = JsonlAuditLogger(str(p))
    audit.log_event("scan_started", site_url="http://example.com", max_pages=5)
    audit.log_event("human_confirmed", note="人工确认通过", verdict="nsfw")

    assert p.is_file()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])

    assert rec1["event"] == "scan_started"
    assert rec1["site_url"] == "http://example.com"
    assert rec1["max_pages"] == 5
    assert isinstance(rec1["ts"], str) and rec1["ts"]  # 自动带 ts

    assert rec2["event"] == "human_confirmed"
    assert rec2["note"] == "人工确认通过"  # 中文按 utf-8 原样写出
    assert rec2["verdict"] == "nsfw"
    assert isinstance(rec2["ts"], str) and rec2["ts"]
    # ts 形如 ISO8601(至少含日期与时分秒)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", rec2["ts"])


def test_log_event_recreates_parent_dir(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "audit" / "audit.jsonl"
    audit = JsonlAuditLogger(str(p))
    audit.log_event("first", ok=True)
    assert p.is_file()
    shutil.rmtree(tmp_path / "audit")  # 模拟目录被外部清理
    audit.log_event("second", ok=True)
    # 目录与文件被重建,至少新事件不丢
    assert p.is_file()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "second"


def test_log_event_serializes_odd_values(tmp_path: pathlib.Path) -> None:
    """不可 JSON 序列化的值以 str() 兜底,审计不中断。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p))
    audit.log_event("bundle", path=tmp_path, extra={"n": 1})
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["event"] == "bundle"
    assert str(tmp_path) in rec["path"]
    assert rec["extra"] == {"n": 1}


def test_log_event_ts_takes_precedence(tmp_path: pathlib.Path) -> None:
    """字段名 ts 与自动时间戳冲突时,以自动 ts 为准。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p))
    audit.log_event("real_event", ts="fake")
    rec = json.loads(p.read_text(encoding="utf-8"))
    assert rec["event"] == "real_event"
    assert rec["ts"] != "fake"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", rec["ts"])


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

def test_setup_logging_creates_log_file_with_full_format(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    log_path = tmp_path / "logs" / "netsentinel.log"  # 父目录需自动创建
    cfg = Config(log_path=str(log_path))
    setup_logging(cfg)

    assert log_path.is_file()
    logging.getLogger("netsentinel.probe").warning("文件日志格式检查")
    _flush_own_file_handlers()

    content = log_path.read_text(encoding="utf-8")
    line = content.strip().splitlines()[-1]
    # 文件格式含 时间 / 级别 / 模块
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}", line)
    assert "WARNING" in line
    assert "test_logging_util" in line  # %(module)s 为调用方模块
    assert "文件日志格式检查" in line


def test_setup_logging_idempotent_no_duplicate_handlers(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    log_path = tmp_path / "logs" / "netsentinel.log"
    cfg = Config(log_path=str(log_path))

    setup_logging(cfg)
    handlers_first = _own_handlers()
    assert len(handlers_first) == 2  # 控制台 + 文件各一个
    assert sum(type(h) is logging.StreamHandler for h in handlers_first) == 1
    assert sum(isinstance(h, logging.FileHandler) for h in handlers_first) == 1

    setup_logging(cfg)
    handlers_second = _own_handlers()
    assert len(handlers_second) == 2
    assert {id(h) for h in handlers_second}.isdisjoint({id(h) for h in handlers_first})

    # 行为验证:一条日志只落盘一次(无重复 handler 输出)
    logging.getLogger("netsentinel.probe").info("唯一消息-只应出现一次")
    _flush_own_file_handlers()
    content = log_path.read_text(encoding="utf-8")
    assert content.count("唯一消息-只应出现一次") == 1


def test_setup_logging_switches_file_path(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    path_a = tmp_path / "logs_a" / "a.log"
    path_b = tmp_path / "logs_b" / "b.log"
    setup_logging(Config(log_path=str(path_a)))
    logging.getLogger("netsentinel.probe").info("到 A")
    setup_logging(Config(log_path=str(path_b)))
    logging.getLogger("netsentinel.probe").info("到 B")
    _flush_own_file_handlers()

    file_handlers = [
        h for h in _own_handlers() if isinstance(h, logging.FileHandler)
    ]
    assert len(file_handlers) == 1  # 旧文件 handler 已被替换
    assert path_a.exists() and path_b.exists()
    assert "到 A" in path_a.read_text(encoding="utf-8")
    assert "到 B" in path_b.read_text(encoding="utf-8")
    assert "到 B" not in path_a.read_text(encoding="utf-8")


def test_setup_logging_verbose_flag_sets_level(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    cfg = Config(log_path=str(tmp_path / "logs" / "x.log"))
    setup_logging(cfg, verbose=False)
    assert logging.getLogger().level == logging.INFO
    setup_logging(cfg, verbose=True)
    assert logging.getLogger().level == logging.DEBUG


# ---------------------------------------------------------------------------
# V5 工程升级
# ---------------------------------------------------------------------------

def test_v5_setup_logging_thread_safe_and_counted(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    """V5 健壮性:多线程并发 setup_logging 不重复/不丢 handler,且计数准确。"""
    cfg = Config(log_path=str(tmp_path / "logs" / "x.log"))
    telemetry.reset()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            setup_logging(cfg)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常必须带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []  # 任何并发调用都不得抛出
        handlers = _own_handlers()
        assert len(handlers) == 2  # 恰好控制台 + 文件各一个
        assert sum(type(h) is logging.StreamHandler for h in handlers) == 1
        assert sum(isinstance(h, logging.FileHandler) for h in handlers) == 1
        assert telemetry.snapshot()["counters"]["log.setup"] == 8
        assert telemetry.snapshot()["counters"].get("log.setup.degraded", 0) == 0
    finally:
        telemetry.reset()


@pytest.mark.parametrize("failure_mode", ["parent_is_file", "log_path_is_dir"])
def test_v5_setup_logging_degrades_to_console_on_file_failure(
    tmp_path: pathlib.Path,
    clean_logging: None,
    caplog: pytest.LogCaptureFixture,
    failure_mode: str,
) -> None:
    """V5 健壮性:日志文件不可写时降级为仅控制台 + WARNING,绝不抛出。"""
    if failure_mode == "parent_is_file":
        blocker = tmp_path / "blocker"
        blocker.write_text("占位文件使子目录无法创建", encoding="utf-8")
        bad_log_path = blocker / "sub" / "x.log"  # 父路径是文件 → mkdir 失败
    else:
        bad_log_path = tmp_path / "as_dir"
        bad_log_path.mkdir()  # 日志路径本身是目录 → 打开失败

    cfg = Config(log_path=str(bad_log_path))
    telemetry.reset()
    try:
        with caplog.at_level(logging.WARNING, logger="netsentinel.logging_util"):
            setup_logging(cfg)  # 绝不抛出
        handlers = _own_handlers()
        assert sum(type(h) is logging.StreamHandler for h in handlers) == 1  # 仅控制台
        assert not any(isinstance(h, logging.FileHandler) for h in handlers)
        assert "降级" in caplog.text  # 有 WARNING 告警
        assert telemetry.snapshot()["counters"]["log.setup.degraded"] == 1
    finally:
        telemetry.reset()


def test_v5_setup_logging_recovers_after_degrade(
    tmp_path: pathlib.Path, clean_logging: None
) -> None:
    """V5 健壮性:降级后的下一次正常调用可恢复文件日志。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    setup_logging(Config(log_path=str(blocker / "sub" / "x.log")))
    good_path = tmp_path / "logs" / "good.log"
    setup_logging(Config(log_path=str(good_path)))
    handlers = _own_handlers()
    assert sum(isinstance(h, logging.FileHandler) for h in handlers) == 1
    logging.getLogger("netsentinel.probe").info("降级恢复检查")
    _flush_own_file_handlers()
    assert "降级恢复检查" in good_path.read_text(encoding="utf-8")


def test_v5_audit_logger_default_is_write_through(tmp_path: pathlib.Path) -> None:
    """V5 兼容锁定:默认 buffer_size=0 写透,每条事件无需 flush 即落盘。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p))
    audit.log_event("e1", n=1)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1
    audit.log_event("e2", n=2)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["event"] == "e2"
    audit.flush()  # 写透模式下 flush 为无操作,不产生多余输出
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_v5_audit_logger_buffered_batch_flush(tmp_path: pathlib.Path) -> None:
    """V5 性能:批量缓冲攒满一批一次性写出,flush/close 可手动收尾。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), buffer_size=3)
    audit.log_event("e1")
    audit.log_event("e2")
    assert not p.exists()  # 未满一批不落盘

    audit.log_event("e3")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # 攒满一批一次写出
    assert [json.loads(x)["event"] for x in lines] == ["e1", "e2", "e3"]

    audit.log_event("e4")
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3  # 剩余仍缓冲
    audit.flush()
    assert len(p.read_text(encoding="utf-8").splitlines()) == 4  # 手动 flush
    audit.log_event("e5")
    audit.close()
    assert len(p.read_text(encoding="utf-8").splitlines()) == 5  # close 同样收尾


def test_v5_audit_logger_context_manager_flushes(tmp_path: pathlib.Path) -> None:
    """V5 健壮性:with 语句保证退出时自动 flush 缓冲。"""
    p = tmp_path / "audit.jsonl"
    with JsonlAuditLogger(str(p), buffer_size=100) as audit:
        audit.log_event("only", ok=True)
        assert not p.exists()  # 仍在缓冲
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "only"
    assert rec["ok"] is True
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", rec["ts"])


def test_v5_audit_logger_negative_buffer_rejected(tmp_path: pathlib.Path) -> None:
    """V5 质量:非法 buffer_size 早失败(中文报错)。"""
    with pytest.raises(ValueError) as ei:
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), buffer_size=-1)
    assert "buffer_size" in str(ei.value)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(ei.value))


def test_v5_audit_event_telemetry_counter(tmp_path: pathlib.Path) -> None:
    """V5 可观测:每条审计事件计入 telemetry.inc("audit.event")。"""
    telemetry.reset()
    try:
        audit = JsonlAuditLogger(str(tmp_path / "a.jsonl"))
        audit.log_event("e1")
        audit.log_event("e2")
        buffered = JsonlAuditLogger(str(tmp_path / "b.jsonl"), buffer_size=10)
        buffered.log_event("e3")  # 缓冲未落盘也计数
        assert telemetry.snapshot()["counters"]["audit.event"] == 3
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# A190:Merkle 审计透明日志接线
# ---------------------------------------------------------------------------

_MERKLE_KEY = b"wiring-test-hmac-key-0123456789ab"


def _read_records(p: pathlib.Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _rebuild_tree(records: list[dict]) -> merkle.MerkleTree:
    """从落盘事件行(剔除 merkle_leaf 字段)独立重建 Merkle 树。"""
    tree = merkle.MerkleTree()
    for rec in records:
        tree.append({k: v for k, v in rec.items() if k != "merkle_leaf"})
    return tree


def test_merkle_off_by_default_keeps_legacy_lines(tmp_path: pathlib.Path) -> None:
    """兼容锁定:不注入 merkle_key 时行为与升级前一致(无 merkle_leaf 字段)。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p))
    assert audit.merkle_tree is None and audit.merkle_degraded is False
    audit.log_event("e1", n=1)
    audit.log_event("e2", n=2)
    recs = _read_records(p)
    assert len(recs) == 2
    assert all("merkle_leaf" not in r for r in recs)  # 零新字段
    assert all(r.get("event") != "merkle_checkpoint" for r in recs)  # 零 checkpoint 行


def test_merkle_leaf_field_matches_canonical_hash(tmp_path: pathlib.Path) -> None:
    """接线:注入密钥后每行带 merkle_leaf,且等于该行(除该字段外)规范哈希。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY)
    audit.log_event("human_confirmed", verdict="nsfw", note="人工确认")
    audit.log_event("submitted", case_id="C-01")
    recs = _read_records(p)
    assert len(recs) == 2
    for rec in recs:
        leaf = hashlib.sha256(
            merkle.canonical_bytes({k: v for k, v in rec.items() if k != "merkle_leaf"})
        ).hexdigest()
        assert rec["merkle_leaf"] == leaf
        assert audit.merkle_tree is not None
        assert rec["merkle_leaf"] in {
            audit.merkle_tree.leaf_hex(i) for i in range(audit.merkle_tree.leaf_count)
        }


def test_merkle_root_rebuilt_from_file_matches_tree_and_proves(tmp_path: pathlib.Path) -> None:
    """离线复核:从文件行序重建树,根与 logger 内树一致,每行包含性证明可验。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1000)
    for i in range(9):
        audit.log_event("scan", i=i)
    recs = _read_records(p)
    rebuilt = _rebuild_tree(recs)
    assert rebuilt.leaf_count == 9
    assert audit.merkle_tree is not None
    assert rebuilt.root_hex() == audit.merkle_tree.root_hex()
    root = rebuilt.root_hex()
    for i, rec in enumerate(recs):
        assert merkle.verify_proof(rec["merkle_leaf"], rebuilt.include_proof(i), root)


def test_merkle_checkpoint_every_n_events(tmp_path: pathlib.Path) -> None:
    """封根节奏:每 merkle_interval 条紧随一行 merkle_checkpoint,签名可验。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=3)
    telemetry.reset()
    try:
        for i in range(7):
            audit.log_event("e", i=i)
        recs = _read_records(p)
        assert len(recs) == 9  # 7 条事件 + 2 条 checkpoint(第 3 / 6 条后)
        cps = [r for r in recs if r["event"] == "merkle_checkpoint"]
        events = [r for r in recs if r["event"] == "e"]
        assert [cp["leaf_count"] for cp in cps] == [3, 6]
        assert [cp["height"] for cp in cps] == [2, 3]  # (n-1).bit_length()
        assert recs.index(cps[0]) == 3 and recs.index(cps[1]) == 7  # 紧随其后
        assert [r["i"] for r in events] == list(range(7))  # 事件顺序不乱
        for cp in cps:
            assert cp["signed"] is True
            assert merkle.verify_checkpoint_signature(
                cp["root"], cp["leaf_count"], cp["signature"], _MERKLE_KEY
            )
            # checkpoint 根与"文件里前 leaf_count 条事件"重建结果一致
            prefix = [r for r in recs if r["event"] != "merkle_checkpoint"][: cp["leaf_count"]]
            assert _rebuild_tree(prefix).root_hex() == cp["root"]
        assert telemetry.snapshot()["counters"]["audit.merkle_checkpoint"] == 2
    finally:
        telemetry.reset()


def test_merkle_tamper_detected_against_checkpoint(tmp_path: pathlib.Path) -> None:
    """篡改任一已封根事件 → 重建根与已签名 checkpoint 根不符,透明可检。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=4)
    for i in range(4):
        audit.log_event("evidence", url=f"http://x/{i}", score=0.1 * i)
    recs = _read_records(p)
    cp = next(r for r in recs if r["event"] == "merkle_checkpoint")

    tampered = [dict(r) for r in recs if r["event"] != "merkle_checkpoint"]
    tampered[2]["score"] = 0.99  # 事后篡改一条已封根事件
    assert _rebuild_tree(tampered).root_hex() != cp["root"]
    # 原样重建则一致,且签名只对原根成立
    assert (
        _rebuild_tree([r for r in recs if r["event"] != "merkle_checkpoint"]).root_hex()
        == cp["root"]
    )
    assert not merkle.verify_checkpoint_signature(
        _rebuild_tree(tampered).root_hex(), 4, cp["signature"], _MERKLE_KEY
    )


def test_merkle_degrades_safely_without_losing_audit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安全失效:Merkle 抛异常时审计本体照写、写一次性降级标记、后续停用树。"""
    p = tmp_path / "audit.jsonl"
    telemetry.reset()
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=2)
    assert audit.merkle_tree is not None
    monkeypatch.setattr(
        merkle.MerkleTree, "append", lambda self, event: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    try:
        audit.log_event("first", ok=True)  # 首条即触发降级
        audit.log_event("second", ok=True)  # 后续纯 JSONL,不再尝试入树
    finally:
        monkeypatch.undo()
        telemetry.reset()

    recs = _read_records(p)
    markers = [r for r in recs if r["event"] == "merkle_degraded"]
    events = [r for r in recs if r["event"] in ("first", "second")]
    assert len(events) == 2  # 审计本体一条不丢
    assert len(markers) == 1 and "boom" in markers[0]["reason"]  # 一次性标记
    assert recs.index(markers[0]) < recs.index(events[0])  # 标记先于首条无叶事件
    assert all("merkle_leaf" not in r for r in events)  # 失效后不加字段
    assert audit.merkle_degraded is True
    assert audit.merkle_tree is not None  # 树对象仍在(供事后取证),只是停用


def test_merkle_degraded_telemetry_counter(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """可观测:降级恰计一次 audit.merkle_degraded。"""
    p = tmp_path / "audit.jsonl"
    telemetry.reset()
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=5)
    assert audit.merkle_tree is not None
    monkeypatch.setattr(
        merkle.MerkleTree, "append", lambda self, event: (_ for _ in ()).throw(
            ValueError("坏事件")
        )
    )
    try:
        for i in range(3):
            audit.log_event("e", i=i)
        assert telemetry.snapshot()["counters"]["audit.event"] == 3
        assert telemetry.snapshot()["counters"]["audit.merkle_degraded"] == 1
    finally:
        monkeypatch.undo()
        telemetry.reset()


def test_merkle_checkpoint_failure_also_degrades_safely(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安全失效(封根路径):checkpoint 抛异常同样降级,事件本体不丢。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1)
    assert audit.merkle_tree is not None
    monkeypatch.setattr(
        merkle.MerkleTree, "checkpoint", lambda self: (_ for _ in ()).throw(
            MemoryError("封根失败")
        )
    )
    try:
        audit.log_event("e1", n=1)  # 第 1 条即到间隔 → 封根抛异常 → 降级
        audit.log_event("e2", n=2)  # 后续不再封根,也不再入树
    finally:
        monkeypatch.undo()
    recs = _read_records(p)
    events = [r for r in recs if r["event"] in ("e1", "e2")]
    assert len(events) == 2  # 审计本体不丢
    assert "merkle_leaf" in events[0]  # 触发事件的入树本身成功,叶字段保留
    assert "merkle_leaf" not in events[1]  # 降级后回归纯 JSONL
    assert not any(r["event"] == "merkle_checkpoint" for r in recs)
    markers = [r for r in recs if r["event"] == "merkle_degraded"]
    assert len(markers) == 1  # 一次性标记(在 e2 行之前)
    assert recs.index(markers[0]) < recs.index(events[1])
    assert audit.merkle_degraded is True


def test_merkle_buffered_mode_keeps_order_with_checkpoints(tmp_path: pathlib.Path) -> None:
    """缓冲模式:checkpoint 行与事件行同批落盘,顺序保持"事件在前,封根紧随"。"""
    p = tmp_path / "audit.jsonl"
    with JsonlAuditLogger(
        str(p), buffer_size=10, merkle_key=_MERKLE_KEY, merkle_interval=3
    ) as audit:
        for i in range(7):
            audit.log_event("e", i=i)
    recs = _read_records(p)
    assert len(recs) == 9
    assert [r["event"] for r in recs] == [
        "e", "e", "e", "merkle_checkpoint",
        "e", "e", "e", "merkle_checkpoint",
        "e",
    ]
    assert audit.merkle_tree is not None and audit.merkle_tree.leaf_count == 7


def test_merkle_concurrent_log_events_file_order_equals_leaf_order(
    tmp_path: pathlib.Path,
) -> None:
    """线程安全:并发 log_event 下,落盘行序 == Merkle 叶序(同一把锁保证)。"""
    p = tmp_path / "audit.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1000)
    threads_n, per_thread = 8, 10
    errors: list[BaseException] = []

    def worker(t: int) -> None:
        try:
            for k in range(per_thread):
                audit.log_event("concurrent", thread=t, k=k)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    recs = _read_records(p)
    events = [r for r in recs if r["event"] == "concurrent"]
    assert len(events) == threads_n * per_thread
    assert audit.merkle_tree is not None
    assert audit.merkle_tree.leaf_count == threads_n * per_thread
    # 按文件行序重建树 == logger 内树 → 落盘顺序与叶序完全一致
    rebuilt = _rebuild_tree(events)
    assert rebuilt.root_hex() == audit.merkle_tree.root_hex()
    root = rebuilt.root_hex()
    for i, rec in enumerate(events):  # 每行包含性证明可独立验证
        assert merkle.verify_proof(rec["merkle_leaf"], rebuilt.include_proof(i), root)


def test_merkle_constructor_validation(tmp_path: pathlib.Path) -> None:
    """质量:非法 merkle_interval / merkle_key 早失败(中文报错)。"""
    with pytest.raises(ValueError, match="merkle_interval"):
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), merkle_key=b"k", merkle_interval=0)
    with pytest.raises(ValueError, match="merkle_interval"):
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), merkle_key=b"k", merkle_interval=-3)
    with pytest.raises(TypeError, match="merkle_interval"):
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), merkle_key=b"k", merkle_interval="3")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="merkle_key"):
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), merkle_key=b"")
    with pytest.raises(ValueError, match="merkle_key"):
        JsonlAuditLogger(str(tmp_path / "a.jsonl"), merkle_key="not-bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A205:seal_now() 手动封根(高敏操作后立即锁定尾部盲区,A193 点名需求)
# ---------------------------------------------------------------------------


def test_seal_now_immediate_checkpoint_without_interval(tmp_path: pathlib.Path) -> None:
    """手动封根:不等 interval 计数立即产 checkpoint 行,签名与根均可离线复核。"""
    p = tmp_path / "seal.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1000)
    telemetry.reset()
    try:
        for i in range(3):
            audit.log_event("e", i=i)
        assert not any(
            r["event"] == "merkle_checkpoint" for r in _read_records(p)
        )  # interval 未到,不会自动封根

        ret = audit.seal_now()
        assert isinstance(ret, dict)
        assert ret["event"] == "merkle_checkpoint"
        assert ret["leaf_count"] == 3 and ret["height"] == 2
        assert ret["signed"] is True
        assert merkle.verify_checkpoint_signature(
            ret["root"], ret["leaf_count"], ret["signature"], _MERKLE_KEY
        )
        recs = _read_records(p)
        assert len(recs) == 4  # 3 事件 + 1 checkpoint
        assert recs[-1] == ret  # 落盘行内容与返回值一致
        assert _rebuild_tree(recs[:3]).root_hex() == ret["root"]  # 根覆盖全部 3 叶
        assert telemetry.snapshot()["counters"]["audit.merkle_checkpoint"] == 1

        # 封根不影响后续事件与再次封根(多次封根相互独立)
        audit.log_event("e", i=3)
        assert audit.merkle_tree is not None and audit.merkle_tree.leaf_count == 4
        ret2 = audit.seal_now()
        assert ret2 is not None and ret2["leaf_count"] == 4
        events4 = [
            r for r in _read_records(p) if r["event"] != "merkle_checkpoint"
        ]
        assert _rebuild_tree(events4).root_hex() == ret2["root"]  # 覆盖全部 4 叶
    finally:
        telemetry.reset()


def test_seal_now_returns_none_for_off_empty_degraded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None 口径:未启用 Merkle / 空树(0 叶)/ 降级态 → 不产任何 checkpoint。"""
    # 1) 未启用(未注入 merkle_key)
    off = JsonlAuditLogger(str(tmp_path / "off.jsonl"))
    off.log_event("e")
    assert off.seal_now() is None
    assert not any(
        r["event"] == "merkle_checkpoint" for r in _read_records(tmp_path / "off.jsonl")
    )

    # 2) 空树:0 叶返回 None,不落任何行(不产空 checkpoint)
    empty = JsonlAuditLogger(str(tmp_path / "empty.jsonl"), merkle_key=_MERKLE_KEY)
    assert empty.seal_now() is None
    assert not (tmp_path / "empty.jsonl").exists()

    # 3) 降级态:封根异常安全降级,seal_now 返回 None 且停用树
    telemetry.reset()
    degraded = JsonlAuditLogger(str(tmp_path / "deg.jsonl"), merkle_key=_MERKLE_KEY)
    degraded.log_event("e1")
    monkeypatch.setattr(
        merkle.MerkleTree, "checkpoint", lambda self: (_ for _ in ()).throw(
            MemoryError("封根失败")
        )
    )
    try:
        assert degraded.seal_now() is None
    finally:
        monkeypatch.undo()
        telemetry.reset()
    assert degraded.merkle_degraded is True
    assert degraded.seal_now() is None  # 降级后手动封根同样返回 None
    degraded.log_event("e2")  # 审计本体照常,且先写一次性降级标记行
    recs = _read_records(tmp_path / "deg.jsonl")
    assert [r["event"] for r in recs] == ["e1", "merkle_degraded", "e2"]
    assert not any(r["event"] == "merkle_checkpoint" for r in recs)


def test_seal_now_volume_replays_clean_in_ops_audit_verify(
    tmp_path: pathlib.Path,
) -> None:
    """整卷复核(只读 import 一轮):seal_now 产出的 checkpoint 可被 audit_verify 对账。"""
    from netsentinel.ops.audit_verify import verify_audit_log  # 只读 import,不写不装

    p = tmp_path / "verify.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1000)
    audit.log_event("human_confirmed", verdict="nsfw")
    audit.log_event("submitted", case_id="C-01")
    assert audit.seal_now() is not None  # 高敏操作后立即锁定尾部
    audit.log_event("batch_released", batch=1)  # 封根后卷尾继续增长(未封卷尾部)
    assert audit.seal_now() is not None  # 再次封根覆盖尾部盲区
    audit.close()

    report = verify_audit_log(str(p), key=_MERKLE_KEY)
    assert report["ok"] is True and report["failures"] == []
    assert report["total_lines"] == 5  # 3 事件行 + 2 checkpoint 行
    assert report["verified_events"] == 3
    assert report["checkpoints_total"] == 2
    assert report["checkpoints_passed"] == 2
    assert report["signatures_verified"] == 2
    assert report["unsealed_tail_leaves"] == 0  # 两次封根后卷尾为零


def test_seal_now_buffered_mode_flushes_events_before_checkpoint(
    tmp_path: pathlib.Path,
) -> None:
    """缓冲模式:seal_now 先落既有缓冲再写 checkpoint,行序保持"事件在前,封根紧随"。"""
    p = tmp_path / "buffered.jsonl"
    with JsonlAuditLogger(
        str(p), buffer_size=100, merkle_key=_MERKLE_KEY, merkle_interval=1000
    ) as audit:
        audit.log_event("e1", n=1)
        audit.log_event("e2", n=2)
        assert not p.exists()  # 仍在缓冲
        ret = audit.seal_now()
        assert ret is not None
        recs = _read_records(p)
        assert [r["event"] for r in recs] == ["e1", "e2", "merkle_checkpoint"]
        assert recs[-1] == ret
        audit.log_event("e3", n=3)
    # with 退出 flush:e3 落在 checkpoint 之后(封根之后的尾部盲区恢复增长)
    assert [r["event"] for r in _read_records(p)] == [
        "e1",
        "e2",
        "merkle_checkpoint",
        "e3",
    ]


def test_seal_now_concurrent_with_log_events_race_safe(tmp_path: pathlib.Path) -> None:
    """线程安全:并发 log_event 与 seal_now 同锁互斥,整卷重放零失败。"""
    from netsentinel.ops.audit_verify import verify_audit_log

    p = tmp_path / "race.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=_MERKLE_KEY, merkle_interval=1000)
    writers_n, per_writer, sealers_n, seals_each = 6, 20, 4, 5
    errors: list[BaseException] = []
    sealed: list[dict] = []

    def writer(t: int) -> None:
        try:
            for k in range(per_writer):
                audit.log_event("concurrent", thread=t, k=k)
        except BaseException as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)

    def sealer() -> None:
        try:
            for _ in range(seals_each):
                cp = audit.seal_now()
                if cp is not None:
                    sealed.append(cp)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(writers_n)]
    threads += [threading.Thread(target=sealer) for _ in range(sealers_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    events = [r for r in _read_records(p) if r["event"] == "concurrent"]
    assert len(events) == writers_n * per_writer
    assert audit.merkle_tree is not None
    assert audit.merkle_tree.leaf_count == writers_n * per_writer
    cp_lines = [r for r in _read_records(p) if r["event"] == "merkle_checkpoint"]
    assert len(cp_lines) == len(sealed)  # 每次成功封根恰一行,零丢失零重复

    # 最强口径:整卷重放对账(行序==叶序、每个 checkpoint 根/叶数/签名全对)
    report = verify_audit_log(str(p), key=_MERKLE_KEY)
    assert report["ok"] is True and report["failures"] == []
    assert report["checkpoints_total"] == len(sealed) == report["checkpoints_passed"]
