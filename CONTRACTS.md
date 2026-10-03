# NetSentinel(净网哨兵)团队契约 v1 —— 20 代理并行开发规范

> 规范文件:`netsentinel/contracts.py`(数据结构,禁改)+ 本文件(接口与流程,禁改)。
> 包名拼写:`netsentinel`(注意双写 t 前 net-sen-tinel)。

## 0. 项目定位与安全红线

对运营者提供的 URL 做网页抽样 + 图像识别,判定疑似色情站点,生成证据包;经**人工确认**后,
用浏览器/computer-use 自动化把举报信息填写到 www.12377.cn(中央网信办举报中心)与
www.shdf.gov.cn(扫黄打非)的举报表单。

**红线(必须体现在代码与测试里,违反即缺陷):**
1. 绝不自动识别/绕过验证码 —— 计划中验证码环节只能是 `HUMAN_GATE` 步骤,停下来等人工。
2. 真实提交前必须有人工确认(`HUMAN_GATE`),`human_gate_required=True` 不可配置关闭。
3. 开发与测试期间**绝不访问** www.12377.cn / www.shdf.gov.cn 真实站点;只用本地 mock 或 dry_run。
4. `allow_network=False` 为默认:除 `127.0.0.1` / `localhost` 外,产品代码不得发起真实网络请求。
5. 提交频控:`submit_min_interval_s`(默认 60s)与 `submit_max_per_day`(默认 5)强制生效。

## 1. 目录与文件归属(只能写自己名下的文件)

```
netsentinel/                 # Python 包
  contracts.py               # [项目负责人] 数据契约(已完成,禁改)
  config.py  logging_util.py # [A01]
  crawler/fetcher.py         # [A02]
  crawler/browser.py         # [A03]
  crawler/site_map.py        # [A04]
  vision/classifier_base.py  # [A05] 与 vision/stub_classifier.py
  vision/nudenet_adapter.py  # [A06]
  vision/hf_clip_adapter.py  # [A07]
  vision/ensemble.py         # [A08]
  decision/verdict.py        # [A09]
  decision/review_queue.py   # [A10]
  evidence/packager.py       # [A11]
  submit/form_models.py      # [A12]
  submit/portal_12377.py     # [A13] + docs/portal_12377_notes.md
  submit/portal_shdf.py      # [A14] + docs/portal_shdf_notes.md
  submit/executor_playwright.py # [A15] + tests/fixtures/mini_form.html
  submit/rate_limit.py       # [A18]
  submit/playbook_gen.py     # [A17]
  pipeline/orchestrator.py   # [A18]
  __main__.py                # [A18]
tests/
  conftest.py                # [项目负责人] 已完成,禁改
  test_config.py test_logging_util.py            # [A01]
  test_fetcher.py            # [A02]
  test_browser.py            # [A03]
  test_site_map.py           # [A04]
  test_stub_classifier.py    # [A05]
  test_nudenet_adapter.py    # [A06]
  test_hf_clip_adapter.py    # [A07]
  test_ensemble.py           # [A08]
  test_verdict.py            # [A09]
  test_review_queue.py       # [A10]
  test_packager.py           # [A11]
  test_form_models.py        # [A12]
  test_portal_12377.py       # [A13]
  test_portal_shdf.py        # [A14]
  test_executor_playwright.py# [A15]
  mock_portals/              # [A16] 12377_mock.html shdf_mock.html serve.py
  test_mock_portal_flow.py   # [A16]
  test_playbook_gen.py       # [A17]
  test_orchestrator.py test_rate_limit.py        # [A18]
  test_e2e_stub.py  fixtures/demo_site/*        # [A19]
  fixtures/mini_form.html    # [A15]
drivers/driver.mjs           # [A17]
docs/computer-use-integration.md                # [A17]
scripts/make_png.py scripts/demo_stub_scan.py   # [A19]
README.md docs/USAGE.md docs/ETHICS.md docs/ARCHITECTURE.md  # [A20]
config.example.yaml pyproject.toml .gitignore   # [项目负责人] 已完成,禁改
```

## 2. 模块 API 规范(签名固定,其他代理按此调用)

- `netsentinel.config`:`load_config(path: str|None = None) -> Config`(默认找 ./config.yaml;yaml 键与字段同名;未知键告警忽略;范围校验见下)、`save_config(cfg, path)`。
  校验:`0 < review_threshold <= nsfw_threshold <= 1`;`prob_count_line <= nsfw_threshold`;`submit_min_interval_s >= 30`;`1 <= submit_max_per_day <= 20`;`max_pages >= 1`。违反抛 `ValueError`(中文消息)。
- `netsentinel.logging_util`:`setup_logging(cfg: Config, verbose: bool = False)`;`class JsonlAuditLogger(path)`:`log_event(event: str, **fields)` 追加 JSON 行(自动带 ts,自动建目录)。
- `netsentinel.crawler.fetcher`:`class NetworkDisabledError(RuntimeError)`;`fetch_page(url, cfg) -> tuple[int, str, str]`(status, html, final_url;urllib;UA=`NetSentinel/0.1`;读取上限 2MB;`allow_network=False` 时仅放行 127.0.0.1/localhost);`download_images(urls: list[str], source_page: str, dest_dir: str, cfg) -> list[ImageEvidence]`(去重、单图 ≤ max_image_mb、Content-Type image/*、sha256、失败跳过)。
- `netsentinel.crawler.browser`:`capture_page(url, cfg, *, fetch_page=None, download=None) -> PageSample`(playwright 惰性导入:goto/整页截图/取 html;无 playwright 时退化为仅抓 HTML 解析图片链接,screenshot_path 留空;`<img src>`+`data-src`,urljoin 绝对化,去重,取前 max_images_per_page 张下载;text_hint_hits 用模块级关键词常量表做小写子串匹配)。
- `netsentinel.crawler.site_map`:`discover_links(start_url, cfg, fetch_page=None) -> list[str]`(同 host BFS,含起点,≤ max_pages;fetch_page 可注入)。
- `netsentinel.vision.classifier_base`:`class NsfwClassifier(ABC)`(`name` 属性;`classify(img: ImageEvidence) -> ImageScore`;`classify_batch(imgs) -> list[ImageScore]` 默认循环实现);`register_classifier(name, cls)`;`get_classifier(name, cfg)`。
- `netsentinel.vision.stub_classifier`:规则分类器并注册 `"stub"`:文件名含 `nsfw_hi`→0.97,`nsfw_mid`→0.72,否则 0.02(离线开发/测试专用)。
- `netsentinel.vision.nudenet_adapter` / `hf_clip_adapter`:实现 `NsfwClassifier`,分别注册 `"nudenet"`(NudeDetector 标签加权,EXPOSED_* 高权)与 `"clip"`(transformers pipeline `nateraw/nsfw-image-classification`,label nsfw/sfw 映射概率)。三方库一律惰性导入,缺失抛带安装提示的 ImportError;测试注入 fake detector/pipeline,不下载模型。
- `netsentinel.vision.ensemble`:`ensemble_scores(scores: list[ImageScore], weights: dict[str,float]|None=None) -> list[ImageScore]`(按 `image.path` 分组,加权平均 nsfw_prob,`model="ensemble"`,`scores={"members": {model: prob}}`)。
- `netsentinel.decision.verdict`:`assess(site_url, pages: list[PageSample], ensemble: list[ImageScore], cfg) -> SiteReport`(公式见 §4)。
- `netsentinel.decision.review_queue`:sqlite3 标准库;`Entry` dataclass(id, site_url, verdict, status ∈ pending/approved/rejected/submitted, evidence_zip, created_at, updated_at, note);`ReviewQueue(db_path)`:`add(report: SiteReport, evidence_zip="") -> int`、`list(status=None)`、`get(id)`、`approve(id, note="")`、`reject(id, note="")`、`mark_submitted(id)`;`main(argv=None)` CLI(list/show/approve/reject,中文输出)。
- `netsentinel.evidence.packager`:`build_bundle(report: SiteReport, cfg) -> EvidenceBundle`(复制截图+图片 → `evidence_dir/<safe_host>_<ts>/`;manifest.json = report.as_dict()+files 清单含 sha256;summary.md 中文摘要含 Top10 图片分值表;zipfile 打包)。
- `netsentinel.submit.form_models`:常量 `SELECTORS`(§5);`build_payload(entry_like, portal: Portal, cfg, extra="", reporter_name="", reporter_phone="") -> SubmissionPayload`(entry_like 鸭子类型:site_url/verdict/evidence_zip;description 用中文模板并声明"辅助系统初筛+人工核实");`build_plan(payload, cfg, entry_url=None) -> SubmissionPlan`(步骤序列见 §5;**不得包含任何自动填写验证码的步骤**)。
- `netsentinel.submit.portal_12377`:`plan_12377(entry_like, cfg, entry_url=None, **payload_kw) -> SubmissionPlan`(category="色情低俗信息",portal=P12377,entry_url 默认 cfg.portal_12377_base)。
- `netsentinel.submit.portal_shdf`:`plan_shdf(...)`(category="淫秽色情类",portal=SHDF,entry_url 默认 cfg.portal_shdf_base)。
- `netsentinel.submit.executor_playwright`:`execute(plan, cfg, *, auto_confirm=False, dry_run=None, headless=True, out_dir=None) -> ExecutionResult`。dry_run(默认取 cfg.dry_run_default):不启浏览器,逐 Step 记 notes,ok=True,submitted=False。HUMAN_GATE:auto_confirm→记"自动确认(测试)";dry_run→记"干跑跳过";否则 `input()` 交互等待人工。每步截图到 out_dir(默认 `data/runs/<ts>/`),异常捕获→stopped_at + ok=False。
- `netsentinel.submit.rate_limit`:`RateLimiter(state_path)`:`can_submit(now=None) -> tuple[bool, str]`(间隔≥submit_min_interval_s 且当日次数<submit_max_per_day);`record(now=None)`。
- `netsentinel.pipeline.orchestrator`:`run_scan(url, cfg, *, fetch_page=None, capture=None, classifier=None) -> SiteReport`(discover→逐页 capture→classify_batch(ensemble_members)→ensemble_scores→assess;needs_review 时 build_bundle+queue.add+审计日志);`run_submit(entry_id, portal, cfg, *, auto_confirm=False, dry_run=None, executor=None) -> ExecutionResult`(entry 必须 approved;频控;建 plan;执行;成功 mark_submitted+record)。兄弟模块一律**函数内惰性导入**,缺失抛中文 RuntimeError 指明缺哪个模块。
- `netsentinel.__main__`:argparse 子命令 `scan --url [--config]`、`queue <list|show|approve|reject> [args]`、`submit --id --portal {12377,shdf} [--dry-run/--exec]`(先 setup_logging)。

## 3. 举报表单选择器契约(SELECTORS)

本地 mock 与两个 portal planner 共同遵守(planner 用 `SELECTORS` 常量,mock 用相同 id):

```python
SELECTORS = {
    "url":     "#report-url",      # 举报链接
    "type":    "#report-type",     # 信息类型 select
    "desc":    "#report-desc",     # 具体描述 textarea
    "name":    "#report-name",     # 举报人姓名
    "phone":   "#report-phone",    # 举报人电话
    "file":    "#report-file",     # 附件(证据包 zip)
    "captcha": "#report-captcha",  # 验证码 —— 只允许人工输入
    "submit":  "#report-submit",   # 提交按钮
}
```

标准步骤序列(`build_plan` 生成,顺序固定):
`goto(entry_url)` → `wait(1s)` → `select(type=category)` → `fill(url)` → `fill(desc)` → `fill(name)` → `fill(phone)` → `screenshot(填写完成)` → `HUMAN_GATE(人工核对信息、上传证据包 zip 并输入验证码)` → `click(submit)` → `wait(2s)` → `screenshot(提交结果)`。
附件 zip 不做自动上传(v1 由人工门完成),但 mock 表单需含 `#report-file` 节点。

## 4. 判定公式(assess 的唯一实现依据)

```
candidates = [s for s in ensemble if s.image.width >= cfg.min_image_px or s.image.height >= cfg.min_image_px]
agg        = max((s.nsfw_prob for s in candidates), default=0.0)
nsw_count  = sum(1 for s in candidates if s.nsfw_prob >= cfg.prob_count_line)
verdict    = NSFW    if agg >= cfg.nsfw_threshold and nsw_count >= cfg.min_nsw_images
            else SUSPECT if agg >= cfg.review_threshold
            else CLEAN
needs_review = (verdict != CLEAN)     # NSFW 同样必须人工确认后才允许提交
```

## 5. 测试规则(pytest,根目录运行)

- 离线:不访问外网;本地服务只允许 127.0.0.1;只写 `tmp_path`(或项目 data/ 下并在测试中清理)。
- 可选三方依赖用 `pytest.importorskip`;兄弟模块用 `pytest.importorskip("netsentinel.xxx")` 容错(并行开发中可能尚未就位)。
- 需要真浏览器(chromium)的用例:importorskip("playwright") 且启动浏览器失败时 skip。
- 绝不出现访问 www.12377.cn / www.shdf.gov.cn 的测试或代码路径。
- 完成前自测:`cd /c/1/netsentinel && python -m pytest tests/test_<你的>... -q`,全绿(允许 skip)。

## 6. 术语

- 抽样(scan):抓取 ≤ max_pages 个页面并下载图片;证据包(evidence bundle):截图+图片+manifest+zip;
- 人工门(HUMAN_GATE):暂停自动化等待人工核对/输入验证码;干跑(dry_run):只生成步骤记录不驱动浏览器。
