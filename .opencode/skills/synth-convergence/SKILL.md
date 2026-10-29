---
name: synth-convergence
description: Synthesis QoR convergence loop — compile, read report_qor, classify the QoR problem (timing/area/power), consult the knowledge base for the strategy (effort bump / boundary_opt / ungroup), and re-compile with extra_tcl; at most 3 rounds, escalate to human if not converged. Use when synthesis WNS/TNS or area misses target and the human asks to close QoR.
---

# Synthesis QoR Convergence (Skills 层)

目标: 综合 QoR (WNS/TNS/area/power) 不达标时, 用"编译 → 读 QoR → 知识库
选策略 → 客制化重编译"的闭环收敛, 而不是手动试错或盲目提 effort。
SPEC 出处: SPEC-evolution-and-domain-upgrade §C.1 FE-3。

## 迭代循环 (每轮)

1. **编译**: `api_synth_compile(effort=<本轮 effort>, extra_tcl=<本轮策略命令>)`
   — 首轮 medium 不带 extra_tcl 作为基线; 策略命令经 extra_tcl 在编译前
   注入 (与 icc2 extra_tcl 先例一致, Genus/DC 同语义)。
2. **读 QoR**: `api_report_qor()` → 归一五字段
   `{wns, tns, area, leakage, total_power}` (FE-1/FE-2, 两工具已归一)。
3. **归类 + 选策略** (先查 KB:
   `hacoo_knowledge_query(agent="synthesis", keyword="QoR")`):

   | QoR 问题类型 | 判定 (对照 target) | 典型策略 | 参考 extra_tcl (Genus 系) |
   |---|---|---|---|
   | timing 不达标 | wns < target | effort medium→high | (effort 参数, 无 extra_tcl) |
   | timing 仍不达标 | 提 effort 后 wns 改善不足 | boundary_opt + ungroup 层次 | `ungroup -flatten [get_cells u_sub]` + `set_db boundary_optimize true` |
   | area 超标 | area > budget | 回 medium + 显式 max_area 约束 | `set_db max_area <budget>` |
   | power 超标 | total_power > budget | leakage 优化 effort / clock gating | `set_db lp_power_optimization_effort high` |

   每轮策略选择必须先记录决策:
   `hacoo_decision_record(agent="synthesis", stage="synthesis",
   action="qor_fix_round<N>", rationale=<本轮 QoR 读数+问题类型+策略>,
   params={"wns": ..., "target_wns": ..., "strategy": ...})`。
4. **判收敛**: `wns >= target_wns` 且 area/power 在预算内 → 记录
   `qor_converged` 决策并退出; 每轮结束 `hacoo_decision_outcome` 闭环
   (metrics 带本轮 wns/tns/area)。

## 终止条件 (任一命中即退出循环)

- **收敛**: wns/tns/area/power 全部达标。
- **预算耗尽**: 达到 max_rounds (默认 3) 仍未收敛 → 升级人工,
  `hacoo_feedback(agent="synthesis", content=<现状+已试策略+QoR 轨迹>)`
  并停止, 不要无限迭代。
- **平台期**: 连续 2 轮 wns 不再改善 → 换策略维度 (时序修不动就查约束
  完整性 SDC / 与 DFT 插入次序), 仍无效则升级人工。
- **指标恶化**: 某轮 wns 更差或 area/power 显著恶化 → 回滚本轮策略,
  记 failed outcome, 换次优策略。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- 每轮只改一个策略变量 (effort 或一类 extra_tcl), 不要一轮混用 —
  否则无法归因哪一步起效。
- QoR 读数必须来自 `api_report_qor` 返回, 禁止估计; Genus/DC 报告格式
  差异已在 adapter 内封装, skill 层只读统一五字段。
- 每轮 WNS 应单调改善 (朝 target 方向); 收敛轨迹写入 rationale 供审计。
- 收敛中沉淀新经验 (如 "本设计 ungroup u_sub 后 WNS 收敛") →
  `hacoo_knowledge_add`; flow 结束后 `hacoo_insights_scan(agent="synthesis")`。
- 综合收敛后提醒移交 dft (scan 插入) 或 apr (netlist 质量直接影响
  floorplan), 保持阶段衔接。

## 验收锚点

bench `synth-conv-01` (mock): 基线 WNS=-0.152 → high effort → -0.081 →
boundary_opt/ungroup → 0.012 (达标), 3 轮内 WNS 单调改善且每轮策略决策
入问责日志。mock 的逐轮改善由 MockTclShell 序列应答建模 (shell.py)。
