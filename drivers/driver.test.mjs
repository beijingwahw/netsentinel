#!/usr/bin/env node
/**
 * NetSentinel computer-use 驱动安全测试(A17 加固;零第三方依赖,Node 18+
 * 内置 node:test)。
 *
 * 运行:node --test drivers/
 *
 * 覆盖(drivers/driver.mjs 导出的可测纯函数与分发逻辑):
 * 1. guardUrl(红线 3 第一层,goto 前安检):
 *    - 拒绝真实门户四主机 www.12377.cn / 12377.cn / www.shdf.gov.cn / shdf.gov.cn;
 *    - 拒绝其任意子域(bzpt.shdf.gov.cn / jbts.12377.cn 等,含带端口形态);
 *    - 放行 127.0.0.1 / localhost / ::1 / example 域等本地与第三方 mock;
 *    - 拒绝非 http(s) 协议与非法 URL;--allow-real-portal 显式放行真实门户。
 * 2. isRealPortalHost / hostOf:子域匹配语义(后缀必须以 "." 分隔,
 *    notshdf.gov.cn、12377.cn.example.com 之类伪装域不命中)。
 * 3. guardFinalUrl(红线 3 第二层,goto 后最终落点安检):mock 经 302 /
 *    meta refresh / JS 跳转落在真实门户 → 中文报错;本地/普通域放行。
 * 4. assertNotCaptcha(红线 1,经 executeStep 分发验证):目标是验证码的
 *    click/fill/select 一律拒绝(selector/text/label/meta.target 四路命中,
 *    大小写不敏感,captcha 与"验证码"双关键词);human_gate 不受限。
 * 5. human_gate(红线 2):一律转交 env.humanGate 暂停等待人工;CLI 参数表
 *    不含任何跳过/自动确认人工门的参数;--dry-run 只是整体预览,不执行
 *    任何步骤、submitted 恒为 false。
 * 6. CLI 兼容:不带参数运行打印中文用法并以退出码 2 结束(命令行行为保持)。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

import {
  CLI_OPTIONS,
  assertNotCaptcha,
  executeStep,
  guardFinalUrl,
  guardUrl,
  hostOf,
  isRealPortalHost,
  runPlan,
} from './driver.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DRIVER_PATH = path.join(HERE, 'driver.mjs');
const NO_PORTAL = { allowRealPortal: false };

/** 记录调用序列的六原语假执行环境(humanGate 不读 stdin,直接记录)。 */
function recordingEnv() {
  const calls = [];
  const env = {
    async goto(a) { calls.push(['goto', a.url]); },
    async click(a) { calls.push(['click', a.selector]); },
    async fill(a) { calls.push(['fill', a.selector, a.value]); },
    async select(a) { calls.push(['select', a.selector, a.value]); },
    async wait(s) { calls.push(['wait', s]); },
    async screenshot(l) { calls.push(['screenshot', l]); },
    async humanGate(s) { calls.push(['human_gate', s.label]); },
    async afterStep() { calls.push(['afterStep']); },
  };
  return { env, calls };
}

// ---------------------------------------------------------------------------
// guardUrl:真实门户四主机(含子域)拒绝
// ---------------------------------------------------------------------------
test('guardUrl 拒绝真实门户四主机与任意子域(默认关闭真实门户)', () => {
  const hosts = [
    'www.12377.cn', '12377.cn',
    'www.shdf.gov.cn', 'shdf.gov.cn',
    'bzpt.shdf.gov.cn',          // 任务点名的子域
    'jbts.12377.cn',             // 12377 子域
    'a.b.www.shdf.gov.cn',       // 多级子域
    'WWW.12377.CN',              // 大小写不敏感
  ];
  for (const host of hosts) {
    for (const scheme of ['http', 'https']) {
      assert.throws(
        () => guardUrl(`${scheme}://${host}/form`, NO_PORTAL),
        (err) => {
          assert.ok(err instanceof Error);
          assert.match(err.message, /真实举报门户/);
          assert.match(err.message, /--allow-real-portal/);
          return true;
        },
        `应拒绝 ${scheme}://${host}/form`,
      );
    }
  }
  // 带端口的真实门户主机同样拒绝(hostname 不含端口)
  assert.throws(() => guardUrl('https://www.12377.cn:443/form', NO_PORTAL), /真实举报门户/);
});

test('guardUrl 放行本地回环与普通第三方域(mock 场景)', () => {
  const urls = [
    'http://127.0.0.1:8900/12377_mock.html',
    'https://127.0.0.1:8901/form',
    'http://localhost:8900/',
    'http://[::1]:8900/',           // IPv6 回环
    'https://example.com/form',
    'http://mock.example.org:9000/jbts',
    'http://test.internal:8080/',
  ];
  for (const url of urls) {
    assert.doesNotThrow(() => guardUrl(url, NO_PORTAL), `应放行 ${url}`);
  }
});

test('guardUrl 拒绝非 http(s) 协议与非法 URL', () => {
  assert.throws(() => guardUrl('ftp://www.12377.cn/', NO_PORTAL), /仅允许 http\/https/);
  assert.throws(() => guardUrl('file:///etc/passwd', NO_PORTAL), /仅允许 http\/https/);
  assert.throws(() => guardUrl('javascript:alert(1)', NO_PORTAL), /仅允许 http\/https/);
  assert.throws(() => guardUrl('not a url', NO_PORTAL), /非法 URL/);
  assert.throws(() => guardUrl('', NO_PORTAL), /非法 URL/);
});

test('guardUrl 显式 --allow-real-portal 时放行真实门户(人工监督下的正式提交)', () => {
  assert.doesNotThrow(() => guardUrl('https://www.12377.cn/', { allowRealPortal: true }));
  assert.doesNotThrow(() => guardUrl('https://bzpt.shdf.gov.cn/jbts', { allowRealPortal: true }));
});

// ---------------------------------------------------------------------------
// isRealPortalHost / hostOf:子域匹配语义
// ---------------------------------------------------------------------------
test('isRealPortalHost:四主机等值与子域后缀命中,伪装域不命中', () => {
  for (const hit of [
    'www.12377.cn', '12377.cn', 'www.shdf.gov.cn', 'shdf.gov.cn',
    'bzpt.shdf.gov.cn', 'sub.12377.cn', 'x.y.shdf.gov.cn',
  ]) {
    assert.equal(isRealPortalHost(hit), true, `${hit} 应命中真实门户`);
  }
  // 后缀必须以 "." 分隔:前后拼接的伪装域一律不命中
  for (const miss of [
    'notshdf.gov.cn', 'fake-shdf.gov.cn', '12377.cn.example.com',
    'shdf.gov.cn.evil.io', 'www.12377.cn.attacker.com', 'example.com',
    '127.0.0.1', 'localhost', '', undefined, null,
  ]) {
    assert.equal(isRealPortalHost(miss), false, `${miss} 不应命中真实门户`);
  }
});

test('hostOf:提取小写主机名,非法输入返回空串不抛错', () => {
  assert.equal(hostOf('https://WWW.Example.COM:8443/a?b=1'), 'www.example.com');
  assert.equal(hostOf('http://127.0.0.1:8900/x'), '127.0.0.1');
  assert.equal(hostOf(''), '');
  assert.equal(hostOf('not-a-url'), '');
  assert.equal(hostOf(null), '');
});

// ---------------------------------------------------------------------------
// guardFinalUrl:goto 之后的最终落点安检(302/meta refresh/JS 跳转)
// ---------------------------------------------------------------------------
test('guardFinalUrl:最终落在真实门户(含子域)即中文报错', () => {
  assert.throws(
    () => guardFinalUrl('https://www.12377.cn/', NO_PORTAL),
    /最终落在真实举报门户/,
  );
  assert.throws(
    () => guardFinalUrl('http://bzpt.shdf.gov.cn/jbts/form?x=1', NO_PORTAL),
    /bzpt\.shdf\.gov\.cn/,
  );
  assert.throws(
    () => guardFinalUrl('https://jbts.12377.cn/#/report', NO_PORTAL),
    /--allow-real-portal/,
  );
});

test('guardFinalUrl:本地与普通域落点放行;显式放行开关生效', () => {
  assert.doesNotThrow(() => guardFinalUrl('http://127.0.0.1:8900/after-redir', NO_PORTAL));
  assert.doesNotThrow(() => guardFinalUrl('https://example.com/step', NO_PORTAL));
  assert.doesNotThrow(() => guardFinalUrl('https://www.12377.cn/', { allowRealPortal: true }));
});

// ---------------------------------------------------------------------------
// 红线 1:验证码自动操作拒绝(assertNotCaptcha + executeStep 分发)
// ---------------------------------------------------------------------------
test('executeStep:验证码目标的 click/fill/select 一律拒绝(中文报错)', async () => {
  const { env } = recordingEnv();
  for (const action of ['click', 'fill', 'select']) {
    // executeStep 是 async 函数:验证码拒绝以 rejected promise 形式抛出
    await assert.rejects(
      () => executeStep({ action, selector: '#report-captcha', value: '1234' }, env),
      (err) => err instanceof Error && /验证码|captcha/i.test(err.message),
      `${action} 触碰验证码应拒绝`,
    );
  }
});

test('executeStep:验证码字样命中 selector/text/label/meta.target 四路(大小写不敏感)', async () => {
  const { env } = recordingEnv();
  const variants = [
    { action: 'click', selector: '#form-captcha-input', label: 'x' },
    { action: 'fill', selector: '#x', text: '图形验证码', value: '1' },
    { action: 'select', selector: '#x', label: 'CAPTCHA 选择', value: 'y' },
    { action: 'click', selector: '#x', label: 'y', meta: { target: 'captcha-field' } },
    { action: 'fill', selector: '#x', label: '请输入验证码', value: '0000' },
  ];
  for (const step of variants) {
    await assert.rejects(
      () => executeStep(step, env),
      /安全红线/,
      JSON.stringify(step),
    );
  }
  // 直接调用导出的纯函数同样生效(同步抛出)
  assert.throws(() => assertNotCaptcha({ action: 'fill', selector: '#report-captcha' }), /安全红线/);
});

test('executeStep:非验证码步骤正常放行并分发到对应原语', () => {
  const { env, calls } = recordingEnv();
  const steps = [
    { action: 'goto', value: 'http://127.0.0.1:8900/', label: '打开举报入口' },
    { action: 'select', selector: '#report-type', value: '色情低俗信息' },
    { action: 'fill', selector: '#report-url', value: 'http://bad.example.com/' },
    { action: 'click', selector: '#report-submit' },
    { action: 'wait', value: '2' },
    { action: 'screenshot', label: '提交结果截图' },
  ];
  return Promise.all(steps.map((s) => executeStep(s, env))).then(() => {
    assert.deepEqual(calls, [
      ['goto', 'http://127.0.0.1:8900/'],
      ['select', '#report-type', '色情低俗信息'],
      ['fill', '#report-url', 'http://bad.example.com/'],
      ['click', '#report-submit'],
      ['wait', 2],
      ['screenshot', '提交结果截图'],
    ]);
  });
});

test('executeStep:未知动作拒绝;meta.url 兜底缺 value 的 goto', async () => {
  const { env, calls } = recordingEnv();
  await assert.rejects(() => executeStep({ action: 'focus' }, env), /未知步骤动作/);
  await executeStep({ action: 'goto', meta: { url: 'http://127.0.0.1:8900/x' } }, env);
  assert.deepEqual(calls, [['goto', 'http://127.0.0.1:8900/x']]);
});

// ---------------------------------------------------------------------------
// 红线 2:human_gate 无任何跳过参数,一律转交 env.humanGate
// ---------------------------------------------------------------------------
test('human_gate 步骤一律转交 env.humanGate(验证码选择器也不受限)', async () => {
  const { env, calls } = recordingEnv();
  const step = {
    action: 'human_gate',
    selector: '#report-captcha',
    label: '请在浏览器输入验证码,然后回到终端按回车提交',
  };
  await executeStep(step, env, { index: 9 });
  assert.deepEqual(calls, [['human_gate', '请在浏览器输入验证码,然后回到终端按回车提交']]);
});

test('CLI 参数表不含任何跳过/自动确认人工门的参数(红线 2)', () => {
  const keys = Object.keys(CLI_OPTIONS);
  assert.ok(keys.length > 0);
  const banned = /skip|human|gate|auto|confirm|yes|captcha/i;
  const offenders = keys.filter((k) => banned.test(k));
  assert.deepEqual(offenders, [], `CLI 不应提供人工门相关的旁路参数:${offenders}`);
  // 已知参数语义齐全:引擎/输出目录/预览/无头/真实门户开关/插件模块
  assert.deepEqual(
    [...keys].sort(),
    ['allow-real-portal', 'dry-run', 'engine', 'headless', 'out-dir', 'plugin-module'],
  );
});

test('runPlan --dry-run 只是整体预览:不执行任何步骤,submitted 恒为 false', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'netsentinel-driver-test-'));
  try {
    const planPath = path.join(dir, 'plan.json');
    writeFileSync(planPath, JSON.stringify({
      portal: '12377',
      entry_url: 'http://127.0.0.1:8900/',
      payload: { portal: '12377', site_url: 'http://bad.example.com/' },
      steps: [
        { action: 'goto', value: 'http://127.0.0.1:8900/', label: '打开举报入口' },
        { action: 'human_gate', selector: '#report-captcha', label: '人工输入验证码' },
        { action: 'click', selector: '#report-submit', text: '提交举报' },
      ],
    }), 'utf8');
    const result = await runPlan(planPath, { dryRun: true });
    assert.equal(result.ok, true);
    assert.equal(result.submitted, false); // 预览绝不越过人工门"提交"
    assert.ok(result.notes.every((n) => n.startsWith('[dry-run]') || !n.startsWith('[')));
    // 非法计划文件:中文报错
    const badPath = path.join(dir, 'bad.json');
    writeFileSync(badPath, '{"no_steps": true}', 'utf8');
    await assert.rejects(() => runPlan(badPath, { dryRun: true }), /计划文件格式不正确/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// CLI 兼容:命令行行为保持
// ---------------------------------------------------------------------------
test('CLI:不带参数打印中文用法并以退出码 2 结束(命令行入口保持)', () => {
  const proc = spawnSync(process.execPath, [DRIVER_PATH], { encoding: 'utf8' });
  assert.equal(proc.status, 2);
  assert.match(proc.stderr, /用法: node drivers\/driver\.mjs <plan\.json>/);
});

test('CLI:作为模块导入时不触发命令行主流程(main 仅直跑执行)', async () => {
  // 重复以模块身份导入(与测试顶部 import 同一实例),若误触发 main 会
  // 打印用法并以退出码 2 结束进程,本测试根本到不了 assert。
  const mod = await import(pathToFileURL(DRIVER_PATH).href);
  assert.equal(typeof mod.runPlan, 'function');
  assert.equal(typeof mod.guardUrl, 'function');
  assert.equal(mod.CLI_OPTIONS.engine.default, 'console');
});
