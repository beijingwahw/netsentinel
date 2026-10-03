"""A154:netsentinel.setup.daemon 单元测试(离线;注入 fake factory 与 fake 探活)。

被测单例语义(契约 §3 A154):

- 无锁 → 经 factory 启动并回写锁文件(内容=实际端口);
- 锁活(探活 True)→ 直接复用,**零** factory 调用,外来锁不被 stop 清除;
- 锁死 / 无锁 / 坏锁(非整数、越界)→ 重启换锁;
- 端口冲突:从 cfg.setup_port 起 OSError 递增 +1..+5(同一服务对象重试,
  factory 仅一次);6 个全部用尽抛中文 RuntimeError;
- open_browser 三态与 A149 同口径(参数 > NETSENTINEL_NO_BROWSER >
  onboarding_auto_open),裁决结果原样传给 factory(...).start;
- 进程内缓存:二次调用零 factory / 零探活;
- 并发 8 线程同时 ensure → factory 恰好 1 次、start 恰好 1 次、全员同址;
- stop_setup_server 幂等:停本进程服务 + 删自有锁;外来锁不动;再 ensure 可重启;
- 真实 A145 SetupServer 集成一条(importorskip):起 → GET 200 → 复用 → stop。

全部用例离线:探活经 ``monkeypatch.setattr(daemon, "_port_alive", ...)``
替换,服务器经 ``factory=FakeFactory(FakeServer(...))`` 注入。
"""
from __future__ import annotations

import pathlib
import socket
import sys
import threading
import urllib.request
from typing import Any

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config
from netsentinel.setup import daemon


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """每用例隔离:单例状态清零 + 探活降级为"死"(用例按需覆盖)+ 环境清理。"""
    monkeypatch.setattr(daemon, "_server", None)
    monkeypatch.setattr(daemon, "_url", None)
    monkeypatch.setattr(daemon, "_lock_file", None)
    monkeypatch.setattr(daemon, "_owns_lock", False)
    monkeypatch.setattr(daemon, "_port_alive", lambda port: False)
    monkeypatch.delenv(daemon.NO_BROWSER_ENV, raising=False)
    telemetry.reset()


def make_cfg(tmp_path: pathlib.Path, **overrides: Any) -> Config:
    """构造指向 tmp 目录的配置(data_dir / model_runtime 均落临时区)。"""
    kw: dict[str, Any] = {
        "data_dir": str(tmp_path / "data"),
        "model_runtime_path": str(tmp_path / "model_runtime.json"),
    }
    kw.update(overrides)
    return Config(**kw)


def lock_path_of(cfg: Config) -> pathlib.Path:
    """被测口径下的锁文件路径(cfg.data_dir/.setup.lock)。"""
    return pathlib.Path(cfg.data_dir) / daemon.LOCK_FILENAME


def write_lock(cfg: Config, content: str) -> None:
    """预置锁文件(内容任意,供"锁活/锁死/坏锁"场景)。"""
    path = lock_path_of(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class FakeServer:
    """伪向导服务:记录 start/stop;occupied 内端口抛 OSError(占用)。"""

    def __init__(self, occupied: Any = (), start_url: str | None = None) -> None:
        self.occupied = set(occupied)
        self.start_url = start_url  # 非 None 时忽略端口直接返回(测坏返回值)
        self.start_calls: list[tuple[Any, bool]] = []
        self.stop_calls = 0

    def start(self, port: int | None = None, *, open_browser: bool = False) -> str:
        self.start_calls.append((port, open_browser))
        if port in self.occupied:
            raise OSError(98, f"端口 {port} 已被占用")
        if self.start_url is not None:
            return self.start_url
        return f"http://127.0.0.1:{port}/"

    def stop(self) -> None:
        self.stop_calls += 1


class FakeFactory:
    """伪工厂:记录构造调用,返回固定伪服务对象。"""

    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.calls: list[Config] = []

    def __call__(self, cfg: Config) -> FakeServer:
        self.calls.append(cfg)
        return self.server


class FakeProbe:
    """伪探活函数:记录探过的端口;表内端口(或 default=True 时任意)判活。"""

    def __init__(self, alive: Any = (), default: bool = False) -> None:
        self.alive = set(alive)
        self.default = default
        self.calls: list[int] = []

    def __call__(self, port: int) -> bool:
        self.calls.append(port)
        return self.default or port in self.alive


def _free_port() -> int:
    """取一个当前空闲的回环端口(集成测试用,起服务前先释放)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# 无锁启动 / 写锁
# ---------------------------------------------------------------------------


class TestStart:
    def test_no_lock_starts_and_writes_lock(self, tmp_path):
        """无锁 → factory 启动一次、返回回环地址、锁文件写入实际端口。"""
        cfg = make_cfg(tmp_path)  # setup_port 缺省 8766
        server = FakeServer()
        factory = FakeFactory(server)
        url = daemon.ensure_setup_server(cfg, factory=factory, open_browser=False)
        assert url == "http://127.0.0.1:8766/"
        assert factory.calls == [cfg]
        assert server.start_calls == [(8766, False)]
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "8766"
        assert telemetry.snapshot()["counters"].get(daemon.DAEMON_START_METRIC) == 1

    def test_start_uses_cfg_setup_port(self, tmp_path):
        """cfg.setup_port 自定义 → 从该端口起绑,锁内容同步。"""
        cfg = make_cfg(tmp_path, setup_port=9123)
        url = daemon.ensure_setup_server(cfg, factory=FakeFactory(FakeServer()),
                                         open_browser=False)
        assert url == "http://127.0.0.1:9123/"
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "9123"

    def test_missing_data_dir_created(self, tmp_path):
        """data_dir 不存在 → 自动逐级创建,锁落在其下。"""
        cfg = make_cfg(tmp_path, data_dir=str(tmp_path / "deep" / "nested"))
        url = daemon.ensure_setup_server(cfg, factory=FakeFactory(FakeServer()),
                                         open_browser=False)
        assert url is not None
        assert lock_path_of(cfg).exists()

    def test_garbage_lock_treated_as_absent(self, tmp_path, monkeypatch):
        """坏锁(非整数内容)→ 视为无锁直接重启,不探活、覆盖写锁。"""
        cfg = make_cfg(tmp_path, setup_port=8766)
        write_lock(cfg, "not-a-port")
        probe = FakeProbe()
        monkeypatch.setattr(daemon, "_port_alive", probe)
        url = daemon.ensure_setup_server(cfg, factory=FakeFactory(FakeServer()),
                                         open_browser=False)
        assert url == "http://127.0.0.1:8766/"
        assert probe.calls == []  # 坏锁不值得探活
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "8766"

    def test_out_of_range_lock_treated_as_absent(self, tmp_path, monkeypatch):
        """坏锁(端口越界 70000)→ 同无锁,重启换锁。"""
        cfg = make_cfg(tmp_path, setup_port=8766)
        write_lock(cfg, "70000")
        probe = FakeProbe()
        monkeypatch.setattr(daemon, "_port_alive", probe)
        assert daemon.ensure_setup_server(cfg, factory=FakeFactory(FakeServer()),
                                          open_browser=False)
        assert probe.calls == []
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "8766"


# ---------------------------------------------------------------------------
# 锁活复用 / 锁死重启
# ---------------------------------------------------------------------------


class TestReuse:
    def test_alive_lock_reuses_without_factory(self, tmp_path, monkeypatch):
        """锁活 → 复用地址;零 factory 调用;只探活一次;锁内容原样。"""
        cfg = make_cfg(tmp_path, setup_port=8200)
        write_lock(cfg, "8766")  # 锁里的端口与 cfg.setup_port 不同,以锁为准
        probe = FakeProbe(alive={8766})
        monkeypatch.setattr(daemon, "_port_alive", probe)
        factory = FakeFactory(FakeServer())
        url = daemon.ensure_setup_server(cfg, factory=factory)
        assert url == "http://127.0.0.1:8766/"
        assert factory.calls == []  # 复用:零 factory
        assert probe.calls == [8766]  # 只探锁内端口一次
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "8766"
        counters = telemetry.snapshot()["counters"]
        assert counters.get(daemon.DAEMON_REUSE_METRIC) == 1
        assert daemon.DAEMON_START_METRIC not in counters

    def test_dead_lock_restarts_and_replaces_lock(self, tmp_path, monkeypatch):
        """锁死(探活 False)→ 视为陈旧,重启并按新端口换锁。"""
        cfg = make_cfg(tmp_path, setup_port=8200)
        write_lock(cfg, "8500")
        probe = FakeProbe()
        monkeypatch.setattr(daemon, "_port_alive", probe)
        server = FakeServer()
        url = daemon.ensure_setup_server(cfg, factory=FakeFactory(server),
                                         open_browser=False)
        assert url == "http://127.0.0.1:8200/"
        assert probe.calls == [8500]  # 先探旧锁端口
        assert server.start_calls == [(8200, False)]
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "8200"  # 换锁

    def test_second_call_uses_process_cache(self, tmp_path, monkeypatch):
        """进程内缓存:二次 ensure 同址,零 factory、零新增探活。"""
        cfg = make_cfg(tmp_path)
        write_lock(cfg, "8766")
        probe = FakeProbe(alive={8766})
        monkeypatch.setattr(daemon, "_port_alive", probe)
        factory = FakeFactory(FakeServer())
        first = daemon.ensure_setup_server(cfg, factory=factory)
        second = daemon.ensure_setup_server(cfg, factory=factory)
        assert first == second == "http://127.0.0.1:8766/"
        assert factory.calls == []
        assert probe.calls == [8766]  # 缓存命中不再探活

    def test_stop_after_foreign_reuse_keeps_lock(self, tmp_path, monkeypatch):
        """复用外来(跨进程)存活锁后 stop:只清进程缓存,不动外来服务与锁。"""
        cfg = make_cfg(tmp_path)
        write_lock(cfg, "8766")
        monkeypatch.setattr(daemon, "_port_alive", FakeProbe(alive={8766}))
        server = FakeServer()
        assert daemon.ensure_setup_server(cfg, factory=FakeFactory(server))
        daemon.stop_setup_server()
        assert lock_path_of(cfg).exists()  # 外来锁保持
        assert server.stop_calls == 0  # 外来服务不在管辖范围
        assert daemon._url is None
        daemon.stop_setup_server()  # 再停也不抛
        assert daemon.DAEMON_STOP_METRIC not in telemetry.snapshot()["counters"]


# ---------------------------------------------------------------------------
# 端口冲突递增 / 全满
# ---------------------------------------------------------------------------


class TestPortConflict:
    def test_occupied_ports_increment(self, tmp_path):
        """端口被占(fake 抛 OSError)→ 依端口序列 +1 递增至首个可用。"""
        cfg = make_cfg(tmp_path, setup_port=9000)
        server = FakeServer(occupied={9000, 9001})
        url = daemon.ensure_setup_server(cfg, factory=FakeFactory(server),
                                         open_browser=False)
        assert url == "http://127.0.0.1:9002/"
        assert [p for p, _ in server.start_calls] == [9000, 9001, 9002]
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "9002"

    def test_all_six_ports_full_raises_chinese(self, tmp_path):
        """base 与 +1..+5 共 6 个端口全满 → 中文 RuntimeError,不写锁。"""
        cfg = make_cfg(tmp_path, setup_port=9000)
        server = FakeServer(occupied=set(range(9000, 9006)))
        factory = FakeFactory(server)
        with pytest.raises(RuntimeError, match="均被占用"):
            daemon.ensure_setup_server(cfg, factory=factory, open_browser=False)
        assert len(factory.calls) == 1  # factory 只构造一次,同一对象重试
        assert [p for p, _ in server.start_calls] == list(range(9000, 9006))
        assert not lock_path_of(cfg).exists()
        assert daemon._url is None  # 失败不缓存

    def test_non_oserror_from_start_propagates(self, tmp_path):
        """start 抛非 OSError(真实缺陷)→ 原样上抛,不当端口冲突吞掉。"""
        cfg = make_cfg(tmp_path, setup_port=9000)

        class _Boom(FakeServer):
            def start(self, port=None, *, open_browser=False):
                self.start_calls.append((port, open_browser))
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            daemon.ensure_setup_server(cfg, factory=FakeFactory(_Boom()))


# ---------------------------------------------------------------------------
# open_browser 三态(与 A149 同口径)
# ---------------------------------------------------------------------------


class TestOpenBrowser:
    def _start_flag(self, cfg, **kw) -> bool:
        server = FakeServer()
        daemon.ensure_setup_server(cfg, factory=FakeFactory(server), **kw)
        assert len(server.start_calls) == 1
        return server.start_calls[0][1]

    def test_default_true_when_config_true(self, tmp_path):
        """无参数/无环境变量/onboarding_auto_open=True → 传 True(缺省弹)。"""
        assert self._start_flag(make_cfg(tmp_path, onboarding_auto_open=True)) is True

    def test_param_false_wins(self, tmp_path):
        """参数 False → False(压过配置 True)。"""
        cfg = make_cfg(tmp_path, onboarding_auto_open=True)
        assert self._start_flag(cfg, open_browser=False) is False

    def test_param_true_beats_env_and_config(self, tmp_path, monkeypatch):
        """参数 True 同时压过 NETSENTINEL_NO_BROWSER 与配置 False。"""
        monkeypatch.setenv(daemon.NO_BROWSER_ENV, "1")
        cfg = make_cfg(tmp_path, onboarding_auto_open=False)
        assert self._start_flag(cfg, open_browser=True) is True

    def test_env_beats_config_true(self, tmp_path, monkeypatch):
        """NETSENTINEL_NO_BROWSER 压过 onboarding_auto_open=True。"""
        monkeypatch.setenv(daemon.NO_BROWSER_ENV, "1")
        assert self._start_flag(make_cfg(tmp_path, onboarding_auto_open=True)) is False

    def test_config_false_when_no_param_no_env(self, tmp_path):
        """onboarding_auto_open=False(无参数无环境变量)→ False。"""
        assert self._start_flag(make_cfg(tmp_path, onboarding_auto_open=False)) is False


# ---------------------------------------------------------------------------
# stop 幂等 / 重启
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_idempotent_and_removes_lock(self, tmp_path):
        """stop:停服务+删自有锁+清缓存;重复调用幂等不抛。"""
        cfg = make_cfg(tmp_path)
        server = FakeServer()
        daemon.ensure_setup_server(cfg, factory=FakeFactory(server), open_browser=False)
        daemon.stop_setup_server()
        assert server.stop_calls == 1
        assert not lock_path_of(cfg).exists()
        assert daemon._url is None and daemon._server is None
        assert telemetry.snapshot()["counters"].get(daemon.DAEMON_STOP_METRIC) == 1
        daemon.stop_setup_server()  # 幂等:无服务无锁,静默
        assert server.stop_calls == 1
        assert telemetry.snapshot()["counters"].get(daemon.DAEMON_STOP_METRIC) == 1

    def test_ensure_restarts_after_stop(self, tmp_path):
        """stop 后再 ensure → 重新走 factory 启动并重建锁(守护可循环)。"""
        cfg = make_cfg(tmp_path)
        server = FakeServer()
        factory = FakeFactory(server)
        first = daemon.ensure_setup_server(cfg, factory=factory, open_browser=False)
        daemon.stop_setup_server()
        second = daemon.ensure_setup_server(cfg, factory=factory, open_browser=False)
        assert first == second == "http://127.0.0.1:8766/"
        assert len(factory.calls) == 2
        assert len(server.start_calls) == 2
        assert lock_path_of(cfg).exists()


# ---------------------------------------------------------------------------
# 工厂返回值 / 缺省工厂
# ---------------------------------------------------------------------------


class TestFactory:
    def test_start_returns_empty_raises_chinese(self, tmp_path):
        """服务 start 返回空地址 → 中文 RuntimeError,不写锁不缓存。"""
        cfg = make_cfg(tmp_path)
        with pytest.raises(RuntimeError, match="未返回有效地址"):
            daemon.ensure_setup_server(
                cfg, factory=FakeFactory(FakeServer(start_url="  ")),
                open_browser=False)
        assert not lock_path_of(cfg).exists()
        assert daemon._url is None

    def test_default_factory_module_absent_raises_chinese(self, tmp_path, monkeypatch):
        """缺省工厂:A145 模块缺席(sys.modules 置 None)→ 中文 RuntimeError。"""
        monkeypatch.setitem(sys.modules, "netsentinel.setup.server", None)
        with pytest.raises(RuntimeError, match="A145"):
            daemon.ensure_setup_server(make_cfg(tmp_path), open_browser=False)


# ---------------------------------------------------------------------------
# 并发单例
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_eight_threads_single_start(self, tmp_path):
        """8 线程并发 ensure:全员同址,factory 恰 1 次、start 恰 1 次。"""
        cfg = make_cfg(tmp_path, setup_port=9010)
        server = FakeServer()
        factory = FakeFactory(server)
        results: list[str | None] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    daemon.ensure_setup_server(cfg, factory=factory, open_browser=False))
            except BaseException as exc:  # noqa: BLE001 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert errors == []
        assert not any(thread.is_alive() for thread in threads)
        assert len(results) == 8
        assert all(url == "http://127.0.0.1:9010/" for url in results)
        assert len(factory.calls) == 1
        assert server.start_calls == [(9010, False)]
        assert lock_path_of(cfg).read_text(encoding="utf-8").strip() == "9010"


# ---------------------------------------------------------------------------
# 真实 A145 SetupServer 集成(一条)
# ---------------------------------------------------------------------------


class TestRealIntegration:
    def test_start_reuse_stop_lifecycle(self, tmp_path):
        """真实 SetupServer:起(锁=实际端口)→ GET / 200 → 复用同址 →
        stop(锁删、真实探活转死)→ 再起同端口 → 再停。NO_BROWSER 语义由
        open_browser=False 显式关闭,全程零外呼(仅回环)。"""
        pytest.importorskip("netsentinel.setup.server")
        port = _free_port()
        cfg = Config(
            data_dir=str(tmp_path / "data"),
            model_runtime_path=str(tmp_path / "model_runtime.json"),
            setup_port=port,
            onboarding_auto_open=False,
        )
        lock = pathlib.Path(cfg.data_dir) / daemon.LOCK_FILENAME

        url = daemon.ensure_setup_server(cfg, open_browser=False)
        assert url == f"http://127.0.0.1:{port}/"
        assert lock.read_text(encoding="utf-8").strip() == str(port)

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5.0) as resp:
            assert resp.getcode() == 200
            assert "净网哨兵" in resp.read().decode("utf-8")

        # 复用:进程内缓存命中,同址零重启
        assert daemon.ensure_setup_server(cfg, open_browser=False) == url

        # 停止:锁删除 + 真实探活转死(仅回环 GET,超时 0.5s)
        daemon.stop_setup_server()
        assert not lock.exists()
        assert daemon._port_alive(port) is False

        # 端口已释放:守护可再次拉起(同一端口),再停收尾
        assert daemon.ensure_setup_server(cfg, open_browser=False) == url
        daemon.stop_setup_server()
        assert not lock.exists()
