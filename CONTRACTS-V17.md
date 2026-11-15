# CONTRACTS-V17 · 创世纪全项目深度架构升级(6 席位并行,2026-10-13)

本波为负责人直研后的第六次全项目升级波:V10.11(会话累积)→ V10.12(跨平台
健壮性)→ V10.13(浏览器会话复用/批级事务)→ V10.14(增量关联折叠)之后,
按项目并行代理方法论一次性派 6 席位(G1–G6)做全项目架构级升级。

## §0 全席共约(违反即缺陷)

1. **文件互斥**:每席位只准写自己领地的源文件 + 自己的**新**测试文件
   (`tests/test_genesis_<域>.py`);跨领地改动一律由负责人收口。
2. **禁改基座**:`contracts.py` / `pyproject.toml` / `config.example.yaml` /
   `tests/conftest.py` / 他人领地文件 / `CHANGELOG.md`(负责人统一记账)。
3. **零 API 破坏**:只允许新增带默认值参数或全新函数/方法;既有调用方
   行为语义不变;默认路径与升级前等价(等价重构,非行为变更)。
4. **既有测试**:领地内既有测试必须全绿;仅当"升级对象本身"改变了
   可观察形态(如计数口径)才允许按红线 21 惯例更新断言,并在报告中
   逐条说明。
5. **纪律**:零新依赖、测试零外呼/零真实浏览器/零真实门户;中文报错与
   docstring;惰性导入惯例保持;不跑全仓回归(负责人终验)、不 git 提交。
6. **诚实边界**:优化经核实不可行/不安全 → 如实报告"放弃 + 原因",
   宁缺毋滥,严禁为改而改。

## §1 席位与领地

| 席位 | 领地(只准写这些) | 升级目标 |
| --- | --- | --- |
| G1 | `netsentinel/crawler/*` | v2 引擎接入浏览器会话复用;fetcher 批量下载消重 |
| G2 | `netsentinel/vision/{vlm_client,provider_throttle,response_repair,prompt_dialects}.py` | 传输层热路径消重(渲染缓存/桶快路径/正则预编译);密钥永不缓存 |
| G3 | `netsentinel/intel/{phash,phash_lsh}.py` | `find_similar` 内存分带预过滤(候选才精确汉明,返回语义逐位不变) |
| G4 | `netsentinel/ops/{pool,scheduler,concurrency}.py` | pool 未完成计数 O(1) 化(done-callback);scheduler 巡查批量读 |
| G5 | `netsentinel/submit/{executor_session,executor_playwright,rate_limit}.py` | 双执行器步骤辅助去重;频控窗口判断 O(1);HUMAN_GATE/验证码红线零触碰 |
| G6 | `netsentinel/storage/{event_log,replay}.py` + `netsentinel/report/*` | `append_many` 批量账本口;报表渲染热路径消重 |

## §2 验证口径

每席位完工前运行(3.11 解释器,项目根目录):

```
python -m pytest <领地全部既有测试文件> tests/test_genesis_<域>.py -q
```

全绿(允许 skip)才算交付;报告格式:改动点清单 / 文件清单 / 测试证据
(通过数)/ 放弃项与原因。
