# NetSentinel 提供方密钥获取与本地部署指引(PROVIDER_SETUP)

> **开篇声明:本指南只含流程指引,不含任何密钥值。**文中不会出现任何真实或示例密钥;凡以 `<你的密钥>` 占位的写法,都请你从各平台自己的控制台获取。密钥等同于账户口令,获取后请按 §0.3 的安全口径保管。

本文为 V4「全平台视觉模型统一接入」(A61–A80)配套的运营者指引:16 家云端提供方**逐节给出控制台入口、密钥创建步骤、环境变量写法与额度注意事项**;本地四家(ollama / vllm / lmstudio / xinference)不涉密钥,改为**安装与启动指引**。

行为与红线以 `CONTRACTS-V4.md` 与实际代码为准:目录(`netsentinel/vision/providers.py`)、密钥环(`netsentinel/security/keys.py`)、诊断 CLI(`python -m netsentinel.vision.vlmctl`)。各平台端点、模型名与界面入口均为**提示值,以官方为准,上线前核验一次**(红线 18)。

---

## 0. 通用约定(先读,适用于全部云端提供方)

### 0.1 环境变量两种形态

NetSentinel 对每个云端提供方约定**主/备两个环境变量名**(值相同,主名优先,首个非空生效):

- 主形态:`NETSENTINEL_<提供方大写>_API_KEY`(NetSentinel 专用,推荐,避免与其他工具的 Key 混用);
- 备形态:`<提供方大写>_API_KEY` 或该平台**业界通用名**(如 qwen 用 `DASHSCOPE_API_KEY`、doubao 用 `ARK_API_KEY`、ernie 用 `QIANFAN_API_KEY`)。

写入方式(Linux/macOS 用 `export`,Windows 用 `set`/`setx`):

```bash
# Linux / macOS(当前会话生效;写入 ~/.bashrc 或 ~/.zshrc 持久化)
export NETSENTINEL_OPENAI_API_KEY="<你的密钥>"
export OPENAI_API_KEY="<你的密钥>"          # 可选:备形态,二选一即可
```

```bat
:: Windows CMD(当前会话生效;setx 持久化,重开终端后生效)
set NETSENTINEL_OPENAI_API_KEY=<你的密钥>
setx NETSENTINEL_OPENAI_API_KEY <你的密钥>
```

> PowerShell 用户:`$env:NETSENTINEL_OPENAI_API_KEY = "<你的密钥>"`(会话级)或 `[Environment]::SetEnvironmentVariable(...)`(持久)。

### 0.2 三种配置途径与优先级

`security.keys.get_key(提供方, cfg)` 按以下优先级解析(**先到先得;只记录来源,绝不记录密钥本身**):

| 优先级 | 途径 | 写法 | 适用 |
| --- | --- | --- | --- |
| 1(最高) | 配置项 | `config.yaml` 里 `vlm_api_keys: {openai: "<你的密钥>"}` | 临时试验;**勿提交入库** |
| 2 | 环境变量 | §0.1 两种形态 | 推荐:容器、CI、服务器 |
| 3 | 密钥环文件 | `~/.netsentinel/keys/<提供方>` 文件首行 | 多平台集中管理,不落项目目录 |

密钥环文件用 `set_key` 写入(自动建目录、自动收紧权限:POSIX `chmod 600`,Windows 尝试 `icacls`;只提示路径,绝不回显密钥):

```bash
python -c "from netsentinel.security.keys import set_key; set_key('openai', '<你的密钥>')"
# 写入 ~/.netsentinel/keys/openai 首行;qwen/doubao/... 同理换提供方名
```

### 0.3 安全提示(红线级,五条)

1. **密钥不入库**:不要把带密钥的 `config.yaml` 提交进仓库(优先用环境变量或密钥环文件);`.gitignore` 不应是唯一防线;
2. **密钥不入日志**:NetSentinel 全链路(日志 / manifest / 异常消息 / 诊断输出)只显示「已配置 / 未配置」布尔;`redact_keys()` 会把误入配置的密钥打码为「前 4 位 + `****`」——这是底线(红线 17),也请你在工单、聊天、截图中同样遵守;
3. **最小权限**:为 NetSentinel **单独创建**一枚 API Key,不与人工账号的 Key 混用;平台若提供权限范围选择,只勾选模型调用所需的最小范围;不共用生产业务的 Key;
4. **额度隔离**:专用 Key 便于单独设消费上限与告警,泄漏时的影响面也最小;各平台免费档与预充值要求见各节「额度注意」;
5. **泄露立即吊销**:一旦怀疑密钥外泄——**第一时间在平台控制台删除(吊销)该 Key → 创建新 Key → 更新环境变量或重跑 `set_key` → 用 `vlmctl doctor` 复核**,期间暂停 VLM 外呼(`vlm_online: false`);被吊销的旧密钥自然失效,无需"通知 NetSentinel"。

### 0.4 配好之后:上线前核验一次

```bash
python -m netsentinel.vision.vlmctl list                     # 20 家总表:密钥只显示 已配置/未配置
python -m netsentinel.vision.vlmctl doctor                   # 全提供方配置体检(零外呼)
python -m netsentinel.vision.vlmctl ping openai:gpt-4o-mini  # 真发一次 1x1 测试图(唯一外呼,人工触发)
```

`ping` 是 NetSentinel 唯一允许外呼的诊断动作(红线 20):看到延迟、模型回显与链路可用结论后,该 `提供方:模型` 才算核验通过。云端提供方还需 `vlm_online: true`(双闸门,红线 16)。

---

## 1. glm 智谱(默认提供方)

- **控制台入口**:https://open.bigmodel.cn (智谱开放平台,API Keys 管理页);
- **创建步骤**:① 注册并实名登录 → ② 进入控制台「API Keys」页 → ③ 新建 API Key(形如 `xxxxxx.xxxxxx` 的长串,**通常只在创建时完整展示一次**,立即妥善保存)→ ④ 确认账户已开通 GLM 系列视觉模型(如 `glm-5.3-flash`)的调用额度(新账户一般有免费额度);
- **环境变量**:`NETSENTINEL_GLM_API_KEY` / 备形态 `GLM_API_KEY`;
- **额度注意**:flash 档价格低且有历史免费档(如 `glm-4v-flash`,以官方现行政策为准);大流量前先小批量试跑并观察用量页。

## 2. openai

- **控制台入口**:https://platform.openai.com/api-keys ;
- **创建步骤**:① 注册登录 → ② 进入 API Keys 页 → ③ 「Create new secret key」命名后创建(`sk-` 前缀,只显示一次)→ ④ 预充值:API 与 ChatGPT 订阅**分开计费**,须在 Billing 里单独充值或设额度上限;
- **环境变量**:`NETSENTINEL_OPENAI_API_KEY` / 备形态 `OPENAI_API_KEY`;
- **额度注意**:分钟级限速(RPM/TPM)随账户等级变化,大流量扫描先小批量;建议设硬性月度上限与告警。

## 3. anthropic

- **控制台入口**:https://console.anthropic.com (Settings → API keys);
- **创建步骤**:① 注册登录 → ② Console 的 API Keys 页 → ③ 「Create Key」(`sk-ant-` 前缀,只显示一次)→ ④ Billing 里充值或开通 API 额度;
- **环境变量**:`NETSENTINEL_ANTHROPIC_API_KEY` / 备形态 `ANTHROPIC_API_KEY`;
- **额度注意**:默认模型名 `claude-sonnet-4` 为提示值,以官方模型列表为准;旗舰档单价高,适合复核抽检而非大流量初筛。

## 4. gemini

- **控制台入口**:https://aistudio.google.com/apikey (Google AI Studio;打不开时搜索「Google AI Studio API key」);
- **创建步骤**:① 登录 Google 账号进入 AI Studio → ② 「Get API key」→「Create API key」→ ③ 选择或新建项目,生成密钥 → ④ 免费档可直接用于小规模调用,生产建议绑定计费项目;
- **环境变量**:`NETSENTINEL_GEMINI_API_KEY` / 备形态 `GEMINI_API_KEY`;
- **额度注意**:flash 档便宜且存在免费额度(限 RPM/RPD,以官方为准);**数据出境合规**:调用即送审图片传至 Google 境外服务器,涉个人信息图片须先完成合规评估(见 [ETHICS.md](ETHICS.md))。

## 5. qwen 通义千问(阿里云百炼 DashScope)

- **控制台入口**:https://bailian.console.aliyun.com (打不开时搜索「阿里云百炼 控制台」);
- **创建步骤**:① 注册阿里云账号并实名 → ② 开通「模型服务百炼 / Model Studio」→ ③ 控制台「API-KEY」管理页创建密钥 → ④ 按需领取 / 确认 Qwen-VL 系列额度;
- **环境变量**:`NETSENTINEL_QWEN_API_KEY` / 备形态 `DASHSCOPE_API_KEY`(平台通用名);
- **额度注意**:**API Key 按地域绑定**——北京端点配北京 Key,跨地域调用返回 401 `invalid_api_key`;端点因地域而异(必要时用 `vlm_provider_base_urls` 覆盖)。

## 6. doubao 豆包(字节火山方舟)

- **控制台入口**:https://console.volcengine.com/ark (火山方舟;打不开时搜索「火山方舟 控制台」);
- **创建步骤**:① 注册火山引擎账号并实名 → ② 开通方舟 / Ark 服务 → ③ 「API Key 管理」创建密钥 → ④ 「在线推理」里为豆包视觉模型**创建推理接入点**,或记下要直连的模型 ID;
- **环境变量**:`NETSENTINEL_DOUBAO_API_KEY` / 备形态 `ARK_API_KEY`(平台通用名);
- **额度注意**:`model` 参数两形态二选一:接入点 ID(`ep-` 前缀,写法 `classifier: "doubao:ep-xxxx"`)或官方模型 ID;免费试用额度与后付费以官方页面为准。

## 7. hunyuan 混元(腾讯云)

- **控制台入口**:https://console.cloud.tencent.com (搜索「腾讯云 混元 API 密钥」直达);
- **创建步骤**:① 注册腾讯云账号并实名 → ② 「访问管理 → API 密钥管理」或混元控制台按官方指引生成 API Key → ③ 开通混元大模型服务 → ④ 确认视觉模型调用额度;
- **环境变量**:仅主形态 `NETSENTINEL_HUNYUAN_API_KEY`(目录未定义备形态);
- **额度注意**:文档提示生文接口默认限制 5 个并发、限额由主子账号共享;建议用子账号专用密钥(最小权限),大流量注意节流(`provider_throttle` 可用)。

## 8. moonshot Kimi(月之暗面)

- **控制台入口**:https://platform.kimi.com (原 platform.moonshot.cn,已迁移);
- **创建步骤**:① 注册 / 登录(可用手机号)→ ② 「API Key 管理」新建(`sk-` 前缀,只显示一次)→ ③ 按需充值或领取免费额度;
- **环境变量**:`NETSENTINEL_MOONSHOT_API_KEY` / 备形态 `MOONSHOT_API_KEY`;
- **额度注意**:`max_tokens` 已被官方弃用(建议 `max_completion_tokens`);`finish_reason=length` 表示输出截断——JSON 解析失败时优先怀疑截断;上下文缓存命中部分计费更低。

## 9. minimax

- **控制台入口**:https://platform.minimax.cn (原 platform.minimaxi.com,已迁移);
- **创建步骤**:① 注册并实名 → ② 控制台「API 密钥 / Key 管理」新建 → ③ 确认视觉模型已开通 → ④ 充值(或使用试用额度);
- **环境变量**:仅主形态 `NETSENTINEL_MINIMAX_API_KEY`;
- **额度注意**:**域名更新**——官方 OpenAPI 现为 `https://api.minimax.cn`,目录旧域名建议用 `vlm_provider_base_urls: {minimax: "https://api.minimax.cn/v1"}` 覆盖后 `vlmctl ping minimax` 核验;单图 ≤10MB;限流错误码 1002;深度思考内容走 `reasoning_content`(最终答案仍在 `content`,NetSentinel 解析只取后者)。

## 10. stepfun 阶跃星辰

- **控制台入口**:https://platform.stepfun.com ;
- **创建步骤**:① 注册并实名 → ② 控制台「API 密钥」页新建(`sk-` 前缀)→ ③ 开通 Step 系列多模态调用权限 → ④ 充值或领取新用户额度;
- **环境变量**:仅主形态 `NETSENTINEL_STEPFUN_API_KEY`;
- **额度注意**:建议图片长或宽 ≤4096 像素、多图总量 ≤20MB、单请求 ≤60 张;目录默认 `step-1v-8k` 为旧提示值,现行视觉模型以官方列表为准。

## 11. siliconflow 硅基流动

- **控制台入口**:https://cloud.siliconflow.cn (SiliconCloud;打不开时搜索「硅基流动 SiliconCloud」);
- **创建步骤**:① 注册登录 → ② 「API 密钥」页新建密钥 → ③ 在模型广场确认目标视觉模型(如 `Qwen/Qwen2.5-VL-7B-Instruct`)可用 → ④ 充值或使用赠送额度;
- **环境变量**:仅主形态 `NETSENTINEL_SILICONFLOW_API_KEY`;
- **额度注意**:模型 ID 为**『组织 / 模型』两级形式**(命名空间不可省略);视觉输入按分辨率折算 token 计费;官方声明模型列表可能调整,以模型广场为准。

## 12. ernie 文心(百度千帆)

- **控制台入口**:https://console.bce.baidu.com/qianfan (搜索「百度智能云千帆」直达);
- **创建步骤**:① 注册百度智能云账号并实名 → ② 开通千帆 ModelBuilder → ③ 「应用 / API Key」处创建,获得 `bce-v3/` 前缀的 API Key → ④ 为目标视觉模型开通服务(预置模型直接用;自部署服务记下其「服务 API 名称」);
- **环境变量**:`NETSENTINEL_ERNIE_API_KEY` / 备形态 `QIANFAN_API_KEY`;
- **额度注意**:响应带 `X-Ratelimit-*` 配额头,配额用尽后 0–60s 刷新;传图 URL 建议 ≤1024 字节、Base64 原图 ≤10MB、最多 10 张;目录默认 `ernie-4.5-vl` 为提示值,以官方模型列表为准。

## 13. openrouter(聚合网关)

- **控制台入口**:https://openrouter.ai (Settings → API Keys,即 Keys 页);
- **创建步骤**:① 注册登录(Google / GitHub 或邮箱)→ ② 「Keys」页「Create key」(`sk-or-` 前缀)→ ③ 可设置单 Key 消费上限 → ④ 免费档模型无需充值,付费模型须充值信用额度;
- **环境变量**:`NETSENTINEL_OPENROUTER_API_KEY` / 备形态 `OPENROUTER_API_KEY`;
- **额度注意**:`:free` 后缀免费路由**限速显著严于付费档**(每分钟 / 每日请求数约束),只适合小批量试跑;`provider_quirks` 默认注入官方可选归因头 `X-Title: NetSentinel`,`HTTP-Referer` 留给部署方按真实站点附加;数据经境外网关流转,合规评估同 gemini 一节。

## 14. groq

- **控制台入口**:https://console.groq.com/keys ;
- **创建步骤**:① 注册登录 → ② API Keys 页「Create API Key」(`gsk_` 前缀)→ ③ 免费额度即开即用,无需充值;
- **环境变量**:`NETSENTINEL_GROQ_API_KEY` / 备形态 `GROQ_API_KEY`;
- **额度注意**:**速率配额较小**(免费档有 RPM / TPD 限制),适合小批量快跑与延迟敏感抽检,不适合整站扫描主力;配额以控制台显示为准。

## 15. together

- **控制台入口**:https://api.together.ai/settings/keys (打不开时搜索「Together AI API keys」);
- **创建步骤**:① 注册登录 → ② Settings → API Keys「Create new key」→ ③ 绑定充值方式(按量计费,部分开源模型有免费档)→ ④ 确认目标视觉模型在官方模型列表中;
- **环境变量**:`NETSENTINEL_TOGETHER_API_KEY` / 备形态 `TOGETHER_API_KEY`;
- **额度注意**:模型名大小写形态与 groq 略有差异(以官方目录写法为准);数据出境合规同 gemini 一节。

## 16. xai Grok

- **控制台入口**:https://console.x.ai (API → API Keys;打不开时搜索「xAI API」);
- **创建步骤**:① 注册 / 登录 xAI 账号 → ② 「API Keys」创建密钥(`xai-` 前缀)→ ③ 为团队 / 项目绑定充值或试用额度 → ④ 确认视觉模型(如 `grok-2-vision-1212`)可用性以官方为准;
- **环境变量**:`NETSENTINEL_XAI_API_KEY` / 备形态 `XAI_API_KEY`;
- **额度注意**:存在 `grok-2-vision`(不带日期后缀)等别名,确切可用名上线前 `vlmctl ping xai:<模型>` 核验;数据出境合规同 gemini 一节。

---

## 17. 本地四家:安装与启动指引(免密钥、免 vlm_online)

本地提供方 `local=True`:数据不出本机(红线 16),**不需要任何密钥与环境变量**,但仍须显式选择(如 `classifier: "ollama:llava"`),且**仍走预算一本账**(红线 19,本地算力也是成本)。启动后统一用 `vlmctl doctor --probe` 探活、`vlmctl ping <提供方>:<模型>` 核验。

### 17.1 ollama

1. 从 https://ollama.com 下载安装包(macOS / Windows / Linux 脚本),安装后服务常驻(默认监听 `127.0.0.1:11434`);
2. 拉取一个视觉模型:`ollama pull llava`(其他常见视觉模型如 `llama3.2-vision`、`qwen2.5vl`、`minicpm-v`,按显存选;带 tag 写法如 `llava:13b` 也受支持);
3. 核验:`python -m netsentinel.vision.vlmctl ping ollama:llava`(应看到模型回显与链路可用结论);
4. 接入:`classifier: "ollama:llava"`(ollama 是四家里唯一有目录默认模型的,可不写 `:llava`)。

### 17.2 vllm

1. 安装:`pip install vllm`(按官方文档确认 CUDA / 驱动要求);
2. 启动 OpenAI 兼容服务:`vllm serve Qwen/Qwen2.5-VL-7B-Instruct`(模型名即启动参数里的 HF 路径,按显存选杯;默认监听 `127.0.0.1:8000`);
3. 核验:`python -m netsentinel.vision.vlmctl ping "vllm:Qwen/Qwen2.5-VL-7B-Instruct"`;
4. 接入:`classifier: "vllm:Qwen/Qwen2.5-VL-7B-Instruct"`——**必须显式指定模型**(目录无默认)。

### 17.3 lmstudio

1. 从 https://lmstudio.ai 下载安装 LM Studio 桌面端;
2. 在应用内搜索并下载一个视觉模型(如 `qwen2.5-vl-7b-instruct`、`llava-1.5-7b-hf`);
3. 加载该模型,并在 Developer / Local Server 标签页**开启本地服务**(默认监听 `127.0.0.1:1234`);
4. 接入:`classifier: "lmstudio:<模型目录名>"`(**必须显式指定模型**,名字以 LM Studio 模型列表为准);核验用 `vlmctl ping lmstudio:<模型目录名>`。

### 17.4 xinference

1. 安装:`pip install xinference`;
2. 启动:`xinference-local`(默认监听 `127.0.0.1:9997`,可用浏览器打开其界面);
3. 在界面或 CLI 里拉起一个视觉模型(如 `qwen2.5vl` / `llava` / `minicpm-v`),记下注册的模型名;
4. 接入:`classifier: "xinference:<模型名>"`(**必须显式指定模型**);核验用 `vlmctl doctor --probe` 看 xinference 槽位是否在线。

---

## 18. 相关文档

- [PROVIDERS.md](PROVIDERS.md) —— 20 家提供方权威总览(端点 / 方言 / 默认模型 / 密钥环境变量 / 每平台特点)
- [MULTI_PROVIDER.md](MULTI_PROVIDER.md) —— 使用指南(classifier 语法 / 密钥三途径 / ensemble / failover / vlmctl / 红线 16–20)
- [VLM_GUIDE.md](VLM_GUIDE.md) —— V2 时期 GLM 单平台深入指南(密钥文件权限收紧的命令行示例)
- [UPGRADE_V4.md](UPGRADE_V4.md) / [../CHANGELOG.md](../CHANGELOG.md) —— V4 升级总览与四轮演进总账
