"""HACOO 端到端测试: 按 SPEC 端到端请求路径验证五层行为 + 多用户并发。

    Agent/Client(SDK) -> Access -> Comm -> Gateway -> Adapter -> Runtime -> 回传

运行: python -m tests.test_e2e   (或 pytest tests/test_e2e.py)
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from typing import Any, List

from hacoo.access.sdk import HacooClient, HacooClientError
from hacoo.audit import AuditLog
from hacoo.config import Config
from hacoo.gateway.control import PreExecutionControl, load_auth
from hacoo.gateway.gateway import Gateway
from hacoo.server import HacooServer


_TEST_TMP = tempfile.TemporaryDirectory()


def start_test_server(config: Config = None, auth_file: str = None,
                      audit_path: str = None):
    """在随机端口启动一个测试服务, 返回 (server, thread)。

    默认把 agents/knowledge/decisions/runs 全部重定向到临时目录, 不污染工作区。
    """
    cfg = config or Config()
    sandbox = os.path.join(_TEST_TMP.name, "srv-%d" % int(time.time() * 1000))
    os.makedirs(sandbox, exist_ok=True)
    cfg.agents_file = cfg.agents_file if os.path.isabs(cfg.agents_file) else \
        os.path.join(sandbox, "hacoo.agents.json")
    cfg.knowledge_dir = os.path.join(sandbox, "knowledge")
    cfg.decisions_log = os.path.join(sandbox, "decisions.jsonl")
    cfg.runs_dir = os.path.join(sandbox, "runs")
    # 成长闭环新增配置项同样沙箱化 (观测/insight/KB miss)
    cfg.observations_dir = os.path.join(sandbox, "observations")
    cfg.insights_log = os.path.join(sandbox, "insights.jsonl")
    cfg.kb_misses_log = os.path.join(sandbox, "kb-misses.jsonl")
    # 升级提案 (SPEC-evolution Part A) 同样沙箱化
    cfg.upgrades_log = os.path.join(sandbox, "upgrade-proposals.jsonl")
    cfg.audit_log = audit_path  # None 关闭审计
    gateway = Gateway(config=cfg,
                      control=PreExecutionControl(load_auth(auth_file)),
                      audit=AuditLog(audit_path))
    server = HacooServer("127.0.0.1", 0, gateway=gateway)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            HacooClient(server.address).close()
            break
        except OSError:
            time.sleep(0.1)
    return server, thread


class HacooE2ETest(unittest.TestCase):
    """基础协议与单用户流程 (默认 dev-token/admin)。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.thread = start_test_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")

    def tearDown(self) -> None:
        self.client.close()

    # ---------------------------------------------------------- G5 能力发现

    def test_ping(self) -> None:
        data = self.client.ping()["data"]
        self.assertTrue(data["pong"])

    def test_list_methods_progressive(self) -> None:
        methods = self.client.list_methods()
        names = [m["name"] for m in methods]
        for expected in ("api_ping", "api_whoami", "api_spawn_instance",
                         "api_load_design", "api_report_timing", "api_task_cancel"):
            self.assertIn(expected, names)
        detail = self.client.method_detail("api_report_timing")  # G6 按需详情
        self.assertEqual(detail["capability"], "read")
        self.assertIn("max_paths", detail["params"])

    # ---------------------------------------------------------- 完整工作流

    def test_full_flow_state_persistence(self) -> None:
        with self.client.session("mock_eda") as eda:
            eda.call("api_load_design", path="chip.v", top="chip")
            eda.call("api_read_liberty", path="nangate45.lib")
            r1 = eda.call("api_report_timing", max_paths=5)["data"]
            eda.call("api_run_synthesis", effort="low")
            r2 = eda.call("api_report_timing", max_paths=5)["data"]
            # R6: 同一实例跨调用保持状态
            self.assertEqual(r2["synthesis_runs"], 1)
            self.assertEqual(r1["top"], "chip")
            state = eda.call("api_get_design_state")["data"]
            self.assertEqual(state["libs"], ["nangate45.lib"])

    def test_instance_isolation(self) -> None:
        iid_a = self.client.spawn("mock_eda")
        iid_b = self.client.spawn("mock_eda")
        try:
            self.assertNotEqual(iid_a, iid_b)
            self.client.call("api_load_design", instance_id=iid_a,
                             path="a.v", top="a")
            with self.assertRaises(HacooClientError) as ctx:
                # B 实例未加载设计 -> 后端业务错误, 证明状态隔离
                self.client.call("api_report_timing", instance_id=iid_b)
            self.assertEqual(ctx.exception.code, "BACKEND_ERROR")
        finally:
            self.client.release(iid_a)
            self.client.release(iid_b)

    # ---------------------------------------------------------- G1-G4 控制

    def test_unregistered_method_rejected(self) -> None:
        # 非 api_* 注册名 -> G1 协议验证拒绝 (任意 shell 过程的执行边界)
        with self.assertRaises(HacooClientError) as ctx:
            self.client.call("exec_shell", arguments={"cmd": "rm -rf /"})
        self.assertEqual(ctx.exception.code, "PROTOCOL_ERROR")

    def test_unregistered_api_method_rejected(self) -> None:
        # api_ 前缀但未注册 -> G2 注册表查找拒绝
        with self.assertRaises(HacooClientError) as ctx:
            self.client.call("api_exec_shell", arguments={"cmd": "rm -rf /"})
        self.assertEqual(ctx.exception.code, "UNREGISTERED_METHOD")

    def test_argument_checking(self) -> None:
        with self.assertRaises(HacooClientError) as ctx:
            self.client.call("api_spawn_instance")  # 缺必填 tool
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

    def test_authorization(self) -> None:
        bad = HacooClient(self.server.address, token="wrong-token")
        try:
            with self.assertRaises(HacooClientError) as ctx:
                bad.ping()
            self.assertEqual(ctx.exception.code, "UNAUTHORIZED")
        finally:
            bad.close()

    # ---------------------------------------------------------- R8 / R11

    def test_async_long_task(self) -> None:
        with self.client.session("mock_eda") as eda:
            eda.call("api_load_design", path="chip.v", top="chip")
            submitted = eda.call("api_run_synthesis", effort="high", async_=True)["data"]
            self.assertEqual(submitted["status"], "running")
            task = self.client.task_status(submitted["task_id"], wait=True)
            self.assertEqual(task["status"], "done")
            self.assertIn("area", task["result"]["data"])

    def test_task_cancel_after_done(self) -> None:
        with self.client.session("mock_eda") as eda:
            eda.call("api_load_design", path="chip.v", top="chip")
            task_id = eda.call("api_run_synthesis", effort="low", async_=True)["data"]["task_id"]
            self.client.task_status(task_id, wait=True)
            result = self.client.task_cancel(task_id)["data"]
            self.assertFalse(result["cancelled"])  # 已完成任务不可取消

    def test_release_then_call_fails(self) -> None:
        iid = self.client.spawn("mock_eda")
        self.client.release(iid)
        with self.assertRaises(HacooClientError) as ctx:
            self.client.call("api_load_design", instance_id=iid, path="x", top="x")
        self.assertEqual(ctx.exception.code, "NO_INSTANCE")

    # ---------------------------------------------------------- SDK 健壮性

    def test_auto_reconnect(self) -> None:
        self.client._conn.close()  # 模拟长连接断开
        data = self.client.ping()["data"]  # 应自动重连并重试
        self.assertTrue(data["pong"])


class HacooMultiUserTest(unittest.TestCase):
    """多用户: 身份 / 所有权隔离 / 配额 / 并发串行化 / 审计。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        auth_path = os.path.join(cls._tmp.name, "auth.json")
        with open(auth_path, "w", encoding="utf-8") as fh:
            json.dump({
                "tokens": {"tok-alice": {"user": "alice", "role": "engineer"},
                           "tok-bob": {"user": "bob", "role": "engineer"},
                           "tok-root": {"user": "root", "role": "admin"}},
            }, fh)
        cls.audit_path = os.path.join(cls._tmp.name, "audit.jsonl")
        cfg = Config(max_instances_per_user=2)
        cls.server, cls.thread = start_test_server(cfg, auth_path, cls.audit_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    def client(self, token: str) -> HacooClient:
        return HacooClient(self.server.address, token=token)

    def test_whoami(self) -> None:
        with self.client("tok-alice") as c:
            who = c.whoami()
            self.assertEqual(who["user"], "alice")
            self.assertEqual(who["role"], "engineer")
            self.assertEqual(who["instances_quota"], 2)

    def test_instance_ownership_isolation(self) -> None:
        alice = self.client("tok-alice")
        bob = self.client("tok-bob")
        try:
            iid = alice.spawn("mock_eda")
            # bob 不能调用 alice 的实例
            with self.assertRaises(HacooClientError) as ctx:
                bob.call("api_load_design", instance_id=iid, path="x", top="x")
            self.assertEqual(ctx.exception.code, "UNAUTHORIZED")
            # bob 不能释放 alice 的实例
            with self.assertRaises(HacooClientError) as ctx:
                bob.release(iid)
            self.assertEqual(ctx.exception.code, "UNAUTHORIZED")
            # bob 看不到 alice 的实例; admin 能看到
            self.assertEqual(bob.list_instances(), [])
            root = self.client("tok-root")
            try:
                all_inst = root.list_instances()
                self.assertTrue(any(i["instance_id"] == iid for i in all_inst))
            finally:
                root.close()
            alice.release(iid)
        finally:
            alice.close()
            bob.close()

    def test_instance_quota(self) -> None:
        alice = self.client("tok-alice")
        try:
            iid1 = alice.spawn("mock_eda")
            iid2 = alice.spawn("mock_eda")
            with self.assertRaises(HacooClientError) as ctx:
                alice.spawn("mock_eda")  # 配额 2, 第三个被拒
            self.assertEqual(ctx.exception.code, "QUOTA_EXCEEDED")
            alice.release(iid1)
            iid3 = alice.spawn("mock_eda")  # 释放后可再开
            alice.release(iid2)
            alice.release(iid3)
        finally:
            alice.close()

    def test_concurrent_calls_serialized(self) -> None:
        """同一实例并发调用被串行化: 两次并发综合后 runs_total 必须恰为 2。"""
        alice = self.client("tok-alice")
        try:
            iid = alice.spawn("mock_eda")
            alice.call("api_load_design", instance_id=iid, path="chip.v", top="chip")
            barrier = threading.Barrier(2)
            errors = []

            def run_synthesis():
                try:
                    barrier.wait(timeout=5)
                    alice.call("api_run_synthesis", instance_id=iid, effort="low")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=run_synthesis) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(errors, [])
            state = alice.call("api_get_design_state", instance_id=iid)["data"]
            self.assertEqual(state["synthesis_runs"], 2)
            alice.release(iid)
        finally:
            alice.close()

    def test_task_ownership(self) -> None:
        alice = self.client("tok-alice")
        bob = self.client("tok-bob")
        try:
            iid = alice.spawn("mock_eda")
            alice.call("api_load_design", instance_id=iid, path="chip.v", top="chip")
            task_id = alice.call("api_run_synthesis", instance_id=iid,
                                 effort="low", async_=True)["data"]["task_id"]
            with self.assertRaises(HacooClientError) as ctx:
                bob.task_status(task_id)  # bob 不能轮询 alice 的任务
            self.assertEqual(ctx.exception.code, "UNAUTHORIZED")
            alice.task_status(task_id, wait=True)
            alice.release(iid)
        finally:
            alice.close()
            bob.close()

    def test_audit_log_written(self) -> None:
        with self.client("tok-alice") as c:
            c.ping()
        self.server.gateway.audit._fh.flush()
        with open(self.audit_path, "r", encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(entries, "audit log is empty")
        entry = entries[-1]
        self.assertEqual(entry["user"], "alice")
        self.assertEqual(entry["method"], "api_ping")
        self.assertEqual(entry["status"], "ok")
        self.assertTrue(entry["request_id"])


class HacooAgentTest(unittest.TestCase):
    """多 Agent 协作: 启停强制 / 白名单 / 知识库 / 决策问责 / flow 编排 / ISF。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        # 本次"实验"只启用 sta / synthesis / flow 三个 Agent
        cls.agents_file = os.path.join(cls._tmp.name, "hacoo.agents.json")
        with open(cls.agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta", "synthesis", "flow"]}, fh)
        cls.audit_path = os.path.join(cls._tmp.name, "audit.jsonl")
        cfg = Config(agents_file=cls.agents_file)
        cls.server, cls.thread = start_test_server(cfg, audit_path=cls.audit_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")

    def tearDown(self) -> None:
        self.client.close()

    # ---------------------------------------------------------- 启停与白名单

    def test_list_agents(self) -> None:
        agents = {a["name"]: a for a in self.client.list_agents()}
        self.assertEqual(len(agents), 9)
        self.assertTrue(agents["sta"]["enabled"])
        self.assertFalse(agents["apr"]["enabled"])
        self.assertEqual(agents["flow"]["stage"], "control")

    def test_disabled_agent_rejected(self) -> None:
        iid = self.client.spawn("mock_eda")
        try:
            with self.assertRaises(HacooClientError) as ctx:
                self.client.call("api_load_design", instance_id=iid,
                                 arguments={"path": "x", "top": "x"}, agent="apr")
            self.assertEqual(ctx.exception.code, "AGENT_DISABLED")
        finally:
            self.client.release(iid)

    def test_agent_tool_whitelist(self) -> None:
        iid = self.client.spawn("isf")
        try:
            with self.assertRaises(HacooClientError) as ctx:
                # sta 的白名单只有 mock_eda, 不能用 isf 工具
                self.client.call("api_isf_flows", instance_id=iid,
                                 arguments={}, agent="sta")
            self.assertEqual(ctx.exception.code, "AGENT_FORBIDDEN")
        finally:
            self.client.release(iid)

    def test_unknown_agent_rejected(self) -> None:
        with self.assertRaises(HacooClientError) as ctx:
            self.client.call("api_ping", agent="nobody")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

    # ---------------------------------------------------------- 知识库自成长

    def test_knowledge_lifecycle(self) -> None:
        entry = self.client.knowledge_add(
            agent="sta", symptom="WNS 在 high effort 综合后反而恶化",
            root_cause="过度优化导致 hold 修复插入大量 buffer",
            resolution="high effort 前先冻结 hold-critical 路径",
            tags=["sta", "hold", "eco"], run_id=None)
        self.assertTrue(entry["id"].startswith("kb-"))
        hits = self.client.knowledge_query("sta", keyword="WNS")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["reuse_count"], 1)  # 查询命中即复用计数
        hits2 = self.client.knowledge_query("sta", keyword="WNS")
        self.assertEqual(hits2[0]["reuse_count"], 2)
        # tag 过滤
        self.assertEqual(self.client.knowledge_query("sta", tags=["congestion"]), [])
        self.assertEqual(len(self.client.knowledge_query("sta", tags=["hold"])), 1)

    def test_disabled_agent_cannot_write_kb(self) -> None:
        with self.assertRaises(HacooClientError) as ctx:
            self.client.knowledge_add(agent="apr", symptom="s", resolution="r")
        self.assertEqual(ctx.exception.code, "AGENT_DISABLED")

    # ---------------------------------------------------------- 决策问责

    def test_decision_lifecycle(self) -> None:
        dec_id = self.client.decision_record(
            agent="sta", stage="sta_report", action="api_report_timing",
            rationale="综合后必须确认 WNS 是否满足 -0.1ns 阈值",
            run_id="run-test", params={"max_paths": 20})
        pending = self.client.decision_query(agent="sta", status="pending")
        self.assertTrue(any(d["decision_id"] == dec_id for d in pending))
        self.client.decision_outcome(dec_id, "ok", metrics={"wns": -0.05})
        done = self.client.decision_query(agent="sta", status="ok")
        entry = [d for d in done if d["decision_id"] == dec_id][0]
        self.assertEqual(entry["metrics"]["wns"], -0.05)
        self.assertEqual(entry["rationale"], "综合后必须确认 WNS 是否满足 -0.1ns 阈值")

    # ---------------------------------------------------------- Flow 编排

    def test_flow_run_success_and_trace(self) -> None:
        run = self.client.flow_start("sta_only", {"design_path": "chip.v", "top": "chip"})
        self.assertEqual(run["status"], "success")
        self.assertEqual([s["status"] for s in run["stages"]], ["ok", "ok"])
        # 问责视图: 决策时间线 + 审计
        trace = self.client.flow_trace(run["run_id"])
        self.assertEqual(len(trace["decisions"]), 2)
        self.assertTrue(all(d["status"] == "ok" for d in trace["decisions"]))
        self.assertEqual({d["agent"] for d in trace["decisions"]}, {"sta"})
        self.assertTrue(len(trace["audit"]) >= 4)  # spawn + 2 stages + release

    def test_flow_fails_on_disabled_agent(self) -> None:
        # block_signoff 含 synthesis+sta; 禁用 synthesis 后必须失败且 stage 未执行
        from hacoo import agents as registry
        registry.set_enabled("synthesis", False, self.agents_file)
        try:
            run = self.client.flow_start("block_signoff")
            self.assertEqual(run["status"], "failed")
            self.assertIn("AGENT_DISABLED", run["error"])
            self.assertEqual(run["stages"][0]["status"], "failed")
            self.assertEqual(len(run["stages"]), 1)  # 后续 stage 未执行
            decisions = self.client.decision_query(run_id=run["run_id"])
            self.assertEqual(decisions[0]["status"], "failed")
            self.assertIn("AGENT_DISABLED", decisions[0]["note"])
        finally:
            registry.set_enabled("synthesis", True, self.agents_file)

    # ---------------------------------------------------------- ISF adapter

    def test_isf_adapter(self) -> None:
        os.environ["HACOO_ISF_MOCK"] = "1"  # 无 ISF 环境, 强制 mock 保证确定性
        iid = self.client.spawn("isf")
        try:
            setup = self.client.call("api_isf_setup", instance_id=iid,
                                     arguments={"project": "N3CTV1"},
                                     agent="flow")["data"]
            self.assertEqual(setup["project"], "N3CTV1")
            self.assertEqual(setup["mode"], "mock")
            flow = self.client.call(
                "api_isf_fw_start", instance_id=iid, agent="flow",
                arguments={"flow": "APR+TC+PV", "block": "BLOCK1",
                           "flow_path": "/FLOW/PATH/NAME",
                           "usertcl": "user.tcl",
                           "data_version": "20260920R",
                           "design_version": "20260711"})["data"]
            # 命令拼接与真实 fw start 用法一致
            self.assertIn("fw start -flow APR+TC+PV -block BLOCK1 /FLOW/PATH/NAME",
                          flow["command"])
            self.assertIn("-usertcl user.tcl", flow["command"])
            self.assertIn("-data_version 20260920R", flow["command"])
            self.assertEqual(flow["project"], "N3CTV1")
            time.sleep(2.2)
            status = self.client.call("api_isf_flow_status", instance_id=iid,
                                      arguments={"flow_id": flow["flow_id"]},
                                      agent="flow")["data"]
            self.assertEqual(status["status"], "success")
            out = self.client.call("api_isf_run", instance_id=iid,
                                   arguments={"command": "fw list"},
                                   agent="flow")
            self.assertTrue(out["text"])
            flows = self.client.call("api_isf_flows", instance_id=iid,
                                     arguments={}, agent="flow")["data"]
            self.assertEqual(flows["total"], 1)
            # 未 setup 的新实例不允许 fw start
            iid2 = self.client.spawn("isf")
            try:
                with self.assertRaises(HacooClientError):
                    self.client.call("api_isf_fw_start", instance_id=iid2,
                                     arguments={"flow": "STA", "block": "B",
                                                "flow_path": "/tmp/f"},
                                     agent="flow")
            finally:
                self.client.release(iid2)
        finally:
            self.client.release(iid)
            os.environ.pop("HACOO_ISF_MOCK", None)


class HacooEcoLoopTest(unittest.TestCase):
    """EC-1 (SPEC-evolution): eco_loop flow 模板与引擎阶段内循环。

    验收: (1) mock 下 eco_loop 收敛 (wns 逐轮改善至达标), trace 含全部
    迭代决策 (params 带 _iteration); (2) 超 max_iter 未收敛 -> 状态
    escalated 且原因明确; (3) loop body 内 Agent 被禁 -> run failed
    AGENT_DISABLED 且不执行后续; (4) 线性模板 (sta_only/block_signoff)
    行为不变 (既有 HacooAgentTest 覆盖)。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        # eco_loop 需要 sta (诊断/验证) + apr (执行 ECO) + flow
        cls.agents_file = os.path.join(cls._tmp.name, "hacoo.agents.json")
        with open(cls.agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta", "apr", "flow"]}, fh)
        cfg = Config(agents_file=cls.agents_file)
        cls.server, cls.thread = start_test_server(cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")

    def tearDown(self) -> None:
        self.client.close()

    def test_eco_loop_converges_with_full_trace(self) -> None:
        run = self.client.flow_start("eco_loop",
                                     {"design_path": "chip.v", "top": "chip"})
        self.assertEqual(run["status"], "success", run.get("error"))
        self.assertEqual([s["name"] for s in run["stages"]],
                         ["load_design", "eco_iterate"])
        loop_entry = run["stages"][1]
        self.assertEqual(loop_entry["status"], "ok")
        iterations = loop_entry["loop"]["iterations"]
        # 最坏 base=-1.0 时 3 轮内收敛 (+0.4/轮); max_iter=5 为硬上界
        self.assertGreaterEqual(len(iterations), 1)
        self.assertLessEqual(len(iterations), 5)
        # 每轮子阶段齐整: diagnose(sta) -> apply_eco(apr) -> verify(sta)
        for it in iterations:
            self.assertEqual([s["name"] for s in it["stages"]],
                             ["diagnose", "apply_eco", "verify"])
            self.assertTrue(all(s["status"] == "ok" for s in it["stages"]))
            self.assertEqual([s["agent"] for s in it["stages"]],
                             ["sta", "apr", "sta"])
        # 收敛轨迹单调改善 (judge 记录每轮 verify 的 wns)
        wns_seq = [it["judge"]["wns"] for it in iterations]
        self.assertTrue(all(b >= a for a, b in zip(wns_seq, wns_seq[1:])),
                        "wns 未单调改善: %s" % wns_seq)
        self.assertGreaterEqual(wns_seq[-1], 0.0)  # target_wns 默认 0.0
        # 决策链可 trace: 循环级 eco_loop 决策 + 每轮 3 个子阶段决策,
        # 子阶段决策 params 带 _iteration, 全部 outcome ok
        trace = self.client.flow_trace(run["run_id"])
        decisions = trace["decisions"]
        loop_decisions = [d for d in decisions if d["action"] == "eco_loop"]
        self.assertEqual(len(loop_decisions), 1)
        self.assertEqual(loop_decisions[0]["status"], "ok")
        self.assertIn("converged at iteration", loop_decisions[0]["note"])
        body_decisions = [d for d in decisions if "_iteration" in (d.get("params") or {})]
        self.assertEqual(len(body_decisions), 3 * len(iterations))
        self.assertTrue(all(d["status"] == "ok" for d in body_decisions))
        self.assertEqual({d["agent"] for d in body_decisions}, {"sta", "apr"})
        self.assertEqual({d["params"]["_iteration"] for d in body_decisions},
                         set(range(1, len(iterations) + 1)))

    def test_eco_loop_escalates_when_max_iter_exhausted(self) -> None:
        run = self.client.flow_start(
            "eco_loop", {"target_wns": 99.0, "max_iter": 2})  # 不可达阈值
        self.assertEqual(run["status"], "escalated")
        self.assertIn("max_iter 2 exhausted", run["error"])
        self.assertIn("eco_iterate", run["error"])
        loop_entry = run["stages"][1]
        self.assertEqual(loop_entry["status"], "escalated")
        self.assertEqual(len(loop_entry["loop"]["iterations"]), 2)
        # 升级原因进入问责日志
        decisions = self.client.decision_query(run_id=run["run_id"])
        loop_decisions = [d for d in decisions if d["action"] == "eco_loop"]
        self.assertEqual(loop_decisions[0]["status"], "escalated")
        self.assertIn("max_iter", loop_decisions[0]["note"])

    def test_eco_loop_fails_on_disabled_body_agent(self) -> None:
        from hacoo import agents as registry
        registry.set_enabled("apr", False, self.agents_file)
        try:
            run = self.client.flow_start("eco_loop")
            self.assertEqual(run["status"], "failed")
            self.assertIn("AGENT_DISABLED", run["error"])
            loop_entry = run["stages"][1]
            self.assertEqual(loop_entry["status"], "failed")
            # 第一轮: diagnose 已执行, apply_eco 被禁即中止, verify 未执行
            first = loop_entry["loop"]["iterations"][0]
            self.assertEqual([s["name"] for s in first["stages"]],
                             ["diagnose", "apply_eco"])
            self.assertEqual(first["stages"][0]["status"], "ok")
            self.assertEqual(first["stages"][1]["status"], "failed")
            decisions = self.client.decision_query(run_id=run["run_id"])
            disabled = [d for d in decisions
                        if "AGENT_DISABLED" in (d.get("note") or "")]
            self.assertTrue(disabled)
        finally:
            registry.set_enabled("apr", True, self.agents_file)


class HacooGrowthLoopTest(unittest.TestCase):
    """成长闭环 (SPEC-agent-growth-loop §1-§4):
    观测钩子 / 人类反馈闭环 / 复核队列 / KB miss / 检测器 / insight 去重与闭环 / 治理豁免。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.thread = start_test_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")
        self.gateway = self.server.gateway

    def tearDown(self) -> None:
        self.client.close()

    # ---------------------------------------------------------- 1. 观测钩子

    def test_observation_hook(self) -> None:
        iid = self.client.spawn("mock_eda")
        try:
            before = len(self.gateway.observations.query(agent="sta"))
            self.client.call("api_load_design", instance_id=iid,
                             arguments={"path": "chip.v", "top": "chip"},
                             agent="sta", run_id="run-obs")
            obs = self.gateway.observations.query(agent="sta")
            self.assertEqual(len(obs), before + 1)  # 恰增 1 条
            entry = obs[-1]
            self.assertTrue(entry["obs_id"].startswith("obs-"))
            self.assertTrue(entry["ok"])
            self.assertTrue(entry["metrics"])   # cells 等扁平数值被提取
            self.assertTrue(entry["excerpt"])
            self.assertEqual(entry["run_id"], "run-obs")

            # 失败调用: ok=false + error_signatures 非空
            iid2 = self.client.spawn("mock_eda")
            try:
                with self.assertRaises(HacooClientError) as ctx:
                    self.client.call("api_report_timing", instance_id=iid2, agent="sta")
                self.assertEqual(ctx.exception.code, "BACKEND_ERROR")
                failed = [o for o in self.gateway.observations.query(agent="sta")
                          if not o["ok"]]
                self.assertTrue(failed)
                self.assertTrue(failed[-1]["error_signatures"])
            finally:
                self.client.release(iid2)

            # 通用方法 (kind="generic") 不产生观测
            before = len(self.gateway.observations.query(agent="sta"))
            self.client.call("api_ping", agent="sta")
            self.assertEqual(len(self.gateway.observations.query(agent="sta")), before)

            # api_observations_query 排障入口可读
            data = self.client.call("api_observations_query",
                                    arguments={"agent": "sta", "limit": 10})["data"]
            self.assertTrue(data)
        finally:
            self.client.release(iid)

    # ---------------------------------------------------------- 2. 反馈闭环

    def test_feedback_loop(self) -> None:
        dec_id = self.client.decision_record(
            agent="sta", stage="sta_report", action="api_report_timing",
            rationale="验证反馈绑定决策")
        entry = self.client.feedback(agent="sta", content="这种情况应先查 clock skew",
                                     decision_id=dec_id, tags=["clock"])
        self.assertEqual(entry["source"], "human_correction")
        self.assertEqual(entry["weight"], 5)
        self.assertEqual(entry["decision_id"], dec_id)

        # decision_id 属于别的 agent -> ARGUMENT_ERROR
        other = self.client.decision_record(agent="synthesis", stage="s",
                                            action="a", rationale="r")
        with self.assertRaises(HacooClientError) as ctx:
            self.client.feedback(agent="sta", content="x", decision_id=other)
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")
        # decision_id 不存在同样报错
        with self.assertRaises(HacooClientError) as ctx:
            self.client.feedback(agent="sta", content="x", decision_id="dec-nonexistent")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

        # 排序: 人类纠正 (weight=5) 排在同关键词 agent 条目 (weight=1) 之前
        self.client.knowledge_add(agent="sta", symptom="clock skew 导致的 setup 违例",
                                  resolution="调整 CTS 目标", tags=["clock"])
        hits = self.client.knowledge_query("sta", keyword="clock")
        self.assertEqual(hits[0]["source"], "human_correction")

    # ---------------------------------------------------------- 3. 复核队列

    def test_review_queue(self) -> None:
        entry = self.client.knowledge_add(
            agent="apr", symptom="congestion 在 utilization>0.8 后失控",
            resolution="提前降 utilization 或加 halo", tags=["congestion"])
        dec_id = self.client.decision_record(agent="apr", stage="place",
                                             action="set_util", rationale="待验证")
        queue = self.client.review_queue(agent="apr")
        self.assertTrue(any(d["decision_id"] == dec_id for d in queue["decisions"]))
        self.assertTrue(any(e["id"] == entry["id"] for e in queue["knowledge"]))

        # approve 后复核队列减少
        self.client.knowledge_review(entry["id"], "approve", note="已验证")
        queue2 = self.client.review_queue(agent="apr")
        self.assertFalse(any(e["id"] == entry["id"] for e in queue2["knowledge"]))

        # reject 后 weight=0 且不再被 query 推举
        entry2 = self.client.knowledge_add(agent="apr", symptom="高频下 antenna 违例",
                                           resolution="插入 diode", tags=["antenna"])
        self.client.knowledge_review(entry2["id"], "reject")
        self.assertFalse(any(e["id"] == entry2["id"]
                             for e in self.client.knowledge_query("apr", keyword="antenna")))
        stored = [e for e in self.gateway.knowledge.list("apr", limit=100)
                  if e["id"] == entry2["id"]][0]
        self.assertEqual(stored["weight"], 0)
        self.assertEqual(stored["review_status"], "rejected")
        # 不存在的 entry -> ARGUMENT_ERROR
        with self.assertRaises(HacooClientError) as ctx:
            self.client.knowledge_review("kb-nonexistent", "approve")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

    # ---------------------------------------------------------- 4. KB miss 记录

    def test_kb_miss_log(self) -> None:
        hits = self.client.knowledge_query("pv", keyword="绝不存在的主题xyz123")
        self.assertEqual(hits, [])
        with open(self.gateway.config.kb_misses_log, "r", encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        self.assertTrue(any(m["agent"] == "pv" and m["keyword"] == "绝不存在的主题xyz123"
                            for m in lines))

    # ---------------------------------------------------------- 5. 检测器 (纯函数)

    def test_detectors_pure(self) -> None:
        from hacoo.insights import run_detectors
        now = time.time()

        def obs(run_id, metrics, ts, ok=True, sigs=()):
            return {"obs_id": "obs-%s" % run_id, "ts": ts, "run_id": run_id,
                    "ok": ok, "metrics": metrics,
                    "error_signatures": list(sigs), "excerpt": ""}

        # 趋势: wns 越小越差, 连续 3 点单调恶化
        cands = run_detectors("sta", [obs("r1", {"wns": -0.02}, now - 30),
                                      obs("r2", {"wns": -0.05}, now - 20),
                                      obs("r3", {"wns": -0.11}, now - 10)],
                              [], [], [], now)
        trends = [c for c in cands if c["kind"] == "trend"]
        self.assertEqual(len(trends), 1)
        self.assertIn("wns", trends[0]["title"])
        self.assertEqual(len(trends[0]["evidence"]), 3)
        # 计数类指标: 越大越差
        cands = run_detectors("sta", [obs("r1", {"violator_lines": 3}, now - 30),
                                      obs("r2", {"violator_lines": 8}, now - 20),
                                      obs("r3", {"violator_lines": 15}, now - 10)],
                              [], [], [], now)
        self.assertTrue(any(c["kind"] == "trend" for c in cands))
        # 改善方向不报警
        cands = run_detectors("sta", [obs("r1", {"wns": -0.11}, now - 30),
                                      obs("r2", {"wns": -0.05}, now - 20),
                                      obs("r3", {"wns": -0.02}, now - 10)],
                              [], [], [], now)
        self.assertFalse(any(c["kind"] == "trend" for c in cands))

        # 失败签名聚类: 窗口内同一签名 >=3 次
        sig = "ERROR: (PTS-012) cannot open liberty"
        cands = run_detectors("sta",
                              [obs("r%d" % i, {}, now - 10 * i, ok=False, sigs=[sig])
                               for i in range(3)],
                              [], [], [], now)
        self.assertTrue(any(c["kind"] == "anomaly" for c in cands))

        # 知识盲区: 同 keyword 未命中 >=2 次
        misses = [{"ts": now - 100, "agent": "sta", "keyword": "clock skew", "tags": []},
                  {"ts": now - 50, "agent": "sta", "keyword": "clock skew", "tags": []}]
        cands = run_detectors("sta", [], [], misses, [], now)
        self.assertEqual(len([c for c in cands if c["kind"] == "blind_spot"]), 1)

        # 自我问题 (a): 最近 5 条决策 2 failed (40% >= 30%)
        decs = [{"decision_id": "dec-%d" % i, "ts": now - 10 * i,
                 "status": "failed" if i < 2 else "ok", "stage": "s", "action": "a"}
                for i in range(5)]
        cands = run_detectors("sta", [], decs, [], [], now)
        self.assertTrue(any(c["kind"] == "self_issue" for c in cands))
        # 自我问题 (b): 窗口内 2 条 human_correction
        corr = [{"id": "kb-%d" % i, "ts": now - 10, "source": "human_correction",
                 "symptom": "correction", "tags": ["eco"]} for i in range(2)]
        cands = run_detectors("sta", [], [], [], corr, now)
        self.assertTrue(any(c["kind"] == "self_issue" for c in cands))
        # 签名结构: <agent>:<kind>:<12hex>
        for c in cands:
            parts = c["signature"].split(":")
            self.assertEqual(parts[0], "sta")
            self.assertEqual(len(parts[2]), 12)

    # ---------------------------------------------------------- 6. insight 去重与闭环

    def test_insight_dedupe_and_close_loop(self) -> None:
        # 造知识盲区: 同 keyword 两次零命中
        for _ in range(2):
            self.client.knowledge_query("thermal", keyword="热点定位topic-xyz")
        r1 = self.client.insights_scan(agent="thermal")
        created = [i for i in r1["created"] if i["kind"] == "blind_spot"]
        self.assertTrue(created)
        ins = created[0]
        self.assertEqual(ins["status"], "new")
        self.assertTrue(ins["evidence"])
        # 重复 scan 不产生同 signature insight
        r2 = self.client.insights_scan(agent="thermal")
        self.assertEqual(r2["created"], [])
        self.assertGreaterEqual(r2["skipped"], 1)
        # confirm + auto_kb 自动生成知识条目 (source=insight, 关联 insight_id)
        reviewed = self.client.insight_review(ins["insight_id"], "confirm")
        self.assertEqual(reviewed["status"], "confirmed")
        kb_id = reviewed["kb_entry_id"]
        kb = [e for e in self.gateway.knowledge.list("thermal", limit=100)
              if e["id"] == kb_id][0]
        self.assertEqual(kb["source"], "insight")
        self.assertEqual(kb["insight_id"], ins["insight_id"])
        # dismiss 后再次 scan 仍不产生 (避免告警疲劳)
        self.client.feedback(agent="sipi", content="纠正1: 应先查 PDN 阻抗", tags=["pdn"])
        self.client.feedback(agent="sipi", content="纠正2: decap 策略不对", tags=["pdn"])
        r3 = self.client.insights_scan(agent="sipi")
        self_issue = [i for i in r3["created"] if i["kind"] == "self_issue"]
        self.assertTrue(self_issue)
        self.client.insight_review(self_issue[0]["insight_id"], "dismiss", auto_kb=False)
        r4 = self.client.insights_scan(agent="sipi")
        self.assertFalse(any(i["kind"] == "self_issue" for i in r4["created"]))

    # ---------------------------------------------------------- 7. 治理豁免

    def test_governance_exemption(self) -> None:
        from hacoo import agents as registry
        agents_file = self.gateway.config.agents_file
        registry.set_enabled("dft", False, agents_file)
        try:
            # 人类面向方法不受 Agent 启停限制
            entry = self.client.feedback(agent="dft", content="scan chain 顺序应先确认")
            self.assertEqual(entry["source"], "human_correction")
            scan = self.client.insights_scan(agent="dft")
            self.assertIn("agents", scan)
            self.assertIsInstance(self.client.insights_list(agent="dft"), list)
            # 对照: 被禁 Agent 的自动沉淀仍被拒绝
            with self.assertRaises(HacooClientError) as ctx:
                self.client.knowledge_add(agent="dft", symptom="s", resolution="r")
            self.assertEqual(ctx.exception.code, "AGENT_DISABLED")
        finally:
            registry.set_enabled("dft", True, agents_file)


class HacooRecipeProfileTest(unittest.TestCase):
    """X-2 (SPEC-evolution): 知识库 design 画像检索 —— add() 带 profile
    (foundry/node/block_type), query(profile=...) 同画像条目加权优先;
    不传 profile 时排序与旧版完全一致 (向后兼容)。"""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from hacoo.knowledge import KnowledgeStore
        self.kb = KnowledgeStore(self._tmp.name)

    def _seed(self) -> None:
        self.generic = self.kb.add("sta", symptom="WNS 收敛慢",
                                   resolution="通用: 提 effort",
                                   weight=2)
        self.profiled = self.kb.add("sta", symptom="WNS 收敛慢",
                                    resolution="N3 CPU: 先查 clock gating 比率",
                                    weight=1,
                                    profile={"foundry": "tsmc", "node": "n3",
                                             "block_type": "cpu"})

    def test_profile_recorded_in_entry(self) -> None:
        self._seed()
        self.assertEqual(self.profiled["profile"],
                         {"foundry": "tsmc", "node": "n3", "block_type": "cpu"})
        self.assertIsNone(self.generic["profile"])

    def test_ranking_unchanged_without_profile(self) -> None:
        """向后兼容: 不传 profile 时 (weight, reuse_count, ts) 旧排序。"""
        self._seed()
        hits = self.kb.query("sta", keyword="WNS")
        self.assertEqual(hits[0]["id"], self.generic["id"])  # weight 2 > 1

    def test_profile_matched_entry_ranks_first(self) -> None:
        """同画像命中维度多的条目排在通用条目之前 (即使 weight 更低):
        profiled 1*(0+1)*(1+3)=4 > generic 2*(0+1)*1=2。"""
        self._seed()
        hits = self.kb.query("sta", keyword="WNS",
                             profile={"foundry": "tsmc", "node": "n3",
                                      "block_type": "cpu"})
        self.assertEqual(hits[0]["id"], self.profiled["id"])
        self.assertEqual(hits[1]["id"], self.generic["id"])

    def test_more_matched_dims_rank_higher(self) -> None:
        """匹配维度越多系数越高: 3 维命中 (x4) 排在 1 维命中 (x2) 前。"""
        self._seed()
        one_dim = self.kb.add("sta", symptom="WNS 收敛慢",
                              resolution="tsmc 通用节点经验", weight=1,
                              profile={"foundry": "tsmc", "node": "n5",
                                       "block_type": "gpu"})
        hits = self.kb.query("sta", keyword="WNS",
                             profile={"foundry": "tsmc", "node": "n3",
                                      "block_type": "cpu"})
        ids = [h["id"] for h in hits]
        self.assertLess(ids.index(self.profiled["id"]), ids.index(one_dim["id"]))

    def test_profile_passthrough_through_gateway(self) -> None:
        """经网关全链路: knowledge_add/profile -> knowledge_query(profile)
        同画像优先。"""
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        client.knowledge_add(agent="sta", symptom="hold 修复",
                             resolution="通用: 插 delay cell")
        client.knowledge_add(agent="sta", symptom="hold 修复",
                             resolution="N3: 优先复用 eco spare cell",
                             profile={"foundry": "tsmc", "node": "n3"})
        hits = client.knowledge_query("sta", keyword="hold",
                                      profile={"foundry": "tsmc", "node": "n3"})
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["resolution"], "N3: 优先复用 eco spare cell")


class HacooUpgradeTest(unittest.TestCase):
    """升级提案管理 (SPEC-evolution Part A / Part D.1.1 + D.1.4):
    生命周期与状态机 / acceptance 强制 / insight confirm 联动。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.thread = start_test_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")

    def tearDown(self) -> None:
        self.client.close()

    def test_upgrade_lifecycle(self) -> None:
        p = self.client.upgrade_propose(
            title="STA agent 需要支持 MCMM 场景管理", kind="agent_capability",
            target="sta", motivation="多 corner 场景爆炸, 报告解析靠人工",
            acceptance="bench case sta-mcmm-01 通过")
        self.assertTrue(p["proposal_id"].startswith("up-"))
        self.assertEqual(p["status"], "proposed")
        self.assertEqual(p["source"], "human")

        # 无 acceptance 的提案不允许 accept (A.2 强制检查)
        p2 = self.client.upgrade_propose(
            title="没有验收标准的提案", kind="platform_feature",
            target="platform", motivation="试试看")
        with self.assertRaises(HacooClientError) as ctx:
            self.client.upgrade_update(p2["proposal_id"], "accepted")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

        # 非法流转被拒绝: proposed 不能直接 done; done 是终态
        with self.assertRaises(HacooClientError) as ctx:
            self.client.upgrade_update(p["proposal_id"], "done")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")
        accepted = self.client.upgrade_update(p["proposal_id"], "accepted",
                                              note="立项评审通过")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["history"][-1]["from"], "proposed")
        wip = self.client.upgrade_update(p["proposal_id"], "in_progress")
        self.assertEqual(wip["status"], "in_progress")
        done = self.client.upgrade_update(p["proposal_id"], "done",
                                          version_after="sta@1.3.0")
        self.assertEqual(done["version_after"], "sta@1.3.0")
        self.assertEqual([h["to"] for h in done["history"]],
                         ["accepted", "in_progress", "done"])
        with self.assertRaises(HacooClientError) as ctx:
            self.client.upgrade_update(p["proposal_id"], "rejected")  # 终态不可再流转
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

        # 未知提案 / 非法 kind / 非法 to_status
        with self.assertRaises(HacooClientError) as ctx:
            self.client.upgrade_update("up-nonexistent", "accepted")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")
        with self.assertRaises(HacooClientError) as ctx:
            self.client.upgrade_propose(title="t", kind="nonsense", target="sta",
                                        motivation="m")
        self.assertEqual(ctx.exception.code, "ARGUMENT_ERROR")

        # 查询: 默认 ts 倒序 + status/target 过滤
        done_list = self.client.upgrade_list(status="done", target="sta")
        self.assertTrue(any(x["proposal_id"] == p["proposal_id"] for x in done_list))
        all_list = self.client.upgrade_list(target="sta")
        self.assertTrue(all(x["target"] == "sta" for x in all_list))

    def test_insight_confirm_create_upgrade(self) -> None:
        """A.5: insight confirm + create_upgrade 生成 source=insight 的提案。"""
        for _ in range(2):  # 造知识盲区 -> blind_spot insight
            self.client.knowledge_query("pv", keyword="升级联动罕见主题-uvw")
        scan = self.client.insights_scan(agent="pv")
        created = [i for i in scan["created"] if i["kind"] == "blind_spot"
                   and "升级联动" in i["title"]]
        self.assertTrue(created)
        ins = created[0]
        reviewed = self.client.insight_review(ins["insight_id"], "confirm",
                                              create_upgrade=True)
        self.assertEqual(reviewed["status"], "confirmed")
        self.assertTrue(reviewed["kb_entry_id"])            # auto_kb 仍生效
        proposal_id = reviewed["upgrade_proposal_id"]       # 联动生成提案
        proposals = [x for x in self.client.upgrade_list(target="pv")
                     if x["proposal_id"] == proposal_id]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["source"], "insight")
        self.assertEqual(proposals[0]["source_ref"], ins["insight_id"])
        self.assertEqual(proposals[0]["status"], "proposed")
        self.assertTrue(proposals[0]["acceptance"])  # 联动生成的提案自带验收标准

    def test_agent_version_recorded_in_knowledge(self) -> None:
        """A.3.3: 知识条目记录沉淀时的 Agent 生效版本。"""
        from hacoo import agents as registry
        agents_file = self.server.gateway.config.agents_file
        registry.set_version("sta", "9.9.9", agents_file)
        entry = self.client.knowledge_add(agent="sta", symptom="版本归因验证",
                                          resolution="r")
        self.assertEqual(entry["agent_version"], "9.9.9")


class HacooAgentVersionTest(unittest.TestCase):
    """Agent 版本化 (SPEC-evolution A.3 / Part D.1.5):
    agents.json 覆盖版本号 + sync 写 frontmatter。"""

    def test_version_override_and_frontmatter(self) -> None:
        from hacoo import agents as registry

        tmp = tempfile.mkdtemp(prefix="hacoo-agents-")
        agents_file = os.path.join(tmp, "hacoo.agents.json")
        agent_dir = os.path.join(tmp, "agent")
        # 默认版本 1.0.0
        self.assertEqual(registry.version_of("sta", agents_file), "1.0.0")
        # agents.json 覆盖版本号
        with open(agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta"], "versions": {"sta": "1.3.0"}}, fh)
        self.assertEqual(registry.version_of("sta", agents_file), "1.3.0")
        self.assertEqual(registry.version_of("apr", agents_file), "1.0.0")
        # sync 生成的 subagent frontmatter 写入生效版本
        result = registry.sync_opencode(agent_dir=agent_dir, path=agents_file)
        self.assertEqual(len(result["written"]), 1)  # 只有 sta 启用
        with open(os.path.join(agent_dir, "hacoo-sta.md"), "r", encoding="utf-8") as fh:
            content = fh.read()
        frontmatter = content.split("---")[1]
        self.assertIn("version: 1.3.0", frontmatter)
        # list_agents 报告生效版本
        listed = {a["name"]: a for a in registry.list_agents(agents_file)}
        self.assertEqual(listed["sta"]["version"], "1.3.0")


class HacooContractSnapshotTest(unittest.TestCase):
    """契约快照 (SPEC-evolution B.2 / Part D.1.2):
    新增宽松(自动更新) / 删除收窄严格(失败) / check 退出码。"""

    def test_committed_snapshot_has_no_breaking_drift(self) -> None:
        from hacoo import compat
        # 宽松方向 (新增方法/可选参数) 自动更新快照; 破坏性变化必须显式处理
        result = compat.check_snapshots(update_additions=True)
        self.assertEqual(result["breaking"], [],
                         msg="contract breaking changes detected: %s" % result["breaking"])
        self.assertTrue(result["ok"])
        if result["additions"]:
            print("\ncontract snapshot auto-updated for additions: %s"
                  % result["additions"])

    def test_diff_rules_lenient_vs_strict(self) -> None:
        from hacoo import compat

        old = {"api_x": {"params": {"a": {"type": "str", "required": True},
                                    "b": {"type": "int", "required": False}},
                         "capability": "read", "kind": "generic", "tool": None}}
        # 新增方法 + 新增可选参数 + required 放宽 -> 全部宽松, 无 breaking
        new = {"api_x": {"params": {"a": {"type": "str", "required": False},
                                    "b": {"type": "int", "required": False},
                                    "c": {"type": "str", "required": False}},
                         "capability": "read", "kind": "generic", "tool": None},
               "api_y": {"params": {}, "capability": "read",
                         "kind": "generic", "tool": None}}
        breaking, additions = compat.diff_snapshot(old, new)
        self.assertEqual(breaking, [])
        self.assertTrue(any("api_y" in a for a in additions))
        self.assertTrue(any("'c'" in a for a in additions))
        # 删除方法 -> breaking
        breaking, _ = compat.diff_snapshot(old, {})
        self.assertTrue(any("removed" in b for b in breaking))
        # 删除参数 / required 收窄 / 类型变化 / capability 变化 -> breaking
        for mutated in (
                {"api_x": {"params": {"a": {"type": "str", "required": True}},
                           "capability": "read", "kind": "generic", "tool": None}},
                {"api_x": {"params": {"a": {"type": "str", "required": True},
                                      "b": {"type": "int", "required": True}},
                           "capability": "read", "kind": "generic", "tool": None}},
                {"api_x": {"params": {"a": {"type": "dict", "required": True},
                                      "b": {"type": "int", "required": False}},
                           "capability": "read", "kind": "generic", "tool": None}},
                {"api_x": {"params": {"a": {"type": "str", "required": True},
                                      "b": {"type": "int", "required": False}},
                           "capability": "write", "kind": "generic", "tool": None}},
        ):
            breaking, _ = compat.diff_snapshot(old, mutated)
            self.assertTrue(breaking, "expected breaking for %s" % mutated)

    def test_check_exit_codes(self) -> None:
        from hacoo import compat

        tmp = tempfile.mkdtemp(prefix="hacoo-contracts-")
        compat.write_snapshots(tmp)
        clean = compat.check_snapshots(tmp)
        self.assertTrue(clean["ok"])
        self.assertEqual(clean["breaking"], [])
        # 快照含代码中已不存在的方法 (等价于代码删除了方法) -> 检出破坏性变化
        path = os.path.join(tmp, compat.METHODS_SNAPSHOT_FILE)
        with open(path, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
        snapshot["api_deleted_method"] = {"params": {}, "capability": "read",
                                          "kind": "generic", "tool": None}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        tampered = compat.check_snapshots(tmp)
        self.assertFalse(tampered["ok"])
        self.assertTrue(any("removed" in b for b in tampered["breaking"]))
        # CLI 退出码: 当前代码与入库快照一致 -> compat check 退出 0
        import sys as _sys
        from hacoo import cli
        argv = _sys.argv
        _sys.argv = ["hacoo", "compat", "check"]
        try:
            self.assertEqual(cli.main(), 0)
        finally:
            _sys.argv = argv


class HacooBenchTest(unittest.TestCase):
    """能力基准 (SPEC-evolution B.3 / Part D.1.3):
    run_all 计数 / compare 的 fail->pass 与 pass->fail。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.thread = start_test_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")
        self.tmp = tempfile.mkdtemp(prefix="hacoo-bench-test-")
        from hacoo.bench import BenchRunner
        self.runner = BenchRunner(
            benchmarks_dir=os.path.join(self.tmp, "benchmarks"),
            baselines_dir=os.path.join(self.tmp, "baselines"),
            bench_runs_dir=os.path.join(self.tmp, "bench_runs"))

    def tearDown(self) -> None:
        self.client.close()

    def _write_cases(self) -> None:
        """一通过一失败两个 golden case: pass-case 有知识种子, fail-case 没有。"""
        directory = os.path.join(self.tmp, "benchmarks", "sta")
        os.makedirs(directory, exist_ok=True)
        pass_case = {
            "id": "case-pass", "agent": "sta", "tool": "mock_eda",
            "setup": {"knowledge_seed": [
                {"symptom": "bench pass case symptom", "resolution": "r",
                 "tags": ["bench"]}]},
            "script": [
                {"call": "api_knowledge_query", "args": {"keyword": "bench pass"}},
                {"call": "api_decision_record", "args": {
                    "stage": "s", "action": "a", "rationale": "r"}}],
            "assertions": [{"type": "call_ok", "all": True},
                           {"type": "knowledge_hit", "min": 1},
                           {"type": "decision_recorded"}],
            "tags": ["t"]}
        fail_case = {
            "id": "case-fail", "agent": "sta", "tool": "mock_eda",
            "setup": {"knowledge_seed": []},
            "script": [
                {"call": "api_knowledge_query",
                 "args": {"keyword": "绝不命中-bench-fail-case"}},
                {"call": "api_report_timing"}],  # 未加载设计 -> 后端报错
            "assertions": [{"type": "call_ok", "all": True},
                           {"type": "knowledge_hit", "min": 1}],
            "tags": ["t"]}
        for case in (pass_case, fail_case):
            with open(os.path.join(directory, "%s.json" % case["id"]), "w",
                      encoding="utf-8") as fh:
                json.dump(case, fh, ensure_ascii=False)

    def test_run_all_counts(self) -> None:
        self._write_cases()
        report = self.runner.run_all(client=self.client)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 1)
        by_id = {r["id"]: r for r in report["cases"]}
        self.assertTrue(by_id["case-pass"]["pass"])
        self.assertFalse(by_id["case-fail"]["pass"])
        self.assertTrue(by_id["case-fail"]["failures"])  # 失败原因非空

    def test_compare_lists_transitions(self) -> None:
        self._write_cases()
        # 基准: case-pass 挂, case-fail 过 -> 与当前恰好相反
        baseline = {"agent": None, "version": "0.0.0", "ts": 0, "pass_rate": 0.5,
                    "cases": {"case-pass": {"pass": False},
                              "case-fail": {"pass": True}}}
        os.makedirs(self.runner.baselines_dir, exist_ok=True)
        path = os.path.join(self.runner.baselines_dir, "sta@0.0.0.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(baseline, fh)
        result = self.runner.compare(path, client=self.client)
        self.assertEqual(result["fail_to_pass"], ["case-pass"])
        self.assertEqual(result["pass_to_fail"], ["case-fail"])
        self.assertEqual(result["pass_rate_delta"], 0.0)

    def test_requires_unavailable_tool_marks_skipped(self) -> None:
        """BE-2 增强: case 声明 requires 且真实工具不可用时记 skipped
        (不计 pass 也不计 fail); 工具可用 (如 mock_eda) 的 case 照常执行。"""
        directory = os.path.join(self.tmp, "benchmarks", "sta")
        os.makedirs(directory, exist_ok=True)
        skip_case = {
            "id": "case-requires-missing", "agent": "sta", "tool": "mock_eda",
            "requires": ["nonexistent_eda_tool"],
            "script": [], "assertions": [], "tags": ["t"]}
        run_case = {
            "id": "case-requires-ok", "agent": "sta", "tool": "mock_eda",
            "requires": ["mock_eda"],
            "script": [
                {"call": "api_decision_record", "args": {
                    "stage": "s", "action": "a", "rationale": "r"}}],
            "assertions": [{"type": "call_ok", "all": True},
                           {"type": "decision_recorded"}],
            "tags": ["t"]}
        for case in (skip_case, run_case):
            with open(os.path.join(directory, "%s.json" % case["id"]), "w",
                      encoding="utf-8") as fh:
                json.dump(case, fh, ensure_ascii=False)
        report = self.runner.run_all(client=self.client)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["pass_rate"], 1.0)  # skipped 不计入分母
        by_id = {r["id"]: r for r in report["cases"]}
        skipped = by_id["case-requires-missing"]
        self.assertTrue(skipped["skipped"])
        self.assertIsNone(skipped["pass"])
        self.assertIn("nonexistent_eda_tool", skipped["skip_reason"])
        self.assertTrue(by_id["case-requires-ok"]["pass"])

    def test_compare_ignores_skipped_cases(self) -> None:
        """skipped 不算回退: 基准 pass 的 case 本轮 skip 不进 pass_to_fail。"""
        self.test_requires_unavailable_tool_marks_skipped()
        baseline = {"agent": None, "version": "0.0.0", "ts": 0, "pass_rate": 1.0,
                    "cases": {"case-requires-missing": {"pass": True},
                              "case-requires-ok": {"pass": True}}}
        os.makedirs(self.runner.baselines_dir, exist_ok=True)
        path = os.path.join(self.runner.baselines_dir, "sta@0.0.0.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(baseline, fh)
        result = self.runner.compare(path, client=self.client)
        self.assertEqual(result["pass_to_fail"], [])
        self.assertEqual(result["fail_to_pass"], [])


class HacooRealAdapterTest(unittest.TestCase):
    """生产 EDA adapter: 注册/自省/缺失可执行文件时的引导性报错。"""

    def test_all_tools_registered(self) -> None:
        from hacoo.adapters import list_tools
        for tool in ("primetime", "icc2", "innovus", "calibre",
                     "voltus", "redhawk_sc", "isf", "mock_eda"):
            self.assertIn(tool, list_tools())

    def test_adapter_specs_introspection(self) -> None:
        from hacoo.adapters import get_adapter_class
        pt = get_adapter_class("primetime")
        names = [s.name for s in pt.api_specs()]
        self.assertIn("api_report_timing", names)
        self.assertIn("api_read_sdc", names)
        calibre = get_adapter_class("calibre")
        self.assertIn("api_run_drc", [s.name for s in calibre.api_specs()])

    def test_missing_binary_gives_guidance(self) -> None:
        """未安装工具时报错必须含 HACOO_<TOOL>_BIN 配置指引。"""
        from hacoo.adapters.primetime import PrimeTimeAdapter
        adapter = PrimeTimeAdapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter.api_load_design(path="chip.v", top="chip")
        self.assertIn("HACOO_PRIMETIME_BIN", str(ctx.exception))

    def test_agent_tool_mapping(self) -> None:
        from hacoo import agents
        self.assertEqual(agents.get("sta").tools, ["primetime", "mock_eda"])
        self.assertEqual(agents.get("apr").tools, ["icc2", "innovus", "mock_eda"])
        self.assertEqual(agents.get("pv").tools, ["calibre", "mock_eda"])
        self.assertEqual(agents.get("irem").tools, ["redhawk_sc", "voltus", "mock_eda"])


class HacooAprSubstageTest(unittest.TestCase):
    """BE-1 (SPEC-evolution C.3): icc2/innovus 六个 APR 子阶段方法。

    验收: 两 adapter 方法签名一致 (T2 统一接口); mock 模式下各自生成
    正确的工具命令序列 (ICC2 系 vs Innovus 系, 差异封装在 adapter 内)。
    """

    SUBSTAGE_METHODS = ("api_fp_create", "api_fp_place_macros", "api_place_opt",
                        "api_cts_build", "api_cts_report", "api_route_opt")
    _MOCK_ENV = ("HACOO_ICC2_MOCK", "HACOO_INNOVUS_MOCK")

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._MOCK_ENV}

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _signature(fn: Any) -> List[Any]:
        import inspect
        return [(p.name, str(p.annotation), p.default)
                for p in inspect.signature(fn).parameters.values()
                if p.name != "self"]

    def _mock_adapter(self, cls: Any, env_key: str) -> Any:
        os.environ[env_key] = "1"
        return cls()

    # ---------------------------------------------------------- 签名一致性

    def test_substage_methods_registered_and_signatures_match(self) -> None:
        from hacoo.adapters.icc2 import ICC2Adapter
        from hacoo.adapters.innovus import InnovusAdapter
        icc2_specs = {s.name: s for s in ICC2Adapter.api_specs()}
        inv_specs = {s.name: s for s in InnovusAdapter.api_specs()}
        for name in self.SUBSTAGE_METHODS:
            self.assertIn(name, icc2_specs, "icc2 缺少 %s" % name)
            self.assertIn(name, inv_specs, "innovus 缺少 %s" % name)
            sig_i = self._signature(getattr(ICC2Adapter, name))
            sig_v = self._signature(getattr(InnovusAdapter, name))
            self.assertEqual(sig_i, sig_v,
                             "%s 签名不一致: icc2=%s innovus=%s" % (name, sig_i, sig_v))

    # ---------------------------------------------------------- ICC2 命令

    def test_icc2_substage_commands(self) -> None:
        from hacoo.adapters.icc2 import ICC2Adapter
        adapter = self._mock_adapter(ICC2Adapter, "HACOO_ICC2_MOCK")
        adapter.api_load_design(netlist="chip.v", top="chip", lib_name="ndm_lib",
                                tech_tf="tech.tf", ndm_libs=["ref.ndm"])
        fp = adapter.api_fp_create(utilization=0.7, aspect_ratio=1.5, core_margin=2.0)
        adapter.api_fp_place_macros()
        place = adapter.api_place_opt()
        adapter.api_cts_build(target_skew_ps=80)
        report = adapter.api_cts_report()
        route = adapter.api_route_opt()
        shell = adapter._icc2()
        self.assertEqual(shell.commands, [
            "create_lib ndm_lib -technology {tech.tf} -ref_libs {ref.ndm}",
            "read_verilog -top chip {chip.v}",
            "link_block",
            "initialize_floorplan -core_utilization 0.700 -core_offset {2.00}"
            " -side_ratio {1 1.500}",
            "report_utilization",
            "set_app_options -name plan.macro.macro_place_only -value true",
            "create_placement -floorplan",
            "place_opt",
            "report_congestion",
            "set_clock_tree_options -target_skew 0.080",
            "clock_opt -from build_clock -to route_clock",
            "report_clock_qor -type summary",
            "route_opt",
            "report_design -drc",
        ])
        # BE-2: 各子阶段指标已从 mock 报告解析进返回 data (增强版归一 key)
        self.assertEqual(fp["data"]["utilization"], 0.7)
        self.assertEqual(place["data"]["hotspot_count"], 3)
        self.assertEqual(place["data"]["overflow_max"], 6)
        self.assertEqual(route["data"]["drc_count"], 5)
        # cts_report 解析 mock 应答: skew/latency_max/clock_power (ICC2 单位 ns)
        self.assertEqual(report["data"]["skew"], 0.042)
        self.assertEqual(report["data"]["latency_max"], 0.352)
        self.assertEqual(report["data"]["clock_power"], 12.45)
        self.assertEqual(report["data"]["units"], "ns")
        self.assertEqual(len(adapter.stage_history), 5)

    # ---------------------------------------------------------- Innovus 命令

    def test_innovus_substage_commands(self) -> None:
        from hacoo.adapters.innovus import InnovusAdapter
        adapter = self._mock_adapter(InnovusAdapter, "HACOO_INNOVUS_MOCK")
        adapter.api_load_design(netlist="chip.v", top="chip",
                                lef=["tech.lef"], libs=["tt.lib"])
        fp = adapter.api_fp_create(utilization=0.7, aspect_ratio=1.5, core_margin=3.0)
        adapter.api_fp_place_macros()
        place = adapter.api_place_opt()
        adapter.api_cts_build(target_skew_ps=80)
        report = adapter.api_cts_report()
        route = adapter.api_route_opt()
        shell = adapter._inv()
        self.assertEqual(shell.commands, [
            "set init_lef_file {tech.lef}",
            "set init_mmmc_file {view_definition.tcl}",
            "set init_verilog {chip.v}",
            "set init_top_cell {chip}",
            "init_design",
            "floorPlan -r 1.500 0.700 3.00 3.00 3.00 3.00",
            "report_floorplan",
            "planDesign",
            "place_opt_design",
            "report_congestion",
            "set_ccopt_property target_skew 80",
            "ccopt_design -cts",
            "report_ccopt_clock_trees",
            "report_ccopt_skew_groups",
            "routeDesign",
            "optDesign -postRoute",
            "verify_drc",
        ])
        # BE-2: 各子阶段指标已从 mock 报告解析进返回 data (增强版归一 key)
        self.assertEqual(fp["data"]["utilization"], 0.698)
        self.assertEqual(place["data"]["hotspot_count"], 4)
        self.assertEqual(place["data"]["overflow_max"], 7)
        self.assertEqual(route["data"]["drc_count"], 7)
        # cts_report 解析 mock 应答: skew/latency_max/clock_power (Innovus 单位 ps)
        self.assertEqual(report["data"]["skew"], 45.2)
        self.assertEqual(report["data"]["latency_max"], 380.1)
        self.assertEqual(report["data"]["clock_power"], 8.73)
        self.assertEqual(report["data"]["units"], "ps")
        self.assertEqual(len(adapter.stage_history), 5)

    # ---------------------------------------------------------- binary 缺失引导

    def test_missing_binary_guidance(self) -> None:
        """未开 mock 且未安装工具时报错必须含 HACOO_<TOOL>_BIN 指引。"""
        from hacoo.adapters.icc2 import ICC2Adapter
        from hacoo.adapters.innovus import InnovusAdapter
        for key in self._MOCK_ENV:
            os.environ.pop(key, None)
        with self.assertRaises(RuntimeError) as ctx:
            ICC2Adapter().api_load_design(netlist="chip.v", top="chip",
                                          lib_name="ndm_lib", tech_tf="tech.tf",
                                          ndm_libs=["ref.ndm"])
        self.assertIn("HACOO_ICC2_BIN", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            InnovusAdapter().api_load_design(netlist="chip.v", top="chip",
                                             lef=["tech.lef"], libs=["tt.lib"])
        self.assertIn("HACOO_INNOVUS_BIN", str(ctx.exception))

    # ---------------------------------------------------------- 全链路 (mock 经网关)

    def test_substage_methods_through_gateway(self) -> None:
        """mock 模式经完整五层链路: spawn icc2 -> 6 个子阶段方法全部可调。"""
        os.environ["HACOO_ICC2_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("icc2")
        try:
            client.call("api_load_design", instance_id=iid, agent="apr",
                        arguments={"netlist": "chip.v", "top": "chip",
                                   "lib_name": "ndm_lib", "tech_tf": "tech.tf",
                                   "ndm_libs": ["ref.ndm"]})
            resp = client.call("api_fp_create", instance_id=iid, agent="apr",
                               arguments={"utilization": 0.65})
            self.assertIn("initialize_floorplan",
                          json.dumps(resp["data"], ensure_ascii=False))
            for method in ("api_fp_place_macros", "api_place_opt", "api_cts_build",
                           "api_cts_report", "api_route_opt"):
                resp = client.call(method, instance_id=iid, agent="apr", arguments={})
                self.assertIsNotNone(resp)
        finally:
            client.release(iid)


class HacooAprMetricsTest(unittest.TestCase):
    """BE-2 增强 (SPEC-evolution C.3): APR 阶段指标提取进观测层。

    覆盖: (1) 报告解析纯函数对 tests/fixtures/ 样例文本字段提取齐全
    (数值与 _MOCK_CANNED 不同, 证明解析逻辑而非死记); (2) 独立报告方法
    api_report_congestion / api_route_drc_report 两工具签名一致、mock 下
    命令与解析正确; (3) mock 全链路下阶段 metrics 经网关观测钩子进入
    观测层 (api_observations_query 可查)。指标 key 两工具归一:
    hotspot_count / overflow_max / skew / latency_max / clock_power /
    drc_count; top_rules 为结构化列表不进扁平 metrics。
    """

    _MOCK_ENV = ("HACOO_ICC2_MOCK", "HACOO_INNOVUS_MOCK")
    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._MOCK_ENV}

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name),
                  "r", encoding="utf-8") as fh:
            return fh.read()

    # ---------------------------------------------------------- 解析纯函数 (fixtures)

    def test_icc2_report_parsers(self) -> None:
        from hacoo.adapters import icc2
        util = icc2.parse_utilization_report(
            "Report : utilization\nDesign : top\n"
            "Utilization Ratio    : 0.642\n")
        self.assertEqual(util, {"utilization": 0.642})
        cong = icc2.parse_congestion_report(
            self._fixture("icc2_report_congestion.txt"))
        self.assertEqual(cong, {"hotspot_count": 7, "overflow_max": 8})
        clock = icc2.parse_clock_qor_report(
            self._fixture("icc2_report_clock_qor.txt"))
        self.assertEqual(clock, {"skew": 0.055, "latency_max": 0.410,
                                 "clock_power": 9.87, "units": "ns"})
        drc = icc2.parse_drc_report(self._fixture("icc2_report_design_drc.txt"))
        self.assertEqual(drc, {"drc_count": 3,
                               "top_rules": [("Spacing", 2), ("Short", 1)]})
        # 解析不到时数值字段为 None / top_rules 为空 (不抛异常)
        self.assertIsNone(icc2.parse_utilization_report("garbage")["utilization"])
        self.assertEqual(icc2.parse_congestion_report("garbage"),
                         {"hotspot_count": None, "overflow_max": None})
        self.assertEqual(icc2.parse_drc_report("garbage"),
                         {"drc_count": None, "top_rules": []})

    def test_innovus_report_parsers(self) -> None:
        from hacoo.adapters import innovus
        util = innovus.parse_utilization_report(
            "Floorplan information:\n  Core Utilization: 0.655\n")
        self.assertEqual(util, {"utilization": 0.655})
        cong = innovus.parse_congestion_report(
            self._fixture("innovus_report_congestion.txt"))
        self.assertEqual(cong, {"hotspot_count": 9, "overflow_max": 11})
        clock = innovus.parse_clock_report(
            self._fixture("innovus_report_ccopt_clock_trees.txt"))
        self.assertEqual(clock, {"skew": 33.7, "latency_max": 402.5,
                                 "clock_power": 6.42, "units": "ps"})
        drc = innovus.parse_drc_report(self._fixture("innovus_verify_drc.txt"))
        self.assertEqual(drc, {"drc_count": 11,
                               "top_rules": [("Spacing", 8), ("Short", 3)]})
        self.assertIsNone(innovus.parse_utilization_report("garbage")["utilization"])
        self.assertEqual(innovus.parse_congestion_report("garbage"),
                         {"hotspot_count": None, "overflow_max": None})
        self.assertEqual(innovus.parse_drc_report("garbage"),
                         {"drc_count": None, "top_rules": []})

    # ---------------------------------------------------------- 独立报告方法 (mock)

    def test_report_methods_signatures_match(self) -> None:
        """api_report_congestion / api_route_drc_report 两工具签名一致 (T2)。"""
        import inspect
        from hacoo.adapters.icc2 import ICC2Adapter
        from hacoo.adapters.innovus import InnovusAdapter
        icc2_specs = {s.name: s for s in ICC2Adapter.api_specs()}
        inv_specs = {s.name: s for s in InnovusAdapter.api_specs()}
        for name in ("api_report_congestion", "api_route_drc_report",
                     "api_cts_report"):
            self.assertIn(name, icc2_specs, "icc2 缺少 %s" % name)
            self.assertIn(name, inv_specs, "innovus 缺少 %s" % name)
            sig_i = [(p.name, str(p.annotation), p.default)
                     for p in inspect.signature(
                         getattr(ICC2Adapter, name)).parameters.values()
                     if p.name != "self"]
            sig_v = [(p.name, str(p.annotation), p.default)
                     for p in inspect.signature(
                         getattr(InnovusAdapter, name)).parameters.values()
                     if p.name != "self"]
            self.assertEqual(sig_i, sig_v,
                             "%s 签名不一致: icc2=%s innovus=%s" % (name, sig_i, sig_v))

    def test_icc2_standalone_report_methods(self) -> None:
        """api_report_congestion / api_route_drc_report: 命令与解析 (序列首元素)。"""
        from hacoo.adapters.icc2 import ICC2Adapter
        os.environ["HACOO_ICC2_MOCK"] = "1"
        adapter = ICC2Adapter()
        adapter.api_load_design(netlist="chip.v", top="chip", lib_name="ndm_lib",
                                tech_tf="tech.tf", ndm_libs=["ref.ndm"])
        cong = adapter.api_report_congestion()
        self.assertEqual(cong["data"]["hotspot_count"], 3)
        self.assertEqual(cong["data"]["overflow_max"], 6)
        self.assertEqual(cong["data"]["commands"], ["report_congestion"])
        drc = adapter.api_route_drc_report()
        self.assertEqual(drc["data"]["drc_count"], 5)
        self.assertEqual(drc["data"]["top_rules"],
                         [("Spacing", 3), ("Short", 2)])
        self.assertEqual(drc["data"]["commands"], ["report_design -drc"])

    def test_innovus_standalone_report_methods(self) -> None:
        from hacoo.adapters.innovus import InnovusAdapter
        os.environ["HACOO_INNOVUS_MOCK"] = "1"
        adapter = InnovusAdapter()
        adapter.api_load_design(netlist="chip.v", top="chip",
                                lef=["tech.lef"], libs=["tt.lib"])
        cong = adapter.api_report_congestion()
        self.assertEqual(cong["data"]["hotspot_count"], 4)
        self.assertEqual(cong["data"]["overflow_max"], 7)
        self.assertEqual(cong["data"]["commands"], ["report_congestion"])
        drc = adapter.api_route_drc_report()
        self.assertEqual(drc["data"]["drc_count"], 7)
        self.assertEqual(drc["data"]["top_rules"],
                         [("Spacing", 5), ("Short", 2)])
        self.assertEqual(drc["data"]["commands"], ["verify_drc"])

    # ---------------------------------------------------------- 观测层白名单

    def test_stage_metric_keys_in_observation_whitelist(self) -> None:
        from hacoo.observations import KNOWN_METRIC_KEYS, extract_metrics
        for key in ("utilization", "hotspot_count", "overflow_max", "skew",
                    "latency_max", "clock_power", "drc_count"):
            self.assertIn(key, KNOWN_METRIC_KEYS)
        # 迁移后旧 key 已移出白名单
        for old_key in ("congestion_hotspots", "latency"):
            self.assertNotIn(old_key, KNOWN_METRIC_KEYS)
        data = {"utilization": 0.7, "hotspot_count": 3, "overflow_max": 6,
                "skew": 0.042, "latency_max": 0.352, "clock_power": 12.45,
                "drc_count": 5, "top_rules": [("Spacing", 3)],
                "dropped_none": None, "dropped_str": "x"}
        metrics = extract_metrics("icc2", data)
        # top_rules 为结构化列表, 不进扁平 metrics
        self.assertEqual(metrics, {"utilization": 0.7, "hotspot_count": 3,
                                   "overflow_max": 6, "skew": 0.042,
                                   "latency_max": 0.352, "clock_power": 12.45,
                                   "drc_count": 5})

    # ---------------------------------------------------------- mock 全链路进观测层

    def _run_substage_flow(self, tool: str, env_key: str,
                           load_args: Dict[str, Any]) -> Any:
        os.environ[env_key] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn(tool)
        self.addCleanup(client.release, iid)
        client.call("api_load_design", instance_id=iid, agent="apr",
                    arguments=load_args)
        for method in ("api_fp_create", "api_fp_place_macros", "api_place_opt",
                       "api_report_congestion", "api_cts_build",
                       "api_cts_report", "api_route_opt",
                       "api_route_drc_report"):
            client.call(method, instance_id=iid, agent="apr", arguments={})
        return client

    def _assert_stage_metrics_observed(self, client: Any, tool: str) -> None:
        obs = client.call("api_observations_query",
                          arguments={"agent": "apr", "limit": 500})["data"]
        by_method: Dict[str, Dict[str, Any]] = {}
        for entry in obs:
            if entry["tool"] == tool and entry["ok"]:
                by_method.setdefault(entry["method"], {}).update(
                    entry.get("metrics") or {})
        expected = {
            "api_fp_create": ["utilization"],
            "api_place_opt": ["hotspot_count", "overflow_max"],
            "api_report_congestion": ["hotspot_count", "overflow_max"],
            "api_cts_report": ["skew", "latency_max", "clock_power"],
            "api_route_opt": ["drc_count"],
            "api_route_drc_report": ["drc_count"],
        }
        for method, keys in expected.items():
            self.assertIn(method, by_method, "无 %s 观测记录" % method)
            for key in keys:
                self.assertIsInstance(by_method[method].get(key), (int, float),
                                      "%s 观测缺少指标 %s" % (method, key))

    def test_icc2_metrics_into_observations_through_gateway(self) -> None:
        client = self._run_substage_flow(
            "icc2", "HACOO_ICC2_MOCK",
            {"netlist": "chip.v", "top": "chip", "lib_name": "ndm_lib",
             "tech_tf": "tech.tf", "ndm_libs": ["ref.ndm"]})
        self._assert_stage_metrics_observed(client, "icc2")

    def test_innovus_metrics_into_observations_through_gateway(self) -> None:
        client = self._run_substage_flow(
            "innovus", "HACOO_INNOVUS_MOCK",
            {"netlist": "chip.v", "top": "chip",
             "lef": ["tech.lef"], "libs": ["tt.lib"]})
        self._assert_stage_metrics_observed(client, "innovus")


class HacooSynthesisAdapterTest(unittest.TestCase):
    """FE-1 (SPEC-evolution C.1): Genus/DC 真实综合 adapter。

    验收: (1) 两工具注册且三个同名方法签名一致 (T2 统一接口);
    (2) mock 模式下各自生成正确的工具命令序列 (read_hdl/syn_* 系 vs
    analyze/elaborate/compile_ultra 系); (3) report_qor 解析为归一化
    五字段 {wns, tns, area, leakage, total_power} — 样例取自
    tests/fixtures/ (与 _MOCK_CANNED 数值不同, 证明解析逻辑而非死记);
    (4) 五字段经网关观测钩子进观测层; (5) binary 缺失时报错含
    HACOO_<TOOL>_BIN 配置指引。
    """

    SYNTH_METHODS = ("api_synth_elaborate", "api_synth_compile", "api_report_qor")
    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")
    _MOCK_ENV = ("HACOO_GENUS_MOCK", "HACOO_DC_SHELL_MOCK")

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._MOCK_ENV}

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _signature(fn: Any) -> List[Any]:
        import inspect
        return [(p.name, str(p.annotation), p.default)
                for p in inspect.signature(fn).parameters.values()
                if p.name != "self"]

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name),
                  "r", encoding="utf-8") as fh:
            return fh.read()

    # ---------------------------------------------------------- 注册与签名

    def test_tools_registered_and_signatures_match(self) -> None:
        from hacoo.adapters import get_adapter_class, list_tools
        for tool in ("genus", "dc_shell"):
            self.assertIn(tool, list_tools())
        genus_cls = get_adapter_class("genus")
        dc_cls = get_adapter_class("dc_shell")
        genus_specs = {s.name: s for s in genus_cls.api_specs()}
        dc_specs = {s.name: s for s in dc_cls.api_specs()}
        for name in self.SYNTH_METHODS:
            self.assertIn(name, genus_specs, "genus 缺少 %s" % name)
            self.assertIn(name, dc_specs, "dc_shell 缺少 %s" % name)
            sig_g = self._signature(getattr(genus_cls, name))
            sig_d = self._signature(getattr(dc_cls, name))
            self.assertEqual(sig_g, sig_d,
                             "%s 签名不一致: genus=%s dc=%s" % (name, sig_g, sig_d))

    def test_synthesis_agent_whitelist(self) -> None:
        from hacoo import agents
        tools = agents.get("synthesis").tools
        self.assertIn("genus", tools)
        self.assertIn("dc_shell", tools)

    # ---------------------------------------------------------- 解析纯函数 (fixtures)

    def test_parse_genus_qor_fixture(self) -> None:
        from hacoo.adapters.synthesis import parse_genus_qor
        qor = parse_genus_qor(self._fixture("genus_report_qor.txt"))
        self.assertEqual(qor, {"wns": -0.237, "tns": -2.915, "area": 18110.42,
                               "leakage": 1.037, "total_power": 5.882})

    def test_parse_dc_qor_fixture(self) -> None:
        from hacoo.adapters.synthesis import parse_dc_qor
        qor = parse_dc_qor(self._fixture("dc_report_qor.txt"))
        self.assertEqual(qor, {"wns": -0.084, "tns": -1.237, "area": 1822.468,
                               "leakage": 0.342, "total_power": 2.6908})

    def test_parsers_degrade_to_none(self) -> None:
        from hacoo.adapters.synthesis import parse_dc_qor, parse_genus_qor
        for parser in (parse_genus_qor, parse_dc_qor):
            qor = parser("garbage report without metrics")
            self.assertEqual(qor, {"wns": None, "tns": None, "area": None,
                                   "leakage": None, "total_power": None})

    # ---------------------------------------------------------- FE-2 指标归一

    def test_qor_output_normalized(self) -> None:
        """FE-2: 两工具解析输出归一为同一 key 集合, fixtures 全字段为 float。"""
        from hacoo.adapters.synthesis import parse_dc_qor, parse_genus_qor
        expected_keys = {"wns", "tns", "area", "leakage", "total_power"}
        for parser, fixture in ((parse_genus_qor, "genus_report_qor.txt"),
                                (parse_dc_qor, "dc_report_qor.txt")):
            qor = parser(self._fixture(fixture))
            self.assertEqual(set(qor), expected_keys,
                             "%s 输出 key 未归一" % fixture)
            for key, val in qor.items():
                self.assertIsInstance(val, float,
                                      "%s.%s 不是 float: %r" % (fixture, key, val))

    # ---------------------------------------------------------- Genus 命令序列 (mock)

    def test_genus_commands_and_qor(self) -> None:
        from hacoo.adapters.synthesis import GenusAdapter
        os.environ["HACOO_GENUS_MOCK"] = "1"
        adapter = GenusAdapter()
        elab = adapter.api_synth_elaborate(files=["rtl/chip.v", "rtl/alu.v"],
                                           top="chip")
        comp = adapter.api_synth_compile(effort="high")
        qor = adapter.api_report_qor()
        shell = adapter._session()
        self.assertEqual(shell.commands, [
            "read_hdl {rtl/chip.v rtl/alu.v}",
            "elaborate chip",
            "syn_generic -effort high",
            "syn_map -effort high",
            "syn_opt",
            "report_qor",
        ])
        self.assertEqual(elab["data"]["files"], 2)
        self.assertEqual(comp["data"]["compiles_done"], 1)
        # mock 应答按 Genus 风格编写: 五字段全部解析出来
        self.assertEqual(qor["data"]["wns"], -0.152)
        self.assertEqual(qor["data"]["tns"], -1.834)
        self.assertEqual(qor["data"]["area"], 15234.68)
        self.assertEqual(qor["data"]["leakage"], 0.842)
        self.assertEqual(qor["data"]["total_power"], 4.059)
        self.assertEqual(qor["data"]["top"], "chip")

    # ---------------------------------------------------------- DC 命令序列 (mock)

    def test_dc_commands_and_qor(self) -> None:
        from hacoo.adapters.synthesis import DCAdapter
        os.environ["HACOO_DC_SHELL_MOCK"] = "1"
        adapter = DCAdapter()
        adapter.api_synth_elaborate(files=["rtl/chip.v"], top="chip")
        comp = adapter.api_synth_compile()  # 缺省 medium
        adapter.api_synth_compile(effort="high")
        qor = adapter.api_report_qor()
        shell = adapter._session()
        self.assertEqual(shell.commands, [
            "analyze -format verilog {rtl/chip.v}",
            "elaborate chip",
            "compile_ultra",
            "compile_ultra",
            "compile_ultra -incremental",
            "report_qor",
            "report_power",
        ])
        self.assertEqual(comp["data"]["effort"], "medium")
        self.assertEqual(len(adapter.compile_history), 2)
        # DC report_qor 不含功耗 -> 由 report_power 补齐 (T2 封装)
        self.assertEqual(qor["data"]["wns"], -0.042)
        self.assertEqual(qor["data"]["tns"], -0.518)
        self.assertEqual(qor["data"]["area"], 13456.78)
        self.assertEqual(qor["data"]["leakage"], 0.215)
        self.assertEqual(qor["data"]["total_power"], 1.8649)
        self.assertEqual(qor["data"]["commands"], ["report_qor", "report_power"])

    # ---------------------------------------------------------- FE-3 收敛迭代 (extra_tcl + 序列应答)

    def test_genus_compile_extra_tcl_precedes_compile(self) -> None:
        """FE-3: extra_tcl 策略命令必须在编译命令之前注入 (两工具一致),
        并记入 compile_history。"""
        from hacoo.adapters.synthesis import DCAdapter, GenusAdapter
        os.environ["HACOO_GENUS_MOCK"] = "1"
        os.environ["HACOO_DC_SHELL_MOCK"] = "1"
        genus = GenusAdapter()
        genus.api_synth_elaborate(files=["chip.v"], top="chip")
        comp = genus.api_synth_compile(
            effort="high", extra_tcl="ungroup -flatten [get_cells u_mid]")
        shell = genus._session()
        idx_extra = shell.commands.index("ungroup -flatten [get_cells u_mid]")
        idx_compile = shell.commands.index("syn_generic -effort high")
        self.assertLess(idx_extra, idx_compile)
        self.assertEqual(comp["data"]["extra_tcl"],
                         "ungroup -flatten [get_cells u_mid]")
        dc = DCAdapter()
        dc.api_synth_elaborate(files=["chip.v"], top="chip")
        comp_d = dc.api_synth_compile(
            effort="medium", extra_tcl="set_boundary_optimization true")
        shell_d = dc._session()
        self.assertLess(shell_d.commands.index("set_boundary_optimization true"),
                        shell_d.commands.index("compile_ultra"))
        self.assertEqual(comp_d["data"]["extra_tcl"],
                         "set_boundary_optimization true")
        # 不带 extra_tcl 时 history 不含该键 (向后兼容)
        comp_plain = dc.api_synth_compile(effort="low")
        self.assertNotIn("extra_tcl", comp_plain["data"])

    def test_genus_qor_sequence_monotonic_convergence(self) -> None:
        """FE-3: Genus mock 序列应答建模 WNS 逐轮改善
        -0.152 -> -0.081 -> 0.012, 耗尽后钳制在收敛值。"""
        from hacoo.adapters.synthesis import GenusAdapter
        os.environ["HACOO_GENUS_MOCK"] = "1"
        adapter = GenusAdapter()
        adapter.api_synth_elaborate(files=["chip.v"], top="chip")
        adapter.api_synth_compile(effort="medium")
        wns_rounds = [adapter.api_report_qor()["data"]["wns"]]
        adapter.api_synth_compile(effort="high")
        wns_rounds.append(adapter.api_report_qor()["data"]["wns"])
        adapter.api_synth_compile(effort="high",
                                  extra_tcl="set_db boundary_optimize true")
        wns_rounds.append(adapter.api_report_qor()["data"]["wns"])
        self.assertEqual(wns_rounds, [-0.152, -0.081, 0.012])
        self.assertTrue(all(b > a for a, b in zip(wns_rounds, wns_rounds[1:])),
                        "WNS 未单调改善: %s" % wns_rounds)
        # 序列耗尽后钳制在收敛值 (继续 report 仍返回 0.012)
        self.assertEqual(adapter.api_report_qor()["data"]["wns"], 0.012)

    # ---------------------------------------------------------- 参数校验与报错引导

    def test_compile_effort_validation(self) -> None:
        from hacoo.adapters.synthesis import DCAdapter, GenusAdapter
        os.environ["HACOO_GENUS_MOCK"] = "1"
        os.environ["HACOO_DC_SHELL_MOCK"] = "1"
        for cls in (GenusAdapter, DCAdapter):
            adapter = cls()
            with self.assertRaises(RuntimeError):
                adapter.api_synth_compile()  # 未 elaborate
            adapter.api_synth_elaborate(files=["chip.v"], top="chip")
            with self.assertRaises(ValueError):
                adapter.api_synth_compile(effort="ultra")

    def test_missing_binary_guidance(self) -> None:
        """未开 mock 且未安装工具时报错必须含 HACOO_<TOOL>_BIN 指引。"""
        from hacoo.adapters.synthesis import DCAdapter, GenusAdapter
        for key in self._MOCK_ENV:
            os.environ.pop(key, None)
        with self.assertRaises(RuntimeError) as ctx:
            GenusAdapter().api_synth_elaborate(files=["chip.v"], top="chip")
        self.assertIn("HACOO_GENUS_BIN", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            DCAdapter().api_synth_elaborate(files=["chip.v"], top="chip")
        self.assertIn("HACOO_DC_SHELL_BIN", str(ctx.exception))

    # ---------------------------------------------------------- 全链路 (mock 经网关) 进观测层

    def test_genus_through_gateway_into_observations(self) -> None:
        """mock 模式经完整五层链路: synthesis agent spawn genus ->
        elaborate/compile/report_qor -> 五字段指标进观测层。"""
        os.environ["HACOO_GENUS_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("genus")
        try:
            client.call("api_synth_elaborate", instance_id=iid, agent="synthesis",
                        arguments={"files": ["rtl/chip.v"], "top": "chip"})
            client.call("api_synth_compile", instance_id=iid, agent="synthesis",
                        arguments={"effort": "medium"})
            resp = client.call("api_report_qor", instance_id=iid,
                               agent="synthesis", arguments={})
            for key in ("wns", "tns", "area", "leakage", "total_power"):
                self.assertIsInstance(resp["data"].get(key), (int, float),
                                      "report_qor 缺少字段 %s" % key)
        finally:
            client.release(iid)
        obs = client.call("api_observations_query",
                          arguments={"agent": "synthesis", "limit": 500})["data"]
        qor_obs = [o for o in obs if o.get("ok") and o.get("tool") == "genus"
                   and o.get("method") == "api_report_qor"]
        self.assertTrue(qor_obs, "无 api_report_qor 观测记录")
        for key in ("wns", "tns", "area", "leakage", "total_power"):
            self.assertIsInstance(qor_obs[-1]["metrics"].get(key), (int, float),
                                  "观测缺少指标 %s" % key)

    def test_qor_metric_keys_in_observation_whitelist(self) -> None:
        from hacoo.observations import KNOWN_METRIC_KEYS, extract_metrics
        for key in ("wns", "tns", "area", "leakage", "total_power"):
            self.assertIn(key, KNOWN_METRIC_KEYS)
        data = {"wns": -0.152, "tns": -1.834, "area": 15234.68,
                "leakage": 0.842, "total_power": 4.059, "top": "chip"}
        metrics = extract_metrics("genus", data)
        self.assertEqual(metrics, {"wns": -0.152, "tns": -1.834,
                                   "area": 15234.68, "leakage": 0.842,
                                   "total_power": 4.059})


class HacooDftAdapterTest(unittest.TestCase):
    """FE-4 (SPEC-evolution C.1): Tessent/DFTMAX 真实 DFT adapter。

    验收: (1) 两工具注册且三个同名方法签名一致 (T2 统一接口);
    (2) mock 模式下各自生成正确的工具命令序列 (Tessent set_system_mode/
    insert_test_logic 系 vs DFTMAX set_scan_configuration/insert_dft 系);
    (3) 覆盖率报告解析为归一化三字段 {test_coverage, fault_coverage,
    untestable} — 样例取自 tests/fixtures/ (与 _MOCK_CANNED 数值不同);
    (4) 三字段在观测白名单且方向表登记 (覆盖率 LOWER_IS_WORSE);
    (5) 状态守卫: 未插 scan 不可 ATPG, 未 ATPG 不可报覆盖率;
    (6) binary 缺失时报错含 HACOO_<TOOL>_BIN 配置指引。
    """

    DFT_METHODS = ("api_dft_insert_scan", "api_dft_atpg", "api_dft_report_coverage")
    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")
    _MOCK_ENV = ("HACOO_TESSENT_MOCK", "HACOO_DFTMAX_MOCK")

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._MOCK_ENV}

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _signature(fn: Any) -> List[Any]:
        import inspect
        return [(p.name, str(p.annotation), p.default)
                for p in inspect.signature(fn).parameters.values()
                if p.name != "self"]

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name),
                  "r", encoding="utf-8") as fh:
            return fh.read()

    # ---------------------------------------------------------- 注册与签名

    def test_tools_registered_and_signatures_match(self) -> None:
        from hacoo.adapters import get_adapter_class, list_tools
        for tool in ("tessent", "dftmax"):
            self.assertIn(tool, list_tools())
        tess_cls = get_adapter_class("tessent")
        dftmax_cls = get_adapter_class("dftmax")
        tess_specs = {s.name: s for s in tess_cls.api_specs()}
        dftmax_specs = {s.name: s for s in dftmax_cls.api_specs()}
        for name in self.DFT_METHODS:
            self.assertIn(name, tess_specs, "tessent 缺少 %s" % name)
            self.assertIn(name, dftmax_specs, "dftmax 缺少 %s" % name)
            sig_t = self._signature(getattr(tess_cls, name))
            sig_d = self._signature(getattr(dftmax_cls, name))
            self.assertEqual(sig_t, sig_d,
                             "%s 签名不一致: tessent=%s dftmax=%s"
                             % (name, sig_t, sig_d))

    def test_dft_agent_whitelist(self) -> None:
        from hacoo import agents
        tools = agents.get("dft").tools
        self.assertIn("tessent", tools)
        self.assertIn("dftmax", tools)

    # ---------------------------------------------------------- 解析纯函数 (fixtures)

    def test_parse_tessent_statistics_fixture(self) -> None:
        from hacoo.adapters.dft import parse_tessent_statistics
        cov = parse_tessent_statistics(self._fixture("tessent_statistics.txt"))
        self.assertEqual(cov, {"test_coverage": 94.18, "fault_coverage": 93.02,
                               "untestable": 1043.0})

    def test_parse_dftmax_coverage_fixture(self) -> None:
        from hacoo.adapters.dft import parse_dftmax_coverage
        cov = parse_dftmax_coverage(self._fixture("dftmax_coverage.txt"))
        self.assertEqual(cov, {"test_coverage": 95.66, "fault_coverage": 94.77,
                               "untestable": 287.0})

    def test_parsers_degrade_to_none(self) -> None:
        from hacoo.adapters.dft import parse_dftmax_coverage, parse_tessent_statistics
        for parser in (parse_tessent_statistics, parse_dftmax_coverage):
            cov = parser("garbage report without coverage")
            self.assertEqual(cov, {"test_coverage": None, "fault_coverage": None,
                                   "untestable": None})

    def test_coverage_normalized_and_in_whitelist(self) -> None:
        """三字段 key 归一 + 观测白名单登记 + 方向表登记 (覆盖率越低越差)。"""
        from hacoo.adapters.dft import parse_dftmax_coverage, parse_tessent_statistics
        from hacoo.insights import HIGHER_IS_WORSE, LOWER_IS_WORSE
        from hacoo.observations import KNOWN_METRIC_KEYS
        expected_keys = {"test_coverage", "fault_coverage", "untestable"}
        for parser, fixture in ((parse_tessent_statistics, "tessent_statistics.txt"),
                                (parse_dftmax_coverage, "dftmax_coverage.txt")):
            cov = parser(self._fixture(fixture))
            self.assertEqual(set(cov), expected_keys,
                             "%s 输出 key 未归一" % fixture)
            for key, val in cov.items():
                self.assertIsInstance(val, float,
                                      "%s.%s 不是 float: %r" % (fixture, key, val))
        for key in ("test_coverage", "fault_coverage", "untestable"):
            self.assertIn(key, KNOWN_METRIC_KEYS)
        self.assertIn("test_coverage", LOWER_IS_WORSE)
        self.assertIn("fault_coverage", LOWER_IS_WORSE)
        self.assertIn("untestable", HIGHER_IS_WORSE)

    # ---------------------------------------------------------- 命令序列与状态守卫 (mock)

    def test_tessent_commands_and_coverage(self) -> None:
        from hacoo.adapters.dft import TessentAdapter
        os.environ["HACOO_TESSENT_MOCK"] = "1"
        adapter = TessentAdapter()
        ins = adapter.api_dft_insert_scan(chains=16)
        atpg = adapter.api_dft_atpg(patterns=2000)
        cov = adapter.api_dft_report_coverage()
        self.assertEqual(adapter._session().commands, [
            "set_system_mode dft",
            "set_scan_configuration -chain_count 16",
            "insert_test_logic",
            "set_system_mode analysis",
            "set_pattern_limit 2000",
            "create_patterns",
            "report_statistics",
        ])
        self.assertEqual(ins["data"]["chains"], 16)
        self.assertEqual(atpg["data"]["atpg_runs"], 1)
        self.assertEqual(cov["data"]["test_coverage"], 97.42)
        self.assertEqual(cov["data"]["fault_coverage"], 96.85)
        self.assertEqual(cov["data"]["untestable"], 512.0)

    def test_dftmax_commands_and_coverage(self) -> None:
        from hacoo.adapters.dft import DftMaxAdapter
        os.environ["HACOO_DFTMAX_MOCK"] = "1"
        adapter = DftMaxAdapter()
        adapter.api_dft_insert_scan(chains=8)
        adapter.api_dft_atpg(patterns=1000)
        cov = adapter.api_dft_report_coverage()
        self.assertEqual(adapter._session().commands, [
            "set_scan_configuration -chain_count 8",
            "insert_dft",
            "create_test_patterns -pattern_count 1000",
            "report_test_coverage",
        ])
        self.assertEqual(cov["data"]["test_coverage"], 97.31)
        self.assertEqual(cov["data"]["fault_coverage"], 96.12)
        self.assertEqual(cov["data"]["untestable"], 423.0)

    def test_state_guards_and_validation(self) -> None:
        from hacoo.adapters.dft import DftMaxAdapter, TessentAdapter
        os.environ["HACOO_TESSENT_MOCK"] = "1"
        os.environ["HACOO_DFTMAX_MOCK"] = "1"
        for cls in (TessentAdapter, DftMaxAdapter):
            adapter = cls()
            with self.assertRaises(RuntimeError):
                adapter.api_dft_atpg()          # 未插 scan
            with self.assertRaises(RuntimeError):
                adapter.api_dft_report_coverage()  # 未 ATPG
            with self.assertRaises(ValueError):
                adapter.api_dft_insert_scan(chains=0)
            adapter.api_dft_insert_scan(chains=4)
            with self.assertRaises(ValueError):
                adapter.api_dft_atpg(patterns=0)

    def test_missing_binary_guidance(self) -> None:
        """未开 mock 且未安装工具时报错必须含 HACOO_<TOOL>_BIN 指引。"""
        from hacoo.adapters.dft import DftMaxAdapter, TessentAdapter
        for key in self._MOCK_ENV:
            os.environ.pop(key, None)
        with self.assertRaises(RuntimeError) as ctx:
            TessentAdapter().api_dft_insert_scan()
        self.assertIn("HACOO_TESSENT_BIN", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            DftMaxAdapter().api_dft_insert_scan()
        self.assertIn("HACOO_DFTMAX_BIN", str(ctx.exception))


class HacooMockSequenceTest(unittest.TestCase):
    """BE-3/BE-4 (SPEC-evolution C.3): MockTclShell 序列应答 + ICC2 mock 的
    迭代收敛建模。

    覆盖: (1) canned 值为列表时按命中次数依次返回, 耗尽钳制末值,
    字符串值保持静态 (向后兼容); (2) icc2 mock 下 route_opt 反复调用的
    drc_count 逐轮下降 (5->3->1->0, 钳制在 0); (3) fp 探索三方案的
    utilization/congestion 指标各异 (0.70->3, 0.80->6, 0.60->1)。
    """

    def setUp(self) -> None:
        self._saved_env = os.environ.get("HACOO_ICC2_MOCK")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("HACOO_ICC2_MOCK", None)
        else:
            os.environ["HACOO_ICC2_MOCK"] = self._saved_env

    # ---------------------------------------------------------- 序列应答原语

    def test_mock_shell_static_canned_backward_compatible(self) -> None:
        from hacoo.adapters.shell import MockTclShell
        shell = MockTclShell("t", canned={"report_qor": "WNS: 0.0\n"})
        # 字符串 canned: 多次调用恒定返回 (BE-1 以来的既有行为)
        self.assertEqual(shell.cmd("report_qor -summary"), "WNS: 0.0\n")
        self.assertEqual(shell.cmd("report_qor -summary"), "WNS: 0.0\n")
        # 未命中 canned: 缺省回显
        self.assertIn("[mock:t] ok", shell.cmd("some_other_cmd"))
        self.assertEqual(shell.commands,
                         ["report_qor -summary", "report_qor -summary",
                          "some_other_cmd"])

    def test_mock_shell_sequence_canned(self) -> None:
        from hacoo.adapters.shell import MockTclShell
        shell = MockTclShell("t", canned={
            "report_design": ["total=5\n", "total=3\n", "total=0\n"],
            "report_qor": "static\n"})
        # 按命中次数依次返回
        self.assertEqual(shell.cmd("report_design -drc"), "total=5\n")
        self.assertEqual(shell.cmd("report_design -drc"), "total=3\n")
        self.assertEqual(shell.cmd("report_design -drc"), "total=0\n")
        # 耗尽后钳制在末值 (建模"收敛")
        self.assertEqual(shell.cmd("report_design -drc"), "total=0\n")
        # 计数按 key 独立: 其他 key 不受影响
        self.assertEqual(shell.cmd("report_qor"), "static\n")

    # ---------------------------------------------------------- ICC2 mock 收敛建模

    def _mock_icc2_adapter(self) -> Any:
        os.environ["HACOO_ICC2_MOCK"] = "1"
        from hacoo.adapters.icc2 import ICC2Adapter
        adapter = ICC2Adapter()
        adapter.api_load_design(netlist="chip.v", top="chip", lib_name="ndm_lib",
                                tech_tf="tech.tf", ndm_libs=["ref.ndm"])
        return adapter

    def test_icc2_mock_drc_descends_per_round(self) -> None:
        """BE-4 验收锚点: mock 下 DRC 数逐轮下降 (5->3->1->0), 收敛后钳制。"""
        adapter = self._mock_icc2_adapter()
        counts = [adapter.api_route_opt()["data"]["drc_count"] for _ in range(5)]
        self.assertEqual(counts, [5, 3, 1, 0, 0])
        # 逐轮严格下降至阈值
        for prev, cur in zip(counts, counts[1:]):
            self.assertLessEqual(cur, prev)

    def test_icc2_mock_fp_exploration_scenarios(self) -> None:
        """BE-3 验收锚点: 三组 utilization 方案试跑, 指标各异可比。"""
        adapter = self._mock_icc2_adapter()
        table = []
        for util in (0.7, 0.8, 0.6):
            fp = adapter.api_fp_create(utilization=util)
            place = adapter.api_place_opt()
            table.append((fp["data"]["utilization"],
                          place["data"]["hotspot_count"]))
        self.assertEqual(table, [(0.7, 3), (0.8, 6), (0.6, 1)])
        # >=2 方案对比成立: congestion 随 utilization 单调 (mock 建模)
        self.assertEqual(len({row[1] for row in table}), 3)


class HacooDashboardDataTest(unittest.TestCase):
    """Dashboard 数据层 (SPEC-dashboard §4/§8): 在线模式 (start_test_server 沙箱)。

    覆盖全部 dd_* 函数 + 新增只读方法 api_runs_list (SPEC §5)。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.audit_path = os.path.join(cls._tmp.name, "logs", "hacoo-audit.jsonl")
        os.makedirs(os.path.dirname(cls.audit_path), exist_ok=True)
        cls.server, cls.thread = start_test_server(audit_path=cls.audit_path)
        cls.client = HacooClient(cls.server.address, token="dev-token")
        cls._seed()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    @classmethod
    def _seed(cls) -> None:
        c = cls.client
        # 一个成功 run (sta_only: load_design + sta_report, 产生决策与观测)
        run = c.flow_start("sta_only", {"design_path": "chip.v", "top": "chip"})
        cls.run_id = run["run_id"]
        assert run["status"] == "success"
        # 一条 pending 决策 + 一条绑定反馈 (human_correction, weight=5)
        cls.pending_dec = c.decision_record(
            agent="sta", stage="sta_report", action="api_report_timing",
            rationale="待人工复核的决策", run_id=cls.run_id)
        c.feedback(agent="sta", content="dashboard 测试: 先查 clock skew",
                   decision_id=cls.pending_dec, tags=["clock"])
        # 一条 agent 自我沉淀 (unreviewed)
        c.knowledge_add(agent="apr", symptom="dashboard 测试 congestion 经验",
                        resolution="降 utilization", tags=["congestion"])
        # 知识盲区: 同 keyword 两次零命中 -> scan 产生 blind_spot
        for _ in range(2):
            c.knowledge_query("pv", keyword="dashboard测试盲区主题-qaz")
        cls.scan_pv = c.insights_scan(agent="pv")
        # 自我问题: 窗口内 2 条 human_correction -> scan 产生 self_issue
        c.feedback(agent="sipi", content="dashboard 测试纠正1: 查 PDN 阻抗")
        c.feedback(agent="sipi", content="dashboard 测试纠正2: decap 策略")
        cls.scan_sipi = c.insights_scan(agent="sipi")
        # 升级提案 (proposed)
        cls.proposal = c.upgrade_propose(
            title="dashboard 测试提案", kind="agent_capability", target="sta",
            motivation="验证 P07 看板", acceptance="bench case 通过")
        # 一条失败观测 (产生 error_signature)
        iid = c.spawn("mock_eda")
        try:
            with cls.assertRaises(cls, HacooClientError):
                c.call("api_report_timing", instance_id=iid, agent="sta")
        finally:
            c.release(iid)

    # ---------------------------------------------------------- §4 清单

    def test_dd_agents_status(self) -> None:
        from hacoo import dashboard_data as dd
        agents = dd.dd_agents_status(client=self.client)
        self.assertEqual(len(agents), 9)
        sta = [a for a in agents if a["name"] == "sta"][0]
        self.assertIn("enabled", sta)
        self.assertIn("version", sta)
        self.assertEqual(sta["stage"], "signoff")

    def test_runs_list_api(self) -> None:
        """api_runs_list (SPEC §5): 注册/分发/SDK 三处一致。"""
        names = [m["name"] for m in self.client.list_methods()]
        self.assertIn("api_runs_list", names)
        runs = self.client.runs_list(limit=10)
        self.assertTrue(any(r["run_id"] == self.run_id for r in runs))
        entry = [r for r in runs if r["run_id"] == self.run_id][0]
        self.assertEqual(entry["template"], "sta_only")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["n_stages"], 2)
        self.assertIsNotNone(entry["duration_s"])
        # status 过滤
        failed = self.client.runs_list(status="failed")
        self.assertFalse(any(r["run_id"] == self.run_id for r in failed))

    def test_dd_recent_runs_and_trace(self) -> None:
        from hacoo import dashboard_data as dd
        runs = dd.dd_recent_runs(client=self.client, limit=10)
        self.assertTrue(any(r["run_id"] == self.run_id for r in runs))
        trace = dd.dd_run_trace(client=self.client, run_id=self.run_id)
        self.assertEqual(trace["run"]["status"], "success")
        self.assertTrue(len(trace["decisions"]) >= 2)
        self.assertTrue(trace["audit"])  # 本类沙箱开启了审计

    def test_dd_todo_counts(self) -> None:
        from hacoo import dashboard_data as dd
        todos = dd.dd_todo_counts(client=self.client)
        self.assertGreaterEqual(todos["pending_decisions"], 1)
        self.assertGreaterEqual(todos["new_insights"], 2)   # blind_spot + self_issue
        self.assertGreaterEqual(todos["proposed_upgrades"], 1)

    def test_dd_metric_trend(self) -> None:
        from hacoo import dashboard_data as dd
        points = dd.dd_metric_trend(client=self.client, agent="sta",
                                    metric_key="wns")
        self.assertTrue(points)
        for p in points:
            self.assertIn("ts", p)
            self.assertIn("value", p)
            self.assertIn("run_id", p)
        self.assertEqual(points, sorted(points, key=lambda p: p["ts"]))

    def test_dd_review_queue(self) -> None:
        from hacoo import dashboard_data as dd
        items = dd.dd_review_queue(client=self.client)
        decisions = [i for i in items if i["item_type"] == "decision"]
        knowledge = [i for i in items if i["item_type"] == "knowledge"]
        self.assertTrue(any(d["decision_id"] == self.pending_dec
                            for d in decisions))
        self.assertTrue(any("dashboard 测试 congestion" in e["symptom"]
                            for e in knowledge))
        # 人类纠正也进复核队列 (SPEC-dashboard §9 闭环: 反馈 -> P04 -> 批准)
        self.assertTrue(any(e.get("source") == "human_correction"
                            and "clock skew" in e["symptom"] for e in knowledge))
        # agent 过滤
        only_sta = dd.dd_review_queue(client=self.client, agent="sta")
        self.assertTrue(all(i.get("agent") == "sta" for i in only_sta))

    def test_dd_knowledge_search(self) -> None:
        from hacoo import dashboard_data as dd
        hits = dd.dd_knowledge_search(client=self.client, agent="sta",
                                      keyword="clock skew")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["source"], "human_correction")
        self.assertEqual(hits[0]["weight"], 5)

    def test_dd_insights(self) -> None:
        from hacoo import dashboard_data as dd
        all_new = dd.dd_insights(client=self.client, status="new")
        self.assertTrue(any(i["kind"] == "blind_spot" for i in all_new))
        self.assertTrue(any(i["kind"] == "self_issue" for i in all_new))
        # self_issue 置顶 (SPEC §2 P06)
        self.assertEqual(all_new[0]["kind"], "self_issue")
        blind = dd.dd_insights(client=self.client, agent="pv", kind="blind_spot")
        self.assertEqual(len(blind), 1)

    def test_dd_upgrades_by_status(self) -> None:
        from hacoo import dashboard_data as dd
        board = dd.dd_upgrades_by_status(client=self.client)
        for status in ("proposed", "accepted", "in_progress", "done", "rejected"):
            self.assertIn(status, board)
        self.assertTrue(any(p["proposal_id"] == self.proposal["proposal_id"]
                            for p in board["proposed"]))

    def test_dd_agent_stats(self) -> None:
        from hacoo import dashboard_data as dd
        stats = dd.dd_agent_stats(client=self.client, agent="sta", days=30)
        self.assertGreaterEqual(stats["decisions_ok"], 2)  # flow 两个 stage
        self.assertEqual(stats["success_rate"], 1.0)       # 无失败决策
        self.assertGreaterEqual(stats["decisions_pending"], 1)
        self.assertGreaterEqual(stats["knowledge_count"], 1)
        self.assertGreaterEqual(stats["corrections"], 1)
        # 无闭环决策的 agent: success_rate 为 None
        empty = dd.dd_agent_stats(client=self.client, agent="thermal", days=30)
        self.assertIsNone(empty["success_rate"])

    def test_dd_observations_and_signatures(self) -> None:
        from hacoo import dashboard_data as dd
        obs = dd.dd_observations(client=self.client, agent="sta", limit=200)
        self.assertTrue(obs)
        self.assertTrue(any(o["run_id"] == self.run_id for o in obs))
        # tool 过滤
        obs_mock = dd.dd_observations(client=self.client, tool="mock_eda")
        self.assertTrue(all(o["tool"] == "mock_eda" for o in obs_mock))
        # 失败签名聚类: 种子里的失败调用被聚合
        sigs = dd.dd_error_signatures(client=self.client, agent="sta")
        self.assertTrue(sigs)
        self.assertGreaterEqual(sigs[0]["count"], 1)
        self.assertIn("sta", sigs[0]["agents"])

    def test_dd_audit_tail(self) -> None:
        from hacoo import dashboard_data as dd
        root = self._tmp.name  # logs/hacoo-audit.jsonl 位于其下
        entries = dd.dd_audit_tail(offline_dir=root, limit=50)
        self.assertTrue(entries)
        by_run = dd.dd_audit_tail(offline_dir=root, run_id=self.run_id)
        self.assertTrue(all(e["run_id"] == self.run_id for e in by_run))

    def test_dd_requires_exactly_one_source(self) -> None:
        from hacoo import dashboard_data as dd
        with self.assertRaises(ValueError):
            dd.dd_agents_status()
        with self.assertRaises(ValueError):
            dd.dd_agents_status(client=self.client, offline_dir="x")


class HacooDashboardOfflineTest(unittest.TestCase):
    """Dashboard 数据层离线分支 (SPEC-dashboard §6/§8):
    构造临时 JSONL 目录, 断言相同函数离线返回一致结构 (纯只读)。"""

    @classmethod
    def setUpClass(cls) -> None:
        import hacoo.dashboard_data as dd
        cls.dd = dd
        cls.root = tempfile.mkdtemp(prefix="hacoo-dash-offline-")
        now = time.time()
        # agents 配置
        cls._write("hacoo.agents.json",
                   {"enabled": ["sta", "flow"], "versions": {"sta": "2.0.0"}},
                   jsonl=False)
        # run manifests
        cls._write("runs/run-a.json", {
            "run_id": "run-a", "template": "sta_only", "status": "success",
            "user": "dev", "started_at": now - 100, "finished_at": now - 95,
            "stages": [{"name": "load_design", "agent": "sta", "status": "ok"},
                       {"name": "sta_report", "agent": "sta", "status": "ok"}]},
            jsonl=False)
        cls._write("runs/run-b.json", {
            "run_id": "run-b", "template": "block_signoff", "status": "failed",
            "user": "dev", "started_at": now - 50, "finished_at": now - 48,
            "stages": [{"name": "load_design", "agent": "synthesis",
                        "status": "failed"}], "error": "boom"}, jsonl=False)
        # decisions: 1 pending + 1 ok (都属于 sta / run-a)
        cls._write("logs/decisions.jsonl", [
            {"decision_id": "dec-1", "ts": now - 90, "agent": "sta",
             "run_id": "run-a", "stage": "s", "action": "a",
             "rationale": "r", "status": "pending", "metrics": {}, "note": ""},
            {"decision_id": "dec-2", "ts": now - 80, "agent": "sta",
             "run_id": "run-a", "stage": "s", "action": "a",
             "rationale": "r", "status": "ok", "metrics": {"wns": -0.05},
             "note": ""}])
        # insights: blind_spot (较新) + self_issue (较旧, 但应置顶)
        cls._write("logs/insights.jsonl", [
            {"insight_id": "ins-1", "ts": now - 60, "agent": "sta",
             "kind": "self_issue", "title": "自我问题", "status": "new",
             "evidence": [], "confidence": 0.8},
            {"insight_id": "ins-2", "ts": now - 10, "agent": "sta",
             "kind": "blind_spot", "title": "盲区", "status": "new",
             "evidence": [], "confidence": 0.6}])
        # upgrades: 1 proposed
        cls._write("logs/upgrade-proposals.jsonl", [
            {"proposal_id": "up-1", "ts": now - 10, "title": "t",
             "kind": "agent_capability", "target": "sta", "motivation": "m",
             "acceptance": "a", "status": "proposed", "source": "human",
             "history": []}])
        # knowledge: human_correction (weight 5) + agent unreviewed
        cls._write("knowledge/sta.jsonl", [
            {"id": "kb-1", "ts": now - 20, "agent": "sta",
             "symptom": "clock skew 修正", "root_cause": "", "resolution": "r1",
             "tags": ["clock"], "source": "human_correction", "weight": 5,
             "reuse_count": 3, "review_status": "unreviewed",
             "agent_version": "2.0.0"},
            {"id": "kb-2", "ts": now - 15, "agent": "sta",
             "symptom": "cts 经验", "root_cause": "", "resolution": "r2",
             "tags": ["cts"], "source": "agent", "weight": 1,
             "reuse_count": 0, "review_status": "unreviewed"}])
        # observations: 3 条 wns + 1 条失败带签名
        cls._write("observations/sta.jsonl", [
            {"obs_id": "obs-1", "ts": now - 30, "agent": "sta", "run_id": "run-a",
             "tool": "mock_eda", "method": "api_report_timing", "ok": True,
             "metrics": {"wns": -0.02}, "error_signatures": [], "excerpt": ""},
            {"obs_id": "obs-2", "ts": now - 25, "agent": "sta", "run_id": "run-b",
             "tool": "mock_eda", "method": "api_report_timing", "ok": True,
             "metrics": {"wns": -0.05}, "error_signatures": [], "excerpt": ""},
            {"obs_id": "obs-3", "ts": now - 5, "agent": "sta", "run_id": "run-b",
             "tool": "mock_eda", "method": "api_report_timing", "ok": False,
             "metrics": {}, "error_signatures": ["ERROR: boom"], "excerpt": ""}])
        # audit
        cls._write("logs/hacoo-audit.jsonl", [
            {"ts": now - 90, "request_id": "req-1", "user": "dev",
             "method": "api_load_design", "status": "ok", "run_id": "run-a",
             "agent": "sta", "elapsed_ms": 1.0}])
        # 记录目录快照, 测试结束后验证离线分支没有任何写入 (只读)
        cls._snapshot = cls._dir_snapshot()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._dir_snapshot() != cls._snapshot:
            raise AssertionError("offline mode must be strictly read-only")

    @classmethod
    def _write(cls, rel: str, payload, jsonl: bool = True) -> None:
        path = os.path.join(cls.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            if jsonl:
                for entry in payload:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            else:
                json.dump(payload, fh, ensure_ascii=False)

    @classmethod
    def _dir_snapshot(cls):
        snap = {}
        for base, _dirs, files in os.walk(cls.root):
            for fname in files:
                path = os.path.join(base, fname)
                with open(path, "rb") as fh:
                    snap[os.path.relpath(path, cls.root)] = fh.read()
        return snap

    def test_agents_status(self) -> None:
        agents = self.dd.dd_agents_status(offline_dir=self.root)
        by_name = {a["name"]: a for a in agents}
        self.assertTrue(by_name["sta"]["enabled"])
        self.assertEqual(by_name["sta"]["version"], "2.0.0")
        self.assertFalse(by_name["apr"]["enabled"])

    def test_recent_runs(self) -> None:
        runs = self.dd.dd_recent_runs(offline_dir=self.root, limit=10)
        self.assertEqual([r["run_id"] for r in runs], ["run-b", "run-a"])
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[1]["duration_s"], 5.0)
        ok_only = self.dd.dd_recent_runs(offline_dir=self.root,
                                         status="success")
        self.assertEqual([r["run_id"] for r in ok_only], ["run-a"])

    def test_todo_counts(self) -> None:
        todos = self.dd.dd_todo_counts(offline_dir=self.root)
        self.assertEqual(todos, {"pending_decisions": 1, "new_insights": 2,
                                 "proposed_upgrades": 1})

    def test_run_trace(self) -> None:
        trace = self.dd.dd_run_trace(offline_dir=self.root, run_id="run-a")
        self.assertEqual(trace["run"]["template"], "sta_only")
        self.assertEqual(len(trace["decisions"]), 2)
        self.assertEqual(len(trace["audit"]), 1)
        with self.assertRaises(KeyError):
            self.dd.dd_run_trace(offline_dir=self.root, run_id="run-nope")

    def test_metric_trend(self) -> None:
        points = self.dd.dd_metric_trend(offline_dir=self.root, agent="sta",
                                         metric_key="wns")
        self.assertEqual([p["value"] for p in points], [-0.02, -0.05])
        self.assertEqual([p["run_id"] for p in points], ["run-a", "run-b"])

    def test_review_queue(self) -> None:
        items = self.dd.dd_review_queue(offline_dir=self.root)
        # dec-1 (pending) + kb-1 (人类纠正待复核) + kb-2 (agent 未复核)
        self.assertEqual(len(items), 3)
        kinds = sorted(i["item_type"] for i in items)
        self.assertEqual(kinds, ["decision", "knowledge", "knowledge"])
        self.assertTrue(any(i.get("source") == "human_correction"
                            for i in items))

    def test_knowledge_search(self) -> None:
        hits = self.dd.dd_knowledge_search(offline_dir=self.root, agent="sta",
                                           keyword="clock")
        self.assertEqual([h["id"] for h in hits], ["kb-1"])
        # 离线查询不递增 reuse_count (只读语义; 与在线 KnowledgeStore.query 不同)
        hits2 = self.dd.dd_knowledge_search(offline_dir=self.root, agent="sta",
                                            keyword="clock")
        self.assertEqual(hits2[0]["reuse_count"], 3)

    def test_insights_self_issue_first(self) -> None:
        entries = self.dd.dd_insights(offline_dir=self.root, status="new")
        self.assertEqual([e["insight_id"] for e in entries], ["ins-1", "ins-2"])

    def test_upgrades_by_status(self) -> None:
        board = self.dd.dd_upgrades_by_status(offline_dir=self.root)
        self.assertEqual([p["proposal_id"] for p in board["proposed"]], ["up-1"])
        self.assertEqual(board["done"], [])

    def test_agent_stats(self) -> None:
        stats = self.dd.dd_agent_stats(offline_dir=self.root, agent="sta",
                                       days=30)
        self.assertEqual(stats["decisions_ok"], 1)
        self.assertEqual(stats["decisions_pending"], 1)
        self.assertEqual(stats["success_rate"], 1.0)
        self.assertEqual(stats["knowledge_count"], 2)
        self.assertEqual(stats["corrections"], 1)

    def test_observations_and_signatures(self) -> None:
        obs = self.dd.dd_observations(offline_dir=self.root, agent="sta")
        self.assertEqual(len(obs), 3)
        failed_only = self.dd.dd_observations(offline_dir=self.root,
                                              run_id="run-b")
        self.assertEqual(len(failed_only), 2)
        sigs = self.dd.dd_error_signatures(offline_dir=self.root)
        self.assertEqual(sigs[0]["signature"], "ERROR: boom")
        self.assertEqual(sigs[0]["count"], 1)

    def test_audit_tail(self) -> None:
        entries = self.dd.dd_audit_tail(offline_dir=self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["request_id"], "req-1")


class HacooDashboardUITest(unittest.TestCase):
    """Dashboard UI 冒烟 (SPEC-dashboard §8): streamlit 未安装时 skip。"""

    def test_dashboard_files_syntax(self) -> None:
        """全部 dashboard 文件可编译 (语法检查, 不依赖 streamlit, 3.8 兼容)。"""
        import glob as _glob
        files = [os.path.join("dashboard", "app.py"),
                 os.path.join("dashboard", "common.py")]
        files += sorted(_glob.glob(os.path.join("dashboard", "pages", "*.py")))
        self.assertEqual(len(files), 12)  # app + common + 10 pages
        for path in files:
            with open(path, "r", encoding="utf-8") as fh:
                compile(fh.read(), path, "exec")

    def test_dashboard_data_importable_without_streamlit(self) -> None:
        """数据层不含 streamlit import (核心层零第三方依赖)。"""
        import hacoo.dashboard_data  # noqa: F401 — 在 3.8 (无 streamlit) 上可导入
        with open(os.path.join("hacoo", "dashboard_data.py"), "r",
                  encoding="utf-8") as fh:
            self.assertNotIn("import streamlit", fh.read())

    def test_dashboard_app_runs_headless(self) -> None:
        """AppTest 无头跑通入口页 (无 server -> 离线只读模式, 不抛异常)。"""
        import importlib.util
        if importlib.util.find_spec("streamlit") is None:
            self.skipTest("streamlit 未安装 (Dashboard 为可选组件)")
        from streamlit.testing.v1 import AppTest
        app_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "dashboard", "app.py")
        at = AppTest.from_file(app_path, default_timeout=60)
        at.run()
        self.assertFalse(at.exception,
                         msg=str(at.exception[0].value) if at.exception else "")
        self.assertTrue(at.warning)  # 离线黄色横幅


class HacooSiAwareTest(unittest.TestCase):
    """SO-3 (SPEC-evolution): PrimeTime SI-aware 时序分析。

    验收: (1) si_enabled=True 时命令序列含 set_si_options (delta delay +
    noise) 且在 update_timing 之前; (2) 含 crosstalk delta/noise 分量行的
    报告 slack 提取兼容 (fixture 与 mock canned 双重验证); (3) 缺省
    si_enabled=False 行为不变 (无 set_si_options 命令)。
    """

    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")

    def setUp(self) -> None:
        self._saved_env = os.environ.get("HACOO_PRIMETIME_MOCK")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("HACOO_PRIMETIME_MOCK", None)
        else:
            os.environ["HACOO_PRIMETIME_MOCK"] = self._saved_env

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name), "r",
                  encoding="utf-8") as fh:
            return fh.read()

    def _mock_adapter(self):
        os.environ["HACOO_PRIMETIME_MOCK"] = "1"
        from hacoo.adapters.primetime import PrimeTimeAdapter
        adapter = PrimeTimeAdapter()
        adapter.api_load_design(path="chip.v", top="chip")
        return adapter

    def test_si_enabled_issues_si_options_before_update(self) -> None:
        adapter = self._mock_adapter()
        resp = adapter.api_report_timing(max_paths=5, si_enabled=True)
        commands = adapter._pt().commands
        idx_si = commands.index("set_si_options -delta_delay true -noise true")
        idx_update = commands.index("update_timing -full")
        self.assertLess(idx_si, idx_update)
        self.assertTrue(resp["data"]["si_enabled"])
        # SI-aware mock canned: 3 条 slack, delta/noise 行不影响提取
        self.assertEqual(resp["data"]["slacks"], [-0.143, -0.071, 0.022])
        self.assertEqual(resp["data"]["wns"], -0.143)
        self.assertEqual(resp["data"]["tns"], -0.214)
        self.assertEqual(resp["data"]["paths_analyzed"], 3)

    def test_si_disabled_default_unchanged(self) -> None:
        adapter = self._mock_adapter()
        resp = adapter.api_report_timing(max_paths=5)
        commands = adapter._pt().commands
        self.assertNotIn("set_si_options -delta_delay true -noise true",
                         commands)
        self.assertFalse(resp["data"]["si_enabled"])

    def test_parse_si_report_fixture_with_delta_components(self) -> None:
        """含 delta 分量的真实风格报告 (fixture, 数值与 mock canned 不同):
        slack 提取不受影响, wns/tns 正确。"""
        from hacoo.adapters.shell import MockTclShell
        adapter = self._mock_adapter()
        adapter._shell = MockTclShell(
            "primetime", canned={"report_timing": self._fixture("pt_report_si.txt")})
        resp = adapter.api_report_timing(max_paths=5, si_enabled=True)
        self.assertEqual(resp["data"]["slacks"], [-0.188, -0.045, 0.033])
        self.assertEqual(resp["data"]["wns"], -0.188)
        self.assertEqual(resp["data"]["tns"], -0.233)


class HacooMCMMTest(unittest.TestCase):
    """SO-1: PrimeTime MCMM 场景管理 —— 场景创建命令序列、跨场景报告解析、
    mock 模式经网关全链路。无 pt_shell 时走 HACOO_PRIMETIME_MOCK=1。"""

    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")

    def setUp(self) -> None:
        self._saved_env = os.environ.get("HACOO_PRIMETIME_MOCK")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("HACOO_PRIMETIME_MOCK", None)
        else:
            os.environ["HACOO_PRIMETIME_MOCK"] = self._saved_env

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name), "r",
                  encoding="utf-8") as fh:
            return fh.read()

    def _mock_adapter(self):
        os.environ["HACOO_PRIMETIME_MOCK"] = "1"
        from hacoo.adapters.primetime import PrimeTimeAdapter
        adapter = PrimeTimeAdapter()
        adapter.api_load_design(path="chip.v", top="chip")
        return adapter

    _SCENARIOS = [
        ("func.ss_0v99_125c", "func", "ss_0v99_125c", ["ss_0v99_125c.db"], "func.sdc"),
        ("func.ff_1v10_m40c", "func", "ff_1v10_m40c", ["ff_1v10_m40c.db"], "func.sdc"),
        ("test.ss_0v99_125c", "test", "ss_0v99_125c", ["ss_0v99_125c.db"], "test.sdc"),
        ("test.ff_1v10_m40c", "test", "ff_1v10_m40c", ["ff_1v10_m40c.db"], "test.sdc"),
    ]

    # ---------------------------------------------------------- 解析器

    def test_parse_mcmm_fixture(self) -> None:
        from hacoo.adapters.primetime import parse_mcmm_report
        parsed = parse_mcmm_report(self._fixture("pt_mcmm_report.txt"))
        self.assertEqual(len(parsed["scenarios"]), 4)
        func_ss = parsed["scenarios"]["func.ss_0v99_125c"]
        self.assertEqual(func_ss["wns"], -0.152)
        self.assertEqual(func_ss["tns"], -0.232)
        self.assertEqual(func_ss["paths"], 3)
        self.assertEqual(parsed["global_wns"], -0.210)
        self.assertEqual(parsed["worst_scenario"], "test.ss_0v99_125c")

    def test_parse_garbage_is_tolerant(self) -> None:
        from hacoo.adapters.primetime import parse_mcmm_report
        parsed = parse_mcmm_report("garbage\nno scenarios here\n")
        self.assertEqual(parsed["scenarios"], {})
        self.assertIsNone(parsed["global_wns"])
        self.assertIsNone(parsed["worst_scenario"])

    # ---------------------------------------------------------- adapter 行为

    def test_create_scenario_commands(self) -> None:
        adapter = self._mock_adapter()
        adapter.api_mcmm_create_scenario(
            name="func.ss_0v99_125c", mode="func", corner="ss_0v99_125c",
            lib_paths=["ss_0v99_125c.db"], sdc="func.sdc")
        cmds = adapter._shell.commands
        self.assertIn("create_scenario -name func.ss_0v99_125c -mode func "
                      "-corner ss_0v99_125c", cmds)
        self.assertIn("current_scenario func.ss_0v99_125c", cmds)
        self.assertTrue(any("link_library" in c and "ss_0v99_125c.db" in c
                            for c in cmds))
        self.assertIn("read_sdc {func.sdc}", cmds)
        self.assertIn("set_scenario_status func.ss_0v99_125c -active true", cmds)
        state = adapter.api_get_design_state()["data"]
        self.assertEqual(len(state["scenarios"]), 1)
        self.assertEqual(state["scenarios"][0]["mode"], "func")

    def test_duplicate_scenario_rejected(self) -> None:
        adapter = self._mock_adapter()
        adapter.api_mcmm_create_scenario(
            name="s1", mode="func", corner="ss", lib_paths=["a.db"])
        with self.assertRaises(RuntimeError) as ctx:
            adapter.api_mcmm_create_scenario(
                name="s1", mode="test", corner="ff", lib_paths=["b.db"])
        self.assertIn("already exists", str(ctx.exception))

    def test_report_all_requires_scenarios(self) -> None:
        adapter = self._mock_adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter.api_mcmm_report_all()
        self.assertIn("api_mcmm_create_scenario", str(ctx.exception))

    def test_report_all_parsing_mock(self) -> None:
        adapter = self._mock_adapter()
        for name, mode, corner, libs, sdc in self._SCENARIOS:
            adapter.api_mcmm_create_scenario(
                name=name, mode=mode, corner=corner, lib_paths=libs, sdc=sdc)
        out = adapter.api_mcmm_report_all(max_paths=5)["data"]
        self.assertEqual(len(out["scenarios"]), 4)
        self.assertEqual(out["global_wns"], -0.210)
        self.assertEqual(out["worst_scenario"], "test.ss_0v99_125c")
        self.assertEqual(out["wns"], -0.210)  # 扁平 key 供观测层提取

    # ---------------------------------------------------------- 经网关全链路

    def test_mcmm_through_gateway(self) -> None:
        """mock 模式经完整五层链路: spawn primetime -> 建 4 场景 -> 跨场景报告,
        且扁平 wns 进入观测层。"""
        os.environ["HACOO_PRIMETIME_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("primetime")
        try:
            client.call("api_load_design", instance_id=iid,
                        arguments={"path": "chip.v", "top": "chip"}, agent="sta")
            for name, mode, corner, libs, sdc in self._SCENARIOS:
                client.call("api_mcmm_create_scenario", instance_id=iid,
                            arguments={"name": name, "mode": mode, "corner": corner,
                                       "lib_paths": libs, "sdc": sdc}, agent="sta")
            data = client.call("api_mcmm_report_all", instance_id=iid,
                               arguments={"max_paths": 5}, agent="sta")["data"]
            self.assertEqual(len(data["scenarios"]), 4)
            self.assertEqual(data["global_wns"], -0.210)
            obs = client.call("api_observations_query",
                              arguments={"agent": "sta", "limit": 50})["data"]
            mcmm_obs = [o for o in obs
                        if o["method"] == "api_mcmm_report_all"]
            self.assertEqual(len(mcmm_obs), 1)
            self.assertEqual(mcmm_obs[0]["metrics"].get("wns"), -0.210)
        finally:
            client.release(iid)


class HacooCalibreTest(unittest.TestCase):
    """SO-4: Calibre LVS/PERC —— parse_rve / parse_lvs_report 纯函数、
    mock transcript 模式、经网关全链路。HACOO_CALIBRE_MOCK=1 驱动。"""

    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")

    def setUp(self) -> None:
        self._saved_env = os.environ.get("HACOO_CALIBRE_MOCK")

    def tearDown(self) -> None:
        if self._saved_env is None:
            os.environ.pop("HACOO_CALIBRE_MOCK", None)
        else:
            os.environ["HACOO_CALIBRE_MOCK"] = self._saved_env

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name), "r",
                  encoding="utf-8") as fh:
            return fh.read()

    # ---------------------------------------------------------- 解析器

    def test_parse_rve_fixture(self) -> None:
        from hacoo.adapters.calibre import parse_rve
        parsed = parse_rve(self._fixture("calibre_drc_results.txt"))
        self.assertEqual(parsed["total"], 19)
        by_rule = {v["rule"]: v["count"] for v in parsed["violations"]}
        self.assertEqual(by_rule, {"M1.S.2": 5, "VIA1.R.1": 2, "M2.W.1": 12})

    def test_parse_lvs_fixture(self) -> None:
        from hacoo.adapters.calibre import parse_lvs_report
        parsed = parse_lvs_report(self._fixture("calibre_lvs_report.txt"))
        self.assertEqual(parsed["result"], "INCORRECT")
        self.assertEqual(parsed["shorts"], 2)
        self.assertEqual(parsed["opens"], 1)
        self.assertEqual(parsed["net_mismatch"], 3)
        self.assertFalse(parsed["clean"])

    def test_lvs_verdict_not_confused_by_incorrect(self) -> None:
        """回归: 'INCORRECT' 不得被匹配成 'CORRECT' (子串陷阱)。"""
        from hacoo.adapters.calibre import parse_lvs_report
        self.assertEqual(parse_lvs_report("RESULT: INCORRECT")["result"],
                         "INCORRECT")
        self.assertEqual(parse_lvs_report("RESULT: CORRECT")["result"],
                         "CORRECT")

    def test_parse_garbage_is_tolerant(self) -> None:
        from hacoo.adapters.calibre import parse_lvs_report, parse_rve
        self.assertEqual(parse_rve("garbage")["violations"], [])
        parsed = parse_lvs_report("garbage")
        self.assertIsNone(parsed["result"])
        self.assertEqual(parsed["shorts"], 0)

    # ---------------------------------------------------------- adapter 行为

    def test_mock_runs(self) -> None:
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wd = lambda name: os.path.join(tmp.name, name)  # noqa: E731
        os.environ["HACOO_CALIBRE_MOCK"] = "1"
        from hacoo.adapters.calibre import CalibreAdapter
        adapter = CalibreAdapter()
        drc = adapter.api_run_drc(runset="drc.rules", layout="chip.gds",
                                  top="chip", work_dir=wd("drc"))["data"]
        self.assertEqual(drc["total_errors"], 7)
        self.assertEqual(len(drc["violations"]), 2)
        lvs = adapter.api_run_lvs(runset="lvs.rules", layout="chip.gds",
                                  source="chip.sp", top="chip",
                                  work_dir=wd("lvs"))["data"]
        self.assertEqual(lvs["verdict"], "INCORRECT")
        self.assertEqual(lvs["shorts"], 2)
        perc = adapter.api_run_perc(runset="perc.rules", top="chip",
                                    perc_type="esd",
                                    work_dir=wd("perc"))["data"]
        self.assertEqual(perc["perc_type"], "esd")
        history = adapter.api_get_run_history()["data"]
        self.assertEqual(history["total"], 3)

    # ---------------------------------------------------------- 经网关全链路

    def test_lvs_through_gateway(self) -> None:
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HACOO_CALIBRE_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("calibre")
        try:
            data = client.call("api_run_lvs", instance_id=iid,
                               arguments={"runset": "lvs.rules",
                                          "layout": "chip.gds",
                                          "source": "chip.sp", "top": "chip",
                                          "work_dir": os.path.join(tmp.name,
                                                                   "lvs")},
                               agent="pv")["data"]
            self.assertEqual(data["verdict"], "INCORRECT")
            self.assertEqual(data["shorts"], 2)
            obs = client.call("api_observations_query",
                              arguments={"agent": "pv", "limit": 50})["data"]
            lvs_obs = [o for o in obs if o["method"] == "api_run_lvs"]
            self.assertEqual(len(lvs_obs), 1)
            self.assertEqual(lvs_obs[0]["metrics"].get("shorts"), 2)
        finally:
            client.release(iid)

    # ---------------------------------------------------------- SO-5: waiver 流程

    def test_waive_rejected_without_decision(self) -> None:
        """SO-5 硬性纪律: 无 decision_record 时网关拒绝 waive
        (DECISION_REQUIRED), waiver 不可匿名执行。"""
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HACOO_CALIBRE_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("calibre")
        try:
            client.call("api_run_drc", instance_id=iid, agent="pv",
                        arguments={"runset": "drc.rules", "layout": "chip.gds",
                                   "top": "chip",
                                   "work_dir": os.path.join(tmp.name, "drc")})
            with self.assertRaises(HacooClientError) as ctx:
                client.call("api_calibre_waive", instance_id=iid, agent="pv",
                            arguments={"rule": "M1.S.2", "bbox": "0,0,10,10",
                                       "justification": "foundry 确认的伪违例",
                                       "work_dir": os.path.join(tmp.name, "drc")})
            self.assertEqual(ctx.exception.code, "DECISION_REQUIRED")
        finally:
            client.release(iid)

    def test_waive_with_decision_classifies(self) -> None:
        """SO-5: 先 decision_record 后 waive -> waived/unwaived 分类正确,
        waiver 文件落盘, 指标进观测层。"""
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HACOO_CALIBRE_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("calibre")
        wd = os.path.join(tmp.name, "drc")
        try:
            client.call("api_run_drc", instance_id=iid, agent="pv",
                        arguments={"runset": "drc.rules", "layout": "chip.gds",
                                   "top": "chip", "work_dir": wd})
            client.decision_record(
                agent="pv", stage="pv_waiver", action="api_calibre_waive",
                rationale="M1.S.2 为 foundry 确认的伪违例 (密度填充单元边界)")
            resp = client.call("api_calibre_waive", instance_id=iid, agent="pv",
                               arguments={"rule": "M1.S.2", "bbox": "0,0,10,10",
                                          "justification": "foundry 确认的伪违例",
                                          "work_dir": wd})
            data = resp["data"]
            # mock transcript: M1.S.2=5, VIA1.R.1=2
            self.assertEqual(data["waived_count"], 5)
            self.assertEqual(data["unwaived_count"], 2)
            self.assertEqual([v["rule"] for v in data["waived"]], ["M1.S.2"])
            self.assertEqual([v["rule"] for v in data["unwaived"]], ["VIA1.R.1"])
            self.assertTrue(data["waiver_file"].endswith("waivers.jsonl"))
            self.assertTrue(os.path.isfile(data["waiver_file"]))
            obs = client.call("api_observations_query",
                              arguments={"agent": "pv", "limit": 50})["data"]
            waive_obs = [o for o in obs if o["method"] == "api_calibre_waive"]
            self.assertEqual(len(waive_obs), 1)
            self.assertEqual(waive_obs[0]["metrics"].get("waived_count"), 5)
            self.assertEqual(waive_obs[0]["metrics"].get("unwaived_count"), 2)
        finally:
            client.release(iid)

    def test_waive_guards_direct_adapter(self) -> None:
        """SO-5 adapter 层守卫: 无 DRC run 不可 waive; justification 必填。"""
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["HACOO_CALIBRE_MOCK"] = "1"
        from hacoo.adapters.calibre import CalibreAdapter
        adapter = CalibreAdapter()
        wd = os.path.join(tmp.name, "drc")
        with self.assertRaises(RuntimeError):
            adapter.api_calibre_waive(rule="M1.S.2", bbox="0,0,1,1",
                                      justification="x", work_dir=wd)
        adapter.api_run_drc(runset="drc.rules", layout="chip.gds",
                            top="chip", work_dir=wd)
        with self.assertRaises(ValueError):
            adapter.api_calibre_waive(rule="M1.S.2", bbox="0,0,1,1",
                                      justification="  ", work_dir=wd)


class HacooEmirHotspotTest(unittest.TestCase):
    """SO-7: EMIR hotspot 解析 —— RedHawk-SC/Voltus 双工具解析器、
    签名一致性、mock 模式经网关。HACOO_<TOOL>_MOCK=1 驱动。"""

    FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "fixtures")
    _MOCK_ENV = ("HACOO_REDHAWK_SC_MOCK", "HACOO_VOLTUS_MOCK")

    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._MOCK_ENV}

    def tearDown(self) -> None:
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.FIXTURES_DIR, name), "r",
                  encoding="utf-8") as fh:
            return fh.read()

    # ---------------------------------------------------------- 解析器

    def test_parse_redhawk_fixture(self) -> None:
        from hacoo.adapters.redhawk_sc import parse_ir_hotspots
        parsed = parse_ir_hotspots(self._fixture("rh_sc_ir_hotspots.txt"))
        self.assertEqual(len(parsed["hotspots"]), 4)
        first = parsed["hotspots"][0]
        self.assertEqual(first["instance"], "u_core/u_alu/mult_u1")
        self.assertEqual(first["x"], 120.5)
        self.assertEqual(first["layer"], "M5")
        self.assertEqual(parsed["worst_drop_mv"], 98.5)

    def test_parse_voltus_fixture(self) -> None:
        from hacoo.adapters.voltus import parse_ir_hotspots
        parsed = parse_ir_hotspots(self._fixture("voltus_ir_hotspots.txt"))
        self.assertEqual(len(parsed["hotspots"]), 3)
        self.assertEqual(parsed["hotspots"][0]["layer"], "met5")
        self.assertEqual(parsed["worst_drop_mv"], 98.5)

    def test_parse_top_n_truncation_and_garbage(self) -> None:
        from hacoo.adapters.redhawk_sc import parse_ir_hotspots
        parsed = parse_ir_hotspots(self._fixture("rh_sc_ir_hotspots.txt"),
                                   top_n=2)
        self.assertEqual(len(parsed["hotspots"]), 2)
        empty = parse_ir_hotspots("garbage\nnothing here\n")
        self.assertEqual(empty["hotspots"], [])
        self.assertIsNone(empty["worst_drop_mv"])

    def test_hotspot_method_signature_consistent(self) -> None:
        """两工具 api_emir_report_hotspots 签名一致 (BE-1 式跨工具约束)。"""
        import inspect
        from hacoo.adapters.redhawk_sc import RedHawkSCAdapter
        from hacoo.adapters.voltus import VoltusAdapter
        sigs = []
        for cls in (RedHawkSCAdapter, VoltusAdapter):
            params = [p for p in inspect.signature(
                cls.api_emir_report_hotspots).parameters if p != "self"]
            sigs.append(params)
        self.assertEqual(sigs[0], sigs[1])

    # ---------------------------------------------------------- 经网关全链路

    def test_hotspots_through_gateway(self) -> None:
        os.environ["HACOO_REDHAWK_SC_MOCK"] = "1"
        server, _thread = start_test_server()
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        iid = client.spawn("redhawk_sc")
        try:
            client.call("api_load_design", instance_id=iid,
                        arguments={"def_file": "chip.def", "top": "chip",
                                   "lef": ["tech.lef"], "libs": ["ss.lib"]},
                        agent="irem")
            client.call("api_run_ir_analysis", instance_id=iid,
                        arguments={"analysis_type": "static"}, agent="irem")
            data = client.call("api_emir_report_hotspots", instance_id=iid,
                               arguments={"top_n": 20}, agent="irem")["data"]
            self.assertEqual(len(data["hotspots"]), 4)
            self.assertEqual(data["worst_drop_mv"], 98.5)
            self.assertEqual(data["ir_drop_mv"], 98.5)
            obs = client.call("api_observations_query",
                              arguments={"agent": "irem", "limit": 50})["data"]
            hs_obs = [o for o in obs
                      if o["method"] == "api_emir_report_hotspots"]
            self.assertEqual(len(hs_obs), 1)
            self.assertEqual(hs_obs[0]["metrics"].get("ir_drop_mv"), 98.5)
        finally:
            client.release(iid)

    def test_hotspots_require_ir_analysis(self) -> None:
        os.environ["HACOO_REDHAWK_SC_MOCK"] = "1"
        from hacoo.adapters.redhawk_sc import RedHawkSCAdapter
        adapter = RedHawkSCAdapter()
        adapter.api_load_design(def_file="chip.def", top="chip",
                                lef=["tech.lef"], libs=["ss.lib"])
        with self.assertRaises(RuntimeError) as ctx:
            adapter.api_emir_report_hotspots()
        self.assertIn("api_run_ir_analysis", str(ctx.exception))


class HacooTrendDirectionTest(unittest.TestCase):
    """X-1: 趋势检测器方向表 —— 双向触发/不触发、未知 key 默认、
    e2e 种子观测经 api_insights_scan 命中 trend insight。"""

    @staticmethod
    def _obs(key: str, values: List[float]) -> List[Dict[str, Any]]:
        return [{"obs_id": "obs-%d" % i, "run_id": "run-%d" % i,
                 "ts": 1000.0 + i, "agent": "sta", "tool": "primetime",
                 "method": "m", "metrics": {key: v}, "excerpt": ""}
                for i, v in enumerate(values)]

    # ---------------------------------------------------------- 方向判定

    def test_lower_is_worse_triggers_on_decrease(self) -> None:
        from hacoo.insights import detect_trend
        hits = detect_trend("sta", self._obs("wns", [-0.02, -0.05, -0.11]))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "trend")
        self.assertIn("wns", hits[0]["title"])

    def test_lower_is_worse_no_trigger_on_increase(self) -> None:
        from hacoo.insights import detect_trend
        self.assertEqual(detect_trend("sta", self._obs("wns", [-0.11, -0.05, -0.02])), [])

    def test_higher_is_worse_triggers_on_increase(self) -> None:
        from hacoo.insights import detect_trend
        for key in ("drc_count", "ir_drop_mv", "hotspot_count", "total_power"):
            hits = detect_trend("apr", self._obs(key, [5.0, 9.0, 14.0]))
            self.assertEqual(len(hits), 1, "key=%s 应触发" % key)

    def test_higher_is_worse_no_trigger_on_decrease(self) -> None:
        from hacoo.insights import detect_trend
        self.assertEqual(detect_trend("apr", self._obs("drc_count", [14.0, 9.0, 5.0])), [])

    def test_unknown_key_defaults_higher_is_worse(self) -> None:
        from hacoo.insights import detect_trend
        hits = detect_trend("sta", self._obs("custom_metric", [1.0, 2.0, 3.0]))
        self.assertEqual(len(hits), 1)
        self.assertEqual(detect_trend("sta", self._obs("custom_metric", [3.0, 2.0, 1.0])), [])

    def test_fewer_than_three_points_no_trigger(self) -> None:
        from hacoo.insights import detect_trend
        self.assertEqual(detect_trend("sta", self._obs("wns", [-0.05, -0.11])), [])

    # ---------------------------------------------------------- e2e 种子命中

    def test_trend_insight_through_gateway(self) -> None:
        """种子观测 (ir_drop_mv 连续恶化) 写入沙箱 -> api_insights_scan 命中。
        ts 必须在扫描窗口内 (默认 7 天)。"""
        cfg = Config()
        server, _thread = start_test_server(config=cfg)
        self.addCleanup(server.stop)
        now = time.time()
        os.makedirs(cfg.observations_dir, exist_ok=True)
        with open(os.path.join(cfg.observations_dir, "irem.jsonl"), "w",
                  encoding="utf-8") as fh:
            for i, v in enumerate([70.0, 82.5, 98.5]):
                fh.write(json.dumps({
                    "obs_id": "obs-seed-%d" % i, "run_id": "run-seed-%d" % i,
                    "ts": now - (3 - i) * 86400.0,
                    "agent": "irem", "tool": "redhawk_sc",
                    "method": "api_emir_report_hotspots", "ok": True,
                    "metrics": {"ir_drop_mv": v}, "error_signatures": [],
                    "excerpt": ""}, ensure_ascii=False) + "\n")
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        created = client.call("api_insights_scan",
                              arguments={"agent": "irem"})["data"]
        trends = [i for i in created["created"] if i["kind"] == "trend"]
        self.assertEqual(len(trends), 1)
        self.assertIn("ir_drop_mv", trends[0]["title"])
        self.assertEqual(len(trends[0]["evidence"]), 3)


class HacooFlowFanoutTest(unittest.TestCase):
    """X-3 (SPEC-evolution): flow engine 并行 fan-out。

    验收: (1) timing_fanout 模板 2 路并行执行成功, 每分支独立实例
    (分支内自含 load_design), compare 产出 winner; (2) trace 完整 —
    fan-out 级决策 + 每分支决策 (params 带 _branch) + 审计含分支
    spawn/release; (3) 配额超限 -> run failed 且错误为 QUOTA_EXCEEDED,
    绝不静默排队; (4) 串行/loop 模板行为不变 (既有测试覆盖)。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.agents_file = os.path.join(cls._tmp.name, "hacoo.agents.json")
        with open(cls.agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta", "flow"]}, fh)
        cls.audit_path = os.path.join(cls._tmp.name, "audit.jsonl")
        cfg = Config(agents_file=cls.agents_file)
        cls.server, cls.thread = start_test_server(cfg, audit_path=cls.audit_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")

    def tearDown(self) -> None:
        self.client.close()

    def test_fanout_two_branches_parallel_and_compare(self) -> None:
        run = self.client.flow_start("timing_fanout",
                                     {"design_path": "chip.v", "top": "chip"})
        self.assertEqual(run["status"], "success", run.get("error"))
        self.assertEqual(len(run["stages"]), 1)
        entry = run["stages"][0]
        self.assertEqual(entry["status"], "ok")
        branches = entry["parallel"]["branches"]
        self.assertEqual(len(branches), 2)
        # 每分支: 自含 setup (load_design) + 主方法, 全部 ok, 决策带 _branch
        for i, br in enumerate(branches):
            self.assertEqual(br["params"]["max_paths"], [5, 20][i])
            self.assertEqual([s["name"] for s in br["stages"]],
                             ["load_design", "explore"])
            self.assertTrue(all(s["status"] == "ok" for s in br["stages"]))
            self.assertEqual(br["stages"][-1]["params"]["_branch"], i)
            self.assertIn("_branch_data", br["stages"][-1])
        # compare: wns 越大越好, winner 必选出且来自某一分支
        result = entry["parallel"]["compare_result"]
        self.assertEqual(result["pick"], "max")
        self.assertEqual(len(result["branches"]), 2)
        self.assertIsNotNone(result["winner"])
        win = result["winner"]["branch"]
        self.assertIn(win, (0, 1))
        wns_vals = [b["metrics"]["wns"] for b in result["branches"]]
        self.assertEqual(result["winner"]["metrics"]["wns"], max(wns_vals))

    def test_fanout_trace_complete(self) -> None:
        run = self.client.flow_start("timing_fanout",
                                     {"design_path": "chip.v", "top": "chip"})
        trace = self.client.flow_trace(run["run_id"])
        decisions = trace["decisions"]
        # fan-out 级决策 + 每分支 2 个 stage 决策 = 5
        fanout = [d for d in decisions if d["action"] == "fanout"]
        self.assertEqual(len(fanout), 1)
        self.assertEqual(len(fanout[0]["params"]["branches"]), 2)
        stage_decisions = [d for d in decisions if d["action"] != "fanout"]
        self.assertEqual(len(stage_decisions), 4)
        self.assertEqual({d["params"].get("_branch") for d in stage_decisions},
                         {0, 1})
        # 审计: X-4 起按工具懒 spawn, 纯 fan-out 模板只有 2 分支实例
        spawns = [a for a in trace["audit"] if a.get("method") == "api_spawn_instance"]
        releases = [a for a in trace["audit"]
                    if a.get("method") == "api_release_instance"]
        self.assertEqual(len(spawns), 2)
        self.assertEqual(len(releases), 2)

    def test_fanout_quota_exceeded_fails_fast(self) -> None:
        # 配额 1: 2 路 fan-out 需 2 席 (X-4 起按工具懒 spawn, 预检时 used=0)
        # -> 1 > 0? 0+2 > 1 -> QUOTA_EXCEEDED, 绝不静默排队
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agents_file = os.path.join(tmp.name, "hacoo.agents.json")
        with open(agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta", "flow"]}, fh)
        cfg = Config(agents_file=agents_file, max_instances_per_user=1)
        server, _thread = start_test_server(cfg)
        self.addCleanup(server.stop)
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        run = client.flow_start("timing_fanout",
                                {"design_path": "chip.v", "top": "chip"})
        self.assertEqual(run["status"], "failed")
        self.assertIn("QUOTA_EXCEEDED", run["error"])
        entry = run["stages"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("QUOTA_EXCEEDED", entry["error"])
        # 失败快速发生: 任一分支 stage 都未执行
        self.assertTrue(all(not br["stages"]
                            for br in entry["parallel"]["branches"]))
        # 决策闭环: fanout 决策 outcome=failed
        trace = client.flow_trace(run["run_id"])
        fanout = [d for d in trace["decisions"] if d["action"] == "fanout"]
        self.assertEqual(len(fanout), 1)
        self.assertEqual(fanout[0]["status"], "failed")


class HacooRunVersionTest(unittest.TestCase):
    """X-4 (SPEC-evolution): run <-> 数据版本双向追踪。

    验收: (1) flow_start params 的 data_version/design_version 写入 run
    manifest, trace 可见; (2) isf_flow_submit 模板: fw_start 把 run_id
    挂进 ISF record (反向), 返回的 flow_id 被引擎收集进 manifest
    isf_flow_ids (正向); (3) engine 按方法反查 tool 懒 spawn, isf
    stage 可在 flow 中运行。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.agents_file = os.path.join(cls._tmp.name, "hacoo.agents.json")
        with open(cls.agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["sta", "flow"]}, fh)
        cls.audit_path = os.path.join(cls._tmp.name, "audit.jsonl")
        cfg = Config(agents_file=cls.agents_file)
        cls.server, cls.thread = start_test_server(cfg, audit_path=cls.audit_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.server.gateway.audit.close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.client = HacooClient(self.server.address, token="dev-token")
        self._prev_mock = os.environ.get("HACOO_ISF_MOCK")
        os.environ["HACOO_ISF_MOCK"] = "1"  # 无 ISF 环境, 强制 mock 保证确定性

    def tearDown(self) -> None:
        self.client.close()
        if self._prev_mock is None:
            os.environ.pop("HACOO_ISF_MOCK", None)
        else:
            os.environ["HACOO_ISF_MOCK"] = self._prev_mock

    def test_manifest_records_data_versions(self) -> None:
        run = self.client.flow_start(
            "sta_only", {"design_path": "chip.v", "top": "chip",
                         "data_version": "20260920R",
                         "design_version": "20260711"})
        self.assertEqual(run["status"], "success", run.get("error"))
        self.assertEqual(run["data_version"], "20260920R")
        self.assertEqual(run["design_version"], "20260711")
        self.assertEqual(run["isf_flow_ids"], [])
        # trace 同样可见版本字段 (复盘入口)
        trace = self.client.flow_trace(run["run_id"])
        self.assertEqual(trace["run"]["data_version"], "20260920R")
        self.assertEqual(trace["run"]["design_version"], "20260711")

    def test_versions_default_none_when_not_given(self) -> None:
        run = self.client.flow_start("sta_only",
                                     {"design_path": "chip.v", "top": "chip"})
        self.assertEqual(run["status"], "success", run.get("error"))
        self.assertIsNone(run["data_version"])
        self.assertIsNone(run["design_version"])

    def test_isf_bidirectional_reference(self) -> None:
        run = self.client.flow_start(
            "isf_flow_submit",
            {"project": "N3CTV1", "flow": "APR+TC+PV", "block": "BLOCK1",
             "flow_path": "/FLOW/PATH/NAME", "usertcl": "user.tcl",
             "data_version": "20260920R", "design_version": "20260711"})
        self.assertEqual(run["status"], "success", run.get("error"))
        self.assertEqual([s["name"] for s in run["stages"]],
                         ["isf_setup", "fw_start", "flows_check"])
        self.assertEqual([s["status"] for s in run["stages"]], ["ok"] * 3)
        # manifest 版本字段
        self.assertEqual(run["data_version"], "20260920R")
        self.assertEqual(run["design_version"], "20260711")
        # 正向: manifest 收集 isf flow_id
        fw_stage = run["stages"][1]
        flow_id = fw_stage["result_data"]["flow_id"]
        self.assertTrue(flow_id.startswith("isf-"))
        self.assertEqual(run["isf_flow_ids"], [flow_id])
        # 反向: ISF record 回带 run_id (经 flows_check stage 的实时快照)
        flows = run["stages"][2]["result_data"]["flows"]
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["flow_id"], flow_id)
        self.assertEqual(flows[0]["run_id"], run["run_id"])
        self.assertEqual(flows[0]["data_version"], "20260920R")
        # run_id 注入模板参数 (${run_id} 可渲染)
        self.assertEqual(run["params"]["run_id"], run["run_id"])


class HacooCrossDomainTest(unittest.TestCase):
    """X-5 (SPEC-evolution): 跨域关联 insight 检测器 (thermal x irem)。

    验收: (1) 纯函数: 同网格 (容差 5um) 重叠触发, 不同网格不触发;
    (2) 观测层: hotspot 坐标随观测落盘 (extract_hotspots);
    (3) 网关全链路: 种子双侧观测 -> api_insights_scan 为 thermal/irem
    各生成一条 cross_domain insight, evidence 双侧引用; 重扫去重。
    """

    # ---------------------------------------------------------- 纯函数

    def test_extract_hotspots_filters(self) -> None:
        from hacoo.observations import extract_hotspots
        data = {"hotspots": [
            {"instance": "u1", "x": 100.0, "y": 200.0, "ir_drop_mv": 98.5},
            {"instance": "bad", "x": "n/a", "y": 1.0},   # 非数值 x -> 丢弃
            {"instance": "bad2", "x": True, "y": 2.0},   # bool -> 丢弃
            "garbage"]}
        out = extract_hotspots(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["instance"], "u1")
        self.assertEqual(extract_hotspots({"other": 1}), [])
        self.assertEqual(extract_hotspots("nondict"), [])

    def test_detector_overlap_and_non_overlap(self) -> None:
        from hacoo.insights import detect_cross_domain
        thermal = [{"obs_id": "obs-t1", "run_id": "run-t",
                    "hotspots": [{"instance": "u_th", "x": 100.0, "y": 200.0,
                                  "temp_c": 105.2}]}]
        # 同网格: (103, 204) 与 (100, 200) 在 5um 网格下同属 (20, 40)
        irem_near = [{"obs_id": "obs-i1", "run_id": "run-i",
                      "hotspots": [{"instance": "u_ir", "x": 103.0, "y": 204.0,
                                    "ir_drop_mv": 98.5}]}]
        cands = detect_cross_domain({"thermal": thermal, "irem": irem_near})
        self.assertEqual(len(cands), 2)  # 双侧各一条
        self.assertEqual({c["agent"] for c in cands}, {"thermal", "irem"})
        self.assertTrue(all(c["kind"] == "cross_domain" for c in cands))
        self.assertTrue(all(len(c["evidence"]) == 2 for c in cands))
        self.assertEqual({e["ref"] for c in cands for e in c["evidence"]},
                         {"obs-t1", "obs-i1"})  # 双侧引用
        # 签名含容差取整坐标网格 (经 sha1 摘要): 双侧同 digest, 重跑稳定
        digest = cands[0]["signature"].rsplit(":", 1)[-1]
        self.assertTrue(all(c["signature"].endswith(digest) for c in cands))
        cands2 = detect_cross_domain({"thermal": thermal, "irem": irem_near})
        self.assertEqual([c["signature"] for c in cands2],
                         [c["signature"] for c in cands])
        # 不同网格: (110, 200) -> (22, 40), 不触发
        irem_far = [{"obs_id": "obs-i2", "run_id": "run-i",
                     "hotspots": [{"instance": "u_ir2", "x": 110.0, "y": 200.0,
                                   "ir_drop_mv": 88.0}]}]
        self.assertEqual(
            detect_cross_domain({"thermal": thermal, "irem": irem_far}), [])

    # ---------------------------------------------------------- 网关全链路

    def test_cross_domain_through_gateway(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agents_file = os.path.join(tmp.name, "hacoo.agents.json")
        with open(agents_file, "w", encoding="utf-8") as fh:
            json.dump({"enabled": ["thermal", "irem", "flow"]}, fh)
        cfg = Config(agents_file=agents_file)
        server, _thread = start_test_server(cfg)
        self.addCleanup(server.stop)
        # 种子双侧观测 (同网格热点)
        now = time.time()
        seeds = {
            "thermal": {"instance": "u_th", "x": 100.0, "y": 200.0,
                        "temp_c": 105.2},
            "irem": {"instance": "u_ir", "x": 103.0, "y": 204.0,
                     "ir_drop_mv": 98.5},
        }
        for agent, hs in seeds.items():
            path = os.path.join(cfg.observations_dir, "%s.jsonl" % agent)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "obs_id": "obs-x-%s" % agent, "run_id": "run-x",
                    "ts": now, "agent": agent, "tool": "voltus",
                    "method": "api_emir_report_hotspots", "ok": True,
                    "metrics": {}, "error_signatures": [],
                    "excerpt": "", "hotspots": [hs]},
                    ensure_ascii=False) + "\n")
        client = HacooClient(server.address, token="dev-token")
        self.addCleanup(client.close)
        created = client.call("api_insights_scan", arguments={})["data"]
        cross = [i for i in created["created"] if i["kind"] == "cross_domain"]
        self.assertEqual({i["agent"] for i in cross}, {"thermal", "irem"})
        for ins in cross:
            self.assertEqual(len(ins["evidence"]), 2)
            self.assertEqual({e["ref"] for e in ins["evidence"]},
                             {"obs-x-thermal", "obs-x-irem"})
            self.assertIn("联合会诊", ins["proposed_action"])
        # 去重稳定: 重扫同签名 (含坐标网格) 不再生成
        again = client.call("api_insights_scan", arguments={})["data"]
        self.assertEqual(
            [i for i in again["created"] if i["kind"] == "cross_domain"], [])
        self.assertGreaterEqual(again["skipped"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
