"""scripts/start_services.py 纯逻辑测试(V10.27,离线,不启动任何进程)。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "start_services", Path(__file__).resolve().parents[1] / "scripts" / "start_services.py"
)
ss = importlib.util.module_from_spec(_spec)
sys.modules["start_services"] = ss
_spec.loader.exec_module(ss)


def test_port_open_false_for_closed():
    """回环未监听端口探测为 False(随机高位端口,不启动任何进程)。"""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # 立即释放,该端口几乎必然关闭
    assert ss.port_open(port, timeout=0.2) is False


def test_port_open_true_for_listening():
    """临时监听中的端口探测为 True。"""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert ss.port_open(port, timeout=0.5) is True
    finally:
        srv.close()


def test_service_table_and_ports():
    """核心五服务端口互不冲突,argv 以项目根为 cwd,入口为助手页。"""
    core = ss._SERVICES_CORE
    ports = [p for _n, p, _a, _c in core]
    assert len(ports) == len(set(ports)) == 5
    assert (8765, 8500, 8501, 8502, 8503) == tuple(ports)
    for _n, _p, argv, cwd in core:
        assert argv[0] == sys.executable and cwd.name == "netsentinel"
    demo_ports = [p for _n, p, _a, _c in ss._SERVICES_DEMO]
    assert demo_ports == [8899]


def test_main_status_readonly(capsys):
    """status 只读体检:打印四行状态,退出码 0。"""
    assert ss.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "8765" in out and "8500" in out and "只读" in out
