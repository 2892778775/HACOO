# HACOO Agent 成长闭环 SPEC

> 实现目标：让工程师可以方便地纠正 Agent（人的嘴），让 Agent 可以从
> 接触到的 log/report/决策数据中定期主动发现问题（Agent 的眼）。
> 两者共用同一条 knowledge 流水线，构成完整的成长闭环：
>
> ```
> 观测(看数据) → 检测(发现问题) → insight(带证据上报)
>      → 工程师确认/纠正(反馈入口) → 知识库/solution
>      → 后续决策复用 → (项目结束后合并 trunk, 后续阶段)
> ```
>
> 本 SPEC 面向 Kimi Code 实现，所有集成点均标注现有代码位置。
> 代码风格、错误码、测试约定与现有代码库保持一致。

---

## 0. 背景与现状锚点

现有平台已实现（不要重做，只扩展）：

| 现有能力 | 位置 | 本 SPEC 的关系 |
|---|---|---|
| 按 Agent 隔离的知识库 (append-only JSONL, `source` 字段已预留) | `hacoo/knowledge.py` `KnowledgeStore` | 反馈入口直接复用 `add(source="human_correction")` |
| 决策日志 (pending/ok/failed, outcome 更新) | `hacoo/decisions.py` `DecisionLog` | 反馈可绑定 `decision_id`；检测器的数据源 |
| 审计日志 (request_id 级) | `hacoo/audit.py` | 观测钩子的参考实现 |
| 网关通用方法分发 (kind="generic") | `hacoo/gateway/gateway.py:287-341` (`api_knowledge_*` / `api_decision_*` / `api_flow_*`) | 新方法按同一模式注册分发 |
| 通用方法注册表 | `hacoo/gateway/registry.py:56-110` (`MethodSpec` 列表) | 新方法在此登记 |
| MCP 工具表 | `hacoo/access/mcp_server.py:34` `TOOLS` 列表 + 分发 | 新工具在此登记 |
| SDK 便捷方法 | `hacoo/access/sdk.py:162-198` | 新增对应便捷方法 |
| 集中配置 (env 覆盖) | `hacoo/config.py` | 新增 2 个配置项 |
| 测试沙箱约定 (agents/knowledge/decisions/runs 重定向到临时目录) | `tests/test_e2e.py:27-55` `start_test_server` | 新目录同样沙箱化 |

设计约束（沿用现有文化）：
- 存储一律 append-only JSONL + 读-改-写更新，单服务进程 + 锁，不引入外部 DB
- 检测器全部规则化、确定性、可单测；不依赖 LLM（LLM 总结留给后续 digest 阶段）
- 中文注释、英文标识符；结构化错误码沿用 `ARGUMENT_ERROR` / `UNAUTHORIZED` 等

---

## 1. 观测沉淀层（Observation Store）

**职责**：每次工具调用后自动落盘一条观测记录，让 Agent 的"从业经历"跨 run、跨项目累积。

### 1.1 新模块 `hacoo/observations.py`

```python
class ObservationStore:
    def __init__(self, directory: str = "observations") -> None: ...
    def record(self, agent, run_id, tool, method, instance_id,
               ok: bool, metrics: dict, error_signatures: list,
               excerpt: str = "") -> dict   # 返回落盘条目
    def query(self, agent=None, tool=None, run_id=None,
              since_ts=None, limit=500) -> list
```

观测条目 schema（`observations/<agent>.jsonl`，每行一条）：

```json
{
  "obs_id": "obs-<10hex>",
  "ts": 1758800000.123,
  "agent": "sta",
  "run_id": "run-xxx 或 null",
  "tool": "primetime",
  "method": "api_report_timing",
  "instance_id": "inst-xxx",
  "ok": true,
  "metrics": {"wns": -0.05, "tns": -1.2},
  "error_signatures": ["ERROR: (PTS-012) ..."],
  "excerpt": "text 输出尾部 <=500 字符"
}
```

### 1.2 指标与错误签名提取（`hacoo/observations.py` 内）

- `extract_metrics(tool, data) -> dict`：白名单式提取。通用规则：取 `data`
  中值为 int/float 的扁平字段；已知 key 优先（`wns`/`tns`/`slack`/
  `violator_lines`/`paths_analyzed`/`version`/`run_no` 等）。不做深度遍历。
- `extract_error_signatures(text) -> list`：正则
  `r"(?m)^.*(?:\bERROR\b|\bError\b|VIOLATED|\*\*\*.*\bfail).{0,80}$"`，
  每次调用最多保留 5 条，每条截断 120 字符。

### 1.3 网关观测钩子

位置：`hacoo/gateway/gateway.py` 结果规范化之后（G8/G9 对应的后处理路径，
即工具调用成功/失败均已确定处）。行为：

- 仅当调用带 `agent` 参数且目标是工具特定方法（`kind="tool"`）时记录；
  通用方法（`api_ping` 等）不记录，避免噪声
- 调用失败（`BACKEND_ERROR` 等）也要记录，`ok=false` + 从错误消息提取签名
- 钩子失败（磁盘满等）只记 warning，**绝不影响主调用返回**

### 1.4 配置项（`hacoo/config.py`）

```
HACOO_OBSERVATIONS_DIR    观测存储目录 (默认 observations/)
HACOO_INSIGHTS_LOG        insight 存储路径 (默认 logs/insights.jsonl)
HACOO_KB_MISSES_LOG       知识库未命中日志 (默认 logs/kb-misses.jsonl)
```

`.gitignore` 增加 `observations/`（运行时数据，与 `logs/` `runs/` 同级处理）。

---

## 2. 人类反馈纠正入口（Feedback）

**职责**：工程师用自然语言/一条命令纠正或确认 Agent 的决策，纠正内容
直接进入知识库并绑定到具体决策，权重高于 Agent 自我沉淀。

### 2.1 知识库 schema 扩展（`hacoo/knowledge.py`）

`KnowledgeStore.add()` 增加可选参数，条目新增字段（缺省不破坏旧条目）：

```python
decision_id: Optional[str] = None    # 绑定的决策
review_status: str = "unreviewed"    # unreviewed|approved|rejected
weight: int = 1                      # human_correction 默认 5, agent 默认 1
```

- `query()` 排序键改为 `(weight, reuse_count, ts)` 降序——人类纠正优先被复用
- 新增 `review(entry_id, action, note="")`：`action in ("approve","reject")`，
  读-改-写更新 `review_status`；`reject` 时把 `weight` 置 0（不再被排序推举，
  但保留记录供审计）
- 新增 `miss_log` 机制：`query()` 在 `keyword` 非空且零命中时，向
  `HACOO_KB_MISSES_LOG` 追加 `{ts, agent, keyword, tags}`（检测器 3 的数据源）

### 2.2 新通用方法（注册表 + 网关分发 + MCP + SDK + CLI 五处同步）

| 方法 | capability | 参数 | 行为 |
|---|---|---|---|
| `api_feedback_submit` | write | `agent`(req), `content`(req), `decision_id`, `feedback_type`(`correction`\|`confirmation`\|`note`, 默认 correction), `tags`, `run_id` | 生成 `source="human_correction"`(correction 时) 或 `source="human"` 的知识条目；`decision_id` 存在时校验其属于该 agent（不存在报 `ARGUMENT_ERROR`）；correction 的 `weight=5` |
| `api_review_queue` | read | `agent`, `limit`(默认 50) | 返回该 agent 的待复核项：`status=pending` 的决策 + `review_status=unreviewed` 且 `source=agent` 的知识条目，各带摘要 |
| `api_knowledge_review` | write | `entry_id`(req), `action`(req: approve\|reject), `note` | 调 `KnowledgeStore.review`；entry 不存在报 `ARGUMENT_ERROR` |

治理规则：以上三个方法是**人类面向**的，不受 Agent 启停/白名单限制
（与 `api_knowledge_query` 的豁免逻辑一致，见 `gateway.py:296` 注释）。

### 2.3 MCP 工具（`hacoo/access/mcp_server.py` TOOLS）

`hacoo_feedback` / `hacoo_review_queue` / `hacoo_knowledge_review`，
schema 与上表一致。`hacoo_feedback` 的 description 必须写明使用时机：
"当人类指出 Agent 的决策/做法有误，或明确确认某个做法正确时调用"。

### 2.4 SDK（`hacoo/access/sdk.py`）

```python
client.feedback(agent, content, decision_id=None, feedback_type="correction", tags=None, run_id=None)
client.review_queue(agent=None, limit=50)
client.knowledge_review(entry_id, action, note="")
```

### 2.5 CLI（`hacoo/cli.py`）

```
python -m hacoo feedback --agent sta --content "这种情况应先查 clock skew" [--decision-id dec-xxx]
python -m hacoo review [--agent sta]
python -m hacoo review-mark --entry kb-xxx --action approve [--note "..."]
```

---

## 3. 主动发现层（Insights）

**职责**：定期从观测/决策/知识库数据中检测四类问题，生成带证据的
insight 条目，工程师确认后回流知识库； dismissed 的同类问题不再报警。

### 3.1 新模块 `hacoo/insights.py`

insight schema（`logs/insights.jsonl`，append-only + 读-改-写更新状态）：

```json
{
  "insight_id": "ins-<10hex>",
  "ts": 1758800000.123,
  "agent": "sta",
  "kind": "anomaly | trend | blind_spot | self_issue",
  "title": "wns 连续 3 个 run 恶化",
  "detail": "run-a: -0.02 -> run-b: -0.05 -> run-c: -0.11",
  "evidence": [{"type": "observation", "ref": "obs-xxx", "excerpt": "..."}],
  "confidence": 0.8,
  "signature": "<agent>:<kind>:<稳定关键串的 sha1[:12]>",
  "status": "new | confirmed | dismissed",
  "proposed_action": "建议沉淀为知识条目 / 建议调整决策策略",
  "review_note": ""
}
```

```python
class InsightStore:
    def __init__(self, path: str) -> None: ...
    def add(self, ...) -> dict
    def exists_active(self, signature: str) -> bool
        # 同 signature 且 status in (new, confirmed) 或 dismissed 均视为已存在
        # —— dismissed 不再重复报警 (避免告警疲劳)
    def review(self, insight_id, action, note="") -> dict
        # action in ("confirm","dismiss")
    def query(self, agent=None, kind=None, status=None, limit=100) -> list
```

### 3.2 检测器（`hacoo/insights.py` 内，纯函数，可单测）

输入：观测列表 + 决策列表 + KB miss 日志 + 知识条目；输出：候选 insight
列表（不含持久化）。阈值集中为模块常量，便于调优：

| # | 检测器 | kind | 规则（默认阈值） | signature 关键串 |
|---|---|---|---|---|
| 1 | 趋势异常 | `trend` | 同一 (agent, metric key) 按 ts 排序取最近 ≥3 个 run 的值，连续 3 点单调恶化（wns/tns 类"越小越差"与计数类"越大越差"分别判定方向，方向表内置：`wns/tns/slack` 越小越差，其余数值越大越差） | `metric:<key>` |
| 2 | 失败签名聚类 | `anomaly` | 窗口内（默认 7 天）同一 agent 的同一 error_signature 出现 ≥3 次 | `sig:<signature 前 40 字符>` |
| 3 | 知识盲区 | `blind_spot` | KB miss 日志中同一 (agent, keyword) 未命中 ≥2 次 | `miss:<keyword>` |
| 4 | 自我问题 | `self_issue` | 满足任一：(a) 最近 ≥5 条决策中 failed 占比 ≥30%；(b) 窗口内 `source=human_correction` 条目 ≥2 条 | `self:<agent>` 或 `corr:<tag 集合>` |

`run_detectors(agent, observations, decisions, kb_misses, knowledge_entries,
now_ts) -> List[dict]` 为统一入口。

### 3.3 新通用方法（五处同步，同 §2.2）

| 方法 | capability | 参数 | 行为 |
|---|---|---|---|
| `api_insights_scan` | write | `agent`(缺省=全部 enabled agent), `window_days`(默认 7) | 对每个目标 agent 跑 `run_detectors`；`InsightStore.exists_active` 去重后落盘；返回新建 insight 列表（含跳过计数） |
| `api_insights_list` | read | `agent`, `kind`, `status`, `limit` | 查询 insight |
| `api_insight_review` | write | `insight_id`(req), `action`(req: confirm\|dismiss), `note`, `auto_kb`(默认 true) | confirm 且 `auto_kb=true` 时自动生成知识条目（`source="insight"`, symptom=title, resolution=proposed_action, 关联 insight_id）；dismiss 仅更新状态 |
| `api_observations_query` | read | `agent`, `run_id`, `tool`, `limit` | 查询观测记录（排障用） |

MCP 工具：`hacoo_insights_scan` / `hacoo_insights_list` / `hacoo_insight_review`。
SDK：`client.insights_scan(agent=None, window_days=7)` /
`client.insights_list(...)` / `client.insight_review(...)`。
CLI：`python -m hacoo insights [--agent sta] [--scan] [--kind self_issue]`、
`python -m hacoo insight-mark --id ins-xxx --action confirm`。

### 3.4 Agent prompt 更新（`hacoo/agents/registry.py` `_AGENT_TEMPLATE`）

Knowledge loop 一节追加两条纪律，然后 `python -m hacoo agents sync` 重新生成：

1. 被人类纠正后（对话中出现纠正语义），必须主动调用 `hacoo_feedback`
   沉淀，而不是只在当次对话里改口
2. 每次 flow 结束后调用 `hacoo_insights_scan(agent=<self>)`，对返回的
   `new` 状态 insight 向人类简要汇报（一条一行，附 evidence 引用）

---

## 4. 测试要求（`tests/test_e2e.py` 新增 `HacooGrowthLoopTest`）

沿用 `start_test_server` 沙箱模式，新增配置项同样重定向到临时目录。
至少覆盖：

1. **观测钩子**：带 `agent` 调一次 `mock_eda` 方法 → `observations/<agent>.jsonl`
   恰增 1 条且 metrics/excerpt 非空；失败调用 `ok=false` 且有 error_signatures；
   通用方法调用不产生观测
2. **反馈闭环**：`api_feedback_submit` 带 `decision_id` → 知识条目
   `source=human_correction`、`weight=5`、`decision_id` 一致；`decision_id`
   属于别的 agent 时报 `ARGUMENT_ERROR`；`query` 排序中人类纠正条目
   排在同关键词 agent 条目之前
3. **复核队列**：造 pending 决策 + unreviewed agent 条目 → `api_review_queue`
   两类都在；`api_knowledge_review` approve 后队列减少；reject 后条目
   `weight=0` 且不再被 query 推举
4. **KB miss 记录**：零命中查询产生 miss 日志行
5. **检测器**：构造数据分别触发 4 类 insight（趋势用 3 个递增/递减点，
   聚类用 3 条同签名，盲区用 2 次同 keyword miss，自我问题用 5 决策 2 failed
   或 2 条 human_correction）
6. **insight 去重与闭环**：重复 scan 不产生同 signature insight；
   confirm + auto_kb 生成知识条目；dismiss 后再次 scan 仍不产生
7. **治理豁免**：某 agent 被 disable 后，`api_feedback_submit` /
   `api_insights_*` 仍可用（人类面向）

回归标准：全部测试通过（现有 32 + 新增，预计 39~42 个）。

---

## 5. 验收清单

- [ ] `python -m tests.test_e2e` 全绿
- [ ] `python -m hacoo doctor` 通过（新配置项有默认值，无新必填项）
- [ ] OpenCode 对话场景可走通：「sta 刚才那个决策不对，应该先查 clock skew」
      → orchestrator 调 `hacoo_feedback` → 知识库出现 human_correction 条目
- [ ] 「sta，扫描一下最近有没有发现新问题」→ `hacoo_insights_scan` 返回
      带 evidence 的 insight 列表
- [ ] README「Flow 编排」节后新增「成长闭环」小节（反馈/复核/巡检三件套用法）

---

## 6. 明确不做（后续阶段，防止范围蔓延）

- Solution 资产库分层（症状→策略索引）、遗留脚本 importer
- Web Dashboard（Streamlit）与复核队列图形化
- 每周 digest 的 cron Automation 与 LLM 汇总
- 项目结业 review 工作流与知识 trunk 合并
- `eco_loop` flow 模板
- 跨 Agent 关联检测（thermal×irem 等）

以上各项在对话中已讨论方向，待本 SPEC 落地后另行立项。
