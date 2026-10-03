"""V10 测试:举报理由自动生成(≤39 字)+ 个人信息模板(V10.3 永久固化)。"""
from __future__ import annotations

import io
import pathlib
import types

import pytest

from netsentinel.contracts import Config, Portal, Verdict
from netsentinel.submit import reason_gen
from netsentinel.submit.form_models import build_payload


def make_entry(**kw):
    base = dict(site_url="http://bad.example.com/", verdict=Verdict.NSFW,
                evidence_zip="x.zip", agg_nsw_prob=0.97, nsw_image_count=3, pages=[1, 2, 3])
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离 HOME + cwd + env。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(tmp_path)
    for f in ("reporter_name", "reporter_phone", "reporter_email", "reporter_id",
              "reporter_address", "reporter_postcode", "reporter_type", "reporter_org"):
        monkeypatch.delenv(f"NETSENTINEL_{f.upper()}", raising=False)
    cfg = Config()
    cfg.profile_path = ""  # auto-detect (permanent → project)
    return cfg


# ── 理由生成 ──────────────────────────────────────────────────────────

class TestReason:
    def test_template_under_39(self):
        r = reason_gen.template_reason(make_entry())
        assert r == "bad.example.com抽查3页,3张图涉色情内容,已人工核实"
        assert len(r) <= 39

    def test_no_pages_variant(self):
        r = reason_gen.template_reason(make_entry(pages=0))
        assert "3张图涉色情" in r and "人工核实" in r and len(r) <= 39

    def test_long_host_truncated(self):
        e = make_entry(site_url="http://" + "a" * 60 + ".example.com/")
        assert len(reason_gen.template_reason(e)) <= 39

    def test_generate_reason_glm_ok(self):
        class FakeClient:
            def chat_json(self, messages):
                return {"reason": "该站多页含色情图,已核实,建议查处"}
        r = reason_gen.generate_reason(make_entry(), Config(), client=FakeClient())
        assert r == "该站多页含色情图,已核实,建议查处" and len(r) <= 39

    def test_generate_reason_glm_overlong_fitted(self):
        class FakeClient:
            def chat_json(self, messages):
                return {"reason": "长" * 80}
        assert len(reason_gen.generate_reason(make_entry(), Config(), client=FakeClient())) <= 39

    def test_generate_reason_glm_exception_fallback(self):
        class Bad:
            def chat_json(self, messages):
                raise RuntimeError("离线")
        r = reason_gen.generate_reason(make_entry(), Config(), client=Bad())
        assert "人工核实" in r

    def test_prompt_forbids_new_facts(self):
        captured = {}
        class Cap:
            def chat_json(self, messages):
                captured["m"] = messages[0]["content"]
                return {"reason": "ok"}
        reason_gen.generate_reason(make_entry(), Config(), client=Cap())
        assert "禁止添加任何新信息" in captured["m"]


# ── 永久模板 ──────────────────────────────────────────────────────────

class TestProfile:
    def test_permanent_write_and_load(self, isolated):
        from netsentinel.submit.profile import save_permanent, load_profile
        save_permanent({"reporter_name": "张三", "reporter_phone": "13800001234"})
        p = load_profile(isolated)
        assert p.reporter_name == "张三" and p.reporter_phone == "13800001234"

    def test_env_overrides_permanent(self, isolated, monkeypatch):
        from netsentinel.submit.profile import save_permanent, load_profile
        save_permanent({"reporter_name": "永久"})
        monkeypatch.setenv("NETSENTINEL_REPORTER_NAME", "环境")
        assert load_profile(isolated).reporter_name == "环境"

    def test_explicit_overrides_all(self, isolated):
        from netsentinel.submit.profile import save_permanent, load_profile
        save_permanent({"reporter_name": "永久"})
        p = load_profile(isolated, explicit={"reporter_name": "显式"})
        assert p.reporter_name == "显式"

    def test_masked_never_full(self, isolated, monkeypatch):
        from netsentinel.submit.profile import save_permanent, load_profile
        save_permanent({"reporter_name": "张三丰", "reporter_phone": "13812345678",
                        "reporter_id": "110101199001011234"})
        p = load_profile(isolated)
        m = p.masked()
        joined = "".join(str(v) for v in m.values())
        assert "13812345678" not in joined and "110101199001011234" not in joined

    def test_all_fields_in_profile(self, isolated):
        from netsentinel.submit.profile import save_permanent, load_profile, PROFILE_FIELDS
        vals = {f: f"val_{f}" for f in PROFILE_FIELDS}
        save_permanent(vals)
        p = load_profile(isolated)
        for f in PROFILE_FIELDS:
            assert getattr(p, f) == f"val_{f}"


# ── build_payload 集成 ────────────────────────────────────────────────

class TestPayloadWiring:
    def test_reason_first_line(self, isolated):
        payload = build_payload(make_entry(), Portal.P12377, isolated)
        first = payload.description.split(chr(10))[0]
        assert payload.reason == first and len(first) <= 39

    def test_profile_autofilled(self, isolated):
        from netsentinel.submit.profile import save_permanent
        save_permanent({"reporter_name": "钱七", "reporter_phone": "13666667777",
                        "reporter_email": "q@x.com", "reporter_id": "110101200001011234"})
        payload = build_payload(make_entry(), Portal.P12377, isolated)
        assert payload.reporter_name == "钱七"
        assert payload.reporter_phone == "13666667777"
        assert payload.reporter_email == "q@x.com"
        assert payload.reporter_id == "110101200001011234"

    def test_reason_param_passthrough(self, isolated):
        payload = build_payload(make_entry(), Portal.SHDF, isolated, reason="自定义理由不超限")
        assert payload.reason == "自定义理由不超限"
