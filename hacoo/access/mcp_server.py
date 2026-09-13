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
    if name == "hacoo_task_status":
        return _text(client.task_status(args["task_id"]))
    if name == "hacoo_task_cancel":
        return _text(client.task_cancel(args["task_id"]))
    if name == "hacoo_list_instances":
        return _text(client.list_instances())
    if name == "hacoo_release_instance":
        client.release(args["instance_id"])
        return _text({"released": args["instance_id"]})
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
