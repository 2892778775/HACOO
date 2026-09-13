---
name: timing-signoff
description: Timing signoff workflow on HACOO — load design, read libraries, synthesize, report timing, iterate on violations. Use when the human asks to check/fix timing, run STA, or do timing signoff on a design.
---

# Timing Signoff Flow (Skills 层: 工作流知识编码)

Encodes WHEN and IN WHAT ORDER to combine HACOO `api_*` calls for timing
signoff. The MCP layer owns safe tool exposure; this skill owns the flow.

## Task decomposition (高层意图 -> api_* 调用序列)

1. `hacoo_spawn_instance(tool=<timing-capable tool>)` → keep `instance_id` for
   the WHOLE flow (context consistency — never respawn mid-flow).
2. `hacoo_call api_load_design(path, top)` — load the netlist.
3. `hacoo_call api_read_liberty(path)` — one call per required library corner.
4. `hacoo_call api_run_synthesis(effort)` with `async_=true` if slow; poll
   `hacoo_task_status`.
5. `hacoo_call api_report_timing(max_paths=20)` — read WNS/TNS + critical paths.
6. Iterate: if WNS < 0, adjust (higher effort / constraint change) and go to 4.
7. `hacoo_release_instance` when the human confirms the result.

## Input validation (调用前业务合法性检查)

- `path` must be an existing file path the human gave you — ask if missing.
- `top` must be the design's top module name — confirm if ambiguous.
- `effort` ∈ {low, medium, high}; default `medium` unless the human specifies.
- `max_paths` ≤ 50 to keep responses inside context budget.

## Execution order constraints

- NEVER call `api_report_timing` before `api_load_design` (backend raises
  `BACKEND_ERROR: no design loaded` — that's a flow violation, not a tool bug).
- Libraries must be read before synthesis when the flow requires them.

## Session context

- Record `instance_id`, loaded design path/top, and each synthesis run's result
  in the conversation so the human can interrupt/resume at any step.

## Reusable report template

```
Timing signoff — <top> @ <instance_id>
  WNS: <ns>   TNS: <ns>   paths analyzed: <n>
  Worst endpoints: <top-3 endpoint: slack>
  Synthesis runs: <count> (last effort: <effort>)
  Recommendation: <meet | needs <action>>
```
