# 净网哨兵 模型切换手册(MODEL_SWITCH)

本文是 V8「视觉模型自动接管 · 连接向导 · 手动切换」的**切换手册**(契约 A160):讲清"活动模型"这一份持久化状态如何驱动全系统的模型选择——它存在哪、谁写入、怎么切换(向导页 / CLI / REST 三入口)、切换前默认做怎样的连通性测试、对进行中的任务何时生效、离线桩 stub 的整套替换语义,以及"哪些模型名会被认成视觉模型"的能力判定表。

全部行为以 `CONTRACTS-V8.md`(§0 红线 32–34、§2 核心概念、§3 A143–A156)与实际代码为准:

| 模块 | 职责 |
| --- | --- |
| `netsentinel/vision/model_manager.py`(A144) | `model_runtime.json` 的读写 / 校验 / 热切换(`ModelManager`) |
| `netsentinel/vision/connectivity.py`(A147) | 连通性测试唯一入口 `test_connection`(本地清点 / 云端 1x1 ping) |
| `netsentinel/modelmgr.py`(A148) | 手动切换 CLI 入口(status/probe/list/switch/test/takeover/serve) |
| `netsentinel/setup/server.py`(A145) | 连接向导自托管服务(向导页即手动切换器) |
| `netsentinel/service/model_api.py`(A151) | REST 入口(`/model/*` 路由) |
| `netsentinel/vision/capability.py`(A150) | 模型名视觉能力判定(`VISION_PATTERNS`,本文 §6 的权威来源) |
| `netsentinel/vision/providers.py`(A61) | 20 家提供方目录与 `提供方:模型` 语法解析 |
| `netsentinel/pipeline/takeover.py`(A155) | 首次运行自动接管 `takeover_once` |

本文不引入契约之外的承诺。相关文档:[MULTI_PROVIDER.md](MULTI_PROVIDER.md)(classifier 写法与跨平台接入)、[PROVIDERS.md](PROVIDERS.md)(20 家提供方总览)、[USAGE.md](USAGE.md)(日常使用)。

---

## 1. 活动模型机制:model_runtime.json

V8 起,"当前用哪个视觉模型"不再散落在各处配置里,而是收敛为**单一状态文件** `data/model_runtime.json`(路径由 `cfg.model_runtime_path` 决定,默认 `data/model_runtime.json`)。所有切换入口最终都落到同一个写入方:`ModelManager.set_active()`。

### 1.1 文件结构

已连接(任一入口切换成功)时,文件内容为三键:

```json
{
  "spec": "ollama:llava",
  "switched_at": "2026-10-02T10:30:00+08:00",
  "switched_by": "cli"
}
```

| 键 | 含义 | 取值 |
| --- | --- | --- |
| `spec` | 活动模型规格 | `提供方:模型`(如 `ollama:llava`、`glm:glm-4.5v`)、单独提供方名(如 `glm`,模型按目录默认解析)、或 `stub`(离线桩) |
| `switched_at` | 切换时间 | 本地时区 ISO8601 秒级时间戳(`contracts.now_iso()`) |
| `switched_by` | 来源 | 契约登记四值:**`takeover`**(自动接管)/ **`wizard`**(连接向导页)/ **`cli`**(modelmgr 命令行)/ **`rest`**(REST 接口) |

未连接时文件为空对象 `{}`(或文件不存在):`get_active()` 返回 `None`,`status()` 返回 `{"spec": null}`。代码内还有一个空值兜底默认 `manual`——仅当记录里 `switched_by` 字段缺失或为空白时用于展示,不属于四个登记来源。

### 1.2 写入纪律:原子 + 线程安全

`ModelManager._write()`(调用方持实例级 `RLock`):

1. 同目录 `tempfile.mkstemp()` 建临时文件 → `json.dump` → `flush` → `fsync`;
2. `os.replace(tmp, target)` 原子替换——任何时刻读者要么看到完整旧文件、要么看到完整新文件,**绝不会读到半截 JSON**;
3. 任一步失败即删除临时文件并抛出,原文件保持原状。

读取侧的自愈:文件缺失按空状态处理;文件损坏(非法 JSON / 非 dict)时**自动重建为空对象并视为"未连接"**,只记 WARNING,绝不向调用方抛出。

### 1.3 三条读 API

- `get_active() -> str | None`:仅取 spec(去空白;空/未连接 → `None`);
- `status() -> dict`:未连接 `{"spec": None}`;已连接返回 `{"spec","switched_by","switched_at"}` 三键快照;
- `apply(cfg) -> Config`:把活动 spec 应用到 Config(**原地修改并返回同一对象**)——无活动模型时不做任何修改(由向导/takeover 兜底);有则 `cfg.classifier = spec`;spec 为 `stub` 时**同时** `cfg.ensemble_members = ["stub"]`(见 §5)。

---

## 2. spec 语法与切换三入口

### 2.1 spec 语法速查:`提供方[:模型]`

语法由 `providers.parse_spec()` 解析,规则只有四条:

1. **按第一个冒号拆分**:`"openai:gpt-4o-mini" → ("openai", "gpt-4o-mini")`;模型名内部再含冒号不受影响(openrouter 的 `qwen/qwen2.5-vl-72b-instruct:free` 尾部 `:free` 完整保留);
2. **无冒号**(如 `glm`)→ 只写提供方,模型留给 `providers.resolve` 按三级优先级解析(当场模型 > `cfg.vlm_provider_models[提供方]` > 目录默认);
3. **冒号后为空**(如 `ollama:`)视为未指定模型;
4. **未知提供方 → 中文 ValueError**,消息列出全部 20 个可用提供方与正确写法。

### 2.2 提供方目录(20 家,`providers.PROVIDERS`)

目录中的模型名/端点均为**提示值,以各平台官方文档为准**;全部可用 `cfg.vlm_provider_models` / `vlm_provider_base_urls` 覆盖。写法示例(仅 `ollama` 有本地默认模型;`vllm`/`lmstudio`/`xinference` 必须显式指定模型):

| 提供方 | 示例 spec | 默认模型 | 本地 | 备注 |
| --- | --- | --- | --- | --- |
| `glm` | `glm:glm-4.5v` | glm-5.3-flash | — | 智谱 GLM,OpenAI 兼容口,项目默认提供方 |
| `openai` | `openai:gpt-4o-mini` | gpt-4o-mini | — | GPT-4o 系列,分钟级限速需留意 |
| `anthropic` | `anthropic:claude-sonnet-4` | claude-sonnet-4 | — | anthropic 方言(system 顶层、max_tokens 必填) |
| `gemini` | `gemini:gemini-2.0-flash` | gemini-2.0-flash | — | gemini 方言(x-goog-api-key 头、inline_data 传图) |
| `qwen` | `qwen:qwen-vl-max` | qwen-vl-max | — | 通义千问 VL,DashScope 兼容模式 |
| `doubao` | `doubao:doubao-1.5-vision-pro` | doubao-1.5-vision-pro | — | 字节豆包(火山方舟),也可填推理接入点 ID |
| `hunyuan` | `hunyuan:hunyuan-vision` | hunyuan-vision | — | 腾讯混元视觉 |
| `moonshot` | `moonshot:kimi-latest` | kimi-latest | — | 月之暗面 Kimi,长上下文强 |
| `minimax` | `minimax:MiniMax-VL-01` | MiniMax-VL-01 | — | MiniMax 视觉模型 |
| `stepfun` | `stepfun:step-1v-8k` | step-1v-8k | — | 阶跃星辰 Step-1V |
| `siliconflow` | `siliconflow:Qwen/Qwen2.5-VL-7B-Instruct` | Qwen/Qwen2.5-VL-7B-Instruct | — | 硅基流动聚合平台,模型名带命名空间 |
| `ernie` | `ernie:ernie-4.5-vl` | ernie-4.5-vl | — | 百度文心 ERNIE VL(千帆 v2) |
| `openrouter` | `openrouter:qwen/qwen2.5-vl-72b-instruct:free` | 同左 | — | 聚合入口,免配置换平台 |
| `groq` | `groq:meta-llama/llama-4-scout-17b-16e-instruct` | 同左 | — | 超低延迟,速率配额较小 |
| `together` | `together:meta-llama/Llama-4-Scout-17B-16E-Instruct` | 同左 | — | 开源模型托管,按量计费 |
| `xai` | `xai:grok-2-vision-1212` | grok-2-vision-1212 | — | xAI Grok 视觉 |
| `ollama` | `ollama:llava` | llava | ✔(127.0.0.1:11434) | 本地 Ollama,数据不出本机 |
| `vllm` | `vllm:<启动加载的模型名>` | (无,必须显式) | ✔(127.0.0.1:8000) | 本地 vLLM,模型随启动参数而定 |
| `lmstudio` | `lmstudio:<手动加载的模型名>` | (无,必须显式) | ✔(127.0.0.1:1234) | 本地 LM Studio 桌面端 |
| `xinference` | `xinference:<自行部署的模型名>` | (无,必须显式) | ✔(127.0.0.1:9997) | 本地 Xinference |

本地提供方(后四家,`local=True`)免 `vlm_online` 闸门与密钥,但模型名缺失且目录无默认时,`resolve` 直接报中文错「本地提供方必须指定模型,如 ollama:llava」。

**`stub` 不走这套语法**:它不是目录条目,`set_active("stub")` 跳过 `parse_spec` 与连通性测试,任何环境下永远可用(见 §5)。

### 2.3 切换三入口命令对照

三个入口殊途同归——最终都是 `ModelManager.set_active(spec, switched_by=…, validate=…)`,差别只在来源标记与失败形态:

| | ① 连接向导页 | ② CLI(modelmgr) | ③ REST |
| --- | --- | --- | --- |
| 打开方式 | `python -m netsentinel.modelmgr serve [--port N]` → 浏览器访问 `http://127.0.0.1:8766/`(端口取 `cfg.setup_port`,缺省 8766;`NETSENTINEL_NO_BROWSER=1` 不自动弹浏览器) | `python -m netsentinel.modelmgr <子命令>` | `service/app.py` 挂载 `create_model_router(cfg)` 后 `POST /model/switch` |
| 切换命令 | 页面【启用】按钮 → `POST /api/activate {"spec": "ollama:llava"}` | `python -m netsentinel.modelmgr switch ollama:llava`(加 `--force` 跳过连通性测试) | `curl -X POST .../model/switch -d '{"spec":"ollama:llava"}'` |
| `switched_by` | `wizard` | `cli` | `rest`(固定,不接受请求体指定来源) |
| 默认校验 | 语法 + 连通性测试(不过即拒绝) | 同左;`--force` = `validate=False` | 同左(REST 无 force 旁路) |
| 失败形态 | HTTP 400 + 中文 `{"error": …}` | stderr 中文报错,退出码 1(用法错误 2) | HTTP 409(校验/连通不通过)、422(缺 spec)、503(依赖未就位)、500(内部) |
| 配套动作 | `POST /api/status`(状态卡)/ `POST /api/probe`(本机扫描)/ `POST /api/setkey`(密钥,password 语义)/ `POST /api/test` | `status` / `probe` / `list [provider]` / `test [SPEC]` / `takeover` / `serve` | `GET /model/active` / `POST /model/probe` / `POST /model/test` |
| 密钥输入 | ✔(向导独有,password 输入,响应只回掩码) | ✘(CLI 不提供任何密钥输入,红线 33) | ✘(**REST 绝不提供写密钥端点**,降攻击面,红线 33) |

向导服务为纯标准库 `http.server`,只绑定 `127.0.0.1`(红线 32),不依赖 fastapi/streamlit;已有活动模型时,向导页顶部即"切换模型"横幅——同一套操作就是手动切换。

---

## 3. validate 语义:切换前的连通性测试

`set_active(spec, *, switched_by, validate=True)`:语法校验(`parse_spec`)**永远执行**(连 `--force` 也不放过,防手误写坏状态文件);连通性测试仅当 `validate=True` 时执行,**测试不过即拒绝切换**(抛中文 ValueError 含原因,`model_runtime.json` 保持原状)。`--force` = `validate=False`:跳过连通性直接落盘,CLI 会提示"建议随后手动 test 复核一次"。

测试本体是 A147 `connectivity.test_connection(spec, cfg)`(`modelmgr test` 与向导/REST 的"测试连接"也直接调它),返回 `{"ok", "latency_ms", "model", "mode", "error"?}`,**任何分支都不抛出**。本地与云端两条路径纪律不同:

### 3.1 本地提供方:一次 /models 清点,零评分外呼

对 `resolved.local=True` 的四家(ollama / vllm / lmstudio / xinference):

- 只发**一次** `GET {base_url}/models` 模型清点请求,固定超时 **3.0 秒**(`LOCAL_TIMEOUT_S`,刻意不读 `cfg.discovery_query_delay_s`——那是发现层的引擎限速,与本探测无关);
- 响应 `data[].id` 含目标模型名(**忽略大小写**)即通过,返回 `{"ok": true, "mode": "local", "latency_ms": …}`;模型不在线时中文报错并列出当前可用模型(前 10 个);
- **零预算记账、零评分外呼**(红线 32:本地探测只清点模型,不发评分请求、不消耗 `vlm_daily_budget`);
- 端点仅限回环(`127.0.0.1` / `localhost` / `::1`):`vlm_provider_base_urls` 覆盖到非回环地址时**直接拒绝、零网络**;不跟随重定向,响应体上限 1 MiB。

### 3.2 云端提供方:双条件闸门 + 预算记账 + 1x1 ping

云端测试叠加既有红线 16 / 19 / 34:

1. **双条件闸门**:`cfg.vlm_online=True`(**且**)已解析到 API 密钥,缺一即中文报错且**零外呼**(transport 调用次数为 0)。`vlm_online` 默认 `False`——图像数据不出本机;
2. **先记账、后外呼**(红线 19):外呼前先记一笔预算(缺省惰性 `vlm_cache.VlmCache(...).spend_one()`,预算缺省 `vlm_daily_budget=200` 次/日);预算尽或记账失败 → 中文说明并取消外呼(宁可误杀,不可漏账);
3. 满足后才发**一次 1x1 PNG 评分 ping**(纯标准库手工生成的灰色 1x1 图,经 A62 `UniversalVLMClient` 发出,与生产链路完全同构),通过返回 `{"ok": true, "mode": "cloud", …}`。评分内容本身不进结论——连通性只看链路是否可用;
4. 一切失败(`VlmConfigError` / `ModelNotFoundError` / 任何异常)转中文 `error` 字段,**错误消息绝不包含密钥本体**(红线 17)。

---

## 4. 切换对进行中任务的影响

- **下一条生效**。切换只写 `model_runtime.json`,不通知、不中断任何运行中的进程:进行中的扫描/批次继续使用其启动时经 `ModelManager.apply(cfg)` 写入 `cfg.classifier` 的模型;新启动的扫描(下一条任务)读到新 spec 才切换过去。CLI 切换成功的收尾提示即"已切换:{spec}(下一条扫描生效)"。
- **扫描入口经 `takeover_once` 会话内沿用**。按契约 §2/§4,CLI 入口(`netsentinel/__main__.py` 与 `batchflow.py`)在进程首行调用 `pipeline.takeover.takeover_once(cfg)`(惰性导入,异常只告警不阻断)。`takeover_once` 有两条与会话相关的纪律:
  - **会话幂等**:模块级标记,同进程二次调用直接返回 `{"action": "already"}`,零副作用(不重探、不写审计、不计数)——一个会话只打扰一次;
  - **kept 沿用**:`model_runtime.json` 里已有活动 spec 时,第一级决策即"沿用已连接模型,不重探"(零网络、零打扰),不会覆盖你在别处刚切好的模型。
  
  也就是说:会话启动时 `takeover_once` 把活动 spec 确认/接管妥当,会话内的任务沿用该模型;期间其他入口(向导/CLI/REST)切换落盘,本会话不被动跟随,下一条任务生效。接管阶段本身**零外呼、零连通测试**(本地路径 `validate=False`——模型刚在扫描中被证实存在,红线 32;云端路径只写提供方名,`vlm_online`/预算纪律在真正调用时才生效,红线 34)。
  
  > 现状说明:契约 §4 的入口接线由负责人统一合入;截至本文撰写,`__main__.py` / `batchflow.py` 尚未插入该调用,当前可用 `python -m netsentinel.modelmgr takeover` 手动触发,语义同上。

---

## 5. 回退 stub:整套替换语义与红字警示

全无可用模型时,离线桩 `stub` 是保底出口——`stub` **不是**提供方目录条目,因此:

- **免校验**:`set_active("stub")` 跳过 `parse_spec` 语法校验与连通性测试,任何环境下永远可用;
- **整套替换**:`ModelManager.apply(cfg)` 对 `stub` 做两件事——`cfg.classifier = "stub"` **且同时** `cfg.ensemble_members = ["stub"]`(红线 34)。这是刻意设计:若只换 classifier 而留下集成成员,ensemble 里残留的云端成员仍会真实外呼,违背"离线兜底"的承诺。反过来,**其余一切 spec 都不动 `ensemble_members`**——切换到真实模型时不碰你的集成配置。
- **红字警示**(红线 34:"stub 兜底必须明示'离线桩,非模型判定'"):
  - 向导页【使用离线桩继续】按钮旁固定红字 `离线桩,非模型判定`(`STUB_NOTE_TEXT`),配提示"没有任何可用模型时的兜底:流程可继续,但结论不来自视觉模型,产出会明确标注离线桩来源";启用后警示区再次明示;
  - CLI:`switch stub` 显式打印"注意:stub 为离线桩,非模型判定(红线 34)";`status` 对 stub 活动模型追加同样标注;且 stub 免校验,CLI 不冒称"测试通过";
  - REST:`GET /model/active` 原样返回 `spec: "stub"`(三键白名单出口),由消费方按同一口径标注。

想退出离线桩:任一入口正常切换到真实模型即可(`apply` 会恢复 `classifier`,但注意 `ensemble_members` 只在切到 stub 时被改写,此前若被置为 `["stub"]`,需在配置里自行恢复原集成成员)。

---

## 6. 视觉模型名识别表(VISION_PATTERNS 全列)

本地扫描(A143)过滤、复核台"模型"页(A161)"视觉"列与本文共用同一权威来源:`vision/capability.py` 的 `VISION_PATTERNS`(26 条正则,全部 `re.IGNORECASE` 编译;任一命中即判视觉)。逐条对照(`(?![a-z])` 为边界负向断言,忽略大小写下同时挡大写字母,防"名字里有个 v"误伤):

| # | 正则 | 家族说明 | 正例 → 命中 | 反例 → 不命中 |
| --- | --- | --- | --- | --- |
| 1 | `llava` | LLaVA / BakLLaVA 家族 | `llava:13b`、`llava-1.5-7b-hf`、`bakllava` | `llama3`(无 llava 子串) |
| 2 | `llama[\w.-]*(?:vision\|vl)(?![a-z])` | Llama 3.2 Vision 系列 | `llama3.2-vision`、`Llama-3.2-11B-Vision-Instruct` | `llama3`(纯文本) |
| 3 | `qwen[\w.-]*(?:vl\|vision)(?![a-z])` | Qwen-VL 系列 | `qwen-vl-max`、`qwen2.5vl:7b`、`Qwen/Qwen2.5-VL-72B-Instruct` | `qwen2.5-72b-instruct`(无 vl/vision 标记) |
| 4 | `minicpm[\w.-]*-v(?![a-z])` | MiniCPM-V 系列(要求连字符接 v) | `minicpm-v`、`MiniCPM-V-2_6`、`minicpm-v:8b` | `minicpm3-4b`(无 "-v") |
| 5 | `moondream` | Moondream 小型 VLM | `moondream`、`moondream2` | `mistral-7b` |
| 6 | `gemma[\w.-]*-(?:vl\|vision)(?![a-z])` | Gemma 视觉变体(要求连字符显式接 vl/vision) | `gemma-3-4b-vl` | `gemma-7b`(纯文本) |
| 7 | `cogvlm` | CogVLM(图像生成的 cogview 不收:生成 ≠ 视觉判定) | `cogvlm`、`cogvlm2-19b` | `cogview-3` |
| 8 | `internvl` | InternVL(文本 internlm 不含 internvl) | `internvl`、`OpenGVLab/InternVL2_5-8B` | `internlm2` |
| 9 | `phi[\w.-]*(?:vision\|multimodal)(?![a-z])` | Phi-3/3.5/4 视觉与多模态 | `phi-3-vision`、`phi-3.5-vision-instruct`、`phi-4-multimodal-instruct` | `phi-3-mini`(纯文本) |
| 10 | `deepseek-vl` | DeepSeek-VL(r1/chat 等纯文本不收) | `deepseek-vl`、`deepseek-vl2`、`deepseek-vl-chat:7b` | `deepseek-r1`、`deepseek-chat` |
| 11 | `idefics` | IDEFICS | `idefics-9b-instruct`、`idefics2-8b` | `falcon-7b` |
| 12 | `gpt-4o(?![a-z])\|gpt-4[\w.-]*vision(?![a-z])\|gpt-4\.1(?![a-z0-9])` | OpenAI GPT-4o / GPT-4 vision / GPT-4.1 | `gpt-4o`、`gpt-4o-mini`、`gpt-4-vision-preview`、`gpt-4.1`、`gpt-4.1-mini` | `gpt-3.5`、`gpt-4-32k`(无 vision 字样,宁缺勿滥) |
| 13 | `claude-(?:3\|sonnet\|haiku\|opus)` | Claude 3/4 系(claude-2.x 纯文本) | `claude-3-5-haiku`、`claude-sonnet-4`、`claude-opus-4-5` | `claude-2.1` |
| 14 | `gemini` | Gemini 全系多模态 | `gemini-1.5-pro`、`gemini-2.0-flash` | `palm-2` |
| 15 | `glm-?\d[\d.]*v(?:ision\|l)?(?![a-z])` | GLM 视觉系("glm"+数字后显式接 v;preview 词中的 v 不误伤) | `glm-4v`、`glm-4.5v`、`glm4v:9b`、`glm-4vl` | `glm-3-turbo-preview`(v 在 preview 词中) |
| 16 | `glm[\w.-]*vl(?![a-z])` | GLM …vl 形态兜底(非数字中缀写法) | `glm-4.6vl` | `glm-4-flash` |
| 17 | `step-\d[\w.-]*v(?:ision\|l)?(?![a-z])` | 阶跃 Step-1V/1O 系列 | `step-1v-8k`、`step-1v-32k`、`step-1o-turbo-vision` | `step-1-8k`(纯文本) |
| 18 | `doubao[\w.-]*(?:vision\|vl)(?![a-z])` | 豆包视觉系(词中 v 不误伤) | `doubao-1.5-vision-pro`、`doubao-1.5-vision-lite`、`doubao-1.5-vl` | `doubao-pro-32k`、`doubao-lite-4k`、`doubao-pro-v2` |
| 19 | `hunyuan[\w.-]*vision(?![a-z])` | 混元视觉 | `hunyuan-vision`、`hunyuan-turbo-vision` | `hunyuan-pro` |
| 20 | `minimax[\w.-]*(?:vl\|vision)(?![a-z])` | MiniMax-VL(abab 纯文本系不收) | `MiniMax-VL-01`、`minimax-vl-01` | `abab5.5-chat` |
| 21 | `kimi[\w.-]*vision(?![a-z])` | Kimi 显式 vision 变体 | `kimi-latest-vision`(任何 kimi…vision 形态) | `kimi-latest`(名字无标记 → False,宁缺勿滥) |
| 22 | `moonshot[\w.-]*vision(?![a-z])` | Moonshot 平台 vision 变体 | `moonshot-v1-8k-vision-preview` | `moonshot-v1-8k` |
| 23 | `ernie[\w.-]*(?:vl\|vision)(?![a-z])` | 文心 ERNIE VL | `ernie-4.5-vl`、`ernie-4.5-vl-flash` | `ernie-4.5`(纯文本) |
| 24 | `xcomposer` | InternLM-XComposer(文本 internlm2 不收) | `internlm-xcomposer2-4khd` | `internlm2-7b` |
| 25 | `minigpt` | MiniGPT-4 | `minigpt-4`、`minigpt-v2` | `gpt-j-6b` |
| 26 | `grok[\w.-]*vision(?![a-z])` | Grok 视觉 | `grok-2-vision`、`grok-2-vision-1212` | `grok-beta`(未知不收) |

判定纪律(红线 18 的提示级定位,务必知晓):

- **只看名字形态**:命中 ≠ 该部署真的开了图像输入,更不构成可用性承诺;命名随平台迭代频繁变动,一切以官方文档为准;
- **宁缺勿滥**:仅收录上述家族;名字不带视觉标记的一律 `False`(如 `kimi-latest`、`grok-beta`、`gpt-4-turbo`,哪怕官方说明支持图像);
- `None` / 非字符串 / 空白 / 长度超 200 一律 `False`,绝不抛异常;`filter_vision()` 按去重键 `strip().casefold()` 去重并保持首次出现顺序。

---

## 7. 常见问题(FAQ)

**Q1:切了没生效?**
依次排查:

1. **入口是否经过 takeover / apply**。切换是"下一条生效"(§4):进行中的任务不会中途换模型;且契约规定的扫描入口 `takeover_once` 接线若尚未合入你手上的版本(§4 现状说明),扫描仍读 `config.yaml` 里的 `classifier`——确认代码版本或先用 `modelmgr takeover` / 向导确认链路;
2. **会话内沿用**:`takeover_once` 会话幂等且 kept 沿用——本会话已接管过(`already`)时不会因外部切换而重探;新起一条任务才会读到新 spec;
3. **状态文件是否同一份**:`model_runtime.json` 路径取 `cfg.model_runtime_path`(默认 `data/model_runtime.json`);若 `--config` 指向的配置把该路径改去了别处,你的切换和扫描进程可能各写各读一份;
4. 核实方法:`python -m netsentinel.modelmgr status`(打印 spec/来源/切换时间)或 `GET /model/active`,比对 `switched_by` 与 `switched_at` 是否为刚才那次切换。

**Q2:测试连接报 vlm_online?**
这是云端双条件闸门(§3.2)在正确工作:`vlm_online` 默认 `False`(图像数据不出本机),未开启或密钥未配任一条件不满足都会**零外呼**并中文报错(如"云端提供方未开启外呼:vlm_online=False … 请在配置中开启 vlm_online: true 后重试"/"未配置 API 密钥 … 请经连接向导/密钥命令写入")。处置:确认确需云端外呼后,在 `config.yaml` 开启 `vlm_online: true` 并经向导页(或 `vlm_api_keys` / 对应环境变量)配置密钥,再测。本地提供方(ollama 等)不受该闸门。若报"预算记账未通过,已取消本次云端测试外呼",是当日 `vlm_daily_budget`(默认 200)已用尽——次日恢复或调高预算,这是"先记账、后外呼"的硬纪律。

**Q3:并发切换安全吗(原子性)?**
单实例内:`ModelManager` 持实例级 `RLock`,读-改-写串行;落盘走"同目录临时文件 + fsync + `os.replace`"原子替换,任何读者不会看到半截 JSON。跨进程/跨实例并发时:单次写入仍是原子的(不会出现损坏文件),语义为**最后写入者胜**——两个入口同时切换,以较晚完成 `os.replace` 的为准,`switched_at`/`switched_by` 可用来核对最终赢家。唯一需要留意的角落:若外部工具把该文件写坏(非法 JSON),读取侧会自动重建为空对象并按"未连接"处理(向导/takeover 会重新兜底),这是刻意的自愈,不是丢数据。

---

*本文对应 V8 契约 A160;行为变化请以 `CONTRACTS-V8.md` 与上述模块代码为准同步修订。*
