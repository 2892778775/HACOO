"""网关层主体 — 能力发现 (G5-G6) / 方法分发 (G7) / 后处理 (G8-G9)。

请求路径: 主体识别 -> 控制检查 (G1-G4) -> 分发 (G7) -> [适配层/运行时层执行]
          -> 结果规范化 (G8) -> 构造结构化响应 (G9) -> 审计落盘。

多用户:
  - 实例/任务按 Principal.user 归属, 非属主访问返回 UNAUTHORIZED (admin 豁免)
  - spawn 受 max_instances_per_user 配额限制
  - api_list_instances 仅列属主实例 (admin 可见全部)

R8 长任务: metadata.async_=true 时提交线程池异步执行,
           api_task_status 轮询 / api_task_cancel 取消。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from hacoo.audit import AuditLog
from hacoo.comm.protocol import RPCRequest, RPCResponse, now_ms
from hacoo.config import Config
from hacoo.decisions import DecisionLog
from hacoo.flow.engine import FlowEngine
from hacoo.knowledge import KnowledgeStore
from hacoo.runtime.manager import InstanceError, RuntimeManager

from hacoo import agents as agent_registry
from .control import HacooError, PreExecutionControl, Principal
from .registry import Registry

log = logging.getLogger("hacoo.gateway")

ASYNC_EXECUTOR_SIZE = 16  # 多用户并发任务池
TASK_TTL_S = 3600.0


class Gateway:
    def __init__(self, runtime: Optional[RuntimeManager] = None,
                 registry: Optional[Registry] = None,
                 control: Optional[PreExecutionControl] = None,
                 config: Optional[Config] = None,
                 audit: Optional[AuditLog] = None) -> None:
        self.config = config or Config.from_env()
        self.runtime = runtime or RuntimeManager(self.config)
        self.registry = registry or Registry()
        self.control = control or PreExecutionControl()
        self.audit = audit if audit is not None else AuditLog(self.config.audit_log)
        # 多 Agent 协作基础设施: 知识库 / 决策日志 / flow 引擎
        self.knowledge = KnowledgeStore(self.config.knowledge_dir)
        self.decisions = DecisionLog(self.config.decisions_log)
        self.flow = FlowEngine(self, runs_dir=self.config.runs_dir)
        self._executor = ThreadPoolExecutor(max_workers=ASYNC_EXECUTOR_SIZE)
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._tasks_lock = threading.Lock()
        self.started_at = time.time()

    # ------------------------------------------------------------ 主入口

    def handle(self, req: RPCRequest) -> RPCResponse:
        t0 = now_ms()
        resp = RPCResponse(request_id=req.request_id)
        principal: Optional[Principal] = None
        try:
            principal = self.control.identify(req.metadata.get("token"))  # 主体识别
            # ---- 阶段1: 执行前控制检查
            self.control.check_protocol(req)                       # G1
            # 同名 api_* 方法按实例的工具命名空间解析签名 (多工具同名方法)
            inst_tool: Optional[str] = None
            if req.instance_id:
                try:
                    inst_tool = self.runtime.route(req.instance_id).tool
                except InstanceError:
                    inst_tool = None
                # 实例已释放/不存在: 工具方法直接 NO_INSTANCE (无需再做签名检查)
                if inst_tool is None and not self.registry.is_generic(req.method):
                    raise InstanceError("unknown or released instance_id: '%s'" % req.instance_id)
            spec = self.registry.lookup(req.method, tool=inst_tool)  # G2
            if spec is None:
                raise HacooError("UNREGISTERED_METHOD",
                                 "'%s' is not a registered api_* method%s; "
                                 "call api_list_method for available methods"
                                 % (req.method, " for tool '%s'" % inst_tool if inst_tool else ""))
            self.control.check_arguments(spec, req.arguments)      # G3
            self.control.authorize(spec, principal)                # G4

            # ---- Agent 策略强制: 启停 + 工具白名单 (多 Agent 自由搭配)
            agent_name = req.metadata.get("agent")
            if agent_name:
                agent_spec = agent_registry.get(agent_name)
                if agent_spec is None:
                    raise HacooError(
                        "ARGUMENT_ERROR",
                        "unknown agent: '%s'; call api_list_agents for valid names" % agent_name)
                if not agent_registry.is_enabled(agent_name, self.config.agents_file):
                    raise HacooError(
                        "AGENT_DISABLED",
                        "agent '%s' is disabled for this experiment; an admin can enable it "
                        "via 'python -m hacoo agents enable %s'" % (agent_name, agent_name))
                if spec.kind == "tool" and spec.tool not in agent_spec.tools:
                    raise HacooError(
                        "AGENT_FORBIDDEN",
                        "agent '%s' (%s) may not use tool '%s'; allowed: %s"
                        % (agent_name, agent_spec.title, spec.tool, agent_spec.tools))

            # ---- 阶段3: 方法分发 (G7)
            if spec.kind == "generic":
                out = self._exec_generic(spec.name, req, principal)
            else:
                out = self._dispatch_to_tool(spec, req, principal)

            # ---- 后处理 (G8-G9): 规范化 + 结构化响应
            resp.status = "ok"
            resp.data = out.get("data")
            resp.text = out.get("text")
            resp.artifacts = out.get("artifacts") or []
        except HacooError as exc:
            resp.status = "error"
            resp.error = {"code": exc.code, "message": str(exc)}
        except InstanceError as exc:
            resp.status = "error"
            resp.error = {"code": "NO_INSTANCE", "message": str(exc)}
        except Exception as exc:  # 兜底, 保证响应结构化
            log.exception("unhandled gateway error")
            resp.status = "error"
            resp.error = {"code": "GATEWAY_ERROR", "message": "%s: %s" % (type(exc).__name__, exc)}
        resp.elapsed_ms = now_ms() - t0
        self.audit.record(  # request_id 全链路可追溯
            request_id=req.request_id,
            user=principal.user if principal else "<anonymous>",
            role=principal.role if principal else "<none>",
            method=req.method, instance_id=req.instance_id,
            status=resp.status,
            error_code=(resp.error or {}).get("code"),
            elapsed_ms=resp.elapsed_ms,
            client=str(req.metadata.get("client", "")),
            agent=str(req.metadata.get("agent", "")),
            run_id=str(req.metadata.get("run_id", "")))
        return resp

    # ------------------------------------------------------------ G7 分发

    def _check_instance_ownership(self, instance_id: str, principal: Principal) -> None:
        """多用户隔离: 非属主且非 admin 不得访问该实例。"""
        inst = self.runtime.route(instance_id)
        if inst.owner != principal.user and not principal.is_admin:
            raise HacooError("UNAUTHORIZED",
                             "instance %s belongs to user '%s'" % (instance_id, inst.owner))

    def _dispatch_to_tool(self, spec, req: RPCRequest, principal: Principal) -> Dict[str, Any]:
        if not req.instance_id:
            raise HacooError("NO_INSTANCE",
                             "%s requires instance_id; call api_spawn_instance first" % spec.name)
        self._check_instance_ownership(req.instance_id, principal)
        timeout = req.metadata.get("timeout")
        if req.metadata.get("async"):  # R8: 长任务异步执行
            task_id = "task-" + uuid.uuid4().hex[:12]
            with self._tasks_lock:
                self._tasks[task_id] = {"status": "running", "submitted_at": time.time(),
                                        "method": spec.name, "instance_id": req.instance_id,
                                        "owner": principal.user, "future": None}
            future = self._executor.submit(self._run_task, task_id, spec, req, timeout)
            with self._tasks_lock:
                if task_id in self._tasks:
                    self._tasks[task_id]["future"] = future
            return {"data": {"task_id": task_id, "status": "running"},
                    "text": "task %s submitted; poll with api_task_status" % task_id}
        return self._call_backend(spec, req, timeout)

    def _call_backend(self, spec, req: RPCRequest, timeout: Optional[float]) -> Dict[str, Any]:
        raw = self.runtime.call(req.instance_id, spec.name, req.arguments,
                                req.request_id, timeout=timeout)
        if raw.get("status") != "ok":
            err = raw.get("error") or {}
            raise HacooError(err.get("code", "BACKEND_ERROR"), err.get("message", "backend error"))
        return {"data": raw.get("data"), "text": raw.get("text"),
                "artifacts": raw.get("artifacts") or []}

    def _run_task(self, task_id: str, spec, req: RPCRequest, timeout: Optional[float]) -> None:
        try:
            out = self._call_backend(spec, req, timeout)
            result: Dict[str, Any] = {"status": "done", "result": out}
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
        result["finished_at"] = time.time()
        with self._tasks_lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(result)
            self._gc_tasks_locked()

    def _gc_tasks_locked(self) -> None:
        cutoff = time.time() - TASK_TTL_S
        for tid in [t for t, v in self._tasks.items()
                    if v.get("finished_at", v["submitted_at"]) < cutoff]:
            self._tasks.pop(tid, None)

    def _get_task(self, task_id: str, principal: Principal) -> Dict[str, Any]:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise HacooError("NO_INSTANCE", "unknown or expired task_id: '%s'" % task_id)
        if task.get("owner") != principal.user and not principal.is_admin:
            raise HacooError("UNAUTHORIZED", "task %s belongs to another user" % task_id)
        return task

    def _require_known_agent(self, name: str) -> None:
        if agent_registry.get(name) is None:
            raise HacooError("ARGUMENT_ERROR",
                             "unknown agent: '%s'; call api_list_agents for valid names" % name)

    def _require_enabled_agent(self, name: str) -> None:
        self._require_known_agent(name)
        if not agent_registry.is_enabled(name, self.config.agents_file):
            raise HacooError("AGENT_DISABLED",
                             "agent '%s' is disabled for this experiment" % name)

    # ------------------------------------------------------------ 通用 API

    def _exec_generic(self, name: str, req: RPCRequest, principal: Principal) -> Dict[str, Any]:
        args = req.arguments
        if name == "api_ping":  # G5: 自省查询 — 连通性
            return {"data": {"pong": True, "version": "1.0",
                             "uptime_s": round(time.time() - self.started_at, 1),
                             "instances": len(self.runtime.list_instances())},
                    "text": "pong"}
        if name == "api_whoami":
            return {"data": {"user": principal.user, "role": principal.role,
                             "capabilities": sorted(principal.capabilities),
                             "instances_used": self.runtime.count_by_owner(principal.user),
                             "instances_quota": self.config.max_instances_per_user}}
        if name == "api_list_method":  # G5: 能力发现 — 渐进式暴露
            return {"data": self.registry.list(tool=args.get("tool"))}
        if name == "api_method_detail":  # G6: 按需获取详情
            spec = self.registry.lookup(args["method"], tool=args.get("tool"))
            if spec is None:
                raise HacooError("UNREGISTERED_METHOD",
                                 "'%s' is not registered" % args["method"])
            return {"data": spec.to_dict()}
        if name == "api_list_tools":
            from hacoo.adapters import list_tools
            return {"data": list_tools()}
        if name == "api_spawn_instance":  # R1-R4, 带配额与归属
            used = self.runtime.count_by_owner(principal.user)
            quota = self.config.max_instances_per_user
            if used >= quota:
                raise HacooError("QUOTA_EXCEEDED",
                                 "user '%s' already owns %d instances (quota %d); "
                                 "release unused instances first" % (principal.user, used, quota))
            try:
                inst = self.runtime.spawn(args["tool"], owner=principal.user)
            except KeyError as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": inst.to_dict(),
                    "text": "instance %s (%s) ready; pass this instance_id in subsequent calls"
                            % (inst.instance_id, inst.tool)}
        if name == "api_release_instance":  # R11, 带归属校验
            self._check_instance_ownership(args["instance_id"], principal)
            return {"data": self.runtime.release(args["instance_id"])}
        if name == "api_list_instances":
            owner = None if principal.is_admin else principal.user
            return {"data": self.runtime.list_instances(owner=owner)}
        if name == "api_task_status":  # R8 轮询, 带归属校验
            task = self._get_task(args["task_id"], principal)
            return {"data": {k: v for k, v in task.items() if k != "future"}}
        if name == "api_task_cancel":  # R8 取消 (仅对尚未开始执行的任务生效)
            task = self._get_task(args["task_id"], principal)
            future = task.get("future")
            if task["status"] != "running":
                return {"data": {"task_id": args["task_id"], "status": task["status"],
                                 "cancelled": False},
                        "text": "task already finished"}
            cancelled = future.cancel() if future is not None else False
            if cancelled:
                with self._tasks_lock:
                    self._tasks[args["task_id"]].update(
                        {"status": "cancelled", "finished_at": time.time()})
            return {"data": {"task_id": args["task_id"], "cancelled": cancelled},
                    "text": "cancelled" if cancelled else
                            "task already running; cancellation not possible"}
        # ---- 多 Agent 协作 ----
        if name == "api_list_agents":
            return {"data": agent_registry.list_agents(self.config.agents_file)}
        # ---- 知识库 ----
        if name == "api_knowledge_add":
            self._require_enabled_agent(args["agent"])  # 被禁 Agent 不能沉淀
            entry = self.knowledge.add(
                agent=args["agent"], symptom=args["symptom"],
                resolution=args["resolution"],
                root_cause=args.get("root_cause") or "",
                tags=args.get("tags"), run_id=args.get("run_id"))
            return {"data": entry,
                    "text": "lesson %s recorded for agent '%s'" % (entry["id"], args["agent"])}
        if name == "api_knowledge_query":  # 读 KB 不受启停限制 (决策支持)
            self._require_known_agent(args["agent"])
            return {"data": self.knowledge.query(args["agent"],
                                                 keyword=args.get("keyword") or "",
                                                 tags=args.get("tags"),
                                                 limit=args.get("limit", 10))}
        if name == "api_knowledge_list":
            self._require_known_agent(args["agent"])
            return {"data": self.knowledge.list(args["agent"], limit=args.get("limit", 50))}
        # ---- 决策日志 ----
        if name == "api_decision_record":
            self._require_enabled_agent(args["agent"])
            entry = self.decisions.record(
                agent=args["agent"], run_id=args.get("run_id"), stage=args["stage"],
                action=args["action"], rationale=args["rationale"],
                params=args.get("params"))
            return {"data": entry, "text": "decision %s recorded" % entry["decision_id"]}
        if name == "api_decision_outcome":
            try:
                entry = self.decisions.outcome(args["decision_id"], args["status"],
                                               metrics=args.get("metrics"),
                                               note=args.get("note") or "")
            except KeyError as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": entry}
        if name == "api_decision_query":
            return {"data": self.decisions.query(agent=args.get("agent"),
                                                 run_id=args.get("run_id"),
                                                 status=args.get("status"),
                                                 limit=args.get("limit", 100))}
        # ---- Flow 编排 ----
        if name == "api_flow_start":
            try:
                run = self.flow.start(args["template"], args.get("params") or {},
                                      token=req.metadata.get("token", ""),
                                      user=principal.user)
            except KeyError as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": run,
                    "text": "flow run %s finished with status: %s" % (run["run_id"], run["status"])}
        if name == "api_flow_status":
            try:
                return {"data": self.flow.status(args["run_id"])}
            except KeyError as exc:
                raise HacooError("NO_INSTANCE", str(exc))
        if name == "api_flow_trace":
            try:
                return {"data": self.flow.trace(args["run_id"])}
            except KeyError as exc:
                raise HacooError("NO_INSTANCE", str(exc))
        if name == "api_list_flow_templates":
            return {"data": self.flow.list_templates()}
        raise HacooError("UNREGISTERED_METHOD", "no generic handler for %s" % name)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        self.runtime.shutdown()
        self.audit.close()
