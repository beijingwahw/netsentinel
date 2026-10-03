#!/usr/bin/env node
/**
 * NetSentinel(净网哨兵)computer-use 备选驱动(A17)。
 *
 * 用途:
 *   读取 SubmissionPlan 的 JSON 计划文件(netsentinel/submit/playbook_gen.py
 *   的 plan_to_json 输出,字段名 snake_case),逐步分发执行
 *   goto / click / fill / select / wait / screenshot / human_gate。
 *   执行原语被抽象为 env 六原语,提供三种引擎:
 *     - console(默认):人工模式 —— 逐步打印中文指令,由人工在自己浏览器里操作,
 *       每步回车确认;humanGate 同样等待人工回车。
 *     - playwright:Playwright 库回退引擎(装了 playwright 就能用;
 *       `npm i playwright && npx playwright install chromium`)。
 *     - plugin:dsh-computer-use-plugin 客户端适配引擎(按仓库 README 调研结果编写,
 *       API 未实测,需要自备适配模块,见 docs/computer-use-integration.md §2/§5)。
 *
 * 用法:
 *   node drivers/driver.mjs plan.json                          # console 人工模式
 *   node drivers/driver.mjs plan.json --engine=playwright      # Playwright 引擎
 *   node drivers/driver.mjs plan.json --engine=plugin --plugin-module=./my-dsh-client.mjs
 *   node drivers/driver.mjs plan.json --dry-run                # 只打印步骤,不执行
 *   可选:--out-dir=<dir>  截图输出目录
 *         --headless / --no-headless(仅 playwright 引擎)
 *         --allow-real-portal  允许访问真实举报门户(默认拒绝,防误触;仅限人工监督下的正式提交)
 *   退出码:0=全部步骤完成;1=中途失败(见 stopped_at);2=用法/计划文件错误。
 *
 * 安全声明(项目红线,CONTRACTS.md §0,违反即缺陷):
 *   1. 本驱动绝不识别、绝不绕过、绝不代填验证码;目标是验证码(selector/text 含
 *      captcha/验证码)的 click/fill/select 会被直接拒绝,验证码环节只能走 human_gate。
 *   2. human_gate 步骤一律暂停等待人工回车确认,本驱动不提供任何跳过人工门的参数。
 *   3. 开发与测试期间绝不访问 www.12377.cn / www.shdf.gov.cn:默认拒绝真实门户主机,
 *      只允许 127.0.0.1 / localhost 的 mock(A16);正式提交须人工监督并显式加
 *      --allow-real-portal。
 *   4. 提交频控(submit_min_interval_s / submit_max_per_day)由 Python 侧 orchestrator
 *      (A18)强制生效,本驱动不得、也不会绕过。
 *
 * Node 18+;仅依赖 node: 内置模块,playwright / 插件适配模块按需动态加载。
 */

import { mkdir, readFile } from 'node:fs/promises';
import readline from 'node:readline/promises';
import { setTimeout as sleep } from 'node:timers/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

const ENGINES = new Set(['console', 'playwright', 'plugin']);
const REAL_PORTAL_HOSTS = new Set([
  'www.12377.cn', '12377.cn', 'www.shdf.gov.cn', 'shdf.gov.cn',
]);
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
const VALID_ACTIONS = new Set([
  'goto', 'click', 'fill', 'select', 'wait', 'screenshot', 'human_gate',
]);

// ------------------------------------------------------------------ 安全守卫

/** goto 前的 URL 安检:仅 http/https;真实门户默认拒绝(红线 3)。 */
function guardUrl(url, opts) {
  let u;
  try {
    u = new URL(url);
  } catch {
    throw new Error(`非法 URL:${url}`);
  }
  if (!['http:', 'https:'].includes(u.protocol)) {
    throw new Error(`仅允许 http/https 协议:${url}`);
  }
  const host = u.hostname.toLowerCase();
  if (REAL_PORTAL_HOSTS.has(host) && !LOCAL_HOSTS.has(host) && !opts.allowRealPortal) {
    throw new Error(
      `目标是真实举报门户 ${host}:测试期间禁止自动化访问;`
      + '确属人工监督下的正式提交请显式加 --allow-real-portal',
    );
  }
}

/** 红线 1:目标是验证码的自动化操作一律拒绝。 */
function assertNotCaptcha(step) {
  const target = [step.selector, step.text, step.label, step.meta?.target]
    .filter(Boolean).join(' ');
  if (/captcha|验证码/i.test(target)) {
    throw new Error(
      '安全红线:验证码环节只能人工完成(HUMAN_GATE),驱动拒绝自动处理验证码:'
      + `step=${step.action} target=${step.selector || step.text || step.label}`,
    );
  }
}

// ------------------------------------------------------------------ 执行分发

/**
 * 把单个 Step 分发到 env 原语。
 * @param {object} step 计划中的一步(snake_case 字段)
 * @param {object} env 六原语执行环境:goto/click/fill/select/wait/screenshot/humanGate
 * @param {object} [ctx] 上下文 { index } 等
 */
export async function executeStep(step, env, ctx = {}) {
  const action = String(step.action || '').toLowerCase();
  if (!VALID_ACTIONS.has(action)) {
    throw new Error(`未知步骤动作:${step.action}(计划文件损坏或版本不兼容)`);
  }
  const timeoutMs = Math.max(1, Number(step.timeout_s) || 10) * 1000;
  switch (action) {
    case 'goto':
      return env.goto({ url: step.value || step.meta?.url || '', timeoutMs, step });
    case 'click':
      assertNotCaptcha(step); // 红线:验证码字段连点击聚焦都不自动化
      return env.click({ selector: step.selector, text: step.text || step.label, timeoutMs, step });
    case 'fill':
      assertNotCaptcha(step);
      return env.fill({
        selector: step.selector, text: step.text || step.label,
        value: step.value ?? '', timeoutMs, step,
      });
    case 'select':
      assertNotCaptcha(step);
      return env.select({
        selector: step.selector, text: step.text || step.label,
        value: step.value ?? '', timeoutMs, step,
      });
    case 'wait':
      return env.wait(Number(step.value) || 1);
    case 'screenshot':
      return env.screenshot(step.label || 'screenshot');
    case 'human_gate':
      // 红线 2:人工门没有任何自动化实现,一律交给 env.humanGate 暂停等待人工
      return env.humanGate({ ...step, index: ctx.index });
    default:
      throw new Error(`未知步骤动作:${action}`);
  }
}

// ------------------------------------------------------------------ 人工门提示

async function promptHumanGate(rl, step) {
  console.log('');
  console.log('════════ 人工门(HUMAN_GATE)════════');
  console.log(`任务:${step.label || '人工核对信息、上传证据包 zip 并输入验证码'}`);
  console.log('请人工完成:');
  console.log('  1) 核对已自动填写的信息是否准确;');
  console.log('  2) 上传证据包 zip 附件(v1 不做自动上传);');
  console.log('  3) 输入验证码 —— 只能人工输入,任何自动化组件不得代填。');
  await rl.question('全部完成并确认可以继续后,按回车 > ');
}

function describeTarget(selector, text) {
  if (selector && text) return `${text}(${selector})`;
  return selector || text || '(计划未提供目标)';
}

function slug(text) {
  return String(text).replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '')
    .slice(0, 24) || 'step';
}

// ------------------------------------------------------------------ 引擎:console(人工模式)

function createConsoleEnv(opts, result) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  let shot = 0;
  const confirm = async (instruction) => {
    await rl.question(`${instruction}\n  ↳ 人工完成后按回车继续 > `);
  };
  return {
    async goto({ url }) {
      guardUrl(url, opts);
      await confirm(`【人工】请在浏览器打开举报入口:${url}`);
    },
    async click({ selector, text }) {
      await confirm(`【人工】请点击:${describeTarget(selector, text)}`);
    },
    async fill({ selector, text, value }) {
      await confirm(`【人工】请在 ${describeTarget(selector, text)} 中填写:${value}`);
    },
    async select({ selector, text, value }) {
      await confirm(`【人工】请在 ${describeTarget(selector, text)} 中选择:${value}`);
    },
    async wait(seconds) {
      await sleep(Math.max(0, seconds) * 1000);
    },
    async screenshot(label) {
      shot += 1;
      const target = opts.outDir
        ? path.join(opts.outDir, `step-${String(shot).padStart(2, '0')}-${slug(label)}.png`)
        : null;
      await confirm(
        `【人工】请截图留存${target ? `并保存到 ${target}` : '(建议存入本次运行目录)'}:${label}`,
      );
      if (target) result.screenshots.push(target);
    },
    async humanGate(step) {
      await promptHumanGate(rl, step);
    },
    async close() {
      rl.close();
    },
  };
}

// ------------------------------------------------------------------ 引擎:playwright(回退)

async function createPlaywrightEnv(opts, result) {
  let pw;
  try {
    pw = await import('playwright');
  } catch (err) {
    throw new Error(
      '未安装 playwright 库,playwright 引擎不可用。可执行 '
      + '`npm i playwright && npx playwright install chromium`,或改用 --engine=console。'
      + `(${err.message})`,
    );
  }
  const browser = await pw.chromium.launch({ headless: opts.headless });
  const page = await browser.newPage();
  const outDir = opts.outDir || 'data/runs/driver-playwright';
  await mkdir(outDir, { recursive: true });
  let shot = 0;
  const locatorFor = (selector, text) => (selector
    ? page.locator(selector).first()
    : page.getByText(text).first());
  return {
    async goto({ url, timeoutMs }) {
      guardUrl(url, opts);
      await page.goto(url, { timeout: timeoutMs, waitUntil: 'domcontentloaded' });
    },
    async click({ selector, text, timeoutMs }) {
      await locatorFor(selector, text).click({ timeout: timeoutMs });
    },
    async fill({ selector, text, value, timeoutMs }) {
      await locatorFor(selector, text).fill(value, { timeout: timeoutMs });
    },
    async select({ selector, text, value, timeoutMs }) {
      await locatorFor(selector, text).selectOption(value, { timeout: timeoutMs });
    },
    async wait(seconds) {
      await sleep(Math.max(0, seconds) * 1000);
    },
    async screenshot(label) {
      shot += 1;
      const file = path.join(outDir, `step-${String(shot).padStart(2, '0')}-${slug(label)}.png`);
      await page.screenshot({ path: file, fullPage: true });
      result.screenshots.push(file);
      console.log(`  [screenshot] ${file}`);
    },
    async humanGate(step) {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      try {
        await promptHumanGate(rl, step);
      } finally {
        rl.close();
      }
    },
    async close() {
      await browser.close();
    },
  };
}

// ------------------------------------------------------------------ 引擎:plugin(dsh-computer-use-plugin 适配)

/** 从 find_text 结果中尽力提取坐标(适配层返回结构未定,按常见形态兼容)。 */
function extractPoint(res) {
  if (!res) return null;
  const cand = res.point ?? res.data ?? res.result ?? res;
  if (Array.isArray(cand) && cand.length >= 2
      && typeof cand[0] === 'number' && typeof cand[1] === 'number') {
    return { x: cand[0], y: cand[1] };
  }
  if (cand && typeof cand.x === 'number' && typeof cand.y === 'number') {
    return { x: cand.x, y: cand.y };
  }
  return null;
}

/**
 * 【假设性实现】调研结论(README,2026-10 抓取):dsh-computer-use-plugin 是
 * DeepSeek Harness(DSH)宿主插件,向 LLM Agent 注册 take_screenshot / click_mouse /
 * type_text / open_url / find_text 等工具,并没有官方独立 JS 客户端。
 * 本引擎约定一个最小适配模块接口:default export 或具名导出
 *   async function callTool(name, args) -> { ok: boolean, ...工具返回 }
 * 适配模块如何连到 DSH 宿主(HTTP / 子进程 / 官方 SDK)需真实环境联调后确定,
 * 详见 docs/computer-use-integration.md §2 与 §5。
 */
async function createPluginEnv(opts, result) {
  const modName = opts.pluginModule || 'dsh-computer-use-plugin';
  let client;
  try {
    const mod = await import(modName);
    client = mod.callTool || mod.default?.callTool;
    if (typeof client !== 'function') {
      throw new Error('模块未导出 callTool(name, args)');
    }
  } catch (err) {
    throw new Error(
      `无法加载 computer-use 插件客户端 "${modName}"(${err.message})。`
      + '调研结论:该插件为 DSH 宿主插件,无独立 JS 客户端;请先编写适配模块 '
      + '(见 docs/computer-use-integration.md §2),或改用 --engine=playwright|console',
    );
  }
  const call = async (name, args) => {
    const res = await client(name, args);
    if (res && res.ok === false) {
      throw new Error(`插件工具 ${name} 返回失败:${JSON.stringify(res)}`);
    }
    return res;
  };
  // 视觉路线没有 CSS selector,一律依赖计划里的 text/label 文本定位
  const clickByText = async (text) => {
    const found = await call('find_text', { keyword: text });
    const pt = extractPoint(found);
    if (!pt) {
      throw new Error(
        `find_text 未找到目标文本:"${text}"(视觉路线依赖 Step.text,请在计划中补全 text 字段)`,
      );
    }
    await call('click_mouse', { x: pt.x, y: pt.y, target_description: text });
  };
  return {
    async goto({ url }) {
      guardUrl(url, opts);
      await call('open_url', { url, reasoning: 'NetSentinel 举报计划 goto 步骤' });
    },
    async click({ text }) {
      await clickByText(text);
    },
    async fill({ text, value }) {
      // 注意:type_text 对验证码/密码焦点会返回 ACTION_REQUIRED(README 调研结论),
      // 此处 assertNotCaptcha 已在 executeStep 里先行拒绝,构成双保险。
      await clickByText(text);
      await call('type_text', { text: value, clearFirst: true });
    },
    async select({ text, value }) {
      await clickByText(text); // 展开下拉
      await clickByText(value); // 点击选项文本
    },
    async wait(seconds) {
      await sleep(Math.max(0, seconds) * 1000); // 亦可换用 diff_view 观察页面稳定
    },
    async screenshot(label) {
      const res = await call('take_screenshot', { force: true });
      const note = `take_screenshot(${label})${
        res?.path ? ` -> ${res.path}` : '(返回结构待联调,截图落盘路径未知)'}`;
      result.screenshots.push(`(plugin)${label}`);
      console.log(`  [screenshot] ${note}`);
    },
    async humanGate(step) {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      try {
        await promptHumanGate(rl, step);
      } finally {
        rl.close();
      }
    },
    async close() {},
  };
}

// ------------------------------------------------------------------ 计划加载与运行

async function loadPlan(planPath) {
  let plan;
  try {
    plan = JSON.parse(await readFile(planPath, 'utf8'));
  } catch (err) {
    throw new Error(`读取/解析计划文件失败:${planPath}(${err.message})`);
  }
  if (!plan || typeof plan !== 'object' || !Array.isArray(plan.steps)) {
    throw new Error('计划文件格式不正确:需要 { portal, entry_url, payload, steps: [...] }');
  }
  return plan;
}

/**
 * 执行一份 SubmissionPlan JSON 计划。
 * @param {string} planPath 计划文件路径(playbook_gen.plan_to_json 的输出)
 * @param {object} [options] { engine, outDir, dryRun, headless, allowRealPortal, pluginModule }
 * @returns {Promise<{ok:boolean,portal:string,screenshots:string[],stopped_at:string,submitted:boolean,notes:string[]}>}
 */
export async function runPlan(planPath, options = {}) {
  const opts = {
    engine: 'console',
    outDir: null,
    dryRun: false,
    headless: true,
    allowRealPortal: false,
    pluginModule: null,
    ...options,
  };
  if (!ENGINES.has(opts.engine)) {
    throw new Error(`未知引擎:${opts.engine}(可选:${[...ENGINES].join('|')})`);
  }
  if (opts.outDir) {
    await mkdir(opts.outDir, { recursive: true });
  }

  const plan = await loadPlan(planPath);
  const result = {
    ok: false,
    portal: String(plan.portal ?? ''),
    screenshots: [],
    stopped_at: '',
    submitted: false,
    notes: [`引擎=${opts.engine},计划=${planPath},步骤数=${plan.steps.length}`],
  };

  const envFactories = {
    console: createConsoleEnv,
    playwright: createPlaywrightEnv,
    plugin: createPluginEnv,
  };
  const env = await envFactories[opts.engine](opts, result);

  let currentDesc = '';
  try {
    for (const [i, step] of plan.steps.entries()) {
      const desc = `#${i + 1} ${step.action}${step.label ? `(${step.label})` : ''}`;
      currentDesc = desc;
      if (opts.dryRun) {
        result.notes.push(`[dry-run] ${desc} 跳过`);
        continue;
      }
      await executeStep(step, env, { index: i + 1 });
      result.notes.push(`[ok] ${desc}`);
    }
    result.ok = true;
    // 是否真正点击了提交按钮:以计划里最后一个 human_gate 之后的 click(submit) 为准,
    // 这里只做记录,真相以截图与人工确认为准。
    const hasSubmit = plan.steps.some((s) => String(s.action).toLowerCase() === 'click'
      && /submit|提交/i.test(`${s.selector || ''}${s.text || ''}${s.label || ''}`));
    result.submitted = result.ok && hasSubmit && !opts.dryRun;
  } catch (err) {
    result.stopped_at = currentDesc || String(err.message.slice(0, 120));
    result.notes.push(`[fail] ${err.message}`);
  } finally {
    await env.close?.();
  }
  return result;
}

// ------------------------------------------------------------------ CLI

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      engine: { type: 'string', default: 'console' },
      'out-dir': { type: 'string' },
      'dry-run': { type: 'boolean', default: false },
      headless: { type: 'boolean', default: true },
      'allow-real-portal': { type: 'boolean', default: false },
      'plugin-module': { type: 'string' },
    },
  });
  const planPath = positionals[0];
  if (!planPath) {
    console.error(
      '用法: node drivers/driver.mjs <plan.json> '
      + '[--engine=console|playwright|plugin] [--out-dir=<dir>] [--dry-run] '
      + '[--no-headless] [--allow-real-portal] [--plugin-module=<mod>]',
    );
    process.exitCode = 2;
    return;
  }
  let result;
  try {
    result = await runPlan(planPath, {
      engine: values.engine,
      outDir: values['out-dir'],
      dryRun: values['dry-run'],
      headless: values.headless,
      allowRealPortal: values['allow-real-portal'],
      pluginModule: values['plugin-module'],
    });
  } catch (err) {
    console.error(`[driver] ${err.message}`);
    process.exitCode = 2;
    return;
  }
  console.log('');
  console.log('════════ NetSentinel computer-use 驱动:执行摘要 ════════');
  console.log(`引擎:${values.engine}  门户:${result.portal}  结果:${result.ok ? '全部步骤完成' : '中止'}`);
  if (result.stopped_at) console.log(`中止于:${result.stopped_at}`);
  for (const note of result.notes) console.log(`  ${note}`);
  process.exitCode = result.ok ? 0 : 1;
}

const isMain = process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  await main();
}
