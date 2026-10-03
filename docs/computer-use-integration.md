# Computer-Use 备选执行路线集成说明(A17)

> 适用组件:`netsentinel/submit/playbook_gen.py`(计划 → 人类可读 playbook / JSON)、
> `drivers/driver.mjs`(JS 驱动层)。
> 主路径是 A15 的 Playwright 执行器(`netsentinel/submit/executor_playwright.py`);
> 本文档描述**备选的 computer-use 路线**,供主路径不可用或需要"视觉+人工"混合执行时使用。

## 0. 安全红线(先读这里,不可协商)

以下条款来自 `CONTRACTS.md` §0,computer-use 路线**全部继承**:

1. **验证码只能人工输入。** 任何引擎(Playwright / computer-use 插件)都不得识别、
   绕过或代填验证码;计划中验证码环节只能是 `HUMAN_GATE` 步骤。
   `drivers/driver.mjs` 对 `selector/text/label` 含 `captcha` 或 `验证码` 的
   click/fill/select 步骤**直接抛错拒绝**(双保险之一,另一层见 §1.4)。
2. **真实提交前必须经过 HUMAN_GATE 人工确认。** 驱动不提供任何跳过人工门的参数;
   `human_gate` 步骤一律暂停,等待人工回车确认后才继续。
3. **开发与测试期间绝不访问真实门户**(www.12377.cn / www.shdf.gov.cn)。
   驱动默认拒绝真实门户主机,只放行 `127.0.0.1` / `localhost`(配合 A16 的 mock 门户);
   人工监督下的正式提交必须显式加 `--allow-real-portal`。
4. **提交频控**(默认 60s 间隔、每日 ≤5 次)由 Python 侧 orchestrator(A18)强制,
   computer-use 路线不得绕过。
5. 附件(证据包 zip)上传在 v1 一律由人工门完成,不做自动上传。

---

## 1. 插件调研结论(dsh-computer-use-plugin)

调研对象:https://github.com/beijingwahw/dsh-computer-use-plugin
调研方式:抓取仓库首页与 raw README(2026-10)。**未做代码级实测**,结论均标注
【调研结论】(来自 README 原文)或【假设】(本项目推断,待联调验证)。

### 1.1 插件定位【调研结论】

- 名称:`dsh-computer-use-plugin`,MIT 协议,跨平台(Win/Mac/Linux)。
- 基于 **DeepSeek Harness(DSH)宿主**的插件,定位"纯视觉桌面自动化 Agent 插件":
  放弃 UI 树 / Accessibility API,只靠截图(视觉 Grounding)+ SoM 叠加,
  让 LLM Agent "看"屏幕并操作鼠标键盘。
- **关键结论:它不是传统的"客户端 SDK"。** 没有面向开发者的
  "初始化客户端 → 发送 click 指令"式 JS/TS API;它向 DSH 宿主中的 LLM Agent
  **注册一组工具(tools)**,由模型自主调用。插件入口为 `index.ts` 的 `apply()`
  (`buildAllTools(config)` 构建工具,`onLlmPreRequest` 注入滑动窗口截图)。
  物理执行层由 Python(pyautogui;Windows 下用 `SendInput` + `KEYEVENTF_UNICODE`
  注入文字以规避 IME 干扰)完成【调研结论,细节未实测】。

### 1.2 安装与启用【调研结论,命令未在本机执行】

方式一(pnpm git 依赖):

```bash
pnpm add dsh-computer-use-plugin@github:beijingwahw/dsh-computer-use-plugin
# 或 package.json:
# { "dependencies": { "dsh-computer-use-plugin": "github:beijingwahw/dsh-computer-use-plugin" } }
```

方式二(DSH 插件源声明):`dsh-computer-use-plugin github:beijingwahw/dsh-computer-use-plugin`

构建产物已入库(`dist/` 随仓库分发),安装时不执行构建脚本;通过 `dsh.bundle`
指向 `cordis.patch.yml` 自动注册激活;启动:`pnpm dsh web`。
本地开发:`git clone` → `pnpm install` → `npm run build` → `npm test`。

配置覆盖示例(`cordis.patch.yml`,README 原文):

```yaml
insert:
  id: dsh-computer-use-plugin
  name: 'dsh-computer-use-plugin'
  config:
    mouseSpeed: 1500
    compressWidth: 1440
    jpegQuality: 75
```

### 1.3 API 摘要(工具清单)【调研结论:参数签名来自 README,未实测】

没有"初始化接口";**动作接口 = 工具调用**,**截图接口 = `take_screenshot`**。
与本路线相关的核心工具:

| 工具 | 关键参数 | 说明 |
| --- | --- | --- |
| `take_screenshot` | `region`, `force?` | 截图 + SoM 叠加 + 压缩 + 滑动窗口 + 弹窗感知 + 变化门控;`force:true` 强制刷新 |
| `open_url` | `url`, `reasoning?` | URL 安检(scheme 白名单 http/https,拒 file:/javascript:;`www.` 自动补 https;>2048 字符拒绝)→ 系统默认浏览器跳转 |
| `find_text` | `keyword` | OCR 找文字 → 返回坐标 |
| `click_mouse` | `x`, `y`, `button`, `confidence?`, `target_description?`, `allow_text_click?` | 归一化坐标点击 |
| `type_text` | `text`, `clearFirst` | 焦点处输入;输入后 OCR 焦点邻域自证"真的上屏了" |
| `scroll_page` / `press_hotkey` / `drag_mouse` | `direction, amount` / `keys`(白名单,防注入)/ `startX/Y, endX/Y` | 滚动 / 组合键 / 拖拽 |
| `read_text` / `zoom_inspect` / `diff_view` / `probe_interactivity` | — | 区域读字 / 二阶段精定位 / 视觉差分 / 交互性判决 |
| `replay_actions` | `confirm`, `from_step?`, `to_step?` | 重放动作序列(需 confirm) |
| `save_skill` / `match_skill` / `run_skill` | — | 技能沉淀 / 匹配 / 执行 |
| `start_complex_task` | `userRequest`(另见 `time_budget_sec`) | Planner-Actor 编排 |

### 1.4 与本项目红线的契合点【调研结论】

- `type_text` 对**敏感焦点(密码 / 验证码等风险词)返回 `ACTION_REQUIRED`,
  待输内容绝不回显** —— 与本项目"验证码只能人工输入"天然契合。
- `press_hotkey` 有键位白名单(防注入);`open_url` 有 scheme 白名单 ——
  与本项目"仅 http/https"约束一致。
- 注意:这是插件自身行为,README 声明、未经实测;因此 `drivers/driver.mjs`
  在 `executeStep` 里又做了一层 captcha 硬拒绝,不依赖插件自律。

### 1.5 结论与假设速览

| 内容 | 性质 |
| --- | --- |
| 插件是 DSH 宿主插件、无独立 JS 客户端 SDK;工具名与参数如 §1.3 | 【调研结论】 |
| `type_text` 拒绝验证码焦点、`open_url` scheme 白名单 | 【调研结论】 |
| 为 driver.mjs 编写的 `callTool(name, args)` 适配层接口(§2) | 【假设】 |
| `find_text` 返回值可提取 `{x, y}` 坐标(§2 `extractPoint`) | 【假设】 |
| ZCode node REPL 中引导计算机操作能力的具体 SDK 形态 | 【假设】(以 ZCode 官方 skill 文档为准) |

---

## 2. 在 ZCode node REPL 加载插件客户端(示例)

ZCode 内置 node REPL(`mcp__node_repl__js`)可运行 JS 并驱动计算机操作,但**只允许
引导官方 skill 提供的能力**(browser-use / computer-use 官方 SDK),不能当通用运行时。
由于 dsh 插件没有 JS 客户端,推荐的集成方式是写一个**极小适配模块**,把
`drivers/driver.mjs` 的六个原语落到实际执行层上。适配模块的约定接口【假设】:

```js
// my-dsh-client.mjs —— 适配层骨架(【假设】接口:导出 callTool(name, args))
// 如何连到 DSH 宿主(HTTP / 子进程 / 官方 SDK)需真实环境联调后确定,见 §5。
//
// 安全红线:本适配层不封装任何验证码相关调用;captcha 类目标由 driver 侧拒绝。
export async function callTool(name, args) {
  // TODO(联调):把 {name, args} 转发给 DSH 宿主 / 插件工具执行层,并返回:
  //   { ok: true, ...工具返回(find_text 需含坐标 {x, y} 或 [x, y]) }
  //   { ok: false, error: '...' }
  throw new Error('my-dsh-client 尚未接入 DSH 宿主(未联调)');
}
```

在 ZCode node REPL 中做冒烟验证(仅演示加载方式;官方 computer-use SDK 的确切
引导代码以 ZCode 内置 skill 文档为准【假设】):

```js
// 标题:冒烟验证 computer-use 适配模块是否可加载
// 1) 用绝对 file:// URL 引入本仓库内的适配模块(bare specifier 不解析):
const { pathToFileURL } = await import('node:url');
const mod = await import(pathToFileURL('C:/1/netsentinel/drivers/my-dsh-client.mjs').href);
// 2) 约定的最小接口:
typeof mod.callTool; // 期望 'function'
// 3) 冒烟(联调后才会真正成功;失败即视为"plugin 引擎不可用"):
//    await mod.callTool('take_screenshot', { force: true });
```

driver.mjs 的 plugin 引擎即按上述约定动态加载适配模块:

```bash
node drivers/driver.mjs plan.json --engine=plugin --plugin-module=./my-dsh-client.mjs
```

---

## 3. SubmissionPlan Step → computer-use 操作映射表

`Step` 字段(契约见 `netsentinel/contracts.py`):`action`(StepAction 枚举)、
`label`(中文说明)、`selector`(CSS,浏览器执行器用)、`text`(人类可读目标,
**视觉路线的主力定位字段**)、`value`(fill/select 的值 / goto 的 URL / wait 的秒数
字符串)、`timeout_s`、`meta`。

| Step(action + 字段) | computer-use 对应调用(插件工具) | 人工说明 |
| --- | --- | --- |
| `goto` + `value`=URL | `open_url({ url: value, reasoning: label })` | scheme 白名单 http/https;系统默认浏览器打开;driver 侧另有真实门户守卫 |
| `click` + `selector`/`text` | `find_text({ keyword: text })` → 取坐标 → `click_mouse({ x, y, target_description: text })` | 视觉路线**没有 CSS selector**,必须依赖 `text`/`label`;selector 仅作人工参考 |
| `fill` + `text` + `value` | 先按 click 定位焦点 → `type_text({ text: value, clearFirst: true })` | 若焦点是验证码/密码,插件返回 `ACTION_REQUIRED`【调研结论】;driver 侧对 captcha 目标先行抛错【双保险】 |
| `select` + `value` | click 展开下拉 → `find_text({ keyword: value })` → `click_mouse` 点击选项 | 无原生 `<select>` 支持,按视觉点击选项文本;选项文本须与 `value` 一致 |
| `wait` + `value`=秒 | 宿主 sleep;或 `diff_view` 观察页面稳定后继续 | — |
| `screenshot` + `label` | `take_screenshot({ force: true })` | 截图含 SoM 叠加,可作执行证据;落盘路径联调后接 `--out-dir` |
| `human_gate`(HUMAN_GATE) | **无任何自动化调用:暂停并提示人工** | 验证码输入、证据包 zip 上传、信息核对全部由人工完成;人工回车确认后驱动才继续 |
| `timeout_s` | 传给宿主的等待/重试逻辑 | README 未明示统一的超时参数,当前由各引擎自行解释【假设】 |
| `meta` | 预留扩展(如 mock 环境标记) | — |

补充【调研结论,谨慎使用】:`replay_actions({ confirm })` 可重放历史动作、
`save_skill/run_skill` 可沉淀"提交举报"技能;若日后启用,`confirm` 必须绑定
HUMAN_GATE 人工确认,不得默认放行(本项目红线 2)。

---

## 4. drivers/driver.mjs 使用方法

前置:Node 18+(仅依赖 `node:` 内置模块;`--no-headless` 写法需 Node ≥18.15)。

```bash
cd /c/1/netsentinel

# 0) 语法自检(本机未装 node 时,在任一 Node 18+ 环境执行)
node --check drivers/driver.mjs

# 1) Python 侧生成计划 JSON(plan_to_json 输出,snake_case 字段)
python -c "from netsentinel.submit.portal_12377 import plan_12377; \
  from netsentinel.submit.playbook_gen import plan_to_json; import json; \
  import sys; print(json.dumps(plan_to_json(plan_12377(entry, cfg)), ensure_ascii=False))" > plan.json
#    (兄弟模块 A12/A13 未就绪时,可用 tests/test_playbook_gen.py 的 make_plan 自造样例)

# 2) 干跑:只打印步骤序列,不执行、不需要任何引擎依赖
node drivers/driver.mjs plan.json --dry-run

# 3) console 人工模式(默认引擎):逐步打印中文指令,人工操作后回车继续
node drivers/driver.mjs plan.json --out-dir=data/runs/manual-001

# 4) playwright 引擎(装了 playwright 库即可;测试只许连 127.0.0.1 mock)
npm i playwright && npx playwright install chromium
node drivers/driver.mjs plan.json --engine=playwright --out-dir=data/runs/pw-001

# 5) plugin 引擎(未联调,需自备适配模块,见 §2)
node drivers/driver.mjs plan.json --engine=plugin --plugin-module=./my-dsh-client.mjs

# 6) 人工监督下的正式提交(真实门户;默认被拒绝,必须显式声明)
node drivers/driver.mjs plan.json --engine=playwright --allow-real-portal
```

要点:

- 计划文件即 `playbook_gen.plan_to_json` 的输出;同一份计划也可交给
  `python -m netsentinel.submit.playbook_gen plan.json -o playbook.md` 渲染人工手册。
- 六原语:`env.goto / click / fill / select / wait / screenshot / humanGate`;
  `humanGate` 在**所有**引擎里都是 readline 等待人工回车,无自动化实现。
- 退出码:`0` 全部步骤完成;`1` 中止(摘要里给出 `stopped_at`);`2` 用法/计划文件错误。
- 截图:playwright 引擎落盘到 `--out-dir`(默认 `data/runs/driver-playwright`);
  console 引擎提示人工保存;plugin 引擎记录 `take_screenshot` 调用(路径待联调)。

---

## 5. 风险与待验证项清单

| # | 事项 | 性质 | 建议 |
| --- | --- | --- | --- |
| 1 | 插件工具参数(§1.3)来自 README,未实测;返回值结构(尤其 `find_text` 的坐标格式)未知 | 未核实 API | 真实环境联调后修正 `extractPoint` 与适配模块 |
| 2 | 插件无 JS 客户端;`callTool` 适配层如何连到 DSH 宿主(HTTP / 子进程 / SDK)未定 | 假设 | 先跑通最小链路:`take_screenshot` → `find_text` → `click_mouse` |
| 3 | `type_text` 对验证码焦点返回 `ACTION_REQUIRED` 为 README 声明,未实测 | 未核实 | 联调用例必须包含"验证码字段自动填写被拒"的断言;driver 侧 captcha 硬拒绝已先行兜底 |
| 4 | Windows 下 `SendInput` + IME 场景、SoM 坐标点击精度、`find_text` 中文 OCR 识别率 | 需真实环境联调 | 在本地 mock 表单(A16)上做点击/填写精度评估 |
| 5 | `<input type=file>` 附件上传需文件对话框操作,v1 不做 | 设计决定 | 保持人工门完成;如做 v2,上传前仍须 HUMAN_GATE |
| 6 | 真实门户联调(仅限项目负责人批准、人工全程监督) | 流程 | 日常测试只连 `127.0.0.1` mock;驱动默认拒绝真实门户主机 |
| 7 | 频控 / 审计在 Python orchestrator(A18),computer-use 路线绕过即缺陷 | 流程 | code review 时检查 `run_submit` 之外无提交入口 |
| 8 | 本机(开发机)未安装 Node,`node --check` 尚未实际执行 | 环境 | 在任一 Node 18+ 机器上执行 `node --check drivers/driver.mjs` 补验 |

## 6. 额外依赖建议

- **Python 侧:零新增依赖**(playbook_gen 仅标准库,符合团队规则)。
- Node 侧(可选,不进 `pyproject.toml`):
  - `playwright`(npm)—— pyproject 已有 optional `browser = ["playwright>=1.40"]`
    的 Python 版;JS 驱动如需 playwright 引擎,另装 npm 版并 `npx playwright install chromium`;
  - dsh-computer-use-plugin 链路需要 `pnpm` + DSH 宿主(重度依赖,建议隔离在
    独立目录/容器,不并入本仓库依赖树);
  - Node ≥ 18(建议 ≥ 18.15,以支持 parseArgs 的 `--no-` 布尔否定写法)。
