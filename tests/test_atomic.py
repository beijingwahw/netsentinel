# -*- coding: utf-8 -*-
"""atomic(跨平台原子替换微工具)单元测试 —— 全离线,零真实重试等待。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from netsentinel.atomic import replace_with_retry


def test_success_first_try_no_sleep(tmp_path):
    """成功路径:一次 os.replace 即返回,sleep 从未被调用。"""
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    sleeps: list[float] = []
    replace_with_retry(src, dst, sleep=sleeps.append)
    assert dst.read_text(encoding="utf-8") == "{}"
    assert not src.exists()
    assert sleeps == []


def test_retries_then_success_on_shared_violation(tmp_path, monkeypatch):
    """Windows 共享冲突语义:PermissionError 两次后第三次成功(重试生效)。"""
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("x", encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("目标文件被其他进程占用")
        real_replace(a, b)

    monkeypatch.setattr(os, "replace", flaky_replace)
    sleeps: list[float] = []
    replace_with_retry(src, dst, sleep=sleeps.append)
    assert calls["n"] == 3
    assert sleeps == [0.05, 0.05]
    assert dst.read_text(encoding="utf-8") == "x"


def test_exhaustion_raises_last_permission_error(tmp_path, monkeypatch):
    """重试耗尽:原样抛出最后的 PermissionError(绝不静默丢状态)。"""
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("x", encoding="utf-8")

    def always_busy(a, b):
        raise PermissionError("一直被占用")

    monkeypatch.setattr(os, "replace", always_busy)
    with pytest.raises(PermissionError):
        replace_with_retry(src, dst, attempts=3, wait_s=0.0, sleep=lambda s: None)
    assert src.exists()  # 源文件保留,调用方负责清理


def test_other_oserror_no_retry(tmp_path, monkeypatch):
    """非 PermissionError(如磁盘满/路径非法):首错即抛,不重试。"""
    src = tmp_path / "a.tmp"
    calls = {"n": 0}

    def failing(a, b):
        calls["n"] += 1
        raise OSError("invalid path")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        replace_with_retry(str(src), str(tmp_path / "b"), sleep=lambda s: None)
    assert calls["n"] == 1


def test_accepts_str_and_path(tmp_path):
    """str 与 Path 入参均可(与各调用点的既有形态兼容)。"""
    src = tmp_path / "s.json.tmp"
    src.write_text("1", encoding="utf-8")
    replace_with_retry(str(src), str(tmp_path / "d1.json"), sleep=lambda s: None)
    src2 = tmp_path / "s2.json.tmp"
    src2.write_text("2", encoding="utf-8")
    replace_with_retry(src2, Path(tmp_path / "d2.json"), sleep=lambda s: None)
    assert (tmp_path / "d1.json").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "d2.json").read_text(encoding="utf-8") == "2"
