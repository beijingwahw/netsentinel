# -*- coding: utf-8 -*-
"""本地模拟举报门户静态文件服务(NetSentinel A16)。

用法::

    python serve.py [port]

- 服务本脚本所在目录(即 ``tests/mock_portals``),只监听 ``127.0.0.1``;
- ``port`` 缺省 ``0`` 表示由操作系统随机分配端口;
- 启动后向 stdout 打印实际端口(第一行,已 flush,便于脚本读取),
  第二行打印人类可读的访问提示;
- ``Ctrl+C`` 优雅退出。

安全红线:这是开发期唯一允许的"举报门户",仅限本地离线测试,
绝不要把它当成真实举报站点的代理或镜像。
"""
from __future__ import annotations

import functools
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

#: 只绑定本机回环地址,避免局域网暴露
HOST = "127.0.0.1"


def make_server(port: int = 0, host: str = HOST) -> ThreadingHTTPServer:
    """创建服务本脚本所在目录的 HTTP 服务(不启动事件循环)。

    :param port: 监听端口,``0`` 表示随机分配。
    :param host: 监听地址,默认 ``127.0.0.1``。
    """
    directory = os.path.dirname(os.path.abspath(__file__))
    handler = functools.partial(SimpleHTTPRequestHandler, directory=directory)
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    port = 0
    if argv:
        try:
            port = int(argv[0])
        except ValueError:
            print(f"端口参数无效:{argv[0]!r}(应为整数,缺省 0 = 随机端口)", file=sys.stderr)
            return 2
        if not 0 <= port <= 65535:
            print(f"端口超出范围:{port}(应在 0-65535)", file=sys.stderr)
            return 2

    try:
        httpd = make_server(port)
    except OSError as exc:
        print(f"无法监听 {HOST}:{port} -> {exc}", file=sys.stderr)
        return 1

    actual_port = httpd.server_address[1]
    # 第一行:机器可读的实际端口(已 flush)
    print(actual_port, flush=True)
    print(
        f"本地模拟举报门户已启动: http://{HOST}:{actual_port}/ "
        f"(目录 {os.path.dirname(os.path.abspath(__file__))};Ctrl+C 退出)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C,本地模拟门户已停止。", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
