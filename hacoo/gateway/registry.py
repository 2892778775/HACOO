"""网关层 — 方法注册表 (G2 注册表查找 / T1 方法注册)。

安全边界 (SPEC): 网关仅解析显式注册的 api_* 方法,
拒绝未注册的任意 shell 过程, 为后端访问定义清晰的执行边界。

命名空间: 不同工具可定义同名 api_* 方法 (如各工具的 api_load_design
签名不同), 注册表按 (tool, method) 存储; 网关用 instance_id 解析出
实例的工具后查到正确的签名 (G3 参数检查才不会张冠李戴)。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from hacoo.adapters import get_adapter_class, list_tools
from hacoo.adapters.base import MethodSpec

# 网关通用 API (kind="generic"), 由网关自身实现
GENERIC_METHODS: List[MethodSpec] = [
    MethodSpec(name="api_ping", description="Check connectivity to the HACOO gateway.",
               capability="read", kind="generic"),
    MethodSpec(name="api_whoami", description="Show current user identity, role, "
               "capabilities and instance quota usage.",
               capability="read", kind="generic"),
    MethodSpec(name="api_list_method", description="List registered api_* methods and metadata "
               "(progressive capability exposure; pass tool=... to filter).",
               capability="read", kind="generic",
               params={"tool": {"type": "str", "required": False, "default": None}}),
    MethodSpec(name="api_method_detail", description="Get call requirements of one method "
               "(fetch details on demand to save context budget).",
               capability="read", kind="generic",
               params={"method": {"type": "str", "required": True, "default": None},
                       "tool": {"type": "str", "required": False, "default": None}}),
    MethodSpec(name="api_list_tools", description="List registered EDA tool adapters.",
               capability="read", kind="generic"),
    MethodSpec(name="api_spawn_instance", description="Spawn a backend instance of an EDA tool; "
               "returns instance_id as the routing handle for subsequent calls.",
               capability="write", kind="generic",
               params={"tool": {"type": "str", "required": True, "default": None}}),
    MethodSpec(name="api_release_instance", description="Explicitly release a backend instance.",
               capability="write", kind="generic",
               params={"instance_id": {"type": "str", "required": True, "default": None}}),
    MethodSpec(name="api_list_instances", description="List live backend instances "
               "(own instances only; admins see all).",
               capability="read", kind="generic"),
    MethodSpec(name="api_task_status", description="Poll an async task (R8 long-running support).",
               capability="read", kind="generic",
               params={"task_id": {"type": "str", "required": True, "default": None}}),
    MethodSpec(name="api_task_cancel", description="Cancel a pending async task.",
               capability="write", kind="generic",
               params={"task_id": {"type": "str", "required": True, "default": None}}),
    # ---- 多 Agent 协作 ----
    MethodSpec(name="api_list_agents", description="List specialist agents and whether each "
               "is enabled for the current experiment.",
               capability="read", kind="generic"),
    # ---- 知识库 (Agent 自我成长) ----
    MethodSpec(name="api_knowledge_add", description="Persist a lesson into an agent's "
               "knowledge base (symptom/root_cause/resolution/tags).",
               capability="write", kind="generic",
               params={"agent": {"type": "str", "required": True, "default": None},
                       "symptom": {"type": "str", "required": True, "default": None},
                       "resolution": {"type": "str", "required": True, "default": None},
                       "root_cause": {"type": "str", "required": False, "default": ""},
                       "tags": {"type": "list", "required": False, "default": None},
                       "run_id": {"type": "str", "required": False, "default": None}}),
    MethodSpec(name="api_knowledge_query", description="Query an agent's knowledge base "
               "by keyword and/or tags (decision support).",
               capability="read", kind="generic",
               params={"agent": {"type": "str", "required": True, "default": None},
                       "keyword": {"type": "str", "required": False, "default": ""},
                       "tags": {"type": "list", "required": False, "default": None},
                       "limit": {"type": "int", "required": False, "default": 10}}),
    MethodSpec(name="api_knowledge_list", description="List recent knowledge entries of an agent.",
               capability="read", kind="generic",
               params={"agent": {"type": "str", "required": True, "default": None},
                       "limit": {"type": "int", "required": False, "default": 50}}),
    # ---- 决策日志 (问责/问题定位) ----
    MethodSpec(name="api_decision_record", description="Record an agent decision BEFORE acting; "
               "returns decision_id for outcome tracking.",
               capability="write", kind="generic",
               params={"agent": {"type": "str", "required": True, "default": None},
                       "stage": {"type": "str", "required": True, "default": None},
                       "action": {"type": "str", "required": True, "default": None},
                       "rationale": {"type": "str", "required": True, "default": None},
                       "run_id": {"type": "str", "required": False, "default": None},
                       "params": {"type": "dict", "required": False, "default": None}}),
    MethodSpec(name="api_decision_outcome", description="Close a decision with its outcome.",
               capability="write", kind="generic",
               params={"decision_id": {"type": "str", "required": True, "default": None},
                       "status": {"type": "str", "required": True, "default": None},
                       "metrics": {"type": "dict", "required": False, "default": None},
                       "note": {"type": "str", "required": False, "default": ""}}),
    MethodSpec(name="api_decision_query", description="Query decisions by agent/run/status.",
               capability="read", kind="generic",
               params={"agent": {"type": "str", "required": False, "default": None},
                       "run_id": {"type": "str", "required": False, "default": None},
                       "status": {"type": "str", "required": False, "default": None},
                       "limit": {"type": "int", "required": False, "default": 100}}),
    # ---- Flow 编排 ----
    MethodSpec(name="api_flow_start", description="Start a templated flow run across enabled "
               "agents; returns run_id. Use metadata.async_ for long flows.",
               capability="write", kind="generic",
               params={"template": {"type": "str", "required": True, "default": None},
                       "params": {"type": "dict", "required": False, "default": None}}),
    MethodSpec(name="api_flow_status", description="Get flow run manifest/status.",
               capability="read", kind="generic",
               params={"run_id": {"type": "str", "required": True, "default": None}}),
    MethodSpec(name="api_flow_trace", description="Full accountability view of a run: "
               "manifest + decision timeline + audit entries (which agent decided what).",
               capability="read", kind="generic",
               params={"run_id": {"type": "str", "required": True, "default": None}}),
    MethodSpec(name="api_list_flow_templates", description="List available flow templates.",
               capability="read", kind="generic"),
]


class Registry:
    """已注册 api_* 方法表: 通用 API + 按 (tool, method) 命名空间的工具方法。"""

    def __init__(self) -> None:
        self._generic: Dict[str, MethodSpec] = {m.name: m for m in GENERIC_METHODS}
        self._by_tool: Dict[str, Dict[str, MethodSpec]] = {}
        for tool in list_tools():
            try:
                specs = get_adapter_class(tool).api_specs()
                self._by_tool[tool] = {s.name: s for s in specs}
            except Exception:
                continue  # 单个工具加载失败不影响整体注册表

    def is_generic(self, name: str) -> bool:
        return name in self._generic

    def lookup(self, name: str, tool: Optional[str] = None) -> Optional[MethodSpec]:
        """G2: 查找目标方法; 未注册返回 None -> 网关拒绝执行。

        tool 已知时 (来自 instance_id 解析) 优先查该工具的命名空间,
        保证 G3 参数检查用的是正确工具的签名。
        """
        if name in self._generic:
            return self._generic[name]
        if tool is not None:
            return self._by_tool.get(tool, {}).get(name)
        for specs in self._by_tool.values():  # 无工具上下文: 取任一实现 (详情查询用)
            if name in specs:
                return specs[name]
        return None

    def list(self, tool: Optional[str] = None) -> List[Dict[str, Any]]:
        specs: List[MethodSpec] = list(self._generic.values())
        tools = [tool] if tool else sorted(self._by_tool)
        for t in tools:
            specs.extend(self._by_tool.get(t, {}).values())
        return [s.to_dict() for s in
                sorted(specs, key=lambda s: (s.kind, s.tool or "", s.name))]
