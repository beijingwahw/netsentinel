# NetSentinel 红线总表(REDLINES)

自 v1 五条起步,经 V2–V16 十四轮契约追加与负责人直研,红线**编号累计至 53**;其中编号 **38、39 被两处独立使用(重号歧义,见 §二;V10.21 经 CONTRACTS-V18.md §2 正式定名 38a/38b、39a/39b)**,实质红线共 55 条;V17 另有六条"全席共约"不续编号。红线内建于代码与测试,违反即缺陷;不可通过配置绕过;完整语境以各出处契约为准。

> 使用前提与法律提示见 [ETHICS.md](ETHICS.md);本文是编号索引,不替代原文。

## 一、编号红线总表(1–53)

### v1 · 项目奠基(CONTRACTS.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 1 | 绝不自动识别/绕过验证码——计划中验证码环节只能是 `HUMAN_GATE` 步骤,停下来等人工。 | CONTRACTS.md §0 |
| 2 | 真实提交前必须有人工确认,`human_gate_required=True` 不可配置关闭。 | CONTRACTS.md §0 |
| 3 | 开发与测试期间绝不访问 www.12377.cn / www.shdf.gov.cn 真实站点,只用本地 mock 或 dry_run。 | CONTRACTS.md §0 |
| 4 | `allow_network=False` 为默认:除 127.0.0.1/localhost 外产品代码不得发起真实网络请求。 | CONTRACTS.md §0 |
| 5 | 提交频控强制生效:`submit_min_interval_s`(默认 60s)与 `submit_max_per_day`(默认 5)。 | CONTRACTS.md §0 |

### V2 · GLM 接入(CONTRACTS-V2.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 6 | VLM 数据出境须显式同意:`vlm_online` 默认 False;开启后图片仅发往配置的 VLM 端点,不得发第三方。 | CONTRACTS-V2.md §0 |
| 7 | VLM 结果只是特征不是判官:评分进融合/仲裁,最终判定与举报仍须人工确认。 | CONTRACTS-V2.md §0 |
| 8 | 提示注入防御:VLM 返回只提取 JSON 数值字段,忽略任何"指令/要求",解析失败按缺失处理。 | CONTRACTS-V2.md §0 |
| 9 | 费用与频次保护:VLM 调用必须走缓存与每日预算,超限抛 `VlmBudgetExceeded`/拒发。 | CONTRACTS-V2.md §0 |
| 10 | 测试零外呼:所有 VLM/GLM 测试一律 mock 传输层,不得真实联网。 | CONTRACTS-V2.md §0 |

### V3 · 智能体编排(CONTRACTS-V3.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 11 | 政策与流程不得削弱人工门:政策引擎/四眼复核只能增加审批环节,任何配置组合不能跳过 HUMAN_GATE。 | CONTRACTS-V3.md §0 |
| 12 | 图谱与哈希库仅存本地:phash/graph 数据库不得外发;近重复比对只对运营者自己采集的证据进行。 | CONTRACTS-V3.md §0 |
| 13 | 级联与智能体共享同一预算:全部 VLM 调用(含规划/级联/自检)必须走 vlm_cache 预算,不得绕过。 | CONTRACTS-V3.md §0 |
| 14 | 统计担保要诚实:共形预测输出必须附担保成立前提(校准集来源与规模),不得宣传无条件精度。 | CONTRACTS-V3.md §0 |
| 15 | 测试零外呼、零真实门户、零真实 VLM 调用(mock 传输层),同前。 | CONTRACTS-V3.md §0 |

### V4 · 全平台统一接入(CONTRACTS-V4.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 16 | 默认零外呼不变:本地提供方数据不出本机、免 `vlm_online` 闸门但须显式选择;云端提供方沿用 `vlm_online`+密钥双条件。 | CONTRACTS-V4.md §0 |
| 17 | 密钥绝不入日志/manifest/异常消息(统一经 `security.redact` 口径;请求头只在 debug 级打码打印)。 | CONTRACTS-V4.md §0 |
| 18 | 提供方目录的模型名/端点只是提示:以官方文档为准,可覆盖;文档必须写明"上线前核验一次"。 | CONTRACTS-V4.md §0 |
| 19 | 跨平台共用一本预算账:任何真实 VLM 外呼(含 failover 第二跳、多平台 ensemble)都走 `vlm_cache.spend_one`。 | CONTRACTS-V4.md §0 |
| 20 | ping/doctor 是唯一允许外呼的诊断动作,且只能由人在 vlmctl 里手动触发;常规扫描/测试零外呼。 | CONTRACTS-V4.md §0 |

### V5 · 工程升级规则(CONTRACTS-V5.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 21 | 对外行为与 API 冻结:公共签名不变(允许新增带默认值 keyword-only 参数);基座文件禁改;既有测试必须原样通过(例外须逐条报告)。 | CONTRACTS-V5.md §0 |
| 22 | 每项升级要可验证:性能/健壮性改动必须有新增测试锁定或可量化说明;禁止无依据的顺手重构。 | CONTRACTS-V5.md §0 |
| 23 | 升级不引入新第三方依赖;遥测统一接 telemetry(零依赖、只存名称与数字、绝不存密钥/URL 内容)。 | CONTRACTS-V5.md §0 |

### V6 · 批量流水线(CONTRACTS-V6.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 24 | 批量举报仍是逐条人工门:批量只是顺序编排,验证码输入与最终确认仍由人工完成;批量代码不得出现 auto_confirm=True 真实提交路径。 | CONTRACTS-V6.md §0 |
| 25 | 批量确认声明(留痕):案件组进批量队列前必须逐组人工核验并显式声明(声明文本入审计日志);无声明的组不得提交。 | CONTRACTS-V6.md §0 |
| 26 | 批量模式频控不得放宽:`batch_item_interval_s >= submit_min_interval_s`(config 强制校验);每日上限照常,额度用尽挂起可续批。 | CONTRACTS-V6.md §0 |

### V6.5 · 线索发现(无独立契约文件,CHANGELOG 记账)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 27 | 发现即线索:discovery 产出只是待核查线索,不构成处置/举报依据。 | CHANGELOG.md V6.5 节 |
| 28 | 关键词零内置 + 默认零外呼:搜索关键词全部由运营者提供(discovery_online 默认 False),不内置任何词表。 | CHANGELOG.md V6.5 节 |

### V7 · 内核进化(CONTRACTS-V7.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 29 | 零 API 破坏:新内核全部为新增文件/keyword-only 参数/注册名;既有模块不改;开关默认值=旧行为。 | CONTRACTS-V7.md §0 |
| 30 | 学习型内核零外呼:可靠性加权/n-gram 等只从本地反馈与内置合成语料学习,绝不联网取数、绝不调 VLM 训练。 | CONTRACTS-V7.md §0 |
| 31 | 基准可复现:新内核测试至少一个操作计数/复杂度断言(禁依赖墙钟的脆弱断言)。 | CONTRACTS-V7.md §0 |

### V8 · 接管与向导(CONTRACTS-V8.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 32 | 本地探测仅限回环:自动接管只探测 127.0.0.0/8 上配置端口的 OpenAI 兼容服务;绝不扫外网与回环全端口段;仅首次初始化与显式触发时探测。 | CONTRACTS-V8.md §0 |
| 33 | 密钥只进不显:向导/切换的密钥输入为 password 语义(回显/日志一律掩码);落盘仅经 security.keys;REST/页面响应绝不包含密钥本体。 | CONTRACTS-V8.md §0 |
| 34 | 接管不放宽既有纪律:云端仍受 vlm_online+密钥双条件(红线 16)与预算(红线 19);无模型时 stub 兜底必须明示"离线桩,非模型判定"。 | CONTRACTS-V8.md §0 |

### V9 · 三档并发与结案代理(CONTRACTS-V9.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 35 | 压榨边界:高档并发只作用于本地计算与本地回环 IO;对外礼貌间隔/引擎限速/举报频控(红线 26)一概不放宽;进程池不得 pickle 分类器实例/连接。 | CONTRACTS-V9.md §0 |
| 36 | 结案代理无自主提交权:SummaryAgent 只做汇总与举报准备;SequentialReportAgent 只编排 run_batch(恒 auto_confirm=False),逐组声明+逐条 HUMAN_GATE+频控全部沿用。 | CONTRACTS-V9.md §0 |
| 37 | 资源治理:任一执行器 workers ≤ CPU 核数;CPU 过载让位保护可中断;并行任务有总量与超时上限,失控不留孤儿进程/线程。 | CONTRACTS-V9.md §0 |

### V9.1 / V10.0 · 真实接入与举报体验(负责人直研,CHANGELOG 记账)——与 V10 契约重号,见 §二

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 38a | 真实接入 ≠ 自主运行:golive 只做只读验证(GET 首页取样 512B,不解析表单不提交);密钥/目标清单/逐组声明/每条验证码与最终提交永远属于运营者。 | CHANGELOG.md V9.1 节;代码引用见 `netsentinel/golive.py` |
| 39a | 个人信息只进表单不进日志(掩码);举报理由只基于已核实事实、禁止编造;≤39 字硬校验。 | CHANGELOG.md V10.0 节;代码引用见 `contracts.py` / `submit/reason_gen.py` / `submit/profile.py` |

### V10 · 认证与反脆弱(CONTRACTS-V10.md §0)——38/39 与上一组重号,见 §二

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 38b | 统计诚实性:conformal 担保以认证层落地;真率 100% 的 CP 下界按闭式 δ^(1/n) 报告,禁止有限样本宣称绝对精度;对抗评测单点估计必须伴随 CI/检验字段。 | CONTRACTS-V10.md §0;代码引用见 `netsentinel/vision/guard_calib.py`(该文件 :53 自注"均指 38(B) 面") |
| 39b | 透明日志完整性:审计 JSONL 的 Merkle 接入为安全失效设计(异常不丢审计本体,降级标记后停用树);checkpoint 根经 HMAC-SHA256 签名,包含性证明可离线独立验证(verify 绝不抛错只返回 False)。 | CONTRACTS-V10.md §0 |
| 40 | 等待反脆弱与红线隔离:`wait_until` 为可选增强,缺省路径字节级不变;三级兜底链每级落 step notes;HUMAN_GATE/验证码别名拒绝逻辑对任何等待升级不可触碰。 | CONTRACTS-V10.md §0 |

### V11 · 签名与弃权(CONTRACTS-V11.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 41 | 非对称签名与时间证明:Ed25519 签名块内嵌 public_key 供第三方验签;旧 HMAC 包字节级兼容;TSA 外呼仅显式配置、默认全离线,时间证明纳入签名载荷回拨不可抵赖。 | CONTRACTS-V11.md §0 |
| 42 | 候选链防绕过:验证码别名拒绝对 selector_chain 逐候选生效;链首必须与主选择器同源;胜者候选只进进程内缓存+审计,绝不回写门户 YAML;HUMAN_GATE/FOCUS 步骤不携带候选链。 | CONTRACTS-V11.md §0 |
| 43 | 弃权非档位:abstain 是复核优先级信号,绝不进入 verdict 三档判定公式;sweep 与 decide 的阈值刻度互补对应,接线不得混用。 | CONTRACTS-V11.md §0 |

### V12 · 草稿与追踪(CONTRACTS-V12.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 44 | 草稿不生效:form_scanner 产出仅为门户 YAML 草稿——绝不自动加载、绝不落位 portals/ 目录;验证码类字段任何信号命中即整体排除;无 id/重复 id 字段标 manual_required。 | CONTRACTS-V12.md §0 |
| 45 | trace/弃权默认可关且不触判定:trace_enabled/abstain_enabled 缺省 False=现状逐字一致;abstain 只置 needs_review 标记+队列提权,绝不进三档公式;X-Trace-Id 与 /trace 仅本机只读元数据。 | CONTRACTS-V12.md §0 |

### V13 · 事件账本与 AIMD(CONTRACTS-V13.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 46 | 事件账本不可变:event_log 全库 append-only(API 无改删路径+DB 触发器拦截+replay 对账暴露手迹);复核状态变更必须先账本后状态。 | CONTRACTS-V13.md §0 |
| 47 | AIMD 只收紧不放宽:并发窗口 cap 恒等于注入档位 workers(≤cores,红线 37),floor≥1;对外礼貌间隔/频控在数学上不可能被 AIMD 放宽(与红线 35 叠加)。 | CONTRACTS-V13.md §0 |

### V14 · 多哈希候选与草稿落位(CONTRACTS-V14.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 48 | 多哈希候选语义:mirror/ring/pyramid 命中是候选生成而非判定(各哈希有已知混淆面),候选必须经 phash256 与人工复核确认。 | CONTRACTS-V14.md §0 |
| 49 | 草稿落位双门槛:webui 草稿落位=显式确认口令(须与门户名完全一致)+临时文件全链校验通过;逐字段确认无批量捷径;落位后封存不可变更;落位 ≠ 生效。 | CONTRACTS-V14.md §0 |

### V15 · 动态门户与预算哨兵(CONTRACTS-V15.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 50 | 动态门户显式启用:portals/ 目录门户必须 YAML 内显式 `enabled: true` 且 entry_url_key 存在于 Config 才可加载;草稿工具恒写 `enabled: false`;内置门户豁免。 | CONTRACTS-V15.md §0 |
| 51 | 预算哨兵不执行:批预算超限只产生告警+遥测+结果标记——绝不删除已产出文件、绝不修改复核队列状态、绝不阻断收官;unpriced 调用单独诚实计数。 | CONTRACTS-V15.md §0 |

### V16 · 留痕补齐与镜像候选(CONTRACTS-V16.md §0)

| 编号 | 一句话 | 出处 |
| --- | --- | --- |
| 52 | 留痕补齐独立确认链:approvals 愈合是独立于 entries 状态补齐的第二条确认链,两清单两确认绝不捆绑;补写 actor 恒为 "replay-heal" 与真人署名可区分;补写后复跑对账归零。 | CONTRACTS-V16.md §0 |
| 53 | 镜像候选不直接并团:mirror_near 边独立于 phash_near;团伙 connectivity 判定默认不含镜像边(须显式开启),community 模式乘法降权——镜像命中只能作候选关联。 | CONTRACTS-V16.md §0 |

## 二、编号 38/39 重号歧义(V10.21 起正式定名 38a/38b、39a/39b)

编号 **38** 与 **39** 各被**两处独立来源**使用;**V10.21 席位4 经 [CONTRACTS-V18.md](../CONTRACTS-V18.md) §2 正式消歧定名:38a/39a = A 面,38b/39b = B 面**,与本文历史"A 面/B 面"叫法逐一对应:

| 编号 | a 面(负责人直研,CHANGELOG 记账) | b 面(V10 并行代理轮,CONTRACTS-V10.md §0) |
| --- | --- | --- |
| **38** | **38a · 真实接入 ≠ 自主运行**(V9.1,2026-10-02):golive 只读验证,密钥/声明/验证码/提交永远属于运营者。引用方:`netsentinel/golive.py`、docs/GO_LIVE.md | **38b · 统计诚实性**(V10 轮):conformal 认证层、CP 下界闭式报告、对抗评测须伴 CI/检验。引用方:`netsentinel/vision/guard_calib.py`(guard_calib 引用即此面) |
| **39** | **39a · 个人信息只进表单不进日志**(V10.0,2026-10-03):掩码、理由只基于已核实事实、≤39 字。引用方:`contracts.py`、`submit/reason_gen.py`、`submit/profile.py`、`submit/form_models.py` | **39b · 透明日志完整性**(V10 轮):审计 Merkle 安全失效设计、checkpoint 根 HMAC-SHA256 签名、离线包含性证明。引用方:`netsentinel/security/merkle.py`、`netsentinel/logging_util.py` |

成因:V9.1/V10.0 两轮为负责人直研(红线记入 CHANGELOG,无独立契约文件);而 CONTRACTS-V10 是 V10 轮 20 代理并行契约,其 §0 自行从 38 起编号("累计 40 条"),未对账直研轮的记账。此后 V11 起统一从 41 续编,两套 38/39 均被后续代码按各自语义引用,**未做合并**(至 V10.21 定名消歧,存量裸引用不强制回改)。

阅读与引用约定:新引用一律**带字母后缀**(38a/38b/39a/39b);存量"红线 38/39"以**引用方的语境**判读(安全/隐私语境=a 面,统计/审计语境=b 面);guard_calib.py 内全部"红线 38"字样即 38b(该文件 :53 已自注)。

## 三、V17 全席共约(CONTRACTS-V17.md §0,不续编号)

V10.15"创世纪"架构升级波对 6 席位(G1–G6)的全席约束:

1. **文件互斥**:每席位只准写自己领地源文件+新测试文件;跨领地改动由负责人收口。
2. **禁改基座**:contracts.py / pyproject.toml / config.example.yaml / conftest.py / 他人领地 / CHANGELOG.md。
3. **零 API 破坏**:只允许新增带默认值参数或全新函数;默认路径与升级前等价。
4. **既有测试**:领地内既有测试必须全绿;按红线 21 惯例更新断言须逐条说明。
5. **纪律**:零新依赖、测试零外呼/零真实浏览器/零真实门户;中文报错;不 git 提交。
6. **诚实边界**:优化经核实不可行/不安全 → 如实报告"放弃+原因",严禁为改而改。

## 四、V10.16 附注:自动上传不触碰红线 1/2

V10.16 的证据截图自动上传(Top-10 池,12377 十张/shdf 五张)发生在 **HUMAN_GATE 之前**,属辅助动作:上传失败只记 notes 降级为人工门手动上传,绝不中断举报;验证码的识别与输入、最终提交的确认仍永远属于人工(与 V10.1 FOCUS 半自动焦点同一设计哲学)。详见 `submit/evidence_pool.py` 模块 docstring 与 [ETHICS.md 第六节](ETHICS.md)。

## 五、V10.19–V10.21 无新增编号红线(V10.21 席位4 grep 核实)

V10.19(关键词工作台)/ V10.20(创新旗舰+深度修复)/ V10.21(遗留清偿收官,四席位)三轮均**无新增编号红线**:编号仍止于 53(V16 第 52/53 条),实质 55 条(38/39 各两面);CHANGELOG V10.19/V10.20 两节及 V10.21 各席位领地均无新红线立项(全仓 grep"红线 54/55/56"零命中);各轮仅复引既有红线(红线 1/2/19/28 等)并保持"新接线默认关闭、主线逐字节不变"纪律。38/39 的正式消歧(38a/38b、39a/39b)见 §二与 [CONTRACTS-V18.md](../CONTRACTS-V18.md) §2。

---

相关文档:[ETHICS.md](ETHICS.md)(红线语境版)、[USAGE.md](USAGE.md)(红线在 CLI/配置中的落点)、[../CHANGELOG.md](../CHANGELOG.md)(各轮记账)、根目录 `CONTRACTS.md` 系列(原文;主契约 §3/§5 勘误见 [../CONTRACTS-V18.md](../CONTRACTS-V18.md) §1)。
