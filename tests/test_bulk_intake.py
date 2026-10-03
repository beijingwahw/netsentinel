"""A107 批量清单导入(netsentinel.ops.bulk_intake)单元测试。

全部离线、只写 tmp_path:

- 三种格式(.txt/.csv/.yaml)各自的解析、混合畸形与拒绝留痕;
- BOM / CRLF / 大小写扩展名容错;
- 拒绝原因中文可读(无 scheme 不可修 / 坏 scheme / 无 host / 解析失败);
- 合法 URL 精确串去重保序;plan_scan 三计数(待扫/未变跳过/同站重复);
- FakeMemory 注入 + 惰性 SiteMemory(A39 语义,真实 sqlite 落在 tmp_path);
- CLI capsys 摘要与错误码。
"""
from __future__ import annotations

import pathlib

import pytest

from netsentinel.contracts import Config
from netsentinel.ops import bulk_intake
from netsentinel.ops.bulk_intake import load_bulk, main, plan_scan


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeMemory:
    """A39 SiteMemory 契约 fake:记住每站点指纹;指纹一致即判"未变跳过"。"""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(store or {})
        self.rescan_calls: list[tuple[str, str]] = []

    def last_fingerprint(self, site_url: str) -> str | None:
        return self.store.get(site_url)

    def should_rescan(self, site_url: str, fp: str) -> tuple[bool, str]:
        self.rescan_calls.append((site_url, fp))
        if self.store.get(site_url) == fp:
            return (False, "指纹一致且未过 TTL(fake)")
        return (True, "站点内容已变化(fake)")


class ExplodingMemory:
    """should_rescan 抛异常:规划必须按"需要重扫"处理(安全方向)。"""

    def last_fingerprint(self, site_url: str) -> str:
        return "fp:boom"

    def should_rescan(self, site_url: str, fp: str) -> tuple[bool, str]:
        raise RuntimeError("记忆库比对炸了(测试注入)")


class PlainBoolMemory:
    """should_rescan 返回裸 bool(宽容契约)的注入对象。"""

    def last_fingerprint(self, site_url: str) -> str:
        return "fp:always"

    def should_rescan(self, site_url: str, fp: str) -> bool:
        return False  # 永远"未变"


def make_cfg(tmp_path: pathlib.Path) -> Config:
    """data_dir 收进 tmp_path 的最小 Config。"""
    return Config(data_dir=str(tmp_path / "data"))


def write(tmp_path: pathlib.Path, name: str, content: str, *, encoding: str = "utf-8") -> pathlib.Path:
    """把文本写入 tmp_path 下的清单文件并返回路径。"""
    p = tmp_path / name
    p.write_text(content, encoding=encoding, newline="")
    return p


# ---------------------------------------------------------------------------
# .txt 解析
# ---------------------------------------------------------------------------
def test_txt_basic_comments_and_blank_lines(tmp_path: pathlib.Path) -> None:
    """普通行生效;# 注释与空行跳过(不计拒绝)。"""
    p = write(
        tmp_path,
        "leads.txt",
        "# 头部注释\n"
        "https://a.example.com\n"
        "\n"
        "   \n"
        "  https://b.example.org/list  \n"
        "# 尾部注释\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://b.example.org/list"]
    assert rejected == []


def test_txt_schemeless_prefix_https(tmp_path: pathlib.Path) -> None:
    """无 scheme 的裸域自动补 https://;host:port 形态(冒号前缀含点)同样可修。"""
    p = write(
        tmp_path,
        "leads.txt",
        "a.example.com\nhttp://b.example.org\nexample.net:8080/x\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == [
        "https://a.example.com",
        "http://b.example.org",
        "https://example.net:8080/x",
    ]
    assert rejected == []


def test_txt_reject_bad_scheme(tmp_path: pathlib.Path) -> None:
    """坏 scheme(ftp)行级拒绝,原因中文可读,不中断整批。"""
    p = write(tmp_path, "leads.txt", "https://a.example.com\nftp://b.example.com\nmailto:x@y.z\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert [r[0] for r in rejected] == ["ftp://b.example.com", "mailto:x@y.z"]
    assert all("http/https" in r[1] for r in rejected)


def test_txt_reject_missing_host(tmp_path: pathlib.Path) -> None:
    """有 scheme 但无 host(https:// 与 https:///x)→ 拒绝"缺少主机名"。"""
    p = write(tmp_path, "leads.txt", "https://\nhttps:///only/path\nhttps://a.example.com\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert len(rejected) == 2
    assert all("主机名" in r[1] for r in rejected)


def test_txt_reject_unfixable_schemeless(tmp_path: pathlib.Path) -> None:
    """无 scheme 的自然语句补 https:// 后仍非法(主机含空格)→ 拒绝。"""
    p = write(tmp_path, "leads.txt", "这不是一个网址 而是一句话\nhello world\na.example.com\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert len(rejected) == 2
    assert all("仍不是合法网址" in r[1] for r in rejected)
    assert rejected[0][0] == "这不是一个网址 而是一句话"


def test_txt_reject_parse_error(tmp_path: pathlib.Path) -> None:
    """urlsplit 解析失败(坏 IPv6 括号)→ 行级拒绝而非崩溃。"""
    p = write(tmp_path, "leads.txt", "https://[bad\nhttps://a.example.com\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert len(rejected) == 1
    assert rejected[0][0] == "https://[bad"
    assert "无法解析为合法 URL" in rejected[0][1]


def test_txt_dedup_preserve_order(tmp_path: pathlib.Path) -> None:
    """合法 URL 精确串去重、保首次出现顺序;补全后的同串也算重复。"""
    p = write(
        tmp_path,
        "leads.txt",
        "b.example.org\na.example.com\nhttps://b.example.org\na.example.com\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://b.example.org", "https://a.example.com"]
    assert rejected == []


def test_txt_bom_and_crlf(tmp_path: pathlib.Path) -> None:
    """UTF-8 BOM 与 CRLF 行尾均被吸收,不影响解析。"""
    p = tmp_path / "leads.txt"
    p.write_bytes(
        "# 注释\r\nhttps://a.example.com\r\nb.example.org\r\n".encode("utf-8-sig")
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://b.example.org"]
    assert rejected == []


def test_txt_empty_file(tmp_path: pathlib.Path) -> None:
    """空文件 / 仅注释文件 → 空结果而非报错。"""
    p1 = write(tmp_path, "empty.txt", "")
    p2 = write(tmp_path, "comments.txt", "# 只有注释\n#\n")
    assert load_bulk(str(p1)) == ([], [])
    assert load_bulk(str(p2)) == ([], [])


# ---------------------------------------------------------------------------
# .csv 解析
# ---------------------------------------------------------------------------
def test_csv_basic_with_extra_columns(tmp_path: pathlib.Path) -> None:
    """标准 url 列生效;其余列(备注等)忽略;行内空白剥离。"""
    p = write(
        tmp_path,
        "leads.csv",
        "url,备注,来源\n"
        "https://a.example.com,线索1,举报\n"
        "  b.example.org ,线索2,巡查  \n"
        "https://c.example.net,线索3,巡查\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://b.example.org", "https://c.example.net"]
    assert rejected == []


def test_csv_bom_header(tmp_path: pathlib.Path) -> None:
    """带 BOM 的 CSV(utf-8-sig)表头可识别,不因 \\ufeffurl 缺列报错。"""
    p = write(tmp_path, "leads.csv", "url,note\nhttps://a.example.com,x\n", encoding="utf-8-sig")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert rejected == []


@pytest.mark.parametrize("header", ["URL", "Url", "网址", "链接"])
def test_csv_url_header_variants(tmp_path: pathlib.Path, header: str) -> None:
    """url/URL/Url/网址/链接 表头均可识别(ASCII 大小写不敏感)。"""
    p = write(tmp_path, "leads.csv", f"{header},note\nhttps://a.example.com,x\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert rejected == []


def test_csv_missing_column_raises(tmp_path: pathlib.Path) -> None:
    """缺网址列 → 整体 ValueError(中文,含实际表头便于排查)。"""
    p = write(tmp_path, "leads.csv", "name,note\n站点甲,举报\n")
    with pytest.raises(ValueError, match="缺少网址列"):
        load_bulk(str(p))


def test_csv_empty_file_raises(tmp_path: pathlib.Path) -> None:
    """零字节 CSV 无表头 → 整体 ValueError(缺表头)。"""
    p = write(tmp_path, "leads.csv", "")
    with pytest.raises(ValueError, match="缺少表头"):
        load_bulk(str(p))


def test_csv_empty_url_cell_rejected(tmp_path: pathlib.Path) -> None:
    """网址单元格为空的数据行计入拒绝("网址为空");完全空行自动跳过。"""
    p = write(
        tmp_path,
        "leads.csv",
        "url,note\nhttps://a.example.com,ok\n,缺网址\n\nhttps://b.example.org,ok\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://b.example.org"]
    assert rejected == [("", "网址为空")]


def test_csv_mixed_valid_and_rejected(tmp_path: pathlib.Path) -> None:
    """混合畸形:补 scheme、坏 scheme、无 host 同批共存,坏行只拒绝不中断。"""
    p = write(
        tmp_path,
        "leads.csv",
        "url\na.example.com\nftp://b.example.com\nhttps://\nhttps://c.example.net\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://c.example.net"]
    assert [r[0] for r in rejected] == ["ftp://b.example.com", "https://"]
    assert "http/https" in rejected[0][1]
    assert "主机名" in rejected[1][1]


# ---------------------------------------------------------------------------
# .yaml / .yml 解析
# ---------------------------------------------------------------------------
def test_yaml_top_level_list(tmp_path: pathlib.Path) -> None:
    """顶层列表形态;含注释与 schemeless 修复。"""
    p = write(
        tmp_path,
        "leads.yaml",
        "# 线索清单\n- https://a.example.com\n- b.example.org\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com", "https://b.example.org"]
    assert rejected == []


def test_yml_suffix_dispatch(tmp_path: pathlib.Path) -> None:
    """.yml 扩展名同样按 YAML 分发。"""
    p = write(tmp_path, "leads.yml", "- https://a.example.com\n")
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert rejected == []


def test_yaml_urls_mapping(tmp_path: pathlib.Path) -> None:
    """{"urls": [...]} 映射形态。"""
    p = write(
        tmp_path,
        "leads.yaml",
        "urls:\n  - https://a.example.com\n  - https://a.example.com  # 精确重复\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert rejected == []


def test_yaml_missing_pyyaml_raises_with_hint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyYAML 缺失时 .yaml 形态抛 ValueError 且带安装提示(惰性导入缝)。"""
    monkeypatch.setattr(bulk_intake, "_import_yaml_optional", lambda: None)
    p = write(tmp_path, "leads.yaml", "- https://a.example.com\n")
    with pytest.raises(ValueError, match="pip install PyYAML"):
        load_bulk(str(p))


def test_yaml_broken_syntax_raises(tmp_path: pathlib.Path) -> None:
    """YAML 语法损坏 → 整体 ValueError(中文)。"""
    p = write(tmp_path, "leads.yaml", "a: b: c\n")
    with pytest.raises(ValueError, match="不是合法 YAML"):
        load_bulk(str(p))


@pytest.mark.parametrize(
    "content",
    [
        "https://a.example.com",          # 顶层标量
        "urls: https://a.example.com",    # urls 不是列表
        "name: 甲\nnote: 乙\n",           # 映射缺 urls 键
    ],
    ids=["scalar", "urls-not-list", "mapping-without-urls"],
)
def test_yaml_bad_shape_raises(tmp_path: pathlib.Path, content: str) -> None:
    """顶层形态不合法 → 整体 ValueError。"""
    p = write(tmp_path, "leads.yaml", content)
    with pytest.raises(ValueError):
        load_bulk(str(p))


def test_yaml_non_string_entries_rejected(tmp_path: pathlib.Path) -> None:
    """列表内非字符串元素行级拒绝(中文原因含实际类型),不中断整批。"""
    p = write(
        tmp_path,
        "leads.yaml",
        "- https://a.example.com\n- 123\n- null\n",
    )
    valid, rejected = load_bulk(str(p))
    assert valid == ["https://a.example.com"]
    assert [r[0] for r in rejected] == ["123", "None"]
    assert "int" in rejected[0][1] and "NoneType" in rejected[1][1]


def test_yaml_empty_only_comments(tmp_path: pathlib.Path) -> None:
    """仅注释的 YAML → 空结果而非报错。"""
    p = write(tmp_path, "leads.yaml", "# 空\n")
    assert load_bulk(str(p)) == ([], [])


# ---------------------------------------------------------------------------
# 分发与文件级错误
# ---------------------------------------------------------------------------
def test_missing_file_raises_chinese(tmp_path: pathlib.Path) -> None:
    """文件不存在 → ValueError 中文(批量导入是显式动作,必须立刻暴露)。"""
    with pytest.raises(ValueError, match="不存在"):
        load_bulk(str(tmp_path / "nope.txt"))


def test_unsupported_suffix_raises(tmp_path: pathlib.Path) -> None:
    """不支持的扩展名(.json)→ ValueError;大写 .TXT 可用。"""
    p = write(tmp_path, "leads.json", '{"urls": ["https://a.example.com"]}')
    with pytest.raises(ValueError, match="不支持的批量清单格式"):
        load_bulk(str(p))
    up = tmp_path / "leads.TXT"
    up.write_text("https://a.example.com\n", encoding="utf-8")
    assert load_bulk(str(up))[0] == ["https://a.example.com"]


# ---------------------------------------------------------------------------
# plan_scan:三计数 + 记忆注入/惰性
# ---------------------------------------------------------------------------
def test_plan_same_site_dedup_first_kept(tmp_path: pathlib.Path) -> None:
    """同站多个 URL 只扫第一个,其余计 duplicate_urls;顺序保持。"""
    urls = [
        "https://a.example.com/first",
        "https://a.example.com/second",
        "https://a.example.com:8080/third",
        "https://b.example.org/only",
    ]
    plan = plan_scan(urls, make_cfg(tmp_path), memory=FakeMemory())
    assert plan["to_scan"] == ["https://a.example.com/first", "https://b.example.org/only"]
    assert plan["duplicate_urls"] == 2
    assert plan["skipped_unchanged"] == 0


def test_plan_www_and_subdomain_same_site(tmp_path: pathlib.Path) -> None:
    """www 前缀 / 子域差异视为同站(canonical 语义),只扫第一个。"""
    urls = [
        "https://www.example.com/",
        "https://example.com/",
        "https://sub.example.com/deep",
    ]
    plan = plan_scan(urls, make_cfg(tmp_path), memory=FakeMemory())
    assert plan["to_scan"] == ["https://www.example.com/"]
    assert plan["duplicate_urls"] == 2


def test_plan_empty_urls(tmp_path: pathlib.Path) -> None:
    """空列表 → 三计数全零。"""
    plan = plan_scan([], make_cfg(tmp_path), memory=FakeMemory())
    assert plan == {"to_scan": [], "skipped_unchanged": 0, "duplicate_urls": 0}


def test_plan_lazy_default_memory_creates_db(tmp_path: pathlib.Path) -> None:
    """memory=None 时惰性构造 <data_dir>/site_memory.db(A39 语义);首见必扫。"""
    cfg = make_cfg(tmp_path)
    plan = plan_scan(["https://a.example.com/", "https://b.example.org/"], cfg)
    assert plan["to_scan"] == ["https://a.example.com/", "https://b.example.org/"]
    assert plan["skipped_unchanged"] == 0
    assert plan["duplicate_urls"] == 0
    assert (tmp_path / "data" / "site_memory.db").is_file()


def test_plan_fake_memory_skips_unchanged(tmp_path: pathlib.Path) -> None:
    """注入 FakeMemory:last_fingerprint 非空且 should_rescan 判 False → 跳过。"""
    mem = FakeMemory(store={"https://a.example.com/": "fp:a"})
    plan = plan_scan(
        ["https://a.example.com/", "https://b.example.org/"], make_cfg(tmp_path), memory=mem
    )
    assert plan == {
        "to_scan": ["https://b.example.org/"],
        "skipped_unchanged": 1,
        "duplicate_urls": 0,
    }
    assert mem.rescan_calls == [("https://a.example.com/", "fp:a")]


def test_plan_fake_memory_rescan_when_changed(tmp_path: pathlib.Path) -> None:
    """指纹已变化(should_rescan 判 True)→ 不跳过,进入待扫。"""

    class ChangedMemory(FakeMemory):
        def should_rescan(self, site_url: str, fp: str) -> tuple[bool, str]:
            return (True, "站点内容已变化")

    mem = ChangedMemory(store={"https://a.example.com/": "fp:old"})
    plan = plan_scan(["https://a.example.com/"], make_cfg(tmp_path), memory=mem)
    assert plan["to_scan"] == ["https://a.example.com/"]
    assert plan["skipped_unchanged"] == 0


def test_plan_should_rescan_raising_treated_as_rescan(tmp_path: pathlib.Path) -> None:
    """should_rescan 抛异常 → 按"需要重扫"处理(安全方向:宁可多扫)。"""
    plan = plan_scan(
        ["https://a.example.com/"], make_cfg(tmp_path), memory=ExplodingMemory()
    )
    assert plan["to_scan"] == ["https://a.example.com/"]
    assert plan["skipped_unchanged"] == 0


def test_plan_should_rescan_plain_bool(tmp_path: pathlib.Path) -> None:
    """should_rescan 返回裸 bool 也兼容:False → 跳过。"""
    plan = plan_scan(
        ["https://a.example.com/", "https://b.example.org/"],
        make_cfg(tmp_path),
        memory=PlainBoolMemory(),
    )
    assert plan["to_scan"] == []
    assert plan["skipped_unchanged"] == 2


def test_plan_memory_without_fingerprint_api(tmp_path: pathlib.Path) -> None:
    """记忆对象缺 last_fingerprint/should_rescan 接口 → 不跳过、不崩溃。"""
    plan = plan_scan(
        ["https://a.example.com/"], make_cfg(tmp_path), memory=object()
    )
    assert plan["to_scan"] == ["https://a.example.com/"]
    assert plan["skipped_unchanged"] == 0


def test_plan_lazy_real_site_memory_end_to_end(tmp_path: pathlib.Path) -> None:
    """端到端:真实 SiteMemory 预记指纹 → 惰性路径读到并跳过(sqlite 落 tmp)。"""
    site_memory = pytest.importorskip("netsentinel.intel.site_memory")
    db = tmp_path / "site_memory.db"
    with site_memory.SiteMemory(str(db), ttl_hours=72) as mem:
        mem.remember("https://a.example.com/", "fp:stable")
    cfg = Config(data_dir=str(tmp_path))
    plan = plan_scan(["https://a.example.com/", "https://b.example.org/"], cfg)
    assert plan["to_scan"] == ["https://b.example.org/"]
    assert plan["skipped_unchanged"] == 1
    assert plan["duplicate_urls"] == 0


# ---------------------------------------------------------------------------
# CLI(capsys;--plan 一律注入 fake 记忆,零落盘零外呼)
# ---------------------------------------------------------------------------
def test_cli_default_load_summary(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """默认(无 --plan)只加载打印中文摘要:合法 N / 拒绝 M(含原因)。"""
    p = write(
        tmp_path,
        "leads.txt",
        "https://a.example.com\nftp://bad.example.com\n",
    )
    rc = main(["--input", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "合法 URL 1 个" in out
    assert "拒绝 1 条" in out
    assert "ftp://bad.example.com" in out
    assert "http/https" in out
    assert "待扫" not in out  # 未加 --plan 不做规划


def test_cli_rejected_overflow_first_five(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """拒绝超过 5 条只展示前 5 条原因,其余中文提示从略。"""
    lines = ["https://ok.example.com"] + [f"ftp://bad{i}.example.com" for i in range(7)]
    p = write(tmp_path, "leads.txt", "\n".join(lines) + "\n")
    rc = main(["--input", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "拒绝 7 条" in out
    assert out.count("  拒绝:") == 5
    assert "其余 2 条拒绝条目从略" in out


def test_cli_missing_file_returns_2(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """清单不存在 → 打印中文错误并返回 2。"""
    rc = main(["--input", str(tmp_path / "nope.txt")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "批量清单加载失败" in out and "不存在" in out


def test_cli_csv_missing_column_returns_2(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CSV 缺网址列 → CLI 捕获 ValueError 返回 2(中文)。"""
    p = write(tmp_path, "leads.csv", "name,note\n甲,乙\n")
    rc = main(["--input", str(p)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "缺少网址列" in out


def test_cli_plan_with_injected_memory(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--plan:加载后追加规划摘要;注入 fake 缺省记忆避免真实落盘。"""
    monkeypatch.setattr(
        bulk_intake,
        "_default_memory",
        lambda cfg: FakeMemory(store={"https://a.example.com/a": "fp:a"}),
    )
    p = write(
        tmp_path,
        "leads.txt",
        "https://a.example.com/a\n"
        "https://a.example.com/a\n"      # 精确重复(加载期去重)
        "https://a.example.com/b\n"      # 同站重复(canonical)
        "https://b.example.org/\n",
    )
    rc = main(["--input", str(p), "--plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "合法 URL 3 个" in out
    assert "待扫 1 个" in out
    assert "指纹未变跳过 1 个" in out
    assert "同站重复 1 条" in out


def test_cli_requires_input_exits() -> None:
    """缺 --input 参数:argparse 报错退出(SystemExit 2)。"""
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_cli_plan_bad_file_returns_2(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--plan 下清单坏损同样返回 2:先加载失败就没有规划可谈。"""
    rc = main(["--input", str(tmp_path / "ghost.csv"), "--plan"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "加载失败" in out
