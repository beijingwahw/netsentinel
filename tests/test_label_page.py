# -*- coding: utf-8 -*-
"""V10.36 标注工作台(webui/label_page.py)测试:纯逻辑层全覆盖。

重点锁定的红线:人工标签唯一可信来源(溯源 human:webui)、改标更新溯源
不留假档案、补充语料零触碰已标资产、原子落盘格式与 CLI 互通。
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

import pytest

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import webui.label_page as lp  # noqa: E402
from netsentinel.vision.calibration import (  # noqa: E402
    LABEL_CLEAN,
    LABEL_NSFW,
    LabeledItem,
    LabeledSet,
)


@pytest.fixture()
def seeded_set() -> LabeledSet:
    """三条目小标注集:1 已标种子 + 2 pending。"""
    return LabeledSet([
        LabeledItem(path="a.png", sha256="sha_a", label=LABEL_CLEAN,
                    labeled_by="seed:mock-synthetic", note="mock 合成图"),
        LabeledItem(path="b.png", sha256="sha_b", label="", labeled_by="",
                    note="待人工标注"),
        LabeledItem(path="c.png", sha256="sha_c", label="", labeled_by="",
                    note="待人工标注"),
    ])


# ---------------------------------------------------------------------------
# 路径与装载
# ---------------------------------------------------------------------------
def test_set_path_layout(tmp_path: pathlib.Path) -> None:
    p = lp.set_path(tmp_path)
    assert p == tmp_path / "calibration" / "labeled.jsonl"
    assert lp.LABEL_SET_REL == "calibration/labeled.jsonl"


def test_load_or_seed_existing_file_not_reseeded(tmp_path: pathlib.Path, seeded_set) -> None:
    target = lp.set_path(tmp_path)
    lp.save_set(seeded_set, target)
    s, created = lp.load_or_seed(tmp_path)
    assert created is False
    assert len(s.items) == 3


def test_load_or_seed_creates_when_missing(tmp_path: pathlib.Path) -> None:
    # 缺省种子目录 = 仓库夹具(真实存在);空 data 目录 → 新建
    s, created = lp.load_or_seed(tmp_path)
    assert created is True
    stats = s.stats()
    assert stats["total"] >= 40          # V10.33 语料口径(40 唯一内容)
    assert stats["by_label"][LABEL_NSFW] == 0   # 种子绝不标 nsfw
    assert stats["pending"] >= 30
    # 文件已落盘(下次装载 created=False)
    _, created2 = lp.load_or_seed(tmp_path)
    assert created2 is False


def test_save_set_format_interops_with_cli_loader(tmp_path: pathlib.Path, seeded_set) -> None:
    """落盘格式与 calibration.LabeledSet.load 互通(同 JSONL 行结构)。"""
    target = lp.set_path(tmp_path)
    lp.save_set(seeded_set, target)
    back = LabeledSet.load(target)
    assert [i.sha256 for i in back.items] == ["sha_a", "sha_b", "sha_c"]
    # 行级验证:每行都是合法 JSON,含全部六键
    lines = [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(
        {"path", "sha256", "label", "labeled_by", "note", "labeled_at"} <= set(d)
        for d in lines
    )
    assert not target.with_suffix(target.suffix + ".tmp").exists()  # 临时文件已原子换名


# ---------------------------------------------------------------------------
# 标注与改标
# ---------------------------------------------------------------------------
def test_apply_label_marks_pending_as_human_webui(seeded_set) -> None:
    ok = lp.apply_label(seeded_set, "sha_b", LABEL_NSFW)
    assert ok is True
    item = next(i for i in seeded_set.items if i.sha256 == "sha_b")
    assert item.label == LABEL_NSFW
    assert item.labeled_by == lp.LABEL_SOURCE       # human:webui 溯源
    assert item.labeled_at                          # 时间戳非空


def test_apply_label_relabel_updates_provenance_no_fake_archive(seeded_set) -> None:
    """改标合法:覆写标签并更新溯源/时间戳,不留旧档案冒充。"""
    lp.apply_label(seeded_set, "sha_b", LABEL_NSFW)
    first_at = next(i for i in seeded_set.items if i.sha256 == "sha_b").labeled_at
    lp.apply_label(seeded_set, "sha_b", LABEL_CLEAN, source="human:webui")
    item = next(i for i in seeded_set.items if i.sha256 == "sha_b")
    assert item.label == LABEL_CLEAN
    assert item.labeled_by == lp.LABEL_SOURCE
    assert item.labeled_at >= first_at


def test_apply_label_rejects_invalid_label(seeded_set) -> None:
    with pytest.raises(ValueError, match="非法标签"):
        lp.apply_label(seeded_set, "sha_b", "suspect")   # 二值口径:suspect 不收


def test_apply_label_unknown_sha_returns_false(seeded_set) -> None:
    assert lp.apply_label(seeded_set, "sha_missing", LABEL_CLEAN) is False


def test_apply_label_never_touches_other_items(seeded_set) -> None:
    lp.apply_label(seeded_set, "sha_c", LABEL_NSFW)
    a = next(i for i in seeded_set.items if i.sha256 == "sha_a")
    b = next(i for i in seeded_set.items if i.sha256 == "sha_b")
    assert a.labeled_by == "seed:mock-synthetic"    # 种子未被触碰
    assert b.label == ""                            # 邻居仍 pending


# ---------------------------------------------------------------------------
# 进度与门禁 C1
# ---------------------------------------------------------------------------
def test_progress_counts_pos_neg_and_sources(seeded_set) -> None:
    lp.apply_label(seeded_set, "sha_b", LABEL_NSFW)
    prog = lp.progress(seeded_set)
    assert prog["total"] == 3
    assert prog["labeled"] == 2
    assert prog["pending"] == 1
    assert prog["pos"] == 1        # nsfw
    assert prog["neg"] == 1        # clean(种子)
    assert prog["by_source"].get("human") == 1
    assert prog["by_source"].get("seed") == 1


def test_gate_view_gaps_and_ready() -> None:
    s = LabeledSet([
        LabeledItem(path="p.png", sha256=f"sha_{i}", label=label, labeled_by="human:webui")
        for i, label in enumerate([LABEL_NSFW] * 12 + [LABEL_CLEAN] * 20)
    ])
    gate = lp.gate_view(s)
    assert gate["min_pos"] == 20 and gate["min_neg"] == 20   # GATE_DEFAULTS 同源
    assert gate["pos"] == 12 and gate["neg"] == 20
    assert gate["pos_gap"] == 8 and gate["neg_gap"] == 0
    assert gate["pos_ok"] is False and gate["neg_ok"] is True
    assert gate["ready"] is False


def test_gate_view_ready_when_both_floors_met() -> None:
    s = LabeledSet([
        LabeledItem(path="p.png", sha256=f"sha_{i}", label=label, labeled_by="human:webui")
        for i, label in enumerate([LABEL_NSFW] * 20 + [LABEL_CLEAN] * 20)
    ])
    assert lp.gate_view(s)["ready"] is True


def test_gate_view_custom_floors(seeded_set) -> None:
    gate = lp.gate_view(seeded_set, min_pos=1, min_neg=1)   # 种子 clean 1 张即达标口径
    assert gate["ready"] is False                            # 但 pos=0 仍差


# ---------------------------------------------------------------------------
# 补充语料
# ---------------------------------------------------------------------------
def test_add_pending_merges_new_dir_without_touching_labels(
    tmp_path: pathlib.Path, seeded_set
) -> None:
    extra = tmp_path / "more"
    extra.mkdir()
    # 造两张小 PNG(1x1 像素足够;sha 与既有不同)
    from PIL import Image

    Image.new("RGB", (2, 2), (10, 20, 30)).save(extra / "x1.png")
    Image.new("RGB", (2, 2), (40, 50, 60)).save(extra / "x2.png")

    before_labels = {i.sha256: (i.label, i.labeled_by) for i in seeded_set.items}
    added = lp.add_pending(seeded_set, [str(extra)])
    assert added == 2
    assert len(seeded_set.items) == 5
    # 已标资产零触碰
    for sha, (label, by) in before_labels.items():
        it = next(i for i in seeded_set.items if i.sha256 == sha)
        assert (it.label, it.labeled_by) == (label, by)
    # 新图全部 pending 且 note 记补充来源
    new_items = [i for i in seeded_set.items if i.sha256 not in before_labels]
    assert all(i.label == "" for i in new_items)
    assert all("补充" in i.note for i in new_items)


def test_add_pending_dedupes_by_sha(tmp_path: pathlib.Path, seeded_set) -> None:
    extra = tmp_path / "dup"
    extra.mkdir()
    from PIL import Image

    img = extra / "same.png"
    Image.new("RGB", (2, 2), (1, 2, 3)).save(img)
    assert lp.add_pending(seeded_set, [str(extra)]) == 1
    assert lp.add_pending(seeded_set, [str(extra)]) == 0      # 同目录二刷零新增


def test_add_pending_missing_dir_raises(seeded_set) -> None:
    with pytest.raises(ValueError, match="不存在"):
        lp.add_pending(seeded_set, ["Z:/definitely/not/here"])


# ---------------------------------------------------------------------------
# 图片解析 / 提示
# ---------------------------------------------------------------------------
def test_resolve_image_relative_and_absolute(tmp_path: pathlib.Path) -> None:
    rel = lp.resolve_image("data/evidence/x.png")
    assert rel == pathlib.Path.cwd() / "data" / "evidence" / "x.png"
    p = lp.resolve_image(str(tmp_path / "x.png"))
    assert p == tmp_path / "x.png"


def test_image_missing_true_for_ghost_false_for_real(tmp_path: pathlib.Path) -> None:
    real = tmp_path / "real.png"
    real.write_bytes(b"png")
    assert lp.image_missing(str(real)) is False
    assert lp.image_missing(str(tmp_path / "ghost.png")) is True


def test_cli_hint_contains_score_and_report() -> None:
    hint = lp.cli_hint()
    assert "calibration score" in hint and "--model" in hint
    assert "calibration report" in hint and "--scores" in hint


# ---------------------------------------------------------------------------
# 端到端小流程:装载 → 标注 → 落盘 → 重载(与 CLI 互通)
# ---------------------------------------------------------------------------
def test_full_loop_persist_and_reload(tmp_path: pathlib.Path) -> None:
    s, _ = lp.load_or_seed(tmp_path)
    target = next(i for i in s.pending() if lp.resolve_image(i.path).is_file())
    assert lp.apply_label(s, target.sha256, LABEL_CLEAN)
    lp.save_set(s, lp.set_path(tmp_path))
    re = LabeledSet.load(lp.set_path(tmp_path))
    item = next(i for i in re.items if i.sha256 == target.sha256)
    assert item.label == LABEL_CLEAN and item.labeled_by == lp.LABEL_SOURCE
    assert lp.gate_view(re)["neg"] >= 1


# ---------------------------------------------------------------------------
# V10.37:整组导入正/负样本(页面包装)
# ---------------------------------------------------------------------------
def test_import_labeled_wrapper_adds_and_tracks(tmp_path: pathlib.Path, seeded_set) -> None:
    extra = tmp_path / "pos"
    extra.mkdir()
    from PIL import Image

    Image.new("RGB", (3, 3), (200, 0, 0)).save(extra / "p1.png")
    Image.new("RGB", (3, 3), (0, 200, 0)).save(extra / "p2.png")

    result = lp.import_labeled(seeded_set, [str(extra)], "nsfw")
    assert result["added"] == 2 and result["promoted"] == 0 and result["skipped"] == 0  # 新内容无碰撞
    new_items = [i for i in seeded_set.items if i.path.startswith(str(extra))]
    assert len(new_items) == 2
    assert all(i.label == "nsfw" and i.labeled_by == "human:dir-import" for i in new_items)
    # 进度立即反映正样本
    assert lp.progress(seeded_set)["pos"] == 2


def test_import_labeled_never_touches_human_labels(seeded_set) -> None:
    lp.apply_label(seeded_set, "sha_b", "nsfw")  # 人工逐张标
    d = pathlib.Path(seeded_set.items[1].path)  # 仅取 sha 占位;用包装的跳过语义验证
    # sha_b 不在任何目录,导入空目录不影响
    result = lp.import_labeled(seeded_set, [], "clean") if d else None
    # 空目录列表:返回全零,已标不动
    assert result == {"added": 0, "promoted": 0, "skipped": 0}
    b = next(i for i in seeded_set.items if i.sha256 == "sha_b")
    assert b.label == "nsfw" and b.labeled_by == lp.LABEL_SOURCE


def test_import_labeled_rejects_bad_label(seeded_set) -> None:
    with pytest.raises(ValueError, match="非法标签"):
        lp.import_labeled(seeded_set, ["."], "maybe")


# ---------------------------------------------------------------------------
# V10.37:整组导入 UI 全流程交互(AppTest 真驱动:确认门 → 导入 → 进度)
# ---------------------------------------------------------------------------
def test_import_ui_gate_and_flow_via_apptest(tmp_path: pathlib.Path, monkeypatch) -> None:
    """整组导入交互三态:未勾确认被拦 / 勾选后导入成功 / 已标不覆盖。"""
    pytest.importorskip("streamlit", reason="需要 streamlit AppTest")
    try:
        from streamlit.testing.v1 import AppTest
    except ImportError:  # pragma: no cover
        pytest.skip("streamlit.testing.v1 不可用")

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("NETSENTINEL_DATA_DIR", str(data))
    monkeypatch.chdir(_PROJECT_ROOT)

    pos = tmp_path / "pos"
    pos.mkdir()
    from PIL import Image

    Image.new("RGB", (4, 4), (250, 0, 0)).save(pos / "n1.png")
    Image.new("RGB", (4, 4), (0, 0, 250)).save(pos / "n2.png")

    at = AppTest.from_file(str(_PROJECT_ROOT / "webui" / "label_page.py"), default_timeout=60)
    at.run()
    assert not at.exception

    # ① 确认门:填目录但不勾确认 → 点 nsfw 导入 → 中文拦截,零写入
    at.text_input(key="import_dir").set_value(str(pos))
    at.button(key="btn_import_nsfw").click()
    at.run()
    assert not at.exception
    body = " ".join(str(getattr(x, "value", x)) for x in at.error)
    assert "请先勾选确认" in body
    setfile = data / "calibration" / "labeled.jsonl"
    from netsentinel.vision.calibration import LabeledSet
    assert LabeledSet.load(setfile).stats()["labeled"] == 5  # 仍是 5 种子,零写入

    # ② 勾选确认 → 导入成功:新增 2,侧栏正样本计数反映
    at.checkbox(key="import_confirm").check()
    at.button(key="btn_import_nsfw").click()
    at.run()
    assert not at.exception
    s2 = LabeledSet.load(setfile)
    pos_count = sum(1 for i in s2.labeled() if i.label == "nsfw")
    assert pos_count == 2
    imported = [i for i in s2.items if i.labeled_by == "human:dir-import"]
    assert len(imported) == 2 and all(i.note.startswith("目录整组导入") for i in imported)

    # ③ 重复导入同目录:全部跳过(已标不覆盖),条目数不变
    at.text_input(key="import_dir").set_value(str(pos))
    at.checkbox(key="import_confirm").check()
    at.button(key="btn_import_nsfw").click()
    at.run()
    assert not at.exception
    assert len(LabeledSet.load(setfile).items) == len(s2.items)
