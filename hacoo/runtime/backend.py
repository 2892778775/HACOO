"""后端 worker 进程: 加载工具 adapter, 常驻状态, 通过 socket 服务请求 (R1/R3/R6)。

由 RuntimeManager 以子进程方式启动:
    python -m hacoo.runtime.backend --tool <tool_name> --port <port>

内部控制方法 (非 api_*, 仅运行时层使用):
    __heartbeat__     R7 心跳
    __shutdown__      R11 显式释放
    __list_methods__  网关注册表同步
"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
import time

from hacoo.adapters import get_adapter_class
from hacoo.comm import protocol

log = logging.getLogger("hacoo.backend")


class BackendServer:
    """单实例后端: 一个 adapter 对象 = 一份持久执行状态 (R6)。"""

    def __init__(self, tool: str, port: int) -> None:
        self.tool = tool
        self.port = port
        self.adapter = get_adapter_class(tool)()
        self.started_at = time.time()
        self._stop = threading.Event()
        self._srv: socket.socket = None  # type: ignore

    # ------------------------------------------------------------ 服务循环

    def serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(8)
        srv.settimeout(0.5)
        self._srv = srv
        log.info("backend[%s] listening on 127.0.0.1:%d", self.tool, self.port)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass

    # ------------------------------------------------------------ 请求处理

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            while True:
                try:
                    msg = protocol.recv_message(conn)
                except protocol.ProtocolError:
                    break
                resp = self._dispatch(msg)
                protocol.send_message(conn, resp)
                if msg.get("method") == "__shutdown__":
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, msg: dict) -> dict:
        request_id = msg.get("request_id", "")
        method = msg.get("method", "")
        t0 = protocol.now_ms()
        try:
            if method == "__heartbeat__":  # R7
                out = {"data": {"alive": True, "tool": self.tool,
                                "uptime_s": round(time.time() - self.started_at, 2)}}
            elif method == "__shutdown__":  # R11
                out = {"text": "backend shutting down"}
                threading.Thread(target=self._delayed_stop, daemon=True).start()
            elif method == "__list_methods__":
                out = {"data": [s.to_dict() for s in self.adapter.api_specs()]}
            elif method.startswith("api_"):
                # T3: 在后端实例上执行适配后的操作; T4: 输出规范化
                out = self.adapter.invoke(method, msg.get("arguments") or {})
            else:
                return protocol.RPCResponse(
                    request_id=request_id, status="error",
                    error={"code": "UNREGISTERED_METHOD",
                           "message": "backend only serves registered api_* methods, got '%s'" % method},
                    elapsed_ms=protocol.now_ms() - t0).to_dict()
            return protocol.RPCResponse(request_id=request_id, status="ok",
                                        data=out.get("data"), text=out.get("text"),
                                        artifacts=out.get("artifacts") or [],
                                        elapsed_ms=protocol.now_ms() - t0).to_dict()
        except Exception as exc:  # 工具原生错误规范化回传
            return protocol.RPCResponse(
                request_id=request_id, status="error",
                error={"code": "BACKEND_ERROR", "message": "%s: %s" % (type(exc).__name__, exc)},
                elapsed_ms=protocol.now_ms() - t0).to_dict()

    def _delayed_stop(self) -> None:
        time.sleep(0.1)
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="HACOO backend worker")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[hacoo.backend] %(message)s")
    BackendServer(args.tool, args.port).serve()


if __name__ == "__main__":
    main()
