# CONTRACTS-V18 · 遗留清偿收官波:契约勘误 + 未通电内核收口(V10.21 席位4,2026-10-13)

本波为 V10.21 遗留清偿收官波(四席位并行,席位1 数据目录 env / 席位2 时间源 / 席位3 SQLite 工厂 / 席位4 本文件)。本文件是**勘误与追认契约**,按项目增量契约传统新增:`CONTRACTS.md`(v1 主契约)与 `netsentinel/contracts.py` 均冻结不动。要解决的三笔账:

1. **主契约口径漂移**(V10.17 遗留①):CONTRACTS.md §3 选择器 8 键 / 步骤序列 12 步 / "附件不自动上传" / §5 测试路径 `/c/1`,四处均已与现行代码不符;
2. **红线 38/39 双源重号**(V10.17 遗留②):负责人直研轮与 CONTRACTS-V10 并行轮各自从 38 起编号,至今未合并;
3. **V10.17–V10.21 行为追认**:一批 `getattr(cfg, ..., 缺省)` 附加属性接线的默认值与语义散落各模块 docstring,无集中清单。

每个事实性断言均附源码出处(文件:行),全部经席位4 本轮 grep / 实测核实。

## §1 主契约勘误(CONTRACTS.md 口径对齐,主文件不动)

以下勘误**只改口径描述,不改任何代码**;现行实现以引出处代码为准,CONTRACTS.md 原文保留作历史档案。

### 1.1 §3 选择器:8 键 → 13 键

CONTRACTS.md §3 列 8 键(`url/type/desc/name/phone/file/captcha/submit`,CONTRACTS.md:100-110);V10.2"个人信息完整字段"(CHANGELOG.md:233)新增 5 键后,现行 `SELECTORS` 为 **13 键**(`netsentinel/submit/form_models.py:100-114`):

```python
url  type  desc  name  phone          # v1 原有 5 键
email  id  address  postcode  org     # V10.2 新增 5 键(#report-email/#report-idcard/
                                     #         #report-address/#report-postcode/#report-org)
file  captcha  submit                # v1 原有 3 键
```

冻结语义不变:`SELECTORS` 为冻结契约常量(`_FrozenSelectors`,原地修改即 `TypeError`,form_models.py:66-96);`captcha` 键仍然"只允许人工输入"(红线 1)。

### 1.2 §3 步骤序列:12 步 → 18 步(含 FOCUS 与自动上传)

CONTRACTS.md §3 记 12 步序列(CONTRACTS.md:113-114);现行 `build_plan` 生成 **18 步**(`netsentinel/submit/form_models.py:405-513`,V5 起"12 步列表字面量"注释已随扩步过时):

| # | 动作 | 说明 |
| --- | --- | --- |
| 1 | `GOTO` | 打开举报入口 |
| 2 | `WAIT` | 等待页面加载(1s) |
| 3 | `SELECT` | 选择信息类型 |
| 4–7 | `FILL` ×4 | 举报链接 / 具体描述 / 举报人姓名 / 举报人电话(v1 原有) |
| 8–12 | `FILL` ×5 | 电子邮箱 / 身份证号 / 通讯地址 / 邮政编码 / 单位名称(**V10.2** 新增,form_models.py:450-490,空值 `skippable`) |
| 13 | `SCREENSHOT` | 填写完成后截图 |
| 14 | `FOCUS` | **半自动焦点**:光标自动聚焦验证码输入框(**V10.1**,form_models.py:492-498;CHANGELOG.md:263) |
| 15 | `HUMAN_GATE` | 人工输入验证码(红线 1,不可配置关闭) |
| 16 | `CLICK` | 点击提交 |
| 17 | `WAIT` | 等待提交处理(2s) |
| 18 | `SCREENSHOT` | 提交结果截图 |

不变量:顺序固定;**绝不包含任何自动填写验证码的步骤**(form_models.py:392-393 docstring 红线重申);FOCUS 只是聚焦不打字,验证码输入与最终提交确认仍永远属于人工(红线 1/2)。

### 1.3 "附件 zip 不做自动上传" → V10.16 人工门前自动上传

CONTRACTS.md:115 记"附件 zip 不做自动上传(v1 由人工门完成)";V10.16(CHANGELOG.md:70)起,双执行器在 **HUMAN_GATE 之前**按门户配额自动上传证据截图:

- 配额:`PORTAL_QUOTA = {"12377": 10, "shdf": 5}`(`netsentinel/submit/evidence_pool.py:60`;每站 Top-10 池 `POOL_LIMIT=10`,evidence_pool.py:57);
- 执行点:执行器步骤循环命中 `HUMAN_GATE` 步时先调 `_auto_upload_evidence`(`netsentinel/submit/executor_playwright.py:673-676`;函数 docstring 明示"发生在 HUMAN_GATE 之前",executor_playwright.py:720-728;会话执行器 executor_session.py:304 同源);
- 红线 1/2 零触碰:上传失败只记 notes 降级为人工门手动上传,绝不中断举报;人工门随即可见核对已传附件、输入验证码并最终确认(docs/REDLINES.md §四 V10.16 附注同口径)。

### 1.4 §5 测试路径:`/c/1` → `D:\3`

CONTRACTS.md:135 记"完成前自测:`cd /c/1/netsentinel && python -m pytest ...`";项目现驻 `D:\3\netsentinel`(V10.17 文档还债已同步 DEPLOY.md,CHANGELOG.md:58 H5),自测命令应为:

```
cd /d/3/netsentinel && python -m pytest tests/test_<你的>.py -q
```

## §2 红线 38/39 重号消歧(正式定名 38a/38b、39a/39b)

历史成因(保留,详见 docs/REDLINES.md §二):V9.1/V10.0 两轮为负责人直研(红线记 CHANGELOG,无独立契约文件);CONTRACTS-V10 为并行代理轮,其 §0 自行从 38 起编号("累计 40 条",CONTRACTS-V10.md §0:8-10),未对账直研轮记账。此后 V11 起统一从 41 续编,两套 38/39 均被代码按各自语义引用,未做合并。本节正式定名,**a/b 与既有文档"A 面/B 面"逐一对应**:

| 正式名 | 一句话 | 面 | 首记出处 | 代码引用(即该面) |
| --- | --- | --- | --- | --- |
| **38a** | 真实接入 ≠ 自主运行:golive 只读验证(GET 首页取样 512B,不解析表单不提交);密钥/目标清单/逐组声明/每条验证码与最终提交永远属于运营者。 | A(直研) | CHANGELOG.md V9.1 节(2026-10-02) | `netsentinel/golive.py:3,237,260,303,312`;docs/GO_LIVE.md |
| **38b** | 统计诚实性:conformal 担保以认证层落地;真率 100% 的 CP 下界按闭式 δ^(1/n) 报告,禁止有限样本宣称绝对精度;对抗评测单点估计必须伴随 CI/检验字段。 | B(V10 契约) | CONTRACTS-V10.md §0:8 | `netsentinel/vision/guard_calib.py:25,37`(**guard_calib 引用即此面**;其 :53 已自注"本模块全部'红线 38'均指 38(B) 面") |
| **39a** | 个人信息只进表单不进日志(掩码);举报理由只基于已核实事实、禁止编造;≤39 字硬校验。 | A(直研) | CHANGELOG.md V10.0 节(2026-10-03) | `netsentinel/contracts.py:167,367`;`submit/reason_gen.py:3`;`submit/form_models.py:315,327`;`submit/profile.py` |
| **39b** | 透明日志完整性:审计 JSONL 的 Merkle 接入为安全失效设计(异常不丢审计本体,降级标记后停用树);checkpoint 根经 HMAC-SHA256 签名;包含性证明可离线独立验证(verify 绝不抛错只返回 False)。 | B(V10 契约) | CONTRACTS-V10.md §0:9 | `netsentinel/security/merkle.py` + `netsentinel/logging_util.py:12-15`(审计 Merkle 接入) |

**引用约定**:后续代码 / 文档 / 契约新引用一律带字母后缀(38a/38b/39a/39b);存量裸引用"红线 38/39"按引用方语境判读(安全/隐私语境 = a 面,统计/审计语境 = b 面),不强制回改。docs/REDLINES.md 已按本节同步消歧(§一/§二表格与正文)。

## §3 V10.17–V10.21 行为追认

### 3.1 `getattr` 附加属性一览(逐一 grep 核实)

下表为各内核经 `getattr(cfg, 名, 缺省)` 动态读取、**未收录进 Config 一等字段**的开关/参数(收录前的项目惯例;`contracts.py` 冻结期间以此方式扩展,缺省值 = 关闭 = 现状):

| 附加属性 | 读取处(文件:行) | 缺省 | 开启语义 | 引入 |
| --- | --- | --- | --- | --- |
| `enable_text_fusion` | `pipeline/orchestrator.py:1004` | `False` | 各页文本线索经 `intel.text_intel` 复算计入融合(可能抬升 fused 并上调档位,故默认关) | V10.17 H1 |
| `arbiter_enabled` | `pipeline/orchestrator.py:671,982`(`_wire_arbiter`) | `False` | ensemble 聚合后、verdict 判定前,成员分歧 ≥ `arbiter.DISAGREE_GAP`(0.35)的图送 GLM 第三方仲裁,ensemble 条目替换为 `model="vlm-arbiter"`;每站最多 `min(3, vlm_max_images_per_site)` 张外呼;计数 `scan.arbiter.enabled/skipped/errors` | **V10.21 席位4(本波)** |
| `graph_wire` | `pipeline/orchestrator.py:1089`;`pipeline/kernel_wire.py:67` | `False` | 批末站群图谱通电(图片 sha256/模板指纹/phash_near 边) | A194(V10.4 波) |
| `graph_wire_multihash` | `pipeline/kernel_wire.py:654` | `False` | 图谱多哈希候选(mirror/ring/pyramid) | V10.8(A229) |
| `ensemble_reliability_weights` | `pipeline/orchestrator.py:460` | `False` | Brier 反比可靠性加权集成(样本 ≥ MIN_N 才生效) | V7 |
| `model_negotiate_enabled` | `vision/multi_provider.py:599` | `False` | `ModelNotFoundError` 后换名协商重试(重试前 `spend_one()` 再记账,红线 19) | V10.17 H3 |
| `provider_rpm` | `vision/vlm_client.py:461`(`_resolve_throttle`);进程级注入 `set_default_rpm()`(`vlm_client.py:420`)**优先于** cfg 属性 | `None` | 每提供方礼貌限速令牌桶(真实等待计入 `throttle.waited`) | A74 / V10.17 H3 通电 |
| `cost_ledger_enabled` | `vision/vlm_client.py:666` | `False` | VLM 调用成本落账 jsonl | V10.4 波(A76) |
| `evidence_pool_dedup` | `submit/executor_playwright.py:469`;`submit/evidence_pool.py:30`;`submit/executor_session.py:304` | **`True`(默认开)** | 证据池 pHash64 汉明 ≤6 近重复剔除(名额回填不重复图;三重优雅降级)——**注意这是少数默认开启的附加属性**(仿 V10.16 自动上传先例) | V10.20 |
| `guard_model_path` / `guard_family` | `vision/guard_adapter.py:316`;`vision/classifier_base.py:48`(映射注释) | `""` | 本地守卫模型路径;缺席时 guard 适配器构造期抛中文 `ValueError`(缺省错误语义) | A217(V10.4 波) |
| `capture_engine` | `pipeline/orchestrator.py:169,186` | `"v1"` | `"v2"` = 增强采集引擎(滚动触发懒加载) | V2 引入 / V10.13 批量会话 |

**已升格 Config 一等字段**(消费方 getattr 读取路径同名字段无缝命中,缺省语义零变化;`contracts.py:225-233` 注明收录模式):`trace_enabled`(`contracts.py:390`,缺省 `False`,读取处 `orchestrator.py:1155`)、`abstain_enabled`(`contracts.py:391`,缺省 `False`,读取处 `orchestrator.py:599`)、`abstain_threshold`(`contracts.py:384`,0.35)、`bayes_reliability`(`contracts.py:400`,缺省 `False`,开启时**优先于** `ensemble_reliability_weights`,`orchestrator.py:457`)、`vlm_fallback_chain`(`contracts.py:315`,缺省空链)。

### 3.2 `NETSENTINEL_DATA_DIR` 全局覆盖语义(V10.21 席位1)

- **优先级:环境变量 > config.yaml 文件值 > 字段默认**(`config.py:96` docstring;实现 `config.py:620`"环境变量覆盖在文件加载/校验之后";`apply_data_dir` `config.py:533`,字面量 `DATA_DIR_ENV` `config.py:518`);
- 环境变量设置且非空白时,按默认目录布局**整体迁移** `data_dir` 及派生子路径(与 webui 语义逐字段一致);CLI / REST(service) / webui 三条入口同认一个变量(`config.py:88-99`);
- **环境变量未设置/空串/纯空白时,行为与升级前逐字节一致**(缺省主线零变化);
- 背景:V10.20 实跑体检发现该变量此前仅 webui 认(CHANGELOG.md:3 V10.20"环境发现①"),核心 CLI 数据目录退化为 CWD 相对默认。

### 3.3 V10.19–V10.21 关键行为变更摘要(CHANGELOG 记账,此处索引)

- **V10.19**(CHANGELOG.md:27):关键词工作台 `webui/keywords_page.py`(第 8 个独立入口页):`<data_dir>/keywords/active.txt` 当前活文件 + 命名词组库 + `.bak` 滚动备份;发现每次运行重新装载 → **编辑/切换落盘后下一次发现运行即生效**;页面零外呼(只展示 CLI 命令提示);词组名防穿越白名单。附带修复 webui 两处 UI 缺陷(页签控件 key 碰撞、证据图片相对路径双拼)。
- **V10.20**(CHANGELOG.md:3):9 处真 bug 根治(最重:Louvain 模块度内部项漏乘因子 2,`intel/graph_kernel.py`,社区判定曾系统性拆团)+ 3 项创新:`scripts/entry_check.py` 全入口离线体检器(27 入口,零真实外呼)、**证据池感知去重(默认开,见 §3.1 `evidence_pool_dedup`)**、关键词模板持久化 `<data>/keywords/templates.txt`;REST `GET /queue` 可选分页(缺省全量完全向后兼容);telemetry 8 线程并发回归测试;webui 控件 key 矩阵化审计。
- **V10.21**(本波,四席位):①席位1 `NETSENTINEL_DATA_DIR` 核心链路接入(§3.2);②席位2 时间源单源化 `netsentinel/timesource.py`(L0 基座级,`utc_now/now_local/now_iso` 等单源);③席位3 SQLite 连接工厂机制化(storage/intel/decision/discovery 领地,WAL+busy_timeout 口径统一);④席位4(本文件):arbiter 接线默认关(§3.1)、契约勘误(§1)、红线消歧(§2)、failover 通电核查(§5 移交)。

## §4 席位与纪律

### 4.1 本波四席位文件互斥表(只准写自己领地)

| 席位 | 领地文件 | 任务 |
| --- | --- | --- |
| 席位1 | `netsentinel/config.py` | `NETSENTINEL_DATA_DIR` 核心链路接入(env > 文件 > 默认) |
| 席位2 | `netsentinel/timesource.py` | 时间源单源化(L0 基座) |
| 席位3 | `netsentinel/storage/**`、`netsentinel/intel/**`、`netsentinel/decision/**`、`netsentinel/discovery/**` | SQLite 连接工厂机制化 |
| 席位4 | `CONTRACTS-V18.md`(新建)、`docs/REDLINES.md`、`docs/MULTI_PROVIDER.md`、`netsentinel/pipeline/orchestrator.py`、`tests/test_orchestrator*.py`、`tests/test_arbiter*.py` | 契约勘误 + 未通电内核收口(arbiter 接线 / failover 核查 / prompt_dialects 定位) |

公共纪律(沿 V17 全席共约,CONTRACTS-V17.md §0):禁改基座(`contracts.py` / `pyproject.toml` / `config.example.yaml` / `conftest.py` / `CHANGELOG.md`);跨领地改动由负责人收口;零新依赖;测试零外呼/零真实浏览器/零真实门户;中文报错与 docstring;不 git 提交;诚实边界(不可行/不安全 → 如实报告"放弃+原因")。

### 4.2 零 API 破坏声明(席位4)

- `run_scan` / `run_submit` 公开签名零变化;orchestrator 仅新增**模块级私有**工厂 `_default_arbiter_module`(orchestrator.py:536)与接线函数 `_wire_arbiter`(orchestrator.py:638),及 `_scan_once` 内 ensemble 之后的一处内部调用(orchestrator.py:982);
- 默认路径(`arbiter_enabled` 缺省 False)与 V10.20 基线一致;唯一可观察差异为 telemetry 计数器 `scan.arbiter.skipped` +1/轮(仿 V10.18 text_fusion 门控双侧计数先例:只加计数,零行为变化,CHANGELOG.md:45 V10.18 S1);
- `contracts.py` 冻结未触碰;`vision/**` 只读(failover/arbiter 均未改一行);零新依赖;不 git 提交。

## §5 验证口径与移交项

### 5.1 验证

```
cd /d/3/netsentinel
python -m pytest -k "orchestrator or arbiter or scan or fusion or e2e or contracts" -q
python -m pytest -q   # 全量(席位3 并行加测试,基线 ≥6751 passed)
```

席位4 新增测试:`tests/test_orchestrator_arbiter.py` 9 项(默认关炸弹不被触 / 开启+分歧触发且条目被替换 model=vlm-arbiter / 闭边界 0.35 触发 / 无分歧不触发 / 单成员不触发 / 内核未就位优雅降级 / arbitrate 抛异常优雅降级 / 仲裁整体性放弃原样返回不计 errors)。

### 5.2 移交项(本波不实施,登记负责人收口)

1. **failover 惰性导入接线**(详见 docs/MULTI_PROVIDER.md §5.2):`classifier_base._LAZY_IMPORT_MODULES`(`vision/classifier_base.py:50-56`)增加一行 `"failover": "netsentinel.vision.failover"`。核查结论:`failover` 内核功能完备且 `vlm_fallback_chain` 为一等字段(contracts.py:315),但注册 `register_classifier("failover", ...)`(failover.py:527)依赖模块被导入,而惰性映射与产品代码均无导入路径——实测 `get_classifier("failover", cfg)` 报 `ValueError: 未知视觉模型提供方:failover...`。该文件在席位4 领地之外(vision/** 只读),故只登记。
2. `guard_calib.py:53` 的自注"38(B) 面"可在后续顺手改为"38b"(非必须;本波定名后语义已无歧义)。
