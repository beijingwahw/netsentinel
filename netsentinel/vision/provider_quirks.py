# -*- coding: utf-8 -*-
"""国内平台特化层:各提供方 API 的差异性细节集中管理(NetSentinel V4 · A64)。

与 CONTRACTS-V4 §4 A64 逐条一致:

- :data:`QUIRKS`:覆盖国内 9 家(glm/qwen/doubao/hunyuan/moonshot/minimax/stepfun/
  siliconflow/ernie)+ openrouter(国际,但确需附加归因头)。每家可选键:

  - ``extra_headers``:仅收录**调研确证**的附加请求头(如 OpenRouter 官方可选
    归因头 X-Title);未确证的一律不写,只在 ``notes`` 说明;
  - ``model_aliases``:模型参数的可用形态/别名说明(如豆包的推理接入点
    ID ``ep-xxxx``、硅基流动的 ``组织/模型`` 两级 ID);
  - ``response_quirk``:响应侧怪癖(如 Kimi 的 ``finish_reason=length`` 截断、
    MiniMax 深度思考内容走 ``reasoning_content`` 字段、OpenAI 方言家族偶发
    markdown 围栏包裹 JSON);
  - ``notes``:中文备注(计费/限流/模型名以官方文档为准)。

  安全红线:**不杜撰接口细节**——凡未在官方公开文档核实的字段一律不进
  ``extra_headers``/``model_aliases``/``response_quirk``,只放 ``notes`` 并标注
  "以官方文档为准"。调研于 2026-10-01 经各平台公开文档页核对(见模块末尾
  调研记录);核对失败的条目保留 TODO,不阻塞开发。

- :func:`apply_quirks`:容器逐层新建、不可变叶子共享的等价拷贝(V5:取代
  :func:`copy.deepcopy`,返回对象与入参在任何**容器**层级都不共享可变结构)
  后附加 ``extra_headers``(**绝不覆盖**调用方已设的头,尤其 Authorization 类键,
  匹配按头名大小写不敏感);payload 不做任何破坏性修改,仅允许经
  :data:`PAYLOAD_DEFAULTS` 注入非冲突字段(缺键才补,当前该表为空,属预留);
  无 quirk 或未知提供方 → 原样返回(仍拷贝)。

- :func:`quirk_notes`:取某提供方的中文备注,无则返回 ``""``。

本模块纯数据 + 纯函数,只用标准库,零外呼(红线 16/20)。

用法示例::

    from netsentinel.vision.provider_quirks import apply_quirks, quirk_notes

    headers = {"Authorization": "Bearer sk-or-xxx"}
    payload = {"model": "qwen/qwen2.5-vl-72b-instruct:free",
               "messages": [{"role": "user", "content": "..."}]}
    new_h, new_p = apply_quirks("openrouter", headers, payload)
    new_h["X-Title"]          # -> "NetSentinel"(官方可选归因头,未占用才附加)
    new_h is headers          # -> False(返回对象与入参隔离)
    quirk_notes("glm")        # -> 智谱调研备注中文串
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "QUIRKS",
    "QUIRK_KEYS",
    "PAYLOAD_DEFAULTS",
    "apply_quirks",
    "quirk_notes",
]

#: QUIRKS 每个提供方条目允许出现的键(超出即数据录入错误,测试会校验)
QUIRK_KEYS: frozenset[str] = frozenset(
    {"extra_headers", "model_aliases", "response_quirk", "notes"}
)

#: payload 预留注入表:provider -> {字段: 值},**仅注入 payload 缺失的字段**。
#: 当前为空表(契约 A64:实现为"透传+预留");未来确需注入某平台字段时,
#: 只允许在这里追加,且必须是非冲突(缺键才补)字段,绝不允许改写调用方已设值。
PAYLOAD_DEFAULTS: dict[str, dict[str, Any]] = {}

#: 各提供方特化表(键 = providers.PROVIDERS 中的提供方名,小写)。
#:
#: 每条内容均标注调研结论(2026-10-01 核对官方公开文档);"以官方文档为准"
#: 是 NetSentinel V4 红线 18 的强制口径:目录里的模型名/端点只是提示值。
QUIRKS: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------
    # 智谱 GLM(open.bigmodel.cn,OpenAI 兼容)
    # ------------------------------------------------------------------
    "glm": {
        "notes": (
            "智谱开放平台为 OpenAI 兼容接口,仅需 Bearer 鉴权,调研未发现平台"
            "附加请求头(2026-10 核对 docs.bigmodel.cn);视觉输入 image_url.url "
            "支持公网 URL 或 Base64 Data URL,多图可叠多个 image_url。"
            "文档现行视觉型号为 glm-5.3-flash / glm-5.3-flashx,官方推荐 "
            "temperature=1、top_p=0.95,但内容审核场景仍建议低温(0.1)保证输出"
            "稳定;模型名与计费以官方文档为准。"
        ),
    },
    # ------------------------------------------------------------------
    # 通义千问 Qwen(阿里云百炼 DashScope 兼容模式)
    # ------------------------------------------------------------------
    "qwen": {
        "notes": (
            "阿里云百炼 OpenAI 兼容模式端点因地域而异(北京 dashscope.aliyuncs."
            "com、弗吉尼亚 dashscope-us 等,均为 /compatible-mode/v1 结尾;新版"
            "业务空间走 {WorkspaceId}.cn-beijing.maas.aliyuncs.com 子域名),业务"
            "空间经 URL 体现而非请求头——调研未发现需要附加的请求头(2026-10 "
            "核对 help.aliyun.com/zh/model-studio)。注意:API Key 按地域绑定,"
            "跨地域调用返回 401 invalid_api_key;Qwen-VL 系列模型名、QPS 限流与"
            "计费以官方模型列表为准。TODO:若后续官方给出兼容模式可选头"
            "(如工作空间类),以官方文档为准再补。"
        ),
    },
    # ------------------------------------------------------------------
    # 豆包 Doubao(火山方舟 Ark v3)
    # ------------------------------------------------------------------
    "doubao": {
        "model_aliases": {
            "ep-<接入点ID>": (
                "火山方舟 model 参数支持直接填推理接入点 ID:形如 "
                "ep-2024xxxxxx-xxxxx,在方舟控制台『在线推理』创建"
            ),
            "<模型ID>": (
                "亦可直接填官方模型 ID(如 doubao-seed 系列带版本后缀形态);"
                "目录默认 doubao-1.5-vision-pro 仅为提示值,以官方模型列表为准"
            ),
        },
        "notes": (
            "火山方舟 Ark v3(ark.cn-beijing.volces.com/api/v3)完全兼容 OpenAI "
            "格式,Bearer 鉴权;视觉理解经 content 数组 image_url 传图(公网 URL "
            "或 Base64)。接入点 ID 与模型 ID 两种形态二选一;实际可用模型名、"
            "计费与限流以官方文档为准(2026-10 核对;docs.volcengine.com 页面"
            "为脚本渲染无法直接抓取,双形态结论经官方 api.volcengine.com 文档"
            "检索摘要与契约 §2 交叉确证,上线前建议人工复核一次)。"
        ),
    },
    # ------------------------------------------------------------------
    # 混元 Hunyuan(腾讯云,OpenAI 兼容)
    # ------------------------------------------------------------------
    "hunyuan": {
        "notes": (
            "腾讯混元 OpenAI 兼容接口(api.hunyuan.cloud.tencent.com/v1),Bearer "
            "鉴权,仅需 Content-Type/Authorization,调研未发现平台附加头(2026-10 "
            "核对 cloud.tencent.com/document/product/1729)。注意:文档提示生文"
            "接口默认限制 5 个并发、限额由主子账号共享;hunyuan-vision 系列实际"
            "型号与计费以官方文档为准(平台功能将逐步迁移 TokenHub,留意公告)。"
        ),
    },
    # ------------------------------------------------------------------
    # Kimi / Moonshot(api.moonshot.cn/v1,OpenAI 兼容)
    # ------------------------------------------------------------------
    "moonshot": {
        "response_quirk": (
            "finish_reason=length 表示输出被截断(官方支持按此续写补全);"
            "max_tokens 参数已弃用,建议改用 max_completion_tokens 并按需调大"
            "(文档标注 Kimi K3 默认 131072、最大 1048576);超出上下文窗口会返回"
            " invalid_request_error,多轮长对话建议只保留最近消息或做压缩"
        ),
        "notes": (
            "Kimi 开放平台(api.moonshot.cn/v1)Bearer 鉴权;多模态经 content "
            "数组 image_url/video_url 传入。可选请求签名头 X-Msh-Request-Nonce"
            "(UUID v4)需配合签名校验逻辑才有意义,本模块不注入。模型名以官方"
            "为准:文档现行列出 kimi-k2.6 / kimi-k3 等,目录默认 kimi-latest 为"
            "提示值;上下文缓存命中部分计费更低,额度与限流以平台为准"
            "(2026-10 核对 platform.moonshot.cn,已 301 迁移至 platform.kimi.com)。"
        ),
    },
    # ------------------------------------------------------------------
    # MiniMax(OpenAI 兼容)
    # ------------------------------------------------------------------
    "minimax": {
        "response_quirk": (
            "MiniMax-M3.1-Flash-Preview 深度思考强制开启:思考过程经 "
            "reasoning_content 字段返回,最终答案仍在 message.content——解析时"
            "只取 content 并忽略 reasoning_content,避免把思考文本混进 JSON;"
            "思考深度用 reasoning_effort(low~max)调节"
        ),
        "notes": (
            "MiniMax 开放平台 OpenAI 兼容接口 Bearer 鉴权。重要差异:文档 "
            "OpenAPI servers 现为 https://api.minimax.cn(POST /v1/chat/"
            "completions),目录中的 api.minimax.chat/v1 为旧域名——部署时建议用"
            " cfg.vlm_provider_base_urls 覆盖并 ping 验证。单图 ≤10MB"
            "(JPEG/PNG/GIF/WEBP),detail 档位影响图片 token 消耗;视觉模型名"
            "以官方为准(文档现行 MiniMax-M3 / MiniMax-M3.1-Flash-Preview,"
            "MiniMax-VL-01 为旧型号);限流触发错误码 1002,service_tier="
            "priority 价格为 standard 的 1.5 倍(2026-10 核对 platform.minimaxi."
            "com,已 302 迁移至 platform.minimax.cn)。"
        ),
    },
    # ------------------------------------------------------------------
    # 阶跃星辰 StepFun(api.stepfun.com/v1,OpenAI 兼容)
    # ------------------------------------------------------------------
    "stepfun": {
        "notes": (
            "阶跃星辰(api.stepfun.com/v1)OpenAI 兼容 Bearer 鉴权,官方迁移指南"
            "仅需改 base_url/api_key/模型名,调研未发现附加头(2026-10 核对 "
            "platform.stepfun.com)。Base64 传图遵循 data:[<mediatype>][;base64],"
            "<data> 的 Data URL 形态(官方文档笔误写 RFC2394,实为 RFC2397 规范);"
            "建议图片长或宽 ≤4096 像素,多图总量 ≤20MB、单请求 ≤60 张。"
            "step-1v-8k 为旧提示值,文档现行视觉模型为 Step 5 Preview / "
            "Step 3.7 Flash / Step-1o Turbo Vision,计费限流以官方为准。"
        ),
    },
    # ------------------------------------------------------------------
    # 硅基流动 SiliconFlow(api.siliconflow.cn/v1,OpenAI 兼容)
    # ------------------------------------------------------------------
    "siliconflow": {
        "model_aliases": {
            "{组织名}/{模型名}": (
                "硅基流动模型 ID 采用『组织/模型』两级形式,如 Qwen/"
                "Qwen2.5-VL-72B-Instruct、PaddlePaddle/PaddleOCR-VL;"
                "可用 cfg.vlm_provider_models 覆盖目录默认值"
            ),
        },
        "notes": (
            "硅基流动 SiliconCloud(api.siliconflow.cn/v1)OpenAI 兼容,Bearer 为"
            "唯一鉴权头;所有多模态模型统一走 /chat/completions,image_url.url "
            "支持公网 URL 或 Base64 编码数据,可选 detail(auto/low/high)。官方"
            "声明『支持的模型可能发生调整』,模型名以官方平台模型广场为准;视觉"
            "输入按分辨率折算 token 计费(如 Qwen 系列 detail=low 统一按 448×448 "
            "约 256 token),账单以官方最终转换结果为准(2026-10 核对 docs."
            "siliconflow.cn)。"
        ),
    },
    # ------------------------------------------------------------------
    # 文心 ERNIE(百度千帆 v2,OpenAI 兼容)
    # ------------------------------------------------------------------
    "ernie": {
        "model_aliases": {
            "<服务API名称>": (
                "千帆平台自训练/部署服务:model 参数填该服务详情页对应的"
                " API 名称(千帆控制台『在线推理』查看);预置服务则直接填模型 ID"
            ),
        },
        "notes": (
            "百度千帆 v2(qianfan.baidubce.com/v2/chat/completions)OpenAI 兼容,"
            "API Key 为 bce-v3/ 前缀形态、Bearer 鉴权;官方声明『除公共头域外,"
            "无其它特殊头域』。响应带 X-Ratelimit-Limit-Requests / "
            "X-Ratelimit-Remaining-Input-Tokens 等 RPM/TPM 配额头,配额用尽后 "
            "0-60s 刷新。视觉模型名以官方列表为准(文档现行 ernie-4.5-turbo-"
            "vl-preview / ernie-4.5-vl-28b-a3b 等,目录默认 ernie-4.5-vl 为提示"
            "值);传图支持 URL(UTF-8 下建议 ≤1024 字节)与 Base64(原图 "
            "≤10MB),最多 10 张;图片折算 token 计量计费(2026-10 核对 "
            "cloud.baidu.com/doc/qianfan-api)。"
        ),
    },
    # ------------------------------------------------------------------
    # OpenRouter(国际聚合网关,OpenAI 兼容,但确需附加归因头)
    # ------------------------------------------------------------------
    "openrouter": {
        "extra_headers": {
            # 官方可选归因头(非鉴权):用于 openrouter.ai 上的应用排名展示。
            # 值必须是本应用真实信息:X-Title 用本项目名;HTTP-Referer 需部署方
            # 真实站点 URL,本项目无固定站点,故不默认注入(杜撰 URL 违反红线)。
            "X-Title": "NetSentinel",
        },
        "response_quirk": (
            "OpenRouter 聚合大量上游模型,响应内容由上游生成:OpenAI 方言家族"
            "同样偶发 markdown 代码围栏(```json ... ```)包裹 JSON 的现象"
            "(gemini 系上游经转接时尤甚)。建议解析统一走 response_repair(A73)"
            "去围栏兜底,以实际响应为准"
        ),
        "notes": (
            "OpenRouter(openrouter.ai/api/v1)OpenAI 兼容。HTTP-Referer 与 "
            "X-Title 为官方文档确认的可选归因头(『Identifies your app on "
            "openrouter.ai』,用于排名展示,非鉴权必需);本模块默认仅注入 "
            "X-Title=NetSentinel 且绝不覆盖调用方已设头,HTTP-Referer 留给部署"
            "方按需自行附加。模型名(如 :free 后缀的免费路由)与计费以官方"
            "文档为准(2026-10 核对 openrouter.ai/docs)。"
        ),
    },
}


def _shared_copy(value: Any) -> Any:
    """等价拷贝(V5 性能:取代 :func:`copy.deepcopy`)。

    规则:

    - dict / list / tuple / set / bytearray 逐层**重建** → 返回对象与入参在
      任何容器层级都不共享可变结构(调用方改返回值不影响入参,隔离性与
      deepcopy 等价);
    - 其余叶子(str / int / float / bool / None / bytes / frozenset ...)
      为**不可变对象**,直接共享引用——不可变无所谓"谁的引用",省去
      deepcopy 的 memo 字典与派发表查找开销(实测小 payload 约快一个量级);
    - dict 键与 set 元素必为可哈希(事实不可变)对象,按叶子共享处理。

    适用域:headers/payload 均为 JSON 形结构(本模块契约口径);含自定义
    可变对象的入参不在兼容范围(headers/payload 需可经 HTTP 序列化)。
    """
    if isinstance(value, dict):
        return {k: _shared_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shared_copy(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_shared_copy(v) for v in value)
    if isinstance(value, set):
        return set(value)  # 元素必可哈希(事实不可变),按叶子共享
    if isinstance(value, bytearray):
        return bytearray(value)
    return value


def apply_quirks(
    provider: str,
    headers: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按提供方应用特化规则,返回与入参隔离的 ``(headers, payload)`` 新对象。

    规则(契约 A64):

    - 入参绝不就地修改(调用方可安全复用);返回对象与入参在任何容器层级
      都不共享可变结构(V5:由 :func:`_shared_copy` 逐层重建实现,等价于
      旧版深拷贝;不可变叶子共享引用);
    - ``extra_headers`` 仅在头名**未被占用**时附加(大小写不敏感比较),即绝不
      覆盖调用方已设的任何头——Authorization 类键天然受此保护;
    - payload 仅允许经 :data:`PAYLOAD_DEFAULTS` 注入**缺失**字段(非冲突),
      当前该表为空,等价于透传;
    - 无 quirk 或未知提供方:原样返回(仍是隔离拷贝)。

    用法示例::

        new_h, new_p = apply_quirks(
            "openrouter",
            {"Authorization": "Bearer sk-or"},
            {"model": "m", "messages": []},
        )
        assert new_h["X-Title"] == "NetSentinel"

    :param provider: 提供方名(如 ``"openrouter"``/``"glm"``)
    :param headers: 已构造好的请求头字典
    :param payload: 已构造好的请求体字典
    :return: ``(新 headers, 新 payload)``,容器层级与入参隔离、不可变叶子共享
    """
    new_headers: dict[str, Any] = _shared_copy(headers)
    new_payload: dict[str, Any] = _shared_copy(payload)

    # 0) payload 预留注入:只补缺失字段,绝不改写已有值(当前表为空=透传);
    #    该注入按 provider 独立生效,不以 QUIRKS 是否收录该家为前提
    for field, value in PAYLOAD_DEFAULTS.get(provider, {}).items():
        new_payload.setdefault(str(field), value)

    quirk = QUIRKS.get(provider)
    if not isinstance(quirk, dict) or not quirk:
        return new_headers, new_payload

    # 1) 附加头:头名未被占用(大小写不敏感)才附加,已有一律不覆盖
    extra = quirk.get("extra_headers")
    if isinstance(extra, dict):
        occupied = {str(name).lower() for name in new_headers}
        for name, value in extra.items():
            if str(name).lower() in occupied:
                continue
            new_headers[str(name)] = value

    return new_headers, new_payload


def quirk_notes(provider: str) -> str:
    """取提供方的中文备注(计费/限流/模型名提示);无 quirk 或未收录返回 ``""``。"""
    quirk = QUIRKS.get(provider)
    if not isinstance(quirk, dict):
        return ""
    notes = quirk.get("notes")
    return str(notes) if notes else ""


# ---------------------------------------------------------------------------
# 调研记录(2026-10-01,只读公开文档,零外呼测试)
# ---------------------------------------------------------------------------
# 已确证:
#   - openrouter:HTTP-Referer / X-Title 为官方可选归因头(openrouter.ai/docs/
#     api-reference/overview,原文 "Identifies your app on openrouter.ai")。
#   - moonshot:max_tokens 已弃用、finish_reason=length 截断、K3 默认 131072;
#     可选签名头 X-Msh-Request-Nonce(platform.kimi.com/docs/api/chat)。
#   - minimax:OpenAPI servers=https://api.minimax.cn;M3.1 思考内容走
#     reasoning_content;单图 ≤10MB;限流错误码 1002。
#   - stepfun:base_url=api.stepfun.com/v1,仅需 Bearer;现行视觉模型
#     Step 5 Preview / Step 3.7 Flash / Step-1o Turbo Vision;Base64 传图
#     data:[<mediatype>][;base64],<data>;≤4096px/20MB/60 图。
#   - siliconflow:Bearer 唯一头;模型 ID 为 组织/模型 两级形式;
#     "支持的模型可能发生调整,请以平台实际展示为准"。
#   - ernie:v2 端点/bce-v3 密钥/无特殊头域/X-Ratelimit-* 响应头/视觉模型名
#     ernie-4.5-turbo-vl-preview 等/URL≤1024 字节、Base64≤10MB、≤10 图。
#   - hunyuan:api.hunyuan.cloud.tencent.com/v1;默认 5 并发、主子账号共享。
#   - qwen(dashscope 兼容模式):无附加头;密钥按地域绑定;Qwen-VL 走兼容口。
#   - glm:docs.bigmodel.cn 现行视觉型号 glm-5.3-flash/glm-5.3-flashx;
#     image_url 支持 URL 或 Base64 Data URL。
# TODO(上线前人工复核):
#   - doubao:docs.volcengine.com 页面为脚本渲染,WebFetch 抓取为空;接入点
#     ep-xxxx / 模型 ID 双形态经官方 api.volcengine.com 文档检索摘要与契约
#     §2 交叉确证,建议 vlmctl ping 时复核。
#   - 各平台具体限流数值/单价随官方调整,一律以官方文档为准(红线 18)。
