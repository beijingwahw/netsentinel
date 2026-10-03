# NetSentinel 全平台视觉模型使用指南(MULTI_PROVIDER)

本文是 V4「全平台视觉模型统一接入」的**使用指南**:任何提供方的视觉模型用 `classifier: 提供方:模型` 语法一套代码接入。覆盖:分类器写法、密钥配置、外呼闸门与本地豁免、跨平台 ensemble、故障转移链、`vlmctl` 诊断、限速与成本、跨平台一致性报告,以及 V4 红线 16–20 全文。

全部行为以 `CONTRACTS-V4.md` 与实际代码为准:`netsentinel/vision/providers.py`(A61 目录)、`vlm_client.py`(A62 三方言传输层)、`multi_provider.py`(A63 统一分类器)、`provider_quirks.py`(A64)、`failover.py`(A68)、`provider_agreement.py`(A69)、`netsentinel/security/keys.py`(A70)、`vlmctl.py`(A67 诊断 CLI)。本文不引入契约之外的承诺。

相关文档:[PROVIDERS.md](PROVIDERS.md)(20 家提供方总览与各平台注意事项)、[VLM_GUIDE.md](VLM_GUIDE.md)(V2 GLM 单平台深入)、[ETHICS.md](ETHICS.md)(使用红线)。

---

## 1. classifier 三种写法

`classifier`(及 `ensemble_members` 成员名)支持三种形态,**v1–v3 的全部注册名继续有效**:

| 形态 | 写法 | 解析方 |
| --- | --- | --- |
| ① 注册名 | `classifier: stub`(或 `vlm` / `failover` / `nudenet` / `clip` 等) | `classifier_base` 注册表直取 |
| ② 提供方名 | `classifier: qwen` | 未注册名称自动转交 `multi_provider.build_classifier`,模型用目录默认值(可被 `vlm_provider_models` 覆盖) |
| ③ 提供方:模型 | `classifier: openai:gpt-4o-mini` | 同上,按第一个冒号拆分,模型为显式当场选择(优先于配置覆盖) |

示例(8 条,YAML 中含冒号的值**必须加引号**,否则 `openai:gpt-4o-mini` 会被解析成映射):

```yaml
classifier: stub                        # ① 注册名:离线桩,零外呼(默认)
classifier: glm                         # ①/② 双重身份:glm 既是 v2 注册名也是目录提供方
classifier: vlm                         # ① 泛型注册名:按 cfg.vlm_provider 决定实际提供方
classifier: failover                    # ① 注册名:沿 vlm_fallback_chain 逐成员转移(见 §5)
classifier: qwen                        # ② 提供方:目录默认模型 qwen-vl-max
classifier: anthropic                   # ② 提供方:目录默认模型 claude-sonnet-4(以官方为准)
classifier: "openai:gpt-4o-mini"        # ③ 提供方:模型
classifier: "ollama:llava"              # ③ 本地提供方:免 vlm_online 免密钥(见 §3)
```

要点:

- **冒号拆分只按第一个**:`openrouter:qwen/qwen2.5-vl-72b-instruct:free` 里模型名内部的 `:free` 不受影响;
- **模型缺省解析优先级**:`提供方:模型` 的显式模型 > `cfg.vlm_provider_models[提供方]` > 目录默认模型(`providers.resolve` 三级优先级;端点同理,`vlm_provider_base_urls` 可覆盖);
- 未知提供方抛中文 `ValueError`,消息列出全部 20 个可用提供方与正确写法;
- 本地提供方中目录无默认模型的(vllm / lmstudio / xinference)必须用③显式给模型,否则报「本地提供方必须指定模型,如 ollama:llava」;
- `classifier: vlm` 与②的区别:`vlm` 是注册表里的泛型入口,实际提供方由 `cfg.vlm_provider`(默认 `glm`)决定,该字段也支持 `openai:gpt-4o-mini` 写法。

评分结果统一为 `ImageScore`:`model = "提供方:模型"`,`scores` 含 `provider` / `categories` / `reasoning`(/`confidence`);失败降级为 `nsfw_prob=0.0 + scores.error`。评分缓存键 = 模型名 + 提示词版本 + 图片 sha256,缓存命中不花预算不外呼。

---

## 2. 密钥三种配置途径与优先级

云端提供方的 API 密钥有三种途径,`security.keys.get_key(provider, cfg)` 按以下优先级解析(**先到先得,仅记录来源,绝不记录密钥本身**):

| 优先级 | 途径 | 写法 | 适用 |
| --- | --- | --- | --- |
| 1(最高) | 配置项 | `vlm_api_keys: {openai: "sk-..."}` | 临时试验;**勿提交入库** |
| 2 | 环境变量 | 目录 `key_envs` 按序逐个,首个非空生效(如 `NETSENTINEL_OPENAI_API_KEY` → `OPENAI_API_KEY`) | 推荐的生产方式 |
| 3 | 密钥环文件 | `~/.netsentinel/keys/<提供方>` 文件首行 | 多提供方集中管理,不落项目目录 |

密钥环文件用 `set_key` 写入(自动建目录、收紧权限:POSIX `chmod 600`,Windows 尝试 `icacls`;只提示路径不回显密钥):

```python
from netsentinel.security.keys import set_key, configured

set_key("openai", "<你的密钥>")     # 写入 ~/.netsentinel/keys/openai 首行
print(configured(cfg))              # {提供方: 是否已配置} 布尔表,供体检/WebUI 用
```

环境变量等效写法(以 openai 为例):

```bash
export NETSENTINEL_OPENAI_API_KEY="sk-..."   # 主;缺省时回退 OPENAI_API_KEY
```

配置项写法(config.yaml):

```yaml
vlm_api_keys:
  openai: "sk-..."        # 最高优先级,但注意不要把密钥提交进仓库
  qwen: "sk-..."
```

三条红线级口径:

- **密钥绝不入日志 / manifest / 异常消息**(红线 17):`vlmctl` 全部输出只显示「已配置 / 未配置」;`redact_keys` 会把 `vlm_api_keys` 的值打码为「前 4 位 + `****`」;
- 本地四家(ollama / vllm / lmstudio / xinference)免密钥,`key_envs` 为空,完全不参与上述解析;
- 各平台控制台获取密钥的具体步骤见 docs/PROVIDER_SETUP.md(A80)。

---

## 3. vlm_online 外呼闸门与本地提供方豁免

NetSentinel **默认零外呼**:`vlm_online: false` 是出厂默认,任何图片数据不离开本机。

**云端提供方双条件闸门**(红线 16,由 `vlm_client` 在每次外呼前强制执行):

```
外呼 ⟺ cfg.vlm_online == True  且  已解析到该提供方的 API 密钥
缺一 → VlmConfigError(中文,含提供方与缺失项)→ 上抛,不静默降级
```

`VlmConfigError` 在 ensemble 场景由成员循环**跳过该成员**(记 warning,扫描不中断);在 failover 链里则触发向下一成员转移(§5)。单图超限(`vlm_max_image_mb`,默认 8MB)跳图并 warning,不整体失败。

**本地提供方豁免**(ollama / vllm / lmstudio / xinference,目录 `local=True`):

- 免 `vlm_online` 闸门、免密钥——数据不出本机,没有"出境"可言;
- **但仍须运营者显式选择**(在配置里写 `classifier: ollama:llava`),不是自动启用;
- **仍受预算红线 19 约束**:本地算力也是成本,每次真实调用照走 `vlm_cache.spend_one` 记账。

诊断口诀:`python -m netsentinel.vision.vlmctl doctor` 一眼看清 20 家「就绪 / 待配置 / 配置错误」(§6)。

---

## 4. 跨平台 ensemble:多平台同图互评

`ensemble_members` 支持任意成员名混排——离线桩、注册名、`提供方:模型` 全部可同场:

```yaml
ensemble_members: [stub, "openai:gpt-4o-mini", "gemini:gemini-2.0-flash"]
```

语义(orchestrator + ensemble):

- 每个成员独立对整批图片 `classify_batch`,再按图片聚合为一条 `model="ensemble"` 的加权平均(权重 `weights.get(成员名, 1.0)`,缺省等权);
- 成员未就位 / 构造失败 / 批量评分失败 → 记 warning 跳过,**不拖垮整轮扫描**;至少一个成员成功即可;
- `stub` 放在链里是离线保底:云端两家全挂时仍有兜底评分,扫描不空转。

**预算一本账**(红线 19):任何真实 VLM 外呼——无论哪个成员、是否 failover 第二跳——都走同一个 `vlm_cache.spend_one`,共享 `vlm_daily_budget`(默认每日 200 次)上限,超限上抛绝不静默超支;缓存命中(键 = 模型名 + 提示词版本 + 图片 sha256)不花预算。三个成员 × 100 张图,最坏情况就是 300 次记账,一本账管到底。

**rank_for_vlm 排序**:预算有限时,别把调用浪费在高置信图上。`netsentinel.intel.active_learn.rank_for_vlm(image_scores, budget)` 把 ensemble 评分按不确定度 **|p − 0.5| 升序**(边缘优先)排序并截取预算条数——最接近五五开的图信息量最大,优先送 VLM 复核;高置信正/负样本留给人或直接跳过。该函数纯排序、零外呼,外呼与扣减仍由 vlm_cache / orchestrator 统一管辖:

```python
from netsentinel.intel.active_learn import rank_for_vlm
top = rank_for_vlm(report.image_scores, budget=20)   # 本轮最多送审 20 张
```

多平台互评之后,用 §8 的一致性报告检查谁偏松谁偏严。

---

## 5. failover 故障转移链

`classifier: failover`(注册名)沿 `vlm_fallback_chain` 逐成员尝试,是「GLM 挂了自动切 OpenAI,再挂切本地 Ollama」的可用性保险丝:

```yaml
classifier: failover
vlm_fallback_chain: ["glm:glm-5.3-flash", "openai:gpt-4o-mini", "ollama:llava"]
```

语义(`failover.FailoverClassifier`):

- **逐成员尝试,成功即返回**:成功分标注 `model="failover→{胜者名}"`,`scores.fallback_from` 为先前失败成员名列表(首位即成功时为空表);
- **配置错跳过,不重试**:`VlmConfigError`(密钥缺失、`vlm_online` 未开)与 `ModelNotFoundError`(模型不存在)属于「换一家就能解决」的问题,记入 `fallback_from` 后直接下一成员;
- **全败上抛**:链尽时有非配置异常(网络错误等)→ 上抛**最后一个**非配置异常;全为配置错 → 抛 `VlmConfigError` 语义的中文汇总(缺什么、查什么,如「请检查 vlm_online 开关、vlm_api_keys 各提供方密钥与 vlm_fallback_chain 链配置」);空链在构造期即抛中文 `ValueError`;
- 成员未抛异常但返回带 `scores.error` 的降级分,同样视为失败转下一家;
- **每张图独立走完整条链**:同一批内不同图片允许落到不同成员(前一张击穿到备用,不代表后一张绕开主成员);
- **预算一本账**:每次真实外呼由各成员分类器自行 `spend_one`——failover 不绕过、不重复扣,也不做二级缓存(成员自身的评分缓存已兜底)。

链成员写法就是 §1 的②③形态,可混搭云端与本地;去重按 spec 精确匹配,重复项自动去重。链合法性可用 `vlmctl doctor` 逐条检查(§6)。

---

## 6. vlmctl 诊断 CLI 四命令

`vlmctl` 是运营者的螺丝刀,独立于扫描管线之外。排查三连:**配了没**(list / doctor)、**通不通**(ping)、**有哪些模型**(models)。除 `ping`(及 `doctor --probe` 中的探测)外**全部零外呼**;`ping` 是唯一允许外呼的诊断动作,且只能由人工显式敲下(红线 20)。密钥只显示「已配置 / 未配置」,绝不回显(红线 17)。

### 6.1 `list`——提供方总表(零外呼)

```bash
python -m netsentinel.vision.vlmctl list
```

输出 20 家的表格:提供方 / 方言 / 默认模型(有 `vlm_provider_models` 覆盖时标注「(cfg 覆盖)」)/ 本地 / 密钥(✓已配置 / ✗ / 免密钥)。末行提示:默认模型为目录提示值,以各平台官方文档为准(上线前核验一次)。

### 6.2 `models`——某提供方的模型目录(零外呼)

```bash
python -m netsentinel.vision.vlmctl models glm
```

列出 `model_catalog.MODELS[glm]` 全部条目:模型名 / 档位标签([cheap, balanced] 等)/ 中文备注,`★` 标注当前默认模型(区分「目录提示值」与「cfg 覆盖」来源);无默认模型的提供方会提示「使用时必须显式指定,如 {提供方}:<模型名>」。

### 6.3 `ping`——真发一次评分请求(唯一外呼命令)

```bash
python -m netsentinel.vision.vlmctl ping openai:gpt-4o-mini          # 真实外呼一次
python -m netsentinel.vision.vlmctl ping ollama:llava --offline     # 只查配置,零外呼
python -m netsentinel.vision.vlmctl ping glm --image data/evidence/x.png   # 用真实图片代替 1x1 测试图
```

真实 ping 的流程与输出:目标行(提供方 / 模型 / 方言 / 端点 / 云端或本地)→ 图片(缺省自动生成 1×1 PNG)→ **预算提示(本次外呼已计入 vlm_cache 记账,红线 19,记账失败即拒绝外呼)** → 延迟(ms)→ 模型回显 → `nsfw_prob` → 解析是否成功 → 结论。解析成功输出「✅ 链路可用」(退出码 0);链路可达但返回不合 schema 输出「❌ 检查模型名/方言/提示词」(退出码 1)。`--offline` 只做配置检查:本地提供方直接就绪;云端需「密钥已配置 且 vlm_online=True」,否则明确指出缺什么。

### 6.4 `doctor`——全提供方配置体检(默认零外呼)

```bash
python -m netsentinel.vision.vlmctl doctor           # 纯本地体检
python -m netsentinel.vision.vlmctl doctor --probe   # 追加:本地网关探活 + 一次云端 ping
```

体检内容:全局摘要(`vlm_online` / `vlm_provider` / 超时 / 单图上限 / fallback 链)→ 20 家逐行表格(提供方 / 方言 / 密钥布尔 / cfg 覆盖了哪些项——只列项名 / 状态 ✅⚠️)→ cfg 覆盖项指向未知提供方判 ❌ → fallback 链逐条 `parse_spec` 合法性 → 结论统计(就绪 / 待配置 / 配置错误,有 ❌ 则退出码 1)。

`--probe` 追加两段真实探测(同样仅人工显式触发):本地推理网关探活(经 `local_gateway`,只探 127.0.0.1 的 `/v1/models`,四家本地服务在线与否、各自加载了哪些模型一目了然)+ 一次云端 ping(目标取 fallback 链首位,未配置链则取 `vlm_provider`)。

全部命令支持 `--config <路径>` 指定配置文件(缺省读 `./config.yaml`,不存在用默认配置)。

---

## 7. 限速桶与成本计量(提示值口径)

多平台混跑有两件事要管:**别撞限流**、**知道花了多少钱**。V4 按 `CONTRACTS-V4` §4 A74/A75 的口径提供两个机制(均以实际合入实现为准):

**每提供方限速桶(`provider_throttle.ProviderThrottle`,A74)**:构造时可传 `rpm_hints: dict[提供方 → 每分钟请求数]`(**默认 60,提示值**);`acquire(provider)` 令牌桶式阻塞等待——桶空则 sleep 到令牌恢复,线程安全,`state(provider)` 可观测当前水位。提示值口径:60 RPM 只是占位,各平台真实限流差异极大(groq 配额偏小、ernie 千帆在 `X-Ratelimit-*` 响应头透出 RPM/TPM、openrouter 免费档另行收紧),**以各平台官方限流文档为准**(红线 18),按需在自己的配置/脚本里传入真实档位。

**成本计量(`cost_meter.CostMeter`,A75)**:`PRICE_HINTS` 按 `"提供方:模型"` 键给出每千次调用的价格**提示值**(缺失返回 `None`,绝不猜测);`record(provider, model, images)` 追加 jsonl 流水(`{"ts","provider","model","images"}`);`estimate()` 按提示价估算;`summary()` 按提供方汇总 `{calls, images, est_cost}`。提示值口径:**一切估算以平台实际账单为准**——模型更名、档位调整、图片分辨率折算都会让单价漂移,`summary` 的数字用于相对比较与异常检测,不作为对账依据。

与预算一本账的关系:`vlm_daily_budget`(次数口径,红线 19 的硬闸)管「还能不能打」,限速桶管「此刻该不该打」,成本计量管「打完了大概多少钱」。本地提供方免前两者的云端语义但记账照走——本地算力也是成本。

---

## 8. 跨平台一致性报告怎么读

多平台 ensemble 跑完后,`provider_agreement.analyze(scores)`(A69)回答三个问题:**谁偏松、谁偏严、哪张图争议大**。输入是多平台成员的 `ImageScore` 列表(兼容 A56 agreement_matrix 的输入形态);提供方名从 `ImageScore.model` 还原(`openai:gpt-4o-mini → openai`;`failover→glm:... → failover`;`stub` 等注册名原样)。仅统计同一张图上有 **≥2 个不同提供方**评分的组;共识 = 逐图全部评分的均值。

```python
from netsentinel.vision.provider_agreement import analyze
report = analyze(all_member_scores)   # 扫描管线外/复核台内离线计算,零外呼
```

输出五个字段,读法如下:

| 字段 | 含义 | 怎么用 |
| --- | --- | --- |
| `providers` | 参与分析的提供方(去重排序) | 确认本批实际参与了几家 |
| `per_image_spread` | 每图 `{image, max, min, spread}`,spread = max−min,**按争议度降序,最多 20 条** | 排头就是**争议最大的图**,人工复核优先看这些 |
| `bias` | 每提供方逐图「分值 − 共识」的均值:**> 0 系统性偏松(打分偏高),< 0 系统性偏严** | 绝对值排序即"哪家最不可信";调 ensemble 权重时的依据 |
| `outliers` | `|bias| > 0.15` 的提供方,附中文建议「该提供方系统性偏高/偏低,建议人工抽检其评分样本」 | 出现在这里 = 触发人工抽检动作,或降权/暂停该成员 |
| `pair_agreement` | 每对提供方在同图上 `|a−b| ≤ 0.2` 的占比(0~1) | 两家一致率长期高 → 冗余可精简;长期低 → 至少一家标定有偏 |

典型动作:某成员反复进 `outliers` → `vlmctl ping <该成员>` 复核链路与模型名 → 人工抽检其高分/低分样本 → 确认有偏后在 ensemble 权重里降权或从成员表移除。复核台(WebUI)提供方面板会把这些表格化展示。

---

## 9. V4 红线 16–20 全文重述

以下五条叠加在既有 15 条红线之上,是本文全部机制的安全底座(`CONTRACTS-V4.md` §0 原文):

> 16. **默认零外呼不变**;本地提供方(ollama/vllm/lmstudio/xinference)数据不出本机,免 `vlm_online` 闸门但仍须运营者显式选择;云端提供方沿用 `vlm_online=True + 密钥` 双条件。
>
> 17. **密钥绝不入日志/manifest/异常消息**(统一经 `security.redact` 口径;请求头只在 debug 级打印打码后形式)。
>
> 18. **目录里的模型名/端点是提示信息**:以各平台官方文档为准,全部可用 `vlm_provider_models/base_urls` 覆盖;文档必须写明"上线前核验一次"。
>
> 19. **跨平台共用一本预算账**:任何真实 VLM 外呼(含 failover 第二跳、多平台 ensemble 成员)都走 `vlm_cache.spend_one`。
>
> 20. **ping/doctor 是唯一允许外呼的诊断动作**,且只能由人在 `vlmctl` 里手动触发;常规扫描/测试零外呼。

一句话总结:模型与端点都是提示值(18),上线前人工 `ping` 核验一次(20);密钥只藏在三种配置途径里、任何输出不回显(17);打出去的每一次调用都有闸门(16)和一本账(19)。
