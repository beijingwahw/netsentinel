# NetSentinel V3 团队契约(A41–A60 并行升级)—— 与 CONTRACTS.md(v1)/CONTRACTS-V2.md 共同生效

> 前两轮 40 个模块(A01–A40)已全部就位,740 测试全绿,可直接 import。
> V3 主题:从"感知"到"智能体平台"——案件智能体编排、级联路由、统计保证、证据网络、平台治理。

## 0. V3 新红线(叠加在 v1/v2 十条之上)

11. **政策与流程不得削弱人工门**:政策引擎/四眼复核只能增加审批环节,任何配置组合都不能跳过 HUMAN_GATE 与人工确认。
12. **图谱与哈希库仅存本地**:phash/graph 数据库不得外发;近重复比对只对运营者自己采集的证据进行。
13. **级联与智能体共享同一预算**:所有 VLM 调用(含案件智能体规划、级联升级、自检)必须走 vlm_cache 预算,不得绕过。
14. **统计担保要诚实**:共形预测输出必须附担保成立的前提(校准集来源与规模),不得宣传无条件精度。
15. **测试零外呼、零真实门户、零真实 VLM 调用**(mock 传输层),同前。

## 1. 新增 Config 字段(已由项目负责人落地 contracts.py,禁改)

`case_agent_model / vlm_cascade / vlm_escalate_above(0.85) / vlm_escalate_below(0.15) / phash_db / graph_db / four_eyes_required / policy_path / redirect_max_hops(5) / video_max_frames(6) / conformal_target_precision(0.95) / adaptive_base_interval_h(72)`

## 2. 文件归属(A41–A60,只能写自己名下文件)

```
netsentinel/agent/__init__.py  case_agent.py  case_flow.py                  # [A41] + tests/test_case_agent.py
netsentinel/vision/cascade.py  tests/test_cascade.py                        # [A42]
netsentinel/intel/phash.py     tests/test_phash.py                          # [A43]
netsentinel/intel/active_learn.py  tests/test_active_learn.py               # [A44]
netsentinel/decision/conformal.py  tests/test_conformal.py                 # [A45]
netsentinel/intel/graph.py     tests/test_graph.py                          # [A46]
netsentinel/crawler/redirect.py  tests/test_redirect.py                    # [A47]
netsentinel/vision/video_frames.py  tests/test_video_frames.py             # [A48]
netsentinel/policy/__init__.py  engine.py  policy.example.yaml  tests/test_policy.py  # [A49]
netsentinel/decision/four_eyes.py  tests/test_four_eyes.py                 # [A50]
netsentinel/intel/regulation.py  docs/regulations/*.md  tests/test_regulation.py  # [A51]
netsentinel/submit/describer_critic.py  tests/test_describer_critic.py     # [A52]
netsentinel/submit/portal_defs.py  portals/12377.yaml  portals/shdf.yaml  tests/test_portal_defs.py  # [A53]
netsentinel/security/bundle_sign.py  tests/test_bundle_sign.py             # [A54]
benchmarks/adversarial.py  tests/test_adversarial.py                       # [A55]
webui/dashboard.py  tests/test_dashboard_helpers.py                        # [A56]
netsentinel/cli/__init__.py  review_tui.py  tests/test_review_tui.py       # [A57]
netsentinel/ops/pool.py  tests/test_pool.py                                # [A58]
netsentinel/ops/adaptive.py  tests/test_adaptive.py                        # [A59]
docs/AGENT_GUIDE.md  docs/PLATFORM.md  docs/UPGRADE_V3.md  docs/README_V3.md  # [A60]
```

## 3. 模块 API 规范(签名固定)

- **A41 案件智能体**:`netsentinel/agent/case_agent.py`:
  - `plan_investigation(report: SiteReport, cfg, *, client=None) -> dict`:client 缺省 GlmVlmClient(离线→返回 {"offline": True});提示词(内置中文,风格对齐 vlm_prompts,防注入同款规则)输入=报告摘要(pages/agg/分歧图/URL风险),要求输出严格 JSON:{"hypothesis": "类别假设≤60字", "actions": [{"kind": "rescan_page"|"recheck_image"|"sample_more", "target": str, "reason": "≤40字"} ≤5 项], "confidence": 0~1};解析失败→{"offline": False, "error": 中文, "actions": []}。
  - `apply_plan(plan: dict, report, cfg, *, rescan=None, recheck=None) -> SiteReport`:按 actions 调注入回调(缺省惰性接 orchestrator/cascade,缺失跳过并记 notes);结果合并进 report.intel["case_agent"];只升不降。`netsentinel/agent/case_flow.py`:`run_case(url, cfg, **deps) -> SiteReport` = run_scan → plan → apply(≤2 轮)→ 复核入列(复用 orchestrator 逻辑,兄弟模块惰性导入)。
- **A42 级联路由**:`class CascadeClassifier(NsfwClassifier)` name="cascade",注册 "cascade":`classify(img)`:先 flash(cfg.glm_model,经缓存);分值落在 [escalate_below, escalate_above] 之外(即高置信)→直接用;落在不确定带且配置了升级模型(case_agent_model 或 glm_models_fallback 中非 flash 首个)→再调升级模型取值并 scores 标 {"escalated": True, "flash_prob": ...};预算走 vlm_cache.spend_one(每次真实外呼一次);离线→VlmOfflineError 语义透传给上层(由 orchestrator 跳过)。测试全 mock。
- **A43 感知哈希库**:`phash(path) -> hex str`(PIL 惰性:灰度 32x32 → DCT 8x8(手写余弦变换,stdlib math)→ 中位阈值 64bit;PIL 缺失退化 aHash);`hamming(a,b) -> int`;`class PhashRegistry(db_path)`:`register(image_sha256, phash_hex, site_url, verdict_tag)`,`find_similar(phash_hex, max_distance=8) -> list[{sha256, site, distance}]`(全表扫即可),`stats()`;线程安全 sqlite。测试:PIL 造图,同图缩放/轻微修改距离小、异图距离大、registry 往返。
- **A44 主动学习**:`class ReviewFeedback`:`record(entry_verdict, human_action: "approve"|"reject", agg, nsw_count)`;`threshold_suggestions(cfg) -> list[{"param", "current", "suggested", "reason"}]`:基于 approve/reject 的 agg 分布(如被 reject 的 NSFW 集中在某低分段→建议上调 min_nsw_images 或阈值);样本 <20 返回空并说明;`rank_for_vlm(images_scores, budget) -> list`:不确定度 |p-0.5| 升序(边缘优先)且预算内。纯内存/可持久化 jsonl。测试:构造分布断言方向。
- **A45 共形预测**:`fit_threshold(calibration: list[tuple[float, bool]], target_precision) -> dict`:校准集(分值,是否真阳)按分值降序找最大前缀使 精度≥target → {"threshold": t, "guarantee": "≥95%", "n": 校准集规模, "caveat": "担保依赖校准集分布,样本不足时降级", "valid": n>=30};`apply(scores, threshold) -> {"selected": [...], "expected_precision": ...}`;n<30 时 valid=False 且 threshold=None。测试:构造完美/含噪校准集,断言阈值单调性与小样本降级。
- **A46 站点关联图谱**:`class EvidenceGraph(db_path)`:sqlite 表 nodes(id TEXT PK, kind site|image|template, meta JSON)/edges(src, dst, kind shared_image|phash_near|shared_template|redirect, weight, created_at);`add_site(url)`/`add_image(sha256, site_url)`/`add_template(simhash, site_url)`;`link_shared_images()`(同 image sha 出现在多 site → 边)/`link_templates()`;`related_sites(url, depth=1) -> list[{site, via, weight}]`(BFS);`export_json() -> dict`。测试:三站点两共享图断言关联与权重。
- **A47 重定向追踪**:`trace_redirects(url, cfg, *, fetch=None) -> list[str]`:逐跳(HTTP 3xx Location / html meta refresh / 顶级 JS location.href 正则),≤cfg.redirect_max_hops,环路检测,allow_network 闸门(仅本机);返回访问序列;每跳写 graph(可选参数 graph=None 注入)。测试:本地 server 链 301→meta→JS;环路截断;跳数上限。
- **A48 帧采样**:`sample_frames(media_path, cfg, out_dir) -> list[ImageEvidence]`:GIF(PIL,n帧均匀采 ≤video_max_frames);MP4/MOV 无解码器时返回 []+中文提示(建议 ffmpeg 抽帧后放目录);命名 `<stem>_frame{i}.png`(保留原关键词);sha256/宽高回填。测试:PIL 造多帧 GIF 采帧;无 PIL 分支;假 mp4 提示。
- **A49 政策引擎**:`@dataclass Rule(name, when: dict, action: str, note="")`:when 支持 {verdict∈, min_agg, min_risk(url intel), needs_review};action ∈ {"queue","notify","ignore","four_eyes"};`load_policy(path) -> list[Rule]`(yaml,缺文件→默认单条 queue-all);`decide(report: SiteReport, rules) -> Decision(rule_name, action, note)`:首条命中生效,默认兜底 "queue";**任何 action 都不能跳过人工门,four_eyes 只增审批**;policy.example.yaml 三个示例规则+注释。测试:规则优先级/边界/默认兜底/非法 action 拒绝加载。
- **A50 四眼复核**:`class FourEyesQueue`:组合(非修改)ReviewQueue:`__init__(db_path, required: bool)`;`approve(entry_id, reviewer: str) -> dict`:required=False 或第二人时透传底层 approve;否则记录 approvals(entry_id, reviewer, ts) 表,返回 {"state": "awaiting_second", "first": reviewer};`second_approver(entry_id, reviewer)`:同人重复→ValueError 中文;两人齐→底层 approve;`status(entry_id) -> dict`。测试:全状态机、同人拦截、required=False 直通、与 ReviewQueue 集成。
- **A51 法规检索**:`netsentinel/intel/regulation.py` + `docs/regulations/*.md`(≥3 个中文文件:12377 举报分类与受理范围[引用 A13 调研:政治类/暴恐类/诈骗类/色情类/低俗类/赌博类/侵权类/谣言类/其他类]、扫黄打非受理范围、相关法律条文标题清单——《网络安全法》《未成年人保护法》《出版管理条例》《互联网信息服务管理办法》条目级摘要,只写标题与适用要点,不杜撰条文细节);`class RegulationIndex(corpus_dir)`:`search(query, top_k=3) -> list[{file, title, snippet}]`(中文分词=2-gram,BM25-lite 纯 python);`suggest_category(report) -> {"category": "色情类"|"低俗类"|..., "basis": 检索依据}`。测试:索引/检索相关性/类别建议。
- **A52 描述自检**:`critique_description(draft: str, facts: dict, cfg, *, client=None) -> list[str]`:GLM 路径:提示词要求逐句核对草稿与事实清单,输出 {"issues": [中文问题 ≤5]};离线回退规则版:草稿数值与 facts 不一致、出现夸张词表("大量/极其/遍布/全部")、超 240 字;返回问题列表(空=通过)。测试:mock client、规则版三类问题、通过用例。
- **A53 门户适配器**:`load_portal_def(path) -> PortalDef`(dataclass: name, entry_url_key(读 cfg 字段名), category_value, selectors(dict, 缺省=契约 §3));`portals/12377.yaml`/`portals/shdf.yaml` 与现有 planner 常量一致;`build_plan_from_def(defn, payload, cfg, entry_url=None) -> SubmissionPlan`(步骤序列=契约 §5,复用 form_models 常量,禁止验证码自动步骤);校验:未知字段拒绝、captcha 选择器只能出现在 HUMAN_GATE。测试:加载/计划生成/篡改校验(把 captcha 放进 fill → 拒绝)。
- **A54 证据签名**:`class BundleSigner`:密钥=env NETSENTINEL_SIGN_KEY 或 data/.signkey 自动生成(0600/icacls 尝试);`sign_manifest(bundle_dir) -> str`(对 manifest.json 文件清单排序后 HMAC-SHA256 链,签名写入 manifest["signature"]);`verify(bundle_dir) -> (bool, str 中文)`;密钥缺失且未生成→(False,提示)。测试:签名往返、篡改任一文件检出、无密钥提示。
- **A55 对抗鲁棒性**:`benchmarks/adversarial.py`:PIL 对 corpus 图生成扰动变体(高斯模糊 k=3、8x8 马赛克、25% 中央遮挡、JPEG q=30)到 tmp/bench 目录;`run(corpus_dir, out_dir, classifier="stub")`:原始 vs 扰动的分数矩阵与下降统计 → report.md(中文)+ report.json;CLI `--corpus --out --classifier`。测试:小语料跑通、stub 规则不受视觉扰动影响(文件名保留)的断言、扰动生成四类各产出。注意 stub 按文件名打分——变体命名保留原 stem,报告需注明"stub 对视觉扰动免疫是桩特性,生产模型会体现真实退化"。
- **A56 复核台 v2**:`webui/dashboard.py`(独立入口,不动 A30 的 app.py):纯逻辑层 `trend_rows(entries) -> list`(按日期聚合 verdict 计数)、`agreement_matrix(image_scores) -> dict`(模型两两一致率)、`graph_rows(export: dict) -> list`(图谱表格化)、`policy_preview(report, rules) -> str`;Streamlit 层(惰性):统计图表(st.bar_chart)、模型一致性热力表、关联站点表、政策模拟器、四眼状态面板。tests/test_dashboard_helpers.py 只测纯函数(streamlit 缺失不受影响)。
- **A57 复核 TUI**:`netsentinel/cli/review_tui.py`:纯标准库交互式终端(无 curses):ANSI 颜色徽章(🔴🟡🟢)、命令 `list/approve <id> --reviewer 名字/reject <id> --note/quit`、批准前显示摘要+证据路径+人工确认问题"我已人工核实(Y/N)";组合 FourEyesQueue(required=cfg.four_eyes_required);`main(argv, stdin=None, stdout=None)` 可注入 IO 便于测试;`python -m netsentinel.cli.review_tui`。测试:注入 stdin 脚本化全流程(list→approve→二次同人拒绝→第二人通过)。
- **A58 扫描池**:`netsentinel/ops/pool.py`:`run_pool(cfg, items: list[str], *, run_scan=None, workers=2, sleep=time.sleep) -> dict summary`:concurrent.futures.ThreadPoolExecutor(可注入 executor;进程版注明 Windows spawn 注意事项放 docstring);全局礼貌间隔(per-item sleep 1+rand);site_memory 去重(注入 memory);单项失败继续;返回 {"done","failed","skipped","results": {url: verdict}};绝不扩大扫描范围(只扫 items)。测试:注入 fake run_scan+fake sleep,断言并发上限、失败隔离、汇总正确。
- **A59 自适应重扫**:`netsentinel/ops/adaptive.py`:`volatility(history: list[str 指纹]) -> float`(变化频率 0~1);`suggest_interval_hours(history, base_h) -> int`:波动高(≥0.5)→base/2,低(≤0.1 且有≥3 期)→base*2,其余 base;上下限 [6, 720];`next_run(schedule: list[datetime], interval_h) -> datetime`。纯函数。测试:各波动档、边界钳制、历史不足返回 base。
- **A60 文档**:docs/AGENT_GUIDE.md(案件智能体/级联/主动学习:定位、配置、提示词与预算关系、误用警示)、docs/PLATFORM.md(四眼/政策/图谱/池化/TUI/复核台v2 平台化叙事与运维)、docs/UPGRADE_V3.md(A41–A60 总览表+新数据流 mermaid+三轮演进 v1规则→v2感知→v3智能体)、docs/README_V3.md(面向新读者的 V3 能力速览与快速上手)。基于契约与代码,不臆造。

## 4. 接线(项目负责人集成期完成,代理勿改 orchestrator)

- `classifier: cascade` 即启用级联;`four_eyes_required: true` 时 CLI/TUI 走 FourEyesQueue;
- run_scan 不变(稳定层),V3 智能体编排经 `netsentinel.agent.case_flow.run_case` 入口;
- conformal/active_learn 产出以"建议"形式呈现(benchmarks 与复核台),不自动改生产阈值。

## 5. 测试规则:同 v1/v2(离线/mock/importorskip/不碰真实门户与真实 API/VLM)。
