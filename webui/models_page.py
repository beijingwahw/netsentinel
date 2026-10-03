"""净网哨兵 · 复核台"模型"页(A161,独立入口,不改动既有 app.py /
dashboard.py / providers_page.py / groups_page.py)。

定位:面向**复核员/运营者**的"当前视觉模型 + 本地服务 + 密钥状态"只读复核
与手动切换视图——顶部横幅展示活动模型;扫描本机视觉服务(仅点击后探测)
后逐行给出【切换到此模型】按钮;密钥状态**只显示掩码**;附切换说明。
与连接向导(setup/,密钥录入的唯一关口)和 CLI
``python -m netsentinel.modelmgr`` 互补:本页**不提供任何密钥输入**。

结构约定(与 tests/test_models_page.py 对应):
- 纯逻辑层(本文件上半部分,无 streamlit、无兄弟模块顶层依赖,可独立导入):
  * :func:`model_rows`     A143 扫描行 → 本地服务表行(视觉计数/前5模型/是否活动);
  * :func:`switch_banner`  活动模型 → 顶部横幅中文文案(两态);
  * :func:`key_status`     cfg → 逐提供方密钥状态(已配置→掩码,未配置→None);
  * :func:`can_switch`     spec 语法预检(stub 或 parse_spec 通过,惰性)。
- UI 层(下半部分,streamlit 顶部惰性 try/except,缺依赖时 main() 打印
  中文安装提示并返回退出码 1):
  * :func:`render` 四区:①横幅 ②本地服务表(扫描按钮 + 切换按钮)
    ③密钥状态表(只读掩码)④切换说明。

安全红线(必须体现在代码里):
- **密钥只进不显**(红线 33):本页任何输出只含掩码(前 4 位 + ``****``)
  或 未配置/None,绝不出现密钥本体;**页面不提供密钥输入**,录入一律指向
  连接向导(``python -m netsentinel.modelmgr serve``);
- **本地探测仅限回环**(红线 32):扫描复用 A143 ``LocalVisionScanner``
  (恒 127.0.0.1、仅 ``cfg.local_probe_ports`` 指定端口),且只在用户点击
  "扫描"按钮后发起,常规浏览零外呼;
- **离线桩必须明示**(红线 34):活动模型为 ``stub`` 时横幅下追加
  "离线桩,非模型判定"警示;切换走 A144 ``ModelManager.set_active``
  (``switched_by="wizard"``,连通性测试走 A147 connectivity,不过即拒绝)。
"""
from __future__ import annotations

import sys
from typing import Any

# ---------------------------------------------------------------------------
# streamlit 惰性导入:缺失时纯逻辑层仍可被测试导入(UI 在 main() 里拦截)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    import streamlit as st

    _HAS_ST = True
except ImportError:  # pragma: no cover - 取决于运行环境
    st = None  # type: ignore[assignment]
    _HAS_ST = False

__all__ = [
    "BANNER_NONE",
    "STUB_SPEC",
    "can_switch",
    "key_status",
    "main",
    "model_rows",
    "render",
    "switch_banner",
]

# ===========================================================================
# 纯逻辑层(无 streamlit 依赖;兄弟模块一律函数内惰性导入)
# ===========================================================================

#: 未连接任何视觉模型时的横幅文案(把用户引向连接向导)。
BANNER_NONE: str = "尚未连接视觉模型(向导:python -m netsentinel.modelmgr serve)"

#: 离线桩规格名(与 A144 ``ModelManager.STUB_SPEC`` 同值;本页独立声明避免顶层耦合)。
STUB_SPEC: str = "stub"

#: 本地服务表每行展示的模型名上限(切换按钮只针对首个视觉模型)。
MAX_MODELS_SHOWN: int = 5

#: model_rows 每行的固定键(顺序即展示顺序;不含任何密钥字段,红线 33)。
_ROW_KEYS: tuple[str, ...] = (
    "provider",
    "port",
    "base_url",
    "vision_count",
    "models",
    "is_active",
)

#: 掩码后缀(与 security.keys / vault 打码口径一致:前 4 位 + ****,红线 33)。
_MASK_SUFFIX = "****"

#: 状态文件路径兜底(与 contracts.Config.model_runtime_path 同默认值)。
_DEFAULT_RUNTIME_PATH = "data/model_runtime.json"


def _field(src: Any, name: str, default: Any = "") -> Any:
    """从鸭子类型对象或 dict 里取字段;取到 None 时回退默认值。"""
    if isinstance(src, dict):
        val = src.get(name, default)
    else:
        val = getattr(src, name, default)
    return default if val is None else val


def _clean_str(value: Any) -> str:
    """规整为去空白字符串(非字符串 / None → 空串)。"""
    return str(value).strip() if isinstance(value, str) else ""


def _clean_models(raw: Any) -> list[str]:
    """模型名列表规整:仅保留非空字符串项,逐项去空白,去重保序。"""
    out: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        text = _clean_str(item)
        if text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# model_rows:A143 扫描行 → 本地服务表行
# ---------------------------------------------------------------------------


def model_rows(local_scan: list[dict] | None, active: str | None = None) -> list[dict]:
    """把 A143 ``LocalVisionScanner.scan()`` 的结果规整为本地服务表行。

    每行固定键 ``{provider, port, base_url, vision_count, models, is_active}``:

    - 只保留**在线行**(``ok=True``):离线/报错端口无从切换,由 UI 层另行
      展示错误行;在线但无视觉模型(vision_count=0)同样是有效清点结果,
      保留该行(无切换按钮);
    - ``vision_count``:该服务清点到的视觉模型**总数**;
    - ``models``:前 :data:`MAX_MODELS_SHOWN` 个视觉模型名(仅展示用);
    - ``is_active``:两判式——``spec == "{provider}:{首个视觉模型}"`` 或
      ``spec == provider``(目录默认模型写法)任一命中即为 ``True``;
    - 入参容错:行可为 dict 或鸭子对象;``None`` / 非列表 / 空扫描一律
      返回 ``[]``,绝不抛错。

    示例::

        >>> model_rows([{"provider": "ollama", "port": "11434",
        ...              "base_url": "http://127.0.0.1:11434/v1",
        ...              "models": ["llava:13b"], "ok": True}])[0]["is_active"]
        False
    """
    spec = _clean_str(active)
    rows: list[dict] = []
    for entry in local_scan if isinstance(local_scan, (list, tuple)) else []:
        if not _field(entry, "ok", False):
            continue
        provider = _clean_str(_field(entry, "provider"))
        if not provider:
            continue
        models = _clean_models(_field(entry, "models", []))
        first = models[0] if models else ""
        rows.append(
            {
                "provider": provider,
                "port": _clean_str(_field(entry, "port")),
                "base_url": _clean_str(_field(entry, "base_url")),
                "vision_count": len(models),
                "models": models[:MAX_MODELS_SHOWN],
                "is_active": bool(
                    spec and (spec == provider or (bool(first) and spec == f"{provider}:{first}"))
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# switch_banner:活动模型 → 横幅文案
# ---------------------------------------------------------------------------


def switch_banner(active: str | None) -> str:
    """活动模型 spec → 顶部横幅中文文案(两态)。

    - 已连接:``"当前视觉模型:{spec}"``;
    - 未连接(``None`` / 空串 / 纯空白)::data:`BANNER_NONE`,把用户引向
      连接向导(``python -m netsentinel.modelmgr serve``)。

    示例::

        >>> switch_banner("ollama:llava")
        '当前视觉模型:ollama:llava'
        >>> switch_banner(None)
        '尚未连接视觉模型(向导:python -m netsentinel.modelmgr serve)'
    """
    text = _clean_str(active)
    if text:
        return f"当前视觉模型:{text}"
    return BANNER_NONE


# ---------------------------------------------------------------------------
# key_status:cfg → 逐提供方密钥状态(掩码 / None,绝不含本体)
# ---------------------------------------------------------------------------


def key_status(cfg: Any) -> dict[str, str | None]:
    """逐提供方返回密钥状态:已配置 → 掩码(前 4 位 + ``****``),未配置 → ``None``。

    惰性委托 A70 ``security.keys.configured(cfg)`` 取全目录布尔表,再对
    已配置项经 ``keys.get_key`` 取值打码。**任何路径都绝不返回密钥本体**
    (红线 33):``get_key`` 缺席 / 抛异常 / 返回空值时,已配置项回退为
    纯 ``"****"`` 掩码,不泄露任何片段。

    容错:A70 缺席 / ``configured`` 不可调用 / 抛异常 / 返回非 dict →
    返回 ``{}``(页面显示中文提示);``cfg`` 为 ``None`` 也可(A70 内部
    按无配置项处理)。

    示例::

        >>> status = key_status(cfg)          # doctest: +SKIP
        >>> status["qwen"], status["glm"]
        ('sk-A****', None)                    # 掩码或 None,绝无本体
    """
    keys_mod: Any = None
    try:
        from netsentinel.security import keys as keys_mod  # 惰性导入
    except Exception:  # noqa: BLE001 - A70 允许缺席(并行开发期)
        keys_mod = None
    if keys_mod is None:
        return {}

    fn = getattr(keys_mod, "configured", None)
    if not callable(fn):
        return {}
    try:
        configured = fn(cfg)
    except Exception:  # noqa: BLE001 - A70 异常不让页面崩溃
        return {}
    if not isinstance(configured, dict):
        return {}

    get_fn = getattr(keys_mod, "get_key", None)
    status: dict[str, str | None] = {}
    for name, flag in configured.items():
        provider = _clean_str(name)
        if not provider:
            continue
        if not flag:
            status[provider] = None
            continue
        masked = _MASK_SUFFIX  # 兜底掩码:宁可少显,绝不泄露(红线 33)
        if callable(get_fn):
            try:
                value = get_fn(provider, cfg)
            except Exception:  # noqa: BLE001 - 取值失败保持兜底掩码
                value = ""
            if isinstance(value, str) and value.strip():
                masked = value.strip()[:4] + _MASK_SUFFIX
        status[provider] = masked
    return status


# ---------------------------------------------------------------------------
# can_switch:spec 语法预检(惰性)
# ---------------------------------------------------------------------------


def can_switch(spec: str | None) -> bool:
    """判断 spec 能否作为切换目标(**纯语法预检**,不发起任何网络请求)。

    三分支:

    - ``"stub"``(离线桩)→ ``True``(永远可用,红线 34);
    - 其余 spec:惰性经 A61 ``providers.parse_spec`` 校验,通过 → ``True``
      (如 ``"ollama:llava"``、``"glm"`` 目录默认写法);
    - 未知提供方 / 空串 / 非字符串 / A61 缺席 → ``False``。

    注意:``True`` 只代表语法合法;实际切换仍由 A144 ``set_active`` 做连通性
    测试(不过即拒绝)。本函数零外呼。

    示例::

        >>> can_switch("stub"), can_switch("ollama:llava"), can_switch("nope:m")
        (True, True, False)
    """
    text = _clean_str(spec)
    if not text:
        return False
    if text == STUB_SPEC:
        return True
    try:
        from netsentinel.vision import providers  # 惰性导入
    except Exception:  # noqa: BLE001 - A61 允许缺席(并行开发期)
        return False
    parse_spec = getattr(providers, "parse_spec", None)
    if not callable(parse_spec):
        return False
    try:
        parse_spec(text)
    except Exception:  # noqa: BLE001 - ValueError(未知提供方)等一律 False
        return False
    return True


# ===========================================================================
# UI 层(以下代码仅在 streamlit 运行时执行;兄弟模块一律函数内导入)
# ===========================================================================

_PAGE_TITLE = "视觉模型 · 模型页"
_MOTTO = (
    "当前模型一目了然:活动横幅 · 本地服务(仅回环探测)· 密钥状态(只读掩码)"
    "· 手动切换;本页面不执行任何举报提交。"
)
_KEY_NOTICE = (
    "⚠️ 密钥只进不显(红线 33):本页只显示掩码,不提供密钥输入;"
    "录入密钥请走连接向导:python -m netsentinel.modelmgr serve。"
)
_SWITCH_NOTE = (
    "切换仅对**下一条**扫描/批次生效,不打断进行中任务;"
    "连通性测试不过会被拒绝(可用 CLI --force 强制)。"
)
_STUB_WARNING = "当前使用**离线桩,非模型判定**(红线 34):结果仅供演练,不能作为模型结论。"

#: 本地扫描结果(原始行,含离线错误行)在 session_state 里的键。
_SS_SCAN = "models_page_local_scan"


def _runtime_path(cfg: Any) -> str:
    """活动模型状态文件路径(cfg.model_runtime_path,缺省 data/model_runtime.json)。"""
    return _clean_str(_field(cfg, "model_runtime_path", _DEFAULT_RUNTIME_PATH)) or _DEFAULT_RUNTIME_PATH


def _load_manager(cfg: Any) -> Any:
    """惰性构造 A144 ``ModelManager``;模块未就位返回 ``None``(UI 负责提示)。"""
    try:
        from netsentinel.vision.model_manager import ModelManager  # 惰性导入
    except Exception:  # noqa: BLE001 - A144 允许缺席
        return None
    return ModelManager(_runtime_path(cfg), cfg=cfg)


def _load_tester() -> Any:
    """惰性取 A147 ``connectivity.test_connection`` 作为切换连通性测试器;缺席 ``None``。"""
    try:
        from netsentinel.vision.connectivity import test_connection  # 惰性导入
    except Exception:  # noqa: BLE001 - A147 允许缺席(set_active 内部还会再试)
        return None
    return test_connection


def _do_switch(cfg: Any, spec: str) -> None:
    """把 ``spec`` 设为活动模型(A144 set_active,来源 wizard;tester 走 A147)。"""
    manager = _load_manager(cfg)
    if manager is None:
        st.error("活动模型管理器(netsentinel.vision.model_manager,A144)未就位,无法切换。")
        return
    try:
        status = manager.set_active(spec, switched_by="wizard", tester=_load_tester())
    except Exception as exc:  # noqa: BLE001 - 中文 ValueError / RuntimeError 直接展示
        st.error(f"切换失败:{exc}")
        return
    st.success(f"已切换:{status.get('spec')}(下一条扫描生效)")
    st.rerun()


def _render_local_services(cfg: Any, active: str | None) -> None:
    """区②本地服务表:显式点击才扫描(红线 32),在线行给切换按钮。"""
    st.subheader("本地视觉服务(仅本机回环)")
    st.caption(
        "点击「扫描」后才发起探测:仅访问 127.0.0.1 上 cfg.local_probe_ports"
        " 指定端口的 /v1/models,绝不扫外网(红线 32);常规浏览零外呼。"
    )
    if st.button("扫描本机服务", type="primary"):
        try:
            from netsentinel.vision.local_probe import LocalVisionScanner  # 惰性导入
        except Exception as exc:  # noqa: BLE001 - A143 允许缺席
            st.error(f"本地探测模块(netsentinel.vision.local_probe,A143)未就位:{exc}")
        else:
            ports = _field(cfg, "local_probe_ports", None) or None
            try:
                st.session_state[_SS_SCAN] = LocalVisionScanner(ports).scan()
            except Exception as exc:  # noqa: BLE001 - 端口表损坏等不让页面崩溃
                st.error(f"本地扫描失败:{exc}")

    scan = st.session_state.get(_SS_SCAN)
    if not isinstance(scan, list):
        st.info("尚未扫描。点击「扫描本机服务」发起一次人工显式探测(不点不探)。")
        return

    offline = [
        (f"- ❌ **{_clean_str(_field(row, 'provider')) or '未知服务'}**"
         f"(端口 {_clean_str(_field(row, 'port'))}):{_clean_str(_field(row, 'error')) or '探测失败'}")
        for row in scan
        if isinstance(row, dict) and not _field(row, "ok", False)
    ]
    if offline:
        st.markdown("\n".join(offline))

    rows = model_rows(scan, active)
    if not rows:
        st.info("本次扫描未发现在线的本地视觉服务;可改用连接向导接入云平台,或使用离线桩。")
        return
    for idx, row in enumerate(rows):
        left, right = st.columns([4, 1])
        model_text = "、".join(row["models"]) if row["models"] else "(无视觉模型)"
        extra = row["vision_count"] - len(row["models"])
        if extra > 0:
            model_text += f" 等 {row['vision_count']} 个"
        mark = "【当前活动】" if row["is_active"] else ""
        left.markdown(
            f"{mark}**{row['provider']}** · 端口 {row['port']} · `{row['base_url']}`\n\n"
            f"视觉模型 {row['vision_count']} 个:{model_text}"
        )
        if row["models"]:
            if right.button("切换到此模型", key=f"models_page_switch_{idx}", disabled=row["is_active"]):
                _do_switch(cfg, f"{row['provider']}:{row['models'][0]}")
        else:
            right.caption("(无可切换)")
    st.caption(
        f"表中最多展示前 {MAX_MODELS_SHOWN} 个视觉模型;【切换到此模型】切换到该服务的"
        "首个视觉模型(与自动接管口径一致),其余模型可经 CLI "
        "`python -m netsentinel.modelmgr switch 提供方:模型` 切换;"
        "切换来源记为 wizard,连通性测试走 A147 connectivity,不过即拒绝。"
    )


def _render_key_status(cfg: Any) -> None:
    """区③密钥状态表:只读掩码,绝不提供密钥输入(红线 33)。"""
    st.subheader("密钥状态(只读)")
    st.caption(_KEY_NOTICE)
    status = key_status(cfg)
    if not status:
        st.info("密钥模块(netsentinel.security.keys)未就位或无提供方目录,无法生成状态表。")
        return
    configured = [name for name, masked in status.items() if masked]
    st.caption(f"已配置 {len(configured)} / {len(status)} 家(云端);本地提供方免密钥。")
    st.table(
        [
            {
                "提供方": provider,
                "密钥状态": masked if masked else "未配置",
            }
            for provider, masked in status.items()
        ]
    )
    st.caption(
        "掩码口径:前 4 位 + ****;本页任何位置都不出现密钥本体(红线 33),"
        "也不提供密钥输入——录入请走连接向导(python -m netsentinel.modelmgr serve)。"
    )


def _render_switch_help() -> None:
    """区④切换说明:spec 语法 / 三入口 / 生效时机 / 离线桩警示。"""
    st.subheader("切换说明")
    st.markdown(
        "- **spec 语法**:`提供方:模型`(如 `ollama:llava`、`glm:glm-4.5v`)"
        "或只写提供方(如 `glm`,用目录默认模型);`stub` = 离线桩,"
        "**离线桩,非模型判定**(红线 34);\n"
        "- **三入口**:本页按钮(来源 wizard)/ CLI "
        "`python -m netsentinel.modelmgr switch SPEC [--force]` / "
        "REST `POST /model/switch`;\n"
        f"- {_SWITCH_NOTE}\n"
        "- **回退离线桩**:`python -m netsentinel.modelmgr switch stub`;"
        "当前无任何模型时,首次运行会自动触发连接向导(takeover)。"
    )


def render() -> None:
    """模型页主界面(streamlit 脚本入口调用的渲染函数)。"""
    if not _HAS_ST:  # pragma: no cover - main() 已拦截,防御性兜底
        raise RuntimeError(
            "streamlit 不可用,无法渲染模型页;请先安装 python -m pip install -e \".[ui]\""
        )

    from netsentinel.config import load_config  # 兄弟模块函数内导入
    from webui.app import apply_data_dir

    st.set_page_config(page_title=_PAGE_TITLE, page_icon="🎛️", layout="wide")
    st.title(f"🎛️ {_PAGE_TITLE}")
    st.caption(_MOTTO)
    st.warning(_KEY_NOTICE)  # 页面顶部声明:密钥只进不显,录入走向导(红线 33)

    cfg = apply_data_dir(load_config())
    manager = _load_manager(cfg)
    if manager is None:
        st.error("活动模型管理器(netsentinel.vision.model_manager,A144)未就位,本页仅能浏览。")
        active: str | None = None
    else:
        active = manager.get_active()

    if active:
        st.success(switch_banner(active))
    else:
        st.warning(switch_banner(active))
    if active == STUB_SPEC:
        st.error(_STUB_WARNING)  # 离线桩必须明示(红线 34)

    _render_local_services(cfg, active)
    _render_key_status(cfg)
    _render_switch_help()


def main() -> int:
    """脚本入口:缺 streamlit 时打印中文安装提示并返回退出码 1。"""
    if not _HAS_ST:
        print(
            "未安装 Streamlit,模型页无法启动。\n"
            "请先执行:python -m pip install -e \".[ui]\"\n"
            "然后运行:streamlit run webui/models_page.py",
            file=sys.stderr,
        )
        return 1
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
