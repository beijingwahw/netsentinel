# NetSentinel V4 团队契约(A61–A80 并行升级)—— 与 v1/V2/V3 契约共同生效

> 前三轮 60 个模块(A01–A60)全部就位,1255 测试全绿。
> V4 主题:**全平台视觉模型统一接入**——任何提供方的视觉模型都能用 `classifier: 提供方:模型` 语法接入。

## 0. V4 新红线(叠加在既有 15 条之上)

16. **默认零外呼不变**;本地提供方(ollama/vllm/lmstudio/xinference)数据不出本机,免 `vlm_online` 闸门但仍须运营者显式选择;云端提供方沿用 `vlm_online=True + 密钥` 双条件。
17. **密钥绝不入日志/manifest/异常消息**(统一经 `security.redact` 口径;请求头只在 debug 级打印打码后形式)。
18. **目录里的模型名/端点是提示信息**:以各平台官方文档为准,全部可用 `vlm_provider_models/base_urls` 覆盖;文档必须写明"上线前核验一次"。
19. **跨平台共用一本预算账**:任何真实 VLM 外呼(含 failover 第二跳、多平台 ensemble 成员)都走 `vlm_cache.spend_one`。
20. **ping/doctor 是唯一允许外呼的诊断动作**,且只能由人在 `vlmctl` 里手动触发;常规扫描/测试零外呼。

## 1. 新增 Config 字段(已落地 contracts.py,禁改)

`vlm_provider("glm") / vlm_api_keys{提供方→密钥} / vlm_provider_base_urls{提供方→覆盖} / vlm_provider_models{提供方→默认模型覆盖} / vlm_fallback_chain(["glm:glm-5.3-flash","openai:gpt-4o-mini"]) / vlm_request_timeout_s(90) / vlm_max_image_mb(8)`

分类器语法(v1-v3 名称全部继续有效):`classifier: <注册名>` 或 **`classifier: <提供方>` / `classifier: <提供方>:<模型>`**,如 `qwen:qwen-vl-max`、`anthropic`、`ollama:llava`。`get_classifier` 已由负责人改造:未注册名称自动转交 `netsentinel.vision.multi_provider.build_classifier(name, cfg)`。

## 2. 提供方目录规范(权威清单,写进 providers.py)

三种 API 方言:`openai`(POST {base}/chat/completions,image_url=dataURI)、`anthropic`(POST {base}/messages,content 块 type=image/source.base64,system 顶层,max_tokens 必填)、`gemini`(POST {base}/models/{model}:generateContent,parts[].inline_data,generationConfig.response_mime_type=application/json,标头 x-goog-api-key)。

| 提供方 | base_url | 方言 | 默认模型(提示值) | 密钥环境变量 |
| --- | --- | --- | --- | --- |
| glm 智谱 | https://open.bigmodel.cn/api/paas/v4 | openai | glm-5.3-flash | NETSENTINEL_GLM_API_KEY / GLM_API_KEY |
| openai | https://api.openai.com/v1 | openai | gpt-4o-mini | NETSENTINEL_OPENAI_API_KEY / OPENAI_API_KEY |
| anthropic | https://api.anthropic.com/v1 | anthropic | claude-sonnet-4(以官方为准) | NETSENTINEL_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY |
| gemini | https://generativelanguage.googleapis.com/v1beta | gemini | gemini-2.0-flash | NETSENTINEL_GEMINI_API_KEY / GEMINI_API_KEY |
| qwen 通义 | https://dashscope.aliyuncs.com/compatible-mode/v1 | openai | qwen-vl-max | NETSENTINEL_QWEN_API_KEY / DASHSCOPE_API_KEY |
| doubao 豆包 | https://ark.cn-beijing.volces.com/api/v3 | openai | doubao-1.5-vision-pro(或推理接入点 ID) | NETSENTINEL_DOUBAO_API_KEY / ARK_API_KEY |
| hunyuan 混元 | https://api.hunyuan.cloud.tencent.com/v1 | openai | hunyuan-vision | NETSENTINEL_HUNYUAN_API_KEY |
| moonshot Kimi | https://api.moonshot.cn/v1 | openai | kimi-latest | NETSENTINEL_MOONSHOT_API_KEY / MOONSHOT_API_KEY |
| minimax | https://api.minimax.chat/v1 | openai | MiniMax-VL-01(以官方为准) | NETSENTINEL_MINIMAX_API_KEY |
| stepfun 阶跃 | https://api.stepfun.com/v1 | openai | step-1v-8k | NETSENTINEL_STEPFUN_API_KEY |
| siliconflow 硅基流动 | https://api.siliconflow.cn/v1 | openai | Qwen/Qwen2.5-VL-7B-Instruct | NETSENTINEL_SILICONFLOW_API_KEY |
| ernie 文心 | https://qianfan.baidubce.com/v2 | openai | ernie-4.5-vl(以官方为准) | NETSENTINEL_ERNIE_API_KEY / QIANFAN_API_KEY |
| openrouter | https://openrouter.ai/api/v1 | openai | qwen/qwen2.5-vl-72b-instruct:free | NETSENTINEL_OPENROUTER_API_KEY / OPENROUTER_API_KEY |
| groq | https://api.groq.com/openai/v1 | openai | meta-llama/llama-4-scout-17b-16e-instruct | NETSENTINEL_GROQ_API_KEY / GROQ_API_KEY |
| together | https://api.together.xyz/v1 | openai | meta-llama/Llama-4-Scout-17B-16E-Instruct | NETSENTINEL_TOGETHER_API_KEY / TOGETHER_API_KEY |
| xai Grok | https://api.x.ai/v1 | openai | grok-2-vision-1212 | NETSENTINEL_XAI_API_KEY / XAI_API_KEY |
| ollama(本地) | http://127.0.0.1:11434/v1 | openai | llava | (免密钥,local=True) |
| vllm(本地) | http://127.0.0.1:8000/v1 | openai | (必填模型) | (免密钥,local=True) |
| lmstudio(本地) | http://127.0.0.1:1234/v1 | openai | (必填模型) | (免密钥,local=True) |
| xinference(本地) | http://127.0.0.1:9997/v1 | openai | (必填模型) | (免密钥,local=True) |

`local=True` 的提供方:免 vlm_online、免密钥,但仍受预算红线 19 约束(成本为本地算力,spend_one 仍记账)。

## 3. 文件归属(A61–A80)

```
netsentinel/vision/providers.py  tests/test_providers.py            # [A61] 目录+parse_spec+resolve
netsentinel/vision/vlm_client.py  tests/test_vlm_client.py          # [A62] 统一传输层(三方言)
netsentinel/vision/multi_provider.py  tests/test_multi_provider.py  # [A63] 统一分类器+build_classifier
netsentinel/vision/provider_quirks.py  tests/test_provider_quirks.py  # [A64] 国内平台特化
netsentinel/vision/model_catalog.py  tests/test_model_catalog.py    # [A65] 模型目录与推荐
netsentinel/vision/local_gateway.py  tests/test_local_gateway.py    # [A66] 本地推理网关
netsentinel/vision/vlmctl.py  tests/test_vlmctl.py                  # [A67] 诊断 CLI(list/ping/models/doctor)
netsentinel/vision/failover.py  tests/test_failover.py              # [A68] 故障转移路由
netsentinel/vision/provider_agreement.py  tests/test_provider_agreement.py  # [A69] 跨平台一致性分析
netsentinel/security/keys.py  tests/test_keys.py                    # [A70] 多平台密钥环
netsentinel/vision/model_negotiate.py  tests/test_model_negotiate.py  # [A71] 模型名协商回退
netsentinel/vision/prompt_dialects.py  tests/test_prompt_dialects.py  # [A72] 请求方言适配
netsentinel/vision/response_repair.py  tests/test_response_repair.py  # [A73] 跨家族 JSON 修复语料
netsentinel/vision/provider_throttle.py  tests/test_provider_throttle.py  # [A74] 每提供方限速桶
netsentinel/vision/cost_meter.py  tests/test_cost_meter.py          # [A75] 成本计量
webui/providers_page.py  tests/test_providers_page.py               # [A76] 复核台提供方面板
benchmarks/providers.py  tests/test_provider_bench.py               # [A77] 跨平台基准(模拟)
tests/test_multi_provider_e2e.py  scripts/demo_multi_provider.py    # [A78] 端到端+演示
docs/PROVIDERS.md  docs/MULTI_PROVIDER.md                           # [A79] 用户文档
docs/UPGRADE_V4.md  CHANGELOG.md  docs/PROVIDER_SETUP.md            # [A80] 升级文档+密钥获取指南
```

## 4. 模块 API 规范(签名固定)

- **A61 providers.py**:`@dataclass ProviderSpec(key, base_url, style ∈ openai|anthropic|gemini, default_model, key_envs: list[str], local: bool=False, notes="")`;`PROVIDERS: dict[str, ProviderSpec]`(§2 全部 20 项);`parse_spec(name) -> tuple[provider, model|None]`(":" 拆分,未知提供方 ValueError 中文列出可用项);`resolve(provider, cfg) -> ResolvedProvider(base_url, model, api_key, style, local)`:优先级 cfg.vlm_provider_models/base_urls/api_keys → 目录;密钥解析委托 `security.keys.get_key`(未就位时按 key_envs 顺序读 os.environ);本地提供方 model 为空且目录也无默认 → ValueError 中文"必须指定模型,如 ollama:llava"。
- **A62 vlm_client.py**:`class VlmConfigError(RuntimeError)`(中文,含提供方与缺失项);`class UniversalVLMClient`:`__init__(self, resolved, cfg, *, transport=None)`;`chat_json(messages, *, image_paths=None) -> dict`:按 style 构造请求(openai:复用 glm_adapter 语义泛化;anthropic:system 顶层/messages content=[{type:text},{type:image,source:{type:base64,media_type,data}}],max_tokens=1024;gemini:URL `{base}/models/{model}:generateContent`,contents=[{role:user,parts:[{text},{inline_data:{mime_type,data}}]}],systemInstruction,generationConfig={responseMimeType:"application/json",temperature:0.1},header x-goog-api-key);响应分别取 choices[0].message.content / content[0].text / candidates[0].content.parts[*].text 拼接;非 dict 内容 → 经 vlm_prompts.parse_json_response(未就位用内置);超时 cfg.vlm_request_timeout_s,URLError 重试 1 次;模型不存在(400/404/错误体含 model 字样)→ `ModelNotFoundError`(本模块定义);**外呼前置**:非 local 且 (非 vlm_online 或无 key) → VlmConfigError;单图 > cfg.vlm_max_image_mb → 跳图并 warning;密钥不落日志。transport 注入签名自定,测试零外呼。
- **A63 multi_provider.py**:`build_classifier(name: str, cfg) -> UniversalVLMClassifier`(parse_spec→resolve→构造;name 含 ":" 或为目录提供方名;VlmConfigError 透传给上层按成员跳过);`class UniversalVLMClassifier(NsfwClassifier)`:name=f"{provider}:{model}" 或 provider;`__init__(cfg, *, resolved=None, client=None, cache=None)`;`classify(img)`:缓存(vlm_cache,键=provider:model+PROMPT_VERSION+img sha)→miss→spend_one→client.chat_json(vlm_prompts IMAGE_SCORING 或内置)→validate+calibrate→ImageScore(model=self.name, scores 含 provider/categories/reasoning);失败→0.0+scores.error;离线(VlmConfigError)上抛由成员循环跳过;local 提供方照常。模块导入时 `register_classifier("vlm", UniversalVLMClassifier)`(泛型入口,用 cfg.vlm_provider)。
- **A64 provider_quirks.py**:`QUIRKS: dict[str, dict]`:每提供方可选 {extra_headers(如 openrouter 需 HTTP-Referer/X-Title 建议)、model_aliases(doubao endpoint-id 别名说明)、response_quirk(gemini 偶发围栏、moonshot 长文截断建议 max_tokens 提示)、notes};`apply_quirks(provider, headers: dict, payload: dict) -> (headers, payload)`;`quirk_notes(provider) -> str`(中文)。可用 WebFetch 尽力核对各平台公开文档(失败不阻塞,标 TODO)。测试:quirks 应用/无 quirk 提供方透传/未知提供方透传。
- **A65 model_catalog.py**:`MODELS: dict[str, list[ModelInfo]]`(ModelInfo: id, tags ∈ {cheap,balanced,flagship,local,free}, note);覆盖 §2 各提供方 ≥2 个视觉模型(不确定的标 note="以官方为准");`suggest(provider, need="balanced") -> str|None`;`validate_model(provider, model) -> bool`(在目录或提供方支持通配如 ark endpoint-id/开源路径格式);`search(keyword) -> list[ModelInfo]`。测试齐。
- **A66 local_gateway.py**:`probe(base_url, timeout=2) -> {"ok":bool,"models":[str],"error":中文}`(GET {base}/models,urllib,仅当调用方显式调用;ovllm/lmstudio/xinference 同一 OpenAI 兼容口);`is_local(provider) -> bool`(查目录 local);`local_status(cfg) -> dict`(对 4 个本地提供方 probe 汇总,给 vlmctl doctor 用)。测试:本地 http.server mock /v1/models(ok/404/超时)。
- **A67 vlmctl.py**:`main(argv) -> int`:`list`(表格:提供方/方言/默认模型/本地/密钥已配?——密钥只显示 已配置/未配置,绝不回显)、`models <provider>`、`ping <provider[:model]>`(真发一次 1x1 PNG 评分请求,显式人工诊断,输出延迟/解析成功/模型回显;--offline 只做配置检查)、`doctor`(全提供方配置体检+本地 probe,默认零外呼除非 --probe);`python -m netsentinel.vlmctl`。测试:mock transport 全子命令。
- **A68 failover.py**:`class FailoverClassifier(NsfwClassifier)` 注册 "failover":`__init__(cfg, *, builders=None, cache=None)`;链=cfg.vlm_fallback_chain(空链→ValueError 中文);`classify(img)`:逐 spec build(builders 可注入 dict spec→classifier)→classify;每真实外呼已由各分类器 spend_one(一本账);成功即返回(标注 model="failover→{胜者}",scores.fallback_from);全链失败→最后异常上抛;VlmConfigError 视为"跳过该成员"继续下一 spec,全为配置错→VlmConfigError。测试:mock builders 三成员(第一个抛/第二个成功;全失败;配置错跳过)。
- **A69 provider_agreement.py**:`analyze(scores: list[ImageScore]) -> {"providers":[...],"per_image_spread":[{image,max,min,spread}],"bias":{provider:平均偏离共识}, "outliers":[{provider,delta,note中文}], "pair_agreement":{(a,b):rate}}`:共识=逐图均值;bias=均值-共识的均值;|bias|>0.15 → outlier(中文注"该提供方系统性偏高/偏低,建议人工抽检");兼容 A56 agreement_matrix 输入(ImageScore 列表)。测试:合成三方分数。
- **A70 keys.py**:`get_key(provider: str, cfg) -> str`:cfg.vlm_api_keys → 目录 key_envs 逐个 os.environ → `~/.netsentinel/keys/<provider>` 文件首行;`set_key(provider, key, cfg_dir="~/.netsentinel/keys")`(写文件+权限收紧,echo 提示也可用 env);`configured(cfg) -> dict[provider, bool]`;`redact_keys(d)`(值→前4+****)。测试:三来源优先级/tmp 家目录/权限分支(monkeypatch os.name)/redact。
- **A71 model_negotiate.py**:`negotiate(provider, model, cfg, *, transport=None) -> str`:发送极小文本探测请求;ModelNotFoundError 时按链重试:cfg.vlm_provider_models[provider] → 目录 default_model → model_catalog suggest(provider,"cheap") → 兼容别名表(quirks);成功返回定格模型;全败抛 RuntimeError 中文。进程内缓存(模块级 dict)。测试:mock transport 404→404→200 链。
- **A72 prompt_dialects.py**:`CAPS: dict[style, {"json_mode":bool,"system_top":bool}]`;`build_request(style, provider, system, user, images: list[tuple[bytes,mime]]) -> dict`(返回 {url,headers,payload} 三方言正确形态;openai 用 response_format json_object,anthropic system 顶层+max_tokens,gemini responseMimeType);`parse_response(style, body) -> str|None`(三种响应形态提取文本);未知 style ValueError。测试:三方言请求/响应形态断言(json 字段、图片块、头)。
- **A73 response_repair.py**:`CORPUS: list[Case]`(≥80 条:纯 JSON/围栏/前后杂文/单引号/尾逗号/True False/python None/中文引号包键/gemini 风格多段文本夹 JSON/gpt 风格"以下是JSON:"/截断 JSON 检测(不修复,返回 None)/注入语样例(修复后仍只取 nsfw_prob));`repair(text) -> dict|None`(先 vlm_prompts.parse_json_response,未就位内置;截断检测:括号不平衡→None);`run_corpus() -> {"total","passed"}`。测试:全语料通过率 ≥95%、截断返回 None、注入样例字段提取不受影响。
- **A74 provider_throttle.py**:`class ProviderThrottle`:构造 `(rpm_hints: dict[str,int] 默认 60)`;`acquire(provider) -> None`(令牌桶,不足则 sleep 等待,时间注入 now=None 参数);线程安全;`state(provider)`。测试:时钟注入模拟等待/并发 acquire 不超速。
- **A75 cost_meter.py**:`PRICE_HINTS: dict["provider:model", float]`(每千次调用人民币/美元提示值,标注"提示值,以账单为准";缺失→None);`class CostMeter(state_path)`:jsonl 记账 {"ts","provider","model","images"};`estimate(provider, model, images) -> float|None`;`record(provider, model, images)`;`summary() -> {provider: {calls,images,est_cost}}`。测试:记账/汇总/无价格提示为 None。
- **A76 webui/providers_page.py**:纯逻辑层 `provider_rows(cfg) -> list`(目录+configured+local 标记,密钥布尔化)、`ping_row(ping_result) -> dict`、`agreement_rows(analysis: dict) -> list`(A69 输出表格化);Streamlit 页(惰性):提供方总表、本地网关状态(local_status)、ping 按钮(手动)、一致性表。tests/test_providers_page.py 只测纯函数。
- **A77 benchmarks/providers.py**:`run(out_dir, fixtures: list[str] = 内置 ≥6 条各家族风格响应样例) -> dict`:对每样例走 response_repair.repair + validate_image_json,统计各"家族"(openai/anthropic/gemini/其他)解析成功率、schema 合规率、平均修复深度;产出 providers_report.md/json(中文,注明"模拟语料,真实差异以 vlmctl ping 为准")。测试离线。
- **A78 e2e+demo**:tests/test_multi_provider_e2e.py:对目录**每个提供方**构造 spec(默认模型),monkeypatch vlm_client 传输层返回合法 JSON → build_classifier(name) → classify(demo 站 nsfw_hi 图)断言 nsfw_prob>0.9 且 model 名含提供方;failover 链端到端(mock);ensemble_members=[stub,"openai:gpt-4o-mini"] 混编走 run_scan(注入 capture)NSFW 判定。scripts/demo_multi_provider.py:中文演示:目录清单→(mock)三平台评分对比→failover 演示→agreement 报告→"全程零外呼"收尾。
- **A79 文档**:docs/PROVIDERS.md(提供方总表:端点/默认模型/密钥环境变量/方言/备注,"模型名以官方为准";每平台一节:特点/适用/注意事项);docs/MULTI_PROVIDER.md(使用指南:classifier 语法、vlm_api_keys 配置、fallback_chain、跨平台 ensemble、本地 Ollama 快速上手、vlmctl 诊断、限速与成本、红线 16-20)。
- **A80 文档**:docs/UPGRADE_V4.md(A61–A80 总览表+数据流 mermaid:classifier spec→providers.resolve→vlm_client 方言→vlm_cache/cost/throttle→multi_provider→ensemble/failover→fusion;与 v1-v3 兼容)、CHANGELOG.md(四轮演进总账:各轮模块/测试数/红线编号)、docs/PROVIDER_SETUP.md(每平台密钥获取步骤指引:控制台入口/环境变量写法/权限注意;仅流程指引,不涉密钥值)。

## 5. 接线(负责人集成期完成)

- classifier_base 已支持 spec 转交(见 §1);vlmctl 独立 CLI;A63 的 "vlm" 泛型注册接 cfg.vlm_provider。
- orchestrator 无需改动(ensemble_members 已支持任意成员名)。

## 6. 测试规则:同前(离线/mock/importorskip/零外呼/不碰真实门户;ping 仅 mock)。
