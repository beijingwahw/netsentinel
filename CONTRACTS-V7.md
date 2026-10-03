# NetSentinel V7 团队契约(A123–A142 并行开发)—— 全部内核世界性进化

> 前六轮后全仓 2790 测试全绿。本轮对**八大核心引擎**做代际升级:每个新内核
> 必须附离线可复现微基准(kernelbench),全部默认关闭/开关化,旧调用方零感知。

## 0. V7 新红线(29–31,累计 31 条)

29. **零 API 破坏**:新内核全部为新增文件/新增 keyword-only 参数/新增注册名;既有模块(含 A01–A122 全部文件)一律不改;开关默认值 = 旧行为。
30. **学习型内核零外呼**:可靠性加权/文本 n-gram 等只从运营者本地反馈(review_queue 决策)与内置合成语料学习;绝不联网取数、绝不调用 VLM 训练。
31. **基准可复现**:每个新内核在其测试文件中至少一个 `test_v7_bench_*` 用例,以**操作计数/复杂度断言**(循环次数、调用次数、表扫描行数)或确定性构造数据上的精确结果证明代差;禁止依赖墙钟的脆弱断言。

## 1. 新增 Config 字段(已落地)

`use_sprt(False) / sprt_alpha(0.05) / sprt_beta(0.05) / use_reliability_fusion(False) / phash_lsh_bands(4) / browser_session_reuse(True) / sched_priority(False)`

## 2. 文件归属(A123–A142;兄弟模块只读,一律惰性导入)

| 组 | 新文件 | 内核与 API 要点 | 测试 |
| --- | --- | --- | --- |
| A123 | vision/heuristic_kernel.py | **识别内核·肤色启发式**:`SkinHeuristicClassifier(NsfwClassifier)` 注册名 "skin";PIL 惰性(缺失仅支持 stdlib-zlib PNG);YCbCr/CbCr 肤色概率图阈值 + 肤色占比/最大连通块占比(粗网格)/边缘密度(灰度梯度);nsfw_prob=校准组合;scores={"skin_ratio","max_blob","edge_density"};bench:合成肤色图(≥8 张,人体色调椭圆分布)vs 风景/文本图(≥8 张)线性可分断言(均值差>0.2) | tests/test_heuristic_kernel.py |
| A124 | intel/text_kernel.py | **语言内核·n-gram**:`TextKernel(seed_pos, seed_neg)` 字符 2/3-gram TF-IDF 向量 + 余弦;seed_pos 由既有 LURE/PORN 词表文档化生成,seed_neg 由新闻样例文(自写 5 段);`score(html_or_text) -> {"risk": 0~1, "cos_pos","cos_neg"}`;与 text_intel 并存(不修改它);bench:构造正负语料各 ≥10 段,AUC 断言(排序正确率 ≥0.9) | tests/test_text_kernel.py |
| A125 | decision/sprt.py | **决策内核·序贯检验**:`class SPRT(alpha, beta)`:`llr(p)` 单伯努利对数似然比;`update(nsfw_prob) -> "continue|nsfw|clean"`(边界 A=ln((1-β)/α), B=ln(β/(1-α)));`decide_sequence(probs) -> (verdict, n_used)`;`next_images(candidates, cfg)` 供 rank_for_vlm 后接(升序不确定度送审,每张后 SPRT 判停);**校准假设写明 docstring**(概率须经校准,stub 未校准时建议关) | tests/test_sprt.py |
| A126 | decision/reliability.py + decision/fusion_reliable.py | **融合内核·可靠性加权**:`ReliabilityTracker(jsonl_path)`:record(provider, p, outcome_bool) → Brier 累计;`weights() -> {provider: 1/(mse+ε) 归一}`;`fuse_reliable(report, url_feat, text_feat, page_vlm, cfg, tracker)` 与 fusion.fuse 同构但 image 侧各成员按可靠性加权合成 agg(成员分在 image_scores);**只升不降**与融合输出 intel["fusion"]["rule"] 注明 "reliable-weighted";离线回退=固定权重 | tests/test_reliability.py |
| A127 | intel/phash_lsh.py | **检索内核·分带 LSH**:`LSHIndex(bands)`:insert(hex64, payload)/query(hex64, max_distance) → 候选=任一带桶命中再汉明过滤;bench:10^3 条索引查询 vs 全表扫描的**比较次数**断言(≤ 全表 10%);与 PhashRegistry 互操作(提供 `build_from(registry)`) | tests/test_phash_lsh.py |
| A128 | intel/graph_kernel.py | **图谱内核·增量并查集**:`UnionFindKernel()`:`union(a,b)/find(a)/components() -> {root: set}` 全 O(α);`ingest_edges(iter)`;`related(host, depth)` 基于分量+邻接表;bench:10^4 次 union 后 components O(1) 查询,对比重算连通分量次数=0 | tests/test_graph_kernel.py |
| A129 | submit/executor_session.py | **执行内核·会话复用**:`class SessionExecutor`:`__init__(cfg, *, launcher=None)`;`run(plan, *, auto_confirm=False, dry_run=None) -> ExecutionResult`;首个 plan 启动浏览器(注入 launcher 便于测试),后续 plan 复用 context 仅 new_page;human_gate 语义与 dry_run 分支与 executor_playwright 完全一致(复用其 step 语义,可 import 其辅助);`close()`;bench:3 个 plan 注入 FakeLauncher 断言 launch 恰 1 次、new_page 3 次 | tests/test_executor_session.py |
| A130 | ops/sched_kernel.py | **调度内核·优先级预算**:`priority(item: {url, volatility, url_risk, staleness_h}) -> float`(三因子加权,常量导出);`select_round(items, budget, *, min_interval=1.0) -> list`(性价比排序 + 间隔预算背包);bench:10 项 budget=4 恰选 4 项且按优先级序 | tests/test_sched_kernel.py |
| A131 | storage/kernel.py(+ storage/__init__.py) | **存储内核·统一存储**:`SQLiteKernel(path)`:WAL+busy_timeout(与全仓一致)、`migrate(version, ddl_list)` 版本表 schema_version、`repo(table)` 生成 thread-safe CRUD 助手、`vacuum_if_needed(min_fragments)`;既有库兼容(打开不迁移);bench:migrate 幂等(二次打开零 DDL 执行,以执行计数断言) | tests/test_storage_kernel.py |
| A132 | vision/cache2.py | **缓存内核·双层**:`class MemoLRU(capacity)`(OrderedDict,线程安全,命中率计数);`CachedClassifier(inner, lru)` 包装任意 NsfwClassifier(命中零 inner 调用);bench:重复 classify 同图 inner 调用次数 5→1 | tests/test_cache2.py |
| A133 | mathx.py(包根) | **推理内核·张量微库**:纯 stdlib:`dot/matmul/softmax/sigmoid/standardize`(生成器实现,任意嵌套 list);与手算精确一致断言;bench:64x64 matmul 操作计数 == 64^3 | tests/test_mathx.py |
| A134 | vision/phash2.py | **指纹内核 v2**:256bit pHash(32x32 灰度 → 4 个 16x16 块 DCT,每块取 4x4 低频 → 4×16bit);`phash256(path) -> hex`;`hamming_hex`;与 64bit 的映射导出 `to64(hex256)`;bench:同图缩放距离小/异图距离大(≥8 组) | tests/test_phash2.py |
| A135 | submit/style_kernel.py | **风格内核·举报文本**:`style_score(text) -> {"len_ok","has_facts","no_hype","formality"}`(规则:数值密度/夸张词/口语词/法言法语模板命中);`polish(text)` 轻量改写(去夸张词、截断、补"以上情况本人已人工核实。");与 describer_critic 语义互补不重叠 | tests/test_style_kernel.py |
| A136 | security/threat_kernel.py | **安全内核·模糊测试**:`fuzz_url/fuzz_html/fuzz_yaml/fuzz_json` 生成器(各 ≥30 变体:超长/控制字符/嵌套深度/坏编码);`smoke(module_fn, cases)` harness:跑关键内核入口(canonical/text_intel/response_repair/policy.load)零异常断言(允许抛 ValueError/RuntimeError 中文,不得 segfault/裸 Exception);`main` CLI 跑全量并输出报告 | tests/test_threat_kernel.py |
| A137 | telemetry_export.py(包根) | **观测内核·指标导出**:`to_prometheus(snapshot) -> str`(# HELP/TYPE + 指标行,counter→_total);`diff_snaps(a,b)`;挂载说明(service 端点文档);bench:快照往返解析 | tests/test_telemetry_export.py |
| A138 | benchmarks/kernel_bench.py | **内核基准总控**:统一调用各 v7 内核的自检函数(各内核模块提供 `kernel_selfcheck() -> {"name","metric","value","baseline"}`);产出 benchmarks/out/kernel_report.md/json(中文对比表);CLI;失败阈值退出码 2 | tests/test_kernel_bench.py |
| A139 | pipeline/kernel_wire.py | **内核装配线**:`assemble(cfg) -> KernelBox`(dataclass:classifier 工厂链/sprt 可选/fusion 选择/executor 选择(sched/browser 开关)/lsh 可选);**不改 orchestrator**——提供 `run_scan_v7(url, cfg)` 包装(内部走 orchestrator.run_scan 并按开关套 CachedClassifier/SPRT 后处理/fuse_reliable);开关全关时行为与 v6 完全一致(断言) | tests/test_kernel_wire.py |
| A140 | scripts/demo_kernel_evolution.py | **内核演练场**(离线):依次演示 skin 评分对比表、SPRT 早停(20 张排序第 5 张停)、LSH 查询计数 vs 全表、会话复用 launch 计数、优先级轮选、Prometheus 导出片段;收尾声明 | 实跑验证 |
| A141 | docs/KERNEL_EVOLUTION.md | 内核进化白皮书:八内核一章(进化前后/算法/开关/bench 数字/局限);总对比表;开关矩阵;调优指南 | — |
| A142 | docs/UPGRADE_V7.md | V7 总览:A123–A142 一览、mermaid(装配线数据流)、七轮演进全景、兼容性声明、配置速查 | — |

冻结:contracts.py、telemetry.py、conftest.py、全部既有模块与文档。

## 3. 工作流程(同 V5/V6):契约→实施→test_v7_* 锁定(含 test_v7_bench_*)→全仓回归全绿(终跑时间点注明)→报告(升级项/量化/新增测试数/终态)。
