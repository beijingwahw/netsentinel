# 变更总账(NetSentinel 净网哨兵)

## V10.38(2026-11-10)—— P2-6 SPRT 实时早停落地 + 内核门禁执行器:性能重构提案最后一块拼图,「门禁通过且运营者批准前不开」从纪律变成制度

背景:性能重构提案九项已完成八项,唯一剩余的 **P2-6 SPRT 实时早停**是全提案**唯一动准确率**的项(早停改变「哪些图被评分」;α/β=0.05 错误率担保以校准概率为前提)。地基此前已全部就位:SPRT 数学内核(V7 Wald + A160 mixture-SPRT/e-process)、校准测量与五判据门禁(V10.33)、标注工作台与整组导入(V10.36/V10.37)、流水线分块消费循环(V10.31)——但 `use_sprt=true` 只触发 kernel_wire 的**事后演算**(`run_scan_v7` 无生产调用方),真实送审从不早停;且「门禁通过且运营者批准前不开」只写在文档里,**没有任何代码强制**。本波补齐两块:①把早停代码路径建成(默认关、三道锁锁死);②把门禁从纪律变成制度(代码强制、批准绑定测量指纹)。**运营者完成标注那天,开启只差一条 `calibration approve` 命令。**

- **新模块 `netsentinel/vision/kernel_gate.py`(门禁执行器,零外呼)**:**三道锁**全部可复算,任何一道缺失即拒开——①**测量锁**:`<data_dir>/calibration/report_<模型>.json` 存在且五判据全过,且报告模型 == 当前主分类器(换模型=没测过=拒);②**批准锁**:`kernel_approval.json` 存在且 kernel/model 匹配——批准是一次**显式人工动作**(approve 子命令,报告未过时拒绝写入,绝不自动生成);③**指纹锁**:批准内的 `digest`(sha256 over 模型+五判据结论的稳定 JSON)必须等于当前报告的 `gate_digest`——**批准与「被测量的那一份数据」绑定**,模型重测/标注集变化 → 判据 detail 变 → 指纹变 → **旧批准自动失效**(想再开必须重新人批)。保守边界(宁严勿松,如实登记):多成员集成未测不开(校准测量只覆盖单一模型,融合概率的 SPRT 担保未建立,整环校准后放行);`GATED_KERNELS` 登记制(cascade 等整环校准体系建成后再入册);只读本地文件,绝不自动 ping(红线 20)。
- **calibration CLI 增补**:`save_report_json`——report 子命令在 md(给人看)之外**无条件落机读 JSON**(给门禁执行器看;`{model, generated_at, report, gate, digest}` 两份同源同刻);`--config` 可选(data_dir 定位,缺省 "data" 与 kernel_gate 读取口径同一约定);新子命令 **`approve --kernel sprt`**——仅在当前模型报告存在且门禁通过时可写(中文拒绝退出 1),重跑覆盖更新(批准永远是最新测量的快照)。
- **orchestrator SPRT 实时接线(仅默认流水线路径;`use_sprt` 缺省 False = 逐字节旧行为)**:
  - `_scan_once` 顶部:classifier 未注入且 `use_sprt` → 门禁评估;**未过 = 响亮降级**(中文 warning + `sprt.gate.refused` 计数 + `intel["sprt"]={"active": False, "reason": ...}`,全量送审旧行为——「不开」而非「不许扫」,扫描绝不阻塞);门禁执行器自身异常同样按未过降级(增强项失败不阻断主流程,家规);classifier 注入 / `scan_pipeline_overlap` 关闭 → inactive 标记(注入=调用方接管的既有哲学)。
  - `_pipelined_fetch_and_classify` SPRT 激活模式:站级 `SPRT(sprt_alpha, sprt_beta)` 随**已完成分块增量判停**(生产者逐页轮询 + 收割段两处喂入,按 future 身份防重复);**判停(统计结论已足)→ 停止爬取**(同早夭路径关生成器)+ 收割时取消未起跑分块(省预算);**在飞分块自然完成、结果照收**(已外呼的调用弃收=白烧;多收的证据只会让判定更稳,浪费有界=单分块);**`vlm_max_images_per_site` 首次成为真实站级送审上界**(放行项超出剩余配额截下 + `sprt.budget.skipped` 计数;预算满即停爬;`gate.stats.sent` 同步回扣,保持 sent=实际送审的诚实口径;**预算内已排队的分块照常执行**——取消会让账目与事实脱节,取消只发生在 SPRT 判停);SPRT 模式**不合并**排队分块(合并=一次性整批送审,与早停意志相悖)。
  - `_sprt_feed_chunk` 喂入口径:**带内错误分(nsfw_prob=0.0 + scores.error,A63 降级哨兵)绝不喂**——「识别没做成」不是「确认干净」的证据,方向性与 V10.24 失败感知一致;融合走 `_default_ensemble`(与站点判定同源;门禁只放行单成员,融合=成员分本身)。
  - `intel["sprt"]`(use_sprt 开启时才出现,只含名称/布尔/数字,红线 17;挂在融合块之后,同 sendgate):`{kernel, allowed, active, verdict, n_used(检验器吸收点,非整块), images_sent(=实际送审), budget_cap, budget_saved(截下+取消), stopped_early, stop_reason}`;**判定机器(assess 阈值/needs_review/聚合公式)零触碰**——SPRT 只决定「还送不送」,不改「怎么判」(min_nsw_images=3 ≤ Wald nsfw 停审下限,强站判定一致性有构造保证)。
  - `kernel_wire._sprt_intel` 事后演算保留(预算参考),`run_scan_v7` 见到 `active` 键**不覆写**(单一事实源)。
- **实测收益(A/B,`D:\3\verify_v10_38.py`,19/19 PASS,全部离线替身)**:**门禁全生命周期**——小样本(3 正 4 负)→ report 如实未过(C1)→ `use_sprt=true` 响亮降级(全量 18 张,intel inactive+理由,遥测计数)→ approve 拒绝写入;补足标注(25 正 30 负,生产链路 score,实测 ECE=0.0727/Brier=0.0057)→ report 五判据全过 → approve 写入 → **同一配置 SPRT 激活**:强 clean 站(0.05×18)**18 → 8 次调用(−56%)**,SPRT n_used=2(2 张即停,检验器吸收点),**verdict/needs_review/agg 与全量送审逐项一致**;强 nsfw 站 16 → 8 次调用,NSFW 判定保持;不确定站(0.73 永不判停)预算上界恰送 5 张(stop_reason=budget);**指纹锁**——一张图重测(判据数字变化)→ digest 换 → 旧批准自动失效 → 扫描降级全量送审(绝不静默)。
- **开发中抓出并修复的两处接线缺陷(如实登记)**:①预算截断顺序——`feed()` 已把整页计入 stats.sent,剩余配额须按**本批放行前**的已送量算(按截后余量算会错把本批当已花掉,首页只送 cap−N 张);②`budget_stop` 后收割曾取消排队分块——预算内已计入 sent 的图片必须送审完成(取消致账实脱节+竞态),取消只应发生在 SPRT 判停。
- **测试(+26 项全绿)**:`tests/test_kernel_gate.py` 18 项——指纹稳定/敏感(判据数字/模型变化)、三道锁逐锁拒开(报告缺失/模型不一致/门禁未过含未过判据名/无批准/kernel 不匹配/model 不匹配/报告重测指纹失配/多成员/未配 classifier)与全过放行(单成员配置)、批准落地(缺失/未过/模型不符拒绝写;覆盖更新)、机读报告结构(digest 自洽)、report CLI 落盘、approve CLI 全生命周期;`tests/test_sprt_wire.py` 8 项——强 clean 站早停(判定与全量逐项一致/n_used=吸收点 2/页数停止/images_sent=实际调用)、强 nsfw 站(n_used=6/NSFW 保持)、预算上界(恰送 N/截下记账/SUSPECT 复核线不动)、门禁未过降级(全量+inactive+遥测)、指纹过期降级、注入/串行 inactive 标记、错误分零喂入(n_used=0,V10.24 诚信升级照常)。
- **回归**:全量套件 exit 0 零 FAILED(`D:\3\full_suite_v1038.txt` 留痕);entry_check **29/29**(py -3.11;python314 缺 fastapi 属环境既有差异);受影响存量族(sprt/kernel_wire/scan_pipeline/calibration/orchestrator×4/e2e_stub)全绿。
- **配置文档**:`config.example.yaml` 收录 `use_sprt / sprt_alpha / sprt_beta`(V7 既有字段首次文档化,含三前提说明);无新增 Config 字段,`use_sprt` 缺省 False 逐字节旧行为。
- **红线保持**:SPRT 只截断对**自有 VLM** 的送审——对外礼貌节奏/频控/人工门/验证码零触碰(红线 35 等);判定公式/阈值/复核语义零变化;早停以校准门禁 + 显式人批为前提(α/β 担保成立的唯一制度保证);门禁只读本地文件零外呼;批准绑定测量指纹、重测即失效;降级语义=「不开」而非「不许扫」。

## V10.37(2026-11-10)—— 整组导入正/负样本入口:目录即标签,补齐门禁 C1 正样本的最短路径

背景:V10.36 标注工作台实测运营者标完 35 张(33 clean / 7 nsfw),门禁 C1 负样本 33/20 已达标、**正样本 7/20 差 13**——剩余缺口是"过往案件中已确认的整组色情图",逐张点击是纯重复劳动(目录内容运营者早已核实过)。

- **calibration 新增 `import_labeled_dir(s, dirs, label)`(数据集目录式标注语义)**:①新图直接入集为已标;②集内 **pending 条目升级**为已标(运营者显式判定覆盖"待标"——补齐正样本缺口的捷径);③**已标条目跳过绝不覆盖**(改标只能走显式改标路径,整组导入无权改既有标签——防误导覆盖人工资产);④`label` 白名单校验(仅 clean/nsfw)、目录不存在中文 ValueError;溯源 `human:dir-import`(与逐张点击 `human:webui`、CLI `human:console` 三路并列,报告 by_source 同归 human 层),note 记「目录整组导入(标签;来源)」留审计线索,升级条目保留原 note;返回 `{added, promoted, skipped}` 三路口径。
- **CLI 子命令 `calibration import`**:`--dirs 目录... --label clean|nsfw --set 标注集`——显式给出 `--label` 即视为对整组内容的判定(终端批量入口,与页面互通同一 JSONL);输出三路口径 + 当前统计。
- **标注工作台新折叠区「📥 整组导入正/负样本(目录即标签,批量入口)」**:目录输入 + **确认勾选门**(「我已核实并对该目录全部图片内容作出整组判定(导入即视为人工标注)」)+ clean/nsfw 双按钮;未填目录 →「请先填写目录路径」;未勾确认 →「请先勾选确认」(**零写入**,AppTest 断言锁定);成功 → 三路反馈(新增/升级/跳过)+ 侧栏进度即时更新;全跳过 →「该目录没有可导入内容(已标绝不覆盖)」。原「补充语料目录」更名「新图入 pending,待逐张标注」(两个入口职责分明:pending 逐张 / 整组已标)。
- **测试(+10 项全绿)**:`test_calibration.py` +6——新增入集(溯源/时间戳/note)/pending 升级(原线索保留)/已标不覆盖(人工资产原封)/二刷幂等全跳过/非法标签与目录缺失中文报错/CLI 子命令端到端(输出口径 + 落盘回读);`test_label_page.py` +4——包装函数(新增+进度反映/人工标签零触碰/非法标签)+ **AppTest 真驱动 UI 全流程**(确认门拦截零写入 → 勾选导入 2 张 nsfw 落盘溯源正确 → 重复导入全跳过条目数不变)。
- **真机验证**:8503 重启(runOnSave)渲染确认——导入折叠区/确认勾选框/双按钮全部在场(visible DOM 断言);交互正确性由 AppTest 确定性锁定(浏览器像素点击受 streamlit 重排干扰,不作为验证手段)。
- **红线保持**:整组导入 = 运营者对目录内容的**整组人工判定**(溯源独立可审计,note 留目录线索);已标资产绝不覆盖;页面零模型调用零外呼;标注集不入库。

## V10.36(2026-11-10)—— 标注工作台(webui 第 10 页):校准标注集搬进浏览器,SPRT 门禁人工地基开工

背景:V10.33 建成的误判率/校准测量体系以**人工标签**为唯一可信 ground truth(机器标签绝不冒充人标),但 `calibration label` 是终端 `input()` 交互——35+ 张真实证据图逐张看图打字效率低,门禁 C1(正/负样本各 ≥20)迟迟开不了工。运营者一声「开始标注」,本波把标注搬进浏览器并初始化正式标注集。

- **正式标注集初始化**:`data/calibration/labeled.jsonl`(此前 V10.33 只在临时目录演示)——`calibration init --dirs tests/fixtures/demo_site data/evidence`:40 唯一内容(sha 去重)= 5 种子 clean(合成图,内容可验证)+ 35 pending 真实证据图待人工。
- **新页面 `webui/label_page.py`(标注工作台,8503)**:①主区逐张标注——大图展示 + ✅clean / 🔞nsfw / ⏭跳过 / ⏮上一张,每次点击**整集原子落盘**(atomic.replace_with_retry,Windows 重试),溯源恒为 `human:webui`(与 CLI `human:console` 并列,报告按来源分层);②侧栏实时进度——已标/待标/正样本/负样本 + 门禁 C1 双进度条(正/负 ≥20)+ 差口中文提示 + 来源分层;③已标清单改标(人工纠错合法:覆写标签并更新溯源与时间戳,**不留假档案**);④补充语料目录(证据语料天然偏 nsfw,负样本不足时并入 clean 图目录,sha 去重、已标资产零触碰);⑤红线与用法说明(标注口径:按图片内容二值判定,模糊跳过宁缺毋滥)。
- **calibration 模块增补(公开 API)**:`add_pending_from_dirs(s, dirs)`——把目录新图并入**既有**标注集为 pending(与 `seed_from_dirs` 的区别:保留已标资产;sha 去重;目录不存在中文 ValueError);标注工作台的"补充语料"即此函数。
- **红线页面化**:页面只记录**运营者的点击**——绝不预填、绝不猜测、零模型调用、零外呼(score/report 仍按红线在 CLI 显式运行,页内只展示命令提示);标注集随 `data/` 整体不入库(内容含真实证据);缺 streamlit 时 main() 中文安装提示退出码 1(同既有页面契约)。
- **注册四清单**:render_smoke 页面清单(9→10 页,AppTest 真渲染零异常)、entry_check `_WEBUI_PAGES`(28→29 项探针全绿)、`start_services` 服务表(核心四→五服务,标注工作台 8503)、webui/README 页面表。
- **测试(+20 项全绿)**:`tests/test_label_page.py`——路径布局/装载(存在不重建、缺失按种子新建)/落盘格式与 CLI 互通(六键行结构+临时文件换名)/标注五态(人工溯源、改标更新溯源不留假档案、非法标签拒绝、未知 sha 返回 False、邻居零触碰)/进度与门禁(正负计数、来源分层、C1 差口、达标、自定义阈值)/补充语料(并入零触碰已标、sha 去重、目录缺失中文报错)/图片解析与 CLI 提示/端到端小流程(装载→标注→落盘→重载);`test_start_services` 端口断言 4→5(8765/8500/8501/8502/8503)。
- **真机验证(浏览器实测)**:streamlit 8503 启动 → 页面渲染零异常(标题/侧栏进度 5-35-0-5/门禁 C1 差口提示/第 1/35 张证据图/四按钮);**「跳过」交互闭环实测**(序号 1→2,盘上标注集零写入——skip 不产生标签的完整性验证);视口 1440×900 截图留痕。
- **运营者开工指引**:`python -m netsentinel.start_services` 一键常驻(或单独 `streamlit run webui/label_page.py`)→ 浏览器 127.0.0.1:8503 逐张点击标注;35 张真实证据图标完后看侧栏 C1 差口——**负样本大概率不足 20**(证据语料偏 nsfw),用「补充语料目录」并入你确认为正常内容的图片;正/负各 ≥20 后 CLI `calibration score → report` 出全量门禁判定(SPRT/级联维持默认关,门禁通过且运营者批准前不开)。

## V10.35(2026-11-10)—— P2-7/8/9 无人值守韧性:优雅退出排空 + 站级断点续跑 + 遥测落盘 + 告警有界重试 + 服务池档位化(性能重构收官波)

背景:V10.28–V10.33 完成提速六步后,提案(阶段 3/阶段 5 第 8 步)剩余的**无人值守韧性**缺口:Ctrl-C 在提交/聚合循环硬抛 KeyboardInterrupt(在飞站预算已扣、进度丢失,上百站批量中途崩溃后重跑=已完成站整站重抓);`telemetry.export_jsonl` 实现完好但**全仓零调用方**(指标随进程蒸发);notify webhook 未配置且**绝不重试**(告警单发即丢);REST 服务扫描池固定 2 路与三档并发体系脱节(mid 档批量池 10 路,服务池 2 路排队)。本波一次性收口,**P2 提案至此全部落地**(P2-6 SPRT 早停仍按既定硬门禁等待人工标注,见 V10.33)。

- **新模块 `netsentinel/ops/graceful.py`(两段式中断协调器)**:①首个 SIGINT/SIGTERM → 置位标志(中文 info"停接新目标,排空在飞任务后收尾");②再次信号 → 信号处理器内直接抛 `KeyboardInterrupt` 硬中断(**等待与否永远由操作者按几次决定**——"先礼后兵"标准形态);安装即记忆原处理器、退出原样交还;**非主线程注册 `ValueError` 捕获降级**(只保留标志位,REST/嵌入式宿主不受影响);`graceful_exit(install_signals=False)` 供测试/嵌入式拿到纯协调器。遥测 `graceful.signal` / `graceful.requested`。
- **`run_pool` 优雅排空接线(stop_check 注入口,缺省 None=逐字节旧行为)**:`_run_shard` 在**每个提交间隙**检查(先于 A236 重建检查;stop 优先于"每分片至少 1 个"进度保证——整批收尾时推进性不再有意义),置位即停接新目标;**已提交任务在聚合循环中全部排空,绝不半途弃单**;摘要新增 `interrupted: True` + `not_submitted: [未提交 URL]` 两键(**仅中断时出现**,正常批次摘要形状与旧版逐字节一致);记账恒等式扩为 `done + failed + skipped + len(not_submitted) == total`(形态不合法目标仍计 failed,不混入 not_submitted);A236 重建路径分片间+分片内双层检查,未提交尾部由 `_run_batch`/`_run_rebuilding` 统一登记(**分片只返回处理数,登记职责归调用方——修掉初版双层重复记账缺陷**);遥测 `pool.graceful.not_submitted`。
- **站级断点续跑(新模块 `netsentinel/ops/scan_journal.py` + run_flow 接线)**:每站结局落 SQLite 一行(主键 `(batch_id, url)` 幂等,重记覆盖绝不双行);库表/连接工厂(WAL+busy_timeout+check_same_thread=False)/遥测口径全部照抄 A113 `batch_state` 模式;**诚实边界**:只记 URL 与 `done/failed` 枚值(报告/证据有唯一事实源在队列与证据包);pool 摘要的 `skipped`(指纹未变)只有计数无 URL 清单——**不记 skipped**,续跑时这类站重扫一次由 site_memory 再次指纹跳过(判定侧幂等);`failed` 不进 `done_urls`(续跑默认重试失败站,运营直觉;`processed_urls` 供想连失败也跳过的调用方)。`run_flow` 批前过滤(resume_scan 剔除已完成站)/批后落账(results→done、errors→failed);**开库失败只中文告警降级**(fail-open,批量照跑不记断点);批次号 `scan-<秒级时间戳>-<进程内序号>`(防同秒碰撞);结果携带 `scan_batch_id` / `scan_journal_skipped`(仅 >0)/ `scan_interrupted`(仅中断),CLI 摘要直接打印可复制的 `--resume-scan` 续跑命令;CLI 新旗标 `--resume-scan 扫描批次号`(仅 `--input` 模式有效,误用中文报错码 2)。**实测价值**:崩溃续跑零重抓(慢站 40–80s/页 × 3 页/站的采集墙钟全免,vlm_cache 只免 VLM 调用不免抓取)。
- **遥测批末落盘(P2-7b,`telemetry_jsonl_path` 一等字段,空=不落盘逐字节旧行为)**:`run_pool` 批末(含优雅中断路径——finally 语义)`telemetry.export_jsonl` 追加一行快照(自动建目录;长跑自然形成时序台账);**只存名称与数字**(红线 17,断言覆盖);落盘失败只中文告警 + `telemetry.export.failed` 计数,绝不中断批次(观测性让位主流程);REST 服务 `create_app` 注册 FastAPI shutdown 钩子,服务关闭时同样落盘一次。`export_jsonl` 自 V5 实现以来**首次获得生产调用方**。
- **通知有界重试(P2-8,`notify_retry_max: 0` / `notify_retry_backoff_s: 5.0` 一等字段)**:缺省 0 = **单发不重试,V2 任务书红线缺省逐字节保持**;显式 >0 时失败后最多重发 N 次、指数退避(5s/10s/20s…),**只重发通知,绝不重发动作**(本模块只发通知;载荷字节级同一,断言锁定);任意一次成功立即停;终态计数(sent/fail)每次调用恰一次;`notify.retry` 重试计数 + `_sleep` 注入缝(测试零真实等待)。重构载荷构建一次、`Request` 每次新建(urllib Request 不可重用)。
- **服务扫描池档位化(P2-9)**:`scan_pool_size(cfg) = min(io_workers(档位), SCAN_POOL_CAP=10)`,mid 档 20 核 = 10 路(与批量池对齐,提案目标值);`SCAN_MAX_WORKERS=2` 保留为**回退缺省**(无 cfg / 档位解析失败 / concurrency 模块缺席),无参 `_get_scan_pool()` 与档位化前逐字节一致;`create_app` 开池时传入档位化大小,四端点(/scan、/scan-lead、/discover、/captcha/session)全部受益;上界 10 恒钳(服务与批量共用一台机的资源保护,红线 37 精神)。
- **配置收录(V10.35 段,独立于 V9–V14 冻结契约对账段)**:`graceful_exit_enabled: bool = True` / `telemetry_jsonl_path: str = ""` / `notify_retry_max: int = 0` / `notify_retry_backoff_s: float = 5.0` / `scan_journal_enabled: bool = True` / `scan_journal_db: str = ""`(空=<data_dir>/scan_journal.db);config.py 类型表(int/float/bool)与 example.yaml 同步。
- **batchflow 宿主接线**:`run_flow` 扫描段包 `graceful_exit()` 上下文(cfg.graceful_exit_enabled=False 时 `install_signals=False` 纯降级);**注入 scanner/pool_runner = 调用方接管并发语义**(与 V10.30/V10.31 注入哲学一致,既有测试零扰动),tier pool runner 闭包注入 `stop_check`;第二次信号硬中断由 CLI 捕获,中文提示"已完成站已记入扫描日志,可用 --resume-scan 续跑"退出码 0;优雅中断(单次信号)走正常返回路径,归组/入列/摘要/指引全部照常,摘要后打印 `⚠ 优雅退出` 行与续跑命令。
- **测试(+44 项全绿)**:`tests/test_graceful.py` 15 项——协调器五态(初始/首请/二请硬中断/主线程安装交还/非主线程降级)、信号处理器端到端、run_pool 六态(无 stop_check 摘要形状逐字节不变/开局即停零提交/首站排空不弃单/形态被拒目标恒等式/协调器全链/批末遥测落盘三态含失败不阻断);`tests/test_scan_journal.py` 17 项——幂等重记覆盖/批次隔离/done↔processed 口径/counts 三桶/同秒批次号唯一/重开持久/父目录自建/run_flow 记账+续跑剔除+failed 重试语义+开关回旧+interrupted 传播+CLI 误用;`tests/test_notify_hub.py` +7 项——缺省单发逐字节/二发成功退避序列/耗尽恰上界/载荷字节同一/零退避/负退避钳零;`tests/test_service_api.py` +5 项——无 cfg 回退/mid 与 high 档钳上界/无参单例稳定/非法档位回退(RecordingPool 替身随调用缝演进接受档位参数)。
- **端到端验证(`D:\3\verify_v10_35.py`,21/21 PASS)**:①优雅排空(3 站批,首站在飞停机 → 排空 1/未提交 2/恒等式/遥测);②断点续跑(a 完成崩溃 → 续跑零重抓只扫 b/c,批次号沿用,剔除计数如实);③通知重试(首败重试成功 + 缺省单发不重试两态);④服务池档位化(size=10/上界钳制/回退两态);⑤遥测批末落盘(恰一行、含本批计数、零内容字段)。
- **红线保持**:优雅退出只作用于**扫描侧**提交节奏,绝不触碰举报提交链路(人工门/频控 60s/日 5 原样);已提交任务的完成语义不变(排空≠弃单);对外礼貌间隔零触碰(红线 35——stop_check 只停"接新",不停"在飞");通知重试只重发通知绝不重发动作、缺省 0 保持 V2"宁可单发漏发不轰炸"红线;扫描日志只记 URL+枚值不存内容(红线 17);全自动化仍止步于"扫描-识别-证据-入列-告警",举报提交永远人工确认。

## V10.34(2026-10-15)—— yandex-html 搜索站点域名可显式指定:`yandex_html_base` 升格一等配置字段

背景:运营者要求把搜索站点域名**显式指定为 yandex.com**。`YandexHtmlEngine` 虽有三级端点解析(构造注入 → `cfg.yandex_html_base` 附加属性 → env `NETSENTINEL_YANDEX_HTML_BASE` → 内置默认 `https://yandex.com/search/`),但 `yandex_html_base` **不是** `Config` 字段——写进 config.yaml 会被当未知键告警忽略,实际只有 env 一条可用路径,配置文件里"指定"落空。

- **配置收录(附加属性升格,V11 起的标准做法)**:`contracts.py` Config 增 `yandex_html_base: str = ""`(一等字段,字符串走类型表默认分支无需另注册);空串 = 内置默认 `https://yandex.com/search/`,解析优先级与现状一致(字段值 → env → 内置默认),引擎零改动。
- **真实配置显式指定**:`config.yaml` 增 `yandex_html_base: "https://yandex.com/search/"`(搜索站点域名钉死 yandex.com,不再依赖代码默认值);`config.example.yaml` 同步文档化(默认留空)。
- **文档**:`docs/DISCOVERY.md` yandex-html 段"附加属性覆盖"措辞更新为一等配置键。
- **测试**:`tests/test_yandex_html.py` 增 load_config 全链用例(YAML 键 → cfg 字段 → 请求 URL)与空串回退内置默认用例;存量"端点覆盖"用例随字段升格自然仍绿。

## V10.33(2026-10-14)—— P2 第 5 步 标注集 + GLM 误判率/校准测量:把 SPRT/cascade 的开启条件变成可复算的数字门禁(误判控制地基)

背景:SPRT 的 α/β 担保以**校准概率**为前提(decision/sprt.py 校准假设原文)、cascade 不确定带与多图批处理同理——但 GLM 的 FP/FN 与校准度从未测过,一切"能不能开"只能靠感觉。本波把信任变成账本。

- **新模块 `netsentinel/vision/calibration.py`(测量与门禁一体)**:①**标注集**(JSONL,每条带溯源分级 `labeled_by`:human > site_verdict > seed——报告按来源分层,种子标签绝不冒充人标;跨目录同内容按 sha 去重);②**评分走生产链路**(`get_classifier→classify`,缓存与日预算一本账生效——测量即生产行为,不是旁路复制品;2026-10-13 实弹留下的真实 GLM 缓存分零外呼直接入账);③**度量**:Brier / ECE(10 桶可靠性曲线)/ 生产三阈值(0.5 复核、0.8 计数、0.9 NSFW)混淆矩阵 / FP-FN 明细 / 覆盖率;④**SPRT 数字门禁五判据**(C1 样本量 pos/neg≥20、C2 ECE≤0.10、C3 Brier≤0.20、C4 FN@0.9≤0.10、C5 FP@0.5≤0.10;无正样本判「不可测」而非通过;判据可配,可复算);⑤CLI 四子命令 `init/label/score/report`(交互式人工标注是唯一可信标签来源)。
- **种子标注集(诚实分级落地)**:`seed_from_dirs` 只对**内容可验证**的 mock 合成图标 clean(`seed:mock-synthetic`;文件名含 nsfw_hi_* 亦为纯色合成图——按内容标 clean 并在 note 留痕"真实 GLM 2026-10-13 判 0.02-0.03 佐证"),真实站图全部 pending 待人工;实测 fixtures+evidence 两目录 55 文件 → sha 去重后 **40 唯一内容(种子 5 clean + 35 pending)**。
- **真实 GLM 校准测量(零外呼,首批真数字)**:对种子集评分——**15→5 张全部命中 2026-10-13 实弹缓存(真实 GLM 分)**,种子层 **Brier=0.0005、ECE=0.0222**(clean 合成图上 GLM 校准极好:给 0.02-0.03 而标签为 0);门禁如实判「未过」(人标 0/20、正样本 0——**不冒充通过**,C1-C5 逐条列出缺什么)。
- **队列对账字段与 reconcile(阶段 3 误判控制④落地)**:`entries` 表新增 `machine_agg REAL DEFAULT -1.0`(入列时定格机器聚合分;旧库 PRAGMA 探测自动补列,老数据 -1)+ 只读对账 `reconcile_human_machine` 四口径(机器 nsfw 档被人确认 / suspect 档被人补判 / **误报候选 FP proxy**(人否决且机器 nsfw 或 agg≥0.9——最值得复盘)/ 识别失信条目单独计数不进 FP)+ CLI 子命令 `python -m netsentinel.decision.review_queue reconcile`;诚实边界留痕:**机器判 clean 不入列,本报告测不了 FN**——FN 由图片级标注集测量。
- **测试**:`tests/test_calibration.py` 13 项全绿——Brier 边界/ECE(确定性完美校准构造 ECE<1e-9、系统性过信≥0.85、可靠性桶)/三阈值混淆/标注集往返与校验(非法标签、重复)/种子生成(mock 内容标+nsfw_hi 事实注记+pending)/measure 分层与 FP-FN 明细与错误行/门禁全分支(样本不足、不可测≠通过、全过、单项不过、判据可配)/评分生产链路(单张失败记行不中断)/machine_agg 定格与**旧库迁移**/reconcile 四口径。
- **端到端验证**(`D:\3\verify_p2s5.py` 9/9 PASS):种子集生成 → 真 GLM 缓存分测量(Brier=0.0005/ECE=0.0222,门禁如实未过)→ stub 全管线冒烟(8 张模拟人标正样本,stub 故意错标 → FN=1.0 被度量引擎当场抓住——证明门禁会拦坏分类器)→ reconcile CLI 四口径;产物 labeled.jsonl / scores_glm.jsonl / report_glm.md / report_stub_smoke.md 留痕。
- **运营者下一步(一次人工投入)**:`calibration init` 已可复跑;执行 `calibration label` 人工标注 ≥35 张 pending(正样本 ≥20 是 FN 门禁的硬前提)→ 充值后 `calibration score --model glm:glm-5.3-flash` → `calibration report` 出全量真 GLM 门禁判定;**SPRT/cascade 维持默认关,门禁通过且运营者批准前不开**。

## V10.32(2026-10-14)—— P1-4 同站近重复拦下:phash 先于 VLM,重编码/缩放变体只送代表图(性能重构第五步)

背景:VLM 缓存键仅 sha256(字节级)——同一图的重压缩/缩放变体(网格站常态)逐张重复送审;phash 体系完整但此前只用于批末图谱。

- **前置实测锚点(设计依据)**:`tests/fixtures/demo_site` 五张夹具 phash 最小两两距离 **6**(阈值 4 下零误伤,存量 e2e 不受影响);`data/evidence` 45 张真实语料近重复对 **7 对(16%)**,其中 **mrdsk 站两张截图距离 2、两个站点 logo 距离 4——sha 完全抓不到的真近重复**,机制价值实证。
- **SendGate 第 4 层(近重复闸)**:拟送审图计算 64bit phash(`intel.phash` DCT 路径,实测 P50 1.27ms/张,比一次 VLM 便宜 ~3600×),与**已送审**指纹比对,汉明距 ≤ `vlm_neardup_threshold_bits`(缺省 4/64bit 业界保守线)拦下;**仅同站生效**(gate 实例即站点边界,跨站共享需缓存键升级,明确不做);fail-open 三重兜底(intel.phash 缺席/文件损坏/解码失败一律放行 + `sendgate.neardup.hash_failed` 计数);sha 字节去重优先于近重复(先字节后感知,计数分立);流式 `feed` 跨页生效(P1-3 流水线天然受益);汉明距内联计算不依赖惰性导入副作用。**风险如实登记**:近重复拦下是语义判断而非可证等价(≤4bit 变体视觉近乎相同,VLM 按内容打分分差预期≈0,但未在真实 VLM 分数上量化——账户欠费);保守阈值 + 一键回退 `vlm_neardup_dedup: false` + `skipped_neardup` 计数入 intel/审计留痕。
- **配置收录**:`vlm_neardup_dedup: bool = True`、`vlm_neardup_threshold_bits: int = 4`(一等 Config 字段 V10.32 段 + 类型表 + example)。
- **P1-3 遗留缺陷修复(全量回归实测抓出)**:早夭/中止路径 `chunk_pool.shutdown(wait=False)` 留下**在飞僵尸分块**——后台继续 VLM 外呼,调用跨越中止边界(烧预算 + 全量套件下复现为跨测试计数串扰 12≠9);改为 `shutdown(wait=True, cancel_futures=True)`:排队分块取消、在飞分块 **join 后**再传播异常(等待上界 = 单分块时长,确定性收敛)。
- **测试**:`tests/test_neardup.py` 10 项全绿——受控指纹层(阈值边界 ≤4 拦/>4 放、阈值 0 仅同指纹、fail-open、关闭零指纹调用、sha 优先、跨页流式、未测得图同样过闸、stats 字段)+ 真实图像层(PIL 生成 base/重编码/缩放变体 vs 无关图,前提自证距离)+ run_scan 默认链路跨页近重复(调用 2 次、`skipped_neardup=1` 入 intel)。
- **实测收益(A/B,`D:\3\verify_p14.py`,回环 3 页:p1 六底图 / p2 同图 JPEG q85 重编码变体 / p3 六新图,真实 GLM 延迟分布)**:送审 18 → 12(**−33%**),墙钟 17.3 → 12.2s;判定/needs_review 逐项一致,送审分集为子集;近重复记账=6;真实语料聚类报告留痕(45 张 7 对 16%)。断言 4/4 PASS。
- **红线保持**:仅作用于对自有 VLM 的送审决策;phash 计算在本地(图像数据不出本机);判定/提交/礼貌抓取零触碰。

## V10.31(2026-10-14)—— P1-3 站内爬取/推理流水线重叠:主线程逐页礼貌爬取 + 后台按页分块评分,VLM 等待藏进爬取窗口(性能重构第四步)

背景:capture 与 VLM 两阶段串行相加(orchestrator `_scan_once`);真实站 capture 160–240s 期间分类线程空转,快站则是 VLM 期间爬取线程空转。

- **采集引擎流式内核(重叠的前提,本轮最大发现)**:实测发现 `browser.capture_pages` / `capture_v2.capture_pages_v2` 返回 **list**(全部页采样完才返回)——任何"边爬边消费"在此结构上都不可能发生。两引擎重构为**流式内核 + list 包装**:`capture_pages_iter` / `capture_pages_v2_iter` 生成器逐页即时产出(会话复用/三级降级/页级容错/闸门语义逐项不变;提前关闭经 GeneratorExit 正常回收浏览器会话),旧 list 函数变为 `list(iter(...))` 物化包装——**list 语义与流式化之前逐字节一致**(browser/capture_v2 引擎测试全绿);`_default_capture_many` 改返迭代器(串行路径 `list(...)` 包装不变)。
- **新编排函数 `_pipelined_fetch_and_classify`**(orchestrator):主线程逐页迭代流式内核(页加载/滚动/图片下载/逐张礼貌间隔全部留在主线程,对目标站请求节奏与串行完全一致,红线 35);每页证据经**跨页共享** `SendGate`(P0-1 闸门新增有状态流式接口,`apply_send_gate` 变薄包装,一次性/分块喂入结果与记账完全一致)放行后提交为分块任务(单工作者 FIFO 池 → `_collect_member_scores` → P0-2 受界并发,成员环/V10.24 块级熔断原样)。**收割合并**:爬取结束后仍未起跑的排队分块取消并合并为单批回到同池(恢复整池吞吐,快站不因分块串行排队损失;FIFO 保证页序首现序不变)。
- **早夭止损(比串行更早)**:每提交一页后轮询已完成分块,分块抛系统性错误(V10.24 熔断)立即停止爬取并关闭采样生成器——串行路径要爬完所有页才轮到评分失败,流水线在**第 2 页爬完时**就能放弃第 3 页(P1-5 拦截跨站空烧,本波拦同站剩余页空烧)。
- **聚合熔断补口**:每页图片数 < 熔断阈值时块级永不触发;收割后按整站总量复判(全败且 ≥ 阈值 → 与串行同款中文 RuntimeError,消息单一事实源 `_classify_abort_runtimeerror` 供块级/聚合两处复用)。
- **接线与零扰动**:`scan_pipeline_overlap: bool = True`(一等 Config 字段)且**仅全默认路径**(未注入 capture/fetch_page/classifier)启用——注入任一组件 = 调用方接管,走原串行路径逐字节旧行为(service/batchflow/scheduler 真实路径均不注入 → 全部受益);"无可用成员"报错保留在两路各自 vlm span 内(V11 异常 span 契约测试原样通过);健康回报(P1-5)/闸门记账/intel/审计在流水线路径全部接续。
- **测试**:`tests/test_scan_pipeline.py` 9 项全绿——流式闸门等价 / 流水线⟷串行等价(判定+逐图分数+页序+调用数逐项一致,重叠计数 +3,墙钟收益断言)/ 顺序保持 / 早夭止损(生产者只拉 2/3 页)/ 聚合熔断 / 跨块 sha 去重 / 跨块改名映射 / 开关与注入回串行;browser/capture_v2 引擎族、orchestrator×4、e2e(CLI 子进程真默认链路)、send_gate 56 项、preflight/bounded_parallel 全绿。
- **实测收益(A/B,`D:\3\verify_p13.py`,回环 3 页 × 6 互异真实证据图,真实 GLM 延迟分布模拟)**:**快站**(0.6s/页,VLM 主导)整扫 22.7 → 19.8s(**1.15×**);**慢站**(5s/页,爬取主导,真实站 40–80s/页同构)整扫 50.1 → 40.1s(**1.25×**,VLM 14.4s 完全藏进爬取窗);两态判定/逐图分数/页序逐项一致,断言 6/6 PASS。
- **红线保持**:对目标站点的请求节奏(页序、礼貌间隔、robots、降级链)与串行路径完全一致——重叠的只有对自有 VLM 提供方的等待;判定/提交/频控零触碰。

## V10.30(2026-10-14)—— P1-5 VLM 扫描预检:三道免费闸门把系统性故障拦在爬取之前(性能重构第三步;10/13 空烧事故的爬前补闸)

背景:2026-10-13 实弹事故中 GLM 余额耗尽(HTTP 429 code 1113)后系统仍完整爬完 2 个真实站(慢站采样 40–80s/页 × 3 页 ≈ 131–240s/站),57 次 429 盲打后才由 V10.24 熔断收场——V10.24 的闸在**爬完之后**,本波补上**爬取之前**。

- **计费标记(计量检测的地基)**:`NsfwClassifier` 新增类属性 `consumes_vlm_budget`(缺省 False;本地内核 stub/nudenet/clip/guard 零改动);四家远程 VLM 分类器声明 True(UniversalVLMClassifier / GlmVlmClassifier / CascadeClassifier / FailoverClassifier)。预检据此判断"该不该检"——纯本地扫描零打扰(连 vlm_cache.db 都不打开)。
- **新模块 `netsentinel/vision/preflight.py`(三道免费闸门,全部零外呼)**:①**计量检测**——按 `_collect_member_scores` 同口径解析成员(构造失败不抢答,主体自会报错;同配置进程内记忆),无计量成员直接放行;②**健康账本**——进程内线程安全账本记录每个计量成员的最近扫描结局(评分阶段抛异常=V10.24 全败中止/无可用成员 → 记 dead;完整返回=提供方仍活着 → 记 ok 并清除 dead;零送审站不扰动既有状态),预检时成员 dead 且未过 `vlm_health_ttl_s`(缺省 300s,0=立即过期)→ 中文报错跳过本站,**提示人工 `vlmctl ping` 诊断——预检绝不自动 ping(红线 20:外呼诊断只能人工显式发起)**;③**本地预算闸**——读 VlmCache 当日一本账,余量 <1 → 跳过(计数 `scan.skipped.no_budget`,文案注明纯缓存重扫可临时关闸);余量 < 计量成员数×5 仅警告不拦截(部分耗尽属正常运营,V10.24 诚信升级已兜底)。跳过计数 `scan.skipped.provider_dead`、gauge `scan.budget.remaining`。
- **编排接线(orchestrator `_scan_once`)**:顶部(`classifier is None` 默认解析路径才预检;**注入 classifier = 调用方接管识别链路,预检整体跳过——全部注入替身的既有测试零扰动**;service/batchflow/scheduler 真实路径均不注入 → 全部受保护);评分阶段 try/except 收尾回报健康(Exception 记 dead 后原样重抛;KeyboardInterrupt 等非 Exception 不记——人为中断不是提供方的错);成功且 send_imgs 非空记 ok。
- **配置收录(一等 Config 字段,V10.30 段)**:`vlm_budget_precheck: bool = True`(False=逐字节旧行为)、`vlm_health_ttl_s: int = 300`;config.py 类型表 + example.yaml 同步。
- **语义边界(如实登记)**:本地预算账与**提供方侧**余额是两本账——提供方余额耗尽在本地账不可见,由健康账本(上一站死亡)与 V10.24 熔断(本站全败)接力拦截,**首站冷启动仍需人工 ping 诊断**(预检不发明新外呼);账本进程内不跨进程共享。
- **测试**:`tests/test_preflight.py` 16 项全绿——四家+基类计费标记/成员解析(记忆、构造失败不抢答)/**预算耗尽爬取前拦截(capture 间谍零调用 + 零评分调用)**/本地扫描零预算照常/注入绕过/开关关闭回旧/**健康账本三态(dead 拦截-TTL 过期-ok 清除)**/零送审不扰动/低预算警告不拦截/run_scan 集成。
- **实测收益(事故复刻 A/B,`D:\3\verify_p15.py`,回环 3 站 × 6 互异图,替身模拟 429 带内失败 0.29s/次)**:预检关(旧行为)3 站全部完整爬取后中止(爬取页面 6 次双取/下载 36 图/18 次盲打);预检开第 1 站冷启动中止、第 2/3 站**毫秒级跳过**(2 页/12 图/6 次盲打)——**盲打 −67%,按 10/13 慢站实测投影省 ≈6.2 分钟爬取**;预算耗尽场景第 1 站即拦(零爬取零评分)。断言 4/4 PASS。
- **红线保持**:预检零外呼、绝不自动 ping(红线 20);判定/提交/礼貌抓取零触碰;跳过语义=整站以中文 RuntimeError 收场,批量池按站隔离不拖垮整批(与 V10.24 同款)。

## V10.29(2026-10-14)—— P0-2 受界并发送审:逐图串行外呼改有界线程池,判定不变(性能重构第二步)

背景:站内 VLM 串行推理是全链路唯一最大瓶颈(真实 GLM 单图 P50 4.91s、站内占比 71.6%、并发度恒 1);A166 `vision/parallel_classify` 机器 27 个版本前已备好但从未接线。

- **新模块 `netsentinel/vision/bounded_parallel.py`(资格检测 + 受界池)**:`classify_bounded` 按序短路决策——①`vlm_parallel_workers`<2 → 原串行;②批量<2 张 → 原串行;③**分类器覆写了 `classify_batch`**(`type(inst).classify_batch is not NsfwClassifier.classify_batch`,如 failover 链/nudenet/clip/guard 原生批量)→ 原串行(尊重其批量语义);④其余 → 注入**调用方持有的受界线程池**(`min(workers, len(imgs))`,with 退出即关不留孤儿,红线 37)交给 A166 `classify_parallel` 保序执行(注入 executor 契约:不关闭、单图异常容错为 error 分)。**注入替身全部自带 classify_batch → 既有测试自动落串行路径,零扰动**。
- **编排接线(orchestrator `_collect_member_scores`)**:成员批量经 `classify_bounded` 调度;带内 error 分语义两路径一致,均由 V10.24 收货口记账(全败 ≥5 快速中止 / 部分失败诚信升级)。**异常语义分歧如实登记**:串行默认循环单图上抛类异常(如 VlmBudgetExceeded)炸整批→成员跳过→"没有可用成员";并行按 A166 容错为 error 分→命中 **V10.24 快速中止**(中文 RuntimeError 附余额/密钥/网络根因提示 + `vlmctl ping` 指引)——两路都以中止收场且**并行的报错更可诊断**,绝无静默 clean。
- **线程安全审计(并发放行前提,全部核实)**:VlmCache 全操作含 `spend_one` 预算"查+增"在 `self._lock` 内 + 连接经 storage.kernel 工厂 `check_same_thread=False`——跨线程安全、**预算扣减原子**(20 线程竞争 limit=5 实测恰放行 5 次,绝不超支,+1 测试);vlm_client 断路器 `_BREAKERS` 全程持锁;A74 限速桶建桶持锁;multi_provider 惰性定格为幂等良性竞态。
- **配置收录(附加属性升格一等 Config 字段,默认值精确保持可回退)**:`vlm_parallel_workers: int = 0`(0=自动档 `min(档位 io_workers, 6)`;1=强制串行逐字节回旧;2~16 显式,>16 钳制)、`provider_rpm: int = 0`(A74 每提供方令牌桶礼貌护栏,0=不限;并发下建议按提供方配额设置);config.py `_INT_FIELDS` 同步登记;config.example.yaml 补注释示例。新段 `# ---- V10.29(P0-2)` 独立于 V9–V14 契约对账段,冻结文档零触碰。
- **测试**:`tests/test_bounded_parallel.py` 14 项全绿——workers 解析四态/覆写者与协议替身走串行(并行计数零增量)/单张不建池/串行旋钮生效/**保序且逐项等于串行结果**/**并发峰值恰为 cap**/单图异常 in-band 容错/空与单张输入/**VlmCache 预算原子性(20 线程竞争 limit=5 恰 5 过 15 拒)**/run_scan 集成(整扫内并行计数+1、判定与串行逐字段一致)。
- **实测收益(A/B,`D:\3\verify_p02.py`,回环站 + 真实 GLM 延迟分布,P0-1 闸门两态全开隔离纯并发收益)**:整扫 **34.2s → 13.0s(2.6×)**;VLM 阶段 30.1s(串行累计)→ 墙钟内 ~9.1s 完成(≈3.3×);**判定与逐图分数完全一致**(verdict/agg/needs_review/逐图 sha+prob 同序同值);调用数两态一致 7(并发不改变送审量);并发峰值恰 6(受界验证);串行态峰值 1。
- **红线保持**:只加速对自有 VLM 提供方的 IO 等待;目标站点礼貌抓取(fetch_delay_s)/举报频控/人工门零触碰;对目标站的站内图片拉取并发**明确不做**(V10.28 提案 B 节红线);礼貌护栏 `provider_rpm` 可选配。

## V10.28(2026-10-14)—— P0-1 送审闸门:判定口径前置过滤 + 送审级去重(零判定影响;性能重构第一步)

背景:全链路实测(2026-10-13 实弹日志 + 受控基准)坐实两层纯浪费外呼——mrdsk 真实站 30 张送审、ensemble 按 path 归并仅 17 张(13 次重复调用;识别失败不进缓存,余额耗尽时重复图放大盲打,实测 57 次 HTTP 429);受控基准 19 次送审仅 9 个唯一文件、其中 22×19 / 128×128 两张小图从不参与判定(verdict.assess 只扫"宽或高 ≥ min_image_px"候选)。

- **新模块 `netsentinel/vision/send_gate.py`(三层闸门,全部可证零判定影响)**:①判定口径镜像——非候选图(两边均 < min_image_px)不送审;**部分未测得(如 0×500)按候选处理**(h 已达标,assess 今日就采信,闸门一律放行);②送审级去重——同内容(sha256,缺失回退 path)只送首次;③未测得且非候选(如 0×0)缺省 **fail-open 放行**(绝不因测不出尺寸丢证据),`vlm_send_gate_drop_unmeasured`(附加属性,缺省 False)显式开启才拦。**机器对账**:vision 层不得 import decision(import-linter 分层),口径一致性由 `tests/test_send_gate.py` 的 (宽,高,min_px) 全网格对账锁定——"闸门放行 ⟺ assess 候选",任何人单侧改动即测试失败。
- **编排接线(orchestrator `_scan_once`)**:scan.vlm span 内、`_collect_member_scores` 前施加闸门;`vlm_send_gate` 附加属性缺省 True(置 False 逐字节回旧行为)。记账三路:遥测计数(`sendgate.passed/skipped.small/skipped.duplicate/skipped.unmeasured/kept.unmeasured`)+ gauge `scan.images_sent` + `report.intel["sendgate"]`(只在**确有拦截**时写入,零拦截主线 intel 键集与审计载荷逐字节不变;写在融合块之后——fusion.fuse 会整体替换 report.intel);审计事件同条件附 `images_sent`。
- **嗅探升级(修漏报洞)**:`_sniff_image_size` 补 WebP(VP8X/VP8/VP8L 三位流)与 AVIF/HEIF(ftyp 品牌核验 + ISOBMFF 盒子树 meta→iprp→ipco→ispe,层级/数量/体积三重守卫)——此前这两类图宽高恒 (0,0),**从不作为候选参与判定**(真实网格站常用 webp,系统性无视真实证据);补齐后恢复完整判定参与权(方向只会更严:多出的真实证据只可能抬升 agg,needs_review 只升不降)。嗅探失败一律 (0,0) 由闸门 fail-open 兜底。
- **V10.24 失败记账语义增强**:被闸门拦下的图不进入 `_collect_member_scores`,天然不计"识别失败"(预期跳过 ≠ 识别没做成);全小图站点零送审调用、判 CLEAN 与旧行为一致(省全部外呼)。
- **测试**:`tests/test_send_gate.py` 56 项全绿(网格对账 32 参数化 / 三层拦截单元 / run_scan 集成开关两侧 / 全小图零调用 / WebP·AVIF·HEIF 嗅探含畸形回归);受影响存量族(orchestrator×4 / verdict / kernel_wire / e2e_stub)全绿。
- **实测收益(A/B 对照,D:\3\verify_p01.py,回环站 + 真实 GLM 延迟模拟 3.47–5.58s,素材=data/evidence 真实证据图)**:VLM 调用 19 → 7(**−63%**:拦下小图 2 + 重复 10);VLM 阶段墙钟 86.5 → 32.1s(2.7×);整扫 91.2 → 36.3s;**判定逐项一致**(verdict/agg/needs_review 相等,送审分集为子集)。预算吞吐换算:200 次/日预算下,站点吞吐同比例放大。
- **红线保持**:判定公式/阈值零改动;人工门/提交频控/礼貌抓取零涉及;fail-open 保证任何嗅探失败不丢证据;全自动化仍止步于"扫描-识别-证据-入列",举报提交永远人工确认。

## V10.27(2026-10-14)—— 后台实弹续跑:服务保活 / 引擎配置纠正 / 无头指纹结论 / 线索恢复(负责人直研)

- **服务保活脚本 `scripts/start_services.py`**:净网助手形态的骨架(REST 8765 + 助手页 8500 + 复核台 8501 + 关键词页 8502,`--with-demo` 连 8899)跨会话会整体掉线(实测)——一条命令缺谁补谁(端口探测已在线跳过、Windows CREATE_NO_WINDOW 无窗口、就绪等待 20s),`status` 只读体检;USAGE 入口表新增行;+4 测试(端口探测双态/服务表无冲突/status 只读)。
- **配置纠正**:`config.yaml` 的 `discovery_engine` 仍是 V10.22 写的 `yandex`(XML API,建于内置内核之前)——后台 /discover 首跑因此报"XML 凭据缺失";已改 `yandex-html` 并热更 REST。
- **无头指纹结论(重要诚实登记)**:验证 cookie **不能**让无头后台免验证——运营者经显式验证窗口(POST /captcha/session,12 分钟后 rc=0 人工完成)拿到有效会话后,随后台无头 /discover 仍被验证码拦。定性:Yandex SmartCaptcha 评分绑定行为+环境指纹(有头/无头是不同指纹),cookie 单独不携带豁免。**净网助手形态下的发现实践定式:后台无头跑,被拦→页面出验证卡→显式开窗人工完成一次→有头子进程当场完成发现**;无头全自动发现需官方 XML API 凭据(不受指纹评分)。
- **线索恢复**:今日 0 结果轮覆盖了 leads.txt——从 V10.23 成功轮记录恢复 10 条真实线索(文件头注明恢复来源);`GET /leads` 实测 10 条。注:验证完成那轮有头发现 rc=0 但 SERP 当时 0 自然结果(查询词当刻返回状态,如实登记)。
- **验证**:entry_check **28/28**(净网助手页已注册);全量回归 exit 0(+4 测试);五服务全在线。

## V10.26(2026-10-13)—— 净网助手统一工作台:所有操作后台化,单页承载全流程(2 席位并行 + 负责人收口)

需求:所有操作在后台进行,用户只看见"净网助手"页面。

- **席位A · REST 服务端后台化(service/app.py + 23 测试)**:新增五端点——`POST /discover`(后台线程执行发现,worker 内**强制无头**(HEADED 环境临时摘除+事后恢复)且 NETSENTINEL_DATA_DIR 绝对化对齐;缺省读 active.txt;整轮 errors 也算 done 四键 result;验证码失败以中文进 failed)、`GET /leads`(只读解析 leads.txt,行号/mtime)、`POST /scan-lead`(与 /scan 同机制,受理核心抽 `_submit_scan_job` 共享,既有语义逐字节不变)、`POST /captcha/session`(**用户显式点击才触发**的一次性可见验证窗口:subprocess 运行发现 CLI,env 显式注入 HEADED=1 不污染父进程,30 分钟超时)、`GET /assistant/status`(GLM 密钥/Yandex 会话/关键词数/队列四计数/三配置开关,全本地只读**零外呼**)。API.md 新增 §3.9 五端点与安全边界第三条。
- **席位B · 净网助手页面(webui 第 9 页 assistant_page.py,780 行 + 39 测试)**:一站式控制台七区块——状态卡(打开即拉 /assistant/status)/关键词区(复用关键词工作台纯逻辑落盘)/发现区([开始发现]→后台 job→手动轮询→完成自动拉线索)/线索区(域名短显+逐条[扫描]按钮+批量进度)/验证会话卡(仅在需要时出现;[打开验证窗口]是全页**唯一弹窗操作**,且仅点击才发生)/复核区(队列计数+跳转复核台)/红线页脚。纯逻辑层 7 函数(urllib REST 封装含服务未启动中文提示);页面零外呼、零新依赖;双段铁律保持。
- **负责人收口**:assistant_page 注册进渲染冒烟清单(AppTest 真渲染 10/10 全绿)与 entry_check 页面清单;REST 热更新重启;净网助手页 8500 上线(浏览器实测:状态卡七项全绿真实拉取、各区块渲染完整)。
- **红线**:人工门不变(页面无任何提交入口);验证码仍只等真人(后台无头遇验证码→failed→页面提示显式开窗);页面常规浏览零外呼(状态全本地)。
- **验证**:全量 exit 0(席位各自跑+收口后 6856+ 全绿);`-k assistant` 70 过 2 跳;渲染冒烟 10/10;净网助手页 HTTP 200 + REST 五端点实测。

## V10.25(2026-10-13)—— 内置 Yandex 浏览器:同厂内核接入 + 原生 UA + 会话持久化(负责人直研)

- **安装与接入**:winget 静默安装官方 Yandex Browser(26.10.0.1796)→ 引擎浏览器通道优先级升级为 **Yandex Browser → Chrome → Edge → 自带 Chromium**;`detect_yandex_browser()` 自动探测(env 显式指定 > Windows 标准三路径),`NETSENTINEL_YANDEX_BROWSER_PATH` 可覆盖;Playwright 经 executable_path 驱动实测通过。
- **原生 UA 修正(自伤点修复)**:V10.23 起浏览器通道曾把 UA 覆盖为 `NetSentinel/0.17`——真浏览器配机器人 UA 等于自报家门;现浏览器通道一律用浏览器自身 UA(HTTP 通道保持 NetSentinel 公开身份不变)。
- **会话持久化**:`launch_persistent_context` + `<data>/browser_profiles/yandex_html`(缺省开启,随 data/ 不入库;`NETSENTINEL_YANDEX_HTML_PROFILE=0` 关闭)——人工验证通过后的 cookie 跨查询/跨运行保留,Yandex 对已验证会话显著宽容。
- **人工门增强**:等待时长 env 可配(`NETSENTINEL_YANDEX_HTML_CAPTCHA_WAIT_S`,默认提至 300s);超时报错文案动态秒数。红线 1 不变:验证码只等真人绝不代填。
- **测试**:新增 3 项(检测 env 覆盖与盲信拒绝/通道优先级序/会话目录缺省与开关,隔离本机真实安装);engine 套件 16 项 + discovery 全绿;全仓回归 exit 0。
- **实弹诚实记录**:① 缓存纠偏——V10.23 人工门通过后结果入了 discovery 缓存(TTL 168h),本波最初两次"零验证码成功"实为**缓存命中**(未发真实请求,已清缓存重验);② 清缓存后真实请求:Yandex 浏览器通道机械链路全通(启动/渲染/验证码检测/人工门等待/profile 落盘),但本机 IP 仍被 Yandex 验证码标记,两轮 180s/300s 人工门**均无人完成**(窗口弹出时运营者未在桌面旁)——超时诚实报错;③ 结论:能力就绪,等待运营者在场完成一次验证后 cookie 持久化即接管后续查询。每轮触发都会加重 IP 标记,已停止重试。

## V10.24(2026-10-13)—— 创世纪遗留清偿波:CHANGELOG 现存遗留项全部清零(3 席位并行 + 负责人收口)

波前快照 `D:\3\netsentinel-backup-pre-v1024-20261013.tar.gz`;基线全量 exit 0;清偿 V10.23①②③ + V10.21② 登记项 + import-linter 声明。

- **席位1 · 识别失败诚信升级 + 余额耗尽熔断**(V10.23①③,V10.23 实弹 57 次盲打事故的正解):
  - **失败感知**:查明现有信号盲区——`scores["error"]` 带内 0 分降级(failover A63/GLM 降级语义)与"入批数−返回数"静默缺评(事故 27→13 的 14 张)在 ensemble 出口均不可反推,必须在 orchestrator 成员收货口对数;双口径合计 `dropped + errored` 为站点级失败数。
  - **诚信升级**(默认开,`escalate_on_classify_error`):失败>0 → 强制 `needs_review=True`(verdict/agg 原档不动,"只升不降"同哲学)+ 入列 note `[识别失败 N 张,判定置信受损]` + clean 时 CLI/日志醒目中文警示 + `scan.classify.failed_images` 计数;零失败主线逐字节不变。
  - **快速中止**(`classify_abort_threshold` 缺省 5,与诚信开关独立):首笔成员批量全败且 ≥ 阈值 → 中文 RuntimeError(计数+余额/密钥/网络根因提示+vlmctl ping 指引),不再对剩余图片盲打;部分失败转诚信升级。+16 测试(含两种事故形态/阈值边界/首笔判定/整批异常旧语义保持)。
- **席位2 · capture goto 三级降级**(V10.23②):根因——重试在但慢站重试仍超时,且"退化 urllib"只覆盖 ImportError 不覆盖运行时超时。新增 `_shoot_with_goto_fallback`(v1/v2 共用):① 瞬态重试(既有)→ ② 超时族整页退回 urllib 退化采样(截图缺席如实声明,`capture.degraded_pages` 计数)→ ③ 双败记页级错误**continue 其余页**(页级失败绝不杀整扫;单页路径维持 fail-fast);非超时异常不进降级链(安全闸门语义原样)。+10 测试(含 V10.23 事故场景复刻:批量双败 scan 不崩样本 1/2)。
- **席位3 · 登记项收编**:timesource 三处 holdout 清零(telemetry_trace 毫秒 ISO/notify 墙钟/vlm_cache 秒级 ISO + `_is_expired` 双态式等价收编,守门白名单清空、迁移登记 17 文件);service/app.py 只读直连收敛到 `storage.kernel.connect(readonly=True)`(回落路径保留);BUSY_TIMEOUT_MS 三处同源再导出(review_queue/batch_state/kernel 同一对象);**pyproject import-linter 声明 timesource**(主干分层插入 + 零内部依赖双契约,与 AST 守门双保险)。
- **验证**:全仓 **6840 收集,6830 passed + 10 skipped,exit 0**(净增 26 测试);`entry_check.py` **27/27 全绿 19.6s**;LIVE 服务热更新重启全健康。
- **遗留状态:CHANGELOG 现存全部遗留项清零**。新登记(观察项,非缺陷):① `storage/__init__` 懒加载 `__getattr__` 的 fromlist 递归理论边缘(全仓无触发用法,未动);② GLM client 退避层的批内重试中断(编排层已整批间熔断兜底);③ git 提交仍待运营者决策(V10.11 起 13 个版本)。

## V10.23(2026-10-13)—— 内置 Yandex 网页内核 + 人工验证码门(负责人直研;真实任务实弹)

- **新增 `netsentinel/discovery/yandex_html.py`**:内置 Yandex 网页检索引擎(`--engine yandex-html`),不依赖官方付费 XML API——直接读取 `yandex.com/search` 结果页并解析自然结果链接(纯标准库 html.parser,零新依赖;clck/jsredir 跳转包装解码、自域剔除、去重保序、canonical 归并由流水线统一)。
- **双通道传输**:`--transport browser/http`(缺省 auto:Playwright 在席即浏览器,缺席退纯 HTTP);浏览器通道内核优先级 系统 Chrome→Edge→自带 Chromium;HTTP 通道仅 urllib+桌面 UA。端点可经 `yandex_html_base` 附加属性覆盖;非法通道值构造即拒。
- **红线 1 双形态落实**:① 检测到验证码/反爬页(URL 特征或 页面特征+自然结果≤3)一律**中文报错即停**——绝不识别/解析/绕过;② **人工验证码门**(V10.23 核心创新,有头模式 `NETSENTINEL_YANDEX_HTML_HEADED=1`):遇验证码时浏览器窗口保持可见,`wait_human_captcha` 轮询等待**运营者亲手完成**(最长 180s,超时如实报错)——机器只等待绝不代填,这是"验证码永远人工"红线的最合规工程形态。
- **实弹战况**(真实任务「每日大赛」):HTTP/无头 Chromium/无头 Edge 均被 Yandex 验证码拦截(引擎全部正确即停)→ **有头+人工门实弹打通:运营者窗口内完成验证后,1 次查询产出 10 条真实线索** → leads.txt(bulk_intake 兼容)。真实扫描 2 条线索走通全链(礼貌上限生效);**诚实登记**:GLM 密钥中途余额耗尽(HTTP 429 "余额不足或无可用资源包"共 57 次),后续图片评分失败被跳过,相关 clean 判定**不可信**——待充值后重扫;config 已按实跑工况补 `fetch_timeout_s: 30`(慢站)与 `capture_engine: v2`(懒加载网格站)。
- **CLI/文档**:`--transport` 参数接入(cfg 附加属性注入);DISCOVERY.md 备选引擎节、USAGE 入口表同步。
- **测试**:新增 tests/test_yandex_html.py 13 项全绿(SERP 解析/跳转解码/验证码即停不误伤/门禁/传输注入/注册表别名/discover 接线含 canonical 去重口径/人工门三态);全仓回归 exit 0。
- **下波候选(实跑发现)**:① 主分类器图片评分失败当前按跳过处理——部分失败 + 零分集合会产出"看似 clean"的判定,应升 needs_review 或在报告中显式标注"识别失败 N 张";② capture goto 超时的优雅降级(重试/退回 fetcher 路径);③ GLM 余额耗尽的预检提示(vlmctl doctor 增强)。

## V10.22(2026-10-13)—— 真实任务上线波:GLM 5.3 Flash 实弹接入(负责人直研;按 GO_LIVE 六步)

- **密钥安全落盘**:运营者 GLM 密钥经 A152 受纳关口 `security.keys.set_key` 写入 `~/.netsentinel/keys/glm`(icacls 仅当前用户);掩码回读 `298c****` 确认;密钥不进 config.yaml/git/日志。
- **关键词**:「每日大赛」经关键词工作台纯逻辑落 `data/keywords/active.txt`(1 词);展开预览/命令提示联动正常。
- **真实接入配置**:新建 `config.yaml`(.gitignore 防护)——`classifier: glm:glm-5.3-flash`、`vlm_online: true`、`allow_network: true`、`discovery_online: true`、`discovery_engine: yandex`、礼貌上限(max_pages 3/max_images 10/fetch_delay 1.5s/discovery_query_delay 3s/单查询 10/总 30);提交红线原样(human_gate/dry_run/频控)。
- **GLM 真实链路双验证**:①`vlmctl ping glm:glm-5.3-flash` 真实外呼成功(模型回显正确,1x1 探针 0.02 带中文理由,延迟 8.2s,预算记账生效);②本地 mock 站真 GLM 全链扫描——5 张图全部经真实 API 评分,接管触发(云端 glm 接管),判定 clean(agg=0.031)。**诚实注记**:mock 纯色图在桩模式下按文件名得 0.97,真模型判 0.03——证明视觉链路已实弹化且不认文件名只看内容。
- **Yandex 发现**:实跑确认凭据门槛按设计生效(中文拒绝:需 yandex_xml_user/key 或 NETSENTINEL_YANDEX_USER/KEY 环境变量,官方 XML API 控制台签发);通用搜索引擎替代检索无有效结果(过滤口径),**不编造线索**——真实发现待运营者补两凭据后一条命令即通(命令已在关键词页/本文档留痕)。
- **红线保持**:全程零自动提交、人工门原样;外呼仅在运营者显式指令下发生(本次为负责人指令接入)。

## V10.21(2026-10-13)—— 遗留清偿收官波:V10.17–V10.20 全部遗留项一次清零(4 席位并行 + 负责人收口;仿 V10.9/V10.10 清偿波传统)

波前快照 `D:\3\netsentinel-backup-pre-v1021-20261013.tar.gz`;基线全量 exit 0;四席位严格文件互斥。

- **席位1 · NETSENTINEL_DATA_DIR 全链路接入**(V10.20 最大遗留):load_config() 内实现 env 覆盖——优先级 **env > config.yaml > 字段默认**(与 webui apply_data_dir 现状同口径),公共纯函数 apply_data_dir 落 config.py(含 5 个派生字段整体迁移:evidence_dir/db_path/audit_path/log_path/vlm_cache_db;phash/graph/discovery_cache 不迁与 webui 现状对齐),contracts.py 零改动(data_dir 默认 "data" 冻结)。空白 env 视为未设,缺省行为逐字节不变;11 处 getattr 兜底点全部自动受益。**定义性验收**:`NETSENTINEL_DATA_DIR=<tmp> scan` 全产物落 tmp,真实 data/ 文件清单+大小+mtime **逐字节一致**(前后双 checkpoint diff 为空);queue list 读隔离库。+18 测试;DEPLOY.md 升级为"全局可用"。
- **席位2 · 时间源单源化**(V10.18 审计 Top1):新建 L0 基座 `netsentinel/timesource.py`(utc_now/now_local/now_iso/now_naive_local/fromtimestamp_local,零内部依赖);迁移 13 处"可安全迁移"点(evidence×3/report×2/browser/executors×2/finishflow×2/modelmgr/telemetry/cost_meter)——每处**逐字符等价**(冻结时钟 fixture 下新旧两式输出对照硬证+真实时钟三式同秒对照);**AST 守门测试**:aware 惯用式只许出现在 timesource+显式白名单,双向对账(活体注入违规验证过会红);豁免登记(contracts.py 冻结/rate_limit/adaptive naive 契约)。顺带清偿 browser.py 7 处资源关闭 debug 日志(S5"降级可见性")。
- **席位3 · SQLite 连接工厂机制化**(V10.18 审计 Top2):storage/kernel.py 新增公开 `connect(path, *, readonly=False)`——全仓唯一许可的 sqlite3.connect 入口(WAL+busy_timeout=5000;readonly 用 URI mode=ro 且缺文件绝不误建空库;:memory: 跳 WAL;WAL 失败先关连接再抛防 Windows 句柄泄漏堵重建路径);**16 处直连全部迁移**(decision×3/intel×7/submit/sessionstore/vlm_cache/discovery/replay-cli/kernel 本体,依赖单向无环);**AST 反向扫描护栏**:裸 sqlite3.connect 只许在 kernel.py+空白名单,新违规即红(当前零违规);+14 测试。协同:kernel/graph/site_memory 时间源迁移并更新席位2 守门白名单。/trace 分页遗留正式关闭(单任务 span 树 MAX_SPANS_PER_TRACE=4096 天然有界,API.md 注记)。
- **席位4 · 契约勘误 + 未通电内核收口**(V10.17 遗留①②④):**arbiter 通电**(orchestrator ensemble 后判定前,`getattr(cfg,"arbiter_enabled",False)` 默认关,分歧 ≥0.35 才仲裁、缺省连内核都不导入;门控双侧计数;异常优雅降级;+9 测试含 SUSPECT→CLEAN 翻转锁定替换真实生效);**CONTRACTS-V18.md 勘误契约**(主契约 8→13 键/12→18 步逐项列名+出处、V10.16 自动上传追认、测试路径更正;**红线 38/39 重号正式消歧为 38a/38b、39a/39b**;V10.17–V10.21 附加属性 12 项一览+行为追认;四席位文件互斥表);REDLINES.md 消歧落地+确认 V10.19 后无新增编号红线;MULTI_PROVIDER.md 补 failover 断路器语义节+prompt_dialects 定位声明。
- **负责人收口**:**failover 惰性接线一行根治**(席位4 实测断点:classifier_base._LAZY_IMPORT_MODULES 补 "failover" 映射——配置 `classifier: failover` 从"被 warning 跳过"变为直达构造;干净子进程回归测试锁定);entry_check.py 沙箱 env 注入对齐 `<沙箱>/data`(与隔离 config 布局一致);MULTI_PROVIDER 临时导入姿势注记由接线落地自然解决。
- **验证**:全仓 **6801 收集,exit 0 零失败**(净增 58 测试);`python scripts/entry_check.py` **27/27 全绿 17.5s**;env 隔离 scan/queue 双 checkpoint 实跑通过;LIVE 服务(REST/复核台/关键词页)热更新重启全部 200/health OK。
- **遗留状态:V10.17–V10.20 CHANGELOG 所列遗留项至此全部清偿或正式关闭**。仍待负责人决策的仅剩:① git 提交(V10.11 起 11 个版本约万行级改动在工作区,**强烈建议立即落盘**)；② 次要登记项(timesource 三处 holdout 收编:telemetry_trace/notify/vlm_cache 时间式;service/app.py URI 直连若入包需登记;BUSY_TIMEOUT_MS 文档性常量收编)。

## V10.20(2026-10-13)—— 世界性创新升级+深度修复波:9 处真 bug 根治 / 3 项创新 / 全入口体检器(4 席位并行 + 负责人收口)

波前快照 `D:\3\netsentinel-backup-pre-v1020-20261013.tar.gz`;基线全量 exit 0。打法:三路深度修 bug(实跑体检+边路深读)+ 一路创新旗舰,负责人收口文档漂移与线上热更新。

- **真 bug 根治(9 处,全部"只有实跑/深读才暴露"级)**:
  1. **Louvain 模块度内部项漏乘因子 2**(intel/graph_kernel.py):标准加权 Q 的社区内边须计两次,代码转录丢因子 → 三角形/K4/等权团等最强社区信号全被拆成单点(gang_mode=community 路径);按 docstring 本正确的推导式修回,4 组边界用例数值对照+4 处编码旧缺陷预期的断言更新。
  2. **8 个 webui 页面 `raise SystemExit(main())` 杀 streamlit 脚本线程**(webui 全页 `__main__` 块):SystemExit 属 BaseException,streamlit rerun 线程只捕 Exception → 每轮收尾事件不发、AppTest 官方测试通道全灭;改为仅真直跑(无 script_run_ctx)才转退出码。
  3. webui/keywords_page 词组"启用/载入/新建/另存"成功后不 rerun(编辑器与词组表停留旧值)。
  4. webui/dashboard 四眼批准成功后不刷新(第二审核人流程引导失效);groups_page 声明落库同款。
  5. webui/groups_page 组选择器字典去重吞组(多个空名组后者覆盖前者,排前组无法选中)——同名加"(第N组)"消歧、声明键仍用真实组名。
  6. webui/keywords_page 清空按钮改已实例化控件的 session_state 抛 StreamlitWidgetAlreadyInstantiatedError(AppTest 端到端当场抓住)。
  7. service/app.py `main()` 忽略 argv(--help 直接绑 8765 起服务)——加 argparse(--host/--port/--help,默认语义不变)。
  8. submit/profile.py 交互 setup 的 stdin EOF 裸崩(EOFError traceback / 注入流 readline 死循环)——收敛中文取消提示退出 1。
  9. vision/arbiter.py vlm_prompts 增强的 `except Exception: pass` 连模块 bug 也吞——收窄 ImportError+其余 debug 日志。
- **创新旗舰 3 项**:
  1. **scripts/entry_check.py 全入口离线体检器**(27 条 curated 清单:18 CLI+8 webui 导入+1 service 工厂;临时沙箱 NETSENTINEL_DATA_DIR+隔离 config 危险开关全关+自起本地静态服务,**零真实外呼零真实 data/ 写入**;--list/--only/--json;退出码 0/1/2 门禁语义)。实跑 **27/27 全绿 17.4s**;27 个单测+1 门控真跑。docs/USAGE.md 新增 §1.7。
  2. **证据池感知去重**(submit/evidence_pool.py 重写+双执行器接线):Top-10 选取前按 pHash64 汉明 ≤6 剔除近重复(同图变体只留 ensemble 分最高,**名额回填不重复图**);三重优雅降级(PIL 缺席/单张解码失败/比较异常);evidence_pool.json 留痕 dropped_near_duplicate+notes 提示;`evidence_pool_dedup` 默认开启(仿 V10.16 先例)。+10 测试。
  3. **关键词模板持久化**:`<data>/keywords/templates.txt`(与 active.txt 同纪律)+ 关键词页模板编辑区(原子写+.bak+同源校验)+ 预览默认读持久模板+cli_hint 联动 `--template-file`;discovery CLI 新增可选 `--template-file`(与内联 --template 叠加,缺省主线逐字节不变)。+16 测试。
- **其余修复与加固**:REST GET /queue 可选分页(?limit&offset,缺省全量**完全向后兼容**,非法 422 中文,+4 测试;API.md 同步含 400→409 错误码勘误);setup/trigger `_announce` 可注入 sink(库链路 print 可替换,CLI 行为不变,+5 测试);telemetry 8 线程×500 次并发回归测试(计数不丢);webui 全页控件 key 唯一性审计(25 处显式 key 矩阵化,6 个 key 构造纯函数+9 断言+AppTest 渲染冒烟 9 用例——UI 层从此有回归网);8 页空态/脏数据 78 项探测全过+headless 渲染 16 项全过;USAGE/README 的 `netsentinel.profile`→`netsentinel.submit.profile` 文档漂移更正。
- **实跑体检**(三席+C 席):33+ 条 CLI/页面入口逐条实跑记录退出码,除设计内非零(vlmctl doctor 无密钥 1 等)全绿;边路深读 8 项核查结论(fetcher 熔断×重试/robots 双缓存/redirect 环路/kernel_wire 双路径/failover 半开复位/cascade 兜底短路/vlm_client 熔断次序/rate_limit stale——7 项无缺陷+1 项挖出 Louvain 真 bug)。
- **环境发现(如实登记)**:① **NETSENTINEL_DATA_DIR 仅 webui 认,核心 CLI 不认**(数据目录 CWD 相对)——涉 contracts.py 字段语义,列为下波候选;② D 席一次原型失误向真实 data/audit.jsonl 追加 1 行 takeover/wizard 事件(未删,如实登记;终版体检器三重防护杜绝);③ B 席首跑误写真实 data/ 已外科还原(review_queue id=2 行/audit 2 行/evidence 目录)。
- **验证**:全仓 **6743 收集,exit 0 零失败**(净增 93 测试);`python scripts/entry_check.py` 27/27(17.4s);LIVE 服务(REST 8765/复核台 8501/关键词页 8502)热更新重启,分页接口实测生效、两页 200。
- **遗留(下波候选)**:① NETSENTINEL_DATA_DIR 核心链路接入(涉 contracts.py);② 时间源单源化(承 V10.18);③ GET /trace 等其余读端点分页;④ arbiter/failover 通电决策;⑤ git 提交(V10.11 起全部版本仍在工作区,**强烈建议尽快落盘**)。

## V10.19(2026-10-13)—— 关键词工作台:发现搜索词网页实时编辑与切换(负责人直研)

需求:关键词能在网页里实时编辑切换。发现子系统的搜索词(红线 28:零内置)此前只能手工编辑 .txt 后经 CLI 使用;本版把它搬进浏览器。

- **新增 `webui/keywords_page.py`**(第 8 个独立入口页面,严格双段铁律):
  - **文件布局**:`<data_dir>/keywords/active.txt` 为**当前关键词活文件**(发现命令 `--keywords-file` 指向它)、`<名称>.txt` 命名词组库、`.bak` 覆盖保存前滚动备份(单份);关键词文件随 `data/` 整体不入库(搜索意图敏感)。
  - **实时生效机制**:发现每次运行都重新装载关键词文件——编辑保存/词组切换落盘后,**下一次发现运行即生效**,无需重启任何服务。
  - **三页签 UI**:①当前关键词(编辑器+实时统计[有效词/空行注释重复]+保存+展开预览[关键词×模板→实际查询,纯本地零外呼,展示上限 200]+CLI 命令提示)②词组库(清单表/一键启用=整组拷贝为活文件/载入编辑器/删除/新建/另存当前为词组)③红线与用法说明。
  - **纯逻辑层 12 个函数**(无 streamlit 依赖可独立单测):keywords_root/active_path/parse_stats/read_active/save_active/list_sets/read_set/create_set/delete_set/switch_active/save_active_as_set/preview_queries/cli_hint。
- **红线落实**:零内置关键词——保存/新建/切换一律复用 `discovery.keywords.load_keywords` 校验(空表中文报错,红线 28 同源);**页面零外呼**——绝不发起发现/搜索,只展示 CLI 命令提示(发现须显式 CLI 运行且受 discovery_online/密钥门槛);**原子落盘**——复用 V10.12 `atomic.replace_with_retry`,覆盖前 `.bak` 滚动;**词组名防穿越**——`[A-Za-z0-9_\-中文]{1,24}` 白名单+保留名 `active` 拒用。
- **文档**:docs/USAGE.md 入口表补"关键词工作台"行、线索发现行补 `active.txt` 约定;docs/DISCOVERY.md 新增"关键词工作台"小节;webui/README.md 新增同目录 8 页面总表(可发现性)。
- **测试**:新增 tests/test_keywords_page.py 15 项全绿(路径布局/词组名防穿越与保留名/空表校验同源/实时统计含重复行/.bak 滚动与无 .tmp 残留/清单排序与坏词组如实标注/拒覆盖/删除与切换/整组拷贝/另存/展开预览三态/cli_hint/无 streamlit 时 main() 退出码 1 中文提示/惰性导入存活)。
- **验证**:全仓回归零失败(exit 0);未安装 streamlit 环境直跑打印中文安装提示退出码 1(与其余 7 页同款降级)。
- **实跑收口(本版部署实测抓出两处 UI 层缺陷并修复)**:
  1. **复核台页签控件 key 碰撞**(webui/app.py):`st.tabs` 每轮执行**全部**页签体,同一 pending 条目在"待复核"与"全部"页签重复渲染,批准确认复选框 `key='chk_{id}'` 等 4 组控件键冲突 → StreamlitDuplicateElementKey 异常(纯逻辑测试无法覆盖 UI 层,实跑才暴露)。修复:`_render_card/_render_actions/_render_plan_preview` 透传**页签身份前缀**(pending_/approved_/all_;不能用侧栏筛选值——多页签 effective 相同时仍碰撞)。
  2. **证据图片相对路径双拼**(webui/app.py `safe_image_paths`):报告记录的图片路径相对项目根(已含 `data/` 前缀),UI 又以 `cfg.data_dir` 作 base 拼接成 `data\data\...` 导致复核台图片全部不显示。修复:相对路径**先按原样(cwd 相对)解析,找不到再按 base 拼**,两种历史口径兼容;实测 5 张证据图全部上屏。
  3. 实测环境注记:Streamlit 1.65 的 text_area 需 **Ctrl+Enter 提交**(非失焦),关键词工作台实测"输入→实时统计→保存→落盘 active.txt"全链路通。

## V10.18(2026-10-13)—— 全模块世界性升级波:性能/健壮/可观测/质量四线并进(5 席位并行 + 负责人收口;仿 V5.0"零新功能零新依赖"纪律)

波前快照 `D:\3\netsentinel-backup-pre-v1018-20261013.tar.gz`;基线 6605 收集全绿;纪律:零新功能、零新依赖、零 API 破坏、主线默认行为不变,性能改动一律带等价性证明(操作计数/逐字节/随机性质/暴力对拍,仿 V10.12–V10.15 先例)。

- **S1 采集/情报性能健壮**:`intel/canonical.py::canonical_key` 加 lru_cache(2048,线程安全,签名经 `__wrapped__` 不变;200 条随机 URL 缓存开/关全等 + 3000 键淘汰等价测试)——批量链路(bulk_intake/batch_scan/group_linker)同 URL 反复归一化全命中;**graph link 全量路径 O(P·S)→O(P)**——`_link_shared_kind` 弃逐对物化并集临时集合,改 |a∪b|=|a|+|b|−|a∩b| 整数恒等式(V10.14 增量路径同口径;6 站 24 图随机重叠对暴力参照逐对全等测试);discovery 缓存时区防御(naive created_at aware 化统一比较,NULL/畸形按未命中,4 测试);merge_bundles manifest 流式读取(json.load 直读免整串拷贝);fetcher/discovery 网络超时全覆盖审计(13 个出站点全带 timeout,零缺口);text_fusion 门控双侧计数 telemetry;`_HOST_LOCKS` 有界化经论证放弃(取用协议竞态+同一性契约冻结,注释存档);site_map BFS/redirect 环判定/phash LSH 核查无缺陷。+9 测试。
- **S2 决策/存储/提交**:**rate_limit TOCTOU 决策间隙闭环**(V10.17 遗留③)——RateLimiter 新增 `locked()` 上下文管理器(线程本地重入计数,段内 can_submit/record 共享外层临界区;不使用时逐字节等价),batch_submit 判定→计划→执行→落账整段包进 `with _rate_locked()`(getattr 探测,旧替身自动回退);双线程并发恰好配额不超发测试。replay 偶发失败 47 次复现不可达(产品侧建库/关连接实证无瑕),测试侧加诊断性前置断言加固。submit 链路 trace span 补齐(`submit.rate_limit/plan/execute`,trace_enabled 门控默认关零行为,span 存在性测试);SQLite 卫生审计(领地 6 处 connect,补 replay/cli.py 直连的 WAL+busy_timeout);数值确定性核查:triage 整数精确无舍入、sprt 到达序累加无重排路径(fsum 化破坏吸收停机语义,放弃)、reliability 检查点 vs 重放 5.6e-17 远小于声明容差——三处均无求和顺序分歧缺陷,零改动;form_models auto_profile 静默吞错收窄为 debug 日志、policy/engine.py:552 生产 assert 改显式 RuntimeError(质量审计移交项);merkle/ed25519 实测(10 万事件 append 0.51s/验签 3.2ms)无瓶颈。+3 测试。
- **S3 vision**:**ensemble_scores sum→math.fsum**——实测抓出同组成员分 `[0.2,0.65,0.25,0.9]` 因到达顺序不同左结合累加得 0.5 vs 0.49999999999999994,`agg>=0.5` 硬比较直接翻档 SUSPECT/CLEAN(fusion V10.12 同类缺陷),fsum 后任意顺序逐位一致(顺序无关性测试锁定);可观测补齐:`vision.throttle.wait`(等待计数+时长 observe)/`vision.negotiate.retry`(failover 已有 failover.switch 不双计);**vlm_client 响应体 10MiB 上限**——`_read_capped` 分块读取(上限内逐字节等价,成功超限抛中文 RuntimeError,错误体截断;4 测试,其中 1 个抓出初版跨限块整块丢弃缺陷);preprocess 4000² 峰值实测 91.8MiB 可接受、ring/dft_ring 采样与尺寸解耦(实测 17/21/56ms)、cache2/vlm_cache TTL 每日至多一次批量清无全表扫描——核查无缺陷。+10 测试。
- **S4 外围/服务**:service `_find_entry_id` O(N) 全量物化降为 SQL `SELECT COALESCE(MAX(id),0)` 只读连接(打不开回落旧路径逐字节等价;三态+对拍+炸弹证明真命中,4 测试);service jobs 线程安全审计(jobs 本体无实质竞态;`_get_scan_pool` 单例 check-then-act 加双检锁防并发首扫各建池泄漏,get_trace 读键收进锁内);webui dashboard `_render_agreement` zip manifest mtime 键控缓存(上限 256 LRU,命中零 IO,实测冷 17.4ms→热 6.5ms;同结果/失效/淘汰等价 3 测试);webui 大列表核查(app/groups 补 50 条渲染上限+超出提示,runs 20/models 5/providers 50 已有,指标计算仍全量)。+7 测试。
- **S5 只读质量审计席**(全仓 11 项扫描,4 项满分):utcnow 0 处/naive 17 处分级(13 可安全迁移 strftime 场景、2 子系统自洽勿动、1 格式耦合);timeout/encoding/可变默认参数/裸 except 四项零缺陷;print 全部锁在 CLI 入口(唯一库链路渗漏 setup/trigger.py 向导播报,GUI 化时再立项);生产 assert 3 处(1 处已由 S2 修);telemetry 线程安全实现验证成立但缺并发回归测试(下波补);TODO 终态 2 处(集中 provider_quirks 上线复核项);距世界级剩余差距 Top5:时间源单源化/SQLite 口径机制化/telemetry 并发测试/降级可见性/assert 与 print 边界。
- **负责人收口**:discovery 缓存补 WAL+busy_timeout(全仓存储口径对齐,跨进程并行 discovery 不再 SQLITE_BUSY);线索文件头时间戳统一 aware 格式;**glm_adapter 独立传输层补同款响应体上限**(复用 vlm_client._read_capped,专用层→通用层单向依赖无环;测试桩 FakeResponse.read 补 size 参数对齐真实 http.client 语义,超限中文 RuntimeError 测试)。
- **验证**:全仓 **6635 收集,6613 passed + 22 skipped 零失败 exit 0**(净增 30 测试);`node --test` 17/17;**双版本编译通过(Python 3.11 + 3.14 compileall 全仓零错)**;kernel_bench 17/17 通过(numpy 后端在席)。零 API 破坏、contracts.py 未触碰、红线全部保持。
- **遗留(下波候选)**:① 时间源单源化 `_now_iso()` 贯穿全仓(S5 Top1,牵 tz-aware 化需单独立项);② SQLite 连接工厂机制化(S5 Top2);③ telemetry 8 线程并发回归测试;④ GET /queue 万级队列分页(API 形状变更需立项);⑤ setup/trigger 库链路 print 改注入回调(GUI 化前置);⑥ arbiter/failover/prompt_dialects 通电决策(默认关哲学保持);⑦ git 仍未提交(V10.11 起全部版本在工作区,建议尽快提交)。

## V10.17(2026-10-13)—— 全项目审计驱动深度升级重构波:缺陷修复/解耦拆包/未通电内核接线/外围闭环/文档还债(5 席位并行;负责人基线+收口终验)

前置:全项目 5 路深度遍历审计(文档层/采集侧/决策输出侧/外围质量体系/全局横切)产出问题清单,本波逐项兑现;波前快照 `D:\3\netsentinel-backup-pre-refactor-20261013.tar.gz`;开工前基线全仓 6566 收集全绿。

- **H1 采集侧(discovery/crawler/pipeline/evidence/intel)**:discovery URLError(DNS/断网)击穿修复——引擎层转中文 RuntimeError、pipeline 层 OSError 兜底记 errors 绝不击穿整轮(+2 回归测试);orchestrator `run_submit` 队列连接 finally 关闭(对齐 V10.12 扫描路径修法);robots.txt 拉取 10s 超时+失败 TTL 负缓存(免挂起重试);UA 三处统一 `NetSentinel/0.17`(fetcher/redirect 兜底/yandex);capture_v2 wait timeout 补 int() 取整;**文本融合接线** `_site_text_features`(页文本线索经 text_intel 复算入 fuse 第三参,`getattr(cfg,"enable_text_fusion",False)` 默认关,主线逐字节不变);kernel_wire 弃 `graph._conn` 私有穿透,改调 EvidenceGraph 新公开口 `commit_pending()`;回环判定口径三处统一(127/8+::1)加交叉锁注释。
- **H2 决策/存储/安全**:rate_limit 跨平台文件锁(msvcrt/fcntl 惰性导入,`<state>.lock` 同目录,30s stale 自愈降级不死等;can_submit 扫描与 record 读改写全程持锁,消除"丢时间戳行→频控被放宽"超发根因;+5 并发回归);finishflow summary 计时器错位修复(SummaryAgent.run 真正耗时纳入 `_METRIC_FINISH_SUMMARY`);**storage/replay.py(2067 行)拆包**为 `storage/replay/` 包(_common/projection/reconcile/heal/cli 五文件,34 个公开名 `__init__` 重导出,`from ... import replay` 与 `from netsentinel.storage.replay import X` 双用法实测兼容,AST 对比逐字搬运;新增 `python -m netsentinel.storage.replay` 入口);decision↔storage 架构环解耦接缝——`set_queue_factories` 注入工厂(未注入保持惰性导入默认行为,完全向后兼容,+注入测试);security 三份 `_tighten_key_file_permissions` 收敛 `security/_fsutil.py` 单实现(三处保留同名薄包装,测试 monkeypatch 锚点不变);review_queue 崩溃窗口("账本有 entry_added 而 entries 无该行")差异报告补可执行中文处置指引(不自动重建,+3 测试);finishflow/batchflow 四函数双拷贝(_as_mapping/_merge_resumed_items/_trace_id_of/_site_dirname)提取 `netsentinel/_flowutil.py`(口径差异经 require_fields 开关原样保留)。
- **H3 vision 重构**:cascade/arbiter 重复约 148 行同名助手提取 `vision/_vlm_helpers.py`(build_glm_client/build_vlm_cache 经参数注入日志文案,输出逐字不变;原模块名 re-export 保测试兼容);**provider_throttle 通电**——vlm_client `_post_with_retry` 每次真实外呼尝试(含重试跳)前取令牌,注入点默认禁用(`set_default_rpm()`/`cfg.provider_rpm` 双开启路径),主线零影响(+10 测试);**model_negotiate 通电**——multi_provider 捕 ModelNotFoundError 后可选换名协商重试(`getattr(cfg,"model_negotiate_enabled",False)` 默认关;重试前 `spend_one()` 再记账守红线 19;新名进程内定格;+6 测试);死代码核查(arbiter 的 DISAGREE_GAP 被 case_agent 活引用不可删;failover/prompt_dialects 未通电保留——"先进内核默认关"哲学);provider_quirks 三处 TODO 补 docs/PROVIDERS.md 指引注释。
- **H4 外围闭环**:webui/service **门户动态化**——`_VALID_PORTALS` 硬编码改惰性调 `portal_defs.list_portals()`(失败回退内置 12377/shdf 并告警),计划生成统一切 `build_plan_from_def`(v1/v3 语义等价由既有一致性测试锁定;A233 动态门户从此可进 webui 预览与 REST /plan);webui 标签口径统一 `webui/_labels.py` 单一权威(clean 统一译"未见违规",四页面共享同一字典对象);**driver.mjs 三层防线加固**——goto 前守卫升级子域感知(精确等值或 .域名 后缀,`notshdf.gov.cn` 等伪装域不误伤)+ goto 后 `page.url()` 最终落点校验(堵 302/meta refresh/JS 跳转绕过真实门户拦截)+ framenavigated 导航拦截;删恒真死代码 `!LOCAL_HOSTS.has(host)`;**新增 `drivers/driver.test.mjs`**(node:test 零依赖 17 用例:四主机含子域拒绝/回环与 example 放行/captcha 四路拒绝/human_gate 无跳过参数断言;**全仓 JS 测试零的突破**,Node v24 实测 17/17);portals 两 YAML note 补"mock 约定"声明。
- **H5 文档配置还债**:.gitignore 补 config.yaml/profile.yaml/watchlist.yaml/keywords.txt(防密钥/举报人身份/巡查目标入库,example 不受影响,git check-ignore 验证);pyproject 新增 `system = ["psutil>=5.9","keyring>=24"]` extra(清"用了没声明"缺口;httpx 经 grep 证实 4 处测试引用**保留**未删);README.md 重写至 V10.16 实况(18 运行入口速查全经 argparse 核实/30+ 子包职责/契约体系);docs/USAGE.md 重写(全 CLI 入口+参数/13 键选择器/18 步序列读 form_models.py 核实/Config 分组概览);ETHICS.md §6 如实更新(自动上传发生在人工门之前,人工门仍逐条核对,红线 1/2 零触碰);ARCHITECTURE.md 刷新 L0–L5 分层;API.md 补 /model/* 四端点+/trace+X-Trace-Id(响应键 playbook_md 修正);DEPLOY.md 路径 C:\1→D:\3+8766 向导端口;**新增 docs/INDEX.md(36 篇文档分类索引)与 docs/REDLINES.md(55 条红线总表,含 38/39 重号歧义专节)**。
- **验证**:全仓 6605 收集,**6583 passed + 22 skipped,零失败 exit 0**(净增 39 测试;各席位领地测试自证+负责人全仓终验两轮);`node --test` 17/17;零 API 破坏(contracts.py 未触碰、全部新增口为带默认值参数/私有重构);红线全部保持——所有新接线默认关闭主线逐字节不变、人工门/频控/禁网/密钥红线零触碰。
- **遗留(下波候选)**:① CONTRACTS.md 主契约 §3/§5 仍是 8 键/12 步/"不自动上传"旧口径(增量契约 V10/V11/V12 已各自追认,主契约禁改待负责人出勘误);② 红线 38/39 重号是否重编号待负责人定夺(REDLINES.md 已梳理双面对照表);③ rate_limit can_submit→record 决策间隙需 batch_submit 在锁内闭环(本波只消状态文件竞态根因);④ arbiter/failover/prompt_dialects 仍未通电(默认关哲学保留);⑤ git 仍未提交 V10.11 起全部版本(建议尽快 `git add -A && git commit`)。

## V10.16(2026-10-13)—— 证据截图自动上传:每站预留 Top-10,按门户配额上传(负责人直研)

需求:每个网站名下的举报预留十张色情证据截图;扫黄打非自动上传五张、12377 自动上传十张。

- **新增 `submit/evidence_pool.py`**:从证据包 manifest.json 的 ensemble 评分行按 **nsfw_prob 降序**取 Top-10 现存图片(不足用整页截图补足;目录内同名优先、原绝对路径兜底;去重保序);预留清单落 `evidence_pool.json` 留痕(幂等);门户配额 `PORTAL_QUOTA = {12377: 10, shdf: 5}`;manifest 缺失/损坏一律空池降级,纯本地零网络。
- **执行器接线(双执行器同源)**:`_run_plan_steps` 在 **HUMAN_GATE 之前**执行 `_auto_upload_evidence`——`page.set_input_files("#report-file", 按门户配额切片)`,notes 记 `[上传] …N 张(人工门请核对附件)`;任何失败只记 notes 降级(人工门手动上传兜底),绝不中断举报。**红线 1/2 零触碰**:上传发生在人工门之前,人工门随后可见核对附件、输入验证码并最终确认——验证码与提交永远属于人工(与 V10.1 FOCUS 半自动焦点同一设计哲学)。干跑在命中池时预演提示(无池零 notes,既有干跑断言不受扰)。
- **测试**:新增 test_evidence_pool.py 7 项(排序截断/截图补足/配额切片/缺 manifest 空池/双执行器真实模式配额上传(fake page)/无池降级 notes/干跑预演),executor 双套件回归全绿。

## V10.15(2026-10-13)—— 创世纪全项目深度架构升级(6 席位并行;契约见 CONTRACTS-V17.md)

以项目并行代理方法论(文件互斥 + 零 API 破坏 + 领地测试自证)一次派 G1–G6 六席位做全项目架构级升级,负责人收口接线与全仓终验:

- **G1 crawler 引擎层**:browser.py 新增 `browser_session` 会话原语(contextmanager,统一回收,缺 playwright 产出 None);capture_v2 抽公共核心(`_capture_loaded_page_v2` 等)并新增 **`capture_pages_v2` 批量口**——v2 引擎(滚动懒加载)同样一次 launch 逐页复用、瞬态重试只重建 page、fail-fast;fetcher 闸门 host 贯穿 robots 与批量下载(同 URL 解析 3→1 次)。**负责人接线**:orchestrator `_default_capture_many` v2 分支改调 `capture_pages_v2`(旧口回退)。site_map/redirect 核实无收益放弃。+13 测试。
- **G2 vision 传输层**:prompt_dialects `_encode_image/_data_uri` 纯函数 lru_cache(16;**api_key 是 build_request 形参——密钥绝不进缓存键**,入口刻意不缓存);response_repair True/False/None 字面量正则模块级预编译;vlm_client `_extract_content` 成功路径免预 dumps 错误片段(仅失败分支惰性构造,消息逐字符不变)。provider_throttle/vlm_client 序列化核实无冗余放弃。+16 测试(含密钥不进缓存静态核查;1 条既有计数断言按红线 21 更新)。
- **G3 intel 检索层**:`PhashRegistry.find_similar` 64bit 路径(phash/mirror)**4 带×16bit 鸽笼预过滤**——距离 d′ 的每个翻转位只落一个带 ⇒ 非零带数 ≤ d′;非零带数 > max_distance 的行免精确汉明。返回/排序/并列/截断/dirty_hash/遥测逐位不变;有效域 d≤3(生产查重口径 d=0 全额受益),d≥4 诚实退化等价快速路径;pyramid(三层 min)无法安全分带保持原样。随机性质测试 800 对锁定等价 + 操作计数断言。+14 测试。
- **G4 ops 并发层**:pool `_PendingGauge` 未完成计数 O(1) 化(提交 +1 / add_done_callback −1,轻量锁护跨线程;静态背压与 AIMD 共用一次读数),替换逐 future `done()` 扫描;背压布尔语义/停顿序列恒等,红线 35 常量源级断言不变。scheduler 批量读属 intel 领地不擅改(建议后续 site_memory 批量 API);concurrency 核实无缺陷。+5 测试。
- **G5 submit 执行层**:双执行器逐字重复的步骤循环(约 45 行)提取共享 `_run_plan_steps`(session 委托 playwright 同源);rate_limit `can_submit` 改 `_scan_window` 单遍流式聚合(内存 O(1),**判定口径/文案/落盘/旧状态文件兼容零变化**)。"只读尾部"IO 捷径经论证会放宽频控(红线)而放弃并记录。HUMAN_GATE/验证码/auto_confirm 红线零触碰。+多组等价测试(两执行器 dry_run 逐 Step notes 全等)。
- **G6 存储报表层**:event_log 新增 **`append_many`** 批量口(整批校验→单事务 N INSERT + seq 连续校验 + 一次 commit;任一条非法整批拒绝零 DB 触碰;旧 append 抽共享校验行为不变);html_report 图片名/标题重复 escape 2→1 次(冻结 sha256 快照证明渲染逐字节一致)。replay/batch_report/run_summary 核实无 N² 放弃。+19 测试(含 9 参原子拒绝与 5 组快照)。
- **验证**:各席位领地测试自证全绿(G1 127+6s / G2 191 / G3 135 / G4 154+54 / G5 93+8s / G6 187);负责人收口后全仓回归见下。
- 全波新增测试 70+;**零 API 破坏**(全部新增口/带默认值参数/私有重构),禁改基座未触碰。

## V10.14(2026-10-13)—— 增量关联折叠引擎:图谱通电 O(K²) → O(K)(负责人直研;单独立项兑现 V10.13 遗留)

- **问题**:`link_shared_images / link_templates` 每次通电全图重算——读全量节点 + 全量边、枚举**全部**站点对重算权重并整表重写;每站扫描调用一次,随图谱累积呈平方膨胀(K 站 = K 次全图重算)。
- **等价定理(append-only 前提)**:本类素材写口(add_image/add_template/+bulk)使 meta.sites 单调增长、无删除;故自上次 link 起,两端点均未触及的站点对其 |items_a|、|items_b|、|items_a∩items_b| 全不变 ⇒ 权重不变、共享状态不变、不可能陈旧 ⇒ 未触及对的边状态与全量重算完全一致。增量模式只需处理触及站点对。
- **实现**(`_link_shared_kind(touched_sites=...)`):单遍扫 kind 节点维护**每站素材计数**(并集分母经恒等式 |a∪b| = |a|+|b|−|a∩b| 整数求出,与全量集合运算的浮点商逐位相同),只枚举含触及站点的对(≤K 个,全量 ≤K²);既有边只读触及端点(SQL IN 限定,免全边表扫描);触及对 upsert + 触及对陈旧删除(append-only 下恒空,保留对称覆盖);触及数 > 256 自动回退全量(SQL 变量数防御)。**全量路径(touched_sites=None/空)与旧版逐字节一致**;调用契约:touched_sites 须覆盖自上次 link 起素材有变化的全部站点,漏报只滞后不自愈错(下次全量自愈)。
- **接线**:kernel_wire 通电步改 `_link_touched`(本站即唯一变化站点,传 touched_sites=[site_url]);注入替身/旧实例不接受关键字自动回退全量(语义等价)。每站通电复杂度 O(K²) → O(K)。
- **测试**:新增 test_incremental_link.py 6 项——**固定种子 40 步随机追加序列性质验证**(冷启动/私有素材/2~4 站共享/幂等重登/单条+批量混写/image+template 混跑;每步断言增量图与全量图全部边**含权重逐位相同**、新增计数一致,收敛到全量不动点)、未触及对冻结、权重刷新浮点相等、空 touched 显式全量、kernel_wire 接线 spy。图谱相关 7 个测试文件 207 项全绿。
- 净效果:`graph_wire` 开启时,站群通电从"每站全图重算"变为"每站只算自己的关联"——百站图谱每站通电的计算量降两个数量级,且边数据与全量重算数学等价。

## V10.13(2026-10-13)—— 架构级性能重构波:浏览器会话复用 / 批级单事务写入(负责人直研)

兑现 V10.12"后续建议"中的两项架构级优化(第三项 link 增量化另立项):

- **浏览器会话复用(A 类,真实扫描最大耗时点)**:此前 v1 引擎同站扫描每页各做一次 `sync_playwright().start()` + `chromium.launch()` + close + stop(冷启动 0.5~2s/页)。新增 `crawler/browser.capture_pages(urls, cfg)` ——**一次** Playwright 启动 + launch,逐页 `new_page()` 复用,单页采样口径(goto 瞬态重试 / networkidle 容错 / 整页截图 / 图片证据 / 文本线索 / 慢采样告警 / capture.page 计时 / capture.errors 计数 / fail-fast 传播 / allow_network 逐页闸门)与 `capture_page` 逐项一致;缺 playwright 自动逐页退回退化模式(行为不变);单页失败原样上抛、会话 finally 统一回收。orchestrator 缺省链路(未注入 capture/fetch_page 且 v1 引擎)改走 `_default_capture_many` 会话批采样;注入路径与 v2 引擎逐字节不变。新增 test_browser_session.py 5 项(fake playwright:单次 launch/逐页回收/fail-fast 清理/无依赖退化/逐页安全闸门)。
- **PhashRegistry.register_many(B 类,写放大消除)**:批量登记口——先全量校验(任一行非法整批拒绝、不落半截数据)再一次 executemany + 单次 commit;UPSERT/覆盖/可选列语义与逐条 register 完全一致,N 行从 N 次 WAL fsync 降为 1 次。kernel_wire 登记步改为循环收集 + 一次批量落库(LSH 条件插入原位不变;旧实例缺 bulk 口自动回退逐条)。
- **EvidenceGraph.add_images_bulk / add_templates_bulk(B 类)**:`_add_media_node` 拆出无锁内核 `_add_media_node_locked`,批级方法单锁单事务写完整批(异常整批回滚);单条写口语义不变。kernel_wire 通电步素材登记改批级(首见序去重保持;旧口回退)。graph_wire 开启时每站数十次单行事务 → 3 次(素材 1 + link 既有批量 + 登记 1)。
- **测试**:新增 test_bulk_registration.py 5 项(批量=逐条等价/原子校验/覆盖与空批/图谱节点等价/跨站并入与幂等);受影响 15 个测试文件(461 项)全绿。
- **遗留(下波候选)**:link_shared_images/link_templates 增量化——现每次全图站点对重算;数学上追加点只影响触及站点对(共享数与并集对第三站不变),可改"触及站点对限定重算",默认关闭开关不变,单独立项做。

## V10.12(2026-10-13)—— 深度优化升级波:跨版本兼容 / 跨平台健壮性 / 数值确定性 / 热路径降复杂度(负责人直研)

- **Python 3.10–3.11 兼容缺陷修复(A 类,阻塞级)**:`submit/form_scanner.py` 草稿 note 的 f-string 花括号内换行是 3.12+(PEP 701)语法,在声明支持的 3.10/3.11 上直接 SyntaxError(全仓 AST 扫描唯一违例)——提取局部变量改写,3.10+ 全兼容。附全仓 3.11 语法审计(175+ 模块仅此一处)。
- **跨平台原子替换统一(B 类,状态文件防损)**:新增 `netsentinel/atomic.py::replace_with_retry`(仅 PermissionError 有限重试 ≤2s,其余异常首错即抛;POSIX 行为与裸 os.replace 逐字节一致;语义与 tier_state 既有实现同参)。接入 4 处状态写回点——`security/timestamp.py`(时间证明计数器)、`vision/model_manager.py`(活动模型状态,webui/CLI 跨进程读写)、`submit/rate_limit.py`(频控状态,并行提交并发写)、`sessionstore.py`(会话存档,另补失败时临时文件清理)。
- **Windows 密钥权限收紧账户链修复(B 类,安全)**:`keys/vault/bundle_sign` 三处 `_tighten_key_file_permissions` 的账户回退链 `USERNAME → USERDOMAIN → path.owner()` 有两个缺陷——USERDOMAIN 是计算机/域名而非账户(icacls 必然解析失败)、path.owner() 在 Windows + Py3.10–3.12 抛 NotImplementedError(收紧静默失效)。改为 `DOMAIN\USERNAME` 组合、缺 USERNAME 回退 `getpass.getuser()`,取不到账户时显式提示手动收紧(绝不抛出);test_keys 断言按红线 21 惯例更新(匹配域名限定形态)。
- **融合判定数值确定性(A 类,跨版本行为分歧)**:`decision/fusion.py` 加权和 `sum → math.fsum`——边界用例(Σw·x 恰等于复核线 0.5)此前在不同平台 libm/求和顺序下落到阈值两侧,3.11 与 3.14 判定档位不同(test_fusion 实测抓出);fsum 后逐位确定。
- **热路径降复杂度(C 类,性能)**:`ops/pool.py` 背压计数静态路径改为"数到阈值+1 即早退"(整批提交循环 O(n²)→O(n)·backlog;aimd 路径需精确值保持全量);`vision/phash2.py::tile_any_match` 每侧 tile 预转 int(144 对比较免 288 次字符串规范化,口径等价 popcount(a^b));`crawler/fetcher.py` host 放弃告警改 `itertools.islice` 免拷贝剩余清单;`pipeline/orchestrator.py` 每站扫描的复核队列连接用毕即关(getattr 容错,批量池下消除连接churn)。
- **测试环境基线补齐**:`benchmarks/out/*.json`(6 份)为 .gitignore 的本地生成物,克隆后 test_drift 4 项必挂——补跑 run_benchmark/kernel_bench/grouping_bench/providers/adversarial/tier_bench 全部生成(stub 离线,基准指标全绿:P/R/F1=1.0)。
- **新增测试**:test_atomic.py 5 项(成功零等待/共享冲突重试/耗尽上抛/非 PermissionError 不重试/str+Path 兼容)。
- **后续建议(本波未做,涉及架构级改动,单独立项)**:① browser.py 每页冷启动 Playwright(站内复用 Browser 实例,executor_session 已有范式,真实扫描每页省 0.5–2s);② graph/phash 登记批级合并事务(逐行 commit → executemany,graph_wire 开启后每站数十次 fsync);③ kernel_wire link_* 从每站全图重算提升为批级一次(随图谱累积的平方膨胀)。

## V10.11(2026-10-13)—— 会话累积:跨会话按网站名字累积 / 存档 / 续跑 / 确认举报(负责人直研)

- **需求**:运行结果自动按网站名字分类累积;结束运行自动存档;下次运行在上一次基础上继续;用户确认举报时对所有结果按网站名字举报。
- **`netsentinel/sessionstore.py`**:会话累积库(SQLite WAL,`<data_dir>/session/accumulation.db`)——同 canonical 可注册域一组(与 A104 案件分组、A110 组名解析同口径,www/子域/端口变体并入同组),verdict 取历史最严重档(只升不降)、agg_max 取历史最大、aliases/site_urls 并集;扫描明细 append-only(`scan_events`);`mark_reported` 收档幂等;`archive()` 原子写 JSON 存档(`<data_dir>/session/archive.json`)。A103 canonical 惰性导入,缺席内置同语义兜底;零网络、零提交动作。
- **`netsentinel/sessionflow.py`**:总流程 CLI 五模式——`--input/--url`(扫描→归组入列→逐报告累积→自动存档,复用 A107/A108 链路,永不提交)/ `--status`(累积现状)/ `--archive`(立即存档)/ `--report`(确认举报)。`report_all`:列出全部待举报组与条目状态 → **人工确认(确认前零状态变更:审核人留名 + 显式 y,提示语含"人工核实")** → 确认动作 = 批准 pending 条目(留 actor)+ 逐组 `BatchReview.attest`(红线 25)→ 按网站名字排序组装、超 `batch_max_items` 自动分批,每批 A113 建批 + A112 `run_batch` 顺序举报(恒 `auto_confirm=False`,红线 24;频控不放宽,红线 26)→ **真实提交成功条目回写复核队列 approved→submitted**(修 V6 批量链路缺口:此前批量提交不回写队列,存在重复收集风险)→ 全组完成 `mark_reported` 收档 + 存档刷新;频控挂起下次 `--report` 自动续跑。
- **接线细节**:零新增 Config 字段(路径全部由 `cfg.data_dir` 推导,不动禁改的 contracts.py);兄弟模块全部惰性导入 + 鸭子注入;遥测计数 `sessionflow.*` / `sessionstore.*`(只记名称与数字)。
- **测试**:新增 22 项全离线(累积合并/多段后缀/跨会话续跑/收档幂等/存档原子写;确认举报分组排序/非 y 零状态变更/缺审核人中止/空库零交互/已举报组与其他流程条目排除/频控部分完成只收档已完成组;红线 24 源码级断言锁定)。相关回归(batchflow/batch_submit/batch_state/batch_review/review_queue/case_group/batch_scan)全绿。
- **文档**:新增 docs/SESSION.md;README 文档清单补链接。
- 红线全部保持:1–5/24–26/38 等均未触碰;默认干跑、禁网、人工门不变。

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
