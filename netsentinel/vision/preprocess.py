"""反混淆预处理(V2 · A26,负责人补齐实现;V5 性能/可观测升级)。

违规站点常用小图(躲避尺寸过滤/人眼快速浏览)、九宫格拼图(单图混淆识别)
等手段规避审查。本模块在送入分类器**之前**派生增强变体:

- 宽或高 < 300 的图:放大 2 倍(提升小图可识别性);
- 宽高均 ≥ 600 的图:2x2 切块(把拼图还原成单图)。

变体文件名保留原始文件名关键词(如 ``nsfw_hi_1_up2x.png`` 仍含
``nsfw_hi``),保证 stub 离线桩规则与证据链路继续可用。全部依赖 Pillow
(惰性导入,缺失时返回空列表并提示安装,不阻断主流程)。

V5 升级要点:
- 源图 **Image.open 单次打开**,放大/切块全部分支复用同一次解码
  (多分支绝不重复解码同一张图);
- 变体**单遍落盘**:PNG 字节只编码一次,sha256 直接对内存字节计算,
  不再对每个变体做"写盘后整文件回读"(2x2 拼图省 4 次文件读);
- 遥测:每次派生 ``telemetry.inc("preprocess.variants", 变体数)``。

用法示例::

    from netsentinel.contracts import ImageEvidence
    from netsentinel.vision.preprocess import derive_variants

    variants = derive_variants(
        ImageEvidence(path="dl/a.png", url="http://x/a.png", source_page="http://x/"),
        out_dir="dl/variants",
    )
"""
from __future__ import annotations

import hashlib
import io
import logging
import types
from pathlib import Path

from netsentinel import telemetry
from netsentinel.contracts import ImageEvidence

__all__ = ["derive_variants"]

logger = logging.getLogger(__name__)

_SMALL_PX = 300     # 小图判定线:宽或高小于该值 → 放大
_GRID_PX = 600      # 拼图判定线:宽高均达到该值 → 2x2 切块
_UPSCALE = 2        # 放大倍数


def _load_pil():
    try:
        import importlib

        image = importlib.import_module("PIL.Image")
        pil_pkg = importlib.import_module("PIL")
    except Exception:  # None/缺失/半初始化的 PIL 一律按"未安装"处理
        return None
    return types.SimpleNamespace(Image=image, LANCZOS=image.LANCZOS, pil=pil_pkg)


def derive_variants(img: ImageEvidence, out_dir: str) -> list[ImageEvidence]:
    """为一张图片派生反混淆变体;不适派生/缺依赖/坏文件时返回空列表。

    源图只打开/解码一次,放大与切块分支复用同一解码结果;每个变体
    单遍编码为 PNG(内存字节计算 sha256 后一次写盘)。
    """
    src = Path(img.path)
    if not src.is_file():
        logger.warning("反混淆跳过:文件不存在 %s", img.path)
        return []

    pil = _load_pil()
    if pil is None:
        logger.info("未安装 Pillow,跳过反混淆变体派生(pip install Pillow 启用)")
        return []

    try:
        variants: list[ImageEvidence] = []
        # 单次打开:整个函数(含全部分支)只解码源图一次
        with pil.Image.open(src) as im:
            im.load()
            width, height = im.size
            stem = src.stem

            def _save_variant(piece, name: str, tag: str) -> None:
                out = Path(out_dir)
                out.mkdir(parents=True, exist_ok=True)
                # 单遍流式:PNG 只编码一次,哈希与落盘共用同一份字节
                # (旧实现写盘后整文件回读算哈希,2x2 拼图多读 4 次文件)。
                buf = io.BytesIO()
                piece.save(buf, format="PNG")
                data = buf.getvalue()
                target = out / name
                target.write_bytes(data)
                variants.append(
                    ImageEvidence(
                        path=str(target),
                        url=f"{img.url}#variant:{tag}",
                        source_page=img.source_page,
                        sha256=hashlib.sha256(data).hexdigest(),
                        width=piece.width,
                        height=piece.height,
                    )
                )

            if width < _SMALL_PX or height < _SMALL_PX:
                new_size = (width * _UPSCALE, height * _UPSCALE)
                upscaled = im.resize(new_size, pil.LANCZOS)
                _save_variant(upscaled, f"{stem}_up2x.png", "up2x")
            elif width >= _GRID_PX and height >= _GRID_PX:
                half_w, half_h = width // 2, height // 2
                for r in range(2):
                    for c in range(2):
                        box = (c * half_w, r * half_h,
                               (c + 1) * half_w if c == 0 else width,
                               (r + 1) * half_h if r == 0 else height)
                        piece = im.crop(box)
                        _save_variant(piece, f"{stem}_grid_r{r}c{c}.png",
                                      f"grid_r{r}c{c}")
            # else: 中间尺寸不适派生,variants 保持空列表
        telemetry.inc("preprocess.variants", len(variants))
        return variants
    except (OSError, ValueError) as exc:
        logger.warning("反混淆跳过:无法解码图片 %s(%s)", img.path, exc)
        return []
