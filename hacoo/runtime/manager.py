"""运行时管理层 (R1-R11): 后端实例全生命周期管理。

  阶段1 创建与绑定: R1 进程启动 / R2 端口分配 / R3 就绪检查 / R4 instance_id 绑定
  阶段2 会话维护:   R5 请求路由 / R6 状态保持 / R7 心跳监控 / R8 长任务(网关线程池配合)
  阶段3 实例回收:   R9 超时控制 / R10 故障处理 / R11 显式释放

多用户与安全:
  - Instance 记录 owner (Principal.user), 网关负责归属校验
  - 同一实例上的调用经 per-instance 锁串行化, 保护 adapter 持久状态 (R6)
  - spawn 前网关按 owner 统计配额 (max_instances_per_user)
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hacoo.comm import protocol
from hacoo.config import Config

log = logging.getLogger("hacoo.runtime")


class InstanceError(Exception):
    pass


@dataclass
class Instance:
    """R4: 进程与唯一 instance_id 的绑定记录。"""

    instance_id: str
    tool: str
    port: int
    proc: subprocess.Popen
    owner: str = "unknown"
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    restarted: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # lock: 同一实例上的调用串行化, 保证多用户/异步任务下状态一致 (R6)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "tool": self.tool,
            "port": self.port,
            "pid": self.proc.pid if self.proc else None,
            "owner": self.owner,
            "alive": self.proc is not None and self.proc.poll() is None,
            "uptime_s": round(time.time() - self.created_at, 1),
            "idle_s": round(time.time() - self.last_used_at, 1),
            "restarted": self.restarted,
        }


def _free_port() -> int:
    """R2: 为新实例分配通信端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RuntimeManager:
    def __init__(self, config: Optional[Config] = None) -> None:
        cfg = config or Config.from_env()
        self.instances: Dict[str, Instance] = {}
        self.idle_timeout_s = cfg.idle_timeout_s
        self.heartbeat_interval_s = cfg.heartbeat_interval_s
        self.ready_timeout_s = cfg.ready_timeout_s
        self.auto_restart = cfg.auto_restart  # R10 故障后可选重启
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor.start()

    # -------------------------------------------------- 阶段1: 创建与绑定

    def spawn(self, tool: str, owner: str = "unknown") -> Instance:
        # 校验工具名 (失败会抛 KeyError, 由网关规范化)
        from hacoo.adapters import get_adapter_class
        get_adapter_class(tool)

        port = _free_port()  # R2
        proc = self._start_process(tool, port)  # R1: 进程启动, 纳入运行时管理
        instance_id = "inst-" + uuid.uuid4().hex[:12]
        inst = Instance(instance_id=instance_id, tool=tool, port=port,
                        proc=proc, owner=owner)
        try:
            self._wait_ready(inst)  # R3: 就绪检查
        except Exception:
            self._kill(inst)
            raise
        with self._lock:
            self.instances[instance_id] = inst  # R4: 绑定路由句柄
        log.info("spawned %s -> %s (owner=%s, pid=%s, port=%d)",
                 tool, instance_id, owner, proc.pid, port)
        return inst

    def _start_process(self, tool: str, port: int) -> subprocess.Popen:
        env = dict(os.environ)
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            [sys.executable, "-m", "hacoo.runtime.backend", "--tool", tool, "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    def _wait_ready(self, inst: Instance) -> None:
        deadline = time.time() + self.ready_timeout_s
        while time.time() < deadline:
            if inst.proc.poll() is not None:
                raise InstanceError("backend process exited during startup (rc=%s)" % inst.proc.returncode)
            try:
                self._backend_call(inst, "__heartbeat__", timeout=1.0)
                return
            except (OSError, protocol.ProtocolError):
                time.sleep(0.2)
        raise InstanceError("backend not ready within %.0fs" % self.ready_timeout_s)

    # -------------------------------------------------- 阶段2: 会话维护

    def route(self, instance_id: str) -> Instance:
        """R5: 根据 instance_id 路由到对应后端实例。"""
        with self._lock:
            inst = self.instances.get(instance_id)
        if inst is None:
            raise InstanceError("unknown or released instance_id: '%s'" % instance_id)
        return inst

    def call(self, instance_id: str, method: str, arguments: Dict[str, Any],
             request_id: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """R5+R6: 将请求转发给 instance_id 对应的活实例执行 (同实例串行化)。"""
        inst = self.route(instance_id)
        with inst.lock:
            resp = self._backend_call(inst, method, arguments, request_id, timeout)
            inst.last_used_at = time.time()
        return resp

    def _backend_call(self, inst: Instance, method: str,
                      arguments: Optional[Dict[str, Any]] = None,
                      request_id: Optional[str] = None,
                      timeout: Optional[float] = None) -> Dict[str, Any]:
        payload = {
            "method": method,
            "arguments": arguments or {},
            "instance_id": inst.instance_id,
            "request_id": request_id or uuid.uuid4().hex,
        }
        try:
            with socket.create_connection(("127.0.0.1", inst.port), timeout=timeout or 30.0) as conn:
                return protocol.roundtrip(conn, payload, timeout=timeout)
        except OSError as exc:
            raise InstanceError("backend %s unreachable: %s" % (inst.instance_id, exc))

    # -------------------------------------------------- 阶段3: 回收

    def release(self, instance_id: str) -> Dict[str, Any]:
        """R11: 客户端显式释放实例。"""
        with self._lock:
            inst = self.instances.pop(instance_id, None)
        if inst is None:
            raise InstanceError("unknown or released instance_id: '%s'" % instance_id)
        try:
            self._backend_call(inst, "__shutdown__", timeout=3.0)
        except (OSError, InstanceError, protocol.ProtocolError):
            pass
        self._kill(inst)
        log.info("released %s (owner=%s)", instance_id, inst.owner)
        return {"released": instance_id}

    def _kill(self, inst: Instance) -> None:
        if inst.proc and inst.proc.poll() is None:
            inst.proc.terminate()
            try:
                inst.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                inst.proc.kill()

    def _monitor_loop(self) -> None:
        """R7/R9/R10: 心跳监控 + 空闲超时 + 故障处理。"""
        while not self._stop.wait(self.heartbeat_interval_s):
            with self._lock:
                snapshot = list(self.instances.values())
            for inst in snapshot:
                if inst.proc.poll() is not None:  # R10: 进程已死
                    self._handle_failure(inst)
                    continue
                try:  # R7: 心跳
                    self._backend_call(inst, "__heartbeat__", timeout=2.0)
                except (OSError, InstanceError, protocol.ProtocolError):
                    self._handle_failure(inst)
                    continue
                if time.time() - inst.last_used_at > self.idle_timeout_s:  # R9
                    log.info("instance %s idle timeout, recycling", inst.instance_id)
                    try:
                        self.release(inst.instance_id)
                    except InstanceError:
                        pass

    def _handle_failure(self, inst: Instance) -> None:
        """R10: 故障检测 -> 清理 -> 可选重启 (保留原 instance_id 路由句柄)。"""
        with self._lock:
            if self.instances.get(inst.instance_id) is not inst:
                return  # 已被释放或已处理
        log.warning("instance %s (owner=%s) failed", inst.instance_id, inst.owner)
        if not self.auto_restart:
            with self._lock:
                self.instances.pop(inst.instance_id, None)
            return
        try:
            port = _free_port()
            inst.proc = self._start_process(inst.tool, port)
            inst.port = port
            inst.restarted += 1
            self._wait_ready(inst)
            log.info("instance %s restarted (attempt %d)", inst.instance_id, inst.restarted)
        except Exception as exc:
            log.error("restart of %s failed: %s; dropping instance", inst.instance_id, exc)
            with self._lock:
                self.instances.pop(inst.instance_id, None)

    # -------------------------------------------------- 多用户视图

    def list_instances(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        """按属主过滤 (None = 全部, 仅 admin 使用)。"""
        with self._lock:
            instances = list(self.instances.values())
        if owner is not None:
            instances = [i for i in instances if i.owner == owner]
        return [inst.to_dict() for inst in instances]

    def count_by_owner(self, owner: str) -> int:
        with self._lock:
            return sum(1 for i in self.instances.values() if i.owner == owner)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            ids = list(self.instances)
        for iid in ids:
            try:
                self.release(iid)
            except InstanceError:
                pass
