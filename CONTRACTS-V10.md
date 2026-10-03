# NetSentinel V10 团队契约(A183–A191 并行开发)—— 世界前沿创新波 · 统计担保 · 透明日志 · 反脆弱执行

> 九代理只读分析波(9 领域对标 2026 前沿)+ 九领地互不重叠实施波。收口后全仓 4881 测试绿(基线 4621,净增 260)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V10 新增保证(38–40,累计 40 条)

38. **统计诚实性**:conformal 担保以认证层落地(`ltt`/`ltt_advisory`/`empirical_fallback`),生效 threshold 恒保持既有经验语义;真率 100% 的 CP 下界按闭式 δ^(1/n) 报告,禁止有限样本宣称绝对精度;对抗评测单点估计必须伴随 CI/检验字段(新增字段,不删旧字段)。
39. **透明日志完整性**:审计 JSONL 的 Merkle 接入为安全失效设计——Merkle 任何异常不丢审计本体,一次性 `merkle_degraded` 标记后停用树;checkpoint 根经 HMAC-SHA256 签名,包含性证明可离线独立验证(verify 函数零内部状态、绝不抛错只返回 False)。
40. **等待反脆弱与红线隔离**:`wait_until` 为可选增强,缺省路径字节级不变;三级兜底链(事件条件→selector 可见→盲睡)每级落 step notes 可审计;`validate_wait_until` 拒绝未知条件键;HUMAN_GATE/`_assert_no_captcha_autofill`/captcha 别名拒绝逻辑对任何等待升级不可触碰。

## 1. 九领地归属(A183–A191;兄弟模块只读,行为向后兼容)

| 组 | 文件 | 创新要点 | API/出口 | 测试 |
| --- | --- | --- | --- | --- |
| A183 | decision/conformal.py | CP 精确 (1-δ) 下界 + LTT/Bonferroni 认证层(lgamma 对数空间防溢出) | `fit_threshold(..., delta=0.05)`;新字段 lower_bound/confidence/p_value/ltt_*;n<30 降级语义不变 | tests/test_conformal.py(77) |
| A184 | decision/sprt.py | MixtureSPRT(Beta-Jeffreys 对数闭式 e-process,Ville 阈值)+ Howard 线性 stitching 置信序列 | `log_evalue`/`MixtureSPRT`/`stitched_radius`/`anytime_ci`/`ConfidenceSequence`/`ci_band_stop`;Wald SPRT 一字未动 | tests/test_sprt.py(38) |
| A185 | config.py + pipeline/orchestrator.py | Brier 反比可靠性权重回流 ensemble(开关默认关=等权;≥MIN_N 自动启用,缺失成员均值兜底) | config 键 `ensemble_reliability_weights`;orchestrator 适配层 + telemetry `scan.ensemble_weights.*` 三计数 | tests/test_orchestrator_weights.py(16) |
| A186 | decision/triage.py + decision/review_queue.py | 决策论分诊排序:四因子(p(NSFW)×危害×翻案后验×老化)纯函数;`list(sort="triage")` 仅关键字参数,默认 FIFO | `TriageWeights`/`OverturnStats.from_sqlite`(只读 GROUP BY,不改 schema)/`sort_entries`;CLI `--sort {fifo,triage}` | tests/test_triage.py(37)+test_review_queue.py 增 10 |
| A187 | ops/sched_kernel.py | 四因子优先级(+age_h aging,W_AGING=0 位级等价)+ 变成本价值密度背包(等成本退化逐项相等) | `priority(item, *, weights)`/`select_round(..., weights)`;kernel_selfcheck 增两确定性附加键 | tests/test_sched_kernel.py(101) |
| A188 | security/merkle.py + logging_util.py | 离线透明日志:append-only Merkle(O(log n) 折叠)+HMAC checkpoint(每 128 条)+独立证明验证 | `MerkleTree.append/root_hex/include_proof/checkpoint`;`sign_checkpoint`/`verify_proof`/`verify_checkpoint_signature`;JSONL 行 `merkle_leaf` 字段 | tests/test_merkle.py(15)+test_logging_util.py 增 10 |
| A189 | benchmarks/adversarial.py | 统计推断层:配对 bootstrap CI(B=10000)+符号翻转置换检验(精确≤12/MC 9999)+Wilson CI,固定种子逐位可复现 | `bootstrap_mean_ci95`/`signflip_permutation_pvalue`/`wilson_ci95`;payload 增 ci95/p_value/wilson_ci+inference 元数据;markdown 老 payload 回退 | tests/test_adversarial.py(79) |
| A190 | submit/form_models.py + executor_playwright.py | 反脆弱等待:`wait_until`(`selector_visible`/`text_present`/`requests_idle` Any-of 组合)→退避重试→三级兜底链全落 notes | `WAIT_UNTIL_KEYS`/`validate_wait_until`/`step_wait_until`;`_event_driven_wait`;缺省盲睡与 dry_run 字节级不变;HUMAN_GATE 回归锁定测试 | tests/test_form_models.py+test_executor_playwright.py(137,增 31) |
| A191 | pyproject.toml(dev extra)+ 新测试 ×3 | doctest 激活机(5 模块 21 示例+全包分诊守门)/性质测试双引擎(hypothesis 优先,Random(42) 兜底)/契约同步机(AST↔V9.md 双向对账) | tests/test_doctests.py / test_mathx_props.py / test_contracts_sync.py;不改全局 addopts | 三文件(65 passed+1 skip) |

## 2. 已识别的后续机会(只读分析波结论,未实施)

- **第三方可信证据**:RFC3161 可信时间戳(TSA 可选离线)、Ed25519 非对称签名升级(HMAC 兼容旧包)、OS 密钥库(DPAPI/keyring)托管——司法举证效力跃升的最大单点。
- **图谱通电**:intel/graph.py 的 add_image/add_template/link_* 与 phash_lsh 在生产管线无调用点("已建成未通电");建议 orchestrator 接通 + 加权社区检测(Leiden-lite)替换纯连通并团。
- **多表 multi-probe LSH**:单表分带→K 张置换掩码表+邻桶探测+sqlite 持久化,d=16 召回 0→>99%。
- **本地守卫模型**:ShieldGemma-2/Llama Guard 4 开放权重适配器入 ensemble(适配器架构零冲突)。
- **FrugalGPT 自适应级联**:不确定带由成员"选择性风险-成本"曲线在线推导;分歧超阈 ABSTAIN 第四档直送人工优先队列。
- **AIMD 自适应并发/anyio 结构化 IO/mathx 可选 numpy 后端**(探测式双后端,kernel_selfcheck 断言一致)。
- **事件溯源**:audit.jsonl 升格 event log,review/batch 状态为投影视图+saga 补偿;W3C trace 贯通(OTel 缺席时零依赖退化)。
- **SPRT×共形预算化审计统一框架成文**:序贯早停+精度担保组合在内容审核领域近乎空白,具备技术报告首发价值。

## 3. 验收记录(2026-10-03)

- 九领地并行开发期曾出现并发写文件导致的瞬态失败,收口后全量回归三次 **4881 passed + 5 skipped**(exit 0,无失败字符);`test_conformal.py::test_fit_threshold_monotone_in_target` 曾报偶发,隔离与复跑均绿,判定并行期瞬态。
- 安全备份:`C:\1\netsentinel-backup-20261003.tar.gz`(实施前快照,排除缓存与 .git)。
- 测试规模:4621 → 4881(净增 260);全部创新均为纯增量或默认关闭开关,旧行为字节级兼容。
