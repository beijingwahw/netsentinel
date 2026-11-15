"""security 包内共享的文件系统小工具(密钥文件权限收紧的单一实现)。

收敛说明(V?):``_tighten_key_file_permissions`` 此前在 keys.py / vault.py /
bundle_sign.py 三处逐字几乎相同(仅日志称谓略异)。现收敛为本模块的
:func:`tighten_key_file_permissions` **单一实现**——Windows ``icacls`` 的
``USERDOMAIN`` 账户处理等最新逻辑只保留这一份;三个模块各自保留同名薄包装
(转发到本实现,模块级名字不变,测试可继续 monkeypatch 各模块的名字)。

只用标准库;任何失败只记日志提示,绝不抛出(权限收紧是建议性动作)。
"""
from __future__ import annotations

import logging
import os
import pathlib
import stat
import subprocess
from typing import Any

__all__ = ["tighten_key_file_permissions"]

_LOGGER = logging.getLogger(__name__)


def tighten_key_file_permissions(
    path: pathlib.Path,
    *,
    logger: logging.Logger | None = None,
    label: str = "密钥文件",
) -> None:
    """收紧密钥类文件权限;任何失败只记日志提示,绝不抛出。

    - POSIX:``os.chmod(path, 0o600)``(以 ``stat.S_IRUSR | stat.S_IWUSR``
      表达);
    - Windows(``os.name == 'nt'``):尝试 ``icacls`` 移除继承并仅授权当前用户。
      输出按**字节**捕获(不传 ``text=True``),避免非 UTF-8 / GBK 控制台输出
      触发解码错误;只看退出码,非 0 仅中文提示手动处理。

    :param path: 目标文件路径。
    :param logger: 记日志用的 logger(缺省用本模块 logger);调用方模块传入
        自己的 logger,让中文提示落在调用方名下(测试 caplog 的口径)。
    :param label: 日志文案中的文件称谓("密钥文件" / "签名密钥文件"),
        收敛前三个模块唯一的措辞差异。

    行为与收敛前逐字一致(以 keys.py 版本为基准;bundle_sign 的
    ``os.chmod(path, 0o600)`` 与 ``stat.S_IRUSR | stat.S_IWUSR`` 等值)。
    """
    log = logger if logger is not None else _LOGGER
    try:
        if os.name == "nt":
            # V10.12:icacls 授权需要账户名——USERNAME 是账户名,USERDOMAIN
            # 是计算机/域名(单独当账户用必失败);二者组合 DOMAIN\user,
            # 缺 USERNAME 回退 getpass(旧回退 path.owner() 在 Windows +
            # Py3.10–3.12 抛 NotImplementedError,权限收紧会静默失效)。
            import getpass

            _name = os.environ.get("USERNAME", "").strip()
            if _name:
                _domain = os.environ.get("USERDOMAIN", "").strip()
                account = f"{_domain}\\{_name}" if _domain else _name
            else:
                try:
                    account = getpass.getuser()
                except Exception:  # noqa: BLE001 - 取不到账户按未收紧处理
                    account = ""
            if not account:
                log.info("无法确定当前 Windows 账户,请手动收紧%s权限:%s", label, path)
                return
            grant = f"{account}:F"
            result: Any = subprocess.run(  # noqa: S603 本地权限命令,参数受控
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,  # 字节捕获:绝不解码 icacls 输出,只看退出码
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                log.info(
                    "密钥文件 %s 未能自动收紧权限(icacls 退出码 %s),"
                    "建议手动执行:icacls \"%s\" /inheritance:r /grant:r \"%s\"",
                    path,
                    result.returncode,
                    path,
                    grant,
                )
            else:
                log.info("已通过 icacls 收紧%s权限:%s", label, path)
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            log.info("已将%s权限收紧为仅属主可读写(chmod 600):%s", label, path)
    except Exception as exc:  # noqa: BLE001 权限收紧是建议性动作,失败不阻断
        log.warning(
            "无法自动收紧%s %s 的权限(%s),建议手动限制为仅当前用户可读。",
            label,
            path,
            exc,
        )
