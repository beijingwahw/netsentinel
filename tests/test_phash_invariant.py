# -*- coding: utf-8 -*-
"""A219 不变性指纹族验收测试(mirror / ring / pyramid,红队基准 A218 对标)。

**动机与验收基准**:红队基准 A218 实测(benchmarks/phash_redteam 的 9 攻击
族矩阵)点名既有指纹的三大盲区——镜像翻转(flip h/v)**全档零召回**、
中心裁剪(crop 0.1/0.2/0.3)**全档零召回**、旋转 3° 即破分块 256bit
(p256@32 = 0.02)。本文件以同一攻击函数(只读 import
``benchmarks.phash_redteam.apply_attack``,固定种子确定性变换)在 8 种
宽带构图语料上量化 A219 三哈希的修复效果(建议工作距
mirror 12/64、ring 6/32、pyramid 11/36):

========== ============ ==========================================
攻击族      A218 基线    A219 实测(8 宽带构图,seed=20261033)
========== ============ ==========================================
flip h/v   0.00         mirror **1.000**(距离恒 0,轨道严格不变)
rotate +3° p256@32=.02  ring 1.000(pyramid 辅助 1.000)
rotate -3° ~0           ring 1.000
rotate +8° 0.00         ring 0.750(pyramid 辅助 1.000)
rotate -8° 0.00         ring 0.625
rotate ±15°(超纲) 0    ring 0.188(边界,见用例)
crop 0.1   0.00         pyramid 0.250(**部分恢复**,诚实数字)
crop 0.2   0.00         pyramid 0.125
crop 0.3   0.00         pyramid 0.125
jpeg/resize 1.0         三哈希 1.000(最差距 m2 / r4 / p2)
恒等重存   1.0          三哈希距离恒 0
异图 FP    —            mirror 14 / pyramid 12 / ring 8(> 各自阈值)
========== ============ ==========================================

裁剪是**部分恢复**而非全档 1.0:结构主导构图(photo 族)zoom 后三层低频
三角均失配(实测距离 12~22 > 11),粗结构构图(quad)恢复最好(距 4~6)
——这是低频 DCT 指纹的原理性边界,完整方案需子窗口/兴趣点哈希(见
vision/phash2.py 的后续建议)。**构图口径说明**:纯条纹 / 棋盘 / 单频
径向环等稀疏频谱构图对一切低频 DCT 哈希是已知对抗类(粗层谱线稀疏、
跨图易撞),故语料采用与红队基准 syn2 同哲学的**宽带构图**(照片式 ×2 /
随机矩形 / 平滑云纹 / 块噪声 / 双频环 / 四象限斜坡 / 鞍形波纹),保证
结论对"降采样后仍保有强低频结构"的内容成立。

**A228 深化验收(dft_ring / tile,同一语料与攻击函数,seed 同源)**:
A219 交付报告点名两项原理性边界,本文件补两组对照实验——

========== ================ ============================ =====================
攻击族      A219 基线        A219 哈希实测                A228 新哈希实测
========== ================ ============================ =====================
rotate ±15° ring 0.188      dft_ring **1.000**(距 0~2)
rotate ±3°  ring 1.000      ——                           dft_ring 1.000
rotate ±8°  ring 0.688 合并 ——                           dft_ring 1.000
crop 0.1    pyramid 0.250   ——                           tile 0.375
crop 0.2    pyramid 0.125   ——                           tile **0.750**
crop 0.3    pyramid 0.125   ——                           tile **0.875**
jpeg/resize 三哈希 1.000     ——                           dft 1.000 / tile 1.000
                                            (良性最大距 dft 2 / tile 6)
恒等重存    三哈希距离 0     ——                           两新哈希距离恒 0
异图 FP     —               mirror 14/pyr 12/ring 8      dft 4(7 构图 21 对)
                                                           / tile 12(28 对)
========== ================ ============================ =====================

dft_ring 严格优于 ring 的机理由:DFT 模长对角向(循环)平移严格不变
(循环移位定理),而 DCT 能量签名在 ~1.3 桶平移(±15°)时泄漏过半。tile
位宽权衡的实测依据:8/15bit tile 与 0.25 边长梯级在 144 对任一命中语义下
异图最小距塌缩到 0(必撞),64bit tile × 12 个 + 工作距 8 是本语料下能
同时容纳"任一命中语义 + 裁剪召回"的最小配置(见 phash2.tile_hash
docstring)。

测试惯例:**stdlib 手写 PNG**(zlib + CRC,与 test_kernel_wire /
test_heuristic_kernel 同款,生成侧零三方依赖),Pillow 仅用于施加红队
攻击变换(importorskip);全确定性(构图解析公式 + 攻击种子
``variant_seed(20261033, ...)``),断言零墙钟,只写 tmp_path。
"""
from __future__ import annotations

import math
import shutil
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

PIL = pytest.importorskip("PIL", reason="需要 Pillow 才能施加红队攻击变换")
from PIL import Image

# 领地外只读引用:红队基准的攻击变换函数(不复制实现,口径与 A218 一致)
from benchmarks.phash_redteam import apply_attack, variant_seed
from netsentinel.intel.phash import PhashRegistry
from netsentinel.vision.phash2 import (
    DFT_RING_SUGGESTED_MAX_DISTANCE,
    MIRROR_SUGGESTED_MAX_DISTANCE,
    PYRAMID_SUGGESTED_MAX_DISTANCE,
    RING_SUGGESTED_MAX_DISTANCE,
    TILE_SUGGESTED_MAX_DISTANCE,
    dft_ring_hash,
    hamming_hex,
    mirror_hash,
    pyramid_distance,
    pyramid_hash,
    ring_hash,
    tile_any_match,
    tile_hash,
)

#: 攻击噪声种子(与红队基准 DEFAULT_SEED 同值,便于横向对照)。
SEED = 20261033

#: 合成构图边长(保证降采样到 32x32 后低频结构仍在)。
SIZE = 220

#: 8 种宽带构图(解析公式,零随机源;见模块 docstring 的构图口径说明)。
KINDS: tuple[str, ...] = (
    "photo", "photo2", "rects", "cloud",
    "blocks3", "rings_broad", "quad", "cross_wavy",
)

#: ring 误报检查用的"角向有信息"构图子集——rings_broad(双频径向环)是
#: 角向常量带(旋转等价类,ring_hash docstring 明示的退化类,实测与其余
#: 构图最小距 2),不参与 ring 的跨图误报断言,mirror/pyramid 不受影响。
ANGULAR_KINDS: tuple[str, ...] = tuple(k for k in KINDS if k != "rings_broad")


# ---------------------------------------------------------------------------
# stdlib 手写 PNG 写入器(灰度 color_type 0,滤波 0;与解码器互为逆运算)
# ---------------------------------------------------------------------------


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _write_png_gray(path: Path, rows: list[list[int]]) -> str:
    """把灰度矩阵落盘为 8bit 非隔行 PNG(stdlib zlib + CRC,零三方依赖)。"""
    height, width = len(rows), len(rows[0])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    return str(path)


def _cell_hash(ix: int, iy: int, salt: float = 0.0) -> float:
    """确定性伪随机 [0,1):经典 GLSL 风格 sin 哈希(注意 salt 不能取 2π
    整数倍——37.7 ≈ 6·2π,相位抵消会让"加盐"变"同图",已踩坑)。"""
    value = math.sin(12.9898 * ix + 78.233 * iy + salt)
    return value * value


def _synth_rows(kind: str, size: int = SIZE) -> list[list[int]]:
    """确定性宽带灰度构图(解析公式;降采样到 32/16/8 后低频结构非零)。"""
    rows: list[list[int]] = []
    for y in range(size):
        row: list[int] = []
        for x in range(size):
            u, v = x / size, y / size
            if kind == "photo":  # 照片式:双向渐变 + 圆/矩形几何形状
                val = 96 + 48 * math.sin(2 * math.pi * u + 0.7) \
                    + 32 * math.cos(2 * math.pi * v + 1.9)
                if (u - 0.35) ** 2 + (v - 0.45) ** 2 < 0.06:
                    val = 210.0
                elif 0.55 < u < 0.9 and 0.1 < v < 0.45:
                    val = 40.0
                elif (u - 0.72) ** 2 + (v - 0.75) ** 2 < 0.05:
                    val = 150.0
            elif kind == "photo2":  # 第二照片式:相位 / 几何均不同
                val = 105 + 40 * math.sin(2.6 * math.pi * v + 2.5) \
                    + 34 * math.cos(1.8 * math.pi * u + 0.6)
                if (u - 0.6) ** 2 + (v - 0.3) ** 2 < 0.05:
                    val = 30.0
                elif 0.1 < u < 0.4 and 0.55 < v < 0.9:
                    val = 205.0
            elif kind == "rects":  # 3x3 确定性伪随机矩形拼贴
                val = 255.0 * _cell_hash(int(u * 3), int(v * 3))
            elif kind == "cloud":  # 平滑云纹:双向正弦积 + 对角分量
                val = 110 + 60 * math.sin(2.2 * math.pi * u + 0.4) \
                    * math.cos(1.7 * math.pi * v + 1.1) \
                    + 25 * math.sin(4.1 * math.pi * (u + v))
            elif kind == "blocks3":  # 3x3 确定性块噪声(与 rects 异盐)
                val = 255.0 * _cell_hash(int(u * 3), int(v * 3), 2.3)
            elif kind == "rings_broad":  # 双频径向环(角向对称 → ring 退化类)
                r = math.hypot(u - 0.5, v - 0.5)
                val = 128 + 70 * math.sin(4 * math.pi * r) \
                    + 45 * math.sin(9 * math.pi * r + 1.0)
            elif kind == "quad":  # 四象限不同方向斜坡(中心十字硬边)
                if u < 0.5 and v < 0.5:
                    val = 60 + 150 * (u + v)
                elif u >= 0.5 and v < 0.5:
                    val = 210 - 150 * (u - 0.5)
                elif u < 0.5:
                    val = 60 + 150 * (1 - v)
                else:
                    val = 40 + 120 * (v - 0.5) + 60 * (u - 0.5)
            else:  # cross_wavy:鞍形 u·v + 对角波纹
                val = 70 + 120 * u * v + 45 * math.sin(3 * math.pi * (u - v) + 0.8)
            row.append(max(0, min(255, int(val))))
        rows.append(row)
    return rows


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: Any) -> dict[str, Any]:
    """落盘 8 种构图 + 各哈希基线(module 级复用,纯确定性)。"""
    out = tmp_path_factory.mktemp("invariant_corpus")
    paths = {
        kind: _write_png_gray(out / f"{kind}.png", _synth_rows(kind))
        for kind in KINDS
    }
    return {
        "dir": out,
        "paths": paths,
        "mirror": {k: mirror_hash(p) for k, p in paths.items()},
        "ring": {k: ring_hash(p) for k, p in paths.items()},
        "pyramid": {k: pyramid_hash(p) for k, p in paths.items()},
        "dft_ring": {k: dft_ring_hash(p) for k, p in paths.items()},
        "tile": {k: tile_hash(p) for k, p in paths.items()},
    }


def _attacked(corpus: dict[str, Any], kind: str, family: str, param: str) -> str:
    """对构图 kind 施加红队攻击(固定种子)后落盘 PNG,返回路径。

    攻击种子用 ``variant_seed(SEED, 图序, family, param)``——与红队基准
    完全同源(同参数同输出字节),召回口径可直接对照 A218。
    """
    src = corpus["paths"][kind]
    with Image.open(src) as im:
        im.load()
        gray = im.convert("L")
    attacked = apply_attack(
        gray, family, param, seed=variant_seed(SEED, KINDS.index(kind), family, param)
    )
    out = Path(corpus["dir"]) / f"{kind}__{family}_{param}.png"
    attacked.save(out, format="PNG", compress_level=1)
    return str(out)


def _recall(distances: list[int], threshold: int) -> float:
    return sum(1 for d in distances if d <= threshold) / len(distances)


# ---------------------------------------------------------------------------
# 验收一:mirror_hash 对镜像翻转严格不变(A218 基线 0.0 → 1.0)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param", ["h", "v"])
def test_mirror_hash_flip_recall_is_one(corpus: dict[str, Any], param: str) -> None:
    """镜像翻转(h/v × 8 构图)的 mirror 距离**恒为 0** → 召回 = 1.0。

    A218 基线:flip 族对既有 64/256bit 指纹全档零召回(DCT 奇次频率系数
    符号翻转,约半数位翻转)。修复机理:翻转不改变 Klein 群轨道,规范形
    (轨道哈希字典序最小)在轨道上逐位恒定——不是"距离小",是恒等。
    """
    distances = [
        hamming_hex(mirror_hash(_attacked(corpus, kind, "flip", param)),
                    corpus["mirror"][kind])
        for kind in KINDS
    ]
    assert distances == [0] * len(KINDS), f"flip/{param} 距离应全 0,实测 {distances}"


def test_mirror_hash_canonical_form_is_orbit_minimum(corpus: dict[str, Any]) -> None:
    """规范形语义:mirror(翻转版) == mirror(原图) == 轨道最小值
    (等长小写 hex 字典序 = 整数值序),逐构图断言。"""
    for kind in KINDS:
        canonical = corpus["mirror"][kind]
        assert canonical == min(
            mirror_hash(_attacked(corpus, kind, "flip", "h")),
            mirror_hash(_attacked(corpus, kind, "flip", "v")),
            canonical,
        )


# ---------------------------------------------------------------------------
# 验收二:ring_hash 对小角度旋转的召回(A218 基线 p256@32 rotate3°=0.02)
# ---------------------------------------------------------------------------


def test_ring_hash_rotation_recall_gate(corpus: dict[str, Any]) -> None:
    """ring 对 ±3°/±8° 旋转召回 ≥ 0.75(实测 27/32 = 0.844)。

    逐档实测(建议工作距 6/32bit):+3°→1.000、-3°→1.000、+8°→0.750、
    -8°→0.625;合并 0.844 ≥ 验收线 0.6(A218:p256@32 在 3° 即 0.02)。
    断言留 0.06+ 余量,防重采样实现的版本级微差。
    """
    per_param: dict[str, float] = {}
    for param in ("3", "-3", "8", "-8"):
        distances = [
            hamming_hex(ring_hash(_attacked(corpus, kind, "rotate", param)),
                        corpus["ring"][kind])
            for kind in KINDS
        ]
        per_param[param] = _recall(distances, RING_SUGGESTED_MAX_DISTANCE)
    combined = sum(per_param.values()) / len(per_param)
    assert combined >= 0.75, f"±3°/±8° 合并召回 {combined:.3f} < 0.75(逐档 {per_param})"
    assert min(per_param.values()) >= 0.5, f"最弱档 {per_param} 低于 0.5"


def test_ring_hash_large_rotation_partial(corpus: dict[str, Any]) -> None:
    """±15°(超验收口径)诚实量化:合并召回 ≥ 0.125(实测 0.188)。

    ~1.3 桶的角向平移超出 DCT 能量签名的近似不变区,这是 ring 的设计
    边界(任务只验收 ±3°/±8°),锁定边界数字防无声退化。
    """
    per_param = {}
    for param in ("15", "-15"):
        distances = [
            hamming_hex(ring_hash(_attacked(corpus, kind, "rotate", param)),
                        corpus["ring"][kind])
            for kind in KINDS
        ]
        per_param[param] = _recall(distances, RING_SUGGESTED_MAX_DISTANCE)
    combined = sum(per_param.values()) / len(per_param)
    assert combined >= 0.125, f"±15° 合并召回 {combined:.3f} < 0.125(逐档 {per_param})"


# ---------------------------------------------------------------------------
# 验收三:pyramid 对中心裁剪的部分恢复(A218 基线全档 0.0)
# ---------------------------------------------------------------------------


def test_pyramid_center_crop_partial_recovery(corpus: dict[str, Any]) -> None:
    """中心裁剪 10%/20%/30% 面积:pyramid_distance@11 每档召回 > 0。

    实测:0.1 → 0.250(quad 距 4 / rings_broad 距 6)、0.2 → 0.125、
    0.3 → 0.125——从 A218 的全档 0.0 中恢复**部分**档位。明确不是全档
    1.0:结构主导构图(photo 族)zoom 后三层低频三角均失配(实测距
    12~22 > 11),粗结构构图(quad)恢复最好。诚实数字,断言按实测留
    一档余量。
    """
    per_param: dict[str, float] = {}
    for param in ("0.1", "0.2", "0.3"):
        distances = [
            pyramid_distance(pyramid_hash(_attacked(corpus, kind, "crop", param)),
                             corpus["pyramid"][kind])
            for kind in KINDS
        ]
        per_param[param] = _recall(distances, PYRAMID_SUGGESTED_MAX_DISTANCE)
    assert all(r > 0 for r in per_param.values()), (
        f"裁剪各档应至少部分召回,实测 {per_param}(A218 基线全档 0.0)"
    )
    combined = sum(per_param.values()) / len(per_param)
    assert combined >= 0.125, f"裁剪合并召回 {combined:.3f} < 0.125(逐档 {per_param})"


def test_pyramid_rotation_auxiliary_recall(corpus: dict[str, Any]) -> None:
    """pyramid 对 ±3°/±8° 的辅助召回 ≥ 0.5(实测 0.875)——旋转主战是
    ring,这里只锁定金字塔的辅助贡献不退化。"""
    per_param = {}
    for param in ("3", "-3", "8", "-8"):
        distances = [
            pyramid_distance(pyramid_hash(_attacked(corpus, kind, "rotate", param)),
                             corpus["pyramid"][kind])
            for kind in KINDS
        ]
        per_param[param] = _recall(distances, PYRAMID_SUGGESTED_MAX_DISTANCE)
    combined = sum(per_param.values()) / len(per_param)
    assert combined >= 0.5, f"±3°/±8° 辅助召回 {combined:.3f} < 0.5(逐档 {per_param})"


# ---------------------------------------------------------------------------
# 验收(A228)之四:dft_ring_hash 对大角度旋转的严格不变
# (A219 边界:ring 的 DCT 能量签名 ±15° 召回 0.188)
# ---------------------------------------------------------------------------


def test_dft_ring_rotation_recall_all_angles(corpus: dict[str, Any]) -> None:
    """dft_ring ±3°/±8°/±15° 全六档召回:每档 ≥ 0.75、合并 ≥ 0.9。

    实测(建议工作距 3/32bit):全六档均 1.000(最差距 2 ≤ 3)——DFT 模长
    对角向循环平移严格不变(循环移位定理),±15° ≈ 1.33 桶的分数平移不再
    像 DCT 能量签名那样泄漏(A219 ring 同角度 0.188)。断言每档 0.75 /
    合并 0.9,对实测 1.000 留两档余量(防重采样实现的版本级微差)。
    """
    per_param: dict[str, float] = {}
    for param in ("3", "-3", "8", "-8", "15", "-15"):
        distances = [
            hamming_hex(dft_ring_hash(_attacked(corpus, kind, "rotate", param)),
                        corpus["dft_ring"][kind])
            for kind in KINDS
        ]
        per_param[param] = _recall(distances, DFT_RING_SUGGESTED_MAX_DISTANCE)
    combined = sum(per_param.values()) / len(per_param)
    assert min(per_param.values()) >= 0.75, f"最弱档 {per_param} 低于 0.75"
    assert combined >= 0.9, f"六档合并召回 {combined:.3f} < 0.9(逐档 {per_param})"


def test_dft_ring_large_rotation_beats_ring(corpus: dict[str, Any]) -> None:
    """±15° 直接对照(同攻击变体、同种子):dft_ring ≥ 0.6(任务目标线)
    且严格优于 ring(实测 1.000 vs 0.188)。

    两个哈希在同一次运行中对**同一批**落盘攻击变体取距,对照公平;ring
    的失守机理由(DCT 基对平移不封闭,~1.3 桶平移时能量泄漏过半)见
    phash2.dft_ring_hash docstring 的证明概要。
    """
    dft_distances: list[int] = []
    ring_distances: list[int] = []
    for param in ("15", "-15"):
        for kind in KINDS:
            attacked_path = _attacked(corpus, kind, "rotate", param)
            dft_distances.append(
                hamming_hex(dft_ring_hash(attacked_path), corpus["dft_ring"][kind])
            )
            ring_distances.append(
                hamming_hex(ring_hash(attacked_path), corpus["ring"][kind])
            )
    dft_recall = _recall(dft_distances, DFT_RING_SUGGESTED_MAX_DISTANCE)
    ring_recall = _recall(ring_distances, RING_SUGGESTED_MAX_DISTANCE)
    assert dft_recall >= 0.6, f"dft_ring ±15° 召回 {dft_recall:.3f} < 0.6(目标线)"
    assert dft_recall > ring_recall, (
        f"dft_ring {dft_recall:.3f} 未超过 ring {ring_recall:.3f}(同变体对照)"
    )


# ---------------------------------------------------------------------------
# 验收(A228)之五:tile_hash 对中心裁剪的恢复
# (A219 边界:pyramid 仅部分恢复 0.250/0.125/0.125,照片族全档零召回)
# ---------------------------------------------------------------------------


def test_tile_center_crop_recall_vs_pyramid(corpus: dict[str, Any]) -> None:
    """中心裁剪 10%/20%/30% 面积:tile_any_match@8 每档召回 > 0 且合并
    召回严格优于 pyramid(实测 0.375/0.750/0.875 vs 0.250/0.125/0.125)。

    机理:裁剪销毁边缘 tile,中心内容在尺度梯某一级(0.707/0.354)上以
    ≤×1.19 的尺度差保留;照片族从 A219 的全档零召回中恢复(photo 裁剪
    30% 距 10、20% 距 10 ≤ 工作距 8 临界外、0.354 级把 photo2/rings_broad/
    quad 拉回)。诚实边界:rects/blocks3(3×3 块噪声拼贴)裁 10% 距 31
    仍失守——×3.16 zoom 把块边界推进 tile 内部,低频 DCT 原理性跟不上,
    见模块 docstring。断言按实测留余量:最弱档 ≥ 0.25(实测 0.375)。
    """
    tile_recall: dict[str, float] = {}
    pyramid_recall: dict[str, float] = {}
    for param in ("0.1", "0.2", "0.3"):
        tile_distances = []
        pyramid_distances = []
        for kind in KINDS:
            attacked_path = _attacked(corpus, kind, "crop", param)
            tile_distances.append(
                tile_any_match(tile_hash(attacked_path), corpus["tile"][kind])
            )
            pyramid_distances.append(
                pyramid_distance(
                    pyramid_hash(attacked_path), corpus["pyramid"][kind]
                )
            )
        tile_recall[param] = _recall(tile_distances, TILE_SUGGESTED_MAX_DISTANCE)
        pyramid_recall[param] = _recall(
            pyramid_distances, PYRAMID_SUGGESTED_MAX_DISTANCE
        )
    assert all(r > 0 for r in tile_recall.values()), (
        f"裁剪各档应至少部分召回(A219 目标线全档 > 0),实测 {tile_recall}"
    )
    assert min(tile_recall.values()) >= 0.25, (
        f"最弱档 {tile_recall} 低于 0.25(实测 0.375,留一档余量)"
    )
    assert sum(tile_recall.values()) > sum(pyramid_recall.values()), (
        f"tile 合并召回 {tile_recall} 未超过 pyramid {pyramid_recall}(同变体对照)"
    )


def test_tile_bitwidth_tradeoff_small_tiles_collide(corpus: dict[str, Any]) -> None:
    """位宽权衡的锁定证据:64bit tile 下 28 对异图最小距 ≥ 12 > 工作距 8。

    任务草案的 8bit/tile(k=3)与 15bit(k=4)在 144 对任一命中语义下异图
    最小距实测塌缩到 **0**(小 tile 信息量不足必撞,开发期实测,故
    phash2 锁定 64bit);0.25 边长梯级同理。本用例锁定**现行配置**的
    误报上沿:异图最小距 12 与工作距 8 之间留 4bit 裕量,任何把位宽
    调小 / 阈值放大的改动都应先复跑本用例的口径。
    """
    cross = [
        tile_any_match(corpus["tile"][a], corpus["tile"][b])
        for i, a in enumerate(KINDS) for b in KINDS[i + 1:]
    ]
    assert min(cross) >= 12, f"tile 异图最小距 {min(cross)} < 12(误报上沿失守)"


# ---------------------------------------------------------------------------
# 验收四:恒等距离 0 + 合法压缩/缩放不漏报
# ---------------------------------------------------------------------------


def test_identity_variants_distance_zero(corpus: dict[str, Any]) -> None:
    """恒等(字节复制)与无损重编码(PNG 往返)下三哈希距离恒 0。"""
    src = Path(corpus["paths"]["photo"])
    byte_copy = Path(corpus["dir"]) / "photo__bytes.png"
    shutil.copyfile(src, byte_copy)
    reencoded = Path(corpus["dir"]) / "photo__reencoded.png"
    with Image.open(src) as im:
        im.save(reencoded, format="PNG", compress_level=9)
    for path in (str(byte_copy), str(reencoded)):
        assert hamming_hex(mirror_hash(path), corpus["mirror"]["photo"]) == 0
        assert hamming_hex(ring_hash(path), corpus["ring"]["photo"]) == 0
        assert pyramid_distance(pyramid_hash(path), corpus["pyramid"]["photo"]) == 0


@pytest.mark.parametrize("family,param", [
    ("jpeg", "60"), ("jpeg", "30"), ("jpeg", "10"),
    ("resize", "1.5x"), ("resize", "0.5x"),
])
def test_benign_recompression_and_rescale_stay_close(
    corpus: dict[str, Any], family: str, param: str
) -> None:
    """合法压缩(JPEG 60/30/10)与缩放(±)下三哈希不漏报。

    照片式(photo/photo2)与云纹(cloud)构图实测最差距 mirror 2(≤12)/
    ring 4(≤6)/ pyramid 2(≤11)。这是"不误报"的另一半:良性重编码
    不得把同图推出工作距。
    """
    for kind in ("photo", "photo2", "cloud"):
        attacked = _attacked(corpus, kind, family, param)
        dm = hamming_hex(mirror_hash(attacked), corpus["mirror"][kind])
        dr = hamming_hex(ring_hash(attacked), corpus["ring"][kind])
        dp = pyramid_distance(pyramid_hash(attacked), corpus["pyramid"][kind])
        assert dm <= MIRROR_SUGGESTED_MAX_DISTANCE, f"{kind} mirror 距 {dm}"
        assert dr <= RING_SUGGESTED_MAX_DISTANCE, f"{kind} ring 距 {dr}"
        assert dp <= PYRAMID_SUGGESTED_MAX_DISTANCE, f"{kind} pyramid 距 {dp}"


# ---------------------------------------------------------------------------
# 验收五:clean 异构图不误报(跨图最小距严格大于各自建议工作距)
# ---------------------------------------------------------------------------


def test_clean_compositions_no_cross_false_positive(corpus: dict[str, Any]) -> None:
    """8 种互异构图两两(28 对)跨距:mirror > 12(实测最小 14)、
    pyramid > 11(实测最小 12)、ring(角向有信息的 7 构图,21 对)> 6
    (实测最小 8)。

    rings_broad 属旋转等价类退化构图(ring_hash docstring 明示),不参与
    ring 断言;mirror/pyramid 对全部 8 构图成立。
    """
    mirror_pairs = [
        hamming_hex(corpus["mirror"][a], corpus["mirror"][b])
        for i, a in enumerate(KINDS) for b in KINDS[i + 1:]
    ]
    assert min(mirror_pairs) > MIRROR_SUGGESTED_MAX_DISTANCE, (
        f"mirror 跨图最小距 {min(mirror_pairs)} 须 > {MIRROR_SUGGESTED_MAX_DISTANCE}"
    )

    pyramid_pairs = [
        pyramid_distance(corpus["pyramid"][a], corpus["pyramid"][b])
        for i, a in enumerate(KINDS) for b in KINDS[i + 1:]
    ]
    assert min(pyramid_pairs) > PYRAMID_SUGGESTED_MAX_DISTANCE, (
        f"pyramid 跨图最小距 {min(pyramid_pairs)} 须 > {PYRAMID_SUGGESTED_MAX_DISTANCE}"
    )

    ring_pairs = [
        hamming_hex(corpus["ring"][a], corpus["ring"][b])
        for i, a in enumerate(ANGULAR_KINDS) for b in ANGULAR_KINDS[i + 1:]
    ]
    assert min(ring_pairs) > RING_SUGGESTED_MAX_DISTANCE, (
        f"ring 跨图最小距 {min(ring_pairs)} 须 > {RING_SUGGESTED_MAX_DISTANCE}"
    )


def test_ring_degenerate_radial_compositions_documented(
    corpus: dict[str, Any]
) -> None:
    """锁定文档化的退化语义:辐射对称构图(rings_broad)与其余构图在
    ring 32bit 角能量签名下不可分(实测最小距 2)——旋转不变指纹数学上
    不可能区分互为旋转等价的构图,故其不参与误报断言(见上一用例),
    此处锁定该边界为**文档化行为**而非回归。
    """
    cross = [
        hamming_hex(corpus["ring"]["rings_broad"], corpus["ring"][other])
        for other in ANGULAR_KINDS
    ]
    assert min(cross) <= RING_SUGGESTED_MAX_DISTANCE


# ---------------------------------------------------------------------------
# 验收(A228)之六:两新哈希的恒等 / 良性 / 跨图误报 / 退化 / 校验面
# ---------------------------------------------------------------------------


def test_identity_variants_new_hashes_distance_zero(
    corpus: dict[str, Any]
) -> None:
    """恒等(字节复制)与无损重编码(PNG 往返)下 dft_ring / tile 距离恒 0。"""
    src = Path(corpus["paths"]["photo"])
    byte_copy = Path(corpus["dir"]) / "photo__bytes_a228.png"
    shutil.copyfile(src, byte_copy)
    reencoded = Path(corpus["dir"]) / "photo__reencoded_a228.png"
    with Image.open(src) as im:
        im.save(reencoded, format="PNG", compress_level=9)
    for path in (str(byte_copy), str(reencoded)):
        assert hamming_hex(dft_ring_hash(path), corpus["dft_ring"]["photo"]) == 0
        assert tile_any_match(tile_hash(path), corpus["tile"]["photo"]) == 0


@pytest.mark.parametrize("family,param", [
    ("jpeg", "60"), ("jpeg", "30"), ("jpeg", "10"),
    ("resize", "1.5x"), ("resize", "0.5x"),
])
def test_benign_variants_new_hashes_stay_close(
    corpus: dict[str, Any], family: str, param: str
) -> None:
    """合法压缩(JPEG 60/30/10)与缩放(±)下两新哈希不漏报(全 8 构图)。

    实测最差距:dft_ring 2(≤3)/ tile 6(≤8)——dft 的角向模长谱与 tile
    的低频 DCT 对良性重编码同其兄弟哈希一样稳定;tile 的 6 出现在
    jpeg/10 的 cross_wavy/quad(极端质量档的块效应),仍在工作距内。
    """
    for kind in KINDS:
        attacked_path = _attacked(corpus, kind, family, param)
        dft_distance = hamming_hex(
            dft_ring_hash(attacked_path), corpus["dft_ring"][kind]
        )
        tile_distance = tile_any_match(
            tile_hash(attacked_path), corpus["tile"][kind]
        )
        assert dft_distance <= DFT_RING_SUGGESTED_MAX_DISTANCE, (
            f"{kind} dft_ring {family}/{param} 距 {dft_distance}"
        )
        assert tile_distance <= TILE_SUGGESTED_MAX_DISTANCE, (
            f"{kind} tile {family}/{param} 距 {tile_distance}"
        )


def test_clean_compositions_no_cross_false_positive_new_hashes(
    corpus: dict[str, Any]
) -> None:
    """8 种互异构图跨距:dft_ring(角向有信息的 7 构图,21 对)> 3(实测
    最小 4,近邻对为 photo2×cross_wavy / cloud×quad 平滑构图对);tile
    (全部 8 构图,28 对)> 8(实测最小 12)。

    rings_broad 不参与 dft_ring 断言(径向构图是其退化类,见下一用例);
    tile 含全图段,辐射构图不退化,全部 8 构图参与。
    """
    dft_pairs = [
        hamming_hex(corpus["dft_ring"][a], corpus["dft_ring"][b])
        for i, a in enumerate(ANGULAR_KINDS) for b in ANGULAR_KINDS[i + 1:]
    ]
    assert min(dft_pairs) > DFT_RING_SUGGESTED_MAX_DISTANCE, (
        f"dft_ring 跨图最小距 {min(dft_pairs)} 须 > {DFT_RING_SUGGESTED_MAX_DISTANCE}"
    )

    tile_pairs = [
        tile_any_match(corpus["tile"][a], corpus["tile"][b])
        for i, a in enumerate(KINDS) for b in KINDS[i + 1:]
    ]
    assert min(tile_pairs) > TILE_SUGGESTED_MAX_DISTANCE, (
        f"tile 跨图最小距 {min(tile_pairs)} 须 > {TILE_SUGGESTED_MAX_DISTANCE}"
    )


def test_dft_ring_radial_degenerate_documented(corpus: dict[str, Any]) -> None:
    """锁定 dft_ring 的文档化退化语义:辐射对称构图(rings_broad)各带
    角向无结构,经幅度比退化保护置零 → 哈希恒 ``00000000``,与其余构图
    最小距实测 ≥ 12(不可分方向与 ring 的退化类一致,但退化值**稳定**
    ——伪影不决定哈希位,这正是退化保护的意义,对照 A219 rings_broad
    的 ring 哈希由伪影决定、跨图距 2)。
    """
    assert corpus["dft_ring"]["rings_broad"] == "00000000"
    cross = [
        hamming_hex(corpus["dft_ring"]["rings_broad"], corpus["dft_ring"][other])
        for other in ANGULAR_KINDS
    ]
    assert min(cross) >= 12, f"径向退化构图与其余构图最小距 {min(cross)} < 12"


def test_solid_image_degenerate_semantics_new_hashes(tmp_path: Any) -> None:
    """单色图退化语义(与两新哈希 docstring 逐字一致):

    - dft_ring:角向常量带经退化保护置零 → 任意灰度单色图(含全黑)恒
      ``00000000``;
    - tile:每 tile 交流系数全零(相对峰值归零保护)→ 灰度 >0 的单色图
      恒为 ``8000000000000000`` × 12 段(全零灰度恒为全 0);
    - 两个不同灰度的单色图两哈希距离均为 0——无结构可辨,单色图去重
      应走字节 sha256,不入指纹库。
    """
    bright = _write_png_gray(tmp_path / "solid_bright.png", [[200] * 8] * 8)
    other = _write_png_gray(tmp_path / "solid_other.png", [[37] * 8] * 8)
    black = _write_png_gray(tmp_path / "solid_black.png", [[0] * 8] * 8)
    assert dft_ring_hash(bright) == dft_ring_hash(other) == dft_ring_hash(black) \
        == "00000000"
    assert tile_hash(bright) == tile_hash(other) == "8000000000000000" * 12
    assert tile_hash(black) == "0" * 192
    assert hamming_hex(dft_ring_hash(bright), dft_ring_hash(other)) == 0
    assert tile_any_match(tile_hash(bright), tile_hash(other)) == 0


def test_tile_any_match_validation_and_vectors(corpus: dict[str, Any]) -> None:
    """tile_any_match 入参校验(192 hex 定长 / 非法 hex 中文报错)与已知向量:

    1. 自距 0;手工切 12×12 = 144 对逐对汉明取最小 == 函数返回(切片口径
       与返回语义逐位锁定,两处实现漂移即失守);
    2. 每 tile 段翻 1 bit / 2 bit → 最小 tile 距离恰为 1 / 2(任一命中
       语义下的距离下界)。
    """
    base = corpus["tile"]["photo"]
    assert tile_any_match(base, base) == 0
    other = corpus["tile"]["quad"]
    manual = min(
        hamming_hex(base[i * 16:(i + 1) * 16], other[j * 16:(j + 1) * 16])
        for i in range(12) for j in range(12)
    )
    assert tile_any_match(base, other) == manual

    one_bit = "".join(
        f"{int(base[i * 16:(i + 1) * 16], 16) ^ 1:016x}" for i in range(12)
    )
    two_bits = "".join(
        f"{int(base[i * 16:(i + 1) * 16], 16) ^ 3:016x}" for i in range(12)
    )
    assert tile_any_match(base, one_bit) == 1
    assert tile_any_match(base, two_bits) == 2

    with pytest.raises(ValueError):
        tile_any_match(base, base[:-2])   # 长度不符
    with pytest.raises(ValueError):
        tile_any_match(base, "z" * 192)   # 非法 hex


# ---------------------------------------------------------------------------
# 验收六:退化语义与错误分支
# ---------------------------------------------------------------------------


def test_solid_image_degenerate_semantics(tmp_path: Any) -> None:
    """单色图退化语义(与三哈希 docstring 逐字一致):

    - mirror / pyramid:交流系数全零(相对峰值归零保护)→ 灰度 >0 的
      单色图哈希恒为"最高位 1、其余 0"的固定值(DCT 位序 flat[0] 最高);
    - ring:角向常量带能量全零 → 任意单色图(含灰度 0)恒 "00000000";
    - 两个不同灰度的单色图三哈希距离均为 0——无结构可辨,单色图去重
      应走字节 sha256,不入指纹库。
    """
    bright = _write_png_gray(tmp_path / "solid_bright.png", [[200] * 8] * 8)
    other = _write_png_gray(tmp_path / "solid_other.png", [[37] * 8] * 8)
    black = _write_png_gray(tmp_path / "solid_black.png", [[0] * 8] * 8)
    assert mirror_hash(bright) == mirror_hash(other) == "8000000000000000"
    assert mirror_hash(black) == "0" * 16
    assert pyramid_hash(bright) == pyramid_hash(other) == "800000000" * 3
    assert pyramid_hash(black) == "0" * 27
    assert ring_hash(bright) == ring_hash(other) == ring_hash(black) == "00000000"


@pytest.mark.parametrize(
    "func", [mirror_hash, ring_hash, pyramid_hash, dft_ring_hash, tile_hash]
)
def test_missing_file_raises_chinese(tmp_path: Any, func: Any) -> None:
    """各不变性哈希与 phash256 同款容错面:文件不存在 → 中文 ValueError。"""
    with pytest.raises(ValueError, match="图片文件不存在"):
        func(str(tmp_path / "nope.png"))


@pytest.mark.parametrize(
    "func", [mirror_hash, ring_hash, pyramid_hash, dft_ring_hash, tile_hash]
)
def test_corrupt_file_raises_chinese(tmp_path: Any, func: Any) -> None:
    """垃圾字节 → 中文 ValueError(无法解码)。"""
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"definitely not a png \x00\x01")
    with pytest.raises(ValueError, match="无法解码图片文件"):
        func(str(garbage))


@pytest.mark.parametrize(
    "func", [mirror_hash, ring_hash, pyramid_hash, dft_ring_hash, tile_hash]
)
def test_pil_missing_raises_chinese(
    tmp_path: Any, monkeypatch: Any, func: Any
) -> None:
    """Pillow 缺失(sys.modules 屏蔽)→ 各哈希均直接中文 ValueError
    (与 phash2.py 同为无降级路径;新哈希无降级等价物)。"""
    path = _write_png_gray(tmp_path / "pic.png", _synth_rows("photo", 64))
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ValueError, match="未安装 Pillow"):
        func(path)


def test_pyramid_distance_validation_and_vectors(corpus: dict[str, Any]) -> None:
    """pyramid_distance 入参校验(27 hex 定长 / 非法 hex 中文报错)与
    三层取 min 的已知向量。"""
    base = corpus["pyramid"]["photo"]
    seg = len(base) // 3
    # 第二层整体取反(36bit 全翻),其余层不动:min = 0
    flipped_mid = base[:seg] + f"{int(base[seg:2 * seg], 16) ^ (2**36 - 1):09x}" + base[2 * seg:]
    assert pyramid_distance(base, flipped_mid) == 0
    # 三层各差 1 bit:min = 1
    one_bit = "".join(
        f"{int(base[i * seg:(i + 1) * seg], 16) ^ 1:09x}" for i in range(3)
    )
    assert pyramid_distance(base, one_bit) == 1
    assert pyramid_distance(base, base) == 0
    with pytest.raises(ValueError):
        pyramid_distance(base, base[:26])  # 长度不符
    with pytest.raises(ValueError):
        pyramid_distance(base, "z" * 27)   # 非法 hex


# ---------------------------------------------------------------------------
# 验收七:PhashRegistry 多哈希登记 / 查询(端到端,与 intel 扩展闭环)
# ---------------------------------------------------------------------------


def test_registry_mirror_and_pyramid_roundtrip(
    corpus: dict[str, Any], tmp_path: Any
) -> None:
    """端到端:登记图 A(带 mirror/pyramid 列)后——

    1. 用 A 的**水平翻转版**的 mirror 哈希查询(hash_kind="mirror")以
       距离 0 命中 A(A218 基线:flip 对 phash 查询零命中);
    2. 用 A 的**中心裁剪 20%** 版的 pyramid 哈希查询(hash_kind="pyramid",
       max_distance=11)命中 A(quad 构图实测距 6;A218 基线:裁剪零命中);
    3. 旧式记录(不带新列)在 mirror 查询下回退 phash 列比较,行为可用。
    """
    reg = PhashRegistry(str(tmp_path / "phash.db"))
    kind = "quad"
    sha_a = "a" * 64
    reg.register(
        sha_a,
        "0" * 16,  # phash 列:本用例不依赖
        "https://site-a.example/",
        mirror_hash=corpus["mirror"][kind],
        pyramid_hash=corpus["pyramid"][kind],
    )

    flipped = mirror_hash(_attacked(corpus, kind, "flip", "h"))
    hits = reg.find_similar(flipped, max_distance=0, hash_kind="mirror")
    assert [(h["sha256"], h["distance"]) for h in hits] == [(sha_a, 0)]

    cropped = pyramid_hash(_attacked(corpus, kind, "crop", "0.2"))
    distance = pyramid_distance(cropped, corpus["pyramid"][kind])
    assert distance <= PYRAMID_SUGGESTED_MAX_DISTANCE, f"quad 裁剪距 {distance}"
    hits = reg.find_similar(
        cropped, max_distance=PYRAMID_SUGGESTED_MAX_DISTANCE, hash_kind="pyramid"
    )
    assert [(h["sha256"], h["distance"]) for h in hits] == [(sha_a, distance)]

    # 旧式记录(mirror 列为空)→ mirror 查询回退 phash 列比较
    sha_b = "b" * 64
    reg.register(sha_b, "0" * 16, "https://site-b.example/")
    hits = reg.find_similar("0" * 16, max_distance=0, hash_kind="mirror")
    assert {h["sha256"] for h in hits} == {sha_a, sha_b}  # sha_b 经回退列命中
    assert all(h["distance"] == 0 for h in hits)
    reg.close()


def test_registry_pyramid_distance_matches_vision_semantics(
    corpus: dict[str, Any], tmp_path: Any
) -> None:
    """registry 的金字塔距离(本地实现,intel 层不得上溯 import vision 层)
    与 vision.phash2.pyramid_distance 口径锁定:构造"仅一层差 1 bit"的
    查询,find_similar 命中距离 = 0(三层取 min);两处实现漂移即失守。"""
    reg = PhashRegistry(str(tmp_path / "phash.db"))
    sha = "c" * 64
    base = corpus["pyramid"]["cloud"]
    reg.register(sha, "0" * 16, "s", pyramid_hash=base)
    seg = len(base) // 3
    query = base[:seg] + f"{int(base[seg:2 * seg], 16) ^ 1:09x}" + base[2 * seg:]
    assert pyramid_distance(query, base) == 0  # min 语义:最像的层为准
    hits = reg.find_similar(query, max_distance=0, hash_kind="pyramid",
                            exclude_sha256=sha)
    assert hits == []
    hits = reg.find_similar(query, max_distance=0, hash_kind="pyramid")
    assert [(h["sha256"], h["distance"]) for h in hits] == [(sha, 0)]
    reg.close()
