# NetSentinel 全平台视觉模型提供方总览(PROVIDERS)

本文是 V4「全平台视觉模型统一接入」的**提供方权威总览**:20 家提供方的端点、方言、默认模型、密钥环境变量与各自注意事项,以及"如何新增一家提供方"的三步教程。

全部行为以 `CONTRACTS-V4.md`(尤其 §2 提供方目录规范与红线 16–20)与实际代码为准:

- 目录数据:`netsentinel/vision/providers.py`(`PROVIDERS` 权威清单,A61);
- 模型档位:`netsentinel/vision/model_catalog.py`(A65);
- 平台差异细节:`netsentinel/vision/provider_quirks.py`(A64);
- 使用指南(语法/密钥/ensemble/failover/vlmctl):[MULTI_PROVIDER.md](MULTI_PROVIDER.md);
- V2 时期 GLM 单平台深入指南:[VLM_GUIDE.md](VLM_GUIDE.md)。

本文不引入契约之外的承诺。

---

## 0. 开篇红线声明(先读这一段)

> **目录里的模型名与端点均为提示值,不构成可用性承诺。** 各平台模型命名与档位迭代频繁,一切以**官方文档为准**(V4 红线 18)。所有端点可用 `vlm_provider_base_urls` 覆盖、所有默认模型可用 `vlm_provider_models` 覆盖、密钥可用 `vlm_api_keys` / 环境变量 / 密钥环文件三种途径配置(见 [MULTI_PROVIDER.md](MULTI_PROVIDER.md) §2)。
>
> **上线前必须人工核验一次**:用诊断 CLI 对目标提供方真发一次 1×1 测试图评分请求——
>
> ```bash
> python -m netsentinel.vision.vlmctl ping openai:gpt-4o-mini
> ```
>
> 看到延迟、模型回显与「✅ 链路可用」结论后,该 `提供方:模型` 才算核验通过。`ping` 是 NetSentinel 唯一允许外呼的诊断动作,且只能由人工在 `vlmctl` 里显式触发(红线 20);常规扫描与测试一律零外呼。
>
> **密钥绝不入日志 / manifest / 异常消息**(红线 17):所有诊断输出只出现「已配置 / 未配置」布尔,`providers.ResolvedProvider` 的 repr 也只显示密钥状态,绝不回显密钥值。

其余 V4 红线(默认零外呼、预算一本账等)见 [MULTI_PROVIDER.md](MULTI_PROVIDER.md) §9 全文重述。

---

## 1. 三种 API 方言

NetSentinel 把 20 家提供方归一到三种 API 方言(`providers.STYLES`,由 `vlm_client.UniversalVLMClient` 统一实现):

| 方言 | 请求形态 | 传图方式 | 鉴权头 | 使用方 |
| --- | --- | --- | --- | --- |
| `openai` | `POST {base}/chat/completions` | `image_url` = data URI(Base64) | `Authorization: Bearer <key>` | 绝大多数提供方(含全部本地四家) |
| `anthropic` | `POST {base}/messages`,system 提取到顶层,`max_tokens` 必填 | `content` 块 `type=image` + `source.base64` | `x-api-key` + `anthropic-version` | anthropic |
| `gemini` | `POST {base}/models/{model}:generateContent` | `parts[].inline_data`(mime_type + base64) | `x-goog-api-key` | gemini |

三种方言均请求 JSON 输出(openai 方言 `response_format=json_object`,gemini 方言 `generationConfig.responseMimeType=application/json`),温度统一 0.1(内容审核要求输出稳定)。跨家族的响应怪癖(围栏包裹、思考字段等)由 `provider_quirks` 记录、`response_repair` 兜底修复。

---

## 2. 提供方总表(20 家)

以下与 `providers.PROVIDERS` 逐项一致(值照抄 `CONTRACTS-V4.md` §2 权威清单):

| 提供方 | 方言 | 端点(base_url,提示值) | 默认模型(提示值) | 密钥环境变量(主 / 备) | 本地 |
| --- | --- | --- | --- | --- | --- |
| glm 智谱 | openai | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3-flash` | `NETSENTINEL_GLM_API_KEY` / `GLM_API_KEY` | |
| openai | openai | `https://api.openai.com/v1` | `gpt-4o-mini` | `NETSENTINEL_OPENAI_API_KEY` / `OPENAI_API_KEY` | |
| anthropic | anthropic | `https://api.anthropic.com/v1` | `claude-sonnet-4`(以官方为准) | `NETSENTINEL_ANTHROPIC_API_KEY` / `ANTHROPIC_API_KEY` | |
| gemini | gemini | `https://generativelanguage.googleapis.com/v1beta` | `gemini-2.0-flash` | `NETSENTINEL_GEMINI_API_KEY` / `GEMINI_API_KEY` | |
| qwen 通义 | openai | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` | `NETSENTINEL_QWEN_API_KEY` / `DASHSCOPE_API_KEY` | |
| doubao 豆包 | openai | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-1.5-vision-pro`(或推理接入点 ID) | `NETSENTINEL_DOUBAO_API_KEY` / `ARK_API_KEY` | |
| hunyuan 混元 | openai | `https://api.hunyuan.cloud.tencent.com/v1` | `hunyuan-vision` | `NETSENTINEL_HUNYUAN_API_KEY` | |
| moonshot Kimi | openai | `https://api.moonshot.cn/v1` | `kimi-latest` | `NETSENTINEL_MOONSHOT_API_KEY` / `MOONSHOT_API_KEY` | |
| minimax | openai | `https://api.minimax.chat/v1` | `MiniMax-VL-01`(以官方为准) | `NETSENTINEL_MINIMAX_API_KEY` | |
| stepfun 阶跃 | openai | `https://api.stepfun.com/v1` | `step-1v-8k` | `NETSENTINEL_STEPFUN_API_KEY` | |
| siliconflow 硅基流动 | openai | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-VL-7B-Instruct` | `NETSENTINEL_SILICONFLOW_API_KEY` | |
| ernie 文心 | openai | `https://qianfan.baidubce.com/v2` | `ernie-4.5-vl`(以官方为准) | `NETSENTINEL_ERNIE_API_KEY` / `QIANFAN_API_KEY` | |
| openrouter | openai | `https://openrouter.ai/api/v1` | `qwen/qwen2.5-vl-72b-instruct:free` | `NETSENTINEL_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` | |
| groq | openai | `https://api.groq.com/openai/v1` | `meta-llama/llama-4-scout-17b-16e-instruct` | `NETSENTINEL_GROQ_API_KEY` / `GROQ_API_KEY` | |
| together | openai | `https://api.together.xyz/v1` | `meta-llama/Llama-4-Scout-17B-16E-Instruct` | `NETSENTINEL_TOGETHER_API_KEY` / `TOGETHER_API_KEY` | |
| xai Grok | openai | `https://api.x.ai/v1` | `grok-2-vision-1212` | `NETSENTINEL_XAI_API_KEY` / `XAI_API_KEY` | |
| ollama(本地) | openai | `http://127.0.0.1:11434/v1` | `llava` | 免密钥 | ✓ |
| vllm(本地) | openai | `http://127.0.0.1:8000/v1` | (必填模型) | 免密钥 | ✓ |
| lmstudio(本地) | openai | `http://127.0.0.1:1234/v1` | (必填模型) | 免密钥 | ✓ |
| xinference(本地) | openai | `http://127.0.0.1:9997/v1` | (必填模型) | 免密钥 | ✓ |

标注说明:

- **环境变量**:按顺序逐个尝试,首个非空生效;部分平台(hunyuan / minimax / stepfun / siliconflow)只有专用变量名一个来源。本地四家 `key_envs` 为空,完全不看环境变量。
- **本地标记 `local=True`**(ollama / vllm / lmstudio / xinference):免 `vlm_online` 闸门、免密钥,数据不出本机(红线 16);**仍须运营者显式选择**(`classifier: ollama:llava`),且仍受预算红线 19 约束——本地算力也是成本,`vlm_cache.spend_one` 照常记账。
- 「必填模型」的本地三家(以及任何目录无默认值的场景)必须写成 `提供方:模型`(如 `vllm:Qwen/Qwen2.5-VL-7B-Instruct`),否则 `providers.resolve` 抛中文错误「本地提供方必须指定模型,如 ollama:llava」。
- 表中 20 家即 `vlmctl list` / `vlmctl doctor` 体检的完整范围;各提供方更细的模型档位见 `model_catalog.MODELS`(`vlmctl models <提供方>` 可查)。

---

## 3. 每平台一节

以下每节三段式:**定位特点 / 适用场景建议 / 注意事项**。适用场景按项目惯用的三档分工给出——**便宜初筛**(cheap 档,大流量低成本过一遍)、**旗舰复核**(flagship 档,疑难图与高价值判定的精审)、**本地隐私**(数据不出本机)。模型档位标签来自 `model_catalog`,均为提示值。

### 3.1 glm 智谱

- **定位特点**:智谱开放平台 GLM 视觉模型,OpenAI 兼容口,Bearer 鉴权;**项目默认提供方**(`vlm_provider: glm`),也是 V2 以来接入最成熟的一家。
- **适用场景建议**:`glm-5.3-flash`(cheap/balanced)做便宜初筛的主力;`glm-4.5v`(flagship)做旗舰复核;官方曾提供免费档的 `glm-4v-flash` 可作零成本试跑。
- **注意事项**:仅需 Bearer 鉴权,无平台附加头;`image_url.url` 支持公网 URL 或 Base64 Data URL;官方推荐 `temperature=1、top_p=0.95`,但内容审核场景统一低温 0.1 保证输出稳定。现行视觉型号为 `glm-5.3-flash` / `glm-5.3-flashx`,计费以官方为准。

### 3.2 openai

- **定位特点**:GPT-4o 系列多模态,OpenAI 方言的本尊,生态最成熟、行为最可预期。
- **适用场景建议**:`gpt-4o-mini`(cheap/balanced)跨平台 ensemble 的常用初筛成员;`gpt-4o` / `gpt-4.1-mini` 按需替换。
- **注意事项**:分钟级限速(TPM/RPM)需留意,大流量扫描前先小批量试跑;数据将传输至 OpenAI 境外服务器,涉个人信息图片须评估数据出境合规。

### 3.3 anthropic

- **定位特点**:Claude 视觉模型,anthropic 方言(system 顶层、`max_tokens` 必填、`x-api-key` + `anthropic-version` 头)。
- **适用场景建议**:`claude-sonnet-4`(balanced/flagship)主力复核;`claude-haiku-4-5`(cheap)便宜初筛;`claude-opus-4-5`(flagship)顶配抽检。
- **注意事项**:默认模型名 `claude-sonnet-4` 为提示值,**以官方为准**;传输层统一处理方言差异,使用方无需关心请求形态。数据出境合规同 openai 一节。

### 3.4 gemini

- **定位特点**:Google Gemini,gemini 方言(`x-goog-api-key` 标头、`inline_data` 传图、`responseMimeType=application/json`)。
- **适用场景建议**:`gemini-2.0-flash` / `gemini-2.5-flash`(cheap/balanced)便宜初筛;`gemini-2.5-pro`(flagship)旗舰复核。
- **注意事项**:**数据出境合规提示**——调用 Gemini 意味着送审图片传输至 Google 境外服务器;在中国大陆运营场景下,涉及个人信息的图片出镜前须依《个人信息保护法》等完成合规评估(参见 [ETHICS.md](ETHICS.md) 与 docs/regulations)。偶发 markdown 围栏包裹 JSON,已由响应修复层兜底。

### 3.5 qwen 通义千问

- **定位特点**:阿里云百炼(DashScope)OpenAI 兼容模式,国内访问稳定,无需代理。
- **适用场景建议**:`qwen-vl-flash`(cheap)便宜初筛;`qwen-vl-plus`(balanced)均衡主力;`qwen-vl-max`(flagship,目录默认)旗舰复核。
- **注意事项**:API Key 按地域绑定,跨地域调用返回 401 `invalid_api_key`(北京端点配北京 Key);端点因地域而异,弗吉尼亚为 `dashscope-us` 等,业务空间走 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` 子域名——必要时用 `vlm_provider_base_urls` 覆盖。

### 3.6 doubao 豆包

- **定位特点**:字节火山方舟(Ark v3)豆包视觉模型,OpenAI 兼容,Bearer 鉴权。
- **适用场景建议**:`doubao-1.5-vision-pro`(flagship/balanced,目录默认)主力;`doubao-1.5-vision-lite`(cheap)便宜初筛。
- **注意事项**:**接入点 ID**——`model` 参数支持两种形态二选一:方舟控制台『在线推理』创建的推理接入点 ID(形如 `ep-2024xxxxxx-xxxxx`,写成 `classifier: doubao:ep-xxxx`),或直接填官方模型 ID。`model_catalog.validate_model` 对 `ep-` 前缀形态有通配白名单。实际可用模型名、计费与限流以官方为准,上线前 `vlmctl ping doubao:<你的模型>` 复核一次。

### 3.7 hunyuan 混元

- **定位特点**:腾讯云混元视觉模型,OpenAI 兼容口。
- **适用场景建议**:`hunyuan-vision`(balanced,目录默认)主力;`hunyuan-turbo-vision`(flagship)高性能档。
- **注意事项**:仅专用环境变量 `NETSENTINEL_HUNYUAN_API_KEY` 一个密钥来源;文档提示生文接口默认限制 5 个并发、限额由主子账号共享,大流量扫描注意节流;平台功能将逐步迁移 TokenHub,留意官方公告。

### 3.8 moonshot Kimi

- **定位特点**:月之暗面 Kimi 视觉模型,长上下文能力强,OpenAI 兼容口。
- **适用场景建议**:`kimi-latest`(balanced/flagship,目录默认)复核;早期视觉预览版 `moonshot-v1-8k-vision-preview` 可能已下线,不建议新接入。
- **注意事项**:**max_completion_tokens**——官方已弃用 `max_tokens`,建议改用 `max_completion_tokens` 并按需调大(K3 默认 131072、最大 1048576);`finish_reason=length` 表示输出被截断,JSON 解析失败时优先怀疑截断;超出上下文窗口返回 `invalid_request_error`。文档现行列出 `kimi-k2.6` / `kimi-k3` 等,目录默认 `kimi-latest` 为提示值。

### 3.9 minimax

- **定位特点**:MiniMax 视觉模型,OpenAI 兼容口。
- **适用场景建议**:`MiniMax-VL-01`(flagship/balanced,目录默认)——注意这是**旧型号**提示值;`MiniMax-M2`(balanced)新一代系列,视觉输入能力以官方为准。
- **注意事项**:**域名更新**——官方 OpenAPI servers 现为 `https://api.minimax.cn`,目录中的 `api.minimax.chat/v1` 是旧域名;部署时建议用 `cfg.vlm_provider_base_urls: {minimax: "https://api.minimax.cn/v1"}` 覆盖并 `vlmctl ping minimax` 验证。深度思考模型的思考过程走 `reasoning_content` 字段(最终答案仍在 `message.content`,解析只取后者);单图 ≤10MB;限流错误码 1002;`service_tier=priority` 价格为 standard 的 1.5 倍。

### 3.10 stepfun 阶跃星辰

- **定位特点**:阶跃星辰 Step-1V / Step-1O 系列多模态,OpenAI 兼容,国内直连。
- **适用场景建议**:`step-1v-8k`(cheap/balanced,目录默认)初筛;`step-1v-32k` 长上下文档;`step-1o-turbo-vision`(cheap)Omni 快速档。注意 `step-1v-8k` 为旧提示值,文档现行视觉模型为 Step 5 Preview / Step 3.7 Flash / Step-1o Turbo Vision。
- **注意事项**:仅需 Bearer,无附加头;建议图片长或宽 ≤4096 像素、多图总量 ≤20MB、单请求 ≤60 张;Base64 传图遵循 `data:[<mediatype>][;base64],<data>` Data URL 形态。

### 3.11 siliconflow 硅基流动

- **定位特点**:硅基流动 SiliconCloud 聚合平台,一个密钥调用多家开源视觉模型,OpenAI 兼容。
- **适用场景建议**:`Qwen/Qwen2.5-VL-7B-Instruct`(cheap/balanced,目录默认)便宜初筛;`Qwen/Qwen2.5-VL-72B-Instruct`(flagship)开源大杯复核;`OpenGVLab/InternVL2_5-8B`(balanced)备选。
- **注意事项**:模型 ID 采用**『组织/模型』两级形式**(如 `Qwen/Qwen2.5-VL-72B-Instruct`),与本地 vLLM 的 HF 路径形态一致;官方声明「支持的模型可能发生调整」,以平台模型广场为准;视觉输入按分辨率折算 token 计费(如 Qwen 系列 `detail=low` 统一按 448×448 约 256 token),账单以官方转换结果为准。

### 3.12 ernie 文心

- **定位特点**:百度千帆(ModelBuilder)v2 OpenAI 兼容口,API Key 为 `bce-v3/` 前缀形态。
- **适用场景建议**:`ernie-4.5-vl`(flagship/balanced,目录默认)主力——提示值,文档现行视觉模型为 `ernie-4.5-turbo-vl-preview` / `ernie-4.5-vl-28b-a3b` 等;`ernie-4.5-vl-flash`(cheap)快速档。
- **注意事项**:**服务 API 名称**——千帆上自训练/部署服务的 `model` 参数须填该服务详情页对应的 API 名称(千帆控制台『在线推理』查看),预置服务则直接填模型 ID。官方声明除公共头域外无其它特殊头域;响应带 `X-Ratelimit-*` RPM/TPM 配额头,配额用尽后 0–60s 刷新;传图支持 URL(UTF-8 下建议 ≤1024 字节)与 Base64(原图 ≤10MB),最多 10 张。

### 3.13 openrouter

- **定位特点**:国际聚合网关,一个密钥换用几乎全部主流模型,OpenAI 兼容;**免配置换平台**的捷径。
- **适用场景建议**:`qwen/qwen2.5-vl-72b-instruct:free`(free/cheap,目录默认)零成本初筛;`meta-llama/llama-4-scout-17b-16e-instruct:free`(free)备选;`openai/gpt-4o-mini`(cheap)付费便宜档。
- **注意事项**:**免费档限流**——`:free` 后缀的免费路由速率限制显著严于付费档(官方对免费档有每日/每分钟请求数约束),大流量扫描不要依赖免费档;响应内容由上游生成,gemini 系上游经转接时尤易出现 markdown 围栏包裹 JSON,解析统一走响应修复层兜底。`provider_quirks` 默认注入官方可选归因头 `X-Title: NetSentinel`(不覆盖调用方已设头);`HTTP-Referer` 归因头留给部署方按真实站点自行附加。数据经境外网关流转,合规评估同 gemini 一节。

### 3.14 groq

- **定位特点**:Groq 超低延迟推理(LPU),Llama 4 Scout 视觉,OpenAI 兼容。
- **适用场景建议**:`meta-llama/llama-4-scout-17b-16e-instruct`(cheap/balanced,目录默认)对延迟敏感的便宜初筛;`meta-llama/llama-4-maverick-17b-128e-instruct`(flagship)大杯。
- **注意事项**:**速率配额较小**,适合小批量快跑,不适合整站扫描主力;免费额度与限流以官方控制台为准。数据出境合规同 gemini 一节。

### 3.15 together

- **定位特点**:Together AI 开源模型托管,OpenAI 兼容,按量计费。
- **适用场景建议**:`meta-llama/Llama-4-Scout-17B-16E-Instruct`(cheap/balanced,目录默认)便宜初筛;`meta-llama/Llama-4-Maverick-17B-128E-Instruct`(flagship)复核。
- **注意事项**:模型名大小写形态与 groq 略有差异(官方目录写法),以 Together 官方模型列表为准;数据出境合规同 gemini 一节。

### 3.16 xai Grok

- **定位特点**:xAI Grok 视觉模型,OpenAI 兼容口。
- **适用场景建议**:`grok-2-vision-1212`(balanced/flagship,目录默认)主力;`grok-4`(flagship)新一代旗舰(视觉输入能力以官方为准)。
- **注意事项**:存在 `grok-2-vision`(不带日期后缀)等别名,确切可用名以官方为准;数据出境合规同 gemini 一节。

### 3.17 ollama(本地)

- **定位特点**:本地推理运行时,数据不出本机;OpenAI 兼容口默认监听 `127.0.0.1:11434`,免密钥免 `vlm_online`。
- **适用场景建议**:`llava`(local/cheap,目录默认)本地隐私初筛;`llama3.2-vision`(local/balanced)、`qwen2.5vl`(local/flagship)、`minicpm-v`(local/cheap)按机器算力选档。
- **注意事项**:**安装一句话指引**:从 ollama.com 安装后执行 `ollama pull llava` 拉取视觉模型,服务即就绪(`python -m netsentinel.vision.vlmctl ping ollama:llava` 核验)。带 tag 的模型名(如 `llava:13b`)已由 `model_catalog.validate_model` 通配支持。仍受预算一本账约束(本地算力也是成本)。

### 3.18 vllm(本地)

- **定位特点**:本地高性能推理服务(vLLM),OpenAI 兼容口默认监听 `127.0.0.1:8000`;**目录无默认模型,必须显式指定**。
- **适用场景建议**:`Qwen/Qwen2.5-VL-7B-Instruct`、`llava-hf/llava-1.5-7b-hf`(local/cheap)等 HF 路径形态模型,按显存选杯。
- **注意事项**:**安装一句话指引**:`pip install vllm` 后用 `vllm serve <HF模型路径>` 启动 OpenAI 兼容服务,模型名即启动参数里的模型路径(写成 `classifier: vllm:Qwen/Qwen2.5-VL-7B-Instruct`)。

### 3.19 lmstudio(本地)

- **定位特点**:LM Studio 桌面端自带的本地服务,OpenAI 兼容口默认监听 `127.0.0.1:1234`;**模型由用户手动加载,必须显式指定**。
- **适用场景建议**:桌面图形界面党;`qwen2.5-vl-7b-instruct` / `llava-1.5-7b-hf`(local)等按其模型目录名调用。
- **注意事项**:**安装一句话指引**:安装 LM Studio 桌面端,在界面里下载/加载一个视觉模型并开启 Local Server,即可 `classifier: lmstudio:<模型目录名>` 接入。

### 3.20 xinference(本地)

- **定位特点**:Xinference 推理框架,OpenAI 兼容口默认监听 `127.0.0.1:9997`;**模型自行部署,必须显式指定**。
- **适用场景建议**:`qwen2.5vl` / `llava` / `minicpm-v`(local)等,适合多模型统一托管的自建机。
- **注意事项**:**安装一句话指引**:`pip install xinference` 后启动 `xinference-local`,再经其界面或 CLI 拉起一个视觉模型,即可 `classifier: xinference:<模型名>` 接入。

> 本地四家共同边界:`local_gateway.probe` 只探测本机地址(127.0.0.1 / localhost / ::1),`vlmctl doctor --probe` 的本地探活即经它完成;数据不出本机(红线 16),但**选择本地不等于豁免预算**(红线 19)。

---

## 4. 密钥配置(概览)

三种途径与优先级、`set_key` 密钥环文件用法、密钥打码口径,详见 [MULTI_PROVIDER.md](MULTI_PROVIDER.md) §2。这里只给一句话:**配置项 `vlm_api_keys` > 环境变量(上表主/备顺序)> 密钥环文件 `~/.netsentinel/keys/<提供方>` 首行**;任何输出界面只显示「已配置 / 未配置」布尔。各平台控制台获取密钥的具体步骤见 docs/PROVIDER_SETUP.md(A80)。

---

## 5. 教程:新增一个提供方

V4 的设计目标是「新平台三步接入」。假设要新增一家 OpenAI 兼容的假想平台 `foo`,按契约 §4 的三个模块各改一处:

**第 1 步:提供方目录(`netsentinel/vision/providers.py`)**——在 `PROVIDERS` 字典加一项 `ProviderSpec`:

```python
"foo": ProviderSpec(
    key="foo",
    base_url="https://api.foo.example/v1",
    style="openai",                      # openai | anthropic | gemini
    default_model="foo-vision-mini",     # 提示值,以官方为准
    key_envs=["NETSENTINEL_FOO_API_KEY", "FOO_API_KEY"],  # 主/备两个
    local=False,
    notes="Foo 视觉模型,OpenAI 兼容口;模型名以官方为准。",
),
```

`parse_spec` / `resolve` / `vlmctl list` / `vlmctl doctor` / 密钥环(security/keys 经惰性加载自动切到真实目录)随即全部生效,无需改动。

**第 2 步:模型目录(`netsentinel/vision/model_catalog.py`)**——在 `MODELS` 给 `foo` 加至少 2 个 `ModelInfo`,标档位标签(cheap / balanced / flagship / local / free):

```python
"foo": [
    ModelInfo("foo-vision-mini", ("cheap", "balanced"), "轻量档,V4 目录默认;以官方为准"),
    ModelInfo("foo-vision-pro", ("flagship",), "旗舰档,复核可用;以官方为准"),
],
```

把握不足的条目同时加进 `UNCERTAIN_IDS`(键 `foo:foo-vision-mini` 形态),上线前逐条人工核验。这样 `vlmctl models foo`、`suggest("foo", "cheap")`、`validate_model("foo", ...)` 全部就位。

**第 3 步:平台特化(`netsentinel/vision/provider_quirks.py`)**——仅当该平台确有差异时,在 `QUIRKS` 加条目,四个可选键:`extra_headers`(仅收录官方文档确证的附加头)、`model_aliases`(模型参数的可用形态/别名)、`response_quirk`(响应侧怪癖)、`notes`(中文备注)。**未在官方公开文档核实的字段一律不进前三键,只写进 `notes` 并标注「以官方文档为准」**——这是 A64 的硬规矩:不杜撰接口细节。OpenAI 兼容且无差异的平台可整段跳过本步。

**收尾**:

1. 补三份测试(照 `tests/test_providers.py` / `test_model_catalog.py` / `test_provider_quirks.py` 的既有形态,离线、零外呼);
2. 若有并发使用的国内平台限流经验值,可在 A74 限速桶的 `rpm_hints` 补一条(提示值口径);
3. 人工执行一次 `python -m netsentinel.vision.vlmctl list` 与 `vlmctl ping foo:<模型>` 完成上线核验(红线 18/20);
4. 同步更新本文档 §2 总表与 §3 平台小节,以及 docs/PROVIDER_SETUP.md 的密钥获取步骤。

新提供方自动获得 V4 的全部既有保障:预算一本账(`spend_one`)、密钥打码(红线 17)、外呼双条件闸门或本地豁免(红线 16)、跨平台一致性分析(A69)。
