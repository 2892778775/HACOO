---
name: route-drc-convergence
description: Post-route DRC convergence loop — route, read the DRC report, classify violations by type (short/spacing/antenna), pick the targeted fix strategy (reroute/widen/via-pillar), and iterate until the DRC count is under threshold or the round budget is exhausted. Use when route_opt leaves DRC violations and the human asks to clean them up.
---

# Route DRC Convergence (Skills 层)

目标: route 后 DRC 不收敛时, 用"分类 → 针对性策略 → 迭代"的闭环把
DRC 数压到阈值以下, 而不是无脑重复 route_opt。
SPEC 出处: SPEC-evolution-and-domain-upgrade §C.3 BE-4。

## 迭代循环 (每轮)

1. **布线 + 报告**: `api_route_opt(extra_tcl=<本轮策略命令>)` — 返回 data 自带
   `drc_count` (BE-2, report_design -drc / verify_drc 解析), 首轮不带
   extra_tcl 作为基线。
2. **分类**: 从报告文本拆分 short / spacing / antenna 三类计数
   (ICC2 `report_design -drc` / Innovus `verify_drc` 均带分类行),
   确定 dominant type = 计数最大的类别。
3. **选策略** (先查 KB: `hacoo_knowledge_query(agent="apr", keyword="DRC")`):

   | DRC 类型 | 典型根因 | 修复策略 | mock/参考命令 (ICC2 系) |
   |---|---|---|---|
   | short | 局部绕线资源耗尽, 邻网重叠 | reroute 违例网 | `route_eco -reroute modified_nets_only` |
   | spacing | 线距不足 / 走线过密 | widen 或 respace 后增量布线 | 加宽违例网段 → `route_eco` |
   | antenna | 长线段电荷累积 | via-pillar / 插 diode / 跳层 | `insert_via_pillars` → `route_eco` |

   策略选择必须先记录决策: `hacoo_decision_record(agent="apr", stage="route",
   action="drc_fix_round<N>", rationale=<分类计数+主类型+策略>,
   params={"drc_total": N, "dominant_type": ..., "strategy": ...})`。
4. **判收敛**: `drc_count <= threshold` (默认 0, 项目可放宽) → 记录
   `drc_converged` 决策并退出; 每轮结束 `hacoo_decision_outcome` 闭环
   (metrics 带本轮 drc_count)。

## 终止条件 (任一命中即退出循环)

- **收敛**: drc_count <= threshold。
- **预算耗尽**: 达到 max_rounds (默认 5) 仍未收敛 → 升级人工,
  `hacoo_feedback(agent="apr", content=<现状+已试策略>)` 并停止,
  不要无限迭代。
- **平台期**: 连续 2 轮 drc_count 不再下降 → 换策略维度
  (如 short 修不动就回头查 floorplan/congestion, 移交 fp-exploration),
  仍无效则升级人工。
- **指标恶化**: 某轮 drc_count 上升 → 回滚本轮策略, 记 failed outcome,
  换次优策略。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 每轮只改一类主违例, 不要一轮混用多策略 — 否则无法归因哪一步起效。
- 分类计数与 drc_count 必须来自后端返回, 禁止估计; Innovus 与 ICC2 的
  报告格式差异已在 adapter 内封装, skill 层只读统一字段。
- 修复中沉淀新经验 (如 "本 block antenna 集中在顶层长线") →
  `hacoo_knowledge_add`; flow 结束后 `hacoo_insights_scan(agent="apr")`。
- DRC 收敛后若动过 PG/时钟区域, 提醒移交 pv/irem 做增量验证 (C.5/C.6)。

## 验收锚点

bench `apr-route-drc-loop-01` (mock): 基线 DRC=5 → reroute → 3 →
widen → 1 → via-pillar → 0, 断言逐轮下降且每轮策略决策入问责日志。
mock 的逐轮递减由 MockTclShell 序列应答建模 (shell.py)。
