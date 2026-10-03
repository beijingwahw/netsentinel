# NetSentinel V6 团队契约(A103–A122 并行开发)—— 大批量筛选 / 归纳同名 / 依次批量举报

> 前五轮 80+ 模块、2220 测试全绿。V6 主题:从"单站办案"升级为"批量案件流水线"。

## 0. V6 新红线(24–26,叠加于既有 23 条)

24. **批量举报仍是"逐条人工门"**:批量只是顺序编排,每一条提交的验证码输入与最终确认仍由人工在执行器 HUMAN_GATE 完成;任何批量代码不得出现 auto_confirm=True 的真实提交路径(dry_run/测试除外)。
25. **批量确认声明(留痕)**:案件组进入批量队列前,运营者必须逐组完成证据核验并显式声明(声明文本入审计日志,含组名/条数/审核人);无声明的组不得提交。
26. **批量模式频控不得放宽**:`batch_item_interval_s >= submit_min_interval_s`(config 已强制校验);每日上限 `submit_max_per_day` 照常生效,额度用尽自动挂起可续批。

## 1. 新增 Config 字段(已落地)

`group_merge_phash_overlap(0.3) / group_merge_template(True) / batch_max_items(20) / batch_item_interval_s(90) / batch_require_attestation(True)`

## 2. 核心概念(全组统一)

- **canonical(可注册域归一)**:`http://a.b.example.com.cn:8080/x?y` → 可注册域 `example.com.cn`(内置常见多段后缀表 com.cn/net.cn/org.cn/gov.cn/edu.cn/ac.cn + 国际通用 TLD);canonical_key = 可注册域小写;canonical_name = 可注册域(展示主名)。
- **镜像归并**:同 canonical_key 的所有 URL/条目 → 同一**基础组**(www/子域/端口/路径差异视为同站变体,记入 aliases)。
- **团伙归并**:基础组之间若 检索图谱(A46)存在关联边(shared_image/phash_near/shared_template/redirect)或图片指纹集合重叠率 ≥ group_merge_phash_overlap → 并入同一**案件组**(union-find)。
- **案件组(CaseGroup)**:name(canonical 主名)、aliases(全部域名/URL)、entries(成员复核条目)、agg 取最大、图片并集去重;一个组 = 一份合并证据包 = 一次举报。

## 3. 文件归属(A103–A122)

```
netsentinel/intel/canonical.py  tests/test_canonical.py                    # [A103]
netsentinel/intel/case_group.py  tests/test_case_group.py                 # [A104]
netsentinel/intel/group_linker.py  tests/test_group_linker.py             # [A105]
netsentinel/decision/dedup_policy.py  tests/test_dedup_policy.py          # [A106]
netsentinel/ops/bulk_intake.py  tests/test_bulk_intake.py                 # [A107]
netsentinel/ops/batch_scan.py  tests/test_batch_scan.py                   # [A108]
netsentinel/evidence/merge_bundles.py  tests/test_merge_bundles.py        # [A109]
netsentinel/decision/batch_review.py  tests/test_batch_review.py          # [A110]
netsentinel/cli/batch_tui.py  tests/test_batch_tui.py                     # [A111]
netsentinel/submit/batch_submit.py  tests/test_batch_submit.py            # [A112]
netsentinel/submit/batch_state.py  tests/test_batch_state.py              # [A113]
netsentinel/report/batch_report.py  tests/test_batch_report.py            # [A114]
webui/groups_page.py  tests/test_groups_page.py                           # [A115]
netsentinel/intel/name_suggest.py  tests/test_name_suggest.py             # [A116]
netsentinel/intel/group_stats.py  tests/test_group_stats.py               # [A117]
benchmarks/grouping_bench.py  tests/test_grouping_bench.py                # [A118]
netsentinel/batchflow.py  tests/test_batchflow.py                         # [A119]
scripts/demo_batch_flow.py                                                 # [A120]
docs/BATCH_GUIDE.md  docs/GROUPING.md                                      # [A121]
docs/UPGRADE_V6.md  CHANGELOG_V6 追加段(由负责人收口,A121 只写 UPGRADE_V6)  # [A122]
```

冻结:contracts.py、telemetry.py、conftest.py、四份既有 CONTRACTS、pyproject.toml、既有全部模块(兄弟模块只读,惰性导入)。

## 4. 模块 API 规范(签名固定)

- **A103 canonical.py**:`MULTI_SUFFIXES`(常见多段后缀集合)与通用 TLD 判定;`canonical_key(url) -> str`(可注册域小写;IP 直连→IP 串;解析失败→"" );`canonical_name(url) -> str`(=canonical_key;失败→原 URL 截断);`is_same_site(a, b) -> bool`;`alias_label(url) -> str`(中文变体描述:"www 前缀/端口 8080/路径 /a").纯 stdlib,不发网。
- **A104 case_group.py**:`@dataclass CaseGroup(name, aliases: list[str], entry_ids: list[int], site_urls: list[str], agg_max: float, verdict: str, image_sha_set: set[str], created_at)`;`group_entries(entries: list[EntryLike], reports: dict[entry_id, SiteReport]) -> list[CaseGroup]`:按 canonical_key 聚合(EntryLike 鸭子:id/site_url/verdict/evidence_zip);agg_max/verdict 取最严重档;image_sha_set 从 report 页面图片 sha 并集;输出按 (verdict 档, agg_max, 组规模) 降序;空输入→[]。
- **A105 group_linker.py**:`merge_groups(groups: list[CaseGroup], *, graph=None, overlap_threshold: float, merge_template: bool) -> list[CaseGroup]`:组内两两判定——graph.related_sites 命中(过滤 kind:shared_template 仅当 merge_template)或 image_sha_set 重叠率(Jaccard)≥ threshold → union-find 合并(名称取规模最大组的主名,aliases/entries/urls/并集,agg 取最大);graph 为 None 或空图跳过团伙归并只返回基础组。
- **A106 dedup_policy.py**:`@dataclass DedupRules(phash_overlap: float, template: bool, same_site: bool = True)`;`from_config(cfg)`;`should_merge_groups(a: CaseGroup, b: CaseGroup, rules, *, graph=None) -> tuple[bool, str]`(返回中文依据);`dedup_report_url(group: CaseGroup) -> str`:生成举报用的"主站+镜像清单"文本(≤200 字,主 URL + 全部别名域列表)。
- **A107 bulk_intake.py**:`load_bulk(path) -> tuple[list[str], list[tuple[str, str]]]`:(合法 URL 列表, 拒绝清单[(原始行, 中文原因)]);支持 .txt(每行 URL,# 注释)/.csv(含 url/URL/网址 列,BOM 容错)/.yaml(列表或 {urls:[]});URL 归一化(补 https://、去空白);`plan_scan(urls, cfg, *, memory=None) -> dict`:canonical_key 去重 + site_memory 未变跳过 → {"to_scan": [...], "skipped_unchanged": n, "duplicate_urls": n};CLI `main(argv)`(--input/--plan/--config,只规划不扫描)。
- **A108 batch_scan.py**:`batch_scan(urls: list[str], cfg, *, run_scan=None, memory=None) -> dict`:并发池复用 ops.pool.run_pool(惰性)→ 汇总 {"reports": {url: SiteReport}, "summary": pool 摘要};随后 `group_and_enqueue(cfg, *, queue=None, packager=None, linker...)`:case_group+group_linker 归组 → 每组 build_bundle(合并证据:A109)→ 队列 add 一条(组名入 note 前缀 "[组]");返回 {"groups": [...], "enqueued": n}。全部依赖注入,离线可测。
- **A109 merge_bundles.py**:`merge_bundles(bundles: list[EvidenceBundleLike], out_dir, *, title="") -> EvidenceBundle`:文件按 sha256 去重合并复制;manifest.json = {"title", "sub_reports": [各原 manifest 的 report 摘要], "files": 全量清单};summary.md(中文:组名/涉及站点数/各子报告一行表/图片 Top10);zip 打包;空列表→ValueError 中文。
- **A110 batch_review.py**:`@dataclass Attestation(group_name, items: int, reviewer, ts, text)`;`class BatchReview(db_path)`(sqlite:attestations 表 + 组状态):`attest(group_name, items, reviewer, text) -> Attestation`(text 须含"人工核实"四字否则 ValueError 中文;写审计日志惰性 JsonlAuditLogger);`is_attested(group_name) -> bool`;`ready_entries(cfg, *, queue=None, review=None) -> list[dict]`:approved 且已声明的组 → 待批量条目 [{entry_id, group_name, site_urls, portal: 默认 12377}](portal 参数可覆盖);batch_require_attestation=False 时跳过声明要求(仅当 cfg 明示)。红线 24/25 在此收口。
- **A111 cli/batch_tui.py**:交互式批量复核 TUI(风格同 review_tui,io 注入):`groups`(表格:组名/站点数/判定/agg/已声明?)、`show <组名>`、`attest <组名> --reviewer 名`(打印组内全部 URL+证据包路径→"我已逐站人工核实(Y/N)"→Y 记声明)、`ready`(待批量清单)、`queue-batch [--portal 12377|shdf]`(列出 ready → "确认将依次批量举报 N 条,每条仍需人工输入验证码(Y/N)"→Y 后调 A112 dry_run 演练提示并打印真实执行命令);`main(argv, *, stdin=None, stdout=None) -> int`。
- **A112 batch_submit.py**:`run_batch(items: list[dict], cfg, *, executor=None, portal_planner_12377=None, portal_planner_shdf=None, rate=None, state: BatchState|None, dry_run=None, stop=None) -> dict`:顺序逐条:①state 记录 pending→running ②RateLimiter.can_submit 不通过→**挂起**("额度/间隔限制",状态 rate_limited,整体暂停返回,可续批) ③plan(entry, portal) ④executor(plan, cfg, auto_confirm=False 恒定,dry_run=cfg.dry_run_default 当 dry_run 参数 None) ⑤submitted→state.submitted+rate.record+审计 ⑥失败→state.failed(含中文原因) 继续下一条;stop 回调可注入(每条之间检查,Ctrl-C 语义→paused);返回 {"submitted": n, "failed": n, "rate_limited": bool, "results": [...]};**items > cfg.batch_max_items → ValueError 中文**。
- **A113 batch_state.py**:`class BatchState(db_path)`(sqlite WAL):批次表 batches(id, created_at, note) 与条目表 batch_items(batch_id, entry_id, group_name, status ∈ pending/running/submitted/failed/skipped/rate_limited, error, updated_at);`new_batch(items, note="") -> batch_id`;`mark(batch_id, entry_id, status, error="")`;`resume(batch_id) -> list[未终态条目]`;`summary(batch_id) -> dict`;断点续批=resume 后再 run_batch。
- **A114 batch_report.py**:`render_batch_report(batch_id, summary: dict, out_path) -> str`:HTML(内联 CSS 公文风,中文):批次号/时间/条数、逐条状态表(组名/条目/结果/原因)、提交成功截图路径清单、声明清单、结论段(含"每条均经人工门完成验证码"声明);写盘返回。
- **A115 webui/groups_page.py**:纯逻辑层 `group_rows(groups) -> list[dict]`(组名/站点数/aliases 数/agg/verdict 中文/已声明)、`attest_badge(ok) -> str`、`batch_preview(items) -> str`(中文预览:"将依次提交 N 条,间隔 ≥90s,每条需人工验证码");Streamlit 页(惰性):分组列表/组详情/声明按钮(勾选核验确认)/批量队列预览(**页面不含真实提交**,提示用 CLI/TUI 执行——红线 24)。
- **A116 name_suggest.py**:`suggest_name(group: CaseGroup, *, intel: dict|None=None) -> str`:"{主域名}(含 {n} 个关联站点)"(n>1 时)/主域名;GLM 增强(client 注入,离线回退):依据 intel 概括 ≤20 字特征短语附加;`group_title_row(group) -> dict`(展示行)。测试含离线回退。
- **A117 group_stats.py**:`stats(groups) -> dict`(组数/最大组 Top5/单站组占比/重复线索域排行);`export_csv(groups, path)`(utf-8-sig,Excel 友好);`repeat_offenders(entries, groups) -> list`(同组内曾 rejected 又复现者)。纯函数。
- **A118 benchmarks/grouping_bench.py**:`make_synthetic(n_sites=12, mirrors_per=2) -> list[(url, label)]`(合成:主域+镜像+同团伙共享图暗示——phash 由 A99 现成函数算不了纯 URL,标签由生成器自带);`evaluate(grouping: list[set[str]], labels) -> {"purity": float, "completeness": float}`;`run(out_dir) -> report.md/json`(中文);CLI。测试:完美分组 purity=completeness=1;镜像全归并;跨站误并检测。
- **A119 batchflow.py**:`run_flow(input_path, cfg, *, scan=None, intake=None, review=None, submit=None, dry_run=True) -> dict`:intake→plan→batch_scan→group_and_enqueue→打印分组摘要(中文)→提示 TUI 声明;`resume_batch(batch_id, cfg, ...) -> dict`(续批);`main(argv)`:`--input ... [--config] [--dry-run|--exec] [--resume 批次号]`;`python -m netsentinel.batchflow` 入口;真实提交路径必须显式 --exec 且内部仍走 run_batch(逐条人工门)。
- **A120 scripts/demo_batch_flow.py**(中文演示,零外呼):临时 fixture 生成 3 域名 ×(主站+镜像)共 ~8 URL(本地 server,nsfw_hi 图)→ bulk_intake → batch_scan(stub 注入,真扫描本地)→ 归组演示(8 URL → 3 组,镜像归并)→ 合并证据包 → 批量声明 TUI 脚本化演示(io 注入)→ run_batch dry_run 顺序执行 3 条 → batch_report 生成 → telemetry 尾节 → "全程零外呼、零真实提交"收尾。
- **A121 docs/BATCH_GUIDE.md**:批量工作流权威指南(红线 24-26 全文;输入格式三例;筛选→分组→声明→批量→续批→结案报告全流程命令序列;频控与额度如何作用于批量;断点恢复;常见问题);docs/GROUPING.md(归纳规则:canonical 归一/镜像/团伙阈值调优/改名与展示/误并排查)。
- **A122 docs/UPGRADE_V6.md**:V6 总览(A103–A122 一览表+数据流 mermaid:intake→pool→scan→case_group→group_linker→merge_bundles→队列→BatchReview 声明→BatchState→run_batch(逐条:频控→plan→executor 人工门)→batch_report;六轮演进叙事+测试规模);与 v1-v5 兼容性(全部新模块可选,旧流程不变)。

## 5. 测试规则:同前(离线/mock/importorskip/零外呼/不碰真实门户;本地 server 仅 127.0.0.1;TUI/批量提交全部注入 fake executor,真实模式仅 dry_run)。
