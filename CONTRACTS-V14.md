# NetSentinel V14 团队契约(A219–A227 并行开发)—— 指纹盲区修复 · 认证防御 · 成本归集波

> 九领地互不重叠实施波。收口后全仓 6004 用例全绿(基线 5775,净增 229;exit 0;test_phash_invariant 终态 3 轮压测全绿——并行期两席位观测到的失败为 A219 阈值校准中途版本)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V14 新增保证(48–49,累计 49 条)

48. **多哈希候选语义**:mirror/ring/pyramid 三不变哈希的命中是**候选生成而非判定**——mirror 丢弃翻转方向信息(互为翻转的异图不可分)、ring 32bit 码本小且对辐射对称构图趋同、pyramid 对结构主导构图(照片族)的裁剪仅部分恢复;候选必须经 phash256 与人工复核确认;跨图误报阈值按确定性语料校准并留余量(跨 Pillow 版本重采样微差 ±1-2bit 已在断言余量内)。
49. **草稿落位双门槛**:webui 草稿落位=显式确认口令(须与门户名完全一致)+临时文件经 load_portal_def 全链校验通过,任何失败拒绝写入且零残留;确认状态机(confirming→ready→placed)逐字段确认、无批量捷径(源码断言锁定)、落位后封存不可变更;落位≠生效(仍需 PORTAL_FILES 登记加载)。

## 1. 九领地归属(A219–A227;兄弟模块只读,行为向后兼容)

| 组 | 文件 | 要点 | 关键实测 | 测试 |
| --- | --- | --- | --- | --- |
| A219 | vision/phash2.py + intel/phash.py | mirror/ring/pyramid 三不变哈希+Registry schema v2 | flip 0→**1.000**(恒等);±3° 0.02→**1.000**;±8°→0.844;crop→部分恢复;误报距 14>12 | test_phash_invariant(17)+两文件(+6) |
| A220 | webui/providers_page.py + webui/draft_flow_page.py(新) | 贝叶斯权重区间页签+三步草稿工作流 | 状态机转移矩阵/源码无批量捷径断言/落位校验失败零残留 | test_draft_flow_page(43)+providers(+9) |
| A221 | contracts.py + config.py | V13 四字段收录(dynamic_ttl 等) | phash_mt_lsh_db None→推导路径逐字节兼容;guard_family 双源运行时对账 | test_config(+16)/sync(+4) |
| A222 | decision/four_eyes.py + storage/replay.py | 四眼事件账本透传+--apply-ahead 补齐 | 崩溃窗口端到端补齐归零;账本事件零重复;--yes 绝不半途 | test_four_eyes(+6)/test_replay(+18) |
| A223 | pipeline/orchestrator.py + vision/classifier_base.py | 贝叶斯回流(优先级门控)+guard 工厂 | 开关优先级矩阵;bayes 41/64 权重生效;子进程工厂全链 | orchestrator_weights(+9)/guard_adapter(+4) |
| A224 | packager/merge_bundles/parallel_pack + kernel_bench + test_aimd | 签名公开口+自检注册+防闪烁 | kernel_bench **17 内核全绿**+HAS_NUMPY 标注;test_aimd 3 轮压测绿 | 四测试文件(+~30) |
| A225 | vision/cost_meter.py + finishflow.py + pyproject(extras) | per-run 成本归集 | cost.jsonl 永写/parquet 可选降级;aggregate 手算对照 | test_cost_meter(+11)/test_finishflow(+7) |
| A226 | benchmarks/drift.py + adversarial.py | bootstrap CI+曲线金标 | 恒等分布 CI 随 n 收窄;null_flip 双向违例;两金标字节级隔离 | test_drift(+9)/test_adversarial(+10) |
| A227 | benchmarks/certify_bench.py(新) | Cohen 认证平滑评测 | Φ⁻¹ 精度<1e-12;认证率 83.3%/半径 7.55;p_lower>p_upper 判据论证 | test_certify_bench(35) |

## 2. 已识别的后续机会(各席位交付报告结论,未实施——第七波候选)

- **指纹纵深(A219 点名)**:大角度旋转(±15° ring 仅 0.19-0.38)→ DFT 模严格平移不变或加密角度分桶;裁剪全档恢复需子窗口/兴趣点哈希;三哈希生产接线(orchestrator 登记步消费 mirror/pyramid 列、kernel_wire 多哈希建边);LSH 适配多哈希列(MultiTableLSH 泛化)。
- **接线残余**:vision 调用链 record() 透传 run_id(当前多数落"未标记批次"桶);bayes_reliability 待 V14 收录 Config(附加属性模式);批预算熔断复用 aggregate(by=run_id);webui providers 页读 runs/*/cost.jsonl 批次展示;落位门户 PORTAL_FILES 登记;review_tui/dashboard 接 event_log 生产入口;--apply-ahead 交互确认模式。
- **评测纵深**:守卫模型真实上线标定(Yes/No→0.95/0.05 阈值);certify 接 glm 重建基线+半径金标;红队/感知曲线/漂移哨兵接入真实脱敏语料;曲线金标扩展 score-vs-SSIM 形状指纹。
- **清理**:私有别名 _sign_bundle/_zip_bundle V15 移除;approvals 投影纳入重放对账;replay 对 phash 席位阈值跨环境校准基线(A224 移交项——终态已稳定,留观)。
- **性能收尾**:AIMD executor 重建闭环;worker 时长回传;anyio 结构化 IO。

## 3. 验收记录(2026-10-03)

- 收口后总控全量回归 **6004 用例、exit 0、零失败字符**;test_phash_invariant(A224/A220 曾观测失败,系 A219 校准中途版本)终态 3 轮隔离压测全绿。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.7.tar.gz`;备份序列:backup-20261003 → v10.4 → v10.5 → v10.6 → v10.7。
- 全部新能力默认关闭或 opt-in,49 条红线回归测试全绿;无领地外偏例(本波无追认项)。
