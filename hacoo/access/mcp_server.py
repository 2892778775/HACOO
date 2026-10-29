"""访问层 (A1): MCP Server — 面向 Agent (OpenCode) 的统一入口。

标准 MCP stdio transport: 每行一个 JSON-RPC 消息 (无 Content-Length 头)。
零第三方依赖, 直接以 HACOO 客户端身份连接平台前门。

OpenCode 配置见项目根目录 opencode.json:
    "mcp": { "hacoo": { "type": "local",
        "command": ["python", "-m", "hacoo.access.mcp_server"], ... } }

环境变量:
    HACOO_SERVER  平台地址 (默认 127.0.0.1:9877)
    HACOO_TOKEN   访问令牌 (默认 dev-token)

工具集 (对应 SPEC 能力发现 G5-G6 的渐进式暴露):
    hacoo_ping              连通性检查
    hacoo_list_methods      列出已注册 api_* 方法 (可按 tool 过滤)
    hacoo_method_detail     按需获取单个方法的调用要求
    hacoo_spawn_instance    启动后端实例, 返回 instance_id 路由句柄
    hacoo_call              在指定 instance 上调用 api_* 方法 (支持 async_)
    hacoo_task_status       轮询异步任务 (R8)
    hacoo_list_instances    查看活实例
    hacoo_release_instance  显式释放实例
    hacoo_runs_list         列出 flow run 摘要 (可按 status 过滤)
    hacoo_feedback          人类反馈纠正 (成长闭环 §2)
    hacoo_review_queue      待复核队列 (pending 决策 + unreviewed 条目)
    hacoo_knowledge_review  复核知识条目 (approve/reject)
    hacoo_insights_scan     运行检测器扫描新问题 (成长闭环 §3)
    hacoo_insights_list     查询 insight
    hacoo_insight_review    复核 insight (confirm/dismiss, confirm 可联动升级提案)
    hacoo_upgrade_propose   创建升级提案 (SPEC-evolution Part A)
    hacoo_upgrade_list      查询升级提案
    hacoo_upgrade_update    升级提案状态流转
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from hacoo.access.sdk import HacooClient, HacooClientError

MCP_PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "hacoo_ping",
        "description": "Check connectivity to the HACOO platform gateway.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hacoo_whoami",
        "description": "Show current user identity, role, capabilities and instance quota usage.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hacoo_list_methods",
        "description": "List registered api_* methods and their metadata. "
                       "Pass tool to filter by EDA tool. Use this before calling "
                       "unfamiliar methods (progressive capability exposure).",
        "inputSchema": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "optional tool filter, e.g. 'mock_eda'"}}},
    },
    {
        "name": "hacoo_method_detail",
        "description": "Get call requirements (params, capability) of one api_* method.",
        "inputSchema": {"type": "object", "properties": {
            "method": {"type": "string"}}, "required": ["method"]},
    },
    {
        "name": "hacoo_spawn_instance",
        "description": "Spawn a backend instance of an EDA tool. Returns instance_id — "
                       "the routing handle that must be passed to subsequent hacoo_call.",
        "inputSchema": {"type": "object", "properties": {
            "tool": {"type": "string", "description": "registered tool name"}}, "required": ["tool"]},
    },
    {
        "name": "hacoo_call",
        "description": "Call a registered api_* method on a backend instance. "
                       "Set async_=true for long-running operations, then poll hacoo_task_status. "
                       "Agents MUST pass their own name as 'agent' and the current run_id when "
                       "inside a flow (accountability + policy enforcement).",
        "inputSchema": {"type": "object", "properties": {
            "instance_id": {"type": "string"},
            "method": {"type": "string"},
            "arguments": {"type": "object", "description": "method arguments"},
            "async_": {"type": "boolean", "description": "submit as async task (R8)"},
            "agent": {"type": "string", "description": "calling agent name, e.g. 'sta'"},
            "run_id": {"type": "string", "description": "flow run id for traceability"},
        }, "required": ["instance_id", "method"]},
    },
    {
        "name": "hacoo_list_agents",
        "description": "List specialist agents (dft/synthesis/apr/sta/pv/irem/thermal/sipi/flow) "
                       "and whether each is enabled for the current experiment.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hacoo_knowledge_add",
        "description": "Persist a lesson into an agent's knowledge base (self-improvement).",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "symptom": {"type": "string"},
            "resolution": {"type": "string"},
            "root_cause": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "run_id": {"type": "string"},
        }, "required": ["agent", "symptom", "resolution"]},
    },
    {
        "name": "hacoo_knowledge_query",
        "description": "Query an agent's knowledge base by keyword/tags before making decisions.",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "keyword": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        }, "required": ["agent"]},
    },
    {
        "name": "hacoo_decision_record",
        "description": "Record a decision BEFORE acting; returns decision_id for outcome tracking.",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "stage": {"type": "string"},
            "action": {"type": "string"},
            "rationale": {"type": "string"},
            "run_id": {"type": "string"},
            "params": {"type": "object"},
        }, "required": ["agent", "stage", "action", "rationale"]},
    },
    {
        "name": "hacoo_decision_outcome",
        "description": "Close a decision with its outcome (ok/failed + metrics + note).",
        "inputSchema": {"type": "object", "properties": {
            "decision_id": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "failed", "skipped"]},
            "metrics": {"type": "object"},
            "note": {"type": "string"},
        }, "required": ["decision_id", "status"]},
    },
    {
        "name": "hacoo_flow_start",
        "description": "Start a templated flow run across enabled agents; returns the run manifest "
                       "with run_id. Stages whose agent is disabled fail the run (AGENT_DISABLED).",
        "inputSchema": {"type": "object", "properties": {
            "template": {"type": "string", "description": "flow template name"},
            "params": {"type": "object", "description": "template params, e.g. design_path/top"},
        }, "required": ["template"]},
    },
    {
        "name": "hacoo_flow_status",
        "description": "Get a flow run manifest/status by run_id.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "hacoo_flow_trace",
        "description": "Accountability view of a run: which agent made which decision, with "
                       "rationale, outcome, and linked audit entries. Use for root-causing issues.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "hacoo_runs_list",
        "description": "List flow run manifest summaries (latest first), optionally filtered "
                       "by status (running|success|failed).",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer"},
            "status": {"type": "string", "enum": ["running", "success", "failed"]},
        }},
    },
    {
        "name": "hacoo_task_status",
        "description": "Poll status/result of an async task submitted via hacoo_call(async_=true).",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "hacoo_task_cancel",
        "description": "Cancel a pending async task (only effective before it starts running).",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "hacoo_list_instances",
        "description": "List live backend instances (id, tool, uptime, idle time).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hacoo_release_instance",
        "description": "Explicitly release a backend instance and reclaim its resources.",
        "inputSchema": {"type": "object", "properties": {
            "instance_id": {"type": "string"}}, "required": ["instance_id"]},
    },
    {
        "name": "hacoo_feedback",
        "description": "Submit human feedback about an agent's decision or behavior. "
                       "当人类指出 Agent 的决策/做法有误，或明确确认某个做法正确时调用 "
                       "(feedback_type=correction|confirmation); corrections are persisted "
                       "into the agent's knowledge base with high weight (weight=5).",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "content": {"type": "string", "description": "feedback content in natural language"},
            "decision_id": {"type": "string", "description": "optional decision to bind to"},
            "feedback_type": {"type": "string", "enum": ["correction", "confirmation", "note"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "run_id": {"type": "string"},
        }, "required": ["agent", "content"]},
    },
    {
        "name": "hacoo_review_queue",
        "description": "List items pending human review: pending decisions + unreviewed "
                       "agent-sourced knowledge entries.",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    },
    {
        "name": "hacoo_knowledge_review",
        "description": "Approve or reject a knowledge entry. Rejected entries get weight=0 "
                       "and are no longer promoted by knowledge queries.",
        "inputSchema": {"type": "object", "properties": {
            "entry_id": {"type": "string"},
            "action": {"type": "string", "enum": ["approve", "reject"]},
            "note": {"type": "string"},
        }, "required": ["entry_id", "action"]},
    },
    {
        "name": "hacoo_insights_scan",
        "description": "Run rule-based detectors (trend/anomaly/blind_spot/self_issue) over an "
                       "agent's observations, decisions and kb-miss log; persists new insights "
                       "with evidence (deduplicated by signature). Defaults to all enabled agents.",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "window_days": {"type": "integer"},
        }},
    },
    {
        "name": "hacoo_insights_list",
        "description": "Query insights by agent/kind/status (new|confirmed|dismissed).",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string"},
            "kind": {"type": "string", "enum": ["anomaly", "trend", "blind_spot", "self_issue"]},
            "status": {"type": "string", "enum": ["new", "confirmed", "dismissed"]},
            "limit": {"type": "integer"},
        }},
    },
    {
        "name": "hacoo_insight_review",
        "description": "Confirm or dismiss an insight. confirm with auto_kb=true automatically "
                       "persists a knowledge entry (source=insight) linked to the insight; "
                       "create_upgrade=true additionally opens an upgrade proposal "
                       "(source=insight) so the finding becomes a trackable change.",
        "inputSchema": {"type": "object", "properties": {
            "insight_id": {"type": "string"},
            "action": {"type": "string", "enum": ["confirm", "dismiss"]},
            "note": {"type": "string"},
            "auto_kb": {"type": "boolean"},
            "create_upgrade": {"type": "boolean"},
        }, "required": ["insight_id", "action"]},
    },
    {
        "name": "hacoo_upgrade_propose",
        "description": "Create an upgrade proposal for an agent/skill/adapter/platform change. "
                       "acceptance (verifiable criteria, e.g. a bench case that must pass) is "
                       "mandatory before the proposal can be accepted.",
        "inputSchema": {"type": "object", "properties": {
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["agent_capability", "skill_new", "skill_update",
                                                "knowledge_bulk", "adapter_method",
                                                "platform_feature"]},
            "target": {"type": "string", "description": "agent/skill/tool name or 'platform'"},
            "motivation": {"type": "string"},
            "acceptance": {"type": "string", "description": "verifiable acceptance criteria"},
            "proposed_change": {"type": "string"},
            "source": {"type": "string", "enum": ["human", "insight", "review"]},
            "source_ref": {"type": "string", "description": "ins-xxx / kb-xxx or null"},
        }, "required": ["title", "kind", "target", "motivation"]},
    },
    {
        "name": "hacoo_upgrade_list",
        "description": "Query upgrade proposals (latest first), filterable by status/target.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["proposed", "accepted", "in_progress",
                                                  "done", "rejected"]},
            "target": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    },
    {
        "name": "hacoo_upgrade_update",
        "description": "Transition an upgrade proposal: proposed->accepted|rejected; "
                       "accepted->in_progress|rejected; in_progress->done|accepted. "
                       "done should record version_after (e.g. 'sta@1.3.0').",
        "inputSchema": {"type": "object", "properties": {
            "proposal_id": {"type": "string"},
            "to_status": {"type": "string", "enum": ["accepted", "in_progress", "done",
                                                     "rejected"]},
            "note": {"type": "string"},
            "version_after": {"type": "string"},
        }, "required": ["proposal_id", "to_status"]},
    },
]

_client: Optional[HacooClient] = None


def _get_client() -> HacooClient:
    global _client
    if _client is None:
        _client = HacooClient()
    return _client


def _text(payload: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_client()
    if name == "hacoo_ping":
        return _text(client.ping())
    if name == "hacoo_whoami":
        return _text(client.whoami())
    if name == "hacoo_list_methods":
        return _text(client.list_methods(tool=args.get("tool")))
    if name == "hacoo_method_detail":
        return _text(client.method_detail(args["method"]))
    if name == "hacoo_spawn_instance":
        iid = client.spawn(args["tool"])
        return _text({"instance_id": iid,
                      "note": "pass this instance_id to hacoo_call; release when done"})
    if name == "hacoo_call":
        result = client.call(args["method"], instance_id=args["instance_id"],
                             async_=bool(args.get("async_", False)),
                             agent=args.get("agent"), run_id=args.get("run_id"),
                             arguments=args.get("arguments") or {})
        return _text(result)
    if name == "hacoo_list_agents":
        return _text(client.list_agents())
    if name == "hacoo_knowledge_add":
        return _text(client.knowledge_add(
            agent=args["agent"], symptom=args["symptom"], resolution=args["resolution"],
            root_cause=args.get("root_cause", ""), tags=args.get("tags"),
            run_id=args.get("run_id")))
    if name == "hacoo_knowledge_query":
        return _text(client.knowledge_query(
            agent=args["agent"], keyword=args.get("keyword", ""),
            tags=args.get("tags"), limit=args.get("limit", 10)))
    if name == "hacoo_decision_record":
        return _text({"decision_id": client.decision_record(
            agent=args["agent"], stage=args["stage"], action=args["action"],
            rationale=args["rationale"], run_id=args.get("run_id"),
            params=args.get("params"))})
    if name == "hacoo_decision_outcome":
        return _text(client.decision_outcome(
            decision_id=args["decision_id"], status=args["status"],
            metrics=args.get("metrics"), note=args.get("note", "")))
    if name == "hacoo_flow_start":
        return _text(client.flow_start(args["template"], args.get("params")))
    if name == "hacoo_flow_status":
        return _text(client.flow_status(args["run_id"]))
    if name == "hacoo_flow_trace":
        return _text(client.flow_trace(args["run_id"]))
    if name == "hacoo_runs_list":
        return _text(client.runs_list(limit=args.get("limit", 100),
                                      status=args.get("status")))
    if name == "hacoo_task_status":
        return _text(client.task_status(args["task_id"]))
    if name == "hacoo_task_cancel":
        return _text(client.task_cancel(args["task_id"]))
    if name == "hacoo_list_instances":
        return _text(client.list_instances())
    if name == "hacoo_release_instance":
        client.release(args["instance_id"])
        return _text({"released": args["instance_id"]})
    if name == "hacoo_feedback":
        return _text(client.feedback(
            agent=args["agent"], content=args["content"],
            decision_id=args.get("decision_id"),
            feedback_type=args.get("feedback_type", "correction"),
            tags=args.get("tags"), run_id=args.get("run_id")))
    if name == "hacoo_review_queue":
        return _text(client.review_queue(agent=args.get("agent"),
                                         limit=args.get("limit", 50)))
    if name == "hacoo_knowledge_review":
        return _text(client.knowledge_review(
            entry_id=args["entry_id"], action=args["action"],
            note=args.get("note", "")))
    if name == "hacoo_insights_scan":
        return _text(client.insights_scan(agent=args.get("agent"),
                                          window_days=args.get("window_days", 7)))
    if name == "hacoo_insights_list":
        return _text(client.insights_list(
            agent=args.get("agent"), kind=args.get("kind"),
            status=args.get("status"), limit=args.get("limit", 100)))
    if name == "hacoo_insight_review":
        return _text(client.insight_review(
            insight_id=args["insight_id"], action=args["action"],
            note=args.get("note", ""), auto_kb=bool(args.get("auto_kb", True)),
            create_upgrade=bool(args.get("create_upgrade", False))))
    if name == "hacoo_upgrade_propose":
        return _text(client.upgrade_propose(
            title=args["title"], kind=args["kind"], target=args["target"],
            motivation=args["motivation"], acceptance=args.get("acceptance", ""),
            proposed_change=args.get("proposed_change", ""),
            source=args.get("source", "human"), source_ref=args.get("source_ref")))
    if name == "hacoo_upgrade_list":
        return _text(client.upgrade_list(
            status=args.get("status"), target=args.get("target"),
            limit=args.get("limit", 100)))
    if name == "hacoo_upgrade_update":
        return _text(client.upgrade_update(
            proposal_id=args["proposal_id"], to_status=args["to_status"],
            note=args.get("note", ""), version_after=args.get("version_after")))
    raise HacooClientError("UNKNOWN_TOOL", "no such tool: %s" % name)


def _reply(msg_id: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
    resp: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
    try:
        sys.stdout.flush()
    except OSError:
        raise SystemExit(0)  # 父进程关闭管道 -> 退出


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        try:
            if method == "initialize":
                _reply(msg_id, {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hacoo", "version": "0.1.0"},
                })
            elif method in ("notifications/initialized", "initialized"):
                pass  # 通知无需响应
            elif method == "ping":
                _reply(msg_id, {})
            elif method == "tools/list":
                _reply(msg_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    _reply(msg_id, _call_tool(params.get("name", ""), params.get("arguments") or {}))
                except HacooClientError as exc:
                    _reply(msg_id, {"content": [{"type": "text", "text": "ERROR %s" % exc}],
                                    "isError": True})
            else:
                if msg_id is not None:
                    _reply(msg_id, error={"code": -32601, "message": "method not found: %s" % method})
        except (ConnectionError, OSError) as exc:
            _reply(msg_id, {"content": [{"type": "text",
                                         "text": "HACOO server unreachable: %s" % exc}],
                            "isError": True})
            globals()["_client"] = None  # 下次调用重建连接


if __name__ == "__main__":
    main()
