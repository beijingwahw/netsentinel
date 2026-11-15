# NetSentinel 架构说明(ARCHITECTURE)

本文面向开发者与评审者,说明 NetSentinel 的 L0–L5 分层架构、模块职责、数据流与契约机制。一切以 `CONTRACTS.md` 系列(17 份,禁改)与 `netsentinel/contracts.py`(数据结构,禁改)为准;本文不引入契约之外的任何行为承诺。

## 1. L0–L5 分层总览

系统按"基座 → 内核 → 设施 → 能力 → 流程 → 消费端"六层组织(自下而上依赖;上层可依赖下层,反向即违约):

| 层 | 内容 | 关键模块 |
| --- | --- | --- |
| **L0 契约与基座** | 数据契约唯一事实源、数值微库、遥测 | `contracts.py`(数据类+Config,被 88 处 import) / `mathx.py`(纯函数,零内部依赖,可选 numpy) / `telemetry.py`+`telemetry_export.py`+`telemetry_trace.py`(计数计时/Prometheus 导出/全链追踪;只存名称与数字) |
| **L1 情报内核** | 近重复识别、域归并、图谱团伙、文本风险、调度内核 | `intel/phash`(256bit 指纹+mirror/ring/pyramid 候选哈希+LSH 分带)、`intel/canonical`(可注册域归并)、`intel/graph`(增量并查集+团伙 connectivity/community)、`intel/text_kernel`+`url_intel`+`text_intel`(n-gram/URL 风险)、`ops/sched_kernel`(优先级×预算轮选) |
| **L2 设施** | 抓取、识别、配置、存储、安全、并发运营 | `crawler/`(fetcher 禁网闸门、browser 抽样+会话复用、capture_v2 滚动采样、site_map BFS、redirect)、`vision/`(37 文件:20 家提供方、vlm_client、缓存预算、ensemble、failover、并行分类)、`config.py`+`logging_util.py`+`atomic.py`、`storage/`(event_log append-only 事件账本、replay 重放对账)、`security/`(keys 密钥环、vault 脱敏+审计哈希链、bundle_sign 签名、merkle)、`ops/`(concurrency 三档、cpu_profile、pool、aimd、bulk_intake、batch_scan、scheduler 巡查、audit_verify) |
| **L3 能力** | 判定、证据、提交、编排、代理、报告 | `decision/`(verdict 判定公式、review_queue 队列、fusion/fusion_reliable 融合、conformal 共形、sprt 序贯、abstain 弃权、four_eyes 四眼、batch_review/dedup_policy/triage)、`evidence/`(packager 证据包、merge_bundles 合并、parallel_pack 并行打包)、`submit/`(18 文件:form_models 13 键选择器+18 步计划、双门户 planner、executor_playwright/executor_session 双执行器、rate_limit 频控、evidence_pool Top-10 池、reason_gen 理由生成、profile 个人模板、batch_precheck/batch_state/batch_submit 批量、playbook_gen 剧本、form_scanner 门户扫描、portal_defs 动态门户)、`pipeline/`(orchestrator 编排收口、takeover 自动接管、kernel_wire 内核接线)、`agent/`(summary_agent 汇总、sequential_report 依次举报;零自主提交)、`report/`(batch_report/html_report/run_summary)、`policy/engine`、`notify/hub` |
| **L4 流程** | 批量、会话、发现、上线、模型管理、向导 | `batchflow.py`(筛选→归组→续批)、`finishflow.py`(收官+举报准备+批量预审)、`sessionflow.py`+`sessionstore.py`(跨会话累积/存档)、`discovery/`(yandex/searxng/keywords/pipeline 线索发现)、`golive.py`(上线体检)、`modelmgr.py`(模型管理 CLI)、`setup/`(连接向导,纯标准库自托管,端口 8766) |
| **L5 消费端** | 五种形态 | CLI(`__main__.py` scan/queue/submit)、TUI(`cli/review_tui`+`cli/batch_tui`,纯标准库无 curses)、REST(`service/app.py` FastAPI 工厂+`service/model_api.py` /model 路由,默认 127.0.0.1:8765)、WebUI(`webui/` Streamlit 六页)、Node 驱动(`drivers/driver.mjs` computer-use 剧本) |

**机器可验证的分层契约**(pyproject `[tool.importlinter]`,`lint-imports` 执行)只约束主干链:

```
cli → submit → decision → vision → intel → crawler → agent → config → mathx → contracts
```

三条附加禁令:contracts 基座零内部依赖(叶子);mathx 数值内核零内部依赖(红线 29);agent 不上溯业务流水线(红线 36 无自主提交权的结构表达)。未列入主干层的组(ops/security/pipeline/telemetry/discovery/evidence/notify/report/policy/service/setup/storage/logging_util/modelmgr/batchflow/finishflow/golive/telemetry_export/telemetry_trace)不受分层契约约束——其包级 `__init__` 均为空壳,不构成间接上溯链。已知与直觉相反的两处事实:submit→agent 真实存在(batch_precheck 复用 sequential_report),故 agent 排在 submit 之下的层;decision→intel 真实存在(batch_review/dedup_policy/sprt),故二者分层不并置。

## 2. 数据流

### 2.1 单站扫描(scan)

```mermaid
sequenceDiagram
    participant OP as 运营者
    participant CLI as CLI(__main__)
    participant ORC as orchestrator
    participant CR as crawler
    participant VI as vision
    participant VD as decision.verdict
    participant EV as evidence.packager
    participant Q as review_queue

    OP->>CLI: netsentinel scan --url
    CLI->>ORC: run_scan(url, cfg)
    ORC->>CR: discover_links(BFS, ≤max_pages)
    loop 每页(三档并发)
        ORC->>CR: capture_page / capture_pages_v2(会话复用)
        CR-->>ORC: PageSample(截图+图片+text_hint_hits)
    end
    ORC->>VI: classify_batch(ensemble_members;VLM 走缓存+预算)
    VI-->>ORC: 各模型 ImageScore
    ORC->>VI: ensemble_scores(加权平均/reliability 融合)
    ORC->>VD: assess(§4 判定公式)
    VD-->>ORC: SiteReport(verdict/needs_review)
    alt needs_review(非 clean)
        ORC->>EV: build_bundle(manifest+summary+zip)
        ORC->>Q: add(入队+审计日志)
    end
    CLI-->>OP: 判定/证据包路径/队列 id
```

### 2.2 提交举报(submit,18 步计划)

```mermaid
sequenceDiagram
    participant OP as 运营者
    participant CLI as CLI(__main__)
    participant ORC as orchestrator
    participant Q as review_queue
    participant RL as RateLimiter
    participant PL as planner(portal_12377/shdf)
    participant EX as executor(playwright/session)
    participant PT as 举报门户表单

    OP->>OP: 人工查看证据包(summary.md+截图+Top10)
    OP->>CLI: queue approve --id → submit --id --portal --exec
    CLI->>ORC: run_submit(entry_id, portal, cfg)
    ORC->>Q: get(entry_id)——必须 approved
    ORC->>RL: can_submit()(间隔+当日次数;批量另受 batch_item_interval_s)
    ORC->>PL: plan_12377 / plan_shdf(18 步计划,含 FOCUS+HUMAN_GATE)
    ORC->>EX: execute(plan, cfg)
    EX->>PT: goto → wait → select → fill×9(链接/描述/姓名/电话/邮箱/身份证/地址/邮编/单位)
    EX->>EX: screenshot(填写完成)
    Note over EX: V10.16:自动上传证据截图(Top-10 池按门户配额:12377 十张/shdf 五张);失败降级人工
    EX->>PT: focus(验证码框,半自动焦点)
    EX-->>OP: HUMAN_GATE:核对附件与已填信息 → 人工输入验证码 → 回终端回车
    EX->>PT: click(submit) → wait → screenshot(提交结果)
    EX-->>ORC: ExecutionResult(ok/submitted/截图清单)
    ORC->>Q: mark_submitted
    ORC->>RL: record()(频控记账)
    ORC-->>CLI: 执行结果
```

### 2.3 批量流水线(V6/V9)

```
discovery(线索,yandex/searxng,默认零外呼)
  → batchflow --input(批量导入→并发扫描→三级归纳:canonical 域→镜像变体→图片指纹重叠团伙)
  → cli.batch_tui(逐组人工声明,留痕;红线 25)
  → finishflow --report / --precheck(SequentialReportAgent 依次举报,恒 auto_confirm=False,红线 36)
  → batch_report.html(结案报告;额度用尽 --resume 续批)
```

文字要点:

- 编排收口在 `pipeline/orchestrator`:对兄弟模块一律函数内惰性导入,缺失时抛中文 RuntimeError 指明缺哪个模块。
- `capture_page` 的 Playwright 为惰性导入:无 playwright 时退化为仅抓 HTML 解析图片链接;批量走 `capture_pages_v2`/会话复用,一次 launch 逐页复用(V7/V10.13)。
- 执行异常被捕获,记录 `stopped_at` 与 `ok=False`,不静默失败;每一步截图到 `data/runs/<ts>/`。
- 三档并发(V9):low(cores/4)/mid(cores/2)/high(cores-reserve);高档只加速本地计算与回环 IO,对外礼貌间隔/频控不变(红线 35);AIMD 在此之上只收紧不放宽(红线 47)。

## 3. 模块职责表(当前实况,按包)

| 包 / 文件 | 职责(一句话) | 关键 API |
| --- | --- | --- |
| `contracts.py` | 共享数据契约(禁改):Verdict/Portal/StepAction 枚举、PageSample/ImageScore/SiteReport/EvidenceBundle/SubmissionPayload/SubmissionPlan/ExecutionResult/Config | 数据类与 `as_dict()` |
| `config.py` / `logging_util.py` / `atomic.py` / `sessionstore.py` | 配置加载校验保存 / 日志与 JSONL 审计 / 原子写 / 会话存档 | `load_config` `save_config`;`setup_logging`;`atomic_write` |
| `mathx.py` | 数值微库(纯函数,零内部依赖,可选 numpy) | 数值工具集 |
| `telemetry*.py` | 遥测计数计时(只存名称与数字)/ Prometheus 导出 / X-Trace-Id 全链追踪 | `inc` `timer`;`trace` |
| `crawler/` | HTTP 抓取与禁网闸门(fetcher)、页面抽样+会话复用(browser)、滚动懒加载采样(capture_v2)、BFS 链接发现(site_map)、重定向(redirect) | `fetch_page` `download_images` `capture_page` `discover_links` |
| `vision/` | 20 家 VLM 提供方(providers/vlm_client/prompt_dialects/failover/provider_throttle)、缓存与预算(vlm_cache/cache2/cost_meter)、集成(ensemble/arbiter/multi_provider)、并行分类、本地探测(local_probe/local_gateway)、模型管理(model_manager/model_catalog)与 vlmctl、守卫(guard_adapter/guard_calib)、页面级 VLM(page_vlm) | `PROVIDERS` `resolve` `ensemble_scores` |
| `intel/` | 感知哈希与候选哈希(phash/phash_lsh)、域归并(canonical)、图谱与团伙(graph/graph_kernel/group_linker)、案件组(case_group/group_stats)、文本与 URL 风险(text_kernel/url_intel/text_intel)、站点记忆(site_memory)、命名建议(name_suggest)、法规(regulation) | `find_similar` `canonical_host` |
| `decision/` | 三档判定(verdict)、sqlite 复核队列(review_queue)、融合(fusion/fusion_reliable/reliability)、共形担保(conformal)、序贯检验(sprt)、分歧弃权(abstain)、四眼(four_eyes)、批量复核与去重(batch_review/dedup_policy)、分流(triage) | `assess` `ReviewQueue` |
| `evidence/` | 证据包构建(packager)、跨条目合并(merge_bundles)、并行打包(parallel_pack) | `build_bundle` |
| `submit/` | 13 键选择器+18 步计划(form_models)、双门户 planner(portal_12377/portal_shdf)+动态门户(portal_defs)、双执行器(executor_playwright/executor_session)、频控(rate_limit)、Top-10 证据池(evidence_pool)、理由生成(reason_gen)、个人模板(profile)、批量三件套(batch_precheck/batch_state/batch_submit)、剧本(playbook_gen)、门户扫描草稿(form_scanner) | `SELECTORS` `build_plan` `execute` |
| `pipeline/` | 编排收口(orchestrator)、自动接管(takeover)、内核接线(kernel_wire) | `run_scan` `run_submit` |
| `agent/` | 结案汇总(summary_agent)、依次举报(sequential_report)、案件流(case_flow/case_agent)——零自主提交 | `SequentialReportAgent.run` |
| `batchflow.py` / `finishflow.py` / `sessionflow.py` | 批量筛选归组续批 / 收官+举报准备+批量预审 / 会话累积三流程 CLI | `main(argv)` |
| `discovery/` | 搜索引擎线索发现:yandex XML、自托管 searxng、关键词装载、pipeline | `main(argv)` |
| `ops/` | 三档并发(concurrency)、CPU 画像(cpu_profile,psutil 可选)、线程/进程池(pool,V10.35 起 stop_check 优雅排空)、AIMD(aimd)、批量导入(bulk_intake)、批量扫描(batch_scan)、巡查(scheduler,站点记忆+webhook 通知)、审计校验(audit_verify)、动态 TTL(adaptive)、档位状态(tier_state)、调度内核(sched_kernel)、负载让位(load_guard)、优雅退出(graceful,V10.35 两段式中断协调器)、站级扫描日志(scan_journal,V10.35 断点续跑) | `io_workers` `run_once` |
| `security/` | 密钥环(keys:keyring 可选)、vault(密钥解析+脱敏+审计哈希链)、证据包签名(bundle_sign:hmac-sha256/ed25519)、Merkle(merkle)、时间证明(timestamp)、威胁模糊内核(threat_kernel) | `get_key` `AuditChain.verify` |
| `storage/` | 事件账本(event_log:append-only+触发器拦截+批量口 append_many)、重放对账(replay)、存储内核(kernel:版本化迁移) | `EventLog` `replay` |
| `service/` | FastAPI 工厂(app.py:/scan /jobs /trace /queue /plan /health + X-Trace-Id)、模型路由(model_api:/model/active /switch /probe /test) | `create_app` `create_model_router` |
| `setup/` | 连接向导(server/page/render/flow/trigger/daemon;纯标准库单页,端口 8766,占用自动 +1..+5,守护单例) | `serve` |
| `cli/` | 复核 TUI(review_tui:--four-eyes/--event-ledger)、批量声明 TUI(batch_tui)——纯标准库,绝不自动批准/提交 | `main(argv)` |
| `modelmgr.py` / `golive.py` | 模型管理 CLI(status/probe/list/switch/test/takeover/serve)/ 上线体检(check --net / prepare) | `main(argv)` |
| `policy/` `report/` `notify/` | 声明式政策引擎 / HTML·批量·收官报告 / webhook 通知(钉钉/飞书/企微/通用) | `engine` `render_*` `notify_webhook` |
| `webui/` | Streamlit 复核台六页:队列/分组/模型/提供方/运行/仪表盘+草稿流(draft_flow_page) | `streamlit run webui/app.py` |
| `drivers/driver.mjs` | computer-use 剧本驱动(Node;人工驱动同一份 SubmissionPlan) | —— |
| `portals/` | 门户 YAML 定义(12377.yaml/shdf.yaml;动态门户须显式 `enabled: true`,红线 50) | `list_portals` |
| `tests/` `benchmarks/` `scripts/` | 6566 项测试(离线零外呼,mock 门户)/ kernel_bench 内核自检 / 离线演示脚本 | `python -m pytest tests/ -q` |

## 4. 契约机制解释

### 4.1 `contracts.py` 数据类(唯一事实源)

| 数据类 / 枚举 | 用途 | 关键字段 |
| --- | --- | --- |
| `Verdict(str, Enum)` | 站点判定三档 | `clean` / `suspect` / `nsfw` |
| `Portal(str, Enum)` | 举报渠道 | `12377` / `shdf` |
| `StepAction(str, Enum)` | 步骤动作 | `goto/click/fill/select/wait/screenshot/human_gate/focus` |
| `SiteReport` | 站点级判定报告 | `site_url/pages/image_scores/agg_nsw_prob/nsw_image_count/verdict/needs_review/created_at`(+V2 `intel` 融合特征) |
| `SubmissionPayload` | 一次举报的全部填写信息 | portal/site_url/category/description/evidence_zip/reporter_*(8 字段)+reason |
| `SubmissionPlan` | 声明式步骤计划 | `portal/entry_url/payload/steps`(18 步) |
| `ExecutionResult` | 计划执行结果 | `ok/portal/screenshots/stopped_at/submitted/notes` |
| `Config` | 全局配置(默认值即安全值) | 按 V1→V14 版本分组,见 [USAGE.md §5](USAGE.md) |

### 4.2 `SELECTORS`(13 键,冻结)

见 [USAGE.md §4.1](USAGE.md);本地 mock 与两个 portal planner 共用同一份(单一事实来源,V5 冻结为不可变映射)。18 步标准序列见 [USAGE.md §4.2](USAGE.md)。

### 4.3 判定公式(CONTRACTS §4,`assess` 唯一实现依据)

```
candidates = [s for s in ensemble if s.image.width >= cfg.min_image_px
                                   or s.image.height >= cfg.min_image_px]
agg        = max((s.nsfw_prob for s in candidates), default=0.0)
nsw_count  = sum(1 for s in candidates if s.nsfw_prob >= cfg.prob_count_line)
verdict    = NSFW    if agg >= cfg.nsfw_threshold and nsw_count >= cfg.min_nsw_images
            else SUSPECT if agg >= cfg.review_threshold
            else CLEAN
needs_review = (verdict != CLEAN)   # NSFW 同样必须人工确认后才允许提交
```

要点:先剔除小图 → 聚合分取候选图最大值(避免正常图稀释)→ NSFW 需"单图高置信+达标图计数"双条件 → 非 clean 全部 `needs_review`。弃权(abstain,V12)只提权入队,**绝不进入三档公式**(红线 43/45)。

### 4.4 审核队列状态机

`pending → approved / rejected → submitted`(见 [USAGE.md §2](USAGE.md));`run_submit` 前置校验条目必须 `approved`;每一次状态迁移落审计日志(`data/audit.jsonl`,V2 起哈希链,V10 起 Merkle 树);V13 起可选事件账本(先账本后状态双写,可离线重放对账)。

### 4.5 契约对账机器

- `tests/test_contracts_sync.py`:AST 提取 `Config` 字段,与 CONTRACTS-V9/V11/V12/V13/V14 文档声明做双向对账(代码有而文档缺 / 文档有而代码缺 / `cfg.<name>` 引用完整性 / 默认值一致性)。
- `pyproject.toml [tool.importlinter]`:主干分层契约+三条禁令,`lint-imports` 执行(见 §1)。
- `tests/test_cost_meter.py::test_pyproject_analytics_extra_minimal`:pyproject 核心依赖零变化与 extras 对账。

## 5. 并行开发与测试策略

- **契约先行**:每轮升级先出冻结契约文件(CONTRACTS-V*.md),代理只写自己领地文件;跨模块改动由负责人裁定收口。
- **importorskip 容错**:兄弟模块与可选三方依赖缺失时跳过而非失败;测试注入 fake,不下载模型、零外呼。
- **惰性导入**:三方库与兄弟模块一律函数内惰性导入;orchestrator 可在模块不全的环境中降级启动。
- **测试分层**:单元(每模块)→ 贯通(本地 mock 门户 127.0.0.1)→ 端到端(stub/dry_run);全部离线、零真实门户;当前 6566 项。
- **基准可复现**:V7 内核各有 `test_v7_bench_*` 操作计数断言(禁墙钟);`benchmarks/kernel_bench.py` 内核自检总控。

## 6. 相关文档

- [USAGE.md](USAGE.md) —— 全部 CLI 入口、SELECTORS 13 键、18 步序列、Config 分组
- [ETHICS.md](ETHICS.md) / [REDLINES.md](REDLINES.md) —— 红线体系
- [API.md](API.md) —— REST 端点
- [../CONTRACTS.md](../CONTRACTS.md) 系列(17 份)与 [../netsentinel/contracts.py](../netsentinel/contracts.py) —— 契约原文(冲突时以契约为准)
