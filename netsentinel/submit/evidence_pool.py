# -*- coding: utf-8 -*-
"""举报证据截图池(V10.16):每站预留 10 张色情证据截图,按门户配额自动上传。

需求落点:

- **预留**:每个证据包(bundle)在提交前从 manifest 的 ensemble 评分中
  选出 **Top-10** 证据截图(nsfw_prob 降序,现存文件,不足时用整页截图
  补足),并把预留清单落 ``evidence_pool.json`` 留痕(幂等重算覆盖);
- **按门户配额自动上传**:12377 → **10 张**、扫黄打非(shdf)→ **5 张**
  (:data:`PORTAL_QUOTA`);上传由执行器在 **HUMAN_GATE 之前** 执行
  (``set_input_files`` 到 ``#report-file``),人工门随即可见核对附件、
  输入验证码并最终确认——**红线 1/2 零触碰**(上传是辅助动作,验证码
  与最终提交永远属于人工)。

V10.20 创新旗舰 · **证据池感知去重**:同一站点常出现"同图不同尺寸 /
裁切 / 重编码"的近重复截图,旧口径按 nsfw_prob 降序取 Top-10 会被
近重复挤占名额,举报门户收到的是 10 张里 6 张雷同的截图——证据信息量
骤减。现在选池前做 **pHash 近重复剔除**:

- 惰性导入 :mod:`netsentinel.intel.phash` 的 64bit 感知哈希,候选按
  ensemble 分**降序**逐张与已入选图比较汉明距离,
  ``≤ :data:`PHASH_DUP_DISTANCE`(6)`` 视为近重复 → 只留最高分那张,
  计数 ``dropped_near_duplicate``;腾出的名额继续吸纳后续**不重复**图
  (仍受 Top-10 与门户配额约束);
- **优雅降级三重奏**:Pillow 缺席(phash 模块内退化路径抛中文
  ValueError)/ 单张解码失败 / phash 模块本身缺席——该图一律视为
  "无哈希"照旧入池(不参与去重比较),整体保持旧口径,绝不抛错;
- 留痕:``evidence_pool.json`` 新增 ``dropped_near_duplicate`` 计数与
  ``dedup`` 开关;执行器 notes 提示"已按感知去重剔除 N 张近重复";
- 开关:``getattr(cfg, "evidence_pool_dedup", True)``——**默认开启**
  (质量创新型默认上线路径,仿 V10.16 自动上传先例);配置
  ``evidence_pool_dedup: false`` 一键回旧口径。

纯本地实现:只读证据包目录与 manifest.json,零网络;任何失败静默降级
(返回空清单,调用方记 notes),绝不阻断举报主流程。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "POOL_LIMIT",
    "PORTAL_QUOTA",
    "PHASH_DUP_DISTANCE",
    "bundle_dir_of",
    "files_for_portal",
    "pool_dropped_near_duplicate",
    "select_pool",
]

logger = logging.getLogger(__name__)

#: 每站预留的证据截图上限(需求给定 10 张)
POOL_LIMIT: int = 10

#: 门户 → 自动上传配额(12377 十张 / 扫黄打非五张;其余门户 0 = 不自动传)
PORTAL_QUOTA: dict[str, int] = {"12377": 10, "shdf": 5}

#: 感知去重阈值:两图 pHash 汉明距离 ≤ 该值视为近重复(只留 ensemble 分
#: 最高的一张)。6/64bit 是跨尺寸 / 轻裁切 / 重编码下的经验安全边界,
#: 既抓得住同源变体,又不误伤构图近似的异源图。
PHASH_DUP_DISTANCE: int = 6

#: 参与截图池的图片扩展名(小写)
_IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

#: 预留清单文件名(写在证据包目录内,与 zip 同级)
POOL_FILE_NAME: str = "evidence_pool.json"


def bundle_dir_of(evidence_zip: str | Path) -> Path:
    """证据包 zip → 同名目录(packager 的 dir_path 惯例:去 .zip 后缀)。"""
    p = Path(str(evidence_zip or ""))
    return p.parent / p.stem


def _load_phash_module() -> Any:
    """惰性导入 :mod:`netsentinel.intel.phash`;缺席/损坏返回 None(去重优雅关闭)。"""
    try:
        from netsentinel.intel import phash  # 惰性:不装 Pillow 也能用本模块

        return phash
    except Exception as exc:  # noqa: BLE001 - 兄弟模块缺席只降级去重,不阻断选池
        logger.debug("感知哈希模块不可用,证据池去重退回旧口径:%s", exc)
        return None


def _phash_of(path: Path, mod: Any, cache: dict[str, str | None]) -> str | None:
    """算单图 pHash(带缓存);Pillow 缺席 / 解码失败 / 非图片内容 → None。

    None 语义 = "该图无哈希":照旧入池、不参与近重复比较(旧口径),
    单张失败绝不拖累整池。
    """
    key = str(path)
    if key not in cache:
        try:
            cache[key] = mod.phash(key)
        except Exception as exc:  # noqa: BLE001 - 中文 ValueError 等一律降级
            logger.debug("证据图感知哈希计算失败(该图跳过去重):%s(%s)", key, exc)
            cache[key] = None
    return cache[key]


def _is_near_duplicate(digest: str, kept: list[str], mod: Any) -> bool:
    """digest 与已入选哈希集合逐一比较;距离 ≤ 阈值即近重复。

    比较异常(哈希长度不一等理论不可达路径)按"非近重复"兜底——
    去重是增强动作,误放行优于误剔除。
    """
    for held in kept:
        try:
            if mod.hamming(digest, held) <= PHASH_DUP_DISTANCE:
                return True
        except Exception:  # noqa: BLE001 - 比较失败按非近重复处理
            continue
    return False


def _existing_image(directory: Path, raw: str) -> Path | None:
    """把 manifest 里的图片路径解析为现存文件:目录内同名优先,原绝对路径兜底。"""
    if not raw:
        return None
    candidate = directory / Path(raw).name
    if candidate.is_file() and candidate.suffix.lower() in _IMAGE_EXTS:
        return candidate
    absolute = Path(raw)
    if absolute.is_file() and absolute.suffix.lower() in _IMAGE_EXTS:
        return absolute
    return None


def select_pool(
    evidence_zip: str | Path, *, limit: int = POOL_LIMIT, dedup: bool = True
) -> list[str]:
    """选出预留证据截图(ensemble 分降序 Top-N,感知去重,不足用整页截图补足)。

    - 读取 ``<bundle>/manifest.json`` 的 ``report.image_scores``(model ==
      "ensemble" 行,nsfw_prob 降序)与 ``report.pages[].screenshot_path``;
    - 只收**现存**图片文件,按"目录内同名 → 原绝对路径"解析,去重保序;
    - V10.20 感知去重(``dedup=True`` 缺省开):候选按分数降序逐张与已
      入选图比 pHash 汉明距离,``≤ :data:`PHASH_DUP_DISTANCE``` 视为近
      重复只留最高分,名额继续吸纳后续不重复图;Pillow 缺席 / 单张解码
      失败优雅跳过(该图按旧口径入池);``dedup=False`` 一键回旧口径;
    - 结果写入 ``<bundle>/evidence_pool.json`` 留痕(幂等;含
      ``dropped_near_duplicate`` 计数;失败只告警);
    - manifest 缺失/损坏/空 → 返回 [](调用方按"无可自动上传"降级)。
    """
    directory = bundle_dir_of(evidence_zip)
    manifest = directory / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        report = data.get("report") or {}
    except (OSError, ValueError, AttributeError) as exc:
        logger.debug("证据截图池读取 manifest 失败(按空池降级):%s(%s)", manifest, exc)
        return []

    phash_mod = _load_phash_module() if dedup else None
    picked: list[str] = []
    seen: set[Path] = set()
    picked_hashes: list[str] = []
    hash_cache: dict[str, str | None] = {}
    dropped_near_duplicate = 0

    def _take(raw: Any) -> None:
        nonlocal dropped_near_duplicate
        if len(picked) >= limit:
            return
        path = _existing_image(directory, str(raw or ""))
        if path is None or path in seen:
            return
        if phash_mod is not None:
            digest = _phash_of(path, phash_mod, hash_cache)
            if digest is not None:
                if _is_near_duplicate(digest, picked_hashes, phash_mod):
                    # 近重复:只留已入选的最高分,腾出名额给后续不重复图
                    dropped_near_duplicate += 1
                    return
                picked_hashes.append(digest)
        seen.add(path)
        picked.append(str(path))

    scores = report.get("image_scores") or []
    ranked = sorted(
        (s for s in scores if str(s.get("model", "")) == "ensemble"),
        key=lambda s: float(s.get("nsfw_prob", 0.0) or 0.0),
        reverse=True,
    )
    for score in ranked:
        _take(score.get("image"))
    for page in report.get("pages") or []:
        _take(page.get("screenshot_path"))

    if picked:
        try:
            pool_path = directory / POOL_FILE_NAME
            pool_path.write_text(
                json.dumps(
                    {
                        "reserved": picked,
                        "limit": limit,
                        "quota": PORTAL_QUOTA,
                        "dedup": bool(dedup),
                        "dropped_near_duplicate": dropped_near_duplicate,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # 预留留痕失败不影响上传
            logger.warning("证据截图池留痕写入失败(忽略):%s", exc)
    if dropped_near_duplicate:
        from netsentinel import telemetry  # 惰性:无去重命中零接入成本

        telemetry.inc("evidence_pool.near_duplicate_dropped", dropped_near_duplicate)
        logger.info(
            "证据池感知去重:剔除 %d 张近重复(阈值 %d),池内保留 %d 张",
            dropped_near_duplicate,
            PHASH_DUP_DISTANCE,
            len(picked),
        )
    return picked


def pool_dropped_near_duplicate(evidence_zip: str | Path) -> int:
    """读取最近一次 :func:`select_pool` 留痕的近重复剔除数(无留痕/损坏 → 0)。

    执行器据此在 notes 提示"已按感知去重剔除 N 张近重复";读不到
    (尚未选池 / 文件损坏 / 旧版留痕无此键)一律按 0,零异常。
    """
    try:
        data = json.loads(
            (bundle_dir_of(evidence_zip) / POOL_FILE_NAME).read_text(encoding="utf-8")
        )
        return int((data or {}).get("dropped_near_duplicate") or 0)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def files_for_portal(
    evidence_zip: str | Path, portal: Any, *, dedup: bool = True
) -> list[str]:
    """按门户配额取自动上传文件(12377 十张 / shdf 五张;未知门户空表)。

    ``dedup`` 透传 :func:`select_pool` 的感知去重开关;配额切片发生在
    **去重后的池**上——门户拿到的是不重复图的前 N 张。
    """
    quota = PORTAL_QUOTA.get(str(getattr(portal, "value", portal) or ""), 0)
    if quota <= 0:
        return []
    return select_pool(evidence_zip, dedup=dedup)[:quota]
