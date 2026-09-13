# HACOO — Human Agent CO-wOrk eTV design platform

基于 FluxEDA 五层协议架构（见 [docs/FluxEDA.md](docs/FluxEDA.md)）实现的人机协同
eTV 设计平台。以 **OpenCode + KIMI 2.6 (vLLM)** 为 Agent 入口，通过 MCP 把异构 EDA
工具整合到统一、安全、可观测的 API 空间。多用户共享部署，实例级隔离。

## 多 Agent 设计团队（产品核心）

平台内置覆盖数字芯片设计全流程的 9 个专业 Agent，每个有独立 prompt、
工具白名单和**专属知识库**:

| Agent | 阶段 | 职责 | 生产工具 (adapter) |
|---|---|---|---|
| `dft` | 前端 | scan/ATPG/MBIST 可测性设计 | ICC2 / Innovus |
| `synthesis` | 前端 | 逻辑综合，时序/面积/功耗权衡 | ICC2 / Innovus |
| `apr` | 后端 | 布局布线，3D IC 分区/hybrid bonding/TSV 感知 | **ICC2** / **Innovus** |
| `sta` | 签核 | 多 corner 时序，ECO 建议 | **PrimeTime** |
| `pv` | 签核 | DRC/LVS/PERC | **Calibre** |
| `irem` | 签核 | IR drop / EM / PG 加固 | **RedHawk-SC** / **Voltus** |
| `thermal` | 签核 | 热分布、3D 层间热耦合 | Voltus / RedHawk-SC |
| `sipi` | 签核 | 串扰/PDN/SSN/D2D 接口裕度 | PrimeTime / RedHawk-SC |
| `flow` | 控制 | **ISF 平台对接**: 流程串接、DB 管理、CICD、Airflow 监视 | ISF (REST) |

### 生产 EDA adapter（真实命令语义封装）

每个工具 adapter 把 api_* 调用翻译为工具原生命令（T2 语义差异封装在
adapter 内）:PrimeTime/ICC2/Innovus/Voltus/RedHawk-SC 走**持久 Tcl shell
会话**(`hacoo/adapters/shell.py: TclShell`),Calibre 走批处理 + RVE 解析。
工具可执行文件按 `HACOO_<TOOL>_BIN` 环境变量解析（缺失时报错含配置指引）:

```bash
export HACOO_PRIMETIME_BIN=/tools/synopsys/pt/bin/pt_shell
export HACOO_ICC2_BIN=/tools/synopsys/icc2/bin/icc2_shell
export HACOO_INNOVUS_BIN=/tools/cadence/innovus/bin/innovus
export HACOO_CALIBRE_BIN=/tools/mentor/calibre/bin/calibre
export HACOO_VOLTUS_BIN=/tools/cadence/voltus/bin/voltus
export HACOO_REDHAWK_SC_BIN=/tools/ansys/redhawk_sc/bin/redhawk_sc
```

注意：不同工具的同名 api_* 方法（如各家 `api_load_design`）按
(instance → tool) 命名空间解析签名，网关 G3 参数检查始终使用正确工具的签名。

### 自由搭配（按实验裁剪 Agent 集合）

```bash
python -m hacoo agents list              # 查看 9 个 Agent 及启停状态
python -m hacoo agents disable thermal   # 本次实验不让 thermal 参与
python -m hacoo agents disable sipi
python -m hacoo agents sync              # 重新生成 OpenCode subagent 文件
```

禁用是**网关层硬强制**：被禁 Agent 的任何调用返回 `AGENT_DISABLED`；每个 Agent
只能调用白名单内的后端工具，越权返回 `AGENT_FORBIDDEN`。flow 中含被禁 Agent 的
stage 会让 run 立即失败并记录——未参与的 Agent 绝不会"偷偷执行"。

### 自我成长（每 Agent 独立知识库）

每个 Agent 的生成式 prompt 内置强制的 Knowledge loop:
**决策前查 KB → 决策前记录 rationale → 行动后闭环 outcome → 新问题沉淀 lesson**。

- `hacoo_knowledge_add / query` —— 经验按 Agent 隔离存储（`knowledge/<agent>.jsonl`)，
  关键词+tag 检索，被复用的经验权重自动上升（reuse_count)
- 经验是 append-only JSONL，可 git 追踪、可人工审阅

### 问题定位（哪个 Agent 的哪个决策出了问题）

```bash
# Agent 视角: 每个决策都有 rationale + outcome
python -m hacoo call api_decision_query --args '{"run_id": "run-xxx"}'
# Run 视角: manifest + 决策时间线 + request_id 级审计
python -m hacoo call api_flow_trace --args '{"run_id": "run-xxx"}'
```

### Flow 编排

`api_flow_start(template, params)` 按模板执行多 Agent 流程（内置
`block_signoff` / `sta_only`)；所有 stage 共享后端实例（状态跨 stage 连续），
每个 stage 自动记录决策并关联审计。ISF 能力通过 `isf` adapter 暴露
（`api_isf_submit_flow / flow_status / db_save / db_query / cicd_trigger /
airflow_dags`,mock 实现，替换为真实 ISF REST 调用即可上线）。

## 5 分钟上手

```bash
# 1) 启动平台（核心层零第三方依赖, Python >= 3.8）
python -m hacoo serve

# 2) 新终端: 环境自检
python -m hacoo doctor

# 3) 跑一遍演示流程 (mock 时序签核)
python -m hacoo demo

# 4) OpenCode 入口 (Agent 形式)
set VLLM_BASE_URL=http://<vllm-host>:8000/v1   # Windows; Linux 用 export
opencode            # hacoo 为主 orchestrator, hacoo-sta 等为 subagent
```

## 架构映射（SPEC 五层 + Skills 层）

| SPEC 层 | 步骤 | 实现 |
|---|---|---|
| 访问层 | A1-A3 | `hacoo/access/mcp_server.py`（Agent 入口）、`hacoo/access/sdk.py`（Python SDK）、`hacoo/server.py`（TCP 前门） |
| 通信层 | C1-C4 | `hacoo/comm/protocol.py` — socket RPC，`request_id` 追踪、`instance_id` 路由 |
| 网关层 | G1-G9 | `hacoo/gateway/` — `control.py`（G1/G3/G4 + 主体识别）、`registry.py`（G2）、`gateway.py`（G5-G9 + 配额/所有权/审计） |
| 适配层 | T1-T4 | `hacoo/adapters/` — `base.py`（api_* 注册/规范化）、`mock_eda.py`（示例工具） |
| 运行时层 | R1-R11 | `hacoo/runtime/` — `backend.py`（持久状态 worker）、`manager.py`（生命周期/归属/串行化） |
| Skills 层 | — | `.opencode/skills/` — 工作流知识编码（任务分解/输入验证/执行顺序/上下文） |

## 多用户使用

- **身份与授权**：token → `Principal(user, role, capabilities)`。角色：`admin` /
  `engineer`（read+write）/ `viewer`（read-only）。配置 `HACOO_AUTH_FILE`：
  ```json
  {
    "tokens": {"tok-alice": {"user": "alice", "role": "engineer"},
               "tok-admin": {"user": "bob", "role": "admin"}},
    "roles": {"admin": ["read", "write", "admin"],
              "engineer": ["read", "write"],
              "viewer": ["read"]}
  }
  ```
- **实例隔离**：实例按 user 归属，非属主访问返回 `UNAUTHORIZED`（admin 豁免）；
  `api_list_instances` 只列自己的实例。
- **配额**：每用户默认最多 8 个实例（`HACOO_MAX_INSTANCES_PER_USER`），超限返回
  `QUOTA_EXCEEDED`。
- **并发安全**：同一实例上的调用自动串行化（保护持久状态）；不同实例/不同用户
  完全并行。
- **审计**：每次调用落一条 JSONL（`logs/hacoo-audit.jsonl`，`HACOO_AUDIT_LOG=off`
  关闭），含 request_id / user / method / status / elapsed_ms，全链路可追溯。

## CLI 一览（易用入口）

```
python -m hacoo serve [--host H] [--port P]   启动平台服务
python -m hacoo doctor                        环境自检 (Python/服务/授权/工具/vLLM)
python -m hacoo demo                          演示流程 (mock 时序签核)
python -m hacoo whoami                        当前身份 / 角色 / 配额
python -m hacoo methods [-v] [--tool T]       列出可用 api_* 方法
python -m hacoo instances                     我的活实例
python -m hacoo call api_report_timing --instance inst-xxx --args '{"max_paths":5}'
```

## 配置（hacoo/config.py，全部可用环境变量覆盖）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `HACOO_HOST` / `HACOO_PORT` | 127.0.0.1:9877 | 服务监听地址 |
| `HACOO_IDLE_TIMEOUT_S` | 1800 | 实例空闲回收 (R9) |
| `HACOO_HEARTBEAT_INTERVAL_S` | 5 | 心跳周期 (R7) |
| `HACOO_AUTO_RESTART` | 1 | 实例故障自动重启 (R10) |
| `HACOO_MAX_INSTANCES_PER_USER` | 8 | 每用户实例配额 |
| `HACOO_AUDIT_LOG` | logs/hacoo-audit.jsonl | 审计日志路径 / `off` |
| `HACOO_AUTH_FILE` | — | 授权配置 JSON |
| `HACOO_ADAPTERS_PATH` | — | 插件 adapter 目录（os.pathsep 分隔） |
| `HACOO_AGENTS_FILE` | hacoo.agents.json | Agent 启停配置 |
| `HACOO_KNOWLEDGE_DIR` | knowledge/ | Agent 知识库目录 |
| `HACOO_DECISIONS_LOG` | logs/decisions.jsonl | 决策日志 |
| `HACOO_RUNS_DIR` | runs/ | flow run manifest 目录 |
| `HACOO_CONFIG` | — | JSON 配置文件（低于环境变量优先级） |

客户端侧：`HACOO_SERVER`（平台地址）、`HACOO_TOKEN`（访问令牌）。

## 扩展一个新 EDA 工具（维护者指南）

**方式一：插件目录（推荐，零侵入）**
1. 在任意目录写 `mytool_adapter.py`：继承 `ToolAdapter`，设置 `tool_name`，
   用 `@api_method` 封装工具原生命令（T2 语义差异限制在 api_* 方法之后）；
2. 部署时 `export HACOO_ADAPTERS_PATH=/path/to/dir` —— 网关与后端自动发现，
   `hacoo_list_methods` 即刻可见。

**方式二：内置登记**
在 `hacoo/adapters/` 新建模块并在 `ADAPTERS` 字典加一行。

两种方式都自动获得：注册表发现、参数校验、授权、审计、实例生命周期管理。

## 三种 Agent / 客户端入口（A1）

1. **OpenCode（推荐）**：根目录 `opencode.json` 已配好 `vllm-kimi/kimi2.6`
   provider + `hacoo` MCP server；`.opencode/agent/hacoo.md` 为 primary agent。
2. **LangChain runner**（批处理/CI）：
   `python -m hacoo.agent_runner "加载 chip.v (top=chip), 综合并报告时序"`
3. **Python SDK**：
   ```python
   from hacoo.access.sdk import HacooClient
   with HacooClient() as c, c.session("mock_eda") as eda:
       eda.call("api_load_design", path="chip.v", top="chip")
       print(eda.call("api_report_timing", max_paths=5)["data"])
   ```

## 公司 Linux 部署

```bash
bash scripts/deploy_linux.sh ~/hacoo-platform   # 含冒烟测试
# 编辑 ~/hacoo-platform/hacoo.env 填公司 vLLM 地址与 token, 然后:
~/hacoo-platform/start-server.sh
```

## 测试

```bash
python -m tests.test_e2e    # 或 python -m pytest tests/
```

覆盖：能力发现 / 状态保持 (R6) / 实例隔离 / 多用户所有权与配额 / 并发串行化 /
注册表与协议拒绝 (G1-G2) / 参数校验 (G3) / 授权 (G4) / 异步任务与取消 (R8) /
显式释放 (R11) / 审计日志。
