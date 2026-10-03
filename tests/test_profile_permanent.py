"""V10.3 个人信息永久模板测试。"""
from __future__ import annotations

import io
import pathlib

import pytest

from netsentinel.contracts import Config
from netsentinel.submit import profile as pf


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """隔离 HOME 目录(永久模板路径指向 tmp)。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: fake_home))
    monkeypatch.chdir(tmp_path)
    # 清除所有相关环境变量
    for f in pf.PROFILE_FIELDS:
        monkeypatch.delenv(f"NETSENTINEL_{f.upper()}", raising=False)
    return fake_home


class TestPermanentPath:
    def test_permanent_path_in_home(self, isolated_home):
        p = pf.permanent_path()
        assert ".netsentinel" in str(p)
        assert p.parent == isolated_home / ".netsentinel"

    def test_setup_writes_permanent(self, isolated_home):
        stdin = io.StringIO("张三\n13800001234\nz@x.com\n\n\n\n个人\n\n")
        stdout = io.StringIO()
        path = pf.setup_interactive(stdin=stdin, stdout=stdout)
        assert path == pf.permanent_path()
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert "张三" in content and "13800001234" in content

    def test_setup_required_repeats(self, isolated_home):
        # 姓名为空会重复询问
        stdin = io.StringIO("\n张三\n13800001234\n\n\n\n\n\n")
        stdout = io.StringIO()
        pf.setup_interactive(stdin=stdin, stdout=stdout)
        out = stdout.getvalue()
        assert "必填" in out and "不能为空" in out

    def test_load_reads_permanent(self, isolated_home):
        pf.save_permanent({"reporter_name": "李四", "reporter_phone": "13911112222"})
        p = pf.load_profile(Config())
        assert p.reporter_name == "李四" and p.reporter_phone == "13911112222"
        assert p.has_required

    def test_project_yaml_fallback(self, isolated_home):
        # 永久模板不存在时,读项目级 ./profile.yaml
        proj = pathlib.Path.cwd() / "profile.yaml"
        proj.write_text('reporter_name: "王五"\nreporter_phone: "13700001111"\n', encoding="utf-8")
        p = pf.load_profile(Config())
        assert p.reporter_name == "王五"

    def test_permanent_overrides_project(self, isolated_home):
        pf.save_permanent({"reporter_name": "永久", "reporter_phone": "13000000001"})
        proj = pathlib.Path.cwd() / "profile.yaml"
        proj.write_text('reporter_name: "项目"\nreporter_phone: "13000000002"\n', encoding="utf-8")
        p = pf.load_profile(Config())
        assert p.reporter_name == "永久"  # 永久模板优先

    def test_env_overrides_permanent(self, isolated_home, monkeypatch):
        pf.save_permanent({"reporter_name": "永久"})
        monkeypatch.setenv("NETSENTINEL_REPORTER_NAME", "环境")
        p = pf.load_profile(Config())
        assert p.reporter_name == "环境"

    def test_reset(self, isolated_home):
        pf.save_permanent({"reporter_name": "X"})
        assert pf.permanent_path().exists()
        rc = pf.main(["reset"])
        assert rc == 0 and not pf.permanent_path().exists()

    def test_show_masked(self, isolated_home, capsys):
        pf.save_permanent({"reporter_name": "张三丰", "reporter_phone": "13812345678"})
        pf.main(["show"])
        out = capsys.readouterr().out
        assert "张**" in out and "138****78" in out
        assert "张三丰" not in out  # 红线 39:不显示明文

    def test_cli_path(self, isolated_home, capsys):
        pf.main(["path"])
        out = capsys.readouterr().out
        assert ".netsentinel" in out

    def test_setup_preserves_existing_on_enter(self, isolated_home):
        pf.save_permanent({"reporter_name": "旧名", "reporter_phone": "13000000001",
                           "reporter_email": "old@x.com"})
        stdin = io.StringIO("\n\n\n\n\n\n\n\n")  # 全部回车保留
        stdout = io.StringIO()
        pf.setup_interactive(stdin=stdin, stdout=stdout)
        p = pf.load_profile(Config())
        assert p.reporter_name == "旧名" and p.reporter_phone == "13000000001"

    def test_validation_rejects_bad_phone(self, isolated_home):
        stdin = io.StringIO("张三\nabc\n13800001234\n\n\n\n\n\n")
        stdout = io.StringIO()
        pf.setup_interactive(stdin=stdin, stdout=stdout)
        p = pf.load_profile(Config())
        assert p.reporter_phone == "13800001234"  # 重试后正确

    def test_redline_masked_never_full(self, isolated_home):
        """红线 39:masked() 绝不输出完整敏感字段。"""
        pf.save_permanent({
            "reporter_name": "欧阳娜娜子",
            "reporter_phone": "13812345678",
            "reporter_id": "110101199001011234",
            "reporter_email": "very.long.email@example.com",
            "reporter_address": "北京市朝阳区望京街道某小区某楼某单元某室",
        })
        p = pf.load_profile(Config())
        m = p.masked()
        all_text = "".join(str(v) for v in m.values())
        assert "13812345678" not in all_text
        assert "110101199001011234" not in all_text
        assert "very.long.email" not in all_text
        assert "望京街道某小区某楼某单元某室" not in all_text
