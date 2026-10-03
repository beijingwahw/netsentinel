# NetSentinel V15 团队契约(A228–A236 并行开发)—— 遗留清偿波·一

> 九领地互不重叠实施波,系统性清偿六波积累的风险与后续建议(第一批)。收口后全仓 6236 用例全绿(基线 6004,净增 232;exit 0)。
> 铁律全程有效:人工门不可关闭、绝不自动识别/绕过验证码、默认禁网+干跑、强制频控、核心零第三方运行时依赖、纯函数内核零 IO+操作计数不变量。

## 0. V15 新增保证(50–51,累计 51 条)

50. **动态门户显式启用**:portals/ 目录动态发现的门户必须 YAML 内显式 `enabled: true` 且 entry_url_key 存在于 Config 才可被 list_portals/get_portal 加载;草稿落位工具恒写 `enabled: false` 且拒绝草稿自带启用标记;启用=口令确认+全链校验+原子替换的单一人工路径,无批量接口;内置门户豁免(不写 enabled 也加载)。
51. **预算哨兵不执行**:批预算超限只产生告警+遥测+结果标记——绝不删除已产出文件、绝不修改复核队列状态、绝不阻断收官;unpriced 调用单独诚实计数(不猜价)。

## 1. 九领地归属(A228–A236)

| 组 | 文件 | 要点 | 关键实测 | 测试 |
| --- | --- | --- | --- | --- |
| A228 | vision/phash2.py | dft_ring_hash(DFT 模严格旋转不变)+tile_hash(重叠滑窗) | ±15° 0.125→**1.000**;crop0.2 0.125→**0.750** | test_phash_invariant 扩展 |
| A229 | pipeline/kernel_wire.py + intel/phash_lsh.py | 三哈希登记+mirror MT-LSH 实例+建边 | 翻转站群跨批端到端建边(phash 盲区距 30) | kernel_wire(+5)/lsh(+2) |
| A230 | contracts/config/reliability/orchestrator | V14 字段+record ts+遗忘重开 | ts 遗忘反时序翻转手算;半衰期极端边界 | 四文件(+36) |
| A231 | telemetry_trace.py + batchflow.py | drain_to_sink+on_evict+batchflow 消费 | 并发 drain 安全;开关关零行为 | batchflow(+7)/trace(+12) |
| A232 | cost_meter.py + finishflow.py + webui/providers_page.py | check_budget 哨兵+批次成本页 | 超限不删文件/不改队列断言 | 三文件(+21) |
| A233 | submit/portal_defs.py + webui/draft_flow_page.py | 动态发现+enabled 门槛+启用闭环 | 状态机 placed→enabled 唯一转移 | portal_defs(+24)/draft(+16) |
| A234 | storage/replay.py + cli/review_tui.py + webui/dashboard.py | 生产接账本+交互确认+approvals 对账 | 非 y 即跳过失败安全;留痕滞后独立清单 | replay(+18)/tui(+6)/dash(+4) |
| A235 | packager/merge/parallel + security/timestamp.py | 别名移除+CMS 结构校验+计数器 HMAC 链 | 14 类 DER 畸形拒绝;三类篡改全检出 | 五文件(+~30) |
| A236 | ops/pool.py | executor 重建闭环+进程时长回传 | min(permits,档位) 滞回;summary 逐字节不变 | test_pool(+11) |

## 2. 剩余待清偿项(最终批 A237–A245 候选)

- 安全残留:policy_sha256 入审计决策事件;OS 密钥库(keyring/DPAPI)优先回退。
- 评测残留:曲线金标 score-vs-SSIM 形状指纹;KL 参考窗重采样;--curve-missing-baseline 拆分;certify 半径金标+噪声 LUT 加速;守卫模型标定工具(Yes/No 阈值)。
- 建模残留:burst 半衰期标定工具+层级嵌套输出;贝叶斯事件流 O(n) 重放检查点化/窗口截断。
- 接线残留:vision 调用链 run_id 落账(vlmctl/vlm_client);mirror_near 独立边种类(A229 建议,现挂 phash_near 无法区分来源)+resolve_gangs mirror 降权;--apply-ahead approvals 补齐分支;uvicorn 多进程 trace 注记;webui bayes 重建消费行内 ts;config.example.yaml 补 V14 键样例。
- **有意识不做**(有据豁免,非遗漏):anyio 结构化 IO——零第三方依赖核心红线+Playwright sync API 依赖+3.10 支持(无 asyncio.TaskGroup),性能目标已由 AIMD+executor 重建闭环达成;真实模型上线/真实语料标定/streamlit 人工过验——需真实环境,已尽离线可做部分(工具/框架/文档),余项在文档中列明操作指引。

## 3. 验收记录(2026-10-03)

- 收口后总控全量回归 **6236 用例、exit 0、零失败字符**;并行期各席位瞬态失败均隔离复现排除因果。
- 波前快照:`C:\1\netsentinel-backup-20261003-v10.8.tar.gz`;备份序列:backup-20261003 → v10.4 … → v10.8。
- 全部新能力默认关闭或 opt-in,51 条红线回归测试全绿;本波无领地外偏例。
