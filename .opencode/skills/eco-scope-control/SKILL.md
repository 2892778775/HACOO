---
name: eco-scope-control
description: ECO scope control — before applying any ECO, check whether the affected region is frozen; prefer spare-cell / metal-only fixes; when a freeze region would be touched, refuse to act directly and produce an alternative plan with a decision record. Use during any late-stage ECO (timing/DRC/IR) after block freeze, typically inside an eco_loop iteration.
---

# ECO Scope Control (Skills 层)

目标: tapeout 后期的 ECO 必须受控 —— 动了 freeze 区域 = 已签核内容
失效 (灾难性错误)。本 skill 是 EC-1 eco_loop 每轮迭代内的前置闸门:
先查影响范围, 再决定动不动、怎么动。
SPEC 出处: SPEC-evolution-and-domain-upgrade §C.5 EC-2 (依赖 EC-1)。

## 每轮 ECO 前的检查序列

1. **影响范围提取**: 从诊断结果取受影响 instance/net 的坐标区域
   (bbox) — 数据源: sta 报告路径 / pv 违例坐标 / irem hotspot 坐标。
2. **freeze 比对**: 与 block freeze 清单 (freeze regions / 已签核
   macro / PG 网格) 比对; freeze 清单来自项目配置或 KB
   (`hacoo_knowledge_query(agent="apr", keyword="freeze")`)。
3. **分支**:

   | 情形 | 动作 |
   |---|---|
   | 不触及 freeze | 正常执行 ECO (eco_loop 循环体内) |
   | 触及 freeze | **拒绝直接动手**: 记录 `action="eco_rejected"` 决策 (params 带 `freeze_overlap: true` 与拒绝理由), 转替代方案 |
   | 无法判定 (缺 freeze 清单) | 不猜, `hacoo_feedback` 升级人工确认 |

4. **替代方案优先级** (metal-only 优先, 对 freeze 区域零扰动):
   1. **spare cell 替换**: 用预撒 spare cell 实现 buffer/resize,
      不动已有单元 (metal-only);
   2. **Vt swap**: 换阈值电压调驱动, metal-only;
   3. **走线层调整**: 只动金属层 (widen/shield/shift layer);
   4. 以上都不可行 → 记录 `eco_blocked` 决策并升级人工,
      由 PM 决定是否解冻 (解冻是项目级决策, 不是 agent 权限)。

5. **决策记录 (硬性)**:
   `hacoo_decision_record(agent="apr", stage="eco_scope",
   action="eco_rejected"|"eco_alternative"|"eco_applied",
   rationale=<影响范围+freeze 比对结果+方案>,
   params={"freeze_overlap": bool, "strategy": ..., "bbox": ...})`。

## 纪律 (与 _AGENT_TEMPLATE 一致)

- freeze 比对证据必须写入 rationale (哪个 freeze 区、重叠多少),
  禁止"看起来没影响"式跳过。
- spare cell / metal-only 方案用尽前, 不得提议动 freeze 区域。
- 解冻 (unfreeze) 永远不在 agent 权限内 — 只输出 `eco_blocked` +
  人工升级, 由 PM 决策。
- 替代方案执行后回到 eco_loop 的验证步骤 (增量 STA/PV), 确认无
  副作用; freeze 区邻近的 net 修后必须增量 DRC (移交 pv)。

## 验收锚点

bench `eco-scope-01` (mock): 受影响区域与 freeze 重叠 -> 直接 ECO
被拒绝 (freeze_overlap=true) -> 替代方案 (spare cell + Vt swap,
metal-only) 输出并执行 -> 全部决策入问责日志。
