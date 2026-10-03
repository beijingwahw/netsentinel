# 变更总账(NetSentinel 净网哨兵)

## V10.10(2026-10-03)—— 遗留清偿波·二·收官(A237–A245 并行;详细契约见 CONTRACTS-V16.md)

- **策略哈希入审计+OS 密钥库(A237,第一波审计 P4 清偿)**:policy_decide 审计事件附 policy_sha256(注入式 audit_sink,默认零行为);keys.py 读取链=cfg>env>**keyring(惰性可选)**>文件,store_key_to_keyring 显式写入助手。
- **曲线形状指纹+KL 双侧重采样+缺基线策略拆分(A238)**:shape_digest(逐 ε 段斜率符号 hex)堵住"AUC/端点不变的曲线平移"盲区(构造场景验证),曲线金标版本 v13-2,新违例 kind=shape_changed(容差=翻转段数);KL 可选双侧重采样(配对差分语义);--curve-missing-baseline 独立。
- **认证噪声 LUT 加速+半径金标(A239)**:分位数 LUT+C 级混合——**内核 42.9× 加速**(1026µs→23.9µs/张);诚实论证放弃逐字节等价路线(实测 CPython 3.14 单次 gauss 194ns 物理下界),σ>40 回退参考路径;认证准确率/平均半径金标门禁(退出码 0/1/2)。
- **守卫模型标定工具(A240)**:guard_calib.py——混淆矩阵/当前常量 Brier/MLE+拉普拉斯建议常量对/±0.05 敏感性翻转计数;n<30 "样本不足仅演示"降级;建议≠采纳(人工决策);附真实模型上线五步操作指引。
- **爆发层级嵌套+参数标定(A241)**:kleinberg_bursts_nested(同一 Viterbi 路径按 level 分层切树,t=1 与扁平输出逐位一致);calibrate_params 网格回放(命中/浪费互斥窗口+帕累托前沿+三档诚实建议)。
- **贝叶斯流检查点化(A242)**:CheckpointedBayesianReliabilityTracker——指数核泛函方程下"锚点和×单因子=全量重放"恰等价(数学推导入 docstring);**1e5 事件 weights() 612× 加速**(29.64ms→0.048ms);快照 save/load;乱序因果三路径一致性锁定。
- **vision 成本落账+配置样例(A243)**:vlm_client 主落账点(_chat_json 200 成功处:provider/model/图片数/tokens/时长)+vlmctl ping 补记点(防双记标记);cost_ledger_enabled 附加属性门控(缺省关=现状零落账);config.example.yaml 补 V11–V14 全部 17 键注释样例。
- **mirror_near 独立边种类(A244)**:第 5 种边+旧库 CHECK 整表重建迁移(单事务,零数据丢失);resolve_gangs 双管齐下——community 乘法降权(factor 0.5)+**connectivity 默认不并团**(镜像候选须显式开启才并团,红线 48 强化)。
- **留痕补齐+部署注记+webui 口径(A245,收官席)**:--heal-approvals 独立第二确认链(账本先记 approvals_healed 事件、actor 恒为 replay-heal 与真人可区分);uvicorn 多进程部署注记;webui 贝叶斯重建消费行内 ts+bayes_half_life 同口径。
- 全仓 **6442 用例全绿**(基线 6236,净增 206;**连跑两轮 exit 0 零失败**);波前快照 `C:\1\netsentinel-backup-20261003-v10.9.tar.gz`。
- **至此八波 72 席位的全部风险与后续建议清偿完毕**(有据豁免项见 CONTRACTS-V16.md §3)。

## V10.9(2026-10-03)—— 遗留清偿波·一(A228–A236 并行;详细契约见 CONTRACTS-V15.md)

- **DFT 模严格旋转不变+子窗口裁剪哈希(A228)**:dft_ring_hash(角向 DFT 模长,循环移位定理——**±15° 召回 0.125→1.000**,含证明概要);tile_hash(12×64bit 重叠滑窗+尺度梯,crop 0.2 召回 **0.125→0.750**);位宽权衡实测锁定(8bit tile 异图距塌缩 0,64bit×12 为最小可用)。
- **多哈希生产接线(A229)**:登记步三列一次写+mirror 独立持久 MT-LSH 实例;建边步 mirror 源(距≤12);**翻转站群端到端**:批2 传批1 图的水平翻转(sha 不同、phash 距 30 盲区)→mirror 距 0 命中→phash_near 边建立;`graph_wire_multihash` 开关默认关。
- **V14 收录+遗忘重开(A230)**:bayes_reliability/bayes_half_life 升格一等字段;record 流 ts 字段(缺省不写键=旧行逐字节兼容);遗忘重开——缺 ts 旧行回退滴答(epoch 量纲下自然衰减,语义自洽:无时间信息视为远古)。
- **batchflow trace 消费+长窗口转存(A231)**:drain_to_sink(锁内原子领取/锁外写 sink)+on_evict 逐出前转存回调;批次收官 trace_ids 清单+逐站 trace.json 落盘+批次末转存审计 JSONL。
- **批预算熔断+批次成本页(A232)**:check_budget 纯函数(unpriced 诚实计数);finishflow 步⑥哨兵式超限告警(**绝不删文件/绝不改队列**,docstring 声明"哨兵不是执行器");webui 第六页签"批次成本"。
- **门户动态发现与登记闭环(A233)**:discover_portals/list_portals/get_portal——动态门户须 YAML 显式 `enabled: true` 才生效(草稿落位恒写 false,confirm_enable_portal 口令确认后原子翻转);内置两门户豁免零改动。
- **事件账本生产接线+交互确认+approvals 对账(A234)**:review_tui --event-ledger/dashboard cfg.event_ledger 注入;--apply-ahead -i 逐条 y/n 交互(非 y 即跳过,失败安全);approvals 投影第三对账(留痕滞后独立清单)。
- **签名清理+时间戳加固(A235)**:私有别名移除(grep 零残留);verify_tsa_token 离线 DER 结构校验器(严格最小子集,诚实边界:结构校验非密码学验证);单调计数器 v2 HMAC 链(篡改不再静默——tamper_detected 入 TimeProof.reason 且被 Ed25519 签名覆盖)。
- **AIMD executor 重建闭环(A236)**:分片提交+窗口减半即排空重建(min(permits,档位) 滞回×2/÷2,先建新池再关旧);进程池时长经返回值回传父进程汇合(外层 summary 逐字节不变);停顿序列/礼貌常量源级断言零触碰。
- 全仓 **6236 用例全绿**(基线 6004,净增 232;exit 0);波前快照 `C:\1\netsentinel-backup-20261003-v10.8.tar.gz`。

## V10.8(2026-10-03)—— 指纹盲区修复·认证防御·成本归集波(A219–A227 并行;详细契约见 CONTRACTS-V14.md)

- **指纹盲区修复·三不变哈希(A219,本波最高优先)**:mirror_hash(翻转群 Klein 四元轨道规范形,flip 召回 **0→1.000 恒等**)/ring_hash(极坐标 32 角×4 带 DCT 能量,±3° 旋转召回 **0.02→1.000**、±8° 0.844)/pyramid_hash(32/16/8 三层,裁剪 **0→部分恢复** 0.25/0.125);PhashRegistry schema v2 多哈希列迁移+find_similar(hash_kind=);kernel_selfcheck 三附加键;红队攻击族修复前后对照表入契约。
- **webui 双增强(A220)**:providers 页第五页签"权重区间"(贝叶斯 CI 表→点值回退→空态三层降级);新 draft_flow_page 三步草稿工作流(扫描→逐字段人工确认→落位)——确认状态机无批量捷径(源码断言锁定)、落位双门槛(口令+load_portal_def 全链校验失败零残留)。
- **V13 配置收录批(A221)**:dynamic_ttl/phash_mt_lsh_db(None=推导)/guard_model_path/guard_family 升格 `# ---- V13:` 一等字段;guard_family 取值域校验(字面量双源靠运行时对账防漂移);契约同步 V13 段;scheduler hasattr 断言预改。
- **四眼事件账本+补齐裁决(A222)**:FourEyesQueue event_log 透传(双人事件+actor 署名,四眼校验零改动);replay --apply-ahead 半自动补齐——先打印全量动作清单、--yes 全量校验通过才逐条幂等重做、执行后复跑对账验证归零(绝不半途 apply)。
- **贝叶斯回流+守卫工厂(A223)**:orchestrator `bayes_reliability` 开关(优先于 Brier,全关=等权现状逐字节;correctness=(p≥0.5)==outcome 映射);classifier_base._LAZY_IMPORT_MODULES 补 "guard"——get_classifier("guard") 工厂全链打通(含子进程零副作用验证)。
- **工程清理批(A224)**:sign_bundle/zip_bundle 升公开口(私有别名一代兼容);kernel_bench 注册 aimd+reliability.bayes 自检(17 内核全绿)+报告标注 HAS_NUMPY;test_aimd 15 处时钟注入防闪烁(3 轮压测绿)。
- **per-run 成本归集(A225)**:cost_meter record 增 run_id/tokens/duration_s 维度(旧行兼容)+aggregate(by=run_id|model|day) 纯函数;finishflow 步⑤收官成本账 cost.jsonl 永写+pyarrow 可选时 cost.parquet(失败降级绝不中断);pyproject 增 analytics extra。
- **漂移 CI+曲线金标(A226)**:PSI/KL 附 percentile bootstrap 95% CI(B=2000 固定种子,退出码仍按点值——检验力不足诚实呈现);感知曲线独立金标(独立指纹/文件/容差,None↔数值互变即违例,与单点金标字节级隔离)。
- **认证平滑评测(A227)**:benchmarks/certify_bench.py——Cohen randomized smoothing 认证半径闭式(Φ⁻¹ Acklam+Halley<1e-12、CP 精确界 lgamma);ABSTAIN 无证书语义;构造场景认证率 83.3%/半径 7.55 实测;数学事实修正:p_lower+p_upper≡1 恒等式,判据采用 Cohen 原始 p_lower>p_upper(三处论证锁定)。
- 全仓 **6004 用例全绿**(基线 5775,净增 229;收口后全量 exit 0;并行期两席位观测到的 test_phash_invariant 失败经终态 3 轮压测确认为 A219 校准中途版本,终版稳定);波前快照 `C:\1\netsentinel-backup-20261003-v10.7.tar.gz`。

## V10.7(2026-10-03)—— 性能·事件溯源·红队评测波(A210–A218 并行;详细契约见 CONTRACTS-V13.md)

- **开关收录+trace 落盘(A210)**:trace_enabled/abstain_enabled 升格 `# ---- V12:` 一等字段(orchestrator getattr 无缝命中);finishflow 收官把 span 树落盘 `data/runs/<站点>/trace.json`(失败只告警绝不中断收官)。
- **调度器动态 TTL 接线(A211)**:scheduler 消费 temporal 爆发检测——每轮一次 graph 只读快照→kleinberg_bursts→suggest_ttl→should_rescan(ttl_hours=);`dynamic_ttl` 开关默认关=零 IO 零差异;源码级断言频控/礼貌间隔零触碰(红线 35)。
- **AIMD 自适应并发调速器(A212)**:ops/aimd.py——利用率+EWMA 时延双信号乘性减半/加性增窗口(cap=档位 workers,floor≥1,礼貌硬地板);pool 背压 opt-in 切换(不注入=现状逐毫秒一致);kernel_selfcheck 四键口径。
- **事件溯源(A213)**:storage/event_log.py append-only 账本(三层防改:API 无更新路径+DB 触发器+重放对账);review_queue 五事件**先账本后状态**双写(崩溃只产生可审计的"事件超前");storage/replay.py 重放投影+对账 CLI(事件超前/账本缺失/状态不一致三岔中文差异清单,退出码 0/1/2)。
- **清理批+合并重签(A214)**:兑现 A204 承诺——kernel_wire 私有写口回退移除(add_edge 唯一写口);大库 fastpath(登记数>5000 跳过 O(N) 重灌只走持久 MT-LSH,双路径召回等价测试);merge_bundles/parallel_pack 产物经 packager._sign_bundle 单一实现补签(默认=现状零签名)。
- **感知预算扰动曲线(A215)**:四类扰动参数化 ε 网格+纯 stdlib SSIM(8x8 窗);run(grid=True) 输出 score-vs-ε/score-vs-SSIM 双曲线、鲁棒 AUC、min_effective_attack(首个显著掉分强度——直接指导 preprocess 反混淆参数);单点路径与金标门禁逐字节不变(四变体重构后与旧实现逐字节一致)。
- **贝叶斯分层可靠性(A216)**:BayesianReliabilityTracker——Beta 后验+指数遗忘(遗忘权重=伪计数保持共轭闭式)+James-Stein 式分层收缩+纯 stdlib Beta CI(连分数+二分);漂移实测:成员前 200 对后 1 日 20 错,后验 0.89→0.41 一日内消退(Brier 反比滞后份额 0.64 对比);fusion opt-in 接入,Brier 路径逐字节不变。
- **守卫模型适配器(A217)**:vision/guard_adapter.py——ShieldGemma-2/Llama Guard 族开放权重模型本地推理入 ensemble(transformers/torch 惰性、local_files_only 禁自动联网、目录不存在先于导入校验);解析器真值表(Yes/No→0.95/0.05、S1..S14 类别码保留);model_catalog 注册三守卫模型(local/open-weights/guard 标记,无回退档语义)。
- **pHash 红队基准(A218)**:9 攻击族×32 档强度网格量化指纹召回——关键发现:**镜像翻转与中心裁剪是零召回盲区**(全档 0.0)、旋转 3° 即破分块 256bit(p256@32=0.02)、水印 α0.35 破 d8;多表 LSH 在真实扰动分布下较单表 +5.5~+10.9pp、d16 距暴力真值仅 0.4pp;金标门禁复用 adversarial 惯例(退出码 0/1/2)。
- 全仓 **5775 用例全绿**(基线 5458,净增 317;收口后全量 exit 0,闪烁嫌疑文件 3 轮压测全绿);波前快照 `C:\1\netsentinel-backup-20261003-v10.6.tar.gz`。
- 追认记录:A210 因字段升格更新 test_orchestrator.py 两行 hasattr 断言语义(行为断言保留,同 A201 先例)。

## V10.6(2026-10-03)—— 接线收口波(A201–A209 并行;详细契约见 CONTRACTS-V12.md)

- **配置正式收录(A201)**:V10.4/V10.5 的 11 个附加属性键(graph_wire/gang_*/ensemble_reliability_weights/cascade_risk_budget/abstain_threshold/bundle_sign_algo/ed25519_seed_hex/tsa_url 等)升格为 contracts.Config `# ---- V11:` 一等字段;默认值=升格前行为,消费方 getattr 无缝切换;契约同步机新增 V11 段双向对账。
- **orchestrator 全链接线(A202)**:trace 贯通 fetch→vlm→fusion→review 四阶段 span(trace_enabled 默认关);abstain 弃权接线——成员分→batch_decide→needs_review+提权(abstain_enabled 默认关,**三档判定输出分毫不动**);复核队列 priority_weight 列+annotate API(状态机零触碰)、triage boost_by_url 加权项。
- **服务层与线程池 trace(A203)**:/scan 返回 X-Trace-Id 响应头+新只读本机端点 GET /trace/{job_id}(span 树导出);线程池边界 propagate/restore 显式贯通;audit_sink 惰性接入;TRACE_ENABLED=False=现状逐字一致。
- **phash 登记闭环+公开边 API(A204)**:graph.py 公开 add_edge(kind 白名单/幂等/自环拒绝);扫描产出 phash 登记 PhashRegistry+持久 MultiTableLSH——**跨批次 phash_near 闭环**(批1 登记→批2 同像素异封装图→建边 1.0 权重端到端验证);私有写口回退保留一代。
- **产包签名策略接线+seal_now(A205)**:packager 经 getattr 防御式读取 bundle_sign_algo/ed25519_seed_hex(env 覆盖优先)/tsa_url——ed25519 显式 opt-in,缺 seed/构造异常回退 HMAC 现状,签名失败回滚不中断打包;JsonlAuditLogger.seal_now() 手动立即封根(高敏操作后锁定尾部盲区),audit_verify 整卷可验。
- **语料漂移哨兵(A206)**:benchmarks/drift.py——三类报告形态自动提取分数分布→10 桶直方图快照 JSONL;PSI(0.1/0.2/0.3 三级)+标签条件 KL 逐窗对照;双参考窗口(first 对账/prev 滚动健康检查,渐变漂移 prev 检出更早);退出码 2 可作 CI 门禁。
- **时序爆发检测联动(A207)**:intel/temporal.py——Kleinberg 两状态(可扩展 k 层)burst 检测,对数域 Viterbi(lgamma/expm1)任意尺度不溢出+操作计数自检;burst_factor→suggest_ttl(爆发期 TTL 收缩至最低 1/5,钳 [6,720]h);site_memory.should_rescan 增可选 ttl_hours(None=现状)。
- **金标回归门禁(A208)**:对抗基准指纹键(分类器+语料 sha256+扰动参数+统计版本)绑定金标文件;容差=max(10%·|基线|, V10.4 CI 半宽)——统计自洽;--update-golden/--gate,违例退出码 2 可拦截模型/prompt/预处理回归;缺基线 warn/fail 可配。
- **a11y 门户理解器(A209)**:submit/form_scanner.py——stdlib HTMLParser 提取表单模型→语义推断(置信度+依据+manual_required 三件套)→门户 YAML v2 候选链草稿;**仅草稿绝不自动生效**、验证码字段宁可错杀整体排除、拒绝写入 portals/ 目录;草稿经 load_portal_def round-trip 全链验证;新门户接入小时级→分钟级。
- 全仓 **5458 用例全绿**(基线 5198,净增 260;收口后全量 exit 0 零失败);波前快照 `C:\1\netsentinel-backup-20261003-v10.5.tar.gz`。
- 追认记录:A201 因字段升格与旧"属性不存在"断言直接矛盾,更新 5 条领地外断言语义(行为断言全保留,终态全绿)。

## V10.5(2026-10-03)—— 世界前沿创新波·三(A192–A200 并行;详细契约见 CONTRACTS-V11.md)

- **Ed25519 非对称签名 + RFC3161-lite 可信时间戳(A192)**:纯 Python RFC 8032 Ed25519(官方向量 1-3 锁定,sign≈3ms);时间证明=UTC+持久化单调计数器(损坏自愈)+可选 TSA 令牌(默认 None 全离线);`bundle_sign` 升级双算法策略——Ed25519 签名块内嵌 public_key 供受理方**独立验签**(司法举证效力跃升),旧 HMAC 包逐字节兼容。
- **Merkle 整卷离线审计命令(A193)**:`ops/audit_verify.py` 整卷复核——重放全部叶+逐 checkpoint 对账,篡改检出精确到行号;10 万行含验签 **1.846s**;legacy 混合卷/降级段/未封卷取证分级;退出码 0/1/2 可作 CI 门禁。
- **图谱通电 + Louvain 加权社区检测(A194)**:`graph_kernel` 新增 louvain_pass/louvain_communities(模块度单调不减,30 seeds 验证,操作计数入自检);kernel_wire/orchestrator 接通"已建成未通电"的 graph 写口与 phash LSH(`graph_wire` 开关默认关,失败安全降级);团伙判定新增 community 模式(shared_template 弱边降权,直击误并风险)。
- **多表 multi-probe LSH(A195)**:`MultiTableLSH`——K 张置换掩码表+桶键汉明球探测+sqlite 持久化;10⁴ 指纹实测 **d=16 召回 0.83%→99.17%**(旧单表对比),汉明比较均值 54.8 次/查询(<全表 1%);召回下界可证(docstring 论证)。
- **分歧选择性弃权(A196)**:`decision/abstain.py`——成员分歧(极差/标准差双口径)超阈→弃权建议+人工复核优先(不是第四判定档,verdict 三档契约零触碰);risk-coverage 曲线扫描(覆盖率单调由构造保证);`to_triage_hint` 集成接口。
- **FrugalGPT 自适应级联带(A197)**:不确定带从历史"选择性风险-成本"在线推导(风险质量约束下最小期望成本网格);冷启动/无预算返回 static_default 同一对象(字节级兼容);±20% 限幅+只收不放;构造场景实测期望成本 **↓57.1%**(↓30% 目标超额)。
- **零依赖 W3C 风格追踪内核(A198)**:`telemetry_trace.py`——contextvars trace/span 传播、嵌套 span 树、@traced、线程边界 propagate/restore;span 双写 telemetry timer+可选 audit_sink;OTel 惰性薄桥接(缺席零影响);27 测试含并发隔离。
- **mathx numpy 双后端 + import-linter(A199)**:热路径(dot/matmul/softmax/standardize)探测式向量化(规模门 >64、子类替身/大整数回落 stdlib,操作计数双路径统一);pyproject 新增 fast extra 与 [tool.importlinter] 声明式分层契约(每条规则先 AST 全量验证后写入);numpy 缺席全绿。
- **自愈选择器候选链(A200)**:portal YAML schema v2——有序候选链(css id 打头+role/label/placeholder/text 兜底);executor 逐候选探测回退+胜者运行时缓存+审计 notes(绝不回写 YAML);captcha 别名拒绝对候选链**逐候选生效**;改版演练(真 chromium):同表单 id 全换场景 **4 步全回退受理**。
- 全仓 **5198 用例全绿**(基线 4881,净增 317;收口后全量回归 exit 0 零失败);九领地互不重叠;波前快照 `C:\1\netsentinel-backup-20261003-v10.4.tar.gz`。

## V10.4(2026-10-03)—— 世界前沿创新波(9 代理并行:A183–A191;详细契约见 CONTRACTS-V10.md)

- **统计担保升级(conformal)**:`fit_threshold` 新增 Clopper-Pearson 精确 (1-δ) 下界与 Learn-then-Test 式二项精确 p 值 + Bonferroni 多重校正认证层(`ltt`/`ltt_advisory`/`empirical_fallback`),对齐 Angelopoulos & Bates Conformal Risk Control;旧字段逐字节不变。
- **e-process 序贯判定(sprt)**:新增 `MixtureSPRT`(Beta-Jeffreys 混合似然比对数闭式,Ville 不等式任意停时担保)与 Howard 线性 stitching `anytime_ci`/`ConfidenceSequence`(全时段联合覆盖 ≥1-δ)——Wald SPRT 原语义零改动。
- **校准驱动动态集成(orchestrator)**:Brier 反比可靠性权重回流 ensemble 主链路(config 开关 `ensemble_reliability_weights`,默认关闭=等权;≥MIN_N 样本自动降权离群成员,缺失成员均值兜底、Σw=1 归一不变量)。
- **决策论复核分诊(triage)**:复核队列新增 `sort="triage"` 排序——p(NSFW)×危害×Beta(1,1) 翻案后验×老化因子四因子可解释加权,同分 FIFO 平局决断;默认 FIFO 与状态机/四眼/人工门零触碰。
- **调度内核 EDF+aging(sched_kernel)**:四因子优先级(+age_h 防饿死,默认权重 0=位级等价)与变成本 0/1 背包(价值密度贪心,等成本退化与旧算法逐项相等;操作计数不变量保持)。
- **离线 Merkle 审计透明日志(merkle.py)**:append-only Merkle 树(O(log n) 增量折叠)+HMAC 签名 checkpoint(默认每 128 条)+独立包含性证明/验证;接入 `JsonlAuditLogger`(行新增 `merkle_leaf` 字段,Merkle 异常安全失效不丢审计本体),对标 sigstore/rekor 离线变体。
- **对抗评测统计推断层(adversarial)**:配对 percentile bootstrap 95% CI(B=10000 固定种子)+符号翻转配对置换检验(≤12 精确枚举/蒙特卡洛 9999)+Wilson 95% CI;报告/CLI 新增 ci95/p_value/wilson_ci 字段,既有指标零变动,对齐 RobustBench 式报告规范。
- **反脆弱事件驱动等待(submit)**:WAIT 步新增可选 `wait_until`(`selector_visible`/`text_present`/`requests_idle` 可组合)→超时退避重试→networkidle 降级 selector 降级盲睡的三级兜底链,每级落 notes 可审计;缺省路径字节级不变,HUMAN_GATE/captcha 红线零触碰。
- **测试科学三件套**:doctest 激活机(5 模块 21 示例 + 全包 `>>>` 分诊守门)、数学内核性质测试(hypothesis 优先/固定种子 stdlib 兜底双引擎,7 条代数律)、契约同步机(AST↔CONTRACTS-V9.md 双向字段对账);dev extra 加 hypothesis(缺失自动跳过)。
- 全仓 **4881 通过 + 5 跳过**(基线 4621,净增 260 用例);九领地互不重叠并行开发,收口后全量回归三次全绿;安全备份快照 `C:\1\netsentinel-backup-20261003.tar.gz`。

## V10.3(2026-10-03)—— 个人信息一次配置永久固化(负责人直研)

- **`python -m netsentinel.profile setup`**:交互式逐字段录入(必填项空值重复询问/格式校验/回车保留旧值),一次写入 `~/.netsentinel/profile.yaml`——**永久模板,跨项目持久,终身有效,零二次配置**。
- **存储层级**:显式传参 > 环境变量 `NETSENTINEL_REPORTER_<FIELD>` > **永久模板(优先)** > 项目 `./profile.yaml` > config 字段;后续所有命令(scan/finishflow/precheck)自动读取。
- **CLI 四命令**:`setup`(首次配置)/ `show`(掩码查看)/ `reset`(清空)/ `path`(显示路径)。
- 16 个新测试(永久路径写入/环境变量覆盖/显式覆盖/掩码红线/必填重复询问/坏格式重试/保留旧值/项目级回退/永久优先);全仓 **4621 通过 + 4 跳过**。


## V10.2(2026-10-03)—— 个人信息完整字段(两门户统一,8 字段全覆盖)

- **背景**:12377 与扫黄打非门户需要的个人信息一致且不止姓名/电话——用户指出后补全。
- **profile.yaml 扩展至 8 字段**:`reporter_name`(必填)、`reporter_phone`(必填)、`reporter_email`、`reporter_id`(身份证号,部分门户必填)、`reporter_address`、`reporter_postcode`、`reporter_type`(个人/企业/组织)、`reporter_org`(类型=企业/组织时必填);逐字段格式校验(电话/邮箱/身份证/邮编正则);掩码视图增强(姓名姓+**、电话前3****后2、身份证前4******后4、邮箱前2***@域名)。
- **SELECTORS 从 8 键扩至 13 键**:新增 `email/id/address/postcode/org`;两个 mock 门户 HTML 同步新增全部输入框;`build_plan` 步骤从 13 步扩至 **18 步**(新增 5 个 skippable FILL);portal_defs `build_plan_from_def` 同步 18 步;两门户 yaml 注释更新。
- **SubmissionPayload 扩展**:新增 `reporter_email/reporter_id/reporter_address/reporter_postcode/reporter_type/reporter_org` 字段;`build_payload` 经 `load_profile()` 自动带入全部字段。
- **环境变量**:每字段独立 `NETSENTINEL_REPORTER_<FIELD>`(如 NETSENTINEL_REPORTER_EMAIL)。
- 全仓 4618 通过 + 4 跳过(V10.2 零破坏:18 步计划向后兼容——空字段 skippable 跳过,不影响旧配置)。


## 最终状态(2026-10-03 综合演练验证)

| 维度 | 数值 |
| --- | --- |
| 测试 | **4618 通过 + 4 跳过** |
| Python 模块 | **157 个** |
| 文档 | **35 篇** |
| 演示脚本 | **7 个**(全离线可跑) |
| 基准框架 | **7 个**(内核/三档并发/分组/对抗/平台/多提供方/分组质量) |
| 安全红线 | **39 条** |
| 演进轮次 | **11 轮**(V1..V10.1) |
| 协作代理 | **182 席位**(A01..A182) |

**四基准全绿**:内核 15/15 · 三档并发 3/3 · 分组 purity 1.00/completeness 1.00 · 对抗鲁棒性(合成语料零退化)。

**五演示全通**:批量流水线 · 多平台视觉模型 · 内核进化 · 三档收官 · 连接向导(全部退出码 0)。

**真实环境**:外网出口/GLM/Yandex/12377(200)/扫黄打非(521)全可达;真实 GLM-5.3-flash 端到端视觉分类成功(nsfw_prob=0.02,5.2s);唯一人工环节 = 浏览器输入验证码 + 回车。


## V10.1(2026-10-03)—— 批量预审 + 半自动焦点(负责人直研)

- **半自动焦点(V10.1)**:步骤计划从 12 步扩至 **13 步**——新增 `FOCUS(#report-captcha)` 在 HUMAN_GATE 之前:光标自动聚焦到验证码输入框,用户**直接在浏览器打字输入验证码 → 回终端按回车 → 系统自动点提交**。每条举报的人工极限 = 浏览器输入验证码 + 回车(约 10 秒/条)。
  - `StepAction.FOCUS` 新增(contracts.py);两个执行器(playwright/session)支持 `page.focus()`;两个门户 planner(portal_12377/portal_shdf)同步 13 步;portal_defs.build_plan_from_def 同步;两门户 `_enforce_safety` 安全检查放行 FOCUS(只聚焦不填写)。
- **批量预审(`submit/batch_precheck.py`)**:一次确认全部就绪条目(预览表含理由前 39 字),按 Y 后系统逐条执行(半自动焦点+浏览器验证码+回车);每条仍停在 HUMAN_GATE(红线不变);`--precheck` CLI 模式(`python -m netsentinel.finishflow --precheck --exec`)。
- **Mock 门户更新**:验证码输入框从 readonly → 可聚焦(安全由 HUMAN_GATE 保障,不是 readonly)。
- 红线不变:验证码由人在浏览器中输入;批量预审只是合并确认,不是移除人工门。7 个新测试。

## V10.0(2026-10-03)—— 举报理由自动生成(≤39字)+ 个人信息模板自动填入(负责人直研)

- **`submit/reason_gen.py`**:确定性模板理由(域名+抽查页数+达标图数+"已人工核实")保底;GLM 可选压缩措辞(输入仅事实清单,提示词禁增新信息);`fit_39` 硬保证 ≤39 字(标点边界收尾/硬裁);任何失败回退模板绝不阻断举报。**真实 GLM 实测**:压缩理由 38 字 ✓,离线模板 34 字 ✓。
- **`submit/profile.py`**:个人信息模板 `profile.yaml`(用户自主配置,勿入库;优先级:传参>env>模板>config);电话校验;`masked()` 掩码视图(红线 39:只进表单不进日志)。
- **接线**:`build_payload(reason=None, auto_profile=True)`(keyword-only,零 API 破坏)——理由自动生成并作为**描述首行**自动填入(正文模板逐字不变),同时存 `payload.reason`;姓名/电话自动带入表单 fill 步骤(空则 skippable)。新 Config 字段 3 项 + `SubmissionPayload.reason`;两个钉死描述测试按 V10 语义更新(正文逐字不变)。
- 新红线 39:个人信息只进表单不进日志(掩码);理由只基于已核实事实、禁止编造;≤39 字硬校验。25 个新测试。

## V9.1(2026-10-02)—— 真实环境接入(golive)· 真实测通验证(负责人)

- **新红线 38**:真实接入≠自主运行;golive 只做只读验证(GET 首页取样 512B,不解析表单不提交);密钥/目标清单/逐组声明/每条验证码与最终提交永远属于运营者。
- **新增 `netsentinel/golive.py`**:`check`(离线体检:配置开关/密钥布尔/活动模型/本地服务/chromium/可选依赖/存储)+ `check --net`(真实外网只读探活:出口/GLM/Yandex/12377/扫黄打非/OpenAI)+ `prepare`(生产配置模板,拒覆盖)+ `human_only`(运营者专属环节清单);13 项离线测试(外呼零容忍用例)。
- **真实实测(本机)**:外网出口/GLM 端点/Yandex 端点/www.12377.cn(200)/www.shdf.gov.cn(521 防护)全部可达;OpenAI 网络不可达(已知);**真实端到端视觉分类成功**——glm-5.3-flash 云端评分测试图(nsfw_prob=0.02、"正常"、中文依据、5.2s)。
- **修复真实环境缺陷**:glm-5.3 思考模型族 + `response_format=json_object` 会截断 JSON 输出(实测定位);glm_adapter 与 vlm_client 两传输层对该族省略 response_format,修复后真实分类链路全通。
- 文档:docs/GO_LIVE.md(体检/实测结果/接入顺序/注意事项/运营者环节)。


## V9.0(2026-10-02)—— CPU 自适应三档并发 · 收官汇总 · 结案代理依次举报(A163–A182,20 席位)

- **CPU 三档并发**:detect(核数/架构,psutil 可选)→ low(max(1,N/4))/ mid(max(1,N/2))/ **high(max(1,N-reserve),最大限度压榨本地计算)**;`concurrency_auto` 首启写建议档(data/concurrency.json,用户配置优先);并行分类(线程 IO 型 + 进程池纯函数三元组)/ 并行打包(同 host 锁串行)/ load_guard(进程 CPU>0.92 让位);tier_bench Barrier 峰值法证明(本机 16 核:low4/mid8/high15 全一致)。
- **压榨边界(红线 35)**:高档只加速本地计算与回环 IO;对外抓取礼貌间隔、引擎限速、举报频控一概不变(48 项红线专项测试含 48 组合行为断言:workers×tier×jitter 下礼貌停顿恒等)。
- **收官汇总 + 结案代理(红线 36)**:`finishflow --input --tier high` 一条命令:档位扫描 → **SummaryAgent 分类汇总**(V6 案件分组/统计/收官报告 md+html/待声明组清单,模块零提交路径)→ 人工逐组声明 → **SequentialReportAgent 依次举报**(编排 run_batch 恒 auto_confirm=False + SessionExecutor 复用,逐条 HUMAN_GATE)→ finish_report.html;断点续报;on_item 进度回调。
- **接线(负责人)**:batchflow/finishflow 入口包 tier pool_runner(pool.py 保持零档位标识,红线隔离);A174 并入 A170、A176 报告被限流截断但 12 用例已落盘全绿、A164-A169 五席报告超时但交付完整、A182 由负责人收口。
- 新红线 35–37(压榨边界/代理无提交权/资源治理 workers≤cores+让位+上限);新 Config 字段 4 项。
- 测试:3968 → **4573 通过 + 4 跳过**(V9 净增约 605 项)。


## V8.0(2026-10-02)—— 视觉模型自动接管 · 连接向导 · 手动切换(A143–A162,20 代理)

- **安装后自动接管**:首次运行任意 CLI 入口触发 `takeover_once`——探测本机 OpenAI 兼容服务(11434/1234/8000/9997,仅回环)→ 命中视觉模型(26 条名称模式识别)即设为活动模型;无本地但有云密钥则接管云平台;全无则自动弹出连接向导(真实 CLI 实测:无模型时自动启动 http://127.0.0.1:8766/ 并提示)。
- **连接向导**(纯标准库自托管单页,零依赖):状态卡/扫描本机服务/本地模型一键启用/云平台 20 家下拉+密钥(password 只进不显,落盘仅密钥环)/测试连接(本地零评分探测,云端 1x1 ping 受预算)/离线桩兜底(红字"离线桩,非模型判定");守护单例(锁文件+探活,端口冲突自动 +1..+5)。
- **手动切换三入口**:向导页 / `python -m netsentinel.modelmgr switch|status|probe|serve|takeover` / REST `/model/switch`(已挂载 service);活动模型持久化 `model_runtime.json`(原子写,来源 takeover|wizard|cli|rest 留痕),下一条扫描生效;stub 为整套替换并明示。
- **并行会话协作**:保留并行工作流预留的 4 个 V8 字段(plugin_supervised_api/onboarding_auto_open/agents_max_workers/agent_task_db),本轮仅消费 onboarding_auto_open;共存在文档中如实说明。
- 新红线 32–34(本地探测仅回环且仅显式触发;密钥只进不显;接管不放宽 vlm_online/预算纪律);入口接线(__main__/batchflow 钩子 + service 路由)由负责人完成。
- 测试:3321 → **3968 通过 + 3 跳过**(V8 净增 647 项,含红线 32-34 专项与端到端);A143 限流重试成功。


## V7.1(2026-10-02)—— 内核全开实战演习 + 搁浅路径缺陷修复(负责人)

- **内核全开演习**(零外呼):本地三站点真实扫描走 `run_scan_v7`——`stub+skin` 双识别内核集成(skin 对真实下载图给出 0.98/skin_ratio 1.00 的真实信号)、SPRT 序贯评估、可靠性融合(reliable-weighted)、LSH 真图索引、**真实 chromium 会话复用连续执行两条举报计划 launch=1**(人工门以注入应答演示)。三种不同肤色图站点全链路判定 NSFW(agg 0.975)。
- **修复演习暴露的 V2 潜在缺陷**:同一物理文件被多条证据引用时(页面同图多链/同内容重复),图片增强的首条重命名使其余证据路径搁浅(分类器收到不存在路径→0 分拖低均分)。修复:批量增强共享"旧路径→新路径"重命名映射,后续证据改指新路径;同内容多证据随后被 ensemble 按路径正确去重为一条(设计语义)。
- 测试:全仓回归全绿(3321+2 skipped 口径)。


## V7.0(2026-10-02)—— 全部内核世界性进化(A123–A142,20 代理)

- **八大内核代际升级**(全部新增文件、开关默认关、零 API 破坏):识别(肤色概率图启发式内核,合成语料均值差 0.97 线性可分)、语言(字符 n-gram TF-IDF 内核,AUC 1.0)、决策(Wald SPRT 序贯早停,20 张序列第 2/4 张停)、融合(Brier 可靠性加权,好坏提供方话语权 21:1)、检索(64bit 分带 LSH,单查询比较次数为全表 0.1%;256bit 指纹 v2,同图/异图区分裕量 86bit)、图谱(增量并查集,分量查询 O(1) 跳转 ≤2)、执行(浏览器会话复用,3 计划 1 次 launch)、调度(优先级×预算轮选)。
- **基础设施内核**:统一存储(版本化迁移幂等)、双层缓存(同图 5 评内层仅 1 调)、张量微库、举报文风内核、安全模糊测试内核(162 变体×5 目标=810 次,**实测抓出 conformal 巨整溢出缺陷并已由负责人修复**)、Prometheus 指标导出、装配线 kernel_wire(开关全关=旧行为逐字节一致)。
- **kernel_bench 总控**:15 内核自检全通过(`python benchmarks/kernel_bench.py`,报告 benchmarks/out/kernel_report.md)。
- 新红线 29–31(零 API 破坏/学习型内核零外呼/基准可复现禁墙钟);新 Config 字段 7 项(use_sprt/sprt_alpha/sprt_beta/use_reliability_fusion/phash_lsh_bands/browser_session_reuse/sched_priority)。
- 测试:2790 → **3321+ 通过**(V7 新增 ~530 用例,其中 test_v7_bench_* 基准 23 个);A126 限流重试成功。


## V6.1(2026-10-02)—— 批量流水线端到端实测 + 集成修复(负责人)

- **端到端验证**:真实 CLI 全链路跑通"大批量筛选→归纳同一名称→依次批量举报(干跑)":bulk_intake 规划(7 URL→3 待扫/4 同站重复)→ batchflow 真扫描归组(**2 案件组**,b/c 共享图片触发团伙归并)→ queue 批准(组条目保留[组:]标记,单站冗余条目驳回)→ batch_tui 逐组声明(交错 Y 确认,sqlite+文本留痕)→ BatchState 建批 → batchflow --resume 干跑逐条执行 → batch_report.html 结案;全程零外呼、零真实提交。
- **集成修复**:①`ReviewQueue.add` 增加 keyword-only `note`(V6 批量流程的 [组:名] 标记此前因签名缺失而丢失——根因修复);②安全闸门放行整个 127.0.0.0/8 回环段(ipaddress.is_loopback,fetcher/browser 两处;外网仍拦截),支持本地多站点演练;③重建 crawler/browser.py(误写事故后按其 41 项测试规格 1:1 重建,并新增 `_screenshot_path(tag=)` 供 capture_v2 避免同秒互撞)。
- 测试:全仓 2790 通过 + 2 跳过(重建与修复后回归全绿)。


## V6.5(2026-10-02)—— 搜索引擎线索发现(优先 Yandex · 自定义关键词;负责人直研)

- 新增 `netsentinel/discovery/`(engines/yandex/searxng/keywords/pipeline + CLI):Yandex 官方 XML API 优先,自托管 SearXNG 备选,mock 离线演练;关键词 txt/yaml/内联三来源 × `{kw}` 模板;结果 TTL 缓存、查询限速(≥1s)、单轮线索预算、canonical 去重、引擎域排除;输出与 bulk_intake 兼容的 leads.txt 直通批量流水线。
- 新红线 27/28:发现即线索(不构成处置依据);关键词零内置 + 默认零外呼。新 Config 字段 10 项(discovery_*/yandex_*/searxng_base_url)。
- 补齐 V6 遗留:A120 演示脚本验证通过、A122/docs/UPGRADE_V6.md、docs/DISCOVERY.md、config.example.yaml V6/V6.5 段。

## V6.0(2026-10-02)—— 大批量筛选/归纳同名/依次批量举报(A103–A122,20 代理)

- **范围**:批量导入(TXT/CSV/YAML)→ 并发扫描 → 三级归纳(可注册域 canonical → 镜像变体 → 团伙证据链/图片指纹重叠 union-find)→ 合并证据包 → 逐组人工声明(留痕)→ 批量队列 → 顺序逐条提交(频控/每日上限/断点续批)→ HTML 结案报告;另含分组统计/CSV、案件命名(GLM 增强)、分组质量基准(purity/completeness)、批量复核 TUI、分组复核页面。
- 新红线 24–26:批量=顺序编排,逐条人工门恒 `auto_confirm=False`(静态扫描锁定);批量须逐组"人工核实"声明双落痕;批量频控不得放宽(config 强制校验)。
- 交付:A103–A119、A121 全部成功;A109 限流重试成功;A120/A122 因账号用量上限由负责人补齐。新增 Config 字段 5 项(group_*/batch_*)。


## V5.0(2026-10-02)—— 全模块世界级工程提升(22 组代理 A81–A102)

- **范围**:全部既有模块(约 85 个源文件)按 22 组全覆盖升级;零新功能、零新依赖、零 API 破坏。
- **四条线**:性能(sqlite 全线 WAL+busy_timeout、批量接口、单遍流式"边复制边哈希"、热点 LRU、断路器、背压);健壮性(指数退避重试、按平台断路器 5 次/60s、截图失败不中断、审计链流式校验、递归深度防护);可观测性(新增零依赖遥测核心 `netsentinel.telemetry`,全仓 100+ 打点:计数器/仪表/计时 p95,两个演示脚本内置遥测尾节);质量(类型注解、`__all__`、docstring 示例、魔法数字常量化、SELECTORS 冻结防篡改)。
- **代表性量化**:VLM 缓存批量写 33.5x;分类器工厂未命中路径 20x;providers 解析缓存;cost_meter 10 万行账本峰值内存 24.6MB→0.2MB;政策规则匹配 1.4-2.0x;图谱批量写 1.7x;语料基准分类调用 5N→1 次批量。
- **兼容性**:公共 API 冻结(仅新增带默认值参数);contracts.py 未动;既有用例仅 3 条因"升级对象本身"获红线 21 豁免更新(rate_limit 落盘格式 2 条 + portal_defs 篡改途径 1 条),其余全部原样通过。
- **测试**:1749 → **2220 通过 + 2 跳过**(新增约 470 个 test_v5_* 用例);红线累计 23 条(新增 21-23:API 冻结、升级可验证、零新依赖)。


本项目以"团队契约 + 并行代理"方式迭代:每轮 20 个编号模块(A01–A80),增量契约(`CONTRACTS.md` → `CONTRACTS-V2.md` → `CONTRACTS-V3.md` → `CONTRACTS-V4.md`)与既有契约共同生效。四轮均在 2026-10-01 内完成(以各轮契约文件落盘时间为准),累计 **80 个模块、20 条安全红线、1740 项测试**(V4 收官实测)。**四轮均无破坏性变更**:旧配置文件、旧命令、旧分类器名称在每一轮都原样有效。

> 行为规范以契约原文为准,本文只做总账;各轮细节见对应升级总览:[UPGRADE_V2.md](docs/UPGRADE_V2.md) / [UPGRADE_V3.md](docs/UPGRADE_V3.md) / [UPGRADE_V4.md](docs/UPGRADE_V4.md)。

---

## V4.0 全平台视觉模型统一接入(2026-10-01)

主题:**任何提供方的视觉模型,`classifier: 提供方:模型` 一套语法接入**——20 家提供方(16 云端 + 4 本地)归一到三种 API 方言,一本预算账,一把诊断螺丝刀。

### 新增模块(A61–A80)

| 编号 | 交付物 |
| --- | --- |
| A61 | `netsentinel/vision/providers.py`(20 家权威目录 + `parse_spec`/`resolve`) |
| A62 | `netsentinel/vision/vlm_client.py`(统一传输层,三方言) |
| A63 | `netsentinel/vision/multi_provider.py`(统一分类器 + `build_classifier`) |
| A64 | `netsentinel/vision/provider_quirks.py`(平台特化层) |
| A65 | `netsentinel/vision/model_catalog.py`(模型目录与推荐) |
| A66 | `netsentinel/vision/local_gateway.py`(本地推理网关探活) |
| A67 | `netsentinel/vision/vlmctl.py`(诊断 CLI:list/models/ping/doctor) |
| A68 | `netsentinel/vision/failover.py`(故障转移路由,注册名 `failover`) |
| A69 | `netsentinel/vision/provider_agreement.py`(跨平台一致性分析) |
| A70 | `netsentinel/security/keys.py`(多平台密钥环) |
| A71 | `netsentinel/vision/model_negotiate.py`(模型名协商回退) |
| A72 | `netsentinel/vision/prompt_dialects.py`(三方言请求/响应纯渲染) |
| A73 | `netsentinel/vision/response_repair.py`(跨家族 JSON 修复语料 ≥85 条) |
| A74 | `netsentinel/vision/provider_throttle.py`(每提供方令牌桶限速) |
| A75 | `netsentinel/vision/cost_meter.py`(成本计量,提示价口径) |
| A76 | `webui/providers_page.py`(复核台提供方面板,四页签) |
| A77 | `benchmarks/providers.py`(跨平台响应解析基准,模拟语料) |
| A78 | `tests/test_multi_provider_e2e.py` + `scripts/demo_multi_provider.py` |
| A79 | `docs/PROVIDERS.md` + `docs/MULTI_PROVIDER.md` |
| A80 | `docs/UPGRADE_V4.md` + `CHANGELOG.md`(本文)+ `docs/PROVIDER_SETUP.md` |

配套测试:`tests/test_providers.py`、`test_vlm_client.py`、`test_multi_provider.py`、`test_provider_quirks.py`、`test_model_catalog.py`、`test_local_gateway.py`、`test_vlmctl.py`、`test_failover.py`、`test_provider_agreement.py`、`test_keys.py`、`test_model_negotiate.py`、`test_prompt_dialects.py`、`test_response_repair.py`、`test_provider_throttle.py`、`test_cost_meter.py`、`test_providers_page.py`、`test_provider_bench.py`、`test_multi_provider_e2e.py`。

### 新增 Config 字段(7 个)

`vlm_provider`(默认 `glm`)/ `vlm_api_keys` / `vlm_provider_base_urls` / `vlm_provider_models` / `vlm_fallback_chain`(默认空)/ `vlm_request_timeout_s`(90)/ `vlm_max_image_mb`(8);分类器语法扩展为 `classifier: <注册名>` / `<提供方>` / `<提供方>:<模型>` 三形态(v1–v3 注册名全部继续有效)。

### 新增红线(第 16–20 条)

16. 默认零外呼不变;本地提供方(ollama/vllm/lmstudio/xinference)数据不出本机,免 `vlm_online` 闸门但仍须运营者显式选择;云端沿用 `vlm_online=True + 密钥` 双条件;
17. 密钥绝不入日志 / manifest / 异常消息(统一经 `security.redact` 口径;请求头只在 debug 级打印打码后形式);
18. 目录里的模型名 / 端点是提示信息,以各平台官方文档为准,全部可覆盖;文档必须写明"上线前核验一次";
19. 跨平台共用一本预算账:任何真实 VLM 外呼(含 failover 第二跳、多平台 ensemble 成员)都走 `vlm_cache.spend_one`;
20. ping / doctor 是唯一允许外呼的诊断动作,且只能由人在 `vlmctl` 里手动触发;常规扫描 / 测试零外呼。

### 测试数量变化

1255 → **1740**(+485;2026-10-01 实测:1737 通过 / 1 失败 / 2 跳过,失败项为 A68"未就位场景"模拟测试的收尾隔离问题,见 [UPGRADE_V4.md](docs/UPGRADE_V4.md) §7)。

### 破坏性变更

**无。**v1–v3 分类器名称、配置键、CLI 命令、数据库与审计格式全部原样有效;V4 七个新字段全部可选且默认不激活任何新外呼(`classifier` 默认仍为 `stub`)。

---

## V3.0 智能体平台(2026-10-01)

主题:**从"感知"到"智能体平台"**——案件智能体编排、级联路由、统计保证、证据网络、平台治理。

### 新增模块(A41–A60)

| 编号 | 交付物 |
| --- | --- |
| A41 | `netsentinel/agent/case_agent.py` + `case_flow.py`(案件智能体) |
| A42 | `netsentinel/vision/cascade.py`(级联路由分类器,注册名 `cascade`) |
| A43 | `netsentinel/intel/phash.py`(感知哈希库) |
| A44 | `netsentinel/intel/active_learn.py`(主动学习) |
| A45 | `netsentinel/decision/conformal.py`(共形预测) |
| A46 | `netsentinel/intel/graph.py`(站点关联图谱) |
| A47 | `netsentinel/crawler/redirect.py`(重定向追踪) |
| A48 | `netsentinel/vision/video_frames.py`(视频/GIF 帧采样) |
| A49 | `netsentinel/policy/engine.py` + `policy.example.yaml`(声明式政策引擎) |
| A50 | `netsentinel/decision/four_eyes.py`(四眼复核) |
| A51 | `netsentinel/intel/regulation.py` + `docs/regulations/*.md`(法规检索 RAG) |
| A52 | `netsentinel/submit/describer_critic.py`(举报描述自检) |
| A53 | `netsentinel/submit/portal_defs.py` + `portals/12377.yaml` + `portals/shdf.yaml`(门户适配器) |
| A54 | `netsentinel/security/bundle_sign.py`(证据签名) |
| A55 | `benchmarks/adversarial.py`(对抗鲁棒性基准) |
| A56 | `webui/dashboard.py`(复核台 v2) |
| A57 | `netsentinel/cli/review_tui.py`(复核 TUI) |
| A58 | `netsentinel/ops/pool.py`(并发扫描池) |
| A59 | `netsentinel/ops/adaptive.py`(自适应重扫) |
| A60 | `docs/AGENT_GUIDE.md` + `docs/PLATFORM.md` + `docs/UPGRADE_V3.md` + `docs/README_V3.md` |

### 新增 Config 字段(12 个)

`case_agent_model` / `vlm_cascade` / `vlm_escalate_above`(0.85)/ `vlm_escalate_below`(0.15)/ `phash_db` / `graph_db` / `four_eyes_required` / `policy_path` / `redirect_max_hops`(5)/ `video_max_frames`(6)/ `conformal_target_precision`(0.95)/ `adaptive_base_interval_h`(72)。

### 新增红线(第 11–15 条)

11. 政策与流程不得削弱人工门(政策引擎 / 四眼复核只能增加审批环节);12. 图谱与哈希库仅存本地;13. 级联与智能体共享同一预算(所有 VLM 调用走 `vlm_cache`);14. 统计担保要诚实(共形输出必须附前提);15. 测试零外呼、零真实门户、零真实 VLM 调用。

### 测试数量变化

740 → **1255**(A41–A50 就位时点实测 1024 通过 / 1 跳过,全量收官 1255)。

### 破坏性变更

**无。**A01–A40 模块一行未动;V3 能力全部默认关闭或零成本可选(默认值即 V2 行为);`scan / queue / submit` 三命令用法不变;复核队列 `entries` 表不动(四眼 `approvals` 为同库新表)。

---

## V2.0 GLM 视觉接入(2026-10-01)

主题:**从"规则"到"感知"**——GLM 视觉大模型作为特征分量进入体系,多源可解释证据 → 融合 → 人工拍板。

### 新增模块(A21–A40)

| 编号 | 交付物 |
| --- | --- |
| A21 | `netsentinel/vision/glm_adapter.py`(GLM 客户端,注册名 `glm`) |
| A22 | `netsentinel/vision/vlm_prompts.py`(提示词/解析/校准) |
| A23 | `netsentinel/vision/vlm_cache.py`(缓存 + 每日预算) |
| A24 | `netsentinel/vision/page_vlm.py`(页面级截图理解) |
| A25 | `netsentinel/vision/arbiter.py`(分歧仲裁) |
| A26 | `netsentinel/vision/preprocess.py`(图像预处理变体) |
| A27 | `netsentinel/intel/url_intel.py`(URL 静态情报) |
| A28 | `netsentinel/intel/text_intel.py`(页面文本情报) |
| A29 | `netsentinel/decision/fusion.py`(logit 融合,只升不降) |
| A30 | `webui/app.py` + `webui/README.md`(Streamlit 复核台) |
| A31 | `service/app.py`(FastAPI REST) |
| A32 | `netsentinel/notify/hub.py`(webhook 通知) |
| A33 | `netsentinel/intel/site_memory.py`(站点记忆) |
| A34 | `netsentinel/report/html_report.py`(HTML 举报材料) |
| A35 | `netsentinel/crawler/capture_v2.py`(懒加载滚动采样) |
| A36 | `netsentinel/submit/llm_describer.py`(AI 描述草拟) |
| A37 | `benchmarks/run_benchmark.py` + `benchmarks/corpus/*`(离线基准) |
| A38 | `netsentinel/security/vault.py`(密钥与审计哈希链) |
| A39 | `netsentinel/ops/scheduler.py` + `watchlist.example.yaml`(巡查调度) |
| A40 | `docs/VLM_GUIDE.md` + `docs/API.md` + `docs/DEPLOY.md` + `docs/UPGRADE_V2.md` |

### 新增 Config 字段(14 个)

`glm_api_key` / `glm_base_url` / `glm_model` / `glm_models_fallback` / `vlm_online`(默认 False)/ `vlm_max_images_per_site`(8)/ `vlm_cache_db` / `vlm_daily_budget`(200)/ `capture_engine` / `use_fusion` / `notify_webhook` / `watchlist_path` / `service_host` / `service_port`;`SiteReport` 新增 `intel` 字段。

### 新增红线(第 6–10 条)

6. VLM 数据出境须显式同意(`vlm_online` 默认 False);7. VLM 结果只是特征,不是判官;8. 提示注入防御(只提取 JSON 数值字段);9. 费用与频次保护(缓存 + 每日预算);10. 测试零外呼。

### 测试数量变化

291 → **740**(+449)。

### 破坏性变更

**无。**v1 判定公式、`SELECTORS` 与步骤序列、队列状态机原样生效;`vlm_online` 默认关,V2 能力全部按需开启。

---

## V1.0 初筛与举报辅助(2026-10-01)

主题:**规则证据链**——对运营者提供的 URL 做网页抽样 + 图像识别,生成证据包,经人工确认后辅助填写 12377 与扫黄打非举报表单。

### 新增模块(A01–A20,首轮全量)

| 编号 | 交付物 |
| --- | --- |
| A01 | `netsentinel/config.py` + `logging_util.py`(配置与结构化日志/审计) |
| A02 | `netsentinel/crawler/fetcher.py`(网络闸门抓取) |
| A03 | `netsentinel/crawler/browser.py`(playwright 页面采样) |
| A04 | `netsentinel/crawler/site_map.py`(同站 BFS 发现) |
| A05 | `netsentinel/vision/classifier_base.py` + `stub_classifier.py`(分类器基座与离线桩) |
| A06 | `netsentinel/vision/nudenet_adapter.py`(NudeNet 适配) |
| A07 | `netsentinel/vision/hf_clip_adapter.py`(CLIP 适配) |
| A08 | `netsentinel/vision/ensemble.py`(加权集成) |
| A09 | `netsentinel/decision/verdict.py`(判定公式) |
| A10 | `netsentinel/decision/review_queue.py`(人工复核队列) |
| A11 | `netsentinel/evidence/packager.py`(证据包) |
| A12 | `netsentinel/submit/form_models.py`(表单模型与 SELECTORS) |
| A13 | `netsentinel/submit/portal_12377.py` + `docs/portal_12377_notes.md` |
| A14 | `netsentinel/submit/portal_shdf.py` + `docs/portal_shdf_notes.md` |
| A15 | `netsentinel/submit/executor_playwright.py` + `tests/fixtures/mini_form.html` |
| A16 | `tests/mock_portals/`(本地 mock 门户 + 全流程测试) |
| A17 | `netsentinel/submit/playbook_gen.py` + `drivers/driver.mjs` + `docs/computer-use-integration.md` |
| A18 | `netsentinel/pipeline/orchestrator.py` + `__main__.py` + `submit/rate_limit.py` |
| A19 | `tests/test_e2e_stub.py` + `tests/fixtures/demo_site/*` + `scripts/make_png.py` + `scripts/demo_stub_scan.py` |
| A20 | `README.md` + `docs/USAGE.md` + `docs/ETHICS.md` + `docs/ARCHITECTURE.md` |

另:`netsentinel/contracts.py` 数据契约与 `tests/conftest.py` 由项目负责人随契约交付(禁改基座)。

### Config 字段(首轮基线,26 个)

判定阈值(`nsfw_threshold` / `review_threshold` / `prob_count_line` / `min_nsw_images` / `min_image_px`)、抓取预算(`max_pages` / `max_images_per_page` / `max_image_mb` / `fetch_timeout_s` / `fetch_delay_s` / `respect_robots` / `allow_network`)、分类器(`classifier` / `ensemble_members`)、提交安全(`human_gate_required` / `dry_run_default` / `submit_min_interval_s` / `submit_max_per_day`)、门户入口(`portal_12377_base` / `portal_shdf_base`)、路径(`data_dir` / `evidence_dir` / `db_path` / `audit_path` / `log_path`)。

### 安全红线(第 1–5 条,项目立身之本)

1. 绝不自动识别 / 绕过验证码(验证码环节只能是 `HUMAN_GATE`);2. 真实提交前必须有人工确认,不可配置关闭;3. 开发与测试期间绝不访问 12377 / 扫黄打非真实站点;4. `allow_network=False` 为默认(除本机外不得真实联网);5. 提交频控(最小间隔 60s / 每日 5 次)强制生效。

### 测试数量变化

0 → **291**(首轮全绿)。

### 破坏性变更

**无**(首轮基线)。
