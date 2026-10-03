"""netsentinel.agent:案件智能体子包(A41,V3)。

把 GLM 从"打分器"升级为"办案侦探":

- :func:`plan_investigation`:阅读站点扫描报告摘要,由 GLM 规划下一步侦查动作
  (类别假设 + 至多 5 个动作 + 置信度),全程走 vlm_cache 预算(V3 红线 13);
- :func:`apply_plan`:按计划调用注入的侦查回调(缺省惰性接 orchestrator 组件 /
  cascade 分类器),结果合并进 ``report.intel["case_agent"]``,
  verdict / needs_review 只升不降(V2 红线 7);
- :func:`run_case`:run_scan → plan → apply(≤2 轮)→ 升级时重新入列复核队列
  (复用 orchestrator / packager / review_queue / logging_util,不复制实现)。

本包模块仅依赖标准库与共享契约;兄弟模块(glm_adapter / vlm_cache /
vlm_prompts / crawler / classifier_base 等)一律函数内惰性导入 + 注入容错。
"""
from netsentinel.agent.case_agent import apply_plan, plan_investigation, summarize_report
from netsentinel.agent.case_flow import run_case

__all__ = ["plan_investigation", "apply_plan", "summarize_report", "run_case"]
