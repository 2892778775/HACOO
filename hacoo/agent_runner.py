"""可选入口: LangChain + KIMI 2.6 (vLLM OpenAI-compatible) 驱动的 HACOO Agent。

OpenCode 之外的第二种 Agent 启动形式 (A1): 直接以 langchain tool-calling
agent 驱动 HACOO 平台, 适合批处理脚本 / CI / 无 IDE 场景。

环境变量:
    VLLM_BASE_URL   vLLM 服务地址, 如 http://vllm.internal:8000/v1
    VLLM_API_KEY    vLLM API key (无鉴权时可为 EMPTY)
    KIMI_MODEL      模型名 (默认 kimi2.6)
    HACOO_SERVER    HACOO 平台地址 (默认 127.0.0.1:9877)
    HACOO_TOKEN     访问令牌 (默认 dev-token)

用法:
    python -m hacoo.agent_runner "加载 chip.v (top=chip), 综合并报告时序"
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

SYSTEM_PROMPT = """You are the HACOO platform agent. You orchestrate EDA tool backends
through HACOO tools ONLY (never run any EDA tool shell directly).

Rules:
1. Discover before calling: use hacoo_list_methods / hacoo_method_detail.
2. Every tool-specific call needs an instance_id from hacoo_spawn_instance;
   the instance keeps persistent state across calls — reuse it for the flow.
3. For long-running operations, call hacoo_call with async_=true and poll
   hacoo_task_status.
4. Release instances with hacoo_release_instance when done.
5. Report only numbers the backend actually returned."""


def build_tools() -> List[Any]:
    """把 HacooClient 封装为 langchain 工具集。"""
    try:
        from langchain_core.tools import tool
    except ImportError:
        try:
            from langchain.tools import tool  # type: ignore
        except ImportError as exc:
            raise SystemExit(
                "langchain is required for agent_runner. "
                "Install with: pip install langchain langchain-openai") from exc

    from hacoo.access.sdk import HacooClient
    client = HacooClient()

    @tool
    def hacoo_ping() -> str:
        """Check connectivity to the HACOO platform gateway."""
        return json.dumps(client.ping(), ensure_ascii=False)

    @tool
    def hacoo_whoami() -> str:
        """Show current user identity, role, capabilities and instance quota usage."""
        return json.dumps(client.whoami(), ensure_ascii=False)

    @tool
    def hacoo_list_methods(tool: str = "") -> str:
        """List registered api_* methods (optionally filtered by tool name)."""
        return json.dumps(client.list_methods(tool=tool or None), ensure_ascii=False)

    @tool
    def hacoo_method_detail(method: str) -> str:
        """Get call requirements (params, capability) of one api_* method."""
        return json.dumps(client.method_detail(method), ensure_ascii=False)

    @tool
    def hacoo_spawn_instance(tool: str) -> str:
        """Spawn a backend instance of an EDA tool; returns instance_id."""
        return json.dumps({"instance_id": client.spawn(tool)})

    @tool
    def hacoo_call(instance_id: str, method: str, arguments: Dict[str, Any] = None,
                   async_: bool = False) -> str:
        """Call a registered api_* method on a backend instance."""
        return json.dumps(client.call(method, instance_id=instance_id,
                                      async_=async_, **(arguments or {})),
                          ensure_ascii=False)

    @tool
    def hacoo_task_status(task_id: str) -> str:
        """Poll status/result of an async task."""
        return json.dumps(client.task_status(task_id), ensure_ascii=False)

    @tool
    def hacoo_task_cancel(task_id: str) -> str:
        """Cancel a pending async task."""
        return json.dumps(client.task_cancel(task_id), ensure_ascii=False)

    @tool
    def hacoo_release_instance(instance_id: str) -> str:
        """Release a backend instance and reclaim resources."""
        client.release(instance_id)
        return json.dumps({"released": instance_id})

    return [hacoo_ping, hacoo_whoami, hacoo_list_methods, hacoo_method_detail,
            hacoo_spawn_instance, hacoo_call, hacoo_task_status, hacoo_task_cancel,
            hacoo_release_instance]


def build_llm() -> Any:
    """KIMI 2.6 via vLLM OpenAI-compatible endpoint。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise SystemExit(
            "langchain-openai is required. Install with: pip install langchain-openai") from exc
    base_url = os.environ.get("VLLM_BASE_URL")
    if not base_url:
        raise SystemExit("set VLLM_BASE_URL to your vLLM endpoint, "
                         "e.g. http://vllm.internal:8000/v1")
    return ChatOpenAI(
        model=os.environ.get("KIMI_MODEL", "kimi2.6"),
        base_url=base_url,
        api_key=os.environ.get("VLLM_API_KEY", "EMPTY"),
        temperature=0.0,
    )


def run(task: str, max_iterations: int = 20) -> str:
    tools = build_tools()
    llm = build_llm()
    try:
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from langchain_core.prompts import ChatPromptTemplate

        prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ])
        agent = create_tool_calling_agent(llm, tools, prompt)
        executor = AgentExecutor(agent=agent, tools=tools,
                                 max_iterations=max_iterations, verbose=True)
        return executor.invoke({"input": task})["output"]
    except ImportError:
        # 兜底: 手写 tool-calling 循环 (适配仅有 langchain-core 的环境)
        return _manual_loop(llm, tools, task, max_iterations)


def _manual_loop(llm: Any, tools: List[Any], task: str, max_iterations: int) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    messages: List[Any] = [SystemMessage(content=SYSTEM_PROMPT),
                           HumanMessage(content=task)]
    for _ in range(max_iterations):
        ai = llm_with_tools.invoke(messages)
        messages.append(ai)
        if not getattr(ai, "tool_calls", None):
            return ai.content
        for call in ai.tool_calls:
            try:
                result = tool_map[call["name"]].invoke(call["args"])
            except Exception as exc:  # 把 HACOO 错误回给模型纠正
                result = "ERROR: %s" % exc
            messages.append(ToolMessage(content=str(result),
                                        tool_call_id=call["id"]))
    return "max iterations reached"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(run(sys.argv[1]))


if __name__ == "__main__":
    main()
