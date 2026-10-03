"""NetSentinel(净网哨兵)多站点证据包合并(evidence.merge_bundles,A109)。

V6 批量案件流水线的关键一环:同一个案件组(CaseGroup)下的多个站点各自
已有单站证据包(A11 packager 产物),而"一个组 = 一份合并证据包 = 一次
举报",本模块把这些子包**合并成一个组级证据包**:

- 输入 ``bundles`` 为鸭子列表(:class:`EvidenceBundleLike`,与
  :class:`~netsentinel.contracts.EvidenceBundle` 同构:``site_url`` /
  ``dir_path`` / ``manifest_path`` / ``zip_path``),逐个读取其
  ``manifest.json``(优先 ``manifest_path``,退回 ``dir_path/manifest.json``,
  BOM 容错);
- 子包 manifest 损坏(缺失 / IO 错误 / 非法 JSON / 非 JSON 对象)只告警
  跳过并计入 ``skipped``,绝不中断合并;全部损坏时仍产出空合并包
  (manifest / summary / zip 照常生成);
- 证据文件按 **sha256 全局去重**后复制:跨站重复引用的同一份证据只保留
  首份;去重判定以复制时**实算的 sha256** 为准(单遍流式"边复制边哈希",
  子包 manifest 里的旧哈希仅作参考,不参与判定);同名不同内容的文件
  自动加序号(``img.png`` → ``img_1.png``),绝不覆盖已有证据(冲突检测
  基于已占用名集合,零额外系统调用);
- manifest.json 记录 ``{"title", "site_urls", "sub_reports", "skipped",
  "files", "merged_at"}``:`sub_reports`` 与输入顺序逐一对齐(各子包
  report 摘要,损坏 / 缺失对应位置为 ``None``);``files`` 全量清单每项含
  相对路径 / sha256 / bytes / ``from_site``(来源站点 URL);
- summary.md 为中文摘要:标题、站点列表、各子报告一行表(站点 / 判定 /
  agg 分值 / 达标图片数)、Top 10 图片分值表(各子报告 ensemble 分
  **并集**降序,同一图片取最高分),并声明
  "本包由 N 个站点证据合并生成,提交前须经人工核实"(红线:辅助系统
  产物必须人工核实);
- 整个合并目录打成同名 zip(ZIP_DEFLATED,内含顶层目录名),返回
  :class:`~netsentinel.contracts.EvidenceBundle`(``site_url`` 为各站点
  URL 以 ``" | "`` 连接后截断到 200 字符)。

A214 合并产物签名接线(默认关闭,与 A205 单站包同一策略同一实现):

- ``cfg=None``(缺省,向后兼容)完全不签名,manifest 字节与升级前
  逐字节一致;传入 ``cfg`` 后经 **A205 ``packager.sign_bundle`` 单一
  实现**(A224 公开口;惰性只读 import,绝不复制签名逻辑)在 zip
  落盘前对合并
  manifest 签名——三键(``bundle_sign_algo`` / ``ed25519_seed_hex`` /
  ``tsa_url``)getattr 防御式读取,默认 ``hmac-sha256`` 配置 = 现状
  零签名行为,``ed25519`` 显式 opt-in;
- 签名失败回滚为未签名 manifest 并告警跳过,绝不中断合并(对齐
  A205 / packager 哲学);签名器不可导入(极端并行态)同样降级不中断。

健壮性(对齐 packager 哲学:证据处理绝不因单个坏文件中断):

- 子包证据文件缺失 / 不可读 / 非常规文件 / 路径越出子包目录(防篡改
  穿越)→ warning 跳过,不中断;
- 复制中途失败(如磁盘满)→ 清理半成品后告警跳过;
- 元数据(copystat)失败仅 debug 日志,不影响证据内容与哈希。

可观测性(仅名称与数字,不涉及站点内容):timer ``merge_bundles.merge``;
counter ``merge_bundles.files``(去重后证据文件数)、
``merge_bundles.skipped`` / ``merge_bundles.missing``(仅发生时计数)。

用法::

    from netsentinel.evidence.merge_bundles import merge_bundles
    merged = merge_bundles(sub_bundles, out_dir, title="某某专案组")
    merged = merge_bundles(sub_bundles, out_dir, cfg=cfg)  # A214:同策略签名

仅使用标准库:json / zipfile / hashlib / shutil / stat / pathlib /
datetime / typing。除签名步对 A205 ``packager.sign_bundle`` 的惰性
只读复用(cfg 传入且显式配置 ed25519 时才触发 import)外零兄弟模块
依赖(常量与 packager 同款自带,并行开发互不阻塞)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import stat as _stat
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from netsentinel import telemetry
from netsentinel.contracts import EvidenceBundle, now_iso

__all__ = [
    "MANIFEST_NAME",
    "SUMMARY_NAME",
    "VERDICT_ZH",
    "CHUNK_BYTES",
    "DEFAULT_TITLE_TEMPLATE",
    "DISCLAIMER_TEMPLATE",
    "SITE_URL_MAX",
    "EvidenceBundleLike",
    "merge_bundles",
]

logger = logging.getLogger(__name__)

#: 子包 / 合并包 manifest 文件名(UTF-8 JSON;与 A11 packager 约定一致)
MANIFEST_NAME = "manifest.json"

#: 中文摘要文件名(与 A11 packager 约定一致)
SUMMARY_NAME = "summary.md"

#: verdict 字符串 → 中文文案(summary.md 展示用;与 packager 同款映射)
VERDICT_ZH: dict[str, str] = {
    "clean": "未发现",
    "suspect": "疑似",
    "nsfw": "高置信",
}

#: 流式复制 / 哈希的分块字节数(1 MiB,与 packager 一致)
CHUNK_BYTES = 1 << 20

#: 缺省标题模板:n = 成功并入合并包的站点数(manifest 损坏被跳过的子包不计入)
DEFAULT_TITLE_TEMPLATE = "合并证据包({n} 个站点)"

#: 合并包必附的人工核实声明模板(红线:辅助系统产物必须人工核实)
DISCLAIMER_TEMPLATE = "本包由 {n} 个站点证据合并生成,提交前须经人工核实。"

#: 返回的 EvidenceBundle.site_url 上限(" | ".join 后截断)
SITE_URL_MAX = 200

#: 合并目录名前缀(完整名 merged_<YYYYmmdd_HHMMSS>[_seq])
_MERGED_PREFIX = "merged"


class EvidenceBundleLike(Protocol):
    """子包鸭子契约:与 :class:`~netsentinel.contracts.EvidenceBundle` 同构。

    本模块只读使用这四个属性;``zip_path`` 目前不参与合并(证据一律从
    ``dir_path`` 目录读取),保留在契约里是为了与既有 EvidenceBundle
    对象无缝互操作。
    """

    site_url: str
    dir_path: str
    manifest_path: str
    zip_path: str


# ---------------------------------------------------------------------------
# 鸭子读取辅助
# ---------------------------------------------------------------------------
def _attr_str(obj: Any, attr: str) -> str:
    """鸭子取字符串属性;缺失 / None → 空串(非字符串宽容 str() 归一)。"""
    value = getattr(obj, attr, "")
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _manifest_file(bundle: Any) -> Path | None:
    """定位子包 manifest.json:优先 manifest_path,退回 dir_path/manifest.json。"""
    manifest_path = _attr_str(bundle, "manifest_path")
    if manifest_path:
        return Path(manifest_path)
    dir_path = _attr_str(bundle, "dir_path")
    if dir_path:
        return Path(dir_path) / MANIFEST_NAME
    return None


def _read_manifest(bundle: Any) -> dict[str, Any] | None:
    """读取子包 manifest.json(UTF-8,BOM 容错);损坏 → warning + None。"""
    target = _manifest_file(bundle)
    if target is None:
        logger.warning(
            "子包既无 manifest_path 也无 dir_path,已跳过:%r", bundle
        )
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("子包 manifest 损坏,已跳过:%s(%s)", target, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("子包 manifest 不是 JSON 对象,已跳过:%s", target)
        return None
    return raw


# ---------------------------------------------------------------------------
# 目录 / 落盘命名
# ---------------------------------------------------------------------------
def _fresh_merged_dir(out_root: Path) -> Path:
    """在 out_root 下新建唯一的 ``merged_<ts>[_seq]`` 目录。

    时间戳秒级;同一秒内重复合并时追加序号避让(packager 同款策略),
    绝不混入上一份合并包的旧文件。``parents=True`` 自动创建多级父目录。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = out_root / f"{_MERGED_PREFIX}_{ts}"
    seq = 1
    while candidate.exists():
        candidate = out_root / f"{_MERGED_PREFIX}_{ts}_{seq}"
        seq += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _unique_dest(bundle_dir: Path, name: str, taken: set[str]) -> Path:
    """返回不冲突的目标路径:同名时加序号,绝不覆盖已有文件。

    冲突检测基于 ``taken``(本次合并已占用的目标名集合),每次判定 O(1)、
    零磁盘系统调用(packager V5 同款)。
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


# ---------------------------------------------------------------------------
# 证据复制:单遍流式"边复制边哈希" + 全局 sha256 去重
# ---------------------------------------------------------------------------
def _copy_dedup(
    item: Any,
    src_root: Path,
    bundle_dir: Path,
    from_site: str,
    entries: list[dict[str, Any]],
    taken: set[str],
    seen_shas: set[str],
) -> None:
    """复制单个子包证据文件并登记清单(全局 sha256 去重)。

    - 清单项不是对象 / 无 path → warning 跳过;
    - 相对路径越出子包目录(``../`` 逃逸或篡改成绝对路径)→ warning
      跳过并计 missing(防穿越:合并包绝不能悄悄吸入子包目录外的文件);
    - 源文件缺失 / 不可读 / 非常规文件 → warning 跳过并计 missing;
    - 打开源一次,按 :data:`CHUNK_BYTES` 分块"读 → 写 → 更新哈希";
      完成后以**实算 sha256** 判重:与已并入内容重复 → 删除刚写的副本、
      只保留首份(记录与去重均以实算值为准);
    - 写入失败(如磁盘满)→ 清理半成品后告警跳过,绝不中断合并;
    - 同名冲突经 :func:`_unique_dest` 加序号;``copystat`` 失败仅 debug。
    """
    if not isinstance(item, dict):
        logger.warning("子包文件清单项不是对象,已跳过:%r", item)
        return
    rel = str(item.get("path", "") or "")
    if not rel:
        logger.warning("子包文件清单项缺少 path,已跳过:%r", item)
        return
    root = src_root.resolve()
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root):
        logger.warning("子包证据路径越出子包目录,已跳过:%s", rel)
        telemetry.inc("merge_bundles.missing")
        return
    try:
        src_stat = resolved.stat()
    except OSError:
        logger.warning("子包证据文件不存在或不可访问,已跳过:%s", resolved)
        telemetry.inc("merge_bundles.missing")
        return
    if not _stat.S_ISREG(src_stat.st_mode):
        logger.warning("子包证据路径不是常规文件,已跳过:%s", resolved)
        telemetry.inc("merge_bundles.missing")
        return
    dest = _unique_dest(bundle_dir, Path(rel).name, taken)
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as src_fh, dest.open("wb") as dest_fh:
            for chunk in iter(lambda: src_fh.read(CHUNK_BYTES), b""):
                dest_fh.write(chunk)
                digest.update(chunk)
    except OSError as exc:
        logger.warning(
            "子包证据文件复制失败,已跳过:%s → %s(%s)", resolved, dest, exc
        )
        dest.unlink(missing_ok=True)  # 清理半成品,避免损坏文件混入合并包
        return
    sha = digest.hexdigest()
    if sha in seen_shas:  # 全局去重:同内容只保留首份
        dest.unlink(missing_ok=True)
        logger.debug("证据文件内容重复(sha256 相同),只保留首份:%s", resolved)
        return
    try:
        shutil.copystat(resolved, dest)
    except OSError as exc:  # 元数据是建议性的,失败不影响内容与哈希
        logger.debug("证据文件元数据复制失败(不影响内容):%s(%s)", resolved, exc)
    taken.add(dest.name)
    seen_shas.add(sha)
    entries.append(
        {
            "path": dest.name,
            "sha256": sha,
            "bytes": src_stat.st_size,
            "from_site": from_site,
        }
    )
    logger.info(
        "已并入证据:%s → %s(来自 %s)", resolved, dest, from_site or "(未知站点)"
    )


# ---------------------------------------------------------------------------
# summary.md 渲染
# ---------------------------------------------------------------------------
def _fmt_prob(value: Any) -> str:
    """宽容地把分值格式化为 4 位小数;取不到 → "-"。"""
    try:
        return f"{float(value):.4f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "-"


def _top_ensemble_scores(
    pairs: list[tuple[str, dict[str, Any] | None]],
    limit: int = 10,
) -> list[tuple[str, float, str]]:
    """各子报告 ensemble 评分**并集**,按 nsfw_prob 降序取前 N。

    只认 ``model == "ensemble"`` 的评分记录(与单站 summary 的 Top 表口径
    一致);同一图片(路径相同)在多个子报告里出现时取最高分。返回
    ``(图片路径, nsfw_prob, 来源站点)`` 三元组列表。
    """
    best: dict[str, tuple[float, str]] = {}
    for site, report in pairs:
        if not isinstance(report, dict):
            continue
        scores = report.get("image_scores")
        if not isinstance(scores, list):
            continue
        for score in scores:
            if not isinstance(score, dict) or score.get("model") != "ensemble":
                continue
            image = str(score.get("image", "") or "")
            if not image:
                continue
            try:
                prob = float(score.get("nsfw_prob", 0.0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                prob = 0.0
            current = best.get(image)
            if current is None or prob > current[0]:
                best[image] = (prob, site)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(image, prob, site) for image, (prob, site) in ranked[:limit]]


def _render_summary(
    title: str,
    site_urls: list[str],
    bundle_sites: list[str],
    sub_reports: list[dict[str, Any] | None],
    skipped: int,
    entries: list[dict[str, Any]],
    merged_at: str,
) -> str:
    """渲染中文摘要 summary.md(标题/站点列表/子报告一行表/Top10/声明)。"""
    lines: list[str] = [
        "# 合并证据包摘要(净网哨兵 NetSentinel)",
        "",
        f"- 标题:{title}",
        f"- 涉及站点数:{len(site_urls)}",
        "- 站点列表:",
    ]
    if site_urls:
        lines.extend(f"  - {u}" for u in site_urls)
    else:
        lines.append("  - (无:全部子包 manifest 损坏被跳过)")
    lines += [
        f"- 跳过子包数(manifest 损坏):{skipped}",
        f"- 证据文件数(sha256 去重后):{len(entries)}",
        f"- 合并时间:{merged_at}",
        "",
        "## 子报告一览",
        "",
        "| 站点 | 判定 | agg 分值 | 达标图片数 |",
        "| --- | --- | --- | --- |",
    ]
    for site, report in zip(bundle_sites, sub_reports):
        if not isinstance(report, dict):
            lines.append(
                f"| {site or '(未知站点)'} | (manifest 损坏或无报告) | - | - |"
            )
            continue
        verdict_raw = str(report.get("verdict", "") or "")
        verdict_zh = VERDICT_ZH.get(verdict_raw, verdict_raw or "-")
        site_col = str(report.get("site_url", "") or "") or site or "(未知站点)"
        lines.append(
            f"| {site_col} | {verdict_zh}({verdict_raw}) "
            f"| {_fmt_prob(report.get('agg_nsw_prob'))} "
            f"| {report.get('nsw_image_count', 0)} |"
        )
    lines += [
        "",
        "## Top 10 图片分值表(各子报告 ensemble 分并集,降序)",
        "",
        "| 文件名 | nsfw_prob | 来源站点 |",
        "| --- | --- | --- |",
    ]
    top = _top_ensemble_scores(list(zip(bundle_sites, sub_reports)))
    if not top:
        lines.append("|(无图片评分记录)| - | - |")
    for image, prob, from_site in top:
        lines.append(
            f"| {Path(image).name} | {prob:.4f} | {from_site or '(未知站点)'} |"
        )
    lines += ["", f"> 声明:{DISCLAIMER_TEMPLATE.format(n=len(site_urls))}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# zip 打包(packager 同款:DEFLATED,内含顶层目录名)
# ---------------------------------------------------------------------------
def _zip_merged_bundle(bundle_dir: Path, zip_path: Path) -> None:
    """把整个合并目录压缩为 zip(ZIP_DEFLATED;zip 内含顶层目录名)。

    本模块自带的压缩实现(A109 立项起与 packager 同款自带,保持
    "cfg=None 时零兄弟模块依赖"的并行开发口径;签名步才惰性复用
    packager 公开口)。V14(A235)起更名单独命名,避免与 packager
    已移除的一代私有别名 ``_zip_bundle`` 同名混淆。
    """
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(bundle_dir.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(bundle_dir.parent).as_posix())


# ---------------------------------------------------------------------------
# A214 合并产物签名(复用 A205 packager.sign_bundle 公开口;默认零签名)
# ---------------------------------------------------------------------------
def _sign_merged_bundle(bundle_dir: Path, manifest_path: Path, cfg: Any) -> None:
    """zip 落盘前按 cfg 策略对合并 manifest 签名(绝不复制签名逻辑)。

    - 签名实现**只读复用** A205 ``netsentinel.evidence.packager.sign_bundle``
      (A224 起的公开口;模块级函数惰性 import,来源注明;三键 getattr
      防御式读取、默认 hmac-sha256 = 现状零签名、ed25519 opt-in、失败回滚
      不中断——全部语义由该单一实现承载,本函数零签名逻辑);
    - 签名器不可导入(极端并行态)→ 中文告警 + ``merge_bundles.sign_skipped``
      计数,合并照常完成(增强项缺席绝不中断);
    - ``sign_bundle`` 自带失败回滚(未签名字节回写)与
      ``bundle.sign.skipped`` 计数;此处再兜一层防御,任何异常都不上抛。
    """
    try:
        from netsentinel.evidence.packager import (  # A205 单一实现,只读复用
            sign_bundle,
        )
    except Exception as exc:  # noqa: BLE001 - 签名器缺席按增强项降级
        telemetry.inc("merge_bundles.sign_skipped")
        logger.warning(
            "产包签名器 packager.sign_bundle 不可用,合并包保持未签名"
            "(合并流程不受影响):%s",
            exc,
        )
        return
    try:
        sign_bundle(bundle_dir, manifest_path, cfg)
    except Exception as exc:  # noqa: BLE001 - 兜底防御:签名绝不中断合并
        telemetry.inc("merge_bundles.sign_skipped")
        logger.warning(
            "合并包签名已跳过(证据包照常完成,manifest 保持未签名):%s", exc
        )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def merge_bundles(
    bundles: list[EvidenceBundleLike],
    out_dir: str | Path,
    *,
    title: str = "",
    cfg: Any | None = None,
) -> EvidenceBundle:
    """把多个单站证据包合并成一个组级证据包,返回 :class:`EvidenceBundle`。

    流程:

    1. ``out_dir`` 下新建唯一的 ``merged_<ts>[_seq]`` 目录(自动建父目录,
       同秒避让);
    2. 逐子包读 ``manifest.json``:损坏 → warning + 计 ``skipped``,
       该子包的文件与站点 URL 不并入(``sub_reports`` 对应位置记
       ``None`` 保持与输入顺序对齐);
    3. 逐文件复制:全局按**实算 sha256** 去重(同内容只留首份),同名
       不同内容加序号,缺失 / 越界 / 复制失败只告警跳过;
    4. 写 manifest.json:``{"title", "site_urls", "sub_reports", "skipped",
       "files", "merged_at"}``;``title`` 缺省为
       ``"合并证据包({n} 个站点)"``(n = 成功并入的站点数);
    5. 写中文 summary.md(标题 / 站点列表 / 子报告一行表 / Top 10
       ensemble 并集降序 / 人工核实声明);
    6. (A214,可选)``cfg`` 传入时 zip 落盘前经 :func:`_sign_merged_bundle`
       按同一签名策略签名(复用 A205 ``packager.sign_bundle`` 单一实现
       (A224 公开口):默认 hmac-sha256 配置 = 现状零签名行为,ed25519
       显式 opt-in,失败回滚不中断;``cfg=None`` 缺省完全不签名,字节
       与升级前一致);
    7. 整目录压成同名 zip(zip 内即已签名 manifest),返回
       :class:`~netsentinel.contracts.EvidenceBundle`(``site_url`` 为
       各站点 URL 以 ``" | "`` 连接后截断到 :data:`SITE_URL_MAX`)。

    可观测性:timer ``merge_bundles.merge``;counter
    ``merge_bundles.files``(去重后文件数)、``merge_bundles.skipped`` /
    ``merge_bundles.missing``(仅发生时计数);签名步复用 A205 的
    ``bundle.sign.*`` 计数(另有 ``merge_bundles.sign_skipped`` 记签名
    步缺席/兜底降级)。

    即使全部子包损坏、一个证据文件都没有,manifest 与 summary 照常生成,
    zip 照常打包,返回的 ``zip_path`` 始终非空(packager 哲学)。

    :param bundles: 子证据包列表(鸭子 :class:`EvidenceBundleLike`)。
    :param out_dir: 合并输出根目录(合并目录在其下新建)。
    :param title: 自定义标题;空白时用缺省模板。
    :param cfg: 可选全局配置;传入且显式配置 ed25519 时对合并包按
                A205 同一策略签名(三键 getattr 防御式读取),缺省
                None / 默认配置 = 现状零签名行为。
    :return: 合并后的 :class:`~netsentinel.contracts.EvidenceBundle`。
    :raises ValueError: ``bundles`` 为空列表时(中文消息)。
    """
    bundle_list = list(bundles or [])
    if not bundle_list:
        raise ValueError("bundles 不能为空:合并证据包至少需要一个输入证据包")
    out_root = Path(out_dir)
    with telemetry.timer("merge_bundles.merge"):
        bundle_dir = _fresh_merged_dir(out_root)
        logger.info(
            "创建合并证据目录:%s(输入子包 %d 个)", bundle_dir, len(bundle_list)
        )

        site_urls: list[str] = []          # 成功并入的站点(输入顺序)
        bundle_sites: list[str] = []       # 全部输入子包的站点(summary 行用)
        sub_reports: list[dict[str, Any] | None] = []  # 与输入逐一对齐
        skipped = 0
        entries: list[dict[str, Any]] = []
        taken: set[str] = set()            # 已占用落盘名(_unique_dest 判定)
        seen_shas: set[str] = set()        # 已并入内容(全局去重判定)

        for bundle in bundle_list:
            site_url = _attr_str(bundle, "site_url")
            bundle_sites.append(site_url)
            manifest = _read_manifest(bundle)
            if manifest is None:
                skipped += 1
                sub_reports.append(None)
                continue
            site_urls.append(site_url)
            report = manifest.get("report")
            sub_reports.append(report if isinstance(report, dict) else None)
            src_root = _attr_str(bundle, "dir_path")
            sub_files = manifest.get("files")
            if not src_root or not isinstance(sub_files, list):
                logger.debug(
                    "子包无文件清单或目录,跳过文件复制:%s", site_url or "(未知站点)"
                )
                continue
            for item in sub_files:
                _copy_dedup(
                    item,
                    Path(src_root),
                    bundle_dir,
                    site_url,
                    entries,
                    taken,
                    seen_shas,
                )

        if skipped:
            telemetry.inc("merge_bundles.skipped", skipped)

        n_sites = len(site_urls)
        final_title = title.strip() or DEFAULT_TITLE_TEMPLATE.format(n=n_sites)
        merged_at = now_iso()
        manifest_out = {
            "title": final_title,
            "site_urls": site_urls,
            "sub_reports": sub_reports,
            "skipped": skipped,
            "files": entries,
            "merged_at": merged_at,
        }
        manifest_path = bundle_dir / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest_out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        summary_path = bundle_dir / SUMMARY_NAME
        summary_path.write_text(
            _render_summary(
                final_title,
                site_urls,
                bundle_sites,
                sub_reports,
                skipped,
                entries,
                merged_at,
            ),
            encoding="utf-8",
        )

        # A214:zip 落盘前按 cfg 策略签名(复用 A205 packager.sign_bundle
        # 公开口,单一实现;cfg=None 或默认 hmac-sha256 配置 = 现状零签名,零字节差)
        if cfg is not None:
            _sign_merged_bundle(bundle_dir, manifest_path, cfg)

        zip_path = bundle_dir.parent / f"{bundle_dir.name}.zip"
        _zip_merged_bundle(bundle_dir, zip_path)

        telemetry.inc("merge_bundles.files", len(entries))
        logger.info(
            "合并证据包构建完成:站点 %d 个(跳过 %d 个),证据文件 %d 份,zip=%s",
            n_sites,
            skipped,
            len(entries),
            zip_path,
        )
        return EvidenceBundle(
            site_url=" | ".join(site_urls)[:SITE_URL_MAX],
            dir_path=str(bundle_dir),
            manifest_path=str(manifest_path),
            zip_path=str(zip_path),
        )
