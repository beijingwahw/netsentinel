"""证据包构建(A11)。

把一次扫描得到的截图与图片证据复制到 ``evidence_dir/<safe_host>_<ts>/``,
生成 manifest.json(报告全文 + 文件清单含 sha256)与 summary.md(中文摘要,
含 Top 10 图片分值表),再把整个目录打成同名 zip,返回 :class:`EvidenceBundle`。

举报可追溯性保证:
- manifest.json 记录每个证据文件的相对路径 / sha256 / 字节数 / 角色
  (``screenshot`` 或 ``image``),与报告原文(:meth:`SiteReport.as_dict`)一同落盘;
- summary.md 附人工核实声明(辅助系统产物,提交前必须人工核实,红线);
- 缺失的源文件只记录 warning 并跳过,绝不中断打包;
- 同名证据文件落盘自动加序号(``shot.png`` → ``shot_1.png``),绝不覆盖已有证据
  (冲突检测基于已占用名集合,V5:零额外系统调用);
- 即使一个证据文件都没有,manifest 与 summary 照常生成,zip 照常打包,
  返回的 ``zip_path`` 始终非空。

V5 升级:复制与哈希**单遍流式合并**(打开源文件一次,分块"读→写→更新
sha256",大文件相比"先复制再整读目标哈希"省一次全量读);复制中途失败
(如磁盘满)清理半成品后告警跳过,绝不中断打包;关键入口接 telemetry
(timer ``packager.bundle`` / counter ``packager.files`` / ``packager.missing``)。

A205 产包签名策略接线(默认关闭,显式配置才签名):

- ``cfg.bundle_sign_algo == "ed25519"`` 且私钥 seed 可得时,zip 前对
  manifest 做 Ed25519 非对称签名(A192 策略模式,签名块含 algo /
  public_key / timestamp_proof,验签方无需共享密钥),时间证明签发器
  接 ``cfg.tsa_url``(默认 None 全离线);
- seed 来源优先级:环境变量 ``NETSENTINEL_ED25519_SEED_HEX`` >
  ``cfg.ed25519_seed_hex``(hex 解码须为 32 字节;非法/缺失只中文告警
  并**回退 HMAC-SHA256 默认策略**——红线:默认签名算法保持 hmac-sha256,
  升级 ed25519 需显式配置);
- 三键经 ``getattr(cfg, ..., 默认)`` 防御式读取,未收录字段的旧
  :class:`~netsentinel.contracts.Config` 实例零感知;**不配置(或 algo
  不是 ed25519)时完全不构造签名器、不新增任何读写,manifest 字节与
  升级前逐字节一致**(向后兼容红线);
- 签名器构造异常安全回退 HMAC 默认策略;签名任何失败回滚为未签名
  manifest 并告警跳过,绝不中断打包;Ed25519 私钥 seed 绝不落入日志
  或 manifest 明文(manifest 只含公钥);
- 可观测:ed25519 策略生效计 ``bundle.sign.ed25519``,ed25519 请求回退
  HMAC 计 ``bundle.sign.ed25519/hmac_fallback``,签名失败跳过计
  ``bundle.sign.skipped``。

用法::

    from netsentinel.evidence.packager import build_bundle
    bundle = build_bundle(report, cfg)      # → EvidenceBundle(zip_path=...)

仅使用标准库:json / zipfile / hashlib / shutil / stat / pathlib / urllib.parse / re / os。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat as _stat
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import (
    Config,
    EvidenceBundle,
    ImageScore,
    SiteReport,
    Verdict,
)
from netsentinel.security import timestamp
from netsentinel.security.bundle_sign import ALGO_ED25519, BundleSigner

logger = logging.getLogger(__name__)

__all__ = [
    "MANIFEST_NAME",
    "SUMMARY_NAME",
    "VERDICT_ZH",
    "DISCLAIMER",
    "CHUNK_BYTES",
    "ED25519_SEED_ENV_NAME",
    "build_bundle",
    "sign_bundle",
    "zip_bundle",
]

#: manifest 文件名(UTF-8 JSON)
MANIFEST_NAME = "manifest.json"

#: 中文摘要文件名
SUMMARY_NAME = "summary.md"

#: verdict → 中文文案(summary.md 展示用)
VERDICT_ZH: dict[Verdict, str] = {
    Verdict.CLEAN: "未发现",
    Verdict.SUSPECT: "疑似",
    Verdict.NSFW: "高置信",
}

#: 摘要必附的人工核实声明(红线:辅助系统产物必须人工核实)
DISCLAIMER = "本证据包由辅助系统自动生成,提交前须经人工核实。"

#: 流式复制 / 哈希的分块字节数(1 MiB)
CHUNK_BYTES = 1 << 20

#: 环境变量名:Ed25519 私钥 seed(hex)的最高优先级来源(覆盖 cfg.ed25519_seed_hex)
ED25519_SEED_ENV_NAME = "NETSENTINEL_ED25519_SEED_HEX"

#: Ed25519 私钥 seed 的字节长度(RFC 8032,与 security.ed25519.SEED_BYTES 一致)
ED25519_SEED_BYTES = 32

#: 目录名中不允许出现的主机名字符(字母数字、点、横线以外)→ "_"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9.-]")


def _safe_host(site_url: str) -> str:
    """提取站点主机名并替换文件系统非法字符;取不到主机名时返回 ``unknown``。"""
    host = urllib.parse.urlparse(site_url).hostname or ""
    cleaned = _UNSAFE_CHARS.sub("_", host).rstrip(".")
    return cleaned or "unknown"


def _unique_dest(bundle_dir: Path, name: str, taken: set[str]) -> Path:
    """返回不冲突的目标路径:同名时加序号,绝不覆盖已有文件。

    冲突检测基于 ``taken``(本次打包已占用的目标名集合)。证据目录由
    :func:`build_bundle` 独占新建且本函数是唯一写者,集合即权威,
    每次判定 O(1)、零磁盘系统调用(V5:替代逐候选 ``exists()`` 探测)。
    """
    if name not in taken:
        return bundle_dir / name
    stem = Path(name).stem
    suffix = Path(name).suffix
    seq = 1
    while True:
        candidate_name = f"{stem}_{seq}{suffix}"
        if candidate_name not in taken:
            return bundle_dir / candidate_name
        seq += 1


def _copy_evidence(
    src: str,
    bundle_dir: Path,
    role: str,
    entries: list[dict[str, Any]],
    seen: set[Path],
    taken: set[str],
) -> None:
    """单遍流式复制单个证据文件并登记清单,边复制边计算 sha256。

    打开源文件一次,按 :data:`CHUNK_BYTES` 分块"读 → 写 → 更新哈希"
    (V5:大文件省去复制后对目标的又一次全量读);复制完成后再
    :func:`shutil.copystat` 保留 mtime 等元数据(shutil.copy2 语义,
    失败仅 debug 日志、不影响证据内容)。源文件缺失 / 不可读、目标
    写入失败(如磁盘满)都只告警跳过并清理半成品,绝不中断打包。
    """
    if not src:  # 空路径 = 该页没有此类证据(如无浏览器时截图留空)
        return
    src_path = Path(src)
    try:
        src_stat = src_path.stat()
    except OSError:
        logger.warning("证据文件不存在或不可访问,已跳过:%s(角色=%s)", src, role)
        telemetry.inc("packager.missing")
        return
    if not _stat.S_ISREG(src_stat.st_mode):
        logger.warning("证据路径不是常规文件,已跳过:%s(角色=%s)", src, role)
        telemetry.inc("packager.missing")
        return
    resolved = src_path.resolve()
    if resolved in seen:  # 同一文件被多个页面引用时只保留一份
        logger.debug("证据文件重复引用,只保留一份:%s", src)
        return
    dest = _unique_dest(bundle_dir, src_path.name, taken)
    digest = hashlib.sha256()
    try:
        with src_path.open("rb") as src_fh, dest.open("wb") as dest_fh:
            for chunk in iter(lambda: src_fh.read(CHUNK_BYTES), b""):
                dest_fh.write(chunk)
                digest.update(chunk)
    except OSError as exc:
        logger.warning("证据文件复制失败,已跳过:%s → %s(%s)", src_path, dest, exc)
        dest.unlink(missing_ok=True)  # 清理半成品,避免损坏文件混入 zip/签名链
        return
    try:
        shutil.copystat(src_path, dest)
    except OSError as exc:  # 元数据是建议性的,失败不影响证据内容与哈希
        logger.debug("证据文件元数据复制失败(不影响内容):%s(%s)", src, exc)
    taken.add(dest.name)
    seen.add(resolved)
    entries.append(
        {
            "path": dest.name,
            "sha256": digest.hexdigest(),
            "bytes": src_stat.st_size,
            "role": role,
        }
    )
    logger.info("已收集证据:%s → %s(角色=%s)", src_path, dest, role)


def _top_image_scores(report: SiteReport, limit: int = 10) -> list[ImageScore]:
    """取 ensemble 模型评分,按 nsfw_prob 降序取前 N;没有 ensemble 时退回全量。"""
    ensemble = [s for s in report.image_scores if s.model == "ensemble"]
    pool = ensemble if ensemble else list(report.image_scores)
    return sorted(pool, key=lambda s: s.nsfw_prob, reverse=True)[:limit]


def _render_summary(
    report: SiteReport,
    entries: list[dict[str, Any]],
    generated_at: str,
) -> str:
    """渲染中文摘要 summary.md(判定结果、聚合分值、Top 10 图片分值表、声明)。"""
    verdict_zh = VERDICT_ZH.get(report.verdict, report.verdict.value)
    lines: list[str] = [
        "# 证据包摘要(净网哨兵 NetSentinel)",
        "",
        f"- 站点:{report.site_url}",
        f"- 判定结果:{verdict_zh}({report.verdict.value})",
        f"- 聚合分值(agg):{report.agg_nsw_prob:.4f}",
        f"- 达标图片数:{report.nsw_image_count}",
        f"- 抽样页面数:{len(report.pages)}",
        f"- 证据文件数:{len(entries)}",
        f"- 生成时间:{generated_at}",
        "",
        "## Top 10 图片分值表",
        "",
        "| 文件名 | nsfw_prob | 模型 |",
        "| --- | --- | --- |",
    ]
    top = _top_image_scores(report)
    if not top:
        lines.append("|(无图片评分记录)| - | - |")
    for score in top:
        name = Path(score.image.path).name
        lines.append(f"| {name} | {score.nsfw_prob:.4f} | {score.model} |")
    lines += ["", f"> 声明:{DISCLAIMER}", ""]
    return "\n".join(lines)


def zip_bundle(bundle_dir: Path, zip_path: Path) -> None:
    """把整个证据目录压缩为 zip(ZIP_DEFLATED;zip 内含顶层目录名)。

    A224(CONTRACTS-V13 §2 工程清理)起升为**公开口**:本包内部与
    兄弟模块(merge_bundles / parallel_pack 的 zip 刷新)一律经此公开名
    调用,消除跨模块私有名依赖;签名仍单源(本模块唯一实现)。
    V14(A235)收口:一代兼容别名 ``_zip_bundle`` 已移除,全部调用点
    均走公开名。
    """
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(bundle_dir.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(bundle_dir.parent).as_posix())


# ---------------------------------------------------------------------------
# A205 产包签名策略(BundleSigner 接线;默认关闭,显式配置才签名)
# ---------------------------------------------------------------------------


def _resolve_ed25519_seed(cfg: Config) -> bytes | None:
    """按 env > cfg 优先级解析 Ed25519 私钥 seed(hex → 32 字节 bytes)。

    - 环境变量 :data:`ED25519_SEED_ENV_NAME` 非空时优先(运维应急换钥
      无需改配置);否则读 ``cfg.ed25519_seed_hex``(经 getattr 防御式,
      字段未收录的旧 Config 零感知);
    - 两者都缺 / 非 str / 非法 hex / 解码后不是 32 字节 → 返回 ``None``
      (调用方据此回退 HMAC 默认策略),告警只报来源与长度,**绝不输出
      seed 内容本身**(红线:私钥不落日志明文)。
    """
    raw = (os.environ.get(ED25519_SEED_ENV_NAME) or "").strip()
    source = f"环境变量 {ED25519_SEED_ENV_NAME}"
    if not raw:
        value = getattr(cfg, "ed25519_seed_hex", None)
        raw = str(value).strip() if value is not None else ""
        source = "配置字段 ed25519_seed_hex"
    if not raw:
        return None
    try:
        seed = bytes.fromhex(raw)
    except ValueError:
        logger.warning(
            "Ed25519 私钥 seed(%s,长度 %d 字符)不是合法十六进制,"
            "产包签名将回退默认 HMAC-SHA256 策略",
            source,
            len(raw),
        )
        return None
    if len(seed) != ED25519_SEED_BYTES:
        logger.warning(
            "Ed25519 私钥 seed(%s)解码为 %d 字节,应为 %d 字节,"
            "产包签名将回退默认 HMAC-SHA256 策略",
            source,
            len(seed),
            ED25519_SEED_BYTES,
        )
        return None
    return seed


def _make_signer(cfg: Config) -> BundleSigner | None:
    """按配置构造产包签名器;未显式请求签名时返回 ``None``(现状不签名)。

    策略选择(A205,三键均经 getattr 防御式读取):

    - ``bundle_sign_algo`` 归一化后为 ``"ed25519"`` 且 seed 可得
      (env > cfg)→ Ed25519 签名器 + :class:`TimestampProver`
      (``tsa_url`` 来自 ``cfg.tsa_url``,默认 None 全离线),计
      ``bundle.sign.ed25519``;
    - 请求了 ed25519 但 seed 缺失 / 非法,或签名器构造抛异常 → 中文
      warning + 回退**默认 HMAC-SHA256 策略**(红线:默认签名算法保持
      hmac-sha256),计 ``bundle.sign.ed25519/hmac_fallback``;
    - 其他取值(缺省 ``"hmac-sha256"`` 等)→ 返回 ``None``,完全不签名
      (不构造签名器、不新增任何读写,manifest 与升级前逐字节一致)。
    """
    raw_algo = getattr(cfg, "bundle_sign_algo", None)
    algo = str(raw_algo).strip().lower() if raw_algo is not None else ""
    if algo != ALGO_ED25519:
        return None  # 未显式请求 ed25519:保持现状(不签名),零额外开销

    seed = _resolve_ed25519_seed(cfg)
    if seed is not None:
        try:
            tsa_url = getattr(cfg, "tsa_url", None)
            prover = timestamp.TimestampProver(
                counter_path=Path(cfg.data_dir) / timestamp.COUNTER_FILE_NAME,
                tsa_url=str(tsa_url) if tsa_url is not None else None,
            )
            signer = BundleSigner(
                data_dir=cfg.data_dir,
                algo=ALGO_ED25519,
                ed25519_seed=seed,
                timestamp_prover=prover,
            )
            telemetry.inc("bundle.sign.ed25519")
            logger.info(
                "产包签名策略:Ed25519(seed 来源=%s,tsa_url=%s)",
                "环境变量" if os.environ.get(ED25519_SEED_ENV_NAME, "").strip() else "配置",
                getattr(prover, "tsa_url", None) or "离线",
            )
            return signer
        except Exception as exc:  # noqa: BLE001 - 构造异常安全回退默认策略
            logger.warning(
                "Ed25519 产包签名器构造失败(%s),回退默认 HMAC-SHA256 策略", exc
            )
    else:
        logger.warning(
            "已配置产包签名算法 ed25519,但私钥 seed 不可得"
            "(环境变量 %s 与配置字段 ed25519_seed_hex 均未提供有效值),"
            "回退默认 HMAC-SHA256 策略",
            ED25519_SEED_ENV_NAME,
        )
    telemetry.inc("bundle.sign.ed25519/hmac_fallback")
    return BundleSigner(data_dir=cfg.data_dir)


def sign_bundle(bundle_dir: Path, manifest_path: Path, cfg: Config) -> None:
    """zip 前对 manifest 按配置策略签名;任何失败回滚并告警,绝不中断打包。

    签名写回 manifest 成功后 zip 内即为**已签名** manifest(目录与 zip
    口径一致);签名中途任何异常(密钥不可得 / 磁盘满等)先把 manifest
    字节回滚为未签名版本(绝不留下半损坏清单),再告警跳过并计
    ``bundle.sign.skipped``——证据包本体照常完成,绝不为签名而中断。

    A224(CONTRACTS-V13 §2 工程清理)起升为**公开口**:merge_bundles /
    parallel_pack 经此公开名复用**单一签名实现**(签名逻辑仍单源,
    公开化不复制)。V14(A235)收口:一代兼容别名 ``_sign_bundle``
    已移除,全部调用点均走公开名。
    """
    signer = _make_signer(cfg)
    if signer is None:
        return  # 现状:未显式请求签名,manifest 字节与升级前一致
    unsigned_bytes = manifest_path.read_bytes()
    try:
        signer.sign_manifest(str(bundle_dir))
        logger.info("产包已按策略签名:目录=%s", bundle_dir)
    except Exception as exc:  # noqa: BLE001 - 签名是增强项,失败绝不中断打包
        telemetry.inc("bundle.sign.skipped")
        try:
            manifest_path.write_bytes(unsigned_bytes)  # 回滚,不留半损坏清单
        except OSError:
            logger.warning("未签名 manifest 回滚失败:%s", manifest_path)
        logger.warning(
            "产包签名失败,已跳过(证据包照常完成,manifest 保持未签名):%s", exc
        )


def build_bundle(report: SiteReport, cfg: Config) -> EvidenceBundle:
    """构建举报证据包:证据目录 + manifest.json + summary.md + zip。

    流程:
    1. 目录 = ``Path(cfg.evidence_dir)/<safe_host>_<ts>``(ts 形如
       ``20261001_123456``;同一秒内重复打包时追加序号避免混入旧文件);
    2. 逐页复制 ``screenshot_path``(角色 screenshot)与 ``image_evidences``
       (角色 image),单遍流式"边复制边哈希",不存在的文件 warning 跳过,
       同名冲突加序号(集合判定);
    3. 写 manifest.json:``{"report": report.as_dict(), "files": [...]}``,
       每个文件含相对路径 / sha256 / bytes / role,UTF-8 编码,
       一次 write_text 成型(不反复改写);
    4. 写中文 summary.md(判定结果、agg 分值、达标图片数、页面数、生成时间、
       Top 10 ensemble 图片分值表、人工核实声明);
    5. (A205,可选)按 ``cfg.bundle_sign_algo`` 策略签名 manifest:
       仅显式配置 ``"ed25519"`` 且私钥 seed 可得(环境变量
       ``NETSENTINEL_ED25519_SEED_HEX`` > ``cfg.ed25519_seed_hex``)时走
       Ed25519 + 时间证明(``cfg.tsa_url``);否则回退默认 HMAC-SHA256;
       seed 缺失/非法或构造异常只中文告警回退;不配置该键则完全不签名
       (现状逐字节兼容);签名任何失败回滚告警,绝不中断打包;
    6. 整个目录压成同名 zip(ZIP_DEFLATED;zip 内即已签名 manifest)。

    可观测性(V5):整体耗时记 telemetry ``packager.bundle``;实际收集的
    证据文件数记 ``packager.files``;缺失 / 不可读源文件数记 ``packager.missing``。

    即使没有任何证据文件被复制,manifest 与 summary 仍会生成,zip 仍会打包,
    返回的 :class:`EvidenceBundle` 的 ``zip_path`` 始终非空。
    """
    with telemetry.timer("packager.bundle"):
        safe_host = _safe_host(report.site_url)
        evidence_root = Path(cfg.evidence_dir)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        bundle_dir = evidence_root / f"{safe_host}_{ts}"
        seq = 1
        while bundle_dir.exists():
            bundle_dir = evidence_root / f"{safe_host}_{ts}_{seq}"
            seq += 1
        bundle_dir.mkdir(parents=True, exist_ok=False)
        logger.info("创建证据目录:%s", bundle_dir)

        entries: list[dict[str, Any]] = []
        seen: set[Path] = set()
        taken: set[str] = set()  # 本次打包已占用的目标名(_unique_dest 冲突判定)
        for page in report.pages:
            _copy_evidence(
                page.screenshot_path, bundle_dir, "screenshot", entries, seen, taken
            )
            for evidence in page.image_evidences:
                _copy_evidence(
                    evidence.path, bundle_dir, "image", entries, seen, taken
                )

        manifest_path = bundle_dir / MANIFEST_NAME
        manifest = {"report": report.as_dict(), "files": entries}
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary_path = bundle_dir / SUMMARY_NAME
        summary_path.write_text(
            _render_summary(report, entries, generated_at), encoding="utf-8"
        )

        # A205:zip 前按配置策略签名(默认未配置 → 不签名,字节与升级前一致;
        # A224 起经公开口调用,签名实现单源)
        sign_bundle(bundle_dir, manifest_path, cfg)

        zip_path = bundle_dir.parent / f"{bundle_dir.name}.zip"
        zip_bundle(bundle_dir, zip_path)

        telemetry.inc("packager.files", len(entries))
        logger.info(
            "证据包构建完成:证据文件数=%d,目录=%s,zip=%s",
            len(entries),
            bundle_dir,
            zip_path,
        )
        return EvidenceBundle(
            site_url=report.site_url,
            dir_path=str(bundle_dir),
            manifest_path=str(manifest_path),
            zip_path=str(zip_path),
        )
