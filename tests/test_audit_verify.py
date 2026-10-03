"""A193:netsentinel.ops.audit_verify 单元测试(离线,纯 stdlib,零网络)。

覆盖:合法卷整轮回放通过(精确计数)、三种退出码语义、五类篡改注入
(改事件字节 / 改叶哈希 / 删行 / 换行序 / 篡改 checkpoint 根或叶数 /
错密钥 / 插入自洽伪造行)全部检出且报出精确行号、旧格式与混合卷兼容、
降级段与新会话树段、未封卷尾部不算篡改、--json 机器可读报告、
10 万行合成卷性能上界(宽松防 CI 抖动)。

全部确定性(固定密钥 / 固定事件序列,零随机);只写 tmp_path。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

from netsentinel.logging_util import JsonlAuditLogger
from netsentinel.ops.audit_verify import (
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_TAMPER,
    main,
    verify_audit_log,
)
from netsentinel.security.merkle import canonical_bytes

#: 固定测试密钥(hex 注入 CLI;确定性,禁随机)
KEY_HEX = "2f" * 32
KEY = bytes.fromhex(KEY_HEX)

#: 另一把密钥(错密钥检出用)
OTHER_KEY = bytes.fromhex("11" * 32)

#: 仓库根(子进程 smoke 用 cwd,保证 netsentinel 可导入)
_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 构卷 / 篡改注入小工具
# ---------------------------------------------------------------------------
def _gen(
    path: pathlib.Path,
    n: int,
    *,
    interval: int = 128,
    key: bytes | None = KEY,
    event: str = "scan",
    start: int = 0,
) -> pathlib.Path:
    """用被测系统同源的 JsonlAuditLogger 生成 n 条事件的合法审计卷。"""
    audit = JsonlAuditLogger(str(path), merkle_key=key, merkle_interval=interval)
    for i in range(start, start + n):
        audit.log_event(event, seq=i, url=f"http://example.com/{i}", ok=i % 2 == 0)
    audit.close()
    return path


def _lines(path: pathlib.Path) -> list[str]:
    """读回全部物理行(不含结尾空行)。"""
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: pathlib.Path, lines: list[str]) -> None:
    """整卷重写(篡改注入后落盘)。"""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _find(lines: list[str], needle: str) -> int:
    """按子串定位唯一行,返回 1-based 行号(找不到或多处命中即断言失败)。"""
    hits = [i + 1 for i, text in enumerate(lines) if needle in text]
    assert len(hits) == 1, f"定位串 {needle!r} 应唯一命中,实际 {hits}"
    return hits[0]


def _rewrite(lines: list[str], lineno: int, fn) -> str:
    """对 1-based 第 lineno 行施加 fn(dict)->dict 篡改,返回新行文本。"""
    rec = json.loads(lines[lineno - 1])
    new = fn(rec)
    return json.dumps(new, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 合法卷整轮通过
# ---------------------------------------------------------------------------
def test_valid_volume_passes_with_exact_counters(tmp_path: pathlib.Path) -> None:
    """整轮回放:6 事件 / 间隔 3 → 2 个 checkpoint 全对账通过、已封卷、退出码 0。"""
    p = _gen(tmp_path / "audit.jsonl", 6, interval=3)
    report = verify_audit_log(p, key=KEY)

    assert report["ok"] is True
    assert report["exit_code"] == EXIT_OK
    assert report["total_lines"] == 8  # 6 事件行 + 2 checkpoint 行
    assert report["verified_events"] == 6
    assert report["checkpoints_total"] == 2
    assert report["checkpoints_passed"] == 2
    assert report["signatures_verified"] == 2
    assert report["signatures_unverified"] == 0
    assert report["legacy_lines"] == 0
    assert report["failures_total"] == 0
    assert report["sealed"] is True
    assert report["unsealed_tail_leaves"] == 0


def test_valid_volume_without_key_structural_pass(tmp_path: pathlib.Path) -> None:
    """不提供密钥:根/叶数/树高仍对账通过,签名计入"未验签"而非失败。"""
    p = _gen(tmp_path / "audit.jsonl", 6, interval=3)
    report = verify_audit_log(p, key=None)

    assert report["ok"] is True
    assert report["checkpoints_passed"] == 2
    assert report["signatures_verified"] == 0
    assert report["signatures_unverified"] == 2


# ---------------------------------------------------------------------------
# 篡改检测:全部检出且报出精确行号
# ---------------------------------------------------------------------------
def test_tampered_event_byte_detected_at_exact_line(tmp_path: pathlib.Path) -> None:
    """改任一事件字节(保留旧叶哈希):在该行精确检出,退出码 2。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=10)
    lines = _lines(p)
    lineno = _find(lines, '"seq": 5')  # 注:json.dumps 默认分隔符含空格
    lines[lineno - 1] = _rewrite(
        lines, lineno, lambda r: {**r, "verdict": "被篡改的裁定"}
    )
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == lineno
    assert "merkle_leaf 与事件内容不符" in report["failures"][0]["reason"]


def test_tampered_leaf_field_detected_at_exact_line(tmp_path: pathlib.Path) -> None:
    """篡改 merkle_leaf 字段本身:同行检出。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=10)
    lines = _lines(p)
    lineno = _find(lines, '"seq": 7')
    lines[lineno - 1] = _rewrite(
        lines, lineno, lambda r: {**r, "merkle_leaf": "00" * 32}
    )
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == lineno
    assert "merkle_leaf 与事件内容不符" in report["failures"][0]["reason"]


def test_deleted_line_detected_at_checkpoint(tmp_path: pathlib.Path) -> None:
    """删一行:下一 checkpoint 叶数不符,在该 checkpoint 行检出。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=10)
    lines = _lines(p)
    del lines[3]  # 删除第 4 行(事件 seq=3);checkpoint 行从 11 行移到 10 行
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == 10  # 对账点 = 移位后的 checkpoint 行
    assert "叶数不一致" in report["failures"][0]["reason"]


def test_swapped_line_order_detected_at_checkpoint(tmp_path: pathlib.Path) -> None:
    """换行序(交换两事件行):叶自洽但重放根漂移,checkpoint 根不符检出。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=10)
    lines = _lines(p)
    lines[2], lines[6] = lines[6], lines[2]  # 交换第 3 行与第 7 行(seq=2 ↔ seq=6)
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == 11  # checkpoint 行(第 11 行)
    assert "根不一致" in report["failures"][0]["reason"]


def test_tampered_checkpoint_root_detected(tmp_path: pathlib.Path) -> None:
    """篡改 checkpoint 根:根不一致 + 签名失效双重检出。"""
    p = _gen(tmp_path / "audit.jsonl", 5, interval=5)
    lines = _lines(p)
    lineno = _find(lines, "merkle_checkpoint")
    lines[lineno - 1] = _rewrite(
        lines, lineno, lambda r: {**r, "root": "ab" * 32}
    )
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == lineno
    reason = report["failures"][0]["reason"]
    assert "根不一致" in reason
    assert "签名验证失败" in reason


def test_tampered_checkpoint_leaf_count_detected(tmp_path: pathlib.Path) -> None:
    """篡改 checkpoint 叶数(5→4):叶数不一致 + 签名失效检出。"""
    p = _gen(tmp_path / "audit.jsonl", 5, interval=5)
    lines = _lines(p)
    lineno = _find(lines, "merkle_checkpoint")
    lines[lineno - 1] = _rewrite(lines, lineno, lambda r: {**r, "leaf_count": 4})
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == lineno
    reason = report["failures"][0]["reason"]
    assert "叶数不一致" in reason
    assert "签名验证失败" in reason


def test_tampered_checkpoint_height_detected(tmp_path: pathlib.Path) -> None:
    """篡改 checkpoint 树高:树高不一致检出。"""
    p = _gen(tmp_path / "audit.jsonl", 5, interval=5)
    lines = _lines(p)
    lineno = _find(lines, "merkle_checkpoint")
    lines[lineno - 1] = _rewrite(lines, lineno, lambda r: {**r, "height": 99})
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert "树高不一致" in report["failures"][0]["reason"]


def test_wrong_key_detected(tmp_path: pathlib.Path) -> None:
    """错密钥:结构对账通过但每个 checkpoint 签名验证失败。"""
    p = _gen(tmp_path / "audit.jsonl", 6, interval=3)
    report = verify_audit_log(p, key=OTHER_KEY)

    assert report["ok"] is False
    assert report["exit_code"] == EXIT_TAMPER
    assert report["checkpoints_passed"] == 0
    assert report["failures_total"] == 2  # 两个 checkpoint 各报一处
    assert all("签名验证失败" in f["reason"] for f in report["failures"])


def test_self_consistent_forged_line_detected(tmp_path: pathlib.Path) -> None:
    """插入自洽伪造行(叶哈希正确重算):checkpoint 叶数/根不符检出。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=10)
    lines = _lines(p)
    forged = {"event": "forged", "ts": "2026-10-03T00:00:00", "seq": 999}
    forged["merkle_leaf"] = hashlib.sha256(canonical_bytes(forged)).hexdigest()
    lines.insert(3, json.dumps(forged, ensure_ascii=False))  # 插在第 4 行
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == 12  # checkpoint 移位到第 12 行
    reason = report["failures"][0]["reason"]
    assert "叶数不一致" in reason  # 重放 11 片 vs 记录 10 片


def test_missing_leaf_field_in_active_region_is_break(tmp_path: pathlib.Path) -> None:
    """启用段内抹掉某行的 merkle_leaf:按断链检出(行号精确)。"""
    p = _gen(tmp_path / "audit.jsonl", 10, interval=100)  # 不封根,只看行级
    lines = _lines(p)
    lineno = _find(lines, '"seq": 4')
    lines[lineno - 1] = _rewrite(
        lines, lineno, lambda r: {k: v for k, v in r.items() if k != "merkle_leaf"}
    )
    _write_lines(p, lines)

    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == lineno
    assert "缺 merkle_leaf" in report["failures"][0]["reason"]


def test_invalid_json_line_and_bad_utf8_detected(tmp_path: pathlib.Path) -> None:
    """非法 JSON 行与字节级破坏:均按篡改检出(不抛异常)。"""
    p = _gen(tmp_path / "audit.jsonl", 4, interval=100)
    lines = _lines(p)
    lines[1] = "{这不是 JSON"
    _write_lines(p, lines)
    report = verify_audit_log(p, key=KEY)
    assert report["exit_code"] == EXIT_TAMPER
    assert report["failures"][0]["line"] == 2
    assert "非法 JSON" in report["failures"][0]["reason"]

    # 字节级破坏:直接写入非 UTF-8 字节,扫描提前终止但仍给出报告
    p2 = _gen(tmp_path / "bytes.jsonl", 4, interval=100)
    raw = p2.read_bytes()
    p2.write_bytes(raw[: raw.index(b'"seq"')] + b"\xff\xfe\"seq\": 0}\n")
    report2 = verify_audit_log(p2, key=KEY)
    assert report2["exit_code"] == EXIT_TAMPER
    assert report2["scan_truncated"] is True
    assert "UTF-8" in report2["failures"][-1]["reason"]


# ---------------------------------------------------------------------------
# 兼容:旧格式 / 混合卷 / 未封卷
# ---------------------------------------------------------------------------
def test_pure_legacy_volume_counts_without_failure(tmp_path: pathlib.Path) -> None:
    """纯旧格式卷(无 merkle 字段):全部计入 legacy,通过、无哈希校验。"""
    p = tmp_path / "legacy.jsonl"
    audit = JsonlAuditLogger(str(p))  # 不注入密钥 → 行为零 merkle 字段
    for i in range(5):
        audit.log_event("scan_started", seq=i)
    audit.close()

    report = verify_audit_log(p, key=KEY)
    assert report["ok"] is True
    assert report["exit_code"] == EXIT_OK
    assert report["legacy_lines"] == 5
    assert report["verified_events"] == 0
    assert report["checkpoints_total"] == 0
    assert report["sealed"] is None
    assert report["merkle_active"] is False


def test_mixed_volume_legacy_then_merkle(tmp_path: pathlib.Path) -> None:
    """混合卷(前旧后新):旧行计 legacy,新行正常重放对账,整卷通过。"""
    p = tmp_path / "mixed.jsonl"
    legacy = JsonlAuditLogger(str(p))  # 第一波:旧格式
    for i in range(3):
        legacy.log_event("old_scan", seq=i)
    legacy.close()
    _gen(p, 4, interval=2, start=100)  # 第二波:同卷追加 Merkle 段

    report = verify_audit_log(p, key=KEY)
    assert report["ok"] is True
    assert report["legacy_lines"] == 3
    assert report["verified_events"] == 4
    assert report["checkpoints_total"] == 2
    assert report["sealed"] is True


def test_unsealed_tail_and_truncation_not_tampering(tmp_path: pathlib.Path) -> None:
    """未封卷尾部 / 真实截断:如实标注"未封卷",不算篡改(退出码 0)。"""
    p = _gen(tmp_path / "tail.jsonl", 130, interval=128)
    lines = _lines(p)
    assert len(lines) == 131  # 128 事件 + checkpoint + 2 事件

    report = verify_audit_log(p, key=KEY)
    assert report["ok"] is True
    assert report["sealed"] is False
    assert report["unsealed_tail_leaves"] == 2  # 尾部 2 条未被 checkpoint 覆盖

    # 真实截断(卷在 checkpoint 后被截断):封卷状态翻转为"已封卷",仍通过
    _write_lines(p, lines[:129])
    report2 = verify_audit_log(p, key=KEY)
    assert report2["ok"] is True
    assert report2["sealed"] is True
    assert report2["total_lines"] == 129

    # 截断到 checkpoint 之前:整段未封,仍不算篡改
    _write_lines(p, lines[:100])
    report3 = verify_audit_log(p, key=KEY)
    assert report3["ok"] is True
    assert report3["checkpoints_total"] == 0
    assert report3["sealed"] is False
    assert report3["unsealed_tail_leaves"] == 100


# ---------------------------------------------------------------------------
# 降级段与新会话树段
# ---------------------------------------------------------------------------
def test_degraded_segment_recorded_without_failure(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """降级段:标记行起记录范围,段内事件不算失败也不算 legacy。"""
    from netsentinel.security import merkle

    p = tmp_path / "degraded.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=KEY, merkle_interval=100)
    for i in range(3):  # 前 3 条正常带叶
        audit.log_event("ok_event", seq=i)

    orig = merkle.MerkleTree.append
    state = {"calls": 0}

    def flaky(self, event):  # monkeypatch 形态:签名须与 MerkleTree.append 一致
        state["calls"] += 1
        if state["calls"] > 0:  # 补丁期内第一条即注入故障
            raise RuntimeError("注入的 Merkle 故障")
        return orig(self, event)  # pragma: no cover - 永不抵达

    monkeypatch.setattr(merkle.MerkleTree, "append", flaky)
    for i in range(3, 6):  # 第 4 条触发降级,后续纯 JSONL
        audit.log_event("degraded_event", seq=i)
    monkeypatch.undo()
    audit.close()

    lines = _lines(p)
    marker_line = _find(lines, "merkle_degraded")
    report = verify_audit_log(p, key=KEY)

    assert report["ok"] is True  # 降级是安全失效,不是篡改
    assert report["verified_events"] == 3
    assert report["degraded_markers"] == 1
    assert report["degraded_events"] == 3
    assert report["legacy_lines"] == 0
    assert report["degraded_segments"] == [
        {
            "start_line": marker_line,
            "end_line": len(lines),
            "reason": report["degraded_segments"][0]["reason"],
        }
    ]
    assert "注入的 Merkle 故障" in report["degraded_segments"][0]["reason"]


def test_new_session_after_degrade_starts_fresh_tree_segment(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """降级后新会话重开树:首片叶出现即收口降级段,checkpoint 叶数从头计。"""
    from netsentinel.security import merkle

    p = tmp_path / "two_sessions.jsonl"
    audit = JsonlAuditLogger(str(p), merkle_key=KEY, merkle_interval=100)
    audit.log_event("before", seq=0)
    monkeypatch.setattr(
        merkle.MerkleTree,
        "append",
        lambda self, event: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    audit.log_event("during", seq=1)  # 触发降级
    monkeypatch.undo()
    audit.close()

    _gen(p, 4, interval=2, event="after", start=10)  # 新会话,新树段

    report = verify_audit_log(p, key=KEY)
    assert report["ok"] is True
    assert report["merkle_segments"] == 2
    assert report["verified_events"] == 1 + 4
    assert report["checkpoints_total"] == 2  # 新树段每 2 条封根
    assert report["checkpoints_passed"] == 2
    assert report["degraded_events"] == 1
    assert report["degraded_segments"] and report["degraded_segments"][0][
        "end_line"
    ] == _find(_lines(p), '"seq": 10') - 1  # 段终于新会话首行(seq=10)之前


# ---------------------------------------------------------------------------
# CLI:退出码 / 人读报告 / --json 机器可读报告
# ---------------------------------------------------------------------------
def test_cli_exit_codes_and_human_report(
    tmp_path: pathlib.Path, capsys
) -> None:
    """CLI:通过 0 / 篡改 2 / 输入错误 1;人读报告含全部规定要素。"""
    p = _gen(tmp_path / "audit.jsonl", 6, interval=3)

    code = main([str(p), "--key-hex", KEY_HEX])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    for fragment in (
        "总行数 8",
        "已验证事件 6",
        "checkpoint 通过 2/2",
        "已验签 2",
        "已封卷",
        "断链/篡改:无",
        "降级段:无",
    ):
        assert fragment in out

    # 篡改后:退出码 2,报告含精确行号与中文原因
    lines = _lines(p)
    lines[2] = _rewrite(lines, 3, lambda r: {**r, "evil": True})
    _write_lines(p, lines)
    capsys.readouterr()
    code = main([str(p), "--key-hex", KEY_HEX])
    out = capsys.readouterr().out
    assert code == EXIT_TAMPER
    assert "第 3 行" in out
    assert "merkle_leaf 与事件内容不符" in out


def test_cli_input_errors_return_one(tmp_path: pathlib.Path, capsys) -> None:
    """输入错误 → 1:文件缺失 / 空文件 / 路径是目录 / --key-hex 非法。"""
    capsys.readouterr()
    assert main([str(tmp_path / "nope.jsonl")]) == EXIT_INPUT_ERROR
    assert "不存在" in capsys.readouterr().out

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main([str(empty)]) == EXIT_INPUT_ERROR
    assert "空文件" in capsys.readouterr().out

    assert main([str(tmp_path)]) == EXIT_INPUT_ERROR
    assert "目录" in capsys.readouterr().out

    p = _gen(tmp_path / "audit.jsonl", 2, interval=2)
    assert main([str(p), "--key-hex", "zz"]) == EXIT_INPUT_ERROR
    assert "--key-hex 非法" in capsys.readouterr().out
    assert main([str(p), "--key-hex", "0x1"]) == EXIT_INPUT_ERROR  # 奇数长度


def test_cli_json_to_stdout_replaces_human_report(
    tmp_path: pathlib.Path, capsys
) -> None:
    """--json(不带值):stdout 为纯机器可读 JSON(可被 json.loads 整体解析)。"""
    p = _gen(tmp_path / "audit.jsonl", 6, interval=3)
    code = main([str(p), "--key-hex", KEY_HEX, "--json"])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    report = json.loads(out)  # 纯 JSON,人读报告被替代
    assert report["ok"] is True
    assert report["checkpoints_total"] == 2
    assert report["verified_events"] == 6


def test_cli_json_written_to_path_keeps_human_report(
    tmp_path: pathlib.Path, capsys
) -> None:
    """--json 路径:报告文件写入机器可读 JSON,stdout 仍打印人读报告。"""
    p = _gen(tmp_path / "audit.jsonl", 3, interval=3)
    out_path = tmp_path / "reports" / "audit.json"
    code = main([str(p), "--key-hex", KEY_HEX, "--json", str(out_path)])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "NetSentinel 审计卷整卷校验报告" in out
    assert f"JSON 报告已写入:{out_path}" in out
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["ok"] is True and report["sealed"] is True


def test_cli_module_invocation_via_python_m(tmp_path: pathlib.Path) -> None:
    """文档形态 smoke:``python -m netsentinel.ops.audit_verify <jsonl>``。"""
    p = _gen(tmp_path / "audit.jsonl", 4, interval=2)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(  # noqa: PLW1510 - 退出码本身就是被测对象,不能 check=True
        [sys.executable, "-m", "netsentinel.ops.audit_verify", str(p), "--key-hex", KEY_HEX],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_ROOT),
        env=env,
        timeout=120,
    )
    assert proc.returncode == EXIT_OK, proc.stderr
    assert "整卷校验通过" in proc.stdout


# ---------------------------------------------------------------------------
# 性能:10 万行(含 ~781 checkpoints)整卷校验
# ---------------------------------------------------------------------------
def test_100k_lines_volume_verified_within_loose_bound(tmp_path: pathlib.Path) -> None:
    """性能:100 000 事件行 + 781 checkpoint 整卷校验 < 宽松上界(需求 10s)。"""
    p = tmp_path / "big.jsonl"
    audit = JsonlAuditLogger(
        str(p), merkle_key=KEY, merkle_interval=128, buffer_size=512
    )
    for i in range(100_000):
        audit.log_event("scan", seq=i, url=f"http://example.com/{i}", ok=i % 2 == 0)
    audit.close()

    t0 = time.perf_counter()
    report = verify_audit_log(p, key=KEY)
    elapsed = time.perf_counter() - t0

    assert report["ok"] is True, report["failures"][:3]
    assert report["total_lines"] == 100_000 + 781
    assert report["checkpoints_total"] == 781  # ⌊100000/128⌋
    assert report["verified_events"] == 100_000
    assert report["signatures_verified"] == 781
    assert report["unsealed_tail_leaves"] == 100_000 - 781 * 128  # 16 条未封尾
    # 需求 <10s;断言用 20s 宽松上界防 CI 抖动(实测见报告 elapsed_seconds)
    assert elapsed < 20.0, f"整卷校验耗时 {elapsed:.2f}s 超出宽松上界"
