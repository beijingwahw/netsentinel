# NetSentinel REST 服务参考(API)

`service/app.py`(A31)提供基于 FastAPI 的本机 REST 服务,把"扫描 → 队列 → 计划预览"流程暴露为 HTTP 接口,便于与运营者自己的工具链集成。接口契约以 `CONTRACTS-V2.md` §3 A31 条目与 `service/app.py` 实际实现为准。

**两条安全边界,先读:**

1. **仅本机使用**。服务默认绑定 `service_host: 127.0.0.1`、`service_port: 8765`,且**没有任何鉴权**。不要绑定到非回环地址、不要做端口转发放到公网;远程访问请自行套 SSH 隧道等受信通道。
2. **`/plan` 只生成计划,绝不提交**。全部端点都不驱动浏览器、不访问 www.12377.cn / www.shdf.gov.cn、不代填验证码。真实提交仍必须走 CLI `netsentinel submit`(经人工门 HUMAN_GATE),服务端不提供提交入口。
3. **发现/线索端点(V10.26)只在后台无头运行**。`POST /discover` 强制无头(绝不弹窗);`POST /captcha/session` 是唯一会打开可见浏览器窗口的端点,且只应由用户在净网助手页面**显式点击**触发——检测到验证码一律不绕过,人工窗口内完成(人工门)。

相关文档:[USAGE.md](USAGE.md)(CLI 参考)、[DEPLOY.md](DEPLOY.md)(部署与 systemd / 计划任务)、[ETHICS.md](ETHICS.md)(红线)。

---

## 1. 安装与启动

```bash
# 1) 安装(含 api 可选依赖:fastapi + uvicorn)
python -m pip install -e ".[api]"

# 2) 在项目根目录启动(读取默认 ./config.yaml,不存在则用安全默认值)
python -m service.app                 # 等价于下方带显式参数的写法
python -m service.app --host 127.0.0.1 --port 8765   # 显式覆盖(缺省取配置)
python -m service.app --help          # 打印用法后退出 0(不起服务、不占端口)
```

启动后 uvicorn 监听 `cfg.service_host:cfg.service_port`(默认 `http://127.0.0.1:8765`);`--host` / `--port` 为一次性显式覆盖(优先于 config.yaml),缺省完全走配置。服务通过 `create_app(cfg)` 工厂构建,配置键:

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `service_host` | `127.0.0.1` | 监听地址;保持回环地址 |
| `service_port` | `8765` | 监听端口 |
| (其余键) | —— | 与 CLI 共用同一份 `config.yaml`(`db_path`、`data_dir`、阈值、VLM 开关等) |

注意:扫描任务在服务进程的**后台线程**中运行,任务状态与进程内存同生命周期(重启即丢,`data/review_queue.db` 等落盘数据不受影响);证据包、队列等仍全部落在配置的本地路径下。

自检(无 FastAPI 时):

```bash
python -m pytest tests/test_service_api.py -q   # 离线 TestClient 冒烟
```

---

## 2. 端点总表

| 方法 | 路径 | 请求体 | 成功响应(概要) | 主要错误码 |
| --- | --- | --- | --- | --- |
| POST | `/scan` | `{"url": "https://..."}` | `202` `{"job_id": "...", "status": "running"}`(trace 开启时附 `X-Trace-Id` 响应头) | `422` 请求体缺 `url` / 非 http(s) |
| GET | `/jobs/{job_id}` | —— | 任务状态 + 站点报告 + 队列 id | `404` 任务不存在 |
| GET | `/trace/{job_id}` | —— | 该任务的追踪 span 树(嵌套 JSON,只读) | `404` 任务不存在 |
| GET | `/queue` | 可选查询串 `?limit=&offset=` | 队列条目列表与统计;带任一分页参数时附 `total` | `422` limit/offset 非法(中文) |
| POST | `/queue/{id}/approve` | 可选 `{"note": "..."}` | 更新后的条目 | `404` 条目不存在;`409` 状态迁移非法 |
| POST | `/queue/{id}/reject` | 可选 `{"note": "驳回理由"}` | 更新后的条目 | `404` / `409` 同上 |
| POST | `/plan/{id}` | `{"portal": "12377" \| "shdf"}` | `{"plan": {...}, "playbook_md": "# markdown...", "notice": "..."}` | `404` 条目不存在;`409` 条目未批准;`422` portal 非法 |
| GET | `/health` | —— | `{"ok": true, "version": "..."}` | —— |
| GET | `/model/active` | —— | 当前活动模型(spec/来源/时间) | `503` 模块未就位;`500` 状态文件异常 |
| POST | `/model/switch` | `{"spec": "ollama:llava"}` | `{"ok": true, "status": {...}}`(switched_by 恒为 "rest") | `422` spec 缺失;`409` spec 非法或连通性测试不通过;`503`;`500` |
| POST | `/model/probe` | —— | `{"local": [本机回环视觉服务探测结果]}`(红线 32:仅 127.0.0.1 端口表) | `503`;`500` |
| POST | `/model/test` | `{"spec": "glm:glm-4.5v"}` | 连通性测试结果(A147 透传:`ok/latency_ms/model/error?`) | `422`;`503`;`500` |
| POST | `/discover` | 可选 `{"keywords": ["词", ...], "engine": "yandex-html"}` | `202` `{"job_id": "...", "status": "queued"}`(后台**无头**整轮发现,线索落 `<data>/leads.txt`;`/jobs` 轮询,done 的 result 含 `urls/queries/errors/out`) | `422` keywords 缺失/类型不符/为空;engine 非法 |
| GET | `/leads` | —— | `{"leads": [{"url", "line"}], "count": n, "mtime": "iso"\|null}`(读 `<data>/leads.txt`,跳 `#` 注释行;只读) | —— |
| POST | `/scan-lead` | `{"url": "https://...", "line": 7(可选)}` | `202` `{"job_id": "...", "status": "running"}`(与 `/scan` 同机制;`line` 为 `GET /leads` 的行号,随任务留痕) | `422` url 缺失/非 http(s);line 非法 |
| POST | `/captcha/session` | —— | `202` `{"job_id": "...", "status": "queued"}`(**用户显式点击才触发**;后台 subprocess 有头跑一次发现 CLI,弹出可见验证窗口;result 含 `returncode/stdout_tail`,0=验证完成) | —— |
| GET | `/assistant/status` | —— | 工作台状态总览(`glm_key/yandex_profile/keywords_count/queue/config`;全本地只读零外呼) | —— |

> 状态机与 CLI 完全一致:`pending → approved / rejected → submitted`。`approve / reject` 只对 `pending` 条目有效(批准前请先人工查看证据包,见 [ETHICS.md](ETHICS.md) 第三节);`/plan` 只对 `approved` 条目有意义。`/model/*` 四端点由 `netsentinel/service/model_api.py` 提供(V8 挂载;缺席只告警不阻断),密钥只显示布尔,响应绝不包含密钥本体(红线 33)。

---

## 3. 端点详解与 JSON 示例

### 3.1 POST /scan —— 提交扫描任务

后台线程池执行 `orchestrator.run_scan(url, cfg)`(链接发现 → 逐页抽样 → 识别 → 判定;非 clean 自动生成证据包并入审核队列)。立即返回任务号,不阻塞。

请求:

```json
{ "url": "https://待核查站点.example/" }
```

响应 `202`(`job_id` 为 uuid4 十六进制串):

```json
{
  "job_id": "9f1c2ab34de56f7890abcdef12345678",
  "status": "running",
  "url": "https://待核查站点.example/"
}
```

**X-Trace-Id(V12 全链追踪)**:追踪总开关默认开启(`service/app.py::TRACE_ENABLED`,或 `create_app(..., trace_enabled=...)` 注入;配置 `trace_enabled: false` 关闭后行为与接线前完全一致——不开 trace、不加响应头)。开启时 `/scan` 响应携带 `X-Trace-Id` 响应头,后台扫描线程首步即接回同一条 trace 树;该 trace_id 可用于 `GET /trace/{job_id}` 查询 span 树。追踪只存本机内存的名称/时间元数据,不落 URL 内容与密钥。

### 3.2 GET /jobs/{job_id} —— 查询任务状态

运行中:

```json
{ "job_id": "9f1c2ab34de56f7890abcdef12345678", "status": "running" }
```

完成(判定非 clean、已入队):

```json
{
  "job_id": "9f1c2ab34de56f7890abcdef12345678",
  "status": "done",
  "queue_id": 3,
  "report": {
    "site_url": "https://待核查站点.example/",
    "pages": [
      {
        "url": "https://待核查站点.example/",
        "screenshot_path": "data/evidence/example_20260101_120001/page_1.png",
        "images": ["data/evidence/example_20260101_120001/img_001.png"],
        "text_hint_hits": ["示例命中词"]
      }
    ],
    "image_scores": [
      {
        "image": "data/evidence/example_20260101_120001/img_001.png",
        "model": "ensemble",
        "nsfw_prob": 0.9312,
        "scores": { "members": { "stub": 0.97, "glm": 0.9 } }
      }
    ],
    "agg_nsw_prob": 0.9312,
    "nsw_image_count": 4,
    "verdict": "nsfw",
    "needs_review": true,
    "created_at": "2026-01-01T12:00:31+08:00",
    "intel": {
      "url":  { "risk": 0.12, "features": {}, "explain": ["顶级域属可疑高风险后缀(xyz/top/tk 等)"] },
      "text": { "risk": 0.0,  "features": {}, "explain": [] },
      "page_vlm": { "page_nsfw_prob": 0.83, "elements": [], "model": "glm-5.3-flash" },
      "fusion": {
        "prob": 0.9417,
        "contrib": { "image": 2.0484, "page_vlm": 1.2960, "url": 0.0420, "text": 0.0 },
        "rule": "只升不降:辅助特征仅加强复核,不降低图像判定"
      }
    }
  }
}
```

说明:`report` 即 `SiteReport.as_dict()`;`intel` 为 V2 融合特征(未开启融合 / 无特征时该键缺省)。失败任务返回 `"status": "failed"` 与中文 `error`。任务状态存进程内存(重启即丢,落盘数据不受影响)。

### 3.2b GET /trace/{job_id} —— 查询任务的追踪 span 树(只读)

V12 全链追踪端点:按任务登记的 `trace_id` 导出嵌套 span 树(`telemetry_trace.export_trace_json`)。只读——本端点自身不开 span、不建 trace。

- 任务不存在 → `404`;
- 任务在追踪关闭期间创建(无 `trace_id`)→ `200` 空树:

```json
{ "trace_id": null, "started_ts": null, "spans": [] }
```

- 有 trace 的任务 → 嵌套 span 树(名称、起止时间、子 span),示例(节选):

```json
{
  "trace_id": "0f2f4a6b-...",
  "started_ts": "2026-10-13T12:00:01+08:00",
  "spans": [
    { "name": "scan.submit", "attrs": {"stage": "submit"}, "children": [
      { "name": "scan.run", "children": [
        { "name": "crawl", "children": [] },
        { "name": "classify", "children": [] }
      ]}
    ]}
  ]
}
```

**有界性说明(V10.21 核查)**:本端点始终只导出单个任务(单条 trace)的 span 树,而 `telemetry_trace` 对每条 trace 设有 `MAX_SPANS_PER_TRACE`(4096)硬上限(超出即丢弃并计数),进程内注册表另有 `MAX_TRACES`(64)逐出上限——单任务响应规模天然有界,无需分页参数。

### 3.3 GET /queue —— 审核队列(可选分页)

**缺省全量返回**(不带参数时与历史响应逐字节一致,含 `summary` 全队列统计):

```json
{
  "entries": [
    {
      "id": 3,
      "site_url": "https://待核查站点.example/",
      "verdict": "nsfw",
      "status": "pending",
      "evidence_zip": "data/evidence/example_20260101_120001.zip",
      "created_at": "2026-01-01T12:00:31+08:00",
      "updated_at": "2026-01-01T12:00:31+08:00",
      "note": ""
    }
  ],
  "summary": { "pending": 1, "approved": 0, "rejected": 0, "submitted": 0 }
}
```

**可选分页(V10.20)**:`?limit=20&offset=40`——`limit` 须为 ≥1 的整数、`offset` 须为 ≥0 的整数;任一参数在场即进入分页语义,`entries` 取按 id 升序全量列表的 `[offset : offset+limit]` 切片,响应附加 `total`(全量条数,供客户端计算页数;`summary` 仍为全队列统计,分页只切条目不切统计)。`offset` 越界(超过 total)返回空 `entries` + `200`,不报错。非法值(非整数、`limit<1`、`offset<0`)返回 `422` 中文错误。

```
GET /queue?limit=2&offset=2
→ 200
{
  "entries": [ { "id": 5, ... }, { "id": 6, ... } ],
  "summary": { "pending": 7, "approved": 0, "rejected": 0, "submitted": 0 },
  "total": 7
}
```

### 3.4 POST /queue/{id}/approve —— 批准(人工复核后)

响应 `200`:

```json
{
  "id": 3,
  "site_url": "https://待核查站点.example/",
  "verdict": "nsfw",
  "status": "approved",
  "evidence_zip": "data/evidence/example_20260101_120001.zip",
  "created_at": "2026-01-01T12:00:31+08:00",
  "updated_at": "2026-01-01T13:02:10+08:00",
  "note": "已人工核对证据包"
}
```

`409` 示例(条目已非 pending,状态机不允许重复迁移):

```json
{ "detail": "条目 3 当前状态为 approved,仅 pending 可批准" }
```

### 3.5 POST /queue/{id}/reject —— 驳回

请求 `{"note": "复核为误报:健身内容,人工判断非色情"}`,响应结构同 3.4(`status` 变为 `rejected`,note 留档)。

### 3.6 POST /plan/{id} —— 生成举报计划预览(不执行提交)

请求条目必须已 `approved`。响应 `200`(`plan` 为 `SubmissionPlan` 序列化;`playbook_md` 为同一计划的 markdown 剧本;`notice` 为人工门提示):

```json
{
  "plan": {
    "portal": "12377",
    "entry_url": "https://www.12377.cn",
    "payload": {
      "portal": "12377",
      "site_url": "https://待核查站点.example/",
      "category": "色情低俗信息",
      "description": "……(中文描述模板,声明'辅助系统初筛+人工核实')",
      "evidence_zip": "data/evidence/example_20260101_120001.zip",
      "reporter_name": "",
      "reporter_phone": ""
    },
    "steps": [
      { "action": "goto",    "label": "打开举报入口", "selector": "", "value": "https://www.12377.cn" },
      { "action": "wait",    "label": "等待页面加载", "value": "1" },
      { "action": "select",  "label": "选择信息类型", "selector": "#report-type", "value": "色情低俗信息" },
      { "action": "fill",    "label": "填写举报链接", "selector": "#report-url",  "value": "https://待核查站点.example/" },
      { "action": "fill",    "label": "填写具体描述", "selector": "#report-desc", "value": "……" },
      { "action": "fill",    "label": "填写举报人姓名", "selector": "#report-name",  "value": "" },
      { "action": "fill",    "label": "填写举报人电话", "selector": "#report-phone", "value": "" },
      { "action": "fill",    "label": "填写电子邮箱", "selector": "#report-email", "value": "" },
      { "action": "fill",    "label": "填写身份证号", "selector": "#report-idcard", "value": "" },
      { "action": "fill",    "label": "填写通讯地址", "selector": "#report-address", "value": "" },
      { "action": "fill",    "label": "填写邮政编码", "selector": "#report-postcode", "value": "" },
      { "action": "fill",    "label": "填写单位名称", "selector": "#report-org", "value": "" },
      { "action": "screenshot", "label": "填写完成后截图" },
      { "action": "focus",     "label": "聚焦验证码输入框(半自动)", "selector": "#report-captcha" },
      { "action": "human_gate", "label": "请在浏览器输入验证码,然后回到终端按回车提交", "selector": "#report-captcha" },
      { "action": "click",   "label": "点击提交", "selector": "#report-submit" },
      { "action": "wait",    "label": "等待提交处理", "value": "2" },
      { "action": "screenshot", "label": "提交结果截图" }
    ]
  },
  "playbook_md": "# 举报计划(12377)……\\n\n1. 打开举报入口 ……\n15. 【人工门】核对附件、输入验证码 ……",
  "notice": "本服务不执行提交;请用 CLI 在人工门下执行"
}
```

要点:

- `plan.steps` 为 18 步固定序列(与 CLI `submit --dry-run` 看到的完全一致;个人信息 6 字段空值 `skippable`);`human_gate` 步骤即人工门——验证码与最终提交永远留给人工;
- 真实执行时,执行器会在 `human_gate` 之前自动上传证据截图(Top-10 池,按门户配额:12377 十张 / shdf 五张;V10.16);
- `playbook_md` 是同一计划的 markdown 剧本(computer-use / 人工驱动用);
- **本端点不打开浏览器、不提交任何内容**。真实提交:`netsentinel submit --id 3 --portal 12377 --exec`(在人工门完成核对 / 验证码)。

### 3.7 GET /health —— 健康检查

响应 `200`:

```json
{ "ok": true, "version": "…" }
```

### 3.8 /model/* —— 视觉模型管理四端点(V8)

由 `netsentinel/service/model_api.py::create_model_router(cfg)` 提供,前缀 `/model`,随 `create_app` 自动挂载(模块缺席只告警不阻断)。`switched_by` 来源留痕:REST 切换恒记 `"rest"`(与 takeover/wizard/cli 区分)。

**GET /model/active** —— 当前活动模型:

```json
{ "spec": "glm:glm-4.5v", "switched_by": "wizard", "switched_at": "2026-10-02T10:00:00" }
```

**POST /model/switch** —— 请求 `{"spec": "ollama:llava"}`;默认先做连通性测试,不通过 → `409` 中文原因;成功 → `{"ok": true, "status": {...}}`,活动模型持久化到 `model_runtime_path`(下一条扫描生效)。

**POST /model/probe** —— 探测本机回环视觉服务(`cfg.local_probe_ports` 端口表,仅 127.0.0.1,红线 32):

```json
{ "local": [ { "port": 11434, "ok": true, "models": ["llava"] } ] }
```

**POST /model/test** —— 请求 `{"spec": "glm:glm-4.5v"}`,响应连通性测试结果(A147 透传):

```json
{ "ok": true, "latency_ms": 5123, "model": "glm-4.5v" }
```

> 密钥只显示布尔,任何响应绝不包含密钥本体(红线 33);云端测试受 `vlm_online` + 密钥双条件与 VLM 预算(红线 16/19)约束。

### 3.9 净网助手统一工作台五端点(V10.26)

支撑"所有操作后台进行,只看净网助手页面":发现/线索/扫描全部后台化,`POST /captcha/session` 是**唯一**会弹可见浏览器窗口的端点(且只应由用户显式点击触发)。五个端点复用既有 jobs 字典/锁/线程池机制(受理即 `queued`,worker 起步置 `running`,终态 `done/failed` 与 `/scan` 同语义,经 `GET /jobs/{job_id}` 轮询)。

**POST /discover —— 提交线索发现(后台无头执行)**

请求体(两字段均可选):

```json
{ "keywords": ["词A", "词B"], "engine": "yandex-html" }
```

- `keywords` 缺省读关键词活文件 `<data>/keywords/active.txt`(按行解析同关键词页口径:strip、跳空行/`#` 注释、去重保序;红线 28:代码零内置词表,活文件缺失或解析为空 → `422` 中文);
- `engine` 缺省取配置 `discovery_engine`;
- 后台线程**强制无头**:worker 体内移除 `NETSENTINEL_YANDEX_HTML_HEADED`(即使服务进程环境设有它),并把 `NETSENTINEL_DATA_DIR` 对齐到 `cfg.data_dir` 绝对路径(yandex-html 会话目录随配置走);
- 完成语义:**整轮带 errors 也算 done**——单查询失败(如验证码拦截)属业务结果,`result.errors` 如实上抛;只有 discover()/写盘本身抛异常才 `failed`(中文 `error`,页面据此提示"先完成验证会话")。

响应 `202`:`{ "job_id": "...", "status": "queued" }`;`done` 后 `/jobs` 的 result:

```json
{
  "urls": 5,
  "queries": 2,
  "errors": ["词A —— yandex-html 检测到验证码,无头模式下已停止"],
  "out": "D:\\...\\data\\leads.txt"
}
```

**GET /leads —— 读线索清单(只读)**

读 `<data>/leads.txt`(发现轮与验证码会话的统一落点):跳 `#` 注释行,`line` 为文件内 1 起始的**真实行号**(注释行占号);文件缺失 → 空表(`mtime: null`);绝不写盘/建目录。

```json
{
  "leads": [ { "url": "http://a.example/1", "line": 3 } ],
  "count": 1,
  "mtime": "2026-10-13T12:00:31.123456"
}
```

**POST /scan-lead —— 按线索提交扫描(与 /scan 同机制)**

请求 `{"url": "http://a.example/1", "line": 3}`(`line` 可选,须为 ≥1 整数,来自 `GET /leads`,仅随任务留痕)。受理/线程池/trace/入列与 `POST /scan` 完全同机制:响应 `202` `{"job_id": "...", "status": "running"}`(trace 开启时同样附 `X-Trace-Id`),`done` 后 result 与 `/scan` 同构。既有 `POST /scan` 保持不动(兼容)。

**POST /captcha/session —— 一次性可见验证会话(用户显式点击才触发)**

后台以 **subprocess** 有头跑一次发现 CLI(人工门:真人窗口内完成验证码,机器只等待):

```
[sys.executable, "-m", "netsentinel.discovery",
 "--keywords-file", "<data>/keywords/active.txt",
 "--engine", "yandex-html",
 "--out", "<data>/leads.txt"]
```

子进程 env **显式传入**(父进程环境不动,后台任务的无头承诺不受影响):`NETSENTINEL_YANDEX_HTML_HEADED=1` + `NETSENTINEL_DATA_DIR=<cfg.data_dir 绝对化>`。验证通过的会话 cookie 落 `<data>/browser_profiles/yandex_html`,后续后台无头发现复用。超时上限 30 分钟(`service/app.py::CAPTCHA_SESSION_TIMEOUT_S`)。响应 `202` `{"job_id", "status": "queued"}`;终态:

- `returncode 0` → `done`,result:`{ "returncode": 0, "stdout_tail": "发现完成:..." }`(0 = 验证完成);
- 非 0 → `failed`(中文 `error` 附退出码,result 仍随附 `stdout_tail` 供页面展示);
- 启动失败/超时 → `failed`(中文 `error`,无 result)。

**GET /assistant/status —— 工作台状态总览(全本地只读,零外呼)**

五路本地探测拼装首屏状态,**不发起任何网络/模型请求**:

```json
{
  "glm_key": true,
  "yandex_profile": true,
  "keywords_count": 2,
  "queue": { "pending": 2, "approved": 1, "rejected": 0, "submitted": 0 },
  "config": { "vlm_online": false, "discovery_online": true, "classifier": "stub" }
}
```

- `glm_key`:`~/.netsentinel/keys/glm` 密钥文件存在性(只查文件,不读内容、零外呼);
- `yandex_profile`:`<data>/browser_profiles/yandex_html` 会话目录存在性;
- `keywords_count`:关键词活文件有效词数(坏文件 → 0);
- `queue`:与 `GET /queue` 的 `summary` 同款四状态计数——但走 sqlite **只读连接**,库缺席 → 全 0,**绝不误建空库**(零副作用);
- `config`:`vlm_online` / `discovery_online` / `classifier` 三项配置原样透传。

### 3.10 错误码约定

| 状态码 | 含义 |
| --- | --- |
| `200 / 202` | 成功(异步任务受理用 202) |
| `409` | 业务状态不允许:队列状态迁移非法(对非 pending 条目 approve/reject)、对未批准条目要计划;`/model/switch` spec 非法或连通性测试不通过 |
| `404` | 任务号 / 队列条目不存在 |
| `422` | 请求体验证失败(FastAPI 校验:缺 `url`/`spec`、portal 取值非法、/queue 分页参数非法、/discover 的 keywords/engine 非法、/scan-lead 的 line 非法等,中文) |
| `503` | `/model/*`:依赖的兄弟模块未就位(RuntimeError,中文) |
| `5xx` | 服务端内部错误(详情见服务进程日志) |

---

## 4. curl 示例

以下在 Git Bash / Linux shell 下可直接执行;Windows cmd 请把单引号换成双引号并转义。默认地址 `http://127.0.0.1:8765`。

```bash
# 1) 提交扫描任务(异步,返回 job_id;响应头含 X-Trace-Id)
curl -i -X POST http://127.0.0.1:8765/scan \
     -H "Content-Type: application/json" \
     -d '{"url": "https://待核查站点.example/"}'

# 2) 查询任务状态与站点报告
curl http://127.0.0.1:8765/jobs/9f1c2ab34de56f7890abcdef12345678

# 3) 查询该任务的追踪 span 树(只读)
curl http://127.0.0.1:8765/trace/9f1c2ab34de56f7890abcdef12345678

# 4) 查看待复核队列(缺省全量;可加 ?limit=20&offset=40 分页,响应附 total)
curl http://127.0.0.1:8765/queue
curl "http://127.0.0.1:8765/queue?limit=20&offset=40"

# 5) 人工复核证据包之后批准条目 3
curl -X POST http://127.0.0.1:8765/queue/3/approve \
     -H "Content-Type: application/json" \
     -d '{"note": "已人工核对证据包"}'

# 6) 驳回误报条目 4
curl -X POST http://127.0.0.1:8765/queue/4/reject \
     -H "Content-Type: application/json" \
     -d '{"note": "复核为误报:健身内容,人工判断非色情"}'

# 7) 生成 12377 举报计划预览(只生成,不提交)
curl -X POST http://127.0.0.1:8765/plan/3 \
     -H "Content-Type: application/json" \
     -d '{"portal": "12377"}'

# 8) 视觉模型:查活动模型 / 切换 / 本机探测 / 连通性测试
curl http://127.0.0.1:8765/model/active
curl -X POST http://127.0.0.1:8765/model/switch \
     -H "Content-Type: application/json" -d '{"spec": "ollama:llava"}'
curl -X POST http://127.0.0.1:8765/model/probe
curl -X POST http://127.0.0.1:8765/model/test \
     -H "Content-Type: application/json" -d '{"spec": "glm:glm-4.5v"}'

# 9) 净网助手工作台(V10.26):后台发现(缺省读关键词活文件)→ 查线索 → 按线索扫描
curl -X POST http://127.0.0.1:8765/discover \
     -H "Content-Type: application/json" -d '{"engine": "yandex-html"}'
curl http://127.0.0.1:8765/jobs/<discover_job_id>        # 轮询到 status=done
curl http://127.0.0.1:8765/leads                          # 只读线索清单
curl -X POST http://127.0.0.1:8765/scan-lead \
     -H "Content-Type: application/json" -d '{"url": "http://a.example/1", "line": 3}'

# 10) 验证会话(用户显式点击才触发;弹可见窗口,人工完成验证码)与状态总览
curl -X POST http://127.0.0.1:8765/captcha/session
curl http://127.0.0.1:8765/jobs/<captcha_job_id>         # result.returncode 0 = 验证完成
curl http://127.0.0.1:8765/assistant/status               # 全本地只读,零外呼
```

典型串联(扫描 → 轮询 → 复核 → 预览):

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8765/scan \
        -H "Content-Type: application/json" \
        -d '{"url": "https://待核查站点.example/"}' | python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s "http://127.0.0.1:8765/jobs/$JOB"          # 轮询到 status=done
curl -s "http://127.0.0.1:8765/trace/$JOB"          # 追踪 span 树
curl -s -X POST http://127.0.0.1:8765/queue/3/approve -H "Content-Type: application/json" -d '{}'
curl -s -X POST http://127.0.0.1:8765/plan/3 -H "Content-Type: application/json" -d '{"portal": "shdf"}'
```

> 再次强调:批准(approve)意味着你已人工核实证据真实有效;对每一条举报的真实性由操作者本人负责([ETHICS.md](ETHICS.md))。服务端不提供也不应提供"真实提交"端点。
