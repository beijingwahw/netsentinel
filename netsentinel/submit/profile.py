"""个人信息模板(V10.3)—— 一次配置,永久固化;所有项目/所有门户自动读取。

## 存储层级(永久 → 临时)

1. **``~/.netsentinel/profile.yaml``**(永久模板;一次配置,终身有效)
2. ``./profile.yaml``(项目级;方便临时覆盖,勿入库)
3. Config 字段(``reporter_*``)

**首次配置**::

    python -m netsentinel.profile setup     # 交互式逐字段录入,写入永久模板
    python -m netsentinel.profile show      # 查看当前配置(掩码)
    python -m netsentinel.profile reset     # 清空永久模板

配置完成后,后续所有命令(scan/finishflow/precheck)自动读取,零二次配置。

**红线 39**:个人信息只进举报表单,不进日志/审计/报告输出。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netsentinel.contracts import Config

__all__ = [
    "ReporterProfile", "PROFILE_FIELDS", "FIELD_LABELS", "REQUIRED_FIELDS",
    "PERMANENT_PATH", "permanent_path", "load_profile", "save_permanent",
    "setup_interactive", "main",
]

logger = logging.getLogger(__name__)

PROFILE_FIELDS: tuple[str, ...] = (
    "reporter_name", "reporter_phone", "reporter_email",
    "reporter_id", "reporter_address", "reporter_postcode",
    "reporter_type", "reporter_org",
)

FIELD_LABELS: dict[str, str] = {
    "reporter_name": "举报人姓名",
    "reporter_phone": "联系电话",
    "reporter_email": "电子邮箱",
    "reporter_id": "身份证号",
    "reporter_address": "通讯地址",
    "reporter_postcode": "邮政编码",
    "reporter_type": "举报人类型",
    "reporter_org": "单位名称",
}

REQUIRED_FIELDS: frozenset[str] = frozenset({"reporter_name", "reporter_phone"})
OPTIONAL_FIELDS: tuple[str, ...] = tuple(f for f in PROFILE_FIELDS if f not in REQUIRED_FIELDS)

FIELD_PROMPTS: dict[str, str] = {
    "reporter_name": "举报人姓名(必填)",
    "reporter_phone": "联系电话(必填;接收回执)",
    "reporter_email": "电子邮箱(选填;回车跳过)",
    "reporter_id": "身份证号(选填;实名举报;回车跳过)",
    "reporter_address": "通讯地址(选填;回车跳过)",
    "reporter_postcode": "邮政编码(选填;回车跳过)",
    "reporter_type": "举报人类型(选填;个人/企业/组织;回车=个人)",
    "reporter_org": "单位名称(选填;类型=企业/组织时填;回车跳过)",
}

_PHONE_RE = re.compile(r"^[0-9+()\-\s]{5,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID_RE = re.compile(r"^\d{17}[\dXx]$")
_POSTCODE_RE = re.compile(r"^\d{6}$")


def permanent_path() -> Path:
    return Path.home() / ".netsentinel" / "profile.yaml"


PERMANENT_PATH = permanent_path


def _project_path() -> Path:
    return Path.cwd() / "profile.yaml"


def _resolve_path(cfg: Config) -> Path:
    explicit = getattr(cfg, "profile_path", "") or ""
    if explicit:
        return Path(explicit)
    if permanent_path().is_file():
        return permanent_path()
    return _project_path()


def _valid(field: str, value: str) -> tuple[bool, str]:
    if not value:
        return True, ""
    if field == "reporter_phone" and not _PHONE_RE.match(value):
        return False, "电话格式应为 5~20 位数字/加号/横线"
    if field == "reporter_email" and not _EMAIL_RE.match(value):
        return False, "邮箱格式应为 xxx@yyy.zzz"
    if field == "reporter_id" and not _ID_RE.match(value):
        return False, "身份证号应为 18 位(末位可为 X)"
    if field == "reporter_postcode" and not _POSTCODE_RE.match(value):
        return False, "邮编应为 6 位数字"
    return True, ""


@dataclass(frozen=True)
class ReporterProfile:
    reporter_name: str = ""
    reporter_phone: str = ""
    reporter_email: str = ""
    reporter_id: str = ""
    reporter_address: str = ""
    reporter_postcode: str = ""
    reporter_type: str = ""
    reporter_org: str = ""

    @property
    def has_any(self) -> bool:
        return any(getattr(self, f) for f in PROFILE_FIELDS)

    @property
    def has_required(self) -> bool:
        return bool(self.reporter_name and self.reporter_phone)

    def to_fill_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in PROFILE_FIELDS if getattr(self, f)}

    def masked(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in PROFILE_FIELDS:
            v = getattr(self, f)
            if not v:
                out[f] = ""
            elif f == "reporter_name":
                out[f] = v[:1] + "**"
            elif f == "reporter_phone":
                out[f] = v[:3] + "****" + v[-2:]
            elif f == "reporter_id":
                out[f] = v[:4] + "**********" + v[-4:]
            elif f == "reporter_email":
                at = v.find("@")
                out[f] = v[:2] + "***" + v[at:] if at > 2 else v[:2] + "***"
            elif f == "reporter_address":
                out[f] = v[:6] + "…" if len(v) > 6 else v
            else:
                out[f] = v
        return out

    def issues(self) -> list[str]:
        out: list[str] = []
        for f in sorted(REQUIRED_FIELDS):
            if not getattr(self, f):
                out.append(f"{FIELD_LABELS[f]}({f})为必填项,当前为空")
        for f in PROFILE_FIELDS:
            v = getattr(self, f)
            if v:
                ok, msg = _valid(f, v)
                if not ok:
                    out.append(f"{FIELD_LABELS[f]}({f}):{msg}")
        if self.reporter_type in ("企业", "组织") and not self.reporter_org:
            out.append("举报人类型为企业/组织时,单位名称须填写")
        return out


def _read_yaml(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception as exc:
        logger.warning("profile 文件读取失败(%s):%s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v.strip() for k, v in data.items()
            if k in PROFILE_FIELDS and isinstance(v, str) and v.strip()}


def _write_yaml(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# NetSentinel 个人信息永久模板(自动生成,一次配置终身有效)",
        "# 修改后无需重启,下次举报自动生效;删除此文件可重置",
        "",
    ]
    for f in PROFILE_FIELDS:
        v = values.get(f, "")
        label = FIELD_LABELS.get(f, f)
        req = "(必填)" if f in REQUIRED_FIELDS else "(选填)"
        lines.append(f'{f}: "{v}"  # {label} {req}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_profile(cfg: Config | None = None, *, explicit: dict[str, str] | None = None) -> ReporterProfile:
    """按优先级合并:显式传参 > 环境变量 > 永久模板 > 项目 profile.yaml > config。"""
    cfg = cfg or Config()
    path = _resolve_path(cfg)
    tmpl = _read_yaml(path)
    if path != _project_path():
        proj = _read_yaml(_project_path())
        for k, v in proj.items():
            tmpl.setdefault(k, v)

    values: dict[str, str] = {}
    for f in PROFILE_FIELDS:
        if explicit and explicit.get(f):
            values[f] = explicit[f].strip()
            continue
        env_val = os.environ.get("NETSENTINEL_" + f.upper(), "")
        if env_val.strip():
            values[f] = env_val.strip()
            continue
        if tmpl.get(f):
            values[f] = tmpl[f]
            continue
        values[f] = (getattr(cfg, f, "") or "").strip()

    profile = ReporterProfile(**values)
    for issue in profile.issues():
        logger.warning("个人信息配置提醒:%s", issue)
    return profile


def save_permanent(values: dict[str, str]) -> Path:
    path = permanent_path()
    _write_yaml(path, values)
    return path


def setup_interactive(*, stdin=None, stdout=None) -> Path:
    """交互式逐字段录入,一次写入永久模板。"""
    _print = (lambda t: print(t, file=stdout)) if stdout else print
    _input = (lambda p: (stdin.readline().strip() if stdin else input(p)))

    existing = _read_yaml(permanent_path())
    _print("=" * 60)
    _print("NetSentinel 个人信息配置(一次配置,永久固化)")
    _print(f"永久模板:{permanent_path()}")
    _print("已有值按回车保留;必填项为空会重复询问")
    _print("=" * 60)

    values: dict[str, str] = {}
    for f in PROFILE_FIELDS:
        prompt_text = FIELD_PROMPTS.get(f, FIELD_LABELS.get(f, f))
        current = existing.get(f, "")
        while True:
            suffix = f" [{current[:4]}…]" if current and len(current) > 6 else (
                f" [{current}]" if current else "")
            raw = _input(f"  {prompt_text}{suffix}: ").strip()
            val = raw or current
            if not val and f in REQUIRED_FIELDS:
                _print(f"  ⚠ {FIELD_LABELS.get(f)}为必填,不能为空")
                continue
            if val:
                ok, msg = _valid(f, val)
                if not ok and raw:
                    _print(f"  ⚠ {msg},请重新输入")
                    continue
            values[f] = val
            break

    path = save_permanent(values)
    profile = ReporterProfile(**values)
    _print("")
    _print(f"✅ 已写入永久模板:{path}")
    _print(f"   必填项:{'✓ 已填' if profile.has_required else '✗ 缺失'}")
    _print(f"   后续所有命令自动读取,无需二次配置;删除文件可重置")
    for issue in profile.issues():
        _print(f"   ⚠ {issue}")
    return path


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m netsentinel.profile",
        description="个人信息模板管理(一次配置,永久固化)")
    parser.add_argument("command", choices=["setup", "show", "reset", "path"])
    args = parser.parse_args(argv)

    if args.command == "path":
        print(f"永久模板:{permanent_path()}")
        print(f"  存在:{permanent_path().is_file()}")
        return 0

    if args.command == "setup":
        setup_interactive()
        return 0

    if args.command == "show":
        profile = load_profile()
        src = permanent_path() if permanent_path().is_file() else _project_path()
        print(f"模板来源:{src}")
        print(f"必填项:{'✓' if profile.has_required else '✗'}")
        m = profile.masked()
        for f in PROFILE_FIELDS:
            v = m.get(f, "")
            mark = "✓" if getattr(profile, f) else "—"
            print(f"  {mark} {FIELD_LABELS.get(f, f):<12} {v or '(未配置)'}")
        for issue in profile.issues():
            print(f"  ⚠ {issue}")
        return 0

    if args.command == "reset":
        path = permanent_path()
        if path.exists():
            path.unlink()
            print(f"已删除永久模板:{path}")
        else:
            print("永久模板不存在,无需重置")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
