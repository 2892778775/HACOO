"""Flow 引擎 — 模板化流程编排, 每个 stage 由指定 Agent 经网关完整控制路径执行。

- stage 的 agent 被禁用 -> run 立即失败, 并记录 AGENT_DISABLED 决策
  ("未参与本次实验的 Agent 不会被执行")
- 每个 stage 自动写一条决策记录 (accountability): 谁/何时/依据/结果
- run manifest 存 runs/<run_id>.json, 支持 api_flow_status / api_flow_trace
- 长流程可用 metadata.async_=true 异步执行, api_task_status 轮询
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from hacoo.comm.protocol import RPCRequest

from .templates import get_template, list_templates

log = logging.getLogger("hacoo.flow")


def _render(value: Any, params: Dict[str, Any]) -> Any:
    """args 模板替换: "${key}" -> params[key]。整串恰为单个占位符时
    保留原始类型 (int/float 不被字符串化, X-3 分支参数需数值类型)。"""
    if isinstance(value, str):
        for k, v in params.items():
            placeholder = "${%s}" % k
            if value == placeholder:
                return v
            value = value.replace(placeholder, str(v))
        return value
    if isinstance(value, dict):
        return {k: _render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, params) for v in value]
    return value


_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _eval_condition(cond: Optional[Dict[str, Any]], data: Dict[str, Any],
                    params: Dict[str, Any]) -> bool:
    """收敛/升级判据 (EC-1): {"metric": <data 内 dotted key>, "op", "value"}
    对 stage 返回 data 求值; 缺 metric/无法数值化 -> False (不收敛不升级)。"""
    if not cond:
        return False
    metric = cond.get("metric") or ""
    cur: Any = data
    for part in metric.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    try:
        lhs = float(cur)
        rhs = float(_render(cond.get("value"), params))
    except (TypeError, ValueError):
        return False
    op = _OPS.get(cond.get("op") or "")
    return bool(op and op(lhs, rhs))


class FlowEngine:
    def __init__(self, gateway: Any, runs_dir: str = "runs") -> None:
        self.gateway = gateway  # 反向引用: stage 走 gateway.handle 完整控制路径
        self.runs_dir = runs_dir
        os.makedirs(runs_dir, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 执行

    def start(self, template_name: str, params: Dict[str, Any],
              token: str, user: str) -> Dict[str, Any]:
        from hacoo import agents  # 延迟导入避免环
        template = get_template(template_name)
        merged = dict(template.get("params", {}))
        merged.update(params or {})
        run_id = "run-" + uuid.uuid4().hex[:10]
        merged["run_id"] = run_id  # X-4: 模板可引用 ${run_id} (如挂到 ISF flow)
        run = {
            "run_id": run_id, "template": template_name, "params": merged,
            "user": user, "status": "running", "started_at": time.time(),
            "finished_at": None, "stages": [],
            # X-4: 数据版本追踪 (复盘可回答 "这次 run 用的哪版 DB/netlist")
            "data_version": merged.get("data_version") or None,
            "design_version": merged.get("design_version") or None,
            "isf_flow_ids": [],  # X-4: run -> ISF flow 正向引用
            "enabled_agents": agents.load_enabled(self.gateway.config.agents_file),
        }
        self._save(run)
        try:
            self._execute(run, template, merged, token)
        except Exception as exc:  # 兜底: 引擎自身异常也落盘
            log.exception("flow run %s crashed", run_id)
            run["status"] = "failed"
            run["error"] = "ENGINE_ERROR: %s" % exc
        run["finished_at"] = time.time()
        self._save(run)
        return run

    def _resolve_tool(self, stage: Dict[str, Any]) -> Optional[str]:
        """X-4: 按 stage 方法名反查所需工具 (registry 按 (tool, method)
        命名空间注册)。多工具同名的方法优先 mock_eda (保持既有行为);
        通用 API (kind=generic) 返回 None — 不需要后端实例。"""
        method = stage.get("method") or ""
        spec = self.gateway.registry.lookup(method)
        if spec is None:
            return "mock_eda"  # 未注册方法照样走网关, 由 G2 拒绝并落盘
        if spec.kind == "generic":
            return None
        mock_spec = self.gateway.registry.lookup(method, tool="mock_eda")
        if mock_spec is not None:
            return "mock_eda"
        return spec.tool or "mock_eda"

    def _ensure_instance(self, instances: Dict[str, str], tool: str,
                         token: str, run_id: str) -> str:
        """按工具惰性 spawn 实例并缓存 (同工具的多个 stage 共享实例,
        保持 R6 状态连续; 不同工具各占一席, 受配额约束)。"""
        if tool in instances:
            return instances[tool]
        spawn_req = RPCRequest(
            method="api_spawn_instance", arguments={"tool": tool},
            metadata={"token": token, "agent": "flow", "run_id": run_id})
        resp = self.gateway.handle(spawn_req)
        if resp.status != "ok":
            raise RuntimeError("spawn %s failed: %s"
                               % (tool, (resp.error or {}).get("message")))
        instances[tool] = resp.data["instance_id"]
        return instances[tool]

    def _execute(self, run: Dict[str, Any], template: Dict[str, Any],
                 params: Dict[str, Any], token: str) -> None:
        run_id = run["run_id"]
        instances: Dict[str, str] = {}  # tool -> instance_id (X-4 按工具缓存)
        try:
            for stage in template["stages"]:
                if stage.get("parallel"):
                    # X-3: 并行 fan-out (每分支独立实例, 完成后 compare)
                    self._run_parallel_stage(run, stage, params, token)
                    if run["status"] != "running":
                        return
                    continue
                tool = self._resolve_tool(stage)
                instance_id = (self._ensure_instance(instances, tool, token, run_id)
                               if tool else None)
                if stage.get("loop"):
                    # EC-1: 阶段内循环 (收敛/上限/升级); 线性 stage 行为不变
                    self._run_loop_stage(run, stage, params, token, instance_id)
                else:
                    self._run_one_stage(run, stage, params, token, instance_id)
                if run["status"] != "running":
                    return
            run["status"] = "success"
        finally:
            for inst_id in instances.values():
                rel = RPCRequest(method="api_release_instance",
                                 arguments={"instance_id": inst_id},
                                 metadata={"token": token, "agent": "flow",
                                           "run_id": run_id})
                self.gateway.handle(rel)

    def _run_one_stage(self, run: Dict[str, Any], stage: Dict[str, Any],
                       params: Dict[str, Any], token: str,
                       instance_id: Optional[str],
                       extra_params: Optional[Dict[str, Any]] = None,
                       sink: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        """执行单个 stage (或 loop body 子阶段): 决策记账 -> 禁用检查 ->
        网关完整控制路径调用 -> outcome 闭环。成功返回 resp.data (dict);
        失败/禁用置 run 失败并返回 None。sink 提供时 entry 写入 sink
        而非 run["stages"] (loop 迭代内收集)。instance_id=None 表示
        通用 API stage (无后端实例, X-4)。"""
        from hacoo import agents
        run_id = run["run_id"]
        entry = {"name": stage["name"], "agent": stage["agent"],
                 "method": stage["method"], "status": "running",
                 "started_at": time.time()}
        if extra_params:
            entry["params"] = dict(extra_params)
        (sink if sink is not None else run["stages"]).append(entry)
        self._save(run)

        # 决策记录: 引擎自动记账 (谁/依据什么/做了什么)
        decision = self.gateway.decisions.record(
            agent=stage["agent"], run_id=run_id, stage=stage["name"],
            action=stage["method"],
            rationale="flow template '%s' stage" % run["template"],
            params=dict(_render(stage.get("args") or {}, params),
                        **(extra_params or {})))

        # 禁用检查: 被禁 Agent 的 stage 直接失败, 绝不执行
        if not agents.is_enabled(stage["agent"], self.gateway.config.agents_file):
            entry["status"] = "failed"
            entry["error"] = "AGENT_DISABLED: agent '%s' is not enabled for this run" % stage["agent"]
            self.gateway.decisions.outcome(
                decision["decision_id"], "failed", note=entry["error"])
            run["status"] = "failed"
            run["error"] = entry["error"]
            self._save(run)
            return None

        req = RPCRequest(
            method=stage["method"],
            arguments=_render(stage.get("args") or {}, params),
            instance_id=instance_id,
            metadata={"token": token, "agent": stage["agent"], "run_id": run_id})
        resp = self.gateway.handle(req)
        entry["finished_at"] = time.time()
        entry["request_id"] = req.request_id  # 关联审计日志
        if resp.status != "ok":
            entry["status"] = "failed"
            entry["error"] = (resp.error or {}).get("message", "unknown")
            entry["error_code"] = (resp.error or {}).get("code")
            self.gateway.decisions.outcome(
                decision["decision_id"], "failed",
                metrics={"elapsed_ms": resp.elapsed_ms}, note=entry["error"])
            run["status"] = "failed"
            run["error"] = "stage '%s' failed: %s" % (stage["name"], entry["error"])
            self._save(run)
            return None
        entry["status"] = "ok"
        entry["result_text"] = resp.text
        entry["result_data"] = resp.data  # EC-1: 收敛判据的数据源
        # X-4: stage 结果含 flow_id -> 收集为 run -> ISF flow 正向引用
        if isinstance(resp.data, dict) and resp.data.get("flow_id"):
            flow_id = str(resp.data["flow_id"])
            if flow_id not in run.setdefault("isf_flow_ids", []):
                run["isf_flow_ids"].append(flow_id)
        self.gateway.decisions.outcome(
            decision["decision_id"], "ok",
            metrics={"elapsed_ms": resp.elapsed_ms},
            note=resp.text or "")
        self._save(run)
        return resp.data if isinstance(resp.data, dict) else {}

    def _run_loop_stage(self, run: Dict[str, Any], stage: Dict[str, Any],
                        params: Dict[str, Any], token: str,
                        instance_id: str) -> None:
        """EC-1 阶段内循环: 重复执行 body 子阶段序列, 每轮结束评估
        converge_when (收敛退出) / escalate_when (主动升级); max_iter
        耗尽未收敛 -> run 状态 escalated (升级人工)。全部迭代决策进
        trace (decisions 带 _iteration 参数)。"""
        loop = stage["loop"]
        try:
            max_iter = int(float(_render(loop.get("max_iter", 3), params)))
        except (TypeError, ValueError):
            max_iter = 3
        max_iter = max(1, max_iter)
        entry = {"name": stage["name"], "agent": stage["agent"],
                 "method": "loop", "status": "running",
                 "started_at": time.time(),
                 "loop": {"max_iter": max_iter,
                          "converge_when": loop.get("converge_when"),
                          "iterations": []}}
        run["stages"].append(entry)
        self._save(run)

        # 循环级决策: 谁发起了这个收敛循环, 依据什么判据
        decision = self.gateway.decisions.record(
            agent=stage["agent"], run_id=run["run_id"], stage=stage["name"],
            action="eco_loop",
            rationale="flow template '%s' loop stage (max_iter=%d, converge_when=%s)"
                      % (run["template"], max_iter, loop.get("converge_when")),
            params=_render(loop.get("converge_when") or {}, params))

        body = stage.get("body") or []
        converged_at: Optional[int] = None
        escalate_reason: Optional[str] = None
        for it in range(1, max_iter + 1):
            iter_entry = {"iteration": it, "stages": []}
            entry["loop"]["iterations"].append(iter_entry)
            source_data: Dict[str, Any] = {}
            last_data: Dict[str, Any] = {}
            for sub in body:
                data = self._run_one_stage(
                    run, sub, params, token, instance_id,
                    extra_params={"_iteration": it},
                    sink=iter_entry["stages"])
                if data is None:  # 子阶段失败/禁用 -> run 已置 failed
                    entry["status"] = "failed"
                    entry["error"] = run.get("error")
                    self.gateway.decisions.outcome(
                        decision["decision_id"], "failed",
                        note=run.get("error") or "")
                    self._save(run)
                    return
                if sub["name"] == loop.get("source"):
                    source_data = data
                last_data = data
            judge_data = source_data or last_data
            iter_entry["judge"] = {k: judge_data.get(k) for k in
                                   ("wns", "tns") if k in judge_data}
            self._save(run)
            if _eval_condition(loop.get("converge_when"), judge_data, params):
                converged_at = it
                break
            if loop.get("escalate_when") and _eval_condition(
                    loop["escalate_when"], judge_data, params):
                escalate_reason = ("escalate_when matched at iteration %d: %s"
                                   % (it, loop["escalate_when"]))
                break

        entry["finished_at"] = time.time()
        if converged_at is not None:
            entry["status"] = "ok"
            self.gateway.decisions.outcome(
                decision["decision_id"], "ok",
                metrics={"iterations": converged_at},
                note="converged at iteration %d/%d" % (converged_at, max_iter))
            self._save(run)
            return
        # 升级人工: 显式 escalate_when 命中 或 max_iter 耗尽未收敛
        reason = escalate_reason or (
            "max_iter %d exhausted without convergence (converge_when=%s, last=%s)"
            % (max_iter, loop.get("converge_when"),
               (entry["loop"]["iterations"] or [{}])[-1].get("judge")))
        entry["status"] = "escalated"
        entry["error"] = reason
        self.gateway.decisions.outcome(
            decision["decision_id"], "escalated", note=reason)
        run["status"] = "escalated"
        run["error"] = "stage '%s' escalated: %s" % (stage["name"], reason)
        self._save(run)

    def _run_parallel_stage(self, run: Dict[str, Any], stage: Dict[str, Any],
                            params: Dict[str, Any], token: str) -> None:
        """X-3 并行 fan-out: 同一 agent 按 parallel 参数组开多个独立实例
        并行执行 (每分支独立 spawn/release, 互不共享状态; 可选 setup 子
        阶段在分支内先跑, 使分支状态自含)。全部完成后按 compare
        {metrics, pick: min|max} 选出优胜分支。fan-out 数受
        max_instances_per_user 配额限制, 超限即失败 (QUOTA_EXCEEDED),
        绝不静默排队。任一分支失败 -> run 失败。"""
        branches = list(stage.get("parallel") or [])
        compare = stage.get("compare") or {}
        run_id = run["run_id"]
        entry = {"name": stage["name"], "agent": stage["agent"],
                 "method": stage.get("method", ""), "status": "running",
                 "started_at": time.time(),
                 "parallel": {"branches": [{"branch": i, "params": dict(p),
                                            "stages": []}
                                           for i, p in enumerate(branches)],
                              "compare": compare or None}}
        run["stages"].append(entry)
        self._save(run)

        # fan-out 级决策: 谁发起了并行探索, 各分支参数是什么
        decision = self.gateway.decisions.record(
            agent=stage["agent"], run_id=run_id, stage=stage["name"],
            action="fanout",
            rationale="flow template '%s' parallel stage (%d branches)"
                      % (run["template"], len(branches)),
            params={"branches": [dict(p) for p in branches]})

        # 配额预检: 已持有席位 (X-4 起按工具懒 spawn) + 分支席位;
        # 超限直接失败报 QUOTA_EXCEEDED (不静默排队)
        used = self.gateway.runtime.count_by_owner(run["user"])
        quota = self.gateway.config.max_instances_per_user
        if used + len(branches) > quota:
            reason = ("QUOTA_EXCEEDED: fan-out needs %d more instances "
                      "(user '%s' owns %d, quota %d); reduce parallel "
                      "branches or release instances"
                      % (len(branches), run["user"], used, quota))
            entry["status"] = "failed"
            entry["error"] = reason
            entry["finished_at"] = time.time()
            self.gateway.decisions.outcome(
                decision["decision_id"], "failed", note=reason)
            run["status"] = "failed"
            run["error"] = "stage '%s' failed: %s" % (stage["name"], reason)
            self._save(run)
            return

        branch_tool = self._resolve_tool(stage) or "mock_eda"  # X-4: 按方法反查

        def _branch(idx: int, branch_params: Dict[str, Any],
                    sink: List[Dict[str, Any]]) -> None:
            merged = dict(params)
            merged.update(branch_params)
            inst_id: Optional[str] = None
            try:
                spawn_req = RPCRequest(
                    method="api_spawn_instance",
                    arguments={"tool": branch_tool},
                    metadata={"token": token, "agent": stage["agent"],
                              "run_id": run_id})
                resp = self.gateway.handle(spawn_req)
                if resp.status != "ok":
                    raise RuntimeError("branch %d spawn failed: %s"
                                       % (idx, (resp.error or {}).get("message")))
                inst_id = resp.data["instance_id"]
                for sub in stage.get("setup") or []:
                    data = self._run_one_stage(
                        run, sub, merged, token, inst_id,
                        extra_params={"_branch": idx}, sink=sink)
                    if data is None:
                        return  # run 已置 failed
                data = self._run_one_stage(
                    run, stage, merged, token, inst_id,
                    extra_params=dict(branch_params, _branch=idx), sink=sink)
                if data is None:
                    return
                sink[-1]["_branch_data"] = data  # compare 数据源
            except Exception as exc:  # 兜底: 分支自身异常也落盘
                log.exception("fan-out branch %d crashed", idx)
                sink.append({"name": stage["name"], "agent": stage["agent"],
                             "method": stage.get("method", ""),
                             "status": "failed", "error": "BRANCH_ERROR: %s" % exc})
                run["status"] = "failed"
                run["error"] = ("stage '%s' branch %d crashed: %s"
                                % (stage["name"], idx, exc))
            finally:
                if inst_id:
                    rel = RPCRequest(method="api_release_instance",
                                     arguments={"instance_id": inst_id},
                                     metadata={"token": token,
                                               "agent": stage["agent"],
                                               "run_id": run_id})
                    self.gateway.handle(rel)

        threads = []
        for i, bp in enumerate(branches):
            t = threading.Thread(
                target=_branch, args=(i, bp, entry["parallel"]["branches"][i]["stages"]),
                name="fanout-%s-%d" % (run_id, i), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        entry["finished_at"] = time.time()
        if run["status"] != "running":
            entry["status"] = "failed"
            entry["error"] = run.get("error")
            self.gateway.decisions.outcome(
                decision["decision_id"], "failed",
                note=run.get("error") or "")
            self._save(run)
            return

        # compare: 模板声明的指标在各分支结果间比选 (pick min|max, 平手取前)
        result = self._compare_branches(entry["parallel"]["branches"], compare)
        if result is not None:
            entry["parallel"]["compare_result"] = result
        entry["status"] = "ok"
        note = ("fan-out %d branches ok" % len(branches))
        if result and result.get("winner"):
            note += "; winner=branch %d (%s)" % (
                result["winner"]["branch"],
                ", ".join("%s=%s" % (m, result["winner"]["metrics"].get(m))
                          for m in (compare.get("metrics") or [])))
        self.gateway.decisions.outcome(
            decision["decision_id"], "ok", note=note)
        self._save(run)

    @staticmethod
    def _compare_branches(branches: List[Dict[str, Any]],
                          compare: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按 compare {metrics, pick} 在各分支末段结果数据间比选, 返回
        compare_result (含 winner); compare 未声明 -> None。"""
        metrics = compare.get("metrics") or []
        pick = compare.get("pick", "max")
        if not metrics:
            return None
        per_branch: List[Dict[str, Any]] = []
        for i, br in enumerate(branches):
            data = {}
            for st in reversed(br.get("stages") or []):
                if isinstance(st.get("_branch_data"), dict):
                    data = st["_branch_data"]
                    break
            per_branch.append({"branch": i, "params": br.get("params") or {},
                               "metrics": {m: data.get(m) for m in metrics}})
        winner: Optional[Dict[str, Any]] = None
        for cand in per_branch:
            vals = cand["metrics"]
            if any(vals.get(m) is None for m in metrics):
                continue
            if winner is None:
                winner = cand
                continue
            better = all(vals[m] < winner["metrics"][m] for m in metrics) \
                if pick == "min" else \
                all(vals[m] > winner["metrics"][m] for m in metrics)
            if better:
                winner = cand
        return {"pick": pick, "branches": per_branch, "winner": winner}

    # ------------------------------------------------------------ 查询

    def _path(self, run_id: str) -> str:
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return os.path.join(self.runs_dir, "%s.json" % safe)

    def _save(self, run: Dict[str, Any]) -> None:
        # X-3: 并行分支会并发修改 run (各自 sink), 序列化时可能撞上
        # "dictionary changed size" — 短重试 + default=str 兜底
        with self._lock:
            payload: Optional[str] = None
            for _ in range(3):
                try:
                    payload = json.dumps(run, ensure_ascii=False, indent=2)
                    break
                except (RuntimeError, ValueError):
                    time.sleep(0.005)
            if payload is None:
                payload = json.dumps(run, ensure_ascii=False, indent=2,
                                     default=str)
            with open(self._path(run["run_id"]), "w", encoding="utf-8") as fh:
                fh.write(payload)

    def status(self, run_id: str) -> Dict[str, Any]:
        path = self._path(run_id)
        if not os.path.isfile(path):
            raise KeyError("unknown run_id: '%s'" % run_id)
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def list_runs(self, limit: int = 100,
                  status: Optional[str] = None) -> List[Dict[str, Any]]:
        """汇总 runs/*.json manifest 的摘要列表 (按 started_at 倒序)。

        每条摘要: run_id / template / status / user / started_at /
        finished_at / duration_s / 阶段数 / error (如有)。
        """
        runs: List[Dict[str, Any]] = []
        if os.path.isdir(self.runs_dir):
            for fname in os.listdir(self.runs_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self.runs_dir, fname), "r",
                              encoding="utf-8") as fh:
                        run = json.load(fh)
                except (OSError, ValueError):
                    continue
                if status and run.get("status") != status:
                    continue
                started = run.get("started_at") or 0
                finished = run.get("finished_at")
                runs.append({
                    "run_id": run.get("run_id", fname[:-5]),
                    "template": run.get("template", ""),
                    "status": run.get("status", "unknown"),
                    "user": run.get("user", ""),
                    "started_at": started,
                    "finished_at": finished,
                    "duration_s": (round(finished - started, 3)
                                   if finished and started else None),
                    "n_stages": len(run.get("stages") or []),
                    "error": run.get("error"),
                })
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]

    def trace(self, run_id: str) -> Dict[str, Any]:
        """问题定位视图: run manifest + 决策时间线 + 关联审计条目。"""
        run = self.status(run_id)
        decisions = self.gateway.decisions.query(run_id=run_id, limit=500)
        audit_entries = self.gateway.audit.query(run_id)
        return {"run": run, "decisions": decisions, "audit": audit_entries}

    def list_templates(self) -> List[Dict[str, Any]]:
        return list_templates()
