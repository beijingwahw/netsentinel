"""Merkle 透明日志整卷离线审计命令(netsentinel.ops.audit_verify,A193)。

思想对标 sigstore/rekor 的 ``verify`` 离线审计形态:审计方拿到整卷
JSONL 审计日志后,**不依赖任何在线见证人 / gossip**,本地重放全部
``merkle_leaf`` 事件重建 Merkle 树,并与卷内每个 ``merkle_checkpoint``
对账(签名验证、根一致、叶数 / 树高一致),从而证明"这卷日志自我
一致、未被增删改序"。创新点在于**整卷复核**(rekor 只验证单条包含性
证明,本工具把整卷当作待证对象一次跑完)与**混合卷 / 降级段 / 未封卷
的取证化分级**:旧格式行不算失败、降级段单列、尾部未被 checkpoint
覆盖的"未封卷"如实标注但不算篡改——这是对 append-only 日志"前缀可
验证、尾部需封根"数学事实的诚实表达。

只读复用 :mod:`netsentinel.security.merkle`(MerkleTree / 验签函数),
本模块零第三方依赖、零网络、零写盘(除非 ``--json PATH``)。

输入格式(由 :class:`~netsentinel.logging_util.JsonlAuditLogger` 落盘):

- 普通事件行:含 ``event``/``ts`` 与 ``merkle_leaf``(该行除该字段外
  规范序列化的 SHA-256,hex);
- ``event=merkle_checkpoint`` 行:含 ``algo``/``root``/``leaf_count``/
  ``height``/``sign_algo``/``signature``/``signed``(树状态元数据,不入树);
- ``event=merkle_degraded`` 行:Merkle 安全失效的一次性标记,其后事件
  行不带 ``merkle_leaf``(纯 JSONL 降级段);若其后再次出现
  ``merkle_leaf`` 行,视为**新会话新树段**,重放树重新归零。

判定口径:

- **退出码 0**:整卷自洽(允许含 legacy 行、降级段、未封卷尾部);
- **退出码 2**:检出篡改或断链——事件字节与 ``merkle_leaf`` 不符、
  checkpoint 根 / 叶数 / 树高 / 签名不符、启用段内出现无叶事件行、
  非法 JSON / 非 UTF-8 字节 / 空行;
- **退出码 1**:输入错误——文件缺失 / 空文件 / 路径是目录 /
  ``--key-hex`` 非法。

用法示例::

    python -m netsentinel.ops.audit_verify data/audit.jsonl \
        --key-hex <hex> [--json [报告路径]]

    # 程序化调用
    from netsentinel.ops.audit_verify import verify_audit_log

    report = verify_audit_log("data/audit.jsonl", key=b"...")
    report["ok"]            # True = 整卷通过
    report["failures"]      # [{"line": 42, "reason": "..."}, ...](封顶 50 条)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
from typing import Any

from netsentinel.security.merkle import (
    HASH_ALGO,
    SIGN_ALGO,
    MerkleTree,
    verify_checkpoint_signature,
)

__all__ = [
    "EXIT_INPUT_ERROR",
    "EXIT_OK",
    "EXIT_TAMPER",
    "AuditVerifyError",
    "main",
    "verify_audit_log",
]

#: 整卷自洽(含 legacy / 降级段 / 未封卷尾部)
EXIT_OK = 0

#: 输入错误(文件缺失 / 空文件 / 密钥 hex 非法)
EXIT_INPUT_ERROR = 1

#: 检出篡改或断链
EXIT_TAMPER = 2

#: 三类 Merkle 元数据行的 event 名(与 logging_util 落盘口径一致)
EVENT_CHECKPOINT = "merkle_checkpoint"
EVENT_DEGRADED = "merkle_degraded"

#: 事件行内叶哈希字段名
LEAF_FIELD = "merkle_leaf"

#: 报告中逐条列出的断链/篡改明细上限(防超大卷刷屏;全量计数不封顶)
MAX_LISTED_FAILURES = 50

#: 报告中根哈希的展示前缀长度(完整根值走 --json)
_ROOT_PREVIEW_LEN = 12


class AuditVerifyError(ValueError):
    """输入错误(文件缺失 / 空文件 / 路径非法):对应退出码 1。"""


def _preview(hexstr: str) -> str:
    """根哈希展示前缀(完整值在 --json 报告里)。"""
    return f"{hexstr[:_ROOT_PREVIEW_LEN]}…" if len(hexstr) > _ROOT_PREVIEW_LEN else hexstr


def verify_audit_log(path: str | pathlib.Path, *, key: bytes | None = None) -> dict[str, Any]:
    """整卷校验器:重放全部事件叶并与每个 checkpoint 对账,返回报告字典。

    ``key`` 为 checkpoint 的 HMAC 验签密钥(可选):提供则逐个验签
    (不符即篡改);不提供则只做结构对账(根 / 叶数 / 树高),未验签
    checkpoint 计入 ``signatures_unverified`` 并在报告中如实标注。

    输入错误(文件缺失 / 空文件 / 路径是目录)抛 :class:`AuditVerifyError`;
    一切卷内问题(篡改 / 断链 / 非法行)不抛异常,全部落入报告的
    ``failures``(行号 + 中文原因,明细封顶 :data:`MAX_LISTED_FAILURES`,
    ``failures_total`` 为全量计数)。
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise AuditVerifyError(f"审计卷不存在:{p}")
    if p.is_dir():
        raise AuditVerifyError(f"审计卷路径是目录,应为 JSONL 文件:{p}")
    if p.stat().st_size == 0:
        raise AuditVerifyError(f"审计卷为空文件:{p}")

    # -- 重放状态 ----------------------------------------------------------
    tree = MerkleTree()  # 当前树段的重放树(降级后新会话出现首片叶时归零)
    merkle_started = False  # 是否已见过任何 Merkle 信号(叶/checkpoint/降级标记)
    degraded = False  # 是否处于降级段(标记行之后、下一树段首片叶之前)
    seg_start: int | None = None  # 当前降级段起始行(标记行号)
    seg_reason = ""  # 当前降级段原因(取自标记行的 reason 字段)
    last_cp_leaves = 0  # 最近一次**对账通过**的 checkpoint 叶数(当前树段口径)

    # -- 计数与报告 --------------------------------------------------------
    total_lines = 0
    verified_events = 0
    checkpoints_total = checkpoints_passed = 0
    signatures_verified = signatures_unverified = 0
    legacy_lines = 0
    degraded_markers = degraded_events = 0
    degraded_segments: list[dict[str, Any]] = []
    merkle_segments = 1  # 树段数(新会话重开一段;初始段从首片叶起算)
    failures: list[dict[str, Any]] = []
    failures_total = 0
    scan_truncated = False

    def add_failure(line: int, reason: str) -> None:
        """记一处断链/篡改(全量计数;明细列表封顶防刷屏)。"""
        nonlocal failures_total
        failures_total += 1
        if len(failures) < MAX_LISTED_FAILURES:
            failures.append({"line": line, "reason": reason})

    def close_degraded_segment(end_line: int) -> None:
        """收口当前降级段(EOF 或新树段首片叶出现时)。"""
        nonlocal seg_start, seg_reason
        if seg_start is not None:
            degraded_segments.append(
                {
                    "start_line": seg_start,
                    "end_line": end_line,
                    "reason": seg_reason or "",
                }
            )
        seg_start = None
        seg_reason = ""

    def check_checkpoint(rec: dict[str, Any], line: int) -> None:
        """对账单个 checkpoint 行:算法标识 / 根 / 叶数 / 树高 / 签名。"""
        nonlocal checkpoints_total, checkpoints_passed
        nonlocal signatures_verified, signatures_unverified, last_cp_leaves
        checkpoints_total += 1
        reasons: list[str] = []

        if rec.get("algo") != HASH_ALGO:
            reasons.append(f"算法标识不符(应为 {HASH_ALGO},实为 {rec.get('algo')!r})")

        root = rec.get("root")
        leaf_count = rec.get("leaf_count")
        height = rec.get("height")
        signature = rec.get("signature")
        signed = rec.get("signed")

        root_ok = isinstance(root, str) and root
        count_ok = isinstance(leaf_count, int) and not isinstance(leaf_count, bool)
        if not root_ok:
            reasons.append("root 字段缺失或非法")
        if not count_ok:
            reasons.append("leaf_count 字段缺失或非法(应为正整数)")
        if not isinstance(height, int) or isinstance(height, bool):
            reasons.append("height 字段缺失或非法(应为整数)")
            height = None

        if root_ok and count_ok and height is not None:
            replay_root = tree.root_hex()
            if replay_root != root:
                reasons.append(
                    f"根不一致(重放根 {_preview(replay_root)},记录根 {_preview(root)})"
                )
            if tree.leaf_count != leaf_count:
                reasons.append(
                    f"叶数不一致(重放 {tree.leaf_count} 片,记录 {leaf_count} 片)"
                )
            if tree.height != height:
                reasons.append(f"树高不一致(重放 {tree.height},记录 {height})")

        # -- 签名:signed=True 或携带非空 signature 即按"应签名"处理 --------
        if signed is True or (isinstance(signature, str) and signature):
            if not (isinstance(signature, str) and signature):
                reasons.append("声明已签名但缺少 signature 字段")
            elif rec.get("sign_algo") != SIGN_ALGO:
                reasons.append(
                    f"签名算法标识不符(应为 {SIGN_ALGO},实为 {rec.get('sign_algo')!r})"
                )
            elif not (root_ok and count_ok):
                pass  # 根/叶数非法已在上面记原因,验签无从谈起
            elif key is not None:
                if verify_checkpoint_signature(root, leaf_count, signature, key):
                    signatures_verified += 1
                else:
                    reasons.append("checkpoint 签名验证失败(密钥不符或签名被篡改)")
            else:
                signatures_unverified += 1  # 未提供密钥:无法验签,如实计数
        else:
            signatures_unverified += 1  # 未签名 checkpoint:仅结构对账

        if reasons:
            add_failure(line, "checkpoint 对账失败:" + ";".join(reasons))
        else:
            checkpoints_passed += 1
            last_cp_leaves = tree.leaf_count

    started = time.perf_counter()
    try:
        with p.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                total_lines = lineno
                line = raw.strip()
                if not line:
                    add_failure(lineno, "空行(合法审计卷不应有空行)")
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    add_failure(lineno, f"非法 JSON 行({exc.msg})")
                    continue
                if not isinstance(rec, dict):
                    add_failure(lineno, "非 JSON 对象行(应为事件对象)")
                    continue

                event = rec.get("event")

                # 1) checkpoint 行:树状态元数据,不入树,直接对账
                if event == EVENT_CHECKPOINT:
                    merkle_started = True
                    check_checkpoint(rec, lineno)
                    continue

                # 2) 降级标记行:开启(或续开)一段降级段
                if event == EVENT_DEGRADED:
                    merkle_started = True
                    if degraded and seg_start is not None:  # 连续标记:前段先收口
                        close_degraded_segment(lineno - 1)
                    degraded = True
                    degraded_markers += 1
                    seg_start, seg_reason = lineno, str(rec.get("reason") or "")
                    continue

                # 3) 带叶事件行:重放进树,并核对"内容 ↔ 叶哈希"一致
                if LEAF_FIELD in rec:
                    if degraded:
                        # 降级后的首片叶 = 新会话新树段:收口降级段、重放树归零
                        close_degraded_segment(lineno - 1)
                        degraded = False
                        tree = MerkleTree()
                        last_cp_leaves = 0
                        merkle_segments += 1
                    leaf_recorded = rec[LEAF_FIELD]
                    if not isinstance(leaf_recorded, str) or not leaf_recorded:
                        add_failure(lineno, "merkle_leaf 字段非法(应为 hex 字符串)")
                        continue
                    event_only = {k: v for k, v in rec.items() if k != LEAF_FIELD}
                    try:
                        replayed = tree.append(event_only)
                    except Exception as exc:  # noqa: BLE001 - 防御:奇异值不中断整卷
                        add_failure(lineno, f"事件重放失败({type(exc).__name__}: {exc})")
                        continue
                    merkle_started = True
                    if replayed != leaf_recorded:
                        add_failure(
                            lineno,
                            "merkle_leaf 与事件内容不符(事件字节疑被篡改:"
                            f"重放 {_preview(replayed)},记录 {_preview(leaf_recorded)})",
                        )
                        continue
                    verified_events += 1
                    continue

                # 4) 无 Merkle 字段的普通事件行:三岔口
                if degraded:
                    degraded_events += 1  # 降级段内的纯 JSONL 事件:不算失败
                elif not merkle_started:
                    legacy_lines += 1  # Merkle 启用前的旧格式行:跳过并计数
                else:
                    add_failure(
                        lineno,
                        "缺 merkle_leaf 字段(Merkle 启用段内出现无叶事件行,疑断链)",
                    )
    except UnicodeDecodeError:
        add_failure(
            total_lines + 1,
            "存在无法按 UTF-8 解码的字节(疑似字节级篡改),扫描提前终止",
        )
        scan_truncated = True

    if degraded:  # 降级段一直延伸到卷尾
        close_degraded_segment(total_lines)

    elapsed = time.perf_counter() - started
    unsealed = max(0, tree.leaf_count - last_cp_leaves) if merkle_started else 0
    sealed: bool | None
    if not merkle_started:
        sealed = None  # 纯旧格式卷:无 Merkle 数据,谈不上封卷
    else:
        sealed = unsealed == 0

    return {
        "file": str(p),
        "ok": failures_total == 0,
        "exit_code": EXIT_OK if failures_total == 0 else EXIT_TAMPER,
        "total_lines": total_lines,
        "verified_events": verified_events,
        "checkpoints_total": checkpoints_total,
        "checkpoints_passed": checkpoints_passed,
        "signatures_verified": signatures_verified,
        "signatures_unverified": signatures_unverified,
        "legacy_lines": legacy_lines,
        "degraded_markers": degraded_markers,
        "degraded_events": degraded_events,
        "degraded_segments": degraded_segments,
        "merkle_active": merkle_started,
        "merkle_segments": merkle_segments if merkle_started else 0,
        "sealed": sealed,
        "unsealed_tail_leaves": unsealed,
        "failures": failures,
        "failures_total": failures_total,
        "scan_truncated": scan_truncated,
        "elapsed_seconds": round(elapsed, 3),
    }


def _print_human_report(report: dict[str, Any]) -> None:
    """打印中文人读报告(总行数 / 通过数 / checkpoint / 断链 / 降级段 / 封卷)。"""
    bar = "=" * 60
    print(bar)
    print("NetSentinel 审计卷整卷校验报告(Merkle 透明日志离线复核)")
    print(bar)
    print(f"文件:{report['file']}")
    print(
        f"总行数 {report['total_lines']}:"
        f"已验证事件 {report['verified_events']} | "
        f"checkpoint 通过 {report['checkpoints_passed']}/{report['checkpoints_total']} | "
        f"旧格式行 {report['legacy_lines']} | "
        f"降级段事件 {report['degraded_events']}"
    )
    print(
        f"checkpoint 签名:已验签 {report['signatures_verified']},"
        f"未验签 {report['signatures_unverified']}(未提供密钥或 checkpoint 未签名)"
    )
    sealed = report["sealed"]
    if sealed is None:
        print("封卷状态:无 Merkle 数据(纯旧格式卷,仅清点,未做哈希校验)")
    elif sealed:
        print("封卷状态:已封卷(全部事件均被 checkpoint 覆盖)")
    else:
        print(
            f"封卷状态:未封卷(尾部 {report['unsealed_tail_leaves']} 条事件未被 "
            f"checkpoint 覆盖,不算篡改)"
        )
    segments = report["degraded_segments"]
    if segments:
        print(f"降级段:{len(segments)} 段")
        for seg in segments:
            print(
                f"  - 第 {seg['start_line']}-{seg['end_line']} 行"
                f"(原因:{seg['reason'] or '未记录'})"
            )
    else:
        print("降级段:无")
    if report["failures_total"] == 0:
        print("断链/篡改:无 —— 整卷校验通过")
    else:
        print(f"断链/篡改:检出 {report['failures_total']} 处")
        for fail in report["failures"]:
            print(f"  - 第 {fail['line']} 行:{fail['reason']}")
        omitted = report["failures_total"] - len(report["failures"])
        if omitted > 0:
            print(f"  ……另有 {omitted} 处省略(全量明细见 --json 报告)")
    if report["scan_truncated"]:
        print("注意:卷内存在无法解码的字节,扫描提前终止(见断链明细)")
    if report["merkle_segments"] > 1:
        print(f"树段:共 {report['merkle_segments']} 段(降级后新会话重开树)")
    print(f"耗时:{report['elapsed_seconds']:.3f}s")


def _parse_key_hex(text: str) -> bytes:
    """``--key-hex`` → 密钥字节;非法(空/奇长/非 hex)抛 :class:`AuditVerifyError`。"""
    s = text.strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    if not s or len(s) % 2 != 0:
        raise AuditVerifyError(f"--key-hex 非法(应为偶数长度的 hex 字符串):{text!r}")
    try:
        key = bytes.fromhex(s)
    except ValueError as exc:
        raise AuditVerifyError(f"--key-hex 非法(含非 hex 字符):{exc}") from exc
    if not key:
        raise AuditVerifyError("--key-hex 非法(解码后为空)")
    return key


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.ops.audit_verify",
        description=(
            "Merkle 透明日志整卷离线审计:重放全部事件叶并与每个 checkpoint "
            "对账(签名/根/叶数/树高),检出增删改序并输出中文报告"
        ),
    )
    parser.add_argument(
        "audit_path",
        help="审计 JSONL 卷路径(JsonlAuditLogger 落盘格式)",
    )
    parser.add_argument(
        "--key-hex",
        default=None,
        help="checkpoint HMAC-SHA256 验签密钥(hex;缺省则只做结构对账不验签)",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        default=None,
        metavar="报告路径",
        help=(
            "输出机器可读 JSON 报告:不带值打印到 stdout(替代人读报告);"
            "带路径则写入该文件,stdout 仍打印人读报告"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:``<jsonl> [--key-hex HEX] [--json [报告路径]]``。

    - 整卷自洽(允许 legacy 行 / 降级段 / 未封卷尾部)→ 打印中文报告,0;
    - 检出篡改或断链 → 报告逐行列出"行号 + 中文原因",2;
    - 输入错误(文件缺失 / 空文件 / 路径是目录 / --key-hex 非法)→ 1;
    - 缺审计卷路径由 argparse 报错(SystemExit 2)。
    """
    args = _build_parser().parse_args(argv)
    key: bytes | None = None
    if args.key_hex is not None:
        try:
            key = _parse_key_hex(args.key_hex)
        except AuditVerifyError as exc:
            print(f"输入错误:{exc}")
            return EXIT_INPUT_ERROR

    try:
        report = verify_audit_log(args.audit_path, key=key)
    except AuditVerifyError as exc:
        print(f"输入错误:{exc}")
        return EXIT_INPUT_ERROR

    if args.json == "-":  # 纯 JSON 到 stdout(可管道给 jq 等离线工具)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report["exit_code"]

    _print_human_report(report)
    if args.json:  # 带路径:另写 JSON 报告文件
        out = pathlib.Path(args.json)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"JSON 报告写入失败:{exc}")
            return EXIT_INPUT_ERROR
        print(f"JSON 报告已写入:{out}")
    return report["exit_code"]


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
