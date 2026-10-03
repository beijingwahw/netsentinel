# NetSentinel V16 团队契约(A237–A245 并行开发)—— 遗留清偿波·二 · 收官总账

> 九领地互不重叠实施波,系统性清偿八波积累的全部风险与后续建议(最终批)。收口后全仓 6442 用例全绿(基线 6236,净增 206;**连跑两轮 exit 0 零失败**——并行期各席位观测的闪烁确认为在途文件瞬态)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V16 新增保证(52–53,累计 53 条)

52. **留痕补齐独立确认链**:approvals 愈合(--heal-approvals)是独立于 entries 状态补齐的第二条确认链——两清单两确认、绝不捆绑;每行补写先在账本落 approvals_healed 事件,actor 恒为 "replay-heal" 与真人署名可区分(防伪造留痕);补写后复跑对账归零。
53. **镜像候选不直接并团**:mirror_near 边独立于 phash_near(第 5 种边,旧库迁移零数据丢失);connectivity 团伙判定默认**不**含镜像边(须显式 gang_mirror_in_connectivity=True),community 模式乘法降权(gang_mirror_weight_factor 缺省 0.5)——镜像命中因翻转规范形丢弃方向信息,只能作候选关联。

## 1. 九领地归属(A237–A245)

| 组 | 文件 | 要点 | 关键实测 | 测试 |
| --- | --- | --- | --- | --- |
| A237 | policy/engine.py + security/keys.py | policy_sha256 入审计+keyring 四源链 | 端到端审计事件哈希一致;三态降级链 | test_policy(+9)/test_keys(+12) |
| A238 | adversarial.py + drift.py | 形状指纹+KL 双侧+策略拆分 | 平移构造场景唯一违例 shape_changed | 两文件(+9) |
| A239 | certify_bench.py | LUT 噪声+半径金标 | **42.9× 加速**;金标 0/1/2 三态 | test_certify(+18) |
| A240 | vision/guard_calib.py(新) | 守卫标定协议 | MLE 手算;n<30 降级;建议≠采纳 | test_guard_calib(49) |
| A241 | intel/temporal.py | 嵌套输出+参数标定 | t=1 与扁平逐位一致;帕累托暴力复核 | test_temporal(41→83) |
| A242 | decision/reliability.py | 检查点化(指数核等价) | **612× 加速**;乱序三路径一致 | test_reliability(+15) |
| A243 | vlm_client/vlmctl + config.example.yaml | 成本落账+17 键样例 | 缺省关零落账;防双记标记 | 两文件(+22) |
| A244 | intel/graph.py + kernel_wire.py | mirror_near 边+降权双管 | 翻转站群默认不并团断言 | graph(+4)/kernel_wire(扩) |
| A245 | replay.py + telemetry_trace.py 注记 + providers_page.py | 留痕补齐+部署注记+口径升级 | CLI 子进程端到端;两轮确定性 | replay(+16) 等 |

## 2. 清偿台账(八波全部"风险与后续建议"终态)

**已清偿(本轮 A237–A245)**:policy_sha256 入审计(第一波 P4)、keyring 托管(P4)、曲线形状指纹/KL 双侧/缺基线拆分(A226)、certify 半径金标+LUT 加速(A227)、守卫标定工具(A217)、爆发嵌套+标定(A207)、贝叶斯检查点化(A216)、vision 落账+配置样例(A232/A230)、mirror_near 边+降权(A229)、approvals 补齐(A234)、uvicorn 注记(A203)、webui 贝叶斯口径(A230)。
**前波已清偿(复核确认)**:Ed25519/RFC3161/TSA(A192/A235)、Merkle+audit_verify+seal_now(A193/A205)、图谱通电/louvain/多哈希/mirror(A194/A229/A244)、LSH 多表(A195)、ABSTAIN(A196/A202)、级联自适应(A197)、trace 全链+转存(A198/A203/A210/A231)、AIMD+executor 闭环(A212/A236)、事件溯源+交互+补齐(A213/A222/A234/A245)、金标门禁×3(A208/A226/A239)、漂移哨兵+CI(A206/A226)、守卫适配器(A217/A223)、红队基准+不变哈希五族(A218/A219/A228)、可信时间戳 CMS+计数器链(A235)、动态门户+草稿闭环(A233)、批预算哨兵+批次成本页(A232)、配置收录 V11–V14(A201/A210/A221/A230)、成本归集(A225)、标定工具(A240/A241)、性能(A236/A239/A242)。

## 3. 有据豁免(非遗漏,记录在案)

- **anyio 结构化 IO**:零第三方依赖核心红线+Playwright sync API 依赖+Python 3.10 支持(asyncio.TaskGroup 需 3.11+)——性能目标已由 AIMD 窗口+executor 重建闭环达成。
- **真实模型上线标定/真实脱敏语料重跑/streamlit 人工过验**:需真实环境;离线部分已尽(标定工具/导入接口/操作指引),操作指引见 guard_calib.py docstring 与各基准 CLI。
- **CI 门禁接入**(kernel_bench/golden gates 挂 CI):项目无 CI 基础设施;退出码语义已全部就绪(0/1/2),接入即用。
- **残余微项**:glm_adapter/page_vlm 直连路径落账、EDGE_KIND_CN 补 mirror_near 中文映射、DYN_TTL_EDGE_KINDS 是否含 mirror_near、approvals_healed 入四眼事件白名单、批次专属模型数——均为单行级增强,随下届开发波顺手清偿。

## 4. 验收记录(2026-10-03)

- 收口后总控**连跑两轮全量:6442 用例、exit 0、零失败字符**;doc 敏感测试(contracts_sync/doctests/redline)全绿。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.9.tar.gz`;备份序列:backup-20261003 → v10.4 … → v10.9(共 7 份)。
- 八波累计:**72 个代理席位,4621 → 6442 用例(+1821),红线 37 → 53 条,全程零失败收口**;仓库仍无 git 提交,建议 `git add -A && git commit` 固化。
