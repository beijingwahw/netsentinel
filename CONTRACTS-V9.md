# NetSentinel V9 团队契约(A163–A182 并行开发)—— CPU 自适应三档并发 · 收官汇总 · 结案代理依次举报

> 八轮后全仓 3968 测试绿。V9 字段已落地:concurrency_tier(mid)/concurrency_auto/cpu_reserve(1)/summary_agent_enabled(True)。

## 0. V9 新红线(35–37,累计 37 条)

35. **压榨边界**:高档并发只作用于本地计算与本地回环 IO(并行分类/并行打包/扫描池);对外网络的礼貌间隔(fetch_delay_s)、引擎限速、举报频控(红线 26)**一概不放宽**;进程池仅用于模块级纯函数的本地计算,不得 pickle 分类器实例/连接。
36. **结案代理无自主提交权**:SummaryAgent 只做分类汇总与举报**准备**(列待声明组);SequentialReportAgent 只编排 run_batch——逐组人工声明、每条 HUMAN_GATE、频控全部沿用 V6 链;代理源码不得出现 auto_confirm=True 或绕过 attest 的路径。
37. **资源治理**:任一执行器 workers ≤ cpu 核数(注入显式值除外需 warning);CPU 过载让位保护可中断;所有并行任务有总量与超时上限,失控不得留孤儿进程/线程。

## 1. 三档定义(权威)

以 `os.cpu_count()`(缺省回退 2)为 N:
- **low**:max(1, N//4) workers —— 后台/省电;
- **mid**:max(1, N//2) workers —— 默认;
- **high**:max(1, N - cfg.cpu_reserve) workers —— **最大限度压榨 CPU**(reserve=0 即全核)。

## 2. 文件归属(A163–A182;兄弟模块只读,惰性导入)

| 组 | 新文件 | API 要点 | 测试 |
| --- | --- | --- | --- |
| A163 | ops/cpu_profile.py | `detect() -> {"cores","arch","platform","psutil":bool}`(psutil 惰性可选);`tier_workers(tier, *, reserve=1, cores=None) -> int`(§1 公式);`recommend(profile) -> "low"\|"mid"\|"high"`(核数≤2→low;≤8→mid;>8 且负载低→high,负载缺省按中);`validate_tier(tier)` | tests/test_cpu_profile.py |
| A164 | ops/concurrency.py | `io_workers(cfg) -> int`(=tier_workers);`cpu_workers(cfg) -> int`(进程池=min(tier_workers, cores),红线 37);`thread_pool(cfg, *, label)`/`process_pool(cfg, *, label)` 上下文管理器(线程名前缀、关闭兜底);tier 非法→中文 ValueError | tests/test_concurrency.py |
| A165 | ops/tier_state.py | `TierState(path)`(data/concurrency.json):get/set {"tier","workers","cores","set_by"};`tier_once(cfg)`:concurrency_auto 且无持久档→detect+recommend 写入(set_by="auto");用户 cfg.concurrency_tier 显式非默认?无从判定——auto 只在持久文件缺失时写建议,**读取优先级 cfg 字段 > 持久文件**;审计 log_event 惰性 | tests/test_tier_state.py |
| A166 | vision/parallel_classify.py | `classify_parallel(classifier, evidences, cfg, *, executor=None, use_processes=False) -> list[ImageScore]`(保序):默认线程池(use_processes=False,IO 型 VLM);use_processes=True 走进程池——**经模块级纯函数 `_proc_classify((name, path, url))` 在子进程内按名构造分类器**(红线 35,不 pickle 实例);异常逐条容错→0 分+error | tests/test_parallel_classify.py |
| A167 | ops/load_guard.py | `LoadGuard(interval_s=0.5)`:进程 CPU 采样(os.times() 差分);`should_throttle() -> bool`(利用率>0.92→True,高档让位);`maybe_yield()`(True 时 sleep 0.05);时钟可注入;纯 stdlib | tests/test_load_guard.py |
| A168 | agent/summary_agent.py | `@dataclass SummaryOutcome{groups,report_md,report_html,ready_count,attest_pending,workers,tier,next_steps}`;`SummaryAgent.run(scan_result: dict, cfg, *, grouper=None, packager=None, stats_fn=None) -> SummaryOutcome`:分类(V6 group_and_enqueue 惰性复用/注入)→汇总统计(A117 group_stats)→收官报告(A175 render_run_summary)→**举报准备**:列出待声明组(经 A110 ready 语义,仅统计不激活)→next_steps 中文(batch_tui 声明→finishflow --report);summary_agent_enabled=False→最小 outcome;**本模块零提交路径**(红线 36) | tests/test_summary_agent.py |
| A169 | finishflow.py(包根 CLI) | `finish(urls: list[str], cfg, *, scan=None, summary=None) -> dict`:tier_once→batch_scan(workers=io_workers)→SummaryAgent.run→打印中文收官汇总表→返回 outcome;`run_report(cfg, *, runner=None) -> dict`:ready→SequentialReportAgent(A174)run_batch(干跑默认);`main(argv)`:`--input --config --tier low\|mid\|high --report [--resume 批次] [--dry-run\|--exec]`;`python -m netsentinel.finishflow` 入口 | tests/test_finishflow.py |
| A170 | agent/sequential_report.py | `SequentialReportAgent`:run(items, cfg, *, executor=None, state=None, dry_run=None, on_item=None) -> dict:**直接编排 A112 run_batch(恒 auto_confirm=False)**+V7 SessionExecutor 可选(browser_session_reuse 时注入 executor_cls);on_item 回调逐条进度;结束后 A114 batch_report 渲染并返回路径;红线 36 docstring+源码断言(无 auto_confirm=True) | tests/test_sequential_report.py |
| A171 | evidence/parallel_pack.py | `pack_all(reports: list[SiteReport], cfg, *, builder=None, executor=None) -> list[EvidenceBundle]`(保序,线程池 IO 型,单包失败→None 占位+中文 warning 计数);builder 注入缺省惰性 packager.build_bundle | tests/test_parallel_pack.py |
| A172 | report/run_summary.py | `render_run_summary(stats: dict, out_base: str) -> (md_path, html_path)`:输入{sites,scanned,failed,verdict_dist,groups,tier,workers,cost_est,budget_used,telemetry_top,attest_pending,ready_count,wall_s?}:MD 中文表+HTML 公文风(内联 CSS);固定含"本汇总由结案代理生成,举报须经逐组声明与逐条人工门" | tests/test_run_summary.py |
| A173 | benchmarks/tier_bench.py | `peak_concurrency(task_fn, n, workers) -> int`(Barrier 计数法:workers 个并发到达才放行,峰值=workers);`run() -> dict`:三档 peak 表+理论 workers 对照(确定性,零墙钟断言用计数);报告 out/tier_report.md | tests/test_tier_bench.py |
| A174 | —(并入 A170 行,编号保留) | — | — |
| A175 | webui/runs_page.py | 纯逻辑层 `run_rows(summaries: list[dict]) -> list`(批次/站点/判定分布/组数/档位徽章)、`tier_badge(tier)`(低🟢 中🟡 高🔴+中文)、`cpu_advice(profile) -> str`;Streamlit 惰性 render | tests/test_runs_page.py |
| A176 | tests/test_v9_e2e.py | 端到端:monkeypatch cpu_count=2→tier high workers=1 退化正确;本地 2 站 tier=mid 真扫描→finishflow→outcome(groups≥1,report 落盘,ready_count 与 attest_pending 正确)→run_report 干跑 SequentialReportAgent(fake executor)逐条执行;红线 35 断言(fetch_delay 透传不变);红线 36 断言(summary_agent 源码无 run_batch/executor 调用) | (单文件) |
| A177 | docs/CONCURRENCY.md | CPU 档位权威文档:检测机制/三档表(N 示例 4/8/16/32)/高档压榨边界(红线 35:哪些被加速、哪些不变)/load_guard/与礼貌频控的区别/tier_bench 解读/调优(reserve/auto)/Windows 注意 | — |
| A178 | docs/FINISH_AGENT.md | 结案代理指南:全流程命令(finishflow --input → 汇总 → batch_tui 声明 → finishflow --report --resume --exec)/红线 36 全文/SequentialReportAgent 与 V6 批量链的关系图/进度回调/断点续报 | — |
| A179 | scripts/demo_tiers_finish.py | 离线演示:①detect+三档表 ②tier_bench 峰值证明(barrier 计数)③本地 2 站 tier=high 扫描(cpu_count 注入 4→workers 3)④收官汇总打印 ⑤SequentialReportAgent 干跑逐条(注入应答)⑥收尾"高档只压榨本地计算;红线 35-37 全程有效" | 实跑验证 |
| A180 | docs/UPGRADE_V9.md | V9 总览:A163–A182 表/mermaid(输入→tier_once→batch_scan(io_workers)→SummaryAgent{分组/统计/报告/待声明}→人工声明→SequentialReportAgent{run_batch 逐条人工门+SessionExecutor}→batch_report;旁路 parallel_classify/parallel_pack/load_guard/tier_bench)/十轮演进/兼容/配置速查 | — |
| A181 | tests/test_redline_v9.py | 红线专项:①35:pool/fetch 参数透传(高档下 fetch_delay_s 不变,源码断言 tier 不影响 rate_limit/fetch_delay);②36:summary_agent/sequential_report 源码无 auto_confirm=True、无 attest 代答;③37:tier_workers≤cores 全档、cpu_workers≤cores、load_guard 高载 yield(sleep 被调) | (单文件) |
| A182 | —(预留:由负责人收口 CHANGELOG/接线) | — | — |

冻结:contracts.py、telemetry.py、conftest.py、全部既有模块与文档、并行会话 V8 字段。

## 3. 集成接线(负责人完成)

- `ops/pool.run_pool` 的 workers 缺省 2 → None(缺省时经 ops.concurrency.io_workers(cfg) 解析,显式传参不变);
- `finishflow` 作为批量跑完的收官入口(独立 CLI,不改 batchflow)。

## 4. 流程:契约→实现→test_v9_*(红线专项必含)→全仓回归全绿(终跑时间点注明)→报告(升级项/量化/新增测试数/终态)。
