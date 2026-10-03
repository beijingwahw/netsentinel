# -*- coding: utf-8 -*-
"""纯标准库 PNG 编码器(NetSentinel · A19)。

只用 struct / zlib 生成最小合法 PNG(8 位真彩色 RGB、无隔行):

- ``make_png(width, height, rgb) -> bytes``:依次输出 IHDR / IDAT / IEND 三个块;
  每条扫描线前置 filter=0(None)字节,像素行整体经 zlib 压缩后放入 IDAT;
- 命令行:``python scripts/make_png W H RRGGBB out.png``。

用途:离线生成演示/测试夹具图片(tests/fixtures/demo_site),不依赖 Pillow,
保证任何 Python 3.10+ 环境都能复现完全相同的夹具字节。全程无网络访问。

V5 确认与升级(生成字节零变化):

- IDAT 生成本就走 ``zlib.compress``(级别 9)——**确认无需改动**;大尺寸
  行构造用的是 ``bytes`` 整体乘法(``scanline * height``,单次分配、无
  字符串/逐块拼接),亦为最优实现——确认保持;
- CLI 宽/高/颜色取值校验改为入口内统一中文提示(退出码 2 语义不变;
  argparse 的 type 回调会把中文 ValueError 吞成英文 "invalid value")。
"""
from __future__ import annotations

import argparse
import string
import struct
import sys
import zlib
from pathlib import Path

__all__ = ["make_png", "main"]

#: PNG 文件签名(8 字节,固定值)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: IDAT 压缩级别(0-9;9 压缩比最高,纯色图体积本就很小)
_ZLIB_LEVEL = 9


def _chunk(tag: bytes, data: bytes) -> bytes:
    """拼装单个 PNG 块:长度(4B,大端)+ 类型(4B)+ 数据 + CRC32(4B)。"""
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _validate_dimension(name: str, value: int) -> int:
    """校验宽/高:必须是非布尔的正整数,否则抛 ValueError(中文)。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须为正整数,当前为 {value!r}")
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数,当前为 {value}")
    return value


def _validate_rgb(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """校验颜色三元组:3 个 0-255 的非布尔整数,否则抛 ValueError(中文)。"""
    if not isinstance(rgb, (tuple, list)) or len(rgb) != 3:
        raise ValueError(f"rgb 必须是 (R, G, B) 三元组,当前为 {rgb!r}")
    values: list[int] = []
    for channel, value in zip("RGB", rgb):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"rgb 的 {channel} 分量必须为 0-255 的整数,当前为 {value!r}")
        if not 0 <= value <= 255:
            raise ValueError(f"rgb 的 {channel} 分量必须在 0-255 范围内,当前为 {value}")
        values.append(value)
    return (values[0], values[1], values[2])


def make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成一张 ``width x height`` 的纯色 RGB PNG,返回完整文件字节串。

    - 颜色模型:8 位 / 真彩色(color type 2),每像素 3 字节;
    - 扫描线:每行前置 filter=0,``rgb`` 重复 ``width`` 次;
    - 块顺序:IHDR → IDAT → IEND(单张最小合法 PNG);
    - 参数非法(非正尺寸 / 颜色越界等)抛 :class:`ValueError`(中文消息)。
    """
    width = _validate_dimension("width", width)
    height = _validate_dimension("height", height)
    rgb = _validate_rgb(rgb)

    # IHDR:宽、高(大端 u32),位深 8,颜色类型 2(RGB),压缩 0,滤波 0,隔行 0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # 一条扫描线:filter=0 + width 个像素;整幅图重复 height 行后一次压缩
    scanline = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(scanline * height, _ZLIB_LEVEL)

    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _parse_hex_color(text: str) -> tuple[int, int, int]:
    """把 ``RRGGBB``(6 位十六进制)解析为 (R, G, B);非法抛 ValueError(中文)。"""
    cleaned = text.strip()
    if len(cleaned) != 6 or any(ch not in string.hexdigits for ch in cleaned):
        raise ValueError(f"颜色必须形如 RRGGBB(6 位十六进制),当前为 {text!r}")
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def _positive_int(text: str) -> int:
    """argparse 用的正整数类型函数;非法抛 ValueError(中文)。"""
    value = int(text)
    if value <= 0:
        raise ValueError(f"必须为正整数,当前为 {text!r}")
    return value


def _parse_width(text: str) -> int:
    """把宽度参数解析为正整数;非法(非整数 / 非正数)抛 ValueError(中文)。"""
    try:
        return _positive_int(text)
    except ValueError:
        if _looks_numeric(text):
            raise ValueError(f"宽度必须为正整数,当前为 {text}") from None
        raise ValueError(f"宽度必须为正整数(十进制数字),当前为 {text!r}") from None


def _parse_height(text: str) -> int:
    """把高度参数解析为正整数;非法(非整数 / 非正数)抛 ValueError(中文)。"""
    try:
        return _positive_int(text)
    except ValueError:
        if _looks_numeric(text):
            raise ValueError(f"高度必须为正整数,当前为 {text}") from None
        raise ValueError(f"高度必须为正整数(十进制数字),当前为 {text!r}") from None


def _looks_numeric(text: str) -> bool:
    """参数是否是"数字但取值非法"(如 0 / -3 / 2.5),用于区分两类中文提示。"""
    cleaned = text.strip()
    return bool(cleaned) and all(
        (ch.isdigit() or ch in "+-.") and not ch.isalpha() for ch in cleaned
    )


def _ensure_utf8_stdio() -> None:
    """Windows 管道/终端非 UTF-8 时切换标准输出编码,避免中文打印报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.encoding and stream.encoding.lower() not in (
                "utf-8",
                "utf8",
            ):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 重新配置失败不影响主流程
            pass


def main(argv: list[str] | None = None) -> int:
    """命令行入口:``python scripts/make_png.py W H RRGGBB out.png``。

    返回码:0 成功;2 参数/取值错误(**中文提示**输出到 stderr)。

    V5 质量:宽/高/颜色的取值校验全部在入口内以中文消息完成(argparse 的
    type 回调会把自定义 ValueError 吞成英文 "invalid value",故改为先收
    原始字符串再统一校验);生成逻辑不变——IDAT 仍由 zlib 单次压缩,
    扫描线用 bytes 整体乘法构造(单次分配,无逐块拼接)。
    """
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="python scripts/make_png.py",
        description="纯标准库 PNG 生成器:输出一张指定尺寸的纯色 RGB PNG(离线,无三方依赖)",
    )
    parser.add_argument("width", metavar="W", help="图片宽度(像素,正整数)")
    parser.add_argument("height", metavar="H", help="图片高度(像素,正整数)")
    parser.add_argument("color", metavar="RRGGBB", help="纯色十六进制颜色,例如 8B1E3F")
    parser.add_argument("out", metavar="out.png", help="输出 PNG 文件路径(父目录自动创建)")
    args = parser.parse_args(argv)

    try:
        width = _parse_width(args.width)
        height = _parse_height(args.height)
        rgb = _parse_hex_color(args.color)
        data = make_png(width, height, rgb)
    except ValueError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    print(f"已生成 PNG:{out_path}({width}x{height},颜色 #{args.color.upper()},{len(data)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
