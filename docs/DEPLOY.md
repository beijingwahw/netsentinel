# NetSentinel 部署手册(DEPLOY)

覆盖:依赖矩阵、目录与数据规划、密钥管理、四种运行形态、开机自启(Windows 计划任务 / Linux systemd)、docker-compose、升级与回滚、监控与审计校验。配置键以 `config.example.yaml` 与 `netsentinel/contracts.py::Config` 为准;GLM 相关背景见 [VLM_GUIDE.md](VLM_GUIDE.md),总览见 [UPGRADE_V2.md](UPGRADE_V2.md)。

> 部署前必读 [ETHICS.md](ETHICS.md)。安全默认:`allow_network=false`、`dry_run_default=true`、`vlm_online=false`——三者在配置里显式开启前,系统不访问外网、不真实提交、不外发图像。

---

## 1. 依赖矩阵

| 组件 | 要求 | 安装 | 不装的后果 |
| --- | --- | --- | --- |
| Python | **3.10+** | —— | 无法运行(代码使用 3.10 语法) |
| PyYAML | ≥6.0(必装,基础依赖) | `python -m pip install -e .` | 无法加载配置,完全不可用 |
| Playwright(chromium) | ≥1.40 | `python -m pip install -e ".[browser]"` 再 `python -m playwright install chromium` | 扫描自动退化为仅抓 HTML、**无整页截图**(page_vlm 随之缺分);`submit --exec` 不可用 |
| NudeNet | ≥3.3 | `python -m pip install -e ".[vision]"` | `nudenet` 分类器不可用(惰性导入,报带安装提示的 ImportError) |
| transformers + torch + Pillow | clip extra | `python -m pip install -e ".[clip]"` | `clip` 分类器不可用;page_vlm 大截图不压缩 |
| Streamlit | ≥1.30 | `python -m pip install -e ".[ui]"` | webui 复核台不可用(CLI 队列操作不受影响) |
| FastAPI + uvicorn | ≥0.110 / ≥0.29 | `python -m pip install -e ".[api]"` | REST 服务不可用 |
| pytest | ≥7.0(dev) | `python -m pip install -e ".[dev]"` | 无法跑测试 |

一条常用全家桶:

```bash
python -m pip install -e ".[browser,vision,ui,api,dev]"
python -m playwright install chromium
```

最小可用(纯离线开发/测试):仅基础依赖 + `stub` 分类器,全部 CLI 可用。chromium 安装位置随平台不同(`~\AppData\Local\ms-playwright\` 或 `~/.cache/ms-playwright/`),容器 / 服务器无头环境建议配合 `playwright install --with-deps chromium` 一并装系统库。

---

## 2. 目录与数据规划

安装布局建议(以 Linux `/opt/netsentinel` 为例,Windows 对应 `C:\1\netsentinel`):

```
<netsentinel 根>/
├── netsentinel/  service/  webui/  benchmarks/  tests/   # 代码
├── config.yaml                  # 运行配置(从 config.example.yaml 复制;勿入库密钥)
├── watchlist.yaml               # 巡查清单(A39;从 watchlist.example.yaml 复制)
└── data/                        # 全部落盘数据(可整体备份/迁移)
    ├── evidence/                #   证据包:<safe_host>_<ts>/ 目录 + 同名 .zip
    ├── review_queue.db          #   审核队列(sqlite)
    ├── audit.jsonl              #   JSONL 审计日志(V2 起含哈希链)
    ├── vlm_cache.db             #   VLM 结果缓存(30 天 TTL;可随时删除重建)
    ├── logs/netsentinel.log     #   运行日志
    └── runs/<ts>/               #   submit 每步执行截图
```

要点:

- **`data/` 是唯一需要备份 / 持久化的目录**(外加 `config.yaml`、`watchlist.yaml`)。容器部署把它挂成卷即可(§6);迁移机器时拷贝整个 `data/` 即完成搬家。
- **备份策略**:sqlite(`review_queue.db`、`vlm_cache.db`)建议停写时复制或用 `sqlite3 .backup`;`audit.jsonl` 追加式,直接增量拷贝;`evidence/` 按需——已 `submitted` 的条目证据建议按你的留存要求归档。
- **清理策略**(对应 ETHICS 第七节"数据合规"):条目被 `reject` 或举报处理结束后,及时删除对应 `evidence/<safe_host>_<ts>/` 目录与 zip;`runs/` 截图同理;`vlm_cache.db` 可直接删除(自动重建,只损失缓存);审计日志按数据最小化原则设定保存期限。
- **容量预估**:证据包体积 ≈ 页面截图 + 下载图片(每站 ≤ `max_pages`×`max_images_per_page` 张、单图 ≤ `max_image_mb`,默认 5×12×8MB 上界,实际远小);`logs/` 建议配 logrotate / 定期裁剪。

---

## 3. 密钥管理(GLM API Key)

密钥三来源,**优先级从高到低**(由 `netsentinel/security/vault.py::get_glm_key(cfg)` 统一解析):

1. `config.yaml` 的 `glm_api_key`(注意:配置文件容易被误提交,不推荐在共享环境使用);
2. 环境变量 `NETSENTINEL_GLM_API_KEY`(推荐:容器、CI、计划任务);
3. 密钥文件 `~/.netsentinel/glm_key`,key 独占一行(推荐:服务器长期运行)。

```bash
mkdir -p ~/.netsentinel && echo "你的key" > ~/.netsentinel/glm_key
chmod 600 ~/.netsentinel/glm_key                       # Linux/macOS
icacls "%USERPROFILE%\.netsentinel\glm_key" /inheritance:r /grant:r "%USERNAME%:F"   # Windows
```

- vault 首次读取密钥文件后会尝试(Windows 下用 icacls)或提示收紧权限——确保该文件仅当前用户可读;
- `vault.redact(obj)` 可对任意 dict / 列表 / 字符串递归打码(形如密钥的值 → `****前4`),用于日志与展示前的脱敏;
- **不要**把带密钥的 `config.yaml` 提交进仓库(`.gitignore` 已排除 `config.yaml`,请保持);
- 密钥只是"能外呼"的必要条件之一,还须 `vlm_online: true` 才真正出网(合规含义见 [VLM_GUIDE.md](VLM_GUIDE.md) §3)。

---

## 4. 四种运行形态

| 形态 | 启动 | 适用 |
| --- | --- | --- |
| **CLI 手动** | `netsentinel scan --url ...` → `queue list / approve` → `submit --id --portal`(完整参考见 [USAGE.md](USAGE.md)) | 单个可疑 URL 的核查与举报;最简部署 |
| **定期巡查(scheduler)** | `python -m netsentinel.ops.scheduler --loop --interval-min 360`(或 `--once` 跑一轮) | 维护 watchlist 清单,自动周期巡查 + 站点记忆去重 + 待复核通知 |
| **Streamlit 复核台** | `streamlit run webui/app.py`(浏览器打开,仅本机) | 人工批量复核:看证据、看 intel 解释、批准 / 驳回、预览举报计划 |
| **REST 服务** | `python -m service.app`(默认 `127.0.0.1:8765`,无鉴权,仅本机) | 与自有工具集成;端点见 [API.md](API.md) |

说明:

- **scheduler(A39)**:读 `watchlist_path`(默认 `watchlist.yaml`,从 `watchlist.example.yaml` 复制;条目为 `url / note / enabled`);每项先问 `SiteMemory`(站点指纹 = 页面 URL 集 + 图片 sha256 集的哈希,默认 72 小时 TTL)是否需要重扫,未变化的跳过;扫描出 `needs_review` 的站点通过 `notify_webhook` 推提醒(钉钉 / 飞书 / 企微 / 通用 JSON);逐项之间带 `间隔基数 + 随机抖动` 的 sleep,避免密集访问。每轮返回 `{"scanned", "skipped", "failed", "notified"}` 摘要。
- **复核台与 REST 服务都没有鉴权**:保持本机回环访问;`webui` 可用环境变量 `NETSENTINEL_DATA_DIR` 指向数据目录(见 `webui/README.md`)。
- 四种形态共用同一份 `config.yaml` 与 `data/`,可并存(注意 sqlite 并发写入是短锁,常规规模无碍)。

`watchlist.yaml` 示例(以 `watchlist.example.yaml` 为准):

```yaml
- url: https://待巡查站点1.example/
  note: 群众反复举报
  enabled: true
- url: https://待巡查站点2.example/
  note: 观察中
  enabled: false
```

---

## 5. 开机自启 / 定期执行

### 5.1 Windows 计划任务(每 6 小时巡查一轮)

```bat
schtasks /Create /TN "NetSentinel巡查" /SC HOURLY /MO 6 ^
  /TR "cmd /c cd /d C:\1\netsentinel && C:\1\netsentinel\.venv\Scripts\python.exe -m netsentinel.ops.scheduler --once >> data\logs\scheduler.log 2>&1" ^
  /ST 08:00 /RU "%USERNAME%"
```

要点:`cd /d` 保证工作目录在项目根(scheduler 与 CLI 一样默认找 `./config.yaml` 与 `./data/`);输出重定向留痕;删除任务 `schtasks /Delete /TN "NetSentinel巡查" /F`。也可用"任务计划程序"图形界面配置(触发器:每天 / 每 N 小时;操作:启动程序如上;勾选"起始于"填项目根目录)。

### 5.2 Linux systemd(scheduler 常驻 + REST 服务)

`/etc/systemd/system/netsentinel-scheduler.service`:

```ini
[Unit]
Description=NetSentinel 巡查调度器(净网哨兵)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=netsentinel
Group=netsentinel
WorkingDirectory=/opt/netsentinel
Environment=NETSENTINEL_GLM_API_KEY=            # 或依赖 ~/.netsentinel/glm_key
ExecStart=/opt/netsentinel/.venv/bin/python -m netsentinel.ops.scheduler --loop --interval-min 360
Restart=on-failure
RestartSec=60
# 加固(可选)
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/netsentinel-api.service`:

```ini
[Unit]
Description=NetSentinel REST 服务(仅本机 127.0.0.1:8765)
After=network-online.target

[Service]
Type=simple
User=netsentinel
WorkingDirectory=/opt/netsentinel
ExecStart=/opt/netsentinel/.venv/bin/python -m service.app
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now netsentinel-scheduler.service netsentinel-api.service
journalctl -u netsentinel-scheduler -f          # 看巡查日志
```

---

## 6. docker-compose 示例

容器内跑浏览器截图需要 chromium 及其系统库,推荐直接用 Playwright 官方镜像或在构建时 `playwright install --with-deps chromium`。

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir -e ".[browser,api]" \
 && python -m playwright install --with-deps chromium
# 应用代码内置,无需密钥文件;key 走环境变量
CMD ["python", "-m", "service.app"]
```

`docker-compose.yml`:

```yaml
services:
  netsentinel-api:
    build: .
    image: netsentinel:local
    container_name: netsentinel-api
    # 仅本机回环访问(无鉴权,绝不映射到 0.0.0.0 公网)
    ports:
      - "127.0.0.1:8765:8765"
    volumes:
      - ./data:/app/data          # 队列 / 证据 / 审计 / VLM 缓存全部持久化
      - ./config.yaml:/app/config.yaml:ro
      - ./watchlist.yaml:/app/watchlist.yaml:ro
    environment:
      - NETSENTINEL_GLM_API_KEY=${NETSENTINEL_GLM_API_KEY}
    restart: unless-stopped

  netsentinel-scheduler:
    image: netsentinel:local
    container_name: netsentinel-scheduler
    command: python -m netsentinel.ops.scheduler --loop --interval-min 360
    volumes:
      - ./data:/app/data          # 与 api 共用同一份数据
      - ./config.yaml:/app/config.yaml:ro
      - ./watchlist.yaml:/app/watchlist.yaml:ro
    environment:
      - NETSENTINEL_GLM_API_KEY=${NETSENTINEL_GLM_API_KEY}
    restart: unless-stopped
```

```bash
export NETSENTINEL_GLM_API_KEY="你的key"      # 只放宿主机环境 / .env(勿提交)
docker compose up -d --build
```

注意:两个容器共用 `./data` 卷时,**sqlite 文件不要同时被高强度写入**(常规巡查频率无碍);chromium 相关系统依赖必须在镜像内安装,否则扫描退化为无截图模式;`vlm_online` 若开启,请确认容器出网策略允许访问 `open.bigmodel.cn`。

---

## 7. 升级与回滚

版本事实源:`pyproject.toml` 的 `version`(当前 `0.1.0`)。惯例做法:

```bash
# 升级前:备份数据 + 记录当前版本
cp -a data data.bak-$(date +%Y%m%d)
python -m pip show netsentinel | grep Version

# 升级(拉取新代码 / 新 wheel 后重装 extras)
git pull            # 或替换发行包
python -m pip install -e ".[browser,vision,ui,api,dev]"

# 回滚:切回旧版本代码 / 旧 wheel 重装,再还原数据
git checkout <旧版本tag或commit>
python -m pip install -e ".[browser,vision,ui,api,dev]"
rm -rf data && cp -a data.bak-YYYYMMDD data
```

- 数据格式兼容性:队列 / 缓存均为 sqlite,契约字段向后兼容(`SiteReport.intel` 在 V2 为新增字段,`as_dict()` 仅在非空时输出,v1 数据可继续读取);跨大版本升级后如遇库结构不兼容,删 `vlm_cache.db` 重建即可(只损失缓存),队列库异常时从备份恢复;
- 升级后自检:`python -m pytest -q`(全绿,允许 skip);再用 stub 跑一遍离线冒烟:

```bash
netsentinel scan --url http://127.0.0.1:8000/    # 本地 demo 站(allow_network=false 放行回环)
```

- 提示词版本升级(`vlm_prompts.PROMPT_VERSION` 变化)会使旧 VLM 缓存自动失效,属预期行为(见 [VLM_GUIDE.md](VLM_GUIDE.md) §5)。

---

## 8. 监控:日志位置与审计哈希链

### 8.1 日志位置

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 运行日志 | `data/logs/netsentinel.log`(CLI/服务共用) | 扫描 / 判定 / GLM 回退 / 仲裁 / 队列操作 |
| 审计日志 | `data/audit.jsonl` | JSONL 追加,每行自动带时间戳;V2 起由 `AuditChain` 写入哈希链字段 |
| systemd | `journalctl -u netsentinel-*` | 容器/服务的标准输出 |
| 值得盯的关键行 | —— | `GLM 模型 X 不可用(...按 glm_models_fallback 更换模型重试)`(回退发生)、`当日 VLM 调用预算已用尽`(预算触顶)、`needs_review`(新待复核条目) |

### 8.2 审计哈希链校验(`vault.AuditChain`)

V2 起 `AuditChain.wrap(event, **fields)` 给每条审计记录追加 `prev_hash` / `entry_hash`(`entry_hash = sha256(prev_hash + json(本行))`),形成 tamper-evident 链。校验:

```bash
python -c "from netsentinel.security.vault import AuditChain; print(AuditChain.verify('data/audit.jsonl'))"
```

返回 `(bool, 中文说明)`:整链完整返回 `(True, ...)`;任何一行被篡改 / 删除返回 `(False, 指明首处断链的行号与原因)`;v1 时期的旧行没有哈希字段,会跳过并在说明里报告"旧格式 N 行"。建议纳入例行巡检(如每日计划任务执行一次并把结果写入日志);校验失败说明审计留痕被动过,应立即排查。

### 8.3 预算与缓存观测

```bash
python - <<'PY'
from netsentinel.contracts import Config
from netsentinel.vision.vlm_cache import VlmCache
cfg = Config()
cache = VlmCache(cfg.vlm_cache_db, cfg.vlm_daily_budget)
print(cache.budget_state())   # {'used': n, 'limit': 200}
print(cache.stats())          # 缓存条数 / 命中等统计
PY
```

---

## 9. 上线前核对清单

- [ ] `python -m pytest -q` 全绿(允许 skip);
- [ ] `config.yaml` 按需开启:`allow_network`(抓取外网)、`vlm_online`(图像出境,见 VLM_GUIDE §3);`dry_run_default` 保持 `true`,首次真实提交用 `--exec` 显式触发;
- [ ] 密钥走环境变量或 `~/.netsentinel/glm_key`,未写进任何会提交的文件;
- [ ] `python -m playwright install chromium` 已执行(抽查一次截图存在);
- [ ] REST / webui 只绑定 `127.0.0.1`;容器端口映射带 `127.0.0.1:` 前缀;
- [ ] `data/` 有备份方案;`evidence/` 有清理周期(ETHICS 第七节);
- [ ] `AuditChain.verify('data/audit.jsonl')` 通过并纳入例行巡检;
- [ ] 真实门户上线前:人工核验 www.12377.cn / www.shdf.gov.cn 当前表单结构与 `SELECTORS` 是否一致(见 README 路线图)。
