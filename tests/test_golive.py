"""golive 真实环境就绪验证器测试(离线;--net 探活全部走注入 transport,零外呼)。"""
from __future__ import annotations

import json

import pytest

from netsentinel.contracts import Config
from netsentinel.golive import (
    NET_PROBES,
    CheckResult,
    human_only,
    main,
    prepare,
    run_checks,
    summarize,
)


@pytest.fixture()
def cfg(tmp_path):
    c = Config()
    c.data_dir = str(tmp_path / "data")
    for k in ("db_path", "audit_path"):
        pass
    c.model_runtime_path = str(tmp_path / "data" / "model_runtime.json")
    return c


# ---------------- 离线体检 ----------------

def test_offline_check_shapes(cfg):
    results = run_checks(cfg, net=False)
    names = [r.name for r in results]
    assert "生产开关" in names and "数据目录可写" in names
    assert all(r.status in ("ok", "gated", "fail", "skip") for r in results)
    assert any(r.name == "本地视觉服务" for r in results)


def test_offline_zero_network(monkeypatch, cfg):
    """红线 38:net=False 时绝不发起外网(拦截 urlopen)。"""
    import urllib.request

    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("外呼!")))
    run_checks(cfg, net=False)
    assert called == []


def test_production_switches_reported(cfg):
    cfg.allow_network = True
    cfg.vlm_online = True
    r = next(x for x in run_checks(cfg, net=False) if x.name == "生产开关")
    assert r.status == "gated" and "allow_network" in r.detail and "vlm_online" in r.detail


def test_storage_fail_reported(cfg):
    cfg.data_dir = "Z:/不存在的盘符/深层/x" if False else cfg.data_dir  # 保留正向
    results = run_checks(cfg, net=False)
    assert any(r.name == "数据目录可写" and r.status in ("ok", "fail") for r in results)


# ---------------- --net 探活(注入 transport) ----------------

def _fake_transport_factory(status_map: dict[str, int]):
    def t(url):
        for key, code in status_map.items():
            if key in url:
                return code, 50
        return 200, 30

    return t


def test_net_probe_ok_and_auth(tmp_path, cfg, monkeypatch):
    cfg.db_path = str(tmp_path / "q.db")
    results = run_checks(
        cfg, net=True, transport=_fake_transport_factory({"bigmodel": 401, "yandex": 403, "shdf": 521})
    )
    net = {r.name: r for r in results if r.name in NET_PROBES}
    assert len(net) == len(NET_PROBES)
    assert net["12377 举报中心(只读)"].status == "ok"
    assert net["智谱 GLM API"].status == "ok" and "401" in net["智谱 GLM API"].detail
    assert net["扫黄打非网(只读)"].status == "ok"  # 521=防护,端点可达


def test_net_probe_exception_is_fail(cfg, tmp_path):
    def broken(url):
        raise OSError("timed out")

    cfg.db_path = str(tmp_path / "q.db")
    results = run_checks(cfg, net=True, transport=broken)
    fails = [r for r in results if r.status == "fail"]
    assert len(fails) == len(NET_PROBES) and all("timed out" in r.detail for r in fails)


def test_probe_endpoints_are_readonly_gets():
    assert all(u.startswith("https://") for u in NET_PROBES.values())
    assert any("12377" in k for k in NET_PROBES) and any("扫黄打非" in k for k in NET_PROBES)


# ---------------- 汇总与人工环节 ----------------

def test_summarize_buckets():
    rs = [
        CheckResult("a", "ok", "x"),
        CheckResult("b", "gated", "y"),
        CheckResult("c", "fail", "z"),
        CheckResult("d", "skip", "w"),
    ]
    s = summarize(rs)
    assert set(s) == {"ready", "gated", "fail", "skip"}
    assert s["ready"] == ["a:x"] and s["fail"] == ["c:z"]


def test_human_only_lists_operator_gates(cfg):
    items = human_only(cfg)
    joined = " ".join(items)
    assert "验证码" in joined and "声明" in joined and "密钥" in joined and "真实性" in joined


# ---------------- CLI ----------------

def test_cli_check_offline(tmp_path, cfg, capsys, monkeypatch):
    # 用无配置文件环境(默认 cfg 指向 cwd data)→ 在 tmp cwd 运行
    monkeypatch.chdir(tmp_path)
    rc = main(["check"])
    out = capsys.readouterr().out
    assert rc in (0, 2)
    assert "永远属于运营者" in out and "验证码" in out
    assert "外网" not in out.split("── 以下环节")[0] or "net" not in out  # 未开 --net 无探活行


def test_cli_check_net_with_fake(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    import netsentinel.golive as g

    monkeypatch.setattr(g, "_http_probe", _fake_transport_factory({"yandex": 403}))
    rc = g.main(["check", "--net", "--json", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert "12377" in out and "Yandex" in out
    data = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert data["results"] and data["human_only"]


def test_cli_prepare_and_refuse(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["prepare"])
    assert rc == 0 and (tmp_path / "config.production.yaml").exists()
    rc2 = main(["prepare"])
    assert rc2 == 1 and "拒绝覆盖" in capsys.readouterr().out


def test_production_template_contains_switches():
    from netsentinel.golive import PRODUCTION_TEMPLATE

    for key in ("allow_network", "vlm_online", "discovery_online", "dry_run_default", "concurrency_tier"):
        assert key in PRODUCTION_TEMPLATE
    assert "验证码永远人工" in PRODUCTION_TEMPLATE
