"""能力基准 (SPEC-evolution B.3, 能力层) — 新能力是否真的有效?

golden case 存于 benchmarks/<agent>/<case-id>.json (git 追踪); 每个 case 在
独立沙箱 server 中执行 setup -> script -> assertions, 结果为 pass/fail +
失败原因, 可与 baselines/<agent>@<version>.json 对比 (fail->pass / pass->fail)。

升级验收 (B.3.4): 目标 case 必须 fail->pass (或新建即 pass), 整体通过率不回退。

断言类型 (5 种): call_ok / knowledge_hit / decision_recorded / output_contains /
metric_observed (BE-2: 断言某 metric 已进入观测层)。
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

DEFAULT_BENCHMARKS_DIR = "benchmarks"
DEFAULT_BASELINES_DIR = "baselines"
DEFAULT_BENCH_RUNS_DIR = "bench_runs"  # 运行时数据 (.gitignore)

ASSERTION_TYPES = ("call_ok", "knowledge_hit", "decision_recorded",
                   "output_contains", "metric_observed")

# case.requires 声明的工具 -> 默认可执行文件名 (与各 adapter 的
# resolve_binary 调用保持一致; 未列出的工具按工具名本身探测)
_TOOL_BIN_DEFAULTS: Dict[str, List[str]] = {
    "icc2": ["icc2_shell"],
    "innovus": ["innovus"],
    "primetime": ["pt_shell"],
    "calibre": ["calibre"],
    "voltus": ["voltus"],
    "redhawk_sc": ["redhawk_sc"],
    "genus": ["genus"],
    "dc_shell": ["dc_shell-t"],
    "tessent": ["tessent"],
    "dftmax": ["dc_shell-t"],
}


def tool_available(tool: str) -> bool:
    """requires 可用性检查 (BE-2 增强): HACOO_<TOOL>_MOCK=1 视为可用;
    否则按 adapter 相同的 resolve_binary 规则探测真实 binary。
    mock_eda 无外部进程依赖, 恒可用。"""
    if tool == "mock_eda":
        return True
    if os.environ.get("HACOO_%s_MOCK" % tool.upper()) == "1":
        return True
    from hacoo.adapters.shell import resolve_binary
    try:
        resolve_binary(tool, _TOOL_BIN_DEFAULTS.get(tool, [tool]))
        return True
    except RuntimeError:
        return False


def load_cases(benchmarks_dir: str = DEFAULT_BENCHMARKS_DIR,
               agent: Optional[str] = None,
               tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """加载 golden cases, 可按 agent / tags 过滤 (按 case id 排序)。"""
    pattern = os.path.join(benchmarks_dir, agent or "*", "*.json")
    cases = []
    for path in sorted(glob.glob(pattern)):
        with open(path, "r", encoding="utf-8") as fh:
            case = json.load(fh)
        if tags and not set(tags) & set(case.get("tags") or []):
            continue
        case["_path"] = path
        cases.append(case)
    return sorted(cases, key=lambda c: c.get("id", ""))


class BenchRunner:
    """golden case 执行器: 沙箱 server 中跑 setup -> script -> assertions。"""

    def __init__(self, benchmarks_dir: str = DEFAULT_BENCHMARKS_DIR,
                 baselines_dir: str = DEFAULT_BASELINES_DIR,
                 bench_runs_dir: str = DEFAULT_BENCH_RUNS_DIR) -> None:
        self.benchmarks_dir = benchmarks_dir
        self.baselines_dir = baselines_dir
        self.bench_runs_dir = bench_runs_dir

    # ------------------------------------------------------------ 单 case

    def run_case(self, case: Dict[str, Any], client: Any) -> Dict[str, Any]:
        """执行一个 golden case, 返回 {"id", "pass", "failures", "duration_s"}。

        client 为已连沙箱 server 的 HacooClient。工具方法调用自动 spawn
        case["tool"] (默认 mock_eda) 实例并以 case["agent"] 身份调用
        (白名单/启停策略真实生效); 结束后释放实例。
        """
        t0 = time.time()
        agent = case.get("agent", "")
        setup = case.get("setup") or {}
        # requires (BE-2 增强): 声明依赖真实工具的 case, 工具不可用时跳过
        # (skipped: 不计 pass 也不计 fail); 检查先于 mock_tool_state 注入,
        # 即 case 自带的 mock setup 不影响可用性判定
        missing = [t for t in (case.get("requires") or [])
                   if not tool_available(t)]
        if missing:
            return {"id": case.get("id", "?"), "agent": agent, "pass": None,
                    "skipped": True,
                    "skip_reason": "required tool(s) unavailable: %s"
                                   % ", ".join(missing),
                    "failures": [], "duration_s": round(time.time() - t0, 3)}
        steps_out: List[Dict[str, Any]] = []
        instance_id: Optional[str] = None
        # setup.mock_tool_state {"mode": "mock"}: 为无真实 EDA binary 的环境
        # 强制工具 adapter 进入 mock 模式 (HACOO_<TOOL>_MOCK=1, 仅本 case 生效,
        # backend 子进程 spawn 时继承该环境变量)
        mock_env_key: Optional[str] = None
        mock_env_prev: Optional[str] = None
        if (setup.get("mock_tool_state") or {}).get("mode") == "mock":
            mock_env_key = "HACOO_%s_MOCK" % (case.get("tool") or "mock_eda").upper()
            mock_env_prev = os.environ.get(mock_env_key)  # 预先存在则恢复而非清除
            os.environ[mock_env_key] = "1"
        try:
    # ---- setup: 知识库种子 (observations_seed 暂为预留字段;
            #            mock_tool_state {"mode": "mock"} 强制工具 mock 模式)
            for seed in setup.get("knowledge_seed") or []:
                client.knowledge_add(
                    agent=agent,
                    symptom=seed.get("symptom", ""),
                    root_cause=seed.get("root_cause", ""),
                    resolution=seed.get("resolution", ""),
                    tags=seed.get("tags") or [],
                    profile=seed.get("profile"))
            # ---- script
            spec_cache: Dict[str, Dict[str, Any]] = {}
            for step in case.get("script") or []:
                name = step.get("call", "")
                call_args = dict(step.get("args") or {})
                try:
                    if name not in spec_cache:
                        spec_cache[name] = client.method_detail(name)
                    spec = spec_cache[name]
                    if "agent" in (spec.get("params") or {}) and "agent" not in call_args:
                        call_args["agent"] = agent
                    if spec.get("kind") == "tool":
                        if instance_id is None:
                            instance_id = client.spawn(case.get("tool") or "mock_eda")
                        resp = client.call(name, instance_id=instance_id,
                                           arguments=call_args, agent=agent)
                    else:
                        resp = client.call(name, arguments=call_args)
                    steps_out.append({"call": name, "ok": True, "response": resp})
                except Exception as exc:
                    steps_out.append({"call": name, "ok": False, "error": str(exc)})
            failures = self._evaluate(case.get("assertions") or [], steps_out,
                                      client=client, agent=agent)
        finally:
            if mock_env_key:
                if mock_env_prev is None:
                    os.environ.pop(mock_env_key, None)
                else:
                    os.environ[mock_env_key] = mock_env_prev  # 恢复 case 前的值
            if instance_id:
                try:
                    client.release(instance_id)
                except Exception:
                    pass
        return {"id": case.get("id", "?"), "agent": agent,
                "pass": not failures, "failures": failures,
                "duration_s": round(time.time() - t0, 3)}

    def _evaluate(self, assertions: List[Dict[str, Any]],
                  steps: List[Dict[str, Any]], client: Any = None,
                  agent: str = "") -> List[str]:
        failures: List[str] = []
        for rule in assertions:
            rtype = rule.get("type")
            if rtype == "call_ok":
                bad = ["%s (%s)" % (s["call"], s.get("error", "?"))
                       for s in steps if not s["ok"]]
                if rule.get("all", True) and bad:
                    failures.append("call_ok: failed calls: %s" % "; ".join(bad))
            elif rtype == "knowledge_hit":
                hits = sum(len(s["response"].get("data") or [])
                           for s in steps
                           if s["ok"] and s["call"] == "api_knowledge_query")
                if hits < int(rule.get("min", 1)):
                    failures.append("knowledge_hit: %d hits < min %d"
                                    % (hits, rule.get("min", 1)))
            elif rtype == "decision_recorded":
                if not any(s["ok"] and s["call"] == "api_decision_record" for s in steps):
                    failures.append("decision_recorded: no decision recorded")
            elif rtype == "output_contains":
                needle = rule.get("text") or rule.get("substring") or ""
                targets = steps
                if rule.get("step") is not None:
                    idx = int(rule["step"])
                    targets = steps[idx:idx + 1] if 0 <= idx < len(steps) else []
                haystacks = [json.dumps({"data": s["response"].get("data"),
                                         "text": s["response"].get("text")},
                                        ensure_ascii=False, default=str)
                             for s in targets if s["ok"]]
                if not any(needle in h for h in haystacks):
                    failures.append("output_contains: '%s' not found in output" % needle)
            elif rtype == "metric_observed":
                # BE-2: 断言某 metric 已进入观测层 (网关观测钩子提取的扁平数值);
                # 可选 method 限定观测来源方法
                metric = rule.get("metric") or ""
                method = rule.get("method")
                if client is None:
                    failures.append("metric_observed: '%s' requires a client" % metric)
                    continue
                try:
                    obs = client.call("api_observations_query",
                                      arguments={"agent": agent, "limit": 500})["data"]
                except Exception as exc:
                    failures.append("metric_observed: query failed: %s" % exc)
                    continue
                hits = [o for o in obs if o.get("ok")
                        and metric in (o.get("metrics") or {})
                        and (method is None or o.get("method") == method)]
                if not hits:
                    failures.append("metric_observed: '%s' (method=%s) not found "
                                    "in observations" % (metric, method or "*"))
            else:
                failures.append("unknown assertion type: '%s' (supported: %s)"
                                % (rtype, "|".join(ASSERTION_TYPES)))
        return failures

    # ------------------------------------------------------------ 汇总

    def run_all(self, agent: Optional[str] = None,
                tags: Optional[List[str]] = None,
                client: Any = None) -> Dict[str, Any]:
        """跑全部 (或过滤后) cases, 返回汇总报告。

        client 为 None 时自建独立沙箱 server (运行时数据全部进临时目录)。
        """
        cases = load_cases(self.benchmarks_dir, agent=agent, tags=tags)
        if client is not None:
            return self._run_cases(cases, client, agent=agent)
        server, client = self._sandbox_client()
        try:
            report = self._run_cases(cases, client, agent=agent)
        finally:
            client.close()
            server.stop()
        os.makedirs(self.bench_runs_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_path = os.path.join(self.bench_runs_dir, "%s.json" % stamp)
        with open(run_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        report["run_file"] = run_path
        return report

    def _run_cases(self, cases: List[Dict[str, Any]], client: Any,
                   agent: Optional[str]) -> Dict[str, Any]:
        results = [self.run_case(case, client) for case in cases]
        passed = sum(1 for r in results if r["pass"])
        skipped = sum(1 for r in results if r.get("skipped"))
        failed = sum(1 for r in results if r["pass"] is False)
        total = len(results)
        rated = total - skipped  # skipped 不计入通过率分母
        return {"agent": agent, "ts": round(time.time(), 3),
                "total": total, "passed": passed, "failed": failed,
                "skipped": skipped,
                "pass_rate": (passed / float(rated)) if rated else 0.0,
                "cases": results}

    @staticmethod
    def _sandbox_client() -> Any:
        """独立沙箱 server (与 tests start_test_server 同一约定, 不污染工作区)。"""
        from hacoo.access.sdk import HacooClient
        from hacoo.config import Config
        from hacoo.gateway.control import PreExecutionControl
        from hacoo.gateway.gateway import Gateway
        from hacoo.server import HacooServer

        sandbox = tempfile.mkdtemp(prefix="hacoo-bench-")
        cfg = Config()
        cfg.agents_file = os.path.join(sandbox, "hacoo.agents.json")  # 不存在 -> 全启用
        cfg.knowledge_dir = os.path.join(sandbox, "knowledge")
        cfg.decisions_log = os.path.join(sandbox, "decisions.jsonl")
        cfg.runs_dir = os.path.join(sandbox, "runs")
        cfg.observations_dir = os.path.join(sandbox, "observations")
        cfg.insights_log = os.path.join(sandbox, "insights.jsonl")
        cfg.kb_misses_log = os.path.join(sandbox, "kb-misses.jsonl")
        cfg.upgrades_log = os.path.join(sandbox, "upgrade-proposals.jsonl")
        cfg.audit_log = None
        gateway = Gateway(config=cfg, control=PreExecutionControl(), audit=None)
        server = HacooServer("127.0.0.1", 0, gateway=gateway)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                client = HacooClient(server.address, token="dev-token")
                client.ping()
                return server, client
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("bench sandbox server failed to start")

    # ------------------------------------------------------------ 基准与对比

    def save_baseline(self, agent: str, client: Any = None) -> str:
        """跑该 agent 全部 cases 并存 baselines/<agent>@<version>.json (git 追踪)。"""
        from hacoo import agents as agent_registry

        report = self.run_all(agent=agent, client=client)
        version = agent_registry.version_of(agent)
        os.makedirs(self.baselines_dir, exist_ok=True)
        path = os.path.join(self.baselines_dir, "%s@%s.json" % (agent, version))
        payload = {"agent": agent, "version": version, "ts": report["ts"],
                   "pass_rate": report["pass_rate"],
                   "cases": {r["id"]: {"pass": r["pass"]} for r in report["cases"]}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return path

    def compare(self, baseline_path: str, client: Any = None) -> Dict[str, Any]:
        """与基准对比: pass 率 delta + 逐 case 的 fail->pass / pass->fail 列表。"""
        with open(baseline_path, "r", encoding="utf-8") as fh:
            baseline = json.load(fh)
        agent = baseline.get("agent")
        report = self.run_all(agent=agent, client=client)
        old = {cid: bool(v.get("pass")) for cid, v in (baseline.get("cases") or {}).items()}
        new = {r["id"]: r["pass"] for r in report["cases"]}
        fail_to_pass = sorted(c for c in old if not old[c] and new.get(c))
        # skipped 的 case (pass=None) 不算回退, 只有显式 fail 才进 pass_to_fail
        pass_to_fail = sorted(c for c in old if old[c] and new.get(c) is False)
        return {"agent": agent,
                "baseline": os.path.basename(baseline_path),
                "baseline_version": baseline.get("version"),
                "baseline_pass_rate": baseline.get("pass_rate"),
                "current_pass_rate": report["pass_rate"],
                "pass_rate_delta": round(report["pass_rate"]
                                         - (baseline.get("pass_rate") or 0.0), 4),
                "fail_to_pass": fail_to_pass,
                "pass_to_fail": pass_to_fail,
                "new_cases": sorted(c for c in new if c not in old),
                "removed_cases": sorted(c for c in old if c not in new),
                "report": report}
