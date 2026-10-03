"""净网哨兵 V8 · A146 连接向导/切换器单文件页面(``netsentinel.setup.page``)。

- ``page_html() -> str``:纯函数,返回**单文件中文 HTML**(<!doctype>/utf-8/
  内联 CSS/JS,无外部资源、无框架、无任何 src/href 外链),供 A145 服务器
  (GET ``/``)与 A156/A157 渲染注入复用;本模块只产出模板,不读环境、
  不落盘、不接收任何用户输入;
- 云平台下拉内嵌 20 家提供方选项:调用时**惰性**读取兄弟模块
  ``netsentinel.vision.providers.PROVIDERS`` 的键(兄弟模块只读),模块缺席
  时回退内置静态清单 :data:`BUILTIN_PROVIDER_KEYS`(两者内容一致,测试互证);
- 红线对齐(CONTRACTS-V8.md §0):
  - 红线 33(密钥只进不显):密钥输入 ``type=password`` 且
    ``autocomplete="new-password"``;内联 JS 保存密钥后**立即清空输入框**,
    界面只显示服务端返回的掩码,任何 DOM 更新都不回显密钥本体;
  - 红线 34(离线桩明示):离线桩按钮旁固定红字 :data:`STUB_NOTE_TEXT`
    ("离线桩,非模型判定"),启用 stub 后警示区再次明示;
  - 红线 32(文案侧):页面声明本机扫描仅限回环端口,页面自身不发起任何
    外网请求(fetch 仅相对路径的 /api/*);
- 模板层安全:下拉选项值经 ``html.escape`` 转义;页面全部动态内容
  (状态/扫描行/掩码/警示)由内联 JS 经 ``textContent`` 写入,不拼接 HTML。
"""
from __future__ import annotations

import html as _html

__all__ = ["BUILTIN_PROVIDER_KEYS", "STUB_NOTE_TEXT", "page_html"]

#: 红线 34 固定声明文案(与 CONTRACTS-V8.md §0 逐字一致)。
STUB_NOTE_TEXT = "离线桩,非模型判定"

#: 内置提供方静态清单(与 ``netsentinel.vision.providers.PROVIDERS`` 键一致,
#: 供该模块缺席时兜底;顺序即目录顺序)。
BUILTIN_PROVIDER_KEYS: tuple[str, ...] = (
    "glm", "openai", "anthropic", "gemini", "qwen", "doubao", "hunyuan",
    "moonshot", "minimax", "stepfun", "siliconflow", "ernie", "openrouter",
    "groq", "together", "xai", "ollama", "vllm", "lmstudio", "xinference",
)

#: 下拉显示名(键缺席时直接显示键本身,仍经转义;本地提供方标注"本机")。
_PROVIDER_LABELS: dict[str, str] = {
    "glm": "智谱 GLM(项目默认)",
    "openai": "OpenAI · GPT-4o",
    "anthropic": "Anthropic · Claude",
    "gemini": "Google · Gemini",
    "qwen": "阿里 · 通义千问 VL",
    "doubao": "字节 · 豆包视觉(火山方舟)",
    "hunyuan": "腾讯 · 混元视觉",
    "moonshot": "月之暗面 · Kimi 视觉",
    "minimax": "MiniMax 视觉",
    "stepfun": "阶跃星辰 · Step-1V",
    "siliconflow": "硅基流动(聚合平台)",
    "ernie": "百度 · 文心 ERNIE VL",
    "openrouter": "OpenRouter(聚合入口)",
    "groq": "Groq · Llama 4",
    "together": "Together AI",
    "xai": "xAI · Grok 视觉",
    "ollama": "Ollama(本机 · 免密钥)",
    "vllm": "vLLM(本机 · 免密钥)",
    "lmstudio": "LM Studio(本机 · 免密钥)",
    "xinference": "Xinference(本机 · 免密钥)",
}

#: 选项占位标记(仅出现一次,渲染时替换为 20 家选项)。
_OPTIONS_MARKER = "__NETSENTINEL_CLOUD_OPTIONS__"


def _provider_keys() -> tuple[str, ...]:
    """惰性读取提供方目录键;兄弟模块缺席或异常时回退内置静态清单。"""
    try:
        from netsentinel.vision.providers import PROVIDERS
        keys = tuple(str(k) for k in PROVIDERS.keys())
    except Exception:
        return BUILTIN_PROVIDER_KEYS
    return keys or BUILTIN_PROVIDER_KEYS


def _options_html(keys: tuple[str, ...]) -> str:
    """渲染下拉选项(键与显示名均经 ``html.escape`` 转义)。"""
    parts: list[str] = []
    for key in keys:
        safe_key = _html.escape(key, quote=True)
        safe_label = _html.escape(_PROVIDER_LABELS.get(key, key), quote=True)
        parts.append('<option value="' + safe_key + '">' + safe_label + "</option>")
    return "\n".join(parts)


_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>净网哨兵 · 视觉模型连接向导/切换器</title>
<style>
  :root {
    --ink: #1c2733;
    --muted: #5b6b7b;
    --line: #d8dfe6;
    --bg: #f2f5f8;
    --card: #ffffff;
    --brand: #0f6b4f;
    --brand-ink: #ffffff;
    --danger: #c62828;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    line-height: 1.65;
  }
  header { background: var(--brand); color: var(--brand-ink); padding: 18px 24px; }
  header h1 { margin: 0; font-size: 20px; letter-spacing: 1px; }
  header p { margin: 6px 0 0; font-size: 13px; opacity: 0.88; }
  main { max-width: 880px; margin: 0 auto; padding: 16px; }
  .banner {
    margin: 16px 0;
    padding: 12px 16px;
    border: 1px solid var(--brand);
    border-left: 6px solid var(--brand);
    border-radius: 8px;
    background: #eaf4f0;
    font-weight: 700;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 18px;
    margin: 16px 0;
  }
  .card h2 { margin: 0 0 8px; font-size: 16px; }
  .hint { color: var(--muted); font-size: 13px; margin: 6px 0; }
  .kv { display: grid; grid-template-columns: 96px 1fr; gap: 4px 12px; margin: 8px 0 0; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; font-weight: 600; word-break: break-all; }
  .btn {
    display: inline-block;
    padding: 7px 16px;
    border: 1px solid var(--brand);
    border-radius: 6px;
    background: var(--brand);
    color: var(--brand-ink);
    font-size: 14px;
    cursor: pointer;
  }
  .btn:hover { opacity: 0.9; }
  .btn.ghost { background: transparent; color: var(--danger); border-color: var(--danger); }
  .list { margin-top: 10px; }
  .row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 8px 10px;
    border: 1px dashed var(--line);
    border-radius: 8px;
    margin: 6px 0;
  }
  .pill {
    padding: 2px 10px;
    border-radius: 999px;
    background: #eaf4f0;
    color: var(--brand);
    font-size: 12px;
    font-weight: 700;
  }
  .model { font-weight: 600; }
  .muted { color: var(--muted); font-size: 12px; }
  .field { display: block; margin: 10px 0; font-size: 14px; }
  .field select, .field input {
    display: block;
    width: 100%;
    max-width: 440px;
    margin-top: 4px;
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    font-size: 14px;
  }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .masked { color: var(--muted); font-size: 13px; }
  .stub-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .stub-note { color: #c62828; font-weight: 700; }
  .warnbox {
    margin: 16px 0;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid var(--danger);
    background: #fdeeee;
    color: var(--danger);
    font-size: 14px;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .warnbox:empty { display: none; }
  footer { text-align: center; color: var(--muted); font-size: 12px; margin: 24px 0 8px; }
</style>
</head>
<body>
<header>
  <h1>净网哨兵 · 视觉模型连接向导/切换器</h1>
  <p>首次运行将自动接管;本页同时是手动切换入口之一(向导页 / 命令行 / REST),所有操作仅在本机完成。</p>
</header>
<main>
  <div class="banner" data-testid="active-banner" id="active-banner">尚未连接视觉模型</div>

  <section class="card" data-testid="status-card" aria-label="当前状态">
    <h2>当前状态</h2>
    <dl class="kv">
      <dt>活动模型</dt><dd data-testid="status-active">未设置</dd>
      <dt>切换来源</dt><dd data-testid="status-source">-</dd>
      <dt>切换时间</dt><dd data-testid="status-time">-</dd>
    </dl>
    <p class="hint">活动模型持久化在 data/model_runtime.json;来源为 takeover / wizard / cli / rest 之一。</p>
  </section>

  <section class="card" aria-label="本机服务">
    <h2>本机服务</h2>
    <p class="hint">仅探测本机回环地址(127.0.0.1)上预置的端口,绝不扫描外网;本地提供方免密钥、免联网开关。</p>
    <button type="button" class="btn" data-testid="btn-probe" id="btn-probe">扫描本机服务</button>
    <div class="list" data-testid="local-list" id="local-list">
      <template id="local-row-tpl">
        <div class="row" data-testid="local-row">
          <span class="pill" data-testid="row-provider">提供方</span>
          <span class="model" data-testid="row-model">模型名</span>
          <span class="muted" data-testid="row-base">回环地址</span>
          <button type="button" class="btn" data-testid="btn-enable">启用</button>
        </div>
      </template>
    </div>
  </section>

  <section class="card" aria-label="云平台">
    <h2>云平台</h2>
    <p class="hint">云端提供方需 vlm_online 开启且密钥已保存才会真实调用,并受视觉预算约束;本地提供方豁免。</p>
    <label class="field">云平台
      <select data-testid="cloud-select" id="cloud-select">
__NETSENTINEL_CLOUD_OPTIONS__
      </select>
    </label>
    <label class="field">API 密钥(只进不显)
      <input type="password" data-testid="key-input" id="key-input" autocomplete="new-password" spellcheck="false" placeholder="粘贴密钥;保存后输入框清空,仅显示掩码">
    </label>
    <div class="actions">
      <button type="button" class="btn" data-testid="btn-setkey" id="btn-setkey">保存密钥</button>
      <button type="button" class="btn" data-testid="btn-test" id="btn-test">测试连接</button>
    </div>
    <p class="masked" data-testid="key-masked">密钥尚未保存</p>
  </section>

  <section class="card" aria-label="离线兜底">
    <h2>离线兜底</h2>
    <div class="stub-line">
      <button type="button" class="btn ghost" data-testid="btn-stub" id="btn-stub">使用离线桩继续</button>
      <span class="stub-note">离线桩,非模型判定</span>
    </div>
    <p class="hint">没有任何可用模型时的兜底:流程可继续,但结论不来自视觉模型,产出会明确标注离线桩来源。</p>
  </section>

  <div class="warnbox" data-testid="warn-area" id="warn-area" aria-live="polite"></div>
</main>
<footer>纪律:本机探测仅限回环端口;密钥只进不显,任何回显均为掩码;离线兜底必须明示"离线桩,非模型判定"。</footer>
<noscript><p>本页需要启用 JavaScript 才能扫描本机服务、保存密钥与切换模型。</p></noscript>
<script>
"use strict";
(function () {
  function q(root, tid) {
    return root.querySelector('[data-testid="' + tid + '"]');
  }
  function qAll(root, tid) {
    return Array.prototype.slice.call(root.querySelectorAll('[data-testid="' + tid + '"]'));
  }
  function byTestid(tid) { return q(document, tid); }

  var banner = byTestid("active-banner");
  var statusActive = byTestid("status-active");
  var statusSource = byTestid("status-source");
  var statusTime = byTestid("status-time");
  var localList = byTestid("local-list");
  var rowTpl = document.getElementById("local-row-tpl");
  var cloudSelect = byTestid("cloud-select");
  var keyInput = byTestid("key-input");
  var keyMasked = byTestid("key-masked");
  var warnArea = byTestid("warn-area");

  function say(text) {
    if (warnArea) { warnArea.textContent = text || ""; }
  }
  function setField(el, text) {
    if (el) { el.textContent = (text === null || text === undefined) ? "" : String(text); }
  }
  function errMsg(err) {
    return (err && err.message) ? String(err.message) : "网络错误(请确认向导服务仍在运行)";
  }
  function getJson(url) {
    return fetch(url).then(function (resp) {
      return resp.json().catch(function () { return null; });
    });
  }
  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (resp) {
      return resp.json().catch(function () { return null; });
    });
  }

  function renderStatus(data) {
    if (!data || typeof data !== "object") { return false; }
    var active = data.active || data.spec || "";
    if (active) {
      setField(banner, "当前活动模型:" + active + "(可在下方继续切换)");
    } else {
      setField(banner, "尚未连接视觉模型");
    }
    setField(statusActive, active ? active : "未设置");
    setField(statusSource, data.switched_by ? String(data.switched_by) : "-");
    setField(statusTime, data.switched_at ? String(data.switched_at) : "-");
    return true;
  }

  function loadStatus() {
    getJson("/api/status").then(function (data) {
      if (!renderStatus(data)) {
        say("状态接口返回异常(无法解析 JSON),请确认向导服务正常。");
      }
    }, function (err) {
      say("获取状态失败:" + errMsg(err));
    });
  }

  function renderLocal(services) {
    if (!localList) { return; }
    qAll(localList, "local-row").forEach(function (node) { node.remove(); });
    qAll(localList, "local-empty").forEach(function (node) { node.remove(); });
    var found = [];
    if (Array.isArray(services)) {
      services.forEach(function (svc) {
        if (!svc || svc.ok === false) { return; }
        var provider = svc.provider ? String(svc.provider) : "未知提供方";
        var base = svc.base_url ? String(svc.base_url) : "";
        var models = Array.isArray(svc.models) ? svc.models : [];
        models.forEach(function (item) {
          var name = (typeof item === "string") ? item : (item && item.id ? String(item.id) : "");
          if (!name) { return; }
          found.push({ provider: provider, base: base, model: name });
        });
      });
    }
    if (!found.length) {
      var tip = document.createElement("p");
      tip.setAttribute("data-testid", "local-empty");
      tip.className = "hint";
      tip.textContent = "未发现本机视觉服务:请先启动 Ollama / LM Studio / vLLM / Xinference 等本地推理服务,再点击\u201c扫描本机服务\u201d。";
      localList.appendChild(tip);
      return;
    }
    if (!rowTpl) { return; }
    found.forEach(function (item) {
      var node = rowTpl.content.firstElementChild.cloneNode(true);
      setField(q(node, "row-provider"), item.provider);
      setField(q(node, "row-model"), item.model);
      setField(q(node, "row-base"), item.base);
      var enableBtn = q(node, "btn-enable");
      if (enableBtn) {
        enableBtn.addEventListener("click", function () {
          activate(item.provider + ":" + item.model);
        });
      }
      localList.appendChild(node);
    });
  }

  function activate(spec) {
    say("正在切换模型…");
    postJson("/api/activate", { spec: spec }).then(function (data) {
      if (!data || typeof data !== "object") {
        say("切换失败:接口返回异常(无法解析 JSON)。");
        return;
      }
      if (data.ok === false || data.error) {
        say("切换失败:" + String(data.error || "未知错误"));
        return;
      }
      renderStatus(data.status || data);
      if (spec === "stub") {
        say("已启用离线桩。注意:离线桩,非模型判定——结论不来自视觉模型,产出会明确标注。");
      } else {
        say("已切换到:" + spec);
      }
    }, function (err) {
      say("切换失败:" + errMsg(err));
    });
  }

  function saveKey() {
    var provider = cloudSelect ? String(cloudSelect.value || "") : "";
    var raw = keyInput ? String(keyInput.value || "") : "";
    if (!provider) {
      say("请先在云平台下拉框中选择提供方。");
      return;
    }
    var key = raw.trim();
    if (key.length < 8) {
      say("密钥长度不足(至少 8 位)。密钥只进不显,请重新输入。");
      return;
    }
    say("正在保存密钥(保存后输入框将清空,页面只保留掩码)…");
    postJson("/api/setkey", { provider: provider, key: key }).then(function (data) {
      // 红线 33:密钥只进不显——无论成败都先清空输入框,界面只回显掩码。
      if (keyInput) { keyInput.value = ""; }
      if (!data || typeof data !== "object") {
        say("保存密钥失败:接口返回异常(无法解析 JSON)。");
        return;
      }
      if (data.ok === false || data.error) {
        say("保存密钥失败:" + String(data.error || "未知错误"));
        return;
      }
      var masked = data.masked ? String(data.masked) : "已保存(服务端未返回掩码)";
      setField(keyMasked, "密钥已保存,掩码:" + masked);
      say("");
      loadStatus();
    }, function (err) {
      if (keyInput) { keyInput.value = ""; }
      say("保存密钥失败:" + errMsg(err));
    });
  }

  function testConn() {
    var provider = cloudSelect ? String(cloudSelect.value || "") : "";
    if (!provider) {
      say("请先在云平台下拉框中选择提供方。");
      return;
    }
    say("正在测试连接(云端仅在 vlm_online 开启且密钥已保存时才真实外呼)…");
    postJson("/api/test", { spec: provider }).then(function (data) {
      if (!data || typeof data !== "object") {
        say("测试连接失败:接口返回异常(无法解析 JSON)。");
        return;
      }
      if (data.ok) {
        var parts = ["测试连接成功"];
        if (data.model) { parts.push("模型 " + String(data.model)); }
        if (typeof data.latency_ms === "number") { parts.push("延迟 " + data.latency_ms + " 毫秒"); }
        say(parts.join(","));
      } else {
        say("测试连接失败:" + String(data.error || "未知错误"));
      }
    }, function (err) {
      say("测试连接失败:" + errMsg(err));
    });
  }

  function mount() {
    var btnProbe = byTestid("btn-probe");
    var btnSetKey = byTestid("btn-setkey");
    var btnTest = byTestid("btn-test");
    var btnStub = byTestid("btn-stub");
    if (btnProbe) {
      btnProbe.addEventListener("click", function () {
        say("正在扫描本机服务(仅回环端口,绝不外扫)…");
        postJson("/api/probe", {}).then(function (data) {
          var list = [];
          if (Array.isArray(data)) {
            list = data;
          } else if (data && Array.isArray(data.services)) {
            list = data.services;
          } else if (data && data.error) {
            say("扫描失败:" + String(data.error));
            return;
          }
          renderLocal(list);
          if (!list.length) {
            say("扫描完成:未发现本机视觉服务。");
          } else {
            say("扫描完成:发现 " + list.length + " 个本机服务,请在上方列表中逐项启用。");
          }
        }, function (err) {
          say("扫描失败:" + errMsg(err));
        });
      });
    }
    if (btnSetKey) { btnSetKey.addEventListener("click", saveKey); }
    if (btnTest) { btnTest.addEventListener("click", testConn); }
    if (btnStub) { btnStub.addEventListener("click", function () { activate("stub"); }); }
    if (keyInput) {
      keyInput.addEventListener("keydown", function (ev) {
        if (ev && ev.key === "Enter") { saveKey(); }
      });
    }
    loadStatus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
</script>
</body>
</html>
"""


def page_html() -> str:
    """返回向导/切换器单文件 HTML(纯函数:无参数、确定性、不落盘)。"""
    return _PAGE_TEMPLATE.replace(_OPTIONS_MARKER, _options_html(_provider_keys()))
