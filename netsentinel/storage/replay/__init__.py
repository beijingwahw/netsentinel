"""事件账本重放与对账 —— 复核状态的离线重建(NetSentinel · A213)。

对标 event sourcing + saga 的"重放校验"::class:`~netsentinel.storage.event_log.EventLog`
里的 append-only 事件流是**事实**,复核队列 entries 表只是**投影**。本包把
事实重放成投影(每条条目的最终状态 + 完整操作时间线),再与队列现状逐条
对账:

- **一致** → OK 报告(退出码 0):账本与状态互相印证,任何历史时刻可重建;
- **不一致** → 中文差异清单(退出码 2):每条差异给出 ``entry_id`` /
  期望状态(重放)/ 实际状态(队列)/ 首个分歧事件 seq,三类典型成因——

  1. ``事件超前``:账本事件已落盘但状态未生效(先账本后状态的**崩溃窗口**,
     A213 双写的预期语义:事件存在但状态可能滞后——不算数据损坏,重放如实
     报告并给出未生效事件的准确 seq,修复动作是重做该操作或人工裁决);
  2. ``账本缺失``:队列有条目但账本无任何事件(账本启用前的历史数据,或
     账本被整段删改);
  3. ``状态不一致 / 字段不一致``:重放结果与队列现状都无法互相解释(payload
     被篡改、或绕过双写直改了 entries 表)。

纯函数核心 :func:`replay_review_queue` 不直接依赖 decision 包:队列经
``queue_factory``(零参可调用,返回带 ``list()`` / ``close()`` 的队列对象)
注入,storage 层不反向 import 决策层(CLI 入口内惰性导入默认工厂)。

CLI(对齐 :mod:`netsentinel.ops.audit_verify` 惯例)::

    python -m netsentinel.storage.replay data/review_events.db data/review_queue.db
    python -m netsentinel.storage.replay 账本.db 队列.db --json [报告路径]

退出码:0 = 一致;1 = 输入错误(文件缺失/路径是目录/账本损坏到无法打开);
2 = 检出差异(含账本 payload 非法 JSON——疑似篡改)。

事件超前补齐(A222 · ``--apply-ahead``,半自动裁决)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead          # 预览
    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --yes    # 补齐

"事件超前"不是数据损坏(先账本后状态的崩溃窗口,A213 双写的预期语义),
但差异需要消除:``--apply-ahead`` 检出后给出**逐条重做**的补齐动作清单
(entry_id / 事件 / 将达状态,重做备注取自事件 payload 以保证字段对账归零)。
红线:**绝不静默自动改状态**——

- 不带 ``--yes``:仅打印将执行的动作清单(含"不可自动补齐,需人工裁决"
  项),不改动任何状态,退出码 0;
- 带 ``--yes``:先复跑对账打印差异,再打印全量动作清单(先全量校验后
  逐条执行,任何校验失败绝不半途 apply),经注入的 queue apply 回调逐条
  重做(幂等:执行前先查当前状态,已到重放链目标步则跳过并标注),执行
  后自动复跑对账验证归零;无超前差异 → 无动作退出 0。
- 退出码:0 = 预览 / 补齐后归零 / 无差异无动作;2 = 补齐失败(回调失败、
  校验失败、或复跑对账差异未归零);1 沿用输入错误语义。

补齐回调**注入式**(本包不 import decision 包,对齐 A213 的依赖方向,
CLI 入口惰性导入默认实现):默认回调用**不接账本**的队列连接重做
``approve / reject / mark_submitted``——事件早已在账本,补的只是 entries
状态投影,重做不得重复记账。

交互确认(A234 · ``-i/--interactive``)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead -i

``--yes`` 之外的第三种确认口径:全量动作清单**先打印**,随后终端逐条
``y/n`` 确认(:func:`input` 阻塞,对齐 HUMAN_GATE 的 input 惯例)——仅
``y/Y`` 执行该条,``n`` / 空回车 / 乱码 / EOF 一律跳过并如实标注(红线:
交互确认绝不静默自动执行);非 TTY 环境中文拒绝并提示改用 ``--yes``,
``--yes`` 与 ``--interactive`` 互斥;执行后同样复跑对账。跳过留下的差异
如实保留(退出码按"差异是否归零"判定,与 ``--yes`` 同口径)。

四眼留痕(approvals)投影对账(A234,注入式启用)::

``replay_review_queue`` 新增仅关键字参数 ``four_eyes_factory``(默认
``None`` = 现状,不启用该段,向后兼容)。注入后重放 ``four_eyes_*``
事件重建 approvals 投影(``{entry_id: [第一人, 第二人]}``),与队列
``status()`` 读出的实际留痕对账:事件超前(账本已记双人齐但 approvals
表缺)→ ``留痕滞后`` 差异(中文,**不阻塞** entries 状态对账结论与退出
码,独立清单);非前缀缺失的互相不可解释 → ``留痕不一致``。事件类型
常量本地声明(字面值与 ``decision.four_eyes`` 一致),storage 层依旧不
反向 import 决策层;CLI 以 ``--four-eyes`` 开关装配默认工厂。

留痕滞后补齐(``--heal-approvals``,独立开关 · A234 报告建议的落地)::

    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --heal-approvals         # 预览(两清单)
    python -m netsentinel.storage.replay 账本.db 队列.db --apply-ahead --yes --heal-approvals   # 补齐 + 补写

对账能报"留痕滞后"但修复此前仍是人工:``--heal-approvals`` 把补写留痕
纳入半自动裁决,是**独立于 entries 状态补齐的第二条确认链**:

- 独立开关(默认关):只加 ``--yes`` 不加 ``--heal-approvals`` 时 approvals
  **一字不动**(两清单两确认,绝不捆绑);开关蕴含 ``--four-eyes`` 的
  approvals 投影对账,且仅在与 ``--apply-ahead`` 搭配时有效;
- dry(不带 ``--yes`` / ``-i``):打印将补写的 approvals 行(entry_id /
  将补 reviewer / 来源事件 seq),零改动;
- ``--yes --heal-approvals``(或 ``-i`` 逐条 y):经注入回调逐行补写——
  幂等预检(留痕已有该 reviewer 则跳过并标注),每行**先在账本补记
  ``approvals_healed`` 审计事件、署名 ``actor="replay-heal"``**(与真人
  复核署名可区分——防伪造留痕:后续审计恒能看出该行是重放补写而非
  真人点击),再写 approvals 行(``acted_at`` 取来源事件 ts,确定性零
  墙钟);单写人路径(事件只带一名 reviewer)自然只补一人;补写后复跑
  approvals 对账验证归零(独立清单,仍不影响状态对账退出码;回调失败
  与 entries 补齐同口径——中止剩余、如实报告、退出 2);
- 无滞后差异 → approvals 无动作;账本校验失败(疑似篡改)→ 一切自动
  补齐整体拒绝(不按可疑事实补写留痕)。

时间戳字段(created_at / updated_at)**不参与对账**:双写时事件与条目各取
一次秒级时钟,合法跨秒差不代表事实分歧(文档化豁免,避免误报)。

零第三方依赖;全部离线;确定性(同一账本+同一队列 → 同一报告,不含墙钟)。

包布局(V? · 由单文件 :mod:`netsentinel.storage.replay` 拆分,逐字搬运
不改逻辑;``from netsentinel.storage.replay import X`` 与
``from netsentinel.storage import replay`` 两种用法完全兼容)::

- ``_common``:常量 / 报告数据类 / 队列工厂注入接缝(:func:`set_queue_factories`
  ——解 storage→decision 架构环的接缝,未注入时保持惰性导入默认行为,
  **不要求调用方现在改**);
- ``projection``:投影重建(entries / approvals 投影纯函数);
- ``reconcile``:对账(差异判定 + 主入口 :func:`replay_review_queue`);
- ``heal``:补齐 / 留痕补写(计划 + 执行纯函数);
- ``cli``:``main`` 与参数解析 / 人读与机器报告 / 默认回调 / 交互确认。

崩溃窗口处置指引(review_queue.add 两步 commit 间崩溃的"条目不存在"
场景):账本有 ``entry_added`` 但 entries 无该行时,重放**不自动重建条目**
(entries 为 AUTOINCREMENT 主键,重做入列无法保证取回同一 entry_id,自动
补齐反而制造新差异),差异报告与补齐清单会输出**可执行的中文处置指引**
(该条目需人工重建或重扫:核对账本事件 payload 后重扫入列,或人工裁决
放弃该编号)。
"""
from __future__ import annotations

from netsentinel.storage.replay._common import (
    AHEAD_ACTION_APPROVE,
    AHEAD_ACTION_REJECT,
    AHEAD_ACTION_SUBMIT,
    AHEAD_ACTIONS_CN,
    AHEAD_EVENT_ACTIONS,
    APPROVAL_DIFF_INCONSISTENT,
    APPROVAL_DIFF_LAGGING,
    ApplyAheadOutcome,
    AheadAction,
    ApprovalDiff,
    ApprovalHealAction,
    EVENT_APPROVALS_HEALED,
    EXIT_INPUT_ERROR,
    EXIT_MISMATCH,
    EXIT_OK,
    EntryDiff,
    FOUR_EYES_EVENT_APPROVED,
    FOUR_EYES_EVENT_AWAITING,
    FOUR_EYES_EVENT_TYPES,
    HEAL_ACTOR,
    HealApprovalsOutcome,
    ProjectedApprovals,
    ProjectedEntry,
    ReplayReport,
    StatusStep,
    TimelineItem,
    make_four_eyes_queue,
    make_review_queue,
    reset_queue_factories,
    set_queue_factories,
)
from netsentinel.storage.replay.cli import _stdin_is_tty, main
from netsentinel.storage.replay.heal import (
    execute_apply_ahead,
    execute_heal_approvals,
    plan_apply_ahead,
    plan_heal_approvals,
)
from netsentinel.storage.replay.projection import (
    build_approvals_projection,
    build_projection,
)
from netsentinel.storage.replay.reconcile import (
    reconcile_approvals,
    replay_review_queue,
)

__all__ = [
    "AHEAD_ACTION_APPROVE",
    "AHEAD_ACTION_REJECT",
    "AHEAD_ACTION_SUBMIT",
    "AHEAD_ACTIONS_CN",
    "AHEAD_EVENT_ACTIONS",
    "APPROVAL_DIFF_INCONSISTENT",
    "APPROVAL_DIFF_LAGGING",
    "ApplyAheadOutcome",
    "AheadAction",
    "ApprovalDiff",
    "ApprovalHealAction",
    "EVENT_APPROVALS_HEALED",
    "EXIT_INPUT_ERROR",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EntryDiff",
    "FOUR_EYES_EVENT_APPROVED",
    "FOUR_EYES_EVENT_AWAITING",
    "FOUR_EYES_EVENT_TYPES",
    "HEAL_ACTOR",
    "HealApprovalsOutcome",
    "ProjectedApprovals",
    "ProjectedEntry",
    "ReplayReport",
    "StatusStep",
    "TimelineItem",
    "build_approvals_projection",
    "build_projection",
    "execute_apply_ahead",
    "execute_heal_approvals",
    "main",
    "make_four_eyes_queue",
    "make_review_queue",
    "plan_apply_ahead",
    "plan_heal_approvals",
    "reconcile_approvals",
    "replay_review_queue",
    "reset_queue_factories",
    "set_queue_factories",
]
