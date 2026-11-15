"""VLM 送审闸门(P0-1):判定口径前置过滤 + 送审级去重,零判定影响。

背景(2026-10-13 实弹 + 受控基准实测):
- 真实站点 www.mrdsk.org:30 张送审 → ensemble 按 path 归并仅 17 张
  (13 次重复文件重复调用;识别失败不进缓存,余额耗尽时重复图放大盲打,
  实测 57 次 HTTP 429);
- 受控基准(本地证据语料回环服务):19 次送审 → 9 个唯一文件,其中
  22×19 / 128×128 两张小图从不参与判定(verdict.assess 只扫
  "宽或高 >= cfg.min_image_px" 的候选)。

三层闸门(全部可证零判定影响):
1. **小图拦截(判定口径镜像)**:非判定候选的图不送审。候选谓词与
   :func:`netsentinel.decision.verdict.assess` 完全一致("宽或高 >=
   ``cfg.min_image_px``"——**部分未测得如 0×500 按候选处理**:h 已
   达标,assess 今日就采信它,闸门同样一律放行)。被拦图在旧行为里
   也不参与 agg/count,判定输入集不变。由于本模块位于 vision 层
   (import-linter 禁止 vision → decision),口径一致性不靠共享导入,
   而由 ``tests/test_send_gate.py`` 的**网格机器对账**锁定:对
   (宽,高,min_px) 全网格断言"闸门放行 ⟺ assess 候选",任何人改动
   其一而对不齐,测试立即失败。
2. **送审级去重**:同一内容(sha256,缺失时回退 path)只送首次出现。
   旧行为里重复证据要么在 ensemble 按 path 归并(同路径),要么产生
   同分多条(同内容异路径),聚合结果与单条完全一致;去重只是把
   "重复外呼"这个纯成本项消掉(缓存命中路径同样省一次编码/查库)。
3. **fail-open 未测得放行(缺省)**:宽或高 <= 0 且已测得一边也不达标
   (如 0×0 / 0×100)的图**默认照送**——绝不因"测不出尺寸"丢证据。
   这类图在 assess 里同样不是候选(0 与任何已测得小值都 < min_px),
   送不送都不动判定;运营者可开 ``cfg.vlm_send_gate_drop_unmeasured``
   (附加属性,缺省 False)连它们一起拦下换最大节省——两种模式都
   严格判定中性。
4. **同站近重复拦下(P1-4,与前三层不同——是语义判断而非可证等价)**:
   拟送审图计算 64bit phash(``intel.phash`` DCT 路径,实测 P50
   1.27ms/张,比一次 VLM 便宜 ~3600×),与**已送审**指纹比对,汉明距
   ≤ ``vlm_neardup_threshold_bits``(缺省 4/64bit,业界保守近重复线)
   拦下(网格站同一图的多分辨率/重压缩变体只送代表图)。**仅同站
   生效**(gate 实例即站点边界;跨站共享需缓存键升级,明确不做);
   指纹失败(模块缺席/文件损坏/解码失败)一律 fail-open 放行。
   **风险如实登记**:距离 ≤4 的两张图视觉上近乎相同,VLM 按内容打分
   分差预期≈0,但**非可证零判定影响**(近重复变体若本可得不同分数,
   拦下即少一份证据);实测锚点:既有 45 张真实证据语料最小夹具距离
   6(无误伤)、近重复率 15%(7 对,含 sha 抓不到的截图/logo 变体);
   误合并影响未在真实 VLM 分数上量化(账户欠费)——保守阈值 + 一键
   回退(``vlm_neardup_dedup: false``)+ 近重复计数入 intel/审计留痕。

开关:``cfg.vlm_send_gate``(附加实例属性,缺省 True = 开启;
置 False 完全回旧行为)。闸门只作用于 run_scan 的图片送审路径,
页面级 VLM(page_vlm)/仲裁(arbiter)/级联(cascade)不受影响。

仅依赖标准库与共享契约;离线、零外呼。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence

__all__ = ["SendGate", "SendGateStats", "apply_send_gate", "is_decision_candidate"]

logger = logging.getLogger(__name__)


def is_decision_candidate(width: int, height: int, min_px: int) -> bool:
    """判定候选谓词——与 :func:`netsentinel.decision.verdict.assess` 的
    候选过滤("宽或高 >= min_image_px",verdict.py 单遍循环里的原式)
    逐式一致;一致性由 tests/test_send_gate.py 的网格对账测试锁定
    (vision 层不得 import decision,故靠测试对账而非共享导入)。

    宽或高任一 >= min_px 即候选;两边都 < min_px(含 0x0 未测得)
    不是候选。
    """
    return width >= min_px or height >= min_px


@dataclass
class SendGateStats:
    """一次闸门的记账(只含名称与数字,不含 URL/路径内容)。"""

    collected: int = 0        # 采集图片总数(闸门入口)
    sent: int = 0             # 实际送审数(闸门出口)
    skipped_small: int = 0    # 拦下:已测得且两边 < min_image_px
    skipped_duplicate: int = 0  # 拦下:同内容(sha256/path)重复
    skipped_neardup: int = 0    # 拦下:同站近重复(phash 汉明距 ≤ 阈值,P1-4)
    skipped_unmeasured: int = 0  # 拦下:宽/高未测得(仅显式开启时)
    kept_unmeasured: int = 0   # 放行:宽/高未测得(fail-open 缺省路径)

    @property
    def saved_calls(self) -> int:
        """本次闸门省下的送审调用数 = 采集 − 送审。"""
        return self.collected - self.sent

    def as_dict(self) -> dict[str, int]:
        return {
            "collected": self.collected,
            "sent": self.sent,
            "skipped_small": self.skipped_small,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_neardup": self.skipped_neardup,
            "skipped_unmeasured": self.skipped_unmeasured,
            "kept_unmeasured": self.kept_unmeasured,
            "saved_calls": self.saved_calls,
        }


class SendGate:
    """有状态送审闸门(P1-3 流水线化):跨批次共享去重键与记账。

    流水线把整站图片按页分块喂入(``feed``),首现去重与统计跨块累计——
    结果与一次性喂入全量(:func:`apply_send_gate`)完全一致(顺序保持
    全局首现序)。每次 ``feed`` 不打日志(由调用方在收尾统一播报),
    遥测计数逐条照记。

    用法::

        gate = SendGate(cfg)          # cfg.vlm_send_gate 为假时 enabled=False
        for chunk in page_chunks:
            send_chunk = gate.feed(chunk)
        stats = gate.stats
    """

    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(getattr(cfg, "vlm_send_gate", True))
        self.min_px = int(getattr(cfg, "min_image_px", 200))
        self.drop_unmeasured = bool(
            getattr(cfg, "vlm_send_gate_drop_unmeasured", False)
        )
        # P1-4 同站近重复闸(phash):仅对拟送审图计算 64bit 感知哈希
        # (intel.phash,DCT 路径 P50 1.27ms/张——比一次 VLM 便宜 ~3600×),
        # 与**已送审**指纹比对,汉明距 ≤ 阈值(缺省 4/64bit,业界保守
        # 近重复线)拦下复用代表图。仅同站生效(gate 实例即站点边界),
        # 跨站共享需缓存键升级(P1-4 明确不做)。指纹失败一律 fail-open
        # 放行——绝不因哈希失败丢证据。
        self.neardup = bool(getattr(cfg, "vlm_neardup_dedup", True))
        try:
            self.neardup_threshold = max(
                0, int(getattr(cfg, "vlm_neardup_threshold_bits", 4))
            )
        except (TypeError, ValueError):
            self.neardup_threshold = 4
        self._sent_hashes: list[str] = []
        self._phash_fn: Any = None
        self.stats = SendGateStats()
        self._seen_keys: set[str] = set()

    def _phash_of(self, img: ImageEvidence) -> str | None:
        """计算图片 phash;模块缺席/文件损坏/任何异常 → None(fail-open)。"""
        if self._phash_fn is None:
            try:
                from netsentinel.intel.phash import phash as _phash

                self._phash_fn = _phash
            except Exception as exc:  # noqa: BLE001 - 兄弟模块缺席按无指纹
                logger.debug("近重复闸:intel.phash 不可用,fail-open 放行:%s", exc)
                self._phash_fn = False  # 哨兵:已尝试且不可用
                return None
        if self._phash_fn is False:
            return None
        try:
            return str(self._phash_fn(str(img.path)))
        except Exception:  # noqa: BLE001 - 文件缺失/损坏/解码失败
            telemetry.inc("sendgate.neardup.hash_failed")
            return None

    def _admit(self, img: ImageEvidence) -> bool:
        """近重复闸:拟送审图与已送审指纹比对;拦下返回 False。

        汉明距内联计算(``(int(a,16)^int(b,16)).bit_count()``,与
        ``intel.phash.hamming`` 同式)——不依赖 _phash_of 的惰性导入
        副作用,测试注入指纹时同样可用。
        """
        if not self.neardup:
            return True
        digest = self._phash_of(img)
        if digest is None:
            return True  # fail-open:无指纹不拦
        threshold = self.neardup_threshold
        for sent in self._sent_hashes:
            if (int(digest, 16) ^ int(sent, 16)).bit_count() <= threshold:
                return False
        self._sent_hashes.append(digest)
        return True

    def feed(self, imgs: list[ImageEvidence]) -> list[ImageEvidence]:
        """喂入一批(页)图片,返回本批放行项(全局首现序);记账累计。"""
        if not self.enabled:
            self.stats.collected += len(imgs)
            self.stats.sent += len(imgs)
            return list(imgs)
        send: list[ImageEvidence] = []
        self.stats.collected += len(imgs)
        for img in imgs:
            key = str(img.sha256) if img.sha256 else f"path:{img.path}"
            if key in self._seen_keys:
                self.stats.skipped_duplicate += 1
                telemetry.inc("sendgate.skipped.duplicate")
                continue
            self._seen_keys.add(key)

            w, h = int(img.width or 0), int(img.height or 0)
            admit: bool
            if is_decision_candidate(w, h, self.min_px):
                admit = True  # 判定参与者:过近重复闸后送审
            elif w > 0 and h > 0:
                self.stats.skipped_small += 1
                telemetry.inc("sendgate.skipped.small")
                continue
            elif self.drop_unmeasured:
                self.stats.skipped_unmeasured += 1  # 未测得且非候选:显式开启才拦
                telemetry.inc("sendgate.skipped.unmeasured")
                continue
            else:
                self.stats.kept_unmeasured += 1  # 缺省 fail-open:放行不丢证据
                telemetry.inc("sendgate.kept.unmeasured")
                admit = True
            if admit and not self._admit(img):
                self.stats.skipped_neardup += 1
                telemetry.inc("sendgate.skipped.neardup")
                continue
            send.append(img)
        self.stats.sent += len(send)
        telemetry.inc("sendgate.passed", float(len(send)))
        return send


def apply_send_gate(
    imgs: list[ImageEvidence], cfg: Config
) -> tuple[list[ImageEvidence], SendGateStats | None]:
    """对采集图片施加送审闸门,返回(送审清单, 记账)。

    判定口径(与 :func:`netsentinel.decision.verdict.assess` 候选过滤
    逐式一致,机器对账测试锁定):

    - **候选图**(宽或高 >= min_image_px;含"部分未测得"如 0×500——
      h 已达标,assess 今日就把它当候选)→ 一律送审,任何模式都不拦;
    - **非候选且已测得**(两边都 < min_image_px)→ 拦下(小图);
    - **非候选且未测得**(宽或高 <= 0 且已测得一边也不达标,如 0×0 /
      0×100)→ 缺省 **fail-open 放行**(绝不因测不出尺寸丢证据);
      显式开启 ``cfg.vlm_send_gate_drop_unmeasured``(缺省 False)才拦。
      assess 对这类图同样不采信(0 < min_px),拦与不拦都不动判定。

    其他语义:``cfg.vlm_send_gate`` 为假 → 原清单原样返回、记账 None
    (逐字节旧行为);顺序保持首现序;去重键 = sha256(缺失回退 path)。

    :param imgs: 采集并增强(宽高已嗅探)后的图片证据。
    :param cfg: 配置(读 ``min_image_px`` / ``vlm_send_gate`` /
        ``vlm_send_gate_drop_unmeasured`` 附加属性)。
    :return: (送审清单, 记账;关闭时 (原清单, None))。
    """
    gate = SendGate(cfg)
    if not gate.enabled:
        return imgs, None
    send = gate.feed(imgs)
    stats = gate.stats
    if stats.saved_calls > 0:
        logger.info(
            "送审闸门:采集 %d 张 / 送审 %d 张(拦下小图 %d + 重复 %d"
            "+ 近重复 %d + 未测得 %d,放行未测得 %d;省 %d 次调用,判定口径不变)",
            stats.collected,
            stats.sent,
            stats.skipped_small,
            stats.skipped_duplicate,
            stats.skipped_neardup,
            stats.skipped_unmeasured,
            stats.kept_unmeasured,
            stats.saved_calls,
        )
        return send, stats
    return send, stats
