---
name: self-improvement
description: Agent self-growth loop — after each flow run (especially failures), record lessons into the owning agent's knowledge base and verify decisions are closed with outcomes. Use at the end of any flow run or when a problem was diagnosed and fixed.
---

# Self-Improvement Loop (Skills 层)

Agent 自我成长的标准动作, 每次 flow 实践后执行:

## 1. 复盘决策链

`hacoo_flow_trace(run_id)` 或 `hacoo_decision_query(run_id=...)`:
- 所有 status=pending 的 decision 必须闭环 (`hacoo_decision_outcome`)
- status=failed 的 decision 是知识库候选

## 2. 沉淀新经验 (只记录"不曾遇到的问题")

对每个新失败/新发现, 调用 `hacoo_knowledge_add`:

```
agent       = 责任 Agent (谁的决策导致/解决了问题)
symptom     = 问题现象 (可检索的关键词, 如 "WNS 在 high effort 后恶化")
root_cause  = 根因
resolution  = 解决方案与参数
tags        = 领域标签, 如 ["sta", "congestion", "3dic"]
run_id      = 关联的 flow run
```

**先查后写**: `hacoo_knowledge_query(agent, keyword)` 已有等价条目时不要
重复添加 — 复用计数 (reuse_count) 会自然让高价值经验排在前面。

## 3. 验证成长

下次遇到同类问题时, 对应 Agent 的 prompt 会强制先查 KB — 若查询命中并
指导了正确决策, 说明闭环有效。

## 反模式

- 不要把 KB 当日志用: 只沉淀"可复用的因果知识", 不记录一次性事件。
- 不要把 A Agent 的经验写进 B Agent 的库: 知识按 Agent 隔离。
