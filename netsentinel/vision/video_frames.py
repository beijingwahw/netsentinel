"""视频/GIF 帧采样(V3 · A48;V5 性能/可观测升级)。

违规站点常用动图/短视频承载违规内容以躲避静态图片审查。本模块把动图
变成可评分的静帧证据,接入现有 ImageEvidence → 分类器 → 复核链路:

- **GIF**:Pillow 直接采样(惰性导入,缺失时返回空列表并提示安装);
  帧数 ≤ cfg.video_max_frames 全取,否则按 round(i*(n-1)/(k-1)) 均匀采样;
- **MP4/MOV/MKV/WEBM**:本模块不捆绑视频解码器,返回空列表并给出
  ffmpeg 抽帧的中文指引(抽帧产物落盘后重扫即可,符合"零重型依赖"红线);
- 帧文件命名 ``<原stem>_frame<帧号:02d>.png``,**保留原文件名关键词**
  (如 ``nsfw_hi.gif`` → ``nsfw_hi_frame00.png``),保证 stub 离线桩规则
  与证据链路继续可用;
- 每帧回填 sha256/宽高,url 形如 ``<原url>#frame:<帧号>``,可溯源到源动图。

V5 升级要点:
- 每帧**单遍落盘**:PNG 字节只编码一次,sha256 直接对内存字节计算,
  不再"写盘后整文件回读"(k 帧省 k 次文件读);
- 遥测:采样成功后 ``telemetry.inc("video.frames", 采样帧数)``;
- 坏文件路径清理半途落盘的帧(不留孤儿证据,见 ``written`` 清理段)。

损坏文件、缺 Pillow、无解码器等情形一律返回空列表,**不抛异常、不阻断**
主扫描流程。

用法示例::

    from netsentinel.contracts import Config
    from netsentinel.vision.video_frames import sample_frames

    evs = sample_frames("dl/anim.gif", Config(), "dl/frames",
                        url="http://x/anim.gif", source_page="http://x/")
"""
from __future__ import annotations

import hashlib
import io
import logging
import types
from pathlib import Path

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence

__all__ = ["is_supported", "sample_frames"]

logger = logging.getLogger(__name__)

# 需要真实视频解码器的容器格式(当前无解码器,仅提示 ffmpeg 工作流)
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm"})
_SUPPORTED_EXTS = _VIDEO_EXTS | {".gif"}

# 视频容器格式的统一中文提示(契约 §3 A48 固定文案)
_FFMPEG_HINT = (
    "暂无视频解码器:请用 ffmpeg -i in.mp4 -vf fps=1/N 抽帧到目录后重扫;"
    "本模块当前仅支持 GIF 直接采样"
)


def _load_pil():
    """惰性加载 PIL.Image;未安装/损坏时返回 None(不抛异常)。"""
    try:
        import importlib

        image = importlib.import_module("PIL.Image")
        importlib.import_module("PIL")
    except Exception:  # None/缺失/半初始化的 PIL 一律按"未安装"处理
        return None
    return types.SimpleNamespace(Image=image)


def _sample_indices(n: int, k: int) -> list[int]:
    """从 n 帧中取 k 帧的原始帧号序列(升序、去重无需)。

    n <= k 时全取;k <= 1 时只取首帧;否则均匀采样 round(i*(n-1)/(k-1))。
    """
    if n <= 0 or k <= 0:
        return []
    if n <= k:
        return list(range(n))
    if k == 1:
        return [0]
    return [round(i * (n - 1) / (k - 1)) for i in range(k)]


def is_supported(path: str | Path) -> bool:
    """判断媒体扩展名是否在帧采样支持范围内(gif/mp4/mov/mkv/webm,不分大小写)。"""
    return Path(path).suffix.lower() in _SUPPORTED_EXTS


def sample_frames(
    media_path: str | Path,
    cfg: Config,
    out_dir: str | Path,
    *,
    url: str = "",
    source_page: str = "",
) -> list[ImageEvidence]:
    """把动图/视频采样为静帧证据列表;任何不可采样情形返回 []。

    Args:
        media_path: 媒体文件路径(本模块仅 .gif 可直接采样)。
        cfg: 全局配置,使用 cfg.video_max_frames 控制每站最多采样帧数。
        out_dir: 帧图片输出目录(不存在时自动创建)。
        url: 原始媒体 URL;缺省用 media_path 本身,帧证据 url 追加
            ``#frame:<帧号>`` 片段以便溯源。
        source_page: 抓到该媒体的页面 URL(可选,缺省空串)。

    Returns:
        采样出的 ImageEvidence 列表(按帧号升序);文件不存在、格式不支持、
        缺 Pillow、GIF 损坏、video_max_frames<=0 等均返回空列表并记录日志。
    """
    src = Path(media_path)
    if not src.is_file():
        logger.warning("帧采样跳过:文件不存在 %s", media_path)
        return []

    ext = src.suffix.lower()
    if ext in _VIDEO_EXTS:
        logger.info("%s(%s)", _FFMPEG_HINT, media_path)
        return []
    if ext != ".gif":
        logger.warning(
            "帧采样跳过:不支持的媒体类型 %s(支持 gif/mp4/mov/mkv/webm)", media_path
        )
        return []

    pil = _load_pil()
    if pil is None:
        logger.info("未安装 Pillow:无法直接采样 GIF 帧(pip install Pillow 启用);%s", src)
        return []

    written: list[Path] = []
    try:
        with pil.Image.open(src) as im:
            n = getattr(im, "n_frames", None)
            if not isinstance(n, int) or n < 1:
                logger.warning("帧采样跳过:GIF 缺少有效帧数,文件可能损坏 %s", media_path)
                return []
            k = int(getattr(cfg, "video_max_frames", 6))
            indices = _sample_indices(n, k)
            if not indices:
                logger.info("video_max_frames=%s(≤0),跳过帧采样 %s", k, media_path)
                return []

            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            base_url = url or str(media_path)
            stem = src.stem
            evidences: list[ImageEvidence] = []
            for idx in indices:
                im.seek(idx)
                frame = im.convert("RGB")
                # 单遍流式:PNG 只编码一次,哈希与落盘共用同一份字节
                # (旧实现每帧写盘后整文件回读算哈希,k 帧多读 k 次)。
                buf = io.BytesIO()
                frame.save(buf, format="PNG")
                data = buf.getvalue()
                target = out / f"{stem}_frame{idx:02d}.png"
                target.write_bytes(data)
                written.append(target)
                evidences.append(
                    ImageEvidence(
                        path=str(target),
                        url=f"{base_url}#frame:{idx}",
                        source_page=source_page,
                        sha256=hashlib.sha256(data).hexdigest(),
                        width=frame.width,
                        height=frame.height,
                    )
                )
        logger.info(
            "GIF 帧采样完成:%s 共 %d 帧,采样 %d 帧 → %s", media_path, n, len(evidences), out_dir
        )
        telemetry.inc("video.frames", len(evidences))
        return evidences
    except Exception as exc:  # 解码/落盘异常统一按"损坏文件"处理,不阻断主流程
        for target in written:  # 清理半途落盘的帧,避免残留孤儿证据
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning("帧采样跳过:GIF 解码失败 %s(%s)", media_path, exc)
        return []
