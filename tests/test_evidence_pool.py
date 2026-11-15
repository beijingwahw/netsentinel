# -*- coding: utf-8 -*-
"""证据截图池 + 执行器自动上传测试(V10.16)—— 全离线。"""
from __future__ import annotations

import json
from pathlib import Path

from netsentinel.contracts import (
    ExecutionResult,
    Portal,
    Step,
    StepAction,
    SubmissionPayload,
    SubmissionPlan,
)
from netsentinel.submit import evidence_pool
from netsentinel.submit.executor_playwright import _dry_run, _run_plan_steps


def _make_bundle(tmp_path: Path, n_scored: int = 0, n_shots: int = 0) -> Path:
    """构造证据包目录:manifest.json + 若干评分图与整页截图文件。"""
    d = tmp_path / "bundle"
    d.mkdir(parents=True, exist_ok=True)
    scores = []
    for i in range(n_scored):
        name = f"img_{i:02d}.png"
        (d / name).write_bytes(b"\x89PNG")
        scores.append(
            {"image": f"/abs/old/{name}", "model": "ensemble", "nsfw_prob": round(0.3 + i * 0.05, 4)}
        )
    pages = []
    for i in range(n_shots):
        name = f"shot_{i}.png"
        (d / name).write_bytes(b"\x89PNG")
        pages.append({"url": f"https://x/{i}", "screenshot_path": f"/abs/old/{name}", "images": []})
    (d / "manifest.json").write_text(
        json.dumps({"report": {"image_scores": scores, "pages": pages}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return d


def test_select_pool_ranked_and_capped(tmp_path):
    """ensemble 分降序取 Top-10(12 张评分图只取前 10,低分在后被截)。"""
    d = _make_bundle(tmp_path, n_scored=12)
    pool = evidence_pool.select_pool(str(tmp_path / "bundle.zip"))
    assert len(pool) == 10
    # 降序:首名是最高分 img_11,末名是第 10 高分 img_02
    assert pool[0].endswith("img_11.png") and pool[-1].endswith("img_02.png")
    # 预留留痕落盘(幂等可重算)
    saved = json.loads((d / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["reserved"] == pool and saved["quota"]["12377"] == 10


def test_screenshot_fallback_and_quota(tmp_path):
    """评分图不足时整页截图补足;门户配额 12377=10 / shdf=5。"""
    _make_bundle(tmp_path, n_scored=3, n_shots=4)  # 池共 7 张
    zip_path = str(tmp_path / "bundle.zip")
    assert len(evidence_pool.select_pool(zip_path)) == 7
    assert len(evidence_pool.files_for_portal(zip_path, Portal.P12377)) == 7  # 池不足配额全给
    assert len(evidence_pool.files_for_portal(zip_path, Portal.SHDF)) == 5   # shdf 截到 5
    d = tmp_path / "bundle"
    for i in range(5):  # 目录塞入 manifest 之外的文件
        (d / f"extra_{i}.png").write_bytes(b"\x89PNG")
    pool10 = evidence_pool.select_pool(zip_path)
    assert len(pool10) == 7  # select_pool 只认 manifest 内文件,extra 不入池


def test_missing_manifest_empty_pool(tmp_path):
    """manifest 缺失 → 空池(各门户零上传,不抛错)。"""
    (tmp_path / "bundle").mkdir()
    zip_path = str(tmp_path / "bundle.zip")
    assert evidence_pool.select_pool(zip_path) == []
    assert evidence_pool.files_for_portal(zip_path, Portal.P12377) == []


class _FakePage:
    """记录 set_input_files / click 的页面替身(离线,_run_plan_steps 直测)。"""

    def __init__(self):
        self.uploaded: list = []
        self.clicked: list = []

    def set_input_files(self, selector, files):
        self.uploaded.append((selector, list(files)))

    def click(self, selector, timeout=None):
        self.clicked.append(selector)

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, full_page=False):
        Path(path).write_bytes(b"\x89PNG")

    def content(self):
        return "<html></html>"


def _plan(portal: Portal, zip_path: str) -> SubmissionPlan:
    payload = SubmissionPayload(
        portal=portal,
        site_url="https://www.example.com/",
        description="x" * 40,
        evidence_zip=zip_path,
        reporter_name="t",
        reporter_phone="13800000000",
    )
    steps = [
        Step(action=StepAction.WAIT, label="等待加载", value="0.1"),
        Step(action=StepAction.HUMAN_GATE, label="人工门:核对并输入验证码"),
        Step(action=StepAction.CLICK, label="点击提交", selector="#report-submit"),
    ]
    return SubmissionPlan(portal=portal, entry_url="http://127.0.0.1/x", payload=payload, steps=steps)


def test_upload_before_human_gate_by_portal_quota(tmp_path):
    """真实模式:人工门前按门户配额自动上传(12377 十张 / shdf 五张)。"""
    _make_bundle(tmp_path, n_scored=12, n_shots=1)
    zip_path = str(tmp_path / "bundle.zip")
    out_dir = tmp_path / "runs"
    out_dir.mkdir()

    page = _FakePage()
    result = ExecutionResult(portal="12377")
    done = _run_plan_steps(
        _plan(Portal.P12377, zip_path), page=page, out_path=out_dir,
        result=result, auto_confirm=True,
    )
    assert done and result.ok
    assert len(page.uploaded) == 1 and page.uploaded[0][0] == "#report-file"
    assert len(page.uploaded[0][1]) == 10  # 12377 → 十张
    assert any("[上传]" in n and "10 张" in n for n in result.notes)

    page2 = _FakePage()
    result2 = ExecutionResult(portal="shdf")
    _run_plan_steps(
        _plan(Portal.SHDF, zip_path), page=page2, out_path=out_dir,
        result=result2, auto_confirm=True,
    )
    assert len(page2.uploaded[0][1]) == 5  # 扫黄打非 → 五张


def test_upload_missing_pool_degrades_to_note(tmp_path):
    """无证据池:只记降级 notes,不中断(后续步骤照常)。"""
    (tmp_path / "empty").mkdir()
    page = _FakePage()
    result = ExecutionResult(portal="12377")
    done = _run_plan_steps(
        _plan(Portal.P12377, str(tmp_path / "empty.zip")), page=page,
        out_path=tmp_path, result=result, auto_confirm=True,
    )
    assert done and result.ok and page.uploaded == []
    assert any("无可自动上传" in n for n in result.notes)


def test_dry_run_notes_pool_preview(tmp_path):
    """干跑:命中池时预演提示;无池零额外 notes(既有干跑断言不受扰)。"""
    _make_bundle(tmp_path, n_scored=12)
    zip_path = str(tmp_path / "bundle.zip")
    result = _dry_run(_plan(Portal.SHDF, zip_path), ExecutionResult(portal="shdf"))
    assert any("自动上传 5 张" in n for n in result.notes)

    (tmp_path / "none").mkdir()
    result2 = _dry_run(_plan(Portal.P12377, str(tmp_path / "none.zip")), ExecutionResult(portal="12377"))
    assert not any("自动上传" in n for n in result2.notes)



# ---------------------------------------------------------------------------
# V10.20 创新旗舰:证据池感知去重(pHash 近重复剔除)
# ---------------------------------------------------------------------------
def _real_image(path: Path, seed: int) -> None:
    """用 Pillow 生成 64x64 随机噪声真图(不同 seed 的 pHash 距离远超阈值)。"""
    import random

    from PIL import Image

    rnd = random.Random(seed)
    im = Image.new("RGB", (64, 64))
    im.putdata(
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(64 * 64)]
    )
    im.save(path, format="PNG")


def _make_real_bundle(
    tmp_path: Path, images: list[tuple[str, float, object]]
) -> Path:
    """构造真图证据包:images = [(文件名, nsfw_prob, seed|None=复制前一张)]。

    ``seed=None`` 表示字节级复制前一张真图(同图变体 → pHash 距离 0,
    必然近重复)。
    """
    d = tmp_path / "bundle"
    d.mkdir(parents=True, exist_ok=True)
    scores = []
    last_bytes: bytes | None = None
    for name, prob, seed in images:
        target = d / name
        if seed is None:
            assert last_bytes is not None
            target.write_bytes(last_bytes)
        else:
            _real_image(target, int(seed))
            last_bytes = target.read_bytes()
        scores.append({"image": f"/abs/old/{name}", "model": "ensemble", "nsfw_prob": prob})
    (d / "manifest.json").write_text(
        json.dumps({"report": {"image_scores": scores, "pages": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return d


def test_v1020_near_duplicate_group_keeps_highest_only(tmp_path):
    """近重复组只留 ensemble 分最高的一张;留痕记 dropped_near_duplicate。"""
    from netsentinel import telemetry

    telemetry.reset()
    _make_real_bundle(
        tmp_path,
        [
            ("img_a.png", 0.99, 101),   # 组内最高分
            ("img_b.png", 0.98, None),  # img_a 的字节复制 → 近重复,剔除
            ("img_c.png", 0.90, 202),   # 异源真图
            ("img_d.png", 0.88, None),  # 又一张 img_a 变体 → 剔除
            ("img_e.png", 0.80, 303),   # 异源真图
        ],
    )
    zip_path = str(tmp_path / "bundle.zip")
    pool = evidence_pool.select_pool(zip_path)
    assert [Path(p).name for p in pool] == ["img_a.png", "img_c.png", "img_e.png"]
    saved = json.loads((tmp_path / "bundle" / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 2
    assert saved["dedup"] is True
    assert saved["reserved"] == pool
    assert telemetry.snapshot()["counters"].get("evidence_pool.near_duplicate_dropped") == 2.0
    # 池留痕读取口:无池留痕按 0,零异常
    assert evidence_pool.pool_dropped_near_duplicate(zip_path) == 2
    assert evidence_pool.pool_dropped_near_duplicate(str(tmp_path / "none.zip")) == 0


def test_v1020_dedup_backfills_slots_with_unique_images(tmp_path):
    """名额回填:6 张同源变体只占 1 席,低分不重复图顶上。"""
    images: list[tuple[str, float, object]] = []
    for i in range(6):  # 前 6 高分全是同一张图的变体
        images.append((f"dup_{i}.png", round(0.99 - i * 0.01, 4), 101 if i == 0 else None))
    for i in range(8):  # 后 8 张异源真图
        images.append((f"uniq_{i}.png", round(0.60 - i * 0.01, 4), 500 + i))
    _make_real_bundle(tmp_path, images)
    zip_path = str(tmp_path / "bundle.zip")

    pool = evidence_pool.select_pool(zip_path)
    names = [Path(p).name for p in pool]
    # 变体只留最高分 dup_0,7 张异源真图回填(共 8 ≤ 10,候选已尽)
    assert names == ["dup_0.png"] + [f"uniq_{i}.png" for i in range(8)]
    saved = json.loads((tmp_path / "bundle" / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 5


def test_v1020_dedup_off_restores_legacy_behavior(tmp_path):
    """dedup=False 一键回旧口径:近重复照单全收(与旧 Top-N 逐字一致)。"""
    images: list[tuple[str, float, object]] = [
        ("img_a.png", 0.99, 101),
        ("img_b.png", 0.98, None),
        ("img_c.png", 0.90, 202),
    ]
    _make_real_bundle(tmp_path, images)
    zip_path = str(tmp_path / "bundle.zip")

    legacy = evidence_pool.select_pool(zip_path, dedup=False)
    assert [Path(p).name for p in legacy] == ["img_a.png", "img_b.png", "img_c.png"]
    saved = json.loads((tmp_path / "bundle" / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 0
    assert saved["dedup"] is False
    # files_for_portal 透传开关
    assert len(evidence_pool.files_for_portal(zip_path, Portal.P12377, dedup=False)) == 3


def test_v1020_phash_unavailable_degrades_to_legacy(tmp_path, monkeypatch):
    """PIL/phash 模块缺席:去重优雅关闭,整体保持旧口径(缺省 dedup=True)。"""
    _make_real_bundle(
        tmp_path,
        [("img_a.png", 0.99, 101), ("img_b.png", 0.98, None), ("img_c.png", 0.90, 202)],
    )
    zip_path = str(tmp_path / "bundle.zip")
    monkeypatch.setattr(evidence_pool, "_load_phash_module", lambda: None)

    pool = evidence_pool.select_pool(zip_path)  # dedup 缺省 True,但无哈希可用
    assert [Path(p).name for p in pool] == ["img_a.png", "img_b.png", "img_c.png"]
    saved = json.loads((tmp_path / "bundle" / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 0


def test_v1020_undecodable_image_skips_dedup_for_that_file(tmp_path):
    """单张解码失败(假 PNG 字节):该图按旧口径入池,不参与比较、不拖累整池。"""
    d = _make_bundle(tmp_path, n_scored=1)  # img_00 假字节(解码必失败)
    # 追加两张真图:一张真图 + 它的字节复制(近重复组)
    _real_image(d / "real_hi.png", 900)
    (d / "real_dup.png").write_bytes((d / "real_hi.png").read_bytes())
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["report"]["image_scores"].extend(
        [
            {"image": "/abs/old/real_hi.png", "model": "ensemble", "nsfw_prob": 0.99},
            {"image": "/abs/old/real_dup.png", "model": "ensemble", "nsfw_prob": 0.70},
        ]
    )
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    pool = evidence_pool.select_pool(str(tmp_path / "bundle.zip"))
    # 真图组(高分)剔除复制张;假字节图(0.35)无哈希 → 照旧入池垫底
    assert [Path(p).name for p in pool] == ["real_hi.png", "img_00.png"]
    saved = json.loads((d / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 1


def test_v1020_phash_dup_distance_threshold_boundary(tmp_path, monkeypatch):
    """阈值边界:汉明距离恰 6 判近重复,7 不判(对 PHASH_DUP_DISTANCE=6)。"""
    from netsentinel.intel import phash as phash_mod

    assert evidence_pool.PHASH_DUP_DISTANCE == 6
    d = _make_bundle(tmp_path, n_scored=3)  # img_00/01/02(内容无所谓,哈希注入)
    hash_map = {
        "img_00.png": "0" * 16,        # 与 img_02 的距离 7 → 保留
        "img_01.png": "0" * 14 + "f3",  # 与 img_02 的距离 6 → 剔除
        "img_02.png": "0" * 14 + "f7",  # 基准(最高分)
    }
    monkeypatch.setattr(
        phash_mod, "phash", lambda p, _m=hash_map: _m[Path(p).name]
    )

    pool = evidence_pool.select_pool(str(tmp_path / "bundle.zip"))
    # _make_bundle 分数 0.30+i*0.05:img_02 最高 → 基准;img_00 距离 7 保留;
    # img_01 距离 6 剔除
    assert [Path(p).name for p in pool] == ["img_02.png", "img_00.png"]
    saved = json.loads((d / "evidence_pool.json").read_text(encoding="utf-8"))
    assert saved["dropped_near_duplicate"] == 1


def test_v1020_portal_quota_slices_deduped_pool(tmp_path):
    """门户配额切片发生在去重后的池上:shdf 五张全不重复。"""
    images: list[tuple[str, float, object]] = []
    for i in range(4):  # 4 组近重复(各 2 张),分数从高到低
        images.append((f"g{i}_hi.png", round(0.95 - i * 0.05, 4), 700 + i))
        images.append((f"g{i}_lo.png", round(0.94 - i * 0.05, 4), None))
    for i in range(6):  # 6 张异源真图垫底
        images.append((f"u{i}.png", round(0.30 - i * 0.01, 4), 800 + i))
    _make_real_bundle(tmp_path, images)
    zip_path = str(tmp_path / "bundle.zip")

    assert len(evidence_pool.select_pool(zip_path)) == 10  # Top-10 仍满
    shdf = evidence_pool.files_for_portal(zip_path, Portal.SHDF)
    assert len(shdf) == 5
    names = [Path(p).name for p in shdf]
    assert names == ["g0_hi.png", "g1_hi.png", "g2_hi.png", "g3_hi.png", "u0.png"]
    assert not (set(names) & {f"g{i}_lo.png" for i in range(4)})  # 变体绝不入前五
    assert len(evidence_pool.files_for_portal(zip_path, Portal.P12377)) == 10


def test_v1020_upload_notes_mention_dedup_drop(tmp_path):
    """真实模式人工门前:有剔除时 notes 提示"已按感知去重剔除 N 张"。"""
    _make_real_bundle(
        tmp_path,
        [
            ("img_a.png", 0.99, 101),
            ("img_b.png", 0.98, None),
            ("img_c.png", 0.90, 202),
        ],
    )
    zip_path = str(tmp_path / "bundle.zip")
    out_dir = tmp_path / "runs"
    out_dir.mkdir()

    page = _FakePage()
    result = ExecutionResult(portal="12377")
    done = _run_plan_steps(
        _plan(Portal.P12377, zip_path), page=page, out_path=out_dir,
        result=result, auto_confirm=True,
    )
    assert done and result.ok
    assert len(page.uploaded[0][1]) == 2  # 剔除 1 张后池剩 2 张
    assert any("已按感知去重剔除 1 张近重复" in n for n in result.notes)

    # 干跑预演同样提示
    dry = _dry_run(_plan(Portal.SHDF, zip_path), ExecutionResult(portal="shdf"))
    assert any("已按感知去重剔除 1 张近重复" in n for n in dry.notes)


def test_v1020_upload_notes_no_dedup_drop_when_clean(tmp_path):
    """无近重复时零额外 notes(既有断言口径不受扰)。"""
    _make_real_bundle(
        tmp_path, [("img_a.png", 0.99, 101), ("img_c.png", 0.90, 202)]
    )
    zip_path = str(tmp_path / "bundle.zip")
    dry = _dry_run(_plan(Portal.P12377, zip_path), ExecutionResult(portal="12377"))
    assert any("自动上传 2 张" in n for n in dry.notes)
    assert not any("感知去重" in n for n in dry.notes)
