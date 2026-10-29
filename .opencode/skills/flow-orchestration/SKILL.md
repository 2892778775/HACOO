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
4. **监视**: `hacoo_flow_status(run_id)`; ISF 侧 flow 由 flow agent 经
   `api_isf_setup`(激活 TO 项目) → `api_isf_fw_start` 启动, 用
   `api_isf_flow_status` / `api_isf_flows` 跟踪 (flow agent 域内)。
5. **复盘**: 见 self-improvement skill — 每个失败 stage 都要沉淀知识库条目。

## ISF 对接要点 (flow agent 必读)

- `fw start` 是"阻塞式初始化": 启动阶段占用会话 (从 ISF central 拉取
  flow 内容, 冷项目可能几分钟), Airflow 接管后终端释放 — 同步调用即可,
  默认 timeout 1800s; 冷项目首次启动建议 `async_=true` 后台执行。
- 启动 log 中的 Airflow GUI URL 会被解析到返回的 `airflow_urls`,
  主动报告给设计人员 (人可在 GUI 上同步监视)。
- Airflow 接管后用 `api_isf_flow_status(flow_id)` (底层 `fw jobs`) 跟踪;
  其他 ISF 命令 (停止/版本查询等) 走 `api_isf_run` 泛命令通道。

## 约束

- 被禁 Agent 的 stage 会让整个 run 失败 (AGENT_DISABLED) — 这是设计意图,
  不是 bug; 启动前先对齐启用集合。
- flow 内所有 stage 共享一个后端实例, 状态跨 stage 连续 — 不要中途
  spawn 新实例。

## 问题定位 (出问题时)

`hacoo_flow_trace(run_id)` 返回: run manifest + 决策时间线 (哪个 Agent /
什么决策 / 依据 / 结果) + 审计条目 (request_id 级)。先看第一个
status=failed 的 stage, 再看对应 decision 的 rationale 是否成立。
