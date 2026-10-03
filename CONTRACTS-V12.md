# NetSentinel V12 团队契约(A201–A209 并行开发)—— 接线收口波 · 建成能力通电 · 基准门禁化

> 九领地互不重叠实施波(五段接线收口 + 四项新能力)。收口后全仓 5458 用例全绿(基线 5198,净增 260;exit 0 零失败)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V12 新增保证(44–45,累计 45 条)

44. **草稿不生效**:form_scanner 产出仅为门户 YAML 草稿——绝不自动加载、绝不落位 portals/ 目录(CLI 层拒绝写入)、头部横幅强制"人工确认后方可生效";验证码类字段任何信号命中即整体排除(宁可错杀),绝不生成指向验证码的候选;无 id/重复 id 字段标 manual_required 回退契约缺省。
45. **trace/弃权默认可关且不触判定**:trace_enabled/abstain_enabled/TRACE_ENABLED 缺省 False=现状逐字一致(无头、无 span、无 intel 键、入队不带 kwargs);abstain 只置 needs_review 标记+队列提权,**绝不进入 verdict 三档公式**;X-Trace-Id 与 /trace 端点仅本机只读元数据;trace 上下文跨线程必须经 propagate/restore 显式对,worker 复用需清残留上下文。

## 1. 九领地归属(A201–A209;兄弟模块只读,行为向后兼容)

| 组 | 文件 | 要点 | 关键 API/实测 | 测试 |
| --- | --- | --- | --- | --- |
| A201 | contracts.py + config.py | 11 个 V10.4/V10.5 附加属性升格一等字段(`# ---- V11:` 段) | 默认值=升格前行为;枚举/数值域校验;契约同步机 V11 段双向对账 | test_config(+26)/test_contracts_sync(+4) |
| A202 | pipeline/orchestrator.py + decision/review_queue.py + decision/triage.py | trace 四阶段 span 全链 + abstain 接线 + 队列提权 | _wire_trace_audit/_wire_abstain;priority_weight 列+annotate(状态机零触碰);sort_entries(boost_by_url) | 三文件(+60) |
| A203 | service/app.py + ops/pool.py | 服务层/线程池 trace 贯通 | X-Trace-Id 头+GET /trace/{job_id};_scan_one_traced 包装(propagate 快照存在才换);audit_sink 双检锁惰性 | test_trace_wiring(12 新)+pool(+2) |
| A204 | intel/graph.py + pipeline/kernel_wire.py | 公开 add_edge + phash 登记跨批闭环 | add_edge(kind 白名单/幂等);register=PhashRegistry.register+MultiTableLSH 持久(.mtlsh);两批端到端建边 1.0 | test_graph(+5)/test_kernel_wire(+3) |
| A205 | evidence/packager.py + logging_util.py | 产包签名策略 + seal_now 手动封根 | ed25519 显式 opt-in(env>cfg),回退链完备;seal_now 同锁立即封根(空树 None),audit_verify 可验 | test_packager(+7)/test_logging_util(+5) |
| A206 | benchmarks/drift.py(新) | 分数分布漂移哨兵(PSI+条件 KL,双参考窗口) | record/evaluate CLI;渐变漂移 prev 检出早于 first(L=18 vs 20);退出码 2 门禁 | test_drift(33) |
| A207 | intel/temporal.py(新) + intel/site_memory.py | Kleinberg burst 检测+动态 TTL | kleinberg_bursts(对数域 Viterbi,k 层可扩展)/burst_factor/suggest_ttl(钳 [6,720]h);should_rescan(ttl_hours=None=现状) | test_temporal(41)+test_site_memory(+7) |
| A208 | benchmarks/adversarial.py + run_benchmark.py | 金标回归门禁(指纹键+CI 容差) | compute_fingerprint/metric_tolerance=max(10%,CI 半宽)/evaluate_gate;退出码 0/1/2;run_benchmark adversarial 子命令透传 | test_adversarial(+10)/test_benchmark(+3) |
| A209 | submit/form_scanner.py(新) | a11y 门户理解器(草稿生成) | stdlib HTMLParser+语义推断(置信度/依据/manual_required);round-trip 经 load_portal_def 全链;拒绝写 portals/ | test_form_scanner(52) |

**追认记录**:A201 因字段升格与旧"属性不存在"断言(hasattr)直接矛盾,更新 test_kernel_wire.py 两处、test_orchestrator_weights.py 三处断言语义为字段默认值断言(行为断言全保留,终态全绿,总控追认)。

## 2. 已识别的后续机会(各席位交付报告结论,未实施——第五波候选)

- **接线残余**:batchflow/finishflow/service 消费 intel["trace_id"] 落盘 span 树;merge_bundles/parallel_pack 合并产物重签(接 _sign_bundle);scheduler 消费 temporal.suggest_ttl(接线示意已在 site_memory docstring);webui 串"扫描→人工确认→落位"草稿工作流;trace_enabled/abstain_enabled 收录 Config 字段(A202 用附加属性模式)。
- **性能/架构批**:AIMD 自适应并发调速器;anyio TaskGroup 结构化 IO 层;事件溯源(audit.jsonl 升格 event log+投影视图);per-run 成本归集 Parquet。
- **评测/建模批**:pHash 对抗鲁棒性红队基准(2026 黑盒攻击复现);感知预算扰动曲线(ε 网格+SSIM);漂移哨兵 bootstrap CI 附不确定度;本地守卫模型适配器(ShieldGemma-2);贝叶斯分层可靠性跟踪;certified 预处理平滑评测(Cohen 公式)。
- **工程科学批**:mutation testing 抽样门禁;type-state 举报状态机;lint-imports 上 CI;SPRT×共形预算化审计统一框架技术报告(领域首发价值)。
- **清理批**:A204 私有写口回退一代(A214 时点移除);kernel_wire 查询步去注册库全量重灌(O(N) 每批)评估;name_suggest docstring 修复后移入 doctest 收编清单。

## 3. 验收记录(2026-10-03)

- 并行期各席位曾观测到兄弟领地在途文件的瞬态失败(陈旧 __pycache__ 字节码一次、contracts.py 并行编辑一次),均隔离复现排除因果;收口后总控全量回归 **5458 用例、exit 0、零失败字符**,无残留中间文件。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.5.tar.gz`;备份序列:backup-20261003 → v10.4 → v10.5。
- 全部接线均为默认关闭开关,45 条红线回归测试全绿;A201 领地外 5 条断言偏例已追认(见 §1)。
