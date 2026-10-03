"""A17 netsentinel.submit.playbook_gen 测试。

只依赖共享契约 netsentinel.contracts(禁改),不依赖兄弟模块;全程离线,
只读写 tmp_path。安全断言:HUMAN_GATE 行必须带"⚠️ 人工操作"标注与人工说明。
"""
import json

from netsentinel.contracts import Portal, Step, StepAction, SubmissionPayload, SubmissionPlan
from netsentinel.submit import playbook_gen


def make_plan(portal=Portal.P12377) -> SubmissionPlan:
    """构造一份本地 mock 入口的示例计划(绝不指向真实门户)。"""
    payload = SubmissionPayload(
        portal=portal,
        site_url="http://127.0.0.1:8777/demo/index.html",
        category="色情低俗信息",
        description=(
            "经图像识别辅助初筛并人工复核,该站点存在大量疑似色情图片,"
            "共抽样 12 张中 9 张达到判定线,截图与图片清单见附件压缩包,"
            "请依法核查处置该站点,并附人工复核记录与模型评分明细。"
        ),  # 共 80+ 字,验证摘要表只取前 80 字
        evidence_zip="data/evidence/example_20260101_120000.zip",
        reporter_name="张三",
        reporter_phone="13800000000",
    )
    entry_url = "http://127.0.0.1:8777/mock/12377.html"
    steps = [
        Step(action=StepAction.GOTO, label="打开举报入口", value=entry_url),
        Step(action=StepAction.WAIT, label="等待页面加载", value="1"),
        Step(
            action=StepAction.SELECT, label="选择信息类型",
            selector="#report-type", text="信息类型下拉框", value="色情低俗信息",
        ),
        Step(
            action=StepAction.FILL, label="填写举报链接",
            selector="#report-url", text="举报链接输入框", value=payload.site_url,
        ),
        Step(
            action=StepAction.FILL, label="填写具体描述",
            selector="#report-desc", text="具体描述输入区", value=payload.description,
        ),
        Step(
            action=StepAction.FILL, label="填写举报人姓名",
            selector="#report-name", text="举报人姓名输入框", value=payload.reporter_name,
        ),
        Step(
            action=StepAction.FILL, label="填写举报人电话",
            selector="#report-phone", text="举报人电话输入框", value=payload.reporter_phone,
        ),
        Step(action=StepAction.SCREENSHOT, label="截图:填写完成"),
        Step(
            action=StepAction.HUMAN_GATE,
            label="人工核对信息、上传证据包 zip 并输入验证码",
            selector="#report-captcha",
        ),
        Step(
            action=StepAction.CLICK, label="点击提交按钮",
            selector="#report-submit", text="提交",
        ),
        Step(action=StepAction.WAIT, label="等待提交结果", value="2"),
        Step(action=StepAction.SCREENSHOT, label="截图:提交结果"),
    ]
    return SubmissionPlan(
        portal=portal, entry_url=entry_url, payload=payload, steps=steps,
    )


# ---------------------------------------------------------------- markdown


def test_markdown_title_entry_and_payload_fields():
    md = playbook_gen.plan_to_markdown(make_plan())
    assert "# 举报 Playbook:" in md
    # 门户名映射
    assert "中央网信办违法和不良信息举报中心" in md
    # 入口 URL
    assert "http://127.0.0.1:8777/mock/12377.html" in md
    # payload 摘要字段
    assert "http://127.0.0.1:8777/demo/index.html" in md
    assert "色情低俗信息" in md
    assert "data/evidence/example_20260101_120000.zip" in md
    # 摘要表"描述(前 80 字)"行只含截断后的预览
    desc_rows = [ln for ln in md.splitlines() if ln.startswith("| 描述(前 80 字) |")]
    assert len(desc_rows) == 1
    plan = make_plan()
    assert desc_rows[0] == f"| 描述(前 80 字) | {plan.payload.description.strip()[:80]} |"


def test_markdown_steps_table_header_and_rows():
    md = playbook_gen.plan_to_markdown(make_plan())
    # 表头五列(整行精确断言)
    assert "| # | 操作 | 说明 | 目标(selector/text) | 值 |" in md
    # 步骤表行数与 selector/text/value 呈现
    assert "`#report-type`" in md
    assert "信息类型下拉框" in md
    assert "| goto |" in md
    assert "| 12 | screenshot |" in md  # 共 12 步,末行为截图


def test_markdown_human_gate_row_and_footer_notice():
    md = playbook_gen.plan_to_markdown(make_plan())
    # HUMAN_GATE 行整行前缀
    assert "⚠️ 人工操作:" in md
    gate_lines = [ln for ln in md.splitlines() if "⚠️ 人工操作:" in ln]
    assert len(gate_lines) == 1
    # 值列写人工说明
    assert "验证码/附件上传由人工完成" in gate_lines[0]
    # 表尾声明
    assert "本 playbook 由系统生成" in md
    assert "验证码严禁自动处理" in md


def test_markdown_unknown_portal_shown_as_is():
    plan = make_plan(portal="weibo")  # 非契约枚举值 → 原样展示
    md = playbook_gen.plan_to_markdown(plan)
    assert "weibo" in md


# ---------------------------------------------------------------- json


def test_plan_to_json_enums_converted_and_steps_kept():
    plan = make_plan()
    data = playbook_gen.plan_to_json(plan)
    assert data["portal"] == "12377" and isinstance(data["portal"], str)
    assert data["payload"]["portal"] == "12377"
    assert isinstance(data["entry_url"], str) and data["entry_url"].startswith("http://127.0.0.1")
    # 枚举转字符串
    actions = [s["action"] for s in data["steps"]]
    assert all(isinstance(a, str) for a in actions)
    assert "human_gate" in actions and "goto" in actions
    # 步骤数量一致,字段为 snake_case
    assert len(data["steps"]) == len(plan.steps)
    for key in ("action", "label", "selector", "text", "value", "timeout_s", "meta"):
        assert key in data["steps"][0]
    # payload 字段齐全
    for key in ("portal", "site_url", "category", "description",
                "evidence_zip", "reporter_name", "reporter_phone"):
        assert key in data["payload"]
    # 可直接 json.dumps
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data


def test_plan_to_json_unknown_portal_value():
    data = playbook_gen.plan_to_json(make_plan(portal="weibo"))
    assert data["portal"] == "weibo"


# ---------------------------------------------------------------- main CLI


def test_main_roundtrip_writes_md_file(tmp_path):
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(
        json.dumps(playbook_gen.plan_to_json(make_plan()), ensure_ascii=False),
        encoding="utf-8",
    )
    out_md = tmp_path / "out" / "playbook.md"
    rc = playbook_gen.main([str(plan_json), "-o", str(out_md)])
    assert rc == 0
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    assert "# 举报 Playbook:" in content
    assert "⚠️ 人工操作:" in content
    assert "验证码严禁自动处理" in content


def test_main_prints_to_stdout(tmp_path, capsys):
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(
        json.dumps(playbook_gen.plan_to_json(make_plan(portal=Portal.SHDF)), ensure_ascii=False),
        encoding="utf-8",
    )
    rc = playbook_gen.main([str(plan_json)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "全国“扫黄打非”工作小组办公室" in out
    assert "| # | 操作 |" in out


def test_main_missing_file_returns_error(tmp_path):
    rc = playbook_gen.main([str(tmp_path / "nope.json")])
    assert rc == 2


# ---------------------------------------------------------------------------
# V5 升级:可观测性(playbook.generated)+ 静态块预计算 + 枚举转换缓存
# ---------------------------------------------------------------------------
from netsentinel import telemetry  # noqa: E402


def test_v5_generated_counter_on_each_markdown_call():
    telemetry.reset()
    playbook_gen.plan_to_markdown(make_plan())
    playbook_gen.plan_to_markdown(make_plan(portal=Portal.SHDF))
    assert telemetry.snapshot()["counters"].get("playbook.generated") == 2.0


def test_v5_generated_counter_not_incremented_by_plan_to_json():
    telemetry.reset()
    playbook_gen.plan_to_json(make_plan())
    assert "playbook.generated" not in telemetry.snapshot()["counters"]


def test_v5_static_blocks_are_precomputed_module_constants():
    """静态区块(表头/页脚)为导入期常量,且逐字符出现在渲染产物中。"""
    md = playbook_gen.plan_to_markdown(make_plan())
    assert playbook_gen._SUMMARY_TABLE_HEADER in md
    assert playbook_gen._STEPS_TABLE_HEADER in md
    assert playbook_gen._FOOTER_BLOCK in md
    # 页脚声明与执行须知各出现一次,无重复拼接
    assert md.count("执行须知:") == 1
    assert md.count("验证码严禁自动处理") == 1
    assert md.endswith("\n")


def test_v5_action_value_conversion_cached_and_stable():
    """枚举转换走 lru_cache:命中不改变结果,未知值仍按字符串透传。"""
    from netsentinel.contracts import StepAction

    first = playbook_gen._action_value(StepAction.HUMAN_GATE)
    second = playbook_gen._action_value(StepAction.HUMAN_GATE)
    assert first == second == "human_gate"
    cached = playbook_gen._cached_action_value
    assert cached.cache_info().hits >= 1  # 第二次命中缓存
    # 非枚举入参(契约外防御)不缓存错值
    assert playbook_gen._action_value("weird-op") == "weird-op"


def test_v5_portal_value_conversion_cached_and_stable():
    assert playbook_gen._portal_value(Portal.P12377) == "12377"
    assert playbook_gen._portal_value("weibo") == "weibo"
    assert playbook_gen._cached_portal_value.cache_info().misses >= 2


def test_v5_markdown_identical_across_repeated_renderings():
    plan = make_plan()
    assert playbook_gen.plan_to_markdown(plan) == playbook_gen.plan_to_markdown(plan)
