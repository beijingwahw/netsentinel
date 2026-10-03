# NetSentinel V5 团队契约(A81–A102 并行升级)—— 对全部既有模块做世界级工程提升

> 前四轮 80+ 模块、1738 测试全绿。本轮**不加新功能**:对全部既有模块做
> 性能 / 健壮性 / 可观测性 / 代码质量 四条线的系统性升级,兼容性零破坏。

## 0. V5 升级规则(红线 21–23,叠加于既有 20 条)

21. **对外行为与 API 冻结**:公共函数签名不变(允许新增带默认值的 keyword-only 参数);`netsentinel/contracts.py`、`tests/conftest.py`、四份 CONTRACTS 文件、`pyproject.toml` 一律禁改;**全部既有测试必须原样通过**——极少数"测试本身断言了被升级的旧实现细节"的用例可以更新,但必须在最终报告中逐条列出改动理由。
22. **每项升级要可验证**:性能/健壮性改动必须(a)有新增测试锁定,或(b)报告中有可量化说明(改了什么、为什么更快/更稳);禁止无依据的"顺手重构"。
23. **升级不引入新第三方依赖**;遥测统一接 `netsentinel.telemetry`(负责人已落地,零依赖、只存名称与数字、绝不存密钥/URL 内容)。

## 1. 全组统一的升级菜单(按组适用,不强制全选,但每组至少:1 性能或健壮性 + 1 可观测 + 1 质量)

- **性能**:sqlite 一律 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`;批量接口去逐条循环(一次性 executemany / 批量 get);热点解析(如 providers.resolve 的 base_url/model/style 进程内缓存——**密钥绝不缓存**,每次现取);重复解码/重复打开文件消除(单遍流式:边复制边哈希);顶层导入瘦身(重依赖全部函数内惰性)。
- **健壮性**:幂等网络/IO 加指数退避重试(base+抖动,≤2 次,仅 GET/只读);断路器(连续 5 次失败→熔断 60s→半开探活,适用于 vlm_client/failover/crawler 侧,自行判断适用性);subprocess/外部进程交互必带超时;Windows 兼容复查(路径分隔、GBK 控制台、文件句柄释放);裸 `except:` 一律改窄类型;文件句柄/连接全部 with 或 finally。
- **可观测性**:关键入口 `telemetry.inc/timer`(命名规范 `<模块>.<动作>`,如 `fetch.page`、`vlm.chat`、`queue.add`);慢路径(>1s)WARNING 日志;错误计数 `telemetry.inc("<模块>.errors")`。
- **质量**:公共函数补全类型注解与 `__all__`;每模块 docstring 至少一个用法示例;魔法数字提为模块常量;中英文案与现有风格一致。

## 2. 分组与文件归属(A81–A102,只许改本组文件)

| 组 | 源文件 | 测试文件 |
| --- | --- | --- |
| A81 核心配置 | netsentinel/config.py, logging_util.py | tests/test_config.py, test_logging_util.py |
| A82 抓取 | crawler/fetcher.py, site_map.py, redirect.py | test_fetcher.py, test_site_map.py, test_redirect.py |
| A83 浏览器 | crawler/browser.py, capture_v2.py | test_browser.py, test_capture_v2.py |
| A84 分类底座 | vision/classifier_base.py, stub_classifier.py, ensemble.py | test_stub_classifier.py, test_ensemble.py |
| A85 GLM | vision/glm_adapter.py, vlm_prompts.py | test_glm_adapter.py, test_vlm_prompts.py |
| A86 缓存仲裁 | vision/vlm_cache.py, arbiter.py, page_vlm.py | test_vlm_cache.py, test_arbiter.py, test_page_vlm.py |
| A87 本地视觉 | vision/nudenet_adapter.py, hf_clip_adapter.py, preprocess.py, video_frames.py | 对应 4 个测试 |
| A88 路由容错 | vision/cascade.py, failover.py | test_cascade.py, test_failover.py |
| A89 统一传输 | vision/providers.py, vlm_client.py | test_providers.py, test_vlm_client.py |
| A90 多平台分类 | vision/multi_provider.py, model_negotiate.py, prompt_dialects.py, response_repair.py | 对应 4 个测试 |
| A91 目录观测 | vision/model_catalog.py, provider_quirks.py, provider_agreement.py, provider_throttle.py, cost_meter.py | 对应 5 个测试 |
| A92 诊断密钥 | vision/vlmctl.py, local_gateway.py, security/keys.py, security/vault.py | 对应 4 个测试 |
| A93 决策政策 | decision/verdict.py, fusion.py, conformal.py, policy/engine.py, policy/policy.example.yaml | test_verdict.py, test_fusion.py, test_conformal.py, test_policy.py |
| A94 队列学习 | decision/review_queue.py, four_eyes.py, intel/active_learn.py | 对应 3 个测试 |
| A95 证据签名 | evidence/packager.py, report/html_report.py, security/bundle_sign.py | 对应 3 个测试 |
| A96 表单门户 | submit/form_models.py, portal_12377.py, portal_shdf.py, portal_defs.py, portals/*.yaml | test_form_models.py, test_portal_12377.py, test_portal_shdf.py, test_portal_defs.py |
| A97 提交执行 | submit/executor_playwright.py, playbook_gen.py, llm_describer.py, describer_critic.py, rate_limit.py | 对应 5 个测试 |
| A98 编排智能体 | pipeline/orchestrator.py, agent/case_agent.py, agent/case_flow.py, netsentinel/__main__.py | test_orchestrator.py, test_case_agent.py |
| A99 情报 | intel/url_intel.py, text_intel.py, phash.py, graph.py, regulation.py | 对应 5 个测试 |
| A100 运营 | ops/scheduler.py, pool.py, adaptive.py, intel/site_memory.py, notify/hub.py | 对应 5 个测试 |
| A101 界面服务 | webui/app.py, dashboard.py, providers_page.py, service/app.py, cli/review_tui.py | test_webui_smoke.py, test_dashboard_helpers.py, test_providers_page.py, test_service_api.py, test_review_tui.py |
| A102 基准演示 | benchmarks/run_benchmark.py, adversarial.py, providers.py, scripts/make_png.py, demo_stub_scan.py, demo_multi_provider.py | test_benchmark.py, test_adversarial.py, test_provider_bench.py |

冻结(任何人不得改):`contracts.py`、`telemetry.py`、`tests/conftest.py`、`tests/test_telemetry.py`、四份 CONTRACTS、`pyproject.toml`、`config.example.yaml`、`docs/**`(文档由负责人收口)、`drivers/**`。

## 3. 工作流程(每组必须)

1. 通读本契约 + 自组每个文件(含测试);列一页升级计划(改什么、为什么、怎么验证)。
2. 实施升级(小步快跑,每改一处跑本组测试)。
3. 新增测试锁定每项行为(命名 test_v5_* 前缀便于识别)。
4. `cd /c/1/netsentinel && python -m pytest tests -q` 全仓回归:全绿才算完成;并行期他人文件的瞬态失败可用"移除我组文件后复跑"对照排除,但**最终一次全仓运行必须全绿**(时间点自选,报告注明)。
5. 报告:升级项清单(分类:性能/健壮性/可观测/质量)、量化说明、新增测试数、被更新的既有用例及理由(红线 21)、全仓最终结果。

## 4. 特别注意

- A98:orchestrator 的测试大量 monkeypatch 模块级工厂函数——升级不得破坏这些接缝;新增 telemetry 钩子在工厂内部,不改变函数签名。
- A89:providers.resolve 缓存时**密钥字段每次现取**(缓存键排除密钥);vlm_client 断路器按 provider 维度,熔断期间快速失败并记 telemetry。
- A101:streamlit 本机未装——UI 层改动只能靠纯逻辑层测试锁定;service 的 TestClient 测试已就位。
- A102:demo 脚本升级后必须实跑一遍确认输出完整(退出码 0)。
