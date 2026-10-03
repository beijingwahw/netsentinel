# NetSentinel V7 内核进化白皮书(docs/KERNEL_EVOLUTION.md)

> 负责人编号:A141。依据:《CONTRACTS-V7.md》(A123–A142 并行开发契约)、
> 各内核模块 docstring 与 `kernel_selfcheck()` 实现、各测试文件中的
> `test_v7_bench_*` 断言。文中标注"实测"的数字来自 2026-10-02 在本仓库
> 逐个运行 `kernel_selfcheck()` 的真实输出,非臆造。
>
> 状态说明:截至本文落笔,A123–A137 共 15 个工号(16 个新增内核文件)
> 已就位;
> A138(`benchmarks/kernel_bench.py`)、A139(`pipeline/kernel_wire.py`)、
> A140(演示脚本)按契约为并行开发任务,尚未合入本仓库,相关章节
> (③ 与 ⑤ 的装配线部分)以契约原文与兄弟模块 docstring 中的引用为据,
> 并明确标注,待其合入后数值以实际运行为准。

---

## ① 开篇:什么是"内核进化"

### 1.1 引擎代际,不是又一次工程打磨

V5(见 CHANGELOG「V5.0 全模块世界级工程提升」)的做法是:**算法一字不动,
工程全面加固**——sqlite 全线 WAL+busy_timeout、批量接口、热点 LRU、断路器、
零依赖遥测打点 100+、类型注解与常量化。它回答的问题是"同一算法能不能跑得
更快、更稳、更可观测"。V6/V6.5 延续该路线(批量流水线、线索发现)。

V7 的"内核进化"回答的是另一个问题:**把算法本身换掉**。八条核心链路
(识别/语言/决策/融合/检索/图谱/执行/调度)各自引入一个新的引擎内核,
旧机制与新内核是两代算法的关系,而不是同一算法的快慢两档:

| 维度 | V5 工程提升 | V7 内核进化 |
| --- | --- | --- |
| 改动对象 | 既有 85 个源文件的实现质量 | 新增 15 个内核文件,算法换代 |
| 代差证据 | 计时类量化(如缓存批量写 33.5x) | **操作计数/复杂度断言**(红线 31,禁墙钟) |
| 默认行为 | 即刻生效 | **默认关闭/开关化,旧调用方零感知**(红线 29) |
| 学习能力 | 无(纯工程) | 学习型内核只从**本地反馈**学习(红线 30) |

三条 V7 新红线(《CONTRACTS-V7.md》§0,累计第 29–31 条)是全部内核的
宪法,后面每一章都会回扣:

> **29. 零 API 破坏**:新内核全部为新增文件/新增 keyword-only 参数/新增
> 注册名;既有模块(含 A01–A122 全部文件)一律不改;开关默认值 = 旧行为。
>
> **30. 学习型内核零外呼**:可靠性加权/文本 n-gram 等只从运营者本地反馈
> (review_queue 决策)与内置合成语料学习;绝不联网取数、绝不调用 VLM 训练。
>
> **31. 基准可复现**:每个新内核在其测试文件中至少一个 `test_v7_bench_*`
> 用例,以**操作计数/复杂度断言**(循环次数、调用次数、表扫描行数)或
> 确定性构造数据上的精确结果证明代差;禁止依赖墙钟的脆弱断言。

### 1.2 开关矩阵:全部 V7 Config 字段

7 个新增 Config 字段(《CONTRACTS-V7.md》§1;定义见
`netsentinel/contracts.py` 309–315 行,校验见 `netsentinel/config.py`):

| Config 字段 | 默认值 | 约束 | 作用 | 对应内核(文件) |
| --- | --- | --- | --- | --- |
| `use_sprt` | `False` | 布尔 | 决策内核开关:逐张送审 + SPRT 判停早停 | `decision/sprt.py`(A125) |
| `sprt_alpha` | `0.05` | ∈(0, 0.5) | SPRT 第一类错误上限(误判 NSFW) | `decision/sprt.py` |
| `sprt_beta` | `0.05` | ∈(0, 0.5) | SPRT 第二类错误上限(漏判 CLEAN) | `decision/sprt.py` |
| `use_reliability_fusion` | `False` | 布尔 | 融合内核开关:图像侧按提供方可靠性(Brier 反比)加权 | `decision/reliability.py` + `decision/fusion_reliable.py`(A126) |
| `phash_lsh_bands` | `4` | ∈[1, 8] | 检索内核分带数(4 带 × 16bit 桶键) | `intel/phash_lsh.py`(A127) |
| `browser_session_reuse` | `True` | 布尔 | 执行内核:批量执行复用一次浏览器会话 | `submit/executor_session.py`(A129) |
| `sched_priority` | `False` | 布尔 | 调度内核:优先级 + 预算轮选(替代 FIFO 顺序) | `ops/sched_kernel.py`(A130) |

要点:

- **除 `browser_session_reuse` 外全部默认关**。`SessionExecutor` 是新增类,
  既有代码不显式构造它就永远是旧行为,因此该开关默认 `True` 不违反
  红线 29(开关的消费方是装配线 A139,见 ⑤)。
- 识别内核("skin")、语言内核、指纹 v2、并查集、风格、安全、指标导出等
  **不设 Config 开关**:它们要么是新增注册名/新增类,调用方不点名就不生效;
  要么是纯新增基础设施,旧路径根本不经过。
- 校验兜底:`sprt_alpha/sprt_beta` 越出 (0, 0.5)、`phash_lsh_bands` 越出
  1~8 时,`config` 启动即报中文错误,不给"带病开关"进生产的机会。

### 1.3 八大内核总对比表(代差一图流)

| # | 内核 | 进化前(旧机制) | 进化后(新算法) | 代差证据(操作计数/精确结果) |
| --- | --- | --- | --- | --- |
| 1 | 肤色启发式(A123) | 图像分依赖 stub/云端 VLM/NudeNet(要权重或网络) | YCbCr 肤色簇 + 连通块 + 边缘密度,纯 stdlib 零外呼 | 合成肤色 8 图 vs 风景 8 图均值差 **0.9702**(基线 >0.2),阈值分类零错分 |
| 2 | n-gram 语言(A124) | `text_intel` 词表逐词命中(未收录词失效) | 字符 2/3-gram TF-IDF + 组质心余弦 | held-out 语料(10+10 段)AUC **1.0**(契约 ≥0.9) |
| 3 | SPRT 决策(A125) | 固定配额送满 `vlm_max_images_per_site` 张才判定 | Wald 序贯检验,证据够即停 | 20 张强 clean 序列第 **2** 张判停(基线 20);混合序列第 4 张停,省 16 张(80%) |
| 4 | 可靠性融合(A126) | ensemble 等权(agg_nsw_prob) | 提供方 Brier 反比加权,本地反馈闭环 | 好坏提供方权重 **0.9545 : 0.0455**(等权基线 0.5);喂 20 条反馈后 agg 0.5→**0.8636** |
| 5 | LSH 检索 + 256bit 指纹(A127/A134) | `find_similar` 全表 O(N) 扫描;64bit 全局 DCT 压平空间 | 分带 LSH 亚线性候选;4 象限 DCT 256bit | 10^3 指纹单查询比较比例 **0.001**(全表=1.0);同图/异图汉明裕量 **108 bit** |
| 6 | 增量并查集(A128) | A46 EvidenceGraph 持久图,连通性靠查询期计算 | 路径压缩 + 按秩合并,均摊 O(α),分量惰性缓存 | 10^4 次 union 后 `components()` 全程重算**恰 1 次**;10^4 次 connected 每次 ≤2 跳(朴素口径 4×10^6 边扫描) |
| 7 | 会话复用执行(A129) | 每份举报计划一次浏览器冷启动 | 一会话一次 launch,后续仅 new_page | 3 份计划 launch **恰 1 次**、new_page 3 次 |
| 8 | 优先级调度(A130) | watchlist 原序 FIFO 逐项扫 | 波动/风险/陈旧三因子加权 + 预算背包 | 10 项预算 4 恰选优先级前 **4** 项(与全排序切片逐项相等);单遍打分 10 次不随预算增长 |

---

## ② 八大内核分论

每章四段:**进化前 → 进化后(算法要点)→ bench 数字 → 局限与误用警示**。
bench 数字来源两途:测试断言(`tests/test_<kernel>.py` 的 `test_v7_bench_*`)
与 `kernel_selfcheck()` 实测输出(2026-10-02 运行)。

### 2.1 识别内核·肤色启发式(A123,`vision/heuristic_kernel.py`)

**进化前。** 图像侧风险分只能来自分类器:stub(假分)、NudeNet/CLIP(本地
模型权重)、GLM 等云端 VLM(外呼花钱)。断网、无权重、CI 环境下图像侧
完全没有本地信号。

**进化后。** 新增注册名 **"skin"** 的 `SkinHeuristicClassifier`(`NsfwClassifier`
子类,导入即自注册到 `classifier_base` 注册表),零网络、零模型权重:

1. **像素加载**:Pillow 惰性加载;未安装 Pillow 时退回纯 stdlib zlib 的
   最小 PNG 读取器 `read_png_rgb`(IHDR/IDAT/IEND、非隔行、8bit、色型
   0/2/4/6、扫描线滤波 0~4 全支持、逐块 CRC 校验、64M 像素护栏);
2. **三特征**(采样至 ≤64×64 网格,分辨率无关、同图同分):
   - `skin_ratio`:BT.601 YCbCr 变换后 Cb∈[77,127] 且 Cr∈[133,173] 的
     采样像素占比(Chai & Ngan 经典肤色簇);
   - `max_blob`:8×8 粗网格上"肤色过半"的格做 4-连通最大连通块占 64 格
     比例——迭代 flood fill(显式栈),每格至多入栈一次,操作计数 ≤64;
   - `edge_density`:采样灰度图上水平/垂直梯度 >40 的像素占比;
3. **组合公式**(常量模块级导出):
   `nsfw_prob = clamp(1.5·skin_ratio + 1.2·max_blob + 0.3·edge_density − 0.35, 0, 0.98)`;
4. 坏文件/缺解码器一律容错为 `nsfw_prob=0.0` + `scores={"error": 原因}`,
   绝不抛出;小图(宽与高均低于 `min_image_px`,缺省 200)照常打分,
   仅标记 `small=True`(口径与 `decision.verdict` 的候选过滤一致)。

**bench。**

- `test_v7_bench_linear_separability`(强制无 PIL 的纯 stdlib 链路):
  8 张合成肤色椭圆图 vs 8 张风景/文本图,均值差断言 >0.2,**实测
  `kernel_selfcheck()` 均值差 0.9702**(基线 0.2);以 max(clean)/min(skin)
  中点为阈值分类**零错分**,且 `min(skin)>0.5`、`max(clean)<0.1`;
- `test_v7_bench_flood_fill_ops`:任意图 flood fill 操作计数 ≤64;
  全肤图恰 **64**,纯背景图恰 **0**。

**局限与误用警示(docstring 原文明示)。** 这是纯色调/结构启发式,不是
学习到的模型:

- **人体艺术、沙滩照、泳装照等大面积裸露肤色但非色情的图片必然误报**;
  暖色调墙面/木地板/沙漠也可能落入肤色簇;重滤镜、暗光、低饱和的真实
  违规图可能漏报;
- 概率钳制上限 0.98——启发式不给满格置信,永远保留人复核空间;
- 本内核**仅作离线粗筛信号,不得单独作为处置/举报依据**,应与文本/页面级
  证据融合后由人复核;缺 Pillow 且非 PNG 格式时只有 error 信号,不是"干净"。

### 2.2 语言内核·字符 n-gram(A124,`intel/text_kernel.py`)

**进化前。** `intel/text_intel`(A28/V2):内容审核通用词表逐词
`str.count` 命中(色情词/诱导短语/混淆块)。词没收录就看不见——同义变体、
谐音改写、未收录表述全部漏检;且它是"命中即得分",不看整体文风。

**进化后。** `TextKernel(seed_pos, seed_neg)` 是与 text_intel **并存**(不改
它一字)的学习型内核:把正/负两组种子样张与待测文本放进同一语料库,看
"文本长得像哪一组样张":

- 分词:统一小写、折叠空白,滑窗取全部字符 **2-gram + 3-gram**(键带阶
  前缀,两阶互不串味);
- TF:词频 L1 归一(长短文本可比);IDF:`log((N+1)/(df+1)) + 1`
  (N = 种子 + 输入共 N+1 篇,平滑防除零);
- 组向量:pos/neg 各取成员 TF-IDF 的**均值向量(质心)**;
- 评分:`risk = clamp(0.5 + 2.0·(cos_pos − cos_neg), 0, 1)`——两组都不像
  → 0.5 中性,像正组上推、像负组下压;
- 种子:正组由 `text_intel` 的 `LURE_PHRASES + PORN_KEYWORDS` 词表组合
  织入 7 段诱导文案模板(惰性只读导入,缺席回退内置同表);负组为自写
  9 段正常资讯文(科技/天气/体育各 3);HTML 输入复用
  `text_intel.extract_visible_text` 剥标签;
- 红线 30:零外呼、不训练外模型,只消费调用方传入的本地文本与内置样张。

**bench。** `test_v7_bench_auc`:与种子**零句重复**的 held-out 语料
(正负各 10 段,风格相近但非原文,考察泛化)上 Mann-Whitney AUC 断言
≥0.9;**实测 `kernel_selfcheck()`(内置 5+5 段自检语料)AUC = 1.0**。

**局限与误用警示。** risk 是相似度几何映射而**非校准概率**(docstring
"局限"节原话):建议作为排序/辅助特征消费(与 fusion 中 text 特征同级),
**不要直接当阈值概率用**;种子风格覆盖有限,运营者应以 `seed_pos/seed_neg`
参数注入本辖区真实语料后效果更佳;对"既不像诱导也不像新闻"的第三类文本
(如纯代码页)会聚在 0.5 中性。

### 2.3 决策内核·序贯概率比检验 SPRT(A125,`decision/sprt.py`)

**进化前。** 站点判定要等图片送审配额花完:`vlm_max_images_per_site` 张
全部送 VLM,再统一算 ensemble 分。边缘站点和铁证站点花一样的钱。

**进化后。** Wald 序贯概率比检验:**每送审一张就检验一次累计证据**,
一旦足够偏 NSFW/CLEAN 立即停审,省下的配额留给下一个站点:

- 单张证据(软概率 p 作为单次伯努利试验期望,先钳进 [0.01, 0.99]
  防 p=0/1 退化):
  `LLR(p) = p·ln(p1/p0) + (1−p)·ln((1−p1)/(1−p0))`,默认 p0=0.5、p1=0.9;
- 判决边界:`A = ln((1−β)/α)`(上界,判 nsfw)、`B = ln(β/(1−α))`(下界,
  判 clean)。默认 α=β=0.05 时 A = ln(19) ≈ **2.9444**,B ≈ −2.9444;
- **吸收态**:一旦越界终判,后续 `update` 直接返回终态,不再消耗样本;
- `decide_sequence(probs) -> (verdict, n_used)`;`next_images(candidates, cfg)`
  复用 `intel.active_learn.rank_for_vlm` 的语义(只取 `model=="ensemble"`
  条目,按 |p−0.5| 升序——最"边缘"的先送,信息量最大),每取一张
  update 一次,判停即截断,上限默认 `cfg.vlm_max_images_per_site`。

**bench(全部操作计数)。**

- `kernel_selfcheck()`:20 张强 clean(全 0.02)序列**第 2 张即判停**,
  送审 2 张 vs 无早停基线 20(实测 value=2, baseline=20);
- `test_v7_bench_early_stop`:序列 [0.52, 0.48] + [0.02]×18,累计 LLR
  −0.4669 → −1.0217 → −2.5871 → −4.1526 ≤ B,**第 4 张停**,省 16 张
  (80% 配额);`next_images(max_send=20)` 同序截断至 4,未送审的 16 张
  从未被消耗;
- 默认参数下的关键数字(docstring 给出):单张证据上限 ln(1.8) ≈ 0.5878,
  故 p=1 连续证据下判 NSFW 最少需 **6 张**(ceil(ln19/ln1.8))。

**局限与误用警示。**

- **校准依赖是本内核的命门**:α/β 的错误率担保只对"校准概率"成立
  (GLM calibrate 后近似满足);**stub 未校准分数不满足假设,不建议开启**
  ——这正是 `use_sprt` 默认 False 的原因,开关由装配线(A139)把关;
- 默认参数化下 LLR 零点位于 p≈0.73(介于 p0 与 p1 之间):p<0.73 的每张
  都在向 clean 侧累积证据,p>0.73 才向 nsfw 侧累积——读 `total_llr` 时
  别拿 0 当"中性";
- `next_images` 本身不外呼 VLM,只做"排序 + 判停截断"的省预算原语;
  真实逐张送审接线在生产环境由 `kernel_wire.run_scan_v7` 装配。

### 2.4 融合内核·可靠性加权(A126,`decision/reliability.py` + `decision/fusion_reliable.py`)

**进化前。** `decision/fusion.fuse`(A29):图像特征直接取
`report.agg_nsw_prob`(ensemble 等权口径)。报得准的平台和报得离谱的
平台在图像侧话语权完全一样。

**进化后。** 两件套:

1. `ReliabilityTracker(jsonl_path)`——证据端学习器。运营者把人工复核结论
   `record(provider, p, outcome)` 进来,按提供方累计 **Brier 分数**
   `mean((p−outcome)²)`;权重 `w = 1/(brier+ε)`,ε=0.05——完美提供方
   权重 20、全错提供方 ≈0.952,**好坏权重比上限 21:1**(既让准的有话语权,
   也不让任何一家被清零或垄断)。样本 ≥5(`MIN_N`)才给归一权重,不足记
   `None`;单锁保护内存+jsonl 追加,坏行跳过,可跨批次/跨进程累积。
2. `fuse_reliable(report, url_feat, text_feat, page_vlm, cfg, tracker=)`——
   与 `fusion.fuse` **同构**(复用其 WEIGHTS/BIAS/归一化/sigmoid),唯一
   差异在图像侧:成员分(`model != "ensemble"` 条目)按提供方(模型名
   冒号前缀)先取均值(话语权不随评分张数放大),再按可靠性加权合成
   `agg_reliable` 进入 logit 融合。样本不足/未知的成员按已知成员权重均值
   参与;一个可用权重都没有(含 tracker=None)时全体等权 = **旧行为**。

**只升不降(安全底线,与 fuse 同构再加码):** `fused_final =
max(fused, agg_reliable)`;verdict 档位不低于原档位——可靠性重加权可能
让图像分**如实**下移,但**绝不能把图像已判 NSFW/SUSPECT 的站点洗成
CLEAN**;needs_review 单向恒真。`report.agg_nsw_prob`/`nsw_image_count`/
`image_scores` 保持原值不动,可靠性视角单列在 `intel["fusion"]` 新增键
`agg_reliable`/`member_weights`,`rule` 注明 **"reliable-weighted 只升不降"**。

**bench。**

- `kernel_selfcheck()`:good 报 p=1 六次全对(brier=0)、bad 报 p=1 六次
  全错(brier=1),**实测 w(good) = 21/22 ≈ 0.9545**(等权基线 0.5);
- `test_v7_bench_weight_shift`:10 图 vlm 全报 0.9、stub 全报 0.1(等权
  agg=0.5);喂 20 条本地反馈(vlm 十对、stub 十错)后同一分值集合的
  **agg_reliable 精确 = 19/22 ≈ 0.8636**,`member_weights ==
  {"vlm": 0.9545, "stub": 0.0455}`,融合概率同步上移,档位受图像张数
  闸门保持 SUSPECT。

**局限与误用警示。**

- **冷启动 = 等权 = 旧行为**:少于 5 条反馈的提供方统计上不可信,别指望
  开关一打开就变准;反馈本身的质量决定一切——复核结论错,权重就学错
  (垃圾进垃圾出);
- Brier 度量的是**校准度**(报 0.9 就该 90% 真),不是区分度;一个永远
  报 0.5 的提供方 Brier 很好但毫无用处,务必配合 ensemble 多成员使用;
- "只升不降"是安全方向的不对称:可靠性加权**不能**用来给站点"洗白",
  降档永远需要人;
- 红线 30:只消费本地 jsonl 反馈,绝不联网取数、绝不调 VLM 训练。

### 2.5 检索内核·分带 LSH + 指纹内核 v2(A127 `intel/phash_lsh.py` + A134 `vision/phash2.py`)

**进化前。** ① `PhashRegistry.find_similar`(A43):对汉明距离近邻只能
**全表扫描**,库到 10^3~10^5 量级时代价线性增长;② 64bit pHash 对整幅
32×32 灰度做一次全局 DCT——空间信息被压平,近重复识别可靠但精细区分度
有限。

**进化后。**

**(a) 分带 LSH** `LSHIndex(bands)`(默认 4 带 × 16bit 桶键):64bit 指纹
均分为 bands 带(不能整除时前若干带多 1 位,保证 64 位全覆盖),任一带
桶键相同即成候选,再对候选做精确汉明过滤(`(a^b).bit_count()`)——
近邻检索从 O(N) 次比较降到 O(候选数) 次。互操作:`build_from(registry)`
惰性只读遍历 `PhashRegistry` 全表登记(负载键口径与 `find_similar` 对齐:
sha256/site/verdict_tag),非 64bit 脏行跳过不中断。`compare_calls` 导出
累计比较计数(bench 依据);RLock 全程串行,扫描/复核线程可共用实例。

**召回保证与漏检边界(方法论固有取舍,docstring 给出推导):** 每个翻转
位至多破坏一个带,距离 d 的近邻至多破坏 d 个带——**d ≤ bands−1 内保证
100% 召回**(必有一带完好 → 必为候选);代价是 **d ≥ bands 的近邻可能漏检**
(每带恰好各翻一位时全带皆毁)。随机无关指纹同桶概率 ≈ bands/2^(64/bands):
默认 4×16bit ≈ 4/2^16 ≈ **6.1e-5**(10^3 条索引上单查询期望候选 ≈ 0.06);
换 8 带 8bit 则 ≈ 8/2^8 ≈ **3.1%**,d ≤ 7 保证召回,候选相应增多;
bands=1 退化为 64bit 精确匹配(零碰撞也零容错)。

**(b) 指纹 v2** `phash256`:32×32 灰度 → 2×2 分成 4 个 16×16 象限 →
每象限独立 2D DCT-II(可分离余弦基,纯 stdlib math)→ 取左上 8×8 低频,
以"除 DC 外 63 个系数的中位数"为阈值(与 A43 同款口径)→ **4×64bit =
256bit**,返回 64 位小写 hex。`hamming_hex` 同时支持 256/64bit 等长比较;
`to64(hex256)` 取每象限系数前两行(u=0,1 共 16 系数)的 16bit 拼接,给出
64bit 视图接入既有生态。

> 口径修正(docstring 明示):契约原文写"每块取 4x4 低频 → 4×16bit"
> (合计 64bit),与"256bit pHash"自相矛盾;实施按修正口径 4 块 × 每块
> 8×8 = 64 系数 → 每块 64bit,共 256bit。

**bench。**

- LSH `kernel_selfcheck()`:1000 条固定种子随机指纹,以表中第 0 条为查询,
  **实测比较比例 = 0.001**(即 1 次比较 vs 全表 1000 次;断言 ≤10% 且
  命中集合与全表扫描**完全一致**);
- `test_v7_bench_query_compare_calls_vs_full_scan`:10^3 随机指纹 + 5 个
  近邻(d∈{1,2,3})共 1005 条,全表扫描需 1005 次比较,LSH ≤ 全表 10%
  (期望 ≈5 次),5 个近邻全召回、命中集合与暴力扫描一致;
- `test_v7_bench_bands8_compare_fraction`:bands=8 时候选期望 ≈31,
  单查询比较仍 ≤ 全表 10%(换取 d ≤ 7 召回保证);
- phash2 `kernel_selfcheck()`:亮度 ×1.1/×0.9 与量化抖动(中位阈值 DCT
  对正线性缩放数学上不变)vs 8 种构图,**实测区分度裕量 = 108 bit**
  (min 异图距 − max 同图距,须 >0);
- `test_v7_bench_separability_margin`:8 组同图各 4 变换(32 个同图距离)
  × 8 照片 vs 10 构图(80 个异图距离):同图最大 ≤24(实测 4)、异图最小
  ≥64(实测 90)、裕量 86。

**局限与误用警示。**

- **LSH 漏检边界就是 d ≥ bands**:默认 4 带只保证 d ≤ 3 的近邻必达;
  若业务把 max_distance 用到 8 而召回不能漏,应调大 `phash_lsh_bands`
  (如 8)并接受候选变多;
- `to64` 与 A43 `phash` 是**强相关而非逐位相等**(全局 DCT vs 象限拼接,
  数学上不是同一变换):用于"粗筛同 64bit 生态"安全,**不可**当逐位相等
  的替代品做去重判定;
- 分块的代价:象限边界重采样可能有 1~2bit 抖动(换来空间布局信息);
  phash2 **无 aHash 降级**(256bit 口径无降级等价物),缺 Pillow 直接抛
  中文 ValueError——离线裸环境请确认 Pillow 在位。

### 2.6 图谱内核·增量并查集(A128,`intel/graph_kernel.py`)

**进化前。** A46 `EvidenceGraph`(SQLite 证据图)负责站点团伙的持久化:
边种类、权重、导出;但"这两个站点是否同伙/全图有几个团伙"这类连通性
查询每次都要在查询期对边集做计算,边越攒越多越慢。

**进化后。** `UnionFindKernel`:纯内存、零依赖、全增量的团伙归并内核——

- 并查集**路径压缩 + 按秩合并**:`union/find/connected` 均摊 O(α(n))
  (反阿克曼函数,实践视为常数);`find` 迭代实现(两趟压缩),长链不触
  递归上限;未登记节点自动登记;
- **惰性分量缓存**:`components()` 只在缓存失效后的首次查询重算一次
  (O(n·α)),其后返回**同一 dict 对象**(O(1));只有**有效合并/新增节点**
  才失效缓存,冗余 union 不失效;`union(a,b)` 仅真实合并返回 True;
- **邻接表同步维护**:每条边(含环边)同时进邻接表与导出边集,
  `related(host, depth)` 在邻接表上做 BFS(不含自身,深放即"分量内可达");
- 与 A46 共存:持久化归 A46,两者仅经 `export_json()/export()` 互导;
  本模块零网络、零三方依赖、不 import 任何 netsentinel 模块;
- 操作计数导出(红线 31):`recompute_count`(真实重算分量次数)、
  `invalidate_count`(有效缓存被丢弃次数)、`find_hops`(find 跳转累计)。

**bench。**

- `test_v7_bench_10k_unions_components_o1_no_full_recompute`:10^4 次
  seeded 随机 union,灌入期 `recompute_count == 0`(从不物化缓存)、
  `invalidate_count == 0`;首次 `components()` 后重算计数**恰 1**;
  再查 100 次返回同一对象、计数仍 1;不变式"合并数 = 节点数 − 分量数"
  成立;查询后灌冗余边仍不失效不重算;
- `test_v7_bench_connected_is_o1_after_compression`:全量压缩后 10^4 次
  connected,每次 ≤2 次父指针跳转(`hops ≤ 2n`);
- `kernel_selfcheck()`:2×10^3 边 + 2×10^3 查询,**实测 find_hops 总数
  3981 vs 朴素口径基线 4,000,000**(n×n 边扫描)。

**局限与误用警示。**

- **不持久化**:内存工作集,进程重启即空,持久化与边种类/权重归 A46
  ——别把它当 EvidenceGraph 的替代品,它是"查询加速 + 增量归并"侧写;
- **不加锁**:单线程装配线语义,跨线程请外部串行化;
- `components()` 返回**活视图**:调用方只读,写它不会被感知也不会触发
  重建(写口会自动失效重建的是内核自己的口)。

### 2.7 执行内核·会话复用(A129,`submit/executor_session.py`)

**进化前。** `executor_playwright.execute`:每份举报计划独立驱动一次
浏览器——冷启动 chromium、开 context、执行、关闭。批量举报 20 份计划
就是 20 次冷启动。

**进化后。** `SessionExecutor(cfg, *, launcher=None)`:在同一浏览器会话内
**顺序**执行多份 `SubmissionPlan`:

- 首个真实模式计划触发一次 `chromium.launch(headless=True)` 并保留
  BrowserContext;后续计划仅 `context.new_page()`;
- **页失败隔离**:单页失败只关闭该页,会话照常服务后续计划;会话级失败
  (launch/new_page)才销毁并允许下一计划重建会话;
- **零语义漂移**:逐步语义(GOTO/WAIT/SELECT/FILL/CLICK/SCREENSHOT/
  HUMAN_GATE)直接 import 复用 executor_playwright 的 `_perform_step`;
  `dry_run` 分支原样委托 `executor_playwright.execute`(纯标准库,notes
  文案一字不差);`close()` 幂等,支持 with;close 后 run 抛 RuntimeError;
- 输出目录:会话根 `runs/session_<时间戳>/plan001、plan002…` 逐计划独立;
- 遥测:`executor_session.launch`(一会话恰计一次)、`executor_session.run`
  计时,`executor.submitted/errors` 与 executor_playwright 同名同义。

**bench。** `test_v7_bench_session_reuse_launch_once_new_page_three`
(FakeLauncher 注入,操作计数):3 份计划后 **launch 恰 1 次**(launcher
调用、chromium.launch 尝试、成功计数三者一致,launch 参数
`{"headless": True}`)、**new_page 恰 3 次**、每页各关一次、
`executor_session.launch` 遥测 == 1。伴随测试:第 2 计划 goto 崩溃 → 该条
failed、第 3 计划照常执行,仍只 launch 1 次、共 3 页,context 未关。

**局限与误用警示。**

- **红线 24 不因会话而松**:会话只是顺序编排,每条计划的验证码输入与
  最终确认仍由人工在 HUMAN_GATE 完成;`auto_confirm` 缺省 False 且本模块
  **不存在任何把它置 True 的路径**;
- 顺序执行、单 context:不要指望并行多标签提交(频控与人工门语义都不
  允许);开发与测试只允许 `file://` 或 127.0.0.1 本地页面;
- 会话共享意味着页面间有浏览器级状态(cookie/存储)——对"同一门户连续
  举报"是收益,对"需要干净指纹的计划"要自己拆会话。

### 2.8 调度内核·优先级预算(A130,`ops/sched_kernel.py`)

**进化前。** `ops/scheduler.run_once`(A39):按 watchlist **原序**逐项
巡查,先扫到谁全凭列表顺序;预算(时间/礼貌间隔)花在谁身上听天由命。

**进化后。** 两个纯函数(零 IO、零时钟,同输入同输出):

- `priority(item) -> float`:三因子加权,
  `0.45·volatility + 0.35·url_risk + 0.20·min(staleness_h/168, 1)`。
  因子来源只读对齐既有模块(volatility ← ops.adaptive,url_risk ←
  intel.url_intel),缺字段按 0、越界钳制回 [0,1];权重设计:波动度最高
  (指纹常变的站点最可能滋生新违规)、风险次之、陈旧度兜底(168h=一周
  即"完全陈旧");
- `select_round(items, budget, *, min_interval_s=1.0) -> list`:按优先级
  降序做**等成本背包**(每项成本 = 礼貌间隔 min_interval_s),装入
  `floor(budget/min_interval_s)` 个最高价值目标;预算连一项都装不下返回空;
  稳定排序(等优先级保持原序);返回原 dict 引用不复制。可变成本扩展点
  已预留(成本函数换成"站点页数×间隔",接口与排序不变)。

**bench。**

- `test_v7_bench_top4_of_10_exact`:10 项确定性候选、budget=4.0——选取
  结果与"全量排序取前 4"**逐项相等**,未入选 6 项优先级无一高于入选
  最低者;
- `test_v7_bench_single_pass_ranking_operation_count`:单遍打分,10 项
  恰 10 次 `priority` 调用;预算 4→8 翻倍,**打分次数不变**(不随预算重扫);
- `kernel_selfcheck()`:value == baseline == 4(恰选 4 项)。

**局限与误用警示。**

- **等成本是演示口径**:真实成本应随站点规模变化(扩展点已预留但未接);
  在接入可变成本前,别把 budget 直接当"分钟数"对外承诺;
- 三因子权重量纲是工程约定非学习所得,辖区数据偏斜时(如全体高波动)
  应调权重复评;
- 纯函数不碰时钟:`staleness_h` 必须由调用方算好传入,内核不知道"现在
  几点"——传错单位(秒 vs 小时)不会被拦截(168 会当 168 小时≈7 天)。

### 2.9 基础设施内核(存储/缓存/张量微库/风格/安全 fuzz/指标导出/装配线)

八个主内核之外,V7 另有一组"底座级"内核——它们不直接产生判定,但让
主内核们有统一的地基。

#### 2.9.1 存储内核(A131,`storage/kernel.py`)

- **进化前**:全仓散落 8+ 个各自为政的 sqlite 封装(review_queue /
  vlm_cache / graph / phash / site_memory / batch_state / four_eyes /
  batch_review…),连接策略、迁移、线程安全各写各的。
- **进化后**:`SQLiteKernel(path)` 统一底座——WAL + busy_timeout=5000 +
  `check_same_thread=False` 单连接 + 全操作共持一把锁(与全仓口径一致);
  `migrate(version, ddl_list)` 版本化迁移(`schema_version` 单行版本表,
  **同版本重复迁移零 DDL 执行**,降级抛中文 ValueError);`repo(table)`
  生成 CRUD 助手(表名/列名按标识符白名单校验后加引号,注入串一律当
  字面量);`vacuum_if_needed(min_fragments)` 按页数阈值惰性 VACUUM
  (`DEFAULT_MIN_PAGE_COUNT=200`)。**打开既有库不建版本表、不迁移、
  不改写任何数据**。
- **bench**:`test_v7_bench_migrate_idempotent_execute_counts`——首次
  迁移 3 条 DDL 执行 3 次;**二次打开再迁移 ddl_executed == 0 且
  sqlite total_changes == 0**(连版本行都没重写);同实例第三次仍 0。
  `test_v7_bench_vacuum_count_thresholds`——空库 0 次 → 250 行 8KB 大字段
  + DELETE 后达阈值恰 1 次 → 回收后冻结在 1。`kernel_selfcheck()`
  实测 value=0(首次 2 条 DDL)。
- **局限**:单连接全锁串行,吞吐上限即单 sqlite 写吞吐;迁移只管 DDL
  版本,不做数据回填——回填脚本归调用方。

#### 2.9.2 缓存内核(A132,`vision/cache2.py`)

- **进化前**:只有 `vlm_cache`(VLM 响应的 sqlite 跨批缓存);分类器
  结果层没有进程内缓存,同一张图被 ensemble/failover 反复评分就反复算。
- **进化后**:`MemoLRU(capacity)`(OrderedDict 线程安全 LRU,读也刷新
  新鲜度,`hit_stats()` 命中率;**capacity ≤ 0 直通不缓存**) +
  `CachedClassifier(inner, lru)` 包装**任意** NsfwClassifier:缓存键 =
  `img.sha256`(缺失回退 `"path#" + sha256(路径)`,与 vlm_cache "同图同分"
  语义一致);命中**返回缓存的原 ImageScore 对象**(零内层调用);不向
  注册表注册名字,只能显式构造。遥测 `cache2.hit`/`cache2.miss` 与
  vlm_cache 计数体系并存。
- **bench**:`test_v7_bench_repeat_classify_inner_calls_5_to_1`——同图
  重复 classify 5 次,**内层恰调用 1 次**(5→1),命中 4 次全部返回同一
  对象,hit_stats == {hits:4, misses:1, hit_rate:0.8};4 张不同图首轮
  4 次内层调用、重放轮全命中零新增(hit_rate 0.5)。
- **局限与误用警示**:进程内缓存,重启即空(跨批持久缓存仍是 vlm_cache
  的领地);命中返回**原对象**——调用方不得原地改 ImageScore,否则污染
  后续命中;容量默认 256,大批量任务请按工作集调大或显式传 LRU。

#### 2.9.3 张量微库(A133,包根 `mathx.py`)

- **进化前**:各学习型内核各手搓点积/归一化/激活,口径容易漂移
  (谁 clamp 谁不 clamp、谁防溢出谁不防)。
- **进化后**:纯 stdlib 纯函数集 `dot / matmul / softmax / sigmoid /
  standardize / clamp / matmul_ops`——数值稳定(softmax 减最大值、sigmoid
  正负双分支,±1000 不溢出)、结构校验全抛中文 ValueError、bool 显式拒绝、
  任意嵌套 list/tuple 入参、元素不拷贝(float 子类替身可透传供计数)。
- **bench**:`test_v7_bench_matmul_op_count` 三路互证 64×64×64:
  公式口 `matmul_ops(64,64,64) == 64³ == 262144`;全 1 构造(输出元素和
  = 乘加总数)**== 262144**;替身元素逐次计数内部乘法**恰 262144 次**;
  另有随机阵×单位阵逐元素精确恒等。`kernel_selfcheck()` 实测
  value == baseline == 262144。
- **局限**:纯 Python 解释器循环,O(m·k·n) 无向量化——定位是内核级小
  数值(V7 各学习内核的特征运算),不是给大矩阵用的;别拿它跑图像张量。

#### 2.9.4 风格内核(A135,`submit/style_kernel.py`)

- **进化前**:`describer_critic`(A52)只查**事实**(草稿数值有没有事实
  清单依据),文风没人管——夸张词、口语化、超长草稿直接出门。
- **进化后**:`style_score(text)` 四维独立 0/1 打分 + total(0~4):
  `len_ok`(30 ≤ 字符数 ≤ 240,上限与 A52/A36 一致)、`has_facts`
  (数值密度 > 0 且至少 1 个数字——只看有无,不核对真伪)、`no_hype`
  (未命中 9 词夸张词表:前 7 词与 A52 HYPERBOLE_WORDS 对齐 + 文风专用
  "铺天盖地/触目惊心")、`formality`(命中法言法语模板 ≥1:经核实/涉嫌/
  含有/违反/人工核实)。`polish(text)` 三步轻改:①夸张词按 NEUTRAL_MAP
  换中性词(单遍替换不回扫);②超 240 截断至句号(预留结尾句 12 字与
  分隔句号额度,正文预算 227);③结尾确保"以上情况本人已人工核实。"
  (幂等)。**不动任何事实性内容**——数值、站点、行为描述一概不碰,
  截断只去尾部整句;数值错 critic 抓、文风差 style 抓,两者互补缺一不可。
- **bench**:`kernel_selfcheck()`——含 7 处夸张词、缺结尾句的草稿,
  polish 后夸张命中 **0**(改写前 7),产物长 85 ≤ 240、结尾句就位;
  `test_v7_bench_batch_scoring_operation_count`——批量 100 段(50 合格 +
  50 掺"大量"):遥测恰 +100,总分精确 = 50×4 + 50×3,重跑逐段一致,
  polish 批量不改打点数。
- **局限**:不做事实核对(那是 critic 的职责);夸张词表有限,变体夸张
  ("海了去了")不识别;截断虽只去整句,超长草稿的**尾部事实**仍会被
  舍弃——重要事实放前面。

#### 2.9.5 安全内核·模糊测试(A136,`security/threat_kernel.py`)

- **进化前**:各内核入口对恶意输入的行为靠零散单测覆盖,没有系统性的
  "把攻击者最爱的输入成规模砸过去"的机制。
- **进化后**:四个**确定性**生成器 `fuzz_url/fuzz_html/fuzz_yaml/fuzz_json`
  (各 ≥30 变体:超长/控制字符/嵌套深度/坏编码;种子 2026_0136 派生独立
  子随机,同进程两次调用逐字节相同;不含孤立代理项,坏 UTF-8 以替换符
  呈现保证可落盘)→ `all_cases()` 共 **162** 条用例 × 5 个真实内核入口
  (canonical / text_intel / response_repair / policy.load_policy /
  conformal.fit_threshold 的 JSON 适配)= **810 格**矩阵。断言口径:入口
  要么正常返回、要么抛允许集 **ValueError/RuntimeError/TypeError**
  (RecursionError ⊂ RuntimeError)——绝不 segfault、绝不裸 Exception。
  **诚实口径**:`SKIP_LIST` 登记已知违规不豁免执行,区分"已登记(待
  修复)"与"新发现"且都原样打印;当前登记 1 项:`decision/conformal.py`
  的 `_clean_and_sort` 对超出 float 表示范围的巨整分值抛 OverflowError
  (不在允许集内;A45 既有模块,按红线 29 不在本内核内修复,如实报告
  待项目负责人处理)。`main` CLI 跑全量输出中文报告,退出码 0/2。
- **bench**:`test_v7_bench_matrix_operation_count`——计数包装注入,
  每目标恰执行 162 次、总执行恰 810;`kernel_selfcheck()` 实测
  total_ran=810、**新发现违规 0**(基线 0)、已登记 1。
- **局限**:语料是确定性有限集,证明"这 810 格内无新违规",不是穷尽
  证明;登记项修复前,conformal 的巨整输入风险仍在(调用方自行钳制)。

#### 2.9.6 观测内核·指标导出(A137,包根 `telemetry_export.py`)

- **进化前**:`telemetry.snapshot()`(V5 冻结)只有进程内 dict 视图,
  运营者无法把指标接进 Prometheus 等外部观测体系。
- **进化后**:`to_prometheus(snapshot)`——计数器 → `<name>_total` 标
  counter;仪表 → 原名标 gauge;计时器 → `count/avg_ms/p95_ms/max_ms`
  四条序列**全部标 gauge**(环形缓冲内的 count 非严格单调,标 counter
  会被 Prometheus 误判"计数器重置");名称清洗(白名单外换 `_`、数字
  开头补 `ns_` 前缀、HELP 内保留原始名可回溯);节序固定、节内排序,
  同一快照恒得同一文本(逐字节确定);`diff_snaps(a,b)` 四键增量 diff;
  `render_metrics` 直通;`parse_prometheus` 极简解析器。telemetry.py 与
  service 均冻结不改,`/metrics` 挂载是运营者在 service 之外的自由扩展。
- **bench**:`kernel_selfcheck()`——40 计数器 + 30 仪表 + 20 计时器 =
  150 条样本,导出→解析**往返保真率实测 1.0**(逐对相等);
  `test_v7_bench_export_parse_roundtrip`——33+21+4×17 = 122 指标,样本
  行数 == 122、HELP/TYPE 族数 == 122、解析值逐对相等。
- **局限**:timer 的 `_count` 是 gauge 不是 counter(diff 时别按单调读);
  解析器是"极简"版(指标名→值),不支持 label/多样本文本。

#### 2.9.7 装配线(A139,`pipeline/kernel_wire.py`——**按契约设计,尚未合入**)

按《CONTRACTS-V7.md》§2 A139 与 `sprt.py`/`fusion_reliable.py`/`cache2.py`
docstring 中的引用:提供 `assemble(cfg) -> KernelBox`(dataclass:classifier
工厂链 / sprt 可选 / fusion 选择 / executor 选择(sched/browser 开关)/
lsh 可选)与 **`run_scan_v7(url, cfg)`** 包装——内部走
`orchestrator.run_scan` 并按开关套 CachedClassifier / SPRT 后处理 /
fuse_reliable;**不改 orchestrator 一行**;开关全关时行为与 v6 完全一致
(契约要求以断言锁定)。合入前,各内核的接线方式见 ⑤ 的"手动接线"。

---

## ③ kernel_bench 使用法与报告解读(A138)

> 状态:`benchmarks/kernel_bench.py` 尚未合入(并行开发中)。本节按契约
> §2 A138 与各内核已落地的 `kernel_selfcheck()` 约定编写;下表数值是
> 2026-10-02 在本仓库逐个实跑 13 个已就位内核自检的真实输出,合入后
> kernel_bench 应逐行复现它们。

### 3.1 使用法(契约口径)

- 每个内核模块提供 `kernel_selfcheck() -> {"name", "metric", "value",
  "baseline"}`——离线、确定性、零墙钟、零真实网络;
- `kernel_bench` 统一调用各自检,产出 **`benchmarks/out/kernel_report.md` /
  `kernel_report.json`(中文对比表)**;
- 提供 CLI;**失败阈值退出码 2**(任一内核 value 越过其基线约定即失败)。

### 3.2 已实测的 13 份自检报告(2026-10-02 实跑)

| name | metric | value(实测) | baseline | 判读方向 |
| --- | --- | --- | --- | --- |
| skin | synthetic_skin_vs_scenery_mean_gap | **0.9702** | 0.2 | value > baseline(越大越好) |
| text_kernel | AUC | **1.0** | 0.9 | value ≥ baseline |
| sprt | 20 张强 clean 序列的送审张数(早停) | **2**(verdict=clean) | 20 | value < baseline(越小越省) |
| reliability | good_provider_weight | **0.9545** | 0.5 | value > baseline |
| phash_lsh | query_compare_ratio_vs_full_scan | **0.001** | 1.0 | value ≤ 0.1×baseline |
| graph_kernel.union_find | 2000 次 connected 的父指针跳转总数 | **3981** | 4,000,000 | value ≪ baseline |
| sched_kernel | top4_of_10_selected | **4** | 4 | value == baseline(精确结果) |
| storage.sqlite_kernel | migrate 二次打开的用户 DDL 执行数 | **0** | 首次迁移执行 2 条 DDL | value == 0(幂等) |
| mathx.matmul | 64×64×64 全 1 矩阵乘乘加计数 | **262144** | 262144 | value == baseline(零漂移) |
| phash2 | same_max_lt_diff_min_margin_bits | **108** | 0 | value > baseline |
| style_kernel | polish 后夸张词命中数 | **0**(改写前 7) | 7 | value < baseline |
| telemetry_export | roundtrip_pair_recovery_ratio | **1.0** | 1.0 | value == baseline(零失真) |
| threat_kernel | 全量模糊矩阵的新发现违规数 | **0**(total_ran=810) | 0 | value == baseline |

注:执行内核与会话内核(A129)与缓存内核(A132)未提供 `kernel_selfcheck`
函数——它们的代差以测试内操作计数锁定(launch 恰 1 次 / 内层调用 5→1),
不进自检表;这是契约允许的(红线 31 只要求 `test_v7_bench_*`,自检函数
是 A138 汇总的补充约定)。

### 3.3 报告解读三原则

1. **先看方向再比大小**:上表"判读方向"一列不统一——有的内核越大越好
   (skin 均值差)、有的越小越好(sprt 送审张数、style 夸张命中)、有的
   必须精确相等(mathx 262144、sched 4)。比较 value 与 baseline 前先确认
   该内核的方向约定,别做"value < baseline 就失败"的一刀切。
2. **数字是操作计数,不是秒**:所有指标都在确定性构造数据上复现(固定
   随机种子/合成语料),换机器、换负载结果应当逐字节一致——若两次运行
   不同,先怀疑环境(如 Pillow 在位状态影响 skin 的解码链路),不要怀疑
   算法。
3. **baseline 的三种含义**:① 旧机制口径(sprt 的 20 = 送满配额;
   phash_lsh 的 1.0 = 全表扫描;graph 的 n² = 朴素全扫)→ value/baseline
   即代差倍数;② 契约下限(skin 的 0.2、text 的 0.9)→ 只是及格线,
   实测应显著高于它;③ 理论精确值(mathx 的 64³、sched 的 4)→ 相等
   即证明实现与复杂度口径零漂移。

---

## ④ 调优指南

按"阈值 / 带宽 / 预算"三类参数给出建议。原则:**一次只动一个开关,
跑一轮 kernel_bench 与既有 2790 项回归,再进生产。**

### 4.1 阈值类

| 参数 | 位置 | 默认 | 调优建议 |
| --- | --- | --- | --- |
| `sprt_alpha` / `sprt_beta` | Config | 0.05 / 0.05 | 减小 α → A=ln((1−β)/α) 增大 → 需要更多证据才判 NSFW(漏报↑误报↓);默认对称 0.05 时 A≈2.9444,p=1 连证最少 6 张判 NSFW。**改前确认概率已校准**(GLM calibrate),否则担保无效 |
| p0 / p1 | `SPRT(...)` 构造参数 | 0.5 / 0.9 | 对称假设 p0=0.1/p1=0.9 证据更"锋利"(早停更快),但零点从 0.73 移动,需重估运营语义 |
| `nsfw_threshold` / `review_threshold` / `min_nsw_images` | Config(既有) | — | fuse_reliable 与 fuse 同构消费它们;NSFW 档仍受 `min_nsw_images` 张数闸门(见 2.4 bench:SUSPECT 未升 NSFW 的原因) |
| skin 组合系数 | `heuristic_kernel` 模块常量 | 1.5/1.2/0.3/−0.35,封顶 0.98 | 常量已模块级导出可复算;误报多先降 `W_SKIN_RATIO`、漏报多查 Pillow 在位与滤镜场景;**不建议**抬 PROB_CAP——0.98 封顶是"保留人复核"的刻意设计 |
| `phash_lsh_bands` | Config | 4(1~8) | 召回优先调大:8 带 8bit 时 d≤7 保证召回、随机同桶概率升到 ≈3.1%(候选与比较次数同涨);`query` 的 `max_distance=8` 与 4 带的 d≤3 保证域不匹配时,以 bands=8 为默认升级位 |
| `MIN_CHARS`/`MAX_CHARS` | `style_kernel` 常量 | 30/240 | 与 A52/A36 对齐冻结,勿单方面改;结尾句预算 227 由 240−12−1 推出 |
| `vacuum_if_needed(min_fragments)` | `SQLiteKernel` | 200 页 | 库小别开(白费 IO);大批量 DELETE 后手动调一次 |

### 4.2 带宽类(学习/缓存容量)

| 参数 | 位置 | 默认 | 调优建议 |
| --- | --- | --- | --- |
| reliability `MIN_N` | `weights(min_n=)` | 5 | 高价值站点可抬到 10+ 让权重更稳;样本不足返回 None → fusion 等权(旧行为),不会崩 |
| reliability `EPSILON` | 模块常量 | 0.05 | 权重比上限 = (1/ε):(1/(1+ε)) ≈ 21:1;想拉开差距减小 ε(同时放大噪声) |
| `MemoLRU capacity` | 构造参数 | 256 | 按"单站图片数 × 在途站点数"估工作集;设 0 = 一键直通回到 V6 无缓存 |
| text kernel 种子 | `TextKernel(seed_pos, seed_neg)` | 内置 7+9 段 | 注入本辖区真实语料(各 ≥6 段)是**最有效**的调优;正负组风格要互斥,否则 cos 差被压平 |
| threat fuzz 用例 | 生成器各 ≥30 | 162 条 | 扩展用例请保持种子确定性(红线 31),勿引入真随机 |

### 4.3 预算类

| 参数 | 位置 | 默认 | 调优建议 |
| --- | --- | --- | --- |
| `vlm_max_images_per_site` | Config(既有) | — | SPRT 开启后它是 `next_images` 的送审**上限**而非"必花额度"——实测强 clean 站 2 张即停,配额可适当上调而总花费反降 |
| `select_round(budget, min_interval_s)` | 调用参数 | min_interval=1.0 | budget 与 min_interval 同单位(演示口径秒);`floor(budget/min_interval)` 即选中项数;`min_interval_s ≤ 0` 抛 ValueError(成本为零会装下一切) |
| 调度三权重 | `sched_kernel` 常量 | 0.45/0.35/0.20 | 辖区高波动站点多 → 维持 volatility 主导;巡查覆盖不足 → 抬 W_STALENESS(168h 窗口内线性饱和) |
| 会话规模 | `SessionExecutor` | — | 一会话顺序执行;计划多时收益最大(launch 摊薄),但页失败隔离以"关该页继续"为界——会话级故障才重建 |

### 4.4 一个推荐的启用顺序

1. **先开无风险三件**(任何时刻):`skin` 作粗筛信号、text_kernel 作
   排序特征、style_kernel + threat_kernel 离线跑——它们不改变任何既有
   判定路径;
2. **开缓存**:`CachedClassifier` 包住现用分类器(先观察 `cache2.hit`/
   `miss` 命中率,再决定容量);
3. **开融合**:`use_reliability_fusion=True` 并开始喂 review 反馈到
   `data/reliability.jsonl`(冷启动期行为 = 旧融合,零风险);
4. **最后开 SPRT**:确认主分类器概率经校准(GLM calibrate)后
   `use_sprt=True`;stub 或未校准通道保持关闭(docstring 明示);
5. LSH:哈希库过千条后按需建 `LSHIndex.build_from(registry)`,bands 视
   业务对 d≥bands 漏检的容忍度在 4 与 8 之间选。

---

## ⑤ 与既有流程的接入点

红线 29 决定了 V7 内核的接入方式是"**新增名字 / 显式包装 / 装配线包装**"
三种,以下按既有流程逐条对接(装配线 A139 合入前的手动接法一并给出)。

### 5.1 分类器名 "skin"(识别内核)

- 注册即接入:`vision/heuristic_kernel` 模块导入时自动以 **"skin"** 注册
  到 `classifier_base` 注册表(基座缺席时静默跳过);
- 配置切换:`cfg.classifier = "skin"`(字段默认 "stub"),或代码里
  `get_classifier("skin", cfg)`;亦可进 `ensemble_members` 与其它成员
  做集成——ensemble 聚合条目 `model == "ensemble"`,成员分以
  `model="skin"` 出现在 `report.image_scores`;
- 零破坏:不点名 "skin" 时,工厂、orchestrator、既有测试全部原样。

### 5.2 "cached" 包装(缓存内核)

`CachedClassifier` **不是注册名**,是装饰器:显式包住任意分类器实例——

```python
from netsentinel.vision.cache2 import CachedClassifier, MemoLRU
from netsentinel.vision.classifier_base import get_classifier

inner = get_classifier("glm:glm-5.3", cfg)          # 任意既有分类器
clf = CachedClassifier(inner, lru=MemoLRU(256))      # 同图重复评分零内层调用
```

orchestrator 的 `classifier` 注入口接受任何 `NsfwClassifier` 实例,
包装后直接传入即可;装配线(A139)按开关把工厂链整体包成 cached 版本。

### 5.3 `kernel_wire.run_scan_v7(url, cfg)`(装配线,契约口径)

合入后即为 V7 扫描的统一入口:内部走 **既有 `orchestrator.run_scan`
一行不改**,仅按开关在外围套三层——分类器工厂链(cached 包装)→
SPRT 后处理(逐张送审判停)→ fuse_reliable(可靠性融合);`assemble(cfg)
-> KernelBox` 供需要自组流水线的调用方取用各内核件。**开关全关时与 v6
行为完全一致**(契约要求断言锁定)。合入前的等效手动接线:

```python
report = orchestrator.run_scan(url, cfg, classifier=CachedClassifier(...))
if cfg.use_sprt:                        # 仅校准概率通道
    sent, sprt = sprt.next_images(report.image_scores, cfg)
if cfg.use_reliability_fusion:
    report = fusion_reliable.fuse_reliable(
        report, url_feat, text_feat, page_vlm, cfg,
        tracker=ReliabilityTracker("data/reliability.jsonl"))
```

### 5.4 其余内核的既有接口对接

| 内核 | 既有流程 | 接入点 |
| --- | --- | --- |
| TextKernel | fusion 的 text 特征同级 | `score(html)` 的 `risk` 作为辅助特征传入(排序用,勿当概率) |
| SPRT | `rank_for_vlm`(active_learn) | `next_images(candidates, cfg)` 直接消费 `report.image_scores`(ensemble 条目) |
| fuse_reliable | `decision.fusion.fuse` | 同参同构,多一个 `tracker=` keyword-only 参数,可原地替换调用 |
| LSHIndex | `PhashRegistry`(A43) | `index.build_from(registry)` 只读装载;查询键口径与 `find_similar` 对齐(sha256/site/verdict_tag) |
| UnionFindKernel | `EvidenceGraph`(A46) | 经 `export()/export_json()` 互导;`site:<url>` 作节点 id |
| SessionExecutor | `executor_playwright.execute` | 批量循环里以 `with SessionExecutor(cfg) as ex: ex.run(plan)` 替代逐计划 `execute`;dry_run 分支原样委托,行为一字不差 |
| sched_kernel | `scheduler.run_once`(A39) | 轮选阶段用 `select_round(items, budget)` 得本轮目标,再交给既有逐项扫描 |
| SQLiteKernel | 全仓各 sqlite 封装 | 新代码首选底座;打开既有库零迁移零改写,可安全共存 |
| telemetry_export | `telemetry.snapshot()` | `render_metrics()` 直通导出;service 挂 `/metrics` 属运营者自由扩展(service 不改) |
| threat_kernel | CI / 发布前 | `python -m netsentinel.security.threat_kernel`(main CLI),退出码 2 即有新发现违规 |

### 5.5 不变式(接入后仍须成立)

- 任何开关组合下,**既有 2790 项测试不改一行应全绿**(红线 29);
- 任何学习型内核不发起网络请求、不外发任何文本/图片(红线 30 + 各模块
  遥测只存名称与数字的红线 17);
- 任何 bench 断言在任何机器上重复运行结果一致(红线 31)。

---

## 附:文档信息

- 依据版本:CONTRACTS-V7.md(A123–A142);代码快照 2026-10-02
  (A123–A137 已合入;A138/A139/A140/A142 并行开发中,相关章节已标注);
- bench 数值来源:文中"实测"= 当日运行 `kernel_selfcheck()` 输出;
  其余数字引自对应测试文件的 `test_v7_bench_*` 断言(已注明文件与用例名);
- 责任:A141。修订请同步 CONTRACTS-V7.md 与 docs/UPGRADE_V7.md(A142)。
