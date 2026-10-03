"""A136 · 安全内核·模糊测试(threat kernel)—— 对关键内核入口做恶意输入冒烟。

NetSentinel 的八个 v7 内核之外,还需要一个**安全内核**:在离线、确定性、
可复现的前提下,把「超长 / 控制字符 / 深嵌套 / 坏编码 / 边界数字」这类
攻击者最爱的输入,成规模地砸向既有内核的关键入口,并回答一个问题:

    **任何输入下,内核入口要么正常返回、要么抛允许集内的异常
    (ValueError / RuntimeError / TypeError),绝不出现允许集之外的
    异常类型、绝不崩溃失控。**

组件(CONTRACTS-V7 §2 A136):

- 四个确定性模糊生成器 :func:`fuzz_url` / :func:`fuzz_html` /
  :func:`fuzz_yaml` / :func:`fuzz_json`(各 ≥30 变体);
- :func:`all_cases`:汇成 ``[(kind, text), ...]`` 全量用例矩阵;
- :func:`smoke`:通用冒烟 harness(逐 case 调 ``fn(text)``,
  允许集内异常记 ok,其余记 violation;不吞 KeyboardInterrupt /
  SystemExit);
- :data:`TARGETS` + :func:`bind_target`:懒绑定五个关键内核入口
  (canonical / text_intel / response_repair / policy.load_policy /
  conformal.fit_threshold 的 JSON 适配);
- :func:`run_suite` / :func:`main`:跑全量 目标 × 用例 矩阵,
  输出中文报告(违规清单 + skip 清单),退出码 0/2;
- :func:`kernel_selfcheck`:A138 kernel_bench 统一自检入口(纯计数,零墙钟)。

确定性口径(红线 31 的模糊测试版):

- 生成器内的随机成分全部来自**模块级种子**派生的独立
  ``random.Random`` 实例:同进程内两次调用同一生成器,产物**逐字节
  相同**;跨进程 / 跨平台亦然(Mersenne Twister + zlib.crc32 均稳定)。
- 语料**不含孤立代理项**(lone surrogate):坏 UTF-8 统一以替换符
  ``\\ufffd`` 呈现——保证每条用例都能以 UTF-8 落盘(政策内核走
  临时文件路径喂入),也不会在报告打印时二次炸编码。

诚实口径(不许静默):

- :data:`SKIP_LIST` 是**已登记的既有内核违规**清单:登记项不豁免
  执行(全量照跑),只把违规区分为「已登记(待修复)」与「新发现」,
  两类都在报告中原样打印;内核修复后登记项失配会被标记为
  「已失效,请移除」。
- 当前无已登记违规(首例登记 conformal 巨整溢出已于 2026-10-02
  由负责人修复并按机制清空登记)。

纯标准库、离线、零网络、零落盘(政策内核的临时文件用后即删);
兄弟模块一律惰性导入(契约 §2),导入本模块不触发任何 netsentinel 子模块加载。
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import random
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "DEFAULT_ALLOWED",
    "FuzzCase",
    "KNOWN_VIOLATIONS",
    "SKIP_LIST",
    "TARGETS",
    "BoundTarget",
    "TargetSpec",
    "all_cases",
    "bind_target",
    "fit_threshold_from_json",
    "fuzz_html",
    "fuzz_json",
    "fuzz_url",
    "fuzz_yaml",
    "kernel_selfcheck",
    "main",
    "run_suite",
    "smoke",
]

# ---------------------------------------------------------------------------
# 常量:种子 / 允许集 / 临时文件前缀
# ---------------------------------------------------------------------------

#: 模块级随机种子(A136 工号派生):四个生成器各自再经 ``zlib.crc32(kind)``
#: 派生独立子种子,互不干扰;任何一次调用的产物只取决于本常量。
_SEED: int = 2026_0136

#: smoke 默认允许集:内核入口对恶意输入只允许以这三种类型"体面地失败"
#: (RecursionError ⊂ RuntimeError,深嵌套导致的栈溢出同样在允许集内)。
DEFAULT_ALLOWED: tuple[type[BaseException], ...] = (
    ValueError,
    RuntimeError,
    TypeError,
)

#: 政策内核临时文件名前缀(冒烟清理断言按它扫描)
_TMP_PREFIX: str = "nsfuzz_"


def _rng(kind: str) -> random.Random:
    """为某类生成器派生独立的确定性随机源(每次调用全新实例,保证幂等)。"""
    return random.Random(_SEED ^ zlib.crc32(kind.encode("utf-8")))


# ---------------------------------------------------------------------------
# 模糊用例:四类生成器(各 ≥30 变体,确定性)
# ---------------------------------------------------------------------------

#: 一条模糊用例:(类别 url/html/yaml/json, 用例文本)。
FuzzCase = tuple[str, str]

#: 深嵌套层数(契约 §2 A136:500 层)
_DEPTH: int = 500

#: 超长用例目标长度(契约 §2 A136:≥10KB)
_LONG: int = 10240


def _seeded_garbage(
    rng: random.Random, pool: str, n: int, min_len: int, max_len: int
) -> list[str]:
    """用模块级种子随机源生成 n 条确定性"乱码"(长度 min_len~max_len)。"""
    return [
        "".join(rng.choice(pool) for _ in range(rng.randint(min_len, max_len)))
        for _ in range(n)
    ]


def fuzz_url() -> list[str]:
    """URL 模糊用例(≥30 变体):空串/空白/坏端口/坏 IPv6/认证信息/大小写/
    畸形域名/IDN 原文与 punycode/300 字超长标签/10KB 路径/500 层子域/
    巨数字与巨负数查询/控制字符/换页符 \\x0c/空字节/坏 UTF-8 替换符/
    javascript·data·file 等异构 scheme/双端口 + 3 条种子随机乱码。

    幂等:两次调用返回逐字节相同的列表(种子派生自 :data:`_SEED`)。
    """
    rng = _rng("url")
    fixed: list[str] = [
        "",                                            # 空串
        " ",                                           # 仅空白(单空格)
        "\t \r\n ",                                    # 混合空白
        "http://",                                     # 仅 scheme,无主机
        "://no-scheme.example.com/",                   # 无 scheme
        "http://localhost",                            # 单标签主机
        "http://localhost:99999/",                     # 端口越界(urlsplit→"")
        "http://localhost:65536/",                     # 端口上界 +1
        "http://127.0.0.1:8080/a?b=1",                 # IPv4 直连
        "http://[2001:0DB8::0001]/x",                  # IPv6(大写压缩)
        "http://[::1",                                 # IPv6 括号不闭合
        "http://user:pa:ss@example.com/",              # 认证信息内含冒号
        "HtTpS://WwW.ExAmPlE.CoM/PaTh",                # 大小写混合
        "http://example.com.",                         # 主机尾部圆点
        "http://.leading.dot/",                        # 前导圆点
        "http://trailing..dot/",                       # 连续圆点
        "http://a..b.example.com/",                    # 标签间空段
        "http://例え.テスト.中国/路径",                  # IDN 原文(非 ASCII TLD)
        "http://xn--fiqs8s.cn/",                       # punycode
        "http://" + "a" * 300 + ".com/",               # 300 字超长单标签
        "http://example.com/" + "p" * _LONG,           # 10KB 路径
        "http://" + "a." * _DEPTH + "com",             # 500 层子域标签
        "http://example.com/?q=" + "9" * 4096,         # 巨数字查询串
        "http://example.com/x?n=-99999999999999999999",  # 巨负数参数
        "http://exa\x01mple.com/\x02y",                # 控制字符
        "http://example.com/\x0cpage",                 # 换页符 \x0c
        "http://example.com/\x00.js",                  # 空字节
        "http://exa\ufffdmple.com/\ufffdpath",         # 坏 UTF-8 替换符
        "javascript:alert(1)//example.com",            # 脚本 scheme
        "data:text/html;base64,PGI+dGVzdDwvYj4=",      # data URL
        "file:///C:/1/netsentinel/x.txt",              # file URL
        "http://example.com:80:80/",                   # 双端口
        "http://-hyphen-.example.com/",                # 连字符包裹的标签
        "http://example.com/#{}\x7f^|",                # 片段内特殊/DEL 字符
    ]
    randoms = [
        "http://" + g for g in _seeded_garbage(
            rng, "htp:/?#&=%@[]{}|\\^~`'\" \x01\x0c\ufffd中", 3, 16, 64
        )
    ]
    return fixed + randoms


def fuzz_html() -> list[str]:
    """HTML 模糊用例(≥30 变体):空串/空白/良构基线/未闭合与多余闭合标签/
    双开尖括号/10KB 属性与 10KB 文本/坏实体与边界码点/词表命中基线/
    500 层嵌套(闭合与未闭合)/截断 script·style/控制字符/换页符 \\x0c/
    空字节/替换符/未闭合与非常规闭合注释/CDATA/DOCTYPE/处理指令/断引号/
    转义引号/错位嵌套/表格残片/实体洪泛/词表洪泛/10KB base64 混淆块 +
    3 条种子随机标签汤。幂等:两次调用逐字节相同。"""
    rng = _rng("html")
    fixed: list[str] = [
        "",                                            # 空串
        "   \n\t ",                                    # 仅空白
        "<html><body><p>正常段落</p></body></html>",      # 良构基线
        "<html>",                                      # 未闭合
        "</div>",                                      # 多余闭合
        "<<p>double open</p>",                         # 双开尖括号
        "<p " + "a" * _LONG + "='x'>y</p>",            # 10KB 属性名
        "<p>" + "x" * _LONG + "</p>",                  # 10KB 文本节点
        "&amp;&lt;&#xZZ;&#999999999;&unknownent;",     # 坏实体(超上限/未定义)
        "&#x10FFFF;&#x0;&#110000;",                    # 边界/越界码点
        "<title>免费观看 深夜福利</title><p>正文</p>",   # 词表命中(正常路径基线)
        "<div>" * _DEPTH + "深" + "</div>" * _DEPTH,   # 500 层嵌套(闭合)
        "<div>" * _DEPTH,                              # 500 层嵌套(全未闭合)
        "<script>var x = '</scr' + 'ipt>';",           # 截断脚本
        "<style>a{color:red}",                         # 截断样式
        "<p>\x01\x02\x1f</p>",                         # 控制字符
        "\x0c<html><body>\x0c</body></html>",          # 换页符 \x0c
        "<p>\x00</p>",                                 # 空字节
        "<p>\ufffd\ufffd</p>",                         # 坏 UTF-8 替换符
        "<!-- 未闭合注释",                              # 未闭合注释
        "<!-- --!>非常规闭合 -->",                      # 非常规闭合注释
        "<![CDATA[ 结构化数据 ]]>",                      # CDATA 段
        '<!DOCTYPE html PUBLIC "-//W3C//DTD x" "http://x.dtd">',
        "<?php echo 'x'; ?>",                          # 处理指令
        "<a href=x'y>断引号",                           # 断引号属性
        '<img alt="他说:\\"hi\\"" src=x.png>',          # 转义引号属性
        "<textarea></div></textarea>",                 # 错位嵌套
        "<tr><td>1<td>2",                              # 表格残片(隐式闭合)
        "&amp;" * 3000,                                # 实体洪泛
        "色情裸聊博彩" * 200,                           # 词表洪泛(密度边界)
        "<p>" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVlaXYWJj" * 285 + "</p>",  # ~10KB base64
        "<base href='http://example.com/'/><meta charset='utf-8'/>",   # void 元素
        "<svg><path d='M0 0'/></svg>",                 # 自闭合 SVG
    ]
    randoms = _seeded_garbage(rng, "<>/\"'=abcY \x0c\x00\ufffd中", 3, 16, 64)
    return fixed + randoms


def fuzz_yaml() -> list[str]:
    """YAML 模糊用例(≥30 变体,喂给 policy.load_policy):空串/空白/合法基线/
    顶层非列表/制表符缩进/非法 action/重复键/未闭合括号与引号/未定义锚/
    锚与别名/合并键/**反序列化攻击标签(python/object)**/坏指令/双文档/
    10KB 标量/500 层流式与块式嵌套/控制字符/换页符 \\x0c/空字节/替换符/
    CRLF/BOM/±inf·nan/巨浮点与巨负浮点/400 位巨整数/非字符串 name/
    未知判级/非数值阈值/流式规则/二进制标签 + 3 条种子随机乱码。幂等。"""
    rng = _rng("yaml")
    fixed: list[str] = [
        "",                                            # 空串(回退默认政策)
        "   \n\t ",                                    # 仅空白
        "- name: r1\n  action: queue\n",               # 合法基线
        "name: r1\naction: queue\n",                   # 顶层非列表(映射)
        "-\tname: r1\n  action: queue\n",              # 制表符缩进
        "- name: r1\n  action: 不存在动作\n",            # 非法 action
        "- name: r1\n  name: r2\n  action: queue\n",   # 重复键
        "- [1, 2",                                     # 未闭合流式括号
        "- name: \"未闭合引号\n",                        # 未闭合双引号
        "- *undefined_anchor\n",                       # 未定义别名
        "- &a\n- *a\n",                                # 锚定 null 后别名
        "- <<: {a: 1}\n  name: r1\n  action: queue\n", # 合并键
        "!!python/object:os.system []\n",              # 反序列化攻击(SafeLoader 拦截)
        "!!python/object/apply:subprocess.Popen ['cmd']\n",  # 攻击变体
        "%YAML 1.3\n---\n[]\n",                        # 非法版本指令
        "%TAG !e! tag:example.com,2000:\n--- []\n",    # TAG 指令
        "---\n---\n",                                  # 双文档(SafeLoader 拒绝)
        "...\n",                                       # 仅文档结束符
        "- " + "x" * _LONG + "\n",                     # 10KB 标量
        "[" * _DEPTH + "]" * _DEPTH + "\n",            # 500 层流式嵌套
        "".join("  " * i + f"k{i}:\n" for i in range(_DEPTH)),  # 500 层块式嵌套
        "- name: \x01r1\n  action: queue\n",           # 控制字符
        "\x0c- name: r1\n  action: queue\n",           # 换页符 \x0c
        "- name: \x00r1\n  action: queue\n",           # 空字节
        "- name: r\ufffd1\n  action: queue\n",         # 坏 UTF-8 替换符
        "- name: r1\r\n  action: queue\r\n",           # CRLF 行尾
        "\ufeff[]\n",                                  # UTF-8 BOM
        "- .inf\n- -.inf\n- .nan\n",                   # 特殊浮点
        "- 1e40000\n",                                 # 巨浮点(→ +inf)
        "- -1e40000\n",                                # 巨负浮点(→ -inf)
        "- " + "9" * 400 + "\n",                       # 400 位巨整数
        "- name: 123\n  action: queue\n",              # 非字符串 name
        "- name: r1\n  when: {verdict: [nsfw, 胡乱判级]}\n  action: queue\n",  # 未知判级
        "- name: r1\n  when: {min_agg: 不是数字}\n  action: queue\n",  # 非数值阈值
        "- {name: r1, action: queue, note: 流式规则}\n",  # 流式映射规则
        "- !!binary aGk=\n",                           # 二进制标签
    ]
    randoms = [g + ": [" + g + "]\n" for g in
               _seeded_garbage(rng, "-:a\"'[]{}#&*!|>%@` \x0c\ufffd中", 3, 8, 32)]
    return fixed + randoms


def fuzz_json() -> list[str]:
    """JSON 模糊用例(≥30 变体,喂给 response_repair 与 conformal 适配入口):
    空串/空白/空对象与空数组/合法基线/截断/多余闭合/数值字符串/布尔/null/
    裸 NaN·Infinity/-0.0/整型 0 与 -0/float 上界 1e308/越界 1e309→inf/
    巨负 inf/下溢 1e-400/400 位巨整数与巨负整数/500 层数组与对象嵌套/
    混合失衡/10KB 字符串值/10KB 键/字符串内原始控制字符·换页符·空字节/
    替换符/转义空字节/合法与孤立代理对转义/重复键/双顶层对象/裸标量/
    python repr/围栏/校准集(健康 31 对·n<30·元数错误·字符串分值·
    **巨整分值→OverflowError 已登记**)+ 3 条种子随机乱码。幂等。"""
    rng = _rng("json")
    healthy_calibration = (
        "[[" + "],[".join(["0.9, true"] + ["0.1, false"] * 30) + "]]"
    )
    fixed: list[str] = [
        "",                                            # 空串
        "   \n\t ",                                    # 仅空白
        "{}",                                          # 空对象
        "[]",                                          # 空数组
        '{"nsfw_prob": 0.5, "confidence": 0.8}',       # 合法基线
        '{"nsfw_prob": 0.5',                           # 数值中途截断
        '{"nsfw_prob": 0.5}}}',                        # 多余闭合括号
        '{"nsfw_prob": "0.5"}',                        # 分值写成字符串
        '{"nsfw_prob": true}',                         # 布尔分值
        '{"nsfw_prob": null}',                         # null 分值
        '{"nsfw_prob": NaN, "confidence": Infinity}',  # 裸 NaN / Infinity
        '{"nsfw_prob": -0.0}',                         # 负零(float)
        '{"nsfw_prob": 0}',                            # 整型零(边界)
        '{"nsfw_prob": -0}',                           # 整型负零(边界)
        '{"nsfw_prob": 1e308}',                        # float 上界附近
        '{"nsfw_prob": 1e309}',                        # 越界 → +inf
        '{"nsfw_prob": -1e309}',                       # 越界 → -inf
        '{"nsfw_prob": 1e-400}',                       # 下溢 → 0.0
        "1" + "0" * 400,                               # 400 位巨整数
        "-1" + "0" * 400,                              # 400 位巨负整数
        "[" * _DEPTH + "]" * _DEPTH,                   # 500 层数组嵌套
        '{"a": ' * _DEPTH + "1" + "}" * _DEPTH,        # 500 层对象嵌套
        '{"a": [1, {"b": ]}',                          # 混合失衡嵌套
        '{"k": "' + "x" * _LONG + '"}',                # 10KB 字符串值
        '"' + "k" * _LONG + '": 1}',                   # 10KB 键 + 失衡
        '{"k": "a\x01b"}',                             # 串内原始控制字符
        '{"k": "a\x0cb"}',                             # 串内换页符 \x0c
        '{"k": "a\x00b"}',                             # 串内空字节
        '{"k": "\ufffd"}',                             # 替换符(合法 JSON)
        '{"k": "\\u0000"}',                            # 转义空字节
        '{"k": "\\ud83d\\ude00"}',                     # 合法代理对转义
        '{"k": "\\ud83d"}',                            # 孤立代理转义(解码产物含代理项)
        '{"a":1,"a":2}',                               # 重复键
        '{"nsfw_prob": 0.5} {"nsfw_prob": 0.9}',       # 双顶层对象
        "null",                                        # 裸 null
        "true",                                        # 裸 true
        "false",                                       # 裸 false
        "123",                                         # 裸整数
        "-123",                                        # 裸负整数
        "'单引号'",                                     # 非法单引号字符串
        "{'nsfw_prob': True}",                         # python repr 风格
        "```json\n{\"nsfw_prob\": 0.7}\n```",          # Markdown 围栏
        "[[0.9, true], [0.1, false]]",                 # 校准集 n<30(降级路径)
        healthy_calibration,                           # 校准集 n=31(健康路径)
        "[[0.9, true], [0.1]]",                        # 校准项元数错误
        "[[0.9, true], [\"x\", false]]",               # 校准分值非数值
        "[[0.9, true], [" + "9" * 400 + ", false]]",   # 巨整分值(SKIP_LIST 登记)
    ]
    randoms = _seeded_garbage(rng, '{}[]":,0-9.ftre ailun\x0c\ufffd中', 3, 16, 64)
    return fixed + randoms


#: 类别 → 生成器(惰性引用函数对象;all_cases 每次汇出全新列表)
_GENERATORS: dict[str, Callable[[], list[str]]] = {
    "url": fuzz_url,
    "html": fuzz_html,
    "yaml": fuzz_yaml,
    "json": fuzz_json,
}


def all_cases() -> list[FuzzCase]:
    """汇出全量用例矩阵:``[(kind, text), ...]``,kind ∈ url/html/yaml/json。

    每次调用返回全新列表(调用方可自由过滤/改写);顺序固定:
    url → html → yaml → json,各类内部顺序即生成器定义序 + 种子随机尾。
    """
    cases: list[FuzzCase] = []
    for kind, gen in _GENERATORS.items():
        cases.extend((kind, text) for text in gen())
    return cases


# ---------------------------------------------------------------------------
# smoke harness
# ---------------------------------------------------------------------------


def smoke(
    fn: Callable[[str], Any],
    cases: Iterable[FuzzCase | str],
    *,
    allowed: Sequence[type[BaseException]] = DEFAULT_ALLOWED,
) -> dict[str, Any]:
    """对 ``fn`` 逐 case 冒烟:``fn(text)`` 正常返回或抛允许集内异常 → ok。

    - case 既可以是 ``(kind, text)`` 元组(取末位作文本),也可以是裸字符串;
    - 允许集默认 ``(ValueError, RuntimeError, TypeError)``(RecursionError
      ⊂ RuntimeError,深嵌套栈溢出同样体面);
    - 允许集之外的任何 ``BaseException`` 记入 violations(含 RecursionError
      之外的栈塌、OverflowError、AttributeError 等"失控"信号);
    - **不吞 KeyboardInterrupt / SystemExit**:即便调用方把 BaseException
      传进 allowed,这两类也原样上抛(冒烟绝不能吞掉人的 Ctrl-C / 退出)。

    返回 ``{"ran": 执行数, "ok": 体面数, "violations": [(case, exc), ...]}``,
    恒有 ``ran == ok + len(violations)``。
    """
    allowed_tuple = tuple(allowed)
    ran = 0
    ok = 0
    violations: list[tuple[Any, BaseException]] = []
    for case in cases:
        text = case[-1] if isinstance(case, (list, tuple)) else case
        ran += 1
        try:
            fn(text)
            ok += 1
        except (KeyboardInterrupt, SystemExit):
            raise  # 人的中断/退出绝不吞
        except allowed_tuple:
            ok += 1  # 允许集内:体面失败
        except BaseException as exc:  # noqa: BLE001 - 冒烟就是要抓一切失控
            violations.append((case, exc))
    return {"ran": ran, "ok": ok, "violations": violations}


# ---------------------------------------------------------------------------
# 目标注册:懒绑定关键内核入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    """一个待冒烟的内核入口描述(懒绑定:import 发生在 bind_target 时)。

    - key / title:稳定标识与中文名(报告与 CLI 过滤用);
    - module_path / attr:入口所在模块与函数名(**惰性导入**,契约 §2);
    - input_kind:``"text"`` 直喂字符串;``"path"`` 每用例落盘临时文件后
      喂路径(load_policy 吃路径类参数,单独标注);
    - wrap:本模块内的适配工厂名(空 = 直连);conformal.fit_threshold
      消费校准集列表而非 str,经 :func:`fit_threshold_from_json` 适配成
      "JSON 文本 → fit_threshold" 的 str 消费入口;
    - extra_allowed:在该内核允许集之外**追加**的异常类型(有契约依据才加);
    - note:中文备注(报告展示)。
    """

    key: str
    title: str
    module_path: str
    attr: str
    input_kind: str = "text"
    wrap: str = ""
    extra_allowed: tuple[type[BaseException], ...] = ()
    note: str = ""


#: 五个关键内核入口(值对象注册,导入全部延迟到 bind_target):
TARGETS: tuple[TargetSpec, ...] = (
    TargetSpec(
        key="canonical",
        title="域归一内核",
        module_path="netsentinel.intel.canonical",
        attr="canonical_key",
        note="URL 归一入口:任意 URL → 可注册域键或空串",
    ),
    TargetSpec(
        key="text_intel",
        title="文本情报内核",
        module_path="netsentinel.intel.text_intel",
        attr="text_features",
        note="页面文本风险特征:HTML 提取 + 词表命中",
    ),
    TargetSpec(
        key="response_repair",
        title="响应修复内核",
        module_path="netsentinel.vision.response_repair",
        attr="repair",
        note="跨家族 JSON 修复:修不动返回 None,绝不抛出",
    ),
    TargetSpec(
        key="policy",
        title="政策引擎内核",
        module_path="netsentinel.policy.engine",
        attr="load_policy",
        input_kind="path",
        extra_allowed=(ImportError,),
        note="YAML 政策加载(路径类输入:每用例落盘临时文件);"
             "PyYAML 为惰性依赖,缺失时按契约抛 ImportError,故追加允许",
    ),
    TargetSpec(
        key="conformal",
        title="共形决策内核",
        module_path="netsentinel.decision.conformal",
        attr="fit_threshold",
        wrap="fit_threshold_from_json",
        note="共形阈值拟合:经 JSON 适配入口消费 str(校准集文本 → "
             "parse → fit_threshold);适配层只做形状校验(违规一律 "
             "ValueError),内核本身的异常类型原样透传、绝不遮蔽",
    ),
)


@dataclass(frozen=True)
class BoundTarget:
    """懒绑定完成的可冒烟目标:``fn(text)`` 形入口 + 该内核允许集。"""

    spec: TargetSpec
    fn: Callable[[str], Any]
    allowed: tuple[type[BaseException], ...]


def _path_entrypoint(fn: Callable[[str], Any]) -> Callable[[str], Any]:
    """把路径类入口(load_policy)适配成 str 消费入口。

    每次调用:mkstemp 建临时文件 → 用例文本以 UTF-8 落盘 → 调 ``fn(path)``
    → 无论成败删除临时文件。语料不含孤立代理项(见模块 docstring),
    UTF-8 落盘必然成功;落盘/删除本身的 OSError 视为环境故障原样暴露
    (真实故障应当被看见,而不是被冒烟器吞掉)。
    """

    def call(text: str) -> Any:
        fd, path = tempfile.mkstemp(suffix=".yaml", prefix=_TMP_PREFIX)
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            return fn(path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    return call


def fit_threshold_from_json(
    fit_fn: Callable[[list[tuple[float, bool]], float], dict],
    target_precision: float = 0.95,
) -> Callable[[str], dict]:
    """把 ``fit_threshold(calibration, target)`` 适配成消费 str 的入口。

    契约 §2 A136"选择消费 str 的入口":模糊用例是文本,而 fit_threshold
    吃 ``[(分值, 是否真实违规), ...]`` 校准集——本适配把 JSON 数组文本解析
    为校准集后喂入。适配层自身只做形状校验(非数组 / 项非二元组一律
    ValueError),**绝不捕获/转换 fit_threshold 本体的异常**:内核若抛
    允许集之外的类型(如巨整分值触发 OverflowError),原样成为违规被
    冒烟器如实登记——这正是本内核要暴露的东西。

    :param fit_fn: 懒绑定传入的 ``decision.conformal.fit_threshold``;
    :param target_precision: 目标精度(固定 0.95,落在合法开区间内)。
    """

    def call(text: str) -> dict:
        data = json.loads(text)  # 坏 JSON → ValueError(允许集内)
        if not isinstance(data, list):
            raise ValueError(
                f"共形适配入口需要 JSON 数组校准集 [(分值, 是否违规), ...],"
                f"当前顶层类型为 {type(data).__name__}"
            )
        calibration: list[tuple[Any, Any]] = []
        for idx, item in enumerate(data):
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError(
                    f"校准集第 {idx} 项应为 [分值, 是否真实违规] 二元组,"
                    f"收到 {item!r}"
                )
            calibration.append((item[0], bool(item[1])))
        return fit_fn(calibration, target_precision)

    return call


def bind_target(spec: TargetSpec) -> BoundTarget:
    """按描述懒导入并绑定入口:import 仅发生在本调用(契约 §2 惰性导入)。

    text 类直连;path 类套 :func:`_path_entrypoint`;声明了适配工厂的
    (conformal)先套适配。允许集 = 默认集 + 该内核契约内追加项。
    模块缺失 / 属性缺失时抛 RuntimeError(中文)——目标注册表与仓库
    实际结构脱钩属于程序错误,应当立刻暴露而不是静默跳过。
    """
    try:
        module = importlib.import_module(spec.module_path)
        raw = getattr(module, spec.attr)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"懒绑定内核入口失败:{spec.module_path}.{spec.attr}({spec.title}):{exc}"
        ) from exc
    if spec.wrap:
        try:
            factory = globals()[spec.wrap]
        except KeyError as exc:  # pragma: no cover - 注册表自检兜底
            raise RuntimeError(
                f"适配工厂 {spec.wrap!r} 不在本模块内(注册表配置错误)"
            ) from exc
        raw = factory(raw)
    fn = raw if spec.input_kind == "text" else _path_entrypoint(raw)
    allowed = tuple(dict.fromkeys(DEFAULT_ALLOWED + tuple(spec.extra_allowed)))
    return BoundTarget(spec=spec, fn=fn, allowed=allowed)


# ---------------------------------------------------------------------------
# skip 清单:已登记的既有内核违规(不许静默)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownViolation:
    """一条已登记的既有内核违规(**不是**豁免:全量照跑,只做归类呈现)。

    - target_key / kind:违规出现的目标与用例类别;
    - marker:用例文本必须包含的锚片段(在该 (目标, 类别) 范围内唯一定位);
    - reason:根因与修复建议归属(中文,报告原样打印);
    - filed_by:登记依据。
    """

    target_key: str
    kind: str
    marker: str
    reason: str
    filed_by: str


#: skip 清单(别名,呼应契约 §2 A136 的"skip 清单"叫法)
SKIP_LIST: tuple[KnownViolation, ...] = ()
#: 历史登记(已修复,2026-10-02 由负责人按安全内核报告修复后移除):
#: conformal 巨整分值 OverflowError —— decision/conformal.py 已加 _finite_float
#: 加固(溢出/非有限一律 ValueError 拒绝),登记失效故清空;机制保留以登记未来违规。
#: 语义化别名:报告与测试统一引用
KNOWN_VIOLATIONS: tuple[KnownViolation, ...] = SKIP_LIST


def _classify(
    violations: list[tuple[str, FuzzCase, BaseException]],
) -> tuple[
    list[tuple[str, FuzzCase, BaseException, KnownViolation]],
    list[tuple[str, FuzzCase, BaseException]],
    list[KnownViolation],
]:
    """把违规三元组 (目标, 用例, 异常) 归类为(已登记 / 新发现 / 失效登记)。

    登记项匹配条件:target_key 相同、用例类别相同、marker 是用例文本子串。
    未匹配任何违规的登记项进入 stale(内核可能已修复 → 报告提示移除,
    防止 skip 清单无限期遮蔽真实回归)。
    """
    expected: list[tuple[str, FuzzCase, BaseException, KnownViolation]] = []
    unexpected: list[tuple[str, FuzzCase, BaseException]] = []
    matched: set[int] = set()
    for key, case, exc in violations:
        hit: KnownViolation | None = None
        for i, entry in enumerate(SKIP_LIST):
            if (
                i not in matched
                and entry.target_key == key
                and entry.kind == case[0]
                and entry.marker in case[-1]
            ):
                hit = entry
                matched.add(i)
                break
        if hit is not None:
            expected.append((key, case, exc, hit))
        else:
            unexpected.append((key, case, exc))
    stale = [entry for i, entry in enumerate(SKIP_LIST) if i not in matched]
    return expected, unexpected, stale


# ---------------------------------------------------------------------------
# 全量矩阵 / CLI 报告
# ---------------------------------------------------------------------------


def _preview(text: str, limit: int = 48) -> str:
    """用例文本的报表预览:截断 + repr 转义控制字符(repr 对任意 str 安全)。"""
    return repr(text[:limit])


def run_suite(
    cases: Sequence[FuzzCase] | None = None,
    specs: Sequence[TargetSpec] = TARGETS,
) -> dict[str, Any]:
    """跑全量 目标 × 用例 矩阵,返回结构化结果(main 与测试共用)。

    返回键:
        targets:   每目标的 {key, title, ran, ok, violations};
        cases_n / targets_n / total_ran:矩阵规模与总执行数
        (恒有 ``total_ran == cases_n * targets_n``,A138/测试断言依据);
        violations / expected / unexpected / stale_skips:违规全量及归类
        (expected=已登记待修复,stale=登记失配→提示移除,均不静默)。
    """
    case_list = list(all_cases() if cases is None else cases)
    spec_list = tuple(specs)
    per_target: list[dict[str, Any]] = []
    violations: list[tuple[str, FuzzCase, BaseException]] = []
    for spec in spec_list:
        bound = bind_target(spec)
        result = smoke(bound.fn, case_list, allowed=bound.allowed)
        per_target.append(
            {
                "key": spec.key,
                "title": spec.title,
                "ran": result["ran"],
                "ok": result["ok"],
                "violations": result["violations"],
            }
        )
        violations.extend(
            (spec.key, case, exc) for case, exc in result["violations"]
        )
    expected, unexpected, stale = _classify(violations)
    return {
        "targets": per_target,
        "cases_n": len(case_list),
        "targets_n": len(spec_list),
        "total_ran": sum(t["ran"] for t in per_target),
        "violations": violations,
        "expected": expected,
        "unexpected": unexpected,
        "stale_skips": stale,
    }


#: CLI 过滤词合法集(类别 + 目标键);其余参数视为用法错误
_FILTER_KINDS = ("url", "html", "yaml", "json")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI:跑全量 targets × all_cases,打印中文报告;退出码 0(零违规)/ 2。

    用法 ``python -m netsentinel.security.threat_kernel [过滤词...]``:
    过滤词可为用例类别(url/html/yaml/json)或目标键(canonical/
    text_intel/response_repair/policy/conformal),两类可混用,分别过滤
    用例与目标维度;未知过滤词 → 中文用法错误 + 退出码 2。

    退出码语义(诚实口径):**只要存在违规(已登记或新发现)即 2**——
    skip 清单是"如实呈现待修复",不是"放行";内核修复并移除对应登记
    项后自然回到 0。失效登记(stale)不影响退出码,只在报告中提示移除。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    valid = set(_FILTER_KINDS) | {s.key for s in TARGETS}
    bad = [a for a in args if a not in valid]
    if bad:
        print(
            f"[threat_kernel] 未知过滤词:{'、'.join(repr(b) for b in bad)}"
            f"(合法值:{'/'.join(sorted(valid))})"
        )
        return 2

    all_matrix = all_cases()
    kinds_sel = {a for a in args if a in _FILTER_KINDS}
    keys_sel = {a for a in args if a not in _FILTER_KINDS}
    cases = [c for c in all_matrix if not kinds_sel or c[0] in kinds_sel]
    specs = [s for s in TARGETS if not keys_sel or s.key in keys_sel]

    result = run_suite(cases, specs)
    n_viol = len(result["violations"])
    n_expected = len(result["expected"])
    n_unexpected = len(result["unexpected"])

    print("=" * 64)
    print(" 净网哨兵 V7 · 安全内核模糊测试报告(A136 threat_kernel)")
    print("=" * 64)
    print(
        f"用例 {result['cases_n']} 条 × 目标 {result['targets_n']} 个"
        f" = 执行 {result['total_ran']} 次(允许集:{'/'.join(e.__name__ for e in DEFAULT_ALLOWED)}"
        f"{';政策内核追加 ImportError' if any(t['key'] == 'policy' for t in result['targets']) else ''})"
    )
    print()
    for t in result["targets"]:
        print(
            f"[{t['key']}] {t['title']}:ran={t['ran']} ok={t['ok']} "
            f"违规={len(t['violations'])}"
        )
    print("-" * 64)
    if n_viol:
        print(" 违规清单(允许集之外抛出的异常,逐条呈现)")
        for i, (key, case, exc) in enumerate(result["violations"], 1):
            print(
                f"  {i}. [{key}] {case[0]} 用例 {_preview(case[-1])}"
                f" → {type(exc).__name__}: {exc}"
            )
        print()
        print(" skip 清单(已登记的既有内核违规:如实呈现,待修复,不静默)")
        for key, case, _exc, entry in result["expected"]:
            print(f"  - [{entry.target_key}/{entry.kind}] {entry.reason}")
            print(f"    登记依据:{entry.filed_by}")
    else:
        print(" 违规清单:空(全部目标 × 用例均在允许集内体面返回或失败)")
    for entry in result["stale_skips"]:
        print(
            f" [提示] skip 项 [{entry.target_key}/{entry.kind}] 未再命中:"
            f"内核可能已修复,请移除该登记(避免遮蔽真实回归)"
        )
    print("-" * 64)
    if n_viol:
        print(
            f"结论:发现违规 {n_viol} 例(已登记 {n_expected} + 新发现 {n_unexpected})"
            f"——退出码 2"
        )
    else:
        print("结论:零违规——退出码 0")
    return 2 if n_viol else 0


def kernel_selfcheck() -> dict[str, Any]:
    """V7 内核自检(A138 kernel_bench 统一调用;确定性、零墙钟)。

    以**操作计数**自证代差:全量矩阵(用例 × 目标)的总执行数与
    新发现违规数——前者证明覆盖规模(每格恰执行一次),后者是安全
    内核的"应然基线 0"(已登记违规另行单列,不静默)。
    """
    result = run_suite()
    return {
        "name": "threat_kernel",
        "metric": "全量模糊矩阵(用例×内核入口)的新发现违规数",
        "value": len(result["unexpected"]),
        "baseline": 0,
        "total_ran": result["total_ran"],
        "expected_registered": len(result["expected"]),
    }


if __name__ == "__main__":  # pragma: no cover - CLI 手动入口
    raise SystemExit(main())
