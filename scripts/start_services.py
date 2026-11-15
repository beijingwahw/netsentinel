"""净网助手后台服务一键保活(V10.27)。

背景:REST 与三个 Web 页面是"净网助手"形态的骨架,跨会话/重启后进程会
掉线——本脚本一条命令把全部后台服务拉起(已在线的自动跳过),并提供
status 只读体检。

服务清单(全部回环,无鉴权仅本机):
- REST 服务      127.0.0.1:8765  python -m service.app
- 净网助手页     127.0.0.1:8500  streamlit run webui/assistant_page.py
- 人工复核台     127.0.0.1:8501  streamlit run webui/app.py
- 关键词工作台   127.0.0.1:8502  streamlit run webui/keywords_page.py
- 演示站点(可选)127.0.0.1:8899  http.server(伺服 tests/fixtures/demo_site)

用法:
    python scripts/start_services.py            # 拉起四个常驻服务(缺谁补谁)
    python scripts/start_services.py --with-demo  # 连演示站点一起(开发/演示)
    python scripts/start_services.py status     # 只读体检(不启动任何进程)

红线:全部绑定回环、不弹任何窗口(CREATE_NO_WINDOW)、不提交任何举报;
本脚本只做进程保活,业务操作仍走净网助手页面/CLI 人工门。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["SERVICES", "port_open", "main"]

#: 常驻服务清单:(名称, 端口, argv 工厂, 工作目录)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SERVICES_CORE: list[tuple[str, int, list[str], Path]] = [
    ("REST 服务", 8765, [sys.executable, "-m", "service.app"], _PROJECT_ROOT),
    ("净网助手页", 8500,
     [sys.executable, "-m", "streamlit", "run", "webui/assistant_page.py",
      "--server.headless", "true", "--server.port", "8500",
      "--browser.gatherUsageStats", "false"], _PROJECT_ROOT),
    ("人工复核台", 8501,
     [sys.executable, "-m", "streamlit", "run", "webui/app.py",
      "--server.headless", "true", "--server.port", "8501",
      "--browser.gatherUsageStats", "false"], _PROJECT_ROOT),
    ("关键词工作台", 8502,
     [sys.executable, "-m", "streamlit", "run", "webui/keywords_page.py",
      "--server.headless", "true", "--server.port", "8502",
      "--browser.gatherUsageStats", "false"], _PROJECT_ROOT),
    ("标注工作台", 8503,
     [sys.executable, "-m", "streamlit", "run", "webui/label_page.py",
      "--server.headless", "true", "--server.port", "8503",
      "--browser.gatherUsageStats", "false"], _PROJECT_ROOT),
]

_SERVICES_DEMO: list[tuple[str, int, list[str], Path]] = [
    ("演示站点", 8899,
     [sys.executable, "-m", "http.server", "8899", "--bind", "127.0.0.1"],
     _PROJECT_ROOT / "tests" / "fixtures" / "demo_site"),
]

#: Windows 无窗口子进程标志(POSIX 无此概念,置 0)。
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 启动后端口就绪等待(秒)。
_READY_WAIT_S = 20.0


def port_open(port: int, *, timeout: float = 1.0) -> bool:
    """端口是否有服务在听(回环探测,只连接不发数据)。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_one(name: str, port: int, argv: list[str], cwd: Path) -> bool:
    """拉起单个服务并等端口就绪;返回是否由本次启动。"""
    if port_open(port):
        print(f"  ✅ {name}(:{port}) 已在线,跳过")
        return False
    print(f"  ⏳ 正在启动 {name}(:{port})…")
    subprocess.Popen(
        argv,
        cwd=str(cwd),
        creationflags=_CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + _READY_WAIT_S
    while time.monotonic() < deadline:
        if port_open(port):
            print(f"  ✅ {name}(:{port}) 已就绪")
            return True
        time.sleep(0.5)
    print(f"  ❌ {name}(:{port}) {int(_READY_WAIT_S)} 秒内未就绪(查看该服务日志排查)")
    return False


def main(argv: list[str] | None = None) -> int:
    """CLI 入口:缺谁补谁地拉起后台服务;status 只读体检。"""
    parser = argparse.ArgumentParser(
        prog="python scripts/start_services.py",
        description="净网助手后台服务一键保活(全部回环,无窗口,不提交任何举报)",
    )
    parser.add_argument("action", nargs="?", default="start", choices=["start", "status"],
                        help="start=缺谁补谁(缺省);status=只读体检")
    parser.add_argument("--with-demo", action="store_true",
                        help="连同本地演示站点(8899)一起,开发/演示用")
    args = parser.parse_args(argv)

    services = list(_SERVICES_CORE) + (_SERVICES_DEMO if args.with_demo else [])
    if args.action == "status":
        print("净网助手后台服务体检(只读):")
        for name, port, _argv, _cwd in services:
            state = "🟢 在线" if port_open(port) else "⚪ 离线"
            print(f"  {state}  {name}  http://127.0.0.1:{port}")
        return 0

    print("净网助手后台服务保活(已在线自动跳过,无窗口启动):")
    started = sum(1 for item in services if _start_one(*item))
    print(f"完成:本次新启动 {started} 个,入口 → 净网助手 http://127.0.0.1:8500")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
