"""访问层 (A1-A3): Python SDK — 面向可编程客户端的统一入口。

A1 选择访问方式: 本 SDK (编程客户端) / MCP Server (Agent)
A2 封装请求:     目标方法名 + 参数列表 + 执行元数据 -> RPCRequest
A3 路由至通信层: 经 socket 发送给 HACOO 服务前门

用法:
    from hacoo.access.sdk import HacooClient
    client = HacooClient("127.0.0.1:9877", token="dev-token")
    inst = client.spawn("mock_eda")
    client.call("api_load_design", instance_id=inst, path="chip.v", top="chip")
    print(client.call("api_report_timing", instance_id=inst))
    client.release(inst)

    # 或用会话上下文 (自动 spawn/release):
    with client.session("mock_eda") as eda:
        eda.call("api_load_design", path="chip.v", top="chip")
        print(eda.call("api_report_timing")["data"])
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from hacoo.comm import protocol


class HacooClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code


class HacooClient:
    """A2: 将方法名/参数/元数据封装为 RPC 消息并维持长连接 (C2 会话管理)。

    健壮性: 线程安全 (可跨线程共享一个 client), 断线自动重连并重试一次。
    """

    def __init__(self, server: Optional[str] = None, token: Optional[str] = None,
                 connect_timeout: float = 10.0) -> None:
        server = server or os.environ.get("HACOO_SERVER", "127.0.0.1:9877")
        host, _, port = server.partition(":")
        self.host, self.port = host, int(port or 9877)
        self.token = token or os.environ.get("HACOO_TOKEN", "dev-token")
        self._connect_timeout = connect_timeout
        self._conn = self._connect()
        self._closed = False
        self._lock = threading.Lock()  # 发送/接收串行化, 支持多线程共享

    def _connect(self) -> socket.socket:
        return socket.create_connection((self.host, self.port), timeout=self._connect_timeout)

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass
        self._conn = self._connect()

    # ------------------------------------------------------------ A2 封装

    def call(self, name: str, instance_id: Optional[str] = None,
             timeout: Optional[float] = None, async_: bool = False,
             arguments: Optional[Dict[str, Any]] = None,
             agent: Optional[str] = None, run_id: Optional[str] = None,
             **kwargs: Any) -> Dict[str, Any]:
        """封装并发送一次 RPC 调用, 返回结构化响应 (C4)。

        name:        目标 api_* 方法名 (与 arguments 中的键无冲突)
        arguments:   显式参数字典 (与 kwargs 合并, kwargs 优先)
        agent/run_id: 多 Agent 归因 — 指定后网关强制启停与白名单策略
        """
        metadata: Dict[str, Any] = {"token": self.token}
        if timeout is not None:
            metadata["timeout"] = timeout
        if async_:
            metadata["async"] = True
        if agent:
            metadata["agent"] = agent
        if run_id:
            metadata["run_id"] = run_id
        args = dict(arguments or {})
        args.update(kwargs)
        req = protocol.RPCRequest(method=name, arguments=args,
                                  instance_id=instance_id, metadata=metadata)
        with self._lock:
            try:
                raw = protocol.roundtrip(self._conn, req.to_dict())
            except (OSError, protocol.ProtocolError):
                # 长连接可能被服务端回收/网络抖动断开: 重连并重试一次
                self._reconnect()
                req = protocol.RPCRequest(method=name, arguments=args,
                                          instance_id=instance_id, metadata=dict(metadata))
                raw = protocol.roundtrip(self._conn, req.to_dict())
        resp = protocol.RPCResponse.from_dict(raw)
        if resp.request_id != req.request_id:  # request_id 响应匹配
            raise HacooClientError("PROTOCOL_ERROR", "request_id mismatch in response")
        if resp.status != "ok":
            err = resp.error or {}
            raise HacooClientError(err.get("code", "ERROR"), err.get("message", "unknown error"))
        return {"data": resp.data, "text": resp.text, "artifacts": resp.artifacts,
                "elapsed_ms": resp.elapsed_ms}

    # ------------------------------------------------------------ 便捷方法

    def ping(self) -> Dict[str, Any]:
        return self.call("api_ping")

    def whoami(self) -> Dict[str, Any]:
        return self.call("api_whoami")["data"]

    def list_methods(self, tool: Optional[str] = None) -> List[Dict[str, Any]]:
        args = {"tool": tool} if tool else {}
        return self.call("api_list_method", **args)["data"]

    def method_detail(self, method: str) -> Dict[str, Any]:
        return self.call("api_method_detail", arguments={"method": method})["data"]

    def list_tools(self) -> List[str]:
        return self.call("api_list_tools")["data"]

    def spawn(self, tool: str) -> str:
        return self.call("api_spawn_instance", tool=tool)["data"]["instance_id"]

    def release(self, instance_id: str) -> None:
        self.call("api_release_instance", arguments={"instance_id": instance_id})

    def list_instances(self) -> List[Dict[str, Any]]:
        return self.call("api_list_instances")["data"]

    def task_status(self, task_id: str, wait: bool = False, poll_s: float = 0.5,
                    timeout_s: float = 300.0) -> Dict[str, Any]:
        """R8: 轮询异步任务; wait=True 时阻塞至完成。"""
        deadline = time.time() + timeout_s
        while True:
            task = self.call("api_task_status", task_id=task_id)["data"]
            if not wait or task["status"] != "running":
                return task
            if time.time() > deadline:
                raise HacooClientError("TIMEOUT", "task %s still running after %.0fs"
                                       % (task_id, timeout_s))
            time.sleep(poll_s)

    def task_cancel(self, task_id: str) -> Dict[str, Any]:
        """R8: 取消尚未开始执行的异步任务。"""
        return self.call("api_task_cancel", task_id=task_id)

    # ------------------------------------------------------------ 多 Agent 协作
    # 注意: 这些方法的参数名 (agent/run_id/...) 与 call() 的元数据参数重名,
    # 必须走显式 arguments= 字典, 不能走 **kwargs。

    def list_agents(self) -> List[Dict[str, Any]]:
        return self.call("api_list_agents")["data"]

    def knowledge_add(self, agent: str, symptom: str, resolution: str,
                      root_cause: str = "", tags: Optional[List[str]] = None,
                      run_id: Optional[str] = None) -> Dict[str, Any]:
        return self.call("api_knowledge_add", arguments={
            "agent": agent, "symptom": symptom, "resolution": resolution,
            "root_cause": root_cause, "tags": tags or [], "run_id": run_id})["data"]

    def knowledge_query(self, agent: str, keyword: str = "",
                        tags: Optional[List[str]] = None, limit: int = 10) -> List[Dict[str, Any]]:
        return self.call("api_knowledge_query", arguments={
            "agent": agent, "keyword": keyword, "tags": tags or [], "limit": limit})["data"]

    def decision_record(self, agent: str, stage: str, action: str, rationale: str,
                        run_id: Optional[str] = None,
                        params: Optional[Dict[str, Any]] = None) -> str:
        return self.call("api_decision_record", arguments={
            "agent": agent, "stage": stage, "action": action, "rationale": rationale,
            "run_id": run_id, "params": params or {}})["data"]["decision_id"]

    def decision_outcome(self, decision_id: str, status: str,
                         metrics: Optional[Dict[str, Any]] = None,
                         note: str = "") -> Dict[str, Any]:
        return self.call("api_decision_outcome", arguments={
            "decision_id": decision_id, "status": status,
            "metrics": metrics or {}, "note": note})["data"]

    def decision_query(self, agent: Optional[str] = None, run_id: Optional[str] = None,
                       status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return self.call("api_decision_query", arguments={
            "agent": agent, "run_id": run_id, "status": status, "limit": limit})["data"]

    def flow_start(self, template: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("api_flow_start", arguments={
            "template": template, "params": params or {}})["data"]

    def flow_status(self, run_id: str) -> Dict[str, Any]:
        return self.call("api_flow_status", arguments={"run_id": run_id})["data"]

    def flow_trace(self, run_id: str) -> Dict[str, Any]:
        return self.call("api_flow_trace", arguments={"run_id": run_id})["data"]

    def list_flow_templates(self) -> List[Dict[str, Any]]:
        return self.call("api_list_flow_templates")["data"]

    def session(self, tool: str) -> "InstanceSession":
        return InstanceSession(self, tool)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except OSError:
                pass

    def __enter__(self) -> "HacooClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class InstanceSession:
    """实例会话: 绑定 instance_id, 退出时自动释放 (R11)。"""

    def __init__(self, client: HacooClient, tool: str) -> None:
        self.client = client
        self.tool = tool
        self.instance_id: Optional[str] = None

    def __enter__(self) -> "InstanceSession":
        self.instance_id = self.client.spawn(self.tool)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.instance_id:
            try:
                self.client.release(self.instance_id)
            except HacooClientError:
                pass
            self.instance_id = None

    def call(self, method: str, **arguments: Any) -> Dict[str, Any]:
        if not self.instance_id:
            raise HacooClientError("NO_INSTANCE", "session not entered")
        timeout = arguments.pop("timeout", None)
        async_ = arguments.pop("async_", False)
        return self.client.call(method, instance_id=self.instance_id,
                                timeout=timeout, async_=async_, **arguments)
