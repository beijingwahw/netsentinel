"""声明式政策引擎包(A49):YAML 政策 → 规则 → 分流决策。

用法::

    from netsentinel.policy import decide, describe_actions, load_policy

    rules = load_policy(cfg.policy_path)   # 缺文件 → 内置默认 queue-all
    decision = decide(report, rules)       # 首条命中;未命中 → 兜底 queue
    print(describe_actions())              # 各 action 的中文说明

红线(CONTRACTS-V3 §0 红线 11):政策只能增加审批环节,任何 action
都不能跳过人工门;four_eyes 表示在人工门之上追加第二审核人。
"""
from netsentinel.policy.engine import (
    VALID_ACTIONS,
    VALID_WHEN_KEYS,
    Decision,
    Rule,
    decide,
    default_rules,
    describe_actions,
    load_policy,
)

__all__ = [
    "VALID_ACTIONS",
    "VALID_WHEN_KEYS",
    "Decision",
    "Rule",
    "decide",
    "default_rules",
    "describe_actions",
    "load_policy",
]
