# NetSentinel V8 团队契约(A143–A162 并行开发)—— 视觉模型自动接管 · 连接向导 · 手动切换

> 七轮后全仓 3321 测试全绿。并行会话已在 contracts.py 预置 4 个 V8 字段
> (plugin_supervised_api / onboarding_auto_open / agents_max_workers / agent_task_db),
> **一律保留不动**;本契约字段在其后追加,均已登记 config。

## 0. V8 新红线(32–34,累计 34 条)

32. **本地探测仅限回环**:自动接管只探测 127.0.0.0/8 上 cfg.local_probe_ports 指定端口的 OpenAI 兼容 `/v1/models`;绝不扫外网、绝不扫回环全端口段;探测动作仅发生在"首次初始化(takeover_once)"与"用户显式点击/命令"时。
33. **密钥只进不显**:向导/切换界面的密钥输入为 password 语义(任何回显/日志一律掩码,前 4 位+****);落盘仅经 `security.keys.set_key`;REST/页面响应绝不包含密钥本体。
34. **接管不放宽既有纪律**:云端提供方仍受 vlm_online+密钥双条件(红线 16)与 VLM 预算(红线 19);本地提供方豁免照旧;无任何模型时的 stub 兜底必须在界面/输出中**明示"离线桩,非模型判定"**。

## 1. 本轮字段(已落地)

`takeover_auto(True) / local_probe_ports(["11434","1234","8000","9997"]) / setup_port(8766) / model_runtime_path(data/model_runtime.json)`;弹窗开关沿用并行会话的 `onboarding_auto_open`。

## 2. 核心概念

- **自动接管(takeover)**:首次运行任意 CLI 入口 → `pipeline.takeover.takeover_once(cfg)`:①探测本地视觉服务 ②有→`ModelManager.set_active("ollama:llava" 等)` ③无本地但云密钥已配→设云 ④全无→触发连接向导(弹浏览器,`NETSENTINEL_NO_BROWSER=1` 或 onboarding_auto_open=False 时不弹只打印 URL)。幂等(会话内一次;持久化状态防重复打扰)。
- **活动模型(model_runtime.json)**:`{"spec": "ollama:llava", "switched_at": ..., "switched_by": "takeover|wizard|cli|rest"}`;`ModelManager.apply(cfg)` 把 spec 写入 cfg.classifier(不动 ensemble_members,除非 spec 为 "stub")。
- **连接向导(setup)**:纯标准库自托管(不依赖 streamlit/fastapi)——`http://127.0.0.1:{setup_port}/` 中文单页:状态卡(当前活动模型/未连接警示)→【扫描本机服务】→本地模型列表(逐项【启用】)→云平台下拉(目录 20 家)+密钥(password)+【测试连接】→【使用离线桩继续】(红字声明);已有模型时页面顶部即"切换模型"横幅(同套操作=手动切换)。
- **手动切换三入口**:向导页 / CLI `python -m netsentinel.modelmgr` / REST(挂 service)。

## 3. 文件归属(A143–A162;兄弟模块只读,惰性导入)

| 组 | 新文件 | API 要点 | 测试 |
| --- | --- | --- | --- |
| A143 | vision/local_probe.py | `LocalVisionScanner(ports, timeout=1.5)`:`scan() -> [{"provider","base_url","models":[...],"ok"}]`;GET `{base}/v1/models`(urllib,仅 127/8 host,红线 32);provider 判定=端口映射目录(11434→ollama,1234→lmstudio,8000→vllm,9997→xinference,其余→"openai 兼容");vision 模型过滤委托 A150(惰性,缺席全返回并标 unfiltered) | tests/test_local_probe.py |
| A144 | vision/model_manager.py | `ModelManager(path)`:get_active()->spec|None;`set_active(spec, *, switched_by, validate=True)`(providers.parse_spec 校验;validate 时惰性调 A147 测试,失败仍可强制?否——失败抛中文 ValueError);`clear()`;`apply(cfg)`(写 cfg.classifier=spec;spec=="stub" 时同时置 ensemble_members=["stub"]);`status()`含来源与时间;线程安全;损坏 json 重建 | tests/test_model_manager.py |
| A145 | setup/server.py(包 netsentinel/setup/) | 纯 stdlib `ThreadingHTTPServer`:GET `/`→A146 页面(A157 渲染注入状态);POST `/api/probe`(触发 A143 扫描) `/api/activate {"spec"}`(A144 set_active+返回状态) `/api/setkey {"provider","key"}`(A152,成功仅回 {"ok":true,"masked":"sk12****"}) `/api/test {"spec" 或 provider+key?}`(A147) `/api/status`;CORS 不需要;仅绑 127.0.0.1;JSON 错误中文;`serve(port, *, open_browser=False)` 线程启动/停 | tests/test_setup_server.py |
| A146 | setup/page.py | 单文件 HTML(内联 CSS/JS,全中文):元素含 data-testid(状态卡/扫描按钮/本地列表容器/云下拉/密钥输入 type=password/测试连接按钮/启用按钮模板/离线桩按钮/切换横幅/警示文案含"离线桩,非模型判定");JS fetch 上述 API(无框架);`page_html() -> str` 纯函数 | tests/test_setup_page.py |
| A147 | vision/connectivity.py | `test_connection(spec, cfg, *, transport=None, spend=None) -> {"ok","latency_ms","model","error?"}`:本地提供方→直接探测 /v1/models 含该模型名;云端→**仅当 vlm_online+key 时**发 1x1 png 评分 ping(预算 spend,缺省惰性 vlm_cache,红线 34);VlmConfigError 转中文 error 不抛 | tests/test_connectivity.py |
| A148 | modelmgr.py(包根,`python -m netsentinel.modelmgr`) | 子命令 status/probe/list(本地扫描结果表)/switch SPEC(--force 跳过连通性测试)/test SPEC/takeover(手动触发 A158)/serve(起向导);中文输出;退出码 0/1/2 | tests/test_modelmgr.py |
| A149 | setup/trigger.py | `has_any_model(cfg) -> bool`(活动 spec 存在 或 任一云密钥已配(keys.configured)或本地扫描命中);`ensure_setup(cfg, *, open_browser=None) -> str|None`:需要时启动 A145(线程,幂等单例)+webbrowser.open(env NETSENTINEL_NO_BROWSER 或 onboarding_auto_open=False→仅打印 URL);返回地址 | tests/test_trigger.py |
| A150 | vision/capability.py | `is_vision_model(name) -> bool`:模式表(llava/llama.*vision|vl/qwen.*vl|minicpm-v|moondream|gemma.*(vl|vision)|cogvlm|internvl|phi.*vision|bakllava|deepseek-vl|idefics|claude|gpt-4o|gemini|glm.*(v|vision)|step-1v|doubao.*vision|hunyuan-vision…≥18 条正则,大小写不敏感);`filter_vision(models) -> list`;`VISION_PATTERNS` 导出供目录复用 | tests/test_capability.py |
| A151 | service/model_api.py | `create_model_router() -> "APIRouter"`(fastapi 惰性 importorskip):GET /model/active、POST /model/switch {"spec"}(经 A144,响应含 masked 状态)、POST /model/probe、POST /model/test {"spec"};密钥端点**不提供**(写密钥仅向导/CLI,降攻击面);错误中文 | tests/test_model_api.py |
| A152 | security/key_api.py | `accept_key_input(provider, key, *, cfg_dir=None) -> {"ok","masked","stored_path"}`:校验非空/长度≥8/去空白;调 security.keys.set_key;**绝不记录 key 本体**(日志只 masked);`masked(provider) -> str|None`(已配时前4+****) | tests/test_key_api.py |
| A153 | setup/flow.py | `SetupState` 状态机:no_model→{local_connected, cloud_connected, stub_only};`next_step(state) -> 中文提示`;`persist/load(setup_state.json)`;`recommend(scanner_results, cfg) -> {"action","spec?"}`(本地优先→云→stub) | tests/test_setup_flow.py |
| A154 | setup/daemon.py | `ensure_setup_server(cfg, *, open_browser=None) -> url|None` 单例(锁文件 data/.setup.lock + 端口探测存活检查,已存活直接复用);`stop_setup_server()`;与 A149 区别:daemon 面向常驻(向导页即切换器),A149 只首启;测试注入端口 | tests/test_setup_daemon.py |
| A155 | pipeline/takeover.py | `takeover_once(cfg, *, scanner=None, manager=None, ensure=None) -> {"action","spec?","wizard?","detail"}`:takeover_auto=False→{"action":"disabled"};会话幂等(模块级 set);探测→本地首个视觉模型 set_active(switched_by="takeover")→云(已配密钥的目录家,取目录默认)→全无 ensure_setup;审计 log_event("takeover",...)惰性;返回中文 detail | tests/test_takeover.py |
| A156 | setup/render.py | `render_page(status: dict, active: str|None, local_models: list) -> str`:A146 模板注入(全插值 html.escape;JSON 状态经 `<script>` 注入时 json.dumps+`</script>` 转义);坏输入容错 | tests/test_setup_render.py |
| A157 | tests/test_v8_e2e.py | 端到端:①mock 本地 /v1/models(含 llava)→takeover_once 自动设为活动→ModelManager.apply(cfg)→get_classifier(cfg.classifier) 可构建;②无任何模型→ensure_setup 起服务器(NO_BROWSER)→/api/status 200→/api/activate 切换生效;③/页面 HTML 含"离线桩,非模型判定"与 password 输入(红线 33/34 断言);④setkey 后 status 含 masked 不含本体 | (单文件) |
| A158 | scripts/demo_model_wizard.py | 离线演示:起 mock 本地服务(含 llava+qwen2.5vl 两个视觉模型与 1 个文本模型)→takeover 自动接管打印→向导 API 全流程(状态/扫描/启用/切到 qwen2.5vl/切 stub/警示)→"全程零外呼(仅本机回环)、密钥只进不显"收尾;NO_BROWSER | 实跑验证 |
| A159 | docs/SETUP_GUIDE.md | 安装后接管与向导权威指南:红线 32-34;首次运行会发生什么(时序图);向导页面逐区说明;三入口切换对照表;端口/防火墙说明;故障排查(端口被占/服务未启/密钥无效);与并行会话 plugin_supervised_api 字段的协同说明(占位,不臆造) | — |
| A160 | docs/MODEL_SWITCH.md | 切换手册:活动模型机制(model_runtime.json)/spec 语法/一键命令/REST/页面;切换对进行中批次的影响(下一条生效)/回退 stub;能力判定表(哪些模型名会被识别为视觉) | — |
| A161 | webui/models_page.py | 复核台"模型"页(惰性 streamlit,纯逻辑层可测):`model_rows(local_scan, active) -> list[dict]`(含"视觉"判定列/启用按钮数据)、`switch_banner(active) -> str`、`key_status(cfg) -> dict`(布尔+masked);UI 层调 A144/A143,**不含密钥输入**(指向向导) | tests/test_models_page.py |
| A162 | docs/UPGRADE_V8.md | V8 总览:A143–A162 一览、接管/向导/切换 mermaid(首启→takeover→{本地/云/向导}→model_runtime→apply→扫描入口;向导常驻=切换器;三入口)、与并行会话 V8 字段共存说明、九轮演进表、配置速查、快速上手 | — |

冻结:contracts.py、telemetry.py、conftest.py、全部既有模块与文档、并行会话的 4 个 V8 字段。

## 4. 集成接线(负责人完成,代理勿改)

- `netsentinel/__main__.py` 与 `batchflow.py` 入口首行插入 `takeover_once(cfg)`(惰性,异常只告警不阻断);
- service/app.py 挂载 A151 router(一行 create_model_router)。

## 5. 流程:契约→实现→test_v8_*(含红线 32-34 专项断言)→全仓回归全绿(终跑时间点注明)→报告。
