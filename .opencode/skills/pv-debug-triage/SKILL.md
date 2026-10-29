---
name: pv-debug-triage
description: PV violation debug triage — cluster DRC/LVS violations by rule and region, consult the knowledge base to judge real layout problems (dispatch to apr) versus rule-deck/setup problems (dispatch to flow), and emit a dispatch table with a decision record per cluster. Use when a PV report has many violations and the human asks where to start.
---

# PV Debug Triage (Skills 层)

目标: PV 报告动辄几百条违例, 工程师最耗时的一步是"分派"——哪些是真
版图问题 (移交 apr 修), 哪些是 rule deck / 设置问题 (移交 flow 修)。
本 skill 把分派标准化: 聚类 → 判定 → 分派表, 每条分派都有问责决策。
SPEC 出处: SPEC-evolution-and-domain-upgrade §C.4 SO-6。

## 流程

1. **取违例**: `api_run_drc` (DRC) / `api_run_lvs` (LVS) 返回结构化违例
   清单 (SO-4 parse_rve / parse_lvs_report); `violations: [{rule, count}]`。
2. **聚类**: 按 rule 聚类 (同一 rule 的违例通常同根因); 若报告含坐标
   再按区域二次聚类 (同区域集中 → 局部布线资源问题, 倾向真实问题)。
3. **判定** (先查 KB: `hacoo_knowledge_query(agent="pv", keyword="triage")`,
   再按下表; 判不准时标 `dispatch: "human"` 升级):

   | 特征 | 判定 | 分派 |
   |---|---|---|
   | 违例数量随版图改动变化 / 集中在布线密集区 | 真实版图问题 | `apr` (widen/reroute/shift) |
   | 违例量级在精度边缘 (如 0.00x um) / 全芯片均匀散布 | deck 精度/单位设置 | `flow` (查 deck 版本与单位) |
   | 换 deck 版本后违例数突变 | deck 问题 | `flow` |
   | foundry 确认的伪违例 (密度填充边界等) | waiver | 走 SO-5 waiver 流程 (**先 decision_record 再 api_calibre_waive**) |
   | 证据不足 | 不猜 | `human` 升级 |

4. **分派表 + 决策**: 每个聚类一条
   `hacoo_decision_record(agent="pv", stage="pv_triage",
   action="dispatch", rationale=<聚类特征+判定依据+KB 命中>,
   params={"rule": ..., "count": N, "dispatch": "apr"|"flow"|"human"})`;
   汇总一条 `triage_done` 决策 (params 带各分派方向的计数)。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 分派必须给证据 (计数/分布/KB 条目), 禁止无依据猜测;
  拿不准就 `human`, 错误的自动分派比不分派更贵。
- waiver 不是 triage 的默认出口: 只有 foundry/设计负责人确认的伪违例
  才走 waiver, 且必须走 SO-5 的强制决策流程。
- 新的分派判据 (如某 deck 版本误报模式) 沉淀 `hacoo_knowledge_add`;
  flow 结束后 `hacoo_insights_scan(agent="pv")`。

## 验收锚点

bench `pv-triage-01` (mock): 混合违例 (M1.S.2 布线间距 -> apr;
VIA1.R.1 精度边缘 -> flow) 分派正确, 每条分派决策入问责日志。
