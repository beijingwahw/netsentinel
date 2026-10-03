# NetSentinel REST 服务参考(API)

`service/app.py`(A31)提供基于 FastAPI 的本机 REST 服务,把"扫描 → 队列 → 计划预览"流程暴露为 HTTP 接口,便于与运营者自己的工具链集成。接口契约以 `CONTRACTS-V2.md` §3 A31 条目与 `service/app.py` 实际实现为准。

**两条安全边界,先读:**

1. **仅本机使用**。服务默认绑定 `service_host: 127.0.0.1`、`service_port: 8765`,且**没有任何鉴权**。不要绑定到非回环地址、不要做端口转发放到公网;远程访问请自行套 SSH 隧道等受信通道。
2. **`/plan` 只生成计划,绝不提交**。全部端点都不驱动浏览器、不访问 www.12377.cn / www.shdf.gov.cn、不代填验证码。真实提交仍必须走 CLI `netsentinel submit`(经人工门 HUMAN_GATE),服务端不提供提交入口。

相关文档:[USAGE.md](USAGE.md)(CLI 参考)、[DEPLOY.md](DEPLOY.md)(部署与 systemd / 计划任务)、[ETHICS.md](ETHICS.md)(红线)。

---

## 1. 安装与启动

```bash
# 1) 安装(含 api 可选依赖:fastapi + uvicorn)
python -m pip install -e ".[api]"

# 2) 在项目根目录启动(读取默认 ./config.yaml,不存在则用安全默认值)
python -m service.app
```

启动后 uvicorn 监听 `cfg.service_host:cfg.service_port`(默认 `http://127.0.0.1:8765`)。服务通过 `create_app(cfg)` 工厂构建,配置键:

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
| POST | `/scan` | `{"url": "https://..."}` | `{"job_id": "...", "status": "running"}` | `422` 请求体缺 `url` / 非 http(s) |
| GET | `/jobs/{job_id}` | —— | 任务状态 + 站点报告 + 队列 id | `404` 任务不存在 |
| GET | `/queue` | ——(可用查询参数过滤,以实现为准) | 队列条目列表 | —— |
| POST | `/queue/{id}/approve` | 可选 `{"note": "..."}` | 更新后的条目 | `404` 条目不存在;`400` 状态迁移非法 |
| POST | `/queue/{id}/reject` | 可选 `{"note": "驳回理由"}` | 更新后的条目 | `404` / `400` 同上 |
| POST | `/plan/{id}` | `{"portal": "12377" \| "shdf"}` | `{"plan": {...}, "playbook": "# markdown..."}` | `404` 条目不存在;`400` 条目未批准;`422` portal 非法 |

> 状态机与 CLI 完全一致:`pending → approved / rejected → submitted`。`approve / reject` 只对 `pending` 条目有效(批准前请先人工查看证据包,见 [ETHICS.md](ETHICS.md) 第三节);`/plan` 只对 `approved` 条目有意义。

---

## 3. 端点详解与 JSON 示例

### 3.1 POST /scan —— 提交扫描任务

后台线程执行 `orchestrator.run_scan(url, cfg)`(链接发现 → 逐页抽样 → 识别 → 判定;非 clean 自动生成证据包并入审核队列)。立即返回任务号,不阻塞。

请求:

```json
{ "url": "https://待核查站点.example/" }
```

响应 `202`:

```json
{
  "job_id": "20260101T120001_ab12cd",
  "status": "running",
  "url": "https://待核查站点.example/"
}
```

### 3.2 GET /jobs/{job_id} —— 查询任务状态

运行中:

```json
{ "job_id": "20260101T120001_ab12cd", "status": "running" }
```

完成(判定非 clean、已入队):

```json
{
  "job_id": "20260101T120001_ab12cd",
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

说明:`report` 即 `SiteReport.as_dict()`;`intel` 为 V2 融合特征(未开启融合 / 无特征时该键缺省)。失败任务返回 `"status": "failed"` 与中文 `error`。

### 3.3 GET /queue —— 审核队列

响应 `200`(字段与 `ReviewQueue.Entry` 一致):

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
  ]
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

`400` 示例(条目已非 pending,状态机不允许重复迁移):

```json
{ "detail": "条目 3 当前状态为 approved,仅 pending 可批准" }
```

### 3.5 POST /queue/{id}/reject —— 驳回

请求 `{"note": "复核为误报:健身内容,人工判断非色情"}`,响应结构同 3.4(`status` 变为 `rejected`,note 留档)。

### 3.6 POST /plan/{id} —— 生成举报计划预览(不执行提交)

请求条目必须已 `approved`。响应 `200`:

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
      { "action": "screenshot", "label": "截图:填写完成" },
      { "action": "human_gate", "label": "人工核对信息、上传证据包 zip 并输入验证码", "selector": "#report-captcha" },
      { "action": "click",   "label": "点击提交", "selector": "#report-submit" },
      { "action": "wait",    "label": "等待提交结果", "value": "2" },
      { "action": "screenshot", "label": "截图:提交结果" }
    ]
  },
  "playbook": "# 举报计划(12377)……\n\n1. 打开举报入口 ……\n9. 【人工门】核对信息、上传证据包、输入验证码 ……"
}
```

要点:

- `plan` 为 `SubmissionPlan` 的序列化形式,步骤序列固定(与 CLI `submit --dry-run` 看到的完全一致),其中 `human_gate` 步骤即人工门——验证码与最终提交永远留给人工;
- `playbook` 是同一计划的 markdown 剧本(computer-use / 人工驱动用);
- **本端点不打开浏览器、不提交任何内容**。真实提交:`netsentinel submit --id 3 --portal 12377 --exec`(在人工门完成核对 / 上传 / 验证码)。

### 3.7 错误码约定

| 状态码 | 含义 |
| --- | --- |
| `200 / 202` | 成功(异步任务受理用 202) |
| `400` | 业务状态不允许(如对非 pending 条目 approve、对未批准条目要计划) |
| `404` | 任务号 / 队列条目不存在 |
| `422` | 请求体验证失败(FastAPI 校验:缺 `url`、`portal` 取值非法等) |
| `5xx` | 服务端内部错误(详情见服务进程日志) |

---

## 4. curl 示例

以下在 Git Bash / Linux shell 下可直接执行;Windows cmd 请把单引号换成双引号并转义。默认地址 `http://127.0.0.1:8765`。

```bash
# 1) 提交扫描任务(异步,返回 job_id)
curl -X POST http://127.0.0.1:8765/scan \
     -H "Content-Type: application/json" \
     -d '{"url": "https://待核查站点.example/"}'

# 2) 查询任务状态与站点报告
curl http://127.0.0.1:8765/jobs/20260101T120001_ab12cd

# 3) 查看待复核队列
curl http://127.0.0.1:8765/queue

# 4) 人工复核证据包之后批准条目 3
curl -X POST http://127.0.0.1:8765/queue/3/approve \
     -H "Content-Type: application/json" \
     -d '{"note": "已人工核对证据包"}'

# 5) 驳回误报条目 4
curl -X POST http://127.0.0.1:8765/queue/4/reject \
     -H "Content-Type: application/json" \
     -d '{"note": "复核为误报:健身内容,人工判断非色情"}'

# 6) 生成 12377 举报计划预览(只生成,不提交)
curl -X POST http://127.0.0.1:8765/plan/3 \
     -H "Content-Type: application/json" \
     -d '{"portal": "12377"}'
```

典型串联(扫描 → 轮询 → 复核 → 预览):

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8765/scan \
        -H "Content-Type: application/json" \
        -d '{"url": "https://待核查站点.example/"}' | python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s "http://127.0.0.1:8765/jobs/$JOB"          # 轮询到 status=done
curl -s -X POST http://127.0.0.1:8765/queue/3/approve -H "Content-Type: application/json" -d '{}'
curl -s -X POST http://127.0.0.1:8765/plan/3 -H "Content-Type: application/json" -d '{"portal": "shdf"}'
```

> 再次强调:批准(approve)意味着你已人工核实证据真实有效;对每一条举报的真实性由操作者本人负责([ETHICS.md](ETHICS.md))。服务端不提供也不应提供"真实提交"端点。
