# NetSentinel 批量工作流权威指南(BATCH_GUIDE)

> 适用版本:V6(A103–A122)。本文是"大批量筛选 → 归纳同名 → 依次批量举报"流水线的权威操作指南。所有行为以 `CONTRACTS-V6.md` 与实际代码为准;各命令的具体旗标以 `--help` 实际输出为准(项目并行开发中,个别模块收尾中)。归组判定规则(同名归纳、阈值调优、误并/漏并排查)详见 `docs/GROUPING.md`。

---

## 0. 开篇红线(先读这一节,再操作)

**批量 = 顺序编排,不是自动发射。**

V6 把"单站办案"升级为"批量案件流水线",升级的只是编排效率:机器负责扫、归、打包、排队;**核验、批准、声明、验证码、最终确认永远由人完成**。批量模式没有任何一处绕过人工门,也不允许有。

以下三条红线摘自 `CONTRACTS-V6.md` §0(编号 24–26,叠加于既有 23 条红线,全文引用):

> 24. **批量举报仍是"逐条人工门"**:批量只是顺序编排,每一条提交的验证码输入与最终确认仍由人工在执行器 HUMAN_GATE 完成;任何批量代码不得出现 auto_confirm=True 的真实提交路径(dry_run/测试除外)。
>
> 25. **批量确认声明(留痕)**:案件组进入批量队列前,运营者必须逐组完成证据核验并显式声明(声明文本入审计日志,含组名/条数/审核人);无声明的组不得提交。
>
> 26. **批量模式频控不得放宽**:`batch_item_interval_s >= submit_min_interval_s`(config 已强制校验);每日上限 `submit_max_per_day` 照常生效,额度用尽自动挂起可续批。

三条红线在代码里的落点(便于核对,均为已实现行为):

| 红线 | 代码落点 |
| --- | --- |
| 24 逐条人工门 | `submit/batch_submit.py` 的 `run_batch` 恒以 `auto_confirm=False` 调用执行器,模块内不存在任何置 `True` 的路径;`cli/batch_tui.py` 的 `queue-batch` 确认后只打印 dry_run 演练示例与真实命令文本,绝不调用执行器;`batchflow` 真实提交必须显式 `--exec` |
| 25 声明留痕 | `decision/batch_review.py` 的 `BatchReview.attest()` 强制审核人非空、声明文本必含"人工核实"四字,否则中文 `ValueError`;声明落 SQLite `attestations` 表并可写 JSONL 审计;`ready_entries()` 对未声明组整组排除 |
| 26 频控不放宽 | `config.py` 校验 `batch_item_interval_s >= submit_min_interval_s`(违反抛中文 `ValueError`);`run_batch` 的频控最小间隔取 `max(batch_item_interval_s, submit_min_interval_s)`,`submit_max_per_day` 照常生效;条数超过 `batch_max_items` 直接拒绝运行 |

---

## 1. 流水线总览

批量案件流水线共七站,顺序固定:

```
线索清单            机器干                        人干
--------  intake 加载/归一/拒绝留痕 (A107)
          plan 规划(同站去重+指纹未变跳过)
          batch_scan 并发扫描 (A108, 复用 A58 pool)
          归组:case_group 同站归并 (A104) → group_linker 团伙归并 (A105)
          合并证据包 → 复核队列入列一条/组 (A108/A109, note 前缀 "[组:组名]")
                                  queue approve 逐组人工确认
                                  batch_tui attest 逐组声明(红线 25)
          run_batch 顺序逐条:频控 → 计划 → 执行器 ←  每条:人工输入验证码 + 确认
          (额度用尽/中断 → 挂起,--resume 续批)
          batch_report 结案报告 (A114)
```

一句话:一个案件组 = 一份合并证据包 = 一次举报;整批 = 若干组按顺序逐条过人工门。

---

## 2. 输入清单格式(A107 bulk_intake)

支持三种格式,按扩展名分发(大小写不敏感):`.txt` / `.csv` / `.yaml` / `.yml`。文件不存在抛中文 `ValueError`(批量导入是显式动作,丢文件必须立刻暴露)。

行级归一化规则(三种格式统一):

- 去空白(strip);空行跳过;
- 无 scheme 的裸域名自动补 `https://`(如 `www.a.com` → `https://www.a.com`);
- 仅接受 http/https(ftp/mailto/javascript 等拒绝);必须有主机名;主机名含空格/反斜杠等非法字符拒绝;
- **坏行不中断整批**:连同中文原因记入拒绝清单 `[(原始行, 原因)]`;
- 合法 URL 按**精确串**去重保序(同站多 URL 的归并去重在规划阶段做,见 2.4)。

### 2.1 TXT(每行一个 URL,`#` 整行注释)

```text
# 2026-10 月度巡查线索(井号开头的整行是注释,空行忽略)
https://www.example-a.com/
www.example-a.com:8443/mirror
https://img.example-a.com/
https://shop.example-b.co.uk/
https://mirror.example-c.com.cn:8080/home
```

### 2.2 CSV(表头须含网址列)

表头识别 `url` / `URL` / `网址` / `链接`(ASCII 表头大小写不敏感),其余列忽略;utf-8 读取,BOM 容错;缺网址列整体报错(中文,含实际表头)。

```csv
url,来源,备注
https://www.example-a.com/,热线线索,2026-10-01 来电
https://img.example-a.com/,巡查,疑似镜像
https://shop.example-b.co.uk/,巡查,
192.168.10.7:8000,专项行动,IP 直连也可(自动补 https://)
```

### 2.3 YAML(顶层列表,或 `{urls: [...]}` 映射)

需安装 PyYAML(未安装时中文报错并提示改用 .txt/.csv)。

```yaml
# 顶层为 URL 列表(或等价的 urls: [...] 映射)
- https://www.example-a.com/
- https://img.example-a.com/
- https://shop.example-b.co.uk/
- https://mirror.example-c.com.cn:8080/home
```

### 2.4 加载与规划命令(只规划,绝不扫描)

```bash
python -m netsentinel.ops.bulk_intake --input leads.txt --plan --config config.yaml
```

输出示例(中文摘要):

```
批量清单(leads.txt)加载完成:合法 URL 42 个,拒绝 3 条
  拒绝:ftp://old.example.com/ —— 协议仅支持 http/https(当前:ftp)
  ……
扫描规划:待扫 31 个,指纹未变跳过 5 个,同站重复 6 条
```

规划(`plan_scan`)两条规则:

1. **canonical 同站去重**:按可注册域归并(详见 `docs/GROUPING.md`),同站的后续 URL 不进待扫清单,计入 `duplicate_urls`,每站只保留第一个出现的 URL;
2. **指纹未变跳过**:站点指纹记忆(`<data_dir>/site_memory.db`,TTL 72 小时)中上轮指纹未变的站点计入 `skipped_unchanged`(记忆不可用时一律按需扫,宁可多扫不漏扫)。

---

## 3. 全流程命令序列

以下 8 步是批量办案的标准走法。示例统一假设:清单 `leads.txt`、配置 `config.yaml`、队列库 `data/review_queue.db`。

### 步骤 0:确认配置(V6 新增字段)

| 配置项 | 默认 | 约束(config 强制校验) | 含义 |
| --- | --- | --- | --- |
| `group_merge_phash_overlap` | 0.3 | — | 组间图片指纹重叠率(Jaccard)阈值,详见 GROUPING.md |
| `group_merge_template` | True | — | 是否把"共享模板"弱证据计入团伙并组 |
| `batch_max_items` | 20 | 1 ≤ 值 ≤ 50 | 单批最大举报条数(超出截断/拒绝) |
| `batch_item_interval_s` | 90 | ≥ `submit_min_interval_s` | 批内两次提交最小间隔(红线 26) |
| `batch_require_attestation` | True | — | 批量前须逐组完成声明(红线 25) |
| `submit_min_interval_s` | 60 | ≥ 30 | 单条提交最小间隔(全局,既有项) |
| `submit_max_per_day` | 5 | 1 ≤ 值 ≤ 20 | 每日提交上限(全局,既有项) |
| `dry_run_default` | True | — | 缺省干跑;真实执行必须显式 `--exec` |

### 步骤 1:导入与规划(见第 2 节)

```bash
python -m netsentinel.ops.bulk_intake --input leads.txt --plan
```

### 步骤 2:批量扫描与归组入列(A108/A119)

```bash
python -m netsentinel.batchflow --input leads.txt --config config.yaml
```

`run_flow` 一次完成:intake 加载 → 规划 → `batch_scan` 并发扫描(复用 ops.pool 有界并发,失败站点进 `summary["errors"]` 不中断整批)→ `group_and_enqueue` 归组(同站归并 + 团伙归并,阈值取配置)→ 需复核组逐组打**合并证据包**(组内文件按 sha256 去重,agg 取组内最大、判定取最严重档)→ 复核队列入列一条,备注前缀 `[组:组名]`。结束打印中文分组摘要并提示进入 TUI 声明。

要点:

- 判定为 clean 且 agg 未达 `review_threshold` 的组不入列(计 `skipped_clean`),不污染人工队列;
- 入列条目一律 `pending`,与单站流程同队——批量从这一步开始就没有任何特权。

### 步骤 3:人工复核批准(逐组)

```bash
python -m netsentinel queue list --status pending
python -m netsentinel queue show --id 1        # 打开证据包人工核验后再批准
python -m netsentinel queue approve 1 --note "已人工核实组内全部站点证据"
```

组的合并条目被 `approve` 后才有资格进入批量队列(交互式操作也可用复核 TUI:`python -m netsentinel.cli.review_tui`)。

### 步骤 4:批量复核 TUI——逐组声明(红线 25)

```bash
python -m netsentinel.cli.batch_tui --config config.yaml
```

在 TUI 内完成(命令参考见第 7 节):

```
批量复核> groups
批量复核> show example-a.com
批量复核> attest example-a.com --reviewer 张三
我已逐站人工核实(Y/N): Y
批量复核> ready
```

声明的技术语义:

- 声明前必须先看组内逐条明细(站点/判定/agg/证据包路径),然后回答"我已逐站人工核实(Y/N)"——仅接受 `Y`/`y`;N、空回车、乱码、EOF 一律取消,**绝不落声明**;
- 声明文案为固定模板,必含"人工核实"四字(校验在 A110 收口),落 `attestations` 表(组名主键,同组重复声明=覆盖更新并刷新时间戳)并可写 JSONL 审计(含组名/条数/审核人);
- **声明只代表核验完成,不触发任何提交**;`batch_require_attestation=False` 仅当配置明示才跳过声明要求。

### 步骤 5:确认待批量清单

TUI 内 `ready`(列出 approved 且已声明的条目)与 `queue-batch [--portal 12377|shdf]`(队列预览:条目表 + 频控提示 + "确认将依次批量举报 N 条,每条仍需人工输入验证码(Y/N)")。`queue-batch` 确认后只打印 dry_run 演练调用示例与真实执行命令,**本 TUI 绝不执行提交**。

### 步骤 6:真实执行——逐条人工门(红线 24)

```bash
python -m netsentinel.batchflow --resume <批次号> --exec
```

`--exec` 才是真实提交路径;批次号由 `BatchState.new_batch` 建批后确定(TUI 的 queue-batch 预览会提示)。内部走 `run_batch` 顺序逐条:**频控前置 → 构造计划 → 执行器(auto_confirm 恒为 False)**。每一条执行到验证码与最终确认时,浏览器停在 HUMAN_GATE,由**人工**输入验证码并点击确认——一条完成后等待 `batch_item_interval_s` 秒再处理下一条。

### 步骤 7:断点续批(额度用尽/中断后)

```bash
# 明日额度恢复后,或中断后:
python -m netsentinel.batchflow --resume <批次号> --exec
```

原理见第 6 节(BatchState 状态机)。

### 步骤 8:结案报告(A114)

批次完成后生成中文 HTML 结案报告(批次号/时间/条数、逐条状态表、提交成功截图路径清单、声明清单、含"每条均经人工门完成验证码"声明的结论段):

```python
from netsentinel.submit.batch_state import BatchState
from netsentinel.report.batch_report import render_batch_report

state = BatchState("data/review_queue.db")     # 与队列同库(示例路径)
summary = state.summary(<批次号>)
out = render_batch_report(<批次号>, summary, "data/batch_report_<批次号>.html")
```

零外呼端到端演示(临时本地 fixture、全程 dry_run、零真实提交)可运行 `python scripts/demo_batch_flow.py`。

---

## 4. 频控与额度如何作用于批量(红线 26)

| 机制 | 取值 | 作用点 | 不通过时的行为 |
| --- | --- | --- | --- |
| 批内间隔 | `max(batch_item_interval_s, submit_min_interval_s)`(默认 max(90, 60)=90s) | 每条提交前 `RateLimiter.can_submit()` 检查距上次提交的间隔;真实模式下每条完成后还 sleep `batch_item_interval_s` | 当前条记 `rate_limited`,**整体挂起返回**(不 sleep 死等),note 提示约 N 秒后再试,可续批 |
| 每日上限 | `submit_max_per_day`(默认 5,1~20) | 每条提交前检查本地自然日已提交次数(跨天自动清零) | 同上,note 提示"请明日再提交" |
| 单批上限 | `batch_max_items`(默认 20,1~50) | `ready_entries` 截断 + `run_batch` 前置校验 | 截断留待下一批 / 直接中文 `ValueError` 拒绝运行(见第 5 节) |

要点:

- 批量间隔**不得低于**单条最小间隔,config 已强制校验(`batch_item_interval_s >= submit_min_interval_s`,违反即中文报错,配置根本加载不过去);运行时再取两者较大值,双保险;
- 提交时间戳落 `data/rate_limit.json`(JSONL,每行一个 ISO 时间戳;与单站提交流水线共用同一份状态,批量与单条互不占便宜);
- **挂起 ≠ 失败**:挂起时已完成的条目结果保留、状态已落库,当前条标记 `rate_limited` 可续批;恢复后从断点继续,已 `submitted` 的条目不会重复提交;
- dry_run 不受频控节奏影响(条间等待跳过),用于演练排队。

---

## 5. batch_max_items 与截断

单批条数受 `batch_max_items`(默认 20,合法区间 1~50)约束,有两道闸:

1. **ready 截断(软闸,自动)**:`ready_entries` 产出的待批量清单超过 `batch_max_items` 时**截断保留前 N 条**(按队列 id 升序),WARNING 日志 + telemetry `batch_review.truncated` 标注,其余条目留待下一批——不需要人工干预,分多天自然消化;
2. **run_batch 校验(硬闸,拒绝)**:传入 `run_batch` 的条数若仍超过 `batch_max_items`,直接抛中文 `ValueError` 拒绝运行(提示拆分批次或调高上限后分批执行)。

配合 `submit_max_per_day`(1~20)理解:一天最多真实提交 `submit_max_per_day` 条,所以 20 条的默认单批在默认日额 5 条下通常要跨数日续批完成——这是**设计行为**,不是故障。

---

## 6. 断点恢复原理(BatchState 状态机)

批次状态由 A113 `BatchState` 落 SQLite(WAL 模式),两张表:

- `batches(id, created_at, note)`:批次;
- `batch_items(batch_id, entry_id, group_name, status, error, updated_at)`:批次内逐条状态。

条目状态机(状态 ∈ pending / running / submitted / failed / skipped / rate_limited):

| 状态 | 含义 | 是否终态 | 续批(`resume`)时 |
| --- | --- | --- | --- |
| `pending` | 已入批,尚未处理 | 否 | 重新取出执行 |
| `running` | 正在执行(进程中断时残留) | 否 | 重新执行该条(未提交成功即安全重跑,人工门天然幂等) |
| `rate_limited` | 频控/额度挂起 | 否 | 额度恢复后优先续跑 |
| `submitted` | 提交成功(已记账+审计) | 是 | 不再重复执行 |
| `failed` | 失败(含中文原因,留痕) | 是 | 不自动重试(人工研判后再办) |
| `skipped` | 干跑/未真实提交 | 是 | 本批视同完成 |

每条在 `run_batch` 中的处理顺序:① `stop` 回调触发则整批暂停(`paused=True`,Ctrl-C 语义)→ ② 状态记 `running` → ③ 频控不通过则该条记 `rate_limited` 并**整体挂起返回** → ④ 按门户构造计划 → ⑤ 执行器执行(`auto_confirm=False` 恒定)→ ⑥ 成功则 `submitted` + 频控记账 `rate.record()` + 审计事件 `batch_submit` → ⑦ 异常/失败则 `failed`(中文原因)并**继续下一条** → ⑧ 真实模式下条间等待 `batch_item_interval_s` 秒。

**断点续批 = `BatchState.resume(batch_id)` 取出全部未终态条目(pending/running/rate_limited),再交给 `run_batch` 执行**;对外即 `python -m netsentinel.batchflow --resume <批次号> --exec`。

---

## 7. TUI 命令参考(batch_tui)

启动:`python -m netsentinel.cli.batch_tui [--db PATH] [--config PATH]`(缺省取配置的 `db_path`)。逐行读命令,Ctrl+D/Ctrl+Z+回车 或 `quit` 退出(退出码 0;未完成的声明与队列保持原状,绝不自动提交)。

| 命令 | 说明 |
| --- | --- |
| `groups` | 分组总览:组名 / 条数 / 判定徽章(🔴 nsfw / 🟡 suspect / 🟢 clean)/ agg 最大 / 已声明(✓/✗);只统计待复核(pending)与已确认(approved)且备注带 `[组:名]` 标记的条目 |
| `show <组名>` | 组内逐条明细:站点 / 判定 / 聚合分 / 状态 / 证据包路径(请人工核验内容) |
| `attest <组名> --reviewer <名字>` | 批量确认声明:先打印组明细,再答"我已逐站人工核实(Y/N)";仅 Y/y 落声明 |
| `ready` | 待批量清单(A110 `ready_entries`:approved 且已声明的条目;受 `batch_max_items` 截断) |
| `queue-batch [--portal 12377\|shdf]` | 批量队列预览:条目表 + 频控提示 + 人工确认问题;确认后**只打印** dry_run 演练示例与真实执行命令(`python -m netsentinel.batchflow --resume <批次号> --exec`),不执行任何提交 |
| `help`(或 `?`) | 显示帮助与安全红线提示 |
| `quit` / `exit` / EOF | 退出 |

安全语义(代码级保证):本 TUI 不 import、不调用批量执行器;`queue-batch` 的确认问题"确认将依次批量举报 N 条,每条仍需人工输入验证码"回答 Y 之后仍然只是打印命令——真实提交必须在 TUI 之外、由人显式执行 `--exec` 命令完成。

---

## 8. 常见问题(FAQ)

**Q1:明明是同一伙的站点,组没并起来(拆成了多个组)?**
团伙归并依赖"图片指纹重叠 ≥ 阈值"或"证据图谱关联边"。先降阈值:把 `group_merge_phash_overlap` 从 0.3 降到 0.2 甚至 0.15(镜像站只共享少量同图时重叠率低);确认 `group_merge_template: true`(模板证据参与并组);两站图片完全不同则任何阈值都并不了,需依赖图谱边(shared_image / phash_near / redirect)。完整排查步骤见 `docs/GROUPING.md` 第 9 节。

**Q2:之前被拒(rejected)的条目又出现在清单/组里?**
批量清单重新导入时,intake 的去重只看"同站",不查历史驳回记录;同组内"曾 rejected 又复现"的站点用 A117 `group_stats.repeat_offenders(entries, groups)` 排查。处置原则:域名换内容上线(改版)属正常复现,按新证据重新复核;恶意重复提交同一已驳回站点,保持 reject 即可——rejected 条目不参与批量(`ready_entries` 只取 approved)。

**Q3:跑到一半提示"额度/间隔限制,可续批"?**
正常现象(红线 26):当日提交数达 `submit_max_per_day`,或距上次提交不足最小间隔。批次已自动挂起并落库,**明日执行 `python -m netsentinel.batchflow --resume <批次号> --exec` 续批**即可,已提交的条目不会重复。

**Q4:清单里有些行没有被扫描?**
三种去向,均可在 intake 输出中核对:① 拒绝清单(协议不支持、无主机名、主机名非法等,附中文原因);② `duplicate_urls`(同站多 URL,canonical 去重,每站只扫第一个);③ `skipped_unchanged`(72 小时内指纹未变的站点跳过重扫)。

**Q5:组已声明,`ready` 还是空的?**
按顺序检查:① 组内条目是否 `approved`(先 `queue approve`,pending 不算);② 声明的组名与条目备注 `[组:名]` 中的名是否一致(声明按组名主键匹配);③ 是否被 `batch_max_items` 截断(看 WARNING 日志,超出部分留待下一批);④ `batch_require_attestation=True` 时未声明组整组排除(日志会列出被排除的组名)。

**Q6:dry_run 与真实执行差在哪?**
`dry_run_default` 默认 True;`--exec` 才真正驱动浏览器。干跑:不启动浏览器、不产生真实提交、跳过条间等待,条目状态记 `skipped`(干跑/未真实提交);真实执行:逐条打开门户、停在人工门等验证码、提交成功后记账 `submitted`。建议任何新批次先完整干跑一遍再 `--exec`。

**Q7:验证码到底谁来输?**
永远是**人**。批量模式下每一条执行到验证码步骤都会停下来等人工输入与最终确认(红线 24);系统不存在代填验证码、自动确认的代码路径,配置里也没有这样的开关。

---

*维护:A121(文档工程)。行为口径变更请先改 `CONTRACTS-V6.md`,再同步本文。*
