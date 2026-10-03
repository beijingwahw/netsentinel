# 净网哨兵 NetSentinel · 安装后接管与连接向导指南(SETUP_GUIDE)

> 版本:V8(模块 A143–A162;本文为 A159 交付物)。
> 依据:`CONTRACTS-V8.md` 与实际代码(`pipeline/takeover.py`、`setup/server.py`、
> `setup/trigger.py`、`setup/daemon.py`、`setup/flow.py`、`setup/page.py`、
> `vision/local_probe.py`、`vision/capability.py`、`vision/connectivity.py`、
> `modelmgr.py`、`service/model_api.py`)。
>
> 读者:刚装好 NetSentinel、第一次运行任意 CLI 入口的运维者/使用者。
> 本文回答一个问题:**装完之后,系统会对我的机器做什么,我该在哪几步做选择。**

---

## 一、安全红线(红线 32–34 全文)

V8 新增三条红线(累计 34 条中的第 32–34 条),全文引述如下(出处:
`CONTRACTS-V8.md` §0)。安装后的所有自动行为都受这三条约束。

> **32. 本地探测仅限回环**:自动接管只探测 127.0.0.0/8 上 cfg.local_probe_ports
> 指定端口的 OpenAI 兼容 `/v1/models`;绝不扫外网、绝不扫回环全端口段;
> 探测动作仅发生在"首次初始化(takeover_once)"与"用户显式点击/命令"时。

> **33. 密钥只进不显**:向导/切换界面的密钥输入为 password 语义(任何回显/日志
> 一律掩码,前 4 位+****);落盘仅经 `security.keys.set_key`;REST/页面响应
> 绝不包含密钥本体。

> **34. 接管不放宽既有纪律**:云端提供方仍受 vlm_online+密钥双条件(红线 16)
> 与 VLM 预算(红线 19);本地提供方豁免照旧;无任何模型时的 stub 兜底必须
> 在界面/输出中**明示"离线桩,非模型判定"**。

代码层面的落点(便于核对):

| 红线 | 落点 |
| --- | --- |
| 32 | `vision/local_probe.py`:探测主机恒为 `127.0.0.1`(模块内唯一来源,不接受任何 host 参数);不跟随重定向;单端口超时缺省 1.5s;响应读取上限 1 MiB;端口先验合法(1–65535),非法直接中文报错、零网络 |
| 33 | `setup/page.py`:密钥输入 `type="password"` + `autocomplete="new-password"`,保存后输入框立即清空,界面只显示服务端返回的掩码;`setup/server.py`:`/api/setkey` 只回 `{"ok","masked"}`,出口还有密钥泄漏拦截兜底;`security/key_api.py`:日志只记提供方名与长度;`service/model_api.py`:REST 不提供任何写密钥端点 |
| 34 | `setup/flow.py`:桩态提示统一口径"正在使用离线桩,非模型判定,结果仅供参考";`modelmgr.py`:spec 为 `stub` 时输出显式标注"离线桩,非模型判定";`vision/connectivity.py`:云端测试仅在 vlm_online=True 且密钥已配时外呼,且先记一笔预算 |

---

## 二、安装后会发生什么(首启时序)

### 2.1 触发点

按 `CONTRACTS-V8.md` §4 的集成接线约定,任意 CLI 入口(`python -m netsentinel`
及其 `netsentinel` 脚本入口、`netsentinel.batchflow`)在首次运行时都会**惰性**
调用一次 `pipeline.takeover.takeover_once(cfg)`;任何异常只告警、绝不阻断主流程。
你也可以随时手动触发:`python -m netsentinel.modelmgr takeover`。

### 2.2 takeover_once 的四级决策

`takeover_once(cfg)` 按以下优先级把"当前可用的视觉模型"接为活动模型
(返回值恒为 dict,`detail` 恒中文,绝不抛出):

1. **kept(已有活动 spec)**:读 `data/model_runtime.json`
   (`ModelManager.get_active()`)非空 → 直接沿用已连接模型,**零网络、
   零打扰**,不再重新探测。
2. **local(本地视觉服务)**:`LocalVisionScanner` 扫描 `cfg.local_probe_ports`
   (缺省 `["11434","1234","8000","9997"]`,仅 127.0.0.1 回环)。任一在线
   条目含视觉模型(按 `vision/capability.py` 的模式表判定)→ 取首个视觉模型
   写入 `set_active("ollama:llava:13b" 之类, switched_by="takeover", validate=False)`。
   validate=False 的理由即红线 32:服务与模型刚在扫描中被证实存在,再做一次
   连通测试属于重复探测。
3. **cloud(云端密钥已配)**:`security.keys.configured(cfg)` 任一提供方为
   True → 取目录顺序首家 `set_active(提供方名, switched_by="takeover",
   validate=False)`。spec 只写提供方名,模型由 `providers.resolve` 按目录默认值
   解析。**接管阶段零外呼**——vlm_online/密钥/预算纪律在真正调用模型时才生效
   (红线 34)。
4. **wizard(全无)**:以上全无 → 惰性 `setup.trigger.ensure_setup(cfg)` 拉起
   连接向导(详见 2.3),返回 `{"action":"wizard", "wizard": url|None}`。
   向导未能启动时 detail 会给出手动入口:`python -m netsentinel.modelmgr serve`。

另有两种结果:`disabled`(配置 `takeover_auto: false`,零探测零副作用)与
`error`(兄弟模块缺席/执行异常,中文原因,不抛出)。

### 2.3 全无模型时:向导如何被拉起

`setup.trigger.ensure_setup(cfg)` 先做三来源核查(`has_any_model`):①活动
spec 已存在;②任一云密钥已配;③本地扫描命中视觉模型。**任一命中即返回
None,绝不打扰**;全无时才启动向导服务器——缺省经 `setup.daemon.
ensure_setup_server`(常驻单例,见 7.1),并按弹窗三态决定是否自动打开浏览器
(见第六节)。不弹或弹失败时,终端会打印一行中文提示:

```
未检测到视觉模型,连接向导已启动:http://127.0.0.1:8766/
```

### 2.4 时序图

```mermaid
sequenceDiagram
    participant U as 使用者
    participant CLI as 任意 CLI 入口
    participant T as takeover_once (A155)
    participant S as LocalVisionScanner (A143)
    participant M as model_runtime.json (A144)
    participant K as security.keys
    participant W as ensure_setup → 向导 (A149/A154/A145)

    U->>CLI: 首次运行任意命令
    CLI->>T: takeover_once(cfg)(惰性,异常只告警)
    alt takeover_auto=False
        T-->>CLI: disabled(零探测)
    else 本会话已接管过
        T-->>CLI: already(零副作用)
    else 活动模型已存在
        T-->>CLI: kept(沿用,零网络)
    else 本地扫描命中视觉模型
        T->>S: 仅探测 127.0.0.1:配置端口的 /v1/models
        S-->>T: 命中(如 ollama + llava)
        T->>M: set_active("ollama:llava", switched_by="takeover", validate=False)
        T-->>CLI: local(已接管)
    else 云密钥已配
        T->>K: 只问"是否已配"(布尔,不碰密钥本体)
        T->>M: set_active(提供方, switched_by="takeover")
        T-->>CLI: cloud(目录默认模型,接管零外呼)
    else 全无
        T->>W: ensure_setup(cfg)
        W->>W: 三来源再核查,全无才起服务(127.0.0.1:8766 起)
        W-->>U: 弹浏览器或打印 URL
        T-->>CLI: wizard
    end
    Note over T,M: 每条决策路径写一行审计 log_event("takeover",…)<br/>并计 telemetry;already 不写不记
```

### 2.5 会话幂等与持久化防打扰

- **会话内幂等**:`takeover_once` 有模块级标记——同一进程内只要完成过一次真实
  接管(kept/local/cloud/wizard 任一),二次调用直接返回
  `{"action":"already","detail":"本会话已完成接管,不再重复执行"}`,零副作用
  (不重探、不写审计、不计数)。`disabled` 与 `error` **不**置位:前者根本没
  接管,后者允许兄弟模块落地后重试。`reset_session()` 仅供测试/显式手动重触发。
- **跨进程幂等(持久化防重复打扰)**:接管结果写入
  `data/model_runtime.json`(`cfg.model_runtime_path`),形如
  `{"spec": "ollama:llava", "switched_at": …, "switched_by": "takeover|wizard|cli|rest"}`。
  下一个进程的 takeover 走 kept 分支直接沿用,**零网络、零打扰**。也就是说:
  向导只会"全无模型"时弹一次;接好之后不再出现。
- **审计与遥测**:每条决策路径写一行审计 `log_event("takeover", action=…,
  spec=…)` 到 `cfg.audit_path`(缺省 `data/audit.jsonl`),并计
  `telemetry.inc("takeover.<action>")`;两者失败都只告警不阻断。审计只记动作名、
  spec 与布尓值,零敏感内容(红线 33)。

---

## 三、连接向导页面逐区说明

向导地址:`http://127.0.0.1:{setup_port}/`(缺省 **8766**,仅回环)。打开方式:
自动弹出(全无模型时)、`python -m netsentinel.modelmgr serve`,或直接浏览器
访问。页面为纯标准库自托管的中文单页(内联 CSS/JS,无任何外部资源与外链,
JS 只请求相对路径 `/api/*`)。**已有活动模型时,同一页面顶部横幅变为
"当前活动模型:…(可在下方继续切换)",同套操作即手动切换器。**

页面自上而下五个区:

| 区块 | 说明 |
| --- | --- |
| ① 顶部横幅 | 未连接时显示"尚未连接视觉模型";已连接时显示当前活动模型,提示可继续切换 |
| ② 当前状态卡 | 三项:活动模型、切换来源(takeover/wizard/cli/rest)、切换时间。数据来自 `POST /api/status`(页面加载即刷新) |
| ③ 本机服务区 | 【扫描本机服务】按钮 → `POST /api/probe`(现扫描,不缓存)。命中的每个本地视觉模型一行:提供方标记、模型名、回环地址 + 【启用】按钮 → `POST /api/activate {"spec"}` → `set_active(spec, switched_by="wizard")`。无发现时给出提示"请先启动 Ollama / LM Studio / vLLM / Xinference 等本地推理服务,再点击扫描" |
| ④ 云平台区 | 提供方下拉(20 家目录:glm、openai、anthropic、gemini、qwen、doubao、hunyuan、moonshot、minimax、stepfun、siliconflow、ernie、openrouter、groq、together、xai、ollama、vllm、lmstudio、xinference;后四家标注"本机 · 免密钥")+ 密钥输入框(**`type="password"`,只进不显**,placeholder:"粘贴密钥;保存后输入框清空,仅显示掩码")+ 【保存密钥】→ `POST /api/setkey` → 服务端经 `security.key_api.accept_key_input` 校验落盘,**只回 `{"ok","masked"}`**(掩码 = 前 4 位 + `****`),界面立即清空输入框;【测试连接】→ `POST /api/test {"spec": 选中的提供方}`;下方常驻掩码状态("密钥尚未保存"或已配掩码) |
| ⑤ 离线兜底区 | 【使用离线桩继续】按钮,旁有**固定红字声明"离线桩,非模型判定"**(红线 34);点击即 `activate("stub")`,警示区再次明示 |

页面底部另有警示区(`aria-live`),承接各操作的中文反馈/错误。

密钥受纳规则(`security/key_api.py`):提供方必须在受支持目录中;密钥非空、
**长度 ≥ 8**、去空白后不含空白与控制字符;不合格给中文 `ValueError`(消息只
含长度/问题类型)。落盘仅经 `security.keys.set_key` 系通道(红线 33)。

向导底层 API(全部 POST JSON、错误中文;仅供页面与脚本使用):

| 接口 | 作用 |
| --- | --- |
| `GET /` | 向导页面本身 |
| `POST /api/status` | `{"active","switched_by","local","keys"}`;local 为现扫描,keys 仅 `{"configured": 布尔, "masked": 掩码}` |
| `POST /api/probe` | 触发一次本地回环扫描,返回 `{"local":[…]}` |
| `POST /api/activate` | `{"spec"}` 启用模型(`switched_by="wizard"`);失败 400 中文 |
| `POST /api/setkey` | `{"provider","key"}` 落盘,只回 `{"ok","masked"}` |
| `POST /api/test` | `{"spec"}` 连通性测试,结果透传(`{"ok","latency_ms","model","error"?}`) |

---

## 四、手动切换三入口对照表

活动模型机制只有一个(`model_runtime.json`),三个入口等价,`switched_by`
来源标记不同:

| | 向导页(A145/A146) | CLI(A148)`python -m netsentinel.modelmgr` | REST(A151,挂本机 service) |
| --- | --- | --- | --- |
| 打开/调用 | `http://127.0.0.1:8766/`(自动弹出或 `modelmgr serve`) | 终端 | `GET /model/active`、`POST /model/switch` 等 |
| 切换命令 | 各【启用】按钮 / 离线桩按钮 | `switch SPEC [--force]` | `POST /model/switch {"spec"}` |
| `switched_by` | `wizard` | `cli` | `rest` |
| 连通性测试 | 【测试连接】按钮 | 默认随 `switch` 做(`--force` 跳过);`test [SPEC]` 单测(缺省测活动模型) | `POST /model/test {"spec"}` |
| 本地扫描 | 【扫描本机服务】 | `probe`(扫描表)/`list`(目录+本地发现)/`status`(一行摘要) | `POST /model/probe` |
| 写入密钥 | **支持**(password 语义,只进不显) | **不支持**(status 只显示"已配置 N/M 家"布尔统计) | **不支持**(刻意不提供密钥端点,降攻击面) |
| 失败语义 | 页面中文警示区 | 退出码 0/1/2(0 成功;1 错误;2 用法错误) | spec 非法或连通不过 → 409 中文;缺 spec → 422 中文 |
| stub 明示 | 红字"离线桩,非模型判定" | 输出显式标注"离线桩,非模型判定(红线 34)" | 状态按 spec 白名单三键返回 |
| 对进行中批次 | 三入口相同:改的只是 `model_runtime.json`,**下一条扫描生效**,不中断在跑任务 | 同左 | 同左 |

spec 语法(`providers.parse_spec`):`提供方:模型`(如 `ollama:llava`、
`glm:glm-4.5v`);只写提供方名(如 `glm`)则模型由目录默认值解析;`stub`
为离线桩。切换细节与能力判定表见姊妹篇 `docs/MODEL_SWITCH.md`(A160)。

---

## 五、端口与防火墙

**所有监听与探测均仅发生在 127.0.0.1 回环上**——外部机器不可见,防火墙**无需
放行任何入站端口**;若本机安全软件提示 Python 进程监听回环端口,属预期行为。

| 端口 | 提供方/用途 | 方向 | 说明 |
| --- | --- | --- | --- |
| 11434 | ollama | 出站探测(回环) | `GET http://127.0.0.1:11434/v1/models` |
| 1234 | lmstudio | 出站探测(回环) | 同上 |
| 8000 | vllm | 出站探测(回环) | 同上 |
| 9997 | xinference | 出站探测(回环) | 同上 |
| 8766 | 连接向导 `cfg.setup_port` | 入站监听(仅绑 127.0.0.1) | 被占时自动 +1..+5 尝试 8767–8771(见 7.1) |
| 8765 | 本机 REST 服务 `cfg.service_port`(缺省 `service_host=127.0.0.1`) | 入站监听(仅绑 127.0.0.1) | `/model/*` 路由挂在此服务上(按契约 §4 由负责人接线) |

补充纪律:

- 探测端口表可在配置 `local_probe_ports` 修改;四个惯例端口之外的端口回报为
  `openai-compat`(OpenAI 兼容统称),**自动接管不会采用**不在统一目录中的
  提供方——请让本地服务跑在惯例端口上;
- 探测从不跟随重定向(3xx 一律按错误回报,防止探测被引离回环),单响应读取
  上限 1 MiB;
- 向导守护的跨进程探活只对锁文件(`data/.setup.lock`)里记录的**单个端口**
  做一次 0.5s 的回环 GET,并显式绕过系统代理(`HTTP_PROXY` 等不得劫持回环
  探活),绝不扫段。

---

## 六、环境变量

| 变量 | 作用 |
| --- | --- |
| `NETSENTINEL_NO_BROWSER` | 置为**非空值**即不自动弹浏览器、只打印向导 URL(推荐固定设为 `1`)。生效范围:首启触发(A149 `ensure_setup`)、向导守护(A154)、`modelmgr serve`、`SetupServer.start`。优先级(弹窗三态):**调用参数 > 本环境变量 > 配置 `onboarding_auto_open`**(缺省 True,该字段为并行会话预置、V8 沿用)。注意:个别入口(如 `modelmgr serve`)把 `0/false/no/off` 等字面值视为"允许弹窗",与触发侧"非空即不弹"口径不同——统一用 `1` 最稳妥 |
| `NETSENTINEL_{提供方大写}_API_KEY` / `{提供方大写}_API_KEY` | 云密钥来源之一(与配置项 `vlm_api_keys`、密钥环并列,取先命中者;如 `NETSENTINEL_QWEN_API_KEY`)。这是既有密钥解析约定,不是 V8 新增 |

---

## 七、故障排查

### 7.1 向导端口被占(自动 +1..+5)

向导守护(`setup.daemon`)启动时从 `cfg.setup_port`(8766)开始尝试绑定,
被占(`OSError`)则依次 +1..+5(即最多试到 8771),成功后把**实际端口**回写
锁文件 `data/.setup.lock`(内容为端口号)。因此向导地址可能是 8766–8771 之一,
以终端打印/浏览器打开的地址为准。

- 跨进程复用:锁文件存在且端口探活存活(任何 HTTP 应答都算)→ 直接复用该
  地址,不再起新服务;
- 陈旧锁(无应答/内容坏)自动视为失效并重启;
- 8766–8771 **全部被占**时抛中文错误
  "连接向导端口 8766~8771 均被占用,无法启动守护;请释放端口或在配置中修改
  setup_port"——此时请改配置 `setup_port` 或释放端口;
- 手动指定端口:`python -m netsentinel.modelmgr serve --port N`(临时覆盖
  `cfg.setup_port`)。

### 7.2 本地服务已跑,但没被扫到

按顺序检查:

1. **服务端口在探测表里吗?** 探测只打 `cfg.local_probe_ports`(缺省
   11434/1234/8000/9997)对应的 `http://127.0.0.1:{端口}/v1/models`。服务若
   跑在其他端口,要么挪到惯例端口,要么把端口加进配置;未映射端口即使在线
   也会被自动接管跳过(见第五节)。
2. **`/v1/models` 接口形态正确吗?** 浏览器或 curl 手测
   `http://127.0.0.1:11434/v1/models`(换成你的端口):必须返回 JSON,且含
   `data` 数组、每个元素带 `id` 字段(OpenAI 兼容形态)。缺 `data` 字段会报
   "接口形态与 OpenAI /v1/models 不符";返回非法 JSON、HTTP 4xx/5xx、超时
   (缺省 1.5s/端口,向导内 1s/端口、总看门狗 6s)都按"该端口无服务"回报。
3. **模型名被认成视觉模型了吗?** 本地清点结果会按 `vision/capability.py` 的
   模式表过滤,名字不带视觉标记的一律不收("宁缺勿滥")。识别表(代表正例;
   **权威以 `VISION_PATTERNS` 为准**,判定仅是名称形态匹配,不构成可用性承诺):

   | 家族(大小写不敏感) | 代表名 | 备注 |
   | --- | --- | --- |
   | llava / bakllava | `llava:13b`、`llava-1.5-7b-hf` | |
   | llama + vision/vl | `llama3.2-vision` | 纯文本 `llama3` 不收 |
   | qwen + vl/vision | `qwen2.5vl:7b`、`Qwen2.5-VL-72B` | |
   | minicpm-v | `minicpm-v:8b` | 文本版 `minicpm3-4b` 不收 |
   | moondream | `moondream2` | |
   | gemma-vl / gemma-vision | `gemma-3-4b-vl` | 纯文本 `gemma-7b` 不收 |
   | cogvlm | `cogvlm2-19b` | cogview(图像生成)不收 |
   | internvl | `InternVL2_5-8B` | 文本 internlm 不收 |
   | phi + vision/multimodal | `phi-3-vision`、`phi-4-multimodal` | |
   | deepseek-vl | `deepseek-vl2` | r1/chat 等纯文本不收 |
   | idefics | `idefics2-8b` | |
   | gpt-4o / gpt-4*vision / gpt-4.1 | `gpt-4o-mini`、`gpt-4.1` | `gpt-3.5`、`gpt-4-32k` 不收 |
   | claude-3/sonnet/haiku/opus | `claude-sonnet-4` | claude-2.x 不收 |
   | gemini 全系 | `gemini-2.0-flash` | |
   | glm + 数字后接 v / …vl | `glm-4v`、`glm-4.5v`、`glm-4vl` | `glm-3-turbo-preview` 不收 |
   | step-数字 v | `step-1v-8k` | 纯文本 `step-1-8k` 不收 |
   | doubao + vision/vl | `doubao-1.5-vision-pro` | `doubao-pro-32k` 纯文本不收 |
   | hunyuan + vision | `hunyuan-vision` | `hunyuan-pro` 不收 |
   | minimax + vl/vision | `MiniMax-VL-01` | abab 系不收 |
   | kimi + vision | `kimi-latest` 不收(名字无标记) | 宁缺勿滥 |
   | moonshot + vision | `moonshot-v1-8k-vision-preview` | `moonshot-v1-8k` 不收 |
   | ernie + vl/vision | `ernie-4.5-vl` | `ernie-4.5` 不收 |
   | xcomposer | `internlm-xcomposer2-4khd` | |
   | minigpt | `minigpt-4` | |
   | grok + vision | `grok-2-vision` | `grok-beta` 不收 |

   兜底说明:若能力判定模块缺席,扫描结果会**全保留**并在行上标
   `unfiltered: true`(向导/CLI 如实展示"未过滤"),绝不因兄弟模块缺席而丢模型。
4. **快速自检命令**:`python -m netsentinel.modelmgr probe`(端口/提供方/视觉
   模型数/模型前 5 的扫描表)与 `python -m netsentinel.modelmgr status`
   (活动模型 + 云密钥布尔 + 本地一行摘要)。

### 7.3 云端"测试连接"失败

`vision/connectivity.test_connection` 对云端提供方维持既有双条件与预算纪律
(红线 16/19/34),失败原因按概率排查:

1. **vlm_online 没开**(最常见,默认关):错误文案为"云端提供方未开启外呼:
   vlm_online=False(默认关,图像数据不出本机)"。需在配置中设
   `vlm_online: true` 后重试。
2. **密钥未配或无效**:密钥解析顺序为 `cfg.vlm_api_keys` → 环境变量
   `NETSENTINEL_{提供方大写}_API_KEY` / `{提供方大写}_API_KEY` → 密钥环;向导
   保存的密钥需通过校验(非空、长度 ≥8、无空白/控制字符)。缺密钥时测试
   **零外呼**直接中文报错。
3. **预算已尽**:云端测试会先记一笔预算再发一次 1x1 PNG 评分 ping;
   `vlm_daily_budget`(缺省 200)用尽时中文说明并取消外呼,次日恢复或调高
   配置。
4. 其余错误(未知提供方、解析配置问题等)一律转为结果体内的中文 `error`
   字段,不抛出、不泄露密钥。

注意区分:**本地提供方**(ollama/vllm/lmstudio/xinference)的测试连接只向
`{base_url}/models` 发一次模型清点请求(固定 3s 超时,响应含目标模型名即通过),
零预算记账、零评分外呼,也不受 vlm_online 影响。

### 7.4 其他常见情况

- **没弹浏览器**:三态裁决为"参数 > NETSENTINEL_NO_BROWSER >
  onboarding_auto_open",且弹失败也会退回打印。看终端是否已打印
  "未检测到视觉模型,连接向导已启动:{url}",手动访问即可;无头环境请设
  `NETSENTINEL_NO_BROWSER=1`。
- **接管好像没执行**:检查 `takeover_auto` 是否被设为 false(结果
  `disabled`);同进程内已执行过会返回 `already`;跨进程已有活动模型返回
  `kept`(这是设计行为,不是故障)。审计可查 `data/audit.jsonl` 中
  `event="takeover"` 的行。
- **model_runtime.json 损坏**:A144 会自动重建(损坏 JSON 重置),下次首启
  重新走接管/向导流程。
- **向导服务自身**:纯标准库,无 streamlit/fastapi 依赖;未知路径 404、方法
  不符 405、坏 JSON/超大请求体(>1 MiB)400,全部中文 JSON 错误;任何处理
  异常兜底 500 且响应不带栈。

---

## 八、与并行会话 `plugin_supervised_api` 字段的协同

V8 并行开发期间,另一条并行工作流已在 `netsentinel/contracts.py` 预置了 4 个
V8 字段并声明"一律保留不动":`plugin_supervised_api`、`onboarding_auto_open`、
`agents_max_workers`、`agent_task_db`。与本文所述链路的关系如下,其余行为
**以该并行工作流的文档为准,本文不臆造**:

- `plugin_supervised_api: bool = True`(contracts.py 注释:"插件模式:自动拉起
  并托管本地 API 服务")——**该字段由并行工作流预留,其取值与行为以该工作流
  的文档为准**。本文所述的接管(`takeover_once`)与向导(`setup/*`)链路
  当前代码**不读取**该字段,接管与向导行为不因它改变;
- 唯一的登记交叉点:`takeover_auto` 的配置注释标注"(与 plugin_supervised_api
  协同)"(contracts.py),具体协同语义以并行工作流的文档为准;
- `onboarding_auto_open: bool = True` 同为并行会话预置字段,V8 连接向导**直接
  沿用**它作为"全无模型时是否自动弹浏览器"的配置开关(弹窗三态中优先级最低
  的一档,见第六节);这是契约 §1 明文约定("弹窗开关沿用并行会话的
  onboarding_auto_open"),不属臆造。

---

## 附:相关文件与数据路径

| 路径 | 内容 |
| --- | --- |
| `netsentinel/pipeline/takeover.py` | A155 首次运行自动接管 |
| `netsentinel/vision/local_probe.py` | A143 本地视觉服务探测(仅回环) |
| `netsentinel/vision/capability.py` | A150 视觉模型名识别表(权威来源) |
| `netsentinel/vision/connectivity.py` | A147 连通性测试 |
| `netsentinel/vision/model_manager.py` | A144 活动模型管理 |
| `netsentinel/setup/server.py` / `page.py` / `render.py` | A145/A146/A156 向导服务与页面 |
| `netsentinel/setup/trigger.py` | A149 首启触发 |
| `netsentinel/setup/daemon.py` | A154 向导常驻守护(端口 +1..+5、锁文件) |
| `netsentinel/setup/flow.py` | A153 向导流程状态机 |
| `netsentinel/modelmgr.py` | A148 模型管理 CLI |
| `netsentinel/service/model_api.py` | A151 REST `/model/*` 路由 |
| `data/model_runtime.json` | 活动模型运行时(spec/来源/时间) |
| `data/.setup.lock` | 向导守护跨进程锁文件(内容为实际端口) |
| `data/audit.jsonl` | 审计日志(含 `takeover` 事件) |
| `scripts/demo_model_wizard.py` | A158 离线演示(全程零外呼,可安全试跑) |
