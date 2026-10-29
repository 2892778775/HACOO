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

import json
import logging
import os
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
from hacoo.insights import InsightStore, detect_cross_domain, run_detectors
from hacoo.knowledge import KnowledgeStore
from hacoo.observations import (ObservationStore, extract_error_signatures,
                                extract_hotspots, extract_metrics)
from hacoo.runtime.manager import InstanceError, RuntimeManager
from hacoo.upgrades import UpgradeStore

from hacoo import agents as agent_registry
from .control import HacooError, PreExecutionControl, Principal
from .registry import Registry

log = logging.getLogger("hacoo.gateway")

ASYNC_EXECUTOR_SIZE = 16  # 多用户并发任务池
TASK_TTL_S = 3600.0


class Gateway:
    # SO-5: 问责敏感操作的决策记录有效窗口 (秒) — 决策必须先于操作
    DECISION_WINDOW_S = 3600.0

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
        self.knowledge = KnowledgeStore(self.config.knowledge_dir,
                                        miss_log=self.config.kb_misses_log)
        self.decisions = DecisionLog(self.config.decisions_log)
        self.flow = FlowEngine(self, runs_dir=self.config.runs_dir)
        # 成长闭环: 观测沉淀 + 主动发现 (SPEC-agent-growth-loop §1/§3)
        self.observations = ObservationStore(self.config.observations_dir)
        self.insights = InsightStore(self.config.insights_log)
        # 升级提案管理 (SPEC-evolution Part A)
        self.upgrades = UpgradeStore(self.config.upgrades_log)
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

            # ---- 问责敏感操作强制 (SO-5): requires_decision 的方法必须先有
            # 同 agent 的决策记录 (waiver 等不可匿名执行)
            if spec.kind == "tool" and getattr(spec, "requires_decision", False):
                self._require_decision_record(spec, agent_name,
                                              req.metadata.get("run_id"))

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
            failure = HacooError(err.get("code", "BACKEND_ERROR"),
                                 err.get("message", "backend error"))
            self._record_observation(spec, req, ok=False, out=None, error=failure)
            raise failure
        out = {"data": raw.get("data"), "text": raw.get("text"),
               "artifacts": raw.get("artifacts") or []}
        self._record_observation(spec, req, ok=True, out=out, error=None)
        return out

    def _require_decision_record(self, spec, agent: Optional[str],
                                 run_id: Optional[str]) -> None:
        """SO-5 问责敏感操作强制: requires_decision 的方法 (如 waiver) 必须
        有同 agent 的有效决策记录 (同 run 内, 或近 1 小时窗口), 否则拒绝。"""
        if not agent:
            raise HacooError(
                "DECISION_REQUIRED",
                "'%s' is accountability-sensitive and requires an agent identity; "
                "record a decision (api_decision_record) and call with agent set"
                % spec.name)
        entries = self.decisions.query(agent=agent, run_id=run_id, limit=500)
        now = time.time()
        valid = [e for e in entries
                 if e.get("status") in ("pending", "ok")
                 and now - float(e.get("ts") or 0) <= self.DECISION_WINDOW_S]
        if not valid:
            raise HacooError(
                "DECISION_REQUIRED",
                "'%s' is accountability-sensitive: agent '%s' must record a "
                "decision first (api_decision_record%s), then retry"
                % (spec.name, agent, " with the same run_id" if run_id else ""))

    def _record_observation(self, spec, req: RPCRequest, ok: bool,
                            out: Optional[Dict[str, Any]],
                            error: Optional[HacooError]) -> None:
        """观测钩子 (成长闭环 §1.3): 工具调用成功/失败确定后落盘一条观测。

        仅记录带 agent 的 kind="tool" 调用 (通用方法不记录, 避免噪声);
        失败也记录 (ok=false + 从错误消息提取签名)。
        钩子自身失败 (磁盘满等) 只记 warning, 绝不影响主调用返回。
        """
        agent = req.metadata.get("agent")
        if not agent or spec.kind != "tool":
            return
        try:
            if ok and out is not None:
                text = out.get("text") or ""
                self.observations.record(
                    agent=agent, run_id=req.metadata.get("run_id"),
                    tool=spec.tool, method=spec.name,
                    instance_id=req.instance_id, ok=True,
                    metrics=extract_metrics(spec.tool, out.get("data")),
                    error_signatures=extract_error_signatures(text),
                    excerpt=text,
                    hotspots=extract_hotspots(out.get("data")))  # X-5
            elif error is not None:
                # 统一带 ERROR 前缀, 保证失败观测总能提取出签名
                message = "ERROR [%s] %s" % (error.code, error)
                self.observations.record(
                    agent=agent, run_id=req.metadata.get("run_id"),
                    tool=spec.tool, method=spec.name,
                    instance_id=req.instance_id, ok=False,
                    metrics={},
                    error_signatures=extract_error_signatures(message),
                    excerpt=message)
        except Exception:
            log.warning("observation hook failed for %s", spec.name, exc_info=True)

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

    def _agent_version(self, name: str) -> Optional[str]:
        """知识条目沉淀时的 Agent 生效版本 (SPEC-evolution A.3.3, 按版本归因经验)。"""
        try:
            return agent_registry.version_of(name, self.config.agents_file)
        except KeyError:
            return None

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
                tags=args.get("tags"), run_id=args.get("run_id"),
                profile=args.get("profile"),
                agent_version=self._agent_version(args["agent"]))
            return {"data": entry,
                    "text": "lesson %s recorded for agent '%s'" % (entry["id"], args["agent"])}
        if name == "api_knowledge_query":  # 读 KB 不受启停限制 (决策支持)
            self._require_known_agent(args["agent"])
            return {"data": self.knowledge.query(args["agent"],
                                                 keyword=args.get("keyword") or "",
                                                 tags=args.get("tags"),
                                                 limit=args.get("limit", 10),
                                                 profile=args.get("profile"))}
        if name == "api_knowledge_list":
            self._require_known_agent(args["agent"])
            return {"data": self.knowledge.list(args["agent"], limit=args.get("limit", 50))}
        # ---- 人类反馈与复核 (成长闭环 §2) ----
        # 以下方法均为人类面向: 与 api_knowledge_query 一样不受 Agent 启停限制
        if name == "api_feedback_submit":
            self._require_known_agent(args["agent"])
            ftype = args.get("feedback_type") or "correction"
            if ftype not in ("correction", "confirmation", "note"):
                raise HacooError("ARGUMENT_ERROR",
                                 "feedback_type must be correction|confirmation|note, "
                                 "got '%s'" % ftype)
            decision_id = args.get("decision_id")
            run_id = args.get("run_id")
            if decision_id:  # 绑定的决策必须存在且属于该 agent
                dec = self.decisions.get(decision_id)
                if dec is None or dec["agent"] != args["agent"]:
                    raise HacooError(
                        "ARGUMENT_ERROR",
                        "decision '%s' does not exist or does not belong to agent '%s'"
                        % (decision_id, args["agent"]))
                run_id = run_id or dec.get("run_id")
            content = args["content"]
            entry = self.knowledge.add(
                agent=args["agent"], symptom=content,
                resolution=content if ftype != "note" else "",
                tags=args.get("tags"), run_id=run_id,
                source="human_correction" if ftype == "correction" else "human",
                decision_id=decision_id,
                agent_version=self._agent_version(args["agent"]))
            return {"data": entry,
                    "text": "feedback (%s) recorded as %s for agent '%s'"
                            % (ftype, entry["id"], args["agent"])}
        if name == "api_review_queue":
            agent = args.get("agent")
            if agent:
                self._require_known_agent(agent)
            limit = args.get("limit", 50)
            pending = self.decisions.query(agent=agent, status="pending", limit=limit)
            unreviewed = self.knowledge.unreviewed(agent=agent, limit=limit)
            return {"data": {"decisions": pending, "knowledge": unreviewed,
                             "total": len(pending) + len(unreviewed)}}
        if name == "api_knowledge_review":
            try:
                entry = self.knowledge.review(args["entry_id"], args["action"],
                                              note=args.get("note") or "")
            except (KeyError, ValueError) as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": entry,
                    "text": "knowledge %s %sd" % (entry["id"], args["action"])}
        # ---- 主动发现 (成长闭环 §3, 同为人类面向) ----
        if name == "api_insights_scan":
            agent = args.get("agent")
            if agent:
                self._require_known_agent(agent)
                targets = [agent]
            else:
                targets = agent_registry.load_enabled(self.config.agents_file)
            window_days = args.get("window_days", 7)
            now = time.time()
            since = now - window_days * 86400.0
            created, skipped = [], 0
            obs_cache: Dict[str, List[Dict[str, Any]]] = {}
            for target in targets:
                obs_cache[target] = self.observations.query(
                    agent=target, since_ts=since, limit=10000)
                candidates = run_detectors(
                    agent=target,
                    observations=obs_cache[target],
                    decisions=self.decisions.query(agent=target, limit=10000),
                    kb_misses=self._load_kb_misses(agent=target, since_ts=since),
                    knowledge_entries=self.knowledge.list(target, limit=10000),
                    now_ts=now)
                for cand in candidates:
                    if self.insights.exists_active(cand["signature"]):
                        skipped += 1  # 同签名已存在 (含 dismissed), 不重复报警
                        continue
                    created.append(self.insights.add(**cand))
            # X-5: 跨域关联检测器 (需双侧观测, 在逐 agent 检测之后运行)
            cross_targets = [t for t in ("thermal", "irem") if t in targets]
            if len(cross_targets) == 2:
                for cand in detect_cross_domain(
                        {t: obs_cache[t] for t in cross_targets}):
                    if self.insights.exists_active(cand["signature"]):
                        skipped += 1
                        continue
                    created.append(self.insights.add(**cand))
            return {"data": {"created": created, "skipped": skipped, "agents": targets},
                    "text": "scan done: %d new insight(s), %d skipped (already known)"
                            % (len(created), skipped)}
        if name == "api_insights_list":
            return {"data": self.insights.query(agent=args.get("agent"),
                                                kind=args.get("kind"),
                                                status=args.get("status"),
                                                limit=args.get("limit", 100))}
        if name == "api_insight_review":
            try:
                insight = self.insights.review(args["insight_id"], args["action"],
                                               note=args.get("note") or "")
            except (KeyError, ValueError) as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            data = dict(insight)
            if args["action"] == "confirm" and args.get("auto_kb", True):
                # 确认的 insight 回流知识库, 关联来源 insight_id
                kb_entry = self.knowledge.add(
                    agent=insight["agent"], symptom=insight["title"],
                    root_cause=insight.get("detail", ""),
                    resolution=insight.get("proposed_action", ""),
                    tags=["insight", insight["kind"]],
                    source="insight", insight_id=insight["insight_id"],
                    agent_version=self._agent_version(insight["agent"]))
                data["kb_entry_id"] = kb_entry["id"]
            if args["action"] == "confirm" and args.get("create_upgrade"):
                # SPEC-evolution A.5: insight 确认可联动生成升级提案
                proposal = self.upgrades.propose(
                    title=insight["title"],
                    kind="agent_capability", target=insight["agent"],
                    motivation=insight.get("detail") or insight["title"],
                    proposed_change=insight.get("proposed_action", ""),
                    acceptance="针对 insight %s 的改进落地, 相关 bench/e2e 验证通过"
                               % insight["insight_id"],
                    source="insight", source_ref=insight["insight_id"])
                data["upgrade_proposal_id"] = proposal["proposal_id"]
            return {"data": data,
                    "text": "insight %s %s" % (insight["insight_id"], insight["status"])}
        # ---- 升级提案管理 (SPEC-evolution Part A, 同为人类面向) ----
        if name == "api_upgrade_propose":
            try:
                entry = self.upgrades.propose(
                    title=args["title"], kind=args["kind"], target=args["target"],
                    motivation=args["motivation"],
                    acceptance=args.get("acceptance") or "",
                    proposed_change=args.get("proposed_change") or "",
                    source=args.get("source") or "human",
                    source_ref=args.get("source_ref"))
            except ValueError as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": entry,
                    "text": "upgrade proposal %s created (%s)" % (entry["proposal_id"],
                                                                  entry["status"])}
        if name == "api_upgrade_list":
            return {"data": self.upgrades.query(status=args.get("status"),
                                                target=args.get("target"),
                                                limit=args.get("limit", 100))}
        if name == "api_upgrade_update":
            try:
                entry = self.upgrades.transition(
                    args["proposal_id"], args["to_status"],
                    note=args.get("note") or "",
                    version_after=args.get("version_after"))
            except (KeyError, ValueError) as exc:
                raise HacooError("ARGUMENT_ERROR", str(exc))
            return {"data": entry,
                    "text": "proposal %s -> %s" % (entry["proposal_id"], entry["status"])}
        if name == "api_observations_query":
            return {"data": self.observations.query(agent=args.get("agent"),
                                                    tool=args.get("tool"),
                                                    run_id=args.get("run_id"),
                                                    limit=args.get("limit", 500))}
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
        if name == "api_runs_list":
            return {"data": self.flow.list_runs(limit=args.get("limit", 100),
                                                status=args.get("status"))}
        raise HacooError("UNREGISTERED_METHOD", "no generic handler for %s" % name)

    def _load_kb_misses(self, agent: Optional[str] = None,
                        since_ts: Optional[float] = None) -> list:
        """读取 KB miss 日志 (知识盲区检测器的数据源)。"""
        path = self.config.kb_misses_log
        if not path or not os.path.isfile(path):
            return []
        out = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if agent and entry.get("agent") != agent:
                    continue
                if since_ts is not None and entry.get("ts", 0) < since_ts:
                    continue
                out.append(entry)
        return out

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)
        self.runtime.shutdown()
        self.audit.close()
