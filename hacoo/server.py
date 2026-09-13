"""HACOO 平台主服务: 网关 + 运行时管理 + TCP 前门 (A3/C3 的公共通信路径入口)。

启动 (推荐 CLI):
    python -m hacoo serve [--host 127.0.0.1] [--port 9877]
兼容入口:
    python -m hacoo.server [--host 127.0.0.1] [--port 9877]

关键约束 (SPEC): 上层客户端不直接接触任何 EDA 工具的 shell,
所有请求必须经 Access 层统一封装后到达这里。
"""
from __future__ import annotations

import argparse
import logging
import socket
import threading
from typing import Optional

from hacoo.comm import protocol
from hacoo.config import Config
from hacoo.gateway.gateway import Gateway

log = logging.getLogger("hacoo.server")


class HacooServer:
    """TCP 前门: 接收 RPCRequest, 交给网关, 返回 RPCResponse (C3/C4)。

    并发模型: 每客户端连接一个线程; 同实例调用在 RuntimeManager 内串行化,
    多用户并发安全 (实例所有权在网关层校验)。
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 gateway: Gateway = None) -> None:
        cfg = Config.from_env()
        self.host = host or cfg.host
        self.port = port if port is not None else cfg.port
        self.gateway = gateway or Gateway(config=cfg)
        self._stop = threading.Event()
        self._srv: socket.socket = None  # type: ignore

    @property
    def address(self) -> str:
        return "%s:%d" % (self.host, self.port)

    def serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        if self.port == 0:
            self.port = srv.getsockname()[1]
        srv.listen(32)
        srv.settimeout(0.5)
        self._srv = srv
        log.info("HACOO gateway listening on %s", self.address)
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break  # stop() 关闭了监听 socket
                threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()
        finally:
            self.gateway.shutdown()

    def stop(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass

    def _handle_conn(self, conn: socket.socket) -> None:
        """长连接: 同一连接上可顺序发送多个 RPC (C2 会话管理在客户端侧)。"""
        try:
            while True:
                try:
                    msg = protocol.recv_message(conn)  # C1 消息到达
                except protocol.ProtocolError:
                    break
                req = protocol.RPCRequest.from_dict(msg)  # G1 前置结构校验
                resp = self.gateway.handle(req)           # 网关全流程
                protocol.send_message(conn, resp.to_dict())  # C4 响应回传
        except protocol.ProtocolError as exc:
            log.warning("dropping connection: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="HACOO platform server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    HacooServer(args.host, args.port).serve()


if __name__ == "__main__":
    main()
