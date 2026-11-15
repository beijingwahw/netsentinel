# NetSentinel 文档索引(INDEX)

全部 36 篇文档的分类索引(33 篇 `docs/*.md` + 3 篇 `docs/regulations/`)。另有 17 份冻结契约(`CONTRACTS.md` 与 `CONTRACTS-V2.md`…`CONTRACTS-V17.md`,项目根目录)与 `CHANGELOG.md`(33 版迭代总账),不属本索引但常被引用。

**新读者路径**:ETHICS(必读红线)→ USAGE(上手)→ ARCHITECTURE(结构)→ 按需深入。

## 一、基础(定位 / 用法 / 架构 / 红线)

| 文档 | 内容 |
| --- | --- |
| [../README.md](../README.md) | 项目门面:定位、目录、入口速查、安装与快速开始 |
| [USAGE.md](USAGE.md) | 全部 CLI 入口、队列状态机、SELECTORS 13 键、18 步序列、Config 分组、FAQ |
| [ARCHITECTURE.md](ARCHITECTURE.md) | L0–L5 分层架构、模块职责表、数据流时序、契约机制 |
| [ETHICS.md](ETHICS.md) | 使用红线与法律提示(必读) |
| [REDLINES.md](REDLINES.md) | 53 条编号红线总表(逐条提炼 + 38/39 重号说明) |
| [API.md](API.md) | REST 服务端点参考(scan/jobs/trace/queue/plan/model) |
| [DEPLOY.md](DEPLOY.md) | 部署:依赖矩阵、密钥、四种运行形态、自启、容器、审计校验 |
| [SETUP_GUIDE.md](SETUP_GUIDE.md) | 安装后接管与连接向导(端口 8766)指南 |

## 二、流程与专题指南

| 文档 | 内容 |
| --- | --- |
| [BATCH_GUIDE.md](BATCH_GUIDE.md) | 批量工作流权威指南(batchflow → batch_tui → finishflow) |
| [FINISH_AGENT.md](FINISH_AGENT.md) | 结案代理指南:收官汇总与依次举报(红线 36) |
| [GROUPING.md](GROUPING.md) | 归纳规则白皮书:canonical 域 / 镜像变体 / 图片指纹团伙 |
| [SESSION.md](SESSION.md) | 会话累积流程:跨会话按网站名字累积 / 存档 / 续跑 / 确认举报 |
| [DISCOVERY.md](DISCOVERY.md) | 线索发现指南:Yandex XML 优先、自定义关键词、零外呼默认 |
| [CONCURRENCY.md](CONCURRENCY.md) | 并发档位权威文档(V9 三档 / 压榨边界红线 35 / 调优) |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | V3 智能体与模型编排权威指南(案件智能体 / 级联) |
| [PLATFORM.md](PLATFORM.md) | V3 平台化运营手册 |
| [GO_LIVE.md](GO_LIVE.md) | 真实环境接入指南:golive 体检 / 只读探活 / 运营者环节 |
| [computer-use-integration.md](computer-use-integration.md) | Computer-Use 备选执行路线(A17,Node 驱动剧本) |

## 三、视觉模型与提供方

| 文档 | 内容 |
| --- | --- |
| [PROVIDERS.md](PROVIDERS.md) | 20 家视觉提供方总览(16 云端 + 4 本地) |
| [MULTI_PROVIDER.md](MULTI_PROVIDER.md) | 全平台视觉模型使用指南(切换 / 故障转移 / ensemble) |
| [PROVIDER_SETUP.md](PROVIDER_SETUP.md) | 各提供方密钥获取与本地部署指引 |
| [VLM_GUIDE.md](VLM_GUIDE.md) | GLM 视觉大模型接入指南(默认提供方,含合规边界) |
| [MODEL_SWITCH.md](MODEL_SWITCH.md) | 模型切换手册(modelmgr / 向导 / REST) |
| [KERNEL_EVOLUTION.md](KERNEL_EVOLUTION.md) | V7 内核进化白皮书(八大内核代际升级) |

## 四、升级记录(每轮一册)

| 文档 | 轮次主题 |
| --- | --- |
| [README_V3.md](README_V3.md) | V3 能力速览 |
| [UPGRADE_V2.md](UPGRADE_V2.md) | GLM 视觉大模型接入 + 平台扩展 |
| [UPGRADE_V3.md](UPGRADE_V3.md) | 智能体编排与平台化 |
| [UPGRADE_V4.md](UPGRADE_V4.md) | 全平台视觉模型统一接入 |
| [UPGRADE_V5.md](UPGRADE_V5.md) | 全模块工程提升(冻结 / 可验证 / 零新依赖) |
| [UPGRADE_V6.md](UPGRADE_V6.md) | V6/V6.5 批量案件流水线 + 搜索引擎线索发现 |
| [UPGRADE_V7.md](UPGRADE_V7.md) | 内核世界性进化(A123–A142) |
| [UPGRADE_V8.md](UPGRADE_V8.md) | 自动接管 · 连接向导 · 手动切换(A143–A162) |
| [UPGRADE_V9.md](UPGRADE_V9.md) | 三档并发 · 收官汇总 · 结案代理(A163–A182) |

> V10.0–V10.16(举报理由 / 个人模板 / 半自动焦点 / 批量预审 / 证据池自动上传 / 会话累积 / 创世纪架构升级等)无独立 UPGRADE 文档,记账于 [../CHANGELOG.md](../CHANGELOG.md);V10–V16 各轮并行契约见根目录 `CONTRACTS-V10.md`…`CONTRACTS-V17.md`。

## 五、门户调研

| 文档 | 内容 |
| --- | --- |
| [portal_12377_notes.md](portal_12377_notes.md) | 12377 门户调研笔记(A13:表单结构 / 分类取值 / 上传口) |
| [portal_shdf_notes.md](portal_shdf_notes.md) | 扫黄打非门户调研笔记(A14) |

## 六、法规语料(本地检索)

| 文档 | 内容 |
| --- | --- |
| [regulations/12377_categories.md](regulations/12377_categories.md) | 12377 举报分类要点 |
| [regulations/legal_basis.md](regulations/legal_basis.md) | 举报相关法规依据条目 |
| [regulations/shdf_scope.md](regulations/shdf_scope.md) | 扫黄打非平台受理范围要点 |

## 七、根目录契约与总账(不在 docs/ 下)

| 文件 | 内容 |
| --- | --- |
| `CONTRACTS.md` | 团队契约 v1(红线 1–5、SELECTORS 8 键初版、判定公式) |
| `CONTRACTS-V2.md` … `CONTRACTS-V17.md` | 每轮升级冻结契约(红线 6–53;V17 为创世纪全席共约) |
| `CHANGELOG.md` | 33 版迭代总账(V1.0 → V10.16,含 V6.5 红线 27/28 与 V9.1/V10.0 红线 38/39 记账) |
| `config.example.yaml` / `watchlist.example.yaml` / `profile.yaml.example` | 三份配置模板(真实文件已被 .gitignore 排除) |
