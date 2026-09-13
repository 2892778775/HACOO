---
name: flow-orchestration
description: Orchestrate a multi-agent design flow on HACOO — pick a template (or compose stages), verify the participating agents are enabled, run the flow, and trace results. Use when the human asks to run an end-to-end or partial design flow (e.g. "只让 STA 和 APR 参与").
---

# Flow Orchestration (Skills 层)

## 编排步骤

1. **确认参与集合**: `hacoo_list_agents` 查看哪些 Agent 处于 enabled 状态。
   若人类指定"本次只要 STA、APR 参与", 提醒管理员执行:
   `python -m hacoo agents disable <name>` 后 `agents sync`。
2. **选模板**: `hacoo_list_flow_templates` (或 `hacoo_call api_list_flow_templates`),
   常用: `block_signoff` (全流程) / `sta_only` (仅 STA)。
3. **启动**: `hacoo_flow_start(template, params)` → 拿 `run_id`。
   长流程用 `hacoo_call api_flow_start` 加 `async_=true` 并轮询。
4. **监视**: `hacoo_flow_status(run_id)`; ISF/Airflow 侧用
   `api_isf_flow_status` / `api_isf_airflow_dags` (flow agent 域内)。
5. **复盘**: 见 self-improvement skill — 每个失败 stage 都要沉淀知识库条目。

## 约束

- 被禁 Agent 的 stage 会让整个 run 失败 (AGENT_DISABLED) — 这是设计意图,
  不是 bug; 启动前先对齐启用集合。
- flow 内所有 stage 共享一个后端实例, 状态跨 stage 连续 — 不要中途
  spawn 新实例。

## 问题定位 (出问题时)

`hacoo_flow_trace(run_id)` 返回: run manifest + 决策时间线 (哪个 Agent /
什么决策 / 依据 / 结果) + 审计条目 (request_id 级)。先看第一个
status=failed 的 stage, 再看对应 decision 的 rationale 是否成立。
