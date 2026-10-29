# HACOO 持续进化与领域能力升级 SPEC

> 目标读者：Kimi Code（实现方）。
> 前置依赖：`docs/SPEC-agent-growth-loop.md`（反馈入口 / 观测 / insight 机制）。
> 本 SPEC 分三部分：
> - **Part A（代码实现）**：用户反馈 → 升级提案 → Agent/Skill 版本化更新 的管理入口
> - **Part B（代码实现）**：升级验证体系 —— 兼容性测试 + 能力基准测试，
>   用数据证明"升级确实有效、且没有破坏旧行为"
> - **Part C（领域升级 backlog）**：从数字芯片设计专家视角，对 front-end /
>   back-end / signoff / ECO / 自动化收敛各领域既有功能的反思与升级建议，
>   按优先级排入 backlog，每项带验收标准
>
> 风格约定与现状锚点沿用 `SPEC-agent-growth-loop.md` §0（append-only JSONL、
> 结构化错误码、测试沙箱、五处同步：registry/gateway/mcp/sdk/cli），不再重复。

---

# Part A：反馈与升级管理入口（Upgrade Management）

## A.1 动机

平台要"不断成长"，光有零散反馈不够——反馈必须汇聚成**可追踪的升级提案**，
升级必须落到**带版本号的 Agent/Skill 变更**上，否则无法回答：
"这个 Agent 相比上个月变强了吗？哪次变更让它变强的？"

## A.2 新模块 `hacoo/upgrades.py`

升级提案存储：`logs/upgrade-proposals.jsonl`（append-only + 读-改-写更新状态）。

```json
{
  "proposal_id": "up-<10hex>",
  "ts": 1760800000.0,
  "title": "STA agent 需要支持 MCMM 场景管理",
  "kind": "agent_capability | skill_new | skill_update | knowledge_bulk | adapter_method | platform_feature",
  "target": "sta",                     // 受影响对象 (agent 名 / skill 名 / tool 名 / "platform")
  "motivation": "反馈/问题的原始描述",
  "source": "human | insight | review",// 来源: 人工提出 / insight 转化 / 复核结论
  "source_ref": "ins-xxx 或 kb-xxx 或 null",
  "proposed_change": "期望行为的可验收描述",
  "acceptance": "bench case sta-mcmm-01 通过",   // 验收标准 (必填, 否则不允许 accept)
  "status": "proposed | accepted | in_progress | done | rejected",
  "version_after": "sta@1.3.0 或 null",          // done 时填写落地版本
  "history": [{"ts": ..., "from": "proposed", "to": "accepted", "note": "..."}]
}
```

```python
class UpgradeStore:
    def __init__(self, path: str) -> None: ...
    def propose(...) -> dict
    def transition(self, proposal_id, to_status, note="", version_after=None) -> dict
        # 合法流转: proposed -> accepted|rejected; accepted -> in_progress|rejected;
        # in_progress -> done|accepted(回退); done 为终态
        # accept 前强制检查 acceptance 非空, 否则 ARGUMENT_ERROR
    def query(self, status=None, target=None, limit=100) -> list
```

## A.3 Agent/Skill 版本化

1. `hacoo/agents/registry.py` 的 AgentSpec 增加 `version: str = "1.0.0"` 字段
   （语义化版本）；`hacoo.agents.json` 支持覆盖版本号；`agents sync` 生成的
   `.opencode/agent/hacoo-*.md` frontmatter 写入 `version: x.y.z`
2. 版本 bump 约定：知识库新增条目 → patch；新增/修改 skill 或白名单 → minor；
   Agent 职责变化 → major
3. 知识库条目（`SPEC-agent-growth-loop` §2.1 已扩展 schema）增加
   `agent_version: str` 字段，记录条目沉淀时的 Agent 版本——便于按版本
   归因"哪个版本的经验"

## A.4 新通用方法（五处同步）

| 方法 | capability | 参数 | 行为 |
|---|---|---|---|
| `api_upgrade_propose` | write | `title`(req), `kind`(req), `target`(req), `motivation`(req), `acceptance`(req), `proposed_change`, `source`(默认 human), `source_ref` | 创建提案 |
| `api_upgrade_list` | read | `status`, `target`, `limit` | 查询提案（默认按 ts 倒序） |
| `api_upgrade_update` | write | `proposal_id`(req), `to_status`(req), `note`, `version_after` | 状态流转（含合法性与 acceptance 检查） |

MCP：`hacoo_upgrade_propose` / `hacoo_upgrade_list` / `hacoo_upgrade_update`。
SDK：`client.upgrade_propose(...)` / `client.upgrade_list(...)` / `client.upgrade_update(...)`。
CLI：

```
python -m hacoo upgrade propose --kind agent_capability --target sta \
    --title "..." --motivation "..." --acceptance "..."
python -m hacoo upgrade list [--status proposed]
python -m hacoo upgrade update --id up-xxx --to accepted
```

## A.5 与成长闭环的衔接

- insight 确认时（`api_insight_review` confirm）可选 `create_upgrade=true`
  → 自动从 insight 生成提案（`source="insight"`, `source_ref=insight_id`）
- 复核队列中 reject 的知识条目，若 note 表明是系统性问题，鼓励（prompt 层面）
  orchestrator 转为升级提案

---

# Part B：升级验证体系（Validation）

## B.1 双层验证模型

任何升级（Agent/skill/adapter/平台）落地后，必须过两层：

| 层 | 回答的问题 | 机制 | 通过标准 |
|---|---|---|---|
| **兼容性层** | 旧行为有没有被破坏？ | 契约快照测试 + 现有 e2e 回归 | 全部通过，契约无未声明的删除/改名 |
| **能力层** | 新能力是否真的有效？ | 能力基准（golden cases）前后对比 | 目标 case 从 fail→pass；整体通过率不回退 |

## B.2 契约快照测试（兼容性层，代码实现）

新模块 `hacoo/compat.py` + 快照文件 `tests/contracts/`：

1. **方法契约快照**：对网关注册表全量 `api_*` 方法导出
   `{name: {params: {p: {type, required}}, capability, kind, tool}}`，
   序列化为 `tests/contracts/methods-snapshot.json` 入库
2. **MCP 工具快照**：`mcp_server.TOOLS` 的 name + inputSchema 导出为
   `tests/contracts/mcp-snapshot.json` 入库
3. **新测试 `test_contract_snapshot`**：每次跑 e2e 时重新导出并与快照 diff——
   - **删除/改名方法、删除必填参数、参数类型变化** → 测试失败（破坏性变更必须
     显式更新快照并在 commit message 声明 BREAKING）
   - 新增方法/新增可选参数 → 测试给出提示并自动更新快照（宽松方向）
4. CLI：`python -m hacoo compat snapshot`（手动重建快照）/
   `python -m hacoo compat check`（只校验不更新）

## B.3 能力基准（能力层，代码实现）

### B.3.1 目录结构

```
benchmarks/
  sta/
    sta-wns-analysis-01.json      # 一个 golden case
    sta-mcmm-01.json
  apr/
    apr-route-drc-loop-01.json
  ...
baselines/                       # 基准成绩（入库, git 追踪）
  sta@v1.2.0.json
bench_runs/                      # 每次运行结果 (.gitignore, 运行时数据)
  20261018-193000.json
```

### B.3.2 Golden case schema

```json
{
  "id": "sta-wns-analysis-01",
  "agent": "sta",
  "description": "给定带 setup violation 的 mock 报告, agent 应查询知识库并给出归因",
  "setup": {
    "knowledge_seed": [{"symptom": "...", "resolution": "...", "tags": ["setup"]}],
    "observations_seed": [],
    "mock_tool_state": {"wns": -0.12}
  },
  "script": [
    {"call": "api_knowledge_query", "args": {"keyword": "setup violation"}},
    {"call": "api_decision_record", "args": {"stage": "sta_report", "action": "analyze"}}
  ],
  "assertions": [
    {"type": "knowledge_hit", "min": 1},
    {"type": "call_ok", "all": true},
    {"type": "metric_delta", "key": "wns", "expect": "present"}
  ],
  "tags": ["sta", "setup", "v1.1-added"]
}
```

断言类型（初版实现 4 种即可）：`call_ok`（所有调用无错误码）、
`knowledge_hit`（KB 查询命中数下限）、`decision_recorded`（产生决策记录）、
`output_contains`（返回 data/text 含指定子串）。
BE-2 追加第 5 种 `metric_observed`（某 metric 已进入观测层，可选 `method` 限定来源方法）。

### B.3.3 新模块 `hacoo/bench.py` 与 CLI

```python
class BenchRunner:
    def run_case(self, case: dict, client) -> dict
        # 在独立沙箱 server 中执行 setup -> script -> assertions
        # 返回 {"id", "pass": bool, "failures": [...], "duration_s": ...}
    def run_all(self, agent=None, tags=None) -> dict  # 汇总报告
```

```
python -m hacoo bench run [--agent sta] [--tag v1.1-added]
python -m hacoo bench save-baseline --agent sta   # 存 baselines/<agent>@<version>.json
python -m hacoo bench compare --baseline baselines/sta@v1.2.0.json
# 输出: pass 率 delta, 逐 case 的 fail->pass / pass->fail 列表
```

### B.3.4 升级验收规则（写入 `docs/CONTRIBUTING.md`）

一次升级要标记 `api_upgrade_update --to done`，必须满足：
1. `python -m tests.test_e2e` 全绿（含契约快照测试）
2. `bench compare` 对受影响 agent：**无 pass→fail 回退**；提案 `acceptance`
   中点名的 case 必须 fail→pass（或新建即 pass）
3. `bench save-baseline` 生成新基准并在提案 `version_after` 中记录版本号

---

# Part C：领域能力升级 backlog（专家视角反思）

> 以下是对既有功能的逐领域反思。既有基础：9 个 Agent + 8 个 adapter
> （PT/ICC2/Innovus/Calibre/Voltus/RedHawk-SC 为真实形态，其余 mock）+
> flow 模板（block_signoff / sta_only）。每项 backlog 条目给出：
> 挑战 → 升级内容 → 优先级（P0/P1/P2）→ 验收（对应 bench case 或测试）。
> 实施时应先走 Part A 的提案流程立项，再按 Part B 验收。

## C.1 Front-end：Synthesis（当前仅 mock_eda，最薄弱）

**挑战**：QoR 三角（timing/area/power）收敛、congestion-aware 综合、
物理感知综合（与 floorplan 的一致性）、多次综合迭代的 recipe 选择。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| FE-1 | 真实综合 adapter（Genus `genus -legacy_ui` / DC `dc_shell-t`，复用 `TclShell` 模式）：`api_synth_elaborate / api_synth_compile / api_report_qor` | P0 | bench `syn-qor-01`：report_qor 返回结构化 WNS/area/power |
| FE-2 | QoR 报告解析器（从 genus/DC 报告提取 WNS/TNS/area/leakage/total power 到 metrics，喂给观测层趋势检测） | P0 | 单测：给定样例报告文本，提取字段齐全 |
| FE-3 | skill `synth-convergence`：compile → report_qor → 未达标时按知识库选择下一轮 effort/boundary 策略的迭代循环 | P1 | bench：mock 环境下 3 轮迭代内 WNS 单调改善 |

## C.2 Front-end：DFT

**挑战**：scan 插入后的 DRC 修复、ATPG 覆盖率收敛（最后 0.5% 最难）、
test point 插入权衡、DFT 与功能时序的相互影响。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| FE-4 | DFT adapter（Tessent `tessent -shell` / DFTMAX 经 dc_shell）：`api_dft_insert_scan / api_dft_atpg / api_dft_report_coverage` | P1 | bench `dft-cov-01`：coverage 解析为 {test_coverage, fault_coverage, untestable} |
| FE-5 | skill `dft-coverage-closure`：coverage 未达标 → 归因（undetectable fault 分类）→ test point / pattern 策略选择 | P2 | bench：对未达标场景输出归因分类 + 策略建议 |

## C.3 Back-end：APR（ICC2/Innovus adapter 已有，按子阶段细化）

**挑战**：flow 不是一次跑完，而是 floorplan → placement → CTS → route
四个子阶段各有收敛循环，且互相耦合（floorplan 差 → route DRC 爆炸）。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| BE-1 | adapter 子阶段方法补全：`api_fp_create / api_fp_place_macros / api_place_opt / api_cts_build / api_cts_report / api_route_opt`（ICC2/Innovus 双实现，命令差异封装在 adapter 内，T2 原则） | P0 | 单测：两 adapter 方法签名一致、各自生成正确工具命令 |
| BE-2 | 阶段指标提取：utilization / congestion hotspot 数 / CTS skew & latency / route DRC 数（`report_congestion`、`report_clock_trees`、`report_design -drc` 解析进 metrics） | P0 | bench `apr-metrics-01`：四阶段 metrics 全部进观测层 |
| BE-3 | skill `fp-exploration`：对同一 block 以多组 utilization/aspect/macro 方案试跑 → 对比 congestion/QoR → 推荐方案（多实例并行，见 X-3） | P1 | bench：mock 下产出 ≥2 方案对比表 |
| BE-4 | skill `route-drc-convergence`：route → DRC 报告 → 按 DRC 类型（short/spacing/antenna）分类 → 针对性修策略（reroute/widen/via-pillar）→ 迭代至阈值 | P0 | bench `apr-route-drc-loop-01`：mock 下 DRC 数逐轮下降 |

## C.4 Signoff：STA（PrimeTime adapter 已有基础）

**挑战**：MCMM（多 corner 多 mode）场景爆炸、SI-aware（crosstalk delta
delay）、POCV/AOCV 统计时序、ECO 修复优先级排序。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| SO-1 | MCMM 场景管理：`api_mcmm_create_scenario / api_mcmm_report_all`（scenario = mode×corner 组合，report 按场景分组解析最差值） | P0 | bench `sta-mcmm-01`：4 场景报告解析出 per-scenario WNS + global WNS |
| SO-2 | skill `sta-eco-priority`：violation 按 (slack×路径数×修复成本) 排序，区分 setup/hold 修复策略冲突（修 setup 恶化的 hold 路径标记） | P0 | bench：对混合 violation 场景输出排序 + 冲突标记 |
| SO-3 | SI-aware 支持：`api_report_timing` 增加 `si_enabled` 参数（`set_si_options` + delta delay 报告解析） | P1 | 单测：命令序列含 SI 设置 |

## C.5 Signoff：Physical Verification（Calibre adapter 已有基础）

**挑战**：DRC 与 route 的拉锯收敛、waiver 流程管理、LVS short/open 调试
耗时、PERC 可靠性检查（ESD/latch-up/CD）规则复杂。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| SO-4 | Calibre 批处理扩展：`api_calibre_lvs / api_calibre_perc`（现为 DRC 为主）；RVE 结果文件解析为结构化 violation 列表（rule/count/bbox） | P0 | bench `pv-lvs-01`：LVS 报告解析出 shorts/opens/net mismatch 计数 |
| SO-5 | waiver 流程：`api_calibre_waive`（waiver 规则文件管理 + waived/unwaived 分类报告），waiver 决策必须走 `api_decision_record`（问责重点区） | P1 | bench：waive 操作产生决策记录且报告分类正确 |
| SO-6 | skill `pv-debug-triage`：DRC/LVS violation 按规则/区域聚类 → 判断是真实问题还是 deck/设置问题 → 分派给 apr（真实 viol）或 flow（设置问题） | P1 | bench：混合 violation 输入的正确分派率 |

## C.6 Signoff：EMIR（RedHawk-SC/Voltus adapter 已有基础）

**挑战**：static/dynamic IR drop 场景选择（vectorless vs VCD）、
PG EM 与 signal EM 限值差异、hotspot 修复手段选择与热耦合。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| SO-7 | hotspot 报告解析：`api_emir_report_hotspots`（instance/坐标/压降值 Top N 结构化输出，进观测层） | P0 | bench `emir-hotspot-01` |
| SO-8 | skill `emir-fix-strategy`：hotspot → 归因（decap 不足/电源网格稀疏/局部高翻转）→ 修复策略（decap 插入/via array/走线加宽）→ 移交 apr 执行 | P1 | bench：hotspot 输入输出归因 + 策略 + 移交决策记录 |

## C.7 ECO（当前空白，目标 80% 工作的关键）

**挑战**：ECO 范围控制（freeze 已签核区域、spare cell 利用、metal-only）、
修一类 violation 引入另一类的冲突、ECO 后的增量签核闭环。

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| EC-1 | flow 模板 `eco_loop`：sta 诊断（SO-2 排序）→ apr 执行 ECO（`api_eco_place/route`）→ 增量 sta/pv 验证 → 收敛或升级人工；最大迭代数与升级阈值可配 | P0 | e2e：mock 下 eco_loop 完整跑通且决策链可 trace |
| EC-2 | skill `eco-scope-control`：ECO 前检查受影响区域是否已签核 freeze、优先 spare cell / metal-only 方案 | P1 | bench：freeze 区域的 ECO 方案被拒绝并给出替代 |

## C.8 跨领域：自动化、优化、快速收敛

| # | 升级 | 优先级 | 验收 |
|---|---|---|---|
| X-1 | QoR 趋势看板数据源：观测层 metrics 覆盖 C.1-C.6 全部关键指标（依赖 FE-2/BE-2/SO-1/SO-7），趋势检测器自动生效（成长闭环 §3.2 检测器 1 无需改动，只需扩充方向表） | P0 | e2e：指标进入观测后可被 trend 检测器命中 |
| X-2 | recipe 学习：知识条目 tags 增加 design 维度（foundry/node/block 类型），Agent 决策前按当前 design 画像检索——同节点历史经验优先 | P1 | bench：带 node tag 的条目检索排序优先 |
| X-3 | 并行方案探索：flow engine 支持 fan-out——同 agent 多实例以不同参数并行跑，结果对比后择优（BE-3 依赖） | P1 | e2e：fan-out 2 实例并行 + 结果汇总 |
| X-4 | run ↔ 数据版本追踪：flow manifest 记录 ISF `data_version/design_version`（isf adapter 已有字段，需透传到 runs/<run_id>.json） | P1 | e2e：manifest 含版本字段 |
| X-5 | thermal/SIPI 跨域关联：insight 检测器扩展——thermal hotspot 与 EMIR hotspot 坐标重叠时生成 cross_domain insight（依赖成长闭环 Part 3 + SO-7） | P2 | 单测：重叠坐标输入产生关联 insight |

## C.9 既有功能的整体反思（架构层）

1. **adapter 覆盖度不均衡**：signoff 工具链（PT/Calibre/RedHawk/Voltus）
   已是真实形态，front-end（综合/DFT）还是 mock——与"PM+2 工程师完成
   80% 工作"的目标不匹配，C.1/C.2 的 P0/P1 项是最大短板
2. **flow 模板太粗**：block_signoff 是线性模板，真实工作是每个子阶段
   内部有收敛循环（C.3 BE-4、C.7 EC-1）——模板引擎需要支持"阶段内循环 +
   收敛判据 + 升级人工"的结构，不只是线性 stage 列表
3. **跨域数据没有关联**：thermal/EMIR/STA 各看各的报告，真实收敛中
   它们互为因果（X-5 是起点）
4. **ECO 完全缺位**：而 ECO 恰是人力消耗最大、决策问责价值最高的环节

---

# Part D：本 SPEC 自身的测试与验收

## D.1 Part A/B 实现后的新测试（追加进 `tests/test_e2e.py`）

1. 升级提案生命周期：propose → accept（无 acceptance 时报 ARGUMENT_ERROR）
   → in_progress → done；非法流转被拒绝
2. 契约快照：新增可选参数快照自动更新；删除方法导致测试失败；
   `compat check` 退出码正确
3. bench runner：给定通过/失败两个 golden case，run_all 报告计数正确；
   compare 正确列出 fail→pass / pass→fail
4. insight → 升级提案联动：confirm + create_upgrade 生成 source=insight 提案
5. Agent 版本写入 subagent frontmatter（sync 后 grep 验证）

回归标准：既有全部测试 + `SPEC-agent-growth-loop` 新增测试 + 上述 5 组全绿。

## D.2 Part C 的推进方式

Part C 是 backlog 不是一次性实现项。每个条目按 Part A 流程立项
（`api_upgrade_propose`，acceptance 栏引用对应 bench case），实现后用
Part B 流程验收。建议实施顺序：

```
第一波 (地基):    FE-1, FE-2, BE-1, BE-2, SO-1, SO-4, SO-7, X-1
第二波 (收敛环):  BE-4, SO-2, EC-1, FE-3
第三波 (深水区):  FE-4, BE-3, SO-5, SO-6, SO-8, EC-2, X-2, X-3, X-4, FE-5, SO-3, X-5
```
