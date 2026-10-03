# NetSentinel V13 团队契约(A210–A218 并行开发)—— 性能·事件溯源·红队评测波

> 九领地互不重叠实施波(两接线收口 + 性能/架构 + 评测/建模 + 清理)。收口后全仓 5775 用例全绿(基线 5458,净增 317;exit 0,闪烁嫌疑 3 轮压测全绿)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V13 新增保证(46–47,累计 47 条)

46. **事件账本不可变**:event_log 全库 append-only 三层保证——API 无 UPDATE/DELETE 路径、DB 触发器拦截裸 SQL 改删、replay 对账暴露任何改动手迹;复核状态变更必须**先账本后状态**(崩溃只产生可审计的"事件超前",绝不出现"状态已变而账本无据");重放器是宽容投影不是第二套状态机校验器。
47. **AIMD 只收紧不放宽**:并发窗口 cap 恒等于注入的档位 workers(≤cores,红线 37)、floor≥1 永不饿死;减窗只增加提交侧停顿、加性增最多回到礼貌基线——对外礼貌间隔/频控在数学上不可能被 AIMD 放宽(与红线 35 叠加);不注入控制器=现状逐字节。

## 1. 九领地归属(A210–A218;兄弟模块只读,行为向后兼容)

| 组 | 文件 | 要点 | 关键实测 | 测试 |
| --- | --- | --- | --- | --- |
| A210 | contracts.py + config.py + finishflow.py | trace/abstain 开关升格 V12 段 + trace 树落盘 | runs/<站点>/trace.json;失败只告警;契约同步 V12 段 | 三文件(+20) |
| A211 | ops/scheduler.py | temporal 动态 TTL 接线(dynamic_ttl 默认关) | 爆发站点 TTL≈17.09h(库级 72h);每轮 1 次图快照;红线 35 源码级断言 | test_scheduler(33→41) |
| A212 | ops/aimd.py(新) + ops/pool.py + ops/load_guard.py | AIMD 自适应并发(双信号窗口) | 减半 [4,2]/回升恰饱和 cap;不注入=V5 基准逐毫秒一致;trace 贯通回归 | test_aimd(54)+pool(+5) |
| A213 | storage/event_log.py(新) + storage/replay.py(新) + decision/review_queue.py | 事件溯源双写+重放对账 | 8 线程×25 并发 seq 无洞;崩溃三场景"事件超前+准确 seq";CLI 0/1/2 | 三文件(72 新) |
| A214 | pipeline/kernel_wire.py + evidence/merge_bundles.py + evidence/parallel_pack.py | 私有口清理+大库 fastpath+合并产物补签 | 双路径召回逐项一致;补签复用 _sign_bundle 单一实现(零复制) | 三文件(+10) |
| A215 | benchmarks/adversarial.py | ε 网格+纯 stdlib SSIM+鲁棒曲线 | 单点四变体与旧实现逐字节一致;SSIM 手算 diff=0;min_effective 手算对照 | test_adversarial(+17) |
| A216 | decision/reliability.py + decision/fusion_reliable.py | 贝叶斯分层可靠性(opt-in) | 漂移 1 日消退 0.89→0.41(Brier 滞后 0.64);CI 覆盖 19/20;Brier 路径逐字节不变 | 两文件(+22) |
| A217 | vision/guard_adapter.py(新) + vision/model_catalog.py | 开放权重守卫模型适配器(本地推理禁网) | local_files_only 参数捕获;解析真值表;目录校验先于 transformers 导入 | 两文件(+79) |
| A218 | benchmarks/phash_redteam.py(新) | 9 攻击族红队基准 | **镜像/裁剪=零召回盲区、旋转 3° 破 p256**;多表 +5.5~10.9pp;金标门禁 0/1/2 | test_phash_redteam(28) |

**追认记录**:A210 因字段升格更新 test_orchestrator.py 两行 hasattr 断言语义(同 A201 先例,行为断言保留,终态全绿,总控追认)。

## 2. 已识别的后续机会(各席位交付报告结论,未实施——第六波候选)

- **指纹盲区修复(A218 点名,高优先)**:镜像翻转与缩放式裁剪对 DCT 指纹零召回——入库补记镜像指纹 + 旋转不变极坐标 DCT/关键点局部指纹(金字塔哈希方向);A218 的召回矩阵即为验收基准。
- **接线残余批**:batchflow 侧 trace_id 消费;classifier_base._LAZY_IMPORT_MODULES 补 "guard"(打通工厂路径);guard_model_path/guard_family、dynamic_ttl、phash_mt_lsh_db 收录 Config(V13 收录批,注意 scheduler 测试仍有 not hasattr 断言需预改);FourEyesQueue 增 event_log 透传+actor 署名;replay --apply-ahead 半自动裁决(须过人工门);orchestrator ensemble 回流加 bayes_tracker 开关;webui 审计页展示 weights_with_ci 区间。
- **工程清理批**:test_aimd 部分用例注入脚本时钟防负载闪烁(A212 自查建议);ops.aimd/bayesian kernel_selfcheck 注册入 kernel_bench KERNEL_MODULES;packager._sign_bundle/_zip_bundle 升公开口(消除跨模块私有名依赖);kernel_bench 报告标注 HAS_NUMPY。
- **评测增强批**:守卫模型真实模型上线核验(Yes/No→0.95/0.05 阈值标定);感知曲线 min_effective_attack 独立曲线金标;漂移哨兵 bootstrap CI;certified 预处理平滑评测(Cohen);红队接入真实脱敏语料。
- **性能批(收尾)**:AIMD 窗口→executor 重建闭环;worker 回传时长计数汇合(进程池形态);anyio 结构化 IO;per-run 成本归集 Parquet。

## 3. 验收记录(2026-10-03)

- 并行期各席位观测到的瞬态失败(test_aimd/test_phash_redteam/test_reliability/test_scheduler)均在本席位外文件、隔离复现排除因果;收口后总控全量回归 **5775 用例、exit 0、零失败字符**,闪烁嫌疑四文件 3 轮隔离压测全绿。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.6.tar.gz`;备份序列:backup-20261003 → v10.4 → v10.5 → v10.6。
- 全部新能力默认关闭或 opt-in,47 条红线回归测试全绿;A210 领地外 2 行断言偏例已追认(见 §1)。
