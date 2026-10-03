"""NetSentinel 真实环境接入(golive)——上线就绪验证与生产准备。

**红线 38(本轮新增)**:真实接入 ≠ 自主运行。

- 本模块只做**只读连通性验证**与配置体检,绝不产生任何真实举报;
- 门户检查仅 GET 首页取状态码(取样 ≤512B),不解析表单、不提交、不留会话;
- 密钥、扫描目标清单、逐组人工声明、每条举报的验证码与最终提交,
  **永远属于运营者**——:func:`human_only` 输出的不是待办,是设计。

用法::

    python -m netsentinel.golive check            # 离线体检(配置/本地服务/浏览器)
    python -m netsentinel.golive check --net      # 追加真实外网只读探活
    python -m netsentinel.golive prepare          # 生成 config.production.yaml 模板
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from netsentinel import telemetry
from netsentinel.contracts import Config

__all__ = ["CheckResult", "run_checks", "summarize", "human_only", "prepare", "main"]

logger = logging.getLogger(__name__)

#: 真实只读探活端点(名称→URL;全部为公开首页/API 根,GET 即止)
NET_PROBES: dict[str, str] = {
    "通用外网出口": "https://pypi.org/simple/",
    "智谱 GLM API": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "Yandex XML 端点": "https://yandex.com/search/xml",
    "12377 举报中心(只读)": "https://www.12377.cn/",
    "扫黄打非网(只读)": "https://www.shdf.gov.cn/",
    "OpenAI API": "https://api.openai.com/v1/models",
}

#: 预期"可达但需鉴权"的状态码(端点活着)
_REACHABLE_AUTH = {401, 403, 405, 521}

_SAMPLE_BYTES = 512
_NET_TIMEOUT_S = 10.0


@dataclass
class CheckResult:
    """一项检查结果。status ∈ ok(就绪)/ gated(运营者提供后可用)/ fail(阻塞)/ skip(跳过)。"""

    name: str
    status: str
    detail: str = ""
    ms: int = 0
    extra: dict = field(default_factory=dict)


def _http_probe(url: str, timeout: float = _NET_TIMEOUT_S) -> tuple[int, int]:
    """GET 只读取样:返回 (status, ms);任何 HTTP 应答都算端点可达。"""
    start = time.perf_counter()
    req = urllib.request.Request(
        url, headers={"User-Agent": "NetSentinel-GoLive/0.9 (read-only check)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(_SAMPLE_BYTES)
            return int(resp.status), int((time.perf_counter() - start) * 1000)
    except urllib.error.HTTPError as exc:
        exc.read(_SAMPLE_BYTES)
        return int(exc.code), int((time.perf_counter() - start) * 1000)


def _check_net(transport: Callable[[str], tuple[int, int]] | None = None) -> list[CheckResult]:
    out: list[CheckResult] = []
    probe = transport or _http_probe
    for name, url in NET_PROBES.items():
        try:
            status, ms = probe(url)
        except Exception as exc:  # noqa: BLE001 - 网络失败即 fail,不抛
            out.append(CheckResult(name, "fail", f"{type(exc).__name__}: {str(exc)[:60]}"))
            telemetry.inc("golive.net_fail")
            continue
        ok = status < 400 or status in _REACHABLE_AUTH
        out.append(
            CheckResult(
                name,
                "ok" if ok else "fail",
                f"HTTP {status}" + ("(端点可达,鉴权/防护预期内)" if status >= 400 else "(可达)"),
                ms,
            )
        )
        telemetry.inc("golive.net_ok" if ok else "golive.net_fail")
    return out


def _check_config(cfg: Config) -> list[CheckResult]:
    out: list[CheckResult] = []
    out.append(
        CheckResult(
            "Python 版本",
            "ok" if (sys_ver := _py_version()) >= (3, 10) else "fail",
            f"{sys_ver[0]}.{sys_ver[1]}",
        )
    )
    switches = []
    if cfg.allow_network:
        switches.append("allow_network=true(真实抓取)")
    if cfg.vlm_online:
        switches.append("vlm_online=true(图像将出本机)")
    if cfg.discovery_online:
        switches.append("discovery_online=true(真实搜索)")
    if not cfg.dry_run_default:
        switches.append("dry_run_default=false(执行器将驱动真实浏览器)")
    out.append(
        CheckResult(
            "生产开关",
            "gated" if switches else "ok",
            (";".join(switches) + " —— 已开启的真实模式开关,请逐一确认") if switches
            else "全部安全默认(离线/干跑);接入真实环境请见 config.production.yaml 模板",
        )
    )
    # 密钥体检(只读布尔,绝不回显)
    try:
        from netsentinel.security import keys

        configured = {p: v for p, v in keys.configured(cfg).items() if v}
        out.append(
            CheckResult(
                "云平台密钥",
                "ok" if configured else "gated",
                f"已配置 {len(configured)} 家"
                + (f":{','.join(sorted(configured)[:6])}" if configured else "(向导/CLI/密钥环三选一配置)"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        out.append(CheckResult("云平台密钥", "skip", f"密钥环未就位:{exc}"))
    # 活动模型
    try:
        from netsentinel.vision.model_manager import ModelManager

        active = ModelManager(cfg.model_runtime_path).get_active()
        out.append(
            CheckResult(
                "活动视觉模型",
                "ok" if active and active != "stub" else ("gated" if not active else "gated"),
                active or "未设置(运行 takeover 或向导连接)",
                extra={"spec": active},
            )
        )
    except Exception as exc:  # noqa: BLE001
        out.append(CheckResult("活动视觉模型", "skip", str(exc)[:80]))
    return out


def _py_version() -> tuple[int, int]:
    import sys

    return sys.version_info[0], sys.version_info[1]


def _check_local_services(cfg: Config) -> CheckResult:
    try:
        from netsentinel.vision.local_probe import LocalVisionScanner

        rows = LocalVisionScanner(list(cfg.local_probe_ports), timeout=1.0).scan()
        hit = [r for r in rows if r.get("ok") and r.get("models")]
        if hit:
            models = ",".join(m for r in hit for m in (r.get("models") or [])[:3])
            return CheckResult("本地视觉服务", "ok", f"{len(hit)} 个服务在线,模型示例:{models[:60]}")
        return CheckResult(
            "本地视觉服务", "gated", "本机 11434/1234/8000/9997 未发现在线服务(Ollama pull llava 后自动接管)"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("本地视觉服务", "skip", str(exc)[:80])


def _check_browser() -> CheckResult:
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True)
            browser.close()
            return CheckResult("chromium 浏览器", "ok", "可启动(真实执行器就绪)")
        finally:
            pw.stop()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "chromium 浏览器", "gated", f"不可用:{str(exc)[:60]}(python -m playwright install chromium)"
        )


def _check_optional_deps() -> list[CheckResult]:
    out = []
    for mod, purpose in (
        ("playwright", "真实浏览器采集/执行"),
        ("yaml", "配置/清单"),
        ("PIL", "图像处理"),
        ("fastapi", "REST 服务"),
        ("streamlit", "复核台 UI"),
        ("nudenet", "本地视觉模型"),
        ("psutil", "CPU 画像增强"),
    ):
        found = importlib.util.find_spec(mod) is not None
        out.append(
            CheckResult(
                f"可选依赖 {mod}",
                "ok" if found else ("skip" if mod in ("nudenet", "psutil", "streamlit") else "gated"),
                purpose + ("" if found else "(未安装)"),
            )
        )
    return out


def _check_storage(cfg: Config) -> CheckResult:
    try:
        d = Path(cfg.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".golive_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("数据目录可写", "ok", str(d))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("数据目录可写", "fail", str(exc)[:80])


def run_checks(
    cfg: Config, *, net: bool = False, transport: Callable[[str], tuple[int, int]] | None = None
) -> list[CheckResult]:
    """执行就绪检查。net=True 时追加真实外网只读探活(红线 38:GET 首页即止)。"""
    results: list[CheckResult] = []
    results += _check_config(cfg)
    results.append(_check_local_services(cfg))
    results.append(_check_browser())
    results += _check_optional_deps()
    results.append(_check_storage(cfg))
    if net:
        results += _check_net(transport)
    telemetry.inc("golive.check")
    return results


def summarize(results: list[CheckResult]) -> dict:
    """归类:ready(就绪)/ gated(待运营者)/ fail(阻塞)/ skip(可选缺失)。"""
    buckets: dict[str, list[str]] = {"ready": [], "gated": [], "fail": [], "skip": []}
    for r in results:
        key = {"ok": "ready", "gated": "gated", "fail": "fail", "skip": "skip"}[r.status]
        buckets[key].append(f"{r.name}:{r.detail}")
    return buckets


def human_only(cfg: Config) -> list[str]:
    """按设计永远保留给运营者的环节(红线 38)——不是待办清单,是系统边界。"""
    return [
        "云平台密钥的获取与保管(向导/CLI/密钥环写入)",
        "扫描目标清单的提供与授权(线索经发现层产出后仍需人工核实)",
        "逐组『我已逐站人工核实』声明(批量举报前置,留痕审计)",
        "每一条举报的验证码输入与最终提交(执行器 HUMAN_GATE,绝不自动处理)",
        "举报内容的真实性负贵(虚假举报违法,系统只辅助不拍板)",
    ]


PRODUCTION_TEMPLATE = """\
# NetSentinel 生产配置模板(golive prepare 生成)——逐项确认后再使用
# 红线:验证码永远人工;每条举报有人工门;频控不放宽。

# ---- 真实模式开关(接入真实环境)----
allow_network: true          # 真实抓取目标站点(仅限你已授权/已核实的线索目标)
vlm_online: true             # 图像将发送至 glm_base_url(云端视觉模型)
discovery_online: true       # 搜索引擎线索发现(需 Yandex XML 凭据或自托管 SearXNG)
dry_run_default: false       # 执行器将驱动真实浏览器(每条仍停在人工门)

# ---- 视觉模型(三选一)----
classifier: glm              # 或 "ollama:llava"(本地,免 vlm_online)/ openai:gpt-4o-mini
glm_api_key: ""              # 强烈建议改用环境变量 NETSENTINEL_GLM_API_KEY

# ---- V9 并发档位 ----
concurrency_tier: high       # 最大限度压榨本地计算(对外频控不受影响)
cpu_reserve: 1

# ---- 其余保持默认即可 ----
"""


def prepare(out_path: str = "config.production.yaml") -> str:
    """生成生产配置模板;已存在时拒绝覆盖。"""
    target = Path(out_path)
    if target.exists():
        raise FileExistsError(f"已存在,拒绝覆盖:{target}")
    target.write_text(PRODUCTION_TEMPLATE, encoding="utf-8")
    return str(target)


def _render(results: list[CheckResult], cfg: Config) -> str:
    icon = {"ok": "✅", "gated": "⏳", "fail": "❌", "skip": "➖"}
    lines = ["═" * 64, "NetSentinel 真实环境就绪检查(红线 38:只读验证,绝不产生举报)", "═" * 64]
    for r in results:
        ms = f" {r.ms}ms" if r.ms else ""
        lines.append(f"{icon[r.status]} [{r.status:<5}] {r.name:<20} {r.detail}{ms}")
    s = summarize(results)
    lines += [
        "",
        f"就绪 {len(s['ready'])} · 待运营者 {len(s['gated'])} · 阻塞 {len(s['fail'])} · 可选缺失 {len(s['skip'])}",
        "",
        "── 以下环节按设计永远属于运营者(红线 38,不可自动化)──",
    ]
    lines += [f"  · {item}" for item in human_only(cfg)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.golive",
        description="真实环境接入就绪验证(只读;--net 追加外网探活)",
    )
    parser.add_argument("command", choices=["check", "prepare"], help="check=体检;prepare=生成生产配置模板")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--net", action="store_true", help="追加真实外网只读探活(GET 首页,取样512B)")
    parser.add_argument("--json", default=None, help="结果另存 JSON 路径")
    args = parser.parse_args(argv)

    from netsentinel.config import load_config

    if args.command == "prepare":
        try:
            print(f"已生成:{prepare()}")
            print("逐项确认后:cp config.production.yaml config.yaml(或 --config 指定)")
            return 0
        except FileExistsError as exc:
            print(f"错误:{exc}")
            return 1

    cfg = load_config(args.config)
    results = run_checks(cfg, net=args.net)
    print(_render(results, cfg))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"results": [vars(r) for r in results], "human_only": human_only(cfg)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已保存:{args.json}")
    s = summarize(results)
    return 2 if s["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
