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
                self.client.call("api_isf_submit_flow", instance_id=iid,
                                 arguments={"flow_name": "f", "stages": []}, agent="sta")
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
        iid = self.client.spawn("isf")
        try:
            flow = self.client.call("api_isf_submit_flow", instance_id=iid,
                                    arguments={"flow_name": "nightly",
                                               "stages": ["syn", "apr", "sta"]},
                                    agent="flow")["data"]
            time.sleep(2.2)
            status = self.client.call("api_isf_flow_status", instance_id=iid,
                                      arguments={"flow_id": flow["flow_id"]},
                                      agent="flow")["data"]
            self.assertEqual(status["status"], "success")
            self.client.call("api_isf_db_save", instance_id=iid,
                             arguments={"key": "chip/pnr", "data": {"wns": -0.05}},
                             agent="flow")
            db = self.client.call("api_isf_db_query", instance_id=iid,
                                  arguments={"key": "chip/pnr"}, agent="flow")["data"]
            self.assertEqual(db["version"], 1)
            cicd = self.client.call("api_isf_cicd_trigger", instance_id=iid,
                                    arguments={"pipeline": "regression"},
                                    agent="flow")["data"]
            self.assertEqual(cicd["run_no"], 1)
            dags = self.client.call("api_isf_airflow_dags", instance_id=iid,
                                    agent="flow")["data"]
            self.assertEqual(dags["total"], 1)
        finally:
            self.client.release(iid)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
