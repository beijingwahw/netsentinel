# NetSentinel V11 团队契约(A192–A200 并行开发)—— 世界前沿创新波·三 · 第三方可信证据 · 图谱通电 · 反脆弱执行

> 九领地互不重叠实施波。收口后全仓 5198 用例全绿(基线 4881,净增 317;exit 0 零失败)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V11 新增保证(41–43,累计 43 条)

41. **非对称签名与时间证明**:Ed25519 签名块必须内嵌 public_key 供第三方独立验签(无需共享密钥);旧 HMAC 包(无 algo 字段)验签路径字节级兼容不可破坏;TSA 外呼仅显式 tsa_url 配置、默认 None 全离线,任何 TSA 异常安全降级本地证明并记录原因;时间证明(UTC+单调计数器)纳入签名载荷,回拨不可抵赖。
42. **候选链防绕过**:验证码别名拒绝对 selector_chain **逐候选生效**(css 同值/文本同值/含"验证码"字样,含构建期与运行期双检);链首必须 css(id) 打头与主选择器同源;胜者候选只进进程内缓存+审计 notes,**绝不回写门户 YAML**;HUMAN_GATE/FOCUS 步骤不携带候选链。
43. **弃权非档位**:abstain 是复核优先级信号,**绝不进入 verdict 三档判定公式**(clean/suspect/nsfw 契约冻结);sweep 的阈值刻度(一致度,阈↑覆盖↓)与 decide 的分歧刻度(≥阈弃权)互补对应(t=1−g),接线不得混用;单成员分歧恒 0 永不弃权。

## 1. 九领地归属(A192–A200;兄弟模块只读,行为向后兼容)

| 组 | 文件 | 创新要点 | 关键 API/实测 | 测试 |
| --- | --- | --- | --- | --- |
| A192 | security/ed25519.py(新)+timestamp.py(新)+bundle_sign.py | 纯 Python RFC 8032 Ed25519 + RFC3161-lite 时间证明 + 签名策略模式 | RFC 向量 1-3 锁定;algo=hmac-sha256(默认)/ed25519;独立验签无需 HMAC 密钥 | test_ed25519(26)+test_timestamp(25)+test_bundle_sign(+18) |
| A193 | ops/audit_verify.py(新) | Merkle 整卷离线审计命令(重放全叶+逐 checkpoint 对账) | 10 万行 1.846s;退出码 0/1/2;篡改检出精确到行号;legacy/降级段/未封卷分级 | test_audit_verify(24) |
| A194 | intel/graph_kernel.py + pipeline/kernel_wire.py + pipeline/orchestrator.py | Louvain 加权社区检测(模块度单调不减)+ 图谱通电(graph_wire 默认关) | louvain_pass/louvain_communities/wire_graph_from_scan/resolve_gangs(connectivity 默认/community 可选);写图失败安全降级 | test_graph_kernel(+8)/test_kernel_wire(+10) |
| A195 | intel/phash_lsh.py | MultiTableLSH:K 张置换掩码表+汉明球 multi-probe+sqlite 持久化 | d=16 召回 0.83%→**99.17%**,比较均值 54.8 次(<全表 1%);召回下界可证;旧 LSHIndex 零改动 | test_phash_lsh(28→47) |
| A196 | decision/abstain.py(新) | 分歧选择性弃权+risk-coverage 曲线(极差/标准差双口径) | AbstainDecision/decide/batch_decide/sweep_disagreement_threshold/to_triage_hint;默认阈 0.35 对齐 arbiter | test_abstain(76) |
| A197 | vision/cascade.py | FrugalGPT 自适应级联带(风险质量约束下最小期望成本) | band_from_history/make_adaptive_band;冷启动返回 static_default 同一对象;构造场景成本 ↓57.1%;±20% 限幅只收不放 | test_cascade(24→42) |
| A198 | telemetry_trace.py(新,包根) | 零依赖 W3C 风格 trace/span(contextvars+线程边界 propagate/restore) | new_trace/span/@traced/export_trace_json/configure;OTel 惰性薄桥接;span 双写 telemetry+可选 audit_sink | test_telemetry_trace(27) |
| A199 | mathx.py + pyproject.toml | numpy 探测式双后端(规模门 64,操作计数双路径统一)+ import-linter 声明式分层 | HAS_NUMPY 探测;fast extra=[numpy];[tool.importlinter] 4 契约(AST 全量验证后写入);pytest 配置逐字节未动 | test_mathx(42→51) |
| A200 | submit/portal_defs.py + executor_playwright.py + portals/*.yaml | 自愈选择器候选链(门户 YAML v2) | 候选类型 css/role+name/label/placeholder/text;逐候选探测回退+胜者缓存;captcha 逐候选拒绝;真浏览器改版演练 4 步全回退 | test_portal_defs(100)+test_executor_playwright(43) |

## 2. 已识别的后续机会(各席位交付报告结论,未实施——第四波候选)

- **接线收口批**(小步快跑):config.py/CONTRACTS 正式收录 `algo/ed25519_seed/tsa_url/graph_wire/gang_*` 等 cfg 附加属性并接 YAML 注入;abstain→triage→review_queue 真实接线(经 to_triage_hint);trace 接线 orchestrator/service/线程池(接线备忘已在 telemetry_trace.py 尾注);phash 登记入 PhashRegistry 闭环(cross-batch 累积);graph.py 增公开 add_edge(A194 已按未来签名探测,迁移零成本);审计 logger 增 seal_now() 手动封根。
- **性能/架构批**:AIMD 自适应并发调速器(pool/load_guard 信号驱动);anyio TaskGroup 结构化 IO 层;事件溯源(audit.jsonl 升格 event log+投影视图+saga 补偿);per-run 成本归集 Parquet(费用可观测)。
- **评测/建模批**:pHash 对抗鲁棒性红队基准(2026 黑盒攻击复现);感知预算扰动曲线(ε 网格+SSIM/LPIPS);金标回归门禁(golden JSON+退出码 2);语料漂移哨兵(PSI/KL);本地守卫模型适配器(ShieldGemma-2/Llama Guard 4 入 ensemble);贝叶斯分层可靠性跟踪(Beta 后验+指数遗忘);时序爆发检测联动巡查(intel/temporal.py,Kleinberg)。
- **举报执行批**:a11y 树门户理解器(自动生成门户 YAML 草稿,人工确认后生效);计划内断点续填+提交前回读校验;门户 schema 指纹与漂移告警;候选链探测与 wait_until 兜底链协同。
- **工程科学批**:mutation testing 抽样门禁(mutmut,MSI≥85);type-state 举报状态机(构造即安全);lint-imports 上 CI;SPRT×共形预算化审计统一框架技术报告(领域首发价值)。

## 3. 验收记录(2026-10-03)

- 九领地并行开发期各席位曾观测到兄弟领地在途文件的瞬态收集错误/偶发失败(各自隔离复跑均绿,非因果);收口后总控全量回归 **5198 用例、exit 0、零失败字符**。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.4.tar.gz`(V10.4 已验证状态);本波收口快照含于备份序列。
- 全部创新均为纯新增或默认关闭开关/静态带回退,旧行为字节级兼容;40+3 条红线回归测试(test_redline_v9 等)全绿。
