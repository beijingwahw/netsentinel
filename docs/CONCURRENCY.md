# NetSentinel 并发档位权威文档(V9 · CPU 自适应三档并发)

> 本文是 V9「CPU 自适应三档并发」的权威文档(A177):档位怎么定、怎么探测、
> 压榨什么、**不压榨什么(红线 35)**、进程池纪律、过载让位(load_guard)、
> 怎么验证(tier_bench)、怎么调优,以及与 V6 批量流水线 / finishflow 的关系。
>
> 依据:CONTRACTS-V9 §0/§1/§2/§3 与实际代码 `netsentinel/ops/cpu_profile.py`(A163)、
> `netsentinel/ops/concurrency.py`(A164)、`netsentinel/ops/tier_state.py`(A165)、
> `netsentinel/vision/parallel_classify.py`(A166)、`netsentinel/ops/load_guard.py`(A167)、
> `netsentinel/finishflow.py`(A169)、`netsentinel/evidence/parallel_pack.py`(A171)、
> `benchmarks/tier_bench.py`(A173)。

## 1. 为什么要档位:本机 CPU 自适应

NetSentinel 既可能跑在 16 逻辑核的工作站上,也可能跑在 2 核的旧笔记本上。
把并发数写死(比如固定 workers=8)必然顾此失彼:大机器跑不满、小机器被拖死。
V9 的解法是**以本机核数为唯一变量**的三档换算:所有执行器(扫描线程池、
并行分类、并行打包、进程池)的规模都从同一条档位公式推导,配置里只需要
表达意图——「省电」「默认」「极限压榨」——而不是手工维护数字。

两个口径,一个来源(A164 `ops.concurrency`):

- **`io_workers(cfg)`**:线程池规模(IO 等待型:抓取等待、VLM 请求等待、
  证据包复制/哈希/压缩),= A163 `tier_workers(tier, reserve=cpu_reserve)`;
- **`cpu_workers(cfg)`**:进程池规模,= `min(io_workers, cores)`——
  **进程池永不超过物理核数**(红线 37 双重钳制,超了会 WARNING 并钳回)。

### 1.1 三档定义(权威,CONTRACTS-V9 §1)

以 `N = os.cpu_count()`(探测不到回退 2)为核数:

| 档位 | 公式 | 定位 |
| --- | --- | --- |
| `low` | `max(1, N//4)` | 后台 / 省电 |
| `mid` | `max(1, N//2)` | **默认** |
| `high` | `max(1, N - cfg.cpu_reserve)` | **最大限度压榨 CPU**(reserve=0 即全核) |

换算示例(`reserve=1` 缺省):

| N(逻辑核) | low(N//4) | mid(N//2) | high(N-1) |
| ---: | ---: | ---: | ---: |
| 4 | 1 | 2 | 3 |
| 8 | 2 | 4 | 7 |
| 16 | 4 | 8 | 15 |
| 32 | 8 | 16 | 31 |

**reserve 说明**:`cfg.cpu_reserve`(缺省 1)是**高档保留核心数**——
高档压榨时仍给操作系统和其他任务留出的核。它**只作用于 high 档**:
low/mid 不受 reserve 影响(它们经 `N//4` / `N//2` 天然只占四分之一/一半,
余量天然存在);负数按 0 处理;`reserve ≥ N` 时 high 退化 1,
任何档位结果恒 ≥ 1、恒 ≤ N(红线 37)。`reserve=0` 即「全核压榨」(见 §7)。

配置字段(`contracts.py`):

```python
concurrency_tier: str = "mid"    # low(cores/4) / mid(cores/2) / high(cores-reserve,极限压榨)
concurrency_auto: bool = False   # 首次运行自动探测 CPU 并写入建议档位(用户显式配置优先)
cpu_reserve: int = 1             # 高档保留核心数(0=全压榨;低/中档自动预留)
```

## 2. 检测机制

### 2.1 `detect()`:本机 CPU 画像(A163)

`ops.cpu_profile.detect()` 返回**恰好四键**的画像字典,**零外呼**
(不 import 任何网络库、不做任何联网探测、不安装任何依赖):

| 键 | 内容 |
| --- | --- |
| `cores` | 逻辑核数。优先 `NETSENTINEL_FAKE_CORES` > `os.cpu_count()` > 回退 2 |
| `arch` | `platform.machine()`,如 `"AMD64"` / `"x86_64"`;空串回退 `"unknown"` |
| `platform` | `"<system>/<sys.platform>"`,如 `"Windows/win32"`、`"Linux/linux"` |
| `psutil` | psutil 是否可导入(惰性探测,**绝不安装**;不可用只影响 recommend 的降档采样) |

档位换算 `tier_workers(tier, *, reserve=1, cores=None)`:非法档位抛中文
`ValueError`(必须是 `low / mid / high` 之一);`cores` 显式注入优先,
缺省取 `detect()` 的核数,坏输入按 2 容错。

### 2.2 `NETSENTINEL_FAKE_CORES`:测试/演示钩子

环境变量 `NETSENTINEL_FAKE_CORES` 可覆盖 `detect()` 的核数,供离线测试与
离线演示(A179)使用。**只有正整数才生效**;非整数、`<1`、空串一律忽略,
回退真实探测。例如 `NETSENTINEL_FAKE_CORES=4 python ...` 即可在一台 16 核
机器上模拟 4 核行为(low=1 / mid=2 / high=3)。

### 2.3 `concurrency_auto`:首启一次性自动建议(A165 `tier_once`)

`concurrency_auto=True` 时,批量入口(finishflow 等)会调用
`ops.tier_state.tier_once(cfg)` 做**会话幂等**的一次性初始化
(同一进程内第二次调用直接返回首结果浅拷贝,不再探测、不再写文件、
遥测每动作每会话恰一次):

1. `concurrency_auto=False` → `{"action": "disabled"}`,零副作用;
2. **持久档已存在 → `{"action": "kept"}`——auto 绝不覆盖既有档**
   (用户或上次运行已经选过);
3. 否则 `detect()` → `recommend(profile)` → 写入持久文件(`set_by="auto"`)
   → `{"action": "auto", "tier", "workers"}`,并惰性写一行审计
   (`log_event("tier_once", ...)`)。

建议规则(`recommend`,取向保守、降档克制):

- 核数 ≤ 2 → `low`;≤ 8 → `mid`;> 8 → `high`;
- psutil 可用且**系统占用 > 0.8** 时降一级(high→mid→low;low 已是底线不再降)。
  占用来源:画像显式 `cpu_usage` 优先,否则非阻塞采样
  `psutil.cpu_percent(interval=None)`;psutil 不可用 / 采样失败 / 读不出
  → **不降档**(读不出高占用就不动,宁保守不激进)。

### 2.4 持久文件 `data/concurrency.json` 与优先级

`TierState`(`ops/tier_state.py`)把「当前档 / workers / 核数 / 由谁设定」
落在 `data/concurrency.json` 一个小 JSON 里,**恰好四键**:

```json
{"tier": "mid", "workers": 8, "cores": 16, "set_by": "auto"}
```

- **读侧宽容**:文件缺失 / 不可读 / JSON 损坏 / 结构不符 → 一律 `None`,
  绝不抛出(档位建议是锦上添花,不该砸崩批量主流程);
- **写侧严格**:任一字段非法抛中文 `ValueError` 且不动原文件;成功则
  **原子写**(同目录临时文件 + flush + fsync + `os.replace`;Windows 下
  目标被并发短暂占用时最多重试 50 次 × 0.01s)。

**读取优先级(恒定,契约 §3 A165)**:

```
cfg.concurrency_tier(合法)> 持久文件 > "mid"
```

原因:`Config.concurrency_tier` 总有值(mid 是默认),无从区分「用户显式
mid」与「默认 mid」,故约定 **cfg 恒优先**;持久文件只作展示与 auto 建议
来源,**不参与生效档仲裁**。cfg 档位非法(仅绕过配置校验直接改 dataclass
才可能出现)才回退持久档,再回退 `mid` 并记 WARNING。auto 写入的持久档
**只是建议**,生效档仍按此优先级解析。

## 3. 压榨边界(红线 35,全文)

> **35. 压榨边界**:高档并发只作用于本地计算与本地回环 IO(并行分类/并行
> 打包/扫描池);对外网络的礼貌间隔(`fetch_delay_s`)、引擎限速、举报频控
> (红线 26)**一概不放宽**;进程池仅用于模块级纯函数的本地计算,不得
> pickle 分类器实例/连接。
> ——CONTRACTS-V9 §0(全文,逐字)

设计逻辑一句话:**加速的是可再生的本地资源(CPU / 本地盘 / 回环),不变的
是对外的、涉及他人的节奏(目标站点、模型服务、举报门户)。**

| 侧 | 清单 | 说明 |
| --- | --- | --- |
| **被档位加速**(workers 随档位变) | 扫描池(finishflow → batch_scan → run_pool 的本地线程池,同一时刻至多 workers 个站点在扫) | 站点间并发度提高,但每个站点自身的请求节奏不变 |
| | 并行分类 `classify_parallel`(线程模式压榨 VLM 请求等待;进程模式压榨本地 CPU 内核,如 `skin` 启发式) | 见 §4 |
| | 并行打包 `pack_all`(本地 IO:证据文件复制 / sha256 / zip) | 同 host 串行化防目录竞争,不同 host 真并发 |
| | 本地回环 IO(本地回环网关 / VLM API 的请求等待) | 回环不占用对外带宽,可放心压榨 |
| **一概不变**(与档位无关,不放宽) | 对外抓取礼貌间隔 `fetch_delay_s`(缺省 1.0s,相邻请求最小间隔) | `run_pool` 原样执行,不随档位变 |
| | 扫描提交侧节奏与背压(相邻提交停顿 1.0s + 0~0.5s 抖动;积压超 workers×2 多停一轮) | 提交侧礼貌,档位只改 `max_workers` 不改间隔 |
| | 引擎限速(模型引擎自身的限速) | 档位与引擎限速互不相干 |
| | 举报频控(红线 26:`batch_item_interval_s ≥ submit_min_interval_s`,缺省 90s/60s;每日上限 `submit_max_per_day=5`) | 全部沿用 V6 链,档位再高也不放宽 |

执行侧的落实:`finishflow` 对 `cfg` **只读**,绝不改写礼貌/频控字段;
`--tier` 覆盖经 `dataclasses.replace` 生成**新** Config(其余字段原样保留),
`cfg` 原样透传给扫描与汇总。收官汇总里也固定印着:「礼貌间隔与举报频控
不随档位放宽,红线 35」。

## 4. 进程池纪律:只传纯函数三元组,不 pickle 实例

红线 35 后半句的工程化(A166 `parallel_classify` 的 `use_processes=True`):

- **主进程只提交纯字符串三元组** `(分类器名, 图片路径, URL)` 给模块级
  纯函数 `_proc_classify`——分类器实例**绝不经 submit 传递**(不 pickle);
- **子进程按名构造**:子进程内经 `classifier_base.get_classifier` 惰性构造
  一次性实例(用缺省 `Config`;跨进程不传 cfg 定制——需定制的 IO 型模型
  请走线程模式注入实例)。未命中且名称在二级惰性导入表
  (`{"skin": "netsentinel.vision.heuristic_kernel"}`)时,先导入自注册
  模块再重试一次(只导入模块,仍不传实例);
- **返回普通 dict**,主进程按位对齐重组 `ImageScore`(`image` 复用原始
  evidence 对象,保序);子进程内任何异常**就地转 error dict**,不让
  异常跨进程边界传播;
- 进程池规模 = `cpu_workers`(`min(io_workers, cores)`,红线 37);
  构造失败统一转中文 `RuntimeError`(保留原始异常为 `__cause__`),
  **不自动吞、不静默降级**;
- 总量与超时上限(红线 37):单批 `MAX_BATCH=4096`(分类)/ `512`(打包),
  单任务取结果超时 300s / 600s;退出必 `shutdown` 并清掉未起跑的排队任务,
  不留孤儿进程。

为什么不 pickle 实例:分类器实例可能持有模型权重、网络连接等不可靠或
不可序列化的状态;传名不传物,子进程是全新解释器,按名构造天然安全,
也是 Windows spawn 语义下的唯一稳妥做法(见 §7.4)。

## 5. load_guard:高档压榨的过载让位(A167)

高档是「最大限度压榨」,但仍有一条软护栏:`ops.load_guard.LoadGuard`
在高档本地计算循环里埋**让位点**,避免把整机拖死。

- **采样口径**(纯标准库):本进程 CPU 利用率 = 相邻两次采样之间
  `os.times()` 的 `user + system` 差分 ÷ 墙钟差分(不含 `children_*`
  子进程部分;多线程叠加可能 > 1,同样按超阈值处理);
- **判定**(`should_throttle`):利用率**严格大于** `threshold`(缺省
  0.92)才让位,恰好等于不让;首采只建基线(样本不足 → False);
  相邻两次真实采样至少间隔 `interval_s`(缺省 0.5s),间隔内复用缓存
  样本——高频探询本身不推高 CPU 读数;
- **让位动作**(`maybe_yield`):判定 True 时 `sleep(0.05)` 并返回 True;
  单次让位固定 50ms、**有界可中断**,高档循环每次迭代重新判定,不存在
  无法中止的长睡眠;
- **锁纪律**(红线 37):内部锁只保护采样簿记,**睡眠发生在锁外**,
  过载让位绝不长期占锁、不阻塞其他线程;
- `times_fn` / `clock_fn` / `sleep_fn` 全部可注入,测试零真实 sleep。

用法(高档并行循环的标准让位点):

```python
from netsentinel.ops.load_guard import LoadGuard

guard = LoadGuard(interval_s=0.5)      # threshold 缺省 0.92
for item in heavy_items:               # 高档并行分类/打包等本地计算
    result = heavy_local_compute(item)
    if guard.maybe_yield():            # CPU > 92% 时让位 50ms
        logger.info("进程负载 %.0f%%,让位 50ms",
                    (guard.last_utilization() or 0) * 100)
```

## 6. tier_bench 解读:Barrier 峰值法(A173)

`benchmarks/tier_bench.py` 是三档并发的**量化证明工具**。它不测耗时——
**峰值 = Barrier 实测并发度,确定性计数非墙钟**(红线 31:同机同参数两次
运行结论恒一致,报告不含时间戳、逐字节可复现)。

方法(`peak_concurrency(task_fn, n, workers)`):

1. 建一个 `workers` 方的共享 `_CountingBarrier`(带到达计数的 Barrier 代理),
   线程池 `max_workers=workers`;
2. 提交 `n` 个任务(每档 n = workers×2,两轮循环),任务进入时在 Barrier
   上等待;
3. **全部 `workers` 方同时到达 → Barrier 放行**——放行即证明恰有
   `workers` 个任务并发在跑,峰值 = workers(确定性到达计数);
4. 部分等待超时(n < workers、个别任务抛异常没到达)→ 栏破,按
   **实际安全到达数**计峰值——异常任务从不进入计数,**不虚报**;
   每次等待都有超时兜底(缺省 10s),不挂死;池退出必
   `shutdown(wait=True)`(红线 37)。

实测峰值恒 ≤ workers ≤ 核数。

**本机实测**(报告 `benchmarks/out/tier_report.md`,16 核,来源 detect,
reserve=1):

| 档位 | 理论 workers | 实测峰值(Barrier) | 判定 |
| --- | ---: | ---: | --- |
| low(低·省电) | 4 | 4 | 一致 |
| mid(中·默认) | 8 | 8 | 一致 |
| high(高·压榨) | 15 | 15 | 一致 |

命令行:`python benchmarks/tier_bench.py --out benchmarks/out [--cores N]`;
退出码 0 = 三档实测与理论全部一致,2 = 存在不一致或参数错误。`--cores`
可离线注入核数复现任意机器的理论对照。

## 7. 调优

### 7.1 `reserve=0`:全核压榨

`cpu_reserve: 0` 时 high 档 = N(全核),配合 load_guard 的 0.92 让位护栏,
是「机器专用、压榨到底」的合法形态——红线 37 只要求 workers ≤ N,
reserve=0 恰等于 N,不越线。low/mid 不受 reserve 影响,无需为它们操心。

### 7.2 自动档建议

省心做法:`concurrency_auto: true` + 不设 `concurrency_tier`(保持默认
mid)。首启会按画像写入建议档(≤2 核→low;≤8 核→mid;>8 核→high,
负载 >0.8 降一级),之后绝不覆盖。要换档:改配置里的 `concurrency_tier`,
或一次性 `python -m netsentinel.finishflow --tier high ...`(经
`dataclasses.replace` 覆盖,礼貌/频控字段原样保留);要重新接受 auto
建议:删除 `data/concurrency.json` 后再跑一次。记住优先级恒为
**cfg 字段 > 持久文件 > mid**。

### 7.3 单核 / 小核机

公式自带 `max(1, ...)` 兜底:N=1 时三档全退化为 1 worker;N=2 时
high(reserve=1)也退化为 1。端到端测试(tests/test_v9_e2e.py,A176)
专门验证了 cpu_count=2 → tier=high → workers=1 的退化正确性。
小核机上档位机制自然安全,不会算出 0 或负数,也不会超核。

### 7.4 Windows 注意

- **spawn 会重新导入 `__main__`**:进程池(`process_pool` /
  `classify_parallel(use_processes=True)`)只应在 `if __name__ == "__main__":`
  守卫内的程序入口使用,或由测试显式注入 `mp_context`;构造失败统一转
  中文 `RuntimeError`(含守卫提示),不静默降级;
- **`data/concurrency.json` 原子替换**:Windows 下目标文件被读者短暂占用
  (`PermissionError`)时自动重试(50 × 0.01s),耗尽才抛出;
- **控制台编码**:tier_bench CLI 在非 UTF-8 控制台自动把 stdout/stderr
  切到 UTF-8,中文报告不乱码;
- 性能提示:进程池子进程按名构造一次性分类器(§4),首次 spawn 有导入
  成本,批量越大摊销越明显;小批量 IO 型(VLM 请求等待)用默认线程模式
  即可,无需进程池。

## 8. 与 V6 批量流水线 / finishflow 的关系

**V6 批量流水线**(batchflow → batch_scan → run_pool)的有界并发与礼貌
节奏全部保留,V9 不改其签名、不改其参数:

- `batch_scan` 签名冻结不收 workers;**finishflow**(A169)经
  `pool_runner` 包装(`_pool_runner_with_workers`:鸭子复刻
  `run_pool(cfg, items, *, run_scan=..., memory=...)` 签名,调用前
  `setdefault("workers", io_workers(cfg))`)把档位换算的 worker 数透传给
  A58 `run_pool`——worker 数只影响本地池并发,`fetch_delay_s` 等礼貌
  参数由 `run_pool` 原样执行,不随档位放宽(红线 35);
- `run_pool` 自身的 `workers` 缺省当前仍为 2(`DEFAULT_WORKERS`);
  契约 §3 计划将其缺省改为 None(缺省时经 `ops.concurrency.io_workers(cfg)`
  解析,显式传参不变),该接线由负责人在收口轮(A182)完成。**当前仓库里
  V9 档位的实际生效路径就是 finishflow 的包装注入**;
- 收官流程 `finish()`:tier_once(档位一次性初始化)→ io_workers(算
  worker 数)→ 并发扫描 → SummaryAgent 分类汇总 + 举报准备(列待声明组,
  红线 36 无自主提交权)→ 打印中文收官汇总(含档位 / worker 数与
  「礼貌间隔与举报频控不随档位放宽」提示);
- 举报准备 `run_report`(默认干跑):SequentialReportAgent 编排
  run_batch,逐组人工声明、逐条 HUMAN_GATE,频控全部沿用 V6 链
  (红线 26/36)——**与并发档位完全正交**;
- `parallel_classify` / `parallel_pack` / `load_guard` / `tier_bench`
  是旁路加速与验证件:由视觉流水线、证据链与基准 CLI 调用,同样只压榨
  本地计算与本地回环 IO,不改变任何对外节奏。

## 附:消费方速查

| 模块 | 用什么 | 干什么 |
| --- | --- | --- |
| finishflow(A169) | `tier_once` + `io_workers` | 收官扫描池并发 |
| parallel_classify(A166) | `thread_pool`(label=classify)/ `cpu_workers` | 并行图像分类 |
| parallel_pack(A171) | `thread_pool`(label=pack) | 并行证据包构建 |
| load_guard(A167) | 独立(高档循环让位) | CPU > 0.92 时 sleep 0.05s |
| tier_bench(A173) | `tier_workers` / `detect` | 三档峰值验证 |
| webui/runs_page(A175) | 档位徽章 / CPU 建议 | 展示层 |
